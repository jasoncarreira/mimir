"""S3-S4 homeostat (FUTURE_WORK §12.4).

The arbiter grades resource pressure into a :class:`Severity` ladder
(CLEAR / ELEVATED / TIGHT / BLOCKED, worst signal wins) and gates
autonomous work — scheduled ticks AND pollers — by each unit's
declared priority (``low`` / ``normal`` / ``high``): ``low`` sheds at
ELEVATED, ``normal`` at TIGHT, everything at BLOCKED. See
:meth:`HomeostaticArbiter.should_fire` for the fire matrix.

**Severity signals (in layering order):**

1. **Recorded 429 pause** (hard fact, checked first) → BLOCKED. The
   provider is actively refusing; headroom math is moot.
2. ``quota`` (subscription) mode — the **burst multiple** M per plan
   window: how many times the established pace the agent would have
   to sustain for the rest of the window to hit 100%, with an
   early-window confidence ramp. Raw utilization ≥
   ``plan_window_suppress_threshold`` (default 0.80) stays a TIGHT
   wall. See ``mimir.billing.evaluate_quota_severity``.
3. ``pay-as-you-go`` (API) mode — cost-rate alert (hourly limit or
   spike vs 7d baseline) → TIGHT; within 80% of either trip →
   ELEVATED; plan-window raw saturation, when data exists, stays a
   TIGHT sanity wall.

**Informational layer (rendered in self-state, no decision):**

4. **S3/S4 partition + tokens.** Per-day tool-call counts split by
   trigger (S3 = user_message, S4 = scheduled_tick) plus 24h/7d
   token totals. The agent reads these via the ``## Self-state``
   prompt section but the arbiter doesn't act on them — earlier
   designs that had this layer suppress on "S3 dominance" risked
   starving heartbeats on busy days, exactly when reflection /
   introspection are most valuable. Code review #7 (deferred-then-
   shipped) removed the partition's decision authority.

Runtime enforcement: ``should_fire(priority=...)`` returns a
:class:`FireDecision`. Prompt block: ``render_self_state_block()``
always includes the partition + tokens for agent awareness, plus the
current severity when above CLEAR."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ._jsonl_tail import tail_jsonl_records  # noqa: F401 — re-export
from .jsonl_snapshot import JsonlSnapshot, iter_snapshot_or_tail
from .billing import (
    BillingMode,
    QuotaProvider,
    Severity,
    evaluate_quota_severity,
    priority_tolerates,
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

# Strong references to fire-and-forget background tasks (chainlink #118).
# Module-level set holds tasks spawned by HomeostaticArbiter.should_fire
# until completion.  The done-callback discards each entry so the set stays
# bounded to in-flight tasks only.  See cpython docs "Coroutines and Tasks /
# Important" callout.
_background_tasks: set[asyncio.Task[Any]] = set()

# Pay-as-you-go ELEVATED early warning: within this fraction of a
# configured cost-rate trip (hourly limit or spike threshold).
_NEAR_TRIP_FRACTION = 0.8


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


@dataclass(frozen=True)
class SeverityAssessment:
    """Output of :meth:`HomeostaticArbiter.assess` — the graded
    pressure level plus the constraint that produced it.
    ``burst_multiple`` / ``gamma`` are populated only when the deciding
    signal was a quota-window pace band (for event payloads)."""

    severity: Severity
    reason: str
    burst_multiple: float | None = None
    gamma: float | None = None


@dataclass(frozen=True)
class FireDecision:
    """Output of :meth:`HomeostaticArbiter.should_fire`. ``fire``
    is the gate; the rest is structured context for the
    ``scheduled_tick_suppressed`` / ``poller_fire_suppressed``
    events and operator triage."""

    fire: bool
    reason: str
    severity: Severity
    priority: str
    burst_multiple: float | None = None


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

    def _check_quota_pause(
        self,
        *,
        now: datetime | None = None,
        event_loop: "asyncio.AbstractEventLoop | None" = None,
    ) -> str | None:
        """Return the BLOCKED reason when a recorded 429 pause is
        active, else None. Handles the lazy-expiry transition: the
        first consult past the recorded reset emits ``quota_recovered``
        (positive algedonic signal so the agent sees "we're back
        online").

        Imported locally to keep budget.py's import surface tight
        (quota_pause is a small module the arbiter only needs at
        decision time)."""
        from .quota_pause import QuotaPauseTracker
        pause_path = self.home / ".mimir" / "quota_pause.json"
        if not pause_path.is_file():
            return None
        tracker = QuotaPauseTracker(pause_path)
        status = tracker.is_paused(now=now)
        if status.paused:
            ts = status.reset_at.isoformat() if status.reset_at else "?"
            return f"quota_exhausted_pause:resets_at={ts}"
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
                    _task = _loop.create_task(_coro)
                    _background_tasks.add(_task)
                    _task.add_done_callback(_background_tasks.discard)
                except RuntimeError:
                    # (b) sync test path — no loop at all; close
                    # the coroutine to avoid ResourceWarning.
                    _coro.close()
        return None

    def assess(
        self,
        *,
        now: datetime | None = None,
        event_loop: "asyncio.AbstractEventLoop | None" = None,
    ) -> "SeverityAssessment":
        """Grade current resource pressure into a :class:`Severity`
        (worst signal wins). This is the single source the priority-
        banded gate reads; ``reason`` names the deciding constraint.

        Layering:

        1. **Recorded 429 pause** (hard fact, checked first) → BLOCKED.
        2. ``quota`` mode — burst-multiple bands + raw wall across
           configured :class:`QuotaProvider` windows (see
           :func:`mimir.billing.evaluate_quota_severity`). Cost-rate
           spikes are advisory in this mode (zero marginal cost up to
           the cap).
        3. ``pay-as-you-go`` mode — every token costs real money:
           cost-rate alert → TIGHT; within 80% of a configured hourly
           limit → ELEVATED; plan-window raw saturation, when data is
           present, stays a TIGHT sanity wall.

        The S3/S4 partition layer is informational (rendered into the
        prompt block) but doesn't gate firing — busy days shouldn't
        starve weekly maintenance work."""
        pause_reason = self._check_quota_pause(now=now, event_loop=event_loop)
        if pause_reason is not None:
            return SeverityAssessment(
                severity=Severity.BLOCKED, reason=pause_reason,
            )

        if self.billing_mode is BillingMode.QUOTA:
            result = evaluate_quota_severity(
                self.quota_providers,
                raw_threshold=self.plan_window_suppress_threshold,
            )
            return SeverityAssessment(
                severity=result.severity,
                reason=result.reason,
                burst_multiple=result.burst_multiple,
                gamma=result.gamma,
            )

        # Pay-as-you-go (and any unrecognized future mode).
        snap = self.snapshot(now=now)
        if (
            snap.plan_window_max_utilization is not None
            and snap.plan_window_max_utilization >= self.plan_window_suppress_threshold
        ):
            reason = (
                f"plan_window_saturated:{snap.plan_window_worst_key}"
                f"@{snap.plan_window_max_utilization:.2f}"
            )
            return SeverityAssessment(severity=Severity.TIGHT, reason=reason)
        if snap.cost_rate_alert is not None:
            return SeverityAssessment(
                severity=Severity.TIGHT,
                reason=f"cost_rate_alert:{snap.cost_rate_alert.reason}",
            )
        near = self._payg_near_trip_reason(snap)
        if near is not None:
            return SeverityAssessment(severity=Severity.ELEVATED, reason=near)
        return SeverityAssessment(severity=Severity.CLEAR, reason="ok")

    def _payg_near_trip_reason(self, snap: BudgetSnapshot) -> str | None:
        """ELEVATED early warning for pay-as-you-go: within
        ``_NEAR_TRIP_FRACTION`` of whichever cost-rate trip the
        operator configured. Mirrors both arms of
        ``evaluate_cost_rate`` so installs with only a spike detector
        (no hourly dollar limit) still get a graduated band instead of
        jumping CLEAR → TIGHT. None when no near-trip applies."""
        rate = snap.cost_rate_now_usd_per_hour
        if rate <= 0:
            return None
        if (
            snap.cost_hourly_limit_usd
            and rate >= _NEAR_TRIP_FRACTION * snap.cost_hourly_limit_usd
        ):
            return (
                f"cost_rate_near_limit:${rate:.2f}/hr"
                f"@limit=${snap.cost_hourly_limit_usd:.2f}"
            )
        if (
            self.cost_spike_ratio
            and snap.cost_rate_baseline_usd_per_hour
            and snap.cost_rate_baseline_usd_per_hour > 0
        ):
            spike_trip = (
                self.cost_spike_ratio * snap.cost_rate_baseline_usd_per_hour
            )
            # Respect the same absolute floor the alert applies — a
            # near-spike reading under the floor would never trip, so
            # it shouldn't pre-warn either.
            floor = self.cost_spike_floor_usd or 0.0
            if rate >= max(_NEAR_TRIP_FRACTION * spike_trip, floor):
                return (
                    f"cost_rate_near_spike:${rate:.2f}/hr"
                    f"@trip=${spike_trip:.2f}"
                )
        return None

    def should_fire(
        self,
        *,
        priority: str = "normal",
        now: datetime | None = None,
        event_loop: "asyncio.AbstractEventLoop | None" = None,
    ) -> "FireDecision":
        """Priority-banded fire decision for autonomous work
        (scheduled ticks AND pollers). ``priority`` is the work's
        declared level (``low`` / ``normal`` / ``high``); it fires iff
        it tolerates the current severity:

        ========== ====== ======== ======
        severity     low   normal   high
        ========== ====== ======== ======
        CLEAR        ✓      ✓        ✓
        ELEVATED     —      ✓        ✓
        TIGHT        —      —        ✓
        BLOCKED      —      —        —
        ========== ====== ======== ======

        ``reason`` carries the deciding constraint either way (callers
        log it on suppression; "ok" when CLEAR)."""
        assessment = self.assess(now=now, event_loop=event_loop)
        fire = priority_tolerates(priority, assessment.severity)
        return FireDecision(
            fire=fire,
            reason=assessment.reason,
            severity=assessment.severity,
            priority=priority,
            burst_multiple=assessment.burst_multiple,
        )

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

        # Autonomy throttle: surface the severity the scheduler is
        # gating on so the agent can see WHY its pollers/heartbeats
        # went quiet (and modulate its own optional work to match).
        try:
            assessment = self.assess(now=now)
        except Exception:  # noqa: BLE001
            log.exception("severity assess failed; skipping throttle line")
            assessment = None
        if assessment is not None and assessment.severity > 0:
            shedding = {
                1: "low-priority scheduled work shedding",
                2: "low+normal scheduled work shedding",
                3: "all scheduled work paused",
            }.get(int(assessment.severity), "")
            m_part = (
                f", M={assessment.burst_multiple:.2f}"
                if assessment.burst_multiple is not None else ""
            )
            lines.append(
                f"- autonomy throttle: {assessment.severity.name} "
                f"({assessment.reason}{m_part}) — {shedding}"
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
