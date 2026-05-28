"""Tests for mimir/skills/reflection/introspection_report.py.

Ports muninnbot's weekly event-introspection pattern to mimir's
turns.jsonl + events.jsonl shape, plus a heartbeat-health algedonic
emit."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.reflection.introspection_report import (
    HeartbeatPipeline,
    Report,
    aggregate,
    maybe_emit_health_event,
    render_markdown,
)

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def _write_turn(
    path: Path,
    *,
    ts: datetime,
    trigger: str = "user_message",
    channel_id: str = "chan",
    duration_ms: int = 1000,
    error: str | None = None,
    tool_calls: list[tuple[str, bool, str]] | None = None,
    skill: str | None = None,
) -> None:
    """tool_calls = [(name, is_error, content), ...]."""
    path.parent.mkdir(parents=True, exist_ok=True)
    events: list[dict] = []
    pairs = tool_calls or []
    for i, (name, is_err, content) in enumerate(pairs):
        events.append({
            "type": "tool_call", "id": f"u{i}", "name": name,
            "args": {"skill": skill} if skill and name == "Skill" else {},
        })
        events.append({
            "type": "tool_result", "id": f"u{i}", "name": name,
            "is_error": is_err, "content": content,
        })
    rec = {
        "ts": ts.isoformat(), "turn_id": "t" + ts.isoformat()[:19],
        "session_id": "s", "saga_session_id": None,
        "trigger": trigger, "channel_id": channel_id,
        "input": "", "events": events,
        "duration_ms": duration_ms, "error": error,
    }
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def _write_event(
    path: Path, *, ts: datetime, type: str, **extra,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"timestamp": ts.isoformat(), "type": type, "session_id": "s", **extra}
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


# ─── aggregate: turn counts ────────────────────────────────────────────


def test_aggregate_groups_turns_by_trigger(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    _write_turn(turns, ts=NOW - timedelta(hours=1), trigger="user_message")
    _write_turn(turns, ts=NOW - timedelta(hours=2), trigger="user_message",
                error="boom")
    _write_turn(turns, ts=NOW - timedelta(hours=3), trigger="scheduled_tick")

    rep = aggregate(turns, events, days=7, now=NOW)
    by_trigger = {s.trigger: s for s in rep.turn_counts}
    assert by_trigger["user_message"].total_turns == 2
    assert by_trigger["user_message"].successful == 1
    assert by_trigger["scheduled_tick"].total_turns == 1
    assert by_trigger["scheduled_tick"].successful == 1


def test_aggregate_drops_old_turns(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    # Production-realistic append order: oldest-first (each turn is
    # logged at the time it happens). The reader iterates the file
    # tail newest-first and early-breaks on ts < scan_cutoff, so the
    # file must be timestamp-monotonic for the early-break to be
    # correct. (chainlink #244.)
    _write_turn(turns, ts=NOW - timedelta(days=30), trigger="user_message")
    _write_turn(turns, ts=NOW - timedelta(days=1), trigger="user_message")
    rep = aggregate(turns, events, days=7, now=NOW)
    assert rep.turn_counts[0].total_turns == 1


# ─── tool_usage / errors / recurrence ──────────────────────────────────


def test_aggregate_counts_tool_calls_and_errors(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                tool_calls=[("Read", False, ""), ("Read", False, "")])
    _write_turn(turns, ts=NOW - timedelta(hours=2),
                tool_calls=[("Read", True, "permission denied")])
    rep = aggregate(turns, events, days=7, now=NOW)
    read_usage = next(t for t in rep.tool_usage if t.tool_name == "Read")
    assert read_usage.total_calls == 3
    assert read_usage.errors == 1
    assert rep.errors_by_tool[0].tool_name == "Read"


def test_aggregate_recurring_errors_sorted_by_count(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    for i in range(3):
        _write_turn(turns, ts=NOW - timedelta(hours=i + 1),
                    tool_calls=[("Bash", True, "exit code 1: file not found")])
    _write_turn(turns, ts=NOW - timedelta(hours=4),
                tool_calls=[("Read", True, "permission denied")])
    rep = aggregate(turns, events, days=7, now=NOW)
    assert rep.error_recurrence[0].tool_name == "Bash"
    assert rep.error_recurrence[0].occurrences == 3


def test_recurring_errors_group_volatile_paths(tmp_path: Path):
    """§12.4 review #10: paths/ids/numbers should be normalized so
    'FileNotFoundError /tmp/abc' and 'FileNotFoundError /tmp/xyz'
    cluster instead of fragmenting per-call."""
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    # Five errors with different paths but same shape.
    for i, p in enumerate(["/tmp/abc", "/var/log/foo", "/home/user/x",
                           "/tmp/zzz", "/etc/y"]):
        _write_turn(turns, ts=NOW - timedelta(hours=i + 1),
                    tool_calls=[("Read", True, f"FileNotFoundError: {p}")])
    rep = aggregate(turns, events, days=7, now=NOW)
    # All 5 cluster into a single recurrence row.
    assert len(rep.error_recurrence) == 1
    assert rep.error_recurrence[0].occurrences == 5
    # Preview shows a real raw path (not the normalized form).
    assert "/" in rep.error_recurrence[0].preview


def test_recurring_errors_group_volatile_numbers(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    for i, n in enumerate([42, 100, 9999]):
        _write_turn(turns, ts=NOW - timedelta(hours=i + 1),
                    tool_calls=[("Bash", True, f"exit code {n} after timeout")])
    rep = aggregate(turns, events, days=7, now=NOW)
    assert len(rep.error_recurrence) == 1
    assert rep.error_recurrence[0].occurrences == 3


def test_recent_errors_capped_to_24h(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    _write_turn(turns, ts=NOW - timedelta(hours=2),
                tool_calls=[("Read", True, "recent")])
    _write_turn(turns, ts=NOW - timedelta(hours=48),
                tool_calls=[("Read", True, "old")])
    rep = aggregate(turns, events, days=7, now=NOW)
    assert len(rep.recent_errors) == 1
    assert rep.recent_errors[0].preview.startswith("recent")


# ─── drift ─────────────────────────────────────────────────────────────


def test_drift_started_and_stopped(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    # Current week (last 7d): Read used.
    _write_turn(turns, ts=NOW - timedelta(days=2),
                tool_calls=[("Read", False, "")])
    # Previous week (8-14d): Bash used, Read NOT used.
    _write_turn(turns, ts=NOW - timedelta(days=10),
                tool_calls=[("Bash", False, "")])
    rep = aggregate(turns, events, days=14, now=NOW)
    assert "Read" in rep.drift_started
    assert "Bash" in rep.drift_stopped


def test_drift_works_at_default_days_7(tmp_path: Path):
    """§12.4 review #1: with the default --days 7 the previous-week
    bucket was empty (records pre-cutoff were filtered before drift
    ran). Now drift extends the scan internally to 14 days."""
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    _write_turn(turns, ts=NOW - timedelta(days=2),
                tool_calls=[("Read", False, "")])
    _write_turn(turns, ts=NOW - timedelta(days=10),
                tool_calls=[("Bash", False, "")])
    rep = aggregate(turns, events, days=7, now=NOW)
    assert "Read" in rep.drift_started
    assert "Bash" in rep.drift_stopped


def test_nameless_tool_calls_excluded_from_drift(tmp_path: Path):
    """§12.4 review #15: tool_calls with missing/empty name shouldn't
    poison the drift sets with a '?' entry."""
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    # Write a turn with a manually-crafted nameless tool_call.
    import json
    rec = {
        "ts": (NOW - timedelta(hours=1)).isoformat(),
        "turn_id": "t1", "session_id": "s", "saga_session_id": None,
        "trigger": "user_message", "channel_id": "c", "input": "",
        "events": [{"type": "tool_call", "id": "u1", "name": "", "args": {}}],
        "duration_ms": 100, "error": None,
    }
    turns.parent.mkdir(parents=True, exist_ok=True)
    with turns.open("w") as f:
        f.write(json.dumps(rec) + "\n")
    rep = aggregate(turns, events, days=14, now=NOW)
    assert "?" not in rep.drift_started
    assert "?" not in rep.drift_stopped
    assert all(t.tool_name != "?" for t in rep.tool_usage)


# ─── performance trends ────────────────────────────────────────────────


def test_performance_trends_aggregated_per_day(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    base = NOW - timedelta(hours=2)
    _write_turn(turns, ts=base, trigger="user_message", duration_ms=1000)
    _write_turn(turns, ts=base + timedelta(minutes=30),
                trigger="user_message", duration_ms=3000)
    rep = aggregate(turns, events, days=7, now=NOW)
    p = next(p for p in rep.performance_trends
             if p.day == base.strftime("%Y-%m-%d") and p.trigger == "user_message")
    assert p.turns == 2
    assert p.avg_sec == 2.0
    assert p.min_sec == 1.0
    assert p.max_sec == 3.0


# ─── skill lifecycle ───────────────────────────────────────────────────


def test_skill_lifecycle_counts_skill_calls(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                tool_calls=[("Skill", False, "ok")], skill="memory")
    _write_turn(turns, ts=NOW - timedelta(hours=2),
                tool_calls=[("Skill", False, "ok"), ("Skill", False, "ok")],
                skill="memory")
    rep = aggregate(turns, events, days=7, now=NOW)
    skill_counts = dict(rep.skill_lifecycle)
    assert skill_counts["memory"] == 3


# ─── heartbeat pipeline ────────────────────────────────────────────────


def test_heartbeat_pipeline_full_picture(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    # 3 fired ticks, 2 completed, 1 with an error in the turn body.
    for i in range(3):
        _write_event(events, ts=NOW - timedelta(hours=i + 1),
                     type="scheduled_tick")
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="scheduled_tick")
    _write_turn(turns, ts=NOW - timedelta(hours=2),
                trigger="scheduled_tick", error="boom")
    # Plus 2 suppressed and 1 dropped.
    for i in range(2):
        _write_event(events, ts=NOW - timedelta(hours=i + 4),
                     type="scheduled_tick_suppressed")
    _write_event(events, ts=NOW - timedelta(hours=6),
                 type="scheduled_tick_dropped")

    rep = aggregate(turns, events, days=7, now=NOW)
    pl = rep.heartbeat
    assert pl.fired == 3
    assert pl.suppressed == 2
    assert pl.dropped == 1
    assert pl.completed == 2
    assert pl.successful == 1
    assert pl.success_rate == pytest.approx(1 / 3)


def test_heartbeat_pipeline_no_signal(tmp_path: Path):
    """No scheduled-tick activity → success_rate is None (not 0)."""
    rep = aggregate(tmp_path / "turns.jsonl", tmp_path / "events.jsonl",
                    days=7, now=NOW)
    assert rep.heartbeat.success_rate is None


# ─── algedonic emit ────────────────────────────────────────────────────


def test_emit_health_event_when_below_threshold(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    rep = Report(
        days=7, generated_at=NOW,
        heartbeat=HeartbeatPipeline(fired=10, successful=5),
    )
    emitted = maybe_emit_health_event(rep, events, threshold=0.80)
    assert emitted is True
    body = events.read_text()
    rec = json.loads(body.splitlines()[-1])
    assert rec["type"] == "heartbeat_health_degraded"
    assert rec["success_rate"] == 0.5
    assert rec["fired"] == 10


def test_no_emit_when_above_threshold(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    rep = Report(
        days=7, generated_at=NOW,
        heartbeat=HeartbeatPipeline(fired=10, successful=9),
    )
    emitted = maybe_emit_health_event(rep, events, threshold=0.80)
    assert emitted is False
    assert not events.is_file()


def test_no_emit_when_no_signal(tmp_path: Path):
    """Empty heartbeat (fired=0) is not a health failure — no signal."""
    events = tmp_path / "events.jsonl"
    rep = Report(days=7, generated_at=NOW)
    emitted = maybe_emit_health_event(rep, events, threshold=0.80)
    assert emitted is False


# ─── render ────────────────────────────────────────────────────────────


def test_render_markdown_includes_all_sections(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "events.jsonl"
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                tool_calls=[("Read", True, "permission denied")])
    _write_event(events, ts=NOW - timedelta(hours=1), type="scheduled_tick")
    _write_turn(turns, ts=NOW - timedelta(hours=1),
                trigger="scheduled_tick")
    rep = aggregate(turns, events, days=7, now=NOW)
    body = render_markdown(rep)
    assert "Event Introspection Report" in body
    assert "Turn Summary" in body
    assert "Heartbeat / scheduled-tick health" in body
    assert "Tool usage by trigger" in body
    assert "Tools with errors" in body
    assert "Recent errors" in body
    assert "Read" in body
