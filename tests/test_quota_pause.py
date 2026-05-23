"""Tests for mid-turn quota-exhaustion handling (SPEC §4.9 / §16 item 18)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mimir.quota_pause import (
    PauseStatus,
    QuotaPauseTracker,
    extract_reset_at,
    is_quota_exhaustion,
)


# ── tracker round-trip ─────────────────────────────────────────────


def test_tracker_unpaused_when_no_state_file(tmp_path: Path):
    tracker = QuotaPauseTracker(tmp_path / "qp.json")
    status = tracker.is_paused()
    assert not status.paused
    assert status.reset_at is None


def test_pause_until_persists_across_instances(tmp_path: Path):
    """A fresh QuotaPauseTracker pointed at the same path should see
    the pause set by a prior instance — survives container restart."""
    path = tmp_path / "qp.json"
    reset = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    tracker_a = QuotaPauseTracker(path)
    tracker_a.pause_until(reset, reason="quota_exhausted", provider="anthropic")

    tracker_b = QuotaPauseTracker(path)
    status = tracker_b.is_paused()
    assert status.paused
    assert status.reset_at == reset
    assert status.reason == "quota_exhausted"


def test_is_paused_lazy_expires_past_reset_time(tmp_path: Path):
    """When ``now`` is past the recorded reset, ``is_paused`` returns
    paused=False AND clears the state file. The reset_at field on the
    result is preserved so the caller can emit ``quota_recovered``."""
    path = tmp_path / "qp.json"
    reset = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    tracker = QuotaPauseTracker(path)
    tracker.pause_until(reset, reason="quota_exhausted")

    status = tracker.is_paused()
    assert not status.paused
    assert status.reset_at == reset
    assert status.reason == "quota_exhausted"
    # State file should be gone.
    assert not path.is_file()


def test_is_paused_respects_explicit_now(tmp_path: Path):
    """The arbiter can pass a specific ``now`` for testability and
    to avoid clock drift between read + decision."""
    path = tmp_path / "qp.json"
    reset = datetime(2026, 5, 23, 18, 0, tzinfo=timezone.utc)
    tracker = QuotaPauseTracker(path)
    tracker.pause_until(reset, reason="quota_exhausted")

    # 5 minutes before reset → still paused.
    assert tracker.is_paused(now=reset - timedelta(minutes=5)).paused
    # 1 second after reset → not paused, state cleared.
    assert not tracker.is_paused(now=reset + timedelta(seconds=1)).paused


def test_clear_removes_state_file(tmp_path: Path):
    path = tmp_path / "qp.json"
    tracker = QuotaPauseTracker(path)
    tracker.pause_until(datetime.now(tz=timezone.utc) + timedelta(hours=1))
    assert path.is_file()
    tracker.clear()
    assert not path.is_file()
    assert not tracker.is_paused().paused


def test_pause_until_overwrites_existing(tmp_path: Path):
    """A newer pause replaces the prior — the newest 429 has the
    most accurate reset info."""
    path = tmp_path / "qp.json"
    tracker = QuotaPauseTracker(path)
    old = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    new = datetime.now(tz=timezone.utc) + timedelta(hours=5)
    tracker.pause_until(old, reason="quota_exhausted", provider="anthropic")
    tracker.pause_until(new, reason="quota_exhausted", provider="anthropic")
    status = QuotaPauseTracker(path).is_paused()
    assert status.paused
    assert status.reset_at == new


def test_malformed_state_file_doesnt_crash(tmp_path: Path):
    """A truncated / non-JSON state file should be treated as no pause
    (not raise) — defensive against the file being half-written."""
    path = tmp_path / "qp.json"
    path.write_text("{not json")
    tracker = QuotaPauseTracker(path)
    assert not tracker.is_paused().paused


def test_state_write_is_atomic(tmp_path: Path):
    """tempfile + rename pattern — leftover tmp files are cleaned up
    on the happy path. Confirms via filesystem state, not behavior."""
    path = tmp_path / "qp.json"
    tracker = QuotaPauseTracker(path)
    tracker.pause_until(datetime.now(tz=timezone.utc) + timedelta(hours=1))
    # Only the final file should be present; no stale ``.tmp`` siblings.
    siblings = list(tmp_path.iterdir())
    assert path in siblings
    assert all(not s.name.endswith(".tmp") for s in siblings)


# ── reset-at extraction ────────────────────────────────────────────


class _FakeResponse:
    """Stand-in for httpx.Response — only needs .headers + .status_code."""

    def __init__(self, headers: dict[str, str], status_code: int = 429):
        self.headers = headers
        self.status_code = status_code


def test_extract_reset_at_from_anthropic_headers():
    """Newer Anthropic responses include ISO timestamps in named
    rate-limit reset headers. Prefer those over Retry-After."""
    target = datetime(2026, 5, 23, 22, 0, tzinfo=timezone.utc)
    response = _FakeResponse({
        "anthropic-ratelimit-tokens-reset": target.isoformat(),
    })
    exc = Exception("rate limit hit")
    exc.response = response  # type: ignore[attr-defined]
    reset, provider = extract_reset_at(exc)
    assert reset == target
    assert provider == "anthropic"


def test_extract_reset_at_from_retry_after_seconds():
    """Generic 429 from a non-Anthropic upstream: Retry-After in
    seconds → reset = now + seconds."""
    response = _FakeResponse({"retry-after": "120"})
    exc = Exception("rate limit")
    exc.response = response  # type: ignore[attr-defined]
    before = datetime.now(tz=timezone.utc)
    reset, provider = extract_reset_at(exc)
    after = datetime.now(tz=timezone.utc)
    # ~120 seconds in the future (accounting for test wall-clock jitter).
    delta = reset - before
    assert timedelta(seconds=119) <= delta <= timedelta(seconds=121) + (after - before)
    assert provider is None


def test_extract_reset_at_fallback_default_5h(tmp_path):
    """Exception without any reset signals → default 5h pause
    (matches Anthropic's 5h rolling window)."""
    exc = Exception("rate limited")
    before = datetime.now(tz=timezone.utc)
    reset, provider = extract_reset_at(exc)
    delta = reset - before
    # Allow ~1s for test wall-clock jitter.
    assert timedelta(hours=4, minutes=59) < delta < timedelta(hours=5, seconds=1)
    assert provider is None


def test_extract_reset_at_from_exception_message():
    """ChatClaudeCode subprocess errors arrive as plain text strings.
    Best-effort regex pulls an ISO-ish timestamp out of the message."""
    exc = Exception("Quota exhausted, resets at 2026-05-24T03:00:00Z")
    reset, _ = extract_reset_at(exc)
    assert reset == datetime(2026, 5, 24, 3, 0, tzinfo=timezone.utc)


# ── exception classification ───────────────────────────────────────


def test_is_quota_exhaustion_classifies_by_class_name():
    class RateLimitError(Exception):
        pass

    assert is_quota_exhaustion(RateLimitError("x"))


def test_is_quota_exhaustion_classifies_by_status_429():
    exc = Exception("not obvious from message")
    exc.response = _FakeResponse({}, status_code=429)  # type: ignore[attr-defined]
    assert is_quota_exhaustion(exc)


def test_is_quota_exhaustion_classifies_by_message_text():
    assert is_quota_exhaustion(Exception("HTTP 429 Too Many Requests"))
    assert is_quota_exhaustion(Exception("hit rate limit"))
    assert is_quota_exhaustion(Exception("quota exceeded"))


def test_is_quota_exhaustion_rejects_unrelated_errors():
    """A generic ValueError or TimeoutError shouldn't be classified
    as quota exhaustion — that would cause us to pause on transient
    network blips or logic bugs."""
    assert not is_quota_exhaustion(ValueError("bad input"))
    assert not is_quota_exhaustion(TimeoutError("network slow"))
    assert not is_quota_exhaustion(Exception("file not found"))
    assert not is_quota_exhaustion(KeyError("missing"))


def test_is_quota_exhaustion_doesnt_match_random_429_in_text():
    """``"429"`` substring matching is intentional — provider
    messages are inconsistent. The trade-off is a body containing
    literally ``"errors=429"`` from another context would false-
    positive. That's acceptable; pause is conservative."""
    # Confirms the documented behavior.
    assert is_quota_exhaustion(Exception("some errors=429 happened"))
