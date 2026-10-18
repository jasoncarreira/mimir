"""Selected GitHub identity and the independent core credential fallback."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
import requests

from mimir import cred_verify


@pytest.fixture
def provider():
    return pytest.importorskip("mimir_cultivate.github_trust")


@pytest.fixture
def session(monkeypatch):
    mocked = MagicMock()
    mocked.__enter__.return_value = mocked
    mocked.get.return_value.status_code = 200
    mocked.get.return_value.json.return_value = {"login": "octocat"}
    monkeypatch.setattr(requests, "Session", MagicMock(return_value=mocked))
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret")
    return mocked


def test_core_generic_probe_requires_token_but_never_uses_network(monkeypatch, session):
    entry = next(p for p in cred_verify._load_manifest(cred_verify._PACKAGE_MANIFEST)
                 if p.name == "GITHUB_TOKEN")
    assert entry.kind == "format"
    assert entry.env_vars == ("GITHUB_TOKEN",)
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret")
    assert entry.fn()[0] is True
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    assert entry.fn()[0] is False
    requests.Session.assert_not_called()


def test_selected_probe_uses_only_current_token_and_no_ambient_credentials(
    monkeypatch, session, provider,
):
    from mimir.plugins.core import policy_scope
    from mimir.plugins.manager import load_plugins

    selected = load_plugins(enable="cultivate").policy
    with policy_scope(selected):
        entry = cred_verify.get_probes()["GITHUB_TOKEN"]
        assert entry.source == "plugin:cultivate"
        assert entry.fn is provider.probe_github_token
        monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid")
        monkeypatch.setenv("GH_TOKEN", "wrong-token")
        for token in ("first-token", "rotated-token"):
            monkeypatch.setenv("GITHUB_TOKEN", token)
            assert entry.fn() == (True, "GitHub authenticated as octocat")
            assert session.trust_env is False
            session.get.assert_called_with(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                timeout=10, allow_redirects=False,
            )
    assert requests.Session.call_count == 2
    assert session.__exit__.call_count == 2


def test_disabled_provider_does_not_import_for_core_credentials():
    env = os.environ.copy()
    env.pop("MIMIR_PLUGINS", None)
    child = subprocess.run(
        [sys.executable, "-c", "import sys; from mimir.cred_verify import get_probes; "
         "assert get_probes()['GITHUB_TOKEN'].kind == 'format'; "
         "assert not any(n.startswith('mimir_cultivate') for n in sys.modules)"],
        env=env, capture_output=True, text=True,
    )
    assert child.returncode == 0, child.stderr


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_provider_token(monkeypatch, session, provider, token):
    monkeypatch.setenv("GH_TOKEN", "not-a-fallback")
    if token is None:
        monkeypatch.delenv("GITHUB_TOKEN")
    else:
        monkeypatch.setenv("GITHUB_TOKEN", token)
    assert provider.probe_github_token() == (False, "GITHUB_TOKEN not set")
    requests.Session.assert_not_called()


@pytest.mark.parametrize("status", [201, 301, 302, 307, 308, 401, 403, 429, 500])
def test_provider_http_failure(session, provider, status):
    session.get.return_value.status_code = status
    session.get.return_value.text = "raw body test-secret"
    assert provider.probe_github_token() == (False, f"GitHub HTTP {status}")
    session.get.return_value.json.assert_not_called()


@pytest.mark.parametrize("error", [
    requests.Timeout("test-secret"), requests.ConnectionError("test-secret"),
    requests.RequestException("test-secret"), RuntimeError("test-secret"),
])
def test_provider_request_exception_sanitized(session, provider, error):
    session.get.side_effect = error
    assert provider.probe_github_token() == (False, "GitHub probe failed")


def test_provider_json_exception_sanitized(session, provider):
    session.get.return_value.json.side_effect = ValueError("raw body test-secret")
    assert provider.probe_github_token() == (False, "GitHub probe failed")


@pytest.mark.parametrize("data", [{}, {"login": ""}, {"login": None}, [], {"login": 1}])
def test_provider_missing_login(session, provider, data):
    session.get.return_value.json.return_value = data
    assert provider.probe_github_token() == (False, "GitHub response missing login")
