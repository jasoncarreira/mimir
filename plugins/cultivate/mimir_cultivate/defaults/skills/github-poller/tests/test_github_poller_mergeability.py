from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from github_poller_test_support import poller


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
BASE = "b" * 40


def _pr(*, mergeable: bool | None, number: int = 42) -> dict:
    return {
        "number": number,
        "title": "Ready after rebase",
        "html_url": f"https://github.com/o/r/pull/{number}",
        "state": "open",
        "merged": False,
        "merged_at": None,
        "user": {"login": "mimir-bot"},
        "head": {
            "sha": HEAD,
            "ref": f"worklink/{number}",
            "repo": {"full_name": "o/r"},
        },
        "base": {"sha": BASE, "ref": "main"},
        "mergeable": mergeable,
    }


def _patch_api(monkeypatch, *, mergeable, behind, reviews=None):
    listed = _pr(mergeable=None)
    detail = _pr(mergeable=mergeable)

    def fake_api(endpoint: str, token: str):
        if endpoint.endswith("/pulls/42/reviews"):
            return reviews or []
        if endpoint.endswith("/pulls/42"):
            return detail
        if "/compare/" in endpoint:
            return {"behind_by": behind}
        if "pulls?state=open" in endpoint:
            return [listed]
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", fake_api)


@pytest.fixture
def events(monkeypatch):
    captured = []
    monkeypatch.setattr(
        poller, "_emit", lambda prompt, **extra: captured.append({"prompt": prompt, **extra}),
    )
    monkeypatch.setattr(
        poller, "_emit_signal", lambda signal, **extra: captured.append({"signal": signal, **extra}),
    )
    return captured


def test_behind_clean_emits_scoped_rebase_and_push_work(monkeypatch, events):
    _patch_api(monkeypatch, mergeable=True, behind=12)

    count, cursor = poller._check_own_mergeability(
        "o/r", "tok", "mimir-bot", {}, now=NOW,
    )

    assert count == 1
    assert cursor["42"] == {
        "head_sha": HEAD,
        "reason": "behind_base",
        "last_attempt_at": "2026-07-31T12:00:00Z",
        "attempts": 1,
    }
    [event] = events
    assert event["event_type"] == "pr_mergeability_rebase"
    assert event["base_ref"] == "main"
    assert event["base_sha"] == BASE
    assert event["head_sha"] == HEAD
    assert "run the repository tests" in event["prompt"]
    assert "retain its lease" in event["prompt"]
    assert "Do not merge" in event["prompt"]


def test_conflict_authorizes_resolution_suite_push_and_review_rerequest(monkeypatch, events):
    _patch_api(monkeypatch, mergeable=False, behind=3)

    count, cursor = poller._check_own_mergeability(
        "o/r", "tok", "mimir-bot", {}, now=NOW,
    )

    assert count == 1
    assert cursor["42"]["reason"] == "conflicting"
    [event] = events
    assert event["event_type"] == "pr_mergeability_conflicting"
    assert BASE in event["prompt"]
    assert "collect every path with repo_unmerged" in event["prompt"]
    assert "base property, head property" in event["prompt"]
    assert "repo_test with no selectors" in event["prompt"]
    assert "repo_push" in event["prompt"]
    assert "re-request review" in event["prompt"]
    assert "do not merge" in event["prompt"]


def test_current_pr_is_noop_and_consumes_no_cycle_budget(monkeypatch, events):
    _patch_api(monkeypatch, mergeable=True, behind=0)
    budget = [1]

    count, cursor = poller._check_own_mergeability(
        "o/r", "tok", "mimir-bot", {}, now=NOW, attempt_budget=budget,
    )

    assert count == 0
    assert cursor == {}
    assert budget == [1]
    assert events == []


def test_pr_merged_between_listing_and_detail_is_not_actioned(monkeypatch, events):
    listed = _pr(mergeable=None)
    detail = {**_pr(mergeable=True), "state": "closed", "merged": True,
              "merged_at": "2026-07-31T11:59:00Z"}

    def fake_api(endpoint: str, token: str):
        if endpoint.endswith("/pulls/42"):
            return detail
        if "pulls?state=open" in endpoint:
            return [listed]
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", fake_api)

    count, cursor = poller._check_own_mergeability(
        "o/r", "tok", "mimir-bot", {}, now=NOW,
    )

    assert count == 0
    assert cursor == {}
    assert events == []


def test_unknown_mergeability_waits_without_acting(monkeypatch, events):
    _patch_api(monkeypatch, mergeable=None, behind=2)
    prior = {"42": {"head_sha": HEAD, "reason": "behind_base", "attempts": 1}}

    count, cursor = poller._check_own_mergeability(
        "o/r", "tok", "mimir-bot", prior, now=NOW,
    )

    assert count == 0
    assert cursor == prior
    assert events == []


def test_blocking_review_prevents_content_free_auto_rebase(monkeypatch, events):
    _patch_api(monkeypatch, mergeable=True, behind=2, reviews=[{
        "user": {"login": "reviewer"},
        "state": "CHANGES_REQUESTED",
        "submitted_at": "2026-07-31T11:00:00Z",
    }])

    count, cursor = poller._check_own_mergeability(
        "o/r", "tok", "mimir-bot", {}, now=NOW,
    )

    assert count == 0
    assert cursor == {}
    assert events == []


def test_blocking_review_prevents_conflict_resolution_push_turn(monkeypatch, events):
    _patch_api(monkeypatch, mergeable=False, behind=2, reviews=[{
        "user": {"login": "reviewer"},
        "state": "CHANGES_REQUESTED",
        "submitted_at": "2026-07-31T11:00:00Z",
    }])

    count, cursor = poller._check_own_mergeability(
        "o/r", "tok", "mimir-bot", {}, now=NOW,
    )

    assert count == 0
    assert cursor == {}
    assert events == []


def test_conflict_attempts_share_backoff_and_named_exhaustion(monkeypatch, events):
    _patch_api(monkeypatch, mergeable=False, behind=2)
    cap = poller.REVIEW_REQUEST_MAX_ATTEMPTS
    prior = {"42": {
        "head_sha": HEAD,
        "reason": "conflicting",
        "last_attempt_at": "2026-07-31T10:00:00Z",
        "attempts": cap,
    }}

    count, cursor = poller._check_own_mergeability(
        "o/r", "tok", "mimir-bot", prior, now=NOW,
    )

    assert count == 1
    assert cursor["42"]["attempts"] == cap + 1
    assert events == [{
        "signal": "pr_mergeability_rebase_gave_up",
        "repo": "o/r",
        "number": 42,
        "url": "https://github.com/o/r/pull/42",
        "head_sha": HEAD,
        "base_sha": BASE,
        "reason": "conflicting",
        "attempts": cap,
    }]


def test_attempt_budget_backs_off_then_reports_named_exhaustion(monkeypatch, events):
    _patch_api(monkeypatch, mergeable=True, behind=2)
    cap = poller.REVIEW_REQUEST_MAX_ATTEMPTS
    prior = {"42": {
        "head_sha": HEAD,
        "reason": "behind_base",
        "last_attempt_at": "2026-07-31T11:30:00Z",
        "attempts": cap,
    }}

    count, cursor = poller._check_own_mergeability(
        "o/r", "tok", "mimir-bot", prior, now=NOW,
    )
    assert count == 0
    assert cursor == prior
    assert events == []

    count, cursor = poller._check_own_mergeability(
        "o/r", "tok", "mimir-bot", prior, now=NOW + timedelta(minutes=31),
    )
    assert count == 1
    assert cursor["42"]["attempts"] == cap + 1
    assert events[0]["signal"] == "pr_mergeability_rebase_gave_up"
    assert events[0]["reason"] == "behind_base"


# --- chainlink #1433: per-tick reconciliation bound -------------------------


def _patch_many(monkeypatch, numbers, *, mergeable=False, behind=2):
    listed = [_pr(mergeable=None, number=n) for n in numbers]
    details = {n: _pr(mergeable=mergeable, number=n) for n in numbers}

    def fake_api(endpoint: str, token: str):
        if endpoint.endswith("/reviews"):
            return []
        if "/compare/" in endpoint:
            return {"behind_by": behind}
        if "pulls?state=open" in endpoint:
            return listed
        if "/pulls/" in endpoint:
            return details[int(endpoint.rsplit("/", 1)[1])]
        raise AssertionError(endpoint)

    monkeypatch.setattr(poller, "_gh_api", fake_api)


def test_truncated_mergeability_pass_preserves_skipped_cursor_entries(
    monkeypatch, events,
):
    """A skipped PR must keep its prior entry — the pass rebuilds its cursor
    from scratch, so an omitted key would rewind the rebase attempt count."""
    numbers = [41, 42, 43, 44, 45]
    _patch_many(monkeypatch, numbers)
    prior = {
        str(n): {
            "head_sha": HEAD,
            "base_sha": BASE,
            "reason": "conflicting",
            "attempts": 2,
            "last_attempt_at": "2026-07-30T12:00:00Z",
        }
        for n in numbers
    }

    spent = poller.TickBudget(deadline_seconds=0.0)
    count, cursor = poller._check_own_mergeability(
        "o/r", "token", "mimir-bot", prior, now=NOW,
        attempt_budget=[10], tick_budget=spent,
    )
    assert spent.truncated == {
        "mergeability": len(numbers) - poller.PR_RECONCILE_MIN_PER_PASS,
    }
    # Reconciled PRs got fresh entries; truncated ones were carried over verbatim
    # so their attempt count and retry floor survive the skip.
    assert sorted(cursor) == sorted(str(n) for n in numbers)
    for n in numbers[poller.PR_RECONCILE_MIN_PER_PASS:]:
        assert cursor[str(n)] == prior[str(n)]
    assert count == poller.PR_RECONCILE_MIN_PER_PASS


def test_unbudgeted_mergeability_pass_reconciles_every_pr(monkeypatch, events):
    """The bound is inert when the tick has time left (attempt budget raised so
    it is not the thing doing the limiting)."""
    numbers = [41, 42, 43]
    _patch_many(monkeypatch, numbers)
    fresh = poller.TickBudget(deadline_seconds=600.0)
    count, cursor = poller._check_own_mergeability(
        "o/r", "token", "mimir-bot", {}, now=NOW,
        attempt_budget=[10], tick_budget=fresh,
    )
    assert fresh.truncated == {}
    assert sorted(cursor) == [str(n) for n in numbers]
    assert count == len(numbers)


def test_spent_attempt_budget_still_costs_api_calls_without_the_tick_bound(
    monkeypatch, events,
):
    """Why the tick bound is needed here at all: an exhausted attempt budget
    `continue`s rather than breaking, so every remaining PR is still fetched."""
    numbers = [41, 42, 43, 44, 45]
    _patch_many(monkeypatch, numbers)
    calls: list[str] = []
    real = poller._gh_api
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda ep, tok: (calls.append(ep), real(ep, tok))[1],
    )
    poller._check_own_mergeability(
        "o/r", "token", "mimir-bot", {}, now=NOW, attempt_budget=[1],
    )
    detail_calls = [c for c in calls if c.endswith(tuple(f"/pulls/{n}" for n in numbers))]
    assert len(detail_calls) == len(numbers)  # all five, for one emission

    calls.clear()
    poller._check_own_mergeability(
        "o/r", "token", "mimir-bot", {}, now=NOW, attempt_budget=[1],
        tick_budget=poller.TickBudget(deadline_seconds=0.0),
    )
    bounded = [c for c in calls if c.endswith(tuple(f"/pulls/{n}" for n in numbers))]
    assert len(bounded) == poller.PR_RECONCILE_MIN_PER_PASS
