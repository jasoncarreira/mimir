"""Tests for §12.3 skill outcome tracking + amplification."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from mimir.skill_outcomes import (
    SkillOutcome,
    _classify_skill_calls,
    aggregate,
    order_skills,
    render_skill_telemetry,
)


def _ts(minutes_ago: float, base: datetime) -> str:
    return (base - timedelta(minutes=minutes_ago)).isoformat()


def test_classify_pairs_call_and_result():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "tool_1", "name": "task",
         "args": {"subagent_type": "memory"}},
        {"type": "tool_result", "id": "tool_1", "is_error": False, "content": "ok"},
        {"type": "tool_call", "id": "tool_2", "name": "task",
         "args": {"subagent_type": "wiki"}},
        {"type": "tool_result", "id": "tool_2", "is_error": True, "content": "boom"},
    ]
    out = list(_classify_skill_calls(events, base))
    # task() invocations always emit kind="execution" — the clean signal.
    assert out == [
        ("memory", "success", base, "execution"),
        ("wiki", "failure", base, "execution"),
    ]


def test_classify_unmatched_call_is_abandoned_when_turn_outcome_unknown():
    """No tool_result + no turn_succeeded → "abandoned" (legacy path)."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "tool_1", "name": "task",
         "args": {"subagent_type": "alert"}},
        # no matching tool_result
    ]
    out = list(_classify_skill_calls(events, base))
    assert out == [("alert", "abandoned", base, "execution")]


def test_classify_unmatched_call_infers_success_from_turn():
    """ChatClaudeCode gap: no tool_result, but turn succeeded → "success".

    Heartbeat/reflection/poller turns run via ChatClaudeCode streaming
    which never captures tool_result events for built-in Claude Code
    tools (Skill, Bash, Read, …). Without turn_succeeded, every Skill
    invocation would be "abandoned" (treated as failure). Passing
    turn_succeeded=True lets us infer the positive outcome from the
    turn's own result_is_error=False signal.
    """
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "tool_1", "name": "task",
         "args": {"subagent_type": "heartbeat"}},
        # No tool_result — ChatClaudeCode streaming gap
    ]
    out = list(_classify_skill_calls(events, base, turn_succeeded=True))
    assert out == [("heartbeat", "success", base, "execution")]


def test_classify_unmatched_call_infers_failure_from_turn():
    """No tool_result + turn errored → "failure"."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "tool_1", "name": "task",
         "args": {"subagent_type": "heartbeat"}},
    ]
    out = list(_classify_skill_calls(events, base, turn_succeeded=False))
    assert out == [("heartbeat", "failure", base, "execution")]


def test_classify_exact_result_takes_precedence_over_turn_success():
    """When a tool_result IS present, its is_error wins even if
    turn_succeeded says otherwise — exact beats inferred."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "tool_1", "name": "task",
         "args": {"subagent_type": "memory"}},
        # Explicit error result despite turn_succeeded=True
        {"type": "tool_result", "id": "tool_1", "is_error": True, "content": "boom"},
    ]
    out = list(_classify_skill_calls(events, base, turn_succeeded=True))
    assert out == [("memory", "failure", base, "execution")]


def test_classify_ignores_non_skill_tools():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "x", "name": "mcp__mimir__file_search",
         "args": {"query": "anything"}},
        {"type": "tool_result", "id": "x", "is_error": False},
    ]
    out = list(_classify_skill_calls(events, base))
    assert out == []


# ─── read_file → SKILL.md (deepagents runtime) ──────────────────────────


def test_classify_read_file_skill_md_as_load():
    """deepagents runtime: SkillsMiddleware (or mimir's _assemble_skill_block)
    tells the agent to load a skill by ``read_file`` on its SKILL.md.
    That read_file is what we count — there is no structured Skill tool
    on this runtime."""
    base = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "r1", "name": "read_file",
         "args": {"file_path": "/mimir-home/.claude/skills/threadborn/SKILL.md"}},
        {"type": "tool_result", "id": "r1", "is_error": False, "content": "..."},
    ]
    out = list(_classify_skill_calls(events, base))
    # read_file() on a SKILL.md emits kind="load" — proxy signal.
    assert out == [("threadborn", "success", base, "load")]


def test_classify_read_file_skill_md_failure():
    base = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "r1", "name": "read_file",
         "args": {"file_path": "/mimir-home/.claude/skills/threadborn/SKILL.md"}},
        {"type": "tool_result", "id": "r1", "is_error": True,
         "content": "Error: File not found"},
    ]
    out = list(_classify_skill_calls(events, base))
    assert out == [("threadborn", "failure", base, "load")]


def test_classify_read_file_tolerates_path_prefixes():
    """Match works regardless of container-absolute, relative, or
    workspace-root prefix."""
    base = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
    cases = [
        "/mimir-home/.claude/skills/moltbook/SKILL.md",
        "./.claude/skills/moltbook/SKILL.md",
        ".claude/skills/moltbook/SKILL.md",
        "/some/other/prefix/.claude/skills/moltbook/SKILL.md",
    ]
    for path in cases:
        events = [
            {"type": "tool_call", "id": "r1", "name": "read_file",
             "args": {"file_path": path}},
            {"type": "tool_result", "id": "r1", "is_error": False},
        ]
        out = list(_classify_skill_calls(events, base))
        assert out == [("moltbook", "success", base, "load")], f"path={path}"


def test_classify_read_file_non_skill_path_ignored():
    """read_file on something that isn't a SKILL.md doesn't count as a
    skill load — even within the skills tree."""
    base = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
    ignored_paths = [
        # Supporting file inside a skill dir — not the load event.
        "/mimir-home/.claude/skills/threadborn/helper.py",
        # README at the skills tree root.
        "/mimir-home/.claude/skills/README.md",
        # Memory file that happens to be SKILL.md.
        "/mimir-home/memory/topics/SKILL.md",
        # Unrelated file.
        "/mimir-home/state/today.md",
    ]
    for path in ignored_paths:
        events = [
            {"type": "tool_call", "id": "r1", "name": "read_file",
             "args": {"file_path": path}},
            {"type": "tool_result", "id": "r1", "is_error": False},
        ]
        out = list(_classify_skill_calls(events, base))
        assert out == [], f"unexpected match on {path}"


def test_classify_read_file_skill_load_falls_back_when_no_result():
    """If the agent reads SKILL.md but no tool_result lands in the same
    turn (streaming gap, agent pivoted), the fallback to turn_succeeded
    applies — same as the existing Skill-tool path."""
    base = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "r1", "name": "read_file",
         "args": {"file_path": "/mimir-home/.claude/skills/heartbeat/SKILL.md"}},
        # No tool_result.
    ]
    # Turn succeeded → infer success. The "load" kind survives the
    # fallback so per-path counters stay accurate even when the exact
    # tool_result is absent.
    out_ok = list(_classify_skill_calls(events, base, turn_succeeded=True))
    assert out_ok == [("heartbeat", "success", base, "load")]
    # Turn failed → infer failure.
    out_fail = list(_classify_skill_calls(events, base, turn_succeeded=False))
    assert out_fail == [("heartbeat", "failure", base, "load")]
    # Turn outcome unknown → abandoned.
    out_unk = list(_classify_skill_calls(events, base))
    assert out_unk == [("heartbeat", "abandoned", base, "load")]


def test_classify_both_invocation_patterns_in_same_turn():
    """A turn can carry both invocation patterns: a delegated skill
    invoked via ``task`` AND an inline skill loaded via ``read_file``.
    Both should be counted — they're not competing signals, they're
    measuring different things (execution outcome vs load outcome)."""
    base = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "s1", "name": "task",
         "args": {"subagent_type": "memory"}},
        {"type": "tool_result", "id": "s1", "is_error": False},
        {"type": "tool_call", "id": "r1", "name": "read_file",
         "args": {"file_path": ".claude/skills/threadborn/SKILL.md"}},
        {"type": "tool_result", "id": "r1", "is_error": False},
    ]
    out = list(_classify_skill_calls(events, base))
    # Both paths land in the output, each tagged with its kind so the
    # downstream aggregator can keep counters separate.
    assert ("memory", "success", base, "execution") in out
    assert ("threadborn", "success", base, "load") in out
    assert len(out) == 2


def test_aggregate_window_filters_old_turns(tmp_path):
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    in_window = {
        "ts": _ts(60, base),
        "events": [
            {"type": "tool_call", "id": "a", "name": "task",
         "args": {"subagent_type": "memory"}},
            {"type": "tool_result", "id": "a", "is_error": False},
        ],
    }
    out_of_window = {
        "ts": _ts(60 * 24 * 30, base),  # 30 days ago, outside 7d default
        "events": [
            {"type": "tool_call", "id": "b", "name": "task",
         "args": {"subagent_type": "memory"}},
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
            {"type": "tool_call", "id": "1", "name": "task",
         "args": {"subagent_type": "memory"}},
            {"type": "tool_result", "id": "1", "is_error": False},
        ]},
        {"ts": _ts(20, base), "events": [
            {"type": "tool_call", "id": "2", "name": "task",
         "args": {"subagent_type": "memory"}},
            {"type": "tool_result", "id": "2", "is_error": True},
        ]},
        {"ts": _ts(30, base), "events": [
            {"type": "tool_call", "id": "3", "name": "task",
         "args": {"subagent_type": "memory"}},
            {"type": "tool_result", "id": "3", "is_error": False},
        ]},
    ]
    turns.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    aggs = aggregate(turns, now=base)
    assert aggs["memory"].success == 2
    assert aggs["memory"].failure == 1
    assert aggs["memory"].success_rate == pytest.approx(2 / 3)


def test_aggregate_chatclaudecode_gap_infers_from_result_is_error(tmp_path):
    """Turn records with result_is_error=False but no tool_results
    (ChatClaudeCode streaming gap) should count skill invocations as
    success, not abandoned → heartbeat/reflection/github skills land
    in the proven bucket instead of risky.
    """
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    # Simulate 3 heartbeat turns: task call, no tool_result, turn ok
    records = [
        {
            "ts": _ts(60 * i, base),
            "result_is_error": False,
            "events": [
                {"type": "tool_call", "id": f"id{i}", "name": "task",
                 "args": {"subagent_type": "heartbeat"}},
                # No matching tool_result — ChatClaudeCode gap
            ],
        }
        for i in range(1, 4)
    ]
    turns.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    aggs = aggregate(turns, now=base)
    hb = aggs["heartbeat"]
    assert hb.success == 3
    assert hb.failure == 0
    assert hb.abandoned == 0


def test_aggregate_chatclaudecode_gap_failed_turn_counts_as_failure(tmp_path):
    """When result_is_error=True and no tool_result, Skill is failure."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    record = {
        "ts": _ts(30, base),
        "result_is_error": True,
        "events": [
            {"type": "tool_call", "id": "x1", "name": "task",
         "args": {"subagent_type": "heartbeat"}},
        ],
    }
    turns.write_text(json.dumps(record) + "\n")
    aggs = aggregate(turns, now=base)
    assert aggs["heartbeat"].failure == 1
    assert aggs["heartbeat"].success == 0


def test_aggregate_result_is_error_absent_falls_back_to_abandoned(tmp_path):
    """Old records without result_is_error get "abandoned" (backward compat)."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    record = {
        "ts": _ts(30, base),
        # no result_is_error field
        "events": [
            {"type": "tool_call", "id": "x1", "name": "task",
         "args": {"subagent_type": "heartbeat"}},
        ],
    }
    turns.write_text(json.dumps(record) + "\n")
    aggs = aggregate(turns, now=base)
    hb = aggs["heartbeat"]
    assert hb.abandoned == 1
    assert hb.success == 0
    assert hb.failure == 0


def test_order_skills_buckets_correctly():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    aggs = {
        "memory": SkillOutcome(skill="memory", success=8, failure=2,
                                last_used=base - timedelta(hours=1)),
        "wiki": SkillOutcome(skill="wiki", success=3, failure=4,
                              last_used=base - timedelta(hours=2)),
        # heartbeat: untried (not in aggs)
        "alert": SkillOutcome(skill="alert", success=1, failure=0,
                              last_used=base - timedelta(days=1)),
    }
    proven, untried, risky = order_skills(
        ["memory", "wiki", "alert", "heartbeat"], aggs, now=base,
    )
    # Proven sorted by success_rate desc then last_used desc.
    # memory (0.8, 1h ago) and alert (1.0, 1d ago): alert's rate higher.
    assert proven == ["alert", "memory"]
    # heartbeat had no aggregates → untried.
    assert untried == ["heartbeat"]
    # wiki: 3/(3+4) = 0.43 < 0.5 → risky.
    assert risky == ["wiki"]


def test_order_skills_proven_sorted_by_rate_then_recency():
    """Same success rate → most-recently-used wins the tie."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    aggs = {
        "old_winner": SkillOutcome(skill="old_winner", success=2, failure=0,
                                    last_used=base - timedelta(days=2)),
        "new_winner": SkillOutcome(skill="new_winner", success=2, failure=0,
                                    last_used=base - timedelta(minutes=10)),
    }
    proven, _, _ = order_skills(["old_winner", "new_winner"], aggs, now=base)
    # Same rate (1.0), new_winner used more recently → first.
    assert proven == ["new_winner", "old_winner"]


def test_render_skill_telemetry_emits_proven_and_risky_only():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    aggs = {
        "memory": SkillOutcome(skill="memory", success=5, failure=1,
                                last_used=base),
        "wiki": SkillOutcome(skill="wiki", success=1, failure=4,
                              last_used=base),
    }
    out = render_skill_telemetry(
        ["memory", "wiki", "heartbeat"], aggs, now=base,
    )
    assert out is not None
    # Proven and Risky present, with N/M counts
    assert "skills proven" in out
    assert "memory (5/6 in window)" in out
    assert "skills risky" in out
    assert "wiki (1/5 in window)" in out
    # Untried skills are NOT enumerated in telemetry — they live in
    # the install-stable catalog only
    assert "heartbeat" not in out


def test_render_skill_telemetry_returns_none_when_no_activity():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    # All seeded skills untried (no aggregates) ⇒ no telemetry
    out = render_skill_telemetry(["memory", "wiki"], {}, now=base)
    assert out is None


def test_render_skill_telemetry_returns_none_when_only_untried():
    """If all skills with aggregates are untried (zero total) the
    telemetry block is empty — both Proven and Risky are empty."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    # Aggregate exists but with zero counts ⇒ untried bucket
    aggs = {"memory": SkillOutcome(skill="memory", last_used=None)}
    out = render_skill_telemetry(["memory"], aggs, now=base)
    assert out is None
