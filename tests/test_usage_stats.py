"""Aggregation + rendering of usage stats from turns.jsonl."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.usage_stats import (
    UsageWindow,
    aggregate,
    context_window_for,
    render_usage_block,
)


def _ts(hours_ago: float = 0) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _turn(
    *, hours_ago: float, cost: float = 0.0,
    input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    output_tokens: int = 0,
    model: str | None = None,
) -> dict:
    rec = {
        "ts": _ts(hours_ago),
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "output_tokens": output_tokens,
        },
    }
    if model is not None:
        rec["model"] = model
    return rec


def _write_turns(path: Path, records: list[dict]) -> None:
    """JSONL is append-chronological — oldest record first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


# ---- aggregation -------------------------------------------------------


def test_aggregates_into_1h_5h_and_7d_windows(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    _write_turns(path, [
        _turn(hours_ago=200, cost=1.00, input_tokens=1000),  # outside all
        _turn(hours_ago=20, cost=2.00, input_tokens=2000),    # 7d only
        _turn(hours_ago=2, cost=3.00, input_tokens=3000),     # 5h + 7d
        _turn(hours_ago=0.1, cost=4.00, input_tokens=4000),   # all three
    ])
    rep = aggregate(path)

    win_1h = rep.windows[0]
    win_5h = rep.windows[1]
    win_7d = rep.windows[2]
    assert win_1h.label == "Last 1h"
    assert win_1h.turns == 1
    assert win_1h.total_cost_usd == 4.00

    assert win_5h.label == "Last 5h"
    assert win_5h.turns == 2
    assert win_5h.total_cost_usd == 7.00
    assert win_5h.input_tokens == 7000

    assert win_7d.label == "Last 7d"
    assert win_7d.turns == 3
    assert win_7d.total_cost_usd == 9.00
    assert win_7d.input_tokens == 9000


def test_aggregate_handles_empty_file(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    rep = aggregate(path)
    assert all(w.turns == 0 for w in rep.windows)
    assert rep.last_turn.ts is None


def test_aggregate_handles_missing_file(tmp_path: Path):
    rep = aggregate(tmp_path / "no" / "such.jsonl")
    assert all(w.turns == 0 for w in rep.windows)


def test_last_turn_snapshot_is_most_recent(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    _write_turns(path, [
        _turn(hours_ago=10, cost=1.00, input_tokens=100, output_tokens=50,
              model="claude-sonnet-4-6"),
        _turn(hours_ago=0.05, cost=2.00,
              input_tokens=200, cache_read_input_tokens=800,
              output_tokens=99, model="claude-opus-4-7"),
    ])
    rep = aggregate(path)
    assert rep.last_turn.input_tokens == 200
    assert rep.last_turn.cache_read_input_tokens == 800
    assert rep.last_turn.cost_usd == 2.00
    assert rep.last_turn.model == "claude-opus-4-7"


def test_short_circuit_stops_at_oldest_cutoff(tmp_path: Path):
    """A very long log shouldn't be fully scanned when the request
    window only spans a small slice of it. Synthetic but the bounded
    scan is the load-bearing property."""
    path = tmp_path / "turns.jsonl"
    records = [_turn(hours_ago=200 + i, cost=0.1) for i in range(500)]  # all old
    records.append(_turn(hours_ago=2, cost=5.00, input_tokens=10000))    # in 5h
    _write_turns(path, records)
    rep = aggregate(path)
    # windows[1] is 5h (windows[0] is the 1h window).
    win_5h = next(w for w in rep.windows if w.label == "Last 5h")
    assert win_5h.turns == 1
    assert win_5h.total_cost_usd == 5.00


def test_handles_records_without_usage(tmp_path: Path):
    """Older turns may have null usage (SDK was not capturing it).
    Aggregation must not crash."""
    path = tmp_path / "turns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ts": _ts(0.5), "total_cost_usd": None, "usage": None}) + "\n"
        + json.dumps({"ts": _ts(0.4), "total_cost_usd": 1.0,
                      "usage": {"input_tokens": 100}}) + "\n"
    )
    rep = aggregate(path)
    win_5h = rep.windows[0]
    assert win_5h.turns == 2
    assert win_5h.total_cost_usd == 1.0
    assert win_5h.input_tokens == 100


# ---- cache hit rate ----------------------------------------------------


def test_cache_hit_rate_fraction():
    w = UsageWindow(
        label="x", input_tokens=100,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=0,
    )
    assert abs(w.cache_hit_rate - 0.9) < 1e-9


def test_cache_hit_rate_zero_when_no_input():
    w = UsageWindow(label="x")
    assert w.cache_hit_rate == 0.0


def test_cache_hit_rate_excludes_output_from_denom():
    """Output tokens are billed separately and don't count for cache
    arithmetic — they're not "input that could've been cached"."""
    w = UsageWindow(
        label="x", input_tokens=100,
        cache_read_input_tokens=100,
        output_tokens=99999,
    )
    assert w.cache_hit_rate == 0.5


# ---- context window mapping --------------------------------------------


def test_context_window_known_models():
    assert context_window_for("claude-opus-4-7") == 200_000
    assert context_window_for("claude-opus-4-7[1m]") == 1_000_000
    assert context_window_for("claude-sonnet-4-6") == 200_000


def test_context_window_unknown_model_falls_back():
    assert context_window_for("some-future-model") == 200_000
    assert context_window_for(None) == 200_000


def test_context_window_1m_beta_lifts_opus_4_and_sonnet_4():
    """When the request opts into ``context-1m-2025-08-07``, Claude 4.x
    Opus and Sonnet jump to a 1M context cap. Bare-model defaults are
    unchanged when the beta isn't set."""
    from mimir.usage_stats import CONTEXT_1M_BETA
    assert context_window_for("claude-opus-4-7", betas=[CONTEXT_1M_BETA]) == 1_000_000
    assert context_window_for("claude-opus-4-5", betas=[CONTEXT_1M_BETA]) == 1_000_000
    assert context_window_for("claude-sonnet-4-6", betas=[CONTEXT_1M_BETA]) == 1_000_000
    # Haiku is excluded from the beta — stays at its bare-model cap.
    assert context_window_for("claude-haiku-4-5", betas=[CONTEXT_1M_BETA]) == 200_000
    # No beta, same model → bare-cap.
    assert context_window_for("claude-opus-4-7", betas=[]) == 200_000
    assert context_window_for("claude-opus-4-7") == 200_000
    # Unknown model name + beta → bare default (no prefix match).
    assert context_window_for("some-future-model", betas=[CONTEXT_1M_BETA]) == 200_000


# ---- rendering ---------------------------------------------------------


def test_render_returns_none_when_no_data(tmp_path: Path):
    rep = aggregate(tmp_path / "missing.jsonl")
    assert render_usage_block(rep) is None


def test_render_includes_last_turn_and_windows(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    _write_turns(path, [
        _turn(hours_ago=2, cost=1.50, input_tokens=2000,
              cache_read_input_tokens=8000, output_tokens=500),
        _turn(hours_ago=0.1, cost=0.50, input_tokens=1000,
              cache_read_input_tokens=9000, output_tokens=200,
              model="claude-opus-4-7[1m]"),
    ])
    rep = aggregate(path, fallback_model="claude-opus-4-7")
    out = render_usage_block(rep, fallback_model="claude-opus-4-7")
    assert out is not None
    # Last-turn line — model + cache hit %.
    assert "Last turn:" in out
    assert "claude-opus-4-7[1m]" in out
    assert "10k prompt" in out  # 1000 + 9000
    assert "200 out" in out
    # Window lines.
    assert "Last 5h:" in out
    assert "Last 7d:" in out
    # Aggregate cache hit visible.
    assert "cache hit" in out


def test_render_includes_budget_percent_when_configured(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    _write_turns(path, [
        _turn(hours_ago=2, cost=2.50, input_tokens=1000),
    ])
    rep = aggregate(path)
    out = render_usage_block(rep, budget_5h_usd=10.0)
    assert out is not None
    # 2.5 / 10 = 25%.
    assert "25% of $10.00" in out


def test_render_omits_budget_when_unset(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    _write_turns(path, [_turn(hours_ago=0.1, cost=1.0, input_tokens=10)])
    rep = aggregate(path)
    out = render_usage_block(rep)  # no budget
    assert out is not None
    assert "% of $" not in out


# ---- cost-rate alert ---------------------------------------------------


def test_evaluate_returns_none_when_no_thresholds(tmp_path: Path):
    from mimir.usage_stats import evaluate_cost_rate

    path = tmp_path / "turns.jsonl"
    _write_turns(path, [_turn(hours_ago=0.1, cost=10.0)])
    rep = aggregate(path)
    assert evaluate_cost_rate(rep) is None
    assert evaluate_cost_rate(rep, hourly_limit_usd=0, spike_ratio=0) is None


def test_evaluate_fires_on_absolute_hourly_limit(tmp_path: Path):
    from mimir.usage_stats import evaluate_cost_rate

    path = tmp_path / "turns.jsonl"
    # $10 in last hour vs $2/hr ceiling — clearly over.
    _write_turns(path, [
        _turn(hours_ago=0.5, cost=5.0),
        _turn(hours_ago=0.1, cost=5.0),
    ])
    rep = aggregate(path)
    alert = evaluate_cost_rate(rep, hourly_limit_usd=2.0)
    assert alert is not None
    assert alert.reason == "absolute_hourly_limit"
    assert alert.rate_now_usd_per_hour == 10.0
    assert alert.threshold_usd_per_hour == 2.0
    assert alert.baseline_usd_per_hour is None


def test_evaluate_fires_on_spike_ratio(tmp_path: Path):
    from mimir.usage_stats import evaluate_cost_rate

    path = tmp_path / "turns.jsonl"
    # 7d total $1.68 → baseline 0.01 USD/hr. last hour $5 → 500× ratio.
    # ratio threshold 3× → triggers.
    _write_turns(path, [
        _turn(hours_ago=24 * 6, cost=1.68),  # in 7d window only
        _turn(hours_ago=0.1, cost=5.0),       # in 1h window
    ])
    rep = aggregate(path)
    alert = evaluate_cost_rate(rep, spike_ratio=3.0)
    assert alert is not None
    assert alert.reason == "spike_ratio"
    assert alert.rate_now_usd_per_hour == 5.0
    assert alert.baseline_usd_per_hour is not None
    assert alert.baseline_usd_per_hour > 0


def test_evaluate_quiet_baseline_disables_spike_check(tmp_path: Path):
    """A baseline below the noise floor (1¢/hr) means we don't have
    enough signal — small spikes shouldn't false-positive."""
    from mimir.usage_stats import evaluate_cost_rate

    path = tmp_path / "turns.jsonl"
    _write_turns(path, [
        _turn(hours_ago=0.5, cost=0.50),  # quiet last hour
    ])
    rep = aggregate(path)
    # 7d baseline = 0.50 / 168 ≈ 0.003 USD/hr — below floor.
    alert = evaluate_cost_rate(rep, spike_ratio=2.0)
    assert alert is None


def test_evaluate_rate_now_floor_silences_spike(tmp_path: Path):
    """The asymmetry fix: even when both baseline and ratio say 'spike,'
    a rate_now below the floor means we're not in spend territory worth
    suppressing S4 over. Models the recurring false-positive shape:
    chatty session (a few cents/hour) over a tiny rolling baseline."""
    from mimir.usage_stats import evaluate_cost_rate

    path = tmp_path / "turns.jsonl"
    # Baseline: $5.04 over 7d → $0.03/hr (above the 1¢/hr noise floor,
    # so the existing baseline gate doesn't silence). Last hour: $0.19
    # — that's > 3× baseline, but well below the default $5/hr floor.
    _write_turns(path, [
        _turn(hours_ago=24 * 6, cost=5.04),
        _turn(hours_ago=0.1, cost=0.19),
    ])
    rep = aggregate(path)

    # With default floor ($5/hr): silenced.
    assert evaluate_cost_rate(rep, spike_ratio=3.0) is None

    # Floor disabled (None or 0): the spike fires as before.
    alert = evaluate_cost_rate(
        rep, spike_ratio=3.0, spike_floor_usd_per_hour=None,
    )
    assert alert is not None
    assert alert.reason == "spike_ratio"
    alert = evaluate_cost_rate(
        rep, spike_ratio=3.0, spike_floor_usd_per_hour=0,
    )
    assert alert is not None

    # Floor cleared at $0.10/hr: $0.19/hr clears it, spike still fires.
    alert = evaluate_cost_rate(
        rep, spike_ratio=3.0, spike_floor_usd_per_hour=0.10,
    )
    assert alert is not None
    assert alert.reason == "spike_ratio"


def test_evaluate_floor_does_not_affect_absolute_limit(tmp_path: Path):
    """The floor only gates the spike check — the absolute hourly limit
    is the real backstop and must still fire even when the spike side
    is silenced."""
    from mimir.usage_stats import evaluate_cost_rate

    path = tmp_path / "turns.jsonl"
    _write_turns(path, [_turn(hours_ago=0.1, cost=0.20)])
    rep = aggregate(path)
    # Floor at $1/hr would silence a spike on $0.20/hr — but the
    # absolute limit at $0.10/hr should still trip.
    alert = evaluate_cost_rate(
        rep,
        hourly_limit_usd=0.10,
        spike_ratio=3.0,
        spike_floor_usd_per_hour=1.0,
    )
    assert alert is not None
    assert alert.reason == "absolute_hourly_limit"


def test_absolute_threshold_takes_precedence_when_both_fire(tmp_path: Path):
    from mimir.usage_stats import evaluate_cost_rate

    path = tmp_path / "turns.jsonl"
    _write_turns(path, [
        _turn(hours_ago=24 * 6, cost=5.04),  # baseline 0.03/hr
        _turn(hours_ago=0.1, cost=20.0),      # both abs and ratio fire
    ])
    rep = aggregate(path)
    alert = evaluate_cost_rate(rep, hourly_limit_usd=10.0, spike_ratio=3.0)
    assert alert is not None
    assert alert.reason == "absolute_hourly_limit"


# ─── CR2-#8: baseline divisor clamps to file's actual coverage ───────


def test_aggregate_records_oldest_record_ts(tmp_path: Path):
    """``UsageReport.oldest_record_ts`` carries the timestamp of the
    oldest turn we walked. ``evaluate_cost_rate`` uses this to clamp
    the baseline divisor on partial-week installs."""
    from mimir.usage_stats import aggregate

    path = tmp_path / "turns.jsonl"
    _write_turns(path, [
        _turn(hours_ago=36, cost=1.0),    # oldest within 7d window
        _turn(hours_ago=0.1, cost=2.0),
    ])
    rep = aggregate(path)
    assert rep.oldest_record_ts is not None
    age_hours = (
        datetime.now(tz=timezone.utc) - rep.oldest_record_ts
    ).total_seconds() / 3600.0
    assert 35 < age_hours < 37  # ~36h ± a fudge for test runtime


def test_aggregate_oldest_record_ts_none_for_empty_file(tmp_path: Path):
    from mimir.usage_stats import aggregate

    path = tmp_path / "turns.jsonl"
    path.write_text("", encoding="utf-8")
    rep = aggregate(path)
    assert rep.oldest_record_ts is None


def test_evaluate_clamps_divisor_on_partial_week_data(tmp_path: Path):
    """CR2-#8: pre-fix, a fresh install with $5 spent over 36h would
    compute baseline = $5 / 168 = $0.030/hr (vs. the actual $5/36 =
    $0.139/hr). The 5× underestimate makes the spike check fire on
    normal-rate sessions during the install's first week. Post-fix,
    the divisor clamps to ``min(baseline_window_hours, hours_since_oldest)``,
    so a 36h-deep file uses divisor=36 and the baseline matches reality."""
    from mimir.usage_stats import aggregate, evaluate_cost_rate

    path = tmp_path / "turns.jsonl"
    _write_turns(path, [
        _turn(hours_ago=36, cost=5.0),     # spent $5 36h ago
        _turn(hours_ago=0.1, cost=6.0),    # last hour: $6/hr
    ])
    rep = aggregate(path)
    alert = evaluate_cost_rate(
        rep,
        spike_ratio=3.0,
        spike_floor_usd_per_hour=None,  # disable floor so we test only the divisor
    )
    # With the clamp, baseline = $5/36 ≈ $0.139/hr → 3× threshold ≈ $0.42/hr.
    # rate_now = $6/hr, well above. Alert fires.
    # WITHOUT the clamp, baseline = $5/168 ≈ $0.030/hr → 3× ≈ $0.09/hr.
    # rate_now = $6/hr also above. Both paths trigger here BUT the
    # threshold value is what proves the clamp is active.
    assert alert is not None
    assert alert.reason == "spike_ratio"
    # Threshold = 3 × baseline_rate. With clamp: ~0.42; without: ~0.09.
    # Tight check: > 0.30 means we used the clamped divisor.
    assert alert.threshold_usd_per_hour > 0.30, (
        f"baseline divisor not clamped — threshold {alert.threshold_usd_per_hour}"
        f" looks like 7d-divisor ($5/168) instead of 36h-divisor ($5/36)"
    )
    assert alert.baseline_usd_per_hour is not None
    assert alert.baseline_usd_per_hour > 0.10  # ~0.139 with clamp


def test_evaluate_defers_spike_check_under_1h_coverage(tmp_path: Path):
    """CR2-#8 corner case: with < 1h of file coverage, the baseline
    signal is too noisy to bother. Spike check returns None to defer
    the alert until enough data accumulates. Prevents the "fresh
    install fires spike on first turn" failure mode."""
    from mimir.usage_stats import aggregate, evaluate_cost_rate

    path = tmp_path / "turns.jsonl"
    _write_turns(path, [
        _turn(hours_ago=0.5, cost=1.0),    # 30 minutes of coverage
        _turn(hours_ago=0.1, cost=10.0),
    ])
    rep = aggregate(path)
    alert = evaluate_cost_rate(
        rep,
        spike_ratio=3.0,
        spike_floor_usd_per_hour=None,
    )
    # Even though rate_now=$10/hr is huge relative to any plausible
    # baseline, < 1h of coverage means we defer.
    assert alert is None


def test_evaluate_clamps_to_oldest_seen_in_window(tmp_path: Path):
    """The divisor is the age of the oldest record we accumulated,
    clamped to ``baseline_window_hours``. With records at 8d and 6d
    ago, the 8d record breaks the tail-walk (older than 7d cutoff)
    so ``oldest_record_ts`` == the 6d record. divisor = 144h, not
    168h — the clamp uses the file's actual coverage within the
    window, not the nominal window size."""
    from mimir.usage_stats import aggregate, evaluate_cost_rate

    path = tmp_path / "turns.jsonl"
    _write_turns(path, [
        _turn(hours_ago=24 * 8, cost=1.68),   # outside 7d — break fires here
        _turn(hours_ago=24 * 6, cost=1.68),   # oldest within 7d window
        _turn(hours_ago=0.1, cost=5.0),
    ])
    rep = aggregate(path)
    alert = evaluate_cost_rate(rep, spike_ratio=3.0)
    assert alert is not None
    assert alert.reason == "spike_ratio"
    # 7d window total = 1.68 + 5.0 = 6.68 (the 0.1h record is in both
    # 1h and 7d windows). divisor = 144 (clamped from 168 to age of
    # the 6d-ago oldest record). baseline = 6.68 / 144 ≈ 0.0464.
    # Without the clamp, this would be 6.68 / 168 ≈ 0.0398.
    assert alert.baseline_usd_per_hour == pytest.approx(0.046, abs=0.005)


def test_evaluate_baseline_unchanged_when_oldest_record_ts_unknown(tmp_path: Path):
    """Defensive: if ``oldest_record_ts`` is None (legacy code path,
    bench fixtures, etc.), the divisor stays at the nominal window
    — no clamp. Pre-CR2-#8 behavior preserved as the fallback."""
    from mimir.usage_stats import (
        UsageReport, UsageWindow, evaluate_cost_rate,
    )

    rep = UsageReport(
        windows=[
            UsageWindow(label="Last 1h", total_cost_usd=5.0, turns=1),
            UsageWindow(label="Last 7d", total_cost_usd=6.68, turns=2),
        ],
        oldest_record_ts=None,
    )
    alert = evaluate_cost_rate(rep, spike_ratio=3.0)
    assert alert is not None
    # divisor = 168 (no clamp). baseline = 6.68/168 ≈ 0.0398.
    assert alert.baseline_usd_per_hour == pytest.approx(0.0398, abs=0.001)


def test_render_includes_alert_annotation(tmp_path: Path):
    from mimir.usage_stats import CostRateAlert

    path = tmp_path / "turns.jsonl"
    _write_turns(path, [_turn(hours_ago=0.1, cost=2.0, input_tokens=100)])
    rep = aggregate(path)
    alert = CostRateAlert(
        reason="absolute_hourly_limit",
        rate_now_usd_per_hour=10.0,
        threshold_usd_per_hour=5.0,
        baseline_usd_per_hour=None,
    )
    out = render_usage_block(rep, alert=alert)
    assert out is not None
    assert "⚠" in out
    assert "Cost rate alert" in out
    assert "$10.00/hr" in out
    assert "$5.00/hr" in out
    assert "scaling back" in out


def test_render_alert_uses_spike_phrasing(tmp_path: Path):
    from mimir.usage_stats import CostRateAlert

    path = tmp_path / "turns.jsonl"
    _write_turns(path, [_turn(hours_ago=0.1, cost=2.0, input_tokens=100)])
    rep = aggregate(path)
    alert = CostRateAlert(
        reason="spike_ratio",
        rate_now_usd_per_hour=6.0,
        threshold_usd_per_hour=3.0,  # 3× of $1
        baseline_usd_per_hour=1.0,
    )
    out = render_usage_block(rep, alert=alert)
    assert out is not None
    assert "baseline" in out
    assert "$1.00/hr" in out


# ---- cooldown ---------------------------------------------------------


def test_cooldown_returns_false_when_no_recent_alert(tmp_path: Path):
    import json
    from mimir.usage_stats import cost_rate_alert_recently_emitted

    events = tmp_path / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        json.dumps({"timestamp": _ts(2), "type": "turn_started"}) + "\n"
        + json.dumps({"timestamp": _ts(1), "type": "tool_call"}) + "\n"
    )
    assert not cost_rate_alert_recently_emitted(events, cooldown_minutes=60)


def test_cooldown_returns_true_for_recent_alert(tmp_path: Path):
    import json
    from mimir.usage_stats import cost_rate_alert_recently_emitted

    events = tmp_path / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        json.dumps({"timestamp": _ts(0.1), "type": "cost_rate_alert"}) + "\n"
    )
    assert cost_rate_alert_recently_emitted(events, cooldown_minutes=60)


def test_cooldown_ignores_old_alert(tmp_path: Path):
    import json
    from mimir.usage_stats import cost_rate_alert_recently_emitted

    events = tmp_path / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        json.dumps({"timestamp": _ts(2), "type": "cost_rate_alert"}) + "\n"
    )
    assert not cost_rate_alert_recently_emitted(events, cooldown_minutes=60)


def test_cooldown_zero_disables(tmp_path: Path):
    """Cooldown=0 means "always emit" — the gate returns False so the
    caller proceeds with emission."""
    import json
    from mimir.usage_stats import cost_rate_alert_recently_emitted

    events = tmp_path / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        json.dumps({"timestamp": _ts(0.001), "type": "cost_rate_alert"}) + "\n"
    )
    assert not cost_rate_alert_recently_emitted(events, cooldown_minutes=0)


def test_find_window_lookup_by_hours_not_label(tmp_path: Path):
    """CR2 (ops & observability) fix: ``_find_window`` looks up by
    ``hours`` field on UsageWindow, not by regenerating the
    ``_default_label(hours)`` string. Caller-supplied custom
    ``window_labels`` to ``aggregate()`` no longer silently break
    the spike-ratio lookup."""
    from mimir.usage_stats import (
        UsageReport, UsageWindow, _find_window,
    )
    rep = UsageReport(windows=[
        UsageWindow(label="custom 1h", hours=1.0, total_cost_usd=5.0),
        UsageWindow(label="custom 7d", hours=24.0 * 7, total_cost_usd=1.68),
    ])
    # Lookup by hours succeeds even though label doesn't match
    # ``_default_label(1.0)`` ("Last 1h").
    found = _find_window(rep, 1.0)
    assert found is not None
    assert found.label == "custom 1h"
    assert found.total_cost_usd == 5.0


def test_find_window_legacy_fallback_via_label(tmp_path: Path):
    """Defensive: a UsageWindow constructed without ``hours`` (default
    0.0) falls back to label-string match for back-compat with old
    test fixtures and external callers."""
    from mimir.usage_stats import (
        UsageReport, UsageWindow, _find_window,
    )
    rep = UsageReport(windows=[
        UsageWindow(label="Last 1h", total_cost_usd=5.0),
    ])
    found = _find_window(rep, 1.0)
    assert found is not None
    assert found.total_cost_usd == 5.0
