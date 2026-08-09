from __future__ import annotations

import ast
import asyncio
import inspect
import io
import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mimir.acp import sdk
from mimir.acp.stdio import _DrainProtocol, _ReservedFrameTransport


EXPECTED_SCHEMA_IMPORTS = {
    "AcpMcpServer",
    "AgentCapabilities",
    "AgentMessageChunk",
    "AgentPlanUpdate",
    "AudioContentBlock",
    "AuthenticateRequest",
    "AuthenticateResponse",
    "AuthMethodAgent",
    "CancelNotification",
    "ClientCapabilities",
    "EmbeddedResourceContentBlock",
    "ImageContentBlock",
    "Implementation",
    "InitializeRequest",
    "InitializeResponse",
    "LoadSessionRequest",
    "LoadSessionResponse",
    "McpCapabilities",
    "NewSessionRequest",
    "NewSessionResponse",
    "PermissionOption",
    "PlanEntry",
    "PromptCapabilities",
    "PromptRequest",
    "PromptResponse",
    "ResourceContentBlock",
    "SessionNotification",
    "TextContentBlock",
    "ToolCallProgress",
    "ToolCallStart",
    "ToolCallUpdate",
    "UserMessageChunk",
}


class FakeConnection:
    def __init__(self, results: list[Any] | None = None) -> None:
        self.results = list(results or [])
        self.requests: list[tuple[str, Any]] = []
        self.notifications: list[tuple[str, Any]] = []

    async def send_request(self, method: str, params: Any = None) -> Any:
        self.requests.append((method, params))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def send_notification(self, method: str, params: Any = None) -> None:
        self.notifications.append((method, params))


class ContractAgent:
    def __init__(self) -> None:
        self.authenticate_call: tuple[str, dict[str, Any]] | None = None
        self.extension_calls: list[tuple[str, dict[str, Any]]] = []
        self.notification_calls: list[tuple[str, dict[str, Any]]] = []
        self.mcp_notifications: list[tuple[str, str, Any]] = []
        self.peer: sdk.AcpPeer | None = None

    def on_connect(self, peer: sdk.AcpPeer) -> int:
        self.peer = peer
        return 7

    async def authenticate(self, method_id: str, **kwargs: Any) -> sdk.AuthenticateResponse:
        self.authenticate_call = (method_id, kwargs)
        return sdk.AuthenticateResponse()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.extension_calls.append((method, params))
        return {"extension": method}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self.notification_calls.append((method, params))

    async def on_mcp_notification(
        self, connection_id: str, method: str, params: dict[str, Any] | None
    ) -> None:
        self.mcp_notifications.append((connection_id, method, params))


def test_pinned_distribution_protocol_and_schema_boundary() -> None:
    assert version("agent-client-protocol") == "0.12.0"
    assert sdk.PROTOCOL_VERSION == 1
    source = Path(sdk.__file__).read_text()
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "acp.schema"
        for alias in node.names
    }
    assert imports == EXPECTED_SCHEMA_IMPORTS
    assert sdk.AcpMcpServer.__module__ == "acp.schema"
    assert sdk.McpCapabilities.model_fields["acp"].default is False
    assert sdk.Agent.__module__ == "acp.interfaces"
    assert not any(
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(getattr(node, "target", None), ast.Attribute)
        and isinstance(getattr(node.target, "value", None), ast.Name)
        and node.target.value == "acp"
        for node in ast.walk(tree)
    )


def test_pinned_schema_aliases_handlers_and_session_updates() -> None:
    assert sdk.InitializeRequest.model_fields["protocol_version"].alias == "protocolVersion"
    assert sdk.InitializeRequest.model_fields["field_meta"].alias == "_meta"
    assert sdk.AuthenticateRequest.model_fields["method_id"].alias == "methodId"
    assert sdk.AgentCapabilities.model_fields["load_session"].alias == "loadSession"
    assert sdk.PromptCapabilities.model_fields["embedded_context"].alias == "embeddedContext"
    assert getattr(sdk.Agent.initialize, "__param_model__") is sdk.InitializeRequest
    assert getattr(sdk.Agent.authenticate, "__param_model__") is sdk.AuthenticateRequest
    assert getattr(sdk.Agent.new_session, "__param_model__") is sdk.NewSessionRequest
    assert getattr(sdk.Agent.load_session, "__param_model__") is sdk.LoadSessionRequest
    assert getattr(sdk.Agent.prompt, "__param_model__") is sdk.PromptRequest
    assert getattr(sdk.Agent.cancel, "__param_model__") is sdk.CancelNotification
    assert sdk.SessionNotification.model_fields["session_id"].alias == "sessionId"
    assert sdk.ToolCallStart.model_fields["tool_call_id"].alias == "toolCallId"
    assert sdk.ToolCallProgress.model_fields["raw_input"].alias == "rawInput"
    text = sdk.TextContentBlock(type="text", text="hello")
    update = sdk.UserMessageChunk(
        content=text, sessionUpdate="user_message_chunk", _meta={"mimir.sequence": 4}
    )
    assert update.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "content": {"type": "text", "text": "hello"},
        "_meta": {"mimir.sequence": 4},
        "sessionUpdate": "user_message_chunk",
    }


def test_pinned_agent_signatures_and_error_objects() -> None:
    assert list(inspect.signature(sdk.Agent.initialize).parameters) == [
        "self", "protocol_version", "client_capabilities", "client_info", "kwargs"
    ]
    assert list(inspect.signature(sdk.Agent.new_session).parameters) == [
        "self", "cwd", "additional_directories", "mcp_servers", "kwargs"
    ]
    assert sdk.auth_required_error().to_error_obj() == {
        "code": -32000,
        "message": "Authentication required",
        "data": {"methodId": "mimir-web-key"},
    }
    assert sdk.method_not_found_error("example").to_error_obj() == {
        "code": -32601,
        "message": "Method not found",
        "data": {"method": "example"},
    }
    assert sdk.unknown_connection_error().to_error_obj() == {
        "code": -32602,
        "message": "Unknown MCP connection",
        "data": None,
    }


@pytest.mark.parametrize(
    "model,payload",
    [
        (sdk.ConnectMcpRequest, {"serverId": "server"}),
        (sdk.ConnectMcpResponse, {"connectionId": "connection"}),
        (
            sdk.MessageMcpRequest,
            {"connectionId": "connection", "method": "tools/call", "params": {"name": "read"}},
        ),
        (
            sdk.MessageMcpNotification,
            {"connectionId": "connection", "method": "notifications/initialized"},
        ),
        (sdk.DisconnectMcpRequest, {"connectionId": "connection"}),
        (sdk.DisconnectMcpResponse, {}),
    ],
)
def test_mcp_schema_v119_outer_models_have_independent_strict_meta(
    model: type[sdk.StrictSchemaModel], payload: dict[str, Any]
) -> None:
    for meta in ({"trace": 1}, None):
        value = dict(payload, _meta=meta)
        parsed = model.model_validate(value)
        assert parsed.model_dump(mode="json", by_alias=True, exclude_none=False, exclude_unset=True) == value
    parsed = model.model_validate(payload)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_none=True) == payload
    for invalid in ([], "bad", 1, True):
        with pytest.raises(ValidationError):
            model.model_validate(dict(payload, _meta=invalid))
    with pytest.raises(ValidationError):
        model.model_validate(dict(payload, extra=True))


def test_mcp_message_nested_params_are_preserved_and_typed() -> None:
    nested = {"name": "read", "arguments": {"path": "a", "nested": [1, {"x": None}]}}
    parsed = sdk.MessageMcpRequest.model_validate(
        {"connectionId": "c", "method": "tools/call", "params": nested}
    )
    assert parsed.params == nested
    assert parsed.params is not nested
    with pytest.raises(ValidationError):
        sdk.MessageMcpRequest.model_validate(
            {"connectionId": "c", "method": "tools/call", "params": []}
        )
    with pytest.raises(ValidationError):
        sdk.MessageMcpRequest.model_validate(
            {"connectionId": 1, "method": "tools/call"}
        )


@pytest.mark.asyncio
async def test_mcp_connect_message_disconnect_exact_outbound_envelopes() -> None:
    connection = FakeConnection([
        {"connectionId": "opaque", "_meta": None},
        {"tools": [{"name": "read"}]},
        {},
    ])
    peer = sdk.AcpPeer(connection, ContractAgent())
    connection_id = await peer.connect_mcp("server-1")
    result = await peer.message_mcp(
        connection_id, "tools/list", {"cursor": {"nested": True}}
    )
    await peer.notify_mcp(
        connection_id, "notifications/cancelled", {"requestId": "outer-id"}
    )
    await peer.disconnect_mcp(connection_id)
    assert result == {"tools": [{"name": "read"}]}
    assert connection.requests == [
        ("mcp/connect", {"serverId": "server-1"}),
        (
            "mcp/message",
            {"connectionId": "opaque", "method": "tools/list", "params": {"cursor": {"nested": True}}},
        ),
        ("mcp/disconnect", {"connectionId": "opaque"}),
    ]
    assert connection.notifications == [
        (
            "mcp/message",
            {
                "connectionId": "opaque",
                "method": "notifications/cancelled",
                "params": {"requestId": "outer-id"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_mcp_response_validation_reuse_and_routing_fail_closed() -> None:
    malformed = sdk.AcpPeer(FakeConnection([{"connectionId": "c", "extra": True}]), ContractAgent())
    with pytest.raises(sdk.AcpProtocolError):
        await malformed.connect_mcp("server")
    connection = FakeConnection([{"connectionId": "c"}, {"connectionId": "c"}])
    agent = ContractAgent()
    peer = sdk.AcpPeer(connection, agent)
    await peer.connect_mcp("one")
    with pytest.raises(sdk.AcpProtocolError):
        await peer.connect_mcp("two")
    assert await peer.route_mcp(
        {"connectionId": "c", "method": "ping", "params": {"nested": {"x": 1}}}, False
    ) == {}
    with pytest.raises(sdk.RequestError) as unknown_method:
        await peer.route_mcp({"connectionId": "c", "method": "sampling/createMessage"}, False)
    assert unknown_method.value.code == -32601
    with pytest.raises(sdk.RequestError) as unknown_connection:
        await peer.route_mcp({"connectionId": "missing", "method": "ping"}, False)
    assert unknown_connection.value.to_error_obj()["message"] == "Unknown MCP connection"
    assert await peer.route_mcp(
        {"connectionId": "missing", "method": "notifications/message", "params": {"x": 1}}, True
    ) is None
    await peer.route_mcp(
        {
            "connectionId": "c",
            "method": "notifications/progress",
            "params": {"progressToken": {"nested": 1}},
            "_meta": {"ignored": True},
        },
        True,
    )
    await peer.route_mcp({"connectionId": "c", "method": "unknown"}, True)
    assert agent.mcp_notifications == [
        ("c", "notifications/progress", {"progressToken": {"nested": 1}})
    ]


def test_permission_request_is_exact_lifecycle_snapshot() -> None:
    snapshot = sdk.PermissionSnapshot(
        tool_call_id="tool-7",
        title="Edit local file",
        kind="edit",
        raw_input={"path": "notes.txt", "oldText": "a", "newText": "b"},
    )
    assert sdk.permission_request_params("session-1", snapshot) == {
        "sessionId": "session-1",
        "toolCall": {
            "toolCallId": "tool-7",
            "title": "Edit local file",
            "kind": "edit",
            "status": "pending",
            "rawInput": {"path": "notes.txt", "oldText": "a", "newText": "b"},
        },
        "options": [
            {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
        ],
    }


@pytest.mark.parametrize(
    "payload,decision",
    [
        ({"outcome": {"outcome": "selected", "optionId": "allow_once"}}, "allow_once"),
        (
            {
                "outcome": {
                    "outcome": "selected",
                    "optionId": "reject_once",
                    "_meta": {"ignored": True},
                },
                "_meta": None,
            },
            "reject_once",
        ),
        ({"outcome": {"outcome": "cancelled"}, "_meta": {"ignored": True}}, "cancelled"),
    ],
)
def test_permission_response_accepted_shapes(payload: Any, decision: str) -> None:
    assert sdk.validate_permission_response(payload) == decision


@pytest.mark.parametrize(
    "payload",
    [
        {"outcome": {"outcome": "selected", "optionId": "allow_always"}},
        {"outcome": {"outcome": "selected", "optionId": "unknown"}},
        {"outcome": {"outcome": "cancelled", "_meta": {}}},
        {"outcome": {"outcome": "cancelled", "extra": True}},
        {"outcome": {"outcome": "selected", "optionId": "allow_once", "extra": True}},
        {"outcome": {"outcome": "selected", "optionId": "allow_once"}, "extra": True},
        {"outcome": {"outcome": "selected", "optionId": "allow_once"}, "_meta": []},
        {"outcome": {"outcome": "cancelled"}, "_meta": "bad"},
        {"outcome": {"outcome": "selected"}},
        {"outcome": {"outcome": "future"}},
        {},
    ],
)
def test_permission_response_rejects_malformed_unknown_and_persistent(payload: Any) -> None:
    with pytest.raises(sdk.AcpProtocolError):
        sdk.validate_permission_response(payload)


@pytest.mark.asyncio
async def test_permission_completion_exposes_cancel_and_errors_without_execution() -> None:
    snapshot = sdk.PermissionSnapshot("t", "Run", "execute", {"command": "true"})
    allow_peer = sdk.AcpPeer(
        FakeConnection([{"outcome": {"outcome": "selected", "optionId": "allow_once"}}]),
        ContractAgent(),
    )
    allow = await allow_peer.request_tool_permission("s", snapshot)
    assert allow.decision == "allow_once"
    assert allow.executable is True
    cancel_peer = sdk.AcpPeer(
        FakeConnection([{"outcome": {"outcome": "cancelled"}}]), ContractAgent()
    )
    cancelled = await cancel_peer.request_tool_permission("s", snapshot)
    assert cancelled.decision == "cancelled"
    assert cancelled.executable is False
    error_peer = sdk.AcpPeer(FakeConnection([sdk.RequestError.internal_error()]), ContractAgent())
    errored = await error_peer.request_tool_permission("s", snapshot)
    assert errored.decision == "reject_once"
    assert isinstance(errored.error, sdk.RequestError)
    assert errored.executable is False


@pytest.mark.asyncio
async def test_actual_runner_preserves_agent_router_and_generic_outer_ids() -> None:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": "auth-id",
            "method": "authenticate",
            "params": {
                "methodId": "mimir-web-key",
                "_meta": {"mimir.webKey": "secret", "principal": "asserted"},
            },
        },
        {"jsonrpc": "2.0", "id": "extension-id", "method": "_example", "params": {"value": 7}},
        {"jsonrpc": "2.0", "method": "_notice", "params": {"value": 8}},
        {
            "jsonrpc": "2.0",
            "id": "unknown-mcp",
            "method": "mcp/message",
            "params": {"connectionId": "missing", "method": "ping"},
        },
    ]
    reader = asyncio.StreamReader()
    for request in requests:
        reader.feed_data((json.dumps(request) + "\n").encode())
    reader.feed_eof()
    output = io.BytesIO()
    protocol = _DrainProtocol()
    transport = _ReservedFrameTransport(output, protocol)
    writer = asyncio.StreamWriter(transport, protocol, None, asyncio.get_running_loop())
    agent = ContractAgent()
    await sdk.run_stdio_agent(agent, request_reader=reader, response_writer=writer)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses == [
        {"jsonrpc": "2.0", "id": "auth-id", "result": {}},
        {"jsonrpc": "2.0", "id": "extension-id", "result": {"extension": "example"}},
        {
            "jsonrpc": "2.0",
            "id": "unknown-mcp",
            "error": {"code": -32602, "message": "Unknown MCP connection", "data": None},
        },
    ]
    assert agent.authenticate_call == (
        "mimir-web-key", {"mimir.webKey": "secret", "principal": "asserted"}
    )
    assert agent.extension_calls == [("example", {"value": 7})]
    assert agent.notification_calls == [("notice", {"value": 8})]
    assert agent.peer is not None
    assert agent.peer.peer_generation == 7


def test_acp_mcp_server_draft_type_is_strictly_adapted() -> None:
    declaration = sdk.validate_acp_mcp_server(
        {"type": "acp", "name": "mimir-hands", "serverId": "server", "_meta": None}
    )
    assert isinstance(declaration, sdk.AcpMcpServer)
    assert declaration.server_id == "server"
    for invalid in (
        {"type": "stdio", "name": "mimir-hands", "serverId": "server"},
        {"type": "acp", "name": "", "serverId": "server"},
        {"type": "acp", "name": "mimir-hands", "serverId": 1},
        {"type": "acp", "name": "mimir-hands", "serverId": "server", "_meta": []},
        {"type": "acp", "name": "mimir-hands", "serverId": "server", "extra": True},
    ):
        with pytest.raises(sdk.AcpProtocolError):
            sdk.validate_acp_mcp_server(invalid)


@pytest.mark.asyncio
async def test_strict_outer_envelopes_and_duplicate_or_late_responses_fail() -> None:
    state = sdk.StrictMessageStateStore()
    future = state.register_outgoing(4, "mcp/connect")
    state.resolve_outgoing(4, {"connectionId": "c"})
    assert await future == {"connectionId": "c"}
    with pytest.raises(sdk.AcpProtocolError, match="Duplicate or late"):
        state.resolve_outgoing(4, {"connectionId": "other"})
    valid = {
        "jsonrpc": "2.0",
        "id": "outer",
        "method": "mcp/message",
        "params": {"connectionId": "c", "method": "ping"},
    }
    sdk.validate_jsonrpc_envelope(valid)
    sdk.validate_jsonrpc_envelope(
        {"jsonrpc": "2.0", "id": "outer", "result": {}}
    )
    sdk.validate_jsonrpc_envelope(
        {
            "jsonrpc": "2.0",
            "id": "outer",
            "error": {"code": -32601, "message": "Method not found", "data": None},
        }
    )
    invalid = [
        dict(valid, extra=True),
        dict(valid, id=True),
        dict(valid, params=[]),
        {"jsonrpc": "2.0", "id": "outer"},
        {"jsonrpc": "2.0", "id": "outer", "result": {}, "error": {}},
        {"jsonrpc": "2.0", "id": "outer", "error": {"code": True, "message": "bad"}},
        {"jsonrpc": "2.0", "id": "outer", "error": {"code": -1, "message": "bad", "extra": 1}},
    ]
    for envelope in invalid:
        with pytest.raises(sdk.AcpProtocolError):
            sdk.validate_jsonrpc_envelope(envelope)
