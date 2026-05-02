"""S3-S4 homeostat (FUTURE_WORK §12.4).

The arbiter that decides whether scheduled-tick (S4) work fires. Reads
four layered constraints in priority order and returns suppress / fire
/ boost. The same data is rendered into a ``## Self-state`` prompt
section so the agent has the same signal as the arbiter.

Layered constraints (strictest first):

1. **Plan-window utilization** (hardest). From ``RateLimitStore`` —
   the SDK's ``RateLimitInfo`` 5h / 7d / 7d_opus / 7d_sonnet windows.
   ≥ ``plan_window_suppress_threshold`` (default 0.80) suppresses S4.
2. **Cost-rate alert** (dollars). From ``usage_stats.evaluate_cost_rate``.
   When tripped, suppress S4.
3. **Tool-call budget** (soft). Per-day count split S3 / S4 from
   turns.jsonl ``trigger`` field. When S3 share > 80%, defer S4
   firings (the day is busy with user work). When < 50%, boost.
4. **Token-count budget** (rolling). Surfaced in the self-state
   block but not currently a suppression input — the dollar
   constraint already captures this in practice.

The order matters: constraint #1 is a hard wall (Anthropic just stops
responding when the window saturates); cost-rate is dollars; tool-call
is a soft heuristic. Saturating #1 overrides all others.

Runtime enforcement lands here (heartbeat-fire decision); the prompt
self-state block lands in the system/turn prompt assembly so the
agent sees the same constraints the arbiter does."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from .rate_limits import RateLimitSnapshot, RateLimitStore
from .usage_stats import (
    CostRateAlert,
    UsageReport,
    aggregate as aggregate_usage,
    evaluate_cost_rate,
)

log = logging.getLogger(__name__)


class HeartbeatDecision(str, Enum):
    """Arbiter outcome for an upcoming S4 tick."""

    SUPPRESS = "suppress"
    FIRE = "fire"
    BOOST = "boost"


@dataclass
class BudgetSnapshot:
    """Per-call view of all four constraint layers. The arbiter
    evaluates this against thresholds; the renderer formats it for the
    prompt. Decoupled so tests can construct snapshots directly."""

    # Layer 1 — plan window
    plan_window_max_utilization: float | None
    plan_window_worst_key: str | None
    plan_window_resets_at: int | None

    # Layer 2 — cost rate
    cost_rate_alert: CostRateAlert | None
    cost_rate_now_usd_per_hour: float
    cost_rate_baseline_usd_per_hour: float | None
    cost_hourly_limit_usd: float | None

    # Layer 3 — tool-call partition
    s3_tool_calls_today: int
    s4_tool_calls_today: int

    # Layer 4 — tokens
    tokens_24h: int
    tokens_7d: int

    @property
    def s3_share_today(self) -> float:
        denom = self.s3_tool_calls_today + self.s4_tool_calls_today
        if denom == 0:
            return 0.0
        return self.s3_tool_calls_today / denom


# VSM: S3-S4 homeostat — arbitrates exploration/exploitation budget
#      across user-message-driven (S3) and scheduled-tick-driven
#      (S4) work. Layered-constraint hierarchy: plan-window >
#      cost-rate > tool-call > tokens.
# loop_id: 12.4
@dataclass
class HomeostaticArbiter:
    """Reads turns.jsonl + rate-limit store + usage report, returns a
    heartbeat-fire decision. Stateless across calls — the inputs are
    re-read each time so live signals win.

    The thresholds default to the §12.4 starting values; operators can
    tune via the per-instance fields."""

    home: Path
    rate_limit_store: RateLimitStore
    turns_log: Path
    plan_window_suppress_threshold: float = 0.80
    s3_share_max: float = 0.80          # > this → S4 deferred
    s3_share_min: float = 0.50          # < this → S4 boosted
    cost_hourly_limit_usd: float | None = None
    cost_spike_ratio: float | None = None
    fallback_model: str | None = None

    def snapshot(self, *, now: datetime | None = None) -> BudgetSnapshot:
        now = now or datetime.now(tz=timezone.utc)

        # Plan window: pick the worst-utilized live entry.
        worst_key: str | None = None
        worst_util: float | None = None
        worst_resets_at: int | None = None
        for key, snap in self.rate_limit_store.current().items():
            if snap.utilization is None:
                continue
            if worst_util is None or snap.utilization > worst_util:
                worst_util = snap.utilization
                worst_key = key
                worst_resets_at = snap.resets_at

        # Cost rate via the existing usage_stats pipeline. We surface
        # the alert AND the raw 1h rate so the prompt block can show
        # both "are we tripped" and "where are we now."
        report: UsageReport
        try:
            report = aggregate_usage(
                self.turns_log,
                fallback_model=self.fallback_model,
            )
        except Exception:  # noqa: BLE001
            log.exception("budget snapshot: usage aggregate failed")
            report = UsageReport()

        alert = evaluate_cost_rate(
            report,
            hourly_limit_usd=self.cost_hourly_limit_usd,
            spike_ratio=self.cost_spike_ratio,
        )
        rate_now = 0.0
        baseline = None
        for w in report.windows:
            if w.label == "Last 1h":
                rate_now = w.total_cost_usd
                break
        for w in report.windows:
            if w.label == "Last 7d" and w.total_cost_usd > 0:
                baseline = w.total_cost_usd / (24 * 7)
                break

        # Tool-call partition + token totals from turns.jsonl.
        s3_calls, s4_calls, tokens_24h, tokens_7d = _partition_turns(
            self.turns_log, now=now,
        )

        return BudgetSnapshot(
            plan_window_max_utilization=worst_util,
            plan_window_worst_key=worst_key,
            plan_window_resets_at=worst_resets_at,
            cost_rate_alert=alert,
            cost_rate_now_usd_per_hour=rate_now,
            cost_rate_baseline_usd_per_hour=baseline,
            cost_hourly_limit_usd=self.cost_hourly_limit_usd,
            s3_tool_calls_today=s3_calls,
            s4_tool_calls_today=s4_calls,
            tokens_24h=tokens_24h,
            tokens_7d=tokens_7d,
        )

    def should_fire_heartbeat(
        self, *, now: datetime | None = None,
    ) -> tuple[HeartbeatDecision, str]:
        """Return (decision, reason). Reason is the constraint that
        produced the decision — useful for ``scheduled_tick_dropped``
        events and operator triage."""
        snap = self.snapshot(now=now)

        # 1. Plan window — hard wall.
        if (
            snap.plan_window_max_utilization is not None
            and snap.plan_window_max_utilization >= self.plan_window_suppress_threshold
        ):
            reason = (
                f"plan_window_saturated:{snap.plan_window_worst_key}"
                f"@{snap.plan_window_max_utilization:.2f}"
            )
            return HeartbeatDecision.SUPPRESS, reason

        # 2. Cost rate — dollars.
        if snap.cost_rate_alert is not None:
            return (
                HeartbeatDecision.SUPPRESS,
                f"cost_rate_alert:{snap.cost_rate_alert.reason}",
            )

        # 3. Tool-call partition — S3 dominance defers S4; S3 idle boosts.
        share = snap.s3_share_today
        total = snap.s3_tool_calls_today + snap.s4_tool_calls_today
        if total >= 10:  # avoid noise from tiny days
            if share > self.s3_share_max:
                return (
                    HeartbeatDecision.SUPPRESS,
                    f"s3_dominant:{share:.2f}>{self.s3_share_max:.2f}",
                )
            if share < self.s3_share_min:
                return (
                    HeartbeatDecision.BOOST,
                    f"s3_idle:{share:.2f}<{self.s3_share_min:.2f}",
                )

        return HeartbeatDecision.FIRE, "ok"

    def render_self_state_block(
        self, *, now: datetime | None = None,
    ) -> str | None:
        """Format the snapshot as a ``## Self-state`` body. Returns
        None when nothing is worth surfacing (no plan data, no usage,
        no tool-call history) — don't print an empty header."""
        snap = self.snapshot(now=now)

        lines: list[str] = []

        if snap.plan_window_max_utilization is not None:
            pct = snap.plan_window_max_utilization * 100
            tail = ""
            if snap.plan_window_resets_at:
                tail = f" (resets {_humanize_resets(snap.plan_window_resets_at, now=now)})"
            lines.append(
                f"- {snap.plan_window_worst_key} window: {pct:.0f}% used{tail}"
            )

        if snap.cost_hourly_limit_usd:
            lines.append(
                f"- cost rate: ${snap.cost_rate_now_usd_per_hour:.2f}/hr "
                f"(limit ${snap.cost_hourly_limit_usd:.2f}/hr)"
            )
        elif snap.cost_rate_now_usd_per_hour > 0:
            lines.append(
                f"- cost rate: ${snap.cost_rate_now_usd_per_hour:.2f}/hr"
            )

        total = snap.s3_tool_calls_today + snap.s4_tool_calls_today
        if total > 0:
            s3_pct = snap.s3_share_today * 100
            s4_pct = (1.0 - snap.s3_share_today) * 100
            lines.append(
                f"- S3/S4 budget today: {s3_pct:.0f}% / {s4_pct:.0f}% "
                f"(target {self.s3_share_max * 100:.0f}/{(1 - self.s3_share_max) * 100:.0f})"
            )

        if snap.tokens_24h > 0 or snap.tokens_7d > 0:
            lines.append(
                f"- tokens: 24h {_fmt_tokens(snap.tokens_24h)}, "
                f"7d {_fmt_tokens(snap.tokens_7d)}"
            )

        if not lines:
            return None
        return "\n".join(lines)


def _partition_turns(
    path: Path, *, now: datetime,
) -> tuple[int, int, int, int]:
    """Walk turns.jsonl, return (s3_tool_calls_24h, s4_tool_calls_24h,
    tokens_24h, tokens_7d). S3 = user_message; S4 = scheduled_tick.
    Other triggers (subagent_completion, etc.) don't count toward either.
    """
    s3_calls = 0
    s4_calls = 0
    tokens_24h = 0
    tokens_7d = 0
    if not path.is_file():
        return 0, 0, 0, 0

    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0, 0, 0, 0

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_raw = rec.get("ts")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < cutoff_7d:
            continue

        usage = rec.get("usage") or {}
        if isinstance(usage, dict):
            tk = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("output_tokens") or 0)
            )
            tokens_7d += tk
            if ts >= cutoff_24h:
                tokens_24h += tk

        if ts < cutoff_24h:
            continue

        trigger = rec.get("trigger")
        events = rec.get("events") or []
        if not isinstance(events, list):
            continue
        tool_calls = sum(
            1 for ev in events
            if isinstance(ev, dict) and ev.get("type") == "tool_call"
        )
        if trigger == "user_message":
            s3_calls += tool_calls
        elif trigger == "scheduled_tick":
            s4_calls += tool_calls

    return s3_calls, s4_calls, tokens_24h, tokens_7d


def _humanize_resets(resets_at: int, *, now: datetime | None = None) -> str:
    now_ts = (now or datetime.now(tz=timezone.utc)).timestamp()
    delta = resets_at - now_ts
    if delta <= 0:
        return "now"
    days, rem = divmod(int(delta), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"in {days}d {hours}h"
    if hours > 0:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)
