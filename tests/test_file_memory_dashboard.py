"""Tests for mimir.file_memory_dashboard (chainlink #223 — Phase 1).

Tests cover:
  - list_tree: tree structure, .md-only filter, desc extraction, dir-first sort
  - read_file_safe: success, path traversal, non-.md rejection, not-found
  - render_memory_html: valid HTML shell with expected tokens
  - web_ui routes: /memory HTML + /api/memory view={tree,file}
  - Path-safety: traversal → 400, non-.md → 400, missing → 404
  - Auth-exempt: /memory in _AUTH_EXEMPT
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir import web_ui
from mimir.file_memory_dashboard import (
    list_tree,
    read_file_safe,
    render_memory_html,
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


# ─── render_memory_html ───────────────────────────────────────────


def test_render_memory_html_is_valid_shell() -> None:
    html = render_memory_html()
    assert "<!doctype html>" in html
    assert "/api/memory" in html
    assert "loadTree()" in html
    # Auth pattern.
    assert "mimir_api_key" in html
    assert "X-API-Key" in html


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
        resp = await client.get("/memory")
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
    assert "children" in body
    assert body["type"] == "dir"


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


def test_memory_page_is_auth_exempt() -> None:
    from mimir.server import _AUTH_EXEMPT

    assert ("GET", "/memory") in _AUTH_EXEMPT
