"""Saga DB ops page — reads ``state/saga.db`` on demand and renders
an operator-facing view of the agent's memory atoms at ``/saga``.

Mirrors the shape of ``ops_dashboard.py``: pure-data ``build_*_payload``
functions return dicts; ``render_saga_html()`` returns the HTML shell.
No HTML in the payload functions — same separation as ops_dashboard.

Chainlink #222 — Phase 1:
  /saga              — HTML shell (same dark-mode palette as /ops)
  /api/saga?view=recent&channel=...&limit=N  — recent atoms
  /api/saga?view=atom&id=...                 — single atom inspector

Phase 2 (future): search, activation histogram, cluster browser.
Phase 3 (future): read-only SQL passthrough.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Row-count safety cap so a large DB doesn't generate a huge payload.
_MAX_RECENT = 200
_DEFAULT_RECENT = 50


# ─── helpers ──────────────────────────────────────────────────────


def _open_conn(db_path: Path) -> sqlite3.Connection | None:
    """Open a read-only connection to the saga DB, or return None on error."""
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def _parse_json_field(val: str | None, default: Any = None) -> Any:
    if val is None:
        return default
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return default


# ─── payload builders ────────────────────────────────────────────


def build_recent_atoms_payload(
    db_path: Path,
    *,
    channel: str | None = None,
    limit: int = _DEFAULT_RECENT,
) -> dict[str, Any]:
    """Return the most recent non-tombstoned atoms as a JSON-serialisable dict.

    Query params forwarded from the route:
      channel   — filter to atoms whose session belongs to this channel_id
      limit     — cap (1..200, default 50)
    """
    limit = max(1, min(limit, _MAX_RECENT))

    conn = _open_conn(db_path)
    if conn is None:
        return {"error": "saga db not found or unreadable", "atoms": [], "total": 0}

    try:
        # Base query — join sessions to get channel_id for filtering / display.
        # LEFT JOIN because many atoms (raw + observation) may have no session.
        base_sql = """
            SELECT
                a.id,
                a.content,
                a.memory_type,
                a.stream,
                a.source_type,
                a.topics,
                a.arousal,
                a.valence,
                a.encoding_confidence,
                a.is_pinned,
                a.created_at,
                a.session_id,
                s.channel_id
            FROM atoms a
            LEFT JOIN sessions s ON s.id = a.session_id
            WHERE a.tombstoned = 0
        """
        params: list[Any] = []
        if channel:
            base_sql += " AND s.channel_id = ?"
            params.append(channel)
        base_sql += " ORDER BY a.created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(base_sql, params).fetchall()
        atoms = []
        for row in rows:
            d = _row_to_dict(row)
            d["topics"] = _parse_json_field(d.get("topics"), [])
            # Truncate content for the list view — full content is in /atom.
            content = d.get("content") or ""
            d["content_preview"] = content[:200] + ("…" if len(content) > 200 else "")
            d.pop("content", None)
            atoms.append(d)

        # Total count for metadata line.
        count_sql = "SELECT COUNT(*) FROM atoms WHERE tombstoned = 0"
        count_params: list[Any] = []
        if channel:
            count_sql = """
                SELECT COUNT(*) FROM atoms a
                LEFT JOIN sessions s ON s.id = a.session_id
                WHERE a.tombstoned = 0 AND s.channel_id = ?
            """
            count_params.append(channel)
        total = conn.execute(count_sql, count_params).fetchone()[0]

        # Distinct channels for the filter dropdown.
        channels_rows = conn.execute(
            "SELECT DISTINCT channel_id FROM sessions WHERE channel_id IS NOT NULL ORDER BY channel_id"
        ).fetchall()
        channels = [r[0] for r in channels_rows]

        return {
            "atoms": atoms,
            "total": total,
            "limit": limit,
            "channel_filter": channel,
            "channels": channels,
        }
    except sqlite3.Error as exc:
        log.warning("saga_dashboard: recent query failed: %s", exc)
        return {"error": str(exc), "atoms": [], "total": 0}
    finally:
        conn.close()


def build_atom_payload(db_path: Path, atom_id: str) -> dict[str, Any]:
    """Return full metadata for a single atom by ID.

    Includes access stats (access_events count), topics, relations out,
    and whether an embedding exists.
    """
    conn = _open_conn(db_path)
    if conn is None:
        return {"error": "saga db not found or unreadable"}

    try:
        row = conn.execute(
            """
            SELECT
                a.*,
                s.channel_id,
                s.started_at AS session_started_at
            FROM atoms a
            LEFT JOIN sessions s ON s.id = a.session_id
            WHERE a.id = ?
            """,
            (atom_id,),
        ).fetchone()

        if row is None:
            return {"error": f"atom {atom_id!r} not found"}

        d = _row_to_dict(row)
        d["topics"] = _parse_json_field(d.get("topics"), [])
        d["metadata"] = _parse_json_field(d.get("metadata"), {})

        # Access event count (activation history length).
        access_count = conn.execute(
            "SELECT COUNT(*) FROM access_events WHERE atom_id = ?", (atom_id,)
        ).fetchone()[0]
        d["access_count"] = access_count

        # Most recent access.
        last_access = conn.execute(
            "SELECT ts, source FROM access_events WHERE atom_id = ? ORDER BY ts DESC LIMIT 1",
            (atom_id,),
        ).fetchone()
        d["last_access_ts"] = last_access["ts"] if last_access else None
        d["last_access_source"] = last_access["source"] if last_access else None

        # Embedding existence (don't send the blob).
        emb_row = conn.execute(
            "SELECT provider, model, dim, embedded_at FROM embeddings WHERE atom_id = ?",
            (atom_id,),
        ).fetchone()
        d["embedding"] = _row_to_dict(emb_row) if emb_row else None

        # Outbound relations.
        rel_rows = conn.execute(
            """
            SELECT r.relation_type, r.target_id, r.confidence,
                   substr(ta.content, 1, 100) AS target_preview
            FROM atom_relations r
            LEFT JOIN atoms ta ON ta.id = r.target_id
            WHERE r.source_id = ?
            ORDER BY r.relation_type
            """,
            (atom_id,),
        ).fetchall()
        d["relations_out"] = [_row_to_dict(r) for r in rel_rows]

        return d
    except sqlite3.Error as exc:
        log.warning("saga_dashboard: atom query failed: %s", exc)
        return {"error": str(exc)}
    finally:
        conn.close()


def build_db_stats_payload(db_path: Path) -> dict[str, Any]:
    """Lightweight stats for the page header card (total atoms, sessions, etc.)."""
    conn = _open_conn(db_path)
    if conn is None:
        return {"error": "saga db not found", "ready": False}
    try:
        atom_count = conn.execute(
            "SELECT COUNT(*) FROM atoms WHERE tombstoned = 0"
        ).fetchone()[0]
        tombstoned_count = conn.execute(
            "SELECT COUNT(*) FROM atoms WHERE tombstoned = 1"
        ).fetchone()[0]
        session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        triple_count = conn.execute(
            "SELECT COUNT(*) FROM triples WHERE tombstoned = 0"
        ).fetchone()[0]
        schema_ver = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        db_size_bytes = db_path.stat().st_size
        return {
            "ready": True,
            "atom_count": atom_count,
            "tombstoned_count": tombstoned_count,
            "session_count": session_count,
            "triple_count": triple_count,
            "schema_version": schema_ver,
            "db_size_bytes": db_size_bytes,
            "db_path": str(db_path),
        }
    except sqlite3.Error as exc:
        return {"error": str(exc), "ready": False}
    finally:
        conn.close()


# ─── HTML shell ──────────────────────────────────────────────────


def render_saga_html() -> str:
    """Return the /saga HTML shell.

    Same dark-mode palette and auth pattern as /ops: the page is exempt
    from the API-key middleware (so the JS can prompt on first visit),
    but all ``/api/saga`` calls require the key via X-API-Key.
    """
    return _SAGA_HTML


# IMPORTANT: this is a Python triple-double-quoted string.
# JS backslash escapes MUST be doubled so Python doesn't consume them
# before the browser sees them. See ops_dashboard.py's IMPORTANT note.
_SAGA_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="robots" content="noindex,nofollow" />
  <title>mimir Saga</title>
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
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(108, 142, 247, 0.08), transparent 32rem),
        linear-gradient(180deg, #0f1117 0%, #141823 60%, #0f1117 100%);
      color: var(--ink);
      font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
    }
    body { padding: 1rem 1.4rem 3rem; }
    .shell { max-width: 1200px; margin: 0 auto; }
    header {
      display: flex; align-items: baseline; justify-content: space-between;
      gap: 1rem; flex-wrap: wrap;
      padding-bottom: 0.6rem;
      border-bottom: 1px solid var(--line);
      margin-bottom: 1.2rem;
    }
    header h1 { margin: 0; font-size: 1.4rem; font-weight: 600; }
    header a { color: var(--accent); text-decoration: none; font-size: 0.9rem; margin-left: 1rem; }
    header a:hover { text-decoration: underline; }
    .meta { color: var(--muted); font-size: 0.82rem; }
    /* Summary cards */
    .stats-row {
      display: flex; gap: 0.6rem; flex-wrap: wrap; margin-bottom: 1.2rem;
    }
    .stat-card {
      background: var(--paper-strong);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0.6rem 1rem;
      min-width: 120px;
    }
    .stat-card .label { color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; }
    .stat-card .value { font-size: 1.3rem; font-weight: 600; margin-top: 0.15rem; }
    /* Controls */
    .controls {
      display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap;
      margin-bottom: 0.9rem;
    }
    .controls label { color: var(--muted); font-size: 0.82rem; }
    .controls select, .controls input, .controls button {
      background: var(--paper-strong);
      border: 1px solid var(--line);
      color: var(--ink);
      border-radius: 6px;
      padding: 0.35rem 0.6rem;
      font-size: 0.85rem;
      font-family: inherit;
    }
    .controls input[type=text] { width: 220px; }
    .controls button {
      cursor: pointer;
      background: var(--accent-soft);
      border-color: var(--accent);
      color: var(--accent);
    }
    .controls button:hover { background: var(--accent); color: white; }
    /* Atom list */
    .atom-list {
      background: var(--paper-strong);
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow: hidden;
      margin-bottom: 1.2rem;
    }
    .atom-list-header {
      display: grid;
      grid-template-columns: 3fr 1fr 1fr 1fr 1.5fr;
      gap: 0.5rem;
      padding: 0.55rem 1rem;
      background: var(--paper-strong-2);
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .atom-row {
      display: grid;
      grid-template-columns: 3fr 1fr 1fr 1fr 1.5fr;
      gap: 0.5rem;
      padding: 0.65rem 1rem;
      border-bottom: 1px solid var(--line);
      cursor: pointer;
      transition: background 0.12s;
    }
    .atom-row:last-child { border-bottom: none; }
    .atom-row:hover { background: var(--accent-soft); }
    .atom-row.selected { background: var(--accent-soft); border-left: 3px solid var(--accent); padding-left: calc(1rem - 3px); }
    .atom-preview { font-size: 0.83rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .badge {
      display: inline-block;
      padding: 0.1rem 0.4rem;
      border-radius: 4px;
      font-size: 0.72rem;
      font-weight: 500;
    }
    .badge-raw { background: rgba(139, 146, 168, 0.2); color: var(--muted); }
    .badge-obs { background: rgba(108, 142, 247, 0.2); color: var(--accent); }
    .badge-model { background: rgba(74, 222, 128, 0.2); color: var(--good); }
    .ts { color: var(--muted); font-size: 0.78rem; font-variant-numeric: tabular-nums; }
    .pinned-mark { color: var(--warn); font-size: 0.8rem; }
    /* Atom detail panel */
    .detail-panel {
      background: var(--paper-strong);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 1rem 1.2rem;
      display: none;
    }
    .detail-panel.visible { display: block; }
    .detail-panel h2 { margin: 0 0 0.8rem; font-size: 1rem; font-weight: 600; }
    .detail-kv { display: grid; grid-template-columns: 140px 1fr; gap: 0.3rem 0.8rem; margin-bottom: 0.8rem; }
    .detail-kv .k { color: var(--muted); font-size: 0.8rem; }
    .detail-kv .v { font-size: 0.83rem; word-break: break-all; }
    .detail-content {
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0.7rem 0.9rem;
      font-size: 0.82rem;
      line-height: 1.6;
      white-space: pre-wrap;
      word-break: break-word;
      margin-bottom: 0.8rem;
    }
    .detail-section { margin-top: 0.8rem; }
    .detail-section h3 { margin: 0 0 0.4rem; font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
    .relation-row { font-size: 0.8rem; padding: 0.25rem 0; border-bottom: 1px solid var(--line); }
    .relation-row:last-child { border-bottom: none; }
    .empty { color: var(--muted); font-size: 0.83rem; padding: 1.5rem; text-align: center; }
    .error-msg { color: var(--bad); font-size: 0.83rem; padding: 1rem; }
    #atom-id-input { font-family: "Courier New", monospace; font-size: 0.8rem; }
  </style>
</head>
<body>
<div class="shell">
  <header>
    <h1>mimir <span style="color:var(--accent)">saga</span></h1>
    <div>
      <a href="/ops">ops</a>
      <a href="/turns">turns</a>
    </div>
  </header>

  <!-- Stats row -->
  <div class="stats-row" id="stats-row">
    <div class="stat-card"><div class="label">atoms</div><div class="value" id="stat-atoms">—</div></div>
    <div class="stat-card"><div class="label">sessions</div><div class="value" id="stat-sessions">—</div></div>
    <div class="stat-card"><div class="label">triples</div><div class="value" id="stat-triples">—</div></div>
    <div class="stat-card"><div class="label">tombstoned</div><div class="value" id="stat-tombstoned">—</div></div>
    <div class="stat-card"><div class="label">db size</div><div class="value" id="stat-dbsize">—</div></div>
    <div class="stat-card"><div class="label">schema</div><div class="value" id="stat-schema">—</div></div>
  </div>

  <!-- Controls -->
  <div class="controls">
    <label>Channel</label>
    <select id="channel-filter">
      <option value="">all channels</option>
    </select>
    <label>Limit</label>
    <select id="limit-select">
      <option value="25">25</option>
      <option value="50" selected>50</option>
      <option value="100">100</option>
      <option value="200">200</option>
    </select>
    <button id="refresh-btn" onclick="loadRecent()">Refresh</button>
    <span style="margin-left:auto; display:flex; align-items:center; gap:0.5rem;">
      <label>Atom ID</label>
      <input type="text" id="atom-id-input" placeholder="paste atom id…" style="width:280px;" />
      <button onclick="loadAtomFromInput()">Inspect</button>
    </span>
  </div>

  <!-- Atom list -->
  <div class="atom-list" id="atom-list">
    <div class="empty">Loading…</div>
  </div>

  <!-- Atom detail panel -->
  <div class="detail-panel" id="detail-panel">
    <h2 id="detail-title">Atom detail</h2>
    <div id="detail-body"></div>
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
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// ── Formatting ────────────────────────────────────────────────────
function fmtTs(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    return d.toISOString().replace("T", " ").slice(0, 19) + "Z";
  } catch { return ts; }
}

function fmtBytes(b) {
  if (b < 1024) return b + " B";
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
  return (b / 1024 / 1024).toFixed(1) + " MB";
}

function badgeClass(memType) {
  if (memType === "observation") return "badge-obs";
  if (memType === "mental_model") return "badge-model";
  return "badge-raw";
}

function esc(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ── Stats ─────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const d = await authedFetch("/api/saga?view=stats");
    if (d.error) { console.warn("stats:", d.error); return; }
    document.getElementById("stat-atoms").textContent = (d.atom_count || 0).toLocaleString();
    document.getElementById("stat-sessions").textContent = (d.session_count || 0).toLocaleString();
    document.getElementById("stat-triples").textContent = (d.triple_count || 0).toLocaleString();
    document.getElementById("stat-tombstoned").textContent = (d.tombstoned_count || 0).toLocaleString();
    document.getElementById("stat-dbsize").textContent = d.db_size_bytes ? fmtBytes(d.db_size_bytes) : "—";
    document.getElementById("stat-schema").textContent = d.schema_version != null ? "v" + d.schema_version : "—";
  } catch (e) { console.warn("stats fetch failed:", e); }
}

// ── Recent atoms ──────────────────────────────────────────────────
let _channels = [];

async function loadRecent() {
  const channel = document.getElementById("channel-filter").value;
  const limit = document.getElementById("limit-select").value;
  let url = "/api/saga?view=recent&limit=" + limit;
  if (channel) url += "&channel=" + encodeURIComponent(channel);

  const list = document.getElementById("atom-list");
  list.innerHTML = '<div class="empty">Loading…</div>';

  try {
    const d = await authedFetch(url);
    if (d.error) { list.innerHTML = '<div class="error-msg">Error: ' + esc(d.error) + "</div>"; return; }

    // Populate channel dropdown from first load
    if (d.channels && d.channels.length && !_channels.length) {
      _channels = d.channels;
      const sel = document.getElementById("channel-filter");
      for (const ch of _channels) {
        const opt = document.createElement("option");
        opt.value = ch; opt.textContent = ch;
        sel.appendChild(opt);
      }
    }

    if (!d.atoms || !d.atoms.length) {
      list.innerHTML = '<div class="empty">No atoms found.</div>';
      return;
    }

    const totalLine = '<div style="padding:0.4rem 1rem; color:var(--muted); font-size:0.78rem;">'
      + 'Showing ' + d.atoms.length + ' of ' + (d.total || '?').toLocaleString() + ' atoms</div>';

    const header = '<div class="atom-list-header"><span>Content</span><span>Type</span><span>Stream</span><span>Pinned</span><span>Created</span></div>';

    const rows = d.atoms.map(a => {
      const preview = esc(a.content_preview || "(empty)");
      const typeClass = badgeClass(a.memory_type);
      const typeBadge = '<span class="badge ' + typeClass + '">' + esc(a.memory_type || "raw") + "</span>";
      const stream = esc(a.stream || "semantic");
      const pinned = a.is_pinned ? '<span class="pinned-mark" title="Pinned">📌</span>' : '';
      const ts = '<span class="ts">' + fmtTs(a.created_at) + "</span>";
      return '<div class="atom-row" onclick="loadAtom(' + JSON.stringify(a.id) + ', this)">'
        + '<span class="atom-preview">' + preview + "</span>"
        + typeBadge
        + '<span class="meta">' + stream + "</span>"
        + pinned
        + ts
        + "</div>";
    }).join("");

    list.innerHTML = totalLine + header + rows;
  } catch (e) {
    list.innerHTML = '<div class="error-msg">Fetch failed: ' + esc(String(e)) + "</div>";
  }
}

// ── Atom detail ───────────────────────────────────────────────────
let _selectedRow = null;

function loadAtomFromInput() {
  const id = document.getElementById("atom-id-input").value.trim();
  if (!id) return;
  loadAtom(id, null);
}

async function loadAtom(atomId, rowEl) {
  // Highlight selected row.
  if (_selectedRow) _selectedRow.classList.remove("selected");
  if (rowEl) { rowEl.classList.add("selected"); _selectedRow = rowEl; }

  const panel = document.getElementById("detail-panel");
  const body = document.getElementById("detail-body");
  panel.classList.add("visible");
  body.innerHTML = "<div class='empty'>Loading…</div>";
  document.getElementById("detail-title").textContent = "Atom " + atomId;

  try {
    const d = await authedFetch("/api/saga?view=atom&id=" + encodeURIComponent(atomId));
    if (d.error) { body.innerHTML = '<div class="error-msg">Error: ' + esc(d.error) + "</div>"; return; }

    const kv = (k, v) => '<div class="k">' + esc(k) + ":</div><div class='v'>" + esc(v !== null && v !== undefined ? String(v) : "—") + "</div>";

    const kvRows = [
      ["ID", d.id],
      ["Memory type", d.memory_type],
      ["Stream", d.stream],
      ["Source type", d.source_type],
      ["Session", d.session_id || "—"],
      ["Channel", d.channel_id || "—"],
      ["Arousal", d.arousal != null ? d.arousal.toFixed(3) : "—"],
      ["Valence", d.valence != null ? d.valence.toFixed(3) : "—"],
      ["Confidence", d.encoding_confidence != null ? d.encoding_confidence.toFixed(3) : "—"],
      ["Pinned", d.is_pinned ? "yes" : "no"],
      ["Created", fmtTs(d.created_at)],
      ["Access count", d.access_count != null ? d.access_count : "—"],
      ["Last access", d.last_access_ts ? fmtTs(d.last_access_ts) + " (" + esc(d.last_access_source) + ")" : "—"],
      ["Embedding", d.embedding ? d.embedding.provider + "/" + d.embedding.model + " dim=" + d.embedding.dim : "none"],
      ["Tombstoned", d.tombstoned ? "yes (" + esc(d.tombstoned_reason || "?") + ")" : "no"],
      ["Topics", Array.isArray(d.topics) ? d.topics.join(", ") || "—" : "—"],
    ].map(([k, v]) => kv(k, v)).join("");

    const relHtml = (d.relations_out && d.relations_out.length)
      ? d.relations_out.map(r =>
          '<div class="relation-row"><b>' + esc(r.relation_type) + '</b> → '
          + esc(r.target_id) + ' (conf ' + (r.confidence || 1).toFixed(2) + ')'
          + (r.target_preview ? ' — <span style="color:var(--muted)">' + esc(r.target_preview) + "…</span>" : "")
          + "</div>"
        ).join("")
      : "<div class='meta' style='font-size:0.8rem'>none</div>";

    body.innerHTML = [
      '<div class="detail-kv">' + kvRows + "</div>",
      '<div class="detail-content">' + esc(d.content || "") + "</div>",
      '<div class="detail-section"><h3>Relations out (' + (d.relations_out || []).length + ")</h3>" + relHtml + "</div>",
    ].join("");
  } catch (e) {
    body.innerHTML = '<div class="error-msg">Fetch failed: ' + esc(String(e)) + "</div>";
  }
}

// ── Init ──────────────────────────────────────────────────────────
loadStats();
loadRecent();
</script>
</body>
</html>"""


__all__ = [
    "build_recent_atoms_payload",
    "build_atom_payload",
    "build_db_stats_payload",
    "render_saga_html",
]
