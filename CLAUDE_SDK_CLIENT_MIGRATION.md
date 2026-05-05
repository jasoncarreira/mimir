# Migration spec: `ClaudeSDKClient` for the agent loop

<!-- desc: design + staged rollout for replacing `query()` with ClaudeSDKClient in mimir/agent.py -->

**Status:** filed 2026-05-04. Not started. Cron-based quota poller is shipping
as the immediate quota-visibility fix; this migration is the long-term shape
for the agent's main loop.

## Motivation

`mimir.agent.Agent.run_turn` currently invokes the Claude Agent SDK via
`claude_agent_sdk.query(prompt, options)` — the one-shot, no-state form. Each
turn spawns (or reuses, via the SDK's internal pool) a Claude Code subprocess,
streams messages, and exits. This was the natural fit when mimir was designed:
each turn is a fresh, independent run; mimir owns all context assembly.

Three things have made the alternative — `ClaudeSDKClient`, the long-lived form
— more attractive:

1. **Quota visibility.** Anthropic's Max plan delivers `null` for utilization
   on stream-event `RateLimitInfo` (the SDK types `utilization: float | None`
   exactly because of this asymmetry). The full per-window picture lives behind
   `ClaudeSDKClient.get_context_usage()`, which is unreachable from `query()`.
   The cron-based quota poller (shipping alongside this spec) calls this method
   from a throwaway client; doing it from the *agent's* persistent client would
   give per-turn freshness and remove the polling overhead.
2. **Control-plane operations.** `ClaudeSDKClient` exposes `interrupt()`,
   `set_permission_mode()`, `set_model()`, `rewind_files()`, `stop_task()`,
   `toggle_mcp_server()`, `reconnect_mcp_server()`, `get_mcp_status()`. Several
   of these would unlock features mimir would benefit from — interrupting a
   runaway subagent, dynamically widening permissions for a privileged
   sub-skill, swapping models mid-conversation for cost reasons.
3. **Symmetry with saga.** Saga's `claude_code` provider already uses
   `ClaudeSDKClient` (saga/saga/_llm.py: `_PersistentClaudeCode`). The agent
   loop using `query()` while every other in-process LLM call uses the long-
   lived form is asymmetric and surprising.

## The previously-blocking concern: history bloat

The historical reason the agent loop used `query()` is that
`ClaudeSDKClient.query()` accumulates conversation history across calls within
a session. mimir's prompt model is "rebuild every turn from canonical context
blocks (core memory, recent activity, saga atoms, etc.)" — silent SDK history
retention would feed the model TWO views of the past (one curated by mimir,
one accumulated invisibly) and would be a debugging nightmare plus a subtle
prompt-injection vector.

**This concern is mitigated** by the SDK's `session_id` parameter:

```python
await client.query(prompt, session_id="default")  # accumulates
await client.query(prompt, session_id=ctx.turn_id)  # fresh per call
```

Each distinct `session_id` is a separate history thread inside the SDK daemon.
By passing a fresh `session_id=ctx.turn_id` per turn, mimir gets the warm
subprocess (no cold-start cost) while keeping each turn's reasoning
hermetically scoped.

## The remaining concern: session-store cleanup

The SDK's daemon retains session histories in an `InMemorySessionStore`
(plain dict keyed by `project_key/session_id`) until process exit. There's no
TTL, no LRU. Per-turn `session_id`s mean every turn's history sits in the
daemon's RAM forever — a real leak for a long-lived mimir process.

Two cleanup approaches:

### Option A — explicit session_store with per-turn delete

Pass `session_store=<custom>` via `ClaudeAgentOptions`. After each turn
completes, mimir calls `store.delete(SessionKey(project_key, ctx.turn_id))` to
drop just that turn. Precise; drops only what we know we're done with;
preserves the warm subprocess across thousands of turns.

**Cost:** ~30 lines of `SessionStore` plumbing (the SDK exports
`SessionStore` as a base class; mimir wraps `InMemorySessionStore` with a
forward-call shim that exposes `delete` to the agent loop). Plus careful
testing — a leak here wouldn't crash anything; it'd quietly eat RAM and
corrupt long-running benchmark stability.

### Option B — periodic disconnect+reconnect

Saga's `_PersistentClaudeCode` pattern: `_RECYCLE_AFTER_CALLS = 10` recycles
the client by disconnect + reconnect. Wipes the entire in-memory store.

**Cost:** simpler, but coarser. Each recycle costs ~1s of subprocess restart.
For mimir we'd want to recycle every 5-10 turns to bound bloat — at which
point the warm-subprocess win shrinks proportionally. The control-plane
operations (interrupt mid-turn, set_permission_mode) also need to be careful
about recycle timing, which adds state-management complexity.

**Recommended:** Option A. The precision is worth the plumbing for an
agent-loop change; the control-plane methods need a stable client lifecycle.

## Concurrency: per-thread client cache

`ClaudeSDKClient.query()` is single-threaded by design. Concurrent turns from
mimir's dispatcher would serialize on a shared client — a real latency hit
under multi-channel load.

The fix is the same `threading.local` pattern saga adopted (commit 54c5618):
each calling Python thread (each dispatcher worker, plus any scheduler-fired
turn that runs on its own thread) caches its own `ClaudeSDKClient`. Per-thread
cleanup happens at thread teardown (daemon thread + OS reaping on process
exit), so no explicit lifecycle management is needed for the worker pool.

**Resource cost:** ~50MB RAM per worker thread (one Claude Code subprocess
each). For mimir's 1-3 dispatcher workers under realistic Discord load, that's
~150MB. The Max plan quota is account-level, not per-subprocess — multiple
warm clients share one quota pool, so per-thread caching doesn't multiply
quota burn.

## Migration plan (staged)

### Stage 1 — agent.py uses ClaudeSDKClient with default session_id

**Goal:** prove the migration works without changing turn semantics.

- Replace `claude_agent_sdk.query(prompt, options)` with
  `client.query(prompt, session_id="default")` + iterate `client.receive_response()`.
- Single shared client per agent process (no per-thread cache yet).
- Use `session_id="default"` for now (history accumulates — same semantics as
  if we'd designed mimir around ClaudeSDKClient from the start, just temporarily).
- Disconnect on agent shutdown.

This is the "does it work at all?" stage. Run it through the full mimir test
suite + a smoke test against mimirbot. If turn behavior is identical to the
`query()` baseline, ship it. Don't migrate quota work yet.

**Success metric:** all 745 mimir tests pass; mimirbot runs a 10-turn Discord
session without behavior drift.

### Stage 2 — per-turn session_id

**Goal:** isolate turns from each other so history doesn't bleed across turns.

- Pass `session_id=ctx.turn_id` on every `client.query()` call.
- No cleanup yet — the leak is now real but bounded by uptime.
- Verify the SDK actually scopes history per session_id (write a test that
  confirms turn N+1's input doesn't see turn N's content via the SDK's session
  state).

**Success metric:** behavior identical to stage 1; new test confirms session
isolation.

### Stage 3 — explicit SessionStore + per-turn delete

**Goal:** eliminate the leak.

- Pass `session_store=MimirSessionStore()` via `ClaudeAgentOptions`.
- `MimirSessionStore` wraps `InMemorySessionStore` and exposes `delete()` to
  the agent.
- After each turn completes (in the `run_turn` cleanup phase), call
  `store.delete(SessionKey(project_key, ctx.turn_id))`.
- Test: assert the store size doesn't grow over 100 sequential turns.

**Success metric:** memory profile flat across a 1000-turn synthetic run
(matches `query()`'s memory profile within noise).

### Stage 4 — per-thread cache

**Goal:** eliminate concurrent-turn serialization.

- Replace the singleton client with a `threading.local` (mirroring saga's
  `_persistent_runner_local` shape).
- Each calling thread gets its own client + own session_store.
- Test: two concurrent turns on different channels don't block on each other.

**Success metric:** parallel-turn latency unchanged from a baseline of two
fire-and-forget queries.

### Stage 5 — wire `get_context_usage()` into the rate-limit store ✅

**Goal:** retire the cron-based quota poller.

- Each turn's cleanup phase calls `client.get_context_usage()` and writes
  `apiUsage` per-window data into `RateLimitStore`.
- Once landed, drop `mimir/quota_poller.py` and the scheduler entry.
- Self-state and Upcoming blocks render the fresh per-turn data with no
  changes (the renderer already handles non-null utilization).

**Landed:** `agent.get_context_usage()` wrapper reuses the same warm
ClaudeSDKClient as `query()` (matching options-fingerprint avoids a
recycle); `Agent._capture_plan_quota_from_client(options)` is called
from `run_turn` after the message loop, gated on
`running_on_claude_max()`. apiUsage parsing + `record_api_usage` live
in `mimir/rate_limits.py`. `mimir/quota_poller.py`,
`tests/test_quota_poller.py`, `Scheduler.add_quota_poll_job`, the
`quota_poll_cron` config field, and the server's poll registration
are removed. New tests in `test_agent_sdk_client.py` and
`test_rate_limits.py`.

**Success metric:** the RateLimitStore has populated `utilization` for both
`five_hour` and `seven_day_opus` after one turn. The cron poller is removable.

### Stage 6 — control-plane methods (opportunistic)

**Goal:** unlock features the migration enables.

- `interrupt()` — wired into the loop-detection circuit breaker so a runaway
  subagent can be cut off mid-turn instead of completing and being denied.
- `set_permission_mode()` — sub-skills that need elevated access (e.g.,
  the chainlink CLI shouldn't need bypassPermissions globally) can scope
  the change to their invocation.
- `set_model()` — heartbeat ticks could swap to haiku for the librarian
  protocol (cheap, fast) and back to sonnet for user-driven turns.

This stage is opportunistic — each item is a separate small win, independently
shippable.

## Test infrastructure

- A `_FakeClaudeSDKClient` test double under `tests/_fakes/` that records
  `query()` calls, supports per-session_id message accumulation, and lets
  tests assert on cleanup (e.g., "after run_turn, the store has no entry for
  this turn_id").
- New file `tests/test_agent_sdk_client.py` covering each migration stage's
  invariant.
- A long-running synthetic test under `tests/integration/` that runs 1000
  turns and asserts memory growth stays bounded — catches regressions in the
  cleanup path.

## Out of scope

- Resuming sessions from disk (file-backed `SessionStore`). mimir doesn't
  resume turns; turns are fresh by design.
- Cross-process session sharing (multiple mimir instances reading the same
  store). Each mimir process is its own session universe.
- Migrating saga's `_PersistentClaudeCode`. Saga's pattern works for its
  workload (high-volume, independent prompts); convergence with mimir's
  per-thread cache could happen later but isn't load-bearing here.

## Open questions

1. **Project key.** The SDK uses `project_key_for_directory(cwd)` to scope
   sessions. mimir's per-turn `cwd=config.home` means all turns share one
   project key — that's fine, the per-turn `session_id` does the isolation.
   Worth confirming by reading `_internal/sessions.py`.
2. **Hook compatibility.** mimir wires PreToolUse + PostToolUse hooks via
   `ClaudeAgentOptions`. ClaudeSDKClient should accept the same options shape
   (it's the same options dataclass), but worth verifying hooks fire on each
   `client.query()` call, not just the first.
3. **Streaming-event capture.** `RateLimitEvent` and `StreamEvent` come
   through `client.receive_response()` the same way `query()` yields them.
   Confirm the per-response `message_start` rate_limits block still arrives
   (it's the same daemon, same protocol).
4. **MCP server lifecycle.** `mcp_servers={"mimir": self._mcp_server}` is
   currently passed via `ClaudeAgentOptions` per turn. With ClaudeSDKClient,
   the MCP server is registered at `connect()` and persists across queries —
   any MCP server state that should reset per-turn needs an explicit reset
   via `reconnect_mcp_server()`. The mimir MCP server is currently stateless
   per-turn (per-turn context flows through arguments, not server state), so
   this should be a no-op, but worth a smoke test.

## Estimate

- Stage 1: ~80 lines + 2-3 tests. ~3 hours including SDK reading + smoke test.
- Stage 2: ~10 lines + 1 test. ~30 min.
- Stage 3: ~80 lines (MimirSessionStore wrapper) + 5 tests. ~4 hours.
- Stage 4: ~40 lines + 2 tests. ~2 hours.
- Stage 5: ~50 lines + retire poller. ~2 hours.
- Stage 6: per-feature; not estimated here.

Total stages 1-5: roughly **one focused day of work**. Each stage is
independently shippable and revertable if behavior diverges.
