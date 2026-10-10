"""Scratch must not be promoted through live loaders or cached search rows."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir._paths import live_loader_path_allowed
from mimir.index import (
    IndexGenerator,
    _build_entries,
    build_memory_index,
    build_state_index,
    build_wiki_index,
)
from mimir.repository_config import RepositoryInventory
from mimir.search import HashEmbedder, Indexer
from mimir.wiki_backlinks import build_graph, build_wiki_payload, find_pages, find_slug_collisions


@pytest.fixture
def reject_scratch_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove exclusion happens before reading, not after parsing the payload."""
    for name in ("read_text", "read_bytes"):
        original = getattr(Path, name)

        def checked(path: Path, *args, _original=original, **kwargs):
            assert live_loader_path_allowed(path), f"scratch source read: {path}"
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, checked)


@pytest.mark.parametrize(
    "shape", ["live", "live-root", "live-leaf", "scratch", "root", "leaf", "dotdot", "outward"],
)
@pytest.mark.parametrize("loader", ["memory", "state", "wiki", "backlinks", "search"])
def test_content_loaders_exclude_scratch(
    tmp_path: Path, shape: str, loader: str, reject_scratch_reads: None,
) -> None:
    home = tmp_path / "home"
    scratch = home / "scratch"
    scratch.mkdir(parents=True)
    roots = {
        "memory": Path("memory"),
        "state": Path("state"),
        "wiki": Path("state/wiki/entities"),
        "backlinks": Path("state/wiki"),
        "search": Path("state"),
    }
    relative_root = roots[loader]
    live_root = home / relative_root
    live_root.mkdir(parents=True)
    filename = "sentinel.md"
    text = "<!-- desc: closure sentinel -->\n# Closure sentinel\n[[missing-sentinel]]\n"
    if shape == "live":
        (live_root / filename).write_text(text)
    elif shape in {"live-root", "live-leaf"}:
        # Memory's existing reference-integrity policy requires a trusted
        # HOME subtree as well as a non-scratch target.
        target_root = home / ("state" if loader == "memory" else "memory") / "live-target"
        target_root.mkdir(parents=True)
        target = target_root / filename
        target.write_text(text)
        if shape == "live-root":
            live_root.rmdir()
            live_root.symlink_to(target_root, target_is_directory=True)
        else:
            (live_root / filename).symlink_to(target)
    elif shape == "leaf":
        target = scratch / filename
        target.write_text(text)
        (live_root / filename).symlink_to(target)
    elif shape == "root":
        target = scratch / "pages"
        target.mkdir()
        (target / filename).write_text(text)
        live_root.rmdir()
        live_root.symlink_to(target, target_is_directory=True)
    elif shape == "scratch":
        home = scratch
        live_root = home / relative_root
        live_root.mkdir(parents=True)
        (live_root / filename).write_text(text)
    elif shape == "dotdot":
        # Lexically outside scratch, but symlink traversal before '..'
        # resolves the supplied home into scratch.
        nested = scratch / "nested"
        nested.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        home = alias / ".."
        live_root = scratch / relative_root
        live_root.mkdir(parents=True)
        (live_root / filename).write_text(text)
    else:
        # Lexical scratch is denied even when its leaf resolves to live.
        outside = tmp_path / "live-pages"
        outside.mkdir()
        (outside / filename).write_text(text)
        home = scratch
        live_root = home / relative_root
        live_root.parent.mkdir(parents=True, exist_ok=True)
        live_root.symlink_to(outside, target_is_directory=True)

    expected = shape in {"live", "live-root", "live-leaf"}
    if loader == "memory":
        body = build_memory_index(home)
        assert ("sentinel.md" in body) == expected
        assert ("closure sentinel" in body) == expected
    elif loader == "state":
        body = build_state_index(home)
        assert ("sentinel.md" in body) == expected
        assert ("closure sentinel" in body) == expected
    elif loader == "wiki":
        body = build_wiki_index(home)
        assert ("sentinel.md" in body) == expected
        assert ("closure sentinel" in body) == expected
    elif loader == "backlinks":
        wiki = home / "state/wiki"
        assert bool(find_pages(wiki)) == expected
        assert bool(build_graph(wiki).pages) == expected
        assert bool(build_graph(wiki).dangling) == expected
        assert bool(build_wiki_payload(wiki)["pages"]) == expected
        assert find_slug_collisions(wiki) == {}
    else:
        indexer = Indexer(home, embedder=HashEmbedder(), db_path=tmp_path / "index.db")
        indexer.init_schema()
        # Search indexes both memory/ and state/: a live alias exposes the
        # canonical memory target as well as the state alias, unlike scratch.
        expected_paths = {"state/sentinel.md"} if expected else set()
        if shape in {"live-root", "live-leaf"}:
            expected_paths.add("memory/live-target/sentinel.md")
        assert indexer._sweep_sync()["added"] == len(expected_paths)
        with indexer._connect() as conn:
            assert {row[0] for row in conn.execute("SELECT path FROM files")} == expected_paths
        assert indexer._reindex_sync("state/sentinel.md") == expected
        assert bool(indexer._search_sync("sentinel", [0.0] * 16, "all", 5, 50)) == expected


@pytest.mark.parametrize("shape", ["live", "missing-live", "scratch", "root", "leaf", "dotdot", "dangling", "outward"])
def test_repository_inventory_fails_closed(
    tmp_path: Path, shape: str, reject_scratch_reads: None,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    path = live / "repositories.yaml"
    content = "repositories: []\nallowed_roots: []\n"
    if shape == "live":
        path.write_text(content)
    elif shape == "missing-live":
        pass
    elif shape == "scratch":
        path = scratch / path.name
        path.write_text(content)
    elif shape == "root":
        live.rmdir()
        live.symlink_to(scratch, target_is_directory=True)
        (scratch / path.name).write_text(content)
    elif shape in {"leaf", "dangling"}:
        target = scratch / path.name
        if shape == "leaf":
            target.write_text(content)
        path.symlink_to(target)
    elif shape == "dotdot":
        nested = scratch / "nested"
        nested.mkdir()
        alias = live / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        (scratch / path.name).write_text(content)
        path = alias / ".." / path.name
    else:
        path.write_text(content)
        alias = scratch / path.name
        alias.symlink_to(path)
        path = alias

    if shape in {"live", "missing-live"}:
        inventory = RepositoryInventory.load(path)
        assert inventory.declared == (shape == "live")
    else:
        with pytest.raises(ValueError, match="cannot be loaded from scratch"):
            RepositoryInventory.load(path)


@pytest.mark.parametrize("operation", ["reindex", "sweep", "search"])
@pytest.mark.parametrize("shape", ["root", "leaf"])
def test_search_evicts_all_existing_rows_after_scratch_retarget(
    tmp_path: Path, operation: str, shape: str, reject_scratch_reads: None,
) -> None:
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    path = state / "sentinel.md"
    path.write_text("closure sentinel cached content")
    indexer = Indexer(home, embedder=HashEmbedder())
    indexer.init_schema()
    assert indexer._reindex_sync("state/sentinel.md")
    scratch = home / "scratch"
    scratch.mkdir()
    target = scratch / path.name
    target.write_text("closure sentinel scratch content")
    path.unlink()
    if shape == "root":
        state.rmdir()
        state.symlink_to(scratch, target_is_directory=True)
    else:
        path.symlink_to(target)

    if operation == "reindex":
        assert not indexer._reindex_sync("state/sentinel.md")
    elif operation == "sweep":
        assert indexer._sweep_sync() == {"added": 0, "updated": 0, "removed": 1}
    else:
        assert indexer._search_sync("sentinel", [0.0] * 16, "all", 5, 50) == []
    with indexer._connect() as conn:
        for table in ("files", "chunks", "chunks_fts"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_direct_entry_builder_and_cached_index_reject_scratch(
    tmp_path: Path, reject_scratch_reads: None,
) -> None:
    home = tmp_path / "home"
    memory = home / "memory"
    memory.mkdir(parents=True)
    scratch = home / "scratch"
    scratch.mkdir()
    target = scratch / "payload.md"
    target.write_text("scratch sentinel")
    leaf = memory / "leaf.md"
    leaf.symlink_to(target)
    (memory / "INDEX.md").symlink_to(target)
    assert _build_entries(memory, [leaf]) == []
    assert "scratch sentinel" not in IndexGenerator(home).read_memory_index()


def test_scratch_skip_override_is_not_consumed(
    tmp_path: Path, reject_scratch_reads: None,
) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "state/live.md").write_text("live sentinel")
    (home / ".mimir").mkdir()
    (home / "scratch").mkdir()
    target = home / "scratch/skip.txt"
    target.write_text("state/live.md\n")
    (home / ".mimir/index-skip.txt").symlink_to(target)
    assert "state/live.md" not in build_state_index(home)  # entries are state-relative
    assert "live.md" in build_state_index(home)
    indexer = Indexer(home, embedder=HashEmbedder())
    indexer.init_schema()
    assert indexer._reindex_sync("state/live.md")


@pytest.mark.parametrize("shape", ["live", "root", "leaf", "scratch"])
def test_search_database_is_a_guarded_live_source(tmp_path: Path, shape: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    scratch = home / "scratch"
    scratch.mkdir()
    db = home / "index.db"
    if shape == "scratch":
        db = scratch / "index.db"
    elif shape == "leaf":
        db.symlink_to(scratch / "index.db")
    elif shape == "root":
        alias = home / "db-root"
        alias.symlink_to(scratch, target_is_directory=True)
        db = alias / "index.db"
    indexer = Indexer(home, embedder=HashEmbedder(), db_path=db)
    if shape == "live":
        indexer.init_schema()
        assert indexer._stats_sync().files == 0
    else:
        with pytest.raises(ValueError, match="database cannot be loaded from scratch"):
            indexer.init_schema()
        assert not (scratch / "index.db").exists()


def test_scratch_pages_do_not_create_slug_collisions(
    tmp_path: Path, reject_scratch_reads: None,
) -> None:
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    (wiki / "topics").mkdir()
    (wiki / "entities/sentinel.md").write_text("# Live\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = scratch / "sentinel.md"
    target.write_text("# Scratch\n[[scratch-dangling]]")
    (wiki / "topics/sentinel.md").symlink_to(target)
    assert find_slug_collisions(wiki) == {}
    graph = build_graph(wiki)
    assert graph.collisions == {}
    assert graph.dangling == []
    assert set(graph.pages) == {"entities/sentinel.md"}


@pytest.mark.parametrize("shape", ["root", "leaf", "dotdot"])
def test_repository_inventory_accepts_live_aliases(tmp_path: Path, shape: str) -> None:
    live = tmp_path / "live"
    live.mkdir()
    target = live / "repositories.yaml"
    target.write_text("repositories: []\n")
    if shape == "leaf":
        path = tmp_path / "alias.yaml"
        path.symlink_to(target)
    else:
        alias = tmp_path / "alias"
        if shape == "dotdot":
            nested = live / "nested"
            nested.mkdir()
            alias.symlink_to(nested, target_is_directory=True)
            path = alias / ".." / target.name
        else:
            alias.symlink_to(live, target_is_directory=True)
            path = alias / target.name
    assert RepositoryInventory.load(path).declared
