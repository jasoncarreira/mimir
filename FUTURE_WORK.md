# Mimir — Future Work

**Status:** living document
**Owner:** jcarreira
**Last updated:** 2026-04-26

This is the backlog: improvements identified during design, build, and benchmarking that aren't blocking the current architecture but should be addressed before serious production deployment or as the system scales.

Items are organized by area, not priority. Where an item has a likely sequence with another, the dependency is noted.

---

## 1. Recent activity / channel context

### 1.1 Total-bytes budget on the Recent activity block

**Status:** committed, pre-rollout work.

The current caps are per-message (`MIMIR_RECENT_MESSAGE_CHARS=4096`) and per-pull (`recent_per_channel=10` + `recent_author_cross=10`). Worst case: 20 messages × 4KB ≈ 80KB just for Recent activity. Fine for normal traffic, heavy for chatty channels with long threads.

**Approach:** add `MIMIR_RECENT_TOTAL_CHARS` (default ~16KB). After the merge + de-dup step in `assemble_recent_activity`, drop oldest messages until the rendered block fits. Tokens-aware would be better but requires a tokenizer; a char proxy is fine for v1.

**Effort:** ~30 LOC + tests.

### 1.2 Semantic filter for cross-channel author pull

**Status:** designed, behind a flag.

Today the cross-channel pull is "last N messages by same author within 24h." That's a recency proxy for *relevance* and gets it wrong when an author has been chatty across unrelated topics ("Alice talked about deploys yesterday, today she's DMing about her dog — both end up in the prompt").

**Approach:** hybrid score per candidate:

```
score = α · cosine(inbound_emb, msg_emb) + (1-α) · exp(-age_h / halflife_h)
```

- Embed at ingest, not query time. fastembed `BAAI/bge-small-en-v1.5` (same model as the search index — share the load). Persisted on the `Message` and in `chat_history.jsonl`. ~1.5KB per message; 750KB worst case at 500-msg deque cap.
- Apply only to cross-channel pull. Within-channel stays recency — the agent wants conversation continuity even when off-topic.
- Outer 24h window stays as a hard candidate-pool cap (guard against unbounded scans).
- Gracefully degrades when embeddings are missing (replay from old JSONL, fastembed unavailable) — falls back to recency for that candidate.

**Config:**
- `MIMIR_RECENT_CROSS_SEMANTIC=false` — opt-in flag (off by default until shipped dark + verified)
- `MIMIR_RECENT_CROSS_SEMANTIC_WEIGHT=0.7` — α
- `MIMIR_RECENT_CROSS_RECENCY_HALFLIFE_H=12` — half-life for the recency factor

**Effort:** ~150 LOC — embeddings module, ingest hook, scoring path, config, tests.

### 1.3 Filter assistant_message kind from cross-channel pull

**Status:** small follow-on to 1.2.

The Slack DM use case wants Alice's *inbound* messages from other channels, not the bot's prior replies. Add a default `kind` filter to `cross_author_messages` (`{user_message, system_note}`).

**Effort:** ~10 LOC.

### 1.4 DM detection beyond `dm-` prefix

**Status:** noted in SPEC §5.4.

`_is_private_channel(channel_id)` only matches `channel_id.startswith("dm-")`. Real Slack DM channel IDs start with `D...`, Discord DMs are different again. Bridge layer should normalize, OR the predicate should consult a per-bridge "is_dm" hint.

**Approach:** add `is_private: bool` field to the channel registry entry. Bridges set it on registration. `_is_private_channel` consults the registry, falls back to the prefix heuristic.

**Effort:** ~30 LOC.

### 1.5 Source taxonomy expansion

**Status:** open question.

Five sources today (`slack,discord,bluesky,web,stdin`) plus `api` for programmatic injection. Add as bridges land:
- `email` — async, threaded, large bodies (per-message char cap matters more here)
- `voice` — transcribed; may want a different render template
- `cli` distinct from `stdin` (interactive vs. piped)
- `webhook` — generic inbound HTTP

No code change needed yet; add to the default allowlist (and document conventions) when each bridge is built.

---

## 2. Memory model

### 2.1 Renumbering maintenance for `memory/core/`

**Status:** noted in SPEC §16.

10-spacing on numeric prefixes (`00-`, `10-`, `20-`) gives ~10 inserts at a single position before gaps close. After that the agent has to renumber via `mv`. No tooling helps today.

**Approach:** add a maintenance scheduled job that compacts gaps when adjacent files have a gap < 2. Or expose a tool (`renumber_core(strategy="compact")`). Or document the manual recipe and rely on the agent.

**Effort:** ~50 LOC for a tool; ~10 LOC for a scheduled job.

### 2.2 Git audit / rollback layer

**Status:** deferred (SPEC §16, item 8).

Optional: wrap the agent home in a git repo and commit per turn (or per memory write). Not the concurrency story — that's already solved by namespacing + per-file flock. Useful for "show me what changed in the last 5 turns" and "roll back the last turn."

**Cost:** every memory op gains a `git add` + `git commit`. **Benefit:** free history + rollback + per-turn diffing.

**Approach:** post-write hook on `Write` / `Edit` / `Bash` that runs `git add -A && git commit -m "turn {turn_id}"` if the working tree changed. Filter out logs/, .mimir/, .claude/projects/. Optional retention policy (e.g. squash daily).

**Effort:** ~80 LOC + integration tests.

### 2.3 Chat history file growth

**Status:** noted in SPEC §16, item 9.

`messages/chat_history.jsonl` is unbounded. In-memory deques are bounded; the on-disk log isn't.

**Approach:** daily logrotate (gzip + rename) + a bounded number of historical files. Optional `MIMIR_CHAT_HISTORY_MAX_BYTES` for size-based rotation.

**Effort:** ~40 LOC.

### 2.4 Bash content writes

**Status:** documented soft spot (SPEC §16, item 10).

The prompt steers the agent toward `write_file` / `edit_file` for memory edits, but `bash` can `echo > memory/core/00-persona.md` without our flock. Last-writer-wins.

**Approach (when this becomes a real failure mode):** wrap bash with a path-aware preflight — if the command targets `memory/core/` or `memory/shared/`, run it under `flock(1)`. Cheap heuristic, not bulletproof but catches the common cases.

**Effort:** ~30 LOC.

---

## 3. Retrieval / probe-type weak spots

The bluesky_recall benchmark surfaces specific gaps in mimir's retrieval, scored 2026-04-26 at overall 0.330:

| Probe type | n | Mean | Gap |
|---|---|---|---|
| negative | 10 | 0.900 | calibrated rejection works |
| author | 23 | 0.370 | matches lettabot baseline |
| repost | 6 | 0.250 | repost author tracking weak |
| url | 15 | 0.233 | URL→handle index missing |
| topic | 17 | 0.176 | topic→handles index needed |
| reply | 8 | 0.125 | parent-handle tracking missing |
| **temporal** | **6** | **0.000** | **no temporal index** |

### 3.1 Temporal index

**Status:** dominant probe-type miss.

The agent has no native way to answer "who posted about X on day 3?" or "what did Y post on 2026-04-15?" because timestamps are stored inside content but not indexed by date. Same gap open-strix has (0.083 on temporal in its 0.330 run).

**Approach:** when storing a bluesky-style atom, also index by `YYYY-MM-DD`. Two options:
- **(a)** Tag MSAM atoms with a `metadata.date` field; add a `msam_query_by_date` skill that filters atoms with `date == X` before semantic ranking.
- **(b)** Add a `state/by-date/YYYY-MM-DD.md` file per day and have the agent file timestamped facts there during seeds. Agents reach via `read_file` once they know the date is in the question.

(b) is cheaper and matches the SPEC's "let the agent organize" philosophy. (a) is more powerful but requires MSAM schema work.

**Effort:** prompt-level guidance for (b) — minimal code change. (a) is ~200 LOC + MSAM contract update.

### 3.2 URL / domain index

**Status:** weak probe type.

"Who shared <URL>?" requires URL→handle reverse lookup. Currently the agent has to grep through prose. A dedicated URL index (file-based, `memory/by-url/<domain>.md` listing handles) would help.

**Approach:** prompt-level — instruct the agent during seeds to maintain a per-domain file listing handles seen sharing that domain. URL extraction is heuristic (regex). No code change.

**Effort:** prompt + observation pass on results.

### 3.3 Reply / repost graph

**Status:** weakest non-temporal probe types (0.125 / 0.250).

Bluesky posts have explicit reply/repost relationships (parent handle, original author). The agent isn't filing these in a way it can query. Same fix shape as 3.2: dedicated `memory/by-parent/` or `memory/replies/<handle>.md` files.

**Approach:** prompt-level. Document the convention; the agent's seed procedure adds it.

**Effort:** prompt update.

---

## 4. MSAM integration

### 4.1 Triple extraction toggle

**Status:** hardcoded ON in MSAM `server.py:251`.

Every semantic atom store fires `extract_and_store` for triples — no config flag. Fires on every seed atom (~7 LLM calls per task). Failures are silent (try/except), but the cost is real.

**Approach:** patch upstream MSAM to read `[triples].enabled` (default True), short-circuit when False. Or wrap mimir's `MsamClient.store` to send a `disable_triples=True` flag if MSAM exposes one.

**Effort:** ~10 LOC in MSAM, 1-line opt-in in mimir.

### 4.2 Session boundary visibility in retrieval

**Status:** currently default-excluded.

Per `msam-hindsight-ideas` commit `b978fe3`, session_boundary atoms are now distinct from regular atoms. Mimir writes them via `end_session` but never retrieves them — `MsamClient.query()` doesn't pass `include_session_boundaries=True`.

**Approach:** add `MIMIR_MSAM_INCLUDE_SESSION_BOUNDARIES=false` config. When True, `query()` passes the flag through. Useful when session-boundary atoms carry continuity context the agent should see between sessions.

**Effort:** ~15 LOC.

### 4.3 Auto-store cadence tuning

**Status:** noted in SPEC §16, item 3.

MSAM extracts atoms from message content automatically (no explicit `msam_store` call needed). The cadence is MSAM-controlled. If extraction is too noisy or too slow, mimir has no knob.

**Approach:** revisit if extraction proves too aggressive / too sparse in production. Possible mimir-side knobs: `MIMIR_MSAM_AUTO_STORE_THRESHOLD` (token count), `MIMIR_MSAM_AUTO_STORE_KINDS` (filter which message kinds get auto-extracted).

**Effort:** depends on what we want.

### 4.4 MSAM consolidation observability

**Status:** runs on cron, no surfacing.

`MIMIR_MSAM_CONSOLIDATE_CRON=0 4 * * 0` (Sunday 04:00 UTC) triggers consolidation. Today: fire-and-forget. The agent doesn't know consolidation ran, what merged, what survived.

**Approach:** consolidation result emits a `msam_consolidated` event to events.jsonl with cluster counts + delta. Optional: a synthesis turn after consolidation to let the agent file a summary into `memory/`.

**Effort:** ~30 LOC.

---

## 5. Subagents / fan-out

### 5.1 Mountaineering follow-through

**Status:** designed in SPEC §4.3-4.4, not yet exercised in benchmarks.

`climber.md` and the subagent inbox are wired but not tested in real workloads. The first real use case (long autonomous optimization, e.g. tuning a prompt against scored evals) will probably surface gaps.

**Approach:** find a real task; run it; iterate on the SDK background-task event handling.

### 5.2 Parallel research / verification fan-out

**Status:** wired but unused.

`researcher.md` and `critic.md` exist but the agent rarely calls them. Could add prompt-level guidance: "for ambiguous probes, fan out two researchers with different framings and merge."

**Approach:** prompt experiment. May or may not improve scores; possibly slows things down without quality lift.

**Effort:** prompt-only.

### 5.3 Per-task subagent traces

**Status:** SPEC §10.2 deferred.

Subagent invocations land as a single `tool_result` event in turns.jsonl with `name="Agent"` — the inner trace isn't preserved. For debugging a fan-out that went wrong, we want the subagent's own turn list.

**Approach:** when SDK background-task events fire, write a per-call `<home>/logs/agent-runs/<agent>-<turn_id>.jsonl` with the subagent's full event stream.

**Effort:** ~50 LOC.

---

## 6. Operational

### 6.1 Identity reconciliation

**Status:** SPEC §16, item 11.

Author keying is strict-equality on `Message.author`. When Alice's Slack ID changes (workspace migration, account replacement), continuity is lost.

**Approach:** persistent `author_aliases.yaml` mapping old-id → canonical-id. Looked up on history append + on cross-channel pull. Operator-managed initially; could be agent-suggested via a "merge these identities?" tool.

**Effort:** ~80 LOC + tooling.

### 6.2 Token-cost monitoring per turn

**Status:** not measured.

Each turn does a full Claude `query()` + N tool calls + possible subagent calls. Token cost per turn isn't surfaced. For a chatty production deployment, this is the dominant operating cost.

**Approach:** `turn_finished` event already fires; add `usage` (input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens) extracted from the SDK's `ResultMessage`. Roll up per-day in events.jsonl.

**Effort:** ~30 LOC; SDK already exposes the data.

### 6.3 Concurrency back-pressure

**Status:** SPEC §4.5 admission rules in place, never tested under load.

`MIMIR_MAX_CONCURRENT_TURNS=10`, `MIMIR_MAX_CHANNEL_QUEUE=100`, `MIMIR_WORKER_IDLE_TIMEOUT_S=60`. The path of "20 channels with traffic, semaphore at 5" is unit-tested but not load-tested. First production deployment with real concurrency is the test.

### 6.4 Web UI improvements

**Status:** turn viewer ported from open-strix; minimal additions.

- Filter by `source`
- Filter by error / `is_error=true` events
- Diff view between consecutive system prompts (helps catch silent prompt drift)
- Subagent trace inline if 5.3 lands

**Effort:** vanilla JS, ~100 LOC each.

---

## 7. Performance

### 7.1 Embedder upgrade path

**Status:** SPEC §16, item 2.

Today: fastembed `BAAI/bge-small-en-v1.5` (384-dim, local). Likely target: `text-embedding-3-large` (3072-dim, OpenAI, paid) or a stronger open model.

**Cost calculation:**
- bge-small: $0/embedding, ~5-20ms CPU
- text-embedding-3-large: ~$0.13 per 1M tokens, ~50ms latency

For benchmarks, sticking with fastembed is right (zero marginal cost). For production with a recall-quality bottleneck, the upgrade may be worth it.

**Approach:** `MIMIR_EMBED_MODEL` already configurable. The Embedder class accepts the model name; switching models requires schema work in the search index (vector dims differ).

**Effort:** ~50 LOC for the schema migration.

### 7.2 Index regeneration cost at scale

**Status:** SPEC §16, item 5.

Rebuilding `memory/INDEX.md` and `state/INDEX.md` every memory write is cheap for ~50 files; revisit at ~500.

**Approach:** debounce regeneration (rebuild at most every 5 seconds). Or maintain an in-memory index that flushes lazily.

**Effort:** ~30 LOC.

### 7.3 Seed phase speed

**Status:** observed bottleneck.

In bench runs, seed phase costs ~24 min (1410s) for 7 sessions on Minimax-M2.7. That's ~3.5 min per seed, which feels slow given the agent is "just" writing memory blocks.

**Why:** each seed turn includes the full system prompt (memory/core/ + INDEX.md), plus the 500-post seed event_body, and the agent does many tool calls (file_search to check existing memory, write_file × N, msam_store × N). Each tool round-trip is a model call.

**Approaches:**
- **Faster model for seeds.** A cheaper/faster model (Haiku 4.5? gpt-5.4-nano?) for "extract structured facts and write files" might be enough; switch back to Opus for probes. Requires SPEC-level "model per turn-trigger" config.
- **Bulk store.** `msam_store_batch(items)` to amortize HTTP overhead.
- **Skip msam_store at seed time.** Let MSAM's auto-extraction handle it; agent just writes files. Less redundancy.

**Effort:** model-per-trigger is ~80 LOC + config. Bulk store is MSAM API + client work. Skip-msam-at-seed is prompt-only.

### 7.4 Prompt cache utilization

**Status:** not measured.

Each turn rebuilds the system prompt (memory/core/ + indexes) which is ~mostly stable across turns within a task. Anthropic's 5-min prompt cache should be hitting on the stable prefix. Worth verifying via `usage.cache_read_input_tokens` in turn logs (depends on 6.2 landing).

If caching isn't hitting: silent invalidator somewhere (timestamp in system prompt, varying tool list, etc.). Audit per `shared/prompt-caching.md`.

---

## 8. Architecture / longer-term

### 8.1 Periodic reflection / memory consolidation

**Status:** speculative.

The agent's `memory/` accumulates. A periodic "review what I have, merge duplicates, prune stale" pass would reduce clutter and surface contradictions. Could be a scheduled tick that fires `Agent("memory-curator", ...)` weekly.

**Effort:** prompt + a curator subagent definition (~50 LOC). Significant prompt-engineering investment to make the curator behave well.

### 8.2 Cross-channel pull via MSAM (not deque)

**Status:** alternative to 1.2.

The semantic filter in 1.2 is a deque-local hybrid score. An alternative is to make MSAM the cross-channel retriever: at turn time, query MSAM with the inbound text + a `channel != current` filter. MSAM already has embeddings, ranking, atom-typing. Would need atoms tagged with channel.

**Pros over 1.2:** uses an existing embedding store, no parallel embedding state on the deque, MSAM's hybrid scoring (semantic + keyword + metadata) is strictly more sophisticated than ours.

**Cons:** requires every message to land in MSAM, which it doesn't today (messages → chat_history.jsonl; atoms → MSAM via auto-extraction). Auto-extraction is lossy by design.

**Decision:** revisit when 1.2 ships and we have a baseline. If 1.2 is "good enough," skip; if it's noisy, try MSAM-driven.

### 8.3 "Wisdom-keeper" reflection loop

**Status:** thematic, mimir's namesake.

Mimir is the wisdom-keeper Odin consults — implies the agent should *be the source of considered judgment*, not just a transcript-recall device. A periodic reflection loop where the agent reviews its own beliefs against new evidence and revises is the kind of behavior that justifies the name.

**Approach:** open-ended. A scheduled tick that runs with a special prompt asking the agent to review its `memory/shared/` files in light of recent traffic and revise. Distinct from 8.1 (mechanical consolidation) — this is opinion / belief revision.

### 8.4 Multi-mimir coordination

**Status:** speculative.

If two mimir instances share an MSAM (or cross-MSAM coordination existed), they could pool semantic memory while keeping per-channel isolation. The Slack-Discord cross-bridge case.

**Approach:** out of scope until there's a real second-instance use case. MSAM's `enable_sharing` flag (already in config) is the seed.

---

## 9. Documentation gaps

### 9.1 SPEC §10.2 subagent traces

Already noted as deferred — should be a proper section once 5.3 lands.

### 9.2 Bridge implementation guide

How to write a new bridge (Slack, Discord, email, etc.) — what the contract is, how `source` should be set, what `is_private` means, how `make_message` integrates.

### 9.3 Operator runbook

Common operations: "switch to a faster model for one channel," "wipe a single channel's memory," "restore from a turn-N snapshot," "rebuild MSAM from chat history." Tooling exists but isn't documented end-to-end.

---

## Maintenance

When an item from this doc lands, move it to a `## 10. Recently shipped` section (date-stamped) rather than deleting it — preserves the "why" for future archeology.
