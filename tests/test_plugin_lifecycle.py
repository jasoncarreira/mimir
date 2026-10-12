"""Lifecycle hooks on the production server and shared agent turn paths."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mimir.models import AgentEvent
from mimir.plugins.manager import LoadedPlugin
from tests.test_plugin_integration import _auth
from tests.test_server import _controlled_server_app, _run_cleanup, _run_startup
from tests.test_turn_prompt_assembly import _make_agent


def _install(manager, *contributors):
    manager.plugins = tuple(
        LoadedPlugin(SimpleNamespace(name=f"provider-{index}"), contributor)
        for index, contributor in enumerate(contributors)
    )


@pytest.mark.asyncio
async def test_startup_preflight_fails_named_and_compensates_before_bundle_close(
    tmp_path, monkeypatch,
):
    app, control = _controlled_server_app(tmp_path, monkeypatch)

    class Provider:
        def startup_preflight(self, bundle):
            assert app["agent_runtime"] is bundle
            control.hit("plugin:preflight")
            raise ValueError("private details")

        def shutdown(self):
            control.hit("plugin:shutdown")

    _install(app["plugin_manager"], Provider())
    with pytest.raises(RuntimeError, match="provider-0.*startup preflight"):
        await _run_startup(app)
    assert control.events.count("plugin:shutdown") == 1
    assert control.events.index("plugin:preflight") < control.events.index("plugin:shutdown")
    assert "liveness:clean" not in control.events
    await _run_cleanup(app)
    assert control.events.count("plugin:shutdown") == 1


@pytest.mark.asyncio
async def test_shutdown_is_ordered_isolated_once_before_marker(tmp_path, monkeypatch):
    app, control = _controlled_server_app(tmp_path, monkeypatch)

    class Provider:
        def __init__(self, index):
            self.index = index

        def shutdown(self):
            control.hit(f"plugin:{self.index}")
            if self.index == 0:
                raise RuntimeError("cannot stop")

    _install(app["plugin_manager"], Provider(0), Provider(1))
    await _run_startup(app)
    await _run_cleanup(app)
    await _run_cleanup(app)
    assert control.events.count("plugin:0") == control.events.count("plugin:1") == 1
    assert control.events.index("plugin:0") < control.events.index("plugin:1") < control.events.index("liveness:clean")


@pytest.mark.asyncio
async def test_health_is_namespaced_and_failure_does_not_expose_exception(tmp_path, monkeypatch):
    app, _ = _controlled_server_app(tmp_path, monkeypatch)
    app["check_worker_executor_health"] = False

    class Healthy:
        def health(self):
            return {"ready": True, "jobs": 2}

    class Broken:
        def health(self):
            raise RuntimeError("secret-key-123")

    _install(app["plugin_manager"], Healthy(), Broken())
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/health")
        payload = await response.json()
    assert response.status == 200
    assert payload == {"ok": True, "plugins": {
        "provider-0": {"ready": True, "jobs": 2},
        "provider-1": {"error": "health contributor failed"},
    }}
    assert "secret-key-123" not in str(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["approve request-123", "ordinary turn"])
async def test_shared_turn_preflight_blocks_reply_and_prompt(tmp_path, monkeypatch, content):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    agent = _make_agent(tmp_path)
    calls = []

    class Provider:
        def turn_preflight(self, event, auth_context):
            calls.append((event.content, auth_context.channel_id))
            raise ValueError("denied")

    _install(agent._config.plugin_manager, Provider())
    monkeypatch.setattr("mimir.operator_approval._is_authenticated_operator", lambda *a, **kw: True)
    monkeypatch.setattr("mimir.memory_proposals.sync_pending", lambda *a: pytest.fail("approval touched"))
    monkeypatch.setattr(agent, "_run_turn_body", lambda *a: pytest.fail("prompt touched"))
    event = AgentEvent(trigger="user_message", channel_id="ch", content=content)
    with pytest.raises(RuntimeError, match="provider-0.*turn preflight"):
        await agent.run_turn(event)
    assert calls == [(content, "ch")]


@pytest.mark.asyncio
async def test_acp_bound_turn_uses_shared_preflight_before_body(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    agent = _make_agent(tmp_path)
    seen = []

    class Provider:
        async def turn_preflight(self, event, auth_context):
            seen.append(auth_context.saga_session_id)
            raise PermissionError("not admitted")

    _install(agent._config.plugin_manager, Provider())
    monkeypatch.setattr(agent, "_run_turn_body", lambda *a: pytest.fail("body entered"))
    event = AgentEvent(
        trigger="acp_session", channel_id="ch", content="task",
        continuation_auth_context=_auth(),
    )
    with pytest.raises(RuntimeError, match="provider-0.*turn preflight"):
        await agent.run_turn(event, session_id="session", saga_session_id="saga")
    assert seen == ["saga"]
