"""Tests for ``mimir.subagent_compiler``.

Covers: frontmatter parsing, eligibility gating (allowed-tools
declared + not in reflective_override + parses), tool resolution
(framework built-ins auto-pass, mimir-custom tools resolve via
registry, unknowns logged), the algedonic signal event on drift,
and the delegated-skills output for downstream catalog filtering.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.subagent_compiler import (
    CompileResult,
    compile_skills_to_subagents,
    parse_skill_frontmatter,
)


# ─── parse_skill_frontmatter ─────────────────────────────────────────


def test_parse_frontmatter_happy(tmp_path: Path):
    sm = tmp_path / "SKILL.md"
    sm.write_text(
        "---\n"
        "name: x\n"
        "description: do the thing\n"
        "allowed-tools:\n"
        "  - Bash\n"
        "  - memory_store\n"
        "---\n"
        "# X\n\nbody here\n"
    )
    meta, body = parse_skill_frontmatter(sm)
    assert meta["name"] == "x"
    assert meta["description"] == "do the thing"
    assert meta["allowed-tools"] == ["Bash", "memory_store"]
    assert body.startswith("# X")


def test_parse_frontmatter_missing(tmp_path: Path):
    """No leading --- → no frontmatter, body is full text."""
    sm = tmp_path / "SKILL.md"
    sm.write_text("just markdown, no frontmatter\n")
    meta, body = parse_skill_frontmatter(sm)
    assert meta == {}
    assert body == "just markdown, no frontmatter\n"


def test_parse_frontmatter_malformed(tmp_path: Path):
    """YAML errors don't crash — return empty meta + best-effort body."""
    sm = tmp_path / "SKILL.md"
    sm.write_text("---\nname: x\nallowed-tools: [unclosed\n---\nbody\n")
    meta, body = parse_skill_frontmatter(sm)
    assert meta == {}
    assert "body" in body


def test_parse_frontmatter_missing_close(tmp_path: Path):
    """No closing --- → don't parse, return full text as body."""
    sm = tmp_path / "SKILL.md"
    sm.write_text("---\nname: x\nbody never starts\n")
    meta, body = parse_skill_frontmatter(sm)
    assert meta == {}
    assert "body never starts" in body


# ─── compile_skills_to_subagents ─────────────────────────────────────


class _FakeTool:
    """Mimics the langchain BaseTool ``.name`` attribute."""
    def __init__(self, name: str):
        self.name = name


def _seed_skill(
    home: Path, name: str, allowed_tools: list[str] | None,
    description: str = "test skill", body: str = "skill body text",
) -> None:
    """Create ``<home>/.claude/skills/<name>/SKILL.md`` with the given
    frontmatter. ``allowed_tools=None`` means the field is omitted
    entirely (eligibility off)."""
    sd = home / ".claude" / "skills" / name
    sd.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if allowed_tools is not None:
        fm_lines.append("allowed-tools:")
        for t in allowed_tools:
            fm_lines.append(f"  - {t}")
    fm_lines.append("---")
    (sd / "SKILL.md").write_text("\n".join(fm_lines) + "\n\n" + body + "\n")


def test_compile_missing_skills_dir(tmp_path: Path):
    """No skills dir → empty result, no crash."""
    result = compile_skills_to_subagents(tmp_path, [])
    assert result.subagents == []
    assert result.delegated_skills == set()


def test_compile_skill_without_allowed_tools_stays_inline(tmp_path: Path):
    """Skills with no allowed-tools declaration are not eligible."""
    _seed_skill(tmp_path, "memory-only", allowed_tools=None)
    result = compile_skills_to_subagents(tmp_path, [_FakeTool("memory_store")])
    assert result.subagents == []
    assert result.delegated_skills == set()


def test_compile_skill_with_allowed_tools_becomes_subagent(tmp_path: Path):
    """The motivating case: skill declares allowed-tools → subagent."""
    mem = _FakeTool("memory_store")
    _seed_skill(
        tmp_path, "lookup",
        allowed_tools=["Bash", "Read", "memory_store"],
        description="resolve aliases",
    )
    result = compile_skills_to_subagents(tmp_path, [mem])
    assert len(result.subagents) == 1
    assert result.delegated_skills == {"lookup"}
    spec = result.subagents[0]
    assert spec["name"] == "lookup"
    assert spec["description"] == "resolve aliases"
    assert "skill body text" in spec["system_prompt"]
    # Only the custom mimir tool gets registered; Bash/Read are
    # framework built-ins (auto-injected via SubAgent middleware).
    assert spec["tools"] == [mem]


def test_compile_skill_with_only_framework_builtins(tmp_path: Path):
    """Skill with only built-in allowed-tools still compiles (subagent
    gets the built-ins via middleware). ``tools`` field is omitted."""
    _seed_skill(tmp_path, "shell-only", allowed_tools=["Bash"])
    result = compile_skills_to_subagents(tmp_path, [])
    assert result.delegated_skills == {"shell-only"}
    spec = result.subagents[0]
    # No explicit ``tools`` since nothing was resolved as a custom tool.
    assert "tools" not in spec


def test_compile_reflective_override_blocks_compilation(tmp_path: Path):
    """Skills in the override set keep their inline behavior even
    when they declare allowed-tools."""
    _seed_skill(tmp_path, "heartbeat", allowed_tools=["Bash", "memory_store"])
    result = compile_skills_to_subagents(
        tmp_path, [_FakeTool("memory_store")],
    )
    assert result.subagents == []
    assert result.delegated_skills == set()


def test_compile_custom_override_set(tmp_path: Path):
    """Caller can override the reflective set entirely."""
    _seed_skill(tmp_path, "memory", allowed_tools=["Bash"])
    # ``memory`` is normally in the default override; with an empty
    # override, it compiles to a subagent.
    result = compile_skills_to_subagents(
        tmp_path, [], reflective_override=frozenset(),
    )
    assert result.delegated_skills == {"memory"}


def test_compile_unknown_tools_dropped_with_warning(tmp_path: Path):
    """Unknown tool names → logged in warnings, kept out of the
    spec's tools list, but the skill still compiles."""
    mem = _FakeTool("memory_store")
    _seed_skill(
        tmp_path, "drifted",
        allowed_tools=["Bash", "memory_store", "saga_store", "made_up_tool"],
    )
    result = compile_skills_to_subagents(tmp_path, [mem])
    assert result.delegated_skills == {"drifted"}
    spec = result.subagents[0]
    # Only the known custom tool resolves; unknowns dropped.
    assert spec["tools"] == [mem]
    # Both unknowns mentioned in warnings.
    joined = " ".join(result.warnings)
    assert "saga_store" in joined
    assert "made_up_tool" in joined


def test_compile_unknown_tools_emit_algedonic_event(tmp_path: Path):
    """Unknown allowed-tools entries trigger an
    ``allowed_tool_unknown_anomalous`` event to events.jsonl so the
    next turn's feedback signals surface the drift."""
    from mimir.event_logger import init_logger

    events_path = tmp_path / "events.jsonl"
    init_logger(events_path, session_id="t-1")

    _seed_skill(
        tmp_path, "drifted",
        allowed_tools=["Bash", "saga_store", "made_up_tool"],
    )
    compile_skills_to_subagents(tmp_path, [])

    lines = events_path.read_text().splitlines()
    # At least one anomalous event landed.
    anomalous = [
        json.loads(l) for l in lines
        if json.loads(l).get("type") == "allowed_tool_unknown_anomalous"
    ]
    assert len(anomalous) == 1
    ev = anomalous[0]
    assert ev["skill"] == "drifted"
    assert set(ev["unknown_tools"]) == {"saga_store", "made_up_tool"}
    assert ev["skill_path"].endswith("drifted/SKILL.md")


def test_compile_unknown_tools_no_event_when_logger_uninitialized(tmp_path: Path):
    """Telemetry is best-effort: if event_logger isn't initialized, we
    shouldn't crash compilation. Reset the singleton then re-run."""
    import mimir.event_logger as el
    el._logger = None  # ensure uninitialized

    _seed_skill(tmp_path, "drifted", allowed_tools=["made_up_tool"])
    # Must not raise even though no logger is wired.
    result = compile_skills_to_subagents(tmp_path, [])
    assert result.delegated_skills == {"drifted"}


def test_compile_multiple_skills_with_mixed_eligibility(tmp_path: Path):
    """Realistic case: 4 skills — eligible, ineligible-no-tools,
    reflective-override, eligible-with-drift."""
    mem = _FakeTool("memory_store")
    send = _FakeTool("send_message")
    _seed_skill(tmp_path, "lookup", allowed_tools=["Bash", "memory_store"])
    _seed_skill(tmp_path, "inline-only", allowed_tools=None)
    _seed_skill(tmp_path, "heartbeat", allowed_tools=["Bash"])  # in default override
    _seed_skill(
        tmp_path, "messenger",
        allowed_tools=["send_message", "saga_store"],  # drift
    )
    result = compile_skills_to_subagents(tmp_path, [mem, send])
    # Three skills declared allowed-tools, but heartbeat is in the
    # override → only 2 compile.
    assert result.delegated_skills == {"lookup", "messenger"}
    names = {s["name"] for s in result.subagents}
    assert names == {"lookup", "messenger"}
    # messenger had drift: saga_store dropped, send_message resolves.
    messenger = next(s for s in result.subagents if s["name"] == "messenger")
    assert messenger["tools"] == [send]


def test_compile_uses_dirname_when_frontmatter_name_missing(tmp_path: Path):
    """Frontmatter without explicit ``name`` falls back to directory."""
    sd = tmp_path / ".claude" / "skills" / "from-dir"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\ndescription: x\nallowed-tools:\n  - Bash\n---\nbody\n"
    )
    result = compile_skills_to_subagents(tmp_path, [])
    assert result.delegated_skills == {"from-dir"}
    assert result.subagents[0]["name"] == "from-dir"
