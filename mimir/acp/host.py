from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from types import FrameType
from typing import Any, BinaryIO, Protocol, TypeVar

from .agent import MimirAcpAgent
from .composition import AcpComposition
from .sdk import run_stdio_agent
from .stdio import ProtocolStreams, open_protocol_streams


_T = TypeVar("_T")
_PEER_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError)
_RUNTIME_ERROR = "ACP host failed."


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


TimerFactory = Callable[[float, Callable[[], None]], _Timer | asyncio.Task[Any]]
StreamsFactory = Callable[[BinaryIO], Awaitable[ProtocolStreams]]
CompositionFactory = Callable[[], Awaitable[AcpComposition]]
ProtocolRunner = Callable[..., Awaitable[None]]


class HostLifecycle:
    def __init__(
        self,
        frame_file: BinaryIO,
        *,
        timeouts: LifecycleTimeouts = PRODUCTION_TIMEOUTS,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: TimerFactory = threading.Timer,
        fail_stop: Callable[[int], None] = os._exit,
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
        self._streams_factory = streams_factory
        self._composition_factory = composition_factory
        self._agent_factory = agent_factory
        self._protocol_runner = protocol_runner
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
        self._shutdown_requested = asyncio.Event()
        self._watchdog: _Timer | asyncio.Task[Any] | None = None
        self._watchdog_task: asyncio.Task[Any] | None = None
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
        try:
            self._streams = await self._streams_factory(self._frame_file)
            self._pre_composition_tasks = frozenset(asyncio.all_tasks())
            self._composition = await self._composition_factory()
            agent = self._agent_factory(self._composition.bundle)
            self._protocol_task = asyncio.create_task(
                self._protocol_runner(
                    agent,
                    request_reader=self._streams.request_reader,
                    response_writer=self._streams.response_writer,
                ),
                name="acp-run-agent",
            )
            self._install_signal_handlers()
            await self._wait_for_shutdown_request()
            self.stop_protocol_intake()
            await self.await_protocol_terminal()
            await self.close_protocol_writer()
            await self.await_runtime_driver_terminal()
            self._runtime_audit_task = self._create_host_stage_task(
                self.audit_runtime_tasks_terminal(),
                name="acp-runtime-task-audit",
            )
            await self._runtime_audit_task
            self._retrieve_task(self._runtime_audit_task)
            await self.close_adapters_terminal()
            if not self._all_owned_tasks_terminal():
                self._mark_failed()
                await asyncio.Future()
        except _PEER_DISCONNECT_ERRORS:
            self._peer_disconnected = True
            self._mark_failed_if_cleanup_incomplete()
        except asyncio.CancelledError:
            self._mark_failed()
            raise
        except BaseException:
            self._mark_failed()
        finally:
            if self._all_owned_tasks_terminal():
                self._remove_signal_handlers()
                self._stop_watchdog()
        return 1 if self._failed else 0

    def stop_protocol_intake(self) -> None:
        streams = self._streams
        if streams is not None:
            streams.stop_request_intake()
        self._shutdown_requested.set()
        if self._watchdog is not None:
            return
        self._watchdog_deadline = self._clock() + self._timeouts.watchdog
        watchdog = self._timer_factory(
            self._timeouts.watchdog,
            lambda: self._fail_stop(1),
        )
        self._watchdog = watchdog
        if isinstance(watchdog, asyncio.Task):
            self._watchdog_task = watchdog
            return
        watchdog.daemon = True
        watchdog.start()

    async def await_protocol_terminal(self) -> None:
        task = self._protocol_task
        if task is None:
            self._mark_failed()
            return
        done, _ = await asyncio.wait(
            {task},
            timeout=max(0.0, self._timeouts.protocol_grace),
        )
        if task not in done:
            self._protocol_cancel_requested = True
            task.cancel()
            done, _ = await asyncio.wait(
                {task},
                timeout=max(0.0, self._timeouts.protocol_cancel),
            )
        if task not in done:
            self._mark_failed()
            await asyncio.wait({task})
        self._retrieve_protocol_task(task)

    async def close_protocol_writer(self) -> None:
        streams = self._streams
        if streams is None:
            self._mark_failed()
            return
        drained = False
        closed = False
        try:
            drained = await streams.drain_response_writer(
                timeout=max(0.0, self._timeouts.writer_drain)
            )
        except _PEER_DISCONNECT_ERRORS:
            self._peer_disconnected = True
        except BaseException:
            self._mark_failed()
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

    async def await_runtime_driver_terminal(self) -> None:
        composition = self._composition
        if composition is None:
            self._mark_failed()
            return
        driver = composition.start_runtime_close()
        self._runtime_driver = driver
        done, _ = await asyncio.wait(
            {driver},
            timeout=max(0.0, self._timeouts.runtime_driver),
        )
        if driver not in done:
            self._mark_failed()
            await asyncio.wait({driver})
        self._retrieve_task(driver, failure_is_error=True)

    async def audit_runtime_tasks_terminal(self) -> None:
        deadline = self._clock() + self._timeouts.runtime_audit
        empty_scans = 0
        threshold_recorded = False
        while True:
            current = asyncio.current_task()
            candidates = self._runtime_task_candidates(current)
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
                await asyncio.wait(
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
            self._mark_failed()
            return
        stop_waiter = self._create_host_stage_task(
            self._shutdown_requested.wait(),
            name="acp-host-shutdown-wait",
        )
        done, _ = await asyncio.wait(
            {protocol_task, stop_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if protocol_task in done and not stop_waiter.done():
            stop_waiter.cancel()
        await asyncio.gather(stop_waiter, return_exceptions=True)
        self._retrieve_task(stop_waiter)

    def _runtime_task_candidates(
        self,
        current: asyncio.Task[Any] | None,
    ) -> set[asyncio.Task[Any]]:
        excluded: set[asyncio.Task[Any]] = set(self._pre_composition_tasks)
        excluded.update(self._host_stage_tasks)
        if current is not None:
            excluded.add(current)
        if self._protocol_task is not None:
            excluded.add(self._protocol_task)
        if self._runtime_driver is not None:
            excluded.add(self._runtime_driver)
        if self._watchdog_task is not None:
            excluded.add(self._watchdog_task)
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
            await asyncio.wait(
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
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
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
        if isinstance(watchdog, asyncio.Task):
            if not watchdog.done():
                watchdog.cancel()
            self._retrieve_task(watchdog)
        else:
            watchdog.cancel()
            watchdog.join()
        self._watchdog = None

    def _all_owned_tasks_terminal(self) -> bool:
        if self._protocol_task is not None and not self._protocol_task.done():
            return False
        if self._runtime_driver is not None and not self._runtime_driver.done():
            return False
        if self._runtime_audit_task is not None and not self._runtime_audit_task.done():
            return False
        if any(not task.done() for task in self._audited_runtime_tasks):
            return False
        if any(not task.done() for task in self._host_stage_tasks):
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

    def _mark_failed_if_cleanup_incomplete(self) -> None:
        if not self._all_owned_tasks_terminal():
            self._mark_failed()


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
