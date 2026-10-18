"""Selected provider policy is the same authorization surface as the frozen oracle."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from mimir.plugins.core import core_policy_registry, core_runtime_names
from mimir.plugins.manager import load_plugins
from mimir.tools.registry import all_mimir_tools


def test_selected_cultivate_owns_coding_policy_and_preserves_descriptors():
    legacy = core_policy_registry()
    selected = load_plugins(enable="cultivate").policy
    names = set(core_runtime_names("coding")) | {"worklink_run", "worklink_resume"}
    assert len(names) == 40
    for name in names:
        before = legacy.tools[name]
        after = selected.tools[name]
        assert after.owner == "cultivate"
        for field in (
            "flow", "tier", "operation_class", "result_domain", "readable_domain",
            "writes_code_or_state", "profiles", "read_backend", "memberships",
        ):
            assert getattr(after, field) == getattr(before, field), (name, field)
        assert after.descriptor == before.descriptor, name
        assert (after.typed_action.value if after.typed_action else None) == (
            before.typed_action.value if before.typed_action else None
        ), name
        assert after.factory().name == name


def test_disabled_provider_is_not_imported_and_no_coding_tool_is_materialized():
    program = """\
import sys
from mimir.plugins.manager import load_plugins
from mimir.tools.registry import all_mimir_tools
p = load_plugins(enable='').policy
assert not any(k.startswith('mimir_cultivate') for k in sys.modules)
names = {tool.name for tool in all_mimir_tools('openai:test', policy=p)}
assert not names & {'repo_status', 'pr_job_log', 'spawn_open_code'}
"""
    process = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert process.returncode == 0, process.stderr


def test_selected_provider_tool_inventory_has_no_duplicate_names():
    selected = load_plugins(enable="cultivate").policy
    tools = all_mimir_tools("openai:test", policy=selected, coding_enabled=False)
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names))
    assert {"worklink_run", "worklink_resume", "repo_status"} <= set(names)


def test_selected_ci_run_and_branch_grants_cannot_substitute_for_each_other(monkeypatch):
    from mimir.access_control import CapabilityTier, ToolRegistry, build_trigger_service_principal
    from mimir.models import AuthContext
    from mimir.plugins.core import policy_scope

    selected = load_plugins(enable="cultivate").policy
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    with policy_scope(selected):
        service = build_trigger_service_principal(
            canonical="poller:github", trigger="poller", profile="github",
            tier=CapabilityTier.SCOPE_CONTAINED,
            capabilities=("ci_run", "ci_run_jobs", "ci_recent_runs"), creation_path="test",
        )
        auth = AuthContext(
            principal=service.canonical, canonical_principal=service.canonical,
            roles=("service",), event_ingress=None, trigger="poller", channel_id="poller:github",
            interactivity=None, is_service=True, service_authority=service,
            ci_run_targets=frozenset({("owner/repo", 42)}),
            ci_branch_targets=frozenset({("owner/repo", "main", 7)}),
        )
        registry = ToolRegistry()
        run = registry.authorize_tool(
            "ci_run", auth, enforce=True, policy=selected,
            arguments={"repository": "owner/repo", "run_id": 99, "branch": "main", "workflow_id": 7},
        )
        branch = registry.authorize_tool(
            "ci_recent_runs", auth, enforce=True, policy=selected,
            arguments={"repository": "owner/repo", "run_id": 42, "branch": "other"},
        )
        assert not run.allowed and run.reason == "ci_run_scope_denied"
        assert not branch.allowed and branch.reason == "ci_run_scope_denied"


def test_forge_job_log_uses_provider_capture_only_after_head_binding(monkeypatch):
    from mimir_cultivate import ci, forge

    client = forge.GitHubForgeClient(token="secret")
    scope = SimpleNamespace(canonical_repo="owner/repo", pr_number=17, observed_head_sha="a" * 40)
    job = {"id": 42, "run_id": 19, "head_sha": scope.observed_head_sha,
           "run_url": "https://api.github.com/repos/owner/repo/actions/runs/19",
           "status": "completed", "conclusion": "failure"}
    run = {"id": 19, "repository": {"full_name": "owner/repo"},
           "head_sha": scope.observed_head_sha, "status": "completed"}
    monkeypatch.setattr(client, "_request", lambda _method, path, **_kw:
                        job if "/jobs/" in path else run)
    calls = []
    monkeypatch.setattr(ci, "capture_job_log", lambda repo, job_id, **_kw:
                        (calls.append((repo, job_id)) or b"[REDACTED] failure", ""))
    assert client.get_job_log(scope, 42) == "[REDACTED] failure"
    assert calls == [("owner/repo", 42)]
    job["head_sha"] = "b" * 40
    with pytest.raises(forge.ForgeError, match="outside the scoped"):
        client.get_job_log(scope, 42)
    assert calls == [("owner/repo", 42)]


def test_forge_unattested_thread_withholds_through_provider_trust(monkeypatch):
    from mimir_cultivate import forge, github_trust

    scope = SimpleNamespace(canonical_repo="owner/repo")
    runtime = SimpleNamespace(context=object())
    monkeypatch.setattr(forge, "_client", lambda _scope: object())
    monkeypatch.setattr(forge, "_author_verdict", lambda *_args: None)
    monkeypatch.setattr(forge, "_publish_author_attestation", lambda *_args: None)
    monkeypatch.setattr(forge, "_withheld_event", lambda *_args: None)
    calls = []
    original = github_trust.partition

    def traced(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(github_trust, "partition", traced)
    item = forge.CommentProjection("1", "outsider", "secret content", "now", "now")
    result = forge._withhold_thread(runtime, scope, (item,), "pr_comments")
    assert calls == [True]
    assert len(result) == 1 and result[0]["withheld"] is True
    assert "secret content" not in str(result)


@pytest.mark.asyncio
async def test_registered_repository_alert_does_not_call_core_resolver(monkeypatch, tmp_path):
    from mimir_cultivate.registration import _repository_binding_alerts
    from mimir import access_control

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    monkeypatch.setattr(access_control, "repo_binding_startup_alerts",
                        lambda: pytest.fail("core resolver was called"))
    events = []

    async def record(kind, **fields):
        events.append((kind, fields))

    await _repository_binding_alerts(record)
    assert len(events) == 1
    assert events[0][0] == "github_repo_binding_attention_required"
    assert events[0][1]["repository"] == "owner/repo"
    assert events[0][1]["match_count"] == 0


def test_provider_repository_alert_rejects_ambiguous_legacy_roots(monkeypatch, tmp_path):
    from mimir_cultivate.repository import repo_binding_startup_alerts

    home = tmp_path / "home"
    home.mkdir()
    roots = (tmp_path / "one", tmp_path / "two")
    for root in roots:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run([
            "git", "-C", str(root), "remote", "add", "origin",
            "https://github.com/owner/repo.git",
        ], check=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", ",".join(f"{root}:rw" for root in roots))
    [alert] = repo_binding_startup_alerts()
    assert alert["repository"] == "owner/repo"
    assert alert["match_count"] == 2
    assert alert["operator_visible"] is True
    assert sorted(alert["probed_roots"]) == sorted(map(str, roots))
