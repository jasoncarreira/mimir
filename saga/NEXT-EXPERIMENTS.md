# Next Experiments for MSAM

Proposed list of investigations and code changes to look at next. Companion
to `HINDSIGHT-IDEAS.md`, which captured the original P1–P10 roadmap.

This document is the output of a session on 2026-04-26. MSAM head is `591e48a`.

## Status updates since this doc was created

- **P4-bench shipped and rolled back.** Result: 0.766 on LongMemEval (regressed
  -3.0pp vs P9v2). Atom-level supersedes demotion broke temporal-reasoning
  (-6.7pp) because retrieval is query-dependent but supersession is a global
  tag. **Auto-triggers disabled** in `591e48a`. The function
  `resolve_contradictions_to_supersedes` stays callable for users who want
  it. Observation-level supersedes (consolidation writing edges between
  observations whose evidence is a strict superset) is **kept and now
  applied in the obs tier** of two-tier retrieval.
- **P14 (optimize supersedes resolver)** — deferred. Only relevant if
  atom-level supersedes is revisited; not currently a planned experiment.
- **Skip-on-identical and superset-supersedes for observations** shipped
  in `34a4243` and `c6909a2`. Both safe defaults.

The P-series numbering keeps going — P11+ is anything below — but the
groupings reflect the *kind* of work, not the order they should ship in.
The "Recommended order" at the bottom proposes a sequence by goal.

---

## A. Retrieval improvements

### P11 — Cherry-pick query rewriting from `retrieval_v2` [shipped: `fa201ec`, awaiting bench]

**What.** Pattern-based query normalization (entity aliases). Currently in
`retrieval_v2.rewrite_query()`. Plumb to the front of `hybrid_retrieve`
so single-tier and P9 two-tier both benefit.

**Why.** Names appear inconsistently across haystack turns ("user", the
agent's nickname, "I"). Embedding similarity smooths some of this, but
keyword/FTS5 doesn't. Pre-rewrite normalizes both pathways.

**Effort.** 0.5 day.

**Risk.** Low — already tested in retrieval_v2 path.

**Score expectation.** Small but consistent. Helps single-session-user
where the user phrases things differently from how facts were stored.

---

### P12 — Cherry-pick query expansion from `retrieval_v2` [shipped: `fa201ec` + `3d5d497`, awaiting bench]

**What.** Synonym expansion via `[query_expansion] synonyms` config dict
(e.g. `profession → [job, career, work, occupation]`). Currently in
`retrieval_v2.expand_query()`. Apply only to the keyword (FTS5) pathway —
semantic side already handles synonyms via embedding similarity.

**Why.** FTS5 BM25 doesn't know that "profession" and "job" are related.
Query expansion lets the keyword pathway find atoms phrased with
synonyms.

**Effort.** 0.5 day.

**Risk.** Low. Worst case: noisier FTS5 candidate pool. RRF on the
keyword side absorbs the noise.

**Score expectation.** Helps preference and single-session questions
where the haystack uses different vocabulary than the probe.

---

### P13 — Cherry-pick atom quality scoring from `retrieval_v2` [shipped: `fa201ec`, awaiting bench]

**What.** `compute_atom_quality(content)` — info-density score (length,
named-entity count, specificity). Apply as a multiplier inside
`hybrid_retrieve` (low quality ×0.5, high ×1.1). Either compute on
demand or use a precomputed `quality` column populated by
`precompute_atom_quality()`.

**Why.** Some atoms are short turns with no real content ("ok thanks!"),
others are dense with names and dates. Demoting the former gives the
information-rich atoms more room in the top-K.

**Effort.** 0.5 day.

**Risk.** Medium. Quality scoring is heuristic; some "low quality" content
(short turns containing the answer) may carry exactly what we need.

**Score expectation.** Could help temporal-reasoning where dates appear
in dense turns. Could hurt single-session-user where the answer is a
casual sentence. A/B carefully.

---

### P14 — Optimize the P4-bench supersedes resolver

**What.** Currently brute-force pairwise cosine across all topic-grouped
active atoms (~13s per question in P4-bench). Two changes: (a) FAISS-
backed candidate filtering for the contradiction detector, (b) restrict
to `memory_type='raw'` in the candidate SQL.

**Why.** Supersedes resolution is now a real per-question cost in the
bench (~30% of wall time). If P4 lifts scores, we want it cheap. If it
doesn't, the optimization makes future ablation cheaper.

**Effort.** 1 day.

**Risk.** Low. Same algorithm, faster path. Need to verify FAISS
top-K returns the same contradiction pairs we'd find brute-force at
threshold ≥ 0.85.

---

### P30 — Compute true base score for missing evidence atoms in two-tier retrieval [shipped: option 1, awaiting bench]

**What.** In `_two_tier_split` (`core.py:1191-1251`), surfaced observations
boost their evidence atoms via the `evidenced_by` edges. The boost is
applied two ways:

- **Evidence atom in raws top-K candidate pool**: final score is
  `base_RRF + min(boost, cap_multiplier × base_RRF)`. The boost is
  *capped relative to the atom's own RRF*, preventing one atom from
  dominating the top-K.
- **Evidence atom NOT in raws top-K**: final score is
  `boost_map[atom_id]` — *just the boost, no own-score, no cap*.

This is asymmetric in a way that **systematically favors atoms outside
the candidate pool over atoms inside it**. A missing atom evidenced by
3 surfaced observations gets `3 × multiplier × obs_score` (uncapped),
which can exceed the in-top-K atom's bounded `base × cap_multiplier`.
In other words: an atom that *just barely missed* the candidate pool
on its own merit can outscore an atom that *did make it* — purely
because the missing one doesn't have an own-RRF to cap against.

**Why this matters.** The fix is a real ranking correctness issue, not
a niceness change. Whether it moves the bench score depends on how
often the missing-atom path activates and whether the reader benefits
from those particular missing atoms over the in-top-K ones they're
displacing.

**Three implementation options:**

1. **Compute cosine similarity for missing atoms against the query
   embedding** and use that as a base score. Apply the same
   cap-relative-to-base boost formula. Cost: ~one cosine per missing
   atom (typically 3-15 atoms per query). No retrieval re-run needed.
2. **Re-run `retrieve()` and `keyword_search()` with a much larger
   `top_k`** (e.g. 5×) so missing atoms get real RRF scores. Cost:
   one extra retrieval pass per query, but probably caches
   embeddings/FAISS index well. Yields same kind of score as in-top-K
   atoms (RRF rank-based), which is the cleanest comparison.
3. **Don't pull missing atoms in at all.** If an atom couldn't make
   the top-K on its own relevance, it shouldn't be in the raws tier
   just because it backs an observation — the observation itself
   surfaces the synthesized claim, and the raws tier is for primary
   evidence relevant *to the query*. This is the most aggressive
   change and shifts the design philosophy.

**Effort.** 0.5 day for option 1, 1 day for option 2, 0.25 day for
option 3.

**Risk.**
- Option 1: low. Cosine similarity is the same signal `retrieve()`
  uses; just applied here.
- Option 2: low. Same algorithm, more compute. Need to check the
  retrieve cost doesn't bottleneck the bench.
- Option 3: medium. Could lose evidence atoms that the reader actually
  needs (e.g. an observation says "user prefers Sony" and the missing
  raw is the original "I love my Sony A7" turn — the date might
  matter for a temporal-reasoning probe).

**Recommendation.** Ship option 1 first. It's the smallest diff that
fixes the asymmetry. If the bench result is neutral or positive,
consider option 3 as a more aggressive follow-up.

**Score expectation.** Hard to predict. The bug systematically inflates
out-of-pool atoms. Fixing it may help (better atoms now have a fair
shot at top-K) or hurt (some missing atoms that were genuinely useful
get dropped). A/B is the only way to know.

---

### P32 — Wire triple extraction + graph pathway and measure the lift

**What.** Triple extraction is partially wired:

1. ~~**The `[triples] enable_extraction` config flag is unread.**~~
   **Fixed in commit forthcoming** — `/v1/store` now reads the flag.
   Default True for back-compat; bench has it false. (Mimir hit this:
   post-`bc2c4ce` the LLM auth flows through to triples too, so every
   semantic store fires a triple-extraction LLM call until the gate
   was added.)
2. **`store_atom` in `core.py` doesn't call extraction.** Only the REST
   `/v1/store` and `/v1/triples/extract` endpoints invoke it. The bench
   ingests via `store_atom` directly (`ingest.py`), so zero triples are
   written for any benchmark run, and the graph pathway in
   `hybrid_retrieve` (gated by `enable_graph_pathway`, default false)
   would return `[]` even if turned on.

**Plumbing required:**
1. ~~Make `[triples] enable_extraction` actually gate (read in `store_atom`
   and `/v1/store`).~~ Done for `/v1/store`.
2. When the flag is true, call `extract_and_store(atom_id, content)` for
   `stream='semantic'` atoms inside `store_atom` (matching `/v1/store`
   behavior).
3. Enable `[retrieval] enable_graph_pathway = true` in the bench config.
4. Set `[triples] enable_extraction = true` in the bench config.
5. (Pre-req or co-experiment) **P7 — batch the triple extraction LLM
   call** (already in HINDSIGHT-IDEAS.md). Single-shot extraction is
   ~1.5s/atom; with ~500 atoms per question and 500 questions, that's
   ~100 hours per bench run. P7 brings this to ≤ 6h via batching.

**Effort.** 1 day for the gating + bench config. P7 is separate (~1 day,
already speced).

**Score expectation.** Honestly uncertain. Triples shine on multi-hop
queries ("what shows is the user performing in?" → user → performs_in →
Hamilton), where embedding similarity misses but triples bridge. P3's
graph-pathway result was neutral-to-negative because the triple store
was empty — same root cause we'd be fixing. With actual triples in
place, multi-session and temporal-reasoning are the most plausible
lift candidates.

**The bar.** This experiment costs real money — every semantic atom
ingestion adds an LLM call, and we re-run the bench. **Must move the
overall score by ≥ 1pp** vs P9v2 (the current ship configuration) to
be worth shipping. Sub-1pp lift means we eat the LLM cost in production
forever for marginal benefit; that's a bad trade. If the lift is < 1pp,
formally mark triples as research-only and document the cost/benefit
in the architecture spec.

**Risks.**
- **LLM cost & latency.** Even batched, every semantic write adds
  noticeable latency. Mimir's agent harness writes turn-by-turn; ~1s
  added latency per turn is user-visible.
- **Triple quality.** Heuristic-parsed (subject, predicate, object)
  tuples from an LLM are noisy. Bad triples are worse than no triples
  because graph traversal pulls in irrelevant atoms.
- **Storage.** Triples table grows linearly with atoms. Probably fine
  but worth monitoring.

**Recommendation.** Sequence as:
1. Land P7 (batch extraction) first.
2. Wire the gating + flip the bench flag.
3. Enable graph pathway.
4. Run bench, measure.
5. Decide ship/no-ship by the 1pp bar.

If we're going to invest the LLM cost long-term, this is also the right
moment to revisit the triple extraction prompt — the current one returns
SKIP for too many atoms (probably). Worth A/B'ing the prompt.

---

### P33 — Recalibrate confidence_sim_{high,medium,low} thresholds

**What.** The current defaults (`high=0.45, medium=0.30, low=0.15`) were
set for tightly-coupled paraphrase matches. Real "ask a question, find a
fact" probes land lower than the defaults assume: in a Mimir bench
debug bundle (90 raw atoms, OpenAI text-embedding-3-small, 3 indirect
probes against a Bluesky-feed corpus), the *correct* top match scored
0.34–0.43, with only 0–2 atoms per probe clearing the medium floor.
Result: when the API caller passes `min_confidence_tier="medium"`, the
per-atom filter drops nearly everything — including atoms that are
clearly the right answer.

**Why this is a real problem, not a one-off.** The thresholds are the
contract between "atoms are returned" and "atoms feel relevant to the
agent." Right now that contract assumes much tighter cosine coupling
than indirect questions actually produce. Lowering the bar globally
risks flooding agents with weak matches; raising it risks the Mimir
case (zero recall on relevant atoms). We should empirically pick
thresholds against a labelled dataset rather than carry the current
hand-set values.

**Proposed experiment:**
1. Take LongMemEval's labelled question→evidence-atom pairs as ground
   truth (each question has known supporting atoms in the corpus).
2. For each question, compute cosine sim against every atom in its
   haystack. Bucket sims for (a) gold-evidence atoms and (b) random
   non-evidence atoms.
3. Plot the two distributions. Pick `medium` at the crossover where
   recall@medium covers ≥80% of gold atoms; pick `high` at the cosine
   value where precision becomes ≥90%; `low` is the noise floor below
   which essentially nothing is gold.
4. Sanity-check by re-running the bench with the new defaults and
   confirming no regression on the headline scores.

Optional follow-up: per-domain calibration. If indirect-question
domains (chat, social-feed retrieval) consistently sit lower than
encyclopedic-fact domains, the right answer may be a per-call
calibration rather than a global default.

**Effort.** 1 day. Step 2 is straightforward (we already have the
embeddings cached); step 3 is one Jupyter notebook; step 4 is one
bench run.

**Risk.** Low — this is a tuning exercise, not a code change. Worst
case the new defaults regress the bench and we revert to the current
values, having learned something about the sim distribution.

**Score expectation.** Likely flat on LongMemEval (current defaults
were probably tuned against this benchmark's question style). The
real win is for downstream agents like Mimir whose probes don't look
like LongMemEval's.

**Sibling finding (out of scope but record-worthy).** The Mimir debug
DB also revealed that `atom_relations` and several other migration-
created tables were missing entirely — `get_db()` only runs
`SCHEMA_SQL`, and `run_migrations()` is only invoked by `init_db.py`.
If a caller wires up the DB by hitting `/v1/store` directly (no
explicit init), consolidation will fail to write evidenced_by edges
silently. Worth either making `get_db()` run pending migrations on
first connect, or adding a startup check in `server.py` that bails if
the schema is below current. Tracking under a separate "harden DB
init" item, not P33. **Update: shipped in `8b326e6`.**

---

### P34 — Recalibrate consolidation similarity_threshold

**What.** Default `consolidation.similarity_threshold = 0.80` and
`min_cluster_size = 3` were inherited from early experiments. Cluster
analysis on Mimir's 90-atom corpus (where the user expected many
duplicates given the data) shows:

| threshold | clusters of size ≥2 | atoms covered |
|---|---|---|
| 0.85 | 0 | 0 |
| 0.80 (default) | 2 | 5 |
| 0.75 | 5 | 13 |
| 0.70 | 5 | 16 |

Real semantic duplicates in this corpus sit in the 0.75–0.79 band:
two pairs of FY27-budget atoms (sim 0.78), two pairs of Artemis II
splashdown atoms (sim 0.79), three AI-hallucination atoms (sim 0.75).
At default 0.80 they all miss; at 0.75 they all cluster correctly.
None of the 0.65–0.74 candidates were genuine duplicates on
inspection, so 0.75 looks like the right floor for this corpus.

**Why this is a real problem, not a one-off.** Consolidation gates
the entire two-tier boost mechanism. If the threshold is too tight,
no observations form, no boost lifts evidence atoms, and the bench
can't measure what two-tier is actually capable of. The Mimir DB
result is the clearest data point we have on where real duplicates
actually sit on the cosine axis.

**Caveat for LongMemEval.** The bench config already overrides to
`min_cluster_size = 3` and gets meaningful clusters. The bench may
be on the right side of this curve already. Worth confirming with a
similar pair-similarity histogram on a typical LME haystack DB.

**Proposed experiment.**

1. After a normal LME bench run, dump the active raw atoms +
   embeddings to a sidecar DB.
2. Compute pairwise cosine sim, histogram. Inspect the 0.70–0.85
   band: how many pairs are real duplicates (manual eyeball, ≤30
   pairs) vs. coincidental?
3. Pick a new default `similarity_threshold` at the boundary where
   real-duplicate fraction drops below ~50%.
4. Re-run the bench with the new threshold, confirm no regression.

**Connection to P33.** Both are calibration items on the same axis
(cosine sim distribution under text-embedding-3-small) but at
different operating points. P33 is about "is this atom relevant
enough to surface" (retrieval gate). P34 is about "is this atom a
duplicate of that one" (consolidation gate). The shapes are likely
similar — pick from a labelled dataset rather than guessing.

**Sibling finding (already addressed).** Mimir's TOML had
`cluster_similarity_threshold = 0.75` and `stability_reduction = 0.1`
as configuration intent — but the code reads `similarity_threshold`
and `stability_reduction_factor`. The misnamed keys silently fell
through to defaults, which is exactly what the new config-key
warnings (commit forthcoming) are designed to surface.

**Effort.** 1 day. Half for the analysis, half for the bench A/B.

**Risk.** Low — reverse the change if the bench regresses.

**Score expectation.** Net flat or small positive on LongMemEval.
The clearer benefit is for downstream agents like Mimir whose
domain produces real duplicates that the current threshold misses.

---

### P35 — Consolidation as the structured-cognition pass

**What.** Fold triple extraction (and eventually contradiction
surfacing, temporal tagging, quality grading) into the consolidation
LLM call. One prompt produces:

```
OBSERVATION:
<one or two sentences>

TRIPLES:
(subject, predicate, object)
(subject, predicate, object)
```

Persist the observation as an atom (as today) AND the triples linked
to that observation atom. Gate triples persistence on `[triples]
enable_extraction` so users who don't want them just write the
observation.

**Why it's the right architecture.** Consolidation already pays an
LLM call per cluster. The cluster IS the semantic batching — atoms
that should be reasoned about together. Triple extraction is the
same shape of cognition ("look at related text, extract structure"),
running on the same input atoms, yet today it's a separate LLM pass
(per-store originally, P7-batched per-question now). Doing it twice
is a layering mistake — the consolidation prompt is already shaped
for "look at these atoms, produce structured output."

**Cost analysis.**
- Today (post-P7): per-question cost is ~12 (triples) + ~10
  (consolidation) = ~22 LLM calls.
- Post-P35: ~10 (consolidation, with structured output). Triples
  become essentially free; the marginal output tokens are negligible
  vs. reasoning cost.
- Production case: agent stores ~10s of atoms per session. Per-store
  triple extraction = an LLM call per turn. Move to consolidation-
  time = one LLM call per cluster (per session boundary, or per
  scheduled run). Order-of-magnitude cost reduction.

**The previous P32 result was negative** (msam_p32_gptoss_v1 = 0.646,
-2.2pp vs baseline 0.668), with multi-session and knowledge-update
cratering. That measured "is triples-as-extra-pipeline worth it?"
The answer was no — too expensive for too little signal. The right
question is "is triples-as-consolidation-byproduct worth it?" That's
a much better trade and probably answers yes.

**Tradeoff.** Singleton atoms — facts stated in exactly one turn,
never clustered with anything — get no triples. Mitigation: regular
sem+kw retrieval still surfaces them by topical similarity. Real
"answer" turns usually appear amidst topical context (cluster
forms). Knowledge-update questions specifically benefit from
triples-of-clusters because OLD and NEW facts cluster together by
topic, and the synthesizer can emit both with temporal tags.

**Implementation:**

1. Update `ConsolidationEngine._synthesize_phase` prompt to request
   structured output (two sections: OBSERVATION, TRIPLES).
2. Parse both sections; on parse failure default to "no triples"
   (graceful degradation — the observation still lands).
3. In `_restructure_phase`, after storing the observation atom, call
   `store_triples_batch` with `triples.atom_id` set to the
   observation's atom_id.
4. Remove standalone `batch_extract_and_store` from the bench's
   `ingest_question` (triples now come from consolidation).
5. Keep `batch_extract_triples_llm` and the `/v1/triples/extract`
   endpoint as functions for ad-hoc callers.

**Future extensions** (separate experiments): fold contradictions
into the same prompt (`CONTRADICTIONS:` section emitting `supersedes`
edges within the cluster), temporal extraction (`valid_from` /
`valid_until` for the world model), per-cluster quality grading
(replaces P13's heuristic with LLM judgment that's already paid for).

**Effort.** 1 day to refactor + 1 bench to validate.

**Risk.** Low — consolidation already produces structured-ish text;
the LLM will handle the multi-section format. Worst case the parser
fails on some clusters; fallback writes observation only, same as
today.

**Score expectation.** Net positive on bench by at least the cost
delta — even if the additional triples don't lift retrieval,
removing the separate triple-extraction phase saves ~6h on a 500q
bench. If triples-of-clusters DO lift retrieval (especially for
knowledge-update where cross-time facts cluster well), bigger win.

---

### P15 — Evaluate cross-encoder rerank

**What.** Hindsight uses a cross-encoder reranker as their final stage
and credits it for some of their 91.4%. We have it in
`retrieval_v2.rerank_with_llm` (off by default, latency-gated). Either
revive on the modern path or formally delete.

**Effort.** 0.5 day to revive, 0.5 day to A/B test (one bench run).

**Risk.** High latency cost. Adds ~100–300ms per query and one LLM call
per question. Could push bench from ~44s/q to 60s/q.

**Decision criterion.** If A1–A3 + P4-bench together aren't already
beating P9v2, this is the next step. If they are, defer — the latency
cost matters in production.

---

## B. Lifecycle automation

### P16 — Internal scheduler for decay + consolidation

**What.** Add an optional asyncio-based scheduler that wakes on a
configured cadence and runs `run_decay_cycle()`, then optionally
`ConsolidationEngine().consolidate()`. Currently both are external-cron
or manual.

**Effort.** 1–2 days. Scheduler infrastructure (graceful shutdown,
tied to the REST server's lifecycle), config (`[scheduler] enabled`,
`[scheduler] decay_interval_hours`, `[scheduler] consolidate_after_decay`).

**Risk.** Low if gated default-off. Long-running asyncio tasks have
lifecycle questions (graceful shutdown, single-process semantics with
the existing uvicorn server).

**Why.** Currently every deployment re-implements this. Built-in
scheduler with sensible defaults removes a footgun and matches the
"sleep-inspired" framing.

---

### P17 — Trend column writer (or delete the dead reads)

**What.** The `atoms.trend` column is read by `hybrid_retrieve`
(multipliers for `weakening` / `stale`) but **nothing ever writes it**.
The original P4 spec called for trend computation in the decay cycle
from outcome history; we shipped P4-bench (supersedes) instead.

**Two options:**
1. Write the trend computer per the original P4 spec — requires
   `record_outcome` data, which currently has no automatic producer.
   In our benchmark this would produce ~zero signal.
2. Delete the dead reads from `hybrid_retrieve` and the column
   references. Trend stays in the schema (no migration cost) but no
   code pretends to use it.

**Recommendation.** Option 2 (delete). Revisit when we have outcome
data accumulating in real deployments.

**Effort.** 30 min for delete; 1 day for the writer.

---

### P18 — Re-evaluate P10 with the new boundary-atom filter

**What.** P10 (session boundaries + mark_contributions) regressed
single-session subtypes ~5pp in the P8 runs. We hypothesized boundary
atoms were crowding retrieval, and shipped a `source_type='session_boundary'`
filter on `retrieve()` / `hybrid_retrieve()` (commit `b978fe3`). Re-run
with `enable_session_boundaries = true` and the filter active to confirm
the regression is gone.

**Effort.** 0 day code (already shipped). One bench run.

**Risk.** Could still regress if `mark_contributions` is the actual
cause (not boundary atoms). Worth running in two stages: boundaries-on,
contributions-off; then both on.

**Why.** P10 has real value for production (prediction warmup gate,
`get_last_sessions` API). Worth confirming the filter fixed the bench
regression before declaring P10 dead.

---

## C. Dead-code cleanup

### P19 — Delete `retrieve_with_relations`

**What.** `core.py:3791` — pre-P4 supersedes demotion via subtractive
activation penalty. Now duplicates the multiplicative path I shipped in
`_apply_supersedes_demotion` (`24d47f9`). Only called from CLI
`cmd_relations`.

**Effort.** 1 hour. Delete the function, update the CLI to use the new
path or drop the CLI command entirely.

**Risk.** Low — behavior is now subsumed by `hybrid_retrieve`'s P4
demotion.

---

### P20 — Decide on `retrieval_v2.py`

**What.** After P11–P13 cherry-picks, the remaining ~700 LOC (beam
search, temporal filter, triple-augment, LLM rerank, embedding hot-swap,
feedback table) is parallel infrastructure to `hybrid_retrieve` that
doesn't compose with P9.

**Three options:**
1. **Delete entirely** after cherry-picks land. Reduces surface area,
   removes the duplication ambiguity.
2. **Keep as-is, flip prod default to `enabled = False`.** Code stays
   in case someone wants it. 5-min change.
3. **Merge fully into core.py with feature flags.** 3–5 days, large
   refactor.

**Recommendation.** Option 2 first (default flip). Commit to delete
after the next two bench runs prove we don't need it.

---

### P21 — Decide on `session_dedup.py`

**What.** Hour-windowed file-based "served IDs" tracking, used only by
the CLI `msam query` command. Confusing name overlap with the agent's
session_id concept.

**Three options:**
1. Delete entirely (`msam query` loses the `previously_served: True`
   annotation).
2. Rename to `cli_query_dedup.py` so the name reflects purpose.
3. Promote to a real per-agent dedup that affects `retrieve()` (heavier,
   design discussion).

**Recommendation.** Option 2 (rename). The functionality is fine for
CLI poking; the name is just misleading.

**Effort.** 30 min.

---

### P22 — Audit the compression / subatom subsystem

**What.** `msam/subatom.py` and the `[compression]` config block.
Disabled in bench (`enable_subatom = false`). Code exists but may be
bit-rotted (broken imports, references to removed functions).

**Effort.** 0.5 day to audit, decide, document.

**Outcomes:**
- If working: leave it, document when it would be useful (token-budget-
  constrained context assembly).
- If bit-rotted: delete or repair. Probably delete — the use case
  (compress atoms before sending to the reader) overlaps with what
  observations already do under P1/P9.

---

### P23 — Audit + prune CLI-only debug commands

**What.** ~17 `cmd_*` functions in `remember.py` exist purely for CLI
debugging (introspection, drift, confidence, analytics, explain,
provenance, quality, importance, merge, split, summarize, versions,
session-clear, grep, export, import, cache). Most have no REST endpoint
and no programmatic caller.

**Effort.** 1 day to audit which the user actually uses, prune the
rest.

**Risk.** Low if the user agrees they're unused; high if removing one
breaks an existing workflow.

**Recommendation.** Don't auto-prune. Surface the list, ask the user
which they actually use.

---

### P31 — Decide whether to clean up the single-tier retrieval path [decision: keep two-tier]

**What.** With the bench running exclusively in two-tier mode (P9 +
later improvements) and the new agent harness configured for two-tier,
ask whether the single-tier code path is still earning its keep.

**Single-tier surface area:**
- `hybrid_retrieve_with_triples` in `msam/triples.py` — wraps
  `hybrid_retrieve` (single-tier mode) and merges triples results.
- The `else` branch in `hybrid_retrieve` (`core.py:1486-1517`) —
  RRF combine + observation_bonus + sort path. Different from the
  two-tier branch, which uses `_two_tier_split`.
- The single-tier branch in `api_query` (`server.py`) — confidence-
  tier gating, atom-volume reduction, triples-merging.

**What single-tier provides that two-tier currently doesn't:**
- ~~**Confidence-tier gating.**~~ Closed in `c4f0cb0`. Two-tier now
  computes `confidence_tier` and the REST `/v1/query` two-tier path
  applies volume gating matching single-tier semantics, gated by
  `[retrieval] enable_confidence_gating` (default true).
- **Triples merged in the response.** Single-tier returns triples
  alongside atoms; two-tier returns `triples=[]` (intentionally — the
  triple pathway in two-tier is unwired by default).
- **Single flat list shape** for callers that don't want to model
  observations vs raws separately.

**Three options:**
1. **Keep single-tier, fix two-tier feature gaps.** Add confidence-tier
   gating to two-tier, add triples merging to two-tier. Then both
   paths offer feature parity; users pick by response shape preference.
2. **Delete single-tier, port the missing features into two-tier
   only.** All callers eventually move to two-tier; the bench already
   has, and the new agent harness will. Confidence gating becomes a
   property of the surfaced obs+raws set.
3. **Keep both, document the trade-off.** No code changes. Status
   quo. The cost is two parallel scoring paths to maintain forever.

**Effort.** 1-2 days for option 1 or 2. Zero for option 3.

**Decision criterion.** If, by the time the cherry-picks (P11-P13)
land, no caller is actively asking for the single-tier shape AND
two-tier has feature parity, option 2 (delete) is the right call.
Otherwise option 1 (keep both, fix gaps) is the conservative choice.

**Risk.** Medium for option 2 — we'd be removing a working public API
and any external caller relying on the single-tier shape would break.
Low for option 1 — additive feature parity work.

**Recommendation.** Defer. Revisit after P30 + cherry-picks land and
we have a clearer picture of which retrieval mode is the canonical
production answer.

**Decision (2026-04-26): keep two-tier as the canonical path.** Per the
agent harness work, two-tier is the production answer. The remaining
single-tier-only feature (triples merging in the response) tracks via
P32, which is independently scoped. Cleanup of the single-tier code
path itself becomes a follow-up to P32: once two-tier serves triples
in its response, single-tier is structurally redundant and can be
deleted. Bumping P31 from "decide" to "delete after P32 lands."

---

## D. Architecture / design questions

### P24 — Channels as first-class scoping (option B from prior memo)

**What.** Promote `channel` from "metadata field on session boundaries"
to a peer of `agent_id` — a denormalized column on every atom,
queryable filter on `retrieve()` / `hybrid_retrieve()`. The new agent
harness will likely span multiple channels (Slack DMs, email, CLI).

**Effort.** 1 day. Schema migration, denorm column, filter param
everywhere `agent_id` appears.

**Risk.** Cross-channel atom semantics question — should user
preferences shared across Slack and email be one atom or two? Design
decision, not just plumbing.

**Why.** The current channel field (only on session-boundary metadata)
is half-built. Either commit to it or rip it out.

---

### P25 — Reconcile three contradiction detectors

**What.** `find_semantic_contradictions` (embedding-distance, used by
P4-bench supersedes), `detect_contradictions` (triple-based, in
`triples.py`), `world_model.update_world` auto-close. Each was added
at a different time.

**Effort.** 0.5 day to document overlap and pick canonical, 1 day to
migrate callers.

**Risk.** Low. They serve different shapes (atoms vs triples), so
probably keep two but explicitly document when each fires.

---

### P26 — `mental_model` slot — commit or remove

**What.** `memory_type` enum has `'raw' | 'observation' | 'mental_model'`.
Only the first two are written. `HINDSIGHT-IDEAS.md` says mental_model
is "leaning toward never."

**Effort.** 5 min.

**Recommendation.** Delete from the migration enum unless we have a
concrete use case for a third tier. Keeping the slot as documentation
of "we considered this and decided no" is also fine if we add a comment.

---

## E. Diagnostics / observability

### P27 — Per-question_type supersedes effect-size measurement

**What.** Add to `metrics_<run>.jsonl`: `n_supersedes_demoted_in_topk`
(how many supersedes-tagged atoms made it into top-20) and
`mean_score_delta_for_demoted` (how much the multiplier actually changed
scores).

**Effort.** 0.5 day.

**Why.** We can see supersedes edges being written, but we can't
measure whether the demotion is actually doing anything in the final
top-20. This closes the loop.

---

### P28 — Capture skipped/merged cluster counts in bench metrics

**What.** `clusters_before_merge`, `clusters_merged`,
`clusters_skipped_existing`, `observations_superseded` — all returned
by `consolidate()` but only `clusters_consolidated` is currently written
to the bench metrics file.

**Effort.** 30 min.

**Why.** Free observability. Tells us whether the new idempotence
machinery (`34a4243`, `c6909a2`) is firing in production.

---

### P29 — "Why didn't this atom retrieve?" debugger

**What.** A new diagnostic primitive — given an atom_id and a query,
explain what step rejected it (similarity threshold, FTS miss, top-K
cut, supersedes demotion, source_type filter, observation gating, etc).

**Effort.** 1–2 days.

**Risk.** Significant code surface to instrument.

**Why.** When LongMemEval gets a wrong answer, we currently can't tell
whether the right atom was retrieved-but-ignored or never retrieved.
This would let us answer that.

---

## F. Open questions (revisit later)

### F1 — Auto-consolidation trigger inside decay cycle

**What.** Add `auto_consolidate_in_decay` flag (default off?). Decay
already has `_decay_lock`, so contention with manual `/v1/consolidate`
is handled.

**Why deferred.** Consolidation is LLM-bound; running it every decay
cycle gets expensive fast. Workload-dependent. Better as a scheduler
decision (P16) than a decay-cycle flag.

---

## Recommended order

If the goal is **bench score progress**, ship in this order:

1. **P30 (fix missing-atom base score in two-tier)** — ranking
   correctness bug; in-top-K vs missing atoms are scored asymmetrically.
   Smallest diff for option 1 (compute cosine for missing atoms).
   Started 2026-04-26; the next planned bench run.
2. **P11 + P12 + P13 (cherry-picks)** — composable with P9, low risk,
   modest score upside on different subtypes.
3. **P18 (re-run P10 with the new filter)** — zero code, one bench run,
   settles whether P10 was the regression cause.
4. **P19 (delete `retrieve_with_relations`)** — deduplication while
   P-related code is fresh in our heads.

(P14 — optimize supersedes resolver — removed from the bench-score path.
P4-bench result said the demotion approach itself was the wrong shape,
not the cost. Re-evaluate only if we revisit atom-level supersedes with
query-type-aware demotion.)

If the goal is **codebase hygiene**, ship in this order:

1. **P20 (default-flip retrieval_v2)** — stops the parallel pipeline
   from being the advertised default.
2. **P21 (rename session_dedup)** — naming clarity.
3. **P17 (delete trend reads)** — remove dead column references.
4. **P26 (delete mental_model slot)** — also dead.

If the goal is **production-readiness for the new agent harness**:

1. **P24 (channels first-class)** — your harness will need this.
2. **P16 (internal scheduler)** — decay + consolidation cadence baked
   in.
3. **P14 (optimize supersedes)** — production deployments will care
   about latency.

The bench-score order is the highest-information path right now (we're
already running benches). The hygiene work is cheap and clears the deck
for D-track and B-track work later.

---

## Bookkeeping

When proposals from this list are taken on, append the run/result to
`BENCHMARK-RESULTS.md` (for score-affecting changes) and link the
commit hash. Mark the proposal entry above as `[done in <commit>]` so
this file stays current.
