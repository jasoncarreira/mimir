# Issue 1616: Hosted and Kernel Timing Audit

## Status and Scope

The assigned timing audit is complete in `test_acp_hosted.py`,
`test_acp_python_kernel.py`, and `test_acp_unconfined_fallback.py`: the first pass
converted eight test functions (21 cases), and the follow-up converted another
24 functions (35 cases, including existing consumers of the bounded helpers).
All originally reported gaps, including the remaining helper bounds and timeout
tests, are addressed. This is not a claim about other agents' issue-1616 files.
Read CONTRIBUTING.md before editing. The conversions from `88056ea44` and
`3885ec64d` are excluded; neither commit touches these three files.

No product changes or product constant changes remain. Temporary mutations were
made only in `mimir/acp/hosted.py` and `mimir/acp/python_kernel.py`, and each was
restored with apply_patch immediately after its failing run. No commit was made.

## Bounds and Producer Traces

Every converted test has a single effective `pytest.mark.timeout(120)` whole-test ceiling,
including setup and teardown. This is a deadlock/safety ceiling, not a behavioral
assertion or a fresh allowance per read. Polling yields only wait for a specific
owned producer; the polling interval is not evidence that an operation finished.
Asyncio substitutions are module-local namespaces, not changes to the real event
loop or the shared asyncio module.

Measurements in the first table are call durations, rounded by pytest, from the
first-pass three-file `-n6 --durations=0` run. They are observations, not guarantees about
arbitrary machine load, and are not used as assertion thresholds. The slowest
converted case took 0.92 s in that run; 120 s gives substantial diagnostic room
without using small scheduling margins to establish correctness.

| Test (prefix `test_`) | Removed Timing Assumption | Producer Trace and Preserved Assertion | Measured Call |
| --- | --- | --- | --- |
| `shell_and_python_default_timeout_is_60_seconds` | Deadline minus a timestamp before spawn, approximately 60 +/- 1 s | Actual hosted clock read -> actual deadline argument -> exact difference 60; Python execution receives 60 independently | 0.01 s |
| `timeout_comes_only_from_selected_profile` | Deadline minus pre-spawn timestamp, approximately 7 +/- 1 s | Actual hosted clock read -> deadline -> exact difference 7; environment override ignored, Python receives 7, per-call override rejected | 0.02 s |
| `queue_wait_is_outside_timeout_and_other_session_is_parallel` | 5 s blocker, 1.1 s sleep, elapsed > 1 and < 2, 3 s child rendezvous | Child publishes entered -> real state registers waiter while lock held -> owned clock advances 1000 to 1002 -> queued task has made no deadline clock read -> release -> first queued clock read is 1002 -> value 2 without timeout. Separate real workers each publish a marker and wait for the other's marker before returning 1/2 | 0.82 s |
| `queued_and_active_cancellation_have_distinct_worker_effects` | 0.05/0.25 s scheduling race and 10 s sleeper | Warm worker -> active child entered marker -> observed lock waiter -> cancel queued -> waiter count zero and same live worker -> release -> retained value 4 -> second child entered marker -> active cancellation -> no processes -> fresh next call | 0.91 s |
| `late_background_output_is_discarded_between_calls` | 0.1 s child sleep / 0.2 s parent sleep | First response -> parent release marker -> background print actually flushes -> child records stdout's device identity -> assert devnull -> second call outputs only current. This additionally detects a missing stdout reset that the old next-call-only assertion missed | 0.35 s |
| `deadline_expires_during_spawn_handshake_and_output_setup[spawn]` | 0.01 s deadline could fire before fake spawn ran | Deadline 1060 from owned clock 1000 + 60 -> fake spawn is entered -> clock 1061 and real timeout rescheduled to expire -> blocked producer canceled -> exact timeout result, no processes | 0.02 s |
| `deadline_expires_during_spawn_handshake_and_output_setup[handshake]` | 1 s deadline could race actual subprocess creation | Real spawn completes and process is owned -> receive hook asserts one owned process -> trace handshake -> clock 1061 / real timeout expiry -> exact timeout result, process removed | 0.04 s |
| `deadline_expires_during_spawn_handshake_and_output_setup[output]` | Synchronous 0.03 s sleep competing with 0.01 s deadline | Warm real worker -> real output file creation twice -> owned clock advanced beyond deadline -> send/response still receive original 1060 deadline -> real expired timeout -> exact timeout result, no processes | 0.22 s |
| `bounded_wait_retains_ownership_until_eventual_direct_reap` | 0.01 s reap timeout and elapsed < 0.2 s | Real process -> delayed wait entered -> real timeout cancellation of shield -> terminate returns while reaper pending and ownership retained -> explicit release -> direct reap -> ownership removed. Asserted trace is wait, expiry, returned, reaped | 0.01 s |
| `adopted_kernel_warning_uses_this_calls_mode` (12 cases) | 1 s execution deadline also covered cross-mode respawn | Real initial worker -> release/adoption or policy-driven respawn. Timeout cases suspend deadlines until the new/reused child publishes executing, assert the worker's current mode, then expire the real response timeout. All cases check this call's warning; crash and timeout also check distinct kernel outcomes | 0.23-0.92 s |

The queue's 1-second budget is retained as a logical budget: advancing two seconds
while queued cannot consume it. Its module-local timeout adapter expires only
when that owned clock has reached a deadline; it does not reintroduce a one-second
wall-clock race for response delivery. Setup-expiry tests similarly retain the
same deadline across phases. The actual Timeout context performs cancellation;
the tests do not substitute a fabricated timeout result.

## Actual Mutation Evidence

Commands used `uv run --extra dev --extra bench pytest -q` with the named node(s).
Each outcome below was observed in this session, not inferred from source. Every
failure was an assertion failure, not the 120-second ceiling.

| Converted Test(s) | Temporary Product Mutation | Observed Failure |
| --- | --- | --- |
| Both hosted configuration tests | `_confined_shell`: add 2 to computed deadline | 2 failed in 1.00 s: `[62.0] != [60]` and `[9.0] != [7]` |
| Queue/parallel | Move `loop.time() + timeout` before acquiring the state lock | `queued_reads == []` failed with `[1000.0]`; final adapter version reconfirmed, 1 failed in 2.66 s |
| Queued/active cancellation | Remove waiter decrement from lock-acquisition exception handler | `state.waiters == 0` failed with 1; 1 failed in 1.00 s |
| Late output | Remove worker's `os.dup2(saved_stdout, 1)` reset | Child destination assertion failed, `'False' != 'True'`; 1 failed in 1.35 s |
| All three setup-expiry phases | `_timeout_result`: label kernel `crashed` instead of `timed_out` | All phase traces passed, then exact result assertions failed on kernel classification; 3 failed in 1.21 s |
| Bounded direct reap | Remove `asyncio.shield` around reaper | Ownership assertion failed: delayed process absent from `_processes` before release; 1 failed in 1.10 s |
| All 12 adoption cases, including startup | Invert the unconfined-mode predicates for error prefix and result warning | All 12 warning-count assertions failed (0 instead of 1, or 1 instead of 0); 12 failed in 7.62 s |

## Verification

Final scoped command:

```sh
uv run --extra dev --extra bench pytest -q -n6 tests/test_acp_hosted.py tests/test_acp_python_kernel.py tests/test_acp_unconfined_fallback.py --durations=0
```

Final follow-up result with durations: **130 passed in 3.95 s**. A repeat of the
exact requested command without `--durations=0` passed **130 tests in 4.71 s**.
The first-pass results were
130 passed in 17.19 s and 130 passed in 15.05 s. Each converted test also passed
targeted runs before mutation testing. No full suite was run in the follow-up,
as explicitly requested to avoid other agents' concurrent product mutations.

Historical first-pass full-suite command (not rerun in the follow-up):

```sh
uv run --extra dev --extra bench pytest -q -n6
```

Result: **1 failed, 15771 passed, 48 skipped, 100 warnings in 895.05 s**.
The failure was
`tests/test_pollers.py::test_delivery_barrier_refuses_receipt_when_alert_append_fails`:
its receipts list was nonempty despite the simulated durable append failure.
That file has another agent's concurrent edits and was not modified here.
An isolated rerun passed (1 passed in 11.65 s), which does not establish that the
full-suite failure was harmless or flaky. No unmodified-base comparison was run;
the cause is unassigned. The full run preceded the final queue timeout-adapter
refinement; the final scoped run and repeated queue mutation cover that refinement.
**The integrated suite is not established green.**

`git diff --check` passed. Product diffs were checked empty after restoration.
Other agents' changes elsewhere in the working tree were left untouched.

## Follow-Up Producer Traces

These measurements come from the final 130-test, six-worker run (3.95 s total).
The largest converted call was capacity eviction at 0.58 s; the 120-second
ceiling is over 200 times that observed duration. This is measured diagnostic
headroom, not a guarantee about arbitrary load. There are no elapsed-time
assertions, approximate deadline differences, finite child sleeps used to force
ordering, or separate short per-read safety allowances left in the three files.

The kernel file now uses a module-level 120-second pytest timeout, so helper
polling and cleanup share the caller's whole-test budget. Existing explicit
120-second marks resolve to the same effective ceiling, not additional timers.
Hosted/fallback conversions use individual 120-second marks. Short sleeps that
remain either poll an explicit marker or block indefinitely until cancellation;
none claim that elapsed time implies readiness.

| Test (prefix `test_`) | Removed Gap and Actual Producer Trace | Measured Call |
| --- | --- | --- |
| `shell_and_python_cleanup_kill_owned_process_groups` | Removed 2 s ownership poll and 30 s sleeper. The real provider inserts its process into an observed owned map -> event -> assert leader live -> close -> both engines reaped and untracked | 0.13 s |
| `shell_deadline_kills_pipe_holding_owned_grandchild` | Removed 5 s identity poll / 1 s startup competition / 30 s child lifetime. Child flushes held output -> atomic PID/PGID publication -> parent observes captured bytes and shell exit -> call still pending on descendant pipes -> explicit real Timeout expiry -> exact timeout output and descendant stopped | 0.04 s |
| `shell_cancel_and_close_kill_pipe_holding_owned_grandchild` (2) | Atomic child identity -> shell leader exited while call still pending -> cancellation or close -> exact MCP cancellation error -> descendant stopped and map empty. No child self-exit deadline or local stop-poll bound | 0.06-0.07 s |
| `shell_double_cancellation_reaps_without_signalling_killed_group_again` | Removed two 1 s wait_for margins. Owned fake process publishes running -> first cancellation -> publishes reaping -> second cancellation -> exact single signal, returncode and empty tracking sets | <0.005 s |
| `live_control_eof_is_killed_without_waiting_for_worker` | Removed 2 s cold-start race / 10 s child lifetime. Real cold handshake recorded -> actual socket EOF recorded while the owned leader is still live -> crash cleanup kills it -> exact -9 crash result and empty map. Execution timers are unarmed; only the whole-test ceiling bounds a missing EOF | 0.11 s |
| `worker_and_idle_task_are_lazy` | Removed .2/.05 s ordering sleeps. Child publishes entered and waits for release -> idle task absent -> real second waiter registered -> both calls pending -> release -> ordered namespace results -> idle task armed | 0.07 s |
| `idle_retirement_discards_worker_and_next_call_is_fresh` | Removed .05 s idle constant / 2 s poll. Real idle task publishes its requested sleep -> verify original 1800 s delay -> owned clock advances by 1800 -> release and await that exact task -> state absent, worker stopped -> fresh namespace | 0.15 s |
| `registered_waiter_wins_idle_retirement_race` | Removed .1 s constant and .2/.02/.15 s sleeps. Initial idle sleeper observed -> real active execution cancels it -> await canceled idle task -> child entered -> registered waiter -> release -> correct reused results -> observe new idle sleeper -> advance 1800 and await retirement -> fresh next call | 0.14 s |
| `real_waiter_registered_at_idle_expiry_keeps_worker` | Removed .1 s constant / .15 s sleep. Hold state lock -> real waiter count one -> advance clock and release observed idle sleep -> await retirement task's completed decision -> same live worker -> unlock -> reused value 9 | 0.05 s |
| `timeout_and_crash_discard_namespace` | Removed 30 s sleeper / 3 s execution race. Warm worker -> assignment and entered marker -> explicit expiry of real response timeout -> exact 3-second timeout result -> fresh namespace -> actual crash and fresh next call | 0.25 s |
| `timeout_and_crash_retain_streams_exactly` | Both actual os.write calls precede entered marker -> explicit expiry -> assert timeout classification and retained bytes -> actual crash -> exact crash streams/result | 0.17 s |
| `direct_exit_still_kills_owned_process_group_descendant` | Removed 5 s identity margin and finite descendant lifetime. Atomic child PID publication -> worker exits 37 only after publication -> result -> read complete PID -> assert exact exit and stopped descendant under whole-test ceiling | 0.08 s |
| `sessions_parallel_and_namespaces_isolated` | Removed two .2 s sleeps. Each real project worker publishes entered and blocks on its own release -> both markers observed while both calls pending -> release both -> separate namespace values and keys | 0.08 s |
| `other_session_cannot_execute_kill_or_release_owned_cwd` (6) | Removed _appears' 5 s allowance and call's 2 s allowance. Active variants publish entered -> intruder execute/kill/release refused -> owner/worker unchanged -> release active execution -> reused value. Idle variants retain the same authority assertions | 0.06-0.12 s |
| `control_command_waiting_admission_is_rejected_when_close_begins` | Removed outer 5 s margin; preserved observed admission waiter -> close flag set while close waits admission -> release -> exact rejection and completed close | <0.005 s |
| `execution_waiting_state_lock_is_rejected_when_close_begins` | Removed outer 5 s margin; preserved real registered state-lock waiter -> close held at admission -> closed flag observed -> state lock released -> rejection without spawning, waiter/lock cleanup -> close completes | <0.005 s |
| `capacity_evicts_least_active_detached_kernel_and_descendant` | Removed 60 s child lifetime, _stopped's 5 s windows and group-disappearance 5 s window. Spawn and verify descendant group -> set explicit LRU ages -> actual eviction -> mapping/worker ownership assertions -> direct/descendant exit and group disappearance -> other kernels reused | 0.58 s |
| `operator_listing_release_and_kill` | Consumer of formerly 5 s _stopped. Exact listing -> release/adopt -> kill -> empty state/process maps -> direct PID confirmed stopped -> fresh next call | 0.16 s |
| `parent_output_reads_retained_inode_not_worker_symlink` (3) | Removed 1 s completion race and 30 s timeout sleeper. Real output paths replaced by secret symlinks -> timeout branch publishes entered only afterward -> explicit expiry. Success/crash/timeout each assert empty retained outputs, no secret and correct distinct kernel status | 0.06-0.08 s |
| `directory_substituted_for_output_never_retains_kernel_lock` | Removed separate 5 s operation and cleanup bounds. Actual directory substitution -> completed response -> descriptor/lock assertions -> next value 42 -> actual retire -> closed descriptor verified | 0.06 s |
| `kernel_signal_denial_requires_confirmed_process_exit` (2) | Removed .01 s confirmation constant. Process.wait publishes entered -> live case explicitly expires real timeout and preserves original denial; exited case completes before signal-confirmation assertions. Production constants unchanged | <0.005 s |
| `kernel_repeated_cancel_does_not_repeat_successful_signal` | Disabled competition with production's 5 s reap wait in this cancellation test. Owned reaper publishes entered -> explicit cancellation -> signalled flag retained -> release reaper -> second terminate -> exactly one signal | <0.005 s |
| `every_unconfined_python_result_labels_mode` (3) | Removed 1 s completion race / 30 s sleeper. Exception and crash complete naturally; timeout worker publishes entered -> explicit real timeout expiry. All branches verify warning, failure and distinct reused/crashed/timed_out statuses | 0.06-0.08 s |
| `unconfined_shell_failure_and_timeout_label_mode` | Removed 1 s child startup race / 30 s sleeper. Real shell failure checked -> real next child publishes entered -> explicit shell timeout expiry -> -1 and unconfined warning | 0.02 s |

The `_stopped` process-exit-during-read unit test itself was not a timing
conversion: it still makes an immediate owned mocked /proc read and returns.
Its helper's consumers are covered above. Unchanged tests receiving the new
module safety mark are not counted as new behavioral conversions.

## Follow-Up Mutation Evidence

Every command used `uv run --extra dev --extra bench pytest -q -n6` with the
listed test node(s) or a matching `-k` selector and `--tb=short`. These are actual
observed failures. Each mutation was restored with apply_patch before starting
the next mutation or any green verification run. No mutation hit a safety ceiling.

| Converted Tests | Temporary Product Mutation | Observed Assertion Failure and Run Time |
| --- | --- | --- |
| All 5 hosted readiness/descendant/double-cancellation cases | Remove `_terminate_process`'s process-map pop | All five asserted nonempty `_processes` after actual cleanup; 5 failed in 1.20 s |
| Cold EOF | `_crashed` labels result `timed_out` | Producer trace passed, kernel classification assertion failed; 1 failed in 1.69 s |
| Idle retirement and registered-waiter race | Remove idle retirement's `_kernels.pop` | Both awaited actual idle task completion, then asserted lingering state should be absent; 2 failed in 5.67 s |
| Waiter at idle expiry | Label reused execution `fresh` | After expiry decision preserved the worker, reused-kernel assertion failed; 1 failed in 7.03 s |
| Lazy worker/idle | Return immediately from `_arm_idle` | Ordered executions completed, idle task was None; 1 failed in 1.49 s |
| Both timeout/crash tests | `_timeout_result` labels timeout `crashed` | Exact result / kernel assertion failed after readiness and explicit expiry; 2 failed in 2.27 s |
| All 6 intruder authority cases | Label reused execution `fresh` | All six failed the owning session's reused-kernel assertion; 6 failed in 3.20 s |
| Parallel namespace isolation and directory substitution | Append `broken` to returned expression values | `['1broken', '2broken'] != ['1', '2']` and `'42broken' != '42'`; 2 failed in 1.58 s |
| Direct-exit descendant | Report exit code 0 instead of actual code in crash exception | Exact code-37 exception assertion failed; 1 failed in 4.04 s |
| Both close/admission tests | Set `_closed = False` when close begins | Both failed closed-flag assertion at witnessed admission barrier; 2 failed in 2.08 s |
| Capacity eviction and operator kill | Remove `_reap`'s process-map pop | Evicted process remained tracked / post-kill process map nonempty; 2 failed in 1.88 s |
| All 3 retained-inode variants | Read worker-controlled output path instead of retained descriptor | Actual fixture secret appeared in results in all three variants; 3 failed in 1.09 s |
| Confirmed-exit denial and repeated cancellation | Set worker signalled flag False after successful/confirmed signal | Both failed `assert worker.signalled`; 2 failed in 1.06 s |
| Live signal denial | Replace original denied exception after confirmation expiry | Original-exception identity assertion failed; 1 failed in 2.80 s |
| All 3 unconfined Python result modes and shell result | Invert kernel/hosted unconfined warning predicates | All four warning assertions failed; 4 failed in 2.05 s |
| Unconfined shell, additional timeout-path witness | Return -2 instead of -1 on timeout | Initial failure branch passed; post-readiness, post-expiry timeout exit-code assertion failed; 1 failed in 1.92 s |

## Completion Boundary

No reported timing conversion remains blocked or deferred in the three owned
test files. All product mutations are restored, product diffs are empty, and
`git diff --check` passes. Other agents' files were not changed. No commit made.
The historical full-suite failure is still not certified resolved; per the
follow-up instruction, validation here is scoped only, not an integrated-suite
claim.
