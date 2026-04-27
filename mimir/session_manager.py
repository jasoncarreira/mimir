"""Per-channel MSAM session lifecycle (SPEC §5.6).

One ``ChannelSession`` per active channel, keyed by ``channel_id``. Every
inbound event (bridge, scheduler tick, HTTP injection) calls ``touch()``
*before* being enqueued onto the per-channel queue. The session has an
asyncio idle timer that fires after ``MIMIR_MSAM_SESSION_IDLE_MINUTES``
of silence; when it fires the manager:

1. Marks the session ended and drops it from the in-memory dict.
2. Calls the ``on_idle`` callback (registered by the server) which
   enqueues a synthesis turn with ``trigger="msam_session_end"`` carrying
   the old ``msam_session_id`` in ``event.extra``.

The synthesis-turn agent code uses that id to:
- Filter the turn window from turns.jsonl
- Pass it to ``msam_end_session(session_id=...)``

A session **cannot reopen** after ending. The next inbound event for that
channel mints a fresh ``msam_session_id`` (SPEC §5.6).
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
    msam_session_id: str
    channel_id: str
    started_at: float
    last_message_at: float
    turn_count: int = 0
    idle_handle: asyncio.TimerHandle | None = field(default=None, repr=False)
    ended: bool = False


def _make_msam_session_id(channel_id: str) -> str:
    """``msam-<channel>-<epoch_ms>``. Channel id is sanitized for readability."""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in channel_id)
    return f"msam-{safe}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


# Type alias for the on-idle callback the server registers.
OnIdle = Callable[["ChannelSession"], Awaitable[None]]


class SessionManager:
    def __init__(
        self,
        idle_minutes: int = 30,
        on_idle: OnIdle | None = None,
    ) -> None:
        self._idle_seconds = max(1, int(idle_minutes * 60))
        self._on_idle = on_idle
        self._sessions: dict[str, ChannelSession] = {}
        self._lock = asyncio.Lock()

    def _idle_seconds_value(self) -> int:
        return self._idle_seconds

    def set_on_idle(self, on_idle: OnIdle) -> None:
        """Register the idle callback after construction (avoids circular
        wiring: session manager → dispatcher → agent → run_turn)."""
        self._on_idle = on_idle

    async def touch(self, channel_id: str) -> ChannelSession:
        """Ensure a session exists for ``channel_id`` and reset its idle timer.

        Caller MUST do this before enqueueing an event so the upcoming turn's
        ``TurnContext.msam_session_id`` reflects the live session.
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
                msam_session_id=_make_msam_session_id(channel_id),
                channel_id=channel_id,
                started_at=now,
                last_message_at=now,
            )
            new_session.idle_handle = self._schedule_idle(new_session)
            self._sessions[channel_id] = new_session
            await log_event(
                "msam_session_started",
                channel_id=channel_id,
                msam_session_id=new_session.msam_session_id,
                idle_minutes=self._idle_seconds // 60,
            )
            return new_session

    def increment_turn_count(self, channel_id: str) -> None:
        """Bumped by the agent at the start of each turn — surfaces in
        ``msam_session_ended.turn_count`` for observability."""
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
            lambda: asyncio.create_task(self._fire_idle(session.msam_session_id, session.channel_id)),
        )

    async def _fire_idle(self, msam_session_id: str, channel_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(channel_id)
            if session is None or session.msam_session_id != msam_session_id:
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
            "msam_session_ended",
            channel_id=session.channel_id,
            msam_session_id=session.msam_session_id,
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
                    msam_session_id=session.msam_session_id,
                )
