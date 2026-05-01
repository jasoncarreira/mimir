"""Aggregate subagent token usage from events.jsonl.

Source events: ``subagent_started`` / ``subagent_progress`` /
``subagent_notification`` — emitted by ``agent.py`` when the SDK
yields the corresponding Task* messages. ``TaskUsage`` is cumulative
per task (each progress / notification event carries the total
tokens *so far* for that task), so the aggregator picks the LATEST
seen total per ``task_id`` rather than summing across events.

Why this matters: a long-running climber subagent can burn most of
the parent's plan-window budget over an hour or two. Without
surfacing the breakdown, the agent sees "60% of weekly Opus quota
gone" with no way to know it was the climber. With this module,
the Resource usage block carries a Subagent spend line and an
Active subagent listing so the agent can decide "let it finish vs.
kill it."

Tail-streamed via ``_jsonl_tail.tail_jsonl_records`` so cost stays
O(events-in-window) regardless of total log size.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from ._jsonl_tail import tail_jsonl_records

log = logging.getLogger(__name__)


@dataclass
class SubagentWindow:
    """Cumulative spend over a contiguous time window. ``task_count``
    counts unique task_ids whose latest event landed in this window."""

    label: str
    total_tokens: int = 0
    task_count: int = 0


@dataclass
class ActiveSubagent:
    """A task with a started event but no notification within the
    scan range. ``description`` and ``task_type`` come from the
    started event; usage fields update from the latest progress."""

    task_id: str
    description: str | None = None
    task_type: str | None = None
    total_tokens: int = 0
    tool_uses: int = 0
    duration_ms: int = 0
    last_seen_ts: str | None = None


@dataclass
class SubagentReport:
    windows: list[SubagentWindow] = field(default_factory=list)
    active: list[ActiveSubagent] = field(default_factory=list)


def aggregate(
    events_path: Path,
    *,
    window_hours: Iterable[float] = (1.0, 5.0, 24.0 * 7),
    window_labels: Iterable[str] | None = None,
    active_max_age_hours: float = 24.0,
) -> SubagentReport:
    """Tail-stream events.jsonl, build per-task latest-usage map,
    bucket completed tasks by window, list active tasks (started
    within ``active_max_age_hours`` but not yet notified).

    Active-task age is measured against the started event's timestamp;
    a task that started 25h ago and is still progressing falls off the
    active list. The agent typically sees its own subagent activity
    across a few hours; longer than that and either the climber should
    be done or the operator should investigate."""
    windows = [float(h) for h in window_hours]
    if window_labels is None:
        window_labels = [_default_label(h) for h in windows]
    else:
        window_labels = list(window_labels)
    assert len(windows) == len(window_labels), "windows / labels length mismatch"

    now = datetime.now(tz=timezone.utc)
    cutoffs = [now - timedelta(hours=h) for h in windows]
    oldest_cutoff = min(cutoffs)
    active_cutoff = now - timedelta(hours=active_max_age_hours)
    # Bound the scan: don't read past the oldest of our two cutoffs.
    scan_floor = min(oldest_cutoff, active_cutoff)

    # Per-task latest-event map, populated tail-first.
    # We see newest events first, so the first hit per task_id is the
    # latest. Only later (older) events for the same task_id refine
    # description / task_type if those weren't on the latest event.
    latest: dict[str, dict] = {}
    started_records: dict[str, dict] = {}
    notified: set[str] = set()

    for ev in tail_jsonl_records(events_path):
        ts_str = ev.get("timestamp")
        if not isinstance(ts_str, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < scan_floor:
            break

        evtype = ev.get("type")
        task_id = ev.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue

        if evtype == "subagent_progress" or evtype == "subagent_notification":
            if task_id not in latest:
                latest[task_id] = ev
            if evtype == "subagent_notification":
                notified.add(task_id)
        elif evtype == "subagent_started":
            if task_id not in started_records:
                started_records[task_id] = ev

    # Build window aggregates from latest-event map.
    out_windows = [SubagentWindow(label=label) for label in window_labels]
    for task_id, ev in latest.items():
        ts_str = ev.get("timestamp")
        if not isinstance(ts_str, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        tokens = _as_int(ev.get("total_tokens")) or 0
        for idx, cutoff in enumerate(cutoffs):
            if ts >= cutoff:
                w = out_windows[idx]
                w.total_tokens += tokens
                w.task_count += 1

    # Active tasks: started but not yet notified, within active window.
    actives: list[ActiveSubagent] = []
    for task_id, started in started_records.items():
        if task_id in notified:
            continue
        ts_str = started.get("timestamp")
        if not isinstance(ts_str, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < active_cutoff:
            continue

        progress = latest.get(task_id) or {}
        actives.append(
            ActiveSubagent(
                task_id=task_id,
                description=started.get("description") or progress.get("description"),
                task_type=started.get("task_type"),
                total_tokens=_as_int(progress.get("total_tokens")) or 0,
                tool_uses=_as_int(progress.get("tool_uses")) or 0,
                duration_ms=_as_int(progress.get("duration_ms")) or 0,
                last_seen_ts=progress.get("timestamp") or ts_str,
            )
        )
    # Newest started first — agent reads what's most recent and most
    # likely to be the source of current spend.
    actives.sort(key=lambda a: a.last_seen_ts or "", reverse=True)

    return SubagentReport(windows=out_windows, active=actives)


def render_subagent_block(
    report: SubagentReport,
    *,
    max_active_listed: int = 5,
) -> str | None:
    """Markdown body for the Subagent spend subsection of the
    Resource usage block. Returns None when there's nothing to show
    (no completed tasks in any window AND no active subagents)."""
    has_windows = any(w.task_count > 0 for w in report.windows)
    has_active = bool(report.active)
    if not has_windows and not has_active:
        return None

    lines: list[str] = []
    if has_windows:
        for w in report.windows:
            if w.task_count == 0:
                continue
            lines.append(
                f"{w.label}: {_fmt_tokens(w.total_tokens)} tokens / "
                f"{w.task_count} task(s)"
            )

    if has_active:
        if lines:
            lines.append("")
        lines.append(f"Active: {len(report.active)}")
        for a in report.active[:max_active_listed]:
            tag = a.task_type or _short_task_id(a.task_id)
            elapsed = _humanize_duration_ms(a.duration_ms)
            lines.append(
                f"- {tag}: {_fmt_tokens(a.total_tokens)} tokens, "
                f"{a.tool_uses} tool uses, running {elapsed}"
            )
        if len(report.active) > max_active_listed:
            extra = len(report.active) - max_active_listed
            lines.append(f"- ...and {extra} more")

    return "\n".join(lines) if lines else None


def _fmt_tokens(n: int) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _humanize_duration_ms(ms: int) -> str:
    if ms <= 0:
        return "<1s"
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _short_task_id(task_id: str) -> str:
    """Trim long task ids — agent doesn't need the full UUID."""
    return task_id if len(task_id) <= 12 else task_id[:8] + "…"


def _default_label(hours: float) -> str:
    if hours <= 24:
        return f"Last {int(hours)}h"
    days = hours / 24
    if days == int(days):
        return f"Last {int(days)}d"
    return f"Last {days:.1f}d"


def _as_int(v) -> int | None:
    if isinstance(v, (int, float)):
        return int(v)
    return None
