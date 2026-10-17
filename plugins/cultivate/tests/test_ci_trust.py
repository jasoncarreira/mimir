"""Provider-owned CI and GitHub trust contracts."""

from __future__ import annotations

import json
import subprocess
from io import BytesIO
from types import SimpleNamespace

import pytest

from mimir_cultivate import ci, github_trust


def test_provider_ci_summary_survives_cleanup(monkeypatch):
    summary = b"=== short test summary info ===\nFAILED tests/test_api.py::test_read\n1 failed\n"

    def run(argv, **kwargs):
        assert argv[-1] == "repos/o/r/actions/jobs/101/logs"
        assert kwargs["env"]["GH_TOKEN"] == "private-token"
        kwargs["stdout"].write(b"setup\n" * 10000 + summary + b"cleanup\n" * 10000)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ci.subprocess, "run", run)
    excerpt, error = ci.capture_job_log("o/r", 101, token="private-token", timeout=5)
    assert not error
    assert summary in excerpt
    assert b"cleanup" not in excerpt
    assert len(excerpt) <= ci.LOG_EXCERPT_BYTES


def test_provider_ci_refuses_oversized_secret_line():
    payload = b"password=" + b"x" * (65537 - len(b"password="))
    excerpt = ci._select_excerpt(BytesIO(payload + b"secret-tail\nFAILED safe\n"), 2048, "")
    assert b"secret-tail" not in excerpt
    assert b"FAILED safe" in excerpt


def test_provider_ci_capture_redacts_selected_failure_and_bounds_output(monkeypatch):
    token = "private-token"
    secret = "ghp_" + "A" * 36

    def run(argv, **kwargs):
        kwargs["stdout"].write(
            (b"setup\n" * 10000)
            + f"FAILED test_api: {token} {secret}\n".encode()
            + (b"context\n" * 10000)
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ci.subprocess, "run", run)
    excerpt, error = ci.capture_job_log("o/r", 101, token=token, timeout=5, limit=1024)
    assert error == ""
    assert b"FAILED test_api" in excerpt
    assert token.encode() not in excerpt
    assert secret.encode() not in excerpt
    assert len(excerpt) <= 1024
    assert excerpt.endswith(ci.TRUNCATION_MARKER)


@pytest.mark.parametrize("repo,job_id,limit", [
    ("o/r;cat /etc/passwd", 101, 1024),
    ("o/..", 101, 1024),
    ("https://github.com/o/r", 101, 1024),
    ("o/r", True, 1024),
    ("o/r", 0, 1024),
    ("o/r", 101, 0),
])
def test_provider_ci_refuses_invalid_targets_without_invoking_gh(
    monkeypatch, repo, job_id, limit,
):
    def unexpected_run(*args, **kwargs):
        pytest.fail("invalid target reached gh")

    monkeypatch.setattr(ci.subprocess, "run", unexpected_run)
    excerpt, error = ci.capture_job_log(repo, job_id, token="private-token", timeout=5, limit=limit)
    assert excerpt == b""
    assert "invalid" in error


@pytest.mark.parametrize("failure,expected", [
    (b"HTTP 403: private-token ghp_" + b"A" * 36, "HTTP 403"),
    (b"HTTP 401: private-token", "HTTP 401 (unauthenticated gh)"),
    (b"gh auth login: private-token", "HTTP status unavailable (unauthenticated gh)"),
    (b"--allow-escape-sequences private-token", "HTTP status unavailable (gh escape-sequence refusal)"),
])
def test_provider_ci_failure_does_not_return_raw_output(monkeypatch, failure, expected):
    def run(argv, **kwargs):
        kwargs["stdout"].write(b"FAILED private-token secret build output")
        kwargs["stderr"].write(failure)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(ci.subprocess, "run", run)
    excerpt, error = ci.capture_job_log("o/r", 101, token="private-token", timeout=5)
    assert excerpt == b""
    assert error == expected


@pytest.mark.parametrize("failure,expected", [
    (subprocess.TimeoutExpired("gh", 5, output=b"private-token"), "log fetch timed out"),
    (OSError("private-token"), "log fetch or state write failed"),
])
def test_provider_ci_exception_does_not_return_raw_output(monkeypatch, failure, expected):
    def run(argv, **kwargs):
        raise failure

    monkeypatch.setattr(ci.subprocess, "run", run)
    excerpt, error = ci.capture_job_log("o/r", 101, token="private-token", timeout=5)
    assert excerpt == b""
    assert expected in error
    assert "private-token" not in error


def test_provider_cancelled_run_requires_matching_newer_run():
    run = {"id": 10, "head_sha": "head", "workflow_id": 5}
    assert ci.classify_cancelled_run(run, [{"id": 11, "head_sha": "head", "workflow_id": 5}]) == "SUPERSEDED"
    assert ci.classify_cancelled_run(run, [{"id": 11, "head_sha": "other", "workflow_id": 5}]) == "UNKNOWN"


def test_provider_withholds_unattested_content():
    item = {
        "kind": "comment", "body": "untrusted instructions", "repository": "o/r",
        "html_url": "https://github.com/o/r/issues/4", "created_at": "2026-10-17T12:00:00Z",
    }
    kept, withheld = github_trust.partition([item], lambda _: "external", lambda _: None)
    assert kept == []
    assert github_trust.placeholder(withheld[0]) == {
        "kind": "comment", "author": "external", "created_at": "2026-10-17T12:00:00Z",
        "html_url": "https://github.com/o/r/issues/4", "reason": "attestation_unavailable",
        "withheld": True,
    }


def test_provider_trust_partition_keeps_only_attested_content_in_result():
    items = [
        {"kind": "comment", "author": "maintainer", "body": "approved change"},
        {"kind": "comment", "author": "outsider", "body": "UNATTESTED_INSTRUCTIONS",
         "repository": "o/r", "html_url": "https://evil.example/steal?secret=UNATTESTED_INSTRUCTIONS",
         "created_at": "2026-10-17T12:00:00Z\nUNATTESTED_INSTRUCTIONS"},
        {"kind": "review", "author": "robot[bot]", "body": "BOT_INSTRUCTIONS"},
    ]
    verdicts = {"maintainer": True, "outsider": None, "robot[bot]": False}
    kept, withheld = github_trust.partition(
        items, lambda item: item["author"], lambda author: verdicts[author],
    )
    result = {"items": kept, "withheld": [github_trust.placeholder(item) for item in withheld],
              "summary": github_trust.summarise(withheld)}
    serialized = json.dumps(result)
    assert kept == [items[0]]
    assert result["summary"] == {"withheld": 2, "by_kind": {"comment": 1, "review": 1}}
    assert [item["reason"] for item in result["withheld"]] == [
        "attestation_unavailable", "bot_not_allowlisted",
    ]
    assert result["withheld"][0]["html_url"] is None
    assert result["withheld"][0]["created_at"] is None
    assert "UNATTESTED_INSTRUCTIONS" not in serialized
    assert "BOT_INSTRUCTIONS" not in serialized
    assert "approved change" in serialized


def test_provider_token_probe_ignores_ambient_credentials(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "private-token")
    observed = {}

    class Session:
        def __init__(self):
            self.trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, url, **kwargs):
            observed.update(url=url, trust_env=self.trust_env, **kwargs)
            return SimpleNamespace(status_code=200, json=lambda: {"login": "operator"})

    monkeypatch.setattr(github_trust.requests, "Session", Session)
    assert github_trust.probe_github_token() == (True, "GitHub authenticated as operator")
    assert observed["trust_env"] is False
    assert observed["allow_redirects"] is False
    assert observed["headers"]["Authorization"] == "Bearer private-token"
