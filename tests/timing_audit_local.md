# Issue 1616: Local Timing Audit

## Scope and Verification

Read `CONTRIBUTING.md`. Changes are limited to the twelve requested test files
and this report. No final product changes, commits, or changes to other agents'
work. Temporary mutations were applied and restored with `apply_patch`, only in
the corresponding product modules. No mutations to `server.py`, ACP, or Worklink
worker modules.

Per the explicit task instruction, ran scoped suites only while other agents
were running concurrent mutations. This is **not** a full-suite claim.

Final uninstrumented verification (2026-09-11, Python 3.11.15, six xdist workers):

```sh
uv run --extra dev --extra bench pytest -q -n6 \
  tests/test_agent.py tests/test_concurrency.py tests/test_event_logger.py \
  tests/test_history.py tests/test_jsonl_snapshot.py tests/test_loop_watchdog.py \
  tests/test_post_turn_hooks_wiring.py tests/test_spawn_caps.py \
  tests/test_worklink_claims.py tests/test_discord_bridge.py \
  tests/test_slack_bridge.py tests/test_web_ui.py --durations=20
```

Result: **644 passed, 18 warnings, 71.88 seconds**. Warnings were SWIG import
deprecations. An earlier producer-instrumented run also passed all 644 tests in
90.90 seconds; the preceding uninstrumented run passed in 97.13 seconds.
Initial scoped subsets passed 498 and 146 tests respectively.
`git diff --check` passed.

## Conversions

Blocking concurrency conversions have a 30-second whole-test pytest-timeout
ceiling using the repository's signal method. The ceiling is a deadlock guard,
not evidence of a regression. Pure clock/mtime tests retain the repository's
300-second whole-test ceiling. Tests release their gates and join/await their
owned work in `finally` blocks.

| File | Conversion and producer boundary |
| --- | --- |
| `test_agent.py` | Continuation thread waits on an explicit release event rather than sleeping 200 ms. A module-local `asyncio` wrapper arms the real 10 ms continuation timeout after the thread signals entry, excluding executor startup from the experiment. Cleanup releases and observes the thread's exit. The partial-tool-events timeout cases also use an unreleased async event instead of a ten-second fake stall. |
| `test_concurrency.py` | Hold runners until all twenty channel workers have attempted the real global semaphore. Assert exactly five are in flight before release, then check FIFO and final peak. A cap bypass signals from the twentieth runner so the failure reaches an assertion. No 10 ms overlap window. |
| `test_event_logger.py` | The thread test observes a failed nonblocking acquisition of the real, already-held I/O lock. The process test receives `blocked` through a pipe only after a real nonblocking flock fails while trim is paused at rename. A bypass reports completion instead, failing the assertion. No 400/200 ms negative waits or five-second process-start readiness assumption. The child stays gated until trim is joined, so parent scheduling does not consume the product lock-retry budget. |
| `test_history.py` | Disk workers no longer self-release after two seconds. Assert both are still inside the gate under their counter lock; release and await both even on assertion failure. A separate positive-readiness diagnostic bound detects serialization before the whole-test ceiling. |
| `test_jsonl_snapshot.py` | Replace both TTL sleeps with a module-local monotonic clock. Verify the old contents before expiry and new contents after expiry. The unchanged-mtime case also verifies the refreshed TTL deadline so skipping the expired-cache path cannot pass vacuously. |
| `test_loop_watchdog.py` | Observe completion of two real off-loop checks at an explicit stale-heartbeat time. Stop and join that thread before asserting exactly one alert/event. No 150 ms settling delay, and no dependence on when the thread first runs. |
| `test_post_turn_hooks_wiring.py` | Advance each edited file's mtime explicitly with nanosecond `os.utime`, including generated output. The exception test additionally proves its throwing hook was invoked. No filesystem timestamp sleeps. |
| `test_spawn_caps.py` | Observe the second acquisition attempt on the actual guard semaphore, after all preceding checkout/Git work. Do not infer readiness from a downstream contained runner or a 50 ms delay. The first runner is explicitly held; a bypass wakes the observer from the second runner and fails the peak assertion. |
| `test_worklink_claims.py` | Hold the winner in the fake claim runner until the loser's real flock attempt reaches the injected contention sleeper. Release the loser only after the winner finishes. A lock bypass signals overlap directly. No 50 ms overlap window. |
| `test_discord_bridge.py` | Fifth client-start entry signals retry progress. Await the bridge's own outstanding background log tasks, not arbitrary loop turns. The disconnect test observes entry into a gated backoff sleep and asserts cancellation reached that sleep. The `asyncio` patch is module-local. |
| `test_slack_bridge.py` | Fifth handler-start entry signals retry progress; await this bridge's owned log tasks before checking attempts 3 and 4. No fixed retry polling or twenty-turn log drain. |
| `test_web_ui.py` | The real reader wrapper signals the second scan via `call_soon_threadsafe`, carrying the actual `since` argument. Await that signal instead of polling a call count. |

## Mutation Evidence

These were actual temporary source changes, not hypothetical failures or mocks
substituting for a product mutation. Each converted test was exercised against
its relevant mutation. All failures below were assertions, **not pytest-timeout
ceilings**. Restored all mutations before the final green run.

First batch: 16 assertion failures in 13.93 seconds. Generated-output batch:
one assertion failure in 5.52 seconds. Partial-evidence batch: four assertion
failures in 12.39 seconds. History was then rechecked with its original
two-second positive-readiness bound: assertion failure in 2.00 seconds (session
3.15 seconds). Its conversion does not increase that budget. Total: **21 distinct
failing cases across 18 converted test functions**. Durations below are pytest
call-phase durations, not producer
measurements; producer measurements are recorded separately below.

| Test (file as above) | Temporary product mutation | Observed assertion failure | Call time |
| --- | --- | --- | --- |
| `test_budget_continuation_timeout_logs_failure_and_still_runs_finalize_hooks` | `agent.py`: replace timeout diagnostic with `continuation completed` | Timeout error no longer starts with `timed out after` | 0.10 s |
| `test_failed_turn_preserves_partial_tool_events` (native/internal, exception/timeout) | `agent.py`: set `events_partial=False` | `record.events_partial is True` | 0.07-0.22 s |
| `test_twenty_channels_at_max_five_drain_in_per_channel_order` | `dispatcher.py`: use a new 100-slot semaphore per call | In-flight count 20 instead of 5 | 0.02 s |
| `test_log_sync_holds_io_lock` | `event_logger.py`: replace shared I/O lock with a fresh lock | No attempted acquisition of held lock: `[] != [False]` | <0.005 s |
| `test_process_append_survives_trim_rename_window` | `event_logger.py`: omit append's process-lock acquisition | Pipe receives `finished`, not `blocked` | 0.29 s |
| `test_concurrent_appends_run_in_parallel_on_thread_pool` | `history.py`: serialize `to_thread` with a per-buffer async lock | `seen_two` is unset; cleanup still releases the first worker | 2.00 s |
| `test_records_re_reads_after_ttl_expiry_when_mtime_changes` | `jsonl_snapshot.py`: treat any populated cache as unexpired | `[1] != [2, 1]` after clock advance | <0.005 s |
| `test_records_skips_re_read_when_mtime_unchanged` | Same permanently-unexpired cache mutation | Old deadline 100.05 instead of refreshed 100.11 | <0.005 s |
| `test_sustained_stall_fires_structured_alert_once_off_loop` | `loop_watchdog.py`: suppress `_notify` | Zero alerts instead of one after two completed checks | 0.01 s |
| `test_wiki_backlinks_fires_on_edit` | `agent.py`: snapshot constant zero mtimes | Hook awaited zero times instead of once | 0.01 s |
| `test_wiki_backlinks_swallow_exceptions` | Same constant-mtime mutation | Throwing hook's call list is empty | <0.005 s |
| `test_wiki_backlinks_ignores_generated_outputs` | `agent.py`: stop excluding generated output filenames | Hook unexpectedly awaited once | 0.01 s |
| `test_tool_semaphore_bounds_concurrent_contained_spawns` | `tools/registry.py`: use a new 100-slot semaphore instead of the guard | Peak 2 instead of 1 | 0.10 s |
| `test_simultaneous_claims_are_serialized_and_both_succeed` | `worklink/claims.py`: omit nonblocking flock acquisition | `overlap_detected` is true | 0.01 s |
| `test_supervisor_fires_algedonic_after_three_attempts` | `bridges/discord.py`: suppress transient retry notifications | Zero retry events instead of two | 0.03 s |
| `test_disconnect_cancels_supervisor_cleanly` | `bridges/discord.py`: skip supervisor cancel/await branch | Gated backoff never received cancellation | <0.005 s |
| `test_slack_supervisor_fires_algedonic_after_three_attempts` | `bridges/slack.py`: suppress transient retry notifications | Zero retry events instead of two | 0.02 s |
| `test_scoped_live_events_poll_advances_past_filtered_records` | `web_ui.py`: advance to the last delivered, rather than last scanned, cursor | Second scan uses Alice's cursor instead of Bob's | 0.02 s |

The first batch used the twelve-file command above with `-k` selecting its
sixteen tests and `--tb=short --durations=20`. The two additional batches used
the same `uv run --extra dev --extra bench pytest -q -n6` prefix, selecting
`test_post_turn_hooks_wiring.py -k wiki_backlinks_ignores_generated_outputs`
and `test_agent.py -k test_failed_turn_preserves_partial_tool_events`.

## Retained Waits

Measured in the passing six-worker, twelve-file run with a temporary pytest
plugin embedded in the allowed `test_event_logger.py`, loaded using
`-p tests.test_event_logger`. It wrapped direct test `asyncio.wait_for` calls,
the history `to_thread(Event.wait)` boundary, Discord's typing predicate helper,
and `BaseProcess.join`, recording `perf_counter` elapsed time through pytest
reports. A second two-test scoped probe measured history with its original
two-second bound and the durable logger's actual `_acquire_process_lock` call:
both passed in 1.41 seconds. All probes were removed with `apply_patch` before
final verification.

These are **single-run observed waiter latencies**, not intrinsic producer CPU
times, load guarantees, or percentile estimates. They include scheduling and,
for typing predicates, polling granularity. Measurements use the producer being
awaited, not the duration of a downstream spawn/execute stub or total test time.

| Retained wait | Actual producer | Budget | Observed elapsed |
| --- | --- | --- | --- |
| History concurrent append readiness | Both `_append_disk` workers reaching the unreleased gate | 2 s positive diagnostic bound | 0.000807 s |
| Logger process cleanup | Spawned writer exiting after trim join and retry release | 5 s join, then terminate/reap on failure | 0.023495 s |
| Durable logger lock timeout | `_acquire_process_lock` attempting an independently-held descriptor | 0.05 s product timeout | 0.050960 s |
| Agent continuation timeout | Already-started continuation worker | 0.01 s product timeout | 0.012086 s until timeout; worker release cleanup 0.000195 s |
| Agent injection setup readiness | `_BarrierSaga` signalling entry, not a later model/tool spawn | 1 s | 0.000811 s |
| Agent injection completion | Owned `Agent.run_turn` after SAGA release | 2 s | 0.020575 s |
| Agent queued-followup drain | `Queue.join` after the agent has processed the followup | 1 s | 0.000041 s |
| Agent hung finalize hook | `Agent.run_turn`, including real two-second finalize cancellation | 15 s outer guard | 2.015580 s |
| Discord typing entry/exit/replacement/failure | Fake typing context or the owned hold task becoming done | 1 s predicate deadline, 0.01 s polling interval | 0.010176-0.012693 s |
| Discord natural typing cap | Owned hold task exiting after its real 0.05 s cap | 1 s predicate deadline | 0.052568 s |
| Discord connection readiness | Fake `client.start` entry event | 1 s | 0.000069 s |
| Discord connection completion | Owned supervisor after fake client release | 1 s | 0.000648 s |
| Discord login failure/clean exit | Owned supervisor completion | 1 s | 0.000199-0.000528 s |
| Discord transient retries/cap/old-client close | Owned supervisor completion, including configured retry sleeps | 2 s | 0.005624-0.010660 s |
| Slack connection readiness | Fake `handler.start_async` entry event | 1 s | 0.000034 s |
| Slack connection completion | Owned supervisor after fake handler release | 1 s | 0.000691 s |
| Slack invalid-auth/missing-scope/clean exit | Owned supervisor completion | 1 s | 0.000043-0.002274 s |
| Slack retries/auth-refresh/auth-skip/handler-close | Owned supervisor completion, including configured retry sleeps | 2 s | 0.004817-0.011549 s |
| Web SSE frame reads | Local HTTP response `StreamReader.readline` | 2 s per line | 0.000014-0.000095 s |

These waits test positive completion or an intentional product timeout. None
uses expiry of a short negative wait as evidence of exclusion. In particular,
history's worker-release event cannot expire even if its separate readiness
diagnostic does. The child writer's five-second **cleanup** join is not its
readiness protocol; readiness is the pipe message from the attempted flock.

Other retained scheduling constructs are not elapsed-time margins:

- The unit semaphore test's `sleep(0)` yields to a directly-created coroutine
  whose next operation is acquisition of the real semaphore, with no preceding
  Git, thread, network, or executor dependency. Its first holder stays gated.
- Discord/Slack redelivery fakes use `sleep(0)` to yield intentionally; their
  callers await both deliveries with `gather`.
- Existing bridge runner-retention tests yield once after awaiting their owned
  runner, allowing its already-scheduled done callback to remove it from the
  bridge-owned set. Backoff-cap tests use `sleep(0)` as a fake sleeper, not as a
  delay that proves readiness.
- The existing Discord message-to-typing smoke test uses five cooperative loop
  turns through an entirely fake client/context path. The context entry has no
  real I/O or thread prerequisite. The timed typing predicate measurements above
  separately cover this same hold-task/context producer.
- JSONL's thread-safety test uses a real eight-party barrier and joins all
  workers. Explicit release events in fake SAGA, queue admission, and bridge
  connection tests remain producer-controlled. Claims retry-budget tests already
  use injected sleepers and explicit lock release, not wall-clock backoff.
- The durable logger timeout test retains its real 0.05 s product lock timeout
  against an independently-held descriptor. The holder has no timer and cannot
  self-release; the expected result is a `TimeoutError`, not a negative sleep.

No remaining wall-clock sleep is used to manufacture contention in the audited
tests. The final suite still spends most of its time in the unrelated bounded
tool-evidence tests (56.95 s and 8.56 s), not in the converted coordination.
