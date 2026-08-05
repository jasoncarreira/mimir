from __future__ import annotations

import ast
import asyncio
import inspect
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args, get_origin

import pytest

from mimir import runtime


class _Notifier:
    def __init__(self, events: list[tuple[str, Any]] | None = None) -> None:
        self.events = events

    async def notify_operator(self, **kwargs: Any) -> None:
        if self.events is not None:
            self.events.append(("notify_operator", kwargs))

    async def notify_pending_cap_reached(self, **kwargs: Any) -> None:
        if self.events is not None:
            self.events.append(("notify_cap", kwargs))

    async def maybe_reply_dm(self, **kwargs: Any) -> None:
        if self.events is not None:
            self.events.append(("reply_dm", kwargs))


class _Dispatcher:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events
        self._run_turn = None
        self._on_channel_idle = None
        self._on_inject = None
        self._on_event = None
        self._on_pairing_required = None

    def set_run_turn(self, value: Any) -> None:
        self._run_turn = value
        self.events.append(("run_turn", value))

    def set_on_channel_idle(self, value: Any) -> None:
        self._on_channel_idle = value
        self.events.append(("channel_idle", value))

    def set_on_inject(self, value: Any) -> None:
        self._on_inject = value
        self.events.append(("inject", value))

    def set_on_event(self, value: Any) -> None:
        self._on_event = value
        self.events.append(("event", value))

    def set_on_pairing_required(self, value: Any) -> None:
        self._on_pairing_required = value
        self.events.append(("pairing", value))

    def is_channel_busy(self, channel_id: str) -> bool:
        return channel_id == "busy"

    async def enqueue(self, event: Any) -> bool:
        self.events.append(("enqueue", event))
        return True


class _Scheduler:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events
        self._started = False
        self._scheduler = SimpleNamespace(running=False)
        self._arbiter = None

    def set_arbiter(self, value: Any) -> None:
        self._arbiter = value
        self.events.append(("scheduler_arbiter", value))


class _Channels:
    def find(self, channel_id: str) -> None:
        return None

    async def send(self, *args: Any, **kwargs: Any) -> None:
        pass


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        home=tmp_path,
        turns_log=tmp_path / "logs" / "turns.jsonl",
        max_turns_kept=100,
        history_global_max=50,
        history_per_channel_max=25,
        cross_platform_pull=True,
        saga_session_idle_minutes=10,
        saga_session_max_turns=10,
        commitments_log=tmp_path / ".mimir" / "commitments.jsonl",
        operator_alert_channel="",
        pairing_pending_max=100,
        opencode_config_path=None,
        coding_enabled=False,
    )


def _core(tmp_path: Path) -> runtime.CoreServices:
    resolver = SimpleNamespace(
        reload=lambda: 0,
        dm_channel=lambda author, platform: None,
    )
    return runtime.CoreServices(
        identity_resolver=resolver,
        aliases_loaded=0,
        saga_db_path=tmp_path / ".mimir" / "saga.db",
        chat_skill_registry=object(),
    )


def _adapters(events: list[tuple[str, Any]]) -> runtime.RuntimeAdapters:
    def spawn(coroutine: Any, name: str) -> asyncio.Task[Any]:
        return asyncio.create_task(coroutine, name=name)

    return runtime.RuntimeAdapters(
        dispatcher=_Dispatcher(events),
        scheduler=_Scheduler(events),
        channels=_Channels(),
        pairing_notifier=_Notifier(),
        spawn_background_task=spawn,
    )


def _patch_factory(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, Any]],
    *,
    failing_closers: frozenset[str] = frozenset(),
) -> None:
    import mimir.agent
    import mimir.commitments
    import mimir.history
    import mimir.index
    import mimir.saga_client
    import mimir.search
    import mimir.session_manager
    import mimir.subagent_inbox
    import mimir.tools
    import mimir.tools.forge
    import mimir.turn_event_bus
    import mimir.turn_logger

    class TurnLogger:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            events.append(("construct", "turn_logger"))

    class MessageBuffer:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            events.append(("construct", "message_buffer"))

        def replay(self) -> int:
            events.append(("call", "replay"))
            return 7

        def evict_channel(self, channel_id: str) -> bool:
            return True

    class IndexGenerator:
        def __init__(self, home: Path) -> None:
            events.append(("construct", "indexes"))

        def mark_dirty(self, target: str) -> None:
            events.append(("mark_dirty", target))

    class SagaClient:
        async def close(self) -> None:
            events.append(("close", "saga"))
            if "saga" in failing_closers:
                raise ValueError("saga cleanup failed")

    def make_saga_client(*, db_path: Path) -> SagaClient:
        events.append(("construct", ("saga", db_path)))
        return SagaClient()

    class Indexer:
        def __init__(self, home: Path) -> None:
            events.append(("construct", "indexer"))

        async def stop(self) -> None:
            events.append(("close", "indexer"))
            if "indexer" in failing_closers:
                raise RuntimeError("indexer cleanup failed")

    class SessionManager:
        def __init__(self, **kwargs: Any) -> None:
            self.on_idle = None
            self.is_busy = None
            events.append(("construct", "sessions"))

        def set_on_idle(self, value: Any) -> None:
            self.on_idle = value
            events.append(("session_idle", value))

        def set_is_busy(self, value: Any) -> None:
            self.is_busy = value
            events.append(("session_busy", value))

        async def shutdown(self) -> None:
            events.append(("close", "sessions"))
            if "sessions" in failing_closers:
                raise OSError("sessions cleanup failed")

    class SubagentInbox:
        def __init__(self) -> None:
            events.append(("construct", "inbox"))

        def evict_channel(self, channel_id: str) -> bool:
            return True

    class CommitmentsStore:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            events.append(("construct", "commitments"))

        def migrate_ownership(self) -> int:
            events.append(("call", "migrate_ownership"))
            return 3

    class TurnEventBus:
        def __init__(self) -> None:
            events.append(("construct", "turn_event_bus"))

    class Agent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs
            self._arbiter = object()
            self._shell_jobs = object()
            self._handle_shell_job_complete = lambda job: None
            self._memory_client = object()
            mimir.tools.set_memory_client(self._memory_client)
            kwargs["scheduler"].set_arbiter(self._arbiter)
            events.append(("construct", "agent"))

        async def run_turn(self, event: Any) -> None:
            events.append(("run", event))

        async def on_message_injected(self, event: Any) -> None:
            events.append(("injected", event))

    monkeypatch.setattr(mimir.turn_logger, "TurnLogger", TurnLogger)
    monkeypatch.setattr(mimir.history, "MessageBuffer", MessageBuffer)
    monkeypatch.setattr(mimir.index, "IndexGenerator", IndexGenerator)
    monkeypatch.setattr(mimir.saga_client, "make_saga_client", make_saga_client)
    monkeypatch.setattr(mimir.search, "Indexer", Indexer)
    monkeypatch.setattr(mimir.session_manager, "SessionManager", SessionManager)
    monkeypatch.setattr(mimir.subagent_inbox, "SubagentInbox", SubagentInbox)
    monkeypatch.setattr(mimir.commitments, "CommitmentsStore", CommitmentsStore)
    monkeypatch.setattr(mimir.turn_event_bus, "TurnEventBus", TurnEventBus)
    monkeypatch.setattr(mimir.agent, "Agent", Agent)
    monkeypatch.setattr(mimir.tools, "all_mimir_tools", lambda **kwargs: events.append(("tools", kwargs)))
    set_degraded_callback = mimir.tools.forge.set_github_identity_degraded_callback

    def record_degraded_callback(callback: Any, **kwargs: Any) -> None:
        events.append(("forge_callback", callback))
        set_degraded_callback(callback, **kwargs)

    monkeypatch.setattr(
        mimir.tools.forge,
        "set_github_identity_degraded_callback",
        record_degraded_callback,
    )

    def initialize_forge() -> bool:
        mimir.tools.forge.set_forge_client(object())
        events.append(("call", "initialize_forge"))
        return True

    monkeypatch.setattr(
        mimir.tools.forge,
        "initialize_github_forge_identity",
        initialize_forge,
    )


def _assert_runtime_globals_cleared() -> None:
    import mimir.history
    import mimir.tools.extra
    import mimir.tools.forge
    import mimir.tools.mcp
    import mimir.tools.memory
    import mimir.tools.registry
    import mimir.tools.shell_async
    import mimir.tools.web

    assert mimir.tools.forge._github_identity_degraded_callback is None
    assert mimir.tools.forge._default_client is None
    assert mimir.tools.memory._MEMORY_STATE == {"client": None}
    assert mimir.tools.extra._SEARCH_STATE == {"indexer": None}
    assert mimir.tools.extra._INDEX_GEN_STATE == {"generator": None}
    assert mimir.tools.extra._TURN_STATE == {"turns_log_path": None}
    assert mimir.tools.registry._STATE == {
        "channel_registry": None,
        "identity_resolver": None,
        "dispatcher": None,
        "scheduler": None,
        "commitments_store": None,
        "spawn_config": None,
        "arbiter": None,
    }
    assert mimir.history.get_global_buffer() is None
    assert mimir.tools.shell_async._REGISTRY is None
    assert mimir.tools.shell_async._ON_COMPLETE is None
    assert mimir.tools.web._home is None
    assert mimir.tools.mcp.get_mcp_tools() == []


def _assert_runtime_callbacks_cleared(
    adapters: runtime.RuntimeAdapters,
    sessions: Any | None = None,
) -> None:
    assert adapters.dispatcher._run_turn is None
    assert adapters.dispatcher._on_channel_idle is None
    assert adapters.dispatcher._on_inject is None
    assert adapters.dispatcher._on_event is None
    assert adapters.dispatcher._on_pairing_required is None
    if sessions is not None:
        assert sessions.on_idle is None
        assert sessions.is_busy is None
    assert adapters.scheduler._arbiter is None


def test_runtime_public_two_phase_api() -> None:
    assert [field.name for field in fields(runtime.CoreServices)] == [
        "identity_resolver",
        "aliases_loaded",
        "saga_db_path",
        "chat_skill_registry",
    ]
    assert [field.name for field in fields(runtime.RuntimeAdapters)] == [
        "dispatcher",
        "scheduler",
        "channels",
        "pairing_notifier",
        "spawn_background_task",
    ]
    assert [field.name for field in fields(runtime.AgentRuntimeBundle)] == [
        "config",
        "core",
        "adapters",
        "agent",
        "turn_logger",
        "message_buffer",
        "indexes",
        "indexer",
        "saga_client",
        "sessions",
        "subagent_inbox",
        "commitments_store",
        "turn_event_bus",
        "replayed_messages",
        "migrated_commitments",
        "_owned_closers",
        "_runtime_background_tasks",
        "_close_task",
    ]
    assert runtime.CoreServices.__dataclass_params__.frozen is True
    assert runtime.RuntimeAdapters.__dataclass_params__.frozen is True
    assert runtime.AgentRuntimeBundle.__dataclass_params__.frozen is False
    assert "__dict__" not in runtime.CoreServices.__slots__
    assert "__dict__" not in runtime.RuntimeAdapters.__slots__
    assert "__dict__" not in runtime.AgentRuntimeBundle.__slots__

    core = runtime.CoreServices(object(), 2, Path("/tmp/saga.db"), object())
    with pytest.raises(FrozenInstanceError):
        core.aliases_loaded = 3
    adapters = runtime.RuntimeAdapters(object(), object(), object(), _Notifier(), lambda coro, name: None)
    with pytest.raises(FrozenInstanceError):
        adapters.dispatcher = object()

    assert runtime.PairingNotifier._is_protocol is True
    assert str(inspect.signature(runtime.PairingNotifier.notify_operator)) == (
        "(self, *, canonical: 'str', display: 'str', platform: 'str', "
        "channel_id: 'str', delivery: 'str') -> 'None'"
    )
    assert str(inspect.signature(runtime.PairingNotifier.notify_pending_cap_reached)) == (
        "(self, *, platform: 'str', channel_id: 'str', delivery: 'str') -> 'None'"
    )
    assert str(inspect.signature(runtime.PairingNotifier.maybe_reply_dm)) == (
        "(self, *, canonical: 'str', dm_channel_id: 'str') -> 'None'"
    )
    assert get_origin(runtime.BackgroundTaskSpawner).__name__ == "Callable"
    spawner_args = get_args(runtime.BackgroundTaskSpawner)
    assert spawner_args[0][1] is str
    assert get_origin(spawner_args[0][0]).__name__ == "Coroutine"
    assert get_origin(spawner_args[1]) is asyncio.Task

    core_signature = inspect.signature(runtime.create_core_services)
    assert list(core_signature.parameters) == ["config"]
    assert core_signature.parameters["config"].annotation == "Config"
    assert core_signature.return_annotation == "CoreServices"
    factory_signature = inspect.signature(runtime.create_agent_runtime)
    assert list(factory_signature.parameters) == ["config", "core", "adapters"]
    assert [parameter.annotation for parameter in factory_signature.parameters.values()] == [
        "Config",
        "CoreServices",
        "RuntimeAdapters",
    ]
    assert factory_signature.return_annotation == "AgentRuntimeBundle"
    assert inspect.signature(runtime.AgentRuntimeBundle.install_mcp_tools).return_annotation == "None"
    assert inspect.signature(runtime.AgentRuntimeBundle.aclose).return_annotation == "None"
    assert runtime.RUNTIME_RESOURCE_CLOSE_TIMEOUT_SECONDS == 10.0
    source_path = Path(runtime.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert "aiohttp" not in imported
    assert "mimir.server" not in imported
    assert not any(name.endswith(".server") for name in imported)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import mimir.runtime; assert 'aiohttp' not in sys.modules; assert 'mimir.server' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_core_phase_contains_only_adapter_prerequisites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.chat_skills
    import mimir.identities
    from mimir.saga import _config_io

    calls: list[Any] = []

    class Resolver:
        def __init__(self, *, home: Path) -> None:
            calls.append(("resolver", home))

        def reload(self) -> int:
            calls.append("reload")
            return 11

    registry = object()
    previous_saga_config = os.environ.get("SAGA_CONFIG")
    monkeypatch.setenv("SAGA_CONFIG", previous_saga_config or "temporary")
    monkeypatch.delenv("SAGA_CONFIG")
    monkeypatch.setattr(mimir.identities, "IdentityResolver", Resolver)
    monkeypatch.setattr(
        mimir.chat_skills.ChatSkillRegistry,
        "from_config",
        lambda config: calls.append(("registry", config)) or registry,
    )
    monkeypatch.setattr(_config_io, "get_config", lambda: lambda *args: "data/saga.sqlite")
    config = _config(tmp_path)
    (tmp_path / "saga.toml").write_text("[storage]\n", encoding="utf-8")

    core = runtime.create_core_services(config)

    assert calls == [("resolver", tmp_path), "reload", ("registry", config)]
    assert core.aliases_loaded == 11
    assert core.chat_skill_registry is registry
    assert core.saga_db_path == tmp_path / ".mimir" / "data" / "saga.sqlite"
    assert os.environ["SAGA_CONFIG"] == str(tmp_path / "saga.toml")


def test_core_preserves_saga_config_and_db_path_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.chat_skills
    import mimir.identities
    from mimir.saga import _config_io

    class Resolver:
        def __init__(self, *, home: Path) -> None:
            self.home = home

        def reload(self) -> int:
            return 0

    monkeypatch.setattr(mimir.identities, "IdentityResolver", Resolver)
    monkeypatch.setattr(
        mimir.chat_skills.ChatSkillRegistry,
        "from_config",
        lambda config: object(),
    )
    config = _config(tmp_path)
    saga_toml = tmp_path / "saga.toml"
    saga_toml.write_text("[storage]\n", encoding="utf-8")

    missing = object()
    previous = os.environ.pop("SAGA_CONFIG", missing)
    try:
        monkeypatch.setattr(_config_io, "get_config", lambda: lambda *args: "relative.db")
        core = runtime.create_core_services(config)
        assert core.saga_db_path == tmp_path / ".mimir" / "relative.db"
        assert os.environ["SAGA_CONFIG"] == str(saga_toml)

        absolute_path = tmp_path / "elsewhere" / "absolute.db"
        os.environ["SAGA_CONFIG"] = "/already/configured.toml"
        monkeypatch.setattr(_config_io, "get_config", lambda: lambda *args: str(absolute_path))
        core = runtime.create_core_services(config)
        assert core.saga_db_path == absolute_path
        assert os.environ["SAGA_CONFIG"] == "/already/configured.toml"

        saga_toml.unlink()
        os.environ.pop("SAGA_CONFIG")
        monkeypatch.setattr(_config_io, "get_config", lambda: lambda *args: "saga.db")
        core = runtime.create_core_services(config)
        assert core.saga_db_path == tmp_path / ".mimir" / "saga.db"
        assert "SAGA_CONFIG" not in os.environ
    finally:
        if previous is missing:
            os.environ.pop("SAGA_CONFIG", None)
        else:
            os.environ["SAGA_CONFIG"] = previous


@pytest.mark.asyncio
async def test_runtime_enforces_fresh_adapter_preconditions(tmp_path: Path) -> None:
    events: list[tuple[str, Any]] = []
    adapters = _adapters(events)
    adapters.dispatcher._run_turn = object()
    with pytest.raises(ValueError, match="run_turn=None"):
        await runtime.create_agent_runtime(_config(tmp_path), _core(tmp_path), adapters)

    adapters = _adapters(events)
    adapters.dispatcher._on_event = object()
    with pytest.raises(ValueError, match="runtime callbacks"):
        await runtime.create_agent_runtime(_config(tmp_path), _core(tmp_path), adapters)

    adapters = _adapters(events)
    adapters.scheduler._started = True
    with pytest.raises(ValueError, match="must not have been started"):
        await runtime.create_agent_runtime(_config(tmp_path), _core(tmp_path), adapters)

    adapters = _adapters(events)
    adapters.scheduler._scheduler.running = True
    assert adapters.scheduler._started is False
    with pytest.raises(ValueError, match="must not have been started"):
        await runtime.create_agent_runtime(_config(tmp_path), _core(tmp_path), adapters)


@pytest.mark.asyncio
async def test_agent_collaborator_parity_and_final_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []
    _patch_factory(monkeypatch, events)
    adapters = _adapters(events)
    core = _core(tmp_path)

    bundle = await runtime.create_agent_runtime(_config(tmp_path), core, adapters)

    assert bundle.replayed_messages == 7
    assert bundle.migrated_commitments == 3
    assert bundle.agent.args == (
        bundle.config,
        bundle.turn_logger,
        bundle.message_buffer,
        bundle.indexes,
    )
    assert bundle.agent.kwargs == {
        "indexer": bundle.indexer,
        "saga_client": bundle.saga_client,
        "session_manager": bundle.sessions,
        "scheduler": adapters.scheduler,
        "subagent_inbox": bundle.subagent_inbox,
        "channel_registry": adapters.channels,
        "dispatcher": adapters.dispatcher,
        "commitments_store": bundle.commitments_store,
        "turn_event_bus": bundle.turn_event_bus,
        "chat_skill_registry": core.chat_skill_registry,
    }
    assert [value for kind, value in events if kind == "construct"] == [
        "turn_logger",
        "message_buffer",
        "indexes",
        ("saga", core.saga_db_path),
        "indexer",
        "sessions",
        "inbox",
        "commitments",
        "turn_event_bus",
        "agent",
    ]
    assert events[-1][0] == "run_turn"
    assert adapters.dispatcher._run_turn == bundle.agent.run_turn

    await bundle.aclose()


@pytest.mark.asyncio
async def test_runtime_registers_closers_as_resources_are_constructed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.search
    import mimir.session_manager
    import mimir.subagent_inbox

    for failure_point, expected_closes in (
        ("indexer", ["saga"]),
        ("sessions", ["indexer", "saga"]),
        ("inbox", ["sessions", "indexer", "saga"]),
    ):
        events: list[tuple[str, Any]] = []
        with monkeypatch.context() as isolated:
            _patch_factory(isolated, events)
            original = LookupError(f"{failure_point} construction failed")
            if failure_point == "indexer":
                class FailingIndexer:
                    def __init__(self, home: Path) -> None:
                        raise original

                isolated.setattr(mimir.search, "Indexer", FailingIndexer)
            elif failure_point == "sessions":
                class FailingSessions:
                    def __init__(self, **kwargs: Any) -> None:
                        raise original

                isolated.setattr(mimir.session_manager, "SessionManager", FailingSessions)
            else:
                class FailingInbox:
                    def __init__(self) -> None:
                        raise original

                isolated.setattr(mimir.subagent_inbox, "SubagentInbox", FailingInbox)

            with pytest.raises(LookupError) as caught:
                await runtime.create_agent_runtime(
                    _config(tmp_path),
                    _core(tmp_path),
                    _adapters(events),
                )

            assert caught.value is original
            assert [value for kind, value in events if kind == "close"] == expected_closes
            _assert_runtime_globals_cleared()


@pytest.mark.asyncio
async def test_dispatcher_and_session_callback_parity_and_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control
    import mimir.event_logger
    import mimir.identities_populator
    import mimir.models

    events: list[tuple[str, Any]] = []
    _patch_factory(monkeypatch, events)

    class Resolver:
        def __init__(self) -> None:
            self.reload_count = 0

        def reload(self) -> int:
            self.reload_count += 1
            return self.reload_count

        def dm_channel(self, author: str, platform: str) -> None:
            return None

    class Bridge:
        async def resolve_dm_channel(self, author_id: str) -> str:
            events.append(("resolve_dm", author_id))
            return "D-captured"

    class Channels(_Channels):
        def find(self, channel_id: str) -> Bridge:
            events.append(("find", channel_id))
            return Bridge()

    async def direct_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    async def log_event(name: str, **kwargs: Any) -> None:
        events.append(("log_event", (name, kwargs)))

    def capture(*args: Any, **kwargs: Any) -> bool:
        events.append(("capture_dm", (args, kwargs)))
        return True

    def request(*args: Any, **kwargs: Any) -> str:
        events.append(("request_pairing", (args, kwargs)))
        return "changed"

    monkeypatch.setattr(runtime.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(mimir.event_logger, "log_event", log_event)
    monkeypatch.setattr(mimir.identities_populator, "capture_dm_channel", capture)
    monkeypatch.setattr(mimir.identities_populator, "request_pairing_status", request)
    monkeypatch.setattr(
        mimir.access_control,
        "builtin_trigger_service_principal",
        lambda name, home: (name, home),
    )
    monkeypatch.setattr(
        mimir.models,
        "AgentEvent",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    resolver = Resolver()
    core = runtime.CoreServices(
        identity_resolver=resolver,
        aliases_loaded=0,
        saga_db_path=tmp_path / ".mimir" / "saga.db",
        chat_skill_registry=object(),
    )
    dispatcher = _Dispatcher(events)
    scheduler = _Scheduler(events)
    notifier = _Notifier(events)
    adapters = runtime.RuntimeAdapters(
        dispatcher=dispatcher,
        scheduler=scheduler,
        channels=Channels(),
        pairing_notifier=notifier,
        spawn_background_task=lambda coroutine, name: asyncio.create_task(coroutine, name=name),
    )
    bundle = await runtime.create_agent_runtime(_config(tmp_path), core, adapters)

    bindings = [
        kind
        for kind, value in events
        if value is not None
        and kind in {"channel_idle", "inject", "event", "pairing", "session_idle", "session_busy"}
    ]
    assert bindings == [
        "channel_idle",
        "inject",
        "event",
        "pairing",
        "session_idle",
        "session_busy",
    ]
    assert events[-1] == ("run_turn", bundle.agent.run_turn)

    assert dispatcher._on_channel_idle("channel-1") == (True, True)
    injected_event = object()
    await dispatcher._on_inject(injected_event)
    assert ("injected", injected_event) in events

    inbound = SimpleNamespace(
        author="alice",
        author_id="U123",
        author_display="Alice",
        source="slack",
        channel_id="dm-alice",
    )
    await dispatcher._on_event(inbound)
    assert ("resolve_dm", "U123") in events
    assert any(kind == "capture_dm" for kind, _ in events)
    assert resolver.reload_count == 1

    decision = SimpleNamespace(canonical_author="alice-canonical", denial_reason="unknown")
    await dispatcher._on_pairing_required(inbound, decision)
    assert any(kind == "request_pairing" for kind, _ in events)
    assert ("notify_operator", {
        "canonical": "alice-canonical",
        "display": "Alice",
        "platform": "slack",
        "channel_id": "dm-alice",
        "delivery": "dm",
    }) in events
    assert ("reply_dm", {
        "canonical": "alice-canonical",
        "dm_channel_id": "dm-alice",
    }) in events
    assert resolver.reload_count == 2

    session = SimpleNamespace(
        channel_id="session-channel",
        saga_session_id="saga-session",
        source_acl={"operator"},
        ifc_state=SimpleNamespace(current=lambda: frozenset({"private"})),
    )
    await bundle.sessions.on_idle(session)
    queued = [value for kind, value in events if kind == "enqueue"][-1]
    assert queued.trigger == "saga_session_end"
    assert queued.channel_id == "session-channel"
    assert queued.extra == {"saga_session_id": "saga-session"}
    assert queued.source_session_acl == {"operator"}
    assert queued.ifc_labels == frozenset({"private"})
    assert bundle.sessions.is_busy("busy") is True
    assert bundle.sessions.is_busy("idle") is False

    await bundle.aclose()


@pytest.mark.asyncio
async def test_runtime_global_matrix_is_installed_and_reset_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.history
    import mimir.tools
    import mimir.tools.extra
    import mimir.tools.forge
    import mimir.tools.mcp
    import mimir.tools.memory
    import mimir.tools.registry
    import mimir.tools.shell_async
    import mimir.tools.web

    events: list[tuple[str, Any]] = []
    _patch_factory(monkeypatch, events)
    stale = object()
    mimir.tools.forge.set_github_identity_degraded_callback(stale)
    mimir.tools.forge.set_forge_client(stale)
    mimir.tools.set_memory_client(stale)
    mimir.tools.set_indexer(stale)
    mimir.tools.set_index_generator(stale)
    mimir.tools.set_turns_log_path(Path("/stale"))
    mimir.tools.set_channel_registry(stale)
    mimir.tools.set_identity_resolver(stale)
    mimir.tools.set_dispatcher(stale)
    mimir.tools.set_scheduler(stale)
    mimir.tools.set_arbiter(stale)
    mimir.history.set_global_buffer(stale)
    mimir.tools.set_commitments_store(stale)
    mimir.tools.set_spawn_config(stale)
    mimir.tools.set_shell_job_registry(stale, on_complete=stale)
    mimir.tools.web.set_home(Path("/stale"))
    mimir.tools.set_mcp_tools([stale])
    events.clear()

    calls: list[tuple[str, Any]] = []

    def wrap(module: Any, attribute: str, label: str) -> None:
        original = getattr(module, attribute)

        def recorder(value: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append((label, (value, args, kwargs) if args or kwargs else value))
            return original(value, *args, **kwargs)

        monkeypatch.setattr(module, attribute, recorder)

    wrap(mimir.tools.forge, "set_github_identity_degraded_callback", "forge_callback")
    wrap(mimir.tools.forge, "set_forge_client", "forge_client")
    wrap(mimir.tools, "set_memory_client", "memory")
    wrap(mimir.tools, "set_indexer", "indexer")
    wrap(mimir.tools, "set_index_generator", "indexes")
    wrap(mimir.tools, "set_turns_log_path", "turns")
    wrap(mimir.tools, "set_channel_registry", "channels")
    wrap(mimir.tools, "set_identity_resolver", "resolver")
    wrap(mimir.tools, "set_dispatcher", "dispatcher")
    wrap(mimir.tools, "set_scheduler", "scheduler")
    wrap(mimir.tools, "set_arbiter", "arbiter")
    wrap(mimir.history, "set_global_buffer", "buffer")
    wrap(mimir.tools, "set_commitments_store", "commitments")
    wrap(mimir.tools, "set_spawn_config", "spawn")
    wrap(mimir.tools, "set_shell_job_registry", "shell")
    wrap(mimir.tools.web, "set_home", "web")
    wrap(mimir.tools, "set_mcp_tools", "mcp")

    reset_labels = [
        "forge_callback",
        "forge_client",
        "memory",
        "indexer",
        "indexes",
        "turns",
        "channels",
        "resolver",
        "dispatcher",
        "scheduler",
        "arbiter",
        "buffer",
        "commitments",
        "spawn",
        "shell",
        "web",
        "mcp",
    ]
    config = _config(tmp_path)
    config.coding_enabled = True
    adapters = _adapters(events)
    core = _core(tmp_path)
    bundle = await runtime.create_agent_runtime(config, core, adapters)

    assert [label for label, _ in calls[:17]] == reset_labels
    assert all(value is None for _, value in calls[:14])
    assert calls[14] == ("shell", None)
    assert calls[15] == ("web", None)
    assert calls[16] == ("mcp", [])
    install_labels = [label for label, _ in calls[17:]]
    assert install_labels == [
        "forge_callback",
        "forge_client",
        "memory",
        "indexer",
        "indexes",
        "turns",
        "channels",
        "resolver",
        "dispatcher",
        "scheduler",
        "arbiter",
        "buffer",
        "commitments",
        "spawn",
        "shell",
        "web",
        "mcp",
    ]

    assert mimir.tools.forge._github_identity_degraded_callback is not None
    assert mimir.tools.forge._default_client is not None
    assert mimir.tools.memory._MEMORY_STATE["client"] is bundle.agent._memory_client
    assert mimir.tools.extra._SEARCH_STATE["indexer"] is bundle.indexer
    assert mimir.tools.extra._INDEX_GEN_STATE["generator"] is bundle.indexes
    assert mimir.tools.extra._TURN_STATE["turns_log_path"] == config.turns_log
    assert mimir.tools.registry._STATE == {
        "channel_registry": adapters.channels,
        "identity_resolver": core.identity_resolver,
        "dispatcher": adapters.dispatcher,
        "scheduler": adapters.scheduler,
        "commitments_store": bundle.commitments_store,
        "spawn_config": {
            "default_cwd": config.home,
            "opencode_config_path": config.opencode_config_path,
        },
        "arbiter": bundle.agent._arbiter,
    }
    assert adapters.scheduler._arbiter is bundle.agent._arbiter
    assert mimir.history.get_global_buffer() is bundle.message_buffer
    assert mimir.tools.shell_async._REGISTRY is bundle.agent._shell_jobs
    assert mimir.tools.shell_async._ON_COMPLETE == bundle.agent._handle_shell_job_complete
    assert mimir.tools.web._home == config.home
    assert mimir.tools.mcp.get_mcp_tools() == []

    mcp_tools = [object(), object()]
    bundle.install_mcp_tools(mcp_tools)
    assert mimir.tools.mcp.get_mcp_tools() == mcp_tools
    calls_before_close = len(calls)
    await bundle.aclose()

    close_calls = calls[calls_before_close:]
    assert [label for label, _ in close_calls] == reset_labels
    assert all(value is None for _, value in close_calls[:14])
    assert close_calls[14] == ("shell", None)
    assert close_calls[15] == ("web", None)
    assert close_calls[16] == ("mcp", [])
    _assert_runtime_globals_cleared()


@pytest.mark.asyncio
async def test_bundle_aclose_is_idempotent_and_reverses_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []
    _patch_factory(monkeypatch, events)
    adapters = _adapters(events)
    bundle = await runtime.create_agent_runtime(
        _config(tmp_path),
        _core(tmp_path),
        adapters,
    )

    await asyncio.gather(bundle.aclose(), bundle.aclose(), bundle.aclose())
    await bundle.aclose()

    assert [value for kind, value in events if kind == "close"] == [
        "sessions",
        "indexer",
        "saga",
    ]
    assert adapters.dispatcher._run_turn is None
    assert adapters.dispatcher._on_channel_idle is None
    assert adapters.dispatcher._on_inject is None
    assert adapters.dispatcher._on_event is None
    assert adapters.dispatcher._on_pairing_required is None
    assert bundle.sessions.on_idle is None
    assert bundle.sessions.is_busy is None
    assert adapters.scheduler._arbiter is None


@pytest.mark.asyncio
async def test_bundle_does_not_close_entrypoint_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []
    _patch_factory(monkeypatch, events)
    lifecycle_calls: list[str] = []

    class Dispatcher(_Dispatcher):
        async def aclose(self) -> None:
            lifecycle_calls.append("dispatcher")

    class Scheduler(_Scheduler):
        async def stop(self) -> None:
            lifecycle_calls.append("scheduler")

    class Channels(_Channels):
        async def disconnect_all(self) -> None:
            lifecycle_calls.append("channels")

    class Notifier(_Notifier):
        async def aclose(self) -> None:
            lifecycle_calls.append("notifier")

    adapters = runtime.RuntimeAdapters(
        dispatcher=Dispatcher(events),
        scheduler=Scheduler(events),
        channels=Channels(),
        pairing_notifier=Notifier(),
        spawn_background_task=lambda coroutine, name: asyncio.create_task(coroutine, name=name),
    )
    bundle = await runtime.create_agent_runtime(_config(tmp_path), _core(tmp_path), adapters)

    assert bundle.adapters is adapters
    await bundle.aclose()

    assert lifecycle_calls == []
    _assert_runtime_callbacks_cleared(adapters, bundle.sessions)


@pytest.mark.asyncio
async def test_bundle_aclose_continues_and_aggregates_cleanup_failures() -> None:
    events: list[str] = []

    async def saga_close() -> None:
        events.append("saga")
        raise ValueError("saga failed")

    async def indexer_close() -> None:
        events.append("indexer")
        raise RuntimeError("indexer failed")

    async def sessions_close() -> None:
        events.append("sessions")

    adapters = _adapters([])
    sessions = SimpleNamespace(
        set_on_idle=lambda value: None,
        set_is_busy=lambda value: None,
    )
    bundle = SimpleNamespace(
        adapters=adapters,
        sessions=sessions,
        _runtime_background_tasks=set(),
        _owned_closers=[
            ("saga client", saga_close),
            ("indexer", indexer_close),
            ("sessions", sessions_close),
        ],
    )

    with pytest.raises(ExceptionGroup, match="agent runtime cleanup failed") as caught:
        await runtime._close_bundle(bundle)

    assert events == ["sessions", "indexer", "saga"]
    assert [type(exc) for exc in caught.value.exceptions] == [RuntimeError, ValueError]


@pytest.mark.asyncio
async def test_bundle_aclose_times_out_each_resource_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    release = asyncio.Event()

    async def resistant(name: str) -> None:
        events.append(name)
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    adapters = _adapters([])
    sessions = SimpleNamespace(
        set_on_idle=lambda value: None,
        set_is_busy=lambda value: None,
    )
    bundle = SimpleNamespace(
        adapters=adapters,
        sessions=sessions,
        _runtime_background_tasks=set(),
        _owned_closers=[
            ("saga client", lambda: resistant("saga")),
            ("indexer", lambda: resistant("indexer")),
            ("sessions", lambda: resistant("sessions")),
        ],
    )
    monkeypatch.setattr(runtime, "RUNTIME_RESOURCE_CLOSE_TIMEOUT_SECONDS", 0.01)

    try:
        with pytest.raises(ExceptionGroup) as caught:
            await runtime._close_bundle(bundle)
        assert events == ["sessions", "indexer", "saga"]
        assert len(caught.value.exceptions) == 3
        assert all(isinstance(exc, TimeoutError) for exc in caught.value.exceptions)
        assert all("0.01 seconds" in str(exc) for exc in caught.value.exceptions)
    finally:
        release.set()
        await asyncio.sleep(0)


def test_bundle_aclose_total_owned_wait_budget_is_thirty_five_seconds() -> None:
    from mimir.background_tasks import BACKGROUND_TASK_CANCEL_TIMEOUT_SECONDS

    assert BACKGROUND_TASK_CANCEL_TIMEOUT_SECONDS == 5.0
    assert runtime.RUNTIME_RESOURCE_CLOSE_TIMEOUT_SECONDS == 10.0
    assert (
        BACKGROUND_TASK_CANCEL_TIMEOUT_SECONDS
        + 3 * runtime.RUNTIME_RESOURCE_CLOSE_TIMEOUT_SECONDS
    ) == 35.0


@pytest.mark.asyncio
async def test_aclose_task_timeout_continues_reverse_closers_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.background_tasks

    factory_events: list[tuple[str, Any]] = []
    _patch_factory(monkeypatch, factory_events)
    bundle = await runtime.create_agent_runtime(
        _config(tmp_path),
        _core(tmp_path),
        _adapters(factory_events),
    )
    events: list[str] = []
    release = asyncio.Event()
    task_started = asyncio.Event()

    async def resistant_task() -> None:
        task_started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            events.append("runtime task cancelled")
            await release.wait()

    async def resistant_closer(name: str) -> None:
        events.append(name)
        try:
            await release.wait()
        except asyncio.CancelledError:
            events.append(f"{name} cancelled")
            await release.wait()

    async def failing_indexer() -> None:
        events.append("indexer")
        raise RuntimeError("indexer close failed")

    background_task = asyncio.create_task(resistant_task(), name="resistant-runtime-task")
    bundle._runtime_background_tasks.add(background_task)
    await task_started.wait()
    bundle._owned_closers = [
        ("saga client", lambda: resistant_closer("saga")),
        ("indexer", failing_indexer),
        ("sessions", lambda: resistant_closer("sessions")),
    ]
    monkeypatch.setattr(
        mimir.background_tasks,
        "BACKGROUND_TASK_CANCEL_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(runtime, "RUNTIME_RESOURCE_CLOSE_TIMEOUT_SECONDS", 0.01)

    try:
        results = await asyncio.gather(
            bundle.aclose(),
            bundle.aclose(),
            bundle.aclose(),
            return_exceptions=True,
        )
        assert all(isinstance(result, ExceptionGroup) for result in results)
        assert results[0] is results[1] is results[2]
        group = results[0]
        assert isinstance(group, ExceptionGroup)
        with pytest.raises(ExceptionGroup) as later:
            await bundle.aclose()
        assert later.value is group
        assert [type(exc) for exc in group.exceptions] == [
            TimeoutError,
            TimeoutError,
            RuntimeError,
            TimeoutError,
        ]
        assert "resistant-runtime-task" in str(group.exceptions[0])
        assert "sessions did not close within 0.01 seconds" in str(group.exceptions[1])
        assert str(group.exceptions[2]) == "indexer close failed"
        assert "saga client did not close within 0.01 seconds" in str(group.exceptions[3])
        assert events == [
            "runtime task cancelled",
            "sessions",
            "sessions cancelled",
            "indexer",
            "saga",
            "saga cancelled",
        ]
        assert bundle._runtime_background_tasks == set()
    finally:
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_factory_rollback_closes_registered_resources_and_reraises_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []
    _patch_factory(monkeypatch, events)
    import mimir.commitments

    original = LookupError("commitments unavailable")

    class FailingCommitmentsStore:
        def __init__(self, **kwargs: Any) -> None:
            raise original

    monkeypatch.setattr(mimir.commitments, "CommitmentsStore", FailingCommitmentsStore)
    adapters = _adapters(events)

    with pytest.raises(LookupError) as caught:
        await runtime.create_agent_runtime(
            _config(tmp_path),
            _core(tmp_path),
            adapters,
        )

    assert caught.value is original
    assert [value for kind, value in events if kind == "close"] == [
        "sessions",
        "indexer",
        "saga",
    ]
    assert adapters.dispatcher._run_turn is None


@pytest.mark.asyncio
async def test_factory_rollback_task_timeout_continues_reverse_closers_and_reraises_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.background_tasks
    import mimir.event_logger
    import mimir.saga_client
    import mimir.search
    import mimir.session_manager
    import mimir.tools
    import mimir.tools.forge

    events: list[tuple[str, Any]] = []
    _patch_factory(monkeypatch, events)
    release = asyncio.Event()
    started = asyncio.Event()

    async def resistant_runtime_task() -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            events.append(("cancelled", "runtime task"))
            await release.wait()

    runtime_task = asyncio.create_task(
        resistant_runtime_task(),
        name="factory-resistant-task",
    )
    await started.wait()

    async def resistant_close(name: str) -> None:
        events.append(("close", name))
        try:
            await release.wait()
        except asyncio.CancelledError:
            events.append(("cancelled", name))
            await release.wait()

    class SagaClient:
        async def close(self) -> None:
            await resistant_close("saga")

    monkeypatch.setattr(
        mimir.saga_client,
        "make_saga_client",
        lambda *, db_path: SagaClient(),
    )
    base_indexer = mimir.search.Indexer

    class FailingIndexer(base_indexer):
        async def stop(self) -> None:
            events.append(("close", "indexer"))
            raise RuntimeError("indexer rollback failed")

    monkeypatch.setattr(mimir.search, "Indexer", FailingIndexer)
    base_sessions = mimir.session_manager.SessionManager
    sessions_instances: list[Any] = []

    class ResistantSessions(base_sessions):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            sessions_instances.append(self)

        async def shutdown(self) -> None:
            await resistant_close("sessions")

    monkeypatch.setattr(mimir.session_manager, "SessionManager", ResistantSessions)
    set_callback = mimir.tools.forge.set_github_identity_degraded_callback

    def invoke_current(callback: Any, *, notify_current: bool = False) -> None:
        set_callback(callback)
        if callback is not None and notify_current:
            callback(RuntimeError("forge degraded"))

    monkeypatch.setattr(
        mimir.tools.forge,
        "set_github_identity_degraded_callback",
        invoke_current,
    )
    monkeypatch.setattr(mimir.event_logger, "log_event_sync", lambda *args, **kwargs: None)

    async def log_event(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(mimir.event_logger, "log_event", log_event)
    original = LookupError("global publication failed")
    set_scheduler = mimir.tools.set_scheduler
    failed = False

    def fail_scheduler(value: Any) -> None:
        nonlocal failed
        set_scheduler(value)
        if value is not None and not failed:
            failed = True
            raise original

    monkeypatch.setattr(mimir.tools, "set_scheduler", fail_scheduler)
    monkeypatch.setattr(
        mimir.background_tasks,
        "BACKGROUND_TASK_CANCEL_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(runtime, "RUNTIME_RESOURCE_CLOSE_TIMEOUT_SECONDS", 0.01)
    config = _config(tmp_path)
    config.operator_alert_channel = "ops"
    adapters = _adapters(events)

    def spawn(coroutine: Any, name: str) -> asyncio.Task[Any]:
        coroutine.close()
        runtime_task.set_name(name)
        return runtime_task

    adapters = runtime.RuntimeAdapters(
        dispatcher=adapters.dispatcher,
        scheduler=adapters.scheduler,
        channels=adapters.channels,
        pairing_notifier=adapters.pairing_notifier,
        spawn_background_task=spawn,
    )

    try:
        with pytest.raises(LookupError) as caught:
            await runtime.create_agent_runtime(config, _core(tmp_path), adapters)
        assert caught.value is original
        assert [value for kind, value in events if kind == "close"] == [
            "sessions",
            "indexer",
            "saga",
        ]
        notes = getattr(caught.value, "__notes__", [])
        assert len(notes) == 1
        assert "rollback had 4 cleanup failure(s)" in notes[0]
        assert "factory-resistant-task" in notes[0] or "github-identity-degraded-alert" in notes[0]
        assert "sessions did not close within 0.01 seconds" in notes[0]
        assert "RuntimeError: indexer rollback failed" in notes[0]
        assert "saga client did not close within 0.01 seconds" in notes[0]
        _assert_runtime_callbacks_cleared(adapters, sessions_instances[0])
        _assert_runtime_globals_cleared()
    finally:
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage",
    ["post_agent", "dispatcher_callback", "session_callback", "global_install"],
)
async def test_intermediate_construction_and_wiring_failures_are_fully_compensated(
    failure_stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.session_manager
    import mimir.tools

    events: list[tuple[str, Any]] = []
    _patch_factory(monkeypatch, events, failing_closers=frozenset({"indexer"}))
    adapters = _adapters(events)
    sessions_instances: list[Any] = []
    base_sessions = mimir.session_manager.SessionManager
    original = LookupError(f"{failure_stage} failed")

    class CapturingSessions(base_sessions):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            sessions_instances.append(self)
            self.failed_busy_binding = False

        def set_is_busy(self, value: Any) -> None:
            super().set_is_busy(value)
            if (
                failure_stage == "session_callback"
                and value is not None
                and not self.failed_busy_binding
            ):
                self.failed_busy_binding = True
                raise original

    monkeypatch.setattr(mimir.session_manager, "SessionManager", CapturingSessions)

    if failure_stage == "post_agent":
        def fail_bundle(**kwargs: Any) -> None:
            raise original

        monkeypatch.setattr(runtime, "AgentRuntimeBundle", fail_bundle)
    elif failure_stage == "dispatcher_callback":
        set_on_event = adapters.dispatcher.set_on_event
        failed = False

        def fail_event_binding(value: Any) -> None:
            nonlocal failed
            set_on_event(value)
            if value is not None and not failed:
                failed = True
                raise original

        adapters.dispatcher.set_on_event = fail_event_binding
    elif failure_stage == "global_install":
        set_scheduler = mimir.tools.set_scheduler
        failed = False

        def fail_scheduler_install(value: Any) -> None:
            nonlocal failed
            set_scheduler(value)
            if value is not None and not failed:
                failed = True
                raise original

        monkeypatch.setattr(mimir.tools, "set_scheduler", fail_scheduler_install)

    with pytest.raises(LookupError) as caught:
        await runtime.create_agent_runtime(
            _config(tmp_path),
            _core(tmp_path),
            adapters,
        )

    assert caught.value is original
    assert [value for kind, value in events if kind == "close"] == [
        "sessions",
        "indexer",
        "saga",
    ]
    notes = getattr(caught.value, "__notes__", [])
    assert len(notes) == 1
    assert "rollback had 1 cleanup failure(s)" in notes[0]
    assert "RuntimeError: indexer cleanup failed" in notes[0]
    _assert_runtime_callbacks_cleared(adapters, sessions_instances[0])
    _assert_runtime_globals_cleared()


@pytest.mark.asyncio
async def test_non_web_entrypoint_builds_and_closes_real_agent_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "anthropic:test")
    from mimir.channel_registry import ChannelRegistry
    from mimir.config import Config
    from mimir.dispatcher import Dispatcher
    from mimir.scheduler import Scheduler
    from mimir.saga import _config_io

    _config_io.reload_config()
    config = Config.from_env()
    core = runtime.create_core_services(config)
    dispatcher = Dispatcher(config, resolver=core.identity_resolver)
    scheduler = Scheduler(
        config.home / "scheduler.yaml",
        dispatcher.enqueue,
        home=config.home,
        scheduler_tz=config.scheduler_tz,
    )
    owner_tasks: set[asyncio.Task[Any]] = set()

    def spawn(coroutine: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        owner_tasks.add(task)
        task.add_done_callback(owner_tasks.discard)
        return task

    adapters = runtime.RuntimeAdapters(
        dispatcher=dispatcher,
        scheduler=scheduler,
        channels=ChannelRegistry(),
        pairing_notifier=_Notifier(),
        spawn_background_task=spawn,
    )

    bundle = await runtime.create_agent_runtime(config, core, adapters)
    assert bundle.agent.__class__.__name__ == "Agent"
    assert dispatcher._run_turn == bundle.agent.run_turn
    assert "mimir.server" not in sys.modules

    await bundle.aclose()
    assert dispatcher._run_turn is None
    assert scheduler._arbiter is None
    assert not owner_tasks
