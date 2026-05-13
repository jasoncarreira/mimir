"""Recall — the two-pass retrieval path.

Implements the SCORING.md contract:

  Pass 1: candidate generation (FAISS + FTS5)
  Pass 2: activation threshold filter (per stream)
  Pass 3: combined-score ranking → top-k
  Pass 4: post-retrieval access_event fire for returned atoms

Two-tier observation/raw split preserved from saga (the orthogonal
"surfaced observation lifts its evidence raws" boost).

The infrastructure pieces (FAISS index access, FTS5 query, embedding
provider) are injected via callables so this module is testable in
isolation. Wiring happens in mimir.memory's __init__.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from .activation import (
    DEFAULT_STREAM_THRESHOLDS,
    GLOBAL_FALLBACK_THRESHOLD,
    compute_activation,
)
from .mark_access import AccessEvent, mark_access


# Default scoring weights. See SCORING.md for derivation.
DEFAULT_SCORING_WEIGHTS = {
    "w_sim":   0.7,
    "w_kw":    0.2,
    "w_topic": 0.1,
    "w_act":   0.3,
}

# Trend score modifiers — Hindsight P1's "agent should weight beliefs
# by evidence trajectory." Small magnitudes; trend is a tiebreaker, not
# a dominant factor. Stale gets the harshest penalty since the agent
# explicitly shouldn't repeat beliefs that haven't been validated
# recently.
TREND_MODIFIERS = {
    "strengthening": +0.10,
    "stable":         0.00,
    "weakening":     -0.10,
    "stale":         -0.25,
}

# Default top-k for candidate generation passes. FAISS returns 3× the
# eventual k to give the score ranker headroom; FTS5 returns 2× because
# keyword matches are usually narrower.
FAISS_CANDIDATE_MULTIPLIER = 3
FTS_CANDIDATE_MULTIPLIER = 2


# Provider callables — same shape as store.EmbedFn but for queries.
QueryEmbedFn = Callable[[str], list[float]]
# FAISS search: (query_emb, top_k) → [(atom_id, similarity), ...]
FaissSearchFn = Callable[[list[float], int], list[tuple[str, float]]]
# FTS5 search: (query_string, top_k) → [(atom_id, bm25_score), ...]
FtsSearchFn = Callable[[str, int], list[tuple[str, float]]]


@dataclass
class RecallCandidate:
    """An atom plus its scoring components. Returned by recall() to the
    caller so they can inspect WHY a result ranked where it did — useful
    for the turn viewer and for debugging."""
    atom: dict                       # the atom row (everything except embedding)
    activation: float                # ACT-R base-level
    similarity: float                # cosine (0..1)
    keyword_score: float = 0.0       # BM25 contribution
    topic_score: float = 0.0
    evidence_boost: float = 0.0
    session_boost: float = 0.0
    pinned_boost: float = 0.0
    trend_modifier: float = 0.0      # +0.1 strengthening / -0.1 weakening / -0.25 stale
    trend_label: str | None = None   # for observability (turn viewer)
    supersession_penalty: float = 0.0
    contradiction_penalty: float = 0.0
    total: float = 0.0


@dataclass
class RecallResult:
    observations: list[RecallCandidate] = field(default_factory=list)
    raws: list[RecallCandidate] = field(default_factory=list)
    rewritten_query: str | None = None
    gated: bool = False
    gated_reason: str | None = None


def recall(
    conn: sqlite3.Connection,
    query: str,
    *,
    query_embed_fn: QueryEmbedFn,
    faiss_search_fn: FaissSearchFn,
    fts_search_fn: FtsSearchFn,
    k: int = 12,
    thresholds: dict[str, float] | None = None,
    agent_id: str = "default",
    stream_filter: str | None = None,
    topic_filter: list[str] | None = None,
    include_session_boundaries: bool = False,
    session_id: str | None = None,
    weights: dict[str, float] | None = None,
    fire_access_events: bool = True,
) -> RecallResult:
    """Two-pass recall. See SCORING.md for the contract.

    ``fire_access_events=False`` skips Pass 4 (the access_event for
    returned atoms). Used by the migration importer and by tests; the
    agent's normal recall path keeps it True.
    """
    thresholds = thresholds or DEFAULT_STREAM_THRESHOLDS
    weights = weights or DEFAULT_SCORING_WEIGHTS

    # ── Pass 1: candidate generation ────────────────────────────────
    query_emb = query_embed_fn(query)
    faiss_candidates = faiss_search_fn(
        query_emb, k * FAISS_CANDIDATE_MULTIPLIER,
    )
    fts_candidates = fts_search_fn(
        query, k * FTS_CANDIDATE_MULTIPLIER,
    )
    sim_map = {aid: sim for aid, sim in faiss_candidates}
    kw_map = {aid: kw for aid, kw in fts_candidates}
    candidate_ids = set(sim_map) | set(kw_map)
    if not candidate_ids:
        return RecallResult()

    # Fetch full atom rows + summaries in one pass.
    placeholders = ",".join(["?"] * len(candidate_ids))
    candidate_id_list = list(candidate_ids)
    atom_rows = conn.execute(
        f"SELECT id, content, stream, profile, memory_type, source_type, "
        f"topics, metadata, agent_id, is_pinned, created_at, session_id "
        f"FROM atoms WHERE id IN ({placeholders}) AND tombstoned = 0",
        candidate_id_list,
    ).fetchall()
    cols = ("id", "content", "stream", "profile", "memory_type",
            "source_type", "topics", "metadata", "agent_id", "is_pinned",
            "created_at", "session_id")
    atoms = {row[0]: dict(zip(cols, row)) for row in atom_rows}

    # Apply agent_id filter + source_type filter (session_boundary)
    # + optional stream filter at this stage.
    filtered: dict[str, dict] = {}
    for atom_id, atom in atoms.items():
        if atom["agent_id"] != agent_id and atom["agent_id"] != "shared":
            continue
        if not include_session_boundaries and atom["source_type"] == "session_boundary":
            continue
        if stream_filter and atom["stream"] != stream_filter:
            continue
        filtered[atom_id] = atom

    if not filtered:
        return RecallResult()

    # Fetch summaries for activation computation.
    summary_rows = conn.execute(
        f"SELECT atom_id, recent_ts_json, recent_weights_json, "
        f"old_count, old_weight_sum, old_oldest_ts "
        f"FROM atom_access_summary WHERE atom_id IN ({placeholders})",
        candidate_id_list,
    ).fetchall()
    summaries = {
        r[0]: {
            "recent_ts": json.loads(r[1] or "[]"),
            "recent_weights": json.loads(r[2] or "[]"),
            "old_count": r[3] or 0,
            "old_weight_sum": r[4] or 0.0,
            "old_oldest_ts": r[5],
        } for r in summary_rows
    }

    # ── Pass 2: activation filter ──────────────────────────────────
    candidates: list[RecallCandidate] = []
    for atom_id, atom in filtered.items():
        s = summaries.get(atom_id)
        if s is None:
            # Atom exists but never had an access_event logged. This
            # shouldn't happen if store() ran cleanly, but we're defensive:
            # treat as just-stored with activation just above threshold.
            activation = thresholds.get(atom["stream"], GLOBAL_FALLBACK_THRESHOLD)
        else:
            activation = compute_activation(
                recent_ts=s["recent_ts"],
                recent_weights=s["recent_weights"],
                old_count=s["old_count"],
                old_weight_sum=s["old_weight_sum"],
                old_oldest_ts=s["old_oldest_ts"],
            )
        threshold = thresholds.get(atom["stream"], GLOBAL_FALLBACK_THRESHOLD)
        # Pinned atoms bypass the threshold filter — they're meant to
        # always be eligible. The score still ranks them, so the threshold
        # bypass doesn't mean they always win, just that they always
        # compete.
        if not atom["is_pinned"] and activation < threshold:
            continue
        candidates.append(RecallCandidate(
            atom=atom,
            activation=activation,
            similarity=sim_map.get(atom_id, 0.0),
            keyword_score=kw_map.get(atom_id, 0.0),
        ))

    if not candidates:
        return RecallResult()

    # ── Pass 3: combined-score ranking ─────────────────────────────
    _score_candidates(conn, candidates, topic_filter, session_id, weights, thresholds)

    # Two-tier split: observations vs raws. Each ranked on its own
    # pool, then observations' evidenced_by raws get a boost in the
    # raws pool. See SCORING.md "boost_evidence".
    observations = [c for c in candidates if c.atom["memory_type"] == "observation"]
    raws = [c for c in candidates if c.atom["memory_type"] != "observation"]
    observations.sort(key=lambda c: -c.total)
    surfaced_obs = observations[:_obs_top_k(k)]

    # Evidence boost: each surfaced observation lifts its evidence raws.
    _apply_evidence_boost(conn, surfaced_obs, raws, weights)
    raws.sort(key=lambda c: -c.total)
    surfaced_raws = raws[:k]

    # ── Pass 4: fire access_events for returned atoms ──────────────
    # In its own transaction — mark_access is non-transactional; recall
    # wraps it. Failure here doesn't roll back the read pass (which had
    # no writes anyway).
    if fire_access_events:
        events = [
            AccessEvent(atom_id=c.atom["id"], source="retrieval",
                        session_id=session_id)
            for c in surfaced_obs + surfaced_raws
        ]
        if events:
            try:
                conn.execute("BEGIN IMMEDIATE")
                mark_access(conn, events)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    return RecallResult(
        observations=surfaced_obs,
        raws=surfaced_raws,
    )


def _obs_top_k(k: int) -> int:
    """Observations get a smaller top-k than raws — Hindsight's hierarchy
    says the distilled tier is narrower. Match saga's
    observations_top_k=5 default."""
    return min(5, k)


def _sigmoid(x: float) -> float:
    """Maps activation-above-threshold to [0, 1]. Used to compose
    activation contribution into the score."""
    import math
    if x > 50:    # avoid overflow on absurd inputs
        return 1.0
    if x < -50:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _score_candidates(
    conn: sqlite3.Connection,
    candidates: list[RecallCandidate],
    topic_filter: list[str] | None,
    session_id: str | None,
    weights: dict[str, float],
    thresholds: dict[str, float],
) -> None:
    """Compute total score per candidate. Modifies in place."""
    # Topic filter membership (one query for all candidates).
    topic_hits: dict[str, set[str]] = {}
    if topic_filter:
        ids = [c.atom["id"] for c in candidates]
        placeholders = ",".join(["?"] * len(ids))
        tp_holders = ",".join(["?"] * len(topic_filter))
        rows = conn.execute(
            f"SELECT atom_id, topic FROM atom_topics "
            f"WHERE atom_id IN ({placeholders}) AND topic IN ({tp_holders})",
            ids + topic_filter,
        ).fetchall()
        for aid, t in rows:
            topic_hits.setdefault(aid, set()).add(t)

    # Recent-session-access fast path: atoms touched in the current
    # session get a small score boost (proximity matters).
    recent_session_ids: set[str] = set()
    if session_id:
        rows = conn.execute(
            "SELECT DISTINCT atom_id FROM access_events WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        recent_session_ids = {r[0] for r in rows}

    # Trend lookup for observation-typed candidates. One query for all.
    obs_ids_in_candidates = [
        c.atom["id"] for c in candidates
        if c.atom["memory_type"] == "observation"
    ]
    trend_by_atom: dict[str, str] = {}
    if obs_ids_in_candidates:
        placeholders = ",".join(["?"] * len(obs_ids_in_candidates))
        rows = conn.execute(
            f"SELECT atom_id, trend FROM observations_metadata "
            f"WHERE atom_id IN ({placeholders})",
            obs_ids_in_candidates,
        ).fetchall()
        trend_by_atom = {r[0]: r[1] for r in rows if r[1]}

    for c in candidates:
        threshold = thresholds.get(
            c.atom["stream"], GLOBAL_FALLBACK_THRESHOLD,
        )
        c.topic_score = (
            1.0 if topic_filter and topic_hits.get(c.atom["id"])
            else 0.0
        )
        if c.atom["id"] in recent_session_ids:
            c.session_boost = 0.15  # small but meaningful tiebreaker
        if c.atom["is_pinned"]:
            c.pinned_boost = 0.25

        # Trend modifier (observations only — raws have no trend).
        trend = trend_by_atom.get(c.atom["id"])
        if trend:
            c.trend_label = trend
            c.trend_modifier = TREND_MODIFIERS.get(trend, 0.0)

        c.total = (
            weights["w_sim"]   * c.similarity
          + weights["w_kw"]    * c.keyword_score
          + weights["w_topic"] * c.topic_score
          + weights["w_act"]   * _sigmoid(c.activation - threshold)
          + c.session_boost
          + c.pinned_boost
          + c.trend_modifier
          # Evidence boost applied in _apply_evidence_boost (post-split).
          # Supersession/contradiction penalties are TBD — they require
          # querying atom_relations for the candidate set and aren't
          # in this v1 sketch.
        )


def _apply_evidence_boost(
    conn: sqlite3.Connection,
    surfaced_obs: list[RecallCandidate],
    raws: list[RecallCandidate],
    weights: dict[str, float],
) -> None:
    """Each surfaced observation lifts its evidenced_by raws (the
    raws it consolidated from) by a fixed boost. Two-tier evidence
    propagation per SCORING.md."""
    if not surfaced_obs:
        return
    obs_ids = [c.atom["id"] for c in surfaced_obs]
    placeholders = ",".join(["?"] * len(obs_ids))
    rows = conn.execute(
        f"SELECT source_id, target_id FROM atom_relations "
        f"WHERE source_id IN ({placeholders}) AND relation_type = 'evidenced_by'",
        obs_ids,
    ).fetchall()
    boosted_raws: set[str] = {target for _, target in rows}
    if not boosted_raws:
        return
    BOOST = 0.20  # matches saga's surfaced-obs raw-lift magnitude
    for c in raws:
        if c.atom["id"] in boosted_raws:
            c.evidence_boost = BOOST
            c.total += BOOST
