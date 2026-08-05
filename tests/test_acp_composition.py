from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mimir.acp import composition


class _FakeBundle:
    def __init__(self) -> None:
        self.close_calls = 0
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.allow_close.wait()


class _FakeChannels:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    async def disconnect_all(self) -> None:
        self.disconnect_calls += 1


def _composition_with_bundle(
    bundle: _FakeBundle,
    channels: _FakeChannels,
    adapter_tasks: set[asyncio.Task[Any]] | None = None,
) -> composition.AcpComposition:
    adapters = SimpleNamespace(channels=channels)
    return composition.AcpComposition(
        config=SimpleNamespace(),
        core=SimpleNamespace(),
        adapters=adapters,
        bundle=bundle,
        _adapter_tasks=adapter_tasks if adapter_tasks is not None else set(),
    )


@pytest.mark.asyncio
async def test_create_uses_two_phase_runtime_with_empty_unstarted_adapters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[Any] = []
    config = SimpleNamespace(home=tmp_path, scheduler_tz="UTC")
    core = SimpleNamespace(identity_resolver=object())
    bundle = object()

    class FakeConfig:
        @classmethod
        def from_env(cls) -> Any:
            calls.append("config")
            return config

    class FakeDispatcher:
        def __init__(self, received_config: Any, *, resolver: Any) -> None:
            calls.append(("dispatcher", received_config, resolver))
            self._workers: dict[str, Any] = {}

        async def enqueue(self, event: Any) -> bool:
            raise AssertionError("dispatcher worker started")

    class FakeScheduler:
        def __init__(
            self,
            scheduler_yaml: Path,
            enqueue: Any,
            *,
            home: Path,
            scheduler_tz: str,
        ) -> None:
            calls.append(("scheduler", scheduler_yaml, enqueue, home, scheduler_tz))
            self._started = False

    class FakeChannels:
        def __init__(self) -> None:
            calls.append("channels")
            self._bridges: list[Any] = []

    def fake_create_core(received_config: Any) -> Any:
        calls.append(("core", received_config))
        return core

    async def fake_create_runtime(
        received_config: Any,
        received_core: Any,
        adapters: Any,
    ) -> Any:
        calls.append(("runtime", received_config, received_core, adapters))
        return bundle

    monkeypatch.setattr(composition, "Config", FakeConfig)
    monkeypatch.setattr(composition, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(composition, "Scheduler", FakeScheduler)
    monkeypatch.setattr(composition, "ChannelRegistry", FakeChannels)
    monkeypatch.setattr(composition, "create_core_services", fake_create_core)
    monkeypatch.setattr(composition, "create_agent_runtime", fake_create_runtime)

    server_was_loaded = "mimir.server" in sys.modules
    result = await composition.AcpComposition.create()

    assert result.config is config
    assert result.core is core
    assert result.bundle is bundle
    assert result.adapters.channels._bridges == []
    assert result.adapters.dispatcher._workers == {}
    assert result.adapters.scheduler._started is False
    assert isinstance(result.adapters.pairing_notifier, composition._AcpPairingNotifier)
    assert [entry if isinstance(entry, str) else entry[0] for entry in calls] == [
        "config",
        "core",
        "dispatcher",
        "scheduler",
        "channels",
        "runtime",
    ]
    assert calls[-1][1:3] == (config, core)
    assert calls[-1][3] is result.adapters
    assert ("mimir.server" in sys.modules) is server_was_loaded


@pytest.mark.asyncio
async def test_pairing_notifier_is_async_no_op() -> None:
    notifier = composition._AcpPairingNotifier()

    assert await notifier.notify_operator(
        canonical="operator",
        display="Operator",
        platform="acp",
        channel_id="acp:stdio",
        delivery="private",
    ) is None
    assert await notifier.notify_pending_cap_reached(
        platform="acp",
        channel_id="acp:stdio",
        delivery="private",
    ) is None
    assert await notifier.maybe_reply_dm(
        canonical="operator",
        dm_channel_id="acp:stdio",
    ) is None


@pytest.mark.asyncio
async def test_runtime_close_driver_is_single_and_audit_gates_adapters() -> None:
    bundle = _FakeBundle()
    channels = _FakeChannels()
    instance = _composition_with_bundle(bundle, channels)

    driver = instance.start_runtime_close()
    assert instance.start_runtime_close() is driver
    assert driver.get_name() == "acp-runtime-close-driver"
    await bundle.close_started.wait()

    with pytest.raises(RuntimeError, match="runtime close driver"):
        instance.mark_runtime_audit_terminal()
    with pytest.raises(RuntimeError, match="runtime close driver"):
        await instance.close_adapters(0)

    bundle.allow_close.set()
    await driver
    assert bundle.close_calls == 1

    with pytest.raises(RuntimeError, match="runtime task audit"):
        await instance.close_adapters(0)

    instance.mark_runtime_audit_terminal()
    assert await instance.close_adapters(1)
    assert channels.disconnect_calls == 1
    assert await instance.close_adapters(1)
    assert channels.disconnect_calls == 1


@pytest.mark.asyncio
async def test_adapter_cleanup_retains_and_awaits_cancellation_resistant_tasks() -> None:
    bundle = _FakeBundle()
    bundle.allow_close.set()
    channels = _FakeChannels()
    adapter_tasks: set[asyncio.Task[Any]] = set()
    cancelled = asyncio.Event()
    allow_terminal = asyncio.Event()

    async def resistant_adapter() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await allow_terminal.wait()

    adapter_task = asyncio.create_task(resistant_adapter(), name="acp-adapter-owned")
    adapter_tasks.add(adapter_task)
    instance = _composition_with_bundle(bundle, channels, adapter_tasks)
    driver = instance.start_runtime_close()
    await driver
    instance.mark_runtime_audit_terminal()

    assert not await instance.close_adapters(0)
    await cancelled.wait()
    explicit = instance.explicit_adapter_tasks()
    cleanup_tasks = {task for task in explicit if task.get_name() == "acp-adapter-cleanup"}
    assert adapter_task in explicit
    assert len(cleanup_tasks) == 1
    cleanup_task = next(iter(cleanup_tasks))
    assert not cleanup_task.done()

    allow_terminal.set()
    assert await instance.close_adapters(1)
    assert adapter_task.done()
    assert cleanup_task.done()
    assert cleanup_task in instance.explicit_adapter_tasks()
    assert channels.disconnect_calls == 1


@pytest.mark.asyncio
async def test_create_spawner_tracks_only_pending_adapter_tasks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(home=tmp_path, scheduler_tz="UTC")
    core = SimpleNamespace(identity_resolver=object())

    class FakeConfig:
        @classmethod
        def from_env(cls) -> Any:
            return config

    class FakeDispatcher:
        def __init__(self, received_config: Any, *, resolver: Any) -> None:
            pass

        async def enqueue(self, event: Any) -> bool:
            return False

    class FakeScheduler:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class FakeChannels:
        async def disconnect_all(self) -> None:
            pass

    bundle = _FakeBundle()

    async def fake_create_runtime(config: Any, core: Any, adapters: Any) -> Any:
        return bundle

    monkeypatch.setattr(composition, "Config", FakeConfig)
    monkeypatch.setattr(composition, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(composition, "Scheduler", FakeScheduler)
    monkeypatch.setattr(composition, "ChannelRegistry", FakeChannels)
    monkeypatch.setattr(composition, "create_core_services", lambda config: core)
    monkeypatch.setattr(composition, "create_agent_runtime", fake_create_runtime)
    instance = await composition.AcpComposition.create()
    allow_terminal = asyncio.Event()

    async def adapter_work() -> None:
        await allow_terminal.wait()

    task = instance.adapters.spawn_background_task(adapter_work(), "acp-owned")
    assert instance.explicit_adapter_tasks() == frozenset({task})

    allow_terminal.set()
    await task
    await asyncio.sleep(0)
    assert instance.explicit_adapter_tasks() == frozenset()

    bundle.allow_close.set()
    await instance.start_runtime_close()
    instance.mark_runtime_audit_terminal()
    assert await instance.close_adapters(1)
