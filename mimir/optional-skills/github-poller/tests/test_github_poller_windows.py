from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from github_poller_test_support import poller


@pytest.mark.parametrize("remaining", [0.0, 600.0])
@pytest.mark.parametrize("data, held", [
    (None, True), (poller._GH_API_BUDGET_REFUSED, True), ([], False),
])
def test_refused_window(monkeypatch, remaining, data, held):
    budget = poller.TickBudget()
    monkeypatch.setattr(budget, "hard_remaining", lambda: remaining)
    assert poller._refused_window(budget, data, "test_window") is held
    assert budget.hard_truncated is held
    assert budget.truncated == ({"test_window": 1} if held else {})


def test_refused_window_without_budget():
    assert not poller._refused_window(None, None, "test_window")
    assert not poller._refused_window(None, poller._GH_API_BUDGET_REFUSED, "test_window")


@pytest.mark.parametrize("check, window", [
    (poller._collect_issue_comment_context, "issue_comment_context_window"),
    (poller._check_issue_comments, "issue_comments_window"),
])
@pytest.mark.parametrize("data, held", [
    (None, True), (poller._GH_API_BUDGET_REFUSED, True), ([], False),
])
def test_issue_comment_window_guards(monkeypatch, check, window, data, held):
    budget = poller.TickBudget(hard_deadline_seconds=600)
    monkeypatch.setattr(poller, "_gh_api", lambda *args: data)
    check("o/r", "2026-08-23T10:00:00Z", "tok", "bot", tick_budget=budget)
    assert not budget.hard_exhausted()
    assert budget.hard_truncated is held
    assert budget.truncated == ({window: 1} if held else {})


@pytest.mark.parametrize("failed_endpoint", [
    "issues?", "pulls?state=open&sort=created", "pulls/comments?",
    "pulls?state=open&sort=updated", "issues/comments?", None,
])
@pytest.mark.parametrize("failure", ["error", "timeout", "invalid_json"])
def test_failed_window_holds_persisted_watermark(
    monkeypatch, tmp_path, failed_endpoint, failure,
):
    previous = "2026-08-23T10:00:00Z"
    current = "2026-08-23T12:00:00Z"
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "CURSOR_FILE", tmp_path / "cursor.json")
    poller._save_cursor({"last_checked": previous})
    monkeypatch.setenv("GITHUB_REPOS", "o/r")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "bot")
    monkeypatch.setattr(poller, "_resolve_token", lambda: "tok")
    monkeypatch.setattr(poller, "_utc_now_iso", lambda: current)
    budget = poller.TickBudget(hard_deadline_seconds=600)
    monkeypatch.setattr(poller, "TickBudget", lambda **kwargs: budget)
    failed_calls = []

    def api_run(argv, **kwargs):
        endpoint = argv[2].removeprefix("repos/o/r/")
        # Fail only the first matching call: a successful later retry must not
        # erase the earlier incomplete context/window.
        if failed_endpoint and endpoint.startswith(failed_endpoint) and not failed_calls:
            failed_calls.append(endpoint)
            if failure == "timeout":
                raise poller.subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return SimpleNamespace(
                returncode=1 if failure == "error" else 0,
                stdout="not json", stderr="API failure",
            )
        return SimpleNamespace(returncode=0, stdout=json.dumps([]), stderr="")

    monkeypatch.setattr(poller.subprocess, "run", api_run)
    poller.main()

    assert bool(failed_calls) is (failed_endpoint is not None)
    assert not budget.hard_exhausted()
    assert poller._load_cursor()["last_checked"] == (
        previous if failed_endpoint else current
    )
