"""Reciprocal Rank Fusion for hybrid retrieval.

Ported from saga.retrieval_fusion (Cormack, Clarke, Büttcher 2009).
Canonical formula:

    rrf_score(doc) = sum_p w_p / (k + rank_p(doc))

where ``rank_p`` is the 1-based rank of ``doc`` in pathway ``p`` (missing
docs contribute zero) and ``k`` is a damping constant. ``k=60`` is the
Cormack default; saga's bench has been on k=60 since v0.

Why RRF over weighted-sum (the previous recall.py default):

- RRF works on **ranks**, not raw scores. FAISS cosine ~[0,1] vs.
  FTS5/BM25 ~[0,50] vs. activation contributions need careful per-
  pathway scaling under weighted-sum. RRF doesn't care.
- A doc absent from a pathway contributes zero to that pathway's
  score — no need for per-pathway "missing-value" defaults.
- Per-pathway weights are still available (``weights``) for biasing
  toward a pathway known to be more reliable on a query type, but
  they multiply a normalized rank contribution, not a raw score.

Saga's canonical bench (saga_bench.toml line 28) has used
``fusion = "rrf"`` with ``rrf_k = 60`` and equal pathway weights
since the very first run. We default to the same.

Stateless module — callers build the per-pathway ranked lists and
hand them in. Wiring into recall.py happens there.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping


# Cormack default. Higher k → high-rank docs contribute less and
# low-rank docs relatively more. Lower k → top of each list dominates.
DEFAULT_K = 60


def reciprocal_rank_fusion(
    ranked_lists: Mapping[str, Iterable[str]],
    *,
    k: int = DEFAULT_K,
    weights: Mapping[str, float] | None = None,
) -> list[tuple[str, float]]:
    """Fuse per-pathway ranked lists of atom IDs into a single ranking.

    Args:
        ranked_lists: mapping from pathway name (e.g. ``"semantic"``,
            ``"keyword"``) to an ordered iterable of atom IDs, best first.
            Pathways may overlap.
        k: RRF damping constant.
        weights: optional per-pathway weight (default 1.0).

    Returns:
        ``[(atom_id, rrf_score)]`` sorted by descending score.
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    weights = weights or {}
    scores: dict[str, float] = defaultdict(float)
    for pathway, ids in ranked_lists.items():
        w = weights.get(pathway, 1.0)
        for rank, atom_id in enumerate(ids):
            scores[atom_id] += w / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])
