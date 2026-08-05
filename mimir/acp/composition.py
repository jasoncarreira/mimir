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


def _consume_adapter_task(
    tasks: set[asyncio.Task[Any]],
    task: asyncio.Task[Any],
) -> None:
    tasks.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


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
    _runtime_audit_terminal: bool = field(default=False, init=False, repr=False)

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
            task.add_done_callback(
                lambda done: _consume_adapter_task(adapter_tasks, done)
            )
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

    def mark_runtime_audit_terminal(self) -> None:
        driver = self._runtime_close_driver
        if driver is None or not driver.done():
            raise RuntimeError("runtime close driver is not terminal")
        self._runtime_audit_terminal = True

    def explicit_adapter_tasks(self) -> frozenset[asyncio.Task[Any]]:
        tasks = set(self._adapter_tasks)
        if self._adapter_cleanup_task is not None:
            tasks.add(self._adapter_cleanup_task)
        return frozenset(tasks)

    async def close_adapters(self, timeout: float) -> bool:
        driver = self._runtime_close_driver
        if driver is None or not driver.done():
            raise RuntimeError("runtime close driver is not terminal")
        if not self._runtime_audit_terminal:
            raise RuntimeError("runtime task audit is not terminal")
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
        await self.adapters.channels.disconnect_all()
        while self._adapter_tasks:
            tasks = tuple(self._adapter_tasks)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._adapter_tasks.difference_update(
                task for task in tasks if task.done()
            )
            await asyncio.sleep(0)


__all__ = ["AcpComposition"]
