"""Store a new atom.

One atom per call. Embedding is owned by mimir's existing provider
infrastructure (currently voyage via saga's provider stack); this module
calls into it via an injected callable, leaving the provider choice to
mimir/config.

Contract:

- Returns the atom_id (newly-generated UUID hex) on success.
- Dedupes by ``content_hash + agent_id`` per the UNIQUE index in SCHEMA.sql.
  If a duplicate is detected (same content from the same agent, not
  tombstoned), returns the existing atom_id and fires an additional
  ``store`` access_event on it (treating it as a re-encounter).
- Atomic: atom row + embedding row + initial access_event all commit
  together. If embedding fails (provider down), the atom isn't stored.
- Embedding is computed at store time, not lazily. Lazy embedding makes
  retrieval slow on first hit; eager makes recovery painful. Eager is
  the right tradeoff for an agent's working memory.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .mark_access import AccessEvent, mark_access


# Callable signature for the embedding provider. Returns the raw float32
# bytes ready to land in embeddings.vec, plus the metadata fields
# (provider, model, dim). Injected by mimir.memory's __init__ wiring;
# the sketch uses a lambda placeholder.
EmbedFn = Callable[[str], tuple[bytes, str, str, int]]


@dataclass(frozen=True)
class StoreResult:
    """Return shape of store(). When ``stored`` is False, the atom
    already existed (dedupe hit) and ``atom_id`` is the existing id."""
    atom_id: str
    stored: bool                     # True = newly created; False = dedupe hit
    reason: str | None = None        # 'duplicate' when stored=False, else None


def _make_atom_id() -> str:
    """16-char hex prefix of a UUID4. Same shape saga uses; keeps atom
    IDs short enough to print/reference inline in prompts. Birthday
    collision at 50% probability arrives around 2^32 atoms — fine for
    any single agent."""
    return uuid.uuid4().hex[:16]


def _hash_content(content: str) -> str:
    """SHA-256 prefix used as content_hash for the UNIQUE dedupe index."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def store(
    conn: sqlite3.Connection,
    content: str,
    *,
    embed_fn: EmbedFn,
    stream: str = "semantic",
    profile: str = "standard",
    memory_type: str = "raw",
    source_type: str = "conversation",
    topics: list[str] | None = None,
    metadata: dict | None = None,
    agent_id: str = "default",
    session_id: str | None = None,
    is_pinned: bool = False,
    arousal: float = 0.5,
    valence: float = 0.0,
    encoding_confidence: float = 0.7,
    precomputed_embedding: tuple[bytes, str, str, int] | None = None,
    session_dedup_threshold: float | None = None,
) -> StoreResult:
    """Persist one atom + its embedding + the initial access event.

    Raises ``ValueError`` for empty content. Embedding-provider errors
    propagate (caller can retry; nothing landed). DB errors propagate
    too.

    ``precomputed_embedding`` lets bulk callers (bench ingest, importer)
    batch-embed externally and skip the per-atom ``embed_fn`` call.
    Pass ``(vec_bytes, provider, model, dim)`` in the same shape
    ``embed_fn`` would return. The single-atom path keeps the embed_fn
    contract; only batch paths flip this.

    ``session_dedup_threshold`` enables near-duplicate dedup within the
    current session (saga's pre-storage session_dedup). Before content-
    hash dedupe, if both ``session_id`` and this threshold are set, we
    cosine-compare the new embedding against every existing atom in
    the same session. Best match above threshold → dedupe to that atom
    + fire a store re-encounter event. Use 0.92-0.97 for paraphrase
    catching; default None (off) preserves current behavior so the
    bench is unaffected.
    """
    if not content or not content.strip():
        raise ValueError("store: content cannot be empty")
    content = content.strip()
    content_hash = _hash_content(content)
    created_at = _utc_now_iso()

    # Dedupe check FIRST. The UNIQUE index would catch it on insert,
    # but explicit check lets us emit a clean access_event on the
    # existing atom and report stored=False without an exception path.
    #
    # Exception: session_boundary atoms are always distinct events
    # even when the synthesized content collides. Two quiet sessions
    # both producing "[session ended; no significant activity]"
    # deserve separate boundary atoms — they mark different moments
    # in conversation history. Bypass dedupe for the boundary path.
    # The UNIQUE index on (content_hash, agent_id) WHERE tombstoned=0
    # would still trip if the contents truly collide; we mitigate by
    # rendering a unique discriminator into the content at the
    # reflect.py layer (session_id + timestamp).
    skip_dedupe = source_type == "session_boundary"
    if not skip_dedupe:
        existing = conn.execute(
            "SELECT id FROM atoms WHERE content_hash = ? "
            "AND agent_id = ? AND tombstoned = 0",
            (content_hash, agent_id),
        ).fetchone()
        if existing is not None:
            atom_id = existing[0]
            # Fire a 'store' access_event on the existing atom in its
            # own transaction.
            try:
                conn.execute("BEGIN IMMEDIATE")
                mark_access(conn, [AccessEvent(
                    atom_id=atom_id,
                    source="store",
                    session_id=session_id,
                    metadata={"dedupe": True},
                )])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return StoreResult(atom_id=atom_id, stored=False, reason="duplicate")

    # Session near-duplicate dedup (saga's session_dedup, pre-storage).
    # Only fires when caller supplies BOTH a session_id and a threshold;
    # default behavior is unchanged.
    if (
        not skip_dedupe
        and session_id is not None
        and session_dedup_threshold is not None
    ):
        # Need an embedding to compare. If the caller already has one
        # (precomputed path), use it; otherwise compute now (we'd
        # compute it for the insert anyway).
        if precomputed_embedding is not None:
            cand_vec_bytes, cand_provider, cand_model, cand_dim = precomputed_embedding
        else:
            cand_vec_bytes, cand_provider, cand_model, cand_dim = embed_fn(content)
            # Stash so the insert path doesn't re-embed below.
            precomputed_embedding = (
                cand_vec_bytes, cand_provider, cand_model, cand_dim,
            )
        existing_id = _find_session_near_duplicate(
            conn, session_id, agent_id,
            cand_vec_bytes, cand_dim,
            threshold=session_dedup_threshold,
        )
        if existing_id is not None:
            try:
                conn.execute("BEGIN IMMEDIATE")
                mark_access(conn, [AccessEvent(
                    atom_id=existing_id,
                    source="store",
                    session_id=session_id,
                    metadata={"dedupe": "session_near"},
                )])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return StoreResult(
                atom_id=existing_id, stored=False,
                reason="session_near_duplicate",
            )

    atom_id = _make_atom_id()

    # Embed BEFORE entering the DB transaction. Embedding is network
    # I/O (50-300ms via voyage); we don't want to hold a SQLite write
    # lock over it — concurrent writers would block. Bulk callers can
    # bypass this with precomputed_embedding.
    if precomputed_embedding is not None:
        vec_bytes, provider, model, dim = precomputed_embedding
    else:
        vec_bytes, provider, model, dim = embed_fn(content)

    # One transaction wraps EVERYTHING: atom + embedding + topics +
    # initial access_event(s). Pre-restructure this opened BEGIN twice
    # (once here, once in mark_access) which collided. Now mark_access
    # is non-transactional; this is the only BEGIN/COMMIT pair.
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO atoms (id, content, content_hash, created_at, "
            "stream, profile, memory_type, arousal, valence, "
            "encoding_confidence, topics, source_type, metadata, "
            "agent_id, session_id, is_pinned) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                atom_id, content, content_hash, created_at,
                stream, profile, memory_type, arousal, valence,
                encoding_confidence, json.dumps(topics or []),
                source_type, json.dumps(metadata or {}),
                agent_id, session_id, 1 if is_pinned else 0,
            ),
        )
        conn.execute(
            "INSERT INTO embeddings (atom_id, provider, model, dim, vec, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (atom_id, provider, model, dim, vec_bytes, created_at),
        )
        if topics:
            conn.executemany(
                "INSERT OR IGNORE INTO atom_topics (atom_id, topic) VALUES (?, ?)",
                [(atom_id, t) for t in topics],
            )

        events = [AccessEvent(
            atom_id=atom_id, source="store", session_id=session_id,
        )]
        if is_pinned:
            events.append(AccessEvent(
                atom_id=atom_id, source="pinned_init", session_id=session_id,
            ))
        mark_access(conn, events)

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return StoreResult(atom_id=atom_id, stored=True)


def _find_session_near_duplicate(
    conn: sqlite3.Connection,
    session_id: str,
    agent_id: str,
    cand_vec_bytes: bytes,
    cand_dim: int,
    *,
    threshold: float,
) -> str | None:
    """Cosine-scan every atom in ``session_id`` for one whose embedding
    is **strictly greater than** ``threshold`` similar to ``cand_vec_bytes``.
    Returns the best matching atom_id, or None if no atom clears the
    threshold. (At sim == threshold the candidate is treated as
    not-duplicate — the threshold itself is the line that must be
    crossed, not touched.)

    Scoped per-session because the bench / production case for this is
    "the user is paraphrasing the same fact within a single conversation."
    Cross-session paraphrases get caught later by the consolidation
    similarity clustering, not here.

    Atoms with mismatched embedding dim are skipped (provider switch
    safety).
    """
    import math
    import struct as _struct

    cand_floats = _struct.unpack(f"{cand_dim}f", cand_vec_bytes[: cand_dim * 4])
    cand_norm = math.sqrt(sum(x * x for x in cand_floats))
    if cand_norm == 0.0:
        return None

    rows = conn.execute(
        "SELECT a.id, e.vec, e.dim FROM atoms a "
        "JOIN embeddings e ON e.atom_id = a.id "
        "WHERE a.session_id = ? AND a.agent_id = ? AND a.tombstoned = 0",
        (session_id, agent_id),
    ).fetchall()

    best_id: str | None = None
    best_sim = threshold  # only beat the threshold to win
    for atom_id, vec, dim in rows:
        if dim != cand_dim:
            continue
        if vec is None or len(vec) < dim * 4:
            continue
        try:
            atom_floats = _struct.unpack(f"{dim}f", vec[: dim * 4])
        except _struct.error:
            continue
        a_norm = math.sqrt(sum(x * x for x in atom_floats))
        if a_norm == 0.0:
            continue
        sim = sum(c * a for c, a in zip(cand_floats, atom_floats)) / (cand_norm * a_norm)
        if sim > best_sim:
            best_sim = sim
            best_id = atom_id
    return best_id
