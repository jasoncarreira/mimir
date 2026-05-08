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

### 6.1 Identity reconciliation (cross-platform context)

**Status:** designed (this doc); SPEC §16, item 11.

Today `Message.author` is a raw platform ID — Slack `U123ABC`, Discord numeric, Bluesky handle. Cross-channel pull (`MessageBuffer.cross_author_messages`) does strict equality, so Alice on Slack and Alice on Discord look like different people to the bot. A turn for Alice on Slack does NOT pull her Discord public history. The original SPEC framing was workspace migration ("Alice's Slack ID changed"); the bigger use case is cross-platform — the bot should treat one human as one human regardless of which bridge their inbound landed on.

#### Core mechanism

One operator-managed file, `<home>/state/identities.yaml`:

```yaml
people:
  - canonical: alice
    display_name: Alice Smith
    aliases:
      - slack-U123ABC
      - discord-456789
      - bsky:alice.bsky.social
      - email:alice@example.com
    notes: Eng team lead, prefers async
```

Alias prefix convention (informational — resolver treats every alias as
an opaque string, so the prefix is for human readability, not parsed):

- ``slack-<user_id>``         hyphen separator (id is alphanumeric)
- ``discord-<numeric_id>``    hyphen separator (id is numeric)
- ``bsky:<handle>``           colon — handle contains dots
- ``email:<address>``         colon — address contains @ and dots

Loaded at startup into a flat `dict[platform_id, canonical]` lookup table. A resolver function:

```python
def resolve_canonical(author: str | None) -> str | None:
    if not author: return None
    return _alias_map.get(author, author)  # falls through if no alias
```

Falls through to the raw ID when no entry exists, so the system degrades gracefully — a human who only uses Slack still gets cross-channel pull from their own Slack channels even without a YAML row.

#### Pre-requisite: platform-prefixed `event.author`

This is a small breaking change that has to land **before** identity reconciliation makes sense. Bridges today set `event.author` to a naked platform ID (`"99"` for Discord, `"U123"` for Slack). Those collide — a Slack user `99` and Discord user `99` would alias to each other accidentally.

Each bridge's inbound construction needs to prefix:

```python
# bridges/discord.py
author_id = f"discord-{message.author.id}"

# bridges/slack.py (when it lands)
author_id = f"slack-{user_id}"
```

`MessageBuffer.replay` needs to be tolerant of legacy unprefixed records in `chat_history.jsonl` — leave them alone; they won't match new prefixed queries (so they fall out of cross-pull naturally), which is acceptable for old history.

#### Where the resolver wires in

`MessageBuffer.cross_author_messages` is the main consumer:

```python
def cross_author_messages(self, *, author, exclude_channel, limit, ...):
    target_canonical = resolve_canonical(author)
    for msg in reversed(self._all):
        if msg.channel_id == exclude_channel: continue
        if _is_private_channel(msg.channel_id): continue
        if resolve_canonical(msg.author) != target_canonical: continue
        ...
```

`MessageBuffer.recent_for_channel` doesn't need it — within-channel matching is by `channel_id`, not author.

Memory file conventions can shift to canonical: `memory/people/alice.md` instead of `memory/people/<platform-id>.md`. The agent writes there using the canonical name. The auto-generated memory index already keys by filename, so the agent searching "what do I know about Alice?" finds one place.

#### Privacy: still one-directional

The DM rule applies at the source side. `cross_author_messages` filters `_is_private_channel(msg.channel_id)` regardless of canonical match. Worked example:

| Source channel | Source content | Target = `slack-eng` (public) | Target = `dm-discord-alice` (DM) |
|---|---|---|---|
| `slack-eng` | public | ✓ | ✓ (her own public msgs as DM context) |
| `discord-eng` | public | ✓ (cross-platform) | ✓ |
| `dm-slack-alice` | DM | ✗ (DM rule) | ✗ (DM rule) |
| `dm-discord-alice` | DM | ✗ (DM rule) | ✗ (DM rule) |

Identity reconciliation is orthogonal to the DM rule. Identity says "who is this person?"; DM rule says "what content is private?" Both compose.

#### Operator UX

Manual YAML editing for v1, hot-reloaded (or just reload-on-event — file is tiny). Add a CLI:

```
mimir identities add --canonical alice --alias discord-456789
mimir identities list
mimir identities remove --alias discord-456789
```

~50 LOC, optional.

**Auto-discovery is tempting but risky.** Two patterns to consider, neither in v1:

1. **Display-name observation.** Every inbound message records `(author_id, author_display)`. When the same display name shows up under two different prefixes, log an `identity_match_proposal` event. Operator reviews and adds to YAML. False-positive prone (multiple "Alice"s); low signal on its own, useful as a hint.
2. **Agent-driven proposals.** MCP tool `propose_identity_merge(slack="...", discord="...")` the agent calls when a user *tells* it ("by the way, my Discord is alice#1234"). Writes a pending entry; operator confirms. Better signal (explicit assertion) but introduces a new write path with a confirmation gate. Worth doing if cross-platform usage takes off.

Ship v1 with pure manual; revisit auto-discovery only if operators ask.

#### Subtleties

- **Display name conflict.** Alice's Slack display is "Alice Smith"; her Discord display is "alice_eng". The agent's prompt currently shows whichever the bridge passes. With identity merging, prefer the YAML's `display_name` field for consistency. ~10 LOC in `render_recent_activity`.
- **Self-identity.** The bot has IDs across platforms (different Discord bot user, different Slack bot user). The per-bridge self-skip check stays correct; if you want the agent to treat its own messages as one thing across platforms (it already does — `kind="assistant_message"` is the same), a `canonical: self` row keeps the alias map consistent.
- **Alias removal / staleness.** If Alice leaves the org and her Slack ID gets reassigned, the YAML row needs an audit. No automatic mechanism. A `last_seen` timestamp per alias would help operators prune; cheap to add.
- **MSAM session continuity is unchanged.** Sessions stay per-channel (Alice's `slack-eng` session is distinct from her `discord-eng` session, correctly — different conversations). Only the bot's *memory* of Alice (the `memory/people/alice.md` file, MSAM atoms about her) gets unified across platforms.
- **Privacy opt-out.** Some operators may want to disable cross-platform pull entirely (privacy reasons, regulatory). A `MIMIR_CROSS_PLATFORM_PULL=true|false` flag (default true) gives them the kill switch. ~5 LOC.

#### Implementation plan

Three commits, in order:

1. **Foundation** (~100 LOC): schema + YAML loader + resolver + bridge prefix change for `event.author`. `MessageBuffer.replay` tolerant of legacy unprefixed records. Tests for the loader and resolver.
2. **Cross-pull rewrite** (~30 LOC + tests): `cross_author_messages` resolves both sides through the alias map. End-to-end tests for cross-platform pull ("Alice on Slack pulls her Discord public history").
3. **CLI + display-name preference** (~80 LOC, optional): `mimir identities {add,list,remove}` and the `render_recent_activity` display-name override.

Realistic budget: **~250 LOC including tests, ~1 day of focused work.** Gated on having a real cross-platform deployment to test with — without that the feature ships dark.

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

### 7.5 Cross-encoder reranker over file_search candidates

**Status:** designed; not blocking.

`file_search` today does first-pass hybrid retrieval — weighted linear sum of normalized cosine similarity (`W_COSINE=0.5`), normalized BM25 (`W_BM25=0.2`), and recency (`W_RECENCY=0.3`) — over a candidate pool. Both signals are computed *independently of the query-document interaction*: the embedder vectorizes query and document separately, and BM25 is a statistical match. The fusion picks the right candidates well at the broad level (Engram showed `retrieval.hit_rate=0.86`), but ordering within the pool can miss "this chunk specifically *answers* the question" vs "this chunk is on the same topic."

Note: not RRF. The mimir runtime's hybrid scheme is weighted-linear; MSAM's atom-retrieval pipeline is the place that uses Reciprocal Rank Fusion, and the `rrf_keyword_weight` config lives there.

**Approach:** plug a cross-encoder reranker into `Indexer._search_sync` after the candidate pool is built. The cross-encoder processes `(query, document)` jointly so it can capture the query-document interaction the bi-encoder embeddings miss.

```python
# in _search_sync, after the candidates dict is populated
if self._reranker is not None:
    candidates = self._reranker.rerank(query, candidates)
candidates.sort(key=score)[:k]
```

`fastembed` (already a mimir dependency) ships a `Rerank` class with `bge-reranker-v2-m3`. Adds ~500MB ONNX model on top of the existing `bge-small-en-v1.5`. Local, CPU, ~10ms per query-document pair → ~200ms to rerank a top-20 pool. Same dependency surface as today; no new API key, no network call.

**When this matters:** failures of shape "right answer is in the candidate pool but ranked too low to be returned in the top-k." Doesn't help when the answer isn't in the pool at all (that's a retrieval problem, not an ordering one).

**Cost:** +200ms per `file_search` call. The agent calls `file_search` several times per turn during research-shaped work; aggregate latency on a chatty channel is meaningful. Memory: +500MB. Pi-class deployments care.

**When to revisit:** after a bench rerun shows retrieval ordering (not retrieval coverage) is the limiting factor. If the failure cluster is "answer not retrieved at all" — like the cross-agent disambiguation gap on Engram — a reranker can't help, and the leverage is elsewhere (identity reconciliation, versioning convention, etc.).

**Effort:** ~150 LOC. Needs a config flag (`MIMIR_RERANKER=true`), the fastembed `Rerank` lazy-loaded like `FastEmbedder`, a unit test that the reranker is called when configured, and an integration test verifying it actually changes ranking on a contrived case.

**Considered alternative — contextual chunk enhancement (LLM-generated context prepended at index time, à la Anthropic's contextual retrieval).** Per-chunk LLM call at indexing means a real token bill that scales with reindex frequency, plus a new runtime dependency (LLM client in the indexer). Not cost-justified for current workloads — mimir's wiki layer keeps chunks largely self-contained, and the bench's failures aren't retrieval-coverage-limited. Reranking is the cheaper path if/when retrieval becomes the bottleneck.

---

## 8. Architecture / longer-term

### 8.1 Periodic reflection / memory consolidation

**Status:** ✅ shipped in v0.4 §4. See `mimir/skills/reflection/SKILL.md` and §11 below. The "memory architecture review" track within reflection covers cleanup + promotion across `memory/core/`, `memory/<anywhere>/`, and `state/wiki/`. Atom-to-core promotion candidates come from `mimir reflection most-retrieved --contributed-only`.

### 8.2 Cross-channel pull via MSAM (not deque)

**Status:** alternative to 1.2.

The semantic filter in 1.2 is a deque-local hybrid score. An alternative is to make MSAM the cross-channel retriever: at turn time, query MSAM with the inbound text + a `channel != current` filter. MSAM already has embeddings, ranking, atom-typing. Would need atoms tagged with channel.

**Pros over 1.2:** uses an existing embedding store, no parallel embedding state on the deque, MSAM's hybrid scoring (semantic + keyword + metadata) is strictly more sophisticated than ours.

**Cons:** requires every message to land in MSAM, which it doesn't today (messages → chat_history.jsonl; atoms → MSAM via auto-extraction). Auto-extraction is lossy by design.

**Decision:** revisit when 1.2 ships and we have a baseline. If 1.2 is "good enough," skip; if it's noisy, try MSAM-driven.

### 8.3 "Wisdom-keeper" reflection loop

**Status:** partially shipped in v0.4 §4 (the reflection skill's "memory architecture review" track) — the structure for belief-revision exists; what's still open is the *belief* part (revising what mimir thinks vs. just what mimir remembers).

Mimir is the wisdom-keeper Odin consults — implies the agent should *be the source of considered judgment*, not just a transcript-recall device. The shipped reflection covers cleanup/promotion of memory; what's still future work is the explicit "review my own claims and revise where evidence contradicts them" loop. Distinct from 8.1 (which is now done as mechanical consolidation).

**Approach:** extend the reflection skill with a "claims audit" sub-pass — for `state/wiki/` pages where the agent has stated opinions, scan recent events for contradicting evidence, propose revisions via `state/proposed-changes.md`. Probably an additional sub-pass under reflection's Track B rather than a separate skill.

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

## 10. Deployment recipes

Per-installation integrations that mimir core should NOT bundle, but that
have a known shape. Add as recipes accumulate.

### 10.1 Bluesky via social-cli

**Status:** designed (this section); deliberately not bundled in mimir.

Mimir core stays platform-agnostic. For deployments that want Bluesky,
the recipe is to layer `social-cli` (https://github.com/letta-ai/social-cli)
on top of an existing mimir home rather than building a `BlueskyBridge`.
This is a better fit than the bridge model for Bluesky's batch-asynchronous
usage pattern (mention floods, async-feeling DMs, scheduled triage), and
social-cli already solves auth + retry + multi-platform (Bluesky + X) +
per-author user-context enrichment.

**Layout** (operator sets up in their agent home):

- **Working dir:** `<home>/state/social/` — social-cli runs from here.
  Holds `inbox.yaml`, `outbox.yaml`, `outbox_archive/`, `dispatch_result.yaml`,
  `processed-bsky.yaml`, `sent_ledger-bsky.yaml`, `attachments/`, plus a
  local `.env` with `ATPROTO_HANDLE` + `ATPROTO_APP_PASSWORD` and a
  `config.yaml` declaring the account. Indexed under scope `state` so the
  agent can `file_search` past inboxes.
- **User-context dir:** `state/wiki/entities/` — pass to social-cli via
  `--users-dir <home>/state/wiki/entities/`. Each notification gets its
  author's wiki entity file (if present) injected as `userContext`. Zero
  glue code: any markdown content in the file works as user context;
  social-cli reads it as raw text. **Filename exception** for Bluesky
  entities: keep the handle as-is (e.g. `alice.bsky.social.md`), not
  lowercase-hyphenated, so social-cli's lookup finds the file. Wikilinks
  become `[[alice.bsky.social]]` for those entities. Other-platform
  entities stay lowercase-hyphenated.
- **Skill:** an operator-supplied `bluesky/SKILL.md` under the agent's
  `.claude/skills/`, NOT bundled. Teaches the sync→check→decide→dispatch
  loop, the outbox YAML schema, the wiki integration ("ingest interesting
  notification authors as wiki entities"), and the failure modes from
  social-cli's `AGENT_GUIDE.md` (don't use `social-cli reply` on inbox
  notifications — only `dispatch`; use `ignore` liberally; dry-run
  complex outboxes).
- **Scheduler:** a `scheduler.yaml` entry that fires the triage every
  N minutes, dispatching onto the synthetic `scheduler:bluesky-triage`
  channel (no `channel_id` set, so it's serialized per-job).

**Install path:** clone + `pnpm install && pnpm build`, then either
`pnpm link` to put `social-cli` on `$PATH` or invoke
`node /path/to/dist/cli.js`. The skill should document whichever the
deployment chose.

**What's not covered by social-cli today:** Bluesky DMs (`chat.bsky.convo.*`
namespace). If a deployment wants real-time DM behavior, that's a small
mimir-side bridge to write — but most agent use cases (broadcast, mentions,
thread participation) work fine via social-cli's polling.

**What changes for the bench:** nothing. The benchmark adapter operates
on synthetic Bluesky data, not the real API; this recipe is for live
deployments only.

---

## Maintenance

When an item from this doc lands, move it to the `## 11. Recently shipped` section below (date-stamped) rather than deleting it — preserves the "why" for future archeology.

---

## 12. Feedback loops / VSM expansion

Complete inventory of mimir's existing feedback loops lives in
`FEEDBACK-LOOPS.md`. Five new loops + three pieces of architectural
support sit on the roadmap, prioritized by leverage-per-LOC.

### 12.1 Feedforward — `## Upcoming` prompt section

**VSM:** S4 (intelligence / "there and then") — currently thin. The
heartbeat tick is reactive ("pick from backlog"); the rate-limit
off-pace projection is the only forward-looking signal we have.

**What.** New `mimir/upcoming.py` produces a prompt block surfacing
near-term predictable events: next-N scheduled-tick / cron firings
from `scheduler.yaml`, plan-window resets (5h, 7d), recurring
weekly-reflection / saga-consolidation cadence. Renders alongside
`## Resource usage` and `## Recent feedback signals`.

**Why this loop matters.** Lets the agent prepare for predictable
load (cron-driven heartbeats, weekly reflection coming up) instead
of being surprised by them. Closes the feedforward gap Beer's S4
exists to fill.

**Out of scope first pass:** calendar / Bluesky-poll / external
event sources. Hook left for them.

**Files:** `mimir/upcoming.py` (~80 LOC), `mimir/prompts.py` (+15),
`mimir/agent.py` (+15), `tests/test_upcoming.py` (~120).

**Effort:** ~1 day.

---

### 12.2 Applied-proposals audit — close the double-loop

**VSM:** S5 (policy update). Currently the slowest loop in the
system is open: reflection drafts, operator merges, mimir never
sees whether the change worked. Single-loop only.

**What.**
- `mimir reflection mark-applied <proposal-id>` CLI subcommand:
  moves a proposal from `## Pending` to `## Applied` in
  `state/proposed-changes.md` AND appends a record to
  `state/applied-proposals.jsonl` capturing the proposal's
  `Predicted effect:` line.
- Reflection v2: each weekly run reads `applied-proposals.jsonl`,
  pulls proposals applied 1-4 weeks ago, and runs an audit pass
  that compares predicted-vs-actual against measurable signals
  in turns.jsonl + events.jsonl (error-rate delta, tool-frequency
  delta, react-polarity ratio, retrieval contribution rate).
- Output: a `## Effects of prior proposals` section in the
  weekly reflection write-up, alongside the existing
  `## Pending proposals`.

**Why this is the load-bearing one.** Without it, mimir is
permanently stuck in single-loop learning. Reflection drafts
plausible-sounding proposals; nothing measures whether merging
them helped. Closing this loop is the difference between mimir
appearing thoughtful and mimir actually getting smarter over time.

**Concrete predicted-effect signals to support first:**
- "Error rate in <area> would drop" → events.jsonl error count delta
- "Agent would invoke <skill> more often" → tool_call frequency delta
- "Positive reactions would increase / negative would decrease" → react_polarity ratio
- "Atom contribution rate for <topic> would rise" → access_log.contributed delta

Each maps to a one-line query on the existing logs.

**Files:** `mimir/skills/reflection/applied_audit.py` (~150),
`mimir/cli.py` (mark-applied subcommand, +50),
`mimir/skills/reflection/SKILL.md` (extended prompt),
tests (~100).

**Effort:** ~2 days.

---

### 12.3 Skill outcome tracking — positive feedback / amplification

**VSM:** S3 (control), with reinforcement properties. Currently
every skill seeded by `mimir setup` has equal billing in the
agent's prompt forever. `mark_contributions` is the only
amplification loop in the whole system.

**What.**
- `mimir/skill_outcomes.py` aggregates per-skill success / failure
  / abandonment rates from turns.jsonl tool-call events.
- Per-turn post-hook scans tool_call events for skill invocations
  (`mcp__mimir__skill__<name>__<entry>`), classifies outcome from
  `is_error` + retry-within-turn signal, appends to
  `state/skill-outcomes.jsonl`.
- Rolling-window aggregator (1d / 7d / 30d) feeds the prompt's
  skills listing: high-success-rate skills sort first, low-rate
  skills get a quiet ⚠ marker, never-tried skills sit in their own
  bucket so they don't crowd the proven ones.
- Operator override via `state/skill-pin.yaml` (force show / hide
  / pin-to-top).

**Why.** Adds the first real amplification loop beyond saga's atom
ranking. Skills that genuinely work get amplified; noise gets
demoted. Same shape as `mark_contributions` but at the skill layer.

**Files:** `mimir/skill_outcomes.py` (~150),
`mimir/agent.py` (post-turn hook, +20),
`mimir/prompts.py` (ordering, +15), tests (~120).

**Effort:** ~1.5 days.

---

### 12.4 S3-S4 homeostat — exploration/exploitation budget

**VSM:** the perpetual S3-vs-S4 tension that VSM literally exists
to mediate. Currently we have many S3 loops (§1.1, §2.4, §4.3-§4.6
in FEEDBACK-LOOPS.md) and one S4 loop (heartbeat). No arbiter.

**What.** The arbiter consults a layered hierarchy of constraints
(strictest first) instead of just one metric:

1. **Plan-window utilization** (hardest constraint). From
   `mimir/rate_limits.py`'s `RateLimitEvent` capture — 5h /
   7d / 7d_opus / 7d_sonnet / overage windows. If any window
   is at 80%+ utilization, suppress ALL S4 firings; the model
   will literally stop responding when the window saturates,
   so spending S4 budget there is wasted. Above 95% even S3
   gets a soft denial (agent gets `## Plan windows` warning
   in prompt urging it to scale back).
2. **Cost-rate alert** (dollar constraint). From
   `mimir/usage_stats.py` — current $/hr vs
   `MIMIR_COST_HOURLY_LIMIT_USD`. When tripped, S4 suppressed
   first; S3 kept running with a `cost_rate_alert` event
   surfaced in the algedonic block (§2.1).
3. **Tool-call budget** (soft heuristic). Per-day count split
   80/20 between S3 (user-message-driven) and S4 (heartbeat /
   scheduled-tick-driven). When S3 is dominating, heartbeat
   firings deferred; when S3 idle, heartbeat boosts.
4. **Token-count budget** (rolling). 24h / 7d token totals
   from turns.jsonl `usage` blocks. Catches cases where
   tool-call count looks fine but each call is expensive
   (large-context queries on long sessions).

The order matters: plan-window is a hard wall (Anthropic just
stops responding); cost-rate is dollars; tool-call + tokens are
soft heuristics for catching trouble before a hard wall hits.
Constraint #1 saturating overrides all the others.

**Implementation.** `mimir/budget.py`:
- Reads RateLimitInfo (latest rate_limits.py snapshot), recent
  usage_stats aggregates, scheduler state.
- Exposes `should_fire_heartbeat()` returning suppress / fire /
  boost. Scheduler hits this before each heartbeat dispatch.
- Exposes `render_self_state_block()` for the prompt — surfaces
  whichever constraint is closest to its limit so the agent
  has the same signal as the arbiter:
  ```
  ## Self-state
  - 7d_opus window: 68% used (resets in 3d 4h)
  - cost rate: $1.20/hr (limit $5/hr)
  - S3/S4 budget today: 67% / 12% (cap 80/20)
  - tokens 24h: 2.1M (~$8 at current model)
  ```

**Why.** Without an explicit arbiter, organizations under load
default to pure firefighting and starve foresight. The same
failure mode applies to mimir: if user_message turns dominate
the day's budget, heartbeat work never lands. **And without the
plan-window dimension specifically, S4 work happily burns through
the 7d_opus quota leaving the agent unable to respond to user
messages by Friday.** The hardest constraint must drive the
arbiter; tool-call count alone is too soft.

**Subtle tuning:**
- The danger is making the agent dormant during busy days. 80/20
  default + monotonic decay (S4 can borrow from S3 surplus, not
  vice-versa during peak load) is the soft-budget starting point.
- For plan-window, suppress threshold defaults to 80% (5/7d) and
  on-pace projection >100%. The on-pace logic is already in
  rate_limits.py — `rate_limit_off_pace` event fires when the
  current rate projects to overflow by reset.
- Cost-rate suppression should be cooldown-gated (already
  exists in usage_stats — `MIMIR_COST_ALERT_COOLDOWN_MINUTES`)
  so it doesn't oscillate.

**Files:** `mimir/budget.py` (~250),
`mimir/scheduler.py` (heartbeat suppression / boost, +60),
`mimir/prompts.py` (`## Self-state` block, +20),
`mimir/agent.py` (turn-boundary updates, +10),
tests (~180).

**Effort:** ~2.5 days. The plan-window + cost-rate plumbing
already exists (v0.4); §12.4 is the integration layer that turns
those signals into S4 dispatch decisions. Tuning probably an
additional week of observation before the defaults stabilize.

---

### 12.5 Subagent recursion — requisite variety on S1 units

**VSM:** Beer's "law of requisite variety" — each level must have
enough internal complexity to regulate the level below. Subagents
(climber, researcher, critic) are mimir's S1 units. Today they
have personas (S5) and tasks (S1) and nothing else. No S2
(anti-oscillation between iterations), no S3 (per-subagent budget
enforcement), no S4 (subagent-specific foresight).

**What.** Subagent definition frontmatter gains a `vsm:` block:

```yaml
---
name: climber
description: ...
vsm:
  s3_tool_budget: 20            # max tool calls per invocation
  s2_anti_oscillation:
    iteration_cap: 5
    duplicate_change_window: 3  # don't make same change in last N
  s4_foresight: false           # subagent-specific scanning
---
```

- `mimir/subagent_defs.py` parses; defaults preserve current behavior.
- `mimir/subagent_runtime.py` enforces budget via PreToolUse hook
  scoped to the subagent's session. Tracks recent edits for
  anti-oscillation; the climber's iteration is the immediate
  beneficiary (mountaineering gets "I just did this same change
  two iterations ago, skip").

**Why.** Climber is the most expensive S1 we run (mountaineering
iterations call multiple LLMs, run code, edit files). Letting it
run unbounded by S2/S3 is the kind of place runaway costs and
oscillation get born. Adding regulation now is cheap; adding it
after a runaway incident is expensive.

**Files:** `mimir/subagent_defs.py` (frontmatter parser, +80),
`mimir/subagent_runtime.py` (~150),
`mimir/.claude/agents/*.md` (extend frontmatter on climber +
researcher + critic), tests (~120).

**Effort:** ~2 days.

---

### 12.6 Architectural support — VSM as a first-class concept

Three small pieces that make adding new loops easier going forward.

**a. Per-loop VSM-layer tags in code.** Convention: every loop's
entry point gets a `# VSM: S<N>` comment. `FEEDBACK-LOOPS.md`
becomes auto-generatable from `grep "# VSM:" -r`. Effort: tag
existing 16 loops + a lint check, ~1 day.

**b. `mimir loops` CLI subcommand.** Reads recent events.jsonl +
turns.jsonl, prints a table:

```
Layer       Loop                       Last fired  Volume (24h)  Status
S1          tool-result                7s ago      342           healthy
S3          mark_contributions         12s ago      58           healthy
S3*         reflection                  4d ago       1           healthy
S4          heartbeat                  18m ago      42           healthy
algedonic   inbound reactions          never        0           never-fired
```

The never-fired rows are the diagnostic: if inbound-reactions never
fires for a week, either nobody's reacting or the bridge wiring is
broken. Effort: ~120 LOC + ~80 of test scaffolding, ~1 day.

**c. Loop-test pattern.** Every PR adding a feedback loop should
include: (a) what triggers it, (b) what it changes, (c) how to
verify it fires, (d) the time horizon. Codified as a checklist in
`.github/PULL_REQUEST_TEMPLATE.md` and a fixture pattern in
`tests/test_loops_smoke.py` that asserts each documented loop's
events fire under a synthetic scenario. Effort: ~0.5 day.

---

### 12.x Out of scope (for now)

- **Cross-S1 anti-oscillation across channels.** No symptoms yet;
  build when we see them.
- **Delayed-feedback oscillation diagnostic** — a tool that
  grep-scans turns.jsonl for behavior cycles. Build on demand.
- **Closed-loop S5 policy update.** Letting mimir edit
  `memory/core/identity.md` directly is a real boundary. Per
  `FEEDBACK-LOOPS.md` §7's note, kept human-anchored by design
  until §12.2 (applied-proposals audit) proves the agent can be
  trusted to know which corrections actually worked.

**Suggested order to build in:**

1. §12.6a (VSM tags) — 1 day, prereq for clean reasoning across
   the rest
2. §12.1 (Upcoming section) — 1 day, low-risk feedforward
3. §12.6b (`mimir loops` CLI) — 1 day, makes #4 verifiable
4. §12.3 (skill outcomes) — 1.5 days, first real amplification
5. §12.5 (subagent recursion) — 2 days, before climber gets used at scale
6. §12.4 (S3-S4 homeostat) — 2.5 days, real arbiter; integrates
   plan-window + cost-rate (already plumbed) + tool-call + token
   counts as a layered hierarchy of constraints, hardest-first
7. §12.2 (applied-proposals audit) — 2 days, the load-bearing
   double-loop closure; do last so the metrics it audits already
   exist

Total: ~10.5 days of focused work for the whole §12 stack.

---

## 11. Recently shipped

### 2026-05-01 — v0.5 saga merge + in-process integration

Closed the cross-repo coordination tax between mimir and msam. See
`V0.5.md` for the full plan and commit refs (`b7e368b .. 45837d0`):

- **§1 Workspace monorepo merge** — `msam2` (hindsight-ideas branch)
  merged into mimir at `saga/` via `git filter-repo` with full history
  preservation. uv workspace setup in root `pyproject.toml`. saga stays
  standalone-runnable (no mimir dep); cross-package integration benches
  live OUTSIDE saga at `benchmarks/longmemeval_via_mimir/`.
- **§6 Rename msam → saga** (commits `7568594`, `fb48295`) — Norse
  poet-goddess of history. Pairs with mimir at the pantheon level;
  "saga" already carries the English meaning "ongoing chronicle" so
  the name self-explains. Single-grep rename across imports, tests,
  config keys, env vars (`MSAM_*` → `SAGA_*`).
- **§7 Unified LLM transport** (commit `45837d0`) — saga's 9
  chat-completion call sites route through `saga._llm.call_llm_sync`
  which dispatches on `[<section>].provider`: `anthropic` (default,
  uses `anthropic.Anthropic` Messages API) or `openai_compat` (default
  for the bench harness, preserves the `requests.post` path against
  saga_p30_canon_v4 baseline 0.774).
- **§2 In-process saga adapter** (commit `3346952`) — `mimir/saga_client.py`
  becomes a Protocol with `_InProcessSaga` (default; saga.core via
  asyncio.to_thread) and `_HttpSaga` (external-saga deployments). HTTP
  hop eliminated for the same-host case. `mimir setup` writes
  `<home>/saga.toml` with v0.5 defaults (contextual_rewrite on, triples
  extraction on, augment_v2 off). Separate SQLite files (saga.db +
  index.db) in shared `<home>/.mimir/` directory — independent lock
  granularity for consolidation vs. per-turn reindexes.
- **§3 Integration bench harness** — `benchmarks/longmemeval_via_mimir/`
  drives LongMemEval through mimir's `BenchBridge` so cache /
  contextual_rewrite / mark_contributions / session_boundary effects
  become end-to-end measurable. Hypotheses JSONL feeds saga's existing
  gpt-4o judge; numbers stay comparable to the saga-direct baseline.

Also retired:
- `mimir/saga_client.py` HTTP retry block (still present for `_HttpSaga`)
  is no longer the default codepath. Production hits saga directly.
- The "muninnbot pulls msam from local source" path — muninnbot still
  exists per the operator notes, but mimir's saga story is now
  self-contained.

### 2026-05-01 — v0.4 self-awareness loops

Closed several long-standing items. See `V0.4.md` for the full plan and commit refs (`bf7e3a4 .. 708647b`):

- **§8.1 Periodic reflection / memory consolidation** → reflection skill + weekly cron entry. Two parallel tracks (behavioral + memory-architecture-review). Propose-only by default per `memory/core/30-reflection-policy.md`. Bundled `mimir reflection most-retrieved` CLI for atom-to-core promotion candidates.
- **§8.3 Wisdom-keeper reflection loop** → partially. Memory-architecture-review track ships; "claims audit" extension is still future work (re-scoped above).
- **§6.1 Identity reconciliation** (commits `b6a8f9b`, `c043ff3`, `291e754`) — resolver primitive, bridge prefix + cross-pull, identity records surfaced in turn prompt.
- **`state/social/` indexer skip** — `search.py` now short-circuits `state/heartbeat-backlog.md`, `state/proposed-changes.md`, `state/identities.yaml`, and the `state/social/` prefix via `INDEX_SKIP_PATHS` / `INDEX_SKIP_PREFIXES`. Resolves the "social-cli artifacts shouldn't be embedded" tail of §10.1.

Also shipped (not previously tracked here, captured for posterity):
- Heartbeat tick (V0.4 §1) — autonomous-work cadence with librarian protocol + backlog. Foundation for the rest of v0.4.
- Algedonic surfacing (V0.4 §2) — recent error/feedback signals in the turn prompt, both polarities. New `feedback.py` reads `events.jsonl` + `turns.jsonl` tail-stream.
- Session boundary surfacing (V0.4 §3) — `## Recent session summaries` block in turn prompt; MSAM source-of-truth with local-mirror fallback at `<home>/.mimir/session_boundaries.jsonl`.
- Mountaineering port (V0.4 §5) — five framework files verbatim from open-strix; SKILL.md adapted for the climber subagent.
- Operator alert channel (V0.4 §6) — `MIMIR_OPERATOR_ALERT_CHANNEL` + alert skill teaching when/how to escalate.
- Indexer exclusion list (V0.4 §7) — see §6.1 above.
- API key auth on POST /event (post-v0.4 review) — `MIMIR_API_KEY`; without it the server's 0.0.0.0 bind exposed an arbitrary-trigger injection surface.
- JSONL log caps + tail-streaming — `MIMIR_MAX_TURNS` default 5000 (hard ceiling 50000), `MIMIR_MAX_EVENTS` default 75000 (hard ceiling 750000, 15× turns to match observed events/turn rate), hysteresis trim, tail-streamed reads via `mimir/_jsonl_tail.py`.

---

## Filed specs (separate documents)

- **[CLAUDE_SDK_CLIENT_MIGRATION.md](./CLAUDE_SDK_CLIENT_MIGRATION.md)** —
  staged plan to replace `claude_agent_sdk.query()` with the long-lived
  `ClaudeSDKClient` in the agent loop. Unlocks `get_context_usage()` (Max
  plan window utilization), `interrupt()`, `set_permission_mode()`,
  `set_model()`, and lets us retire the cron-based quota poller. Filed
  2026-05-04 alongside the cron poller in `mimir/quota_poller.py` —
  the cron is the ship-this-week fix; the migration is the long-term
  shape. ~one focused day of work across five stages.

- **[SYNTHESIS_AND_BUDGET_FIXES.md](./SYNTHESIS_AND_BUDGET_FIXES.md)** —
  two cost/autonomy fixes from the 2026-05-04 self-reflection cycle.
  (1) Synthesis turn embeds full turn JSON including each turn's
  `input` (a 30k-token rendered prompt that already contains prior
  turns' context) — quadratic blowup, $2-3 per session-end at 500k
  prompt tokens. Fix: pass turn IDs + atom-feedback structure;
  add `mimir_get_turn(turn_id)` MCP tool for selective fetch during
  memory capture. (2) `MIMIR_TOOL_CALL_BUDGET` default of 30 is too
  low for an autonomous engineer; bump to 120. Two independent PRs.
