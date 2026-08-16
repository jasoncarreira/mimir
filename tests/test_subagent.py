"""Subagent .md definitions (SPEC §4.3)."""

from __future__ import annotations

from pathlib import Path

from mimir.subagent_defs import (
    list_subagents,
    parse_vsm_config,
    seed_subagent_defs,
)


def test_seed_creates_three_subagents(tmp_path: Path):
    out = seed_subagent_defs(tmp_path)
    assert out == {
        "climber.md": "created",
        "researcher.md": "created",
        "critic.md": "created",
    }
    for name in ("climber.md", "researcher.md", "critic.md"):
        assert (tmp_path / ".claude" / "agents" / name).is_file()


def test_seed_does_not_overwrite_existing(tmp_path: Path):
    target = tmp_path / ".claude" / "agents" / "climber.md"
    target.parent.mkdir(parents=True)
    target.write_text("user-modified", encoding="utf-8")

    out = seed_subagent_defs(tmp_path)
    assert out["climber.md"] == "present"
    assert target.read_text() == "user-modified"


def test_climber_marked_background(tmp_path: Path):
    seed_subagent_defs(tmp_path)
    body = (tmp_path / ".claude" / "agents" / "climber.md").read_text()
    assert "background: true" in body


# ─── §12.5: VSM frontmatter parsing ────────────────────────────────────


def test_list_subagents_returns_bundled_names():
    names = list_subagents()
    assert set(names) == {"climber", "researcher", "critic"}


def test_parse_vsm_config_falls_back_to_bundled(tmp_path: Path):
    # Home is empty — no .claude/agents/<name>.md present.
    vsm = parse_vsm_config(tmp_path, "climber")
    assert vsm is not None
    assert vsm["s3_tool_budget"] == 60
    assert vsm["s2_anti_oscillation"]["iteration_cap"] == 20
    assert vsm["s4_foresight"] is False


def test_parse_vsm_config_reads_home_file(tmp_path: Path):
    seed_subagent_defs(tmp_path)
    vsm = parse_vsm_config(tmp_path, "researcher")
    assert vsm is not None
    assert vsm["s3_tool_budget"] == 15
    assert vsm["s4_foresight"] is False


def test_parse_vsm_config_returns_none_for_unknown_subagent(tmp_path: Path):
    assert parse_vsm_config(tmp_path, "does-not-exist") is None


def test_parse_vsm_config_handles_no_frontmatter(tmp_path: Path):
    target = tmp_path / ".claude" / "agents" / "climber.md"
    target.parent.mkdir(parents=True)
    target.write_text("just a body, no frontmatter\n", encoding="utf-8")
    assert parse_vsm_config(tmp_path, "climber") is None


def test_parse_vsm_config_handles_no_vsm_block(tmp_path: Path):
    target = tmp_path / ".claude" / "agents" / "climber.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\nname: climber\ndescription: test\ntools: Bash\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert parse_vsm_config(tmp_path, "climber") is None


def test_parse_vsm_config_handles_malformed_yaml(tmp_path: Path):
    target = tmp_path / ".claude" / "agents" / "climber.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\nname: climber\n  bad: indent: stuff\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert parse_vsm_config(tmp_path, "climber") is None


def test_parse_vsm_config_user_override_wins_over_bundled(tmp_path: Path):
    target = tmp_path / ".claude" / "agents" / "critic.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\n"
        "name: critic\n"
        "description: custom\n"
        "vsm:\n"
        "  s3_tool_budget: 999\n"
        "  s4_foresight: true\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    vsm = parse_vsm_config(tmp_path, "critic")
    assert vsm == {"s3_tool_budget": 999, "s4_foresight": True}
