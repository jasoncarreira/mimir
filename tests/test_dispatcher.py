"""Dispatcher concurrency & ordering (SPEC §4.5)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock

import pytest

from mimir.config import Config
from mimir.dispatcher import Dispatcher, TRUSTED_INTERNAL_SOURCES, _ChannelQueue
from mimir.event_logger import init_logger
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent
from mimir.server import _PairingNotifier
from mimir.worklink.continuation import HTTP_EVENT_INGRESS_EXTRA_KEY, HTTP_EVENT_INGRESS_EXTRA_VALUE


def _make_config(home: Path, **overrides) -> Config:
    cfg = Config.from_env()
    return replace(
        cfg,
        home=home,
        max_concurrent_turns=overrides.get("max_concurrent_turns", 4),
        max_channel_queue=overrides.get("max_channel_queue", 100),
        worker_idle_timeout_s=overrides.get("worker_idle_timeout_s", 1),
        access_control_enforced=overrides.get(
            "access_control_enforced", False
        ),
    )


def _resolver(tmp_path: Path, body: str) -> IdentityResolver:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "identities.yaml").write_text(dedent(body), encoding="utf-8")
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()
    return resolver


def test_dispatcher_callbacks_and_runner_can_be_cleared(tmp_path: Path):
    async def callback(*args) -> None:
        return None

    disp = Dispatcher(_make_config(tmp_path))
    disp.set_run_turn(callback)
    disp.set_on_inject(callback)
    disp.set_notice_sender(callback)
    disp.set_on_event(callback)
    disp.set_on_pairing_required(callback)
    disp.set_on_channel_idle(lambda channel_id: None)

    disp.set_run_turn(None)
    disp.set_on_inject(None)
    disp.set_notice_sender(None)
    disp.set_on_event(None)
    disp.set_on_pairing_required(None)
    disp.set_on_channel_idle(None)

    assert disp._run_turn is None
    assert disp._on_inject is None
    assert disp._notice_sender is None
    assert disp._on_event is None
    assert disp._on_pairing_required is None
    assert disp._on_channel_idle is None


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-proc")


@pytest.mark.asyncio
async def test_event_observer_failure_is_observed(tmp_path: Path, monkeypatch):
    from mimir import background_tasks

    failures = []
    release = asyncio.Event()
    monkeypatch.setattr(
        background_tasks, "log_event_sync",
        lambda event, **fields: failures.append((event, fields)),
    )

    async def observer(event):
        await release.wait()
        raise RuntimeError("observer failed")

    disp = Dispatcher(_make_config(tmp_path))
    disp.set_on_event(observer)
    try:
        assert await disp.enqueue(AgentEvent(
            channel_id="c1", source="api", trigger="user_message", content="hello",
        ))
        tasks = tuple(disp._bg_tasks)
        assert len(tasks) == 1
        # Let the task and its completion callback run without retrieving its result.
        completed = asyncio.Event()
        tasks[0].add_done_callback(lambda task: completed.set())
        release.set()
        await asyncio.wait_for(completed.wait(), timeout=2)
        assert not disp._bg_tasks
        assert failures == [("background_task_failed", {
            "name": "dispatcher-event-observer",
            "error": "RuntimeError: observer failed",
        })]
    finally:
        await disp.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("retire_workers", [False, True])
async def test_drain_cancels_retained_event_observer(tmp_path: Path, retire_workers):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def observer(event):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    disp = Dispatcher(_make_config(tmp_path))
    disp.set_on_event(observer)
    await disp.enqueue(AgentEvent(
        channel_id="c1", source="api", trigger="user_message", content="hello",
    ))
    await asyncio.wait_for(started.wait(), timeout=2)
    tasks = tuple(disp._bg_tasks)
    try:
        if retire_workers:
            await asyncio.wait_for(
                asyncio.gather(*disp._workers.values()), timeout=3,
            )
            assert not disp._workers
        await disp.drain(timeout=1)
        assert cancelled.is_set()
        assert all(task.cancelled() for task in tasks)
        assert not disp._bg_tasks
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await disp.drain(timeout=1)


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


def _poller_event(source_id: str) -> AgentEvent:
    return AgentEvent(
        trigger="poller",
        channel_id="poller:github-activity",
        content="review pull request",
        source="poller",
        source_id=source_id,
        extra={"poller_name": "github-activity", "items": []},
    )


@pytest.mark.asyncio
async def test_poller_delivery_relevance_actionable_subject_runs(tmp_path: Path):
    delivered: list[str | None] = []
    checks: list[str | None] = []

    async def relevance(event: AgentEvent) -> bool:
        checks.append(event.source_id)
        return True

    async def runner(event: AgentEvent) -> None:
        delivered.append(event.source_id)

    disp = Dispatcher(_make_config(tmp_path), runner)
    assert await disp.enqueue(_poller_event("actionable"), relevance_check=relevance)
    await disp.drain()

    assert checks == ["actionable"]
    assert delivered == ["actionable"]


@pytest.mark.asyncio
async def test_poller_delivery_relevance_non_actionable_subject_is_dropped(
    tmp_path: Path,
):
    delivered: list[str | None] = []

    async def runner(event: AgentEvent) -> None:
        delivered.append(event.source_id)

    async def stale(event: AgentEvent) -> bool:
        return False

    disp = Dispatcher(_make_config(tmp_path), runner)
    assert await disp.enqueue(_poller_event("merged"), relevance_check=stale)
    await disp.drain()

    records = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
    ]
    drops = [row for row in records if row["type"] == "poller_delivery_stale_dropped"]
    assert delivered == []
    assert len(drops) == 1
    assert drops[0]["poller"] == "github-activity"
    assert drops[0]["channel_id"] == "poller:github-activity"
    assert drops[0]["source_id"] == "merged"
    assert not any(row["type"] == "poller_turn_gave_up" for row in records)


@pytest.mark.asyncio
async def test_poller_delivery_relevance_indeterminate_fails_open(tmp_path: Path):
    """This guards the branch where fail-closed behavior would lose real work."""
    delivered: list[str | None] = []

    async def indeterminate(event: AgentEvent) -> None:
        return None

    async def runner(event: AgentEvent) -> None:
        delivered.append(event.source_id)

    disp = Dispatcher(_make_config(tmp_path), runner)
    assert await disp.enqueue(
        _poller_event("unknown"), relevance_check=indeterminate,
    )
    await disp.drain()

    assert delivered == ["unknown"]


@pytest.mark.asyncio
async def test_poller_delivery_without_relevance_predicate_is_unchanged(tmp_path: Path):
    delivered: list[str | None] = []

    async def runner(event: AgentEvent) -> None:
        delivered.append(event.source_id)

    disp = Dispatcher(_make_config(tmp_path), runner)
    assert await disp.enqueue(_poller_event("no-predicate"))
    await disp.drain()

    assert delivered == ["no-predicate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["resolved", "superseded"])
async def test_worklink_incident_relevance_drops_stale_turn_before_model(
    tmp_path: Path, transition: str,
) -> None:
    from mimir.pollers import _worklink_recovery_relevance_check
    from mimir.worklink.dispatch_failures import (
        dispatch_failure_state_dir,
        record_failure,
        record_success,
    )

    state_dir = dispatch_failure_state_dir(tmp_path)
    entry = record_failure(
        state_dir,
        issue_id=441,
        attempt=1,
        exit_status=1,
        error="original failure",
        log_path="run.log",
    )
    key = f"worklink-run-failure:441:{entry['signature']}:{entry['occurrence_id']}"
    incident = AgentEvent(
        trigger="poller",
        channel_id="poller:worklink-ready-queue",
        content="diagnose",
        source="poller",
        source_id=key,
        extra={
            "poller_name": "worklink-ready-queue",
            "items": [{
                "issue_id": 441,
                "error_signature": entry["signature"],
                "failure_occurrence_id": entry["occurrence_id"],
                "delivery_key": key,
            }],
        },
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    delivered: list[str | None] = []

    async def runner(event: AgentEvent) -> None:
        delivered.append(event.source_id)
        if event.source_id == "blocker":
            entered.set()
            await release.wait()

    disp = Dispatcher(_make_config(tmp_path), runner)
    blocker = replace(incident, source_id="blocker", content="hold")
    assert await disp.enqueue(blocker)
    await entered.wait()
    assert await disp.enqueue(
        incident,
        relevance_check=_worklink_recovery_relevance_check(state_dir),
    )
    if transition == "resolved":
        record_success(state_dir, 441)
    else:
        record_failure(
            state_dir,
            issue_id=441,
            attempt=2,
            exit_status=1,
            error="newer failure",
            log_path="newer.log",
        )
    release.set()
    await disp.drain()

    assert delivered == ["blocker"]


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
async def test_channel_drained_callback_fires_after_final_turn(tmp_path: Path):
    cfg = _make_config(tmp_path)
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def runner(event: AgentEvent) -> None:
        first_started.set()
        await release.wait()

    disp = Dispatcher(cfg, runner)
    drained: list[str] = []
    disp.set_on_channel_drained(drained.append)
    assert await disp.enqueue(AgentEvent(trigger="poller", channel_id="c1", content="1"))
    await first_started.wait()
    assert await disp.enqueue(AgentEvent(trigger="poller", channel_id="c1", content="2"))

    release.set()
    for _ in range(100):
        if drained:
            break
        await asyncio.sleep(0.01)
    assert drained == ["c1"]
    await disp.drain()


@pytest.mark.asyncio
async def test_channel_drained_callback_is_suppressed_during_shutdown(tmp_path: Path):
    """A final draining turn must not launch work against a closed dispatcher."""
    cfg = _make_config(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(event: AgentEvent) -> None:
        started.set()
        await release.wait()

    disp = Dispatcher(cfg, runner)
    drained: list[str] = []
    disp.set_on_channel_drained(drained.append)
    assert await disp.enqueue(
        AgentEvent(trigger="poller", channel_id="poller:github-activity", content="1")
    )
    await started.wait()

    drain_task = asyncio.create_task(disp.drain())
    await asyncio.sleep(0)
    assert disp._closed is True
    release.set()
    await drain_task

    assert drained == []


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
    retired: list[str] = []
    disp.set_on_channel_idle(retired.append)
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
    assert retired == ["c-ephemeral"]
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
        resolver = _resolver(
            tmp_path,
            """
            people:
              - canonical: alice
                aliases: [discord-1]
                access: {roles: [user]}
            """,
        )

        async def runner(event: AgentEvent) -> None:
            if event.channel_id == "scheduler:reflect":
                scheduler_started.set()
                await release_scheduler.wait()
                completed.append("scheduler")
            elif event.channel_id == "discord-123":
                user_started.set()
                completed.append("user")

        # Exercise concurrency after the real enforced ingress gate admits an
        # allowlisted bridge principal. Authorization must not be bypassed just
        # to keep scheduler ticks and user turns concurrent.
        cfg = replace(cfg, access_control_enforced=True)
        disp = Dispatcher(cfg, runner, resolver=resolver)
        assert await disp.enqueue(AgentEvent(
            trigger="scheduled_tick", channel_id="scheduler:reflect", content=""
        ))
        await scheduler_started.wait()
        try:
            assert await disp.enqueue(AgentEvent(
                trigger="user_message",
                channel_id="discord-123",
                content="hi",
                author="discord-1",
                source="discord",
            ))
            # User turn proceeds even though scheduler is holding the
            # scheduler-tick lock.
            await asyncio.wait_for(user_started.wait(), timeout=1.0)
            assert completed == ["user"]
        finally:
            release_scheduler.set()
            await disp.drain(timeout=1.0)
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
            trigger="user_message", channel_id="discord-1", content="", source="api",
        ))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await disp.enqueue(AgentEvent(
            trigger="user_message", channel_id="discord-2", content="", source="api",
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
        access_control_enforced=False,
        open_bridge=True,  # These routing tests intentionally use anonymous events.
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
async def test_slack_discord_denied_before_queue_observer_or_injection(tmp_path: Path):
    cfg = replace(
        _inj_config(tmp_path, ("discord-",)),
        access_control_enforced=True,
    )
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [discord-1]
            access: {roles: [user]}
        """,
    )
    disp = Dispatcher(cfg, resolver=resolver)
    observed: list[AgentEvent] = []

    async def on_event(event: AgentEvent) -> None:
        observed.append(event)

    disp.set_on_event(on_event)
    disp._in_flight.add("discord-chan")
    _mti.register_inflight("discord-chan")

    accepted = await disp.enqueue(
        AgentEvent(
            trigger="user_message",
            channel_id="discord-chan",
            content="do not leak",
            author="discord-2",
            author_display="Mallory",
            author_id="2",
            source="discord",
        )
    )

    assert accepted is False
    assert observed == []
    assert "discord-chan" not in disp._queues
    assert _mti._drain("discord-chan") == []

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    denied = [row for row in rows if row.get("type") == "inbound_event_denied"]
    assert len(denied) == 1
    assert denied[0]["source"] == "discord"
    assert denied[0]["channel_id"] == "discord-chan"
    assert denied[0]["author"] == "discord-2"
    assert denied[0]["raw_author_handle"] == "discord-2"
    assert denied[0]["canonical_author"] == "discord-2"
    assert denied[0]["reason"] == "unknown_author"
    assert "content" not in denied[0]
    assert not any(row.get("type") == "event_queued" for row in rows)


@pytest.mark.asyncio
async def test_allowlisted_slack_user_message_enqueues_normally(tmp_path: Path):
    cfg = _make_config(tmp_path, access_control_enforced=True)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ran: list[str] = []

    async def runner(event: AgentEvent) -> None:
        ran.append(event.content)

    disp = Dispatcher(cfg, runner, resolver=resolver)
    accepted = await disp.enqueue(
        AgentEvent(
            trigger="user_message",
            channel_id="slack-C1",
            content="hello",
            author="slack-U1",
            author_id="U1",
            source="slack",
        )
    )

    assert accepted is True
    await disp.drain()
    assert ran == ["hello"]


@pytest.mark.asyncio
async def test_default_compat_allows_non_allowlisted_discord_user(tmp_path: Path):
    cfg = replace(_make_config(tmp_path, access_control_enforced=False), open_bridge=True)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [discord-1]
        """,
    )
    ran: list[str] = []

    async def runner(event: AgentEvent) -> None:
        ran.append(event.content)

    disp = Dispatcher(cfg, runner, resolver=resolver)
    accepted = await disp.enqueue(
        AgentEvent(
            trigger="user_message",
            channel_id="discord-C1",
            content="legacy",
            author="discord-1",
            source="discord",
        )
    )

    assert accepted is True
    await disp.drain()
    assert ran == ["legacy"]

    rows = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    allowed = [row for row in rows if row.get("type") == "inbound_event_allowed"]
    assert len(allowed) == 1
    assert allowed[0]["status"] == "legacy_allowed"
    assert allowed[0]["enforcement_enabled"] is False


def test_open_bridge_env_is_opt_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MIMIR_OPEN_BRIDGE", raising=False)
    assert Config.from_env().open_bridge is False
    monkeypatch.setenv("MIMIR_OPEN_BRIDGE", "true")
    assert Config.from_env().open_bridge is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("author", "roles", "reason"),
    [
        ("discord-2", "[user]", "unknown_author"),
        ("discord-1", "[]", "user_not_allowlisted"),
        (None, "[user]", "missing_author"),
    ],
)
async def test_default_intake_refuses_discord_author_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    author: str | None, roles: str, reason: str,
):
    monkeypatch.delenv("MIMIR_ACCESS_CONTROL_ENFORCED", raising=False)
    monkeypatch.delenv("MIMIR_OPEN_BRIDGE", raising=False)
    cfg = _make_config(tmp_path)
    assert cfg.open_bridge is False
    resolver = _resolver(tmp_path, f"""
        people:
          - canonical: alice
            aliases: [discord-1]
            access: {{roles: {roles}}}
    """)
    ran: list[AgentEvent] = []
    observed: list[AgentEvent] = []

    async def runner(event: AgentEvent) -> None:
        ran.append(event)

    async def observer(event: AgentEvent) -> None:
        observed.append(event)

    disp = Dispatcher(cfg, runner, resolver=resolver)
    disp.set_on_event(observer)
    assert await disp.enqueue(AgentEvent(
        trigger="user_message", channel_id="discord-C1", content="hi",
        author=author, source="discord", author_id="2" if author else None,
    )) is False
    await disp.drain()
    assert ran == observed == []
    assert "discord-C1" not in disp._queues
    rows = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    denied = [row for row in rows if row.get("type") == "inbound_event_denied"]
    assert len(denied) == 1
    assert denied[0]["reason"] == reason
    assert denied[0]["intake_gate"] is True
    assert denied[0]["enforcement_enabled"] is False
    assert not any(row.get("type") in {"event_queued", "inbound_event_allowed"} for row in rows)


@pytest.mark.asyncio
async def test_shadow_mode_admits_allowlisted_bridge_author(tmp_path: Path):
    resolver = _resolver(tmp_path, """
        people:
          - canonical: alice
            aliases: [discord-1]
            access: {roles: [user]}
    """)
    ran: list[str] = []

    async def runner(event: AgentEvent) -> None:
        ran.append(event.content)

    disp = Dispatcher(_make_config(tmp_path), runner, resolver=resolver)
    assert await disp.enqueue(AgentEvent(
        trigger="user_message", channel_id="discord-C1", content="hi",
        author="discord-1", source="discord",
    )) is True
    await disp.drain()
    assert ran == ["hi"]
    rows = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    assert any(row.get("type") == "inbound_event_allowed" and
               row.get("enforcement_enabled") is False for row in rows)


@pytest.mark.asyncio
async def test_open_bridge_cannot_override_enforcement(tmp_path: Path):
    disp = Dispatcher(replace(_make_config(tmp_path, access_control_enforced=True), open_bridge=True),
                      resolver=_resolver(tmp_path, "people: []\n"))
    assert await disp.enqueue(AgentEvent(
        trigger="user_message", channel_id="discord-C1", content="hi",
        author="discord-unknown", source="discord",
    )) is False
    assert "discord-C1" not in disp._queues


@pytest.mark.asyncio
@pytest.mark.parametrize("source", sorted(TRUSTED_INTERNAL_SOURCES))
async def test_shadow_intake_keeps_internal_sources_trusted(tmp_path: Path, source: str):
    disp = Dispatcher(_make_config(tmp_path), resolver=_resolver(tmp_path, "people: []\n"))
    assert await disp.enqueue(AgentEvent(
        trigger="user_message", channel_id="internal-C1", content="hi",
        author="unknown", source=source,
    )) is True
    await disp.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["poller", "scheduled_tick", "synthesis"])
async def test_shadow_intake_keeps_non_user_triggers(tmp_path: Path, trigger: str):
    disp = Dispatcher(_make_config(tmp_path), resolver=_resolver(tmp_path, "people: []\n"))
    assert await disp.enqueue(AgentEvent(
        trigger=trigger, channel_id="discord-C1", content="hi",
        author="unknown", source="discord",
    )) is True
    await disp.drain()


@pytest.mark.asyncio
async def test_shadow_intake_gates_generic_http_even_with_internal_source(tmp_path: Path):
    disp = Dispatcher(_make_config(tmp_path), resolver=_resolver(tmp_path, "people: []\n"))
    assert await disp.enqueue(AgentEvent(
        trigger="synthesis", channel_id="api-C1", content="hi",
        author="unknown", source="api",
        extra={HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE},
    )) is False
    assert "api-C1" not in disp._queues


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("author", "source", "trigger", "http", "enforced", "open_bridge", "expected"),
    [
        ("discord-1", "discord", "user_message", False, False, False, True),
        ("discord-2", "discord", "user_message", False, False, False, False),
        (None, "discord", "user_message", False, False, False, False),
        ("discord-1", "discord", "user_message", False, True, False, True),
        ("discord-2", "discord", "user_message", False, True, True, False),
        ("discord-2", "discord", "user_message", False, False, True, True),
        ("unknown", "discord", "poller", False, False, False, True),
        ("unknown", "api", "user_message", False, False, False, True),
        ("unknown", "api", "synthesis", True, False, False, False),
        ("unknown", "discord", "user_message", False, False, False, False),
        *[("unknown", source, "user_message", False, False, False, True)
          for source in sorted(TRUSTED_INTERNAL_SOURCES)],
    ],
)
async def test_intake_admits_matches_authorization_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    author: str | None, source: str, trigger: str, http: bool,
    enforced: bool, open_bridge: bool, expected: bool,
):
    cfg = replace(_make_config(tmp_path, access_control_enforced=enforced), open_bridge=open_bridge)
    resolver = _resolver(tmp_path, """
        people:
          - canonical: alice
            aliases: [discord-1]
            access: {roles: [user]}
    """)
    disp = Dispatcher(cfg, resolver=resolver)
    logs: list[str] = []
    pairing: list[AgentEvent] = []

    async def record_log(kind: str, **fields) -> None:
        logs.append(kind)

    async def record_pairing(event: AgentEvent, decision) -> None:
        pairing.append(event)

    monkeypatch.setattr("mimir.dispatcher.log_event", record_log)
    disp.set_on_pairing_required(record_pairing)
    event = AgentEvent(
        trigger=trigger, channel_id="discord-C1", content="hi", author=author,
        source=source,
        extra={HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE} if http else {},
    )
    assert disp.intake_admits(event) is expected
    assert logs == pairing == []
    assert disp._queues == {}
    assert await disp._authorize_bridge_event(event) is expected
    if not expected:
        assert logs == ["inbound_event_denied"]
        assert pairing == [event]


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [True, False])
async def test_authorization_uses_shared_intake_decision(tmp_path: Path, monkeypatch, admitted):
    """Admission and audit metadata consume one decision even if the rule changes."""
    from mimir.access_control import authorize_inbound

    disp = Dispatcher(_make_config(tmp_path), resolver=_resolver(tmp_path, "people: []\n"))
    event = AgentEvent(trigger="user_message", channel_id="discord-C1", content="hi",
                       author="discord-2", source="discord")
    decision = authorize_inbound(event, disp._identity_resolver, enforce=not admitted)
    opposite = authorize_inbound(event, disp._identity_resolver, enforce=admitted)
    calls: list[AgentEvent] = []
    logs: list[tuple[str, dict]] = []

    def changing_decision(incoming: AgentEvent):
        calls.append(incoming)
        return decision if len(calls) == 1 else opposite

    async def record_log(kind: str, **fields) -> None:
        logs.append((kind, fields))

    monkeypatch.setattr(disp, "_intake_decision", changing_decision)
    monkeypatch.setattr("mimir.dispatcher.log_event", record_log)
    assert disp.intake_admits(event) is admitted
    assert calls == [event]
    assert logs == []
    calls.clear()
    assert await disp._authorize_bridge_event(event) is admitted
    assert calls == [event]
    kind, fields = logs[0]
    assert kind == ("inbound_event_allowed" if admitted else "inbound_event_denied")
    assert fields["status"] == decision.status.value
    if not admitted:
        assert fields["reason"] == decision.denial_reason


@pytest.mark.asyncio
async def test_access_gate_allows_non_user_messages_and_trusted_internal_sources(
    tmp_path: Path,
):
    cfg = _make_config(tmp_path, access_control_enforced=True)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ran: list[tuple[str, str | None]] = []

    async def runner(event: AgentEvent) -> None:
        ran.append((event.trigger, event.source))

    disp = Dispatcher(cfg, runner, resolver=resolver)
    assert await disp.enqueue(
        AgentEvent(
            trigger="poller",
            channel_id="slack-C1",
            content="tick",
            author="slack-unknown",
            source="slack",
        )
    )
    assert await disp.enqueue(
        AgentEvent(
            trigger="user_message",
            channel_id="api-C1",
            content="api",
            author="api-unknown",
            source="api",
        )
    )

    await disp.drain()
    assert ran == [("poller", "slack"), ("user_message", "api")]
    assert "web" in TRUSTED_INTERNAL_SOURCES


@pytest.mark.asyncio
async def test_unknown_external_source_is_gated_fail_closed(tmp_path: Path):
    cfg = _make_config(tmp_path, access_control_enforced=True)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ran: list[str] = []

    async def runner(event: AgentEvent) -> None:
        ran.append(event.content)

    disp = Dispatcher(cfg, runner, resolver=resolver)
    accepted = await disp.enqueue(
        AgentEvent(
            trigger="user_message",
            channel_id="bsky-C1",
            content="future bridge",
            author="bsky-unknown",
            source="bsky",
        )
    )

    assert accepted is False
    await disp.drain()
    assert ran == []

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    denied = [row for row in rows if row.get("type") == "inbound_event_denied"]
    assert len(denied) == 1
    assert denied[0]["source"] == "bsky"
    assert denied[0]["reason"] == "unknown_author"
    assert not any(row.get("type") == "event_queued" for row in rows)


@pytest.mark.asyncio
async def test_missing_source_user_message_is_gated_fail_closed(tmp_path: Path):
    cfg = _make_config(tmp_path, access_control_enforced=True)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    disp = Dispatcher(cfg, resolver=resolver)

    accepted = await disp.enqueue(
        AgentEvent(
            trigger="user_message",
            channel_id="mystery-C1",
            content="missing source",
            author="mystery-unknown",
        )
    )

    assert accepted is False
    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    denied = [row for row in rows if row.get("type") == "inbound_event_denied"]
    assert len(denied) == 1
    assert denied[0]["source"] == "unknown"
    assert denied[0]["reason"] == "unknown_author"


@pytest.mark.asyncio
async def test_unknown_dm_sender_gets_pairing_hook_without_normal_dispatch(
    tmp_path: Path,
):
    cfg = _make_config(tmp_path, access_control_enforced=False)
    resolver = _resolver(tmp_path, "people: []\n")
    ran: list[str] = []
    pairing: list[tuple[AgentEvent, str | None]] = []

    async def runner(event: AgentEvent) -> None:
        ran.append(event.content)

    async def on_pairing(event: AgentEvent, decision) -> None:
        pairing.append((event, decision.denial_reason))

    disp = Dispatcher(cfg, runner, resolver=resolver)
    disp.set_on_pairing_required(on_pairing)

    accepted = await disp.enqueue(
        AgentEvent(
            trigger="user_message",
            channel_id="dm-slack-D1",
            content="please help",
            author="slack-Uunknown",
            author_id="Uunknown",
            source="slack",
        )
    )

    assert accepted is False
    await disp.drain()
    # Let the fire-and-forget pairing callback complete.
    for _ in range(20):
        if pairing:
            break
        await asyncio.sleep(0.01)
    assert ran == []
    assert "dm-slack-D1" not in disp._queues
    assert pairing[0][0].author == "slack-Uunknown"
    assert pairing[0][1] == "unknown_author"

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert any(row.get("type") == "inbound_pairing_required" for row in rows)
    assert any(row.get("type") == "inbound_event_denied" and
               row.get("intake_gate") is True for row in rows)
    assert not any(row.get("type") == "event_queued" for row in rows)


@pytest.mark.asyncio
async def test_public_unauthorized_prompt_to_pair_logs_without_queueing(
    tmp_path: Path,
):
    cfg = replace(
        _make_config(tmp_path, access_control_enforced=False),
        unauthorized_user_behavior="prompt-to-pair",
    )
    resolver = _resolver(tmp_path, "people: []\n")
    ran: list[str] = []

    async def runner(event: AgentEvent) -> None:
        ran.append(event.content)

    disp = Dispatcher(cfg, runner, resolver=resolver)
    accepted = await disp.enqueue(
        AgentEvent(
            trigger="user_message",
            channel_id="slack-C1",
            content="public request",
            author="slack-Uunknown",
            source="slack",
        )
    )

    assert accepted is False
    await disp.drain()
    assert ran == []
    assert "slack-C1" not in disp._queues
    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert any(row.get("type") == "inbound_pairing_prompted" for row in rows)
    assert not any(row.get("type") == "event_queued" for row in rows)


@pytest.mark.asyncio
async def test_public_unknown_sender_gets_pairing_hook_without_public_send(
    tmp_path: Path,
):
    cfg = _make_config(tmp_path, access_control_enforced=False)
    resolver = _resolver(tmp_path, "people: []\n")
    pairing: list[tuple[AgentEvent, str | None]] = []

    async def on_pairing(event: AgentEvent, decision) -> None:
        pairing.append((event, decision.denial_reason))

    disp = Dispatcher(cfg, resolver=resolver)
    disp.set_on_pairing_required(on_pairing)

    accepted = await disp.enqueue(
        AgentEvent(
            trigger="user_message",
            channel_id="slack-C1",
            content="public request",
            author="slack-Uunknown",
            source="slack",
        )
    )

    assert accepted is False
    await disp.drain()
    assert pairing[0][0].channel_id == "slack-C1"
    assert pairing[0][1] == "unknown_author"
    assert "slack-C1" not in disp._queues


@pytest.mark.asyncio
async def test_intake_denial_warns_once_per_source_and_author_with_approval_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
):
    resolver = _resolver(tmp_path, "people: []\n")
    disp = Dispatcher(_make_config(tmp_path, access_control_enforced=True), resolver=resolver)
    sent = AsyncMock()
    disp.set_notice_sender(sent)
    event = AgentEvent(
        trigger="user_message", channel_id="discord-C1", content="hello",
        author="discord-1907001", author_id="1907001", source="discord",
    )
    with caplog.at_level("WARNING", logger="mimir.dispatcher"):
        assert await disp.enqueue(event) is False
        assert await disp.enqueue(event) is False
        # The limit is per process, not per Dispatcher instance.
        another = Dispatcher(_make_config(tmp_path, access_control_enforced=True), resolver=resolver)
        assert await another.enqueue(event) is False
        assert await disp.enqueue(AgentEvent(
            trigger="user_message", channel_id="slack-C1", content="hello",
            author="slack-1907001", author_id="1907001", source="slack",
        )) is False
    warnings = [r.message for r in caplog.records if r.name == "mimir.dispatcher" and
                r.message.startswith("Inbound message denied:")]
    assert len(warnings) == 2
    assert "source=discord" in warnings[0]
    assert "raw_author_handle='discord-1907001'" in warnings[0]
    assert "author_id='1907001'" in warnings[0]
    assert "canonical_identity='discord-1907001'" in warnings[0]
    assert "reason=unknown_author" in warnings[0]
    assert f"mimir identities approve-pairing discord-1907001 --home {tmp_path}" in warnings[0]
    assert "--admin" in warnings[0]
    assert "source=slack" in warnings[1]
    sent.assert_not_awaited()
    assert disp._queues == {}
    rows = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    assert [row["type"] for row in rows] == ["inbound_event_denied"] * 4


@pytest.mark.asyncio
async def test_intake_denial_hints_use_bounded_lru(tmp_path: Path, monkeypatch, caplog):
    from collections import OrderedDict

    import mimir.dispatcher as dispatcher

    # The cache is the subject of this test: own a small cache and restore it.
    cache = OrderedDict()
    monkeypatch.setattr(dispatcher, "_denial_hints_seen", cache)
    monkeypatch.setattr(dispatcher, "_DENIAL_HINTS_LIMIT", 3)
    disp = Dispatcher(
        _make_config(tmp_path, access_control_enforced=True),
        resolver=_resolver(tmp_path, "people: []\n"),
    )

    async def deny(author_id: str) -> None:
        assert await disp.enqueue(AgentEvent(
            trigger="user_message", channel_id="discord-C1", content="hello",
            author=f"discord-{author_id}", author_id=author_id, source="discord",
        )) is False
        assert len(cache) <= 3

    with caplog.at_level("WARNING", logger="mimir.dispatcher"):
        for author_id in ("1", "2", "3", "1", "4"):
            await deny(author_id)
        # Repeated denial refreshes recency without logging again.
        assert list(cache) == [("discord", "3"), ("discord", "1"), ("discord", "4")]
        await deny("2")  # evicted author receives a new hint
    warnings = [r.message for r in caplog.records if r.name == "mimir.dispatcher" and
                r.message.startswith("Inbound message denied:")]
    assert len(warnings) == 5
    assert list(cache) == [("discord", "1"), ("discord", "4"), ("discord", "2")]


@pytest.mark.asyncio
async def test_intake_denial_hint_escapes_raw_handle(tmp_path: Path, caplog):
    disp = Dispatcher(
        _make_config(tmp_path, access_control_enforced=True),
        resolver=_resolver(tmp_path, "people: []\n"),
    )
    raw_handle = "stranger\nFORGED warning\r\t"
    with caplog.at_level("WARNING", logger="mimir.dispatcher"):
        assert await disp.enqueue(AgentEvent(
            trigger="user_message", channel_id="discord-C1", content="hello",
            author=raw_handle, author_id="1907003", source="discord",
        )) is False
    warnings = [r.message for r in caplog.records if r.name == "mimir.dispatcher" and
                r.message.startswith("Inbound message denied:")]
    assert len(warnings) == 1
    assert f"raw_author_handle={raw_handle!r}" in warnings[0]
    assert f"canonical_identity={raw_handle!r}" in warnings[0]
    assert "\n" not in warnings[0]
    assert "\r" not in warnings[0]
    assert "\t" not in warnings[0]


@pytest.mark.asyncio
async def test_intake_warning_failure_still_denies(tmp_path: Path, monkeypatch):
    disp = Dispatcher(
        _make_config(tmp_path, access_control_enforced=True),
        resolver=_resolver(tmp_path, "people: []\n"),
    )

    def broken_warning(*args, **kwargs):
        raise RuntimeError("console failed")

    monkeypatch.setattr("mimir.dispatcher.log.warning", broken_warning)
    assert await disp.enqueue(AgentEvent(
        trigger="user_message", channel_id="discord-C1", content="hello",
        author="discord-1907002", author_id="1907002", source="discord",
    )) is False
    assert disp._queues == {}
    rows = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    assert [row["type"] for row in rows] == ["inbound_event_denied"]


class _FakePairingChannels:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, channel_id: str, text: str, attachment_paths=None, *, final=True):
        self.sent.append((channel_id, text))
        from mimir.bridges.base import SendResult
        return SendResult(sent=True)


@pytest.mark.asyncio
async def test_pairing_notifier_coalesces_operator_alerts_and_limits_dm_replies(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr("mimir.identities_populator.prepare_pairing_code_delivery", lambda *a, **k: True)
    channels = _FakePairingChannels()
    cfg = replace(
        _make_config(tmp_path),
        operator_alert_channel="dm-slack-OPS",
        pairing_operator_digest_delay_seconds=0.01,
        pairing_dm_auto_reply_enabled=True,
        pairing_dm_auto_reply_interval_seconds=0.0,
    )
    notifier = _PairingNotifier(cfg, channels)

    for i in range(5):
        await notifier.notify_operator(
            canonical=f"slack-U{i}",
            display=f"User {i}",
            platform="slack",
            channel_id=f"slack-C{i}",
            delivery="public_shared_channel",
        )
    await asyncio.sleep(0.05)

    operator_sends = [s for s in channels.sent if s[0] == "dm-slack-OPS"]
    assert len(operator_sends) == 1
    for i in range(5):
        assert f"mimir identities approve-pairing slack-U{i}" in operator_sends[0][1]

    await notifier.maybe_reply_dm(canonical="slack-U0", dm_channel_id="dm-slack-D0", code="ABCDEF23")
    await notifier.maybe_reply_dm(canonical="slack-U0", dm_channel_id="dm-slack-D0", code="ABCDEF23")
    await notifier.maybe_reply_dm(canonical="slack-U1", dm_channel_id="slack-C1", code="ABCDEF23")
    await notifier._dm_reply_queue.join()

    dm_sends = [s for s in channels.sent if s[0] == "dm-slack-D0"]
    public_sends = [s for s in channels.sent if s[0] == "slack-C1"]
    assert dm_sends == [
        ("dm-slack-D0", cfg.pairing_dm_auto_reply_text.replace("{code}", "ABCDEF23"))
    ]
    assert public_sends == []
    await notifier.maybe_reply_dm(canonical="slack-U0", dm_channel_id="dm-slack-D0", code="ABCDEF24")
    await notifier._dm_reply_queue.join()
    assert len([s for s in channels.sent if s[0] == "dm-slack-D0"]) == 2


@pytest.mark.asyncio
async def test_pairing_notifier_sends_pending_cap_alert_once(tmp_path: Path):
    channels = _FakePairingChannels()
    cfg = replace(
        _make_config(tmp_path),
        operator_alert_channel="dm-slack-OPS",
        pairing_pending_max=1,
    )
    notifier = _PairingNotifier(cfg, channels)

    await notifier.notify_pending_cap_reached(
        platform="slack",
        channel_id="slack-C1",
        delivery="public_shared_channel",
    )
    await notifier.notify_pending_cap_reached(
        platform="slack",
        channel_id="slack-C2",
        delivery="public_shared_channel",
    )

    assert len(channels.sent) == 1
    assert channels.sent[0][0] == "dm-slack-OPS"
    assert "Pairing pending cap reached" in channels.sent[0][1]
    assert "max=1" in channels.sent[0][1]
    assert "slack-C1" in channels.sent[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("template,expected", [
    ("Wait for approval", "Wait for approval\nPairing code: `ABCDEF23`"),
    ("Code {code} — keep it private", "Code ABCDEF23 — keep it private"),
])
async def test_pairing_dm_custom_template_always_includes_code(tmp_path, monkeypatch, template, expected):
    monkeypatch.setattr("mimir.identities_populator.prepare_pairing_code_delivery", lambda *a, **k: True)
    channels = _FakePairingChannels()
    notifier = _PairingNotifier(replace(_make_config(tmp_path),
        pairing_dm_auto_reply_enabled=True, pairing_dm_auto_reply_text=template,
        pairing_dm_auto_reply_interval_seconds=0), channels)
    try:
        await notifier.maybe_reply_dm(canonical="slack-U1", dm_channel_id="dm-slack-D1", code="ABCDEF23")
        await notifier._dm_reply_queue.join()
        assert channels.sent == [("dm-slack-D1", expected)]
    finally:
        await notifier.aclose()


@pytest.mark.asyncio
async def test_pairing_dm_reply_can_be_disabled(tmp_path):
    channels = _FakePairingChannels()
    notifier = _PairingNotifier(replace(_make_config(tmp_path),
        pairing_dm_auto_reply_enabled=False), channels)
    await notifier.maybe_reply_dm(canonical="slack-U1", dm_channel_id="dm-slack-D1", code="ABCDEF23")
    await notifier._dm_reply_queue.join()
    assert channels.sent == []
    await notifier.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,author,private_channel,shared_channel", [
    ("slack", "slack-U123", "dm-slack-D123", "dm-slack-G123"),
    ("discord", "discord-123", "dm-discord-123", "discord-123"),
    ("slack", "slack-U123", "dm-slack-D123", "slack-C123"),
])
async def test_pairing_notifier_refuses_shared_destination(
    tmp_path, platform, author, private_channel, shared_channel,
):
    from mimir.identities_populator import (
        prepare_pairing_code_delivery, request_pairing_with_code,
    )

    # A real deliverable code keeps the worker's hash gate from masking a
    # missing destination guard in maybe_reply_dm.
    status, code = request_pairing_with_code(
        tmp_path, author, platform, channel_id=private_channel, is_dm=True,
    )
    assert status == "changed"
    assert code is not None
    assert prepare_pairing_code_delivery(tmp_path, author, code) is True
    channels = _FakePairingChannels()
    notifier = _PairingNotifier(replace(_make_config(tmp_path),
        pairing_dm_auto_reply_enabled=True, pairing_dm_auto_reply_interval_seconds=0), channels)
    try:
        await notifier.maybe_reply_dm(
            canonical=author, dm_channel_id=shared_channel, code=code,
        )
        await notifier._dm_reply_queue.join()
        assert not channels.sent
        assert not notifier._dm_reply_sent
        # Refusal must not consume or invalidate the valid private code.
        assert prepare_pairing_code_delivery(tmp_path, author, code) is True
        await notifier.maybe_reply_dm(
            canonical=author, dm_channel_id=private_channel, code=code,
        )
        await notifier._dm_reply_queue.join()
        assert channels.sent == [(
            private_channel,
            notifier._config.pairing_dm_auto_reply_text.replace("{code}", code),
        )]
        assert notifier._dm_reply_sent
    finally:
        await notifier.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "result"])
async def test_failed_pairing_send_allows_immediate_reissue(tmp_path, caplog, failure):
    from mimir.identities_populator import request_pairing_with_code, approve_pairing_code
    from mimir.bridges.base import SendResult

    class Channels:
        calls = 0
        sent = []

        async def send(self, destination, text, *, final=True):
            self.calls += 1
            if self.calls == 1:
                if failure == "exception":
                    raise RuntimeError(text)  # plaintext in exception must not be logged
                return SendResult(sent=False, error=text)
            self.sent.append(text)
            return SendResult(sent=True)

    channels = Channels()
    notifier = _PairingNotifier(replace(_make_config(tmp_path),
        pairing_dm_auto_reply_enabled=True, pairing_dm_auto_reply_interval_seconds=0), channels)
    kwargs = dict(channel_id="dm-slack-D1", is_dm=True)
    _, first = request_pairing_with_code(tmp_path, "slack-U1", "slack", **kwargs)
    try:
        with caplog.at_level("DEBUG"):
            await notifier.maybe_reply_dm(canonical="slack-U1", dm_channel_id="dm-slack-D1", code=first)
            await notifier._dm_reply_queue.join()
        assert first not in caplog.text
        assert not notifier._dm_reply_sent
        _, second = request_pairing_with_code(tmp_path, "slack-U1", "slack", **kwargs)
        assert second and second != first
        await notifier.maybe_reply_dm(canonical="slack-U1", dm_channel_id="dm-slack-D1", code=second)
        await notifier._dm_reply_queue.join()
        assert len(channels.sent) == 1 and second in channels.sent[0]
        assert approve_pairing_code(tmp_path, second)
    finally:
        await notifier.aclose()


@pytest.mark.asyncio
async def test_queued_pairing_code_gets_full_ttl_and_superseded_code_is_not_sent(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from mimir import identities_populator as pop
    import yaml

    class Clock(datetime):
        current = datetime(2026, 10, 9, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(pop, "datetime", Clock)
    channels = _FakePairingChannels()
    notifier = _PairingNotifier(replace(_make_config(tmp_path),
        pairing_dm_auto_reply_enabled=True, pairing_dm_auto_reply_interval_seconds=0), channels)
    kwargs = dict(channel_id="dm-slack-D1", is_dm=True)
    _, first = pop.request_pairing_with_code(tmp_path, "slack-U1", "slack", **kwargs)
    Clock.current += timedelta(minutes=10)
    _, second = pop.request_pairing_with_code(tmp_path, "slack-U1", "slack", **kwargs)
    try:
        # Enqueue both before the worker runs, simulating a delayed backlog.
        await notifier.maybe_reply_dm(canonical="slack-U1", dm_channel_id="dm-slack-D1", code=first)
        await notifier.maybe_reply_dm(canonical="slack-U1", dm_channel_id="dm-slack-D1", code=second)
        Clock.current += timedelta(hours=2)
        await notifier._dm_reply_queue.join()
        assert len(channels.sent) == 1 and second in channels.sent[0][1]
        raw = (tmp_path / "state" / "identities.yaml").read_text()
        assert first not in raw and second not in raw
        pairing = yaml.safe_load(raw)["people"][0]["pairing"]
        assert datetime.fromisoformat(pairing["code_expires_at"]) == Clock.current + timedelta(hours=1)
        Clock.current += timedelta(minutes=59)
        assert pop.approve_pairing_code(tmp_path, second)
    finally:
        await notifier.aclose()


def _arm_authenticated_injection(disp, tmp_path):
    from mimir.access_control import create_auth_context
    from mimir.turn_event_bus import TurnEventEmitter

    disp._identity_resolver = _resolver(tmp_path, """
        people:
          - canonical: alice
            aliases: [slack-U1, discord-1]
            access: {roles: [admin]}
          - canonical: bob
            aliases: [slack-U2]
            access: {roles: [user]}
    """)
    auth = create_auth_context(
        AgentEvent(trigger="user_message", channel_id="c1", author="slack-U1"),
        disp._identity_resolver, enforce=True,
    )
    _mti.register_inflight("c1", emitter=TurnEventEmitter(
        None, turn_id="running-admin", channel_id="c1", auth_context=auth,
    ))
    return auth


@pytest.mark.asyncio
@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("author", ["slack-U2", "unknown", None, "discord-1"])
async def test_injection_cannot_borrow_running_principal(tmp_path, enforce, author):
    from mimir.access_control import create_auth_context

    disp = Dispatcher(replace(_inj_config(tmp_path, ("c",)), access_control_enforced=enforce))
    auth = _arm_authenticated_injection(disp, tmp_path)
    disp._in_flight.add("c1")
    recorded = []

    async def on_inject(event):
        recorded.append(event)

    disp.set_on_inject(on_inject)
    event = AgentEvent(
        trigger="user_message", channel_id="c1", author=author,
        content="execute an admin operation", source="web",
        extra={"authorized_principals": ["alice", "bob"], "principal": "alice"},
    )
    assert await disp.enqueue(event)
    if author == "discord-1":
        assert _mti._drain("c1") == [event]
        assert recorded == [event]
        assert "c1" not in disp._queues
    else:
        assert _mti._drain("c1") == []
        assert recorded == []
        queued = disp._queues["c1"].get_nowait()
        assert queued is event
        own_auth = create_auth_context(queued, disp._identity_resolver, enforce=True)
        assert "admin" not in own_auth.roles
        assert own_auth.canonical_principal != auth.canonical_principal
        disp._queues["c1"].task_done()
    assert _mti._REGISTRY["c1"].auth_context is auth
    assert auth.roles == ("admin",)


@pytest.mark.asyncio
@pytest.mark.parametrize("author", ["slack-U2", "unknown", None])
async def test_startup_principal_boundary_preserves_fifo(tmp_path, author):
    disp = Dispatcher(_inj_config(tmp_path, ("c",)))
    _arm_authenticated_injection(disp, tmp_path)
    queue = disp._queues["c1"] = _ChannelQueue(maxsize=10)
    events = [AgentEvent(trigger="user_message", channel_id="c1", author=a)
              for a in ("alice", author, "alice")]
    for event in events:
        queue.put_nowait(event)
    assert disp.drain_startup_user_messages("c1") == events[:1]
    assert [queue.get_nowait(), queue.get_nowait()] == events[1:]
    queue.task_done()
    queue.task_done()
    await asyncio.wait_for(queue.join(), timeout=1)


@pytest.mark.asyncio
async def test_enqueue_injects_when_in_flight_and_opted_in(tmp_path: Path):
    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    disp._in_flight.add("c1")          # simulate a running turn
    _arm_authenticated_injection(disp, tmp_path)
    accepted = await disp.enqueue(
        AgentEvent(trigger="user_message", channel_id="c1", content="folded", author="alice")
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
    _arm_authenticated_injection(disp, tmp_path)
    # Pre-seed a queued predecessor.
    q = asyncio.Queue(maxsize=disp._config.max_channel_queue)
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="earlier"))
    disp._queues["c1"] = q
    disp._high_water_logged["c1"] = False

    await disp.enqueue(
        AgentEvent(trigger="user_message", channel_id="c1", content="later", author="alice")
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
    _arm_authenticated_injection(disp, tmp_path)
    await disp.enqueue(
        AgentEvent(trigger="poller", channel_id="c1", content="tick", author="alice")
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
async def test_requeue_front_reports_partial_acceptance(tmp_path: Path):
    from dataclasses import replace

    seen = []

    async def runner(event):
        seen.append(event.content)

    disp = Dispatcher(replace(_inj_config(tmp_path, ("c",)), max_channel_queue=1), runner)
    events = [
        AgentEvent(trigger="user_message", channel_id="c1", content=content)
        for content in ("first", "second")
    ]
    assert disp.requeue_front(events) == 1
    await disp.drain()
    assert seen == ["second"]


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
    _arm_authenticated_injection(disp, tmp_path)
    accepted = await disp.enqueue(
        AgentEvent(trigger="user_message", channel_id="c1", content="folded", author="alice")
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
    _arm_authenticated_injection(disp, tmp_path)
    q = disp._queues["c1"] = _ChannelQueue(maxsize=disp._config.max_channel_queue)  # type: ignore[name-defined]
    disp._high_water_logged["c1"] = False
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="follow-1", author="alice"))
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="follow-2", author="alice"))

    drained = disp.drain_startup_user_messages("c1")

    assert [e.content for e in drained] == ["follow-1", "follow-2"]
    assert q.qsize() == 0
    await asyncio.wait_for(q.join(), timeout=1.0)


@pytest.mark.asyncio
async def test_drain_startup_user_messages_stops_at_non_user_boundary(tmp_path: Path):
    """A queued non-user event remains an ordering boundary: user messages behind
    it must not be startup-folded ahead of it."""
    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    _arm_authenticated_injection(disp, tmp_path)
    q = disp._queues["c1"] = _ChannelQueue(maxsize=disp._config.max_channel_queue)  # type: ignore[name-defined]
    disp._high_water_logged["c1"] = False
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="follow-1", author="alice"))
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
    _arm_authenticated_injection(disp, tmp_path)
    await disp.enqueue(AgentEvent(
        trigger="user_message", channel_id="c1", content="deferred topic",
        author="alice",
        extra={"force_new_turn": True},
    ))
    assert _mti._drain("c1") == []                  # not injected
    assert disp._queues["c1"].qsize() == 1          # queued as its own turn


@pytest.mark.asyncio
async def test_drain_startup_treats_force_new_turn_as_boundary(tmp_path: Path):
    """A force_new_turn event in the queue prefix is a hard boundary: startup-
    drain stops at it so the deferred message keeps its own turn."""
    disp = Dispatcher(_inj_config(tmp_path, ("c",)), None)
    _arm_authenticated_injection(disp, tmp_path)
    q = disp._queues["c1"] = _ChannelQueue(maxsize=disp._config.max_channel_queue)  # type: ignore[name-defined]
    disp._high_water_logged["c1"] = False
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="foldable", author="alice"))
    await q.put(AgentEvent(
        trigger="user_message", channel_id="c1", content="deferred",
        author="alice",
        extra={"force_new_turn": True},
    ))
    await q.put(AgentEvent(trigger="user_message", channel_id="c1", content="behind", author="alice"))

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

    # This outer bound is a hang guard, not a latency assertion. Cancellation
    # state below witnesses that the dispatcher's 0.2s timeout fired.
    await asyncio.wait_for(disp.drain(timeout=0.2), timeout=30)

    assert finished == []
    assert not disp._in_flight
    assert all(worker.done() for worker in disp._workers.values())


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


@pytest.mark.asyncio
async def test_http_ingress_with_client_source_does_not_skip_inbound_auth(tmp_path: Path):
    """chainlink #890: client-supplied source on HTTP ingress cannot bypass
    inbound allowlist. Even if client sets source="api", it must go through
    authorize_inbound."""
    from mimir.worklink.continuation import HTTP_EVENT_INGRESS_EXTRA_VALUE

    cfg = _make_config(tmp_path, access_control_enforced=True)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ran: list[str] = []

    async def runner(event: AgentEvent) -> None:
        ran.append(event.content)

    disp = Dispatcher(cfg, runner, resolver=resolver)
    accepted = await disp.enqueue(
        AgentEvent(
            trigger="user_message",
            channel_id="api-C1",
            content="client forged source",
            author="unknown-author",
            source="api",
            extra={'_mimir_event_ingress': HTTP_EVENT_INGRESS_EXTRA_VALUE},
        )
    )

    assert accepted is False
    await disp.drain()
    assert ran == []

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    denied = [row for row in rows if row.get("type") == "inbound_event_denied"]
    assert len(denied) == 1
    assert denied[0]["source"] == "api"
    assert denied[0]["reason"] == "unknown_author"


@pytest.mark.asyncio
async def test_http_ingress_non_user_trigger_does_not_skip_inbound_auth(tmp_path: Path):
    from mimir.worklink.continuation import HTTP_EVENT_INGRESS_EXTRA_VALUE

    cfg = _make_config(tmp_path, access_control_enforced=True)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ran: list[str] = []

    async def runner(event: AgentEvent) -> None:
        ran.append(event.content)

    disp = Dispatcher(cfg, runner, resolver=resolver)
    accepted = await disp.enqueue(
        AgentEvent(
            trigger="react",
            channel_id="api-C1",
            content="client forged trigger",
            author="unknown-author",
            source="api",
            extra={"_mimir_event_ingress": HTTP_EVENT_INGRESS_EXTRA_VALUE},
        )
    )

    assert accepted is False
    await disp.drain()
    assert ran == []
    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    denied = [row for row in rows if row.get("type") == "inbound_event_denied"]
    assert len(denied) == 1
    assert denied[0]["trigger"] == "react"
    assert denied[0]["reason"] == "unknown_author"


@pytest.mark.asyncio
async def test_server_owned_source_bypasses_with_audit(tmp_path: Path):
    """chainlink #890: server-owned source (not from HTTP ingress) still
    bypasses authorize_inbound and emits an audit event."""
    cfg = _make_config(tmp_path, access_control_enforced=True)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ran: list[str] = []

    async def runner(event: AgentEvent) -> None:
        ran.append(event.content)

    disp = Dispatcher(cfg, runner, resolver=resolver)
    assert await disp.enqueue(
        AgentEvent(
            trigger="user_message",
            channel_id="api-C1",
            content="server-owned api",
            author="api-unknown",
            source="api",
        )
    )

    await disp.drain()
    assert ran == ["server-owned api"]

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    allowed = [row for row in rows if row.get("type") == "inbound_event_allowed"]
    assert len(allowed) == 1
    assert allowed[0]["source"] == "api"
    assert allowed[0]["reason"] == "trusted_internal_source"
