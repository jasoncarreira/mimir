"""chainlink #141 Slice 2 A/B harness.

Three arms measuring file_search retrieval quality:

- Arm A — legacy BM25 + dense weighted-sum (ColBERT channel disabled)
- Arm B — three-channel RRF fusion (PR #184 as shipped, alpha=0)
- Arm C — three-channel RRF fusion + post-RRF recency multiplier (alpha=0.3)

Probes target three categories: 'path-citation' (verbatim from chainlink
#140's recon set), 'colbert-favorable' (rare tech tokens / fingerprints),
and 'rare-token' (exact path / PR / chainlink-ID refs).

Results land at state/spec/chainlink-141-slice2-ab-results.md.
"""
