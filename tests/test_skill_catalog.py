"""Tests for the skill catalog generator (chainlink #81 / G5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.skill_catalog import (
    SkillEntry,
    _extract_trigger,
    generate,
    load_catalog,
    load_skill,
    render_catalog,
)


def _make_skill(root: Path, name: str, body: str) -> Path:
    """Helper: create a SKILL.md under ``root/<name>/`` with the given body."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body)
    return skill_dir


def test_load_skill_parses_frontmatter(tmp_path: Path) -> None:
    skill_dir = _make_skill(
        tmp_path,
        "demo",
        "---\n"
        "name: demo\n"
        "description: A demo skill. Use when smoke-testing the loader.\n"
        "allowed-tools:\n"
        "  - Read\n"
        "  - Write\n"
        "---\n"
        "# Demo\n",
    )
    entry = load_skill(skill_dir)
    assert entry is not None
    assert entry.name == "demo"
    assert "A demo skill" in entry.description
    assert entry.allowed_tools == ["Read", "Write"]
    assert entry.trigger == "Use when smoke-testing the loader"


def test_load_skill_returns_none_when_skill_md_missing(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    assert load_skill(tmp_path / "broken") is None


def test_load_skill_returns_none_on_malformed_frontmatter(tmp_path: Path) -> None:
    skill_dir = _make_skill(tmp_path, "bad", "no frontmatter here\n")
    assert load_skill(skill_dir) is None


def test_load_skill_falls_back_to_dir_name_if_name_missing(tmp_path: Path) -> None:
    skill_dir = _make_skill(
        tmp_path,
        "no-name-field",
        "---\n"
        "description: Missing name on purpose.\n"
        "---\n",
    )
    entry = load_skill(skill_dir)
    assert entry is not None
    assert entry.name == "no-name-field"


def test_load_skill_handles_missing_allowed_tools(tmp_path: Path) -> None:
    skill_dir = _make_skill(
        tmp_path,
        "no-tools",
        "---\n"
        "name: no-tools\n"
        "description: Skill with no allowed-tools field yet.\n"
        "---\n",
    )
    entry = load_skill(skill_dir)
    assert entry is not None
    assert entry.allowed_tools == []


def test_load_catalog_sorts_alphabetically(tmp_path: Path) -> None:
    _make_skill(tmp_path, "charlie", "---\nname: charlie\ndescription: c.\n---\n")
    _make_skill(tmp_path, "alpha", "---\nname: alpha\ndescription: a.\n---\n")
    _make_skill(tmp_path, "bravo", "---\nname: bravo\ndescription: b.\n---\n")
    entries = load_catalog(tmp_path)
    assert [e.name for e in entries] == ["alpha", "bravo", "charlie"]


def test_load_catalog_skips_non_directory_entries(tmp_path: Path) -> None:
    _make_skill(tmp_path, "real", "---\nname: real\ndescription: r.\n---\n")
    (tmp_path / "stray.md").write_text("not a skill dir")
    entries = load_catalog(tmp_path)
    assert [e.name for e in entries] == ["real"]


def test_load_catalog_returns_empty_when_root_missing(tmp_path: Path) -> None:
    assert load_catalog(tmp_path / "does-not-exist") == []


def test_extract_trigger_prefers_use_when_sentence() -> None:
    desc = "A short intro. Use when smoke-testing the system. Other context here."
    assert _extract_trigger(desc) == "Use when smoke-testing the system"


def test_extract_trigger_falls_back_to_first_sentence() -> None:
    desc = "Just a plain description. With a second sentence."
    assert _extract_trigger(desc) == "Just a plain description"


def test_extract_trigger_handles_use_for_use_to_variants() -> None:
    desc = "Intro. Use for fetching things. Tail."
    assert _extract_trigger(desc) == "Use for fetching things"
    desc2 = "Intro. Use to render. Tail."
    assert _extract_trigger(desc2) == "Use to render"


def test_extract_trigger_empty_input() -> None:
    assert _extract_trigger("") == ""


def test_render_catalog_smoke(tmp_path: Path) -> None:
    """Render produces a well-formed markdown table."""
    entries = [
        SkillEntry(
            name="alpha",
            description="A skill. Use when alpha-ing.",
            allowed_tools=["Read"],
            trigger="Use when alpha-ing",
        ),
        SkillEntry(
            name="beta",
            description="B skill. Use when beta-ing.",
            allowed_tools=["Bash", "Read"],
            trigger="Use when beta-ing",
        ),
    ]
    output = render_catalog(entries)
    assert "# Skills Catalog" in output
    assert "_2 skills indexed._" in output
    assert "| `alpha` | Use when alpha-ing |" in output
    assert "| `beta` | Use when beta-ing | `Bash`, `Read` |" in output
    assert "### `alpha`" in output
    assert "### `beta`" in output


def test_render_catalog_handles_empty_allowed_tools() -> None:
    entries = [
        SkillEntry(
            name="silent",
            description="No tools needed.",
            allowed_tools=[],
            trigger="No tools needed",
        ),
    ]
    output = render_catalog(entries)
    # Empty list renders as em-dash sentinel.
    assert "| `silent` | No tools needed | — |" in output


def test_load_skill_handles_empty_description(tmp_path: Path) -> None:
    """``description:`` present but empty — both ``_extract_trigger`` and
    the row renderer must handle it cleanly (em-dash sentinel, no crash).
    PR #131 review feedback: the other edge cases (missing SKILL.md,
    malformed frontmatter, missing name, missing allowed-tools) are
    pinned; this one wasn't."""
    skill_dir = _make_skill(
        tmp_path,
        "blank-desc",
        "---\n"
        "name: blank-desc\n"
        "description: \n"
        "allowed-tools:\n"
        "  - Read\n"
        "---\n",
    )
    entry = load_skill(skill_dir)
    assert entry is not None
    assert entry.description == ""
    assert entry.trigger == ""
    output = render_catalog([entry])
    # Empty trigger renders as the em-dash sentinel (matches empty-tools
    # cell convention, so the table doesn't have visually-empty cells).
    assert "| `blank-desc` | — | `Read` |" in output
    # Per-skill section falls back to the explicit "no description" stub.
    assert "_(no description)_" in output


def test_load_skill_handles_omitted_description(tmp_path: Path) -> None:
    """``description:`` entirely omitted from frontmatter — same fallback
    path as the explicitly-empty case."""
    skill_dir = _make_skill(
        tmp_path,
        "no-desc",
        "---\n"
        "name: no-desc\n"
        "allowed-tools: []\n"
        "---\n",
    )
    entry = load_skill(skill_dir)
    assert entry is not None
    assert entry.description == ""
    assert entry.trigger == ""


def test_render_catalog_escapes_pipes_in_trigger() -> None:
    entries = [
        SkillEntry(
            name="piped",
            description="Trigger | with | pipes.",
            allowed_tools=[],
            trigger="Trigger | with | pipes",
        ),
    ]
    output = render_catalog(entries)
    # The pipe inside the cell is escaped so the table layout survives.
    assert r"Trigger \| with \| pipes" in output


def test_generate_on_real_bundled_skills_includes_known_skill() -> None:
    """generate() with the default skills root should index every
    bundled skill — including ones we know exist (memory, heartbeat,
    introspection)."""
    output = generate()
    assert "### `memory`" in output
    assert "### `heartbeat`" in output
    assert "### `introspection`" in output


def test_extract_trigger_sentence_split_known_edge_cases() -> None:
    """Pin the sentence-split regex's documented behavior on edge cases.

    The regex ``(?<=[.!?])\\s+(?=[A-Z])`` is tolerant by design: it
    correctly splits at end-of-sentence-followed-by-Capital but trips on
    abbreviation-followed-by-Capital (``U.S. Department`` splits at the
    abbreviation's terminal period). The skill_catalog.py module docs
    document this failure mode and recommend rewriting descriptions to
    avoid it rather than growing the regex. This test pins the
    behavior so future regex tweaks notice if the failure-mode surface
    changes.

    PR #131 punch-list r3218670920.
    """
    # Failure mode: abbreviation followed by a capitalized word. The
    # split happens inside the abbreviation; the first "sentence" gets
    # truncated at the abbreviation's period.
    assert _extract_trigger("U.S. Department of Whatever") == "U.S"

    # Failure mode: ``e.g.`` followed by a capitalized word.
    assert _extract_trigger("e.g. When X happens") == "e.g"

    # Safe: ``e.g.`` followed by a lowercase word does NOT split.
    assert _extract_trigger("e.g. when X happens") == "e.g. when X happens"

    # Safe: digit-then-period-then-digit (decimal number) doesn't split
    # because the next char isn't a capital letter.
    assert _extract_trigger("8.5 million users.") == "8.5 million users"

    # Safe: regular end-of-sentence splits as expected.
    assert _extract_trigger("First sentence. Second sentence.") == "First sentence"


def test_extract_trigger_trigger_phrase_wins_over_abbreviation_split() -> None:
    """Even if an earlier sentence trips the abbreviation-split failure
    mode, a later ``Use when ...`` sentence still wins the trigger.

    Regression guard: ensures the preferred-phrase scan doesn't get
    confused by an under-split first sentence."""
    desc = "Built for U.S. East users. Use when serving traffic from us-east-1."
    assert _extract_trigger(desc) == "Use when serving traffic from us-east-1"


def test_generate_is_idempotent(tmp_path: Path) -> None:
    """Re-running generate() against the same root produces identical output.
    Required by the chainlink #81 acceptance criterion."""
    _make_skill(tmp_path, "alpha", "---\nname: alpha\ndescription: a. Use when alpha.\n---\n")
    _make_skill(tmp_path, "beta", "---\nname: beta\ndescription: b. Use when beta.\n---\n")
    first = generate(tmp_path)
    second = generate(tmp_path)
    assert first == second
