# LongMemEval Benchmark Results

Running log of every graded run so we can compare deltas across upgrades.
Judge is always GPT-4o-2024-08-06 (required for leaderboard comparability).

Dataset: LongMemEval `S` (500 questions) unless noted.
Harness: `msam/benchmarks/longmemeval/` (worktree copy on `hindsight-ideas`).

## Summary table (overall accuracy)

| Run tag | Reader | Fusion | Pathways | Questions | Overall |
|---|---|---|---|---|---|
| `msam_baseline_v0` | MiniMax-M2.7 | weighted_sum | sem + kw | 500 | **0.734** |
| `msam_rrf_v1` | MiniMax-M2.7 | rrf | sem + kw | 500 (498 graded) | **0.799** |
| `msam_rrf_gpt4omini_v1` | gpt-4o-mini | rrf | sem + kw | 500 | **0.728** |
| `pref_probe_max1024` | MiniMax-M2.7 | weighted_sum | sem + kw | 30 (pref only) | **0.333** |
| `msam_p3_minimax_v1` | MiniMax-M2.7 | rrf | sem + kw + graph* + temporal (chrono) | 500 | **0.772** |
| `msam_p3_minimax_v2` | MiniMax-M2.7 | rrf | sem + kw + graph* + temporal (cos-ranked) | 500 | **0.768** |
| `hindsight_rrf_baseline` | gpt-4o-mini | Hindsight TEMPR | 4-way + cross-encoder | 60 | (running) |

\* graph pathway returns `[]` during these runs because `[triples] enable_extraction = false` in `msam_bench.toml`. Unblocked by P7.

## Per-subtype scores

### `msam_baseline_v0` — weighted_sum, MiniMax (500q)
| Subtype | Score | N |
|---|---|---|
| single-session-assistant | 0.964 | 56 |
| single-session-user | 0.943 | 70 |
| knowledge-update | 0.936 | 78 |
| temporal-reasoning | 0.707 | 133 |
| multi-session | 0.549 | 133 |
| single-session-preference | 0.233 | 30 |

### `msam_rrf_v1` — RRF, MiniMax (500q, 498 graded after 2 errors)
| Subtype | Score | N | Δ vs baseline |
|---|---|---|---|
| single-session-assistant | 1.000 | 56 | +3.6 |
| single-session-user | 0.957 | 70 | +1.4 |
| knowledge-update | 0.949 | 78 | +1.3 |
| **temporal-reasoning** | **0.827** | 133 | **+12.0** |
| **multi-session** | **0.634** | 131 | **+8.5** |
| single-session-preference | 0.267 | 30 | +3.4 |

**Overall: 0.734 → 0.799 (+6.6 pp)**. Cleared P2's ≥1 pt bar.

### `msam_rrf_gpt4omini_v1` — RRF, gpt-4o-mini (500q)
| Subtype | Score | N | Δ vs RRF+MiniMax |
|---|---|---|---|
| single-session-assistant | 0.946 | 56 | -5.4 |
| single-session-user | 0.900 | 70 | -5.7 |
| knowledge-update | 0.846 | 78 | -10.3 |
| temporal-reasoning | 0.714 | 133 | -11.3 |
| multi-session | 0.609 | 133 | -2.5 |
| single-session-preference | 0.200 | 30 | -6.7 |

**Overall: 0.799 → 0.728 (-7.1 pp)**. Reader downgrade hurts across the board.
MiniMax-M2.7's `<think>`-budgeted reasoning beats gpt-4o-mini on this task
despite the token-cap overhead. Keep MiniMax for future MSAM runs.

### `msam_p3_minimax_v1` — RRF + graph + temporal, MiniMax (500q)
| Subtype | Score | N | Δ vs RRF+MiniMax (P2) |
|---|---|---|---|
| single-session-assistant | 0.982 | 56 | -1.8 |
| single-session-user | 0.971 | 70 | +1.4 |
| knowledge-update | 0.923 | 78 | -2.6 |
| temporal-reasoning | 0.789 | 133 | -3.8 |
| multi-session | 0.594 | 133 | -4.0 |
| single-session-preference | 0.233 | 30 | -3.4 |

**Overall: 0.799 → 0.772 (-2.7 pp). P3 regresses at default weights.**
Critically, temporal-reasoning *drops* 3.8 pp — the subtype the temporal
pathway was supposed to help. Suspected cause: the temporal pathway ranks
every in-window atom at score 1.0 regardless of semantic relevance, so
RRF over-weights topically-unrelated-but-chronologically-close atoms.
Follow-up work:
1. Score-then-rank within the temporal pathway (cosine on the filtered set)
   instead of chronological order.
2. Lower `rrf_temporal_weight` from 1.0 → 0.3-0.5 by default.
3. Gate pathway activation on window-width (only fire for narrow scopes).
Graph pathway has no effect this run — triples disabled.

### `msam_p3_minimax_v2` — temporal pathway ranks by cosine within window (500q)
| Subtype | Score | N | Δ vs P3v1 | Δ vs P2 (RRF+MiniMax) |
|---|---|---|---|---|
| single-session-assistant | 0.982 | 56 | 0.0 | -1.8 |
| single-session-user | 0.957 | 70 | -1.4 | 0.0 |
| knowledge-update | 0.936 | 78 | +1.3 | -1.3 |
| temporal-reasoning | 0.729 | 133 | -6.0 | -9.8 |
| multi-session | 0.647 | 133 | +5.3 | +1.3 |
| single-session-preference | 0.200 | 30 | -3.3 | -6.7 |

**Overall: 0.768 (-3.1 vs P2, -0.4 vs P3v1).** Fixing the pathway's internal
ordering (chrono → cosine) helped multi-session but hurt temporal-reasoning
even more. Adding any new ranked list to RRF dilutes rank-1 positions from
semantic/keyword even when the new list is "well-ordered," because the top
atoms overlap heavily with what semantic already surfaces. Net effect:
temporal pathway doesn't add new signal, just redistributes RRF mass.

Conclusion: P3's structural hypothesis (more pathways = better) isn't
supported at these weights. Potential rescues:
1. Gate temporal pathway activation on window width (only fire narrow
   scopes where dates actually differentiate candidates).
2. Drop `rrf_temporal_weight` to 0.3 or lower — treat it as a boost
   for narrow-window queries, not a full ranked list.
3. Move temporal handling from "parallel pathway" to "post-filter boost"
   on the semantic list (multiply by an in-window bonus).

### `pref_probe_max1024` — MiniMax, weighted_sum, 1024-token cap (30q, preference only)
Baseline preference score: 7/30 (0.233). Probe: **10/30 (0.333, +10 pp)**.
Raising the reader cap from 512 → 1024 reclaims some budget-truncated
preference answers. 19/30 baseline answers hit the 512 cap; probe still had
several truncations at 1024. Confirms token cap is part (not all) of the
preference gap — the rest is retrieval.

## Notes on errors / caveats

- `msam_rrf_v1` processed 500, saved 498 to hypotheses (2 ingestion errors).
- Hindsight's 60q run uses their own harness and patched `prepare_sessions_for_ingestion` for duplicate-`session_id` questions (Q4, Q5, Q45 in the first 60).
- Hindsight's reported 91.4% is on the same dataset but their prior runs used different reader config; our 60q run recreates under matched reader (gpt-4o-mini).

## File locations

Hypotheses + metrics: `results/longmemeval/hypotheses_<tag>.jsonl` and
`metrics_<tag>.jsonl`. Judge output: same filename + `.eval-results-gpt-4o`.
Full MSAM run logs: `results/longmemeval/<tag>.log`.
Hindsight results JSON: `external/hindsight/hindsight-dev/benchmarks/longmemeval/results/*.json`.

## Update protocol

When a new graded run lands, append:
1. A row to the summary table.
2. A per-subtype section with deltas against the most directly comparable prior run.
3. A note on anything anomalous (reader behavior, rate-limit pattern, errors).
