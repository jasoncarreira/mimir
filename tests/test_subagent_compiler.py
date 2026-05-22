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
        "subagent: true\n"
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
    *, subagent: bool | None = None,
) -> None:
    """Create ``<home>/skills/<name>/SKILL.md`` with the given
    frontmatter. ``allowed_tools=None`` means the field is omitted
    entirely.

    ``subagent`` controls the opt-in delegation flag. Default behavior:
    a skill that sets ``allowed_tools`` is assumed to be a delegation
    candidate (the old test contract); pass ``subagent=False`` to
    exclude the flag explicitly, or ``subagent=True`` to include it
    regardless of whether ``allowed_tools`` is set.
    """
    sd = home / "skills" / name
    sd.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if subagent is None:
        subagent = allowed_tools is not None
    if subagent:
        fm_lines.append("subagent: true")
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


def _seed_skill_in(
    dir_: Path, name: str, allowed_tools: list[str] | None,
    description: str = "test skill", body: str = "skill body text",
    *, subagent: bool | None = None,
) -> None:
    """Same as _seed_skill but for arbitrary parent dirs (e.g.
    ``.mimir_builtin_skills`` for dual-location tests)."""
    sd = dir_ / name
    sd.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---", f"name: {name}", f"description: {description}"]
    if subagent is None:
        subagent = allowed_tools is not None
    if subagent:
        fm_lines.append("subagent: true")
    if allowed_tools is not None:
        fm_lines.append("allowed-tools:")
        for t in allowed_tools:
            fm_lines.append(f"  - {t}")
    fm_lines.append("---")
    (sd / "SKILL.md").write_text("\n".join(fm_lines) + "\n\n" + body + "\n")


def test_compile_picks_up_bundled_skills(tmp_path: Path):
    """A fresh deployment with only the bundled refresh in place
    should still compile every allowed-tools-having bundled skill."""
    bundled = tmp_path / ".mimir_builtin_skills"
    _seed_skill_in(bundled, "alpha", allowed_tools=["Bash"])
    _seed_skill_in(bundled, "beta", allowed_tools=["Bash"])
    result = compile_skills_to_subagents(tmp_path, [])
    assert result.delegated_skills == {"alpha", "beta"}


def test_compile_operator_shadows_bundled(tmp_path: Path):
    """When the same skill name exists in both directories, the
    operator-installed version wins (matches SkillsMiddleware's
    last-source-wins rule). Same semantics as Pattern 1 / Pattern 2."""
    bundled = tmp_path / ".mimir_builtin_skills"
    _seed_skill_in(
        bundled, "memory-helper", allowed_tools=["Bash"],
        description="BUNDLED version",
    )
    _seed_skill(
        tmp_path, "memory-helper", allowed_tools=["Bash"],
        description="OPERATOR override",
    )
    result = compile_skills_to_subagents(tmp_path, [])
    assert len(result.subagents) == 1
    assert result.subagents[0]["description"] == "OPERATOR override"


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


def test_compile_skill_without_subagent_flag_stays_inline(tmp_path: Path):
    """Post-2026-05-22: ``allowed-tools`` is no longer the delegation
    trigger. A skill that declares allowed-tools but omits
    ``subagent: true`` stays inline (the agent reads SKILL.md into
    parent context). This is the inverted default from PR #264's
    spike-era heuristic."""
    _seed_skill(
        tmp_path, "documented-but-inline",
        allowed_tools=["Bash", "memory_store"],
        subagent=False,
    )
    result = compile_skills_to_subagents(
        tmp_path, [_FakeTool("memory_store")],
    )
    assert result.subagents == []
    assert result.delegated_skills == set()


def test_compile_subagent_flag_without_allowed_tools(tmp_path: Path):
    """A skill can opt into delegation without restricting tools.
    The SubAgent inherits the framework's default tool surface; the
    parent gets a structured ``task`` call regardless."""
    _seed_skill(
        tmp_path, "delegated", allowed_tools=None, subagent=True,
    )
    result = compile_skills_to_subagents(tmp_path, [])
    assert result.delegated_skills == {"delegated"}
    # No allowed-tools → no explicit tools field on the spec; framework
    # built-ins still flow through the subagent middleware default.
    assert "tools" not in result.subagents[0]


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
    declares-tools-but-no-subagent-flag, eligible-with-drift."""
    mem = _FakeTool("memory_store")
    send = _FakeTool("send_message")
    _seed_skill(tmp_path, "lookup", allowed_tools=["Bash", "memory_store"])
    _seed_skill(tmp_path, "inline-only", allowed_tools=None)
    # Declares allowed-tools for documentation but opts OUT of
    # delegation — stays inline per the post-2026-05-22 contract.
    _seed_skill(
        tmp_path, "documented-inline",
        allowed_tools=["Bash"], subagent=False,
    )
    _seed_skill(
        tmp_path, "messenger",
        allowed_tools=["send_message", "saga_store"],  # drift
    )
    result = compile_skills_to_subagents(tmp_path, [mem, send])
    # Only the two skills that opted into delegation compile.
    assert result.delegated_skills == {"lookup", "messenger"}
    names = {s["name"] for s in result.subagents}
    assert names == {"lookup", "messenger"}
    # messenger had drift: saga_store dropped, send_message resolves.
    messenger = next(s for s in result.subagents if s["name"] == "messenger")
    assert messenger["tools"] == [send]


def test_compile_uses_dirname_when_frontmatter_name_missing(tmp_path: Path):
    """Frontmatter without explicit ``name`` falls back to directory."""
    sd = tmp_path / "skills" / "from-dir"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\ndescription: x\nsubagent: true\nallowed-tools:\n  - Bash\n---\nbody\n"
    )
    result = compile_skills_to_subagents(tmp_path, [])
    assert result.delegated_skills == {"from-dir"}
    assert result.subagents[0]["name"] == "from-dir"


# ─── params / returns schema handling ─────────────────────────────────


def test_compile_skill_with_params_schema_renders_block(tmp_path: Path):
    """Skill declares ``params`` JSON Schema → rendered as
    ``## Parameters`` block in subagent system_prompt, summarized
    into the SubAgent description."""
    sd = tmp_path / "skills" / "weather"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\n"
        "name: weather\n"
        "description: Get weather for a city.\n"
        "subagent: true\n"
        "allowed-tools:\n"
        "  - Bash\n"
        "params:\n"
        "  type: object\n"
        "  properties:\n"
        "    city:\n"
        "      type: string\n"
        "      description: City name\n"
        "    days:\n"
        "      type: integer\n"
        "      description: Forecast horizon\n"
        "  required: [city]\n"
        "---\n"
        "Skill body.\n"
    )
    result = compile_skills_to_subagents(tmp_path, [])
    spec = result.subagents[0]
    # system_prompt gets the parameters block appended.
    assert "## Parameters" in spec["system_prompt"]
    assert "city" in spec["system_prompt"]
    assert "City name" in spec["system_prompt"]
    # description gets the one-line summary appended.
    assert "Get weather for a city." in spec["description"]
    assert "city (required)" in spec["description"]
    assert "days (optional)" in spec["description"]


def test_compile_skill_with_returns_schema_sets_response_format(tmp_path: Path):
    """Skill declares ``returns`` JSON Schema → passed verbatim to
    SubAgent ``response_format`` field, summarized into description."""
    sd = tmp_path / "skills" / "weather"
    sd.mkdir(parents=True)
    returns_block = (
        "returns:\n"
        "  type: object\n"
        "  properties:\n"
        "    forecast:\n"
        "      type: string\n"
        "    high_temp_c:\n"
        "      type: number\n"
        "  required: [forecast]\n"
    )
    (sd / "SKILL.md").write_text(
        "---\n"
        "name: weather\n"
        "description: Get weather.\n"
        "subagent: true\n"
        "allowed-tools:\n"
        "  - Bash\n"
        f"{returns_block}"
        "---\nbody\n"
    )
    result = compile_skills_to_subagents(tmp_path, [])
    spec = result.subagents[0]
    # response_format gets the schema dict.
    assert spec["response_format"]["type"] == "object"
    assert "forecast" in spec["response_format"]["properties"]
    assert spec["response_format"]["required"] == ["forecast"]
    # description gets the return-shape summary.
    assert "Returns: forecast, high_temp_c" in spec["description"]
    # system_prompt gets a ## Final Response block so the SubAgent
    # knows to call the structured-output tool. The schema in this
    # fixture has no ``title`` field, so the rendered phrasing falls
    # back to the generic "structured-output tool" wording.
    assert "## Final Response" in spec["system_prompt"]
    assert "structured-output tool" in spec["system_prompt"]
    # Schema rendered into the block for readability.
    assert "forecast:" in spec["system_prompt"]


def test_compile_skill_with_titled_returns_renders_tool_name(tmp_path: Path):
    """When the returns schema carries a ``title``, the rendered
    ``## Final Response`` block names the tool explicitly (matches
    the runtime tool name langchain binds — structured_output.py:
    159-164 uses the schema's ``title`` as the tool name)."""
    sd = tmp_path / "skills" / "weather"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\n"
        "name: weather\n"
        "description: Get weather.\n"
        "subagent: true\n"
        "allowed-tools:\n"
        "  - Bash\n"
        "returns:\n"
        "  title: weather_result\n"
        "  description: forecast payload\n"
        "  type: object\n"
        "  properties:\n"
        "    forecast: {type: string}\n"
        "  required: [forecast]\n"
        "---\nbody\n"
    )
    result = compile_skills_to_subagents(tmp_path, [])
    spec = result.subagents[0]
    # The renderer surfaces the title verbatim so the SubAgent can
    # refer to the tool by name rather than infer it.
    assert "``weather_result`` tool" in spec["system_prompt"]
    # Generic phrasing should not appear when a title is set.
    assert "structured-output tool" not in spec["system_prompt"]


def test_compile_skill_without_params_or_returns(tmp_path: Path):
    """Backward-compat: skills without params/returns work as before
    — no ``## Parameters`` block, no ``## Final Response`` block, no
    ``response_format`` field, no summary suffix on description."""
    _seed_skill(tmp_path, "bare", allowed_tools=["Bash"])
    result = compile_skills_to_subagents(tmp_path, [])
    spec = result.subagents[0]
    assert "## Parameters" not in spec["system_prompt"]
    assert "## Final Response" not in spec["system_prompt"]
    assert "response_format" not in spec
    # description unchanged from frontmatter
    assert "Params:" not in spec["description"]
    assert "Returns:" not in spec["description"]


def test_compile_skill_with_both_params_and_returns(tmp_path: Path):
    """Both fields together — both render correctly, description
    carries both summaries."""
    sd = tmp_path / "skills" / "weather"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\n"
        "name: weather\n"
        "description: Get weather.\n"
        "subagent: true\n"
        "allowed-tools:\n"
        "  - Bash\n"
        "params:\n"
        "  type: object\n"
        "  properties:\n"
        "    city: {type: string}\n"
        "  required: [city]\n"
        "returns:\n"
        "  type: object\n"
        "  properties:\n"
        "    forecast: {type: string}\n"
        "  required: [forecast]\n"
        "---\nbody\n"
    )
    result = compile_skills_to_subagents(tmp_path, [])
    spec = result.subagents[0]
    assert "## Parameters" in spec["system_prompt"]
    assert "response_format" in spec
    assert "Params: city (required)." in spec["description"]
    assert "Returns: forecast." in spec["description"]


def test_compile_skill_with_malformed_params_is_ignored(tmp_path: Path):
    """``params`` that isn't a dict gets silently ignored — skill
    still compiles, just without the params rendering."""
    sd = tmp_path / "skills" / "x"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\n"
        "name: x\n"
        "description: x\n"
        "subagent: true\n" "allowed-tools:\n  - Bash\n"
        "params: not-a-dict\n"
        "---\nbody\n"
    )
    result = compile_skills_to_subagents(tmp_path, [])
    assert result.delegated_skills == {"x"}
    spec = result.subagents[0]
    assert "## Parameters" not in spec["system_prompt"]
