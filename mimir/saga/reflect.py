"""reflect — session-end bookkeeping.

Triggered at session_boundary turn (mimir's synthesis-turn mechanism).
Writes exactly one ``sessions`` row per session with the full structured
boundary (summary, topics, decisions, unfinished, emotional_state,
closed_since) plus an embedding of the summary so ``search_sessions()``
can do semantic retrieval over past sessions.

Sessions live in the dedicated ``sessions`` table — NOT in ``atoms``.
The old path that stored ``source_type='session_boundary'`` atoms with
``session_member`` relations was removed: it added an 80%-bulk atom
class that retrieval / FTS / consolidate / dedup all filtered out and
that wasted FAISS slots. The ``sessions`` table holds the same
information without the indirection.

Observation synthesis is NOT done here — it lives in
``mimir.saga.consolidate.consolidate()``, which runs on a cron over
cross-session evidence.

Idempotency: ``reflect(session_id)`` called twice detects the existing
sessions row and short-circuits. The lifecycle layer can re-call after
a freshly-resolved channel_id and we backfill that column without
re-synthesizing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


# Injected callables — same pattern as store.EmbedFn.

# Boundary synthesis: agent + atoms → boundary content fields.
# Mimir's synthesis turn renders these. Returns a dict with summary,
# topics_discussed, decisions_made, unfinished, emotional_state,
# closed_since.
BoundarySynthFn = Callable[[list[dict], dict | None], dict]


@dataclass
class ReflectResult:
    session_id: str
    #: ``True`` if a new sessions row was written this call; ``False`` if
    #: a row already existed (idempotent re-run — channel_id backfill may
    #: still have applied but no synthesis fired).
    session_summary_written: bool = False


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


def _parse_json_list(v: str | None) -> list:
    """Parse a JSON-encoded list column from the sessions table. Returns
    [] for NULL / empty / malformed input — sessions rows may have
    missing structured fields when the boundary synth produced an empty
    extraction."""
    if not v:
        return []
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return []


def _existing_session_summary(
    conn: sqlite3.Connection,
    session_id: str,
) -> bool:
    """True if a sessions row with this id has already been written
    by reflect (reflected_at IS NOT NULL means the row has structured
    fields beyond the lifecycle-only stub)."""
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE id = ? AND reflected_at IS NOT NULL",
        (session_id,),
    ).fetchone()
    return row is not None


def reflect(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    channel_id: str | None,
    embed_fn,
    boundary_synth_fn: BoundarySynthFn,
    boundary_context: dict | None = None,
    agent_id: str = "default",
    owner_principal: str | None = None,
    origin_channel: str | None = None,
    origin_domain: str | None = None,
    visibility: str | None = None,
    provenance: dict | None = None,
) -> ReflectResult:
    """Session-end bookkeeping.

    Writes a sessions row capturing the full structured boundary
    (summary, topics_discussed, decisions_made, unfinished,
    emotional_state, closed_since) plus an embedding of the summary
    for ``search_sessions()``. Does NOT write atoms.

    Does NOT synthesize observations — that's consolidate.py's job
    (cron-driven, cross-session).

    ``boundary_context`` is optional state from mimir's lifecycle layer
    (recent sessions to chain from, the agent's running emotional_state).
    Passed through to ``boundary_synth_fn`` so the synthesis can stitch
    sessions together coherently.
    """
    # Idempotency: short-circuit if reflect has already written this
    # session. Still backfill channel_id when a caller re-ends with a
    # freshly-resolved channel — the dispatcher sometimes learns the
    # channel after the first reflect call.
    if _existing_session_summary(conn, session_id):
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
                # Non-fatal — the session row still resolves; channel_id
                # backfill is best-effort.
        return ReflectResult(
            session_id=session_id,
            session_summary_written=False,
        )

    atoms = _session_atoms(conn, session_id, agent_id=agent_id)

    # ─── Synthesize boundary fields ───────────────────────────────
    fields = boundary_synth_fn(atoms, boundary_context)
    summary = (fields.get("summary") or "").strip()
    topics = fields.get("topics_discussed") or []
    decisions = fields.get("decisions_made") or []
    unfinished = fields.get("unfinished") or []
    emotional_state = fields.get("emotional_state")
    closed_since = fields.get("closed_since") or []

    # ─── Embed the summary ────────────────────────────────────────
    # Used by search_sessions() for semantic retrieval. Best-effort —
    # a failed embed doesn't abort the session close; the row lands
    # with NULL embedding and search_sessions() falls back to recency.
    emb_bytes: bytes | None = None
    emb_dim: int | None = None
    if embed_fn is not None and summary:
        try:
            emb_result = embed_fn(summary)
            if emb_result:
                emb_bytes, _, _, emb_dim = emb_result
        except Exception:
            pass

    now = _utc_now_iso()

    # ─── Write sessions row ───────────────────────────────────────
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            INSERT INTO sessions
                (id, channel_id, started_at, ended_at, summary, reflected_at,
                 topics_discussed, decisions_made, unfinished,
                 emotional_state, closed_since, embedding, embedding_dim,
                 owner_principal, origin_channel, origin_domain, visibility, provenance)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                ended_at         = excluded.ended_at,
                -- Structured extraction fields: COALESCE(NULLIF(..., '[]'), existing)
                -- so a re-run where LLM extraction failed ('[]') doesn't nuke the
                -- first run's data. NULLIF treats '[]' as absent (same as NULL) so
                -- COALESCE falls back to the existing row value.
                summary          = COALESCE(NULLIF(excluded.summary, ''), sessions.summary),
                reflected_at     = excluded.reflected_at,
                topics_discussed = COALESCE(NULLIF(excluded.topics_discussed, '[]'), sessions.topics_discussed),
                decisions_made   = COALESCE(NULLIF(excluded.decisions_made, '[]'), sessions.decisions_made),
                unfinished       = COALESCE(NULLIF(excluded.unfinished, '[]'), sessions.unfinished),
                emotional_state  = COALESCE(excluded.emotional_state, sessions.emotional_state),
                closed_since     = COALESCE(NULLIF(excluded.closed_since, '[]'), sessions.closed_since),
                embedding        = COALESCE(excluded.embedding, sessions.embedding),
                embedding_dim    = COALESCE(excluded.embedding_dim, sessions.embedding_dim),
                owner_principal = COALESCE(excluded.owner_principal, sessions.owner_principal),
                origin_channel  = COALESCE(excluded.origin_channel, sessions.origin_channel),
                origin_domain   = COALESCE(excluded.origin_domain, sessions.origin_domain),
                visibility      = COALESCE(excluded.visibility, sessions.visibility),
                provenance      = COALESCE(excluded.provenance, sessions.provenance)
        """, (
            session_id, channel_id,
            atoms[0]["created_at"] if atoms else now,
            now,
            summary,
            now,
            json.dumps(topics),
            json.dumps(decisions),
            json.dumps(unfinished),
            emotional_state,
            json.dumps(closed_since),
            emb_bytes,
            emb_dim,
            owner_principal or "legacy_admin",
            origin_channel,
            origin_domain,
            visibility or "legacy_admin",
            json.dumps(provenance or {}),
        ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return ReflectResult(
        session_id=session_id,
        session_summary_written=True,
    )


def recent_session_boundaries(
    conn: sqlite3.Connection,
    *,
    channel_id: str | None = None,
    count: int = 3,
    agent_id: str = "default",  # noqa: ARG001 — see note below
) -> list[dict]:
    """Return the most recent session summaries (by ``reflected_at``),
    optionally scoped to a channel. Used by the prompt-build path to
    render cross-session continuity.

    Reads from the ``sessions`` table directly — no atoms involved.
    Bypasses activation-based recall: sessions are ordered by recency,
    not relevance. Their job is "what happened recently in this channel,"
    not "what's semantically related to a query." Use
    ``SagaStore.search_sessions()`` for the semantic path.

    Return dict shape (matches the pre-migration prompt-build contract —
    ``mimir/agent.py`` and ``session_boundary_log.render_session_summaries``
    read fields from the top level, not from ``metadata``):
        id, atom_id, session_id  — all equal to session_id (atom_id is
                                   the historical key the local mirror
                                   used; preserved for compatibility)
        ts                       — sessions.ended_at (falls back to reflected_at);
                                   the prompt builder keys turn_counts on this
        created_at               — alias of ts
        content                  — rendered summary block (summary + topics + ...)
        summary                  — sessions.summary
        topics_discussed         — list (parsed from JSON)
        decisions_made           — list
        unfinished               — list — render_session_summaries reads this
                                   for the Unfinished section
        closed_since             — list — render_session_summaries reads this
                                   for cross-boundary unfinished suppression
        emotional_state          — string | None
        channel_id, channel      — both populated from sessions row
        metadata                 — dict echoing the structured fields, for
                                   callers that prefer the nested shape

    ``agent_id`` is accepted for caller-signature compatibility
    (saga_client surface, ``mimir/agent.py``, tests) but intentionally
    NOT used in the query — the sessions table is agent-agnostic. A
    multi-agent deployment sharing one sessions table would see all
    agents' sessions here. Non-issue under the single-agent-per-DB
    posture; revisit if a multi-agent DB shape lands.
    """
    if channel_id is not None:
        rows = conn.execute("""
            SELECT id, channel_id, ended_at, reflected_at, summary,
                   topics_discussed, decisions_made, unfinished,
                   emotional_state, closed_since
            FROM sessions
            WHERE channel_id = ?
              AND reflected_at IS NOT NULL
            ORDER BY reflected_at DESC LIMIT ?
        """, (channel_id, count)).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, channel_id, ended_at, reflected_at, summary,
                   topics_discussed, decisions_made, unfinished,
                   emotional_state, closed_since
            FROM sessions
            WHERE reflected_at IS NOT NULL
            ORDER BY reflected_at DESC LIMIT ?
        """, (count,)).fetchall()

    out: list[dict] = []
    for r in rows:
        (sid, ch, ended_at, reflected_at, summary,
         topics_json, decisions_json, unfinished_json,
         emotional_state, closed_since_json) = r

        topics = _parse_json_list(topics_json)
        decisions = _parse_json_list(decisions_json)
        unfinished = _parse_json_list(unfinished_json)
        closed_since = _parse_json_list(closed_since_json)

        # Render the same content shape reflect() used to produce so
        # prompt-build callers that read ``content`` keep working.
        parts = [summary.strip() if summary else ""]
        if topics:
            parts.append("Topics: " + "; ".join(topics))
        if decisions:
            parts.append("Decisions: " + "; ".join(decisions))
        if unfinished:
            parts.append("Unfinished: " + "; ".join(unfinished))
        if emotional_state:
            parts.append(f"Emotional state: {emotional_state}")
        content = "\n\n".join(p for p in parts if p)
        if not content.strip():
            content = "[session ended; no significant activity]"

        ts = ended_at or reflected_at
        out.append({
            # Identity — multiple aliases match the pre-migration shape.
            # ``atom_id`` was the SAGA atom id of the boundary atom; that
            # atom no longer exists, but local-mirror code keys on it and
            # the session_id serves the same identity role.
            "id": sid,
            "atom_id": sid,
            "session_id": sid,
            # Timestamp keys: render_session_summaries reads ``ts``;
            # agent._assemble_session_summaries reads it for turn_counts.
            # ``created_at`` is the post-migration alias.
            "ts": ts,
            "created_at": ts,
            # Channel: both spellings populated.
            "channel_id": ch,
            "channel": ch,
            # Rendered + raw content. ``summary`` MUST be at the top level
            # — render_session_summaries reads it there, not from metadata.
            "content": content,
            "summary": summary or "",
            # Structured fields at the top level (top-level reads in
            # render_session_summaries + cross-boundary closed_since
            # suppression). Mirrored in ``metadata`` for nested callers.
            "topics_discussed": topics,
            "decisions_made": decisions,
            "unfinished": unfinished,
            "emotional_state": emotional_state,
            "closed_since": closed_since,
            "metadata": {
                "summary": summary or "",
                "topics_discussed": topics,
                "decisions_made": decisions,
                "unfinished": unfinished,
                "emotional_state": emotional_state,
                "closed_since": closed_since,
            },
        })
    return out
