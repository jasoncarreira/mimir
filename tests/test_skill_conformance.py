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
_LIST_ITEM = re.compile(r"^\s+-\s+(?P<value>.+)$")


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


def _extract_list_field(text: str, key: str) -> list[str] | None:
    """Return the YAML-list values under ``<key>:`` in the frontmatter,
    or ``None`` if the field is missing entirely. Returns ``[]`` for
    an explicitly empty list (``<key>:`` with no bullet lines).

    Used for ``allowed-tools:`` which is a list shape that the flat
    ``_parse_frontmatter`` collapses awkwardly. Kept separate so the
    primary parser stays simple.
    """
    lines = text.splitlines()
    if not lines or not _FRONTMATTER_DELIM.match(lines[0]):
        return None

    found = False
    in_block = False
    items: list[str] = []
    for raw in lines[1:]:
        if _FRONTMATTER_DELIM.match(raw):
            break
        match = _KEY_LINE.match(raw)
        if match:
            if match.group("key") == key:
                found = True
                in_block = True
                inline_value = match.group("value").strip()
                # Inline form (``allowed-tools: [Foo, Bar]``) — split.
                if inline_value.startswith("[") and inline_value.endswith("]"):
                    payload = inline_value[1:-1].strip()
                    if not payload:
                        return []
                    return [v.strip() for v in payload.split(",")]
                # Empty value followed by bullet lines — fall through.
                if inline_value:
                    # ``allowed-tools: Foo`` — scalar form. Not the list
                    # shape we expect; treat as a single-element list so
                    # the caller's "field present" check still passes.
                    return [inline_value]
            else:
                # A different top-level key — close the list block but
                # remember that we found the field, so accumulated items
                # are still returned.
                in_block = False
        elif in_block:
            list_match = _LIST_ITEM.match(raw)
            if list_match:
                items.append(list_match.group("value").strip())
    if found:
        return items
    return None


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
def test_skill_md_has_allowed_tools(skill_dir: Path) -> None:
    """Every bundled SKILL.md must declare ``allowed-tools:`` in frontmatter.

    chainlink #79 (G3) under chainlink #29 (GBrain pattern adoption).
    The field lists the tools the skill body explicitly references,
    so reviewers can spot ad-hoc tool dependencies growing into a
    skill without updating the documented surface. Docs-only today
    (no runtime enforcement — see state/spec/g3-allowed-tools-audit.md
    for the enforcement-decision discussion).

    An explicitly empty list is allowed for skills that are pure prose
    (no tool surface) but the convention is to enumerate at least the
    ``Read`` you need to follow the skill's instructions.
    """
    skill_md = skill_dir / "SKILL.md"
    text = skill_md.read_text()
    tools = _extract_list_field(text, "allowed-tools")
    assert tools is not None, (
        f"{skill_dir.name}/SKILL.md: missing 'allowed-tools:' field in "
        f"frontmatter. Add a YAML list of the tools the skill body "
        f"references — see state/spec/g3-allowed-tools-audit.md for "
        f"the audit-derived per-skill surface. Use an explicit empty "
        f"list (``allowed-tools: []``) if the skill is pure prose."
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
