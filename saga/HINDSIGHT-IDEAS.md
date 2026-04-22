# Hindsight Ideas for MSAM

Design notes for selectively borrowing architectural ideas from [Vectorize's Hindsight](https://github.com/vectorize-io/hindsight) into MSAM, plus what to skip and why.

Context: Hindsight is a recent open-source agent-memory system built around retain / recall / reflect, with a memory hierarchy (Mental Models → Observations → World Facts / Experience Facts), TEMPR parallel retrieval (Semantic + Keyword + Graph + Temporal), and evidence/confidence tracking on consolidated beliefs. It reports 91.4% on LongMemEval (Dec 2025). Worth studying; not worth copying wholesale.

This document is the output of a session on 2026-04-19. MSAM head is `d4bfcad`.

---

## 1. What MSAM already has that overlaps

Before planning additions, a scan of what's already implemented so we don't re-invent:

| Hindsight concept | MSAM equivalent | Notes |
|---|---|---|
| World facts / Experience facts | `stream = "semantic" / "episodic"` | Already multi-stream with different retrieval behavior |
| Temporal graph with time ranges | `world_model` (`valid_from`, `valid_until`) | Auto-closes prior facts when a new one supersedes |
| Graph retrieval | `triples.py` (S‑P‑O) + `graph_traverse`, `graph_path` | Functionally present |
| Contradiction handling | `contradictions.py` | Negation, temporal supersession, value conflict, antonyms |
| Semantic + keyword retrieval | `hybrid_retrieve` (semantic + FTS5) | Blended multiplicatively inside one scoring function |
| Consolidation | `consolidation.py` | Sleep-inspired; emits synthesis atoms with `consolidated_from` metadata |
| Temporal-aware retrieval | `temporal_recency_hours`, query rewriting | Present but modest |
| Outcome feedback | `felt_consequence` (`record_outcome`, `get_outcome_history`) | Already carries positive/negative signal per atom |
| Forgetting | `forgetting.py`, `decay.py` | Four-signal forgetting, ACT-R decay |

**Takeaway:** MSAM has the raw materials for most of what Hindsight does — the deltas are organizational, not foundational.

---

## 2. What Hindsight does meaningfully differently

Three pieces stand out:

1. **Explicit memory hierarchy during recall.** Mental Models → Observations → Raw Facts are not just separate rows; they're a *retrieval priority order*. The agent sees the distilled versions first; raw facts are evidence, not primary surface.
2. **Observations carry evidence metadata** — a proof count (how many raw facts support this) and a **trend label** (`stable / strengthening / weakening / stale`). The model reasoning over memory can weigh beliefs by their evidence strength.
3. **TEMPR with rank fusion.** Four retrieval strategies run in parallel, ranks are fused with RRF, then cross-encoder reranking. MSAM runs hybrid retrieval in one combined function; weak semantic signal can drag down strong keyword signal (or vice versa) because everything is multiplied in one activation score.

Each of those maps to a concrete proposal below.

---

## 3. Proposals

Ranked by value-per-effort, highest first.

### P1 — Observations tier with evidence counts and trend labels

**Value: high. Effort: ~2 days. Reversible: yes (additive).**

#### What

Promote consolidation output (already being produced by `consolidation.py`) to a first-class memory type with two pieces of metadata the agent can actually read:

- `evidence_count`: how many raw atoms support this observation (already almost implied by `consolidated_from` list length)
- `trend`: `stable | strengthening | weakening | stale`, computed from how evidence has been reinforced over a rolling window

Retrieval prioritizes observations over raw atoms when both are candidates for the same query. The agent sees "User prefers concise responses (evidence: 7, trend: strengthening)" before it sees the seven individual atoms.

#### Why

Current consolidation atoms look like any other atom to the retriever — same stream, same scoring. That wastes the work consolidation just did. Tagging them as observations and letting the retriever prefer them means:

- Fewer tokens per answer (one observation vs. seven raw atoms)
- The agent can reason about belief confidence directly ("I'm confident because evidence=12, trend=stable")
- Stale or weakening beliefs surface so the agent can flag them instead of parroting old facts

#### Schema sketch

Add three columns to `atoms`:

```sql
ALTER TABLE atoms ADD COLUMN memory_type TEXT DEFAULT 'raw';
     -- 'raw' | 'observation' | 'mental_model'
ALTER TABLE atoms ADD COLUMN evidence_count INTEGER DEFAULT 0;
ALTER TABLE atoms ADD COLUMN trend TEXT DEFAULT NULL;
     -- NULL | 'stable' | 'strengthening' | 'weakening' | 'stale'
CREATE INDEX idx_atoms_memory_type ON atoms(memory_type);
```

Migration sets `memory_type = 'observation'` on existing atoms whose `metadata.consolidated_from` exists and is non-empty, and `evidence_count` to `len(consolidated_from)`. Everything else defaults to `'raw'`.

#### Retrieval change

In `hybrid_retrieve` (or `retrieval_v2.retrieve_v2`), add a final rerank step:

1. Run existing retrieval to get candidate atoms.
2. Partition by `memory_type`.
3. Apply a **score bonus** to observations: `score *= 1 + 0.3 * log(evidence_count + 1)`, so well-supported observations win over raw atoms with similar cosine similarity.
4. Apply a **trend penalty** to weakening/stale observations: `score *= 0.7` for weakening, `0.4` for stale.

This is softer than Hindsight's strict priority ordering but preserves the ability for a very-relevant raw atom to beat a vaguely-relevant observation.

#### Trend computation

Runs inside the existing decay cycle. For each observation, look at `felt_consequence.get_outcome_history(atom_id)` over the last N days (configurable, default 14):

- positive outcomes / total > 0.6 AND count increased vs. prior window → `strengthening`
- positive/total stable ± 0.1 AND count ± 20% → `stable`
- positive/total dropped AND negative outcomes appearing → `weakening`
- no accesses in window AND age > stale_threshold → `stale`

Reuses signal MSAM already collects (`record_outcome`, access log). Zero new data pipeline.

#### Acceptance criteria

- LongMemEval score does not *regress* when P1 is enabled with trend bonuses set to zero (baseline preservation)
- LongMemEval `multi-session` subcategory improves ≥ 3 points when bonuses are turned on
- Observations surface ahead of raw atoms in ≥ 70% of queries where both are present (sanity check)
- Stale observations are demoted or excluded in ≥ 90% of cases where they'd otherwise be top-3

#### Risks

- Bad trend labels poison retrieval. Mitigation: ship with small weights, feature-flag, log both pre- and post-bonus rankings for the first week.
- Schema migration on a populated DB. Mitigation: the ALTERs above are additive; no data moves.

---

### P2 — Reciprocal Rank Fusion as an alternative to multiplicative score blending

**Value: medium-high. Effort: ~0.5 day. Reversible: fully (config flag).**

#### What

Right now `hybrid_retrieve` blends semantic similarity, keyword match, and spreading activation into a single score via multiplication / weighted sum. That's brittle across heterogeneous score scales: a weak semantic signal (0.2 cosine) multiplied by a strong keyword match (0.9 BM25) gives a lower final score than a middling signal from both.

RRF ranks each retrieval pathway independently, then fuses: `rrf_score(atom) = Σ_pathways 1 / (k + rank_in_pathway)`. Typical `k = 60`. No score normalization needed; robust to score-scale differences; reorders well when one pathway is confident and another is silent.

#### Why

It's the fusion method Hindsight uses, the one most modern hybrid retrievers (Weaviate, Elastic, Qdrant) default to, and it's shown to outperform weighted-sum fusion on noisy heterogeneous signals. LongMemEval's hard categories (multi-session, knowledge-update) are where signal heterogeneity bites hardest.

#### Implementation sketch

New module `msam/retrieval_fusion.py`:

```python
def reciprocal_rank_fusion(
    ranked_lists: dict[str, list[str]],  # pathway_name -> [atom_id, ...]
    k: int = 60,
    weights: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    scores = defaultdict(float)
    weights = weights or {}
    for pathway, ids in ranked_lists.items():
        w = weights.get(pathway, 1.0)
        for rank, atom_id in enumerate(ids):
            scores[atom_id] += w / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])
```

Wire it into `hybrid_retrieve` behind a config flag:

```toml
[retrieval]
fusion = "weighted_sum"  # or "rrf"
rrf_k = 60
rrf_semantic_weight = 1.0
rrf_keyword_weight = 1.0
rrf_graph_weight = 0.7
```

Default stays `weighted_sum` until we benchmark RRF on LongMemEval and see it win.

#### Acceptance criteria

- A/B on LongMemEval between `weighted_sum` and `rrf`, same reader, same atoms
- RRF wins by ≥ 1 point overall *or* meaningfully wins on at least one hard subtype
- Retrieval latency does not increase measurably (RRF is just another O(n) pass)

#### Risks

- Very low. The flag makes it trivially reversible, and both implementations can ship side-by-side.

---

### P3 — Retrieval as four independent pathways

**Value: medium. Effort: ~1.5 days. Reversible: partial.**

#### What

To make P2 meaningful, the four pathways should actually be independent rankings — not the current entangled semantic + keyword blend. Split into:

- **Semantic**: cosine over atom embeddings (already exists, easy to isolate)
- **Keyword**: FTS5 / BM25 over content (already exists, already isolated)
- **Graph**: traverse triples reachable from entities in the query, return their atoms (exists via `retrieve_triples`, needs wrapping)
- **Temporal**: atoms whose `created_at` intersects the temporal scope inferred from the query ("last week", "yesterday", "when I …")

Each pathway returns its own ranked list. Fuse via RRF (P2). Apply P1 observation bonuses after fusion.

#### Why

With four independent rankings, pathway-level weights (in `msam.toml`) become a meaningful tuning knob. Temporal queries can dial up temporal weight; factual queries can dial up graph weight. Right now the fact that the pipeline has all four signals is hidden behind one blended activation score, so operators can't steer it.

Also unlocks measurement: per-pathway recall@K on LongMemEval tells us which signal is actually pulling weight on which question type.

#### Implementation sketch

- Refactor `retrieval_v2.retrieve_v2` so each pathway is a pure function `(query_context) -> list[(atom_id, rank)]`
- Each pathway has its own `top_k` (typically 20–50 each — wider pre-fusion)
- `retrieve_v2` becomes: run pathways in parallel (threadpool), fuse, rerank, truncate

Temporal pathway is the most new work: needs a lightweight "date scope extractor" over the query. A small LLM call for date parsing is fine (we already have `annotate.py` infrastructure), or regex + chrono-style heuristics for common phrases ("yesterday", "last month", "in June").

#### Acceptance criteria

- On the `temporal-reasoning` subcategory of LongMemEval: score improves by ≥ 5 points
- No regression on `single-session-user` subcategory
- Per-pathway recall@20 logs are emitted and reviewable (instrumentation, not just a score)

#### Risks

- The temporal extractor is the unknown. Rule-based first; promote to LLM only if coverage is < 60% on temporal-reasoning questions.

---

### P4 — Observation trend as a first-class retrieval signal beyond the bonus

**Value: medium. Effort: ~1 day. Reversible: yes.**

#### What

Once P1 lands, trend state becomes a standalone retrieval *filter* the agent can invoke:

- `retrieve(query, filter=stable_only)` — when the agent wants to state a belief
- `retrieve(query, filter=include_weakening)` — when the agent wants to flag uncertainty
- `retrieve(query, filter=recent_strengthening)` — for "what have I learned lately"

Also: **expose trend via a new primitive**. If P6 lands (removing startup context), this becomes `msam trends --filter strengthening` as its own command. If startup context stays, add a fifth section to it (`recently_strengthening` — beliefs the system is gaining confidence in). Either way, the value is giving the agent a signal that the memory system is actively learning.

#### Why

P1 adds the data; P4 makes it useful as an API surface, not just a ranking tweak. And for self-reflection ("how has my understanding of the user changed?") this is the single cleanest primitive.

#### Acceptance criteria

- New CLI: `msam trends --filter strengthening --limit 20`
- `msam context` output includes a `recently_strengthening` section (configurable)
- Integration test: simulate 30 days of positive outcomes on an atom, verify trend transitions `stable → strengthening`

---

### P5 — LLM-wrapper auto-extraction from conversation turns

**Value: high for production, medium for benchmark. Effort: ~3–5 days. Reversible: yes.**

#### What

Hindsight's headline integration story is "two lines of code": wrap your LLM call, and the system auto-extracts narrative facts from every turn. MSAM currently requires explicit `store_atom` calls or batch ingestion.

Build `msam/wrapper.py`:

```python
from msam import wrap
client = wrap(OpenAI())  # or Anthropic, etc.

# All calls go through; the wrapper:
# 1. Before the model call, retrieves relevant atoms, injects context
# 2. After the call, extracts facts from the user-assistant exchange
#    into atoms + triples, stores them
```

Fact extraction reuses `annotate.py` + `triples.py`. Extraction runs async so it doesn't block the user-visible response.

#### Why

Memory systems that require explicit store calls have adoption friction. The wrapper pattern is why Mem0, Zep, and Hindsight are picking up deployments. It's also the only way MSAM gets tested on long-running, real conversations (not synthetic haystacks).

#### Caveats

- **Latency budget.** Pre-call retrieval adds 200–500ms. Behind a feature flag.
- **Extraction cost.** One small LLM call per assistant turn. NIM free tier or self-host.
- **Doesn't move LongMemEval much.** The benchmark gives us the full haystack upfront; the wrapper's value is in *live* use, not eval.

#### Acceptance criteria

- `wrap()` works with both `openai` and `anthropic` clients
- Round-trip example (`examples/wrapper_integration.py`): start empty MSAM, have a 5-turn conversation, verify the user's stated facts are retrievable by the end
- Latency overhead ≤ 400ms p95 (measured with mock LLM)

---

---

### P7 — Batch the triple extraction LLM call

**Value: enables graph-pathway benchmarking. Effort: ~1 day. Reversible: yes.**

#### What

`msam/triples.py::extract_triples_llm` currently makes one LLM call per atom. MSAM's bench ingest is a serial `for turn in turns: store_atom(...)` loop, so with extraction enabled you eat one network round-trip per atom.

On LongMemEval that's 500 questions × ~500 atoms × ~1.5s = ~100 hours of ingest. Way too slow to actually benchmark the graph pathway's contribution. Hindsight avoids this by batching 5–10 items per retain-extract call (visible as `sub-batch N/M` in their logs).

Add a `extract_and_store_batch(atoms: list[dict]) -> list[int]` that packs multiple atom contents into one extraction prompt and parses the response back per atom. Use it from `ingest.py`'s main loop.

#### Why this matters *now*

P3 lands a graph pathway that returns `[]` whenever the triple store is empty. Without P7, we can't measure the graph pathway's real contribution — we'd be picking between "turn extraction on and wait 100h" or "leave it off and measure nothing." P7 unblocks the measurement.

It's also the right shape for the P5 wrapper later (the wrapper extracts on the assistant-turn boundary, which is naturally batchable across the turn's content chunks).

#### Acceptance criteria

- Ingest throughput with extraction enabled: ≥ 5× current per-atom pace. Concretely: LongMemEval 500-question run with `[triples] enable_extraction = true` completes in ≤ 6h (matches or beats Hindsight's wall clock).
- Per-atom triple yield doesn't drop vs. the single-shot version (prompt format has to be robust under batching).
- Existing `extract_and_store(atom_id, content)` remains as a thin wrapper over the batched version for the non-bench call sites.

#### Risks

- Prompt parsing fragility — batching increases the odds the model emits a stray extra triple or drops one. Mitigate with a per-batch header + explicit `ATOM_N:` delimiters and a robust regex on the response.
- Larger prompts hit context limits for pathological atoms (LongMemEval has some very long turns). Cap batch size by token budget, not item count.

---

### P6 — Remove the startup context and delta encoding

**Value: cleanup. Effort: ~2 hours. Reversible: yes (revert a commit).**

#### What

Delete:
- `msam/remember.py`: `cmd_context()` (~340 lines) and its helpers — `_CODEBOOK`, `_compress`/`_decompress`, `_shannon_floor_tokens`, `_load_hashes`/`_save_hashes`/`_section_hash`, `_DELTA_HASH_FILE` (~60 lines)
- `msam/server.py`: `POST /v1/context` endpoint + `ContextRequest` model (~50 lines)
- One line each in: CLI dispatch, help text, test_server's `/v1/context` test
- `[context]` section from `msam.example.toml`
- Headline "99.3% startup savings" claims from `README.md`, `BENCHMARKS.md`, `CONTROL-FLOW.md`

Leave `msam/subatom.py` alone — subatom extraction is a general-purpose utility even if startup is no longer the primary caller. Leave `msam/api.py`'s Grafana filters referencing `caller=session_startup` — harmless, purely a no-op metrics filter.

Total: ~450 LOC deleted, zero refactoring.

#### Why

Two reasons, independent of Hindsight:

1. **The default startup queries don't match real deployments.** When this was exercised against muninn's 1,242-active-atom corpus (2026-04-19), the canonical queries — `"agent identity core traits personality"` / `"user preferences relationship current situation"` / `"what happened today recent activity"` / `"emotional state mood current feeling"` — returned mediocre matches (similarity 0.30–0.52) that were not identity, not partner info, not emotional state, and not the most salient recent activity. They were whatever atoms happened to contain those words. The feature depends on a corpus style (atoms *about* the agent and its partner) that MSAM's actual users don't produce.

2. **Muninn's architecture doesn't need it.** Jason's stated position: agent state is in context immediately at session start. Startup context was designed as a replacement for the "cold-read SOUL.md + USER.md + MEMORY.md" pattern. If that pattern isn't being used, the feature has no consumer.

Beyond the specific case: `remember.py` is 2,212 lines and growing. The startup-context block is ~20% of the file. Every commit that touches adjacent commands has to reason around it. Zero runtime cost when nobody calls it — but non-zero mental cost for anyone editing `remember.py`.

#### Why this belongs in the Hindsight document

Hindsight's recall layer does *not* have a "startup context" concept at all. Retrieval is on-demand, per-query. The hierarchy (Mental Models → Observations → Raw Facts — see P1) is exposed through ordinary recall, not through a special cold-start path. If P1 lands, the agent gets the same "give me the distilled beliefs first" behavior on every retrieval — startup context becomes strictly redundant.

So: P6 is the cleanup that *follows* P1 naturally. Raw facts → observations works regardless of whether the call is labeled "session start."

#### Acceptance criteria

- Single focused commit (or two: one for delta encoding, one for the rest) with docs updated in the same commit
- `pytest msam/tests/` still passes (only one test touches `/v1/context` — delete it)
- `msam help` output no longer lists `context`
- `README.md` benchmark table either removes the startup-compression row or replaces it with something factual about query-time output volume
- No dangling imports, no broken Grafana dashboards (check `api.py` metric query filters still resolve)

#### Risks

- **Narrative cost.** The "Shannon-compressed startup" is the README's most distinctive claim. Removing it means repositioning MSAM away from "99.3% token savings" as the headline. The underlying architectural advantages (multi-stream, ACT-R scoring, forgetting, world model) don't change — but the pitch does.
- **External API contract.** Any deployment that calls `POST /v1/context` breaks with a 404. Safe to delete in a project that's not versioning its REST surface; otherwise note in release notes.
- **Undo if we're wrong.** Single commit, single revert. Low blast radius.

#### Do this *when*?

After the LongMemEval baseline lands and before P1 work starts. Reason: the removal touches `remember.py` which is where CLI wiring for future P4 trend commands will go — clean slate first.

---

## 4. Ideas from Hindsight that we should *not* copy

### Personality parameters (skepticism, literalism, empathy)

Hindsight exposes these as tunables that shape reflection. Dressed up as architecture, it's prompt-template selection. MSAM agents already isolate by `agent_id`; if a deployment wants a skeptical agent they can write the system prompt themselves. Adding tunables just creates more configuration surface we'd need to test and document.

### Mental Models as user-curated summaries

The "user manually writes summaries of common queries" premise is friction most deployments won't pay. MSAM's `pinned` atoms serve the same function (human-authored, protected from decay) without the ceremony of a dedicated tier. If P1 lands, we already have two tiers (raw / observation); adding a third curated tier is premature.

### TEMPR's cross-encoder reranker

Hindsight reranks fused results with a cross-encoder (a small transformer that scores query-atom pairs). Real quality win, but: (a) it's a dependency (either a local model or an API), (b) our N after fusion is already ≤ 20 and the reader's in-context reasoning does the final ranking anyway, (c) adds ~100–300ms latency. Revisit only if benchmark plateaus and we've exhausted the cheaper knobs.

---

## 5. Proposed sequence

Order chosen to maximize information gain per unit of work:

1. **Establish the baseline first** (done — see `msam/benchmarks/longmemeval/` and `BENCHMARK-RESULTS.md`). No proposal is worth implementing without a measurable starting point.
2. **P6 (remove startup context + delta encoding).** Clean slate. 2 hours, one commit, makes everything downstream easier to reason about. Do right after baseline, before P1. ✅ landed (`39b633d`)
3. **P2 (RRF).** Smallest diff, cleanly A/B-able. Tells us if our current weighted-sum fusion is actually holding us back. ✅ landed (`5f665d1`); default flipped to `rrf`.
4. **P3 (four-pathway split).** RRF is only meaningful if pathways are independent. Do these together. ✅ landed (`d9d7602`); graph pathway returns `[]` until triples exist.
5. **P7 (batch triple extraction).** Unblocks measurement of P3's graph pathway. Do before P1 if we want to know what the graph lever is actually worth.
6. **P1 (observations tier).** Where the biggest expected win lives. Build on a benchmark that already moves. Also subsumes the retrieval behavior that startup context was trying to emulate.
7. **P4 (trend-as-filter).** Small icing on P1. Only interesting if P1 shows a lift.
8. **P5 (wrapper).** Production-value work, do it when the architecture pieces are stable.

Each step gets its own benchmark run. The goal is a dose-response curve, not a big-bang reveal.

---

## 6. Measurement

Every proposal lands behind a flag and gets compared against `msam_baseline_v0` on LongMemEval `S`. The pipeline already exists; we add `--run-tag` per variant:

```
msam_baseline_v0         # main, no changes
msam_rrf_v1              # P2
msam_rrf_pathways_v1     # P2 + P3
msam_observations_v1     # P2 + P3 + P1 (default bonuses)
msam_observations_v2     # P2 + P3 + P1 (tuned bonuses)
msam_full_v1             # P1..P4
```

Per-question-type breakdown is the real signal — overall accuracy can move for the wrong reason. Watch:

- `single-session-user` as a regression canary (should stay flat)
- `multi-session` as the observations win (P1 should move it)
- `temporal-reasoning` as the pathway-split win (P3 should move it)
- `knowledge-update` as the trend win (P4 should move it; older-contradicted atoms should be demoted)

Also track:
- p50 / p95 query latency across variants (regression budget: +100 ms p50)
- GPT-4o judge cost per run (~$2)
- Token usage in the reader prompt (RRF and observations may change what gets surfaced, which changes context size)

---

## 7. Open questions

- **Does MSAM's existing felt_consequence signal carry enough information for trend computation, or does the trend labeller need its own access log?** Spike: run P1 with trends computed purely from `record_outcome` history; if trends look noisy, layer in retrieval-frequency data.
- **For P3, is the temporal pathway worth a first-class retrieval mode, or is it better as a post-filter on the other three?** Hindsight treats it as a first-class mode; MSAM could go either way. Measure.
- **How does P1 interact with `felt_consequence`'s existing score boosting?** Potential for double-counting (an observation that's been positively reinforced gets boosted once via felt_consequence and again via its evidence_count). Need a single reconciled scoring function, not two bonuses stacked.
- **Do we want a "mental model" tier (P1 extension) at all, or does pinning solve that?** Leaning toward no; revisit if P1 users start asking for it.

---

## 8. Appendix: Hindsight architecture quick reference

From [hindsight.vectorize.io](https://hindsight.vectorize.io/) and the repo README, as of December 2025:

- **Memory types** (priority order at recall): Mental Models → Observations → World Facts / Experience Facts
- **Core ops:** Retain (ingest), Recall (TEMPR retrieval), Reflect (synthesize new beliefs)
- **TEMPR retrieval:** Semantic + Keyword (BM25) + Graph + Temporal, fused via RRF, reranked by cross-encoder
- **Evidence tracking:** each observation carries a proof count; trend labels (stable/strengthening/weakening/stale) refresh as new evidence lands
- **Integration:** LLM wrapper ("two lines of code") auto-extracts facts into entities + relationships + time series
- **Reported:** 91.4% on LongMemEval (first system past 90% per their blog)

Their repo is MIT; worth skimming their TEMPR fusion and observation-refinement code if we implement P1/P2/P3. Don't vendor — our schemas and lifecycle are different enough that copy-paste would create maintenance burden.
