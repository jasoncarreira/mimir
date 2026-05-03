"""Tests for §12.4 S3-S4 homeostat (mimir/budget.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.budget import (
    BudgetSnapshot,
    HomeostaticArbiter,
    _partition_turns,
)
from mimir.rate_limits import RateLimitSnapshot, RateLimitStore


NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def _write_turn(
    path: Path,
    *,
    ts: datetime,
    trigger: str,
    tool_calls: int = 0,
    tokens: int = 0,
    cost: float = 0.0,
) -> None:
    rec = {
        "ts": ts.isoformat(),
        "turn_id": "t" + ts.isoformat()[:19],
        "session_id": "s",
        "saga_session_id": None,
        "trigger": trigger,
        "channel_id": "chan",
        "input": "",
        "events": [
            {"type": "tool_call", "id": f"u{i}", "name": "Read", "args": {}}
            for i in range(tool_calls)
        ],
        "usage": {"input_tokens": tokens, "output_tokens": 0,
                  "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0},
        "total_cost_usd": cost,
    }
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def _arbiter(tmp_path: Path, **kwargs) -> HomeostaticArbiter:
    rls = RateLimitStore(path=tmp_path / "rate_limits.json")
    turns = tmp_path / "turns.jsonl"
    return HomeostaticArbiter(
        home=tmp_path,
        rate_limit_store=rls,
        turns_log=turns,
        **kwargs,
    )


# ─── _partition_turns ───────────────────────────────────────────────────


def test_partition_splits_s3_and_s4(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tool_calls=5)
    _write_turn(turns, ts=NOW - timedelta(hours=2),
                trigger="scheduled_tick", tool_calls=3)
    _write_turn(turns, ts=NOW - timedelta(hours=3),
                trigger="user_message", tool_calls=2)
    s3, s4, _, _ = _partition_turns(turns, now=NOW)
    assert s3 == 7
    assert s4 == 3


def test_partition_drops_old_turns(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tool_calls=5)
    _write_turn(turns, ts=NOW - timedelta(hours=48),  # > 24h, dropped
                trigger="user_message", tool_calls=99)
    s3, _, _, _ = _partition_turns(turns, now=NOW)
    assert s3 == 5


def test_partition_aggregates_tokens_24h_and_7d(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tokens=1000)
    _write_turn(turns, ts=NOW - timedelta(hours=48),
                trigger="user_message", tokens=2000)
    _write_turn(turns, ts=NOW - timedelta(days=10),  # > 7d, dropped
                trigger="user_message", tokens=99999)
    _, _, t24, t7d = _partition_turns(turns, now=NOW)
    assert t24 == 1000
    assert t7d == 3000


def test_partition_handles_missing_file(tmp_path: Path):
    s3, s4, t24, t7d = _partition_turns(tmp_path / "missing.jsonl", now=NOW)
    assert (s3, s4, t24, t7d) == (0, 0, 0, 0)


# ─── should_fire_heartbeat: layered constraints ─────────────────────────


def test_should_fire_when_no_signal(tmp_path: Path):
    arb = _arbiter(tmp_path)
    fire, reason = arb.should_fire_heartbeat(now=NOW)
    assert fire is True
    assert reason == "ok"


def test_plan_window_saturation_suppresses(tmp_path: Path):
    arb = _arbiter(tmp_path)
    # Far-future resets_at — RateLimitStore.current() filters stale
    # entries using real time.time(), not our injected NOW. Push the
    # reset out beyond real wall-clock so the entry survives.
    future = int((datetime.now(tz=timezone.utc) + timedelta(days=14)).timestamp())
    arb.rate_limit_store._load = lambda: {  # type: ignore[method-assign]
        "7d_opus": {
            "status": "allowed_warning",
            "utilization": 0.92,
            "resets_at": future,
            "observed_at": NOW.isoformat(),
        },
    }
    fire, reason = arb.should_fire_heartbeat(now=NOW)
    assert fire is False
    assert "plan_window_saturated" in reason
    assert "7d_opus" in reason


def test_plan_window_below_threshold_does_not_suppress(tmp_path: Path):
    arb = _arbiter(tmp_path, plan_window_suppress_threshold=0.80)
    future = int((datetime.now(tz=timezone.utc) + timedelta(days=14)).timestamp())
    arb.rate_limit_store._load = lambda: {  # type: ignore[method-assign]
        "7d": {
            "status": "allowed",
            "utilization": 0.50,
            "resets_at": future,
            "observed_at": NOW.isoformat(),
        },
    }
    fire, _ = arb.should_fire_heartbeat(now=NOW)
    assert fire is True


def test_cost_rate_alert_suppresses(tmp_path: Path):
    # Cost-rate flows through usage_stats.aggregate() which reads
    # real wall-clock; write turns with real-now timestamps so the
    # 1h window picks them up.
    real_now = datetime.now(tz=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    for _ in range(5):
        _write_turn(turns, ts=real_now - timedelta(minutes=10),
                    trigger="user_message", cost=2.0)
    arb = _arbiter(tmp_path, cost_hourly_limit_usd=5.0)
    fire, reason = arb.should_fire_heartbeat(now=real_now)
    assert fire is False
    assert "cost_rate_alert" in reason


def test_partition_no_longer_suppresses_busy_days(tmp_path: Path):
    """Review #7: the S3-share partition layer is now informational
    only — busy days (high S3 share) can no longer starve heartbeats.
    Only plan-window + cost-rate gate firing."""
    turns = tmp_path / "turns.jsonl"
    # 10 S3 calls, 1 S4 call → 91% S3 share, would have suppressed
    # under the old 0.80 threshold. New behavior: still fires.
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tool_calls=10)
    _write_turn(turns, ts=NOW - timedelta(hours=2),
                trigger="scheduled_tick", tool_calls=1)
    arb = _arbiter(tmp_path)
    fire, reason = arb.should_fire_heartbeat(now=NOW)
    assert fire is True
    assert reason == "ok"


# ─── render_self_state_block ────────────────────────────────────────────


def test_render_returns_none_with_no_signal(tmp_path: Path):
    arb = _arbiter(tmp_path)
    assert arb.render_self_state_block(now=NOW) is None


def test_render_includes_plan_window_when_present(tmp_path: Path):
    # RateLimitStore.current() filters out entries whose ``resets_at``
    # is before ``time.time()`` (real now, not the ``now`` arg). Anchor
    # the future timestamp on real-now so this test stays valid as the
    # calendar advances past the fixed NOW constant.
    arb = _arbiter(tmp_path)
    real_now = datetime.now(tz=timezone.utc)
    future = int((real_now + timedelta(days=2, hours=3)).timestamp())
    arb.rate_limit_store._load = lambda: {  # type: ignore[method-assign]
        "7d_opus": {
            "status": "allowed_warning",
            "utilization": 0.68,
            "resets_at": future,
            "observed_at": real_now.isoformat(),
        },
    }
    body = arb.render_self_state_block(now=real_now)
    assert body is not None
    assert "7d_opus" in body
    assert "68%" in body


def test_render_includes_cost_rate_with_limit(tmp_path: Path):
    real_now = datetime.now(tz=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    _write_turn(turns, ts=real_now - timedelta(minutes=30),
                trigger="user_message", cost=1.20)
    arb = _arbiter(tmp_path, cost_hourly_limit_usd=5.0)
    body = arb.render_self_state_block(now=real_now)
    assert body is not None
    assert "$1.20/hr" in body
    assert "$5.00/hr" in body


def test_render_includes_s3_s4_share(tmp_path: Path):
    """Partition is now informational-only — render still surfaces it
    so the agent can see how its day skews user-driven vs autonomous."""
    turns = tmp_path / "turns.jsonl"
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tool_calls=8)
    _write_turn(turns, ts=NOW - timedelta(hours=2),
                trigger="scheduled_tick", tool_calls=2)
    arb = _arbiter(tmp_path)
    body = arb.render_self_state_block(now=NOW)
    assert body is not None
    assert "S3/S4 tool-call share" in body
    assert "80%" in body  # 8/(8+2) = 80%


def test_render_includes_token_totals(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tokens=2_100_000)
    arb = _arbiter(tmp_path)
    body = arb.render_self_state_block(now=NOW)
    assert body is not None
    assert "tokens" in body
    assert "2.1M" in body


# ─── prompt integration ─────────────────────────────────────────────────


def test_build_turn_prompt_renders_self_state_section():
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    prompt = build_turn_prompt(
        AgentEvent(trigger="user_message", channel_id="chan",
                   author="alice", content="hi"),
        self_state_block="- 7d_opus window: 68% used (resets in 3d 4h)",
    )
    assert "## Self-state" in prompt
    assert "7d_opus window: 68% used" in prompt


def test_build_turn_prompt_omits_self_state_when_none():
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    prompt = build_turn_prompt(
        AgentEvent(trigger="user_message", channel_id="chan",
                   author="alice", content="hi"),
        self_state_block=None,
    )
    assert "## Self-state" not in prompt
