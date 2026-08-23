"""Tests for ``_check_own_changes_requested`` — the chainlink #449
state-based reconciliation for the agent's own PRs stuck at
CHANGES_REQUESTED.

Mirrors the conventions of ``test_github_poller_pr_pushes.py``: mocks
``_gh_api`` per-endpoint and captures ``_emit`` via fixture. Asserts
both emit counts and the returned dedupe cursor, since the caller
relies on the rebuild-on-every-poll cleanup contract.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from github_poller_test_support import poller
from mimir import poller_recovery
from mimir.models import AgentEvent


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _entry(
    sha: str,
    reminded_at: str = "2026-07-19T12:00:00Z",
    attempts: int = 1,
) -> dict:
    return {
        "head_sha": sha,
        "last_reminded_at": reminded_at,
        "attempts": attempts,
    }


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
        "head": {"sha": sha, "ref": f"worklink/{number}"},
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

    def fake_emit_signal(signal, **extras):
        events.append({"signal": signal, **extras})

    monkeypatch.setattr(poller, "_emit", fake_emit)
    monkeypatch.setattr(poller, "_emit_signal", fake_emit_signal)
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


def _recovery_entry(
    *, attempts: int = 0, outcome_at: str | None = None,
    disposition: str | None = None, reasons: list[str] | None = None,
) -> dict:
    entry = {
        "attempts": attempts,
        "enqueued_at": "2026-07-19T12:00:00+00:00",
        "event": {"extra": {"items": [{
            "event_type": "pr_changes_requested_stale",
            "repo": "o/r",
            "number": 638,
            "head_sha": "aaa111",
        }]}},
    }
    if outcome_at:
        entry["last_outcome_at"] = outcome_at
    if disposition:
        entry["outcome_disposition"] = disposition
    if reasons is not None:
        entry["attempt_reasons"] = reasons
    return entry


def _write_recovery(tmp_path, entries: dict) -> None:
    (tmp_path / ".recovery.json").write_text(json.dumps({
        "last_reconciled": "", "inflight": entries,
    }), encoding="utf-8")


def test_stale_changes_requested_reemits_on_elapsed_boundary(
    monkeypatch, captured_emits,
):
    """An unchanged head re-emits hourly despite small run-time jitter."""
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
    assert ev["head_ref"] == "worklink/638"
    assert ev["reviewers"] == ["jasoncarreira"]
    assert "stuck at CHANGES_REQUESTED" in ev["prompt"]

    # Before the floor, repeated polls neither emit nor refresh the timestamp.
    count2, cursor2 = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
        now=NOW + timedelta(minutes=58, seconds=59),
    )
    assert count2 == 0
    assert cursor2 == {"638": _entry("aaa111")}
    assert len(captured_emits) == 1

    # Exactly at the one-minute jitter-adjusted boundary, re-emit once.
    count3, cursor3 = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor2,
        now=NOW + timedelta(minutes=59),
    )
    assert count3 == 1
    assert cursor3 == {
        "638": _entry("aaa111", "2026-07-19T12:59:00Z", attempts=2),
    }
    assert len(captured_emits) == 2

    count4, cursor4 = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor3,
        now=NOW + timedelta(minutes=59, seconds=1),
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
    assert cursor == {
        "638": _entry("aaa111", "2026-07-19T12:10:00Z", attempts=2),
    }


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


def test_offset_commit_date_is_compared_as_an_instant(
    monkeypatch, captured_emits,
):
    """13:00 +02:00 predates 12:00 UTC despite sorting after it."""
    _patch_api(
        monkeypatch,
        prs=[_pr(644, "offset-head")],
        reviews_by_pr={644: [
            _review("jasoncarreira", "CHANGES_REQUESTED", "2026-06-11T12:00:00Z"),
        ]},
        commit_dates={"offset-head": "2026-06-11T13:00:00+02:00"},
    )

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {}, now=NOW,
    )

    assert count == 1
    assert cursor == {"644": _entry("offset-head")}
    assert len(captured_emits) == 1


def test_content_free_rebase_resets_attempt_budget(
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
    assert count == 1
    assert cursor == {
        "642": _entry("rebased-head", "2026-07-19T12:59:59Z"),
    }
    assert captured_emits[0]["attempt"] == 1

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
        now=NOW + timedelta(minutes=60),
    )
    assert count == 0
    assert cursor == {
        "642": _entry("rebased-head", "2026-07-19T12:59:59Z"),
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


def test_legacy_cursor_migrates_quietly(
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
        now=NOW + timedelta(minutes=58, seconds=59),
    )
    assert count == 0
    assert cursor == {"956": _entry("legacy-sha")}

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
        now=NOW + timedelta(minutes=59),
    )
    assert count == 1
    assert cursor == {
        "956": _entry("legacy-sha", "2026-07-19T12:59:00Z", attempts=2),
    }


def test_structured_legacy_cursor_migrates_quietly(
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
    legacy = {
        "956": {
            "head_sha": "legacy-sha",
            "last_reminded_at": "2026-07-19T10:00:00Z",
        },
    }

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", legacy, now=NOW,
    )

    assert count == 0
    assert cursor == {"956": _entry("legacy-sha")}
    assert captured_emits == []


def test_attempt_cap_gives_up_once_then_rearms_after_backstop(
    monkeypatch, captured_emits,
):
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [
            _review("reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z"),
        ]},
        commit_dates={"aaa111": "2026-07-19T10:00:00Z"},
    )
    cap = poller.REVIEW_REQUEST_MAX_ATTEMPTS
    prior = {"638": _entry("aaa111", "2026-07-19T11:00:00Z", cap)}

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", prior, now=NOW,
    )
    assert count == 1
    assert cursor == {
        "638": _entry("aaa111", "2026-07-19T12:00:00Z", cap + 1),
    }
    [gave_up] = captured_emits
    assert gave_up["signal"] == "pr_changes_requested_gave_up"
    assert gave_up["attempts"] == cap

    count, cursor2 = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
        now=NOW + timedelta(hours=23, minutes=59, seconds=59),
    )
    assert count == 0
    assert cursor2 == cursor
    assert len(captured_emits) == 1

    # Emissions can be stranded in the in-memory queue. A long backstop starts
    # a new bounded series even when the head never changed, so give-up is not
    # a permanent silent state for the exact PR that still needs remediation.
    count, cursor3 = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor2,
        now=NOW + timedelta(days=1),
    )
    assert count == 1
    assert cursor3 == {
        "638": _entry("aaa111", "2026-07-20T12:00:00Z", attempts=1),
    }
    assert len(captured_emits) == 2
    assert captured_emits[-1]["event_type"] == "pr_changes_requested_stale"
    assert captured_emits[-1]["attempt"] == 1


def test_emitted_but_undelivered_reminder_does_not_charge_or_duplicate(
    monkeypatch, captured_emits, tmp_path,
):
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [_review(
            "reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z",
        )]},
        commit_dates={"aaa111": "2026-07-19T10:00:00Z"},
    )
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    _write_recovery(tmp_path, {})

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {}, now=NOW,
    )
    assert count == 1
    assert cursor["638"]["attempts"] == 0

    _write_recovery(tmp_path, {"queued": _recovery_entry()})
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor, now=NOW + timedelta(hours=2),
    )
    assert count == 0
    assert cursor["638"]["attempts"] == 0
    assert len(captured_emits) == 1

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor, now=NOW + timedelta(days=1),
    )
    assert count == 1
    assert cursor["638"]["attempts"] == 0
    assert len(captured_emits) == 2


def test_hard_refusal_is_uncharged_and_retries_only_at_daily_backstop(
    monkeypatch, captured_emits, tmp_path,
):
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [_review(
            "reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z",
        )]},
        commit_dates={"aaa111": "2026-07-19T10:00:00Z"},
    )
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    refused = _recovery_entry(
        outcome_at="2026-07-19T12:00:00+00:00",
        disposition="exempt_hard_refusal",
    )
    refused["outcome_reason"] = "service_scope_denied"
    _write_recovery(tmp_path, {"refused": refused})
    prior = {"638": _entry("aaa111", attempts=0)}

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", prior, now=NOW + timedelta(hours=2),
    )
    assert count == 0
    assert cursor["638"]["attempts"] == 0

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor, now=NOW + timedelta(days=1),
    )
    assert count == 1
    assert cursor["638"]["attempts"] == 0
    assert captured_emits[0]["attempt"] == 1
    assert captured_emits[0]["prior_refusal_reasons"] == ["service_scope_denied"]
    assert captured_emits[0]["prior_refusal_classification"] == "operator_gated"


def test_real_retained_lease_refusal_retries_at_hourly_floor_without_charge(
    monkeypatch, captured_emits, tmp_path,
):
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [_review(
            "reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z",
        )]},
        commit_dates={"aaa111": "2026-07-19T10:00:00Z"},
    )
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    reason = (
        "repository checkout rejected: superseded PR checkout lease has retained "
        "work; refusing release: deadbeef (/workspace/pr-leases/retained-lease)"
    )
    refused = _recovery_entry(
        outcome_at="2026-07-19T12:01:00+00:00",
        disposition="exempt_hard_refusal",
    )
    refused["outcome_reason"] = reason
    _write_recovery(tmp_path, {"refused": refused})
    prior = {"638": _entry("aaa111", attempts=0)}

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", prior,
        now=NOW + timedelta(minutes=58, seconds=59),
    )
    assert count == 0
    assert cursor["638"]["attempts"] == 0

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
        now=NOW + timedelta(minutes=59),
    )
    assert count == 1
    assert cursor["638"]["attempts"] == 0
    [event] = captured_emits
    assert event["attempt"] == 1
    assert event["prior_refusal_reasons"] == [reason]
    assert event["prior_refusal_classification"] == "self_clearing"
    assert event["prior_self_clearing_refusals"] == 1


@pytest.mark.parametrize("outcome_reason", [None, "new_unrecognised_boundary"])
def test_absent_and_unknown_refusal_reasons_default_to_operator_gated(
    monkeypatch, captured_emits, tmp_path, outcome_reason,
):
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [_review(
            "reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z",
        )]},
        commit_dates={"aaa111": "2026-07-19T10:00:00Z"},
    )
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    refused = _recovery_entry(
        outcome_at="2026-07-19T12:00:00+00:00",
        disposition="exempt_hard_refusal",
    )
    if outcome_reason is not None:
        refused["outcome_reason"] = outcome_reason
    _write_recovery(tmp_path, {"refused": refused})
    prior = {"638": _entry("aaa111", attempts=0)}

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", prior, now=NOW + timedelta(hours=2),
    )
    assert count == 0
    assert cursor["638"]["attempts"] == 0

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor, now=NOW + timedelta(days=1),
    )
    assert count == 1
    assert cursor["638"]["attempts"] == 0
    [event] = captured_emits
    assert event["prior_refusal_classification"] == "operator_gated"
    assert event["prior_refusal_reasons"] == [
        outcome_reason or "hard_boundary_refusal"
    ]


def test_self_clearing_refusal_series_is_bounded_and_rearms_daily(
    monkeypatch, captured_emits, tmp_path,
):
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [_review(
            "reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z",
        )]},
        commit_dates={"aaa111": "2026-07-19T10:00:00Z"},
    )
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    reason = "repository checkout rejected: PR checkout lease collision"
    refused_entries = {}
    for attempt in range(poller.REVIEW_REQUEST_MAX_ATTEMPTS):
        refused = _recovery_entry(
            outcome_at=f"2026-07-19T{12 + attempt:02d}:01:00+00:00",
            disposition="exempt_hard_refusal",
        )
        refused["outcome_reason"] = reason
        refused_entries[str(attempt)] = refused
    _write_recovery(tmp_path, refused_entries)
    prior = {
        "638": _entry("aaa111", "2026-07-19T14:00:00Z", attempts=0),
    }

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", prior, now=NOW + timedelta(hours=3),
    )
    assert count == 0
    assert cursor["638"]["attempts"] == 0

    rearmed_at = "2026-07-20T14:01:00Z"
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
        now=NOW + timedelta(days=1, hours=2, minutes=1),
    )
    assert count == 1
    assert cursor["638"]["attempts"] == 0
    assert cursor["638"]["rearmed_at"] == rearmed_at
    [event] = captured_emits
    assert event["prior_refusal_classification"] == "self_clearing"
    assert event["prior_self_clearing_refusals"] == poller.REVIEW_REQUEST_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_repeated_refused_turns_never_exhaust_remediation_budget(
    monkeypatch, captured_emits, tmp_path,
):
    """Framework outcomes, not a synthesized cursor, reproduce the #1459 path."""
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [_review(
            "reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z",
        )]},
        commit_dates={"aaa111": "2026-07-19T10:00:00Z"},
    )
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    events_path = tmp_path / "events.jsonl"
    item = {
        "event_type": "pr_changes_requested_stale",
        "repo": "o/r",
        "number": 638,
        "head_sha": "aaa111",
    }
    for attempt in range(poller.REVIEW_REQUEST_MAX_ATTEMPTS + 1):
        source_id = f"refused-{attempt}"
        await poller_recovery.stash_enqueued_event(
            tmp_path,
            AgentEvent(
                trigger="poller",
                channel_id="poller:github-activity",
                content="fix PR",
                source_id=source_id,
                extra={"poller_name": "github-activity", "items": [item]},
            ),
            enqueued_at=(NOW - timedelta(minutes=1)).isoformat(),
        )
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "turn_completed",
                "timestamp": (NOW + timedelta(minutes=attempt)).isoformat(),
                "channel_id": "poller:github-activity",
                "source_id": source_id,
                "attempt_disposition": "exempt_hard_refusal",
                "attempt_reason": "unsupported_operation",
                "hard_refusals": [{
                    "tool": "unsupported_operation",
                    "boundary": "typed_action_set",
                    "reason": "unsupported_operation",
                }],
                "remediation_effects": [],
            }) + "\n")

    await poller_recovery.reconcile_failed_turns(
        poller_name="github-activity",
        channel_id="poller:github-activity",
        persist_dir=tmp_path,
        events_path=events_path,
        enqueue=lambda event: None,
        recover_failed_turns=False,
    )
    recovery = poller_recovery._load_state(tmp_path)["inflight"]
    assert len(recovery) == poller.REVIEW_REQUEST_MAX_ATTEMPTS + 1
    assert all(entry["attempts"] == 0 for entry in recovery.values())
    assert all(
        entry["attempt_reasons"] == ["unsupported_operation"]
        for entry in recovery.values()
    )

    prior = {"638": _entry("aaa111", attempts=0)}
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", prior,
        now=NOW + timedelta(hours=2),
    )
    assert count == 0
    assert cursor["638"]["attempts"] == 0
    assert not any(
        event.get("signal") == "pr_changes_requested_gave_up"
        for event in captured_emits
    )

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", cursor,
        now=NOW + timedelta(days=1, minutes=poller.REVIEW_REQUEST_MAX_ATTEMPTS),
    )
    assert count == 1
    assert cursor["638"]["attempts"] == 0
    assert captured_emits[-1]["event_type"] == "pr_changes_requested_stale"
    assert captured_emits[-1]["prior_refusal_reasons"] == ["unsupported_operation"]
    assert not any(
        event.get("signal") == "pr_changes_requested_gave_up"
        for event in captured_emits
    )


def test_model_failure_charges_attempt_and_advances_retry(
    monkeypatch, captured_emits, tmp_path,
):
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [_review(
            "reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z",
        )]},
        commit_dates={"aaa111": "2026-07-19T10:00:00Z"},
    )
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    _write_recovery(tmp_path, {"failed": _recovery_entry(
        attempts=1,
        outcome_at="2026-07-19T12:01:00+00:00",
        disposition="charge",
        reasons=["model_context_window_exceeded"],
    )})

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {"638": _entry("aaa111", attempts=0)},
        now=NOW + timedelta(hours=2),
    )
    assert count == 1
    assert cursor["638"]["attempts"] == 1
    assert captured_emits[0]["attempt"] == 2


def test_completed_turn_with_unchanged_pr_charges_attempt(
    monkeypatch, captured_emits, tmp_path,
):
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [_review(
            "reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z",
        )]},
        commit_dates={"aaa111": "2026-07-19T10:00:00Z"},
    )
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    _write_recovery(tmp_path, {"completed": _recovery_entry(
        attempts=1,
        outcome_at="2026-07-19T12:01:00+00:00",
        disposition="charge",
        reasons=["turn_completed_without_state_change"],
    )})

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {"638": _entry("aaa111", attempts=0)},
        now=NOW + timedelta(hours=2),
    )
    assert count == 1
    assert cursor["638"]["attempts"] == 1
    assert captured_emits[0]["attempt"] == 2


def test_exhaustion_reports_each_charged_outcome_reason(
    monkeypatch, captured_emits, tmp_path,
):
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [_review(
            "reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z",
        )]},
        commit_dates={"aaa111": "2026-07-19T10:00:00Z"},
    )
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    reasons = ["bad_patch", "tests_failed", "review_unaddressed"]
    _write_recovery(tmp_path, {"failed": _recovery_entry(
        attempts=3,
        outcome_at="2026-07-19T12:01:00+00:00",
        disposition="charge",
        reasons=reasons,
    )})

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {"638": _entry("aaa111", attempts=0)},
        now=NOW + timedelta(hours=2),
    )
    assert count == 1
    assert cursor["638"]["attempts"] == poller.REVIEW_REQUEST_MAX_ATTEMPTS + 1
    assert captured_emits[0]["signal"] == "pr_changes_requested_gave_up"
    assert captured_emits[0]["attempt_reasons"] == reasons


def test_seconds_after_boundary_reemits_on_intended_cycle(
    monkeypatch, captured_emits,
):
    _patch_api(
        monkeypatch,
        prs=[_pr(638, "aaa111")],
        reviews_by_pr={638: [
            _review("reviewer", "CHANGES_REQUESTED", "2026-07-19T11:00:00Z"),
        ]},
        commit_dates={"aaa111": "2026-07-19T10:00:00Z"},
    )
    prior = {"638": _entry("aaa111", "2026-07-19T12:00:04Z")}

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", prior,
        now=NOW + timedelta(hours=1),
    )

    assert count == 1
    assert cursor == {
        "638": _entry("aaa111", "2026-07-19T13:00:00Z", attempts=2),
    }
    assert captured_emits[0]["attempt"] == 2


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


def _cr_review() -> dict:
    return _review("jasoncarreira", "CHANGES_REQUESTED", "2026-06-11T12:00:00Z")


def _patch_many(monkeypatch, numbers):
    """Every listed PR is the agent's own and stuck at CHANGES_REQUESTED."""
    _patch_api(
        monkeypatch,
        prs=[_pr(n, f"sha{n}") for n in numbers],
        reviews_by_pr={n: [_cr_review()] for n in numbers},
        commit_dates={f"sha{n}": "2026-06-11T05:00:00Z" for n in numbers},
    )


def test_rotate_preserves_every_element_and_wraps():
    assert poller._rotate([1, 2, 3, 4], 0) == [1, 2, 3, 4]
    assert poller._rotate([1, 2, 3, 4], 2) == [3, 4, 1, 2]
    assert poller._rotate([1, 2, 3, 4], 6) == [3, 4, 1, 2]  # offset wraps
    assert sorted(poller._rotate([1, 2, 3, 4], 3)) == [1, 2, 3, 4]  # nothing lost
    assert poller._rotate([], 5) == []


def test_exhausted_budget_still_reconciles_the_guaranteed_minimum(
    monkeypatch, captured_emits,
):
    """An already-spent budget must not starve the pass to zero: chainlink
    #1433's floor is what guarantees forward progress every tick."""
    numbers = [601, 602, 603, 604, 605]
    _patch_many(monkeypatch, numbers)
    budget = poller.TickBudget(deadline_seconds=0.0)
    assert budget.exhausted()

    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {}, now=NOW, tick_budget=budget,
    )
    assert count == poller.PR_RECONCILE_MIN_PER_PASS
    assert budget.truncated == {
        "changes_requested": len(numbers) - poller.PR_RECONCILE_MIN_PER_PASS,
    }
    assert sorted(cursor) == ["601", "602"]


def test_truncated_prs_keep_their_prior_cursor_entry(monkeypatch, captured_emits):
    """The passes rebuild their cursor from scratch, so a skipped PR must be
    carried over explicitly. Dropping the key would reset ``last_reminded_at``
    (reminder storm on the next tick) and rewind ``attempts`` (give-up budget
    never reached)."""
    numbers = [601, 602, 603, 604, 605]
    _patch_many(monkeypatch, numbers)
    recent = (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prior = {
        str(n): _entry(f"sha{n}", reminded_at=recent, attempts=3) for n in numbers
    }

    budget = poller.TickBudget(deadline_seconds=0.0)
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", prior, now=NOW, tick_budget=budget,
    )
    # Nothing is due (reminded a minute ago), so no PR — reconciled or skipped —
    # changes, and the cursor round-trips intact.
    assert count == 0
    assert captured_emits == []
    assert cursor == prior


def test_rotate_offset_advances_the_reconciled_window(monkeypatch, captured_emits):
    """Successive truncated ticks must cover different PRs, or the tail is
    never reconciled at all."""
    numbers = [601, 602, 603, 604, 605]

    def _reconciled_with(offset: int) -> list[int]:
        emits: list[dict] = []
        monkeypatch.setattr(poller, "_emit", lambda p, **e: emits.append(e))
        _patch_many(monkeypatch, numbers)
        poller._check_own_changes_requested(
            "o/r", "tok", "mimir-bot", {}, now=NOW,
            tick_budget=poller.TickBudget(deadline_seconds=0.0),
            rotate_offset=offset,
        )
        return sorted(e["number"] for e in emits)

    first = _reconciled_with(0)
    second = _reconciled_with(poller.PR_RECONCILE_MIN_PER_PASS)
    third = _reconciled_with(poller.PR_RECONCILE_MIN_PER_PASS * 2)
    assert first == [601, 602]
    assert second == [603, 604]
    assert third == [601, 605]  # wraps, and 605 is finally reached
    assert set(first) | set(second) | set(third) == set(numbers)


def test_unexhausted_budget_reconciles_every_pr(monkeypatch, captured_emits):
    """The bound must be inert when the tick has time left."""
    numbers = [601, 602, 603]
    _patch_many(monkeypatch, numbers)
    budget = poller.TickBudget(deadline_seconds=600.0)
    count, cursor = poller._check_own_changes_requested(
        "o/r", "tok", "mimir-bot", {}, now=NOW, tick_budget=budget,
    )
    assert count == len(numbers)
    assert budget.truncated == {}
    assert sorted(cursor) == ["601", "602", "603"]


# --- #1705 review: the bound must be wall-clock, not call-count -------------


def test_gh_api_timeout_is_clamped_to_the_remaining_hard_budget(monkeypatch):
    """Checking the budget only between PRs leaves a single call free to carry
    the tick past the framework's SIGKILL. Each call must be clamped instead."""
    seen: list[float] = []

    class _Result:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(argv, **kwargs):
        seen.append(kwargs["timeout"])
        return _Result()

    monkeypatch.setattr(poller.subprocess, "run", fake_run)

    # No budget installed: the standing ceiling applies.
    poller.set_active_tick_budget(None)
    poller._gh_api("repos/o/r/pulls", "tok")
    assert seen[-1] == float(poller.GH_API_TIMEOUT_SECONDS)

    # Plenty of budget left: still capped by the ceiling, never above it.
    poller.set_active_tick_budget(poller.TickBudget(hard_deadline_seconds=600.0))
    poller._gh_api("repos/o/r/pulls", "tok")
    assert seen[-1] == float(poller.GH_API_TIMEOUT_SECONDS)

    # Little budget left: clamped down, so the call cannot outlive the tick.
    budget = poller.TickBudget(hard_deadline_seconds=5.0)
    poller.set_active_tick_budget(budget)
    poller._gh_api("repos/o/r/pulls", "tok")
    assert poller.GH_API_MIN_TIMEOUT_SECONDS <= seen[-1] <= 5.0
    assert seen[-1] < poller.GH_API_TIMEOUT_SECONDS

    # Budget already spent: the call is not made at all. Flooring it to a
    # minimum instead would let each late call overshoot the hard deadline, so
    # the bound would be "deadline + one full timeout" rather than the deadline.
    before = len(seen)
    poller.set_active_tick_budget(poller.TickBudget(hard_deadline_seconds=0.0))
    assert poller._gh_api("repos/o/r/pulls", "tok") is None
    assert len(seen) == before, "a call was made past the hard deadline"
    poller.set_active_tick_budget(None)


def test_hard_deadline_overrides_the_minimum_floor():
    """The floor guarantees progress against the soft deadline only. Past the
    hard deadline it must not authorise even one more PR, or the 'minimum' is an
    unbounded exception to the tick bound."""
    spent_soft = poller.TickBudget(deadline_seconds=0.0, hard_deadline_seconds=600.0)
    # Soft-exhausted: the floor still admits the first PRs.
    assert poller._truncate_here(spent_soft, 0, "changes_requested") is False
    assert poller._truncate_here(spent_soft, 1, "changes_requested") is False
    assert poller._truncate_here(spent_soft, 2, "changes_requested") is True
    assert spent_soft.hard_truncated is False

    spent_hard = poller.TickBudget(deadline_seconds=0.0, hard_deadline_seconds=0.0)
    # Hard-exhausted: truncates immediately, floor or not.
    assert poller._truncate_here(spent_hard, 0, "changes_requested") is True
    # ...but does NOT freeze the watermark. These passes defer through their own
    # dedupe cursor, so holding `last_checked` here would stop the since-window
    # advancing on every busy tick — a slow-motion version of #1433.
    assert spent_hard.hard_truncated is False


def test_hard_stop_has_no_floor_and_marks_the_tick():
    """The since-based passes keep no dedupe cursor, so they get no floor —
    and their truncation must mark the tick so the watermark is held."""
    live = poller.TickBudget(hard_deadline_seconds=600.0)
    assert poller._hard_stop(live, "pr_reviews") is False
    assert poller._hard_stop(None, "pr_reviews") is False

    spent = poller.TickBudget(hard_deadline_seconds=0.0)
    assert poller._hard_stop(spent, "pr_reviews") is True
    assert spent.hard_truncated is True


def _slow_main_harness(
    monkeypatch, tmp_path, *, n_prs, call_seconds, soft, hard,
    author="mimir-bot",
):
    """Drive the real pass sequence through main() with deliberately slow calls.

    Deadlines are scaled down rather than faked, so the wall-clock path under
    test is the real one — `time.monotonic` is not patched.
    """
    import time as _time

    numbers = list(range(600, 600 + n_prs))
    calls: list[str] = []
    saved: list[dict] = []

    def pr(n):
        return {
            "number": n, "title": f"PR {n}", "state": "open", "merged": False,
            "merged_at": None, "html_url": f"https://github.com/o/r/pull/{n}",
            "user": {"login": author}, "mergeable": True,
            "created_at": "2026-08-23T11:00:00Z",
            "head": {"sha": f"{n:040d}", "ref": f"worklink/{n}",
                     "repo": {"full_name": "o/r"}},
            "base": {"sha": "b" * 40, "ref": "main"},
        }

    prs = [pr(n) for n in numbers]

    def slow_api(endpoint: str, token: str):
        calls.append(endpoint)
        _time.sleep(call_seconds)
        if "pulls?state=open" in endpoint:
            return prs
        if endpoint.endswith("/reviews"):
            return [_review("jasoncarreira", "CHANGES_REQUESTED",
                            "2026-08-22T12:00:00Z")]
        if "/check-runs" in endpoint:
            return {"check_runs": []}
        if "/compare/" in endpoint:
            return {"behind_by": 1}
        if "/commits/" in endpoint:
            return {"commit": {"committer": {"date": "2026-08-20T00:00:00Z"}}}
        tail = endpoint.rsplit("/", 1)[1]
        if "/pulls/" in endpoint and tail.isdigit():
            return prs[numbers.index(int(tail))]
        return []

    real_budget = poller.TickBudget
    monkeypatch.setattr(
        poller, "TickBudget",
        lambda: real_budget(deadline_seconds=soft, hard_deadline_seconds=hard),
    )
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "_resolve_token", lambda: "tok")
    monkeypatch.setattr(poller, "_gh_api", slow_api)
    monkeypatch.setattr(poller, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(poller, "_emit_signal", lambda *a, **k: None)
    monkeypatch.setattr(poller, "_save_cursor", lambda c: saved.append(c))
    monkeypatch.setattr(
        poller, "_load_cursor",
        lambda: {"last_checked": "2026-08-23T10:00:00Z"},
    )
    monkeypatch.setenv("GITHUB_REPOS", "o/r")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "mimir-bot")

    started = _time.monotonic()
    poller.main()
    return _time.monotonic() - started, calls, saved


def test_main_returns_within_the_tick_bound_when_every_call_is_slow(
    monkeypatch, tmp_path,
):
    """The postcondition that matters: a slow API cannot carry main() past the
    framework cap. Deadlines are scaled (0.3s soft / 0.5s hard) so the test is
    fast, but nothing about the timing path is stubbed — `time.monotonic` is
    real.

    The thresholds are chosen so the *unbounded* path cannot satisfy them:
    `_check_pr_reviews` alone walks all 150 open PRs at 0.01s per call, so
    without a hard stop the tick spends >=1.5s and makes >=150 calls before the
    reconcile passes even begin. Bounded, it stops near the 0.5s deadline.
    Removing the hard stop must fail this test, not squeeze under it.
    """
    elapsed, calls, saved = _slow_main_harness(
        monkeypatch, tmp_path, n_prs=150, call_seconds=0.01, soft=0.3, hard=0.5,
    )
    # Scaled equivalent of "returns before POLLER_TIMEOUT_SECONDS": the tick
    # finishes near its hard deadline plus one in-flight call, not at the
    # unbounded cost of the work it was asked to do.
    assert elapsed < 1.2, f"tick ran {elapsed:.2f}s; wall-clock bound not enforced"
    assert len(calls) < 100, f"{len(calls)} calls; per-item loops not bounded"
    # It must still have returned under its own power and saved a cursor.
    assert len(saved) == 1


def test_hard_truncated_tick_holds_its_watermark(monkeypatch, tmp_path):
    """A hard-truncated tick skipped items in since-based passes that keep no
    dedupe cursor, so advancing `last_checked` would step over them for good."""
    _elapsed, _calls, saved = _slow_main_harness(
        monkeypatch, tmp_path, n_prs=150, call_seconds=0.01, soft=0.3, hard=0.5,
    )
    assert saved[0]["last_checked"] == "2026-08-23T10:00:00Z"


def test_untruncated_tick_still_advances_its_watermark(monkeypatch, tmp_path):
    """The hold must be specific to hard truncation — a tick with room to spare
    has to advance, or the window grows forever."""
    _elapsed, _calls, saved = _slow_main_harness(
        monkeypatch, tmp_path, n_prs=2, call_seconds=0.0, soft=600.0, hard=600.0,
    )
    assert saved[0]["last_checked"] != "2026-08-23T10:00:00Z"


#: Captured at import, before the autouse fixture stubs it out. That fixture
#: replacing `_pr_author_is_trusted` with an instant lambda is precisely why the
#: first wall-clock regression could not see the attestation transport.
_REAL_TRUST_RESOLVER = poller._pr_author_is_trusted


def test_slow_trust_attestations_cannot_overrun_the_tick(monkeypatch, tmp_path):
    """The second GitHub transport.

    `_check_prs` and `_check_pr_pushes` resolve author trust through
    `mimir.pollers._github_content_author` / `_github_author_is_trusted`, which
    reach GitHub over `urllib` with their own 10s timeouts — not through
    `_gh_api`'s subprocess. Clamping only `_gh_api` left this path free to carry
    the tick past the framework cap.

    It is bounded by reserving each lookup's worst case rather than by passing a
    timeout across the module boundary: doing the latter would make the installed
    skill script require a matching `mimir` package, and a live dry run against a
    slightly older checkout failed with exactly that `TypeError`.
    """
    import time as _time

    monkeypatch.setattr(poller, "_pr_author_is_trusted", _REAL_TRUST_RESOLVER)
    # Scale the reservations with the scaled deadlines, or every lookup is
    # refused before it starts and the test silently proves nothing.
    monkeypatch.setattr(poller, "AUTHOR_LOOKUP_WORST_CASE_SECONDS", 0.06)
    monkeypatch.setattr(poller, "TRUST_LOOKUP_WORST_CASE_SECONDS", 0.12)
    started: list[str] = []

    def slow_author(repo, extras, token):
        started.append("author")
        _time.sleep(0.05)
        return "outside-contributor"

    def slow_trust(repo, author, token):
        started.append("trust")
        _time.sleep(0.05)
        return True

    monkeypatch.setattr(poller, "_github_content_author", slow_author)
    monkeypatch.setattr(poller, "_github_author_is_trusted", slow_trust)

    elapsed, _calls, saved = _slow_main_harness(
        monkeypatch, tmp_path, n_prs=150, call_seconds=0.01,
        soft=0.3, hard=0.5, author="outside-contributor",
    )

    assert elapsed < 1.2, f"tick ran {elapsed:.2f}s; trust path not bounded"
    assert len(saved) == 1
    # The transport was genuinely exercised...
    assert started, "trust path never reached — the test cannot see the bug"
    # ...and stopped well short of one lookup per PR, which unbounded would be.
    assert len(started) < 40, f"{len(started)} lookups; headroom not reserved"


def test_unresolvable_trust_skips_the_pr_instead_of_marking_it_untrusted(
    monkeypatch, tmp_path,
):
    """A budget-denied trust lookup must not fail closed.

    `False` routes the PR through `_surface_untrusted_pr_once`, which emits a
    signal and records the verdict as already-surfaced — so a trusted
    contributor's PR would be permanently mislabelled because the poller ran out
    of time. `None` means "unresolved": skip, hold the watermark, retry next tick.
    """
    monkeypatch.setattr(poller, "_pr_author_is_trusted", _REAL_TRUST_RESOLVER)
    surfaced: set[str] = set()
    calls: list[str] = []

    monkeypatch.setattr(
        poller, "_github_content_author",
        lambda *a: calls.append("author") or "outside-contributor",
    )
    monkeypatch.setattr(
        poller, "_github_author_is_trusted",
        lambda *a: calls.append("trust") or True,
    )
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda ep, tok: [{
            "number": 700, "title": "t", "state": "open",
            "html_url": "https://github.com/o/r/pull/700",
            "user": {"login": "outside-contributor"},
            "created_at": "2026-08-23T11:00:00Z",
            "head": {"sha": "a" * 40, "ref": "f", "repo": {"full_name": "o/r"}},
            "base": {"sha": "b" * 40, "ref": "main"},
        }] if "pulls?state=open" in ep else [],
    )
    emitted: list[dict] = []
    monkeypatch.setattr(poller, "_emit", lambda p, **e: emitted.append(e))
    monkeypatch.setattr(poller, "_emit_signal", lambda s, **e: emitted.append(e))

    spent = poller.TickBudget(hard_deadline_seconds=0.0)
    count = poller._check_prs(
        "o/r", "2026-08-23T10:00:00Z", "tok", "mimir-bot",
        surfaced_untrusted=surfaced, tick_budget=spent,
    )

    assert count == 0
    assert calls == [], "an attestation was made past the hard deadline"
    assert surfaced == set(), "unresolved trust was recorded as untrusted"
    assert emitted == [], "unresolved trust emitted a verdict"
    assert spent.hard_truncated is True, "watermark not held for a skipped PR"


def test_unresolvable_trust_in_pushes_carries_the_prior_head_forward(monkeypatch):
    """`_check_pr_pushes` rebuilds its head map from scratch, so a PR skipped for
    lack of trust budget must have its *prior* head carried forward.

    Dropping the key makes the next tick treat the PR as first-seen and miss the
    push; writing the *current* head makes the next tick see no change and miss
    it just as thoroughly. Only the prior head preserves the comparison.
    """
    monkeypatch.setattr(poller, "_pr_author_is_trusted", _REAL_TRUST_RESOLVER)
    attempted: list[str] = []
    monkeypatch.setattr(
        poller, "_github_content_author",
        lambda *a: attempted.append("author") or "outside-contributor",
    )
    monkeypatch.setattr(poller, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(poller, "_emit_signal", lambda *a, **k: None)
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda ep, tok: [{
            "number": 700, "title": "t", "state": "open",
            "html_url": "https://github.com/o/r/pull/700",
            "user": {"login": "outside-contributor"},
            "head": {"sha": "n" * 40, "ref": "f", "repo": {"full_name": "o/r"}},
            "base": {"sha": "b" * 40, "ref": "main"},
        }] if "pulls?state=open" in ep else [],
    )

    spent = poller.TickBudget(hard_deadline_seconds=0.0)
    count, new_heads, _rr = poller._check_pr_pushes(
        "o/r", "tok", "mimir-bot", {"700": "o" * 40}, tick_budget=spent,
    )

    assert count == 0
    assert attempted == [], "an attestation was made past the hard deadline"
    assert new_heads == {"700": "o" * 40}, (
        "prior head not carried forward — next tick would miss the push"
    )
    assert spent.hard_truncated is True
