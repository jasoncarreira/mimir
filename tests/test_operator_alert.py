from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from langgraph.runtime import Runtime

from mimir._context import reset_current_turn, set_current_turn
from mimir.access_control import (
    CapabilityTier,
    ToolRegistry,
    build_trigger_service_principal,
    create_auth_context,
)
from mimir.models import (
    AgentEvent,
    InformationFlowLabels,
    SourceLabel,
    TurnContext,
)
from mimir.tools.budget_gate import BudgetGateMiddleware
from mimir.tools.operator_alert import (
    OPERATOR_ALERT_MAX_CHARS,
    OPERATOR_ALERT_MAX_PER_TURN,
    operator_alert,
    set_operator_alert_dependencies,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("integrity", ["trusted", "untrusted", None])
async def test_web_alert_persists_once_without_sse_and_keeps_turn_integrity(tmp_path, integrity):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from mimir.bridges.web_chat import WebChatBridge
    from mimir.channel_registry import ChannelRegistry
    from mimir.history import MessageBuffer, set_global_buffer

    buffer = MessageBuffer(tmp_path / "chat_history.jsonl")
    set_global_buffer(buffer)
    bridge = WebChatBridge(enqueue=lambda _: None, home=tmp_path)
    channels = ChannelRegistry()
    channels.register(bridge)
    set_operator_alert_dependencies(channels, SimpleNamespace(operator_alert_channel="web-owner"))
    labels = InformationFlowLabels().with_source(SourceLabel(
        principal="service:poller", domain="channel", resource_id="poller:test",
        bridge_instance="poller", sensitivity="internal",
        authorized_principals=frozenset({"service:poller"}), source_kind="service",
        integrity=integrity or "untrusted", integrity_effect="active_ingest",
    )) if integrity is not None else None
    turn = TurnContext(turn_id="alert-history", session_id="poller:test", trigger="poller",
                       channel_id="poller:test", started_at=time.monotonic(), ifc_labels=labels)
    token = set_current_turn(turn)
    try:
        result = await operator_alert.coroutine(text="failure requires attention")
    finally:
        reset_current_turn(token)
    assert result.startswith("operator_alert ok:")
    assert bridge._subscribers == []
    assert buffer.channel_count("web-owner") == 1
    record, = buffer.recent_for_channel("web-owner", 10, source_allowlist=frozenset({"web"}))
    expected_integrity = integrity or "untrusted"
    assert record.content == "failure requires attention" and record.integrity == expected_integrity
    assert record.source == "web" and record.kind == "assistant_message"
    from mimir.agent import _recent_message_is_self_authored
    assert _recent_message_is_self_authored(record) is (integrity == "trusted")
    replayed = MessageBuffer(buffer.history_path)
    assert replayed.replay() == 1
    assert replayed.recent_in_channel("web-owner", 1)[0].integrity == expected_integrity

    @web.middleware
    async def authenticate(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="owner", access=SimpleNamespace(is_admin=True))
        return await handler(request)

    app = web.Application(middlewares=[authenticate])
    bridge.register_routes(app)
    client = TestClient(TestServer(app))
    try:
        await client.start_server()
        response = await client.get("/api/v1/chat/history")
        assert response.status == 200
        messages = (await response.json())["data"]["messages"]
        assert [(m["role"], m["text"]) for m in messages] == [("assistant", "failure requires attention")]
    finally:
        await client.close()
        set_global_buffer(None)


@pytest.mark.asyncio
async def test_alert_setup_check_requires_owner_admin_and_uses_real_tool(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from mimir.bridges.web_chat import WebChatBridge
    from mimir.channel_registry import ChannelRegistry
    from mimir.history import MessageBuffer, set_global_buffer

    buffer = MessageBuffer(tmp_path / "chat_history.jsonl")
    set_global_buffer(buffer)
    bridge = WebChatBridge(enqueue=lambda _: None, home=tmp_path)
    channels = ChannelRegistry()
    channels.register(bridge)
    set_operator_alert_dependencies(channels, SimpleNamespace(operator_alert_channel="web-owner"))
    @web.middleware
    async def authenticate(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical=request.headers.get("X-Identity", "owner"),
            access=SimpleNamespace(is_admin=request.headers.get("X-Admin") == "yes"))
        return await handler(request)
    app = web.Application(middlewares=[authenticate])
    bridge.register_routes(app)
    client = TestClient(TestServer(app))
    try:
        await client.start_server()
        route = "/api/v1/chat/operator-alert-check"
        assert (await client.post(route)).status == 403
        assert (await client.post(route, headers={"X-Admin": "yes", "X-Identity": "other"})).status == 409
        assert buffer.channel_count("web-owner") == 0
        response = await client.post(route, headers={"X-Admin": "yes"})
        assert response.status == 200 and (await response.json())["data"]["sent"] is True
        assert [m.content for m in buffer.recent_in_channel("web-owner", 10)] == [
            "Cultivate setup: operator alerts enabled"]
    finally:
        await client.close()
        set_global_buffer(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["discord", "slack"])
async def test_alert_setup_check_requires_connected_bot_and_successful_send(tmp_path, adapter):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from mimir.bridges.web_chat import WebChatBridge
    from mimir.bridges.base import SendResult

    class Bot:
        _client = SimpleNamespace(is_ready=lambda: False)
        _handler = None

    class Channels:
        def __init__(self):
            self.bot = Bot()
            self.sent = False
            self.calls = []

        def find(self, channel):
            return self.bot

        async def send(self, channel, text, *, final):
            self.calls.append((channel, text, final))
            return SendResult(sent=self.sent, message_id="probe-id", error="token-sensitive-error")

    destination = adapter + ("-123456789012345678" if adapter == "discord" else "-C12345678")
    channels = Channels()
    set_operator_alert_dependencies(channels, SimpleNamespace(operator_alert_channel=destination))
    bridge = WebChatBridge(enqueue=lambda _: None, home=tmp_path)

    @web.middleware
    async def authenticate(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="owner", access=SimpleNamespace(is_admin=True))
        return await handler(request)

    app = web.Application(middlewares=[authenticate])
    bridge.register_routes(app)
    client = TestClient(TestServer(app))
    try:
        await client.start_server()
        route = "/api/v1/chat/operator-alert-check"
        channels.bot = None
        response = await client.post(route)
        assert response.status == 409 and (await response.json())["error"]["code"] == "alert_bridge_unconnected"
        channels.bot = Bot()
        response = await client.post(route)
        assert response.status == 409 and (await response.json())["error"]["code"] == "alert_bridge_unconnected"
        assert channels.calls == []
        channels.bot._client = SimpleNamespace(is_ready=lambda: True)
        async def disconnected():
            return False
        channels.bot._handler = SimpleNamespace(client=SimpleNamespace(is_connected=disconnected))
        response = await client.post(route)
        if adapter == "slack":
            assert response.status == 409 and (await response.json())["error"]["code"] == "alert_bridge_unconnected"
        async def connected():
            return True
        channels.bot._handler = SimpleNamespace(client=SimpleNamespace(is_connected=connected))
        channels.calls.clear()
        response = await client.post(route)
        assert response.status == 503 and (await response.json())["error"]["code"] == "alert_send_failed"
        assert "token-sensitive-error" not in await response.text()
        channels.sent = True
        response = await client.post(route)
        assert response.status == 200 and (await response.json())["data"]["sent"] is True
        assert channels.calls == [(destination, "Cultivate setup: operator alerts enabled", False)] * 2
    finally:
        await client.close()


class _Channels:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def send(self, channel_id: str, text: str, *, final: bool = True) -> Any:
        self.calls.append((channel_id, text, final))
        return SimpleNamespace(sent=True, message_id=f"message-{len(self.calls)}")


def _request(name: str, auth: Any, args: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": f"call-{name}", "type": "tool_call"},
        tool=None,
        state=None,
        runtime=Runtime(context=auth),
    )


def _tainted_poller(tmp_path: Any) -> tuple[TurnContext, Any]:
    service = build_trigger_service_principal(
        canonical="poller:research",
        trigger="poller",
        profile="research",
        tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
        capabilities=("operator_alert", "write_file"),
        roots=(tmp_path,),
        creation_path="test",
    )
    channel = service.canonical
    principal = f"service:{service.canonical}"
    labels = InformationFlowLabels().with_channel(channel).with_source(SourceLabel(
        principal=principal,
        domain="channel",
        resource_id=channel,
        bridge_instance="poller",
        sensitivity="internal",
        authorized_principals=frozenset({principal}),
        source_kind="service",
        integrity="untrusted",
        integrity_effect="active_ingest",
    ))
    auth = create_auth_context(
        AgentEvent(
            trigger="poller",
            channel_id=channel,
            source="poller",
            service_principal=service.canonical,
            service_authority=service,
        ),
        enforce=True,
        ifc_labels=labels,
    )
    auth.ifc_state.merge(labels)
    turn = TurnContext(
        turn_id="operator-alert-test",
        session_id=channel,
        trigger="poller",
        channel_id=channel,
        started_at=time.monotonic(),
        auth_context=auth,
        ifc_labels=labels,
        tool_call_budget=20,
    )
    return turn, auth


@pytest.fixture(autouse=True)
def _reset_dependencies() -> Any:
    yield
    set_operator_alert_dependencies(None, None)


@pytest.mark.asyncio
async def test_tainted_service_alert_uses_configured_destination_and_other_sinks_refuse(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")
    monkeypatch.setenv("MIMIR_OPERATOR_ALERT_CHANNEL", "discord-operator")
    channels = _Channels()
    set_operator_alert_dependencies(
        channels, SimpleNamespace(operator_alert_channel="discord-operator"),
    )
    turn, auth = _tainted_poller(tmp_path)
    middleware = BudgetGateMiddleware()
    token = set_current_turn(turn)

    async def alert_handler(request: ToolCallRequest) -> ToolMessage:
        result = await operator_alert.coroutine(text=request.tool_call["args"]["text"])
        return ToolMessage(content=result, tool_call_id=request.tool_call["id"])

    denied_calls = 0

    async def denied_handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal denied_calls
        denied_calls += 1
        return ToolMessage(content="unexpected", tool_call_id=request.tool_call["id"])

    try:
        alert = await middleware.awrap_tool_call(
            _request(
                "operator_alert",
                auth,
                {"text": "review this finding", "destination": "discord-hostile"},
            ),
            alert_handler,
        )
        cross_channel = await middleware.awrap_tool_call(
            _request(
                "send_message",
                auth,
                {"text": "leak", "channel_id": "discord-hostile"},
            ),
            denied_handler,
        )
        write = await middleware.awrap_tool_call(
            _request("write_file", auth, {"file_path": str(tmp_path / "note.md")}),
            denied_handler,
        )
    finally:
        reset_current_turn(token)

    assert alert.status != "error"
    assert channels.calls == [("discord-operator", "review this finding", False)]
    assert cross_channel.status == "error"
    assert write.status == "error"
    assert denied_calls == 0


@pytest.fixture
def selected_repository_policy():
    from importlib.metadata import PackageNotFoundError, version
    from mimir.plugins.core import policy_scope
    from mimir.plugins.manager import load_plugins

    try:
        version("mimir-cultivate")
    except PackageNotFoundError:
        pytest.skip("shipped Cultivate poller requires installed Cultivate")
    with policy_scope(load_plugins(enable="cultivate").policy):
        yield


@pytest.mark.asyncio
async def test_shipped_worklink_principal_can_use_real_operator_alert_sink(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, selected_repository_policy,
) -> None:
    from mimir.pollers import _parse_poller_authority

    manifest_path = (
        Path(__file__).parents[1]
            / "plugins/cultivate/mimir_cultivate/defaults/skills"
        / "chainlink-orchestrator"
        / "pollers.json"
    )
    entry = json.loads(manifest_path.read_text(encoding="utf-8"))["pollers"][0]
    persist_dir = tmp_path / "state" / "pollers" / entry["name"]
    persist_dir.mkdir(parents=True)
    authority = _parse_poller_authority(
        entry["authority"],
        name=entry["name"],
        persist_dir=persist_dir,
        state_root=persist_dir.parent,
        manifest_path=manifest_path,
    )
    assert authority.canonical == "poller:worklink-ready-queue"
    assert "operator_alert" in authority.capabilities
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")
    monkeypatch.setenv("MIMIR_OPERATOR_ALERT_CHANNEL", "discord-operator")
    channels = _Channels()
    set_operator_alert_dependencies(
        channels, SimpleNamespace(operator_alert_channel="discord-operator")
    )
    principal = f"service:{authority.canonical}"
    labels = InformationFlowLabels().with_channel(authority.canonical).with_source(
        SourceLabel(
            principal=principal,
            domain="channel",
            resource_id=authority.canonical,
            bridge_instance="poller",
            sensitivity="internal",
            authorized_principals=frozenset({principal}),
            source_kind="service",
            integrity="untrusted",
            integrity_effect="active_ingest",
        )
    )
    event = AgentEvent(
        trigger="poller",
        channel_id=authority.canonical,
        source="poller",
        service_principal=authority.canonical,
        service_authority=authority,
    )
    auth = create_auth_context(event, enforce=True, ifc_labels=labels)
    auth.ifc_state.merge(labels)
    turn = TurnContext(
        turn_id="worklink-operator-alert",
        session_id=authority.canonical,
        trigger="poller",
        channel_id=authority.canonical,
        started_at=time.monotonic(),
        auth_context=auth,
        ifc_labels=labels,
        tool_call_budget=20,
    )
    token = set_current_turn(turn)

    async def handler(request: ToolCallRequest) -> ToolMessage:
        content = await operator_alert.coroutine(text=request.tool_call["args"]["text"])
        return ToolMessage(content=content, tool_call_id=request.tool_call["id"])

    try:
        result = await BudgetGateMiddleware().awrap_tool_call(
            _request("operator_alert", auth, {"text": "factory recovery unsafe"}),
            handler,
        )
    finally:
        reset_current_turn(token)

    assert result.status != "error"
    assert channels.calls == [("discord-operator", "factory recovery unsafe", False)]


@pytest.mark.asyncio
async def test_unconfigured_alert_fails_loudly_in_real_sink_gate(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIMIR_OPERATOR_ALERT_CHANNEL", raising=False)
    turn, auth = _tainted_poller(tmp_path)
    called = False

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="unexpected", tool_call_id=request.tool_call["id"])

    result = await BudgetGateMiddleware().awrap_tool_call(
        _request("operator_alert", auth, {"text": "finding"}), handler,
    )

    assert result.status == "error"
    assert "MIMIR_OPERATOR_ALERT_CHANNEL" in str(result.content)
    assert called is False


@pytest.mark.asyncio
async def test_operator_alert_bounds_text_and_per_turn_count(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin the VALUES, not just the mechanism. Every input and assertion below is
    # expressed in terms of these constants, so raising one raises the test's own
    # input to match and the bound can never fail. These two literals are what make
    # the rest of this test load-bearing: they are the ceiling on how much
    # attacker-supplied text an untrusted-ingest turn can push at the operator, so
    # widening either must be a deliberate edit here rather than a silent one.
    assert OPERATOR_ALERT_MAX_CHARS == 4000
    assert OPERATOR_ALERT_MAX_PER_TURN == 3
    monkeypatch.setenv("MIMIR_OPERATOR_ALERT_CHANNEL", "discord-operator")
    channels = _Channels()
    set_operator_alert_dependencies(
        channels, SimpleNamespace(operator_alert_channel="discord-operator"),
    )
    turn, _ = _tainted_poller(tmp_path)
    token = set_current_turn(turn)
    try:
        with pytest.raises(Exception, match="character limit"):
            await operator_alert.coroutine(text="x" * (OPERATOR_ALERT_MAX_CHARS + 1))
        for index in range(OPERATOR_ALERT_MAX_PER_TURN):
            await operator_alert.coroutine(text=f"alert {index}")
        with pytest.raises(Exception, match="per-turn limit"):
            await operator_alert.coroutine(text="one too many")
    finally:
        reset_current_turn(token)

    assert len(channels.calls) == OPERATOR_ALERT_MAX_PER_TURN


def test_operator_alert_is_registered() -> None:
    from mimir.tools import all_mimir_tools

    assert "operator_alert" in {tool.name for tool in all_mimir_tools()}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "unsent"])
async def test_failed_alert_refunds_only_its_parallel_reservation(tmp_path, failure):
    entered = [asyncio.Event() for _ in range(3)]
    release = [asyncio.Event() for _ in range(3)]
    calls = []

    class Channels:
        async def send(self, channel_id, text, *, final):
            index = len(calls)
            calls.append(text)
            if index < 3:
                entered[index].set()
                await release[index].wait()
            if index == 0:
                if failure == "exception":
                    raise OSError("offline")
                return SimpleNamespace(sent=False, error="offline")
            return SimpleNamespace(sent=True)

    set_operator_alert_dependencies(
        Channels(), SimpleNamespace(operator_alert_channel="discord-operator"),
    )
    turn, _ = _tainted_poller(tmp_path)
    token = set_current_turn(turn)
    tasks = []
    try:
        for index in range(3):
            tasks.append(asyncio.create_task(operator_alert.coroutine(text=str(index))))
            await entered[index].wait()
        with pytest.raises(ToolException, match="per-turn limit"):
            await operator_alert.coroutine(text="overflow")
        release[1].set()
        await tasks[1]
        release[0].set()
        with pytest.raises(ToolException, match="offline"):
            await tasks[0]
        # One successful send and one pending send still own their slots.
        assert turn.operator_alert_count == 2
        await operator_alert.coroutine(text="retry")
        with pytest.raises(ToolException, match="per-turn limit"):
            await operator_alert.coroutine(text="overflow again")
        release[2].set()
        await tasks[2]
        assert turn.operator_alert_count == 3
        assert calls == ["0", "1", "2", "retry"]
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        reset_current_turn(token)
