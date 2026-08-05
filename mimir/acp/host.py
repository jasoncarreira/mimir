from __future__ import annotations

import asyncio
import os
import queue
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from enum import Enum, auto
from types import FrameType
from typing import Any, BinaryIO, Protocol, TypeVar, cast

from .agent import MimirAcpAgent
from .composition import AcpComposition
from .sdk import run_stdio_agent
from .stdio import ProtocolStreams, open_protocol_streams


_T = TypeVar("_T")
_PEER_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError)
_RUNTIME_ERROR = "ACP host failed."
_DELIVERY_END = object()
FRAME_DELIVERY_CAPACITY = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LifecycleTimeouts:
    protocol_grace: float = 5.0
    protocol_cancel: float = 5.0
    writer_drain: float = 2.0
    writer_close: float = 1.0
    runtime_driver: float = 36.0
    runtime_audit: float = 5.0
    adapter_cleanup: float = 5.0
    audit_rescan: float = 0.1
    watchdog: float = 65.0


PRODUCTION_TIMEOUTS = LifecycleTimeouts()


class _Timer(Protocol):
    daemon: bool

    def start(self) -> None: ...

    def cancel(self) -> None: ...

    def join(self) -> None: ...


class _Stage(Enum):
    PROTOCOL = auto()
    WRITER = auto()
    RUNTIME = auto()
    AUDIT = auto()
    ADAPTERS = auto()


class _StageState(Enum):
    NOT_STARTED = auto()
    RUNNING = auto()
    TERMINAL = auto()


TimerFactory = Callable[[float, Callable[[], None]], _Timer]
StreamsFactory = Callable[[BinaryIO], Awaitable[ProtocolStreams]]
CompositionFactory = Callable[[], Awaitable[AcpComposition]]
ProtocolRunner = Callable[..., Awaitable[None]]
TaskWaiter = Callable[..., Awaitable[tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]]]


class _FrameDelivery:
    def __init__(
        self,
        frame_file: BinaryIO,
        capacity: int,
        error_callback: Callable[[], None],
    ) -> None:
        if capacity <= 0:
            raise ValueError("frame delivery capacity must be positive")
        self._frame_file = frame_file
        self._queue: queue.SimpleQueue[bytes | object] = queue.SimpleQueue()
        self._loop = asyncio.get_running_loop()
        self._terminal = self._loop.create_future()
        self._capacity = capacity
        self._reserved_bytes = 0
        self._peak_reserved_bytes = 0
        self._lock = threading.Lock()
        self._error_callback = error_callback
        self._accepting = True
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._deliver,
            name="acp-frame-delivery",
            daemon=True,
        )
        self._thread.start()

    @property
    def closed(self) -> bool:
        return not self._accepting

    @property
    def terminal(self) -> bool:
        return not self._thread.is_alive()

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved_bytes

    @property
    def peak_reserved_bytes(self) -> int:
        with self._lock:
            return self._peak_reserved_bytes

    def fileno(self) -> int:
        return self._frame_file.fileno()

    def write(self, data: bytes | bytearray | memoryview) -> int:
        size = len(data)
        if size == 0:
            return 0
        delivery_error: BaseException | None = None
        with self._lock:
            if not self._accepting:
                raise ConnectionResetError("protocol frame delivery is closed")
            if self._error is not None:
                raise self._error
            if size > self._capacity - self._reserved_bytes:
                delivery_error = BufferError(
                    "protocol frame delivery capacity exceeded"
                )
                self._error = delivery_error
                self._accepting = False
                self._queue.put(_DELIVERY_END)
            else:
                self._reserved_bytes += size
                self._peak_reserved_bytes = max(
                    self._peak_reserved_bytes,
                    self._reserved_bytes,
                )
                try:
                    payload = bytes(data)
                except BaseException as exc:
                    self._reserved_bytes -= size
                    self._error = exc
                    self._accepting = False
                    self._queue.put(_DELIVERY_END)
                    delivery_error = exc
                else:
                    self._queue.put(payload)
        if delivery_error is not None:
            self._loop.call_soon(self._error_callback)
            raise delivery_error
        return size

    def flush(self) -> None:
        with self._lock:
            if self._error is not None:
                raise self._error

    def finish(self) -> None:
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
            self._queue.put(_DELIVERY_END)

    async def wait_terminal(self) -> None:
        await self._terminal
        if self._error is not None:
            raise self._error

    def join(self) -> None:
        self._thread.join()

    def _deliver(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is _DELIVERY_END:
                    break
                remaining = memoryview(cast(bytes, item))
                while remaining:
                    written = self._frame_file.write(remaining)
                    if written is None:
                        written = len(remaining)
                    if written <= 0:
                        raise BrokenPipeError("protocol frame write made no progress")
                    remaining = remaining[written:]
                self._frame_file.flush()
                with self._lock:
                    self._reserved_bytes -= len(cast(bytes, item))
        except BaseException as exc:
            with self._lock:
                if self._error is None:
                    self._error = exc
                self._accepting = False
            self._loop.call_soon_threadsafe(self._error_callback)
        finally:
            try:
                self._loop.call_soon_threadsafe(self._mark_terminal)
            except RuntimeError:
                pass

    def _mark_terminal(self) -> None:
        if not self._terminal.done():
            self._terminal.set_result(None)


class HostLifecycle:
    def __init__(
        self,
        frame_file: BinaryIO,
        *,
        timeouts: LifecycleTimeouts = PRODUCTION_TIMEOUTS,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: TimerFactory = threading.Timer,
        fail_stop: Callable[[int], None] = os._exit,
        task_waiter: TaskWaiter = asyncio.wait,
        frame_delivery_capacity: int = FRAME_DELIVERY_CAPACITY,
        streams_factory: StreamsFactory = open_protocol_streams,
        composition_factory: CompositionFactory = AcpComposition.create,
        agent_factory: Callable[[Any], Any] = MimirAcpAgent,
        protocol_runner: ProtocolRunner = run_stdio_agent,
    ) -> None:
        self._frame_file = frame_file
        self._timeouts = timeouts
        self._clock = clock
        self._timer_factory = timer_factory
        self._fail_stop = fail_stop
        self._task_waiter = task_waiter
        self._frame_delivery_capacity = frame_delivery_capacity
        self._streams_factory = streams_factory
        self._composition_factory = composition_factory
        self._agent_factory = agent_factory
        self._protocol_runner = protocol_runner
        self._delivery: _FrameDelivery | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._streams: ProtocolStreams | None = None
        self._composition: AcpComposition | None = None
        self._protocol_task: asyncio.Task[None] | None = None
        self._runtime_driver: asyncio.Task[None] | None = None
        self._runtime_audit_task: asyncio.Task[None] | None = None
        self._pre_composition_tasks: frozenset[asyncio.Task[Any]] = frozenset()
        self._audited_runtime_tasks: set[asyncio.Task[Any]] = set()
        self._audit_cancel_requested: set[asyncio.Task[Any]] = set()
        self._retrieved_tasks: set[asyncio.Future[Any]] = set()
        self._host_stage_tasks: set[asyncio.Task[Any]] = set()
        self._required_stages: set[_Stage] = {_Stage.WRITER}
        self._stage_states = {
            stage: _StageState.NOT_STARTED for stage in _Stage
        }
        self._shutdown_requested = asyncio.Event()
        self._watchdog: _Timer | None = None
        self._watchdog_deadline: float | None = None
        self._loop_signal_handlers: set[signal.Signals] = set()
        self._fallback_signal_handlers: dict[
            signal.Signals,
            signal.Handlers,
        ] = {}
        self._protocol_cancel_requested = False
        self._peer_disconnected = False
        self._failed = False
        self._audit_pending_names: tuple[str, ...] = ()

    @property
    def audited_runtime_tasks(self) -> frozenset[asyncio.Task[Any]]:
        return frozenset(self._audited_runtime_tasks)

    @property
    def audit_cancel_requested(self) -> frozenset[asyncio.Task[Any]]:
        return frozenset(self._audit_cancel_requested)

    @property
    def host_stage_tasks(self) -> frozenset[asyncio.Task[Any]]:
        return frozenset(self._host_stage_tasks)

    @property
    def audit_pending_names(self) -> tuple[str, ...]:
        return self._audit_pending_names

    async def run(self) -> int:
        self._delivery = _FrameDelivery(
            self._frame_file,
            self._frame_delivery_capacity,
            self._frame_delivery_failed,
        )
        await self._start()
        self.stop_protocol_intake()
        if self._protocol_task is not None:
            await self._run_stage(_Stage.PROTOCOL, self.await_protocol_terminal)
        await self._run_stage(_Stage.WRITER, self.close_protocol_writer)
        if self._composition is not None:
            await self._run_stage(_Stage.RUNTIME, self.await_runtime_driver_terminal)
            if not self._runtime_driver_proven_terminal():
                self._mark_failed()
                await asyncio.Future()
        if self._pre_composition_tasks:
            await self._start_runtime_audit()
        if self._composition is not None:
            await self._run_stage(_Stage.ADAPTERS, self.close_adapters_terminal)
        if not self._teardown_proven_terminal():
            self._mark_failed()
            await asyncio.Future()
        self._remove_signal_handlers()
        self._stop_watchdog()
        return 1 if self._failed else 0

    def _frame_delivery_failed(self) -> None:
        self._mark_failed()
        self.stop_protocol_intake()

    async def _start(self) -> None:
        delivery = self._delivery
        if delivery is None:
            self._mark_failed()
            return
        try:
            self._streams = await self._streams_factory(cast(BinaryIO, delivery))
        except BaseException:
            self._mark_failed()
            return
        self._install_signal_handlers()
        self._pre_composition_tasks = frozenset(asyncio.all_tasks())
        self._required_stages.add(_Stage.AUDIT)
        try:
            self._composition = await self._composition_factory()
        except BaseException:
            self._mark_failed()
            return
        self._required_stages.update({_Stage.RUNTIME, _Stage.ADAPTERS})
        if self._shutdown_requested.is_set():
            return
        try:
            agent = self._agent_factory(self._composition.bundle)
            self._protocol_task = asyncio.create_task(
                self._protocol_runner(
                    agent,
                    request_reader=self._streams.request_reader,
                    response_writer=self._streams.response_writer,
                ),
                name="acp-run-agent",
            )
        except BaseException:
            self._mark_failed()
            return
        self._required_stages.add(_Stage.PROTOCOL)
        try:
            await self._wait_for_shutdown_request()
        except BaseException:
            self._mark_failed()

    async def _run_stage(
        self,
        stage: _Stage,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        self._stage_states[stage] = _StageState.RUNNING
        try:
            await operation()
        except BaseException:
            self._mark_failed()
        finally:
            self._stage_states[stage] = _StageState.TERMINAL

    async def _start_runtime_audit(self) -> None:
        self._stage_states[_Stage.AUDIT] = _StageState.RUNNING
        try:
            self._runtime_audit_task = self._create_host_stage_task(
                self.audit_runtime_tasks_terminal(),
                name="acp-runtime-task-audit",
            )
            await self._runtime_audit_task
            self._retrieve_task(self._runtime_audit_task, failure_is_error=True)
        except BaseException:
            self._mark_failed()
        finally:
            self._stage_states[_Stage.AUDIT] = _StageState.TERMINAL

    def stop_protocol_intake(self) -> None:
        streams = self._streams
        if streams is not None:
            try:
                streams.stop_request_intake()
            except BaseException:
                self._mark_failed()
        self._shutdown_requested.set()
        if self._watchdog is not None:
            return
        self._watchdog_deadline = self._clock() + self._timeouts.watchdog
        try:
            watchdog = self._timer_factory(
                self._timeouts.watchdog,
                lambda: self._fail_stop(1),
            )
            watchdog.daemon = True
            watchdog.start()
        except BaseException:
            self._mark_failed()
            return
        self._watchdog = watchdog

    async def await_protocol_terminal(self) -> None:
        task = self._protocol_task
        if task is None:
            self._mark_failed()
            return
        done, _ = await self._wait_tasks(
            {task},
            timeout=max(0.0, self._timeouts.protocol_grace),
        )
        if task not in done:
            self._protocol_cancel_requested = True
            task.cancel()
            done, _ = await self._wait_tasks(
                {task},
                timeout=max(0.0, self._timeouts.protocol_cancel),
            )
        if task not in done:
            self._mark_failed()
            await self._wait_tasks({task})
        self._retrieve_protocol_task(task)

    async def close_protocol_writer(self) -> None:
        streams = self._streams
        drained = streams is None
        drain_started = self._clock()
        if streams is not None:
            try:
                drained = await streams.drain_response_writer(
                    timeout=max(0.0, self._timeouts.writer_drain)
                )
            except _PEER_DISCONNECT_ERRORS:
                self._peer_disconnected = True
            except BaseException:
                self._mark_failed()
        delivery = self._delivery
        if delivery is None:
            self._mark_failed()
            return
        delivery.finish()
        self._delivery_task = self._create_host_stage_task(
            delivery.wait_terminal(),
            name="acp-frame-delivery-terminal",
        )
        drain_remaining = max(
            0.0,
            self._timeouts.writer_drain - (self._clock() - drain_started),
        )
        delivery_done = await self._delivery_done_within(drain_remaining)
        if not delivery_done:
            self._mark_failed()
        close_started = self._clock()
        closed = streams is None
        if streams is not None:
            try:
                closed = await streams.close_response_writer(
                    timeout=max(0.0, self._timeouts.writer_close)
                )
            except _PEER_DISCONNECT_ERRORS:
                self._peer_disconnected = True
            except BaseException:
                self._mark_failed()
            if not drained or not closed:
                if self._writer_has_peer_disconnect():
                    self._peer_disconnected = True
                elif not self._peer_disconnected:
                    self._mark_failed()
            for task in streams.writer_helper_tasks():
                if task.done():
                    self._retrieve_task(task)
        if not delivery_done:
            close_remaining = max(
                0.0,
                self._timeouts.writer_close - (self._clock() - close_started),
            )
            delivery_done = await self._delivery_done_within(close_remaining)
        if not delivery_done:
            await self._wait_tasks({self._delivery_task})
        self._retrieve_delivery_task()
        if delivery.terminal:
            delivery.join()

    async def _delivery_done_within(self, timeout: float) -> bool:
        task = self._delivery_task
        if task is None:
            return False
        if task.done():
            return True
        done, _ = await self._wait_tasks({task}, timeout=max(0.0, timeout))
        return task in done

    def _retrieve_delivery_task(self) -> None:
        task = self._delivery_task
        if task is None or task in self._retrieved_tasks or not task.done():
            return
        self._retrieved_tasks.add(task)
        if task.cancelled():
            self._mark_failed()
            return
        try:
            task.result()
        except _PEER_DISCONNECT_ERRORS:
            self._peer_disconnected = True
        except BaseException:
            self._mark_failed()

    async def await_runtime_driver_terminal(self) -> None:
        composition = self._composition
        if composition is None:
            self._mark_failed()
            return
        try:
            driver = composition.start_runtime_close()
        except BaseException:
            self._mark_failed()
            return
        if not isinstance(driver, asyncio.Task):
            self._mark_failed()
            return
        self._runtime_driver = driver
        done, _ = await self._wait_tasks(
            {driver},
            timeout=max(0.0, self._timeouts.runtime_driver),
        )
        if driver not in done:
            self._mark_failed()
            await self._wait_tasks({driver})
        self._retrieve_task(driver, failure_is_error=True)

    async def audit_runtime_tasks_terminal(self) -> None:
        deadline = self._clock() + self._timeouts.runtime_audit
        empty_scans = 0
        threshold_recorded = False
        while True:
            candidates = self._runtime_task_candidates(asyncio.current_task())
            self._audited_runtime_tasks.update(candidates)
            for task in tuple(self._audited_runtime_tasks):
                if task.done():
                    self._retrieve_task(task, failure_is_error=True)
                elif task not in self._audit_cancel_requested:
                    self._audit_cancel_requested.add(task)
                    task.cancel()
            pending = {
                task for task in self._audited_runtime_tasks if not task.done()
            }
            if not threshold_recorded and self._clock() >= deadline and pending:
                threshold_recorded = True
                self._audit_pending_names = tuple(
                    sorted(task.get_name() for task in pending)
                )
                self._mark_failed()
            if not pending and not candidates:
                empty_scans += 1
                if empty_scans == 2:
                    for task in self._audited_runtime_tasks:
                        self._retrieve_task(task, failure_is_error=True)
                    return
                await asyncio.sleep(0)
                continue
            empty_scans = 0
            if pending:
                await self._wait_tasks(
                    pending,
                    timeout=max(0.0, self._timeouts.audit_rescan),
                )
            else:
                await asyncio.sleep(0)

    async def close_adapters_terminal(self) -> None:
        composition = self._composition
        audit_task = self._runtime_audit_task
        if composition is None or audit_task is None:
            self._mark_failed()
            return
        timeout = min(
            max(0.0, self._timeouts.adapter_cleanup),
            self._remaining_watchdog_time(),
        )
        try:
            closed = await composition.close_adapters(
                timeout,
                runtime_audit_task=audit_task,
            )
            if not closed:
                self._mark_failed()
                await self._await_explicit_adapter_tasks_terminal()
                await composition.close_adapters(
                    0,
                    runtime_audit_task=audit_task,
                )
        except BaseException:
            self._mark_failed()

    def _create_host_stage_task(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        name: str,
    ) -> asyncio.Task[_T]:
        task = asyncio.create_task(coroutine, name=name)
        self._host_stage_tasks.add(task)
        return task

    async def _wait_for_shutdown_request(self) -> None:
        protocol_task = self._protocol_task
        if protocol_task is None:
            return
        stop_waiter = self._create_host_stage_task(
            self._shutdown_requested.wait(),
            name="acp-host-shutdown-wait",
        )
        done, _ = await self._wait_tasks(
            {protocol_task, stop_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if protocol_task in done and not stop_waiter.done():
            stop_waiter.cancel()
        await asyncio.gather(stop_waiter, return_exceptions=True)
        self._retrieve_task(stop_waiter)

    async def _wait_tasks(
        self,
        tasks: Iterable[asyncio.Future[Any]],
        *,
        timeout: float | None = None,
        return_when: str = asyncio.ALL_COMPLETED,
    ) -> tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]:
        return await self._task_waiter(
            set(tasks),
            timeout=timeout,
            return_when=return_when,
        )

    def _runtime_task_candidates(
        self,
        current: asyncio.Task[Any] | None,
    ) -> set[asyncio.Task[Any]]:
        excluded: set[asyncio.Task[Any]] = set(self._pre_composition_tasks)
        excluded.update(self._host_stage_tasks)
        if current is not None:
            excluded.add(current)
        for task in (
            self._protocol_task,
            self._runtime_driver,
            self._delivery_task,
        ):
            if task is not None:
                excluded.add(task)
        streams = self._streams
        if streams is not None:
            excluded.update(streams.writer_helper_tasks())
        composition = self._composition
        if composition is not None:
            excluded.update(composition.explicit_adapter_tasks())
        return {
            task
            for task in asyncio.all_tasks()
            if task not in excluded and not task.done()
        }

    async def _await_explicit_adapter_tasks_terminal(self) -> None:
        composition = self._composition
        if composition is None:
            return
        while True:
            pending = {
                task
                for task in composition.explicit_adapter_tasks()
                if not task.done()
            }
            if not pending:
                return
            await self._wait_tasks(
                pending,
                timeout=min(0.1, self._remaining_watchdog_time()),
            )

    def _retrieve_protocol_task(self, task: asyncio.Task[Any]) -> None:
        if task in self._retrieved_tasks:
            return
        self._retrieved_tasks.add(task)
        if task.cancelled():
            if not self._protocol_cancel_requested:
                self._mark_failed()
            return
        try:
            task.result()
        except _PEER_DISCONNECT_ERRORS:
            self._peer_disconnected = True
        except BaseException:
            self._mark_failed()

    def _retrieve_task(
        self,
        task: asyncio.Future[Any],
        *,
        failure_is_error: bool = False,
    ) -> None:
        if task in self._retrieved_tasks or not task.done():
            return
        self._retrieved_tasks.add(task)
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException:
            if failure_is_error:
                self._mark_failed()

    def _remaining_watchdog_time(self) -> float:
        deadline = self._watchdog_deadline
        if deadline is None:
            return max(0.0, self._timeouts.watchdog)
        return max(0.0, deadline - self._clock())

    def _runtime_driver_proven_terminal(self) -> bool:
        driver = self._runtime_driver
        return (
            isinstance(driver, asyncio.Task)
            and driver.done()
            and not driver.cancelled()
            and driver in self._retrieved_tasks
        )

    def _writer_has_peer_disconnect(self) -> bool:
        streams = self._streams
        if streams is None:
            return False
        for task in streams.writer_helper_tasks():
            if not task.done() or task.cancelled():
                continue
            try:
                error = task.exception()
            except asyncio.CancelledError:
                continue
            if isinstance(error, _PEER_DISCONNECT_ERRORS):
                return True
        return False

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.stop_protocol_intake)
            except (NotImplementedError, RuntimeError):
                try:
                    previous = signal.getsignal(signum)
                    signal.signal(signum, self._fallback_signal_handler)
                except (OSError, RuntimeError, ValueError):
                    continue
                self._fallback_signal_handlers[signum] = previous
            else:
                self._loop_signal_handlers.add(signum)

    def _fallback_signal_handler(
        self,
        signum: int,
        frame: FrameType | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(self.stop_protocol_intake)

    def _remove_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in self._loop_signal_handlers:
            loop.remove_signal_handler(signum)
        self._loop_signal_handlers.clear()
        for signum, previous in self._fallback_signal_handlers.items():
            signal.signal(signum, previous)
        self._fallback_signal_handlers.clear()

    def _stop_watchdog(self) -> None:
        watchdog = self._watchdog
        if watchdog is None:
            return
        watchdog.cancel()
        watchdog.join()
        self._watchdog = None

    def _teardown_proven_terminal(self) -> bool:
        if any(
            self._stage_states[stage] is not _StageState.TERMINAL
            for stage in self._required_stages
        ):
            return False
        if self._delivery_task is None or not self._delivery_task.done():
            return False
        if self._delivery is None or not self._delivery.terminal:
            return False
        if _Stage.PROTOCOL in self._required_stages and self._protocol_task is None:
            return False
        if self._protocol_task is not None and not self._protocol_task.done():
            return False
        if _Stage.RUNTIME in self._required_stages:
            if not self._runtime_driver_proven_terminal():
                return False
        if _Stage.AUDIT in self._required_stages:
            audit_task = self._runtime_audit_task
            if (
                audit_task is None
                or not audit_task.done()
                or audit_task.cancelled()
                or audit_task.exception() is not None
            ):
                return False
        if self._runtime_audit_task is not None and not self._runtime_audit_task.done():
            return False
        if any(not task.done() for task in self._audited_runtime_tasks):
            return False
        current = asyncio.current_task()
        if any(
            task is not current and not task.done()
            for task in self._host_stage_tasks
        ):
            return False
        streams = self._streams
        if streams is not None and any(
            not task.done() for task in streams.writer_helper_tasks()
        ):
            return False
        composition = self._composition
        if composition is not None and any(
            not task.done() for task in composition.explicit_adapter_tasks()
        ):
            return False
        return True

    def _mark_failed(self) -> None:
        self._failed = True


def run(frame_file: BinaryIO) -> int:
    try:
        status = asyncio.run(HostLifecycle(frame_file).run())
    except BaseException:
        status = 1
    if status != 0:
        sys.stderr.write(f"{_RUNTIME_ERROR}\n")
        sys.stderr.flush()
    return status


__all__ = [
    "HostLifecycle",
    "LifecycleTimeouts",
    "PRODUCTION_TIMEOUTS",
    "run",
]
