"""INDEX.md generation (SPEC §3.4, §6.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.index import (
    IndexGenerator,
    build_memory_index,
    build_state_index,
    build_wiki_index,
)


def _seed_wiki(home: Path) -> None:
    wiki = home / "state" / "wiki"
    (wiki / "entities").mkdir(parents=True)
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "topics").mkdir(parents=True)

    (wiki / "entities" / "alice.md").write_text(
        "<!-- desc: Alice the operator -->\n# Alice\nbody.\n"
    )
    (wiki / "concepts" / "actor-model.md").write_text(
        "<!-- desc: Hewitt's actor model -->\n# Actors\nbody.\n"
    )
    (wiki / "concepts" / "no-desc-page.md").write_text(
        "# Untagged\nbody without desc-comment.\n"
    )
    (wiki / "topics" / "bench-map.md").write_text(
        "<!-- desc: bench layout -->\n# Bench Map\nbody.\n"
    )


def _seed_memory(home: Path) -> None:
    (home / "memory" / "core").mkdir(parents=True)
    (home / "memory" / "channels").mkdir(parents=True)

    (home / "memory" / "core" / "00-persona.md").write_text("<!-- desc: persona -->\n# P")
    (home / "memory" / "channels" / "alice.md").write_text(
        "<!-- desc: notes on Alice -->\nAlice prefers markdown."
    )
    (home / "memory" / "topics.md").write_text("# Topics\nthings I know about.")


def test_build_memory_index_includes_core_with_tag(tmp_path: Path):
    """Core files now appear in the index tagged ``[core]`` so the
    agent can navigate to them for edits. They're still skipped by
    file_search (they're already in the system prompt)."""
    _seed_memory(tmp_path)
    body = build_memory_index(tmp_path)

    assert "channels/alice.md" in body
    assert "topics.md" in body
    # Core files appear, tagged so the agent recognizes them.
    assert "core/00-persona.md" in body
    assert "`[core]`" in body
    # The auto-generated INDEX.md itself is still excluded.
    assert "memory/INDEX.md" not in body
    assert "- INDEX.md" not in body


def test_files_without_desc_are_marked_auto(tmp_path: Path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "without-desc.md").write_text("# Title\nThis is the body.")
    (tmp_path / "memory" / "with-desc.md").write_text("<!-- desc: explicit -->\nfoo")

    body = build_memory_index(tmp_path)
    assert "[auto] " in body
    # The explicit one shouldn't have the marker.
    assert "with-desc.md — explicit" in body
    assert "with-desc.md — [auto]" not in body


def test_build_state_index_lists_state_files(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "transcript.md").write_text("<!-- desc: kickoff -->\nbody")
    body = build_state_index(tmp_path)
    assert "transcript.md" in body
    assert "kickoff" in body


def test_empty_tree_produces_none_marker(tmp_path: Path):
    body = build_memory_index(tmp_path)
    assert "(none)" in body


@pytest.mark.asyncio
async def test_generator_writes_indexes_on_flush(tmp_path: Path):
    _seed_memory(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "doc.md").write_text("# doc\nbody.")

    gen = IndexGenerator(tmp_path)
    gen.mark_dirty("all")
    await gen.flush()

    mem_idx = (tmp_path / "memory" / "INDEX.md").read_text()
    state_idx = (tmp_path / "state" / "INDEX.md").read_text()
    assert "channels/alice.md" in mem_idx
    assert "doc.md" in state_idx


@pytest.mark.asyncio
async def test_generator_flush_no_op_when_clean(tmp_path: Path):
    _seed_memory(tmp_path)
    gen = IndexGenerator(tmp_path)
    gen.mark_dirty("all")
    await gen.flush()
    mtime_before = (tmp_path / "memory" / "INDEX.md").stat().st_mtime_ns

    # No mark_dirty between flushes → second flush is a no-op (no rewrite).
    await gen.flush()
    mtime_after = (tmp_path / "memory" / "INDEX.md").stat().st_mtime_ns
    assert mtime_after == mtime_before


@pytest.mark.asyncio
async def test_generator_flushes_only_dirty_scope(tmp_path: Path):
    _seed_memory(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "doc.md").write_text("# doc")

    gen = IndexGenerator(tmp_path)
    gen.mark_dirty("memory")
    await gen.flush()

    assert (tmp_path / "memory" / "INDEX.md").exists()
    # state index wasn't dirty, so it shouldn't have been written.
    assert not (tmp_path / "state" / "INDEX.md").exists()
    # wiki index also untouched.
    assert not (tmp_path / "state" / "wiki" / "index.md").exists()


def test_build_wiki_index_section_grouped(tmp_path: Path):
    """chainlink #31 #38: wiki/index.md is auto-regen'd from page
    desc-comments, grouped by Entities/Concepts/Topics."""
    _seed_wiki(tmp_path)
    body = build_wiki_index(tmp_path)

    # Section headers present (only when populated).
    assert "## Entities" in body
    assert "## Concepts" in body
    assert "## Topics" in body

    # Wiki-style link + rel-path + desc, one per line.
    assert "[[alice]] — `entities/alice.md` — Alice the operator" in body
    assert "[[actor-model]] — `concepts/actor-model.md` — Hewitt's actor model" in body
    assert "[[bench-map]] — `topics/bench-map.md` — bench layout" in body

    # Page without desc-comment renders with [auto] prefix.
    assert "[[no-desc-page]]" in body
    assert "[auto]" in body


def test_build_wiki_index_skips_meta_files(tmp_path: Path):
    """AGENTS.md, log.md, and an existing index.md at wiki root are
    documentation about the wiki, not catalog entries — must not show
    up in the auto-regen output."""
    wiki = tmp_path / "state" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "AGENTS.md").write_text("<!-- desc: ingest conventions -->\nbody")
    (wiki / "log.md").write_text("<!-- desc: ingest log -->\nbody")
    (wiki / "index.md").write_text("# stale hand-curated\n")
    body = build_wiki_index(tmp_path)

    assert "AGENTS.md" not in body
    assert "log.md" not in body
    # The previously hand-curated index.md doesn't list itself.
    assert "[[index]]" not in body


def test_build_wiki_index_omits_empty_sections(tmp_path: Path):
    """A fresh agent with only entities seeded shouldn't see empty
    Concepts/Topics headers cluttering the file."""
    wiki = tmp_path / "state" / "wiki"
    (wiki / "entities").mkdir(parents=True)
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "topics").mkdir(parents=True)
    (wiki / "entities" / "only.md").write_text("<!-- desc: lone entity -->\n")

    body = build_wiki_index(tmp_path)
    assert "## Entities" in body
    assert "## Concepts" not in body
    assert "## Topics" not in body


def test_build_wiki_index_handles_no_pages(tmp_path: Path):
    """Empty wiki tree renders the header with the no-pages marker
    instead of a bare blank index."""
    body = build_wiki_index(tmp_path)
    assert "Wiki Index" in body
    assert "(no pages yet)" in body


@pytest.mark.asyncio
async def test_generator_writes_wiki_index_on_flush(tmp_path: Path):
    _seed_memory(tmp_path)
    _seed_wiki(tmp_path)

    gen = IndexGenerator(tmp_path)
    gen.mark_dirty("all")
    await gen.flush()

    wiki_idx = (tmp_path / "state" / "wiki" / "index.md").read_text()
    assert "## Entities" in wiki_idx
    assert "[[alice]]" in wiki_idx


@pytest.mark.asyncio
async def test_generator_wiki_scope_isolated(tmp_path: Path):
    """mark_dirty('wiki') writes only the wiki index — memory and
    state indexes stay untouched. Mirrors the per-scope-isolation
    contract the existing memory/state scopes already enforce."""
    _seed_memory(tmp_path)
    _seed_wiki(tmp_path)
    (tmp_path / "state" / "doc.md").write_text("# doc")

    gen = IndexGenerator(tmp_path)
    gen.mark_dirty("wiki")
    await gen.flush()

    assert (tmp_path / "state" / "wiki" / "index.md").exists()
    assert not (tmp_path / "memory" / "INDEX.md").exists()
    assert not (tmp_path / "state" / "INDEX.md").exists()


@pytest.mark.asyncio
async def test_generator_wiki_overwrites_hand_edits(tmp_path: Path):
    """Hand-edits to wiki/index.md get overwritten on next flush —
    same contract as memory/INDEX.md and state/INDEX.md. Resolves
    the drift-amplifier flagged in Phase 1 audit finding #4."""
    _seed_wiki(tmp_path)
    wiki_idx = tmp_path / "state" / "wiki" / "index.md"
    wiki_idx.write_text("# Hand-curated content that should be wiped.\n")

    gen = IndexGenerator(tmp_path)
    gen.mark_dirty("wiki")
    await gen.flush()

    body = wiki_idx.read_text()
    assert "Hand-curated" not in body
    assert "Wiki Index" in body  # auto-regen header replaces it


# ── skills-catalog auto-regen (chainlink #109) ─────────────────────


def _make_fake_skills_root(root: Path) -> Path:
    """Seed a minimal skills root with one SKILL.md so catalog generation
    produces deterministic non-empty output."""
    skills_root = root / "skills"
    skill_dir = skills_root / "dummy-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: dummy-skill\n"
        "description: Use when you need a dummy skill for testing.\n"
        "allowed-tools: Read\n"
        "---\n"
        "<!-- desc: a dummy test skill -->\n"
        "# Dummy Skill\n"
        "Body.\n",
        encoding="utf-8",
    )
    return skills_root


@pytest.mark.asyncio
async def test_memory_flush_creates_skills_catalog_when_missing(tmp_path: Path):
    """When memory/skills-catalog.md doesn't exist, _write_memory creates it."""
    _seed_memory(tmp_path)
    skills_root = _make_fake_skills_root(tmp_path)
    gen = IndexGenerator(tmp_path, skills_root=skills_root)
    gen.mark_dirty("memory")
    await gen.flush()

    catalog = tmp_path / "memory" / "skills-catalog.md"
    assert catalog.is_file(), "skills-catalog.md should be created"
    body = catalog.read_text()
    assert "dummy-skill" in body
    assert "<!-- desc:" in body


@pytest.mark.asyncio
async def test_memory_flush_updates_stale_skills_catalog(tmp_path: Path):
    """When skills-catalog.md exists but is stale, _write_memory overwrites it."""
    _seed_memory(tmp_path)
    skills_root = _make_fake_skills_root(tmp_path)
    catalog = tmp_path / "memory" / "skills-catalog.md"
    catalog.write_text("<!-- desc: old stale content -->\nstale", encoding="utf-8")

    gen = IndexGenerator(tmp_path, skills_root=skills_root)
    gen.mark_dirty("memory")
    await gen.flush()

    body = catalog.read_text()
    assert "dummy-skill" in body
    assert "stale" not in body


@pytest.mark.asyncio
async def test_memory_flush_skips_write_when_catalog_already_current(tmp_path: Path):
    """When skills-catalog.md already matches the fresh generation, no write occurs."""
    _seed_memory(tmp_path)
    skills_root = _make_fake_skills_root(tmp_path)

    # First flush — creates the catalog
    gen = IndexGenerator(tmp_path, skills_root=skills_root)
    gen.mark_dirty("memory")
    await gen.flush()

    catalog = tmp_path / "memory" / "skills-catalog.md"
    mtime_after_first_flush = catalog.stat().st_mtime_ns

    # Second flush — catalog is already current; mtime should NOT change
    gen.mark_dirty("memory")
    await gen.flush()

    assert catalog.stat().st_mtime_ns == mtime_after_first_flush, (
        "skills-catalog.md should not be rewritten when already current"
    )
