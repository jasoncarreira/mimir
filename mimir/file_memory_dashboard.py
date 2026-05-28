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


# ─── HTML shell ──────────────────────────────────────────────────


def render_memory_html() -> str:
    """Return the /memory HTML shell.

    Two-pane layout: left (30%) is a collapsible directory tree loaded
    from GET /api/memory?view=tree; right (70%) shows file content
    loaded from GET /api/memory?view=file&path=...

    Same dark-mode palette and auth pattern as /ops and /saga.
    """
    return _MEMORY_HTML


# IMPORTANT: this is a Python triple-double-quoted string.
# JS backslash escapes MUST be doubled so Python doesn't consume them
# before the browser sees them. See ops_dashboard.py's IMPORTANT note.
# Phase 2 additions: search box + state/ subtree in the left pane tree.
_MEMORY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="robots" content="noindex,nofollow" />
  <title>mimir Memory</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --paper: #0f1117;
      --paper-strong: #1a1d27;
      --paper-strong-2: #22263a;
      --ink: #e2e6f0;
      --muted: #8b92a8;
      --line: rgba(226, 230, 240, 0.12);
      --accent: #6c8ef7;
      --accent-soft: rgba(108, 142, 247, 0.16);
      --warn: #fbbf24;
      --bad: #f87171;
      --good: #4ade80;
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0; height: 100%;
      background:
        radial-gradient(circle at top left, rgba(108, 142, 247, 0.08), transparent 32rem),
        linear-gradient(180deg, #0f1117 0%, #141823 60%, #0f1117 100%);
      color: var(--ink);
      font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
    }
    body { display: flex; flex-direction: column; min-height: 100vh; padding: 0; margin: 0; }
    .shell { display: flex; flex-direction: column; flex: 1; max-width: 1400px; width: 100%; margin: 0 auto; padding: 1rem 1.4rem 2rem; }
    header {
      display: flex; align-items: baseline; justify-content: space-between;
      gap: 1rem; flex-wrap: wrap;
      padding-bottom: 0.6rem;
      border-bottom: 1px solid var(--line);
      margin-bottom: 1rem;
    }
    header h1 { margin: 0; font-size: 1.4rem; font-weight: 600; }
    header a { color: var(--accent); text-decoration: none; font-size: 0.9rem; margin-left: 1rem; }
    header a:hover { text-decoration: underline; }
    /* Two-pane layout */
    .panes {
      display: flex;
      gap: 0.8rem;
      flex: 1;
      min-height: 0;
      height: calc(100vh - 7rem);
    }
    .left-pane {
      width: 30%;
      min-width: 200px;
      max-width: 360px;
      background: var(--paper-strong);
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow-y: auto;
      padding: 0.6rem 0;
    }
    .right-pane {
      flex: 1;
      background: var(--paper-strong);
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
    }
    /* Tree styles */
    .tree-node { font-size: 0.83rem; user-select: none; }
    .tree-dir-label {
      display: flex; align-items: center; gap: 0.3rem;
      padding: 0.22rem 0.6rem;
      cursor: pointer;
      color: var(--muted);
      font-weight: 500;
    }
    .tree-dir-label:hover { color: var(--ink); background: var(--paper-strong-2); }
    .tree-dir-label .caret { font-size: 0.7rem; transition: transform 0.15s; display: inline-block; width: 0.8rem; }
    .tree-dir-label.open .caret { transform: rotate(90deg); }
    .tree-dir-children { padding-left: 1rem; display: none; }
    .tree-dir-children.open { display: block; }
    .tree-file-label {
      display: flex; align-items: baseline; gap: 0.3rem;
      padding: 0.2rem 0.6rem;
      cursor: pointer;
      border-radius: 4px;
    }
    .tree-file-label:hover { background: var(--accent-soft); }
    .tree-file-label.selected { background: var(--accent-soft); color: var(--accent); }
    .tree-file-name { font-size: 0.82rem; }
    .tree-file-desc { color: var(--muted); font-size: 0.73rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 140px; }
    /* Right pane content */
    .file-header {
      padding: 0.7rem 1rem 0.5rem;
      border-bottom: 1px solid var(--line);
      background: var(--paper-strong-2);
      border-radius: 10px 10px 0 0;
    }
    .file-header .file-path { font-size: 0.85rem; font-weight: 600; margin-bottom: 0.25rem; }
    .file-header .file-meta { color: var(--muted); font-size: 0.77rem; }
    .file-content {
      flex: 1;
      padding: 0.8rem 1rem;
      font-family: "Courier New", Courier, monospace;
      font-size: 0.8rem;
      line-height: 1.55;
      white-space: pre-wrap;
      word-break: break-word;
      background: var(--paper);
      border-radius: 0 0 10px 10px;
      overflow-y: auto;
    }
    .empty-pane {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--muted);
      font-size: 0.85rem;
    }
    .error-msg { color: var(--bad); font-size: 0.83rem; padding: 1rem; }
    .tree-loading { color: var(--muted); font-size: 0.82rem; padding: 1rem; }
    /* Search */
    .search-box { padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--line); }
    .search-input {
      width: 100%; background: var(--paper); border: 1px solid var(--line);
      border-radius: 6px; color: var(--ink); padding: 0.3rem 0.5rem;
      font-size: 0.82rem; outline: none; font-family: inherit;
    }
    .search-input:focus { border-color: var(--accent); }
    .search-results-header {
      padding: 0.35rem 0.7rem; color: var(--muted); font-size: 0.78rem;
      border-bottom: 1px solid var(--line);
    }
    .search-hit {
      padding: 0.35rem 0.7rem; cursor: pointer;
      border-bottom: 1px solid var(--line);
    }
    .search-hit:hover { background: var(--accent-soft); }
    .search-hit-path { color: var(--accent); font-size: 0.78rem; margin-bottom: 0.1rem; }
    .search-hit-snippet {
      color: var(--muted); font-size: 0.76rem;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
  </style>
</head>
<body>
<div class="shell">
  <header>
    <h1>mimir <span style="color:var(--accent)">memory</span></h1>
    <div>
      <a href="/ops">ops</a>
      <a href="/saga">saga</a>
      <a href="/turns">turns</a>
    </div>
  </header>

  <div class="panes">
    <!-- Left pane: search + directory tree -->
    <div class="left-pane" id="left-pane">
      <div class="search-box">
        <input class="search-input" id="search-input" type="search"
               placeholder="Search files..." autocomplete="off" />
      </div>
      <div class="tree-loading" id="tree-loading">Loading tree...</div>
      <div id="tree-root"></div>
      <div id="search-results" style="display:none"></div>
    </div>

    <!-- Right pane: file content -->
    <div class="right-pane" id="right-pane">
      <div class="empty-pane" id="file-placeholder">Select a file to view its contents.</div>
    </div>
  </div>
</div>

<script>
// ── Auth ─────────────────────────────────────────────────────────
function getApiKey() {
  let k = localStorage.getItem("mimir_api_key") || "";
  if (!k) {
    k = prompt("API key (leave blank if none):") || "";
    if (k) localStorage.setItem("mimir_api_key", k);
  }
  return k;
}

async function authedFetch(url) {
  const k = getApiKey();
  const headers = k ? {"X-API-Key": k} : {};
  const r = await fetch(url, {headers});
  if (r.status === 401) {
    localStorage.removeItem("mimir_api_key");
    throw new Error("Unauthorized — bad API key?");
  }
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

// ── Formatting ────────────────────────────────────────────────────
function fmtBytes(b) {
  if (b < 1024) return b + " B";
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
  return (b / 1024 / 1024).toFixed(1) + " MB";
}

function fmtTs(ts) {
  if (!ts) return "—";
  try {
    return new Date(ts).toISOString().replace("T", " ").slice(0, 19) + "Z";
  } catch { return ts; }
}

function esc(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ── Tree rendering ────────────────────────────────────────────────
let _selectedFileEl = null;

// Dirs that start open by default.
function _isDefaultOpen(nodePath) {
  if (!nodePath) return true; // virtual home root
  const parts = nodePath.split("/").filter(Boolean);
  if (parts.length === 0) return true;
  if (parts.length === 1) return true; // "memory" and "state" top-level dirs
  // memory/core is always open so core blocks are visible immediately.
  if (parts[0] === "memory" && parts[1] === "core") return true;
  // Other second-level dirs start collapsed (state/ has many subdirs).
  return false;
}

function renderNode(node, container) {
  if (node.type === "dir") {
    const wrapper = document.createElement("div");
    wrapper.className = "tree-node";

    const label = document.createElement("div");
    const isOpen = _isDefaultOpen(node.path);
    label.className = "tree-dir-label" + (isOpen ? " open" : "");

    const caret = document.createElement("span");
    caret.className = "caret";
    caret.textContent = "\\u25b6";

    const nameSpan = document.createElement("span");
    nameSpan.textContent = node.name;

    label.appendChild(caret);
    label.appendChild(nameSpan);

    const children = document.createElement("div");
    children.className = "tree-dir-children" + (isOpen ? " open" : "");

    label.addEventListener("click", () => {
      const open = label.classList.toggle("open");
      children.classList.toggle("open", open);
    });

    if (node.children && node.children.length) {
      for (const child of node.children) {
        renderNode(child, children);
      }
    }

    wrapper.appendChild(label);
    wrapper.appendChild(children);
    container.appendChild(wrapper);

  } else {
    // File leaf
    const label = document.createElement("div");
    label.className = "tree-file-label";
    label.dataset.path = node.path;

    const nameSpan = document.createElement("span");
    nameSpan.className = "tree-file-name";
    nameSpan.textContent = node.name;

    label.appendChild(nameSpan);

    if (node.desc) {
      const descSpan = document.createElement("span");
      descSpan.className = "tree-file-desc";
      descSpan.textContent = node.desc;
      descSpan.title = node.desc;
      label.appendChild(descSpan);
    }

    label.addEventListener("click", () => loadFile(node.path, label));
    container.appendChild(label);
  }
}

// ── Tree loading ──────────────────────────────────────────────────
async function loadTree() {
  const loading = document.getElementById("tree-loading");
  const treeRoot = document.getElementById("tree-root");

  try {
    const data = await authedFetch("/api/memory?view=tree");
    loading.style.display = "none";

    if (data.error) {
      treeRoot.innerHTML = '<div class="error-msg">Error: ' + esc(data.error) + "</div>";
      return;
    }

    treeRoot.innerHTML = "";
    // data is the virtual "home" root from list_trees() — render its children
    // directly so the left pane shows memory/ and state/ as top-level entries.
    const tops = (data.children && data.children.length) ? data.children : [data];
    for (const child of tops) {
      renderNode(child, treeRoot);
    }

    // Auto-select memory/INDEX.md if it exists.
    const indexEl = treeRoot.querySelector('[data-path="memory/INDEX.md"]');
    if (indexEl) {
      indexEl.click();
    }
  } catch (e) {
    loading.textContent = "";
    treeRoot.innerHTML = '<div class="error-msg">Tree load failed: ' + esc(String(e)) + "</div>";
  }
}

// ── Search ────────────────────────────────────────────────────────
let _searchTimeout = null;

function _showTree() {
  document.getElementById("tree-root").style.display = "";
  document.getElementById("search-results").style.display = "none";
}

function _showSearch() {
  document.getElementById("tree-root").style.display = "none";
  document.getElementById("search-results").style.display = "";
}

async function loadSearch(q) {
  const searchResults = document.getElementById("search-results");
  _showSearch();
  searchResults.innerHTML = '<div class="tree-loading">Searching...</div>';
  try {
    const data = await authedFetch("/api/memory?view=search&q=" + encodeURIComponent(q));
    let html = '<div class="search-results-header">'
      + esc(String(data.total)) + " result(s)"
      + (data.truncated ? " (truncated)" : "")
      + "</div>";
    if (!data.hits || !data.hits.length) {
      html += '<div class="tree-loading">No results.</div>';
    } else {
      for (const hit of data.hits) {
        html += '<div class="search-hit" data-path="' + esc(hit.path) + '">';
        html += '<div class="search-hit-path">' + esc(hit.path) + ":" + esc(String(hit.line_no)) + "</div>";
        html += '<div class="search-hit-snippet">' + esc(hit.snippet) + "</div>";
        html += "</div>";
      }
    }
    searchResults.innerHTML = html;
    for (const el of searchResults.querySelectorAll(".search-hit")) {
      el.addEventListener("click", () => loadFile(el.dataset.path, null));
    }
  } catch (e) {
    searchResults.innerHTML = '<div class="error-msg">Search failed: ' + esc(String(e)) + "</div>";
  }
}

// ── File loading ──────────────────────────────────────────────────
async function loadFile(filePath, labelEl) {
  // Highlight selected file.
  if (_selectedFileEl) _selectedFileEl.classList.remove("selected");
  if (labelEl) { labelEl.classList.add("selected"); _selectedFileEl = labelEl; }

  const pane = document.getElementById("right-pane");
  pane.innerHTML = '<div class="empty-pane">Loading…</div>';

  try {
    const data = await authedFetch("/api/memory?view=file&path=" + encodeURIComponent(filePath));
    if (data.error) {
      pane.innerHTML = '<div class="error-msg">Error: ' + esc(data.error) + "</div>";
      return;
    }

    const header = document.createElement("div");
    header.className = "file-header";
    header.innerHTML = [
      '<div class="file-path">' + esc(data.path) + "</div>",
      '<div class="file-meta">' + fmtBytes(data.size || 0) + " &nbsp;·&nbsp; modified " + fmtTs(data.modified) + "</div>",
    ].join("");

    const content = document.createElement("pre");
    content.className = "file-content";
    content.textContent = data.content || "";

    pane.innerHTML = "";
    pane.appendChild(header);
    pane.appendChild(content);
  } catch (e) {
    pane.innerHTML = '<div class="error-msg">Fetch failed: ' + esc(String(e)) + "</div>";
  }
}

// ── Init ──────────────────────────────────────────────────────────
loadTree();

document.getElementById("search-input").addEventListener("input", function(e) {
  clearTimeout(_searchTimeout);
  const q = e.target.value.trim();
  if (!q) { _showTree(); return; }
  _searchTimeout = setTimeout(() => loadSearch(q), 300);
});
</script>
</body>
</html>"""


__all__ = [
    "list_tree",
    "list_trees",
    "read_file_safe",
    "read_file_safe_multi",
    "render_memory_html",
    "search_files",
]
