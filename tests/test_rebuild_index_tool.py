"""Tests for the rebuild_index MCP tool (chainlink #144).

The tool is a thin async wrapper around IndexGenerator.mark_dirty +
IndexGenerator.flush. The IndexGenerator (mimir.index.IndexGenerator)
manages the human-readable INDEX.md files; it is distinct from the
search Indexer (mimir.search.Indexer) that powers file_search.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mimir.index import IndexGenerator
from mimir.tools.extra import _INDEX_GEN_STATE, rebuild_index, set_index_generator


# ── helpers ───────────────────────────────────────────────────────────────────


def _seed(home: Path) -> None:
    """Populate a minimal MIMIR_HOME so INDEX files can be built."""
    (home / "memory" / "core").mkdir(parents=True)
    (home / "state" / "wiki" / "concepts").mkdir(parents=True)
    (home / "memory" / "core" / "00-persona.md").write_text(
        "<!-- desc: persona -->\n# Persona"
    )
    (home / "state" / "wiki" / "concepts" / "variety.md").write_text(
        "<!-- desc: variety concept -->\n# Variety"
    )


def _make_generator(home: Path) -> IndexGenerator:
    return IndexGenerator(home)


# ── error path ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rebuild_index_no_generator_returns_error():
    """rebuild_index fails gracefully when no IndexGenerator is injected."""
    original = _INDEX_GEN_STATE["generator"]
    try:
        set_index_generator(None)
        result = await rebuild_index.ainvoke({"scope": "all"})
        assert "rebuild_index failed" in result
        assert "no IndexGenerator" in result
    finally:
        _INDEX_GEN_STATE["generator"] = original


@pytest.mark.asyncio
async def test_rebuild_index_invalid_scope_returns_error(tmp_path: Path):
    """Unknown scope returns an error string naming the bad value."""
    _seed(tmp_path)
    gen = _make_generator(tmp_path)
    set_index_generator(gen)
    try:
        result = await rebuild_index.ainvoke({"scope": "bogus"})
        assert "rebuild_index failed" in result
        assert "bogus" in result
    finally:
        set_index_generator(None)


# ── scope variants ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rebuild_index_scope_memory(tmp_path: Path):
    """scope='memory' creates memory/INDEX.md but NOT state/INDEX.md."""
    _seed(tmp_path)
    gen = _make_generator(tmp_path)
    set_index_generator(gen)
    memory_index = tmp_path / "memory" / "INDEX.md"
    state_index = tmp_path / "state" / "INDEX.md"
    try:
        result = await rebuild_index.ainvoke({"scope": "memory"})
        assert result == "rebuild_index ok: scope=memory"
        assert memory_index.exists(), "memory/INDEX.md should have been created"
        assert not state_index.exists(), "state/INDEX.md must NOT be created"
    finally:
        set_index_generator(None)


@pytest.mark.asyncio
async def test_rebuild_index_scope_state(tmp_path: Path):
    """scope='state' creates state/INDEX.md but NOT memory/INDEX.md."""
    _seed(tmp_path)
    gen = _make_generator(tmp_path)
    set_index_generator(gen)
    memory_index = tmp_path / "memory" / "INDEX.md"
    state_index = tmp_path / "state" / "INDEX.md"
    try:
        result = await rebuild_index.ainvoke({"scope": "state"})
        assert result == "rebuild_index ok: scope=state"
        assert state_index.exists(), "state/INDEX.md should have been created"
        assert not memory_index.exists(), "memory/INDEX.md must NOT be created"
    finally:
        set_index_generator(None)


@pytest.mark.asyncio
async def test_rebuild_index_scope_all(tmp_path: Path):
    """scope='all' (default) creates both memory/INDEX.md and state/INDEX.md."""
    _seed(tmp_path)
    gen = _make_generator(tmp_path)
    set_index_generator(gen)
    memory_index = tmp_path / "memory" / "INDEX.md"
    state_index = tmp_path / "state" / "INDEX.md"
    try:
        result = await rebuild_index.ainvoke({"scope": "all"})
        assert result == "rebuild_index ok: scope=all"
        assert memory_index.exists(), "memory/INDEX.md should exist after scope=all"
        assert state_index.exists(), "state/INDEX.md should exist after scope=all"
    finally:
        set_index_generator(None)


@pytest.mark.asyncio
async def test_rebuild_index_default_scope_is_all(tmp_path: Path):
    """Calling rebuild_index with no scope argument defaults to 'all'."""
    _seed(tmp_path)
    gen = _make_generator(tmp_path)
    set_index_generator(gen)
    memory_index = tmp_path / "memory" / "INDEX.md"
    state_index = tmp_path / "state" / "INDEX.md"
    try:
        result = await rebuild_index.ainvoke({})
        assert result == "rebuild_index ok: scope=all"
        assert memory_index.exists()
        assert state_index.exists()
    finally:
        set_index_generator(None)


# ── registry membership ───────────────────────────────────────────────────────


def test_rebuild_index_in_all_mimir_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """rebuild_index is an unconditional member of all_mimir_tools().

    Acceptance criterion (d) from chainlink #144.
    """
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("FETCH_URL_ENABLED", "0")
    from mimir.tools import all_mimir_tools

    names = {t.name for t in all_mimir_tools()}
    assert "rebuild_index" in names, (
        "rebuild_index must appear in all_mimir_tools() "
        "(chainlink #144 acceptance criterion d)"
    )
