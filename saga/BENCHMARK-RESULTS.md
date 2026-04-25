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
| `msam_p1_minimax_v1` | MiniMax-M2.7 | rrf + obs-bonus | sem + kw (obs enabled, consolidation on) | 500 | **0.720** |
| `msam_p1_minimax_v2` | MiniMax-M2.7 | rrf + obs-bonus | sem + kw (preserve-specifics consolidation prompt) | 500 | **0.692** |
| `msam_p1_minimax_v3` | MiniMax-M2.7 | rrf + obs-bonus | sem + kw (consolidation.enable_llm=false, longest-atom fallback) | 500 | **0.704** |
| `msam_p9_minimax_v1` | MiniMax-M2.7 | rrf + two-tier (P9) | sem + kw (consolidation gpt-5.4-nano, min_cluster_size=2) | 500 (499 graded) | **0.7816** |
| `msam_p9_minimax_v2` | MiniMax-M2.7 | rrf + two-tier (P9) | sem + kw (consolidation gpt-5.4-nano, min_cluster_size=3) | 500 | **0.796** |
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

### `msam_p1_minimax_v1` — observations tier + bonus, MiniMax (500q)

Bench config: `[consolidation] enabled=true` with `min_cluster_size=2`
and `max_clusters_per_run=20`; consolidation LLM = gpt-4o-mini;
`retrieval.enable_observation_bonus = true` (default alpha=0.3).

| Subtype | Score | N | Δ vs P2 (RRF+MiniMax) |
|---|---|---|---|
| single-session-assistant | 0.964 | 56 | -3.6 |
| single-session-user | 0.900 | 70 | -5.7 |
| knowledge-update | 0.872 | 78 | -7.7 |
| temporal-reasoning | 0.654 | 133 | **-17.3** |
| multi-session | 0.571 | 133 | -6.3 |
| **single-session-preference** | **0.400** | 30 | **+13.3** |

**Overall: 0.720 (-7.9 pp vs P2).** Only preference improved — a clean
"user prefers X" observation beats scattered raw evidence for rubric-
style questions, consistent with the P1 thesis. Everything else
regressed, hard.

Likely cause: with `min_cluster_size=2` and a fresh per-question DB,
consolidation produces ~20 LLM-synthesized observations per question.
The evidence-count bonus pulls them into top-20 retrieval even when
the original atoms carry more precise evidence (dates, exact quotes).
The reader gets summaries instead of primary evidence — fatal for
temporal-reasoning which lost 17 pp.

Paths to rescue P1:
1. Tighten consolidation: raise `min_cluster_size` to 4–5 so only
   high-confidence observations form. Fewer observations, less
   displacement of primary evidence.
2. Lower bonus alpha from 0.3 to 0.1 — observations only tie-break,
   don't dominate.
3. Hybrid retrieval contract change: return top-K raws AND include
   observations as a separate tier in the reader prompt, instead of
   boosting observations into the raw top-K. Closer to Hindsight's
   "observations surface first, raw atoms as evidence" design.
4. Gate the bonus on query type — preference-ish queries get it,
   factual/temporal queries skip it.

### `msam_p1_minimax_v2` — consolidation prompt v2 (preserve dates, numbers, named entities), MiniMax (500q)

Prompt changed from "synthesize a concise summary" to a version that
requires verbatim preservation of dates, times, numbers, names, direct
quotes — and explicitly keeps both versions when atoms disagree. All
other settings match P1v1.

| Subtype | Score | N | Δ vs P1v1 | Δ vs P2 |
|---|---|---|---|---|
| single-session-assistant | 0.964 | 56 | 0.0 | -3.6 |
| single-session-user | 0.857 | 70 | -4.3 | -10.0 |
| knowledge-update | 0.872 | 78 | 0.0 | -7.7 |
| **temporal-reasoning** | **0.684** | 133 | **+3.0** | -14.3 |
| multi-session | 0.504 | 133 | -6.7 | -13.0 |
| **single-session-preference** | **0.200** | 30 | **-20.0** | -6.7 |

**Overall: 0.692 (-2.8 vs P1v1, -10.7 vs P2).**

The prompt fix did what it was designed to do — temporal-reasoning
recovered 3 pp because dates now survive consolidation. But it broke
preference hard (-20 pp): preference queries benefit from *abstract*
observations ("user prefers Sony gear") synthesized out of scattered
specific episodes, and the preserve-specifics prompt kills that
abstraction. V1's preference gain came from the opposite of what v2
asks for.

Real tension uncovered: **consolidation abstraction helps preference,
hurts temporal; literal preservation helps temporal, kills preference.**
One prompt can't do both. The architectural response is probably
query-type-aware consolidation, or routing different memory_type
variants ("abstract_observation" vs "literal_observation") — closer
to Hindsight's disposition-aware reflect than to our current single-
tier observation design.

For now: P1 at default settings is net-negative on LongMemEval across
both prompt variants. Schema + wiring are useful for future work
(production deployments with real outcome loops can still benefit),
but we should stop defaulting `enable_observation_bonus = true` until
we have a design that doesn't trade one subtype for another.

### `msam_p1_minimax_v3` — LLM off in consolidation, longest-atom fallback (500q)

Bench config: `consolidation.enable_llm = false`. The fallback path
picks the longest atom in each cluster, prefixes with "[Consolidated
from N atoms]", and stores it as an observation with the usual
evidence_count bonus at retrieval time. Consolidation completes in
~10s per question (vs 40-60s with the LLM).

| Subtype | Score | N | Δ vs P1v1 | Δ vs P1v2 | Δ vs P2 |
|---|---|---|---|---|---|
| single-session-assistant | 0.929 | 56 | -3.5 | -3.5 | -7.1 |
| **knowledge-update** | **0.949** | 78 | **+7.7** | **+7.7** | **0.0** |
| single-session-user | 0.900 | 70 | 0.0 | +4.3 | -5.7 |
| multi-session | 0.511 | 133 | -6.0 | +0.7 | -12.3 |
| temporal-reasoning | 0.662 | 133 | +0.8 | -2.2 | -16.5 |
| single-session-preference | 0.233 | 30 | -16.7 | +3.3 | -3.4 |

**Overall: 0.704 (+1.2 vs v2, -1.6 vs v1, -9.5 vs P2).**

No-LLM recovered knowledge-update to P2 levels — literal content
survives intact, no LLM distortion. But it lost all of v1's preference
gain because the fallback doesn't actually synthesize anything; it
just re-tags the longest atom. That helps factual queries but gives
preference nothing to lean on.

## P1 summary: three variants, all regressed

Three P1 variants tried, each trading subtypes against others:

- v1 (LLM, abstract prompt): preference +13, temporal -17
- v2 (LLM, preserve-specifics prompt): temporal +3 vs v1, preference -20
- v3 (no LLM, longest-atom fallback): knowledge-update back to P2 (0.949),
  preference back to baseline (0.233)

None beat P2 (0.799). The issue isn't the prompt or the consolidation
model — it's that a single merged top-K forces observations and raws
to compete for the same slots, and consolidation halves source
stability regardless of whether the resulting observation is any good.

Response: P9 in HINDSIGHT-IDEAS.md — two-tier retrieval with
observation→raw evidence boost. Observations and raws are RRF-ranked
independently; when an observation surfaces it lifts its backing
evidence atoms in the raw tier by (1 / stability_reduction) × obs_RRF.
Reader prompt presents both as labeled blocks so preference queries
lean on observations, temporal queries lean on raws. Expected to land
the preference lift without sacrificing temporal/multi-session.

### `msam_p9_minimax_v1` — two-tier retrieval (P9), MiniMax reader, gpt-5.4-nano consolidation (500q)

Bench config: `retrieval.two_tier_enabled = true`, `observations_top_k = 5`,
`observation_confidence_min_sim = 0.30`, `evidence_boost_cap_multiplier = 3.0`,
`consolidation.enable_llm = true` with gpt-5.4-nano,
`consolidation.min_cluster_size = 2`. Reader = MiniMax-M2.7.

| Subtype | Score | N | Δ vs P1v1 | Δ vs P2 |
|---|---|---|---|---|
| **knowledge-update** | **0.962** | 78 | +9.0 | **+1.3** |
| single-session-assistant | 0.964 | 56 | 0.0 | -3.6 |
| single-session-user | 0.928 | 69 | +2.8 | -2.9 |
| multi-session | 0.632 | 133 | +6.1 | -0.2 |
| temporal-reasoning | 0.759 | 133 | +10.5 | -6.8 |
| **single-session-preference** | **0.400** | 30 | 0.0 | **+13.3** |

**Overall: 0.7816 (-1.7 vs P2, +6.2 vs P1v1).**

The architecture works:
- **Preference** recovered to P1v1's gain — the two-tier design captures
  the +13.3 pp preference lift abstract observations provide.
- **Multi-session** essentially held at P2 levels (+0.2 within noise).
  P1v1 had pulled this down -6.3; P9 gives it back.
- **Knowledge-update** actually beat P2 (+1.3 pp). Observations help
  with "what does the user know about X" without hurting factual recall
  because the raws tier preserves specifics.

Only loss is **temporal-reasoning** (-6.8 pp vs P2). Reader is being
distracted by observations on time-sensitive questions even when the
raws contain the precise dates. 5 obs + 20 raws = 25 items in prompt
vs P2's 20 raws — small attention dilution.

Observation fire rates from the run:
- single-session-preference: 77% (matches design intent)
- multi-session: 56%
- temporal-reasoning: 46%
- single-session-user: 22%
- knowledge-update / assistant: similar

Errors: 1 (Q `8a137a7f` lost to OpenAI 500 on embeddings).
Pace: ~37s/q with gpt-5.4-nano consolidation (vs ~110s with MiniMax).

### `msam_p9_minimax_v2` — same as v1 but min_cluster_size=3 (overnight)

Reasoning:
- v1 hit the 20-cluster cap on most questions with min=2, meaning many
  small "two atoms happen to cosine-cluster" pair observations that
  add noise.
- min=3 reduces total observation count (less fire on noisy queries,
  particularly temporal where firing currently hurts) and bumps the
  average evidence_count (size-3 clusters = stronger signal per obs).
- Preset plan called for min=3 + lowered sim_floor=0.25, but lowering
  the floor would surface MORE observations including on temporal
  queries — wrong direction given temporal is the only weak subtype.
  Going with min=3 alone.

### `msam_p9_minimax_v2` — same as v1 but `min_cluster_size=3` (500q)

| Subtype | Score | N | Δ vs P9v1 (min=2) | Δ vs P2 |
|---|---|---|---|---|
| single-session-user | 0.986 | 70 | +5.7 | **+2.9** |
| single-session-assistant | 0.982 | 56 | +1.8 | -1.8 |
| **multi-session** | **0.669** | 133 | +3.6 | **+3.5** |
| knowledge-update | 0.949 | 78 | -1.3 | 0.0 |
| temporal-reasoning | 0.774 | 133 | +1.5 | -5.3 |
| **single-session-preference** | 0.267 | 30 | **-13.3** | 0.0 |

**Overall: 0.796 (+1.4 vs v1, -0.3 vs P2 — within noise).**

Bumping `min_cluster_size` from 2 → 3 traded the preference gain for
across-the-board lifts on every other subtype:

- **Preference** lost the +13.3 pp gain — patterns like "user prefers
  Sony" often appear across just 2-3 atoms and the size-3 floor filters
  out the smaller pattern clusters.
- **Multi-session** gained +3.6 vs v1 and now beats P2 by +3.5.
  Higher-evidence observations match cross-session questions better.
- **Single-session-user** jumped +5.7 vs v1, beating P2 by +2.9.
- **Temporal-reasoning** recovered +1.5 vs v1 (still -5.3 vs P2 — the
  reader-distraction issue from observations isn't fully solved).

Cluster size effect on consolidation cost: cluster counts dropped from
hitting the 20-cap on most v1 questions to ~7-12 on v2, confirming
~50-80% of v1's clusters were size-2 noise. Cons time fell from
~22s/q to ~12s/q.

## P9 summary

Two variants tried; both essentially tie P2 within ±2 pp overall but
with very different subtype profiles:

- **P9v1 (min=2)**: keeps P1's preference gain (+13.3), basically holds
  multi-session at P2.
- **P9v2 (min=3)**: loses preference gain, but lifts multi-session
  (+3.5), single-session-user (+2.9), and partially recovers temporal.

Neither beats P2 overall by enough to call it a clear win. The
architecture is sound — observations help where they should help — but
the cluster-size knob fundamentally trades subtypes against each
other, and on this benchmark the trade comes out roughly even.

If we wanted to keep tuning, paths forward:
1. **Per-stream `min_cluster_size`** — preference-rich streams (episodic
   user statements) keep min=2; factual streams use min=3+.
2. **Lower `observations_top_k`** for non-preference questions —
   reduces reader distraction on temporal queries.
3. **Tag observations with their dominant subtype hint** — let the
   reader weigh them differently.

But those are tunings for a +1-2 pp regime. The architectural lesson
is more useful than the score: P9 successfully isolated abstraction
from raw evidence, removed the P1-style stability-halving harm, and
made consolidation a benefit-or-neutral on this bench rather than the
net-negative P1 was. Good shape to ship.

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
