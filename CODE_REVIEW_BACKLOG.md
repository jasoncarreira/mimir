# Mimir + Saga code review — prioritized backlog

<!-- desc: 21-item backlog from a 2026-05-05 review pass over both codebases -->

**Source:** code review of `mimir/` and `saga/saga/` on 2026-05-05, deduped against
existing `FUTURE_WORK.md`, the filed migration specs, and merged PRs 1-16.

Items grouped by category. Each carries severity (high/med/low), effort (S = <100 LOC,
M = 100-300, L = 300+), and a concrete approach so an autonomous engineer can pick it up
without further design questions. Items flagged "design decision" name the recommended
choice rather than leaving it open.

---

## Bugs / correctness

### 1. Saga in-process embedding provider singleton has TOCTOU init race

- **Where:** `saga/saga/embeddings.py:244-282`
- **What's wrong:** `get_provider()` does `if _provider_instance is None: ... _provider_instance = provider_cls()` with no lock. Saga's `_InProcessSaga` calls now happen in `asyncio.to_thread`, so two concurrent mimir turns can race — both see None, both construct an `ONNXProvider` (which downloads/loads a 33MB ONNX model), the loser's instance leaks. Worse, the LRU `cached_embed_query` is module-global; the lost provider's first calls warm the cache against a concurrent re-init. Saga also runs vector-index updates on a different thread (`vector_index._atoms_index_lock`), and the `_provider_instance` global is unsynced across all of them.
- **Why it matters:** Race window is small but real on the first concurrent two-turn dispatch after process start. Most visible symptom is doubled fastembed cold-start latency (each branch takes the ~3s ONNX load) and a one-shot memory blip.
- **Approach:** Wrap with a `threading.Lock` (mirroring `_migrations_lock` in `core.py:138`) double-checked-lock. ~10 LOC.
- **Effort:** S
- **Severity:** sev:med

### 2. `Agent._assemble_usage_block` creates fire-and-forget tasks via `asyncio.create_task` that can be GC'd

- **Where:** `mimir/agent.py:1019-1031`, `1054-1063`, `1389-1396`
- **What's wrong:** Three `asyncio.create_task(log_event(...))` sites with no reference retained. CPython's docs explicitly warn that "the result of `create_task()` may be garbage collected before it has run" — short events.jsonl writes usually finish before GC runs, but on a busy event loop with many concurrent turns the task object can be collected mid-write, silently dropping the event. Also: `log_event` itself acquires an `asyncio.Lock`, so dropping a task between `create_task` and the actual write window means the event vanishes with no error trail.
- **Why it matters:** Lost `cost_rate_alert` / `rate_limit_off_pace` / `saga_synthesis_empty_window` events under load — exactly when the agent most needs to surface them. The algedonic block (`feedback.py`) reads events.jsonl and won't see what wasn't written.
- **Approach:** Either `await` (these sites already run in async context) or push the task handle into a small bounded `set[asyncio.Task]` on the Agent instance and discard via `task.add_done_callback(set.discard)` (the standard idiom). The lock-acquisition concern means `await` is fine — these aren't hot paths. ~15 LOC.
- **Effort:** S
- **Severity:** sev:med

### 3. `ChannelSession.idle_handle` cancels by `TimerHandle` after replacement, leaving zombie tasks

- **Where:** `mimir/session_manager.py:101-104`, `157-162`
- **What's wrong:** `_schedule_idle` uses `loop.call_later(N, lambda: asyncio.create_task(self._fire_idle(...)))`. When `touch()` cancels the timer at `idle_handle.cancel()`, that only cancels the lambda invocation. If the timer already fired and the lambda already created the `_fire_idle` task, the cancel is a no-op — and `_fire_idle` then runs against a stale `(saga_session_id, channel_id)` snapshot. The guard inside `_fire_idle` (`session.saga_session_id != saga_session_id`) catches the common case, but if multiple rapid-fire `touch()` calls happen on the same channel, the lambda-vs-task gap means several `_fire_idle` tasks can be in flight at once.
- **Why it matters:** Edge case is fine in test: the second `_fire_idle` sees the new session_id and bails. But under bursty churn (a chatty channel hitting `touch()` 10×/sec while the timer is at the cusp of firing) you get a small fan-out of orphaned `_fire_idle` tasks; if any survive past their bail-check window — or if a future change moves any logic before that check — synthesis fires twice. Today's code is correct but fragile.
- **Approach:** Track the inner task (not the TimerHandle) in `idle_handle`; cancel that on `touch()`. Or restructure so the timer just sets a flag and a single long-running consumer task handles all idle-fire dispatches. ~30 LOC.
- **Effort:** S
- **Severity:** sev:low

### 4. `_filter_session_turns` reads the entire turns.jsonl synchronously inside the event loop

- **Where:** `mimir/agent.py:108-127`, called from `_build_synthesis_prompt:1387` which runs inside the event loop
- **What's wrong:** Function does `with turns_path.open("r")` and iterates the whole file, blocking the event loop. With `MIMIR_MAX_TURNS=1000` (default) the file is bounded, but each line can be up to ~50KB after the synthesis fix (event lists can be large). At 50MB worst case this blocks every other dispatcher worker for 100-500ms. The synthesis turn happens in a worker context that holds the global semaphore, but the read still blocks the event loop, which means typing-indicator refresh tasks, OAuth poller cron, scheduled-tick dispatch all stall.
- **Why it matters:** Visible in production as periodic event-loop stalls timed to session-end synthesis. Workers across all channels feel it.
- **Approach:** Wrap the body in `asyncio.to_thread(...)` (same pattern as `scheduler.list_jobs:341`). The function already has no await — just one wrap. Or better: use `tail_jsonl_records` from `_jsonl_tail.py` and stop once you've collected all matches (keeps memory bounded too). ~10 LOC.
- **Effort:** S
- **Severity:** sev:med

### 5. `_partition_turns` and `aggregate_usage` read entire turns.jsonl synchronously every turn

- **Status:** ✅ Landed 2026-05-06. `_partition_turns` rewritten to use `tail_jsonl_records` with early break on `ts < cutoff_7d` (eliminates the `path.read_text()` whole-file slurp). The three async call sites — `_assemble_usage_block`, `_assemble_self_state_block`, and `should_fire_heartbeat` — now wrap the snapshot path in `asyncio.to_thread` so the JSONL scan no longer blocks the event loop. New regression test `test_partition_early_break_on_7d_cutoff` exercises the early-break invariant. 60s mtime-keyed TTL cache deferred to CR#10 (per-Agent JsonlSnapshot) since this fix already covers the memory-spike + event-loop-block concerns.
- **Where:** `mimir/budget.py:322` (`text = path.read_text(...)`); `mimir/usage_stats.py:202` (uses `tail_jsonl_records`, but iterates until oldest cutoff — typically the whole file at 7d window)
- **What's wrong:** Both are called from `_assemble_usage_block` and `_assemble_self_state_block` per turn (sync reads inside `run_turn`). `aggregate_usage` does use `tail_jsonl_records` (good — chunked) but iterates 1+5+168 hour windows; the 7d window forces walking the entire 1000-record file every turn. `_partition_turns` is worse — `path.read_text(encoding="utf-8")` reads the whole 50MB at once, plus `splitlines()` and `json.loads()` per line. Both are sync inside the event loop.
- **Why it matters:** Per-turn cost of ~50-200ms parsing turns.jsonl on the event loop. This compounds with #4. At scale this is the dominant per-turn fixed cost outside the LLM call.
- **Approach:** `_partition_turns` should use `tail_jsonl_records` and stop when `ts < cutoff_7d`. Both should be wrapped in `asyncio.to_thread`. Cache the result with a 60s TTL keyed off file mtime — these don't need per-turn freshness. ~50 LOC.
- **Effort:** S
- **Severity:** sev:med

### 6. Saga's `_PersistentClaudeCode` thread-local cache has unbounded thread-leak when used from worker tasks

- **Where:** `saga/saga/_llm.py:192-217`, `220-346`
- **What's wrong:** `_persistent_runner_local = threading.local()` plus `_PersistentClaudeCode.__init__` spawns a daemon thread per calling thread. Saga is called from `_InProcessSaga` via `asyncio.to_thread` — Python's default executor reuses threads, so this is bounded in steady state (good). But `asyncio.to_thread` uses a `concurrent.futures.ThreadPoolExecutor` whose default `max_workers` is `min(32, os.cpu_count() + 4)` — so on a typical 8-core box you can end up with up to 12 daemon threads each holding a Claude Code subprocess (~50MB RAM × 12 = 600MB) once saga consolidation hits diverse threads. Worse, there's no shutdown — the threads run `loop.run_forever()` and only exit on process termination.
- **Why it matters:** Slow RSS growth on long-lived mimir processes when consolidation cron fires across thread pool churn. The daemon-thread design means leaks aren't visible in `ps`, just memory. For a Pi-class deployment running mimir 24/7 this is real. Additionally, serializing through `_submit_lock` would pin total saga-LLM throughput at "one channel at a time" — a real regression for multi-channel operation since the dispatcher runs per-channel queues with cross-channel parallelism (Discord + Slack turns can both hit `_pre_message_hook` → atom extraction simultaneously).
- **Approach (revised 2026-05-06 — see `memory/shared/cr6-pool-refactor-design.md`):** Replace `threading.local` with a **bounded persistent-client pool, decoupled from worker-thread identity**. The threading.local design is a degenerate unbounded pool keyed on the calling thread; the fix is an explicit bounded pool with FIFO checkout.
  - Semaphore-guarded queue of N persistent-client instances, lazily created up to the cap.
  - `call_llm_sync` checks one out, makes the call, returns it.
  - Worker thread churn doesn't create new instances — workers borrow from the pool.
  - Cross-channel parallelism preserved up to N concurrent calls.
  - Subprocess count bounded at N (default 4: covers 2 chat channels + 1 cron with headroom). Configurable via env.
  - **Implementation opportunity:** mimir already has a real pool primitive at `agent.py:238` (`ClientPool`). Extract the asyncio-condition-guarded idle/in-flight tracking into a shared `mimir.client_pool` module; mimir keeps its fingerprint-keyed flavor (drain-on-flip), saga gets a single-fingerprint flavor (recycle-after-N-calls). Two policies, shared primitive. Saga's pool wrapper still owns its own daemon-thread + asyncio loop for the sync-bridge contract; this PR does NOT remove the bridge — that's chainlink #20 (step 2: async-native `call_llm`).
  - ~80-150 LOC plus tests for concurrent checkout / leak fix.
- **Effort:** M
- **Severity:** sev:med

### 7. `oauth_usage_poller` writes credentials via `tmp.write_text` + `os.replace` without `fsync`, can lose newly-rotated refresh token on crash

- **Where:** `mimir/oauth_usage_poller.py:135-158`
- **What's wrong:** `write_credentials` does `tmp.write_text(...)` then `os.replace(tmp, path)`. The temp write isn't fsync'd before the rename; on a kernel panic / power loss between rename and writeback, the new file's metadata commits but its contents are zeroes. Anthropic's OAuth rotates the refresh token on every refresh, so a lost write here means the token in memory works until the next refresh, then `invalid_grant` — the agent surfaces "OAuth logged out" and operator must re-`/login`. The credentials file is the single point of identity for the entire deployment.
- **Why it matters:** Rare but operator-painful failure. A mimir restart (crash, OOM) within the millisecond-window between rename-commit and tmp-content-flush leaves the credentials file as a pile of zeroes, requiring manual operator intervention.
- **Approach:** Use the standard atomic-replace pattern: open tmp, write, `os.fsync(tmp.fileno())`, close, `os.replace`. Additionally, fsync the parent dir after the replace to ensure the rename is durable. Same fix needed in the sidecar at line 220-228. ~15 LOC.
- **Effort:** S
- **Severity:** sev:med

### 8. `record_first_seen` re-initializes `first_login_at_unix` to `now()` on a corrupt sidecar — silently resets the age warning

- **Where:** `mimir/oauth_usage_poller.py:181-231`
- **What's wrong:** Lines 213-215: `first_login_at = existing.get("first_login_at_unix")` then `if not isinstance(first_login_at, (int, float)): first_login_at = now`. The `existing` dict is reset to `{}` on `JSONDecodeError` — meaning a transient I/O error during sidecar write (which is more likely than the credentials file given there's no atomic-write here either) silently resets the "first login" timestamp. The age-warn that was supposed to fire at 25 days now restarts its 25-day countdown.
- **Why it matters:** The age-warn never fires for an operator whose sidecar gets corrupted, leading them to think their refresh token is fresh until it actually expires (Anthropic's TTL is unpublished but observed at 30 days). Then `oauth_logged_out` fires with no prior warning, mid-task.
- **Approach:** Treat sidecar corruption as a hard error — log + skip the warning rather than silently resetting. Or: if `first_login_at` can't be read, derive a fallback from the credentials file's mtime. ~10 LOC.
- **Effort:** S
- **Severity:** sev:med

### 9. `_HttpSaga` keeps a single `aiohttp.ClientSession` but `_ensure_session` doesn't lock — concurrent first calls leak sessions

- **Where:** `mimir/saga_client.py:530-541`
- **What's wrong:** `_ensure_session` checks `if self._session is None or self._session.closed` then constructs. Two concurrent turns hitting saga simultaneously on first request both see None, both construct `ClientSession`, the loser's session leaks. `_HttpSaga` is rarely used (default is `_InProcessSaga`) so this is mostly a multi-deployment edge case, but it'll show up as "I see a connection-pool warning" with no actionable trace.
- **Why it matters:** Mostly moot since in-process is the default, but for shared-saga deployments (mentioned in v0.5 §1) this leaks a small amount of fd state and produces aiohttp deprecation warnings in production logs.
- **Approach:** Add an `asyncio.Lock` and double-check inside it. Or: lazily construct in `__init__` (the session construction itself is cheap and only fails on event-loop mismatch). ~10 LOC.
- **Effort:** S
- **Severity:** sev:low

---

## Performance / cost

### 10. Per-turn synchronous reads of events.jsonl + turns.jsonl in 5+ places, no caching

- **Where:** `mimir/agent.py:_assemble_usage_block` (events.jsonl × 2 via `event_recently_emitted`), `_assemble_self_state_block` (events.jsonl + turns.jsonl), `_assemble_session_summaries`, `FeedbackLog.recent` (both files), `aggregate_subagents` (events.jsonl), `_partition_turns` (turns.jsonl) — all called per turn, all read tail-first but uncached
- **What's wrong:** A single turn calls into events.jsonl tail-streaming **at least** 4 times (cost-rate cooldown check, off-pace cooldown check, feedback.recent, subagent aggregate, pending-forget check) and turns.jsonl 3 times (usage aggregate, partition, session synthesis filter). Each spawns its own seek+read pattern. With WAL'd writers happening concurrently, this becomes a real I/O storm at high turn rates.
- **Why it matters:** ~50-200ms per turn just on JSONL parsing. On a Max plan that's a meaningful fraction of cycle time when turns themselves are 1-3s. A cold-cache linear scan of 50MB events.jsonl is ~500ms of I/O.
- **Approach (design decision):** Add a per-Agent cache layer: `JsonlSnapshot(path, ttl_s=10)` that re-reads at most every TTL or on mtime change. Wire all six call sites through. Optionally: have the loggers themselves push parsed records onto an in-memory deque on write, with a snapshotting `recent(n)` method — eliminates the read entirely for the common case. **Recommended choice: start with mtime-checked TTL cache;** the deque-on-write approach is a bigger refactor. ~150 LOC.
- **Effort:** M
- **Severity:** sev:med

### 11. Saga `get_db()` opens a fresh SQLite connection on every call; FAISS path additionally reads atom rows after a separate index search

- **Where:** `saga/saga/core.py:141-163`; called from 49+ sites including hot retrieval path
- **What's wrong:** Every saga function does `conn = get_db()` → `conn.close()`. Each open re-runs `executescript(SCHEMA_SQL)` (which is a dozen `CREATE TABLE IF NOT EXISTS` statements) and `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=...`. SQLite's IF NOT EXISTS is fast but not free — at the typical mimir turn cadence (1 retrieval + N feedback + 1 store + cluster ops) this is 5-10 schema-rebuilds per turn. Plus, with multiple worker threads each opening their own conn, SQLite WAL syncs more aggressively.
- **Why it matters:** Probably 20-100ms per turn lost to SQLite handshake + schema reaffirmation. Not catastrophic but easy money.
- **Approach (design decision):** Connection pool keyed by thread (saga.core is sync — `threading.local` works correctly here). `get_db()` returns the cached one; close becomes a no-op or release-to-pool. Schema apply runs once per process via `_migrations_done`. ~80 LOC. **Open question:** does saga rely on close() committing implicitly? Audit shows explicit `conn.commit()` in all write paths, so this is safe.
- **Effort:** M
- **Severity:** sev:med

### 12. `_search_sync` re-embeds the query inside the worker thread on every search call; no LRU cache

- **Where:** `mimir/search.py:344-350`
- **What's wrong:** `Indexer.search(query, ...)` always calls `self._embedder.embed([query])` — no cache. The bench harness and benchmarks routinely call file_search with the same query 5-10× per turn (semantic + keyword + variations). Saga has `cached_embed_query` (LRU 64) for the same reason. Mimir's search doesn't.
- **Why it matters:** ~10-50ms per duplicate `file_search`. Each saved cache hit is real cost on a 200-turn benchmark.
- **Approach:** Add `@lru_cache(maxsize=64)` on a private `_embed_query_cached(text: str) -> tuple[float, ...]` method, mirroring saga's pattern. ~15 LOC. Or shared cache between mimir's Embedder and saga's — they often use the same model (bge-small) and would share embedding cache hits.
- **Effort:** S
- **Severity:** sev:low

### 13. `extract_turn_events` walks `messages` 3× (once for tool_indices precompute, once for emit, once for ResultMessage scan happens elsewhere)

- **Where:** `mimir/turn_logger.py:107-181` and `agent.py:1602-1758` (multiple sequential passes over `messages`)
- **What's wrong:** `agent.run_turn` collects `messages: list = []` then walks it 4 times: (a) `extract_turn_events` (which itself walks twice when streaming_active), (b) ResultMessage scan, (c) RateLimitEvent + StreamEvent capture loop, (d) TaskStarted/Progress/Notification scan. Each turn's message list can be 100-500 messages including streaming chunks. Aggregate cost is small per turn but unnecessary.
- **Why it matters:** Cumulative ~10-50ms per turn, mostly noise but compounds with #5 + #10.
- **Approach:** Single pass through `messages` after the streaming loop ends, dispatching by type. ~40 LOC consolidation.
- **Effort:** S
- **Severity:** sev:low

---

## Architecture / leaky abstractions

### 14. Saga's response-shape mirroring in `_InProcessSaga._sync_query` duplicates `saga/saga/server.py::api_query` logic

- **Where:** `mimir/saga_client.py:225-279` mirrors `saga/saga/server.py:332-541`
- **What's wrong:** Two implementations of the confidence-gating and tier-filtering logic exist — one in saga's FastAPI handler, one duplicated in mimir's `_InProcessSaga`. The comment at saga_client.py:160-164 explicitly acknowledges this and says "If they drift, the integration bench (v0.5 §3) catches it" — but the integration bench is a 500-question batch, so drift detection takes a full run. Worse, the two-tier path is ~50 lines of nuanced logic (per-atom confidence floors, gating reasons) that's almost certainly already drifted from server.py given how much the server has changed since v0.5 §2 landed.
- **Why it matters:** Slow-burning correctness bug. Mimir's in-process saga returns subtly different rankings/gating than HTTP saga, but the difference is invisible until someone runs the bench and notices a regression.
- **Approach (design decision):** Extract the shared logic into `saga/saga/api.py` as a pure function — `apply_confidence_gating(observations, raws, floor, gating_enabled) -> (obs, raws, gated_reason)`. Both `server.py::api_query` and `saga_client.py::_sync_query` call it. ~80 LOC refactor; net code reduction. **Make `apply_confidence_gating` and triple-augmentation pure helpers; don't change the wire shapes.**
- **Effort:** M
- **Severity:** sev:med

### 15. `agent.py` is 1871 lines; `Agent.run_turn` is 460 lines doing 11 distinct phases

- **Where:** `mimir/agent.py:1407-1871`
- **What's wrong:** `Agent.run_turn` is a single linear function with 11 numbered comment-delimited phases (1. SAGA session attach, 2. Inbound, 3. Index flush, ..., 11. Cancel typing). Adding any new turn-lifecycle behavior — e.g. the §12.4 arbiter, §12.2 applied-proposals audit, §12.5 subagent budget — requires editing this monster directly. Hooks would be the seam, but right now the hook system (`make_pre_tool_use_hook`, `make_post_tool_use_hook`) only covers tool-level events, not turn-lifecycle events.
- **Why it matters:** Every new feedback loop in §12 of FUTURE_WORK.md is going to either bloat run_turn further or end up grafted on as another "if not error: try _capture_X()" block. The function will hit 2000 lines in the next quarter at the current rate. Test surface for the function is also painful — most tests mock 5-7 dependencies at once.
- **Approach (design decision):** Define a `TurnHook` protocol with `pre_query(ctx, event) → block_or_None`, `post_query(ctx, event, messages, result) → None`, `finalize(ctx, record) → None` methods. Move existing behavior (rate-limit capture, plan-quota capture, subagent inbox push, post-message hook, index rebuild, typing cancel) into separate `TurnHook` implementations. `run_turn` becomes a 50-line orchestrator. **Keep ordering explicit in a `list[TurnHook]` rather than implicit-by-import.** ~400 LOC refactor; pure-shape change.
- **Effort:** L
- **Severity:** sev:med

### 16. Saga has no transactional context manager for multi-statement writes; `store_atom` does INSERT-OR-IGNORE + atom_topics + FTS + commit + close in non-atomic sequence

- **Where:** `saga/saga/core.py:296-357`
- **What's wrong:** `store_atom` opens a connection, INSERTs into `atoms` (could fail for unique constraint), then INSERTs into `atom_topics`, then INSERT INTO `atoms_fts`, then `commit`, then `close`. SQLite's autocommit-disabled mode (no `BEGIN`) means each INSERT is its own transaction. If FTS5 INSERT fails (caught by `try/except: pass`), the atoms_fts row is missing — but the atom is committed. Future search queries silently miss this atom for keyword retrieval. Same shape in `merge_atoms` (3389-3424), `pin_atom` (5420-5440), and `store_session_boundary` and 30+ other write paths.
- **Why it matters:** Quiet data divergence between the SQLite tables. The mimir-via-saga integration bench would surface big regressions but small inconsistencies (atoms-fts missing 1% of atoms) won't be visible. Long-running saga DBs slowly accumulate orphan rows that retrieval can't find.
- **Approach (design decision):** Wrap each write in `with conn:` (Python sqlite3's autocommit context manager) so the FTS failure rolls back the parent INSERTs. Or change get_db() to start an explicit transaction. **Prefer `BEGIN IMMEDIATE`/explicit txns** since FTS5 writes against the same DB serialize anyway, so no concurrency cost. ~50 LOC of careful refactoring across the 30+ call sites; bulk apply is feasible.
- **Effort:** M
- **Severity:** sev:med

### 17. `MessageBuffer.append` writes to disk inside the asyncio.Lock, blocking the event loop on slow filesystem

- **Where:** `mimir/history.py:144-158`
- **What's wrong:** `append` does `async with self._write_lock: ... await asyncio.to_thread(self._append_disk, msg)`. The lock is held *across* the to_thread — meaning every concurrent `append` serializes through one thread, defeating the to_thread parallelism. For a chatty deployment with 5 channels each appending at 10 msg/min, this is fine; for a Bluesky-firehose-like deployment, throughput tops out at 1/I-O-latency.
- **Why it matters:** Real bottleneck if any deployment ever drives high inbound throughput. Discord-only deployments won't notice; Slack workspace-wide subscribe + cross-channel pull might.
- **Approach (design decision):** Two options: (a) drop the lock entirely — append-only writes to a single file with `O_APPEND` semantics are kernel-atomic on POSIX for writes <PIPE_BUF (~4KB on Linux), and the in-memory deque mutation is single-threaded under asyncio; (b) move the disk write off the per-append critical path entirely — a background flusher task drains a queue. **Recommended choice: (a).** The in-memory deque update doesn't need the disk write to complete first; the file's append semantics are atomic. Drop the `await` and let the to_thread fire-and-forget with a bounded queue. ~30 LOC.
- **Effort:** S
- **Severity:** sev:low

---

## Observability

### 18. `tool_call_denied` and `tool_call_budget_warning` events log via `log_event` from inside the SDK's hook callback task — the contextvar fallback at `hooks.py:151` admits stale ctx but never logs the resolution path

- **Where:** `mimir/hooks.py:138-205`
- **What's wrong:** The comment at lines 142-150 explains an important subtlety: hook callbacks run on a separately-forked task whose contextvars predate `set_current_turn`, so the hook does `get_turn_by_session_id(input_data.get("session_id"))` first, falling back to `get_current_turn()`. But there's no telemetry distinguishing which path resolved — tests that mock the contextvar pass; production calls might be silently using the lookup table. If the SDK ever changes how it propagates `session_id` (e.g. drops it from hook args), this fallback breaks silently and the budget stops counting.
- **Why it matters:** When tool-budget enforcement breaks, the agent silently runs over budget and the operator only notices via cost spikes. The audit trail is the load-bearing signal for tuning.
- **Approach:** Add a `resolution_path: "session_id" | "contextvar" | "missing"` field to `tool_call_denied` and `tool_call_budget_warning` events. Add a counter assertion in `test_hooks.py` that the by-session-id path resolves under the SDK harness. ~15 LOC.
- **Effort:** S
- **Severity:** sev:low

### 19. No metric for "synthesis turn ran but agent skipped step 3 (saga_end_session)" — silent contract failure

- **Where:** `mimir/agent.py:_post_message_hook:1284`, `templates.py:render_saga_session_end`, `session_manager._dispatch_idle:198`
- **What's wrong:** The synthesis-turn prompt instructs the agent to call `saga_end_session` as step 3. Agent failures to call it are invisible — `_post_message_hook` skips synthesis turns, the session manager drops the in-memory session unconditionally, and the only observability is "did `saga_end_session` show up in events.jsonl during this synthesis turn." No event correlates the synthesis turn's `turn_id` to a missing `saga_end_session` call. Operators only notice via "Recent session summaries" being empty for a channel with active history.
- **Why it matters:** The session boundary atom is the load-bearing record of "what were we doing last time?" for the next session. If the agent skips step 3 on, say, 30% of synthesis turns (because the prompt instructions get crowded out by token-budget pressure), retrieval quality degrades over time with no diagnostic.
- **Approach:** Track `saga_end_session` calls per-turn-id in events.jsonl (already done — the tool emits an event). Add a synthesis-turn post-check that scans the just-completed turn's events for the call and emits `saga_synthesis_skipped_boundary` when missing. ~30 LOC. Bonus: surface in the algedonic block as a negative signal so the agent self-corrects.
- **Effort:** S
- **Severity:** sev:med

### 20. ClientPool's fingerprint-flip drain has no observability — silent client churn under config edits

- **Where:** `mimir/agent.py:268-301` (`_drain_idle_for_fingerprint_change`, `acquire`)
- **What's wrong:** When `_options_fingerprint` changes (system prompt edit, model swap, mid-run config reload), the pool drains all idle clients and marks in-flight ones stale. No event is emitted. No metric tracks how often this happens. If a deployment has flapping config (e.g. a memory file that contains the current timestamp and ends up in the system prompt), the pool churns continuously — every turn pays the cold-start tax (~5-9s) — and the operator sees only "mimir is slow" with no signal.
- **Why it matters:** This is exactly the kind of silent performance footgun the SPEC §16 prompt-cache audit was designed to catch but lacks a shipping mechanism for. The pool drains are the proximate cause; the fingerprint change is the effect of an unstable system prompt.
- **Approach:** Emit `client_pool_drained` event on `_drain_idle_for_fingerprint_change` with `(old_fingerprint_8, new_fingerprint_8, idle_disconnected, in_flight_marked_stale)`. Aggregate frequency in introspection-report cron — flag when drains happen > N/hour. ~25 LOC.
- **Effort:** S
- **Severity:** sev:med

---

## Tests where the gap masks a bug class

### 21. No test verifies `_pre_message_hook`'s `recent` context-window filtering correctly excludes the just-recorded inbound

- **Where:** `mimir/agent.py:1226-1240`
- **What's wrong:** The hook does `recent = self._buffer.recent_for_channel(...)` then conditionally drops `recent[-1]` if it matches the current event content. If `_record_inbound` ran (step 2), the inbound IS in the buffer, but the dedup check only matches on `content` equality. If two messages with identical content arrive in quick succession — say a user double-tapping a one-word query — the dedup drops the wrong one and the saga query gets the wrong rewrite context. There's no test for the boundary condition.
- **Why it matters:** Edge case but easy to hit. Saga's contextual rewrite costs an LLM call; feeding it the wrong context produces a worse rewrite, which then chains through retrieval quality.
- **Approach:** Add a unit test that constructs two identical-content messages, asserts the dedup keeps the right one (the prior, not the just-recorded). ~30 LOC.
- **Effort:** S
- **Severity:** sev:low

### 22. Arbiter trusts OAuth-endpoint utilization at face value; spurious 100% spikes wrongly suppress hours of scheduled work

- **Status:** ✅ Layer (a) landed 2026-05-06 (commit f4a5281). `detect_5h_anomaly` in `mimir/oauth_usage_poller.py` rejects readings where 5h jumps ≥50pp while 7d delta <5pp; rejected readings keep the prior trusted value, emit `quota_reading_anomalous` (first-occurrence-only feedback rendering), 6 new tests (suite 920→926). 200 historical anomalies back-filled into events.jsonl with `_backfill: cr22-backfill-2026-05-06`. Layer (b) (cost-rate-back-derived estimator) still open — see chainlink for follow-up.
- **Where:** `mimir/billing.py:AnthropicQuotaProvider.get_windows`, `mimir/billing.py:evaluate_quota`, `mimir/oauth_usage_poller.py:record_usage` (writer side)
- **What's wrong:** Anthropic's `/api/oauth/usage` endpoint occasionally reports bogus utilization values for the 5-hour window — observed twice now (2026-05-05 14:00 UTC: bounced 0%/100%/3% in three consecutive polls; 2026-05-06 04:03 UTC: jumped 7%→100% in 3 minutes and got stuck for 7+ hours). Each event suppressed all `scheduled_tick` heartbeats for the duration. Cross-checks confirm both were spurious: in the second incident the bot only spent ~$13 across the surrounding hour while the 7-day window moved +1pp — internally inconsistent with 100% of 5h quota actually being burned. The arbiter (`evaluate_quota` in `mimir/billing.py:215-280`) consumes whatever the provider returns without sanity-checking; `record_usage` writes whatever the endpoint sent without comparing against prior readings.
- **Why it matters:** Hours of S4 (autonomous) work get silently dropped when the endpoint glitches. The current Self-state block surfaces the suppression reason but the operator has to manually cross-reference cost-rate-vs-utilization to spot the data quality issue. This is the third quota-data-quality issue we've hit since the OAuth poller shipped (PR #8); the pattern is going to keep coming back as long as we trust the endpoint blindly.
- **Approach:** Two layers, additive:
  - **(a) Anomaly detector at write time.** In `record_usage` (or a new `mimir/billing.py:detect_quota_anomaly`), keep a small in-memory ring of the last N (=10) snapshots per window. When a new reading lands, flag as anomalous if: (i) jump size > 50 percentage points in a single poll interval (3 min) AND (ii) the corresponding longer-window value barely moved (e.g., 5h jumped >50pp but 7d delta was <5pp over the same interval, scaled to the period). Flagged readings get persisted with `anomalous: True` and emit `quota_reading_anomalous` algedonic event but do NOT replace the prior known-good value in `RateLimitStore` for that window. The renderer keeps showing the previous trustworthy value with a warning suffix ("(last verified Xm ago)").
  - **(b) Cost-rate cross-check estimator** (the back-propagation idea). When a 5h reading is anomalous, the arbiter falls back to an estimate derived from `aggregate_usage` (already exists in `mimir/usage_stats.py`): take the cost spent in the last 5 hours from `turns.jsonl`, divide by the operator's known 7d-quota-in-dollars (back-derived: `7d_dollars_observed / 7d_utilization_observed`), and compute `estimated_5h_util = 5h_dollars_recent * (5h_to_7d_quota_ratio) / 7d_quota_dollars`. We don't know the exact 5h-to-7d quota ratio but `(5h / 168h) * 14` (Anthropic's stated 5h-to-7d ratio is roughly 1.4× what flat scaling would give — confirmed empirically across recent windows) gets us within a factor of 1.5. Round to nearest 5pp; mark the estimate as `derived` so the arbiter can apply a higher suppress threshold (e.g., 90% derived vs 80% direct) to avoid over-aggressive suppression on noisy estimates. **Recommended choice:** ship (a) first as a 1-day item (anomaly detection without estimator); evaluate whether suppression-during-anomaly is rare enough that (b) isn't needed before building the estimator.
  - The arbiter (`evaluate_quota`) consults the anomaly flag: when a window's snapshot is `anomalous=True` AND a derived estimate exists, use the derived value with the higher threshold; if no derived estimate, treat the window as "no signal" (suppress=False) until the next non-anomalous reading.
- **Effort:** S for (a) alone, M for (a)+(b)
- **Severity:** sev:med

---

## Recommended sequencing

If shipping in priority order:

1. **#15 (run_turn refactor)** before any new §12 backlog item lands. Otherwise each §12 item bloats the monolith further.
2. **#10 (per-turn JSONL cache)** — biggest single perf win, blocks #5 and several §12 items.
3. **#2 (fire-and-forget `create_task`)** — sev:med, S, real correctness bug under load.
4. **#7 + #8 (OAuth credential durability)** — paired; both touch credential-write atomicity.
5. **#16 (saga transactional writes)** — quiet data-integrity bug.
6. **#14 (saga adapter dedup)** — slow-burning, but only matters when both `_InProcessSaga` and `_HttpSaga` ship in production.
7. Remaining items in any order.

**Added 2026-05-06 from operational incident:**

8. **#22 (arbiter quota anomaly detection)** — observed twice in 48h; second incident suppressed S4 work for 7+ hours unnecessarily. Ship the anomaly detector (part a) first.
