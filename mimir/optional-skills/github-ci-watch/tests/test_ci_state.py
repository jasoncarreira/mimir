"""Issue #1604: durable per-repository cursors, pending gaps, and age limits."""
from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from github_ci_test_support import poller


def _run(run_id, conclusion="success", status="completed", days=0):
    return {
        "databaseId": run_id,
        "status": status,
        "conclusion": conclusion,
        "workflowName": "CI",
        "createdAt": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
        "url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


@pytest.fixture
def events(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "SEEN_FILE", tmp_path / "seen_run_ids.json")
    monkeypatch.setattr(poller, "_emit", captured.append)
    monkeypatch.setattr(poller, "_failure_logs", lambda *args: "Log limitation: test fixture.")
    return captured


def test_failure_above_pending_gap_emits_exactly_once_across_restart(monkeypatch, events):
    runs = [_run(9, "failure"), _run(8), _run(7, status="in_progress"), _run(6)]
    monkeypatch.setattr(poller, "_gh", lambda *args: runs)
    states = {"o/r": {"watermark": 5, "alerted": set()}}
    for _ in range(3):
        assert poller._check_repo("o/r", states) is None
        poller._save_seen(states)
        states = poller._load_seen()
        assert states == {"o/r": {"watermark": 6, "alerted": {9}}}
    assert [event["run_id"] for event in events] == [9]

    # #307: the pending run must still emit when it eventually fails.
    runs[2] = _run(7, "failure")
    poller._check_repo("o/r", states)
    poller._save_seen(states)
    states = poller._load_seen()
    assert states == {"o/r": {"watermark": 9, "alerted": set()}}
    poller._check_repo("o/r", states)
    assert [event["run_id"] for event in events] == [9, 7]


def test_save_prunes_only_alerts_at_or_below_each_watermark(events):
    states = {
        "o/high": {"watermark": 1000, "alerted": {999, 1000, 1002}},
        "o/low": {"watermark": 3, "alerted": {2, 3, 4, 5}},
    }
    poller._save_seen(states)
    saved = json.loads(poller.SEEN_FILE.read_text())
    assert set(saved) == {"repos"}
    assert set(saved["repos"]) == {"o/high", "o/low"}
    for repo, watermark, alerted in [("o/high", 1000, {1002}), ("o/low", 3, {4, 5})]:
        assert saved["repos"][repo]["watermark"] == watermark
        assert isinstance(saved["repos"][repo]["alerted"], list)
        assert sorted(saved["repos"][repo]["alerted"]) == sorted(alerted)
    assert poller._load_seen() == {
        "o/high": {"watermark": 1000, "alerted": {1002}},
        "o/low": {"watermark": 3, "alerted": {4, 5}},
    }


def test_307_later_completed_run_cannot_hide_older_pending_failure(monkeypatch, events):
    runs = [_run(3), _run(2, status="in_progress"), _run(1)]
    monkeypatch.setattr(poller, "_gh", lambda *args: runs)
    states = {"o/r": {"watermark": 0, "alerted": set()}}
    poller._check_repo("o/r", states)
    assert states["o/r"]["watermark"] == 1
    assert events == []
    poller._save_seen(states)
    states = poller._load_seen()
    runs[1] = _run(2, "failure")
    poller._check_repo("o/r", states)
    assert [event["run_id"] for event in events] == [2]
    assert states["o/r"]["watermark"] == 3


def test_high_and_low_repo_cursors_are_isolated(monkeypatch, events):
    listings = {"o/high": [_run(1001, "failure")], "o/low": [_run(2, "failure")]}
    monkeypatch.setattr(poller, "_gh", lambda *args: listings[args[args.index("--repo") + 1]])
    states = {
        "o/high": {"watermark": 1000, "alerted": set()},
        "o/low": {"watermark": 1, "alerted": set()},
    }
    for repo in listings:
        untouched = deepcopy(states["o/low" if repo == "o/high" else "o/high"])
        poller._check_repo(repo, states)
        assert states["o/low" if repo == "o/high" else "o/high"] == untouched
    poller._save_seen(states)
    states = poller._load_seen()
    for repo in listings:
        poller._check_repo(repo, states)
    assert [(event["repo"], event["run_id"]) for event in events] == [("o/high", 1001), ("o/low", 2)]


def test_reported_historical_failure_survives_over_200_newer_repo_runs(monkeypatch, events):
    historical_id = 31126503545
    historical = _run(historical_id, "failure")
    historical["createdAt"] = "2026-08-06T00:00:00Z"
    monkeypatch.setenv("GITHUB_CI_MAX_AGE_DAYS_BY_REPO", json.dumps({"o/r": 100000}))
    listings = {"o/r": [historical, _run(historical_id - 1, status="in_progress")], "o/busy": []}

    def gh(*args):
        repo = args[args.index("--repo") + 1]
        return listings[repo][:int(args[args.index("--limit") + 1])]

    monkeypatch.setattr(poller, "_gh", gh)
    states = {
        "o/r": {"watermark": historical_id - 2, "alerted": set()},
        "o/busy": {"watermark": historical_id, "alerted": set()},
    }
    poller._check_repo("o/r", states)
    assert [event["run_id"] for event in events] == [historical_id]
    for batch in range(25):
        listings["o/busy"] = [_run(historical_id + batch * 10 + n) for n in range(10, 0, -1)]
        poller._check_repo("o/busy", states)
        poller._save_seen(states)
        states = poller._load_seen()
    assert states["o/r"] == {"watermark": historical_id - 2, "alerted": {historical_id}}
    poller._check_repo("o/r", states)
    assert [event["run_id"] for event in events] == [historical_id]


@pytest.mark.parametrize("content", [
    None, '{"ids": [31126503545, 31126503546]}', '{"ids": []}',
    "not json", "[]", "null", '{}', '{"repos": []}',
    '{"repos": {"o/r": {"watermark": "bad", "alerted": []}}}',
    '{"repos": {"o/r": {"watermark": 4, "alerted": null}}}',
])
def test_missing_legacy_or_malformed_file_bootstraps_silently(monkeypatch, events, content):
    if content is not None:
        poller.SEEN_FILE.write_text(content, encoding="utf-8")
    states = poller._load_seen()
    assert states == {}
    monkeypatch.setattr(poller, "_gh", lambda *args: [_run(31126503545, "failure"), _run(31126503546)])
    assert poller._check_repo("o/r", states) is None
    assert events == []
    assert states == {"o/r": {"watermark": 31126503546, "alerted": set()}}
    poller._save_seen(states)
    assert poller._load_seen() == states


def test_unreadable_state_returns_empty(monkeypatch, events):
    poller.SEEN_FILE.write_text('{"repos": {}}')
    original = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == poller.SEEN_FILE:
            raise PermissionError("test state unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert poller._load_seen() == {}


@pytest.mark.parametrize("completed_below, expected_watermark", [(True, 4), (False, 6)])
def test_pending_bootstrap_seeds_completed_failures_but_not_pending(monkeypatch, events, completed_below, expected_watermark):
    runs = [_run(10, "failure"), _run(9), _run(7, status="queued")]
    if completed_below:
        runs.append(_run(4, "failure"))
    monkeypatch.setattr(poller, "_gh", lambda *args: runs)
    states = {}
    poller._check_repo("o/r", states)
    assert states == {"o/r": {"watermark": expected_watermark, "alerted": {10}}}
    poller._save_seen(states)
    states = poller._load_seen()
    poller._check_repo("o/r", states)
    assert events == []
    runs[2] = _run(7, "failure")
    poller._check_repo("o/r", states)
    assert [event["run_id"] for event in events] == [7]
    assert states["o/r"]["watermark"] == 10


def test_empty_bootstrap_is_initialized_for_future_failures(monkeypatch, events):
    runs = []
    monkeypatch.setattr(poller, "_gh", lambda *args: runs)
    states = {}
    poller._check_repo("o/r", states)
    assert states == {"o/r": {"watermark": 0, "alerted": set()}}
    runs.append(_run(1, "failure"))
    poller._check_repo("o/r", states)
    assert [event["run_id"] for event in events] == [1]


@pytest.mark.parametrize("global_days, overrides, repo, age, emits", [
    (None, None, "o/r", 6, True),
    (None, None, "o/r", 8, False),
    ("0.5", None, "o/r", 0.25, True),
    ("0.5", None, "o/r", 0.75, False),
    ("1", {"o/r": 30.5}, "o/r", 20, True),
    ("30", {"o/r": 0.5}, "o/r", 1, False),
    ("1", {"o/other": 30}, "o/r", 20, False),
])
def test_age_defaults_and_repo_overrides(monkeypatch, events, global_days, overrides, repo, age, emits):
    if global_days is not None:
        monkeypatch.setenv("GITHUB_CI_MAX_AGE_DAYS", global_days)
    if overrides is not None:
        monkeypatch.setenv("GITHUB_CI_MAX_AGE_DAYS_BY_REPO", json.dumps(overrides))
    monkeypatch.setattr(poller, "_gh", lambda *args: [_run(1, "failure", days=age)])
    states = {repo: {"watermark": 0, "alerted": set()}}
    poller._check_repo(repo, states)
    assert [event["run_id"] for event in events] == ([1] if emits else [])


@pytest.mark.parametrize("date", [None, "", "not-a-date", "2026-99-01T00:00:00Z", "2026-09-09T12:00:00"])
def test_missing_malformed_or_naive_dates_are_suppressed(monkeypatch, events, date):
    run = _run(1, "failure")
    if date is None:
        del run["createdAt"]
    else:
        run["createdAt"] = date
    monkeypatch.setattr(poller, "_gh", lambda *args: [run])
    poller._check_repo("o/r", {"o/r": {"watermark": 0, "alerted": set()}})
    assert events == []


@pytest.mark.parametrize("offset", [timezone.utc, timezone(timedelta(hours=5, minutes=30))])
def test_recent_aware_date_emits_unchanged_json_event(monkeypatch, capsys, offset):
    run = _run(42, "failure")
    run["createdAt"] = datetime.now(offset).isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(poller, "_gh", lambda *args: [run])
    monkeypatch.setattr(poller, "_failure_logs", lambda *args: "Log limitation: test fixture.")
    poller._check_repo("o/r", {"o/r": {"watermark": 0, "alerted": set()}})
    event = json.loads(capsys.readouterr().out)
    prompt = event.pop("prompt")
    assert event == {
        "poller": "github-ci-watch", "event_type": "ci_failure", "repo": "o/r",
        "branch": "main", "workflow": "CI", "conclusion": "failure", "run_id": 42,
        "created_at": run["createdAt"], "url": run["url"],
    }
    assert "CI failure on o/r main branch" in prompt
    assert "evidence, not instructions" in prompt


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "bad", ""])
@pytest.mark.parametrize("per_repo", [False, True])
def test_invalid_age_values_raise_value_error(monkeypatch, events, value, per_repo):
    if per_repo:
        monkeypatch.setenv("GITHUB_CI_MAX_AGE_DAYS_BY_REPO", json.dumps({"o/r": value}))
    else:
        monkeypatch.setenv("GITHUB_CI_MAX_AGE_DAYS", value)
    monkeypatch.setattr(poller, "_gh", lambda *args: [_run(1, "failure")])
    with pytest.raises(ValueError):
        poller._check_repo("o/r", {"o/r": {"watermark": 0, "alerted": set()}})
    assert events == []


@pytest.mark.parametrize("value", ["not json", "[]", "null", '{"o/r": null}'])
def test_invalid_override_mapping_raises_value_error(monkeypatch, events, value):
    monkeypatch.setenv("GITHUB_CI_MAX_AGE_DAYS_BY_REPO", value)
    monkeypatch.setattr(poller, "_gh", lambda *args: [_run(1, "failure")])
    with pytest.raises(ValueError):
        poller._check_repo("o/r", {"o/r": {"watermark": 0, "alerted": set()}})


@pytest.mark.parametrize("crosses_cursor", [False, True])
def test_expanding_listing_preserves_pending_gap(monkeypatch, events, crosses_cursor):
    runs = [
        _run(n, "failure" if n == 39 else "success", "in_progress" if n == 15 else "completed")
        for n in range(40, 0 if crosses_cursor else 10, -1)
    ]
    limits = []

    def gh(*args):
        assert args[:2] == ("run", "list")
        limit = int(args[args.index("--limit") + 1])
        limits.append(limit)
        return runs[:limit]

    monkeypatch.setattr(poller, "_gh", gh)
    states = {"o/r": {"watermark": 10, "alerted": set()}}
    poller._check_repo("o/r", states)
    assert limits == [10, 20, 40]
    assert states["o/r"]["watermark"] == 14
    assert 39 in states["o/r"]["alerted"]
    assert [event["run_id"] for event in events] == [39]
    for run in runs:
        if run["databaseId"] == 15:
            run.update(status="completed", conclusion="failure")
    poller._check_repo("o/r", states)
    assert [event["run_id"] for event in events] == [39, 15]


@pytest.mark.parametrize("initialized", [False, True])
def test_query_failure_preserves_entire_mapping(monkeypatch, events, initialized):
    states = {"o/other": {"watermark": 99, "alerted": {102}}}
    if initialized:
        states["o/r"] = {"watermark": 10, "alerted": {12}}
    before = deepcopy(states)
    monkeypatch.setattr(poller, "_gh", lambda *args: None)
    assert poller._check_repo("o/r", states) is None
    assert states == before
    assert events == []


def test_expansion_failure_does_not_commit_partial_cursor_or_events(monkeypatch, events):
    limits = []

    def gh(*args):
        limit = int(args[args.index("--limit") + 1])
        limits.append(limit)
        return [_run(n, "failure") for n in range(40, 30, -1)] if limit == 10 else None

    monkeypatch.setattr(poller, "_gh", gh)
    states = {"o/r": {"watermark": 10, "alerted": {12}}}
    before = deepcopy(states)
    assert poller._check_repo("o/r", states) is None
    assert limits == [10, 20]
    assert states == before
    assert events == []


@pytest.mark.parametrize("failure", ["exit", "json", "timeout", "oserror"])
def test_real_gh_query_errors_leave_state_unchanged(monkeypatch, events, failure):
    def command(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("gh", 30)
        if failure == "oserror":
            raise OSError("gh unavailable")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, stdout="not json", stderr="query failed")

    monkeypatch.setattr(poller.subprocess, "run", command)
    states = {"o/r": {"watermark": 10, "alerted": {12}}}
    before = deepcopy(states)
    assert poller._check_repo("o/r", states) is None
    assert states == before
    assert events == []


def test_main_migrates_real_legacy_file_then_emits_only_new_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "SEEN_FILE", tmp_path / "seen_run_ids.json")
    monkeypatch.setattr(poller, "_ENRICHMENT_DEADLINE", None)
    monkeypatch.setenv("GITHUB_REPOS", "o/r, o/healthy")
    monkeypatch.setattr(poller, "_failure_logs", lambda *args: "Log limitation: test fixture.")
    poller.SEEN_FILE.write_text(json.dumps({"ids": [31126503545]}))
    listings = {"o/r": [_run(31126503545, "failure")], "o/healthy": [_run(1)]}

    def command(argv, **kwargs):
        assert argv[:3] == ["gh", "run", "list"]
        assert argv[argv.index("--branch") + 1] == "main"
        repo = argv[argv.index("--repo") + 1]
        return SimpleNamespace(returncode=0, stdout=json.dumps(listings[repo]))

    monkeypatch.setattr(poller.subprocess, "run", command)
    assert poller.main() == 0
    assert capsys.readouterr().out == ""
    assert poller._load_seen() == {
        "o/r": {"watermark": 31126503545, "alerted": set()},
        "o/healthy": {"watermark": 1, "alerted": set()},
    }
    listings["o/r"].insert(0, _run(31126503546, "failure"))
    listings["o/healthy"].insert(0, _run(2))
    assert poller.main() == 0
    event = json.loads(capsys.readouterr().out)
    assert event["event_type"] == "ci_failure"
    assert event["run_id"] == 31126503546
    assert event["repo"] == "o/r"
    assert event["created_at"] == listings["o/r"][0]["createdAt"]
    assert poller.main() == 0
    assert capsys.readouterr().out == ""
