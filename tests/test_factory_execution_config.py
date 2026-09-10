from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.contained_execution import CollectedExecutionResult
from mimir.worklink import compute, worker_exec


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
            ["opencode-feature-factory@0.8.3", {"profile": "production", "options": {"parallel": 3}}],
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
