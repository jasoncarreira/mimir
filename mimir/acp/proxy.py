from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .credentials import CredentialError, NativeCredentialStore
from .hosted import HostedHandsProvider, HostedMcpError
from .profiles import Profile, ProfileError, ProfileStore, selected_profile
from .transport import FORCE_CLOSE_TIMEOUT, PEER_EOF_GRACE_TIMEOUT, close_writer

CONNECT_TIMEOUT = 5.0
MAX_FRAME_BYTES = 1024 * 1024
MAX_OUTSTANDING_REQUESTS = 1024
MAX_GENERATION_SERVER_IDS = 1024
MAX_GENERATION_CONNECTION_IDS = 4096
MAX_LIVE_CONNECTIONS = 1024

PERMISSION_METHOD = "session/request_permission"
PERMISSION_OPTIONS = [
    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
    {
        "optionId": "allow_session",
        "name": "Allow for this session",
        "kind": "allow_always",
    },
    {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
]
HANDS_PERMISSION_ARGUMENTS = {
    "hands_edit": (frozenset({"path", "old_text", "new_text"}),),
    "hands_shell": (frozenset({"command"}),),
    "hands_python": (frozenset({"code"}),),
}

class ProxyError(RuntimeError):
    pass


class PermissionGrantStore:
    def __init__(self) -> None:
        self._grants: set[tuple[str, str]] = set()

    def add(self, session_id: str, wrapper_name: str) -> None:
        self._grants.add((session_id, wrapper_name))

    def allows(self, session_id: str, wrapper_name: str) -> bool:
        return (session_id, wrapper_name) in self._grants

    def revoke_session(self, session_id: str) -> None:
        self._grants = {
            grant for grant in self._grants if grant[0] != session_id
        }

    def clear(self) -> None:
        self._grants.clear()

    def __len__(self) -> int:
        return len(self._grants)

class FrameWriter:
    def __init__(self, writer: Any, credential: str, *, inject_credential: bool = True) -> None:
        self._writer = writer
        self._credential = credential
        self._inject_credential = inject_credential
        self._buffer = bytearray()

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            remaining = MAX_FRAME_BYTES - len(self._buffer)
            newline = view.tobytes().find(b"\n")
            if newline < 0:
                if len(view) > remaining:
                    raise ProxyError("invalid frame")
                self._buffer.extend(view)
                return
            if newline > remaining:
                raise ProxyError("invalid frame")
            self._buffer.extend(view[:newline])
            view = view[newline + 1:]
            self._write_frame(bytes(self._buffer))
            self._buffer.clear()

    def _write_frame(self, frame: bytes) -> None:
        try:
            message = json.loads(frame)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProxyError("invalid frame") from exc
        if not isinstance(message, dict):
            raise ProxyError("invalid frame")
        self.write_message(message)

    def write_message(self, message: dict[str, Any]) -> None:
        if self._inject_credential and message.get("method") == "authenticate":
            params = message.get("params")
            if not isinstance(params, dict):
                raise ProxyError("invalid frame")
            metadata = params.get("_meta", {})
            if not isinstance(metadata, dict):
                raise ProxyError("invalid frame")
            clean = {
                key: value for key, value in metadata.items()
                if key != "mimir" and not key.startswith("mimir.")
            }
            clean["mimir.webKey"] = self._credential
            params["_meta"] = clean
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > MAX_FRAME_BYTES:
            raise ProxyError("invalid frame")
        self._writer.write(encoded)

    def write_raw(self, frame: bytes) -> None:
        if len(frame) > MAX_FRAME_BYTES or not frame.endswith(b"\n"):
            raise ProxyError("invalid frame")
        self._writer.write(frame)

    async def drain(self) -> None: await self._writer.drain()
    def write_eof(self) -> None:
        if self._buffer: raise ProxyError("invalid frame")
        method = getattr(self._writer, "write_eof", None)
        if method is not None: method()
    def close(self) -> None: self._writer.close()
    def is_closing(self) -> bool: return self._writer.is_closing()
    async def wait_closed(self) -> None:
        method = getattr(self._writer, "wait_closed", None)
        if method is not None: await method()
    @property
    def transport(self) -> Any: return getattr(self._writer, "transport", None)

ReservedMetadataWriter = FrameWriter


def _request_key(value: Any) -> tuple[type[Any], Any]:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProxyError("invalid frame")
    return type(value), value


def _message_kind(message: dict[str, Any]) -> str:
    if message.get("jsonrpc") != "2.0":
        raise ProxyError("invalid frame")
    if "method" in message:
        if (
            not isinstance(message["method"], str)
            or "result" in message
            or "error" in message
        ):
            raise ProxyError("invalid frame")
        if "id" in message:
            _request_key(message["id"])
            return "request"
        return "notification"
    if "id" not in message or ("result" in message) == ("error" in message):
        raise ProxyError("invalid frame")
    _request_key(message["id"])
    return "response"


@dataclass(slots=True)
class _PendingSession:
    method: str
    cwd: str
    server_id: str | None
    session_id: str | None


@dataclass(frozen=True, slots=True)
class _PendingPermission:
    session_id: str
    wrapper_name: str
    generation: object


def _related_permission(message: dict[str, Any]) -> bool:
    if message.get("method") != PERMISSION_METHOD:
        return False
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    tool_call = params.get("toolCall")
    metadata = params.get("_meta")
    hands_identity = (
        isinstance(tool_call, dict)
        and tool_call.get("title") in HANDS_PERMISSION_ARGUMENTS
    )
    reserved_metadata = isinstance(metadata, dict) and any(
        key == "mimir" or key.startswith("mimir.") for key in metadata
    )
    return hands_identity or reserved_metadata


def _permission_candidate(
    message: dict[str, Any], kind: str
) -> tuple[str, str, bool] | None:
    if not _related_permission(message):
        return None
    if kind != "request" or set(message) != {"jsonrpc", "id", "method", "params"}:
        raise ProxyError("invalid reserved permission request")
    params = message["params"]
    if not isinstance(params, dict):
        raise ProxyError("invalid reserved permission request")
    tool_call = params.get("toolCall")
    metadata = params.get("_meta")
    if not isinstance(metadata, dict):
        raise ProxyError("invalid reserved permission metadata")
    reserved = {
        key for key in metadata if key == "mimir" or key.startswith("mimir.")
    }
    if not reserved.issubset({"mimir.wrapper", "mimir.tainted"}):
        raise ProxyError("invalid reserved permission metadata")
    wrapper_name = metadata.get("mimir.wrapper")
    if wrapper_name not in HANDS_PERMISSION_ARGUMENTS:
        raise ProxyError("invalid reserved permission metadata")
    if "mimir.tainted" in metadata and metadata["mimir.tainted"] is not True:
        raise ProxyError("invalid reserved permission metadata")
    if set(metadata) != reserved:
        raise ProxyError("invalid reserved permission metadata")
    if set(params) != {"sessionId", "toolCall", "options", "_meta"}:
        raise ProxyError("invalid reserved permission request")
    session_id = params.get("sessionId")
    if not isinstance(session_id, str) or not session_id or not isinstance(tool_call, dict):
        raise ProxyError("invalid reserved permission request")
    if params.get("options") != PERMISSION_OPTIONS:
        raise ProxyError("invalid reserved permission request")
    if set(tool_call) != {
        "toolCallId", "title", "kind", "status", "rawInput",
    }:
        raise ProxyError("invalid reserved permission request")
    raw_input = tool_call.get("rawInput")
    argument_keys = frozenset(raw_input) if isinstance(raw_input, dict) else None
    if (
        not isinstance(tool_call.get("toolCallId"), str)
        or not tool_call["toolCallId"]
        or tool_call.get("title") != wrapper_name
        or tool_call.get("kind") != "other"
        or tool_call.get("status") != "pending"
        or not isinstance(raw_input, dict)
        or argument_keys not in HANDS_PERMISSION_ARGUMENTS[wrapper_name]
        or any(not isinstance(value, str) for value in raw_input.values())
    ):
        raise ProxyError("invalid reserved permission request")
    return session_id, wrapper_name, "mimir.tainted" in metadata


def _permission_response_decision(message: dict[str, Any]) -> str | None:
    if set(message) == {"jsonrpc", "id", "error"}:
        error = message["error"]
        if (
            isinstance(error, dict)
            and {"code", "message"}.issubset(error)
            and set(error).issubset({"code", "message", "data"})
            and isinstance(error["code"], int)
            and not isinstance(error["code"], bool)
            and isinstance(error["message"], str)
        ):
            return None
        raise ProxyError("invalid reserved permission response")
    if set(message) != {"jsonrpc", "id", "result"}:
        raise ProxyError("invalid reserved permission response")
    result = message["result"]
    if not isinstance(result, dict) or not set(result).issubset({"outcome", "_meta"}):
        raise ProxyError("invalid reserved permission response")
    if "outcome" not in result:
        raise ProxyError("invalid reserved permission response")
    if "_meta" in result and result["_meta"] is not None and not isinstance(
        result["_meta"], dict
    ):
        raise ProxyError("invalid reserved permission response")
    outcome = result.get("outcome")
    if not isinstance(outcome, dict):
        raise ProxyError("invalid reserved permission response")
    if outcome.get("outcome") == "cancelled":
        if set(outcome) != {"outcome"}:
            raise ProxyError("invalid reserved permission response")
        return "cancelled"
    if set(outcome) - {"outcome", "optionId", "_meta"} or not {
        "outcome", "optionId"
    }.issubset(outcome):
        raise ProxyError("invalid reserved permission response")
    if "_meta" in outcome and outcome["_meta"] is not None and not isinstance(
        outcome["_meta"], dict
    ):
        raise ProxyError("invalid reserved permission response")
    if outcome.get("outcome") != "selected" or outcome.get("optionId") not in {
        "allow_once", "allow_session", "reject_once",
    }:
        raise ProxyError("invalid reserved permission response")
    return outcome["optionId"]


class ProxyRouter:
    def __init__(
        self,
        client_writer: Any,
        daemon_writer: Any,
        credential: str,
        timeout_seconds: int = 60,
    ) -> None:
        self._client = FrameWriter(client_writer, credential, inject_credential=False)
        self._daemon = FrameWriter(daemon_writer, credential)
        self._provider = HostedHandsProvider(timeout_seconds)
        self._generation = object()
        self._grants = PermissionGrantStore()
        self._active_sessions: set[str] = set()
        self._client_requests: dict[tuple[type[Any], Any], _PendingSession | None] = {}
        self._daemon_requests: dict[
            tuple[type[Any], Any], _PendingPermission | None
        ] = {}
        self._local_requests: dict[tuple[type[Any], Any], asyncio.Task[None] | None] = {}
        self._local_sessions: dict[tuple[type[Any], Any], str] = {}
        self._daemon_tombstones: set[tuple[type[Any], Any]] = set()
        self._server_sessions: dict[str, str] = {}
        self._server_provider_sessions: dict[str, str] = {}
        self._connection_sessions: dict[str, str] = {}
        self._connection_provider_sessions: dict[str, str] = {}
        self._used_server_ids: set[str] = set()
        self._used_connection_ids: set[str] = set()
        self._local_connections: dict[tuple[type[Any], Any], str] = {}
        self._client_lock = asyncio.Lock()
        self._daemon_lock = asyncio.Lock()
        self._failure: asyncio.Future[BaseException] = asyncio.get_running_loop().create_future()
        self._closed = False

    async def route_client(self, message: dict[str, Any], raw: bytes | None = None) -> None:
        self._require_open()
        kind = _message_kind(message)
        if kind == "response":
            key = _request_key(message["id"])
            if key in self._daemon_tombstones:
                return
            if key not in self._daemon_requests:
                raise ProxyError("unsolicited response")
            pending_permission = self._daemon_requests.pop(key)
            decision = None
            if pending_permission is not None:
                if (
                    pending_permission.generation is not self._generation
                    or pending_permission.session_id not in self._active_sessions
                ):
                    raise ProxyError("stale reserved permission response")
                decision = _permission_response_decision(message)
            await self._write_daemon(message, raw)
            if (
                pending_permission is not None
                and decision == "allow_session"
            ):
                self._grants.add(
                    pending_permission.session_id, pending_permission.wrapper_name
                )
            return
        if kind == "notification":
            await self._client_notification(message)
            await self._write_daemon(message, raw)
            return
        key = _request_key(message["id"])
        self._register(self._client_requests, key)
        pending, transformed = await self._prepare_session(message)
        self._client_requests[key] = pending
        authenticate = message["method"] == "authenticate"
        await self._write_daemon(message, None if transformed or authenticate else raw)

    async def route_daemon(self, message: dict[str, Any], raw: bytes | None = None) -> None:
        self._require_open()
        kind = _message_kind(message)
        candidate = _permission_candidate(message, kind)
        if kind == "response":
            key = _request_key(message["id"])
            if key not in self._client_requests:
                raise ProxyError("unsolicited response")
            pending = self._client_requests.pop(key)
            if pending is not None:
                await self._finish_session(pending, message)
            await self._write_client(message, raw)
            return
        method = message["method"]
        params = message.get("params")
        if method in {"mcp/connect", "mcp/message", "mcp/disconnect"}:
            intercepted = await self._route_hosted(message, kind, method, params)
            if intercepted:
                return
        if kind == "request":
            key = _request_key(message["id"])
            self._register_daemon(key)
            if candidate is not None:
                session_id, wrapper_name, tainted = candidate
                if session_id not in self._active_sessions:
                    self._daemon_requests.pop(key, None)
                    self._grants.revoke_session(session_id)
                    raise ProxyError("stale reserved permission request")
                pending_permission = _PendingPermission(
                    session_id, wrapper_name, self._generation
                )
                if self._grants.allows(session_id, wrapper_name) and not tainted:
                    self._daemon_requests.pop(key)
                    await self._write_daemon({
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {
                            "outcome": {
                                "outcome": "selected",
                                "optionId": "allow_once",
                            }
                        },
                    })
                    return
                self._daemon_requests[key] = pending_permission
        await self._write_client(message, raw)

    async def wait_failed(self) -> BaseException:
        return await self._failure

    def _require_open(self) -> None:
        if self._closed:
            raise ProxyError("proxy generation is closed")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._grants.clear()
        self._active_sessions.clear()
        tasks = tuple(task for task in self._local_requests.values() if task is not None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._local_requests.clear()
        self._local_sessions.clear()
        self._local_connections.clear()
        await self._provider.close()
        self._client_requests.clear()
        self._daemon_requests.clear()
        self._server_sessions.clear()
        self._server_provider_sessions.clear()
        self._connection_sessions.clear()
        self._connection_provider_sessions.clear()

    def _register(
        self,
        requests: dict[tuple[type[Any], Any], Any],
        key: tuple[type[Any], Any],
    ) -> None:
        if key in requests or len(requests) >= MAX_OUTSTANDING_REQUESTS:
            raise ProxyError("duplicate outstanding request ID")
        requests[key] = None

    def _register_daemon(self, key: tuple[type[Any], Any]) -> None:
        if (
            key in self._daemon_requests
            or key in self._local_requests
            or key in self._daemon_tombstones
            or len(self._daemon_requests) + len(self._local_requests) >= MAX_OUTSTANDING_REQUESTS
        ):
            raise ProxyError("duplicate outstanding request ID")
        self._daemon_requests[key] = None

    async def _prepare_session(
        self, message: dict[str, Any]
    ) -> tuple[_PendingSession | None, bool]:
        method = message["method"]
        if method not in {"session/new", "session/load"}:
            return None, False
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("cwd"), str):
            raise ProxyError("invalid frame")
        session_id = params.get("sessionId") if method == "session/load" else None
        if method == "session/load":
            if not isinstance(session_id, str) or not session_id:
                raise ProxyError("invalid frame")
            await self._retire_session(session_id)
        servers = params.get("mcpServers")
        if "mcpServers" in params and servers != []:
            return _PendingSession(method, params["cwd"], None, session_id), False
        server_id = self._new_server_id()
        params["mcpServers"] = [{
            "type": "acp",
            "name": "mimir-hands",
            "serverId": server_id,
        }]
        self._provider.bind_session(server_id, params["cwd"])
        self._server_sessions[server_id] = server_id
        self._server_provider_sessions[server_id] = server_id
        return _PendingSession(method, params["cwd"], server_id, session_id), True

    def _new_server_id(self) -> str:
        if len(self._used_server_ids) >= MAX_GENERATION_SERVER_IDS:
            raise ProxyError("too many hosted server IDs")
        while True:
            token = secrets.token_urlsafe(18)
            server_id = f"mimir-hosted:{token}"
            if len(token) == 24 and server_id not in self._used_server_ids:
                self._used_server_ids.add(server_id)
                return server_id

    async def _finish_session(
        self, pending: _PendingSession, response: dict[str, Any]
    ) -> None:
        if "error" in response:
            if pending.server_id is not None:
                await self._retire_session(pending.server_id)
            return
        result = response.get("result")
        if not isinstance(result, dict):
            if pending.server_id is not None:
                await self._retire_session(pending.server_id)
            raise ProxyError("invalid frame")
        if pending.method == "session/new":
            session_id = result.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                await self._retire_session(pending.server_id)
                raise ProxyError("invalid frame")
            await self._retire_session(session_id)
        else:
            session_id = pending.session_id
            if session_id is None:
                raise ProxyError("invalid frame")
        self._active_sessions.add(session_id)
        if pending.server_id is None:
            return
        self._server_sessions[pending.server_id] = session_id
        for connection_id, owner in tuple(self._connection_sessions.items()):
            if owner == pending.server_id:
                self._connection_sessions[connection_id] = session_id

    async def _retire_session(self, session_id: str) -> None:
        self._active_sessions.discard(session_id)
        self._grants.revoke_session(session_id)
        for key, permission in tuple(self._daemon_requests.items()):
            if permission is not None and permission.session_id == session_id:
                self._daemon_requests.pop(key, None)
                self._tombstone(key)
        self._cancel_local_requests(session_id=session_id)
        for connection_id, owner in tuple(self._connection_sessions.items()):
            if owner == session_id:
                try:
                    await self._provider.disconnect(connection_id)
                except HostedMcpError:
                    pass
                self._connection_sessions.pop(connection_id, None)
                self._connection_provider_sessions.pop(connection_id, None)
        for server_id, owner in tuple(self._server_sessions.items()):
            if owner == session_id:
                self._server_sessions.pop(server_id, None)
                provider_session_id = self._server_provider_sessions.pop(server_id, None)
                if provider_session_id is not None:
                    await self._provider.cancel_session(provider_session_id)
                    self._provider.revoke_session(provider_session_id)

    async def _client_notification(self, message: dict[str, Any]) -> None:
        if message["method"] not in {"session/cancel", "session/cancellation"}:
            return
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("sessionId"), str):
            raise ProxyError("invalid frame")
        session_id = params["sessionId"]
        self._cancel_local_requests(session_id=session_id)
        await asyncio.gather(
            *(self._provider.cancel_session(item) for item in self._provider_sessions(session_id))
        )

    async def _route_hosted(
        self, message: dict[str, Any], kind: str, method: str, params: Any
    ) -> bool:
        if not isinstance(params, dict):
            return False
        if method == "mcp/connect":
            server_id = params.get("serverId")
            if server_id not in self._server_sessions:
                if isinstance(server_id, str) and server_id in self._used_server_ids:
                    raise ProxyError("stale hosted server ID")
                return False
            if set(params) != {"serverId"}:
                raise ProxyError("invalid frame")
            if kind != "request":
                raise ProxyError("invalid frame")
            if (
                len(self._connection_sessions) >= MAX_LIVE_CONNECTIONS
                or len(self._used_connection_ids) >= MAX_GENERATION_CONNECTION_IDS
            ):
                raise ProxyError("too many hosted connections")
            key = _request_key(message["id"])
            self._register_local(key)
            session_id = self._server_sessions[server_id]
            provider_session_id = self._server_provider_sessions[server_id]
            try:
                connection_id = self._provider.connect(provider_session_id)
                if connection_id in self._used_connection_ids:
                    raise ProxyError("reused hosted connection ID")
                self._used_connection_ids.add(connection_id)
                self._connection_sessions[connection_id] = session_id
                self._connection_provider_sessions[connection_id] = provider_session_id
                await self._complete_local(key, {"connectionId": connection_id})
            except HostedMcpError as exc:
                await self._fail_local(key, exc)
            return True
        connection_id = params.get("connectionId")
        if connection_id not in self._connection_sessions:
            if isinstance(connection_id, str) and connection_id in self._used_connection_ids:
                raise ProxyError("stale hosted connection ID")
            return False
        if method == "mcp/disconnect":
            if set(params) != {"connectionId"}:
                raise ProxyError("invalid frame")
            if kind != "request":
                raise ProxyError("invalid frame")
            key = _request_key(message["id"])
            self._register_local(key)
            session_id = self._connection_sessions[connection_id]
            self._grants.revoke_session(session_id)
            try:
                self._cancel_local_requests(connection_id=connection_id)
                result = await self._provider.disconnect(connection_id)
                self._connection_sessions.pop(connection_id, None)
                self._connection_provider_sessions.pop(connection_id, None)
                await self._complete_local(key, result)
            except HostedMcpError as exc:
                await self._fail_local(key, exc)
            return True
        nested_method = params.get("method")
        if set(params) - {"connectionId", "method", "params"} or not isinstance(
            nested_method, str
        ):
            raise ProxyError("invalid frame")
        nested_params = params.get("params")
        if kind == "notification":
            if nested_method == "notifications/cancelled":
                if not isinstance(nested_params, dict) or set(nested_params) != {"requestId"}:
                    raise ProxyError("invalid frame")
                cancelled_key = _request_key(nested_params["requestId"])
                if self._local_connections.get(cancelled_key) == connection_id:
                    task = self._local_requests.get(cancelled_key)
                    self._tombstone(cancelled_key)
                    if task is not None:
                        task.cancel()
            try:
                await self._provider.notification(connection_id, nested_method, nested_params)
            except HostedMcpError as exc:
                raise ProxyError("invalid hosted notification") from exc
            return True
        key = _request_key(message["id"])
        self._register_local(key)
        session_id = self._connection_sessions[connection_id]
        task = asyncio.create_task(
            self._hosted_request(key, session_id, connection_id, nested_method, nested_params)
        )
        self._local_requests[key] = task
        self._local_sessions[key] = session_id
        self._local_connections[key] = connection_id
        return True

    def _register_local(self, key: tuple[type[Any], Any]) -> None:
        if (
            key in self._daemon_requests
            or key in self._local_requests
            or key in self._daemon_tombstones
            or len(self._daemon_requests) + len(self._local_requests) >= MAX_OUTSTANDING_REQUESTS
        ):
            raise ProxyError("duplicate outstanding request ID")
        self._local_requests[key] = None

    async def _hosted_request(
        self,
        key: tuple[type[Any], Any],
        session_id: str,
        connection_id: str,
        method: str,
        params: Any,
    ) -> None:
        try:
            try:
                result = await self._provider.request(
                    connection_id, method, params, request_id=key[1]
                )
            except HostedMcpError as exc:
                await self._fail_local(key, exc)
            else:
                await self._complete_local(key, result)
        except asyncio.CancelledError:
            if key not in self._daemon_tombstones:
                raise
        except BaseException as exc:
            self._fail_generation(exc)
        finally:
            self._local_requests.pop(key, None)
            self._local_sessions.pop(key, None)
            self._local_connections.pop(key, None)

    async def _complete_local(self, key: tuple[type[Any], Any], result: Any) -> None:
        self._local_requests.pop(key, None)
        self._local_sessions.pop(key, None)
        self._local_connections.pop(key, None)
        if key not in self._daemon_tombstones:
            await self._write_daemon({"jsonrpc": "2.0", "id": key[1], "result": result})

    async def _fail_local(
        self, key: tuple[type[Any], Any], error: HostedMcpError
    ) -> None:
        self._local_requests.pop(key, None)
        self._local_sessions.pop(key, None)
        self._local_connections.pop(key, None)
        if key not in self._daemon_tombstones:
            await self._write_daemon({"jsonrpc": "2.0", "id": key[1], "error": error.as_error()})

    def _tombstone(self, key: tuple[type[Any], Any]) -> None:
        if key in self._daemon_tombstones:
            return
        if len(self._daemon_tombstones) >= MAX_OUTSTANDING_REQUESTS:
            raise ProxyError("too many cancelled requests")
        self._daemon_tombstones.add(key)

    def _cancel_local_requests(
        self, *, session_id: str | None = None, connection_id: str | None = None
    ) -> None:
        for key, task in tuple(self._local_requests.items()):
            if task is None:
                continue
            if session_id is not None and self._local_sessions.get(key) != session_id:
                continue
            if connection_id is not None and self._local_connections.get(key) != connection_id:
                continue
            self._tombstone(key)
            task.cancel()

    def _provider_sessions(self, session_id: str) -> set[str]:
        result = {
            provider_session_id
            for connection_id, provider_session_id in self._connection_provider_sessions.items()
            if self._connection_sessions.get(connection_id) == session_id
        }
        result.update(
            provider_session_id
            for server_id, provider_session_id in self._server_provider_sessions.items()
            if self._server_sessions.get(server_id) == session_id
        )
        return result

    def _fail_generation(self, error: BaseException) -> None:
        self._grants.clear()
        self._active_sessions.clear()
        if not self._failure.done():
            self._failure.set_result(error)

    async def _write_client(
        self, message: dict[str, Any], raw: bytes | None = None
    ) -> None:
        async with self._client_lock:
            if raw is None:
                self._client.write_message(message)
            else:
                self._client.write_raw(raw)
            await self._client.drain()

    async def _write_daemon(
        self, message: dict[str, Any], raw: bytes | None = None
    ) -> None:
        async with self._daemon_lock:
            if raw is None:
                self._daemon.write_message(message)
            else:
                self._daemon.write_raw(raw)
            await self._daemon.drain()


async def _route_stream(reader: Any, route: Any) -> None:
    buffer = bytearray()
    while True:
        data = await reader.read(64 * 1024)
        if not data:
            if buffer:
                raise ProxyError("invalid frame")
            return
        buffer.extend(data)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                if len(buffer) >= MAX_FRAME_BYTES:
                    raise ProxyError("invalid frame")
                break
            if newline + 1 > MAX_FRAME_BYTES:
                raise ProxyError("invalid frame")
            raw = bytes(buffer[:newline])
            del buffer[: newline + 1]
            try:
                message = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProxyError("invalid frame") from exc
            if not isinstance(message, dict):
                raise ProxyError("invalid frame")
            await route(message, raw + b"\n")


async def _raise_completed(
    completed: set[asyncio.Task[Any]], failure_task: asyncio.Task[BaseException]
) -> None:
    ordered = tuple(completed)
    results = await asyncio.gather(*ordered, return_exceptions=True)
    if failure_task in completed:
        raise failure_task.result()
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def run_router(
    client_reader: Any,
    client_writer: Any,
    daemon_reader: Any,
    daemon_writer: Any,
    credential: str,
    *,
    timeout_seconds: int = 60,
    close_on_daemon_exit: bool = False,
) -> None:
    router = ProxyRouter(client_writer, daemon_writer, credential, timeout_seconds)
    client_task = asyncio.create_task(_route_stream(client_reader, router.route_client))
    daemon_task = asyncio.create_task(_route_stream(daemon_reader, router.route_daemon))
    failure_task = asyncio.create_task(router.wait_failed())
    tasks = {client_task, daemon_task, failure_task}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        await _raise_completed(done, failure_task)
        stream_pending = pending - {failure_task}
        if stream_pending and not (close_on_daemon_exit and daemon_task in done):
            completed, stream_pending = await asyncio.wait(
                stream_pending | {failure_task}, timeout=PEER_EOF_GRACE_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            await _raise_completed(completed, failure_task)
        pending = stream_pending | ({failure_task} if not failure_task.done() else set())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        await router.close()
        closing = asyncio.gather(
            close_writer(client_writer), close_writer(daemon_writer), return_exceptions=True
        )
        try:
            await asyncio.wait_for(closing, FORCE_CLOSE_TIMEOUT)
        except TimeoutError:
            pass

class _OutputWriter:
    def __init__(self, output: BinaryIO) -> None: self.output, self.closed = output, False
    def write(self, data: bytes) -> None:
        if self.closed: raise BrokenPipeError
        remaining = memoryview(data)
        while remaining:
            size = self.output.write(remaining)
            if size is None: size = len(remaining)
            if size <= 0: raise BrokenPipeError
            remaining = remaining[size:]
        self.output.flush()
    async def drain(self) -> None: self.output.flush()
    def close(self) -> None: self.closed = True
    def is_closing(self) -> bool: return self.closed
    async def wait_closed(self) -> None: return None

async def open_stdio(output: BinaryIO) -> tuple[asyncio.StreamReader, Any, asyncio.BaseTransport]:
    loop = asyncio.get_running_loop(); reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    try:
        output.fileno()
    except (AttributeError, io.UnsupportedOperation):
        writer: Any = _OutputWriter(output)
    else:
        output_protocol = asyncio.streams.FlowControlMixin(loop=loop)
        output_transport, _ = await loop.connect_write_pipe(lambda: output_protocol, output)
        writer = asyncio.StreamWriter(output_transport, output_protocol, None, loop)
    return reader, writer, transport


def socket_path(profile: Profile) -> Path:
    path = profile.home / ".mimir" / "acp" / "daemon.sock"
    try:
        value = path.lstat(); directory = path.parent.lstat()
    except OSError as exc: raise ProxyError("connection failed") from exc
    uid = os.getuid()
    if (not stat.S_ISSOCK(value.st_mode) or stat.S_ISLNK(value.st_mode) or value.st_uid != uid or
        not stat.S_ISDIR(directory.st_mode) or stat.S_ISLNK(directory.st_mode) or directory.st_uid != uid or directory.st_mode & 0o077):
        raise ProxyError("connection failed")
    return path

async def run_local_proxy(profile: Profile, credential: str, output: BinaryIO) -> None:
    path = socket_path(profile)
    upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), CONNECT_TIMEOUT)
    try:
        stdin_reader, stdout_writer, stdin_transport = await open_stdio(output)
    except BaseException:
        await close_writer(upstream_writer)
        raise
    try:
        await run_router(
            stdin_reader,
            stdout_writer,
            upstream_reader,
            upstream_writer,
            credential,
            timeout_seconds=profile.timeout_seconds,
        )
    finally:
        stdin_transport.close()

async def run_proxy(profile_name: str | None, output: BinaryIO, *, profiles: ProfileStore | None = None, credentials: NativeCredentialStore | None = None) -> None:
    name = selected_profile(profile_name)
    profile = (profiles or ProfileStore()).get(name)
    if profile is None: raise ProfileError("profile-not-found")
    credential = (credentials or NativeCredentialStore()).get(name)
    if credential is None: raise CredentialError("credential-read-failed")
    if profile.remote is not None: raise ProxyError("remote profile requires SSH")
    await run_local_proxy(profile, credential, output)
