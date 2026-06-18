"""Tests for mimir.file_memory_dashboard (chainlink #223 — Phase 1 + 2 + 3).

Tests cover:
  - list_tree: tree structure, .md-only filter, desc extraction, dir-first sort
  - list_trees: virtual home root with multiple roots
  - read_file_safe: success, path traversal, non-.md rejection, not-found
  - read_file_safe_multi: dispatches to correct root; rejects unknown prefix
  - search_files: hits across multiple roots, empty query, truncation
  - list_channel_dirs: basic, missing root, no channels subdir, files excluded
  - render_memory_html: valid HTML shell with expected tokens
  - web_ui routes: /state HTML + /api/memory view={tree,file,search,channels}
  - Path-safety: traversal → 400, non-.md → 400, missing → 404
  - Auth-exempt: /state in _AUTH_EXEMPT
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir import web_ui
from mimir.file_memory_dashboard import (
    list_channel_dirs,
    list_tree,
    list_trees,
    read_file_safe,
    read_file_safe_multi,
    render_memory_html,
    search_files,
)


# ─── list_tree ────────────────────────────────────────────────────


def test_list_tree_missing_root(tmp_path: Path) -> None:
    result = list_tree(tmp_path / "nonexistent")
    assert "error" in result
    assert result["children"] == []
    assert "not found" in result["error"]


def test_list_tree_basic_structure(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "INDEX.md").write_text("# Index\n")
    core = memory / "core"
    core.mkdir()
    (core / "00-identity.md").write_text("# Identity\n")

    result = list_tree(memory)
    assert result["type"] == "dir"
    assert result["name"] == "memory"
    # path is relative to root.parent (tmp_path)
    assert result["path"] == "memory"
    assert "children" in result

    # Should have one dir (core) and one file (INDEX.md)
    types = [c["type"] for c in result["children"]]
    assert "dir" in types
    assert "file" in types

    # Find the file leaf
    file_nodes = [c for c in result["children"] if c["type"] == "file"]
    assert len(file_nodes) == 1
    assert file_nodes[0]["name"] == "INDEX.md"
    assert file_nodes[0]["path"] == "memory/INDEX.md"
    assert isinstance(file_nodes[0]["size"], int)
    assert "modified" in file_nodes[0]

    # Find core dir
    dir_nodes = [c for c in result["children"] if c["type"] == "dir"]
    assert len(dir_nodes) == 1
    assert dir_nodes[0]["name"] == "core"
    assert dir_nodes[0]["path"] == "memory/core"
    # core should contain 00-identity.md
    core_files = dir_nodes[0]["children"]
    assert len(core_files) == 1
    assert core_files[0]["name"] == "00-identity.md"
    assert core_files[0]["path"] == "memory/core/00-identity.md"


def test_list_tree_only_md_files(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "notes.md").write_text("# notes\n")
    (memory / "data.json").write_text('{"key": "value"}')
    (memory / "readme.txt").write_text("plain text")
    (memory / "script.py").write_text("print('hi')")

    result = list_tree(memory)
    names = [c["name"] for c in result["children"]]
    assert "notes.md" in names
    assert "data.json" not in names
    assert "readme.txt" not in names
    assert "script.py" not in names


def test_list_tree_desc_extracted(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "identity.md").write_text("<!-- desc: who mimir is -->\n# Identity\n")

    result = list_tree(memory)
    file_node = result["children"][0]
    assert file_node["name"] == "identity.md"
    assert file_node["desc"] == "who mimir is"


def test_list_tree_dirs_before_files(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    # Create files first to ensure sort is by type, not creation order.
    (memory / "aaa.md").write_text("# A\n")
    (memory / "zzz.md").write_text("# Z\n")
    subdir = memory / "bbb"
    subdir.mkdir()
    (subdir / "child.md").write_text("# child\n")

    result = list_tree(memory)
    children = result["children"]
    # First child should be the dir (bbb), even though 'aaa.md' sorts before it.
    assert children[0]["type"] == "dir"
    assert children[0]["name"] == "bbb"
    # Files follow.
    assert children[1]["type"] == "file"
    assert children[1]["name"] == "aaa.md"
    assert children[2]["type"] == "file"
    assert children[2]["name"] == "zzz.md"


# ─── read_file_safe ───────────────────────────────────────────────


def test_read_file_safe_success(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "notes.md").write_text("# Hello\nWorld\n")

    # rel is relative to root.parent (same format as list_tree paths)
    result = read_file_safe(memory, "memory/notes.md")
    assert "error" not in result
    assert result["path"] == "memory/notes.md"
    assert result["content"] == "# Hello\nWorld\n"
    assert result["size"] > 0
    assert "modified" in result
    # modified should be ISO8601
    assert "T" in result["modified"]


def test_read_file_safe_path_traversal_rejected(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()

    # Path traversal: goes outside root
    result = read_file_safe(memory, "../etc/passwd.md")
    assert "error" in result
    assert "traversal" in result["error"]


def test_read_file_safe_non_md_rejected(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "data.json").write_text('{}')

    result = read_file_safe(memory, "memory/data.json")
    assert "error" in result
    assert "only .md" in result["error"]


def test_read_file_safe_not_found(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()

    result = read_file_safe(memory, "memory/ghost.md")
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.skipif(
    not hasattr(Path, "symlink_to"),
    reason="symlinks not supported on this platform",
)
def test_read_file_safe_md_symlink_to_non_md_rejected(tmp_path: Path) -> None:
    """A .md symlink whose resolved target has a non-.md suffix must be rejected.

    This pins the post-resolve suffix check in read_file_safe — a future
    refactor that moves the suffix check back to the unresolved path string
    would silently break this protection.
    """
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "real.txt").write_text("plain text\n")
    (memory / "looks_like_md.md").symlink_to(memory / "real.txt")

    result = read_file_safe(memory, "memory/looks_like_md.md")
    assert "error" in result
    assert "only .md" in result["error"]


# ─── list_trees ───────────────────────────────────────────────────


def test_list_trees_virtual_root(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    memory.mkdir()
    state.mkdir()
    (memory / "INDEX.md").write_text("# Index\n")
    (state / "wiki.md").write_text("# Wiki\n")

    result = list_trees([memory, state])
    assert result["name"] == "home"
    assert result["type"] == "dir"
    assert result["path"] == ""
    assert len(result["children"]) == 2
    names = [c["name"] for c in result["children"]]
    assert "memory" in names
    assert "state" in names


def test_list_trees_skips_missing_roots(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "notes.md").write_text("# notes\n")
    missing = tmp_path / "nonexistent"

    result = list_trees([memory, missing])
    # Only the existing root appears.
    assert len(result["children"]) == 1
    assert result["children"][0]["name"] == "memory"


def test_list_trees_empty_roots(tmp_path: Path) -> None:
    result = list_trees([])
    assert result["name"] == "home"
    assert result["children"] == []


# ─── read_file_safe_multi ─────────────────────────────────────────


def test_read_file_safe_multi_dispatches_to_correct_root(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    memory.mkdir()
    state.mkdir()
    (memory / "notes.md").write_text("# Memory notes\n")
    (state / "spec.md").write_text("# State spec\n")

    result = read_file_safe_multi([memory, state], "memory/notes.md")
    assert "error" not in result
    assert "Memory notes" in result["content"]

    result2 = read_file_safe_multi([memory, state], "state/spec.md")
    assert "error" not in result2
    assert "State spec" in result2["content"]


def test_read_file_safe_multi_unknown_prefix_rejected(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "notes.md").write_text("# notes\n")

    result = read_file_safe_multi([memory], "etc/passwd.md")
    assert "error" in result
    assert "not in any" in result["error"]


def test_read_file_safe_multi_traversal_still_blocked(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()

    # Even with a valid prefix, path traversal inside the root must be blocked.
    result = read_file_safe_multi([memory], "memory/../etc/passwd.md")
    assert "error" in result


def test_read_file_safe_multi_empty_rel_rejected(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()

    result = read_file_safe_multi([memory], "")
    assert "error" in result


# ─── search_files ─────────────────────────────────────────────────


def test_search_files_basic_hit(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "notes.md").write_text("hello world\nfoo bar\n")

    result = search_files([memory], "hello")
    assert result["query"] == "hello"
    assert result["total"] == 1
    assert not result["truncated"]
    assert result["hits"][0]["path"] == "memory/notes.md"
    assert result["hits"][0]["line_no"] == 1
    assert "hello world" in result["hits"][0]["snippet"]


def test_search_files_case_insensitive(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "doc.md").write_text("UPPER case LINE\n")

    result = search_files([memory], "upper case")
    assert result["total"] == 1


def test_search_files_across_multiple_roots(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    memory.mkdir()
    state.mkdir()
    (memory / "a.md").write_text("needle in memory\n")
    (state / "b.md").write_text("needle in state\n")

    result = search_files([memory, state], "needle")
    assert result["total"] == 2
    paths = {h["path"] for h in result["hits"]}
    assert "memory/a.md" in paths
    assert "state/b.md" in paths


def test_search_files_empty_query(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "doc.md").write_text("content\n")

    result = search_files([memory], "")
    assert result["total"] == 0
    assert result["hits"] == []
    assert not result["truncated"]


def test_search_files_truncation(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    # Create a file with 200 matching lines.
    content = "\n".join(f"find me line {i}" for i in range(200)) + "\n"
    (memory / "big.md").write_text(content)

    result = search_files([memory], "find me", max_hits=10)
    assert result["truncated"] is True
    assert result["total"] == 10
    assert len(result["hits"]) == 10


def test_search_files_no_results(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "doc.md").write_text("nothing here\n")

    result = search_files([memory], "xyzzy_not_present")
    assert result["total"] == 0
    assert result["hits"] == []
    assert not result["truncated"]


def test_search_files_snippet_capped_at_200(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    long_line = "find " + "x" * 300
    (memory / "long.md").write_text(long_line + "\n")

    result = search_files([memory], "find")
    assert result["total"] == 1
    assert len(result["hits"][0]["snippet"]) <= 200


# ─── render_memory_html ───────────────────────────────────────────


def test_render_memory_html_is_valid_shell() -> None:
    html = render_memory_html()
    assert "<!doctype html>" in html
    assert "/api/memory" in html
    assert "loadTree()" in html
    # Auth pattern — shared helper, no inline key handling.
    assert "/app/auth.js" in html
    assert "window.MimirAuth.authedJson" in html
    assert "API_KEY_LS" not in html


def test_render_memory_html_has_search_ui() -> None:
    """Phase 2: the HTML shell must include the search box and loadSearch."""
    html = render_memory_html()
    assert "search-input" in html
    assert "loadSearch" in html
    assert "view=search" in html


# ─── /memory web routes ────────────────────────────────────────────


@pytest.fixture
def memory_app(tmp_path: Path):
    home = tmp_path / "mimir_home"
    memory_root = home / "memory"
    memory_root.mkdir(parents=True)
    (memory_root / "INDEX.md").write_text("<!-- desc: test index -->\n# Index\n")
    core = memory_root / "core"
    core.mkdir()
    (core / "00-identity.md").write_text("<!-- desc: identity -->\n# Identity\n")
    # Phase 2: also create state/ subtree so it's included in tree + search.
    state_root = home / "state"
    wiki = state_root / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "overview.md").write_text("# Wiki overview\nsearchable content\n")

    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl",
        home=home,
    )
    return a, home


@pytest.mark.asyncio
async def test_memory_page_serves_html(memory_app) -> None:
    app, _ = memory_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/state")  # renamed from /memory
        assert resp.status == 200
        assert resp.content_type == "text/html"
        body = await resp.text()
    assert "mimir" in body
    assert "/api/memory" in body


@pytest.mark.asyncio
async def test_api_memory_tree(memory_app) -> None:
    app, _ = memory_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/memory?view=tree")
        assert resp.status == 200
        body = await resp.json()
    # Phase 2: tree returns virtual "home" root with memory/ + state/ children.
    assert body["type"] == "dir"
    assert "children" in body
    child_names = [c["name"] for c in body["children"]]
    assert "memory" in child_names
    assert "state" in child_names


@pytest.mark.asyncio
async def test_api_memory_file(memory_app) -> None:
    app, home = memory_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/memory?view=file&path=memory/INDEX.md")
        assert resp.status == 200
        body = await resp.json()
    assert "error" not in body
    assert body["path"] == "memory/INDEX.md"
    assert "Index" in body["content"]


@pytest.mark.asyncio
async def test_api_memory_path_traversal_rejected(memory_app) -> None:
    app, _ = memory_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/memory?view=file&path=../../etc/passwd.md")
        assert resp.status == 400
        body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_api_memory_unknown_root_rejected(memory_app) -> None:
    """A path whose first component isn't 'memory' or 'state' → 400."""
    app, _ = memory_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/memory?view=file&path=etc/passwd.md")
        assert resp.status == 400
        body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_api_memory_search_basic(memory_app) -> None:
    app, _ = memory_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/memory?view=search&q=searchable")
        assert resp.status == 200
        body = await resp.json()
    assert body["query"] == "searchable"
    assert body["total"] >= 1
    paths = [h["path"] for h in body["hits"]]
    assert any("state" in p for p in paths)


@pytest.mark.asyncio
async def test_api_memory_search_empty_q(memory_app) -> None:
    app, _ = memory_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/memory?view=search")
        assert resp.status == 400
        body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_api_memory_file_in_state_subtree(memory_app) -> None:
    """Phase 2: /api/memory?view=file can serve state/ files."""
    app, _ = memory_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/memory?view=file&path=state/wiki/overview.md")
        assert resp.status == 200
        body = await resp.json()
    assert "error" not in body
    assert "Wiki overview" in body["content"]


def test_memory_page_is_auth_exempt() -> None:
    from mimir.server import _AUTH_EXEMPT

    assert ("GET", "/state") in _AUTH_EXEMPT  # renamed from /memory
    assert ("GET", "/memory") not in _AUTH_EXEMPT


# ─── list_channel_dirs (Phase 3) ──────────────────────────────────


def test_list_channel_dirs_basic(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    (mem / "channels" / "chan1").mkdir(parents=True)
    (mem / "channels" / "chan2").mkdir(parents=True)
    result = list_channel_dirs(mem)
    assert result == ["chan1", "chan2"]


def test_list_channel_dirs_sorted(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    for name in ["zzz", "aaa", "mmm"]:
        (mem / "channels" / name).mkdir(parents=True)
    result = list_channel_dirs(mem)
    assert result == ["aaa", "mmm", "zzz"]


def test_list_channel_dirs_missing_root(tmp_path: Path) -> None:
    result = list_channel_dirs(tmp_path / "memory")
    assert result == []


def test_list_channel_dirs_no_channels_subdir(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    result = list_channel_dirs(mem)
    assert result == []


def test_list_channel_dirs_excludes_files(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    (mem / "channels" / "chan1").mkdir(parents=True)
    (mem / "channels" / "notadir.md").write_text("# not a dir\n")
    result = list_channel_dirs(mem)
    assert result == ["chan1"]


# ─── Phase 3 HTML assertions ──────────────────────────────────────


def test_render_memory_html_has_channel_filter() -> None:
    html = render_memory_html()
    assert 'id="channel-filter"' in html
    assert 'channel-filter-box' in html
    assert 'channel-filter-select' in html


def test_render_memory_html_has_index_landing_js() -> None:
    html = render_memory_html()
    assert "findIndexMd" in html
    assert "INDEX.md" in html
    assert "_pathToEl" in html
    assert "expandAndScrollToDir" in html


def test_render_memory_html_has_populate_channel_filter() -> None:
    html = render_memory_html()
    assert "populateChannelFilter" in html


# ─── /api/memory?view=channels route (Phase 3) ────────────────────


@pytest.mark.asyncio
async def test_api_memory_channels_empty(memory_app) -> None:
    """When no channels/ dir exists, view=channels returns empty list."""
    app, _ = memory_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/memory?view=channels")
        assert resp.status == 200
        body = await resp.json()
    assert body == {"channels": []}


@pytest.mark.asyncio
async def test_api_memory_channels_with_dirs(memory_app) -> None:
    """Channels present in memory/channels/ are returned sorted."""
    app, home = memory_app
    (home / "memory" / "channels" / "discord-111").mkdir(parents=True)
    (home / "memory" / "channels" / "slack-222").mkdir(parents=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/memory?view=channels")
        assert resp.status == 200
        body = await resp.json()
    assert body == {"channels": ["discord-111", "slack-222"]}
