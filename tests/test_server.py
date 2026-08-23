"""Dedicated coverage for mimir/server.py (chainlink #247, slice 3/5).

Pins behaviour of the components that aren't covered by the three
existing focused files (test_server_auth_warning, test_server_bind_security,
test_server_consolidate):

- ``_safe_str_eq``         — constant-time string comparison helper
- ``_make_auth_middleware`` — key-header gate + exempt-route bypass
- ``_AUTH_EXEMPT``         — correct set membership
- ``_MaskApiKeyInAccessLog`` — access-log filter redaction
- ``_handle_health``       — liveness endpoint
- ``_handle_event``        — event injection endpoint (valid + error paths)

All tests exercise these units without standing up the full ``build_app``
wiring (dispatcher, scheduler, saga, bridges) — each test builds a
minimal ``aiohttp.web.Application`` with only the routes and state
needed to prove the behaviour under test.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir.server import (
    _AUTH_EXEMPT,
    _AUTH_EXEMPT_PREFIXES,
    _MaskApiKeyInAccessLog,
    _is_auth_exempt,
    _make_auth_middleware,
    _safe_str_eq,
    _start_mcp_servers,
    _handle_health,
    _handle_event,
    _handle_root,
    build_app,
    reattach_inflight_worklink_runs,
)


def test_server_startup_routes_factory_recovery_to_run_epic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The expected argv below is the DEFAULT run_bin. server.py resolves it as
    # shlex.split(os.environ.get("WORKLINK_RUN_BIN") or "mimir"), and the live
    # deployment sets WORKLINK_RUN_BIN="uv run mimir", so without pinning it here
    # this test asserts ambient state it does not own: green in CI, red in every
    # Worklink sandbox that inherits the container env.
    monkeypatch.setenv("WORKLINK_RUN_BIN", "mimir")
    import mimir.worklink.control as control
    import mimir.worklink.factory_state as factory_state

    record = SimpleNamespace(
        issue_id=700,
        controller_phase="running",
        status=SimpleNamespace(is_terminal=False),
    )
    monkeypatch.setenv("WORKLINK_REPO", "/workspace/mimir")
    monkeypatch.setattr(control, "reconcile_run_states", lambda *args, **kwargs: [])
    monkeypatch.setattr(factory_state, "list_factory_records", lambda home: [record])
    monkeypatch.setattr(factory_state, "factory_process_is_verified_dead", lambda value: True)
    spawned: list[list[str]] = []

    dispatched = reattach_inflight_worklink_runs(
        tmp_path,
        popen=lambda argv, **kwargs: spawned.append(list(argv)) or object(),
    )

    assert dispatched == [700]
    assert spawned[0][:4] == ["mimir", "worklink", "run-epic", "700"]


def _production_call_sites(call_name: str) -> set[str]:
    root = Path(__file__).resolve().parent.parent
    sites: set[str] = set()
    for path in (root / "mimir").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id == call_name:
                sites.add(path.relative_to(root).as_posix())
            elif isinstance(function, ast.Attribute) and function.attr == call_name:
                sites.add(path.relative_to(root).as_posix())
    return sites


def test_runtime_is_only_production_agent_constructor() -> None:
    assert _production_call_sites("Agent") == {"mimir/runtime.py"}


def test_runtime_is_only_production_global_buffer_writer() -> None:
    assert _production_call_sites("set_global_buffer") == {"mimir/runtime.py"}


def test_runtime_is_only_production_mcp_tools_writer() -> None:
    assert _production_call_sites("set_mcp_tools") == {"mimir/runtime.py"}


def _writer_call_sites(writer_names: set[str]) -> dict[str, set[str]]:
    root = Path(__file__).resolve().parent.parent
    sites = {name: set() for name in writer_names}
    for path in (root / "mimir").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        module_name = relative.removesuffix(".py").replace("/", ".")
        package_parts = module_name.split(".")[:-1]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases: dict[str, str] = {}
        local_writers = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in writer_names
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = package_parts[: len(package_parts) - node.level + 1]
                    base = ".".join([*prefix, *(node.module or "").split(".")]).rstrip(".")
                else:
                    base = node.module or ""
                for alias in node.names:
                    aliases[alias.asname or alias.name] = ".".join(
                        part for part in (base, alias.name) if part
                    )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            writer: str | None = None
            if isinstance(node.func, ast.Name):
                imported = aliases.get(node.func.id, "")
                if imported.rsplit(".", 1)[-1] in writer_names:
                    writer = imported.rsplit(".", 1)[-1]
                elif node.func.id in local_writers:
                    writer = node.func.id
            elif isinstance(node.func, ast.Attribute):
                parts: list[str] = []
                value: ast.expr = node.func
                while isinstance(value, ast.Attribute):
                    parts.append(value.attr)
                    value = value.value
                if isinstance(value, ast.Name):
                    base = aliases.get(value.id)
                    if base is not None:
                        resolved = ".".join([base, *reversed(parts)])
                        candidate = resolved.rsplit(".", 1)[-1]
                        if candidate in writer_names and resolved.startswith(
                            ("mimir.tools", "mimir.history", "mimir.event_logger")
                        ):
                            writer = candidate
            if writer is not None:
                sites[writer].add(relative)
    return sites


def test_production_global_writers_are_confined() -> None:
    root = Path(__file__).resolve().parent.parent
    expected = {
        "init_logger": {
            "mimir/server.py",
            "mimir/commands/worklink.py",
            "mimir/wiki_backlinks.py",
        },
        "set_github_identity_degraded_callback": {"mimir/runtime.py"},
        "set_forge_client": {"mimir/runtime.py", "mimir/tools/forge.py"},
        "set_memory_client": {"mimir/agent.py", "mimir/runtime.py"},
        "set_indexer": {"mimir/runtime.py"},
        "set_index_generator": {"mimir/runtime.py"},
        "set_turns_log_path": {"mimir/runtime.py"},
        "set_channel_registry": {"mimir/runtime.py"},
        "set_identity_resolver": {"mimir/runtime.py"},
        "set_dispatcher": {"mimir/runtime.py"},
        "set_scheduler": {"mimir/runtime.py"},
        "set_arbiter": {"mimir/runtime.py"},
        "set_global_buffer": {"mimir/runtime.py"},
        "set_commitments_store": {"mimir/runtime.py"},
        "set_spawn_config": {"mimir/runtime.py"},
        "set_shell_job_registry": {"mimir/runtime.py"},
        "set_home": {"mimir/runtime.py"},
        "set_mcp_tools": {"mimir/runtime.py"},
    }
    assert _writer_call_sites(set(expected)) == expected

    saga_config_writers = {
        path.relative_to(root).as_posix()
        for path in (root / "mimir").rglob("*.py")
        if 'os.environ["SAGA_CONFIG"] =' in path.read_text(encoding="utf-8")
    }
    assert saga_config_writers == {"mimir/runtime.py", "mimir/reindex.py"}

    server_source = (root / "mimir" / "server.py").read_text(encoding="utf-8")
    assert 'os.environ["MIMIR_WORKLINK_AGENT_ID"] = worklink_agent_id' in server_source
    assert "_access_log.addFilter(_MaskApiKeyInAccessLog())" in server_source

    server_tree = ast.parse(
        server_source
    )
    module_assignments = [
        node
        for node in server_tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    assert not any(
        target.id == "_STARTUP_BACKGROUND_TASKS"
        for node in module_assignments
        for target in (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
        )
        if isinstance(target, ast.Name)
    )


# ──────────────────────────────────────────────────────────────────────────────
# MCP production startup wiring
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_mcp_servers_returns_tools_and_policy_attention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from mimir.mcp_client import MCPProvenance, MCPServerConfig

    config = MCPServerConfig(
        name="demo",
        command="demo-server",
        args=[],
        server_config_id="demo-production",
        policy_version="policy-v2",
    )
    provenance = replace(
        MCPProvenance.create(
            config,
            "read_item",
            {},
            server_config_id=config.server_config_id,
        ),
        classification="resource_scoped",
        adapter_name="demo-owner",
        adapter_version="adapter-v1",
        approval_version="approval-v1",
        policy_version="policy-v1",
    )
    tool = SimpleNamespace(name="mcp_demo_read_item", mcp_provenance=provenance)
    manager = MagicMock()
    manager.start_servers = AsyncMock(return_value=[tool])
    manager.shutdown = AsyncMock()
    events: list[tuple[str, dict[str, Any]]] = []

    async def capture(kind: str, **fields: Any) -> None:
        events.append((kind, fields))

    monkeypatch.setattr("mimir.server.log_event", capture)
    returned_manager, tools = await _start_mcp_servers(manager, [config])

    manager.start_servers.assert_awaited_once_with([config])
    assert tools == [tool]
    assert returned_manager is manager
    assert events[0] == (
        "mcp_servers_ready",
        {"count": 1, "tool_names": ["mcp_demo_read_item"]},
    )
    attention = next(fields for kind, fields in events if kind == "mcp_policy_attention_required")
    assert attention["count"] >= 1
    assert any(issue.get("actual_policy") == "policy-v1" for issue in attention["issues"])


@pytest.mark.asyncio
async def test_start_mcp_servers_failure_shuts_down_and_returns_no_manager() -> None:
    manager = MagicMock()
    manager.start_servers = AsyncMock(side_effect=RuntimeError("start failed"))
    manager.shutdown = AsyncMock()

    returned_manager, tools = await _start_mcp_servers(manager, [object()])

    assert returned_manager is None
    assert tools == []
    manager.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_mcp_servers_retains_manager_when_failure_shutdown_fails() -> None:
    manager = MagicMock()
    manager.start_servers = AsyncMock(side_effect=RuntimeError("start failed"))
    manager.shutdown = AsyncMock(side_effect=RuntimeError("shutdown failed"))

    returned_manager, tools = await _start_mcp_servers(manager, [object()])

    assert returned_manager is manager
    assert tools == []


@pytest.mark.asyncio
async def test_start_mcp_servers_emits_operator_event_for_skipped_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MagicMock()
    manager.start_servers = AsyncMock(return_value=[])
    manager.startup_failures = [{
        "server_config_id": "broken-id",
        "server_name": "broken",
        "error": "binary not found",
    }]
    events: list[tuple[str, dict[str, Any]]] = []

    async def capture(kind: str, **fields: Any) -> None:
        events.append((kind, fields))

    monkeypatch.setattr("mimir.server.log_event", capture)
    returned_manager, tools = await _start_mcp_servers(
        manager, [SimpleNamespace(policy_version="")]
    )

    assert returned_manager is manager
    assert tools == []
    assert events == [("mcp_server_start_failed", manager.startup_failures[0])]


def test_runtime_field_proxies_delegate_and_fail_closed() -> None:
    from types import SimpleNamespace

    from mimir.server import _RuntimeFieldProxy, _RuntimeSlot

    slot = _RuntimeSlot()
    proxy = _RuntimeFieldProxy(slot, "turn_event_bus")
    with pytest.raises(RuntimeError, match="agent runtime is not initialized"):
        proxy.subscribe("channel")

    bus = SimpleNamespace(subscribe=lambda channel: f"queue:{channel}")
    slot.bundle = SimpleNamespace(turn_event_bus=bus)
    assert proxy.subscribe("channel") == "queue:channel"


@pytest.mark.asyncio
async def test_pairing_notifier_aclose_is_idempotent_and_clears_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from mimir.server import _PairingNotifier

    monkeypatch.setattr("mimir.server.log_event", AsyncMock())
    channels = MagicMock()
    channels.send = AsyncMock()
    config = SimpleNamespace(
        operator_alert_channel="ops",
        pairing_operator_digest_delay_seconds=60.0,
        pairing_dm_auto_reply_enabled=True,
        pairing_dm_auto_reply_interval_seconds=60.0,
        pairing_dm_auto_reply_text="pending",
        pairing_pending_max=100,
    )
    notifier = _PairingNotifier(config, channels)
    await notifier.notify_operator(
        canonical="alice",
        display="Alice",
        platform="slack",
        channel_id="dm-alice",
        delivery="dm",
    )
    await notifier.maybe_reply_dm(canonical="alice", dm_channel_id="dm-alice")
    await asyncio.sleep(0)
    operator_task = notifier._operator_task
    dm_task = notifier._dm_reply_task

    await notifier.aclose()
    await notifier.aclose()

    assert notifier._operator_task is None
    assert notifier._dm_reply_task is None
    assert notifier._operator_pending == []
    assert notifier._dm_reply_queue.empty()
    assert operator_task is not None and operator_task.cancelled()
    assert dm_task is not None and dm_task.done()


@dataclass
class _ServerControl:
    events: list[str] = field(default_factory=list)
    failures: dict[str, BaseException] = field(default_factory=dict)
    runtime_failure: BaseException | None = None
    mcp_enabled: bool = False
    mcp_returns_manager: bool = True
    panel_enabled: bool = False
    optional_bridges: bool = False
    scheduler_should_start: bool = True
    bundle: Any | None = None
    app: web.Application | None = None
    route_kwargs: dict[str, Any] = field(default_factory=dict)
    core: Any | None = None
    dispatcher: Any | None = None
    scheduler: Any | None = None
    channels: Any | None = None
    web_chat: Any | None = None
    mcp_manager: Any | None = None
    panel: Any | None = None

    def hit(self, name: str) -> None:
        self.events.append(name)
        failure = self.failures.get(name)
        if failure is not None:
            raise failure


def _controlled_server_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: _ServerControl | None = None,
) -> tuple[web.Application, _ServerControl]:
    import mimir.background_tasks
    import mimir.doc_seed
    import mimir.liveness
    import mimir.mcp_client
    import mimir.runtime
    import mimir.skill_install
    import mimir.tools
    import mimir.update_on_start
    import mimir.worklink.autonomy
    from mimir.config import Config
    from mimir.history import set_global_buffer

    control = control or _ServerControl()
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("MIMIR_API_KEY", "test-key")
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "anthropic:test")
    monkeypatch.setenv("MIMIR_GIT_TRACKING_ENABLED", "false")
    monkeypatch.setenv("MIMIR_LIVENESS_BEAT_SECONDS", "0")
    monkeypatch.setenv("MIMIR_SOURCE_REPO", str(tmp_path / "missing-source"))
    monkeypatch.setenv("DISCORD_TOKEN", "")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("SLACK_APP_TOKEN", "")

    class Resolver:
        def resolve_web_key(self, key: str) -> None:
            return None

        def has_web_keys(self) -> bool:
            return False

    resolver = Resolver()
    chat_skills = object()
    core = SimpleNamespace(
        identity_resolver=resolver,
        aliases_loaded=4,
        saga_db_path=tmp_path / ".mimir" / "saga.db",
        chat_skill_registry=chat_skills,
    )
    control.core = core

    def create_core_services(config: Any) -> Any:
        control.hit("core")
        return core

    class Dispatcher:
        def __init__(self, config: Any, run_turn: Any = None, *, resolver: Any = None) -> None:
            control.hit("dispatcher")
            self.config = config
            self.resolver = resolver
            self._run_turn = run_turn
            self._on_channel_idle = None
            self._on_inject = None
            self._on_event = None
            self._on_pairing_required = None
            control.dispatcher = self

        def set_run_turn(self, run_turn: Any) -> None:
            self._run_turn = run_turn
            control.events.append("dispatcher:bound" if run_turn is not None else "dispatcher:unbound")

        async def enqueue(self, event: Any) -> bool:
            control.events.append(f"enqueue:{self._run_turn is not None}")
            return self._run_turn is not None

        async def drain(self, *, timeout: float) -> None:
            control.hit("dispatcher:drain")

        def is_channel_busy(self, channel_id: str) -> bool:
            return False

    class Scheduler:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            control.hit("scheduler")
            self._started = False
            self._scheduler = SimpleNamespace(running=False)
            self._arbiter = None
            control.scheduler = self

        def __getattr__(self, name: str) -> Any:
            if not name.startswith("add_"):
                raise AttributeError(name)

            def add(*args: Any, **kwargs: Any) -> Any:
                control.hit(f"scheduler:{name}")
                if name == "add_saga_consolidate_job":
                    return control.scheduler_should_start
                if name == "add_poller_jobs":
                    return 0
                return False

            return add

        def reload(self) -> dict[str, int]:
            control.hit("scheduler:reload")
            return {"registered": 0, "invalid": 0}

        def start(self) -> None:
            self._started = True
            self._scheduler.running = True
            control.hit("scheduler:start")

        async def stop(self) -> None:
            self._started = False
            self._scheduler.running = False
            control.hit("scheduler:stop")

        def set_arbiter(self, arbiter: Any) -> None:
            self._arbiter = arbiter

    class Channels:
        def __init__(self) -> None:
            self._bridges: list[Any] = []
            control.channels = self

        def register(self, bridge: Any) -> None:
            self._bridges.append(bridge)
            control.events.append(f"register:{type(bridge).__name__}")

        def bridges(self) -> list[Any]:
            return list(self._bridges)

        async def connect_all(self) -> None:
            control.events.append(f"bridges:bound:{control.dispatcher._run_turn is not None}")
            control.hit("bridges:connect")

        async def disconnect_all(self) -> None:
            control.hit("bridges:disconnect")

        async def send(self, *args: Any, **kwargs: Any) -> None:
            control.hit("channels:send")

        def find(self, channel_id: str) -> None:
            return None

    class BenchBridge:
        def __init__(self, *, home: Path) -> None:
            self.home = home
            control.hit("bench")

    class WebChatBridge:
        def __init__(self, *, enqueue: Any, home: Path, chat_skill_registry: Any) -> None:
            self.enqueue = enqueue
            self.home = home
            self.chat_skill_registry = chat_skill_registry
            control.web_chat = self
            control.hit("webchat")

        def register_routes(self, app: web.Application) -> None:
            control.hit("webchat:routes")

            async def ok(request: web.Request) -> web.Response:
                return web.json_response({"ok": True})

            app.router.add_post("/chat", ok)
            app.router.add_get("/chat/stream", ok)

    class DiscordBridge:
        def __init__(self, **kwargs: Any) -> None:
            control.hit("discord")

    class SlackBridge:
        def __init__(self, **kwargs: Any) -> None:
            control.hit("slack")

    class Indexer:
        async def start(self, **kwargs: Any) -> None:
            control.hit("indexer:start")

        async def sweep(self) -> None:
            control.hit("indexer:sweep")

    class Agent:
        def __init__(self) -> None:
            self._commitments = object()
            self._rate_limits = object()

        async def run_turn(self, event: Any) -> None:
            return None

    class Bundle:
        def __init__(self, adapters: Any) -> None:
            self.agent = Agent()
            self.turn_logger = object()
            self.message_buffer = object()
            self.indexes = object()
            self.indexer = Indexer()
            self.saga_client = object()
            self.sessions = object()
            self.commitments_store = SimpleNamespace(list=lambda *args, **kwargs: [])
            self.turn_event_bus = SimpleNamespace(
                subscribe=lambda channel: asyncio.Queue(),
                unsubscribe=lambda channel, queue: None,
            )
            self.replayed_messages = 9
            self.adapters = adapters
            self.installed_tools: list[Any] = []
            self.closed = False

        def install_mcp_tools(self, tools: list[Any]) -> None:
            self.installed_tools = list(tools)
            control.hit("bundle:mcp")

        async def aclose(self) -> None:
            if self.closed:
                return
            self.closed = True
            self.adapters.dispatcher.set_run_turn(None)
            set_global_buffer(None)
            control.hit("bundle:close")

    async def create_agent_runtime(config: Any, core_arg: Any, adapters: Any) -> Any:
        control.hit("runtime")
        if control.runtime_failure is not None:
            raise control.runtime_failure
        bundle = Bundle(adapters)
        control.bundle = bundle
        adapters.dispatcher.set_run_turn(bundle.agent.run_turn)
        set_global_buffer(bundle.message_buffer)
        return bundle

    class ActivityPanel:
        def __init__(self, bus: Any, channels: Any, channel_ids: Any) -> None:
            self.bus = bus
            self.channels = channels
            self.channel_ids = channel_ids
            control.panel = self
            control.hit("panel:construct")

        def start(self) -> None:
            control.hit("panel:start")

        async def stop(self) -> None:
            control.hit("panel:stop")

    class MCPManager:
        def __init__(self, **kwargs: Any) -> None:
            control.mcp_manager = self
            control.hit("mcp:construct")

        async def shutdown(self) -> None:
            control.hit("mcp:shutdown")

    class MCPPolicyStore:
        def __init__(self, path: Path) -> None:
            self.path = path

        def load_server_configs(self) -> list[Any]:
            return []

    async def start_mcp(manager: Any, configs: list[Any], **kwargs: Any) -> tuple[Any | None, list[Any]]:
        assert control.app is not None
        assert control.app["startup_state"].mcp_manager is manager
        control.hit("mcp:start")
        if control.mcp_returns_manager:
            return manager, ["mcp-tool"]
        return None, []

    async def event(kind: str, **fields: Any) -> None:
        control.hit(f"log:{kind}")

    def register_routes(app: web.Application, **kwargs: Any) -> None:
        control.route_kwargs = kwargs
        control.hit("webui:routes")

        async def ok(request: web.Request) -> web.Response:
            return web.json_response({"ok": True})

        app.router.add_get("/api/v1/turn-events", ok)

    async def cancel_tasks(tasks: set[asyncio.Task[Any]], *, label: str) -> list[BaseException]:
        control.hit(f"cancel:{label}")
        return await mimir.background_tasks.cancel_background_tasks(tasks, label=label)

    monkeypatch.setattr("mimir.server.init_logger", lambda *args, **kwargs: control.hit("logger"))
    monkeypatch.setattr("mimir.server.seed_subagent_defs", lambda home: {})
    monkeypatch.setattr("mimir.server.migrate_legacy_skills_dir", lambda home: None)
    monkeypatch.setattr("mimir.server.refresh_builtin_skills", lambda home: {})
    monkeypatch.setattr("mimir.server.seed_prompts", lambda home: None)
    monkeypatch.setattr("mimir.server.seed_scheduler", lambda home: None)
    monkeypatch.setattr("mimir.server.ensure_chainlink_initialized", lambda home: None)
    monkeypatch.setattr("mimir.server.Dispatcher", Dispatcher)
    monkeypatch.setattr("mimir.server.Scheduler", Scheduler)
    monkeypatch.setattr("mimir.server.ChannelRegistry", Channels)
    monkeypatch.setattr("mimir.server.BenchBridge", BenchBridge)
    monkeypatch.setattr("mimir.server.WebChatBridge", WebChatBridge)
    monkeypatch.setattr("mimir.server.log_event", event)
    monkeypatch.setattr("mimir.server.repo_binding_startup_alerts", lambda: [])
    monkeypatch.setattr("mimir.server.reattach_inflight_worklink_runs", lambda home: [])
    monkeypatch.setattr("mimir.server.cancel_background_tasks", cancel_tasks)
    monkeypatch.setattr("mimir.server._start_mcp_servers", start_mcp)
    monkeypatch.setattr("mimir.server.web_ui.register_routes", register_routes)
    monkeypatch.setattr(mimir.runtime, "create_core_services", create_core_services)
    monkeypatch.setattr(mimir.runtime, "create_agent_runtime", create_agent_runtime)
    monkeypatch.setattr(mimir.tools, "all_mimir_tools", lambda **kwargs: control.hit("preflight"))
    monkeypatch.setattr(mimir.doc_seed, "refresh_docs", lambda home: {})
    monkeypatch.setattr(
        mimir.skill_install,
        "auto_update_installed_optional_skills",
        lambda home: SimpleNamespace(any_updates=False),
    )
    monkeypatch.setattr(mimir.update_on_start, "consume_startup_events", AsyncMock(return_value=0))
    monkeypatch.setattr(mimir.update_on_start, "consume_update_digest", AsyncMock(return_value=0))
    monkeypatch.setattr(mimir.update_on_start, "emit_version_bump_digest", AsyncMock(return_value=False))
    monkeypatch.setattr(mimir.liveness, "detect_unclean_restart", lambda home: None)
    monkeypatch.setattr(
        mimir.liveness,
        "mark_session_running",
        lambda *args, **kwargs: control.hit("liveness:running"),
    )
    monkeypatch.setattr(
        mimir.liveness,
        "mark_clean_shutdown",
        lambda home: control.hit("liveness:clean"),
    )
    monkeypatch.setattr(
        mimir.worklink.autonomy,
        "release_claims_for_graceful_shutdown",
        lambda *args, **kwargs: control.hit("claims:release") or [],
    )
    monkeypatch.setattr(mimir.mcp_client, "MCPManager", MCPManager)
    monkeypatch.setattr(mimir.mcp_client, "MCPPolicyStore", MCPPolicyStore)
    monkeypatch.setitem(
        sys.modules,
        "mimir.bridges.discord",
        SimpleNamespace(DiscordBridge=DiscordBridge),
    )
    monkeypatch.setitem(
        sys.modules,
        "mimir.bridges.slack",
        SimpleNamespace(SlackBridge=SlackBridge),
    )
    monkeypatch.setattr("mimir.bridges._activity_panel.ActivityPanel", ActivityPanel)

    config = Config.from_env()
    config = replace(
        config,
        home=tmp_path,
        api_key="test-key",
        git_tracking_enabled=False,
        liveness_beat_seconds=0,
        activity_panel_channels=("ops",) if control.panel_enabled else (),
        mcp_servers=[SimpleNamespace(policy_version="", server_config_id="server")]
        if control.mcp_enabled
        else [],
        discord_token="discord" if control.optional_bridges else "",
        slack_bot_token="slack-bot" if control.optional_bridges else "",
        slack_app_token="slack-app" if control.optional_bridges else "",
        oauth_credentials_path=None,
        commitments_due_check_cron="",
        minimax_usage_poll_cron="",
    )
    set_global_buffer(None)
    app = build_app(config)
    control.app = app
    pairing_notifier = app["pairing_notifier"]
    original_pairing_close = pairing_notifier.aclose

    async def close_pairing() -> None:
        control.hit("pairing:close")
        await original_pairing_close()

    pairing_notifier.aclose = close_pairing
    return app, control


async def _run_startup(app: web.Application) -> None:
    hooks = [
        hook
        for hook in app.on_startup
        if getattr(hook, "__qualname__", "").startswith("build_app.<locals>.")
    ]
    assert len(hooks) == 1
    await hooks[0](app)


async def _run_cleanup(app: web.Application) -> None:
    hooks = [
        hook
        for hook in app.on_cleanup
        if getattr(hook, "__qualname__", "").startswith("build_app.<locals>.")
    ]
    assert len(hooks) == 1
    await hooks[0](app)


def test_build_app_remains_synchronous_with_unchanged_signature_and_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, control = _controlled_server_app(tmp_path, monkeypatch)

    assert inspect.iscoroutinefunction(build_app) is False
    assert str(inspect.signature(build_app)) == "(config: 'Config') -> 'web.Application'"
    assert isinstance(app, web.Application)
    assert control.events.index("preflight") < control.events.index("logger")
    assert control.events.index("logger") < control.events.index("core")
    assert (tmp_path / "memory" / "core").is_dir()
    assert (tmp_path / "messages").is_dir()


def test_build_app_constructs_core_before_unbound_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, control = _controlled_server_app(tmp_path, monkeypatch)

    assert control.events.index("core") < control.events.index("dispatcher")
    assert control.events.index("dispatcher") < control.events.index("scheduler")
    assert app["dispatcher"].resolver is control.core.identity_resolver
    assert app["dispatcher"]._run_turn is None
    assert app["scheduler"]._started is False


def test_server_collaborator_and_bridge_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _ServerControl(optional_bridges=True)
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)

    assert [type(bridge).__name__ for bridge in app["channels"].bridges()] == [
        "BenchBridge",
        "WebChatBridge",
        "DiscordBridge",
        "SlackBridge",
    ]
    assert app["pairing_notifier"]._channels is app["channels"]
    assert control.web_chat.chat_skill_registry is control.core.chat_skill_registry
    assert control.web_chat.enqueue.__self__ is app["dispatcher"]


def test_route_and_hook_parity_with_runtime_proxies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.server import _RuntimeFieldProxy

    app, control = _controlled_server_app(tmp_path, monkeypatch)
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert {
        ("GET", "/"),
        ("POST", "/event"),
        ("GET", "/health"),
        ("POST", "/api/memory/consolidate"),
        ("GET", "/api/v1/turn-events"),
        ("POST", "/chat"),
        ("GET", "/chat/stream"),
    } <= routes
    assert sum(
        getattr(hook, "__qualname__", "").startswith("build_app.<locals>.")
        for hook in app.on_startup
    ) == 1
    assert sum(
        getattr(hook, "__qualname__", "").startswith("build_app.<locals>.")
        for hook in app.on_cleanup
    ) == 1
    assert isinstance(control.route_kwargs["commitments_store"], _RuntimeFieldProxy)
    assert isinstance(control.route_kwargs["turn_event_bus"], _RuntimeFieldProxy)
    with pytest.raises(RuntimeError, match="agent runtime is not initialized"):
        control.route_kwargs["turn_event_bus"].subscribe("channel")


@pytest.mark.asyncio
async def test_startup_constructs_runtime_first_and_publishes_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, control = _controlled_server_app(tmp_path, monkeypatch)
    control.events.clear()

    assert app["agent"] is None
    await _run_startup(app)
    await asyncio.sleep(0)

    assert control.events[0] == "runtime"
    assert "bridges:bound:True" in control.events
    assert app["agent"] is control.bundle.agent
    assert app["agent_runtime"] is control.bundle
    assert app["runtime_slot"].bundle is control.bundle
    assert app["startup_state"].runtime_published is True
    assert control.route_kwargs["turn_event_bus"].subscribe("channel") is not None

    await _run_cleanup(app)


@pytest.mark.asyncio
async def test_operational_startup_order_and_mcp_bundle_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _ServerControl(panel_enabled=True, mcp_enabled=True)
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)
    control.events.clear()

    await _run_startup(app)
    await asyncio.sleep(0)

    ordered = [
        "runtime",
        "panel:construct",
        "panel:start",
        "indexer:start",
        "bridges:connect",
        "mcp:construct",
        "mcp:start",
        "bundle:mcp",
        "scheduler:start",
        "liveness:running",
    ]
    positions = [control.events.index(name) for name in ordered]
    assert positions == sorted(positions)
    assert app["mcp_manager"] is control.mcp_manager
    assert control.bundle.installed_tools == ["mcp-tool"]
    assert app["activity_panel"] is control.panel

    await _run_cleanup(app)


@pytest.mark.asyncio
async def test_startup_preserves_bad_cron_and_mcp_start_best_effort_policies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _ServerControl(mcp_enabled=True, mcp_returns_manager=False)
    control.failures["scheduler:add_saga_consolidate_job"] = ValueError("bad cron")
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)

    await _run_startup(app)

    assert app["agent"] is control.bundle.agent
    assert app["mcp_manager"] is None
    assert control.bundle.installed_tools == []
    assert "log:scheduler_invalid_cron" in control.events
    assert "bridges:bound:True" in control.events

    await _run_cleanup(app)


@pytest.mark.asyncio
async def test_bridge_connection_keeps_per_bridge_failures_best_effort() -> None:
    from mimir.channel_registry import ChannelRegistry

    events: list[str] = []

    class Bridge:
        prefixes: tuple[str, ...] = ()

        def __init__(self, name: str, failure: BaseException | None = None) -> None:
            self.name = name
            self.failure = failure

        async def connect(self) -> None:
            events.append(self.name)
            if self.failure is not None:
                raise self.failure

        async def disconnect(self) -> None:
            return None

        def matches(self, channel_id: str) -> bool:
            return False

    channels = ChannelRegistry()
    channels.register(Bridge("failed", RuntimeError("bridge unavailable")))
    channels.register(Bridge("healthy"))

    await channels.connect_all()

    assert events == ["failed", "healthy"]


@pytest.mark.asyncio
async def test_startup_state_records_every_attempt_and_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _ServerControl(panel_enabled=True, mcp_enabled=True)
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)

    await _run_startup(app)
    state = app["startup_state"]

    assert state.runtime_attempted is True
    assert state.bundle is control.bundle
    assert state.runtime_published is True
    assert state.activity_panel is control.panel
    assert state.activity_panel_start_attempted is True
    assert state.indexer_start_attempted is True
    assert state.bridges_connect_attempted is True
    assert state.mcp_start_attempted is True
    assert state.mcp_manager is control.mcp_manager
    assert state.scheduler_start_attempted is True
    assert state.scheduler_started is True
    assert state.liveness_mark_attempted is True
    assert state.compensated is False

    await _run_cleanup(app)


@pytest.mark.asyncio
async def test_failed_factory_cancels_resistant_app_tasks_and_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.background_tasks

    original = LookupError("runtime failed")
    control = _ServerControl(runtime_failure=original)
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)
    release = asyncio.Event()
    started = asyncio.Event()

    async def resistant() -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(resistant(), name="resistant-server-task")
    app["startup_background_tasks"].add(task)
    await started.wait()
    monkeypatch.setattr(mimir.background_tasks, "BACKGROUND_TASK_CANCEL_TIMEOUT_SECONDS", 0.01)

    try:
        with pytest.raises(LookupError) as caught:
            await _run_startup(app)
        assert caught.value is original
        assert "resistant-server-task" in " ".join(getattr(original, "__notes__", []))
        assert app["startup_background_tasks"] == set()
        assert app["startup_state"].compensated is True
        assert app["agent"] is None
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_liveness_write_then_raise_preserves_unclean_marker_and_logs_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.event_logger import init_logger as init_event_logger
    from mimir.event_logger import log_event as write_event
    from mimir.liveness import detect_unclean_restart, mark_session_running, read_session_marker

    original = RuntimeError("liveness write failed")
    control = _ServerControl()
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)
    events_path = app["config"].events_log
    init_event_logger(events_path, "failed-startup-test")
    monkeypatch.setattr("mimir.server.log_event", write_event)

    def mark_then_raise(*args: Any, **kwargs: Any) -> None:
        mark_session_running(*args, **kwargs)
        raise original

    monkeypatch.setattr("mimir.liveness.mark_session_running", mark_then_raise)

    with pytest.raises(RuntimeError) as caught:
        await _run_startup(app)

    assert caught.value is original
    records = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    failure = next(record for record in records if record["type"] == "startup_failed")
    assert failure["phase"] == "liveness_marker"
    assert failure["exception"] == repr(original)
    marker = read_session_marker(tmp_path)
    assert marker is not None and marker["clean"] is False
    assert detect_unclean_restart(tmp_path) == marker
    assert "liveness:clean" not in control.events
    assert "bundle:close" in control.events
    assert "pairing:close" in control.events
    assert app["agent"] is None
    assert app["runtime_slot"].bundle is None
    assert app["startup_state"].compensated is True


@pytest.mark.asyncio
async def test_next_start_reports_aborted_startup_as_unclean_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.liveness
    from mimir.liveness import detect_unclean_restart, mark_session_running

    mark_session_running(tmp_path, started_at=1000.0)
    prior = detect_unclean_restart(tmp_path)
    assert prior is not None
    app, control = _controlled_server_app(tmp_path, monkeypatch)
    notified: list[dict[str, Any]] = []

    async def notify(home: Path, *, prior: dict[str, Any]) -> None:
        notified.append(prior)

    monkeypatch.setattr(mimir.liveness, "detect_unclean_restart", detect_unclean_restart)
    monkeypatch.setattr(mimir.liveness, "notify_unclean_restart", notify)

    await _run_startup(app)
    await asyncio.sleep(0)

    assert "log:liveness_unclean_restart" in control.events
    assert notified == [prior]
    await _run_cleanup(app)


@pytest.mark.asyncio
async def test_partial_scheduler_start_is_stopped_during_compensation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("scheduler partial start")
    control = _ServerControl()
    control.failures["scheduler:start"] = original
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)

    with pytest.raises(RuntimeError) as caught:
        await _run_startup(app)

    assert caught.value is original
    assert app["startup_state"].scheduler_start_attempted is True
    assert app["startup_state"].scheduler_started is False
    assert "scheduler:stop" in control.events
    assert control.scheduler._scheduler.running is False


@pytest.mark.asyncio
async def test_retained_mcp_manager_is_shutdown_on_later_startup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("later startup failure")
    control = _ServerControl(mcp_enabled=True)
    control.failures["scheduler:reload"] = original
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)

    with pytest.raises(RuntimeError) as caught:
        await _run_startup(app)

    assert caught.value is original
    assert control.events.index("mcp:start") < control.events.index("mcp:shutdown")
    assert app["mcp_manager"] is None
    assert app["startup_state"].mcp_manager is None


@pytest.mark.asyncio
async def test_bridge_attempt_is_disconnected_on_startup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("bridge connection failed")
    control = _ServerControl()
    control.failures["bridges:connect"] = original
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)

    with pytest.raises(RuntimeError) as caught:
        await _run_startup(app)

    assert caught.value is original
    assert "bridges:disconnect" in control.events
    assert app["startup_state"].bridges_connect_attempted is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["runtime", "indexer:start"])
async def test_startup_failure_distinguishes_factory_and_published_bundle_rollback(
    failure_stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError(f"{failure_stage} failed")
    control = _ServerControl(
        runtime_failure=original if failure_stage == "runtime" else None,
    )
    if failure_stage != "runtime":
        control.failures[failure_stage] = original
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)

    with pytest.raises(RuntimeError) as caught:
        await _run_startup(app)

    assert caught.value is original
    assert ("bundle:close" in control.events) is (failure_stage != "runtime")
    assert app["dispatcher"]._run_turn is None
    assert app["agent_runtime"] is None


@pytest.mark.asyncio
async def test_partial_activity_panel_start_is_stopped_and_pairing_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("panel start failed")
    control = _ServerControl(panel_enabled=True)
    control.failures["panel:start"] = original
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)

    with pytest.raises(RuntimeError) as caught:
        await _run_startup(app)

    assert caught.value is original
    assert "bundle:close" in control.events
    assert "panel:stop" in control.events
    assert "pairing:close" in control.events
    assert app["activity_panel"] is None


@pytest.mark.asyncio
async def test_startup_compensation_continues_after_errors_and_reraises_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = LookupError("startup terminal failure")
    control = _ServerControl(panel_enabled=True, mcp_enabled=True)
    control.failures.update(
        {
            "liveness:running": original,
            "scheduler:stop": RuntimeError("scheduler cleanup"),
            "mcp:shutdown": RuntimeError("mcp cleanup"),
            "bridges:disconnect": RuntimeError("bridge cleanup"),
            "bundle:close": RuntimeError("bundle cleanup"),
            "panel:stop": RuntimeError("panel cleanup"),
            "pairing:close": RuntimeError("pairing cleanup"),
        }
    )
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)

    with pytest.raises(LookupError) as caught:
        await _run_startup(app)

    assert caught.value is original
    for event_name in (
        "scheduler:stop",
        "mcp:shutdown",
        "bridges:disconnect",
        "bundle:close",
        "panel:stop",
        "pairing:close",
    ):
        assert event_name in control.events
    assert "cleanup failure(s)" in " ".join(getattr(original, "__notes__", []))
    assert app["agent"] is None
    assert app["startup_state"].compensated is True


@pytest.mark.asyncio
async def test_server_startup_and_cleanup_resource_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _ServerControl(panel_enabled=True, mcp_enabled=True)
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)
    await _run_startup(app)
    await asyncio.sleep(0)
    assert "log:app_started" in control.events
    assert "log:api_started" in control.events
    control.events.clear()

    await _run_cleanup(app)

    ordered = [
        "liveness:clean",
        "claims:release",
        "log:shutdown",
        "dispatcher:drain",
        "cancel:server cleanup",
        "scheduler:stop",
        "bundle:close",
        "panel:stop",
        "pairing:close",
        "bridges:disconnect",
        "mcp:shutdown",
    ]
    positions = [control.events.index(name) for name in ordered]
    assert positions == sorted(positions)


@pytest.mark.asyncio
async def test_server_cleanup_continues_aggregates_errors_and_clears_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _ServerControl(panel_enabled=True, mcp_enabled=True)
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)
    await _run_startup(app)
    await asyncio.sleep(0)
    control.events.clear()
    control.failures.update(
        {
            "liveness:clean": RuntimeError("liveness cleanup"),
            "claims:release": RuntimeError("claims cleanup"),
            "log:shutdown": RuntimeError("log cleanup"),
            "dispatcher:drain": RuntimeError("drain cleanup"),
            "cancel:server cleanup": RuntimeError("task cleanup"),
            "scheduler:stop": RuntimeError("scheduler cleanup"),
            "bundle:close": RuntimeError("bundle cleanup"),
            "panel:stop": RuntimeError("panel cleanup"),
            "pairing:close": RuntimeError("pairing cleanup"),
            "bridges:disconnect": RuntimeError("bridge cleanup"),
            "mcp:shutdown": RuntimeError("mcp cleanup"),
        }
    )

    with pytest.raises(ExceptionGroup, match="server cleanup failed") as caught:
        await _run_cleanup(app)

    assert len(caught.value.exceptions) == 11
    for event_name in control.failures:
        assert event_name in control.events
    assert app["agent"] is None
    assert app["agent_runtime"] is None
    assert app["runtime_slot"].bundle is None
    assert app["startup_state"].bundle is None
    assert app["mcp_manager"] is None
    assert app["activity_panel"] is None


@pytest.mark.asyncio
async def test_normal_cleanup_resistant_task_timeout_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.background_tasks

    app, control = _controlled_server_app(tmp_path, monkeypatch)
    await _run_startup(app)
    await asyncio.sleep(0)
    release = asyncio.Event()
    started = asyncio.Event()

    async def resistant() -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(resistant(), name="cleanup-resistant-task")
    app["startup_background_tasks"].add(task)
    await started.wait()
    monkeypatch.setattr(mimir.background_tasks, "BACKGROUND_TASK_CANCEL_TIMEOUT_SECONDS", 0.01)
    control.events.clear()

    try:
        with pytest.raises(ExceptionGroup, match="server cleanup failed") as caught:
            await _run_cleanup(app)
        assert any(
            isinstance(error, TimeoutError) and "cleanup-resistant-task" in str(error)
            for error in caught.value.exceptions
        )
        assert "scheduler:stop" in control.events
        assert "bundle:close" in control.events
        assert "pairing:close" in control.events
        assert "bridges:disconnect" in control.events
        assert app["agent"] is None
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_server_cleanup_does_not_repeat_compensated_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("indexer failed")
    control = _ServerControl()
    control.failures["indexer:start"] = original
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)
    with pytest.raises(RuntimeError):
        await _run_startup(app)
    control.events.clear()

    await _run_cleanup(app)

    assert control.events == ["cancel:server cleanup"]


def test_complete_prestartup_app_key_contract_and_benchmark_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, control = _controlled_server_app(tmp_path, monkeypatch)
    runtime_fields = {
        "agent",
        "turn_logger",
        "message_buffer",
        "indexes",
        "indexer",
        "saga_client",
        "sessions",
        "agent_runtime",
        "replayed_messages",
    }

    assert all(app[field_name] is None for field_name in runtime_fields)
    assert app["config"] is not None
    assert app["dispatcher"] is control.dispatcher
    assert app["scheduler"] is control.scheduler
    assert app["channels"] is control.channels
    assert app["pairing_notifier"] is not None
    assert app["identity_resolver"] is control.core.identity_resolver
    assert app["aliases_loaded"] == 4
    assert app["seeded_subagents"] == {}
    assert app["seeded_skills"] == {}
    assert app["api_key"] == "test-key"
    assert app["consolidate_guard"] is not None
    assert app["startup_background_tasks"] == set()
    assert app["startup_state"].runtime_attempted is False
    assert "activity_panel" not in app
    assert "mcp_manager" not in app


@pytest.mark.asyncio
async def test_runtime_and_server_owned_app_keys_follow_success_and_cleanup_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.history import get_global_buffer

    control = _ServerControl(panel_enabled=True, mcp_enabled=True)
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)
    dispatcher = app["dispatcher"]
    scheduler = app["scheduler"]
    channels = app["channels"]
    notifier = app["pairing_notifier"]

    await _run_startup(app)

    assert app["agent"] is control.bundle.agent
    assert app["turn_logger"] is control.bundle.turn_logger
    assert app["message_buffer"] is control.bundle.message_buffer
    assert app["indexes"] is control.bundle.indexes
    assert app["indexer"] is control.bundle.indexer
    assert app["saga_client"] is control.bundle.saga_client
    assert app["sessions"] is control.bundle.sessions
    assert app["replayed_messages"] == 9
    assert get_global_buffer() is control.bundle.message_buffer
    assert app["dispatcher"] is dispatcher
    assert app["scheduler"] is scheduler
    assert app["channels"] is channels
    assert app["pairing_notifier"] is notifier

    await _run_cleanup(app)

    assert all(app[field_name] is None for field_name in (
        "agent",
        "turn_logger",
        "message_buffer",
        "indexes",
        "indexer",
        "saga_client",
        "sessions",
        "agent_runtime",
        "replayed_messages",
    ))
    assert get_global_buffer() is None
    assert dispatcher._run_turn is None
    assert app["dispatcher"] is dispatcher
    assert app["scheduler"] is scheduler
    assert app["channels"] is channels
    assert app["pairing_notifier"] is notifier


@pytest.mark.asyncio
async def test_conditional_panel_mcp_and_dispatcher_ingress_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _ServerControl(panel_enabled=True, mcp_enabled=True)
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)

    assert app["activity_panel"] is None
    assert "mcp_manager" not in app
    assert await app["dispatcher"].enqueue(object()) is False
    assert control.web_chat.enqueue.__self__ is app["dispatcher"]

    await _run_startup(app)

    assert app["activity_panel"] is control.panel
    assert app["mcp_manager"] is control.mcp_manager
    assert await app["dispatcher"].enqueue(object()) is True

    await _run_cleanup(app)

    assert app["activity_panel"] is None
    assert app["mcp_manager"] is None
    assert await app["dispatcher"].enqueue(object()) is False


# ──────────────────────────────────────────────────────────────────────────────
# _safe_str_eq
# ──────────────────────────────────────────────────────────────────────────────


class TestSafeStrEq:
    def test_equal_strings_returns_true(self) -> None:
        assert _safe_str_eq("abc123", "abc123") is True

    def test_unequal_strings_returns_false(self) -> None:
        assert _safe_str_eq("abc123", "abc124") is False

    def test_empty_equal(self) -> None:
        assert _safe_str_eq("", "") is True

    def test_empty_vs_non_empty(self) -> None:
        assert _safe_str_eq("", "x") is False

    def test_different_length(self) -> None:
        assert _safe_str_eq("short", "much-longer-string") is False

    def test_unicode_equal(self) -> None:
        assert _safe_str_eq("kéy-🔑", "kéy-🔑") is True

    def test_unicode_unequal(self) -> None:
        assert _safe_str_eq("kéy-🔑", "key-🔑") is False


# ──────────────────────────────────────────────────────────────────────────────
# _AUTH_EXEMPT
# ──────────────────────────────────────────────────────────────────────────────


class TestAuthExemptSet:
    def test_health_get_is_exempt(self) -> None:
        assert ("GET", "/health") in _AUTH_EXEMPT

    def test_react_app_get_is_exempt(self) -> None:
        assert ("GET", "/app") in _AUTH_EXEMPT

    def test_browser_auth_bootstrap_is_exempt(self) -> None:
        assert ("GET", "/app/auth.js") in _AUTH_EXEMPT
        assert ("GET", "/api/web/bootstrap") in _AUTH_EXEMPT
        assert ("GET", "/api/v1/web/bootstrap") in _AUTH_EXEMPT

    def test_skill_auto_update_event_reports_failures_without_drift(self) -> None:
        from mimir.server import _skill_auto_update_event
        from mimir.skill_install import AutoSkillUpdateResult

        event = _skill_auto_update_event(AutoSkillUpdateResult(
            failed={"github-poller": ["poller.py"]},
        ))

        assert event is not None
        kind, fields = event
        assert kind == "skills_auto_update_failed"
        assert fields["failed"] == {"github-poller": ["poller.py"]}

    def test_skill_auto_update_event_reports_remaining_drift_as_non_failed(self) -> None:
        from mimir.server import _skill_auto_update_event
        from mimir.skill_install import AutoSkillUpdateResult

        event = _skill_auto_update_event(AutoSkillUpdateResult(
            remaining_drift={"github-poller": {"extra": ["local-note.md"]}},
        ))

        assert event is not None
        kind, fields = event
        assert kind == "skills_auto_update"
        assert fields["remaining_drift"] == {
            "github-poller": {"extra": ["local-note.md"]}
        }

    def test_react_assets_get_are_prefix_exempt(self) -> None:
        assert ("GET", "/app/") in _AUTH_EXEMPT_PREFIXES
        assert _is_auth_exempt("GET", "/app/assets/index.js") is True

    def test_turns_get_is_exempt(self) -> None:
        assert ("GET", "/turns") in _AUTH_EXEMPT

    def test_ops_get_is_exempt(self) -> None:
        assert ("GET", "/ops") in _AUTH_EXEMPT

    def test_saga_get_is_exempt(self) -> None:
        assert ("GET", "/saga") in _AUTH_EXEMPT

    def test_state_get_is_exempt(self) -> None:
        assert ("GET", "/state") in _AUTH_EXEMPT  # renamed from /memory

    def test_root_get_is_exempt(self) -> None:
        assert ("GET", "/") in _AUTH_EXEMPT

    def test_event_post_is_not_exempt(self) -> None:
        assert ("POST", "/event") not in _AUTH_EXEMPT

    def test_health_post_is_not_exempt(self) -> None:
        """A hypothetical POST /health must NOT inherit the GET exemption."""
        assert ("POST", "/health") not in _AUTH_EXEMPT

    def test_turns_post_is_not_exempt(self) -> None:
        assert ("POST", "/turns") not in _AUTH_EXEMPT

    def test_react_prefix_does_not_exempt_post(self) -> None:
        assert _is_auth_exempt("POST", "/app/assets/index.js") is False

    def test_is_frozenset_of_tuples(self) -> None:
        assert isinstance(_AUTH_EXEMPT, frozenset)
        for item in _AUTH_EXEMPT:
            assert isinstance(item, tuple)
            assert len(item) == 2


# ──────────────────────────────────────────────────────────────────────────────
# _MaskApiKeyInAccessLog
# ──────────────────────────────────────────────────────────────────────────────


class TestMaskApiKeyInAccessLog:
    def _make_record(self, msg: Any, args: tuple = ()) -> logging.LogRecord:
        record = logging.LogRecord(
            name="aiohttp.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=args,
            exc_info=None,
        )
        return record

    def test_masks_query_param_in_msg(self) -> None:
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("GET /?api_key=supersecret HTTP/1.1")
        result = filt.filter(record)
        assert result is True
        assert "supersecret" not in record.msg
        assert "REDACTED" in record.msg

    def test_masks_mid_query_string(self) -> None:
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("/path?foo=bar&api_key=s3cr3t&baz=qux")
        filt.filter(record)
        assert "s3cr3t" not in record.msg
        assert "REDACTED" in record.msg
        # Other params survive
        assert "foo=bar" in record.msg
        assert "baz=qux" in record.msg

    def test_masks_in_args_tuple(self) -> None:
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record(
            "method=%s url=%s",
            ("GET", "/?api_key=exposed"),
        )
        filt.filter(record)
        for arg in record.args:
            assert "exposed" not in str(arg)

    def test_non_string_args_left_alone(self) -> None:
        """Non-string elements in args (numbers, None) are passed through."""
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("code=%s time=%s", (200, 0.001))
        filt.filter(record)
        assert record.args == (200, 0.001)

    def test_non_string_msg_not_crashed(self) -> None:
        """A non-string msg (aiohttp may pass structured objects) must not crash."""
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record(42)  # int msg
        result = filt.filter(record)
        assert result is True
        assert record.msg == 42  # untouched

    def test_clean_record_unchanged(self) -> None:
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("GET /health HTTP/1.1")
        filt.filter(record)
        assert record.msg == "GET /health HTTP/1.1"

    def test_case_insensitive_match(self) -> None:
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("GET /?API_KEY=secret HTTP/1.1")
        filt.filter(record)
        assert "secret" not in record.msg

    def test_always_returns_true(self) -> None:
        """filter() must return True to keep the record in the log stream."""
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("irrelevant")
        assert filt.filter(record) is True


# ──────────────────────────────────────────────────────────────────────────────
# _handle_health
# ──────────────────────────────────────────────────────────────────────────────


def _health_app() -> web.Application:
    """Minimal app with only the /health route."""
    app = web.Application()
    app.router.add_get("/health", _handle_health)
    return app


def _root_app() -> web.Application:
    """Minimal app with only the / route."""
    app = web.Application()
    app.router.add_get("/", _handle_root)
    return app


class TestRootRedirect:
    async def test_root_redirects_to_react_app(self) -> None:
        async with TestClient(TestServer(_root_app())) as client:
            resp = await client.get("/", allow_redirects=False)
            assert resp.status == 302
            assert resp.headers["Location"] == "/app"


class TestHandleHealth:
    @pytest.mark.asyncio
    async def test_health_returns_200(self) -> None:
        async with TestClient(TestServer(_health_app())) as client:
            resp = await client.get("/health")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_health_returns_ok_true(self) -> None:
        async with TestClient(TestServer(_health_app())) as client:
            resp = await client.get("/health")
            body = await resp.json()
        assert body == {"ok": True}

    @pytest.mark.asyncio
    async def test_health_is_json(self) -> None:
        async with TestClient(TestServer(_health_app())) as client:
            resp = await client.get("/health")
        assert "application/json" in resp.headers.get("Content-Type", "")

    @pytest.mark.asyncio
    async def test_health_reports_stale_root_executor_before_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from mimir.worklink import worker_client

        socket_path = tmp_path / "executor.sock"
        socket_path.touch()
        monkeypatch.setattr(worker_client, "DEFAULT_EXECUTOR_SOCKET", socket_path)

        async def stale(_socket_path: Path = socket_path) -> None:
            raise worker_client.StaleWorkerExecutorError(
                worker_client.STALE_EXECUTOR_DIAGNOSTIC
            )

        monkeypatch.setattr(worker_client, "verify_executor_identity", stale)
        app = _health_app()
        app["check_worker_executor_health"] = True
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            body = await resp.json()

        assert resp.status == 503
        assert body["ok"] is False
        assert "stale root executor image" in body["error"]
        assert "rebuild the image and restart the container" in body["error"]


# ──────────────────────────────────────────────────────────────────────────────
# _make_auth_middleware
# ──────────────────────────────────────────────────────────────────────────────


async def _ok_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def _auth_app(expected_key: str, *, web_host: str | None = None) -> web.Application:
    """Minimal app wiring the auth middleware around a simple route."""
    app = web.Application(
        middlewares=[_make_auth_middleware(expected_key, web_host=web_host)]
    )
    app.router.add_get("/protected", _ok_handler)
    app.router.add_post("/protected", _ok_handler)
    app.router.add_put("/protected", _ok_handler)
    app.router.add_patch("/protected", _ok_handler)
    app.router.add_delete("/protected", _ok_handler)
    app.router.add_get("/api/v1/sessions", _ok_handler)
    # Register all exempt paths so we can hit them in tests
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/turns", _ok_handler)
    app.router.add_get("/ops", _ok_handler)
    app.router.add_get("/saga", _ok_handler)
    app.router.add_get("/state", _ok_handler)
    app.router.add_get("/api/web/bootstrap", _ok_handler)
    app.router.add_get("/api/v1/web/bootstrap", _ok_handler)
    return app


class TestAuthMiddlewareNoKey:
    """When no key is configured the middleware is a no-op pass-through."""

    @pytest.mark.asyncio
    async def test_no_key_allows_any_request(self) -> None:
        async with TestClient(TestServer(_auth_app(""))) as client:
            resp = await client.get("/protected")
        assert resp.status == 200

class TestBrowserRequestSecurity:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("expected_key", ["", "secret"])
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    async def test_cross_site_fetch_metadata_rejects_write_before_auth(
        self, expected_key: str, method: str,
    ) -> None:
        headers = {"Sec-Fetch-Site": "cross-site"}
        if expected_key:
            headers["X-API-Key"] = expected_key
        async with TestClient(TestServer(_auth_app(expected_key))) as client:
            resp = await client.request(method, "/protected", headers=headers)
            body = await resp.json()
        assert resp.status == 403
        assert body == {"error": "cross_site_request"}

    @pytest.mark.asyncio
    async def test_foreign_origin_rejects_write_when_gate_is_inactive(self) -> None:
        async with TestClient(TestServer(_auth_app(""))) as client:
            resp = await client.post(
                "/protected",
                headers={"Origin": "https://attacker.example"},
            )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_same_origin_write_is_allowed(self) -> None:
        async with TestClient(TestServer(_auth_app(""))) as client:
            origin = str(client.make_url("/")).rstrip("/")
            resp = await client.post(
                "/protected",
                headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"},
            )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_non_browser_write_without_origin_headers_is_allowed(self) -> None:
        async with TestClient(TestServer(_auth_app(""))) as client:
            resp = await client.post("/protected")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_loopback_bind_rejects_rebinding_host_on_read_route(self) -> None:
        app = _auth_app("", web_host="127.0.0.1")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/v1/sessions", headers={"Host": "attacker.example"}
            )
            body = await resp.json()
        assert resp.status == 403
        assert body == {"error": "invalid_host"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "host", ["127.0.0.1:8080", "localhost:8080", "[::1]:8080"]
    )
    async def test_loopback_bind_accepts_loopback_hostnames(self, host: str) -> None:
        app = _auth_app("", web_host="127.0.0.1")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/sessions", headers={"Host": host})
        assert resp.status == 200


class TestAuthMiddlewareWithKey:
    """When a key IS configured the middleware gates every non-exempt route."""

    @pytest.mark.asyncio
    async def test_correct_header_key_passes(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get(
                "/protected", headers={"X-API-Key": "my-secret"}
            )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_wrong_header_key_rejected(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get(
                "/protected", headers={"X-API-Key": "wrong"}
            )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_missing_key_header_rejected(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get("/protected")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_401_body_has_error_field(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get("/protected")
            body = await resp.json()
        assert body.get("error") == "unauthorized"

    @pytest.mark.asyncio
    async def test_query_param_api_key_is_rejected(self) -> None:
        """API keys in URLs are rejected; browsers use header-based fetch."""
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get("/protected?api_key=my-secret")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_query_param_wrong_key_is_rejected(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get("/protected?api_key=nope")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_header_takes_precedence_over_query_when_both_present(
        self,
    ) -> None:
        """Header auth still works if a non-auth query string is present."""
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get(
                "/protected?api_key=garbage",
                headers={"X-API-Key": "my-secret"},
            )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_empty_header_does_not_fall_back_to_query(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get(
                "/protected?api_key=my-secret",
                headers={"X-API-Key": ""},
            )
        assert resp.status == 401


class TestAuthMiddlewareExemptRoutes:
    """_AUTH_EXEMPT routes bypass the gate even when a key is configured."""

    @pytest.mark.asyncio
    async def test_health_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/health")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_turns_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/turns")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_ops_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/ops")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_saga_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/saga")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_state_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/state")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_v1_web_bootstrap_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/api/v1/web/bootstrap")
        assert resp.status == 200


# ──────────────────────────────────────────────────────────────────────────────
# _handle_event
# ──────────────────────────────────────────────────────────────────────────────


def _event_app(
    *,
    enqueue_returns: bool = True,
) -> tuple[web.Application, MagicMock]:
    """Minimal app wiring the /event route with a stub dispatcher.

    Returns (app, stub_dispatcher) so tests can inspect calls.
    """
    stub = MagicMock()
    stub.enqueue = AsyncMock(return_value=enqueue_returns)

    app = web.Application(middlewares=[_make_auth_middleware("")])
    app["dispatcher"] = stub
    app.router.add_post("/event", _handle_event)
    return app, stub


def _event_app_authed(
    identity: Any,
    *,
    is_admin: bool = False,
) -> tuple[web.Application, MagicMock]:
    """/event app that injects an authenticated per-user identity.

    Mirrors the request keys the real auth middleware stamps
    (``auth_identity`` / ``auth_is_admin``) so the per-user channel-binding
    path in ``_handle_event`` is exercised.
    """
    stub = MagicMock()
    stub.enqueue = AsyncMock(return_value=True)

    @web.middleware
    async def _inject(request: web.Request, handler: Any) -> web.StreamResponse:
        request["auth_identity"] = identity
        request["auth_is_admin"] = is_admin
        return await handler(request)

    app = web.Application(middlewares=[_inject])
    app["dispatcher"] = stub
    app.router.add_post("/event", _handle_event)
    return app, stub


class TestHandleEvent:
    @pytest.mark.asyncio
    async def test_cross_site_text_plain_event_is_rejected_before_enqueue(self) -> None:
        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                data='{"channel_id":"web-default","content":"run"}',
                headers={
                    "Content-Type": "text/plain",
                    "Origin": "https://attacker.example",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
        assert resp.status == 403
        stub.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_event_returns_200(self) -> None:
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "test-channel", "content": "hello"},
            )
        assert resp.status == 200

    async def test_event_strips_forged_chat_skill_extra(self) -> None:
        # chainlink #783 (security): the generic /event ingress is
        # client-controlled, so a forged chat-skill invocation must be stripped
        # before enqueue — only the WebChatBridge may produce one.
        from mimir.chat_skills import CHAT_SKILL_EXTRA_KEY, LEGACY_CHAT_SKILL_EXTRA_KEY
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": "c",
                    "content": "/deploy now",
                    "extra": {
                        CHAT_SKILL_EXTRA_KEY: {
                            "name": "deploy", "command": "/deploy",
                            "args": "now", "raw": "/deploy now",
                        },
                        LEGACY_CHAT_SKILL_EXTRA_KEY: {"x": 1},
                        "keep": "me",
                    },
                },
            )
        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert CHAT_SKILL_EXTRA_KEY not in event.extra
        assert LEGACY_CHAT_SKILL_EXTRA_KEY not in event.extra
        assert event.extra == {
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "keep": "me",
        }

    async def test_event_strips_client_asserted_bridge_authority(self) -> None:
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": "c",
                    "content": "publish my synthesis",
                    "author": "alice",
                    "source": "api",
                    "extra": {
                        "channel_visibility": "public",
                        "bridge_instance": "forged-bridge",
                        "keep": "me",
                    },
                },
            )
        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.extra == {
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "keep": "me",
        }
        from mimir.access_control import create_auth_context
        from mimir.models import SessionACL

        context = create_auth_context(event, enforce=True)
        durable_acl = SessionACL.from_auth_context(
            context,
            origin_domain=event.source,
            visibility=event.extra.get("channel_visibility", "private"),
        )
        assert durable_acl.visibility == "private"

    async def test_event_strips_client_asserted_saga_session_id(self) -> None:
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": "c",
                    "extra": {
                        "saga_session_id": "saga-foreign-123-abc",
                        "keep": "me",
                    },
                },
            )

        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.extra == {
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "keep": "me",
        }

    async def test_event_strips_client_asserted_delivery_channel(self) -> None:
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": "c",
                    "content": "summarize my recent messages",
                    "extra": {"deliver": "slack-C0PRIVATE", "keep": "me"},
                },
            )

        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.extra == {
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "keep": "me",
        }

    async def test_event_strips_forged_worklink_hint_extra(self) -> None:
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": "c",
                    "content": "resume chainlink #740",
                    "extra": {
                        "issue_id": 740,
                        "pr_url": "https://github.com/acme/demo/pull/7",
                        "worktree": "/tmp/evil",
                        "poller_name": "forged-poller",
                        "schedule_name": "forged-schedule",
                        "run_id": "chainlink-740",
                        "keep": "me",
                        "nested": {
                            "issue_id": 999,
                            "schedule_name": "nested-forged-schedule",
                            "still_here": True,
                        },
                    },
                },
            )
        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.extra == {
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "keep": "me",
            "nested": {"still_here": True},
        }

    async def test_event_stamps_http_ingress_as_untrusted_for_privileged_side_effects(self) -> None:
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": "c",
                    "content": "resume chainlink #740",
                    "extra": {
                        HTTP_EVENT_INGRESS_EXTRA_KEY: "forged-client-value",
                        "keep": {"nested": True},
                    },
                },
            )
        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.extra == {
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "keep": {"nested": True},
        }

    @pytest.mark.asyncio
    async def test_valid_event_returns_ok_true(self) -> None:
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "test-channel"},
            )
            body = await resp.json()
        assert body["ok"] is True
        assert body["channel_id"] == "test-channel"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                data=b"not-json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400
        # Dispatcher must not be called when the body is invalid
        app["dispatcher"].enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_channel_id_returns_400(self) -> None:
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/event", json={"content": "oops"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_channel_id_error_body(self) -> None:
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/event", json={})
            body = await resp.json()
        assert "channel_id" in body.get("error", "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel_id", [["web-x"], 123])
    async def test_non_string_channel_id_returns_400_before_enqueue(
        self, channel_id: Any
    ) -> None:
        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event", json={"channel_id": channel_id, "content": "hi"}
            )
            body = await resp.json()

        assert resp.status == 400
        assert body["error"] == "channel_id must be a string"
        stub.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_dict_extra_returns_400(self) -> None:
        """#487: a truthy non-dict ``extra`` is rejected with 400, not coerced
        (coercion let it reach ``event.extra.get(...)`` → AttributeError/500)."""
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "ch", "extra": "oops"},
            )
        assert resp.status == 400
        app["dispatcher"].enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_list_attachment_names_returns_400(self) -> None:
        """#487: a non-list ``attachment_names`` is rejected with 400."""
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "ch", "attachment_names": "a.txt"},
            )
        assert resp.status == 400
        app["dispatcher"].enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatcher_rejects_returns_503(self) -> None:
        """When the dispatcher's queue is full it returns False → 503."""
        app, _ = _event_app(enqueue_returns=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "busy-channel"},
            )
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_dispatcher_rejects_body_has_channel_id(self) -> None:
        app, _ = _event_app(enqueue_returns=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "busy-channel"},
            )
            body = await resp.json()
        assert body.get("channel_id") == "busy-channel"

    @pytest.mark.asyncio
    async def test_non_admin_key_cannot_target_another_users_channel(self) -> None:
        # Security: the request-body channel_id is spoofable. A per-user web key
        # must not be able to run a turn on another user's channel (which would
        # pull that user's private history into context / steer its egress).
        from types import SimpleNamespace

        from mimir.web_channels import web_channel_for_identity

        bob = SimpleNamespace(canonical="bob", display_name="Bob")
        app, stub = _event_app_authed(bob, is_admin=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": web_channel_for_identity("alice"),
                    "content": "read alice's history",
                },
            )
        assert resp.status == 403
        stub.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_admin_key_may_target_own_channel(self) -> None:
        from types import SimpleNamespace

        from mimir.web_channels import web_channel_for_identity

        bob = SimpleNamespace(canonical="bob", display_name="Bob")
        own = web_channel_for_identity("bob")
        app, stub = _event_app_authed(bob, is_admin=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": own, "content": "hi"},
            )
        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.channel_id == own
        assert event.author == "bob"

    @pytest.mark.asyncio
    async def test_admin_key_may_target_any_channel(self) -> None:
        # Admins (and the master key, which has identity=None) target any
        # channel for operator/automation use.
        from types import SimpleNamespace

        admin = SimpleNamespace(canonical="op", display_name="Op")
        app, stub = _event_app_authed(admin, is_admin=True)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "web-alice", "content": "ops broadcast"},
            )
        assert resp.status == 200
        stub.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_defaults_to_user_message(self) -> None:
        """A body with no ``trigger`` field should default to ``user_message``."""
        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/event",
                json={"channel_id": "ch"},
            )
        call_args = stub.enqueue.call_args
        event = call_args.args[0]
        assert event.trigger == "user_message"

    @pytest.mark.asyncio
    async def test_non_admin_arbitrary_trigger_is_rejected(self) -> None:
        from types import SimpleNamespace

        from mimir.web_channels import web_channel_for_identity

        bob = SimpleNamespace(canonical="bob", display_name="Bob")
        app, stub = _event_app_authed(bob, is_admin=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": web_channel_for_identity("bob"),
                    "trigger": "arbitrary_internal_wake",
                },
            )
            body = await resp.json()

        assert resp.status == 403
        assert body == {
            "error": "trigger not permitted for non-admin HTTP callers",
            "allowed_triggers": ["user_message"],
        }
        stub.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_admin_privileged_trigger_is_rejected(self) -> None:
        from types import SimpleNamespace

        from mimir.web_channels import web_channel_for_identity

        bob = SimpleNamespace(canonical="bob", display_name="Bob")
        app, stub = _event_app_authed(bob, is_admin=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": web_channel_for_identity("bob"),
                    "trigger": "saga_session_end",
                },
            )

        assert resp.status == 403
        stub.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_admin_explicit_trigger_is_forwarded_without_session_selector(
        self,
    ) -> None:
        from types import SimpleNamespace

        admin = SimpleNamespace(canonical="op", display_name="Op")
        app, stub = _event_app_authed(admin, is_admin=True)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": "ch",
                    "trigger": "scheduled_tick",
                    "extra": {"saga_session_id": "saga-foreign-123-abc"},
                },
            )

        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.trigger == "scheduled_tick"
        assert "saga_session_id" not in event.extra

    @pytest.mark.asyncio
    async def test_content_forwarded(self) -> None:
        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/event",
                json={"channel_id": "ch", "content": "ping"},
            )
        event = stub.enqueue.call_args.args[0]
        assert event.content == "ping"

    @pytest.mark.asyncio
    async def test_author_forwarded(self) -> None:
        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/event",
                json={"channel_id": "ch", "author": "alice", "author_id": "u123"},
            )
        event = stub.enqueue.call_args.args[0]
        assert event.author == "alice"
        assert event.author_id == "u123"

    @pytest.mark.asyncio
    async def test_per_user_key_ignores_spoofed_admin_and_service_authority(
        self, tmp_path,
    ) -> None:
        from mimir.access_control import create_auth_context
        from mimir.identities import IdentityResolver
        from mimir.identities_populator import issue_web_key
        from mimir.web_channels import web_channel_for_identity

        user_key = issue_web_key(tmp_path, "alice", roles=["user"])
        issue_web_key(tmp_path, "operator", roles=["admin"])
        resolver = IdentityResolver(tmp_path)
        resolver.reload()

        stub = MagicMock()
        stub.enqueue = AsyncMock(return_value=True)
        app = web.Application(middlewares=[_make_auth_middleware("master-secret")])
        app["dispatcher"] = stub
        app["identity_resolver"] = resolver
        app.router.add_post("/event", _handle_event)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                headers={"X-API-Key": user_key},
                json={
                    # A non-admin per-user key may only target its own channel;
                    # the spoofed author/service_principal below must still be
                    # stripped regardless.
                    "channel_id": web_channel_for_identity("alice"),
                    "author": "operator",
                    "author_id": "operator",
                    "trigger": "user_message",
                    "source": "api",
                    "service_principal": "scheduler",
                },
            )

        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.author == "alice"
        assert event.author_id == "alice"
        assert event.service_principal is None
        auth_context = create_auth_context(event, resolver, enforce=True)
        assert auth_context.roles == ("user",)
        assert auth_context.is_service is False

    @pytest.mark.asyncio
    async def test_empty_body_returns_400(self) -> None:
        """A totally empty JSON object has no channel_id → 400."""
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/event", json={})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_event_routed_through_auth_middleware(self) -> None:
        """POST /event is NOT in _AUTH_EXEMPT → gated when a key is configured."""
        stub = MagicMock()
        stub.enqueue = AsyncMock(return_value=True)

        app = web.Application(middlewares=[_make_auth_middleware("gatekey")])
        app["dispatcher"] = stub
        app.router.add_post("/event", _handle_event)

        async with TestClient(TestServer(app)) as client:
            # No key → 401
            resp = await client.post(
                "/event", json={"channel_id": "ch"}
            )
        assert resp.status == 401
        stub.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_source_forwarded_but_http_ingress_marker_added(self) -> None:
        """chainlink #890: client-supplied source is forwarded but the HTTP
        ingress marker is added so the dispatcher knows it's untrusted."""
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/event",
                json={"channel_id": "ch", "source": "api", "author": "unknown"},
            )
        event = stub.enqueue.call_args.args[0]
        assert event.source == "api"
        assert event.extra.get(HTTP_EVENT_INGRESS_EXTRA_KEY) == HTTP_EVENT_INGRESS_EXTRA_VALUE
