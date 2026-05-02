"""
Rank fusion for hybrid retrieval.

Reciprocal Rank Fusion (RRF) is an alternative to the weighted-sum blending
used by ``hybrid_retrieve``. RRF works on ranks rather than raw scores, so it
is robust to score-scale differences between pathways (semantic cosine ~[0,1]
vs. BM25 ~[0,50] vs. activation ~[0,100]) that the multiplicative/additive
path has to normalize by hand.

The canonical formula (Cormack, Clarke, Büttcher 2009) is

    rrf_score(doc) = sum_p w_p / (k + rank_p(doc))

where ``rank_p`` is the 1-based rank of ``doc`` in pathway ``p`` (missing
docs contribute zero) and ``k`` is a constant that dampens the contribution
of high ranks. ``k = 60`` is the common default.

This module is intentionally stateless — callers build the per-pathway ranked
lists and hand them in. Wiring into ``hybrid_retrieve`` lives in ``core.py``.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping


def reciprocal_rank_fusion(
    ranked_lists: Mapping[str, Iterable[str]],
    k: int = 60,
    weights: Mapping[str, float] | None = None,
) -> list[tuple[str, float]]:
    """
    Fuse per-pathway ranked lists of atom IDs into a single ranking.

    Args:
        ranked_lists: mapping from pathway name to an ordered iterable of
            atom IDs (best first). Pathways may overlap in IDs.
        k: RRF damping constant. Higher k => high-rank docs contribute less
            and low-rank docs relatively more. 60 is the Cormack default.
        weights: optional per-pathway weight (default 1.0). Useful for biasing
            toward a pathway known to be more reliable on a query type.

    Returns:
        List of ``(atom_id, rrf_score)`` tuples sorted by descending score.
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
