from __future__ import annotations

import json
import os
import stat
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mimir.contained_execution import CollectedExecutionResult
from mimir.worklink import compute, worker_exec


@pytest.fixture
def factory_control_identity(monkeypatch):
    from mimir.worklink import worker_client

    # These tests fake the executor, so they must not require host accounts.
    identity = SimpleNamespace(worklink_uid=42424)
    monkeypatch.setattr(worker_client.identities, "get_identities", lambda: identity)
    return identity


@pytest.mark.asyncio
async def test_retained_factory_control_uses_worker_without_runtime_refresh(
    tmp_path, monkeypatch, factory_control_identity,
):
    from mimir.worklink import worker_client

    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", tmp_path)
    checkout = tmp_path / "repo/41-2/checkout"
    calls = []

    async def launch(client, **kwargs):
        assert client._socket_timeout_s == 30 + worker_client.CANCEL_SOCKET_TIMEOUT_S
        assert client.run_uid == factory_control_identity.worklink_uid
        calls.append((client._launch_op, client.path_checkout, kwargs))
        os.write(kwargs["stdout_sink"].fd, b"retained-status")

        async def wait():
            return 0

        return SimpleNamespace(wait=wait, timed_out=False, output_overflow=False)

    monkeypatch.setattr(worker_client.WorkerClient, "launch", launch)
    result = worker_client.run_factory_control(
        checkout / ".factory-sandboxes/run", ["node", "factory.js", "status"],
        env={"HOME": "/controller", "PATH": "/usr/bin:/bin"},
    )
    assert result.stdout == b"retained-status"
    assert result.returncode == 0
    operation, path, kwargs = calls[0]
    assert operation == "launch_factory_control"
    assert path == checkout
    assert kwargs["local_checkout"] == checkout
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}
    assert "projections" not in kwargs


@pytest.mark.parametrize("failure", ["timed_out", "output_overflow"])
def test_factory_control_rejects_incomplete_results(
    tmp_path, monkeypatch, failure, factory_control_identity,
):
    from mimir.worklink import worker_client

    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", tmp_path)

    async def launch(client, **kwargs):
        assert client.run_uid == factory_control_identity.worklink_uid
        async def wait():
            return 0
        return SimpleNamespace(wait=wait, timed_out=failure == "timed_out", output_overflow=failure == "output_overflow")

    monkeypatch.setattr(worker_client.WorkerClient, "launch", launch)
    expected = subprocess.TimeoutExpired if failure == "timed_out" else RuntimeError
    message = "timed out" if failure == "timed_out" else "factory control output exceeds bounds"
    with pytest.raises(expected, match=message):
        worker_client.run_factory_control(tmp_path / "repo/41-2/checkout", ["node", "status"], env={})


def test_factory_control_socket_uses_its_bound(monkeypatch):
    from mimir.worklink import worker_client

    sock = Mock()
    sock.getsockopt.return_value = struct.pack("3i", 123, 0, 0)
    monkeypatch.setattr(worker_client.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(worker_client.socket, "socket", lambda *args: sock)
    client = worker_client.WorkerClient(None)
    client._socket_timeout_s = 7
    assert client._connect() is sock
    sock.settimeout.assert_called_once_with(7)


@pytest.mark.parametrize("side", ["client", "executor"])
def test_executor_requires_peer_credentials_before_side_effects(tmp_path, monkeypatch, side):
    from mimir.worklink import worker_client

    monkeypatch.delattr(worker_client.socket, "SO_PEERCRED", raising=False)
    create_socket = Mock(side_effect=AssertionError("unauthenticated socket created"))
    monkeypatch.setattr(worker_client.socket, "socket", create_socket)
    path = tmp_path / "not-created" / "executor.sock"
    with pytest.raises(RuntimeError, match="requires Linux SO_PEERCRED peer authentication"):
        if side == "client":
            worker_client.WorkerClient(None, socket_path=path)._connect()
        else:
            worker_exec.serve(path)
    create_socket.assert_not_called()
    assert not path.parent.exists()


@pytest.mark.parametrize("relative,result", [
    ("repo/41-2/checkout/nested", "valid"),
    ("repo/41-2", "none"),
    ("repo/41-2/project", "none"),
    ("../41-2/checkout", "error"),
    ("repo/0-2/checkout", "error"),
    ("repo/41-0/checkout", "error"),
    ("repo/41-2/checkout/../elsewhere", "error"),
])
def test_factory_control_path_classification(tmp_path, monkeypatch, relative, result):
    from mimir.worklink import worker_client

    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", tmp_path)
    path = tmp_path / relative
    if result == "error":
        with pytest.raises(ValueError):
            worker_client.factory_checkout_for_path(path)
    elif result == "none":
        assert worker_client.factory_checkout_for_path(path) is None
    else:
        assert worker_client.factory_checkout_for_path(path) == (tmp_path / "repo/41-2/checkout", 41, 2)
    assert worker_client.factory_checkout_for_path(tmp_path.parent / "outside/repo/41-2/checkout") is None


@pytest.mark.parametrize("guard", ["owner", "directory", "nofollow"])
def test_factory_controller_boundary_guard(tmp_path, monkeypatch, guard):
    from mimir.worklink import orchestrator, worker_client

    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "get_identities", lambda: SimpleNamespace(mimir_uid=os.getuid()))
    path = tmp_path / "repo/41-2/checkout"
    path.mkdir(parents=True)
    path.parent.chmod(0o2700)
    real_stat = Path.stat
    if guard == "nofollow":
        moved = path.parent.with_name("saved")
        path.parent.rename(moved)
        path.parent.symlink_to(moved, target_is_directory=True)
    else:
        def observed(target, **kwargs):
            value = real_stat(target, **kwargs)
            if target == path.parent:
                return SimpleNamespace(
                    st_uid=os.getuid() + (guard == "owner"),
                    st_mode=(stat.S_IFREG if guard == "directory" else stat.S_IFDIR) | 0o2700,
                )
            return value
        monkeypatch.setattr(Path, "stat", observed)
    controller = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    worker = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
    monkeypatch.setattr(worker_client, "run_factory_control", worker)
    with pytest.raises(orchestrator.WorklinkError, match="boundary is not controller-owned"):
        orchestrator._factory_git_runner(controller)(["git", "-C", str(path), "status"])
    controller.assert_not_called()
    worker.assert_not_called()


def test_factory_git_recovery_uses_owner_not_safe_directory(tmp_path, monkeypatch):
    from mimir.worklink import orchestrator, worker_client

    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "get_identities", lambda: SimpleNamespace(mimir_uid=os.getuid()))
    checkout = tmp_path / "repo/41-2/checkout"
    checkout.mkdir(parents=True)
    calls = []

    def controller(args):
        calls.append(("controller", args))
        return subprocess.CompletedProcess(args, 0, "initial", "")

    def worker(root, args, **kwargs):
        assert root == checkout
        assert "GIT_CONFIG_COUNT" not in kwargs["env"]
        assert not any("safe.directory" in arg for arg in args)
        calls.append(("worker", args))
        return subprocess.CompletedProcess(args, 0, b"retained", b"")

    monkeypatch.setattr(worker_client, "run_factory_control", worker)
    run = orchestrator._factory_git_runner(controller)
    args = ["git", "-C", str(checkout / ".factory-sandboxes/run"), "rev-parse", "HEAD"]
    checkout.parent.chmod(0o2700)
    assert run(args).stdout == "initial"
    checkout.parent.chmod(0o2750)
    assert run(args).stdout == "retained"
    assert [who for who, _ in calls] == ["controller", "worker"]


def test_legacy_factory_recovery_refuses_before_git_or_lock_mutation(tmp_path, monkeypatch):
    from mimir.worklink import orchestrator, worker_client

    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", tmp_path)
    launcher = tmp_path / "installed/factory.js"
    retained = SimpleNamespace(
        issue_id=41, run_id=orchestrator.factory_record_run_ids(41)[0], repository="owner/repo",
        base_ref="main", launcher=str(launcher), controller_phase="failed", session="retained",
        sandbox=str(tmp_path / "repo/41-2/.factory-sandboxes/run"), branch="feature/run",
    )
    Path(retained.sandbox).mkdir(parents=True)
    verify = Mock(return_value="head")
    monkeypatch.setattr(orchestrator, "_verify_factory_checkout", verify)

    def runner(args):
        assert args == ["git", "-C", str(tmp_path / "base"), "config", "--get", "remote.origin.url"]
        return subprocess.CompletedProcess(args, 0, "https://github.com/owner/repo.git", "")

    with pytest.raises(orchestrator.WorklinkError, match="legacy factory checkout"):
        orchestrator._verify_factory_recovery_target(
            runner=SimpleNamespace(repo=tmp_path / "base"), issue=SimpleNamespace(issue_id=41),
            retained=retained, launcher=launcher, repo_slug="owner/repo", base="main",
            command_runner=runner,
        )
    verify.assert_not_called()


@pytest.mark.parametrize("command", ["status", "resume", "heartbeat", "lock"])
def test_factory_backend_controls_use_retained_owner(tmp_path, monkeypatch, command):
    from mimir.worklink import worker_client
    from mimir.worklink.backends import feature_factory

    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", tmp_path)
    entrypoint = tmp_path / "installed/bin/factory.js"
    monkeypatch.setattr(feature_factory, "resolve_factory_entrypoint", lambda path: entrypoint)
    sandbox = tmp_path / "repo/41-2/checkout/.factory-sandboxes/run"
    calls = []

    def control(path, argv, **kwargs):
        calls.append((path, argv))
        return subprocess.CompletedProcess(argv, 0, b"{}", b"")

    def forbidden(*args, **kwargs):
        raise AssertionError("controller ran a retained-tree control operation")

    monkeypatch.setattr(worker_client, "run_factory_control", control)
    backend = feature_factory.FeatureFactoryBackend(runner=forbidden)
    backend._control(entrypoint, [command, "run", "--repo", str(sandbox)], sandbox=sandbox)
    assert calls == [(sandbox, ["node", str(entrypoint), command, "run", "--repo", str(sandbox)])]


@pytest.mark.asyncio
@pytest.mark.parametrize("opencode", [True, False])
async def test_factory_projects_native_config_only_for_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opencode: bool,
) -> None:
    controller = tmp_path / "controller"
    controller.mkdir()
    config = controller / "opencode.jsonc"
    native = {
        "model": "anthropic/test-model",
        "plugin": [
            ["opencode-feature-factory@0.8.6", {"profile": "production", "options": {"parallel": 3}}],
            "opencode-project-memory@0.1.0",
            "opencode-anthropic-auth@0.0.13",
        ],
        "command": {"factory": {"template": "factory $ARGUMENTS"}},
        "agent": {"factory": {"mode": "primary"}},
        "provider": {
            "anthropic": {"options": {"apiKey": "{env:FACTORY_PROVIDER_KEY}"}},
            "unused": {"options": {"apiKey": "unneeded-secret"}},
        },
    }
    config.write_text("// trusted controller configuration\n" + json.dumps(native))
    auth = controller / ".local/share/opencode/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({
        "anthropic": {"type": "oauth", "access": "fresh-access", "refresh": "fresh-refresh"},
        "unused": {"type": "api", "key": "unneeded-secret"},
    }))
    monkeypatch.setenv("HOME", str(controller))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("OPENCODE_CONFIG", str(config if opencode else controller / "missing"))
    monkeypatch.setenv("FACTORY_PROVIDER_KEY", "materialized-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shadowing-key")
    monkeypatch.setenv("PATH", "/opt/mimir-opencode/bin:/usr/bin:/bin")
    observed = []

    async def execute(command, capability, env, projections, **kwargs):
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_VALUE_0" not in env
        observed.append((dict(env), {p.path: json.loads(p.document) for p in projections}))
        capability._contained_started(SimpleNamespace(pid=None))
        return CollectedExecutionResult(0, b"", b"", False, False, 0, 0)

    monkeypatch.setattr(compute, "execute_contained", execute)
    backend = compute.LocalSubprocessComputeBackend(_worker_client=object())
    spec = compute.WorkSpec(
        issue_id=41, attempt=2, repo_url="", base_ref="", branch="", prompt="",
        rules=None, test_command="", backend="feature_factory", timeout_s=10,
        local_checkout=tmp_path / "41-2",
        local_argv=("/opt/mimir-opencode/bin/opencode", "run") if opencode else ("python", "-c", "pass"),
        env={"FACTORY_INPUT": "issue-41"},
    )
    for _ in range(2):
        handle = await backend.launch(spec)
        assert (await backend.wait(handle, 10)).exit_code == 0
        await backend.cleanup(handle)
    first_env, projections = observed[0]
    assert first_env["XDG_DATA_HOME"] == str(spec.local_checkout / ".factory-runtime/data")
    assert observed[1][0]["XDG_DATA_HOME"] == first_env["XDG_DATA_HOME"]
    assert observed[1][0]["XDG_CONFIG_HOME"] != first_env["XDG_CONFIG_HOME"]
    assert first_env["PATH"] == "/opt/mimir-opencode/bin:/usr/bin:/bin"
    assert first_env["FACTORY_INPUT"] == "issue-41"
    if not opencode:
        assert projections == {}
        return
    projected = projections[".config/opencode/opencode.json"]
    for key in ("plugin", "command", "agent", "model"):
        assert projected[key] == native[key]
    assert projected["provider"] == {"anthropic": {"options": {"apiKey": "materialized-key"}}}
    assert projections[".local/share/opencode/auth.json"] == {
        "anthropic": {"type": "oauth", "access": "fresh-access", "refresh": "fresh-refresh"},
    }
    assert "ANTHROPIC_API_KEY" not in first_env
    assert first_env["OPENCODE_CONFIG"] == first_env["XDG_CONFIG_HOME"] + "/opencode/opencode.json"


def test_factory_runtime_refreshes_auth_and_retains_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "attempt"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    for attempt in range(2):
        home = tmp_path / f"home-{attempt}"
        auth = home / ".local/share/opencode/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"anthropic": {"type": "api", "key": f"key-{attempt}"}}))
        worker_exec._prepare_factory_runtime(home)
        data = checkout / ".factory-runtime/data/opencode"
        assert (data / "auth.json").read_bytes() == auth.read_bytes()
        assert (data / "auth.json").stat().st_mode & 0o777 == 0o600
        session = data / "opencode.db"
        if attempt == 0:
            session.write_bytes(b"retained-session")
        else:
            assert session.read_bytes() == b"retained-session"


def test_factory_runtime_without_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    stale = tmp_path / ".factory-runtime/data/opencode/auth.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"old-provider": {"type": "api", "key": "stale"}}')
    worker_exec._prepare_factory_runtime(tmp_path / "empty-home")
    assert (tmp_path / ".factory-runtime/data/opencode").is_dir()
    assert not (tmp_path / ".factory-runtime/data/opencode/auth.json").exists()


def test_factory_runtime_preparation_follows_successful_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    monkeypatch.setattr(worker_exec, "_drop_worker", lambda fd: events.append(("drop", fd)))
    monkeypatch.setattr(worker_exec, "_prepare_factory_runtime", lambda home: events.append(("runtime", home)))
    worker_exec._drop_factory(42, tmp_path)
    assert events == [("drop", 42), ("runtime", tmp_path)]
    events.clear()

    def failed_drop(fd):
        raise PermissionError("drop failed")

    monkeypatch.setattr(worker_exec, "_drop_worker", failed_drop)
    with pytest.raises(PermissionError, match="drop failed"):
        worker_exec._drop_factory(42, tmp_path)
    assert events == []
