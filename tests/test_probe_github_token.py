from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from mimir import cred_verify, probe_github_token


@pytest.fixture
def session(monkeypatch):
    mocked = MagicMock()
    mocked.__enter__.return_value = mocked
    mocked.get.return_value.status_code = 200
    mocked.get.return_value.json.return_value = {"login": "octocat"}
    monkeypatch.setattr(requests, "Session", MagicMock(return_value=mocked))
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret")
    return mocked


@pytest.mark.parametrize("manifest", [False, True])
def test_current_token_and_request_policy(monkeypatch, session, manifest):
    fn = probe_github_token.probe
    if manifest:
        entry = next(
            p for p in cred_verify._load_manifest(cred_verify._PACKAGE_MANIFEST)
            if p.name == "GITHUB_TOKEN"
        )
        assert entry.kind == "python"
        assert entry.env_vars == ("GITHUB_TOKEN",)
        fn = entry.fn
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid")
    monkeypatch.setenv("GH_TOKEN", "wrong-token")
    for token in ("first-token", "rotated-token"):
        monkeypatch.setenv("GITHUB_TOKEN", token)
        assert fn() == (True, "GitHub authenticated as octocat")
        assert session.trust_env is False
        session.get.assert_called_with(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
            allow_redirects=False,
        )
    assert requests.Session.call_count == 2
    assert session.__exit__.call_count == 2


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_token(monkeypatch, session, token):
    monkeypatch.setenv("GH_TOKEN", "not-a-fallback")
    if token is None:
        monkeypatch.delenv("GITHUB_TOKEN")
    else:
        monkeypatch.setenv("GITHUB_TOKEN", token)
    assert probe_github_token.probe() == (False, "GITHUB_TOKEN not set")
    requests.Session.assert_not_called()


@pytest.mark.parametrize("status", [201, 301, 302, 307, 308, 401, 403, 429, 500])
def test_http_failure(session, status):
    session.get.return_value.status_code = status
    session.get.return_value.text = "raw body test-secret"
    assert probe_github_token.probe() == (False, f"GitHub HTTP {status}")
    session.get.return_value.json.assert_not_called()


@pytest.mark.parametrize("error", [
    requests.Timeout("test-secret"),
    requests.ConnectionError("test-secret"),
    requests.RequestException("test-secret"),
    RuntimeError("test-secret"),
])
def test_request_exception_sanitized(session, error):
    session.get.side_effect = error
    assert probe_github_token.probe() == (False, "GitHub probe failed")


def test_json_exception_sanitized(session):
    session.get.return_value.json.side_effect = ValueError("raw body test-secret")
    assert probe_github_token.probe() == (False, "GitHub probe failed")


@pytest.mark.parametrize("data", [{}, {"login": ""}, {"login": None}, [], {"login": 1}])
def test_missing_login(session, data):
    session.get.return_value.json.return_value = data
    assert probe_github_token.probe() == (False, "GitHub response missing login")
