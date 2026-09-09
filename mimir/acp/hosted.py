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
from collections.abc import Awaitable, Callable

from .audit import safe_log_event
from .confinement import prepare_command, ConfinementUnavailable, BackendUnavailable
from .execution_scope import ExecutionScope, canonical_scope_path, MAX_SCOPE_REQUESTS, SCOPE_WARNING, UNCONFINED_WARNING

from .hands_contract import (
    HandsContractError,
    hands_v1_wire_descriptors,
    validate_tool_arguments,
    validate_tool_result,
)
from .python_kernel import PythonKernelManager, PythonKernelUnavailable


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
    timeout_seconds: int = SHELL_TIMEOUT_SECONDS
    scope: ExecutionScope = field(init=False)
    kernel_id: str = field(default_factory=lambda: secrets.token_urlsafe(18))

    def __post_init__(self) -> None:
        self.cwd = self.cwd.resolve()
        self.scope = ExecutionScope(self.cwd)


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
    def __init__(self, timeout_seconds: int = SHELL_TIMEOUT_SECONDS, *,
                 request_scope_permission: Callable[[str, str], Awaitable[bool]] | None = None,
                 request_unconfined_permission: Callable[[str], Awaitable[bool]] | None = None) -> None:
        self._request_scope_permission = request_scope_permission
        self._request_unconfined_permission = request_unconfined_permission
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 600
        ):
            raise ValueError("timeout_seconds must be an integer from 1 through 600")
        self._timeout_seconds = timeout_seconds
        self._sessions: dict[str, HostedSession] = {}
        self._connections: dict[str, _Connection] = {}
        self._used_connection_ids: set[str] = set()
        self._processes: dict[asyncio.subprocess.Process, int] = {}
        self._signalled_processes: set[asyncio.subprocess.Process] = set()
        self._provider_cancelled: set[asyncio.Task[Any]] = set()
        self._python_kernels = PythonKernelManager()
        self._retirements: set[asyncio.Task[None]] = set()
        self._closed = False

    def bind_session(self, session_id: str, cwd: str | os.PathLike[str]) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        self.revoke_session(session_id)
        self._sessions[session_id] = HostedSession(
            session_id, Path(os.path.abspath(cwd)), self._timeout_seconds
        )

    def revoke_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.scope.invalidate(close=True)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # No worker can be executing without its event loop.
            task = loop.create_task(self._python_kernels.retire(session.kernel_id))
            self._retirements.add(task)
            task.add_done_callback(self._retirements.discard)

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
        connection = self._connections.get(connection_id)
        if connection is None:
            raise HostedMcpError(-32602, "Unknown MCP connection")
        session_id = connection.session.session_id
        session_connections = tuple(
            item
            for item in self._connections.values()
            if item.session.session_id == session_id
        )
        await asyncio.gather(*(self._cancel_calls(item) for item in session_connections))
        old_scope = connection.session.scope
        old_scope.invalidate(close=True)
        await self._python_kernels.retire(connection.session.kernel_id)
        # Connection reset revokes grants, not final session rejection/budgets.
        connection.session.scope = ExecutionScope(
            connection.session.cwd, denied=set(old_scope.denied),
            attempts=old_scope.attempts, risk_requested=old_scope.risk_requested,
        )
        self._connections.pop(connection_id, None)
        return {}

    async def cancel_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.scope.invalidate()
        await asyncio.gather(
            *(
                self._cancel_calls(connection)
                for connection in tuple(self._connections.values())
                if connection.session.session_id == session_id
            )
        )
        if session is not None:
            await self._python_kernels.retire(session.kernel_id)

    async def execute_python(
        self,
        session: HostedSession,
        code: str,
    ) -> dict[str, Any]:
        try:
            scope = session.scope
            await self._ensure_execution_permission(session)
            async with scope.execution_lock:
                self._require_live_scope(session)
                if session.scope is not scope or scope.closed:
                    raise HostedMcpError(-32000, "Execution scope changed")
                return await self._python_kernels.execute(
                    session.kernel_id, session.cwd, code, session.timeout_seconds,
                    approved_paths=tuple(scope.approved),
                    allow_unconfined=scope.unconfined_approved,
                )
        except (PythonKernelUnavailable, ConfinementUnavailable) as exc:
            raise HostedMcpError(
                -32000, f"hands_python kernel unavailable: {exc}"
            ) from None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for session in self._sessions.values():
            session.scope.invalidate(close=True)
        connections = tuple(self._connections.values())
        self._connections.clear()
        await asyncio.gather(*(self._cancel_calls(item) for item in connections))
        await asyncio.gather(
            *(
                self._terminate_process(process, pgid)
                for process, pgid in tuple(self._processes.items())
            )
        )
        if self._retirements:
            await asyncio.gather(*tuple(self._retirements))
        await self._python_kernels.close()

    def terminate_owned_children(self) -> None:
        processes = tuple(self._processes.items())
        self.kill_owned_process_groups()
        for process, _ in processes:
            try:
                os.waitpid(process.pid, 0)
            except (ChildProcessError, ProcessLookupError):
                pass
            self._processes.pop(process, None)
        self._python_kernels.terminate_owned_children()

    def kill_owned_process_groups(self) -> None:
        for _, pgid in tuple(self._processes.items()):
            try:
                os.killpg(pgid, 9)
            except (ProcessLookupError, PermissionError):
                # Best-effort signal/atexit cleanup must continue to the other
                # owned groups, including Python kernels, after a denied group.
                pass
        self._python_kernels.kill_owned_process_groups()

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
        if name == "request_scope":
            return await self.request_scope(session, arguments["path"])
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
        if name == "python":
            return await self.execute_python(session, arguments["code"])
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

    def _require_live_scope(self, session: HostedSession) -> None:
        if self._closed or session.scope.closed:
            raise HostedMcpError(-32000, "Execution scope is closed")

    async def request_scope(self, session: HostedSession, value: str) -> dict[str, Any]:
        scope = session.scope
        self._require_live_scope(session)
        try:
            prepared = prepare_command(("/bin/true",), cwd=session.cwd,
                                       approved_paths=tuple(scope.approved),
                                       allow_unconfined=scope.unconfined_approved)
            mode_message = (UNCONFINED_WARNING + " This is the next-execution policy; existing children retain their launch policy."
                            if prepared.execution_mode == "unconfined"
                            else "CONFINED: Next execution uses confinement; runtime read-only allowances also apply. Existing children retain their launch policy.")
        except ConfinementUnavailable:
            mode_message = "BLOCKED: Next execution requires a working confinement backend or separate operator risk acceptance. Existing children retain their launch policy."
        if value == "":
            return {"approved": True, "paths": scope.paths(), "message": mode_message}
        if mode_message.startswith("UNCONFINED:"):
            await safe_log_event("acp_permission_outcome", wrapper_name="hands_request_scope",
                                 path="<unconfined>", outcome="denied", resource_resolvable=False)
            return {"approved": False, "paths": scope.paths(), "message": UNCONFINED_WARNING + " Path grants cannot constrain unconfined execution."}
        path: Path | None = None
        approved = False
        outcome = "denied"
        message = "Scope denied"
        try:
            path = canonical_scope_path(value, session.cwd)
            if scope.rejected(path):
                message = "This scope was rejected for this session; do not retry."
            elif scope.allows(path):
                approved = True
                outcome = "already_approved"
                message = "Scope already approved; Python state unchanged."
            elif scope.pending or scope.attempts >= MAX_SCOPE_REQUESTS:
                message = "Scope request limit reached; no approval submitted."
            elif self._request_scope_permission is None:
                message = "Scope approval unavailable: no operator permission channel."
            else:
                scope.pending = True
                scope.attempts += 1
                generation = scope.generation
                try:
                    # Validate backend before asking; no unconfined fallback.
                    prepare_command(("/bin/true",), cwd=session.cwd,
                                    approved_paths=(*scope.approved, path))
                    async with asyncio.timeout(60):
                        answer = await self._request_scope_permission(session.session_id, str(path))
                    async with scope.execution_lock:
                        self._require_live_scope(session)
                        if scope.closed or session.scope is not scope or generation != scope.generation:
                            message = "Scope approval expired; access unchanged."
                        elif answer is True:
                            await self._python_kernels.retire(session.kernel_id)
                            self._require_live_scope(session)
                            if not scope.closed and session.scope is scope and generation == scope.generation:
                                scope.approved.add(path)
                                approved = True
                                outcome = "approved"
                                message = SCOPE_WARNING
                        else:
                            scope.denied.add(path)
                            message = "Scope rejected for this session; do not retry."
                finally:
                    scope.pending = False
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except (ValueError, OSError, ConfinementUnavailable, TimeoutError) as exc:
            message = f"Scope unavailable: {exc}"
        finally:
            await safe_log_event("acp_permission_outcome", wrapper_name="hands_request_scope",
                                 path=str(path) if path is not None else "<invalid>",
                                 outcome=outcome, resource_resolvable=path is not None)
        return {"approved": approved, "paths": scope.paths(), "message": message}

    async def _ensure_execution_permission(self, session: HostedSession) -> None:
        """Ask only for a missing backend, never to bypass a policy/runtime error."""
        self._require_live_scope(session)
        scope = session.scope
        try:
            prepare_command(("/bin/true",), cwd=session.cwd,
                            approved_paths=tuple(scope.approved))
            return
        except BackendUnavailable:
            pass
        if scope.unconfined_approved:
            return
        if scope.risk_pending or scope.risk_requested:
            await safe_log_event("acp_permission_outcome", wrapper_name="hands_unconfined_execution",
                                 path="<unconfined>", outcome="denied", resource_resolvable=False)
            raise HostedMcpError(-32000, "Unconfined execution not approved; risk request is pending or final for this session")
        if self._request_unconfined_permission is None:
            await safe_log_event("acp_permission_outcome", wrapper_name="hands_unconfined_execution",
                                 path="<unconfined>", outcome="denied", resource_resolvable=False)
            raise HostedMcpError(-32000, "Confinement unavailable; no operator risk approval channel. Execution blocked")
        scope.risk_requested = True
        scope.risk_pending = True
        generation = scope.generation
        outcome = "denied"
        try:
            async with asyncio.timeout(60):
                answer = await self._request_unconfined_permission(session.session_id)
            async with scope.execution_lock:
                self._require_live_scope(session)
                task = asyncio.current_task()
                if (scope.closed or session.scope is not scope
                        or scope.generation != generation or answer is not True
                        or (task is not None and task.cancelling())):
                    raise HostedMcpError(-32000, "Unconfined execution rejected or risk approval expired for this session")
                # A newly available backend always wins; approval never bypasses
                # a backend that is available but has a malformed profile.
                try:
                    prepare_command(("/bin/true",), cwd=session.cwd,
                                    approved_paths=tuple(scope.approved))
                except BackendUnavailable:
                    await self._python_kernels.retire(session.kernel_id)
                    self._require_live_scope(session)
                    if scope.closed or session.scope is not scope or scope.generation != generation:
                        raise HostedMcpError(-32000, "Risk approval expired")
                    scope.unconfined_approved = True
                    outcome = "approved"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except TimeoutError:
            raise HostedMcpError(-32000, "Unconfined risk approval timed out; execution blocked") from None
        finally:
            scope.risk_pending = False
            await safe_log_event("acp_permission_outcome", wrapper_name="hands_unconfined_execution",
                                 path="<unconfined>", outcome=outcome, resource_resolvable=False)

    async def _shell(self, session: HostedSession, command: str) -> dict[str, Any]:
        scope = session.scope
        try:
            await self._ensure_execution_permission(session)
            async with scope.execution_lock:
                self._require_live_scope(session)
                if session.scope is not scope or scope.closed:
                    raise HostedMcpError(-32000, "Execution scope changed")
                return await self._confined_shell(session, command)
        except ConfinementUnavailable as exc:
            raise HostedMcpError(-32000, f"hands_shell unavailable: {exc}") from None

    async def _confined_shell(self, session: HostedSession, command: str) -> dict[str, Any]:
        timeout = session.timeout_seconds
        startup_warning = ""
        try:
            prepared = prepare_command(("/bin/sh", "-c", command), cwd=session.cwd,
                                       approved_paths=tuple(session.scope.approved),
                                       allow_unconfined=session.scope.unconfined_approved)
            if prepared.execution_mode == "unconfined":
                startup_warning = UNCONFINED_WARNING + " "
            process = await asyncio.create_subprocess_exec(
                *prepared.argv,
                cwd=session.cwd,
                env=prepared.env,
                start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ConfinementUnavailable) as exc:
            raise HostedMcpError(
                -32000, f"{startup_warning}hands_shell unavailable: {exc}"
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
            # A successful shell may leave background children in its group.
            await self._terminate_process(process, pgid)
            await self._finish_readers(readers)
        stderr_text = self._format_output(stderr_capture)
        if prepared.execution_mode == "unconfined":
            stderr_text = UNCONFINED_WARNING + "\n" + stderr_text
        if prepared.execution_mode == "confined" and process.returncode and any(text in stderr_text.lower() for text in ("permission denied", "operation not permitted")):
            stderr_text += "\n[Possible confinement refusal. Use hands_request_scope for the path shown above; empty path lists approved scopes.]"
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
        if process not in self._signalled_processes:
            try:
                os.killpg(pgid, 9)
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.returncode is None:
                    raise
            # Retain successful signaling across cancellation of process.wait().
            # macOS can deny a second killpg while the killed leader has not yet
            # been reaped and asyncio still reports returncode=None.
            self._signalled_processes.add(process)
        try:
            await process.wait()
        except ProcessLookupError:
            pass
        self._processes.pop(process, None)
        self._signalled_processes.discard(process)

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
