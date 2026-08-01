from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import poller


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


def test_conflict_escalates_paths_and_base_without_push(monkeypatch, events):
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
    assert "repo_rebase_abort" in event["prompt"]
    assert "Never resolve, complete, or push" in event["prompt"]


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
