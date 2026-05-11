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

Future extensions land here as later subissues:

  * chainlink #79 (G3 ``allowed-tools:``) — once the field is added,
    assert it is present (populated or explicitly empty).
  * G4 ``triggers:`` (deferred) — would add a similar assertion.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Bundled skills live alongside the source so the test does not depend
# on a seeded ``<home>/.claude/skills/`` directory.
SKILLS_ROOT = Path(__file__).parent.parent / "mimir" / "skills"

# Bare-bones YAML frontmatter parser: avoid pulling in PyYAML for a
# 30-line schema check. SKILL.md frontmatter is single-level key: value
# pairs (with the occasional folded ``>`` block); a tolerant
# line-by-line scan is enough for the conformance bar this test
# enforces. If the schema grows nested structure later, swap in
# ``yaml.safe_load``.
_FRONTMATTER_DELIM = re.compile(r"^---\s*$")
_KEY_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*)$")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Return a flat key->value map for the leading ``--- ... ---`` block.

    Raises ``ValueError`` if the block is missing or malformed (no
    closing delimiter). Values are stripped; multi-line folded values
    are collapsed to the literal first line plus continuation suffix.
    """
    lines = text.splitlines()
    if not lines or not _FRONTMATTER_DELIM.match(lines[0]):
        raise ValueError("missing opening '---' delimiter")

    out: dict[str, str] = {}
    current_key: str | None = None
    closed = False
    for raw in lines[1:]:
        if _FRONTMATTER_DELIM.match(raw):
            closed = True
            break
        match = _KEY_LINE.match(raw)
        if match:
            current_key = match.group("key")
            value = match.group("value").strip()
            # Strip ``>`` folded-block marker (onboarding/SKILL.md uses
            # ``description: >`` then continues on subsequent indented
            # lines). The continuation accumulates below.
            if value in {">", "|"}:
                out[current_key] = ""
            else:
                out[current_key] = value
        elif current_key is not None and raw.strip():
            # Continuation line for a folded block.
            prior = out.get(current_key, "")
            out[current_key] = (prior + " " + raw.strip()).strip()

    if not closed:
        raise ValueError("missing closing '---' delimiter")
    return out


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
