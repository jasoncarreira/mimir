"""Weekly event introspection report (ported from muninnbot).

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

Two data sources: ``logs/turns.jsonl`` and ``logs/events.jsonl``.

Algedonic side-effect: when ``--emit-algedonic`` is set and heartbeat
success rate falls below ``--health-threshold``, append a
``heartbeat_health_degraded`` event to events.jsonl so the agent's
algedonic surfacing picks it up next turn.

Invoked via ``mimir reflection introspection-report``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

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


# ─── Aggregation ───────────────────────────────────────────────────────


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_jsonl(path: Path) -> Iterable[dict]:
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec


def aggregate(
    turns_log: Path,
    events_log: Path,
    *,
    days: int = 7,
    now: datetime | None = None,
    recent_error_hours: int = 24,
    error_recurrence_top_n: int = 10,
) -> Report:
    """Walk turns.jsonl + events.jsonl once, build a Report."""
    now = now or datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=days)
    drift_cur_start = now - timedelta(days=7)
    drift_prev_start = now - timedelta(days=14)
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
        if ts is None or ts < cutoff:
            continue

        trigger = str(rec.get("trigger") or "?")
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
                name = ev.get("name") or "?"
                if isinstance(tid, str):
                    pending[tid] = name
                tool_usage_counts[(trigger, name)][0] += 1
                # Drift bucketing.
                if ts >= drift_cur_start:
                    cur_week_tools.add(name)
                elif ts >= drift_prev_start:
                    prev_week_tools.add(name)
                # Skill lifecycle: count Skill tool by skill arg.
                if name == "Skill":
                    args = ev.get("args") or {}
                    skill_name = (
                        args.get("skill") if isinstance(args, dict) else None
                    )
                    if isinstance(skill_name, str):
                        skill_counts[skill_name] += 1
            elif etype == "tool_result":
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
                    error_recurrence[(tool_name, preview)].append(ts)
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

    # Error recurrence: occurrences desc, top N.
    recurrence_rows = []
    for (tool, preview), occurrences in error_recurrence.items():
        recurrence_rows.append(ErrorRecurrence(
            tool_name=tool, preview=preview,
            occurrences=len(occurrences),
            first_seen=min(occurrences),
            last_seen=max(occurrences),
        ))
    recurrence_rows.sort(key=lambda r: -r.occurrences)
    recurrence_rows = recurrence_rows[:error_recurrence_top_n]

    # Pass 2: events.jsonl for heartbeat pipeline counts.
    pipeline = HeartbeatPipeline()
    for rec in _iter_jsonl(events_log):
        ts = _parse_ts(rec.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
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

    return "\n".join(lines).rstrip() + "\n"


# ─── Algedonic emit ────────────────────────────────────────────────────


def maybe_emit_health_event(
    report: Report,
    events_log: Path,
    *,
    threshold: float,
) -> bool:
    """When pipeline success rate is below ``threshold``, append a
    ``heartbeat_health_degraded`` event to events.jsonl. Returns True
    when emitted. No-op when:
      - heartbeat fired==0 in window (no signal to interpret)
      - success_rate >= threshold
    """
    rate = report.heartbeat.success_rate
    if rate is None or rate >= threshold:
        return False
    payload = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "type": "heartbeat_health_degraded",
        "session_id": "introspection-report",
        "success_rate": round(rate, 4),
        "threshold": threshold,
        "fired": report.heartbeat.fired,
        "successful": report.heartbeat.successful,
        "suppressed": report.heartbeat.suppressed,
        "dropped": report.heartbeat.dropped,
        "window_days": report.days,
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

    report = aggregate(turns, events, days=args.days)
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
