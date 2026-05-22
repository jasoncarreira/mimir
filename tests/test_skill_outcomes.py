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
    render_skill_catalog,
    render_skill_telemetry,
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


def test_classify_unmatched_call_is_abandoned_when_turn_outcome_unknown():
    """No tool_result + no turn_succeeded → "abandoned" (legacy path)."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "tool_1", "name": "Skill",
         "args": {"skill": "alert"}},
        # no matching tool_result
    ]
    out = list(_classify_skill_calls(events, base))
    assert out == [("alert", "abandoned", base)]


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
        {"type": "tool_call", "id": "tool_1", "name": "Skill",
         "args": {"skill": "heartbeat"}},
        # No tool_result — ChatClaudeCode streaming gap
    ]
    out = list(_classify_skill_calls(events, base, turn_succeeded=True))
    assert out == [("heartbeat", "success", base)]


def test_classify_unmatched_call_infers_failure_from_turn():
    """No tool_result + turn errored → "failure"."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "tool_1", "name": "Skill",
         "args": {"skill": "heartbeat"}},
    ]
    out = list(_classify_skill_calls(events, base, turn_succeeded=False))
    assert out == [("heartbeat", "failure", base)]


def test_classify_exact_result_takes_precedence_over_turn_success():
    """When a tool_result IS present, its is_error wins even if
    turn_succeeded says otherwise — exact beats inferred."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "tool_1", "name": "Skill",
         "args": {"skill": "memory"}},
        # Explicit error result despite turn_succeeded=True
        {"type": "tool_result", "id": "tool_1", "is_error": True, "content": "boom"},
    ]
    out = list(_classify_skill_calls(events, base, turn_succeeded=True))
    assert out == [("memory", "failure", base)]


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
    assert out == [("threadborn", "success", base)]


def test_classify_read_file_skill_md_failure():
    base = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "r1", "name": "read_file",
         "args": {"file_path": "/mimir-home/.claude/skills/threadborn/SKILL.md"}},
        {"type": "tool_result", "id": "r1", "is_error": True,
         "content": "Error: File not found"},
    ]
    out = list(_classify_skill_calls(events, base))
    assert out == [("threadborn", "failure", base)]


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
        assert out == [("moltbook", "success", base)], f"path={path}"


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
    # Turn succeeded → infer success.
    out_ok = list(_classify_skill_calls(events, base, turn_succeeded=True))
    assert out_ok == [("heartbeat", "success", base)]
    # Turn failed → infer failure.
    out_fail = list(_classify_skill_calls(events, base, turn_succeeded=False))
    assert out_fail == [("heartbeat", "failure", base)]
    # Turn outcome unknown → abandoned.
    out_unk = list(_classify_skill_calls(events, base))
    assert out_unk == [("heartbeat", "abandoned", base)]


def test_classify_dual_runtime_in_same_turn():
    """If both runtimes' patterns appear in one turn (e.g. mixed
    pre/post-migration log), both count. Last-source-wins is not the
    semantic; both are real load events."""
    base = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
    events = [
        {"type": "tool_call", "id": "s1", "name": "Skill",
         "args": {"skill": "memory"}},
        {"type": "tool_result", "id": "s1", "is_error": False},
        {"type": "tool_call", "id": "r1", "name": "read_file",
         "args": {"file_path": ".claude/skills/threadborn/SKILL.md"}},
        {"type": "tool_result", "id": "r1", "is_error": False},
    ]
    out = list(_classify_skill_calls(events, base))
    assert ("memory", "success", base) in out
    assert ("threadborn", "success", base) in out
    assert len(out) == 2


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


def test_aggregate_chatclaudecode_gap_infers_from_result_is_error(tmp_path):
    """Turn records with result_is_error=False but no tool_results
    (ChatClaudeCode streaming gap) should count Skill invocations as
    success, not abandoned → heartbeat/reflection/github skills land
    in the proven bucket instead of risky.
    """
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    # Simulate 3 heartbeat turns: Skill call, no tool_result, turn ok
    records = [
        {
            "ts": _ts(60 * i, base),
            "result_is_error": False,
            "events": [
                {"type": "tool_call", "id": f"id{i}", "name": "Skill",
                 "args": {"skill": "heartbeat"}},
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
            {"type": "tool_call", "id": "x1", "name": "Skill",
             "args": {"skill": "heartbeat"}},
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
            {"type": "tool_call", "id": "x1", "name": "Skill",
             "args": {"skill": "heartbeat"}},
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


def test_render_skill_catalog_alphabetic_install_stable():
    """Catalog is alphabetical, contains every seeded skill, no
    bucket headers, no telemetry — install-stable so the system-prompt
    cache prefix isn't busted by skill invocations (chainlink #15)."""
    pin = SkillPinConfig()
    out = render_skill_catalog(["wiki", "memory", "heartbeat"], pin)
    assert out is not None
    # Alphabetical
    assert out.split("\n") == ["- heartbeat", "- memory", "- wiki"]
    # No bucket headers / counts in the catalog
    assert "**" not in out
    assert "in window" not in out


def test_render_skill_catalog_filters_hidden():
    pin = SkillPinConfig(hide=["legacy"])
    out = render_skill_catalog(["legacy", "memory"], pin)
    assert out is not None
    assert "legacy" not in out
    assert "memory" in out


def test_render_skill_catalog_returns_none_when_empty():
    assert render_skill_catalog([], SkillPinConfig()) is None
    # All seeded skills hidden ⇒ None
    assert render_skill_catalog(["x"], SkillPinConfig(hide=["x"])) is None


def test_render_skill_catalog_renders_descriptions_when_provided():
    """When ``descriptions`` is passed, each line renders as
    ``- name — desc`` so the model can dispatch on what each skill
    is for without round-tripping through find-skills."""
    pin = SkillPinConfig()
    descs = {
        "memory": "Criteria for deciding when, where and how to remember information",
        "wiki": "Maintain a structured wiki under state/wiki/",
    }
    out = render_skill_catalog(["memory", "wiki"], pin, descriptions=descs)
    assert out is not None
    assert "- memory — Criteria for deciding when, where and how to remember information" in out
    assert "- wiki — Maintain a structured wiki under state/wiki/" in out


def test_render_skill_catalog_falls_back_to_bare_name_when_desc_missing():
    """A skill present in ``seeded`` but absent from ``descriptions``
    (or with an empty desc) renders as bare ``- name`` — never blocks
    a skill from showing up."""
    pin = SkillPinConfig()
    out = render_skill_catalog(
        ["alpha", "beta"], pin, descriptions={"alpha": "first skill"}
    )
    assert out is not None
    lines = out.split("\n")
    assert lines == ["- alpha — first skill", "- beta"]


def test_render_skill_catalog_truncates_long_descriptions():
    """Long triggers/descriptions (frontmatter is often 100-300 chars)
    truncate to a single-line budget so the system-prompt block stays
    one terminal-row per skill."""
    pin = SkillPinConfig()
    long_desc = (
        "Use when something has gone very wrong and you need to walk a long "
        "diagnostic checklist across multiple subsystems including pollers, "
        "scheduled ticks, the dispatcher, and the bridge layer to figure "
        "out where the message actually got dropped"
    )
    out = render_skill_catalog(
        ["introspection"], pin, descriptions={"introspection": long_desc}
    )
    assert out is not None
    line = out.split("\n")[0]
    # Truncation produces a single line under a reasonable bound and
    # ends with the ellipsis sentinel.
    assert line.startswith("- introspection — ")
    assert line.endswith("…")
    assert len(line) < 150  # name + " — " + ~120 char desc + ellipsis


def test_render_skill_catalog_filters_hidden_with_descriptions():
    """Hidden skills don't get descriptions rendered either."""
    pin = SkillPinConfig(hide=["legacy"])
    out = render_skill_catalog(
        ["legacy", "memory"], pin, descriptions={"legacy": "x", "memory": "y"}
    )
    assert out is not None
    assert "legacy" not in out
    assert "- memory — y" in out


def test_render_skill_telemetry_emits_proven_and_risky_only():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    aggs = {
        "memory": SkillOutcome(skill="memory", success=5, failure=1,
                                last_used=base),
        "wiki": SkillOutcome(skill="wiki", success=1, failure=4,
                              last_used=base),
    }
    pin = SkillPinConfig()
    out = render_skill_telemetry(
        ["memory", "wiki", "heartbeat"], aggs, pin, now=base,
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
    out = render_skill_telemetry(
        ["memory", "wiki"], {}, SkillPinConfig(), now=base,
    )
    assert out is None


def test_render_skill_telemetry_returns_none_when_only_untried():
    """If all skills with aggregates are untried (zero total) the
    telemetry block is empty — both Proven and Risky are empty."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    # Aggregate exists but with zero counts ⇒ untried bucket
    aggs = {"memory": SkillOutcome(skill="memory", last_used=None)}
    out = render_skill_telemetry(["memory"], aggs, SkillPinConfig(), now=base)
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
