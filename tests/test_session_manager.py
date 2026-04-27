"""Per-channel MSAM session lifecycle (SPEC §5.6)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mimir.event_logger import init_logger
from mimir.session_manager import ChannelSession, SessionManager, _make_msam_session_id


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-proc")


def test_session_id_format():
    sid = _make_msam_session_id("dm-slack-alice")
    assert sid.startswith("msam-dm-slack-alice-")


@pytest.mark.asyncio
async def test_touch_creates_then_reuses_session():
    mgr = SessionManager(idle_minutes=60)
    s1 = await mgr.touch("c1")
    s2 = await mgr.touch("c1")
    assert s1 is s2
    assert s1.msam_session_id == s2.msam_session_id


@pytest.mark.asyncio
async def test_touch_resets_idle_timer():
    """The first touch creates a session and arms a timer; the second touch
    must cancel the old handle (rather than racing to fire it)."""
    mgr = SessionManager(idle_minutes=60)
    s = await mgr.touch("c1")
    h1 = s.idle_handle
    s2 = await mgr.touch("c1")
    assert s2.idle_handle is not h1  # new handle
    assert h1 is not None and h1.cancelled()


@pytest.mark.asyncio
async def test_idle_timer_fires_callback_with_old_session():
    """SPEC §5.6: when the timer fires, the manager invokes ``on_idle`` with
    the (now-ended) ChannelSession; subsequent ``touch()`` mints a fresh
    msam_session_id."""
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    mgr = SessionManager(idle_minutes=60, on_idle=on_idle)
    s = await mgr.touch("c1")

    # Override the timer with a near-immediate callback.
    s.idle_handle.cancel()
    loop = asyncio.get_running_loop()
    s.idle_handle = loop.call_later(
        0.05,
        lambda: asyncio.create_task(mgr._fire_idle(s.msam_session_id, "c1")),
    )
    await asyncio.sleep(0.15)

    assert len(fired) == 1
    assert fired[0].msam_session_id == s.msam_session_id
    assert fired[0].ended is True

    # Next touch creates a brand-new session.
    s_next = await mgr.touch("c1")
    assert s_next.msam_session_id != s.msam_session_id


@pytest.mark.asyncio
async def test_touch_after_timer_fires_creates_fresh_session():
    """Race: timer fires → session removed; a touch() arriving immediately
    after must mint a new session (not resurrect the old one)."""
    mgr = SessionManager(idle_minutes=60)
    s = await mgr.touch("c1")
    s.idle_handle.cancel()
    await mgr._fire_idle(s.msam_session_id, "c1")  # simulate the timer

    s2 = await mgr.touch("c1")
    assert s2.msam_session_id != s.msam_session_id
    assert s2.ended is False


@pytest.mark.asyncio
async def test_stale_timer_callback_is_a_no_op():
    """Race: touch() replaces the session before the (now-stale) timer
    callback runs. Stale callback must not end the new session."""
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    mgr = SessionManager(idle_minutes=60, on_idle=on_idle)
    s_old = await mgr.touch("c1")
    s_new = await mgr.touch("c1")  # same id (touch reuses), but exercise no-op path
    assert s_old is s_new

    # Manually fire with a stale msam_session_id — should be a no-op.
    await mgr._fire_idle("msam-bogus-stale", "c1")
    assert fired == []


@pytest.mark.asyncio
async def test_increment_turn_count_only_for_active():
    mgr = SessionManager(idle_minutes=60)
    await mgr.touch("c1")
    mgr.increment_turn_count("c1")
    mgr.increment_turn_count("c1")
    assert mgr._sessions["c1"].turn_count == 2


@pytest.mark.asyncio
async def test_end_now_triggers_synthesis_callback():
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    mgr = SessionManager(idle_minutes=60, on_idle=on_idle)
    s = await mgr.touch("c1")
    ended = await mgr.end_now("c1")
    assert ended is not None
    assert ended.msam_session_id == s.msam_session_id
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_without_firing():
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    mgr = SessionManager(idle_minutes=60, on_idle=on_idle)
    await mgr.touch("c1")
    await mgr.shutdown()
    # No synthesis turn on shutdown — the worker pool is draining.
    assert fired == []


@pytest.mark.asyncio
async def test_idle_timer_defers_when_dispatcher_reports_busy():
    """SPEC §5.6: if the dispatcher reports the channel is busy when the
    session timer fires, defer (re-arm) instead of synthesizing. Synthesis
    fires only once the channel is actually parked.
    """
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    busy_state = {"busy": True}

    def is_busy(channel_id: str) -> bool:
        return busy_state["busy"]

    mgr = SessionManager(idle_minutes=60, on_idle=on_idle, is_busy=is_busy)
    s = await mgr.touch("c1")

    # Timer fires while busy — must defer.
    await mgr._fire_idle(s.msam_session_id, "c1")
    assert fired == [], "synthesis fired even though channel was busy"
    # Session is still active; idle_handle was re-armed.
    assert "c1" in mgr._sessions
    assert mgr._sessions["c1"].ended is False
    assert mgr._sessions["c1"].idle_handle is not None

    # Channel goes parked. Next timer fire dispatches.
    busy_state["busy"] = False
    await mgr._fire_idle(s.msam_session_id, "c1")
    assert len(fired) == 1
    assert fired[0].msam_session_id == s.msam_session_id


@pytest.mark.asyncio
async def test_set_is_busy_can_be_wired_after_construction():
    """``set_is_busy`` lets the server wire the dispatcher's predicate after
    both objects exist (same pattern as ``set_on_idle``)."""
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    mgr = SessionManager(idle_minutes=60, on_idle=on_idle)
    mgr.set_is_busy(lambda channel_id: True)  # always busy
    s = await mgr.touch("c1")
    await mgr._fire_idle(s.msam_session_id, "c1")
    assert fired == []
    assert "c1" in mgr._sessions  # still alive, deferred
