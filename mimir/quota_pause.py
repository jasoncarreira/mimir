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
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Fallback pause length when the exception carries no usable reset
# info. Matches Anthropic's 5-hour rolling window so we don't pause
# longer than the actual reset cycle.
_DEFAULT_PAUSE_HOURS = 5


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
        self._load()

    @property
    def state_path(self) -> Path:
        return self._path

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

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("quota_pause: can't create dir: %s", exc)
            return
        payload: dict[str, Any] = {
            "reset_at": self._reset_at.isoformat() if self._reset_at else None,
            "reason": self._reason,
            "provider": self._provider,
        }
        # Atomic write (tmpfile + rename in same dir) so a crash mid-
        # write doesn't leave a half-truncated state file.
        fd, tmp_str = tempfile.mkstemp(
            prefix=self._path.name + ".", suffix=".tmp", dir=str(self._path.parent),
        )
        tmp = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self._path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            log.warning("quota_pause: state write failed: %s", exc)

    # ── public API ──────────────────────────────────────────────────

    def pause_until(
        self,
        reset_at: datetime,
        *,
        reason: str = "quota_exhausted",
        provider: str | None = None,
    ) -> None:
        """Record that the agent should treat itself as quota-paused
        until ``reset_at``. Idempotent — overwrites any existing pause
        (the newest pause wins, since it has the freshest reset
        info)."""
        self._reset_at = reset_at
        self._reason = reason
        self._provider = provider
        self._save()

    def clear(self) -> None:
        """Drop the pause unconditionally — for tests and for the
        ``quota_recovered`` lazy-expiry path."""
        self._reset_at = None
        self._reason = None
        self._provider = None
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
            self.clear()
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


def extract_reset_at(exc: BaseException) -> tuple[datetime, str | None]:
    """Best-effort: parse a ``reset_at`` datetime from a 429
    exception. Returns ``(reset_at, provider_label)``.

    Strategy:
    1. If the exception carries an ``httpx.Response`` (anthropic /
       openai SDKs do), check its headers for a reset timestamp or
       ``Retry-After`` seconds value.
    2. Failing that, return ``now + _DEFAULT_PAUSE_HOURS`` so the
       agent at least pauses for one quota window.
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
                return reset, "anthropic"
            except ValueError:
                continue
        # Generic Retry-After (seconds).
        retry_after = (
            headers.get("retry-after") if hasattr(headers, "get") else None
        )
        seconds = _parse_retry_after_header(retry_after)
        if seconds is not None:
            return (now + timedelta(seconds=seconds), None)

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
            return reset, None
        except ValueError:
            pass

    return (now + timedelta(hours=_DEFAULT_PAUSE_HOURS), None)


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
    if cls_name == "RateLimitError":
        return True
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
