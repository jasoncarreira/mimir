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


@pytest.mark.parametrize("skill_dir", _bundled_skill_dirs(), ids=lambda p: p.name)
def test_skill_md_body_starts_with_desc_comment(skill_dir: Path) -> None:
    """Each bundled SKILL.md body must start with ``<!-- desc: ... -->``
    (chainlink #102).

    The skill-creator authoring guide (mimir/skills/skill-creator/SKILL.md,
    §"The ``<!-- desc: -->`` first-body-line convention") requires this as
    step 3 of authoring a new skill. The conformance test now enforces it.

    The body ``<!-- desc: -->`` describes "what's in this file" for the
    indexer (``core_blocks.describe_file()``) and future tools. It is
    distinct from the frontmatter ``description:`` (which drives the catalog
    trigger phrase): the body desc is the file-content summary, the
    frontmatter desc is the operator-facing "when to use" signal.
    """
    skill_md = skill_dir / "SKILL.md"
    text = skill_md.read_text()
    # Find body start: everything after the closing "---" of frontmatter.
    lines = text.split("\n")
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                body_start = i + 1
                break
    # First non-blank body line must be <!-- desc: ... -->.
    first_body = next(
        (ln.strip() for ln in lines[body_start:] if ln.strip()), ""
    )
    assert first_body.startswith("<!-- desc:") and first_body.endswith("-->"), (
        f"{skill_dir.name}/SKILL.md: body does not start with ``<!-- desc: ... -->``.\n"
        f"  First body line: {first_body[:80]!r}\n"
        f"  Add ``<!-- desc: <one-line summary> -->`` as the first line of the "
        f"skill body (after the closing ``---`` of frontmatter). See "
        f"mimir/skills/skill-creator/SKILL.md §'The desc first-body-line "
        f"convention' for the pattern."
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


def test_parse_frontmatter_folded_block_terminated_by_next_key() -> None:
    """A zero-indent key after a folded block correctly ends the block.

    The key concern (chainlink #104): a poorly-formatted SKILL.md with
    ``description: >`` followed by an unindented continuation could
    silently absorb the next key.  A proper key at column 0 must
    always start a new field, even when inside a folded block.
    """
    text = (
        "---\n"
        "name: example\n"
        "description: >\n"
        "  Properly indented line.\n"
        "trigger: when operator asks\n"
        "---\n"
    )
    fm = _parse_frontmatter(text)
    assert fm["description"] == "Properly indented line."
    assert fm["trigger"] == "when operator asks"


def test_parse_frontmatter_folded_block_rejects_unindented_continuation() -> None:
    """Non-indented non-key line inside a folded block raises ValueError.

    The silent-swallow gotcha (chainlink #104): if an author writes
    ``description: >`` and the continuation has no leading whitespace,
    the old parser would silently accumulate it.  The fixed parser
    raises loudly so authors notice the mis-format.
    """
    text = (
        "---\n"
        "name: example\n"
        "description: >\n"
        "no indent here\n"  # not indented — should raise
        "---\n"
    )
    with pytest.raises(ValueError, match="folded-scalar continuation"):
        _parse_frontmatter(text)


