"""Dispatcher concurrency & ordering (SPEC §4.5)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from mimir.config import Config
from mimir.dispatcher import Dispatcher
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
