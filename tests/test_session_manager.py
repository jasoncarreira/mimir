"""Per-channel SAGA session lifecycle (SPEC §5.6)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mimir.event_logger import init_logger
from mimir.session_manager import ChannelSession, SessionManager, _make_saga_session_id


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-proc")


def test_session_id_format():
    sid = _make_saga_session_id("dm-slack-alice")
    assert sid.startswith("saga-dm-slack-alice-")


@pytest.mark.asyncio
async def test_touch_creates_then_reuses_session():
    mgr = SessionManager(idle_minutes=60)
    s1 = await mgr.touch("c1")
    s2 = await mgr.touch("c1")
    assert s1 is s2
    assert s1.saga_session_id == s2.saga_session_id


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
    saga_session_id."""
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
        lambda: asyncio.create_task(mgr._fire_idle(s.saga_session_id, "c1")),
    )
    await asyncio.sleep(0.15)

    assert len(fired) == 1
    assert fired[0].saga_session_id == s.saga_session_id
    assert fired[0].ended is True

    # Next touch creates a brand-new session.
    s_next = await mgr.touch("c1")
    assert s_next.saga_session_id != s.saga_session_id


@pytest.mark.asyncio
async def test_touch_after_timer_fires_creates_fresh_session():
    """Race: timer fires → session removed; a touch() arriving immediately
    after must mint a new session (not resurrect the old one)."""
    mgr = SessionManager(idle_minutes=60)
    s = await mgr.touch("c1")
    s.idle_handle.cancel()
    await mgr._fire_idle(s.saga_session_id, "c1")  # simulate the timer

    s2 = await mgr.touch("c1")
    assert s2.saga_session_id != s.saga_session_id
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

    # Manually fire with a stale saga_session_id — should be a no-op.
    await mgr._fire_idle("saga-bogus-stale", "c1")
    assert fired == []


@pytest.mark.asyncio
async def test_increment_turn_count_only_for_active():
    # ``max_turns=0`` disables the cap so the simple increment test
    # isn't tripped by hitting the burst-cap default.
    mgr = SessionManager(idle_minutes=60, max_turns=0)
    await mgr.touch("c1")
    mgr.increment_turn_count("c1")
    mgr.increment_turn_count("c1")
    assert mgr._sessions["c1"].turn_count == 2


@pytest.mark.asyncio
async def test_turn_cap_forces_synthesis_for_burst_channel():
    """SPEC §5.6 / §16 item 17 — burst-messaging gap: a channel that
    never idles should still get session synthesis once turn_count
    reaches the cap. Synthesis fires via the same on_idle callback
    path as the idle timer; the session is removed from the manager
    so the next ``touch()`` mints a fresh id.
    """
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    # idle_minutes=60 so the idle timer never fires during the test;
    # the turn cap must be the trigger.
    mgr = SessionManager(idle_minutes=60, max_turns=3, on_idle=on_idle)
    first = await mgr.touch("c1")
    mgr.increment_turn_count("c1")  # 1
    mgr.increment_turn_count("c1")  # 2
    mgr.increment_turn_count("c1")  # 3 → triggers force-end

    # The forced end runs as a background task; wait for it with a
    # generous CI-safe timeout. asyncio.wait_for surfaces a clean
    # TimeoutError rather than a silent assertion failure.
    async def _wait_for_fired() -> None:
        while not fired:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_wait_for_fired(), timeout=2.0)
    assert len(fired) == 1, "burst-cap synthesis didn't fire"
    assert fired[0].saga_session_id == first.saga_session_id
    assert fired[0].turn_count == 3
    # Session must be gone so the next touch starts fresh.
    assert "c1" not in mgr._sessions

    second = await mgr.touch("c1")
    assert second.saga_session_id != first.saga_session_id


@pytest.mark.asyncio
async def test_turn_cap_spawns_exactly_one_task_even_past_cap():
    """Regression: with ``>=`` the spawn condition fires on every
    turn past the cap, accumulating no-op tasks. ``==`` fires exactly
    once. We over-increment to verify only one synthesis lands AND
    ``_pending_tasks`` never grows past the singular entry."""
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    mgr = SessionManager(idle_minutes=60, max_turns=3, on_idle=on_idle)
    await mgr.touch("c1")
    mgr.increment_turn_count("c1")  # 1
    mgr.increment_turn_count("c1")  # 2
    mgr.increment_turn_count("c1")  # 3 → spawn ONE task
    # Past-cap increments. The session.ended guard will short-circuit
    # them once the cap task runs, but they MUST NOT each spawn their
    # own task in the meantime.
    mgr.increment_turn_count("c1")  # 4 → would spawn under >=
    mgr.increment_turn_count("c1")  # 5 → would spawn under >=

    async def _wait_for_fired() -> None:
        while not fired:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_wait_for_fired(), timeout=2.0)
    # Give the loop one more pass in case extra tasks were lingering.
    await asyncio.sleep(0.01)
    assert len(fired) == 1, "cap fired more than once"
    assert len(mgr._pending_tasks) == 0, (
        "cap task pile-up: pending_tasks should drain to zero, "
        f"got {len(mgr._pending_tasks)}"
    )


@pytest.mark.asyncio
async def test_turn_cap_zero_disables():
    """max_turns=0 → cap disabled, only idle timer can end the session."""
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    mgr = SessionManager(idle_minutes=60, max_turns=0, on_idle=on_idle)
    await mgr.touch("c1")
    for _ in range(20):
        mgr.increment_turn_count("c1")
    await asyncio.sleep(0.05)
    assert fired == []
    assert "c1" in mgr._sessions
    assert mgr._sessions["c1"].turn_count == 20


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_turn_cap_task():
    """Hitting the cap schedules a force-end task. If shutdown lands
    before that task acquires the lock and dispatches, the cancel
    path must abort it cleanly — no synthesis callback should fire."""
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    mgr = SessionManager(idle_minutes=60, max_turns=1, on_idle=on_idle)
    await mgr.touch("c1")
    mgr.increment_turn_count("c1")  # schedules force-end task

    # Shutdown before the task gets to run — must cancel and await
    # cleanly without firing the synthesis callback.
    await mgr.shutdown()
    assert fired == []
    assert mgr._pending_tasks == set()


@pytest.mark.asyncio
async def test_end_now_triggers_synthesis_callback():
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    mgr = SessionManager(idle_minutes=60, on_idle=on_idle)
    s = await mgr.touch("c1")
    ended = await mgr.end_now("c1")
    assert ended is not None
    assert ended.saga_session_id == s.saga_session_id
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
    await mgr._fire_idle(s.saga_session_id, "c1")
    assert fired == [], "synthesis fired even though channel was busy"
    # Session is still active; idle_handle was re-armed.
    assert "c1" in mgr._sessions
    assert mgr._sessions["c1"].ended is False
    assert mgr._sessions["c1"].idle_handle is not None

    # Channel goes parked. Next timer fire dispatches.
    busy_state["busy"] = False
    await mgr._fire_idle(s.saga_session_id, "c1")
    assert len(fired) == 1
    assert fired[0].saga_session_id == s.saga_session_id


@pytest.mark.asyncio
async def test_touch_cancels_in_flight_fire_idle_task_after_timer_fired():
    """CR#3 regression: once the timer has fired, ``idle_handle`` is the
    spawned ``_fire_idle`` task — not the (now-spent) TimerHandle. ``touch``
    must cancel that inner task so a delayed ``_fire_idle`` can't end the
    just-touched session.

    Without this fix, the ``TimerHandle.cancel()`` in ``touch`` was a no-op
    once the timer had already fired, and only the saga-session-id bail
    check inside ``_fire_idle`` saved us under bursty churn.
    """
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    mgr = SessionManager(idle_minutes=60, on_idle=on_idle)
    s = await mgr.touch("c1")
    saga_id = s.saga_session_id

    # Force the timer to fire immediately. The scheduled callback
    # synchronously spawns the _fire_idle task and swaps idle_handle from
    # TimerHandle → Task; the task itself hasn't started executing yet
    # (asyncio.create_task only schedules it).
    timer_handle = s.idle_handle
    assert isinstance(timer_handle, asyncio.TimerHandle)
    timer_handle._run()  # run the call_later callback synchronously
    timer_handle.cancel()  # don't let the loop run it again

    assert isinstance(s.idle_handle, asyncio.Task), (
        "fire callback must replace idle_handle with the spawned task"
    )
    in_flight_task = s.idle_handle

    # touch() before the in-flight task gets a chance to run.
    s2 = await mgr.touch("c1")
    assert s2 is s
    assert s2.saga_session_id == saga_id

    # Yield so the cancelled task gets a chance to surface its cancellation.
    for _ in range(3):
        await asyncio.sleep(0)

    assert in_flight_task.cancelled() or in_flight_task.done(), (
        "touch() must cancel the in-flight _fire_idle task"
    )
    # Synthesis must not have fired — the session is alive and just touched.
    assert fired == []
    assert "c1" in mgr._sessions
    assert mgr._sessions["c1"].ended is False
    # And idle_handle is back to a fresh TimerHandle armed by touch().
    assert isinstance(mgr._sessions["c1"].idle_handle, asyncio.TimerHandle)


@pytest.mark.asyncio
async def test_fire_callback_is_no_op_for_replaced_session():
    """If the session has been replaced (or ``end_now`` ran) between the
    timer being scheduled and it firing, the timer callback must not spawn
    a stale ``_fire_idle`` task against the new session."""
    fired: list[ChannelSession] = []

    async def on_idle(session: ChannelSession) -> None:
        fired.append(session)

    mgr = SessionManager(idle_minutes=60, on_idle=on_idle)
    s_old = await mgr.touch("c1")
    timer_handle = s_old.idle_handle
    assert isinstance(timer_handle, asyncio.TimerHandle)

    # Simulate a race: the session got force-ended (end_now) before the
    # timer's callback ran. The callback must notice and bail rather than
    # spawning a task that would re-end the (now ended) session, or worse,
    # interfere with whatever fresh session a subsequent touch() created.
    await mgr.end_now("c1")
    assert len(fired) == 1  # end_now itself triggers the synthesis callback
    fired.clear()

    s_new = await mgr.touch("c1")
    assert s_new.saga_session_id != s_old.saga_session_id

    # Now run the *old* timer's callback synchronously — the session it was
    # bound to is gone; the callback must not spawn a stale task.
    timer_handle._run()
    timer_handle.cancel()
    for _ in range(3):
        await asyncio.sleep(0)

    assert fired == [], "stale timer callback should not have synthesized"
    assert mgr._sessions["c1"] is s_new
    assert s_new.ended is False


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
    await mgr._fire_idle(s.saga_session_id, "c1")
    assert fired == []
    assert "c1" in mgr._sessions  # still alive, deferred
