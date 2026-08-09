from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from acp import PROTOCOL_VERSION, RequestError
from acp.agent.router import build_agent_router
from acp.connection import Connection
from acp.interfaces import Agent, Client
from acp.meta import AGENT_METHODS, CLIENT_METHODS
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AgentMessageChunk,
    AgentPlanUpdate,
    AudioContentBlock,
    AuthenticateRequest,
    AuthenticateResponse,
    AuthMethodAgent,
    CancelNotification,
    ClientCapabilities,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    Implementation,
    InitializeRequest,
    InitializeResponse,
    LoadSessionRequest,
    LoadSessionResponse,
    McpCapabilities,
    NewSessionRequest,
    NewSessionResponse,
    PermissionOption,
    PlanEntry,
    PromptCapabilities,
    PromptRequest,
    PromptResponse,
    ResourceContentBlock,
    SessionNotification,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UserMessageChunk,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


if PROTOCOL_VERSION != 1:
    raise ImportError(f"unsupported ACP protocol version: {PROTOCOL_VERSION}")


AUTH_METHOD_ID = "mimir-web-key"
MCP_CONNECT_METHOD = "mcp/connect"
MCP_MESSAGE_METHOD = "mcp/message"
MCP_DISCONNECT_METHOD = "mcp/disconnect"
PERMISSION_METHOD = CLIENT_METHODS["session_request_permission"]
MCP_REQUEST_METHODS = frozenset({"initialize", "tools/list", "tools/call"})
MCP_NOTIFICATION_METHODS = frozenset({"notifications/initialized", "notifications/cancelled"})
MCP_INBOUND_NOTIFICATIONS = frozenset(
    {"notifications/tools/list_changed", "notifications/progress", "notifications/message"}
)


class AcpProtocolError(RuntimeError):
    pass


@dataclass(slots=True)
class IncomingRequestState:
    method: str
    params: Any
    status: str = "pending"
    result: Any = None
    error: Any = None


class StrictMessageStateStore:
    def __init__(self) -> None:
        self._outgoing: dict[int, asyncio.Future[Any]] = {}

    def register_outgoing(self, request_id: int, method: str) -> asyncio.Future[Any]:
        if request_id in self._outgoing:
            raise AcpProtocolError("Duplicate outgoing request ID")
        future = asyncio.get_running_loop().create_future()
        self._outgoing[request_id] = future
        return future

    def resolve_outgoing(self, request_id: int, result: Any) -> None:
        future = self._outgoing.pop(request_id, None)
        if future is None or future.done():
            raise AcpProtocolError("Duplicate or late response")
        future.set_result(result)

    def reject_outgoing(self, request_id: int, error: Any) -> None:
        future = self._outgoing.pop(request_id, None)
        if future is None or future.done():
            raise AcpProtocolError("Duplicate or late response")
        future.set_exception(error)

    def reject_all_outgoing(self, error: Any) -> None:
        for future in self._outgoing.values():
            if not future.done():
                future.set_exception(error)
        self._outgoing.clear()

    def begin_incoming(self, method: str, params: Any) -> IncomingRequestState:
        return IncomingRequestState(method, params)

    def complete_incoming(self, record: IncomingRequestState, result: Any) -> None:
        record.status = "completed"
        record.result = result

    def fail_incoming(self, record: IncomingRequestState, error: Any) -> None:
        record.status = "failed"
        record.error = error


class StrictNdjsonTransport:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer

    async def receive(self) -> dict[str, Any] | None:
        line = await self._reader.readline()
        if not line:
            return None
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcpProtocolError("Malformed JSON-RPC frame") from exc
        validate_jsonrpc_envelope(message)
        return message

    async def send(self, message: dict[str, Any]) -> None:
        validate_jsonrpc_envelope(message)
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        self._writer.write((payload + "\n").encode("utf-8"))
        await self._writer.drain()

    async def close(self) -> None:
        return None

def validate_jsonrpc_envelope(message: Any) -> None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise AcpProtocolError("Malformed JSON-RPC envelope")
    if "method" in message:
        allowed = {"jsonrpc", "id", "method", "params"}
        if set(message) - allowed or not isinstance(message["method"], str):
            raise AcpProtocolError("Malformed JSON-RPC request")
        if "id" in message and not _valid_outer_id(message["id"]):
            raise AcpProtocolError("Malformed JSON-RPC request ID")
        params = message.get("params")
        if "params" in message and params is not None and not isinstance(params, dict):
            raise AcpProtocolError("Malformed JSON-RPC params")
        return
    allowed = {"jsonrpc", "id", "result", "error"}
    if set(message) - allowed or not _valid_outer_id(message.get("id")):
        raise AcpProtocolError("Malformed JSON-RPC response")
    if ("result" in message) == ("error" in message):
        raise AcpProtocolError("Malformed JSON-RPC response")
    if "error" in message:
        error = message["error"]
        if not isinstance(error, dict) or set(error) - {"code", "message", "data"}:
            raise AcpProtocolError("Malformed JSON-RPC error")
        code = error.get("code")
        valid_code = isinstance(code, int) and not isinstance(code, bool)
        if not valid_code or not isinstance(error.get("message"), str):
            raise AcpProtocolError("Malformed JSON-RPC error")


def _valid_outer_id(value: Any) -> bool:
    return (isinstance(value, (str, int)) and not isinstance(value, bool)) or value is None


class StrictSchemaModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", strict=True)


class MetaSchemaModel(StrictSchemaModel):
    field_meta: dict[str, Any] | None = Field(default=None, alias="_meta")

    @field_validator("field_meta", mode="before")
    @classmethod
    def validate_meta(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, dict):
            raise ValueError("_meta must be an object or null")
        return value


class ConnectMcpRequest(MetaSchemaModel):
    server_id: str = Field(alias="serverId")


class ConnectMcpResponse(MetaSchemaModel):
    connection_id: str = Field(alias="connectionId")


class MessageMcpRequest(MetaSchemaModel):
    connection_id: str = Field(alias="connectionId")
    method: str
    params: dict[str, Any] | None = None


class MessageMcpNotification(MessageMcpRequest):
    pass


class DisconnectMcpRequest(MetaSchemaModel):
    connection_id: str = Field(alias="connectionId")


class DisconnectMcpResponse(MetaSchemaModel):
    pass


class SelectedPermissionOutcome(MetaSchemaModel):
    outcome: Literal["selected"]
    option_id: str = Field(alias="optionId")


class CancelledPermissionOutcome(StrictSchemaModel):
    outcome: Literal["cancelled"]


class RequestPermissionResponse(MetaSchemaModel):
    outcome: SelectedPermissionOutcome | CancelledPermissionOutcome = Field(
        discriminator="outcome"
    )


PermissionDecision = Literal["allow_once", "reject_once", "cancelled"]


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    tool_call_id: str
    title: str
    kind: str
    raw_input: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PermissionCompletion:
    decision: PermissionDecision
    error: BaseException | None = None

    @property
    def executable(self) -> bool:
        return self.decision == "allow_once" and self.error is None


@runtime_checkable
class AcpPeerCallbacks(Protocol):
    async def on_mcp_notification(
        self, connection_id: str, method: str, params: dict[str, Any] | None
    ) -> None: ...

    async def on_transport_closed(self, peer_generation: int) -> None: ...


def auth_required_error() -> RequestError:
    return RequestError.auth_required({"methodId": AUTH_METHOD_ID})


def method_not_found_error(method: str) -> RequestError:
    return RequestError.method_not_found(method)


def invalid_params_error() -> RequestError:
    return RequestError.invalid_params()


def unknown_connection_error() -> RequestError:
    return RequestError(-32602, "Unknown MCP connection")


def internal_error() -> RequestError:
    return RequestError.internal_error()


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(
        mode="json", by_alias=True, exclude_none=True, exclude_unset=True
    )


def permission_request_params(
    session_id: str, snapshot: PermissionSnapshot
) -> dict[str, Any]:
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(snapshot.tool_call_id, str)
        or not snapshot.tool_call_id
        or not isinstance(snapshot.title, str)
        or not isinstance(snapshot.kind, str)
        or not isinstance(snapshot.raw_input, Mapping)
    ):
        raise AcpProtocolError("Malformed permission lifecycle snapshot")
    return {
        "sessionId": session_id,
        "toolCall": {
            "toolCallId": snapshot.tool_call_id,
            "title": snapshot.title,
            "kind": snapshot.kind,
            "status": "pending",
            "rawInput": dict(snapshot.raw_input),
        },
        "options": [
            {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
        ],
    }


def validate_acp_mcp_server(value: Any) -> AcpMcpServer:
    if not isinstance(value, dict) or set(value) - {"type", "name", "serverId", "_meta"}:
        raise AcpProtocolError("Malformed ACP MCP server declaration")
    if value.get("type") != "acp":
        raise AcpProtocolError("Malformed ACP MCP server declaration")
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise AcpProtocolError("Malformed ACP MCP server declaration")
    if not isinstance(value.get("serverId"), str) or not value["serverId"]:
        raise AcpProtocolError("Malformed ACP MCP server declaration")
    if "_meta" in value and value["_meta"] is not None and not isinstance(value["_meta"], dict):
        raise AcpProtocolError("Malformed ACP MCP server declaration")
    try:
        return AcpMcpServer.model_validate(value)
    except ValidationError as exc:
        raise AcpProtocolError("Malformed ACP MCP server declaration") from exc


def validate_permission_response(value: Any) -> PermissionDecision:
    try:
        response = RequestPermissionResponse.model_validate(value)
    except ValidationError as exc:
        raise AcpProtocolError("Malformed permission response") from exc
    outcome = response.outcome
    if isinstance(outcome, CancelledPermissionOutcome):
        return "cancelled"
    if outcome.option_id not in {"allow_once", "reject_once"}:
        raise AcpProtocolError("Unknown permission option")
    return outcome.option_id


class AcpPeer:
    def __init__(self, connection: Connection, agent: Agent) -> None:
        self._connection = connection
        self._agent = agent
        self._active_connections: set[str] = set()
        self._used_connections: set[str] = set()
        self.peer_generation = 0

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        model = SessionNotification(
            session_id=session_id, update=update, field_meta=kwargs or None
        )
        await self._connection.send_notification(CLIENT_METHODS["session_update"], _dump(model))

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        payload = {
            "sessionId": session_id,
            "toolCall": tool_call.model_dump(
                mode="json", by_alias=True, exclude_none=True, exclude_unset=True
            ),
            "options": [
                option.model_dump(
                    mode="json", by_alias=True, exclude_none=True, exclude_unset=True
                )
                for option in options
            ],
        }
        if kwargs:
            payload["_meta"] = kwargs
        if payload != permission_request_params(
            session_id,
            PermissionSnapshot(
                tool_call_id=tool_call.tool_call_id,
                title=tool_call.title,
                kind=tool_call.kind,
                raw_input=tool_call.raw_input,
            ),
        ):
            raise AcpProtocolError("Permission request is not canonical")
        result = await self._connection.send_request(PERMISSION_METHOD, payload)
        try:
            return RequestPermissionResponse.model_validate(result)
        except ValidationError as exc:
            raise AcpProtocolError("Malformed permission response") from exc

    async def request_tool_permission(
        self, session_id: str, snapshot: PermissionSnapshot
    ) -> PermissionCompletion:
        try:
            result = await self._connection.send_request(
                PERMISSION_METHOD, permission_request_params(session_id, snapshot)
            )
            return PermissionCompletion(validate_permission_response(result))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return PermissionCompletion("reject_once", exc)

    async def connect_mcp(self, server_id: str) -> str:
        request = ConnectMcpRequest(serverId=server_id)
        result = await self._connection.send_request(MCP_CONNECT_METHOD, _dump(request))
        try:
            response = ConnectMcpResponse.model_validate(result)
        except ValidationError as exc:
            raise AcpProtocolError("Malformed MCP connect response") from exc
        connection_id = response.connection_id
        if not connection_id or connection_id in self._used_connections:
            raise AcpProtocolError("MCP connection ID is empty or reused")
        self._used_connections.add(connection_id)
        self._active_connections.add(connection_id)
        return connection_id

    async def message_mcp(
        self,
        connection_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if connection_id not in self._active_connections:
            raise unknown_connection_error()
        if method not in MCP_REQUEST_METHODS:
            raise method_not_found_error(method)
        request = MessageMcpRequest(
            connectionId=connection_id, method=method, params=params
        )
        return await self._connection.send_request(MCP_MESSAGE_METHOD, _dump(request))

    async def notify_mcp(
        self,
        connection_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        if connection_id not in self._active_connections:
            raise unknown_connection_error()
        if method not in MCP_NOTIFICATION_METHODS:
            raise method_not_found_error(method)
        notification = MessageMcpNotification(
            connectionId=connection_id, method=method, params=params
        )
        await self._connection.send_notification(MCP_MESSAGE_METHOD, _dump(notification))

    async def disconnect_mcp(self, connection_id: str) -> None:
        if connection_id not in self._active_connections:
            raise unknown_connection_error()
        self._active_connections.remove(connection_id)
        request = DisconnectMcpRequest(connectionId=connection_id)
        result = await self._connection.send_request(MCP_DISCONNECT_METHOD, _dump(request))
        try:
            DisconnectMcpResponse.model_validate(result)
        except ValidationError as exc:
            raise AcpProtocolError("Malformed MCP disconnect response") from exc

    async def route_mcp(
        self, params: Any, is_notification: bool
    ) -> Any:
        model_type = MessageMcpNotification if is_notification else MessageMcpRequest
        try:
            message = model_type.model_validate(params)
        except ValidationError as exc:
            raise invalid_params_error() from exc
        if message.connection_id not in self._active_connections:
            if is_notification:
                return None
            raise unknown_connection_error()
        if not is_notification:
            if message.method == "ping":
                return {}
            raise method_not_found_error(message.method)
        if message.method not in MCP_INBOUND_NOTIFICATIONS:
            return None
        callback = getattr(self._agent, "on_mcp_notification", None)
        if callback is not None:
            await callback(message.connection_id, message.method, message.params)
        return None


async def run_stdio_agent(
    agent: Agent,
    *,
    request_reader: asyncio.StreamReader,
    response_writer: asyncio.StreamWriter,
) -> None:
    holder: dict[str, AcpPeer] = {}
    base_router = build_agent_router(agent, use_unstable_protocol=False)

    async def route(method: str, params: Any, is_notification: bool) -> Any:
        if method == MCP_MESSAGE_METHOD:
            return await holder["peer"].route_mcp(params, is_notification)
        if method in {MCP_CONNECT_METHOD, MCP_DISCONNECT_METHOD}:
            if is_notification:
                return None
            raise method_not_found_error(method)
        return await base_router(method, params, is_notification)

    transport = StrictNdjsonTransport(request_reader, response_writer)
    connection = Connection(
        route,
        transport,
        listening=False,
        state_store=StrictMessageStateStore(),
    )
    peer = AcpPeer(connection, agent)
    holder["peer"] = peer
    on_connect = getattr(agent, "on_connect", None)
    if on_connect is not None:
        generation = on_connect(peer)
        if isinstance(generation, int) and not isinstance(generation, bool):
            peer.peer_generation = generation
    try:
        await connection.main_loop()
    finally:
        on_closed = getattr(agent, "on_transport_closed", None)
        if on_closed is not None:
            await on_closed(peer.peer_generation)
        await asyncio.shield(connection.close())


__all__ = [
    "AGENT_METHODS",
    "AUTH_METHOD_ID",
    "CLIENT_METHODS",
    "MCP_CONNECT_METHOD",
    "MCP_DISCONNECT_METHOD",
    "MCP_INBOUND_NOTIFICATIONS",
    "MCP_MESSAGE_METHOD",
    "MCP_NOTIFICATION_METHODS",
    "MCP_REQUEST_METHODS",
    "PERMISSION_METHOD",
    "AcpMcpServer",
    "AcpPeer",
    "AcpPeerCallbacks",
    "AcpProtocolError",
    "Agent",
    "AgentCapabilities",
    "AgentMessageChunk",
    "AgentPlanUpdate",
    "AudioContentBlock",
    "AuthenticateRequest",
    "AuthenticateResponse",
    "AuthMethodAgent",
    "CancelNotification",
    "CancelledPermissionOutcome",
    "Client",
    "ClientCapabilities",
    "ConnectMcpRequest",
    "ConnectMcpResponse",
    "DisconnectMcpRequest",
    "DisconnectMcpResponse",
    "EmbeddedResourceContentBlock",
    "ImageContentBlock",
    "Implementation",
    "InitializeRequest",
    "InitializeResponse",
    "LoadSessionRequest",
    "LoadSessionResponse",
    "McpCapabilities",
    "MessageMcpNotification",
    "MessageMcpRequest",
    "NewSessionRequest",
    "NewSessionResponse",
    "PROTOCOL_VERSION",
    "PermissionCompletion",
    "PermissionDecision",
    "PermissionOption",
    "PermissionSnapshot",
    "PlanEntry",
    "PromptCapabilities",
    "PromptRequest",
    "PromptResponse",
    "RequestError",
    "RequestPermissionResponse",
    "ResourceContentBlock",
    "SelectedPermissionOutcome",
    "SessionNotification",
    "StrictMessageStateStore",
    "StrictNdjsonTransport",
    "TextContentBlock",
    "ToolCallProgress",
    "ToolCallStart",
    "ToolCallUpdate",
    "UserMessageChunk",
    "auth_required_error",
    "internal_error",
    "invalid_params_error",
    "method_not_found_error",
    "permission_request_params",
    "run_stdio_agent",
    "unknown_connection_error",
    "validate_acp_mcp_server",
    "validate_jsonrpc_envelope",
    "validate_permission_response",
]
