from __future__ import annotations

import ast
import asyncio
import inspect
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mimir import runtime


class _Notifier:
    async def notify_operator(self, **kwargs: Any) -> None:
        pass

    async def notify_pending_cap_reached(self, **kwargs: Any) -> None:
        pass

    async def maybe_reply_dm(self, **kwargs: Any) -> None:
        pass


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


def _patch_factory(monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, Any]]) -> None:
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

    def make_saga_client(*, db_path: Path) -> SagaClient:
        events.append(("construct", ("saga", db_path)))
        return SagaClient()

    class Indexer:
        def __init__(self, home: Path) -> None:
            events.append(("construct", "indexer"))

        async def stop(self) -> None:
            events.append(("close", "indexer"))

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
            kwargs["scheduler"].set_arbiter(self._arbiter)
            events.append(("construct", "agent"))

        async def run_turn(self, event: Any) -> None:
            pass

        async def on_message_injected(self, event: Any) -> None:
            pass

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
    monkeypatch.setattr(
        mimir.tools.forge,
        "set_github_identity_degraded_callback",
        lambda callback, **kwargs: events.append(("forge_callback", callback)),
    )
    monkeypatch.setattr(
        mimir.tools.forge,
        "initialize_github_forge_identity",
        lambda: True,
    )


def test_runtime_public_two_phase_api() -> None:
    assert inspect.signature(runtime.create_core_services).return_annotation == "CoreServices"
    assert list(inspect.signature(runtime.create_agent_runtime).parameters) == [
        "config",
        "core",
        "adapters",
    ]
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
