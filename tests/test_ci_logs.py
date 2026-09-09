"""Shared capture selection, resource bounds, and security contracts."""
from __future__ import annotations

from io import BytesIO
import importlib
from types import SimpleNamespace

import pytest

from mimir import ci_logs


def test_legacy_helper_is_only_a_shared_import():
    legacy = importlib.import_module("mimir.optional-skills.github-ci-watch.scripts.ci_logs")
    assert legacy.capture_job_log is ci_logs.capture_job_log
    assert legacy.clean_log_tail is ci_logs.clean_log_tail


def capture(monkeypatch, payload, **options):
    monkeypatch.setenv("GH_TOKEN", "ambient-token")
    def run(argv, **kwargs):
        assert argv == ["gh", "api", "--allow-escape-sequences", "repos/o/r/actions/jobs/101/logs"]
        assert kwargs["env"]["GH_TOKEN"] == "literal-private-value"
        assert kwargs["timeout"] == 5
        assert kwargs["stdout"].fileno() >= 0
        assert kwargs["stderr"].fileno() >= 0
        assert "shell" not in kwargs
        kwargs["stdout"].write(payload)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ci_logs.subprocess, "run", run)
    data, error = ci_logs.capture_job_log(
        "o/r", 101, token="literal-private-value", timeout=5, **options,
    )
    assert not error
    return data


def test_pytest_summary_survives_long_cleanup(monkeypatch):
    summary = (
        b"2026-09-09T12:00:00Z ================= short test summary info =================\n"
        b"2026-09-09T12:00:00Z FAILED tests/test_api.py::test_read - AssertionError: mismatch\n"
        b"2026-09-09T12:00:00Z ERROR tests/test_db.py::test_write - RuntimeError: fixture\n"
        b"2026-09-09T12:00:00Z ============== 1 failed, 200 passed, 1 error in 4.2s ==============\n"
    )
    data = capture(monkeypatch, b"setup\n" * 10000 + summary + (
        b"Post job cleanup.\n/usr/bin/git config --local --unset-all http.extraheader\n" * 10000
    ) + b"Error: Process completed with exit code 1.\n")
    assert summary in data
    assert b"cleanup" not in data and b"exit code" not in data
    assert data.endswith(ci_logs.TRUNCATION_MARKER)


def test_last_error_region_not_cleanup_tail(monkeypatch):
    data = capture(monkeypatch, b"ERROR old failure\n" + b"noise\n" * 100 +
                   b"context\nERROR compilation failed\nmissing symbol\n" + b"cleanup\n" * 10000)
    assert b"ERROR compilation failed\nmissing symbol" in data
    assert b"old failure" not in data
    assert data.count(b"cleanup") <= 8


@pytest.mark.parametrize("counts", [
    b"1 failed, 200 passed, 1 error in 4.2s",
    b"2026-09-09T12:00:00Z 2 errors in 0.5s",
    b"================ 1 failed, 3 skipped in 1.0s ================",
])
def test_summary_keeps_quiet_and_decorated_counts(monkeypatch, counts):
    data = capture(monkeypatch, b"=== short test summary info ===\n"
                   b"FAILED tests/test_api.py::test_read\n" + counts + b"\n" + b"cleanup\n" * 100)
    assert counts in data and b"cleanup" not in data


def test_exact_cap_needs_no_marker_but_redaction_expansion_does(monkeypatch):
    payload = b"FAILED password=x\n"
    redacted = b"FAILED password=[REDACTED]\n"
    assert capture(monkeypatch, payload, limit=len(redacted)) == redacted
    data = capture(monkeypatch, payload, limit=len(payload))
    assert len(data) == len(payload)
    assert data.endswith(ci_logs.TRUNCATION_MARKER)


@pytest.mark.parametrize("limit", [13, 64, 127, 2048, 32768, 1000000])
def test_hard_byte_cap_after_redaction_expansion(monkeypatch, limit):
    data = capture(monkeypatch, ("FAILED password=x \u20ac\n" * 2000).encode(), limit=limit)
    assert len(data) <= min(limit, ci_logs.LOG_EXCERPT_BYTES)
    assert data.endswith(ci_logs.TRUNCATION_MARKER)
    data.decode("utf-8", errors="strict")
    assert b"password=x" not in data


def test_maximum_cap_clamps_expanded_summary(monkeypatch):
    payload = (b"=== short test summary info ===\n" + b"FAILED password=x\n" * 1500
               + b"1500 failed in 1.0s\n")
    assert len(payload) < 32768
    data = capture(monkeypatch, payload, limit=1000000)
    assert len(data) <= 32768
    assert data.endswith(ci_logs.TRUNCATION_MARKER)
    assert b"[REDACTED]" in data and b"password=x" not in data


def test_redacts_tokens_and_literal_auth_before_return(monkeypatch):
    data = capture(monkeypatch, b"FAILED ghp_abcdef github_pat_abcdef sk-ant-abcdef\n"
                   b"literal-private-value password=x\n")
    for secret in [b"ghp_abcdef", b"github_pat_abcdef", b"sk-ant-abcdef", b"literal-private-value", b"password=x"]:
        assert secret not in data
    assert data.count(b"[REDACTED]") == 5


@pytest.mark.parametrize("control", [
    b"\x1b[31m", b"\x1b]hidden\x07", b"\x1bPhidden\x1b\\",
    "\u009b31m\u202e\u200b\x00\x7f".encode(),
])
def test_capture_removes_controls_before_redaction(monkeypatch, control):
    data = capture(monkeypatch, b"FAILED ghp_" + control + b"private\n")
    assert data == b"FAILED [REDACTED]\n"


@pytest.mark.parametrize("shape", ["huge_line", "long_summary"])
def test_bounded_reads_and_redactor_input_for_huge_line(monkeypatch, shape):
    class BoundedReader(BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 64 * 1024
            return super().read(size)

    original = ci_logs.redact_text
    sizes = []

    def redact(text):
        sizes.append(len(text.encode()))
        assert len(text.encode()) <= ci_logs.LOG_EXCERPT_BYTES
        return original(text)

    monkeypatch.setattr(ci_logs, "redact_text", redact)
    payload = b"password=" + b"s" * (4 * 1024 * 1024) + b"\nFAILED useful\n"
    if shape == "long_summary":
        payload = (b"=== short test summary info ===\n" + b"FAILED useful\n" * 10000
                   + b"10000 failed in 1.0s\n")
    data = ci_logs._select_excerpt(BoundedReader(payload), 2048, "")
    assert b"FAILED useful" in data and b"ssss" not in data
    assert sizes


@pytest.mark.parametrize("context", [b"", b"setup context\n"])
def test_oversized_credential_remainder_is_not_a_new_line(monkeypatch, context):
    # The final fragment fits the excerpt; treating it as a line leaks a value
    # whose credential key was in a previous read chunk.
    prefix = b"password=" + b"s" * (65537 - len(b"password="))
    data = capture(monkeypatch, context + prefix + b"private-credential-tail\nFAILED useful\n")
    assert b"FAILED useful" in data
    assert b"private-credential-tail" not in data


@pytest.mark.parametrize("repo,job_id", [
    (None, 1), (123, 1), (b"o/r", 1),
    ("o/..", 1), ("o/.", 1), ("o/r/extra", 1), ("o/r?x=y", 1),
    ("o/r%2fsecret", 1), ("https://evil/r", 1), ("-o/r", 1),
    ("o/r\n", 1), ("o/r", True), ("o/r", "1"), ("o/r", -1),
    ("o/r", 0), ("o/r", "1/../../secrets"),
])
def test_invalid_targets_never_spawn(monkeypatch, repo, job_id):
    monkeypatch.setattr(ci_logs.subprocess, "run", lambda *a, **k: pytest.fail("must not spawn"))
    data, error = ci_logs.capture_job_log(repo, job_id, token="", timeout=5)
    assert not data and "invalid" in error


@pytest.mark.parametrize("limit", [0, -1, 12, True, 1.5, 13.5, "2048", None])
def test_invalid_cap_never_spawns(monkeypatch, limit):
    monkeypatch.setattr(ci_logs.subprocess, "run", lambda *a, **k: pytest.fail("must not spawn"))
    data, error = ci_logs.capture_job_log("o/r", 101, token="", timeout=5, limit=limit)
    assert not data and "invalid" in error


def test_failed_download_does_not_expose_partial_output_or_stderr(monkeypatch):
    def run(argv, **kwargs):
        kwargs["stdout"].write(b"FAILED private partial output")
        kwargs["stderr"].write(b"HTTP 403 ghp_secret https://signed.private/url" + b"x" * 100000)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(ci_logs.subprocess, "run", run)
    assert ci_logs.capture_job_log("o/r", 101, token="", timeout=5) == (b"", "HTTP 403")


@pytest.mark.parametrize("stderr,expected", [
    (b"To get started with GitHub CLI, please run: gh auth login", "HTTP status unavailable"),
    (b"Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.", "HTTP status unavailable"),
    (b"gh: Bad credentials (HTTP 401)", "HTTP 401"),
])
def test_unauthenticated_gh_has_distinct_safe_refusal(monkeypatch, stderr, expected):
    def run(argv, **kwargs):
        kwargs["stdout"].write(b"FAILED private partial output")
        kwargs["stderr"].write(stderr + b" ghp_private https://signed.private/url")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(ci_logs.subprocess, "run", run)
    assert ci_logs.capture_job_log("o/r", 101, token="", timeout=5) == (
        b"", f"{expected} (unauthenticated gh)",
    )


@pytest.mark.parametrize("timeout", [0, -1])
def test_exhausted_budget_never_spawns(monkeypatch, timeout):
    monkeypatch.setattr(ci_logs.subprocess, "run", lambda *a, **k: pytest.fail("must not spawn"))
    assert ci_logs.capture_job_log("o/r", 101, token="", timeout=timeout) == (
        b"", "HTTP status unavailable (poller time budget exhausted)",
    )


@pytest.mark.parametrize("diagnostic", [b"escape sequences", b"--allow-escape-sequences"])
def test_escape_refusal_is_safe_and_distinct(monkeypatch, diagnostic):
    def run(argv, **kwargs):
        kwargs["stdout"].write(b"private partial output")
        kwargs["stderr"].write(diagnostic + b" ghp_private")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(ci_logs.subprocess, "run", run)
    assert ci_logs.capture_job_log("o/r", 101, token="", timeout=5) == (
        b"", "HTTP status unavailable (gh escape-sequence refusal)",
    )


@pytest.mark.parametrize("returncode", [0, 1])
def test_spooled_reads_are_bounded(monkeypatch, returncode):
    temporary_file = ci_logs.tempfile.TemporaryFile
    reads = []

    def spool(*args, **kwargs):
        file = temporary_file(*args, **kwargs)
        for method in ("read", "readline"):
            original = getattr(file, method)

            def bounded(size=-1, *, original=original, method=method):
                reads.append((method, size))
                assert 0 < size <= 65537
                return original(size)

            monkeypatch.setattr(file, method, bounded)
        return file

    def run(argv, **kwargs):
        kwargs["stdout"].write(b"FAILED useful\n")
        kwargs["stderr"].write(b"HTTP 403 " + b"private" * 20000)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(ci_logs.tempfile, "TemporaryFile", spool)
    monkeypatch.setattr(ci_logs.subprocess, "run", run)
    data, error = ci_logs.capture_job_log("o/r", 101, token="", timeout=5)
    assert (data, error) == ((b"", "HTTP 403") if returncode else (b"FAILED useful\n", ""))
    assert reads
