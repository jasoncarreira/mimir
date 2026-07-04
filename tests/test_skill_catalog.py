"""Tests for the skill catalog generator (chainlink #81 / G5)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from mimir.skill_catalog import (
    SkillEntry,
    _extract_trigger,
    _load_catalog_inner,
    cmd,
    generate,
    invocable_skill_registry,
    list_invocable_skills,
    load_catalog,
    load_skill,
    render_catalog,
    resolve_invocable_skill,
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
        "---\n"
        "# Demo\n",
    )
    entry = load_skill(skill_dir)
    assert entry is not None
    assert entry.name == "demo"
    assert "A demo skill" in entry.description
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
            trigger="Use when alpha-ing",
        ),
        SkillEntry(
            name="beta",
            description="B skill. Use when beta-ing.",
            trigger="Use when beta-ing",
        ),
    ]
    output = render_catalog(entries)
    assert "# Skills Catalog" in output
    assert "_2 skills indexed._" in output
    assert "| `alpha` | Use when alpha-ing |" in output
    assert "| `beta` | Use when beta-ing |" in output
    assert "### `alpha`" in output
    assert "### `beta`" in output


def test_render_catalog_schema_version_marker() -> None:
    """render_catalog emits a catalog-schema version comment near the top.

    Downstream parsers that rely on the two-column shape (Skill / Trigger)
    should key on this marker rather than column indices alone.
    A v2 → v3 bump signals a breaking column change.
    """
    output = render_catalog([])
    assert "<!-- catalog-schema: v2 -->" in output
    lines = output.splitlines()
    # Marker must be in the first three lines (after the desc comment and
    # before the h1 title) so parsers can detect it without scanning the
    # full file.
    assert any("catalog-schema: v2" in line for line in lines[:3])


def test_load_skill_handles_empty_description(tmp_path: Path) -> None:
    """``description:`` present but empty — both ``_extract_trigger`` and
    the row renderer must handle it cleanly (em-dash sentinel, no crash).
    PR #131 review feedback: the other edge cases (missing SKILL.md,
    malformed frontmatter, missing name) are pinned; this one wasn't."""
    skill_dir = _make_skill(
        tmp_path,
        "blank-desc",
        "---\n"
        "name: blank-desc\n"
        "description: \n"
        "---\n",
    )
    entry = load_skill(skill_dir)
    assert entry is not None
    assert entry.description == ""
    assert entry.trigger == ""
    output = render_catalog([entry])
    # Empty trigger renders as the em-dash sentinel.
    assert "| `blank-desc` | — |" in output
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
            trigger="Trigger | with | pipes",
        ),
    ]
    output = render_catalog(entries)
    # The pipe inside the cell is escaped so the table layout survives.
    assert r"Trigger \| with \| pipes" in output


def test_generate_on_real_bundled_skills_includes_known_skill() -> None:
    """generate() with the default skills root should index every
    bundled skill — including ones we know exist (memory, wiki,
    introspection)."""
    output = generate()
    assert "### `memory`" in output
    assert "### `wiki`" in output
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


def test_invocable_skill_registry_is_explicit_safe_allowlist() -> None:
    skills = invocable_skill_registry()
    slash_names = {skill.slash_name for skill in skills}
    assert slash_names == {"/find-skills", "/five-whys", "/try-harder"}
    assert all(skill.side_effect_class == "none" for skill in skills)
    assert all(skill.invocation_syntax.startswith(skill.slash_name) for skill in skills)
    assert all(skill.context_schema.get("type") == "object" for skill in skills)


def test_resolve_invocable_skill_only_accepts_allowlisted_slash_names() -> None:
    assert resolve_invocable_skill("/find-skills") is not None
    assert resolve_invocable_skill("find-skills please") is not None
    assert resolve_invocable_skill("/memory") is None
    assert resolve_invocable_skill("/github") is None
    assert resolve_invocable_skill("/review") is None
    assert resolve_invocable_skill("/chainlink") is None


def test_invocable_skill_listing_uses_registry_shape() -> None:
    payload = list_invocable_skills()
    assert [item["slash_name"] for item in payload] == [
        "/find-skills",
        "/five-whys",
        "/try-harder",
    ]
    assert all(set(item["constraints"]) == {"channels", "users"} for item in payload)
    assert all("context_schema" in item for item in payload)


# ---------------------------------------------------------------------------
# Malformed-SKILL.md stderr warning + --strict flag (chainlink #105)
# ---------------------------------------------------------------------------


def test_load_skill_emits_stderr_on_malformed_frontmatter(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A SKILL.md with broken YAML emits a WARNING to stderr and returns None."""
    skill_dir = _make_skill(tmp_path, "broken", "no frontmatter here\n")
    result = load_skill(skill_dir)
    assert result is None
    captured = capsys.readouterr()
    assert "WARNING:" in captured.err
    assert "broken" in captured.err
    assert "parse error" in captured.err


def test_load_skill_no_stderr_when_skill_md_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A missing SKILL.md (not a skill dir) is silently skipped — no warning."""
    (tmp_path / "notaskill").mkdir()
    result = load_skill(tmp_path / "notaskill")
    assert result is None
    captured = capsys.readouterr()
    assert captured.err == ""


def test_load_skill_emits_algedonic_event_on_malformed_frontmatter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_skill emits skill_frontmatter_error via log_event_sync when frontmatter is broken.

    This makes malformed skills visible in the per-turn algedonic feedback block
    rather than requiring the operator to notice a missing skill by accident
    (chainlink #201).
    """
    emitted: list[dict] = []

    def _fake_log_event_sync(event_type: str, **payload) -> None:  # type: ignore[type-arg]
        emitted.append({"type": event_type, **payload})

    monkeypatch.setattr("mimir.skill_catalog.log_event_sync", _fake_log_event_sync)

    skill_dir = _make_skill(tmp_path, "busted-skill", "not valid frontmatter at all\n")
    result = load_skill(skill_dir)

    assert result is None
    assert len(emitted) == 1
    ev = emitted[0]
    assert ev["type"] == "skill_frontmatter_error"
    assert ev["skill_name"] == "busted-skill"
    assert "SKILL.md" in ev["path"]
    assert ev.get("error")  # non-empty error message


def test_load_skill_no_event_when_logger_not_initialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When log_event_sync raises RuntimeError (logger not initialized), load_skill
    still returns None without crashing — CLI / bench contexts run without a live logger.
    """
    monkeypatch.setattr(
        "mimir.skill_catalog.log_event_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("not initialized")),
    )

    skill_dir = _make_skill(tmp_path, "busted-skill2", "no frontmatter\n")
    result = load_skill(skill_dir)  # must not raise

    assert result is None


def test_load_catalog_inner_counts_parse_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """_load_catalog_inner returns (entries, error_count); good skills are
    included, bad ones are counted in the error total."""
    _make_skill(tmp_path, "good", "---\nname: good\ndescription: A good skill.\n---\n")
    _make_skill(tmp_path, "bad", "no frontmatter at all\n")
    entries, error_count = _load_catalog_inner(tmp_path)
    assert [e.name for e in entries] == ["good"]
    assert error_count == 1
    # The parse failure was reported on stderr.
    captured = capsys.readouterr()
    assert "WARNING:" in captured.err


def test_load_catalog_inner_no_skill_md_not_counted_as_error(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Dirs without a SKILL.md are skipped silently — not counted as errors."""
    (tmp_path / "not-a-skill").mkdir()  # dir with no SKILL.md
    _make_skill(tmp_path, "real", "---\nname: real\ndescription: Real skill.\n---\n")
    entries, error_count = _load_catalog_inner(tmp_path)
    assert error_count == 0
    assert [e.name for e in entries] == ["real"]
    assert capsys.readouterr().err == ""


def test_cmd_strict_exits_1_on_parse_error(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``mimir skills catalog --strict`` exits 1 when any SKILL.md is malformed."""
    _make_skill(tmp_path, "bad", "no frontmatter\n")
    args = argparse.Namespace(out=None, skills_root=tmp_path, strict=True)
    exit_code = cmd(args)
    assert exit_code == 1


def test_cmd_strict_exits_0_when_all_valid(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``mimir skills catalog --strict`` exits 0 when every SKILL.md parses cleanly."""
    _make_skill(tmp_path, "good", "---\nname: good\ndescription: A good skill.\n---\n")
    args = argparse.Namespace(out=None, skills_root=tmp_path, strict=True)
    exit_code = cmd(args)
    assert exit_code == 0


def test_cmd_non_strict_exits_0_even_with_parse_error(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Without --strict, the command exits 0 even when SKILL.md files are broken
    (operator still sees warnings on stderr, but the catalog is still generated)."""
    _make_skill(tmp_path, "bad", "no frontmatter\n")
    args = argparse.Namespace(out=None, skills_root=tmp_path, strict=False)
    exit_code = cmd(args)
    assert exit_code == 0
    # Warning still emitted to stderr.
    captured = capsys.readouterr()
    assert "WARNING:" in captured.err
