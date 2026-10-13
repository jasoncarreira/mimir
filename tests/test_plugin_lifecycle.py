"""Lifecycle hooks on the production server and shared agent turn paths."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mimir.models import AgentEvent
from mimir.approval_requests import is_non_turn_bound_reply
from mimir.chat_skills import CHAT_SKILL_EXTRA_KEY
from mimir.event_logger import init_logger
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
    assert control.events.index("plugin:shutdown") < control.events.index("bundle:close")
    assert "liveness:clean" not in control.events
    await _run_cleanup(app)
    assert control.events.count("plugin:shutdown") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_kind", ["sync", "async"])
async def test_startup_preflight_false_rejects_and_compensates(tmp_path, monkeypatch, hook_kind):
    app, control = _controlled_server_app(tmp_path, monkeypatch)
    seen = []

    def reject(bundle):
        seen.append(bundle)
        control.hit("plugin:preflight")
        return False

    async def reject_async(bundle):
        return reject(bundle)

    provider = SimpleNamespace(
        startup_preflight=reject_async if hook_kind == "async" else reject,
        shutdown=lambda: control.hit("plugin:shutdown"),
    )
    _install(app["plugin_manager"], provider)
    try:
        with pytest.raises(RuntimeError, match="provider-0.*startup preflight") as error:
            await _run_startup(app)
        assert isinstance(error.value.__cause__, ValueError)
        assert str(error.value.__cause__) == "preflight returned False"
        assert seen == [control.bundle]
        assert control.dispatcher._run_turn is None
        assert control.turns == []
        assert "bridges:connect" not in control.events
        assert "indexer:start" not in control.events
        assert control.events.count("plugin:shutdown") == 1
        assert control.events.index("plugin:preflight") < control.events.index("plugin:shutdown")
        assert control.events.index("plugin:shutdown") < control.events.index("bundle:close")
        assert "liveness:clean" not in control.events
    finally:
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
async def test_hook_property_failures_are_named_and_isolated(tmp_path, monkeypatch):
    app, control = _controlled_server_app(tmp_path, monkeypatch)

    class Broken:
        @property
        def startup_preflight(self):
            raise RuntimeError("private startup detail")

        @property
        def shutdown(self):
            raise RuntimeError("private shutdown detail")

    class Later:
        def shutdown(self):
            control.hit("plugin:later-shutdown")

    _install(app["plugin_manager"], Broken(), Later())
    with pytest.raises(RuntimeError, match="provider-0.*startup preflight"):
        await _run_startup(app)
    assert control.events.count("plugin:later-shutdown") == 1
    assert control.events.index("plugin:later-shutdown") < control.events.index("bundle:close")
    await _run_cleanup(app)
    assert control.events.count("plugin:later-shutdown") == 1


@pytest.mark.asyncio
async def test_shutdown_property_failure_does_not_skip_later_hook_or_marker(tmp_path, monkeypatch):
    app, control = _controlled_server_app(tmp_path, monkeypatch)

    class Broken:
        @property
        def shutdown(self):
            control.hit("plugin:lookup-failed")
            raise RuntimeError("private shutdown detail")

    class Later:
        def shutdown(self):
            control.hit("plugin:later-shutdown")

    _install(app["plugin_manager"], Broken(), Later())
    await _run_startup(app)
    await _run_cleanup(app)
    assert control.events.index("plugin:lookup-failed") < control.events.index("plugin:later-shutdown")
    assert control.events.index("plugin:later-shutdown") < control.events.index("liveness:clean")


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

    class BrokenProperty:
        @property
        def health(self):
            raise RuntimeError("secret-health-property")

    class HugeInteger:
        def health(self):
            return {"count": 10 ** 5000}

    class NonFinite:
        def health(self):
            return {"load": float("nan")}

    _install(app["plugin_manager"], Healthy(), Broken(), BrokenProperty(), HugeInteger(), NonFinite())
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/health")
        payload = await response.json()
    assert response.status == 200
    assert payload == {"ok": True, "plugins": {
        "provider-0": {"ready": True, "jobs": 2},
        "provider-1": {"error": "health contributor failed"},
        "provider-2": {"error": "health contributor failed"},
        "provider-3": {"error": "health contributor failed"},
        "provider-4": {"error": "health contributor failed"},
    }}
    assert "secret-key-123" not in str(payload) and "secret-health-property" not in str(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["approve update-abcd", "ordinary turn"])
async def test_shared_turn_preflight_blocks_reply_and_prompt(tmp_path, monkeypatch, content):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    agent = _make_agent(tmp_path)
    calls = []
    panel_events = []
    agent._turn_event_bus = SimpleNamespace(publish=panel_events.append)

    class Provider:
        def turn_preflight(self, event, auth_context):
            calls.append((event.content, auth_context.channel_id))
            raise ValueError("denied")

    _install(agent._config.plugin_manager, Provider())
    monkeypatch.setattr("mimir.operator_approval._is_authenticated_operator", lambda *a, **kw: True)
    monkeypatch.setattr("mimir.memory_proposals.sync_pending", lambda *a: pytest.fail("approval touched"))
    monkeypatch.setattr(agent, "_run_turn_body", lambda *a: pytest.fail("prompt touched"))
    event = AgentEvent(trigger="user_message", channel_id="ch", content=content)
    assert is_non_turn_bound_reply(event) is (content.startswith("approve"))
    with pytest.raises(RuntimeError, match="provider-0.*turn preflight"):
        await agent.run_turn(event)
    assert calls == [(content, "ch")]
    assert [(e["phase"], e.get("status")) for e in panel_events] == [
        ("start", None), ("end", "error"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_kind", ["sync", "async"])
@pytest.mark.parametrize("content", ["approve update-abcd", "ordinary turn"])
async def test_turn_preflight_false_never_runs_turn_runner(
    tmp_path, monkeypatch, hook_kind, content,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    agent = _make_agent(tmp_path)
    agent._config.access_control_enforced = False
    seen = []
    panel_events = []
    agent._turn_event_bus = SimpleNamespace(publish=panel_events.append)

    def reject(event, auth_context):
        seen.append((event, auth_context))
        return False

    async def reject_async(event, auth_context):
        return reject(event, auth_context)

    _install(agent._config.plugin_manager, SimpleNamespace(
        turn_preflight=reject_async if hook_kind == "async" else reject,
    ))
    # Leave run_turn and _run_turn_body intact: observe the actual graph runner
    # boundary, not a substituted turn body that can fail for unrelated reasons.
    stream = Mock(side_effect=AssertionError("actual turn runner entered"))
    build_agent = AsyncMock(return_value=SimpleNamespace(astream=stream))
    build_prompt = AsyncMock(return_value=("test prompt", []))
    monkeypatch.setattr(agent, "_build_agent_if_needed", build_agent)
    monkeypatch.setattr(agent, "_build_turn_prompt", build_prompt)
    approval_sync = Mock()
    pairing_sync = Mock()
    monkeypatch.setattr("mimir.operator_approval._is_authenticated_operator", lambda *a, **kw: True)
    monkeypatch.setattr("mimir.memory_proposals.sync_pending", approval_sync)
    monkeypatch.setattr("mimir.pairing_approval.sync_pending", pairing_sync)
    event = AgentEvent(trigger="user_message", channel_id="ch", content=content)
    try:
        with pytest.raises(RuntimeError, match="provider-0.*turn preflight") as error:
            await agent.run_turn(event)
        assert isinstance(error.value.__cause__, ValueError)
        assert str(error.value.__cause__) == "preflight returned False"
    finally:
        stream.assert_not_called()
    build_agent.assert_not_awaited()
    build_prompt.assert_not_awaited()
    approval_sync.assert_not_called()
    pairing_sync.assert_not_called()
    assert len(seen) == 1
    assert seen[0][0] is event
    assert seen[0][1].channel_id == "ch"
    assert [(e["phase"], e.get("status")) for e in panel_events] == [
        ("start", None), ("end", "error"),
    ]


@pytest.mark.asyncio
async def test_named_reply_reaches_approval_branch_after_successful_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    agent = _make_agent(tmp_path)
    calls = []
    panel_events = []
    agent._turn_event_bus = SimpleNamespace(publish=panel_events.append)

    class Provider:
        async def turn_preflight(self, event, auth_context):
            calls.append("preflight")

    _install(agent._config.plugin_manager, Provider())
    monkeypatch.setattr("mimir.operator_approval._is_authenticated_operator", lambda *a, **kw: True)
    monkeypatch.setattr("mimir.memory_proposals.sync_pending", lambda *a: calls.append("sync_pending"))
    monkeypatch.setattr("mimir.pairing_approval.sync_pending", lambda *a: calls.append("sync_pairings"))
    monkeypatch.setattr(agent, "_run_turn_body", lambda *a: pytest.fail("ordinary turn entered"))
    event = AgentEvent(trigger="user_message", channel_id="ch", content="approve update-abcd")
    assert is_non_turn_bound_reply(event)
    result = await agent.run_turn(event)
    assert result.kind == "operator_approval"
    assert result.output == "no pending request update-abcd"
    assert calls == ["preflight", "sync_pending", "sync_pairings"]
    assert [(e["phase"], e.get("status")) for e in panel_events] == [
        ("start", None), ("end", "ok"),
    ]


@pytest.mark.asyncio
async def test_poller_rejection_records_recovery_outcome_and_closes_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    agent = _make_agent(tmp_path)
    agent._config.access_control_enforced = False
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="plugin-lifecycle")
    panel_events = []
    agent._turn_event_bus = SimpleNamespace(publish=panel_events.append)
    send = AsyncMock(return_value=SimpleNamespace(sent=True))
    agent._channels = SimpleNamespace(send=send)
    monkeypatch.setattr(agent, "_run_turn_body", lambda *a: pytest.fail("prompt touched"))
    monkeypatch.setattr("mimir.memory_proposals.sync_pending", lambda *a: pytest.fail("approval touched"))

    class Provider:
        @property
        def turn_preflight(self):
            raise PermissionError("not admitted")

    _install(agent._config.plugin_manager, Provider())
    event = AgentEvent(
        trigger="poller", channel_id="poller:mail", content="new item",
        source_id="poller:mail:1:batch:0",
        extra={"poller_name": "mail", "deliver": "operator:alerts"},
    )
    with pytest.raises(RuntimeError, match="provider-0.*turn preflight"):
        await agent.run_turn(event)
    events_log = tmp_path / "logs" / "events.jsonl"
    assert events_log.exists(), "poller rejection must record a recovery outcome"
    records = [json.loads(line) for line in events_log.read_text().splitlines()]
    outcomes = [record for record in records if record.get("type") == "turn_failed"]
    assert len(outcomes) == 1
    assert outcomes[0]["source_id"] == "poller:mail:1:batch:0"
    assert outcomes[0]["phase"] == "pre_model_loop"
    send.assert_awaited_once()
    assert send.await_args.args[0] == "operator:alerts"
    assert "mail failed" in send.await_args.args[1]
    assert [(e["phase"], e.get("status")) for e in panel_events] == [
        ("start", None), ("end", "error"),
    ]


@pytest.mark.asyncio
async def test_chat_skill_preflight_rejection_sends_existing_failure_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    agent = _make_agent(tmp_path)
    agent._config.access_control_enforced = False
    send = AsyncMock(return_value=SimpleNamespace(sent=True))
    agent._channels = SimpleNamespace(send=send)

    class Provider:
        def turn_preflight(self, event, auth_context):
            raise ValueError("not admitted")

    _install(agent._config.plugin_manager, Provider())
    monkeypatch.setattr(agent, "_run_turn_body", lambda *a: pytest.fail("body entered"))
    event = AgentEvent(
        trigger="user_message", channel_id="ch", content="/memory remember this",
        extra={CHAT_SKILL_EXTRA_KEY: {"name": "memory", "command": "/memory"}},
    )
    with pytest.raises(RuntimeError, match="provider-0.*turn preflight"):
        await agent.run_turn(event)
    send.assert_awaited_once_with(
        "ch", "⚠️ I couldn't complete /memory on this turn. Please try again in a new message.",
        final=True,
    )


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
