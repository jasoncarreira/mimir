"""Closed, content-free refusal reasons for the PR checkout lease boundary."""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
from types import SimpleNamespace

import pytest

from mimir import access_control as ac
from mimir import event_logger, repo_tools
from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME
from mimir.repo_tools import GitProcessResult
from tests.lease_attestation_helper import assert_lease_attested
from tests.test_lease_source_labels import _real_attested_lease


@pytest.fixture
def attested(tmp_path):
    auth, scope, lease, *_ = _real_attested_lease(tmp_path)
    assert_lease_attested(auth, scope, lease)
    return auth, scope, lease


def _ahead(attested, monkeypatch):
    auth, scope, lease = attested
    head = "f" * 40
    lease.head_sha = head
    auth.repo_review_state.record_git_head(scope.scope_id, head)
    monkeypatch.setattr(ac, "_observed_checkout_state", lambda _: (scope.head_ref, head))
    monkeypatch.setattr(ac, "_lease_has_clean_lineage", lambda *args: True)
    return head


@pytest.mark.parametrize("case,reason", [
    ("trust", "no_author_trust"),
    ("lease", "scope_lease_mismatch"),
    ("scope", "scope_lease_mismatch"),
    ("repo", "scope_lease_mismatch"),
    ("number", "scope_lease_mismatch"),
    ("inactive", "inactive_lease"),
    ("missing_head", "missing_expected_head_or_branch"),
    ("missing_branch", "missing_expected_head_or_branch"),
    ("missing_path", "missing_expected_head_or_branch"),
    ("review", "review_state_mismatch"),
    ("recorded", "missing_recorded_head"),
    ("observed", "checkout_state_mismatch"),
    ("branch", "checkout_state_mismatch"),
    ("lineage", "lineage_commit_mismatch"),
    ("head_state", "checkout_state_mismatch"),
    ("cached_false", "commit_identity_mismatch"),
    ("git_timeout", "git_timeout"),
    ("git_exit", "git_nonzero_exit"),
    ("git_missing", "git_missing_binary"),
    ("git_limit", "git_output_limit"),
    ("git_malformed", "git_malformed_output"),
    ("git_error", "git_subprocess_error"),
])
def test_lease_verdict_branch(attested, monkeypatch, case, reason):
    auth, scope, lease = attested
    if case == "trust":
        auth.ifc_state.pr_checkout_author_trust.clear()
    elif case == "lease":
        lease = None
    elif case == "scope":
        lease.scope_id = "different"
    elif case == "repo":
        lease.canonical_repo = "other/repo"
    elif case == "number":
        lease.pr_number = -1
    elif case == "inactive":
        lease.is_active = False
    elif case == "missing_head":
        object.__setattr__(scope, "observed_head_sha", "")
    elif case == "missing_branch":
        object.__setattr__(scope, "destination_ref", "refs/heads/")
    elif case == "missing_path":
        lease.path = None
    elif case == "review":
        object.__setattr__(auth.repo_review_state, "checkout_lease", SimpleNamespace(head_sha="c" * 40))
        object.__setattr__(auth.repo_review_state, "git_expected_head", "c" * 40)
        lease.head_sha = "c" * 40
    elif case == "recorded":
        object.__setattr__(auth.repo_review_state, "git_expected_head", None)
    elif case == "observed":
        monkeypatch.setattr(ac, "_observed_checkout_state", lambda _: None)
    elif case == "branch":
        monkeypatch.setattr(ac, "_observed_checkout_state", lambda _: ("other", scope.observed_head_sha))
    elif case == "lineage":
        _ahead(attested, monkeypatch)
        monkeypatch.setattr(ac, "_lease_has_clean_lineage", lambda *args: ac._lease_refuse(
            ac.LeaseAttestationReason.LINEAGE_COMMIT_MISMATCH,
        ))
    elif case == "head_state":
        auth.repo_review_state._author_attestation_verdicts.clear()
        monkeypatch.setattr(ac, "_lease_head_is_author_attested", lambda *args, **kwargs: ac._lease_refuse(
            ac.LeaseAttestationReason.CHECKOUT_STATE_MISMATCH,
        ))
    elif case == "cached_false":
        auth.repo_review_state._author_attestation_verdicts.clear()
        monkeypatch.setattr(ac, "_lease_head_is_author_attested", lambda *args, **kwargs: ac._lease_refuse(
            ac.LeaseAttestationReason.COMMIT_IDENTITY_MISMATCH,
        ))
        assert not ac.pr_checkout_lease_attestation(auth, scope, lease).ok
    else:
        def git(_path, _args, **_kwargs):
            if case == "git_missing":
                raise FileNotFoundError("private path")
            if case == "git_error":
                raise subprocess.SubprocessError("private path")
            if case == "git_timeout":
                return GitProcessResult(-9, timed_out=True)
            if case == "git_limit":
                return GitProcessResult(-9, output_limited=True)
            if case == "git_exit":
                return GitProcessResult(1)
            return GitProcessResult(0, "not a branch status")

        monkeypatch.setattr(repo_tools, "hardened_git_command", git)
    result = ac.pr_checkout_lease_attestation(auth, scope, lease)
    assert result == ac.LeaseAttestation(False, reason)
    assert not ac._attested_pr_checkout_lease(auth, scope, lease)


@pytest.mark.parametrize("case,reason", [
    ("head", "lineage_head_mismatch"),
    ("metadata", "lineage_metadata_mismatch"),
    ("missing", "lineage_record_missing"),
    ("commit", "lineage_commit_mismatch"),
    ("read", "lineage_read_error"),
    ("missing_file", "lineage_read_error"),
    ("git_timeout", "git_timeout"),
    ("git_exit", "git_nonzero_exit"),
    ("git_missing", "git_missing_binary"),
    ("git_limit", "git_output_limit"),
    ("git_error", "git_subprocess_error"),
])
def test_lineage_refusal_branch(attested, monkeypatch, case, reason):
    _, scope, lease = attested
    head = "f" * 40
    metadata = lease.path / ".git" / "mimir-pr-checkout-lease.json"
    record = json.loads(metadata.read_text())
    record["head_sha"] = head
    record["lineage"] = {head: True}
    lease.head_sha = head
    if case == "head":
        lease.head_sha = "e" * 40
    elif case == "metadata":
        record["scope_id"] = "different"
    elif case == "missing":
        record["lineage"] = None
    elif case == "commit":
        record["lineage"] = {}
    elif case == "read":
        metadata.write_text("invalid json")
    elif case == "missing_file":
        metadata.unlink()
    if case not in {"read", "missing_file"}:
        metadata.write_text(json.dumps(record))

    def git(_path, _args, **_kwargs):
        if case == "git_missing":
            raise FileNotFoundError("private path")
        if case == "git_error":
            raise subprocess.SubprocessError("private path")
        if case == "git_timeout":
            return GitProcessResult(-9, timed_out=True)
        if case == "git_limit":
            return GitProcessResult(-9, output_limited=True)
        if case == "git_exit":
            return GitProcessResult(1)
        return GitProcessResult(0, head + "\n")

    monkeypatch.setattr(repo_tools, "hardened_git_command", git)
    assert not ac._lease_has_clean_lineage(lease.path, lease, scope, scope.observed_head_sha, head)
    assert ac._lease_refusal_reason.get().value == reason


@pytest.mark.parametrize("case,reason", [
    ("ancestry", "ancestry_failure"),
    ("identity", "commit_identity_mismatch"),
    ("merge_identity", "merge_identity_mismatch"),
    ("merge_parent", "merge_parent_mismatch"),
    ("patch_budget", "patch_budget_exceeded"),
    ("patch_id", "patch_id_mismatch"),
])
def test_advanced_head_refusal_branch(attested, monkeypatch, case, reason):
    auth, scope, lease = attested
    head = _ahead(attested, monkeypatch)
    base = scope.observed_base_sha
    if case in {"merge_identity", "merge_parent", "patch_budget", "patch_id"}:
        object.__setattr__(scope, "destination_ref", "refs/heads/main")
        # Keep the observed branch aligned after the scope mutation.
        monkeypatch.setattr(ac, "_observed_checkout_state", lambda _: (scope.head_ref, head))
        lease.base_sha = base

    def git(_path, args, **_kwargs):
        if args[0] == "merge-base" and "--is-ancestor" in args:
            if case == "ancestry" or case == "merge_parent" and args[-1] == "e" * 40:
                return GitProcessResult(1)
            return GitProcessResult(0)
        if args[0] == "merge-base":
            return GitProcessResult(0, base + "\n")
        if args[0] == "log":
            author = "human" if case in {"patch_budget", "patch_id"} else DEFAULT_USER_NAME
            committer = "foreign" if case == "identity" else DEFAULT_USER_NAME
            parents = f"{base} {'e' * 40}" if case in {"merge_identity", "merge_parent"} else base
            if case == "merge_identity":
                author = "human"
            return GitProcessResult(0, "\x00".join((head, parents, author, DEFAULT_USER_EMAIL, committer,
                                                     DEFAULT_USER_EMAIL, "")))
        if args[0] == "rev-list":
            return GitProcessResult(0, (scope.observed_head_sha + "\n") * (501 if case == "patch_budget" else 1))
        if args[0] == "show":
            return GitProcessResult(0, "replay" if args[5] == head else "original")
        pytest.fail(f"unexpected git command: {args[0]}")

    monkeypatch.setattr(repo_tools, "hardened_git_command", git)
    monkeypatch.setattr(ac.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=f"{('c' if kwargs.get('input') == 'replay' else 'a') * 40} {'b' * 40}\n",
    ))
    result = ac.pr_checkout_lease_attestation(auth, scope, lease)
    assert result == ac.LeaseAttestation(False, reason)


def test_cached_false_preserves_reason_without_git_reverification(attested, monkeypatch):
    auth, scope, lease = attested
    head = _ahead(attested, monkeypatch)
    object.__setattr__(scope, "destination_ref", "refs/heads/main")
    lease.base_sha = scope.observed_base_sha
    calls = []

    def git(_path, args, **_kwargs):
        calls.append(args)
        if args[0] == "merge-base":
            return GitProcessResult(0)
        if args[0] == "log":
            return GitProcessResult(0, "\x00".join((
                head, scope.observed_head_sha, DEFAULT_USER_NAME, DEFAULT_USER_EMAIL,
                "foreign", DEFAULT_USER_EMAIL, "",
            )))
        pytest.fail(f"unexpected git command: {args[0]}")

    monkeypatch.setattr(repo_tools, "hardened_git_command", git)
    expected = ac.LeaseAttestation(False, "commit_identity_mismatch")
    assert ac.pr_checkout_lease_attestation(auth, scope, lease) == expected
    assert [args[0] for args in calls] == ["merge-base", "merge-base", "log"]
    assert auth.repo_review_state._author_attestation_verdicts[
        (scope.scope_id, head)
    ] == (False, "commit_identity_mismatch")

    def forbidden(*args, **kwargs):
        pytest.fail("cached refusal must not launch a diagnostic git subprocess")

    # The live branch/HEAD and lineage checks remain in place, stubbed by _ahead;
    # only the expensive author verification is cached, exactly as before.
    monkeypatch.setattr(repo_tools, "hardened_git_command", forbidden)
    monkeypatch.setattr(ac.subprocess, "run", forbidden)
    assert ac.pr_checkout_lease_attestation(auth, scope, lease) == expected
    assert not ac._attested_pr_checkout_lease(auth, scope, lease)
    assert [args[0] for args in calls] == ["merge-base", "merge-base", "log"]


@pytest.mark.parametrize("flag", ["timed_out", "output_limited"])
def test_status_exit_zero_preserves_attestation_despite_diagnostic_flags(
    attested, monkeypatch, flag,
):
    auth, scope, lease = attested
    auth.repo_review_state._author_attestation_verdicts.clear()

    def git(_path, args, **_kwargs):
        assert args[0] == "status"
        return GitProcessResult(0, (
            f"# branch.oid {scope.observed_head_sha}\n# branch.head {scope.head_ref}\n"
        ), **{flag: True})

    monkeypatch.setattr(repo_tools, "hardened_git_command", git)
    assert ac.pr_checkout_lease_attestation(auth, scope, lease) == ac.LeaseAttestation(True, "ok")
    # The uncached/reconstructed path must keep the same exit-status predicate.
    object.__setattr__(auth, "repo_review_state", None)
    assert ac.pr_checkout_lease_attestation(auth, scope, lease) == ac.LeaseAttestation(True, "ok")


@pytest.mark.parametrize("flag", ["timed_out", "output_limited"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_patch_replay_merge_base_preserves_exit_status_predicate(
    attested, monkeypatch, flag, exit_code,
):
    auth, scope, lease = attested
    head = _ahead(attested, monkeypatch)
    base = scope.observed_base_sha
    object.__setattr__(scope, "destination_ref", "refs/heads/main")
    lease.base_sha = base
    commands = []

    def git(_path, args, **_kwargs):
        commands.append(args[0])
        if args[0] == "merge-base" and "--is-ancestor" in args:
            return GitProcessResult(0)
        if args[0] == "merge-base":
            return GitProcessResult(exit_code, base + "\n", **{flag: True})
        if args[0] == "log":
            return GitProcessResult(0, "\x00".join((
                head, base, "human", DEFAULT_USER_EMAIL,
                DEFAULT_USER_NAME, DEFAULT_USER_EMAIL, "",
            )))
        if args[0] == "rev-list":
            return GitProcessResult(0, scope.observed_head_sha + "\n")
        if args[0] == "show":
            return GitProcessResult(0, "identical patch")
        pytest.fail(f"unexpected git command: {args[0]}")

    monkeypatch.setattr(repo_tools, "hardened_git_command", git)
    patch_calls = []

    def patch(*args, **kwargs):
        patch_calls.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout=f"{'a' * 40} {'b' * 40}\n")

    monkeypatch.setattr(ac.subprocess, "run", patch)
    expected = ac.LeaseAttestation(True, "ok") if exit_code == 0 else ac.LeaseAttestation(
        False, "git_timeout" if flag == "timed_out" else "git_output_limit",
    )
    assert ac.pr_checkout_lease_attestation(auth, scope, lease) == expected
    assert ("rev-list" in commands) is (exit_code == 0)
    assert len(patch_calls) == (2 if exit_code == 0 else 0)


def test_reason_codes_are_closed_against_source_and_cases():
    # Source and test inventory are independent: merging enum values, adding an
    # untested reason or leaving a dead reason fails this test.
    declared = {reason.value for reason in ac.LeaseAttestationReason}
    names = {name.value for name in ac.LeaseAttestationReason.__members__.values()}
    assert len(declared) == len(ac.LeaseAttestationReason.__members__) == len(names)
    used = set()
    for fn in (ac._pr_checkout_lease_attestation, ac._lease_has_clean_lineage,
               ac._observed_checkout_state, ac._lease_head_is_author_attested,
               ac._lease_git_ok, ac._lease_ancestor_ok, ac._lease_exception_reason):
        for node in ast.walk(ast.parse(inspect.getsource(fn))):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "LeaseAttestationReason":
                used.add(node.attr)
    assert {"OK", *used} == set(ac.LeaseAttestationReason.__members__)
    # Explicit branch inventory: no reason can be silently collapsed into another.
    assert declared == {"ok", *{item[1] for fn in (test_lease_verdict_branch,
        test_lineage_refusal_branch, test_advanced_head_refusal_branch)
        for marker in fn.pytestmark for item in marker.args[1]}}


def test_refusal_event_is_content_free_and_deduped_per_turn(attested, monkeypatch):
    from mimir.tools.repo import _publish_attested_lease_result

    auth, scope, lease = attested
    auth.ifc_state.pr_checkout_author_trust.clear()
    events = []
    monkeypatch.setattr(event_logger, "log_event_sync", lambda name, **fields: events.append((name, fields)))
    runtime = SimpleNamespace(context=auth)
    for _ in range(2):
        _publish_attested_lease_result(runtime, auth.repo_review_state)
        ac.protected_result_source(auth, principal="filesystem", domain="filesystem",
                                   resource_id=str(lease.path), bridge_instance="filesystem")
    assert events == [("pr_checkout_lease_attestation_refused", {
        "scope_id": scope.scope_id, "reason": "no_author_trust", "cause": "no_turn_verdict",
    })]


@pytest.mark.parametrize("verdict,expected", [
    (False, "non_collaborator"), ("missing", "no_turn_verdict"),
    (None, "verdict_missing"),
])
def test_lease_missing_and_false_verdicts_are_not_author_resolution_failures(
    attested, monkeypatch, verdict, expected,
):
    auth, scope, lease = attested
    auth.ifc_state.pr_checkout_author_trust.clear()
    if verdict != "missing":
        auth.ifc_state.pr_checkout_author_trust[scope.scope_id] = verdict
    # A stale cause must not override the actual missing/False verdict.
    if verdict in (False, "missing"):
        auth.ifc_state.pr_checkout_author_trust_cause[scope.scope_id] = "author_unresolved"
    events = []
    monkeypatch.setattr(event_logger, "log_event_sync",
                        lambda name, **fields: events.append((name, fields)))
    result = ac.pr_checkout_lease_attestation(auth, scope, lease)
    assert not result.ok and result.reason == "no_author_trust"
    ac.log_pr_checkout_lease_attestation_refused(auth, scope, result.reason)
    assert events[0][1]["cause"] == expected


@pytest.mark.parametrize("cause,expected", [
    ("collaborator_timeout", "collaborator_timeout"),
    ("membership_budget", "membership_budget"),
    ("untrusted response body", "author_unresolved"),
])
def test_no_author_trust_lease_diagnostic_carries_bounded_cause(
    attested, monkeypatch, cause, expected,
):
    auth, scope, lease = attested
    auth.ifc_state.pr_checkout_author_trust[scope.scope_id] = None
    auth.ifc_state.pr_checkout_author_trust_cause[scope.scope_id] = cause
    events = []
    monkeypatch.setattr(event_logger, "log_event_sync",
                        lambda name, **fields: events.append((name, fields)))
    result = ac.pr_checkout_lease_attestation(auth, scope, lease)
    assert result.ok is False and result.reason == "no_author_trust"
    ac.log_pr_checkout_lease_attestation_refused(auth, scope, result.reason)
    assert events == [("pr_checkout_lease_attestation_refused", {
        "scope_id": scope.scope_id, "reason": "no_author_trust", "cause": expected,
    })]
    assert "owner/repo" not in str(events) and str(lease.path) not in str(events)
    with pytest.raises(AssertionError, match="no_author_trust"):
        assert_lease_attested(auth, scope, lease)


def test_reconstructed_lease_preserves_git_status_timeout(attested, monkeypatch):
    auth, scope, lease = attested
    object.__setattr__(auth, "repo_review_state", None)
    monkeypatch.setattr(repo_tools, "hardened_git_command", lambda *args, **kwargs: GitProcessResult(
        -9, timed_out=True,
    ))
    assert ac.pr_checkout_lease_attestation(auth, scope, lease) == ac.LeaseAttestation(
        False, "git_timeout",
    )
