"""Provider executor boundary and worker wire-format compatibility."""

from __future__ import annotations

import ast
import array
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mimir_cultivate.worklink import (
    claims, compute, control, detached_dispatch, dispatch_failures, tool_pins,
    worker_client, worker_exec,
)
from mimir_cultivate.worklink.backends import opencode
from mimir_cultivate.worklink.backends.base import WorkOrder


def test_worker_protocol_matches_controller_and_legacy_image() -> None:
    from mimir.worklink import worker_client as legacy_client

    assert worker_client.EXECUTOR_PROTOCOL_IDENTITY == worker_exec.EXECUTOR_PROTOCOL_IDENTITY
    assert worker_client.EXECUTOR_PROTOCOL_IDENTITY == legacy_client.EXECUTOR_PROTOCOL_IDENTITY
    assert worker_client.MAX_REQUEST_BYTES == legacy_client.MAX_REQUEST_BYTES
    assert worker_client.MAX_PROJECTION_BYTES == legacy_client.MAX_PROJECTION_BYTES
    assert worker_client.DEFAULT_EXECUTOR_SOCKET == legacy_client.DEFAULT_EXECUTOR_SOCKET


def test_factory_checkout_binding_rejects_ambiguous_paths() -> None:
    root = worker_client.WORKLINK_CHECKOUT_ROOT
    checkout = root / "repo" / "1949-2" / "checkout"
    assert worker_client.factory_checkout_for_path(checkout / ".factory" / "run") == (
        checkout, 1949, 2,
    )
    for path in (
        root / "repo" / "01949-2" / "checkout",
        root / "repo" / "1949-0" / "checkout",
        checkout / ".." / "outside",
    ):
        with pytest.raises(ValueError, match="invalid factory checkout path"):
            worker_client.factory_checkout_for_path(path)
    assert worker_client.factory_checkout_for_path(root / "repo" / "1949-2" / "other") is None


def test_provider_execution_modules_do_not_import_legacy_worklink() -> None:
    modules = (compute, control, dispatch_failures, worker_client, worker_exec, opencode)
    for module in modules:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imports = (
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0
        )
        assert not any(name and name.startswith("mimir.worklink") for name in imports)


def test_opencode_work_spec_scrubs_model_selection_with_empty_remove_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(opencode, "_coding_enabled", lambda: False)
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:ambient")
    environment = {
        "HOME": str(tmp_path), "MIMIR_HOME": str(home),
        "MIMIR_MODEL_SPEC": "codex-plus:gpt-agent", "OPENCODE_CONFIG": "",
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "XDG_DATA_HOME": str(tmp_path / ".local/share"),
    }
    auth = tmp_path / ".local/share/opencode/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"openai": {"type": "oauth", "refresh": "subscription"}}))
    spec = opencode.OpenCodeBackend().work_spec(
        WorkOrder(1949, tmp_path, "prompt", None, 30, environment),
        attempt=1, repo_url="repo", base_ref="main", branch="issue/1949",
        test_command="true",
    )
    assert spec.backend_config["args"] == ["-m", "openai/gpt-agent"]
    assert "MIMIR_MODEL_SPEC" not in spec.env
    assert spec.local_argv[:4] == ("opencode", "run", "--dir", str(tmp_path))


def test_provider_worker_wire_and_executor_contract(tmp_path: Path) -> None:
    request = {
        "version": 1, "op": "launch", "executor_identity": worker_client.EXECUTOR_PROTOCOL_IDENTITY,
        "id": "00000000-0000-4000-8000-000000000001", "issue": 1949, "attempt": 1,
        "device": 1, "inode": 2, "argv": ["true"], "env": {}, "projections": [],
        "timeout_s": 30, "stdout_limit": 1, "stderr_limit": 1,
    }
    worker_exec._validate_executor_identity(request)
    assert worker_exec._received_fds([
        (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [3, 4]).tobytes())
    ]) == [3, 4]
    with pytest.raises(RuntimeError, match="stale root executor image"):
        worker_exec._validate_executor_identity({**request, "executor_identity": "old"})
    with pytest.raises(ValueError, match="destination"):
        worker_client.WorkerProjection("../auth.json", b"{}")
    assert "mimir_cultivate.worklink.worker_exec" in worker_client.STALE_EXECUTOR_DIAGNOSTIC


@pytest.mark.asyncio
async def test_provider_worker_client_refuses_non_root_peer_before_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    path = tmp_path / "executor.sock"
    server.bind(str(path))
    server.listen(1)
    client = worker_client.WorkerClient.for_factory_checkout(
        tmp_path / "checkout", issue_id=1949, attempt=1, socket_path=path,
    )
    monkeypatch.setattr(worker_client.struct, "unpack", lambda *_: (1, 1002, 1002))
    try:
        with pytest.raises(RuntimeError, match="peer is not root"):
            client._connect()
        connection, _ = server.accept()
        try:
            assert connection.recv(1) == b""
        finally:
            connection.close()
    finally:
        server.close()


def test_provider_executor_refuses_fd_identity_and_symlink_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="identity does not match"):
            worker_exec._validate_checkout(fd, {
                "issue": 1949, "attempt": 1, "device": os.fstat(fd).st_dev,
                "inode": os.fstat(fd).st_ino + 1,
            })
    finally:
        os.close(fd)
    root = tmp_path / "root"
    root.mkdir()
    (root / "repo").mkdir()
    (root / "repo" / "1949-1").symlink_to(checkout, target_is_directory=True)
    monkeypatch.setattr(worker_exec, "WORKLINK_CHECKOUT_ROOT", root)
    with pytest.raises((OSError, RuntimeError)):
        worker_exec._open_factory_checkout({
            "issue": 1949, "attempt": 1, "path": str(root / "repo/1949-1/checkout"),
            "run_uid": worker_exec.get_identities().worklink_uid,
        })
    target = tmp_path / "target"
    target.mkdir()
    (target / "escape").symlink_to(checkout, target_is_directory=True)
    old_cwd = os.open(".", os.O_RDONLY)
    try:
        os.chdir(target)
        with pytest.raises(OSError):
            worker_exec._factory_file_parent(
                worker_exec.PurePosixPath("escape/file.txt"), create=False,
            )
    finally:
        os.fchdir(old_cwd)
        os.close(old_cwd)


def test_provider_factory_file_child_in_isolated_interpreter(tmp_path: Path) -> None:
    run_id = "chainlink-1949"
    sandbox = tmp_path / ".factory-sandboxes" / run_id
    sandbox.mkdir(parents=True)
    request = {
        "op": "write_file", "issue": 1949, "run_id": run_id,
        "relative_path": f".factory-sandboxes/{run_id}/hello.txt", "content": "hello",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-m", "mimir_cultivate.worklink.worker_exec", "--factory-file-child"],
        input=json.dumps(request), text=True, capture_output=True, cwd=tmp_path, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ok"
    assert (sandbox / "hello.txt").read_text() == "hello"


def test_provider_dispatch_pins_and_claim_finder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def popen(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return SimpleNamespace(pid=42)

    result = detached_dispatch.launch_detached_worklink(
        command="run-epic", issue_id=1949, home=tmp_path, repo=tmp_path,
        state_dir=tmp_path, run_bin=("mimir",), popen=popen,
        recovery=detached_dispatch.FactoryRecoveryIdentity("sig", "occ", "run", 2, "session"),
    )
    assert result.pid == 42
    assert captured["argv"][-10:] == [
        "--expected-signature", "sig", "--expected-occurrence", "occ",
        "--expected-run-id", "run", "--expected-attempt", "2",
        "--expected-session", "session",
    ]
    assert captured["start_new_session"] is True
    assert tool_pins.default_tool_pins() is tool_pins.DEFAULT_TOOL_PINS
    assert next(pin.pin for pin in tool_pins.default_tool_pins() if pin.name == "opencode") == tool_pins.OPENCODE_VERSION
    evidence = tmp_path / "evidence.json"
    finder = lambda *_: (evidence, {
        "status": "completed", "pr_url": "https://example.test/pr/1",
    })
    monkeypatch.setattr(claims, "_find_latest_evidence_file_for_issue", finder)
    receipt = claims.ChainlinkClaims(agent_id="agent", home_path=tmp_path).review_ready_evidence(1949)
    assert receipt is not None and receipt.path == evidence


@pytest.mark.asyncio
async def test_provider_compute_worker_environment_and_cancel() -> None:
    spec = compute.WorkSpec(
        1949, 1, "repo", "main", "issue/1949", "prompt", None, "true", "opencode", 30,
        backend_config={"pass_env": ("EXISTING",)},
    )
    injected = compute.with_worker_environment(spec, {"INJECTED": "safe"})
    assert injected.env == {"INJECTED": "safe"}
    assert injected.backend_config["pass_env"] == ("EXISTING", "INJECTED")
    called = []

    class Client:
        async def cancel(self, identifier):
            called.append(identifier)

    await compute._cancel_worker(Client(), "worker-1949")
    assert called == ["worker-1949"]
    assert spec.env == {}


def test_provider_stop_marker_and_failure_ledger_block_dispatch(tmp_path: Path) -> None:
    assert not control.operator_stop_requested(tmp_path, 1949, 1)
    control._mark_operator_stop(tmp_path, 1949, 1)
    assert control.operator_stop_requested(tmp_path, 1949, 1)
    assert not control.operator_stop_requested(tmp_path, 1949, 2)
    state_dir = dispatch_failures.dispatch_failure_state_dir(tmp_path)
    assert dispatch_failures.autonomous_dispatch_block_reason(state_dir, 1949) is None
    state_dir.mkdir(parents=True)
    (state_dir / dispatch_failures.STATE_FILE).write_text(json.dumps({
        "version": 1, "issues": {"1949": {"active": True, "failure_kind": "spec_invalid"}},
    }))
    assert "unresolved Worklink incident" in dispatch_failures.autonomous_dispatch_block_reason(
        state_dir, 1949,
    )
