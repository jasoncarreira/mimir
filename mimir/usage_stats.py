"""Aggregate token usage / cache hit rate / cost over time.

Source of truth is ``logs/turns.jsonl`` — the per-turn ``usage`` dict
the SDK populates from Anthropic's ``ResultMessage`` plus the
``total_cost_usd`` field. This module reads the tail of that file and
sums by time window so the agent sees:

- Cache hit rate (proportion of input tokens served from prompt cache)
- Cost over rolling 1h / 5h / 7d windows
- Token totals per window
- Last turn's context-window utilization vs. the model cap

Plan-window utilization (5-hour rolling, 7-day plan / Opus / Sonnet,
overage) lives in ``mimir/rate_limits.py`` — that data comes from the
SDK's ``RateLimitEvent`` stream, not from turns.jsonl. The two are
complementary: this module shows *cost* over time (in dollars,
calculable from per-turn tokens × per-token rates), while rate_limits
shows *plan unit consumption* (the same numbers Claude Code's
``/usage`` surfaces).

The operator-configurable budgets ``MIMIR_USAGE_5H_LIMIT_USD`` /
``MIMIR_USAGE_WEEKLY_LIMIT_USD`` annotate dollar consumption against
operator-set thresholds — useful as a *cost* ceiling but distinct
from the plan's unit budget which the SDK tracks separately.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from ._jsonl_tail import tail_jsonl_records

log = logging.getLogger(__name__)


# Model-name → max input context window (tokens). Used to render
# "current turn / cap" percentage in the usage block. Best-effort:
# unknown models fall back to ``_DEFAULT_CONTEXT_WINDOW``.
#
# Sources: Anthropic API docs as of the cutoff. The 1M variant of
# Opus 4.7 reports its own model id (``claude-opus-4-7[1m]``) in the
# Claude Code surface; bare ``claude-opus-4-7`` is the 200k variant.
_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Opus 4.x
    "claude-opus-4-7": 200_000,
    "claude-opus-4-7[1m]": 1_000_000,
    "claude-opus-4-6": 200_000,
    "claude-opus-4-5": 200_000,
    # Sonnet 4.x
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    # Haiku 4.x
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}

_DEFAULT_CONTEXT_WINDOW = 200_000


@dataclass
class UsageWindow:
    """Aggregated usage over a contiguous time window. Field semantics
    match the SDK's ``usage`` dict (Anthropic's ``Message.usage`` shape):
    counts are tokens; cost is dollars."""

    label: str
    turns: int = 0
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0

    @property
    def total_input_tokens(self) -> int:
        """Input + cached input combined — i.e. the full prompt size on
        the wire, regardless of whether it was a cache hit or not."""
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def cache_hit_rate(self) -> float:
        """Proportion of input tokens served from cache. 0.0 when the
        window has no input tokens recorded (avoids div/0)."""
        denom = self.total_input_tokens
        if denom <= 0:
            return 0.0
        return self.cache_read_input_tokens / denom


@dataclass
class LastTurnSnapshot:
    """The most recent turn's per-call shape — used for the "current
    context utilization" line. Distinct from UsageWindow because it's
    a single sample, not an aggregate."""

    ts: str | None = None
    model: str | None = None
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_prompt_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def cache_hit_rate(self) -> float:
        denom = self.total_prompt_tokens
        if denom <= 0:
            return 0.0
        return self.cache_read_input_tokens / denom


@dataclass
class UsageReport:
    last_turn: LastTurnSnapshot = field(default_factory=LastTurnSnapshot)
    windows: list[UsageWindow] = field(default_factory=list)


@dataclass(frozen=True)
class CostRateAlert:
    """Triggered when current hourly spend exceeds one of two thresholds:

    - **absolute_hourly_limit**: ``rate_now_usd_per_hour`` >
      ``MIMIR_COST_HOURLY_LIMIT_USD``. Operator-set ceiling; useful for
      "never go above $X/hour even if we usually do."
    - **spike_ratio**: ``rate_now_usd_per_hour`` >
      ``spike_ratio * baseline_usd_per_hour``. Adapts to your usual
      spend; catches runaway loops (3× the rolling-week average is the
      default).

    When both fire, the absolute one wins (deterministic; no double-
    counted reasons).
    """

    reason: str  # "absolute_hourly_limit" | "spike_ratio"
    rate_now_usd_per_hour: float
    threshold_usd_per_hour: float
    baseline_usd_per_hour: float | None  # populated only for spike_ratio


def context_window_for(model: str | None) -> int:
    """Look up the model's max input context. Unknown / None → default."""
    if not model:
        return _DEFAULT_CONTEXT_WINDOW
    return _MODEL_CONTEXT_WINDOWS.get(model, _DEFAULT_CONTEXT_WINDOW)


def aggregate(
    turns_path: Path,
    *,
    window_hours: Iterable[float] = (1.0, 5.0, 24.0 * 7),
    window_labels: Iterable[str] | None = None,
    fallback_model: str | None = None,
) -> UsageReport:
    """Walk turns.jsonl tail-first; bucket each turn's usage into the
    matching windows. Single pass — stops once the oldest window's
    cutoff is exceeded.

    ``fallback_model`` is what the last-turn snapshot reports when the
    record itself doesn't carry a model field (mimir's TurnRecord
    didn't capture it pre-this-commit; the operator's configured model
    is the right fallback).
    """
    windows = [float(h) for h in window_hours]
    if window_labels is None:
        window_labels = [_default_label(h) for h in windows]
    else:
        window_labels = list(window_labels)
    assert len(windows) == len(window_labels), "windows / labels length mismatch"

    now = datetime.now(tz=timezone.utc)
    cutoffs = [now - timedelta(hours=h) for h in windows]
    # Sort window indices by cutoff age (newest cutoff first) so we can
    # short-circuit cleanly once the OLDEST cutoff is passed.
    order = sorted(range(len(windows)), key=lambda i: cutoffs[i], reverse=True)
    oldest_cutoff = min(cutoffs)

    out_windows = [UsageWindow(label=label) for label in window_labels]
    last_turn = LastTurnSnapshot()
    saw_first = False

    for rec in tail_jsonl_records(turns_path):
        ts_str = rec.get("ts")
        if not isinstance(ts_str, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        if not saw_first:
            saw_first = True
            usage = rec.get("usage") or {}
            last_turn = LastTurnSnapshot(
                ts=ts_str,
                model=rec.get("model") or _model_from_events(rec) or fallback_model,
                input_tokens=int(usage.get("input_tokens") or 0),
                cache_creation_input_tokens=int(
                    usage.get("cache_creation_input_tokens") or 0
                ),
                cache_read_input_tokens=int(
                    usage.get("cache_read_input_tokens") or 0
                ),
                output_tokens=int(usage.get("output_tokens") or 0),
                cost_usd=float(rec.get("total_cost_usd") or 0.0),
            )

        if ts < oldest_cutoff:
            # JSONL is chronological; tail-first iteration newest-first.
            # First record older than the oldest cutoff means everything
            # remaining is also older. Stop scanning.
            break

        usage = rec.get("usage") or {}
        for idx in order:
            if ts >= cutoffs[idx]:
                w = out_windows[idx]
                w.turns += 1
                w.input_tokens += int(usage.get("input_tokens") or 0)
                w.cache_creation_input_tokens += int(
                    usage.get("cache_creation_input_tokens") or 0
                )
                w.cache_read_input_tokens += int(
                    usage.get("cache_read_input_tokens") or 0
                )
                w.output_tokens += int(usage.get("output_tokens") or 0)
                w.total_cost_usd += float(rec.get("total_cost_usd") or 0.0)

    return UsageReport(last_turn=last_turn, windows=out_windows)


def evaluate_cost_rate(
    report: UsageReport,
    *,
    hourly_limit_usd: float | None = None,
    spike_ratio: float | None = None,
    baseline_window_hours: float = 24.0 * 7,
    current_window_hours: float = 1.0,
) -> CostRateAlert | None:
    """Inspect ``report`` and decide whether the current hourly spend
    rate has crossed either threshold. Returns the alert (with the
    reason that fired) or ``None`` when neither does.

    The current rate is taken from the window of size
    ``current_window_hours`` (default 1h) — its total cost equals the
    last-hour spend, which IS the per-hour rate. The baseline is the
    longer window (default 7d) divided by its hour count; a value
    around 0 is treated as "no baseline" and disables the spike check
    so a quiet history doesn't false-positive on a single $0.50 turn.

    ``hourly_limit_usd <= 0`` or ``None`` disables the absolute check.
    ``spike_ratio <= 0`` or ``None`` disables the spike check.
    """
    cur = _find_window(report, current_window_hours)
    if cur is None:
        return None
    rate_now = cur.total_cost_usd  # cost over a 1h window IS $/hr

    # Absolute hourly limit takes precedence — deterministic ordering
    # for the case where both fire.
    if hourly_limit_usd and hourly_limit_usd > 0 and rate_now > hourly_limit_usd:
        return CostRateAlert(
            reason="absolute_hourly_limit",
            rate_now_usd_per_hour=rate_now,
            threshold_usd_per_hour=hourly_limit_usd,
            baseline_usd_per_hour=None,
        )

    if spike_ratio and spike_ratio > 0:
        baseline_w = _find_window(report, baseline_window_hours)
        if baseline_w is not None and baseline_window_hours > 0:
            baseline_rate = baseline_w.total_cost_usd / baseline_window_hours
            # 1¢/hr noise floor — below this we have no baseline signal
            # and the spike check is meaningless.
            if baseline_rate >= 0.01 and rate_now > spike_ratio * baseline_rate:
                return CostRateAlert(
                    reason="spike_ratio",
                    rate_now_usd_per_hour=rate_now,
                    threshold_usd_per_hour=spike_ratio * baseline_rate,
                    baseline_usd_per_hour=baseline_rate,
                )
    return None


def _find_window(report: UsageReport, hours: float) -> UsageWindow | None:
    """Lookup helper: pick the window whose label matches the
    expected ``Last Nh`` / ``Last Nd`` token, or fall back to the
    first one with the matching turn count > 0. Returns None when
    nothing matches."""
    target = _default_label(hours)
    for w in report.windows:
        if w.label == target:
            return w
    return None


def event_recently_emitted(
    events_path: Path, event_type: str, *, cooldown_minutes: int,
) -> bool:
    """True if any event with ``type == event_type`` lies within the
    cooldown window. Used to gate spike-style alert emissions
    (cost_rate_alert, rate_limit_off_pace, etc.) so the firehose
    doesn't churn on a sustained condition — the algedonic block
    surfaces the most recent occurrence anyway."""
    if cooldown_minutes <= 0:
        return False
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=cooldown_minutes)
    cutoff_iso = cutoff.isoformat()
    for ev in tail_jsonl_records(events_path):
        ts = ev.get("timestamp")
        if not isinstance(ts, str):
            continue
        if ts < cutoff_iso:
            return False  # tail-first; everything older than this is too
        if ev.get("type") == event_type:
            return True
    return False


# Backwards-compatible alias for callers built before the rename.
def cost_rate_alert_recently_emitted(
    events_path: Path, *, cooldown_minutes: int,
) -> bool:
    return event_recently_emitted(
        events_path, "cost_rate_alert", cooldown_minutes=cooldown_minutes,
    )


def render_usage_block(
    report: UsageReport,
    *,
    fallback_model: str | None = None,
    budget_5h_usd: float | None = None,
    budget_weekly_usd: float | None = None,
    alert: CostRateAlert | None = None,
    plan_quota_lines: list[str] | None = None,
    off_pace_warning: list[str] | None = None,
) -> str | None:
    """Format the usage report as a markdown body for the
    "## Resource usage" prompt section. Returns None when there's
    nothing to show (no last turn, no aggregated windows with data)."""
    has_windows = any(w.turns > 0 for w in report.windows)
    has_plan_quotas = bool(plan_quota_lines)
    has_off_pace = bool(off_pace_warning)
    if (
        report.last_turn.ts is None
        and not has_windows
        and not has_plan_quotas
        and not has_off_pace
    ):
        return None

    lines: list[str] = []

    # Last-turn snapshot — what the most recent prompt cost.
    if report.last_turn.ts is not None:
        lt = report.last_turn
        model = lt.model or fallback_model or "?"
        ctx_max = context_window_for(model)
        ctx_pct = (lt.total_prompt_tokens / ctx_max * 100) if ctx_max else 0.0
        hit_pct = lt.cache_hit_rate * 100
        lines.append(
            f"Last turn: {_fmt_int(lt.total_prompt_tokens)} prompt + "
            f"{_fmt_int(lt.output_tokens)} out tokens  "
            f"(${lt.cost_usd:.4f}; cache hit {hit_pct:.0f}%; "
            f"context {ctx_pct:.0f}% of {_fmt_int(ctx_max)} on {model})"
        )

    if has_windows:
        if lines:
            lines.append("")  # blank line between sections
        for w in report.windows:
            if w.turns == 0:
                continue
            tail: list[str] = []
            tail.append(f"{w.turns} turn(s)")
            tail.append(f"{_fmt_int(w.total_input_tokens)} prompt tokens")
            tail.append(f"cache hit {w.cache_hit_rate * 100:.0f}%")
            budget = (
                budget_5h_usd if w.label.startswith("Last 5h")
                else budget_weekly_usd if w.label.startswith("Last 7d")
                else None
            )
            cost_part = f"${w.total_cost_usd:.2f}"
            if budget and budget > 0:
                cost_part += f" ({w.total_cost_usd / budget * 100:.0f}% of ${budget:.2f})"
            lines.append(f"{w.label}: {cost_part} / " + " / ".join(tail))

    if has_plan_quotas:
        if lines:
            lines.append("")
        lines.append("Plan windows (from Anthropic):")
        for line in plan_quota_lines or []:
            lines.append(f"- {line}")

    if has_off_pace:
        if lines:
            lines.append("")
        # The off-pace block is itself a multi-line warning paragraph
        # (verb line + per-bucket bullets). Caller built it via
        # render_off_pace_warning; we just splice it in.
        for line in off_pace_warning or []:
            lines.append(line)

    if alert is not None:
        lines.append("")  # separator
        if alert.reason == "absolute_hourly_limit":
            lines.append(
                f"⚠ Cost rate alert: ${alert.rate_now_usd_per_hour:.2f}/hr "
                f"exceeds configured ceiling of "
                f"${alert.threshold_usd_per_hour:.2f}/hr. "
                f"Consider scaling back — defer expensive backlog items, "
                f"prefer cheaper subagents, end heartbeats silently if "
                f"nothing's urgent."
            )
        else:  # spike_ratio
            base = alert.baseline_usd_per_hour or 0.0
            ratio = (
                alert.threshold_usd_per_hour / base if base > 0 else 0.0
            )
            lines.append(
                f"⚠ Cost rate alert: ${alert.rate_now_usd_per_hour:.2f}/hr "
                f"exceeds {ratio:.1f}× baseline (${base:.2f}/hr × {ratio:.1f}). "
                f"Spend rate is unusual — check what's running, scale back "
                f"if a loop or fan-out is responsible."
            )

    return "\n".join(lines) if lines else None


def _fmt_int(n: int) -> str:
    """Compact human-readable integer: 1_234_567 → 1.2M, 12_345 → 12k."""
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _default_label(hours: float) -> str:
    if hours <= 24:
        return f"Last {int(hours)}h"
    days = hours / 24
    if days == int(days):
        return f"Last {int(days)}d"
    return f"Last {days:.1f}d"


def _model_from_events(rec: dict) -> str | None:
    """Extract the assistant model id from a turn record's events list,
    if present. Pre-this-commit TurnRecords didn't capture model
    top-level; the model name is in the per-message events."""
    events = rec.get("events")
    if not isinstance(events, list):
        return None
    for e in events:
        if isinstance(e, dict):
            model = e.get("model")
            if isinstance(model, str) and model:
                return model
    return None
