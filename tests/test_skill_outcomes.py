"""Tests for §12.3 skill outcome tracking + amplification."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from mimir.skill_outcomes import (
    SkillOutcome,
    SkillPinConfig,
    _classify_skill_calls,
    aggregate,
    order_skills,
    render_skill_block,
)


def _ts(minutes_ago: float, base: datetime) -> str:
    return (base - timedelta(minutes=minutes_ago)).isoformat()


def test_classify_pairs_call_and_result():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "tool_1", "name": "Skill",
         "args": {"skill": "memory"}},
        {"type": "tool_result", "id": "tool_1", "is_error": False, "content": "ok"},
        {"type": "tool_call", "id": "tool_2", "name": "Skill",
         "args": {"skill": "wiki"}},
        {"type": "tool_result", "id": "tool_2", "is_error": True, "content": "boom"},
    ]
    out = list(_classify_skill_calls(events, base))
    assert out == [("memory", "success", base), ("wiki", "failure", base)]


def test_classify_unmatched_call_is_abandoned():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "tool_1", "name": "Skill",
         "args": {"skill": "alert"}},
        # no matching tool_result
    ]
    out = list(_classify_skill_calls(events, base))
    assert out == [("alert", "abandoned", base)]


def test_classify_ignores_non_skill_tools():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "x", "name": "mcp__mimir__file_search",
         "args": {"query": "anything"}},
        {"type": "tool_result", "id": "x", "is_error": False},
    ]
    out = list(_classify_skill_calls(events, base))
    assert out == []


def test_aggregate_window_filters_old_turns(tmp_path):
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    in_window = {
        "ts": _ts(60, base),
        "events": [
            {"type": "tool_call", "id": "a", "name": "Skill",
             "args": {"skill": "memory"}},
            {"type": "tool_result", "id": "a", "is_error": False},
        ],
    }
    out_of_window = {
        "ts": _ts(60 * 24 * 30, base),  # 30 days ago, outside 7d default
        "events": [
            {"type": "tool_call", "id": "b", "name": "Skill",
             "args": {"skill": "memory"}},
            {"type": "tool_result", "id": "b", "is_error": True},
        ],
    }
    turns.write_text(
        json.dumps(in_window) + "\n" + json.dumps(out_of_window) + "\n"
    )
    aggs = aggregate(turns, window_hours=24 * 7, now=base)
    assert aggs["memory"].success == 1
    assert aggs["memory"].failure == 0


def test_aggregate_accumulates_across_turns(tmp_path):
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    records = [
        {"ts": _ts(10, base), "events": [
            {"type": "tool_call", "id": "1", "name": "Skill",
             "args": {"skill": "memory"}},
            {"type": "tool_result", "id": "1", "is_error": False},
        ]},
        {"ts": _ts(20, base), "events": [
            {"type": "tool_call", "id": "2", "name": "Skill",
             "args": {"skill": "memory"}},
            {"type": "tool_result", "id": "2", "is_error": True},
        ]},
        {"ts": _ts(30, base), "events": [
            {"type": "tool_call", "id": "3", "name": "Skill",
             "args": {"skill": "memory"}},
            {"type": "tool_result", "id": "3", "is_error": False},
        ]},
    ]
    turns.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    aggs = aggregate(turns, now=base)
    assert aggs["memory"].success == 2
    assert aggs["memory"].failure == 1
    assert aggs["memory"].success_rate == pytest.approx(2 / 3)


def test_order_skills_buckets_correctly():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    aggs = {
        "memory": SkillOutcome(skill="memory", success=8, failure=2,
                                last_used=base - timedelta(hours=1)),
        "wiki": SkillOutcome(skill="wiki", success=3, failure=4,
                              last_used=base - timedelta(hours=2)),
        # heartbeat: untried (not in aggs)
        # alert: pin_top'd
        "alert": SkillOutcome(skill="alert", success=1, failure=0,
                              last_used=base - timedelta(days=1)),
    }
    pin = SkillPinConfig(pin_top=["alert"], hide=[])
    proven, untried, risky = order_skills(
        ["memory", "wiki", "alert", "heartbeat"], aggs, pin, now=base,
    )
    # Pinned-top (alert) before other proven (memory).
    assert proven == ["alert", "memory"]
    # heartbeat had no aggregates → untried.
    assert untried == ["heartbeat"]
    # wiki: 3/(3+4) = 0.43 < 0.5 → risky.
    assert risky == ["wiki"]


def test_order_skills_respects_hide():
    aggs = {"deprecated": SkillOutcome(skill="deprecated", success=5, failure=0)}
    pin = SkillPinConfig(pin_top=[], hide=["deprecated"])
    proven, untried, risky = order_skills(["deprecated", "memory"], aggs, pin)
    assert "deprecated" not in proven
    assert "deprecated" not in risky
    assert "deprecated" not in untried


def test_render_block_groups():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    aggs = {
        "memory": SkillOutcome(skill="memory", success=5, failure=1,
                                last_used=base),
        "wiki": SkillOutcome(skill="wiki", success=1, failure=4,
                              last_used=base),
    }
    pin = SkillPinConfig()
    out = render_skill_block(["memory", "wiki", "heartbeat"], aggs, pin, now=base)
    assert out is not None
    assert "**Proven**" in out
    assert "memory" in out
    assert "**Untried**" in out
    assert "heartbeat" in out
    assert "**Risky**" in out
    assert "wiki" in out


def test_render_block_returns_none_when_no_seeded():
    out = render_skill_block([], {}, SkillPinConfig())
    assert out is None


def test_pin_config_loads_yaml(tmp_path):
    yaml_path = tmp_path / "skill-pin.yaml"
    yaml_path.write_text("pin_top:\n  - memory\n  - wiki\nhide:\n  - legacy\n")
    pin = SkillPinConfig.load(yaml_path)
    assert pin.pin_top == ["memory", "wiki"]
    assert pin.hide == ["legacy"]


def test_pin_config_missing_file_returns_empty(tmp_path):
    pin = SkillPinConfig.load(tmp_path / "missing.yaml")
    assert pin.pin_top == []
    assert pin.hide == []
