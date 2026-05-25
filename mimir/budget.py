"""S3-S4 homeostat (FUTURE_WORK §12.4).

The arbiter decides whether scheduled-tick (S4) work fires. Two
layers can suppress; a third is informational only.

**Suppressing layers (in priority order):**

1. **Plan-window utilization** (hardest wall). From ``RateLimitStore``
   — the SDK's ``RateLimitInfo`` 5h / 7d / 7d_opus / 7d_sonnet
   windows. At ≥ ``plan_window_suppress_threshold`` (default 0.80)
   the next heartbeat is suppressed; Anthropic literally stops
   responding when the window saturates so spending S4 budget there
   is wasted.
2. **Cost-rate alert** (dollars). From
   ``usage_stats.evaluate_cost_rate``. When tripped, suppress S4.

**Informational layer (rendered in self-state, no decision):**

3. **S3/S4 partition + tokens.** Per-day tool-call counts split by
   trigger (S3 = user_message, S4 = scheduled_tick) plus 24h/7d
   token totals. The agent reads these via the ``## Self-state``
   prompt section but the arbiter doesn't act on them — earlier
   designs that had this layer suppress on "S3 dominance" risked
   starving heartbeats on busy days, exactly when reflection /
   introspection are most valuable. Code review #7 (deferred-then-
   shipped) removed the partition's decision authority.

Runtime enforcement: ``should_fire_heartbeat()`` returns ``(fire,
reason)``. Prompt block: ``render_self_state_block()`` always
includes the partition + tokens for agent awareness."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ._jsonl_tail import tail_jsonl_records  # noqa: F401 — re-export
from .jsonl_snapshot import JsonlSnapshot, iter_snapshot_or_tail
from .billing import (
    BillingMode,
    QuotaProvider,
    QuotaSuppressionResult,
    evaluate_quota,
)
from .feedback import pending_forget_candidates_count
from .rate_limits import RateLimitSnapshot, RateLimitStore
from .usage_stats import (
    CostRateAlert,
    UsageReport,
    aggregate as aggregate_usage,
    evaluate_cost_rate,
)

log = logging.getLogger(__name__)


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
    tune via the per-instance fields.

    ``events_snapshot`` / ``turns_snapshot`` (CR#10): when provided by
    the constructing Agent, the homeostat reads through the cached
    snapshots instead of re-streaming events.jsonl + turns.jsonl on
    each ``snapshot()`` call. The arbiter is invoked once per turn (via
    ``_assemble_self_state_block`` from ``run_turn``) AND additionally
    by the scheduler before firing a tick — both call sites benefit
    from the per-Agent cache."""

    home: Path
    rate_limit_store: RateLimitStore
    turns_log: Path
    billing_mode: BillingMode = BillingMode.PAY_AS_YOU_GO
    quota_providers: list[QuotaProvider] = field(default_factory=list)
    plan_window_suppress_threshold: float = 0.80
    cost_hourly_limit_usd: float | None = None
    cost_spike_ratio: float | None = None
    cost_spike_floor_usd: float | None = 5.0
    fallback_model: str | None = None
    events_snapshot: "JsonlSnapshot | None" = None
    turns_snapshot: "JsonlSnapshot | None" = None

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
                snapshot=self.turns_snapshot,
            )
        except Exception:  # noqa: BLE001
            log.exception("budget snapshot: usage aggregate failed")
            report = UsageReport()

        alert = evaluate_cost_rate(
            report,
            hourly_limit_usd=self.cost_hourly_limit_usd,
            spike_ratio=self.cost_spike_ratio,
            spike_floor_usd_per_hour=self.cost_spike_floor_usd,
        )
        rate_now = 0.0
        baseline = None
        for w in report.windows:
            if w.label == "Last 1h":
                rate_now = w.total_cost_usd
                break
        for w in report.windows:
            if w.label == "Last 7d" and w.total_cost_usd > 0:
                # CR2-#8: clamp the divisor to the file's actual coverage
                # so a fresh install / post-trim state doesn't artificially
                # deflate the baseline 5-84× and trigger spike-shaped
                # advisory readings on normal usage. Mirrors the same
                # clamp in ``evaluate_cost_rate``.
                divisor = 24.0 * 7
                if report.oldest_record_ts is not None:
                    actual_hours = (
                        now - report.oldest_record_ts
                    ).total_seconds() / 3600.0
                    if actual_hours > 0 and actual_hours < divisor:
                        divisor = actual_hours
                if divisor >= 1.0:
                    baseline = w.total_cost_usd / divisor
                break

        # Tool-call partition + token totals from turns.jsonl.
        s3_calls, s4_calls, tokens_24h, tokens_7d = _partition_turns(
            self.turns_log, now=now,
            snapshot=self.turns_snapshot,
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
        self,
        *,
        now: datetime | None = None,
        event_loop: "asyncio.AbstractEventLoop | None" = None,
    ) -> tuple[bool, str]:
        """Return ``(fire, reason)``. ``fire=True`` lets the scheduled
        tick run; ``fire=False`` suppresses it. Reason is the
        constraint that produced the decision (useful for the
        ``scheduled_tick_suppressed`` event and operator triage).

        Branches on :class:`BillingMode` (chainlink #13):

        - ``quota`` — plan-window utilization is the binding
          constraint (zero marginal cost up to the cap). Suppress on
          on-pace projection or raw saturation across configured
          :class:`QuotaProvider` instances. Cost-rate spikes are
          demoted to advisory in this mode (logged elsewhere; not a
          suppression input).
        - ``pay-as-you-go`` — every token costs real money, so the
          cost-rate spike check is the binding constraint. Plan-
          window data, when present, can still suppress as a
          sanity wall (matches pre-chainlink-#13 behavior).

        The S3/S4 partition layer is informational (rendered into the
        prompt block) but doesn't gate firing — busy days shouldn't
        starve weekly maintenance work.

        Mid-turn quota exhaustion (SPEC §4.9 / §16 item 18): when a
        prior turn hit a 429 and recorded a pause via QuotaPauseTracker,
        ``is_paused()`` returns True until the recorded reset time.
        While paused, scheduled ticks suppress regardless of
        utilization — the upstream provider has already told us
        we're cut off. The lazy-expiry inside ``is_paused`` clears
        the pause when ``now`` crosses the reset timestamp; we
        emit ``quota_recovered`` on that transition (positive
        algedonic signal so the agent sees "we're back online").
        """
        # Quota-pause check fires BEFORE the utilization-based
        # branches: an upstream 429 is a hard fact, not a heuristic.
        # Imported locally to keep budget.py's import surface tight
        # (quota_pause is a small module the arbiter only needs at
        # decision time).
        from .quota_pause import QuotaPauseTracker
        pause_path = self.home / ".mimir" / "quota_pause.json"
        if pause_path.is_file():
            tracker = QuotaPauseTracker(pause_path)
            status = tracker.is_paused(now=now)
            if status.paused:
                ts = status.reset_at.isoformat() if status.reset_at else "?"
                return False, f"quota_exhausted_pause:resets_at={ts}"
            if status.reset_at is not None:
                # Lazy-expiry transition: the pause was active a moment
                # ago and just cleared. Fire-and-forget the recovery
                # event — sync arbiter can't await.
                #
                # Two paths:
                # (a) Called from asyncio.to_thread (the normal scheduler
                #     path) — there IS a running event loop in the main
                #     thread, but get_running_loop() raises RuntimeError
                #     here because this worker thread has none. Use
                #     run_coroutine_threadsafe to bridge across.
                # (b) Called directly from async context or sync test —
                #     get_running_loop() succeeds; schedule via create_task.
                import asyncio
                from .event_logger import log_event
                _coro = log_event(
                    "quota_recovered",
                    reset_at=status.reset_at.isoformat(),
                    previous_reason=status.reason,
                )
                if event_loop is not None:
                    # (a) thread-safe bridge to the caller's event loop
                    asyncio.run_coroutine_threadsafe(_coro, event_loop)
                else:
                    try:
                        _loop = asyncio.get_running_loop()
                        _loop.create_task(_coro)
                    except RuntimeError:
                        # (b) sync test path — no loop at all; close
                        # the coroutine to avoid ResourceWarning.
                        _coro.close()

        snap = self.snapshot(now=now)

        if self.billing_mode is BillingMode.QUOTA:
            result = evaluate_quota(
                self.quota_providers,
                raw_threshold=self.plan_window_suppress_threshold,
            )
            if result.suppress:
                return False, result.reason
            # Cost rate is advisory under quota mode — never gates.
            return True, "ok"

        # Pay-as-you-go (and any unrecognized future mode): existing
        # layered behavior — plan-window first (hard wall when present),
        # then cost-rate spike.
        if (
            snap.plan_window_max_utilization is not None
            and snap.plan_window_max_utilization >= self.plan_window_suppress_threshold
        ):
            reason = (
                f"plan_window_saturated:{snap.plan_window_worst_key}"
                f"@{snap.plan_window_max_utilization:.2f}"
            )
            return False, reason

        if snap.cost_rate_alert is not None:
            return False, f"cost_rate_alert:{snap.cost_rate_alert.reason}"

        return True, "ok"

    def render_self_state_block(
        self, *, now: datetime | None = None,
    ) -> str | None:
        """Format the snapshot as a ``## Self-state`` body. Returns
        None when nothing is worth surfacing (no plan data, no usage,
        no tool-call history) — don't print an empty header."""
        snap = self.snapshot(now=now)

        lines: list[str] = []

        # Pending forget candidates from the most recent decay cycle.
        # Sticky: stays visible across turns until a saga_forget_ok
        # event lands (newer than the latest saga_decay_ok). Top of
        # the block because it's actionable agent-maintenance, not
        # ambient telemetry.
        try:
            pending = pending_forget_candidates_count(
                self.home / "logs" / "events.jsonl",
                snapshot=self.events_snapshot,
            )
        except Exception:  # noqa: BLE001
            log.exception("pending_forget_candidates_count failed; skipping")
            pending = None
        if pending is not None and pending > 0:
            lines.append(
                f"- {pending} forget candidates pending review "
                f"(saga_forget tool, dry_run=true to preview)"
            )

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
            # Informational only — the partition layer doesn't suppress
            # ticks (review #7). Surfaced so the agent can see whether
            # its day skews toward user-driven or autonomous work.
            lines.append(
                f"- S3/S4 tool-call share (24h): {s3_pct:.0f}% / {s4_pct:.0f}%"
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
    snapshot: "JsonlSnapshot | None" = None,
) -> tuple[int, int, int, int]:
    """Walk turns.jsonl tail-first, return (s3_tool_calls_24h,
    s4_tool_calls_24h, tokens_24h, tokens_7d). S3 = user_message;
    S4 = scheduled_tick. Other triggers (subagent_completion, etc.)
    don't count toward either.

    JSONL is append-chronological — oldest record on disk first,
    newest last. ``tail_jsonl_records`` reads chunks from the end so
    we see newest-first; once we hit a record older than the 7d
    cutoff, everything still in the file is also older and we stop.
    Avoids the O(file_size) memory spike of the prior
    ``path.read_text()``-based walk. CR#5.

    ``snapshot`` (CR#10) — when provided, iterate the cached snapshot
    instead of streaming from disk. Falls back to direct tail when None.
    """
    s3_calls = 0
    s4_calls = 0
    tokens_24h = 0
    tokens_7d = 0
    if not path.is_file():
        return 0, 0, 0, 0

    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    for rec in iter_snapshot_or_tail(snapshot, path):
        ts_raw = rec.get("ts")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        if ts < cutoff_7d:
            # Tail-first iteration over a chronological file: every
            # remaining record is also older than the 7d cutoff. Stop.
            break

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
