from __future__ import annotations

import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from mimir.worklink import worker_client, worker_exec


@pytest.fixture
def factory_checkout(tmp_path, monkeypatch):
    root = tmp_path / ".worklink"
    outer = root / "repo" / "41-2"
    recovery = outer / ".factory-sandboxes" / "run-1"
    recovery.mkdir(parents=True)
    outer.chmod(0o2770)
    recovery.chmod(0o2775)
    monkeypatch.setattr(worker_exec, "WORKLINK_CHECKOUT_ROOT", root)
    monkeypatch.setattr(worker_exec, "get_identities", lambda: SimpleNamespace(
        mimir_uid=os.getuid(), worklink_uid=os.getuid() + 1, worklink_gid=os.getgid(),
    ))
    return outer, recovery


def _request(path):
    return {
        "version": 1, "op": "launch_factory", "id": str(uuid.uuid4()),
        "executor_identity": worker_exec.EXECUTOR_PROTOCOL_IDENTITY,
        "path": str(path), "issue": 41, "attempt": 2,
        "run_uid": os.getuid() + 1, "run_id": "run-1",
        "argv": ["uv", "run", "factory"], "env": {}, "projections": [],
        "timeout_s": 30, "stdout_limit": 100, "stderr_limit": 100,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [True, False])
async def test_factory_client_exact_request_and_two_fds(tmp_path, monkeypatch, factory):
    identifier = str(uuid.uuid4())
    peer = Mock()
    peer.recv.return_value = json.dumps({"id": identifier, "status": "started", "pid": 123}).encode()
    constructor = (worker_client.WorkerClient.for_factory_checkout if factory
                   else worker_client.WorkerClient.for_path_checkout)
    client = constructor(tmp_path, issue_id=41, attempt=2, run_uid=1002,
                         socket_path=tmp_path / "socket", **({"run_id": "run-1"} if factory else {}))
    monkeypatch.setattr(client, "_connect", lambda: peer)
    process = await client.launch(local_checkout=tmp_path, argv=["uv", "run", "factory"],
                                  env={}, identifier=identifier, timeout_s=30)
    process._socket.close()
    buffers, ancillary = peer.sendmsg.call_args.args
    request = json.loads(buffers[0])
    expected = {
        **_request(tmp_path), "id": identifier, "run_uid": 1002,
        "stdout_limit": 1, "stderr_limit": 1,
    }
    if not factory:
        expected["op"] = "launch_path"
        expected.pop("run_id")
    assert request == expected
    assert ancillary[0][:2] == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
    assert len(ancillary[0][2]) == 2
    assert client.socket_path == tmp_path / "socket"
    assert worker_client.EXECUTOR_PROTOCOL_IDENTITY == worker_exec.EXECUTOR_PROTOCOL_IDENTITY == "worklink-executor-v7-feature-factory"


@pytest.mark.parametrize("recovery", [False, True])
def test_factory_checkout_accepts_exact_fresh_and_recovery(factory_checkout, recovery):
    path = factory_checkout[int(recovery)]
    fd = worker_exec._open_factory_checkout(_request(path))
    try:
        assert os.path.samestat(os.fstat(fd), path.stat())
    finally:
        os.close(fd)


@pytest.mark.parametrize("run_id", ["a", "0", "a.b_c-1"])
def test_factory_checkout_accepts_valid_run_id(factory_checkout, run_id):
    os.close(worker_exec._open_factory_checkout({**_request(factory_checkout[0]), "run_id": run_id}))


@pytest.mark.parametrize("run_id", [None, "", "../run", "Run", True])
def test_factory_client_cannot_downgrade_to_leaf_launch(tmp_path, run_id):
    with pytest.raises(ValueError, match="factory run_id"):
        worker_client.WorkerClient.for_factory_checkout(
            tmp_path, issue_id=41, attempt=2, run_uid=1002, run_id=run_id,
        )


@pytest.mark.parametrize("mode", [0o770, 0o775, 0o2770, 0o2775])
def test_factory_recovery_allows_worker_umask_modes(factory_checkout, mode):
    recovery = factory_checkout[1]
    recovery.chmod(mode)
    os.close(worker_exec._open_factory_checkout(_request(recovery)))


@pytest.mark.parametrize("component", [".factory-sandboxes", "run-1"])
def test_factory_recovery_nofollow_blocks_symlink_swap(factory_checkout, monkeypatch, component):
    outer, recovery = factory_checkout
    real_open = os.open

    def raced_open(path, flags, *args, **kwargs):
        if path == component:
            target = recovery if component == "run-1" else recovery.parent
            moved = target.with_name(target.name + "-moved")
            target.rename(moved)
            target.symlink_to(moved, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(worker_exec.os, "open", raced_open)
    with pytest.raises(OSError):
        worker_exec._open_factory_checkout(_request(recovery))


@pytest.mark.parametrize("component", ["outer", "sandboxes"])
def test_factory_recovery_anchors_each_parent(factory_checkout, monkeypatch, component):
    outer, recovery = factory_checkout
    expected = recovery.stat()
    parent = outer if component == "outer" else recovery.parent
    relative = recovery.relative_to(parent)
    trigger = ".factory-sandboxes" if component == "outer" else "run-1"
    real_open = os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(path).name == trigger:
            swapped = True
            parent.rename(parent.with_name(parent.name + "-original"))
            replacement = parent / relative
            replacement.mkdir(parents=True)
            replacement.chmod(0o2775)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(worker_exec.os, "open", raced_open)
    fd = worker_exec._open_factory_checkout(_request(recovery))
    try:
        assert swapped
        assert os.path.samestat(os.fstat(fd), expected)
    finally:
        os.close(fd)


@pytest.mark.parametrize("field,value", [
    ("run_id", ""), ("run_id", "../run-1"), ("run_id", "Run-1"),
    ("run_id", "-run"), ("run_id", "run-"), ("run_id", None),
    ("run_id", "run\n"), ("issue", 0), ("attempt", -1), ("issue", True),
    ("attempt", "2"), ("run_uid", -1), ("run_uid", True), ("run_uid", "1002"),
])
def test_factory_checkout_rejects_invalid_identity(factory_checkout, field, value):
    with pytest.raises(RuntimeError):
        worker_exec._open_factory_checkout({**_request(factory_checkout[0]), field: value})


def test_factory_checkout_rejects_wrong_worker_uid(factory_checkout, monkeypatch):
    outer_open = Mock(wraps=worker_exec._open_path_checkout)
    monkeypatch.setattr(worker_exec, "_open_path_checkout", outer_open)
    with pytest.raises(RuntimeError, match="worker uid"):
        worker_exec._open_factory_checkout({**_request(factory_checkout[0]), "run_uid": os.getuid()})
    outer_open.assert_not_called()


@pytest.mark.parametrize("suffix", ["", "repo", "repo/42-2", "repo/41-3",
    "bad repo/41-2",
    "repo/41-2/child", "repo/41-2/.factory-sandboxes/other",
    "repo/41-2/wrong/run-1", "repo/41-2/.factory-sandboxes/run-1/child"])
def test_factory_checkout_rejects_path_boundaries(factory_checkout, suffix, monkeypatch):
    outer, _ = factory_checkout
    path = outer.parent.parent / suffix
    path.mkdir(parents=True, exist_ok=True)
    outer_open = Mock(wraps=worker_exec._open_path_checkout)
    monkeypatch.setattr(worker_exec, "_open_path_checkout", outer_open)
    with pytest.raises(RuntimeError, match="shape"):
        worker_exec._open_factory_checkout(_request(path))
    outer_open.assert_not_called()


@pytest.mark.parametrize("path", ["relative", "\x00", None])
def test_factory_checkout_rejects_invalid_path(factory_checkout, path):
    with pytest.raises(RuntimeError, match="path is invalid"):
        worker_exec._open_factory_checkout({**_request(factory_checkout[0]), "path": path})


def test_factory_checkout_rejects_outside_root(factory_checkout, tmp_path):
    outside = tmp_path / "outside" / "repo" / "41-2"
    outside.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="outside"):
        worker_exec._open_factory_checkout(_request(outside))


@pytest.mark.parametrize("recovery", [False, True])
def test_factory_checkout_rejects_alias_to_valid_checkout(factory_checkout, tmp_path, recovery):
    alias = tmp_path / "alias"
    alias.symlink_to(factory_checkout[int(recovery)], target_is_directory=True)
    with pytest.raises(RuntimeError, match="shape"):
        worker_exec._open_factory_checkout(_request(alias))


def test_factory_checkout_rejects_bool_uid_even_when_equal(factory_checkout, monkeypatch):
    monkeypatch.setattr(worker_exec, "get_identities", lambda: SimpleNamespace(
        mimir_uid=os.getuid(), worklink_uid=1, worklink_gid=os.getgid(),
    ))
    with pytest.raises(RuntimeError, match="run_uid identity"):
        worker_exec._open_factory_checkout({**_request(factory_checkout[0]), "run_uid": True})


def test_factory_launch_uses_factory_validation(factory_checkout, monkeypatch):
    monkeypatch.setattr(worker_exec, "_open_path_checkout", Mock(
        side_effect=AssertionError("invalid factory run_id reached leaf opener"),
    ))
    request = {**_request(factory_checkout[0]), "run_id": "../invalid"}
    with pytest.raises(RuntimeError, match="factory run_id"):
        worker_exec._handle_launch(Mock(), request, [-1, -1])


@pytest.mark.parametrize("component", ["outer", "sandboxes", "recovery"])
def test_factory_checkout_rejects_symlinks(factory_checkout, component):
    outer, recovery = factory_checkout
    path = {"outer": outer, "sandboxes": recovery.parent, "recovery": recovery}[component]
    target = path.with_name(path.name + "-real")
    path.rename(target)
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="shape"):
        worker_exec._open_factory_checkout(_request(recovery))


@pytest.mark.parametrize("recovery", [False, True])
def test_factory_checkout_retains_strict_outer_mode(factory_checkout, recovery):
    factory_checkout[0].chmod(0o2775)
    with pytest.raises(RuntimeError, match="ownership or mode"):
        worker_exec._open_factory_checkout(_request(factory_checkout[int(recovery)]))


@pytest.mark.parametrize("mode", [0o2777, 0o2750, 0o2760, 0o2730])
def test_factory_recovery_rejects_unsafe_mode(factory_checkout, mode):
    recovery = factory_checkout[1]
    recovery.chmod(mode)
    with pytest.raises(RuntimeError, match="ownership or mode"):
        worker_exec._open_factory_checkout(_request(recovery))


@pytest.mark.parametrize("owner", ["worker", "controller", "foreign", "wrong-group", "not-dir"])
def test_factory_recovery_owner_group_and_type(factory_checkout, monkeypatch, owner):
    recovery = factory_checkout[1]
    observed = recovery.stat()
    monkeypatch.setattr(worker_exec.os, "fstat", lambda fd: SimpleNamespace(
        st_uid=os.getuid() + {"worker": 1, "foreign": 2}.get(owner, 0),
        st_gid=os.getgid() + (owner == "wrong-group"),
        st_mode=0o100770 if owner == "not-dir" else observed.st_mode,
    ))
    if owner in {"worker", "controller"}:
        os.close(worker_exec._open_factory_checkout(_request(recovery)))
    else:
        with pytest.raises(RuntimeError, match="ownership or mode"):
            worker_exec._open_factory_checkout(_request(recovery))


@pytest.mark.parametrize("change", ["missing", "extra", "one-fd", "three-fds", "leaf-op", "stale"])
def test_factory_launch_exact_contract(factory_checkout, change, monkeypatch):
    for opener in ("_open_factory_checkout", "_open_path_checkout"):
        monkeypatch.setattr(worker_exec, opener, Mock(
            side_effect=AssertionError("malformed launch reached checkout opener"),
        ))
    request = _request(factory_checkout[0])
    fds = [-1, -1]
    if change == "missing":
        request.pop("run_id")
    elif change == "extra":
        request["uid"] = 0
    elif change == "leaf-op":
        request["op"] = "launch_path"
    elif change == "stale":
        request["executor_identity"] = "old"
    else:
        fds = [-1] * (1 if change == "one-fd" else 3)
    with pytest.raises(RuntimeError, match="stale root executor" if change == "stale" else "exact contract and two FDs"):
        worker_exec._handle_launch(Mock(), request, fds)


@pytest.mark.parametrize("recovery", [False, True])
def test_factory_dispatch_preserves_uv_checkout_and_drops_worker(factory_checkout, tmp_path, monkeypatch, recovery):
    path = factory_checkout[int(recovery)]
    request = _request(path)
    homes = tmp_path / "homes"
    homes.mkdir()
    monkeypatch.setattr(worker_exec, "HOME_ROOT", homes)
    monkeypatch.setattr(worker_exec.os, "chown", lambda *args: None)
    copy = Mock(side_effect=AssertionError("factory must not copy checkout"))
    monkeypatch.setattr(worker_exec, "_execution_checkout_fd", copy)
    drop = Mock()
    monkeypatch.setattr(worker_exec, "_drop_worker", drop)

    def popen(command, **kwargs):
        assert command == request["argv"]
        fd, = kwargs["pass_fds"]
        assert os.path.samestat(os.fstat(fd), path.stat())
        kwargs["preexec_fn"]()
        drop.assert_called_once_with(fd)
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(worker_exec.subprocess, "Popen", popen)
    monkeypatch.setattr(worker_exec, "_wait_with_output_limits", lambda *args: (0, False, False))
    fds = [os.open(tmp_path / name, os.O_RDWR | os.O_CREAT, 0o600) for name in ("stdout", "stderr")]
    connection = Mock()
    connection.recvmsg.return_value = (json.dumps(request).encode(), [], 0, None)
    monkeypatch.setattr(worker_exec, "_received_fds", lambda ancillary: fds)
    worker_exec.handle_connection(connection)
    responses = [json.loads(call.args[0]) for call in connection.send.call_args_list]
    assert [response.get("status") for response in responses] == ["started", "terminal"]
    copy.assert_not_called()
    assert path.is_dir()
    assert not (homes / request["id"]).exists()
