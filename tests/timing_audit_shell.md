# Issue 1616: Shell Timing Audit

## Scope and Protocol

Read `CONTRIBUTING.md` and `AGENTS.md`. This change edits only the four requested
test modules and this explicitly requested audit. Temporary product mutations
were applied and restored with `apply_patch`; no product constants were changed.
Other agents' worktree changes were left untouched. No commit was created.

The requested shell timing inventory is now complete: the initial ten tests plus
28 additional test functions (29 parametrized test cases) are converted and
mutation-validated. The follow-up changed only `test_shell_jobs.py` and this
audit. The other three owned test modules retain their first-pass conversions.

The converted tests rely on the existing pytest-timeout 300-second POSIX-signal
ceiling for the entire test, including startup, protocol, and cleanup. No new
per-stage timeout margins were added. The two bounded-runner tests pass
`timeout=None`, which reaches the existing `Popen.wait` implementation without
an execution deadline. Product drainer-join bounds remain unchanged.

`_held_job`, local to `test_shell_jobs.py`, creates two FIFOs. The real Python
child opens its release FIFO, writes a readiness byte, and blocks reading a
release byte. The test checks actual process liveness before yielding. Cleanup
releases the child, waits for process exit and the registry completion callback,
and asserts exit status and pipe closure before defensive descriptor cleanup.
This replaces finite child lifetimes, not just their numeric durations.

Cancellation readiness and exit polling now wait for state under the whole-test
ceiling. Their 50ms polling intervals are observation cadence, not lifetime or
pass/fail windows. Readiness also fails immediately if the owned child exits.
The SIGTERM-ignore test requests a SIGUSR1 acknowledgment after sending SIGTERM;
the child blocks SIGUSR1 before announcing readiness and consumes it with
`sigwait`, avoiding a lost-signal race. It then remains blocked until SIGKILL.

## Converted Tests and Executed Mutations

Every row below was actually executed with temporary product source changes.
All failures were behavioral assertions or `pytest.raises` failures, not pytest
hang-ceiling failures. Product sources were restored after each batch.

| Test | Conversion | Temporary product mutation | Observed failure |
| --- | --- | --- | --- |
| `test_running_jobs_visible_immediately` | Replace `sleep 2` with ready/release FIFO child. | Remove `visible.append(job)` for running jobs. | `assert any(...)` is false. |
| `test_shell_job_snapshots_running_scope_filters` | FIFO-held running child; completed sibling uses callback without a stage deadline. | Dispatch running snapshots to `registry.all_jobs()`. | Finished job ID unexpectedly appears in `running_ids`. |
| `test_evict_stale_preserves_running_job` | Replace `sleep 60` and kill-only cleanup with FIFO lifecycle. | Remove finished-state eligibility and age jobs using `finished_at or started_at`. | `assert len(evicted) == 0` observes `1`. |
| `test_spawn_refuses_beyond_live_job_cap` | Hold both admitted children by FIFO; rejected probe is finite and awaited if mistakenly admitted. | Disable the live-job cap condition. | `DID NOT RAISE RuntimeError`. |
| `test_finished_jobs_release_all_pipe_descriptors` | Hold ten children through capture of all 20 owned pipe identities; release and assert closure; remove isolated process's sleep and count deadlines. | Remove both drainer and waiter `stream.close()` calls. | `_held_job` observes `stdout.closed == False` before defensive cleanup; all ten contexts unwind. |
| `test_session_leader_isolation_pid_equals_pgid` | Replace finite sleep with signal wait; remove readiness/exit deadlines; direct-PID cleanup. | Set `start_new_session=False`. | Child PID differs from PGID (`119246 != 116495` in final batch). |
| `test_sigterm_to_process_group_kills_grandchildren` | Parent and child block on signals; assert both group identities before group kill; lifecycle-based death checks and cleanup. | Set `start_new_session=False`. | Group identity assertion observes `116495 != 119235`. |
| `test_sigkill_fallback_when_sigterm_ignored` | Replace 60s child lifetime and 0.3s survival sleep with readiness and acknowledgment protocol; assert SIGKILL exit status. | Set `start_new_session=False`. | PGID assertion observes `116495 != 119247`. |
| `test_project_test_capture_discards_output_past_hard_byte_cap` | Remove 5s execution deadline; measure flushed producer bytes and actual process exit; own cleanup. | Replace remaining capture budget with `len(chunk)`. | Captured length is `1000000`, expected `65536`. |
| `test_bounded_runner_stops_oversize_output_during_execution` | Remove 5s execution deadline; record and check actual producer process termination; own cleanup. | Disable overflow detection in the drainer. | `DID NOT RAISE FactoryContractError`. |

Final eight-test mutation batch: **8 failed in 7.53s**. It selected all three
cancellation tests, visibility, snapshots, live cap, and both output tests using
`uv run --extra dev --extra bench pytest -q -n 6` with explicit node IDs.
Running-job eviction was tested separately: **1 failed in 3.58s**. FD closure was
tested separately after its conversion: **1 failed in 4.57s**. Earlier batches
also confirmed the corresponding failures (5 failures in 5.02s, cancellation's
3 failures in 1.15s, eviction's 1 failure in 2.42s).

The cancellation mutation checks the product's session-containment contract,
not a mutation of the child program's signal handler. No claim is made that this
single mutation exhausts every possible cancellation regression.

## Measured Lifecycles and Producers

These measurements are owned per-test observations, not an ambient process or
thread census. They are asserted by the passing tests.

| Test/group | Observed lifecycle or production result |
| --- | --- |
| Four live-job tests | Five held children total; each observed running at readiness, then `poll() == 0` after release; zero surviving held children and both owned pipe objects closed at completion. The snapshots test additionally waits for its finished sibling's successful completion. |
| FD test | Ten simultaneously held children, 20 distinct pipe identities. All ten exit successfully; closure asserted before defensive cleanup; zero matching pipe identities remain in the subsequent FD scan. |
| Session leader | One child observed alive at readiness; zero live owned PIDs after cleanup. |
| SIGTERM tree | Parent and one grandchild alive before signal; parent exit status `-SIGTERM`; zero live owned PIDs afterward. On Linux `_pid_alive` excludes zombies; this is not a claim that container PID 1 reaped every zombie. |
| SIGKILL fallback | One child executes acknowledgment after SIGTERM; exit status `-SIGKILL`; zero live owned PIDs afterward. |
| Shell output cap | One real producer, successful exit, child-reported **1,000,000 flushed bytes**, **65,536 retained bytes**, zero surviving producers before cleanup. Mutant retained all 1,000,000 bytes. |
| Factory overflow | One real producer and zero surviving producers before cleanup. Configured write size is 1,000,000 bytes and output limit is 1,024 bytes. Actual bytes written before termination are **not measured**; do not confuse requested output size with completed production. |

Grandchild and remaining lifecycle measurements appear in the follow-up section
below. No claim is made about every process in the full suite.

## First-Pass Verification

Requested scoped command:

```sh
uv run --extra dev --extra bench pytest -q -n 6 tests/test_shell_jobs.py tests/test_spawn_cancellation.py tests/test_shell_exec.py tests/test_worklink_feature_factory.py
```

After all ten conversions: **202 passed, 18 warnings in 18.76s**.
The FD test alone passed before its mutation: **1 passed in 10.68s**.
`git diff --check` passed for the four test files. Explicit `git diff --exit-code`
checks of the three mutated product files confirmed restoration.

The first full-suite attempt was interrupted by the tool's 120s command limit
at approximately 36%; it is not passing evidence. The subsequent completed run
reported **1 failed, 15,771 passed, 48 skipped in 820.68s**. Its sole failure was
the then-unconverted FD test: `ValueError: I/O operation on closed file` while
capturing pipe identities after a child's 0.2-second lifetime expired. That
failure prompted the FD conversion and mutation check documented above.

Full-suite rerun after the FD conversion, using
`uv run --extra dev --extra bench pytest -q -n 6`: **15,772 passed, 48 skipped,
100 warnings in 281.82s**. Both suites were green for the first-pass revision. This run
includes concurrent agents' changes elsewhere in the shared tree; those files
were not edited as part of this audit.

## Follow-Up Protocols

- `_wait_until_done` no longer polls a wall-clock deadline. It waits for the real
  process, joins threads whose exact names belong to that job ID, and asserts no
  surviving owned threads, a reaped process, matching published/process exit
  status, and closed pipes. Enumeration is scoped to the owned names; no ambient
  thread-count delta is asserted. Since spawn starts every job thread before
  returning, an owned thread already absent from enumeration has terminated.
- The seven real-Popen Event tests now join the lifecycle and then assert the
  callback Event is set. Missing callbacks therefore fail an assertion after
  waiter termination, rather than waiting for the 300-second ceiling. The
  callback-error test's separate 30-second thread join is removed too.
- The two fake-Popen redaction tests also lose their 10-second stage waits.
  Explicit release/resume Events continue to order their fake streams.
- The short-job visibility test sets its duration to 0.1 seconds in job state,
  not by running a 0.1-second child. Other visibility and eviction checks derive
  their injected `now` from `finished_at`, not how recently the runner scheduled
  the assertion. The write-failure test evicts at an explicit logical time
  instead of aging shared state while the sweeper can race it.
- Scheduled eviction uses a fresh real `_RegistrySweeper` thread with a manual
  condition/tick queue and a test clock. It registers and completes one real
  Python job before advancing the clock by the production eviction interval.
  One tick runs the real sweep and reaches its next wait before assertions.
  There is no second spawn, no 0.05-second policy patch, and no polling deadline.
  A stop token exits and joins the owned sweeper in cleanup.
- Eager spawn eviction disables only the background sweeper for that test and
  advances the test clock after the old job's lifecycle ends. A background
  sweep cannot accidentally satisfy its assertion when eager eviction breaks.
- The pipe-holding grandchild forks from the real registry child, publishes its
  PID through a FIFO, and blocks on a release FIFO. The direct parent exits
  immediately. The normal product drain-join window is **unchanged at 10s**:
  bounded joins really execute while the grandchild retains stdout/stderr.
  The test observes join arguments only in this job's waiter. For an unbounded
  request, the observer records `None` and returns instead of hanging; the test
  then fails its explicit bounded-join assertion. This interception is solely
  a mutation oracle, not a replacement for the normal bounded wait.
- The grandchild must still be alive when successful job completion and pipe
  closure are asserted. Cleanup releases it, joins the captured owned job
  threads, and waits for the exact descendant to exit. Procfs distinguishes a
  dead zombie from a live survivor, so this test explicitly requires procfs.
  The 50ms exit-observation cadence is not a lifetime or failure threshold.

## Follow-Up Mutation Evidence

All commands below ran with `uv run --extra dev --extra bench pytest -q -n 6
--tb=short`. Every listed mutation was an actual temporary product edit made and
restored with `apply_patch`; no product changes remain.

### Published Exit Status

Mutation: `_waiter` publishes `job.exit_code = rc + 1`. Running all of
`tests/test_shell_jobs.py` produced **28 failed, 19 passed in 19.38s**. Twenty-five
failures are the full set of current `_wait_until_done` callers below. Each
failed the lifecycle assertion `job.exit_code == process.returncode`, after the
owned process and threads had finished. The nonzero cases observed `8 != 7` and
`4 != 3`; all other rows observed `1 != 0`.

The additional three failures were the grandchild's direct exit-status
assertion and the already-converted FD and snapshots tests' exit assertions.

| Affected test | Real processes completed in normal run | Live process/thread survivors at helper return |
| --- | ---: | --- |
| `test_undeclared_output_is_unchanged` | 1 | 0 / 0 |
| `test_redacted_capture_error_does_not_log_values` | 1 | 0 / 0 |
| `test_redacted_callback_error_does_not_log_values` | 1 | 0 / 0 |
| `test_spawn_captures_stdout_and_stderr` | 1 | 0 / 0 |
| `test_output_write_failure_keeps_draining_and_completes` | 2 | 0 / 0 |
| `test_output_write_bound_discards_excess_without_wedging` | 1 | 0 / 0 |
| `test_nonzero_exit_marked_exited_error` | 1 | 0 / 0 |
| `test_on_complete_fires_after_exit` | 1 | 0 / 0 |
| `test_on_complete_error_isolated_from_registry` | 2 | 0 / 0 |
| `test_on_complete_runs_for_nonzero_exit` | 1 | 0 / 0 |
| `test_spawn_captures_channel_id` | 1 | 0 / 0 |
| `test_env_overlay_sets_value_visible_to_child` | 1 | 0 / 0 |
| `test_env_overlay_none_unsets_inherited_var` | 1 | 0 / 0 |
| `test_env_overlay_default_inherits_parent_env` | 1 | 0 / 0 |
| `test_cwd_kwarg_honored_by_subprocess` | 1 | 0 / 0 |
| `test_short_finished_job_not_visible_after_grace` | 1 | 0 / 0 |
| `test_finished_job_persists_in_all_jobs_after_grace` | 1 | 0 / 0 |
| `test_read_output_supports_stream_filter` | 1 | 0 / 0 |
| `test_read_output_tail_lines_truncates` | 1 | 0 / 0 |
| `test_read_output_marker_fires_when_file_fits_one_chunk_but_has_extra_lines` | 1 | 0 / 0 |
| `test_read_output_no_marker_when_file_has_fewer_lines_than_tail` | 1 | 0 / 0 |
| `test_evict_stale_removes_old_finished_job_and_unlinks_files` | 1 | 0 / 0 |
| `test_evict_stale_preserves_recently_finished_job` | 1 | 0 / 0 |
| `test_scheduled_eviction_does_not_require_later_spawn` | 1 | 0 / 0 |
| `test_spawn_triggers_eviction_of_old_jobs` | 2 | 0 / 0 |

These are 28 real processes across 25 tests, not a suite-wide PID census.
Closed stdout/stderr pipe objects are also asserted at each helper return.
The one global production sweeper is not counted as a per-job thread survivor.

### Missing Callbacks

After restoring the exit-status mutation, replacing `on_complete(job)` with
`pass` produced **7 failed in 2.74s** using this selection:

```sh
uv run --extra dev --extra bench pytest -q -n 6 --tb=short tests/test_shell_jobs.py -k 'redacted_capture_error or redacted_callback_error or output_write_failure or output_write_bound or on_complete'
```

All seven affected real-Popen Event tests failed `event.is_set()` (named
`completed`, `invoked`, `finished`, or `fired` in individual tests). Specifically:
capture error, callback error, write failure, output bound, callback after exit,
callback error isolation, and callback on nonzero exit. None waited for a
deadline or failed on the hang ceiling.

### Redaction, Grandchild, and Sweeps

After restoring callbacks, this selection produced **6 failed in 2.49s**:

```sh
uv run --extra dev --extra bench pytest -q -n 6 --tb=short tests/test_shell_jobs.py -k 'redaction_precedes or running_output_withholds or backgrounded_grandchild or scheduled_eviction or spawn_triggers_eviction'
```

| Test | Actual temporary product mutation | Observed assertion failure |
| --- | --- | --- |
| `test_redaction_precedes_capture_and_tail_limits[10485760]` | Disable the drainer's redactor branch. | Unredacted `prefix` differs from `[REDACTED]` in captured bytes. |
| `test_redaction_precedes_capture_and_tail_limits[25]` | Same redactor mutation. | `header\nprefix\nprivate-suf` differs from the redacted 25-byte prefix. |
| `test_running_output_withholds_split_secret` | Same redactor mutation. | Running tail is `private`, expected empty. |
| `test_backgrounded_grandchild_does_not_block_waiter` | Replace `thread.join(timeout=remaining)` with `thread.join()`. | `waiter requested unbounded drain joins: [None, None]`. |
| `test_scheduled_eviction_does_not_require_later_spawn` | Skip `registry._evict_stale()` in `_RegistrySweeper._run`. | `registry.get(job.job_id)` is still the job after the completed tick. |
| `test_spawn_triggers_eviction_of_old_jobs` | Skip `self._evict_stale()` at the start of spawn. | `old job must be evicted by spawn()`; old record still present. |

All four product mutations in that six-case batch were restored. The grandchild's release and lifecycle cleanup
executed even on the failing unbounded-join assertion, with no survivor or
cleanup assertion failures. The two redaction tests use fake Popen objects and
therefore produce zero real children.

## Follow-Up Measurements

Normal passing runs assert the following additional producer and survivor
measurements, beyond the per-test table and the original measurements above:

| Component | Measured result |
| --- | --- |
| Write-failure producer | Child records the sum of successful `os.write` results: **262,144 stdout bytes**. It and the subsequent slot-reuse child exit 0; zero owned live processes/threads at helper return. |
| Write-cap producer | Child records **262,144 stdout bytes and 262,144 stderr bytes**; files retain **8,192 bytes each**. Exit 0, zero owned live processes/threads at helper return. |
| Pipe grandchild | Direct parent exit 0 while **one descendant remains live** at the completion assertion. After FIFO release: **zero live descendants and zero surviving captured job threads**. Linux zombies are dead, not claimed to have been reaped by PID 1. |
| Scheduled sweep | One completed/reaped producer; **one owned sweeper thread** running before the tick and **zero after stop/join**. Job record and both output files absent after the completed sweep tick. |

The grandchild adds two real processes, bringing follow-up normal-run coverage
to **30 real processes across 29 test cases**. The original ten cases cover 22
additional real processes. No elapsed-time race or process-wide survivor count
is used as a success oracle.

## Final Scoped Gate

The first scoped run after the follow-up conversions was **202 passed, 18
warnings in 14.55s**. The final post-mutation scoped rerun was **202 passed, 18
warnings in 15.80s**, using the four-file command recorded above.
`git diff --check` passed, and `git diff --exit-code` confirmed all three
previously mutated product files were fully restored.

The full suite is intentionally **not rerun for this follow-up**, per the user's
instruction; the user owns the final integrated gate. The full-suite results
above apply only to the earlier first-pass revision, not this final revision.

## Remaining Policy Checks

No requested timing conversion remains open. The remaining real `time.time()`
uses in the shell-job tests construct deliberately old filesystem timestamps or
a far-future eviction argument while a running child is FIFO-held; neither is
a short startup deadline or sleep window.

The feature-factory capability-contract test's `timeout == 5` assertion and its
injected `TimeoutExpired(..., 5)` hazard are intentionally unchanged: they inspect
or simulate product policy without waiting for a real five-second subprocess.
