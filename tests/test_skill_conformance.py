"""Skill conformance test (chainlink #80 / cluster A of chainlink #29).

Every bundled SKILL.md under ``mimir/skills/<name>/`` must have a
YAML frontmatter block with at minimum a non-empty ``name`` field and
a non-empty ``description`` field. Drift here is the failure mode that
caused the introspection skill to be invisible to the find-skills
ranker for ~weeks before the 2026-05-08 audit-resolvable phase 1 pass
caught it.

Schema today (minimum spine):

  ---
  name: <skill-folder-name>
  description: <non-empty, prose preferred>
  ---

The ``allowed-tools:`` field used to live here (chainlink #79 / G3)
but was removed 2026-05-23 — deepagents' SkillsMiddleware silently
rejected mimir's YAML-list form (string-only per Anthropic Agent
Skills spec), so the field never rendered in the catalog and had no
runtime enforcement. After the SubAgent delegation rip-out (PR #271)
there was no remaining consumer for the field; it got dropped along
with its conformance audit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.skill_md import (
    extract_list_field as _extract_list_field,
    parse_frontmatter as _parse_frontmatter,
)

# Bundled skills live alongside the source so the test does not depend
# on a seeded ``<home>/.claude/skills/`` directory.
SKILLS_ROOT = Path(__file__).parent.parent / "mimir" / "skills"

def _bundled_skill_dirs() -> list[Path]:
    return sorted(
        d for d in SKILLS_ROOT.iterdir() if d.is_dir() and (d / "SKILL.md").is_file()
    )


@pytest.mark.parametrize("skill_dir", _bundled_skill_dirs(), ids=lambda p: p.name)
def test_skill_md_has_required_frontmatter(skill_dir: Path) -> None:
    """Every bundled SKILL.md must have non-empty ``name`` and ``description``."""
    skill_md = skill_dir / "SKILL.md"
    text = skill_md.read_text()
    try:
        fm = _parse_frontmatter(text)
    except ValueError as exc:
        pytest.fail(f"{skill_dir.name}/SKILL.md: frontmatter malformed: {exc}")

    name = fm.get("name", "").strip()
    description = fm.get("description", "").strip()

    assert name, (
        f"{skill_dir.name}/SKILL.md: missing or empty 'name:' field in frontmatter. "
        f"Add `name: {skill_dir.name}` (matching the folder name)."
    )
    assert name == skill_dir.name, (
        f"{skill_dir.name}/SKILL.md: 'name: {name}' does not match folder name "
        f"'{skill_dir.name}'. The folder name is the canonical identifier — "
        f"either rename the folder or the frontmatter to match."
    )
    assert description, (
        f"{skill_dir.name}/SKILL.md: missing or empty 'description:' field in "
        f"frontmatter. The description is what find-skills surfaces in skill "
        f"discovery; a skill without one is effectively invisible to the ranker. "
        f"Add a one-sentence summary of when to use this skill."
    )


def test_parse_frontmatter_rejects_missing_opening_delim() -> None:
    """The parser must fail loudly on malformed frontmatter so the
    main parametrized test surfaces it correctly."""
    with pytest.raises(ValueError, match="opening"):
        _parse_frontmatter("name: foo\n---\n")


def test_parse_frontmatter_rejects_missing_closing_delim() -> None:
    with pytest.raises(ValueError, match="closing"):
        _parse_frontmatter("---\nname: foo\n# no closing delim\n")


def test_parse_frontmatter_handles_folded_description() -> None:
    """``onboarding`` uses ``description: >`` with continuation lines."""
    text = (
        "---\n"
        "name: example\n"
        "description: >\n"
        "  First line continuation.\n"
        "  Second line.\n"
        "---\n"
    )
    fm = _parse_frontmatter(text)
    assert fm["name"] == "example"
    assert fm["description"] == "First line continuation. Second line."


def test_extract_list_field_block_form() -> None:
    text = (
        "---\n"
        "name: example\n"
        "allowed-tools:\n"
        "  - Read\n"
        "  - Write\n"
        "  - Bash\n"
        "---\n"
    )
    assert _extract_list_field(text, "allowed-tools") == ["Read", "Write", "Bash"]


def test_extract_list_field_missing_returns_none() -> None:
    text = "---\nname: example\ndescription: foo\n---\n"
    assert _extract_list_field(text, "allowed-tools") is None


def test_extract_list_field_explicitly_empty() -> None:
    text = "---\nname: example\nallowed-tools: []\n---\n"
    assert _extract_list_field(text, "allowed-tools") == []


def test_extract_list_field_inline_array_form() -> None:
    text = "---\nname: example\nallowed-tools: [Read, Write]\n---\n"
    assert _extract_list_field(text, "allowed-tools") == ["Read", "Write"]


def test_extract_list_field_rejects_scalar_form() -> None:
    """``allowed-tools: Foo`` (scalar, not list) must be rejected so the
    conformance test reports it as a missing/malformed field instead of
    silently coercing to ``[Foo]``. PR #130 review feedback."""
    text = "---\nname: example\nallowed-tools: Foo\n---\n"
    assert _extract_list_field(text, "allowed-tools") is None


def test_extract_list_field_stops_at_next_key() -> None:
    """List block should NOT swallow the following frontmatter key."""
    text = (
        "---\n"
        "name: example\n"
        "allowed-tools:\n"
        "  - Read\n"
        "description: foo\n"
        "---\n"
    )
    assert _extract_list_field(text, "allowed-tools") == ["Read"]
