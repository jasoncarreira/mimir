"""Tests for ``_check_own_changes_requested`` — the chainlink #449
state-based reconciliation for the agent's own PRs stuck at
CHANGES_REQUESTED.

Mirrors the conventions of ``test_github_poller_pr_pushes.py``: mocks
``_gh_api`` per-endpoint and captures ``_emit`` via fixture. Asserts
both emit counts and the returned dedupe cursor, since the caller
relies on the rebuild-on-every-poll cleanup contract.
"""
from __future__ import annotations

import pytest

import poller


def _pr(number: int, sha: str, login: str = "mimir-bot",
        title: str = "My PR") -> dict:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "user": {"login": login},
        "head": {"sha": sha},
    }


def _review(
    login: str,
    state: str,
    submitted: str,
    commit_id: str = "review-sha",
) -> dict:
    return {
        "user": {"login": login},
        "state": state,
        "submitted_at": submitted,
        "commit_id": commit_id,
    }


@pytest.fixture
def captured_emits(monkeypatch):
    events: list[dict] = []

    def fake_emit(prompt, **extras):
        events.append({"prompt": prompt, **extras})

    monkeypatch.setattr(poller, "_emit", fake_emit)
    return events


def _patch_api(
    monkeypatch,
    *,
    prs,
    reviews_by_pr=None,
    commit_dates=None,
    compares=None,
):
    """Route ``_gh_api`` calls: PR list, reviews, commits, compare."""
    reviews_by_pr = reviews_by_pr or {}
    commit_dates = commit_dates or {}
    compares = compares or {}

    def fake_api(endpoint: str, token: str):
        if "/reviews" in endpoint:
            number = int(endpoint.split("/pulls/")[1].split("/")[0])
            return reviews_by_pr.get(number, [])
        if "/commits/" in endpoint:
            sha = endpoint.rsplit("/", 1)[1]
            date = commit_dates.get(sha)
            if date is None:
                return None  # API failure shape
            return {"commit": {"committer": {"date": date}}}
        if "/compare/" in endpoint:
            spec = endpoint.rsplit("/compare/", 1)[1]
            return compares.get(spec)
        return prs

    monkeypatch.setattr(poller, "_gh_api", fake_api)


def test_stale_changes_requested_emits_once_per_head(
    monkeypatch, captured_emits,
):
    """Own PR, latest review CHANGES_REQUESTED, head predates the review
    → exactly one reminder, deduped on subsequent polls via the cursor."""
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [
            _review("jasoncarreira", "CHANGES_REQUESTED", "2026-06-11T12:00:00Z"),
        ]},
        commit_dates={"aaa111": "2026-06-11T05:00:00Z"},  # head predates review
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {},
    )
    assert count == 1
    assert cursor == {"638": "aaa111"}
    [ev] = captured_emits
    assert ev["event_type"] == "pr_changes_requested_stale"
    assert ev["reviewers"] == ["jasoncarreira"]
    assert "stuck at CHANGES_REQUESTED" in ev["prompt"]

    # Second poll with the same state: dedupe — no new emit, entry kept.
    count2, cursor2 = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
    )
    assert count2 == 0
    assert cursor2 == {"638": "aaa111"}
    assert len(captured_emits) == 1


def test_commits_after_review_are_not_stale(monkeypatch, captured_emits):
    """Fixes pushed after the blocking review (decision still
    CHANGES_REQUESTED until re-review) must NOT nag — and nothing is
    recorded, so a NEWER blocking review re-arms the reminder."""
    _patch_api(
        monkeypatch,
        prs=[_pr(639, "bbb222")],
        reviews_by_pr={639: [
            _review("jasoncarreira", "CHANGES_REQUESTED", "2026-06-11T12:00:00Z"),
        ]},
        commit_dates={"bbb222": "2026-06-11T13:00:00Z"},  # head AFTER review
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {},
    )
    assert count == 0
    assert cursor == {}
    assert captured_emits == []


def test_content_free_rebase_after_review_is_still_stale(
    monkeypatch, captured_emits,
):
    """A content-free rebase can make committer-date newer than the
    blocking review without addressing feedback.  Empty compare files
    keep the stale reminder armed."""
    _patch_api(
        monkeypatch,
        prs=[_pr(642, "rebased-head")],
        reviews_by_pr={642: [
            _review(
                "jasoncarreira",
                "CHANGES_REQUESTED",
                "2026-06-11T12:00:00Z",
                commit_id="reviewed-head",
            ),
        ]},
        commit_dates={"rebased-head": "2026-06-11T13:00:00Z"},
        compares={"reviewed-head...rebased-head": {"files": [], "ahead_by": 1}},
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {},
    )
    assert count == 1
    assert cursor == {"642": "rebased-head"}
    [ev] = captured_emits
    assert ev["event_type"] == "pr_changes_requested_stale"


def test_real_fix_commit_after_review_suppresses_stale_reminder(
    monkeypatch, captured_emits,
):
    """A newer head with a non-empty diff from the reviewed commit is
    treated as fixes-pushed/awaiting re-review, so no stale reminder."""
    _patch_api(
        monkeypatch,
        prs=[_pr(643, "fixed-head")],
        reviews_by_pr={643: [
            _review(
                "jasoncarreira",
                "CHANGES_REQUESTED",
                "2026-06-11T12:00:00Z",
                commit_id="reviewed-head",
            ),
        ]},
        commit_dates={"fixed-head": "2026-06-11T13:00:00Z"},
        compares={
            "reviewed-head...fixed-head": {
                "files": [{"filename": "mimir/poller.py"}],
                "ahead_by": 1,
            },
        },
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {},
    )
    assert count == 0
    assert cursor == {}
    assert captured_emits == []

def test_later_approval_clears_blocking_state(monkeypatch, captured_emits):
    """A reviewer's later APPROVED supersedes their earlier
    CHANGES_REQUESTED; the PR is not blocked and the cursor entry drops
    (rebuild cleanup)."""
    _patch_api(
        monkeypatch,
        prs=[_pr(640, "ccc333")],
        reviews_by_pr={640: [
            _review("jasoncarreira", "CHANGES_REQUESTED", "2026-06-11T10:00:00Z"),
            _review("jasoncarreira", "APPROVED", "2026-06-11T12:00:00Z"),
        ]},
        commit_dates={"ccc333": "2026-06-11T05:00:00Z"},
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {"640": "old-sha"},
    )
    assert count == 0
    assert cursor == {}  # no longer blocked → entry dropped
    assert captured_emits == []


def test_new_head_with_new_blocking_review_reminds_again(
    monkeypatch, captured_emits,
):
    """A new head sha + a blocking review newer than it = a NEW stale
    state → one more reminder despite a prior entry for the old sha."""
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "ddd444")],
        reviews_by_pr={638: [
            _review("jasoncarreira", "CHANGES_REQUESTED", "2026-06-11T15:00:00Z"),
        ]},
        commit_dates={"ddd444": "2026-06-11T14:00:00Z"},
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {"638": "aaa111"},
    )
    assert count == 1
    assert cursor == {"638": "ddd444"}


def test_unknown_head_date_counts_as_stale_once(monkeypatch, captured_emits):
    """Commit-date lookup failure → conservative: remind (the per-sha
    dedupe caps the cost at one event)."""
    _patch_api(
        monkeypatch,
        prs=[_pr(641, "eee555")],
        reviews_by_pr={641: [
            _review("jasoncarreira", "CHANGES_REQUESTED", "2026-06-11T12:00:00Z"),
        ]},
        commit_dates={},  # /commits/<sha> returns None
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {},
    )
    assert count == 1
    assert cursor == {"641": "eee555"}


def test_other_authors_and_no_me_are_skipped(monkeypatch, captured_emits):
    _patch_api(
        monkeypatch,
        prs=[_pr(642, "fff666", login="alice")],
        reviews_by_pr={642: [
            _review("jasoncarreira", "CHANGES_REQUESTED", "2026-06-11T12:00:00Z"),
        ]},
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {},
    )
    assert count == 0 and cursor == {}
    # Empty ``me`` → skipped entirely.
    count, cursor = poller._check_own_changes_requested("o/r", "tok", "", {})
    assert count == 0 and cursor == {}
    assert captured_emits == []


def test_pr_list_api_failure_preserves_prior_cursor(
    monkeypatch, captured_emits,
):
    monkeypatch.setattr(poller, "_gh_api", lambda e, t: None)
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {"638": "aaa111"},
    )
    assert count == 0
    assert cursor == {"638": "aaa111"}


def test_reviews_api_failure_preserves_entry(monkeypatch, captured_emits):
    """Per-PR reviews fetch failing must not duplicate a prior reminder."""
    def fake_api(endpoint: str, token: str):
        if "/reviews" in endpoint:
            return None
        return [_pr(638, "aaa111")]

    monkeypatch.setattr(poller, "_gh_api", fake_api)
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {"638": "aaa111"},
    )
    assert count == 0
    assert cursor == {"638": "aaa111"}
