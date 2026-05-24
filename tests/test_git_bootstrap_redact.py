"""Tests for the expanded ``_redact`` token-pattern coverage
(pre-OSS hardening, review item #8)."""

from __future__ import annotations

import pytest

from mimir.git_bootstrap import _redact


# ─── github PAT (preserved from original behavior) ───────────────────────


def test_redact_github_classic_pat():
    out = _redact("error: token ghp_abcdef0123456789ABCDEF was rejected")
    assert "ghp_" not in out
    assert "REDACTED" in out


def test_redact_github_fine_grained_pat():
    out = _redact("token github_pat_11ABCDEFG_xyz0123 returned 401")
    assert "github_pat_" not in out
    assert "REDACTED" in out


# ─── anthropic ──────────────────────────────────────────────────────────


def test_redact_anthropic_api_key():
    out = _redact("ANTHROPIC_API_KEY=sk-ant-api03-AbCdEf12_3456-789xyz_long")
    assert "sk-ant-api03-" not in out
    assert "REDACTED" in out


# ─── slack ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", ["xoxb", "xoxp", "xoxa", "xoxs", "xoxr"])
def test_redact_slack_tokens(prefix: str) -> None:
    out = _redact(f"SLACK_BOT_TOKEN={prefix}-1234-5678-AbCdEf-GhIjKlMn")
    assert prefix + "-" not in out
    assert "REDACTED" in out


# ─── openai-style ───────────────────────────────────────────────────────


def test_redact_openai_secret_key():
    """Plain ``sk-`` (not ``sk-ant-``) — OpenAI shape."""
    out = _redact("OPENAI_API_KEY=sk-proj_AbCdEfGh1234567890_ijKlMnOpQrSt")
    assert "sk-proj_" not in out
    assert "REDACTED" in out


# ─── bearer header (preserves prefix) ────────────────────────────────────


def test_redact_bearer_preserves_prefix():
    out = _redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")
    assert "Bearer [REDACTED]" in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_redact_bearer_case_insensitive():
    out = _redact("authorization: bearer abc123def456ghi789")
    assert "REDACTED" in out
    assert "abc123def456ghi789" not in out


# ─── token= / api_key= / password= value-shapes ──────────────────────────


@pytest.mark.parametrize(
    "prefix",
    ["token=", "api_key=", "api-key=", "apikey=", "password=", "passwd=", "secret="],
)
def test_redact_keyvalue_shapes(prefix: str) -> None:
    """The ``key=value`` shape preserves the key, masks the value."""
    src = f"failed: {prefix}supersecretvalue while connecting"
    out = _redact(src)
    assert "supersecretvalue" not in out
    assert prefix in out  # prefix kept
    assert "REDACTED" in out


# ─── empty / no-match passthroughs ───────────────────────────────────────


def test_redact_empty_returns_empty():
    assert _redact("") == ""


def test_redact_none_returns_none():
    """The function preserves the falsy passthrough for ``None``-ish."""
    # _redact's guard treats falsy text as a passthrough.
    assert _redact(None) is None  # type: ignore[arg-type]


def test_redact_innocent_text_unchanged():
    src = "git push to origin/main succeeded after 3 retries"
    assert _redact(src) == src


# ─── env-style dump (the canonical failure mode the review flagged) ──────


def test_redact_env_dump_with_multiple_tokens():
    """The "env | grep TOKEN" failure mode — a single output blob with
    several token shapes. All should be masked."""
    env_dump = (
        "ANTHROPIC_API_KEY=sk-ant-api03-LongValueABCDEFGhijklmn_xyz\n"
        "SLACK_BOT_TOKEN=xoxb-1234-AbCdEfGh_ijklmn\n"
        "GITHUB_TOKEN=ghp_AbCdEf0123456789klMnOpQr\n"
        "DATABASE_URL=postgres://user:secret-pw@host/db\n"
    )
    out = _redact(env_dump)
    assert "sk-ant-api03-" not in out
    assert "xoxb-1234-" not in out
    assert "ghp_AbCdEf" not in out
