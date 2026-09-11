from __future__ import annotations

import array
import asyncio
import errno
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
from unittest.mock import Mock, call

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
from mimir.worklink import identities


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
@pytest.mark.parametrize("factory", [False, True])
async def test_path_client_requests_worker_uid_and_projects_provider_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory: bool,
    synthetic_worklink_identities,
) -> None:
    checkout = tmp_path / ".worklink" / "repo" / "41-2"
    checkout.mkdir(parents=True)
    requests: list[dict[str, object]] = []
    fd_counts: list[int] = []

    class Peer:
        def connect(self, _path: str) -> None:
            pass

        def getsockopt(self, *args: object) -> bytes:
            return struct.pack("3i", 123, 0, 0)

        def sendmsg(self, buffers: list[bytes], ancillary: list[tuple[int, int, bytes]]) -> None:
            requests.append(json.loads(buffers[0]))
            rights = ancillary[0][2]
            fd_counts.append(len(rights))

        def recv(self, _size: int) -> bytes:
            return json.dumps({
                "id": requests[0]["id"],
                "status": "started",
                "pid": 123,
            }).encode()

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "SO_PEERCRED", getattr(socket, "SO_PEERCRED", 17), raising=False)
    monkeypatch.setattr(socket, "socket", lambda *args: Peer())
    monkeypatch.setattr(identities, "get_identities", lambda: synthetic_worklink_identities)
    client = (
        WorkerClient.for_factory_checkout(checkout, issue_id=41, attempt=2)
        if factory else WorkerClient.for_path_checkout(
            checkout, issue_id=41, attempt=2, run_uid=1002
        )
    )
    process = await client.launch(
        local_checkout=checkout,
        argv=["opencode", "run", "--dir", "."],
        env={"OPENCODE_PERMISSION": "{}"},
        projections=[
            WorkerProjection(".config/opencode/opencode.json", b'{"model":"provider/model"}'),
            WorkerProjection(".local/share/opencode/auth.json", b'{"provider":{"token":"x"}}'),
        ],
        identifier=str(uuid.uuid4()),
        timeout_s=30,
    )
    process._socket.close()

    request = requests[0]
    assert request["op"] == ("launch_factory" if factory else "launch_path")
    assert request["path"] == str(checkout)
    assert request["run_uid"] == (synthetic_worklink_identities.worklink_uid if factory else 1002)
    assert set(request) == worker_exec._PATH_LAUNCH_FIELDS
    assert fd_counts == [2]
    assert [item["path"] for item in request["projections"]] == [
        ".config/opencode/opencode.json",
        ".local/share/opencode/auth.json",
    ]


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
                "source_commit": "a" * 40,
            }).encode()

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "SO_PEERCRED", getattr(socket, "SO_PEERCRED", 17), raising=False)
    monkeypatch.setattr(socket, "socket", lambda *args: Peer())

    assert await verify_executor_identity(Path("/executor.sock")) == "a" * 40

    assert sent == [{
        "version": 1,
        "op": "identity",
        "executor_identity": EXECUTOR_PROTOCOL_IDENTITY,
    }]


@pytest.mark.asyncio
async def test_identity_probe_refuses_missing_or_malformed_source_commit(monkeypatch) -> None:
    class Peer:
        def connect(self, _path: str) -> None:
            pass

        def getsockopt(self, *args: object) -> bytes:
            return struct.pack("3i", 123, 0, 0)

        def send(self, _payload: bytes) -> None:
            pass

        def recv(self, _size: int) -> bytes:
            return json.dumps({
                "status": "identity",
                "executor_identity": EXECUTOR_PROTOCOL_IDENTITY,
                "source_commit": "dirty-or-unpinned",
            }).encode()

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "SO_PEERCRED", getattr(socket, "SO_PEERCRED", 17), raising=False)
    monkeypatch.setattr(socket, "socket", lambda *args: Peer())

    with pytest.raises(StaleWorkerExecutorError, match="stale root executor image"):
        await verify_executor_identity(Path("/executor.sock"))


def test_executor_identity_reports_image_recorded_source_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_commit = tmp_path / "executor-source-commit"
    source_commit.write_text("b" * 40 + "\n", encoding="ascii")
    monkeypatch.setattr(worker_exec, "EXECUTOR_SOURCE_COMMIT_PATH", source_commit)

    assert worker_exec._executor_source_commit() == "b" * 40

    source_commit.write_text("dirty\n", encoding="ascii")
    with pytest.raises(RuntimeError, match="invalid recorded source commit"):
        worker_exec._executor_source_commit()


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
    assert set(requests[0]) == old_launch_fields | {
        "timeout_s",
        "stdout_limit",
        "stderr_limit",
        "executor_identity",
    }
    assert requests[0]["timeout_s"] == 30
    assert requests[0]["stdout_limit"] == 1
    assert requests[0]["stderr_limit"] == 1


@pytest.mark.asyncio
async def test_path_client_names_unsupported_operation_as_stale_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / ".worklink" / "repo" / "41-2"
    checkout.mkdir(parents=True)

    class Peer:
        def connect(self, _path: str) -> None:
            pass

        def getsockopt(self, *args: object) -> bytes:
            return struct.pack("3i", 123, 0, 0)

        def sendmsg(self, _buffers: list[bytes], _ancillary: object) -> None:
            pass

        def recv(self, _size: int) -> bytes:
            return json.dumps({
                "id": None,
                "error": "unsupported worker operation",
            }).encode()

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "SO_PEERCRED", getattr(socket, "SO_PEERCRED", 17), raising=False)
    monkeypatch.setattr(socket, "socket", lambda *args: Peer())
    client = WorkerClient.for_path_checkout(
        checkout, issue_id=41, attempt=2, run_uid=1002
    )

    with pytest.raises(StaleWorkerExecutorError, match="rebuild the image and restart"):
        await client.launch(
            local_checkout=checkout,
            argv=["true"],
            env={},
            identifier=str(uuid.uuid4()),
            timeout_s=30,
        )


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


def test_path_checkout_accepts_group_checkout_without_0700_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".worklink"
    checkout = root / "repo" / "41-2"
    checkout.mkdir(parents=True)
    checkout.chmod(0o2770)
    checkout.parent.chmod(0o755)
    monkeypatch.setattr(worker_exec, "WORKLINK_CHECKOUT_ROOT", root)
    monkeypatch.setattr(
        worker_exec,
        "get_identities",
        lambda: SimpleNamespace(
            mimir_uid=os.getuid(), worklink_uid=1002, worklink_gid=os.getgid()
        ),
    )

    fd = worker_exec._open_path_checkout({
        "path": str(checkout),
        "issue": 41,
        "attempt": 2,
        "run_uid": 1002,
    })
    try:
        observed = os.fstat(fd)
        expected = os.stat(checkout)
        assert (observed.st_dev, observed.st_ino) == (expected.st_dev, expected.st_ino)
        assert stat.S_IMODE(checkout.parent.stat().st_mode) == 0o755
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

    # The execution copy is owned by the RUNNER, so the runner is this process --
    # an unprivileged test cannot chown to any other uid. The CONTROLLER is the
    # foreign identity here, which is the inverse of the pre-ownership-change model.
    runner_uid = os.getuid()
    runner_gid = os.getgid()
    controller_uid = os.getuid() + 1
    # An identity that shares only the group, used to prove normalization still
    # widens group access rather than relying on ownership alone.
    group_only_uid = os.getuid() + 1
    source_before = {
        path.relative_to(source): stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        for path in (source, *source.rglob("*"))
    }
    # This is the measured pre-normalization copy: copytree preserves the 0700
    # boundary mode, so an identity represented only by its group cannot traverse it.
    unnormalized = tmp_path / "unnormalized"
    shutil.copytree(source, unnormalized, symlinks=True)
    assert not _identity_can_access(unnormalized, group_only_uid, runner_gid, 0o5)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(
        worker_exec,
        "get_identities",
        lambda: SimpleNamespace(
            mimir_uid=controller_uid, worklink_uid=runner_uid, worklink_gid=runner_gid
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
    # The runner OWNS the copy: that is what lets a repository's own provisioning
    # chmod a tracked file, which group-write alone can never confer.
    assert project.stat().st_uid == runner_uid
    assert _identity_can_access(project, runner_uid, runner_gid, 0o5)
    assert _identity_can_access(project / "nested", runner_uid, runner_gid, 0o5)
    assert _identity_can_access(copied_readable, runner_uid, runner_gid, 0o4)
    # Normalization still widens group access, not just owner access.
    assert _identity_can_access(project, group_only_uid, runner_gid, 0o5)
    assert _identity_can_access(copied_readable, group_only_uid, runner_gid, 0o4)
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
    assert not _identity_can_access(source, group_only_uid, runner_gid, 0o5)

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


@pytest.mark.parametrize(
    ("command", "surface"),
    [
        (["npm", "run", "test:ci"], "repo_test"),
        (["uv", "run", "pytest", "-q"], "worklink"),
    ],
)
def test_execution_copy_is_owned_by_the_runner_not_the_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_worklink_identities,
    command: list[str],
    surface: str,
) -> None:
    """The ephemeral execution tree must be chowned to the uid that runs in it.

    ``chmod`` is owner-only, so a controller-owned copy cannot complete any
    provisioning step that sets a mode on a file the runner did not create --
    ``npm ci`` marking a workspace ``bin`` executable is the case that surfaced
    this. Nothing reads this copy back, so runner ownership costs no reach.
    """
    identities = synthetic_worklink_identities
    assert identities.worklink_uid != identities.mimir_uid
    owners: list[tuple[int, int]] = []
    monkeypatch.setattr(
        worker_exec.shutil,
        "copytree",
        lambda source, destination, *, symlinks: None,
    )
    monkeypatch.setattr(worker_exec.os, "open", lambda *_args, **_kwargs: 29)
    monkeypatch.setattr(
        worker_exec,
        "_normalize_checkout_fd",
        lambda _fd, *, owner_uid, group_gid: owners.append((owner_uid, group_gid)),
    )

    checkout_root = (
        worker_exec.REPO_TEST_CHECKOUT_ROOT
        if surface == "repo_test"
        else worker_exec.WORKLINK_CHECKOUT_ROOT
    )
    result = worker_exec._execution_checkout_fd(
        command, 17, tmp_path / "home", checkout_root=checkout_root
    )

    assert result == 29
    assert owners == [(identities.worklink_uid, identities.worklink_gid)]


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
        "stdout_limit": 100,
        "stderr_limit": 100,
    }

    with pytest.raises(RuntimeError, match=worker_exec._STALE_EXECUTOR_DIAGNOSTIC):
        worker_exec._handle_launch(object(), request, [-1, -1, -1])


@pytest.mark.parametrize("operation,handler", [
    ("launch_path", "_handle_launch"),
    ("launch_factory", "_handle_launch_factory"),
])
def test_executor_dispatches_path_launch_through_the_connection_handler(
    monkeypatch: pytest.MonkeyPatch, operation: str, handler: str,
) -> None:
    identifier = str(uuid.uuid4())
    request = {"version": 1, "op": operation, "id": identifier}
    dispatched: list[tuple[dict[str, object], list[int]]] = []

    class Connection:
        def recvmsg(self, *args: object) -> tuple[bytes, list[object], int, None]:
            return json.dumps(request).encode(), [], 0, None

        def send(self, _data: bytes) -> None:
            raise AssertionError("dispatch must not return an error")

        def close(self) -> None:
            pass

    def handle(_connection: object, observed: dict[str, object], fds: list[int]) -> None:
        dispatched.append((observed, fds))

    monkeypatch.setattr(worker_exec, handler, handle)
    worker_exec.handle_connection(Connection())

    assert dispatched == [(request, [])]


def test_executor_rejects_fd_count_and_extra_request_fields() -> None:
    identifier = str(uuid.uuid4())
    payload = json.dumps({"version": 1, "op": "launch", "id": identifier, "uid": 0}).encode()

    class Connection:
        responses: list[dict[str, object]] = []

        def recvmsg(self, *args: object) -> tuple[bytes, list[object], int, None]:
            return payload, [], 0, None

        def send(self, data: bytes, flags: int = 0) -> None:
            assert flags == socket.MSG_DONTWAIT
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
        "stdout_limit": 100,
        "stderr_limit": 100,
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
    monitor = worker_exec._wait_with_output_limits

    def after_leader_exit(proc, *args):
        # Deadline enforcement has its own controlled test. Here the protocol
        # ceiling owns interpreter startup; retain the real group cleanup path.
        proc.wait()
        return monitor(proc, *args)

    monkeypatch.setattr(worker_exec, "_wait_with_output_limits", after_leader_exit)
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
            sys.executable,
            "-c",
            "import os, signal; r,w=os.pipe(); pid=os.fork(); "
            "exec(\"if pid == 0:\\n"
            " signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
            " os.write(1, b'ready')\\n"
            " os.write(w, b'R')\\n"
            " while True: signal.pause()\\n"
            "else:\\n"
            " assert os.read(r, 1) == b'R'\\n\")",
        ],
        "env": {"PATH": "/usr/bin:/bin"},
        "projections": [],
        "timeout_s": 5,
        "stdout_limit": 100,
        "stderr_limit": 100,
    }
    fds = [checkout_fd, stdout_write, stderr_write]
    try:
        worker_exec._handle_launch(Connection(), request, fds)
        assert events == ["started", "cleanup", "terminal"]
        assert responses[-1]["exit_code"] == 0
        assert not (tmp_path / "homes" / identifier).exists()
        assert stderr_path.read_bytes() == b""
    finally:
        if responses:
            try:
                os.killpg(int(responses[0]["pid"]), signal.SIGKILL)
            except ProcessLookupError:
                pass
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
    times = iter((0.0, 100.0))
    monkeypatch.setattr(worker_exec.time, "monotonic", lambda: next(times))
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
        "stdout_limit": 100,
        "stderr_limit": 100,
    }
    fds = [checkout_fd, stdout_write, stderr_write]
    try:
        worker_exec._handle_launch(Connection(), request, fds)
        assert waits == [None]
        assert responses[-1] == {
            "id": identifier,
            "status": "terminal",
            "exit_code": -signal.SIGKILL,
            "timed_out": True,
            "output_overflow": False,
        }
    finally:
        for fd in fds:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def test_executor_truncates_and_terminates_on_output_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_fd = os.open(stdout_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    stderr_fd = os.open(stderr_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(stdout_fd, b"overflow")

    class Process:
        pid = 4321
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

    process = Process()

    def terminate(observed: Process, timeout_s: float = 5.0) -> None:
        assert observed is process
        process.returncode = -signal.SIGKILL

    monkeypatch.setattr(worker_exec, "_terminate_process_group", terminate)
    try:
        assert worker_exec._wait_with_output_limits(
            process, 30, stdout_fd, 4, stderr_fd, 4
        ) == (-signal.SIGKILL, False, True)
        assert stdout_path.read_bytes() == b"over"
    finally:
        os.close(stdout_fd)
        os.close(stderr_fd)



def test_arm_parent_death_signal_closes_pre_prctl_race(monkeypatch) -> None:
    calls: list[tuple[int, int, int, int, int]] = []

    class Libc:
        def prctl(self, *args: int) -> int:
            calls.append(args)
            return 0

    monkeypatch.setattr(worker_exec.ctypes, "CDLL", lambda *args, **kwargs: Libc())
    monkeypatch.setattr(worker_exec.os, "getppid", lambda: 99)
    exits: list[int] = []
    monkeypatch.setattr(worker_exec.os, "_exit", lambda code: exits.append(code))

    worker_exec._arm_parent_death_signal(100)

    if sys.platform.startswith("linux"):
        assert calls == [(1, signal.SIGKILL, 0, 0, 0)]
        assert exits == [128 + signal.SIGKILL]
    else:
        # No portable PDEATHSIG equivalent exists. Launch remains available, with
        # controller cancellation/reaping but no sudden-parent-death guarantee.
        assert calls == []
        assert exits == []


def test_arm_parent_death_signal_is_noop_off_linux(monkeypatch) -> None:
    monkeypatch.setattr(worker_exec, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        worker_exec.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-Linux fallback must not load libc prctl")
        ),
    )

    worker_exec._arm_parent_death_signal(100)


@pytest.mark.parametrize("error", [errno.EPERM, errno.ESRCH, errno.EINVAL], ids=["EPERM", "ESRCH", "EINVAL"])
def test_terminate_process_group_sigterm_guard(monkeypatch, error) -> None:
    failure = OSError(error, os.strerror(error))
    killpg = Mock(side_effect=failure)
    wait = Mock(return_value=True)
    monkeypatch.setattr(worker_exec.os, "killpg", killpg)
    monkeypatch.setattr(worker_exec, "_wait_process_group", wait)
    monkeypatch.setattr(worker_exec, "_process_group_has_live_members", Mock(return_value=False))

    if error == errno.EINVAL:
        with pytest.raises(OSError) as raised:
            worker_exec._terminate_process_group_pid(4321, timeout_s=0)
        assert raised.value is failure
        wait.assert_not_called()
    else:
        assert worker_exec._terminate_process_group_pid(4321, timeout_s=0) is None
        wait.assert_called_once()
        assert wait.call_args.args[0] == 4321
    killpg.assert_called_once_with(4321, signal.SIGTERM)


@pytest.mark.parametrize("error", [errno.EPERM, errno.ESRCH, errno.EINVAL], ids=["EPERM", "ESRCH", "EINVAL"])
def test_terminate_process_group_sigkill_guard(monkeypatch, error) -> None:
    failure = OSError(error, os.strerror(error))
    killpg = Mock(side_effect=[None, failure])
    wait = Mock(side_effect=[False, True])
    monkeypatch.setattr(worker_exec.os, "killpg", killpg)
    monkeypatch.setattr(worker_exec, "_wait_process_group", wait)
    monkeypatch.setattr(worker_exec, "_process_group_has_live_members", Mock(return_value=True))

    if error == errno.EINVAL:
        with pytest.raises(OSError) as raised:
            worker_exec._terminate_process_group_pid(4321, timeout_s=0)
        assert raised.value is failure
        assert wait.call_count == 1
    else:
        assert worker_exec._terminate_process_group_pid(4321, timeout_s=0) is None
        assert wait.call_count == 2
        assert all(args.args[0] == 4321 for args in wait.call_args_list)
    assert killpg.call_args_list == [call(4321, signal.SIGTERM), call(4321, signal.SIGKILL)]


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


@pytest.fixture
def factory_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / ".worklink"
    checkout = root / "repo" / "41-2" / "checkout"
    checkout.mkdir(parents=True)
    root.chmod(0o755)
    (root / "repo").chmod(0o755)
    checkout.parent.chmod(0o2700)
    checkout.chmod(0o2770)
    worker_uid = worker_exec.get_identities().worklink_uid
    monkeypatch.setattr(worker_exec, "get_identities", lambda: SimpleNamespace(
        mimir_uid=os.getuid(), worklink_uid=worker_uid, worklink_gid=os.getgid(),
    ))
    monkeypatch.setattr(worker_exec, "WORKLINK_CHECKOUT_ROOT", root)
    monkeypatch.setattr(worker_exec, "HOME_ROOT", tmp_path / "homes")
    worker_exec.HOME_ROOT.mkdir()
    return {
        "version": 1, "op": "launch_factory",
        "executor_identity": EXECUTOR_PROTOCOL_IDENTITY,
        "id": str(uuid.uuid4()), "issue": 41, "attempt": 2,
        "path": str(checkout), "run_uid": worker_uid,
        "argv": ["factory-payload"], "env": {}, "projections": [],
        "timeout_s": 5, "stdout_limit": 4096, "stderr_limit": 4096,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("coding_enabled", [False, True])
async def test_factory_compute_requires_executor_without_agent_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coding_enabled: bool,
) -> None:
    from mimir.worklink import compute

    monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: coding_enabled)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Mock(side_effect=AssertionError("agent spawn")))
    monkeypatch.setattr(compute, "_fd_anchored_opencode_argv", Mock(side_effect=AssertionError("leaf argv handler")))
    launches = []

    class Client:
        async def launch(self, **kwargs):
            launches.append(kwargs)
            raise RuntimeError("executor unavailable")

    def factory_client(path, *, issue_id, attempt):
        assert (path, issue_id, attempt) == (tmp_path, 41, 2)
        return Client()

    monkeypatch.setattr(WorkerClient, "for_factory_checkout", factory_client)
    spec = compute.WorkSpec(
        issue_id=41, attempt=2, repo_url="", base_ref="", branch="", prompt="",
        rules=None, test_command="", backend="feature_factory", timeout_s=10,
        local_checkout=tmp_path, local_argv=("factory", "--dir", "factory-specific"),
    )
    with pytest.raises(compute.ComputeLaunchError, match="executor unavailable"):
        await compute.LocalSubprocessComputeBackend().launch(spec)
    assert len(launches) == 1
    assert launches[0]["argv"] == spec.local_argv
    assert "HOME" not in launches[0]["env"]


@pytest.mark.parametrize("denial", [
    "root_uid", "controller_uid", "outside", "symlink", "issue", "attempt",
    "mode", "owner", "group", "extra", "fd_count", "identity", "home",
])
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_factory_launch_denies_invalid_contract(
    factory_request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denial: str, platform: str,
) -> None:
    monkeypatch.setattr(worker_exec, "sys", SimpleNamespace(platform=platform))
    request = factory_request
    checkout = Path(request["path"])
    expected = "ownership or mode"
    if denial in {"root_uid", "controller_uid"}:
        request["run_uid"] = 0 if denial == "root_uid" else os.getuid()
        expected = "invalid worker uid"
    elif denial == "outside":
        request["path"] = str(tmp_path)
        expected = "outside"
    elif denial == "symlink":
        link = checkout.parent / "link"
        link.symlink_to(checkout)
        request["path"] = str(link)
        expected = "shape"
    elif denial in {"issue", "attempt"}:
        request[denial] += 1
        expected = "shape"
    elif denial == "mode":
        checkout.chmod(0o770)
    elif denial in {"owner", "group"}:
        expected = "boundary|ancestor"
        observed = worker_exec.get_identities()
        monkeypatch.setattr(worker_exec, "get_identities", lambda: SimpleNamespace(
            mimir_uid=observed.mimir_uid + (denial == "owner"),
            worklink_uid=observed.worklink_uid,
            worklink_gid=observed.worklink_gid + (denial == "group"),
        ))
    elif denial in {"extra", "fd_count"}:
        if denial == "extra":
            request["uid"] = 0
        expected = "exact contract"
    elif denial == "identity":
        request["executor_identity"] = "old-executor"
        expected = "stale root executor"
    elif denial == "home":
        request["env"] = {"HOME": "/controller"}
        expected = "HOME"
    monkeypatch.setattr(worker_exec.subprocess, "Popen", Mock(side_effect=AssertionError("payload ran")))
    with (tmp_path / "stdout").open("w+b") as stdout, (tmp_path / "stderr").open("w+b") as stderr:
        fds = [stdout.fileno(), stderr.fileno()]
        if denial == "fd_count":
            fds.append(-1)
        try:
            with pytest.raises(RuntimeError, match=expected):
                worker_exec._handle_launch_factory(object(), request, fds)
        finally:
            if len(fds) == 3 and fds[0] != stdout.fileno():
                os.close(fds[0])


def test_factory_launch_refuses_non_linux_before_spawn(factory_request, tmp_path, monkeypatch):
    monkeypatch.setattr(worker_exec, "sys", SimpleNamespace(platform="darwin", executable=sys.executable))
    transfer = Mock(side_effect=AssertionError("non-Linux launch transferred checkout"))
    monkeypatch.setattr(worker_exec, "_normalize_checkout_fd", transfer)
    spawn = Mock(side_effect=AssertionError("non-Linux factory reached supervisor spawn"))
    monkeypatch.setattr(worker_exec.subprocess, "Popen", spawn)
    with (tmp_path / "stdout").open("w+b") as stdout, (tmp_path / "stderr").open("w+b") as stderr:
        fds = [stdout.fileno(), stderr.fileno()]
        try:
            with pytest.raises(RuntimeError, match="PR_SET_CHILD_SUBREAPER requires Linux"):
                worker_exec._handle_launch_factory(Mock(), factory_request, fds)
        finally:
            if len(fds) == 3:
                os.close(fds[0])
    spawn.assert_not_called()
    transfer.assert_not_called()
    path = Path(factory_request["path"])
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o2700
    assert path.stat().st_uid == os.getuid()
    assert factory_request["id"] not in worker_exec._jobs
    assert factory_request["id"] not in worker_exec._launching
    assert not (worker_exec.HOME_ROOT / factory_request["id"]).exists()


@pytest.mark.parametrize("guard", ["boundary_write", "ancestor_write", "ancestor_link", "boundary_link", "checkout_link", "legacy", "control_before_transfer"])
def test_factory_transfer_refuses_unsafe_boundary(factory_request, monkeypatch, guard):
    path = Path(factory_request["path"])
    if guard == "boundary_write":
        path.parent.chmod(0o2770)
    elif guard == "ancestor_write":
        path.parent.parent.chmod(0o777)
    elif guard in {"ancestor_link", "boundary_link", "checkout_link"}:
        original = {"ancestor_link": path.parent.parent, "boundary_link": path.parent, "checkout_link": path}[guard]
        moved = original.with_name(original.name + "-moved")
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
    elif guard == "legacy":
        factory_request["path"] = str(path.parent)
    else:
        factory_request["op"] = "launch_factory_control"
    transfer = Mock(side_effect=AssertionError("unsafe tree reached privileged traversal"))
    monkeypatch.setattr(worker_exec, "_normalize_checkout_fd", transfer)
    with pytest.raises((RuntimeError, OSError)):
        worker_exec._open_factory_checkout(factory_request)
    transfer.assert_not_called()


def test_factory_transfer_failure_keeps_boundary_private(factory_request, monkeypatch):
    path = Path(factory_request["path"])

    def fail(fd, **kwargs):
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o2700
        assert os.fstat(fd).st_ino == path.stat().st_ino
        raise RuntimeError("transfer failed")

    monkeypatch.setattr(worker_exec, "_normalize_checkout_fd", fail)
    with pytest.raises(RuntimeError, match="transfer failed"):
        worker_exec._open_factory_checkout(factory_request)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o2700


@pytest.mark.parametrize("held_lock", ["shared", "exclusive"])
def test_factory_boundary_lock_is_nonblocking(factory_request, monkeypatch, held_lock):
    import fcntl

    path = Path(factory_request["path"])
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if held_lock == "shared" else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        real_flock = fcntl.flock

        def bounded_flock(fd, operation):
            assert operation & fcntl.LOCK_NB, "executor must not block on a worker-held lock"
            return real_flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", bounded_flock)
        monkeypatch.setattr(worker_exec, "_normalize_checkout_fd", Mock(side_effect=AssertionError("locked boundary reached transfer")))
        with pytest.raises(BlockingIOError):
            worker_exec._open_factory_checkout(factory_request)
    finally:
        os.close(fd)


@pytest.mark.parametrize("guard", [
    "ancestor_uid", "ancestor_world_write", "ancestor_group_write",
    "boundary_uid", "boundary_gid", "boundary_mode",
    "checkout_uid", "checkout_gid", "checkout_mode",
])
def test_factory_metadata_guards_are_independent(factory_request, monkeypatch, guard):
    path = Path(factory_request["path"])
    target = path.parent.parent if guard.startswith("ancestor") else path.parent if guard.startswith("boundary") else path
    target_identity = (target.stat().st_dev, target.stat().st_ino)
    checkout_identity = (path.stat().st_dev, path.stat().st_ino)
    real_fstat = os.fstat

    def fstat(fd):
        value = real_fstat(fd)
        identity = (value.st_dev, value.st_ino)
        changed = dict(st_uid=value.st_uid, st_gid=value.st_gid, st_mode=value.st_mode)
        if identity == target_identity:
            if guard.endswith("uid"):
                changed["st_uid"] = os.getuid() + 10000
            elif guard.endswith("gid"):
                changed["st_gid"] = os.getgid() + 10000
            else:
                modes = {"ancestor_world_write": 0o757, "ancestor_group_write": 0o775,
                         "boundary_mode": 0o2752, "checkout_mode": 0o2777}
                changed["st_mode"] = stat.S_IFDIR | modes[guard]
            return SimpleNamespace(**changed)
        if guard == "boundary_mode" and identity == checkout_identity:
            # An exposed tree must otherwise satisfy the recovery owner check.
            changed["st_uid"] = factory_request["run_uid"]
            return SimpleNamespace(**changed)
        return value

    monkeypatch.setattr(os, "fstat", fstat)
    transfer = Mock()
    monkeypatch.setattr(worker_exec, "_normalize_checkout_fd", transfer)
    expected = "ancestor" if guard.startswith("ancestor") else "boundary" if guard.startswith("boundary") else "ownership or mode"
    with pytest.raises(RuntimeError, match=expected):
        fd = worker_exec._open_factory_checkout(factory_request)
        os.close(fd)
    transfer.assert_not_called()


@pytest.mark.parametrize("guard", ["outside", "canonical", "repo_name", "repo_parent", "leaf_name", "worker_uid", "issue", "attempt"])
def test_factory_path_guards_reject_otherwise_openable_trees(factory_request, monkeypatch, tmp_path, guard):
    path = Path(factory_request["path"])
    if guard == "outside":
        moved = tmp_path / "foreign" / "repo"
        moved.parent.mkdir(mode=0o755)
        path.parent.parent.rename(moved)
        raw = str(moved / "41-2/checkout")
    elif guard == "canonical":
        raw = str(path.parent) + "//checkout"
    elif guard == "repo_name":
        moved = path.parent.parent.with_name("repo space")
        path.parent.parent.rename(moved)
        raw = str(moved / "41-2/checkout")
    elif guard == "repo_parent":
        path.parent.rename(tmp_path / "41-2")
        raw = str(worker_exec.WORKLINK_CHECKOUT_ROOT) + "/../41-2/checkout"
    elif guard == "worker_uid":
        factory_request["run_uid"] = 0
        raw = str(path)
    elif guard in {"issue", "attempt"}:
        factory_request[guard] += 1
        raw = str(path)
    else:
        raw = str(path.with_name("other"))
    factory_request["path"] = raw
    monkeypatch.setattr(worker_exec, "_normalize_checkout_fd", Mock())
    with pytest.raises(RuntimeError, match="outside|shape|invalid worker uid"):
        fd = worker_exec._open_factory_checkout(factory_request)
        os.close(fd)


def test_factory_recovery_checkout_must_be_directory(factory_request, monkeypatch):
    path = Path(factory_request["path"])
    path.rmdir()
    path.write_text("not a directory")
    path.chmod(0o2770)
    path.parent.chmod(0o2750)
    real_fstat = os.fstat
    identity = (path.stat().st_dev, path.stat().st_ino)

    def fstat(fd):
        value = real_fstat(fd)
        if (value.st_dev, value.st_ino) == identity:
            return SimpleNamespace(st_uid=factory_request["run_uid"], st_gid=value.st_gid, st_mode=value.st_mode)
        return value

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(NotADirectoryError):
        fd = worker_exec._open_factory_checkout(factory_request)
        os.close(fd)


def test_factory_control_launch_uses_worker_drop_without_runtime_refresh(factory_request, monkeypatch, tmp_path):
    factory_request["op"] = "launch_factory_control"
    factory_request["argv"] = ["uv", "run", "status"]
    opened = []

    def checkout(request, *, for_launch=False):
        assert for_launch is True
        fd = os.open(request["path"], os.O_RDONLY | os.O_DIRECTORY)
        opened.append(fd)
        return fd

    monkeypatch.setattr(worker_exec, "_open_factory_checkout", checkout)
    # No supervisor is started in this contract test; do not require the host's
    # Linux seqpacket transport merely to inspect the selected uid-drop hook.
    monkeypatch.setattr(worker_exec.socket, "socketpair", lambda *args: (Mock(), Mock()))
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)
    drop = Mock()
    monkeypatch.setattr(worker_exec, "_drop_worker", drop)
    monkeypatch.setattr(worker_exec, "_drop_factory", Mock(side_effect=AssertionError("control refreshed runtime auth")))
    monkeypatch.setattr(worker_exec, "_execution_checkout_fd", Mock(side_effect=AssertionError("factory used disposable copy")))

    def spawn(command, **kwargs):
        kwargs["preexec_fn"]()
        drop.assert_called_once_with(kwargs["pass_fds"][0])
        return SimpleNamespace(pid=123)

    def wait(proc, *args):
        proc.done.set()
        return 0, False, False

    monkeypatch.setattr(worker_exec.subprocess, "Popen", spawn)
    monkeypatch.setattr(worker_exec, "_wait_with_output_limits", wait)
    with (tmp_path / "out").open("w+b") as output:
        fds = [output.fileno(), output.fileno()]
        try:
            worker_exec._handle_launch_factory(Mock(), factory_request, fds)
        finally:
            if len(fds) == 3:
                os.close(fds[0])
    assert len(opened) == 1


@pytest.mark.parametrize("owner_valid", [False, True])
def test_factory_recovery_requires_worker_owner_without_privileged_walk(factory_request, monkeypatch, owner_valid):
    path = Path(factory_request["path"])
    path.parent.chmod(0o2750)
    (path / "hostile-link").symlink_to("/etc/shadow")
    os.mkfifo(path / "hostile-fifo")
    real_fstat = os.fstat
    inode = path.stat().st_ino
    worker_uid = factory_request["run_uid"]

    def fstat(fd):
        value = real_fstat(fd)
        if value.st_ino == inode and owner_valid:
            return SimpleNamespace(st_uid=worker_uid, st_gid=value.st_gid, st_mode=value.st_mode)
        return value

    monkeypatch.setattr(worker_exec.os, "fstat", fstat)
    transfer = Mock(side_effect=AssertionError("recovery traversed worker tree as root"))
    monkeypatch.setattr(worker_exec, "_normalize_checkout_fd", transfer)
    if owner_valid:
        fd = worker_exec._open_factory_checkout(factory_request)
        os.close(fd)
    else:
        with pytest.raises(RuntimeError, match="ownership or mode"):
            worker_exec._open_factory_checkout(factory_request)
    transfer.assert_not_called()


def test_factory_cancel_uses_supervisor_stop_not_legacy_group(monkeypatch):
    identifier = str(uuid.uuid4())
    done = Mock()
    done.is_set.return_value = False
    done.wait.return_value = True
    channel = Mock()
    process = Mock(pid=123)
    proc = worker_exec._FactoryProcess(process, channel, Mock(), done=done)
    monkeypatch.setitem(worker_exec._jobs, identifier, proc)
    legacy = Mock(side_effect=AssertionError("factory cancellation used legacy group signalling"))
    monkeypatch.setattr(worker_exec, "_terminate_process_group_pid", legacy)
    worker_exec._cancel(identifier)
    channel.send.assert_called_once_with(b"stop", socket.MSG_DONTWAIT)
    done.wait.assert_called_once_with(worker_exec._FACTORY_STOP_TIMEOUT_S + worker_exec._PROCESS_REAP_TIMEOUT_S)
    process.wait.assert_not_called()
    legacy.assert_not_called()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux factory launch requires PR_SET_CHILD_SUBREAPER")
def test_factory_drops_identity_before_payload_exec_or_spawn(
    factory_request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = worker_exec.get_identities()
    events = []
    transfer = Mock()
    monkeypatch.setattr(worker_exec, "_normalize_checkout_fd", transfer)
    monkeypatch.setattr(worker_exec, "_execution_checkout_fd", Mock(side_effect=AssertionError("factory copied to HOME")))
    monkeypatch.setattr(worker_exec.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace(prctl=lambda *a: 0))
    monkeypatch.setattr(worker_exec, "_set_capabilities", lambda caps: None)
    monkeypatch.setattr(worker_exec, "_last_capability", lambda: 2)
    monkeypatch.setattr(os, "setgroups", lambda groups: None)
    monkeypatch.setattr(os, "setresgid", lambda *ids: events.append(("gid", ids)), raising=False)

    def setresuid(*ids):
        assert ids == (observed.worklink_uid,) * 3
        assert events == [("gid", (observed.worklink_gid,) * 3)]
        events.append(("uid", ids))

    monkeypatch.setattr(os, "setresuid", setresuid, raising=False)
    for name in ("umask", "setsid", "fchdir", "chown"):
        monkeypatch.setattr(os, name, lambda *a, **kw: None)
    monkeypatch.setattr(worker_exec, "_verify_worker_identity", lambda: None)

    monkeypatch.setattr(worker_exec, "_prepare_factory_runtime", lambda home: events.append("runtime"))

    payload = ["factory-payload", "--dir", "a directory", "--", "$(not-a-shell)"]
    factory_request["argv"] = payload.copy()

    def popen(command, **kwargs):
        transfer.assert_called_once()
        assert transfer.call_args.kwargs == {
            "owner_uid": observed.worklink_uid, "group_gid": observed.worklink_gid,
        }
        assert stat.S_IMODE(Path(factory_request["path"]).parent.stat().st_mode) == 0o2750
        supervisor = Path(worker_exec.__file__).with_name("factory_supervisor.py")
        assert supervisor.is_absolute()
        assert command[:3] == [sys.executable, "-I", str(supervisor)]
        assert command[4:] == payload
        assert kwargs["close_fds"] is True
        assert kwargs["pass_fds"][1] == int(command[3])
        assert stat.S_ISSOCK(os.fstat(int(command[3])).st_mode)
        preexec = kwargs.get("preexec_fn")
        assert callable(preexec), "factory supervisor requires preexec identity drop"
        preexec()
        assert events == [
            ("gid", (observed.worklink_gid,) * 3),
            ("uid", (observed.worklink_uid,) * 3),
            "runtime",
        ], "supervisor execution happened before identity drop/runtime setup"
        # supervise() owns prctl-before-payload; its independent tests live in
        # test_worklink_factory_supervisor, not this mocked exec boundary.
        events.append("supervisor-exec")
        return SimpleNamespace(pid=123, returncode=0)

    monkeypatch.setattr(worker_exec.subprocess, "Popen", popen)
    responses = []
    send_flags = []

    def send(data, flags=0):
        responses.append(json.loads(data))
        send_flags.append(flags)

    def wait(proc, *args):
        assert isinstance(proc, worker_exec._FactoryProcess)
        assert worker_exec._jobs[factory_request["id"]] is proc
        proc.emit({"kind": "event", "event": "worklink_factory_orphan_adopted", "pid": 456})
        proc.done.set()
        return 0, False, False

    monkeypatch.setattr(worker_exec, "_wait_with_output_limits", wait)
    with (tmp_path / "stdout").open("w+b") as stdout, (tmp_path / "stderr").open("w+b") as stderr:
        fds = [stdout.fileno(), stderr.fileno()]
        try:
            worker_exec._handle_launch_factory(
                SimpleNamespace(send=send),
                factory_request, fds,
            )
        finally:
            if len(fds) == 3:
                os.close(fds[0])
    assert events[-1] == "supervisor-exec"
    assert factory_request["argv"] == payload
    assert [response["status"] for response in responses] == ["started", "event", "terminal"]
    assert send_flags == [0, socket.MSG_DONTWAIT, socket.MSG_DONTWAIT]
    assert responses[1] == {
        "kind": "event", "event": "worklink_factory_orphan_adopted", "pid": 456,
        "status": "event", "id": factory_request["id"], "run_id": factory_request["id"],
        "issue_id": 41, "attempt": 2,
    }
    assert factory_request["id"] not in worker_exec._jobs
    assert not (worker_exec.HOME_ROOT / factory_request["id"]).exists()


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux subreaper")
@pytest.mark.parametrize("mode", ["complete", "cancel", "timeout", "stdout", "stderr", "death"])
def test_wait_factory_real_supervisor(tmp_path: Path, monkeypatch, mode: str) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from types import SimpleNamespace

    monkeypatch.setattr(worker_exec, "_CONTROLLER_CANCELLATION_GRACE_S", 0)
    monkeypatch.setattr(worker_exec, "_OUTPUT_LIMIT_POLL_S", .001)
    # Select expiry explicitly after payload readiness. Other modes never
    # advance the deadline clock; real protocol I/O keeps its normal semantics.
    monkeypatch.setattr(worker_exec, "time", SimpleNamespace(
        monotonic=lambda: 0, sleep=worker_exec.time.sleep,
    ))
    legacy = Mock(side_effect=AssertionError("factory used legacy process-group signalling"))
    monkeypatch.setattr(worker_exec, "_terminate_process_group_pid", legacy)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    events = []
    ready = tmp_path / "ready"
    payload = "import os, signal; os.write(1, b'ready'); signal.pause()"
    if mode == "complete":
        payload = "import os; os.write(1, b'done'); os._exit(37)"
    elif mode in {"stdout", "stderr"}:
        payload = f"import os, signal; os.write({1 if mode == 'stdout' else 2}, b'x' * 8192); signal.pause()"
    payload = f"from pathlib import Path; Path({str(ready)!r}).touch(); " + payload
    with parent, child, (tmp_path / "stdout").open("w+b") as stdout, (tmp_path / "stderr").open("w+b") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-I", str(Path(worker_exec.__file__).with_name("factory_supervisor.py")),
             # A bad inherited descriptor simulates supervisor startup death,
             # without spawning an orphan that pytest cannot reap.
             "-1" if mode == "death" else str(child.fileno()),
             sys.executable, "-I", "-c", payload],
            pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr,
        )
        child.close()
        proc = worker_exec._FactoryProcess(process, parent, events.append)
        try:
            assert proc.pid == process.pid
            if mode != "death":
                # The supervisor's ready packet precedes Popen; wait for the
                # payload interpreter itself before expiring or cancelling it.
                while not ready.exists():
                    assert process.poll() is None, "supervisor exited before payload readiness"
                    threading.Event().wait(.01)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    worker_exec._wait_factory, proc, 0 if mode == "timeout" else 10,
                    stdout.fileno(), 64, stderr.fileno(), 4096 if mode == "death" else 64,
                )
                if mode == "cancel":
                    worker_exec._terminate_process_group(proc)
                if mode == "death":
                    with pytest.raises(RuntimeError, match="worklink_factory_supervisor_lost: no terminal cleanup report"):
                        future.result()
                    assert proc.returncode is None
                    assert len(events) == 1
                    assert events[0]["event"] == "worklink_factory_supervisor_lost"
                    assert events[0]["error"] == proc.error
                    with pytest.raises(RuntimeError, match="supervisor_lost"):
                        proc.stop()
                else:
                    code, timed_out, overflow = future.result()
                    assert code == (37 if mode == "complete" else -signal.SIGTERM)
                    assert timed_out is (mode == "timeout")
                    assert overflow is (mode in {"stdout", "stderr"})
                    assert proc.returncode == code
                    assert proc.error is None
                    assert process.returncode == 0
                    if mode in {"stdout", "stderr"}:
                        assert (tmp_path / mode).read_bytes() == b"x" * 64
                    if mode == "complete":
                        assert (tmp_path / "stdout").read_bytes() == b"done"
                    proc.stop()
            assert proc.done.is_set()
            assert parent.fileno() == -1
            assert process.poll() is not None
            legacy.assert_not_called()
        finally:
            parent.close()
            if process.poll() is None:
                process.wait()


def test_wait_factory_unreapable_supervisor_has_finite_stop_bound(tmp_path, monkeypatch):
    clock = iter(i * .1 for i in range(30))
    monkeypatch.setattr(worker_exec, "time", SimpleNamespace(
        monotonic=lambda: next(clock), sleep=lambda _: None,
    ))
    monkeypatch.setattr(worker_exec, "_FACTORY_STOP_TIMEOUT_S", .2)
    monkeypatch.setattr(worker_exec, "_PROCESS_REAP_TIMEOUT_S", .3)
    channel = Mock()
    channel.recv.side_effect = BlockingIOError
    process = Mock(pid=123)
    process.wait.side_effect = [subprocess.TimeoutExpired("supervisor", .2), 0]
    events = []
    proc = worker_exec._FactoryProcess(process, channel, events.append)
    legacy = Mock(side_effect=AssertionError("legacy factory signal"))
    monkeypatch.setattr(worker_exec, "_terminate_process_group_pid", legacy)
    proc.request_stop()
    deadline = proc.stop_deadline
    proc.request_stop()
    assert proc.stop_deadline == deadline
    with (tmp_path / "output").open("w+b") as output:
        with pytest.raises(RuntimeError, match="supervisor stop deadline exceeded"):
            worker_exec._wait_factory(proc, 100, output.fileno(), 64, output.fileno(), 64)
    assert proc.done.is_set()
    assert proc.returncode is None
    assert proc.error == "worklink_factory_reap_refused: supervisor failed to exit"
    assert events == [{"kind": "event", "event": "worklink_factory_supervisor_lost",
                       "error": "worklink_factory_reap_refused: supervisor stop deadline exceeded"}]
    process.wait.assert_has_calls([call(timeout=.2), call(timeout=.3)])
    process.kill.assert_called_once_with()
    channel.close.assert_called_once_with()
    legacy.assert_not_called()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux factory executor uses SOCK_SEQPACKET")
@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["terminal", "error", "identity", "invalid", "eof"])
async def test_worker_process_forwards_factory_events_to_owned_logger(tmp_path, monkeypatch, ending):
    from mimir import event_logger

    path = tmp_path / "events.jsonl"
    logger = event_logger.EventLogger(path, session_id="factory-test")
    monkeypatch.setattr(event_logger, "get_logger", lambda: logger)
    identifier = str(uuid.uuid4())
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with parent, child:
        process = WorkerProcess(identifier, 123, parent)
        names = ["worklink_factory_orphan_adopted", "worklink_factory_reap_refused",
                 "worklink_factory_supervisor_lost"]
        for name in names:
            child.send(json.dumps({
                "id": identifier, "status": "event", "event": name,
                "run_id": identifier, "issue_id": 41, "attempt": 2, "pid": 456,
                "error": "cleanup diagnostic", "untrusted_extra": "must not forward",
            }).encode())
        packet = {"id": identifier, "status": "terminal", "exit_code": 37,
                  "timed_out": True, "output_overflow": True}
        expected = None
        if ending == "error":
            packet = {"id": identifier, "error": "cleanup refused"}
            expected = "cleanup refused"
        elif ending in {"identity", "invalid"}:
            packet = {"id": str(uuid.uuid4()) if ending == "identity" else identifier,
                      "status": "event", "event": names[0] if ending == "identity" else "arbitrary"}
            expected = "invalid terminal/event identity" if ending == "identity" else "invalid event"
        elif ending == "eof":
            expected = "closed before terminal result"
        if ending != "eof":
            child.send(json.dumps(packet).encode())
        child.close()
        if expected:
            with pytest.raises(RuntimeError, match=expected):
                await process.wait()
            assert process.returncode is None
        else:
            assert await process.wait() == 37
            assert await process.wait() == 37
            assert process.timed_out and process.output_overflow
            assert parent.fileno() == -1
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 3
    assert [record["type"] for record in records] == names
    for record in records:
        assert record["run_id"] == identifier
        assert record["issue_id"] == 41
        assert record["attempt"] == 2
        assert record["pid"] == 456
        assert record["error"] == "cleanup diagnostic"
        assert "untrusted_extra" not in record


def test_factory_stop_refuses_missing_monitor_acknowledgement(monkeypatch):
    monkeypatch.setattr(worker_exec, "_FACTORY_STOP_TIMEOUT_S", .2)
    monkeypatch.setattr(worker_exec, "_PROCESS_REAP_TIMEOUT_S", .3)
    done = Mock()
    done.is_set.return_value = False
    done.wait.return_value = False
    channel = Mock()
    channel.send.side_effect = BrokenPipeError
    proc = worker_exec._FactoryProcess(Mock(pid=123), channel, Mock(), done=done)
    with pytest.raises(RuntimeError, match="worklink_factory_reap_refused: supervisor did not finish"):
        proc.stop()
    done.wait.assert_called_once_with(.5)
    channel.send.assert_called_once_with(b"stop", socket.MSG_DONTWAIT)
    assert proc.stop_deadline is not None
    done.is_set.return_value = True
    proc.request_stop()
    assert channel.send.call_count == 1


@pytest.mark.skipif(sys.platform != "linux", reason="Linux factory executor uses SOCK_SEQPACKET")
@pytest.mark.parametrize("event", ["worklink_factory_orphan_adopted", "worklink_factory_reap_refused"])
def test_wait_factory_forwards_supervisor_event_before_terminal(tmp_path, event):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    packet = {"kind": "event", "event": event, "pid": 456, "error": "diagnostic"}
    events = []
    process = Mock(pid=123)
    process.wait.return_value = 0
    proc = worker_exec._FactoryProcess(process, parent, events.append)
    with parent, child, (tmp_path / "output").open("w+b") as output:
        for response in ({"kind": "ready"}, packet, {"kind": "terminal", "exit_code": 37}):
            child.send(json.dumps(response).encode())
        child.close()
        assert worker_exec._wait_factory(proc, 10, output.fileno(), 64, output.fileno(), 64) == (37, False, False)
    assert events == [packet]
    assert proc.done.is_set()
    assert proc.error is None
    assert proc.returncode == 37


@pytest.mark.skipif(sys.platform != "linux", reason="Linux factory executor uses SOCK_SEQPACKET")
def test_factory_connection_error_does_not_block_on_full_controller_socket(tmp_path, monkeypatch):
    import threading
    import time

    full = threading.Event()
    received_fds = []
    sent = []
    identifier = str(uuid.uuid4())

    def launch(connection, request, fds):
        assert request["id"] == identifier
        received_fds.extend(fds)
        packet = json.dumps({"id": identifier, "status": "event",
                             "event": "worklink_factory_orphan_adopted", "pid": 456}).encode()
        try:
            for _ in range(10000):
                connection.send(packet, socket.MSG_DONTWAIT)
                sent.append(packet)
        except BlockingIOError:
            full.set()
            raise
        raise AssertionError("fixture failed to fill executor send buffer")

    monkeypatch.setattr(worker_exec, "_handle_launch_factory", launch)
    controller, executor = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with controller, executor, (tmp_path / "stdout").open("w+b") as stdout, (tmp_path / "stderr").open("w+b") as stderr:
        executor.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        controller.sendmsg(
            [json.dumps({"version": 1, "op": "launch_factory", "id": identifier}).encode()],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [stdout.fileno(), stderr.fileno()]))],
        )
        thread = threading.Thread(target=worker_exec.handle_connection, args=(executor,), daemon=True)
        thread.start()
        packets = []
        try:
            assert full.wait(2), "launch did not reach downstream event backpressure"
            # Keep the peer open and undrained until the handler has finished.
            thread.join(timeout=1)
            assert not thread.is_alive(), "error reporting blocked on the full controller socket"
            assert executor.fileno() == -1
            assert len(received_fds) == 2
            for fd in received_fds:
                with pytest.raises(OSError) as closed:
                    os.fstat(fd)
                assert closed.value.errno == errno.EBADF
            assert stat.S_ISREG(os.fstat(stdout.fileno()).st_mode)
            assert stat.S_ISREG(os.fstat(stderr.fileno()).st_mode)
        finally:
            # A mutation that restores blocking error sends must fail the join,
            # not leave a wedged handler behind. Drain even on assertion failure.
            controller.settimeout(.1)
            deadline = time.monotonic() + 3
            try:
                while time.monotonic() < deadline:
                    try:
                        packet = controller.recv(4096)
                    except socket.timeout:
                        continue
                    if not packet:
                        break
                    packets.append(json.loads(packet))
            finally:
                controller.close()
                thread.join(timeout=2)
            assert not thread.is_alive(), "fixture could not unstick handler by draining peer"
        assert packets
        assert len(packets) == len(sent)
        assert all(packet["status"] == "event" for packet in packets)
        assert not any(packet.get("status") == "terminal" for packet in packets)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux factory executor uses SOCK_SEQPACKET")
@pytest.mark.parametrize(
    "ready,terminal,supervisor_exit,diagnostic",
    [
        (False, {"exit_code": 0}, 0, "invalid cleanup report"),
        (True, {}, 0, "invalid cleanup report"),
        (True, {"exit_code": None}, 0, "invalid cleanup report"),
        (True, {"exit_code": True}, 0, "invalid cleanup report"),
        (True, {"exit_code": "0"}, 0, "invalid cleanup report"),
        (True, {"exit_code": 0.0}, 0, "invalid cleanup report"),
        (True, {"exit_code": 0}, 1, "abnormal supervisor exit"),
        (True, {"exit_code": 0}, -signal.SIGKILL, "abnormal supervisor exit"),
        (True, {"exit_code": 0, "error": "children survived"}, 0, "worklink_factory_reap_refused: children survived"),
    ],
    ids=["not-ready", "missing-code", "null-code", "bool-code", "string-code", "float-code",
         "failed-supervisor", "killed-supervisor", "refused"],
)
def test_wait_factory_rejects_invalid_terminal(tmp_path, ready, terminal, supervisor_exit, diagnostic):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    process = Mock(pid=123)
    process.wait.return_value = supervisor_exit
    events = []
    proc = worker_exec._FactoryProcess(process, parent, events.append)
    with parent, child, (tmp_path / "output").open("w+b") as output:
        if ready:
            child.send(json.dumps({"kind": "ready"}).encode())
        child.send(json.dumps({"kind": "terminal", **terminal}).encode())
        child.close()
        with pytest.raises(RuntimeError, match=diagnostic):
            worker_exec._wait_factory(proc, 10, output.fileno(), 64, output.fileno(), 64)
        assert parent.fileno() == -1
    assert proc.done.is_set()
    assert proc.returncode is None
    assert diagnostic in proc.error
    assert events == [{"kind": "event", "event": "worklink_factory_supervisor_lost", "error": proc.error}]
    process.kill.assert_not_called()


@pytest.mark.parametrize("git_intake", [False, True], ids=["canary", "git-intake"])
def test_factory_descendant_cannot_write_controller_canary_and_negative_control_is_live(
    monkeypatch: pytest.MonkeyPatch,
    git_intake: bool,
) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires Linux root, unavailable in unprivileged sandboxes")
    from mimir.worklink.compute import LocalSubprocessComputeBackend, WorkSpec

    observed = worker_exec.get_identities()
    monkeypatch.setattr(identities, "get_identities", lambda: observed)
    # Probe kernel authority in a disposable child, not the pytest process.
    fd = os.open("/tmp", os.O_RDONLY | os.O_DIRECTORY)
    probe = os.fork()
    if probe == 0:
        try:
            worker_exec._drop_worker(fd)
        except PermissionError:
            os._exit(77)
        except BaseException:
            os._exit(1)
        os._exit(0)
    os.close(fd)
    _, status = os.waitpid(probe, 0)
    if os.waitstatus_to_exitcode(status) == 77:
        pytest.skip("Linux root sandbox lacks authority for the real worker identity drop")
    assert os.waitstatus_to_exitcode(status) == 0

    repo = worker_exec.WORKLINK_CHECKOUT_ROOT / f"factory-test-{uuid.uuid4()}"
    boundary = Path("/tmp") / f"factory-exec-{uuid.uuid4()}"
    checkout = repo / "41-2" / "checkout"
    home_root = boundary / "homes"
    controller_home = boundary / "controller"
    socket_path = boundary / "executor.sock"
    try:
        try:
            checkout.mkdir(parents=True)
            repo.chmod(0o755)
            boundary.mkdir(mode=0o755)
            boundary.chmod(0o755)
            home_root.mkdir(mode=0o710)
            os.chown(home_root, 0, observed.worklink_gid)
            home_root.chmod(0o710)
            os.chown(checkout, observed.mimir_uid, observed.worklink_gid)
            checkout.chmod(0o2770)
            os.chown(checkout.parent, observed.mimir_uid, observed.worklink_gid)
            checkout.parent.chmod(0o2700)
            controller_home.mkdir(mode=0o700)
            os.chown(controller_home, observed.mimir_uid, observed.mimir_uid)
        except PermissionError:
            pytest.skip("root sandbox cannot prepare real Worklink checkout ownership")
        canary = controller_home / "canary"
        canary.write_text("original")
        os.chown(canary, observed.mimir_uid, observed.mimir_uid)
        canary.chmod(0o600)
        monkeypatch.setattr(worker_exec, "HOME_ROOT", home_root)
        python = shutil.which("python3", path="/usr/bin:/bin")
        if python is None:
            pytest.skip("requires system python3 accessible to both non-root identities")
        grandchild = (
            "import os, pathlib\n"
            "print('euid=' + str(os.geteuid()), flush=True)\n"
            "try:\n"
            " pathlib.Path(os.environ['CANARY']).write_text('attacked')\n"
            "except PermissionError:\n"
            " print('write-denied', flush=True)\n"
            "else:\n"
            " print('write-allowed', flush=True)\n"
        )
        payload = (
            "import subprocess, sys; "
            f"sys.exit(subprocess.run([sys.executable, '-c', {grandchild!r}], "
            "start_new_session=True, timeout=5).returncode)"
        )
        if git_intake:
            git = shutil.which("git", path="/usr/bin:/bin")
            if git is None:
                pytest.skip("requires system Git")
            sibling = repo / "untrusted"
            sibling.mkdir(mode=0o755)
            for path in (checkout, sibling):
                subprocess.run([git, "init", str(path)], check=True, capture_output=True)
                for entry in (path / ".git").rglob("*"):
                    os.chown(entry, observed.mimir_uid, observed.worklink_gid)
                os.chown(path / ".git", observed.mimir_uid, observed.worklink_gid)
            tracked = checkout / "tracked-bin"
            tracked.write_text("echo factory")
            os.chown(tracked, observed.mimir_uid, observed.worklink_gid)
            for arguments in (
                ["add", "tracked-bin"],
                ["-c", "user.name=Factory Test", "-c", "user.email=factory@example.test", "commit", "-m", "initial"],
            ):
                subprocess.run(
                    [git, "-C", str(checkout), *arguments], check=True, capture_output=True,
                    user=observed.mimir_uid, group=observed.worklink_gid, extra_groups=[],
                    env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
                )
            payload = (
                "import os, subprocess\n"
                f"assert os.geteuid() == {observed.worklink_uid}\n"
                f"git = {git!r}\n"
                "result = subprocess.run([git, 'rev-parse', '--show-toplevel'], capture_output=True, text=True)\n"
                "assert result.returncode == 0, result.stderr\n"
                f"assert result.stdout.strip() == {str(checkout)!r}\n"
                "assert 'GIT_CONFIG_COUNT' not in os.environ\n"
                "from pathlib import Path\n"
                "Path('tracked-bin').chmod(0o755)\n"
                "result = subprocess.run([git, 'clone', '--local', '.', '.factory-sandboxes/run'], capture_output=True, text=True)\n"
                "assert result.returncode == 0, result.stderr\n"
                "Path('.factory-sandboxes/run/run.json').write_text('{\"status\":\"running\"}')\n"
                "Path('.factory-runtime/data/opencode/opencode.db').write_text('retained-session')\n"
                f"result = subprocess.run([git, '-C', {str(sibling)!r}, 'rev-parse', '--show-toplevel'], capture_output=True, text=True)\n"
                "assert result.returncode != 0 and 'dubious ownership' in result.stderr, result\n"
                f"result = subprocess.run([git, 'clone', '--local', {str(sibling)!r}, 'sibling-clone'], capture_output=True, text=True)\n"
                "assert result.returncode != 0, result\n"
            )

        control_launch = False

        async def controller_run():
            client = WorkerClient.for_factory_checkout(
                checkout, issue_id=41, attempt=2, socket_path=socket_path,
            )
            if control_launch:
                client._launch_op = "launch_factory_control"
            backend = LocalSubprocessComputeBackend(
                _worker_client=client,
            )
            spec = WorkSpec(
                issue_id=41, attempt=2, repo_url="", base_ref="", branch="",
                prompt="", rules=None, test_command="", backend="feature_factory",
                timeout_s=10, local_checkout=checkout,
                local_argv=[python, "-c", payload], env={"CANARY": str(canary)},
            )
            handle = await backend.launch(spec)
            result = await backend.wait(handle, timeout_s=10)
            return {"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}

        def run():
            socket_path.unlink(missing_ok=True)
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
                listener.bind(str(socket_path))
                socket_path.chmod(0o666)
                listener.listen(1)
                listener.settimeout(15)
                read_fd, write_fd = os.pipe()
                pid = os.fork()
                if pid == 0:
                    os.close(read_fd)
                    try:
                        os.setgroups([observed.worklink_gid])
                        os.setresgid(*((observed.mimir_uid,) * 3))
                        os.setresuid(*((observed.mimir_uid,) * 3))
                        result = asyncio.run(controller_run())
                    except BaseException as exc:
                        result = {"error": repr(exc)}
                    os.write(write_fd, json.dumps(result).encode())
                    os._exit(0)
                os.close(write_fd)
                reaped = False
                try:
                    connection, _ = listener.accept()
                    worker_exec.handle_connection(connection)
                    _, status = os.waitpid(pid, 0)
                    reaped = True
                    result = json.loads(os.read(read_fd, 65536))
                    assert os.waitstatus_to_exitcode(status) == 0, result
                    assert "error" not in result, result
                    assert result["exit_code"] == 0, result
                    return result
                finally:
                    os.close(read_fd)
                    if not reaped:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)

        def assert_boundary(result):
            assert canary.read_text() == "original", result
            assert result["stdout"] == f"euid={observed.worklink_uid}\nwrite-denied\n"
            assert result["stderr"] == ""

        if git_intake:
            run()
            assert checkout.stat().st_uid == observed.worklink_uid
            assert checkout.parent.stat().st_uid == observed.mimir_uid
            assert stat.S_IMODE(checkout.parent.stat().st_mode) == 0o2750
            assert (checkout / ".git").stat().st_uid == observed.worklink_uid
            assert (checkout / ".factory-sandboxes/run/run.json").stat().st_uid == observed.worklink_uid
            monkeypatch.setattr(worker_exec, "_normalize_checkout_fd", Mock(side_effect=AssertionError("recovery traversed worker tree as root")))
            payload = (
                "import os; from pathlib import Path\n"
                f"assert os.geteuid() == {observed.worklink_uid}\n"
                "assert Path('.factory-runtime/data/opencode/opencode.db').read_text() == 'retained-session'\n"
                "state = Path('.factory-sandboxes/run/run.json')\n"
                "assert state.read_text() == '{\"status\":\"running\"}'\n"
                "state.write_text('{\"status\":\"resumed\"}')\n"
                "Path('.factory-runtime/data/opencode/auth.json').write_text('retained-auth')\n"
            )
            run()
            assert json.loads((checkout / ".factory-sandboxes/run/run.json").read_text()) == {"status": "resumed"}
            control_launch = True
            payload = (
                "import os; from pathlib import Path\n"
                f"assert os.geteuid() == {observed.worklink_uid}\n"
                "assert Path('.factory-runtime/data/opencode/auth.json').read_text() == 'retained-auth'\n"
            )
            run()
            return
        assert_boundary(run())

        def controller_identity(checkout_fd, home):
            os.setgroups([])
            os.setresgid(*((observed.mimir_uid,) * 3))
            os.setresuid(*((observed.mimir_uid,) * 3))
            os.setsid()
            os.fchdir(checkout_fd)

        monkeypatch.setattr(worker_exec, "_drop_factory", controller_identity)
        vulnerable = run()
        assert vulnerable["stdout"] == f"euid={observed.mimir_uid}\nwrite-allowed\n"
        assert canary.read_text() == "attacked"
        with pytest.raises(AssertionError):
            assert_boundary(vulnerable)
    finally:
        shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(boundary, ignore_errors=True)


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
            stdout_path = boundary / "worker.stdout"
            stderr_path = boundary / "worker.stderr"
            stdout_fd = os.open(stdout_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            stderr_fd = os.open(stderr_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            from mimir.output_capture import OutputSink

            try:
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
                    stdout_sink=OutputSink(stdout_fd, 4096, stdout_path),
                    stderr_sink=OutputSink(stderr_fd, 4096, stderr_path),
                )
                returncode = await process.wait()
                stdout = stdout_path.read_bytes()
                stderr = stderr_path.read_bytes()
            finally:
                os.close(stdout_fd)
                os.close(stderr_fd)
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
