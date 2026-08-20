"""The PR snapshot contract between this poller and mimir's authorization layer.

Every pass that can start a PR work turn must emit the head/base snapshot
``mimir/access_control.py`` needs to issue a ``RepoPRActionScope``. Without it
the framework refuses every PR tool for that turn, and the refusal the agent
sees names the live-discovery operator gate rather than the missing snapshot --
so the agent concludes it lacks authority and escalates to a human instead of
reviewing the PR it was just woken for.

These tests assert the contract end to end: they feed what the poller actually
emits to the real validator. Asserting field presence alone would not catch a
value the validator rejects.
"""
from __future__ import annotations

import pytest

import poller
from mimir import access_control
from mimir.access_control import _repo_pr_scope
from mimir.models import RepoPRAction

REPO = "owner/repo"
SELF = "mimir-carreira"


@pytest.fixture(autouse=True)
def _scope_preconditions(monkeypatch: pytest.MonkeyPatch):
    """Satisfy the validator's environment so only the snapshot is under test."""
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", SELF)
    monkeypatch.setattr(
        access_control, "_canonical_repo_binding_resolution",
        lambda _repo: access_control.RepoBindingResolution(
            ("/server/configured/repo", f"git@github.com:{REPO}.git"),
            ("/server/configured/repo",), 1,
        ),
    )


def _full_pr(number: int, *, author: str = "alice", head_repo: str = REPO) -> dict:
    """A PR shaped like the API returns it, with head and base populated."""
    return {
        "number": number,
        "title": "Some PR",
        "created_at": "2026-06-01T00:00:00Z",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "user": {"login": author},
        "body": "",
        "requested_reviewers": [],
        "head": {
            "sha": "a" * 40,
            "ref": "feature/branch",
            "repo": {"full_name": head_repo},
        },
        "base": {"sha": "b" * 40, "ref": "main"},
    }


def _scope_from(event: dict):
    """Issue a scope from an emitted event exactly as the framework does."""
    return _repo_pr_scope(
        provenance=access_control.RepoPRScopeProvenance.POLLER_PAYLOAD,
        repo=event.get("repo"),
        principal=event.get("author"),
        event_type=event.get("event_type"),
        review_state=event.get("state"),
        number=event.get("number"),
        head_repo=event.get("head_repo"),
        head_remote=event.get("head_remote"),
        head_ref=event.get("head_ref"),
        head_sha=event.get("head_sha"),
        base_ref=event.get("base_ref"),
        base_sha=event.get("base_sha"),
    )


@pytest.fixture
def captured(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(
        poller, "_emit",
        lambda prompt, **extras: events.append({"prompt": prompt, **extras}),
    )
    return events


def test_pr_opened_emits_a_scopeable_snapshot(monkeypatch, captured):
    monkeypatch.setattr(poller, "_gh_api", lambda *a, **k: [_full_pr(7)])
    monkeypatch.setattr(poller, "_emit_pr_review_needed", _passthrough_emit)

    assert _check_prs_count(monkeypatch) == 1
    event = _only_of(captured, "pr_opened")
    scope = _scope_from(event)

    assert scope is not None
    assert scope.pr_number == 7
    assert RepoPRAction.PR_REVIEW.value in scope.allowed_operations


def _check_prs_count(monkeypatch) -> int:
    return poller._check_prs(REPO, "2026-01-01T00:00:00Z", "token", SELF)


def _only_of(events: list[dict], event_type: str) -> dict:
    matching = [e for e in events if e.get("event_type") == event_type]
    assert len(matching) == 1, f"expected one {event_type}, got {len(matching)}"
    return matching[0]


def test_pr_synchronize_emits_a_scopeable_snapshot(monkeypatch, captured):
    monkeypatch.setattr(poller, "_gh_api", lambda *a, **k: None)
    monkeypatch.setattr(poller, "_emit_pr_review_needed", _passthrough_emit)

    emitted = poller._emit_pr_synchronize(
        REPO, 9, "Some PR", f"https://github.com/{REPO}/pull/9",
        "c" * 40, "a" * 40, "token", SELF, pr=_full_pr(9),
    )

    assert emitted is True
    scope = _scope_from(_only_of(captured, "pr_synchronize"))
    assert scope is not None
    assert scope.pr_number == 9
    assert RepoPRAction.PR_REVIEW.value in scope.allowed_operations


def _passthrough_emit(prompt, *, token, reviewer, **extras):
    """Bypass the already-reviewed choke point; the snapshot is what matters."""
    poller._emit(prompt, **extras)
    return True


def test_pr_review_on_own_pr_grants_remediation_authority(monkeypatch, captured):
    """A CHANGES_REQUESTED review on this agent's own PR must let it push a fix.

    Remediation authority is gated on the event's principal being this agent,
    which the framework reads from ``author``. Naming the reviewer there left
    the agent able to read the review but not to act on it.
    """
    pr = _full_pr(11, author=SELF)
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: (
        [{"user": {"login": "jasoncarreira"}, "state": "CHANGES_REQUESTED",
          "submitted_at": "2026-06-01T00:00:00Z", "body": "please fix",
          "html_url": f"https://github.com/{REPO}/pull/11#r1"}]
        if "/reviews" in endpoint else [pr]
    ))

    assert poller._check_pr_reviews(REPO, "2026-01-01T00:00:00Z", "token", SELF) == 1
    event = _only_of(captured, "pr_review")

    assert event["author"] == SELF, "author must be the PR author, not the reviewer"
    assert event["reviewer"] == "jasoncarreira"
    assert "@jasoncarreira requested changes on" in event["prompt"]

    scope = _scope_from(event)
    assert scope is not None
    for action in (RepoPRAction.WRITE, RepoPRAction.COMMIT, RepoPRAction.PUSH):
        assert action.value in scope.allowed_operations


def test_pr_review_from_a_fork_reports_the_source_remote(monkeypatch, captured):
    pr = _full_pr(13, head_repo="contributor/repo")
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: (
        [{"user": {"login": "alice"}, "state": "COMMENTED",
          "submitted_at": "2026-06-01T00:00:00Z", "body": "",
          "html_url": f"https://github.com/{REPO}/pull/13#r1"}]
        if "/reviews" in endpoint else [pr]
    ))

    assert poller._check_pr_reviews(REPO, "2026-01-01T00:00:00Z", "token", SELF) == 1
    event = _only_of(captured, "pr_review")

    assert event["head_remote"] == "source"
    assert _scope_from(event) is not None
