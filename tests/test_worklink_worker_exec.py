from __future__ import annotations

import array
import asyncio
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import signal
import socket
import stat
import struct
import subprocess
import sys
import uuid

import pytest

from mimir.worklink.checkout import CheckoutAuthorization, _mint_checkout_authorization
from mimir.worklink.worker_client import (
    EXECUTOR_PROTOCOL_IDENTITY,
    MAX_PROJECTION_BYTES,
    StaleWorkerExecutorError,
    WorkerClient,
    WorkerProcess,
    WorkerProjection,
    verify_executor_identity,
)
import mimir.worklink.worker_exec as worker_exec


def _issued(tmp_path: Path) -> Path:
    path = tmp_path / "checkouts" / ("a" * 64) / "41-2" / "checkout"
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
            asyncio.run(client.launch(local_checkout=path, argv=["true"], env={}, identifier="job", timeout_s=1))
        identifier = str(uuid.uuid4())
        with pytest.raises(ValueError, match="HOME"):
            asyncio.run(client.launch(local_checkout=path, argv=["true"], env={"HOME": "/tmp"}, identifier=identifier, timeout_s=1))
        with pytest.raises(ValueError, match="non-empty"):
            asyncio.run(client.launch(local_checkout=path, argv=[], env={}, identifier=identifier, timeout_s=1))


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
                timeout_s=1,
            )
    assert sent == []


@pytest.mark.asyncio
async def test_client_reports_executor_peer_uid_refusal(tmp_path: Path, monkeypatch) -> None:
    path = _issued(tmp_path)

    class Peer:
        def connect(self, _path: str) -> None:
            pass

        def getsockopt(self, *args: object) -> bytes:
            return struct.pack("3i", 123, 0, 0)

        def sendmsg(self, *args: object) -> None:
            pass

        def recv(self, _size: int) -> bytes:
            return json.dumps({
                "id": None,
                "error": "worker executor refused peer uid 1000; required mimir uid is 1001",
            }).encode()

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "SO_PEERCRED", getattr(socket, "SO_PEERCRED", 17), raising=False)
    monkeypatch.setattr(socket, "socket", lambda *args: Peer())
    with _authorization(path) as checkout:
        with pytest.raises(RuntimeError, match="peer uid 1000.*mimir uid is 1001"):
            await WorkerClient(checkout).launch(
                local_checkout=path,
                argv=["true"],
                env={},
                identifier=str(uuid.uuid4()),
                timeout_s=1,
            )


@pytest.mark.asyncio
async def test_identity_probe_accepts_matching_image_executor(monkeypatch) -> None:
    sent: list[dict[str, object]] = []

    class Peer:
        def connect(self, _path: str) -> None:
            pass

        def getsockopt(self, *args: object) -> bytes:
            return struct.pack("3i", 123, 0, 0)

        def send(self, payload: bytes) -> None:
            sent.append(json.loads(payload))

        def recv(self, _size: int) -> bytes:
            return json.dumps({
                "status": "identity",
                "executor_identity": EXECUTOR_PROTOCOL_IDENTITY,
            }).encode()

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "SO_PEERCRED", getattr(socket, "SO_PEERCRED", 17), raising=False)
    monkeypatch.setattr(socket, "socket", lambda *args: Peer())

    await verify_executor_identity(Path("/executor.sock"))

    assert sent == [{
        "version": 1,
        "op": "identity",
        "executor_identity": EXECUTOR_PROTOCOL_IDENTITY,
    }]


@pytest.mark.asyncio
async def test_client_names_stale_old_launch_contract_with_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _issued(tmp_path)
    requests: list[dict[str, object]] = []

    class Peer:
        def connect(self, _path: str) -> None:
            pass

        def getsockopt(self, *args: object) -> bytes:
            return struct.pack("3i", 123, 0, 0)

        def sendmsg(self, buffers: list[bytes], _ancillary: object) -> None:
            requests.append(json.loads(buffers[0]))

        def recv(self, _size: int) -> bytes:
            return json.dumps({
                "id": requests[0]["id"],
                "error": "launch request must carry the exact contract and three FDs",
            }).encode()

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "SO_PEERCRED", getattr(socket, "SO_PEERCRED", 17), raising=False)
    monkeypatch.setattr(socket, "socket", lambda *args: Peer())
    with _authorization(path) as checkout:
        with pytest.raises(StaleWorkerExecutorError, match="rebuild the image and restart"):
            await WorkerClient(checkout).launch(
                local_checkout=path,
                argv=["true"],
                env={},
                identifier=str(uuid.uuid4()),
                timeout_s=30,
            )

    old_launch_fields = {
        "version", "op", "id", "issue", "attempt", "device", "inode",
        "argv", "env", "projections",
    }
    assert set(requests[0]) == old_launch_fields | {"timeout_s", "executor_identity"}
    assert requests[0]["timeout_s"] == 30


@pytest.mark.asyncio
async def test_worker_process_requires_identity_bound_terminal_result(monkeypatch) -> None:
    identifier = str(uuid.uuid4())

    class Peer:
        def recv(self, size: int) -> bytes:
            return json.dumps({"id": str(uuid.uuid4()), "status": "terminal", "exit_code": 0}).encode()

        def close(self) -> None:
            pass

    process = WorkerProcess(identifier, 12, Peer())
    with pytest.raises(RuntimeError, match="invalid terminal"):
        await process.wait()


def test_validate_checkout_refuses_arbitrary_and_replaced_fds(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "checkouts"
    issued = _issued(tmp_path)
    monkeypatch.setattr(worker_exec, "ENABLED_CHECKOUT_ROOT", root)
    monkeypatch.setattr(
        worker_exec,
        "get_identities",
        lambda: SimpleNamespace(
            mimir_uid=os.getuid(), worklink_uid=os.getuid(), worklink_gid=os.getgid()
        ),
    )
    issued.chmod(0o2770)
    issued.parent.chmod(0o700)

    def request(fd: int) -> dict[str, object]:
        observed = os.fstat(fd)
        return {
            "device": observed.st_dev,
            "inode": observed.st_ino,
            "issue": 41,
            "attempt": 2,
        }

    arbitrary = tmp_path / "other" / ("a" * 64) / "41-2" / "checkout"
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

    issued.chmod(0o2770)
    issued.parent.chmod(0o710)
    fd = os.open(issued, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(worker_exec.os, "readlink", lambda _name: str(issued))
    try:
        with pytest.raises(RuntimeError, match="isolation boundary"):
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


def test_repo_test_uv_cache_seed_copies_cache_and_tolerates_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "shared"
    source.mkdir()
    (source / "seed.whl").write_text("cached", encoding="utf-8")
    (source / "seed-link.whl").symlink_to("seed.whl")
    # Set the modes explicitly rather than inheriting the process umask. The
    # seeder rejects a group- or other-writable source, which is the property it
    # exists to enforce, and the contained repo_test runner executes as the
    # worklink account with umask 0002 -- so an inherited 0664 makes this
    # fixture fail the very check it is meant to exercise. Only the real bits
    # matter here; the symlink's own mode is not inspected.
    (source / "seed.whl").chmod(0o444)
    source.chmod(0o555)
    monkeypatch.setattr(worker_exec, "REPO_TEST_UV_CACHE", source)

    destination = worker_exec._seed_repo_test_uv_cache(tmp_path / "home")

    assert (destination / "seed.whl").read_text(encoding="utf-8") == "cached"
    assert not (destination / "seed-link.whl").is_symlink()
    assert (destination / "seed-link.whl").read_text(encoding="utf-8") == "cached"
    assert not (source / "miss.whl").exists()

    monkeypatch.setattr(worker_exec, "REPO_TEST_UV_CACHE", tmp_path / "absent")
    missing_destination = worker_exec._seed_repo_test_uv_cache(tmp_path / "cold-home")
    assert missing_destination == tmp_path / "cold-home" / ".cache" / "uv"
    assert not missing_destination.exists()


def _identity_can_access(path: Path, uid: int, gid: int, permissions: int) -> bool:
    observed = path.stat(follow_symlinks=False)
    shift = 6 if observed.st_uid == uid else 3 if observed.st_gid == gid else 0
    return ((stat.S_IMODE(observed.st_mode) >> shift) & permissions) == permissions


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires procfs")
def test_uv_execution_copy_normalizes_for_runner_without_relaxing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "boundary" / "checkout"
    nested = source / "nested"
    nested.mkdir(parents=True)
    readable = nested / "readable.txt"
    readable.write_text("runner input", encoding="utf-8")
    executable = source / "run"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    external = tmp_path / "external"
    external.write_text("outside", encoding="utf-8")
    link = source / "link"
    link.symlink_to(external)
    source.chmod(0o700)
    nested.chmod(0o700)
    readable.chmod(0o600)
    executable.chmod(0o700)
    external.chmod(0o600)

    runner_uid = os.getuid() + 1
    runner_gid = os.getgid()
    source_before = {
        path.relative_to(source): stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        for path in (source, *source.rglob("*"))
    }
    # This is the measured pre-fix copy: copytree preserves the 0700 boundary mode,
    # so an identity represented only by its group cannot traverse it.
    unnormalized = tmp_path / "unnormalized"
    shutil.copytree(source, unnormalized, symlinks=True)
    assert not _identity_can_access(unnormalized, runner_uid, runner_gid, 0o5)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(
        worker_exec,
        "get_identities",
        lambda: SimpleNamespace(
            mimir_uid=os.getuid(), worklink_uid=runner_uid, worklink_gid=runner_gid
        ),
    )
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    try:
        execution_fd = worker_exec._execution_checkout_fd(["uv", "run"], source_fd, home)
    finally:
        os.close(source_fd)
    os.close(execution_fd)

    project = home / "project"
    copied_readable = project / "nested" / "readable.txt"
    copied_executable = project / "run"
    copied_link = project / "link"
    assert _identity_can_access(project, runner_uid, runner_gid, 0o5)
    assert _identity_can_access(project / "nested", runner_uid, runner_gid, 0o5)
    assert _identity_can_access(copied_readable, runner_uid, runner_gid, 0o4)
    assert copied_readable.read_text(encoding="utf-8") == "runner input"
    assert stat.S_IMODE(copied_readable.stat().st_mode) == 0o660
    assert stat.S_IMODE(copied_executable.stat().st_mode) == 0o770
    assert all(
        stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) & 0o007 == 0
        for path in (project, project / "nested", copied_readable, copied_executable)
    )
    assert copied_link.is_symlink()
    assert copied_link.readlink() == external
    assert external.read_text(encoding="utf-8") == "outside"
    assert stat.S_IMODE(external.stat().st_mode) == 0o600
    assert {
        path.relative_to(source): stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        for path in (source, *source.rglob("*"))
    } == source_before
    assert stat.S_IMODE(source.stat().st_mode) == 0o700
    assert not _identity_can_access(source, runner_uid, runner_gid, 0o5)

    worker_exec._cleanup_home(home)
    assert not home.exists()


def test_cleanup_home_tolerates_entry_removed_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    victim = home / "worker.lock"
    victim.write_text("lock\n")
    real_unlink = os.unlink
    raced = False

    def unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        nonlocal raced
        if not raced and os.fsdecode(path) == victim.name:
            raced = True
            real_unlink(path, dir_fd=dir_fd)
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(worker_exec.os, "unlink", unlink)
    worker_exec._cleanup_home(home)

    assert raced
    assert not home.exists()


def test_repo_test_local_runner_selects_fd_sourced_execution_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied: list[tuple[str, Path, bool]] = []
    normalized: list[int] = []
    monkeypatch.setattr(
        worker_exec.shutil,
        "copytree",
        lambda source, destination, *, symlinks: copied.append(
            (source, destination, symlinks)
        ),
    )
    monkeypatch.setattr(worker_exec.os, "open", lambda *_args, **_kwargs: 29)
    monkeypatch.setattr(
        worker_exec,
        "_normalize_checkout_fd",
        lambda fd, **_kwargs: normalized.append(fd),
    )

    result = worker_exec._execution_checkout_fd(
        ["./.venv/bin/pytest", "-q"],
        17,
        tmp_path / "home",
        checkout_root=worker_exec.REPO_TEST_CHECKOUT_ROOT,
    )

    assert result == 29
    assert copied == [("/proc/self/fd/17", tmp_path / "home" / "project", True)]
    assert normalized == [29]


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires procfs")
def test_repo_test_execution_copy_is_fd_sourced_for_checkout_local_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "boundary" / "checkout"
    venv = source / ".venv" / "bin"
    venv.mkdir(parents=True)
    runner = venv / "pytest"
    runner.write_text("#!/bin/sh\necho fd-anchored\n", encoding="utf-8")
    source.chmod(0o2770)
    (source / ".venv").chmod(0o2770)
    venv.chmod(0o2770)
    runner.chmod(0o770)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(
        worker_exec,
        "get_identities",
        lambda: SimpleNamespace(
            mimir_uid=os.getuid(), worklink_uid=os.getuid(), worklink_gid=os.getgid()
        ),
    )

    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    try:
        execution_fd = worker_exec._execution_checkout_fd(
            ["./.venv/bin/pytest", "-q"],
            source_fd,
            home,
            checkout_root=worker_exec.REPO_TEST_CHECKOUT_ROOT,
        )
    finally:
        os.close(source_fd)
    try:
        assert os.path.samefile(f"/proc/self/fd/{execution_fd}", home / "project")
        assert not os.path.samefile(f"/proc/self/fd/{execution_fd}", source)
        completed = subprocess.run(
            ["./.venv/bin/pytest", "-q"],
            preexec_fn=lambda: os.fchdir(execution_fd),
            capture_output=True,
            text=True,
            check=False,
        )
        assert (completed.returncode, completed.stdout, completed.stderr) == (
            0,
            "fd-anchored\n",
            "",
        )
    finally:
        os.close(execution_fd)

    copied_venv = home / "project" / ".venv"
    copied_runner = copied_venv / "bin" / "pytest"
    assert stat.S_IMODE(copied_venv.stat().st_mode) == 0o2770
    assert stat.S_IMODE(copied_runner.stat().st_mode) == 0o770
    assert stat.S_IMODE(copied_venv.stat().st_mode) & 0o007 == 0
    assert stat.S_IMODE(copied_runner.stat().st_mode) & 0o007 == 0


def test_executor_rejects_mismatched_launch_protocol_identity() -> None:
    request = {
        "version": 1,
        "op": "launch",
        "executor_identity": "stale-controller-protocol",
        "id": str(uuid.uuid4()),
        "issue": 41,
        "attempt": 2,
        "device": 1,
        "inode": 2,
        "argv": ["true"],
        "env": {},
        "projections": [],
        "timeout_s": 30,
    }

    with pytest.raises(RuntimeError, match=worker_exec._STALE_EXECUTOR_DIAGNOSTIC):
        worker_exec._handle_launch(object(), request, [-1, -1, -1])


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
    responses: list[dict[str, object]] = []

    class Connection:
        closed = False

        def getsockopt(self, *args: object) -> bytes:
            return struct.pack("3i", 12, 999, 999)

        def send(self, payload: bytes) -> None:
            responses.append(json.loads(payload))

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
    monkeypatch.setattr(
        worker_exec,
        "get_identities",
        lambda: SimpleNamespace(mimir_uid=1001, worklink_uid=1002, worklink_gid=1002),
    )
    monkeypatch.setattr(worker_exec, "HOME_ROOT", tmp_path / "homes")
    monkeypatch.setattr(worker_exec.os, "chown", lambda *args: None)
    monkeypatch.setattr(worker_exec.os, "chmod", lambda *args: None)
    monkeypatch.setattr(worker_exec.threading, "Thread", lambda *args, **kwargs: dispatched.append((args, kwargs)))
    with pytest.raises(RuntimeError, match="stop"):
        worker_exec.serve(tmp_path / "socket")
    assert connection.closed
    assert dispatched == []
    assert responses == [{
        "id": None,
        "error": "worker executor refused peer uid 999; required mimir uid is 1001",
    }]


def test_drop_worker_uses_irreversible_identity_sequence(
    monkeypatch, synthetic_worklink_identities
) -> None:
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
    worker_uid = synthetic_worklink_identities.worklink_uid
    worker_gid = synthetic_worklink_identities.worklink_gid
    assert ("groups", []) in events
    assert ("gid", (worker_gid, worker_gid, worker_gid)) in events
    assert ("uid", (worker_uid, worker_uid, worker_uid)) in events
    assert events.index(("groups", [])) < events.index(
        ("gid", (worker_gid, worker_gid, worker_gid))
    )
    assert events.index(("gid", (worker_gid, worker_gid, worker_gid))) < events.index(
        ("uid", (worker_uid, worker_uid, worker_uid))
    )
    assert events[-1] == ("verify",)
    assert events.count(("caps", set())) == 1


def test_worker_identity_verifier_rejects_any_retained_authority(
    monkeypatch, synthetic_worklink_identities
) -> None:
    worker_uid = synthetic_worklink_identities.worklink_uid
    worker_gid = synthetic_worklink_identities.worklink_gid
    monkeypatch.setattr(
        worker_exec.os, "getresuid", lambda: (worker_uid,) * 3, raising=False
    )
    monkeypatch.setattr(
        worker_exec.os, "getresgid", lambda: (worker_gid,) * 3, raising=False
    )
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


def test_duplicate_worker_id_is_refused_before_popen_without_touching_live_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = str(uuid.uuid4())

    class Incumbent:
        pass

    incumbent = Incumbent()
    request = {
        "version": 1,
        "op": "launch",
        "executor_identity": worker_exec.EXECUTOR_PROTOCOL_IDENTITY,
        "id": identifier,
        "issue": 41,
        "attempt": 2,
        "device": 0,
        "inode": 0,
        "argv": ["worker"],
        "env": {},
        "projections": [],
        "timeout_s": 1,
    }
    monkeypatch.setattr(worker_exec, "_validate_checkout", lambda *args: None)
    monkeypatch.setattr(
        worker_exec.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("duplicate launch reached Popen"),
    )
    with worker_exec._jobs_lock:
        worker_exec._jobs[identifier] = incumbent  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="already active"):
            worker_exec._handle_launch(object(), request, [0, 1, 2])  # type: ignore[arg-type]
        with worker_exec._jobs_lock:
            assert worker_exec._jobs[identifier] is incumbent
    finally:
        with worker_exec._jobs_lock:
            worker_exec._jobs.pop(identifier, None)


def test_terminal_waits_for_in_group_writers_before_cleanup(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    checkout_fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_write = os.open(stdout_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    stderr_write = os.open(stderr_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
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
        assert stdout_path.read_bytes() == b"ready"
        events.append("cleanup")
        cleanup_home(home)

    monkeypatch.setattr(worker_exec, "_cleanup_home", observed_cleanup)
    request = {
        "version": 1,
        "op": "launch",
        "executor_identity": worker_exec.EXECUTOR_PROTOCOL_IDENTITY,
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
        "timeout_s": 5,
    }
    fds = [checkout_fd, stdout_write, stderr_write]
    try:
        worker_exec._handle_launch(Connection(), request, fds)
        assert events == ["started", "cleanup", "terminal"]
        assert responses[-1]["exit_code"] == 0
        assert not (tmp_path / "homes" / identifier).exists()
        assert stderr_path.read_bytes() == b""
    finally:
        for fd in fds:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def test_executor_enforces_worker_deadline(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    checkout_fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)
    stdout_write = os.open(tmp_path / "stdout.log", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    stderr_write = os.open(tmp_path / "stderr.log", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    identifier = str(uuid.uuid4())
    responses: list[dict[str, object]] = []
    waits: list[float | None] = []

    class Connection:
        def send(self, payload: bytes) -> None:
            responses.append(json.loads(payload))

    class Process:
        pid = 4321
        returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired(["worker"], timeout)
            assert self.returncode is not None
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode

    process = Process()
    monkeypatch.setattr(worker_exec, "HOME_ROOT", tmp_path / "homes")
    worker_exec.HOME_ROOT.mkdir()
    monkeypatch.setattr(worker_exec, "_validate_checkout", lambda *args: None)
    monkeypatch.setattr(worker_exec, "_project_home", lambda *args: None)
    monkeypatch.setattr(worker_exec.os, "chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker_exec.os, "chmod", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker_exec,
        "_execution_checkout_fd",
        lambda _command, anchored_fd, _home, **_kwargs: os.dup(anchored_fd),
    )
    monkeypatch.setattr(worker_exec.subprocess, "Popen", lambda *args, **kwargs: process)

    def terminate(observed: Process, timeout_s: float = 5.0) -> None:
        assert observed is process
        process.returncode = -signal.SIGKILL
        process.wait()

    monkeypatch.setattr(worker_exec, "_terminate_process_group", terminate)
    request = {
        "version": 1,
        "op": "launch",
        "executor_identity": worker_exec.EXECUTOR_PROTOCOL_IDENTITY,
        "id": identifier,
        "issue": 41,
        "attempt": 2,
        "device": 0,
        "inode": 0,
        "argv": ["worker"],
        "env": {},
        "projections": [],
        "timeout_s": 0.25,
    }
    fds = [checkout_fd, stdout_write, stderr_write]
    try:
        worker_exec._handle_launch(Connection(), request, fds)
        assert waits[0] == 0.25 + worker_exec._CONTROLLER_CANCELLATION_GRACE_S
        assert responses[-1] == {
            "id": identifier,
            "status": "terminal",
            "exit_code": -signal.SIGKILL,
            "timed_out": True,
        }
    finally:
        for fd in fds:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def test_process_group_cancellation_reports_unreapable_member(monkeypatch) -> None:
    waits: list[tuple[int, float]] = []
    signals: list[int] = []

    monkeypatch.setattr(
        worker_exec,
        "_wait_process_group",
        lambda process_group, deadline: (
            waits.append((process_group, deadline)) or False
        ),
    )
    monkeypatch.setattr(
        worker_exec, "_process_group_has_live_members", lambda _process_group: True
    )
    monkeypatch.setattr(
        worker_exec.os,
        "killpg",
        lambda _process_group, sent_signal: signals.append(sent_signal),
    )

    with pytest.raises(RuntimeError, match="still has live members after SIGKILL"):
        worker_exec._terminate_process_group_pid(4321, timeout_s=0)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert len(waits) == 2
    assert all(deadline is not None for _process_group, deadline in waits)


def test_worker_payload_cannot_reach_controller_canary_and_detector_is_live() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires Linux root to exercise the executor identity boundary")

    boundary = Path("/tmp") / f"mimir-worker-exec-{uuid.uuid4()}"
    checkout_root = boundary / "checkouts"
    checkout = checkout_root / ("a" * 64) / "41-2" / "checkout"
    home_root = boundary / "homes"
    controller_home = boundary / "mimir-home"
    socket_path = boundary / "executor.sock"
    checkout.mkdir(parents=True)
    home_root.mkdir(mode=0o710)
    os.chown(home_root, 0, 1002)
    controller_home.mkdir(mode=0o700)
    os.chown(checkout, 1001, 1002)
    checkout.chmod(0o2770)
    os.chown(checkout.parent, 1001, 1002)
    checkout.parent.chmod(0o700)
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
                timeout_s=5,
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


def test_worker_cannot_cross_attempt_boundary_and_negative_control_is_live() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("the shipped-image proof exercises this real uid boundary in CI")

    boundary = Path("/tmp") / f"mimir-worker-siblings-{uuid.uuid4()}"
    checkout_root = boundary / "checkouts"
    repo_root = checkout_root / ("a" * 64)
    first = repo_root / "41-1" / "checkout"
    second = repo_root / "42-1" / "checkout"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    canary = second / "sibling-canary"
    canary.write_text("original")
    repo_root.chmod(0o710)
    os.chown(repo_root, 1001, 1002)
    for attempt in (first.parent, second.parent):
        os.chown(attempt, 1001, 1002)
    for checkout in (first, second):
        os.chown(checkout, 1001, 1002)
        checkout.chmod(0o2770)
    os.chown(canary, 1001, 1002)
    canary.chmod(0o660)

    def run_worker() -> dict[str, bool]:
        checkout_fd = os.open(first, os.O_RDONLY | os.O_DIRECTORY)
        result_read, result_write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(result_read)
            try:
                os.setgroups([])
                os.setresgid(1002, 1002, 1002)
                os.setresuid(1002, 1002, 1002)
                os.fchdir(checkout_fd)
                Path("own-write").write_text("owned")
                relative = Path("../../42-1/checkout/sibling-canary")
                observed: dict[str, bool] = {"own_write": True}
                for name, target in (("relative", relative), ("absolute", canary)):
                    try:
                        target.read_text()
                        observed[f"{name}_read"] = True
                    except OSError:
                        observed[f"{name}_read"] = False
                    try:
                        target.write_text("attacked")
                        observed[f"{name}_write"] = True
                    except OSError:
                        observed[f"{name}_write"] = False
                    try:
                        target.unlink()
                        observed[f"{name}_delete"] = True
                    except OSError:
                        observed[f"{name}_delete"] = False
                os.write(result_write, json.dumps(observed).encode())
                os._exit(0)
            except BaseException as exc:
                os.write(result_write, json.dumps({"error": repr(exc)}).encode())
                os._exit(1)

        os.close(result_write)
        os.close(checkout_fd)
        payload = json.loads(os.read(result_read, 65536))
        os.close(result_read)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0, payload
        return payload

    try:
        # Negative control: these are the pre-fix executable attempt parents.
        first.parent.chmod(0o710)
        second.parent.chmod(0o710)
        vulnerable = run_worker()
        assert vulnerable["relative_read"] is True
        assert vulnerable["relative_write"] is True
        assert vulnerable["relative_delete"] is True

        canary.write_text("original")
        os.chown(canary, 1001, 1002)
        canary.chmod(0o660)
        first.parent.chmod(0o700)
        second.parent.chmod(0o700)
        isolated = run_worker()
        assert isolated == {
            "own_write": True,
            "relative_read": False,
            "relative_write": False,
            "relative_delete": False,
            "absolute_read": False,
            "absolute_write": False,
            "absolute_delete": False,
        }
        assert canary.read_text() == "original"
        assert (first / "own-write").read_text() == "owned"
    finally:
        shutil.rmtree(boundary, ignore_errors=True)


@pytest.mark.parametrize(
    ("root_name", "issue", "attempt"),
    [
        ("checkouts", 41, 2),
        ("repo-test-checkouts", 41, 7),
        ("opencode-checkouts", 9, 1),
    ],
)
def test_executor_accepts_only_the_three_issued_checkout_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_name: str,
    issue: int,
    attempt: int,
) -> None:
    roots = {
        "checkouts": tmp_path / "checkouts",
        "repo-test-checkouts": tmp_path / "repo-test-checkouts",
        "opencode-checkouts": tmp_path / "opencode-checkouts",
    }
    for root in roots.values():
        root.mkdir()
    monkeypatch.setattr(worker_exec, "ENABLED_CHECKOUT_ROOT", roots["checkouts"])
    monkeypatch.setattr(worker_exec, "REPO_TEST_CHECKOUT_ROOT", roots["repo-test-checkouts"])
    monkeypatch.setattr(worker_exec, "OPENCODE_CHECKOUT_ROOT", roots["opencode-checkouts"])
    monkeypatch.setattr(
        worker_exec,
        "get_identities",
        lambda: SimpleNamespace(
            mimir_uid=os.getuid(), worklink_uid=os.getuid(), worklink_gid=os.getgid()
        ),
    )
    path = roots[root_name] / ("a" * 64) / f"{issue}-{attempt}" / "checkout"
    path.mkdir(parents=True)
    path.parent.chmod(0o700)
    path.chmod(0o2770)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    observed = os.fstat(fd)
    real_readlink = os.readlink
    monkeypatch.setattr(
        worker_exec.os,
        "readlink",
        lambda value: str(path) if str(value).startswith("/proc/self/fd/") else real_readlink(value),
    )
    try:
        accepted_root = worker_exec._validate_checkout(
            fd,
            {
                "device": observed.st_dev,
                "inode": observed.st_ino,
                "issue": issue,
                "attempt": attempt,
            },
        )
        assert accepted_root == roots[root_name]
    finally:
        os.close(fd)


def test_executor_refuses_a_fourth_checkout_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = [tmp_path / name for name in ("checkouts", "repo-test", "opencode")]
    for root in roots:
        root.mkdir()
    monkeypatch.setattr(worker_exec, "ENABLED_CHECKOUT_ROOT", roots[0])
    monkeypatch.setattr(worker_exec, "REPO_TEST_CHECKOUT_ROOT", roots[1])
    monkeypatch.setattr(worker_exec, "OPENCODE_CHECKOUT_ROOT", roots[2])
    path = tmp_path / "fourth" / ("a" * 64) / "41-2" / "checkout"
    path.mkdir(parents=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    observed = os.fstat(fd)
    real_readlink = os.readlink
    monkeypatch.setattr(
        worker_exec.os,
        "readlink",
        lambda value: str(path) if str(value).startswith("/proc/self/fd/") else real_readlink(value),
    )
    try:
        with pytest.raises(RuntimeError, match="exact issued"):
            worker_exec._validate_checkout(
                fd,
                {
                    "device": observed.st_dev,
                    "inode": observed.st_ino,
                    "issue": 41,
                    "attempt": 2,
                },
            )
    finally:
        os.close(fd)
