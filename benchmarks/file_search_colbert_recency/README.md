# file_search_colbert_recency — chainlink #141 Slice 2 A/B harness

Three-arm measurement of `file_search` retrieval quality:

| Arm | Channel | `recency_fuse_alpha` | Meaning |
|---|---|---|---|
| A | BM25 + dense only | n/a (legacy weighted-sum) | What ships today without the `colbert` extra |
| B | BM25 + dense + ColBERT (RRF) | 0.0 | PR #184 as shipped |
| C | BM25 + dense + ColBERT (RRF) | 0.3 | PR #184 + Option A recency multiplier |

The ColBERT sidecar is built once and reused for Arms B + C.

## Running

```bash
# One-shot: build colbert index, run 3 arms, write results.json
uv run python benchmarks/file_search_colbert_recency/run_ab.py \
    --home /mimir-home

# Reuse an already-built sidecar (saves ~13min):
uv run python benchmarks/file_search_colbert_recency/run_ab.py \
    --home /mimir-home --skip-build
```

Requires the `colbert` extra installed
(`uv sync --extra colbert`) and an existing
`<home>/.mimir/index.db` populated via `mimir setup` or the
normal ingest path. Voyage API key (or whatever provider
`saga.toml` points at) must be in env.

## Probes

49 probes in `probes.json`. Three categories:

- **path-citation** (30): verbatim from chainlink #140's recon set
  (state/spec/chainlink-138-sub-b-recon.md). General-shape queries
  with one or more expected target paths.
- **colbert-favorable** (~11): rare technical tokens / error
  fingerprints. ColBERT's late-interaction architecture is
  predicted to outperform dense+BM25 here.
- **rare-token** (~8): exact path / PR / chainlink-ID refs. The
  "I know what the file is called, surface it" shape.

## Metric

Hit-rate@10 is the primary metric: does ANY of the probe's
`expected_paths` appear as a substring of ANY top-10 returned
path? MRR@10 (mean reciprocal rank) is the secondary metric, more
sensitive to small ranking shifts within the top-10.

Per-category breakouts let us see if ColBERT's predicted
advantage on rare-token queries actually shows up in the data.

## Output

`results.json` — raw per-probe outcomes for all 3 arms.
`state/spec/chainlink-141-slice2-ab-results.md` — rendered report
with deltas + honest read + recommendation.
