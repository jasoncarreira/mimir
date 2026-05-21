"""Codex Plus OAuth credentials reader.

Codex CLI (``openai/codex``) persists OAuth state at
``$CODEX_HOME/auth.json`` (defaults to ``~/.codex/auth.json``) after the
user runs ``codex login`` and goes through the ChatGPT-account browser
flow. The file is owned 0600 and has shape::

    {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": null | "sk-...",
        "tokens": {
            "access_token": "...",
            "id_token": "...",
            "refresh_token": "...",
            "account_id": "..."
        },
        "last_refresh": "2026-05-21T01:02:18Z"
    }

This module is read-only. Refresh is out of scope here — the caller is
expected to either (a) trust that the access token is fresh, (b) shell
out to ``codex auth refresh`` when expiry approaches, or (c) implement
the refresh dance against ChatGPT's OAuth token endpoint as a follow-up
once a Codex Plus LangChain client lives in mimir.

References (verified against ``openai/codex`` source 2026-05-20):

* Storage struct: ``codex-rs/login/src/auth/storage.rs`` (``AuthDotJson``)
* Path resolution: ``codex_home.join("auth.json")``
* Codex API base: ``https://chatgpt.com/backend-api/``
* Probe endpoint: ``/codex/models?client_version=<v>`` (GET, cheap)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


CODEX_API_BASE = "https://chatgpt.com/backend-api"
"""Codex Plus protocol base — distinct from ``api.openai.com``."""


@dataclass(frozen=True)
class CodexAuth:
    """OAuth bundle from ``~/.codex/auth.json``."""

    auth_mode: str
    """``"chatgpt"`` for OAuth (subscription) mode, ``"apikey"`` for
    API-key fallback (rare — Codex CLI primarily targets subscription)."""

    access_token: str
    """Short-lived bearer for ``chatgpt.com/backend-api/...``. Codex
    CLI refreshes it via the ``refresh_token``; consumers should treat
    this as opaque and re-read the file when calls start returning 401."""

    id_token: str | None
    """JWT identifying the ChatGPT account — used in some
    ``agent-identities/*`` flows. Not needed for ``/codex/models`` or
    ``/codex/responses``."""

    refresh_token: str | None
    """Used to obtain a fresh ``access_token`` when the current one
    expires. Refresh flow not implemented in this module — let
    ``codex auth refresh`` handle it."""

    account_id: str | None
    """Stable ChatGPT account identifier."""

    last_refresh: datetime | None
    """When the access_token was last minted. Useful as a coarse
    expiry heuristic (Codex's access tokens have ~1h TTL in practice;
    treat anything older than 55min as suspect)."""


def codex_home() -> Path:
    """Return ``$CODEX_HOME`` or ``~/.codex``. Mirrors codex CLI."""
    env = os.environ.get("CODEX_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


def auth_file_path(home: Path | None = None) -> Path:
    return (home or codex_home()) / "auth.json"


def load_codex_auth(path: Path | None = None) -> CodexAuth | None:
    """Read ``auth.json`` and return a :class:`CodexAuth`, or ``None``
    if the file doesn't exist / is missing the OAuth tokens block.

    Does not raise on missing-file (expected case: operator hasn't
    run ``codex login`` yet). Does raise on malformed JSON or wrong
    shape — those are bugs worth surfacing, not silent degradations.
    """
    p = path or auth_file_path()
    if not p.is_file():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    auth_mode = str(raw.get("auth_mode") or "")
    tokens = raw.get("tokens") or {}
    access_token = tokens.get("access_token")
    if not access_token:
        # File exists but no OAuth bundle (likely API-key-only mode).
        # Surface as None so callers fall back to AnthropicQuotaProvider
        # or pay-as-you-go cost-rate suppression.
        return None
    last_refresh_raw = raw.get("last_refresh")
    last_refresh: datetime | None = None
    if isinstance(last_refresh_raw, str):
        # Codex writes ISO-8601 with trailing Z; Python <3.11 needs
        # an explicit replace.
        try:
            last_refresh = datetime.fromisoformat(
                last_refresh_raw.replace("Z", "+00:00")
            )
        except ValueError:
            last_refresh = None
    return CodexAuth(
        auth_mode=auth_mode,
        access_token=str(access_token),
        id_token=tokens.get("id_token"),
        refresh_token=tokens.get("refresh_token"),
        account_id=tokens.get("account_id"),
        last_refresh=last_refresh,
    )


def is_likely_expired(
    auth: CodexAuth, *, ttl_minutes: int = 55
) -> bool:
    """Coarse expiry heuristic. ChatGPT access tokens have ~1h TTL in
    practice; default to 55min so we're conservative.

    Returns ``True`` if ``last_refresh`` is older than ``ttl_minutes``
    OR if ``last_refresh`` is missing entirely (can't prove freshness).
    """
    if auth.last_refresh is None:
        return True
    age = datetime.now(tz=timezone.utc) - auth.last_refresh
    return age.total_seconds() > ttl_minutes * 60
