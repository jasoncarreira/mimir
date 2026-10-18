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

from github_poller_test_support import poller
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
        "state": "open",
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
        principal=event.get("pr_author") if event.get("event_type") == "pr_synchronize"
        else event.get("author"),
        event_type=event.get("event_type"),
        review_state=event.get("state"),
        pr_author_is_trusted=event.get("pr_author_is_trusted"),
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
    assert event["pr_author_is_trusted"] is True
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
    event = _only_of(captured, "pr_synchronize")
    assert event["pr_author_is_trusted"] is True
    scope = _scope_from(event)
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
    assert event["actor"] == "jasoncarreira"
    assert event["pr_state"] == "open"
    assert event["reviewer"] == "jasoncarreira"
    assert "@jasoncarreira requested changes on" in event["prompt"]

    scope = _scope_from(event)
    assert scope is not None
    for action in (RepoPRAction.WRITE, RepoPRAction.COMMIT, RepoPRAction.PUSH):
        assert action.value in scope.allowed_operations


@pytest.mark.parametrize("event_type", ["issue_comment", "pr_review_comment"])
def test_pr_comment_emits_live_scope_snapshot(monkeypatch, captured, event_type):
    pr = _full_pr(12, author=SELF)
    monkeypatch.setattr(
        poller,
        "_gh_api",
        lambda endpoint, token: [] if endpoint.endswith("/reviews") else pr,
    )

    assert poller._emit_pr_review_needed(
        "please fix",
        token="token",
        reviewer=SELF,
        activity_at="2026-06-02T00:00:00Z",
        event_type=event_type,
        repo=REPO,
        number="12",
        url=(
            f"https://github.com/{REPO}/pull/12#issuecomment-1"
            if event_type == "issue_comment"
            else f"https://github.com/{REPO}/pull/12#discussion_r1"
        ),
        author="jasoncarreira",
    ) is True

    event = _only_of(captured, event_type)
    assert event["actor"] == "jasoncarreira"
    assert event["author"] == SELF
    assert event["number"] == 12
    assert event["pr_state"] == "open"
    scope = _scope_from(event)
    assert scope is not None
    assert scope.observed_head_sha == "a" * 40


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


def _framework_scope_from(event: dict, monkeypatch):
    """Consume the emitted payload through the registered-service ingress."""
    from mimir.models import AgentEvent

    monkeypatch.setenv("GITHUB_REPOS", REPO)
    authority = access_control.build_trigger_service_principal(
        canonical="poller:github-activity", trigger="poller", profile="github",
        tier=access_control.CapabilityTier.CODE_EXECUTION,
        capabilities=(), creation_path="test",
    )
    return access_control.create_auth_context(AgentEvent(
        trigger="poller", channel_id="poller:github-activity",
        service_principal=authority.canonical, service_authority=authority,
        extra={"poller_name": "github-activity", "items": [event]},
    ), enforce=True).repo_pr_action_scope


def _drive_trust_event(monkeypatch, pr, path):
    review = {
        "user": {"login": "reviewer"}, "state": "COMMENTED",
        "submitted_at": "2026-06-02T00:00:00Z", "body": "review",
        "html_url": f"{pr['html_url']}#r1",
    }

    def api(endpoint, token):
        if endpoint.endswith("/reviews"):
            return [review]
        if "/compare/" in endpoint:
            return {"commits": [], "ahead_by": 0}
        if endpoint == f"repos/{REPO}/pulls/{pr['number']}":
            return pr
        return [pr]

    monkeypatch.setattr(poller, "_gh_api", api)
    if path == "opened":
        return poller._check_prs(REPO, "2026-01-01T00:00:00Z", "token", SELF)
    if path == "synchronize":
        return poller._emit_pr_synchronize(
            REPO, pr["number"], pr["title"], pr["html_url"],
            "c" * 40, "a" * 40, "token", SELF, pr=pr,
        )
    if path == "review":
        return poller._check_pr_reviews(
            REPO, "2026-01-01T00:00:00Z", "token", SELF,
        )
    return poller._emit_pr_review_needed(
        "trusted comment", token="token", reviewer=SELF,
        activity_at="2026-06-02T00:00:00Z", event_type="issue_comment",
        repo=REPO, number=pr["number"], url=f"{pr['html_url']}#issuecomment-1",
        author="reviewer",
    )


@pytest.mark.parametrize("path", ["opened", "synchronize", "review", "comment"])
@pytest.mark.parametrize("author,verdict", [
    ("outsider", False), ("outsider", None), ("collaborator", True), (SELF, True),
])
def test_scope_verdict_survives_each_poller_emitter(
    monkeypatch, captured, path, author, verdict,
):
    """The real scope builder, not the permissive legacy fixture, must decide.

    Open/review selection already withholds outsiders before emission. Bypass
    only that earlier admission here to independently test the last-line scope
    verdict (including an admission-to-emission trust change). The scope builder
    and all emitters remain real. The next test covers the admission layer.
    """
    pr = _full_pr(21, author=author)
    calls = []

    def author_trust(repo, number, url, token, cache, **kwargs):
        calls.append((repo, number, token))
        assert number == pr["number"]
        return {"outsider": verdict, "collaborator": True}[author]

    monkeypatch.setattr(poller, "_pr_author_is_trusted", author_trust)
    monkeypatch.setattr(
        poller, "_github_author_is_trusted",
        lambda repo, login, token: login in {"collaborator", "reviewer", SELF},
    )
    if path == "opened":
        if author == "outsider":
            monkeypatch.setattr(
                poller, "_partition_activity", lambda *args, **kwargs: (args[2], []),
            )
        if author == SELF:
            # _check_prs intentionally filters own new PRs; test the shared
            # scope builder directly, then its normal downstream emitter.
            assert poller._emit_pr_review_needed(
                "own PR", token="token", reviewer=SELF,
                current_head_reviewed=False, event_type="pr_opened",
                repo=REPO, number=21, author=SELF,
                **poller._pr_scope_fields(pr, REPO, token="token", me=SELF),
            )
        else:
            assert _drive_trust_event(monkeypatch, pr, path)
    else:
        if path == "review" and author == "outsider":
            monkeypatch.setattr(poller, "_trusted_pr", lambda *args: True)
        assert _drive_trust_event(monkeypatch, pr, path)

    event_type = {
        "opened": "pr_opened", "synchronize": "pr_synchronize",
        "review": "pr_review", "comment": "issue_comment",
    }[path]
    event = _only_of(captured, event_type)
    assert event["pr_author_is_trusted"] is verdict
    expected_lookups = 2 if author == "collaborator" and path in {"opened", "review"} else 1
    assert calls == ([] if author == SELF else [(REPO, 21, "token")] * expected_lookups)
    scope = _framework_scope_from(event, monkeypatch)
    if verdict is True:
        assert scope is not None
        assert scope.pull_request_author == author
        assert scope.observed_head_sha == "a" * 40
    else:
        assert scope is None


@pytest.mark.parametrize("path", ["opened", "review"])
@pytest.mark.parametrize("verdict", [False, None])
def test_outsider_admission_withholds_before_scope_emission(
    monkeypatch, captured, path, verdict,
):
    """Do not weaken the real earlier gate to make the transport tests pass."""
    pr = _full_pr(22, author="outsider")
    calls = []

    def author_trust(repo, number, url, token, cache, **kwargs):
        calls.append(number)
        return verdict

    monkeypatch.setattr(poller, "_pr_author_is_trusted", author_trust)
    monkeypatch.setattr(poller, "_emit_signal", lambda *args, **kwargs: None)
    _drive_trust_event(monkeypatch, pr, path)
    assert calls == [22]
    assert captured == []


def test_check_prs_skips_self_without_attestation(monkeypatch, captured):
    def unexpected(*args, **kwargs):
        pytest.fail("self-authored PR must not require a trust lookup")

    monkeypatch.setattr(poller, "_pr_author_is_trusted", unexpected)
    assert _drive_trust_event(monkeypatch, _full_pr(23, author=SELF), "opened") == 0
    assert captured == []
