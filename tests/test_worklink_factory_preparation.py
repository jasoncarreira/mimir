from __future__ import annotations

import ast
import inspect
import json
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mimir.worklink import orchestrator
from mimir.worklink.backends.base import WorkOrder
from mimir.worklink.backends.feature_factory import (
    FACTORY_VERSION,
    FactoryContractError,
    FeatureFactoryBackend,
    parse_factory_status,
)


@pytest.fixture
def factory_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "OPENCODE_CONFIG",
                "OPENCODE_CONFIG_CONTENT", "MIMIR_OPENCODE_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:gpt-5.6-luna")
    config = tmp_path / ".config/opencode/opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({
        "provider": {"openai": {}, "other": {"options": {"apiKey": "unrelated"}}},
        "plugin": ["untrusted-plugin"],
        "mcp": {"untrusted": {"command": ["arbitrary"]}},
    }))
    auth = tmp_path / ".local/share/opencode/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({
        "openai": {"type": "oauth", "refresh": "subscription"},
        "other": {"type": "api", "key": "unrelated"},
    }))
    return config


@pytest.mark.parametrize("coding_enabled", ["true", "false"])
def test_factory_projects_only_selected_provider_and_pinned_plugin(
    tmp_path, monkeypatch, factory_env, coding_enabled,
):
    monkeypatch.setenv("MIMIR_CODING_ENABLED", coding_enabled)
    spec = FeatureFactoryBackend().work_spec(
        WorkOrder(41, tmp_path, "build", None, 60, env={}),
        attempt=2, repo_url="https://github.com/owner/repo.git",
        base_ref="main", branch="feature/chainlink-41", test_command="pytest",
    )
    documents = {p.path: json.loads(p.document) for p in spec.backend_config["worker_projections"]}
    assert documents == {
        ".config/opencode/opencode.json": {
            "model": "openai/gpt-5.6-luna", "provider": {"openai": {}},
            "plugin": [f"opencode-feature-factory@{FACTORY_VERSION}"],
        },
        ".local/share/opencode/auth.json": {
            "openai": {"type": "oauth", "refresh": "subscription"},
        },
    }


def test_factory_rejects_unsafe_selected_provider(tmp_path, factory_env):
    factory_env.write_text(json.dumps({
        "provider": {"openai": {"options": {"apiKey": "inline-secret"}}},
    }))
    with pytest.raises(FactoryContractError, match="config_unsafe_inline_secret"):
        FeatureFactoryBackend().work_spec(
            WorkOrder(41, tmp_path, "build", None, 60, env={}),
            attempt=2, repo_url="https://github.com/owner/repo.git",
            base_ref="main", branch="feature/chainlink-41", test_command="pytest",
        )


@pytest.mark.parametrize("existing_parent", [False, True])
def test_factory_sandbox_parent_is_group_writable_setgid(tmp_path, existing_parent):
    checkout = tmp_path / "41-2"
    checkout.mkdir()
    checkout.chmod(0o2770)
    root = checkout / ".factory-sandboxes"
    if existing_parent:
        root.mkdir(mode=0o700)
    sandbox = root / "chainlink-41"
    result = orchestrator._create_factory_sandbox(
        SimpleNamespace(sandbox=str(sandbox), run_id="chainlink-41"),
        SimpleNamespace(path=checkout),
    )
    assert result == sandbox
    assert not sandbox.exists()
    assert stat.S_IMODE(root.stat().st_mode) == 0o2770
    assert root.stat().st_gid == checkout.stat().st_gid


def test_fresh_factory_checkout_is_unconditionally_worker_eligible():
    tree = ast.parse(inspect.getsource(orchestrator))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "_create_backend_checkout"
             and any(k.arg == "backend" and isinstance(k.value, ast.Name)
                     and k.value.id == "selected" for k in node.keywords)]
    assert len(calls) == 1
    keyword = next(k for k in calls[0].keywords if k.arg == "worker_eligible")
    assert isinstance(keyword.value, ast.Constant) and keyword.value.value is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "git", "token", "identity"])
async def test_recovery_prepares_verified_explicit_identity_before_launch(
    tmp_path, monkeypatch, factory_env, failure,
):
    sandbox = tmp_path / "41-2/.factory-sandboxes/chainlink-41"
    sandbox.mkdir(parents=True)
    monkeypatch.setenv("MIMIR_FACTORY_PUBLISHING_IDENTITY", "publisher")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token" if failure != "token" else "")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    verify = Mock()
    if failure == "identity":
        verify.side_effect = orchestrator.GitHubIdentityVerificationError("identity mismatch")
    client = Mock(return_value=SimpleNamespace(verify_identity=verify))
    monkeypatch.setattr(orchestrator, "GitHubForgeClient", client)
    monkeypatch.setattr(orchestrator, "_verify_factory_recovery_binding", lambda **_: sandbox)
    monkeypatch.setattr(orchestrator, "factory_process_is_alive", lambda _: False)
    monkeypatch.setattr(orchestrator, "factory_process_is_verified_dead", lambda _: True)
    monkeypatch.setattr(orchestrator, "_repo_remote_url", lambda *a, **k: "https://github.com/owner/repo.git")
    monkeypatch.setattr(orchestrator, "_factory_checkout_repository", lambda *a: "owner/repo")
    status = parse_factory_status({
        "run_id": "chainlink-41", "valid": True, "sandbox_path": str(sandbox),
        "status": "running", "mode": "autonomous", "branch": "feature/chainlink-41",
        "pr_base": "main", "pr_draft": False, "lock": "fresh", "dead_lock": False,
        "lock_session": "session-1", "next": "implementation",
    })
    backend = FeatureFactoryBackend()
    monkeypatch.setattr(FeatureFactoryBackend, "status", lambda *a, **k: status)
    monkeypatch.setattr(FeatureFactoryBackend, "resume", lambda *a, **k: status)
    retained = SimpleNamespace(
        run_id="chainlink-41", session="session-1", launcher=backend.entrypoint,
        controller_phase="parked", repository="owner/repo", attempt=2,
        base_ref="main", branch="feature/chainlink-41", sandbox=str(sandbox),
    )
    launch = AsyncMock(side_effect=RuntimeError("launch reached"))

    def runner(argv):
        assert argv[:5] == ["git", "-C", str(sandbox), "config", "--get"]
        value = {"user.name": "Factory Author", "user.email": "factory@example.com"}[argv[-1]]
        return subprocess.CompletedProcess(argv, 1 if failure == "git" else 0, value, "")

    expected = {None: "launch reached", "git": "Git identity preflight failed",
                "token": "GITHUB_TOKEN", "identity": "identity mismatch"}[failure]
    with pytest.raises((RuntimeError, orchestrator.WorklinkError), match=expected):
        await orchestrator.WorklinkRunner(home=tmp_path, repo=tmp_path)._recover_factory_070(
            issue=orchestrator.IssueContext(41, "epic", "build", {"worklink:epic"}),
            claim_record=SimpleNamespace(), claims=SimpleNamespace(), backend=backend,
            compute=SimpleNamespace(launch=launch), retained=retained,
            launcher=Path(backend.entrypoint), repo_slug="owner/repo", base="main",
            test_cmd="pytest", runner=runner,
        )
    if failure:
        launch.assert_not_awaited()
    else:
        client.assert_called_once_with(token="github-token")
        verify.assert_called_once_with("publisher")
        spec = launch.call_args.args[0]
        assert spec.local_checkout == sandbox
        assert spec.env == {
            "MIMIR_HOME": str(tmp_path), "GITHUB_TOKEN": "github-token",
            "GH_TOKEN": "github-token", "FACTORY_PUBLISHING_IDENTITY": "publisher",
            "GIT_AUTHOR_NAME": "Factory Author", "GIT_COMMITTER_NAME": "Factory Author",
            "GIT_AUTHOR_EMAIL": "factory@example.com", "GIT_COMMITTER_EMAIL": "factory@example.com",
        }
        assert len(spec.backend_config["worker_projections"]) == 2
