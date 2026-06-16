"""Dispatcher concurrency & ordering (SPEC §4.5)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from mimir.config import Config
from mimir.dispatcher import Dispatcher, _ChannelQueue
from mimir.event_logger import init_logger
from mimir.models import AgentEvent


def _make_config(home: Path, **overrides) -> Config:
    cfg = Config.from_env()
    return replace(
        cfg,
        home=home,
        max_concurrent_turns=overrides.get("max_concurrent_turns", 4),
        max_channel_queue=overrides.get("max_channel_queue", 100),
        worker_idle_timeout_s=overrides.get("worker_idle_timeout_s", 1),
    )


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-proc")


@pytest.mark.asyncio
async def test_within_channel_events_run_in_order(tmp_path: Path):
    cfg = _make_config(tmp_path)

    seen: list[str] = []

    async def runner(event: AgentEvent) -> None:
        await asyncio.sleep(0.01)
        seen.append(event.content)

    disp = Dispatcher(cfg, runner)
    for i in range(5):
        await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content=str(i)))

    await disp.drain()
    assert seen == ["0", "1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_separate_channels_run_concurrently(tmp_path: Path):
    cfg = _make_config(tmp_path)
    started = asyncio.Event()
    second_started = asyncio.Event()
    release = asyncio.Event()
    finished: list[str] = []

    async def runner(event: AgentEvent) -> None:
        if event.channel_id == "slow":
            started.set()
            await release.wait()
            finished.append("slow")
        else:
            second_started.set()
            finished.append("fast")

    disp = Dispatcher(cfg, runner)
    await disp.enqueue(AgentEvent(trigger="x", channel_id="slow", content="0"))
    await started.wait()
    # slow channel is parked; a different channel must still progress
    await disp.enqueue(AgentEvent(trigger="x", channel_id="fast", content="0"))
    await asyncio.wait_for(second_started.wait(), timeout=1.0)
    assert finished == ["fast"]
    release.set()
    await disp.drain()
    assert "slow" in finished


@pytest.mark.asyncio
async def test_event_enqueued_during_worker_retire_is_not_stranded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """chainlink #302: an enqueue() that lands during the ``worker_retired``
    log (a yield point) sees ``worker.done() is False`` and spawns no
    replacement worker. The worker must notice the queued event and keep
    serving instead of retiring and stranding it — which would be a silent
    dropped event AND would hang ``drain()`` on the never-got item."""
    import mimir.dispatcher as dispatcher_mod

    cfg = _make_config(tmp_path, worker_idle_timeout_s=0.05)
    processed: list[str] = []

    async def runner(event: AgentEvent) -> None:
        processed.append(event.content)

    disp = Dispatcher(cfg, runner)

    real_log_event = dispatcher_mod.log_event
    raced = False

    async def racing_log_event(event_type: str, **kw):
        nonlocal raced
        # On the worker's first retire log, slip an event into the SAME
        # channel's queue before the worker decides to retire — exactly the
        # race window (worker parked here, not yet done → no respawn).
        if event_type == "worker_retired" and not raced:
            raced = True
            await disp.enqueue(
                AgentEvent(trigger="x", channel_id="c1", content="raced")
            )
        return await real_log_event(event_type, **kw)

    monkeypatch.setattr(dispatcher_mod, "log_event", racing_log_event)

    # "first" spawns the worker + is processed; the worker then idles, times
    # out, and on the worker_retired log our hook injects "raced".
    await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="first"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if raced:
            break
    # Buggy version strands "raced" → drain()'s queue.join() hangs; guard it
    # so the test fails on the assertion rather than hanging the suite.
    try:
        await asyncio.wait_for(disp.drain(), timeout=3.0)
    except asyncio.TimeoutError:
        pass

    assert "first" in processed
    assert "raced" in processed, "event enqueued during worker retire was stranded"


@pytest.mark.asyncio
async def test_global_semaphore_caps_in_flight(tmp_path: Path):
    cfg = _make_config(tmp_path, max_concurrent_turns=2)
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def runner(event: AgentEvent) -> None:
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1

    disp = Dispatcher(cfg, runner)
    for i in range(8):
        # Different channels so workers run concurrently.
        await disp.enqueue(AgentEvent(trigger="x", channel_id=f"c{i}", content="0"))

    await disp.drain()
    assert peak <= 2


@pytest.mark.asyncio
async def test_runner_exception_does_not_wedge_channel(tmp_path: Path):
    cfg = _make_config(tmp_path)
    seen: list[str] = []
    raised_once = False

    async def runner(event: AgentEvent) -> None:
        nonlocal raised_once
        if not raised_once:
            raised_once = True
            raise RuntimeError("synthetic")
        seen.append(event.content)

    disp = Dispatcher(cfg, runner)
    await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="0"))
    await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="1"))
    await disp.drain()
    # First event raised; second event still ran.
    assert seen == ["1"]


@pytest.mark.asyncio
async def test_runner_exception_logs_traceback(tmp_path: Path):
    """When run_turn raises, the dispatcher's structured error event must
    include a ``traceback`` field with the formatted traceback. Without
    this, a self-diagnosing event log can't tell which line in run_turn
    raised — the operator has to dig into stderr / app logs to learn
    anything (regression captured 2026-05-06: a RuntimeError from
    asyncio.create_task on a non-running loop showed up in events.jsonl
    as just ``RuntimeError: no running event loop`` with no frames).
    """
    import json

    cfg = _make_config(tmp_path)

    async def runner(event: AgentEvent) -> None:
        # Use a function call so the traceback frame names are informative.
        def _inner() -> None:
            raise RuntimeError("synthetic-explode")

        _inner()

    disp = Dispatcher(cfg, runner)
    await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="0"))
    await disp.drain()

    events_path = tmp_path / "logs" / "events.jsonl"
    rows = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]
    err_rows = [
        r
        for r in rows
        if r.get("type") == "error"
        and r.get("where") == "dispatcher.worker"
    ]
    assert err_rows, "expected a dispatcher.worker error event"
    err = err_rows[-1]
    assert "traceback" in err, "error event missing traceback field"
    tb = err["traceback"]
    assert "RuntimeError: synthetic-explode" in tb
    assert "_inner" in tb, "traceback should include the raising frame"


@pytest.mark.asyncio
async def test_is_channel_busy_tracks_in_flight_and_queued(tmp_path: Path):
    """``is_channel_busy`` distinguishes parked-on-get from work-in-flight.

    True iff a turn is currently inside ``run_turn`` for the channel OR
    events are queued for it. False once the worker is back on
    ``queue.get()``. Used by SessionManager to defer synthesis (SPEC §5.6).
    """
    cfg = _make_config(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(event: AgentEvent) -> None:
        started.set()
        await release.wait()

    disp = Dispatcher(cfg, runner)

    # No worker yet for "c1" — not busy.
    assert disp.is_channel_busy("c1") is False

    # Enqueue while runner is paused; turn enters run_turn → busy.
    await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="0"))
    await started.wait()
    assert disp.is_channel_busy("c1") is True

    # Queue a second event while the first is still in flight — still busy.
    await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="1"))
    assert disp.is_channel_busy("c1") is True

    # Release; both turns drain. After drain, no queue depth and no in-flight.
    release.set()
    await disp.drain()
    assert disp.is_channel_busy("c1") is False


@pytest.mark.asyncio
async def test_queue_full_returns_false(tmp_path: Path):
    cfg = _make_config(tmp_path, max_channel_queue=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(event: AgentEvent) -> None:
        started.set()
        await release.wait()

    disp = Dispatcher(cfg, runner)
    # Block one event in the runner so the next put hits maxsize.
    assert await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="a"))
    await started.wait()
    assert await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="b"))
    accepted = await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="c"))
    assert accepted is False
    release.set()
    await disp.drain()


# ─── CR2-#4: drain() must not deadlock on CancelledError ───────────────


@pytest.mark.asyncio
async def test_drain_completes_when_run_turn_is_cancelled(tmp_path: Path):
    """CR2-#4 regression: ``queue.task_done()`` must fire even when
    ``_run_turn`` raises ``CancelledError``. Pre-fix, the task_done()
    was outside the try/except, so a CancelledError raised inside
    _run_turn skipped it — leaving ``queue._unfinished_tasks > 0``
    and blocking ``await queue.join()`` in ``drain()`` forever.

    This test simulates that path: a runner that immediately raises
    CancelledError. ``drain()`` must complete within a reasonable
    timeout (here 2s — the deadlock shape would block indefinitely).
    """
    cfg = _make_config(tmp_path)
    cancelled_count = 0

    async def runner(event: AgentEvent) -> None:
        nonlocal cancelled_count
        cancelled_count += 1
        raise asyncio.CancelledError()

    disp = Dispatcher(cfg, runner)
    assert await disp.enqueue(
        AgentEvent(trigger="x", channel_id="c1", content="hi"),
    )
    # Bound the drain so the deadlock-shape test fails fast rather than
    # hanging the test runner.
    await asyncio.wait_for(disp.drain(), timeout=2.0)
    assert cancelled_count == 1


# ─── PR #110 review-followup: dict cleanup pin ─────────────────────────


@pytest.mark.asyncio
async def test_idle_worker_retires_and_cleans_up_per_channel_dicts(
    tmp_path: Path,
):
    """PR #110 review-followup: when a worker idle-times-out, its
    queue + high-water entries are removed from the per-channel
    dicts. Pre-fix, ephemeral channel_ids accumulated indefinitely.

    Cleanup is gated on ``queue.qsize() == 0`` and ``not _closed``
    so an in-flight ``drain()`` waiting on ``queue.join()`` doesn't
    lose its reference."""
    cfg = _make_config(tmp_path, worker_idle_timeout_s=0.05)

    async def runner(event: AgentEvent) -> None:
        return None

    disp = Dispatcher(cfg, runner)
    assert await disp.enqueue(
        AgentEvent(trigger="x", channel_id="c-ephemeral", content="hi"),
    )
    # Wait for the worker to drain + retire (idle_timeout 0.05s).
    for _ in range(20):
        await asyncio.sleep(0.05)
        if "c-ephemeral" not in disp._queues:
            break
    assert "c-ephemeral" not in disp._queues
    assert "c-ephemeral" not in disp._high_water_logged
    # chainlink #255: _workers was missing from the CR2 cleanup — the
    # done() Task lingered forever for ephemeral channel_ids.
    assert "c-ephemeral" not in disp._workers
    await disp.drain()


class TestSchedulerTickSerialization:
    """S2-3 — scheduler:* channels share a process-wide async mutex so
    the weekly reflect and an hourly heartbeat firing in the same minute
    don't race on shared state files (heartbeat-backlog.md,
    learnings-pending.md, proposed-changes.md)."""

    @pytest.mark.asyncio
    async def test_two_scheduler_ticks_serialize(self, tmp_path: Path) -> None:
        """Two scheduler-triggered turns enqueued back-to-back must run
        one-at-a-time even though they're on different channels and
        the global semaphore would otherwise let them run concurrently."""
        cfg = _make_config(tmp_path, max_concurrent_turns=4)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        order: list[str] = []

        async def runner(event: AgentEvent) -> None:
            if event.channel_id == "scheduler:reflect":
                first_started.set()
                await release_first.wait()
                order.append("reflect")
            elif event.channel_id == "scheduler:heartbeat":
                second_started.set()
                order.append("heartbeat")

        disp = Dispatcher(cfg, runner)
        await disp.enqueue(AgentEvent(
            trigger="scheduled_tick", channel_id="scheduler:reflect", content=""
        ))
        await first_started.wait()
        # Second scheduler tick on a DIFFERENT scheduler channel — must
        # wait for the first to release the cross-channel mutex.
        await disp.enqueue(AgentEvent(
            trigger="scheduled_tick", channel_id="scheduler:heartbeat", content=""
        ))
        # Give the worker a chance to TRY to start the second turn.
        await asyncio.sleep(0.05)
        # If serialization works, second_started is still unset.
        assert not second_started.is_set(), (
            "scheduler:heartbeat started while scheduler:reflect was holding "
            "the cross-job mutex"
        )

        release_first.set()
        await disp.drain()
        # Order is deterministic: reflect finishes before heartbeat starts.
        assert order == ["reflect", "heartbeat"]

    @pytest.mark.asyncio
    async def test_non_scheduler_turn_runs_concurrently_with_scheduler_tick(
        self, tmp_path: Path,
    ) -> None:
        """A user_message turn must NOT be blocked by an in-flight
        scheduler tick. The mutex only constrains scheduler:* among
        themselves; user-facing turns stay responsive."""
        cfg = _make_config(tmp_path, max_concurrent_turns=4)
        scheduler_started = asyncio.Event()
        release_scheduler = asyncio.Event()
        user_started = asyncio.Event()
        completed: list[str] = []

        async def runner(event: AgentEvent) -> None:
            if event.channel_id == "scheduler:reflect":
                scheduler_started.set()
                await release_scheduler.wait()
                completed.append("scheduler")
            elif event.channel_id == "discord-123":
                user_started.set()
                completed.append("user")

        disp = Dispatcher(cfg, runner)
        await disp.enqueue(AgentEvent(
            trigger="scheduled_tick", channel_id="scheduler:reflect", content=""
        ))
        await scheduler_started.wait()
        await disp.enqueue(AgentEvent(
            trigger="user_message", channel_id="discord-123", content="hi"
        ))
        # User turn proceeds even though scheduler is holding the
        # scheduler-tick lock.
        await asyncio.wait_for(user_started.wait(), timeout=1.0)
        assert completed == ["user"]
        release_scheduler.set()
        await disp.drain()
        assert "scheduler" in completed

    @pytest.mark.asyncio
    async def test_scheduler_lock_released_on_exception(
        self, tmp_path: Path,
    ) -> None:
        """If a scheduler turn raises, the lock must still release so
        the next scheduler turn isn't stuck waiting indefinitely.
        Regression for the deadlock-on-error class."""
        cfg = _make_config(tmp_path)
        completed: list[str] = []

        async def runner(event: AgentEvent) -> None:
            if event.content == "raise":
                raise RuntimeError("synthetic scheduler failure")
            completed.append(event.content)

        disp = Dispatcher(cfg, runner)
        await disp.enqueue(AgentEvent(
            trigger="scheduled_tick", channel_id="scheduler:reflect",
            content="raise",
        ))
        await disp.enqueue(AgentEvent(
            trigger="scheduled_tick", channel_id="scheduler:heartbeat",
            content="after-raise",
        ))
        await disp.drain()
        # Second scheduler turn ran — lock was released even though
        # the first raised.
        assert completed == ["after-raise"]

    @pytest.mark.asyncio
    async def test_two_non_scheduler_turns_run_concurrently(
        self, tmp_path: Path,
    ) -> None:
        """Sanity check: two user_message turns on different channels
        still run concurrently (max_concurrent_turns=4). The mutex
        change must not regress non-scheduler concurrency."""
        cfg = _make_config(tmp_path, max_concurrent_turns=4)
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release = asyncio.Event()
        completed: list[str] = []

        async def runner(event: AgentEvent) -> None:
            if event.channel_id == "discord-1":
                first_started.set()
                await release.wait()
                completed.append("c1")
            else:
                second_started.set()
                completed.append("c2")

        disp = Dispatcher(cfg, runner)
        await disp.enqueue(AgentEvent(
            trigger="user_message", channel_id="discord-1", content=""
        ))
        await first_started.wait()
        await disp.enqueue(AgentEvent(
            trigger="user_message", channel_id="discord-2", content=""
        ))
        # Second user_message proceeds in parallel — no scheduler-tick
        # mutex constrains it.
        await asyncio.wait_for(second_started.wait(), timeout=1.0)
        assert completed == ["c2"]
        release.set()
        await disp.drain()
        assert "c1" in completed


@pytest.mark.asyncio
async def test_drain_does_not_purge_dict_entries_for_busy_channels(
    tmp_path: Path,
):
    """Defensive: ``drain()`` sets ``self._closed = True`` before
    waiting on ``queue.join()``. The cleanup gate ``not self._closed``
    must NOT purge a queue entry while drain is iterating queue.values().
    """
    cfg = _make_config(tmp_path, worker_idle_timeout_s=0.05)
    release = asyncio.Event()

    async def runner(event: AgentEvent) -> None:
        await release.wait()

    disp = Dispatcher(cfg, runner)
    assert await disp.enqueue(
        AgentEvent(trigger="x", channel_id="c-busy", content="hi"),
    )
    # Worker is parked in runner — closed flag not yet set.
    drain_task = asyncio.create_task(disp.drain())
    # Brief yield to ensure drain started.
    await asyncio.sleep(0.02)
    # Channel is still tracked while drain is waiting.
    assert "c-busy" in disp._queues
    release.set()
    await drain_task


# ─── chainlink #376: mid-turn injection routing ──────────────────────

from mimir import mid_turn_injection as _mti  # noqa: E402


def _inj_config(home: Path, channels: tuple[str, ...]) -> Config:
    return replace(
        Config.from_env(),
        home=home,
        max_concurrent_turns=4,
        max_channel_queue=100,
        worker_idle_timeout_s=1,
        midturn_injection_channels=channels,
    )


@pytest.fixture(autouse=True)
def _clear_injection_registry():
    _mti._REGISTRY.clear()
    yield
    _mti._REGISTRY.clear()


def test_injection_enabled_prefix_matching(tmp_path: Path):
    disp = Dispatcher(_inj_config(tmp_path, ("discord-", "slack-")), None)
    assert disp._injection_enabled("discord-99")
    assert disp._injection_enabled("slack-C1")
    assert not disp._injection_enabled("poller:gmail-inbox")
    # Disabled by default (empty allow-list).
    assert not Dispatcher(_inj_config(tmp_path, ()), None)._injection_enabled("discord-99")
    # Wildcard enables all.
    assert Dispatcher(_inj_config(tmp_path, ("*",)), None)._injection_enabled("anything")


@pytest.mark.asyncio
async def test_enqueue_injects_when_in_flight_and_opted_in(tmp_path: Path):
    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    disp._in_flight.add("c1")          # simulate a running turn
    _mti.register_inflight("c1")
    accepted = await disp.enqueue(
        AgentEvent(trigger="user_message", channel_id="c1", content="folded")
    )
    assert accepted is True
    # Folded into the registry, NOT queued.
    assert "c1" not in disp._queues or disp._queues["c1"].qsize() == 0
    assert [e.content for e in _mti._drain("c1")] == ["folded"]


@pytest.mark.asyncio
async def test_enqueue_falls_back_to_queue_when_no_active_turn(tmp_path: Path):
    """In-flight + opted-in, but the registry has no active entry (the race where
    the turn ended) → inject_message returns no_active_turn → normal enqueue."""
    seen: list[str] = []

    async def runner(event: AgentEvent) -> None:
        seen.append(event.content)

    disp = Dispatcher(_inj_config(tmp_path, ("c",)), runner)
    disp._in_flight.add("c1")          # busy, but...
    # ...no register_inflight → inject_message → no_active_turn.
    await disp.enqueue(
        AgentEvent(trigger="user_message", channel_id="c1", content="fallback")
    )
    assert _mti._drain("c1") == []     # not injected
    await disp.drain()
    assert seen == ["fallback"]        # ran as a normal turn


@pytest.mark.asyncio
async def test_enqueue_skips_injection_with_queued_predecessor(tmp_path: Path):
    """A later user_message must not overtake an already-queued earlier event —
    injection is gated on an EMPTY queue, not the broad is_channel_busy()."""
    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    disp._in_flight.add("c1")
    _mti.register_inflight("c1")
    # Pre-seed a queued predecessor.
    q = asyncio.Queue(maxsize=disp._config.max_channel_queue)
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="earlier"))
    disp._queues["c1"] = q
    disp._high_water_logged["c1"] = False

    await disp.enqueue(
        AgentEvent(trigger="user_message", channel_id="c1", content="later")
    )
    # NOT injected (queue had a predecessor) → enqueued behind it.
    assert _mti._drain("c1") == []
    assert disp._queues["c1"].qsize() == 2


@pytest.mark.asyncio
async def test_enqueue_skips_injection_for_non_user_message(tmp_path: Path):
    """Only user_message events are eligible — a poller tick on an in-flight
    opted-in channel must not be folded."""
    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    disp._in_flight.add("c1")
    _mti.register_inflight("c1")
    await disp.enqueue(
        AgentEvent(trigger="poller", channel_id="c1", content="tick")
    )
    assert _mti._drain("c1") == []     # not injected
    assert disp._queues["c1"].qsize() == 1


@pytest.mark.asyncio
async def test_leftover_injection_reroutes_ahead_of_later_queued_event(tmp_path: Path):
    """mimir's #593 ordering finding: a mid-turn user message accepted by
    inject_message but never folded (the turn ended before the next
    before_model boundary) must run BEFORE a later non-user same-channel event
    that queued while the turn ran — and leftovers keep their own order.

    Re-routing via enqueue() would append the leftover behind the later event
    (tail); requeue_front() puts it at the head, preserving arrival order."""
    order: list[str] = []
    started = asyncio.Event()
    gate = asyncio.Event()

    async def runner(event: AgentEvent) -> None:
        order.append(event.content)
        if event.content == "turn1":
            started.set()
            await gate.wait()          # occupy the worker → channel in-flight

    disp = Dispatcher(_inj_config(tmp_path, ("c",)), runner)
    await disp.enqueue(AgentEvent(trigger="user_message", channel_id="c1", content="turn1"))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert "c1" in disp._in_flight

    # Two follow-ups arrive mid-turn and are accepted as injections, but the
    # stubbed turn never reaches a before_model boundary to fold them.
    _mti.register_inflight("c1")
    assert _mti.inject_message(
        "c1", AgentEvent(trigger="user_message", channel_id="c1", content="inject1")
    ) == "injected"
    assert _mti.inject_message(
        "c1", AgentEvent(trigger="user_message", channel_id="c1", content="inject2")
    ) == "injected"
    # A later NON-user same-channel event queues behind the running turn.
    await disp.enqueue(AgentEvent(trigger="react_received", channel_id="c1", content="later"))
    assert disp._queues["c1"].qsize() == 1

    # Turn ends: deactivate yields the unfolded leftovers; agent.py's finally
    # re-routes them to the FRONT (ahead of "later").
    leftovers, folded, deferred = _mti.deactivate("c1")
    assert [e.content for e in leftovers] == ["inject1", "inject2"]
    assert folded == []
    assert deferred == []
    assert disp.requeue_front(leftovers) == 2
    assert disp._queues["c1"].qsize() == 3     # leftovers ahead of the react

    gate.set()
    await disp.drain()

    # Both injected messages ran, in order, BEFORE the later-queued react.
    assert order == ["turn1", "inject1", "inject2", "later"]


@pytest.mark.asyncio
async def test_requeue_front_delivers_when_worker_already_retired(tmp_path: Path):
    """If no live queue exists (the worker retired before the leftover was
    re-routed), requeue_front still creates a queue + worker and delivers it."""
    seen: list[str] = []

    async def runner(event: AgentEvent) -> None:
        seen.append(event.content)

    disp = Dispatcher(_inj_config(tmp_path, ("c",)), runner)
    assert "c1" not in disp._queues
    assert disp.requeue_front(
        [AgentEvent(trigger="user_message", channel_id="c1", content="orphan")]
    ) == 1
    await disp.drain()
    assert seen == ["orphan"]


@pytest.mark.asyncio
async def test_requeue_front_noop_on_empty_or_closed(tmp_path: Path):
    """No events, or a closed dispatcher, is a zero-count no-op (never raises in
    run_turn's finally)."""
    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    assert disp.requeue_front([]) == 0
    disp._closed = True
    assert disp.requeue_front(
        [AgentEvent(trigger="user_message", channel_id="c1", content="x")]
    ) == 0


@pytest.mark.asyncio
async def test_enqueue_calls_on_inject_at_inject_time(tmp_path: Path):
    """PR 4: when a message is folded, on_inject fires immediately (true arrival
    time) so chat history threads it ahead of the running turn's later replies."""
    recorded: list[str] = []

    async def on_inject(event: AgentEvent) -> None:
        recorded.append(event.content)

    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    disp.set_on_inject(on_inject)
    disp._in_flight.add("c1")
    _mti.register_inflight("c1")
    accepted = await disp.enqueue(
        AgentEvent(trigger="user_message", channel_id="c1", content="folded")
    )
    assert accepted is True
    assert recorded == ["folded"]                       # recorded at inject time
    assert [e.content for e in _mti._drain("c1")] == ["folded"]  # and folded


@pytest.mark.asyncio
async def test_enqueue_skips_on_inject_when_not_folded(tmp_path: Path):
    """on_inject fires ONLY on a real fold — not on the no_active_turn fallback
    (in-flight but no active registry entry) which enqueues a normal turn."""
    recorded: list[str] = []
    ran: list[str] = []

    async def on_inject(event: AgentEvent) -> None:
        recorded.append(event.content)

    async def runner(event: AgentEvent) -> None:
        ran.append(event.content)

    disp = Dispatcher(_inj_config(tmp_path, ("c",)), runner)
    disp.set_on_inject(on_inject)
    disp._in_flight.add("c1")          # busy, but no register_inflight → no_active_turn
    await disp.enqueue(
        AgentEvent(trigger="user_message", channel_id="c1", content="fallback")
    )
    await disp.drain()
    assert recorded == []              # never folded → on_inject not called
    assert ran == ["fallback"]         # ran as a normal turn instead


@pytest.mark.asyncio
async def test_drain_startup_user_messages_drains_contiguous_user_prefix(tmp_path: Path):
    """chainlink #383 facet 1: back-to-back same-channel user messages that
    queued before the turn armed are drained for folding into the starting turn,
    and task_done accounting lets drain()/join() finish."""
    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    q = disp._queues["c1"] = _ChannelQueue(maxsize=disp._config.max_channel_queue)  # type: ignore[name-defined]
    disp._high_water_logged["c1"] = False
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="follow-1"))
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="follow-2"))

    drained = disp.drain_startup_user_messages("c1")

    assert [e.content for e in drained] == ["follow-1", "follow-2"]
    assert q.qsize() == 0
    await asyncio.wait_for(q.join(), timeout=1.0)


@pytest.mark.asyncio
async def test_drain_startup_user_messages_stops_at_non_user_boundary(tmp_path: Path):
    """A queued non-user event remains an ordering boundary: user messages behind
    it must not be startup-folded ahead of it."""
    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    q = disp._queues["c1"] = _ChannelQueue(maxsize=disp._config.max_channel_queue)  # type: ignore[name-defined]
    disp._high_water_logged["c1"] = False
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="follow-1"))
    await q.put(AgentEvent(trigger="react_received", channel_id="c1", content="react"))
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="follow-2"))

    drained = disp.drain_startup_user_messages("c1")

    assert [e.content for e in drained] == ["follow-1"]
    assert [q.get_nowait().content, q.get_nowait().content] == ["react", "follow-2"]
    q.task_done()
    q.task_done()
    await asyncio.wait_for(q.join(), timeout=1.0)


# ─── chainlink #384: force_new_turn (deferred messages) ──────────────


@pytest.mark.asyncio
async def test_force_new_turn_event_is_not_injected(tmp_path: Path):
    """A deferred (force_new_turn) message must NEVER be folded — even with an
    active in-flight turn it falls through to its own queued turn (loop guard)."""
    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    disp._in_flight.add("c1")
    _mti.register_inflight("c1")
    await disp.enqueue(AgentEvent(
        trigger="user_message", channel_id="c1", content="deferred topic",
        extra={"force_new_turn": True},
    ))
    assert _mti._drain("c1") == []                  # not injected
    assert disp._queues["c1"].qsize() == 1          # queued as its own turn


@pytest.mark.asyncio
async def test_drain_startup_treats_force_new_turn_as_boundary(tmp_path: Path):
    """A force_new_turn event in the queue prefix is a hard boundary: startup-
    drain stops at it so the deferred message keeps its own turn."""
    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    q = disp._queues["c1"] = _ChannelQueue(maxsize=disp._config.max_channel_queue)  # type: ignore[name-defined]
    disp._high_water_logged["c1"] = False
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="foldable"))
    await q.put(AgentEvent(
        trigger="user_message", channel_id="c1", content="deferred",
        extra={"force_new_turn": True},
    ))
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="behind"))

    drained = disp.drain_startup_user_messages("c1")

    assert [e.content for e in drained] == ["foldable"]          # stops at force_new_turn
    assert [q.get_nowait().content, q.get_nowait().content] == ["deferred", "behind"]
    q.task_done()
    q.task_done()
    await asyncio.wait_for(q.join(), timeout=1.0)


# ─── chainlink #510: bounded graceful drain ──────────────────────────


@pytest.mark.asyncio
async def test_drain_timeout_cancels_slow_inflight_turn(tmp_path: Path):
    """A turn slower than the drain timeout is cancelled so shutdown stays
    bounded (doesn't hang past the compose stop_grace_period)."""
    cfg = _make_config(tmp_path)
    started = asyncio.Event()
    finished: list[str] = []

    async def runner(event: AgentEvent) -> None:
        started.set()
        await asyncio.sleep(10)  # >> the drain timeout below
        finished.append(event.content)

    disp = Dispatcher(cfg, runner)
    await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="slow"))
    await asyncio.wait_for(started.wait(), timeout=2)  # ensure it's in-flight

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await disp.drain(timeout=0.2)
    elapsed = loop.time() - t0

    assert elapsed < 5, elapsed          # bounded by the 0.2s timeout, not 10s
    assert finished == []                # the slow turn was cancelled


@pytest.mark.asyncio
async def test_drain_timeout_lets_fast_turn_finish(tmp_path: Path):
    """A turn that finishes within the drain timeout completes (not cut off)."""
    cfg = _make_config(tmp_path)
    done: list[str] = []

    async def runner(event: AgentEvent) -> None:
        await asyncio.sleep(0.05)
        done.append(event.content)

    disp = Dispatcher(cfg, runner)
    await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="fast"))
    await disp.drain(timeout=5)
    assert done == ["fast"]


@pytest.mark.asyncio
async def test_enqueue_rejected_after_drain(tmp_path: Path):
    """Once draining/closed, new inbound is rejected cleanly (enqueue → False)."""
    cfg = _make_config(tmp_path)

    async def runner(event: AgentEvent) -> None:
        return None

    disp = Dispatcher(cfg, runner)
    await disp.drain(timeout=1)
    accepted = await disp.enqueue(
        AgentEvent(trigger="x", channel_id="c1", content="late")
    )
    assert accepted is False
