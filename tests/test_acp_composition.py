from __future__ import annotations

import asyncio
import importlib
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def composition_module() -> Any:
    return importlib.import_module("mimir.acp.composition")


class _FakeBundle:
    def __init__(self) -> None:
        self.close_calls = 0
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.allow_close.wait()


class _ShieldedFakeBundle:
    def __init__(self) -> None:
        self.close_calls = 0
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.cleanup_task: asyncio.Task[None] | None = None

    async def _cleanup(self) -> None:
        self.close_started.set()
        await self.allow_close.wait()

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(
                self._cleanup(),
                name="fake-shielded-bundle-cleanup",
            )
        await asyncio.shield(self.cleanup_task)


class _FakeChannels:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    async def disconnect_all(self) -> None:
        self.disconnect_calls += 1


def _composition_with_bundle(
    composition: Any,
    bundle: Any,
    channels: _FakeChannels,
    adapter_tasks: set[asyncio.Task[Any]] | None = None,
) -> Any:
    adapters = SimpleNamespace(channels=channels)
    return composition.AcpComposition(
        config=SimpleNamespace(),
        core=SimpleNamespace(),
        adapters=adapters,
        bundle=bundle,
        _adapter_tasks=adapter_tasks if adapter_tasks is not None else set(),
    )


async def _successful_audit_task(
    name: str = "acp-runtime-task-audit",
) -> asyncio.Task[None]:
    task = asyncio.create_task(asyncio.sleep(0), name=name)
    await task
    return task


def test_composition_import_does_not_import_forbidden_runtime_facilities() -> None:
    forbidden = (
        "mimir.server",
        "mimir.mcp_client",
        "mimir.bridges.bench",
        "mimir.bridges.discord",
        "mimir.bridges.slack",
        "mimir.bridges.web_chat",
    )
    script = f"""
import importlib
import sys

forbidden = {forbidden!r}
assert not [name for name in forbidden if name in sys.modules]
importlib.import_module("mimir.acp.composition")
loaded = [name for name in forbidden if name in sys.modules]
assert not loaded, loaded
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_create_uses_only_empty_unstarted_runtime_adapters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    composition_module: Any,
) -> None:
    composition = composition_module
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

        async def drain(self, *, timeout: float | None = None) -> None:
            raise AssertionError("dispatcher worker lifecycle started")

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

        def start(self) -> None:
            raise AssertionError("scheduler worker started")

        def shutdown(self) -> None:
            raise AssertionError("scheduler worker lifecycle started")

    class FakeChannels:
        def __init__(self) -> None:
            calls.append("channels")
            self._bridges: list[Any] = []

        def register(self, bridge: Any) -> None:
            raise AssertionError("bridge registered")

        async def connect_all(self) -> None:
            raise AssertionError("bridge started")

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

    async def forbidden_async(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network facility started")

    def forbidden_sync(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network facility started")

    monkeypatch.setattr(composition, "Config", FakeConfig)
    monkeypatch.setattr(composition, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(composition, "Scheduler", FakeScheduler)
    monkeypatch.setattr(composition, "ChannelRegistry", FakeChannels)
    monkeypatch.setattr(composition, "create_core_services", fake_create_core)
    monkeypatch.setattr(composition, "create_agent_runtime", fake_create_runtime)
    monkeypatch.setattr(asyncio, "start_server", forbidden_async)
    monkeypatch.setattr(asyncio, "open_connection", forbidden_async)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_async)
    monkeypatch.setattr(socket, "create_connection", forbidden_sync)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "create_server", forbidden_async)
    monkeypatch.setattr(loop, "create_connection", forbidden_async)
    tasks_before = asyncio.all_tasks()

    result = await composition.AcpComposition.create()

    assert result.config is config
    assert result.core is core
    assert result.bundle is bundle
    assert result.adapters.channels._bridges == []
    assert result.adapters.dispatcher._workers == {}
    assert result.adapters.scheduler._started is False
    assert isinstance(result.adapters.pairing_notifier, composition._AcpPairingNotifier)
    assert asyncio.all_tasks() == tasks_before
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
    assert "mimir.server" not in sys.modules
    assert "mimir.mcp_client" not in sys.modules


@pytest.mark.asyncio
async def test_pairing_notifier_is_async_no_op(composition_module: Any) -> None:
    notifier = composition_module._AcpPairingNotifier()

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
async def test_runtime_close_driver_and_successful_audit_gate_adapters(
    composition_module: Any,
) -> None:
    bundle = _FakeBundle()
    channels = _FakeChannels()
    instance = _composition_with_bundle(composition_module, bundle, channels)
    successful_audit = await _successful_audit_task()

    driver = instance.start_runtime_close()
    assert instance.start_runtime_close() is driver
    assert driver.get_name() == "acp-runtime-close-driver"
    await bundle.close_started.wait()
    done, pending = await asyncio.wait({driver}, timeout=0)
    assert not done
    assert pending == {driver}
    with pytest.raises(RuntimeError, match="runtime close driver"):
        await instance.close_adapters(
            0,
            runtime_audit_task=successful_audit,
        )
    assert channels.disconnect_calls == 0

    bundle.allow_close.set()
    await driver
    assert bundle.close_calls == 1

    pending_audit = asyncio.create_task(asyncio.sleep(60), name="pending-audit")
    with pytest.raises(RuntimeError, match="not terminal"):
        await instance.close_adapters(0, runtime_audit_task=pending_audit)
    pending_audit.cancel()
    await asyncio.gather(pending_audit, return_exceptions=True)

    cancelled_audit = asyncio.create_task(asyncio.sleep(60), name="cancelled-audit")
    cancelled_audit.cancel()
    await asyncio.gather(cancelled_audit, return_exceptions=True)
    with pytest.raises(RuntimeError, match="audit was cancelled"):
        await instance.close_adapters(0, runtime_audit_task=cancelled_audit)

    audit_error = LookupError("audit failed")

    async def fail_audit() -> None:
        raise audit_error

    failed_audit = asyncio.create_task(fail_audit(), name="failed-audit")
    await asyncio.gather(failed_audit, return_exceptions=True)
    with pytest.raises(RuntimeError, match="audit failed") as caught:
        await instance.close_adapters(0, runtime_audit_task=failed_audit)
    assert caught.value.__cause__ is audit_error

    assert await instance.close_adapters(
        1,
        runtime_audit_task=successful_audit,
    )
    assert channels.disconnect_calls == 1
    other_audit = await _successful_audit_task("other-audit")
    with pytest.raises(RuntimeError, match="does not match"):
        await instance.close_adapters(1, runtime_audit_task=other_audit)


@pytest.mark.asyncio
async def test_cancelled_runtime_driver_never_allows_adapter_cleanup(
    composition_module: Any,
) -> None:
    bundle = _ShieldedFakeBundle()
    channels = _FakeChannels()
    instance = _composition_with_bundle(composition_module, bundle, channels)
    driver = instance.start_runtime_close()
    await bundle.close_started.wait()
    assert bundle.cleanup_task is not None
    assert not bundle.cleanup_task.done()

    driver.cancel()
    await asyncio.gather(driver, return_exceptions=True)
    assert driver.cancelled()
    assert not bundle.cleanup_task.done()
    assert instance.start_runtime_close() is driver
    audit_task = await _successful_audit_task()

    with pytest.raises(RuntimeError, match="driver was cancelled"):
        await instance.close_adapters(0, runtime_audit_task=audit_task)
    assert channels.disconnect_calls == 0
    assert not bundle.cleanup_task.done()

    bundle.allow_close.set()
    await bundle.cleanup_task


@pytest.mark.asyncio
async def test_adapter_cleanup_retains_cancellation_resistant_tasks(
    composition_module: Any,
) -> None:
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
    instance = _composition_with_bundle(
        composition_module,
        bundle,
        channels,
        adapter_tasks,
    )
    await instance.start_runtime_close()
    audit_task = await _successful_audit_task()

    assert not await instance.close_adapters(0, runtime_audit_task=audit_task)
    await cancelled.wait()
    explicit = instance.explicit_adapter_tasks()
    cleanup_tasks = {task for task in explicit if task.get_name() == "acp-adapter-cleanup"}
    assert adapter_task in explicit
    assert len(cleanup_tasks) == 1
    cleanup_task = next(iter(cleanup_tasks))
    assert not cleanup_task.done()

    allow_terminal.set()
    assert await instance.close_adapters(1, runtime_audit_task=audit_task)
    assert adapter_task.done()
    assert cleanup_task.done()
    assert cleanup_task in instance.explicit_adapter_tasks()
    assert channels.disconnect_calls == 1


@pytest.mark.asyncio
async def test_adapter_failures_are_retained_and_reported_deterministically(
    composition_module: Any,
) -> None:
    bundle = _FakeBundle()
    bundle.allow_close.set()
    channels = _FakeChannels()
    preexisting_error = LookupError("failed before cleanup")
    cleanup_error = ValueError("failed during cleanup")
    preexisting_started = asyncio.Event()
    cleanup_started = asyncio.Event()

    async def fail_before_cleanup() -> None:
        preexisting_started.set()
        raise preexisting_error

    async def fail_during_cleanup() -> None:
        cleanup_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise cleanup_error

    preexisting_task = asyncio.create_task(
        fail_before_cleanup(),
        name="z-prefailed-adapter",
    )
    cleanup_task = asyncio.create_task(
        fail_during_cleanup(),
        name="a-cleanup-failure",
    )
    await preexisting_started.wait()
    await cleanup_started.wait()
    await asyncio.sleep(0)
    assert preexisting_task.done()
    adapter_tasks = {preexisting_task, cleanup_task}
    instance = _composition_with_bundle(
        composition_module,
        bundle,
        channels,
        adapter_tasks,
    )
    await instance.start_runtime_close()
    audit_task = await _successful_audit_task()

    with pytest.raises(ExceptionGroup, match="ACP adapter cleanup failed") as caught:
        await instance.close_adapters(1, runtime_audit_task=audit_task)

    assert caught.value.exceptions == (cleanup_error, preexisting_error)
    assert preexisting_task in instance._adapter_tasks
    assert cleanup_task in instance._adapter_tasks
    assert preexisting_task.done()
    assert cleanup_task.done()
    assert channels.disconnect_calls == 1


@pytest.mark.asyncio
async def test_create_spawner_keeps_completed_tasks_until_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    composition_module: Any,
) -> None:
    composition = composition_module
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
    assert instance.explicit_adapter_tasks() == frozenset()
    assert task in instance._adapter_tasks

    bundle.allow_close.set()
    await instance.start_runtime_close()
    audit_task = await _successful_audit_task()
    assert await instance.close_adapters(1, runtime_audit_task=audit_task)
