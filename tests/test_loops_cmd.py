"""Tests for ``mimir loops`` CLI (FUTURE_WORK §12.6b)."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from mimir.loops_cmd import (
    LoopStatus,
    _measure_runtime,
    collect_loops,
    render_table,
)
from mimir.loop_inventory import LoopTag


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _ts(minutes_ago: float, base: datetime) -> str:
    return (base - timedelta(minutes=minutes_ago)).isoformat()


def test_measure_runtime_no_events_file(tmp_path):
    last, vol = _measure_runtime(
        tmp_path / "missing.jsonl",
        ["saga_feedback_sent"],
        now=datetime.now(tz=timezone.utc),
    )
    assert last is None
    assert vol == 0


def test_measure_runtime_counts_24h_window(tmp_path):
    now = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = tmp_path / "events.jsonl"
    _write_events(events, [
        {"timestamp": _ts(60, now), "type": "saga_feedback_sent"},     # in window
        {"timestamp": _ts(120, now), "type": "saga_feedback_sent"},    # in window
        {"timestamp": _ts(60 * 26, now), "type": "saga_feedback_sent"},  # outside window
        {"timestamp": _ts(30, now), "type": "other_event"},            # not our type
    ])
    last, vol = _measure_runtime(events, ["saga_feedback_sent"], now=now)
    assert vol == 2
    assert last is not None
    # Last fire = the most recent matching event = 60 min ago.
    assert (now - last).total_seconds() == pytest.approx(3600, abs=2)


def test_measure_runtime_multi_type(tmp_path):
    """When multiple event types are listed (e.g. tool_call_denied
    + tool_call_budget_warning both signal 1.4), volume sums across
    types."""
    now = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = tmp_path / "events.jsonl"
    _write_events(events, [
        {"timestamp": _ts(15, now), "type": "tool_call_denied"},
        {"timestamp": _ts(30, now), "type": "tool_call_budget_warning"},
        {"timestamp": _ts(45, now), "type": "tool_call_budget_warning"},
    ])
    _, vol = _measure_runtime(
        events,
        ["tool_call_denied", "tool_call_budget_warning"],
        now=now,
    )
    assert vol == 3


def test_collect_loops_assigns_status_correctly(tmp_path, monkeypatch):
    """Three buckets: healthy (24h volume>0), idle (fired before but
    not in 24h), never-fired (no record at all)."""
    now = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = tmp_path / "logs" / "events.jsonl"
    _write_events(events, [
        {"timestamp": _ts(15, now), "type": "saga_feedback_sent"},     # 1.1 healthy
        {"timestamp": _ts(60 * 30, now), "type": "react_received"},     # 2.6 idle (>24h)
        # 2.2 never-fired
    ])

    fake_tags = [
        LoopTag(file=Path("a.py"), line=1, layer="S3",
                description="post-turn credit",
                loop_id="1.1", target="def x():"),
        LoopTag(file=Path("b.py"), line=1, layer="algedonic (in)",
                description="inbound reactions",
                loop_id="2.6", target="def y():"),
        LoopTag(file=Path("c.py"), line=1, layer="S3* (cross-session)",
                description="session boundaries",
                loop_id="2.2", target="def z():"),
    ]
    monkeypatch.setattr("mimir.loops_cmd.scan_inventory", lambda roots: fake_tags)

    rows = collect_loops(events, now=now)
    by_id = {r.loop_id: r for r in rows}
    assert by_id["1.1"].status == "healthy"
    assert by_id["1.1"].volume_24h == 1
    assert by_id["2.6"].status == "idle"  # fired 30h ago
    assert by_id["2.6"].volume_24h == 0
    assert by_id["2.2"].status == "never-fired"


def test_collect_loops_groups_multi_site_tags(monkeypatch, tmp_path):
    """Loop 2.6 has two sites (Discord + Slack bridges). Should
    collapse to one row in the output."""
    fake_tags = [
        LoopTag(file=Path("discord.py"), line=1, layer="algedonic (in)",
                description="discord _on_reaction", loop_id="2.6",
                target="async def _on_reaction(self, payload):"),
        LoopTag(file=Path("slack.py"), line=1, layer="algedonic (in)",
                description="slack _on_reaction", loop_id="2.6",
                target="async def _on_reaction(self, event):"),
    ]
    monkeypatch.setattr("mimir.loops_cmd.scan_inventory", lambda roots: fake_tags)

    rows = collect_loops(tmp_path / "events.jsonl")
    assert len(rows) == 1
    assert rows[0].loop_id == "2.6"
    assert len(rows[0].sites) == 2


def test_render_table_lists_silences_in_footer(monkeypatch, tmp_path):
    """When loops are never-fired, the table should call them out
    in a footer — that's the core diagnostic value of this command."""
    fake_tags = [
        LoopTag(file=Path("a.py"), line=1, layer="algedonic (in)",
                description="inbound reactions", loop_id="2.6",
                target="def react():"),
    ]
    monkeypatch.setattr("mimir.loops_cmd.scan_inventory", lambda roots: fake_tags)
    rows = collect_loops(tmp_path / "missing.jsonl")
    out = render_table(rows)
    assert "never-fired" in out
    assert "2.6" in out
    assert "react_received" in out  # the expected event listed in footer


def test_render_table_orders_by_vsm_layer(monkeypatch, tmp_path):
    """S1 < S2 < S3 < S3* < S4 < S5 < algedonic, then alphabetic
    on the loop_id within each layer."""
    fake_tags = [
        LoopTag(file=Path("a"), line=1, layer="S4", description="x",
                loop_id="4.1", target="def a():"),
        LoopTag(file=Path("b"), line=1, layer="S2", description="y",
                loop_id="1.3", target="def b():"),
        LoopTag(file=Path("c"), line=1, layer="algedonic", description="z",
                loop_id="2.1", target="def c():"),
        LoopTag(file=Path("d"), line=1, layer="S3", description="w",
                loop_id="1.1", target="def d():"),
    ]
    monkeypatch.setattr("mimir.loops_cmd.scan_inventory", lambda roots: fake_tags)
    rows = collect_loops(tmp_path / "events.jsonl")
    out = render_table(rows)

    s2_pos = out.index("1.3")
    s3_pos = out.index("1.1")
    s4_pos = out.index("4.1")
    alg_pos = out.index("2.1")
    assert s2_pos < s3_pos < s4_pos < alg_pos
