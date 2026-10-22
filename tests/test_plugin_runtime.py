"""Selected plugin factories join the model surface without changing core defaults."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import replace
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


def _install_example_distribution(root):
    """Build a real wheel with all uv writes confined to worker-owned fixture state."""
    root.mkdir(parents=True, exist_ok=True)
    # CI's non-owning worker has HOME=/nonexistent and a read-only checkout.
    # Both subprocesses need a private cache, not HOME or an inherited uv cache.
    env = {**os.environ, "UV_CACHE_DIR": str(root / "uv-cache"),
           "UV_PYTHON": sys.executable}
    subprocess.run(["uv", "build", "--wheel", str(EXAMPLE), "--out-dir", str(root)],
                   env=env, check=True)
    wheel, = root.glob("*.whl")
    target = root / "site"
    subprocess.run(["uv", "pip", "install", "--no-deps", "--target", str(target), str(wheel)],
                   env=env, check=True)
    return target


@pytest.fixture(scope="module")
def example_distribution(tmp_path_factory):
    return _install_example_distribution(tmp_path_factory.mktemp("example-distribution"))


@pytest.mark.parametrize("inherited_cache", [False, True])
def test_example_distribution_build_with_unusable_home(tmp_path, monkeypatch, inherited_cache):
    # A file cannot serve as a cache parent, even when pytest runs as root.
    # This reproduces the worker-UID job's unwritable HOME without changing uid.
    home = tmp_path / "unusable-home"
    home.write_text("not a directory")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    if inherited_cache:
        monkeypatch.setenv("UV_CACHE_DIR", str(home / "controller-cache"))
    else:
        monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    target = _install_example_distribution(tmp_path / "distribution")
    assert (target / "mimir_example_plugin" / "__init__.py").is_file()
    assert list(target.glob("mimir_example_plugin-*.dist-info/METADATA"))
    assert home.read_text() == "not a directory"


def test_installed_example_provider_end_to_end(example_distribution, tmp_path, monkeypatch):
    from importlib import metadata

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


@pytest.mark.asyncio
async def test_installed_example_poller_scheduler_and_live_state(example_distribution, tmp_path, monkeypatch):
    from mimir.access_control import create_auth_context
    from mimir.models import AgentEvent, TurnContext
    from mimir.plugins.manager import load_plugins
    from mimir.scheduler import Scheduler

    monkeypatch.syspath_prepend(str(example_distribution))
    policy = load_plugins(enable="example").policy
    poller_home = tmp_path / "skills"
    from mimir.pollers import discover_pollers
    poller, = discover_pollers(poller_home, policy=policy)
    principal = poller.resolved_authority()
    assert principal.authority_profile == "example-reader"
    assert principal.capabilities == ("example_read",)
    assert poller.hooks is not None and poller.hooks.recovery_relevance is not None

    scheduler_yaml = tmp_path / "scheduler.yaml"
    scheduler_yaml.write_text("- name: example-cron\n  callable: example-tick\n  cron: '0 6 * * *'\n")
    sched = Scheduler(scheduler_yaml=scheduler_yaml, enqueue=lambda event: asyncio.sleep(0, result=True),
                      policy=policy)
    assert sched.bind_plugin_jobs(policy.scheduler_jobs.values()) == 1
    job = sched._scheduler.get_job("example-tick")
    assert job is not None and job.func is policy.scheduler_jobs["example-tick"].callback
    assert "hour='6'" in str(job.trigger)
    assert sched.add_poller_jobs(poller_home) == 1
    assert sched.registered_pollers() == ["example-watch"]
    assert sched._scheduler.get_job("poller:example-watch") is not None
    assert sched._pollers["example-watch"].resolved_authority().authority_profile == "example-reader"

    slot = policy.state_slots["example"]
    # The frozen registry does not issue registrars, but slot writes still
    # validate a registrar's owner and selected-policy identity.
    from mimir.plugins.registry import PolicyRegistrar
    owner = PolicyRegistrar(policy, "example")
    foreign = PolicyRegistrar(policy, "foreign")
    event = AgentEvent(trigger="user_message", channel_id="channel", author="user")
    auth = create_auth_context(event, policy=policy)
    with pytest.raises(TypeError, match="registered slot type"):
        auth.with_plugin_state(owner, object())
    with pytest.raises(TypeError, match="registered slot type"):
        auth.with_plugin_state(foreign, slot("foreign"))
    impostor = PolicyRegistrar(PolicyRegistry(), "example")
    with pytest.raises(ValueError, match="matching owner registrar"):
        auth.with_plugin_state(impostor, slot("foreign"))
    bound = auth.with_plugin_state(owner, slot("inbound"))
    turn = TurnContext("turn", "channel", event.trigger, event.channel_id, 0, auth_context=bound)
    assert turn.plugin_state("example") == slot("inbound")
    turn.set_plugin_state(owner, slot("live"))
    with pytest.raises(TypeError, match="registered slot type"):
        turn.set_plugin_state(owner, object())
    with pytest.raises(TypeError, match="registered slot type"):
        turn.set_plugin_state(foreign, slot("foreign"))
    assert turn.auth_with_plugin_state().plugin_state("example") == slot("live")
    assert replace(turn.auth_with_plugin_state(), trigger="continuation").plugin_state("example") == slot("live")
    assert auth.plugin_state("example") is None


@pytest.mark.asyncio
async def test_installed_example_http_web_and_turn_prompt(example_distribution, tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from mimir import web_ui
    from mimir.identities import IdentityResolver
    from mimir.identities_populator import issue_web_key
    from mimir.models import AgentEvent, Integrity
    from mimir.plugins.core import policy_scope
    from mimir.plugins.manager import load_plugins
    from mimir.server import _make_auth_middleware
    from tests.test_turn_prompt_assembly import _make_ctx

    monkeypatch.syspath_prepend(str(example_distribution))
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.delenv("MIMIR_WEB_DEFAULT_SKIN", raising=False)
    manager = load_plugins(enable="example")
    app = web.Application()
    app["config"] = SimpleNamespace(plugin_manager=manager)
    app["api_key"] = "example-master-key"
    user_key = issue_web_key(tmp_path, "user", roles=["user"])
    resolver = IdentityResolver(tmp_path)
    resolver.reload()
    app["identity_resolver"] = resolver
    app.middlewares.append(_make_auth_middleware("example-master-key"))
    web_ui.register_routes(app, turns_log=tmp_path / "turns.jsonl", events_log=tmp_path / "events.jsonl", home=tmp_path)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/v1/plugins/example/status")).status == 401
        response = await client.get("/api/v1/plugins/example/status", headers={"X-API-Key": user_key})
        assert response.status == 200 and await response.json() == {"provider": "example"}
        headers = {"X-API-Key": "example-master-key"}
        bootstrap = (await (await client.get("/api/v1/web/bootstrap", headers=headers)).json())["data"]
        assert bootstrap["ui"]["default_skin"] == "example-skin"
        skin, = bootstrap["skins"]["plugins"]
        assert skin["plugin"] == "example" and skin["name"] == "Example"
        assert set(skin["tokens"]) == web_ui.SKIN_TOKEN_NAMES
        assert bootstrap["plugin_bundles"] == [{
            "plugin": "example", "apiMajor": 1,
            "moduleUrl": "/plugins/example/assets/frontend/example.v1.js?v=0.1.0",
            "cssUrl": "/plugins/example/assets/frontend/example.v1.css?v=0.1.0",
        }]
        assert skin["characterRenderer"]["assets"][0]["href"] == "/plugins/example/assets/skins/character.svg?v=0.1.0"
        assert "fonts" not in skin
        assert any(entry["id"] == "example-page" and entry["plugin"] == "example"
                   for entry in bootstrap["dashboard_extensions"])
        for resource in ("skins/character.svg", "frontend/example.v1.js", "frontend/example.v1.css"):
            response = await client.get(f"/plugins/example/assets/{resource}?v=0.1.0", headers=headers)
            assert response.status == 200
            assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
            assert await response.read()
        assert (await client.get("/plugins/example/assets/frontend/example.v1.js?v=wrong", headers=headers)).headers[
            "Cache-Control"].startswith("no-store")

    memory = tmp_path / "memory/core/00-identity.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("Trusted example identity")
    cfg = replace(Config.from_env(), plugin_manager=manager)
    agent = Agent(config=cfg, turn_logger=TurnLogger(cfg.turns_log),
                  message_buffer=MessageBuffer(history_path=tmp_path / "messages.jsonl"),
                  index_generator=IndexGenerator(tmp_path))
    event = AgentEvent(trigger="user_message", channel_id="channel", author="user", content="hello")
    ctx = _make_ctx(event)
    with policy_scope(manager.policy):
        prompt, _ = await agent._build_turn_prompt(ctx, event, None)
    assert "## Plugin: example_memory\n\nExample memory: Trusted example identity" in prompt
    sources = [source for source in ctx.ifc_labels.sources if source.domain == "example_memory"]
    assert len(sources) == 1
    assert sources[0].resource_id == str(memory)
    assert sources[0].integrity is Integrity.TRUSTED
    assert sources[0].integrity_effect.value == "informational"


def test_installed_example_scaffold_service_wrapper(example_distribution, tmp_path, monkeypatch):
    from mimir.plugin_dockerfile import render_stage, select_plugins
    from mimir.scaffold_docker import scaffold

    monkeypatch.syspath_prepend(str(example_distribution))
    monkeypatch.setenv("MIMIR_PLUGINS", "example")
    selected = select_plugins()
    assert [candidate.name for candidate in selected] == ["example"]
    assert "ca-certificates" in render_stage(selected, "base")
    build = render_stage(selected, "build")
    assert all(item in build for item in ("EXAMPLE_GREETING", "example_service", "/var/lib/example-service",
                                          "chmod 755 /var/lib/example-service"))
    assert render_stage(selected, "runtime") == ""
    assert "example-example-worker" in render_stage(selected, "services")
    home = tmp_path / "home"
    home.mkdir()
    result = scaffold(home, mode="pypi")
    assert result.plugins_with_services == ["example-example-worker"]
    dockerfile = (home / "Dockerfile").read_text()
    assert "mimir-example-plugin==0.1.0" in dockerfile
    assert "ca-certificates" in dockerfile
    assert "example_service" in dockerfile
    assert 'ENTRYPOINT ["/init"]' in dockerfile
    context = home / ".mimir-scaffold"
    assert (context / "s6-rc.d/user/contents.d/example-example-worker").is_file()
    copied = context / "plugin-services/example/example-worker/run"
    source = example_distribution / "mimir_example_plugin/services/example-worker/run"
    assert copied.read_bytes() == source.read_bytes()
    wrapper = context / "s6-rc.d/example-example-worker/run"
    image_script = "/usr/local/libexec/mimir-plugin-services/example/example-worker/run"
    assert f"exec s6-setuidgid mimir {image_script}" in wrapper.read_text()
    shim = tmp_path / "bin"
    shim.mkdir()
    shim_script = shim / "s6-setuidgid"
    shim_script.write_text('#!/bin/sh\nprintf "%s" "$1" > "$SHIM_USER"\nshift\nexec "$@"\n')
    shim_script.chmod(0o755)
    local_wrapper = tmp_path / "wrapper"
    local_wrapper.write_text(wrapper.read_text().replace(image_script, str(copied)))
    local_wrapper.chmod(0o755)
    identity = tmp_path / "user"
    completed = subprocess.run([str(local_wrapper)], env={**os.environ, "EXAMPLE_GREETING": "installed",
        "SHIM_USER": str(identity), "PATH": f"{shim}:{os.environ['PATH']}"}, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert identity.read_text() == "mimir"
    assert completed.stdout.strip() == "example worker: installed"


@pytest.mark.asyncio
async def test_process_selected_installed_example_reaches_server_startup(example_distribution, tmp_path, monkeypatch):
    """Process selection, not a hand-built policy, reaches real server route and scheduler wiring."""
    from aiohttp.test_utils import TestClient, TestServer
    from mimir.pollers import discover_pollers
    from tests.test_server import _ServerControl, _controlled_server_app, _run_cleanup

    monkeypatch.syspath_prepend(str(example_distribution))
    monkeypatch.setenv("MIMIR_PLUGINS", "example")
    control = _ServerControl(real_web_routes=True, plugin_jobs_installed=1,
                             scheduler_should_start=False)
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)
    manager = app["plugin_manager"]
    assert [plugin.candidate.name for plugin in manager.plugins] == ["example"]
    assert "example-tick" in manager.policy.scheduler_jobs
    assert "example-watch" in [poller.name for poller in discover_pollers(
        tmp_path / "skills", policy=manager.policy)]
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/v1/plugins/example/status", headers={"X-API-Key": "test-key"})
            assert response.status == 200
            assert await response.json() == {"provider": "example"}
            bootstrap = (await (await client.get("/api/v1/web/bootstrap",
                                           headers={"X-API-Key": "test-key"})).json())["data"]
            assert bootstrap["ui"]["default_skin"] == "example-skin"
            assert bootstrap["plugin_bundles"][0]["plugin"] == "example"
            assert "scheduler:bind_plugin_jobs" in control.events
            assert "scheduler:start" in control.events
            assert control.poller_candidates == manager.policy.selected_plugin_candidates
    finally:
        await _run_cleanup(app)


def test_disabled_installed_example_does_not_import_provider(example_distribution, tmp_path):
    """A fresh interpreter owns the entire import inventory, not the pytest process."""
    script = '''
import asyncio
import contextlib
import io
from pathlib import Path
import sys
from types import SimpleNamespace
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from mimir import web_ui
from mimir.cli import main as cli_main
from mimir.plugins.config_api import plugin_config_rows
from mimir.plugins.defaults import seed_plugin_defaults
from mimir.plugins.discovery import discover_plugins
from mimir.plugins.manager import load_plugins
from mimir.plugin_dockerfile import select_plugins
from mimir.pollers import discover_pollers
from mimir.scheduler import Scheduler
home = Path(sys.argv[1])
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
assert "example" in {candidate.name for candidate in select_plugins(enable="", all_installed=True)}
assert discover_pollers(home / "skills", policy=manager.policy) == []
with contextlib.redirect_stderr(io.StringIO()):
    try:
        cli_main(["example-hello"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("disabled CLI command dispatched")
assert seed_plugin_defaults(home, selected=manager.policy.selected_plugin_candidates) == {}
assert not (home / "skills/example-example-watch").exists()
try:
    plugin_config_rows("example", {})
except ValueError:
    pass
else:
    raise AssertionError("disabled config leaked")
async def check_host():
    scheduler = Scheduler(home / "scheduler.yaml", enqueue=lambda event: asyncio.sleep(0, result=True),
                          policy=manager.policy)
    assert scheduler.bind_plugin_jobs(manager.policy.scheduler_jobs.values()) == 0
    assert scheduler.add_poller_jobs(home / "skills") == 0
    assert scheduler.registered_pollers() == []
    app = web.Application()
    app["config"] = SimpleNamespace(plugin_manager=manager)
    web_ui.register_routes(app, turns_log=home / "turns.jsonl", events_log=home / "events.jsonl", home=home)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/v1/plugins/example/status")).status == 404
        bootstrap = (await (await client.get("/api/v1/web/bootstrap")).json())["data"]
        assert bootstrap["ui"]["default_skin"] == "neon-terminal"
        assert not bootstrap["skins"]["plugins"]
        assert not bootstrap["plugin_bundles"]
        assert "example-page" not in {entry["id"] for entry in bootstrap["dashboard_extensions"]}
        assert (await client.get("/plugins/example/assets/skins/character.svg?v=0.1.0")).status == 404
asyncio.run(check_host())
assert "mimir_example_plugin" not in sys.modules
'''
    env = {**os.environ, "PYTHONPATH": str(example_distribution),
           "MIMIR_PLUGINS": "", "MIMIR_HOME": str(tmp_path)}
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unselected_third_party_seeds_are_skipped_without_import(example_distribution, tmp_path):
    """Any plugin's seeded home pollers keep its identity once it is deselected.

    Covers the installed-but-unselected case (package metadata names the seed)
    and the uninstalled case (only the home's seed marker names it). Fresh
    interpreters own the import claim.
    """
    script = '''
import sys
from pathlib import Path
from mimir.plugins.discovery import enabled_plugins
from mimir.plugins.defaults import seed_plugin_defaults
from mimir.plugins.manager import load_plugins
from mimir.pollers import discover_pollers
mode, home = sys.argv[1], Path(sys.argv[2])
if mode == "seed":
    seed_plugin_defaults(home, selected=enabled_plugins(enable="example"))
    assert (home / "skills/example-example-watch/pollers.json").is_file()
else:
    invalid = []
    pollers = discover_pollers(home / "skills", policy=load_plugins(enable="").policy,
                               invalid_entries=invalid)
    assert [p.name for p in pollers] == ["operator-watch"], pollers
    assert [(path.parent.name, name) for path, name, reason in invalid
            if "seeded by plugin 'example'" in reason] == [("example-example-watch", "<manifest>")], invalid
    assert "mimir_example_plugin" not in sys.modules
'''
    home = tmp_path / "home"
    operator = home / "skills" / "operator-watch"
    operator.mkdir(parents=True)
    (operator / "pollers.json").write_text(json.dumps({"pollers": [{
        "name": "operator-watch", "command": "python3 poller.py", "cron": "*/5 * * * *",
    }]}), encoding="utf-8")
    installed = {**os.environ, "PYTHONPATH": str(example_distribution),
                 "MIMIR_PLUGINS": "", "MIMIR_HOME": str(home)}
    uninstalled = {key: value for key, value in installed.items() if key != "PYTHONPATH"}
    for mode, env in (("seed", installed), ("installed", installed), ("uninstalled", uninstalled)):
        result = subprocess.run([sys.executable, "-c", script, mode, str(home)],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0, f"{mode}: {result.stderr[-4000:]}"


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
