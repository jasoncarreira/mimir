from __future__ import annotations

import asyncio
import io
import subprocess
import sys
import textwrap
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from mimir.acp.host import HostLifecycle, LifecycleTimeouts, PRODUCTION_TIMEOUTS


ROOT = Path(__file__).resolve().parents[1]


class _FakeTimer:
    def __init__(self, interval: float, callback: Callable[[], None]) -> None:
        self.interval = interval
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.joined = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def join(self) -> None:
        self.joined = True


class _FakeStreams:
    def __init__(self, events: list[str] | None = None) -> None:
        self.request_reader = asyncio.StreamReader()
        self.response_writer = object()
        self.events = events if events is not None else []
        self.intake_stopped = False
        self.writer_failed = False
        self.drain_timeout: float | None = None
        self.close_timeout: float | None = None
        self.drain_result = True
        self.close_result = True
        self._helpers: list[asyncio.Task[None]] = []

    def stop_request_intake(self) -> None:
        self.events.append("protocol-intake-stopped")
        self.intake_stopped = True
        self.request_reader.feed_eof()

    async def drain_response_writer(self, timeout: float = 2.0) -> bool:
        self.events.append("writer-drained")
        self.drain_timeout = timeout
        task = asyncio.create_task(asyncio.sleep(0), name="fake-writer-drain")
        self._helpers.append(task)
        await task
        return self.drain_result

    async def close_response_writer(self, timeout: float = 1.0) -> bool:
        self.events.append("writer-closed")
        self.close_timeout = timeout
        task = asyncio.create_task(asyncio.sleep(0), name="fake-writer-close")
        self._helpers.append(task)
        await task
        return self.close_result

    def writer_helper_tasks(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(self._helpers)


class _FakeComposition:
    def __init__(
        self,
        runtime_close: Callable[[], Awaitable[None]],
        events: list[str] | None = None,
    ) -> None:
        self.bundle = object()
        self.events = events if events is not None else []
        self._runtime_close = runtime_close
        self._driver: asyncio.Task[None] | None = None
        self.adapter_tasks: set[asyncio.Task[Any]] = set()
        self.adapter_calls: list[tuple[float, asyncio.Future[Any]]] = []
        self.adapter_started = asyncio.Event()
        self.adapter_closed = True

    def start_runtime_close(self) -> asyncio.Task[None]:
        if self._driver is None:
            self.events.append("runtime-started")
            self._driver = asyncio.create_task(
                self._runtime_close(),
                name="fake-runtime-close-driver",
            )
        return self._driver

    def explicit_adapter_tasks(self) -> frozenset[asyncio.Task[Any]]:
        return frozenset(self.adapter_tasks)

    async def close_adapters(
        self,
        timeout: float,
        *,
        runtime_audit_task: asyncio.Future[Any],
    ) -> bool:
        assert runtime_audit_task.done()
        assert not runtime_audit_task.cancelled()
        assert runtime_audit_task.exception() is None
        self.adapter_calls.append((timeout, runtime_audit_task))
        self.events.append("adapters-closed")
        self.adapter_started.set()
        return self.adapter_closed


def _timer_factory(timers: list[_FakeTimer]) -> Callable[[float, Callable[[], None]], _FakeTimer]:
    def create(interval: float, callback: Callable[[], None]) -> _FakeTimer:
        timer = _FakeTimer(interval, callback)
        timers.append(timer)
        return timer

    return create


async def _completed() -> None:
    return None


def _lifecycle(
    *,
    streams: _FakeStreams | None = None,
    composition: _FakeComposition | None = None,
    protocol_runner: Callable[..., Awaitable[None]] | None = None,
    timeouts: LifecycleTimeouts | None = None,
    timers: list[_FakeTimer] | None = None,
    frame_file: io.BytesIO | None = None,
) -> HostLifecycle:
    selected_streams = streams or _FakeStreams()
    selected_composition = composition or _FakeComposition(_completed)
    selected_timers = timers if timers is not None else []

    async def streams_factory(frame: Any) -> Any:
        return selected_streams

    async def composition_factory() -> Any:
        return selected_composition

    async def default_protocol_runner(*args: Any, **kwargs: Any) -> None:
        return None

    return HostLifecycle(
        frame_file or io.BytesIO(),
        timeouts=timeouts or LifecycleTimeouts(
            protocol_grace=0.05,
            protocol_cancel=0.05,
            writer_drain=0.02,
            writer_close=0.01,
            runtime_driver=0.05,
            runtime_audit=0.05,
            adapter_cleanup=0.05,
            audit_rescan=0.001,
            watchdog=1.0,
        ),
        timer_factory=_timer_factory(selected_timers),
        fail_stop=lambda status: None,
        streams_factory=streams_factory,
        composition_factory=composition_factory,
        agent_factory=lambda bundle: object(),
        protocol_runner=protocol_runner or default_protocol_runner,
    )


@pytest.mark.asyncio
async def test_eof_shutdown_stage_order_and_descriptor_ownership() -> None:
    events: list[str] = []
    streams = _FakeStreams(events)
    composition = _FakeComposition(_completed, events)
    timers: list[_FakeTimer] = []
    frame_file = io.BytesIO()

    async def protocol_runner(*args: Any, **kwargs: Any) -> None:
        events.append("protocol-terminal")

    lifecycle = _lifecycle(
        streams=streams,
        composition=composition,
        protocol_runner=protocol_runner,
        timers=timers,
        frame_file=frame_file,
    )

    assert await lifecycle.run() == 0
    assert events.index("protocol-terminal") < events.index("writer-drained")
    assert events.index("writer-closed") < events.index("runtime-started")
    assert events.index("runtime-started") < events.index("adapters-closed")
    assert streams.intake_stopped
    assert frame_file.closed is False
    assert len(composition.adapter_calls) == 1
    assert composition.adapter_calls[0][1] in lifecycle.host_stage_tasks
    assert timers[0].daemon is True
    assert timers[0].cancelled is True
    assert timers[0].joined is True


@pytest.mark.asyncio
async def test_peer_disconnect_returns_clean_only_after_cleanup_terminal() -> None:
    events: list[str] = []
    streams = _FakeStreams(events)
    composition = _FakeComposition(_completed, events)

    async def protocol_runner(*args: Any, **kwargs: Any) -> None:
        events.append("peer-disconnected")
        raise BrokenPipeError

    lifecycle = _lifecycle(
        streams=streams,
        composition=composition,
        protocol_runner=protocol_runner,
    )

    assert await lifecycle.run() == 0
    assert events.index("peer-disconnected") < events.index("adapters-closed")
    assert len(composition.adapter_calls) == 1


@pytest.mark.asyncio
async def test_unexpected_protocol_failure_cleans_up_and_returns_failure() -> None:
    events: list[str] = []
    streams = _FakeStreams(events)
    composition = _FakeComposition(_completed, events)

    async def protocol_runner(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("private detail")

    lifecycle = _lifecycle(
        streams=streams,
        composition=composition,
        protocol_runner=protocol_runner,
    )

    assert await lifecycle.run() == 1
    assert "adapters-closed" in events


@pytest.mark.asyncio
@pytest.mark.parametrize("signum", ["SIGINT", "SIGTERM"])
async def test_signal_stops_protocol_and_completes_cleanup(signum: str) -> None:
    protocol_stopped = asyncio.Event()

    async def protocol_runner(*args: Any, **kwargs: Any) -> None:
        await protocol_stopped.wait()

    streams = _FakeStreams()
    lifecycle = _lifecycle(streams=streams, protocol_runner=protocol_runner)
    run_task = asyncio.create_task(lifecycle.run())
    while lifecycle._protocol_task is None:
        await asyncio.sleep(0)
    lifecycle.stop_protocol_intake()
    protocol_stopped.set()

    assert await run_task == 0
    assert streams.intake_stopped
    assert signum in {"SIGINT", "SIGTERM"}


@pytest.mark.asyncio
async def test_protocol_terminal_uses_five_plus_five_second_policy_and_cancels_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _lifecycle(timeouts=PRODUCTION_TIMEOUTS)
    allow_terminal = asyncio.Event()
    cancel_count = 0

    async def resistant_protocol() -> None:
        nonlocal cancel_count
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancel_count += 1
            await allow_terminal.wait()

    task = asyncio.create_task(resistant_protocol(), name="acp-run-agent")
    lifecycle._protocol_task = task
    waits: list[float | None] = []
    real_wait = asyncio.wait

    async def fake_wait(
        futures: Any,
        *,
        timeout: float | None = None,
        return_when: Any = asyncio.ALL_COMPLETED,
    ) -> Any:
        waits.append(timeout)
        if len(waits) < 3:
            await asyncio.sleep(0)
            return set(), set(futures)
        allow_terminal.set()
        return await real_wait(futures, return_when=return_when)

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    await lifecycle.await_protocol_terminal()

    assert waits[:3] == [5.0, 5.0, None]
    assert cancel_count == 1
    assert task.done()


@pytest.mark.asyncio
async def test_writer_stages_use_two_and_one_second_bounds() -> None:
    streams = _FakeStreams()
    lifecycle = _lifecycle(streams=streams, timeouts=PRODUCTION_TIMEOUTS)
    lifecycle._streams = streams

    await lifecycle.close_protocol_writer()

    assert streams.drain_timeout == 2.0
    assert streams.close_timeout == 1.0


@pytest.mark.asyncio
async def test_runtime_driver_timeout_does_not_advance_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_close = asyncio.Event()
    close_started = asyncio.Event()

    async def runtime_close() -> None:
        close_started.set()
        await allow_close.wait()

    composition = _FakeComposition(runtime_close)
    lifecycle = _lifecycle(composition=composition, timeouts=PRODUCTION_TIMEOUTS)
    lifecycle._composition = composition
    waits: list[float | None] = []
    real_wait = asyncio.wait

    async def fake_wait(
        futures: Any,
        *,
        timeout: float | None = None,
        return_when: Any = asyncio.ALL_COMPLETED,
    ) -> Any:
        waits.append(timeout)
        if len(waits) == 1:
            await close_started.wait()
            return set(), set(futures)
        assert not composition.adapter_started.is_set()
        allow_close.set()
        return await real_wait(futures, return_when=return_when)

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    await lifecycle.await_runtime_driver_terminal()

    assert waits == [36.0, None]
    assert composition._driver is not None
    assert composition._driver.done()
    assert composition._driver.cancelled() is False
    assert lifecycle._failed is True
    assert not composition.adapter_started.is_set()


async def _run_audit(
    lifecycle: HostLifecycle,
) -> asyncio.Task[None]:
    task = asyncio.create_task(
        lifecycle.audit_runtime_tasks_terminal(),
        name="acp-runtime-task-audit",
    )
    lifecycle._runtime_audit_task = task
    lifecycle._host_stage_tasks.add(task)
    await task
    return task


@pytest.mark.asyncio
async def test_runtime_task_audit_catches_dropped_background_task() -> None:
    lifecycle = _lifecycle()
    lifecycle._pre_composition_tasks = frozenset(asyncio.all_tasks())
    cancelled = asyncio.Event()

    async def dropped() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(dropped(), name="dropped-runtime-background")
    await _run_audit(lifecycle)

    assert cancelled.is_set()
    assert task in lifecycle.audited_runtime_tasks
    assert task in lifecycle.audit_cancel_requested
    assert task.done()


@pytest.mark.asyncio
async def test_runtime_task_audit_catches_timed_out_close_resource_task() -> None:
    lifecycle = _lifecycle()
    lifecycle._pre_composition_tasks = frozenset(asyncio.all_tasks())
    cancelled = asyncio.Event()

    async def resource_close() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    resource_task = asyncio.create_task(
        resource_close(),
        name="agent-runtime-close-saga",
    )
    await _run_audit(lifecycle)

    assert cancelled.is_set()
    assert resource_task in lifecycle.audited_runtime_tasks
    assert resource_task.done()


@pytest.mark.asyncio
async def test_cancellation_resistant_runtime_child_blocks_adapters_and_descriptor() -> None:
    lifecycle = _lifecycle()
    lifecycle._pre_composition_tasks = frozenset(asyncio.all_tasks())
    allow_terminal = asyncio.Event()
    cancellation_seen = asyncio.Event()
    frame_file = lifecycle._frame_file

    async def resistant() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_terminal.wait()

    child = asyncio.create_task(resistant(), name="resistant-runtime-child")
    audit = asyncio.create_task(
        lifecycle.audit_runtime_tasks_terminal(),
        name="acp-runtime-task-audit",
    )
    lifecycle._runtime_audit_task = audit
    lifecycle._host_stage_tasks.add(audit)
    await cancellation_seen.wait()

    assert not audit.done()
    assert child in lifecycle.audited_runtime_tasks
    assert frame_file.closed is False
    assert lifecycle._composition is None

    allow_terminal.set()
    await audit
    assert child.done()
    assert frame_file.closed is False


@pytest.mark.asyncio
async def test_pre_composition_and_explicit_adapter_tasks_are_excluded_from_runtime_audit() -> None:
    adapter_allow_terminal = asyncio.Event()
    baseline_allow_terminal = asyncio.Event()
    baseline = asyncio.create_task(
        baseline_allow_terminal.wait(),
        name="pre-composition-task",
    )
    lifecycle = _lifecycle()
    lifecycle._pre_composition_tasks = frozenset({*asyncio.all_tasks(), baseline})
    adapter = asyncio.create_task(
        adapter_allow_terminal.wait(),
        name="explicit-adapter-task",
    )
    composition = _FakeComposition(_completed)
    composition.adapter_tasks.add(adapter)
    lifecycle._composition = composition

    await _run_audit(lifecycle)

    assert baseline not in lifecycle.audited_runtime_tasks
    assert adapter not in lifecycle.audited_runtime_tasks
    assert not baseline.done()
    assert not adapter.done()
    baseline_allow_terminal.set()
    adapter_allow_terminal.set()
    await asyncio.gather(baseline, adapter)


@pytest.mark.asyncio
async def test_runtime_audit_rescans_for_children_spawned_during_audit() -> None:
    lifecycle = _lifecycle()
    lifecycle._pre_composition_tasks = frozenset(asyncio.all_tasks())
    spawned: list[asyncio.Task[None]] = []
    child_cancelled = asyncio.Event()

    async def child() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            child_cancelled.set()
            raise

    async def parent() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            spawned.append(
                asyncio.create_task(child(), name="runtime-audit-spawned-child")
            )
            raise

    parent_task = asyncio.create_task(parent(), name="runtime-audit-parent")
    await _run_audit(lifecycle)

    assert parent_task in lifecycle.audited_runtime_tasks
    assert len(spawned) == 1
    assert spawned[0] in lifecycle.audited_runtime_tasks
    assert child_cancelled.is_set()
    assert spawned[0].done()


@pytest.mark.asyncio
async def test_runtime_audit_requires_two_empty_scans_and_retrieves_failures() -> None:
    lifecycle = _lifecycle()
    lifecycle._pre_composition_tasks = frozenset(asyncio.all_tasks())

    async def fail_after_cancel() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise ValueError("private failure")

    task = asyncio.create_task(fail_after_cancel(), name="failing-runtime-child")
    await _run_audit(lifecycle)

    assert task in lifecycle._retrieved_tasks
    assert lifecycle._failed is True


@pytest.mark.asyncio
async def test_close_adapters_receives_actual_successful_audit_task() -> None:
    composition = _FakeComposition(_completed)
    lifecycle = _lifecycle(composition=composition)
    lifecycle._composition = composition
    lifecycle._pre_composition_tasks = frozenset(asyncio.all_tasks())
    audit_task = await _run_audit(lifecycle)

    await lifecycle.close_adapters_terminal()

    assert composition.adapter_calls == [
        (lifecycle._timeouts.adapter_cleanup, audit_task)
    ]


def test_maximum_clean_stage_budget_completes_before_65_second_watchdog() -> None:
    timeouts = PRODUCTION_TIMEOUTS
    clean_budget = (
        timeouts.protocol_grace
        + timeouts.protocol_cancel
        + timeouts.writer_drain
        + timeouts.writer_close
        + timeouts.runtime_driver
        + timeouts.runtime_audit
        + timeouts.adapter_cleanup
    )
    clock_value = 100.0
    timers: list[_FakeTimer] = []
    lifecycle = HostLifecycle(
        io.BytesIO(),
        timeouts=timeouts,
        clock=lambda: clock_value,
        timer_factory=_timer_factory(timers),
        fail_stop=lambda status: None,
    )
    lifecycle.stop_protocol_intake()

    assert clean_budget == 59.0
    assert timeouts.watchdog == 65.0
    assert timeouts.watchdog - clean_budget == 6.0
    assert lifecycle._watchdog_deadline == 165.0
    assert timers[0].interval == 65.0
    lifecycle._stop_watchdog()


def test_cancellation_resistant_runtime_child_hard_fail_stops_subprocess(
    tmp_path: Path,
) -> None:
    adapter_marker = tmp_path / "adapter"
    descriptor_marker = tmp_path / "descriptor"
    script = textwrap.dedent(
        f"""
        import asyncio
        import io
        from mimir.acp.host import HostLifecycle, LifecycleTimeouts

        adapter_marker = {str(adapter_marker)!r}
        descriptor_marker = {str(descriptor_marker)!r}

        class Streams:
            def __init__(self):
                self.request_reader = asyncio.StreamReader()
                self.response_writer = object()
            def stop_request_intake(self):
                self.request_reader.feed_eof()
            async def drain_response_writer(self, timeout=2.0):
                return True
            async def close_response_writer(self, timeout=1.0):
                return True
            def writer_helper_tasks(self):
                return ()

        class Composition:
            bundle = object()
            def __init__(self):
                self.driver = None
            def start_runtime_close(self):
                if self.driver is None:
                    self.driver = asyncio.create_task(asyncio.sleep(0))
                return self.driver
            def explicit_adapter_tasks(self):
                return frozenset()
            async def close_adapters(self, timeout, *, runtime_audit_task):
                open(adapter_marker, "w").close()
                return True

        async def main():
            streams = Streams()
            composition = Composition()
            async def streams_factory(frame):
                return streams
            async def composition_factory():
                async def resistant():
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        await asyncio.Future()
                asyncio.create_task(resistant(), name="subprocess-resistant-child")
                return composition
            async def protocol_runner(*args, **kwargs):
                return None
            lifecycle = HostLifecycle(
                io.BytesIO(),
                timeouts=LifecycleTimeouts(
                    protocol_grace=0.01,
                    protocol_cancel=0.01,
                    writer_drain=0.01,
                    writer_close=0.01,
                    runtime_driver=0.01,
                    runtime_audit=0.02,
                    adapter_cleanup=0.01,
                    audit_rescan=0.005,
                    watchdog=0.2,
                ),
                streams_factory=streams_factory,
                composition_factory=composition_factory,
                agent_factory=lambda bundle: object(),
                protocol_runner=protocol_runner,
            )
            await lifecycle.run()
            open(descriptor_marker, "w").close()

        asyncio.run(main())
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 1
    assert not adapter_marker.exists()
    assert not descriptor_marker.exists()


def test_lifecycle_has_no_environment_or_authentication_timeout_controls() -> None:
    source = Path(HostLifecycle.__module__.replace(".", "/") + ".py")
    assert "MIMIR_ACP" not in (ROOT / source).read_text()
    assert "auth" not in HostLifecycle.__init__.__annotations__
