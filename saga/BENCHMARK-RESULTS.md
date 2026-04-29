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
| `msam_p9_minimax_v3` | MiniMax-M2.7 | rrf + two-tier (P9) | sem + kw (per-stream cluster floors, obs_top_k=3, obs_sim≥0.35, per-stream prompts) | 500 | **0.760** |
| `msam_p8_minimax_v1` | MiniMax-M2.7 | rrf + two-tier (P9v2) + cluster merge pass (P8) + session_boundaries + mark_contributions (P10) | sem + kw (merge_threshold=0.75, max_cluster_size=50) | 500 | **0.752** |
| `msam_p8_minimax_v2` | MiniMax-M2.7 | P8v1 stack with tighter merge knobs | sem + kw (merge_threshold=0.85, max_cluster_size=15) | 500 | **0.758** |
| `msam_p4_minimax_v1` | MiniMax-M2.7 | P9v2 + P4-bench (contradiction→supersedes→demotion); P10 disabled | sem + kw (supersedes_resolution_threshold=0.85, supersedes_score_multiplier=0.4) | 500 | **0.766** |
| `msam_p30_minimax_v1` | MiniMax-M2.7 | P9v2 + P30 (missing-atom base score in two-tier); atom-level supersedes off; obs-level supersedes on | sem + kw (additive boost + P30 cosine-based missing-atom pull-in) | 500 | **0.780** |
| `msam_p30_minimax_v2` | MiniMax-M2.7 | P30v1 stack with **flat 2× restoration** replacing the additive boost; per-atom confidence filtering | sem + kw (raws endorsed by surfaced obs get score×2; no additive boost) | 500 | **0.756** |
| `msam_p30_minimax_v3` | MiniMax-M2.7 | P30v1 stack reinstated (additive boost) + per-atom confidence filtering retained from P30v2 | sem + kw (additive boost + P30 cosine-based missing-atom pull-in; per-atom tiers) | 500 | **0.784** |
| `msam_cherrypicks_minimax_v1` | MiniMax-M2.7 | P30v3 + P11/P12/P13 cherry-picks (query rewriting + synonym expansion + atom quality multiplier), all on | sem + kw (P11 rewriting on both pathways; P12 synonyms on keyword only; P13 quality ×0.5/×1.1 multiplier) | 500 | **0.756** |
| `msam_cherrypicks_off_gptoss_v1` | **gpt-oss-120b**¹ | P30v3 mechanism (cherry-picks reverted to off); reader + MSAM consolidation + judge ALL on `openai/gpt-oss-120b` via OpenRouter | sem + kw (P30v3 stack — additive boost + per-atom tiers + missing-atom pull-in) | 500 | **0.668** |
| `msam_p32_gptoss_v1` | **gpt-oss-120b**¹ | P30v3 + P32 (triple extraction via P7 batched, graph pathway joins RRF as 4th ranker) | sem + kw + **graph** (triples populated per-question via batch_extract_and_store) | 500 | **0.646** |
| `msam_rerank_gptoss_v1` | **gpt-oss-120b**¹ | P30v3 + P15 (cross-encoder LLM rerank on top-8 candidates); triples + graph pathway off | sem + kw, then LLM rerank on top-8 | 500 | **0.648** |
| `msam_p35_canon_v1` | MiniMax-M2.7 | P30v3 + P35 (consolidation as structured-cognition pass: observation + triples in one LLM call); graph pathway on; cherry-picks off; rerank off | sem + kw + **graph** (triples-as-byproduct of consolidation, not separate extraction pipeline) | 500 | **0.758** |
| `msam_p11_canon_v1` | MiniMax-M2.7 | P30v3 + cherry-pick P11 ONLY (`enable_query_rewriting=true`); P12/P13 off | sem + kw (P11 rewriting on both pathways via built-in `_QUERY_REWRITES`) | 498 (2 errors) | **0.771** |
| `msam_p12_canon_v1` | MiniMax-M2.7 | P30v3 + cherry-pick P12 ONLY (`enable_query_expansion=true`); P11/P13 off | sem + kw (P12 synonym expansion on keyword pathway only via 29-key default synonym dict) | 499 (1 error) | **0.792** ✓ |
| `msam_p13_canon_v1` | MiniMax-M2.7 | P30v3 + cherry-pick P13 ONLY (`enable_quality_filter=true`); P11/P12 off | sem + kw (P13 atom-quality multiplier ×0.5/×1.1 on raws and observations after RRF) | 500 | **0.758** |
| `msam_p34_canon_v1` | MiniMax-M2.7 | P30v3 mechanism + `[consolidation] similarity_threshold = 0.75` (was 0.80); cherry-picks off; triples off | sem + kw (lower clustering threshold → more clusters → more observations) | 500 | **0.772** |
| `msam_p36_canon_v1` | MiniMax-M2.7 | P35 features ON (triples + graph pathway) + `[retrieval] rrf_graph_weight = 0.3` (was 0.7) | sem + kw + **graph at lower fusion weight** (graph as tiebreaker rather than co-equal vote) | 500 | **0.760** |
| `hindsight_rrf_baseline` | gpt-4o-mini | Hindsight TEMPR | 4-way + cross-encoder | 60 | (running) |

¹ Indicative-only result. Reader, MSAM's consolidation LLM, and judge were
all switched to gpt-oss-120b for this run. Direct comparison to MiniMax /
gpt-4o-judged runs above is not strictly apples-to-apples; absolute numbers
shift, deltas-vs-baseline within this same configuration would be.

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

### `msam_p9_minimax_v3` — per-stream cluster floors + tighter obs gating + per-stream prompts (500q)

Four tunings stacked on top of v2 to try to recover the v1 preference
gain without losing v2's multi-session lift:

1. `min_cluster_size_episodic = 2`, `min_cluster_size = 3` (others) —
   episodic preference patterns cluster at 2; semantic/procedural keep
   the 3-floor noise filter.
2. `observations_top_k = 5 → 3` — fewer observations in the reader
   prompt, less distraction on factual queries.
3. `observation_confidence_min_sim = 0.30 → 0.35` — observations only
   surface when they're strongly relevant.
4. Per-stream consolidation prompts — episodic abstracts the pattern
   ("user prefers X"); semantic/procedural preserves specifics
   verbatim (dates, numbers, named entities).

| Subtype | Score | N | Δ vs P9v2 | Δ vs P2 |
|---|---|---|---|---|
| single-session-assistant | 1.000 | 56 | +1.8 | 0.0 |
| single-session-user | 0.929 | 70 | -5.7 | -2.9 |
| knowledge-update | 0.936 | 78 | -1.3 | -1.3 |
| **multi-session** | 0.639 | 133 | **-3.0** | +0.5 |
| **temporal-reasoning** | 0.707 | 133 | **-6.7** | **-12.0** |
| **single-session-preference** | 0.233 | 30 | -3.4 | -3.4 |

**Overall: 0.760 (-3.6 vs P9v2, -3.9 vs P2).** Regressed across nearly
every subtype except single-session-assistant. The tunings worked
*against* each other rather than additively:

- Episodic `min=2` was supposed to recover preference, but preference
  *slipped* slightly (0.267 → 0.233). Likely cause: the abstract
  episodic prompt produces observations like "user prefers concise
  responses" that don't lexically match preference probe wording,
  hurting retrieval even though the cluster forms.
- The tighter obs gating (top_k 5→3, min_sim 0.30→0.35) starved
  multi-session, which was P9v2's main win. Multi-session lost -3.0
  exactly as expected — fewer observations, less coverage.
- Temporal-reasoning lost -6.7 vs v2 and is now -12.0 vs P2. The new
  episodic abstraction prompt is the most plausible culprit: temporal
  questions need verbatim dates from raw atoms, and abstracted episodic
  observations may have leaked into the semantic top-K via the
  evidence-boost edge, displacing primary date evidence.

In short: each tuning was individually defensible; together they
compounded into a net loss. P9v2 is still the best P9 variant.

### `msam_p8_minimax_v1` — P9v2 + cluster merge pass + P10 wiring (500q)

P9v2 baseline (the best P9 variant) + P8 merge pass + P10 session
boundary writes during ingest + mark_contributions after each probe.
Two new things compared to any prior P9 run:

1. **Cluster merge pass.** Centroid-distance agglomerative pass after
   greedy clustering (merge_threshold=0.75, max_cluster_size=50).
   Combines clusters fragmented by greedy walking's "A near B, B near
   C, but A not near C" pattern. Result is fewer, broader observations
   with higher evidence counts.
2. **P10 wiring.** Each haystack session gets one episodic
   session_boundary atom written during ingest with summary "<date>:
   conversation with N user turns, M assistant turns" (40-52 boundary
   atoms per question on top of ~470-540 turn atoms). After each probe,
   mark_contributions runs against the response.

| Subtype | Score | N | Δ vs P9v2 | Δ vs P2 |
|---|---|---|---|---|
| single-session-assistant | 0.929 | 56 | -5.4 | -7.1 |
| single-session-user | 0.943 | 70 | -4.3 | -1.4 |
| knowledge-update | 0.910 | 78 | -3.8 | -3.9 |
| temporal-reasoning | 0.782 | 133 | +0.8 | -4.5 |
| **multi-session** | 0.571 | 133 | **-9.7** | **-6.2** |
| single-session-preference | 0.233 | 30 | -3.4 | -3.4 |

**Overall: 0.752 (-4.4 vs P9v2, -4.7 vs P2).** Regressed on five of
six subtypes. The collapse is concentrated in **multi-session
(-9.7)** — exactly the subtype P9v2 was winning vs P2 (+3.5).

What likely went wrong:

- **The merge pass produced observations that are too broad.** With
  merge_threshold=0.75 and max_cluster_size=50, three or four related
  clusters now collapse into a single observation covering 15-20+
  atoms. The synthesis is forced to abstract over a wider topic span,
  producing more general statements that match fewer specific probes.
  Multi-session questions especially need observations that recall
  specific cross-session details — broader observations dilute that
  signal.
- **P10's session_boundary atoms compete for retrieval slots.** ~45
  extra episodic atoms per question. With a top_k=20 raws cap, the
  boundary beacons (which contain only "N user turns, M assistant
  turns" — no semantic content) can crowd out genuinely relevant
  raw atoms. Suspect this is part of the across-the-board single-session
  drop too.

Confounders: P10 + P8 landed in the same run. We can't cleanly
attribute the loss to one or the other. Two follow-up experiments to
disambiguate:

1. **P8-only ablation:** turn off P10 (skip session_boundary writes
   and mark_contributions), keep P8 merge. If this recovers most of
   the loss, P10 was the main culprit.
2. **Tighter merge:** drop max_cluster_size to 15-20 and raise
   merge_threshold to 0.85. Limits the runaway-merge case while
   keeping the fragment-fix benefit.

Pace: ~50s/q (vs P9v2's ~38s/q). The slowdown is from session_boundary
embedding calls during ingest — each boundary goes through `store_atom`
without a pre-computed embedding, so each hits the OpenAI API solo
instead of being batched.

### `msam_p8_minimax_v2` — P8v1 stack with tighter merge knobs (500q)

Same code, same P10 wiring, only two config changes:
- `merge_threshold` 0.75 → 0.85
- `max_cluster_size` 50 → 15

Hypothesis: tighter knobs limit the runaway-merge that produced
over-broad observations in P8v1, especially the multi-session
collapse.

| Subtype | Score | N | Δ vs P8v1 | Δ vs P9v2 | Δ vs P2 |
|---|---|---|---|---|---|
| single-session-assistant | 0.929 | 56 | 0.0 | -5.4 | -7.1 |
| single-session-user | 0.943 | 70 | 0.0 | -4.3 | -1.4 |
| **knowledge-update** | 0.923 | 78 | +1.3 | -2.6 | -2.6 |
| **temporal-reasoning** | 0.729 | 133 | **-5.3** | **-4.5** | **-9.8** |
| **multi-session** | 0.639 | 133 | **+6.8** | -3.0 | +0.5 |
| single-session-preference | 0.233 | 30 | 0.0 | -3.4 | -3.4 |

**Overall: 0.758 (+0.6 vs P8v1, -3.8 vs P9v2, -4.1 vs P2).**

The tighter knobs partly worked: **multi-session recovered 6.8 pp**
and now matches P2 (the prior best on that subtype). Knowledge-update
also nudged up. But the gain came at temporal-reasoning's expense —
it dropped 5.3 pp vs P8v1, the run that *had* the over-broad merge
problem.

Why temporal regressed under tighter merge:

- Tighter `merge_threshold=0.85` means fewer merges happen, so we
  end up with **more clusters total** (each smaller). The bench cap
  of 20 post-merge clusters fills up faster.
- Each surviving observation has lower `evidence_count` (fewer atoms
  per cluster) and represents a narrower topic.
- Temporal-reasoning queries need specific dates from raw atoms.
  The two-tier retrieval pulls observations into the prompt
  alongside raws. With more observations in the prompt (via
  evidence_boost edges), the reader sees more abstracted statements
  and fewer raw `[YYYY-MM-DD ...]` lines competing for attention.
  Net: dates get diluted.

So this isn't a clean win — it's a subtype trade. P8v1 traded
multi-session for everything else; P8v2 trades temporal for
multi-session. Neither beats P9v2 overall.

**The merge pass appears to be the wrong intervention on this
benchmark.** Greedy clustering's fragmentation isn't actually
hurting us — clusters are coherent enough as-is. Adding a merge
layer just shifts where observations help and where they hurt,
without producing a net lift.

P10 wiring is still in this run too; it's the load-bearing
unknown. To isolate, we'd need a P10-only run (P9v2 + P10 wiring,
no merge). That would tell us whether the session_boundary atoms
themselves are dragging single-session subtypes — they all dropped
~5 pp vs P9v2 in both P8v1 and P8v2, which is suspicious.

### `msam_p4_minimax_v1` — P9v2 + P4-bench supersedes resolution (500q)

P9v2 baseline + per-question contradiction-to-supersedes resolution
between consolidation and retrieval, plus retrieval-side multiplicative
demotion (0.4×) for any raw atom marked as the target of a `supersedes`
edge from another candidate. P10 wiring (session boundaries, mark
contributions) **disabled** in this run to isolate the P4-bench effect.

Supersedes signal observed across the run:
- single-session-user: 23% of questions wrote ≥1 edge, avg 0.9/q
- single-session-preference: 27%, avg 0.6/q
- multi-session: 46%, avg 1.5/q (strongest signal as expected — cross-
  session preference flips)
- temporal-reasoning: 36%, avg 0.9/q
- knowledge-update: ~50% (later in the run)

| Subtype | Score | N | Δ vs P9v2 | Δ vs P2 |
|---|---|---|---|---|
| single-session-assistant | 1.000 | 56 | +1.8 | 0.0 |
| **single-session-user** | 0.957 | 70 | -2.9 | 0.0 |
| **knowledge-update** | 0.936 | 78 | **-1.3** | -1.3 |
| **temporal-reasoning** | 0.707 | 133 | **-6.7** | **-12.0** |
| multi-session | 0.647 | 133 | -2.2 | +1.3 |
| single-session-preference | 0.233 | 30 | -3.4 | -3.4 |

**Overall: 0.766 (-3.0 vs P9v2, -3.3 vs P2).** Regressed on five of
six subtypes. Most damaging: **temporal-reasoning -6.7pp**.

Why this regressed:

- **The supersedes mechanism is too aggressive for time-aware queries.**
  A typical pattern: user said "Alex started at Acme" in May, "Alex
  moved to Beta" in November. The November atom is newer, so P4 writes
  a `supersedes` edge from November → May. Retrieval demotes the May
  atom by 0.4×. For queries about *current* state ("Where does Alex
  work?"), this is fine. For queries about *historical* state ("Where
  did Alex work in May?"), the May atom is the correct answer — and
  it's been demoted out of top-K.
- **Knowledge-update was the prime target and didn't move.** The
  hypothesis was that supersedes would help "user changed their mind"
  questions by demoting the older state. Instead it stayed flat
  (-1.3pp). Likely because the LongMemEval reader is already good at
  picking the most-recent dated turn from chronological evidence —
  supersedes was solving a problem the reader already handled.
- **Temporal-reasoning is the exact wrong subtype to apply blanket
  supersession to.** It depends on time-stratified retrieval, and we
  collapsed the time dimension by demoting older atoms uniformly.

Cost: ~44s/q (vs P9v2's ~38s/q). The supersedes resolution adds ~13s
per question (FAISS-less brute force pairwise cosine inside topic
groups, in `find_semantic_contradictions`). P14 in NEXT-EXPERIMENTS.md
proposes optimizing this; given the negative result, optimization is
no longer warranted unless we redesign the demotion to be query-aware.

**The architectural lesson:** supersession is a global tag, but
retrieval is query-dependent. An atom that's superseded for "current
state" queries is still primary evidence for "state at time X" queries.
The retrieval-side demotion can't tell which query it's serving, so
it applies uniformly — and that's wrong on this benchmark.

Possible follow-ups (not yet tried):
1. **Query-type detection** — only apply supersedes demotion when the
   query implies "current state" (no temporal scope detected). Reuses
   `retrieval_v2.detect_temporal_scope()`. This effectively narrows the
   demotion to the "user changed their mind" case where it was
   designed to help.
2. **Lighter demotion** — multiplier 0.7 or 0.8 instead of 0.4. Lets
   superseded atoms still appear in top-K when nothing else is more
   relevant.
3. **Drop the demotion entirely** — keep the supersedes edges as a
   diagnostic / metadata tag, but don't penalize scores. The reader
   can be told "this atom was superseded by Y on date Z" via the
   prompt and decide for itself.

P9v2 (0.796) remains the best configuration. P4-bench is a clear
regression on this benchmark and should not ship as default.

### `msam_p30_minimax_v1` — P9v2 + P30 missing-atom base score fix (500q)

P9v2 baseline + P30 (asymmetric missing-atom scoring fix) + atom-level
supersedes auto-triggers disabled (commit `591e48a`) + observation-level
supersedes demotion in two-tier (kept). Mechanism in this run: additive
boost on in-pool raws + cosine-derived base score for missing endorsed
raws (P30's original implementation).

| Subtype | Score | N | Δ vs P9v2 | Δ vs P2 |
|---|---|---|---|---|
| **single-session-preference** | **0.367** | 30 | **+10.0** | **+10.0** |
| single-session-assistant | 0.982 | 56 | 0.0 | -1.8 |
| single-session-user | 0.957 | 70 | -2.9 | 0.0 |
| knowledge-update | 0.923 | 78 | -2.6 | -2.6 |
| temporal-reasoning | 0.752 | 133 | -2.2 | -7.5 |
| multi-session | 0.639 | 133 | -3.0 | +0.5 |

**Overall: 0.780 (-1.6 vs P9v2, -1.9 vs P2).** Net regression, but the
subtype profile is the most interesting we've seen:

- **Preference jumped +10.0pp** — to **0.367**, the highest preference
  score ever logged on this bench. P9v2 / P2 / baseline were all stuck
  at 0.267, and P9v1 hit 0.400 but lost everything else. P30 cracked
  preference without P9v1's losses.
- **Other subtypes dropped 2-3pp uniformly.** No subtype-specific
  breakage; just a moderate, broad cost.

The trade is: P30's mechanism pulls in evidence atoms that didn't make
the candidate top-K (with cosine-similarity-derived base + boost). On
preference questions, those missing-but-endorsed raws are exactly the
ones the reader needs (specific user statements that didn't lexically
match the probe but are evidence for a preference observation that did
match). On other subtypes — especially temporal and knowledge-update —
the missing atoms displace better-ranked direct evidence, so the
score drops.

Note that this run used the **additive boost + P30 missing-atom
pull-in** mechanism (commits up to `7049e0f`). The next bench will
measure the **flat 2× restoration** variant (commit `fa365a0`),
which removes the obs_score-dependent magnitude and prevents weak
raws from being inflated above their relevance.

Pace: ~28s/q (vs P9v2's ~38s/q). The supersedes resolver is gone, so
each question runs ~10s faster.

### `msam_p30_minimax_v2` — P30v1 stack with flat 2× restoration (500q)

Replaced the additive boost (`base + min(2 × obs_score, 2 × base)`) with
a flat `base × 2` restoration applied to raws endorsed by surfaced
observations. Same missing-atom pull-in (P30 cosine-based base). Same
obs-tier supersedes demotion. Same atom-level supersedes disabled.

| Subtype | Score | N | Δ vs P30v1 | Δ vs P9v2 |
|---|---|---|---|---|
| single-session-assistant | 1.000 | 56 | +1.8 | +1.8 |
| **single-session-preference** | **0.200** | 30 | **-16.7** | **-6.7** |
| single-session-user | 0.929 | 70 | -2.9 | -5.7 |
| knowledge-update | 0.923 | 78 | 0.0 | -2.6 |
| temporal-reasoning | 0.714 | 133 | -3.8 | -6.0 |
| multi-session | 0.632 | 133 | -0.8 | -3.7 |

**Overall: 0.756 (-2.4 vs P30v1, -4.0 vs P9v2).**

The flat restoration is **strictly worse** than the additive boost. The
clearest signal is preference: P30v1 scored 0.367 (the highest preference
ever logged on this bench); P30v2 collapsed to 0.200, **worse than the
0.233 baseline.**

Why: preference probes are exactly the case where the additive boost
mattered. A weak raw with sim=0.18 (low tier) endorsed by a strong
observation got significantly lifted by `base + 2×obs_score` —
typically pushing it from ~0.025 to ~0.10 or so. With flat restoration,
that same atom only gets `0.025 × 2 = 0.05`, half of what additive
delivered. Top-K rankings shift accordingly: weak-but-endorsed atoms
fall out of top-20 under flat restoration, taking preference answers
with them.

Other subtypes saw smaller losses (-3 to -6 pp), consistent with the
general "less-aggressive boost = less endorsement-driven retrieval" story.

**Decision: revert the flat restoration. The additive boost (P30v1's
mechanism) is the canonical model.** It was a trade — preference lift
at the cost of moderate losses elsewhere — and that trade is the
right one given the data.

### `msam_p30_minimax_v3` — P30v1 additive boost reinstated + per-atom tiers (500q)

After P30v2 confirmed flat restoration was strictly worse (commit `5cbcb26`
reverted it), this run measures the additive boost on the **current**
codebase, which has acquired several other small changes since P30v1:
per-atom `_confidence_tier` (replaced bucket-level inheritance), revised
`_two_tier_split` docstring, and a few minor fixups. The intent: confirm
that the revert restored P30v1's overall ~0.78 and ship cherry-picks
behind it.

| Subtype | Score | N | Δ vs P30v1 | Δ vs P30v2 | Δ vs P9v2 |
|---|---|---|---|---|---|
| single-session-assistant | 0.982 | 56 | 0.0 | -1.8 | 0.0 |
| single-session-user | 0.971 | 70 | +1.4 | +4.3 | +0.0 |
| knowledge-update | 0.949 | 78 | +2.6 | +2.6 | +0.0 |
| temporal-reasoning | 0.759 | 133 | +0.7 | +4.5 | -6.7 |
| multi-session | 0.647 | 133 | +0.8 | +1.5 | -3.0 |
| **single-session-preference** | **0.267** | 30 | **-10.0** | **+6.7** | 0.0 |

**Overall: 0.784 (+0.4 vs P30v1, +2.8 vs P30v2, -1.2 vs P9v2).**

Within ±0.4pp of P30v1 overall — the revert restored the canonical
additive-boost behavior, as intended. The bench thus serves the
workflow's gating function: the additive boost is the right mechanism;
proceed with the cherry-picks (P11/P12/P13) on top of this stack.

**The preference divergence is the interesting finding.** P30v1 hit
0.367 on preference (the all-time high on this bench); P30v3 fell back
to 0.267 (matching P9v2 and most other runs). What changed: P30v1 used
**bucket-level** confidence inheritance (a low-similarity raw endorsed
by a strong observation inherited the bucket's "high" tier and survived
the per-atom filter). P30v3 keeps the **per-atom** tier model from P30v2
— each raw gets its own tier from its own similarity, regardless of
which observation endorses it. With per-atom tiers + a `medium` floor
(where this bench is run), exactly the preference-helpful raws — weak
on their own, but endorsed — get filtered out before the reader sees
them.

This is a clean experimental result: bucket-tier inheritance was the
load-bearing preference mechanism, not the additive boost on its own.
The other subtypes prefer the per-atom model (knowledge-update +2.6,
single-session-user +1.4, temporal +0.7, multi-session +0.8). Net
trade is +0.4pp overall but a 10pp loss on preference. The preference
loss is now the most expensive single-subtype regression on this bench
and an explicit choice we are making — bucket-tier inheritance is
exactly the kind of "globally tagged, query-blind" mechanism that
P4-bench's atom-level supersedes also fell to.

Pace: ~31s/q (within noise of P30v1's ~28s/q).

### `msam_cherrypicks_minimax_v1` — P30v3 + all three cherry-picks (500q)

The full P11/P12/P13 cherry-pick stack (commits `fa201ec` + `3d5d497`)
on top of the P30v3 baseline:
- **P11 (`enable_query_rewriting`)** applied to both pathways — built-in
  patterns (`user → User`, `agent → Agent`) plus the additive
  `entity_mappings` mechanism (4033c85). The bench config doesn't add
  custom mappings, so only the built-in normalization fires.
- **P12 (`enable_query_expansion`)** on keyword pathway only — 29-key
  synonym dict from `_DEFAULTS["query_expansion"]["synonyms"]`
  (3d5d497). Keys appearing in the query (case-insensitive) get their
  synonym list appended to the FTS5 query string.
- **P13 (`enable_quality_filter`)** on both raws and observations —
  info-density multiplier (×0.5 for quality<0.3, ×1.1 for >0.7,
  no-op in between).

| Subtype | Score | N | Δ vs P30v3 | Δ vs P9v2 |
|---|---|---|---|---|
| **single-session-preference** | **0.333** | 30 | **+6.7** | **+6.7** |
| knowledge-update | 0.949 | 78 | 0.0 | 0.0 |
| single-session-user | 0.943 | 70 | -2.8 | -1.4 |
| single-session-assistant | 0.946 | 56 | -3.6 | -3.6 |
| multi-session | 0.609 | 133 | -3.8 | -2.5 |
| temporal-reasoning | 0.707 | 133 | -5.3 | -12.0 |

**Overall: 0.756 (-2.8 vs P30v3, -4.0 vs P9v2).**

Net regression. Only one subtype gained — preference (+6.7pp). Every
other subtype was flat or lost ground, with temporal-reasoning hit
hardest at -5.3pp.

**Reading the per-subtype profile:**

- **Temporal-reasoning -5.3pp.** Predicted in the pre-bench risk note:
  P12 synonym expansion adds terms to the FTS5 query string that
  dilute precise time-bound matches. Atom "the user said yesterday X"
  vs probe "what did I tell you yesterday?" → expansion appends "last
  day previous day" which then matches *every* atom containing
  similar generic time language. Keyword pathway noise compounds.
- **Multi-session -3.8pp.** Same FTS5-noise story — multi-session
  questions span turns and rely on precise topic matching.
- **Single-session-assistant -3.6pp** and **single-session-user
  -2.8pp.** Suggests P13 quality scoring is firing — short
  answer-bearing turns ("1:10 ratio", "Patagonia", "Veja") score
  low quality and get demoted by ×0.5. The other risk note that
  materialized.
- **Preference +6.7pp** (recovered most of the drop from P30v1's
  0.367 → P30v3's 0.267). The query-rewriting + quality multiplier
  on observations seems to lift dense user-statement raws into
  surfacing range. Or possibly noise — n=30 is small.
- **Knowledge-update flat.** No effect either way.

**Decision: don't ship the bundle as configured.** Net -2.8pp is too
expensive for a +6.7pp preference gain on 30 questions. Two paths
forward:

1. **Ablate.** Run three separate bench probes with one of P11/P12/P13
   enabled at a time. Cheap signal for which is helping vs hurting.
   Best candidate to ship-alone: **P11 (query rewriting)** —
   theoretically lowest noise, just identity normalization. Worst:
   **P12** — clear FTS5-noise story.
2. **Skip.** Keep the cherry-pick code in place (gated, off in prod by
   default) and revisit when we have evidence about which one carries
   the preference gain.

The cherry-picks are not a regression in the codebase — they're
opt-in flags, prod is unaffected. Just leave the bench config with
them off until ablation says otherwise. Updating msam_bench.toml to
disable the three flags accordingly is a follow-up task (next
session). Pace: ~32s/q (vs P30v3's ~31s/q — synonym expansion +
quality scoring add minor overhead).

### `msam_cherrypicks_off_gptoss_v1` — P30v3 mechanism, gpt-oss-120b for everything (500q)

Two changes from the prior P30v3 baseline (0.784):
- **Cherry-picks reverted to off** (post `msam_cherrypicks_minimax_v1` regression).
  Mechanism is exactly the P30v3 stack: additive boost, per-atom confidence
  tiers, missing-atom cosine pull-in.
- **Every LLM in the bench loop switched to `openai/gpt-oss-120b`** via
  OpenRouter — reader (was MiniMax-M2.7), MSAM's consolidation synthesizer
  (was gpt-5.4-nano), and the judge (was gpt-4o). Auth via the new unified
  `[llm]` section + `OPENAI_BASE_URL` override on the judge.

| Subtype | Score | N | Δ vs P30v3 (gpt-4o judge) |
|---|---|---|---|
| single-session-user | 0.943 | 70 | -2.8 |
| single-session-assistant | 0.911 | 56 | -7.1 |
| knowledge-update | 0.833 | 78 | -11.6 |
| multi-session | 0.549 | 133 | -9.8 |
| **temporal-reasoning** | **0.549** | 133 | **-21.0** |
| single-session-preference | 0.200 | 30 | -6.7 |

**Overall: 0.668 (-11.6 vs P30v3, -12.8 vs P9v2).**

Big absolute drop, but **the comparison isn't apples-to-apples** —
three things changed at once. Most of the headline regression is
likely coming from the **judge swap**, not the reader. gpt-oss-120b's
chain-of-thought reasoning produces stricter / different yes-no
verdicts on borderline cases that gpt-4o would mark correct.
Temporal-reasoning cratered (-21pp) because temporal answers have the
most ambiguous "is this close enough" calls — exactly where judge
disagreement bites hardest.

**Operational notes from running this:**

- **Reader speed: gpt-oss-120b is ~2× faster than MiniMax-M2.7.** Read
  phase dropped from ~5–10s/q to ~1s/q. Total ingest 3h25m vs 4h20m
  on prior runs — meaningful for iteration speed, less for cost.
- **Reasoning models break the LongMemEval judge.** evaluate_qa.py
  hard-coded `max_tokens=10` and read `message.content`. gpt-oss-120b
  put its yes/no inside `message.reasoning` (content was None),
  killing the judge on Q1. Patched upstream evaluate_qa.py to fall
  back to `reasoning` and bumped `max_tokens` to 256. Re-running just
  the judge took ~2 min.
- **Cost: trivial.** 500 reader + 500 judge + ~5 consolidation calls
  on the paid OpenRouter route, roughly $0.30 total at gpt-oss-120b's
  pricing.

**Take on gpt-oss-120b as the bench reader:** **viable for ablation
runs but not for headline numbers.** It's fast, cheap, and the
deltas-within-this-config are meaningful. But absolute scores aren't
comparable to gpt-4o-judged / MiniMax-read runs, and the reasoning-
model footguns (judge max_tokens, content vs reasoning) need handling
in any future bench harness that uses one. For headline numbers stick
with MiniMax reader + gpt-4o judge.

**Take on the cherry-picks-off mechanism (P30v3 config):** the bench
config is in a sensible "go-forward" state — cherry-picks off, P30v3
mechanism intact. To get a clean comparable number to confirm the
revert worked, re-run with reader=MiniMax-M2.7 and judge=gpt-4o
(uncommitted bench config + reader changes can be reverted to do
that). Expected: ≈ P30v3 (0.784) ± small drift from the keyword-only-
atom backfill fix in `29efa38`.

Pace: ~25s/q ingest (vs P30v3's ~31s/q — gpt-oss-120b reader is
faster than MiniMax). Judge: ~2 min for 500q (vs ~7 min on gpt-4o).

### `msam_p32_gptoss_v1` — P30v3 + triples + graph pathway (500q)

P32 wired end-to-end. Bench's `ingest.py` now collects (atom_id, content)
for every semantic-stream atom, then calls `batch_extract_and_store`
(P7) once per question after embedding. ~12 LLM calls per question
instead of ~250 single-shot. Graph pathway joins RRF as a fourth
ranked list (`enable_graph_pathway = true`). All other knobs match
P30v3 + cherry-picks-off baseline. Reader, MSAM consolidation, judge,
and triple extraction all on `openai/gpt-oss-120b` via OpenRouter.

| Subtype | Score | N | Δ vs `cherrypicks_off_gptoss` (0.668) |
|---|---|---|---|
| single-session-assistant | 0.929 | 56 | **+1.8** ✓ |
| single-session-user | 0.929 | 70 | -1.4 |
| **single-session-preference** | **0.233** | 30 | **+3.3** ✓ |
| temporal-reasoning | 0.534 | 133 | -1.5 |
| knowledge-update | 0.795 | 78 | -3.8 |
| multi-session | 0.496 | 133 | **-5.3** |

**Overall: 0.646 (-2.2 vs cherrypicks_off_gptoss).**

Triples + graph pathway **regressed -2.2pp** on the apples-to-apples
gpt-oss baseline. Two subtypes gained — single-session-assistant
(+1.8pp) and preference (+3.3pp), the latter consistent with the
P30v1 / preference-helps-from-graph-features pattern. But
multi-session cratered (-5.3pp) and knowledge-update lost -3.8pp.

**Reading the per-subtype profile:**

- **Multi-session -5.3pp** is the dominant signal. These questions
  span sessions and depend on linking facts across turns. The graph
  pathway should *help* here — it's exactly the cross-session
  fact-linking signal triples theoretically provide. The opposite
  happened. Possible explanations:
  1. Triple extraction noise: gpt-oss-120b extracted bad triples
     (mis-attributed subjects, hallucinated relations) and those
     surfaced via graph pathway, displacing better atoms in RRF.
  2. Graph pathway returned high-confidence-but-irrelevant atoms
     because triples on bench data ("[2023-04-12 user] What's the
     weather?") are extraction-poor.
  3. The 4-way RRF fusion gave too much weight to the graph ranker.
     Default `rrf_graph_weight=0.7`, but with a noisy ranker that
     might still be too much.
- **Knowledge-update -3.8pp** suggests fact-replacement queries
  (where the graph "knows" the *old* fact and surfaces it) are
  getting hurt. This is the temporal-staleness problem the world
  model was designed to handle, but `auto_close_on_conflict` is off
  in the bench (`[world_model] enabled = false`).
- **Preference +3.3pp** and **single-session-assistant +1.8pp** —
  the wins are on tightly-scoped questions where the graph adds a
  redundant correct signal.

**Cost: bad.** P32 took **9h40m** wall time vs cherrypicks-off-gptoss
~3h25m. Triple extraction cost ~6h on top of regular ingest. ~6,500
batched LLM calls at ~3-5s each (gpt-oss reasoning model burns CoT
tokens before emitting). Even amortized via P7 batching, this is a
massive cost increase for what turned out to be a regression.

**Decision: don't ship triples + graph pathway as wired.** The
mechanism is in place and tested but the bench says it hurts more
than it helps on this corpus / this judge. Keep the code (triples
have other uses — graph traversal, contradiction detection, world
model), but leave `[triples] enable_extraction = false` in the bench
config and `[retrieval] enable_graph_pathway = false` in retrieval.

**What might rescue triples in a future experiment:**
1. Lower `rrf_graph_weight` substantially (e.g. 0.3) so graph votes
   only break ties rather than overwhelm the fusion.
2. Filter graph-pathway hits by triple-confidence threshold.
3. Use a more conservative triple extraction prompt (current
   extraction is greedy — accepts more triples).
4. Test on a stronger reader/judge stack (gpt-4o reader + gpt-4o
   judge) where the bench-score noise floor is lower.

But these are follow-on experiments, not corrections to the running
P32 baseline. P32 is a **null result** for this corpus + judge.

### `msam_rerank_gptoss_v1` — P30v3 + cross-encoder LLM rerank (500q)

P15 cherry-picked from `retrieval_v2.rerank_with_llm`. After RRF
fusion produces the top-K candidates, the top 8 atoms are sent to
gpt-oss-120b with a "rank these passages by relevance" prompt; the
returned ordering replaces RRF's. Triples + graph pathway OFF for
this run (clean rerank-only signal vs `cherrypicks_off_gptoss`
baseline).

| Subtype | Score | N | Δ vs `cherrypicks_off_gptoss` (0.668) | Δ vs P32 (0.646) |
|---|---|---|---|---|
| single-session-user | 0.943 | 70 | 0.0 | +1.4 |
| single-session-assistant | 0.929 | 56 | **+1.8** ✓ | 0.0 |
| **single-session-preference** | **0.233** | 30 | **+3.3** ✓ | 0.0 |
| temporal-reasoning | 0.526 | 133 | -2.3 | -0.8 |
| knowledge-update | 0.795 | 78 | -3.8 | 0.0 |
| multi-session | 0.504 | 133 | **-4.5** | +0.8 |

**Overall: 0.648 (-2.0 vs baseline, +0.2 vs P32).**

The striking finding: **rerank-only and triples-only produced
almost identical scores** (0.648 vs 0.646) with **almost identical
per-subtype profiles** — both gained on single-session-assistant
(+1.8) and preference (+3.3), both lost on multi-session (-4.5
to -5.3), knowledge-update (-3.8 each), and temporal-reasoning
(-1.5 to -2.3). Two completely different mechanisms — one adds a
fourth RRF ranker fed by extracted facts, the other replaces the
final ordering with an LLM judge — and they hit the same numbers.

**The most likely explanation:** the regression isn't about the
mechanisms. It's the gpt-oss-120b reader-and-judge combo
producing a noisy ceiling that any reordering past RRF runs into.
Multi-session is hardest for the reader; the judge is hardest on
multi-session calls. Both reorder paths shuffle which atoms land in
the reader's context window — and shuffling away from RRF's
default produces marginal-but-correlated regressions across the
hard subtypes regardless of how the reordering happens.

The +1.8/+3.3 gains on the easy subtypes are real — single-session-
assistant and preference both benefit from "second-look" mechanisms
in general (graph fact-linking for preference, LLM judging for the
short-answer cases in single-session-assistant). But these gains
are small and correlate, suggesting they reflect a property of the
gpt-oss stack rather than the levers.

**Decision summary across the gpt-oss bench triplet:**

| Bench | Score | vs baseline | Cost | Verdict |
|---|---|---|---|---|
| `cherrypicks_off_gptoss_v1` | 0.668 | — | ~3h25m | Baseline. Two-tier P30v3 mechanism is the floor. |
| `p32_gptoss_v1` | 0.646 | -2.2 | ~9h40m | **Don't ship.** 6h extra cost for a regression. |
| `rerank_gptoss_v1` | 0.648 | -2.0 | ~3h12m + ~5min judge | **Don't ship.** Cheap, but doesn't help. |

**Take on which lever earned its keep:** **neither, on this stack.**
Both cost something (triples = a lot of LLM calls + ingest time;
rerank = an LLM call per query in production); both regressed
equally on the gpt-oss-judged baseline. The strong correlation
between their per-subtype regressions suggests the issue is with
the gpt-oss-120b reader + judge being noisy on the hard subtypes,
not the levers themselves.

**Next bench worth running:** rerank-only on the **MiniMax + gpt-4o
canonical stack** (the original benchmark configuration). If rerank
helps there, the levers are real and the gpt-oss noise was masking
them. If it doesn't, rerank is just dead weight on this benchmark
and we can definitively kill P15. Same logic applies to triples,
but rerank is cheaper to test (one bench instead of one + 6h
extraction cost).

Pace: rerank ~24s/q (vs baseline ~25s/q — rerank adds ~100ms per
query in the LLM call but cons/retrieve/read sum dominates).

### `msam_p35_canon_v1` — P30v3 + P35 (triples as consolidation byproduct), canonical stack (500q)

P35 takes the same end-state as P32 (triples + graph pathway on) but
moves extraction inside consolidation: one LLM call per cluster
produces both the observation and structured triples. Cluster-level
extraction was hypothesized to be higher-quality than per-batch
extraction (more topical context, better coreference, less noise from
short atoms). Same canonical stack as P30v3 (MiniMax reader, gpt-5.4-
nano consolidation, gpt-4o judge) — the only diff is P35 features ON.
Plus P35.1 (prior triples passed to synthesizer on supersede) and
P35.2 (ownership transfer on restate).

| Subtype | Score | N | Δ vs P30v3 (0.784) |
|---|---|---|---|
| single-session-assistant | 0.982 | 56 | 0.0 |
| single-session-user | 0.929 | 70 | -4.3 |
| **multi-session** | **0.602** | 133 | **-4.5** |
| temporal-reasoning | 0.752 | 133 | -0.7 |
| knowledge-update | 0.910 | 78 | -3.8 |
| single-session-preference | 0.267 | 30 | 0.0 |

**Overall: 0.758 (-2.6 vs P30v3, -3.8 vs P9v2).**

**Hypothesis falsified.** The pre-bench prediction was that cluster-level
triple extraction (more topical context, better coreference, no
short-atom noise) would lift multi-session by **+2 to +5pp** because
graph fact-linking is exactly what multi-session needs. **Actual:
multi-session DROPPED -4.5pp** — the same direction P32 went on the
gpt-oss stack.

The architecture change (extraction-during-consolidation vs separate-
batch-extraction) cleaned up the cost story (no extra LLM calls,
~6h ingest savings) but didn't change the retrieval signal. Both
P32 and P35 land at roughly -2 to -3pp regression on their respective
canonical baselines:

| Bench | Stack | Triples | Δ vs same-stack baseline |
|---|---|---|---|
| `msam_p32_gptoss_v1` | gpt-oss | separate pipeline | -2.2 vs 0.668 |
| `msam_p35_canon_v1` | canonical | byproduct of consolidation | -2.6 vs 0.784 |

So **triples + graph pathway is just net-negative on LongMemEval**,
independent of how they're extracted. The cluster-level quality
hypothesis didn't save it.

**Likely failure modes (post-hoc):**

1. **Graph pathway has too much voice in RRF.** Default
   `rrf_graph_weight=0.7` (vs 1.0 for sem and kw). With even decent
   triples, the graph pathway's atoms get nontrivial fusion weight,
   and triples on observations are noisier than direct atom
   embeddings on raws. This is the same hypothesis the P32 writeup
   floated.
2. **Triples are too abstract for multi-session questions.**
   Multi-session needs "find the specific turn in session 5 and the
   related turn in session 12." Triples synthesize away the
   per-turn specificity. The graph pathway then surfaces the
   *observation* atom (which has the consolidated fact but loses
   the source-turn provenance the reader needs).
3. **Per-question DBs are too small for triple graphs to be useful.**
   ~15 clusters → ~30–100 triples per question. Graph queries on
   that scale don't have enough material to disambiguate. Production
   deployments with months of accumulated triples might benefit;
   this benchmark by construction doesn't.
4. **Knowledge-update -3.8pp** is the clearest "old triple still
   surfacing" signal — even with P35.2's ownership transfer on
   restate, retracted facts on demoted observations are still hit
   by graph queries when the new observation isn't in the candidate
   pool (the conditional supersedes-demotion issue we flagged).

**Decision: don't ship triples + graph pathway as default.** Keep the
P35 architecture — it's strictly better for production agents that
*want* the graph layer (one LLM call, free triples, ownership
transfer, prior-belief handling). But on LongMemEval the graph
pathway is a net regression regardless of extraction quality.

`msam_bench.toml` going forward: `enable_extraction = false` (P35
no-op when disabled — synthesizer just emits observation, skips
triples in its prompt) and `enable_graph_pathway = false`. P30v3
mechanism remains the canonical configuration.

**What this rules out:**
- "P32 was bad because per-atom batch extraction was noisy" — false.
  Cluster-level extraction was equally bad.
- "Triples need a richer LLM than gpt-oss-120b for extraction" —
  false. gpt-5.4-nano on the canonical stack didn't fix it.
- "Triples just need a stronger reader/judge to surface their lift"
  — false. MiniMax + gpt-4o didn't surface a lift either.

**What's still possible:**
- Lower `rrf_graph_weight` to 0.3 or 0.2 — graph pathway becomes a
  tiebreaker rather than a vote. Would cap the damage. Worth one
  more bench if we want to definitively close the question.
- Use triples for confidence/contradiction signals only, not as a
  retrieval pathway. Different feature, different bench.

For now, P32 and P35 are both archived as "tested, doesn't help on
LongMemEval, kept in code for production-graph-feature deployments."

Pace: ~38s/q (vs P30v3's ~31s/q — consolidation prompt produces more
output and gpt-5.4-nano takes a bit longer for the structured format).

### `msam_p11_canon_v1` — cherry-pick ablation: P11 alone (498q, 2 errors)

First of three ablation runs disambiguating the cherry-picks bundle
regression (`msam_cherrypicks_minimax_v1` = 0.756, -2.8pp vs P30v3 =
0.784). P11 is built-in query rewriting (`user → User`,
`agent → Agent`, etc.) applied on both pathways via
`_apply_query_rewriting`. P12 and P13 stay off.

| Subtype | Score | N | Δ vs P30v3 (0.784) | Δ vs bundle (0.756) |
|---|---|---|---|---|
| **single-session-preference** | **0.300** | 30 | **+3.3** ✓ | -3.3 |
| single-session-assistant | 0.946 | 56 | -3.6 | 0.0 |
| single-session-user | 0.943 | 70 | -2.8 | 0.0 |
| knowledge-update | 0.936 | 78 | -1.3 | -1.3 |
| temporal-reasoning | 0.752 | 133 | -0.7 | +4.5 |
| multi-session | 0.634 | 131 | -1.3 | +2.5 |

**Overall: 0.771 (-1.3pp vs P30v3, +1.5pp vs bundle).**

P11 alone sits between the bundle (0.756) and the canonical baseline
(0.784): better than the bundle, but still slightly worse than no
cherry-picks. Preference recovers (+3.3pp vs P30v3) — the bundle's
preference gain was apparently coming from P11 specifically. Other
subtypes regress 1-4pp, similar to the bundle's pattern but milder.

Net interpretation so far (P11 done, P12/P13 pending): the bundle's
-2.8pp regression decomposes partially into P11's -1.3pp. The
remaining ~1.5pp gap between P11 alone and the bundle must come
from P12 or P13. Specifically:

- P11 contributes the preference gain (+3.3pp) and ~1.3pp of broad
  regression
- P12 and/or P13 must contribute an additional ~1.5pp of regression
  on top of P11's

The next two ablations (P12 alone, P13 alone) will tell us which of
those is the primary culprit.

**Two errors note.** 498 processed instead of 500 — likely MiniMax
API hiccups during the run. Subtype counts shifted slightly
(multi-session 131 vs 133). Doesn't materially change the headline.

Pace: ~33s/q (~4h29m for 500 questions).

### `msam_p12_canon_v1` — cherry-pick ablation: P12 alone (499q, 1 error)

Second of three ablation runs. P12 is synonym expansion on the
keyword pathway only — for query terms matching keys in the 29-entry
default synonym dict (`profession`, `food`, `movie`, `told`, etc.),
the synonyms are appended to the FTS5 BM25 query string. Semantic
pathway sees the original query unchanged. P11 and P13 stay off.

| Subtype | Score | N | Δ vs P30v3 (0.784) | Δ vs bundle (0.756) | Δ vs P11-alone (0.771) |
|---|---|---|---|---|---|
| **single-session-preference** | **0.483** | 29 | **+21.6** ✓✓ | +21.6 | +18.3 |
| knowledge-update | 0.962 | 78 | **+1.3** ✓ | +1.3 | +2.6 |
| single-session-user | 0.957 | 70 | -1.4 | +1.4 | +1.4 |
| multi-session | 0.647 | 133 | 0.0 | +1.5 | +1.3 |
| temporal-reasoning | 0.752 | 133 | -0.7 | +4.5 | 0.0 |
| single-session-assistant | 0.946 | 56 | -3.6 | 0.0 | 0.0 |

**Overall: 0.792 (+0.8pp vs P30v3, +3.6pp vs the cherry-picks bundle).**

**This is the first new positive bench result on this stack since
P30 shipped.** P12 alone beats P30v3 (canonical baseline) by +0.8pp
overall and effectively ties P9v2 (best ever, 0.796) within
benchmark noise.

The headline number is interesting; the preference subtype number is
**massive**: from 0.267 to 0.483, a +21.6pp absolute gain on a 30-
question subtype. This is the biggest single-subtype lift we've ever
recorded on this bench. Knowledge-update also gained +1.3pp, hitting
0.962 — a new high. Multi-session held flat, temporal stayed near
baseline, and only single-session-assistant lost meaningful ground
(-3.6pp).

**Why P12 worked here.** Preference probes are the case where the
question vocabulary and the answer vocabulary are most likely to
differ. A probe asks *"What kind of music do I like?"* and the gold
atom says *"I love bluegrass"* — the FTS5 keyword pathway wouldn't
match those tokens directly. P12's synonym expansion appends
synonyms when keys match: `prefer → favorite/like/favourite`,
`favorite → preferred/like/love/enjoy`. The expanded keyword query
hits the gold atom; FTS5 brings it into the candidate pool; RRF
fuses it with the (already-decent) semantic pathway result.

Knowledge-update's +1.3pp also fits this story: fact-replacement
questions ("the user changed jobs" → atoms about new vs old job)
benefit from synonym coverage on `job/profession/career/work` and
`company/employer/office`.

**The bundle decomposition is now legible.** Bundle = -2.8pp vs P30v3:

- P11 alone = -1.3pp (small regression, +3.3pp preference gain)
- P12 alone = **+0.8pp** (small overall gain, +21.6pp preference gain)
- P13 alone = pending (probably the biggest regression source)

P11 and P12 each lift preference (+3.3pp and +21.6pp respectively)
but in different ways and presumably non-additively. The bundle
captured P12's preference gain (+13.3pp in the bundle's preference
result) but **didn't** capture the +21.6pp P12 alone delivers — so
P11 and P13 actively interfere with P12's lift when stacked.

P13's result will tell us how much it personally costs (and where
the residual ~3pp of bundle regression came from). Best guess
ahead of data: P13 is -2 to -3pp because the heuristic atom-quality
multiplier demotes short answer-bearing turns ("Patagonia",
"1:10 ratio", "Veja") that the reader needs.

**One error note.** 499 processed (single MiniMax API hiccup mid-
run). Preference subtype n=29 instead of 30. Doesn't affect the
headline (a single +/- on a 29-q subtype is ±3.4pp, well within
the +21.6pp lift).

Pace: ~32s/q. P12 took 4h42m total ingest.

### `msam_p13_canon_v1` — cherry-pick ablation: P13 alone (500q)

Third and final ablation. P13 is the heuristic atom-quality
multiplier — `compute_atom_quality(content)` produces a 0-1 score
from length, vocabulary diversity, named-entity density, and
structure markers; atoms scoring <0.3 get `_combined_score × 0.5`,
atoms scoring >0.7 get `_combined_score × 1.1`. P11 and P12 stay off.

| Subtype | Score | N | Δ vs P30v3 (0.784) | Δ vs bundle (0.756) |
|---|---|---|---|---|
| single-session-assistant | 0.982 | 56 | 0.0 | +3.6 |
| single-session-user | 0.943 | 70 | -2.8 | +1.4 |
| temporal-reasoning | 0.737 | 133 | -2.2 | +2.2 |
| multi-session | 0.632 | 133 | -1.5 | +1.6 |
| knowledge-update | 0.910 | 78 | -3.8 | -3.8 |
| **single-session-preference** | **0.167** | 30 | **-10.0** ⚠ | **-10.0** |

**Overall: 0.758 (-2.6pp vs P30v3, -0.2pp vs the cherry-picks bundle).**

**P13 is the bundle's primary regression source.** P13 alone is
basically as bad as the full bundle, and the preference regression
is severe: -10pp absolute (0.267 → 0.167), the worst single-subtype
regression we've ever recorded.

**The hypothesis materialized exactly as predicted in the
pre-bench risk note** ("P13 may demote short answer-bearing
turns"). Preference probes have the shortest gold answers in the
bench: "Patagonia", "Veja", "1:10 ratio", color names, single-word
genres. Those atoms hit the heuristic's "low quality" branch (n_words
< 5 → length_score = 0.2) and get demoted. The reader then doesn't
see them, can't answer, and preference cratered.

**Bundle decomposition is fully legible now:**

| Lever | vs P30v3 | Preference Δ |
|---|---|---|
| P11 alone (query rewriting) | -1.3pp | +3.3pp |
| P12 alone (synonym expansion) | **+0.8pp** ✓ | **+21.6pp** ✓✓ |
| P13 alone (quality multiplier) | -2.6pp | -10.0pp |
| Sum (predicted bundle) | -3.1pp | +14.9pp |
| Actual bundle measured | -2.8pp | +10.0pp |

The sum-of-deltas (-3.1pp) closely matches the actual bundle
(-2.8pp) — small interaction effects but no major synergies. P12's
preference gain partially survives in the bundle (+10pp vs +21.6pp
solo), and P13's regression dominates the overall headline.

**Decision:** ship **P12 alone**, **kill P11 and P13**.

P11 brings small preference gain (+3.3pp) at -1.3pp overall cost —
not worth it when P12 alone delivers +21.6pp preference gain at
+0.8pp overall. P13 is straight-up harmful. Both should be
default-off in the bench config and code.

**Configuration to apply (next session):**

```toml
[retrieval_v2]
enable_query_rewriting = false  # P11: small lift on preference, broader regression
enable_query_expansion = true   # P12: +0.8pp overall, +21.6pp preference
enable_quality_filter  = false  # P13: -10pp on preference, demotes short answer-bearing turns
```

**Cleanup follow-up.** P13's `compute_atom_quality` heuristic is
fundamentally miscalibrated for short conversational turns where
the answer is exactly the kind of content the heuristic flags as
"low quality." Worth filing as a future experiment: replace the
heuristic with an LLM-graded quality assessment as part of the
P35 consolidation pass (would only score atoms in clusters), or
delete `enable_quality_filter` entirely.

Pace: ~32s/q, 4h42m total ingest.

### `msam_p34_canon_v1` — consolidation threshold sweep (500q)

P34 lowers `[consolidation] similarity_threshold` from 0.80 to 0.75
on the canonical stack. P34's offline pair-similarity analysis
(`p34_threshold_analysis.py`) showed LongMemEval haystacks have
~55 pairs/question crossing 0.80 vs ~103 crossing 0.75 — roughly
2× the cluster-yield from a lower threshold.

| Subtype | Score | N | Δ vs P30v3 (0.784) |
|---|---|---|---|
| single-session-assistant | 0.982 | 56 | 0.0 |
| single-session-user | 0.957 | 70 | -1.4 |
| single-session-preference | 0.267 | 30 | 0.0 |
| multi-session | 0.647 | 133 | 0.0 |
| temporal-reasoning | 0.744 | 133 | -1.5 |
| knowledge-update | 0.910 | 78 | **-3.8** |

**Overall: 0.772 (-1.2pp vs P30v3).**

**More clusters didn't lift retrieval; they hurt knowledge-update.**
The pair-sim analysis was right that the 0.75–0.80 band contains
real paraphrase-pair content; the bench result says clustering that
content into observations doesn't help. The biggest hit is on
knowledge-update (-3.8pp): fact-replacement questions are exactly
where consolidating "the user said X" + "the user said updated-X"
into one observation produces a confused belief that retrieval
then surfaces.

The offline analysis already flagged this risk — at 0.75 we're
clustering paraphrase pairs ("revised LinkedIn tagline" with the
original tagline), and the consolidation prompt's ability to
preserve "first this, then that" is imperfect. More clusters means
more chances to confuse fact-evolution.

**Decision: don't ship.** Default 0.80 is correct for LongMemEval.
The Mimir-derived 0.75 was right for Mimir's news-feed corpus
(which doesn't have fact-evolution patterns); LongMemEval has them
and pays the cost.

P34's clustering threshold could still help in a configuration that
ALSO has temporal tagging on consolidation outputs (P37 option a).
That's a future test — not run yet.

---

### `msam_p36_canon_v1` — graph pathway as tiebreaker (500q)

P36 keeps P35's triples + graph pathway ON but drops
`[retrieval] rrf_graph_weight` from 0.7 to 0.3. Tests whether graph
as a tiebreaker (rather than a co-equal vote) rescues the P35
regression.

| Subtype | Score | N | Δ vs P30v3 (0.784) | Δ vs P35 canon (0.758) |
|---|---|---|---|---|
| single-session-assistant | 0.982 | 56 | 0.0 | **+3.6** ✓ |
| single-session-user | 0.971 | 70 | 0.0 | **+4.2** ✓ |
| multi-session | 0.639 | 133 | -0.8 | **+3.7** ✓ |
| single-session-preference | 0.233 | 30 | -3.4 | -3.4 |
| temporal-reasoning | 0.722 | 133 | -3.7 | -3.0 |
| knowledge-update | 0.885 | 78 | **-6.4** | -2.5 |

**Overall: 0.760 (-2.4pp vs P30v3, +0.2pp vs P35 canon — within noise).**

The data is informative but the answer is "no, it doesn't help."

**Vs P35 canon (0.758, same triples-on stack but rrf_graph_weight=0.7):**
the lower weight DOES recover single-session subtypes by ~+3.6 to
+4.2pp each (these are the cases where a noisy graph pathway was
displacing good direct-similarity matches). Multi-session also
recovers +3.7pp.

**Vs P30v3 (0.784, no triples at all):** still net regression
of -2.4pp. The recovery on single-session subtypes is offset by
even bigger losses on knowledge-update (-6.4pp), temporal-reasoning
(-3.7pp), and preference (-3.4pp). The graph pathway, even at
weight 0.3, still introduces structural problems in those subtypes
that the lower weight can't fix.

**Knowledge-update -6.4pp is the dominant signal.** Same shape as
P34 but worse — fact-replacement questions are where stale-fact
triples on superseded observations actively mislead retrieval.
Lower graph weight reduces but doesn't eliminate the damage.

**Decision: don't ship.** Triples + graph pathway is fundamentally
net-negative on LongMemEval regardless of fusion weight. The
hypothesis "rrf_graph_weight=0.7 was just too aggressive" is
falsified — even at 0.3 it costs more than it gives.

**What this rules out** (combined with P32 and P35 results):
- "Per-batch extraction was noisy" — falsified by P35
- "Cluster-level extraction is higher quality" — falsified by P35
- "Stronger reader/judge surfaces the lift" — falsified by P35 canon
- "Graph pathway needs lower fusion weight" — falsified here (P36)

Triples + graph pathway are confirmed dead for LongMemEval
retrieval. The P35 architecture (consolidation as structured-
cognition pass) stays in the codebase as the right design for
production deployments that want graph features for their own
sake (audit trails, contradiction detection, world model). Bench
config keeps `enable_extraction = false` and
`enable_graph_pathway = false` as the canonical defaults.

Pace: P34 ~42s/q (slow because more clusters → more LLM calls per
question); P36 ~35s/q.

## P9 summary

Three variants tried:

- **P9v1 (min=2)**: keeps P1's preference gain (+13.3), basically holds
  multi-session at P2. **Overall 0.7816.**
- **P9v2 (min=3)**: loses preference gain, but lifts multi-session
  (+3.5), single-session-user (+2.9), and partially recovers temporal.
  **Overall 0.796.** Best of the three.
- **P9v3 (per-stream cluster floors + tighter obs gating + per-stream
  prompts)**: regressed -3.6 from v2 across nearly every subtype.
  Stacked tunings interacted badly. **Overall 0.760.**

Neither beats P2 overall by enough to call it a clear win. The
architecture is sound — observations help where they should help — but
the cluster-size knob fundamentally trades subtypes against each
other, and on this benchmark the trade comes out roughly even.

The v3 lesson is that the four "obvious" knobs do not stack additively.
Episodic-only `min=2` doesn't recover preference if the consolidation
prompt is also changed to abstract patterns instead of preserve
specifics — the abstract observations don't match probe wording.
Tightening obs gating reduces multi-session signal, which was the main
v2 win. The takeaway: tune one knob per run.

Architectural lesson is more useful than the score: P9 successfully
isolated abstraction from raw evidence, removed the P1-style
stability-halving harm, and made consolidation a benefit-or-neutral
on this bench rather than the net-negative P1 was. P9v2 is the
recommended P9 configuration. Good shape to ship.

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
