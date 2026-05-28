"""Saga DB ops page — reads ``state/saga.db`` on demand and renders
an operator-facing view of the agent's memory atoms at ``/saga``.

Mirrors the shape of ``ops_dashboard.py``: pure-data ``build_*_payload``
functions return dicts; ``render_saga_html()`` returns the HTML shell.
No HTML in the payload functions — same separation as ops_dashboard.

Chainlink #222 — Phase 1:
  /saga              — HTML shell (same dark-mode palette as /ops)
  /api/saga?view=recent&channel=...&limit=N  — recent atoms
  /api/saga?view=atom&id=...                 — single atom inspector

Phase 2:
  /api/saga?view=search&q=...&channel=...   — text search over atom content
  /api/saga?view=activation_hist&days=7     — activation score histogram
  /api/saga?view=clusters                   — cluster browser by session

Phase 3:
  POST /api/saga/sql                        — read-only SQL passthrough
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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


# ─── Phase 2 payload builders ────────────────────────────────────


def build_search_payload(
    db_path: Path,
    query: str,
    channel: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Full-text substring search over atom content.

    Case-insensitive LIKE '%query%' over the ``content`` column.
    Optionally scoped to a channel via a sessions join.
    Capped at ``limit`` results (max 100).

    Returns ``{atoms: [...], total_matched: int, query: str, channel_filter: str|None}``.
    """
    limit = max(1, min(limit, 100))

    conn = _open_conn(db_path)
    if conn is None:
        return {"error": "saga db not found or unreadable", "atoms": [], "total_matched": 0}

    if not query:
        return {"error": "q param required", "atoms": [], "total_matched": 0}

    try:
        search_term = f"%{query}%"
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
                a.is_pinned,
                a.created_at,
                a.session_id,
                s.channel_id
            FROM atoms a
            LEFT JOIN sessions s ON s.id = a.session_id
            WHERE a.tombstoned = 0
              AND a.content LIKE ? ESCAPE '\\'
        """
        params: list[Any] = [search_term]
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
            content = d.get("content") or ""
            d["content_preview"] = content[:200] + ("…" if len(content) > 200 else "")
            d.pop("content", None)
            atoms.append(d)

        # Count total matches (without LIMIT) for the metadata line.
        count_sql = """
            SELECT COUNT(*) FROM atoms a
            LEFT JOIN sessions s ON s.id = a.session_id
            WHERE a.tombstoned = 0 AND a.content LIKE ? ESCAPE '\\'
        """
        count_params: list[Any] = [search_term]
        if channel:
            count_sql += " AND s.channel_id = ?"
            count_params.append(channel)
        total_matched = conn.execute(count_sql, count_params).fetchone()[0]

        return {
            "atoms": atoms,
            "total_matched": total_matched,
            "query": query,
            "channel_filter": channel,
            "limit": limit,
        }
    except sqlite3.Error as exc:
        log.warning("saga_dashboard: search query failed: %s", exc)
        return {"error": str(exc), "atoms": [], "total_matched": 0}
    finally:
        conn.close()


def build_activation_hist_payload(
    db_path: Path,
    days: int = 7,
) -> dict[str, Any]:
    """Activation score histogram for atoms created within ``days``.

    Computes the Petrov OL activation for each atom that has an entry
    in ``atom_access_summary``, limiting to atoms created in the last
    ``days`` days.  Atoms with no summary entry (never accessed since
    the summary table was introduced) are counted as zero-accessed and
    contribute a ``-inf`` activation — these are reported separately
    as ``never_accessed`` rather than polluting the histogram buckets.

    Buckets the finite activations into 10 equal-width intervals from
    the minimum observed finite activation to the maximum.  Returns:

      {
        buckets: [{range_start, range_end, count}],   # 10 items
        total: int,            # atoms with finite activation
        never_accessed: int,   # atoms with no summary / -inf activation
        days: int,
      }

    If no atoms have finite activation, returns empty buckets.
    """
    import math as _math

    days = max(1, days)

    conn = _open_conn(db_path)
    if conn is None:
        return {"error": "saga db not found or unreadable", "buckets": [], "total": 0}

    try:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        rows = conn.execute(
            """
            SELECT
                a.id,
                aas.recent_ts_json,
                aas.recent_weights_json,
                aas.old_count,
                aas.old_weight_sum,
                aas.old_oldest_ts
            FROM atoms a
            LEFT JOIN atom_access_summary aas ON aas.atom_id = a.id
            WHERE a.tombstoned = 0
              AND a.created_at >= ?
            """,
            (cutoff,),
        ).fetchall()

        now_utc = datetime.now(timezone.utc)
        DECAY_D = 0.5
        EPSILON = 1.0

        def _petrov(row: sqlite3.Row) -> float:
            """Compute Petrov OL activation for a summary row."""
            recent_ts_json = row["recent_ts_json"]
            if not recent_ts_json:
                return float("-inf")
            recent_ts = json.loads(recent_ts_json or "[]")
            recent_weights = json.loads(row["recent_weights_json"] or "[]")
            old_count = int(row["old_count"] or 0)
            old_weight_sum = float(row["old_weight_sum"] or 0.0)
            old_oldest_ts = row["old_oldest_ts"]

            total = 0.0
            for ts_str, weight in zip(recent_ts, recent_weights):
                t = datetime.fromisoformat(ts_str)
                age_s = max((now_utc - t.replace(tzinfo=timezone.utc) if t.tzinfo is None else now_utc - t).total_seconds(), EPSILON)
                total += weight * (age_s ** (-DECAY_D))

            if old_count > 0 and old_oldest_ts is not None and old_weight_sum > 0:
                import math
                oldest = datetime.fromisoformat(old_oldest_ts)
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=timezone.utc)
                if recent_ts:
                    recent_dts = sorted(
                        (datetime.fromisoformat(t).replace(tzinfo=timezone.utc) if datetime.fromisoformat(t).tzinfo is None else datetime.fromisoformat(t))
                        for t in recent_ts
                    )
                    upper = recent_dts[0]
                else:
                    upper = now_utc
                mean_weight = old_weight_sum / old_count
                upper_age = max((now_utc - upper).total_seconds(), EPSILON)
                oldest_age = max((now_utc - oldest).total_seconds(), EPSILON)
                if abs(1.0 - DECAY_D) < 1e-9:
                    integral = math.log(oldest_age / upper_age)
                else:
                    integral = (
                        (oldest_age ** (1.0 - DECAY_D)) - (upper_age ** (1.0 - DECAY_D))
                    ) / (1.0 - DECAY_D)
                window_seconds = max(oldest_age - upper_age, EPSILON)
                total += old_count * mean_weight * (integral / window_seconds)

            if total <= 0.0:
                return float("-inf")
            import math
            return math.log(total)

        activations = []
        never_accessed = 0
        for row in rows:
            act = _petrov(row)
            if _math.isfinite(act):
                activations.append(act)
            else:
                never_accessed += 1

        if not activations:
            return {
                "buckets": [],
                "total": 0,
                "never_accessed": never_accessed,
                "days": days,
            }

        min_act = min(activations)
        max_act = max(activations)
        n_buckets = 10

        if max_act == min_act:
            # All activations identical — one meaningful bucket.
            buckets = [
                {
                    "range_start": round(min_act, 4),
                    "range_end": round(max_act, 4),
                    "count": len(activations),
                }
            ]
        else:
            width = (max_act - min_act) / n_buckets
            bucket_counts = [0] * n_buckets
            for act in activations:
                idx = min(int((act - min_act) / width), n_buckets - 1)
                bucket_counts[idx] += 1
            buckets = [
                {
                    "range_start": round(min_act + i * width, 4),
                    "range_end": round(min_act + (i + 1) * width, 4),
                    "count": bucket_counts[i],
                }
                for i in range(n_buckets)
            ]

        return {
            "buckets": buckets,
            "total": len(activations),
            "never_accessed": never_accessed,
            "days": days,
        }
    except sqlite3.Error as exc:
        log.warning("saga_dashboard: activation_hist query failed: %s", exc)
        return {"error": str(exc), "buckets": [], "total": 0}
    finally:
        conn.close()


def build_clusters_payload(
    db_path: Path,
    sample_size: int = 3,
) -> dict[str, Any]:
    """List clusters by session with a sample of member atoms.

    Groups non-tombstoned atoms by ``session_id`` (NULL session_id atoms
    are grouped as "unclustered").  For each group returns the session_id
    (used as cluster_id), the count of atoms in that group, and a sample
    of up to ``sample_size`` atoms showing a truncated content preview.

    Returns:
      {
        clusters: [
          {
            cluster_id: str | None,
            size: int,
            sample_atoms: [{id, content_preview}],
          },
          ...
        ],
        total_clusters: int,
        total_atoms: int,
      }

    Clusters are sorted largest-first so the most active sessions appear
    at the top of the operator view.
    """
    sample_size = max(1, min(sample_size, 20))

    conn = _open_conn(db_path)
    if conn is None:
        return {"error": "saga db not found or unreadable", "clusters": []}

    try:
        # Fetch all non-tombstoned atoms ordered so we can group and sample.
        rows = conn.execute(
            """
            SELECT id, content, session_id
            FROM atoms
            WHERE tombstoned = 0
            ORDER BY session_id NULLS LAST, created_at DESC
            """
        ).fetchall()

        # Group by session_id.
        groups: dict[str | None, list[dict[str, Any]]] = {}
        for row in rows:
            key = row["session_id"]  # may be None
            if key not in groups:
                groups[key] = []
            groups[key].append({"id": row["id"], "content": row["content"]})

        clusters = []
        for session_id, atoms_in_group in groups.items():
            sample = []
            for atom in atoms_in_group[:sample_size]:
                content = atom["content"] or ""
                sample.append({
                    "id": atom["id"],
                    "content_preview": content[:120] + ("…" if len(content) > 120 else ""),
                })
            clusters.append({
                "cluster_id": session_id,
                "size": len(atoms_in_group),
                "sample_atoms": sample,
            })

        # Sort largest-first.
        clusters.sort(key=lambda c: c["size"], reverse=True)

        total_atoms = sum(c["size"] for c in clusters)
        return {
            "clusters": clusters,
            "total_clusters": len(clusters),
            "total_atoms": total_atoms,
        }
    except sqlite3.Error as exc:
        log.warning("saga_dashboard: clusters query failed: %s", exc)
        return {"error": str(exc), "clusters": []}
    finally:
        conn.close()


# ─── Phase 3: read-only SQL passthrough ──────────────────────────

# Maximum rows returned from a single SQL query.
_SQL_MAX_ROWS = 1000

# Write keywords that must not appear anywhere in a "read-only" statement.
# Covers DML (INSERT/UPDATE/DELETE/REPLACE), DDL (CREATE/DROP/ALTER),
# plus ATTACH/DETACH (mounting external DBs) and PRAGMA (many mutate state).
_SQL_WRITE_KEYWORDS_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA)\b",
    re.IGNORECASE,
)

# First keyword of a statement must be one of these.
_SQL_ALLOWED_FIRST_WORDS = {"SELECT", "EXPLAIN", "WITH"}


def _validate_sql_readonly(sql: str) -> str | None:
    """Return an error message string if *sql* is not a safe read-only
    statement, or ``None`` if it looks OK.

    Two-layer check:
    1. First keyword must be SELECT, EXPLAIN, or WITH (CTEs).
    2. No write/mutating keywords anywhere in the statement.
    """
    stripped = sql.strip()
    if not stripped:
        return "SQL statement is empty"
    first_word = stripped.split(None, 1)[0].upper()
    if first_word not in _SQL_ALLOWED_FIRST_WORDS:
        return (
            f"Only SELECT, EXPLAIN, and WITH (CTEs) are allowed; "
            f"got {first_word!r}"
        )
    m = _SQL_WRITE_KEYWORDS_RE.search(stripped)
    if m:
        return (
            f"Write keyword {m.group(0).upper()!r} is not allowed — "
            "only read-only queries are permitted"
        )
    return None


def build_sql_payload(db_path: Path, sql: str) -> dict[str, Any]:
    """Execute a read-only SQL query and return results as a JSON-serialisable dict.

    Safety: only SELECT, EXPLAIN, and WITH (CTE→SELECT) are accepted.
    Write keywords (INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, REPLACE,
    ATTACH, DETACH, PRAGMA) are rejected before execution.

    Results are capped at ``_SQL_MAX_ROWS`` rows (1 000).

    Returns on success::

        {
          "columns": [str, ...],
          "rows": [[Any, ...], ...],
          "row_count": int,
          "truncated": bool,   # True when >1 000 rows were available
        }

    Returns on rejection (safety gate) or SQL error::

        {
          "error": str,
          "rejected": bool,   # True = safety gate fired, False = SQL error
        }
    """
    err = _validate_sql_readonly(sql)
    if err:
        return {"error": err, "rejected": True}

    conn = _open_conn(db_path)
    if conn is None:
        return {"error": "saga db not found or unreadable", "rejected": False}

    try:
        cur = conn.execute(sql)
        columns = [desc[0] for desc in (cur.description or [])]
        # Fetch one extra row to detect truncation without loading all rows.
        rows_raw = cur.fetchmany(_SQL_MAX_ROWS + 1)
        truncated = len(rows_raw) > _SQL_MAX_ROWS
        rows_raw = rows_raw[:_SQL_MAX_ROWS]

        # Coerce non-JSON-native types so json.dumps doesn't choke.
        rows: list[list[Any]] = []
        for row in rows_raw:
            coerced = []
            for val in row:
                if isinstance(val, bytes):
                    coerced.append(f"<bytes len={len(val)}>")
                elif val is None or isinstance(val, (int, float, str, bool)):
                    coerced.append(val)
                else:
                    coerced.append(str(val))
            rows.append(coerced)

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
    except sqlite3.Error as exc:
        log.warning("saga_dashboard: sql query failed: %s", exc)
        return {"error": str(exc), "rejected": False}
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
    /* Phase 2: section tabs + search + histogram + clusters */
    .section-tabs {
      display: flex; gap: 0.3rem; margin-bottom: 1rem; flex-wrap: wrap;
    }
    .tab-btn {
      background: var(--paper-strong);
      border: 1px solid var(--line);
      color: var(--muted);
      border-radius: 6px;
      padding: 0.35rem 0.9rem;
      font-size: 0.83rem;
      font-family: inherit;
      cursor: pointer;
      transition: background 0.12s, color 0.12s;
    }
    .tab-btn:hover { background: var(--accent-soft); color: var(--accent); }
    .tab-btn.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
    .section { display: none; }
    .section.visible { display: block; }
    /* Search section */
    .search-controls {
      display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap;
      margin-bottom: 0.9rem;
    }
    .search-controls input[type=text] { width: 300px; }
    /* Histogram */
    .hist-bar-row {
      display: flex; align-items: center; gap: 0.5rem;
      margin-bottom: 0.25rem; font-size: 0.8rem;
    }
    .hist-label { width: 120px; color: var(--muted); text-align: right; font-variant-numeric: tabular-nums; flex-shrink: 0; }
    .hist-bar-wrap { flex: 1; background: var(--paper-strong-2); border-radius: 3px; height: 18px; overflow: hidden; }
    .hist-bar { background: var(--accent); height: 100%; border-radius: 3px; transition: width 0.3s; }
    .hist-count { width: 40px; color: var(--muted); font-variant-numeric: tabular-nums; }
    /* Clusters */
    .cluster-card {
      background: var(--paper-strong);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0.7rem 1rem;
      margin-bottom: 0.6rem;
    }
    .cluster-header {
      display: flex; align-items: baseline; gap: 0.6rem; margin-bottom: 0.4rem;
    }
    .cluster-id { font-size: 0.78rem; color: var(--muted); font-family: "Courier New", monospace; word-break: break-all; }
    .cluster-size { font-size: 0.85rem; font-weight: 600; color: var(--accent); }
    .cluster-sample { font-size: 0.8rem; color: var(--muted); line-height: 1.5; }
    .cluster-atom-preview { padding: 0.15rem 0; border-bottom: 1px solid var(--line); }
    .cluster-atom-preview:last-child { border-bottom: none; }
    .section-wrap {
      background: var(--paper-strong);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 0.8rem 1rem;
      margin-bottom: 1.2rem;
    }
    /* SQL result table */
    .sql-result-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.8rem;
      font-variant-numeric: tabular-nums;
    }
    .sql-result-table th {
      background: var(--paper-strong-2);
      color: var(--muted);
      text-align: left;
      padding: 0.4rem 0.6rem;
      border-bottom: 1px solid var(--line);
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      white-space: nowrap;
    }
    .sql-result-table td {
      padding: 0.35rem 0.6rem;
      border-bottom: 1px solid var(--line);
      max-width: 400px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .sql-result-table tr:last-child td { border-bottom: none; }
    .sql-result-table tr:hover td { background: var(--accent-soft); }
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

  <!-- Section tabs -->
  <div class="section-tabs">
    <button class="tab-btn active" onclick="showSection('recent', this)">Recent</button>
    <button class="tab-btn" onclick="showSection('search', this)">Search</button>
    <button class="tab-btn" onclick="showSection('activation', this)">Activation</button>
    <button class="tab-btn" onclick="showSection('clusters', this)">Clusters</button>
    <button class="tab-btn" onclick="showSection('sql', this)">SQL</button>
  </div>

  <!-- SECTION: Recent atoms -->
  <div class="section visible" id="section-recent">
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

  <!-- SECTION: Search -->
  <div class="section" id="section-search">
    <div class="search-controls controls">
      <label>Query</label>
      <input type="text" id="search-input" placeholder="search atom content…" style="width:300px;"
             onkeydown="if(event.key==='Enter') runSearch()" />
      <label>Channel</label>
      <select id="search-channel-filter">
        <option value="">all channels</option>
      </select>
      <button onclick="runSearch()">Search</button>
    </div>
    <div id="search-results">
      <div class="empty">Enter a query to search atoms.</div>
    </div>
  </div>

  <!-- SECTION: Activation histogram -->
  <div class="section" id="section-activation">
    <div class="controls">
      <label>Days window</label>
      <select id="hist-days">
        <option value="1">1 day</option>
        <option value="7" selected>7 days</option>
        <option value="30">30 days</option>
        <option value="90">90 days</option>
      </select>
      <button onclick="loadActivationHist()">Load</button>
    </div>
    <div id="hist-container">
      <div class="empty">Click Load to compute activation histogram.</div>
    </div>
  </div>

  <!-- SECTION: Clusters -->
  <div class="section" id="section-clusters">
    <div class="controls">
      <button onclick="loadClusters()">Load clusters</button>
      <span id="cluster-meta" class="meta"></span>
    </div>
    <div id="clusters-container">
      <div class="empty">Click Load to browse session clusters.</div>
    </div>
  </div>

  <!-- SECTION: SQL (expert mode) -->
  <div class="section" id="section-sql">
    <div style="color:var(--warn); font-size:0.8rem; margin-bottom:0.6rem;">
      &#9888; Expert mode — read-only queries only (SELECT / EXPLAIN / WITH).
      Write keywords are rejected before execution. Results capped at 1&#8239;000 rows.
    </div>
    <div style="display:flex; flex-direction:column; gap:0.5rem; margin-bottom:0.8rem;">
      <textarea id="sql-input" rows="5"
        style="width:100%; font-family:'Courier New',monospace; font-size:0.82rem;
               background:var(--paper); color:var(--ink); border:1px solid var(--line);
               border-radius:6px; padding:0.55rem 0.7rem; resize:vertical;"
        placeholder="SELECT id, content, memory_type, created_at FROM atoms WHERE tombstoned=0 ORDER BY created_at DESC LIMIT 20"
        onkeydown="if((event.ctrlKey||event.metaKey) && event.key==='Enter') runSql()"></textarea>
      <div style="display:flex; gap:0.5rem; align-items:center;">
        <button onclick="runSql()">Run Query</button>
        <span class="meta" style="font-size:0.78rem;">Ctrl+Enter to run</span>
      </div>
    </div>
    <div id="sql-results">
      <div class="empty">Enter a SELECT query above, then click Run Query.</div>
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

    // Populate channel dropdowns from first load
    if (d.channels && d.channels.length && !_channels.length) {
      _channels = d.channels;
      const sel = document.getElementById("channel-filter");
      for (const ch of _channels) {
        const opt = document.createElement("option");
        opt.value = ch; opt.textContent = ch;
        sel.appendChild(opt);
      }
      _populateSearchChannels(_channels);
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

// ── Section tabs ──────────────────────────────────────────────────
function showSection(name, btn) {
  document.querySelectorAll(".section").forEach(s => s.classList.remove("visible"));
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
  const sec = document.getElementById("section-" + name);
  if (sec) sec.classList.add("visible");
  if (btn) btn.classList.add("active");
}

// ── Search ────────────────────────────────────────────────────────
async function runSearch() {
  const q = document.getElementById("search-input").value.trim();
  if (!q) return;
  const channel = document.getElementById("search-channel-filter").value;
  let url = "/api/saga?view=search&q=" + encodeURIComponent(q);
  if (channel) url += "&channel=" + encodeURIComponent(channel);

  const container = document.getElementById("search-results");
  container.innerHTML = '<div class="empty">Searching…</div>';

  try {
    const d = await authedFetch(url);
    if (d.error) { container.innerHTML = '<div class="error-msg">Error: ' + esc(d.error) + "</div>"; return; }

    if (!d.atoms || !d.atoms.length) {
      container.innerHTML = '<div class="empty">No results for <b>' + esc(q) + "</b>.</div>";
      return;
    }

    const meta = '<div style="padding:0.4rem 0; color:var(--muted); font-size:0.78rem;">'
      + d.atoms.length + ' of ' + (d.total_matched || '?') + ' matches for <b>' + esc(q) + "</b></div>";

    const header = '<div class="atom-list-header"><span>Content</span><span>Type</span><span>Stream</span><span>Pinned</span><span>Created</span></div>';

    const rows = d.atoms.map(a => {
      const preview = esc(a.content_preview || "(empty)");
      const typeClass = badgeClass(a.memory_type);
      const typeBadge = '<span class="badge ' + typeClass + '">' + esc(a.memory_type || "raw") + "</span>";
      const stream = esc(a.stream || "semantic");
      const pinned = a.is_pinned ? '<span class="pinned-mark" title="Pinned">\\u{1F4CC}</span>' : '';
      const ts = '<span class="ts">' + fmtTs(a.created_at) + "</span>";
      return '<div class="atom-row" onclick="loadAtom(' + JSON.stringify(a.id) + ', this)">'
        + '<span class="atom-preview">' + preview + "</span>"
        + typeBadge
        + '<span class="meta">' + stream + "</span>"
        + pinned
        + ts
        + "</div>";
    }).join("");

    container.innerHTML = '<div class="atom-list">' + meta + header + rows + "</div>";
  } catch (e) {
    container.innerHTML = '<div class="error-msg">Fetch failed: ' + esc(String(e)) + "</div>";
  }
}

// Populate search channel dropdown from the same source as the recent view.
function _populateSearchChannels(channels) {
  const sel = document.getElementById("search-channel-filter");
  if (!sel || sel.options.length > 1) return;
  for (const ch of channels) {
    const opt = document.createElement("option");
    opt.value = ch; opt.textContent = ch;
    sel.appendChild(opt);
  }
}

// ── Activation histogram ───────────────────────────────────────────
async function loadActivationHist() {
  const days = document.getElementById("hist-days").value;
  const container = document.getElementById("hist-container");
  container.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const d = await authedFetch("/api/saga?view=activation_hist&days=" + days);
    if (d.error) { container.innerHTML = '<div class="error-msg">Error: ' + esc(d.error) + "</div>"; return; }

    if (!d.buckets || !d.buckets.length) {
      container.innerHTML = '<div class="empty">No activation data for the last ' + esc(days) + ' day(s).'
        + (d.never_accessed ? ' (' + d.never_accessed + ' atoms never accessed)' : '') + "</div>";
      return;
    }

    const maxCount = Math.max(...d.buckets.map(b => b.count), 1);
    const meta = '<div style="color:var(--muted); font-size:0.8rem; margin-bottom:0.6rem;">'
      + d.total + ' atoms with finite activation; '
      + (d.never_accessed || 0) + ' never accessed — last ' + d.days + ' day(s)</div>';

    const bars = d.buckets.map(b => {
      const pct = (b.count / maxCount * 100).toFixed(1);
      return '<div class="hist-bar-row">'
        + '<div class="hist-label">[' + b.range_start.toFixed(2) + ', ' + b.range_end.toFixed(2) + ')</div>'
        + '<div class="hist-bar-wrap"><div class="hist-bar" style="width:' + pct + '%"></div></div>'
        + '<div class="hist-count">' + b.count + "</div>"
        + "</div>";
    }).join("");

    container.innerHTML = '<div class="section-wrap">' + meta + bars + "</div>";
  } catch (e) {
    container.innerHTML = '<div class="error-msg">Fetch failed: ' + esc(String(e)) + "</div>";
  }
}

// ── Clusters ──────────────────────────────────────────────────────
async function loadClusters() {
  const container = document.getElementById("clusters-container");
  const metaEl = document.getElementById("cluster-meta");
  container.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const d = await authedFetch("/api/saga?view=clusters");
    if (d.error) { container.innerHTML = '<div class="error-msg">Error: ' + esc(d.error) + "</div>"; return; }

    if (!d.clusters || !d.clusters.length) {
      container.innerHTML = '<div class="empty">No clusters found.</div>';
      return;
    }

    metaEl.textContent = d.total_clusters + ' clusters, ' + (d.total_atoms || 0) + ' atoms total';

    const cards = d.clusters.map(c => {
      const cid = c.cluster_id ? esc(c.cluster_id) : '<span style="color:var(--muted)">(no session)</span>';
      const sample = (c.sample_atoms || []).map(a =>
        '<div class="cluster-atom-preview">'
        + '<span style="color:var(--accent); font-size:0.72rem; font-family:monospace">' + esc(a.id) + '</span> — '
        + esc(a.content_preview || "")
        + "</div>"
      ).join("");
      return '<div class="cluster-card">'
        + '<div class="cluster-header">'
        + '<span class="cluster-size">' + c.size + ' atoms</span>'
        + '<span class="cluster-id">' + cid + "</span>"
        + "</div>"
        + '<div class="cluster-sample">' + (sample || '<span class="meta">no preview</span>') + "</div>"
        + "</div>";
    }).join("");

    container.innerHTML = cards;
  } catch (e) {
    container.innerHTML = '<div class="error-msg">Fetch failed: ' + esc(String(e)) + "</div>";
  }
}

// ── SQL passthrough ───────────────────────────────────────────────
async function runSql() {
  const sql = document.getElementById("sql-input").value.trim();
  if (!sql) return;
  const container = document.getElementById("sql-results");
  container.innerHTML = '<div class="empty">Running\\u2026</div>';

  try {
    const k = getApiKey();
    const headers = {"Content-Type": "application/json"};
    if (k) headers["X-API-Key"] = k;
    const r = await fetch("/api/saga/sql", {
      method: "POST",
      headers,
      body: JSON.stringify({sql}),
    });
    if (r.status === 401) {
      localStorage.removeItem("mimir_api_key");
      container.innerHTML = '<div class="error-msg">Unauthorized \\u2014 bad API key?</div>';
      return;
    }
    const d = await r.json();
    if (d.error) {
      const label = d.rejected ? "Rejected" : "Error";
      container.innerHTML = '<div class="error-msg">' + label + ': ' + esc(d.error) + "</div>";
      return;
    }
    if (!d.columns || !d.columns.length) {
      container.innerHTML = '<div class="empty">Query executed — no columns returned (EXPLAIN or empty result).</div>';
      if (d.rows && d.rows.length) {
        container.innerHTML = '<div class="empty">' + esc(JSON.stringify(d.rows)) + "</div>";
      }
      return;
    }

    const truncNote = d.truncated
      ? '<div style="color:var(--warn); font-size:0.78rem; margin-bottom:0.4rem;">'
        + "Results truncated at 1\\u202F000 rows.</div>"
      : "";
    const rowMeta = '<div style="color:var(--muted); font-size:0.78rem; margin-bottom:0.4rem;">'
      + d.row_count + " row" + (d.row_count !== 1 ? "s" : "") + "</div>";

    const thCells = d.columns.map(c => "<th>" + esc(c) + "</th>").join("");
    const tHead = "<thead><tr>" + thCells + "</tr></thead>";
    const tRows = d.rows.map(row => {
      const cells = row.map(v => "<td>" + esc(v !== null ? String(v) : "NULL") + "</td>").join("");
      return "<tr>" + cells + "</tr>";
    }).join("");
    const tBody = "<tbody>" + tRows + "</tbody>";

    container.innerHTML = truncNote + rowMeta
      + '<div style="overflow-x:auto"><table class="sql-result-table">'
      + tHead + tBody + "</table></div>";
  } catch (e) {
    container.innerHTML = '<div class="error-msg">Fetch failed: ' + esc(String(e)) + "</div>";
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
    "build_search_payload",
    "build_activation_hist_payload",
    "build_clusters_payload",
    "build_sql_payload",
    "render_saga_html",
]
