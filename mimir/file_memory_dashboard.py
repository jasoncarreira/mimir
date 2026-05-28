"""File-based memory viewer — reads ``memory/`` and ``state/`` on demand
and renders an operator-facing two-pane view at ``/memory``.

Mirrors the shape of ``saga_dashboard.py``: pure-data functions return
dicts; ``render_memory_html()`` returns the HTML shell.
No HTML in the data functions — same separation as ops_dashboard.

Chainlink #223 — Phase 1:
  /memory                         — HTML shell (two-pane file browser)
  /api/memory?view=tree           — nested dir/file tree as JSON
  /api/memory?view=file&path=...  — safe file reader (only .md)

Chainlink #223 — Phase 2:
  /api/memory?view=search&q=...   — full-text search across memory/ + state/
  /api/memory?view=tree           — now returns a virtual "home" root whose
                                    children are memory/ and state/ sub-trees

Chainlink #223 — Phase 3:
  INDEX.md landing — clicking a collapsed dir with an INDEX.md child opens
                     it AND loads the INDEX.md in the right pane.
  Channel-filter   — ``<select>`` dropdown to jump directly to any
                     ``memory/channels/<channel_id>/`` sub-tree.
  /api/memory?view=channels — JSON list of channel dir names under
                              ``memory/channels/``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .core_blocks import extract_desc_comment

log = logging.getLogger(__name__)


# ─── payload builders ────────────────────────────────────────────


def list_tree(root: Path) -> dict:
    """Recursively walk ``root`` and return a nested dict tree.

    Only ``.md`` files are included; all other extensions are skipped.
    Children are sorted: dirs first (alphabetical), then files (alphabetical).
    Paths in leaf nodes are relative to ``root.parent``.

    Returns an error dict if ``root`` doesn't exist.
    """
    if not root.exists():
        return {"error": "memory dir not found", "children": []}

    def _walk(path: Path) -> dict:
        rel_to_parent = path.relative_to(root.parent)
        if path.is_dir():
            children: list[dict] = []
            dirs: list[dict] = []
            files: list[dict] = []
            for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                if child.is_dir():
                    dirs.append(_walk(child))
                elif child.is_file() and child.suffix == ".md":
                    files.append(_walk(child))
                # skip all other extensions
            children = dirs + files
            return {
                "name": path.name,
                "type": "dir",
                "path": str(rel_to_parent),
                "desc": None,
                "children": children,
            }
        else:
            # It's a file — read first line to extract desc comment.
            try:
                first_line = path.read_text(encoding="utf-8", errors="replace").split("\n")[0]
                desc = extract_desc_comment(first_line)
            except OSError:
                desc = None
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            return {
                "name": path.name,
                "type": "file",
                "path": str(rel_to_parent),
                "size": stat.st_size,
                "modified": modified,
                "desc": desc,
            }

    return _walk(root)


def list_trees(roots: list[Path]) -> dict:
    """Return a virtual combined root wrapping trees for each path in *roots*.

    The returned dict has::

        {"name": "home", "type": "dir", "path": "", "desc": None,
         "children": [list_tree(r) for r in roots if r.exists()]}

    Paths in leaf nodes remain relative to each ``root.parent``, so they
    work unchanged with ``read_file_safe_multi``.
    """
    children = [list_tree(r) for r in roots if r.exists()]
    return {
        "name": "home",
        "type": "dir",
        "path": "",
        "desc": None,
        "children": children,
    }


def read_file_safe_multi(roots: list[Path], rel: str) -> dict:
    """Dispatch ``read_file_safe`` to the matching root in *roots*.

    ``rel`` uses the same path format as ``list_tree`` / ``list_trees``
    (e.g. ``memory/core/00-identity.md`` or ``state/wiki/concepts/foo.md``).
    The first path component of ``rel`` must exactly match one of the
    ``root.name`` values in *roots*; otherwise a rejection dict is returned
    rather than forwarding the path.

    This is the multi-root analogue of ``read_file_safe``; it provides the
    same path-traversal and `.md`-only guarantees via delegation.
    """
    from pathlib import PurePosixPath

    parts = PurePosixPath(rel).parts
    if not parts:
        return {"error": "path not in any allowed root"}
    first = parts[0]
    for root in roots:
        if root.name == first:
            return read_file_safe(root, rel)
    return {"error": "path not in any allowed root"}


def search_files(roots: list[Path], query: str, max_hits: int = 100) -> dict:
    """Case-insensitive full-text search across ``.md`` files under all *roots*.

    Returns a dict::

        {
            "query":     str,
            "hits":      [{"path": str, "line_no": int, "snippet": str}, ...],
            "total":     int,   # number of hits returned (≤ max_hits)
            "truncated": bool,  # True when additional matches exist
        }

    ``path`` in each hit is relative to ``root.parent`` (same format as
    ``list_tree`` leaf nodes).  ``snippet`` is capped at 200 characters.
    """
    query_lower = query.lower().strip()
    if not query_lower:
        return {"query": query, "hits": [], "total": 0, "truncated": False}

    hits: list[dict] = []

    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = str(path.relative_to(root.parent))
            for line_no, line in enumerate(lines, start=1):
                if query_lower in line.lower():
                    hits.append(
                        {
                            "path": rel,
                            "line_no": line_no,
                            "snippet": line[:200],
                        }
                    )
                    if len(hits) >= max_hits:
                        return {
                            "query": query,
                            "hits": hits,
                            "total": max_hits,
                            "truncated": True,
                        }

    return {"query": query, "hits": hits, "total": len(hits), "truncated": False}


def read_file_safe(root: Path, rel: str) -> dict:
    """Safely read a ``.md`` file.

    ``rel`` is a path relative to ``root.parent`` — i.e. the same
    format that ``list_tree`` returns in the ``path`` field of leaf
    nodes (e.g. ``memory/core/00-identity.md`` where ``memory`` is
    ``root.name``).

    Guards:
    - Path traversal: resolved path must be inside ``root.resolve()``.
    - Only ``.md`` files are served.
    - Symlinks that resolve outside root are rejected.

    Returns a dict with ``path``, ``content``, ``size``, ``modified``
    on success, or ``{"error": ...}`` on failure.
    """
    root_resolved = root.resolve()

    # Reject non-.md paths before any filesystem access.
    if not rel.endswith(".md"):
        return {"error": "only .md files are served"}

    # rel is relative to root.parent (e.g. "memory/core/foo.md")
    candidate = (root.parent / rel).resolve()

    # Path traversal check: resolved path must be inside root.
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return {"error": "path traversal rejected"}

    # Re-check suffix on resolved target (guards against .md symlinks to .txt files).
    if candidate.suffix != ".md":
        return {"error": "only .md files are served"}

    if not candidate.exists():
        return {"error": "file not found"}

    try:
        content = candidate.read_text(encoding="utf-8", errors="replace")
        stat = candidate.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        return {
            "path": rel,
            "content": content,
            "size": stat.st_size,
            "modified": modified,
        }
    except OSError as exc:
        log.warning("file_memory_dashboard: read error for %s: %s", rel, exc)
        return {"error": f"read error: {exc}"}


def list_channel_dirs(memory_root: Path) -> list[str]:
    """Return sorted channel-id directory names under ``memory_root/channels/``.

    Returns an empty list if ``memory_root`` or its ``channels/`` sub-dir
    does not exist.  Only directory entries are returned; plain files inside
    ``channels/`` are skipped.
    """
    channels_dir = memory_root / "channels"
    if not channels_dir.exists() or not channels_dir.is_dir():
        return []
    return sorted(p.name for p in channels_dir.iterdir() if p.is_dir())


# ─── HTML shell ──────────────────────────────────────────────────


def render_memory_html() -> str:
    """Return the /memory HTML shell.

    Two-pane layout: left (30%) is a collapsible directory tree loaded
    from GET /api/memory?view=tree; right (70%) shows file content
    loaded from GET /api/memory?view=file&path=...

    Same dark-mode palette and auth pattern as /ops and /saga.
    """
    return _load_memory_html()


# chainlink #243: dashboard HTML lives in a sibling .html file.
# Lazy-loaded + cached so the first /memory request pays the read but
# the rest is in-memory.
_MEMORY_HTML: str | None = None


def _load_memory_html() -> str:
    global _MEMORY_HTML
    if _MEMORY_HTML is None:
        _MEMORY_HTML = (
            Path(__file__).parent / "file_memory_dashboard.html"
        ).read_text(encoding="utf-8")
    return _MEMORY_HTML



__all__ = [
    "list_channel_dirs",
    "list_tree",
    "list_trees",
    "read_file_safe",
    "read_file_safe_multi",
    "render_memory_html",
    "search_files",
]
