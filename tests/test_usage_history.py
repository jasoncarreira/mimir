"""Unit tests for ``mimir.usage_history``.

Covers the three subscription-event shapes (minimax / anthropic OAuth /
codex plus), the provider→window→series output schema, the multi-
provider deployment case (Anthropic OAuth + Codex Plus active in the
same agent), and the downsample-to-max-points behavior.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mimir.usage_history import (
    UsagePoint,
    compute_usage_history,
    normalize_subscription_events,
)


def _ts(minutes_ago: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat()


def _minimax_event(ts: str, five_h: float, seven_d: float) -> dict:
    return {
        "timestamp": ts,
        "type": "minimax_usage_ok",
        "recorded": {
            "minimax_five_hour": {
                "utilization": five_h,
                "resets_at": 1779408000,
                "status": "allowed",
            },
            "minimax_seven_day": {
                "utilization": seven_d,
                "resets_at": 1779667200,
                "status": "allowed",
            },
        },
    }


def _anthropic_event(
    ts: str, five_h: float, seven_d: float, sonnet: float = 0.04,
) -> dict:
    return {
        "timestamp": ts,
        "type": "oauth_usage_ok",
        "recorded": {
            "five_hour": {
                "utilization": five_h,
                "resets_at": 1779406800,
                "status": "allowed",
            },
            "seven_day": {
                "utilization": seven_d,
                "resets_at": 1779908400,
                "status": "allowed",
            },
            "seven_day_sonnet": {
                "utilization": sonnet,
                "resets_at": 1779908400,
                "status": "allowed",
            },
        },
    }


def _codex_event(ts: str, five_h: float, seven_d: float) -> dict:
    return {
        "timestamp": ts,
        "type": "codex_plus_usage_ok",
        "recorded": {
            "five_hour": {
                "utilization": five_h,
                "resets_at": 1779406800,
                "status": "allowed",
            },
            "seven_day": {
                "utilization": seven_d,
                "resets_at": 1779908400,
                "status": "allowed",
            },
        },
    }


# ---- normalize_subscription_events --------------------------------------


def test_normalize_minimax_strips_provider_prefix():
    # minimax poller stores RateLimitStore keys (``minimax_five_hour``)
    # in the event payload; usage_history strips the prefix so the
    # output uses uniform window names.
    events = [_minimax_event(_ts(0), 0.27, 0.11)]
    out = normalize_subscription_events(events)
    assert set(out["minimax"].keys()) == {"five_hour", "seven_day"}
    pt = out["minimax"]["five_hour"][0]
    assert pt.utilization == pytest.approx(0.27)
    assert pt.resets_at == 1779408000


def test_normalize_anthropic_keeps_all_subwindows():
    events = [_anthropic_event(_ts(0), 0.02, 0.18, sonnet=0.04)]
    out = normalize_subscription_events(events)
    # Three sub-windows on Anthropic OAuth: aggregate + model-scoped.
    assert set(out["anthropic"].keys()) == {
        "five_hour", "seven_day", "seven_day_sonnet",
    }
    assert out["anthropic"]["seven_day_sonnet"][0].utilization == pytest.approx(0.04)


def test_normalize_codex_plus():
    events = [_codex_event(_ts(0), 0.10, 0.30)]
    out = normalize_subscription_events(events)
    assert set(out["codex_plus"].keys()) == {"five_hour", "seven_day"}


def test_normalize_multi_provider_deployment():
    # User-stated case: Opus on Anthropic Max OAuth for chat + Codex
    # Plus for saga LLM calls. BOTH providers appear in the output.
    events = [
        _anthropic_event(_ts(2), 0.05, 0.20),
        _codex_event(_ts(1), 0.15, 0.40),
        _anthropic_event(_ts(0), 0.06, 0.20),
    ]
    out = normalize_subscription_events(events)
    assert set(out.keys()) == {"anthropic", "codex_plus"}
    assert len(out["anthropic"]["five_hour"]) == 2
    assert len(out["codex_plus"]["five_hour"]) == 1


def test_normalize_drops_unknown_event_types():
    events = [
        {"timestamp": _ts(0), "type": "turn_finished"},
        {"timestamp": _ts(0), "type": "saga_session_started"},
        _minimax_event(_ts(0), 0.1, 0.05),
    ]
    out = normalize_subscription_events(events)
    assert set(out.keys()) == {"minimax"}


def test_normalize_skips_malformed_records():
    events = [
        None,  # non-dict
        {"type": "minimax_usage_ok"},  # missing timestamp
        {"timestamp": _ts(0), "type": "minimax_usage_ok"},  # missing recorded
        {  # malformed snapshot (utilization is a string)
            "timestamp": _ts(0),
            "type": "minimax_usage_ok",
            "recorded": {"minimax_five_hour": {"utilization": "bad", "resets_at": 1}},
        },
        _minimax_event(_ts(0), 0.1, 0.05),  # one good record at the end
    ]
    out = normalize_subscription_events(events)
    assert len(out["minimax"]["five_hour"]) == 2
    # Malformed utilization comes through as None; good record has 0.1.
    utils = [p.utilization for p in out["minimax"]["five_hour"]]
    assert None in utils
    assert pytest.approx(0.1) in utils


def test_normalize_tolerates_legacy_windows_key():
    # Codex Plus's older quota_capture_ok event used ``windows`` instead
    # of ``recorded``. We tolerate either so historical data isn't lost
    # if the writer gets renamed forward.
    legacy = {
        "timestamp": _ts(0),
        "type": "codex_plus_usage_ok",
        "windows": {
            "five_hour": {"utilization": 0.5, "resets_at": 100},
        },
    }
    out = normalize_subscription_events([legacy])
    assert out["codex_plus"]["five_hour"][0].utilization == pytest.approx(0.5)


# ---- compute_usage_history (end-to-end + downsampling) ------------------


def test_compute_usage_history_emits_serializable_json():
    out = compute_usage_history(
        [_minimax_event(_ts(0), 0.27, 0.11)], days=7,
    )
    # JSON-serializable: every leaf must be primitive (no UsagePoint).
    point = out["minimax"]["five_hour"][0]
    assert isinstance(point, dict)
    assert point["utilization"] == pytest.approx(0.27)
    assert point["resets_at"] == 1779408000
    assert isinstance(point["ts"], str)


def test_compute_usage_history_omits_empty_providers():
    # Only Minimax events present → only "minimax" key in output. An
    # Anthropic-OAuth-only deployment doesn't get an empty Codex Plus
    # chart rendered.
    out = compute_usage_history(
        [_minimax_event(_ts(0), 0.27, 0.11)], days=7,
    )
    assert set(out.keys()) == {"minimax"}


def test_compute_usage_history_downsamples_below_cap():
    # 600 raw points spread evenly across an hour. With
    # max_points_per_series=200 we expect ≤200 in the output.
    events = []
    for m in range(600):
        events.append(_minimax_event(_ts(m), 0.5, 0.2))
    out = compute_usage_history(events, days=7, max_points_per_series=200)
    assert len(out["minimax"]["five_hour"]) <= 200
    # Should keep meaningful detail — not collapse to a single point.
    assert len(out["minimax"]["five_hour"]) > 10


def test_compute_usage_history_passes_through_below_cap():
    # 50 raw points and cap of 200 → all 50 returned, no bucketing loss.
    events = [_minimax_event(_ts(m), 0.5, 0.2) for m in range(50)]
    out = compute_usage_history(events, days=7, max_points_per_series=200)
    assert len(out["minimax"]["five_hour"]) == 50


def test_downsample_keeps_last_value_per_bucket():
    # Two timestamps within the same bucket — only the LATER one
    # should survive (last-value-per-bucket). Encoded by giving them
    # different utilization values and asserting the later one wins.
    base = datetime.now(timezone.utc)
    early = base - timedelta(minutes=10)
    late = base
    events = [
        _minimax_event(early.isoformat(), 0.1, 0.05),
        _minimax_event(late.isoformat(), 0.9, 0.5),
    ]
    out = compute_usage_history(events, days=7, max_points_per_series=1)
    # max_points=1 forces both into the same bucket → late wins.
    assert out["minimax"]["five_hour"][0]["utilization"] == pytest.approx(0.9)
