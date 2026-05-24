"""Tests for ``mimir/token_usage_history.py``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mimir.token_usage_history import compute_token_usage_history


def _turn(
    ts: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    total_cost_usd: float | None = None,
) -> dict:
    """Build a minimal TurnRecord-shaped dict."""
    return {
        "ts": ts,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
        },
        "total_cost_usd": total_cost_usd,
    }


def test_empty_input_returns_empty_list():
    """No turns → no buckets."""
    assert compute_token_usage_history([]) == []


def test_single_turn_creates_single_day_bucket():
    """One turn → one bucket with that turn's tokens."""
    turns = [_turn(
        "2026-05-23T12:00:00+00:00",
        input_tokens=10,
        output_tokens=2000,
        cache_creation_input_tokens=500,
        cache_read_input_tokens=50000,
        total_cost_usd=0.42,
    )]
    out = compute_token_usage_history(turns)
    assert len(out) == 1
    bucket = out[0]
    assert bucket["date"] == "2026-05-23"
    assert bucket["input_tokens"] == 10
    assert bucket["output_tokens"] == 2000
    assert bucket["cache_creation_input_tokens"] == 500
    assert bucket["cache_read_input_tokens"] == 50000
    assert bucket["total_cost_usd"] == pytest.approx(0.42)
    assert bucket["turn_count"] == 1


def test_multiple_turns_same_day_sum_into_one_bucket():
    """Turns on the same UTC date aggregate into a single bucket."""
    turns = [
        _turn("2026-05-23T08:00:00+00:00", input_tokens=100, output_tokens=500),
        _turn("2026-05-23T16:00:00+00:00", input_tokens=200, output_tokens=700),
    ]
    out = compute_token_usage_history(turns)
    assert len(out) == 1
    bucket = out[0]
    assert bucket["input_tokens"] == 300
    assert bucket["output_tokens"] == 1200
    assert bucket["turn_count"] == 2


def test_turns_across_days_create_separate_buckets():
    """Buckets are emitted chronologically (oldest first)."""
    turns = [
        _turn("2026-05-25T08:00:00+00:00", input_tokens=300),
        _turn("2026-05-23T08:00:00+00:00", input_tokens=100),
        _turn("2026-05-24T08:00:00+00:00", input_tokens=200),
    ]
    out = compute_token_usage_history(turns)
    assert [b["date"] for b in out] == ["2026-05-23", "2026-05-24", "2026-05-25"]
    assert [b["input_tokens"] for b in out] == [100, 200, 300]


def test_turn_with_no_usage_dict_still_counted():
    """A turn that errored mid-flight has no ``usage`` but still ran —
    its turn_count contribution surfaces in the chart's tooltip even
    though its token contribution is zero."""
    turns = [
        {"ts": "2026-05-23T12:00:00+00:00"},  # no usage key
        {"ts": "2026-05-23T13:00:00+00:00", "usage": None},  # explicit None
    ]
    out = compute_token_usage_history(turns)
    assert len(out) == 1
    assert out[0]["turn_count"] == 2
    assert out[0]["input_tokens"] == 0
    assert out[0]["output_tokens"] == 0


def test_turn_with_missing_ts_skipped():
    """No usable bucket for an unparseable ts — record is dropped, not
    silently aggregated into the wrong day."""
    turns = [
        _turn("2026-05-23T12:00:00+00:00", input_tokens=100),
        {"usage": {"input_tokens": 999}},  # no ts at all
        {"ts": "bogus", "usage": {"input_tokens": 999}},  # unparseable ts
    ]
    out = compute_token_usage_history(turns)
    assert len(out) == 1
    assert out[0]["input_tokens"] == 100


def test_cost_aggregates_only_when_present():
    """Cost is None unless at least one turn in the bucket has a
    numeric ``total_cost_usd``. Partial-data turns sum what's available."""
    turns = [
        _turn("2026-05-23T08:00:00+00:00", input_tokens=100, total_cost_usd=0.10),
        _turn("2026-05-23T16:00:00+00:00", input_tokens=200, total_cost_usd=0.20),
        _turn("2026-05-23T22:00:00+00:00", input_tokens=50, total_cost_usd=None),
    ]
    out = compute_token_usage_history(turns)
    assert out[0]["total_cost_usd"] == pytest.approx(0.30)


def test_cost_none_when_no_turn_has_cost():
    """Subscription-only deployments have ``total_cost_usd=None`` on
    every turn — the bucket's cost should stay None, not 0.0."""
    turns = [
        _turn("2026-05-23T08:00:00+00:00", input_tokens=100, total_cost_usd=None),
        _turn("2026-05-23T16:00:00+00:00", input_tokens=200, total_cost_usd=None),
    ]
    out = compute_token_usage_history(turns)
    assert out[0]["total_cost_usd"] is None


def test_ts_uses_z_suffix_handled():
    """ISO timestamps with ``Z`` instead of ``+00:00`` parse correctly."""
    turns = [_turn("2026-05-23T12:00:00Z", input_tokens=42)]
    out = compute_token_usage_history(turns)
    assert len(out) == 1
    assert out[0]["date"] == "2026-05-23"
    assert out[0]["input_tokens"] == 42


def test_non_utc_ts_converted_to_utc_date():
    """A turn at 2026-05-23T23:00:00-05:00 is actually 2026-05-24T04:00 UTC,
    so it lands in the 2026-05-24 bucket."""
    turns = [_turn("2026-05-23T23:00:00-05:00", input_tokens=100)]
    out = compute_token_usage_history(turns)
    assert len(out) == 1
    assert out[0]["date"] == "2026-05-24"


def test_non_dict_usage_silently_skipped():
    """A turn with a malformed (non-dict) ``usage`` field shouldn't
    crash; it just contributes zero tokens but still counts as a turn."""
    turns = [
        {"ts": "2026-05-23T12:00:00+00:00", "usage": "bogus-string"},
        {"ts": "2026-05-23T13:00:00+00:00", "usage": 12345},
    ]
    out = compute_token_usage_history(turns)
    assert len(out) == 1
    assert out[0]["turn_count"] == 2
    assert out[0]["input_tokens"] == 0
