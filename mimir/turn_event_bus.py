"""In-process pub/sub for live, ephemeral turn events (chainlink #583, slice 1).

The post-hoc ``/api/v1/live-events`` stream is derived from ``turns.jsonl``,
which is written once at turn end — so the dashboard character *replays* a
turn's phases after it finishes instead of animating live. This bus closes
that gap: the turn loop publishes canonical, bracketed events as the turn
progresses, and SSE consumers (the dossier character first) subscribe.

Design notes:
- **Ephemeral + drop-allowed.** A slow/dead subscriber's bounded queue drops
  its OLDEST event rather than ever blocking the turn loop. Durable history
  stays in ``turns.jsonl`` / the post-hoc live-events stream; this bus is
  presentation-only. Missed events are self-healing — the final state is
  always recoverable from the durable stream.
- **Lock-free under asyncio.** ``publish`` is synchronous and never awaits, so
  on a single-threaded event loop it runs atomically with respect to
  ``subscribe``/``unsubscribe`` (which also never await mid-mutation). That is
  why a custom ~queue bus beats a generic signal lib here: the part that
  matters — bounded queues with drop-oldest backpressure — is ours either way,
  and this keeps it dependency-free and consistent with the WebChatBridge
  subscriber-queue pattern it generalizes.
- **Canonical envelope.** Every event is ``{type, phase, turn_id, channel_id,
  seq, ts, id?, ...payload}`` with ``phase ∈ {start, chunk, end}`` — see
  ``mimir/web_contracts.py`` for the wire contract. There are no atomic events:
  errors ride a terminal ``status`` on the relevant ``*`` end, and a tool's
  *execution* is its own ``tool_result`` span distinct from the ``tool_call``
  (the model emitting the call), sharing the same ``id`` so consumers join them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import AgentEvent

log = logging.getLogger(__name__)

DEFAULT_QUEUE_MAX = 256
WILDCARD = "*"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _offer(queue: "asyncio.Queue[dict[str, Any]]", event: dict[str, Any]) -> None:
    """Enqueue ``event``, dropping the oldest if the queue is full.

    Live state matters more than completeness, and the producer must never
    block, so a saturated subscriber loses its stalest event rather than
    backpressuring the turn loop.
    """
    try:
        queue.put_nowait(event)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
    except Exception:  # noqa: BLE001 — best-effort make-room
        pass
    try:
        queue.put_nowait(event)
    except Exception:  # noqa: BLE001 — never raise into the producer
        pass


class TurnEventBus:
    """Channel-keyed fan-out of live turn events to subscriber queues."""

    def __init__(self, *, queue_max: int = DEFAULT_QUEUE_MAX) -> None:
        self._queue_max = queue_max
        self._subscribers: dict[str, set["asyncio.Queue[dict[str, Any]]"]] = {}

    def subscribe(self, channel_id: str = WILDCARD) -> "asyncio.Queue[dict[str, Any]]":
        """Return a fresh bounded queue subscribed to ``channel_id``.

        ``channel_id=WILDCARD`` ("*") receives every channel's events. Callers
        MUST ``unsubscribe`` the returned queue when done.
        """
        queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue(maxsize=self._queue_max)
        self._subscribers.setdefault(channel_id, set()).add(queue)
        return queue

    def unsubscribe(self, channel_id: str, queue: "asyncio.Queue[dict[str, Any]]") -> None:
        subs = self._subscribers.get(channel_id)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._subscribers.pop(channel_id, None)

    def publish(self, event: dict[str, Any]) -> None:
        """Fan ``event`` out to its channel's subscribers + wildcard subscribers.

        Synchronous and non-blocking — safe to call from the hot turn loop.
        Never raises: a bad event must not break a turn.
        """
        try:
            channel_id = event.get("channel_id") or ""
            for key in (channel_id, WILDCARD):
                subs = self._subscribers.get(key)
                if not subs:
                    continue
                # Snapshot: a subscriber's drained queue can't mutate the set
                # here (no await), but copy defensively against re-entrancy.
                for queue in list(subs):
                    _offer(queue, event)
        except Exception:  # noqa: BLE001 — publishing must never break the caller
            log.debug("turn-event publish failed", exc_info=True)


class TurnEventEmitter:
    """Per-turn helper that brackets a turn's progress onto a :class:`TurnEventBus`.

    Scoped to one turn (own ``seq``/block counters) so concurrent turns on
    different channels never collide. Every method is a no-op when ``bus`` is
    ``None`` (feature unwired) and swallows its own errors — emission must never
    affect turn execution.
    """

    def __init__(
        self,
        bus: TurnEventBus | None,
        *,
        turn_id: str,
        channel_id: str | None,
    ) -> None:
        self._bus = bus
        self._turn_id = turn_id
        self._channel_id = channel_id or ""
        self._seq = 0
        self._block_n = 0
        # chainlink #587: parse only NEW messages per snapshot (incremental)
        # instead of re-walking the full list each time (was O(N²) on the loop
        # per turn). ``_tool_names`` carries tool_call id → name across snapshots
        # so a tool_result parsed in a later snapshot than its call keeps its name.
        self._parsed_count = 0  # messages already turned into events
        self._tool_names: dict[str, str] = {}
        # chainlink #583 slice 2: dedup between token-streamed spans (token_chunk)
        # and value-snapshot blocks (blocks_from_messages), keyed by tool id.
        self._streamed_tool_ids: set[str] = set()  # started live via token_chunk
        self._block_emitted_ids: set[str] = set()  # fully bracketed via blocks
        self._tc_index_id: dict[int, str] = {}  # messages-mode index → tool id

    @property
    def enabled(self) -> bool:
        return self._bus is not None

    def _emit(self, type_: str, phase: str, **payload: Any) -> None:
        if self._bus is None:
            return
        try:
            self._seq += 1
            event = {
                "type": type_,
                "phase": phase,
                "turn_id": self._turn_id,
                "channel_id": self._channel_id,
                "seq": self._seq,
                "ts": _now_iso(),
                **payload,
            }
            self._bus.publish(event)
        except Exception:  # noqa: BLE001 — emission is best-effort, never fatal
            log.debug("turn-event emit failed", exc_info=True)

    def turn_started(self, trigger_event: "AgentEvent | None" = None) -> None:
        self._emit("turn", "start", **_trigger_metadata(trigger_event))

    def turn_ended(
        self,
        *,
        status: str = "ok",
        error: str | None = None,
        outbound_message_sent: bool | None = None,
        injected_input_count: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {"status": status}
        if error:
            payload["error"] = error[:240]
        if outbound_message_sent is not None:
            payload["outbound_message_sent"] = bool(outbound_message_sent)
        if injected_input_count is not None:
            payload["injected_input_count"] = max(0, int(injected_input_count))
        self._emit("turn", "end", **payload)

    def injected_input(self, events: list["AgentEvent"]) -> None:
        if not events:
            return
        items = [_folded_input_metadata(e) for e in events]
        self._emit(
            "injected_input",
            "end",
            count=len(items),
            inputs=items,
        )

    def outbound_message(
        self,
        *,
        channel_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"sent": True}
        if channel_id:
            payload["target_channel_id"] = channel_id
        if message_id:
            payload["message_id"] = message_id
        self._emit("outbound_message", "end", **payload)

    def token_chunk(self, message_chunk: Any) -> None:
        """Stream token-level tool-call arg deltas from a messages-mode chunk.

        Slice 2: where the backend streams (codex-plus >=0.0.4, anthropic,
        openai), LangGraph "messages" mode yields ``AIMessageChunk``s carrying
        ``tool_call_chunks`` (partial args). The user-facing reply rides on the
        send_message tool call, so streaming those args streams the reply. The
        matching value-snapshot block then emits only the ``end`` (see
        ``_bracket``), so a span is never double-bracketed. Content/reasoning
        token deltas are left to slice-1 block brackets — the character already
        animates from those, and avoiding them sidesteps text-span id matching.
        """
        if self._bus is None:
            return
        try:
            pieces = getattr(message_chunk, "tool_call_chunks", None) or []
            for piece in pieces:
                index = piece.get("index")
                tid = piece.get("id")
                name = piece.get("name")
                args = piece.get("args")
                if tid:
                    # An explicit id opens (or reopens) a span. (Re)bind the
                    # index to it so a later tool call reusing the same index —
                    # e.g. a second call also at index 0 — can't leak its chunks
                    # into the previous call's span (#802 review).
                    span_id = tid
                    if index is not None:
                        self._tc_index_id[index] = tid
                elif index is not None and index in self._tc_index_id:
                    span_id = self._tc_index_id[index]
                else:
                    continue  # can't correlate this fragment to a span yet
                if span_id in self._block_emitted_ids:
                    continue  # the value-snapshot block path already owns it
                if span_id not in self._streamed_tool_ids:
                    self._streamed_tool_ids.add(span_id)
                    self._emit("tool_call", "start", id=span_id, tool_name=name or "unknown")
                if args:
                    self._emit("tool_call", "chunk", id=span_id, args_delta=args)
        except Exception:  # noqa: BLE001 — emission is best-effort, never fatal
            log.debug("turn-event token_chunk failed", exc_info=True)

    def blocks_from_messages(self, messages: list[Any]) -> None:
        """Bracket any new reasoning / tool_call / tool_result blocks.

        Reuses ``extract_turn_events`` (all the backend-specific parsing) and
        emits brackets only for items beyond what was already emitted, so it is
        safe to call on every ``astream`` snapshot. Whole blocks arrive as a
        single ``chunk`` between ``start``/``end`` — the same consumer code
        works whether a backend streams token deltas (anthropic/openai) or
        hands over whole blocks (codex-plus, claude-code).
        """
        if self._bus is None:
            return
        # Parse only the messages appended since the last snapshot. In
        # stream_mode="values" each snapshot is the full accumulated list and
        # messages are append-only + complete once present, so parsing the new
        # tail yields exactly the new blocks — without re-walking the whole list
        # (and re-deriving already-emitted events) on every snapshot.
        new = messages[self._parsed_count:]
        self._parsed_count = len(messages)
        if not new:
            return
        try:
            from .turn_logger import extract_turn_events

            events, _ = extract_turn_events(new)
        except Exception:  # noqa: BLE001 — parsing must never break the turn
            log.debug("turn-event block parse failed", exc_info=True)
            return
        for item in events:
            try:
                self._bracket(item)
            except Exception:  # noqa: BLE001
                log.debug("turn-event bracket failed", exc_info=True)

    def _bracket(self, item: dict[str, Any]) -> None:
        kind = item.get("type")
        if kind == "reasoning":
            self._block_n += 1
            span_id = f"{self._turn_id}:b{self._block_n}"
            text = item.get("content") or ""
            self._emit("reasoning", "start", id=span_id)
            if text:
                self._emit("reasoning", "chunk", id=span_id, text=text)
            self._emit("reasoning", "end", id=span_id)
        elif kind == "tool_call":
            self._block_n += 1
            span_id = item.get("id") or f"{self._turn_id}:b{self._block_n}"
            name = item.get("name") or "unknown"
            args = item.get("args")
            self._tool_names[span_id] = name  # for the later tool_result span
            if span_id in self._streamed_tool_ids:
                # token_chunk already streamed start + arg deltas live; just
                # close the span with the authoritative full args (slice 2).
                self._emit("tool_call", "end", id=span_id, tool_name=name, args=args)
            else:
                self._block_emitted_ids.add(span_id)
                self._emit("tool_call", "start", id=span_id, tool_name=name)
                if args is not None:
                    self._emit("tool_call", "chunk", id=span_id, args_delta=args)
                self._emit("tool_call", "end", id=span_id, tool_name=name, args=args)
        elif kind == "tool_result":
            self._block_n += 1
            # Reuse the tool_call id so consumers join call → result by (type,id).
            span_id = item.get("id") or f"{self._turn_id}:b{self._block_n}"
            name = item.get("name") or self._tool_names.get(span_id, "")
            content = item.get("content") or ""
            status = "error" if item.get("is_error") else "ok"
            self._emit("tool_result", "start", id=span_id, tool_name=name)
            if content:
                self._emit("tool_result", "chunk", id=span_id, content_delta=content)
            self._emit(
                "tool_result",
                "end",
                id=span_id,
                tool_name=name,
                status=status,
                content=content,
            )


def _clean_string(value: Any, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _trigger_metadata(event: "AgentEvent | None") -> dict[str, Any]:
    if event is None:
        return {}
    source_id = _clean_string(event.source_id)
    extra = event.extra if isinstance(event.extra, dict) else {}
    slack_thread_ts = _clean_string(extra.get("thread_ts")) or (
        source_id if event.source == "slack" else None
    )
    reply_to_message_id = slack_thread_ts or source_id
    payload: dict[str, Any] = {
        "trigger": _clean_string(event.trigger) or "unknown",
        "source": _clean_string(event.source),
        "source_id": source_id,
        "author": _clean_string(event.author),
        "author_display": _clean_string(event.author_display),
        "reply_to_message_id": reply_to_message_id,
    }
    if slack_thread_ts:
        payload["thread_ts"] = slack_thread_ts
    return {k: v for k, v in payload.items() if v is not None}


def _folded_input_metadata(event: "AgentEvent") -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "source": _clean_string(event.source),
            "source_id": _clean_string(event.source_id),
            "author": _clean_string(event.author),
            "author_display": _clean_string(event.author_display),
        }.items()
        if v is not None
    }
