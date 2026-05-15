"""reflect — session-end bookkeeping.

Triggered at session_boundary turn (mimir's synthesis-turn mechanism).
Emits exactly one session_boundary atom per session, plus the
session_member relations that link the boundary to atoms accessed
during the session.

Observation synthesis is NOT done here — it lives in
``mimir.memory.consolidate.consolidate()``, which runs on a cron over
cross-session evidence. The within-session synthesis hook that lived
here through earlier iterations was removed (2026-05-13): no
production caller used it, and the cluster + synth + relations logic
duplicated consolidate.py's path. Cross-session evidence accumulates
into tighter, more reliable clusters than single-session snapshots.

Idempotency: reflect(session_id) called twice returns the existing
session_boundary atom and skips re-synthesis. The agent can call
saga_end_session multiple times during one session_end turn (e.g. via
retries) without re-doing the work.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .store import store


# Injected callables — same pattern as store.EmbedFn.

# Boundary synthesis: agent + atoms → boundary content fields.
# Saga's current saga_end_session tool's contract (mimir's synthesis
# turn renders these). Returns a dict with summary, topics_discussed,
# decisions_made, unfinished, emotional_state.
BoundarySynthFn = Callable[[list[dict], dict | None], dict]


@dataclass
class ReflectResult:
    session_id: str
    boundary_atom_id: str | None = None
    boundary_created: bool = False         # False = pre-existing (idempotent re-run)
    session_member_count: int = 0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_atoms(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """Atoms accessed or stored during the session. Distinct by atom_id.

    Two sources contribute:
    - atoms.session_id matches (atoms born in this session)
    - access_events.session_id matches (atoms accessed in this session)

    Tombstoned atoms excluded — reflect operates on the live working set.
    """
    rows = conn.execute("""
        SELECT DISTINCT a.id, a.content, a.stream, a.memory_type,
               a.source_type, a.created_at, a.topics, a.metadata
        FROM atoms a
        WHERE a.tombstoned = 0
          AND (
            a.session_id = ?
            OR a.id IN (
                SELECT atom_id FROM access_events WHERE session_id = ?
            )
          )
    """, (session_id, session_id)).fetchall()
    cols = ("id", "content", "stream", "memory_type",
            "source_type", "created_at", "topics", "metadata")
    return [dict(zip(cols, r)) for r in rows]


def _existing_boundary(
    conn: sqlite3.Connection, session_id: str,
) -> str | None:
    """Return atom_id of the session_boundary for this session if one
    exists. Powers idempotency on reflect re-calls."""
    row = conn.execute("""
        SELECT id FROM atoms
        WHERE source_type = 'session_boundary'
          AND session_id = ?
          AND tombstoned = 0
    """, (session_id,)).fetchone()
    return row[0] if row else None


def _link_session_members(
    conn: sqlite3.Connection,
    boundary_id: str,
    atom_ids: list[str],
) -> int:
    """Insert session_member relations from boundary → each session
    atom. Caller-controlled transaction; this just runs statements.

    Idempotent via INSERT OR IGNORE (the PK includes relation_type,
    so duplicate boundary→atom pairs are no-ops)."""
    if not atom_ids:
        return 0
    now = _utc_now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO atom_relations "
        "(source_id, target_id, relation_type, confidence, created_at) "
        "VALUES (?, ?, 'session_member', 1.0, ?)",
        [(boundary_id, aid, now) for aid in atom_ids],
    )
    return len(atom_ids)


def reflect(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    channel_id: str | None,
    embed_fn,
    boundary_synth_fn: BoundarySynthFn,
    boundary_context: dict | None = None,
    agent_id: str = "default",
) -> ReflectResult:
    """Session-end bookkeeping. See module docstring.

    Writes the session_boundary atom + session_member relations + the
    sessions table row. Does NOT synthesize observations — that's
    consolidate.py's job (cron-driven, cross-session).

    ``boundary_context`` is optional state from mimir's lifecycle layer
    (e.g., recent boundaries to chain from, the agent's running
    emotional_state). Passed through to ``boundary_synth_fn`` so the
    synthesis can stitch sessions together coherently.
    """
    # Idempotency: short-circuit if a boundary already exists. Still
    # upsert the sessions row though — a caller re-ending a session
    # with a freshly-resolved channel_id (e.g., the dispatcher learned
    # which channel the boundary belongs to between calls) should be
    # able to backfill that without us silently no-opping the row.
    existing_bid = _existing_boundary(conn, session_id)
    if existing_bid is not None:
        if channel_id is not None:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE sessions SET channel_id = ? "
                    "WHERE id = ? AND (channel_id IS NULL OR channel_id != ?)",
                    (channel_id, session_id, channel_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                # Non-fatal — the boundary still resolves; the
                # channel_id backfill is best-effort.
        return ReflectResult(
            session_id=session_id,
            boundary_atom_id=existing_bid,
            boundary_created=False,
        )

    atoms = _session_atoms(conn, session_id)

    # ─── Always: synthesize + store the session_boundary ──────────
    fields = boundary_synth_fn(atoms, boundary_context)
    # Render the boundary content as a single coherent block. The
    # individual fields land in metadata so the prompt-build path can
    # render them structured if desired.
    content_parts = [fields.get("summary", "").strip()]
    topics = fields.get("topics_discussed") or []
    decisions = fields.get("decisions_made") or []
    unfinished = fields.get("unfinished") or []
    emotional_state = fields.get("emotional_state")
    if topics:
        content_parts.append("Topics: " + "; ".join(topics))
    if decisions:
        content_parts.append("Decisions: " + "; ".join(decisions))
    if unfinished:
        content_parts.append("Unfinished: " + "; ".join(unfinished))
    if emotional_state:
        content_parts.append(f"Emotional state: {emotional_state}")
    content = "\n\n".join(p for p in content_parts if p)
    if not content.strip():
        content = "[session ended; no significant activity]"
    # Append a session-unique discriminator so two boundaries with
    # otherwise-identical synthesis text don't collide on the UNIQUE
    # (content_hash, agent_id) index. The discriminator is visible in
    # the content (rendered to the agent) but small enough to ignore;
    # it could be hidden in metadata instead if we ever care.
    content = f"{content}\n\n[session={session_id}]"

    boundary_result = store(
        conn, content,
        embed_fn=embed_fn,
        memory_type="raw",   # session_boundary is a raw with a marker source_type
        source_type="session_boundary",
        topics=topics,
        metadata={
            "topics_discussed": topics,
            "decisions_made": decisions,
            "unfinished": unfinished,
            "emotional_state": emotional_state,
        },
        agent_id=agent_id,
        session_id=session_id,
    )
    boundary_id = boundary_result.atom_id

    # ─── Always: link session_member relations from boundary → raws ──
    # One short transaction for the relation inserts.
    member_ids = [a["id"] for a in atoms]
    member_count = 0
    if member_ids:
        try:
            conn.execute("BEGIN IMMEDIATE")
            member_count = _link_session_members(conn, boundary_id, member_ids)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # Observation synthesis is handled separately by consolidate.py
    # (cron-driven, cross-session). Within-session synthesis was
    # removed 2026-05-13 — no production caller used it and the
    # cluster + synth logic duplicated consolidate's path.

    # ─── Update sessions table ────────────────────────────────────
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            INSERT INTO sessions (id, channel_id, started_at, ended_at, summary, reflected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                ended_at = excluded.ended_at,
                summary = excluded.summary,
                reflected_at = excluded.reflected_at
        """, (
            session_id, channel_id,
            atoms[0]["created_at"] if atoms else _utc_now_iso(),
            _utc_now_iso(),
            fields.get("summary", ""),
            _utc_now_iso(),
        ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return ReflectResult(
        session_id=session_id,
        boundary_atom_id=boundary_id,
        boundary_created=True,
        session_member_count=member_count,
    )


def recent_session_boundaries(
    conn: sqlite3.Connection,
    *,
    channel_id: str | None = None,
    count: int = 3,
    agent_id: str = "default",
) -> list[dict]:
    """Return the most recent session_boundary atoms (by created_at),
    optionally scoped to a channel. Used by the prompt-build path to
    render cross-session continuity.

    Bypasses activation-based recall entirely — boundaries are ordered
    by recency, not relevance. Their job is "what happened recently in
    this channel," not "what's semantically related to a query."
    """
    if channel_id is not None:
        rows = conn.execute("""
            SELECT a.id, a.content, a.created_at, a.metadata, a.session_id, s.channel_id
            FROM atoms a
            LEFT JOIN sessions s ON s.id = a.session_id
            WHERE a.source_type = 'session_boundary'
              AND a.tombstoned = 0
              AND a.agent_id = ?
              AND s.channel_id = ?
            ORDER BY a.created_at DESC LIMIT ?
        """, (agent_id, channel_id, count)).fetchall()
    else:
        rows = conn.execute("""
            SELECT a.id, a.content, a.created_at, a.metadata, a.session_id, s.channel_id
            FROM atoms a
            LEFT JOIN sessions s ON s.id = a.session_id
            WHERE a.source_type = 'session_boundary'
              AND a.tombstoned = 0
              AND a.agent_id = ?
            ORDER BY a.created_at DESC LIMIT ?
        """, (agent_id, count)).fetchall()
    cols = ("id", "content", "created_at", "metadata",
            "session_id", "channel_id")
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        # Decode metadata JSON for the caller.
        if d.get("metadata"):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (TypeError, ValueError):
                pass
        out.append(d)
    return out
