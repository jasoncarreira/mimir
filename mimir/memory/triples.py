"""Triples extraction, storage, retrieval, and the temporal world model.

This module is the structural-knowledge complement to the
content-addressed atoms layer. Atoms hold prose; triples hold the
structured ``(subject, predicate, object[, valid_from, valid_until])``
distillation of that prose. Saga's P42 in the canonical bench (81.6%)
exposes triples as a retrieval-augment pathway — when a query mentions
an entity, the triple embedding may match where the atom prose doesn't
(or vice versa). We port the same surface here.

Pipeline:

  consolidation LLM call  →  TRIPLES section in response
       ↓
  _parse_triples(text)    →  list[dict] with subject/predicate/object
       ↓
  embed_triple(...)       →  cosine-comparable vector per triple
       ↓
  store_triple(...)       →  INSERT INTO triples + update world_state
       ↓
  triple_augment_search   →  P41-style: query → embed → cosine-match
                             against triples → follow source_atom_id

World model (P37): every triple write tries to maintain
``world_state(subject, predicate, value, valid_from, valid_until,
is_current)``. A new triple for an existing (subj, pred) end-dates the
previous current row and inserts a new one. Read via
``get_current_value(subject, predicate)``.

Bench wiring: the triple_augment pathway adds a new ranked list to
the RRF in ``recall.py``. Saga's bench config defaults to triples
extraction OFF + graph pathway OFF, but the canonical that hit 81.6%
ran with P42 triples ON per the operator note (2026-05-03).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


logger = logging.getLogger("mimir.memory.triples")


# ─── Constants ───────────────────────────────────────────────────────


# Length caps mirror saga's bench-tuned values. Long subjects / objects
# are usually free-text fragments that don't round-trip as named
# entities and pollute the (subject, predicate) graph.
MAX_SUBJECT_CHARS = 30
MAX_OBJECT_CHARS = 30
MIN_TERM_CHARS = 2


# ─── Triple identity ─────────────────────────────────────────────────


def make_triple_id(subject: str, predicate: str, obj: str) -> str:
    """Content-addressed: 16-hex prefix of sha256(lowered subj:pred:obj).

    Two atoms making the same claim land on the same triple row. The
    triple's ``source_atom_id`` tracks the FIRST atom that emitted it;
    a separate relation could record the rest if we ever need fan-out.
    """
    norm = f"{subject.lower().strip()}:{predicate.lower().strip()}:{obj.lower().strip()}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _triple_text(subject: str, predicate: str, obj: str) -> str:
    """Build the embeddable text from a triple. Replace underscores in
    the predicate so the embedding model sees natural phrasing."""
    pred_readable = predicate.replace("_", " ")
    return f"{subject} {pred_readable} {obj}"


# ─── Parsing the LLM TRIPLES section ─────────────────────────────────


# (subject, predicate, object[, valid_from=..., valid_until=...])
# Captures three required slots and an optional trailing kv tail.
# Permissive on whitespace around commas; strict on the parenthesis pair.
_TRIPLE_LINE = re.compile(
    r"\(([^,()]+),\s*([^,()]+),\s*([^,()]+?)(?:,([^()]+))?\)",
)

# Predicate normalization: lowercase, anything not [a-z0-9_] becomes "_".
_PREDICATE_NORM = re.compile(r"[^a-z0-9_]")


def parse_triples(text: str) -> list[dict]:
    """Parse the LLM-emitted TRIPLES block into structured dicts.

    Recognized shapes (one per line, each in its own parens):
        (Alice, prefers, concise_replies)
        (Alice, lives_in, Boston, valid_from=2024-01-15)
        (Alice, employed_at, Acme, valid_from=2023-01, valid_until=2024-06)

    Returns ``[]`` when the section is missing or contains only NONE.
    Invalid lines are dropped silently — saga's parser does the same;
    the LLM occasionally emits garbage and a strict parser would lose
    the good triples too.
    """
    if not text:
        return []
    # Pull out just the TRIPLES section if the prompt asks for both.
    # Tolerant of headers like "TRIPLES:" or "TRIPLES" or "**TRIPLES**".
    section = _slice_triples_section(text)
    if not section or "NONE" in section.upper().splitlines()[:2]:
        return []
    out: list[dict] = []
    for m in _TRIPLE_LINE.finditer(section):
        subj, pred, obj, tail = m.group(1), m.group(2), m.group(3), m.group(4)
        subj = subj.strip().strip("\"'")
        pred = pred.strip().strip("\"'")
        obj = obj.strip().strip("\"'")
        if not subj or not pred or not obj:
            continue
        if len(subj) < MIN_TERM_CHARS or len(obj) < MIN_TERM_CHARS or len(pred) < MIN_TERM_CHARS:
            continue
        if len(subj) > MAX_SUBJECT_CHARS or len(obj) > MAX_OBJECT_CHARS:
            continue
        pred = _PREDICATE_NORM.sub("_", pred.lower()).strip("_")
        if not pred:
            continue
        triple: dict = {"subject": subj, "predicate": pred, "object": obj}
        if tail:
            for kv in tail.split(","):
                kv = kv.strip()
                if "=" not in kv:
                    continue
                k, _, v = kv.partition("=")
                k = k.strip().lower()
                v = v.strip().strip("\"'")
                if k in ("valid_from", "valid_until") and v and v.lower() not in ("null", "none"):
                    triple[k] = v
        out.append(triple)
    return out


def _slice_triples_section(text: str) -> str:
    """Extract just the TRIPLES portion when the response has multiple
    labeled sections (OBSERVATION / TRIPLES / CONTRADICTIONS)."""
    # Find a line starting with TRIPLES (case-insensitive); take from
    # there until the next ALL-CAPS heading or end-of-string.
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("*# ").rstrip(":*# ")
        if stripped.upper() == "TRIPLES":
            start = i + 1
            break
    if start is None:
        # No header — assume the whole text might contain triples.
        return text
    end = len(lines)
    for j in range(start, len(lines)):
        stripped = lines[j].strip().lstrip("*# ").rstrip(":*# ")
        if stripped.upper() in ("OBSERVATION", "CONTRADICTIONS", "TRIPLES"):
            end = j
            break
    return "\n".join(lines[start:end])


# ─── Embedding ───────────────────────────────────────────────────────


# Signature parallel to store.EmbedFn: takes a text, returns
# (vec_bytes, provider, model, dim). The triple embedder reuses the
# atom-level embed_fn — same provider/dim guarantees cross-table
# cosine compatibility.
TripleEmbedFn = Callable[[str], tuple[bytes, str, str, int]]


# ─── Storage ─────────────────────────────────────────────────────────


def store_triples(
    conn: sqlite3.Connection,
    triples: list[dict],
    *,
    source_atom_id: str | None,
    embed_fn: TripleEmbedFn | None = None,
) -> list[str]:
    """Insert a batch of triples. Returns the triple IDs that were
    newly inserted (skips ones already present by content-hash).

    ``embed_fn`` is optional — pass None to skip embeddings (the
    retrieval pathway will exclude un-embedded rows). When provided,
    each triple's ``{subject} {predicate} {object}`` text is embedded
    and the bytes stored alongside.

    Caller is responsible for the surrounding transaction. We don't
    BEGIN/COMMIT internally so the triples write composes with the
    observation write in consolidate().
    """
    inserted: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    for t in triples:
        triple_id = make_triple_id(t["subject"], t["predicate"], t["object"])
        existing = conn.execute(
            "SELECT id FROM triples WHERE id = ?", (triple_id,),
        ).fetchone()
        if existing is not None:
            continue
        emb_bytes = None
        emb_dim = None
        if embed_fn is not None:
            try:
                emb_bytes, _provider, _model, emb_dim = embed_fn(
                    _triple_text(t["subject"], t["predicate"], t["object"]),
                )
            except Exception as exc:
                logger.warning("triple embed failed: %s", exc)
                emb_bytes = None
                emb_dim = None
        conn.execute(
            "INSERT INTO triples "
            "(id, subject, predicate, object, source_atom_id, confidence, "
            " valid_from, valid_until, embedding, embedding_dim, "
            " created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                triple_id, t["subject"], t["predicate"], t["object"],
                source_atom_id, t.get("confidence", 1.0),
                t.get("valid_from"), t.get("valid_until"),
                emb_bytes, emb_dim,
                now, json.dumps(t.get("metadata", {})),
            ),
        )
        inserted.append(triple_id)
        # Update the world-state model: end-date prior current rows
        # for the same (subj, pred), insert the new value as current.
        _update_world_state(
            conn,
            subject=t["subject"], predicate=t["predicate"],
            value=t["object"],
            valid_from=t.get("valid_from"),
            valid_until=t.get("valid_until"),
            source_triple_id=triple_id,
            now=now,
        )
    return inserted


def _update_world_state(
    conn: sqlite3.Connection,
    *,
    subject: str,
    predicate: str,
    value: str,
    valid_from: str | None,
    valid_until: str | None,
    source_triple_id: str,
    now: str,
) -> None:
    """Maintain ``world_state`` for the affected (subj, pred).

    Logic:
    - If the new triple has the same value as the current row: no-op
      (the LLM re-asserting the same fact, nothing to update).
    - Else: mark the prior current row as is_current=0, set its
      valid_until to the new row's valid_from (or now() if missing),
      then insert the new row as is_current=1.

    Idempotent on PK collision: if a row with the same (subj, pred,
    valid_from) already exists, INSERT OR IGNORE is a no-op. This
    matters because the same triple may appear from multiple atoms.
    """
    current = conn.execute(
        "SELECT value, valid_from FROM world_state "
        "WHERE subject = ? AND predicate = ? AND is_current = 1",
        (subject, predicate),
    ).fetchone()
    new_valid_from = valid_from or now
    if current is not None:
        cur_value, cur_valid_from = current
        if cur_value == value:
            return  # re-assertion; nothing changed
        # End-date the current row.
        end_ts = new_valid_from
        conn.execute(
            "UPDATE world_state SET is_current = 0, valid_until = ?, "
            "updated_at = ? "
            "WHERE subject = ? AND predicate = ? AND valid_from = ?",
            (end_ts, now, subject, predicate, cur_valid_from),
        )
    conn.execute(
        "INSERT OR IGNORE INTO world_state "
        "(subject, predicate, value, valid_from, valid_until, "
        " is_current, source_triple_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        (subject, predicate, value, new_valid_from, valid_until,
         source_triple_id, now),
    )


# ─── World-state queries ─────────────────────────────────────────────


@dataclass
class WorldFact:
    subject: str
    predicate: str
    value: str
    valid_from: str | None
    valid_until: str | None
    is_current: bool
    source_triple_id: str | None


def get_current_value(
    conn: sqlite3.Connection,
    subject: str,
    predicate: str,
) -> WorldFact | None:
    """Look up the current value of (subject, predicate). Returns None
    if no row exists. Subject + predicate match is case-sensitive on
    subject (proper noun) and case-folded on predicate (snake_case
    normalized at write time)."""
    pred = _PREDICATE_NORM.sub("_", predicate.lower()).strip("_")
    row = conn.execute(
        "SELECT subject, predicate, value, valid_from, valid_until, "
        "is_current, source_triple_id "
        "FROM world_state WHERE subject = ? AND predicate = ? "
        "AND is_current = 1",
        (subject, pred),
    ).fetchone()
    if row is None:
        return None
    return WorldFact(
        subject=row[0], predicate=row[1], value=row[2],
        valid_from=row[3], valid_until=row[4],
        is_current=bool(row[5]), source_triple_id=row[6],
    )


def get_history(
    conn: sqlite3.Connection,
    subject: str,
    predicate: str,
) -> list[WorldFact]:
    """All historical values for (subject, predicate), oldest first."""
    pred = _PREDICATE_NORM.sub("_", predicate.lower()).strip("_")
    rows = conn.execute(
        "SELECT subject, predicate, value, valid_from, valid_until, "
        "is_current, source_triple_id "
        "FROM world_state WHERE subject = ? AND predicate = ? "
        "ORDER BY valid_from ASC",
        (subject, pred),
    ).fetchall()
    return [
        WorldFact(
            subject=r[0], predicate=r[1], value=r[2],
            valid_from=r[3], valid_until=r[4],
            is_current=bool(r[5]), source_triple_id=r[6],
        )
        for r in rows
    ]


# ─── Retrieval: triple_augment_v2 pathway ────────────────────────────


def triple_augment_search(
    conn: sqlite3.Connection,
    query_emb: list[float],
    *,
    top_k: int = 10,
    dim: int | None = None,
) -> list[tuple[str, float]]:
    """P41-style triple-augmented retrieval.

    Embed the query, cosine-match against every live triple's
    embedding, return ``[(source_atom_id, cosine)]`` sorted by score.
    The caller plugs these into the RRF fusion as a third pathway
    alongside FAISS-semantic and FTS-keyword.

    Triples without embeddings are skipped (they can still be queried
    by entity name via ``retrieve_by_entity``). Triples whose
    embedding dim doesn't match ``dim`` are skipped — protects against
    provider switches that produced mixed-dim triples.
    """
    rows = conn.execute(
        "SELECT id, source_atom_id, embedding, embedding_dim "
        "FROM triples WHERE tombstoned = 0 AND embedding IS NOT NULL",
    ).fetchall()
    if not rows:
        return []
    # Unpack the query vector once into a numpy-like float list. Avoid
    # numpy dependency here — the loop is small relative to FAISS-side
    # work and we want this module to be import-cheap.
    import math
    q_norm = math.sqrt(sum(x * x for x in query_emb))
    if q_norm == 0.0:
        return []
    # Score each candidate.
    scored: dict[str, float] = {}
    for triple_id, source_atom_id, blob, t_dim in rows:
        if source_atom_id is None:
            continue
        if dim is not None and t_dim is not None and t_dim != dim:
            continue
        if t_dim is None:
            # Best-effort: assume the blob matches the query dim.
            t_dim = len(query_emb)
        if len(blob) < t_dim * 4:
            continue
        try:
            vec = list(struct.unpack(f"{t_dim}f", blob[: t_dim * 4]))
        except struct.error:
            continue
        if len(vec) != len(query_emb):
            continue
        v_norm = math.sqrt(sum(x * x for x in vec))
        if v_norm == 0.0:
            continue
        sim = sum(a * b for a, b in zip(query_emb, vec)) / (q_norm * v_norm)
        # Multiple triples may point at the same source_atom_id; keep
        # the best match per atom (saga does the same — atoms surface
        # via their *strongest* triple).
        prev = scored.get(source_atom_id, -1.0)
        if sim > prev:
            scored[source_atom_id] = sim
    ordered = sorted(scored.items(), key=lambda x: -x[1])
    return ordered[:top_k]


# ─── Entity-side retrieval (no embedding needed) ─────────────────────


def retrieve_by_entity(
    conn: sqlite3.Connection,
    entity: str,
    *,
    top_k: int = 50,
) -> list[dict]:
    """Substring match on subject or object. Used for direct entity
    probes ("what did the user say about Alice?") where the query
    *names* the entity and we can skip the embedding path."""
    pat = f"%{entity}%"
    rows = conn.execute(
        "SELECT id, subject, predicate, object, source_atom_id, "
        "valid_from, valid_until "
        "FROM triples WHERE tombstoned = 0 "
        "AND (subject LIKE ? OR object LIKE ?) "
        "LIMIT ?",
        (pat, pat, top_k),
    ).fetchall()
    return [
        {
            "id": r[0], "subject": r[1], "predicate": r[2], "object": r[3],
            "source_atom_id": r[4],
            "valid_from": r[5], "valid_until": r[6],
        }
        for r in rows
    ]


# ─── Contradiction detection ─────────────────────────────────────────


def detect_contradictions(
    conn: sqlite3.Connection,
    *,
    subject: str | None = None,
    predicate: str | None = None,
) -> list[dict]:
    """Find (subj, pred) pairs with multiple distinct CURRENT values.

    A current world_state row with `is_current=1` is by construction
    the only current value per (subj, pred), so this fires when
    triples landed faster than the world_state writer could end-date
    the prior. It also fires on (subj, pred) pairs whose triples
    weren't ingested through the world-state writer (e.g. bulk
    migration). Returns one entry per offending key with the
    conflicting values.
    """
    where = ["is_current = 1"]
    params: list = []
    if subject is not None:
        where.append("subject = ?")
        params.append(subject)
    if predicate is not None:
        where.append("predicate = ?")
        params.append(_PREDICATE_NORM.sub("_", predicate.lower()).strip("_"))
    rows = conn.execute(
        f"SELECT subject, predicate, GROUP_CONCAT(value, '|||') AS values, "
        f"COUNT(*) AS n "
        f"FROM world_state WHERE {' AND '.join(where)} "
        f"GROUP BY subject, predicate HAVING n > 1",
        params,
    ).fetchall()
    return [
        {
            "subject": r[0], "predicate": r[1],
            "values": r[2].split("|||"), "count": r[3],
        }
        for r in rows
    ]


def resolve_contradictions_to_supersedes(
    conn: sqlite3.Connection,
    *,
    strategy: str = "newest",
) -> int:
    """Walk the ``contradicts`` atom_relations and add a supersedes
    relation from the newer atom to the older. Returns the count of
    new supersedes edges written.

    ``strategy = "newest"`` picks the chronologically-later atom as
    the winner. Future strategies could weight confidence or recency
    of access — for now we match saga's bench default.

    Idempotent: relations already present are skipped via
    ``INSERT OR IGNORE``.

    Manages its own transaction (BEGIN IMMEDIATE / COMMIT). Without
    this, the INSERT loop starts an implicit transaction that
    Python's sqlite3 module never auto-commits, leaving subsequent
    ``BEGIN IMMEDIATE`` callers (e.g. recall's post-retrieval
    access-event write) to crash with "cannot start a transaction
    within a transaction."
    """
    if strategy != "newest":
        raise ValueError(f"unknown strategy: {strategy!r}")
    now = datetime.now(timezone.utc).isoformat()
    # Find every contradicts pair and pick the newer atom as winner.
    # Read-only — no transaction needed for the SELECT.
    rows = conn.execute(
        "SELECT r.source_id, r.target_id, a.created_at AS source_at, "
        "b.created_at AS target_at "
        "FROM atom_relations r "
        "JOIN atoms a ON a.id = r.source_id "
        "JOIN atoms b ON b.id = r.target_id "
        "WHERE r.relation_type = 'contradicts' "
        "AND a.tombstoned = 0 AND b.tombstoned = 0",
    ).fetchall()
    if not rows:
        return 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        added = 0
        for source_id, target_id, source_at, target_at in rows:
            winner, loser = (
                (source_id, target_id)
                if (source_at or "") > (target_at or "")
                else (target_id, source_id)
            )
            if winner == loser:
                continue
            cursor = conn.execute(
                "INSERT OR IGNORE INTO atom_relations "
                "(source_id, target_id, relation_type, confidence, "
                " created_at, metadata) "
                "VALUES (?, ?, 'supersedes', 1.0, ?, ?)",
                (winner, loser, now,
                 json.dumps({"trigger": "contradiction_resolution"})),
            )
            if cursor.rowcount > 0:
                added += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return added


__all__ = [
    "make_triple_id",
    "parse_triples",
    "store_triples",
    "triple_augment_search",
    "retrieve_by_entity",
    "get_current_value",
    "get_history",
    "WorldFact",
    "detect_contradictions",
    "resolve_contradictions_to_supersedes",
    "MAX_SUBJECT_CHARS",
    "MAX_OBJECT_CHARS",
]
