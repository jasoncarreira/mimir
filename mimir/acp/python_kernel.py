from __future__ import annotations

import argparse
import ast
import asyncio
import builtins
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Coroutine


STREAM_LIMIT_BYTES = 65_536
TEXT_LIMIT_BYTES = 16_384
IDLE_SECONDS = 1_800
_FRAME_LIMIT_BYTES = 16 * 1024 * 1024
_FILENAME = "<mimir-hands-python>"
_REAP_TIMEOUT_SECONDS = 5


class PythonKernelUnavailable(RuntimeError):
    pass


def _bounded_text(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    retained = encoded[:limit]
    while retained:
        try:
            result = retained.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            retained = retained[: exc.start]
    else:
        result = ""
    omitted = len(encoded) - len(retained)
    if omitted:
        result += f"\n…[truncated {omitted} bytes]"
    return result


def _bounded_file(path: Path, limit: int) -> str:
    with path.open("rb") as stream:
        retained = stream.read(limit)
        stream.seek(0, os.SEEK_END)
        total = stream.tell()
    result = retained.decode("utf-8", errors="replace")
    omitted = total - len(retained)
    if omitted:
        result += f"\n…[truncated {omitted} bytes]"
    return result


def _read_exact(stream: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.recv(size - len(chunks))
        if not chunk:
            raise EOFError("kernel control channel closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_frame(stream: socket.socket) -> dict[str, Any]:
    length = struct.unpack("!I", _read_exact(stream, 4))[0]
    if length > _FRAME_LIMIT_BYTES:
        raise ValueError("kernel control frame is too large")
    value = json.loads(_read_exact(stream, length))
    if not isinstance(value, dict):
        raise ValueError("kernel control frame is not an object")
    return value


def _write_frame(stream: socket.socket, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > _FRAME_LIMIT_BYTES:
        raise ValueError("kernel control frame is too large")
    stream.sendall(struct.pack("!I", len(payload)) + payload)


def _execute(code: str, namespace: dict[str, Any]) -> tuple[bool, str, str]:
    try:
        tree = ast.parse(code, filename=_FILENAME, mode="exec")
        value = ""
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            statements = ast.Module(body=tree.body[:-1], type_ignores=tree.type_ignores)
            if statements.body:
                exec(compile(statements, _FILENAME, "exec"), namespace, namespace)
            expression = ast.Expression(tree.body[-1].value)
            value = repr(eval(compile(expression, _FILENAME, "eval"), namespace, namespace))
        else:
            exec(compile(tree, _FILENAME, "exec"), namespace, namespace)
        return True, _bounded_text(value, TEXT_LIMIT_BYTES), ""
    except BaseException:
        exception = traceback.format_exc()
        return False, "", _bounded_text(exception, TEXT_LIMIT_BYTES)


def worker(control_fd: int) -> None:
    channel = socket.socket(fileno=control_fd)
    baseline = os.open(os.devnull, os.O_WRONLY)
    os.dup2(baseline, 1)
    os.dup2(baseline, 2)
    os.close(baseline)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(write_through=True)
    namespace: dict[str, Any] = {"__name__": "__main__", "__builtins__": builtins}
    _write_frame(channel, {"ready": True})
    while True:
        try:
            request = _read_frame(channel)
        except EOFError:
            return
        code = request.get("code")
        stdout_path = request.get("stdout")
        stderr_path = request.get("stderr")
        if not all(isinstance(item, str) for item in (code, stdout_path, stderr_path)):
            raise ValueError("invalid kernel execution request")
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        try:
            with open(stdout_path, "ab", buffering=0) as stdout_stream, open(
                stderr_path, "ab", buffering=0
            ) as stderr_stream:
                os.dup2(stdout_stream.fileno(), 1)
                os.dup2(stderr_stream.fileno(), 2)
                ok, value, exception = _execute(code, namespace)
                try:
                    sys.stdout.flush()
                    sys.stderr.flush()
                except BaseException:
                    pass
        finally:
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)
        _write_frame(channel, {"ok": ok, "value": value, "exception": exception})


@dataclass(slots=True)
class _Worker:
    process: asyncio.subprocess.Process
    pgid: int
    channel: socket.socket
    usable: bool = False


@dataclass(slots=True)
class _Session:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiters: int = 0
    worker: _Worker | None = None
    last_activity: float = 0.0
    idle_task: asyncio.Task[None] | None = None


class PythonKernelManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._processes: dict[asyncio.subprocess.Process, int] = {}
        self._directory: Path | None = None
        self._closed = False

    async def execute(
        self,
        session_id: str,
        cwd: str | os.PathLike[str],
        code: str,
        timeout: int | float = 60,
    ) -> dict[str, Any]:
        if self._closed:
            raise PythonKernelUnavailable("kernel manager is closed")
        state = self._sessions.setdefault(session_id, _Session())
        state.waiters += 1
        try:
            await state.lock.acquire()
        except BaseException:
            state.waiters -= 1
            raise
        state.waiters -= 1
        self._cancel_idle(state)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        stdout_path: Path | None = None
        stderr_path: Path | None = None
        kernel_state = "fresh"
        try:
            if state.worker is not None:
                if state.worker.process.returncode is not None:
                    result = await self._crashed(state, state.worker, None, None)
                    state.last_activity = loop.time()
                    return result
                kernel_state = "reused"
            else:
                state.worker = await self._spawn(cwd, deadline)
            try:
                stdout_path = self._output_path()
                stderr_path = self._output_path()
            except OSError as exc:
                await self._discard(state, state.worker)
                raise PythonKernelUnavailable(str(exc)) from None
            request = {
                "code": code,
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            }
            try:
                await self._send(state.worker.channel, request, deadline)
                response = await self._response(state.worker, deadline)
            except TimeoutError:
                await self._discard(state, state.worker)
                result = self._timeout_result(timeout, stdout_path, stderr_path)
                state.last_activity = loop.time()
                return result
            except asyncio.CancelledError:
                await self._discard(state, state.worker)
                raise
            except _ProcessExited as exc:
                result = await self._crashed(
                    state,
                    state.worker,
                    stdout_path,
                    stderr_path,
                    returncode=exc.returncode,
                )
                state.last_activity = loop.time()
                return result
            except EOFError:
                result = await self._crashed(
                    state, state.worker, stdout_path, stderr_path
                )
                state.last_activity = loop.time()
                return result
            except OSError:
                result = await self._crashed(
                    state, state.worker, stdout_path, stderr_path
                )
                state.last_activity = loop.time()
                return result
            except (ValueError, json.JSONDecodeError) as exc:
                await self._discard(state, state.worker)
                raise PythonKernelUnavailable(str(exc)) from None
            if set(response) != {"ok", "value", "exception"}:
                await self._discard(state, state.worker)
                raise PythonKernelUnavailable("invalid kernel response")
            if (
                type(response["ok"]) is not bool
                or not isinstance(response["value"], str)
                or not isinstance(response["exception"], str)
            ):
                await self._discard(state, state.worker)
                raise PythonKernelUnavailable("invalid kernel response")
            result = {
                "ok": response["ok"],
                "stdout": _bounded_file(stdout_path, STREAM_LIMIT_BYTES),
                "stderr": _bounded_file(stderr_path, STREAM_LIMIT_BYTES),
                "value": response["value"],
                "exception": response["exception"],
                "timedOut": False,
                "kernel": kernel_state,
            }
            state.last_activity = loop.time()
            return result
        except TimeoutError:
            if state.worker is not None:
                await self._discard(state, state.worker)
            result = self._timeout_result(timeout, stdout_path, stderr_path)
            state.last_activity = loop.time()
            return result
        except OSError as exc:
            if state.worker is not None:
                await self._discard(state, state.worker)
            raise PythonKernelUnavailable(str(exc)) from None
        except asyncio.CancelledError:
            if state.worker is not None:
                await self._discard(state, state.worker)
            raise
        finally:
            for path in (stdout_path, stderr_path):
                if path is not None:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            state.lock.release()
            if state.worker is not None and state.waiters == 0:
                self._arm_idle(session_id, state, state.worker)

    async def retire(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is None:
            return
        self._cancel_idle(state)
        async with state.lock:
            if state.worker is not None:
                await self._discard(state, state.worker)
        if state.waiters == 0:
            self._sessions.pop(session_id, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(*(self.retire(key) for key in tuple(self._sessions)))
        if self._directory is not None:
            await asyncio.to_thread(shutil.rmtree, self._directory, True)
            self._directory = None

    async def _spawn(self, cwd: str | os.PathLike[str], deadline: float) -> _Worker:
        parent, child = socket.socketpair()
        parent.setblocking(False)
        try:
            process = await self._before_deadline(
                deadline,
                asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "mimir.acp.python_kernel",
                    "--control-fd",
                    str(child.fileno()),
                    pass_fds=(child.fileno(),),
                    cwd=cwd,
                    env=None,
                    start_new_session=True,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                ),
            )
        except (TimeoutError, asyncio.CancelledError):
            parent.close()
            child.close()
            raise
        except Exception as exc:
            parent.close()
            child.close()
            raise PythonKernelUnavailable(str(exc)) from None
        child.close()
        worker_state = _Worker(process, process.pid, parent)
        self._processes[process] = process.pid
        try:
            handshake = await self._response(worker_state, deadline, handshake=True)
            if handshake != {"ready": True}:
                raise PythonKernelUnavailable("invalid kernel handshake")
            worker_state.usable = True
            return worker_state
        except TimeoutError:
            await self._terminate(worker_state)
            raise
        except _ProcessExited as exc:
            await self._terminate(worker_state)
            raise PythonKernelUnavailable(
                f"kernel process exited with code {exc.returncode}"
            ) from None
        except asyncio.CancelledError:
            await self._terminate(worker_state)
            raise
        except Exception as exc:
            await self._terminate(worker_state)
            if isinstance(exc, PythonKernelUnavailable):
                raise
            raise PythonKernelUnavailable(str(exc)) from None

    async def _response(
        self, worker_state: _Worker, deadline: float, *, handshake: bool = False
    ) -> dict[str, Any]:
        read_task = asyncio.create_task(self._receive(worker_state.channel))
        exit_task = asyncio.create_task(worker_state.process.wait())
        try:
            async with asyncio.timeout_at(deadline):
                done, _ = await asyncio.wait(
                    (read_task, exit_task), return_when=asyncio.FIRST_COMPLETED
                )
                if read_task in done:
                    return read_task.result()
                raise _ProcessExited(exit_task.result())
        finally:
            for task in (read_task, exit_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(read_task, exit_task, return_exceptions=True)

    async def _receive(self, channel: socket.socket) -> dict[str, Any]:
        header = await self._socket_exact(channel, 4)
        length = struct.unpack("!I", header)[0]
        if length > _FRAME_LIMIT_BYTES:
            raise ValueError("kernel control frame is too large")
        value = json.loads(await self._socket_exact(channel, length))
        if not isinstance(value, dict):
            raise ValueError("kernel control frame is not an object")
        return value

    async def _send(
        self, channel: socket.socket, value: dict[str, Any], deadline: float
    ) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(payload) > _FRAME_LIMIT_BYTES:
            raise ValueError("kernel control frame is too large")
        await self._before_deadline(
            deadline,
            asyncio.get_running_loop().sock_sendall(
                channel, struct.pack("!I", len(payload)) + payload
            ),
        )

    async def _socket_exact(self, channel: socket.socket, size: int) -> bytes:
        chunks = bytearray()
        loop = asyncio.get_running_loop()
        while len(chunks) < size:
            chunk = await loop.sock_recv(channel, size - len(chunks))
            if not chunk:
                raise EOFError("kernel control channel closed")
            chunks.extend(chunk)
        return bytes(chunks)

    async def _before_deadline(
        self, deadline: float, awaitable: Coroutine[Any, Any, Any]
    ) -> Any:
        async with asyncio.timeout_at(deadline):
            return await awaitable

    def _output_path(self) -> Path:
        if self._directory is None:
            self._directory = Path(tempfile.mkdtemp(prefix="mimir-python-kernel-"))
            self._directory.chmod(0o700)
        descriptor, value = tempfile.mkstemp(dir=self._directory)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        return Path(value)

    def _timeout_result(
        self,
        timeout: int | float,
        stdout_path: Path | None,
        stderr_path: Path | None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "stdout": _bounded_file(stdout_path, STREAM_LIMIT_BYTES)
            if stdout_path is not None
            else "",
            "stderr": _bounded_file(stderr_path, STREAM_LIMIT_BYTES)
            if stderr_path is not None
            else "",
            "value": "",
            "exception": f"execution timed out after {timeout} seconds; namespace state lost",
            "timedOut": True,
            "kernel": "timed_out",
        }

    async def _crashed(
        self,
        state: _Session,
        worker_state: _Worker,
        stdout_path: Path | None,
        stderr_path: Path | None,
        *,
        returncode: int | None = None,
    ) -> dict[str, Any]:
        await self._discard(state, worker_state)
        if returncode is None:
            returncode = worker_state.process.returncode
        if returncode is None:
            returncode = -9
        return {
            "ok": False,
            "stdout": _bounded_file(stdout_path, STREAM_LIMIT_BYTES)
            if stdout_path is not None
            else "",
            "stderr": _bounded_file(stderr_path, STREAM_LIMIT_BYTES)
            if stderr_path is not None
            else "",
            "value": "",
            "exception": f"kernel process exited with code {returncode}; namespace state lost",
            "timedOut": False,
            "kernel": "crashed",
        }

    async def _discard(self, state: _Session, worker_state: _Worker) -> None:
        if state.worker is worker_state:
            state.worker = None
        await self._terminate(worker_state)

    async def _terminate(self, worker_state: _Worker) -> None:
        worker_state.channel.close()
        try:
            os.killpg(worker_state.pgid, 9)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(
                worker_state.process.wait(), timeout=_REAP_TIMEOUT_SECONDS
            )
        except (ProcessLookupError, TimeoutError):
            pass
        self._processes.pop(worker_state.process, None)

    def _cancel_idle(self, state: _Session) -> None:
        if state.idle_task is not None:
            state.idle_task.cancel()
            state.idle_task = None

    def _arm_idle(
        self, session_id: str, state: _Session, worker_state: _Worker
    ) -> None:
        self._cancel_idle(state)
        state.idle_task = asyncio.create_task(
            self._retire_when_idle(session_id, state, worker_state)
        )

    async def _retire_when_idle(
        self, session_id: str, state: _Session, worker_state: _Worker
    ) -> None:
        try:
            deadline = state.last_activity + IDLE_SECONDS
            await asyncio.sleep(max(0.0, deadline - asyncio.get_running_loop().time()))
            if (
                self._sessions.get(session_id) is state
                and state.worker is worker_state
                and state.waiters == 0
                and not state.lock.locked()
                and asyncio.get_running_loop().time() >= deadline
            ):
                async with state.lock:
                    if (
                        self._sessions.get(session_id) is state
                        and state.worker is worker_state
                        and state.waiters == 0
                        and asyncio.get_running_loop().time() >= deadline
                    ):
                        await self._discard(state, worker_state)
        except asyncio.CancelledError:
            pass
        finally:
            if state.idle_task is asyncio.current_task():
                state.idle_task = None


class _ProcessExited(Exception):
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-fd", type=int, required=True)
    args = parser.parse_args()
    worker(args.control_fd)


if __name__ == "__main__":
    main()


__all__ = [
    "IDLE_SECONDS",
    "PythonKernelManager",
    "PythonKernelUnavailable",
    "STREAM_LIMIT_BYTES",
    "TEXT_LIMIT_BYTES",
]
