from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from acp import PROTOCOL_VERSION, RequestError
from acp.agent.router import build_agent_router
from acp.connection import Connection
from acp.interfaces import Agent, Client
from acp.meta import AGENT_METHODS, CLIENT_METHODS
from acp.task import DefaultMessageDispatcher
from acp.task.queue import InMemoryMessageQueue
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
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_PENDING_REQUESTS = 64
INPUT_QUEUE_MAX_ITEMS = 64
INPUT_QUEUE_MAX_BYTES = 8 * 1024 * 1024
INPUT_QUEUE_DRAIN_TIMEOUT = 2.0
OUTPUT_DRAIN_TIMEOUT = 30.0
DISPATCHER_STOP_TIMEOUT = 30.0
DISPATCHER_CANCEL_TIMEOUT = 2.0
MAX_ACTIVE_INBOUND_RUNNERS = 64
MAX_CONSECUTIVE_PARSE_ERRORS = 3


class AcpProtocolError(RuntimeError):
    pass


@dataclass(slots=True)
class IncomingRequestState:
    method: str
    params: Any
    status: str = "pending"
    result: Any = None
    error: Any = None


@dataclass(slots=True)
class _StartRegistration:
    method: str
    future: asyncio.Future[tuple[int, asyncio.Future[Any]]]
    abandoned: bool = False


class StrictMessageStateStore:
    def __init__(self) -> None:
        self._outgoing: dict[int, asyncio.Future[Any]] = {}
        self._registration: ContextVar[_StartRegistration | None] = ContextVar(
            "acp_start_registration", default=None
        )
        self._abandoned: set[int] = set()

    def register_outgoing(self, request_id: int, method: str) -> asyncio.Future[Any]:
        if request_id in self._outgoing:
            raise AcpProtocolError("Duplicate outgoing request ID")
        if len(self._outgoing) + len(self._abandoned) >= MAX_PENDING_REQUESTS:
            raise AcpProtocolError("Too many pending requests")
        future = asyncio.get_running_loop().create_future()
        self._outgoing[request_id] = future
        registration = self._registration.get()
        if (
            registration is not None
            and not registration.abandoned
            and registration.method == method
            and not registration.future.done()
        ):
            registration.future.set_result((request_id, future))
        return future

    def prepare_start(self, method: str) -> tuple[_StartRegistration, Any]:
        registration = _StartRegistration(
            method, asyncio.get_running_loop().create_future()
        )
        return registration, self._registration.set(registration)

    async def next_started(
        self, registration: _StartRegistration
    ) -> tuple[int, asyncio.Future[Any]]:
        return await registration.future

    def cancel_start(self, registration: _StartRegistration, token: Any) -> None:
        registration.abandoned = True
        self._registration.reset(token)
        if not registration.future.done():
            registration.future.cancel()

    def abandon_outgoing(self, request_id: int) -> None:
        future = self._outgoing.pop(request_id, None)
        if future is None:
            return
        self._abandoned.add(request_id)
        if not future.done():
            future.cancel()

    def resolve_outgoing(self, request_id: int, result: Any) -> None:
        future = self._outgoing.pop(request_id, None)
        if future is None and request_id in self._abandoned:
            self._abandoned.remove(request_id)
            return
        if future is None or future.done():
            raise AcpProtocolError("Duplicate or late response")
        future.set_result(result)

    def reject_outgoing(self, request_id: int, error: Any) -> None:
        future = self._outgoing.pop(request_id, None)
        if future is None and request_id in self._abandoned:
            self._abandoned.remove(request_id)
            return
        if future is None or future.done():
            raise AcpProtocolError("Duplicate or late response")
        future.set_exception(error)

    def reject_all_outgoing(self, error: Any) -> None:
        for future in self._outgoing.values():
            if not future.done():
                future.set_exception(error)
        self._outgoing.clear()
        self._abandoned.clear()

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
        self._buffer = bytearray()
        self._frame_bytes: int | None = None
        self._consecutive_parse_errors = 0

    async def receive(self) -> dict[str, Any] | None:
        while True:
            frame = await self._read_frame()
            if frame is None:
                return None
            self._frame_bytes = len(frame)
            try:
                message = json.loads(frame)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._consecutive_parse_errors += 1
                await self.send({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                })
                if self._consecutive_parse_errors >= MAX_CONSECUTIVE_PARSE_ERRORS:
                    raise AcpProtocolError("Too many malformed JSON-RPC frames") from exc
                continue
            self._consecutive_parse_errors = 0
            validate_jsonrpc_envelope(message)
            return message

    async def _read_frame(self) -> bytes | None:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_FRAME_BYTES:
                    raise AcpProtocolError("JSON-RPC frame exceeds size limit")
                frame = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                return frame
            if len(self._buffer) > MAX_FRAME_BYTES:
                raise AcpProtocolError("JSON-RPC frame exceeds size limit")
            remaining = MAX_FRAME_BYTES + 1 - len(self._buffer)
            chunk = await self._reader.read(min(64 * 1024, remaining))
            if not chunk:
                if not self._buffer:
                    return None
                frame = bytes(self._buffer)
                self._buffer.clear()
                return frame
            self._buffer.extend(chunk)

    def take_frame_bytes(self) -> int | None:
        frame_bytes = self._frame_bytes
        self._frame_bytes = None
        return frame_bytes

    async def send(self, message: dict[str, Any]) -> None:
        validate_jsonrpc_envelope(message)
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        self._writer.write((payload + "\n").encode("utf-8"))
        await asyncio.wait_for(self._writer.drain(), OUTPUT_DRAIN_TIMEOUT)

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


PermissionDecision = Literal["allow_once", "allow_session", "reject_once", "cancelled"]


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    tool_call_id: str
    title: str
    kind: str
    raw_input: Mapping[str, Any]
    wrapper_name: str | None = None
    tainted: bool = False


@dataclass(frozen=True, slots=True)
class PermissionCompletion:
    decision: PermissionDecision
    error: BaseException | None = None

    @property
    def executable(self) -> bool:
        return self.decision in {"allow_once", "allow_session"} and self.error is None


@runtime_checkable
class AcpPeerCallbacks(Protocol):
    async def on_mcp_notification(
        self, peer_generation: int, connection_id: str, method: str,
        params: dict[str, Any] | None,
    ) -> None: ...

    async def on_transport_closed(self, peer_generation: int) -> None: ...


def auth_required_error() -> RequestError:
    return RequestError.auth_required({"methodId": AUTH_METHOD_ID})


def connection_busy_error() -> RequestError:
    return RequestError(
        -32001,
        "An ACP client is already connected; close it before opening another editor",
    )


def connection_replaced_error() -> RequestError:
    return RequestError(
        -32002,
        "This ACP connection was replaced by another client; reconnect to continue",
    )


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


def _copy_snapshot_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_snapshot_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_snapshot_json(item) for item in value]
    return value


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
        or not isinstance(snapshot.tainted, bool)
    ):
        raise AcpProtocolError("Malformed permission lifecycle snapshot")
    result = {
        "sessionId": session_id,
        "toolCall": {
            "toolCallId": snapshot.tool_call_id,
            "title": snapshot.title,
            "kind": snapshot.kind,
            "status": "pending",
            "rawInput": _copy_snapshot_json(snapshot.raw_input),
        },
        "options": [
            {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "allow_session", "name": "Allow for this session", "kind": "allow_always"},
            {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
        ],
    }
    if snapshot.wrapper_name is not None:
        if snapshot.wrapper_name not in {"hands_edit", "hands_shell"}:
            raise AcpProtocolError("Malformed permission lifecycle snapshot")
        result["_meta"] = {"mimir.wrapper": snapshot.wrapper_name}
        if snapshot.tainted:
            result["_meta"]["mimir.tainted"] = True
    elif snapshot.tainted:
        raise AcpProtocolError("Malformed permission lifecycle snapshot")
    return result


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


def _observe_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def validate_permission_response(value: Any) -> PermissionDecision:
    try:
        response = RequestPermissionResponse.model_validate(value)
    except ValidationError as exc:
        raise AcpProtocolError("Malformed permission response") from exc
    outcome = response.outcome
    if isinstance(outcome, CancelledPermissionOutcome):
        return "cancelled"
    if outcome.option_id not in {"allow_once", "allow_session", "reject_once"}:
        raise AcpProtocolError("Unknown permission option")
    return outcome.option_id


@dataclass(slots=True)
class AcpRequestHandle:
    outer_id: int
    task: asyncio.Task[Any]
    _store: StrictMessageStateStore
    _owned_tasks: tuple[asyncio.Task[Any], ...] = ()
    _abandoned: bool = False

    def abandon(self) -> None:
        if self._abandoned:
            return
        self._abandoned = True
        self._store.abandon_outgoing(self.outer_id)
        self.task.cancel()
        for task in self._owned_tasks:
            task.cancel()
            task.add_done_callback(_observe_task_exception)

    cancel = abandon


class AcpPeer:
    def __init__(
        self,
        connection: Connection,
        agent: Agent,
        state_store: StrictMessageStateStore | None = None,
    ) -> None:
        self._connection = connection
        self._state_store = state_store
        self._start_lock = asyncio.Lock()
        self._agent = agent
        self._active_connections: set[str] = set()
        self._used_connections: set[str] = set()
        self.peer_generation = 0
        self._transport_dead = asyncio.Event()

    def mark_transport_dead(self) -> None:
        if self._transport_dead.is_set():
            return
        self._transport_dead.set()
        self._active_connections.clear()
        if self._state_store is not None:
            self._state_store.reject_all_outgoing(ConnectionError("Connection closed"))

    async def wait_transport_dead(self) -> None:
        await self._transport_dead.wait()

    def _require_live(self) -> None:
        if self._transport_dead.is_set():
            raise ConnectionError("Connection closed")

    @property
    def closed(self) -> bool:
        return self._transport_dead.is_set()

    @property
    def supports_owned_requests(self) -> bool:
        return self._state_store is not None

    async def start_request(self, method: str, params: dict[str, Any]) -> AcpRequestHandle:
        self._require_live()
        if self._state_store is None:
            raise AcpProtocolError("Cancellable requests require the Mimir state store")
        registration, token = self._state_store.prepare_start(method)
        task = asyncio.create_task(self._connection.send_request(method, params))
        started = asyncio.create_task(self._state_store.next_started(registration))
        outer_id: int | None = None
        try:
            done, _ = await asyncio.wait(
                {task, started}, return_when=asyncio.FIRST_COMPLETED
            )
            if started in done:
                outer_id, _ = started.result()
            else:
                task.result()
                raise AcpProtocolError("Outgoing request completed before registration")
        except BaseException:
            if outer_id is None and registration.future.done() and not registration.future.cancelled():
                outer_id, _ = registration.future.result()
            if outer_id is not None:
                self._state_store.abandon_outgoing(outer_id)
            task.cancel()
            started.cancel()
            await asyncio.gather(task, started, return_exceptions=True)
            raise
        finally:
            self._state_store.cancel_start(registration, token)
        return AcpRequestHandle(outer_id, task, self._state_store)

    async def start_tool_permission(
        self, session_id: str, snapshot: PermissionSnapshot
    ) -> AcpRequestHandle:
        handle = await self.start_request(
            PERMISSION_METHOD, permission_request_params(session_id, snapshot)
        )

        async def completion() -> PermissionCompletion:
            try:
                return PermissionCompletion(validate_permission_response(await handle.task))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return PermissionCompletion("reject_once", exc)

        return AcpRequestHandle(
            handle.outer_id,
            asyncio.create_task(completion()),
            handle._store,
            _owned_tasks=(handle.task,),
        )

    async def start_mcp_request(
        self,
        connection_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> AcpRequestHandle:
        if connection_id not in self._active_connections:
            raise unknown_connection_error()
        if method not in MCP_REQUEST_METHODS:
            raise method_not_found_error(method)
        request = MessageMcpRequest(
            connectionId=connection_id, method=method, params=params
        )
        return await self.start_request(MCP_MESSAGE_METHOD, _dump(request))

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self._require_live()
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
        self._require_live()
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
            if self._state_store is not None:
                handle = await self.start_tool_permission(session_id, snapshot)
                try:
                    return await handle.task
                except asyncio.CancelledError:
                    handle.abandon()
                    raise
            result = await self._connection.send_request(
                PERMISSION_METHOD, permission_request_params(session_id, snapshot)
            )
            return PermissionCompletion(validate_permission_response(result))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return PermissionCompletion("reject_once", exc)

    async def connect_mcp(self, server_id: str) -> str:
        self._require_live()
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
        self._require_live()
        if connection_id not in self._active_connections:
            raise unknown_connection_error()
        if method not in MCP_REQUEST_METHODS:
            raise method_not_found_error(method)
        if self._state_store is not None:
            handle = await self.start_mcp_request(connection_id, method, params)
            try:
                return await handle.task
            except asyncio.CancelledError:
                handle.abandon()
                raise
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
        self._require_live()
        if connection_id not in self._active_connections:
            raise unknown_connection_error()
        if method not in MCP_NOTIFICATION_METHODS:
            raise method_not_found_error(method)
        notification = MessageMcpNotification(
            connectionId=connection_id, method=method, params=params
        )
        await self._connection.send_notification(MCP_MESSAGE_METHOD, _dump(notification))

    async def disconnect_mcp(self, connection_id: str) -> None:
        self._require_live()
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
            await callback(
                self.peer_generation, message.connection_id, message.method, message.params
            )
        return None


class BoundedMessageQueue(InMemoryMessageQueue):
    def __init__(self, frame_bytes: Any = None) -> None:
        super().__init__(maxsize=INPUT_QUEUE_MAX_ITEMS)
        self._frame_bytes = frame_bytes
        self._pending_bytes = 0
        self._sizes: deque[int] = deque()
        self._space_available = asyncio.Event()
        self._sentinel_enqueued = False
        # Reserve one item slot for the sentinel so close() never waits behind a
        # blocked publisher. Byte reservations are producer-serialized as well.
        self._publish_slots = asyncio.Semaphore(INPUT_QUEUE_MAX_ITEMS)
        self._publish_lock = asyncio.Lock()

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes

    async def publish(self, task: Any) -> None:
        size = self._message_bytes(task)
        if size > INPUT_QUEUE_MAX_BYTES:
            raise AcpProtocolError("Input queue byte limit exceeded")
        deadline = asyncio.get_running_loop().time() + INPUT_QUEUE_DRAIN_TIMEOUT
        acquired = False
        try:
            async with asyncio.timeout_at(deadline):
                await self._publish_slots.acquire()
                acquired = True
                async with self._publish_lock:
                    while self._pending_bytes + size > INPUT_QUEUE_MAX_BYTES:
                        if self._closed:
                            raise RuntimeError("message queue already closed")
                        self._space_available.clear()
                        await self._space_available.wait()
                    if self._closed:
                        raise RuntimeError("message queue already closed")
                    # Queue capacity and byte accounting commit together under the
                    # producer lock; put_nowait cannot be cancelled after commit.
                    self._queue.put_nowait(task)
                    self._pending_bytes += size
                    self._sizes.append(size)
                    acquired = False
        except (TimeoutError, asyncio.QueueFull):
            raise AcpProtocolError("Input queue drain timed out") from None
        finally:
            if acquired:
                self._publish_slots.release()

    async def close(self) -> None:
        self._closed = True
        self._space_available.set()
        async with self._publish_lock:
            if not self._sentinel_enqueued:
                await self._queue.put(None)
                self._sentinel_enqueued = True

    def close_nowait(self) -> None:
        self._closed = True
        self._space_available.set()

    def task_done(self) -> None:
        if self._sizes:
            self._pending_bytes -= self._sizes.popleft()
            self._publish_slots.release()
            self._space_available.set()
        super().task_done()

    def _message_bytes(self, task: Any) -> int:
        if self._frame_bytes is not None:
            size = self._frame_bytes()
            if size is not None:
                return size
        message = getattr(task, "message", None)
        if message is None:
            return 0
        return len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class BoundedMessageDispatcher(DefaultMessageDispatcher):
    def __init__(
        self,
        queue: BoundedMessageQueue,
        supervisor: Any,
        store: Any,
        request_runner: Any,
        notification_runner: Any,
        *,
        max_active: int = MAX_ACTIVE_INBOUND_RUNNERS,
    ) -> None:
        super().__init__(
            queue=queue,
            supervisor=supervisor,
            store=store,
            request_runner=request_runner,
            notification_runner=notification_runner,
        )
        if max_active <= 0:
            raise ValueError("max_active must be positive")
        self._runner_slots = asyncio.BoundedSemaphore(max_active)
        self._runner_tasks: set[asyncio.Task[Any]] = set()
        self._authentication_fence: asyncio.Task[Any] | None = None
        self._transport_dead = False

    def mark_transport_dead(self) -> None:
        self._transport_dead = True

    async def _dispatch_request(self, message: dict[str, Any]) -> None:
        method = message.get("method", "")
        fence: tuple[asyncio.Task[Any], ...] = ()
        if method == "authenticate":
            # Authentication mutates connection authority. Sequence it after
            # earlier handlers, and gate later work on it, without blocking the
            # ordered dispatcher loop that must still admit session/cancel.
            fence = tuple(self._runner_tasks)
        elif (
            method != "session/cancel"
            and self._authentication_fence is not None
            and not self._authentication_fence.done()
        ):
            fence = (self._authentication_fence,)
        if fence:
            task = self._create_deferred_request(message, fence)
            if method == "authenticate":
                self._authentication_fence = task
            return
        await self._runner_slots.acquire()
        try:
            record = self._store.begin_incoming(method, message.get("params"))
        except BaseException:
            self._runner_slots.release()
            raise

        async def runner() -> None:
            try:
                result = await self._request_runner(message)
            except Exception as exc:
                self._store.fail_incoming(record, exc)
                raise
            else:
                self._store.complete_incoming(record, result)
            finally:
                self._runner_slots.release()

        task = self._create_runner(runner(), "mimir.acp.Dispatcher.request")
        if method == "authenticate":
            self._authentication_fence = task

    async def _dispatch_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method", "")
        if (
            method != "session/cancel"
            and self._authentication_fence is not None
            and not self._authentication_fence.done()
        ):
            self._create_deferred_notification(message, (self._authentication_fence,))
            return
        await self._runner_slots.acquire()

        async def runner() -> None:
            try:
                await self._notification_runner(message)
            finally:
                self._runner_slots.release()

        self._create_runner(runner(), "mimir.acp.Dispatcher.notification")

    def _create_deferred_request(
        self,
        message: dict[str, Any],
        fence: tuple[asyncio.Task[Any], ...],
    ) -> asyncio.Task[Any]:
        async def deferred() -> None:
            await asyncio.wait(fence)
            await self._runner_slots.acquire()
            try:
                record = self._store.begin_incoming(
                    message.get("method", ""), message.get("params")
                )
            except BaseException:
                self._runner_slots.release()
                raise
            try:
                result = await self._request_runner(message)
            except Exception as exc:
                self._store.fail_incoming(record, exc)
                raise
            else:
                self._store.complete_incoming(record, result)
            finally:
                self._runner_slots.release()

        return self._create_waiter(deferred(), "mimir.acp.Dispatcher.request_waiter")

    def _create_deferred_notification(
        self,
        message: dict[str, Any],
        fence: tuple[asyncio.Task[Any], ...],
    ) -> asyncio.Task[Any]:
        async def deferred() -> None:
            await asyncio.wait(fence)
            await self._runner_slots.acquire()
            try:
                await self._notification_runner(message)
            finally:
                self._runner_slots.release()

        return self._create_waiter(
            deferred(), "mimir.acp.Dispatcher.notification_waiter"
        )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            self._queue.close_nowait()
            return
        shutdown = asyncio.create_task(self._shutdown(task))
        cancelled = False
        while not shutdown.done():
            try:
                await asyncio.shield(shutdown)
            except asyncio.CancelledError:
                # Repeated caller cancellation must not cancel cleanup itself.
                cancelled = True
                continue
        try:
            shutdown.result()
        finally:
            self._task = None
        if cancelled:
            raise asyncio.CancelledError

    async def _shutdown(self, task: asyncio.Task[Any]) -> None:
        if not self._transport_dead:
            drain = asyncio.create_task(self._drain(task))
            done, _ = await asyncio.wait((drain,), timeout=DISPATCHER_STOP_TIMEOUT)
            if done:
                drain.result()
                return
            drain.cancel()
            drain.add_done_callback(_observe_task_exception)
        else:
            self._queue.close_nowait()
        task.cancel()
        runners = tuple(self._runner_tasks)
        for runner in runners:
            runner.cancel()
        done, pending = await asyncio.wait(
            (task, *runners), timeout=DISPATCHER_CANCEL_TIMEOUT
        )
        for completed in done:
            if not completed.cancelled():
                completed.exception()
        self._runner_tasks.difference_update(done)
        # A cancellation-resistant handler cannot be awaited again by the ACP
        # supervisor without restoring the same unbounded shutdown. Detach only
        # after both bounded grace periods; the connection is already retiring.
        for pending_task in pending:
            pending_task.add_done_callback(_observe_task_exception)
        supervisor_tasks = getattr(self._supervisor, "_tasks", None)
        if isinstance(supervisor_tasks, set):
            supervisor_tasks.difference_update(pending)
        self._runner_tasks.difference_update(pending)

    async def _drain(self, task: asyncio.Task[Any]) -> None:
        await self._queue.close()
        await task
        while self._runner_tasks:
            runners = tuple(self._runner_tasks)
            # Wait for completion without retrieving runner exceptions here. The
            # ACP supervisor owns exception observation/reporting; gathering these
            # tasks during close would make expected JSON-RPC errors escape from
            # connection.close() on Python 3.12.
            await asyncio.wait(runners)
            self._runner_tasks.difference_update(runners)

    def _create_runner(self, coroutine: Any, name: str) -> asyncio.Task[Any]:
        try:
            task = self._supervisor.create(coroutine, name=name)
        except BaseException:
            coroutine.close()
            self._runner_slots.release()
            raise
        self._runner_tasks.add(task)
        task.add_done_callback(self._runner_tasks.discard)
        return task

    def _create_waiter(self, coroutine: Any, name: str) -> asyncio.Task[Any]:
        try:
            task = self._supervisor.create(coroutine, name=name)
        except BaseException:
            coroutine.close()
            raise
        self._runner_tasks.add(task)
        task.add_done_callback(self._runner_tasks.discard)
        return task


#: How long a cancelled ``run_stdio_agent`` will wait for the shielded
#: ``connection.close()`` before cancelling it and re-raising.
#:
#: The close is shielded so a cancel does not abandon a half-written frame, but
#: the wait must still be bounded: ``AcpDaemon._finish_runner`` allows
#: ``ACP_PEER_CANCEL_TIMEOUT`` before it escalates to ``transport.abort()`` and
#: then gives up, and the daemon releases its single admission slot when the
#: runner task completes. An unbounded wait here therefore lets a close that
#: resists cancellation keep a retired generation's dispatcher alive past the
#: point where the daemon has stopped waiting for it and a replacement peer can
#: be admitted -- two generations against one agent, which ``ACP_MAX_PEERS = 1``
#: exists to prevent.
#:
#: Two intervals must stay strictly below the daemon's cancel timeout so the
#: runner resolves
#: on its own rather than being aborted; asserted by
#: ``test_close_bound_fits_inside_the_daemon_cancel_budget``.
ACP_CLOSE_CANCEL_TIMEOUT = 0.5


def _close_retired_cleanly(close_task: asyncio.Task[None]) -> bool:
    """Whether a shielded close left nothing of its generation running.

    Clean means the task finished and either returned or honoured a
    cancellation -- in both cases nothing of that generation is still
    executing. Still pending is not clean, and neither is finishing by raising:
    teardown reached an unknown state, and the exception would otherwise go
    unretrieved.

    The caller fences admission on anything not clean, so this decision has to
    be reached on EVERY exit path out of the close -- the shield raising, either
    grace interval expiring, the close failing inside a grace interval, or the
    runner being cancelled again. Deciding it in one place is what keeps a path
    from silently skipping it.
    """
    if not close_task.done():
        return False
    if close_task.cancelled():
        return True
    return close_task.exception() is None


async def run_stdio_agent(
    agent: Agent,
    *,
    request_reader: asyncio.StreamReader,
    response_writer: asyncio.StreamWriter,
    on_close_abandoned: Callable[[asyncio.Task[None]], None] | None = None,
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
    state_store = StrictMessageStateStore()
    message_queue = BoundedMessageQueue(transport.take_frame_bytes)
    dispatcher_holder: dict[str, BoundedMessageDispatcher] = {}

    def make_dispatcher(*args: Any, **kwargs: Any) -> BoundedMessageDispatcher:
        dispatcher = BoundedMessageDispatcher(*args, **kwargs)
        dispatcher_holder["dispatcher"] = dispatcher
        return dispatcher

    connection = Connection(
        route,
        transport,
        listening=False,
        state_store=state_store,
        queue=message_queue,
        dispatcher_factory=make_dispatcher,
    )
    peer = AcpPeer(connection, agent, state_store)
    holder["peer"] = peer
    primary: BaseException | None = None
    traceback = None
    try:
        on_connect = getattr(agent, "on_connect", None)
        if on_connect is not None:
            generation = on_connect(peer)
            if isinstance(generation, int) and not isinstance(generation, bool):
                peer.peer_generation = generation
        await connection.main_loop()
    except BaseException as exc:
        primary = exc
        traceback = exc.__traceback__
    # main_loop returning means receive reached EOF just as surely as an
    # exception means the transport failed. Retire either form of disconnect
    # before close drains handlers that can no longer deliver their output.
    peer.mark_transport_dead()
    dispatcher = dispatcher_holder.get("dispatcher")
    if dispatcher is not None:
        dispatcher.mark_transport_dead()
    try:
        on_closed = getattr(agent, "on_transport_closed", None)
        if on_closed is not None:
            await on_closed(peer.peer_generation)
    except BaseException as exc:
        if primary is None:
            primary = exc
            traceback = exc.__traceback__
        else:
            primary.add_note(f"on_transport_closed also failed: {exc!r}")
    close_failure: BaseException | None = None
    close_task: asyncio.Task[None] | None = None
    try:
        close_task = asyncio.create_task(connection.close())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            # asyncio.wait does not deliver the cancellation into close_task, so
            # a well-behaved close still finishes; one that resists is cancelled
            # rather than waited on forever.
            _, unfinished = await asyncio.wait(
                {close_task}, timeout=ACP_CLOSE_CANCEL_TIMEOUT,
            )
            if unfinished:
                close_task.cancel()
                # Bounded a second time on purpose. A close that also swallows
                # cancellation must not pin the runner either, so the worst case
                # is two intervals and the runner still resolves inside the
                # daemon's cancel window.
                await asyncio.wait(
                    {close_task}, timeout=ACP_CLOSE_CANCEL_TIMEOUT,
                )
            raise
    except BaseException as exc:
        close_failure = exc
    finally:
        # THE retirement decision, made once for every way this block can be
        # left: the shield raising, either grace interval expiring, the close
        # failing inside a grace interval, or the runner being cancelled again.
        # It lived inside the ``if unfinished`` branch before, so a close that
        # raised during the FIRST interval -- or one whose failure the shield
        # surfaced directly -- was never reported, and the runner went on to
        # free the daemon's single admission slot after a failed teardown.
        if close_task is not None and on_close_abandoned is not None:
            if not _close_retired_cleanly(close_task):
                on_close_abandoned(close_task)
    if close_failure is not None:
        if primary is None:
            primary = close_failure
            traceback = close_failure.__traceback__
        else:
            primary.add_note(f"connection.close also failed: {close_failure!r}")
    if primary is not None:
        raise primary.with_traceback(traceback)


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
    "MAX_FRAME_BYTES",
    "MAX_PENDING_REQUESTS",
    "INPUT_QUEUE_MAX_ITEMS",
    "INPUT_QUEUE_MAX_BYTES",
    "INPUT_QUEUE_DRAIN_TIMEOUT",
    "MAX_ACTIVE_INBOUND_RUNNERS",
    "PERMISSION_METHOD",
    "AcpMcpServer",
    "AcpPeer",
    "AcpPeerCallbacks",
    "AcpRequestHandle",
    "AcpProtocolError",
    "BoundedMessageDispatcher",
    "BoundedMessageQueue",
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
