# saga

Persistent memory for AI agents — atoms, observations, triples, and a
two-tier retriever that knows what it knows and what it doesn't.

Originally [SAGA][saga] (Multi-Stream Adaptive Memory) by Jaden
Schwab. Heavily modified and renamed; see [LICENSE](./LICENSE) for the
combined copyright. saga is the memory backend of [mimir](../README.md)
but is usable standalone via a Python library or HTTP server.

[saga]: https://github.com/jadenschwab/saga

## What changed from SAGA

If you arrive here from the SAGA repo, the differences worth knowing:

- **Two-tier retrieval is the canonical mechanism.** Observations
  (consolidation-synthesized beliefs) and raws (direct atoms) ride
  separate ranking pools, fused via Reciprocal Rank Fusion at the
  response layer. Single-tier mode still exists for back-compat but
  isn't recommended.
- **Triple extraction during consolidation.** Each consolidation pass
  produces an `OBSERVATION` plus a `TRIPLES` block (subject /
  predicate / object) embedded for cosine retrieval. The same call
  also flags within-cluster `CONTRADICTIONS`.
- **Temporal world model.** Triples carry `valid_from` and
  `valid_until`. Same-(subject, predicate) updates auto-close the
  prior triple's `valid_until`; queries can ask for current state,
  state at a point in time, or the full history of an entity.
- **Triples-in-response (P42).** Top-K triples ranked by query-vs-
  triple cosine surface as a third response block alongside
  observations and raws. Includes valid dates + source_atom_id so
  the agent can backtrack.
- **Trend writer (P47).** Consolidation labels each new observation
  `improving` / `stable` / `weakening` / `stale` based on access-log
  decay of its source atoms. Activates per-trend retrieval-score
  multipliers and feeds promotion / demotion candidate selection.
- **Canonical predicate vocabulary (P48).** Consolidation prompt
  prefers reusing existing predicates / subjects (DB-derived top-N
  + a static seed) over inventing compound domain-specific
  predicates per cluster.
- **Contextual rewrite + missing-reference pivot.** Short
  referential queries ("yes, look for that") get rewritten with
  recent context before retrieval. Otherwise-unmatchable references
  pivot to similarity floor instead of returning empty.
- **Session boundaries.** Atoms tagged `session_boundary` carry
  end-of-session summaries and unfinished-item lists. Retrieval
  excludes them by default; mimir surfaces them in turn prompts
  via a separate path.
- **In-process default.** mimir uses saga as a Python library
  (asyncio.to_thread on the hot paths). HTTP server (`saga serve`)
  is still available for cross-process / cross-language deployments.

The full evolution lives in [BENCHMARK-RESULTS.md](./BENCHMARK-RESULTS.md)
and [NEXT-EXPERIMENTS.md](./NEXT-EXPERIMENTS.md).

## Quickstart (standalone)

If you're using saga from inside mimir, ignore this section — `mimir
setup` configures saga.toml automatically. This is for direct use.

```bash
# Install
cd saga
pip install -e .  # or `uv pip install -e .`

# Configure
cp saga.example.toml ~/saga/saga.toml

# Initialize the DB
python -m saga.init_db

# Library use
python -c "
import saga
atom_id = saga.store_atom('User prefers dark mode', stream='semantic')
result = saga.hybrid_retrieve('what does the user like?', top_k=5)
print(result)
"

# HTTP server (port 3002 by default)
python -m saga.server
```

## Architecture sketch

```
┌────────────┐    write       ┌──────────────────────────┐
│  agent     │ ───────────►  │  store_atom              │
│  (mimir,   │                │  ↳ embed + dedup + log   │
│   custom)  │                └──────────────────────────┘
└─────┬──────┘                            │
      │ query                             ▼
      │                  ┌────────────────────────────────┐
      │                  │  consolidation (cron)          │
      │                  │  cluster → LLM synthesize →    │
      │                  │   OBSERVATION + TRIPLES +      │
      │                  │   CONTRADICTIONS               │
      │                  │  → write observation atom +    │
      │                  │   embedded triples + edges     │
      │                  └────────────────────────────────┘
      ▼
┌─────────────────────────────────────────────────────────┐
│  hybrid_retrieve  (the two-tier path)                   │
│   ┌─────────────────┐    ┌────────────────────────────┐ │
│   │ Observations    │    │ Raws                       │ │
│   │ pool — top-K    │    │ pool — top-K, RRF over    │ │
│   │ from semantic + │    │ semantic + keyword (+      │ │
│   │ keyword         │    │ optional augmentations)   │ │
│   └────────┬────────┘    └─────────────┬──────────────┘ │
│            │  evidenced_by edges:       │               │
│            └─► boost endorsed raws ◄────┘               │
│                                                         │
│  + optional triples block (P42): top-K triples cosine-   │
│    matched to query embedding, returned alongside       │
└─────────────────────────────────────────────────────────┘
                         │
                         ▼
                ┌─────────────────────┐
                │  /v1/query response │
                │  observations: [...] │
                │  raws:         [...] │
                │  triples:      [...] │
                └─────────────────────┘
```

Decay runs as a separate cron: stability-weighted activation drift
plus state transitions (active → fading → dormant → tombstone).
Nothing is deleted; everything is auditable.

## Documentation

- **[SPEC.md](./SPEC.md)** — original SAGA specification (covers
  the foundational ACT-R + multi-stream architecture)
- **[BENCHMARK-RESULTS.md](./BENCHMARK-RESULTS.md)** — every benchmark
  run with per-category breakdowns, ordered by date
- **[NEXT-EXPERIMENTS.md](./NEXT-EXPERIMENTS.md)** — the experimental
  roadmap. Items are tracked through filed → tested → shipped /
  rejected. Currently active: P40, P47, P48.
- **[CONTROL-FLOW.md](./CONTROL-FLOW.md)** — request/response flow
  through the retrieve / store / consolidate hot paths
- **[HINDSIGHT-IDEAS.md](./HINDSIGHT-IDEAS.md)** — early design
  inspirations, including ideas borrowed from the Hindsight
  framework

## Configuration

Saga reads its config from `<home>/saga.toml`. The full surface lives
in [`saga.example.toml`](./saga.example.toml) with inline comments.
Defaults are conservative; the only flag most operators flip is
`[embedding] provider` (NVIDIA NIM, OpenAI, ONNX, sentence-
transformers, fastembed, Voyage AI).

`[retrieval]` is the section to know:
- `two_tier_enabled = true` (canonical)
- `enable_contextual_rewrite = true` (canonical)
- `enable_missing_ref_pivot = true` (canonical)
- `enable_confidence_gating = true` (canonical)
- `default_min_confidence_tier = "low"` (canonical)
- `include_triples_in_response` — opt in for P42-shape responses
- `enable_endorsed_atom_pull_in` — opt out (P40 boost-only mode)

### `[consolidation] similarity_threshold` — tune per embedding model

The default `similarity_threshold = 0.80` is calibrated for NVIDIA NIM
`nv-embedqa-e5-v5` (saga's historical default) and OpenAI
`text-embedding-3-small` — both work cleanly at 0.80. Other embedding
models produce tighter cosine distributions and need a higher
threshold to keep saga's consolidator from saturating its `n=50`
cluster cap.

Recommended values from offline sweeps on LongMemEval-S kept saga DBs:

| Embedding model | Dims | Recommended threshold | At default 0.80 |
|---|---|---|---|
| NVIDIA NIM `nv-embedqa-e5-v5` | 1024 | **0.80** | clean |
| OpenAI `text-embedding-3-small` | 1536 | **0.80** | clean |
| OpenAI `text-embedding-3-large` | 3072 | **0.80** (untuned, likely fine) | likely clean |
| Voyage `voyage-4-lite` | 1024 | **0.92** | cap-saturates 100% |
| Voyage `voyage-3-large` | 1024 | **~0.90** (untuned estimate) | likely cap-saturates |
| fastembed `BAAI/bge-small-en-v1.5` | 384 | **0.92** | cap-saturates 98.8% |
| fastembed `BAAI/bge-base-en-v1.5` | 768 | untested | unknown |
| fastembed `BAAI/bge-large-en-v1.5` | 1024 | untested | unknown |

The thresholds were picked as the lowest value where `cap_hit_pct`
drops below 5% with `mean_cluster_size` in the healthy [2, 4] range.
Cap-saturation **doesn't materially hurt accuracy** (Phase 2A bench
result — the first 50 clusters capture the load-bearing merges) but
does waste wall-clock: tuned thresholds give ~2× faster consolidation
without quality loss.

**Shortcut**: set `similarity_threshold = "auto"` in `[consolidation]`
and saga resolves to the per-provider value at boot. Default for
fresh installs via `mimir setup`.

```toml
[consolidation]
similarity_threshold = "auto"   # 0.92 for voyage/fastembed, 0.80 otherwise
```

### `[embedding] provider = "voyage"` — first-class Voyage shortcut

`provider = "voyage"` is a one-line shortcut for the Voyage AI API. It
bakes in voyage's required URL + `send_input_type = true` + default
`api_key_env = "VOYAGE_API_KEY"`, so the minimal config is:

```toml
[embedding]
provider = "voyage"
model = "voyage-4-lite"   # or voyage-3-large, voyage-4, voyage-4-large
dimensions = 1024
api_key_env = "VOYAGE_API_KEY"
```

Operators can still override any field via saga.toml; the shortcut
just provides correct defaults so a fresh install picking voyage
needs no other voyage-specific knowledge.

### `[embedding] send_input_type` — manual Voyage compatibility

Voyage AI's embedding models REQUIRE the `input_type` parameter
(`"query"` / `"document"`) to produce retrieval-quality embeddings —
the models are trained with instruction prefixes that don't get
applied without it. OpenAI's API rejects the parameter as unknown,
so the flag is opt-in:

```toml
[embedding]
provider = "openai"
url = "https://api.voyageai.com/v1/embeddings"
model = "voyage-4-lite"
dimensions = 1024
api_key_env = "VOYAGE_API_KEY"
send_input_type = true   # Voyage compat
```

Without this flag, voyage scores **0.70** aggregate on LongMemEval-S;
with it, voyage scores **0.904** aggregate (vs OpenAI's 0.880). The
prefix is load-bearing. Default `false` so existing OpenAI deployments
are unaffected.

P48 (canonical predicate vocabulary in the consolidation prompt) is
always on whenever `[triples] enable_extraction = true` — the block
is a vocabulary hint, not enforcement, and reduces predicate aliasing
across clusters with no measured downside.

## License

[MIT](./LICENSE) — combined copyright (Jaden Schwab + Jason Carreira),
see [LICENSE](./LICENSE) for the lineage notes.
