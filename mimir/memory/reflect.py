"""reflect — session-end consolidation.

Triggered at session_boundary turn (mimir's existing synthesis-turn
mechanism). Emits exactly one session_boundary atom per session
(always), plus optionally zero-or-more observation atoms when
clusters of session events warrant synthesis.

This is the ONLY consolidation entry point in mimir.memory — the
daily-cron pattern saga has today goes away. CLS framing: reflection
is the offline "sleep" pass that extracts schemas from episodes;
firing at session-end matches biology and bounds the scope (we only
consider what was actually used).

Idempotency: reflect(session_id) called twice returns the existing
session_boundary atom and skips re-synthesis. The agent can call
saga_end_session multiple times during one session_end turn (e.g. via
retries) without re-doing the work.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .mark_access import AccessEvent, mark_access
from .observations import (
    find_equal_evidence_obs, find_superseded_observations,
    refresh_trend,
)
from .store import store


# Activity filter — number of distinct atoms touched in the session
# before we'll consider synthesizing observations. Quiet sessions
# (e.g. heartbeats with no useful content) get a session_boundary but
# no observation synthesis.
MIN_SESSION_EVENTS_FOR_OBSERVATIONS = 5

# Cap on observations emitted per reflect call. Bounds the LLM cost
# per session and prevents one verbose session from spawning a swarm
# of half-baked beliefs.
MAX_OBSERVATIONS_PER_SESSION = 3

# Minimum atoms per cluster to synthesize an observation. Below this,
# the cluster is "just one or two related raws" and doesn't justify
# a new observation; let saga's two-tier evidence boost surface the
# raws directly when relevant.
MIN_CLUSTER_SIZE_FOR_OBSERVATION = 3


# Injected callables — same pattern as store.EmbedFn.

# Boundary synthesis: agent + atoms → boundary content fields.
# Saga's current saga_end_session tool's contract (mimir's synthesis
# turn renders these). Returns a dict with summary, topics_discussed,
# decisions_made, unfinished, emotional_state.
BoundarySynthFn = Callable[[list[dict], dict | None], dict]

# Observation synthesis: cluster of raw atoms → observation content.
# Returns (content, topics) tuple.
ObservationSynthFn = Callable[[list[dict]], tuple[str, list[str]]]

# Cluster fn: list of session atoms → list of clusters (lists of atoms).
# v1 sketch uses similarity-based clustering; entity-aware clustering
# is a Tier 3 stretch (Hindsight's framing).
ClusterFn = Callable[[list[dict]], list[list[dict]]]


@dataclass
class ReflectResult:
    session_id: str
    boundary_atom_id: str | None = None
    boundary_created: bool = False         # False = pre-existing (idempotent re-run)
    observation_ids: list[str] = field(default_factory=list)
    observations_superseded: list[tuple[str, str]] = field(
        default_factory=list,
    )  # (new_obs_id, superseded_old_id)
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


def _synthesize_observations(
    conn: sqlite3.Connection,
    session_atoms: list[dict],
    session_id: str,
    *,
    embed_fn,
    cluster_fn: ClusterFn | None,
    observation_synth_fn: ObservationSynthFn,
    agent_id: str,
) -> list[tuple[str, list[str], list[str]]]:
    """Cluster the session's raws → call observation_synth_fn per
    cluster → return list of (observation_id, evidence_atom_ids,
    superseded_old_ids).

    Transaction shape: each emitted observation is its own transaction.
    The LLM call (observation_synth_fn) happens OUTSIDE any transaction
    — holding a SQLite write lock across an LLM call (seconds) would
    block any concurrent writer. After the LLM returns, one txn wraps
    the observation + relations + evidence access_events + supersedes
    edges + metadata + trend recompute.
    """
    raws = [a for a in session_atoms if a["memory_type"] == "raw"
            and a["source_type"] != "session_boundary"]
    if len(raws) < MIN_SESSION_EVENTS_FOR_OBSERVATIONS:
        return []
    if cluster_fn is None:
        return []

    clusters = cluster_fn(raws)
    emitted: list[tuple[str, list[str], list[str]]] = []
    for cluster in clusters:
        if len(emitted) >= MAX_OBSERVATIONS_PER_SESSION:
            break
        if len(cluster) < MIN_CLUSTER_SIZE_FOR_OBSERVATION:
            continue

        # LLM synth call — OUTSIDE the transaction.
        content, topics = observation_synth_fn(cluster)
        if not content or not content.strip():
            continue
        evidence_ids = [a["id"] for a in cluster]

        # Pre-check: equal-evidence observation already exists?
        # Read-only; no transaction needed.
        existing_equal = find_equal_evidence_obs(conn, set(evidence_ids))
        if existing_equal:
            # Don't fire an access_event: consolidation is
            # system-internal, not external access. The
            # ``consolidated_into`` / ``evidenced_by`` relations
            # remain the persistent audit trail; access_events is
            # reserved for external-access record only.
            continue

        # store() opens its own transaction internally for the atom +
        # embedding + topics + initial access_event. After it returns,
        # the observation exists committed.
        result = store(
            conn, content,
            embed_fn=embed_fn,
            memory_type="observation",
            stream="semantic",
            topics=topics,
            agent_id=agent_id,
            session_id=session_id,
        )
        if not result.stored:
            # Content-hash dedupe hit on the observation. Relations
            # were already in place from the prior cluster pass; no
            # access_event fired (consolidation stays out of activation).
            continue

        # Now wrap the rest in ONE transaction: relations + consolidation
        # access_events on evidence + supersedes + observations_metadata
        # seed.
        now = _utc_now_iso()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO atom_relations "
                "(source_id, target_id, relation_type, confidence, created_at) "
                "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
                [(result.atom_id, raw_id, now) for raw_id in evidence_ids],
            )
            conn.executemany(
                "INSERT INTO atom_relations "
                "(source_id, target_id, relation_type, confidence, created_at) "
                "VALUES (?, ?, 'consolidated_into', 1.0, ?)",
                [(raw_id, result.atom_id, now) for raw_id in evidence_ids],
            )
            # No mark_access on evidence raws: consolidation is
            # system-internal. The evidence_boost on retrieval is the
            # only ranking signal consolidation produces; activation
            # stays a pure external-access record.

            superseded = find_superseded_observations(
                conn, result.atom_id, set(evidence_ids),
            )
            for old_obs_id in superseded:
                conn.execute(
                    "INSERT OR IGNORE INTO atom_relations "
                    "(source_id, target_id, relation_type, confidence, "
                    "created_at, metadata) "
                    "VALUES (?, ?, 'supersedes', 1.0, ?, ?)",
                    (result.atom_id, old_obs_id, now,
                     json.dumps({"trigger": "reflect"})),
                )

            conn.execute(
                "INSERT INTO observations_metadata "
                "(atom_id, evidence_count, trend, last_evidence_at, "
                "consolidated_at, consolidation_session) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (result.atom_id, len(evidence_ids), "strengthening",
                 now, now, session_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Trend recompute is its own short txn inside refresh_trend.
        refresh_trend(conn, result.atom_id)

        emitted.append((result.atom_id, evidence_ids, superseded))

    return emitted


def reflect(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    channel_id: str | None,
    embed_fn,
    boundary_synth_fn: BoundarySynthFn,
    observation_synth_fn: ObservationSynthFn | None = None,
    cluster_fn: ClusterFn | None = None,
    boundary_context: dict | None = None,
    agent_id: str = "default",
) -> ReflectResult:
    """Session-end reflection. See module docstring.

    ``boundary_context`` is optional state from mimir's lifecycle layer
    (e.g., recent boundaries to chain from, the agent's running
    emotional_state). Passed through to ``boundary_synth_fn`` so the
    synthesis can stitch sessions together coherently.
    """
    # Idempotency: short-circuit if a boundary already exists.
    existing_bid = _existing_boundary(conn, session_id)
    if existing_bid is not None:
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

    # ─── Maybe: synthesize observations from session clusters ─────
    observations: list[tuple[str, list[str], list[str]]] = []
    if observation_synth_fn is not None:
        observations = _synthesize_observations(
            conn, atoms, session_id,
            embed_fn=embed_fn,
            cluster_fn=cluster_fn,
            observation_synth_fn=observation_synth_fn,
            agent_id=agent_id,
        )

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
        observation_ids=[o[0] for o in observations],
        observations_superseded=[
            (o[0], old_id)
            for o in observations for old_id in o[2]
        ],
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
