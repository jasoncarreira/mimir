"""Tests for ``_check_own_changes_requested`` — the chainlink #449
state-based reconciliation for the agent's own PRs stuck at
CHANGES_REQUESTED.

Mirrors the conventions of ``test_github_poller_pr_pushes.py``: mocks
``_gh_api`` per-endpoint and captures ``_emit`` via fixture. Asserts
both emit counts and the returned dedupe cursor, since the caller
relies on the rebuild-on-every-poll cleanup contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import poller


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _entry(sha: str, reminded_at: str = "2026-07-19T12:00:00Z") -> dict:
    return {"head_sha": sha, "last_reminded_at": reminded_at}


def _pr(
    number: int,
    sha: str,
    login: str = "mimir-bot",
    title: str = "My PR",
    base_sha: str = "base-sha",
) -> dict:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "user": {"login": login},
        "head": {"sha": sha},
        "base": {"sha": base_sha},
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


def test_stale_changes_requested_reemits_on_elapsed_boundary(
    monkeypatch, captured_emits,
):
    """The reminder is quiet before 60 minutes and eligible at 60."""
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [
            _review("jasoncarreira", "CHANGES_REQUESTED", "2026-06-11T12:00:00Z"),
        ]},
        commit_dates={"aaa111": "2026-06-11T05:00:00Z"},  # head predates review
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {}, now=NOW,
    )
    assert count == 1
    assert cursor == {"638": _entry("aaa111")}
    [ev] = captured_emits
    assert ev["event_type"] == "pr_changes_requested_stale"
    assert ev["reviewers"] == ["jasoncarreira"]
    assert "stuck at CHANGES_REQUESTED" in ev["prompt"]

    # Before the floor, repeated polls neither emit nor refresh the timestamp.
    count2, cursor2 = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
        now=NOW + timedelta(minutes=59, seconds=59),
    )
    assert count2 == 0
    assert cursor2 == {"638": _entry("aaa111")}
    assert len(captured_emits) == 1

    # Exactly at the boundary, re-emit and advance once.
    count3, cursor3 = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor2,
        now=NOW + timedelta(minutes=60),
    )
    assert count3 == 1
    assert cursor3 == {
        "638": _entry("aaa111", "2026-07-19T13:00:00Z"),
    }
    assert len(captured_emits) == 2

    count4, cursor4 = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor3,
        now=NOW + timedelta(minutes=60, seconds=1),
    )
    assert count4 == 0
    assert cursor4 == cursor3
    assert len(captured_emits) == 2


def test_reminder_interval_is_configurable(monkeypatch, captured_emits):
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [
            _review("jasoncarreira", "CHANGES_REQUESTED", "2026-06-11T12:00:00Z"),
        ]},
        commit_dates={"aaa111": "2026-06-11T05:00:00Z"},
    )
    prior = {"638": _entry("aaa111")}
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", prior,
        now=NOW + timedelta(minutes=10),
        reminder_interval=timedelta(minutes=10),
    )
    assert count == 1
    assert cursor == {"638": _entry("aaa111", "2026-07-19T12:10:00Z")}


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
        "o/r", "tok", "mimir-bot", {}, now=NOW,
    )
    assert count == 0
    assert cursor == {}
    assert captured_emits == []


def test_content_free_rebase_keeps_cadence_across_head_movement(
    monkeypatch, captured_emits,
):
    """A content-free rebase can make committer-date newer than the
    blocking review without addressing feedback.

    The first compare fixture intentionally has non-empty ``files`` —
    the real GitHub three-dot shape for a rebased PR is merge-base →
    current head, not reviewed-head → current-head tree equality.
    """
    unchanged_patch = "@@ -1 +1 @@\n-old\n+new"
    _patch_api(
        monkeypatch,
        prs=[_pr(642, "rebased-head", base_sha="new-base")],
        reviews_by_pr={642: [
            _review(
                "jasoncarreira",
                "CHANGES_REQUESTED",
                "2026-06-11T12:00:00Z",
                commit_id="reviewed-head",
            ),
        ]},
        commit_dates={"rebased-head": "2026-06-11T13:00:00Z"},
        compares={
            "reviewed-head...rebased-head": {
                "status": "diverged",
                "ahead_by": 1,
                "behind_by": 1,
                "merge_base_commit": {"sha": "old-base"},
                "files": [{"filename": "poller.py", "patch": unchanged_patch}],
            },
            "old-base...reviewed-head": {
                "files": [{
                    "filename": "poller.py",
                    "status": "modified",
                    "patch": unchanged_patch,
                }],
            },
            "new-base...rebased-head": {
                "files": [{
                    "filename": "poller.py",
                    "status": "modified",
                    "patch": unchanged_patch,
                }],
            },
        },
    )
    prior = {"642": _entry("reviewed-head")}
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", prior,
        now=NOW + timedelta(minutes=59, seconds=59),
    )
    assert count == 0
    assert cursor == {"642": _entry("rebased-head")}
    assert captured_emits == []

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
        now=NOW + timedelta(minutes=60),
    )
    assert count == 1
    assert cursor == {
        "642": _entry("rebased-head", "2026-07-19T13:00:00Z"),
    }
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
                "status": "ahead",
                "ahead_by": 1,
                "behind_by": 0,
                "merge_base_commit": {"sha": "reviewed-head"},
                "files": [{"filename": "mimir/poller.py"}],
            },
        },
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {"643": _entry("reviewed-head")},
        now=NOW + timedelta(minutes=60),
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
        "o/r", "tok", "mimir-bot", {"640": _entry("old-sha")}, now=NOW,
    )
    assert count == 0
    assert cursor == {}  # no longer blocked → entry dropped
    assert captured_emits == []


def test_closed_pr_drops_cursor_entry(monkeypatch, captured_emits):
    _patch_api(monkeypatch, prs=[])
    prior = {"640": _entry("old-sha")}
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", prior, now=NOW,
    )
    assert count == 0
    assert cursor == {}
    assert captured_emits == []


def test_unresolved_new_head_reminds_again_after_interval(
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
        "o/r", "tok", "mimir-bot", {"638": _entry("aaa111")},
        now=NOW + timedelta(minutes=60),
    )
    assert count == 1
    assert cursor == {"638": _entry("ddd444", "2026-07-19T13:00:00Z")}


def test_unknown_head_date_counts_as_stale_once(monkeypatch, captured_emits):
    """Commit-date lookup failure remains conservatively stale."""
    _patch_api(
        monkeypatch,
        prs=[_pr(641, "eee555")],
        reviews_by_pr={641: [
            _review("jasoncarreira", "CHANGES_REQUESTED", "2026-06-11T12:00:00Z"),
        ]},
        commit_dates={},  # /commits/<sha> returns None
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {}, now=NOW,
    )
    assert count == 1
    assert cursor == {"641": _entry("eee555")}


def test_legacy_cursor_migrates_quietly_for_a_full_interval(
    monkeypatch, captured_emits,
):
    _patch_api(
        monkeypatch,
        prs=[_pr(956, "legacy-sha")],
        reviews_by_pr={956: [
            _review("reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z"),
        ]},
        commit_dates={"legacy-sha": "2026-07-19T10:00:00Z"},
    )
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {"956": "legacy-sha"}, now=NOW,
    )
    assert count == 0
    assert cursor == {"956": _entry("legacy-sha")}
    assert captured_emits == []

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
        now=NOW + timedelta(minutes=59, seconds=59),
    )
    assert count == 0
    assert cursor == {"956": _entry("legacy-sha")}

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
        now=NOW + timedelta(minutes=60),
    )
    assert count == 1
    assert cursor == {
        "956": _entry("legacy-sha", "2026-07-19T13:00:00Z"),
    }


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
    for prior in ({"638": "aaa111"}, {"638": _entry("aaa111")}):
        count, cursor = poller._check_own_changes_requested(
            "o/r", "tok", "mimir-bot", prior, now=NOW + timedelta(hours=2),
        )
        assert count == 0
        assert cursor == prior
    assert captured_emits == []


def test_reviews_api_failure_preserves_entry(monkeypatch, captured_emits):
    """Per-PR reviews fetch failing must not duplicate a prior reminder."""
    def fake_api(endpoint: str, token: str):
        if "/reviews" in endpoint:
            return None
        return [_pr(638, "aaa111")]

    monkeypatch.setattr(poller, "_gh_api", fake_api)
    for prior in ({"638": "aaa111"}, {"638": _entry("aaa111")}):
        count, cursor = poller._check_own_changes_requested(
            "o/r", "tok", "mimir-bot", prior, now=NOW + timedelta(hours=2),
        )
        assert count == 0
        assert cursor == prior
    assert captured_emits == []
