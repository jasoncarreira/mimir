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

DEFAULT_EXECUTOR_SOCKET = Path("/run/mimir-worklink/socket/worklink-execd.sock")
ENABLED_CHECKOUT_ROOT = Path("/var/lib/mimir-worklink/checkouts")
MAX_REQUEST_BYTES = 256 * 1024
MAX_PROJECTION_BYTES = 1024 * 1024
# Keep this literal independent from worker_exec. The executor runs its image-owned
# copy, so changing either side of the launch contract requires an image rebuild.
EXECUTOR_PROTOCOL_IDENTITY = "worklink-executor-v2-repo-copy-timeout"
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
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    _socket: socket.socket
    returncode: int | None = None
    timed_out: bool = False

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
            self._socket.close()
        return self.returncode


class WorkerClient:
    def __init__(
        self,
        checkout: CheckoutCapability,
        *,
        socket_path: Path = DEFAULT_EXECUTOR_SOCKET,
    ) -> None:
        self.checkout = checkout
        self.socket_path = socket_path

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
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
    ) -> WorkerProcess:
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
        request = {
            "version": 1,
            "op": "launch",
            "executor_identity": EXECUTOR_PROTOCOL_IDENTITY,
            "id": identifier,
            "issue": self.checkout.issue_id,
            "attempt": self.checkout.attempt,
            "device": self.checkout.device,
            "inode": self.checkout.inode,
            "argv": list(argv),
            "env": dict(env),
            "projections": [
                {"path": item.path, "document": item.document.decode("utf-8")}
                for item in projections
            ],
            "timeout_s": timeout_s,
        }
        payload = json.dumps(request, separators=(",", ":")).encode()
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("worker request exceeds size limit")
        sock = await asyncio.to_thread(self._connect)
        read_out, write_out = os.pipe()
        read_err, write_err = os.pipe()
        checkout_fd = self.checkout.duplicate_fd()
        try:
            rights = array.array("i", [checkout_fd, write_out, write_err])
            sock.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
            response = json.loads(await asyncio.to_thread(sock.recv, 4096))
            if "error" in response:
                error = str(response["error"])
                # Executors predating the identity operation can only signal drift
                # through their exact-contract error; keep that legacy fallback.
                if "exact contract" in error or "stale root executor image" in error:
                    raise StaleWorkerExecutorError(STALE_EXECUTOR_DIAGNOSTIC)
                raise RuntimeError(error)
            if response.get("id") != identifier:
                raise RuntimeError("worker executor response identity mismatch")
            if response.get("status") != "started":
                raise RuntimeError("worker executor returned an invalid launch response")
            stdout = asyncio.StreamReader()
            stderr = asyncio.StreamReader()
            loop = asyncio.get_running_loop()
            await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(stdout),
                os.fdopen(read_out, "rb", buffering=0),
            )
            read_out = -1
            await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(stderr),
                os.fdopen(read_err, "rb", buffering=0),
            )
            read_err = -1
            return WorkerProcess(identifier, int(response["pid"]), stdout, stderr, sock)
        except Exception:
            sock.close()
            raise
        finally:
            for fd in (checkout_fd, write_out, write_err, read_out, read_err):
                if fd >= 0:
                    os.close(fd)

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
        sock = await asyncio.to_thread(self._connect)
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
