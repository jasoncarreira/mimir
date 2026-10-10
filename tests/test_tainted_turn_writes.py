"""#1937: always-on same-PR veto, egress-only grants, and scratch closure."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir import access_control as ac
from mimir.models import InformationFlowState, IntegrityEffect, Integrity
from tests.test_access_control import _trusted_operator_write_auth, _review_state, _repository_result_labels


def tainted():
    auth = _trusted_operator_write_auth(admin=True)
    scope = _review_state("owner/repo", 7, "fix", "/srv/repo").action_scope
    source = replace(_repository_result_labels("owner/repo", 7, scope.observed_head_sha).sources[0],
                     integrity=Integrity.UNTRUSTED, integrity_effect=IntegrityEffect.ACTIVE_INGEST)
    labels = auth.ifc_labels.with_source(source)
    return replace(auth, ifc_labels=labels, ifc_state=InformationFlowState(labels)), scope


@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("tool", sorted(ac._TAINTED_TURN_WRITE_TOOLS))
def test_every_tainted_write_same_pr_refused(tool, enforce):
    auth, scope = tainted()
    decision = ac.SinkGate.check_sink_flow(tool, "owner/repo#pull/7", auth.ifc_labels, auth,
                                          enforce=enforce, repo_pr_action_scope=scope)
    assert not decision.allowed
    assert decision.reason == "write_blocked_by_untrusted_ingest"
    assert decision.refusal_detail == ac._TAINTED_WRITE_REFUSAL


@pytest.mark.parametrize("tool", ["repo_test", "repo_push", "repo_commit"])
@pytest.mark.parametrize("enforce", [False, True])
def test_egress_grant_never_unlocks_repo_write(tool, enforce):
    auth, scope = tainted()
    target = "owner/repo#pull/7"
    assert auth.ifc_state.approve_sink_once(fallback=auth.ifc_labels, sink_category="forge",
        destination=target, canonical_principal=auth.canonical_principal,
        lifetime_seconds=30, durable_audit=lambda *_: True)
    for _ in range(2):
        decision = ac.SinkGate.check_sink_flow(tool, target, auth.ifc_labels, auth,
                                             enforce=enforce, repo_pr_action_scope=scope)
        assert not decision.allowed
        assert decision.reason == "write_blocked_by_untrusted_ingest"
    assert auth.ifc_state._declassification is not None


def test_unbound_grant_does_not_open_write():
    auth, scope = tainted()
    assert auth.ifc_state.approve_sink_once(fallback=auth.ifc_labels, sink_category="forge",
        destination="owner/repo#pull/7", canonical_principal=auth.canonical_principal,
        lifetime_seconds=30, durable_audit=lambda *_: True)
    assert not ac.SinkGate.check_sink_flow("repo_push", "owner/repo#pull/7", auth.ifc_labels,
        auth, enforce=True, repo_pr_action_scope=scope).allowed


@pytest.mark.parametrize("path", ["state/wiki/a.md", "memory/a.md", "skills/x/SKILL.md",
    "scheduler.yaml", "scratch/../state/a.md", "/workspace/no-git/a.md"])
def test_only_scratch_file_writes(path, tmp_path):
    assert ac.tainted_file_write_target(path, tmp_path)
    assert not ac.tainted_file_write_target("scratch/turns/a.md", tmp_path)


@pytest.mark.parametrize("sibling", ["scratchy", "scratch-old"])
def test_scratch_lexical_siblings_are_live_write_targets(tmp_path, sibling):
    assert ac.tainted_file_write_target(str(tmp_path / sibling / "x.md"), tmp_path)
    assert not ac.tainted_file_write_target("scratch/turns/x.md", tmp_path)


def test_scratch_root_symlink_to_live_is_not_a_write_exception(tmp_path):
    live = tmp_path / "state"
    live.mkdir()
    (tmp_path / "scratch").symlink_to(live, target_is_directory=True)
    assert ac.tainted_file_write_target("scratch/x.md", tmp_path)
    assert ac.tainted_file_write_target(str(live / "x.md"), tmp_path)


def test_write_refusals_do_not_offer_model_unlocks():
    auth, _ = tainted()
    for text in (ac._TAINTED_WRITE_REFUSAL, ac._REPO_PUBLISH_INGEST_REFUSAL,
                 ac._repo_test_ingest_refusal(auth, auth.ifc_labels)):
        for forbidden in ("approve_sink_once", "approve_declassification", "request_operator_approval"):
            assert forbidden not in text


def test_scratch_symlink_to_live_refused(tmp_path):
    (tmp_path / "scratch").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "scratch" / "alias").symlink_to(tmp_path / "state", target_is_directory=True)
    assert ac.tainted_file_write_target("scratch/alias/a.md", tmp_path)


def test_loader_rejects_aliases_and_allows_live(tmp_path):
    from mimir._paths import live_loader_path_allowed
    (tmp_path / "scratch").mkdir()
    target = tmp_path / "scratch/a.md"
    target.write_text("untrusted")
    alias = tmp_path / "live.md"
    alias.symlink_to(target)
    assert not live_loader_path_allowed(alias, tmp_path)
    assert not live_loader_path_allowed(tmp_path / "scratch/../scratch/a.md", tmp_path)
    live = tmp_path / "clean.md"
    live.write_text("trusted")
    assert live_loader_path_allowed(live, tmp_path)


def test_proposal_whole_index_filter_rejects_code(monkeypatch, tmp_path):
    from mimir import proposals
    def git(args, cwd):
        if args[0] == "diff":
            return SimpleNamespace(returncode=0, stdout="memory/core/a.md\0code.py\0")
        return SimpleNamespace(returncode=0, stdout="100644 oid 0\tmemory/core/a.md\0")
    monkeypatch.setattr(proposals, "_git", git)
    assert proposals._check_proposal_index(tmp_path, proposals.PROPOSAL_SURFACES)


def test_loader_production_templates_scheduler_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    from mimir.templates import load_template
    from mimir.scheduler import load_jobs, load_operator_shell_commands, _resolve_prompt_file
    from mimir.skill_catalog import load_skill
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "x.md").write_text("scratch marker")
    prompts = tmp_path / "prompts"
    prompts.symlink_to(scratch, target_is_directory=True)
    assert load_template("x", "default", prompts) == "default"
    assert _resolve_prompt_file(tmp_path, "x.md") is None
    (scratch / "scheduler.yaml").write_text("[]")
    config = tmp_path / "scheduler.yaml"
    config.symlink_to(scratch / "scheduler.yaml")
    assert load_jobs(config)[0] == []
    assert load_operator_shell_commands(config) == ()
    (scratch / "SKILL.md").write_text("---\nname: test\ndescription: test\n---\nmarker")
    skill = tmp_path / "skills/test"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").symlink_to(scratch / "SKILL.md")
    assert load_skill(skill) is None
