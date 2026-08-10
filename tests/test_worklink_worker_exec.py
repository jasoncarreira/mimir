from __future__ import annotations

import array
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import struct
import sys
import uuid

import pytest

from mimir.worklink.checkout import CheckoutAuthorization, _mint_checkout_authorization
from mimir.worklink.worker_client import (
    MAX_PROJECTION_BYTES,
    WorkerClient,
    WorkerProcess,
    WorkerProjection,
)
import mimir.worklink.worker_exec as worker_exec


def _issued(tmp_path: Path) -> Path:
    path = tmp_path / "checkouts" / ("a" * 64) / "41-2"
    path.mkdir(parents=True)
    return path


def _authorization(path: Path) -> CheckoutAuthorization:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    return _mint_checkout_authorization(path, 41, 2, fd)


def test_checkout_authorization_cannot_be_constructed_by_a_client(tmp_path: Path) -> None:
    path = _issued(tmp_path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(TypeError, match="issued by the checkout factory"):
            CheckoutAuthorization(path, 41, 2, fd)
    finally:
        os.close(fd)


def test_worker_projection_has_fixed_destination_json_and_size_contract() -> None:
    projection = WorkerProjection(".config/opencode/opencode.json", b'{"model":"x"}')
    assert json.loads(projection.document) == {"model": "x"}
    WorkerProjection(".local/share/opencode/auth.json", b"{}")
    with pytest.raises(ValueError, match="destination"):
        WorkerProjection("arbitrary.json", b"{}")
    with pytest.raises(json.JSONDecodeError):
        WorkerProjection(".config/opencode/opencode.json", b"invalid")
    with pytest.raises(ValueError, match="size"):
        WorkerProjection(".config/opencode/opencode.json", b" " * (MAX_PROJECTION_BYTES + 1))


def test_client_rejects_non_uuid_home_and_invalid_commands(tmp_path: Path) -> None:
    path = _issued(tmp_path)
    with _authorization(path) as checkout:
        client = WorkerClient(checkout)
        with pytest.raises(ValueError, match="UUIDv4"):
            asyncio.run(client.launch(local_checkout=path, argv=["true"], env={}, identifier="job"))
        identifier = str(uuid.uuid4())
        with pytest.raises(ValueError, match="HOME"):
            asyncio.run(client.launch(local_checkout=path, argv=["true"], env={"HOME": "/tmp"}, identifier=identifier))
        with pytest.raises(ValueError, match="non-empty"):
            asyncio.run(client.launch(local_checkout=path, argv=[], env={}, identifier=identifier))


@pytest.mark.asyncio
async def test_client_authenticates_root_before_sending_fds(tmp_path: Path, monkeypatch) -> None:
    path = _issued(tmp_path)
    sent: list[object] = []

    class Peer:
        def connect(self, path: str) -> None:
            pass

        def getsockopt(self, *args: object) -> bytes:
            return struct.pack("3i", 123, 1001, 1001)

        def sendmsg(self, *args: object) -> None:
            sent.append(args)

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "SO_PEERCRED", getattr(socket, "SO_PEERCRED", 17), raising=False)
    monkeypatch.setattr(socket, "socket", lambda *args: Peer())
    with _authorization(path) as checkout:
        with pytest.raises(RuntimeError, match="not root"):
            await WorkerClient(checkout).launch(
                local_checkout=path,
                argv=["true"],
                env={},
                identifier=str(uuid.uuid4()),
            )
    assert sent == []


@pytest.mark.asyncio
async def test_worker_process_requires_identity_bound_terminal_result(monkeypatch) -> None:
    identifier = str(uuid.uuid4())

    class Peer:
        def recv(self, size: int) -> bytes:
            return json.dumps({"id": str(uuid.uuid4()), "status": "terminal", "exit_code": 0}).encode()

        def close(self) -> None:
            pass

    process = WorkerProcess(identifier, 12, asyncio.StreamReader(), asyncio.StreamReader(), Peer())
    with pytest.raises(RuntimeError, match="invalid terminal"):
        await process.wait()


def test_validate_checkout_refuses_arbitrary_and_replaced_fds(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "checkouts"
    issued = _issued(tmp_path)
    monkeypatch.setattr(worker_exec, "ENABLED_CHECKOUT_ROOT", root)
    monkeypatch.setattr(worker_exec, "MIMIR_UID", os.getuid())
    monkeypatch.setattr(worker_exec, "WORKLINK_GID", os.getgid())
    issued.chmod(0o2770)

    def request(fd: int) -> dict[str, object]:
        observed = os.fstat(fd)
        return {
            "device": observed.st_dev,
            "inode": observed.st_ino,
            "issue": 41,
            "attempt": 2,
        }

    arbitrary = tmp_path / "other" / ("a" * 64) / "41-2"
    arbitrary.mkdir(parents=True)
    old = tmp_path / "old-issued"
    issued.rename(old)
    issued.mkdir()
    for path in (arbitrary, old):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        monkeypatch.setattr(worker_exec.os, "readlink", lambda _name, path=path: str(path))
        try:
            with pytest.raises(RuntimeError, match="exact issued"):
                worker_exec._validate_checkout(fd, request(fd))
        finally:
            os.close(fd)


def test_project_home_completes_partial_writes_and_applies_modes(tmp_path: Path, monkeypatch) -> None:
    document = json.dumps({"payload": "x" * 10000})
    real_write = os.write
    writes: list[int] = []

    def partial(fd: int, data: object) -> int:
        view = memoryview(data)
        count = min(17, len(view))
        writes.append(count)
        return real_write(fd, view[:count])

    monkeypatch.setattr(worker_exec.os, "write", partial)
    monkeypatch.setattr(worker_exec.os, "chown", lambda *args, **kwargs: None)
    worker_exec._project_home(
        tmp_path,
        [{"path": ".config/opencode/opencode.json", "document": document}],
    )
    target = tmp_path / ".config/opencode/opencode.json"
    assert target.read_text() == document
    assert len(writes) > 2
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_executor_rejects_fd_count_and_extra_request_fields() -> None:
    identifier = str(uuid.uuid4())
    payload = json.dumps({"version": 1, "op": "launch", "id": identifier, "uid": 0}).encode()

    class Connection:
        responses: list[dict[str, object]] = []

        def recvmsg(self, *args: object) -> tuple[bytes, list[object], int, None]:
            return payload, [], 0, None

        def send(self, data: bytes) -> None:
            self.responses.append(json.loads(data))

        def close(self) -> None:
            pass

    connection = Connection()
    worker_exec.handle_connection(connection)
    assert "exact contract and three FDs" in connection.responses[0]["error"]
    assert "path" not in worker_exec._LAUNCH_FIELDS
    assert "uid" not in worker_exec._LAUNCH_FIELDS


@pytest.mark.parametrize("field,value", [("issue", 0), ("attempt", -1), ("issue", "41")])
def test_executor_requires_positive_integer_issue_and_attempt(
    field: str, value: object
) -> None:
    request = {"issue": 41, "attempt": 2, field: value}

    with pytest.raises(RuntimeError, match="positive integer"):
        worker_exec._positive_integer(request, field)


def test_executor_authenticates_mimir_peer_before_dispatch(monkeypatch, tmp_path: Path) -> None:
    dispatched: list[object] = []

    class Connection:
        closed = False

        def getsockopt(self, *args: object) -> bytes:
            return struct.pack("3i", 12, 999, 999)

        def close(self) -> None:
            self.closed = True

    connection = Connection()

    class Listener:
        def bind(self, path: str) -> None:
            pass

        def listen(self, count: int) -> None:
            pass

        def accept(self) -> tuple[Connection, None]:
            if not connection.closed:
                return connection, None
            raise RuntimeError("stop")

    monkeypatch.setattr(socket, "SO_PEERCRED", getattr(socket, "SO_PEERCRED", 17), raising=False)
    monkeypatch.setattr(socket, "socket", lambda *args: Listener())
    monkeypatch.setattr(worker_exec, "MIMIR_UID", 1001)
    monkeypatch.setattr(worker_exec, "HOME_ROOT", tmp_path / "homes")
    monkeypatch.setattr(worker_exec.os, "chown", lambda *args: None)
    monkeypatch.setattr(worker_exec.os, "chmod", lambda *args: None)
    monkeypatch.setattr(worker_exec.threading, "Thread", lambda *args, **kwargs: dispatched.append((args, kwargs)))
    with pytest.raises(RuntimeError, match="stop"):
        worker_exec.serve(tmp_path / "socket")
    assert connection.closed
    assert dispatched == []


def test_drop_worker_uses_irreversible_identity_sequence(monkeypatch) -> None:
    events: list[object] = []

    class Libc:
        def prctl(self, *args: object) -> int:
            events.append(("prctl", args[0], args[1]))
            return 0

    monkeypatch.setattr(worker_exec.ctypes, "CDLL", lambda *args, **kwargs: Libc())
    monkeypatch.setattr(worker_exec, "_set_capabilities", lambda caps: events.append(("caps", set(caps))))
    monkeypatch.setattr(worker_exec, "_last_capability", lambda: 2)
    monkeypatch.setattr(worker_exec.os, "setgroups", lambda groups: events.append(("groups", groups)))
    monkeypatch.setattr(worker_exec.os, "setresgid", lambda *ids: events.append(("gid", ids)), raising=False)
    monkeypatch.setattr(worker_exec.os, "setresuid", lambda *ids: events.append(("uid", ids)), raising=False)
    monkeypatch.setattr(worker_exec.os, "umask", lambda mode: events.append(("umask", mode)))
    monkeypatch.setattr(worker_exec.os, "setsid", lambda: events.append(("setsid",)))
    monkeypatch.setattr(worker_exec.os, "fchdir", lambda fd: events.append(("cwd", fd)))
    monkeypatch.setattr(worker_exec, "_verify_worker_identity", lambda: events.append(("verify",)))
    worker_exec._drop_worker(9)
    assert ("groups", []) in events
    assert ("gid", (1002, 1002, 1002)) in events
    assert ("uid", (1002, 1002, 1002)) in events
    assert events.index(("groups", [])) < events.index(("gid", (1002, 1002, 1002)))
    assert events.index(("gid", (1002, 1002, 1002))) < events.index(("uid", (1002, 1002, 1002)))
    assert events[-1] == ("verify",)
    assert events.count(("caps", set())) == 1


def test_worker_identity_verifier_rejects_any_retained_authority(monkeypatch) -> None:
    monkeypatch.setattr(worker_exec.os, "getresuid", lambda: (1002, 1002, 1002), raising=False)
    monkeypatch.setattr(worker_exec.os, "getresgid", lambda: (1002, 1002, 1002), raising=False)
    monkeypatch.setattr(worker_exec.os, "getgroups", lambda: [])
    clean = {
        "CapInh": "0", "CapPrm": "0", "CapEff": "0", "CapAmb": "0",
        "CapBnd": "0", "NoNewPrivs": "1",
    }
    monkeypatch.setattr(worker_exec, "_status_fields", lambda: clean)
    worker_exec._verify_worker_identity()
    monkeypatch.setattr(worker_exec, "_status_fields", lambda: {**clean, "CapBnd": "1"})
    with pytest.raises(RuntimeError, match="capabilities"):
        worker_exec._verify_worker_identity()


def test_terminal_waits_for_in_group_writers_before_cleanup(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    checkout_fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    identifier = str(uuid.uuid4())
    responses: list[dict[str, object]] = []
    events: list[str] = []

    class Connection:
        def send(self, payload: bytes) -> None:
            response = json.loads(payload)
            responses.append(response)
            events.append(str(response["status"]))

    monkeypatch.setattr(worker_exec, "HOME_ROOT", tmp_path / "homes")
    worker_exec.HOME_ROOT.mkdir()
    monkeypatch.setattr(worker_exec, "_validate_checkout", lambda *args: None)
    monkeypatch.setattr(worker_exec.os, "chown", lambda *args, **kwargs: None)

    def enter_group(fd: int) -> None:
        os.setsid()
        os.fchdir(fd)

    monkeypatch.setattr(worker_exec, "_drop_worker", enter_group)
    cleanup_home = worker_exec._cleanup_home

    def observed_cleanup(home: Path) -> None:
        pid = int(responses[0]["pid"])
        assert not worker_exec._process_group_has_live_members(pid)
        output = bytearray()
        while True:
            chunk = os.read(stdout_read, 4096)
            if not chunk:
                break
            output.extend(chunk)
        assert bytes(output) == b"ready"
        events.append("cleanup")
        cleanup_home(home)

    monkeypatch.setattr(worker_exec, "_cleanup_home", observed_cleanup)
    request = {
        "version": 1,
        "op": "launch",
        "id": identifier,
        "issue": 41,
        "attempt": 2,
        "device": 0,
        "inode": 0,
        "argv": [
            "/bin/sh",
            "-c",
            '(trap "" TERM; printf ready; sleep 30; printf late > "$HOME/retained") & sleep .2',
        ],
        "env": {"PATH": "/usr/bin:/bin"},
        "projections": [],
    }
    fds = [checkout_fd, stdout_write, stderr_write]
    try:
        worker_exec._handle_launch(Connection(), request, fds)
        assert events == ["started", "cleanup", "terminal"]
        assert responses[-1]["exit_code"] == 0
        assert not (tmp_path / "homes" / identifier).exists()
        assert os.read(stderr_read, 4096) == b""
    finally:
        for fd in (*fds, stdout_read, stderr_read):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def test_worker_payload_cannot_reach_controller_canary_and_detector_is_live() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires Linux root to exercise the executor identity boundary")

    boundary = Path("/tmp") / f"mimir-worker-exec-{uuid.uuid4()}"
    checkout_root = boundary / "checkouts"
    checkout = checkout_root / ("a" * 64) / "41-2"
    home_root = boundary / "homes"
    controller_home = boundary / "mimir-home"
    socket_path = boundary / "executor.sock"
    checkout.mkdir(parents=True)
    home_root.mkdir(mode=0o710)
    os.chown(home_root, 0, 1002)
    controller_home.mkdir(mode=0o700)
    os.chown(checkout, 1001, 1002)
    checkout.chmod(0o2770)
    os.chown(controller_home, 1001, 1001)
    canary = controller_home / "canary"
    canary.write_text("original")
    os.chown(canary, 1001, 1001)
    canary.chmod(0o600)

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(str(socket_path))
    socket_path.chmod(0o666)
    listener.listen(1)
    async def controller_run() -> dict[str, object]:
        canary.write_text("control")
        detector_live = canary.read_text() == "control"
        canary.write_text("original")
        with _authorization(checkout) as authorization:
            process = await WorkerClient(authorization, socket_path=socket_path).launch(
                local_checkout=checkout,
                argv=["/bin/sh", "-c", (
                    'cd "$HOME" || exit 60; printf "home-ok\n"; cd /; '
                    'parent=${HOME%/*}; '
                    'if ls "$parent" >/dev/null 2>&1; then exit 61; fi; '
                    'if touch "$parent/sibling" 2>/dev/null; then exit 62; fi; '
                    'if mkdir "$parent/sibling-dir" 2>/dev/null; then exit 63; fi; '
                    'if mv "$HOME" "$parent/renamed" 2>/dev/null; then exit 64; fi; '
                    'if rmdir "$HOME" 2>/dev/null; then exit 65; fi; '
                    'if cat "$CANARY" >/dev/null 2>&1; then exit 66; fi; '
                    'if printf attack > "$CANARY" 2>/dev/null; then exit 67; fi; '
                    'exit 23'
                )],
                env={"PATH": "/usr/bin:/bin", "CANARY": str(canary)},
                identifier=str(uuid.uuid4()),
            )
            stdout, stderr = await asyncio.gather(process.stdout.read(), process.stderr.read())
            returncode = await process.wait()
        return {
            "detector_live": detector_live,
            "returncode": returncode,
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
        }

    previous_root = worker_exec.ENABLED_CHECKOUT_ROOT
    previous_homes = worker_exec.HOME_ROOT
    worker_exec.ENABLED_CHECKOUT_ROOT = checkout_root
    worker_exec.HOME_ROOT = home_root
    result_read, result_write = os.pipe()
    controller_pid = os.fork()
    if controller_pid == 0:
        os.close(result_read)
        try:
            os.setgroups([1002])
            os.setresgid(1001, 1001, 1001)
            os.setresuid(1001, 1001, 1001)
            payload = json.dumps(asyncio.run(controller_run())).encode()
            os.write(result_write, payload)
            os._exit(0)
        except BaseException as exc:
            os.write(result_write, json.dumps({"error": repr(exc)}).encode())
            os._exit(1)

    os.close(result_write)
    controller_reaped = False
    try:
        connection, _ = listener.accept()
        _pid, uid, _gid = struct.unpack(
            "3i",
            connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
        )
        assert uid == 1001
        worker_exec.handle_connection(connection)
        _, status = os.waitpid(controller_pid, 0)
        controller_reaped = True
        result = json.loads(os.read(result_read, 65536))
        assert os.waitstatus_to_exitcode(status) == 0, result
        assert result["detector_live"] is True
        assert result["returncode"] == 23
        assert result["stdout"] == "home-ok\n"
        assert result["stderr"] == ""
        assert not (home_root / "sibling").exists()
        assert not (home_root / "sibling-dir").exists()
        assert not (home_root / "renamed").exists()
        assert canary.read_text() == "original"
    finally:
        if not controller_reaped:
            try:
                os.kill(controller_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(controller_pid, 0)
        worker_exec.ENABLED_CHECKOUT_ROOT = previous_root
        worker_exec.HOME_ROOT = previous_homes
        os.close(result_read)
        listener.close()
        shutil.rmtree(boundary, ignore_errors=True)
