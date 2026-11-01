"""Provider executor boundary and worker wire-format compatibility."""

from __future__ import annotations

import ast
import array
import asyncio
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from mimir_cultivate.worklink import (
    checkout, claims, compute, continuation, control, detached_dispatch, dispatch_failures,
    merge_closure, poller_hooks, retained_scope, tool_pins, worker_client, worker_exec,
    orchestrator,
)
from mimir_cultivate.worklink.backends import opencode
from mimir_cultivate.worklink.backends.base import WorkOrder
from mimir.output_capture import open_output_pair
from tests.opencode_fixtures import seed_codex_subscription


_OTLP_CARRIERS = {
    "OTEL_EXPORTER_OTLP_HEADERS": "plain-canary",
    "otel_exporter_otlp_traces_client_key": "/tmp/plain-canary",
    "OTEL_EXPORTER_OTLP_METRICS_CERTIFICATE": "/tmp/plain-canary",
    "OTEL_EXPORTER_OTLP_LOGS_CLIENT_CERTIFICATE": "/tmp/plain-canary",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://user:plain-canary@collector.test/base",
    "MIMIR_OTLP_ENDPOINT": "https://collector.test/base?token=plain-canary",
}
_OTLP_SAFE = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.test/base",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OPENAI_API_KEY": "provider-canary",
}
_ENV_PROBE = (
    sys.executable, "-c",
    "import json, os; print(json.dumps({k: v for k, v in os.environ.items() "
    "if k in ('OPENAI_API_KEY', 'OTEL_EXPORTER_OTLP_ENDPOINT', "
    "'OTEL_EXPORTER_OTLP_PROTOCOL') or 'OTLP' in k.upper()}))",
)


def _assert_safe_env(output: str) -> None:
    observed = json.loads(output)
    assert not set(observed).intersection(set(_OTLP_CARRIERS) - set(_OTLP_SAFE))
    assert "plain-canary" not in output
    assert observed["OTEL_EXPORTER_OTLP_ENDPOINT"] == _OTLP_SAFE["OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert observed["OPENAI_API_KEY"] == "provider-canary"


def test_detached_launch_filters_ambient_and_overrides_in_child_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _OTLP_CARRIERS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _OTLP_CARRIERS["OTEL_EXPORTER_OTLP_ENDPOINT"])
    launched = detached_dispatch.launch_detached_worklink(
        command="run", issue_id=1949, home=tmp_path, repo=tmp_path,
        state_dir=tmp_path, run_bin=(sys.executable,), argv=_ENV_PROBE,
        env_overrides={**_OTLP_CARRIERS, **_OTLP_SAFE},
    )
    os.waitpid(launched.pid, 0)
    output = launched.log_path.read_text()
    _assert_safe_env(output)
    assert "plain-canary" not in output


@pytest.mark.asyncio
async def test_local_compute_filters_spec_env_in_child_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "plain-canary")
    backend = compute.LocalSubprocessComputeBackend()
    spec = compute.WorkSpec(
        1949, 1, "repo", "main", "issue/1949", "prompt", None, "true", "local", 30,
        env={**_OTLP_CARRIERS, **_OTLP_SAFE}, local_checkout=tmp_path,
        local_argv=_ENV_PROBE, output_root=tmp_path,
    )
    handle = await backend.launch(spec)
    try:
        result = await backend.wait(handle, timeout_s=10)
        assert result.exit_code == 0, result.stderr
        _assert_safe_env(result.stdout)
        assert "plain-canary" not in result.stderr
        assert "plain-canary" not in result.stdout_path.read_text()
        assert "plain-canary" not in result.stderr_path.read_text()
    finally:
        await backend.cleanup(handle)


def test_contained_environment_and_worker_request_filter_carriers() -> None:
    spec = compute.WorkSpec(
        1949, 1, "repo", "main", "issue/1949", "prompt", None, "true", "opencode", 30,
        env={**_OTLP_CARRIERS, **_OTLP_SAFE},
        backend_config={"pass_env": tuple({**_OTLP_CARRIERS, **_OTLP_SAFE})},
    )
    env = compute._enabled_child_env(spec, "00000000-0000-4000-8000-000000000002")
    assert not set(env).intersection(set(_OTLP_CARRIERS) - set(_OTLP_SAFE))
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == _OTLP_SAFE["OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert env["OPENAI_API_KEY"] == "provider-canary"
    injected = compute.with_worker_environment(spec, _OTLP_CARRIERS)
    assert not set(injected.env).intersection(set(_OTLP_CARRIERS) - set(_OTLP_SAFE))
    assert not set(injected.backend_config["pass_env"]).intersection(set(_OTLP_CARRIERS) - set(_OTLP_SAFE))
    assert not set(worker_exec._validate_environment({**_OTLP_CARRIERS, **_OTLP_SAFE})).intersection(
        set(_OTLP_CARRIERS) - set(_OTLP_SAFE)
    )


def test_worker_protocol_matches_executor_image_contract() -> None:
    expected_protocol = "worklink-executor-v12-factory-cancel"
    assert worker_client.EXECUTOR_PROTOCOL_IDENTITY == expected_protocol
    assert worker_exec.EXECUTOR_PROTOCOL_IDENTITY == expected_protocol
    assert worker_client.MAX_REQUEST_BYTES == worker_exec.MAX_REQUEST_BYTES == 256 * 1024
    assert worker_client.MAX_PROJECTION_BYTES == worker_exec.MAX_PROJECTION_BYTES == 1024 * 1024
    assert worker_client.DEFAULT_EXECUTOR_SOCKET == worker_exec.DEFAULT_EXECUTOR_SOCKET == Path(
        "/run/mimir-worklink/socket/worklink-execd.sock"
    )


def test_checkout_root_env_and_default_are_shared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "worklink" / "base" / "trellis"
    monkeypatch.delenv("WORKLINK_CHECKOUT_ROOT", raising=False)
    assert worker_client.worklink_checkout_root() == Path("/workspace/.worklink")
    assert checkout._isolated_checkout_path(repo, ".worklink", 1, 1, worker_authorized=False) == (
        Path("/workspace/.worklink/trellis/1-1")
    )
    assert checkout._isolated_checkout_path(Path("/workspace/mimir"), ".worklink", 12, 3,
                                           worker_authorized=False) == Path("/workspace/.worklink/mimir/12-3")
    root = tmp_path / "checkouts"
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(root))
    assert checkout._isolated_checkout_path(repo, ".worklink", 1, 1, worker_authorized=False) == (
        worker_client.worklink_checkout_root() / "trellis" / "1-1"
    )
    assert worker_exec.worklink_checkout_root is worker_client.worklink_checkout_root


def test_retained_checkout_policy_follows_shared_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.access_control import worklink_retained_checkout_root
    from mimir.plugins.core import policy_scope
    from mimir.plugins.manager import load_plugins

    selected = load_plugins(enable="cultivate").policy
    with policy_scope(selected):
        monkeypatch.delenv("WORKLINK_CHECKOUT_ROOT", raising=False)
        assert worklink_retained_checkout_root() == Path("/workspace/.worklink")
        root = tmp_path / "custom-checkouts"
        monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(root))
        assert worklink_retained_checkout_root() == root.resolve()
        assert worklink_retained_checkout_root() == worker_client.worklink_checkout_root()
        # The already-registered hook must read the setting on every call.
        other_root = tmp_path / "other-checkouts"
        monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(other_root))
        assert worklink_retained_checkout_root() == other_root.resolve()


def test_reattach_observation_uses_checkout_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "checkouts"
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(root))
    repo = tmp_path / "worklink" / "base" / "trellis"
    calls = []

    def runner(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    lease = orchestrator._create_observation_worktree(
        repo, issue_id=1, attempt=2, base="main", local_base="main",
        branch="issue/1-a2", runner=runner,
    )
    assert lease.path == root / "trellis" / "reattach-1-2"
    assert calls[-1][-1] == str(lease.path)


def test_foreign_base_checkout_is_accepted_without_request_supplied_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "checkouts"
    repo = tmp_path / "worklink" / "base" / "trellis"
    root.mkdir()
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(root))
    path = checkout._isolated_checkout_path(repo, ".worklink", 1, 1, worker_authorized=False)
    path.mkdir(parents=True)
    path.chmod(0o2770)
    monkeypatch.setattr(worker_exec, "get_identities", lambda: SimpleNamespace(
        mimir_uid=os.getuid(), worklink_uid=os.getuid(), worklink_gid=os.getgid(),
    ))
    request = {"issue": 1, "attempt": 1, "path": str(path), "run_uid": os.getuid()}
    fd = worker_exec._open_path_checkout(request)
    try:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
    # This untrusted field cannot move the executor's boundary to the base clone.
    (repo / "1-1").mkdir(parents=True)
    (repo / "1-1").chmod(0o2770)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere").chmod(0o2770)
    with pytest.raises(RuntimeError, match="outside the Worklink root"):
        worker_exec._open_path_checkout({**request, "path": str(repo / "1-1"),
                                         "checkout_root": str(repo.parent)})
    with pytest.raises(RuntimeError, match="outside the Worklink root"):
        worker_exec._open_path_checkout({**request, "path": str(tmp_path / "elsewhere"),
                                         "checkout_root": str(tmp_path)})
    (path / "nested").mkdir()
    with pytest.raises(RuntimeError, match="shape is invalid"):
        worker_exec._open_path_checkout({**request, "path": str(path / "nested")})


@pytest.mark.asyncio
async def test_executor_identity_requires_matching_independent_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(tmp_path / "checkouts"))
    response = {"status": "identity", "executor_identity": worker_exec.EXECUTOR_PROTOCOL_IDENTITY,
                "source_commit": "a" * 40, "checkout_root": str(worker_exec.worklink_checkout_root())}

    class Socket:
        def send(self, payload):
            assert "checkout_root" not in json.loads(payload)

        def recv(self, size):
            return json.dumps(response).encode()

        def close(self):
            pass

    monkeypatch.setattr(worker_client.WorkerClient, "_connect", lambda self: Socket())
    assert await worker_client.verify_executor_identity() == "a" * 40
    response["checkout_root"] = str(tmp_path / "wrong-root")
    with pytest.raises(worker_client.CheckoutRootMismatchError) as mismatch:
        await worker_client.verify_executor_identity()
    assert str(tmp_path / "checkouts") in str(mismatch.value)
    assert str(tmp_path / "wrong-root") in str(mismatch.value)
    assert "align WORKLINK_CHECKOUT_ROOT" in str(mismatch.value)
    assert "rebuild" not in str(mismatch.value)
    # A stale protocol still gets the image-rebuild remedy, even if roots differ.
    response["executor_identity"] = "old-protocol"
    with pytest.raises(worker_client.StaleWorkerExecutorError, match="rebuild the image"):
        await worker_client.verify_executor_identity()
    response["executor_identity"] = worker_exec.EXECUTOR_PROTOCOL_IDENTITY
    del response["checkout_root"]
    with pytest.raises(worker_client.StaleWorkerExecutorError, match="rebuild the image"):
        await worker_client.verify_executor_identity()


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


@pytest.mark.parametrize("image_install", [True, False])
def test_opencode_work_spec_scrubs_model_selection_with_empty_remove_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image_install: bool,
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
    seed_codex_subscription(tmp_path)
    image_bin = tmp_path / "opt/mimir-opencode/bin/opencode"
    fallback = tmp_path / "usr/bin/opencode"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("#!/bin/sh\nexit 0\n")
    fallback.chmod(0o755)
    if image_install:
        image_bin.parent.mkdir(parents=True)
        image_bin.write_text("#!/bin/sh\nexit 0\n")
        image_bin.chmod(0o755)
    monkeypatch.setattr(opencode, "IMAGE_OPENCODE_BIN", image_bin)
    monkeypatch.setattr(opencode.shutil, "which", lambda name: str(fallback) if name == "opencode" else None)
    # A checkout or worker PATH with its own opencode must never choose argv[0].
    hostile = tmp_path / "checkout/bin"
    hostile.mkdir(parents=True)
    (hostile / "opencode").write_text("untrusted")
    environment["PATH"] = str(hostile)
    spec = opencode.OpenCodeBackend().work_spec(
        WorkOrder(1949, tmp_path, "prompt", None, 30, environment),
        attempt=1, repo_url="repo", base_ref="main", branch="issue/1949",
        test_command="true",
    )
    assert spec.backend_config["args"] == ["-m", "openai/gpt-agent"]
    assert "MIMIR_MODEL_SPEC" not in spec.env
    expected = image_bin if image_install else fallback
    assert spec.local_argv[:4] == (str(expected), "run", "--dir", str(tmp_path))
    assert spec.local_argv[0] == spec.backend_config["bin"]
    assert expected.is_absolute() and expected.is_file()
    assert spec.local_argv[0] != "opencode"
    assert spec.local_argv[0] != str(hostile / "opencode")


def test_opencode_image_symlink_is_preserved_and_missing_binary_fails_closed(tmp_path, monkeypatch):
    image_bin = tmp_path / "opt/mimir-opencode/bin/opencode"
    target = tmp_path / "opt/mimir-opencode/lib/node_modules/opencode-ai/bin/opencode.exe"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    image_bin.parent.mkdir(parents=True)
    image_bin.symlink_to(target)
    monkeypatch.setattr(opencode, "IMAGE_OPENCODE_BIN", image_bin)
    monkeypatch.setattr(opencode.shutil, "which", lambda name: None)
    assert opencode.resolve_opencode_binary() == str(image_bin)
    image_bin.unlink()
    with pytest.raises(FileNotFoundError, match="opencode_missing"):
        opencode.resolve_opencode_binary()
    with pytest.raises(ValueError, match="absolute path or a bare name"):
        opencode.resolve_opencode_binary("./opencode")
    target.chmod(0o644)
    with pytest.raises(FileNotFoundError, match="opencode_missing"):
        opencode.resolve_opencode_binary(str(target))


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


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux only: executor RPC requires AF_UNIX SOCK_SEQPACKET and SO_PEERCRED",
)
@pytest.mark.asyncio
async def test_provider_worker_client_refuses_non_root_peer_before_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, short_socket_dir: Path,
) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    path = short_socket_dir / "executor.sock"
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
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(root))
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


def test_provider_executor_refuses_mismatched_factory_run_uid_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "worklink"
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(root))
    monkeypatch.setattr(
        worker_exec, "get_identities",
        lambda: SimpleNamespace(worklink_uid=2001),
    )

    def unexpected_open(*args, **kwargs):
        pytest.fail("executor opened a checkout for a mismatched worker uid")

    monkeypatch.setattr(worker_exec.os, "open", unexpected_open)
    with pytest.raises(RuntimeError, match="invalid worker uid"):
        worker_exec._open_factory_checkout({
            "issue": 1949, "attempt": 1,
            "path": str(root / "repo" / "1949-1" / "checkout"),
            "run_uid": 2002,
        })


def test_provider_worker_identity_guard_rejects_wrong_uid_without_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_exec, "get_identities",
        lambda: SimpleNamespace(worklink_uid=2001, worklink_gid=2002),
    )
    # getresuid/getresgid are Linux-only; the guard's decision is portable.
    monkeypatch.setattr(worker_exec.os, "getresuid", lambda: (2001, 2001, 0), raising=False)
    monkeypatch.setattr(
        worker_exec.os, "getresgid", lambda: (2002, 2002, 2002), raising=False,
    )
    with pytest.raises(RuntimeError, match="worker identity drop failed"):
        worker_exec._verify_worker_identity()


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
    if sys.platform == "linux":
        assert json.loads(result.stdout)["status"] == "ok"
        assert (sandbox / "hello.txt").read_text() == "hello"
    else:
        # Create-only publication needs renameat2(RENAME_NOREPLACE); elsewhere
        # the isolated child still runs and must refuse rather than overwrite.
        assert json.loads(result.stdout) == {
            "status": "error", "error": "atomic create-only publication is unavailable",
        }
        assert not (sandbox / "hello.txt").exists()


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


def test_provider_tool_pin_is_independent_of_core_backend() -> None:
    pin = tool_pins.default_tool_pins()[0]
    assert type(pin) is tool_pins.ToolPin
    assert type(pin).__module__ == "mimir_cultivate.worklink.tool_pins"
    assert tuple(pin.__dataclass_fields__) == (
        "name", "category", "pin", "smoke", "source", "package", "repo", "install", "risk",
    )
    assert pin.name == "chainlink"
    with pytest.raises(AttributeError):
        pin.pin = "untrusted"


@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux only: executor RPC requires AF_UNIX SOCK_SEQPACKET",
)
@pytest.mark.asyncio
async def test_provider_worker_wire_fd_identity_launch_wait_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    descriptor = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)
    metadata = os.fstat(descriptor)

    class Capability:
        issue_id = 1949
        attempt = 1
        device = metadata.st_dev
        inode = metadata.st_ino

        def verify(self, path):
            assert path == checkout

        def duplicate_fd(self):
            return os.dup(descriptor)

    # Socket pairs still carry real SCM_RIGHTS; only peer authentication is
    # substituted because the test executor is not installed as root.
    servers = []
    threads = []

    def connect(self, timeout_s=None):
        client_sock, server_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        servers.append(server_sock)
        thread = threading.Thread(target=worker_exec.handle_connection, args=(server_sock,))
        thread.start()
        threads.append(thread)
        return client_sock

    monkeypatch.setattr(worker_client.WorkerClient, "_connect", connect)
    monkeypatch.setattr(worker_exec, "_executor_source_commit", lambda: "a" * 40)
    client = worker_client.WorkerClient(Capability())
    identifier = "00000000-0000-4000-8000-000000000002"
    try:
        assert await worker_client.verify_executor_identity() == "a" * 40
        with pytest.raises(RuntimeError, match="identity does not match"):
            wrong = worker_client.WorkerClient(Capability())
            wrong.checkout = SimpleNamespace(
                issue_id=1949, attempt=1, device=metadata.st_dev,
                inode=metadata.st_ino + 1, verify=lambda path: None,
                duplicate_fd=lambda: os.dup(descriptor),
            )
            await wrong.launch(
                local_checkout=checkout, argv=("true",), env={}, identifier=identifier,
                timeout_s=5,
            )
        monkeypatch.setattr(worker_exec, "_validate_checkout", lambda fd, request: checkout)
        monkeypatch.setattr(worker_exec, "HOME_ROOT", tmp_path)
        monkeypatch.setattr(worker_exec, "_drop_worker", lambda fd: os.fchdir(fd))
        monkeypatch.setattr(worker_exec.os, "chown", lambda *args: None)
        process = await client.launch(
            local_checkout=checkout, argv=(sys.executable, "-c", "print('worker-ok')"),
            env={}, identifier=identifier, timeout_s=10,
        )
        assert await process.wait() == 0
        # Simulate a crafted client request that bypasses controller filtering.
        monkeypatch.setattr(worker_client, "filter_otlp_env", dict)
        stdout, stderr = open_output_pair(None, 4096, None, 4096)
        try:
            process = await client.launch(
                local_checkout=checkout, argv=_ENV_PROBE,
                env={**_OTLP_CARRIERS, **_OTLP_SAFE},
                identifier="00000000-0000-4000-8000-000000000004", timeout_s=10,
                stdout_sink=stdout, stderr_sink=stderr,
            )
            assert await process.wait() == 0
            _assert_safe_env(stdout.read_bounded()[0].decode())
            assert "plain-canary" not in stderr.read_bounded()[0].decode()
        finally:
            stdout.close()
            stderr.close()
        cancel_id = "00000000-0000-4000-8000-000000000003"
        cancelled = []
        monkeypatch.setattr(worker_exec, "_terminate_process_group", lambda proc: cancelled.append(proc))
        with worker_exec._jobs_lock:
            worker_exec._jobs[cancel_id] = SimpleNamespace(pid=42)
        try:
            await client.cancel(cancel_id)
            assert len(cancelled) == 1 and cancelled[0].pid == 42
        finally:
            with worker_exec._jobs_lock:
                worker_exec._jobs.pop(cancel_id, None)
    finally:
        os.close(descriptor)
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()
        for server in servers:
            assert server.fileno() == -1


@pytest.mark.asyncio
async def test_provider_compute_launch_wait_and_cancel(tmp_path: Path) -> None:
    backend = compute.LocalSubprocessComputeBackend()
    def spec(command):
        return compute.WorkSpec(
            1949, 1, "repo", "main", "issue/1949", "prompt", None,
            "true", "local", 30, local_checkout=tmp_path, local_argv=command,
        )

    handle = await backend.launch(spec((sys.executable, "-c", "print('computed')")))
    result = await backend.wait(handle, timeout_s=10)
    assert result.exit_code == 0 and result.stdout.strip() == "computed"
    await backend.cleanup(handle)

    handle = await backend.launch(spec((sys.executable, "-c", "import time; time.sleep(30)")))
    assert backend.job_alive(handle)
    await backend.cancel(handle)
    result = await backend.wait(handle, timeout_s=10)
    assert result.exit_code != 0
    await backend.cleanup(handle)


@pytest.mark.asyncio
async def test_provider_redirected_incident_and_recovery_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    intervention = SimpleNamespace(trigger="poller", service_principal="poller:worklink-ready-queue", extra={
        "poller_name": dispatch_failures.POLLER_NAME, "items": [{
        "issue_id": 1949, "error_signature": "signature", "failure_occurrence_id": "occ",
        "delivery_key": "worklink-run-failure:1949:signature:occ",
    }]})
    assert dispatch_failures.is_dispatch_failure_intervention(intervention)
    assert continuation.maybe_create_worklink_budget_continuation(
        home=tmp_path, event=intervention,
        ctx=SimpleNamespace(tool_call_budget_exhausted=True), record=SimpleNamespace(),
    ) is None

    state_dir = dispatch_failures.dispatch_failure_state_dir(tmp_path)
    state_dir.mkdir(parents=True)
    (state_dir / dispatch_failures.STATE_FILE).write_text(json.dumps({
        "version": 1, "issues": {"1949": {
            "active": True, "signature": "signature", "occurrence_id": "occ",
        }},
    }))
    assert await poller_hooks.recovery_relevance_check(state_dir)(intervention) is True
    (state_dir / dispatch_failures.STATE_FILE).write_text(json.dumps({
        "version": 1, "issues": {},
    }))
    assert await poller_hooks.recovery_relevance_check(state_dir)(intervention) is False
    monkeypatch.setattr(retained_scope, "current_failure_record", lambda *args: None)
    denied = retained_scope.derive_retained_factory_scope(
        intervention,
        SimpleNamespace(canonical="poller:worklink-ready-queue", trigger="poller", authority_profile="github"),
        home=tmp_path,
    )
    assert denied.refusal_reason == "incident occurrence is stale"


def test_provider_merge_closure_reads_provider_failure_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []
    monkeypatch.setattr(merge_closure, "resolve_completion_repository", lambda *args, **kwargs: SimpleNamespace(slug="repo"))
    monkeypatch.setattr(merge_closure, "_pending_intents", lambda *_: {})
    monkeypatch.setattr(merge_closure, "active_failure_identities", lambda *args, **kwargs: [(1949, "sig", "occ")])
    monkeypatch.setattr(merge_closure, "_list_open_issue_ids", lambda *_: set())
    monkeypatch.setattr(merge_closure, "resolve_failure_if_current", lambda *args: seen.append(args))
    monkeypatch.setattr(merge_closure, "resolve_merge_reconciliation_notices", lambda *args, **kwargs: None)
    assert merge_closure.reconcile_merged_leaves(
        tmp_path, chainlink_runner=lambda args: subprocess.CompletedProcess(args, 0, ""),
    ) == []
    assert seen == [(dispatch_failures.dispatch_failure_state_dir(tmp_path), 1949, "sig", "occ")]
