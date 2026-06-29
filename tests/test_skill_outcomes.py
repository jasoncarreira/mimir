"""Tests for §12.3 skill outcome tracking + amplification."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from mimir.skill_outcomes import (
    SkillOutcome,
    SkillSuccessCriteria,
    _classify_skill_calls,
    _parse_criteria_from_skill_md,
    aggregate,
    load_skill_success_criteria,
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
    applies — same as the existing Skill-tool path.

    This is the empty-boundary-window case (no events at all after the
    load) so Approach C's window scan finds nothing and the terminal
    turn_succeeded fallback fires."""
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


def test_classify_approach_c_localizes_error_to_boundary_window():
    """Approach C: when two inline skills load in the same turn and a
    tool_result error occurs in skill A's boundary window (before B
    loads), only A is attributed failure. B's window is clean so B
    yields success — even when turn_succeeded=False.

    Approach A would give both A and B "failure" from the whole-turn
    signal. Approach C narrows attribution to the inter-skill-load
    window, preventing false-blame propagation."""
    base = datetime(2026, 5, 22, tzinfo=timezone.utc)
    events = [
        # Skill A loads — no tool_result (streaming gap)
        {"type": "tool_call", "id": "rA", "name": "read_file",
         "args": {"file_path": "/h/.mimir_builtin_skills/alert/SKILL.md"}},
        # Error in A's boundary window (before B loads)
        {"type": "tool_call", "id": "oA", "name": "send_message",
         "args": {"channel_id": "x"}},
        {"type": "tool_result", "id": "oA", "is_error": True,
         "content": "connection refused"},
        # Skill B loads — no tool_result (streaming gap)
        {"type": "tool_call", "id": "rB", "name": "read_file",
         "args": {"file_path": "/h/.mimir_builtin_skills/wiki/SKILL.md"}},
        # Clean result in B's boundary window
        {"type": "tool_call", "id": "oB", "name": "write_file",
         "args": {"file_path": "state/wiki/foo.md"}},
        {"type": "tool_result", "id": "oB", "is_error": False, "content": "ok"},
    ]
    # Turn failed overall — Approach A would blame both; Approach C localizes.
    out = list(_classify_skill_calls(events, base, turn_succeeded=False))
    outcomes = {skill: outcome for skill, outcome, _, _ in out}
    assert outcomes["alert"] == "failure"   # A's window has error
    assert outcomes["wiki"] == "success"   # B's window is clean


def test_classify_approach_c_clean_window_overrides_failed_turn():
    """Approach C: a single skill whose boundary window has only clean
    tool_results yields 'success' even when turn_succeeded=False.

    This covers the common case where the agent loads one inline skill,
    the skill's work succeeds (tool_result is_error=False), but then the
    turn fails for an unrelated reason AFTER the window closes (e.g. a
    later, non-skill tool_call errors out)."""
    base = datetime(2026, 5, 22, tzinfo=timezone.utc)
    events = [
        # Skill A loads — no tool_result (streaming gap)
        {"type": "tool_call", "id": "rA", "name": "read_file",
         "args": {"file_path": "/h/.mimir_builtin_skills/memory/SKILL.md"}},
        # Clean result in skill's boundary window
        {"type": "tool_call", "id": "m1", "name": "memory_store",
         "args": {"tier": "ATOMIC", "text": "some atom"}},
        {"type": "tool_result", "id": "m1", "is_error": False},
    ]
    # Turn "failed" but only because of something after the skill window
    out = list(_classify_skill_calls(events, base, turn_succeeded=False))
    assert out == [("memory", "success", base, "load")]


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


def test_aggregate_treats_naive_turn_timestamp_as_utc(tmp_path):
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    record = {
        # Older or hand-authored turn records may omit the UTC offset. These
        # still represent UTC turns and should not crash the whole aggregate.
        "ts": "2026-05-02T11:55:00",
        "events": [
            {"type": "tool_call", "id": "1", "name": "task",
             "args": {"subagent_type": "memory"}},
            {"type": "tool_result", "id": "1", "is_error": False},
        ],
    }
    turns.write_text(json.dumps(record) + "\n")

    aggs = aggregate(turns, now=base)

    assert aggs["memory"].success == 1
    assert aggs["memory"].failure == 0


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


# ─── success_criteria refinement ─────────────────────────────────────────


def _read_file_event(skill: str, idx: int = 1, is_error: bool = False) -> list[dict]:
    """Helper: a read_file(SKILL.md) call + tool_result pair."""
    return [
        {"type": "tool_call", "id": f"r{idx}", "name": "read_file",
         "args": {"file_path": f"/h/.mimir_builtin_skills/{skill}/SKILL.md"}},
        {"type": "tool_result", "id": f"r{idx}", "is_error": is_error},
    ]


def test_classify_load_with_criteria_met_yields_success():
    """A load whose skill has success_criteria, with a matching event
    after the load in the same turn, stays classified as success."""
    base = datetime(2026, 5, 22, tzinfo=timezone.utc)
    criteria = {
        "alert": SkillSuccessCriteria(
            any_of=[{"tool_call": {"name": "send_message"}}],
        ),
    }
    events = _read_file_event("alert") + [
        {"type": "tool_call", "id": "s1", "name": "send_message",
         "args": {"channel_id": "operator", "text": "wake up"}},
        {"type": "tool_result", "id": "s1", "is_error": False},
    ]
    out = list(_classify_skill_calls(events, base, skill_criteria=criteria))
    assert out == [("alert", "success", base, "load")]


def test_classify_load_with_criteria_unmet_yields_incomplete():
    """Load succeeded but the operator's success_criteria found no
    matching event in the tail → ``incomplete`` (not failure).
    Operators see this distinct from "the file errored" so they can
    investigate procedure drift vs broken skills separately."""
    base = datetime(2026, 5, 22, tzinfo=timezone.utc)
    criteria = {
        "alert": SkillSuccessCriteria(
            any_of=[{"tool_call": {"name": "send_message"}}],
        ),
    }
    events = _read_file_event("alert") + [
        {"type": "tool_call", "id": "w1", "name": "write_file",
         "args": {"file_path": "state/notes.md"}},
        {"type": "tool_result", "id": "w1", "is_error": False},
    ]
    out = list(_classify_skill_calls(events, base, skill_criteria=criteria))
    assert out == [("alert", "incomplete", base, "load")]


def test_classify_load_failure_skips_criteria_check():
    """If the load itself errored (is_error=True), don't run criteria
    — there's no procedure to check. Stays as ``failure``."""
    base = datetime(2026, 5, 22, tzinfo=timezone.utc)
    criteria = {
        "alert": SkillSuccessCriteria(
            any_of=[{"tool_call": {"name": "send_message"}}],
        ),
    }
    events = _read_file_event("alert", is_error=True)
    out = list(_classify_skill_calls(events, base, skill_criteria=criteria))
    assert out == [("alert", "failure", base, "load")]


def test_classify_load_with_no_criteria_for_skill_yields_success():
    """A skill not in the criteria map keeps the legacy load-only
    signal — backward compat for skills that haven't been audited."""
    base = datetime(2026, 5, 22, tzinfo=timezone.utc)
    criteria = {
        "alert": SkillSuccessCriteria(
            any_of=[{"tool_call": {"name": "send_message"}}],
        ),
    }
    events = _read_file_event("threadborn")
    out = list(_classify_skill_calls(events, base, skill_criteria=criteria))
    assert out == [("threadborn", "success", base, "load")]


def test_classify_execution_kind_ignores_criteria():
    """task() invocations already have a clean signal via
    tool_result.is_error. Criteria don't apply — even if the skill
    has a criteria block, an execution-path outcome stays raw."""
    base = datetime(2026, 5, 22, tzinfo=timezone.utc)
    criteria = {
        "weather": SkillSuccessCriteria(
            # Deliberately impossible to match, to prove it's ignored.
            any_of=[{"tool_call": {"name": "never_called"}}],
        ),
    }
    events = [
        {"type": "tool_call", "id": "t1", "name": "task",
         "args": {"subagent_type": "weather"}},
        {"type": "tool_result", "id": "t1", "is_error": False},
    ]
    out = list(_classify_skill_calls(events, base, skill_criteria=criteria))
    assert out == [("weather", "success", base, "execution")]


def test_classify_criteria_args_subset_match():
    """Pattern with ``args`` matches when every declared key equals
    the event's value; the event may carry extra args not in the
    pattern."""
    base = datetime(2026, 5, 22, tzinfo=timezone.utc)
    criteria = {
        "memory": SkillSuccessCriteria(
            any_of=[{
                "tool_call": {
                    "name": "memory_store",
                    "args": {"tier": "ATOMIC"},
                },
            }],
        ),
    }
    # Matching: tier matches; other args ignored
    events_ok = _read_file_event("memory") + [
        {"type": "tool_call", "id": "m1", "name": "memory_store",
         "args": {"tier": "ATOMIC", "session_id": "abc", "text": "x"}},
        {"type": "tool_result", "id": "m1", "is_error": False},
    ]
    out_ok = list(_classify_skill_calls(events_ok, base, skill_criteria=criteria))
    assert out_ok[0][1] == "success"

    # Mismatching: tier different
    events_drift = _read_file_event("memory") + [
        {"type": "tool_call", "id": "m1", "name": "memory_store",
         "args": {"tier": "CORE"}},
        {"type": "tool_result", "id": "m1", "is_error": False},
    ]
    out_drift = list(_classify_skill_calls(events_drift, base, skill_criteria=criteria))
    assert out_drift[0][1] == "incomplete"


def test_aggregate_threads_criteria_through_to_classifier(tmp_path):
    """End-to-end: aggregate() respects skill_criteria — the incomplete
    counter increments for loads that don't trigger any criteria."""
    base = datetime(2026, 5, 22, tzinfo=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    drift_turn = {
        "ts": _ts(60, base),
        "events": _read_file_event("alert") + [
            {"type": "tool_call", "id": "w1", "name": "write_file",
             "args": {"file_path": "state/notes.md"}},
            {"type": "tool_result", "id": "w1", "is_error": False},
        ],
    }
    turns.write_text(json.dumps(drift_turn) + "\n")
    criteria = {
        "alert": SkillSuccessCriteria(
            any_of=[{"tool_call": {"name": "send_message"}}],
        ),
    }
    aggs = aggregate(turns, now=base, skill_criteria=criteria)
    alert = aggs["alert"]
    assert alert.incomplete == 1
    assert alert.load_incomplete == 1
    assert alert.success == 0
    assert alert.total == 1
    # success_rate folds incomplete into the denominator as not-success
    assert alert.success_rate == 0.0


# ─── frontmatter loader ──────────────────────────────────────────────────


def test_parse_criteria_from_skill_md_returns_none_without_block(tmp_path: Path):
    """A SKILL.md with no ``success_criteria`` field returns None
    (signal: "no criteria to check, trust the load signal")."""
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: bare\ndescription: x\nallowed-tools: [Bash]\n---\nbody\n"
    )
    assert _parse_criteria_from_skill_md(path) is None


def test_parse_criteria_from_skill_md_extracts_any_of(tmp_path: Path):
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\n"
        "name: alert\n"
        "description: x\n"
        "success_criteria:\n"
        "  any_of:\n"
        "    - tool_call:\n"
        "        name: send_message\n"
        "    - tool_call:\n"
        "        name: write_file\n"
        "---\nbody\n"
    )
    c = _parse_criteria_from_skill_md(path)
    assert c is not None
    assert len(c.any_of) == 2
    assert c.any_of[0]["tool_call"]["name"] == "send_message"


def test_load_skill_success_criteria_scans_both_dirs(tmp_path: Path):
    """Operator-installed skills under ``<home>/skills/`` shadow
    bundled same-named entries (matches the rest of the dual-location
    contract)."""
    bundled = tmp_path / ".mimir_builtin_skills"
    operator = tmp_path / "skills"
    (bundled / "alert").mkdir(parents=True)
    (bundled / "alert" / "SKILL.md").write_text(
        "---\nname: alert\ndescription: x\n"
        "success_criteria:\n  any_of:\n    - tool_call: {name: send_message}\n"
        "---\nbody\n"
    )
    (operator / "alert").mkdir(parents=True)
    (operator / "alert" / "SKILL.md").write_text(
        "---\nname: alert\ndescription: x\n"
        "success_criteria:\n  any_of:\n    - tool_call: {name: OPERATOR_OVERRIDE}\n"
        "---\nbody\n"
    )
    crits = load_skill_success_criteria(tmp_path)
    assert "alert" in crits
    # Operator entry wins on collision.
    assert crits["alert"].any_of[0]["tool_call"]["name"] == "OPERATOR_OVERRIDE"


def test_load_skill_success_criteria_real_bundle(tmp_path: Path):
    """Smoke: alert and memory SKILL.md from the bundle parse and
    expose their declared criteria via the loader."""
    from mimir.skill_defs import refresh_builtin_skills
    refresh_builtin_skills(tmp_path)
    crits = load_skill_success_criteria(tmp_path)
    assert "alert" in crits, "alert SKILL.md should declare success_criteria"
    assert "memory" in crits, "memory SKILL.md should declare success_criteria"
    # alert: send_message; memory: memory_store / memory_query / saga_end_session
    assert any(
        p["tool_call"]["name"] == "send_message"
        for p in crits["alert"].any_of
    )
    memory_names = {p["tool_call"]["name"] for p in crits["memory"].any_of}
    assert {"memory_store", "memory_query"} <= memory_names


def test_classify_criteria_args_glob_match():
    """``<key>_glob`` triggers fnmatch on ``event.args[<key>]`` —
    used for file-path matching where exact equality would be too
    strict (operators don't know the exact path the agent will pick)."""
    base = datetime(2026, 5, 22, tzinfo=timezone.utc)
    criteria = {
        "wiki": SkillSuccessCriteria(
            any_of=[{
                "tool_call": {
                    "name": "write_file",
                    "args": {"file_path_glob": "*state/wiki/*"},
                },
            }],
        ),
    }
    # On-target: path matches the glob → success.
    events_ok = _read_file_event("wiki") + [
        {"type": "tool_call", "id": "w1", "name": "write_file",
         "args": {"file_path": "/h/state/wiki/entities/Foo.md", "content": "x"}},
        {"type": "tool_result", "id": "w1", "is_error": False},
    ]
    out_ok = list(_classify_skill_calls(events_ok, base, skill_criteria=criteria))
    assert out_ok[0][1] == "success"

    # Off-target: path doesn't match the glob → incomplete.
    events_drift = _read_file_event("wiki") + [
        {"type": "tool_call", "id": "w1", "name": "write_file",
         "args": {"file_path": "/h/state/notes.md", "content": "x"}},
        {"type": "tool_result", "id": "w1", "is_error": False},
    ]
    out_drift = list(_classify_skill_calls(events_drift, base, skill_criteria=criteria))
    assert out_drift[0][1] == "incomplete"


def test_load_skill_success_criteria_wiki_introspection_pollers(tmp_path: Path):
    """Bundle-wide smoke: wiki + introspection + pollers all expose
    their declared criteria via the loader (next batch of skills
    audited after alert + memory)."""
    from mimir.skill_defs import refresh_builtin_skills
    refresh_builtin_skills(tmp_path)
    crits = load_skill_success_criteria(tmp_path)
    for name in ("wiki", "introspection", "pollers"):
        assert name in crits, f"{name} should declare success_criteria"
        assert crits[name].any_of, f"{name} criteria.any_of is empty"
    # Spot-check the path-glob form landed for the file-shaped checks.
    wiki_globs = [
        p["tool_call"].get("args", {}).get("file_path_glob")
        for p in crits["wiki"].any_of
        if "args" in p["tool_call"]
    ]
    assert any(g and "state/wiki" in g for g in wiki_globs), (
        "wiki criteria should target state/wiki/ paths"
    )
