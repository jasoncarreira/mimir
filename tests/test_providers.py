"""Tests for the canonical provider registry (chainlink #292).

The registry's behavior-preservation for the two migrated consumers is
covered by ``test_model_registry`` (routing) and ``test_billing``
(quota). These tests pin the registry's own surface: the table
invariants and the two resolution directions.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from mimir import providers
from mimir.providers import (
    PROVIDER_ANTHROPIC_API,
    PROVIDER_ANTHROPIC_MAX,
    PROVIDER_MINIMAX,
    PROVIDER_MOONSHOT,
    PROVIDER_OPENAI,
    provider_for_model_name,
    provider_for_quota,
)

FAKE_TOKEN = "fake-oauth-sentinel-not-a-token"


@pytest.fixture
def fake_claude(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "claude"
    cli.write_text(
        "#!/bin/sh\n"
        "printf 'run\\n' >> \"$FAKE_CLAUDE_MARKER\"\n"
        "printf '%s\\n' \"$FAKE_CLAUDE_OUTPUT\"\n"
        "printf '%s\\n' \"$FAKE_CLAUDE_OUTPUT\" >&2\n"
        "if [ -n \"$FAKE_CLAUDE_SLEEP\" ]; then /bin/sleep \"$FAKE_CLAUDE_SLEEP\"; fi\n"
        "exit \"${FAKE_CLAUDE_EXIT:-0}\"\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    marker = tmp_path / "invocations"
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_MARKER", str(marker))
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", FAKE_TOKEN)
    monkeypatch.delenv("FAKE_CLAUDE_SLEEP", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_EXIT", raising=False)

    # Exercise the real executable while checking the output-discard contract
    # on every smoke invocation, including failed and timed-out ones.
    real_run = subprocess.run
    calls = []

    def checked_run(args, **kwargs):
        assert args == [str(cli), "-p", "ping"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["check"] is False
        assert kwargs["timeout"] > 0
        calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(providers.subprocess, "run", checked_run)
    return marker, calls


def _invocations(marker):
    return marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []


def _assert_no_token(status, capfd):
    captured = capfd.readouterr()
    assert FAKE_TOKEN not in status.reason + status.remediation
    assert FAKE_TOKEN not in captured.out + captured.err


# ── table invariants ────────────────────────────────────────────────


def test_exactly_one_default_provider():
    defaults = [p for p in providers.PROVIDERS if p.is_default]
    assert len(defaults) == 1
    assert defaults[0].name == PROVIDER_ANTHROPIC_API


def test_provider_names_are_unique():
    names = [p.name for p in providers.PROVIDERS]
    assert len(names) == len(set(names))


def test_quota_keys_are_all_buildable():
    """Every non-empty ``quota_provider_key`` must resolve to a real
    poller in billing — catches a typo'd key in the table."""
    from mimir.billing import _QUOTA_PROVIDER_BUILDERS

    for p in providers.PROVIDERS:
        if p.quota_provider_key:
            assert p.quota_provider_key in _QUOTA_PROVIDER_BUILDERS, (
                f"{p.name} has unknown quota_provider_key={p.quota_provider_key!r}"
            )


# ── forward: bare model name → provider ─────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("MiniMax-M2.7", PROVIDER_MINIMAX),
        ("abab6.5", PROVIDER_MINIMAX),
        ("ABAB6.5", PROVIDER_MINIMAX),  # abab is matched case-insensitively
        ("kimi-k2", PROVIDER_MOONSHOT),
        ("Kimi-K2-Instruct", PROVIDER_MOONSHOT),
        ("moonshot-v1-128k", PROVIDER_MOONSHOT),
        ("gpt-4o", PROVIDER_OPENAI),
        ("o1-preview", PROVIDER_OPENAI),
        ("o3-mini", PROVIDER_OPENAI),
        ("o4-mini", PROVIDER_OPENAI),
        ("claude-sonnet-4-6", PROVIDER_ANTHROPIC_API),  # Claude family → default
        ("totally-unknown-model", PROVIDER_ANTHROPIC_API),  # unknown → default
    ],
)
def test_provider_for_model_name(name, expected):
    assert provider_for_model_name(name).name == expected


def test_minimax_name_match_is_case_sensitive():
    """Canonical ``MiniMax`` caps → Minimax; a wrong-case typo falls
    through to the default (mirrors detect_route's intentional rule —
    the Minimax API rejects other casings, so fail loudly, don't
    misroute)."""
    assert provider_for_model_name("MiniMax-M2.7").name == PROVIDER_MINIMAX
    assert provider_for_model_name("minimax-m2.7").name == PROVIDER_ANTHROPIC_API


def test_blank_name_routes_to_default():
    assert provider_for_model_name("").name == PROVIDER_ANTHROPIC_API
    assert provider_for_model_name("   ").name == PROVIDER_ANTHROPIC_API


# ── reverse: resolved spec + base URL → provider (quota) ────────────


@pytest.mark.parametrize(
    "model_spec,base_url,expected",
    [
        # owned non-anthropic spec prefixes fully determine the provider
        ("openai:gpt-4o", "", PROVIDER_OPENAI),
        ("codex-plus:gpt-4o", "", PROVIDER_OPENAI),
        ("claude-code:claude-sonnet-4-6", "", PROVIDER_ANTHROPIC_MAX),
        # anthropic: routes disambiguate by base-URL host
        ("anthropic:MiniMax-M2.7", "https://api.minimax.io/anthropic", PROVIDER_MINIMAX),
        # chainlink #259: regional gateway host still matches by substring
        ("anthropic:MiniMax-M2.7", "https://api.minimaxi.com/anthropic", PROVIDER_MINIMAX),
        ("anthropic:kimi-k2", "https://api.moonshot.ai/anthropic", PROVIDER_MOONSHOT),
        # canonical / unset → default Anthropic direct
        ("anthropic:claude-sonnet-4-6", "", PROVIDER_ANTHROPIC_API),
        ("anthropic:claude-sonnet-4-6", "https://api.anthropic.com", PROVIDER_ANTHROPIC_API),
        ("", "", PROVIDER_ANTHROPIC_API),
    ],
)
def test_provider_for_quota(model_spec, base_url, expected):
    assert provider_for_quota(model_spec, base_url).name == expected


# ── pip-extra resolution (PR2) ──────────────────────────────────────


@pytest.mark.parametrize(
    "model_spec,expected_extra",
    [
        ("anthropic:claude-sonnet-4-6", "anthropic"),
        ("anthropic:MiniMax-M2.7", "anthropic"),  # compat gateways use langchain-anthropic
        ("openai:gpt-4o", "openai"),
        ("codex-plus:gpt-4o", "codex-plus"),
        ("claude-code:claude-sonnet-4-6", "claude-code"),
        ("claude-sonnet-4-6", ""),  # bare name, no prefix
        ("", ""),
    ],
)
def test_extra_for_spec(model_spec, expected_extra):
    from mimir.providers import extra_for_spec

    assert extra_for_spec(model_spec) == expected_extra


def test_claude_code_auth_status_missing_cli_is_actionable(
    fake_claude, monkeypatch, tmp_path, capfd
):
    marker, calls = fake_claude
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    status = providers.claude_code_auth_status()

    assert status.ok is False
    assert "claude CLI is not on PATH" in status.reason
    assert "npm install -g @anthropic-ai/claude-code" in status.remediation
    assert ".credentials.json contents" in status.remediation
    assert not calls and not _invocations(marker)
    _assert_no_token(status, capfd)


def test_claude_code_auth_status_missing_credentials_is_actionable(
    fake_claude, monkeypatch, capfd
):
    marker, calls = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")

    status = providers.claude_code_auth_status()

    assert status.ok is False
    assert "no usable OAuth token or .credentials.json" in status.reason
    assert "smoke check failed" in status.reason
    assert "claude login" in status.remediation
    assert "Anthropic API" not in status.reason
    assert len(calls) == 1 and len(_invocations(marker)) == 1
    _assert_no_token(status, capfd)


def test_claude_code_auth_status_accepts_credentials_without_printing_secret(
    fake_claude, tmp_path, capfd
):
    marker, calls = fake_claude
    path = tmp_path / ".credentials.json"
    path.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": FAKE_TOKEN}}),
        encoding="utf-8",
    )

    status = providers.claude_code_auth_status(credentials_path=path)

    assert status.ok is True
    assert not calls and not _invocations(marker)
    _assert_no_token(status, capfd)


def test_claude_code_auth_status_invalid_credentials_is_refused(
    fake_claude, tmp_path, capfd
):
    marker, calls = fake_claude
    path = tmp_path / ".credentials.json"
    path.write_text("{invalid JSON", encoding="utf-8")

    status = providers.claude_code_auth_status(credentials_path=path)

    assert status.ok is False
    assert "unreadable or invalid JSON" in status.reason
    assert not calls and not _invocations(marker)
    _assert_no_token(status, capfd)


def test_claude_code_auth_status_accepts_env_token_without_smoke(
    fake_claude, monkeypatch, capfd
):
    marker, calls = fake_claude
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)

    status = providers.claude_code_auth_status()

    assert status.ok is True
    assert not calls and not _invocations(marker)
    _assert_no_token(status, capfd)


def test_claude_code_auth_status_cli_managed_login(fake_claude, capfd):
    marker, calls = fake_claude

    status = providers.claude_code_auth_status()

    assert status.ok is True
    assert "CLI-managed login" in status.reason
    assert "macOS Keychain" in status.reason
    assert status.remediation == ""
    assert len(calls) == 1 and len(_invocations(marker)) == 1
    _assert_no_token(status, capfd)


def test_claude_code_auth_status_cli_managed_timeout(
    fake_claude, monkeypatch, capfd
):
    marker, calls = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "2")

    status = providers.claude_code_auth_status(timeout_seconds=0.5)

    assert status.ok is False
    assert "no usable OAuth token or .credentials.json" in status.reason
    assert "smoke check failed or timed out" in status.reason
    assert "TimeoutExpired" in status.reason
    assert "claude login" in status.remediation
    assert len(calls) == 1 and len(_invocations(marker)) == 1
    _assert_no_token(status, capfd)


def test_claude_code_auth_status_cli_managed_oserror(
    fake_claude, monkeypatch, capfd
):
    marker, calls = fake_claude

    def unavailable(*args, **kwargs):
        raise OSError("could not start CLI")

    monkeypatch.setattr(providers.subprocess, "run", unavailable)

    status = providers.claude_code_auth_status()

    assert status.ok is False
    assert "no usable OAuth token or .credentials.json" in status.reason
    assert "smoke check failed or timed out" in status.reason
    assert "OSError" in status.reason
    assert "claude login" in status.remediation
    assert not calls and not _invocations(marker)
    _assert_no_token(status, capfd)


def test_claude_code_auth_status_cli_managed_success_is_cached(fake_claude, capfd):
    marker, calls = fake_claude

    first = providers.claude_code_auth_status()
    second = providers.claude_code_auth_status()

    assert first.ok is True and second == first
    assert len(calls) == 1 and len(_invocations(marker)) == 1
    _assert_no_token(second, capfd)


def test_claude_code_auth_status_cli_managed_failure_is_retried(
    fake_claude, monkeypatch, capfd
):
    marker, calls = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")

    first = providers.claude_code_auth_status()
    second = providers.claude_code_auth_status()

    assert first.ok is False and second.ok is False
    assert len(calls) == 2 and len(_invocations(marker)) == 2
    _assert_no_token(second, capfd)


def test_claude_code_auth_status_explicit_smoke_still_checks_token(
    fake_claude, monkeypatch, capfd
):
    marker, calls = fake_claude
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")

    status = providers.claude_code_auth_status(run_smoke=True)

    assert status.ok is False
    assert "smoke check failed" in status.reason
    assert len(calls) == 1 and len(_invocations(marker)) == 1
    _assert_no_token(status, capfd)


def test_claude_code_auth_status_explicit_smoke_timeout_is_refused(
    fake_claude, monkeypatch, capfd
):
    marker, calls = fake_claude
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "2")

    status = providers.claude_code_auth_status(run_smoke=True, timeout_seconds=0.5)

    assert status.ok is False
    assert "smoke check could not run: TimeoutExpired" in status.reason
    assert len(calls) == 1 and len(_invocations(marker)) == 1
    _assert_no_token(status, capfd)
