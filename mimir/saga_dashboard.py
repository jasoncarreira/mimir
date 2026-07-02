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
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mimir.saga._like import escape_like_pattern

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
        search_term = f"%{escape_like_pattern(query)}%"
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
        malformed_summaries = 0
        for row in rows:
            try:
                act = _petrov(row)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                atom_id = row["id"] if "id" in row.keys() else "<unknown>"
                log.warning(
                    "saga_dashboard: skipping malformed activation summary for atom %s: %s",
                    atom_id,
                    exc,
                )
                malformed_summaries += 1
                never_accessed += 1
                continue
            if _math.isfinite(act):
                activations.append(act)
            else:
                never_accessed += 1

        if not activations:
            payload = {
                "buckets": [],
                "total": 0,
                "never_accessed": never_accessed,
                "days": days,
            }
            if malformed_summaries:
                payload["malformed_summaries"] = malformed_summaries
            return payload

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

        payload = {
            "buckets": buckets,
            "total": len(activations),
            "never_accessed": never_accessed,
            "days": days,
        }
        if malformed_summaries:
            payload["malformed_summaries"] = malformed_summaries
        return payload
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

# chainlink #611: the read-only passthrough's ONLY write protection is the
# ``mode=ro`` connection — the keyword blacklist is brittle defense-in-depth and
# does nothing to bound resource use. The row cap (above) limits rows *fetched*,
# not work *performed* or a single value's size, so a recursive-CTE compute bomb
# (CPU) or a huge ``zeroblob()``/``randomblob()`` scalar (memory) can hang or OOM
# the ``asyncio.to_thread`` worker. These two limits close that availability gap:
#   - ``_SQL_TIMEOUT_S``: wall-clock budget enforced via a progress handler that
#     aborts the running statement (covers execute AND lazy row streaming).
#   - ``_SQL_MAX_VALUE_BYTES``: caps any single string/blob/row via
#     ``SQLITE_LIMIT_LENGTH`` so an oversized scalar raises before allocating.
# Both are env-tunable for operators who knowingly enable the SQL console.
_SQL_TIMEOUT_S = float(os.environ.get("MIMIR_SAGA_SQL_TIMEOUT_S") or 5.0)
_SQL_MAX_VALUE_BYTES = int(os.environ.get("MIMIR_SAGA_SQL_MAX_VALUE_BYTES") or 10_000_000)
# VM opcodes between progress-handler deadline checks (cheap; sub-ms cadence).
_SQL_PROGRESS_OPS = 1000


def _apply_sql_value_limit(conn: sqlite3.Connection) -> None:
    """Cap the max size of any single string/blob/row (chainlink #611) so an
    oversized ``zeroblob()``/``randomblob()`` scalar raises 'string or blob too
    big' instead of allocating. ``setlimit`` + the constant are Python 3.11+;
    degrade gracefully (no cap) if unavailable."""
    setlimit = getattr(conn, "setlimit", None)
    category = getattr(sqlite3, "SQLITE_LIMIT_LENGTH", None)
    if setlimit is None or category is None:
        return
    try:
        setlimit(category, _SQL_MAX_VALUE_BYTES)
    except (sqlite3.Error, OverflowError, ValueError):
        pass

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

    # chainlink #611: bound per-value memory + wall-clock CPU. mode=ro already
    # prevents writes; these stop a compute/memory DoS the row cap can't.
    _apply_sql_value_limit(conn)
    deadline = time.monotonic() + _SQL_TIMEOUT_S
    timed_out = False

    def _abort_if_overtime() -> int:
        nonlocal timed_out
        if time.monotonic() > deadline:
            timed_out = True
            return 1
        return 0

    conn.set_progress_handler(_abort_if_overtime, _SQL_PROGRESS_OPS)

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
        if timed_out:
            log.warning("saga_dashboard: sql query exceeded %ss limit", _SQL_TIMEOUT_S)
            return {
                "error": f"query exceeded the {_SQL_TIMEOUT_S:g}s time limit",
                "rejected": False,
            }
        log.warning("saga_dashboard: sql query failed: %s", exc)
        return {"error": str(exc), "rejected": False}
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


# ─── HTML shell ──────────────────────────────────────────────────


def render_saga_html() -> str:
    """Return the /saga HTML shell.

    Same dark-mode palette and auth pattern as /ops: the page is exempt
    from the API-key middleware (so the JS can prompt on first visit),
    but all ``/api/saga`` calls require the key via X-API-Key.
    """
    return _load_saga_html()


# chainlink #243: dashboard HTML lives in a sibling .html file.
# Lazy-loaded + cached so the first /saga request pays the read but
# the rest is in-memory.
_SAGA_HTML: str | None = None


def _load_saga_html() -> str:
    global _SAGA_HTML
    if _SAGA_HTML is None:
        _SAGA_HTML = (
            Path(__file__).parent / "saga_dashboard.html"
        ).read_text(encoding="utf-8")
    return _SAGA_HTML



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
