"""Selected plugin factories join the model surface without changing core defaults."""

from __future__ import annotations

from dataclasses import replace
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.tools import StructuredTool
from langgraph.runtime import Runtime

from mimir.access_control import CapabilityMatrixError, ServicePrincipal, assert_model_tool_inventory_cataloged
from mimir.agent import Agent
from mimir.config import Config
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.plugins.core import register_core_tools
from mimir.plugins.manager import PluginManager
from mimir.plugins.registry import PolicyRegistry
from mimir.models import AuthContext
from mimir.subagents import build_mimir_subagents
from mimir.tool_descriptors import ResultOriginKind, SinkCategory
from mimir.tools.registry import all_mimir_tools
from mimir.tools.service_tool_surface import ServiceToolSurfaceMiddleware
from mimir.turn_logger import TurnLogger


EXAMPLE = Path(__file__).parent / "fixtures" / "plugins" / "example"


@pytest.fixture(scope="module")
def example_distribution(tmp_path_factory):
    """Build and install an actual provider wheel without installing it into Mimir's venv."""
    root = tmp_path_factory.mktemp("example-distribution")
    subprocess.run(["uv", "build", "--wheel", str(EXAMPLE), "--out-dir", str(root)], check=True)
    wheel, = root.glob("*.whl")
    target = root / "site"
    subprocess.run(["uv", "pip", "install", "--no-deps", "--target", str(target), str(wheel)], check=True)
    return target


def test_installed_example_provider_end_to_end(example_distribution, tmp_path, monkeypatch):
    from importlib import metadata
    import json

    from mimir.plugin_dockerfile import pinned_packages, render_stage, services
    from mimir.plugins.config_api import plugin_config_rows, validate_plugin_config_write
    from mimir.plugins.defaults import plugin_default_files, seed_plugin_defaults
    from mimir.plugins.discovery import enabled_plugins
    from mimir.pollers import discover_pollers
    from mimir.poller_hooks import resolve_poller_hooks
    from mimir.agent import _render_plugin_prompt
    from mimir.models import AgentEvent

    monkeypatch.syspath_prepend(str(example_distribution))
    assert metadata.distribution("mimir-example-plugin").files
    candidate, = enabled_plugins(enable="example")
    assert candidate.distribution == "mimir-example-plugin"
    assert candidate.manifest.skin_defaults == ("example-skin",)
    manager = __import__("mimir.plugins.manager", fromlist=["load_plugins"]).load_plugins(enable="example")
    policy = manager.policy
    assert len(manager.plugins) == 1
    assert policy.frozen
    tool = next(t for t in all_mimir_tools(policy=policy) if t.name == "example_read")
    assert tool.invoke({}) == "example-result"
    assert policy.capabilities["example_read"] == "example"
    assert policy.profiles["example-reader"].capabilities == frozenset({"example_read"})
    assert policy.state_slots["example"].__dataclass_params__.frozen
    assert policy.state_slots["example"]("hi").greeting == "hi"

    skill_manifest = example_distribution / "mimir_example_plugin/skills/example-watch/pollers.json"
    hooks = resolve_poller_hooks("example-hooks", skill_manifest, policy=policy, plugin_owner="example")
    assert hooks.recovery_relevance is not None
    assert json.loads(skill_manifest.read_text())["pollers"][0]["authority"]["profile"] == "example-reader"
    rejected = []
    pollers = discover_pollers(tmp_path / "skills", policy=policy, invalid_entries=rejected)
    assert not rejected
    assert [poller.name for poller in pollers] == ["example-watch"]

    job = policy.scheduler_jobs["example-tick"]
    assert (job.owner, job.job_id) == ("example", "example-tick")
    assert callable(job.callback)
    asyncio.run(job.callback())
    command = policy.cli_commands["example-hello"]
    import argparse
    parser = argparse.ArgumentParser()
    command.configure(parser.add_subparsers(dest="command"))
    assert command.dispatch(parser.parse_args(["example-hello", "--name", "Mimir"])) == 0
    cli_env = {**os.environ, "PYTHONPATH": str(example_distribution), "MIMIR_PLUGINS": "example"}
    cli = subprocess.run([sys.executable, "-c", "from mimir.entrypoint import main; main()",
                          "example-hello", "--name", "Mimir"],
                         env=cli_env, capture_output=True, text=True)
    assert cli.returncode == 0, cli.stderr
    assert "hello, Mimir" in cli.stdout
    route = policy.routes[("GET", "/api/v1/plugins/example/status")]
    assert route.owner == "example" and route.role == "user"
    assert json.loads(asyncio.run(route.handler(None)).text) == {"provider": "example"}
    assert policy.dashboard_entries["example-page"][1].route_path == "/example"
    provider = policy.prompt_providers["example_memory"]
    (tmp_path / "memory/core").mkdir(parents=True)
    (tmp_path / "memory/core/00-identity.md").write_text("Trusted example identity")
    text, served = _render_plugin_prompt(provider, object(), AgentEvent(
        trigger="user_message", channel_id="test", content="hello"), tmp_path)
    assert "Trusted example identity" in text
    assert served == (str(tmp_path / "memory/core/00-identity.md"),)

    skin = json.loads((example_distribution / "mimir_example_plugin/skins/example.json").read_text())
    assert skin["default"] and len(skin["tokens"]) == 66
    from mimir.plugins.manifest import _SKIN_TOKENS
    assert set(skin["tokens"]) == _SKIN_TOKENS
    assert skin["characterRenderer"]["assets"][0]["href"] == "skins/character.svg"
    assert candidate.manifest.frontend["api_major"] == 1
    module = example_distribution / "mimir_example_plugin" / candidate.manifest.frontend["module"]
    assert 'host.registerRoute({ plugin: "example"' in module.read_text()
    assert (example_distribution / "mimir_example_plugin" / candidate.manifest.frontend["css"]).is_file()
    assert candidate.manifest.dockerfile["s6_services"] == ["services/example-worker"]
    assert pinned_packages((candidate,)) == ("mimir-example-plugin==0.1.0",)
    assert "ca-certificates" in render_stage((candidate,), "base")
    assert "EXAMPLE_GREETING" in render_stage((candidate,), "build")
    service, = services((candidate,))
    assert service.name == "example-worker" and os.access(service.run, os.X_OK)
    worker = subprocess.run([str(service.run)], env={**os.environ, "EXAMPLE_GREETING": "hello"},
                            capture_output=True, text=True)
    assert worker.returncode == 0 and worker.stdout.strip() == "example worker: hello"
    defaults = plugin_default_files(candidate)
    assert Path("skills/example-example-watch/SKILL.md") in defaults
    assert Path("prompts/example/example.md") in defaults
    (tmp_path / "scheduler.yaml").write_text("[]\n")
    seed_plugin_defaults(tmp_path, selected=(candidate,))
    assert (tmp_path / "skills/example-example-watch/SKILL.md").is_file()
    assert (tmp_path / "prompts/example/example.md").is_file()
    assert "example-example-daily" in (tmp_path / "scheduler.yaml").read_text()
    assert candidate.manifest.config["MIMIR_PLUGIN_EXAMPLE_SECRET"]["secret"]
    monkeypatch.setenv("MIMIR_PLUGIN_EXAMPLE_GREETING", "hello")
    assert plugin_config_rows("example", {"example": candidate.manifest})[0]["value"] == "hello"
    monkeypatch.setenv("MIMIR_PLUGIN_EXAMPLE_SECRET", "private")
    assert plugin_config_rows("example", {"example": candidate.manifest})[1]["value"] == "[REDACTED]"
    with pytest.raises(ValueError, match="not writable"):
        validate_plugin_config_write("example", "MIMIR_PLUGIN_EXAMPLE_SECRET", {"example": candidate.manifest})


def test_disabled_installed_example_does_not_import_provider(example_distribution):
    """A fresh interpreter owns the entire import inventory, not the pytest process."""
    script = '''
import sys
from mimir.plugins.discovery import discover_plugins
from mimir.plugins.manager import load_plugins
from mimir.plugin_dockerfile import select_plugins
assert any(c.name == "example" and not c.enabled for c in discover_plugins(enable=""))
manager = load_plugins(enable="")
assert not manager.plugins
assert "example_read" not in manager.policy.tools
assert not manager.policy.scheduler_jobs
assert not manager.policy.routes
assert not manager.policy.dashboard_entries
assert not manager.policy.prompt_providers
assert not manager.policy.state_slots
assert "example-hello" not in manager.policy.cli_commands
assert not select_plugins(enable="")
assert "mimir_example_plugin" not in sys.modules
'''
    env = {**os.environ, "PYTHONPATH": str(example_distribution),
           "MIMIR_PLUGINS": ""}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _policy(factory, *, result_domain="plugin-data", service_constraints=False, boundary=False):
    registry = PolicyRegistry()
    register_core_tools(registry)
    registry.registrar("sample").register_tool(
        name="sample_read", factory=factory,
        flow="both" if service_constraints else "source", tier="scope-contained",
        operation_class="open", result_domain=result_domain,
        result_origin=ResultOriginKind.EXTERNAL if service_constraints else ResultOriginKind.NONE,
        sink_category=SinkCategory.NETWORK if service_constraints else None,
        sink_destination="plugin-network" if service_constraints else None,
        sink_target_extractor=(lambda _name, args, _context: (args.get("target"),))
        if service_constraints else None,
        sink_payload_extractor=(lambda _name, args, _context: (args.get("body", ""),))
        if service_constraints else None,
        writes_code_or_state=False, profiles=(), read_backend=False,
        readable_domain="plugin-data" if service_constraints else None,
    )
    if boundary:
        registrar = registry.registrar("sample")
        registrar.register_capability("sample_read")
        registrar.add_to_profile("session-boundary", "sample_read")
    return registry.freeze()


def test_selected_factory_is_not_invoked_by_enforced_inventory_and_is_assembled_once():
    calls = []

    def factory():
        calls.append(True)
        return SimpleNamespace(name="sample_read")

    policy = _policy(factory)
    assert_model_tool_inventory_cataloged(policy=policy)
    assert calls == []
    tools = all_mimir_tools(policy=policy)
    assert calls == [True]
    assert [tool.name for tool in tools].count("sample_read") == 1
    assert "sample_read" not in {tool.name for tool in all_mimir_tools()}
    assert policy.tools["sample_read"].result_domain == "plugin-data"
    assert policy.tools["sample_read"].descriptor.result_origin == ResultOriginKind.NONE


def test_plugin_factory_must_match_declared_identity():
    policy = _policy(lambda: SimpleNamespace(name="other"))
    with pytest.raises(ValueError, match="plugin sample: factory for sample_read returned 'other'"):
        all_mimir_tools(policy=policy)


def test_enforced_inventory_rejects_unclassified_plugin_result():
    policy = _policy(lambda: SimpleNamespace(name="sample_read"), result_domain=None)
    with pytest.raises(CapabilityMatrixError, match="plugin sample: tool sample_read has incomplete model policy"):
        assert_model_tool_inventory_cataloged(policy=policy)


@pytest.mark.parametrize("enforced", [False, True])
@pytest.mark.parametrize("profile", ["research", "session-boundary"])
def test_service_surface_checks_plugin_domain_and_destination_before_core_fallback(
    enforced, profile,
):
    tool = StructuredTool.from_function(lambda: "result", name="sample_read", description="Test plugin")
    policy = _policy(lambda: tool, service_constraints=True)
    principal = ServicePrincipal(
        canonical="poller:sample", trigger="poller", authority_profile=profile,
        capabilities=("sample_read",), readable_domains=("plugin-data",),
        sink_destinations=("plugin-network",),
    )
    middleware = ServiceToolSurfaceMiddleware(policy)

    def visible(service, surface=middleware):
        context = AuthContext(
            principal=service.canonical, canonical_principal=service.canonical,
            roles=("service",), event_ingress=None, trigger="poller",
            channel_id="test", interactivity=None, is_service=True,
            service_authority=service, enforcement_enabled=enforced,
        )
        request = ModelRequest(
            model=SimpleNamespace(), messages=[], tools=[tool], runtime=Runtime(context=context),
        )
        return [item.name for item in surface._filter_request(request).tools]

    assert visible(principal) == ["sample_read"]
    assert visible(replace(principal, readable_domains=())) == []
    assert visible(replace(principal, sink_destinations=())) == []
    assert visible(replace(principal, capabilities=())) == []

    child = build_mimir_subagents(policy=policy)[0]
    child_surface = next(item for item in child["middleware"]
                         if isinstance(item, ServiceToolSurfaceMiddleware))
    assert child_surface._policy is policy
    assert visible(principal, child_surface) == ["sample_read"]
    assert visible(replace(principal, readable_domains=()), child_surface) == []
    assert visible(replace(principal, sink_destinations=()), child_surface) == []


@pytest.mark.asyncio
async def test_agent_graph_assembles_selected_policy_for_parent_and_children(tmp_path, monkeypatch):
    import deepagents

    tool = StructuredTool.from_function(lambda: "result", name="sample_read", description="Test plugin")
    policy = _policy(lambda: tool, service_constraints=True)
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    config = replace(Config.from_env(), plugin_manager=PluginManager(policy, ()))
    agent = Agent(
        config=config, turn_logger=TurnLogger(config.turns_log),
        message_buffer=MessageBuffer(history_path=tmp_path / "messages.jsonl"),
        index_generator=IndexGenerator(tmp_path),
    )
    monkeypatch.setattr(agent, "_current_system_prompt", lambda **_kwargs: "test prompt")
    monkeypatch.setattr("mimir.agent.resolve_model_from_config", lambda *args, **kwargs: object())
    captured = []
    graph = object()
    monkeypatch.setattr(deepagents, "create_deep_agent",
                        lambda **kwargs: captured.append(kwargs) or graph)

    assert await agent._build_agent_if_needed() is graph
    assert [item.name for item in captured[0]["tools"]].count("sample_read") == 1
    assert next(item for item in captured[0]["middleware"]
                if isinstance(item, ServiceToolSurfaceMiddleware))._policy is policy
    for child in captured[0]["subagents"]:
        assert next(item for item in child["middleware"]
                    if isinstance(item, ServiceToolSurfaceMiddleware))._policy is policy


def test_factory_observes_selected_module_views_and_nested_selection_restores():
    from mimir.access_control import _TOOL_FLOW_MAP
    from mimir.plugins.core import core_policy_registry, policy_scope, selected_policy_registry
    from mimir.tool_descriptors import TOOL_DESCRIPTORS

    seen = []
    def factory():
        seen.append(selected_policy_registry())
        assert _TOOL_FLOW_MAP["sample_read"] == "source"
        assert "sample_read" in TOOL_DESCRIPTORS
        assert "sample_read" not in {tool.name for tool in all_mimir_tools(policy=core_policy_registry())}
        assert selected_policy_registry() is seen[-1]
        return SimpleNamespace(name="sample_read")

    selected = _policy(factory)
    with policy_scope(core_policy_registry()):
        all_mimir_tools(policy=selected)
        assert selected_policy_registry() is core_policy_registry()
        assert "sample_read" not in TOOL_DESCRIPTORS
    assert seen == [selected]


@pytest.mark.asyncio
async def test_production_synthesis_renderer_uses_agent_policy_without_view_monkeypatch(tmp_path, monkeypatch):
    from mimir.models import AgentEvent, InformationFlowLabels
    from mimir.plugins.core import core_policy_registry, selected_policy_registry
    from tests.test_turn_prompt_assembly import _make_agent

    agent = _make_agent(tmp_path)
    selected = _policy(lambda: SimpleNamespace(name="sample_read"), boundary=True)
    agent._config.plugin_manager = PluginManager(selected, ())
    monkeypatch.setattr("mimir.agent._filter_session_turns", lambda *args, **kwargs: [])
    ctx = SimpleNamespace(saga_session_id="session", ifc_labels=InformationFlowLabels())
    event = AgentEvent(trigger="saga_session_end", channel_id="channel", content="close")
    prompt = await agent._build_synthesis_prompt(ctx, event)
    assert "- `sample_read`\n" in prompt.content
    assert selected_policy_registry() is core_policy_registry()
    agent._config.plugin_manager = PluginManager(core_policy_registry(), ())
    prompt = await agent._build_synthesis_prompt(ctx, event)
    assert "- `sample_read`\n" not in prompt.content


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_name", ["startup_preflight", "turn_preflight", "health", "shutdown"])
@pytest.mark.parametrize("async_hook", [False, True])
async def test_lifecycle_hooks_use_manager_policy_and_restore_after_await_or_thread(hook_name, async_hook):
    from mimir.plugins.core import core_policy_registry, selected_policy_registry
    from mimir.plugins.manager import LoadedPlugin
    from mimir.tool_descriptors import TOOL_DESCRIPTORS

    selected = _policy(lambda: SimpleNamespace(name="sample_read"))
    seen = []
    def hook(*args):
        seen.append(selected_policy_registry())
        assert "sample_read" in TOOL_DESCRIPTORS
        return {"ready": True} if hook_name == "health" else None
    async def async_call(*args):
        import asyncio
        await asyncio.sleep(0)
        return hook(*args)
    contributor = SimpleNamespace(**{hook_name: async_call if async_hook else hook})
    manager = PluginManager(selected, (LoadedPlugin(SimpleNamespace(name="sample"), contributor),))
    args = {"startup_preflight": (object(),), "turn_preflight": (object(), object()),
            "health": (), "shutdown": ()}[hook_name]
    await getattr(manager, hook_name)(*args)
    assert seen == [selected]
    assert selected_policy_registry() is core_policy_registry()


@pytest.mark.asyncio
async def test_agent_turn_selects_policy_before_reply_paths_and_resets_on_exception(tmp_path, monkeypatch):
    from mimir.models import AgentEvent
    from mimir.plugins.core import core_policy_registry, selected_policy_registry
    from mimir.tool_descriptors import TOOL_DESCRIPTORS
    from tests.test_turn_prompt_assembly import _make_agent

    agent = _make_agent(tmp_path)
    selected = _policy(lambda: SimpleNamespace(name="sample_read"))
    agent._config.plugin_manager = PluginManager(selected, ())
    monkeypatch.setattr("mimir.operator_approval._is_authenticated_operator", lambda *args, **kwargs: True)
    seen = []
    def sync_pending(*args):
        seen.append(selected_policy_registry())
        assert "sample_read" in TOOL_DESCRIPTORS
        raise ValueError("test reply path stop")
    monkeypatch.setattr("mimir.memory_proposals.sync_pending", sync_pending)
    event = AgentEvent(trigger="user_message", channel_id="ch", content="approve update-abcd")
    with pytest.raises(ValueError, match="test reply path stop"):
        await agent.run_turn(event)
    assert seen == [selected]
    assert selected_policy_registry() is core_policy_registry()
    assert "sample_read" not in TOOL_DESCRIPTORS
