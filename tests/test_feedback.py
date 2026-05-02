"""v0.4 §2: algedonic surfacing.

FeedbackLog reads logs/events.jsonl + logs/turns.jsonl tail-first,
classifies records by polarity, renders a prompt block."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mimir.feedback import (
    FeedbackLog,
    FeedbackSignal,
    render_feedback_block,
)


def _ts(hours_ago: float = 0) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _make_log(tmp_path: Path, events: list[dict] | None = None,
              turns: list[dict] | None = None) -> FeedbackLog:
    events_path = tmp_path / "logs" / "events.jsonl"
    turns_path = tmp_path / "logs" / "turns.jsonl"
    if events is not None:
        _write_jsonl(events_path, events)
    if turns is not None:
        _write_jsonl(turns_path, turns)
    return FeedbackLog(events_path=events_path, turns_path=turns_path)


# ---- Empty / missing files ----------------------------------------------


def test_recent_block_returns_none_when_logs_missing(tmp_path: Path):
    log = FeedbackLog(
        events_path=tmp_path / "logs" / "events.jsonl",
        turns_path=tmp_path / "logs" / "turns.jsonl",
    )
    assert log.recent_block() is None


def test_recent_block_returns_none_when_logs_have_nothing_relevant(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[{"timestamp": _ts(0.5), "type": "event_queued", "channel_id": "x"}],
    )
    assert log.recent_block() is None


# ---- Polarity rendering --------------------------------------------------


def test_negative_only_signals_render(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[
            {
                "timestamp": _ts(0.1),
                "type": "tool_call_denied",
                "tool": "file_search",
                "reason": "budget exhausted",
                "channel_id": "slack-eng",
            }
        ],
    )
    block = log.recent_block()
    assert block is not None
    assert "Negative (last 24h):" in block
    assert "Positive" not in block
    assert "tool_denied file_search" in block
    assert "budget exhausted" in block
    assert "[slack-eng]" in block


def test_positive_only_signals_render(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[
            {
                "timestamp": _ts(0.5),
                "type": "saga_feedback_sent",
                "n_atoms": 3,
                "channel_id": "slack-eng",
            }
        ],
    )
    block = log.recent_block()
    assert block is not None
    assert "Positive (last 24h):" in block
    assert "Negative" not in block
    assert "saga_feedback_sent (3 atoms credited)" in block


def test_mixed_polarity_renders_both_subsections_with_blank_separator(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[
            {"timestamp": _ts(0.1), "type": "saga_feedback_sent", "n_atoms": 2,
             "channel_id": "slack-eng"},
            {"timestamp": _ts(0.2), "type": "tool_call_denied", "tool": "saga_query",
             "reason": "rate-limited", "channel_id": "discord-99"},
        ],
    )
    block = log.recent_block()
    assert block is not None
    assert "Negative (last 24h):" in block
    assert "Positive (last 24h):" in block
    # Negative comes first; subsections separated by a blank line for
    # readability.
    neg_idx = block.index("Negative")
    pos_idx = block.index("Positive")
    assert neg_idx < pos_idx
    assert "\n\nPositive" in block


# ---- Window cutoff -------------------------------------------------------


def test_records_outside_window_are_dropped(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[
            # JSONL is appended chronologically — oldest event first.
            {"timestamp": _ts(48), "type": "tool_call_denied", "tool": "y",
             "channel_id": "c2"},
            {"timestamp": _ts(1), "type": "tool_call_denied", "tool": "x",
             "channel_id": "c1"},
        ],
    )
    block = log.recent_block(window_hours=24)
    assert block is not None
    assert "tool_denied x" in block
    assert "tool_denied y" not in block


def test_short_window_excludes_recent_but_older_records(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[
            {"timestamp": _ts(2), "type": "tool_call_denied", "tool": "stale",
             "channel_id": "c"},
            {"timestamp": _ts(0.1), "type": "tool_call_denied", "tool": "fresh",
             "channel_id": "c"},
        ],
    )
    block = log.recent_block(window_hours=1)
    assert block is not None
    assert "fresh" in block
    assert "stale" not in block


# ---- Per-polarity cap ----------------------------------------------------


def test_per_polarity_cap_truncates(tmp_path: Path):
    events = [
        {"timestamp": _ts(0.01 * i), "type": "tool_call_denied", "tool": f"t{i}",
         "channel_id": "c"}
        for i in range(10)
    ]
    log = _make_log(tmp_path, events=events)
    negatives, _ = log.recent(limit_per_polarity=3)
    assert len(negatives) == 3


# ---- turns.jsonl integration --------------------------------------------


def test_turn_errors_surface_as_negative_signals(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[],
        turns=[
            {
                "ts": _ts(0.5),
                "turn_id": "abc",
                "channel_id": "slack-eng",
                "error": "TimeoutError: query() crashed",
                "result_is_error": True,
            }
        ],
    )
    block = log.recent_block()
    assert block is not None
    assert "Negative" in block
    assert "turn error: TimeoutError" in block


def test_turn_records_without_error_are_ignored(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[],
        turns=[
            {"ts": _ts(0.5), "turn_id": "abc", "channel_id": "x",
             "error": None, "result_is_error": False}
        ],
    )
    assert log.recent_block() is None


# ---- Channel scoping (no scoping — feedback is global) ------------------


def test_feedback_is_global_not_channel_scoped(tmp_path: Path):
    """A turn for slack-eng should still see signals from discord-99 —
    the whole point is self-feedback across channels."""
    log = _make_log(
        tmp_path,
        events=[
            {"timestamp": _ts(0.5), "type": "tool_call_denied", "tool": "x",
             "channel_id": "discord-99"},
            {"timestamp": _ts(0.6), "type": "saga_feedback_sent", "n_atoms": 1,
             "channel_id": "slack-eng"},
        ],
    )
    block = log.recent_block()
    assert block is not None
    # Both surface in the same block.
    assert "[discord-99]" in block
    assert "[slack-eng]" in block


# ---- Renderer direct -----------------------------------------------------


def test_render_feedback_block_returns_none_for_empty_inputs():
    assert render_feedback_block([], []) is None


def test_render_feedback_block_uses_window_hours_in_header():
    sig = FeedbackSignal(
        ts="2026-05-01T12:00:00+00:00",
        polarity="negative",
        kind="error",
        channel_id="x",
        content="error in foo: bar",
    )
    block = render_feedback_block([sig], [], window_hours=72)
    assert block is not None
    assert "Negative (last 72h):" in block
    assert "2026-05-01 12:00" in block


def test_heartbeat_health_degraded_renders_in_feedback(tmp_path: Path):
    log = _make_log(tmp_path, events=[
        {
            "timestamp": _ts(0.5),
            "type": "heartbeat_health_degraded",
            "session_id": "introspection-report",
            "success_rate": 0.25,
            "threshold": 0.80,
            "fired": 4,
            "successful": 1,
        },
    ])
    block = log.recent_block()
    assert block is not None
    assert "heartbeat pipeline degraded" in block
    assert "25%" in block
    assert "1/4 fired" in block


def test_saga_consolidate_ok_renders_as_positive(tmp_path: Path):
    log = _make_log(tmp_path, events=[
        {
            "timestamp": _ts(0.5),
            "type": "saga_consolidate_ok",
            "session_id": "s",
            "dry_run": False,
            "result": {
                "clusters_processed": 4,
                "atoms_merged": 7,
                "atoms_retired": 2,
                "duration_s": 12.4,
            },
        },
    ])
    block = log.recent_block()
    assert block is not None
    assert "Positive" in block
    assert "saga consolidation ran" in block
    assert "4 clusters" in block
    assert "7 merged" in block
    assert "2 retired" in block


def test_introspection_report_ok_surfaces_output_path(tmp_path: Path):
    """The agent should see the report's file path so it can Read it."""
    log = _make_log(tmp_path, events=[
        {
            "timestamp": _ts(0.5),
            "type": "introspection_report_ok",
            "session_id": "s",
            "output": "/home/agent/state/reports/introspection-2026-05-02.md",
            "days": 7,
            "pipeline_success_rate": 0.92,
            "fired": 50,
            "successful": 46,
            "algedonic_emitted": False,
        },
    ])
    block = log.recent_block()
    assert block is not None
    assert "Positive" in block
    assert "introspection report ready" in block
    assert "/home/agent/state/reports/introspection-2026-05-02.md" in block
    assert "92%" in block


def test_introspection_report_error_renders_as_negative(tmp_path: Path):
    log = _make_log(tmp_path, events=[
        {
            "timestamp": _ts(0.5),
            "type": "introspection_report_error",
            "session_id": "s",
            "error": "OSError: events.jsonl missing",
        },
    ])
    block = log.recent_block()
    assert block is not None
    assert "Negative" in block
    assert "introspection report failed" in block
    assert "events.jsonl missing" in block


def test_scheduled_tick_suppressed_renders_in_feedback(tmp_path: Path):
    log = _make_log(tmp_path, events=[
        {
            "timestamp": _ts(0.5),
            "type": "scheduled_tick_suppressed",
            "session_id": "s",
            "reason": "plan_window_saturated:7d_opus@0.92",
        },
    ])
    block = log.recent_block()
    assert block is not None
    assert "suppressed by arbiter" in block
    assert "plan_window_saturated" in block
