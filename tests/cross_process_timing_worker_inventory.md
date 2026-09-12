# Cross-Process Timing Audit: Worker Tests

Issue: #1669.

Scope: `tests/test_worklink_worker_parity.py`,
`tests/test_worklink_factory_supervisor.py`, `tests/test_pollers.py`, and
`tests/test_pytest_timeout_config.py`, including their locally owned helpers.
`CONTRIBUTING.md` was read before the audit.

This document records the preceding audit and its test-only edits. Creating this
inventory makes no further code changes. Line references describe the files after
those edits and before this document was added.

## Classification

- **a: Overspecified.** An incidental fixture lifetime or scheduling expectation
  exceeds the behavior under test. Remove that assumption without weakening the
  subject's assertions.
- **b: Demonstrated current product defect.** Evidence establishes a defect in
  current production behavior. Possible regressions, regression-test coverage,
  and uninvestigated failures do not qualify.
- **c: Contract requiring synchronization.** Ordering, readiness, output
  completion, or deadline behavior is part of the test's contract. This includes
  unchanged tests that already establish the required ordering correctly.

No category-b product defect was demonstrated by this audit. In particular,
parity lost-output issue #1617 is a known, explicitly excluded report; it was not
reproduced here. The normal-completion parity test is classified **c**, not **b**.
The unrelated full-suite ACP failure described below has not been diagnosed or
baseline-attributed and is not classified as a product defect.

## Changes Recorded

| File / Location | Previous Assumption | Test-Only Edit |
| --- | --- | --- |
| `test_worklink_factory_supervisor.py:271-280` | Respawning descendants needed to be created during the supervisor's TERM grace for the adoption assertion. | Reuse `term_ready_pids=6` for respawning signal variants, establishing the population before signalling the supervisor. Escaped variants retain their existing handshake. |
| `test_pytest_timeout_config.py:156-185` | The child would write its diagnostic and optionally close output pipes before the one-second live-child guard expired. | Add an owned readiness pipe after those actions, before invoking the guard. Preserve diagnostic, liveness, exit-code, and cleanup assertions. |
| `test_pollers.py:3207-3214` | The event-loop callback would be serviced within two seconds. | Detect on-loop execution directly and synchronize off-loop callback delivery with the existing event. Preserve responsiveness, thread identity, redaction, and truncation assertions. |
| `test_pollers.py:4873-4874` | A 120-second fixture sleep would outlast timeout handling. | Use `signal.pause()` so the fixture does not finish naturally. Retain the real one-second timeout path. |

No production files or worker-parity code were edited. No numeric test or product
bounds were widened. No assertion retries were introduced. No commit was made.

## Worker Parity Tests

File: `tests/test_worklink_worker_parity.py`. All entries are unchanged.

| Test / Lines | Class | Reason and Unchanged Rationale |
| --- | --- | --- |
| `test_enabled_launch_cancellation_waits_for_handshake_then_cancels`, 230-269 | c | `entered` and `resume` order cancellation against the client handshake. The existing one-second wait is a failure guard with task cleanup, not a guessed sleep establishing readiness. |
| `test_enabled_launch_failure_after_suspension_is_a_launch_error`, 273-289 | c | `sleep(0)` deliberately introduces an asynchronous suspension before failure. It does not estimate how long another process needs to become ready. |
| `test_job_alive_tracks_contained_task_and_direct_process`, 293-316 | c | Contained completion is event-controlled. The direct child blocks indefinitely, and its test has no child-output prerequisite before cancellation. Results are awaited before terminal liveness checks. |
| `test_contained_timeout_bounds_cancel_and_collection_independently[cancel]`, 321-363 | c | An intentionally stuck cancellation operation exercises the actual internal cancellation bound and its exact error message. No child startup race is involved. |
| `test_contained_timeout_bounds_cancel_and_collection_independently[collection]`, 321-363 | c | Cancellation completes, but collection remains stuck. The actual collection bound must raise the correct error and cancel the job. |
| `test_worker_cancel_socket_receive_has_timeout`, 367-399 | c | A deterministic socket double records the configured timeout and raises a socket timeout. The test checks configuration and propagation rather than measured latency. |
| `test_direct_process_reap_is_bounded`, 403-417 | c | A non-completing process wait exercises the internal reap bound and exact error. The outer guard is retained only to terminate a broken implementation. |
| `test_direct_terminated_output_is_durable_while_running[sigterm]`, 454-493 | c | The child flushes both streams before publishing readiness. Production directs output to regular files, so readiness does not race a separate pipe-draining task. Exact running and terminal output checks remain. |
| `test_direct_terminated_output_is_durable_while_running[timeout]`, 454-493 | c | The same flush/readiness ordering precedes an immediate timeout. Timeout status, signal exit, output, permissions, and durable paths remain asserted. |
| `test_direct_termination_kills_pipe_holding_grandchild[timeout]`, 498-554 | c | The grandchild acknowledges installed SIGTERM immunity before the leader reports readiness. Immediate timeout then exercises group TERM/KILL ordering. |
| `test_direct_termination_kills_pipe_holding_grandchild[overflow]`, 498-554 | c | A separate release pipe prevents overflow from killing the leader before its readiness write completes. The real overflow path and exact signal sequence remain the subject. |
| `test_closed_worker_direct_parity_inventory[normal_completion]`, 573-669 | c | Producer completion and server join synchronize inspection. Exact large-output equality is a valid contract and must not be relaxed. #1617 is a known excluded report, not reproduced or newly demonstrated by this run. |
| `test_closed_worker_direct_parity_inventory[timeout]`, 671-679 | c | Both subjects remain blocked until cancellation. No startup-output or handler-readiness requirement is asserted. Timeout and exit-code parity remain exact. |
| `test_closed_worker_direct_parity_inventory[running_cancellation]`, 681-864 | c | Readiness establishes the installed signal behavior and initial write before cancellation. The existing contract correctly allows the TERM-handler line to be absent under bounded TERM grace while requiring signal/reap/terminal/cleanup ordering. This is already correct, not a new category-a finding. |
| `test_closed_worker_direct_parity_inventory[running_cancellation-delayed-handler]`, 681-864 | c | The negative control explicitly holds the handler before its write until SIGKILL. Exact ready-only output is therefore synchronized rather than inferred from scheduler speed. |
| `test_closed_worker_direct_parity_inventory[concurrency]`, 866-954 | c | Worker completions are controlled independently by events. The first direct child publishes readiness after flushing its output. Identity, cancellation isolation, and exact result assertions remain. |
| `test_closed_worker_direct_parity_inventory[coding_disabled]`, 956-1043 | c | Completion precedes result inspection. Negative claims use component-specific forbidden-operation counters rather than ambient process state. |

### Worker Parity Helpers

| Helper / Lines | Ownership and Ordering | Disposition |
| --- | --- | --- |
| `Authorization`, 33-44 | Test-owned authorization double; verification counter and duplicated descriptor have no timing-dependent assertions. | Unchanged. |
| `WorkerProcess`, 47-74 | Supplies pre-fed streams and an explicit `release` event. `wait()` records reaping only after release. | Unchanged; completion ordering is explicit. |
| `WorkerClient`, 77-120 | Writes fixture bytes to the provided sinks before publishing the process. Release events control completion and cancellation. | Unchanged; no independent producer scheduling is assumed. |
| `spec`, 123-145 | Constructs test work specifications, including the existing two-second timeout value. | Unchanged; construction itself has no timing dependency. |
| `launch_worker`, 217-226 | Configures the contained backend and awaits its launch handshake. | Unchanged. |
| `direct_spec`, 420-424 | Builds real interpreter commands in the current checkout. | Unchanged. |
| `run_direct`, 427-435 | Awaits launch, result, and cleanup in order. | Unchanged; no polling of incomplete output. |
| `wait_for_child_ready`, 438-449 | Reads exactly one readiness byte from a test-owned pipe and closes its transport. | Unchanged; the per-test ceiling covers the protocol without restarting a stage deadline. |
| `create_with_readiness`, 515-517, and grandchild source, 526-539 | Pass readiness/release descriptors explicitly and acknowledge descendant signal setup. | Unchanged; overflow release is separate from readiness. |
| `after_output_producer`, 613-617, and `serve`, 621-625 | Wait for output producer completion before monitoring, then close the executor socket when serving finishes. | Unchanged; normal-completion output inspection follows producer completion and server join. |
| Cancellation observers and `ObservedProcess`, 706-825 | Record actual signal and reap boundaries; the readiness pipe establishes child signal setup first. | Unchanged; signal-handler output is not guaranteed merely by sending TERM. |
| `ForbiddenAuthorization`, `ForbiddenClient`, `ForbiddenProjection`, and `forbidden_socket`, 967-994 | Counters are owned by the disabled-path test. | Unchanged; negative assertions do not compare ambient state. |

## Factory Supervisor Tests

File: `tests/test_worklink_factory_supervisor.py`.

| Test / Lines | Class | Reason and Disposition |
| --- | --- | --- |
| `test_escaped_double_fork_is_adopted_and_reaped`, 235-241 | c | Unchanged. Pipe readiness establishes escaped-descendant setup and reparenting. The harness waits for supervisor exit before checking cleanup. |
| `test_disabled_prctl_mutation_is_detected_and_fixture_reaps_leak`, 244-248 | c | Unchanged. Uses the same established escaped population. The isolated outer subreaper owns negative-control cleanup. |
| `test_cancellation_reaps_multigeneration_respawning_group[stop]`, 252-258 | c | Unchanged. Already records at least six processes before cancellation; checks cleanup and the terminal signal result. |
| `test_cancellation_reaps_multigeneration_respawning_group[eof]`, 252-258 | c | Unchanged. The same population synchronization precedes EOF cancellation. |
| `test_cancellation_reaps_escaped_descendant`, 261-265 | c | Unchanged. Escaped-fixture readiness precedes the stop request. |
| `test_signalled_supervisor_reaps_every_descendant[escaped-SIGTERM/SIGINT]`, 271-280 | c | Existing escaped handshake retained. No extra prepopulation is needed for these variants. Cleanup, ECHILD evidence, and adoption assertions remain. |
| `test_signalled_supervisor_reaps_every_descendant[respawning-SIGTERM/SIGINT]`, 271-280 | c | Edited. Reuse `term_ready_pids=6` before signalling the supervisor. The adoption assertion no longer depends on fixture handlers creating descendants during TERM grace. No supervisor deadlines or teardown behavior are altered. |
| `test_disabled_signal_handlers_leave_surviving_descendant`, 283-289 | c | Unchanged. An established escaped descendant survives deliberately disabled supervisor handlers. The harness owns cleanup. |
| `test_failure_exit_is_reported_not_supervisor_exit`, 292-296 | c | Unchanged. Terminal reporting and supervisor exit precede inspection of the payload exit status. |
| `test_zombie_adoptee_is_reported`, 299-318 | c | Unchanged. `waitid(..., WNOWAIT)` establishes zombie state before proceeding. |
| `test_adopted_zombie_reaped_while_primary_still_running`, 321-344 | c | Unchanged. `.checked` is published after an observed adoptee leaves the child set, not merely after enumeration. The primary remains alive until that evidence exists. |
| `test_full_socket_fails_run_without_leaking_live_adoptees`, 347-372 | c | Unchanged. All 40 descendants acknowledge readiness. Draining is withheld until supervisor exit, establishing actual backpressure rather than relying on a short scheduling window. |
| `test_event_loss_survives_reaping_and_socket_recovery`, 375-425 | c | Unchanged. Socket saturation, recovery, and reap ordering are explicitly controlled in-process. |
| `test_permission_denied_survivor_hits_overall_bound`, 476-490 | c | Unchanged. A controlled monotonic clock drives the deadline; no runner-latency assertion is made. |
| `test_signals_during_spawn_and_teardown_do_not_extend_budget`, 494-537 | c | Unchanged. Controlled time and direct handler invocation test the budget without a cross-process delivery race. Both signalled variants retain the exact budget assertion. |
| `test_sends_are_bounded_on_full_or_broken_socket`, 565-573 | c | Unchanged. Establishes actual socket saturation and closure before asserting send refusal. |
| `test_fd_closed_in_payload_and_ready_precedes_spawn`, 576-587 | c | Unchanged. Reads readiness synchronously at the mocked spawn boundary. |

### Factory Supervisor Helpers

| Helper / Lines | Ownership and Ordering | Disposition |
| --- | --- | --- |
| `HARNESS`, 29-159 | An isolated outer process becomes a subreaper and owns all fixture descendants, including mutation leaks. Receives supervisor readiness, waits for fixture markers, collects reports, waits for supervisor exit, and then checks recorded PIDs. | Unchanged. Never introduces process-wide waits or subreaper state into pytest. |
| `HARNESS` live-reap wrapper, 40-62 | Tracks previously observed non-primary children and publishes `.checked` after they leave the observed child set. | Unchanged; enumeration alone is not treated as proof of reaping. |
| `HARNESS` signal wrapper, 63-81 | Publishes `.reaped` only when the supervisor's all-child `waitid` raises ECHILD. | Unchanged; proves completion rather than merely signal issuance. |
| `HARNESS` `term_ready_pids` branch, 91-104 | Signals the established payload group, then waits for recorded fixture descendants before the supervisor receives its cancellation trigger. | Implementation unchanged; newly reused by the respawning supervisor-signal variants. Existing ten-second fixture guard retained. |
| `HARNESS` final cleanup, 137-158 | Kills/reaps descendants owned by the outer subreaper, including when mutations leave survivors. | Unchanged; existing five-second cleanup deadline retained. |
| `PRELUDE` / `record`, 161-169 | Uses `os.write` on an append descriptor and closes it for each PID record. | Unchanged; records do not depend on buffered Python stream flushing. |
| `ESCAPED`, 171-191 | Uses a pipe to establish escaped descendant signal setup and reparenting before publishing `.ready`. | Unchanged. |
| `RESPAWN`, 193-215 | Installs TERM behavior before forking and uses two readiness bytes for the initial multigeneration population. | Unchanged; extra population required by relevant tests is established through the existing harness branch. |
| `run_isolated`, 218-227 | Captures the harness output through process completion and parses the final JSON. | Unchanged; one 240-second ceiling covers startup, descendants, reporting, and cleanup. |
| `assert_clean`, 230-232 | Checks supervisor disappearance and the harness's owned-PID leak results after completion. | Unchanged. |

## Poller Tests

File: `tests/test_pollers.py`.

| Test / Lines | Class | Reason and Disposition |
| --- | --- | --- |
| `test_run_poller_exports_its_effective_timeout`, 1809-1852 | c | Unchanged correct contract. Already separates exported values from interpreter startup using `_control_poller_wait`. Default, 45-second, and fractional 0.5-second values remain asserted. |
| `test_run_poller_diagnostic_redaction_offloop_before_truncation`, 3175-3230 | a/c | Edited. Removed the incidental two-second callback-latency assumption. On-loop execution is detected directly; off-loop callback delivery is synchronized. Exact redaction, truncation, callback responsiveness, and thread identity assertions remain for all variants. |
| `test_run_poller_timeout_kills_subprocess`, 3513-3560 | c | Unchanged. All count/tail variants flush before readiness. Controlled expiry then exercises record discard, timeout logging, and circuit-breaker behavior. |
| `test_run_poller_timeout_kills_child_holding_pipes`, 3584-3669 | c | Unchanged. Grandchild and output readiness precede expiry. Owned-group liveness synchronization accounts for descendant death becoming visible after direct-child reaping. |
| `test_run_poller_bounded_when_child_closes_pipes_but_keeps_running`, 3675-3712 | c | Unchanged. Closure readiness precedes actual drain completion; controlled reap expiry exercises the post-EOF path for both count variants. |
| `test_run_poller_cursor_retry_delivers_once`, 3718-3754 | c | Unchanged. Flush/readiness and an indefinite hold establish the interrupted tick. Repeated poller invocations are the cursor-retry subject, not assertion retries. |
| `test_delivery_barrier_is_acked_before_dispatch_phase_timeout`, 4423-4495 | c | Unchanged. Barrier completion precedes expiry. The dispatch marker is fully written and closed before atomic publication, avoiding existence-before-write races. |
| `test_delivery_barrier_refuses_receipt_when_alert_append_fails`, 4500-4565 | c | Unchanged. The observer waits for refusal processing to complete before receipt/dispatch absence is asserted. |
| `test_run_poller_reaps_subprocess_on_timeout`, 4865-4888 | a | Edited. Indefinite blocking replaces a finite fixture lifetime unrelated to timeout correctness. The real one-second timeout remains. Reaping is still checked implicitly rather than by explicitly observing `wait()`; that separate coverage limitation was not expanded in this audit. |
| `test_run_poller_output_overflow_kills_and_fails`, 6551-6597 | c | Unchanged. Both real capped drains finish before their release and controlled expiry. All stream/timeout variants retain real overflow handling and failure assertions. |

### Poller Helpers

| Helper / Lines | Ownership and Ordering | Disposition |
| --- | --- | --- |
| `home`, 125-130, and `_read_events`, 133-140 | Initialize the test logger and read its event file after the awaited operation. | Unchanged; no additional sleep or flush retry was added. |
| `_install_script`, 150-156 | Writes and closes each fixture script before launching it. | Unchanged; script publication is synchronous. |
| `slow_redact`, 3204-3215 | Test-local redaction observer posts a callback to the owned loop and waits on its event only when running off-loop. | Edited. On-loop execution records non-responsiveness without blocking its own callback. No callback-latency threshold remains; the existing outer ten-second guard is retained. |
| `_control_poller_wait`, 3461-3490 | Replaces only the poller module's deadline operations through a namespace, preserving the event loop's real clock and waits. Calls readiness before controlled expiry, or awaits real task completion for the non-expiring path. Can deterministically expire the post-EOF reap. | Unchanged. Readiness does not restart a stage deadline; the per-test ceiling bounds the protocol. |
| `_poller_file_ready`, 3493-3495 | Polls a test-owned marker until it exists. | Unchanged. This is condition synchronization, not a fixed sleep standing in for readiness. |
| `_poller_clock`, 3498-3506 | Supplies a poller-local monotonic clock for exact circuit-breaker assertions without altering the event loop clock. | Unchanged. |
| `_live_process_group_members`, 3564-3579 | Filters `/proc` entries by the test-owned process-group ID and excludes zombies. | Unchanged. The assertion concerns the owned group, not equality of ambient process state. |
| Pipe-holder `ready` and `observed_kill`, 3621-3642 | Wait for fixture readiness and observe real group signalling. A rescue kill applies only when a regression omitted the group kill, so the test can fail its signal assertion rather than hang on inherited pipes. | Unchanged; rescue is not an assertion retry. |
| `_observe_poller_barrier`, 4402-4418 | Wraps the real capped drain's line callback and sets an event only after barrier processing completes. | Unchanged; refusal/acknowledgement completion precedes absence or dispatch checks. |
| Dispatch marker publication, 4451-4456 | Writes and closes a pending marker, then atomically renames it. | Unchanged; observing existence establishes complete contents. |
| Overflow `held_drain` and `ready`, 6565-6575 | Await both real drains, hold their returned values behind an event, and release them before controlled timeout selection. | Unchanged; overflow/EOF processing is not inferred from a producer-side flush alone. |

## Pytest Timeout Tests

File: `tests/test_pytest_timeout_config.py`.

| Test / Lines | Class | Reason and Disposition |
| --- | --- | --- |
| `test_kill_child_group_cleanup_races`, 35-49 | c | Unchanged. Deterministic error/return-code matrix covers cleanup semantics without scheduling dependence. |
| `test_wait_for_child_does_not_require_descendant_pipe_eof`, 112-151 | c | Unchanged. A descendant is held on an unreleased pipe, rather than kept alive by a guessed sleep. Both the legacy timeout control and the new collector's exact output/diagnostic assertions remain. |
| `test_wait_for_child_reports_live_child[open-pipes]`, 156-185 | c | Edited. A readiness byte confirms the stderr write before the unchanged live-child guard begins. The child remains blocked on stdin. |
| `test_wait_for_child_reports_live_child[closed-pipes]`, 156-185 | c | Edited. The readiness byte follows both output-pipe closures as well as the diagnostic write. The closed-pipe/live-child branch is therefore established before its guard is exercised. |
| `test_wait_for_child_drains_output_while_child_runs`, 190-198 | c | Unchanged. Real pipe pressure requires concurrent draining. Full million-byte outputs and successful exit remain asserted. |
| `test_hanging_async_test_fails_and_session_continues[serial]`, 213-341 | c | Unchanged. Confirms coroutine suspension, waits for the real diagnostic frame, joins the forwarding thread, then delivers SIGALRM. No guessed flush margin is used. |
| `test_hanging_async_test_fails_and_session_continues[xdist]`, 213-341 | c | Unchanged. Uses the same synchronized child protocol inside xdist and retains failure identity, continued-session, XML, and last-failed-cache assertions. |

`test_timeout_policy` at 201-208 is a synchronous configuration assertion, not a
cross-process race candidate. It remains unchanged and pins the repository's
300-second timeout, signal method, 300-second diagnostic dump, and non-exiting
faulthandler policy.

### Pytest Timeout Helpers

| Helper / Lines | Ownership and Ordering | Disposition |
| --- | --- | --- |
| `_kill_child_group`, 21-29 | Signals only the child-owned session/group. Distinguishes a live-child EPERM from an exited-leader cleanup race. | Unchanged. |
| `_wait_for_child`, 52-108 | Drains stdout/stderr while the controller runs, distinguishes a live controller from exited-controller inherited writers, bounds post-exit draining, kills the owned group, and reaps only the direct child. | Unchanged. Default 30-second controller guard, one-second drain guard, and five-second reap guard retained. |
| Descendant hold pipe, 113-147 | Parent retains the release writer so descendants cannot reach EOF or complete naturally during the collector test. | Unchanged; explicit ownership establishes the open-writer condition. |
| Live-child readiness pipe, 157-185 | Passed only to the child; child acknowledges diagnostic emission and optional closures. Parent closes unused descriptors and cleans up the child on readiness failure. | Added by the preceding audit; this document makes no further edit. |
| Generated `record_alarm`, `record_diagnostic`, and `pytest_runtest_protocol`, 240-258 | Record requested alarm/diagnostic policy while suppressing accidental setup-time delivery. | Unchanged; configured values remain asserted before controlled real signal delivery. |
| Generated `blocked_select`, 260-295 | Confirms the coroutine is actually suspended and the future unfinished, waits for the real dump, then sends SIGALRM to the handler installed by pytest-timeout. | Unchanged. |
| Generated `forward_dump`, 275-282 | Drains the diagnostic pipe concurrently, forwards bytes to the captured destination, and signals when the blocked frame is present. | Unchanged; writer closure and thread join complete forwarding before SIGALRM. |

## Unchanged Bounds and Timing Rationale

- Repository policy remains a 300-second per-test timeout with the POSIX signal
  method and a 300-second non-exiting faulthandler dump. No global policy changed.
- Worker-parity internal test constants remain 0.01 seconds for cancellation,
  collection, and direct reap where explicitly overridden. Existing 0.001-second
  timeout triggering and ten-second outer hang guards remain. The audit did not
  adopt or repeat historical bound-widening commentary as a new fix.
- Existing worker TERM acceleration remains 0.05 seconds where configured. The
  cancellation handler's execution is not guaranteed by that grace; tests already
  distinguish guaranteed readiness output from optional handler output.
- Factory production bounds remain `TERM_GRACE=0.2`, `REAP_TIMEOUT=2.0`, and
  `INTERVAL=0.01`. Relevant fixtures establish required populations before those
  deadlines begin, without intercepting or extending production teardown.
- Factory harness guards remain 240 seconds overall, ten seconds for recording
  the pre-trigger respawn population, and five seconds for outer-harness cleanup.
  These guards are failure ceilings, not evidence that readiness occurred.
- Poller tests retain their existing per-test ceilings, including the 60-second
  marks, configured timeout values, and the redaction test's ten-second outer
  guard. Controlled waits establish the intended deadline branch without
  requiring interpreter startup or flush completion within a tiny test budget.
- The poller redaction fixture's two-second responsiveness threshold was removed
  as the category-a assumption, not increased to another duration. Thread
  identity detects the on-loop regression, and event delivery proves callback
  responsiveness. The 120-second reap-fixture sleep was likewise an incidental
  lifetime, replaced by indefinite blocking while retaining the real timeout.
- The live-child collector test retains its one-second guard. Readiness is
  established first; the guard then measures the intended blocked-child state,
  rather than racing diagnostic emission or optional output-pipe closure.
- The collector retains its default 30-second execution and one-second
  post-exit-drain guards, its five-second direct-child reap bound, and the
  descendant test's 0.1-second drain override. Open inherited writers are held
  explicitly, not assumed to survive for those durations.
- Nested pytest still derives and asserts a two-second timeout and a 0.2-second
  diagnostic setting. Delivery is synchronized to suspension and observed dump
  output, not achieved by increasing either value.
- Existing marker and owned-group polling waits for specific state transitions.
  No new retries of failed assertions or new sleep-based readiness estimates were
  added.

## Coverage Boundary

This is the complete inventory of relevant tests and helpers from the preceding
report, with the classification correction above. Other tests in the four files
exercise configuration, parsing, environment values, or synchronous mocked state
without a relevant cross-process timing/flush dependency. They were not changed.
No category-b classification is inferred merely because a test protects a product
contract or could expose a future regression.

## Verification Recorded

Scoped command:

```sh
uv run --locked pytest -q tests/test_worklink_worker_parity.py tests/test_worklink_factory_supervisor.py tests/test_pollers.py tests/test_pytest_timeout_config.py
```

Result: **510 passed**, three warnings, in 23.84 seconds.

Full command:

```sh
uv run --locked pytest -q
```

The first invocation was stopped by the terminal tool's 120-second command
allowance at approximately 36%, with no reported failures at that point. It is
not a completed full-suite result. A subsequent invocation used a longer command
allowance without changing any test bounds and completed in 372.14 seconds:
**17,120 passed, 51 skipped, one failed**, with 100 warnings.

The failure was
`tests/test_acp_shutdown.py::test_signal_deadline_preserves_observed_failure_during_resistant_drain`,
which hit its shutdown ceiling. No failures were reported in the four audited
files. The ACP failure has not been diagnosed or compared against an unmodified
base; it is not evidence for a category-b finding in this audit. That intermediate
run was not green. After the ACP synchronization edits completed, the final
integrated gate was green: `uv run --extra dev --extra bench pytest -q -n 6`
reported **17,124 passed, 48 skipped**, 100 warnings in 271.18 seconds. The seven
named files also passed together under `-n 12`: **802 passed**, 36 warnings in
21.72 seconds. See `.worklink-pr-body.md` for the exact scoped command.

`git diff --check` passed after the test edits. Concurrent changes appeared in
`tests/test_acp_daemon.py` and `tests/test_acp_shutdown.py`; they were not edited or
reverted by this audit. No commit was made.
