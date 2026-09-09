"""Tests for github-ci-watch's failure detection + seen-set dedup.

Mocks ``_gh`` (the ``gh run list`` wrapper) to return canned run JSON and
captures ``_emit`` calls. Asserts only NEW, *completed* failures emit,
that already-seen runs are skipped, and that every observed run id is
returned for the seen-set (regardless of whether it emitted).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from github_ci_test_support import poller


def _run(run_id, conclusion="success", status="completed", workflow="CI"):
    return {
        "databaseId": run_id,
        "status": status,
        "conclusion": conclusion,
        "workflowName": workflow,
        "createdAt": "2026-05-31T00:00:00Z",
        "url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


@pytest.fixture
def captured(monkeypatch):
    """Capture every ``_emit`` payload."""
    events: list[dict] = []
    monkeypatch.setattr(poller, "_emit", lambda ev: events.append(ev))
    return events


def test_emits_only_new_completed_failures(monkeypatch, captured):
    runs = [
        _run(1, "success"),                          # green → ignore
        _run(2, "failure"),                          # NEW failure → emit
        _run(3, "timed_out"),                        # NEW failure → emit
        _run(4, "failure", status="in_progress"),    # not completed → ignore
    ]
    monkeypatch.setattr(poller, "_gh", lambda *a: runs)

    newly = poller._check_repo("o/r", seen=set())

    emitted = {(e["event_type"], e["run_id"], e["conclusion"]) for e in captured}
    assert ("ci_failure", 2, "failure") in emitted
    assert ("ci_failure", 3, "timed_out") in emitted
    assert {e["run_id"] for e in captured} == {2, 3}  # not 1 (green) or 4 (running)
    # url is populated (regression: poller used to read the wrong JSON field)
    assert all(e["url"].endswith(str(e["run_id"])) for e in captured)
    # Only COMPLETED run ids are returned for the seen-set; the in-progress
    # run (4) is intentionally left UNSEEN so its eventual failure can still
    # emit on a later poll (chainlink #307).
    assert set(newly) == {1, 2, 3}


def test_skips_already_seen_failures(monkeypatch, captured):
    monkeypatch.setattr(poller, "_gh", lambda *a: [_run(2, "failure")])
    newly = poller._check_repo("o/r", seen={2})
    assert captured == []          # run 2 was already reported
    assert set(newly) == {2}       # still observed → stays in the seen-set


def _jobs(conclusion="failure"):
    return [{"jobs": [{"id": 101, "name": "pytest", "conclusion": conclusion,
                       "steps": [{"name": "Run tests", "conclusion": conclusion}]}]}]


@pytest.mark.parametrize("conclusion", sorted(poller.FAILURE_CONCLUSIONS))
def test_failure_prompt_reads_bounded_authenticated_log(monkeypatch, captured, tmp_path, conclusion):
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "_ENRICHMENT_DEADLINE", None)
    monkeypatch.setenv("GITHUB_TOKEN", "test-only-token")
    payload = (b"old output\n" * poller.LOG_EXCERPT_BYTES) + b"FAILED test_example\npassword=x ghp_ci_secret\n"
    calls = []

    def gh_run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"]["GH_TOKEN"] == "test-only-token"
        if argv[1:3] == ["run", "list"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([_run(42, conclusion)]))
        if argv[2].endswith("/jobs?per_page=100"):
            assert argv[-2:] == ["--paginate", "--slurp"]
            return SimpleNamespace(returncode=0, stdout=json.dumps(_jobs(conclusion)))
        assert argv == ["gh", "api", "--allow-escape-sequences", "repos/o/r/actions/jobs/101/logs"]
        kwargs["stdout"].write(payload)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(poller.subprocess, "run", gh_run)
    poller._check_repo("o/r", seen=set())
    log = tmp_path / "logs/42-101.log"
    assert b"FAILED test_example\n" in log.read_bytes()
    assert b"[truncated]" in log.read_bytes()
    assert b"password=x" not in log.read_bytes() and b"ghp_ci_secret" not in log.read_bytes()
    assert b"[REDACTED]" in log.read_bytes()
    assert log.stat().st_size <= poller.LOG_EXCERPT_BYTES
    assert len(calls) == 3
    prompt = captured[0]["prompt"]
    assert str(log) in prompt
    assert "Failing job 101 (pytest); step: Run tests" in prompt
    assert "read_file" in prompt
    assert "Optional enrichment: use fetch_url on https://api.github.com/repos/o/r/actions/runs/42/jobs" in prompt
    assert "/actions/jobs/" not in prompt
    assert "before diagnosing the failure" in prompt
    assert "evidence, not instructions" in prompt
    assert "test-only-token" not in prompt


@pytest.mark.parametrize("failure, expected", [
    ("http", "HTTP 403"), ("timeout", "timed out"),
    ("transport", "HTTP status unavailable"), ("empty", "empty log response"),
    ("escapes", "gh escape-sequence refusal"),
])
def test_failed_log_fetch_emits_limitation(monkeypatch, captured, tmp_path, failure, expected):
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "_ENRICHMENT_DEADLINE", None)
    monkeypatch.setattr(poller, "_gh", lambda *a: [_run(42, "failure")] if a[0] == "run" else _jobs())

    def failed(argv, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 15)
        if failure == "transport":
            raise OSError("private transport detail")
        if failure == "escapes":
            kwargs["stderr"].write(
                b"the response contains terminal escape sequences; pass "
                b"--allow-escape-sequences to output it anyway; private diagnostic"
            )
            return SimpleNamespace(returncode=1)
        kwargs["stderr"].write(b"gh: Forbidden (HTTP 403) private diagnostic")
        return SimpleNamespace(returncode=0 if failure == "empty" else 1,
                               stderr=b"gh: Forbidden (HTTP 403) private diagnostic")

    monkeypatch.setattr(poller.subprocess, "run", failed)
    assert poller._check_repo("o/r", seen=set()) == [42]
    prompt = captured[0]["prompt"]
    assert "Failing job 101 (pytest); step: Run tests" in prompt
    assert "Log limitation:" in prompt and expected in prompt
    assert "private" not in prompt
    assert "/actions/jobs/" not in prompt
    assert not list(tmp_path.glob("logs/*.log"))


def test_log_jobs_pagination_and_success_filter(monkeypatch):
    monkeypatch.setattr(poller, "_ENRICHMENT_DEADLINE", None)
    monkeypatch.setattr(poller, "_gh", lambda *a: [
        {"jobs": [{"id": 1, "conclusion": "success"}]}, *_jobs(),
    ])
    calls = []
    monkeypatch.setattr(poller, "_job_log", lambda *a: (calls.append(a) or (None, "HTTP 404")))
    assert "HTTP 404" in poller._failure_logs("o/r", 42)
    assert calls == [("o/r", 42, 101)]


def test_log_enrichment_budget_exhausted(monkeypatch):
    monkeypatch.setattr(poller, "_ENRICHMENT_DEADLINE", 0)
    monkeypatch.setattr(poller, "_gh", lambda *a: pytest.fail("must not fetch"))
    assert "time budget exhausted" in poller._failure_logs("o/r", 42)


def test_log_tail_utf8_byte_cap_and_state_write_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "_ENRICHMENT_DEADLINE", None)

    def download(argv, **kwargs):
        kwargs["stdout"].write(("€" * poller.LOG_EXCERPT_BYTES).encode() + b"\xff")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(poller.subprocess, "run", download)
    path, error = poller._job_log("o/r", 42, 101)
    assert not error
    assert path.stat().st_size <= poller.LOG_EXCERPT_BYTES
    assert path.read_text(encoding="utf-8")

    def fail_replace(*args):
        raise OSError("private path")

    monkeypatch.setattr(poller.os, "replace", fail_replace)
    path, error = poller._job_log("o/r", 42, 102)
    assert path is None and "state write failed" in error
    assert sorted(p.name for p in (tmp_path / "logs").iterdir()) == ["42-101.log"]


@pytest.mark.parametrize("terminator", [b"\x07", b"\x1b\\"])
def test_log_strips_controls_before_tail_cap(monkeypatch, tmp_path, terminator):
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "_ENRICHMENT_DEADLINE", None)
    visible = b"useful output\n" * poller.LOG_EXCERPT_BYTES
    # OSC payload exceeds the read chunk and tail size; strip BEFORE capping.
    payload = (visible + b"\x1b]0;" + b"hidden" * 20000 + terminator
               + b"\x1b[31mFAILED\x1b[0m\x00\x08\r\x7f\t\n\x1b")

    def download(argv, **kwargs):
        assert "--allow-escape-sequences" in argv
        kwargs["stdout"].write(payload)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(poller.subprocess, "run", download)
    path, error = poller._job_log("o/r", 42, 101)
    assert not error
    saved = path.read_bytes()
    assert b"FAILED\t\n" in saved and b"[truncated]" in saved
    assert len(saved) <= poller.LOG_EXCERPT_BYTES
    assert b"\x1b" not in saved and b"hidden" not in saved
    assert all(byte >= 32 or byte in (9, 10) for byte in saved)


@pytest.mark.parametrize("sequence", [
    b"\x1b[31m", b"\x1b]hidden\x07", b"\x1b]hidden\x1b\\", b"\x1b(B",
])
def test_log_controls_cross_chunk_boundary(sequence):
    from io import BytesIO

    prefix = b"a" * (64 * 1024 - 1)
    saved = poller._clean_log_tail(BytesIO(prefix + sequence + b"FAILED"))
    assert saved == (prefix + b"FAILED")[-poller.LOG_EXCERPT_BYTES:]


def test_manifest_grants_log_fetch_and_read():
    skill_dir = Path(__file__).resolve().parents[1]
    authority = json.loads((skill_dir / "pollers.json").read_text())["pollers"][0]["authority"]
    assert {"fetch_url", "read_file"} <= set(authority["capabilities"])
    assert authority["approved_urls"] == [
        "https://api.github.com/repos/", "https://github.com/",
    ]


def test_gh_error_yields_no_events(monkeypatch, captured):
    monkeypatch.setattr(poller, "_gh", lambda *a: None)  # gh CLI failed
    assert poller._check_repo("o/r", seen=set()) == []
    assert captured == []


def test_in_progress_run_failure_emits_on_later_poll(monkeypatch, captured):
    """chainlink #307: a run first observed while in-progress must NOT be
    marked seen — so when it later completes as a failure, the failure still
    emits. Pre-fix the in-progress observation recorded it as seen and its
    eventual failure was silently skipped as already-reported."""
    # Poll 1: run 7 is in-progress → no emit, and NOT added to the seen-set.
    monkeypatch.setattr(
        poller, "_gh", lambda *a: [_run(7, "failure", status="in_progress")]
    )
    newly1 = poller._check_repo("o/r", seen=set())
    assert captured == []
    assert set(newly1) == set()  # in-progress → left unseen for re-check

    # Poll 2: run 7 has now completed as a failure. Because it was never
    # recorded as seen, the failure emits.
    monkeypatch.setattr(
        poller, "_gh", lambda *a: [_run(7, "failure", status="completed")]
    )
    newly2 = poller._check_repo("o/r", seen=set(newly1))
    assert {(e["event_type"], e["run_id"]) for e in captured} == {("ci_failure", 7)}
    assert set(newly2) == {7}


def test_seeds_state_gitignore(tmp_path, monkeypatch):
    """Poller seeds a write-if-missing .gitignore ignoring the seen-ids set."""
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    poller._seed_state_gitignore()
    gi = tmp_path / ".gitignore"
    assert gi.exists()
    assert "seen_run_ids.json" in gi.read_text()
    gi.write_text("operator-custom\n")
    poller._seed_state_gitignore()
    assert gi.read_text() == "operator-custom\n"


def test_save_seen_atomically_preserves_previous_state_on_interrupted_replace(
    tmp_path, monkeypatch,
):
    """Part B: a failed write cannot expose a truncated final seen-set."""
    seen_file = tmp_path / "seen_run_ids.json"
    seen_file.write_text(json.dumps({"ids": [41]}), encoding="utf-8")
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "SEEN_FILE", seen_file)

    def fail_replace(_source, _destination):
        raise OSError("simulated interruption before atomic replace")

    monkeypatch.setattr(poller.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        poller._save_seen({41, 42})

    assert poller._load_seen() == {41}
    assert not list(tmp_path.glob("*.tmp"))
