"""Regression tests for per-PR event coalescing and terminal comments."""
from __future__ import annotations

import pytest

import poller


SINCE = "2026-06-28T15:45:00Z"
CREATED = "2026-06-28T15:55:30Z"


def _pr(
    number: int,
    sha: str,
    *,
    requested_reviewers: list[str] | None = None,
) -> dict:
    return {
        "number": number,
        "title": "Fix the thing",
        "html_url": f"https://github.com/o/r/pull/{number}",
        "user": {"login": "alice"},
        "head": {"sha": sha},
        "requested_reviewers": [
            {"login": login} for login in (requested_reviewers or [])
        ],
    }


def _comment(number: int, *, pull_request: bool = True) -> dict:
    kind = "pull" if pull_request else "issues"
    return {
        "created_at": CREATED,
        "user": {"login": "jason"},
        "body": "Updated the description.",
        "html_url": (
            f"https://github.com/o/r/{kind}/{number}#issuecomment-123"
        ),
        "issue_url": f"https://api.github.com/repos/o/r/issues/{number}",
    }


@pytest.fixture
def captured_emits(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    events: list[dict] = []

    def fake_emit(prompt: str, **extras: object) -> None:
        events.append({"prompt": prompt, **extras})

    monkeypatch.setattr(poller, "_emit", fake_emit)
    return events


def test_pr_comment_created_before_merge_is_dropped_when_polled_after_merge(
    monkeypatch: pytest.MonkeyPatch,
    captured_emits: list[dict],
) -> None:
    calls: list[str] = []

    def fake_api(endpoint: str, token: str):
        calls.append(endpoint)
        if "/issues/comments?" in endpoint:
            comment = _comment(1234)
            comment["html_url"] = (
                "https://github.com/o/r/issues/1234#issuecomment-123"
            )
            return [comment]
        if endpoint == "repos/o/r/issues/1234":
            return {
                "state": "closed",
                "pull_request": {"merged_at": "2026-06-28T15:59:22Z"},
            }
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", fake_api)

    count = poller._check_issue_comments("o/r", SINCE, "t", "mimir-carreira")

    assert count == 0
    assert captured_emits == []
    assert "repos/o/r/issues/1234" in calls


def test_open_pr_comment_still_emits_when_it_is_the_only_signal(
    monkeypatch: pytest.MonkeyPatch,
    captured_emits: list[dict],
) -> None:
    def fake_api(endpoint: str, token: str):
        if "/issues/comments?" in endpoint:
            return [_comment(42)]
        if endpoint == "repos/o/r/issues/42":
            return {"state": "open", "pull_request": {"url": "api/pr/42"}}
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", fake_api)

    count = poller._check_issue_comments("o/r", SINCE, "t", "mimir-carreira")

    assert count == 1
    assert [event["event_type"] for event in captured_emits] == ["issue_comment"]
    assert captured_emits[0]["number"] == "42"


def test_pr_parent_lookup_failure_fails_open(
    monkeypatch: pytest.MonkeyPatch,
    captured_emits: list[dict],
) -> None:
    def fake_api(endpoint: str, token: str):
        if "/issues/comments?" in endpoint:
            return [_comment(42)]
        if endpoint == "repos/o/r/issues/42":
            return None
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", fake_api)

    count = poller._check_issue_comments("o/r", SINCE, "t", "mimir-carreira")

    assert count == 1
    assert captured_emits[0]["event_type"] == "issue_comment"


def test_issue_comment_is_not_suppressed_even_when_issue_is_closed(
    monkeypatch: pytest.MonkeyPatch,
    captured_emits: list[dict],
) -> None:
    def fake_api(endpoint: str, token: str):
        if "/issues/comments?" in endpoint:
            return [_comment(42, pull_request=False)]
        if endpoint == "repos/o/r/issues/42":
            return {"state": "closed"}
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", fake_api)

    count = poller._check_issue_comments(
        "o/r",
        SINCE,
        "t",
        "mimir-carreira",
        review_needed_pr_numbers={"42"},
    )

    assert count == 1
    assert captured_emits[0]["event_type"] == "issue_comment"


def test_same_poll_comment_and_synchronize_emit_one_review_turn(
    monkeypatch: pytest.MonkeyPatch,
    captured_emits: list[dict],
) -> None:
    def fake_api(endpoint: str, token: str):
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return [_pr(42, "new-sha")]
        if "compare/old-sha...new-sha" in endpoint:
            return None
        if "/issues/comments?" in endpoint:
            return [_comment(42)]
        if endpoint == "repos/o/r/issues/42":
            raise AssertionError("coalesced comment should not need a parent lookup")
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", fake_api)
    review_needed: set[str] = set()

    push_count, _, _ = poller._check_pr_pushes(
        "o/r",
        token="t",
        me="mimir-carreira",
        pr_heads={"42": "old-sha"},
        review_needed_pr_numbers=review_needed,
    )
    comment_count = poller._check_issue_comments(
        "o/r",
        SINCE,
        "t",
        "mimir-carreira",
        review_needed_pr_numbers=review_needed,
    )

    assert push_count == 1
    assert comment_count == 0
    assert [event["event_type"] for event in captured_emits] == ["pr_synchronize"]
    assert review_needed == {"42"}


def test_synchronize_coalesces_review_request_and_preserves_retry_state(
    monkeypatch: pytest.MonkeyPatch,
    captured_emits: list[dict],
) -> None:
    def first_poll_api(endpoint: str, token: str):
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return [_pr(42, "new-sha", requested_reviewers=["mimir-carreira"])]
        if "compare/old-sha...new-sha" in endpoint:
            return None
        if endpoint == "repos/o/r/pulls/42/reviews":
            return []
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", first_poll_api)
    review_needed: set[str] = set()

    count, new_heads, new_rr = poller._check_pr_pushes(
        "o/r",
        token="t",
        me="mimir-carreira",
        pr_heads={"42": "old-sha"},
        pr_review_requests={},
        review_needed_pr_numbers=review_needed,
    )

    assert count == 1
    assert [event["event_type"] for event in captured_emits] == ["pr_synchronize"]
    assert new_heads == {"42": "new-sha"}
    assert new_rr == {"42": 1}

    captured_emits.clear()

    def second_poll_api(endpoint: str, token: str):
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return [_pr(42, "new-sha", requested_reviewers=["mimir-carreira"])]
        if endpoint == "repos/o/r/pulls/42/reviews":
            return []
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", second_poll_api)
    count, _, new_rr = poller._check_pr_pushes(
        "o/r",
        token="t",
        me="mimir-carreira",
        pr_heads=new_heads,
        pr_review_requests=new_rr,
        review_needed_pr_numbers=set(),
    )

    assert count == 1
    assert [event["event_type"] for event in captured_emits] == [
        "pr_review_requested"
    ]
    assert captured_emits[0]["attempt"] == 2
    assert new_rr == {"42": 2}


@pytest.mark.parametrize(
    ("prior_attempts", "expected_attempts"),
    [
        (0, 1),
        (1, 2),
        (
            poller.REVIEW_REQUEST_MAX_ATTEMPTS - 1,
            poller.REVIEW_REQUEST_MAX_ATTEMPTS,
        ),
        (
            poller.REVIEW_REQUEST_MAX_ATTEMPTS,
            poller.REVIEW_REQUEST_MAX_ATTEMPTS,
        ),
        (
            poller.REVIEW_REQUEST_MAX_ATTEMPTS + 1,
            poller.REVIEW_REQUEST_MAX_ATTEMPTS + 1,
        ),
    ],
)
def test_synchronize_consumes_one_review_request_attempt_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    captured_emits: list[dict],
    prior_attempts: int,
    expected_attempts: int,
) -> None:
    def fake_api(endpoint: str, token: str):
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return [_pr(42, "new-sha", requested_reviewers=["mimir-carreira"])]
        if "compare/old-sha...new-sha" in endpoint:
            return None
        if endpoint == "repos/o/r/pulls/42/reviews":
            return []
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", fake_api)

    count, _, new_rr = poller._check_pr_pushes(
        "o/r",
        token="t",
        me="mimir-carreira",
        pr_heads={"42": "old-sha"},
        pr_review_requests={"42": prior_attempts} if prior_attempts else {},
        review_needed_pr_numbers=set(),
    )

    assert count == 1
    assert [event["event_type"] for event in captured_emits] == ["pr_synchronize"]
    assert new_rr == {"42": expected_attempts}


def test_main_threads_one_per_repo_coalescing_set_and_persists_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    emitted: list[dict] = []
    saved: list[dict] = []

    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "_resolve_token", lambda: "t")
    monkeypatch.setattr(
        poller,
        "_load_cursor",
        lambda: {
            "last_checked": SINCE,
            "pr_heads": {"o/r": {"42": "old-sha"}},
            "pr_review_requests": {"o/r": {}},
        },
    )
    monkeypatch.setattr(poller, "_save_cursor", lambda cursor: saved.append(cursor))
    monkeypatch.setattr(
        poller,
        "_check_issues",
        lambda *args, **kwargs: 0,
    )
    monkeypatch.setattr(
        poller,
        "_check_pr_review_comments",
        lambda *args, **kwargs: 0,
    )
    monkeypatch.setattr(poller, "_check_pr_reviews", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        poller,
        "_check_own_changes_requested",
        lambda *args, **kwargs: (0, {}),
    )
    monkeypatch.setattr(poller, "_emit", lambda prompt, **extras: emitted.append(extras))

    def fake_api(endpoint: str, token: str):
        if endpoint == "repos/o/r/pulls?state=open&sort=created&direction=desc":
            return []
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return [_pr(42, "new-sha", requested_reviewers=["mimir-carreira"])]
        if "compare/old-sha...new-sha" in endpoint:
            return None
        if endpoint == "repos/o/r/pulls/42/reviews":
            return []
        if "/issues/comments?" in endpoint:
            return [_comment(42)]
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", fake_api)
    monkeypatch.setenv("GITHUB_REPOS", "o/r")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "mimir-carreira")

    poller.main()

    assert [event["event_type"] for event in emitted] == ["pr_synchronize"]
    assert saved[0]["pr_review_requests"] == {"o/r": {"42": 1}}
