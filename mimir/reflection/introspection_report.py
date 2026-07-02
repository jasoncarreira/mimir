"""Weekly event introspection report.

Replaces step-1 hand work in the reflection skill with a structured
report computed once. Produces a markdown summary covering:

- Turn counts by trigger type
- Tool usage (per trigger, with error rates)
- Tool error analysis (only tools that errored)
- Recent errors (last 24h, capped)
- Behavioral drift week-over-week (tools started/stopped)
- Heartbeat / scheduled-tick health (success rate, suppressed/dropped counts)
- Performance trends (daily avg duration)
- Recurring error patterns (top-N with first/last seen)
- Skill lifecycle (Skill tool calls)
- Skill refine/retire candidates (chainlink #267): per-skill objective
  signals — skill_outcomes success-rate, negative-kind skill_learning
  count, zero-recent-usage — surfaced so the reflection turn can author
  operator-gated refine/retire recommendations (#226; surface, not
  auto-act).
- Memory health: compact read-only ``mimir memory doctor`` summary for
  memory/core, channel memory, issue notes, SAGA substrate, indexes, and
  wiki/state drift.

Data sources: ``logs/turns.jsonl``, ``logs/events.jsonl``, and (for the
skill-health section) the installed-skill inventory + a read-only saga
connection for the negative-learning count. When ``home`` is provided, the
report also includes home-gated sections such as skill health and the
memory-health section, which calls :mod:`mimir.memory_doctor` read-only.

Algedonic side-effect: when ``--emit-algedonic`` is set and heartbeat
success rate falls below ``--health-threshold``, append a
``heartbeat_health_degraded`` event to events.jsonl so the agent's
algedonic surfacing picks it up next turn.

Invoked via ``mimir reflection introspection-report``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# ─── Data model ────────────────────────────────────────────────────────


@dataclass
class TriggerStats:
    trigger: str
    total_turns: int = 0
    successful: int = 0
    avg_duration_sec: float | None = None
    unique_channels: int = 0

    @property
    def success_rate(self) -> float | None:
        if self.total_turns == 0:
            return None
        return self.successful / self.total_turns


@dataclass
class ToolUsage:
    trigger: str
    tool_name: str
    total_calls: int
    errors: int

    @property
    def error_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.errors / self.total_calls


@dataclass
class RecentError:
    ts: datetime
    trigger: str
    tool_name: str
    preview: str


@dataclass
class PerformanceTrend:
    day: str           # YYYY-MM-DD
    trigger: str
    turns: int
    avg_sec: float
    min_sec: float
    max_sec: float


@dataclass
class ErrorRecurrence:
    tool_name: str
    preview: str        # truncated content
    occurrences: int
    first_seen: datetime
    last_seen: datetime


@dataclass
class HeartbeatPipeline:
    """Full scheduled-tick pipeline counts. Each tick goes through:
    arbiter → dispatcher → agent. We track each layer so degradation
    can be located, not just observed."""

    fired: int = 0          # scheduled_tick events
    suppressed: int = 0     # scheduled_tick_suppressed events (arbiter)
    dropped: int = 0        # scheduled_tick_dropped events (dispatcher)
    completed: int = 0      # turns with trigger=scheduled_tick (any error)
    successful: int = 0     # turns with trigger=scheduled_tick AND error is None

    @property
    def attempted(self) -> int:
        return self.fired + self.suppressed

    @property
    def success_rate(self) -> float | None:
        """Pipeline success: completed turns with no error / fired."""
        if self.fired == 0:
            return None
        return self.successful / self.fired


@dataclass
class SkillHealth:
    """Per-skill evaluate→refine signal for the reflection turn (chainlink
    #267). Aggregates the objective per-skill signals so the reflection
    turn can author a refine/retire proposed-change (operator-gated, #226 —
    this never auto-acts). ``reasons`` is the human-readable evidence."""

    skill: str
    invocations: int
    success_rate: float | None        # skill_outcomes; None if never ran in window
    runs: int                         # outcome samples behind success_rate
    negative_learnings: int           # count of negative-kind skill_learning atoms
    refine_candidate: bool
    retire_candidate: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class MemoryHealthFinding:
    section: str
    severity: str
    path: str
    check: str
    message: str
    suggestion: str


@dataclass
class MemoryHealthSummary:
    """Compact read-only ``mimir memory doctor`` projection for the weekly
    introspection report. The full doctor remains the source of truth; this
    summary is intentionally small enough to make drift visible without
    turning the report into the full diagnostic dump."""

    status: str
    severity_counts: dict[str, int]
    section_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    top_findings: list[MemoryHealthFinding] = field(default_factory=list)
    error: str | None = None


@dataclass
class Report:
    days: int
    generated_at: datetime
    turn_counts: list[TriggerStats] = field(default_factory=list)
    tool_usage: list[ToolUsage] = field(default_factory=list)
    errors_by_tool: list[ToolUsage] = field(default_factory=list)
    recent_errors: list[RecentError] = field(default_factory=list)
    drift_started: list[str] = field(default_factory=list)
    drift_stopped: list[str] = field(default_factory=list)
    heartbeat: HeartbeatPipeline = field(default_factory=HeartbeatPipeline)
    performance_trends: list[PerformanceTrend] = field(default_factory=list)
    error_recurrence: list[ErrorRecurrence] = field(default_factory=list)
    skill_lifecycle: list[tuple[str, int]] = field(default_factory=list)
    skill_health: list[SkillHealth] = field(default_factory=list)
    memory_health: MemoryHealthSummary | None = None


# ─── Aggregation ───────────────────────────────────────────────────────


# Patterns for error-message normalization. Strip volatile tokens so
# "FileNotFoundError: /tmp/abc" and "FileNotFoundError: /tmp/xyz" land
# in the same bucket. Order matters — paths before numbers (paths
# contain numbers) and numbers before short hex (longer hex first).
import re as _re

_READ_FILE_NOT_FOUND_RE = _re.compile(r"^Error: File '[^']+' not found\b")

_NORMALIZE_PATTERNS: list[tuple["_re.Pattern[str]", str]] = [
    # Filesystem paths (absolute and home-relative).
    (_re.compile(r"(?:/[A-Za-z0-9_.-]+)+"), "<path>"),
    # IPv4 addresses.
    (_re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b"), "<ip>"),
    # UUIDs (hyphenated, 8-4-4-4-12).
    (_re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    # Long hex ids (16+ hex chars — includes saga's 16-char tids).
    (_re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<hex>"),
    # Bare integers (line numbers, sizes, timeouts).
    (_re.compile(r"\b\d+\b"), "<n>"),
]


def _normalize_error_for_grouping(text: str) -> str:
    """Collapse volatile tokens so similar errors group. Used as the
    dict key in error_recurrence — the rendered preview still shows
    the raw form.

    Read-file missing-path errors are the exception: the path is the
    actionable identity, not volatile decoration. Grouping every
    ``Error: File '<path>' not found`` row together makes the report
    attribute unrelated missing files to whichever path happened to be
    latest in the bucket.
    """
    if _READ_FILE_NOT_FOUND_RE.match(text):
        return " ".join(text.split())

    out = text
    for pat, repl in _NORMALIZE_PATTERNS:
        out = pat.sub(repl, out)
    # Collapse runs of whitespace introduced by the substitutions.
    out = " ".join(out.split())
    return out


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_jsonl(path: Path) -> Iterable[dict]:
    """Yield records newest-first from ``path``.

    chainlink #244: prior shape was ``read_text()`` + chronological
    splitlines (full file in memory). Both call sites filter by
    ``ts >= cutoff`` and skip older records — iterating newest-first
    via :func:`tail_jsonl_records` lets them produce identical results
    on O(window) reads instead of O(file).
    """
    from .._jsonl_tail import tail_jsonl_records

    for rec in tail_jsonl_records(path):
        if isinstance(rec, dict):
            yield rec


# ─── Skill refine/retire candidates (chainlink #267) ───────────────────
# Thresholds for surfacing a skill as a reflection refine/retire
# candidate. Deliberately conservative — the reflection turn proposes,
# the operator decides (#226); a false-positive candidate costs only a
# glance, a missed one costs a silently-degrading skill.
_SKILL_REFINE_SUCCESS_FLOOR = 0.5   # success rate under this → refine
_SKILL_REFINE_MIN_RUNS = 3          # ...but only with enough samples to trust
_SKILL_REFINE_NEG_LEARNINGS = 3     # negative-kind learnings in window → refine


def _build_memory_health(
    home: "Path | None",
    *,
    top_n: int = 8,
) -> MemoryHealthSummary | None:
    """Run ``mimir memory doctor`` read-only and collapse it to the section
    needed in the introspection report.

    Best-effort by design: a doctor failure should appear as a Memory Health
    error row, not fail the whole behavioral report. The compact summary is
    self-contained; callers that want the full doctor detail should run
    ``mimir memory doctor --json`` separately.
    """
    if home is None:
        return None
    try:
        from ..memory_doctor import run_doctor

        doctor = run_doctor(home)
    except Exception as exc:  # noqa: BLE001 — diagnostics must degrade visibly
        log.warning("memory_health: memory doctor failed", exc_info=True)
        return MemoryHealthSummary(
            status="error",
            severity_counts={"error": 1, "warning": 0, "info": 0},
            error=f"{type(exc).__name__}: {exc}",
        )

    section_counts: dict[str, dict[str, int]] = {
        section.name: dict(section.metrics) for section in doctor.sections
    }
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    findings = sorted(
        doctor.findings,
        key=lambda f: (severity_rank.get(f.severity, 99), f.layer, f.path, f.check),
    )[:top_n]
    return MemoryHealthSummary(
        status=doctor.status,
        severity_counts=dict(doctor.severity_counts),
        section_counts=section_counts,
        top_findings=[
            MemoryHealthFinding(
                section=f.layer,
                severity=f.severity,
                path=f.path,
                check=f.check,
                message=f.message,
                suggestion=f.suggestion,
            )
            for f in findings
        ],
    )


def _build_skill_health(
    *,
    home: "Path | None",
    turns_log: Path,
    days: int,
    now: datetime,
    skill_counts: "Counter[str]",
    saga_conn: object | None = None,
) -> list[SkillHealth]:
    """Per-skill refine/retire candidates from the objective signals
    (chainlink #267): skill_outcomes success-rate + negative-kind
    ``skill_learning`` count + zero-recent-usage. The reflection turn reads
    these and authors operator-gated refine/retire recommendations.

    Best-effort: a missing input (no ``home``, no saga conn, an import or
    DB error) degrades to the signals available rather than failing the
    whole report. Returns only skills that crossed at least one threshold.
    """
    if home is None:
        return []
    outcomes: dict = {}
    try:
        from ..skill_outcomes import (
            aggregate as _agg_outcomes,
            load_skill_success_criteria,
        )
        outcomes = _agg_outcomes(
            turns_log, window_hours=days * 24, now=now,
            skill_criteria=load_skill_success_criteria(home),
        )
    except Exception:  # noqa: BLE001 — report must not fail on this section
        log.warning("skill_health: skill_outcomes aggregate failed", exc_info=True)

    installed: list[str] = []
    try:
        from ..skill_defs import installed_skill_names
        installed = installed_skill_names(home)
    except Exception:  # noqa: BLE001
        installed = []

    cutoff_iso = (now - timedelta(days=days)).isoformat()

    def _negatives(skill: str) -> int:
        if saga_conn is None:
            return 0
        try:
            from ..skill_memory import count_negative_learnings
            return count_negative_learnings(saga_conn, skill, since_iso=cutoff_iso)
        except Exception:  # noqa: BLE001
            return 0

    candidates: list[SkillHealth] = []
    for skill in sorted(set(skill_counts) | set(outcomes) | set(installed)):
        oc = outcomes.get(skill)
        success_rate = oc.success_rate if oc is not None else None
        runs = oc.total if oc is not None else 0
        invocations = int(skill_counts.get(skill, 0))
        negatives = _negatives(skill)

        reasons: list[str] = []
        refine = retire = False
        if (
            success_rate is not None
            and runs >= _SKILL_REFINE_MIN_RUNS
            and success_rate < _SKILL_REFINE_SUCCESS_FLOOR
        ):
            refine = True
            reasons.append(
                f"success rate {success_rate:.0%} over {runs} run(s)"
            )
        if negatives >= _SKILL_REFINE_NEG_LEARNINGS:
            refine = True
            reasons.append(f"{negatives} negative learning(s) in {days}d")
        # Retire: installed but no sign of use this window (neither an
        # explicit Skill() call nor any skill_outcomes sample).
        if skill in installed and invocations == 0 and runs == 0:
            retire = True
            reasons.append(f"no usage in {days}d")

        if refine or retire:
            candidates.append(SkillHealth(
                skill=skill,
                invocations=invocations,
                success_rate=success_rate,
                runs=runs,
                negative_learnings=negatives,
                refine_candidate=refine,
                retire_candidate=retire,
                reasons=reasons,
            ))
    return candidates


def aggregate(
    turns_log: Path,
    events_log: Path,
    *,
    days: int = 7,
    now: datetime | None = None,
    home: "Path | None" = None,
    saga_conn: object | None = None,
    recent_error_hours: int = 24,
    error_recurrence_top_n: int = 10,
) -> Report:
    """Walk turns.jsonl + events.jsonl once, build a Report.

    Drift always uses a 14-day window (current week vs previous week)
    regardless of ``days`` — otherwise the previous-week bucket is
    empty and ``drift_stopped`` is always []. The outer scan extends
    to ``max(days, 14)`` for that reason; non-drift sections still
    respect the requested ``days`` cutoff."""
    now = now or datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=days)
    drift_cur_start = now - timedelta(days=7)
    drift_prev_start = now - timedelta(days=14)
    # Extend the read horizon so drift's previous-week bucket is
    # populated when days < 14.
    scan_cutoff = min(cutoff, drift_prev_start)
    recent_errors_cutoff = now - timedelta(hours=recent_error_hours)

    # Pass 1: turns.
    by_trigger: dict[str, TriggerStats] = defaultdict(
        lambda: TriggerStats(trigger="?")
    )
    duration_sums: dict[str, float] = defaultdict(float)
    duration_counts: dict[str, int] = defaultdict(int)
    channels_by_trigger: dict[str, set[str]] = defaultdict(set)

    tool_usage_counts: dict[tuple[str, str], list[int]] = defaultdict(
        lambda: [0, 0]  # [calls, errors]
    )
    cur_week_tools: set[str] = set()
    prev_week_tools: set[str] = set()
    recent_errors: list[RecentError] = []
    error_recurrence: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    daily_perf: dict[tuple[str, str], list[float]] = defaultdict(list)
    skill_counts: Counter[str] = Counter()

    for rec in _iter_jsonl(turns_log):
        ts = _parse_ts(rec.get("ts"))
        if ts is None:
            continue
        if ts < scan_cutoff:
            # chainlink #244: newest-first iteration — older records
            # can't contribute, early-stop.
            break

        # Non-drift sections (tool usage, errors, etc.) still respect
        # the requested ``days`` window — only drift looks further back.
        in_window = ts >= cutoff

        trigger = str(rec.get("trigger") or "?")
        if in_window:
            stats = by_trigger[trigger]
            stats.trigger = trigger
            stats.total_turns += 1
            if rec.get("error") is None:
                stats.successful += 1
            ch = rec.get("channel_id")
            if isinstance(ch, str):
                channels_by_trigger[trigger].add(ch)
            dur_ms = rec.get("duration_ms")
            if isinstance(dur_ms, (int, float)) and dur_ms > 0:
                duration_sums[trigger] += float(dur_ms)
                duration_counts[trigger] += 1
                day = ts.strftime("%Y-%m-%d")
                daily_perf[(day, trigger)].append(float(dur_ms) / 1000.0)

        # Pair tool_call ↔ tool_result by id within this turn.
        events = rec.get("events") or []
        if not isinstance(events, list):
            continue
        pending: dict[str, str] = {}
        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type")
            if etype == "tool_call":
                tid = ev.get("id")
                name = ev.get("name")
                # Skip nameless tool_calls — they shouldn't normally
                # exist, but historic schema drift produced a few; the
                # "?" sentinel poisons drift sets and tool-usage rows.
                if not isinstance(name, str) or not name:
                    continue
                if isinstance(tid, str):
                    pending[tid] = name
                if in_window:
                    tool_usage_counts[(trigger, name)][0] += 1
                # Drift bucketing — always considered (window is the
                # full 14-day scan; only this section uses pre-cutoff data).
                if ts >= drift_cur_start:
                    cur_week_tools.add(name)
                elif ts >= drift_prev_start:
                    prev_week_tools.add(name)
                # Skill lifecycle: count Skill tool by skill arg.
                if in_window and name == "Skill":
                    args = ev.get("args") or {}
                    skill_name = (
                        args.get("skill") if isinstance(args, dict) else None
                    )
                    if isinstance(skill_name, str):
                        skill_counts[skill_name] += 1
            elif etype == "tool_result" and in_window:
                tid = ev.get("id")
                is_error = bool(ev.get("is_error"))
                tool_name = (
                    pending.get(tid) if isinstance(tid, str) else None
                ) or ev.get("name") or "?"
                if is_error:
                    tool_usage_counts[(trigger, tool_name)][1] += 1
                    content = ev.get("content") or ""
                    if not isinstance(content, str):
                        content = str(content)
                    preview = content[:100].replace("\n", " ")
                    # Normalize for grouping — strip volatile tokens
                    # (paths, hex ids, line numbers, IPs) so similar
                    # errors cluster instead of fragmenting per-call. Use
                    # full content for the read_file missing-path exception:
                    # long paths can push " not found" beyond the preview cap.
                    norm_source = content if _READ_FILE_NOT_FOUND_RE.match(content) else preview
                    norm = _normalize_error_for_grouping(norm_source)
                    error_recurrence[(tool_name, norm)].append((ts, preview))
                    if ts >= recent_errors_cutoff:
                        recent_errors.append(RecentError(
                            ts=ts, trigger=trigger,
                            tool_name=tool_name,
                            preview=preview[:80],
                        ))

    # Finalize trigger stats.
    turn_counts = []
    for trigger, stats in sorted(
        by_trigger.items(), key=lambda kv: -kv[1].total_turns,
    ):
        if duration_counts[trigger] > 0:
            stats.avg_duration_sec = round(
                duration_sums[trigger] / duration_counts[trigger] / 1000.0, 2
            )
        stats.unique_channels = len(channels_by_trigger[trigger])
        turn_counts.append(stats)

    # Tool usage: list sorted by trigger then count desc.
    tool_usage = []
    for (trigger, tool), (calls, errors) in tool_usage_counts.items():
        tool_usage.append(ToolUsage(
            trigger=trigger, tool_name=tool,
            total_calls=calls, errors=errors,
        ))
    tool_usage.sort(key=lambda t: (t.trigger, -t.total_calls))
    errors_by_tool = sorted(
        [t for t in tool_usage if t.errors > 0],
        key=lambda t: -t.error_rate,
    )

    # Recent errors: most-recent first, cap.
    recent_errors.sort(key=lambda e: e.ts, reverse=True)
    recent_errors = recent_errors[:20]

    # Drift.
    drift_started = sorted(cur_week_tools - prev_week_tools)
    drift_stopped = sorted(prev_week_tools - cur_week_tools)

    # Performance trends: most-recent day first.
    performance_trends = []
    for (day, trigger), durs in sorted(daily_perf.items(), reverse=True):
        if not durs:
            continue
        performance_trends.append(PerformanceTrend(
            day=day, trigger=trigger,
            turns=len(durs),
            avg_sec=round(sum(durs) / len(durs), 2),
            min_sec=round(min(durs), 2),
            max_sec=round(max(durs), 2),
        ))

    # Error recurrence: occurrences desc, top N. The dict keys on the
    # *normalized* form so similar errors cluster; the rendered preview
    # uses the most-recent raw content so the agent sees a real example.
    recurrence_rows = []
    for (tool, _norm), entries in error_recurrence.items():
        # entries: list of (ts, raw_preview) tuples
        timestamps = [e[0] for e in entries]
        latest = max(entries, key=lambda e: e[0])
        recurrence_rows.append(ErrorRecurrence(
            tool_name=tool,
            preview=latest[1],
            occurrences=len(entries),
            first_seen=min(timestamps),
            last_seen=max(timestamps),
        ))
    recurrence_rows.sort(key=lambda r: -r.occurrences)
    recurrence_rows = recurrence_rows[:error_recurrence_top_n]

    # Pass 2: events.jsonl for heartbeat pipeline counts.
    pipeline = HeartbeatPipeline()
    for rec in _iter_jsonl(events_log):
        ts = _parse_ts(rec.get("timestamp"))
        if ts is None:
            continue
        if ts < cutoff:
            # chainlink #244: newest-first iteration — older records
            # can't contribute, early-stop.
            break
        etype = rec.get("type")
        if etype == "scheduled_tick":
            pipeline.fired += 1
        elif etype == "scheduled_tick_suppressed":
            pipeline.suppressed += 1
        elif etype == "scheduled_tick_dropped":
            pipeline.dropped += 1
    # Completed/successful come from turn_counts (already computed).
    sched = next((s for s in turn_counts if s.trigger == "scheduled_tick"), None)
    if sched is not None:
        pipeline.completed = sched.total_turns
        pipeline.successful = sched.successful

    skill_lifecycle = sorted(skill_counts.items(), key=lambda kv: -kv[1])
    skill_health = _build_skill_health(
        home=home,
        turns_log=turns_log,
        days=days,
        now=now,
        skill_counts=skill_counts,
        saga_conn=saga_conn,
    )
    memory_health = _build_memory_health(home)

    return Report(
        days=days,
        generated_at=now,
        turn_counts=turn_counts,
        tool_usage=tool_usage,
        errors_by_tool=errors_by_tool,
        recent_errors=recent_errors,
        drift_started=drift_started,
        drift_stopped=drift_stopped,
        heartbeat=pipeline,
        performance_trends=performance_trends,
        error_recurrence=recurrence_rows,
        skill_lifecycle=skill_lifecycle,
        skill_health=skill_health,
        memory_health=memory_health,
    )


# ─── Render ────────────────────────────────────────────────────────────


def _fmt_pct(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{rate * 100:.1f}%"


def _fmt_ts(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def render_markdown(report: Report) -> str:
    lines: list[str] = []
    lines.append(f"# Event Introspection Report")
    lines.append(f"")
    lines.append(f"**Generated:** {_fmt_ts(report.generated_at)}")
    lines.append(f"**Period:** Last {report.days} days")
    lines.append("")

    # Turn summary.
    lines.append("## Turn Summary")
    lines.append("")
    lines.append("| Trigger | Total | Success Rate | Avg Duration (s) | Channels |")
    lines.append("|---------|-------|--------------|------------------|----------|")
    for s in report.turn_counts:
        lines.append(
            f"| {s.trigger} | {s.total_turns} | {_fmt_pct(s.success_rate)} | "
            f"{s.avg_duration_sec if s.avg_duration_sec is not None else 'n/a'} | "
            f"{s.unique_channels} |"
        )
    lines.append("")

    # Heartbeat / scheduled-tick health.
    lines.append("## Heartbeat / scheduled-tick health")
    lines.append("")
    pl = report.heartbeat
    lines.append(f"- Fired (reached enqueue): **{pl.fired}**")
    lines.append(f"- Suppressed by arbiter: **{pl.suppressed}**")
    lines.append(f"- Dropped by dispatcher: **{pl.dropped}**")
    lines.append(f"- Completed turns: **{pl.completed}**")
    lines.append(f"- Successful turns: **{pl.successful}**")
    lines.append(f"- Pipeline success rate: **{_fmt_pct(pl.success_rate)}** "
                 f"(successful / fired)")
    lines.append("")

    # Memory health from mimir memory doctor.
    if report.memory_health is not None:
        mh = report.memory_health
        lines.append("## Memory Health")
        lines.append("")
        lines.append(
            f"- Status: **{mh.status}** "
            f"(error={mh.severity_counts.get('error', 0)}, "
            f"warning={mh.severity_counts.get('warning', 0)}, "
            f"info={mh.severity_counts.get('info', 0)})"
        )
        if mh.error:
            lines.append(f"- Doctor run failed: `{mh.error}`")
        if mh.section_counts:
            lines.append("")
            lines.append("| Section | Key counts |")
            lines.append("|---------|------------|")
            for section, metrics in sorted(mh.section_counts.items()):
                key_counts = ", ".join(
                    f"{k}={v}" for k, v in sorted(metrics.items())
                    if isinstance(v, int)
                )
                lines.append(f"| {section} | {key_counts} |")
        if mh.top_findings:
            lines.append("")
            lines.append("Top actionable findings:")
            for finding in mh.top_findings:
                path = f" `{finding.path}`" if finding.path else ""
                lines.append(
                    f"- [{finding.severity}] {finding.section}/{finding.check}"
                    f"{path}: {finding.message} Suggestion: {finding.suggestion}"
                )
        else:
            lines.append("- Top actionable findings: none")
        lines.append("")

    # Tool usage.
    if report.tool_usage:
        lines.append("## Tool usage by trigger")
        lines.append("")
        cur = None
        for t in report.tool_usage:
            if t.trigger != cur:
                cur = t.trigger
                lines.append(f"### {cur}")
                lines.append("")
                lines.append("| Tool | Calls | Errors | Error rate |")
                lines.append("|------|-------|--------|------------|")
            lines.append(
                f"| {t.tool_name} | {t.total_calls} | {t.errors} | "
                f"{_fmt_pct(t.error_rate)} |"
            )
        lines.append("")

    # Errors by tool.
    if report.errors_by_tool:
        lines.append("## Tools with errors (sorted by error rate)")
        lines.append("")
        lines.append("| Trigger | Tool | Calls | Errors | Error rate |")
        lines.append("|---------|------|-------|--------|------------|")
        for t in report.errors_by_tool:
            lines.append(
                f"| {t.trigger} | {t.tool_name} | {t.total_calls} | "
                f"{t.errors} | {_fmt_pct(t.error_rate)} |"
            )
        lines.append("")

    # Recent errors.
    if report.recent_errors:
        lines.append("## Recent errors (last 24h)")
        lines.append("")
        lines.append("| Time | Trigger | Tool | Preview |")
        lines.append("|------|---------|------|---------|")
        for e in report.recent_errors:
            preview = e.preview.replace("|", "\\|")
            lines.append(
                f"| {_fmt_ts(e.ts)} | {e.trigger} | {e.tool_name} | {preview} |"
            )
        lines.append("")

    # Behavioral drift.
    if report.drift_started or report.drift_stopped:
        lines.append("## Behavioral drift (week-over-week)")
        lines.append("")
        if report.drift_started:
            lines.append("**Started using:**")
            for tool in report.drift_started:
                lines.append(f"- {tool}")
            lines.append("")
        if report.drift_stopped:
            lines.append("**Stopped using:**")
            for tool in report.drift_stopped:
                lines.append(f"- {tool}")
            lines.append("")

    # Performance trends.
    if report.performance_trends:
        lines.append("## Performance trends (daily)")
        lines.append("")
        lines.append("| Date | Trigger | Turns | Avg (s) | Min (s) | Max (s) |")
        lines.append("|------|---------|-------|---------|---------|---------|")
        for p in report.performance_trends[:14]:
            lines.append(
                f"| {p.day} | {p.trigger} | {p.turns} | {p.avg_sec} | "
                f"{p.min_sec} | {p.max_sec} |"
            )
        lines.append("")

    # Error recurrence.
    if report.error_recurrence:
        lines.append("## Recurring errors")
        lines.append("")
        lines.append("| Tool | Preview | Occurrences | First seen | Last seen |")
        lines.append("|------|---------|-------------|------------|-----------|")
        for r in report.error_recurrence:
            preview = r.preview.replace("|", "\\|")
            lines.append(
                f"| {r.tool_name} | {preview} | {r.occurrences} | "
                f"{_fmt_ts(r.first_seen)} | {_fmt_ts(r.last_seen)} |"
            )
        lines.append("")

    # Skill lifecycle.
    if report.skill_lifecycle:
        lines.append("## Skill invocations")
        lines.append("")
        lines.append("| Skill | Calls |")
        lines.append("|-------|-------|")
        for skill, count in report.skill_lifecycle:
            lines.append(f"| {skill} | {count} |")
        lines.append("")

    # Skill refine/retire candidates (chainlink #267). The reflection turn
    # reads this and authors operator-gated refine/retire recommendations
    # (#226 — surface, don't auto-act). Empty section is omitted.
    if report.skill_health:
        lines.append("## Skill refine/retire candidates")
        lines.append("")
        lines.append(
            "_Objective per-skill signals that crossed a threshold. "
            "Consider a refine/retire proposed-change; the operator decides._"
        )
        lines.append("")
        lines.append("| Skill | Action | Success | Runs | Neg. learnings | Why |")
        lines.append("|-------|--------|---------|------|----------------|-----|")
        for sh in report.skill_health:
            action = "retire" if sh.retire_candidate and not sh.refine_candidate else "refine"
            lines.append(
                f"| {sh.skill} | {action} | {_fmt_pct(sh.success_rate)} | "
                f"{sh.runs} | {sh.negative_learnings} | "
                f"{'; '.join(sh.reasons)} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ─── Algedonic emit ────────────────────────────────────────────────────


def health_degraded_fields(report: Report, *, threshold: float) -> dict | None:
    """Pure decision: the ``heartbeat_health_degraded`` event fields when the
    pipeline success rate is below ``threshold``, else ``None``.

    Returns only the payload fields (no ``type``/``session_id``/``timestamp`` —
    the EventLogger stamps those). No signal when ``success_rate`` is ``None``
    (heartbeat fired==0 in window) or >= threshold. Callers emit via the shared
    logger (``log_event`` / ``log_event_sync``) so the write is serialized —
    never a raw append, which races the EventLogger's trim (#486)."""
    rate = report.heartbeat.success_rate
    if rate is None or rate >= threshold:
        return None
    return {
        "success_rate": round(rate, 4),
        "threshold": threshold,
        "fired": report.heartbeat.fired,
        "successful": report.heartbeat.successful,
        "suppressed": report.heartbeat.suppressed,
        "dropped": report.heartbeat.dropped,
        "window_days": report.days,
    }


def maybe_emit_health_event(
    report: Report,
    events_log: Path,
    *,
    threshold: float,
) -> bool:
    """Standalone-CLI helper: append a ``heartbeat_health_degraded`` event to
    ``events_log`` when degraded. Returns True when emitted.

    Used only by the ``mimir`` introspection CLI, which runs as its own process
    with no concurrent EventLogger — so the direct append is race-free there.
    The in-process scheduler path must NOT use this; it emits via
    :func:`health_degraded_fields` + ``log_event`` to share the EventLogger lock
    (#486)."""
    fields = health_degraded_fields(report, threshold=threshold)
    if fields is None:
        return False
    payload = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "type": "heartbeat_health_degraded",
        "session_id": "introspection-report",
        **fields,
    }
    events_log.parent.mkdir(parents=True, exist_ok=True)
    with events_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return True


# ─── CLI ───────────────────────────────────────────────────────────────


def add_argparse(p: argparse.ArgumentParser) -> None:
    p.add_argument("--days", type=int, default=7,
                   help="Window in days (default 7).")
    p.add_argument("--output", type=Path, default=None,
                   help="Write report to this file. Default: stdout.")
    p.add_argument("--emit-algedonic", action="store_true",
                   help="When pipeline success rate is below "
                        "--health-threshold, append a "
                        "heartbeat_health_degraded event to events.jsonl.")
    p.add_argument("--health-threshold", type=float, default=0.80,
                   help="Pipeline success rate below which the algedonic "
                        "event fires (default 0.80).")
    p.add_argument("--home", type=Path, default=None,
                   help="Agent home (overrides MIMIR_HOME; default: cwd).")


def run(args: argparse.Namespace) -> int:
    import os
    home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
    turns = home / "logs" / "turns.jsonl"
    events = home / "logs" / "events.jsonl"

    # Best-effort read-only saga connection for the per-skill negative-
    # learning count (#267). Missing db / open failure just drops that one
    # evidence input — the rest of the skill-health section still computes.
    saga_conn = None
    saga_db = home / ".mimir" / "saga.db"
    if saga_db.is_file():
        try:
            import sqlite3
            saga_conn = sqlite3.connect(f"file:{saga_db}?mode=ro", uri=True)
        except Exception:  # noqa: BLE001
            log.warning("introspection: saga.db open failed", exc_info=True)
            saga_conn = None

    try:
        report = aggregate(
            turns,
            events,
            days=args.days,
            home=home,
            saga_conn=saga_conn,
        )
    finally:
        if saga_conn is not None:
            saga_conn.close()
    body = render_markdown(report)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(body)

    if args.emit_algedonic:
        emitted = maybe_emit_health_event(
            report, events, threshold=args.health_threshold,
        )
        if emitted:
            print(
                f"heartbeat_health_degraded emitted "
                f"(rate {report.heartbeat.success_rate:.2f} < "
                f"threshold {args.health_threshold:.2f})",
                file=sys.stderr,
            )
    return 0
