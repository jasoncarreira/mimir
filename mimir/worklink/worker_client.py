from __future__ import annotations

import array
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
from pathlib import Path, PurePosixPath
import socket
import struct
import subprocess
from typing import Literal, Mapping, Protocol, Sequence
import uuid

from ..output_capture import OutputSink, open_output_pair
from . import identities
from .run_state import LeafProcessIdentity

DEFAULT_EXECUTOR_SOCKET = Path("/run/mimir-worklink/socket/worklink-execd.sock")
ENABLED_CHECKOUT_ROOT = Path("/var/lib/mimir-worklink/checkouts")
WORKLINK_CHECKOUT_ROOT = Path("/workspace/.worklink")
MAX_REQUEST_BYTES = 256 * 1024
MAX_PROJECTION_BYTES = 1024 * 1024
CANCEL_SOCKET_TIMEOUT_S = 20.0
# Keep this literal independent from worker_exec. The executor runs its image-owned
# copy, so changing either side of the launch contract requires an image rebuild.
EXECUTOR_PROTOCOL_IDENTITY = "worklink-executor-v10-retained-control"
STALE_EXECUTOR_DIAGNOSTIC = (
    "stale root executor image: controller and mimir.worklink.worker_exec protocol "
    "or source identities do not match; rebuild the image and restart the container"
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


def factory_checkout_for_path(path: Path) -> tuple[Path, int, int] | None:
    """Locate the factory boundary lexically; the executor verifies it by FD."""
    try:
        parts = path.relative_to(WORKLINK_CHECKOUT_ROOT).parts
    except ValueError:
        return None
    if len(parts) < 3 or parts[2] != "checkout":
        return None
    match = re.fullmatch(r"([1-9][0-9]*)-([1-9][0-9]*)", parts[1])
    if match is None or ".." in parts:
        raise ValueError("invalid factory checkout path")
    return WORKLINK_CHECKOUT_ROOT.joinpath(*parts[:3]), int(match[1]), int(match[2])


def run_factory_control(
    checkout: Path, argv: Sequence[str], *, env: Mapping[str, str],
    timeout: float = 30, output_limit: int = 1024 * 1024,
) -> subprocess.CompletedProcess[bytes]:
    """Run retained-tree operations as its owner, without refreshing runtime auth."""
    binding = factory_checkout_for_path(checkout)
    if binding is None:
        raise ValueError("factory control requires an inner checkout")
    root, issue, attempt = binding

    async def run() -> subprocess.CompletedProcess[bytes]:
        client = WorkerClient.for_factory_checkout(root, issue_id=issue, attempt=attempt)
        client._launch_op = "launch_factory_control"
        client._socket_timeout_s = timeout + CANCEL_SOCKET_TIMEOUT_S
        stdout, stderr = open_output_pair(None, output_limit, None, output_limit)
        try:
            process = await client.launch(
                local_checkout=root, argv=argv,
                env={key: value for key, value in env.items() if key != "HOME"},
                identifier=str(uuid.uuid4()), timeout_s=timeout,
                stdout_sink=stdout, stderr_sink=stderr,
            )
            code = await process.wait()
            if process.timed_out:
                raise subprocess.TimeoutExpired(argv, timeout)
            if process.output_overflow:
                raise RuntimeError("factory control output exceeds bounds")
            return subprocess.CompletedProcess(
                argv, code, stdout.read_bounded()[0], stderr.read_bounded()[0],
            )
        finally:
            stdout.close()
            stderr.close()

    # Factory control has a synchronous API, also called by the async controller.
    # Own the loop in a thread rather than nesting it in the controller's loop.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(run())).result()


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


@dataclass(frozen=True)
class RetainedWorkerTarget:
    """Server-held retained target; it contains no caller-selected path or PID."""

    kind: Literal["leaf", "factory"]
    issue_id: int
    attempt: int
    worker_identifier: str | None = None
    leaf_session: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"leaf", "factory"} or self.issue_id < 1 or self.attempt < 1:
            raise ValueError("retained worker target identity is invalid")
        if self.kind == "factory":
            if self.worker_identifier is not None or self.leaf_session is not None:
                raise ValueError("factory retained target cannot carry a leaf process")
            return
        if self.worker_identifier is None or self.leaf_session is None:
            raise ValueError("leaf retained target requires its worker session")
        _validate_identifier(self.worker_identifier)
        if re.fullmatch(r"[0-9a-f]{64}", self.leaf_session) is None:
            raise ValueError("leaf retained target session is invalid")

    @classmethod
    def for_leaf(cls, identity: LeafProcessIdentity) -> RetainedWorkerTarget:
        if identity.handle_substrate != "local_subprocess":
            raise ValueError("retained leaf requires a local subprocess handle")
        # Worker controls address the executor's opaque UUID. Legacy direct PID
        # handles remain readable, but are deliberately not controllable here.
        _validate_identifier(identity.handle_identifier)
        return cls(
            "leaf",
            identity.issue_id,
            identity.attempt,
            identity.handle_identifier,
            identity.leaf_session,
        )

    @classmethod
    def for_factory(cls, *, issue_id: int, attempt: int) -> RetainedWorkerTarget:
        return cls("factory", issue_id, attempt)


@dataclass(frozen=True)
class RetainedWorkerReceipt:
    operation_id: str
    request_digest: str
    outcome: Literal["held", "running", "admitted"]
    already_applied: bool
    checkout: Path | None = None
    device: int | None = None
    inode: int | None = None


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
            while True:
                payload = await loop.run_in_executor(None, self._socket.recv, 4096)
                if not payload:
                    raise RuntimeError("worker executor closed before terminal result")
                response = json.loads(payload)
                if response.get("id") != self.identifier:
                    raise RuntimeError("worker executor returned an invalid terminal/event identity")
                if response.get("status") != "event":
                    break
                from ..event_logger import safe_log_event

                event = response.get("event")
                if event not in {
                    "worklink_factory_orphan_adopted", "worklink_factory_reap_refused",
                    "worklink_factory_supervisor_lost",
                }:
                    raise RuntimeError("worker executor returned an invalid event")
                await safe_log_event(event, **{
                    key: value for key, value in response.items()
                    if key in {"run_id", "issue_id", "attempt", "pid", "error", "adopted_count", "final"}
                })
            if "error" in response:
                raise RuntimeError(str(response["error"]))
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
        self._socket_timeout_s: float | None = None
        self._launch_op = "launch_path" if path_checkout is not None else "launch"

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

    @classmethod
    def for_factory_checkout(
        cls,
        path: Path,
        *,
        issue_id: int,
        attempt: int,
        socket_path: Path = DEFAULT_EXECUTOR_SOCKET,
    ) -> WorkerClient:
        client = cls.for_path_checkout(
            path,
            issue_id=issue_id,
            attempt=attempt,
            run_uid=identities.get_identities().worklink_uid,
            socket_path=socket_path,
        )
        client._launch_op = "launch_factory"
        return client

    def _connect(self, timeout_s: float | None = None) -> socket.socket:
        # This local executor protocol requires Linux peer authentication; do not
        # connect (or send a request) when the controller cannot verify root.
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("worker executor requires Linux SO_PEERCRED peer authentication")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            if timeout_s is None:
                timeout_s = getattr(self, "_socket_timeout_s", None)
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
            "op": self._launch_op,
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

    async def hold_leaf(
        self, target: RetainedWorkerTarget, *, operation_id: str
    ) -> RetainedWorkerReceipt:
        if target.kind != "leaf":
            raise ValueError("leaf hold requires a leaf retained target")
        return await self._retained_control("hold_leaf", target, operation_id)

    async def resume_leaf(
        self, target: RetainedWorkerTarget, *, operation_id: str
    ) -> RetainedWorkerReceipt:
        if target.kind != "leaf":
            raise ValueError("leaf resume requires a leaf retained target")
        return await self._retained_control("resume_leaf", target, operation_id)

    async def admit_retained_checkout(
        self, target: RetainedWorkerTarget, *, operation_id: str
    ) -> RetainedWorkerReceipt:
        return await self._retained_control("admit_retained_checkout", target, operation_id)

    async def _retained_control(
        self,
        operation: Literal["hold_leaf", "resume_leaf", "admit_retained_checkout"],
        target: RetainedWorkerTarget,
        operation_id: str,
    ) -> RetainedWorkerReceipt:
        _validate_identifier(operation_id)
        request: dict[str, object] = {
            "version": 1,
            "op": operation,
            "executor_identity": EXECUTOR_PROTOCOL_IDENTITY,
            "operation_id": operation_id,
            "target_kind": target.kind,
            "issue": target.issue_id,
            "attempt": target.attempt,
        }
        if target.kind == "leaf":
            request["target_identifier"] = target.worker_identifier
            request["leaf_session"] = target.leaf_session
        payload = json.dumps(request, separators=(",", ":"), sort_keys=True).encode()
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("retained worker request exceeds size limit")
        expected_digest = hashlib.sha256(payload).hexdigest()
        sock = await asyncio.to_thread(self._connect, CANCEL_SOCKET_TIMEOUT_S)
        try:
            sock.send(payload)
            response = json.loads(await asyncio.to_thread(sock.recv, 4096))
            if "error" in response:
                error = str(response["error"])
                if "stale root executor image" in error or "unsupported worker operation" in error:
                    raise StaleWorkerExecutorError(STALE_EXECUTOR_DIAGNOSTIC)
                raise RuntimeError(error)
            if response.get("operation_id") != operation_id:
                raise RuntimeError("worker executor response operation identity mismatch")
            status = response.get("status")
            if status not in {"applied", "already_applied"}:
                raise RuntimeError("worker executor returned an invalid retained-control result")
            if response.get("request_digest") != expected_digest:
                raise RuntimeError("worker executor response request digest mismatch")
            expected_outcome = {
                "hold_leaf": "held",
                "resume_leaf": "running",
                "admit_retained_checkout": "admitted",
            }[operation]
            if response.get("outcome") != expected_outcome:
                raise RuntimeError("worker executor returned an invalid retained-control outcome")
            checkout = response.get("checkout")
            path: Path | None = None
            device: int | None = None
            inode: int | None = None
            if operation == "admit_retained_checkout":
                if not isinstance(checkout, dict) or set(checkout) != {"path", "device", "inode"}:
                    raise RuntimeError("worker executor returned an invalid checkout admission")
                raw_path = checkout["path"]
                device = checkout["device"]
                inode = checkout["inode"]
                if (
                    not isinstance(raw_path, str)
                    or not raw_path.startswith("/")
                    or type(device) is not int
                    or device < 0
                    or type(inode) is not int
                    or inode < 0
                ):
                    raise RuntimeError("worker executor returned an invalid checkout admission")
                path = Path(raw_path)
            elif checkout is not None:
                raise RuntimeError("worker executor returned an unexpected checkout admission")
            return RetainedWorkerReceipt(
                operation_id=operation_id,
                request_digest=expected_digest,
                outcome=expected_outcome,
                already_applied=status == "already_applied",
                checkout=path,
                device=device,
                inode=inode,
            )
        finally:
            sock.close()


async def verify_executor_identity(
    socket_path: Path = DEFAULT_EXECUTOR_SOCKET,
) -> str:
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
            or not isinstance(response.get("source_commit"), str)
            or len(response["source_commit"]) != 40
            or any(character not in "0123456789abcdef" for character in response["source_commit"])
        ):
            raise StaleWorkerExecutorError(STALE_EXECUTOR_DIAGNOSTIC)
        return response["source_commit"]
    finally:
        sock.close()


def _validate_identifier(identifier: str) -> None:
    try:
        parsed = uuid.UUID(identifier)
    except (ValueError, AttributeError) as exc:
        raise ValueError("worker id must be canonical UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != identifier:
        raise ValueError("worker id must be canonical UUIDv4")
