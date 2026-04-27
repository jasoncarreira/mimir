# MSAM Control Flow

**Multi-Stream Adaptive Memory** -- system control flow and architecture reference.

This document maps every path through the MSAM system: how atoms are stored, how queries become confidence-gated output, how decay reclaims budget, and how the feedback loop closes. Each flow corresponds to a code path in production -- the diagrams are extracted from the implementation, not designed before it.

## System Overview

Three entry points drive the system: scheduled heartbeats (cron-based decay, snapshots, canary checks), session events (session boundaries), and direct user or CLI commands (store, query, admin). All paths converge on the same atom store, metrics infrastructure, and feedback loop.

```
                        EXTERNAL TRIGGERS
          Heartbeat(cron) / Session / User / CLI
                    |          |          |
          +---------+          |          +---------+
          v                    v                    v
      +--------+         +----------+          +--------+
      | STORE  |         | RETRIEVE |          | DECAY  |
      +---+----+         +----+-----+          +---+----+
          |                    |                    |
          v                    v                    v
    +-----------+     +---------------+      +-----------+
    | store_atom|     | retrieve_v2   |      | decay_cycle|
    | dedup     |     | confidence    |      | transitions|
    | budget    |     | gating        |      | compaction |
    | embed     |     | metrics       |      | confidence |
    | annotate  |     |               |      | feedback   |
    | metrics   |     |               |      | metrics    |
    +-----------+     +-------+-------+      +-----+-----+
                              |                    |
                              v                    |
                      +-------------+              |
                      | FEEDBACK    |<-------------+
                      | contribute  |
                      | adjust      |
                      | (cycle)     |
                      +-------------+
```

## Primary Flows

The five primary flows below represent the complete operational surface of MSAM. Store and Retrieve handle the read/write path. Decay manages the lifecycle. Feedback closes the loop between retrieval quality and future scoring. Session boundary capture bridges the gap between conversations.

### 1. STORE

```
remember.cmd_store  /  server.api_store
  -> core.get_db (first call: runs SCHEMA_SQL + pending migrations,
                  process-cached so subsequent calls are fast)
  -> core.store_atom
    -> content_hash dedup (SHA256, reject active/fading dupes)
    -> budget check (>95% refuse, >85% auto-compact to lightweight)
    -> embed_text (configured provider, cached)
    -> annotate.heuristic_annotate
      -> classify_stream (semantic / episodic / procedural)
      -> extract topics, arousal, valence
    -> INSERT atoms table (memory_type='raw' by default; 'observation'
                           reserved for consolidation output)
    -> triples.extract_triples (SPO extraction; gated by
                                [triples] enable_extraction)
    -> metrics.log_store + log_access_event
```

### 1b. CONSOLIDATE (Sleep Cycle)

```
server.api_consolidate  /  ConsolidationEngine.consolidate
  -> _cluster_phase: greedy anchor-and-absorb over active raw atoms
       within each stream. Threshold = [consolidation] similarity_threshold
       (default 0.80); min cluster size = min_cluster_size (default 3).
  -> skip-on-identical: clusters whose source set already has an
       observation are skipped.
  -> _synthesize_phase: LLM rolls each cluster into one observation atom
       (memory_type='observation', evidence_count=N).
  -> _restructure_phase:
       INSERT observation atom
       INSERT atom_relations rows: observation -evidenced_by-> raws
       optionally INSERT supersedes edges between observations whose
       evidence set is a strict superset of an existing observation
       reduce source raw stability by stability_reduction_factor (0.5)
```

### 2. RETRIEVE (Two-Tier, Confidence-Gated)

The canonical path is `core.hybrid_retrieve` with `two_tier=True`, gated
through the REST `/v1/query` endpoint. `retrieval_v2.py` is no longer
the default pipeline — its useful pieces (query rewriting, synonym
expansion, atom quality scoring) were cherry-picked into
`hybrid_retrieve` as opt-in flags (P11/P12/P13).

```
server.api_query  /  remember.cmd_query
  -> core.hybrid_retrieve(query, two_tier=True)
    -> P11 (opt-in): _apply_query_rewriting
        pattern-based rewrites (user -> User, agent -> Agent, plus
        user-supplied [retrieval_v2.entity_mappings])
    -> P12 (opt-in): _expand_query_for_keyword
        append synonyms from [query_expansion.synonyms] to the
        keyword pathway only (semantic side handles synonyms via
        embedding similarity)

    -> CANDIDATE FETCH (split by memory_type):
        -> retrieve(memory_type='observation')  -- semantic similarity
        -> keyword_search(memory_type='observation')  -- FTS5 BM25
        -> retrieve(memory_type='raw')           -- semantic similarity
        -> keyword_search(memory_type='raw')     -- FTS5 BM25

    -> RRF FUSION (per pool):
        -> observations: RRF over (semantic, keyword)
        -> raws:         RRF over (semantic, keyword [+ graph, temporal
                         if pathways enabled])

    -> P13 (opt-in): _apply_quality_scoring
        compute_atom_quality(content) -> ×0.5 if <0.3, ×1.1 if >0.7
        applied to both pools, re-rank

    -> _two_tier_split (P9 / P30):
        -> observation supersedes demotion (skip-on-identical,
           strict-superset between observations from consolidation)
        -> obs surfacing gate: similarity >= obs_conf_min_sim (0.30)
                              AND rank within observations_top_k
        -> for each surfaced observation:
              find evidenced_by raws via atom_relations
              boost = (1 / stability_reduction) × obs_score
              for in-pool raws: score = base + min(boost, cap × base)
              for missing raws: pull in with cosine-derived base score,
                                then apply same formula
        -> per-atom _confidence_tier from each atom's own similarity

  -> REST: api_query
    -> per-atom filter by min_confidence_tier (default "low";
       drops only "none"-tier atoms unless caller raises the floor)
    -> return {"observations": [...], "raws": [...]}

  -> CLI: remember.cmd_query
    -> CONFIDENCE-GATED OUTPUT:
        high:   full atoms (zero-sim pruned), <= 12 triples
        medium: top 3 atoms (sim > 0.15), <= 8 triples
        low:    1 atom, 0 triples, advisory text
        none:   0 atoms, 0 triples, advisory only

  -> SESSION DEDUP:
    -> check served atom IDs (file-based, hourly window)
    -> flag previously_served atoms
    -> record served IDs for next query

  -> metrics.log_access_event
```

### 3. DECAY CYCLE

```
decay.run_decay_cycle (heartbeat, hourly)
  -> compute_all_retrievability
    -> R(t) = e^(-t/S) per active/fading atom
  -> transition_states
    -> protected set: recently accessed + pinned
    -> active -> fading (R < 0.3, not protected)
    -> fading -> dormant (R < 0.1, not protected)
    -> log_forgetting (reason + factors)
  -> compact_profiles (under token pressure)
    -> full -> standard -> lightweight
  -> decay_confidence
    -> 0.01/day after 7-day grace, floor 0.1
  -> compute_retrieval_adjustments (feedback loop)
    -> over-retrieved + low-contribution -> dampen (0.9x)
    -> high-contribution -> boost (1.1x)
  -> metrics.log_decay
```

### 4. FEEDBACK LOOP

```
retrieve -> atoms surfaced with scores
  |
  v
mark_contributions -> which atoms influenced the response
  -> content overlap detection
  -> UPDATE access_log SET contributed=1
  |
  v
compute_retrieval_adjustments -> (runs in decay cycle)
  -> contribution_rate per atom
  -> dampen over-retrieved noise, boost high-value atoms
  |
  v
retrieve -> adjusted scores from stability changes
  (cycle repeats)
```

### 5. SESSION BOUNDARY CAPTURE

```
heartbeat -> scripts/session-capture.sh
  -> check memory/context/last-session-summary.md
  -> if non-empty:
    -> msam store "<summary>" (episodic atom)
    -> clear the file
  -> check memory/context/ for stale files
  -> msam snapshot (metrics to Grafana)
```

### 6. STREAM CLASSIFICATION

```
annotate.classify_stream(content)
  -> PROCEDURAL check (word-boundary regex):
    -> "how to", "step 1", "install", "always", "never", "rule:", etc.
  -> EPISODIC check (temporal + conversational markers):
    -> dates, "yesterday", "user said", "we decided", "session", etc.
  -> DEFAULT: semantic (facts, knowledge, descriptions)
```

### 7. OUTCOME FEEDBACK (Felt Consequence)

```
Agent responds to user
  -> user provides feedback (positive/negative)
  -> core.record_outcome(atom_ids, score)
    -> for each atom:
      -> existing = get current outcome_score (0.0 if none)
      -> decayed = existing * outcome_decay (0.95)
      -> new_score = decayed + score
      -> UPDATE retrieval_outcomes SET outcome_score = new_score, feedback_count += 1

retrieve -> activation scoring:
  -> if atom.feedback_count >= min_outcomes_for_effect (3):
    -> activation += outcome_weight * outcome_score
  -> high outcome_score = boosted retrieval
  -> low/negative outcome_score = dampened retrieval
```

### 8. WORLD MODEL (Temporal Knowledge)

```
triples.update_world(subject, predicate, object, valid_from, valid_until)
  -> CHECK world_model.enabled (skip if disabled)
  -> CHECK world_model.temporal_extraction
    -> if disabled: strip valid_from/valid_until
    -> if enabled: default valid_from = now
  -> CHECK auto_close_on_conflict (and temporal_extraction):
    -> SELECT existing triples WHERE subject=s AND predicate=p AND valid_until IS NULL
    -> UPDATE existing SET valid_until = now (auto-close)
  -> INSERT new triple with temporal metadata

triples.query_world(entity)
  -> CHECK world_model.enabled (return [] if disabled)
  -> SELECT * FROM triples WHERE subject = entity AND valid_until IS NULL
  -> returns current state of the world for this entity

triples.world_history(entity)
  -> CHECK world_model.enabled (return [] if disabled)
  -> SELECT * FROM triples WHERE subject = entity ORDER BY valid_from
  -> returns full temporal chain (past + current)
```

### 9. PREDICTIVE CONTEXT ASSEMBLY

```
prediction.PredictiveEngine.predict_context(hour, day_of_week)
  -> WARMUP GATE:
    -> count session_boundary atoms in DB
    -> if count < warmup_sessions (50): return [] (not enough data)

  -> TEMPORAL PATTERN QUERY:
    -> SELECT atoms from temporal_patterns
      WHERE hour_of_day within +/- temporal_window_hours (2)
      AND retrieval_count >= min_pattern_count (5)
    -> returns atoms frequently retrieved at this time of day

  -> CO-RETRIEVAL EXPANSION:
    -> for each temporal candidate:
      -> SELECT co-retrieved atoms from co_retrieval
        WHERE co_count >= co_retrieval_threshold (3)
      -> add co-retrieved atoms to candidate pool

  -> MERGE + RANK:
    -> deduplicate, weight by pattern strength
    -> cap at max_predicted_atoms (8)
    -> return predicted atom set for context injection
```

### 10. AGREEMENT TRACKING (Sycophancy Detection)

```
Agent responds to user
  -> metrics.record_agreement(agreed: bool, agent_id)
    -> INSERT INTO agreement_signals (agent_id, agreed, timestamp)

metrics.get_agreement_rate(agent_id, window)
  -> SELECT last N signals for agent_id
  -> compute: agreement_rate = sum(agreed) / count
  -> if rate > warning_threshold (0.85):
    -> flag: sycophancy_warning = true
  -> return { rate, count, warning }
```

## State Machines and Decision Trees

The following diagrams show the internal state machines that govern atom lifecycle and retrieval confidence. These are the core invariants of the system -- every atom follows the same state machine, and every query passes through the same confidence gate.

## Atom State Machine

```
                  store_atom()
                      |
                      v
  +--------------  ACTIVE  <--------------+
  |             (default)                  |
  |                 |                      |
  |      R < 0.3   |     access            |
  |   (not pinned) |    (reactivate)       |
  |                 v                      |
  |            FADING  -------------------+
  |                 |
  |      R < 0.1   |
  |   (not pinned) |
  |                 v
  |            DORMANT
  |                 |
  |      (manual    |
  |       only)     |
  |                 v
  +----------> TOMBSTONE
              (never deleted)
              (content retained)
              (history preserved)
```

## Confidence Tier Decision Tree

```
Query
  |
  v
Has atoms with similarity?
  |
  +-- No atoms, no triples -> NONE (0 tokens)
  |
  +-- Triples only (no atom similarity) -> LOW (0-33 tokens)
  |
  +-- Has atoms:
        |
        +-- Is temporal query? ("right now", "today", etc.)
        |     |
        |     +-- Has recent atoms (24h) with sim >= 0.30? -> use normal tiers
        |     +-- No recent atoms? -> demote all to LOW
        |
        +-- Best atom similarity:
              |
              +-- >= 0.45 -> HIGH (140-176 tokens)
              +-- >= 0.30 -> MEDIUM (91-131 tokens)
              +-- >= 0.15 -> LOW (0-33 tokens)
              +-- < 0.15  -> NONE (0 tokens)
```

## Adaptive Scaling Gates

The system self-tunes retrieval strategy based on database size. At small scale (< 10K atoms), a single retrieval call is sufficient. As the database grows, multi-beam search activates automatically to maintain recall quality at the cost of additional latency. This gate prevents paying a scale tax before scale arrives.

```
Atom count check (per query):
  |
  +-- < 10,000 atoms:
  |     -> single hybrid_retrieve (1 embedding call)
  |     -> latency: ~870ms
  |
  +-- >= 10,000 atoms:
  |     -> beam_search_retrieve (3 beams, merged)
  |     -> latency: ~3,000ms (projected)
  |
  Configuration:
    enable_beam_search = "auto" | true | false
    beam_search_atom_threshold = 10000
    beam_width = 3
```

## Module Dependencies

The dependency graph below shows how the 24 modules connect. The CLI layer (`remember.py`) and REST API (`server.py`) are the two primary entry points. Both delegate to the same core engine, ensuring consistent behavior across CLI and API access.

```
remember.py (CLI + gating layer)
  +-- core.py (atoms, retrieval, scoring)
  +-- retrieval_v2.py (v2 pipeline, beam search, entity roles)
  |     +-- entity_roles.py (entity-aware scoring)
  +-- triples.py (knowledge graph, hybrid retrieval)
  +-- subatom.py (sentence extraction, dedup)
  +-- session_dedup.py (multi-turn dedup)
  +-- annotate.py (stream classification, topic extraction)
  +-- prediction.py (predictive prefetch, 3 strategies)
  +-- config.py (TOML configuration)
  +-- metrics.py (observability)

core.py (atom engine)
  +-- embeddings.py (NIM, OpenAI, ONNX, local providers)
  +-- vector_index.py (FAISS-backed ANN search)
  +-- config.py

decay.py (lifecycle)
  +-- core.py
  +-- forgetting.py (intentional forgetting engine)
  +-- metrics.py

server.py (REST API, FastAPI)
  +-- core.py
  +-- triples.py
  +-- decay.py
  +-- agents.py (multi-agent isolation)
  +-- prediction.py
  +-- contradictions.py (semantic contradiction detection)

api.py (Grafana HTTP)
  +-- metrics DB (direct SQL)
  +-- core DB (live queries)

consolidation.py (sleep-inspired consolidation)
  +-- core.py
  +-- config.py

calibration.py (cross-provider identity)
  +-- core.py
  +-- embeddings.py
```

## Key Numbers

| Metric | Value |
|--------|-------|
| Modules | 24 |
| CLI commands | 56 |
| REST API endpoints | 19 |
| Tests | 437 across 25 test files |
| Atoms | 675+ |
| Triples | 1,500+ |
| DB size | ~26MB |
| Markdown baseline (per query) | 7,327 tokens |
| Compression vs markdown (per query) | 96.5–100% |
| Query latency | ~870ms |
| Embedding providers | 4 (NVIDIA NIM, OpenAI, ONNX Runtime, sentence-transformers) |
| Config sections | 27 (160+ parameters) |
