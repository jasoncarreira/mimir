"""Tests for the canonical provider registry (chainlink #292).

The registry's behavior-preservation for the two migrated consumers is
covered by ``test_model_registry`` (routing) and ``test_billing``
(quota). These tests pin the registry's own surface: the table
invariants and the two resolution directions.
"""

from __future__ import annotations

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


# ── claude-code availability — spawn_claude_code gate (PR3) ─────────


def test_claude_code_available_reflects_cli_on_path(monkeypatch):
    import mimir.providers as P

    monkeypatch.setattr(
        P.shutil, "which", lambda cmd: "/usr/bin/claude" if cmd == "claude" else None
    )
    assert P.claude_code_available() is True

    monkeypatch.setattr(P.shutil, "which", lambda cmd: None)
    assert P.claude_code_available() is False


def test_claude_code_auth_status_missing_cli_is_actionable(monkeypatch, tmp_path):
    import mimir.providers as P

    monkeypatch.setattr(P.shutil, "which", lambda cmd: None)

    status = P.claude_code_auth_status(credentials_path=tmp_path / ".credentials.json")

    assert status.ok is False
    assert "claude CLI" in status.reason
    assert "npm install -g @anthropic-ai/claude-code" in status.remediation
    assert ".credentials.json contents" in status.remediation
    assert "sk-ant" not in status.reason + status.remediation


def test_claude_code_auth_status_missing_credentials_is_actionable(
    monkeypatch, tmp_path
):
    import mimir.providers as P

    monkeypatch.setattr(
        P.shutil, "which", lambda cmd: "/usr/bin/claude" if cmd == "claude" else None
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    status = P.claude_code_auth_status(credentials_path=tmp_path / "missing.json")

    assert status.ok is False
    assert "no usable OAuth token or .credentials.json" in status.reason
    assert "claude login" in status.remediation
    assert "Anthropic API" not in status.reason


def test_claude_code_auth_status_accepts_credentials_without_printing_secret(
    monkeypatch, tmp_path
):
    import json
    import mimir.providers as P

    monkeypatch.setattr(
        P.shutil, "which", lambda cmd: "/usr/bin/claude" if cmd == "claude" else None
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    secret = "sk-ant-oat01-secret"
    path = tmp_path / ".credentials.json"
    path.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": secret}}),
        encoding="utf-8",
    )

    status = P.claude_code_auth_status(credentials_path=path)

    assert status.ok is True
    assert secret not in status.reason + status.remediation


# ── codex availability — spawn_codex gate (chainlink #293) ──────────


def test_codex_available_reflects_cli_on_path(monkeypatch):
    import mimir.providers as P

    monkeypatch.setattr(
        P.shutil, "which", lambda cmd: "/usr/bin/codex" if cmd == "codex" else None
    )
    assert P.codex_available() is True

    monkeypatch.setattr(P.shutil, "which", lambda cmd: None)
    assert P.codex_available() is False
