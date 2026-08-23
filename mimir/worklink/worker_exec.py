from __future__ import annotations

import array
import ctypes
import json
import os
from pathlib import Path, PurePosixPath
import math
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import threading
import time
from typing import Any

from .._rmtree import rmtree_missing_ok
from .checkout import _normalize_checkout_fd
from .identities import get_identities
from .worker_client import (
    DEFAULT_EXECUTOR_SOCKET,
    ENABLED_CHECKOUT_ROOT,
    MAX_PROJECTION_BYTES,
    MAX_REQUEST_BYTES,
    _PROJECTION_PATHS,
    _validate_identifier,
)

HOME_ROOT = Path("/var/lib/mimir-worklink/homes")
REPO_TEST_CHECKOUT_ROOT = Path("/var/lib/mimir-worklink/repo-test-checkouts")
OPENCODE_CHECKOUT_ROOT = Path("/var/lib/mimir-worklink/opencode-checkouts")
REPO_TEST_UV_CACHE = Path("/opt/mimir-worklink/uv-cache")
MAX_FDS = 3
# Deliberately not imported from worker_client: this value must describe the
# immutable executor installed in the root-owned image, not mutable controller code.
EXECUTOR_PROTOCOL_IDENTITY = "worklink-executor-v3-repo-uv-cache"
_STALE_EXECUTOR_DIAGNOSTIC = (
    "stale root executor image: controller and mimir.worklink.worker_exec protocol "
    "identities do not match; rebuild the image and restart the container"
)
_CONTROLLER_CANCELLATION_GRACE_S = 10.0
_PR_SET_NO_NEW_PRIVS = 38
_PR_CAPBSET_DROP = 24
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_CAP_SETGID = 6
_CAP_SETUID = 7
_CAP_SETPCAP = 8
_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_LINUX_CAPABILITY_U32S_3 = 2
_LAUNCH_FIELDS = frozenset({
    "version", "op", "id", "issue", "attempt", "device", "inode",
    "argv", "env", "projections", "timeout_s", "executor_identity",
})
_CANCEL_FIELDS = frozenset({"version", "op", "id", "executor_identity"})
_IDENTITY_FIELDS = frozenset({"version", "op", "executor_identity"})
_jobs: dict[str, subprocess.Popen[bytes]] = {}
_launching: set[str] = set()
_jobs_lock = threading.Lock()


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _libc_call(name: str, *args: object) -> None:
    function = getattr(ctypes.CDLL(None, use_errno=True), name)
    if function(*args) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _set_capabilities(capabilities: set[int]) -> None:
    header = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * _LINUX_CAPABILITY_U32S_3)()
    for capability in capabilities:
        index, offset = divmod(capability, 32)
        bit = 1 << offset
        data[index].effective |= bit
        data[index].permitted |= bit
    _libc_call("capset", ctypes.byref(header), ctypes.byref(data))


def _last_capability() -> int:
    try:
        return int(Path("/proc/sys/kernel/cap_last_cap").read_text().strip())
    except (OSError, ValueError):
        return 63


def _drop_worker(checkout_fd: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    retained = {_CAP_SETPCAP, _CAP_SETUID, _CAP_SETGID}
    _set_capabilities(retained)
    for capability in range(_last_capability() + 1):
        if libc.prctl(_PR_CAPBSET_DROP, capability, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    _set_capabilities({_CAP_SETUID, _CAP_SETGID})
    os.setgroups([])
    os.setresgid(get_identities().worklink_gid, get_identities().worklink_gid, get_identities().worklink_gid)
    os.setresuid(get_identities().worklink_uid, get_identities().worklink_uid, get_identities().worklink_uid)
    _set_capabilities(set())
    os.umask(0o002)
    os.setsid()
    os.fchdir(checkout_fd)
    _verify_worker_identity()


def _status_fields() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key] = value.strip()
    return result


def _verify_worker_identity() -> None:
    if os.getresuid() != (get_identities().worklink_uid,) * 3 or os.getresgid() != (get_identities().worklink_gid,) * 3:
        raise RuntimeError("worker identity drop failed")
    if os.getgroups():
        raise RuntimeError("worker supplementary groups were not cleared")
    status = _status_fields()
    for field in ("CapInh", "CapPrm", "CapEff", "CapAmb", "CapBnd"):
        if int(status.get(field, "1"), 16) != 0:
            raise RuntimeError("worker capabilities were not cleared")
    if status.get("NoNewPrivs") != "1":
        raise RuntimeError("worker no-new-privileges was not set")


def _received_fds(ancillary: list[tuple[int, int, bytes]]) -> list[int]:
    result: list[int] = []
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            values = array.array("i")
            values.frombytes(data[: len(data) - len(data) % values.itemsize])
            result.extend(values)
    return result


def _positive_integer(request: dict[str, Any], field: str) -> int:
    value = request.get(field)
    if type(value) is not int or value < 1:
        raise RuntimeError(f"worker {field} must be a positive integer")
    return value


def _identity_integer(request: dict[str, Any], field: str) -> int:
    value = request.get(field)
    if type(value) is not int or value < 0:
        raise RuntimeError(f"worker {field} identity is invalid")
    return value


def _issued_checkout_relative(resolved: Path) -> tuple[Path, Path]:
    matches: list[tuple[Path, Path]] = []
    for root in (ENABLED_CHECKOUT_ROOT, REPO_TEST_CHECKOUT_ROOT, OPENCODE_CHECKOUT_ROOT):
        try:
            canonical = root.resolve(strict=True)
            matches.append((root, resolved.relative_to(canonical)))
        except (OSError, ValueError):
            continue
    if len(matches) != 1:
        raise RuntimeError("received checkout FD is not the exact issued checkout")
    return matches[0]


def _validate_checkout(fd: int, request: dict[str, Any]) -> Path:
    issue = _positive_integer(request, "issue")
    attempt = _positive_integer(request, "attempt")
    device = _identity_integer(request, "device")
    inode = _identity_integer(request, "inode")
    observed = os.fstat(fd)
    if not stat.S_ISDIR(observed.st_mode):
        raise RuntimeError("received checkout FD is not a directory")
    if (observed.st_dev, observed.st_ino) != (device, inode):
        raise RuntimeError("received checkout FD identity does not match request")
    resolved = Path(os.readlink(f"/proc/self/fd/{fd}"))
    root, relative = _issued_checkout_relative(resolved)
    if (
        len(relative.parts) != 3
        or re.fullmatch(r"[0-9a-f]{64}", relative.parts[0]) is None
        or re.fullmatch(
            rf"{issue}-{attempt}(?:-[0-9a-f]{{32}})?", relative.parts[1]
        ) is None
        or relative.parts[2] != "checkout"
        or (root == OPENCODE_CHECKOUT_ROOT and attempt != 1)
    ):
        raise RuntimeError("received checkout FD is not the exact issued checkout")
    # The boundary is controller-owned and worker-traversable (0710) but NOT
    # readable, so the worker can resolve its own issued checkout by path — which
    # a third-party Node backend requires — while still being unable to enumerate
    # sibling attempts. The directory name carries 128 bits of entropy, and the
    # device/inode check above pins the exact issued directory, so reaching a
    # sibling would require guessing its token AND would still fail identity.
    boundary = resolved.parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(boundary.st_mode)
        or boundary.st_uid != get_identities().mimir_uid
        or boundary.st_gid != get_identities().worklink_gid
        or stat.S_IMODE(boundary.st_mode) != 0o710
    ):
        raise RuntimeError("issued checkout isolation boundary is invalid")
    if observed.st_uid != get_identities().mimir_uid or observed.st_gid != get_identities().worklink_gid:
        raise RuntimeError("issued checkout ownership is invalid")
    if stat.S_IMODE(observed.st_mode) != 0o2770:
        raise RuntimeError("issued checkout mode is invalid")
    return root


def _validate_command(request: dict[str, Any]) -> list[str]:
    argv = request.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(value, str) or not value or "\x00" in value for value in argv)
    ):
        raise RuntimeError("invalid worker command")
    return argv


def _execution_checkout_fd(
    command: list[str], checkout_fd: int, home: Path, *, checkout_root: Path | None = None
) -> int:
    if checkout_root != REPO_TEST_CHECKOUT_ROOT and Path(command[0]).name != "uv":
        return os.dup(checkout_fd)
    project = home / "project"
    shutil.copytree(f"/proc/self/fd/{checkout_fd}", project, symlinks=True)
    project_fd = os.open(
        project,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        _normalize_checkout_fd(
            project_fd, owner_uid=get_identities().mimir_uid, group_gid=get_identities().worklink_gid
        )
    except Exception:
        os.close(project_fd)
        raise
    return project_fd


def _validate_environment(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RuntimeError("worker environment must be string pairs")
    if "HOME" in value:
        raise RuntimeError("worker HOME is assigned by the executor")
    if any(
        not isinstance(key, str)
        or not isinstance(item, str)
        or not key
        or "\x00" in key + item
        for key, item in value.items()
    ):
        raise RuntimeError("worker environment must be string pairs")
    return dict(value)


def _project_home(home: Path, projections: object) -> None:
    if not isinstance(projections, list) or len(projections) > 2:
        raise RuntimeError("at most two worker projections are allowed")
    seen: set[str] = set()
    for projection in projections:
        if not isinstance(projection, dict) or set(projection) != {"path", "document"}:
            raise RuntimeError("invalid projection document")
        path = projection["path"]
        document = projection["document"]
        if not isinstance(path, str) or path not in _PROJECTION_PATHS or path in seen:
            raise RuntimeError("invalid projection destination")
        if not isinstance(document, str):
            raise RuntimeError("invalid projection document")
        encoded = document.encode()
        if len(encoded) > MAX_PROJECTION_BYTES:
            raise RuntimeError("invalid projection document")
        parsed = json.loads(document)
        if not isinstance(parsed, dict):
            raise RuntimeError("invalid projection document")
        seen.add(path)
        relative = PurePosixPath(path)
        parent = home.joinpath(*relative.parts[:-1])
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = parent / relative.name
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise RuntimeError("incomplete projection write")
                view = view[written:]
        finally:
            os.close(fd)
    for root, directories, files in os.walk(home):
        for name in directories:
            os.chown(Path(root) / name, get_identities().worklink_uid, get_identities().worklink_gid, follow_symlinks=False)
            os.chmod(Path(root) / name, 0o700, follow_symlinks=False)
        for name in files:
            os.chown(Path(root) / name, get_identities().worklink_uid, get_identities().worklink_gid, follow_symlinks=False)
            os.chmod(Path(root) / name, 0o600, follow_symlinks=False)


def _seed_repo_test_uv_cache(home: Path) -> Path:
    """Copy the immutable deployment cache into one execution's writable HOME."""
    destination = home / ".cache" / "uv"
    try:
        source = REPO_TEST_UV_CACHE.resolve(strict=True)
    except OSError:
        return destination
    metadata = source.stat()
    if (
        not source.is_dir()
        or metadata.st_uid == get_identities().worklink_uid
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError("repo test uv cache source is not immutable")
    for root, directories, files in os.walk(source):
        for name in (*directories, *files):
            entry = Path(root) / name
            entry_metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(entry_metadata.st_mode):
                try:
                    resolved_entry = entry.resolve(strict=True)
                    resolved_entry.relative_to(source)
                except (OSError, ValueError):
                    raise RuntimeError(
                        "repo test uv cache source is not immutable"
                    ) from None
                continue
            if (
                entry_metadata.st_uid == get_identities().worklink_uid
                or entry_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise RuntimeError("repo test uv cache source is not immutable")
    shutil.copytree(source, destination)
    return destination


def _cleanup_home(home: Path) -> None:
    if not home.exists():
        return
    rmtree_missing_ok(home)


def _process_group_has_live_members(process_group: int) -> bool:
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == process_group and fields[0] not in {"Z", "X"}:
                    return True
            except (OSError, IndexError, ValueError):
                continue
        return False
    observed = subprocess.run(
        ["ps", "-axo", "pgid=,state="],
        check=False,
        capture_output=True,
        text=True,
    )
    if observed.returncode == 0:
        for line in observed.stdout.splitlines():
            fields = line.split()
            if (
                len(fields) >= 2
                and fields[0] == str(process_group)
                and not fields[1].startswith("Z")
            ):
                return True
        return False
    try:
        os.killpg(process_group, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_process_group(process_group: int, deadline: float | None) -> None:
    while _process_group_has_live_members(process_group):
        if deadline is not None and time.monotonic() >= deadline:
            return
        time.sleep(0.01)


def _terminate_process_group_pid(process_group: int, timeout_s: float = 5.0) -> None:
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    _wait_process_group(process_group, time.monotonic() + timeout_s)
    if _process_group_has_live_members(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _wait_process_group(process_group, None)


def _terminate_process_group(proc: subprocess.Popen[bytes], timeout_s: float = 5.0) -> None:
    _terminate_process_group_pid(proc.pid, timeout_s)
    proc.wait()


def _cancel(identifier: str) -> None:
    with _jobs_lock:
        proc = _jobs.get(identifier)
    if proc is None:
        raise RuntimeError("unknown worker id")
    _terminate_process_group(proc)


def _send(connection: socket.socket, response: dict[str, object]) -> None:
    connection.send(json.dumps(response, separators=(",", ":")).encode())


def _validate_executor_identity(request: dict[str, Any]) -> None:
    if request.get("executor_identity") != EXECUTOR_PROTOCOL_IDENTITY:
        raise RuntimeError(_STALE_EXECUTOR_DIAGNOSTIC)


def _handle_identity(connection: socket.socket, request: dict[str, Any], fds: list[int]) -> None:
    if fds or set(request) != _IDENTITY_FIELDS:
        raise RuntimeError("invalid executor identity request")
    _validate_executor_identity(request)
    _send(connection, {
        "status": "identity",
        "executor_identity": EXECUTOR_PROTOCOL_IDENTITY,
    })


def _handle_cancel(connection: socket.socket, request: dict[str, Any], fds: list[int]) -> None:
    if fds or set(request) != _CANCEL_FIELDS:
        raise RuntimeError("invalid cancel request")
    _validate_executor_identity(request)
    identifier = request.get("id")
    if not isinstance(identifier, str):
        raise RuntimeError("invalid worker id")
    _validate_identifier(identifier)
    _cancel(identifier)
    _send(connection, {"id": identifier, "status": "cancelled"})


def _handle_launch(connection: socket.socket, request: dict[str, Any], fds: list[int]) -> None:
    if set(request) != _LAUNCH_FIELDS or len(fds) != MAX_FDS:
        raise RuntimeError("launch request must carry the exact contract and three FDs")
    _validate_executor_identity(request)
    identifier = request.get("id")
    if not isinstance(identifier, str):
        raise RuntimeError("invalid worker id")
    _validate_identifier(identifier)
    checkout_root = _validate_checkout(fds[0], request)
    command = _validate_command(request)
    environment = _validate_environment(request["env"])
    timeout_s = request["timeout_s"]
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        raise RuntimeError("worker timeout must be a positive finite number")
    with _jobs_lock:
        if identifier in _jobs or identifier in _launching:
            raise RuntimeError("worker id is already active")
        _launching.add(identifier)
    home = Path()
    proc: subprocess.Popen[bytes] | None = None
    try:
        anchored_fd = os.open(
            ".",
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=fds[0],
        )
        os.close(fds[0])
        fds[0] = anchored_fd
        candidate_home = HOME_ROOT / identifier
        candidate_home.mkdir(mode=0o700)
        home = candidate_home
        if checkout_root == REPO_TEST_CHECKOUT_ROOT and Path(command[0]).name == "uv":
            environment["UV_CACHE_DIR"] = str(_seed_repo_test_uv_cache(home))
        _project_home(home, request["projections"])
        os.chown(home, get_identities().worklink_uid, get_identities().worklink_gid)
        os.chmod(home, 0o700)
        environment["HOME"] = str(home)
        execution_fd = _execution_checkout_fd(
            command,
            anchored_fd,
            home,
            checkout_root=checkout_root,
        )
        os.close(anchored_fd)
        anchored_fd = execution_fd
        fds[0] = anchored_fd
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=fds[1],
            stderr=fds[2],
            env=environment,
            preexec_fn=lambda: _drop_worker(fds[0]),
            close_fds=True,
            pass_fds=(fds[0],),
        )
        with _jobs_lock:
            _jobs[identifier] = proc
            _launching.remove(identifier)
        os.close(fds[1])
        fds[1] = -1
        os.close(fds[2])
        fds[2] = -1
        _send(connection, {"id": identifier, "status": "started", "pid": proc.pid})
        timed_out = False
        try:
            exit_code = proc.wait(timeout=timeout_s + _CONTROLLER_CANCELLATION_GRACE_S)
            _terminate_process_group(proc, 0)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(proc)
            exit_code = proc.returncode
        with _jobs_lock:
            _jobs.pop(identifier, None)
        _cleanup_home(home)
        home = Path()
        _send(connection, {
            "id": identifier,
            "status": "terminal",
            "exit_code": exit_code,
            "timed_out": timed_out,
        })
    finally:
        with _jobs_lock:
            _launching.discard(identifier)
            active = _jobs.pop(identifier, None) if _jobs.get(identifier) is proc else None
        if active is not None and active.poll() is None:
            _terminate_process_group(active)
        if home != Path():
            _cleanup_home(home)


def handle_connection(connection: socket.socket) -> None:
    fds: list[int] = []
    identifier: str | None = None
    try:
        payload, ancillary, flags, _address = connection.recvmsg(
            MAX_REQUEST_BYTES + 1,
            socket.CMSG_SPACE(MAX_FDS * array.array("i").itemsize),
        )
        fds = _received_fds(ancillary)
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(payload) > MAX_REQUEST_BYTES:
            raise RuntimeError("truncated or oversized worker request")
        request = json.loads(payload)
        if not isinstance(request, dict) or request.get("version") != 1:
            raise RuntimeError("unsupported worker request")
        raw_identifier = request.get("id")
        identifier = raw_identifier if isinstance(raw_identifier, str) else None
        if request.get("op") == "launch":
            _handle_launch(connection, request, fds)
        elif request.get("op") == "cancel":
            _handle_cancel(connection, request, fds)
        elif request.get("op") == "identity":
            _handle_identity(connection, request, fds)
        else:
            raise RuntimeError("unsupported worker operation")
    except Exception as exc:
        try:
            _send(connection, {"id": identifier, "error": str(exc)})
        except OSError:
            pass
    finally:
        for fd in fds:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        connection.close()


def serve(socket_path: Path = DEFAULT_EXECUTOR_SOCKET) -> None:
    socket_path.parent.mkdir(mode=0o710, parents=True, exist_ok=True)
    HOME_ROOT.mkdir(mode=0o710, parents=True, exist_ok=True)
    os.chown(HOME_ROOT, 0, get_identities().worklink_gid)
    os.chmod(HOME_ROOT, 0o710)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(str(socket_path))
    os.chown(socket_path, 0, get_identities().mimir_uid)
    os.chmod(socket_path, 0o660)
    listener.listen(16)
    while True:
        connection, _address = listener.accept()
        _pid, uid, _gid = struct.unpack(
            "3i",
            connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            ),
        )
        if uid != get_identities().mimir_uid:
            try:
                _send(connection, {
                    "id": None,
                    "error": (
                        f"worker executor refused peer uid {uid}; "
                        f"required mimir uid is {get_identities().mimir_uid}"
                    ),
                })
            except OSError:
                pass
            connection.close()
            continue
        threading.Thread(target=handle_connection, args=(connection,), daemon=True).start()


if __name__ == "__main__":
    serve()
