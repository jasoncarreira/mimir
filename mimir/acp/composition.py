from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from mimir.channel_registry import ChannelRegistry
from mimir.config import Config
from mimir.dispatcher import Dispatcher
from mimir.runtime import (
    AgentRuntimeBundle,
    CoreServices,
    RuntimeAdapters,
    create_agent_runtime,
    create_core_services,
)
from mimir.scheduler import Scheduler


class _AcpPairingNotifier:
    async def notify_operator(
        self,
        *,
        canonical: str,
        display: str,
        platform: str,
        channel_id: str,
        delivery: str,
    ) -> None:
        return None

    async def notify_pending_cap_reached(
        self,
        *,
        platform: str,
        channel_id: str,
        delivery: str,
    ) -> None:
        return None

    async def maybe_reply_dm(
        self,
        *,
        canonical: str,
        dm_channel_id: str,
    ) -> None:
        return None


def _as_cleanup_exception(exc: BaseException) -> Exception:
    if isinstance(exc, Exception):
        return exc
    return RuntimeError(f"{type(exc).__name__}: {exc}")


@dataclass(slots=True)
class AcpComposition:
    config: Config
    core: CoreServices
    adapters: RuntimeAdapters
    bundle: AgentRuntimeBundle
    _adapter_tasks: set[asyncio.Task[Any]] = field(repr=False)
    _runtime_close_driver: asyncio.Task[None] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _adapter_cleanup_task: asyncio.Task[None] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _runtime_audit_task: asyncio.Future[Any] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    @classmethod
    async def create(cls) -> AcpComposition:
        config = Config.from_env()
        core = create_core_services(config)
        dispatcher = Dispatcher(config, resolver=core.identity_resolver)
        scheduler = Scheduler(
            config.home / "scheduler.yaml",
            dispatcher.enqueue,
            home=config.home,
            scheduler_tz=config.scheduler_tz,
        )
        channels = ChannelRegistry()
        pairing_notifier = _AcpPairingNotifier()
        adapter_tasks: set[asyncio.Task[Any]] = set()

        def spawn_background_task(
            coroutine: Coroutine[Any, Any, None],
            name: str,
        ) -> asyncio.Task[Any]:
            task = asyncio.create_task(coroutine, name=name)
            adapter_tasks.add(task)
            return task

        adapters = RuntimeAdapters(
            dispatcher=dispatcher,
            scheduler=scheduler,
            channels=channels,
            pairing_notifier=pairing_notifier,
            spawn_background_task=spawn_background_task,
        )
        bundle = await create_agent_runtime(config, core, adapters)
        return cls(
            config=config,
            core=core,
            adapters=adapters,
            bundle=bundle,
            _adapter_tasks=adapter_tasks,
        )

    def start_runtime_close(self) -> asyncio.Task[None]:
        if self._runtime_close_driver is None:
            self._runtime_close_driver = asyncio.create_task(
                self.bundle.aclose(),
                name="acp-runtime-close-driver",
            )
        return self._runtime_close_driver

    def explicit_adapter_tasks(self) -> frozenset[asyncio.Task[Any]]:
        tasks = {task for task in self._adapter_tasks if not task.done()}
        if self._adapter_cleanup_task is not None:
            tasks.add(self._adapter_cleanup_task)
        return frozenset(tasks)

    async def close_adapters(
        self,
        timeout: float,
        *,
        runtime_audit_task: asyncio.Future[Any],
    ) -> bool:
        driver = self._runtime_close_driver
        if driver is None or not driver.done():
            raise RuntimeError("runtime close driver is not terminal")
        if driver.cancelled():
            raise RuntimeError("runtime close driver was cancelled")
        driver.exception()
        if not runtime_audit_task.done():
            raise RuntimeError("runtime task audit is not terminal")
        if runtime_audit_task.cancelled():
            raise RuntimeError("runtime task audit was cancelled")
        audit_error = runtime_audit_task.exception()
        if audit_error is not None:
            raise RuntimeError("runtime task audit failed") from audit_error
        if self._runtime_audit_task is None:
            self._runtime_audit_task = runtime_audit_task
        elif self._runtime_audit_task is not runtime_audit_task:
            raise RuntimeError("runtime task audit does not match cleanup owner")
        if self._adapter_cleanup_task is None:
            self._adapter_cleanup_task = asyncio.create_task(
                self._cleanup_adapters(),
                name="acp-adapter-cleanup",
            )
        task = self._adapter_cleanup_task
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
        if task not in done:
            return False
        task.result()
        return True

    async def _cleanup_adapters(self) -> None:
        errors: list[tuple[str, Exception]] = []
        try:
            await self.adapters.channels.disconnect_all()
        except Exception as exc:
            errors.append(("channels", exc))
        while True:
            tasks = tuple(task for task in self._adapter_tasks if not task.done())
            if not tasks:
                break
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)
            await asyncio.sleep(0)
        for task in self._adapter_tasks:
            if task.cancelled():
                continue
            task_error = task.exception()
            if task_error is not None:
                errors.append((task.get_name(), _as_cleanup_exception(task_error)))
        if errors:
            errors.sort(
                key=lambda item: (
                    item[0],
                    type(item[1]).__name__,
                    str(item[1]),
                )
            )
            raise ExceptionGroup(
                "ACP adapter cleanup failed",
                [error for _, error in errors],
            )


__all__ = ["AcpComposition"]
