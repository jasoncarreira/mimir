"""Mid-turn user message injection — registry + middleware (issue #376).

PR 1 of the rollout in ``docs/internal/MID_TURN_MESSAGE_INJECTION.md``: the
per-turn injection registry and the ``before_model`` middleware that folds queued
user messages into the running turn at the next model-call boundary.

**Dormant until PR 2.** Nothing feeds the queue yet — the dispatcher's
in-flight routing (which calls :func:`inject_message`) is a later slice. So
``before_model`` is a no-op (empty queue) on every turn, and wiring the
middleware into the stack changes no behavior. ``run_turn`` registers an
in-flight entry per turn and drops it in a ``finally`` so a late inject after
the turn ends is rejected (``no_active_turn``).

Keying is by ``channel_id`` (the dispatcher serializes per channel, so at most
one turn per channel is in flight). The middleware reads the current
``channel_id`` via ``langgraph.config.get_config()`` — NOT off the ``runtime``
argument, which does not carry the ``RunnableConfig`` (see the spec).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.config import get_config

if TYPE_CHECKING:
    from .models import AgentEvent

log = logging.getLogger(__name__)


@dataclass
class _Inflight:
    """One in-flight turn's pending-injection queue + liveness flag.

    The queue holds whole ``AgentEvent``s (not just text) so a leftover — a
    message accepted after the turn's final ``before_model`` boundary — can be
    re-enqueued faithfully as its own next turn (PR 2), preserving author / ids /
    trigger. The middleware folds ``event.content`` at the boundary.

    ``folded`` accumulates the events actually drained into the turn so
    ``run_turn`` can record them durably (``TurnRecord.injected_inputs``,
    synthesis summary, turn viewer). Each entry is ``(event, fold_monotonic)``
    where ``fold_monotonic`` is ``time.monotonic()`` at the boundary that folded
    it — the SAME clock as ``TurnContext.started_at`` (PR 4), so ``run_turn`` can
    compute a ``t_ms`` offset that lines the message up on the turn-viewer
    timeline next to the events/tool-calls it interleaved with. ``queue``
    (pending) and ``folded`` (consumed) are disjoint: ``_drain`` moves events
    from one to the other.

    ``deferred`` maps the ``source_id`` of a folded message to the reason the
    agent punted it (chainlink #384 — the ``defer_injected_message`` tool). At
    turn end ``run_turn`` re-enqueues those events as their own fresh turns and
    marks the originating turn's ``injected_inputs`` entries ``deferred``.
    """

    queue: list["AgentEvent"] = field(default_factory=list)
    folded: list[tuple["AgentEvent", float]] = field(default_factory=list)
    deferred: dict[str, str] = field(default_factory=dict)
    active: bool = True


# channel_id -> in-flight state. Guarded by ``_LOCK`` because ``before_model``
# may run in a worker thread while the dispatcher (event loop) calls
# ``inject_message``.
_REGISTRY: dict[str, _Inflight] = {}
_LOCK = threading.Lock()


def register_inflight(channel_id: str | None) -> None:
    """Mark a turn in-flight for ``channel_id`` (called at ``run_turn`` start).

    Overwrites any prior entry for the channel — the dispatcher serializes per
    channel, so a leftover entry from a crashed turn is self-healed here.
    """
    if not channel_id:
        return
    with _LOCK:
        _REGISTRY[channel_id] = _Inflight()


def deactivate(
    channel_id: str | None,
) -> tuple[
    list["AgentEvent"],
    list[tuple["AgentEvent", float]],
    list[tuple["AgentEvent", str]],
]:
    """Atomically mark the turn done and drop its registry entry.

    Returns ``(leftovers, folded, deferred)`` under the same lock that pops the
    entry. ``leftovers`` are queued-but-not-folded events accepted after the
    final ``before_model`` boundary; ``folded`` and ``deferred`` are the durable
    visibility snapshots that ``run_turn`` records/re-enqueues. Taking all three
    snapshots atomically prevents a worker-thread ``before_model`` drain from
    moving events from ``queue`` to ``folded`` between separate
    ``folded_records()``/``deferred_records()`` reads and deactivation.
    """
    if not channel_id:
        return [], [], []
    with _LOCK:
        inflight = _REGISTRY.pop(channel_id, None)
        if inflight is None:
            return [], [], []
        inflight.active = False
        return _snapshot_inflight(inflight)


def _snapshot_inflight(
    inflight: _Inflight,
) -> tuple[
    list["AgentEvent"],
    list[tuple["AgentEvent", float]],
    list[tuple["AgentEvent", str]],
]:
    """Return copied ``(leftovers, folded, deferred)`` for an in-flight turn.

    Caller must hold ``_LOCK``.
    """
    leftovers = list(inflight.queue)
    folded = list(inflight.folded)
    deferred = [
        (e, inflight.deferred[e.source_id])
        for e, _t in inflight.folded
        if e.source_id in inflight.deferred
    ]
    return leftovers, folded, deferred


def inject_startup_messages(channel_id: str | None, events: list["AgentEvent"]) -> int:
    """Queue startup-drained follow-ups for the active turn (chainlink #383).

    Unlike :func:`inject_message`, this path runs inside ``run_turn`` after
    :func:`register_inflight` has armed the channel and after the dispatcher has
    removed the events from its per-channel FIFO. A concurrent ``deactivate``
    race cannot happen on this path, but keep the same active-entry check so a
    setup-phase failure never silently parks events on a stale registry. Returns
    the number accepted.
    """
    if not channel_id or not events:
        return 0
    with _LOCK:
        inflight = _REGISTRY.get(channel_id)
        if inflight is None or not inflight.active:
            return 0
        inflight.queue.extend(events)
        return len(events)


def inject_message(channel_id: str, event: "AgentEvent") -> str:
    """Queue a user-message ``event`` for the in-flight turn on ``channel_id``.

    Returns ``"injected"`` when the turn is active, or ``"no_active_turn"`` when
    no turn is running (so the dispatcher falls back to enqueuing a fresh event).
    The whole event is stored so an un-folded leftover re-enqueues faithfully.
    """
    with _LOCK:
        inflight = _REGISTRY.get(channel_id)
        if inflight is None or not inflight.active:
            return "no_active_turn"
        inflight.queue.append(event)
        return "injected"


def _drain(channel_id: str | None) -> list["AgentEvent"]:
    """Pop all queued events for ``channel_id`` (FIFO); ``[]`` when none.

    Drained events are recorded on ``folded`` (stamped with the fold time) so
    :func:`folded_records` can report what the turn absorbed and when (durable
    visibility).
    """
    if not channel_id:
        return []
    with _LOCK:
        inflight = _REGISTRY.get(channel_id)
        if inflight is None or not inflight.queue:
            return []
        drained = inflight.queue[:]
        inflight.queue.clear()
        now = time.monotonic()
        inflight.folded.extend((e, now) for e in drained)
        return drained


def folded_records(channel_id: str | None) -> list[tuple["AgentEvent", float]]:
    """``(event, fold_monotonic)`` for every message folded into the in-flight
    turn on ``channel_id`` so far.

    Read in ``run_turn`` BEFORE :func:`deactivate` pops the entry, so the turn
    can record the mid-turn inputs it consumed and place them on the timeline
    (``TurnRecord.injected_inputs`` with a start-relative ``t_ms``). ``[]`` when
    nothing was folded or the channel has no active turn.
    """
    if not channel_id:
        return []
    with _LOCK:
        inflight = _REGISTRY.get(channel_id)
        if inflight is None:
            return []
        return list(inflight.folded)


def defer_message(channel_id: str | None, source_id: str | None, reason: str) -> str:
    """Mark a folded message as deferred to its own later turn (chainlink #384).

    Returns one of:
      - ``"deferred"`` — recorded; ``run_turn`` will re-enqueue it as a fresh
        force-new-turn event at turn end.
      - ``"not_found"`` — no message with that ``source_id`` was folded into the
        current turn (the only messages eligible to defer).
      - ``"already_deferred"`` — idempotent no-op; it's already marked.
      - ``"no_active_turn"`` — no injectable turn is in flight on the channel.

    Note this only RECORDS intent. Whether the model also answers the deferred
    message in this turn's final response is a cooperative contract the runtime
    cannot enforce — the text is already in the model's context.
    """
    if not channel_id or not source_id:
        return "not_found"
    with _LOCK:
        inflight = _REGISTRY.get(channel_id)
        if inflight is None or not inflight.active:
            return "no_active_turn"
        if source_id in inflight.deferred:
            return "already_deferred"
        if not any(e.source_id == source_id for e, _t in inflight.folded):
            return "not_found"
        inflight.deferred[source_id] = reason or ""
        return "deferred"


def deferred_records(channel_id: str | None) -> list[tuple["AgentEvent", str]]:
    """``(folded_event, reason)`` for every message the agent deferred this turn.

    Read in ``run_turn`` BEFORE :func:`deactivate` pops the entry, so the turn
    can (a) re-enqueue each as its own fresh turn and (b) mark the matching
    ``injected_inputs`` entry ``deferred``. ``[]`` when nothing was deferred.
    """
    if not channel_id:
        return []
    with _LOCK:
        inflight = _REGISTRY.get(channel_id)
        if inflight is None:
            return []
        return [
            (e, inflight.deferred[e.source_id])
            for e, _t in inflight.folded
            if e.source_id in inflight.deferred
        ]


def render_injected_message(event: "AgentEvent") -> str:
    """Render a folded event into the text the model sees mid-turn.

    A normal ``user_message`` reaches the model via
    ``prompts.build_turn_prompt``, which appends an ``Attachments:`` block and an
    author / msg-id header to the body. Folding only ``event.content`` would
    silently drop attachment paths (and authorship) so the model couldn't act on
    a mid-turn attachment (mimir's #593 review). This mirrors that body
    rendering so a folded message carries the same actionable content.
    """
    author = event.author_display or event.author or "-"
    msg_id_part = f", msg_id: {event.source_id}" if event.source_id else ""
    body = event.content or "(no content)"
    if event.attachment_names:
        paths = "\n".join(f"- {p}" for p in event.attachment_names)
        body = f"{body}\n\nAttachments:\n{paths}"
    return f"[mid-turn message from {author}{msg_id_part}]\n{body}"


def _current_channel_id() -> str | None:
    """Read ``channel_id`` from the live LangGraph config.

    ``get_config()`` raises outside a graph run context (e.g. in a bare unit
    test that calls ``before_model`` directly without monkeypatching it); treat
    that as "no channel" so the hook degrades to a no-op rather than erroring.
    """
    try:
        return get_config().get("configurable", {}).get("channel_id")
    except Exception:  # noqa: BLE001 — no graph context / missing config
        return None


class MidTurnInjectionMiddleware(AgentMiddleware):
    """Fold queued mid-turn user messages into the running turn at each
    model-call boundary (issue #376). No-op while the per-turn queue is empty,
    which is every turn until the dispatcher feeds it (PR 2)."""

    def before_model(self, state, runtime):  # noqa: ANN001 — langchain hook shape
        pending = _drain(_current_channel_id())
        if not pending:
            return None  # common case: one dict lookup, no state change
        # The ``messages`` channel uses an append reducer, so returning new
        # HumanMessages folds them into the conversation before the next call.
        # Render each event (content + attachments + author/msg-id) so a
        # mid-turn attachment isn't dropped — NOT just ``event.content``.
        return {
            "messages": [
                HumanMessage(content=render_injected_message(e)) for e in pending
            ]
        }
