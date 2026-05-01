"""Subagent token tracking from events.jsonl.

Source events: subagent_started / subagent_progress / subagent_notification.
TaskUsage is cumulative per task, so the aggregator picks the LATEST
seen total per task_id rather than summing across events."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mimir.subagent_stats import (
    aggregate,
    render_subagent_block,
)


def _ts(hours_ago: float = 0) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _ev(**kwargs) -> dict:
    return kwargs


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


# ---- aggregation -------------------------------------------------------


def test_aggregate_empty_returns_empty(tmp_path: Path):
    rep = aggregate(tmp_path / "events.jsonl")
    assert all(w.task_count == 0 for w in rep.windows)
    assert rep.active == []


def test_aggregate_picks_latest_total_per_task(tmp_path: Path):
    """Cumulative usage means we sum the latest, not all events."""
    path = tmp_path / "events.jsonl"
    _write(path, [
        # Append-chronological: oldest first.
        _ev(timestamp=_ts(2), type="subagent_started", task_id="t1"),
        _ev(timestamp=_ts(1.5), type="subagent_progress",
            task_id="t1", total_tokens=10_000),
        _ev(timestamp=_ts(1.0), type="subagent_progress",
            task_id="t1", total_tokens=20_000),
        _ev(timestamp=_ts(0.5), type="subagent_notification",
            task_id="t1", status="completed", total_tokens=30_000),
    ])
    rep = aggregate(path)
    win_5h = next(w for w in rep.windows if w.label == "Last 5h")
    # Latest is 30k (notification), NOT 10k+20k+30k=60k.
    assert win_5h.total_tokens == 30_000
    assert win_5h.task_count == 1


def test_aggregate_buckets_by_window(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write(path, [
        _ev(timestamp=_ts(48), type="subagent_started", task_id="t-old"),
        _ev(timestamp=_ts(40), type="subagent_notification",
            task_id="t-old", status="completed", total_tokens=5_000),
        _ev(timestamp=_ts(2), type="subagent_started", task_id="t-recent"),
        _ev(timestamp=_ts(0.5), type="subagent_notification",
            task_id="t-recent", status="completed", total_tokens=15_000),
    ])
    rep = aggregate(path)
    win_1h = next(w for w in rep.windows if w.label == "Last 1h")
    win_5h = next(w for w in rep.windows if w.label == "Last 5h")
    win_7d = next(w for w in rep.windows if w.label == "Last 7d")
    assert win_1h.total_tokens == 15_000
    assert win_5h.total_tokens == 15_000
    assert win_7d.total_tokens == 20_000  # both


def test_aggregate_lists_active_subagents(tmp_path: Path):
    """Tasks with a started event but no notification stay active.
    Their latest progress event populates the live usage."""
    path = tmp_path / "events.jsonl"
    _write(path, [
        _ev(timestamp=_ts(3), type="subagent_started",
            task_id="climber-1", description="optimize prompt",
            task_type="climber"),
        _ev(timestamp=_ts(1.5), type="subagent_progress",
            task_id="climber-1", description="optimize prompt",
            total_tokens=42_000, tool_uses=18, duration_ms=90 * 60 * 1000,
            last_tool_name="Bash"),
    ])
    rep = aggregate(path)
    assert len(rep.active) == 1
    a = rep.active[0]
    assert a.task_id == "climber-1"
    assert a.task_type == "climber"
    assert a.total_tokens == 42_000
    assert a.tool_uses == 18


def test_aggregate_excludes_notified_from_active(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write(path, [
        _ev(timestamp=_ts(2), type="subagent_started", task_id="t1"),
        _ev(timestamp=_ts(0.5), type="subagent_notification",
            task_id="t1", status="completed", total_tokens=1000),
    ])
    rep = aggregate(path)
    assert rep.active == []


def test_aggregate_drops_active_older_than_max_age(tmp_path: Path):
    """A task started 30h ago without a notification is probably orphaned;
    don't list it as 'active' anymore."""
    path = tmp_path / "events.jsonl"
    _write(path, [
        _ev(timestamp=_ts(48), type="subagent_started", task_id="t-stale"),
        _ev(timestamp=_ts(0.5), type="subagent_progress",
            task_id="t-stale", total_tokens=100),
    ])
    rep = aggregate(path, active_max_age_hours=24.0)
    assert rep.active == []


def test_aggregate_sorts_active_newest_first(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write(path, [
        _ev(timestamp=_ts(2), type="subagent_started", task_id="t-older",
            task_type="researcher"),
        _ev(timestamp=_ts(1.5), type="subagent_progress",
            task_id="t-older", total_tokens=5_000),
        _ev(timestamp=_ts(0.5), type="subagent_started", task_id="t-newer",
            task_type="climber"),
        _ev(timestamp=_ts(0.1), type="subagent_progress",
            task_id="t-newer", total_tokens=20_000),
    ])
    rep = aggregate(path)
    assert [a.task_id for a in rep.active] == ["t-newer", "t-older"]


# ---- rendering --------------------------------------------------------


def test_render_returns_none_when_nothing_to_show(tmp_path: Path):
    rep = aggregate(tmp_path / "missing.jsonl")
    assert render_subagent_block(rep) is None


def test_render_includes_window_lines(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write(path, [
        _ev(timestamp=_ts(2), type="subagent_started", task_id="t1"),
        _ev(timestamp=_ts(0.5), type="subagent_notification",
            task_id="t1", status="completed", total_tokens=42_000),
    ])
    rep = aggregate(path)
    body = render_subagent_block(rep)
    assert body is not None
    assert "Last 5h: 42k tokens / 1 task(s)" in body


def test_render_lists_active_subagents(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write(path, [
        _ev(timestamp=_ts(2), type="subagent_started",
            task_id="climber-abc", task_type="climber"),
        _ev(timestamp=_ts(0.5), type="subagent_progress",
            task_id="climber-abc", total_tokens=320_000, tool_uses=42,
            duration_ms=2 * 3600 * 1000),
    ])
    rep = aggregate(path)
    body = render_subagent_block(rep)
    assert body is not None
    assert "Active: 1" in body
    assert "climber" in body
    assert "320k tokens" in body
    assert "42 tool uses" in body


def test_render_caps_active_listing(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    records = []
    for i in range(8):
        records.append(_ev(
            timestamp=_ts(0.5 - i * 0.01), type="subagent_started",
            task_id=f"t-{i}", task_type="researcher",
        ))
        records.append(_ev(
            timestamp=_ts(0.5 - i * 0.01) + "x",  # invalid; stays out of latest
            type="x", task_id=f"t-{i}",
        ))
    # Not actually testing the invalid records here — just enough Active
    # entries to trigger truncation.
    _write(path, records)
    rep = aggregate(path)
    body = render_subagent_block(rep, max_active_listed=3)
    assert body is not None
    assert "Active: 8" in body
    assert "...and 5 more" in body
