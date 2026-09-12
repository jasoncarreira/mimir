from __future__ import annotations

import atexit
import asyncio
import io
import json
import logging
import os
import secrets
import signal
import socket
import stat
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .credentials import CredentialError, NativeCredentialStore
from .execution_scope import ScopeApproval
from .hosted import HostedHandsProvider, HostedMcpError
from .profiles import Profile, ProfileError, ProfileStore, selected_profile
from .transport import FORCE_CLOSE_TIMEOUT, PEER_EOF_GRACE_TIMEOUT, close_writer
from .diagnostics import failure_detail

CONNECT_TIMEOUT = 5.0
# One process-wide grace from the first signal, not one timeout per await.
SIGNAL_EXIT_TIMEOUT = 5.0
MAX_FRAME_BYTES = 1024 * 1024
MAX_OUTSTANDING_REQUESTS = 1024
MAX_GENERATION_SERVER_IDS = 1024
MAX_GENERATION_CONNECTION_IDS = 4096
MAX_LIVE_CONNECTIONS = 1024
SCOPE_PERMISSION_TIMEOUT_SECONDS = 60.0
MAX_SCOPE_PERMISSION_REQUESTS = 1024
MAX_UNCONFINED_PERMISSION_REQUESTS = 128
_SCOPE_REQUEST_PREFIX = "mimir-scope:"
_UNCONFINED_REQUEST_PREFIX = "mimir-unconfined:"


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


class ProxySignalExit(Exception):
    def __init__(self, signum: int) -> None:
        self.code = 128 + signum
        super().__init__(self.code)


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
    explicit_hands_server_ids: tuple[str, ...]
    claimed_explicit_server_ids: set[str] = field(default_factory=set)
    explicit_connection_ids: set[str] = field(default_factory=set)
    resolved_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class _PendingPermission:
    session_id: str
    wrapper_name: str
    generation: object


@dataclass(frozen=True, slots=True)
class _PendingExecutionPermission:
    purpose: str
    session_id: str
    provider_session_id: str
    connection_id: str
    owner_key: tuple[type[Any], Any]
    owner_task: asyncio.Task[Any]
    generation: object
    completion: asyncio.Future[bool]


@dataclass(frozen=True, slots=True)
class _PendingExplicitConnect:
    owner: str | _PendingSession
    generation: object

    @property
    def session_id(self) -> str | None:
        if isinstance(self.owner, str):
            return self.owner
        return self.owner.resolved_session_id


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
        self._provider = HostedHandsProvider(
            timeout_seconds,
            request_scope_permission=self._request_scope_permission,
            request_unconfined_permission=self._request_unconfined_permission,
        )
        self._generation = object()
        self._grants = PermissionGrantStore()
        self._active_sessions: set[str] = set()
        self._client_requests: dict[tuple[type[Any], Any], _PendingSession | None] = {}
        self._daemon_requests: dict[
            tuple[type[Any], Any], _PendingPermission | _PendingExplicitConnect | None
        ] = {}
        self._local_requests: dict[tuple[type[Any], Any], asyncio.Task[None] | None] = {}
        self._local_sessions: dict[tuple[type[Any], Any], str] = {}
        self._daemon_tombstones: set[tuple[type[Any], Any]] = set()
        self._execution_permissions: dict[tuple[type[Any], Any], _PendingExecutionPermission] = {}
        self._execution_permission_tombstones: set[tuple[type[Any], Any]] = set()
        self._scope_request_count = 0
        self._unconfined_request_count = 0
        self._server_sessions: dict[str, str] = {}
        self._server_provider_sessions: dict[str, str] = {}
        self._connection_sessions: dict[str, str] = {}
        self._connection_provider_sessions: dict[str, str] = {}
        self._explicit_server_sessions: dict[str, str] = {}
        self._explicit_connection_sessions: dict[str, str] = {}
        self._used_server_ids: set[str] = set()
        self._used_connection_ids: set[str] = set()
        self._local_connections: dict[tuple[type[Any], Any], str] = {}
        self._client_lock = asyncio.Lock()
        self._daemon_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._client_routes: set[asyncio.Task[Any]] = set()
        self._failure: asyncio.Future[BaseException] = asyncio.get_running_loop().create_future()
        self._generation_cleanup_task: asyncio.Task[None] | None = None
        self._closed = False
        self._close_complete = False
        self._generation_failed = False

    async def route_client(self, message: dict[str, Any], raw: bytes | None = None) -> None:
        self._require_open()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("client routing requires an asyncio task")
        self._client_routes.add(task)
        try:
            await self._route_client(message, raw)
        finally:
            self._client_routes.discard(task)

    async def _route_client(self, message: dict[str, Any], raw: bytes | None = None) -> None:
        kind = _message_kind(message)
        if kind == "response":
            key = _request_key(message["id"])
            scope_permission = self._execution_permissions.get(key)
            if scope_permission is not None:
                try:
                    approved = _permission_response_decision(message) == "allow_session"
                except ProxyError:
                    approved = False
                if not scope_permission.completion.done():
                    scope_permission.completion.set_result(
                        approved and self._execution_permission_is_current(scope_permission)
                    )
                return
            if key in self._execution_permission_tombstones:
                return
            if key in self._daemon_tombstones:
                return
            if key not in self._daemon_requests:
                raise ProxyError("unsolicited response")
            pending = self._daemon_requests.pop(key)
            decision = None
            if isinstance(pending, _PendingPermission):
                if (
                    pending.generation is not self._generation
                    or pending.session_id not in self._active_sessions
                ):
                    raise ProxyError("stale reserved permission response")
                decision = _permission_response_decision(message)
            elif (
                isinstance(pending, _PendingExplicitConnect)
                and pending.generation is self._generation
            ):
                result = message.get("result")
                connection_id = result.get("connectionId") if isinstance(result, dict) else None
                if isinstance(connection_id, str) and connection_id:
                    session_id = pending.session_id
                    if session_id in self._active_sessions:
                        self._explicit_connection_sessions[connection_id] = session_id
                    elif isinstance(pending.owner, _PendingSession):
                        pending.owner.explicit_connection_ids.add(connection_id)
            await self._write_daemon(message, raw)
            if (
                isinstance(pending, _PendingPermission)
                and decision == "allow_session"
            ):
                self._grants.add(
                    pending.session_id, pending.wrapper_name
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
        if (
            method == "mcp/disconnect"
            and kind == "request"
            and isinstance(params, dict)
            and set(params) == {"connectionId"}
        ):
            connection_id = params.get("connectionId")
            if isinstance(connection_id, str):
                session_id = self._explicit_connection_sessions.pop(connection_id, None)
                if session_id is not None:
                    self._grants.revoke_session(session_id)
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
                    # Telemetry only: the daemon emits the single permission event
                    # from this completion; the proxy must not emit another event.
                    await self._write_daemon({
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {
                            "outcome": {
                                "outcome": "selected",
                                "optionId": "allow_once",
                            },
                            "_meta": {"mimir.permission_source": "session_grant"},
                        },
                    })
                    return
                self._daemon_requests[key] = pending_permission
            elif (
                method == "mcp/connect"
                and isinstance(params, dict)
                and set(params) == {"serverId"}
                and isinstance(params.get("serverId"), str)
            ):
                owner: str | _PendingSession | None = self._pending_explicit_session(
                    params["serverId"]
                )
                if owner is None:
                    owner = self._explicit_server_sessions.get(params["serverId"])
                if owner is not None:
                    self._daemon_requests[key] = _PendingExplicitConnect(
                        owner, self._generation
                    )
        await self._write_client(message, raw)

    def _execution_permission_is_current(self, pending: _PendingExecutionPermission) -> bool:
        return (
            not self._closed
            and not self._generation_failed
            and pending.generation is self._generation
            and pending.session_id in self._active_sessions
            and self._connection_sessions.get(pending.connection_id) == pending.session_id
            and self._connection_provider_sessions.get(pending.connection_id)
            == pending.provider_session_id
            and self._local_requests.get(pending.owner_key) is pending.owner_task
            and pending.owner_key not in self._daemon_tombstones
            and not pending.owner_task.cancelling()
        )

    async def _request_scope_permission(
        self, provider_session_id: str, approval: ScopeApproval,
    ) -> bool:
        return await self._request_execution_permission(
            provider_session_id, path=str(approval.path), recursive=approval.recursive,
        )

    async def _request_unconfined_permission(self, provider_session_id: str) -> bool:
        return await self._request_execution_permission(provider_session_id, path=None)

    async def _request_execution_permission(
        self, provider_session_id: str, *, path: str | None, recursive: bool = False,
    ) -> bool:
        """Ask the operator for one distinct scope or unavailable-backend risk.

        Only a live hosted tools/call task may ask. The provider owns path
        canonicalization, backend eligibility, final denials, session grants,
        and the approval audit. Neither flow consults reusable wrapper grants.
        """
        task = asyncio.current_task()
        owner_key = next(
            (key for key, owner in self._local_requests.items() if owner is task), None
        )
        if owner_key is None or task is None:
            return False
        connection_id = self._local_connections.get(owner_key)
        session_id = self._local_sessions.get(owner_key)
        if connection_id is None or session_id is None:
            return False
        unconfined = path is None
        purpose = "unconfined" if unconfined else "scope"
        if (
            (unconfined and self._unconfined_request_count >= MAX_UNCONFINED_PERMISSION_REQUESTS)
            or (not unconfined and self._scope_request_count >= MAX_SCOPE_PERMISSION_REQUESTS)
            or len(self._execution_permissions) + len(self._daemon_requests)
            + len(self._local_requests) >= MAX_OUTSTANDING_REQUESTS
            or any(item.session_id == session_id for item in self._execution_permissions.values())
        ):
            return False
        pending = _PendingExecutionPermission(
            purpose, session_id, provider_session_id, connection_id, owner_key, task,
            self._generation, asyncio.get_running_loop().create_future(),
        )
        if not self._execution_permission_is_current(pending):
            return False
        if unconfined:
            self._unconfined_request_count += 1
            request_id = f"{_UNCONFINED_REQUEST_PREFIX}{self._unconfined_request_count}"
        else:
            self._scope_request_count += 1
            request_id = f"{_SCOPE_REQUEST_PREFIX}{self._scope_request_count}"
        key = _request_key(request_id)
        self._execution_permissions[key] = pending
        # No model-controlled command, code, reason, or backend error is sent.
        title = (
            "Confinement is unavailable. Allow UNCONFINED hands_shell and hands_python "
            "with the local proxy user's unrestricted filesystem permissions? "
            "The cwd and path-scope grants do NOT protect files in this mode. "
            "Acceptance restarts any existing Python kernel and loses variables/imports. "
            "It is for this session only, is not persisted, and is separate "
            "from tool permissions and taint acknowledgement. Rejection is final "
            "for this session."
            if unconfined else
            "Allow read/write access to "
            + ("this directory and everything beneath it" if recursive else "this file alone")
            + " for this session? "
            "Approval restarts the Python kernel and loses all REPL state."
        )
        params = {
            "sessionId": session_id,
            "toolCall": {
                "toolCallId": request_id,
                "title": title,
                "kind": "other",
                "status": "pending",
                "rawInput": {} if unconfined else {"path": path},
            },
            "options": [
                {
                    "optionId": "allow_session",
                    "name": (
                        "Accept unconfined execution for this session"
                        if unconfined else "Allow this scope for this session"
                    ),
                    "kind": "allow_always",
                },
                {
                    "optionId": "reject_once",
                    "name": (
                        "Reject unconfined execution for this session"
                        if unconfined else "Reject this path for this session"
                    ),
                    "kind": "reject_once",
                },
            ],
            "_meta": {
                "mimir.unconfined_execution" if unconfined else "mimir.execution_scope": True,
            },
        }
        try:
            async with asyncio.timeout(SCOPE_PERMISSION_TIMEOUT_SECONDS):
                await self._write_client({
                    "jsonrpc": "2.0", "id": request_id,
                    "method": PERMISSION_METHOD, "params": params,
                })
                approved = await pending.completion
            return approved and self._execution_permission_is_current(pending)
        except TimeoutError:
            return False
        finally:
            self._execution_permissions.pop(key, None)
            # Counts bound the separate ID spaces, including completed requests.
            # Duplicate/late answers must never reach the daemon or another grant.
            self._execution_permission_tombstones.add(key)
            if not pending.completion.done():
                pending.completion.cancel()

    def _cancel_execution_permissions(self, session_id: str | None = None) -> None:
        for pending in tuple(self._execution_permissions.values()):
            if session_id is None or pending.session_id == session_id:
                if not pending.completion.done():
                    pending.completion.set_result(False)

    async def wait_failed(self) -> BaseException:
        return await self._failure

    def _require_open(self) -> None:
        if self._closed or self._generation_failed:
            raise ProxyError("proxy generation is closed")

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_complete:
                return
            self._closed = True
            self._cancel_execution_permissions()
            current = asyncio.current_task()
            routes = tuple(task for task in self._client_routes if task is not current)
            for task in routes:
                task.cancel()
            await asyncio.gather(*routes, return_exceptions=True)
            self._grants.clear()
            self._active_sessions.clear()
            tasks = tuple(task for task in self._local_requests.values() if task is not None)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._local_requests.clear()
            self._local_sessions.clear()
            self._local_connections.clear()
            if self._generation_cleanup_task is None:
                await self._provider.close()
            elif self._generation_cleanup_task is not asyncio.current_task():
                await asyncio.shield(self._generation_cleanup_task)
            self._client_requests.clear()
            self._daemon_requests.clear()
            self._server_sessions.clear()
            self._server_provider_sessions.clear()
            self._connection_sessions.clear()
            self._connection_provider_sessions.clear()
            self._explicit_server_sessions.clear()
            self._explicit_connection_sessions.clear()
            self._used_server_ids.clear()
            self._used_connection_ids.clear()
            self._daemon_tombstones.clear()
            self._execution_permissions.clear()
            self._execution_permission_tombstones.clear()
            self._close_complete = True

    def terminate_owned_children(self) -> None:
        self._grants.clear()
        self._provider.terminate_owned_children()

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
            (isinstance(key[1], str) and key[1].startswith((_SCOPE_REQUEST_PREFIX, _UNCONFINED_REQUEST_PREFIX)))
            or key in self._daemon_requests
            or key in self._local_requests
            or key in self._daemon_tombstones
            or len(self._daemon_requests) + len(self._local_requests) >= MAX_OUTSTANDING_REQUESTS
        ):
            raise ProxyError("duplicate outstanding request ID")
        self._daemon_requests[key] = None

    def _pending_explicit_session(self, server_id: str) -> _PendingSession | None:
        for pending in self._client_requests.values():
            if (
                pending is not None
                and server_id in pending.explicit_hands_server_ids
                and server_id not in pending.claimed_explicit_server_ids
            ):
                pending.claimed_explicit_server_ids.add(server_id)
                return pending
        return None

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
            explicit_hands_server_ids = (
                tuple(
                    server["serverId"]
                    for server in servers
                    if isinstance(server, dict)
                    and server.get("type") == "acp"
                    and server.get("name") == "mimir-hands"
                    and isinstance(server.get("serverId"), str)
                    and server["serverId"]
                )
                if isinstance(servers, list)
                else ()
            )
            return _PendingSession(
                method,
                params["cwd"],
                None,
                session_id,
                explicit_hands_server_ids,
                resolved_session_id=session_id,
            ), False
        server_id = self._new_server_id()
        params["mcpServers"] = [{
            "type": "acp",
            "name": "mimir-hands",
            "serverId": server_id,
        }]
        self._provider.bind_session(server_id, params["cwd"])
        self._server_sessions[server_id] = server_id
        self._server_provider_sessions[server_id] = server_id
        return _PendingSession(method, params["cwd"], server_id, session_id, ()), True

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
            pending.resolved_session_id = session_id
            await self._retire_session(session_id)
        else:
            session_id = pending.session_id
            if session_id is None:
                raise ProxyError("invalid frame")
            pending.resolved_session_id = session_id
        self._active_sessions.add(session_id)
        for server_id in pending.explicit_hands_server_ids:
            self._explicit_server_sessions[server_id] = session_id
        for connection_id in pending.explicit_connection_ids:
            self._explicit_connection_sessions[connection_id] = session_id
        if pending.server_id is None:
            return
        self._server_sessions[pending.server_id] = session_id
        for connection_id, owner in tuple(self._connection_sessions.items()):
            if owner == pending.server_id:
                self._connection_sessions[connection_id] = session_id

    async def _retire_session(self, session_id: str) -> None:
        self._active_sessions.discard(session_id)
        self._cancel_execution_permissions(session_id)
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
        for server_id, owner in tuple(self._explicit_server_sessions.items()):
            if owner == session_id:
                self._explicit_server_sessions.pop(server_id, None)
        for connection_id, owner in tuple(self._explicit_connection_sessions.items()):
            if owner == session_id:
                self._explicit_connection_sessions.pop(connection_id, None)

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
                if method == "mcp/disconnect":
                    if set(params) != {"connectionId"} or kind != "request":
                        raise ProxyError("invalid frame")
                    key = _request_key(message["id"])
                    self._register_local(key)
                    await self._complete_local(key, {})
                    return True
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
                self._cancel_local_requests(session_id=session_id)
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
            (isinstance(key[1], str) and key[1].startswith((_SCOPE_REQUEST_PREFIX, _UNCONFINED_REQUEST_PREFIX)))
            or key in self._daemon_requests
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
        if not self._generation_failed and key not in self._daemon_tombstones:
            await self._write_daemon({"jsonrpc": "2.0", "id": key[1], "result": result})

    async def _fail_local(
        self, key: tuple[type[Any], Any], error: HostedMcpError
    ) -> None:
        self._local_requests.pop(key, None)
        self._local_sessions.pop(key, None)
        self._local_connections.pop(key, None)
        if not self._generation_failed and key not in self._daemon_tombstones:
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
        if session_id is not None:
            self._cancel_execution_permissions(session_id)
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
        if self._failure.done() or self._generation_cleanup_task is not None:
            return
        self._generation_failed = True
        self._cancel_execution_permissions()
        self._grants.clear()
        self._active_sessions.clear()
        for task in tuple(self._local_requests.values()):
            if task is not None:
                task.cancel()
        self._local_requests.clear()
        self._local_sessions.clear()
        self._local_connections.clear()
        self._client_requests.clear()
        self._daemon_requests.clear()
        self._server_sessions.clear()
        self._server_provider_sessions.clear()
        self._connection_sessions.clear()
        self._connection_provider_sessions.clear()
        self._explicit_server_sessions.clear()
        self._explicit_connection_sessions.clear()
        self._used_server_ids.clear()
        self._used_connection_ids.clear()
        self._daemon_tombstones.clear()
        self._generation_cleanup_task = asyncio.create_task(
            self._complete_generation_failure(error)
        )

    async def _complete_generation_failure(self, error: BaseException) -> None:
        try:
            await self._provider.close()
        except BaseException as cleanup_error:
            if not self._failure.done():
                self._failure.set_result(cleanup_error)
            return
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


async def _wait_for_eof(reader: Any) -> None:
    while await reader.read(64 * 1024):
        pass


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


class _SignalReapFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Synchronous owned-child reaping intentionally races asyncio's child
        # watcher. This one diagnostic is expected after a committed signal exit;
        # do not suppress other asyncio errors or unsignalled failures.
        return record.msg != "Unknown child process pid %d, will report returncode 255"


class _ShutdownHooks:
    _wakeup_owners: set[_ShutdownHooks] = set()

    def __init__(
        self, router: ProxyRouter, signal_cleanup: Callable[[], None] | None = None,
    ) -> None:
        self._router = router
        self._signal_cleanup = signal_cleanup
        self._watchdog: threading.Timer | None = None
        self._failure_detail: bytes | None = None
        self._signals: dict[int, Any] = {}
        self._handler = self._handle_signal
        self._installed = False
        self.signum: int | None = None
        self.closing = False
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self._wakeup: tuple[socket.socket, socket.socket] | None = None
        self._previous_wakeup_fd = -1
        self._loop_close = self._loop.close
        self._close_loop = self._close_loop

    def _drain_wakeup(self) -> None:
        assert self._wakeup is not None
        try:
            while self._wakeup[0].recv(4096):
                pass
        except BlockingIOError:
            pass

    def _close_wakeup(self) -> None:
        if self._wakeup is None:
            return
        # Detach before closing descriptors, so a signal cannot write to a
        # recycled fd. The Python handler deliberately has a longer lifetime.
        reader, writer = self._wakeup
        # Unlink saved restore targets before releasing this owner's sockets.
        # Wakeup ownership is process-wide, even across different event loops.
        for owner in self._wakeup_owners:
            if owner is self:
                continue
            if owner._previous_wakeup_fd == writer.fileno():
                owner._previous_wakeup_fd = self._previous_wakeup_fd
            if owner._loop_close is self._close_loop:
                owner._loop_close = self._loop_close
            for signum, previous in self._signals.items():
                if owner._signals.get(signum) is self._handler:
                    owner._signals[signum] = previous
        # set_wakeup_fd has no getter. Preserve a replacement owner's fd rather
        # than restoring ours over it; never close a descriptor still installed.
        current = signal.set_wakeup_fd(-1)
        signal.set_wakeup_fd(
            self._previous_wakeup_fd if current == writer.fileno() else current,
            warn_on_full_buffer=False,
        )
        self._loop.remove_reader(reader.fileno())
        reader.close()
        writer.close()
        self._wakeup = None
        self._wakeup_owners.discard(self)
        if self._loop.close is self._close_loop:
            self._loop.close = self._loop_close

    def _close_loop(self) -> None:
        if self._loop.is_running():
            # Preserve close()'s error without dismantling a live loop's wakeup.
            self._loop_close()
            return
        self._close_wakeup()
        self._loop_close()

    def install(self) -> None:
        if self._wakeup is not None:
            return
        if threading.current_thread() is threading.main_thread():
            reader, writer = socket.socketpair()
            try:
                reader.setblocking(False)
                writer.setblocking(False)
                self._loop.add_reader(reader.fileno(), self._drain_wakeup)
                self._previous_wakeup_fd = signal.set_wakeup_fd(
                    writer.fileno(), warn_on_full_buffer=False,
                )
            except BaseException:
                self._loop.remove_reader(reader.fileno())
                reader.close()
                writer.close()
                raise
            self._wakeup = reader, writer
            self._wakeup_owners.add(self)
            # asyncio has no public close-callback API. Bind cleanup to close,
            # not task cancellation: Runner still drains tasks and the executor.
            self._loop_close = self._loop.close
            self._loop.close = self._close_loop
            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                self._signals[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handler)
        atexit.register(self._cleanup)
        self._installed = True

    def close(self) -> None:
        if not self._installed:
            return
        if self.signum is not None:
            atexit.unregister(self._cleanup)
            # A signal commits this CLI process to exit. Keep both the deadline
            # and escalation handler through outer SSH/asyncio.run/atexit drains.
            # Cancelling the watchdog here would make those waits unbounded again.
            return
        self._installed = False
        atexit.unregister(self._cleanup)
        if threading.current_thread() is threading.main_thread():
            for signum, previous in self._signals.items():
                if signal.getsignal(signum) is self._handler:
                    signal.signal(signum, previous)
        self._close_wakeup()
        self._signals.clear()

    def _cleanup(self) -> None:
        try:
            self._router.terminate_owned_children()
        except Exception:
            # Last-resort best effort at the signal/atexit boundary. Never throw
            # through interrupted I/O. Routing and async close failures still use
            # record_failure; only this synchronous cleanup callback is guarded.
            pass

    def record_failure(self, error: BaseException) -> None:
        if isinstance(error, (asyncio.CancelledError, ProxySignalExit)):
            return
        if self._failure_detail is None:
            self._failure_detail = failure_detail(error)

    def _force_exit(self) -> None:
        assert self.signum is not None
        if self._failure_detail is not None:
            try:
                # Diagnostics must not turn the hard deadline into another
                # drain wait when stderr is a full or disconnected pipe.
                os.set_blocking(2, False)
                os.write(2, self._failure_detail)
            finally:
                os._exit(1)
        os._exit(128 + self.signum)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        del frame
        if self.signum is not None:
            # A repeated supported signal explicitly abandons graceful draining.
            os._exit(128 + signum)
        self.signum = signum
        # wait_for/task.cancel cannot bound cancellation-resistant coroutines (or
        # a blocked event loop). Arm before synchronous cleanup, and retain until
        # process exit. Normal EOF and genuine failures never arm this watchdog.
        self._watchdog = threading.Timer(SIGNAL_EXIT_TIMEOUT, self._force_exit)
        self._watchdog.daemon = True
        self._watchdog.start()
        logging.getLogger("asyncio").addFilter(_SignalReapFilter())
        if self._signal_cleanup is not None:
            try:
                self._signal_cleanup()
            except Exception:
                # An outer owned-child callback must not prevent router cleanup.
                pass
        self._cleanup()
        # Wake the loop without throwing through an interrupted selector/transport.
        try:
            self._loop.call_soon_threadsafe(self._cancel)
        except RuntimeError:
            # The loop may already be closed during atexit; the watchdog still
            # owns the process deadline and repeat-signal escalation stays armed.
            pass

    def _cancel(self) -> None:
        if self._task is None or self._task.done():
            # Readiness can be printed just before the installing task returns;
            # recheck here, not only in the signal handler, or run_forever hangs.
            if self._task is not None and not self._task.cancelled():
                error = self._task.exception()
                if error is not None:
                    self.record_failure(error)
                    # Let the caller report its normal failure/signal result;
                    # the watchdog still bounds any outer cleanup.
                    return
            self._force_exit()
        if not self.closing:
            self._task.cancel()


async def run_router(
    client_reader: Any,
    client_writer: Any,
    daemon_reader: Any,
    daemon_writer: Any,
    credential: str,
    *,
    timeout_seconds: int = 60,
    close_on_daemon_exit: bool = False,
    signal_cleanup: Callable[[], None] | None = None,
) -> None:
    router = ProxyRouter(client_writer, daemon_writer, credential, timeout_seconds)
    hooks = _ShutdownHooks(router, signal_cleanup)
    hooks.install()
    client_task = asyncio.create_task(_route_stream(client_reader, router.route_client))
    daemon_task = asyncio.create_task(_route_stream(daemon_reader, router.route_daemon))
    failure_task = asyncio.create_task(router.wait_failed())
    tasks = {client_task, daemon_task, failure_task}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        await _raise_completed(done, failure_task)
        if daemon_task in done:
            client_was_pending = client_task in pending
            if client_was_pending:
                client_task.cancel()
                await asyncio.gather(client_task, return_exceptions=True)
            await router.close()
            if client_was_pending and not close_on_daemon_exit:
                peer_task = asyncio.create_task(_wait_for_eof(client_reader))
                tasks.add(peer_task)
                stream_pending = {peer_task}
            else:
                stream_pending = set()
        else:
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
    except BaseException as exc:
        hooks.record_failure(exc)
        for task in tasks:
            if task.done() and not task.cancelled():
                error = task.exception()
                if error is not None:
                    hooks.record_failure(error)
                elif task is failure_task:
                    hooks.record_failure(task.result())
        hooks.closing = True
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if not (isinstance(exc, asyncio.CancelledError) and hooks.signum is not None):
            raise
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                hooks.record_failure(result)
                raise result
    finally:
        hooks.closing = True
        try:
            await router.close()
            closing = asyncio.gather(
                close_writer(client_writer), close_writer(daemon_writer), return_exceptions=True
            )
            try:
                await asyncio.wait_for(closing, FORCE_CLOSE_TIMEOUT)
            except TimeoutError:
                pass
        except BaseException as exc:
            hooks.record_failure(exc)
            raise
        finally:
            hooks.close()
    if hooks.signum is not None:
        raise ProxySignalExit(hooks.signum)

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

class _FileOutputWriter(_OutputWriter):
    """Regular files are not selectable pipes (including on kqueue).

    Buffer at most one frame; each router write is followed by an awaited drain.
    File I/O runs off-loop so a slow filesystem cannot stall signal cancellation.
    """
    def __init__(self, output: BinaryIO) -> None:
        super().__init__(output)
        self._pending = bytearray()
        self._inflight: asyncio.Task[Any] | None = None

    def write(self, data: bytes) -> None:
        if self.closed:
            raise BrokenPipeError
        if len(self._pending) + len(data) > MAX_FRAME_BYTES:
            raise ProxyError("output frame too large")
        self._pending.extend(data)

    async def drain(self) -> None:
        if self._inflight is not None:
            # Cancellation cannot stop a worker thread. Retain and join that
            # write on the next drain before closing or starting another write.
            await asyncio.shield(self._inflight)
            self._inflight = None
        data = bytes(self._pending)
        self._pending.clear()
        if data:
            self._inflight = asyncio.create_task(asyncio.to_thread(super().write, data))
            await asyncio.shield(self._inflight)
            self._inflight = None


async def open_stdio(output: BinaryIO) -> tuple[asyncio.StreamReader, Any, asyncio.BaseTransport]:
    loop = asyncio.get_running_loop(); reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    try:
        try:
            mode = os.fstat(output.fileno()).st_mode
        except (AttributeError, io.UnsupportedOperation):
            writer: Any = _OutputWriter(output)
        else:
            if stat.S_ISREG(mode):
                writer = _FileOutputWriter(output)
            else:
                output_protocol = asyncio.streams.FlowControlMixin(loop=loop)
                output_transport, _ = await loop.connect_write_pipe(lambda: output_protocol, output)
                writer = asyncio.StreamWriter(output_transport, output_protocol, None, loop)
    except BaseException:
        # Output acquisition can fail after input has registered its fd.
        transport.close()
        raise
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
