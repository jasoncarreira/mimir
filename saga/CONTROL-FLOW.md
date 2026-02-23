# MSAM Control Flow

**Multi-Stream Adaptive Memory** -- system control flow and architecture reference.

This document maps every path through the MSAM system: how atoms are stored, how queries become confidence-gated output, how decay reclaims budget, and how the feedback loop closes. Each flow corresponds to a code path in production -- the diagrams are extracted from the implementation, not designed before it.

## System Overview

Three entry points drive the system: scheduled heartbeats (cron-based decay, snapshots, canary checks), session events (startup context, session boundaries), and direct user or CLI commands (store, query, admin). All paths converge on the same atom store, metrics infrastructure, and feedback loop.

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
    | embed     |     | shannon       |      | confidence |
    | annotate  |     | metrics       |      | feedback   |
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
remember.cmd_store
  -> core.store_atom
    -> content_hash dedup (SHA256, reject active/fading dupes)
    -> budget check (>95% refuse, >85% auto-compact to lightweight)
    -> embed_text (configured provider, cached)
    -> annotate.heuristic_annotate
      -> classify_stream (semantic / episodic / procedural)
      -> extract topics, arousal, valence
    -> INSERT atoms table
    -> triples.extract_triples (SPO extraction)
    -> metrics.log_store + log_access_event
```

### 2. RETRIEVE (Confidence-Gated)

```
remember.cmd_query
  -> retrieve_v2 pipeline:
    -> rewrite_query (entity resolution: aliases -> canonical names)
    -> detect_temporal_scope ("right now" / "today" -> require recent atoms)
    -> ADAPTIVE GATE:
        atoms < 10K? -> single hybrid_retrieve
        atoms >= 10K? -> beam_search_retrieve (3 beams, merged)
    -> triple_augment (entity-linked atoms from triple graph)
    -> entity_role_scoring (query entity vs atom entity matching)
    -> quality_filter (penalize low-quality atoms)
    -> sort + trim to top_k

  -> triples.hybrid_retrieve_with_triples
    -> triples.retrieve_triples (embedding similarity on SPO store)
    -> merge atoms + triples within token budget

  -> CONFIDENCE TIER CLASSIFICATION:
    -> per-atom: similarity thresholds (high >= 0.45, medium >= 0.30, low >= 0.15)
    -> temporal demotion: stale atoms capped at "low" for temporal queries
    -> overall tier: best tier across all atoms

  -> CONFIDENCE-GATED OUTPUT:
    -> high:   full atoms (zero-sim pruned), <= 12 triples
    -> medium: top 3 atoms (sim > 0.15), <= 8 triples
    -> low:    1 atom, 0 triples, advisory text
    -> none:   0 atoms, 0 triples, advisory only

  -> SESSION DEDUP:
    -> check served atom IDs (file-based, hourly window)
    -> flag previously_served atoms
    -> record served IDs for next query

  -> SHANNON METRICS:
    -> compute post-gate token count
    -> compute Shannon entropy floor
    -> output: raw_tokens, compressed_tokens, shannon_floor, efficiency %

  -> metrics.log_access_event
```

### 3. CONTEXT STARTUP (Shannon-Compressed)

```
remember.cmd_context
  -> 4 retrieval queries:
    -> identity:  "agent identity core traits personality"
    -> partner:   "user preferences relationship current situation"
    -> recent:    "what happened today recent activity"
    -> emotional: "emotional state mood current feeling"

  -> COMPRESSION PIPELINE (per section):
    -> subatom extraction (sentence-level, not full atoms)
    -> codebook compression (Agent->A, User->U, MSAM->M, etc.)
    -> semantic dedup (0.75 similarity threshold)

  -> DELTA ENCODING (identity + partner only):
    -> hash section content
    -> compare to stored hash from last startup
    -> if unchanged: emit "[no_change]" marker (~1 token)
    -> if changed: emit full content, update stored hash

  -> output: 51 tokens (delta) / 90 tokens (first-run)
  -> comparison: 7,327 tokens markdown baseline
  -> savings: 99.3%
```

### 4. DECAY CYCLE

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
  -> expire_working_memory
    -> access_count > 3: PROMOTE to episodic
    -> TTL expired: TOMBSTONE
  -> metrics.log_decay
```

### 5. FEEDBACK LOOP

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

### 6. SESSION BOUNDARY CAPTURE

```
heartbeat -> scripts/session-capture.sh
  -> check memory/context/last-session-summary.md
  -> if non-empty:
    -> msam store "<summary>" (episodic atom)
    -> clear the file
  -> check memory/context/ for stale files
  -> msam snapshot (metrics to Grafana)
```

### 7. STREAM CLASSIFICATION

```
annotate.classify_stream(content)
  -> PROCEDURAL check (word-boundary regex):
    -> "how to", "step 1", "install", "always", "never", "rule:", etc.
  -> EPISODIC check (temporal + conversational markers):
    -> dates, "yesterday", "user said", "we decided", "session", etc.
  -> DEFAULT: semantic (facts, knowledge, descriptions)
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
  +-- subatom.py (Shannon compression, sentence extraction)
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
| CLI commands | 54 |
| REST API endpoints | 20 |
| Tests | 264 across 20 test files |
| Atoms | 675+ |
| Triples | 1,500+ |
| DB size | ~26MB |
| Startup tokens | 51 (delta) / 90 (first-run) |
| Markdown baseline | 7,327 tokens |
| Compression | 99.3% |
| Query latency | ~870ms |
| Shannon efficiency | 51% of theoretical minimum |
| Embedding providers | 4 (NVIDIA NIM, OpenAI, ONNX Runtime, sentence-transformers) |
| Config sections | 23 (100+ parameters) |
