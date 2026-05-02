"""Per-channel SAGA session lifecycle (SPEC §5.6).

One ``ChannelSession`` per active channel, keyed by ``channel_id``. Every
inbound event (bridge, scheduler tick, HTTP injection) calls ``touch()``
*before* being enqueued onto the per-channel queue. The session has an
asyncio idle timer that fires after ``MIMIR_SAGA_SESSION_IDLE_MINUTES``
of silence; when it fires the manager:

1. **Busy check (SPEC §5.6).** Asks the dispatcher's ``is_channel_busy``
   predicate whether a turn is in flight or events are queued for this
   channel. If yes — the conversation isn't actually parked, just slow —
   re-arm the timer for another idle window and emit
   ``saga_session_idle_deferred``.
2. Otherwise: marks the session ended, drops it from the in-memory dict,
   and calls the ``on_idle`` callback (registered by the server) which
   enqueues a synthesis turn with ``trigger="saga_session_end"`` carrying
   the old ``saga_session_id`` in ``event.extra``.

The synthesis-turn agent code uses that id to:
- Filter the turn window from turns.jsonl
- Pass it to ``saga_end_session(session_id=...)``

A session **cannot reopen** after ending. The next inbound event for that
channel mints a fresh ``saga_session_id`` (SPEC §5.6).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .event_logger import log_event

log = logging.getLogger(__name__)


@dataclass
class ChannelSession:
    saga_session_id: str
    channel_id: str
    started_at: float
    last_message_at: float
    turn_count: int = 0
    idle_handle: asyncio.TimerHandle | None = field(default=None, repr=False)
    ended: bool = False


def _make_saga_session_id(channel_id: str) -> str:
    """``saga-<channel>-<epoch_ms>``. Channel id is sanitized for readability."""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in channel_id)
    return f"saga-{safe}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


# Type alias for the on-idle callback the server registers.
OnIdle = Callable[["ChannelSession"], Awaitable[None]]
# Type alias for the dispatcher's busy predicate.
IsBusy = Callable[[str], bool]


class SessionManager:
    def __init__(
        self,
        idle_minutes: int = 10,
        on_idle: OnIdle | None = None,
        is_busy: IsBusy | None = None,
    ) -> None:
        self._idle_seconds = max(1, int(idle_minutes * 60))
        self._on_idle = on_idle
        self._is_busy = is_busy
        self._sessions: dict[str, ChannelSession] = {}
        self._lock = asyncio.Lock()

    def _idle_seconds_value(self) -> int:
        return self._idle_seconds

    def set_on_idle(self, on_idle: OnIdle) -> None:
        """Register the idle callback after construction (avoids circular
        wiring: session manager → dispatcher → agent → run_turn)."""
        self._on_idle = on_idle

    def set_is_busy(self, is_busy: IsBusy) -> None:
        """Register the dispatcher's busy predicate. When the timer fires and
        the channel is busy, we defer instead of synthesizing — the
        conversation isn't actually parked, just slow."""
        self._is_busy = is_busy

    async def touch(self, channel_id: str) -> ChannelSession:
        """Ensure a session exists for ``channel_id`` and reset its idle timer.

        Caller MUST do this before enqueueing an event so the upcoming turn's
        ``TurnContext.saga_session_id`` reflects the live session.
        """
        async with self._lock:
            now = time.time()
            session = self._sessions.get(channel_id)
            if session is not None and not session.ended:
                if session.idle_handle is not None:
                    session.idle_handle.cancel()
                session.last_message_at = now
                session.idle_handle = self._schedule_idle(session)
                return session

            new_session = ChannelSession(
                saga_session_id=_make_saga_session_id(channel_id),
                channel_id=channel_id,
                started_at=now,
                last_message_at=now,
            )
            new_session.idle_handle = self._schedule_idle(new_session)
            self._sessions[channel_id] = new_session
            await log_event(
                "saga_session_started",
                channel_id=channel_id,
                saga_session_id=new_session.saga_session_id,
                idle_minutes=self._idle_seconds // 60,
            )
            return new_session

    def increment_turn_count(self, channel_id: str) -> None:
        """Bumped by the agent at the start of each turn — surfaces in
        ``saga_session_ended.turn_count`` for observability."""
        session = self._sessions.get(channel_id)
        if session and not session.ended:
            session.turn_count += 1

    async def end_now(self, channel_id: str) -> ChannelSession | None:
        """Force-end a session (e.g. from a bridge disconnect). Triggers the
        same synthesis-turn flow as the idle timer."""
        async with self._lock:
            session = self._sessions.pop(channel_id, None)
            if session is None or session.ended:
                return None
            session.ended = True
            if session.idle_handle is not None:
                session.idle_handle.cancel()
                session.idle_handle = None
        await self._dispatch_idle(session)
        return session

    async def shutdown(self) -> None:
        """Cancel all timers and drop all sessions. Called at app shutdown
        — does NOT trigger synthesis turns (the worker pool is draining)."""
        async with self._lock:
            for session in self._sessions.values():
                if session.idle_handle is not None:
                    session.idle_handle.cancel()
                    session.idle_handle = None
                session.ended = True
            self._sessions.clear()

    # ---- internals ------------------------------------------------------

    def _schedule_idle(self, session: ChannelSession) -> asyncio.TimerHandle:
        loop = asyncio.get_running_loop()
        return loop.call_later(
            self._idle_seconds,
            lambda: asyncio.create_task(self._fire_idle(session.saga_session_id, session.channel_id)),
        )

    async def _fire_idle(self, saga_session_id: str, channel_id: str) -> None:
        # Defer if the dispatcher reports the channel is busy (queued events
        # or a turn currently in run_turn). Re-arm the timer instead of
        # firing synthesis; the conversation isn't parked yet.
        if self._is_busy is not None and self._is_busy(channel_id):
            async with self._lock:
                session = self._sessions.get(channel_id)
                if session is None or session.saga_session_id != saga_session_id:
                    return
                if session.ended:
                    return
                # Re-arm. The just-fired handle is dead; replace it.
                session.idle_handle = self._schedule_idle(session)
            await log_event(
                "saga_session_idle_deferred",
                channel_id=channel_id,
                saga_session_id=saga_session_id,
                reason="worker_busy",
            )
            return

        async with self._lock:
            session = self._sessions.get(channel_id)
            if session is None or session.saga_session_id != saga_session_id:
                # touch() already replaced this session, or it was force-ended.
                return
            if session.ended:
                return
            session.ended = True
            self._sessions.pop(channel_id, None)
            if session.idle_handle is not None:
                session.idle_handle = None
        await self._dispatch_idle(session)

    async def _dispatch_idle(self, session: ChannelSession) -> None:
        duration_s = max(0.0, time.time() - session.started_at)
        await log_event(
            "saga_session_ended",
            channel_id=session.channel_id,
            saga_session_id=session.saga_session_id,
            duration_s=round(duration_s, 3),
            turn_count=session.turn_count,
        )
        if self._on_idle is not None:
            try:
                await self._on_idle(session)
            except Exception:  # noqa: BLE001
                log.exception("session on_idle handler failed for %s", session.channel_id)
                await log_event(
                    "error",
                    where="session_manager.on_idle",
                    channel_id=session.channel_id,
                    saga_session_id=session.saga_session_id,
                )
