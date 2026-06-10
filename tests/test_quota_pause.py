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
    _MAX_RESET_WINDOW_DAYS,
    _clamp_reset_at,
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
    paused=False and deactivates the pause. The reset_at field on the
    result is preserved so the caller can emit ``quota_recovered``."""
    path = tmp_path / "qp.json"
    reset = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    tracker = QuotaPauseTracker(path)
    tracker.pause_until(reset, reason="quota_exhausted")

    status = tracker.is_paused()
    assert not status.paused
    assert status.reset_at == reset
    assert status.reason == "quota_exhausted"
    # Lazy-expiry deactivates the pause but PRESERVES the file (the
    # escalation counter survives so a header-less cap that recovers and
    # immediately re-429s keeps backing off). A fresh read sees no pause.
    assert tracker.reset_at is None
    reloaded = QuotaPauseTracker(path)
    assert not reloaded.is_paused().paused
    assert reloaded.reset_at is None


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
    # Use a future target so the clamp doesn't modify it.
    target = datetime.now(tz=timezone.utc) + timedelta(hours=3)
    # Strip sub-second precision so fromisoformat round-trips cleanly.
    target = target.replace(microsecond=0)
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


def test_extract_reset_at_fallback_returns_none(tmp_path):
    """Exception without any parseable reset → ``(None, None)``. The old
    behavior blindly defaulted to a 5h pause; now the caller
    (``record_rate_limit``) treats a header-less 429 as transient with a
    short escalating backoff instead."""
    exc = Exception("rate limited")
    reset, provider = extract_reset_at(exc)
    assert reset is None
    assert provider is None


def test_extract_reset_at_from_exception_message():
    """ChatClaudeCode subprocess errors arrive as plain text strings.
    Best-effort regex pulls an ISO-ish timestamp out of the message."""
    # Use a future date so the clamp doesn't fire.
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=2)).replace(
        microsecond=0, second=0, minute=0
    )
    exc = Exception(f"Quota exhausted, resets at {future.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    reset, _ = extract_reset_at(exc)
    assert reset == future


# ── _clamp_reset_at ────────────────────────────────────────────────


def test_clamp_reset_at_passes_through_valid_near_future():
    """A sane near-future timestamp is returned unchanged."""
    now = datetime(2026, 5, 29, 15, 0, tzinfo=timezone.utc)
    reset = now + timedelta(hours=5)
    assert _clamp_reset_at(reset, now) == reset


def test_clamp_reset_at_clamps_far_future_to_max_window():
    """A far-future (garbage) timestamp is clamped to now + MAX days."""
    now = datetime(2026, 5, 29, 15, 0, tzinfo=timezone.utc)
    far_future = datetime(9999, 1, 1, tzinfo=timezone.utc)
    clamped = _clamp_reset_at(far_future, now)
    assert clamped == now + timedelta(days=_MAX_RESET_WINDOW_DAYS)


def test_clamp_reset_at_clamps_past_to_min_floor():
    """A past timestamp is raised to now + 1s."""
    now = datetime(2026, 5, 29, 15, 0, tzinfo=timezone.utc)
    past = now - timedelta(hours=1)
    clamped = _clamp_reset_at(past, now)
    assert clamped == now + timedelta(seconds=1)


def test_extract_reset_at_clamps_far_future_anthropic_header():
    """A malformed Anthropic header with a far-future year is clamped."""
    now = datetime.now(tz=timezone.utc)
    response = _FakeResponse({
        "anthropic-ratelimit-tokens-reset": "9999-12-31T23:59:59+00:00",
    })
    exc = Exception("rate limit hit")
    exc.response = response  # type: ignore[attr-defined]
    reset, provider = extract_reset_at(exc)
    max_allowed = now + timedelta(days=_MAX_RESET_WINDOW_DAYS)
    # Allow 1s for test wall-clock jitter.
    assert reset <= max_allowed + timedelta(seconds=1)
    assert provider == "anthropic"


def test_extract_reset_at_clamps_huge_retry_after_seconds():
    """A Retry-After of millions of seconds is clamped to max window."""
    now = datetime.now(tz=timezone.utc)
    response = _FakeResponse({"retry-after": str(10 * 365 * 24 * 3600)})  # 10 years
    exc = Exception("rate limit")
    exc.response = response  # type: ignore[attr-defined]
    reset, _ = extract_reset_at(exc)
    max_allowed = now + timedelta(days=_MAX_RESET_WINDOW_DAYS)
    assert reset <= max_allowed + timedelta(seconds=1)


def test_extract_reset_at_clamps_far_future_exception_message():
    """A garbage year in the exception message is clamped, not used raw."""
    now = datetime.now(tz=timezone.utc)
    exc = Exception("Quota exhausted, resets at 9999-01-01T00:00:00Z")
    reset, _ = extract_reset_at(exc)
    max_allowed = now + timedelta(days=_MAX_RESET_WINDOW_DAYS)
    assert reset <= max_allowed + timedelta(seconds=1)


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


# ── record_rate_limit: transient-vs-cap policy (chainlink: quota backoff) ──


def test_record_rate_limit_authoritative_reset_uses_it(tmp_path: Path):
    """A 429 carrying a parseable reset → pause exactly until then,
    reason 'quota_exhausted' (a real window, not a transient blip)."""
    path = tmp_path / "qp.json"
    now = datetime.now(tz=timezone.utc)
    reset = (now + timedelta(minutes=30)).replace(microsecond=0)
    exc = Exception(f"rate limited; retry after {reset.isoformat()}")
    reset_at, reason = QuotaPauseTracker(path).record_rate_limit(exc, now=now)
    assert reason == "quota_exhausted"
    assert abs((reset_at - reset).total_seconds()) < 2


def test_record_rate_limit_headerless_uses_short_backoff(tmp_path: Path):
    """A header-less 429 (Codex's bare 'HTTP 429: Rate limit exceeded')
    → a short 60s backoff, NOT a 5h window pause."""
    path = tmp_path / "qp.json"
    now = datetime.now(tz=timezone.utc)
    exc = Exception("HTTP 429: Rate limit exceeded")
    reset_at, reason = QuotaPauseTracker(path).record_rate_limit(exc, now=now)
    assert reason == "rate_limited_backoff"
    assert (reset_at - now).total_seconds() == 60


def test_record_rate_limit_escalates_within_decay_window(tmp_path: Path):
    """Repeated header-less 429s within the decay window escalate
    (60s → 4m) so a real header-less cap backs off instead of being
    hammered. Each call reloads the tracker (mirrors the per-turn
    fresh-construction in agent.py)."""
    path = tmp_path / "qp.json"
    exc = Exception("HTTP 429: Rate limit exceeded")
    now0 = datetime.now(tz=timezone.utc)
    r1, _ = QuotaPauseTracker(path).record_rate_limit(exc, now=now0)
    assert (r1 - now0).total_seconds() == 60
    now1 = now0 + timedelta(seconds=90)  # within the 30-min decay window
    r2, reason2 = QuotaPauseTracker(path).record_rate_limit(exc, now=now1)
    assert reason2 == "rate_limited_backoff"
    assert (r2 - now1).total_seconds() == 240  # 60 * 4


def test_record_rate_limit_resets_escalation_after_decay(tmp_path: Path):
    """An isolated header-less 429 long after the previous one resets to
    the 60s floor (the escalation decays — blips hours apart don't
    accumulate)."""
    path = tmp_path / "qp.json"
    exc = Exception("HTTP 429: Rate limit exceeded")
    now0 = datetime.now(tz=timezone.utc)
    QuotaPauseTracker(path).record_rate_limit(exc, now=now0)
    now1 = now0 + timedelta(hours=1)  # well past the 30-min decay window
    r2, _ = QuotaPauseTracker(path).record_rate_limit(exc, now=now1)
    assert (r2 - now1).total_seconds() == 60


# ── Codex 429 with surfaced x-codex-* windows (langchain-codex-plus >= 0.0.3) ──


def _codex_429(*, primary_used: float, reset_at: int) -> Exception:
    """A CodexResponseError-shaped exception carrying parsed rate-limit
    windows (duck-typed; extract_reset_at reads via getattr)."""
    from types import SimpleNamespace
    exc = Exception("HTTP 429: Rate limit exceeded")
    exc.status_code = 429
    exc.rate_limits = SimpleNamespace(
        primary=SimpleNamespace(
            used_percent=primary_used, reset_at=reset_at, reset_after_seconds=None,
        ),
        secondary=None,
    )
    return exc


def test_extract_reset_at_codex_window_at_cap(tmp_path: Path):
    """A 429 whose binding window is at cap → use its reset, provider codex-plus."""
    reset_ts = int((datetime.now(tz=timezone.utc) + timedelta(hours=3)).timestamp())
    reset, provider = extract_reset_at(_codex_429(primary_used=100.0, reset_at=reset_ts))
    assert provider == "codex-plus"
    assert reset is not None and abs(reset.timestamp() - reset_ts) < 2


def test_extract_reset_at_codex_low_util_is_transient(tmp_path: Path):
    """A 429 while utilization is low (the 'graph wasn't near quota' case)
    → NOT treated as an authoritative cap; falls through to (None, None)
    so the caller applies a short transient backoff."""
    reset_ts = int((datetime.now(tz=timezone.utc) + timedelta(hours=3)).timestamp())
    reset, _ = extract_reset_at(_codex_429(primary_used=12.0, reset_at=reset_ts))
    assert reset is None


def test_record_rate_limit_codex_cap_uses_window_reset(tmp_path: Path):
    reset_ts = int((datetime.now(tz=timezone.utc) + timedelta(hours=3)).timestamp())
    reset_at, reason = QuotaPauseTracker(tmp_path / "qp.json").record_rate_limit(
        _codex_429(primary_used=100.0, reset_at=reset_ts)
    )
    assert reason == "quota_exhausted"
    assert abs(reset_at.timestamp() - reset_ts) < 2


def test_record_rate_limit_codex_low_util_falls_back_to_backoff(tmp_path: Path):
    reset_ts = int((datetime.now(tz=timezone.utc) + timedelta(hours=3)).timestamp())
    now = datetime.now(tz=timezone.utc)
    reset_at, reason = QuotaPauseTracker(tmp_path / "qp.json").record_rate_limit(
        _codex_429(primary_used=20.0, reset_at=reset_ts), now=now,
    )
    assert reason == "rate_limited_backoff"
    assert (reset_at - now).total_seconds() == 60


def test_authoritative_reset_does_not_seed_transient_escalation(tmp_path: Path):
    """Regression (mimir-carreira #559): an authoritative cap must NOT seed
    the transient escalation clock. A header-less 429 shortly after an
    authoritative pause recovers must start at the 60s floor, not the 4-min
    second tier. (Lazy-expiry preserves the clock, so the authoritative
    branch has to clear it explicitly.)"""
    path = tmp_path / "qp.json"
    now0 = datetime.now(tz=timezone.utc)

    # 1) authoritative cap (Codex window at 100%, reset 20 min out)
    reset_ts = int((now0 + timedelta(minutes=20)).timestamp())
    _, reason1 = QuotaPauseTracker(path).record_rate_limit(
        _codex_429(primary_used=100.0, reset_at=reset_ts), now=now0,
    )
    assert reason1 == "quota_exhausted"

    # 2) the authoritative pause lazy-expires (window rolled over)
    later = now0 + timedelta(minutes=25)
    assert QuotaPauseTracker(path).is_paused(now=later).paused is False

    # 3) a header-less 429 within the decay window → MUST be the 60s floor,
    #    not 240s — the authoritative pause didn't seed escalation.
    now1 = later + timedelta(seconds=30)
    reset_at3, reason3 = QuotaPauseTracker(path).record_rate_limit(
        Exception("HTTP 429: Rate limit exceeded"), now=now1,
    )
    assert reason3 == "rate_limited_backoff"
    assert (reset_at3 - now1).total_seconds() == 60


def test_authoritative_reset_clears_existing_transient_escalation(tmp_path: Path):
    """The complement: an in-progress transient escalation is reset by an
    authoritative cap, so the next header-less 429 is back to the floor."""
    path = tmp_path / "qp.json"
    exc = Exception("HTTP 429: Rate limit exceeded")
    now0 = datetime.now(tz=timezone.utc)
    # two header-less 429s → escalated to 240s
    QuotaPauseTracker(path).record_rate_limit(exc, now=now0)
    r2, _ = QuotaPauseTracker(path).record_rate_limit(exc, now=now0 + timedelta(seconds=90))
    assert (r2 - (now0 + timedelta(seconds=90))).total_seconds() == 240
    # a (short-lived) authoritative cap lands at +120s → clears the transient clock
    reset_ts = int((now0 + timedelta(seconds=125)).timestamp())
    QuotaPauseTracker(path).record_rate_limit(
        _codex_429(primary_used=100.0, reset_at=reset_ts), now=now0 + timedelta(seconds=120),
    )
    # the cap expires (lazy-expiry)
    assert QuotaPauseTracker(path).is_paused(now=now0 + timedelta(seconds=130)).paused is False
    # a header-less 429 AFTER the cap expired → floor again (clock was cleared).
    # (While the cap was still active it would correctly report the cap, not 60s —
    # see test_headerless_429_does_not_shorten_active_authoritative_pause.)
    now1 = now0 + timedelta(seconds=140)
    r4, _ = QuotaPauseTracker(path).record_rate_limit(exc, now=now1)
    assert (r4 - now1).total_seconds() == 60


def test_headerless_429_does_not_shorten_active_authoritative_pause(tmp_path: Path):
    """Review fix (mimir-carreira #559): a header-less 429 carries no reset
    info, so it must NOT shorten/downgrade an already-active authoritative cap.
    The stored pause stays at the authoritative reset — a user-message turn's
    bare 429 during a real cap can't re-enable work before the cap resets."""
    path = tmp_path / "qp.json"
    now = datetime.now(tz=timezone.utc)
    auth_reset = (now + timedelta(minutes=10)).replace(microsecond=0)
    QuotaPauseTracker(path).pause_until(auth_reset, reason="quota_exhausted", provider="anthropic")

    new_reset, reason = QuotaPauseTracker(path).record_rate_limit(
        Exception("HTTP 429: Rate limit exceeded"), now=now + timedelta(minutes=1),
    )
    assert reason == "quota_exhausted"
    assert new_reset == auth_reset
    # the stored pause is unchanged — not downgraded to a 60s transient backoff
    reloaded = QuotaPauseTracker(path)
    assert reloaded.reset_at == auth_reset
    assert reloaded.is_paused(now=now + timedelta(minutes=1)).reason == "quota_exhausted"


# ─── recorded_at (early-recovery probe support) ────────────────────────


def test_pause_records_recorded_at_and_persists(tmp_path):
    from datetime import datetime, timedelta, timezone
    from mimir.quota_pause import QuotaPauseTracker

    path = tmp_path / "quota_pause.json"
    now = datetime.now(tz=timezone.utc)
    tracker = QuotaPauseTracker(path)
    tracker.pause_until(now + timedelta(hours=2), now=now)
    assert tracker.recorded_at == now

    # Round-trips through the state file.
    reloaded = QuotaPauseTracker(path)
    assert reloaded.recorded_at == now


def test_recorded_at_defaults_to_wall_clock(tmp_path):
    from datetime import datetime, timedelta, timezone
    from mimir.quota_pause import QuotaPauseTracker

    before = datetime.now(tz=timezone.utc)
    tracker = QuotaPauseTracker(tmp_path / "quota_pause.json")
    tracker.pause_until(before + timedelta(hours=1))
    after = datetime.now(tz=timezone.utc)
    assert tracker.recorded_at is not None
    assert before <= tracker.recorded_at <= after


def test_recorded_at_cleared_with_pause(tmp_path):
    from datetime import datetime, timedelta, timezone
    from mimir.quota_pause import QuotaPauseTracker

    now = datetime.now(tz=timezone.utc)
    tracker = QuotaPauseTracker(tmp_path / "quota_pause.json")
    tracker.pause_until(now + timedelta(hours=1), now=now)
    tracker.clear()
    assert tracker.recorded_at is None


def test_recorded_at_missing_in_old_state_file(tmp_path):
    """State files written before the field existed load as None —
    the early-recovery probe then falls back to plain reset expiry."""
    import json
    from datetime import datetime, timedelta, timezone
    from mimir.quota_pause import QuotaPauseTracker

    path = tmp_path / "quota_pause.json"
    reset = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    path.write_text(json.dumps({
        "reset_at": reset.isoformat(),
        "reason": "quota_exhausted",
        "provider": "anthropic",
    }), encoding="utf-8")
    tracker = QuotaPauseTracker(path)
    assert tracker.is_paused().paused is True
    assert tracker.recorded_at is None
