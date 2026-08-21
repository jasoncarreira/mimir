from __future__ import annotations

from datetime import datetime, timedelta, timezone

from github_poller_test_support import poller
import pytest


SINCE = "2026-08-16T10:00:00Z"
HEAD = "a" * 40


@pytest.fixture
def captured_emits(monkeypatch: pytest.MonkeyPatch, tmp_path) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(
        poller, "_emit", lambda _prompt, **extras: events.append(extras),
    )
    return events


def _pr(author: str = "mimir-bot", *, state: str = "open", head: str = HEAD) -> dict:
    return {
        "number": 42,
        "state": state,
        "merged": False,
        "merged_at": None,
        "title": "Fix CI",
        "html_url": "https://github.com/o/r/pull/42",
        "user": {"login": author},
        "head": {"sha": head, "ref": "worklink/42", "repo": {"full_name": "o/r"}},
        "base": {"sha": "b" * 40, "ref": "main"},
    }


def _check(conclusion: str = "failure", *, completed_at: str = "2026-08-16T10:01:00Z") -> dict:
    return {
        "id": 99,
        "name": "tests",
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": completed_at,
        "html_url": "https://github.com/o/r/runs/99",
        "details_url": "https://github.com/o/r/runs/99/logs",
        "external_id": "job-99",
    }


def _api(pr: dict, checks: list[dict]):
    def fake(endpoint: str, _token: str):
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return [pr]
        if endpoint == "repos/o/r/pulls/42":
            return pr
        if endpoint == f"repos/o/r/commits/{pr['head']['sha']}/check-runs?per_page=100":
            return {"check_runs": checks}
        raise AssertionError(endpoint)

    return fake


def test_owned_failure_emits_bound_remediation(monkeypatch, captured_emits):
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [_check()]))

    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {},
    )

    assert count == 1
    event = captured_emits[0]
    assert event["event_type"] == "pr_ci_failure"
    assert event["repo"] == "o/r"
    assert event["number"] == 42
    assert event["head_sha"] == HEAD
    assert event["author"] == "mimir-bot"
    assert event["failed_checks"] == [{
        "id": 99,
        "name": "tests",
        "conclusion": "failure",
        "url": "https://github.com/o/r/runs/99",
        "details_url": "https://github.com/o/r/runs/99/logs",
        "external_id": "job-99",
    }]
    assert cursor["42"]["delivery_key"] == event["delivery_key"]


def test_external_failure_routes_to_signal_only(monkeypatch, captured_emits):
    signals: list[tuple[str, dict]] = []
    monkeypatch.setattr(poller, "_gh_api", _api(_pr("contributor"), [_check()]))
    monkeypatch.setattr(
        poller, "_emit_signal", lambda signal, **extra: signals.append((signal, extra)),
    )

    count, _cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {},
    )

    assert count == 1
    assert captured_emits == []
    assert signals[0][0] == "pr_ci_failure_external"
    assert signals[0][1]["author"] == "contributor"


def test_same_failure_is_deduped_during_overlapping_poll(monkeypatch, captured_emits):
    now = datetime(2026, 8, 16, 10, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [_check()]))
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {}, now=now,
    )
    assert count == 1

    count, cursor2 = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor,
        now=now + timedelta(seconds=1),
    )
    assert count == 0
    assert cursor2["42"] == cursor["42"]
    assert len(captured_emits) == 1


def test_unacknowledged_delivery_retries_but_receipt_dedupes(monkeypatch, captured_emits):
    now = datetime(2026, 8, 16, 10, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [_check()]))
    _, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {}, now=now,
    )
    count, retried = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor,
        now=now + poller.CI_DELIVERY_RETRY_INTERVAL,
    )
    assert count == 1

    monkeypatch.setattr(poller, "_delivery_receipt_exists", lambda _key: True)
    count, _ = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", retried,
        now=now + timedelta(hours=1),
    )
    assert count == 0
    assert len(captured_emits) == 2


def test_closed_and_green_races_terminate_and_clear_cursor(monkeypatch, captured_emits):
    prior = {"42": {"head_sha": HEAD, "delivery_key": "old", "emitted_at": SINCE}}
    closed = _pr(state="closed")
    monkeypatch.setattr(poller, "_gh_api", _api(closed, [_check()]))
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", prior,
    )
    assert count == 0
    assert "42" not in cursor

    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [_check("success")]))
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", prior,
    )
    assert count == 0
    assert "42" not in cursor
    assert captured_emits == []


def test_check_api_failure_preserves_cursor(monkeypatch, captured_emits):
    prior = {"42": {"head_sha": HEAD, "delivery_key": "old", "emitted_at": SINCE}}

    def fake(endpoint: str, _token: str):
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return [_pr()]
        if endpoint == "repos/o/r/pulls/42":
            return _pr()
        return None

    monkeypatch.setattr(poller, "_gh_api", fake)
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", prior,
    )
    assert count == 0
    assert cursor["42"] == prior["42"]
    assert cursor["_last_checked"] == SINCE
    assert captured_emits == []


def test_check_api_failure_does_not_advance_new_failure_window(
    monkeypatch, captured_emits,
):
    def failed_api(endpoint: str, _token: str):
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return [_pr()]
        if endpoint == "repos/o/r/pulls/42":
            return _pr()
        return None

    monkeypatch.setattr(poller, "_gh_api", failed_api)
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {},
    )
    assert count == 0
    assert cursor == {"_last_checked": SINCE}

    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [_check()]))
    count, _ = poller._check_pr_ci_failures(
        "o/r", "2026-08-16T10:10:00Z", "token", "mimir-bot", cursor,
    )
    assert count == 1
    assert len(captured_emits) == 1


def test_old_red_head_is_baselined_without_later_retry(monkeypatch, captured_emits):
    old_check = _check(completed_at="2026-08-16T09:00:00Z")
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [old_check]))
    first_now = datetime(2026, 8, 16, 10, 2, tzinfo=timezone.utc)

    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {}, now=first_now,
    )
    assert count == 0
    assert cursor["42"]["baseline"] is True

    count, _ = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor,
        now=first_now + timedelta(hours=1),
    )
    assert count == 0
    assert captured_emits == []
