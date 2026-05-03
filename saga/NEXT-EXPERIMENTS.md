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
- **P14 (optimize supersedes resolver)** — **dropped 2026-05-02.** Atom-level
  supersession is decided dead — no plan to revisit. P14 is moot.
- **Skip-on-identical and superset-supersedes for observations** shipped
  in `34a4243` and `c6909a2`. Both safe defaults.
- **2026-05-02 sweep.** Several items closed out via the saga code review
  + integration-bench sessions. See the per-item status blocks below for
  details, but the headline:
  - **Shipped**: P32 (triple extraction in consolidation), P34 (similarity
    threshold 0.80→0.75), P37(a) (temporal valid_from/valid_until),
    P42 (triples in /v1/query response), P46 (sentence splitter).
  - **Tested + neutral/regressed, kept behind flag**: P37(b)
    (world_model_pathway, -0.8pp), P38 (HyDE, -2.2pp), P41
    (triple_augment_v2, neutral), P43 (subatom beam, flat), P36
    (graph weight 0.3, -1.4pp).
  - **Decided not to do**: P14 (atom supersession dead), P15 (agent
    handles rerank in its prompt context), P16 (mimir-side cron handles
    cadence), P26 (mental_model removed from documented vocabulary).
  - **Decided differently**: P18 (we keep boundaries off in bench;
    mimir surfaces the previous N session boundary atoms in the turn
    prompt instead).

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

**Status.** **Triple extraction shipped 2026-04-30** as part of
consolidation (`consolidation.py:_parse_consolidation_output` parses
`OBSERVATION + TRIPLES`; `_persist_consolidation_triples` writes
embedded triples). The graph-pathway side was tested separately as P36
(see below) and regressed at `rrf_graph_weight=0.3`. So **extraction is
done; the graph pathway as a co-equal RRF ranker is not the right
shape**. Current canonical ships extraction-on, graph-pathway-off.

**What.** (original — kept for context) Triple extraction is partially wired:

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

### P33 — Recalibrate confidence_sim_{high,medium,low} thresholds [shipped 2026-04-28]

**Result.** Offline analysis on 50 LongMemEval questions
(`p33_threshold_analysis.py`, 97 gold atoms, 24,872 noise atoms) showed
the gold/noise sim distributions are well-separated (gold median 0.44,
noise median 0.10). Threshold sweep:

| thr | gold ≥ thr | noise ≥ thr | precision | recall |
|---|---|---|---|---|
| 0.50 | 24 | 46 | 0.343 | 0.247 |
| 0.45 (was high) | 40 | 106 | 0.274 | **0.412** |
| 0.40 (new high) | 65 | 191 | 0.254 | **0.670** |
| 0.30 (medium, unchanged) | 89 | 660 | 0.119 | 0.918 |
| 0.20 (new low) | 97 | 2442 | 0.038 | 1.000 |
| 0.15 (was low) | 97 | 5683 | 0.017 | 1.000 |

**Shipped defaults (commit 2026-04-28):**
- `confidence_sim_high`: 0.45 → **0.40** (recall +25pp; the old default
  was missing 60% of gold-evidence atoms)
- `confidence_sim_medium`: 0.30 → unchanged (already at 92% recall)
- `confidence_sim_low`: 0.15 → **0.20** (same 100% recall, half the
  noise — old 0.15 was an over-permissive noise floor)

**Caveat from the original spec.** Bench results don't bear on this
calibration — `_confidence_tier` is set on atoms by retrieval but the
LongMemEval bench harness bypasses both api_query (which filters by
tier) and the single-tier output volume gate. The data motivating the
change is the offline gold/noise distribution analysis.

**Why this matters.** Affects the REST `/v1/query` per-atom filter
when callers pass `min_confidence_tier`, and the single-tier bucket
output volume gating. Downstream agents like Mimir whose probes were
hitting the old over-strict 0.45 floor will now see appropriate
"high"-tier atoms surface.

---

### P33 (original proposal — preserved for context)

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

### P34 — Recalibrate consolidation similarity_threshold [shipped 2026-05-02]

**Status.** **Shipped 2026-05-02.** Default lowered from 0.80 → 0.75
in `saga/config.py`. Decision driven by post-bench measurement on
mimir's atom corpus: 0.80 missed clusters of paraphrased observations
that should have merged; 0.75 caught them without false-merging
unrelated atoms in spot-checks. `min_cluster_size = 3` left at the
default. The bench correctness cross-check rides on the next
P30-baseline run.

**What.** (original — preserved for context) Default
`consolidation.similarity_threshold = 0.80` and `min_cluster_size = 3`
were inherited from early experiments. Cluster analysis on Mimir's
90-atom corpus (where the user expected many duplicates given the
data) shows:

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

### P35 — Consolidation as the structured-cognition pass [partially shipped]

**Status (2026-05-02).** Mostly shipped. The consolidation prompt
already produces:

- ✅ **OBSERVATION** (the synthesis text)
- ✅ **TRIPLES** (S/P/O extraction with embeddings)
- ✅ **valid_from / valid_until** temporal tagging (P37(a), shipped)
- ✅ **supersedes** detection (consolidation writes the
  `'supersedes'` `atom_relations` row directly)
- ✅ **trend** labeling — folded into the consolidation pass via
  access-log-driven heuristic (planned 2026-05-02; see P17 below)

What's **not** in the prompt yet:

- ❌ **CONTRADICTIONS** section — the LLM has all source atoms in
  context; emitting "atoms X and Y disagree on Z" as a separate
  output section is pure additive (no retrieval impact). Feeds the
  P25 reconciliation work. **Recommended: ship next.**
- ❌ **Quality grading** per observation — deferred until there's a
  concrete consumer. Triples already carry confidence; observation-
  level quality would need a clear retrieval-side use before
  maintaining it earns its keep.

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

### P36 — Lower `rrf_graph_weight` to test graph pathway as tiebreaker [tested, doesn't help]

**Status.** Tested at `rrf_graph_weight = 0.3` (`msam_p36_canon_v1` =
0.760 vs ~0.774 baseline → **−1.4pp**). The graph pathway as an RRF
ranker just doesn't earn its keep at any weight tested. Decision:
leave at 0.7 in code; do **not** spend more bench cycles tuning the
weight. The graph-pathway-as-ranker shape is wrong; if we revisit,
it should be a separate experiment that surfaces graph-linked atoms
as a *supplement* (like P42's triples block) rather than competing
in RRF.



**What.** P32 and P35 both regressed ~-2.5pp when the graph pathway
joined RRF as a fourth ranker. Graph pathway runs at default
`rrf_graph_weight = 0.7` (vs 1.0 for sem and kw). Hypothesis: 0.7 is
too much voice for a noisier ranker; the graph pathway should be a
tiebreaker, not a vote.

**Proposed sweep:**
- `rrf_graph_weight = 0.3` — graph influences ordering only when
  sem+kw produce a tie or near-tie
- `rrf_graph_weight = 0.5` — moderate influence
- (already measured) `rrf_graph_weight = 0.7` — current default

Run on the canonical stack with P35 features ON (triples-as-
byproduct, graph pathway on). Tag `msam_p35_canon_lowgraph_v1` etc.

**Why it might rescue triples:** the graph pathway's atoms are noisier
than direct atom embeddings (triples synthesize away per-turn
specificity). At weight 0.7, even mediocre triples shift rankings.
At 0.3, only consistently-strong graph signals matter — sem+kw stays
the dominant signal, graph just breaks ties when both other pathways
are confused.

**Effort.** 1-2 bench runs (canonical stack, P35 features ON, vary
the weight).

**Risk.** Low — pure tuning, no code change beyond editing the bench
config.

**Score expectation.** If graph pathway is the regression source,
weight=0.3 should partially recover the -2.6pp gap. If it doesn't
recover, triples themselves are the problem (irrespective of fusion
weight) and we close the question definitively.

**Connection to P32/P35.** This is the last untested hypothesis on
why triples regress. If P36 doesn't help, ship triples + graph
pathway as a production-only feature (off-by-default) and stop
benchmarking them on LongMemEval.

---

### P37 — Temporal world model: explicit pathway separate from graph

**What.** The temporal world model API exists (`update_world`,
`query_world`, `world_history` in `triples.py`; `valid_from` /
`valid_until` columns in the triples schema since `8b326e6`) but has
never been wired into retrieval. Two ways to do it:

**a. Implicit (consolidation-time tagging).** Extend the P35
structured-output prompt to emit `valid_from` / `valid_until` on
triples when atom dates indicate fact evolution. The synthesizer
already sees cluster atoms with `[YYYY-MM-DD role]` prefixes — it
can output time-bound triples directly into the world model.
`auto_close_on_conflict` fires naturally on the new emissions.

**b. Explicit (separate retrieval pathway).** Add
`enable_world_model_pathway` to retrieval. At query time:
- Detect entities mentioned in the query (existing
  `extract_query_entities` helper)
- For each, call `query_world(entity)` to get current-state triples
- Emit those triples' source atoms as a fifth RRF ranked list

Option (a) needs (b) to be useful (no point producing temporal
triples if nothing reads them). But (b) can run on existing
update_world calls (production users who explicitly populate the
world model) without (a).

**Why this is its own experiment, not part of P35.** The P35 graph
pathway just regressed -2.6pp on canonical. Temporal-tagged triples
would flow through the same graph pathway and inherit that
regression. A separate world-model pathway is a different code path
— it queries by entity rather than by triple-embedding-similarity,
and it's filtered to current-state triples by construction (no
stale-fact noise). Clean A/B vs the failed graph pathway.

**Where it might lift the bench:**

- **knowledge-update (-3.8pp on P35 canon)** — fact-replacement
  questions are the world model's home turf. "When did the user
  change jobs?" → `world_history(User)` returns the timeline.
- **temporal-reasoning (-0.7pp on P35 canon)** — most LongMemEval
  temporal questions are "how long ago" / "earlier today" style;
  the world model probably doesn't help those. Some are "did X
  happen before Y" which the timeline does help.
- **multi-session (-4.5pp on P35 canon)** — the world model could
  help cross-session entity questions ("what did the user mention
  about their job across our conversations?") via `query_world` on
  the entity. Different shape than triple-similarity-search.

**Where it won't help:** single-session subtypes (the answer is in
one session, no temporal reasoning needed) — should be flat.

**Pre-req: P36.** If lowering `rrf_graph_weight` rescues triples
(P36 result), then implicit temporal tagging via the existing graph
pathway becomes worth trying — option (a) on top of P36's tuning.
If P36 doesn't rescue, only option (b) is on the table.

**Effort.** Option (b) is ~1 day: detect entities, call query_world,
add as RRF ranker, test. Option (a) on top is ~0.5 day to extend
the consolidation prompt.

**Risk.** Low for option (b) — pure additive, off by default. Worst
case it's flat and we ship as production-only. Medium for option (a)
because it stacks on P35's already-questionable graph pathway.

**Score expectation.** Knowledge-update is the most likely
beneficiary (+1 to +3pp). Multi-session second (could go either
way). Other subtypes flat.

**Connection to production users.** Even if this lever is flat on
LongMemEval, the world model is genuinely useful for production
agents that need fact-evolution audit ("when did the user start
preferring X?"). Test with eyes open — neutral on the bench is
fine for a feature whose primary value is non-bench.

---

### P38 — Confidence-gated HyDE (escalate to hypothetical-doc embedding only when first pass is weak)

**Status.** Bench-tested 2026-04-29: **regressed -2.2pp on canonical
(0.762 vs P30v3 0.784).** Code stays behind `enable_hyde=false`
default for production deployments that may have a different
question/answer-shape profile. Bench config will revert. Full
post-mortem in BENCHMARK-RESULTS.md §msam_p38_canon_v1; short
version: P33's question/answer shape-gap analysis didn't predict
retrieval outcomes — the cohorts expected to gain the most
(multi-session, knowledge-update) lost the most. The hypothetical-
answer pathway shifts retrieval toward the LLM's prior rather than
toward the user's specific facts, and RRF blending can bury clear
top-K gold matches under a noisier consensus. P38 closes the
"shift the query embedding via LLM" probe line on this benchmark.

**What.** Standard HyDE replaces the query embedding with the
embedding of an LLM-generated hypothetical answer. Cheap-path-first
HyDE wraps that in a confidence gate:

```
Stage 1: hybrid_retrieve(original_query)
Stage 2: if max(top_K._similarity) >= hyde_trigger_confidence:
             return  # cheap path was good enough
Stage 3: hypothetical = LLM("Write a 1-2 sentence answer to: {query}")
         hybrid_retrieve(hypothetical)
         (optionally: RRF-fuse both passes)
```

The agent pays for HyDE's extra LLM call only when the first pass
produces no confident match. P33 data says ~67% of queries find a
gold atom at sim ≥ 0.40 on the first pass — those don't need HyDE.
The ~33% that don't are exactly where HyDE would help most.

**Why this is the right shape for LongMemEval.** P33 showed gold
atoms median sim only 0.44 to their queries. The question-vs-answer
shape mismatch is real:

| Text | Sim to gold atom (typical) |
|---|---|
| Question ("What degree did I graduate with?") | 0.44 |
| HyDE hypothetical ("I graduated with a Business Administration degree.") | 0.70+ |
| Gold atom ("[user] I graduated with a degree in Business Administration...") | — |

Even if the LLM guesses the wrong degree, the hypothetical lives in
**answer-shape** — same syntactic register as real atoms — and the
embedding distance is dominated by shape, not content.

**Why gate it.** Two reasons:

1. **Cost in production.** An always-on HyDE adds an LLM call per
   query forever. For an agent making 100s of memory queries per
   session, that's real. The gate only escalates when needed.
2. **Bad hypotheticals can hurt.** If the LLM hallucinates an
   off-topic answer, retrieval shifts toward that. Gating on
   first-pass confidence means we only pay the bad-hypothetical
   risk on queries where the cheap path already failed — there's
   not much to lose.

**Connection to P11 (query rewriting).** P11 alone scored -1.3pp
because regex rewrites apply uniformly even when not helpful. Gated
HyDE is the higher-quality version of the same idea: **don't
rewrite when retrieval is already confident.** If we ship gated
HyDE, P11 becomes mostly redundant — could either retire it or
gate it the same way (run only when first pass is weak).

**Implementation:**

1. New helper `_hyde_query(query)` in `core.py`:
   ```python
   def _hyde_query(query: str) -> str | None:
       if not _cfg('retrieval', 'enable_hyde', False):
           return None
       llm = resolve_llm_config('retrieval_v2')
       if not llm['api_key']:
           return None
       prompt = (
           "Write a 1-2 sentence hypothetical answer to this question, "
           "in the voice of a user describing themselves or an "
           "assistant providing the fact. The answer doesn't need to "
           "be factually accurate — write it in conversational "
           "answer-shape, not as a question.\n\n"
           f"Question: {query}\n\nHypothetical answer:"
       )
       # POST to llm['url'], return text or None on failure
   ```

2. In `hybrid_retrieve`, after the first-pass result is built but
   before two-tier split / supersedes:
   ```python
   max_sim = max(
       (r.get("_similarity", 0) for r in combined.values()),
       default=0.0,
   )
   trigger_thr = _cfg('retrieval', 'hyde_trigger_confidence', 0.45)
   if max_sim < trigger_thr:
       hyp = _hyde_query(query)
       if hyp:
           # Re-run semantic pathway with HyDE'd embedding,
           # keep keyword pathway on original (BM25 needs question
           # vocabulary), RRF-fuse the result with the first-pass
           ...
   ```

3. Two new config keys:
   - `[retrieval] enable_hyde` — defaults False
   - `[retrieval] hyde_trigger_confidence` — defaults 0.45 (matches
     the new P33 high-tier threshold)

4. RRF fusion strategy: combine first-pass + HyDE-pass results via
   RRF, so atoms that score well in either pass surface. Don't
   replace; augment.

**Effort.** 1 day for the gated implementation + 1 bench run.

**Risk.** Low — gated. Worst case the LLM call is wasted on
already-confident queries (avoided by the gate) or produces noise
(absorbed by RRF fusion with the first pass).

**Score expectation.** This is the highest-expected-value untested
lever for LongMemEval. The P33 mismatch data directly motivates it.

- multi-session is the most likely beneficiary (these queries have
  the lowest first-pass sim — P33 showed median 0.41 there). HyDE
  could move them up substantially.
- knowledge-update second (median 0.49 on P33) — HyDE helps when
  the question doesn't lexically match the answer's phrasing.
- single-session-* probably already at the ceiling for the cheap
  path, won't trigger the gate often.

**Relationship to other levers:**
- Composes with rerank (P15): better candidates → better rerank input
- Composes with triples (P32/P35): orthogonal — HyDE shifts the
  semantic pathway; triples add a separate one
- Mostly subsumes P11 (query rewriting): gated HyDE is the
  smarter version of "modify the query when retrieval is weak"

If gated HyDE lifts the bench by even 1pp, it's the first lever
that's earned its keep on this benchmark since P30. If it doesn't,
the question-shape gap may be inherent and we're at LongMemEval's
ceiling for this architecture.

**File location:** new function in `core.py` next to
`_apply_query_rewriting`. Tests for the gating logic and for the
HyDE LLM call (mock the LLM, verify gate triggers correctly).

---

### P39 — Pulled-in raw scoring is anchored to bottom of pool; consider raising it

**Status.** Implemented + bench-tested 2026-04-29. **Median pivot
is a real positive lever**: +16.7pp preference, +3.0pp multi-session
vs the same-day P12_v2 baseline (the cohorts P39's spec predicted).
Costs -2.6pp on knowledge-update (fact-replacement) due to
superseded atoms getting pulled in.

**Not yet shipped to canonical.** Two reasons:
1. The P12_v2 re-baseline regressed unexpectedly (0.762 vs P12_v1's
   historical 0.792, with preference collapsing 0.483 → 0.200) —
   need to investigate before claiming P39's win is durable.
2. The knowledge-update tradeoff is real and worth a follow-up
   that gates the median pivot on whether the endorsing observation's
   evidence is current (no superseded atoms).

See BENCHMARK-RESULTS.md §msam_p39_canon_v1 for full data and the
side-by-side with P12_v2.

**What.** In `_two_tier_split`, observation-endorsed raws that miss
the cheap-path candidate pool (the `missing_ids` branch in
`msam/core.py`) get a base score of `ref_score × sim(query, R)`,
where `ref_score = min positive RRF score in the in-pool raws`.
Their cap is then `2 × base`. A pulled-in raw at sim 1.0 (perfect
query match the cheap path missed) caps at exactly `ref_score`'s
own value — which equals the *worst* in-pool raw's base score.
After boosting, the pulled-in's final score is roughly half of
that same in-pool raw's final score.

In numbers, with one endorsing observation at score 0.030 and
ref_score 0.005:

| Atom | Base | Cap | Final |
|---|---|---|---|
| In-pool raw, RRF base 0.010 (worst-ranked) | 0.010 | 0.020 | **0.030** |
| Pulled-in raw, sim 1.00 | 0.005 | 0.010 | **0.015** |
| Pulled-in raw, sim 0.50 | 0.0025 | 0.005 | **0.0075** |

A pulled-in raw with a perfect query match scores half of the
worst in-pool raw. With `top_k_raws = 20` in the bench, pulled-ins
are routinely sorted below 20 in-pool raws and dropped silently —
even though the consolidation system's `evidenced_by` edges are
specifically saying "include this atom because it's part of a
coherent pattern that matters here."

**Why this matters.** The pulled-in branch exists for the
preference / multi-session failure mode: gold atom is an
off-handed user statement ("I really love X") that doesn't match
the question phrasing ("does the user prefer X?") at the cosine
level, but consolidation reliably groups such statements into a
"user prefers X" observation. P30v3 preference subtype = 0.267 —
worst across all subtypes — suggesting the gold raw atoms aren't
surfacing despite the observation pathway working. Multi-session
shows similar drift (P30v3 = 0.647 vs P9v2 = 0.669). These are
exactly the cohorts where pulled-in scoring should help most.

**Why not implement immediately.** P30v2 already tested a more
aggressive scheme (flat 2× restoration replacing the additive
boost) and lost preference -16.7pp. The current conservatism has
been A/B-tested and won. So this is a real experiment with real
risk of regression.

**Options to test, ordered by blast radius:**

1. **Raise `ref_score` to median** of the in-pool RRF distribution
   instead of `min`. Roughly doubles pulled-in bases and caps.
   Cleanest single-knob change.

2. **Floor the cap.** Change `cap = max(2 × base, fixed_min)` so
   the cap doesn't collapse for low-base atoms. A pulled-in with
   sim 0.50 still gets meaningful boost-driven lift even though
   its base is tiny. More targeted: only changes pulled-in
   behavior, doesn't change in-pool weak-raw behavior.

3. **Decouple from `ref_score` entirely.** Use
   `base = sim × max_in_pool_rrf` so a sim-1.0 pulled-in matches
   the *top* of the pool, not the bottom. Most aggressive —
   closest to "trust the observation endorsement as much as the
   cheap-path ranking."

**Implementation (option 1 — recommended starting point):**

In `_two_tier_split` around line 1403:

```python
in_pool_scores = [s for _, s in raw_ranked if s > 0]
if in_pool_scores:
    sorted_scores = sorted(in_pool_scores)
    ref_score = sorted_scores[len(sorted_scores) // 2]  # median
else:
    ref_score = 0.01
```

One new config key for the experiment:
- `[retrieval] missing_ref_score_pivot` = `"median"` | `"min"`
  (default `"min"` for back-compat; bench config flips to
  `"median"` for the P39 variant).

**Effort.** ~1 hour for option 1 + 1 bench run.

**Risk.** Medium — the conservatism is empirically load-bearing
on preference (P30v2 lost it). The mitigation is the per-atom
confidence_tier filtering layered on top: any pulled-in raw still
has to clear `min_confidence_tier` (default "low" → sim ≥ 0.20),
so we're not flooding the response with low-quality endorsed
noise.

**Score expectation.** If pulled-ins are currently being silently
dropped by the score math, this should lift multi-session and
single-session-preference. If they're being correctly dampened
because endorsement noise is high, this regresses the same
subtypes — same A/B answer either way.

**Connection to other levers:**
- Composes with P12 (synonym expansion). P12's +21.6pp preference
  gain was on the keyword pathway alone — adding pulled-in
  weight could compound there if P12's keyword expansion is
  what's filling the cheap-path pool with the right atoms.
- Orthogonal to triples / graph pathway (those are dead anyway).
- Doesn't touch the cap formula; just where `ref_score` lands.

**File location:** `msam/core.py:_two_tier_split` around line 1403
(the `in_pool_scores` / `ref_score` assignment). Tests should
cover the math: synthetic atoms with known RRF scores, verify
pulled-in atoms rank where the formula predicts.

---

### P40 — Disable pull-in of endorsed atoms that miss the cheap-path pool; keep boost for in-pool endorsed atoms [queued 2026-05-02]

**Status.** Queued 2026-05-02 — recommended next-bench candidate
after the current P30 vs P42 Sonnet run completes. Hypothesis still
holds: pulling in endorsed-but-out-of-pool atoms anchors them at the
bottom of the score distribution and dilutes the in-pool ranking.



**Status.** Filed 2026-04-30. Not yet implemented. Queued behind
the P12 v3 solo re-run.

**What.** `_two_tier_split` does two things with `evidenced_by`
endorsements right now:

1. **Boost** in-pool raws (raws already in the cheap-path candidate
   pool) by adding `2 × obs_score` to their RRF score, capped at
   `2 × base`.
2. **Pull in** missing endorsed raws (atoms not in the cheap-path
   pool but reachable via observation endorsement), score them
   with a synthetic base `ref_score × sim`, then boost the same way.

P40 keeps step 1, drops step 2. Endorsement only ever affects atoms
that the cheap path already surfaced. Atoms that missed the cheap
path don't get a synthetic base score injected.

**Why test this.** P39's analysis showed pulled-in atoms score so
low that they rarely make the top-K cut even with the median pivot
(at `min` pivot they essentially never do). And the P39 bench
showed knowledge-update -2.6pp because the median pivot's looser
threshold pulls in superseded atoms tied to outdated facts. A clean
A/B between P30v3 (current canonical, pull-in enabled) and P40
(pull-in disabled, boost-only) tests:

- Whether the pull-in branch is actually contributing recall on the
  cohorts P30 was designed to help (preference, multi-session) —
  if it is, P40 should regress those subtypes.
- Whether knowledge-update recovers when superseded atoms can't
  sneak in via endorsement edges.
- Whether the boost-on-in-pool-only mechanism is enough on its
  own — simpler code path, fewer ways to introduce noise.

**Implementation:**

In `_two_tier_split`, gate the missing-atom branch on a new flag:

```python
# Around line 1391 (the `if boost_map:` that triggers pull-in)
if boost_map and _cfg('retrieval', 'enable_missing_atom_pull_in', True):
    missing_ids = [aid for aid in boost_map if aid not in raw_score_map]
    if missing_ids:
        ... existing pull-in branch ...
```

One new config key:
- `[retrieval] enable_missing_atom_pull_in` — default True
  (back-compat with P30v1/v3); bench variant flips to False.

**Effort.** 30 min for the gate + 1 bench run.

**Risk.** Low. The boost-only path is a strict subset of current
behavior. Worst case: regresses preference / multi-session by the
amount the pull-in was actually contributing. Either way, clean
signal.

**Score expectation.** Three plausible outcomes:
1. **P40 ≈ P30v3 (within noise).** Pull-in wasn't actually doing
   much because its scores were anchored too low to clear top-K
   anyway. Confirms P39's diagnosis from a different angle.
2. **P40 < P30v3 on preference / multi-session.** The pull-in does
   contribute recall on those cohorts; P39's median pivot was the
   right intervention.
3. **P40 > P30v3 on knowledge-update.** Removing the pull-in
   prevents superseded-atom interference. Could compose with P39
   later (median-pivot for endorsement boost, no pull-in).

**Connection to other levers:**
- Composes with P39 (median pivot) — P40 + P39 would mean: in-pool
  endorsed atoms get the bigger pivot-based boost, but no missing
  atoms come along for the ride. The boost cap (`2 × base`)
  applies to bigger bases, lifting in-pool scores meaningfully.
- Doesn't touch the LLM stack — pure scoring change.
- Can ship behind a flag without changing prod behavior.

**File location:** `msam/core.py:_two_tier_split` around line 1391
(the `if boost_map:` that triggers the pull-in branch). Tests
should cover both flag states and confirm that endorsed-but-
missing atoms simply don't appear in the response when the flag
is off.

---

### P41 — Triple augmentation v2: embedding-cosine match + proper score scaling

**Status.** Bench-tested 2026-04-30 (run as P43+P41 alongside P43
solo). Standalone-equivalent delta: −1.4pp vs P43 baseline,
−1.4pp vs P30v3 canonical. Per-subtype isolated effect:
knowledge-update **+2.6pp** ✓, single-session-assistant
**−3.6pp**, multi-session **−4.5pp**. P41 specifically rescues
the fact-replacement cohort that subatom hurts, but the
multi-session crowd-out cost dominates the overall delta.

Code stays behind `enable_triple_augment_v2 = false` (default) for
production deployments where the query mix differs. Not shipped to
canonical. Full data + analysis: BENCHMARK-RESULTS.md
§msam_p43_p41_canon_v1.

**What.** The current `enable_triple_augment` (in `retrieval_v2.py`)
has two design problems:

1. **Brittle entity extraction.** `extract_query_entities` uses
   regex on capitalized words + a tiny hardcoded entity dict
   (`user`, `agent`, `msam`, `openclaw`). NER-free, embedding-
   free. Misses anything not in title case or in the dict.
2. **Flat score baseline.** Triple-augmented atoms get
   `_combined_score = 2.0` regardless of how strong the triple's
   relevance to the query actually is. Either dominates RRF
   results (when other RRF scores are <1) or is invisible (when
   they're >2). No graceful degradation.

P41 replaces both with the same pattern semantic atom retrieval
already uses: triples are already embedded
(`_embed_triple_safe` writes `embedding` column from
`subject + predicate + object`), so we can do nearest-neighbor
search directly on triples for any query.

**Mechanism.**

```python
def triple_augment_v2(query, query_emb, top_k=5):
    # 1. Cosine match query_emb against all active triples' embeddings.
    # 2. Take top_k triples by cosine.
    # 3. For each, follow triple.atom_id → the atom that produced it.
    # 4. Score that atom proportional to triple cosine, NOT a flat 2.0.
    #    Calibrate to the in-pool RRF distribution so scores are
    #    comparable (similar to P30's missing-atom math).
```

Concretely, scale via `score = sim(triple, query) × ref_score` where
`ref_score` is the median in-pool RRF (matching P39's calibration
approach). That makes triple-augmented atoms compete with mid-rank
in-pool raws when their triple is highly relevant, and degrade
gracefully when it isn't.

**Why this is better.**
- No regex / no NER / no hardcoded entity list. Works for any
  deployment vocabulary.
- Reuses the `triples.embedding` column we already maintain
  (no schema change).
- Score scaling matches the rest of the retrieval math.
- Degrades to no-op when there are no triples in the DB (current
  behavior) — but more usefully when triples exist.

**Connection to P42.** P41 surfaces the *atoms* triples point at
(via `atom_id` foreign key); P42 surfaces the **triples themselves**
as a separate response block. They share the embedding-cosine
ranking infrastructure.

**Effort.** 1 day implementation + tests + 1 bench run.

**Risk.** Low–medium. Replaces a known-broken mechanism with a
better-designed one. Bench risk: if the triples-as-augmentation
hypothesis was wrong (P32, P35, P36 all regressed when triples
joined RRF as a pathway), then this might also regress —
augmenting via atom is a different pattern from RRF-as-pathway,
but the underlying signal is the same.

**File location:** rewrite `triple_augmented_retrieve` in
`msam/retrieval_v2.py:36`. Drop `extract_query_entities` calls
within it. Tests for cosine ranking, score scaling, and no-op
when triples table is empty.

**New-entity handling.** Triples are embedded at write time via
`_embed_triple_safe(subject, predicate, object)`, so any newly
extracted triple is searchable on the next query — no re-index,
no maintenance, no entity-vocabulary to track.

**Limitation: first-time mentions miss the triple pathway.**
Triples capture entities only after consolidation runs and
clusters them (`min_cluster_size = 3` default). When a user first
mentions "my co-worker Sarah", there's no Sarah-triple yet — so
embedding-cosine match returns nothing for Sarah-related atoms
via this path. The cheap path (semantic + keyword via
`hybrid_retrieve`) handles first-time mentions correctly; this is
working-as-designed, not a bug. The lag between first mention and
triple availability depends on consolidation cadence + cluster
threshold. Triples earn their keep by surfacing *correlated*
atoms across many sessions, which inherently requires
consolidation to have noticed the pattern. Accept the lag; first-
time atoms are handled by the cheap path anyway.

---

### P42 — Triples as a third response block on `/v1/query` [shipped 2026-05-02]

**Status.** Shipped 2026-05-02. `query_triples_for_response()` in
`triples.py` cosine-matches the query embedding against active
triples, filters expired (`valid_until < now`), returns top-K with
**subject / predicate / object / valid_from / valid_until /
confidence / _similarity / source_atom_id**. Wired into
`server.py` two-tier path behind `[retrieval]
include_triples_in_response = false` (canonical default). Mimir's
pre-message hook reads the new block and renders triples as a
`Triples:` sub-section beneath atoms; source_atom_ids flow into
contribution credit. Bench: P30 vs P42 with Sonnet 4.6 reader is
running as of 2026-05-02 22:34.



**Status.** Filed 2026-04-30. Not yet implemented.

**What.** The two-tier `/v1/query` response shape already has a
`triples: []` slot — currently always empty. P42 populates it
with the top-N most relevant active triples for the query, ranked
by embedding cosine. The reader sees compact factual triples
alongside observations + raws and can ground answers more
directly.

**Mechanism.**

```python
# In api_query, after _two_tier_split:
if _cfg('retrieval', 'include_triples_in_response', False):
    candidate_triples = embedding_cosine_match(
        query_emb,
        active_triples,
        top_k=_cfg('retrieval', 'response_triples_top_k', 5),
    )
    # Optional weighting: cosine × confidence column,
    # tiebreaker created_at desc.
    response['triples'] = [_format_triple(t) for t in candidate_triples]
```

**Ranking.** Primary signal is cosine similarity of query
embedding to triple embedding (already populated by
`_embed_triple_safe`). Then optionally:
- × `confidence` column (default 1.0; useful when triples come
  from sources of varying reliability)
- recency tiebreaker (newer wins ties)
- skip triples whose `valid_until` has expired (handled via
  `query_world(include_expired=False)` — same path)

**Scoring triples in the response.** Return both the cosine score
and the source `atom_id` so the caller / reader can backtrack to
the originating atom if it wants more context. Format example:

```json
{
  "subject": "user",
  "predicate": "profession",
  "object": "software_engineer",
  "valid_from": "2024-03-15T...",
  "valid_until": null,
  "confidence": 0.92,
  "_similarity": 0.71,
  "source_atom_id": "abc123..."
}
```

**Auto entity detection — solved by embedding cosine.** Don't try
to extract entities from the query and look them up in
`triples.subject`. Just compute cosine on triple embeddings — the
match is "this triple's subject+predicate+object embedding is
close to the query's embedding," which works without explicit
entity matching.

**`valid_until` population — currently mostly NULL.** The
consolidation prompt asks for `(subject, predicate, object)` only
(msam/consolidation.py:600); the temporal columns aren't being
extracted. `update_world(valid_until=...)` only sets them when
callers pass them explicitly, and `auto_close_on_conflict` only
fires on the `update_world` path (not the consolidation path).

Two complementary fixes ship as part of P42:

1. **Extend the consolidation prompt** to extract optional
   temporal scope:
   ```
   TRIPLES:
   (subject, predicate, object, valid_from?, valid_until?)
   ```
   Update the parser to read the optional 4th/5th fields and
   `INSERT INTO triples (..., valid_from, valid_until, ...)`.
   Captures phrases like "no longer X" / "until 2024" / "after
   switching from Y" that the LLM can identify in the source
   atoms.

2. **Supersede-edge inference.** When consolidation writes a
   `supersedes` edge from observation A to observation B, also
   set `valid_until = A.created_at` on every triple whose
   `atom_id` belongs to B (the superseded observation). Free,
   automatic, tracks fact-replacement timing for the common case
   where the LLM didn't explicitly extract a `valid_until`.

Both should ship together. Option 2 is the safety net for
anything option 1 misses.

**Effort.** 1 day for the response wiring + 1 bench run; +0.5 day
for the supersede-edge `valid_until` inference.

**Risk.** Low. Returning extra info in the response is purely
additive — readers can ignore the new block if they don't want
it. Bench risk: only if the reader prompt template changes to
include the triples block (then it's a real A/B).

**Connection to other levers:**
- Composes with P41 (triple-cosine atom augmentation): P42 returns
  the triples themselves; P41 surfaces the atoms behind them.
  Both leverage `triples.embedding`.
- Orthogonal to graph pathway (which we've established is dead
  for retrieval). P42 is presentation, not ranking.

**File location:** `msam/server.py:api_query` two-tier path.
Tests: cosine ranking, top_k cap, expired-triple filtering, empty
when triples table empty.

---

### P43 — Beam search always-on; subatom retrieval as third beam [re-test queued 2026-05-02]

**Update 2026-05-02.** Last result `msam_p43_canon_v1` was flat at
0.784. Worth re-running now that P30 baseline has shifted (post-
consolidation-similarity 0.75, post-P42 wiring) — the original
neutral-result reading may have been correct only against the old
baseline. Queued behind P40.



**Status.** Bench-tested 2026-04-30. **Overall flat (0.784,
exactly canonical) with a clean shape change: multi-session
+2.2pp ✓, knowledge-update −3.8pp ⚠.** Sentence-level extraction
helps cross-session recall (where many partially-relevant atoms
combine) but hurts fact-replacement (where stale fact sentences
can outrank current ones). Net trade is symmetric.

Implementation deviated slightly from spec — instead of going
through `retrieve_v2.beam_search_retrieve`, the subatom pathway
was wired directly into `hybrid_retrieve` as a new RRF pathway
('subatom') alongside semantic + keyword. Functionally
equivalent; cleaner code path; no need to flip
`retrieval_v2.enabled` in bench config.

Code stays behind `enable_subatom_beam = false` (default) for
production. Not shipped to canonical. Full data:
BENCHMARK-RESULTS.md §msam_p43_canon_v1.

**What.** Three coupled changes:

1. **Drop the 10,000-atom threshold** for beam search. Currently
   `enable_beam_search = "auto"` defers to
   `beam_search_atom_threshold = 10000` — beam doesn't activate
   below that. Untested on bench (which has ~500 atoms/question).
   We don't know if beam would help at small atom counts.

2. **Drop beam 2 (regex rewrite, P11) entirely.** The cleanup batch
   removes `rewrite_query()`, so beam 2's mechanism goes away. We
   considered replacing it with a synonym-expanded query applied
   to both pathways, but: P12's gain came from BM25's term-exact
   limitation (synonyms unlock keyword recall); embeddings already
   handle synonyms reasonably. Adding "job career work occupation"
   to the semantic query risks shifting the embedding centroid
   away from question intent, with no clear recall mechanism. Skip
   the speculative beam.

3. **Replace beam 3 (triple-graph term expansion)** with
   `compressed_retrieve` — sentence-level extracts via
   `enable_subatom` + `enable_fact_dedup`. Beam 3 currently calls
   `expand_query()` which appends triple-graph subject/object
   terms to the query — effectively dead in our bench (triples
   extraction is off in `msam_bench.toml`) and overlaps with
   what P41 will surface via direct cosine match anyway.

**Why test these together.** Beam search's value is in covering
blind spots from any single query formulation. Three beams of
similar coverage (orig, regex-rewritten, synonym-expanded) all
returning whole atoms is a narrower test than three beams that
include a sentence-level pass.

**Mechanism.**

```python
# Beam 1: original query, hybrid_retrieve (whole atoms)
# Beam 2: regex-rewritten query, hybrid_retrieve (whole atoms)
# Beam 3: original query, compressed_retrieve (sentences,
#         dedup'd, then mapped back to parent atom_ids)
# Merge into a single ranked list, RRF-fuse by atom_id keeping
# max score per atom across beams.
```

Subatom returns sentences with atom_id metadata; reaggregate
sentences → parent atom and use the *highest* sentence score as
the parent atom's beam-3 score. So an atom with a tight 3-sentence
hit ranks high; an atom with diffuse weak relevance ranks low.

**Beam composition after cleanup + swap: 2 beams.**
- Beam 1: original query → `hybrid_retrieve` (whole atoms; P12
  synonym expansion fires internally on the keyword pathway)
- Beam 2 [new, was beam 3]: original query → `compressed_retrieve`
  (sentence-level extracts, dedup'd, mapped back to parent
  atom_ids)

Two distinct signals: granularity (whole atoms vs sentences) on
the same query. RRF fuses by atom_id keeping the max score per
atom across beams. Tighter than 3 speculative beams; no dead
paths.

**"Beam search" is then a 2-path RRF.** That's still meaningfully
different from single-path retrieve — sentence-level matches can
surface atoms whose whole-atom RRF ranks them out of top-K.

**Recommended run plan: ship P41 + P43 together, A/B in parallel.**
Implement both behind strict-no-op flags. Bench two configs at the
same time:
- **A (`msam_p43_canon_v1`)**: P43 minimal — 2 beams (original-
  hybrid + subatom). P41-as-beam flag OFF.
- **B (`msam_p43_p41_canon_v1`)**: P43 + P41 — 3 beams (original-
  hybrid + subatom + P41 triple-augmented). P41-as-beam flag ON.

Comparisons:
- A vs current canonical (data on hand) → P43-alone effect
- B vs A → P41-as-3rd-beam delta on top of P43

Discipline required: P41's flag-off path must early-return before
any DB queries or embedding lookups, otherwise we contaminate A.
Same pattern we used for `enable_hyde` (bench was clean).

Trust the per-subtype shape over the overall delta. With ~±1.3pp
overall noise, a single-run overall delta of ±1pp tells us nothing.
A 5pp+ shift on a specific cohort (preference, multi-session,
knowledge-update) is real signal.

**Two flags affected.**
- `[retrieval_v2] enable_beam_search` — change default from
  "auto" to True. Drop or keep the threshold for the optional
  user-config override.
- `[retrieval_v2] beam_search_atom_threshold` — irrelevant if
  beam is always-on; keep config key for back-compat, default
  unchanged.

**Effort.** 0.5 day for the beam restructure + 1 bench run.
Compressed retrieve already exists in `subatom.py` — just need to
glue it into beam 3.

**Risk.** Medium. Three risks:
1. Beam search at small atom counts could just be 3× the latency
   for no recall lift.
2. Subatom sentence scoring is a different similarity scale than
   atom-level RRF — may not RRF-fuse cleanly.
3. The compression LLM call (`enable_synthesis`) is OFF by default
   so subatom shouldn't add LLM cost, but verify no path enables
   it transitively.

**Connection to other levers:**
- The bench currently bypasses `retrieval_v2` entirely
  (`[retrieval_v2] enabled = false`). Testing P43 means flipping
  the bench's two-tier path through `retrieve_v2`, OR plumbing
  beam search down into `hybrid_retrieve`. The latter is the
  cleaner refactor. The former is the smaller change.
- Composes with P39 (median pivot): if beam 3 surfaces sentence-
  level matches that drag in atoms the cheap path missed, those
  could become P39's pulled-in atoms.

**File location:** `msam/retrieval_v2.py:beam_search_retrieve`.
Tests: beam fuse correctness, atom_id-level dedup of sentence
results, subatom path no-op when no sentences match.

---

### P44 — LLM-driven relations maintenance job (elaborates / supports / contextualizes) [partially shipped]

**Status (2026-05-02).** **Three relation types are now written
automatically by consolidation** (`saga/consolidation.py`):
`'consolidated_into'`, `'evidenced_by'`, `'supersedes'`. These cover
the basic structural edges (raw → observation, observation → its
sources, observation → prior observation it replaces).

What's **still open** is the richer semantic-edge set the original
P44 proposed: `elaborates / supports / contextualizes / depends_on /
refines / contradicts`. These need an explicit LLM pass over
candidate atom pairs — different shape from the consolidation-time
writes (which are derivable from cluster membership + the supersedes
detector). No consumer for the semantic edges yet; queue behind a
concrete retrieval-side use case (e.g., a graph-supplement block
that follows `elaborates` edges from a hit atom).



**Status.** Filed 2026-04-30. Not yet implemented.

**What.** The `atom_relations` table allows
`elaborates / supports / contextualizes / depends_on / refines /
contradicts` edges, but **nothing in MSAM auto-writes them**.
They exist only when callers manually call `add_atom_relation()`.
Spreading activation reads from
`elaborates / supports / contextualizes / consolidated_into` —
so spreading is currently working with only `consolidated_into`
(plus co-retrieval pairs). The other three relation types are
dead.

P44 adds a periodic LLM-driven maintenance job that scans atom
pairs and writes relation edges where they apply.

**Pre-filter to keep cost down.** O(N²) atom pairs is infeasible
at 10k+ atoms. Filter to candidate pairs that:
- Share at least 2 topics (`atom_topics` join), OR
- Appear together in `co_retrieval` with `co_count ≥ 3`, OR
- Are in the same atom_relations cluster via existing edges

That winnows from O(N²) to a few thousand pairs even for a
moderate-size deployment.

**Batched LLM call shape.** One call per batch of N pairs (target
~10–20 pairs/batch to keep prompts bounded), N labels back. Single
API roundtrip beats N sequential calls.

```
Score each pair below. For each, output one of:
- elaborates    (B adds detail to A)
- supports      (B provides evidence for A's claim)
- contextualizes (A provides context for interpreting B)
- contradicts   (B disagrees with A)
- supersedes    (B replaces A as more current)
- depends_on    (A's truth depends on B)
- refines       (B is a more precise version of A)
- none          (no significant relationship)

Output exactly one line per pair, in the format:
PAIR_<n>: <relationship>

Pair 1:
A: {content_A1}
B: {content_B1}

Pair 2:
A: {content_A2}
B: {content_B2}
... etc
```

Parser reads `PAIR_<n>: <label>` lines, writes
`add_atom_relation()` for each non-`none` label.

**Incremental: only score what's new since the last run.** Track
`last_relations_run_at` in a `job_runs` table (or a single-row
metadata table). On each run, candidate pairs are restricted to
those where at least one atom has `created_at` OR `last_accessed_at`
after the timestamp. Stable old pairs that have already been
scored don't get re-evaluated. Idempotent on re-run; missed pairs
get caught the next time either atom is touched.

Run as a periodic decay-cycle pass — daily or weekly. Skip pairs
that already have an `atom_relations` row between them (covers any
relation type, since relabelling is rare).

**Effort.** 2 days for the maintenance job + a small bench run
(the lift would show up in spreading activation; bench measures
that path).

**Risk.** Low for correctness (the relations are additive — bad
labels can be detected and removed). Medium for cost: thousands
of LLM calls per run, even with pre-filtering. Run on a cheap
model (gpt-5.4-nano).

**Connection to other levers:**
- Spreading activation gets meaningfully more material to
  traverse. Today it's mostly `consolidated_into` + co-retrieval;
  with elaborates/supports/contextualizes populated, it covers
  cross-cluster associations.
- Composes with predictive retrieval (P-prediction): the relation
  edges become inputs to topic-momentum prediction.
- Orthogonal to triples / graph pathway (those track facts, not
  inter-atom semantic relations).

**File location:** new file `msam/relations_maintenance.py`. Hook
into the decay cycle as an optional periodic pass.

---

### P45 — Caller-driven recall endpoints (boundaries + most-retrieved atoms)

**Status.** Filed 2026-04-30, expanded 2026-05-01. Not yet
implemented.

Two related REST endpoints that surface MSAM's chronological /
frequency-based state for prompt assembly. Both are paired
because they share the same auth, response shape, and latency
profile (single SELECT, no LLM, ~5-10ms each), and callers tend
to want them together when assembling pre-message context.

#### Endpoint 1 — `GET /v1/sessions/recent`

Expose `get_last_sessions(count, channel, session_id)` from
`core.py` over REST. The function exists with the right shape but
is only callable from the CLI today. For prompt-assembly use
cases like "the last 3 boundaries on this channel" (a separate
person's conversation in a different channel isn't useful),
callers need REST access.

**Why a separate endpoint, not a flag on `/v1/query`.** Two
different intents:
- `/v1/query` with `include_session_boundaries = true` ranks
  boundaries by *semantic similarity* to the query — useful for
  "what did we discuss about X?".
- "Last 3 boundaries on this channel" is *chronological recall* —
  no query, just `ORDER BY created_at DESC LIMIT 3`.

**Shape:**
```
GET /v1/sessions/recent?channel=X&count=3&session_id=...
```
Response: list of boundary atoms with `id`, `content`, `timestamp`,
`topics`, `session_id`, parsed `metadata` (channel, decisions,
unfinished, emotional_state, etc.).

**Implementation:**
```python
@app.get("/v1/sessions/recent", dependencies=[Depends(verify_api_key)])
async def api_recent_sessions(
    count: int = 3,
    channel: Optional[str] = None,
    session_id: Optional[str] = None,
):
    def _list():
        from .core import get_last_sessions
        return get_last_sessions(count=count, channel=channel,
                                 session_id=session_id)
    return await asyncio.to_thread(_list)
```

~10 lines in `server.py`. The function in core.py already does
all the work.

#### Endpoint 2 — `GET /v1/atoms/most_retrieved`

Top-N atoms by retrieval count over a recent time window. Useful
for "what has the agent been thinking about lately?" pre-message
context, or for selecting candidate atoms to surface in heartbeat
turns when no specific query is being asked.

**Shape:**
```
GET /v1/atoms/most_retrieved?days=7&count=10&channel=X&contributed_only=false
```
- `days` (default 7) — only count retrievals from the last N days
- `count` (default 10) — top-K atoms by retrieval count
- `channel` (optional) — filter to atoms tagged with this channel
- `contributed_only` (default false) — when true, count only
  retrievals where `access_log.contributed = 1` (the agent's
  feedback indicated the atom was actually used). Surfaces "atoms
  that earned their keep" rather than just "atoms that got pulled
  in often."

Response: list of atoms with `id`, `content`, `created_at`,
`topics`, `session_id`, plus computed fields `retrieval_count`,
`contributed_count`, `last_retrieved_at`.

**SQL** (against `access_log` joined with `atoms`):
```sql
SELECT a.id, a.content, a.created_at, a.topics, a.session_id,
       COUNT(*) AS retrieval_count,
       SUM(CASE WHEN al.contributed = 1 THEN 1 ELSE 0 END)
           AS contributed_count,
       MAX(al.accessed_at) AS last_retrieved_at
FROM access_log al
JOIN atoms a ON a.id = al.atom_id
WHERE al.accessed_at >= datetime('now', ?-' || ? || ' days')
  AND a.state IN ('active', 'fading')
  -- optional: AND al.contributed = 1
  -- optional: AND json_extract(a.metadata, '$.channel') = ?
GROUP BY a.id
ORDER BY retrieval_count DESC, last_retrieved_at DESC
LIMIT ?
```

Uses the `idx_access_log_atom` and `idx_access_log_session` (now
indexed) to keep the time-window scan cheap. At typical access_log
sizes (thousands to tens of thousands of rows) the query should
return in <10ms; at very large logs we'd want a partial index on
`(accessed_at, atom_id)` but defer until needed.

**Implementation:** new helper in `core.py` named
`get_most_retrieved(days, count, channel, contributed_only)` that
runs the SQL above and returns the atom list. New endpoint
`api_most_retrieved` in `server.py` wraps it. ~30 lines including
the helper.

#### Shared notes

**Performance caveat (channel filter).** Both endpoints use
`json_extract(metadata, '$.channel') = ?` which is a row scan
(no index on JSON columns). For DBs with thousands of relevant
atoms this could get slow. Mitigation: denormalize `channel` to
an `atoms.channel` column when volume warrants — same pattern
used for `session_id` in migration 3. Skip until needed.

**Effort.** 1 hour total (both endpoints + helper + tests).

**Risk.** Endpoint 1: none — exposes existing well-tested
function. Endpoint 2: medium — new SQL helper that hasn't been
exercised; tests should cover the `days` window edge case
(retrievals exactly at the boundary), the `contributed_only`
filter, the channel filter, and the empty-result case (no
retrievals in the window).

---

### P46 — Smarter sentence splitting for subatom retrieval

**Status.** Bench-tested 2026-05-01 (paired with P43+P41 re-run).
List-aware splitter shipped. Significant per-subtype shape
changes:
- **single-session-preference**: 0.267 → 0.433 in P43_v2 (+16.7pp,
  largest preference lift we've ever measured on the bench).
- **single-session-assistant** in P43+P41_v2 hit **1.000** (perfect
  score, +5.4pp over P43+P41_v1).
- P41 went from −1.4pp regression to +0.4pp marginal win when
  fed the cleaner splitter.

Net overall is essentially flat vs canonical (P43_v2 = 0.778,
P43+P41_v2 = 0.782 vs P30v3 = 0.784) — within the ±1.3pp noise
floor — but the preference and assistant subtype lifts are well
above their respective noise floors and durable. Multi-session
−3.7pp vs P43_v1 because the fragmented per-bullet matches that
helped it before are gone.

**Splitter fix shipped to canonical** since it costs nothing
(faster retrieve too, 3-5s vs 5-7s per query).

Subatom + triple_augment_v2 NOT shipped to canonical — net flat
at the overall level despite favorable per-subtype shape. Stays
behind `enable_subatom_beam = false` and `enable_triple_augment_v2
= false` defaults.

Full data: BENCHMARK-RESULTS.md §msam_p43_canon_v2 and
§msam_p43_p41_canon_v2.

**Why.** P43 bench (msam_p43_canon_v1) showed sentence-level
retrieval helps multi-session +2.2pp but hurts knowledge-update
−3.8pp. Inspecting the per-question DBs revealed the splitter
breaks long assistant responses into way too many fragments,
including pure markdown formatting:

| Bucket | Count | Example |
|---|---|---|
| ≤20 chars | 46 | `1. **Lighting**:` |
| 21–40 chars | 46 | `* Avoid harsh overhead lighting.` |
| 41–80 chars | 151 | `* Position your chair at a comfortable height.` |
| 81–200 chars | 343 | (well-formed sentences) |
| 200+ chars | 25 | (long sentences, fine) |

~15% of cached "sentences" are markdown fragments. Each gets a
real embedding and competes for top-K. A concrete failure: query
"What degree did I graduate with?" pulled in
`* Position your chair...90-degree angle to the keyboard` from a
home-office advice atom — a sentence-level keyword match the
whole-atom embedding would have correctly ranked out.

**Three improvements, ordered by effort:**

1. **Hard min-length filter** (cheapest, high signal). Raise the
   8-char floor to 50 chars in `split_sentences`. Drops the worst
   markdown-formatting fragments. Risk: loses real short sentences
   like "Yes." or "I do." but those are rare in real content and
   low-signal anyway.

2. **List-aware grouping** (the actual fix). When the splitter
   encounters a header followed by ≥2 bullets/numbered items,
   group them under one chunk:
   ```
   "1. **Lighting**:
   * Natural light is ideal...
   * Avoid harsh overhead lighting..."
   → ONE chunk (300-400 chars), not 4 separate ones
   ```
   Detection heuristic: lookahead for `^\s*[*\-•]\s` or `^\s*\d+\.\s`
   on subsequent lines after a header-shaped line.

3. **Char-budget chunking** (industry-standard RAG approach).
   Sliding window with target chunk size ~200-400 chars, breaking
   on natural boundaries (sentence end > paragraph end > line
   end > whitespace). LlamaIndex / LangChain pattern. Replaces
   regex splitting entirely.

**Recommendation: ship #1 + #2 together as one change.** Both are
regex-level fixes inside `split_sentences`; together they
eliminate the worst fragments without needing a new chunking
framework. If P43's bench result improves with the cleaner
splitter, then revisit #3 (heavier rewrite, library option).

**Effort.** Half a day for #1 + #2. Tests with sample assistant-
response atoms confirming the merge behavior. Re-bench P43 with
the new splitter — should lift knowledge-update closer to
canonical while keeping multi-session gain.

**Risk.** Low. The current splitter has no calibrated tests, so
any change has to be evaluated by re-benching, but the fragment
problem is concrete and the fix is targeted.

**File location:** `msam/subatom.py:split_sentences` (line 39)
+ `_SENT_SPLIT` regex (line 30). Tests in `test_subatom.py`.

---

### P47 — Trend-driven promotion / demotion of saga atoms [filed 2026-05-03]

**What.** Tie three previously-independent threads into one signal:
- **P17** activates `atoms.trend` (improving / stable / weakening / stale)
- **P45** surfaces `most-retrieved` atoms as promotion candidates
- The retrieval-side trend multipliers (already in `core.py:2510-2514`)

…so that promotion candidates AREN'T just "high cumulative retrieval"
but "high recent retrieval AND currently growing." This distinguishes
"atom that was hot 6 weeks ago and is now stale" from "atom that's
currently growing in usage" — only the latter belongs in mimir's
in-context core memory.

**Bundle: ship as one consolidation-pass-v2 PR + one bench:**

1. **P17** — write `trend` from access-log decay during consolidation.
   ratio = retrievals_30d / max(retrievals_30-90d, 1):
   - > 1.2 → `improving`  (no penalty)
   - 0.7-1.2 → `stable`   (no penalty)
   - 0.3-0.7 → `weakening` (×0.7 penalty kicks in)
   - < 0.3 → `stale`      (×0.4 penalty)

2. **P35-contradictions** — add `CONTRADICTIONS:` output section to
   the consolidation prompt. Cheap (LLM already has all source atoms
   in context); pure additive (no retrieval ranking impact); feeds
   P25's reconciliation work with structured cluster-level signal.

3. **P45 extension** — add `trend` to the `get_most_retrieved`
   response and a `--trend improving|stable|weakening|stale` filter
   on the `mimir reflection most-retrieved` CLI.

4. **reflection/SKILL.md track B.3 update** — promote candidates =
   `--trend improving --contributed-only`; cleanup candidates =
   `--trend stale`; demote-from-core candidates = core blocks whose
   content correlates with stale atoms.

**Why bundle.** All three changes touch consolidation (P17 + P35-c
both modify `_persist_consolidation` / the prompt) plus the
downstream consumer (P45 + reflection skill) that uses the new
column. Doing them as one PR + one bench run isolates the delta
cleanly from other work; doing them separately means three bench
cycles and harder attribution.

**Effort.** ~50 LOC across saga + mimir + skill + tests. ~1 day code
+ 1 bench cycle.

**Risk.** Low. P17 only writes a previously-unwritten column —
existing retrieval multipliers were already coded but no-op.
P35-contradictions adds an output section the post-processor doesn't
yet read (forward-compat). P45 extension is additive to the response
shape.

---

### P48 — Canonical predicate vocabulary for entity-relation triples [filed 2026-05-03]

**What.** Reduce predicate fragmentation in consolidation-extracted
triples. Today the LLM invents ad-hoc predicates that fuse intent
with qualifier:

```
User|prefers_podcast_length|20-30_minutes
User|prefers_podcast_format|solo_episodes
User|likes_podcast|<show>
User|prefers_themes|philosophical
User|prefers_soft_cozy_material|...
```

These should all reduce to a small set of canonical intents
(``prefers``, ``likes``) with the qualifier moved into the object:

```
User|prefers|podcast_length=20-30_minutes
User|prefers|podcast_format=solo_episodes
User|likes|podcast:<show>
User|prefers|theme=philosophical
```

**The data point.** Diagnostic on the 500-question P42 Sonnet
bench corpus (commit 435699f, 2026-05-03):
- **9,997 distinct predicates** across all per-question DBs
- Bare ``prefers``: 7 occurrences
- Bare ``likes``: 12 occurrences
- 30+ ``prefers_*``/``likes_*`` variants in the long tail (most
  N-of-1 or N-of-few)

This kills cosine match for preference queries: "What kind of
podcasts do I like?" should hit the cluster of preference triples
about podcasts, but each variant is in a different embedding
neighborhood. Single-session-preference category was the only one
where P42 (triples-direct-to-agent) lost on Sonnet — and this is
why.

**Mechanism.** Two-part change to the consolidation prompt:

1. **Soft canonical seed.** Instead of "must use one of," prefer
   reuse and provide a list:

   ```
   Rules for TRIPLES:
   - Predicate must be lowercase_snake_case.
   - PREFER reusing canonical intent predicates over inventing
     domain-specific compounds. Common canonical predicates:
       prefers / likes / dislikes / has / owns / works_at /
       lives_in / discussed / recommends / asked_about /
       offers / includes / provides / supports / uses
   - Detail goes in the OBJECT, not the predicate. Instead of
     (User, prefers_podcast_length, 20-30_minutes), emit
     (User, prefers, podcast_length=20-30_minutes).
   - You MAY introduce a new predicate when the canonical set
     genuinely doesn't fit (a domain-specific relation between
     two non-User entities, e.g. (CompanyX, manufactures, ProductY)).
   ```

2. **Surface existing vocabulary.** At the start of each
   consolidation pass (or per-cluster), query the DB for the
   top-N existing predicates and top-N existing subjects. Inject
   into the prompt as context the LLM can canonicalize against:

   ```
   Existing predicates in this database (prefer reusing when
   they fit; counts in parens):
     offers (408), includes (388), provides (310), supports (211),
     uses (172), located_in (164), has (157), suggests (156),
     prefers (7), likes (12), ...

   Existing subjects (prefer reusing the canonical capitalization):
     User (1247), Sony (12), Anthropic (8), ...
   ```

The DB query is cheap (one COUNT-grouped scan over a small table);
runs once per consolidation pass, not per cluster. Cold-start
behavior: empty DB → fall back to the static seed list. After the
first few clusters in a fresh DB, the surfaced list grows and
becomes the dominant canonicalization signal.

**Why this matters beyond preferences.**
- 9,997 unique predicates serving as 9,997 unique buckets is
  pathological for any cosine-on-triple-embedding mechanism (P41,
  P42, P47).
- Subject canonicalization (``User`` vs ``user`` vs ``the_user``)
  has the same problem at smaller scale.
- This is a quality fix for the whole structured-side of saga,
  not just one category. P42, P41, P47 promotion / demotion all
  benefit.

**Effort.** ~30 LOC: the prompt change + a `_canonical_vocab_block`
helper that queries existing predicates + subjects from the DB.
Tests for the prompt rendering + the DB-vocab-injection contract.
~1 day code + 1 bench cycle.

**Risk.** Low–medium. The "PREFER" softening avoids over-
constraining the LLM. The seed list is conservative (only intent
verbs canonicalized; domain action predicates like "manufactures"
stay free). Worst case: predicate diversity stays high but cosine
recall on preferences improves anyway because the canonical
predicates cluster better.

**Pre-bench validation.** Before running a full 500q bench, run a
50-question slice scoped to single-session-preference questions
only and compare predicate distributions before/after. If the
fragmentation drops meaningfully (say >50% reduction in distinct
predicates among preference triples), the full bench is justified.

**Connection to P25.** Predicate canonicalization makes
contradiction detection (P25) much sharper — currently
"(User, prefers_genre, jazz)" and "(User, likes_music_genre, jazz)"
look like two unrelated facts; canonicalized to
"(User, prefers, music_genre=jazz)" and
"(User, likes, music_genre=jazz)" they're recognizable as the
same claim. Same for temporal closure (P37(a)).

---

### P15 — Evaluate cross-encoder rerank [decided not to do 2026-05-02]

**Status.** The mimir agent already does its own ranking pass over
returned atoms inside its response prompt (it's an LLM with full
context). A saga-side cross-encoder would just duplicate work
already happening downstream — at the cost of adding a second model
dependency to saga. `retrieval_v2.rerank_with_llm` stays callable
for non-agent harness users; not a planned bench experiment.



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

### P16 — Internal scheduler for decay + consolidation [decided: skip 2026-05-02]

**Status.** Mimir's `add_saga_consolidate_job` (in `mimir/scheduler.py`)
already handles cadence saga-side via APScheduler — fires
`consolidate()` on a configurable cron (default Sun 04:00 UTC).
Decay would slot in the same way if needed. The only remaining
argument for moving the scheduler *into* saga is "saga without mimir";
defer until that's a real use case. No code change planned.



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

### P17 — Trend column writer (or delete the dead reads) [design 2026-05-02]

**Update 2026-05-02.** Decision: **fold into consolidation** rather
than build a separate cron job. Consolidation already iterates over
atom clusters and queries `access_log` for stability calculations —
adding one more field write costs ~0 and reuses the same data path.

**Mechanism.** In `_persist_consolidation`, after computing
`source_ids` for the cluster, compute trend from access decay:

```python
# Ratio of retrievals in last 30d vs the 30-90d window before.
ratio = recent_retrievals / max(prior_retrievals, 1)
if   ratio > 1.2: trend = None         # improving / stable — no penalty
elif ratio > 0.7: trend = None         # stable
elif ratio > 0.3: trend = "weakening"  # 0.7× multiplier (existing)
else:             trend = "stale"      # 0.4× multiplier (existing)
```

Pure data-driven — no extra LLM call. Writes to `atoms.trend` for
the consolidated observation; source raws stay at NULL (default no
penalty). When the cluster is small / has no access history, leaves
trend NULL.

**Effort.** ~1 day code + tests. Ship alongside P35's contradiction
surfacing as one consolidation-pass enhancement.



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

### P18 — Re-evaluate P10 with the new boundary-atom filter [decided differently 2026-05-02]

**Status.** **Not pursuing as a saga-side bench experiment.** The
benchmark intentionally runs with `enable_session_boundaries = false`
to keep ingest fast (each haystack session would write a boundary
atom otherwise — ~50 extra embed+store calls per question). Mimir
surfaces the prior N session boundary atoms in its turn prompt as
the `## Recent session summaries` block (loop §2.2 in
FEEDBACK-LOOPS.md), which provides the cross-session continuity P10
was reaching for — without crowding retrieval. The boundary-atom
filter (`source_type != 'session_boundary'`) at `core.py:698` stays
in place as a defensive guard.



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

### P19 — Delete `retrieve_with_relations` [done 2026-05-03]

**Status.** Deleted 2026-05-03 along with its CLI debug subcommand
(``cmd_relations`` retrieve branch in ``remember.py``) and its unit
test. Pure dead-code removal — the supersedes branch was duplicated
by the multiplicative path in main retrieval, and the elaborates
branch could never fire because no code writes
``relation_type='elaborates'`` edges (P44 territory).



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

### Cleanup batch — features confirmed dead or harmful

Filed 2026-04-30. Confirmed via discussion + bench data.

1. **Default `[retrieval] two_tier_enabled = True`.** Canonical-best
   retrieval is two-tier; the False default forces every caller to
   know to flip it. Bench config already runs with this on.

2. **Remove `[retrieval_v2] enable_quality_filter` (P13).** Bench-
   regressed alone (P13_canon_v1 = 0.758, -2.6pp). Delete the flag,
   delete the helper, drop it from cherry-pick wiring.

3. **Remove `[retrieval] enable_temporal_pathway` and
   `[retrieval_v2] enable_temporal`.** Pathway regressed in P3
   bench. Detector untested but its design (filter atoms older
   than max_age_days) is exactly what hurts temporal-reasoning
   queries asking *about* old facts. Replace temporal handling
   with `query_world(at_time=...)` against the world model
   (covered by P42).

4. **Remove `[retrieval_v2] enable_entity_roles` + `entity_roles.py`.**
   Pattern-based, deployment-specific, untested on bench, dormant
   in canonical path. Hardcoded patterns for `Things Agent Knows`,
   `Core Traits`, etc. don't generalize.

5. **Remove `_QUERY_REWRITES` + `entity_mappings` config + the
   `enable_rewrite` and `enable_query_rewriting` flags** that gate
   them. The hardcoded `("user" → "User", "agent" → "Agent")`
   defaults assume one specific deployment, and the configurable
   `entity_mappings` interface has no callers using it. Contextual
   rewrite (LLM, context-aware) is the better path for any
   downstream caller that needs entity disambiguation. Removes
   `rewrite_query()`, `_apply_query_rewriting()`, and the cherry-
   pick wiring entirely.

8. **Remove `expand_query` (triple-graph term expansion in
   `retrieval_v2.py:151`).** Two call sites today:
   - `beam_search_retrieve` beam 3 — P43 replaces with subatom.
   - `compressed_retrieve` internal "extraction_query" alignment
     — a vestigial step that can be rewritten to use the original
     query directly.

   Subtle catch: the SAME flag `[retrieval_v2] enable_query_expansion`
   gates both `expand_query` (triple-graph, in retrieval_v2.py)
   AND `_expand_query_for_keyword` (P12 synonym dict, in core.py).
   Same name, two completely different mechanisms. **Keep the flag
   and the P12 keyword-pathway version** (it's the +0.8pp lever
   shipped to canonical); only remove the triple-graph
   `expand_query` function and its call sites.

6. **Remove store-time triples extraction.** P35 added the
   consolidation-time path; the store-time path
   (`server.py:314 if stream == "semantic" and ... extract_and_store(...)`)
   should be deleted. Triples should only be extracted during
   consolidation, not on every store.

7. **Remove `sycophancy` infrastructure.** Caller-driven, no
   auto-detection, dead in absence of explicit
   `record_agreement()` calls. Drop the table, the metric helper,
   the config block.

All 7 are hygienic, none should affect bench scores meaningfully.
Ship together as one cleanup commit; expected bench impact ≤ noise
floor on the canonical configuration.

---

### Queued: predictive_retrieval bench after Bluesky data warmup

Not a new experiment per se — `predict_needed_atoms` /
`PredictiveEngine` exists (no LLM, pure SQL on access_log +
co_retrieval + topic_momentum) but needs a populated DB to be
meaningful. After a Bluesky-driven warmup run produces real
access_log + atom history, queue a bench / smoke that exercises
the heartbeat-turn seeding path: `predict_needed_atoms({...})` →
extract topics from returned atoms → seed conversational ideas.

---

## D. Architecture / design questions

### P24 — Channels as first-class scoping (option B from prior memo) [queued 2026-05-02]

**Status.** Approved 2026-05-02. Promote `channel` from
`metadata.channel` (JSON-extracted at query time) to a denormalized
`atoms.channel TEXT` column. Plan:

1. Add column + index, migrate existing rows from
   `json_extract(metadata, '$.channel')`.
2. Update `store_atom` to write the column directly.
3. Replace `json_extract` filters in `core.py` (lines 5071, 5138)
   with native column comparisons.
4. Add `channel` as a peer of `agent_id` in `RetrieveRequest` /
   `QueryRequest`.

Net: ~150 LOC + a one-shot migration. Buys real query-time perf
(no per-row JSON parse) plus cleaner filter semantics for mimir's
bench bridge and per-channel scoping in production.



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

### P25 — Reconcile three contradiction detectors [design 2026-05-02]

**Update 2026-05-02.** Reframe: most "contradictions" the system
sees are actually **temporal closures**, not contradictions.
P37(a)'s `valid_from`/`valid_until` machinery handles same-(S, P)
updates by setting `valid_until = now` on the prior row — that's a
new fact superseding an old one in time, not a conflict. Real
contradictions (overlapping valid windows + conflicting objects on
same (S, P, *)) are rare with auto-close working.

**The reconciled shape:**

- **`update_world(auto_close=True)` is the temporal updater.**
  Renamed-in-spirit: not a contradiction handler but a write-side
  enforcer of temporal closure. New (S, P, new_obj) → prior
  (S, P, old_obj) gets `valid_until = now`. This is the common path
  and it's correct.

- **`detect_contradictions` (triple-based)** runs as a periodic
  audit (cron / reflection), surfaces overlap-conflicts as
  algedonic `triple_contradiction_detected` events. **Don't
  auto-resolve**; let operator/agent decide. Removed from the
  query hot path.

- **`find_semantic_contradictions` (embedding-based)** runs on
  the same audit cron, surfaces observation-level dissonance
  (paraphrase-similar atoms with opposing valence) as
  `semantic_contradiction_detected` events. Different surface
  from triple contradictions — observations can dissent without
  contradicting any specific (S, P, O) tuple.

- **No contradiction detection in the query hot path.** Both
  detectors become diagnostic / surfacing tools, not retrieval
  filters.

**Effort.** ~1 day code + tests. Mostly: delete the contradiction
hooks from query paths, add a `mimir saga audit-contradictions`
CLI subcommand that runs both detectors and emits algedonic
events when matches land.



**What.** `find_semantic_contradictions` (embedding-distance, used by
P4-bench supersedes), `detect_contradictions` (triple-based, in
`triples.py`), `world_model.update_world` auto-close. Each was added
at a different time.

**Effort.** 0.5 day to document overlap and pick canonical, 1 day to
migrate callers.

**Risk.** Low. They serve different shapes (atoms vs triples), so
probably keep two but explicitly document when each fires.

---

### P26 — `mental_model` slot — commit or remove [removed 2026-05-02]

**Status.** **Removed 2026-05-02.** Decision: drop `mental_model`
from the documented `memory_type` vocabulary. No clear job that the
existing `observation` (multi-source synthesis) and world-model
triples (per-subject stateful beliefs with temporal scope) don't
already cover. The committing version would have introduced a third
category with a fuzzy boundary against `observation` — net
complexity without net value.

Kept the `memory_type` column as `TEXT` (no CHECK constraint added)
so any prior data with `mental_model` rows would still load. Updated
the documentation comment in `core.py:3550` and `HINDSIGHT-IDEAS.md`.



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
