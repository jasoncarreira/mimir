"""Offline log reachability through the real remediation emission paths."""
from __future__ import annotations

import json
import subprocess
import unicodedata
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from github_poller_test_support import poller

HEAD = "a" * 40
NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
SINCE = "2026-09-08T10:00:00Z"


@pytest.fixture
def ci(monkeypatch, tmp_path):
    pr = {
        "number": 42, "state": "open", "title": "Fix tests",
        "html_url": "https://github.com/o/r/pull/42",
        "user": {"login": "bot"},
        "head": {"sha": HEAD, "ref": "fix", "repo": {"full_name": "o/r"}},
        "base": {"sha": "b" * 40, "ref": "main"},
    }
    check = {
        "id": 99, "head_sha": HEAD, "name": "\x1b[31mtests\x1b[0m\x00", "status": "completed",
        "conclusion": "failure", "completed_at": "2026-09-08T11:00:00Z",
        "details_url": "https://github.com/o/r/actions/runs/50/job/101",
    }
    job = {
        "id": 101, "run_id": 50, "head_sha": HEAD, "conclusion": "failure",
        "name": "\x1b[31mpytest\x1b[0m\x00\u202e",
    }
    run = {
        "id": 50, "head_sha": HEAD, "repository": {"full_name": "o/r"},
        "head_repository": {"full_name": "o/r"},
    }
    review = {"user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED",
              "submitted_at": "2026-09-08T11:00:00Z", "commit_id": HEAD}
    payload = (
        b"useful output\n" * 6000 + b"\x1b]0;" + b"hidden" * 20000
        + b"\x1b\\\x1b[31mFAILED sentinel\x1b[0m\x00\x08\r\x7f\t\n"
        + "\u009dhidden\u009c\u202e".encode() + b"password=x ghp_ci_secret\n"
    )
    calls = []
    log_timeouts = []

    def gh(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"]["GH_TOKEN"] == "test-token"
        if "--allow-escape-sequences" in argv:
            log_timeouts.append(kwargs.get("timeout"))
            assert argv == ["gh", "api", "--allow-escape-sequences", "repos/o/r/actions/jobs/101/logs"]
            kwargs["stdout"].write(payload)
            return SimpleNamespace(returncode=0, stderr=b"")
        endpoint = argv[2]
        if endpoint.startswith("repos/o/r/pulls?"):
            data = [pr]
        elif endpoint == "repos/o/r/pulls/42":
            data = pr
        elif endpoint == "repos/o/r/pulls/42/reviews":
            data = [review]
        elif endpoint == f"repos/o/r/commits/{HEAD}":
            data = {"commit": {"committer": {"date": "2026-09-08T09:00:00Z"}}}
        elif endpoint == f"repos/o/r/commits/{HEAD}/check-runs?per_page=100":
            data = {"check_runs": [check], "total_count": 1}
        elif endpoint == "repos/o/r/actions/jobs/101":
            data = job
        elif endpoint == "repos/o/r/actions/runs/50":
            data = run
        else:
            pytest.fail(f"unexpected API request: {argv}")
        return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "POLLER_NAME", "github-activity")
    monkeypatch.setenv("GH_TOKEN", "ambient-token")
    monkeypatch.setattr(poller.subprocess, "run", gh)
    return SimpleNamespace(pr=pr, check=check, job=job, run=run, review=review,
                           calls=calls, log_timeouts=log_timeouts)


@pytest.mark.parametrize("path", ["changes_requested", "stale", "rearmed", "ci_failure"])
def test_remediation_log_read_reachable(ci, capsys, path):
    # Use the actual installed-skill declaration, not an invented test grant.
    # Removing approved_urls/fetch_url must kill this positive reachability test.
    if path == "changes_requested":
        poller._check_pr_reviews("o/r", SINCE, "test-token", "bot")
    elif path in {"stale", "rearmed"}:
        prior = {} if path == "stale" else {"42": {
            "head_sha": HEAD, "attempts": poller.REVIEW_REQUEST_MAX_ATTEMPTS + 1,
            "last_reminded_at": "2026-09-06T12:00:00Z",
        }}
        poller._check_own_changes_requested("o/r", "test-token", "bot", prior, now=NOW)
    else:
        poller._check_pr_ci_failures("o/r", SINCE, "test-token", "bot", {}, now=NOW)
    event = json.loads(capsys.readouterr().out)
    prompt = event["prompt"]
    assert event["head_sha"] == HEAD and event["repo"] == "o/r"
    if path == "ci_failure":
        assert event["failed_checks"][0]["name"] == "tests"
    assert "untrusted third-party content" in prompt
    assert f"CI evidence for o/r at immutable head {HEAD}" in prompt
    evidence, _ = json.JSONDecoder().raw_decode(prompt.split("before changing anything.\n", 1)[1])
    assert "Job 101 (pytest), run 50:" in evidence
    assert "FAILED sentinel" in evidence and "hidden" not in evidence
    assert all(c in "\t\n" or unicodedata.category(c) not in {"Cc", "Cf"} for c in evidence)
    assert len(evidence.encode()) < 2200
    assert "test-token" not in prompt
    assert "password=x" not in prompt and "ghp_ci_secret" not in prompt
    assert "[REDACTED]" in prompt
    assert sum("--allow-escape-sequences" in call for call in ci.calls) == 1


@pytest.mark.parametrize("removed", [
    "approved_urls", "fetch_url", "job_scope", "invalid_urls", "prefix_boundary",
    "job_metadata_scope", "run_metadata_scope", "other_poller",
])
def test_capture_requires_declared_authority(ci, monkeypatch, tmp_path, removed):
    manifest = json.loads(poller.POLLER_MANIFEST.read_text())
    authority = manifest["pollers"][0]["authority"]
    if removed == "fetch_url":
        authority["capabilities"].remove("fetch_url")
    elif removed == "job_scope":
        authority["approved_urls"] = [f"https://api.github.com/repos/o/r/commits/{HEAD}/"]
    elif removed == "invalid_urls":
        authority["approved_urls"] = {"https://api.github.com/repos/": True}
    elif removed == "prefix_boundary":
        authority["approved_urls"] = ["https://api.github.com/repo"]
    elif removed == "other_poller":
        manifest["pollers"].insert(0, {
            **manifest["pollers"][0], "name": "other-poller", "authority": dict(authority),
        })
        authority["approved_urls"] = []
    elif removed in {"job_metadata_scope", "run_metadata_scope"}:
        authority["approved_urls"] = [
            f"https://api.github.com/repos/o/r/commits/{HEAD}/",
            "https://api.github.com/repos/o/r/actions/jobs/101/logs"
            if removed == "job_metadata_scope" else "https://api.github.com/repos/o/r/actions/jobs/101",
        ]
        if removed == "job_metadata_scope":
            authority["approved_urls"].append("https://api.github.com/repos/o/r/actions/runs/50")
    else:
        authority.pop("approved_urls", None)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(poller, "POLLER_MANIFEST", path)
    result = poller._remediation_logs("o/r", ci.pr, "test-token")
    assert "authority" in result and "FAILED sentinel" not in result
    assert not any("/actions/" in " ".join(call) for call in ci.calls)
    if removed not in {"job_scope", "job_metadata_scope", "run_metadata_scope"}:
        assert ci.calls == []  # Discovery is an authorized read too, not just the download.


@pytest.mark.parametrize("target", [
    "third_party", "other_repo", "userinfo", "query", "encoded_path",
    "check_head", "job_head", "run_head", "run_repo", "run_head_repo", "job_run", "fork",
    "job_id", "run_id", "check_status", "job_conclusion", "check_conclusion",
])
def test_rejects_unbound_log_targets(ci, target):
    urls = {
        "third_party": "https://example.com/o/r/actions/runs/50/job/101",
        "other_repo": "https://github.com/o/other/actions/runs/50/job/101",
        "userinfo": "https://github.com@evil.test/o/r/actions/runs/50/job/101",
        "query": "https://github.com/o/r/actions/runs/50/job/101?redirect=evil",
        "encoded_path": "https://github.com/o/r/actions/runs/50/job/%31%30%31",
    }
    if target in urls:
        ci.check["details_url"] = urls[target]
    elif target == "check_head":
        ci.check["head_sha"] = "c" * 40
    elif target == "job_head":
        ci.job["head_sha"] = "c" * 40
    elif target == "run_head":
        ci.run["head_sha"] = "c" * 40
    elif target == "run_repo":
        ci.run["repository"]["full_name"] = "o/other"
    elif target == "run_head_repo":
        ci.run["head_repository"]["full_name"] = "o/fork"
    elif target == "job_run":
        ci.job["run_id"] = 51
    elif target == "job_id":
        ci.job["id"] = 102
    elif target == "run_id":
        ci.run["id"] = 51
    elif target == "check_status":
        ci.check["status"] = "in_progress"
    elif target == "job_conclusion":
        ci.job["conclusion"] = "success"
    elif target == "check_conclusion":
        ci.check["conclusion"] = "success"
    else:
        ci.pr["head"]["repo"]["full_name"] = "o/fork"
    result = poller._remediation_logs("o/r", ci.pr, "test-token")
    assert "limitation" in result and "FAILED sentinel" not in result
    assert not any("--allow-escape-sequences" in call for call in ci.calls)


@pytest.mark.parametrize("failure", ["http", "timeout", "empty"])
def test_capture_failure_keeps_remediation_without_private_diagnostics(ci, monkeypatch, capsys, failure):
    original = poller.subprocess.run

    def gh(argv, **kwargs):
        if "--allow-escape-sequences" not in argv:
            return original(argv, **kwargs)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if failure == "http":
            kwargs["stdout"].write(b"partial response is not a successful log")
        return SimpleNamespace(returncode=1 if failure == "http" else 0,
                               stderr=b"HTTP 403 private signed-url token")

    monkeypatch.setattr(poller.subprocess, "run", gh)
    poller._check_pr_reviews("o/r", SINCE, "test-token", "bot")
    prompt = json.loads(capsys.readouterr().out)["prompt"]
    assert "log limitation" in prompt and "private" not in prompt
    assert "signed-url" not in prompt
    assert "partial response" not in prompt


def test_exhausted_budget_does_not_fetch(ci, monkeypatch):
    # Exercise the actual metadata-call budget too; a partial budget stub would
    # fail with AttributeError when an equivalent early-out is removed.
    monkeypatch.setattr(poller, "_ACTIVE_TICK_BUDGET", poller.TickBudget(hard_deadline_seconds=0))
    assert "limitation" in poller._remediation_logs("o/r", ci.pr, "test-token")
    assert ci.calls == []


def test_external_review_does_not_capture(ci, capsys):
    ci.pr["user"]["login"] = "contributor"
    poller._check_pr_reviews("o/r", SINCE, "test-token", "bot")
    assert "FAILED sentinel" not in json.loads(capsys.readouterr().out)["prompt"]
    assert not any("/actions/" in " ".join(call) for call in ci.calls)


@pytest.mark.parametrize("state", ["APPROVED", "COMMENTED"])
def test_nonblocking_review_does_not_capture(ci, capsys, state):
    ci.review["state"] = state
    poller._check_pr_reviews("o/r", SINCE, "test-token", "bot")
    assert "FAILED sentinel" not in json.loads(capsys.readouterr().out)["prompt"]
    assert not any("/actions/" in " ".join(call) for call in ci.calls)


@pytest.mark.parametrize("repo,sha", [
    ("o/r/extra", HEAD), ("o/..", HEAD), ("o/r", "main"),
])
def test_invalid_binding_does_not_start_discovery(ci, monkeypatch, repo, sha):
    ci.pr["head"]["repo"]["full_name"] = repo
    ci.pr["head"]["sha"] = sha
    calls = []
    # Do not manufacture a successful GitHub response for an invalid identifier.
    monkeypatch.setattr(poller, "_gh_api", lambda *args: calls.append(args))
    assert "limitation" in poller._remediation_logs(repo, ci.pr, "test-token")
    assert calls == []


@pytest.mark.parametrize("remaining,expected", [(2.0, 2.0), (60.0, 15.0)])
def test_log_subprocess_timeout_is_bounded(ci, monkeypatch, remaining, expected):
    budget = poller.TickBudget()
    monkeypatch.setattr(budget, "hard_remaining", lambda: remaining)
    monkeypatch.setattr(poller, "_ACTIVE_TICK_BUDGET", budget)
    assert "FAILED sentinel" in poller._remediation_logs("o/r", ci.pr, "test-token")
    assert ci.log_timeouts == [expected]


@pytest.mark.parametrize("metadata,limit", [("job", 200), ("check", 1024)])
def test_metadata_names_are_bounded(ci, capsys, metadata, limit):
    getattr(ci, metadata)["name"] = "x" * 10000
    poller._check_pr_ci_failures("o/r", SINCE, "test-token", "bot", {}, now=NOW)
    prompt = json.loads(capsys.readouterr().out)["prompt"]
    assert "x" * limit in prompt
    assert "x" * (limit + 1) not in prompt


@pytest.mark.parametrize("jobs", ["distinct", "duplicate"])
def test_capture_is_bounded_and_deduplicated(ci, monkeypatch, jobs):
    ids = [101, 102, 103, 104] if jobs == "distinct" else [101] * 4
    checks = [dict(ci.check, details_url=f"https://github.com/o/r/actions/runs/50/job/{job}") for job in ids]
    captures = []

    def metadata(endpoint, token):
        if "/jobs/" in endpoint:
            return dict(ci.job, id=int(endpoint.rsplit("/", 1)[1]))
        assert endpoint == "repos/o/r/actions/runs/50"
        return ci.run

    def capture(repo, job_id, **kwargs):
        captures.append(job_id)
        return b"bounded log", ""

    monkeypatch.setattr(poller, "_gh_api", metadata)
    monkeypatch.setattr(poller, "capture_job_log", capture)
    result = poller._remediation_logs("o/r", ci.pr, "test-token", checks)
    assert captures == ([101, 102, 103] if jobs == "distinct" else [101])
    if jobs == "distinct":
        assert "additional failing jobs omitted" in result
