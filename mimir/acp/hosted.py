from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .hands_contract import (
    HandsContractError,
    hands_v1_wire_descriptors,
    validate_tool_arguments,
    validate_tool_result,
)


READ_LIMIT_BYTES = 1024 * 1024
FRAME_LIMIT_BYTES = 1024 * 1024
OUTPUT_LIMIT_BYTES = 256 * 1024
SHELL_TIMEOUT_SECONDS = 60


class HostedMcpError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def as_error(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass(slots=True)
class HostedSession:
    session_id: str
    cwd: Path


@dataclass(slots=True)
class _Connection:
    session: HostedSession
    state: str = "connected"
    calls: dict[tuple[type[Any], Any], asyncio.Task[Any]] = field(default_factory=dict)


@dataclass(slots=True)
class _OutputCapture:
    retained: bytearray = field(default_factory=bytearray)
    total: int = 0


def _request_key(value: Any) -> tuple[type[Any], Any]:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise HostedMcpError(-32602, "Invalid params")
    return type(value), value


def _invalid_params() -> HostedMcpError:
    return HostedMcpError(-32602, "Invalid params")


def _not_initialized() -> HostedMcpError:
    return HostedMcpError(-32600, "Hosted MCP connection is not initialized")


def _resolved_path(session: HostedSession, value: str) -> Path:
    if os.path.isabs(value):
        return Path(value)
    return Path(os.path.abspath(os.path.join(session.cwd, value)))


class HostedHandsProvider:
    def __init__(self) -> None:
        self._sessions: dict[str, HostedSession] = {}
        self._connections: dict[str, _Connection] = {}
        self._used_connection_ids: set[str] = set()
        self._processes: dict[asyncio.subprocess.Process, int] = {}
        self._provider_cancelled: set[asyncio.Task[Any]] = set()
        self._closed = False

    def bind_session(self, session_id: str, cwd: str | os.PathLike[str]) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        self._sessions[session_id] = HostedSession(session_id, Path(os.path.abspath(cwd)))

    def revoke_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def connect(
        self,
        session_id: str,
        cwd: str | os.PathLike[str] | None = None,
    ) -> str:
        if self._closed:
            raise HostedMcpError(-32000, "Hosted MCP provider is closed")
        if cwd is not None:
            self.bind_session(session_id, cwd)
        session = self._sessions.get(session_id)
        if session is None:
            raise HostedMcpError(-32602, "Unknown hosted session")
        while True:
            token = secrets.token_urlsafe(18)
            if len(token) == 24 and token not in self._used_connection_ids:
                break
        self._used_connection_ids.add(token)
        connection_id = f"mimir-hosted-connection:{token}"
        self._connections[connection_id] = _Connection(session)
        return connection_id

    async def disconnect(self, connection_id: str) -> dict[str, Any]:
        connection = self._connections.pop(connection_id, None)
        if connection is None:
            raise HostedMcpError(-32602, "Unknown MCP connection")
        await self._cancel_calls(connection)
        return {}

    async def cancel_session(self, session_id: str) -> None:
        await asyncio.gather(
            *(
                self._cancel_calls(connection)
                for connection in tuple(self._connections.values())
                if connection.session.session_id == session_id
            )
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        connections = tuple(self._connections.values())
        self._connections.clear()
        await asyncio.gather(*(self._cancel_calls(item) for item in connections))
        await asyncio.gather(
            *(
                self._terminate_process(process, pgid)
                for process, pgid in tuple(self._processes.items())
            )
        )

    async def request(
        self,
        connection_id: str,
        method: str,
        params: Any = None,
        *,
        request_id: str | int | None = None,
    ) -> dict[str, Any]:
        connection = self._connection(connection_id)
        if method == "initialize":
            return self._initialize(connection, params)
        if method == "tools/list":
            self._require_initialized(connection)
            if params is not None and (not isinstance(params, dict) or params):
                raise _invalid_params()
            return {"tools": hands_v1_wire_descriptors()}
        if method != "tools/call":
            raise HostedMcpError(-32601, "Method not found")
        self._require_initialized(connection)
        key = _request_key(request_id) if request_id is not None else None
        task = asyncio.current_task()
        if key is not None:
            if key in connection.calls:
                raise HostedMcpError(-32600, "Duplicate request ID")
            if task is None:
                raise HostedMcpError(-32603, "Internal error")
            connection.calls[key] = task
        try:
            result = await self._call(connection.session, params)
            response = {"content": [], "structuredContent": result}
            validate_tool_result(params["name"], result)
            self._check_frame_size(request_id, response)
            return response
        except asyncio.CancelledError:
            if task in self._provider_cancelled:
                self._provider_cancelled.discard(task)
                raise HostedMcpError(-32800, "Request cancelled") from None
            raise
        except HandsContractError:
            raise HostedMcpError(-32603, "Internal error") from None
        finally:
            if key is not None and connection.calls.get(key) is task:
                connection.calls.pop(key, None)

    message = request
    handle_request = request

    async def notification(
        self, connection_id: str, method: str, params: Any = None
    ) -> None:
        connection = self._connection(connection_id)
        if method == "notifications/initialized":
            if connection.state != "initializing" or params not in (None, {}):
                raise _not_initialized()
            connection.state = "initialized"
            return
        if method == "notifications/cancelled":
            if not isinstance(params, dict) or set(params) != {"requestId"}:
                raise _invalid_params()
            task = connection.calls.get(_request_key(params["requestId"]))
            if task is not None:
                self._provider_cancelled.add(task)
                task.cancel()
            return
        raise HostedMcpError(-32601, "Method not found")

    notify = notification
    handle_notification = notification

    def _connection(self, connection_id: str) -> _Connection:
        connection = self._connections.get(connection_id)
        if connection is None:
            raise HostedMcpError(-32602, "Unknown MCP connection")
        return connection

    def _initialize(self, connection: _Connection, params: Any) -> dict[str, Any]:
        if connection.state != "connected":
            raise _not_initialized()
        if not isinstance(params, dict) or set(params) != {
            "protocolVersion",
            "capabilities",
            "clientInfo",
        }:
            raise _invalid_params()
        protocol_version = params["protocolVersion"]
        client_info = params["clientInfo"]
        if (
            not isinstance(protocol_version, str)
            or not protocol_version
            or not isinstance(params["capabilities"], dict)
            or not isinstance(client_info, dict)
            or set(client_info) != {"name", "version"}
            or not all(isinstance(client_info[key], str) for key in client_info)
        ):
            raise _invalid_params()
        connection.state = "initializing"
        return {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mimir-hands", "version": "1"},
        }

    def _require_initialized(self, connection: _Connection) -> None:
        if connection.state != "initialized":
            raise _not_initialized()

    async def _call(self, session: HostedSession, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict) or set(params) - {"name", "arguments", "_meta"}:
            raise _invalid_params()
        if not {"name", "arguments"} <= set(params) or not isinstance(
            params["name"], str
        ):
            raise _invalid_params()
        if "_meta" in params:
            metadata = params["_meta"]
            if not isinstance(metadata, dict) or set(metadata) != {"progressToken"}:
                raise _invalid_params()
            token = metadata["progressToken"]
            if isinstance(token, bool) or not isinstance(token, (str, int)):
                raise _invalid_params()
        name = params["name"]
        try:
            arguments = validate_tool_arguments(name, params["arguments"])
        except HandsContractError:
            raise _invalid_params() from None
        if name == "read":
            return await asyncio.to_thread(self._read, session, arguments["path"])
        if name == "edit":
            return await asyncio.to_thread(
                self._edit,
                session,
                arguments["path"],
                arguments["oldText"],
                arguments["newText"],
            )
        if name == "shell":
            return await self._shell(session, arguments["command"])
        raise _invalid_params()

    def _read(self, session: HostedSession, path_value: str) -> dict[str, Any]:
        path = Path(os.path.realpath(_resolved_path(session, path_value)))
        try:
            with path.open("rb") as stream:
                content = stream.read(READ_LIMIT_BYTES + 1)
        except OSError as exc:
            raise HostedMcpError(-32000, f"hands_read failed: {exc}") from None
        if len(content) > READ_LIMIT_BYTES:
            try:
                size = path.stat().st_size
            except OSError:
                size = len(content)
            raise HostedMcpError(-32000, f"file too large ({size} bytes)")
        return {"content": content.decode("utf-8", errors="replace")}

    def _edit(
        self,
        session: HostedSession,
        path_value: str,
        old_text: str,
        new_text: str,
    ) -> dict[str, Any]:
        path = Path(os.path.realpath(_resolved_path(session, path_value)))
        temporary: str | None = None
        try:
            original = path.read_bytes()
            old = old_text.encode("utf-8")
            new = new_text.encode("utf-8")
            count = original.count(old)
            if count != 1:
                raise HostedMcpError(
                    -32000, f"edit mismatch: oldText occurs {count} times"
                )
            replacement = original.replace(old, new, 1)
            if replacement == original:
                return {"changed": False}
            mode = stat.S_IMODE(path.stat().st_mode)
            descriptor, temporary = tempfile.mkstemp(dir=path.parent)
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            return {"changed": True}
        except HostedMcpError:
            raise
        except (OSError, UnicodeError) as exc:
            raise HostedMcpError(-32000, f"hands_edit failed: {exc}") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    async def _shell(self, session: HostedSession, command: str) -> dict[str, Any]:
        timeout = SHELL_TIMEOUT_SECONDS
        try:
            process = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                command,
                cwd=session.cwd,
                env=None,
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise HostedMcpError(
                -32000, f"hands_shell unavailable: {exc}"
            ) from None
        pgid = process.pid
        self._processes[process] = pgid
        stdout_capture = _OutputCapture()
        stderr_capture = _OutputCapture()
        stdout_task = asyncio.create_task(
            self._drain_output(process.stdout, stdout_capture)
        )
        stderr_task = asyncio.create_task(
            self._drain_output(process.stderr, stderr_capture)
        )
        readers = (stdout_task, stderr_task)
        deadline = asyncio.get_running_loop().time() + timeout
        timed_out = False
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    await process.wait()
                    await asyncio.gather(*readers)
            except TimeoutError:
                timed_out = True
                await self._terminate_process(process, pgid)
                await self._finish_readers(readers)
        except BaseException:
            await self._terminate_process(process, pgid)
            await self._finish_readers(readers)
            raise
        finally:
            self._processes.pop(process, None)
        stderr_text = self._format_output(stderr_capture)
        if timed_out:
            stderr_text += f"\n[timed out after {timeout} s]"
        return {
            "stdout": self._format_output(stdout_capture),
            "stderr": stderr_text,
            "exitCode": -1 if timed_out else process.returncode,
        }

    async def _drain_output(
        self, stream: asyncio.StreamReader, capture: _OutputCapture
    ) -> None:
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            capture.total += len(chunk)
            if len(capture.retained) < OUTPUT_LIMIT_BYTES:
                capture.retained.extend(
                    chunk[: OUTPUT_LIMIT_BYTES - len(capture.retained)]
                )

    def _format_output(self, capture: _OutputCapture) -> str:
        result = capture.retained.decode("utf-8", errors="replace")
        omitted = capture.total - len(capture.retained)
        if omitted:
            result += f"\n…[truncated {omitted} bytes]"
        return result

    async def _finish_readers(
        self, readers: tuple[asyncio.Task[None], asyncio.Task[None]]
    ) -> None:
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)

    async def _terminate_process(
        self, process: asyncio.subprocess.Process, pgid: int
    ) -> None:
        try:
            os.killpg(pgid, 9)
        except ProcessLookupError:
            pass
        try:
            await process.wait()
        except ProcessLookupError:
            pass
        self._processes.pop(process, None)

    async def _cancel_calls(self, connection: _Connection) -> None:
        tasks = tuple(connection.calls.values())
        for task in tasks:
            self._provider_cancelled.add(task)
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _check_frame_size(
        self, request_id: str | int | None, result: dict[str, Any]
    ) -> None:
        envelope = {"jsonrpc": "2.0", "id": request_id, "result": result}
        encoded = json.dumps(
            envelope, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        if len(encoded) > FRAME_LIMIT_BYTES:
            raise HostedMcpError(
                -32000, "hosted tool result exceeds JSON-RPC frame limit"
            )


__all__ = [
    "FRAME_LIMIT_BYTES",
    "HostedHandsProvider",
    "HostedMcpError",
    "HostedSession",
    "OUTPUT_LIMIT_BYTES",
    "READ_LIMIT_BYTES",
    "SHELL_TIMEOUT_SECONDS",
]
