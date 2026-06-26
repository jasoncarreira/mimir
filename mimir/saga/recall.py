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
isolation. Wiring happens in mimir.saga's __init__.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from .activation import (
    DEFAULT_STREAM_THRESHOLDS,
    GLOBAL_FALLBACK_THRESHOLD,
    compute_activation,
)
from .dedup import BASELINE_ENCODING_CONFIDENCE
from .mark_access import AccessEvent, mark_access
from .retrieval_fusion import DEFAULT_K as RRF_DEFAULT_K, reciprocal_rank_fusion


# Default scoring weights.
#
# History: v1 used weighted-sum (``w_sim * similarity + w_kw * keyword_score``)
# which required careful score-scale calibration between FAISS cosine
# (~[0,1]) and FTS5 BM25 (~[0,50]). v2 switches to Reciprocal Rank
# Fusion on the FAISS + FTS ranked lists per saga's canonical bench
# (saga_bench.toml: fusion="rrf", rrf_k=60, equal pathway weights).
# RRF produces a normalized base score; the orthogonal modifiers
# (activation, trend, evidence_boost, session_boost, pinned_boost)
# are still added on top.
#
# Calibration of ``w_rrf``: raw RRF score with k=60 maxes at
# 1/(60+1) = 0.0164 per pathway, so a candidate at rank 1 in both
# semantic + keyword pathways gets ~0.033. To keep the modifier
# magnitudes (session_boost=0.15, pinned=0.25, trend ±0.10/-0.25,
# evidence_boost=0.20) as tiebreakers rather than dominators, we
# scale RRF up so top-of-both lands around ~0.65 — comparable to
# the old top-semantic-match base score under weighted-sum.
DEFAULT_SCORING_WEIGHTS = {
    "w_rrf":    20.0,  # RRF base contribution (see calibration above)
    "w_topic":  0.1,
    "w_act":    0.3,
}

# Per-pathway RRF weights. Saga's bench used equal weights since v0;
# keeping the same here. The semantic pathway can be biased up for
# domains where embedding quality dominates keyword overlap (e.g.
# heavily paraphrased question sets) — leaving as a knob.
DEFAULT_RRF_WEIGHTS = {
    "semantic": 1.0,
    "keyword":  1.0,
    "triple":   1.0,   # P42 triple-augment pathway; only contributes
                       # when a triple_search_fn is wired in. Matches
                       # saga's rrf_triple_augment_weight default.
}
_RESERVED_RRF_PATHWAYS = frozenset(DEFAULT_RRF_WEIGHTS)

#: Per-atom confidence_tier thresholds. Saga bench TOML defaults:
#: ``confidence_sim_high = 0.45``, ``_medium = 0.30``, ``_low = 0.10``.
#: An atom's similarity (max of FAISS cosine vs query embedding,
#: triple cosine, and a small floor) maps to a tier label. Production
#: callers filter retrieval by ``min_confidence_tier`` to suppress
#: weakly-grounded atoms; prompt rendering uses the tag for the
#: per-atom label (``observation/high``, ``raw/medium``).
CONFIDENCE_TIER_THRESHOLDS = {
    "high":   0.45,
    "medium": 0.30,
    "low":    0.10,
}

# Tier ranking for the ``min_confidence_tier`` filter — higher index
# means stricter tier. min_confidence_tier="medium" keeps medium+high.
_TIER_ORDER = ["none", "low", "medium", "high"]


def _tier_for_similarity(sim: float) -> str:
    """Map a similarity score to a tier label using the bench defaults."""
    if sim >= CONFIDENCE_TIER_THRESHOLDS["high"]:
        return "high"
    if sim >= CONFIDENCE_TIER_THRESHOLDS["medium"]:
        return "medium"
    if sim >= CONFIDENCE_TIER_THRESHOLDS["low"]:
        return "low"
    return "none"


def _passes_min_tier(tier: str, min_tier: str | None) -> bool:
    """Whether ``tier`` is at-or-above ``min_tier``. None min means
    accept everything (filter off)."""
    if min_tier is None:
        return True
    try:
        return _TIER_ORDER.index(tier) >= _TIER_ORDER.index(min_tier)
    except ValueError:
        return True  # unknown tier label — don't filter

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

#: Weight applied to the encoding-confidence delta in the composite
#: score. Computed as
#: ``ENCODING_CONFIDENCE_WEIGHT * (encoding_confidence - BASELINE)``
#: so a fresh-out-of-the-box atom contributes zero, and post-baseline
#: confidence — produced by dedup absorption today, by future feedback
#: signals later — adds a small additive boost. Magnitude calibrated
#: to roughly match ``TREND_MODIFIERS["strengthening"]`` at the
#: asymptotic ceiling (delta=0.3 × 0.05 ≈ 0.015), well below the
#: ``w_rrf=20`` dominant term — strictly a tiebreaker among
#: similar-relevance candidates, never enough to surface a marginally-
#: relevant repeatedly-encoded fact over a highly-relevant one-shot.
# chainlink #266: source_type tag for skill-learning atoms, excluded from
# general recall (see the filter loop in ``recall``). Defined locally to
# keep mimir.saga a self-contained lower layer (it does not import up into
# mimir.*); the source of truth for the convention is
# ``mimir/skill_memory.py:SKILL_LEARNING_SOURCE_TYPE`` — keep in sync.
_SKILL_LEARNING_SOURCE_TYPE = "skill_learning"

ENCODING_CONFIDENCE_WEIGHT = 0.05

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
# Triple-augment search: (query_emb, top_k) → [(source_atom_id, cosine), ...]
# Same shape as FaissSearchFn so the RRF assembly stays uniform; the
# atoms surfaced here are atoms whose extracted triples match the query
# embedding, complementary to the direct atom-embedding match.
TripleSearchFn = Callable[[list[float], int], list[tuple[str, float]]]


@dataclass
class RecallCandidate:
    """An atom plus its scoring components. Returned by recall() to the
    caller so they can inspect WHY a result ranked where it did — useful
    for the turn viewer and for debugging."""
    atom: dict                       # the atom row (everything except embedding)
    activation: float                # ACT-R base-level
    similarity: float                # cosine (0..1), -1.0 if not in FAISS list
    keyword_score: float = 0.0       # BM25 contribution
    rrf_score: float = 0.0           # fused rank score from FAISS + FTS + triple lists
    semantic_rank: int = -1          # 1-based rank in FAISS list; -1 if absent
    keyword_rank: int = -1           # 1-based rank in FTS list; -1 if absent
    triple_rank: int = -1            # 1-based rank in triple-augment list; -1 if absent
    triple_similarity: float = 0.0   # best cosine of any triple sourcing this atom
    topic_score: float = 0.0
    evidence_boost: float = 0.0
    session_boost: float = 0.0
    pinned_boost: float = 0.0
    trend_modifier: float = 0.0      # +0.1 strengthening / -0.1 weakening / -0.25 stale
    trend_label: str | None = None   # for observability (turn viewer)
    confidence_tier: str = "none"    # high / medium / low / none — derived from similarity vs threshold
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
    triple_search_fn: TripleSearchFn | None = None,
    extra_atom_ranked_pathways: Mapping[str, Iterable[str]] | None = None,
    rrf_pathway_weights: Mapping[str, float] | None = None,
    k: int = 12,
    thresholds: dict[str, float] | None = None,
    agent_id: str = "default",
    stream_filter: str | None = None,
    topic_filter: list[str] | None = None,
    session_id: str | None = None,
    weights: dict[str, Any] | None = None,
    fire_access_events: bool = True,
    reference_date=None,
    min_confidence_tier: str | None = None,
) -> RecallResult:
    """Two-pass recall. See SCORING.md for the contract.

    ``fire_access_events=False`` skips Pass 4 (the access_event for
    returned atoms). Used by the migration importer and by tests; the
    agent's normal recall path keeps it True.

    ``reference_date`` (datetime) anchors temporal scoring to a
    specific moment instead of wall-clock now. Critical for benches
    over historical haystacks ("2 weeks ago" in a 2023 corpus run
    from 2026 should compute against the haystack's timeline). When
    None, all temporal reasoning uses datetime.now(utc).
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
    # Third pathway: triple-augment. Embed query → cosine-match against
    # extracted triples → follow source_atom_id back to the atom. Off
    # by default (triple_search_fn=None); the bench wires it via
    # SagaStore when triples are populated.
    triple_candidates: list[tuple[str, float]] = []
    if triple_search_fn is not None:
        try:
            triple_candidates = triple_search_fn(
                query_emb, k * FAISS_CANDIDATE_MULTIPLIER,
            )
        except Exception:
            triple_candidates = []
    sim_map = {aid: sim for aid, sim in faiss_candidates}
    kw_map = {aid: kw for aid, kw in fts_candidates}
    triple_sim_map = {aid: sim for aid, sim in triple_candidates}
    # Per-pathway 1-based rank, used by both RRF and the candidate
    # diagnostic fields. faiss_candidates / fts_candidates arrive
    # pre-sorted (best first) from their respective adapters.
    semantic_rank_map = {aid: i + 1 for i, (aid, _) in enumerate(faiss_candidates)}
    keyword_rank_map = {aid: i + 1 for i, (aid, _) in enumerate(fts_candidates)}
    triple_rank_map = {aid: i + 1 for i, (aid, _) in enumerate(triple_candidates)}
    extra_ranked_lists: dict[str, list[str]] = {}
    for pathway, atom_ids in (extra_atom_ranked_pathways or {}).items():
        if not pathway:
            continue
        if pathway in _RESERVED_RRF_PATHWAYS:
            raise ValueError(
                "extra RRF pathway collides with built-in pathway: "
                f"{pathway}"
            )
        seen_atom_ids: set[str] = set()
        extra_ranked_lists[pathway] = []
        for aid in atom_ids:
            if not isinstance(aid, str) or not aid or aid in seen_atom_ids:
                continue
            seen_atom_ids.add(aid)
            extra_ranked_lists[pathway].append(aid)
    candidate_ids = (
        set(sim_map)
        | set(kw_map)
        | set(triple_sim_map)
        | {aid for ids in extra_ranked_lists.values() for aid in ids}
    )
    if not candidate_ids:
        return RecallResult()

    # ── RRF fusion ──────────────────────────────────────────────────
    # Compute once, here, over the union of candidate IDs. Per-candidate
    # rrf_score lookups go into _score_candidates. We use the saga
    # canonical weights (semantic=keyword=triple=1.0) and k=60 by default.
    rrf_weights = (
        rrf_pathway_weights
        or (weights and weights.get("rrf_pathway_weights"))
        or DEFAULT_RRF_WEIGHTS
    )
    rrf_k = (weights and weights.get("rrf_k")) or RRF_DEFAULT_K
    ranked_lists = {
        "semantic": [aid for aid, _ in faiss_candidates],
        "keyword":  [aid for aid, _ in fts_candidates],
    }
    if triple_candidates:
        ranked_lists["triple"] = [aid for aid, _ in triple_candidates]
    for pathway, atom_ids in extra_ranked_lists.items():
        ranked_lists[pathway] = atom_ids
    rrf_fused = reciprocal_rank_fusion(
        ranked_lists,
        k=rrf_k, weights=rrf_weights,
    )
    rrf_map = dict(rrf_fused)

    # Fetch full atom rows + summaries in one pass.
    placeholders = ",".join(["?"] * len(candidate_ids))
    candidate_id_list = list(candidate_ids)
    atom_rows = conn.execute(
        f"SELECT id, content, stream, profile, memory_type, source_type, "
        f"topics, metadata, agent_id, is_pinned, created_at, session_id, "
        f"encoding_confidence "
        f"FROM atoms WHERE id IN ({placeholders}) AND tombstoned = 0",
        candidate_id_list,
    ).fetchall()
    cols = ("id", "content", "stream", "profile", "memory_type",
            "source_type", "topics", "metadata", "agent_id", "is_pinned",
            "created_at", "session_id", "encoding_confidence")
    atoms = {row[0]: dict(zip(cols, row)) for row in atom_rows}

    # Apply agent_id filter + optional stream filter at this stage.
    filtered: dict[str, dict] = {}
    for atom_id, atom in atoms.items():
        if atom["agent_id"] != agent_id and atom["agent_id"] != "shared":
            continue
        if stream_filter and atom["stream"] != stream_filter:
            continue
        # chainlink #266: skill-learning atoms are skill-scoped memory —
        # surfaced only via the skill-load injection, never in general
        # recall (a circuit-breaker gotcha must not appear as a "memory"
        # in an unrelated turn). They're embedded/FTS-indexed like any
        # atom, so they can land in the candidate set; drop them here.
        if atom["source_type"] == _SKILL_LEARNING_SOURCE_TYPE:
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
                now=reference_date,
            )
        threshold = thresholds.get(atom["stream"], GLOBAL_FALLBACK_THRESHOLD)
        # Pinned atoms bypass the threshold filter — they're meant to
        # always be eligible. The score still ranks them, so the threshold
        # bypass doesn't mean they always win, just that they always
        # compete.
        if not atom["is_pinned"] and activation < threshold:
            continue
        # The atom's "best" similarity is the max of its FAISS cosine
        # and its triple-augment cosine — both are embedding-cosine
        # comparable; keyword BM25 isn't. This drives the
        # confidence_tier label saga's prompt uses for the
        # per-atom tag (observation/high, raw/medium, etc.) and that
        # min_confidence_tier filters on.
        best_sim = max(
            sim_map.get(atom_id, 0.0),
            triple_sim_map.get(atom_id, 0.0),
        )
        tier = _tier_for_similarity(best_sim)
        if not _passes_min_tier(tier, min_confidence_tier):
            continue
        candidates.append(RecallCandidate(
            atom=atom,
            activation=activation,
            similarity=sim_map.get(atom_id, 0.0),
            keyword_score=kw_map.get(atom_id, 0.0),
            rrf_score=rrf_map.get(atom_id, 0.0),
            semantic_rank=semantic_rank_map.get(atom_id, -1),
            keyword_rank=keyword_rank_map.get(atom_id, -1),
            triple_rank=triple_rank_map.get(atom_id, -1),
            triple_similarity=triple_sim_map.get(atom_id, 0.0),
            confidence_tier=tier,
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
    #
    # chainlink #236: thread ``reference_date`` to mark_access so bench
    # replays of historical corpora write access_events with the
    # corpus's epoch instead of the wall clock that ran the bench.
    # Without this, replaying LongMemEval-S (2023 sessions) in 2026
    # writes 2026 timestamps onto 2023-era atoms, corrupting downstream
    # activation reads within the same bench run.
    if fire_access_events:
        events = [
            AccessEvent(atom_id=c.atom["id"], source="retrieval",
                        session_id=session_id)
            for c in surfaced_obs + surfaced_raws
        ]
        if events:
            try:
                conn.execute("BEGIN IMMEDIATE")
                mark_access(conn, events, now=reference_date)
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

        # Encoding-confidence delta from baseline. Atoms that have
        # absorbed dedup duplicates (or, in future, accumulated
        # positive feedback nudges) get a small additive boost. Solo
        # atoms at baseline contribute exactly zero — no spurious
        # ranking shift for the common case. See
        # ``ENCODING_CONFIDENCE_WEIGHT`` docstring for the
        # calibration rationale.
        enc_conf = c.atom.get("encoding_confidence")
        enc_conf_boost = 0.0
        if isinstance(enc_conf, (int, float)):
            enc_conf_boost = ENCODING_CONFIDENCE_WEIGHT * (
                float(enc_conf) - BASELINE_ENCODING_CONFIDENCE
            )

        # Base ranking signal: RRF over FAISS + FTS ranked lists.
        # See DEFAULT_SCORING_WEIGHTS docstring for the w_rrf calibration.
        c.total = (
            weights.get("w_rrf", 20.0)  * c.rrf_score
          + weights["w_topic"]         * c.topic_score
          + weights["w_act"]           * _sigmoid(c.activation - threshold)
          + c.session_boost
          + c.pinned_boost
          + c.trend_modifier
          + enc_conf_boost
          # Evidence boost applied in _apply_evidence_boost (post-split).
          # Supersession/contradiction penalties are TBD.
        )


#: Multiplier applied to each surfaced observation's RRF score when
#: computing the boost it contributes to its evidence raws. Mirrors
#: saga's ``1 / stability_reduction_factor`` (= 2.0 with
#: ``stability_reduction_factor = 0.5``). The theoretical motivation
#: is "consolidation halves the source raws' stability — compensate by
#: 2× on retrieval"; without per-atom stability we keep the constant
#: as a bench-derived tuning value.
OBSERVATION_BOOST_MULTIPLIER = 2.0

#: Per-raw cap on the cumulative boost: a raw's evidence boost cannot
#: exceed ``base × OBSERVATION_BOOST_CAP_RATIO`` so a strongly-endorsed
#: but otherwise-weak raw can't dominate the top-K. Mirrors saga's
#: ``evidence_boost_cap_multiplier = 3.0`` (cap factor is cap-1 since
#: saga's cap is the FINAL score multiplier; we cap the BOOST itself).
OBSERVATION_BOOST_CAP_RATIO = 2.0


def _apply_evidence_boost(
    conn: sqlite3.Connection,
    surfaced_obs: list[RecallCandidate],
    raws: list[RecallCandidate],
    weights: dict[str, float],
) -> None:
    """Surfaced observations lift their evidenced_by raws.

    Per-observation contribution (saga's mechanism):
      ``boost(raw) = OBSERVATION_BOOST_MULTIPLIER × Σ obs.rrf_score``
      over the surfaced observations that endorse this raw.

    Cap:
      ``boost ≤ base × OBSERVATION_BOOST_CAP_RATIO``
      where ``base`` is the raw's RRF-derived total contribution
      (``w_rrf × rrf_score``). Without this a strongly-endorsed-but-
      weakly-ranked raw could leap to the top-K on observation
      endorsement alone.

    Scaling to total-score space: saga's RRF score is the final score
    directly; ours multiplies by ``w_rrf`` (default 20). To keep the
    boost the same *fraction* of base as saga, we multiply the raw
    rrf-space boost by ``w_rrf`` before adding to ``total``.

    No pull-in: only raws already in the candidate pool are boosted.
    Saga's canonical bench has ``enable_endorsed_atom_pull_in = false``
    (P40) — pulling in cheap-path-rejected atoms regressed bench, so
    in-pool-only is the right default.
    """
    if not surfaced_obs:
        return
    # Map each evidenced raw → cumulative boost contribution (in RRF
    # units, pre-scale).
    obs_ids = [c.atom["id"] for c in surfaced_obs]
    placeholders = ",".join(["?"] * len(obs_ids))
    rows = conn.execute(
        f"SELECT source_id, target_id FROM atom_relations "
        f"WHERE source_id IN ({placeholders}) AND relation_type = 'evidenced_by'",
        obs_ids,
    ).fetchall()
    if not rows:
        return
    # obs_id → rrf_score lookup
    obs_score: dict[str, float] = {c.atom["id"]: c.rrf_score for c in surfaced_obs}
    # raw_id → Σ multiplier × obs_score over endorsing surfaced obs
    raw_boost_rrf: dict[str, float] = {}
    for source_id, target_id in rows:
        contrib = OBSERVATION_BOOST_MULTIPLIER * obs_score.get(source_id, 0.0)
        if contrib <= 0:
            continue
        raw_boost_rrf[target_id] = raw_boost_rrf.get(target_id, 0.0) + contrib
    if not raw_boost_rrf:
        return
    w_rrf = weights.get("w_rrf", 20.0)
    for c in raws:
        boost_rrf = raw_boost_rrf.get(c.atom["id"], 0.0)
        if boost_rrf <= 0:
            continue
        # Cap relative to the raw's own base RRF contribution. base in
        # RRF units = c.rrf_score; cap is "boost can at most equal
        # CAP_RATIO × that". A raw with rrf_score=0 has no own base —
        # we still let it accept the boost (set a small floor) so an
        # observation-only-found raw can surface, capped at its
        # ABSOLUTE boost not relative.
        base_rrf = max(c.rrf_score, 1e-6)
        capped_boost_rrf = min(boost_rrf, base_rrf * OBSERVATION_BOOST_CAP_RATIO)
        # Scale to total-score space.
        boost_total = w_rrf * capped_boost_rrf
        c.evidence_boost = boost_total
        c.total += boost_total
