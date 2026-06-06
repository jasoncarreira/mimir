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
    """

    queue: list["AgentEvent"] = field(default_factory=list)
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


def deactivate(channel_id: str | None) -> list["AgentEvent"]:
    """Mark the turn done and drop the registry entry (``run_turn`` finally).

    Returns any events still queued — accepted by :func:`inject_message` but not
    folded (they arrived after the turn's final ``before_model`` boundary, e.g.
    while the model was generating its final response). ``run_turn`` re-enqueues
    these so the follow-up becomes its own next turn rather than vanishing.
    """
    if not channel_id:
        return []
    with _LOCK:
        inflight = _REGISTRY.pop(channel_id, None)
        if inflight is None:
            return []
        inflight.active = False
        return list(inflight.queue)


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
    """Pop all queued events for ``channel_id`` (FIFO); ``[]`` when none."""
    if not channel_id:
        return []
    with _LOCK:
        inflight = _REGISTRY.get(channel_id)
        if inflight is None or not inflight.queue:
            return []
        drained = inflight.queue[:]
        inflight.queue.clear()
        return drained


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
        return {"messages": [HumanMessage(content=e.content) for e in pending]}
