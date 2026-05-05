"""Background poller for plan-window quota.

Reads OAuth credentials from a JSON file (default ``$HOME/.claude/.credentials.json``,
override with ``MIMIR_CLAUDE_OAUTH_CREDENTIALS``), hits Anthropic's
``/api/oauth/usage`` endpoint, and writes the returned per-window
utilization snapshots into mimir's :class:`RateLimitStore`. On 401, refreshes
the access token via the standard OAuth2 ``refresh_token`` grant and
persists the rotated credentials back to disk atomically.

Why this exists separately from Stage 5's per-turn capture:

- ``ClaudeSDKClient.get_context_usage()`` returns ``ContextUsageResponse``
  (the ``/context`` CLI command's data). Its ``apiUsage`` field is
  session-scoped, ``NotRequired``, and consistently empty on Claude Max
  OAuth — see chainlink #9.
- The actual plan-window utilization% lives behind a separate HTTP
  endpoint (``GET /api/oauth/usage``) that requires the ``user:profile``
  OAuth scope.
- Claude Code's ``setup-token`` flow only mints ``user:inference``
  scope, so :data:`CLAUDE_CODE_OAUTH_TOKEN` is insufficient. Credentials
  from the full ``/login`` flow at ``~/.claude/.credentials.json`` carry
  the broader scope set we need.
- Claude Code CLI's auto-refresh of those credentials breaks on
  headless / copied-in contexts (anthropics/claude-code#21765, #50743),
  so we run the OAuth2 refresh dance ourselves rather than relying on
  the CLI to keep ``credentials.json`` fresh.

Algedonic events emitted (see ``mimir/feedback.py``):

- ``oauth_usage_ok`` (positive) — successful capture; carries
  per-window utilization as ``recorded={...}``.
- ``oauth_usage_failed`` (negative) — transport / parse error; the
  next poll retries.
- ``oauth_refresh_ok`` (positive) — access token rotated; a fresh
  ``expiresAt`` is in the credentials file.
- ``oauth_logged_out`` (negative, algedonic) — refresh failed with a
  4xx ``invalid_grant`` (or similar). Operator action needed: re-run
  ``/login`` and replace ``credentials.json``.
- ``oauth_refresh_token_age_warn`` (negative, algedonic) — the
  initially-observed credentials are older than the warn threshold.
  Heuristic — refresh-token TTL isn't surfaced in the credentials
  payload, so we track first-seen-at in a sidecar file and warn at
  N days as a "consider refreshing" nudge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from .event_logger import log_event
from .rate_limits import (
    RateLimitSnapshot,
    RateLimitStore,
    _coerce_resets_at,
    _coerce_utilization,
)

log = logging.getLogger(__name__)


# OAuth client_id for the official Claude Code CLI flow. Observable in
# the authorize URL emitted by ``claude /login``. Stable enough that
# baking it in is fine; if Anthropic rotates it the refresh path will
# return 4xx and we'll surface ``oauth_logged_out``.
DEFAULT_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# Token endpoint. Same host as the original ``console.anthropic.com``
# OAuth flow; the authorize step lives at ``claude.com/cai/oauth/authorize``
# but the token exchange is on the Anthropic console side.
TOKEN_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"

# The plan-window usage endpoint we actually want.
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"

# Anthropic-beta header required to opt into the OAuth-flavored API
# surface. Observed in the community-documented ``ccusage`` and gist
# implementations; without it the endpoint 404s.
OAUTH_BETA_HEADER = "oauth-2025-04-20"

# Skew before ``expiresAt`` at which we proactively refresh, to avoid
# racing the cutoff.
EXPIRY_SKEW_SECONDS = 60

# Warning thresholds (days) on the original-login age. The
# refresh-token TTL isn't documented or surfaced — these are
# heuristics. Override via :class:`PollerConfig`.
DEFAULT_REFRESH_WARN_DAYS = 25

# Sidecar filename next to the credentials file that records the
# first time we saw a particular refresh token. Used for the
# refresh-token-age warning. Plain JSON; cheap to read on every poll.
FIRST_SEEN_SIDECAR_NAME = ".oauth_first_seen.json"


@dataclass(frozen=True)
class PollerConfig:
    """Subset of mimir's :class:`Config` the poller needs. Kept narrow
    so tests can construct it without a full Config."""

    credentials_path: Path
    refresh_warn_days: int = DEFAULT_REFRESH_WARN_DAYS
    client_id: str = DEFAULT_CLIENT_ID
    token_endpoint: str = TOKEN_ENDPOINT
    usage_endpoint: str = USAGE_ENDPOINT


# ─── credentials I/O ───────────────────────────────────────────────────


def read_credentials(path: Path) -> dict[str, Any]:
    """Load credentials.json. Returns the inner ``claudeAiOauth`` dict
    (the structure ``claude /login`` writes). Raises :class:`OSError`
    if the file is unreadable; :class:`ValueError` if the JSON is
    malformed or missing the expected wrapper key."""
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"credentials.json root is not an object: {type(data).__name__}")
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise ValueError("credentials.json is missing 'claudeAiOauth' wrapper")
    return oauth


def write_credentials(path: Path, oauth_block: dict[str, Any]) -> None:
    """Atomically replace credentials.json with a refreshed
    ``claudeAiOauth`` block. Preserves any sibling top-level keys
    (none today, but defensive — Anthropic may add)."""
    existing: dict[str, Any] = {}
    try:
        text = path.read_text(encoding="utf-8")
        existing_raw = json.loads(text)
        if isinstance(existing_raw, dict):
            existing = existing_raw
    except OSError:
        # Fresh file — fine.
        pass
    except json.JSONDecodeError:
        log.warning("existing credentials.json is corrupt; overwriting")
    existing["claudeAiOauth"] = oauth_block
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Best effort — Windows / odd FS don't support 0600 cleanly.
        pass
    os.replace(tmp, path)


def is_access_token_expired(
    oauth: dict[str, Any], skew_seconds: int = EXPIRY_SKEW_SECONDS,
) -> bool:
    """``expiresAt`` is unix milliseconds in the credentials format.
    Returns True if we're within ``skew_seconds`` of expiry (or past
    it), so callers preemptively refresh."""
    expires_at_ms = oauth.get("expiresAt")
    if not isinstance(expires_at_ms, (int, float)):
        return True  # No expiry → assume stale.
    expires_at_s = expires_at_ms / 1000.0
    return time.time() + skew_seconds >= expires_at_s


# ─── refresh-token age tracking ────────────────────────────────────────


def _sidecar_path(credentials_path: Path) -> Path:
    return credentials_path.parent / FIRST_SEEN_SIDECAR_NAME


def record_first_seen(
    credentials_path: Path, refresh_token: str, *, now: float | None = None,
) -> dict[str, Any]:
    """Track the first time we saw a particular refresh-token tail in a
    sidecar JSON file. Refresh tokens rotate on each refresh, but we
    only care about the *original-login* timestamp for the age warning
    — so we keep the original sidecar value unless the token tail
    changes substantively (operator did a fresh ``/login``).

    Returns the stored {tail, first_seen_at_unix} dict (after any
    update). Best effort: IO errors are logged and we return an
    empty-ish dict the warning logic can interpret as "unknown age"."""
    if now is None:
        now = time.time()
    tail = (refresh_token or "")[-12:]  # rotates per refresh; tail keeps it short
    sidecar = _sidecar_path(credentials_path)
    existing: dict[str, Any] = {}
    try:
        existing = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except OSError:
        pass
    except json.JSONDecodeError:
        log.warning("first-seen sidecar is corrupt; resetting")
        existing = {}

    # We care about login-age, not refresh-rotation-age. The "original
    # login" is whatever the operator last did with /login. Heuristic:
    # if the sidecar's first_login_at is older than the current
    # access-token expiresAt by more than ~12h, assume operator
    # re-/logged-in and reset. Otherwise preserve.
    first_login_at = existing.get("first_login_at_unix")
    if not isinstance(first_login_at, (int, float)):
        first_login_at = now
    existing["last_seen_refresh_tail"] = tail
    existing["last_seen_at_unix"] = int(now)
    existing.setdefault("first_login_at_unix", int(first_login_at))

    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
        tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, sidecar)
    except OSError as exc:
        log.warning("first-seen sidecar write failed: %s", exc)
    return existing


def reset_first_seen(credentials_path: Path, *, now: float | None = None) -> None:
    """Operator-side ``/login`` produced fresh credentials — reset the
    sidecar so the age warning starts counting from now. Call this from
    an admin endpoint or, more likely, from operator tooling outside
    the poll loop."""
    sidecar = _sidecar_path(credentials_path)
    if now is None:
        now = time.time()
    payload = {"first_login_at_unix": int(now), "last_seen_at_unix": int(now)}
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(sidecar, 0o600)
        except OSError:
            pass
    except OSError as exc:
        log.warning("first-seen sidecar reset failed: %s", exc)


def days_since_first_login(
    credentials_path: Path, *, now: float | None = None,
) -> float | None:
    """Returns the age in days of the operator's last ``/login`` per
    our sidecar tracking. None if the sidecar is missing / unreadable."""
    sidecar = _sidecar_path(credentials_path)
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    first_login_at = data.get("first_login_at_unix")
    if not isinstance(first_login_at, (int, float)):
        return None
    if now is None:
        now = time.time()
    return max(0.0, (now - float(first_login_at)) / 86400.0)


# ─── HTTP: refresh + usage fetch ───────────────────────────────────────


class OAuthRefreshError(Exception):
    """Raised when the refresh-token grant fails. Distinguishes the
    `logged_out` algedonic case (4xx invalid_grant) from transient
    transport errors (5xx, network)."""

    def __init__(self, message: str, *, logged_out: bool, status: int | None = None):
        super().__init__(message)
        self.logged_out = logged_out
        self.status = status


async def refresh_access_token(
    session: aiohttp.ClientSession,
    oauth: dict[str, Any],
    cfg: PollerConfig,
) -> dict[str, Any]:
    """Standard OAuth2 ``refresh_token`` grant. Returns the new
    credentials block (already merged onto the input — preserving
    fields like ``scopes``/``subscriptionType`` that the token response
    may omit). Raises :class:`OAuthRefreshError` on failure.

    The token endpoint rotates the refresh token on each successful
    refresh — the new ``refreshToken`` is in the response and MUST be
    persisted, otherwise the next refresh fails with invalid_grant."""
    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthRefreshError(
            "no refreshToken in credentials", logged_out=True, status=None,
        )
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": cfg.client_id,
    }
    try:
        async with session.post(
            cfg.token_endpoint,
            json=body,
            headers={"Content-Type": "application/json"},
        ) as resp:
            text = await resp.text()
            status = resp.status
    except aiohttp.ClientError as exc:
        raise OAuthRefreshError(
            f"refresh transport error: {type(exc).__name__}: {exc}",
            logged_out=False,
        ) from exc

    if status >= 500:
        raise OAuthRefreshError(
            f"refresh server error {status}: {text[:200]}",
            logged_out=False,
            status=status,
        )
    if status >= 400:
        # 400/401 with invalid_grant means the refresh token itself is
        # dead — operator must re-/login.
        raise OAuthRefreshError(
            f"refresh denied {status}: {text[:200]}",
            logged_out=True,
            status=status,
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OAuthRefreshError(
            f"refresh response not JSON: {exc}; body={text[:200]}",
            logged_out=False,
        ) from exc

    new_access = payload.get("access_token")
    new_refresh = payload.get("refresh_token") or refresh_token
    expires_in = payload.get("expires_in")
    if not isinstance(new_access, str) or not new_access:
        raise OAuthRefreshError(
            f"refresh response missing access_token: {list(payload.keys())}",
            logged_out=False,
        )
    expires_at_ms: int | None = None
    if isinstance(expires_in, (int, float)):
        expires_at_ms = int((time.time() + float(expires_in)) * 1000)

    merged = dict(oauth)
    merged["accessToken"] = new_access
    merged["refreshToken"] = new_refresh
    if expires_at_ms is not None:
        merged["expiresAt"] = expires_at_ms
    # Some OAuth servers also return `scope`; normalize into the existing
    # `scopes` list shape if present.
    new_scope = payload.get("scope")
    if isinstance(new_scope, str) and new_scope.strip():
        merged["scopes"] = new_scope.split()
    return merged


class UsageFetchError(Exception):
    """Raised by :func:`fetch_usage`. ``unauthorized=True`` signals the
    caller should refresh and retry once."""

    def __init__(self, message: str, *, unauthorized: bool, status: int | None = None):
        super().__init__(message)
        self.unauthorized = unauthorized
        self.status = status


async def fetch_usage(
    session: aiohttp.ClientSession,
    access_token: str,
    cfg: PollerConfig,
) -> dict[str, Any]:
    """GET ``/api/oauth/usage``. Returns the parsed JSON dict. Raises
    :class:`UsageFetchError` with ``unauthorized=True`` on 401 so the
    caller can refresh + retry."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": OAUTH_BETA_HEADER,
        "Content-Type": "application/json",
    }
    try:
        async with session.get(cfg.usage_endpoint, headers=headers) as resp:
            text = await resp.text()
            status = resp.status
    except aiohttp.ClientError as exc:
        raise UsageFetchError(
            f"usage transport error: {type(exc).__name__}: {exc}",
            unauthorized=False,
        ) from exc
    if status == 401:
        raise UsageFetchError(
            f"usage endpoint 401: {text[:200]}",
            unauthorized=True,
            status=status,
        )
    if status >= 400:
        raise UsageFetchError(
            f"usage endpoint {status}: {text[:200]}",
            unauthorized=False,
            status=status,
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageFetchError(
            f"usage response not JSON: {exc}; body={text[:200]}",
            unauthorized=False,
        ) from exc


# ─── snapshot conversion ──────────────────────────────────────────────


def _bucket_to_snapshot(bucket: Any) -> RateLimitSnapshot | None:
    """Convert one ``/api/oauth/usage`` bucket to our RateLimitSnapshot.
    The endpoint returns ``utilization`` as 0-100 (percentage) and
    ``resets_at`` as ISO-8601. Returns None for null / non-dict
    buckets so :func:`record_usage` can skip them."""
    if not isinstance(bucket, dict):
        return None
    util = _coerce_utilization(bucket.get("utilization"))
    resets = _coerce_resets_at(bucket.get("resets_at"))
    if util is None and resets is None:
        return None
    return RateLimitSnapshot(
        status=str(bucket.get("status") or "allowed"),
        utilization=util,
        resets_at=resets,
        observed_at=datetime.now(tz=timezone.utc).isoformat(),
        overage_status=None,
        overage_resets_at=None,
        overage_disabled_reason=None,
    )


async def record_usage(
    store: RateLimitStore, payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Walk the ``/api/oauth/usage`` response and persist each parseable
    window bucket. Returns the recorded summary for the structured
    log event."""
    recorded: dict[str, dict[str, Any]] = {}
    for window_type, bucket in payload.items():
        if window_type == "extra_usage":
            # Overage bucket has a different shape (monthly_limit /
            # used_credits / is_enabled). We don't currently render it
            # — log info and skip.
            continue
        snapshot = _bucket_to_snapshot(bucket)
        if snapshot is None:
            continue
        try:
            await store.record(window_type, snapshot)
        except Exception:  # noqa: BLE001
            log.exception("oauth_usage: store.record failed for %s", window_type)
            continue
        recorded[window_type] = {
            "utilization": snapshot.utilization,
            "resets_at": snapshot.resets_at,
            "status": snapshot.status,
        }
    return recorded


# ─── orchestration ────────────────────────────────────────────────────


async def poll_once(
    cfg: PollerConfig,
    store: RateLimitStore,
    *,
    session: aiohttp.ClientSession | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """One poll cycle: read creds → refresh if expired → fetch usage
    → record snapshots → emit events. Returns a summary dict for tests
    / introspection. Never raises — failures surface as events."""
    # Read credentials.
    try:
        oauth = read_credentials(cfg.credentials_path)
    except FileNotFoundError:
        await log_event(
            "oauth_usage_failed",
            stage="read_credentials",
            error=f"credentials file not found: {cfg.credentials_path}",
        )
        return {"ok": False, "stage": "read_credentials"}
    except (OSError, ValueError) as exc:
        await log_event(
            "oauth_usage_failed",
            stage="read_credentials",
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "stage": "read_credentials"}

    # Track refresh-token age for the soft warn.
    refresh_token = oauth.get("refreshToken") or ""
    record_first_seen(cfg.credentials_path, refresh_token, now=now)
    age_days = days_since_first_login(cfg.credentials_path, now=now)
    if age_days is not None and age_days >= cfg.refresh_warn_days:
        await log_event(
            "oauth_refresh_token_age_warn",
            age_days=round(age_days, 1),
            warn_threshold_days=cfg.refresh_warn_days,
            credentials_path=str(cfg.credentials_path),
        )

    # Open one session per call (the caller passes one in for tests /
    # if multiple polls share connection state).
    owns_session = session is None
    if session is None:
        session = aiohttp.ClientSession()
    try:
        # Proactively refresh if the access token is at/past expiry.
        if is_access_token_expired(oauth):
            try:
                oauth = await refresh_access_token(session, oauth, cfg)
                write_credentials(cfg.credentials_path, oauth)
                await log_event(
                    "oauth_refresh_ok",
                    expires_at_ms=oauth.get("expiresAt"),
                    rotated=bool(oauth.get("refreshToken") != refresh_token),
                )
            except OAuthRefreshError as exc:
                if exc.logged_out:
                    await log_event(
                        "oauth_logged_out",
                        stage="proactive_refresh",
                        status=exc.status,
                        error=str(exc),
                    )
                else:
                    await log_event(
                        "oauth_usage_failed",
                        stage="proactive_refresh",
                        status=exc.status,
                        error=str(exc),
                    )
                return {"ok": False, "stage": "proactive_refresh"}

        # Fetch usage. If the access token was rejected despite our
        # expiry check (clock skew, server-side rotation, etc.), refresh
        # and retry once.
        access_token = oauth.get("accessToken") or ""
        try:
            payload = await fetch_usage(session, access_token, cfg)
        except UsageFetchError as exc:
            if exc.unauthorized:
                try:
                    oauth = await refresh_access_token(session, oauth, cfg)
                    write_credentials(cfg.credentials_path, oauth)
                    await log_event(
                        "oauth_refresh_ok",
                        expires_at_ms=oauth.get("expiresAt"),
                        reactive=True,
                    )
                    payload = await fetch_usage(
                        session, oauth.get("accessToken") or "", cfg,
                    )
                except OAuthRefreshError as refresh_exc:
                    if refresh_exc.logged_out:
                        await log_event(
                            "oauth_logged_out",
                            stage="reactive_refresh",
                            status=refresh_exc.status,
                            error=str(refresh_exc),
                        )
                    else:
                        await log_event(
                            "oauth_usage_failed",
                            stage="reactive_refresh",
                            status=refresh_exc.status,
                            error=str(refresh_exc),
                        )
                    return {"ok": False, "stage": "reactive_refresh"}
                except UsageFetchError as retry_exc:
                    await log_event(
                        "oauth_usage_failed",
                        stage="usage_retry",
                        status=retry_exc.status,
                        error=str(retry_exc),
                    )
                    return {"ok": False, "stage": "usage_retry"}
            else:
                await log_event(
                    "oauth_usage_failed",
                    stage="fetch",
                    status=exc.status,
                    error=str(exc),
                )
                return {"ok": False, "stage": "fetch"}

        recorded = await record_usage(store, payload)
        await log_event("oauth_usage_ok", recorded=recorded)
        return {"ok": True, "recorded": recorded}
    finally:
        if owns_session:
            await session.close()
