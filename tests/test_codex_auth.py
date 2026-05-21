"""Tests for ``mimir.codex_auth`` — Codex Plus OAuth credentials reader."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.codex_auth import (
    CodexAuth,
    auth_file_path,
    codex_home,
    is_likely_expired,
    load_codex_auth,
)


# ─── codex_home / auth_file_path ────────────────────────────────────────


def test_codex_home_defaults_to_home_dot_codex(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert codex_home() == Path.home() / ".codex"


def test_codex_home_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert codex_home() == tmp_path


def test_codex_home_expanduser_on_env(monkeypatch):
    """``CODEX_HOME`` should honor ``~`` expansion the way the CLI does."""
    monkeypatch.setenv("CODEX_HOME", "~/custom-codex")
    assert codex_home() == Path.home() / "custom-codex"


def test_auth_file_path_default(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert auth_file_path() == Path.home() / ".codex" / "auth.json"


# ─── load_codex_auth ────────────────────────────────────────────────────


def _write_auth_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_returns_none_when_file_missing(tmp_path):
    """Operator hasn't run ``codex login`` → no file → return None.
    Don't raise — let caller fall back to the no-Codex code path."""
    assert load_codex_auth(tmp_path / "nope.json") is None


def test_load_full_chatgpt_oauth_bundle(tmp_path):
    """Smoke: real shape from ``codex login`` (verified against the
    live ``auth.json`` produced on 2026-05-20). Fields are intentionally
    short — the parser doesn't care about token contents, only shape."""
    p = tmp_path / "auth.json"
    _write_auth_json(p, {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": "atk-redacted-1973-chars-in-real-life",
            "id_token": "jwt-redacted",
            "refresh_token": "rtk-redacted",
            "account_id": "32280b02-844b-40b4-b050-9df8af9bccc4",
        },
        "last_refresh": "2026-05-21T01:02:18.121084Z",
    })
    auth = load_codex_auth(p)
    assert auth is not None
    assert auth.auth_mode == "chatgpt"
    assert auth.access_token.startswith("atk-")
    assert auth.id_token == "jwt-redacted"
    assert auth.refresh_token == "rtk-redacted"
    assert auth.account_id == "32280b02-844b-40b4-b050-9df8af9bccc4"
    assert auth.last_refresh == datetime(
        2026, 5, 21, 1, 2, 18, 121084, tzinfo=timezone.utc
    )


def test_load_returns_none_when_no_access_token(tmp_path):
    """API-key-only mode (or partially-written file) — no OAuth bundle,
    nothing for OpenAIQuotaProvider to do."""
    p = tmp_path / "auth.json"
    _write_auth_json(p, {
        "auth_mode": "apikey",
        "OPENAI_API_KEY": "sk-test",
        "tokens": None,
    })
    assert load_codex_auth(p) is None


def test_load_tolerates_missing_optional_fields(tmp_path):
    """``id_token``, ``refresh_token``, ``account_id``, ``last_refresh``
    are all optional in the source struct — load shouldn't choke."""
    p = tmp_path / "auth.json"
    _write_auth_json(p, {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "atk-only"},
    })
    auth = load_codex_auth(p)
    assert auth is not None
    assert auth.access_token == "atk-only"
    assert auth.id_token is None
    assert auth.refresh_token is None
    assert auth.account_id is None
    assert auth.last_refresh is None


def test_load_handles_malformed_last_refresh(tmp_path):
    """Don't crash on a garbage timestamp — just lose freshness info."""
    p = tmp_path / "auth.json"
    _write_auth_json(p, {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "atk"},
        "last_refresh": "not-an-iso-date",
    })
    auth = load_codex_auth(p)
    assert auth is not None
    assert auth.last_refresh is None


def test_load_raises_on_invalid_json(tmp_path):
    """File exists but contents are not JSON — that's a real bug,
    surface it instead of silently degrading."""
    p = tmp_path / "auth.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_codex_auth(p)


# ─── is_likely_expired ──────────────────────────────────────────────────


def _auth_with_age(minutes_old: float) -> CodexAuth:
    return CodexAuth(
        auth_mode="chatgpt",
        access_token="atk",
        id_token=None,
        refresh_token=None,
        account_id=None,
        last_refresh=datetime.now(tz=timezone.utc)
        - timedelta(minutes=minutes_old),
    )


def test_likely_expired_when_no_last_refresh():
    """Can't prove freshness → treat as suspect."""
    auth = CodexAuth(
        auth_mode="chatgpt",
        access_token="atk",
        id_token=None,
        refresh_token=None,
        account_id=None,
        last_refresh=None,
    )
    assert is_likely_expired(auth) is True


def test_likely_expired_recent_token_is_fresh():
    assert is_likely_expired(_auth_with_age(1)) is False


def test_likely_expired_old_token_is_stale():
    # Default ttl_minutes=55, so 60min old is over the line.
    assert is_likely_expired(_auth_with_age(60)) is True


def test_likely_expired_respects_ttl_override():
    """Caller can choose a tighter TTL (e.g., for proactive refresh)."""
    auth = _auth_with_age(10)
    assert is_likely_expired(auth, ttl_minutes=5) is True
    assert is_likely_expired(auth, ttl_minutes=15) is False
