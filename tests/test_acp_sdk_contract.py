from __future__ import annotations

import ast
import asyncio
import inspect
import io
import json
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
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

FORBIDDEN_SCHEMA_IMPORTS = {
    "McpServer",
    "McpServerStdio",
    "HttpMcpServer",
    "SseMcpServer",
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
        self.mcp_event = asyncio.Event()
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
        self, peer_generation: int, connection_id: str, method: str,
        params: dict[str, Any] | None,
    ) -> None:
        self.mcp_notifications.append((connection_id, method, params))
        self.mcp_event.set()


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
    assert FORBIDDEN_SCHEMA_IMPORTS.isdisjoint(vars(sdk))
    assert sdk.AcpMcpServer.__module__ == "acp.schema"
    assert sdk.McpCapabilities.__name__ == "McpCapabilities"
    assert sdk.McpCapabilities.model_fields["acp"].default is False
    assert sdk.Agent.__module__ == "acp.interfaces"
    assert sdk.Client.__module__ == "acp.interfaces"
    assert set(sdk.__all__) == {
        "AGENT_METHODS", "AUTH_METHOD_ID", "CLIENT_METHODS", "MCP_CONNECT_METHOD",
        "MCP_DISCONNECT_METHOD", "MCP_INBOUND_NOTIFICATIONS", "MCP_MESSAGE_METHOD",
        "MCP_NOTIFICATION_METHODS", "MCP_REQUEST_METHODS", "PERMISSION_METHOD",
        "AcpPeer", "AcpPeerCallbacks", "AcpRequestHandle", "AcpProtocolError", "BoundedMessageQueue", "CancelledPermissionOutcome",
        "MAX_FRAME_BYTES", "MAX_PENDING_REQUESTS", "INPUT_QUEUE_MAX_ITEMS",
        "INPUT_QUEUE_MAX_BYTES", "INPUT_QUEUE_DRAIN_TIMEOUT",
        "ConnectMcpRequest", "ConnectMcpResponse", "DisconnectMcpRequest",
        "DisconnectMcpResponse", "MessageMcpNotification", "MessageMcpRequest",
        "PermissionCompletion", "PermissionDecision", "PermissionSnapshot",
        "RequestPermissionResponse", "SelectedPermissionOutcome", "StrictMessageStateStore",
        "StrictNdjsonTransport", "auth_required_error", "internal_error",
        "invalid_params_error", "method_not_found_error", "permission_request_params",
        "run_stdio_agent", "unknown_connection_error", "validate_acp_mcp_server",
        "validate_jsonrpc_envelope", "validate_permission_response", "Agent", "Client",
        "PROTOCOL_VERSION", "RequestError", *EXPECTED_SCHEMA_IMPORTS,
    }
    assert not any(
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(getattr(node, "target", None), ast.Attribute)
        and isinstance(getattr(node.target, "value", None), ast.Name)
        and node.target.value == "acp"
        for node in ast.walk(tree)
    )


def test_pinned_schema_aliases_and_handler_models() -> None:
    assert sdk.InitializeRequest.model_fields["protocol_version"].alias == "protocolVersion"
    assert sdk.InitializeRequest.model_fields["field_meta"].alias == "_meta"
    assert sdk.AuthenticateRequest.model_fields["method_id"].alias == "methodId"
    assert sdk.AuthenticateRequest.model_fields["field_meta"].alias == "_meta"
    assert sdk.InitializeResponse.model_fields["agent_capabilities"].alias == "agentCapabilities"
    assert sdk.AgentCapabilities.model_fields["load_session"].alias == "loadSession"
    assert sdk.PromptCapabilities.model_fields["embedded_context"].alias == "embeddedContext"
    assert getattr(sdk.Agent.initialize, "__param_model__") is sdk.InitializeRequest
    assert getattr(sdk.Agent.authenticate, "__param_model__") is sdk.AuthenticateRequest
    assert getattr(sdk.Agent.new_session, "__param_model__") is sdk.NewSessionRequest
    assert getattr(sdk.Agent.load_session, "__param_model__") is sdk.LoadSessionRequest
    assert getattr(sdk.Agent.prompt, "__param_model__") is sdk.PromptRequest
    assert getattr(sdk.Agent.cancel, "__param_model__") is sdk.CancelNotification


def test_pinned_session_update_models_and_aliases() -> None:
    assert sdk.SessionNotification.model_fields["session_id"].alias == "sessionId"
    assert sdk.SessionNotification.model_fields["field_meta"].alias == "_meta"
    assert sdk.UserMessageChunk.model_fields["message_id"].alias == "messageId"
    assert sdk.UserMessageChunk.model_fields["field_meta"].alias == "_meta"
    assert sdk.AgentMessageChunk.model_fields["message_id"].alias == "messageId"
    assert sdk.AgentMessageChunk.model_fields["field_meta"].alias == "_meta"
    assert sdk.ResourceContentBlock.model_fields["mime_type"].alias == "mimeType"
    assert sdk.ToolCallStart.model_fields["tool_call_id"].alias == "toolCallId"
    assert sdk.ToolCallStart.model_fields["raw_input"].alias == "rawInput"
    assert sdk.ToolCallStart.model_fields["raw_output"].alias == "rawOutput"
    assert sdk.ToolCallProgress.model_fields["tool_call_id"].alias == "toolCallId"
    assert sdk.ToolCallProgress.model_fields["raw_input"].alias == "rawInput"
    assert sdk.ToolCallProgress.model_fields["raw_output"].alias == "rawOutput"
    assert sdk.AgentPlanUpdate.model_fields["field_meta"].alias == "_meta"
    assert getattr(sdk.Client.session_update, "__param_model__") is sdk.SessionNotification


def test_pinned_session_update_wire_shapes() -> None:
    sequence = {"mimir.sequence": 4}
    text = sdk.TextContentBlock(type="text", text="hello")
    user = sdk.UserMessageChunk(
        content=text,
        sessionUpdate="user_message_chunk",
        _meta=sequence,
    )
    agent = sdk.AgentMessageChunk(
        content=text,
        sessionUpdate="agent_message_chunk",
        _meta=sequence,
    )
    tool_start = sdk.ToolCallStart(
        toolCallId="tool-1",
        title="example",
        kind="other",
        status="pending",
        sessionUpdate="tool_call",
        _meta=sequence,
    )
    tool_progress = sdk.ToolCallProgress(
        toolCallId="tool-1",
        status="completed",
        rawInput={"value": 1},
        rawOutput={"ok": True},
        sessionUpdate="tool_call_update",
        _meta=sequence,
    )
    plan = sdk.AgentPlanUpdate(
        entries=[sdk.PlanEntry(content="work", priority="medium", status="pending")],
        sessionUpdate="plan",
        _meta=sequence,
    )
    assert user.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "content": {"type": "text", "text": "hello"},
        "_meta": sequence,
        "sessionUpdate": "user_message_chunk",
    }
    assert agent.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "content": {"type": "text", "text": "hello"},
        "_meta": sequence,
        "sessionUpdate": "agent_message_chunk",
    }
    assert tool_start.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "toolCallId": "tool-1",
        "title": "example",
        "kind": "other",
        "status": "pending",
        "_meta": sequence,
        "sessionUpdate": "tool_call",
    }
    assert tool_progress.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "toolCallId": "tool-1",
        "status": "completed",
        "rawInput": {"value": 1},
        "rawOutput": {"ok": True},
        "_meta": sequence,
        "sessionUpdate": "tool_call_update",
    }
    assert plan.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "entries": [{"content": "work", "priority": "medium", "status": "pending"}],
        "_meta": sequence,
        "sessionUpdate": "plan",
    }

def test_pinned_agent_signatures_and_error_objects() -> None:
    assert list(inspect.signature(sdk.Agent.initialize).parameters) == [
        "self", "protocol_version", "client_capabilities", "client_info", "kwargs"
    ]
    assert list(inspect.signature(sdk.Agent.authenticate).parameters) == [
        "self", "method_id", "kwargs"
    ]
    assert list(inspect.signature(sdk.Agent.new_session).parameters) == [
        "self", "cwd", "additional_directories", "mcp_servers", "kwargs"
    ]
    assert list(inspect.signature(sdk.Agent.load_session).parameters) == [
        "self", "cwd", "session_id", "mcp_servers", "additional_directories", "kwargs"
    ]
    assert list(inspect.signature(sdk.Agent.prompt).parameters) == [
        "self", "session_id", "prompt", "kwargs"
    ]
    runner_parameters = inspect.signature(sdk.run_stdio_agent).parameters
    assert list(runner_parameters) == ["agent", "request_reader", "response_writer"]
    assert runner_parameters["request_reader"].kind is inspect.Parameter.KEYWORD_ONLY
    assert runner_parameters["response_writer"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sdk.AUTH_METHOD_ID == "mimir-web-key"
    assert sdk.auth_required_error().to_error_obj() == {
        "code": -32000, "message": "Authentication required",
        "data": {"methodId": "mimir-web-key"},
    }
    assert sdk.method_not_found_error("_example").to_error_obj() == {
        "code": -32601, "message": "Method not found", "data": {"method": "_example"},
    }
    assert sdk.invalid_params_error().to_error_obj() == {
        "code": -32602, "message": "Invalid params", "data": None,
    }
    assert sdk.unknown_connection_error().to_error_obj() == {
        "code": -32602, "message": "Unknown MCP connection", "data": None,
    }
    assert sdk.internal_error().to_error_obj() == {
        "code": -32603, "message": "Internal error", "data": None,
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
@pytest.mark.parametrize(
    "response,decision,has_error",
    [
        (
            {"result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}}},
            "allow_once",
            False,
        ),
        (
            {"result": {"outcome": {"outcome": "selected", "optionId": "reject_once"}}},
            "reject_once",
            False,
        ),
        ({"result": {"outcome": {"outcome": "cancelled"}}}, "cancelled", False),
        (
            {
                "error": {
                    "code": -32001,
                    "message": "permission client error",
                    "data": {"reason": "closed"},
                }
            },
            "reject_once",
            True,
        ),
    ],
)
async def test_public_connection_permission_completion_correlates_exact_wire_outcomes(
    response: dict[str, Any], decision: str, has_error: bool
) -> None:
    transport = MemoryTransport()
    connection = sdk.Connection(
        lambda *_: asyncio.sleep(0),
        transport,
        state_store=sdk.StrictMessageStateStore(),
    )
    peer = sdk.AcpPeer(connection, ContractAgent())
    snapshot = sdk.PermissionSnapshot("tool-1", "Run", "execute", {"command": "true"})

    permission_task = asyncio.create_task(peer.request_tool_permission("session-1", snapshot))
    emitted = await transport.outgoing.get()
    assert emitted == {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "session/request_permission",
        "params": {
            "sessionId": "session-1",
            "toolCall": {
                "toolCallId": "tool-1",
                "title": "Run",
                "kind": "execute",
                "status": "pending",
                "rawInput": {"command": "true"},
            },
            "options": [
                {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
            ],
        },
    }
    await transport.incoming.put({"jsonrpc": "2.0", "id": emitted["id"], **response})
    completion = await permission_task
    assert completion.decision == decision
    assert completion.executable is (decision == "allow_once" and not has_error)
    if has_error:
        assert isinstance(completion.error, sdk.RequestError)
        assert completion.error.to_error_obj() == response["error"]
    else:
        assert completion.error is None
    await connection.close()


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


class MemoryTransport:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def receive(self) -> dict[str, Any] | None:
        return await self.incoming.get()

    async def send(self, message: dict[str, Any]) -> None:
        sdk.validate_jsonrpc_envelope(message)
        await self.outgoing.put(message)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "model,payload",
    [
        (sdk.ConnectMcpRequest, {"serverId": 1}),
        (sdk.ConnectMcpResponse, {"connectionId": 1}),
        (sdk.MessageMcpRequest, {"connectionId": "c", "method": 1}),
        (sdk.MessageMcpRequest, {"connectionId": "c", "method": "ping", "params": []}),
        (sdk.MessageMcpNotification, {"connectionId": 1, "method": "notice"}),
        (sdk.DisconnectMcpRequest, {"connectionId": 1}),
    ],
)
def test_mcp_schema_v119_rejects_each_malformed_typed_field(
    model: type[sdk.StrictSchemaModel], payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.asyncio
async def test_public_connection_exact_bidirectional_mcp_wire_and_generic_ids() -> None:
    transport = MemoryTransport()
    holder: dict[str, sdk.AcpPeer] = {}
    agent = ContractAgent()

    async def route(method: str, params: Any, is_notification: bool) -> Any:
        assert method == sdk.MCP_MESSAGE_METHOD
        return await holder["peer"].route_mcp(params, is_notification)

    connection = sdk.Connection(
        route, transport, state_store=sdk.StrictMessageStateStore()
    )
    peer = sdk.AcpPeer(connection, agent)
    holder["peer"] = peer
    connect_task = asyncio.create_task(peer.connect_mcp("server-1"))
    assert await transport.outgoing.get() == {
        "jsonrpc": "2.0", "id": 0, "method": "mcp/connect",
        "params": {"serverId": "server-1"},
    }
    await transport.incoming.put(
        {"jsonrpc": "2.0", "id": 0, "result": {"connectionId": "connection-1"}}
    )
    assert await connect_task == "connection-1"

    message_task = asyncio.create_task(
        peer.message_mcp("connection-1", "tools/call", {"name": "read", "arguments": {"path": None}})
    )
    assert await transport.outgoing.get() == {
        "jsonrpc": "2.0", "id": 1, "method": "mcp/message",
        "params": {
            "connectionId": "connection-1", "method": "tools/call",
            "params": {"name": "read", "arguments": {"path": None}},
        },
    }
    await transport.incoming.put(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "ok"}]}}
    )
    assert await message_task == {"content": [{"type": "text", "text": "ok"}]}

    await peer.notify_mcp("connection-1", "notifications/initialized")
    assert await transport.outgoing.get() == {
        "jsonrpc": "2.0", "method": "mcp/message",
        "params": {"connectionId": "connection-1", "method": "notifications/initialized"},
    }

    for outer_id, nested in (("object-id", {"nested": [1, None]}), (91, None)):
        params: dict[str, Any] = {"connectionId": "connection-1", "method": "ping"}
        if outer_id == "object-id":
            params["params"] = nested
        else:
            params["params"] = None
        await transport.incoming.put(
            {"jsonrpc": "2.0", "id": outer_id, "method": "mcp/message", "params": params}
        )
        assert await transport.outgoing.get() == {
            "jsonrpc": "2.0", "id": outer_id, "result": {}
        }
    await transport.incoming.put(
        {
            "jsonrpc": "2.0", "id": "absent-id", "method": "mcp/message",
            "params": {"connectionId": "connection-1", "method": "ping"},
        }
    )
    assert await transport.outgoing.get() == {
        "jsonrpc": "2.0", "id": "absent-id", "result": {}
    }
    await transport.incoming.put(
        {
            "jsonrpc": "2.0", "method": "mcp/message",
            "params": {
                "connectionId": "connection-1", "method": "notifications/message",
                "params": {"level": "info", "data": {"nested": True}},
            },
        }
    )
    await agent.mcp_event.wait()
    assert agent.mcp_notifications == [
        ("connection-1", "notifications/message", {"level": "info", "data": {"nested": True}})
    ]
    await transport.incoming.put(
        {
            "jsonrpc": "2.0", "id": "unknown-request", "method": "mcp/message",
            "params": {"connectionId": "missing", "method": "ping"},
        }
    )
    assert await transport.outgoing.get() == {
        "jsonrpc": "2.0", "id": "unknown-request",
        "error": {"code": -32602, "message": "Unknown MCP connection", "data": None},
    }
    await transport.incoming.put(
        {
            "jsonrpc": "2.0", "method": "mcp/message",
            "params": {"connectionId": "missing", "method": "notifications/message"},
        }
    )

    disconnect_task = asyncio.create_task(peer.disconnect_mcp("connection-1"))
    assert await transport.outgoing.get() == {
        "jsonrpc": "2.0", "id": 2, "method": "mcp/disconnect",
        "params": {"connectionId": "connection-1"},
    }
    await transport.incoming.put({"jsonrpc": "2.0", "id": 2, "result": {}})
    await disconnect_task
    await connection.close()
    assert transport.closed is True


@pytest.mark.asyncio
async def test_public_connection_mcp_request_correlates_direct_client_error() -> None:
    transport = MemoryTransport()
    connection = sdk.Connection(
        lambda *_: asyncio.sleep(0),
        transport,
        state_store=sdk.StrictMessageStateStore(),
    )
    peer = sdk.AcpPeer(connection, ContractAgent())

    connect_task = asyncio.create_task(peer.connect_mcp("server-error"))
    connect_request = await transport.outgoing.get()
    assert connect_request == {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "mcp/connect",
        "params": {"serverId": "server-error"},
    }
    await transport.incoming.put(
        {
            "jsonrpc": "2.0",
            "id": connect_request["id"],
            "result": {"connectionId": "connection-error"},
        }
    )
    assert await connect_task == "connection-error"

    message_task = asyncio.create_task(
        peer.message_mcp(
            "connection-error",
            "tools/call",
            {"name": "read", "arguments": {"path": "missing"}},
        )
    )
    message_request = await transport.outgoing.get()
    assert message_request == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "mcp/message",
        "params": {
            "connectionId": "connection-error",
            "method": "tools/call",
            "params": {"name": "read", "arguments": {"path": "missing"}},
        },
    }
    client_error = {
        "code": -32002,
        "message": "MCP client error",
        "data": {"serverId": "server-error"},
    }
    await transport.incoming.put(
        {"jsonrpc": "2.0", "id": message_request["id"], "error": client_error}
    )
    with pytest.raises(sdk.RequestError) as exc_info:
        await message_task
    assert exc_info.value.to_error_obj() == client_error
    await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["success", "error"])
async def test_public_connection_correlates_direct_results_and_rejects_duplicates(
    kind: str,
) -> None:
    transport = MemoryTransport()
    state = sdk.StrictMessageStateStore()
    connection = sdk.Connection(
        lambda *_: asyncio.sleep(0), transport, listening=False, state_store=state
    )
    future = state.register_outgoing("generic-outer", "mcp/message")
    response = (
        {"jsonrpc": "2.0", "id": "generic-outer", "result": {"ok": True}}
        if kind == "success"
        else {
            "jsonrpc": "2.0", "id": "generic-outer",
            "error": {"code": -32001, "message": "client error", "data": {"nested": True}},
        }
    )
    await transport.incoming.put(response)
    await transport.incoming.put(response)
    with pytest.raises(sdk.AcpProtocolError, match="Duplicate or late"):
        await connection.main_loop()
    if kind == "success":
        assert await future == {"ok": True}
    else:
        with pytest.raises(sdk.RequestError) as exc_info:
            await future
        assert exc_info.value.to_error_obj() == {
            "code": -32001, "message": "client error", "data": {"nested": True}
        }
    await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [False, True])
async def test_public_connection_rejects_late_after_rejection_and_closed_outcomes(
    closed: bool,
) -> None:
    transport = MemoryTransport()
    state = sdk.StrictMessageStateStore()
    connection = sdk.Connection(
        lambda *_: asyncio.sleep(0), transport, listening=False, state_store=state
    )
    future = state.register_outgoing("late-id", "session/request_permission")
    state.reject_all_outgoing(ConnectionError("rejected"))
    with pytest.raises(ConnectionError, match="rejected"):
        await future
    if closed:
        await connection.close()
    else:
        await transport.incoming.put(
            {"jsonrpc": "2.0", "id": "late-id", "result": {"outcome": {"outcome": "cancelled"}}}
        )
        await transport.incoming.put(None)
        with pytest.raises(sdk.AcpProtocolError, match="Duplicate or late"):
            await connection.main_loop()
        await connection.close()
    with pytest.raises(ConnectionError, match="Connection closed"):
        await connection.send_request("mcp/connect", {"serverId": "late"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected_primary",
    [
        ("none", None),
        ("connect", "connect failed"),
        ("closed", "closed failed"),
        ("close", "close failed"),
        ("connect_close", "connect failed"),
        ("closed_close", "closed failed"),
    ],
)
async def test_runner_closes_and_generation_teardown_is_exception_safe(
    monkeypatch: pytest.MonkeyPatch, failure: str, expected_primary: str | None
) -> None:
    instances: list[Any] = []
    events: list[tuple[str, int | None]] = []

    class OwnedConnection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.closed = False
            instances.append(self)

        async def main_loop(self) -> None:
            return None

        async def close(self) -> None:
            events.append(("close", None))
            self.closed = True
            if failure in {"close", "connect_close", "closed_close"}:
                raise RuntimeError("close failed")

    class OwnedAgent:
        def __init__(self) -> None:
            self.closed_generations: list[int] = []

        def on_connect(self, peer: sdk.AcpPeer) -> int:
            events.append(("connect", None))
            if failure in {"connect", "connect_close"}:
                raise RuntimeError("connect failed")
            return 37

        async def on_transport_closed(self, generation: int) -> None:
            events.append(("transport_closed", generation))
            self.closed_generations.append(generation)
            if failure in {"closed", "closed_close"}:
                raise RuntimeError("closed failed")

    monkeypatch.setattr(sdk, "Connection", OwnedConnection)
    reader = asyncio.StreamReader()
    protocol = _DrainProtocol()
    transport = _ReservedFrameTransport(io.BytesIO(), protocol)
    writer = asyncio.StreamWriter(transport, protocol, None, asyncio.get_running_loop())
    agent = OwnedAgent()
    if expected_primary is None:
        await sdk.run_stdio_agent(agent, request_reader=reader, response_writer=writer)
    else:
        with pytest.raises(RuntimeError, match=expected_primary) as exc_info:
            await sdk.run_stdio_agent(agent, request_reader=reader, response_writer=writer)
        assert str(exc_info.value) == expected_primary
    generation = 0 if failure in {"connect", "connect_close"} else 37
    assert events == [("connect", None), ("transport_closed", generation), ("close", None)]
    assert instances[0].closed is True
    assert agent.closed_generations == [generation]


@pytest.mark.asyncio
@pytest.mark.parametrize("main_loop_error", [None, RuntimeError("receive failed")])
async def test_runner_marks_peer_dead_before_draining_queued_handlers(
    monkeypatch: pytest.MonkeyPatch, main_loop_error: RuntimeError | None
) -> None:
    instances: list[Any] = []
    handler_tasks: list[asyncio.Task[Any]] = []
    rejected = asyncio.Event()
    callback_done = asyncio.Event()

    class OwnedConnection:
        def __init__(self, route: Any, *args: Any, queue: Any, **kwargs: Any) -> None:
            self.route = route
            self.queue = queue
            self.closed = False
            instances.append(self)

        async def main_loop(self) -> None:
            await self.queue.publish(SimpleNamespace())

            async def handle() -> None:
                try:
                    while not agent.peer.closed:
                        await asyncio.sleep(0)
                    await self.route("queued/request", {}, False)
                finally:
                    self.queue.task_done()

            handler_tasks.append(asyncio.create_task(handle()))
            await asyncio.sleep(0)
            if main_loop_error is not None:
                raise main_loop_error

        async def close(self) -> None:
            self.closed = True
            await asyncio.gather(*handler_tasks)

    class OwnedAgent:
        peer: sdk.AcpPeer

        def on_connect(self, peer: sdk.AcpPeer) -> int:
            self.peer = peer
            return 41

        async def on_transport_closed(self, generation: int) -> None:
            assert generation == 41
            assert self.peer.closed is True
            callback_done.set()

    agent = OwnedAgent()

    async def route(method: str, params: Any, is_notification: bool) -> Any:
        assert method == "queued/request"
        with pytest.raises(ConnectionError, match="Connection closed"):
            await agent.peer.start_request("session/prompt", {})
        rejected.set()
        return None

    monkeypatch.setattr(sdk, "Connection", OwnedConnection)
    monkeypatch.setattr(sdk, "build_agent_router", lambda *args, **kwargs: route)
    reader = asyncio.StreamReader()
    protocol = _DrainProtocol()
    transport = _ReservedFrameTransport(io.BytesIO(), protocol)
    writer = asyncio.StreamWriter(transport, protocol, None, asyncio.get_running_loop())

    if main_loop_error is None:
        await asyncio.wait_for(
            sdk.run_stdio_agent(agent, request_reader=reader, response_writer=writer), 1
        )
    else:
        with pytest.raises(RuntimeError, match="receive failed") as exc_info:
            await asyncio.wait_for(
                sdk.run_stdio_agent(agent, request_reader=reader, response_writer=writer), 1
            )
        assert exc_info.value is main_loop_error

    assert rejected.is_set()
    assert callback_done.is_set()
    assert instances[0].closed is True
    assert handler_tasks and all(task.done() and not task.cancelled() for task in handler_tasks)


def test_permission_meta_matrix_is_independent_and_cancelled_inner_meta_is_illegal() -> None:
    omitted = object()
    for decision in ("allow_once", "reject_once"):
        for top_meta in (omitted, None, {"top": True}):
            for selected_meta in (omitted, None, {"inner": True}):
                outcome: dict[str, Any] = {"outcome": "selected", "optionId": decision}
                if selected_meta is not omitted:
                    outcome["_meta"] = selected_meta
                payload: dict[str, Any] = {"outcome": outcome}
                if top_meta is not omitted:
                    payload["_meta"] = top_meta
                assert sdk.validate_permission_response(payload) == decision
    for malformed in ([], "bad", 1, True):
        with pytest.raises(sdk.AcpProtocolError):
            sdk.validate_permission_response(
                {"outcome": {"outcome": "selected", "optionId": "allow_once", "_meta": malformed}}
            )
    with pytest.raises(sdk.AcpProtocolError):
        sdk.validate_permission_response({"outcome": {"outcome": "cancelled", "_meta": None}})


@pytest.mark.asyncio
async def test_public_request_handle_exposes_outer_id_and_quarantines_one_late_reply() -> None:
    transport = MemoryTransport()
    state = sdk.StrictMessageStateStore()
    connection = sdk.Connection(lambda *_: asyncio.sleep(0), transport, state_store=state)
    peer = sdk.AcpPeer(connection, ContractAgent(), state)
    snapshot = sdk.PermissionSnapshot("tool", "Run", "execute", {"command": "true"})

    handle = await peer.start_tool_permission("session", snapshot)
    emitted = await transport.outgoing.get()
    assert handle.outer_id == emitted["id"] == 0
    assert handle.task.done() is False
    handle.abandon()
    with pytest.raises(asyncio.CancelledError):
        await handle.task
    await transport.incoming.put({"jsonrpc": "2.0", "id": 0, "result": {"outcome": {"outcome": "cancelled"}}})

    second = await peer.start_tool_permission("session", snapshot)
    emitted_second = await transport.outgoing.get()
    assert second.outer_id == emitted_second["id"] == 1
    await transport.incoming.put({"jsonrpc": "2.0", "id": 1, "result": {"outcome": {"outcome": "selected", "optionId": "reject_once"}}})
    assert (await second.task).decision == "reject_once"
    with pytest.raises(sdk.AcpProtocolError, match="Duplicate or late"):
        state.resolve_outgoing(1, {})
    await connection.close()


@pytest.mark.asyncio
async def test_mcp_handle_cancel_notification_uses_exact_outer_id_and_retains_connection() -> None:
    transport = MemoryTransport()
    state = sdk.StrictMessageStateStore()
    connection = sdk.Connection(lambda *_: asyncio.sleep(0), transport, state_store=state)
    peer = sdk.AcpPeer(connection, ContractAgent(), state)
    peer._active_connections.add("connection")

    handle = await peer.start_mcp_request("connection", "tools/call", {"name": "read", "arguments": {}})
    request = await transport.outgoing.get()
    assert handle.outer_id == request["id"] == 0
    await peer.notify_mcp("connection", "notifications/cancelled", {"requestId": handle.outer_id})
    notification = await transport.outgoing.get()
    assert notification == {
        "jsonrpc": "2.0",
        "method": "mcp/message",
        "params": {
            "connectionId": "connection",
            "method": "notifications/cancelled",
            "params": {"requestId": 0},
        },
    }
    handle.abandon()
    await transport.incoming.put({"jsonrpc": "2.0", "id": 0, "result": {"content": []}})
    next_handle = await peer.start_mcp_request("connection", "tools/list", {})
    next_request = await transport.outgoing.get()
    assert next_request["id"] == next_handle.outer_id == 1
    await transport.incoming.put({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    assert await next_handle.task == {"tools": []}
    await connection.close()


@pytest.mark.asyncio
async def test_request_handle_start_failure_does_not_wait_for_unregistered_id() -> None:
    transport = MemoryTransport()
    state = sdk.StrictMessageStateStore()
    connection = sdk.Connection(
        lambda *_: asyncio.sleep(0), transport, state_store=state
    )
    peer = sdk.AcpPeer(connection, ContractAgent(), state)
    await connection.close()

    with pytest.raises(Exception):
        await asyncio.wait_for(
            peer.start_request("session/request_permission", {}), timeout=0.1
        )


@pytest.mark.asyncio
async def test_request_registration_is_per_invocation_under_same_method_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MemoryTransport()
    state = sdk.StrictMessageStateStore()
    connection = sdk.Connection(lambda *_: asyncio.sleep(0), transport, state_store=state)
    peer = sdk.AcpPeer(connection, ContractAgent(), state)
    send_request = connection.send_request
    entered = {marker: asyncio.Event() for marker in ("one", "two")}
    proceed = {marker: asyncio.Event() for marker in ("one", "two")}

    async def controlled_send_request(method: str, params: Any = None) -> Any:
        marker = params["marker"]
        entered[marker].set()
        await proceed[marker].wait()
        return await send_request(method, params)

    monkeypatch.setattr(connection, "send_request", controlled_send_request)
    start_one = asyncio.create_task(
        peer.start_request("session/request_permission", {"marker": "one"})
    )
    await entered["one"].wait()
    start_two = asyncio.create_task(
        peer.start_request("session/request_permission", {"marker": "two"})
    )
    await entered["two"].wait()

    proceed["two"].set()
    request_two = await transport.outgoing.get()
    handle_two = await start_two
    proceed["one"].set()
    request_one = await transport.outgoing.get()
    handle_one = await start_one

    assert request_one["params"]["marker"] == "one"
    assert handle_one.outer_id == request_one["id"] == 1
    assert request_two["params"]["marker"] == "two"
    assert handle_two.outer_id == request_two["id"] == 0
    await transport.incoming.put(
        {"jsonrpc": "2.0", "id": request_two["id"], "result": {"marker": "two"}}
    )
    await transport.incoming.put(
        {"jsonrpc": "2.0", "id": request_one["id"], "result": {"marker": "one"}}
    )
    assert await handle_one.task == {"marker": "one"}
    assert await handle_two.task == {"marker": "two"}
    await connection.close()


@pytest.mark.asyncio
async def test_request_cancelled_after_prepare_before_registration_leaves_no_stale_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MemoryTransport()
    state = sdk.StrictMessageStateStore()
    connection = sdk.Connection(lambda *_: asyncio.sleep(0), transport, state_store=state)
    peer = sdk.AcpPeer(connection, ContractAgent(), state)
    send_request = connection.send_request
    entered = asyncio.Event()
    proceed = asyncio.Event()
    exited = asyncio.Event()
    send_tasks: list[asyncio.Task[Any]] = []

    async def controlled_send_request(method: str, params: Any = None) -> Any:
        if params["marker"] != "cancelled":
            return await send_request(method, params)
        task = asyncio.current_task()
        assert task is not None
        send_tasks.append(task)
        entered.set()
        try:
            await proceed.wait()
            return await send_request(method, params)
        finally:
            exited.set()

    monkeypatch.setattr(connection, "send_request", controlled_send_request)
    cancelled = asyncio.create_task(
        peer.start_request("session/request_permission", {"marker": "cancelled"})
    )
    await entered.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await exited.wait()
    await asyncio.gather(*send_tasks, return_exceptions=True)
    assert all(task.done() for task in send_tasks)
    assert transport.outgoing.empty()

    survivor = await peer.start_request(
        "session/request_permission", {"marker": "survivor"}
    )
    request = await transport.outgoing.get()
    assert request["params"]["marker"] == "survivor"
    assert survivor.outer_id == request["id"] == 0
    await transport.incoming.put(
        {"jsonrpc": "2.0", "id": request["id"], "result": {"marker": "survivor"}}
    )
    assert await survivor.task == {"marker": "survivor"}
    with pytest.raises(sdk.AcpProtocolError, match="Duplicate or late"):
        state.resolve_outgoing(request["id"], {"marker": "stale"})
    await connection.close()


@pytest.mark.asyncio
async def test_request_connection_closed_after_prepare_before_registration_cleans_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MemoryTransport()
    state = sdk.StrictMessageStateStore()
    connection = sdk.Connection(lambda *_: asyncio.sleep(0), transport, state_store=state)
    peer = sdk.AcpPeer(connection, ContractAgent(), state)
    send_request = connection.send_request
    entered = asyncio.Event()
    proceed = asyncio.Event()
    exited = asyncio.Event()
    send_tasks: list[asyncio.Task[Any]] = []

    async def controlled_send_request(method: str, params: Any = None) -> Any:
        task = asyncio.current_task()
        assert task is not None
        send_tasks.append(task)
        entered.set()
        try:
            await proceed.wait()
            return await send_request(method, params)
        finally:
            exited.set()

    monkeypatch.setattr(connection, "send_request", controlled_send_request)
    closing = asyncio.create_task(
        peer.start_request("session/request_permission", {"marker": "closing"})
    )
    await entered.wait()
    await connection.close()
    proceed.set()
    with pytest.raises(Exception):
        await closing
    await exited.wait()
    await asyncio.gather(*send_tasks, return_exceptions=True)
    assert all(task.done() for task in send_tasks)
    assert transport.outgoing.empty()

    fresh_transport = MemoryTransport()
    fresh_state = sdk.StrictMessageStateStore()
    fresh_connection = sdk.Connection(
        lambda *_: asyncio.sleep(0), fresh_transport, state_store=fresh_state
    )
    fresh_peer = sdk.AcpPeer(fresh_connection, ContractAgent(), fresh_state)
    fresh = await fresh_peer.start_request(
        "session/request_permission", {"marker": "fresh"}
    )
    request = await fresh_transport.outgoing.get()
    assert request["params"]["marker"] == "fresh"
    assert fresh.outer_id == request["id"] == 0
    await fresh_transport.incoming.put(
        {"jsonrpc": "2.0", "id": request["id"], "result": {"marker": "fresh"}}
    )
    assert await fresh.task == {"marker": "fresh"}
    with pytest.raises(sdk.AcpProtocolError, match="Duplicate or late"):
        fresh_state.resolve_outgoing(request["id"], {"marker": "stale"})
    await fresh_connection.close()


@pytest.mark.asyncio
async def test_request_store_close_unknown_duplicate_and_abandon_contract() -> None:
    transport = MemoryTransport()
    state = sdk.StrictMessageStateStore()
    connection = sdk.Connection(lambda *_: asyncio.sleep(0), transport, state_store=state)
    peer = sdk.AcpPeer(connection, ContractAgent(), state)

    with pytest.raises(sdk.AcpProtocolError, match="Duplicate or late"):
        state.resolve_outgoing(404, {})
    with pytest.raises(sdk.AcpProtocolError, match="Duplicate or late"):
        state.reject_outgoing(404, RuntimeError("unknown"))

    handle = await peer.start_request("phase/close", {})
    await transport.outgoing.get()
    await connection.close()
    with pytest.raises(Exception):
        await handle.task
    assert state._outgoing == {}

    abandoned_state = sdk.StrictMessageStateStore()
    future = abandoned_state.register_outgoing(7, "phase/abandon")
    abandoned_state.abandon_outgoing(7)
    with pytest.raises(asyncio.CancelledError):
        await future
    abandoned_state.resolve_outgoing(7, {})
    with pytest.raises(sdk.AcpProtocolError, match="Duplicate or late"):
        abandoned_state.resolve_outgoing(7, {})


@pytest.mark.asyncio
async def test_ndjson_frame_limit_is_counted_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    base = b'{"jsonrpc":"2.0","method":"x","params":{"value":""}}'
    payload = base[:-3] + (b"a" * (sdk.MAX_FRAME_BYTES - len(base))) + base[-3:]
    assert len(payload) == sdk.MAX_FRAME_BYTES
    reader = asyncio.StreamReader()
    reader.feed_data(payload + b"\n")
    reader.feed_eof()
    transport = sdk.StrictNdjsonTransport(reader, SimpleNamespace())
    message = await transport.receive()
    assert message is not None
    assert transport.take_frame_bytes() == sdk.MAX_FRAME_BYTES

    decoded = False
    original_loads = sdk.json.loads

    def loads(value: Any) -> Any:
        nonlocal decoded
        decoded = True
        return original_loads(value)

    monkeypatch.setattr(sdk.json, "loads", loads)
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * (sdk.MAX_FRAME_BYTES + 1))
    reader.feed_eof()
    transport = sdk.StrictNdjsonTransport(reader, SimpleNamespace())
    with pytest.raises(sdk.AcpProtocolError, match="size limit"):
        await transport.receive()
    assert decoded is False
    assert len(transport._buffer) == sdk.MAX_FRAME_BYTES + 1


@pytest.mark.asyncio
async def test_pending_request_limit_admits_exact_boundary() -> None:
    store = sdk.StrictMessageStateStore()
    for request_id in range(sdk.MAX_PENDING_REQUESTS):
        store.register_outgoing(request_id, "method")
    with pytest.raises(sdk.AcpProtocolError, match="Too many pending"):
        store.register_outgoing(sdk.MAX_PENDING_REQUESTS, "method")
    for request_id in range(sdk.MAX_PENDING_REQUESTS):
        store.abandon_outgoing(request_id)


@pytest.mark.asyncio
async def test_input_queue_item_limit_times_out_only_after_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sdk, "INPUT_QUEUE_DRAIN_TIMEOUT", 0.01)
    queue = sdk.BoundedMessageQueue()
    tasks = [SimpleNamespace(message={"jsonrpc": "2.0", "method": "x", "params": {"n": index}}) for index in range(sdk.INPUT_QUEUE_MAX_ITEMS)]
    for task in tasks:
        await queue.publish(task)
    with pytest.raises(sdk.AcpProtocolError, match="drain timed out"):
        await queue.publish(SimpleNamespace(message={"jsonrpc": "2.0", "method": "x"}))
    assert queue._queue.qsize() == sdk.INPUT_QUEUE_MAX_ITEMS
    while not queue._queue.empty():
        await queue._queue.get()
        queue.task_done()


@pytest.mark.asyncio
async def test_input_queue_byte_limit_admits_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sdk, "INPUT_QUEUE_DRAIN_TIMEOUT", 0.01)
    queue = sdk.BoundedMessageQueue()
    base = {"value": ""}
    overhead = len(sdk.json.dumps(base, ensure_ascii=False, separators=(",", ":")).encode())
    task = SimpleNamespace(message={"value": "a" * (sdk.INPUT_QUEUE_MAX_BYTES - overhead)})
    await queue.publish(task)
    assert queue.pending_bytes == sdk.INPUT_QUEUE_MAX_BYTES
    with pytest.raises(sdk.AcpProtocolError, match="drain timed out"):
        await queue.publish(SimpleNamespace(message={"x": 1}))
    await queue._queue.get()
    queue.task_done()
    assert queue.pending_bytes == 0


@pytest.mark.asyncio
async def test_input_queue_capacity_released_before_deadline_admits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sdk, "INPUT_QUEUE_DRAIN_TIMEOUT", 0.2)
    queue = sdk.BoundedMessageQueue()
    for index in range(sdk.INPUT_QUEUE_MAX_ITEMS):
        await queue.publish(SimpleNamespace(message={"index": index}))

    waiting = asyncio.create_task(
        queue.publish(SimpleNamespace(message={"index": "waiting"}))
    )
    await asyncio.sleep(0)
    assert waiting.done() is False
    await queue._queue.get()
    queue.task_done()
    await asyncio.wait_for(waiting, 0.1)
    assert queue._queue.qsize() == sdk.INPUT_QUEUE_MAX_ITEMS

    while not queue._queue.empty():
        await queue._queue.get()
        queue.task_done()


@pytest.mark.asyncio
async def test_input_queue_capacity_held_rejects_only_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = 0.05
    monkeypatch.setattr(sdk, "INPUT_QUEUE_DRAIN_TIMEOUT", timeout)
    queue = sdk.BoundedMessageQueue()
    for index in range(sdk.INPUT_QUEUE_MAX_ITEMS):
        await queue.publish(SimpleNamespace(message={"index": index}))

    loop = asyncio.get_running_loop()
    started = loop.time()
    waiting = asyncio.create_task(
        queue.publish(SimpleNamespace(message={"index": "waiting"}))
    )
    await asyncio.sleep(timeout / 2)
    assert waiting.done() is False
    with pytest.raises(sdk.AcpProtocolError, match="drain timed out"):
        await waiting
    assert loop.time() - started >= timeout
    assert queue._queue.qsize() == sdk.INPUT_QUEUE_MAX_ITEMS

    while not queue._queue.empty():
        await queue._queue.get()
        queue.task_done()
