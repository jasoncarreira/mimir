# Issue 1616: Worker Timing Audit

## Scope and Status

Only `test_worklink_worker_exec.py`, `test_worklink_worker_parity.py`, and
`test_worklink_factory_supervisor.py` were changed, plus this audit. CONTRIBUTING
was read first. Temporary product mutations were confined to
`mimir/worklink/worker_exec.py` and `mimir/worklink/factory_supervisor.py`, applied
and restored with `apply_patch`. No product changes remain. No commits were made.
`mimir/worklink/worker_process.py` does not exist in this checkout; the process
protocol implementation is in `worker_client.py`, which was not mutated.

The runnable conversions have assertion-level mutation evidence below. The
root/shipped-image tests are a real remaining blocker, not completed work. Their
startup-inclusive budgets are not classified as safe. The full suite was not run
during other agents' mutations, as explicitly requested for this shared checkout.

## Completion Paths

| Test or producer | Actual completion path | Change |
| --- | --- | --- |
| Executor in-group writer | `_handle_launch` -> real Popen -> Python leader -> forked writer -> leader exit -> real output monitor -> group termination -> home cleanup -> terminal | Replaced shell `sleep .2` ordering with a pipe byte written after the grandchild installs SIGTERM ignore and writes `ready`. The writer waits indefinitely for termination, not a 30-second natural exit. The test waits for leader exit before entering the real monitor, so its unchanged request deadline does not include interpreter startup. A test-owned final SIGKILL cleans up the group if the cleanup assertion fails. |
| Real supervisor matrix | Popen supervisor -> pre-spawn supervisor `ready` -> Popen payload interpreter -> file readiness -> `_wait_factory` -> terminal packet -> supervisor reap | Payload readiness is distinct from supervisor readiness. Cancellation and expiry start after actual payload readiness. The test-owned monitor clock is fixed; timeout mode explicitly selects zero expiry, other modes retain the 10-second argument without clock advancement. Removed future-result and final-wait stage budgets and the test's shortened product reap/stop constants. Payloads wait for signals instead of sleeping 30 seconds. |
| Direct durable output | Compute launch -> asyncio real subprocess -> regular stdout/stderr files -> child flush -> independent readiness file -> cancellation/expiry -> collection | Removed `200 * .01` output-content polling. Readiness is independent of the asserted transcript bytes. After readiness, expiry is explicit zero; cancellation collection has no separate stage timer. Added the expected SIGTERM exit-code assertion. |
| Pipe-holding grandchild | Compute launch -> real Python leader -> Popen grandchild -> SIGTERM-ignore readiness pipe -> leader readiness pipe -> test release pipe -> output/termination | Removed finite lifetimes and 2/10-second collection timers. Added release after readiness, preventing overflow from killing the leader before it can publish readiness. Both TERM and KILL remain asserted. |
| Normal worker/direct parity | WorkerClient socket -> executor thread -> `_handle_launch` -> Popen interpreter -> two writing threads -> regular files -> terminal -> client collection; then independent direct subprocess | Both million-byte producers terminate by thread joins, not elapsed time. Worker monitor starts after producer exit with the original request budget unchanged. Removed worker/direct collection and server-thread stage timers. |
| Timeout parity | Event-held fake worker; direct real subprocess held on an Event -> explicit zero expiry -> cancellation -> reap | Removed the finite two-second direct child. The direct launch itself is awaited before expiry. This case tests cancellation before child application readiness, not application output. |
| Running cancellation parity | Real executor Popen -> handler-ready pipe -> authenticated cancel -> real group termination; direct Popen -> handler/output-ready pipe -> explicit expiry -> real termination | Removed finite 30-second lifetimes and readiness/collection stage ceilings. Existing signal ordering and output assertions remain. |
| Concurrency parity | Event-held fake workers; real first direct child -> output/readiness file -> signal-held lifetime; independent second direct interpreter -> natural exit | Removed collection timers and the 200-iteration readiness limit. First child cannot naturally finish while the second is being collected. |
| Disabled parity | Real direct interpreter for baseline, then each disabled flag -> environment output -> natural exit | Removed startup-inclusive collection timers. Added a known baseline result, so two equally corrupted outputs cannot pass parity. |
| Job-alive test | Event-held fake worker; real direct Popen -> explicit cancel -> reap | Replaced 30-second lifetime with indefinite Event wait and removed direct collection timer. Added SIGTERM-result assertion. |
| Supervisor harness | Outer isolated Python subreaper -> supervisor interpreter -> payload interpreter -> forks -> protocol events -> terminal/EOF -> supervisor wait -> owned `/proc` checks -> fixture reap | Removed socket timeout 8, readiness deadline 5, and per-stage waits 6. One 240-second subprocess ceiling covers the complete harness protocol under pytest's existing 300-second ceiling. The reported socket timeout in the prompt was 5; the actual file contained 8. |
| Escaped double fork | Middle forks, grandchild installs handler and waits until reparented, writes pipe readiness; leader waits for byte and reaps middle | Removed the extra `.1` sleep. The existing pipe already proves the required order. |
| Respawning group | First child and grandchild record themselves with inherited TERM handler -> each sends a readiness byte -> primary publishes readiness -> harness sends stop/EOF | Replaced `.1` sleep with two explicit acknowledgements. TERM-created descendants still must bring the registry count to at least six. |
| Zombie reporting | Grandchild exits -> middle `waitid(WEXITED | WNOWAIT)` -> pipe acknowledgement -> middle exits -> primary `waitid(... WNOWAIT)` -> primary exits | Removed `.1` and `.3` ordering sleeps without prematurely reaping either expected adoptee. |
| Reap while primary lives | Zombie is adopted -> real supervisor observation/reap iteration -> next observation wrapper publishes `.checked` -> still-running primary asserts zombie `/proc` entry absent | Replaced the three-second zombie-disappearance deadline with an iteration acknowledgement. The wrapper calls the real observer and does not reap. Suppressing the real active-loop reap causes an assertion and payload exit 1, not a ceiling. |
| Backpressure descendants | Middle spawns 40 grandchildren -> each records identity and installs handler -> 40 pipe acknowledgements -> middle exits -> supervisor adopts -> event socket fills -> teardown | Added acknowledgements before exposing adoptees. Previously recording and handler installation could race teardown. |

`None` collection timeouts mean no separate test stage deadline; they do not
change product constants. Poll sleeps `.01`, the orphan reparenting sleep `.001`,
and indefinite child sleeps of 1 second are scheduling yields, not progress or
lifetime assertions. Real child completion remains termination, release, or
natural computation. Tests do not inspect ambient asyncio tasks or module state.

## Retained Guards

Measured using this checkout's locked environment with
`uv run --extra dev --extra bench pytest -q -n6` and `--durations=0
--durations-min=0` over only the owned files, selecting the guard tests. Eleven
selected tests passed in 2.04 seconds. Values below are pytest call durations,
rounded by pytest, not upper-bound guarantees.

| Guard | Producer and reason it remains | Measured call |
| --- | --- | --- |
| Parity handshake `wait_for(..., 1)` | In-process SuspendedClient Event; no subprocess or import in the producer. Failure also checks/collects the launch task. | .01 s |
| Parity cancel/collection outer 10-second guards | In-process Event-held tasks; internal .01-second bounds and RuntimeError messages are the property under test. No child startup. | .01 s each |
| Parity direct-reap outer 10-second guard | Fake Process.wait holds an Event; real process-group termination is replaced, internal .01-second reap bound produces RuntimeError. No child. | .01 s |
| Fake-worker `backend.wait(..., 2)` and invalid-handle waits | Event release already issued, or handle validation raises synchronously. No real producer. | Included in the .01-second-scale fake tests; no independent substage measurement claimed. |
| Worker connection backpressure `full.wait(2)`, join 1 | Launch is replaced with synchronous nonblocking sends into a real socket, no Popen. The thread's forbidden blocking-error-send behavior is the tested property. | < .01 s |
| That test's .1-second receive, 3-second drain, final join 2 | Failure cleanup of the test-owned socket/thread, not startup or a child deadline. | Included in < .01 s healthy call; failed-cleanup path not separately timed. |
| Factory fail-closed receive timeout 1 | `supervise` has already returned after synchronous sends. Popen is replaced with a raising fake; no child. | < .01 s per case |
| Factory `_send` bounded socket report | Actual full/broken socket, no child. Product nonblocking/report timeout is the subject, not a test startup timer. | .10 s |
| Factory permission-denied reap bound | Controlled clock iterator, mocked process operations, no child. | < .01 s |
| Worker unreapable-supervisor bound | Controlled clock, mock process waits including timeout errors, no child. | .01 s |
| Harness final five-second reap deadline | Starts only in fixture cleanup after supervisor termination; isolated harness owns all descendants and sends SIGKILL. Not a spawn/readiness budget. | Whole harness calls .27-.50 s; no separate cleanup timing claimed. |
| Real TERM grace in running-cancellation parity | Starts only after handler readiness; actual TERM/KILL sequence remains the oracle. No product budget was increased. | Whole test 5.11-5.16 s |

Real supervisor matrix calls measured .03 seconds for death and .24-.27 seconds
for other modes. The writer test measured .07 seconds; normal parity .05 seconds;
durable output .02-.04 seconds; grandchild termination .09-.13 seconds;
concurrency .10 seconds; disabled parity .05-.17 seconds. These observations
justify neither a new short stage deadline nor a claim of universal runner speed.

## Actual Mutations

Every entry below was an actual temporary on-disk product edit, followed by a
pytest command with `-q -n6`, `--extra dev --extra bench`, and only the named owned
test node(s). Each edit was restored using `apply_patch`. No listed expected
failure was a timeout/ceiling failure. The death diagnostic check is pytest's
regex-mismatch AssertionError after the product RuntimeError.

| Mutation | Test(s) and actual assertion failure | Run elapsed |
| --- | --- | --- |
| Delete `_terminate_process_group(proc, 0)` on leader exit | `test_terminal_waits_for_in_group_writers_before_cleanup`: group still has a live member at home cleanup | 2.55 s, 1 failed |
| Add 1 to `_wait_factory` terminal exit code; change missing-terminal diagnostic | All six `test_wait_factory_real_supervisor` modes: 38 != 37, -14 != -15, or diagnostic regex mismatch | 4.50 s, 6 failed; repeated after final clock change in combined 1.94 s run |
| Remove supervisor `_enable_subreaper()` | Escaped double-fork, cancellation stop/eof respawning group, escaped cancellation, zombie reporting: owned registry PIDs remain in `leaked` | 2.74 s, 5 failed |
| Suppress active supervisor loop's non-primary `waitpid` | `test_adopted_zombie_reaped_while_primary_still_running`: child assertion exits 1; terminal code 1 != 0 | 2.02 s, 1 failed |
| Add 1 to supervisor terminal code; return success from supervisor error path | Failure-exit report 38 != 37; full-socket result 0 != 1 | 2.12 s, 2 failed |
| Send SIGKILL in place of initial SIGTERM | Durable-output sigterm/timeout, grandchild timeout/overflow, parity timeout/running-cancellation/concurrency: wrong exit code or wrong signal-event sequence | 1.59 s, 7 failed |
| Same initial-signal mutation | `test_job_alive_tracks_contained_task_and_direct_process`: -9 != -15 | 1.67 s, 1 failed |
| Add 1 to normal `_wait_with_output_limits` return code | Normal-completion parity: worker code 1 != direct code 0 | 1.61 s, 1 failed |
| Write unexpected stdout in real `_arm_parent_death_signal` preexec hook | Disabled parity: known baseline stdout differs from `unchanged\n` | 2.19 s, 1 failed |
| Reapply factory terminal/diagnostic, initial-signal, and error-success mutations | Final matrix clock, final grandchild release pipe, final 40-child acknowledgement: all nine selected nodes fail their result/signal assertions | 1.94 s, 9 failed |
| Inline `prctl(36, 1, 0, 0, 0)` after `_enable_subreaper` | Existing disabled-prctl negative-control test: its expected leak disappears, `assert result['leaked']` fails | 1.25 s, 1 failed |

One initial factory mutation invocation hit a SyntaxError in the new embedded
wrapper's string delimiters. That was a test edit error, fixed before repeating
the mutation run; it is not counted as mutation evidence. The repeated run
produced the five assertion failures listed above.

## Blocked Paths

The scoped runs consistently skip four nodes:

- Two parameterizations of
  `test_factory_descendant_cannot_write_controller_canary_and_negative_control_is_live`:
  Linux root is required. This path forks the controller, accepts its executor
  socket, drops identities, launches a real supervisor and payload, and in the
  non-Git case starts another interpreter. Its nested `subprocess.run(timeout=5)`,
  WorkSpec/collection 10-second budgets, and listener timeout 15 remain unresolved.
  An unverified removal of the nested timeout was reverted rather than claiming
  the required actual mutation proof from a skip.
- `test_worker_payload_cannot_reach_controller_canary_and_detector_is_live`:
  requires Linux root for the executor identity boundary; the real launch deadline
  cannot be measured or mutation-verified here.
- `test_worker_cannot_cross_attempt_boundary_and_negative_control_is_live`:
  requires the shipped-image identity environment; its producer path cannot be
  measured here.

These are not grounds to increase product budgets or mock away the real UID
boundary. Completion requires running the same audit and actual mutations in a
root-capable shipped-image test environment.

### Where that environment actually is (added 2026-09-11)

It is CI's `pytest-worker-uid` leg, not "any root shell". That distinction was
established empirically rather than assumed:

- The four nodes were run as **root inside the shipped mimirbot image** (the real
  `mimir`/`worklink` uids, the deployed `.venv`), with `PYTHONDONTWRITEBYTECODE=1`
  and `-p no:cacheprovider` so the run could not leave root-owned artefacts in the
  live checkout.
- The run produced **no output at all for 21 minutes** -- not even collection
  progress -- and was killed. Root alone is therefore NOT sufficient to exercise
  these paths.
- The reason is that the leg seeds state these paths depend on, which
  `tests/test_worklink_worker_identity.py::test_ci_worker_uid_leg_seeds_the_state_that_makes_it_discriminating`
  pins: `MIMIR_FILE_TOOL_ROOTS`, the Claude credential file at mode 0600 under a
  0700 directory, and `MIMIR_CLAUDE_OAUTH_CREDENTIALS`. A bare root shell has none
  of it.

So these four nodes are verified by the `pytest-worker-uid` leg on this PR, and
their durations are NOT locally measured. That is a weaker claim than the
assertion-level mutation evidence the runnable conversions carry, and it is
stated as such rather than presented as equivalent. Their startup-inclusive
budgets remain unclassified: this audit does not assert they are safe.

What a future pass needs in order to close them properly is to reproduce that
leg's seeded environment, not merely to obtain root.

## Verification

Command for restored-product verification:

```sh
uv run --extra dev --extra bench pytest -q -n6 tests/test_worklink_worker_exec.py tests/test_worklink_worker_parity.py tests/test_worklink_factory_supervisor.py
```

Restored-product scoped runs before the final release-pipe refinement reported
251 passed, 4 skipped in 6.71, 6.53, and 8.02 seconds. The final restored-product
run reported **251 passed, 4 skipped in 9.19 seconds**. `git diff --check` passed.
Product diffs were checked explicitly and were empty;
unrelated agents' changes were not reverted or included in this work.
