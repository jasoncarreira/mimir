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

class ProxyError(RuntimeError):
    pass

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
        if not isinstance(message["method"], str):
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
    server_id: str
    session_id: str | None


class ProxyRouter:
    def __init__(self, client_writer: Any, daemon_writer: Any, credential: str) -> None:
        self._client = FrameWriter(client_writer, credential, inject_credential=False)
        self._daemon = FrameWriter(daemon_writer, credential)
        self._provider = HostedHandsProvider()
        self._client_requests: dict[tuple[type[Any], Any], _PendingSession | None] = {}
        self._daemon_requests: dict[tuple[type[Any], Any], None] = {}
        self._local_requests: dict[tuple[type[Any], Any], asyncio.Task[None] | None] = {}
        self._local_sessions: dict[tuple[type[Any], Any], str] = {}
        self._daemon_tombstones: set[tuple[type[Any], Any]] = set()
        self._server_sessions: dict[str, str] = {}
        self._connection_sessions: dict[str, str] = {}
        self._used_server_ids: set[str] = set()
        self._client_lock = asyncio.Lock()
        self._daemon_lock = asyncio.Lock()
        self._closed = False

    async def route_client(self, message: dict[str, Any]) -> None:
        kind = _message_kind(message)
        if kind == "response":
            key = _request_key(message["id"])
            if key in self._daemon_tombstones:
                return
            if key not in self._daemon_requests:
                raise ProxyError("unsolicited response")
            self._daemon_requests.pop(key)
            await self._write_daemon(message)
            return
        if kind == "notification":
            await self._client_notification(message)
            await self._write_daemon(message)
            return
        key = _request_key(message["id"])
        self._register(self._client_requests, key)
        pending = await self._prepare_session(message)
        self._client_requests[key] = pending
        await self._write_daemon(message)

    async def route_daemon(self, message: dict[str, Any]) -> None:
        kind = _message_kind(message)
        if kind == "response":
            key = _request_key(message["id"])
            if key not in self._client_requests:
                raise ProxyError("unsolicited response")
            pending = self._client_requests.pop(key)
            if pending is not None:
                await self._finish_session(pending, message)
            await self._write_client(message)
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
        await self._write_client(message)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = tuple(task for task in self._local_requests.values() if task is not None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._local_requests.clear()
        self._local_sessions.clear()
        await self._provider.close()
        self._client_requests.clear()
        self._daemon_requests.clear()
        self._server_sessions.clear()
        self._connection_sessions.clear()

    def _register(
        self,
        requests: dict[tuple[type[Any], Any], _PendingSession | None],
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

    async def _prepare_session(self, message: dict[str, Any]) -> _PendingSession | None:
        method = message["method"]
        if method not in {"session/new", "session/load"}:
            return None
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("cwd"), str):
            raise ProxyError("invalid frame")
        servers = params.get("mcpServers")
        if "mcpServers" in params and servers != []:
            return None
        server_id = self._new_server_id()
        params["mcpServers"] = [{
            "type": "acp",
            "name": "mimir-hands",
            "serverId": server_id,
        }]
        session_id = params.get("sessionId") if method == "session/load" else None
        if method == "session/load":
            if not isinstance(session_id, str) or not session_id:
                raise ProxyError("invalid frame")
            await self._retire_session(session_id)
        self._provider.bind_session(server_id, params["cwd"])
        self._server_sessions[server_id] = server_id
        return _PendingSession(method, params["cwd"], server_id, session_id)

    def _new_server_id(self) -> str:
        while True:
            token = secrets.token_urlsafe(18)
            if len(token) == 24 and token not in self._used_server_ids:
                self._used_server_ids.add(token)
                return f"mimir-hosted:{token}"

    async def _finish_session(
        self, pending: _PendingSession, response: dict[str, Any]
    ) -> None:
        if "error" in response:
            await self._retire_session(pending.server_id)
            return
        result = response.get("result")
        if not isinstance(result, dict):
            raise ProxyError("invalid frame")
        if pending.method == "session/new":
            session_id = result.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise ProxyError("invalid frame")
            await self._retire_session(session_id)
        else:
            session_id = pending.session_id
            if session_id is None:
                raise ProxyError("invalid frame")
        self._server_sessions[pending.server_id] = session_id
        for connection_id, owner in tuple(self._connection_sessions.items()):
            if owner == pending.server_id:
                self._connection_sessions[connection_id] = session_id
        self._provider.revoke_session(pending.server_id)

    async def _retire_session(self, session_id: str) -> None:
        for connection_id, owner in tuple(self._connection_sessions.items()):
            if owner == session_id:
                try:
                    await self._provider.disconnect(connection_id)
                except HostedMcpError:
                    pass
                self._connection_sessions.pop(connection_id, None)
        for server_id, owner in tuple(self._server_sessions.items()):
            if owner == session_id:
                self._server_sessions.pop(server_id, None)
        await self._provider.cancel_session(session_id)
        self._provider.revoke_session(session_id)

    async def _client_notification(self, message: dict[str, Any]) -> None:
        if message["method"] not in {"session/cancel", "session/cancellation"}:
            return
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("sessionId"), str):
            raise ProxyError("invalid frame")
        session_id = params["sessionId"]
        for key, owner in tuple(self._local_sessions.items()):
            if owner == session_id:
                task = self._local_requests.get(key)
                if task is not None:
                    self._tombstone(key)
                    task.cancel()
        await self._provider.cancel_session(session_id)

    async def _route_hosted(
        self, message: dict[str, Any], kind: str, method: str, params: Any
    ) -> bool:
        if not isinstance(params, dict):
            return False
        if method == "mcp/connect":
            server_id = params.get("serverId")
            if server_id not in self._server_sessions:
                return False
            if set(params) != {"serverId"}:
                raise ProxyError("invalid frame")
            if kind != "request":
                raise ProxyError("invalid frame")
            key = _request_key(message["id"])
            self._register_local(key)
            session_id = self._server_sessions[server_id]
            try:
                connection_id = self._provider.connect(session_id)
                self._connection_sessions[connection_id] = session_id
                await self._complete_local(key, {"connectionId": connection_id})
            except HostedMcpError as exc:
                await self._fail_local(key, exc)
            return True
        connection_id = params.get("connectionId")
        if connection_id not in self._connection_sessions:
            return False
        if method == "mcp/disconnect":
            if set(params) != {"connectionId"}:
                raise ProxyError("invalid frame")
            if kind != "request":
                raise ProxyError("invalid frame")
            key = _request_key(message["id"])
            self._register_local(key)
            try:
                result = await self._provider.disconnect(connection_id)
                self._connection_sessions.pop(connection_id, None)
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
            result = await self._provider.request(
                connection_id, method, params, request_id=key[1]
            )
            await self._complete_local(key, result)
        except HostedMcpError as exc:
            await self._fail_local(key, exc)
        except asyncio.CancelledError:
            if key not in self._daemon_tombstones:
                raise
        finally:
            self._local_requests.pop(key, None)
            self._local_sessions.pop(key, None)

    async def _complete_local(self, key: tuple[type[Any], Any], result: Any) -> None:
        self._local_requests.pop(key, None)
        self._local_sessions.pop(key, None)
        if key not in self._daemon_tombstones:
            await self._write_daemon({"jsonrpc": "2.0", "id": key[1], "result": result})

    async def _fail_local(
        self, key: tuple[type[Any], Any], error: HostedMcpError
    ) -> None:
        self._local_requests.pop(key, None)
        self._local_sessions.pop(key, None)
        if key not in self._daemon_tombstones:
            await self._write_daemon({"jsonrpc": "2.0", "id": key[1], "error": error.as_error()})

    def _tombstone(self, key: tuple[type[Any], Any]) -> None:
        if len(self._daemon_tombstones) >= MAX_OUTSTANDING_REQUESTS:
            raise ProxyError("too many cancelled requests")
        self._daemon_tombstones.add(key)

    async def _write_client(self, message: dict[str, Any]) -> None:
        async with self._client_lock:
            self._client.write_message(message)
            await self._client.drain()

    async def _write_daemon(self, message: dict[str, Any]) -> None:
        async with self._daemon_lock:
            self._daemon.write_message(message)
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
            await route(message)


async def run_router(
    client_reader: Any,
    client_writer: Any,
    daemon_reader: Any,
    daemon_writer: Any,
    credential: str,
    *,
    close_on_daemon_exit: bool = False,
) -> None:
    router = ProxyRouter(client_writer, daemon_writer, credential)
    client_task = asyncio.create_task(_route_stream(client_reader, router.route_client))
    daemon_task = asyncio.create_task(_route_stream(daemon_reader, router.route_daemon))
    tasks = {client_task, daemon_task}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        await asyncio.gather(*done)
        if pending and not (close_on_daemon_exit and daemon_task in done):
            _, pending = await asyncio.wait(pending, timeout=PEER_EOF_GRACE_TIMEOUT)
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
            stdin_reader, stdout_writer, upstream_reader, upstream_writer, credential
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
