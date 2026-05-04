"""INDEX.md generation (SPEC §3.4, §6.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.index import IndexGenerator, build_memory_index, build_state_index


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
