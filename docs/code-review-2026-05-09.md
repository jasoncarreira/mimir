# Mimir Code Review — 2026-05-09

A whole-codebase audit conducted by four parallel review agents, each focused
on a slice (agent runtime; external I/O; memory & retrieval; ops &
observability). Findings are organized by priority for triage, then grouped
by domain for slice-by-slice work. Cross-cutting patterns are called out
explicitly so the same fix isn't re-derived in three places.

This is a **point-in-time snapshot**. Code changes; before acting on a
specific finding, re-read the cited file:line to confirm the issue is still
present.

**Update 2026-05-10 (post-decision pass):** Three findings have been
re-graded after closer inspection — see the [Re-grades](#re-grades-2026-05-10)
section at the bottom. Severity adjustments propagated below.

---

## TL;DR

The codebase is in good shape on the recently-touched surfaces (poller
batching, algedonic surfacing, session summaries — these were reviewed
closely and the review surfaced no new bugs in the v0.4 chainlink work
itself). The audit did surface real bugs in older code that has accumulated
drift:

- **Billing-mode auto-detection is wrong on virtually every API-key
  install** — silently disables the dollar-cost suppression layer (`billing.py:90`).
- **Unauthenticated HTTP routes on `0.0.0.0`** leak conversation history,
  ops dashboards, and OAuth events. `MIMIR_API_KEY` only gates `/event`
  (`web_ui.py`, `ops_dashboard.py`, `bridges/web_chat.py`).
- **A SAGA outage during prompt assembly crashes the turn** instead of
  falling back to the local mirror (`agent.py:1581`).
- **Dispatcher drain() deadlocks on shutdown** if a turn was cancelled
  mid-flight (`dispatcher.py:124`).
- **EventLogger reads the entire events.jsonl into memory at startup** to
  count lines (`event_logger.py:32`); same pattern in `TurnLogger.__init__`,
  `loops_cmd`, `ops_dashboard._load_events`, and `web_ui._read_jsonl`.
- ~~**fsync-before-restart targets a readonly fd against a different writer's
  buffered output**~~ — *re-graded to low (doc-only). POSIX fsync is
  per-inode, not per-fd-open-flags; a readonly fsync correctly flushes the
  inode's pending OS-page-cache. The original framing was overcautious. See
  [Re-grades](#re-grades-2026-05-10).*

Cross-cutting patterns (each appears in ≥3 places): forward-scan whole-file
JSONL reads on the event loop, unauthenticated web endpoints assuming
`MIMIR_API_KEY` covers them when it doesn't, and `break`-early loops that
assume strictly-monotonic timestamps in append-only JSONL written by
multiple concurrent producers.

---

## Cross-cutting patterns

### Pattern A — Sync full-file JSONL reads on the event loop

The pattern: open a JSONL file, read it all into memory, iterate forward
filtering by timestamp / id / type. With the documented caps (events.jsonl
≤ ~300 MB, turns.jsonl ≤ 250 MB), each read is a multi-hundred-MB memory
spike that blocks the event loop.

Sites:
- `mimir/event_logger.py:32-38` — startup line-count via `path.read_text()`
- `mimir/event_logger.py:84` — `_trim` re-reads whole file
- `mimir/turn_logger.py:194-200, 213-237` — startup count + `_trim` + the
  per-turn `_write` is sync inside an `async` method
- `mimir/web_ui.py:48-65` — `/api/events`, `/api/turns` open & read the
  whole file synchronously per HTTP request
- `mimir/ops_dashboard.py:79-102` — forward-scan filtered by cutoff
- `mimir/loops_cmd.py:101-142` — same shape
- `mimir/agent.py:131-150` — `_filter_session_turns` for synthesis turns
- `mimir/shell_jobs.py:322-339` — entire stdout/stderr file into memory

**Fix shape:** Replace forward-reads with `_jsonl_tail.tail_jsonl_records`
(it exists, it's correct, it's used by some hot paths and not others).
Wrap any unavoidable full-file read in `asyncio.to_thread`. For HTTP
endpoints, bound by line-count regardless of `?limit=` query.

### Pattern B — Unauthenticated read endpoints on 0.0.0.0

The server binds `0.0.0.0` and only `/event` consults `MIMIR_API_KEY`.
Every other route on the same app is open to the network. The `MIMIR_API_KEY`
warning is misleading because setting it doesn't actually protect those
routes.

Sites:
- `mimir/web_ui.py:152-161` — `/api/turns`, `/api/events`, `/turns`
- `mimir/ops_dashboard.py` — `/ops`, `/api/ops` (no auth check anywhere
  in the file)
- `mimir/bridges/web_chat.py:129-156` — `/chat` POST + `/chat/stream` SSE
  with `Access-Control-Allow-Origin: *`

**Fix shape:** Apply the `_safe_str_eq(X-API-Key, expected_key)` gate as
a middleware on the whole app (or at minimum on every route that reads
state), or bind to `127.0.0.1` and front with a reverse proxy. Drop the
SSE wildcard CORS or move it behind an opt-in env var.

### Pattern C — `break`-early loops on assumed-monotonic JSONL timestamps

Multiple tail-walks assume timestamps are strictly monotonic and break on
the first record older than the cutoff. But `log_event` is called from
many concurrent coroutines / threads / producers; timestamps captured at
call-site can land out-of-order on disk if a writer pauses between
`datetime.now()` and `f.write()`. A single out-of-order record at the
window boundary terminates the walk early and silently drops in-window data.

Sites:
- `mimir/feedback.py:744-800, 805-815` — algedonic tail-walk, comment
  explicitly assumes chronological order
- `mimir/history.py:299-322` — `cross_author_messages` early-exit
- (likely also affects ops dashboard / loops_cmd if they ever convert to
  tail-reads)

**Fix shape:** Replace `break` with `continue` and rely on
`_DEFAULT_MAX_RECORDS` / equivalent bound. The walks are already bounded
by record count; dropping the early-exit costs essentially nothing.

### Pattern D — `asdict + json.dumps(..., default=str)` swallows schema drift

`turn_logger.py:209` and several other JSON-serialization sites use
`default=str` so any non-serializable type silently coerces to its
`str()` repr. SDK schema drift (e.g. a new `RateLimitInfo` dataclass in
the usage dict) lands in turns.jsonl as `"<RateLimitInfo object at 0x...>"`,
breaking downstream aggregation invisibly.

**Fix shape:** Replace `default=str` with a custom encoder that
`log.warning`s on each non-trivial coercion (or raises if
`MIMIR_STRICT_JSON=1` for tests), so SDK shape changes surface immediately.

---

## Top priorities (critical + high)

The list to action first. Everything below has been verified to file:line.

### 1. Billing-mode auto-detect picks QUOTA on virtually every API-key install
- **Severity:** critical
- **File:** `mimir/billing.py:90`, `mimir/config.py:52-78`
- **What:** `_oauth_credentials_path()` returns a non-None `Path` whenever
  `MIMIR_HOME` or `HOME` is set (no `.exists()` check — it's just a
  "where would credentials live" hint). `detect_billing_mode` then
  evaluates `if oauth_credentials_path:` (truthiness on a Path is always
  True) and returns `QUOTA`. A pure-API-key pay-as-you-go install with no
  Max OAuth at all gets classified as QUOTA, demoting `cost_rate_alert`
  to advisory and letting unbounded $/hr through (`HomeostaticArbiter.should_fire_heartbeat:220-228`).
- **Why it matters:** Silently inverts the docstring's intent
  ("`pay-as-you-go` — every token costs real money"). Operators expect
  cost-rate alerts on API-key installs and don't get them.
- **Fix:** Check `.is_file()` before treating the path as a billing-mode
  signal in `detect_billing_mode`, or pass
  `bool(oauth_credentials_path and oauth_credentials_path.is_file())`
  from `Config.from_env()`.

### 2. Unauthenticated HTTP read endpoints leak conversation history
- **Severity:** critical
- **Files:** `mimir/web_ui.py:152-161`, `mimir/server.py:539`,
  `mimir/ops_dashboard.py` (no auth check), `mimir/bridges/web_chat.py:129-156`
- **What:** See Pattern B above. `/api/events` returns the entire
  `events.jsonl` (oauth_logged_out, channel events, error strings); 
  `/api/turns` returns full agent transcripts; `/ops` returns
  channel ids, recent failure detail strings (often containing user
  message fragments), chainlink issue titles. `/chat` POST is
  unauthenticated and accepts arbitrary `extra` field; `/chat/stream`
  has wildcard CORS.
- **Fix:** API-key middleware on the whole app, or bind to `127.0.0.1`
  and front with a reverse proxy.

### 3. SAGA outage during prompt build crashes the turn
- **Severity:** high
- **File:** `mimir/agent.py:1581-1606`
- **What:** `_assemble_session_summaries` calls
  `await self._saga.recent_session_boundaries(...)` with no try/except.
  The local-mirror fallback at line 1604 only fires when the call returns
  an empty list, not when it raises. A transient SAGA outage at
  prompt-assembly time kills the entire turn instead of degrading to
  local-mirror data.
- **Fix:** Wrap the call in `try/except Exception` and treat any
  exception identically to "empty result". Mirror the pattern used in
  `_assemble_self_state_block` / `_assemble_usage_block`.

### 4. Dispatcher drain() deadlocks on shutdown after a cancelled turn
- **Severity:** high
- **File:** `mimir/dispatcher.py:124-136`
- **What:** Inner try/except catches `Exception` not `BaseException`. A
  `CancelledError` raised inside `_run_turn` skips `queue.task_done()`.
  `_in_flight` discard runs and the semaphore releases, but the queue's
  unfinished count never decrements — so `await queue.join()` in
  `drain()` blocks forever.
- **Fix:** Move `queue.task_done()` into a `finally` block surrounding
  the entire dispatch, and let CancelledError propagate naturally.
  **Also:** zero cancellation tests exist for this path
  (`tests/test_dispatcher.py`) — add one.

### 5. Spawn completion accounting silently dropped on closed loop
- **Severity:** high
- **Files:** `mimir/spawn.py:600-610`, `mimir/agent.py:870-887`
- **What:** `_on_complete` (waiter-thread callback) builds the `_emit()`
  coroutine and passes it to `schedule_from_thread`. When `self._loop` is
  None or closed, `_schedule_from_thread` returns silently without
  awaiting or closing the coroutine. The `claude_code_spawn_completed`
  event and the synthetic TurnRecord (which feeds plan-window spend)
  are lost.
- **Fix:** In `_schedule_from_thread`, when the loop is unavailable,
  explicitly `coro.close()` to suppress the warning and emit a
  `log.warning`. Better: persist the spawn record synchronously to disk
  before/instead of going through the loop.

### 6. EventLogger.__init__ reads entire events.jsonl into memory
- **Severity:** high
- **File:** `mimir/event_logger.py:32-38`, `:84` (`_trim`)
- **What:** On every process start, `path.read_text()` then `.splitlines()`
  against the firehose log. At ~300 MB upper bound, multi-hundred-MB
  memory spike; OOM-vulnerable on tight container limits.
- **Fix:** Chunked line-counter (read 64KB at a time, count `\n`). For
  `_trim`, reuse `_jsonl_tail.tail_jsonl_records`.

### 7. fsync-before-restart targets a readonly fd against different writer's buffered output
- **Severity:** ~~high~~ → **low (re-graded 2026-05-10, doc-only)**
- **Files:** `mimir/health_probe.py:362-376`, `mimir/event_logger.py:53-72`
- **What:** Original framing claimed readonly-fd fsync was "undefined/no-op
  for flushing a different file handle's data." That was overcautious —
  POSIX `fsync(2)` is **per-inode**, not per-fd-open-flags. Linux and
  macOS both follow this. The readonly-fd fsync DOES flush the inode's
  pending OS-page-cache regardless of which fd dirtied it. After
  `EventLogger.log`'s `with` block exits, the writer's Python buffer is
  flushed to OS page cache; the subsequent fsync correctly drives that
  to disk.
- **Why it matters now (smaller):** virtiofs / Docker-on-macOS has weaker
  fsync semantics — not a bug, a platform constraint worth documenting.
- **Fix:** Doc-only. Update `_fsync_events_log`'s docstring to acknowledge
  POSIX inode semantics and flag virtiofs as a known platform caveat. No
  code change.

### 8. Cost-rate baseline divides by full window on partial-week data
- **Severity:** high
- **Files:** `mimir/budget.py:170-171`, `mimir/usage_stats.py:341-342`
- **What:** `baseline = w.total_cost_usd / (24 * 7)` assumes turns.jsonl
  spans 7 days. On fresh installs / after the 5k-turn cap trim, the file
  may only span hours; the divisor is still 168, so baseline is
  artificially deflated 50–500×. The spike check fires on normal usage.
- **Fix:** Use `min(baseline_window_hours, hours_since_first_record)` as
  the divisor, or skip the spike check when fewer than ~24h of data exist.

### 9. _read_jsonl in /api/events and /api/turns blocks event loop
- **Severity:** high
- **File:** `mimir/web_ui.py:48-65` (called at lines 91, 107)
- **What:** Each request opens and reads the whole file into memory
  synchronously — blocks the asyncio loop, O(N) memory spike per request.
  Compounds with the 5s polling from the turn-viewer; concurrent requests
  amplify into a memory DoS.
- **Fix:** `asyncio.to_thread` + `_jsonl_tail`. Add a server-side max
  line cap independent of the `?limit=` query.

### 10. evaluate_cost_rate spike-floor short-circuits the absolute check
- **Severity:** ~~high~~ → **low (re-graded 2026-05-10, doc-only)**
- **File:** `mimir/usage_stats.py:334-339`
- **What:** On closer reading, the existing ordering is correct: the
  absolute hourly-limit check at line 326 fires **before** any spike-branch
  logic, regardless of floor. The line-339 `return None` exits only the
  spike branch (after the absolute check has already been evaluated) —
  the floor doesn't suppress absolute alerts. So the floor is already
  "spike-only" in effect.
- **Real residual concern:** an operator who sets only `spike_ratio`
  (no `hourly_limit_usd`) and runs sub-floor never alerts. That's by
  design (docstring lines 309-318: "$5/hr is the neighborhood that
  catches genuine oddities while ignoring normal working sessions") —
  pair-with-absolute is the documented contract.
- **Fix:** Doc-only. Update the docstring to make the pairing
  requirement explicit: "If `spike_ratio` is set without
  `hourly_limit_usd`, sub-floor rates never alert by design — pair
  with an absolute ceiling for true protection."

### 11. _HttpSaga._get_or_empty never retries — silent on transient blip
- **Severity:** ~~medium-high~~ → **low (re-graded 2026-05-10, doc-only)**
- **File:** `mimir/saga_client.py:584-599`
- **What:** The asymmetry is intentional and the name telegraphs it.
  `_get_or_empty` is the prompt-assembly fast path; adding `_post`'s
  ~1.4s retry backoff would block the prompt build on every transient
  blip — exactly the failure mode PR #96 (CR2-#3, just merged) is
  fighting. Fast-degrade-to-empty is the right shape.
- **Why it doesn't matter:** PR #96 closed the consumer-side loop —
  `_assemble_session_summaries` now falls through to the local mirror
  on both empty result AND raise. No missing fallback.
- **Fix:** Doc-only. Update `_get_or_empty`'s docstring to explicitly
  document "fast degrade for prompt assembly; no retries by design —
  caller is responsible for fallback semantics on empty result." Keep
  `_post`'s retry policy as-is.

### 12. /chat POST unauthenticated with arbitrary extra; SSE wildcard CORS
- **Severity:** high (subset of pattern B but worth surfacing)
- **File:** `mimir/bridges/web_chat.py:129-156`
- **What:** `_handle_post` enqueues an `AgentEvent` with
  `extra=body.get("extra") or {}` — no auth, no `extra` whitelisting.
  `/chat/stream` returns `Access-Control-Allow-Origin: *`, so any
  malicious site can drive the agent into LLM turns and read live
  outbound traffic cross-origin.
- **Fix:** Apply API-key middleware. Drop the `*` CORS header (or
  opt-in env var for local dev only). Whitelist `extra` keys at route
  boundary.

---

## Findings by domain

### Agent runtime

#### Tool-call budget over-counts denied calls; warning fires only on exact equality
- **Severity:** medium
- **File:** `mimir/hooks.py:215-247`
- **What:** `ctx.tool_call_count += 1` runs *before* the budget check, so
  denied calls still increment. After exceeding budget, the count drifts
  arbitrarily above `budget` while logged in `tool_call_denied` events.
  Also, soft-warning trigger is `if count == soft_threshold` — fragile;
  if any path skips an increment, the warning never fires.
- **Fix:** Increment only when a non-denied call is allowed. Change
  warning trigger to `if count >= soft_threshold and not ctx._soft_warning_emitted`.

#### Streaming streamed_plan flag set even when nothing was actually delivered
- **Severity:** medium
- **File:** `mimir/_streaming_dispatch.py:165-169, 280-294`
- **What:** `advance_state` sets `state.streamed_plan = True` when
  `candidate_plan` is non-empty after stripping. But `observe()` may
  reduce that to nothing once `parse_directives` strips action tags, and
  skip the bridge send. If `parse_directives` fails or bridge returns
  `sent=False`, `streamed_plan` is still True. Downstream
  `streaming_active_for_log` (agent.py:2213) then claims text was
  "suppressed from the user" when in fact the user got nothing.
- **Fix:** Only set `state.streamed_plan = True` after the bridge
  confirms `sent=True`. Move the flag flip out of `advance_state`.

#### Pool's _current_client_cell.set() never reset; leaks across child tasks
- **Severity:** medium
- **File:** `mimir/agent.py:396-397`
- **What:** During pool growth, `_current_client_cell.set(entry.cell)`
  is called without capturing/resetting the Token. The contextvar
  binding persists for the rest of the acquiring task's execution; any
  `asyncio.create_task` inherits *this* cell, even on unrelated
  turns/channels.
- **Fix:** Capture the Token returned by `set()`, `reset()` it in a
  `try/finally` around the connect attempt. Or use
  `copy_context().run(...)` for the connect-time hook.

#### TurnLogger sync I/O in async write path
- **Severity:** medium
- **File:** `mimir/turn_logger.py:194-237`
- **What:** Three sync I/O calls block the event loop: __init__ reads
  whole file via `read_text()`; `_write` opens and writes inside an
  `async` function without `to_thread`; `_trim` reads whole file
  synchronously. `_trim` runs from inside `run_turn`'s
  `await self._turn_logger.write(record)`, pausing the loop hundreds of ms
  on hot files.
- **Fix:** Wrap `_trim` in `asyncio.to_thread`. Stream-count for init.

#### spawn_claude_code agent_name LLM-controlled, not validated
- **Severity:** medium
- **File:** `mimir/spawn.py:429, 481, 530`
- **What:** Unknown profile name flows through to spawned `claude -p`
  invocation. Spawn fails opaquely; caller sees only exit code and
  `parse-failed` terminal_reason. Wastes spawn budget and timeout to
  discover a typo.
- **Fix:** Validate `agent_name` against
  `<mimir_home>/.claude/agents/*.md` at spawn time, reject unknown
  names with `_content_block(..., is_error=True)`.

#### Per-channel queue + high-water dicts grow unbounded
- **Severity:** low
- **File:** `mimir/dispatcher.py:33-37, 65-68, 97-110`
- **What:** When a worker idle-times-out, the queue and high-water entry
  remain in dicts forever. Ephemeral channel_ids accumulate.
- **Fix:** On `worker_retired`, `del self._queues[channel_id]` and
  `del self._high_water_logged[channel_id]` after confirming
  `queue.qsize() == 0`.

#### SubagentLifecycleHook drops late-arriving notifications across turns
- **Severity:** low
- **File:** `mimir/turn_hooks.py:213-249`
- **What:** `ctx.task_descriptions` lives one turn. Background-spawned
  subagents (most per `subagent_defs.py`) complete in later turns; the
  description lookup returns `None` because the new turn's ctx has empty
  `task_descriptions`. User-facing "Subagent updates" shows
  `[completed] task_id=abc — None`.
- **Fix:** Move `task_descriptions` to a process-level dict on the hook,
  keyed by task_id. Cap with LRU.

#### TurnContext turn_id has only 48 bits of entropy
- **Severity:** low
- **File:** `mimir/models.py:139`
- **What:** `make_turn_id()` returns `uuid.uuid4().hex[:12]` — 48 bits.
  Birthday-bound 50% collision at ~16M turns. Active-turns registry
  collision causes budget hook to enforce against wrong ctx.
- **Fix:** Use full uuid4 hex (or 16 chars / 64 bits). Id is for keys,
  not display.

#### _filter_session_turns scans turns.jsonl from start (synthesis turn)
- **Severity:** low
- **File:** `mimir/agent.py:131-150`
- **What:** Synthesis turns iterate the entire turns.jsonl from top to
  filter by `saga_session_id`. Wrapped in `to_thread` so off-loop, but
  multi-second blocking thread on 250MB files. Synthesis = idle/pause
  point, where users notice latency.
- **Fix:** Tail-read using `_jsonl_tail.tail_jsonl_records`, break early
  when timestamp predates session start.

### External I/O (pollers, scheduler, server, bridges)

#### Unbounded retry of OAuth poller on permanent logged_out
- **Severity:** medium
- **File:** `mimir/oauth_usage_poller.py:946-960, :982-996`
- **What:** Once `oauth_logged_out` fires (refresh token revoked),
  every cron tick retries — generating a new `oauth_logged_out` event
  per minute. Algedonic surfacing then drowns in repeated negative.
- **Fix:** Track sticky `logged_out_since` in the existing first-seen
  sidecar; while set, skip refresh and emit one throttled reminder/hour.

#### oauth_usage_poller.poll_once: no ClientSession timeout
- **Severity:** medium
- **File:** `mimir/oauth_usage_poller.py:931-933`
- **What:** Default `aiohttp.ClientSession()` has no total timeout. A
  hung Anthropic endpoint blocks the cron callback indefinitely; with
  `coalesce=True, max_instances=1`, every subsequent quota update is
  silently dropped. Arbiter then suppresses S4 work on stale data.
- **Fix:** `aiohttp.ClientTimeout(total=30)`. Wrap `poll_once` body in
  `asyncio.wait_for` belt-and-suspenders.

#### Scheduler.reload() invoked through asyncio.to_thread mutates AsyncIOScheduler from worker thread
- **Severity:** medium
- **File:** `mimir/scheduler.py:498, :508, :645`
- **What:** `add_job` / `remove_job` / `reload_pollers` wrap sync methods
  in `asyncio.to_thread` that then mutate AsyncIOScheduler whose
  internal job-store wakeup uses `loop.call_soon_threadsafe`. APScheduler's
  job-store mutation isn't formally thread-safe across process types.
  In-flight `_fire_poller` may look up `self._pollers[poller_name]`
  after worker thread cleared the dict.
- **Fix:** Make `reload()` / `_reinstall_pollers` async; await directly
  on the loop. Wrap only the file IO (`load_jobs`) in `to_thread`. Or
  hold `_mutate_lock` around the APScheduler mutation segment.

#### git_tracking._pending_push_task is module global — concurrent homes collide
- **Severity:** medium (currently dormant)
- **File:** `mimir/git_tracking.py:47-48, :257-263`
- **What:** Global debounce state. A turn that commits in one repo will
  cancel a pending push in a different repo if the agent ever serves
  more than one home. Async tests reset via `reset_module_state` because
  of this shape.
- **Fix:** Move debounce state onto a class instance owned by Agent, or
  key by `home`.

#### Pollers framework has no global concurrency cap
- **Severity:** medium
- **File:** `mimir/pollers.py:306-316`, `mimir/scheduler.py:680-693`
- **What:** Each poller spawns its own subprocess on its own cron, not
  serialized against each other. 50 skills with `* * * * *` crons →
  50 subprocesses every minute. A buggy skill that spawns its own
  children fork-bombs without any framework ceiling.
- **Fix:** `asyncio.Semaphore` around `run_poller` calls, capped from
  `MIMIR_MAX_CONCURRENT_POLLERS` (default 4-8). Log
  `poller_concurrency_throttled`.

#### APScheduler poller misfire_grace_time=60 collides with POLLER_TIMEOUT_SECONDS=60
- **Severity:** medium
- **File:** `mimir/scheduler.py:683-693`, `mimir/pollers.py:60`
- **What:** Cron `* * * * *` + 60s timeout exactly matches grace=60. A
  legitimately-slow poller silently stops firing every other minute
  without operator visibility (APScheduler drops misfires before the
  callback runs — no `poller_fire_dropped` event).
- **Fix:** Set `misfire_grace_time` to 5-10s so APScheduler explicitly
  logs the missed-fire. Better: register an `EVENT_JOB_MISSED`
  listener that emits a `poller_misfired` event.

#### shell_jobs.read_output reads entire stdout/stderr file into memory
- **Severity:** medium
- **File:** `mimir/shell_jobs.py:322-339`
- **What:** Acknowledged TODO. A 1-hour `bash_async` job streaming to
  stdout produces a multi-GB file; the next `bash_job_output` OOMs the
  agent. The TODO claims it's "bounded in practice" — not a guarantee.
- **Fix:** Read last N×4KB chunks via `seek` from end, or hard-cap reads
  at e.g. 10MB returning a "(truncated, use less or grep)" marker.
  Run inside `asyncio.to_thread` regardless.

#### Pollers /bin/sh -c env inherits full host env including secrets
- **Severity:** low
- **File:** `mimir/pollers.py:296-312`
- **What:** `env = {**os.environ, **poller.env}` passes the entire
  mimir process env (including `MIMIR_API_KEY`, `SAGA_API_KEY`,
  `ANTHROPIC_API_KEY`, `DISCORD_TOKEN`, `SLACK_BOT_TOKEN`,
  `GITHUB_TOKEN`) to every poller subprocess. A buggy poller that
  prints `env` to stderr leaks secrets to events.jsonl.
- **Fix:** Whitelist passed env: `PATH`, `HOME`, `STATE_DIR`,
  `POLLER_NAME`, plus the explicit `poller.env` overlay. Skill authors
  needing a specific secret declare it in `pollers.json` (operator
  review surface).

#### bootstrap_git_repo subprocess.run(timeout=30) doesn't catch TimeoutExpired
- **Severity:** low
- **File:** `mimir/git_bootstrap.py:664-681`
- **What:** Only `CalledProcessError` is caught. A 31s remote turns
  startup into a fatal `TimeoutExpired` that the server's bare
  `except Exception` swallows with a one-line `git_bootstrap_failed`
  event. Partial state (e.g. `.git/credentials` written, remote unset)
  may persist.
- **Fix:** Catch `subprocess.TimeoutExpired` in `_run` (or in
  `bootstrap_git_repo`); emit a structured event; cleanup partial state.

#### web_chat SSE subscribers list iteration unsynchronized
- **Severity:** low
- **File:** `mimir/bridges/web_chat.py:60-72, :178-196`
- **What:** `disconnect()` iterates `list(self._subscribers)` without
  acquiring `self._lock`. Subscribe/unsubscribe paths DO. Shutdown
  racing a new SSE client mid-subscribe can call `q.put_nowait(None)`
  on a removed queue, or miss a queue just appended.
- **Fix:** `async with self._lock:` in `disconnect()`.

#### pollers.py references Any without importing it
- **Severity:** low
- **File:** `mimir/pollers.py:373, 449, 486, 548`
- **What:** Function-body annotations use `Any`, never imported.
  `from __future__ import annotations` makes the module load (PEP 563),
  but `inspect.get_type_hints` raises `NameError`. mypy/pyright strict
  mode breaks too.
- **Fix:** Add `Any` to the existing `from typing import ...` line.

### Memory & retrieval (saga, history, search, identities)

#### MessageBuffer.recent_for_channel ignores channel_id for public targets
- **Severity:** medium
- **File:** `mimir/history.py:212-266`
- **What:** Despite name, public-target branch builds the pool from
  *every* public channel and ignores the named channel entirely. The
  current channel only "naturally dominates" if busy. A quiet target
  channel mid-busy-server-traffic gets *zero* of its own messages in
  the recent window. Docstring acknowledges this is by design (chainlink
  #40 / #43) but the API name is misleading.
- **Fix:** Rename to `recent_for_target(target_channel_id, limit)` to
  reflect that the param is a privacy/policy switch. Separately,
  consider reserving N slots for the target channel before pooling.

#### cross_author_messages early-exit on assumed-monotonic ts
- **Severity:** medium
- **File:** `mimir/history.py:299-322`
- **See Pattern C above.**
- **Fix:** Replace `break` with `continue`. Bounded by `global_max=500`.

#### feedback.py tail-iteration early-exit on assumed-monotonic ts
- **Severity:** medium
- **File:** `mimir/feedback.py:744-800, :805-815`
- **See Pattern C above.**
- **Fix:** Replace `break` with `continue`. Bounded by
  `_DEFAULT_MAX_RECORDS=10000`.

#### _atoms_in_payload may double-count atoms when multiple shapes present
- **Severity:** medium
- **File:** `mimir/sagatools.py:170-206`
- **What:** Iterates `("observations", "raws", "atoms", "_raw_atoms",
  "raw_atoms")` and concatenates them, then walks `sections.values()`.
  If saga returns BOTH `observations` and `atoms` (transitional release
  / server bug), the same atom gets counted twice — `mark_contributions`
  records 2× credit and the rendered prompt block doubles.
- **Fix:** Pick one shape per response (early-return when
  `observations`/`raws` non-empty), or de-dup by
  `atom.get("id") or atom.get("atom_id")`.

#### _HttpSaga._post retry loop's "exhausted" raise loses original exception
- **Severity:** medium
- **File:** `mimir/saga_client.py:548-582`
- **What:** Trailing `raise SagaError(... "retry loop exhausted")` is
  unreachable today. A future edit dropping a `continue` would fall
  through; `last_status`/`last_body` are only set on the 5xx path, so a
  `ClientError`-driven exhaustion would lose the original exception.
- **Fix:** Restructure to `last_exc = None` accumulation pattern with
  one explicit `raise SagaError(...) from last_exc` at the end. At
  minimum, capture `(aiohttp.ClientError, asyncio.TimeoutError)`
  alongside `last_status/last_body`.

#### _search_sync candidate-pool fill stage pollutes scoring
- **Severity:** medium
- **File:** `mimir/search.py:548-568`
- **What:** When BM25 returns close to `candidate_pool=50` results, the
  cosine fill stage reads up to 50 rows *unfiltered by query relevance*
  to fill the pool. Those random chunks pollute scoring with low-cosine
  padding.
- **Fix:** Skip the fill when BM25 returned ≥ k matches. Or switch fill
  to a vector-search candidate generator (sample by mtime recency or
  random hash). Bigger redesign; in the meantime document the intent.

#### chunks not deleted on file-delete branch in _reindex_sync (relies on FK cascade)
- **Severity:** medium
- **File:** `mimir/search.py:419-457`
- **What:** Delete branch deletes from `files` and `chunks_fts` but not
  `chunks`. Relies on `ON DELETE CASCADE` from `files(path)`. The
  *update* branch explicitly deletes from `chunks` + `chunks_fts` —
  inconsistent. If FK support flips off (per-connection PRAGMA), orphans.
- **Fix:** Add explicit `DELETE FROM chunks WHERE path = ?` to delete
  branches (lines 425-429, 488-493). Belt-and-suspenders.

#### saga in-process query latency clock starts after get_config
- **Severity:** low
- **File:** `mimir/saga_client.py:225-231`
- **What:** `t0 = time.time()` set after `get_config()`, so cold-load
  on first call (`_ensure_ready`) isn't reflected in `latency_ms`.
  Bench/observability data drifts from real first-turn latency.
- **Fix:** Move `t0` to before `_ensure_ready`.

#### IdentityResolver.reload not atomic for in-flight readers
- **Severity:** low
- **File:** `mimir/identities.py:130-349`
- **What:** Six attributes reassigned non-atomically relative to each
  other. A concurrent `display_name(author)` straddling reassignment
  reads new `_alias_map` against old `_display_names`. Probably rare
  (reload at startup or operator-triggered), but unguarded.
- **Fix:** Build a single immutable dataclass and assign in one swap.

#### _append_disk has no flush — JSONL line buried in OS cache on crash
- **Severity:** low
- **File:** `mimir/history.py:170-176`
- **What:** Comment claims `O_APPEND` atomicity; that's interleave-atomicity
  not durability. Crash between write-return and OS flush loses recent
  history.
- **Fix:** Either `os.fsync(f.fileno())` or update the docstring to
  clarify durability vs interleave-atomicity. Probably the latter;
  chat_history isn't load-bearing.

#### feedback first-occurrence dedup is global; content dedup is per-polarity
- **Severity:** low
- **File:** `mimir/feedback.py:730-800`
- **What:** Latent: any future kind that classifies polarity dynamically
  (like `react_received`) and lands in `_FIRST_OCCURRENCE_ONLY_KINDS`
  would have non-obvious cross-polarity suppression.
- **Fix:** Make `seen_first_only` per-polarity, or add an assertion
  forbidding polarity-dynamic kinds in `_FIRST_OCCURRENCE_ONLY_KINDS`.

#### _SLACK_ALIAS_TO_GLYPH out of sync with _POSITIVE_GLYPHS
- **Severity:** low
- **File:** `mimir/reactions.py:31-49`
- **What:** Slack `:clap:` (👏) reaches `classify_reaction` as `"clap"` →
  not in alias map → returns unchanged → not in `_POSITIVE_GLYPHS` →
  `neutral`. Same for `eyes`, `pray`, `point_up`, `ok_hand`. Two tables
  silently out of sync.
- **Fix:** Expand alias map to cover every emoji in
  `_POSITIVE_GLYPHS`/`_NEGATIVE_GLYPHS`, or import a shared emoji-name
  table from the `emoji` package.

#### search.py LRU cache bound to bound method retains self refs
- **Severity:** low
- **File:** `mimir/search.py:296-298`
- **What:** Wrapping a bound method in `functools.lru_cache` retains
  `self` refs. Test-side memory growth across Indexer creates/destroys.
  Worse: if `_embedder` is replaced post-construction, cached values
  refer to the old embedder's outputs.
- **Fix:** Move LRU to a free function with `(embedder_id, text)` key.

#### _HttpSaga retry constants loosely coupled
- **Severity:** low
- **File:** `mimir/saga_client.py:59-60, 555-573`
- **What:** `_MAX_RETRIES = 3` and `_RETRY_DELAYS_S = (0.2, 0.4, 0.8)`.
  Bumping `_MAX_RETRIES` without growing the delays tuple raises
  `IndexError` inside the retry loop, bubbling unwrapped (not as
  `SagaError`).
- **Fix:** `delay = _RETRY_DELAYS_S[min(attempt, len(_RETRY_DELAYS_S) - 1)]`.

#### identities_populator partial-pagination invisible
- **Severity:** low
- **File:** `mimir/identities_populator.py:493-580`
- **What:** Slack pagination loop catches all exceptions and breaks. Page
  3 of 10 failing leaves pages 1-2 partial; `merge_into_yaml` doesn't
  know it got partial data; YAML write "looks complete". Idempotency
  saves the next run, but operator has no visibility.
- **Fix:** Track `pagination_completed: bool` per loop; emit
  `populator_partial` event if False; consider not writing partial pages.

#### _post_message_hook may double-credit atoms on multi-send turns
- **Severity:** low (needs trace)
- **File:** `mimir/agent.py:1708-1753`, `channeltools.py:382-401`
- **What:** Two `send_message` calls on the same turn each trigger
  `feedback` with `ctx.saga_atom_ids` (not reset between sends). If
  saga's `mark_contributions` isn't idempotent on
  `(atom_id, session_id)` pairs, atoms get credited twice with two
  different texts.
- **Fix:** Trace saga server's `mark_contributions` idempotency. If not,
  reset `ctx.saga_atom_ids` after each `send_message` feedback or move
  the credit pass to once-per-turn at end.

#### saga_session_end local-mirror may attribute boundary atom to wrong channel
- **Severity:** low
- **File:** `mimir/sagatools.py:533-549`
- **What:** Local-mirror append uses `ctx.channel_id`. When ctx
  resolution returns `single_active`, that ctx may belong to a different
  session/channel than the one being closed. The local mirror would
  then mis-attribute boundary atom; `recent_session_boundaries` filters
  on the wrong channel next turn.
- **Fix:** Use the explicit `session_id` to look up channel via session
  manager, rather than trusting resolved ctx.

#### wiki_backlinks self-link exclusion drops cross-category-same-stem links
- **Severity:** low
- **File:** `mimir/wiki_backlinks.py:72-91, :138-167`
- **What:** Slug → path is last-wins; orphan calc and self-link
  exclusion go by slug, so `concepts/foo.md` linking to `topics/foo.md`
  is dropped as a self-link (both have `slug == "foo"`).
- **Fix:** Path-key everything; slug only for `[[link]]` resolution.
  Or emit `wiki_slug_collision` algedonic event when `find_pages`
  overwrites — operator-visible.

### Ops & observability

#### mimir stats CLI replicates a subset of agent's resource block — drifts
- **Severity:** medium
- **File:** `mimir/cli.py:1575-1617`
- **What:** Skips billing-mode-aware advisory-vs-alert distinction,
  cooldown gate, quota-mode evaluation. Operators trust `mimir stats`
  to mirror what the agent saw last turn; today it's a parallel
  implementation that drifts. Wrong answers under quota mode.
- **Fix:** Extract the rendering helper from
  `agent.py:_assemble_resource_usage_block` so both call sites share
  alert classification. Or have `mimir stats` print billing-mode + which
  event the alert would emit.

#### mimir setup vs Config use different env-var bool parsers
- **Severity:** medium
- **File:** `mimir/cli.py:1063-1064`, `mimir/config.py:493`
- **What:** cli.py compares against `{"false","0","no","off"}`; config.py
  uses `_env_bool` (accepts `1/true/yes/on`). For
  `MIMIR_GIT_TRACKING_ENABLED=y` or `=enabled`, setup interprets as
  enabled-by-default; Config interprets as disabled. Different code
  paths → different behavior on the same env var.
- **Fix:** Have `setup_home` import and use `_env_bool`. One canonical
  bool parser.

#### loops_cmd._measure_runtime re-reads entire events.jsonl per invocation
- **Severity:** medium
- **File:** `mimir/loops_cmd.py:101-142`
- **See Pattern A above.**
- **Fix:** `tail_jsonl_records`, short-circuit at 24h cutoff. Bonus:
  return early once all loop_ids seen.

#### ops_dashboard._load_events forward-scans whole file
- **Severity:** medium
- **File:** `mimir/ops_dashboard.py:79-102`
- **See Pattern A above.**
- **Fix:** `tail_jsonl_records`, break when `ts < cutoff`.

#### health_probe._send_restart_signal proceeds without confirming bookkeeping write
- **Severity:** low
- **File:** `mimir/health_probe.py:486-494`
- **What:** Deliberate per the comment: even on bookkeeping write
  failure, restart still fires. Defeats the rolling-window guard if
  writes consistently fail — exactly the case the bind-mount staleness
  recovery is fighting. Persistently broken file → unbounded restart
  loop.
- **Fix:** Persist restart timestamps to a fallback location (e.g.
  `/tmp/mimir-health-probe-restarts.jsonl`) when home write fails.

#### Config.from_env() calls _oauth_credentials_path() twice
- **Severity:** low
- **File:** `mimir/config.py:463-474`
- **Fix:** Resolve once into a local, pass to both call sites.

#### usage_stats._find_window matches by label string
- **Severity:** low
- **File:** `mimir/usage_stats.py:355-364`
- **What:** Caller-supplied `window_labels` to `aggregate()` silently
  break the spike-ratio lookup. Currently no such caller, but coupling
  is implicit; future operator-tuning vector introduces silent
  regression.
- **Fix:** Lookup by `hours` (store hours alongside label on
  `UsageWindow`) instead of regenerating the label string.

#### regenerate_api_key function blindly writes vs CLI's existence guard
- **Severity:** low
- **File:** `mimir/cli.py:1323-1329, :1619-1635`
- **What:** Inconsistent guards between the function and its CLI
  front-door. Importing `regenerate_api_key()` from elsewhere silently
  scaffolds a `.env`.
- **Fix:** Move `.is_file()` precondition into the function itself.

---

## Trace-further items

These need cross-file investigation, not a one-line fix:

1. **Saga server's `mark_contributions` idempotency** on
   `(atom_id, session_id)`. Affects whether multi-send turns
   double-credit atoms (`agent.py:1708-1753` finding above). Worth
   confirming server-side before deciding on the agent-side fix.

2. **`_assemble_session_summaries` fallback behavior** on empty list vs
   raise (`agent.py:1581`). The local-mirror fallback fires only on
   empty — does empty actually happen on transient SAGA blips? Trace
   `_get_or_empty` failure modes against caller expectations.

3. **Whether out-of-order `ts` events occur in events.jsonl under load.**
   The whole "Pattern C" class of bugs assumes append-order can diverge
   from timestamp-order. Audit a recent events.jsonl tail to confirm
   the failure mode is reachable in practice. If yes, fix is a class
   fix; if no, severity drops to "future-proofing."

4. **Whether saga's response shape can return both `observations` and
   `atoms` simultaneously** (`sagatools.py:170-206`). Affects whether
   the de-dup is a correctness fix or just defense-in-depth.

---

## What's known-good (from this review)

These were reviewed in detail and verified correct — don't re-investigate
unless something changes:

- **PR #93 framework-level poller batching:** per-fire `source_id`
  timestamps, `batch_size` validation, per-item starvation cap when
  `batch_size > 1`, marker padding for 10+ item batches, `finally`
  cleanup of subprocesses on timeout/cancel. The framework's chainlink
  consumers (tests, scheduler `_fire_poller`) are aligned with the new
  shape.
- **`_jsonl_tail._tail_lines` chunk-boundary handling:** structurally
  correct (the leading_fragment concatenation isn't backwards as it
  reads at first glance). Multi-byte UTF-8 across chunk boundaries is a
  non-issue because line breaks are single-byte `\n`.
- **`O_APPEND` atomicity claim in `MessageBuffer.append`:** correct for
  per-line interleave-atomicity (which is what the docstring actually
  documents, despite the durability ambiguity).

---

## How this review was conducted

Four parallel review agents, each focused on a slice:
- **Runtime:** agent.py, spawn.py, dispatcher.py, _streaming_dispatch.py,
  hooks.py, turn_hooks.py, turn_logger.py, subagent_*.py, loop_detector.py,
  session_manager.py
- **External I/O:** pollers.py, scheduler.py, scheduletools.py,
  oauth_usage_poller.py, server.py, web_ui.py, bridges/, git_bootstrap.py,
  shell_jobs.py
- **Memory & retrieval:** saga_client.py, sagatools.py, feedback.py,
  history.py, identities*.py, channel_registry.py, search.py,
  searchtools.py, wiki_backlinks.py, reactions.py
- **Ops & observability:** cli.py, config.py, billing.py, budget.py,
  rate_limits.py, event_logger.py, health*.py, ops_dashboard.py,
  usage_stats.py, jsonl_snapshot.py, _jsonl_tail.py, models.py

Each agent looked for: correctness bugs, security/safety, resource leaks,
async pitfalls, dead code / drift, test coverage gaps for critical paths,
non-trivial design smells. Style nits and items already in TODO/FIXME
comments were excluded unless they hid a deeper issue.

Findings are weighted toward signal over volume — a short list of real
issues beats a long list of nits. ~62 raw findings in total; this doc
reflects the consolidated set, with cross-cutting patterns lifted out
of per-finding redundancy.

---

## Re-grades (2026-05-10)

After a closer pass on the four highest-stakes "needs-design-call" items,
three were re-graded down on the basis of "the original review was
overcautious or missed an existing invariant." Recording the deltas here
so the same mistake isn't re-derived on the next review:

| # | Title | Original | Re-graded | Reason |
|---|-------|----------|-----------|--------|
| 7 | fsync against readonly fd | high | low (doc) | POSIX fsync is per-inode, not per-fd-open-flags; readonly-fd fsync correctly flushes the inode's pending writes regardless of which fd dirtied it. |
| 10 | spike-floor short-circuits absolute | high | low (doc) | The absolute-limit check at line 326 fires *before* the spike branch, so the floor never suppresses absolute alerts. Sub-floor "no spike alert" is the documented design intent ("ignore normal working sessions"); pair-with-absolute is the contract. |
| 11 | _get_or_empty no retries | med-high | low (doc) | Intentional fast-degrade-to-empty for the prompt-assembly path. PR #96 closes the consumer loop (local-mirror fallback fires on empty AND raise). |

The doc-only fixes are still worth shipping because they prevent the
same misreading on the next review, but they shouldn't compete with
real correctness work for queue priority.

**CR2-#1 (billing-mode auto-detect)** retains its original critical
severity — verified the path-truthy claim still holds at billing.py:90
and config.py:52-78. Mechanical fix (add `.is_file()` guard).

### Lesson for future code review prompts

Three of four "high-severity" findings on the design-call slice were
overstated. Common cause: the review agents flagged shapes that *looked*
suspicious without verifying the runtime invariant (POSIX fsync
semantics, branch ordering in `evaluate_cost_rate`, intentional naming
in `_get_or_empty`). When dispatching review agents, prompt them to
distinguish "this code's behavior is wrong" from "this code's shape
looks risky"; the second category needs a verification step before it
graduates to "high."

