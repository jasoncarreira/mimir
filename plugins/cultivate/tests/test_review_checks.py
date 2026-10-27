"""Approval requires complete, passing CI evidence at the exact reviewed head."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from mimir.tools.refusals import ToolPolicyRefusal
from mimir_cultivate import forge, review_guard
from mimir_cultivate.review_checks import approval_blocker
from test_forge import _runtime


_MALFORMED_CI_CASES = [
    ("null_checks", "CI checks could not be read"),
    ("list_checks", "CI checks could not be read"),
    ("null_statuses", "CI checks could not be read"),
    ("list_statuses", "CI checks could not be read"),
    ("check_missing_status", "CI check response is malformed"),
    ("check_non_string_status", "CI check response is malformed"),
    ("check_missing_name", "CI check response is malformed"),
    ("check_empty_name", "CI check response is malformed"),
    ("status_missing_state", "CI status response is malformed"),
    ("status_non_string_state", "CI status response is malformed"),
    ("status_empty_context", "CI status response is malformed"),
]


def _ci_evidence(scenario):
    check = {"name": "tests", "status": "completed", "conclusion": "success"}
    if scenario == "pending":
        check.update(status="in_progress", conclusion=None)
    if scenario == "failing":
        check["conclusion"] = "failure"
    checks = [] if scenario == "missing" else [check]
    check_data = {"check_runs": checks, "total_count": len(checks)}
    if scenario == "incomplete":
        check_data["total_count"] += 1
    if scenario == "malformed":
        del check_data["total_count"]
    status_data = {"statuses": ([{"context": "legacy", "state": "pending"}]
                                if scenario == "status_pending" else [])}
    if scenario == "null_checks":
        check_data = None
    elif scenario == "list_checks":
        check_data = []
    elif scenario == "null_statuses":
        status_data = None
    elif scenario == "list_statuses":
        status_data = []
    elif scenario == "check_missing_status":
        del check["status"]
    elif scenario == "check_non_string_status":
        check["status"] = 1
    elif scenario == "check_missing_name":
        del check["name"]
    elif scenario == "check_empty_name":
        check["name"] = ""
    elif scenario in {"status_missing_state", "status_non_string_state", "status_empty_context"}:
        status = {"context": "legacy", "state": "success"}
        if scenario == "status_missing_state":
            del status["state"]
        elif scenario == "status_non_string_state":
            status["state"] = 1
        elif scenario == "status_empty_context":
            status["context"] = ""
        status_data["statuses"] = [status]
    return check_data, status_data


@pytest.mark.parametrize("scenario,reason", _MALFORMED_CI_CASES)
def test_approval_blocker_refuses_malformed_ci(scenario, reason):
    # All other evidence is green, so skipping a malformed row would approve.
    assert approval_blocker(*_ci_evidence(scenario)) == reason


def test_approval_blocker_accepts_green_ci():
    assert approval_blocker(*_ci_evidence("green")) is None


@pytest.mark.parametrize("scenario,reason", [
    ("green", None), ("pending", "tests (in_progress)"),
    ("failing", "tests (failure)"), ("missing", "no CI checks"),
    ("read_error", "could not be read"), ("status_pending", "legacy (pending)"),
    ("incomplete", "could not be read completely"),
    ("malformed", "could not be read completely"),
    ("status_error", "could not be read"),
    *_MALFORMED_CI_CASES,
])
def test_both_approval_paths(monkeypatch, scenario, reason):
    head = "a" * 40
    check_data, status_data = _ci_evidence(scenario)
    calls = []

    def api(_spec, argv):
        calls.append(argv)
        if argv[-1] == ".head.sha":
            data = head
        elif argv[:2] == ["api", "user"]:
            data = "reviewer"
        elif argv[-1] == "--paginate":
            data = "[]"
        elif "/check-runs?" in argv[1]:
            data = json.dumps(check_data)
        elif argv[1].endswith("/status"):
            data = json.dumps(status_data)
        else:
            raise AssertionError(argv)
        failed = ((scenario == "read_error" and "/check-runs?" in argv[1])
                  or (scenario == "status_error" and argv[1].endswith("/status")))
        return SimpleNamespace(returncode=1 if failed else 0, stdout=data)

    monkeypatch.setattr(review_guard, "_run", api)
    for state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
        spec = review_guard.ReviewSubmission("gh", "owner/repo", 17, state, None)
        if state == "APPROVED" and reason:
            claim = None
            try:
                with pytest.raises(ToolPolicyRefusal, match=re.escape(reason)):
                    claim = review_guard.claim_review_submission(spec)
            finally:
                # A fail-open mutation must not strand the submission lock.
                if claim is not None:
                    claim.release()
        else:
            claim = review_guard.claim_review_submission(spec)
            assert claim and claim.head == head
            claim.release()
    assert any(f"/commits/{head}/check-runs?" in " ".join(call) for call in calls)

    client = forge.GitHubForgeClient(token="test")
    monkeypatch.setattr(client, "_target", lambda scope: ("owner/repo", 17))
    monkeypatch.setattr(client, "_confirm_effect_identity", lambda scope: None)
    posts = []

    def request(method, path, **kwargs):
        if method == "POST":
            posts.append(kwargs["body"])
            return {"id": 1, "user": {"login": "reviewer"}, "state": "APPROVED"}
        if path.endswith("/pulls/17"):
            return {"head": {"sha": head}}
        if (scenario == "read_error" and "/check-runs?" in path
                or scenario == "status_error" and path.endswith("/status")):
            raise forge.ForgeError("transport failure")
        return check_data if "/check-runs?" in path else status_data

    monkeypatch.setattr(client, "_request", request)
    scope = _runtime().context.repo_pr_action_scope
    for verdict in forge.ReviewVerdict:
        if verdict == forge.ReviewVerdict.APPROVE and reason:
            with pytest.raises(forge.ForgeError, match=re.escape(reason)):
                client.submit_review(scope, verdict, "review")
        else:
            client.submit_review(scope, verdict, "review")
    assert [post["event"] for post in posts] == (
        ["APPROVE", "COMMENT", "REQUEST_CHANGES"] if not reason
        else ["COMMENT", "REQUEST_CHANGES"]
    )
    assert all(post["commit_id"] == head for post in posts)


def test_forge_refuses_approval_after_head_moves(monkeypatch):
    client = forge.GitHubForgeClient(token="test")
    monkeypatch.setattr(client, "_confirm_effect_identity", lambda scope: None)
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        return {"head": {"sha": "b" * 40}}

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(forge.ForgeError, match="head is no longer current"):
        client.submit_review(_runtime().context.repo_pr_action_scope,
                             forge.ReviewVerdict.APPROVE, "review")
    assert calls == [("GET", "/repos/owner/repo/pulls/17")]
