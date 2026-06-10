"""Mid-turn quota exhaustion handling (SPEC §4.9 / §16 item 18).

Background: the homeostatic arbiter (``mimir/budget.py``) pre-suppresses
scheduled ticks when plan-window utilization crosses 0.80 (configurable).
That covers the "we know we'd exhaust" case. It does NOT cover the
"started at 78%, mid-turn jump to 100%, got a 429 from the upstream
model" case — a long turn (large spawn, lots of tool calls) can blow
through the remaining budget before the next pre-check fires.

When that happens, the model call surfaces as a ``RateLimitError`` /
HTTP 429 / equivalent provider-specific exception. Pre-fix the
exception just landed in the generic ``except Exception`` in
``agent.run_turn`` — logged + dropped, no signal to the arbiter, no
operator-facing event with the reset time.

This module provides the missing piece: a persistent
``QuotaPauseTracker`` that the agent's exception handler writes to,
and that the arbiter consults BEFORE its utilization check. While
paused:

- Scheduled ticks are suppressed (arbiter returns ``fire=False`` with
  reason ``quota_exhausted_pause``).
- User-message turns still run — interactive responsiveness wins over
  quota conservation per §4.9. The model call will fail again with
  429 if quota is still 100%; the agent surfaces that to the operator
  via send_message rather than vanishing into a logged exception.
- A ``quota_recovered`` event fires the first time ``is_paused()`` is
  consulted past the reset timestamp (lazy expiry — no scheduler
  wakeup needed).

State persists at ``<home>/.mimir/quota_pause.json`` so a container
restart mid-pause doesn't lose the pause and immediately retry.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ._atomic import atomic_write_json

log = logging.getLogger(__name__)


# Fallback pause length when the exception carries no usable reset
# info. Matches Anthropic's 5-hour rolling window so we don't pause
# longer than the actual reset cycle.
_DEFAULT_PAUSE_HOURS = 5

# Maximum window we'll accept from a parsed reset timestamp. Longer
# values indicate a malformed/garbage header (e.g. "9999-01-01") that
# would wedge the agent indefinitely. Anthropic's longest real window
# is 7 days; clamp to that.
_MAX_RESET_WINDOW_DAYS = 7

# Transient-vs-cap policy (chainlink: quota-pause backoff). A 429 that
# carries NO parseable reset (no Anthropic header, no Retry-After, no
# ISO timestamp in the message — e.g. Codex's bare "HTTP 429: Rate limit
# exceeded") is far more likely a momentary burst/requests-rate limit
# than a genuine usage-window cap (a real cap almost always ships a
# reset hint). Treat it as transient: a SHORT backoff that escalates on
# repeat so a real, header-less cap eventually backs off to the window
# length instead of being hammered. The escalation decays — if no 429
# has landed for ``_TRANSIENT_DECAY_SECONDS`` the counter resets, so
# isolated blips hours apart each get the cheap 60s treatment rather
# than accumulating.
_TRANSIENT_BASE_SECONDS = 60
_TRANSIENT_FACTOR = 4
_TRANSIENT_MAX_SECONDS = _DEFAULT_PAUSE_HOURS * 3600  # cap at one window
_TRANSIENT_DECAY_SECONDS = 30 * 60


def _transient_backoff_seconds(consecutive: int) -> int:
    """Escalating backoff for header-less 429s: 60s, 4m, 16m, ~1h, …
    capped at one quota window. ``consecutive`` is the number of
    back-to-back (un-decayed) header-less 429s seen so far."""
    n = max(0, consecutive)
    return int(min(_TRANSIENT_BASE_SECONDS * (_TRANSIENT_FACTOR ** n), _TRANSIENT_MAX_SECONDS))


def _clamp_reset_at(reset: datetime, now: datetime) -> datetime:
    """Return *reset* clamped to ``[now + 1s, now + _MAX_RESET_WINDOW_DAYS]``.

    A parsed reset-at that lies more than ``_MAX_RESET_WINDOW_DAYS``
    in the future (e.g. from a garbage ``9999-01-01`` header) is
    silently clamped to the maximum. A value in the past or equal to
    *now* is clamped up to ``now + 1s`` so we always record a
    forward-looking pause.
    """
    max_reset = now + timedelta(days=_MAX_RESET_WINDOW_DAYS)
    if reset > max_reset:
        return max_reset
    min_reset = now + timedelta(seconds=1)
    if reset < min_reset:
        return min_reset
    return reset


@dataclass(frozen=True)
class PauseStatus:
    """Result of ``QuotaPauseTracker.is_paused()``."""

    paused: bool
    reset_at: datetime | None
    reason: str | None


class QuotaPauseTracker:
    """File-backed pause-state tracker. Single-instance per home."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._reset_at: datetime | None = None
        self._reason: str | None = None
        self._provider: str | None = None
        # When the current pause was recorded. Lets the early-recovery
        # recheck (scheduler) distinguish quota observations made AFTER
        # the 429 from stale pre-pause readings. None on state files
        # written before this field existed — the recheck then falls
        # back to plain reset-at expiry.
        self._recorded_at: datetime | None = None
        # Transient-backoff escalation state (see _transient_backoff_seconds).
        # ``_last_transient_at`` tracks the last *header-less/transient*
        # backoff only — authoritative caps deliberately leave it untouched
        # (cleared) so they don't seed the burst escalation.
        self._consecutive: int = 0
        self._last_transient_at: datetime | None = None
        self._load()

    @property
    def state_path(self) -> Path:
        return self._path

    @property
    def reset_at(self) -> datetime | None:
        """The recorded reset timestamp, if any, WITHOUT lazy-expiry.

        Unlike :meth:`is_paused`, reading this never clears the pause —
        it lets the scheduler peek at when to arm a recovery wake on
        startup without consuming the recovery transition."""
        return self._reset_at

    @property
    def provider(self) -> str | None:
        """The provider label recorded with the current pause, if any."""
        return self._provider

    @property
    def recorded_at(self) -> datetime | None:
        """When the current pause was recorded, if known. None for
        pauses persisted before this field existed."""
        return self._recorded_at

    # ── persistence ─────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("quota_pause: state file unreadable: %s", exc)
            return
        if not isinstance(data, dict):
            return
        raw_reset = data.get("reset_at")
        if isinstance(raw_reset, str):
            try:
                # ``datetime.fromisoformat`` rejects trailing 'Z' on
                # Python ≤ 3.11; normalize to ``+00:00``.
                self._reset_at = datetime.fromisoformat(
                    raw_reset.replace("Z", "+00:00"),
                )
            except ValueError:
                self._reset_at = None
        self._reason = data.get("reason") or None
        self._provider = data.get("provider") or None
        raw_recorded = data.get("recorded_at")
        if isinstance(raw_recorded, str):
            try:
                self._recorded_at = datetime.fromisoformat(
                    raw_recorded.replace("Z", "+00:00"),
                )
            except ValueError:
                self._recorded_at = None
        raw_consecutive = data.get("consecutive")
        self._consecutive = raw_consecutive if isinstance(raw_consecutive, int) and raw_consecutive >= 0 else 0
        raw_last = data.get("last_transient_at")
        if isinstance(raw_last, str):
            try:
                self._last_transient_at = datetime.fromisoformat(raw_last.replace("Z", "+00:00"))
            except ValueError:
                self._last_transient_at = None

    def _save(self) -> None:
        payload: dict[str, Any] = {
            "reset_at": self._reset_at.isoformat() if self._reset_at else None,
            "reason": self._reason,
            "provider": self._provider,
            "recorded_at": self._recorded_at.isoformat() if self._recorded_at else None,
            "consecutive": self._consecutive,
            "last_transient_at": self._last_transient_at.isoformat() if self._last_transient_at else None,
        }
        # chainlink #239: shared atomic-write helper applies the CR#7
        # invariant (fsync file + fsync parent dir). Prior shape only
        # fsynced the file — a crash between rename and writeback could
        # revert the pause state. The helper raises on failure; we log
        # + swallow here because a missed quota_pause.json write self-
        # heals at the next pause_until call.
        try:
            atomic_write_json(self._path, payload)
        except OSError as exc:
            log.warning("quota_pause: state write failed: %s", exc)

    # ── public API ──────────────────────────────────────────────────

    def pause_until(
        self,
        reset_at: datetime,
        *,
        reason: str = "quota_exhausted",
        provider: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Record that the agent should treat itself as quota-paused
        until ``reset_at``. Idempotent — overwrites any existing pause
        (the newest pause wins, since it has the freshest reset info).
        ``now`` (tests) overrides the ``recorded_at`` stamp.

        Does NOT touch the transient-backoff escalation clock
        (``_consecutive`` / ``_last_transient_at``) — only
        :meth:`record_rate_limit`'s header-less branch advances that, so
        an authoritative cap can't be mistaken for a transient burst."""
        self._reset_at = reset_at
        self._reason = reason
        self._provider = provider
        self._recorded_at = now or datetime.now(tz=timezone.utc)
        self._save()

    def record_rate_limit(
        self, exc: BaseException, *, now: datetime | None = None,
    ) -> tuple[datetime, str]:
        """Classify a 429 and record an appropriate pause. Returns
        ``(reset_at, reason)``.

        - If the exception carries an authoritative reset (header /
          Retry-After / ISO timestamp in the message, or a Codex window
          at cap), pause exactly until then with reason ``quota_exhausted``
          — and CLEAR the transient escalation clock, because an
          authoritative cap is not part of a header-less burst chain.
        - Otherwise (a header-less 429 — e.g. Codex's bare "HTTP 429:
          Rate limit exceeded") treat it as a likely-transient burst:
          a short, escalating backoff (reason ``rate_limited_backoff``)
          that escalates only when the PREVIOUS transient backoff was
          recent. The escalation decays after ``_TRANSIENT_DECAY_SECONDS``
          of quiet (or an authoritative pause clearing it) so isolated
          blips reset to the 60s floor."""
        now = now or datetime.now(tz=timezone.utc)
        parsed_reset, provider = extract_reset_at(exc)
        if parsed_reset is not None:
            # Authoritative cap — clear the transient clock so a later
            # header-less 429 starts at the 60s floor, not mid-escalation.
            self._consecutive = 0
            self._last_transient_at = None
            self.pause_until(parsed_reset, reason="quota_exhausted", provider=provider, now=now)
            return parsed_reset, "quota_exhausted"

        # A header-less 429 carries no reset info, so it must NOT shorten or
        # downgrade an already-active authoritative cap whose real reset is
        # known and still in the future. While such a cap is unexpired, leave
        # it intact and report it — the bare 429 is subsumed by the cap (and
        # doesn't seed transient escalation). Without this, a user-message
        # turn's 429 during an active cap (user turns run while scheduled ticks
        # are suppressed) would overwrite the global pause down to the 60s
        # floor and re-enable S4 work minutes before the cap actually resets.
        if (
            self._reset_at is not None
            and self._reason == "quota_exhausted"
            and self._reset_at > now
        ):
            return self._reset_at, "quota_exhausted"

        # Header-less 429 → transient backoff. Escalate only when the
        # previous *transient* backoff was recent; an authoritative pause
        # (or >30 min of quiet) leaves _last_transient_at None/stale, so
        # this starts fresh at the floor.
        if (
            self._last_transient_at is not None
            and (now - self._last_transient_at).total_seconds() <= _TRANSIENT_DECAY_SECONDS
        ):
            consecutive = self._consecutive + 1
        else:
            consecutive = 0
        self._consecutive = consecutive
        self._last_transient_at = now
        reset_at = now + timedelta(seconds=_transient_backoff_seconds(consecutive))
        self.pause_until(reset_at, reason="rate_limited_backoff", provider=provider, now=now)
        return reset_at, "rate_limited_backoff"

    def _mark_recovered(self) -> None:
        """Clear the active pause on lazy-expiry but KEEP the transient
        escalation state (``_consecutive`` / ``_last_transient_at``), so a
        real header-less cap that recovers and immediately 429s again keeps
        escalating instead of resetting to the 60s floor every cycle. The
        decay in :meth:`record_rate_limit` is what eventually resets it."""
        self._reset_at = None
        self._reason = None
        self._provider = None
        self._recorded_at = None
        self._save()

    def clear(self) -> None:
        """Drop the pause AND the escalation state unconditionally —
        for tests and explicit resets. (Lazy-expiry uses
        :meth:`_mark_recovered`, which preserves escalation state.)"""
        self._reset_at = None
        self._reason = None
        self._provider = None
        self._recorded_at = None
        self._consecutive = 0
        self._last_transient_at = None
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("quota_pause: state delete failed: %s", exc)

    def is_paused(self, *, now: datetime | None = None) -> PauseStatus:
        """Return current pause status. Lazy-expires when ``now`` is
        past the recorded reset — caller can branch on
        ``result.paused`` and (when False but reset_at is non-None)
        emit ``quota_recovered``."""
        if self._reset_at is None:
            return PauseStatus(paused=False, reset_at=None, reason=None)
        when = now or datetime.now(tz=timezone.utc)
        if when >= self._reset_at:
            # Lazy expiry. Caller (the arbiter) is responsible for
            # the ``quota_recovered`` algedonic emit since this is a
            # sync method.
            saved_reset = self._reset_at
            saved_reason = self._reason
            self._mark_recovered()
            return PauseStatus(
                paused=False, reset_at=saved_reset, reason=saved_reason,
            )
        return PauseStatus(
            paused=True, reset_at=self._reset_at, reason=self._reason,
        )


# ── exception → reset-at extraction ─────────────────────────────────


def _parse_retry_after_header(value: str | None) -> int | None:
    """``Retry-After`` is either a seconds-int or an HTTP-date. We
    only care about the seconds case in practice (Anthropic sends
    seconds); HTTP-date support is left as a future addition."""
    if not value:
        return None
    try:
        secs = int(value.strip())
        return secs if secs > 0 else None
    except (TypeError, ValueError):
        return None


_ANTHROPIC_RESET_HEADERS = (
    # Newer Anthropic headers (precise reset timestamps).
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-reset",
    "anthropic-ratelimit-input-tokens-reset",
    "anthropic-ratelimit-output-tokens-reset",
)


# A Codex 429 surfaces both quota windows (langchain-codex-plus >= 0.0.3).
# Only trust a window's reset as authoritative when that window is
# genuinely near its cap — a 429 while utilization is low is far more
# likely a transient burst limit (→ short escalating backoff), which is
# exactly the "usage graph wasn't near quota" case. The headers turn the
# transient-vs-cap guess into a real signal.
_CODEX_CAP_THRESHOLD = 90.0


def _codex_window_reset(rate_limits: Any, now: datetime) -> datetime | None:
    """Reset datetime of the binding (most-utilized) Codex window when it
    is at/near its cap, else ``None``.

    Reads the parsed ``CodexRateLimits`` surfaced on a ``CodexResponseError``
    (``primary`` / ``secondary`` windows, each with ``used_percent`` +
    ``reset_at`` (unix ts) / ``reset_after_seconds``)."""
    best_used = -1.0
    best_when: datetime | None = None
    for win in (getattr(rate_limits, "primary", None), getattr(rate_limits, "secondary", None)):
        if win is None:
            continue
        used = getattr(win, "used_percent", None)
        used = float(used) if isinstance(used, (int, float)) else 0.0
        reset_at = getattr(win, "reset_at", None)
        reset_after = getattr(win, "reset_after_seconds", None)
        when: datetime | None = None
        if isinstance(reset_at, (int, float)):
            try:
                when = datetime.fromtimestamp(int(reset_at), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                when = None
        elif isinstance(reset_after, (int, float)):
            when = now + timedelta(seconds=int(reset_after))
        if when is not None and used > best_used:
            best_used, best_when = used, when
    if best_when is not None and best_used >= _CODEX_CAP_THRESHOLD:
        return best_when
    return None


def extract_reset_at(exc: BaseException) -> tuple[datetime | None, str | None]:
    """Best-effort: parse a real ``reset_at`` datetime from a 429
    exception. Returns ``(reset_at, provider_label)`` — ``reset_at`` is
    ``None`` when no authoritative reset could be parsed.

    Strategy:
    1. If the exception carries an ``httpx.Response`` (anthropic /
       openai SDKs do), check its headers for a reset timestamp or
       ``Retry-After`` seconds value.
    2. Codex Plus (``langchain-codex-plus`` >= 0.0.3) surfaces the parsed
       ``x-codex-*`` windows on the exception — use the binding window's
       reset, but only when it's genuinely at cap (otherwise the 429 is a
       transient burst and the caller's backoff is the right response).
    3. Otherwise scrape the exception message for an ISO timestamp.
    4. Failing all, return ``(None, None)``. The caller decides the
       backoff — a header-less 429 is treated as transient (short,
       escalating) rather than blindly pausing for a full window, which
       is what the old ``now + _DEFAULT_PAUSE_HOURS`` default did.
    """
    now = datetime.now(tz=timezone.utc)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None

    if headers is not None:
        # Anthropic-style: ISO timestamps in dedicated reset headers.
        for header_name in _ANTHROPIC_RESET_HEADERS:
            raw = headers.get(header_name) if hasattr(headers, "get") else None
            if not raw:
                continue
            try:
                reset = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return _clamp_reset_at(reset, now), "anthropic"
            except ValueError:
                continue
        # Generic Retry-After (seconds).
        retry_after = (
            headers.get("retry-after") if hasattr(headers, "get") else None
        )
        seconds = _parse_retry_after_header(retry_after)
        if seconds is not None:
            return _clamp_reset_at(now + timedelta(seconds=seconds), now), None

    # Codex Plus: parsed x-codex-* windows surfaced on the exception.
    rate_limits = getattr(exc, "rate_limits", None)
    if rate_limits is not None:
        reset = _codex_window_reset(rate_limits, now)
        if reset is not None:
            return _clamp_reset_at(reset, now), "codex-plus"
    codex_headers = getattr(exc, "headers", None)
    if isinstance(codex_headers, dict):
        # Direct Retry-After fallback if the parsed windows weren't usable.
        ra = _parse_retry_after_header(codex_headers.get("retry-after"))
        if ra is not None:
            return _clamp_reset_at(now + timedelta(seconds=ra), now), "codex-plus"

    # Fallback parse: scrape the exception message for an ISO-ish
    # timestamp. ChatClaudeCode surfaces 429s as plain text from the
    # subprocess; a regex-fallback covers that path opportunistically.
    msg = str(exc)
    m = re.search(
        r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
        msg,
    )
    if m:
        try:
            reset = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            if reset.tzinfo is None:
                reset = reset.replace(tzinfo=timezone.utc)
            return _clamp_reset_at(reset, now), None
        except ValueError:
            pass

    return (None, None)


# ── exception classification ────────────────────────────────────────


def is_quota_exhaustion(exc: BaseException) -> bool:
    """Heuristic: does this exception represent an upstream quota /
    rate-limit refusal (vs. a transient network blip or a logic bug)?

    Checks, in order:
    1. anthropic.RateLimitError (string-match the class name so we
       don't have to import the SDK eagerly).
    2. httpx-shaped exception with ``response.status_code == 429``.
    3. The exception class name contains ``RateLimit``.
    4. The message contains ``"429"`` or ``"rate limit"`` / ``"quota"``
       (case-insensitive) — covers ChatClaudeCode subprocess errors
       and generic provider-specific wrappers.
    """
    cls_name = type(exc).__name__
    # ``"RateLimit" in cls_name`` subsumes the exact-match check
    # (the exact name IS a substring of itself) — single check
    # catches ``RateLimitError`` and any ``*RateLimit*`` variant
    # provider SDKs introduce.
    if "RateLimit" in cls_name:
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    msg = str(exc).lower()
    if "429" in msg:
        return True
    if "rate limit" in msg or "rate_limit" in msg:
        return True
    if "quota" in msg and ("exhaust" in msg or "exceed" in msg or "limit" in msg):
        return True
    return False
