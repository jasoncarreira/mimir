"""reflect — session-end bookkeeping.

Triggered at session_boundary turn (mimir's synthesis-turn mechanism).
Writes exactly one sessions row per session (idempotent) and populates
the full structured boundary fields (summary, topics, decisions, etc.).

Session boundaries live in the ``sessions`` table — NOT in ``atoms``.
The old path that stored a ``source_type='session_boundary'`` atom plus
``session_member`` relations has been removed; the sessions table is the
canonical store for cross-session continuity.

Observation synthesis is NOT done here — it lives in
``mimir.saga.consolidate.consolidate()``, which runs on a cron over
cross-session evidence.

Idempotency: reflect(session_id) called twice detects the existing
sessions row and skips re-synthesis. The agent can call saga_end_session
multiple times (e.g. via retries) without re-doing the work.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


# Injected callables — same pattern as store.EmbedFn.

# Boundary synthesis: agent + atoms → boundary content fields.
# Saga's current saga_end_session tool's contract (mimir's synthesis
# turn renders these). Returns a dict with summary, topics_discussed,
# decisions_made, unfinished, emotional_state.
BoundarySynthFn = Callable[[list[dict], dict | None], dict]


@dataclass
class ReflectResult:
    session_id: str
    boundary_atom_id: str | None = None  # deprecated; equals session_id post-migration
    boundary_created: bool = False       # False = pre-existing (idempotent re-run)
    session_member_count: int = 0        # deprecated; always 0 (no atom relations written)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_atoms(
    conn: sqlite3.Connection,
    session_id: str,
    agent_id: str = "default",
) -> list[dict]:
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
          AND a.agent_id = ?
          AND (
            a.session_id = ?
            OR a.id IN (
                SELECT atom_id FROM access_events WHERE session_id = ?
            )
          )
    """, (agent_id, session_id, session_id)).fetchall()
    cols = ("id", "content", "stream", "memory_type",
            "source_type", "created_at", "topics", "metadata")
    return [dict(zip(cols, r)) for r in rows]


def _existing_boundary(
    conn: sqlite3.Connection,
    session_id: str,
    agent_id: str = "default",  # kept for signature compat; sessions table is agent-agnostic
) -> str | None:
    """Return session_id if a sessions row already exists for this session.

    Powers idempotency on reflect re-calls — if a row exists, reflect
    short-circuits rather than re-synthesizing.
    """
    row = conn.execute(
        "SELECT id FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    return row[0] if row else None


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
    """Session-end bookkeeping.

    Writes a sessions row capturing the full structured boundary (summary,
    topics_discussed, decisions_made, unfinished, emotional_state,
    closed_since). Does NOT write a session_boundary atom — boundaries
    live in the sessions table, not in atoms.

    Does NOT synthesize observations — that's consolidate.py's job
    (cron-driven, cross-session).

    ``boundary_context`` is optional state from mimir's lifecycle layer
    (e.g., recent boundaries to chain from, the agent's running
    emotional_state). Passed through to ``boundary_synth_fn`` so the
    synthesis can stitch sessions together coherently.
    """
    # Idempotency: short-circuit if a sessions row already exists.
    # Still upsert with channel_id if it was missing — a caller re-ending
    # a session with a freshly-resolved channel_id should be able to
    # backfill that without triggering a full re-synthesis.
    existing_id = _existing_boundary(conn, session_id, agent_id=agent_id)
    if existing_id is not None:
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
            boundary_atom_id=existing_id,
            boundary_created=False,
        )

    atoms = _session_atoms(conn, session_id, agent_id=agent_id)

    # ─── Synthesize boundary fields ───────────────────────────────
    fields = boundary_synth_fn(atoms, boundary_context)
    summary = fields.get("summary") or ""
    topics = fields.get("topics_discussed") or []
    decisions = fields.get("decisions_made") or []
    unfinished = fields.get("unfinished") or []
    emotional_state = fields.get("emotional_state")
    closed_since = fields.get("closed_since") or []

    now = _utc_now_iso()

    # ─── Write sessions row ───────────────────────────────────────
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            INSERT INTO sessions
                (id, channel_id, started_at, ended_at, summary, reflected_at,
                 topics_discussed, decisions_made, unfinished,
                 emotional_state, closed_since)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                ended_at         = excluded.ended_at,
                summary          = excluded.summary,
                reflected_at     = excluded.reflected_at,
                topics_discussed = excluded.topics_discussed,
                decisions_made   = excluded.decisions_made,
                unfinished       = excluded.unfinished,
                emotional_state  = excluded.emotional_state,
                closed_since     = excluded.closed_since
        """, (
            session_id,
            channel_id,
            atoms[0]["created_at"] if atoms else now,
            now,
            summary,
            now,
            json.dumps(topics),
            json.dumps(decisions),
            json.dumps(unfinished),
            emotional_state,
            json.dumps(closed_since),
        ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # boundary_atom_id set to session_id for back-compat (callers that
    # inspect the field still get a useful identifier).
    return ReflectResult(
        session_id=session_id,
        boundary_atom_id=session_id,
        boundary_created=True,
        session_member_count=0,
    )


def recent_session_boundaries(
    conn: sqlite3.Connection,
    *,
    channel_id: str | None = None,
    count: int = 3,
    agent_id: str = "default",  # kept for signature compat; sessions table is agent-agnostic
) -> list[dict]:
    """Return the most recent session boundaries (by ended_at / reflected_at),
    optionally scoped to a channel.

    Queries the sessions table directly — no atoms join needed. Used by
    the prompt-build path to render cross-session continuity; ordered by
    recency, not relevance.

    Return shape preserves the legacy keys callers used when this
    queried atoms (``id``, ``content``, ``created_at``, ``metadata``,
    ``session_id``, ``channel_id``) plus the structured fields.
    """
    if channel_id is not None:
        rows = conn.execute("""
            SELECT id, channel_id, ended_at, summary,
                   topics_discussed, decisions_made, unfinished,
                   emotional_state, closed_since
            FROM sessions
            WHERE channel_id = ?
            ORDER BY COALESCE(ended_at, reflected_at) DESC LIMIT ?
        """, (channel_id, count)).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, channel_id, ended_at, summary,
                   topics_discussed, decisions_made, unfinished,
                   emotional_state, closed_since
            FROM sessions
            ORDER BY COALESCE(ended_at, reflected_at) DESC LIMIT ?
        """, (count,)).fetchall()

    def _parse(v):
        try:
            return json.loads(v or '[]')
        except (TypeError, ValueError):
            return []

    out = []
    for r in rows:
        sid, ch, ended_at, summary, topics_j, decisions_j, unfinished_j, es, closed_j = r
        topics = _parse(topics_j)

        # Legacy keys expected by agent.py + render_session_summaries:
        d = {
            # New structured keys:
            "ts": ended_at,
            "channel_id": ch,
            "summary": summary or "",
            "topics_discussed": topics,
            "decisions_made": _parse(decisions_j),
            "unfinished": _parse(unfinished_j),
            "emotional_state": es,
            "closed_since": _parse(closed_j),
            # Legacy keys (atom-era names kept for back-compat):
            "id": sid,
            "content": f"Session Boundary [{sid}]: {summary or ''}",
            "created_at": ended_at,
            "metadata": {
                "summary": summary,
                "topics_discussed": topics,
                "decisions_made": _parse(decisions_j),
                "unfinished": _parse(unfinished_j),
                "emotional_state": es,
                "closed_since": _parse(closed_j),
            },
            "session_id": sid,
        }
        out.append(d)
    return out
