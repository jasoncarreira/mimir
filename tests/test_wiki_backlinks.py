"""Tests for the wiki backlinks tool (`mimir wiki backlinks`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.event_logger import init_logger
from mimir.wiki_backlinks import (
    build_graph,
    build_wiki_payload,
    extract_links,
    find_pages,
    render_backlinks_index_md,
    render_dangling_md,
    render_orphans_md,
    run,
)


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    """Standard fixture: ``state/wiki/`` skeleton with a few empty
    category dirs. Tests fill in pages as needed."""
    wd = tmp_path / "state" / "wiki"
    (wd / "concepts").mkdir(parents=True)
    (wd / "topics").mkdir()
    (wd / "entities").mkdir()
    return wd


@pytest.fixture
def home(tmp_path: Path, wiki: Path) -> Path:
    """The MIMIR_HOME root — wiki is at ``home/state/wiki/``. Initializes
    the event logger so ``run()`` can emit ``wiki_backlinks_unhealthy``
    without crashing on no-logger."""
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-wiki")
    return tmp_path


def _write(wd: Path, rel: str, content: str) -> None:
    p = wd / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _read_events(home: Path) -> list[dict]:
    path = home / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ─── extract_links ───────────────────────────────────────────────────


def test_extract_links_basic():
    out = list(extract_links("see [[stigmergy]] for context"))
    assert out == [(1, "stigmergy")]


def test_extract_links_strips_display_text():
    out = list(extract_links("see [[stigmergy|swarm coordination]] for context"))
    assert out == [(1, "stigmergy")]


def test_extract_links_strips_heading_locator():
    out = list(extract_links("see [[stigmergy#origins]] for context"))
    assert out == [(1, "stigmergy")]


def test_extract_links_strips_block_locator():
    out = list(extract_links("see [[stigmergy^def-1]] for context"))
    assert out == [(1, "stigmergy")]


def test_extract_links_multiple_per_line():
    out = list(extract_links("[[a]] and [[b|alt]] and [[c#h]]"))
    assert out == [(1, "a"), (1, "b"), (1, "c")]


def test_extract_links_tracks_line_numbers():
    text = "first\n[[a]]\n\n[[b]]"
    out = list(extract_links(text))
    assert out == [(2, "a"), (4, "b")]


def test_extract_links_tolerates_inner_whitespace():
    out = list(extract_links("[[ stigmergy ]]"))
    assert out == [(1, "stigmergy")]


def test_extract_links_ignores_single_brackets():
    """``[link]`` (markdown link) and ``[ ]`` (checkbox) should NOT
    match. Only double-bracket wikilinks count."""
    out = list(extract_links("[markdown](url) and [ ] checkbox"))
    assert out == []


def test_extract_links_strips_md_extension():
    """Per Obsidian convention, ``[[name.md]]`` and ``[[name]]`` are
    equivalent. Without normalization, the explicit-extension form
    would show up as a dangling link to literal ``name.md`` while the
    real ``name`` page would be flagged as orphan."""
    assert list(extract_links("[[stigmergy.md]]")) == [(1, "stigmergy")]
    # Combined with display text + locator:
    assert list(extract_links("[[stigmergy.md|swarms]]")) == [(1, "stigmergy")]
    assert list(extract_links("[[stigmergy.md#origins]]")) == [(1, "stigmergy")]


def test_extract_links_strips_category_prefix():
    """Wiki pages live under ``concepts/`` / ``topics/`` / ``entities/``
    subdirs (per the wiki SKILL.md layout). Slug-based resolution
    treats ``[[concepts/foo]]`` as equivalent to ``[[foo]]``. Without
    this normalization the prefixed form gets recorded as a dangling
    link to literal ``"concepts/foo"`` AND the real ``foo`` page gets
    falsely flagged as orphan — the bug muninn-mimir's wiki-health
    report surfaced on 2026-05-23."""
    assert list(extract_links("[[concepts/stigmergy]]")) == [(1, "stigmergy")]
    assert list(extract_links("[[topics/mempalace]]")) == [(1, "mempalace")]
    assert list(extract_links("[[entities/penny]]")) == [(1, "penny")]


def test_extract_links_prefix_combined_with_md_extension():
    """Both ``[[concepts/foo.md]]`` and ``[[concepts/foo]]`` should
    normalize to ``"foo"``. Order: strip ``.md`` first, then category
    prefix — works in both orderings, but pinning this ensures the
    interaction is tested."""
    assert list(extract_links("[[concepts/stigmergy.md]]")) == [(1, "stigmergy")]
    assert list(extract_links("[[topics/foo.md|display]]")) == [(1, "foo")]
    assert list(extract_links("[[entities/bar.md#origins]]")) == [(1, "bar")]


def test_extract_links_no_prefix_left_alone():
    """Targets without a category prefix pass through unchanged —
    we should not accidentally strip arbitrary leading path-like
    fragments (e.g. ``random/foo`` keeps its prefix because
    ``random/`` isn't a known category)."""
    assert list(extract_links("[[random/foo]]")) == [(1, "random/foo")]
    # Bare slug unchanged.
    assert list(extract_links("[[just_a_slug]]")) == [(1, "just_a_slug")]


# ─── find_pages ──────────────────────────────────────────────────────


def test_find_pages_walks_subdirectories(wiki: Path):
    _write(wiki, "concepts/stigmergy.md", "# Stigmergy")
    _write(wiki, "topics/mempalace.md", "# Mempalace")
    _write(wiki, "entities/penny.md", "# Penny")

    # Path-keyed: each markdown file gets a unique entry by its
    # relative POSIX path string. Slug lives in build_graph's slug
    # index now.
    pages = find_pages(wiki)
    assert set(pages) == {
        "concepts/stigmergy.md",
        "topics/mempalace.md",
        "entities/penny.md",
    }
    assert pages["concepts/stigmergy.md"] == Path("concepts/stigmergy.md")


def test_find_pages_skips_meta_files(wiki: Path):
    _write(wiki, "AGENTS.md", "schema")
    _write(wiki, "index.md", "# Index")
    _write(wiki, "log.md", "log")
    _write(wiki, "concepts/real.md", "# Real")
    # The tool's own outputs must also be excluded so they don't show
    # up as orphans on the next run.
    _write(wiki, "orphans.md", "stale prior run")
    _write(wiki, "dangling-links.md", "stale")
    _write(wiki, "backlinks-index.md", "stale")
    _write(wiki, "unwired.md", "llm-wiki priority list")

    pages = find_pages(wiki)
    assert set(pages) == {"concepts/real.md"}


def test_find_pages_handles_missing_wiki_dir(tmp_path: Path):
    # A home with no wiki at all → empty page set, no error.
    assert find_pages(tmp_path / "state" / "wiki") == {}


# ─── build_graph ─────────────────────────────────────────────────────


def test_build_graph_inbound_outbound(wiki: Path):
    _write(wiki, "concepts/stigmergy.md", "# Stigmergy\n\nSee [[boids]].")
    _write(wiki, "concepts/boids.md", "# Boids\n\nRelated: [[stigmergy]].")
    _write(wiki, "topics/orphan.md", "# Orphan with no links in or out")

    graph = build_graph(wiki)

    # Path-keyed: ``pages`` indexed by relative POSIX path. ``inbound``
    # entries are source paths, ``outbound`` entries are target slugs
    # (the wikilink form).
    assert graph.pages["concepts/stigmergy.md"]["outbound"] == ["boids"]
    assert graph.pages["concepts/stigmergy.md"]["inbound"] == [
        "concepts/boids.md",
    ]
    assert graph.pages["concepts/boids.md"]["inbound"] == [
        "concepts/stigmergy.md",
    ]
    assert graph.pages["topics/orphan.md"]["inbound"] == []
    assert graph.orphans == ["topics/orphan.md"]
    assert graph.dangling == []


def test_build_graph_dangling_link(wiki: Path):
    _write(
        wiki,
        "concepts/foo.md",
        "Linking to [[real-page]] and [[ghost-page]].",
    )
    _write(wiki, "concepts/real-page.md", "# Real")

    graph = build_graph(wiki)
    assert graph.pages["concepts/foo.md"]["inbound"] == []
    assert graph.pages["concepts/real-page.md"]["inbound"] == [
        "concepts/foo.md",
    ]
    assert len(graph.dangling) == 1
    d = graph.dangling[0]
    assert d["target"] == "ghost-page"
    assert d["source"] == "concepts/foo.md"
    assert d["line"] == 1


def test_build_graph_self_link_does_not_count_as_inbound(wiki: Path):
    """A page linking to itself shouldn't show up in its own inbound
    list — that's not 'another page supports this'."""
    _write(wiki, "concepts/lonely.md", "I link to [[lonely]] which is myself.")
    graph = build_graph(wiki)
    assert graph.pages["concepts/lonely.md"]["inbound"] == []
    assert graph.orphans == ["concepts/lonely.md"]


def test_build_graph_dedups_repeated_inbound(wiki: Path):
    """If page A links to B three times, B's inbound should list A once."""
    _write(wiki, "concepts/a.md", "[[b]] and [[b]] and [[b]] again")
    _write(wiki, "concepts/b.md", "# B")
    graph = build_graph(wiki)
    assert graph.pages["concepts/b.md"]["inbound"] == ["concepts/a.md"]


def test_build_graph_cross_category_collision_resolves_to_both(wiki: Path):
    """Path-key refactor regression: ``concepts/foo.md`` and
    ``topics/foo.md`` both exist; a third page links ``[[foo]]``.

    Pre-refactor the slug-keyed map would have last-wins-dropped one
    of them in ``find_pages``, so the inbound link would attach to
    whichever scan order survived. Post-refactor both pages get
    the inbound entry."""
    _write(wiki, "concepts/foo.md", "# Foo (concept)")
    _write(wiki, "topics/foo.md", "# Foo (topic)")
    _write(wiki, "concepts/linker.md", "See [[foo]] for details.")

    graph = build_graph(wiki)

    # Both ``foo.md`` pages exist as separate entries.
    assert "concepts/foo.md" in graph.pages
    assert "topics/foo.md" in graph.pages
    # Both receive the inbound link (no silent conflation).
    assert graph.pages["concepts/foo.md"]["inbound"] == [
        "concepts/linker.md",
    ]
    assert graph.pages["topics/foo.md"]["inbound"] == [
        "concepts/linker.md",
    ]
    # Collision is still surfaced for the operator's wiki-health view.
    assert "foo" in graph.collisions
    assert len(graph.collisions["foo"]) == 2


def test_build_graph_collision_pages_can_link_to_each_other(wiki: Path):
    """The pre-refactor self-link guard used ``target_slug != source_slug``,
    which silently dropped genuine cross-category same-stem links
    (concepts/foo.md → topics/foo.md). Path-key refactor: self-link
    check is now path-based, so the cross-category link is preserved."""
    _write(wiki, "concepts/foo.md", "I link to [[foo]] — the other one.")
    _write(wiki, "topics/foo.md", "# Foo (topic)")

    graph = build_graph(wiki)

    # The link from concepts/foo.md → [[foo]] resolves to BOTH
    # concepts/foo.md (self — dropped) AND topics/foo.md (kept).
    assert graph.pages["concepts/foo.md"]["inbound"] == []  # self-link
    assert graph.pages["topics/foo.md"]["inbound"] == [
        "concepts/foo.md",
    ]


def test_build_wiki_payload_returns_json_friendly_page_and_graph_shape(wiki: Path):
    _write(wiki, "concepts/source.md", "# Source Page\n\nSee [[target]] and [[ghost]].")
    _write(wiki, "topics/target.md", "# Target Page\n")
    _write(wiki, "orphans.md", "# generated report should be excluded")

    payload = build_wiki_payload(wiki)

    assert payload["page_count"] == 2
    assert {p["path"] for p in payload["pages"]} == {
        "concepts/source.md",
        "topics/target.md",
    }
    source = next(p for p in payload["pages"] if p["path"] == "concepts/source.md")
    target = next(p for p in payload["pages"] if p["path"] == "topics/target.md")
    assert source == {
        "slug": "source",
        "title": "Source Page",
        "category": "concepts",
        "path": "concepts/source.md",
        "mtime": source["mtime"],
        "outbound": ["ghost", "target"],
        "inbound": [],
        "is_orphan": True,
        "has_slug_collision": False,
    }
    assert isinstance(source["mtime"], str)
    assert target["title"] == "Target Page"
    assert target["inbound"] == ["concepts/source.md"]
    assert payload["orphans"] == ["concepts/source.md"]
    assert payload["dangling_links"] == [
        {"target": "ghost", "source": "concepts/source.md", "line": 3}
    ]
    assert payload["graph"] == {
        "nodes": [
            {
                "id": "concepts/source.md",
                "slug": "source",
                "title": "Source Page",
                "category": "concepts",
                "is_orphan": True,
                "has_slug_collision": False,
            },
            {
                "id": "topics/target.md",
                "slug": "target",
                "title": "Target Page",
                "category": "topics",
                "is_orphan": False,
                "has_slug_collision": False,
            },
        ],
        "edges": [
            {
                "source": "concepts/source.md",
                "target": "topics/target.md",
                "target_slug": "target",
            }
        ],
    }


def test_build_wiki_payload_surfaces_slug_collisions(wiki: Path):
    _write(wiki, "concepts/foo.md", "# Foo Concept")
    _write(wiki, "topics/foo.md", "# Foo Topic")
    _write(wiki, "entities/linker.md", "# Linker\n\nSee [[foo]].")

    payload = build_wiki_payload(wiki)

    assert payload["slug_collisions"] == {
        "foo": ["concepts/foo.md", "topics/foo.md"],
    }
    collision_pages = {
        p["path"]: p for p in payload["pages"] if p["has_slug_collision"]
    }
    assert set(collision_pages) == {"concepts/foo.md", "topics/foo.md"}
    assert payload["graph"]["edges"] == [
        {
            "source": "entities/linker.md",
            "target": "concepts/foo.md",
            "target_slug": "foo",
        },
        {
            "source": "entities/linker.md",
            "target": "topics/foo.md",
            "target_slug": "foo",
        },
    ]


# ─── Renderers ───────────────────────────────────────────────────────


def test_render_orphans_clean_wiki(wiki: Path):
    _write(wiki, "concepts/a.md", "[[b]]")
    _write(wiki, "concepts/b.md", "[[a]]")
    graph = build_graph(wiki)
    out = render_orphans_md(graph, "2026-05-09T00:00:00+00:00")
    assert out.startswith("<!-- desc: generated wiki health report")
    assert "(none — every page has at least one inbound" in out


def test_render_orphans_groups_by_category(wiki: Path):
    _write(wiki, "concepts/c1.md", "")
    _write(wiki, "topics/t1.md", "")
    _write(wiki, "topics/t2.md", "")
    graph = build_graph(wiki)
    out = render_orphans_md(graph, "2026-05-09T00:00:00+00:00")
    assert "## concepts (1)" in out
    assert "## topics (2)" in out
    # Slug + path both rendered for actionable navigation.
    assert "[[c1]] — `concepts/c1.md`" in out
    assert "[[t1]] — `topics/t1.md`" in out


def test_render_dangling_groups_by_source(wiki: Path):
    _write(wiki, "concepts/foo.md", "[[ghost-1]]\n\n[[ghost-2]]")
    graph = build_graph(wiki)
    out = render_dangling_md(graph, "2026-05-09T00:00:00+00:00")
    assert out.startswith("<!-- desc: generated wiki health report")
    assert "## `concepts/foo.md`" in out
    assert "[[ghost-1]]" in out and "(line 1)" in out
    assert "[[ghost-2]]" in out and "(line 3)" in out


def test_render_backlinks_index_marks_orphans(wiki: Path):
    _write(wiki, "concepts/lonely.md", "")
    _write(wiki, "concepts/popular.md", "")
    _write(wiki, "concepts/source.md", "[[popular]]")
    graph = build_graph(wiki)
    out = render_backlinks_index_md(graph, "2026-05-09T00:00:00+00:00")
    assert out.startswith("<!-- desc: generated wiki backlinks index")
    # Path-keyed sections (was slug-keyed pre-refactor).
    assert "## concepts/lonely.md" in out
    assert "_(orphan — no inbound links)_" in out
    assert "## concepts/popular.md" in out
    assert "[[source]] — `concepts/source.md`" in out


# ─── End-to-end run ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_writes_three_files_and_emits_event(home: Path):
    wiki = home / "state" / "wiki"
    _write(wiki, "concepts/foo.md", "[[bar]] and [[ghost]]")
    _write(wiki, "concepts/bar.md", "# Bar")

    summary = await run(home)

    # Outputs land at the expected paths with desc headers for index/search hygiene.
    assert (wiki / "orphans.md").exists()
    assert (wiki / "dangling-links.md").exists()
    assert (wiki / "backlinks-index.md").exists()
    for output in ("orphans.md", "dangling-links.md", "backlinks-index.md"):
        first = (wiki / output).read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("<!-- desc: ")

    assert summary["page_count"] == 2
    assert summary["orphan_count"] == 1  # foo has no inbound
    assert summary["dangling_count"] == 1  # ghost is missing

    events = _read_events(home)
    unhealthy = [
        e for e in events if e.get("type") == "wiki_backlinks_unhealthy"
    ]
    assert len(unhealthy) == 1
    assert unhealthy[0]["orphan_count"] == 1
    assert unhealthy[0]["dangling_count"] == 1
    assert unhealthy[0]["page_count"] == 2


@pytest.mark.asyncio
async def test_run_clean_wiki_emits_no_event(home: Path):
    """A wiki with zero orphans + zero dangling links must NOT emit an
    event — clean state shouldn't crowd the algedonic firehose."""
    wiki = home / "state" / "wiki"
    _write(wiki, "concepts/a.md", "[[b]]")
    _write(wiki, "concepts/b.md", "[[a]]")

    summary = await run(home)
    assert summary["orphan_count"] == 0
    assert summary["dangling_count"] == 0

    events = _read_events(home)
    unhealthy = [
        e for e in events if e.get("type") == "wiki_backlinks_unhealthy"
    ]
    assert unhealthy == [], (
        "clean wiki must not emit wiki_backlinks_unhealthy"
    )


@pytest.mark.asyncio
async def test_run_orphans_md_overwritten_each_call(home: Path):
    """The orphans.md file is regenerated; stale content from a prior
    run must be replaced, not appended."""
    wiki = home / "state" / "wiki"
    (wiki / "orphans.md").write_text("# old stale content\n", encoding="utf-8")
    _write(wiki, "concepts/clean.md", "[[friend]]")
    _write(wiki, "concepts/friend.md", "[[clean]]")

    await run(home)
    out = (wiki / "orphans.md").read_text()
    assert "old stale content" not in out
    assert "Generated" in out


@pytest.mark.asyncio
async def test_run_no_wiki_raises(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test")
    with pytest.raises(FileNotFoundError):
        await run(tmp_path)


def test_find_slug_collisions_returns_paths_for_duplicate_stems(tmp_path):
    """CR2 (memory & retrieval) helper: cross-category same-stem files
    must be discoverable so the introspection skill can surface them."""
    from mimir.wiki_backlinks import find_slug_collisions

    (tmp_path / "concepts").mkdir()
    (tmp_path / "topics").mkdir()
    (tmp_path / "concepts" / "foo.md").write_text("a")
    (tmp_path / "topics" / "foo.md").write_text("b")
    (tmp_path / "topics" / "bar.md").write_text("c")  # no collision

    collisions = find_slug_collisions(tmp_path)
    assert "foo" in collisions
    assert len(collisions["foo"]) == 2
    assert "bar" not in collisions  # only collisions returned
