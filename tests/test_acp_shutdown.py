from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mimir.acp.agent import ConnectionState, MimirAcpAgent
from mimir.acp.host import (
    HostLifecycle,
    LifecycleTimeouts,
    PRODUCTION_TIMEOUTS,
    _FrameDelivery,
)


ROOT = Path(__file__).resolve().parents[1]


def _wait_for_process_marker(
    marker: Path,
    process: subprocess.Popen[bytes],
    timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        status = process.poll()
        if status is not None:
            raise AssertionError(f"process exited before readiness with status {status}")
        time.sleep(0.01)
    raise AssertionError("process readiness timed out")


def _kill_and_reap(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    if process.poll() is None:
        process.kill()
    return process.communicate(timeout=5)


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
    task_waiter: Callable[..., Awaitable[Any]] = asyncio.wait,
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
        task_waiter=task_waiter,
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
async def test_frame_delivery_preserves_complete_order_without_owning_descriptor() -> None:
    frame_file = io.BytesIO()
    composition = _FakeComposition(_completed)

    class Writer:
        def __init__(self, frame: Any) -> None:
            self.frame = frame

        def write(self, data: bytes) -> int:
            return self.frame.write(data)

    async def streams_factory(frame: Any) -> Any:
        streams = _FakeStreams()
        streams.response_writer = Writer(frame)
        return streams

    async def composition_factory() -> Any:
        return composition

    async def protocol_runner(
        agent: Any,
        *,
        response_writer: Writer,
        **kwargs: Any,
    ) -> None:
        response_writer.write(b"first\n")
        response_writer.write(b"second\n")
        response_writer.write(b"third\n")

    lifecycle = HostLifecycle(
        frame_file,
        timeouts=LifecycleTimeouts(watchdog=1.0, audit_rescan=0.001),
        timer_factory=_timer_factory([]),
        fail_stop=lambda status: None,
        streams_factory=streams_factory,
        composition_factory=composition_factory,
        agent_factory=lambda bundle: object(),
        protocol_runner=protocol_runner,
    )

    assert await lifecycle.run() == 0
    assert frame_file.getvalue() == b"first\nsecond\nthird\n"
    assert frame_file.closed is False
    assert lifecycle._delivery is not None
    assert lifecycle._delivery.terminal


@pytest.mark.asyncio
async def test_frame_delivery_reserves_exact_capacity_and_overflow_fails_closed() -> None:
    class BlockingFrame:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.data = bytearray()

        def write(self, data: bytes | memoryview) -> int:
            self.started.set()
            self.release.wait()
            self.data.extend(data)
            return len(data)

        def flush(self) -> None:
            return None

    frame = BlockingFrame()
    failed = asyncio.Event()
    delivery = _FrameDelivery(frame, 10, lambda _error: failed.set())
    assert delivery.write(b"123456") == 6
    while not frame.started.is_set():
        await asyncio.sleep(0)
    assert delivery.write(b"7890") == 4

    with pytest.raises(BufferError):
        delivery.write(b"x")

    await failed.wait()
    assert delivery.reserved_bytes == 10
    assert delivery.peak_reserved_bytes == 10
    frame.release.set()
    with pytest.raises(BufferError):
        await delivery.wait_terminal()
    delivery.join()
    assert bytes(frame.data) == b"1234567890"
    assert delivery.reserved_bytes == 0
    assert delivery.terminal


@pytest.mark.asyncio
async def test_frame_delivery_sustained_ingress_remains_bounded_and_ordered() -> None:
    frame = io.BytesIO()
    failures: list[None] = []
    delivery = _FrameDelivery(frame, 16, lambda _error: failures.append(None))
    frames = [f"{index:03d}\n".encode() for index in range(200)]

    for payload in frames:
        while delivery.reserved_bytes > 11:
            await asyncio.sleep(0)
        delivery.write(payload)

    delivery.finish()
    await delivery.wait_terminal()
    delivery.join()

    assert frame.getvalue() == b"".join(frames)
    assert delivery.peak_reserved_bytes <= 16
    assert delivery.reserved_bytes == 0
    assert failures == []


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
async def test_frame_delivery_write_peer_disconnect_exits_clean() -> None:
    """A peer that hangs up mid-write is a clean disconnect, not a host failure.

    The existing peer-disconnect coverage raises from the protocol runner, a path
    that was already classified correctly. This drives the error from the physical
    frame-delivery sink instead, which reaches ``_frame_delivery_failed``. That
    callback used to mark failure unconditionally, and because the flag is sticky it
    outlived the correct classification ``_retrieve_delivery_task`` performs on the
    same exception — so a clean disconnect exited 1 after a complete teardown.
    """
    events: list[str] = []
    lifecycle = _lifecycle(composition=_FakeComposition(_completed, events))

    lifecycle._frame_delivery_failed(BrokenPipeError("peer hung up"))

    assert await lifecycle.run() == 0
    assert "adapters-closed" in events


@pytest.mark.asyncio
async def test_frame_delivery_flush_peer_disconnect_exits_clean() -> None:
    """Same contract for the flush call site, which is separate from write."""
    events: list[str] = []
    lifecycle = _lifecycle(composition=_FakeComposition(_completed, events))

    lifecycle._frame_delivery_failed(ConnectionResetError("peer reset"))

    assert await lifecycle.run() == 0
    assert "adapters-closed" in events


@pytest.mark.asyncio
async def test_frame_delivery_real_write_failure_still_fails() -> None:
    """A genuine sink failure must remain a failure — the fix narrows, not removes."""
    events: list[str] = []
    lifecycle = _lifecycle(composition=_FakeComposition(_completed, events))

    lifecycle._frame_delivery_failed(OSError("disk went away"))

    assert await lifecycle.run() == 1
    assert "adapters-closed" in events


@pytest.mark.asyncio
async def test_frame_delivery_sink_error_reaches_the_callback() -> None:
    """The exception must cross the thread/loop boundary at all.

    The callback previously took no argument, so the delivery thread discarded which
    exception occurred before any classification could run. This pins that the
    failure itself is delivered.
    """
    seen: list[BaseException] = []

    class _BrokenSink(io.BytesIO):
        def write(self, data: Any) -> int:  # type: ignore[override]
            raise BrokenPipeError("peer hung up")

    delivery = _FrameDelivery(_BrokenSink(), 64, seen.append)
    delivery.write(b'{"jsonrpc":"2.0"}\n')
    delivery.finish()
    with pytest.raises(BrokenPipeError):
        await delivery.wait_terminal()

    assert seen and isinstance(seen[0], BrokenPipeError)


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
async def test_composition_startup_failure_still_closes_writer_and_audits_tasks() -> None:
    streams = _FakeStreams()
    timers: list[_FakeTimer] = []
    dropped_cancelled = asyncio.Event()
    dropped: asyncio.Task[None] | None = None

    async def streams_factory(frame: Any) -> Any:
        return streams

    async def composition_factory() -> Any:
        nonlocal dropped

        async def background() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                dropped_cancelled.set()
                raise

        dropped = asyncio.create_task(background(), name="failed-startup-background")
        raise RuntimeError("startup detail")

    lifecycle = HostLifecycle(
        io.BytesIO(),
        timeouts=LifecycleTimeouts(watchdog=1.0, audit_rescan=0.001),
        timer_factory=_timer_factory(timers),
        fail_stop=lambda status: None,
        streams_factory=streams_factory,
        composition_factory=composition_factory,
    )

    assert await lifecycle.run() == 1
    assert streams.drain_timeout is not None
    assert streams.close_timeout is not None
    assert dropped is not None
    assert dropped in lifecycle.audited_runtime_tasks
    assert dropped_cancelled.is_set()
    assert dropped.done()
    assert lifecycle._stage_states[next(
        stage for stage in lifecycle._stage_states if stage.name == "RUNTIME"
    )].name == "NOT_STARTED"
    assert timers[0].cancelled
    assert timers[0].joined


@pytest.mark.asyncio
async def test_agent_startup_failure_runs_runtime_audit_and_adapters() -> None:
    events: list[str] = []
    streams = _FakeStreams(events)
    composition = _FakeComposition(_completed, events)
    lifecycle = _lifecycle(streams=streams, composition=composition)
    lifecycle._agent_factory = lambda bundle: (_ for _ in ()).throw(
        RuntimeError("agent startup detail")
    )

    assert await lifecycle.run() == 1
    assert events.index("writer-closed") < events.index("runtime-started")
    assert events.index("runtime-started") < events.index("adapters-closed")
    assert composition.adapter_calls[0][1] is lifecycle._runtime_audit_task


@pytest.mark.asyncio
async def test_writer_stage_failures_do_not_skip_runtime_audit_or_adapters() -> None:
    events: list[str] = []

    class FailingStreams(_FakeStreams):
        async def drain_response_writer(self, timeout: float = 2.0) -> bool:
            self.events.append("writer-drain-failed")
            raise RuntimeError("drain detail")

        async def close_response_writer(self, timeout: float = 1.0) -> bool:
            self.events.append("writer-close-failed")
            raise RuntimeError("close detail")

    streams = FailingStreams(events)
    composition = _FakeComposition(_completed, events)
    lifecycle = _lifecycle(streams=streams, composition=composition)

    assert await lifecycle.run() == 1
    assert events.index("writer-close-failed") < events.index("runtime-started")
    assert events.index("runtime-started") < events.index("adapters-closed")
    assert lifecycle._runtime_audit_task is composition.adapter_calls[0][1]


@pytest.mark.asyncio
async def test_runtime_driver_failure_still_runs_audit_and_adapters() -> None:
    events: list[str] = []

    async def failing_runtime_close() -> None:
        events.append("runtime-failed")
        raise RuntimeError("runtime detail")

    composition = _FakeComposition(failing_runtime_close, events)
    lifecycle = _lifecycle(
        streams=_FakeStreams(events),
        composition=composition,
    )

    assert await lifecycle.run() == 1
    assert events.index("runtime-failed") < events.index("adapters-closed")
    assert lifecycle._runtime_driver is not None
    assert lifecycle._runtime_driver.done()
    assert lifecycle._runtime_audit_task is composition.adapter_calls[0][1]


@pytest.mark.asyncio
async def test_adapter_failure_is_terminal_before_descriptor_can_return() -> None:
    composition = _FakeComposition(_completed)
    adapter_attempted = asyncio.Event()

    async def failing_adapters(
        timeout: float,
        *,
        runtime_audit_task: asyncio.Future[Any],
    ) -> bool:
        adapter_attempted.set()
        raise RuntimeError("adapter detail")

    composition.close_adapters = failing_adapters
    frame_file = io.BytesIO()
    lifecycle = _lifecycle(composition=composition, frame_file=frame_file)

    assert await lifecycle.run() == 1
    assert adapter_attempted.is_set()
    assert frame_file.closed is False
    assert lifecycle._stage_states[next(
        stage for stage in lifecycle._stage_states if stage.name == "ADAPTERS"
    )].name == "TERMINAL"


@pytest.mark.asyncio
async def test_protocol_terminal_uses_five_plus_five_second_policy_and_cancels_once() -> None:
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

    lifecycle = _lifecycle(
        timeouts=PRODUCTION_TIMEOUTS,
        task_waiter=fake_wait,
    )
    lifecycle._protocol_task = task
    await lifecycle.await_protocol_terminal()

    assert waits[:3] == [5.0, 5.0, None]
    assert cancel_count == 1
    assert task.done()


@pytest.mark.asyncio
async def test_writer_stages_use_two_and_one_second_bounds() -> None:
    streams = _FakeStreams()
    lifecycle = _lifecycle(streams=streams, timeouts=PRODUCTION_TIMEOUTS)

    assert await lifecycle.run() == 0

    assert streams.drain_timeout == 2.0
    assert streams.close_timeout == 1.0


@pytest.mark.asyncio
async def test_runtime_driver_timeout_does_not_advance_cleanup() -> None:
    allow_close = asyncio.Event()
    close_started = asyncio.Event()

    async def runtime_close() -> None:
        close_started.set()
        await allow_close.wait()

    composition = _FakeComposition(runtime_close)
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

    lifecycle = _lifecycle(
        composition=composition,
        timeouts=PRODUCTION_TIMEOUTS,
        task_waiter=fake_wait,
    )
    lifecycle._composition = composition
    await lifecycle.await_runtime_driver_terminal()

    assert waits == [36.0, None]
    assert composition._driver is not None
    assert composition._driver.done()
    assert composition._driver.cancelled() is False
    assert lifecycle._failed is True
    assert not composition.adapter_started.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["start-failure", "cancelled"])
async def test_runtime_driver_without_terminal_proof_cannot_unlock_cleanup(
    mode: str,
) -> None:
    class UnprovenComposition(_FakeComposition):
        def start_runtime_close(self) -> asyncio.Task[None]:
            if mode == "start-failure":
                raise RuntimeError("start detail")
            task = asyncio.create_task(asyncio.sleep(60), name="cancelled-runtime")
            task.cancel()
            return task

    composition = UnprovenComposition(_completed)
    lifecycle = _lifecycle(composition=composition)
    lifecycle._composition = composition

    await lifecycle.await_runtime_driver_terminal()

    assert not lifecycle._runtime_driver_proven_terminal()
    assert composition.adapter_calls == []
    assert lifecycle._runtime_audit_task is None


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


@pytest.mark.asyncio
async def test_maximum_clean_stage_budget_completes_before_65_second_watchdog() -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

        def advance(self, amount: float) -> None:
            self.value += amount
            checkpoints.append(self.value)

    clock = Clock()
    checkpoints: list[float] = []
    timers: list[_FakeTimer] = []
    fail_stops: list[int] = []
    protocol_release = asyncio.Event()
    runtime_release = asyncio.Event()
    audit_release = asyncio.Event()
    protocol_waits = 0
    real_wait = asyncio.wait

    class BlockingFrame:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def write(self, data: bytes | memoryview) -> int:
            self.started.set()
            self.release.wait()
            return len(data)

        def flush(self) -> None:
            return None

    class Writer:
        def __init__(self, frame: Any) -> None:
            self.frame = frame

        def write(self, data: bytes) -> int:
            return self.frame.write(data)

    frame_file = BlockingFrame()

    class BudgetStreams(_FakeStreams):
        async def drain_response_writer(self, timeout: float = 2.0) -> bool:
            assert timeout == 2.0
            clock.advance(0.5)
            return True

        async def close_response_writer(self, timeout: float = 1.0) -> bool:
            assert timeout == 1.0
            clock.advance(timeout)
            return True

    async def runtime_close() -> None:
        await runtime_release.wait()

    composition = _FakeComposition(runtime_close)

    async def close_adapters(
        timeout: float,
        *,
        runtime_audit_task: asyncio.Future[Any],
    ) -> bool:
        assert timeout == 5.0
        assert runtime_audit_task.done()
        clock.advance(timeout)
        composition.adapter_calls.append((timeout, runtime_audit_task))
        return True

    composition.close_adapters = close_adapters

    async def protocol_runner(
        *args: Any,
        response_writer: Writer,
        **kwargs: Any,
    ) -> None:
        response_writer.write(b"controlled-frame")
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await protocol_release.wait()

    async def task_waiter(
        futures: Any,
        *,
        timeout: float | None = None,
        return_when: Any = asyncio.ALL_COMPLETED,
    ) -> Any:
        nonlocal protocol_waits
        tasks = set(futures)
        names = {task.get_name() for task in tasks if isinstance(task, asyncio.Task)}
        if timeout == 5.0 and "acp-run-agent" in names:
            protocol_waits += 1
            clock.advance(timeout)
            if protocol_waits == 1:
                await asyncio.sleep(0)
                return set(), tasks
            await asyncio.sleep(0)
            protocol_release.set()
            return await real_wait(tasks)
        if timeout == 1.5 and "acp-frame-delivery-terminal" in names:
            while not frame_file.started.is_set():
                await asyncio.sleep(0)
            clock.advance(timeout)
            frame_file.release.set()
            return await real_wait(tasks)
        if timeout == 36.0:
            clock.advance(timeout)
            runtime_release.set()
            return await real_wait(tasks)
        if timeout == 0.1 and "budget-audit-child" in names:
            clock.advance(5.0)
            audit_release.set()
            return await real_wait(tasks)
        return await real_wait(
            tasks,
            timeout=timeout,
            return_when=return_when,
        )

    async def composition_factory() -> Any:
        async def audit_child() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await audit_release.wait()

        asyncio.create_task(audit_child(), name="budget-audit-child")
        return composition

    streams = BudgetStreams()
    lifecycle = _lifecycle(
        streams=streams,
        composition=composition,
        protocol_runner=protocol_runner,
        timeouts=PRODUCTION_TIMEOUTS,
        timers=timers,
        task_waiter=task_waiter,
        frame_file=frame_file,
    )
    lifecycle._clock = clock
    lifecycle._fail_stop = fail_stops.append
    lifecycle._composition_factory = composition_factory

    async def streams_factory(frame: Any) -> Any:
        streams.response_writer = Writer(frame)
        return streams

    lifecycle._streams_factory = streams_factory
    tasks_before = asyncio.all_tasks()
    run_task = asyncio.create_task(lifecycle.run(), name="budget-lifecycle")
    while lifecycle._protocol_task is None:
        await asyncio.sleep(0)
    lifecycle.stop_protocol_intake()

    assert await run_task == 0
    assert clock.value == 59.0
    assert checkpoints == [
        5.0,
        10.0,
        10.5,
        12.0,
        13.0,
        49.0,
        54.0,
        59.0,
    ]
    assert lifecycle._watchdog_deadline == 65.0
    assert timers[0].interval == 65.0
    assert timers[0].cancelled
    assert timers[0].joined
    assert fail_stops == []
    assert all(task.done() for task in lifecycle.host_stage_tasks)
    assert lifecycle._delivery is not None
    assert lifecycle._delivery.terminal
    assert asyncio.all_tasks() == tasks_before


@pytest.mark.parametrize("mode", ["start-failure", "cancelled"])
def test_unproven_runtime_driver_hard_stops_before_audit_adapters_or_return(
    mode: str,
    tmp_path: Path,
) -> None:
    ready_marker = tmp_path / f"runtime-{mode}-ready"
    audit_marker = tmp_path / f"runtime-{mode}-audit"
    adapter_marker = tmp_path / f"runtime-{mode}-adapter"
    returned_marker = tmp_path / f"runtime-{mode}-returned"
    script = textwrap.dedent(
        f"""
        import asyncio
        import io
        from mimir.acp.host import HostLifecycle, LifecycleTimeouts

        mode = {mode!r}
        ready_marker = {str(ready_marker)!r}
        audit_marker = {str(audit_marker)!r}
        adapter_marker = {str(adapter_marker)!r}
        returned_marker = {str(returned_marker)!r}

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
            def start_runtime_close(self):
                open(ready_marker, "w").close()
                if mode == "start-failure":
                    raise RuntimeError("start detail")
                task = asyncio.create_task(asyncio.sleep(60))
                task.cancel()
                return task
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
                    runtime_audit=0.01,
                    adapter_cleanup=0.01,
                    audit_rescan=0.005,
                    watchdog=0.2,
                ),
                streams_factory=streams_factory,
                composition_factory=composition_factory,
                agent_factory=lambda bundle: object(),
                protocol_runner=protocol_runner,
            )
            original_audit = lifecycle.audit_runtime_tasks_terminal
            async def marked_audit():
                open(audit_marker, "w").close()
                await original_audit()
            lifecycle.audit_runtime_tasks_terminal = marked_audit
            await lifecycle.run()
            open(returned_marker, "w").close()

        asyncio.run(main())
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = b""
    stderr = b""
    try:
        _wait_for_process_marker(ready_marker, process)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            stdout, stderr = _kill_and_reap(process)

    assert process.returncode == 1, stderr.decode()
    assert stdout == b""
    assert not audit_marker.exists()
    assert not adapter_marker.exists()
    assert not returned_marker.exists()


def test_cancellation_resistant_runtime_child_hard_fail_stops_subprocess(
    tmp_path: Path,
) -> None:
    ready_marker = tmp_path / "resistant-ready"
    adapter_marker = tmp_path / "adapter"
    descriptor_marker = tmp_path / "descriptor"
    script = textwrap.dedent(
        f"""
        import asyncio
        import io
        from mimir.acp.host import HostLifecycle, LifecycleTimeouts

        ready_marker = {str(ready_marker)!r}
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
            resistant_entered = asyncio.Event()
            async def streams_factory(frame):
                return streams
            async def composition_factory():
                async def resistant():
                    resistant_entered.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        open(ready_marker, "w").close()
                        await asyncio.Future()
                asyncio.create_task(resistant(), name="subprocess-resistant-child")
                await resistant_entered.wait()
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
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = b""
    stderr = b""
    try:
        _wait_for_process_marker(ready_marker, process)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            stdout, stderr = _kill_and_reap(process)

    assert process.returncode == 1, stderr.decode()
    assert stdout == b""
    assert not adapter_marker.exists()
    assert not descriptor_marker.exists()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_real_signal_completes_ordered_subprocess_cleanup(
    signum: signal.Signals,
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX process signals are unavailable")
    ready_marker = tmp_path / f"signal-{signum.name}-ready"
    marker = tmp_path / f"signal-{signum.name}.json"
    script = textwrap.dedent(
        f"""
        import asyncio
        import json
        import os
        import sys
        from mimir.acp.host import HostLifecycle, LifecycleTimeouts

        ready_marker = {str(ready_marker)!r}
        marker = {str(marker)!r}
        events = []
        stopped = asyncio.Event()

        class Streams:
            def __init__(self):
                self.request_reader = asyncio.StreamReader()
                self.response_writer = object()
                self.stopped = False
            def stop_request_intake(self):
                if self.stopped:
                    return
                self.stopped = True
                events.append("intake")
                self.request_reader.feed_eof()
                stopped.set()
            async def drain_response_writer(self, timeout=2.0):
                events.append("drain")
                return True
            async def close_response_writer(self, timeout=1.0):
                events.append("writer-close")
                return True
            def writer_helper_tasks(self):
                return ()

        class Composition:
            bundle = object()
            def __init__(self):
                self.driver = None
            def start_runtime_close(self):
                if self.driver is None:
                    async def close():
                        events.append("runtime")
                    self.driver = asyncio.create_task(close(), name="signal-runtime")
                return self.driver
            def explicit_adapter_tasks(self):
                return frozenset()
            async def close_adapters(self, timeout, *, runtime_audit_task):
                assert runtime_audit_task.done()
                assert runtime_audit_task.exception() is None
                events.append("adapters")
                return True

        async def main():
            streams = Streams()
            composition = Composition()
            async def streams_factory(frame):
                return streams
            async def composition_factory():
                return composition
            async def protocol_runner(*args, **kwargs):
                open(ready_marker, "w").close()
                await stopped.wait()
                events.append("protocol")
            lifecycle = HostLifecycle(
                sys.stdout.buffer,
                timeouts=LifecycleTimeouts(
                    protocol_grace=0.5,
                    protocol_cancel=0.5,
                    writer_drain=0.2,
                    writer_close=0.1,
                    runtime_driver=0.5,
                    runtime_audit=0.2,
                    adapter_cleanup=0.2,
                    audit_rescan=0.01,
                    watchdog=3.0,
                ),
                streams_factory=streams_factory,
                composition_factory=composition_factory,
                agent_factory=lambda bundle: object(),
                protocol_runner=protocol_runner,
            )
            status = await lifecycle.run()
            with open(marker, "w") as output:
                json.dump({{"status": status, "events": events}}, output)
            return status

        raise SystemExit(asyncio.run(main()))
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = b""
    stderr = b""
    try:
        _wait_for_process_marker(ready_marker, process)
        os.kill(process.pid, signum)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            stdout, stderr = _kill_and_reap(process)

    assert process.returncode == 0, stderr.decode()
    assert stdout == b""
    result = json.loads(marker.read_text())
    assert result["status"] == 0
    assert result["events"] == [
        "intake",
        "protocol",
        "drain",
        "writer-close",
        "runtime",
        "adapters",
    ]


def test_unread_stdout_backpressure_hard_fail_stops_subprocess(
    tmp_path: Path,
) -> None:
    ready_marker = tmp_path / "backpressure-ready"
    adapter_marker = tmp_path / "backpressure-adapter"
    returned_marker = tmp_path / "backpressure-returned"
    script = textwrap.dedent(
        f"""
        import asyncio
        import sys
        from mimir.acp.host import HostLifecycle, LifecycleTimeouts

        ready_marker = {str(ready_marker)!r}
        adapter_marker = {str(adapter_marker)!r}
        returned_marker = {str(returned_marker)!r}

        class Writer:
            def __init__(self, frame):
                self.frame = frame
            def write(self, data):
                return self.frame.write(data)

        class Streams:
            def __init__(self, frame):
                self.request_reader = asyncio.StreamReader()
                self.response_writer = Writer(frame)
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
            composition = Composition()
            async def streams_factory(frame):
                return Streams(frame)
            async def composition_factory():
                return composition
            async def protocol_runner(agent, *, response_writer, **kwargs):
                open(ready_marker, "w").close()
                payload = b"x" * (1024 * 1024)
                for _ in range(32):
                    response_writer.write(payload)
            lifecycle = HostLifecycle(
                sys.stdout.buffer,
                timeouts=LifecycleTimeouts(
                    protocol_grace=0.05,
                    protocol_cancel=0.05,
                    writer_drain=0.05,
                    writer_close=0.05,
                    runtime_driver=0.05,
                    runtime_audit=0.05,
                    adapter_cleanup=0.05,
                    audit_rescan=0.005,
                    watchdog=0.3,
                ),
                streams_factory=streams_factory,
                composition_factory=composition_factory,
                agent_factory=lambda bundle: object(),
                protocol_runner=protocol_runner,
            )
            await lifecycle.run()
            open(returned_marker, "w").close()

        asyncio.run(main())
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = b""
    stderr = b""
    try:
        _wait_for_process_marker(ready_marker, process)
        # The wait timeout owns the bounded-shutdown assertion. A tighter
        # wall-clock check also measures interpreter startup and scheduler load.
        process.wait(timeout=5)
        stdout, stderr = process.communicate(timeout=2)
    finally:
        if process.poll() is None:
            stdout, stderr = _kill_and_reap(process)

    assert process.returncode == 1, stderr.decode()
    assert stdout
    assert not adapter_marker.exists()
    assert not returned_marker.exists()


def test_lifecycle_has_no_environment_or_authentication_timeout_controls() -> None:
    source = Path(HostLifecycle.__module__.replace(".", "/") + ".py")
    assert "MIMIR_ACP" not in (ROOT / source).read_text()
    assert "auth" not in HostLifecycle.__init__.__annotations__


async def test_transport_death_tears_down_only_bound_generation() -> None:
    class Peer:
        def __init__(self) -> None:
            self.disconnects: list[str] = []

        async def disconnect_mcp(self, connection_id: str) -> None:
            self.disconnects.append(connection_id)

    old_peer = Peer()
    new_peer = Peer()
    old_connection = ConnectionState(1, old_peer)
    new_connection = ConnectionState(2, new_peer)
    old_provider = SimpleNamespace(
        peer=old_peer, connection_id="old-connection", closed=False
    )
    successor_provider = SimpleNamespace(
        peer=new_peer, connection_id="new-connection", closed=False
    )
    old_state = SimpleNamespace(
        generation=1,
        active_prompt=None,
        provider=old_provider,
        record=SimpleNamespace(session_id="old-only"),
    )
    successor = SimpleNamespace(
        generation=2,
        active_prompt=None,
        provider=successor_provider,
        record=SimpleNamespace(session_id="shared"),
    )
    stale_same_id = SimpleNamespace(
        generation=1,
        active_prompt=None,
        provider=old_provider,
        record=SimpleNamespace(session_id="shared"),
    )
    old_connection.connection_sessions["old-connection"] = old_state
    new_connection.connection_sessions["new-connection"] = successor

    agent = object.__new__(MimirAcpAgent)
    agent._connections = {1: old_connection, 2: new_connection}
    agent._connection = new_connection
    agent._client = new_peer
    agent._bridge = SimpleNamespace(_connected=True)
    agent._sessions = {"old-only": old_state, "shared": successor}
    agent._environments = {"old-only": (1, object()), "shared": (2, object())}
    agent._boundary_lock = asyncio.Lock()

    old_connection.server_sessions["old"] = stale_same_id
    await agent.on_transport_closed(1)
    await agent.on_transport_closed(1)

    assert old_connection.closed is True
    assert old_peer.disconnects == []
    assert "old-only" not in agent._sessions
    assert agent._sessions["shared"] is successor
    assert agent._environments["shared"][0] == 2
    assert successor_provider.closed is False
    assert new_peer.disconnects == []
    assert agent._connection is new_connection
    assert agent._client is new_peer
    assert agent._bridge._connected is True


async def test_inbound_mcp_generation_identity_prevents_connection_id_collision() -> None:
    old_state = SimpleNamespace(generation=1, record=SimpleNamespace(session_id="old"))
    new_state = SimpleNamespace(generation=2, record=SimpleNamespace(session_id="new"))
    old_connection = ConnectionState(1, object())
    new_connection = ConnectionState(2, object())
    old_connection.connection_sessions["collision"] = old_state
    new_connection.connection_sessions["collision"] = new_state
    agent = object.__new__(MimirAcpAgent)
    agent._connections = {1: old_connection, 2: new_connection}
    observed: list[tuple[int, str]] = []

    async def revalidate(state: Any) -> None:
        observed.append((state.generation, state.record.session_id))

    agent._revalidate_provider = revalidate
    await agent.on_mcp_notification(1, "collision", "notifications/tools/list_changed", None)
    await agent.on_mcp_notification(2, "collision", "notifications/tools/list_changed", None)
    await asyncio.gather(*old_connection.tasks, *new_connection.tasks)

    assert observed == [(1, "old"), (2, "new")]


async def test_replaced_generation_retirement_is_not_owned_by_successor_connection() -> None:
    agent = object.__new__(MimirAcpAgent)
    old_peer = object()
    new_peer = object()
    old = ConnectionState(1, old_peer)
    agent._connection = old
    agent._connections = {1: old}
    agent._generation = 1
    agent._client = old_peer
    agent._auth_context = object()
    agent._display_name = "old"
    agent._bridge = SimpleNamespace(_connected=False)
    agent._active_prompts = {}
    agent._environments = {}
    agent._retirement_tasks = set()
    retired = asyncio.Event()

    async def retire(generation: int) -> None:
        assert generation == 1
        retired.set()

    agent._retire_generation = retire
    successor_generation = agent.on_connect(new_peer)
    successor = agent._connections[successor_generation]
    await retired.wait()
    await asyncio.gather(*agent._retirement_tasks)

    assert successor.tasks == set()
    assert agent._connection is successor
    assert successor.auth_context is None
    assert successor.principal is None
