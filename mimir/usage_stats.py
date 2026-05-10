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

from ._jsonl_tail import tail_jsonl_records  # noqa: F401 — kept for back-compat re-exports
from .jsonl_snapshot import JsonlSnapshot, iter_snapshot_or_tail

log = logging.getLogger(__name__)


# Model-name → max input context window (tokens). Used to render
# "current turn / cap" percentage in the usage block. Best-effort:
# unknown models fall back to ``_DEFAULT_CONTEXT_WINDOW``.
#
# Sources: Anthropic API docs as of the cutoff. The 1M variant of
# Opus 4.7 reports its own model id (``claude-opus-4-7[1m]``) in the
# Claude Code surface; bare ``claude-opus-4-7`` is the 200k variant
# unless the request opts into the 1M context window via the
# ``context-1m-2025-08-07`` beta header (see ``CONTEXT_1M_BETA``
# below). Pass ``betas=`` to ``context_window_for`` to reflect that.
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

# Anthropic beta header that lifts Claude 4.x Opus / Sonnet from the
# 200k context cap to a 1M cap. Same shape as the OAuth beta header in
# ``mimir/oauth_usage_poller.py`` — a single string passed via the
# claude_agent_sdk's ``betas=`` option.
CONTEXT_1M_BETA = "context-1m-2025-08-07"

# Model-id prefixes for which the 1M-context beta lifts the cap. Haiku
# is excluded — the beta is documented for Sonnet 4 / Opus 4 only.
_CONTEXT_1M_MODEL_PREFIXES = ("claude-opus-4-", "claude-sonnet-4-")


@dataclass
class UsageWindow:
    """Aggregated usage over a contiguous time window. Field semantics
    match the SDK's ``usage`` dict (Anthropic's ``Message.usage`` shape):
    counts are tokens; cost is dollars.

    ``hours`` is the window size used by ``_find_window`` for lookup
    (CR2 ops & observability fix). Pre-fix, ``_find_window`` matched
    by label string, regenerating the label via ``_default_label`` —
    so a caller passing custom ``window_labels`` to ``aggregate()``
    silently broke the spike-ratio lookup. Now lookup is by hours
    directly.
    """

    label: str
    hours: float = 0.0
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
    # CR2-#8: timestamp of the oldest record we walked during this
    # aggregate. Used by ``evaluate_cost_rate`` to clamp the baseline
    # divisor on partial-week installs (fresh deploy, post-trim, etc.).
    # ``None`` means we touched no records (empty / missing file).
    oldest_record_ts: datetime | None = None


@dataclass(frozen=True)
class CostRateAlert:
    """Triggered when current hourly spend exceeds one of two thresholds:

    - **absolute_hourly_limit**: ``rate_now_usd_per_hour`` >
      ``MIMIR_COST_HOURLY_LIMIT_USD``. Operator-set ceiling; useful for
      "never go above $X/hour even if we usually do."
    - **spike_ratio**: ``rate_now_usd_per_hour`` >
      ``spike_ratio * baseline_usd_per_hour`` AND
      ``rate_now_usd_per_hour >= spike_floor_usd_per_hour``.
      Adapts to your usual spend; catches runaway loops (3× the
      rolling-week average is the default). The floor is the asymmetry
      fix: a quiet-history agent (low baseline) shouldn't false-positive
      on a normal working session that's expensive only in *ratio*
      terms. Below the floor, we're not in spend territory worth
      suppressing S4 over — the absolute ceiling remains the backstop.

    When both fire, the absolute one wins (deterministic; no double-
    counted reasons).
    """

    reason: str  # "absolute_hourly_limit" | "spike_ratio"
    rate_now_usd_per_hour: float
    threshold_usd_per_hour: float
    baseline_usd_per_hour: float | None  # populated only for spike_ratio


def context_window_for(
    model: str | None,
    *,
    betas: Iterable[str] | None = None,
) -> int:
    """Look up the model's max input context. Unknown / None → default.

    ``betas`` reflects Anthropic beta headers active on the request.
    When ``CONTEXT_1M_BETA`` is present and the model is a Claude 4.x
    Opus or Sonnet, the 1M window applies — the per-model dict's bare
    ``claude-opus-4-7`` entry would otherwise return the 200k default.
    """
    if betas and CONTEXT_1M_BETA in betas and model:
        if any(model.startswith(p) for p in _CONTEXT_1M_MODEL_PREFIXES):
            return 1_000_000
    if not model:
        return _DEFAULT_CONTEXT_WINDOW
    return _MODEL_CONTEXT_WINDOWS.get(model, _DEFAULT_CONTEXT_WINDOW)


def aggregate(
    turns_path: Path,
    *,
    window_hours: Iterable[float] = (1.0, 5.0, 24.0 * 7),
    window_labels: Iterable[str] | None = None,
    fallback_model: str | None = None,
    snapshot: "JsonlSnapshot | None" = None,
) -> UsageReport:
    """Walk turns.jsonl tail-first; bucket each turn's usage into the
    matching windows. Single pass — stops once the oldest window's
    cutoff is exceeded.

    ``fallback_model`` is what the last-turn snapshot reports when the
    record itself doesn't carry a model field (mimir's TurnRecord
    didn't capture it pre-this-commit; the operator's configured model
    is the right fallback).

    ``snapshot`` (CR#10) — when provided, iterate from the cached
    JsonlSnapshot instead of streaming the file. Falls back to direct
    tail when None for back-compat with module-level callers that don't
    construct an Agent.
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

    out_windows = [
        UsageWindow(label=label, hours=h)
        for label, h in zip(window_labels, windows)
    ]
    last_turn = LastTurnSnapshot()
    saw_first = False
    # CR2-#8: track the oldest record we actually accumulate into the
    # widest window. On a 30d-deep file this is the 7d-cutoff record;
    # on a 24h-old fresh install (no break fires) this is the file's
    # oldest record. ``evaluate_cost_rate`` uses this to clamp the
    # baseline divisor when the file's coverage is shorter than the
    # nominal baseline window.
    oldest_record_ts: datetime | None = None

    for rec in iter_snapshot_or_tail(snapshot, turns_path):
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

        # Track the oldest ts we actually processed (not the one that
        # triggered the break). Newest-first iteration → each
        # successive record is older than the last; ``ts`` after the
        # final loop iteration is the oldest accumulated record.
        oldest_record_ts = ts

    return UsageReport(
        last_turn=last_turn,
        windows=out_windows,
        oldest_record_ts=oldest_record_ts,
    )


def evaluate_cost_rate(
    report: UsageReport,
    *,
    hourly_limit_usd: float | None = None,
    spike_ratio: float | None = None,
    spike_floor_usd_per_hour: float | None = 5.0,
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
    ``spike_floor_usd_per_hour`` (default $5.0/hr) gates the spike
    check on rate_now: a 100× ratio over a $0.001/hr baseline is still
    only a $0.10/hr session — not worth suppressing S4 over. The floor
    is paired with an absolute ``hourly_limit_usd`` (typical bench
    setting: $120/hr) which serves as the real brake; the spike check
    is a second line of defense for "weird shape, not yet at ceiling,"
    so $5/hr is the neighborhood that catches genuine oddities while
    ignoring normal working sessions. Set to 0 or None to disable the
    floor and revert to baseline-only gating.

    **Pairing requirement (re-grade follow-up).** If ``spike_ratio`` is
    set without ``hourly_limit_usd``, sub-floor rates never alert by
    design — the floor exists precisely to ignore normal working
    sessions. Pair with an absolute ceiling for true protection.

    **Baseline divisor (CR2-#8).** Below ``baseline_window_hours``,
    the divisor used for the baseline rate clamps to the file's actual
    coverage (``min(baseline_window_hours, hours_since_oldest_record)``).
    Without the clamp, a fresh install (file spans 2h) or post-trim
    state (file spans 30h) would divide 7d's cost by 168 and produce
    a baseline 5-84× too low — making the spike check hyper-sensitive
    on day 1 and after every major trim. With < 1h of coverage, the
    spike check defers entirely (returns None) until enough data
    accumulates.
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
        # rate_now floor — see docstring. Asymmetry fix for low-baseline
        # agents whose normal working sessions register as "spikes."
        if spike_floor_usd_per_hour and spike_floor_usd_per_hour > 0:
            if rate_now < spike_floor_usd_per_hour:
                return None
        baseline_w = _find_window(report, baseline_window_hours)
        if baseline_w is not None and baseline_window_hours > 0:
            # CR2-#8: clamp the divisor to the file's actual coverage.
            # On a fresh install (file spans 2h) or post-trim state
            # (file spans 30h), dividing 7d's cost by 168 underestimates
            # the baseline 5–84×, making the spike check hyper-sensitive
            # right when the operator has the least data to reason
            # about. ``oldest_record_ts`` is the timestamp of the oldest
            # turn we actually walked — when present, the file covers at
            # most ``hours_since_oldest`` worth of activity. Use the
            # smaller of (nominal baseline window, actual coverage).
            now = datetime.now(tz=timezone.utc)
            divisor = baseline_window_hours
            if report.oldest_record_ts is not None:
                actual_hours = (
                    now - report.oldest_record_ts
                ).total_seconds() / 3600.0
                if actual_hours > 0 and actual_hours < divisor:
                    divisor = actual_hours
            # Below ~1h of coverage, the baseline signal is too noisy
            # to bother — defer the spike check until enough data
            # accumulates.
            if divisor < 1.0:
                return None
            baseline_rate = baseline_w.total_cost_usd / divisor
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
    """Lookup helper: pick the window with the matching ``hours``
    field. Returns None when nothing matches.

    CR2 (ops & observability) fix: was a label-string compare against
    a regenerated ``_default_label(hours)`` — caller-supplied custom
    ``window_labels`` to ``aggregate()`` silently broke the spike-
    ratio lookup. Lookup is now by ``hours`` directly so labels are
    purely cosmetic. Falls back to label-match for legacy callers
    that may construct ``UsageWindow(label=...)`` without setting
    ``hours``.
    """
    for w in report.windows:
        if w.hours == hours:
            return w
    # Legacy fallback: any UsageWindow constructed without ``hours``
    # has the dataclass default 0.0; match on label as a backstop so
    # external callers / older test fixtures still work.
    target = _default_label(hours)
    for w in report.windows:
        if w.hours == 0.0 and w.label == target:
            return w
    return None


def event_recently_emitted(
    events_path: Path, event_type: str, *, cooldown_minutes: int,
    snapshot: "JsonlSnapshot | None" = None,
) -> bool:
    """True if any event with ``type == event_type`` lies within the
    cooldown window. Used to gate spike-style alert emissions
    (cost_rate_alert, rate_limit_off_pace, etc.) so the firehose
    doesn't churn on a sustained condition — the algedonic block
    surfaces the most recent occurrence anyway.

    ``snapshot`` (CR#10) — when provided, iterate the cached snapshot
    instead of streaming events.jsonl from disk."""
    if cooldown_minutes <= 0:
        return False
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=cooldown_minutes)
    cutoff_iso = cutoff.isoformat()
    for ev in iter_snapshot_or_tail(snapshot, events_path):
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
    snapshot: "JsonlSnapshot | None" = None,
) -> bool:
    return event_recently_emitted(
        events_path, "cost_rate_alert", cooldown_minutes=cooldown_minutes,
        snapshot=snapshot,
    )


# VSM: S3 — self-state sensing; cost / cache / token rolls into
#          rolling windows the agent reads in-band each turn.
#          Threshold trips fire cost_rate_alert events that the
#          algedonic block (loop 2.1) picks up.
# loop_id: 2.4
def render_usage_block(
    report: UsageReport,
    *,
    fallback_model: str | None = None,
    budget_5h_usd: float | None = None,
    budget_weekly_usd: float | None = None,
    alert: CostRateAlert | None = None,
    plan_quota_lines: list[str] | None = None,
    off_pace_warning: list[str] | None = None,
    subagent_block: str | None = None,
    betas: Iterable[str] | None = None,
) -> str | None:
    """Format the usage report as a markdown body for the
    "## Resource usage" prompt section. Returns None when there's
    nothing to show (no last turn, no aggregated windows with data)."""
    has_windows = any(w.turns > 0 for w in report.windows)
    has_plan_quotas = bool(plan_quota_lines)
    has_off_pace = bool(off_pace_warning)
    has_subagents = bool(subagent_block)
    if (
        report.last_turn.ts is None
        and not has_windows
        and not has_plan_quotas
        and not has_off_pace
        and not has_subagents
    ):
        return None

    lines: list[str] = []

    # Last-turn snapshot — what the most recent prompt cost.
    if report.last_turn.ts is not None:
        lt = report.last_turn
        model = lt.model or fallback_model or "?"
        ctx_max = context_window_for(model, betas=betas)
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

    if has_subagents:
        if lines:
            lines.append("")
        lines.append("Subagent spend:")
        for line in (subagent_block or "").splitlines():
            lines.append(line)

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
