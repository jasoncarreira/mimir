"""Selected provider policy is the same authorization surface as the frozen oracle."""

from __future__ import annotations

import subprocess
import sys
import json
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.plugins.core import policy_scope
from mimir.plugins.manager import load_plugins
from mimir.tool_descriptors import _PAYLOAD_EXTRACTOR_UNDECLARED, _TARGET_EXTRACTOR_UNDECLARED
from mimir.tools.registry import all_mimir_tools
from mimir_cultivate.policy import CODING_TOOLS, CULTIVATE_TOOL_NAMES, TRACKER_TOOL_NAMES
from tests.test_plugin_policy_parity import (
    CODING_WRITES_CODE_OR_STATE, _canonical, _extractor, _extractor_results, _prepare_inputs,
)


def test_selected_cultivate_matches_frozen_pre_extraction_policy(monkeypatch, tmp_path):
    oracle = json.loads((Path(__file__).parents[3] / "tests/fixtures/plugin_core_policy_baseline.json").read_text())
    tables = oracle["tables"]
    selected = load_plugins(enable="cultivate").policy
    assert len(CODING_TOOLS) == 42
    for name in CODING_TOOLS - {"repo_restore_file", "repo_file_at_revision"}:
        after = selected.tools[name]
        before = oracle["descriptors"][name]
        assert after.owner == "cultivate"
        assert after.flow == tables["flow"][name]
        assert after.tier == tables["tier"][name]
        assert after.operation_class == tables["catalog"][name]
        assert after.result_domain == tables["protected_domains"].get(name)
        assert after.readable_domain == tables["readable_domains"].get(name)
        assert after.read_backend == (name in tables["read_backend"])
        assert after.writes_code_or_state == (name in CODING_WRITES_CODE_OR_STATE)
        assert after.profiles == frozenset(
            profile for profile, members in tables["profiles"].items() if name in members
        )
        for membership, members in (
            ("forge_actions", tables["forge_actions"]),
            ("repo_actions", tables["repo_actions"]),
            ("retained_repo_tools", tables["retained_repo_tools"]),
            ("worklink_build_tools", tables["worklink_build_tools"]),
            ("repo_publish_tools", tables["repo_publish_tools"]),
            ("tainted_writes", tables["tainted_writes"]),
            ("standing_review", tables["standing_review"]),
            ("remediation_effect", tables["remediation_effect"]),
            ("typed_error_signal", tables["typed_error_signal"]),
            ("write_resource_operations", tables["write_resource_operations"]),
        ):
            assert (membership in after.memberships) == (name in members), (name, membership)
        if name in tables["forge_actions"] or name in tables["repo_actions"]:
            assert (after.typed_action.value if after.typed_action else None) == (
                tables["forge_actions"].get(name, tables["repo_actions"].get(name))
            )
        else:
            assert after.typed_action is None
        assert after.descriptor is not None
        for field in fields(after.descriptor):
            value = getattr(after.descriptor, field.name)
            if field.name == "result_origin":
                value = int(value)
            elif field.name in {"sink_target_extractor", "sink_payload_extractor"}:
                undeclared = (_TARGET_EXTRACTOR_UNDECLARED if field.name == "sink_target_extractor"
                              else _PAYLOAD_EXTRACTOR_UNDECLARED)
                value = _extractor(value, undeclared)
                for function in ("_repo_pr_target", "_spawn_target", "_worklink_target"):
                    value = value.replace(f"mimir_cultivate.policy.{function}:",
                                          f"mimir.tool_descriptors.{function}:")
            assert value == before[field.name], (name, field.name)
        assert after.factory().name == name

    home = tmp_path / "oracle-home"
    _prepare_inputs(home, monkeypatch)
    with policy_scope(selected):
        extracted = _extractor_results(home)
    for name in CODING_TOOLS - {"repo_restore_file", "repo_file_at_revision"}:
        assert json.loads(_canonical(extracted[name], home)) == oracle["extractor_results"][name], name


def test_history_tools_have_exact_provider_policy() -> None:
    from mimir.plugins.core import CapabilityTier
    from mimir.access_control import ToolFlowDirection
    from mimir.tool_descriptors import ResultOriginKind, SinkCategory
    from mimir_cultivate.policy import RepoPRAction

    selected = load_plugins(enable="cultivate").policy
    read = selected.tools["repo_file_at_revision"]
    write = selected.tools["repo_restore_file"]
    assert {tool.name for tool in (read.factory(), write.factory())} == {
        "repo_file_at_revision", "repo_restore_file",
    }
    assert read.flow == ToolFlowDirection.SOURCE
    assert write.flow == ToolFlowDirection.SINK
    assert read.tier == CapabilityTier.SCOPE_CONTAINED
    assert write.tier == CapabilityTier.SCOPED_WITH_PROVENANCE
    assert read.profiles == selected.tools["repo_diff"].profiles
    assert write.profiles == selected.tools["repo_stage"].profiles
    assert read.typed_action == RepoPRAction.INSPECT
    assert write.typed_action == RepoPRAction.WRITE
    assert "repo_actions" in read.memberships & write.memberships
    assert "tainted_writes" in write.memberships
    assert "retained_repo_tools" not in read.memberships | write.memberships
    assert read.result_domain == read.readable_domain == "repository"
    assert read.read_backend and not write.read_backend
    assert read.descriptor.git_operation_result
    assert read.descriptor.result_origin == ResultOriginKind.REPOSITORY
    assert write.descriptor.sink_category == SinkCategory.FORGE
    assert write.descriptor.sink_destination == "bound_pull_request"
    assert write.descriptor.result_origin == ResultOriginKind.NON_INGESTING
    assert write.writes_code_or_state


def test_selected_registration_uses_immutable_provider_metadata_not_core_inventory(monkeypatch):
    from mimir.plugins import core
    from mimir_cultivate import policy as provider_policy

    def missing_core_inventory():
        raise AssertionError("selected registration consulted the legacy core inventory")

    monkeypatch.setattr(core, "core_policy_registry", missing_core_inventory)
    selected = load_plugins(enable="cultivate").policy
    assert {name for name, tool in selected.tools.items() if tool.owner == "cultivate"} == CULTIVATE_TOOL_NAMES
    with pytest.raises(TypeError):
        provider_policy._TOOL_DECLARATIONS["repo_status"] = None


def test_disabled_provider_is_not_imported_and_no_coding_tool_is_materialized():
    program = """\
import sys
from mimir.plugins.manager import load_plugins
from mimir.tools.registry import all_mimir_tools
p = load_plugins(enable='').policy
assert not any(k.startswith('mimir_cultivate') for k in sys.modules)
names = {tool.name for tool in all_mimir_tools('openai:test', policy=p)}
assert not names & {'repo_status', 'pr_job_log', 'spawn_open_code', 'tracker_file_work', 'tracker_comment', 'tracker_set_state'}
"""
    process = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert process.returncode == 0, process.stderr


def test_selected_provider_tool_inventory_has_no_duplicate_names(monkeypatch):
    from mimir_cultivate import forge

    selected = load_plugins(enable="cultivate").policy
    expected = {name for name, tool in selected.tools.items() if tool.owner == "cultivate"}
    assert expected
    monkeypatch.setattr(forge, "_github_identity_degraded", False)
    tools = all_mimir_tools("openai:test", policy=selected, require_provider_available=False)
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names))
    assert expected <= set(names)

    monkeypatch.setattr(forge, "_github_identity_degraded", True)
    degraded_names = {
        tool.name for tool in all_mimir_tools(
            "openai:test", policy=selected, require_provider_available=False,
        )
    }
    assert not degraded_names & expected


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
