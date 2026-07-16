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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ._like import escape_like_pattern


logger = logging.getLogger("mimir.saga.triples")


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
# Each slot accepts EITHER ``"..."``/``'...'`` (quoted — commas
# allowed inside) OR a bare token (no commas, no parens). The
# quoted form unblocks the case where the LLM emits
# ``(Alice, born_in, "Cambridge, MA")`` — pre-fix, the bare-token
# branch's ``[^,()]+`` rejected the embedded comma and dropped the
# whole triple silently.
_SLOT = r'(?:"[^"]*"|\'[^\']*\'|[^,()]+)'
_TRIPLE_LINE = re.compile(
    rf"\(({_SLOT}),\s*({_SLOT}),\s*({_SLOT}?)(?:,([^()]+))?\)",
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
      First-mention ``valid_from`` is preserved; a later re-assertion
      with a different (typically later) ``valid_from`` does NOT
      bump the stored timestamp. The fact's validity is anchored at
      first claim.
    - Else: mark the prior current row as is_current=0, set its
      valid_until to the new row's valid_from (or now() if missing),
      then upsert the new row as is_current=1.

    chainlink #304: the insert is ``INSERT OR REPLACE``, not ``OR
    IGNORE``. When a new (different) value shares the current row's
    ``valid_from`` — a same-timestamp change — the PK
    (subject, predicate, valid_from) collides with the row we just
    end-dated, and ``OR IGNORE`` silently dropped the new value, leaving
    is_current=0 on the old row and NO current row at all. ``OR REPLACE``
    makes the new value win. Re-assertions of the SAME value are still a
    no-op via the ``cur_value == value`` early return above, so a triple
    appearing from multiple atoms stays idempotent.
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
        # OR REPLACE (not OR IGNORE): a same-valid_from value change
        # collides with the row we just end-dated; IGNORE would drop the
        # new value and leave no current row (chainlink #304).
        "INSERT OR REPLACE INTO world_state "
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
        "AND is_current = 1 "
        "ORDER BY valid_from DESC, rowid DESC LIMIT 1",
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


def _cosine_scores(
    query_emb: list[float],
    blobs_dims: list[tuple[bytes | None, int | None]],
    *,
    dim: int | None,
) -> list[tuple[int, float]]:
    """Cosine of *query_emb* against each ``(blob, t_dim)`` candidate, as
    one numpy matmul. Returns ``(index, sim)`` for usable rows, in input
    order (caller maps the index back to its row).

    chainlink #257 perf half: replaces the former O(N·dim) pure-Python
    cosine loop (``struct.unpack`` + ``math.sqrt`` + ``zip`` dot per
    triple) with a single vectorized matmul, so the hot ``query()`` path
    no longer scales linearly *in Python* with the triple corpus. The math
    runs in float64 to match the original loop's precision (embeddings are
    stored float32; upcast for the dot/norm).

    Row-skip semantics are preserved exactly: a candidate is dropped when
    its ``t_dim`` mismatches *dim*, its blob is too short, its unpacked
    shape doesn't match the query, or its (or the query's) norm is zero.
    No persistent ANN index — at the projected corpus size (tens of
    thousands of triples) a vectorized matmul is sub-millisecond; a FAISS
    triples index analogous to atoms would only pay off at million-scale
    and is deferred (it would add a build/add/freshness lifecycle).
    """
    import numpy as np

    q = np.asarray(query_emb, dtype=np.float64)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0.0:
        return []
    q_dim = int(q.shape[0])

    kept_idx: list[int] = []
    vecs: list[np.ndarray] = []
    for i, (blob, t_dim) in enumerate(blobs_dims):
        td = t_dim if t_dim is not None else q_dim
        if dim is not None and t_dim is not None and t_dim != dim:
            continue
        if blob is None or len(blob) < td * 4:
            continue
        v = np.frombuffer(blob[: td * 4], dtype=np.float32)
        if v.shape[0] != q_dim:
            continue
        kept_idx.append(i)
        vecs.append(v.astype(np.float64))
    if not vecs:
        return []

    mat = np.vstack(vecs)
    norms = np.linalg.norm(mat, axis=1)
    dots = mat @ q
    out: list[tuple[int, float]] = []
    for k, i in enumerate(kept_idx):
        n = float(norms[k])
        if n == 0.0:
            continue
        out.append((i, float(dots[k]) / (q_norm * n)))
    return out


def triple_augment_search(
    conn: sqlite3.Connection,
    query_emb: list[float],
    *,
    top_k: int = 10,
    dim: int | None = None,
    reference_date=None,
    auth_context: Any = None,
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

    ``reference_date`` (datetime or None) anchors the ``valid_until``
    expiry filter. Expired triples (valid_until ≤ reference_date) are
    excluded from the candidate set — they represent superseded facts
    and should not surface in retrieval. Defaults to utcnow when None.

    chainlink #883: authorization filters triples based on source atom ownership.
    """
    from .ownership import (
        authorization_predicate,
        get_authorization_scope,
    )

    auth_scope = get_authorization_scope(auth_context)
    auth_where, auth_params = authorization_predicate(auth_scope, table="a")

    ref_iso = (
        reference_date.isoformat()
        if reference_date is not None
        else datetime.now(timezone.utc).isoformat()
    )
    rows = conn.execute(
        f"""SELECT t.id, t.source_atom_id, t.embedding, t.embedding_dim
            FROM triples t
            JOIN atoms a ON t.source_atom_id = a.id
            WHERE t.tombstoned = 0 AND t.embedding IS NOT NULL
              AND (t.valid_until IS NULL OR t.valid_until > ?)
              AND {auth_where}
        """,
        (ref_iso,) + tuple(auth_params),
    ).fetchall()
    if not rows:
        return []
    candidates = [r for r in rows if r[1] is not None]
    scores = _cosine_scores(
        query_emb, [(r[2], r[3]) for r in candidates], dim=dim,
    )
    best: dict[str, float] = {}
    for i, sim in scores:
        source_atom_id = candidates[i][1]
        if sim > best.get(source_atom_id, -1.0):
            best[source_atom_id] = sim
    ordered = sorted(best.items(), key=lambda x: -x[1])
    return ordered[:top_k]


def top_triples_with_payload(
    conn: sqlite3.Connection,
    query_emb: list[float],
    *,
    top_n: int = 10,
    dim: int | None = None,
    reference_date=None,
    auth_context: Any = None,
) -> list[dict]:
    """Same cosine match as ``triple_augment_search`` but returns the
    FULL triple data — subject/predicate/object/source_atom_id/valid
    range/confidence — keyed on triple id rather than collapsing to one
    row per source atom.

    Used by ``SagaStore.query`` to surface a top-N triples block in
    the response payload (saga's P42 ``include_triples_in_response``
    shape). Distinct from ``triple_augment_search`` because the
    retrieval-pathway view wants the best-triple-per-atom (no
    duplicates ranking the same atom), but the response-payload view
    wants each individual triple match so the agent reads structured
    facts directly.

    ``reference_date`` (datetime or None) anchors the ``valid_until``
    expiry filter. Expired triples are excluded so stale facts (e.g.
    "user works at X" after a close-out event) don't surface as current.
    Defaults to utcnow when None. Documents the behaviour the config
    key ``include_triples_in_response`` claims: "Filters out triples
    whose valid_until has expired."

    chainlink #883: authorization filters triples based on source atom ownership.
    """
    from .ownership import (
        authorization_predicate,
        get_authorization_scope,
    )

    auth_scope = get_authorization_scope(auth_context)
    auth_where, auth_params = authorization_predicate(auth_scope, table="a")

    ref_iso = (
        reference_date.isoformat()
        if reference_date is not None
        else datetime.now(timezone.utc).isoformat()
    )
    rows = conn.execute(
        f"""SELECT t.id, t.source_atom_id, t.subject, t.predicate, t.object,
                  t.valid_from, t.valid_until, t.confidence, t.embedding, t.embedding_dim
            FROM triples t
            JOIN atoms a ON t.source_atom_id = a.id
            WHERE t.tombstoned = 0 AND t.embedding IS NOT NULL
              AND (t.valid_until IS NULL OR t.valid_until > ?)
              AND {auth_where}
        """,
        (ref_iso,) + tuple(auth_params),
    ).fetchall()
    if not rows:
        return []
    candidates = [r for r in rows if r[1] is not None]
    scores = _cosine_scores(
        query_emb, [(r[8], r[9]) for r in candidates], dim=dim,
    )
    scored: list[tuple[float, dict]] = []
    for i, sim in scores:
        (triple_id, source_atom_id, subj, pred, obj,
         valid_from, valid_until, confidence, _blob, _t_dim) = candidates[i]
        scored.append((sim, {
            "id": triple_id,
            "source_atom_id": source_atom_id,
            "subject": subj,
            "predicate": pred,
            "object": obj,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "confidence": confidence,
            "_cosine": sim,
        }))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:top_n]]


# ─── Entity-side retrieval (no embedding needed) ─────────────────────


def retrieve_by_entity(
    conn: sqlite3.Connection,
    entity: str,
    *,
    top_k: int = 50,
    auth_context: Any = None,
) -> list[dict]:
    """Substring match on subject or object. Used for direct entity
    probes ("what did the user say about Alice?") where the query
    *names* the entity and we can skip the embedding path.

    chainlink #883: authorization filters triples based on source atom ownership.
    """
    from .ownership import (
        authorization_predicate,
        get_authorization_scope,
    )

    auth_scope = get_authorization_scope(auth_context)
    auth_where, auth_params = authorization_predicate(auth_scope, table="a")

    pat = f"%{escape_like_pattern(entity)}%"
    rows = conn.execute(
        f"""SELECT t.id, t.subject, t.predicate, t.object, t.source_atom_id,
                  t.valid_from, t.valid_until
            FROM triples t
            JOIN atoms a ON t.source_atom_id = a.id
            WHERE t.tombstoned = 0
              AND (t.subject LIKE ? ESCAPE '\\' OR t.object LIKE ? ESCAPE '\\')
              AND {auth_where}
            LIMIT ?""",
        (pat, pat, *auth_params, top_k),
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

    Scope: this query catches the residual transient race window in
    ``world_state``. The load-bearing contradiction surface in
    production is ``atom_relations.contradicts``, populated by
    ``synthesize._parse_contradictions`` during the rich-synth
    consolidation pass and persisted via ``store_triples`` /
    ``resolve_contradictions_to_supersedes``. Callers that want the
    full picture should walk both this and that table.
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
        f"SELECT subject, predicate, GROUP_CONCAT(value, '|||') AS vals, "
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


def repair_world_state_dual_current(conn: sqlite3.Connection) -> list[dict]:
    """Collapse any (subject, predicate) carrying >1 ``is_current=1`` row down to
    a single current value, end-dating the rest (chainlink #331).

    ``detect_contradictions`` REPORTS the dual-current transient race — triples
    landing faster than ``_update_world_state`` can end-date the prior current
    row, or bulk-migrated rows that bypassed the writer — but never repaired it,
    so ``get_current_value`` stayed ambiguous and nothing fixed it. This is the
    repair: for each offending key, keep the newest row by ``valid_from`` and set
    the losers to ``is_current=0`` with ``valid_until`` = the winner's
    ``valid_from``.

    Returns one record per repaired key —
    ``{subject, predicate, kept_value, kept_valid_from, superseded:[{value,
    valid_from}, ...]}`` — and an empty list when world_state is already
    consistent. Each repair logs at WARNING so the race is observable rather than
    silent.

    Manages its own transaction (BEGIN IMMEDIATE / COMMIT), matching
    ``resolve_contradictions_to_supersedes`` — so it composes in the
    consolidation pass without leaving an open implicit transaction for the next
    ``BEGIN IMMEDIATE`` caller to trip over.
    """
    conflicts = detect_contradictions(conn)  # read-only; identifies candidate keys
    if not conflicts:
        return []
    now = datetime.now(timezone.utc).isoformat()
    repairs: list[dict] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for conflict in conflicts:
            subject = conflict["subject"]
            predicate = conflict["predicate"]
            # Re-read under the write lock — the candidate may have raced clean
            # between detect and here. ``rowid`` keys the UPDATE so it is
            # NULL-safe (bulk-migrated rows may have NULL valid_from) and never
            # matches the winner. NULLs sort last under DESC, so a timestamped
            # row wins; ties fall back to rowid for a deterministic winner.
            rows = conn.execute(
                "SELECT rowid, value, valid_from FROM world_state "
                "WHERE subject = ? AND predicate = ? AND is_current = 1 "
                "ORDER BY valid_from DESC, rowid DESC",
                (subject, predicate),
            ).fetchall()
            if len(rows) <= 1:
                continue
            _, kept_value, kept_valid_from = rows[0]
            superseded: list[dict] = []
            for loser_rowid, loser_value, loser_valid_from in rows[1:]:
                conn.execute(
                    "UPDATE world_state SET is_current = 0, valid_until = ?, "
                    "updated_at = ? WHERE rowid = ?",
                    (kept_valid_from, now, loser_rowid),
                )
                superseded.append(
                    {"value": loser_value, "valid_from": loser_valid_from}
                )
            logger.warning(
                "world_state_dual_current_repaired: (%r, %r) kept %r "
                "(valid_from=%s); end-dated %d stale current row(s): %s",
                subject, predicate, kept_value, kept_valid_from,
                len(superseded), [s["value"] for s in superseded],
            )
            repairs.append({
                "subject": subject,
                "predicate": predicate,
                "kept_value": kept_value,
                "kept_valid_from": kept_valid_from,
                "superseded": superseded,
            })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return repairs


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
    "repair_world_state_dual_current",
    "MAX_SUBJECT_CHARS",
    "MAX_OBJECT_CHARS",
]
