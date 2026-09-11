# Poller Timing Audit: Issue 1616

Scope: implementation changes only in `tests/test_pollers.py`; this file records
the audit and executed evidence. No product changes remain. Read and followed
`CONTRIBUTING.md`. All mutation probes below were actual manual edits to
`mimir/pollers.py`, executed against pytest, and restored with `apply_patch`
immediately after the failing run. No commits were made.

## Product Trace

- `run_poller` exports the effective budget as `POLLER_TIMEOUT_SECONDS`, then
  starts a real shell in a new session with stdout/stderr pipes.
- Two `_drain_capped` tasks run under `asyncio.wait(..., timeout=timeout)`.
  Pending drains cause group termination, process reap, and completion of both
  drains, preserving already-written output.
- EOF is not process exit. After collecting the drains, `asyncio.wait_for`
  bounds `proc.wait()` by `min(POLLER_EXIT_GRACE_SECONDS, timeout)`.
- Overflow is handled before timeout recovery. It must fail the tick rather
  than dispatch a valid prefix of an overflowed stream.
- Timeout recovery trims the final unterminated record, increments the circuit
  failure count, and reports recovered events without reporting a nonzero exit.
- The stdout drain awaits `_accept_delivery_barrier` for complete records.
  That callback awaits durable logging and then receipt writing; an append
  exception returns without acknowledging the record. A completed callback is
  therefore a causal checkpoint for checking receipt presence or absence.
- `_cb_record_failure` sets a monotonic deadline on every failure at or above
  threshold. `run_poller` compares that deadline with its monotonic clock.

## Conversions

The wait controller replaces the `asyncio` binding in `mimir.pollers` only.
It does not patch the shared asyncio module or loop clock. Real children,
real capped drains, durable receipt I/O, parsing, and timeout handling remain.
Controlled subprocess protocols have one 60-second pytest ceiling, covering
startup, readiness, expiry, assertions, and cleanup. No readiness stage gets
its own deadline. Two-second product arguments remain two seconds; product
constants remain 120 seconds for execution, 5 seconds for exit grace, and
300 seconds for circuit backoff.

| Test (without `test_` prefix) | Old dependency | Controlled protocol |
| --- | --- | --- |
| `run_poller_timeout_kills_subprocess` (6 cases) | Complete records and optional tail had to be written before 2 seconds | Child flushes records and tail, publishes `output-ready`, and parks without a lifetime timer. Only then does the drain wait report expiry. A module-local frozen clock also makes the circuit deadline exact. |
| `run_poller_timeout_kills_child_holding_pipes` | Python and its grandchild had to start before 2 seconds; group teardown had a separate 5-second guard | Wait for the grandchild's own marker, group metadata, and the parent's flushed-output marker. Then expire the drain wait. Observe the actual group signal and wait for kernel teardown under the same whole-protocol ceiling. A missing product group signal triggers test-owned rescue, followed by a signal assertion failure, rather than hanging on inherited pipes. Rescue is not used on the passing path. |
| `run_poller_bounded_when_child_closes_pipes_but_keeps_running` (2 cases) | FD closure before 2 seconds, 30-second child lifetime, 15-second outer bound | Use `exec` so the shell cannot retain duplicate pipe FDs. Child closes both FDs, publishes readiness, and parks. Await real drain EOF without expiry; inject expiry at the reap wait. Assert the exact drain/reap calls and unchanged two-second budgets. |
| `delivery_barrier_is_acked_before_dispatch_phase_timeout` | Receipt and dispatch marker before the two-second kill, with a two-second receipt loop and ten-second dispatch sleep | Wait for the real barrier callback to finish and the child to observe its receipt and mark dispatch. Then expire. Missing receipt skips the marker wait and fails an assertion after cleanup. |
| `delivery_barrier_refuses_receipt_when_alert_append_fails` | One-second absence observation versus a two-second kill | Wait for completion of the callback that attempts the failing append, then expire and check the owned receipt directory after `run_poller` finishes. No absence-by-sleep assertion. |
| `run_poller_output_overflow_kills_and_fails` (4 cases) | Child startup before 2 seconds; `slow_drain` slept 2.1 seconds against 2 seconds | Run both real capped drains to completion, holding their results behind an event. Once both results are ready, release them and either report expiry immediately (before the held tasks resume) or await their completion. No timer orders overflow versus expiry. |
| `circuit_breaker_rearms_after_backoff_expiry` | Real-clock comparisons and direct rewriting of the stored deadline | Freeze only `pollers.time.monotonic`, preserve the first stored deadline, then advance the product clock past it. Assert the exact rearmed deadline. The asyncio clock stays real. |
| `run_poller_exports_its_effective_timeout` | Python startup/output before the fractional 0.5-second budget | Await real drain completion without a drain deadline. Retain the default, 45, and 0.5 budget values and subprocess environment assertions. This tests value propagation, not startup speed. |

An initial controlled EOF run exposed the shell's duplicate-FD problem: both
cases hit the whole-protocol ceiling before the `exec` correction. Those were
test-development failures, not mutation evidence. The corrected test proves the
reap branch is reached instead of accidentally covering drain expiry again.

## Executed Mutation Evidence

Every row below produced an assertion failure, not a pytest ceiling or shell
tool timeout. All parameter cases of every converted test were exercised.
Commands shared this prefix:

```sh
uv run --extra dev --extra bench pytest -q -n 6 tests/test_pollers.py
```

Append the listed selector and `--tb=short` to reproduce the probe command.
Mutations are described for evidence only; none remains in the working tree.

| Selector (`-k`) | Temporary product edit | Observed assertion evidence | Run time |
| --- | --- | --- | --- |
| `'timeout_kills_subprocess or timeout_kills_child_holding_pipes or is_acked_before_dispatch_phase_timeout'` | Set `timed_out = False` in the pending-drain branch | 8 failed: three recovery cases returned `0 != 3`; three zero-record cases, the group case, and the barrier case lacked the expected timeout event | 8.94 s |
| `bounded_when_child_closes` | Change the reap `wait_for` budget to `None` | Both cases failed the recorded-call assertion: `('reap', None) != ('reap', 2.0)` | 6.33 s |
| `refuses_receipt_when_alert_append_fails` | Write a receipt inside the barrier callback's exception handler before returning | Failed `receipts == []` with an actual receipt path despite the append failure | 15.30 s |
| `output_overflow_kills_and_fails` | Disable the `_overflow['hit']` handling branch | All 4 failed: normal-drain cases lacked `poller_output_overflow`; expiry cases wrongly dispatched a valid prefix (`1 != 0`) | 10.43 s |
| `rearms_after_backoff_expiry` | Change failure threshold comparison from `>=` to `==` | Failed exact rearm deadline: `400.0 != 401.0 + 300` | 12.98 s |
| `exports_its_effective_timeout` | Export `str(int(timeout))` instead of preserving the fraction | Failed `'cap=0.5' in 'cap=0'` | 10.29 s |
| `is_acked_before_dispatch_phase_timeout` | Omit the real barrier receipt write | Failed receipt-count assertion, `0 != 1`, after controlled timeout cleanup | 8.57 s |
| `timeout_kills_child_holding_pipes` | Replace POSIX `os.killpg` with `proc.kill()` | Failed `killpg.assert_any_call(child_pgid, SIGKILL)`; owned rescue prevented a pipe-holder hang | 5.86 s |

## Measurements And Surviving Guards

Final measurement command:

```sh
uv run --extra dev --extra bench pytest -q -n 6 tests/test_pollers.py \
  -k 'timeout_kills or bounded_when_child or delivery_barrier or output_overflow or rearms_after or exports_its_effective_timeout or reaps_subprocess_on_timeout or circuit_breaker_open' \
  --durations=0
```

Result: **19 passed in 14.15 seconds**. These are pytest call durations, including
the complete producer/consumer protocol, not isolated startup microbenchmarks.
Thus they are measured enclosing upper bounds on producer readiness, not claims
that the producer itself takes exactly that long or will always do so.

| Producer/protocol | Final measured call duration | Surviving guard or scheduling mechanism |
| --- | --- | --- |
| Child records/tail, six cases | 0.09-0.29 s | One 60 s protocol ceiling; 10 ms file-check cadence, not a stage bound |
| Grandchild startup, output, kill and teardown | 0.20 s | One 60 s protocol ceiling; 50 ms `/proc` rescan cadence, no independent 5 s deadline |
| FD closure, EOF, controlled reap, two cases | 0.12-0.28 s | One 60 s protocol ceiling; unchanged 2 s budget asserted at the controlled reap seam |
| Positive receipt and child dispatch observation | 0.21 s | One 60 s protocol ceiling; child's 10 ms receipt-check cadence has no expiry |
| Failed append and completed negative receipt check | 0.07 s | One 60 s protocol ceiling; no timed absence window |
| Overflow producer, both streams and both wait outcomes | 0.07-0.13 s | One 60 s protocol ceiling; event-controlled drain release, no 2.1 s sleep |
| Three effective-timeout export runs | 0.42 s total | One 60 s protocol ceiling; fractional drain budget is not a startup gate |
| Circuit rearm across expiry | 0.45 s | Logical monotonic advancement; existing 300 s whole-test ceiling and ordinary subprocess budgets |
| Unchanged `circuit_breaker_open_suppresses_subsequent_runs` | 0.27 s total | Real 300 s backoff. Producer is three tiny failing subprocesses followed by a suppressed run, not a sleep near backoff expiry. |
| Unchanged `run_poller_reaps_subprocess_on_timeout` | 1.05 s | Real 1 s execution timeout against a 120 s child sleep. No child output/readiness must arrive first: this is the retained real expiry smoke test. |

The controller delegates non-injected reap waits to real asyncio. In the
controlled drain-expiry tests, the product has already awaited process reap
before this second wait. In the overflow tests both actual drains have already
finished; in the export test EOF already makes the complete record recoverable
even if exit grace expires. These are not short child-startup gates.

Other normal subprocess tests retain the product defaults (120 s drain budget,
5 s exit grace) and the repository's 300 s test ceiling. The scoped file's
slowest measured call in the earlier all-file duration run was 4.43 s, for
`github_activity_repo_read_and_scratch_write_scopes_are_separate`; that includes
more than just its producer. Neither those defaults nor the backoff constant
was increased. Remaining sleeps were reviewed with a file-scoped search:
10/50 ms polling cadences and the retained 120 s timeout-smoke producer above.

## Validation

- Final requested scoped command:
  `uv run --extra dev --extra bench pytest -q -n 6 tests/test_pollers.py`:
  **308 passed**, 3 warnings, **17.44 s**.
- Final full suite:
  `uv run --extra dev --extra bench pytest -q -n 6`:
  **15,772 passed, 48 skipped**, 100 warnings, **252.73 s**.
- An earlier full run had 15,770 passes, 48 skips, and two subprocess-ceiling
  failures outside this file:
  `test_env_isolation.py::test_coding_state_tests_ignore_the_host_override`
  and `test_worklink_backends.py::test_opencode_test_env_reaches_pytest_through_bash[False]`.
  They were not changed here or diagnosed as flaky; both passed in the final
  integrated rerun. Other contributors were concurrently editing other tests,
  so these results are workspace integration evidence, not an isolated base
  comparison or a causal diagnosis of those earlier failures.
- `git diff --check -- tests/test_pollers.py mimir/pollers.py` passed.
  `git diff -- mimir/pollers.py` was empty after all mutation probes.
- All requested conversions and per-conversion mutation checks are complete.
  No product budget changes, final product edits, commits, or changes to other
  contributors' files were made by this task.
