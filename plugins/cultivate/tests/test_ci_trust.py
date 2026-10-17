"""Provider-owned CI and GitHub trust contracts."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

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
