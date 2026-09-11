# Issue 1616: Git Timing Audit

Scope: `tests/test_git_tracking.py`, `tests/test_repo_tools.py`, and this
requested evidence file. No retained product changes or relocated timeout
constants. Read `CONTRIBUTING.md` and `AGENTS.md`. Measurements below were
obtained on Linux, Python 3.11.15, using this checkout's `uv` environment with
both `dev` and `bench` extras, on 2026-09-11. Other agents were working in this
checkout; their changes were neither reverted nor included in this work.

## Protocol Changes

The existing pytest-timeout **300-second whole-test signal ceiling** covers Git
startup, all task/event joins, assertions, and teardown. The converted Git tests
no longer have independent 2/3-second `wait_for` budgets. No elapsed-time margin
is an assertion. Real Git still runs wherever it ran before.

`retry_gate` wraps the real `_retry_push`: it announces entry through an owned
queue, waits on a fresh release event for each attempt, then calls the real
worker with zero backoff. A retry cannot finish and remove its task reference
before the test inspects it. Success/exhaustion tests explicitly release each
attempt; scheduling/cancellation tests keep the worker parked until cancellation.
The production retry-count tuple is unchanged. Tests still assert exactly three
exhausted attempts, not a count derived from the implementation.

Debounce reset uses a controlled worker with entry and release synchronization
instead of relying on a five-second debounce staying pending across real Git.
Both cancellation tests assert `Task.cancelling()` before joining the task, so
omitting cancellation produces an assertion failure instead of a hang. The
existing completion and no-event assertions remain. The lock-scheduling spy
parks its dummy task on an event rather than `sleep(10)` and joins cancellation.

The synthetic pytest child no longer sleeps three seconds hoping its 0.1-second
faulthandler dump wins the race. It preserves pytest's actual
`dump_traceback_later` arguments but defers arming until the test frame and the
worker frame exist. The worker announces entry; the test writes 5,000 noise
bytes, arms the real dump, and blocks on stdin. The parent drains stderr until
both named stack frames are observed, then releases stdin. The child releases
and joins its worker and exits normally. All raw-dump, truncation, frame-name,
return-code, and non-disclosure assertions remain.

The child helper has one **240-second monotonic deadline** spanning spawn,
dump observation, stdin release, output collection, and normal reap. `select`
and `communicate` consume the remaining time, not fresh per-stage budgets.
Exceptional cleanup kills and reaps the owned child. The outer test retains
pytest's 300-second ceiling. The helper's existing fast-child caller also uses
this deadline; its source and assertions are unchanged.

The live-output test waits for its owned `output_written` event without the
one-second startup margin. Its runner remains blocked on `release_runner` while
the test inspects the actual files and asserts the execution task is unfinished.
Output limits, path identity, captured content, and final verdict assertions
remain unchanged.

## Complete Conversion Inventory

Original line numbers refer to the pre-edit files. Timing columns are pytest
`--durations=0` call-phase seconds, not invented per-stage measurements. The
surviving ceiling for every row is the shared 300-second whole-test ceiling;
the synthetic-child row additionally has the single 240-second child deadline.
The passing samples are from the 37.13-second scoped run concurrent with a full
suite. Mutation times come from the actual failing mutation runs below.

| Test | Original timing | Conversion | Passing call (s) | Mutation / failing call (s) |
| --- | --- | --- | ---: | --- |
| `test_commit_and_schedule_push` | 202: 2s task wait | Direct join | 0.47 | A1 / 0.09 |
| `test_debounced_push_pulls_rebase_before_push` | 319: 3s task wait | Direct join; pull-before-push assertion retained | 0.76 | A2 / 0.14 |
| `test_debounced_push_aborts_rebase_and_skips_push_on_pull_failure` | 387: 3s task wait | Direct join; abort/skip assertions retained | 0.51 | B1 / 0.09 |
| `test_debounce_coalesces_burst_to_single_push` | 460: 3s controlled-worker wait | Direct join; existing release event retained | 0.75 | B2 / 0.05 |
| `test_debounce_reset_cancels_prior_task` | 473: 5s pending hold | Worker entry/release events; explicit cancellation assertion | 0.35 | B2 / 0.02 |
| `test_push_timeout_logs_git_push_failed` | 759: 2s task wait | Direct join; injected product timeout unchanged | 0.25 | A3 / 0.04 |
| `test_push_success_emits_git_push_ok` | 830: 2s task wait | Direct join | 0.27 | A2 / 0.05 |
| `test_no_remote_skips_push_silently` | 878: 2s task wait | Direct join | 0.16 | B3 / 0.07 |
| `test_push_failure_schedules_retry` | 1226: 5/10/20s holds; 1240: 2s wait | Gated retry entry and direct debounce join | 0.28 | B4 / 0.05 |
| `test_debounced_push_schedules_retry_under_home_lock` | 1281: `sleep(10)`; 1292: 2s wait | Event-held dummy retry; direct join and cancellation reap | 0.05 | C1 / 0.01 |
| `test_retry_success_emits_git_push_ok` | 1306: finite backoffs; 1338/1343: separate 2s waits | Entry/release for real retry; direct joins | 0.23 | A4 / 0.04 |
| `test_retry_exhaustion_emits_git_push_stale` | 1358: finite backoffs; 1373/1383: repeated 2s waits | Release each real retry; direct joins; no swallowed stage timeout | 0.46 | A5 / 0.06 |
| `test_new_commit_cancels_retry` | 1403: 10/20/40s holds; 1415: 2s wait | Retry entry gate; explicit cancellation assertion and join | 0.52 | C2 / 0.05 |
| `test_project_test_retains_builtin_hang_dump_after_stderr_truncation` | 2014-2070: child 3s sleep vs 0.1s dump; 30s subprocess cap | Observed real dump releases child; one child deadline | 1.29 | D1 / 0.72 |
| `test_project_test_hang_is_observable_before_runner_completes` | 2171: 1s event wait downstream of Git | Direct event wait under whole-test ceiling | 0.02 | D2 / 0.02 |

## Executed Product Mutations

All edits and restorations used `apply_patch`. These were actual temporary source
changes, not mutations of test assertions. None changed an authorization,
credential, containment, or other security guard. Each named conversion failed
an assertion, not the child deadline, pytest ceiling, or an asyncio timeout.
Mutations were grouped into four runs and restored between groups, except C2's
cancellation omission was carried forward from group B. The normal scoped and
full runs below occurred after all these mutations were restored.

| ID | Temporary product mutation | Observed assertion trace |
| --- | --- | --- |
| A1 | `mimir/git_tracking.py`, `_debounced_push` GitError branch: rename emitted `git_push_failed` to `mutation_push_failed` | `test_commit_and_schedule_push`: `assert len(push_failures) == 1`; `E assert 0 == 1` |
| A2 | Same worker's success emit: rename `git_push_ok` to `mutation_push_ok` | `test_debounced_push_pulls_rebase_before_push`: `assert [e for e in events if e["type"] == "git_push_ok"]`; `E assert []`. `test_push_success_emits_git_push_ok`: `assert len(push_oks) == 1`; `E assert 0 == 1` |
| A3 | Same worker's TimeoutError event: `reason="timeout"` becomes `reason="mutation"` | `test_push_timeout_logs_git_push_failed`: `assert push_failures[0]["reason"] == "timeout"`; `E AssertionError: assert 'mutation' == 'timeout'` |
| A4 | `_retry_push` success metadata: `via="retry"` becomes `via="mutation"` | `test_retry_success_emits_git_push_ok`: `assert ok_events[0]["via"] == "retry"`; `E AssertionError: assert 'mutation' == 'retry'` |
| A5 | `_retry_push` stale event: `attempts=next_attempt + 1` instead of `next_attempt` | `test_retry_exhaustion_emits_git_push_stale`: `assert stale_events[0]["attempts"] == 3`; `E assert 4 == 3` |
| B1 | `_sync_remote_before_push`: rename emitted `git_pull_blocked` to `mutation_pull_blocked` | `test_debounced_push_aborts_rebase_and_skips_push_on_pull_failure`: `assert blocked and blocked[0]["turn_id"] == "t2"`; `E assert ([])` |
| B2 | `_schedule_debounced_push`: replace `existing.cancel()` with `pass` | `test_debounce_coalesces_burst_to_single_push`: `assert completed_pushes == [("t4", home_repo)]`; actual contains t0 through t4, four extra entries. `test_debounce_reset_cancels_prior_task`: `assert first_task.cancelling()`; `E AssertionError: assert 0`, task pending on the controlled event |
| B3 | `_debounced_push` no-origin branch: emit `git_push_failed` before returning (remote check unchanged) | `test_no_remote_skips_push_silently`: `assert push_failures == []`; actual contains one `git_push_failed` for t1 |
| B4 | `_schedule_push_retry_locked`: insert immediate `return` | `test_push_failure_schedules_retry`: `assert retry_task is not None`; `E assert None is not None` |
| C1 | `_debounced_push` GitError branch: call `_schedule_push_retry_locked` outside the home-lock context | `test_debounced_push_schedules_retry_under_home_lock`: `assert observed == [True]`; `E assert [False] == [True]` |
| C2 | `_schedule_debounced_push`: replace `existing_retry.cancel()` with `pass` | `test_new_commit_cancels_retry`: `assert retry_task.cancelling()`; `E AssertionError: assert 0`, real retry wrapper pending on its release event |
| D1 | `mimir/project_tests.py`, `_safe_stderr_output`: use the scrubbed prefix instead of the dump-marker slice, leaving scrubbing intact | `test_project_test_retains_builtin_hang_dump_after_stderr_truncation`: `assert result.stderr.startswith("Timeout (0:00:00.100000)!")`; `E AssertionError: assert False`, stderr is the noise prefix |
| D2 | `RepoProjectTests.execute`: timeout result code becomes `mutation_test_timeout` | `test_project_test_hang_is_observable_before_runner_completes`: `assert result.code == "test_timeout"`; `E AssertionError: assert 'mutation_test_timeout' == 'test_timeout'` |

Exact mutation run commands (the expected nonzero exits were inspected):

```sh
uv run --extra dev --extra bench pytest -q -n 6 tests/test_git_tracking.py -k 'test_commit_and_schedule_push or test_debounced_push_pulls_rebase_before_push or test_push_timeout_logs_git_push_failed or test_push_success_emits_git_push_ok or test_retry_success_emits_git_push_ok or test_retry_exhaustion_emits_git_push_stale' --tb=short --durations=0
# A: 6 failed in 1.41s, all assertion failures.

uv run --extra dev --extra bench pytest -q -n 6 tests/test_git_tracking.py -k 'test_push_failure_schedules_retry or test_debounce_reset_cancels_prior_task or test_debounce_coalesces_burst_to_single_push or test_no_remote_skips_push_silently or test_debounced_push_aborts_rebase_and_skips_push_on_pull_failure' --tb=short --durations=0
# B: 5 failed in 1.22s, all assertion failures.

uv run --extra dev --extra bench pytest -q -n 6 tests/test_git_tracking.py -k 'test_new_commit_cancels_retry or test_debounced_push_schedules_retry_under_home_lock' --tb=short --durations=0
# C: 2 failed in 1.19s, all assertion failures.

uv run --extra dev --extra bench pytest -q -n 6 tests/test_repo_tools.py -k 'test_project_test_retains_builtin_hang_dump_after_stderr_truncation or test_project_test_hang_is_observable_before_runner_completes' --tb=short --durations=0
# D: 2 failed, 18 warnings in 3.85s, both assertion failures.
```

## Surviving Bounds

- Whole test: existing 300s signal ceiling, not moved or raised. Largest measured
  converted call in the contended scoped sample: 1.29s. This is observed evidence,
  not a promise of a scheduler margin or a new pass criterion.
- Synthetic child: one 240s monotonic deadline. The complete owning test call
  measured 1.29s under concurrent suite load; the unchanged fast-child caller
  measured 0.99s. No separate phase stopwatch was added. The 0.1s diagnostic
  threshold remains the exercised behavior, but no finite child sleep competes
  against it. The actual raw dump and post-truncation frame assertions passed.
- Product Git command limits remain 10s (ordinary commands) and 30s (push/sync);
  tests no longer impose shorter 2/3s completion margins around those operations.
  No product deadline was modified. Those individual subprocess durations were
  not instrumented; the inventory reports end-to-end call durations instead.
- Short 0.01/0.02/0.05s debounce stimuli still trigger real workers, but no test
  requires work to finish before a multiple of that delay. Retry waits that
  participate in pending-state assertions are now events. The retry exhaustion
  loop retains its existing five-iteration structural bound and exact three-
  attempt assertion; the bound is not a wall-clock budget.
- Existing zero-second scheduler yields in coalescing/cancellation tests remain
  yields, not finite pending holds. The unrelated empty-porcelain test's 0.1s
  post-no-task observation and the unrelated real index-lock retry test were
  not converted in this scoped change.

## Verification Runs

```sh
uv run --extra dev --extra bench pytest -q -n 6 tests/test_git_tracking.py tests/test_repo_tools.py --durations=20
# Initial conversion: 255 passed, 1 skipped, 18 warnings in 9.35s.

uv run --extra dev --extra bench pytest -q -n 6 tests/test_git_tracking.py tests/test_repo_tools.py --durations=0
# Final code, all own mutations restored, concurrent full-suite load:
# 255 passed, 1 skipped, 18 warnings in 37.13s.

uv run --extra dev --extra bench pytest -q -n 6 --tb=short --durations=20
# First attempt was terminated by the tool's 120s command limit around 33%.
# This is not a completed suite and is not counted as success.
# Second attempt, with a 900s tool limit:
# 18 failed, 15754 passed, 48 skipped, 100 warnings in 645.10s.
# Third attempt, after product diffs were observed clear:
# 15772 passed, 48 skipped, 100 warnings in 506.02s.
```

The failed completed full run overlapped concurrent work in other agents' files.
None of its failures were in the two files owned here. It reported nine poller
timeout assertions, one shell-exec capture-cap assertion, three shell-job
assertions, three process-group assertions, one feature-factory output-bound
assertion, and the server ACP test's `wait_for(channel_queue.join(), 10.0)`
TimeoutError. Earlier status output showed concurrent product edits in
`shell_jobs.py`, `tools/extra.py`, and `worklink/backends/feature_factory.py`.
No clean-base causal comparison was performed for those out-of-scope failures;
they are not labeled flaky or blamed on these conversions. No outside files
were edited to obtain the subsequent green full run.

`git diff --check` passed. After restoration, `git diff --name-only -- mimir
pyproject.toml uv.lock` was empty. No commit was made.
