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

import poller
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
