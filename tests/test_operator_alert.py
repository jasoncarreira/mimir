from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
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
