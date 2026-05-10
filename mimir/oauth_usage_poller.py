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
    so tests can construct it without a full Config.

    ``turns_log_path`` (chainlink #17): when set, the cost-rate-back-
    derived 5h estimator runs on layer-(a) anomaly rejection — instead
    of just keeping the prior trusted 5h reading, the poller computes
    a fresh estimate from the last 5h of cost in turns.jsonl divided
    by a 7d quota back-derived from the prior 7d reading. None
    disables the derive path (current behavior: prior trusted value
    persists indefinitely on long endpoint glitches)."""

    credentials_path: Path
    refresh_warn_days: int = DEFAULT_REFRESH_WARN_DAYS
    client_id: str = DEFAULT_CLIENT_ID
    token_endpoint: str = TOKEN_ENDPOINT
    usage_endpoint: str = USAGE_ENDPOINT
    turns_log_path: Path | None = None


# ─── credentials I/O ───────────────────────────────────────────────────


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as pretty JSON to ``path`` durably.

    CR#7: the previous shape used ``Path.write_text(...)`` + ``os.replace``
    without an ``fsync`` on the temp file, so a crash between rename-commit
    and content-flush left ``path`` pointing at a zero-length region. The
    credentials file's refresh token rotates on every refresh — losing a
    write here forces an operator re-``/login``. Same shape applies to the
    sidecar (less catastrophic, but corrupting it triggered the CR#8 bug
    where the age-warn countdown silently reset).

    The standard atomic-write pattern: open tmp, write, ``fsync`` the file,
    close, ``rename``, then ``fsync`` the parent directory so the rename
    itself is durable. Mode 0o600 matches the prior chmod-after-write
    behavior.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = json.dumps(payload, indent=2).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    # Make the rename durable. Without this, on crash between rename and
    # parent-dir writeback, the new entry can revert to the pre-rename
    # state. Parent-dir fsync is cheap (single block usually) and the
    # only way to actually commit the rename. Best-effort — Windows /
    # exotic FS may reject O_RDONLY on directories.
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


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
    _atomic_write_json(path, existing)


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
        # Fresh / missing sidecar — proceed with empty existing; we'll
        # populate first_login_at_unix=now below.
        pass
    except json.JSONDecodeError:
        # CR#8: a corrupt sidecar must NOT silently reset the first-login
        # timestamp. The previous code reset existing={} and then wrote a
        # fresh sidecar with first_login_at_unix=now, restarting the
        # 30-day refresh-token age-warn countdown without operator
        # awareness. Treat corruption as a hard error: log + skip the
        # write entirely. ``days_since_first_login`` already returns None
        # on the same JSONDecodeError, so the age-warn won't fire — which
        # is the right behavior (we don't know the age) until the
        # operator investigates the corruption (re-``/login`` or restore
        # from backup will write a clean sidecar).
        log.warning(
            "first-seen sidecar at %s is corrupt; skipping update to "
            "preserve unknown-age signal. Operator should investigate.",
            sidecar,
        )
        return {"corrupt": True}

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
        _atomic_write_json(sidecar, existing)
    except OSError as exc:
        log.warning("first-seen sidecar write failed: %s", exc)
    return existing


# CR2 (external I/O) fix: sticky logged_out + throttled reminder.
# Pre-fix, once ``oauth_logged_out`` fired (refresh token revoked /
# ``invalid_grant``), every subsequent cron tick retried the whole
# refresh flow — generating a new ``oauth_logged_out`` event per tick
# (typically every minute on the default cron). The algedonic block's
# "Recent feedback signals" surface drowned in the same negative
# repeatedly, drowning out other signals; the token endpoint also got
# hit needlessly. After the operator sees one ``oauth_logged_out``
# event, all they need is a periodic reminder that the agent is still
# in the logged-out state — until they re-run ``/login``.
_LOGGED_OUT_REMINDER_INTERVAL_SECONDS = 3600  # one reminder per hour


def is_known_logged_out(
    credentials_path: Path,
) -> tuple[bool, float | None, float | None]:
    """Read the sidecar; return ``(is_logged_out, logged_out_since_unix,
    last_reminder_at_unix)``. ``is_logged_out`` reflects
    ``oauth_logged_out`` having fired with no successful refresh
    since. Both timestamps may be None when not set. Returns
    ``(False, None, None)`` for any missing / unreadable / corrupt
    sidecar — defaults to "not in known logged-out state" so the
    regular flow runs."""
    sidecar = _sidecar_path(credentials_path)
    try:
        existing = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            return False, None, None
    except (OSError, json.JSONDecodeError):
        return False, None, None
    logged_out_since = existing.get("logged_out_since_unix")
    if not isinstance(logged_out_since, (int, float)):
        return False, None, None
    last_reminder = existing.get("logged_out_last_reminder_unix")
    if not isinstance(last_reminder, (int, float)):
        last_reminder = None
    return True, float(logged_out_since), last_reminder


def mark_logged_out(
    credentials_path: Path, *, now: float | None = None,
) -> None:
    """Stamp the sidecar with ``logged_out_since_unix`` (if not already
    set) and ``logged_out_last_reminder_unix`` = now. Best-effort; IO
    failures log a warning."""
    if now is None:
        now = time.time()
    sidecar = _sidecar_path(credentials_path)
    existing: dict[str, Any] = {}
    try:
        existing = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    existing.setdefault("logged_out_since_unix", int(now))
    existing["logged_out_last_reminder_unix"] = int(now)
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(sidecar, existing)
    except OSError as exc:
        log.warning("logged_out sidecar write failed: %s", exc)


def clear_logged_out(credentials_path: Path) -> None:
    """Clear the sticky logged-out state from the sidecar. Called when
    a refresh succeeds (``oauth_refresh_ok``) — the operator re-ran
    ``/login`` and the next tick caught up."""
    sidecar = _sidecar_path(credentials_path)
    try:
        existing = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            return
    except (OSError, json.JSONDecodeError):
        return
    if "logged_out_since_unix" not in existing and "logged_out_last_reminder_unix" not in existing:
        return  # already clean — skip the write
    existing.pop("logged_out_since_unix", None)
    existing.pop("logged_out_last_reminder_unix", None)
    try:
        _atomic_write_json(sidecar, existing)
    except OSError as exc:
        log.warning("clear_logged_out sidecar write failed: %s", exc)


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
        _atomic_write_json(sidecar, payload)
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


# Cross-window anomaly thresholds (CR#22 layer a). The 5h window is a
# *subset* of the 7d window — so a real 50pp jump in 5h must show up
# as a corresponding 7d delta. When 7d barely moves while 5h spikes
# (observed live: 7%→100% in 3 minutes with 7d steady at 49%), the
# endpoint is reporting noise. Skipping the write keeps the prior
# trusted value in the store so the arbiter doesn't suppress
# scheduled work on bogus data.
ANOMALY_5H_JUMP_PP = 0.50   # 5h utilization rise that triggers cross-check
ANOMALY_7D_DELTA_PP = 0.05  # 7d delta below this confirms the 5h jump is bogus

# chainlink #17 (CR#22 layer b): cost-rate-back-derived 5h estimator.
#
# The factor encodes how much smaller the 5h dollar-budget is than the
# 7d budget on a Max-20x plan. Two empirical samples (2026-05-09):
#
#   sample 1 (21:54Z): 5h_util=0.20, 5h_cost=$247.70,
#       7d_util=0.23, 7d_cost=$2608.21
#       → factor = 0.20 × (2608.21/0.23) / 247.70 ≈ 9.18
#
#   sample 2 (22:03Z): 5h_util=0.23, 5h_cost=$263.14,
#       7d_util=0.23, 7d_cost=$2623.65
#       → factor = 0.23 × (2623.65/0.23) / 263.14 ≈ 9.97
#
# Both bracket ~10×, consistent with Anthropic's published shape on
# the Max-20x plan ("5h cap fits ~5h of Sonnet 4 nonstop; 7d cap fits
# ~50-70h" → 7d/5h_quota ≈ 10-14×). The earlier "1.4× pro-rata"
# claim from chainlink #17's spec was wrong — Mimir's PR #89 review
# (2026-05-09) caught the formula/docstring divergence + did the
# empirical back-derivation that led to this corrected constant.
#
# Equivalent interpretation: 5h_quota_$ ≈ 7d_quota_$ / FACTOR. The
# formula in derive_5h_from_cost uses this directly:
# ``estimated_5h_util = (5h_cost × FACTOR) / 7d_quota_$`` is just
# ``5h_cost / 5h_quota_$`` rewritten.
#
# Operator override: ``MIMIR_QUOTA_5H_BACKDERIVE_FACTOR`` env var.
# Re-derive against fresh telemetry on plan-tier changes. The constant
# couples directly to ``DEFAULT_RAW_SUPPRESS_DERIVED`` in
# mimir/billing.py — both encode trust in derived values; if the
# factor's accuracy band shifts (different plan, different
# telemetry-confirmed value), the suppress threshold may need to
# move with it.
QUOTA_5H_BACKDERIVE_FACTOR_DEFAULT = 10.0
# Round derived utilization to nearest 5pp. Acknowledges the math is
# approximate (back-derived 7d quota dollars + flat factor + turns.jsonl
# cost aggregation slop) without being so coarse that 0.78→0.82
# transitions get smeared into the same bucket.
DERIVE_ROUND_STEP = 0.05


def _resolve_backderive_factor() -> float:
    """Resolve ``MIMIR_QUOTA_5H_BACKDERIVE_FACTOR`` env override or
    fall back to the empirical default. Empty / non-positive / non-
    numeric values fall back with a warning so a typo doesn't silently
    kill the estimator."""
    raw = os.environ.get("MIMIR_QUOTA_5H_BACKDERIVE_FACTOR", "").strip()
    if not raw:
        return QUOTA_5H_BACKDERIVE_FACTOR_DEFAULT
    try:
        v = float(raw)
        if v > 0:
            return v
    except ValueError:
        pass
    log.warning(
        "MIMIR_QUOTA_5H_BACKDERIVE_FACTOR=%r invalid (expected positive "
        "float); falling back to default %s",
        raw, QUOTA_5H_BACKDERIVE_FACTOR_DEFAULT,
    )
    return QUOTA_5H_BACKDERIVE_FACTOR_DEFAULT


def detect_5h_anomaly(
    new_5h: float | None,
    prev_5h: float | None,
    new_7d: float | None,
    prev_7d: float | None,
) -> str | None:
    """Cross-window sanity check on the 5h reading. Returns a reason
    string if the new 5h is anomalous (large jump unmatched by 7d
    response), else None.

    Rule:
      - 5h rose by >= ANOMALY_5H_JUMP_PP (50pp) since prior reading
      - AND 7d delta over the same interval is < ANOMALY_7D_DELTA_PP (5pp)

    The 5h window is contained within the 7d window: every dollar that
    counts toward 5h also counts toward 7d. So a 50pp 5h jump implies
    spending equal to 50% of the 5h quota, which is some non-trivial
    fraction of the 7d quota too — 7d should move accordingly. When
    it doesn't (deltas <5pp), the 5h reading is internally inconsistent
    with the 7d trajectory and almost certainly an endpoint glitch.

    Skips the check when either side is None — first poll, missing
    data, or a 7d-less response. "Trust the value" is the right
    fallback because we have no signal to distrust it.
    """
    if new_5h is None or prev_5h is None:
        return None
    jump = new_5h - prev_5h
    if jump < ANOMALY_5H_JUMP_PP:
        return None
    if new_7d is None or prev_7d is None:
        return None
    sevenday_delta = abs(new_7d - prev_7d)
    if sevenday_delta >= ANOMALY_7D_DELTA_PP:
        return None
    return (
        f"five_hour jumped +{jump * 100:.0f}pp "
        f"({prev_5h * 100:.0f}% → {new_5h * 100:.0f}%) "
        f"but seven_day only moved {sevenday_delta * 100:.1f}pp — "
        f"endpoint glitch suspected"
    )


def derive_5h_from_cost(
    turns_log_path: Path,
    *,
    prior_7d_utilization: float,
    backderive_factor: float | None = None,
) -> float | None:
    """Estimate 5h utilization from observed cost when the endpoint
    reading is unavailable / anomalous (chainlink #17 CR#22 layer b).

    Math::

        back_derived_7d_quota_$ = observed_7d_cost / observed_7d_util
        estimated_5h_util       = (observed_5h_cost × FACTOR)
                                / back_derived_7d_quota_$

    Where ``FACTOR ≈ 10`` (= ``QUOTA_5H_BACKDERIVE_FACTOR_DEFAULT``)
    encodes the empirical 5h:7d dollar-budget ratio on the Max-20x
    plan: 5h_quota_$ ≈ 7d_quota_$ / 10. See the constant's block
    comment for empirical derivation + override env var.

    ``backderive_factor=None`` reads the env-resolved value; tests
    pass an explicit value to pin the math.

    Output is clamped to ``[0, 1]`` and rounded to the nearest 5pp
    so it's a stable signal rather than jitter.

    Returns ``None`` when the math can't run:
      - ``prior_7d_utilization`` outside ``(0, 1]`` (zero / negative
        / impossibly-large; without it, back-deriving the 7d quota
        dollar-budget is impossible)
      - turns.jsonl missing or zero observed 7d cost (back-derived
        quota would be 0 or undefined)
      - aggregate raises (turns.jsonl corrupt / partial)

    None is the right "no signal" fallback — the caller (record_usage)
    keeps the prior trusted 5h value rather than synthesizing a
    plausible-but-wrong estimate.
    """
    if not (0.0 < prior_7d_utilization <= 1.0):
        return None
    factor = (
        backderive_factor if backderive_factor is not None
        else _resolve_backderive_factor()
    )
    if factor <= 0:
        return None
    try:
        # Lazy import: defers the (cheap but non-zero) module load
        # off the poller's cold-start path. ``usage_stats`` doesn't
        # import this module so there's no cycle to avoid; the late
        # bind is purely about startup-time hygiene.
        from .usage_stats import aggregate
        report = aggregate(
            turns_log_path,
            window_hours=(5.0, 24.0 * 7),
            window_labels=("5h_cost", "7d_cost"),
        )
    except Exception:  # noqa: BLE001 — never crash the poll cycle
        log.exception("derive_5h_from_cost: aggregate failed")
        return None

    if len(report.windows) < 2:
        return None
    last_5h_cost = report.windows[0].total_cost_usd
    last_7d_cost = report.windows[1].total_cost_usd
    if last_7d_cost <= 0:
        return None
    back_derived_7d_quota = last_7d_cost / prior_7d_utilization
    if back_derived_7d_quota <= 0:
        return None
    estimated = (last_5h_cost * factor) / back_derived_7d_quota
    estimated = max(0.0, min(1.0, estimated))
    # Round to nearest DERIVE_ROUND_STEP, half-up. Two issues with the
    # naive ``round(x / step) * step``: (1) Python's banker's rounding
    # (``round(3.5) == 4`` but ``round(2.5) == 2``), and (2) float
    # precision — ``0.175 / 0.05 == 3.4999...`` so plain ``round``
    # returns 3 (→ 0.15) instead of 4 (→ 0.20). Decimal with the
    # input as a string sidesteps both quirks: exact arithmetic +
    # explicit ROUND_HALF_UP semantics.
    from decimal import Decimal, ROUND_HALF_UP
    steps_per_unit = int(round(1.0 / DERIVE_ROUND_STEP))  # 0.05 → 20
    scaled = (Decimal(str(estimated)) * steps_per_unit).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP,
    )
    return float(scaled / steps_per_unit)


async def record_usage(
    store: RateLimitStore,
    payload: dict[str, Any],
    *,
    cfg: PollerConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Walk the ``/api/oauth/usage`` response and persist each parseable
    window bucket. Returns the recorded summary for the structured
    log event.

    CR#22 layer a: before writing, the 5h reading is cross-checked
    against the 7d delta. When the 5h jumps implausibly (a >=50pp
    rise unmatched by a corresponding 7d move), the new reading is
    rejected — the prior trustworthy value stays in the store, a
    ``quota_reading_anomalous`` event fires for the algedonic surface,
    and the recorded summary marks the rejection. The arbiter then
    sees the prior (trusted) utilization rather than the bogus spike,
    avoiding the hours-of-suppression-on-bad-data failure we observed
    twice in 48h. Other windows (7d, 7d_sonnet, etc.) write through
    unchanged — only the 5h direction is asymmetric enough to warrant
    rejection.

    chainlink #17 (CR#22 layer b): when ``cfg.turns_log_path`` is set
    and the layer-(a) detector rejects a 5h reading, the framework
    additionally tries to compute a cost-rate-back-derived 5h
    utilization estimate. On success, the derived value lands in the
    store with ``derived=True``, a ``quota_5h_derived`` event fires,
    and the arbiter (mimir/billing.py) applies a 90% suppress
    threshold (vs 80% for direct readings) to absorb the estimate's
    slop. On derive failure, the prior trusted 5h value persists
    (current layer-a behavior).
    """
    recorded: dict[str, dict[str, Any]] = {}

    # Build the new snapshots first so the 5h-vs-7d cross-check has
    # access to both before either is written.
    prior_snaps = store.current()
    new_snaps: dict[str, RateLimitSnapshot] = {}
    for window_type, bucket in payload.items():
        if window_type == "extra_usage":
            # Overage bucket has a different shape (monthly_limit /
            # used_credits / is_enabled). We don't currently render
            # it — skip.
            continue
        snapshot = _bucket_to_snapshot(bucket)
        if snapshot is not None:
            new_snaps[window_type] = snapshot

    new_5h = new_snaps.get("five_hour")
    prior_5h = prior_snaps.get("five_hour")
    new_7d = new_snaps.get("seven_day")
    prior_7d = prior_snaps.get("seven_day")
    anomaly_reason = detect_5h_anomaly(
        new_5h.utilization if new_5h else None,
        prior_5h.utilization if prior_5h else None,
        new_7d.utilization if new_7d else None,
        prior_7d.utilization if prior_7d else None,
    )

    for window_type, snapshot in new_snaps.items():
        if window_type == "five_hour" and anomaly_reason:
            # Reject the spike. Surface the rejection so the operator
            # (and the agent's algedonic block) can investigate.
            await log_event(
                "quota_reading_anomalous",
                window_type=window_type,
                reason=anomaly_reason,
                rejected_utilization=snapshot.utilization,
                kept_utilization=prior_5h.utilization if prior_5h else None,
                kept_observed_at=prior_5h.observed_at if prior_5h else None,
                seven_day_prev=prior_7d.utilization if prior_7d else None,
                seven_day_new=new_7d.utilization if new_7d else None,
            )
            # chainlink #17: try the cost-rate-back-derived estimator
            # before falling back to the prior trusted value. The
            # derived snapshot lives in the same slot but is flagged
            # ``derived=True`` so the arbiter applies a 90% suppress
            # threshold instead of the direct 80%. If derive fails
            # (no turns_log wired, no observable 7d cost, no prior 7d
            # utilization), the prior trusted value persists — current
            # layer-(a) behavior.
            # Prefer the just-arrived 7d reading (Mimir's PR #89 nit
            # #2): the layer-(a) cross-check above only fires when the
            # 7d half moved <5pp, which means the 7d side of THIS
            # poll's payload is presumed trustworthy — anomaly is
            # specifically the 5h jump unmatched by 7d. Using the
            # fresher reading gives a sharper back-derived quota.
            # Fall back to prior_7d when new_7d is missing (rare —
            # 7d-less response).
            seven_day_for_derive = (
                new_7d if (new_7d is not None
                           and new_7d.utilization is not None)
                else prior_7d
            )
            derived_util = None
            if (
                cfg is not None
                and cfg.turns_log_path is not None
                and seven_day_for_derive is not None
                and seven_day_for_derive.utilization is not None
            ):
                derived_util = derive_5h_from_cost(
                    cfg.turns_log_path,
                    prior_7d_utilization=seven_day_for_derive.utilization,
                )
            if derived_util is not None:
                # ``resets_at=None`` is unconditional for derived
                # snapshots. Two reasons: (1) we don't actually know
                # when the real 5h window resets — we never got a
                # successful endpoint reading this poll, so any
                # inherited value would be a guess based on the
                # glitch-pre-existing read. (2) Inheriting a
                # window-resets-at value that later goes stale would
                # cause ``RateLimitStore.current()`` to filter the
                # derived snapshot out (it drops entries with
                # ``resets_at < now``), silently evicting our derived
                # signal during long glitches that cross a window
                # boundary. ``None`` survives that filter
                # unconditionally and the arbiter handles missing
                # window-timing as "no time signal" (on-pace
                # projection is already skipped for derived in
                # AnthropicQuotaProvider).
                # Status reflects the derived value rather than a
                # fixed string (Mimir's PR #89 nit #3): "allowed" when
                # below the warn threshold, "allowed_warning" once the
                # value crosses it. Mirrors what an endpoint reading
                # would carry; prevents downstream consumers that key
                # off ``status`` from being misled when the derived
                # value is low.
                derived_status = (
                    "allowed_warning" if derived_util >= 0.50 else "allowed"
                )
                derived_snap = RateLimitSnapshot(
                    status=derived_status,
                    utilization=derived_util,
                    resets_at=None,
                    observed_at=datetime.now(tz=timezone.utc).isoformat(),
                    derived=True,
                )
                try:
                    await store.record(window_type, derived_snap)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "oauth_usage: store.record failed for derived %s",
                        window_type,
                    )
                else:
                    await log_event(
                        "quota_5h_derived",
                        utilization=derived_util,
                        seven_day_utilization=(
                            seven_day_for_derive.utilization
                            if seven_day_for_derive is not None else None
                        ),
                        seven_day_source=(
                            "new" if seven_day_for_derive is new_7d
                            else "prior"
                        ),
                        anomaly_reason=anomaly_reason,
                    )
                    recorded[window_type] = {
                        "derived": True,
                        "utilization": derived_util,
                        "rejected_utilization": snapshot.utilization,
                        "reason": anomaly_reason,
                    }
                    continue
            # Derive unavailable — prior trusted value persists.
            recorded[window_type] = {
                "anomalous": True,
                "rejected_utilization": snapshot.utilization,
                "kept_utilization": prior_5h.utilization if prior_5h else None,
                "reason": anomaly_reason,
            }
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

    # CR2 (external I/O) fix: throttled logged_out reminder. If the
    # sidecar carries ``logged_out_since_unix`` (a previous poll fired
    # ``oauth_logged_out``), don't churn the same network call every
    # cron tick. Emit ``oauth_logged_out_reminder`` at most once per
    # hour — enough for the operator to know the agent is still
    # waiting on a re-``/login`` without flooding events.jsonl.
    is_logged_out, logged_out_since, last_reminder = is_known_logged_out(
        cfg.credentials_path,
    )
    if is_logged_out:
        elapsed = (
            now - last_reminder
            if last_reminder is not None else float("inf")
        )
        if elapsed >= _LOGGED_OUT_REMINDER_INTERVAL_SECONDS:
            hours_since_logout = (
                round((now - logged_out_since) / 3600.0, 1)
                if logged_out_since is not None else None
            )
            await log_event(
                "oauth_logged_out_reminder",
                credentials_path=str(cfg.credentials_path),
                hours_since_logout=hours_since_logout,
            )
            mark_logged_out(cfg.credentials_path, now=now)
        return {"ok": False, "stage": "logged_out_throttled"}

    # Open one session per call (the caller passes one in for tests /
    # if multiple polls share connection state).
    #
    # CR2 (external I/O) fix: explicit total timeout. ``aiohttp.ClientSession()``
    # default has NO total timeout — a hung Anthropic endpoint blocks
    # the cron callback indefinitely; with ``coalesce=True,
    # max_instances=1`` on the OAuth-usage cron, every subsequent quota
    # update is silently dropped. The arbiter then suppresses S4 work
    # on stale data. 30s is generous (refresh + usage typically <1s
    # each) without making the poll feel hung from the operator's side.
    owns_session = session is None
    if session is None:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
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
                # CR2 fix: refresh succeeded → clear sticky logged_out
                # state so the next ticks resume normal flow.
                clear_logged_out(cfg.credentials_path)
            except OAuthRefreshError as exc:
                if exc.logged_out:
                    await log_event(
                        "oauth_logged_out",
                        stage="proactive_refresh",
                        status=exc.status,
                        error=str(exc),
                    )
                    # CR2 fix: stamp the sidecar so the next tick takes
                    # the throttled-reminder branch above instead of
                    # re-firing the network call + the same event.
                    mark_logged_out(cfg.credentials_path, now=now)
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
                    clear_logged_out(cfg.credentials_path)
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
                        mark_logged_out(cfg.credentials_path, now=now)
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

        recorded = await record_usage(store, payload, cfg=cfg)
        await log_event("oauth_usage_ok", recorded=recorded)
        return {"ok": True, "recorded": recorded}
    finally:
        if owns_session:
            await session.close()
