from __future__ import annotations

import array
import asyncio
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PurePosixPath
import socket
import struct
from typing import Mapping, Protocol, Sequence
import uuid

from ..output_capture import OutputSink, open_output_pair

DEFAULT_EXECUTOR_SOCKET = Path("/run/mimir-worklink/socket/worklink-execd.sock")
ENABLED_CHECKOUT_ROOT = Path("/var/lib/mimir-worklink/checkouts")
WORKLINK_CHECKOUT_ROOT = Path("/workspace/.worklink")
MAX_REQUEST_BYTES = 256 * 1024
MAX_PROJECTION_BYTES = 1024 * 1024
CANCEL_SOCKET_TIMEOUT_S = 20.0
# Keep this literal independent from worker_exec. The executor runs its image-owned
# copy, so changing either side of the launch contract requires an image rebuild.
EXECUTOR_PROTOCOL_IDENTITY = "worklink-executor-v6-bounded-output-path-checkout"
STALE_EXECUTOR_DIAGNOSTIC = (
    "stale root executor image: controller and mimir.worklink.worker_exec protocol "
    "identities do not match; rebuild the image and restart the container"
)
_PROJECTION_PATHS = frozenset({
    ".config/opencode/opencode.json",
    ".local/share/opencode/auth.json",
})


class CheckoutCapability(Protocol):
    path: Path
    issue_id: int
    attempt: int
    device: int
    inode: int

    def verify(self, local_checkout: Path | None) -> None: ...

    def duplicate_fd(self) -> int: ...


class StaleWorkerExecutorError(RuntimeError):
    """The root-owned executor image does not implement this controller contract."""


@dataclass(frozen=True)
class WorkerProjection:
    path: str
    document: bytes

    def __post_init__(self) -> None:
        relative = PurePosixPath(self.path)
        if self.path not in _PROJECTION_PATHS or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("worker projection destination is not permitted")
        if len(self.document) > MAX_PROJECTION_BYTES:
            raise ValueError("worker projection exceeds size limit")
        parsed = json.loads(self.document)
        if not isinstance(parsed, dict):
            raise ValueError("worker projection must be a JSON object")


@dataclass
class WorkerProcess:
    identifier: str
    pid: int
    _socket: socket.socket
    returncode: int | None = None
    timed_out: bool = False
    output_overflow: bool = False

    async def wait(self) -> int:
        if self.returncode is None:
            loop = asyncio.get_running_loop()
            payload = await loop.run_in_executor(None, self._socket.recv, 4096)
            if not payload:
                raise RuntimeError("worker executor closed before terminal result")
            response = json.loads(payload)
            if response.get("id") != self.identifier or response.get("status") != "terminal":
                raise RuntimeError("worker executor returned an invalid terminal result")
            self.returncode = int(response["exit_code"])
            self.timed_out = response.get("timed_out") is True
            self.output_overflow = response.get("output_overflow") is True
            self._socket.close()
        return self.returncode


class WorkerClient:
    def __init__(
        self,
        checkout: CheckoutCapability | None,
        *,
        socket_path: Path = DEFAULT_EXECUTOR_SOCKET,
        path_checkout: Path | None = None,
        issue_id: int | None = None,
        attempt: int | None = None,
        run_uid: int | None = None,
    ) -> None:
        self.checkout = checkout
        self.socket_path = socket_path
        self.path_checkout = path_checkout
        self.issue_id = issue_id
        self.attempt = attempt
        self.run_uid = run_uid

    @classmethod
    def for_path_checkout(
        cls,
        path: Path,
        *,
        issue_id: int,
        attempt: int,
        run_uid: int,
        socket_path: Path = DEFAULT_EXECUTOR_SOCKET,
    ) -> WorkerClient:
        if issue_id < 1 or attempt < 1 or run_uid < 0:
            raise ValueError("path-addressed worker launch identity is invalid")
        return cls(
            None,
            socket_path=socket_path,
            path_checkout=Path(os.path.abspath(path)),
            issue_id=issue_id,
            attempt=attempt,
            run_uid=run_uid,
        )

    def _connect(self, timeout_s: float | None = None) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            if timeout_s is not None:
                sock.settimeout(timeout_s)
            sock.connect(str(self.socket_path))
            _pid, uid, _gid = struct.unpack(
                "3i",
                sock.getsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_PEERCRED,
                    struct.calcsize("3i"),
                ),
            )
            if uid != 0:
                raise RuntimeError("worker executor peer is not root")
            return sock
        except Exception:
            sock.close()
            raise

    async def launch(
        self,
        *,
        local_checkout: Path | None,
        argv: Sequence[str],
        env: Mapping[str, str],
        projections: Sequence[WorkerProjection] = (),
        identifier: str,
        timeout_s: float,
        stdout_sink: OutputSink | None = None,
        stderr_sink: OutputSink | None = None,
    ) -> WorkerProcess:
        path_addressed = self.path_checkout is not None
        if path_addressed:
            if local_checkout is None or Path(os.path.abspath(local_checkout)) != self.path_checkout:
                raise ValueError("work spec checkout does not match path-addressed checkout")
        else:
            if self.checkout is None:
                raise ValueError("worker launch requires a checkout")
            self.checkout.verify(local_checkout)
        _validate_identifier(identifier)
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or any(not isinstance(value, str) or not value or "\x00" in value for value in argv)
        ):
            raise ValueError("worker command must contain non-empty strings")
        if len(projections) > 2 or len({item.path for item in projections}) != len(projections):
            raise ValueError("worker projections must use at most two unique destinations")
        if "HOME" in env:
            raise ValueError("worker HOME is assigned by the executor")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("worker timeout must be positive")
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "\x00" in key + value
            for key, value in env.items()
        ):
            raise ValueError("worker environment must contain string pairs")
        request: dict[str, object] = {
            "version": 1,
            "op": "launch_path" if path_addressed else "launch",
            "executor_identity": EXECUTOR_PROTOCOL_IDENTITY,
            "id": identifier,
            "argv": list(argv),
            "env": dict(env),
            "projections": [
                {"path": item.path, "document": item.document.decode("utf-8")}
                for item in projections
            ],
            "timeout_s": timeout_s,
            "stdout_limit": stdout_sink.limit if stdout_sink is not None else 1,
            "stderr_limit": stderr_sink.limit if stderr_sink is not None else 1,
        }
        if path_addressed:
            request.update({
                "path": str(self.path_checkout),
                "issue": self.issue_id,
                "attempt": self.attempt,
                "run_uid": self.run_uid,
            })
        else:
            assert self.checkout is not None
            request.update({
                "issue": self.checkout.issue_id,
                "attempt": self.checkout.attempt,
                "device": self.checkout.device,
                "inode": self.checkout.inode,
            })
        payload = json.dumps(request, separators=(",", ":")).encode()
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("worker request exceeds size limit")
        opened_here = stdout_sink is None and stderr_sink is None
        if (stdout_sink is None) != (stderr_sink is None):
            raise ValueError("worker output sinks must be supplied together")
        if stdout_sink is None or stderr_sink is None:
            stdout_sink, stderr_sink = open_output_pair(None, 1, None, 1)
        if path_addressed:
            checkout_fd = -1
        else:
            assert self.checkout is not None
            checkout_fd = self.checkout.duplicate_fd()
        sock = await asyncio.to_thread(self._connect)
        try:
            rights = array.array(
                "i",
                [stdout_sink.fd, stderr_sink.fd]
                if path_addressed
                else [checkout_fd, stdout_sink.fd, stderr_sink.fd],
            )
            sock.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
            response = json.loads(await asyncio.to_thread(sock.recv, 4096))
            if "error" in response:
                error = str(response["error"])
                # Executors predating the identity operation can only signal drift
                # through their exact-contract error; keep that legacy fallback.
                if any(
                    marker in error
                    for marker in (
                        "exact contract",
                        "stale root executor image",
                        "unsupported worker operation",
                    )
                ):
                    raise StaleWorkerExecutorError(STALE_EXECUTOR_DIAGNOSTIC)
                raise RuntimeError(error)
            if response.get("id") != identifier:
                raise RuntimeError("worker executor response identity mismatch")
            if response.get("status") != "started":
                raise RuntimeError("worker executor returned an invalid launch response")
            return WorkerProcess(identifier, int(response["pid"]), sock)
        except Exception:
            sock.close()
            raise
        finally:
            if checkout_fd >= 0:
                os.close(checkout_fd)
            if opened_here:
                stdout_sink.close()
                stderr_sink.close()

    async def cancel(self, identifier: str) -> None:
        _validate_identifier(identifier)
        payload = json.dumps(
            {
                "version": 1,
                "op": "cancel",
                "id": identifier,
                "executor_identity": EXECUTOR_PROTOCOL_IDENTITY,
            },
            separators=(",", ":"),
        ).encode()
        sock = await asyncio.to_thread(self._connect, CANCEL_SOCKET_TIMEOUT_S)
        try:
            sock.send(payload)
            response = json.loads(await asyncio.to_thread(sock.recv, 4096))
            if "error" in response:
                error = str(response["error"])
                if "invalid cancel request" in error or "stale root executor image" in error:
                    raise StaleWorkerExecutorError(STALE_EXECUTOR_DIAGNOSTIC)
                raise RuntimeError(error)
            if response.get("id") != identifier:
                raise RuntimeError("worker executor response identity mismatch")
            if response.get("status") != "cancelled":
                raise RuntimeError("worker executor returned an invalid cancel response")
        finally:
            sock.close()


async def verify_executor_identity(
    socket_path: Path = DEFAULT_EXECUTOR_SOCKET,
) -> None:
    """Verify the image-owned executor before a launch contract is needed."""
    client = object.__new__(WorkerClient)
    client.socket_path = socket_path
    sock = await asyncio.to_thread(client._connect)
    try:
        payload = json.dumps(
            {
                "version": 1,
                "op": "identity",
                "executor_identity": EXECUTOR_PROTOCOL_IDENTITY,
            },
            separators=(",", ":"),
        ).encode()
        sock.send(payload)
        response = json.loads(await asyncio.to_thread(sock.recv, 4096))
        if (
            response.get("status") != "identity"
            or response.get("executor_identity") != EXECUTOR_PROTOCOL_IDENTITY
        ):
            raise StaleWorkerExecutorError(STALE_EXECUTOR_DIAGNOSTIC)
    finally:
        sock.close()


def _validate_identifier(identifier: str) -> None:
    try:
        parsed = uuid.UUID(identifier)
    except (ValueError, AttributeError) as exc:
        raise ValueError("worker id must be canonical UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != identifier:
        raise ValueError("worker id must be canonical UUIDv4")
