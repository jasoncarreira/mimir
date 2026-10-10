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
    """Prove existing scratch payloads are excluded before being read."""
    for name in ("read_text", "read_bytes"):
        original = getattr(Path, name)

        def checked(path: Path, *args, _original=original, **kwargs):
            # Missing optional files carry no payload. A loader given a
            # subtree as its explicit home may probe its own index-skip.txt;
            # that re-anchors the helper, but must not read existing scratch.
            if path.exists():
                assert live_loader_path_allowed(path), f"scratch source read: {path}"
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, checked)


def test_live_wiki_directory_named_scratch_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reject_scratch_reads: None,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    wiki = home / "state/wiki"
    page = wiki / "scratch/page.md"
    page.parent.mkdir(parents=True)
    page.write_text("<!-- desc: live scratch-named page -->\n# Live page\n")
    assert live_loader_path_allowed(page, home)
    assert live_loader_path_allowed(page)
    assert set(find_pages(wiki)) == {"scratch/page.md"}
    assert set(build_graph(wiki).pages) == {"scratch/page.md"}
    assert "wiki/scratch/page.md" in build_state_index(home)
    indexer = Indexer(home, embedder=HashEmbedder())
    indexer.init_schema()
    assert indexer._reindex_sync("state/wiki/scratch/page.md")
    assert indexer._stats_sync().files == 1


def test_live_skill_named_scratch_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reject_scratch_reads: None,
) -> None:
    from mimir.skill_catalog import load_skill

    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    skill = home / "skills/scratch"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: scratch\ndescription: A live skill named scratch\n---\n# Live skill\n"
    )
    loaded = load_skill(skill)
    assert loaded is not None
    assert loaded.name == "scratch"


@pytest.mark.parametrize("explicit_home", [False, True])
def test_live_symlink_resolving_into_configured_scratch_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_home: bool,
    reject_scratch_reads: None,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    target = home / "scratch/page.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Quarantined page\n")
    wiki = home / "state/wiki"
    wiki.mkdir(parents=True)
    alias = wiki / "page.md"
    alias.symlink_to(target)
    assert not alias.is_relative_to(home / "scratch")
    assert alias.resolve().is_relative_to(home / "scratch")
    # Removing the resolved-path check must fail this assertion, even though
    # the alias's lexical spelling is an otherwise valid live wiki path.
    assert not live_loader_path_allowed(alias, home if explicit_home else None)
    assert find_pages(wiki) == {}


@pytest.mark.parametrize("explicit_home", [False, True])
def test_lexical_scratch_alias_to_live_outside_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_home: bool,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    target = home / "state/wiki/page.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Live page\n")
    alias = home / "scratch/alias.md"
    alias.parent.mkdir()
    alias.symlink_to(target)
    boundary_home = home if explicit_home else None
    assert live_loader_path_allowed(alias.resolve(), boundary_home)
    assert not live_loader_path_allowed(alias, boundary_home)


def test_explicit_loader_home_takes_precedence_over_mimir_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = tmp_path / "explicit"
    configured = tmp_path / "configured"
    monkeypatch.setenv("MIMIR_HOME", str(configured))
    assert not live_loader_path_allowed(explicit / "scratch/page.md", explicit)
    assert live_loader_path_allowed(configured / "scratch/page.md", explicit)
    assert not live_loader_path_allowed(configured / "scratch/page.md")
    # A supplied home is never implicitly replaced by its parent merely
    # because its name is scratch.
    assert live_loader_path_allowed(explicit / "scratch/page.md", explicit / "scratch")
    assert not live_loader_path_allowed(
        explicit / "scratch/scratch/page.md", explicit / "scratch"
    )


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_unconfigured_loader_does_not_ban_scratch_path_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str | None,
) -> None:
    if configured is None:
        monkeypatch.delenv("MIMIR_HOME", raising=False)
    else:
        monkeypatch.setenv("MIMIR_HOME", configured)
    assert live_loader_path_allowed(tmp_path / "scratch/page.md")


@pytest.mark.parametrize(
    "shape", ["live", "live-root", "live-leaf", "scratch", "root", "leaf", "dotdot", "outward"],
)
@pytest.mark.parametrize("loader", ["memory", "state", "wiki", "backlinks", "search"])
def test_content_loaders_exclude_scratch(
    tmp_path: Path, shape: str, loader: str, reject_scratch_reads: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    configured_home = home
    monkeypatch.setenv("MIMIR_HOME", str(configured_home))
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
    # Even when a loader receives home/scratch, the configured home stays
    # anchored to its parent; do not infer a new boundary from the subtree.
    assert live_loader_path_allowed(home / relative_root / filename) == expected
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
        # Indexer's home is the configured boundary, not a loader subtree.
        # Passing scratch as its explicit home would intentionally configure
        # scratch/scratch instead, overriding MIMIR_HOME.
        indexer = Indexer(configured_home, embedder=HashEmbedder(), db_path=tmp_path / "index.db")
        indexer.init_schema()
        # Search indexes both memory/ and state/: a live alias exposes the
        # canonical memory target as well as the state alias, unlike scratch.
        expected_paths = {"state/sentinel.md"} if expected else set()
        if shape in {"live-root", "live-leaf"}:
            expected_paths.add("memory/live-target/sentinel.md")
        assert indexer._sweep_sync()["added"] == len(expected_paths)
        with indexer._connect() as conn:
            assert {row[0] for row in conn.execute("SELECT path FROM files")} == expected_paths
        rel = (
            "scratch/state/sentinel.md"
            if shape in {"scratch", "dotdot", "outward"}
            else "state/sentinel.md"
        )
        assert indexer._abs_path(rel).is_file()
        assert indexer._reindex_sync(rel) == expected
        assert bool(indexer._search_sync("sentinel", [0.0] * 16, "all", 5, 50)) == expected


@pytest.mark.parametrize("shape", ["live", "missing-live", "scratch", "root", "leaf", "dotdot", "dangling", "outward"])
def test_repository_inventory_fails_closed(
    tmp_path: Path, shape: str, reject_scratch_reads: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
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
def test_search_database_is_a_guarded_live_source(
    tmp_path: Path, shape: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
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
def test_repository_inventory_accepts_live_aliases(
    tmp_path: Path, shape: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
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
