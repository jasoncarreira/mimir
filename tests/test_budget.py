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
    # JSONL is append-chronological — oldest first, newest last. CR#5
    # switched _partition_turns to tail-first reading with an early
    # break on `ts < cutoff_7d`, which requires the on-disk order to
    # be chronological-ascending (matching real mimir writes).
    _write_turn(turns, ts=NOW - timedelta(hours=3),
                trigger="user_message", tool_calls=2)
    _write_turn(turns, ts=NOW - timedelta(hours=2),
                trigger="scheduled_tick", tool_calls=3)
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tool_calls=5)
    s3, s4, _, _ = _partition_turns(turns, now=NOW)
    assert s3 == 7
    assert s4 == 3


def test_partition_drops_old_turns(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    _write_turn(turns, ts=NOW - timedelta(hours=48),  # > 24h, dropped
                trigger="user_message", tool_calls=99)
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tool_calls=5)
    s3, _, _, _ = _partition_turns(turns, now=NOW)
    assert s3 == 5


def test_partition_aggregates_tokens_24h_and_7d(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    _write_turn(turns, ts=NOW - timedelta(days=10),  # > 7d, dropped
                trigger="user_message", tokens=99999)
    _write_turn(turns, ts=NOW - timedelta(hours=48),
                trigger="user_message", tokens=2000)
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tokens=1000)
    _, _, t24, t7d = _partition_turns(turns, now=NOW)
    assert t24 == 1000
    assert t7d == 3000


def test_partition_handles_missing_file(tmp_path: Path):
    s3, s4, t24, t7d = _partition_turns(tmp_path / "missing.jsonl", now=NOW)
    assert (s3, s4, t24, t7d) == (0, 0, 0, 0)


def test_partition_early_break_on_7d_cutoff(tmp_path: Path):
    """CR#5 regression: with a chronological file, the tail-first walk
    must stop as soon as it encounters a record older than 7d. The 99K
    poison-token record at the head of the file would inflate the 7d
    total if the early break weren't in place."""
    turns = tmp_path / "turns.jsonl"
    # Way-old record at the start. If the early break works correctly,
    # the tail walk encounters the recent record first, then hits this
    # one and stops; this record's tokens never accrue.
    _write_turn(turns, ts=NOW - timedelta(days=30),
                trigger="user_message", tokens=99999, tool_calls=99)
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tokens=500, tool_calls=2)
    s3, _, t24, t7d = _partition_turns(turns, now=NOW)
    assert s3 == 2
    assert t24 == 500
    assert t7d == 500  # 99999 from the 30d-old record was excluded


# ─── should_fire: layered constraints ─────────────────────────


def test_should_fire_when_no_signal(tmp_path: Path):
    arb = _arbiter(tmp_path)
    decision = arb.should_fire(priority="low", now=NOW)
    assert decision.fire is True
    assert decision.reason == "ok"
    assert decision.severity.name == "CLEAR"


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
    low = arb.should_fire(priority="low", now=NOW)
    assert low.fire is False
    assert "plan_window_saturated" in low.reason
    assert "7d_opus" in low.reason
    assert low.severity.name == "TIGHT"
    # TIGHT sheds low + normal; high rides through a raw wall.
    assert arb.should_fire(priority="normal", now=NOW).fire is False
    assert arb.should_fire(priority="high", now=NOW).fire is True


def test_quota_mode_raw_wall_backstop_with_empty_provider(tmp_path: Path):
    """#483: in QUOTA mode a stub/cold provider yields CLEAR, but the raw
    plan-window wall from the rate-limit store must still backstop to TIGHT —
    otherwise the throttle runs wide open with no signal."""
    from mimir.billing import BillingMode

    arb = _arbiter(tmp_path, billing_mode=BillingMode.QUOTA, quota_providers=[])
    future = int((datetime.now(tz=timezone.utc) + timedelta(days=14)).timestamp())
    arb.rate_limit_store._load = lambda: {  # type: ignore[method-assign]
        "7d_opus": {
            "status": "allowed_warning",
            "utilization": 0.92,
            "resets_at": future,
            "observed_at": NOW.isoformat(),
        },
    }
    low = arb.should_fire(priority="low", now=NOW)
    assert low.fire is False
    assert low.severity.name == "TIGHT"
    assert "plan_window_saturated" in low.reason
    # Empty provider + no store saturation stays CLEAR (no false positive).
    arb2 = _arbiter(tmp_path, billing_mode=BillingMode.QUOTA, quota_providers=[])
    assert arb2.should_fire(priority="low", now=NOW).severity.name == "CLEAR"


def test_quota_backstop_ignores_stale_no_reset_window(tmp_path: Path):
    """#692 review: the QUOTA raw-wall backstop (#483) must not trust a
    ``resets_at=None`` reading older than its own window. ``current()`` keeps
    no-reset entries forever, so a rolled 5h window would otherwise pin TIGHT
    with nothing left under suppression to refresh the store. Mirror the
    provider path's staleness guard. Uses real-now-relative ``observed_at``
    because ``_is_stale_observation`` reads real wall-clock."""
    from mimir.billing import BillingMode

    real_now = datetime.now(tz=timezone.utc)
    stale = (real_now - timedelta(hours=6)).isoformat()  # older than the 5h window

    arb = _arbiter(tmp_path, billing_mode=BillingMode.QUOTA, quota_providers=[])
    arb.rate_limit_store._load = lambda: {  # type: ignore[method-assign]
        "openai_five_hour": {
            "status": "allowed_warning",
            "utilization": 0.95,
            "resets_at": None,
            "observed_at": stale,
        },
    }
    # Stale no-reset window is no-signal → backstop stays CLEAR, work fires.
    low = arb.should_fire(priority="low", now=real_now)
    assert low.fire is True
    assert low.severity.name == "CLEAR"

    # Control: a FRESH reading of the same no-reset window still walls to TIGHT.
    arb2 = _arbiter(tmp_path, billing_mode=BillingMode.QUOTA, quota_providers=[])
    arb2.rate_limit_store._load = lambda: {  # type: ignore[method-assign]
        "openai_five_hour": {
            "status": "allowed_warning",
            "utilization": 0.95,
            "resets_at": None,
            "observed_at": real_now.isoformat(),
        },
    }
    hot = arb2.should_fire(priority="low", now=real_now)
    assert hot.fire is False
    assert hot.severity.name == "TIGHT"
    assert "plan_window_saturated:openai_five_hour" in hot.reason


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
    assert arb.should_fire(priority="low", now=NOW).fire is True


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
    decision = arb.should_fire(priority="low", now=real_now)
    assert decision.fire is False
    assert "cost_rate_alert" in decision.reason
    assert decision.severity.name == "TIGHT"


def test_partition_no_longer_suppresses_busy_days(tmp_path: Path):
    """Review #7: the S3-share partition layer is now informational
    only — busy days (high S3 share) can no longer starve heartbeats.
    Only plan-window + cost-rate gate firing."""
    turns = tmp_path / "turns.jsonl"
    # 10 S3 calls, 1 S4 call → 91% S3 share, would have suppressed
    # under the old 0.80 threshold. New behavior: still fires.
    _write_turn(turns, ts=NOW - timedelta(hours=2),
                trigger="scheduled_tick", tool_calls=1)
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tool_calls=10)
    arb = _arbiter(tmp_path)
    decision = arb.should_fire(priority="low", now=NOW)
    assert decision.fire is True
    assert decision.reason == "ok"


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
    _write_turn(turns, ts=NOW - timedelta(hours=2),
                trigger="scheduled_tick", tool_calls=2)
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="user_message", tool_calls=8)
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


# ─── quota-pause integration (SPEC §4.9, §16 item 18) ─────────────────


def test_quota_pause_suppresses_heartbeat(tmp_path: Path):
    """When QuotaPauseTracker has an active pause, should_fire grades
    BLOCKED ("quota_exhausted_pause:...") regardless of utilization or
    priority. SPEC §4.9 / §16 item 18."""
    from datetime import datetime, timedelta, timezone
    from mimir.quota_pause import QuotaPauseTracker

    # Pre-write a pause file at the path the arbiter consults.
    pause_path = tmp_path / ".mimir" / "quota_pause.json"
    tracker = QuotaPauseTracker(pause_path)
    reset_at = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    tracker.pause_until(reset_at, reason="quota_exhausted", provider="anthropic")

    arb = _arbiter(tmp_path)
    decision = arb.should_fire(priority="low")
    assert not decision.fire
    assert decision.reason.startswith("quota_exhausted_pause:resets_at=")
    assert decision.severity.name == "BLOCKED"
    # The reset timestamp is surfaced in the reason so operator triage
    # via events.jsonl can see when the pause clears.
    assert reset_at.isoformat() in decision.reason
    # BLOCKED sheds EVERYTHING — even high-priority pollers: the
    # provider is actively refusing, headroom math is moot.
    assert arb.should_fire(priority="high").fire is False


def test_quota_pause_clears_after_reset(tmp_path: Path):
    """Past-reset pause → lazy-expiry deactivates the pause and the
    arbiter returns to its normal behavior. The next read of the
    tracker sees no active pause."""
    from datetime import datetime, timedelta, timezone
    from mimir.quota_pause import QuotaPauseTracker

    pause_path = tmp_path / ".mimir" / "quota_pause.json"
    tracker = QuotaPauseTracker(pause_path)
    # Reset 5 minutes ago — should lazy-expire on the first arbiter call.
    past = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    tracker.pause_until(past, reason="quota_exhausted")
    assert pause_path.is_file()

    arb = _arbiter(tmp_path)
    decision = arb.should_fire(priority="low")
    # Pause has expired → normal arbiter behavior. No utilization, no
    # cost rate, no other suppressors → should fire.
    assert decision.fire
    assert decision.reason == "ok"
    # Lazy-expiry deactivates the pause (preserving the escalation
    # counter), so a fresh read sees no active pause.
    assert QuotaPauseTracker(pause_path).is_paused().paused is False


def test_no_quota_pause_file_skips_check_cleanly(tmp_path: Path):
    """When no pause file exists, the arbiter skips the QuotaPauseTracker
    construction entirely (the ``if pause_path.is_file()`` guard).
    Confirmed by behavior: arbiter returns normal decision with no
    side effects on the .mimir dir."""
    arb = _arbiter(tmp_path)
    decision = arb.should_fire(priority="low")
    assert decision.fire
    assert decision.reason == "ok"
    # No .mimir dir got created as a side effect — the early guard
    # prevented an unnecessary tracker.
    assert not (tmp_path / ".mimir").exists()


def test_quota_recovered_uses_run_coroutine_threadsafe_when_loop_provided(tmp_path: Path):
    """When ``event_loop`` is passed to ``should_fire``
    (the normal ``asyncio.to_thread`` path), the ``quota_recovered``
    coroutine is submitted via ``asyncio.run_coroutine_threadsafe``
    rather than ``get_running_loop()`` — which would raise RuntimeError
    in a worker thread. Chainlink #184."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch
    from mimir.quota_pause import QuotaPauseTracker

    pause_path = tmp_path / ".mimir" / "quota_pause.json"
    tracker = QuotaPauseTracker(pause_path)
    # Pause that expired 5 minutes ago — triggers the lazy-expiry +
    # quota_recovered emission path.
    past = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    tracker.pause_until(past, reason="quota_exhausted")

    arb = _arbiter(tmp_path)

    submitted: list[object] = []

    def _capture_threadsafe(coro, loop):  # noqa: ARG001
        submitted.append(coro)
        coro.close()  # avoid ResourceWarning — we're not running it

    with patch("asyncio.run_coroutine_threadsafe", _capture_threadsafe):
        decision = arb.should_fire(priority="low", event_loop=object())

    assert decision.fire
    assert decision.reason == "ok"
    assert len(submitted) == 1, (
        "run_coroutine_threadsafe should have been called exactly once "
        f"for quota_recovered; calls: {submitted}"
    )


@pytest.mark.asyncio
async def test_quota_recovered_emits_from_to_thread(tmp_path: Path):
    """End-to-end: when the scheduler's ``asyncio.to_thread`` shape runs
    ``should_fire`` in a worker thread, ``quota_recovered``
    actually fires on the main event loop. Chainlink #184."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from mimir.quota_pause import QuotaPauseTracker

    pause_path = tmp_path / ".mimir" / "quota_pause.json"
    tracker = QuotaPauseTracker(pause_path)
    past = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    tracker.pause_until(past, reason="quota_exhausted")

    emitted: list[str] = []

    async def _fake_log_event(kind: str, **_kwargs) -> None:
        emitted.append(kind)

    import mimir.event_logger as _el_mod
    original_log = _el_mod.log_event
    _el_mod.log_event = _fake_log_event  # type: ignore[assignment]
    try:
        _loop = asyncio.get_running_loop()
        arb = _arbiter(tmp_path)
        decision = await asyncio.to_thread(
            arb.should_fire,
            priority="low",
            event_loop=_loop,
        )
        # Yield to the event loop so run_coroutine_threadsafe's task runs.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        _el_mod.log_event = original_log  # type: ignore[assignment]

    assert decision.fire
    assert decision.reason == "ok"
    assert "quota_recovered" in emitted, (
        f"quota_recovered not emitted; got: {emitted}"
    )


# ─── pay-as-you-go ELEVATED early warning ───────────────────────────────


def test_payg_near_limit_is_elevated(tmp_path: Path):
    """Rate within 80% of the hourly limit but not over it → ELEVATED:
    low sheds, normal still fires (graduated band, not a binary trip)."""
    real_now = datetime.now(tz=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    # $4.50/hr against a $5 limit → 90% of limit, alert not tripped.
    for _ in range(3):
        _write_turn(turns, ts=real_now - timedelta(minutes=10),
                    trigger="user_message", cost=1.5)
    arb = _arbiter(tmp_path, cost_hourly_limit_usd=5.0)
    low = arb.should_fire(priority="low", now=real_now)
    assert low.fire is False
    assert low.severity.name == "ELEVATED"
    assert "cost_rate_near_limit" in low.reason
    assert arb.should_fire(priority="normal", now=real_now).fire is True


def test_payg_well_under_limit_is_clear(tmp_path: Path):
    real_now = datetime.now(tz=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    # $1/hr against a $5 limit → 20%, comfortably CLEAR.
    _write_turn(turns, ts=real_now - timedelta(minutes=10),
                trigger="user_message", cost=1.0)
    arb = _arbiter(tmp_path, cost_hourly_limit_usd=5.0)
    decision = arb.should_fire(priority="low", now=real_now)
    assert decision.fire is True
    assert decision.severity.name == "CLEAR"


def test_render_self_state_includes_throttle_line_when_pressured(tmp_path: Path):
    """The agent should see WHY autonomous work went quiet — the
    severity + reason render into the Self-state block."""
    real_now = datetime.now(tz=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    for _ in range(5):
        _write_turn(turns, ts=real_now - timedelta(minutes=10),
                    trigger="user_message", cost=2.0)
    arb = _arbiter(tmp_path, cost_hourly_limit_usd=5.0)
    block = arb.render_self_state_block(now=real_now)
    assert block is not None
    assert "autonomy throttle: TIGHT" in block
    assert "cost_rate_alert" in block
    assert "low+normal scheduled work shedding" in block


def test_render_self_state_no_throttle_line_when_clear(tmp_path: Path):
    real_now = datetime.now(tz=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    _write_turn(turns, ts=real_now - timedelta(minutes=10),
                trigger="user_message", cost=0.1)
    arb = _arbiter(tmp_path)
    block = arb.render_self_state_block(now=real_now)
    assert block is not None
    assert "autonomy throttle" not in block


def test_render_threads_event_loop_to_assess(tmp_path: Path):
    """#489: render_self_state_block runs under asyncio.to_thread, so it must
    forward the caller's event_loop to assess() — otherwise a lazy-expiry
    quota_recovered emit is dropped on the worker thread (no running loop)."""
    arb = _arbiter(tmp_path)
    captured = {}
    real_assess = arb.assess

    def _spy(*, now=None, event_loop=None):
        captured["event_loop"] = event_loop
        return real_assess(now=now, event_loop=event_loop)

    arb.assess = _spy  # type: ignore[method-assign]
    sentinel = object()
    arb.render_self_state_block(now=NOW, event_loop=sentinel)
    assert captured["event_loop"] is sentinel
