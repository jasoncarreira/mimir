"""Core's process-bound GitHub identity for credentialed gh children."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mimir import github_identity as core_identity
from mimir.tools.refusals import ToolPolicyRefusal


@pytest.fixture(autouse=True)
def unbound_identity(monkeypatch):
    monkeypatch.setattr(core_identity, "_verified_identity", None)
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")


def test_first_gh_call_binds_once_then_answers_from_the_process_binding(monkeypatch) -> None:
    calls: list[str] = []

    def fetch(token):
        calls.append(token)
        return "Reviewer"

    monkeypatch.setattr(core_identity, "fetch_github_login", fetch)
    assert core_identity.confirm_gh_cli_identity("process-token") == "Reviewer"
    assert core_identity.confirm_gh_cli_identity("process-token") == "Reviewer"
    assert calls == ["process-token"]
    assert core_identity.verified_identity() == (
        "Reviewer", core_identity.credential_fingerprint("process-token"),
    )
    # A rotated credential is never rebound by a later call.
    with pytest.raises(ToolPolicyRefusal, match="does not match active credential"):
        core_identity.confirm_gh_cli_identity("rotated-token")
    assert calls == ["process-token"]


@pytest.mark.parametrize("failure", ["mismatch", "invalid", "transport"])
def test_failed_first_binding_refuses_and_caches_nothing(monkeypatch, failure) -> None:
    def fetch(token):
        if failure == "transport":
            raise core_identity.GitHubIdentityVerificationError(
                "github identity transport failed: ConnectionError",
                failure_kind=core_identity.GitHubIdentityFailureKind.TRANSIENT,
            )
        return {"mismatch": "someone-else", "invalid": "not a login!"}[failure]

    monkeypatch.setattr(core_identity, "fetch_github_login", fetch)
    with pytest.raises(ToolPolicyRefusal, match="identity verification failed"):
        core_identity.confirm_gh_cli_identity("process-token")
    assert core_identity.verified_identity() is None


@pytest.mark.parametrize("declared", ["", "   "])
def test_empty_declared_identity_refuses_without_asking_the_forge(monkeypatch, declared) -> None:
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", declared)
    monkeypatch.setattr(core_identity, "fetch_github_login", lambda token: pytest.fail("fetched"))
    with pytest.raises(ToolPolicyRefusal, match="declared identity is empty"):
        core_identity.confirm_gh_cli_identity("process-token")


def test_missing_token_refuses_without_asking_the_forge(monkeypatch) -> None:
    monkeypatch.setattr(core_identity, "fetch_github_login", lambda token: pytest.fail("fetched"))
    with pytest.raises(ToolPolicyRefusal, match="GITHUB_TOKEN is not set"):
        core_identity.confirm_gh_cli_identity("")


class _Raw:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, limit, decode_content=True):
        return self._body[:limit]


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self.raw = _Raw(body)

    def close(self) -> None:
        pass


@pytest.mark.parametrize(("status", "kind"), [
    (401, core_identity.GitHubIdentityFailureKind.PERMANENT),
    (429, core_identity.GitHubIdentityFailureKind.TRANSIENT),
    (502, core_identity.GitHubIdentityFailureKind.TRANSIENT),
])
def test_fetch_login_classifies_http_failures(monkeypatch, status, kind) -> None:
    import requests

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Response(status, b"{}"))
    with pytest.raises(core_identity.GitHubIdentityVerificationError) as raised:
        core_identity.fetch_github_login("process-token")
    assert raised.value.failure_kind == kind


def test_fetch_login_sends_only_the_given_token(monkeypatch) -> None:
    import requests

    seen = {}

    def get(url, *, headers, timeout, stream):
        seen.update(url=url, authorization=headers.get("Authorization"))
        return _Response(200, b'{"login": "reviewer"}')

    monkeypatch.setattr(requests, "get", get)
    assert core_identity.fetch_github_login("process-token") == "reviewer"
    assert seen == {"url": "https://api.github.com/user", "authorization": "Bearer process-token"}


def test_core_gh_identity_and_env_never_import_the_provider() -> None:
    """Proved in a child: the core path binds and refuses with Cultivate absent."""
    script = """
import sys
from mimir import github_identity as core_identity
from mimir.tools._shell_env import direct_exec_env
from mimir.tools.refusals import ToolPolicyRefusal

core_identity._verified_identity = (
    'reviewer', core_identity.credential_fingerprint('process-token'),
)
env = direct_exec_env(['/usr/bin/gh', 'pr', 'view', '1'])
assert env['GITHUB_TOKEN'] == 'process-token'
assert 'GH_TOKEN' not in env
core_identity._verified_identity = ('someone-else', core_identity.credential_fingerprint('process-token'))
try:
    direct_exec_env(['/usr/bin/gh', 'pr', 'view', '1'])
except ToolPolicyRefusal:
    pass
else:
    raise AssertionError('identity mismatch was not refused')
assert not any(name == 'mimir_cultivate' or name.startswith('mimir_cultivate.')
               for name in sys.modules)
"""
    env = {**os.environ, "MIMIR_PLUGINS": "", "GITHUB_TOKEN": "process-token",
           "GH_TOKEN": "stray-token", "MIMIR_GITHUB_SELF_LOGIN": "reviewer"}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("bound", "principal", "token", "message"), [
    (None, "reviewer", "process-token", "cache is empty"),
    (("reviewer", "process-token"), "reviewer", "rotated-token", "does not match active credential"),
    (("reviewer", "process-token"), "someone-else", "process-token", "acting identity mismatch"),
])
def test_confirm_refuses_without_a_matching_binding(monkeypatch, bound, principal, token, message) -> None:
    if bound is not None:
        monkeypatch.setattr(core_identity, "_verified_identity", (
            bound[0], core_identity.credential_fingerprint(bound[1]),
        ))
    with pytest.raises(core_identity.GitHubIdentityVerificationError, match=message):
        core_identity.confirm_github_identity(principal, token)


def test_confirm_accepts_the_bound_identity_case_insensitively(monkeypatch) -> None:
    monkeypatch.setattr(core_identity, "_verified_identity", (
        "Reviewer", core_identity.credential_fingerprint("process-token"),
    ))
    assert core_identity.confirm_github_identity("reviewer", "process-token") == "Reviewer"
