# ACP Cross-Process Timing Inventory

Audit for issue #1669 of `test_acp_shutdown.py`, `test_acp_hosted.py`,
`test_acp_daemon.py`, and their dedicated helpers. `CONTRIBUTING.md` was read
before the audit. This document records the completed audit and test changes;
creating this inventory does not change executable code.

Line references identify the audited worktree after the test changes. Each test
entry covers all of that test's parameter variants. Paths in the tables are
relative to this directory.

## Classification

- **a: Overspecified.** The test assumed scheduling, timing, or an incidental
  lifetime that was not the intended contract. Some entries were already fixed
  in the audited baseline and needed no additional change.
- **b: Demonstrated current product defect.** This category requires evidence of
  a present production defect. Merely covering a possible or historical product
  defect does not qualify.
- **c: Contract requiring synchronization.** The behavior or bound is legitimate.
  Synchronization was either added by this audit or already present. Correct
  bound-contract tests remain in this category even when they are regression
  coverage for historical defects.

**No category-b current product defect was demonstrated by this audit.** The
shutdown watchdog issue **#1658 is an out-of-scope known report, not reproduced
by this audit**. Its presence in the issue tracker is not evidence that the
audited execution reproduced it.

## Scope and Outcome

Eight tests were changed in two files:

- `test_acp_shutdown.py`: synchronize a diagnostic snapshot; control expiry
  after failure observation and resistant drain; use teardown-only shells in
  two generation-lifecycle tests.
- `test_acp_daemon.py`: acknowledge actual admission; synchronize concurrency
  and authentication observations; release a failing close specifically inside
  its first grace interval.
- `test_acp_hosted.py`: unchanged. Its relevant subprocess tests already
  synchronize ownership, captured output, leader exit, and completion.

No production code was edited. No commits were made. Unrelated worktree changes
were left untouched. No test bound was widened and no retries were introduced.
Existing controlled-timer patterns were used to remove unrelated natural expiry
from lifecycle tests, not to relax their teardown or harness bounds.

## Shutdown Tests

All references in this section are to `test_acp_shutdown.py`.

| Test | Class | Disposition, Reason, and Bound Rationale |
| --- | --- | --- |
| [`test_idle_selector_signal_wakes_and_tears_down`, line 215](test_acp_shutdown.py#L215) | c | Unchanged. Parent waits for idle readiness; child checks the actual wakeup descriptor and completed teardown. The five-second parent wakeup bound is retained: waking the idle selector is the subject, and a child-loop timer must not rescue a missing signal wakeup. |
| [`test_signal_wakeup_resource_lifetime`, line 303](test_acp_shutdown.py#L303) | c | Unchanged. Assertions execute in an isolated child after explicit close or install-failure operations; parent waits for completion. The 120-second subprocess timeout is a hang guard, not evidence of resource release. |
| [`test_shutdown_hooks_restore_only_owned_resources`, line 378](test_acp_shutdown.py#L378) | c | Unchanged. Resource ownership and restoration are checked synchronously in the child, including foreign-resource controls. The 120-second subprocess guard remains unchanged; assertions depend on completed operations, not elapsed time. |
| [`test_proxy_signal_teardown_and_client_eof_are_silent`, line 479](test_acp_shutdown.py#L479) | c | Unchanged. Idle delivery uses readiness; other stages inject signals at the intended operation. `communicate()` precedes final output assertions. Bootstrap's reserved output is unbuffered. The existing 0.01-second peer-EOF grace setting accelerates this teardown fixture; the shutdown ceiling remains a hang guard, not a readiness mechanism. |
| [`test_signal_exit_bounds_entire_teardown`, line 654](test_acp_shutdown.py#L654) | c | Unchanged. Controlled expiry or escalation follows ordered stage markers and the existing timer-thread diagnostic handshake. The 120-second whole-protocol ceiling is a hang guard. Individual stage reads do not race short deadlines; the controlled watchdog and resistant stage prove the exit boundary. |
| [`test_controlled_watchdog_reports_why_callback_did_not_run`, line 785](test_acp_shutdown.py#L785) | c | Unchanged. Explicit input distinguishes expiry from EOF and cancellation; `join()` orders final journal reads. The 120-second subprocess guard does not decide whether the callback should run. |
| [`test_shutdown_diagnostics_locate_signal_before_journal_flush`, line 814](test_acp_shutdown.py#L814) | c | Changed. `flush-blocked` is a main-thread marker, not proof that the watchdog thread wrote `watchdog-input-wait`. The test now awaits that diagnostic before taking the snapshot. The 0.05-second diagnostic timeout is unchanged and begins only after the required observations; it tests timeout formatting against an intentionally blocked child. |
| [`test_preinstall_sigint_exits_without_blocked_teardown`, line 867](test_acp_shutdown.py#L867) | c | Unchanged. Startup SIGINT must not enter blocked teardown. An explicit blocked marker and exact exit assertions expose violations. The shutdown ceiling remains a hang guard; adding a delay or permitting blocked teardown would weaken the contract. This is not evidence of a current product defect. |
| [`test_startup_sigint_handler_restored`, line 936](test_acp_shutdown.py#L936) | c | Unchanged. Restoration is asserted inside the isolated child after `_proxy` returns or raises. The 120-second subprocess timeout is only a hang guard. |
| [`test_signal_exit_timeout_reports_child_progress`, line 975](test_acp_shutdown.py#L975) | c | Unchanged. Child writes its progress prefix before the `sleeping` handshake. The 0.05-second protocol timeout tests diagnostics against an intentionally surviving child rather than child startup. The existing startup ceiling is retained. |
| [`test_shutdown_journal_timeout_distinguishes_surviving_child`, line 1018](test_acp_shutdown.py#L1018) | c | Unchanged. Each simulated stage is acknowledged before the diagnostic timeout begins. The 0.05-second timeout tests formatting, not signal delivery or startup; the existing 120-second test and handshake guards remain. |
| [`test_shutdown_journal_wakeup_without_python_signal_handler`, line 1109](test_acp_shutdown.py#L1109) | c | Unchanged. Linux readability transition proves entry into the blocking C receive; tee flush completes before `delivered`. No sleep is used as evidence of signal delivery. The 0.05-second diagnostic timeout and 120-second test guard remain unchanged because the child is deliberately held in the C call. |
| [`test_real_signal_watchdog_bounds_blocked_cleanup`, line 1174](test_acp_shutdown.py#L1174) | c | Unchanged. Real independent watchdog expiry is the subject. Its 0.5-second timer setting and existing parent ceiling are retained; replacing the timer with controlled expiry would weaken this particular test. Watchdog #1658 is an out-of-scope known report, not reproduced here. |
| [`test_sync_group_cleanup_continues_after_unsignalable_group`, line 1214](test_acp_shutdown.py#L1214) | c | Unchanged. Deterministic injected signal failures verify continued cleanup. No cross-process observation race or elapsed-time assertion exists. This is correct cleanup-contract coverage, not a demonstrated current defect. |
| [`test_sync_cleanup_exceptions_do_not_escape_exit_boundary`, line 1236](test_acp_shutdown.py#L1236) | c | Unchanged. Cleanup exceptions must not escape the exit boundary. Child completion orders output checks. The watchdog is deliberately inert where callback exit is the subject, so the existing shutdown ceiling detects a hang rather than letting the fallback mask a broken callback. |
| [`test_signal_callback_rechecks_completed_installing_task`, line 1282](test_acp_shutdown.py#L1282) | c | Unchanged. Child queues cancellation and finishes setup without yielding, deterministically exercising the completed-task interleaving. The inert watchdog and existing shutdown ceiling preserve the requirement that the callback itself exits. |
| [`test_signal_deadline_preserves_observed_failure_during_resistant_drain`, line 1320](test_acp_shutdown.py#L1320) | a | Changed. Failure precedence previously depended on observation beating a real 0.5-second timer. Controlled expiry now follows the real `record_failure` call and a cancellation-resistant drain handshake. Exact exit code and sanitized diagnostics remain asserted. The parent shutdown ceiling is unchanged. Real watchdog timing remains covered separately; failure precedence is this test's subject. |
| [`test_local_proxy_signal_with_real_stdio_and_unix_socket`, line 1396](test_acp_shutdown.py#L1396) | c | Unchanged. An actual routed frame proves readiness before signaling across pipe, file, and TTY variants. Final diagnostics are collected after child exit. Production stdio paths remain exercised. The existing shutdown ceiling bounds the complete protocol without substituting a sleep for routed-frame readiness. |
| [`test_file_output_is_bounded_off_loop_and_preserves_errors`, line 1554](test_acp_shutdown.py#L1554) | c | Unchanged. `drain()` orders worker-thread output and error observation. The capacity assertion remains exact; there is no elapsed-time assumption to relax. |
| [`test_frame_delivery_is_bounded_and_terminal`, line 1608](test_acp_shutdown.py#L1608) | c | Unchanged. Terminal wait and thread join precede output checks. The capacity setting remains exact and completion, not a delay, orders observation. |
| [`test_frame_delivery_rejects_capacity`, line 1621](test_acp_shutdown.py#L1621) | c | Unchanged. Terminal failure and join are explicitly awaited. Capacity rejection is deterministic; no timing bound was changed. |
| [`test_frame_delivery_handles_partial_writes`, line 1636](test_acp_shutdown.py#L1636) | c | Unchanged. Complete output is checked only after terminal completion and join. Partial writes are injected directly, not produced by timing. |
| [`test_frame_delivery_sustained_ingress_is_ordered_and_bounded`, line 1647](test_acp_shutdown.py#L1647) | c | Unchanged. Waits on owned reservation state, then terminal completion. Polling observes a condition rather than assuming elapsed time proves progress. Exact output order and peak capacity remain asserted. |
| [`test_frame_delivery_protocol_failures_reach_owner`, line 1663](test_acp_shutdown.py#L1663) | c | Unchanged. Terminal failure and join order the owner-callback assertion. The failure is injected synchronously by the sink. |
| [`test_peer_disconnect_write_and_flush_are_reported`, line 1708](test_acp_shutdown.py#L1708) | c | Unchanged. Synchronously injected write and flush failures test propagation, not timing. No current product defect was demonstrated. |
| [`test_writer_close_uses_exact_finite_drain_close_and_abort_bounds`, line 1724](test_acp_shutdown.py#L1724) | c | Unchanged. Exact configured bounds of 2.0, 1.0, and 1.0 seconds are captured directly without measuring elapsed time. These bounds are the contract and must not be widened or replaced with loose timing expectations. |
| [`test_proxy_generation_teardown_retires_hosted_ids_grants_calls_and_workers`, line 1840](test_acp_shutdown.py#L1840) | a | Changed. `sleep 30` and the unrelated shell timeout could finish the worker independently of generation teardown. The lifecycle fixture removes both alternative completion paths. Ownership, teardown-completion, reaping, and empty-generation checks remain, as does the 120-second test-operation ceiling. |
| [`test_daemon_eof_retires_generation_before_client_grace`, line 1904](test_acp_shutdown.py#L1904) | a | Changed. Uses the same teardown-only shell. Existing ownership polling, five-second retirement bound, 30-second client-grace setting, and assertion that client grace is still active remain intact. These distinguish retirement from waiting for grace expiry; widening them would erode that distinction. |
| [`test_daemon_eof_quiesces_inflight_allow_session_response`, line 2009](test_acp_shutdown.py#L2009) | c | Unchanged. Drain-entry and cancellation events order EOF and final generation assertions. Five-second operation bounds and the 30-second client-grace setting remain: cancellation must occur while client grace is still active. |
| [`test_close_cancels_inflight_allow_session_response_before_clearing_grants`, line 2105](test_acp_shutdown.py#L2105) | c | Unchanged. Explicit drain-entry handshake, awaited close, and routing-task cancellation establish quiescence. Existing five-second operation bounds are failure guards, not synchronization substitutes. |
| [`test_daemon_eof_quiesces_inflight_session_transition`, line 2171](test_acp_shutdown.py#L2171) | c | Unchanged. Transition-entry and `_close_complete` synchronize assertions; subsequent release cannot resurrect the generation. Five-second operation bounds and 30-second client grace are retained to require quiescence before grace expires. |

## Hosted Tests

All references in this section are to `test_acp_hosted.py`. No changes to this
file were necessary.

| Test | Class | Unchanged Rationale and Bounds |
| --- | --- | --- |
| [`test_terminate_process_killpg_error_still_reaps_and_untracks`, line 54](test_acp_hosted.py#L54) | c | Explicitly models an exited leader and awaits reaping before asserting untracking. Signal errors are injected directly. This is cleanup-contract coverage, not a demonstrated current defect. |
| [`test_terminate_process_killpg_einval_propagates`, line 75](test_acp_hosted.py#L75) | c | Deterministic error injection verifies that unexpected signal errors are not hidden. No elapsed-time assumption or adjustable observation bound exists. |
| [`test_shell_and_python_cleanup_kill_owned_process_groups`, line 110](test_acp_hosted.py#L110) | c | Ownership event precedes close. Shell has no natural expiry and its tool timer is controlled. Close and request completion precede process assertions. The 120-second test guard remains unchanged; it is not used to infer ownership or reaping. |
| [`test_runtime_symlink_escape_requires_scope`, line 357](test_acp_hosted.py#L357) | c | Awaits successful shell completion before accessing the created symlink. The normal shell timeout is not used as a readiness signal and was not changed. |
| [`test_shell_uses_bin_sh_cwd_environment_and_bounded_streams`, line 484](test_acp_hosted.py#L484) | c | Awaits completed shell requests before checking exact output and truncation. No producer-startup assumption. Exact stream limits and the 60-second default-timeout assertion are retained. |
| [`test_shell_deadline_kills_pipe_holding_owned_grandchild`, line 558](test_acp_hosted.py#L558) | c | Already waits for atomic identity publication, captured `held` output, and shell-leader exit before expiring the controlled deadline. The one-second configured timeout remains part of the exact result assertion; the 120-second test guard remains unchanged. No real one-second startup race is introduced. |
| [`test_shell_and_python_default_timeout_is_60_seconds`, line 640](test_acp_hosted.py#L640) | a | Already avoids elapsed-startup measurement by observing deadline construction. Existing floating-point tolerance was not changed. Exact configured budget remains asserted; the derived-value comparison accounts only for arithmetic representation, not scheduling latency. The 120-second test guard remains. |
| [`test_timeout_comes_only_from_selected_profile`, line 675](test_acp_hosted.py#L675) | a | Same direct deadline observation verifies the selected profile rather than scheduler latency. The exact Python timeout, existing derived-value tolerance, and 120-second test guard are unchanged. |
| [`test_shell_cancel_and_close_kill_pipe_holding_owned_grandchild`, line 724](test_acp_hosted.py#L724) | c | Identity and leader-exit handshakes precede cancellation or close. It does not assert captured output, so no capture barrier is needed. Controlled shell timers prevent natural expiry from satisfying cleanup; the 120-second test guard remains. |
| [`test_hosted_provider_releases_project_kernel`, line 829](test_acp_hosted.py#L829) | c | Completed kernel operations and release boundaries precede ownership-transfer checks. The live-worker assertion concerns an intentionally retained kernel, not elapsed startup time. No bounds were changed. |
| [`test_python_result_has_exact_seven_keys`, line 931](test_acp_hosted.py#L931) | c | Awaits the kernel response before checking exact output and result shape. Completion is the synchronization boundary; the operation's normal timeout remains unchanged. |
| [`test_python_operator_commands_preserve_wire_contract_and_project_scope`, line 962](test_acp_hosted.py#L962) | c | Commands are awaited sequentially. Release, kill, and reuse observations follow completion boundaries, not sleeps. Existing operation timeouts remain unchanged. |
| [`test_shell_double_cancellation_reaps_without_signalling_killed_group_again`, line 1081](test_acp_hosted.py#L1081) | c | Explicit running and reaping events place both cancellations. Exact single-signal and reaping assertions remain. The 120-second test guard is only a hang ceiling; event handoffs determine the interleaving. |
| [`test_first_live_process_signal_denial_is_not_hidden`, line 1148](test_acp_hosted.py#L1148) | c | Deterministic denial injection verifies that production must not wait forever on an unsignalable live process. The fake wait fails immediately if called, so no timing-based inference is required. No current product defect was demonstrated. |

## Daemon Tests

All references in this section are to `test_acp_daemon.py`.

| Test | Class | Disposition, Reason, and Bound Rationale |
| --- | --- | --- |
| [`test_daemon_creates_owner_only_socket_and_removes_it`, line 80](test_acp_daemon.py#L80) | c | Unchanged. Awaits start and stop before filesystem assertions. Completion, not elapsed time, orders observation. |
| [`test_live_socket_is_not_unlinked`, line 107](test_acp_daemon.py#L107) | c | Unchanged. First listener startup completes before the second startup probe. No readiness delay or widened probe bound is needed. |
| [`test_stale_socket_is_revalidated_and_replaced`, line 168](test_acp_daemon.py#L168) | c | Unchanged. Stale socket is synchronously closed; replacement startup and connection are awaited. No cross-process publication race is assumed away. |
| [`test_stop_does_not_unlink_successor_socket`, line 188](test_acp_daemon.py#L188) | c | Unchanged. Successor installation precedes awaited stop; there is no competing publisher. The inode assertion is ordered by those operations. |
| [`test_single_peer_cap_refuses_second_with_actionable_error`, line 250](test_acp_daemon.py#L250) | c | Unchanged. First peer is held by an event, refusal is awaited, and held tasks are gathered on release. The production retirement wait is exercised rather than bypassed; it cannot retire the deliberately held peer. |
| [`test_clean_close_allows_immediate_reconnect`, line 289](test_acp_daemon.py#L289) | c | Changed. Silence could mean admission was still waiting, not successful reconnect. Both connections now acknowledge actual admission. The existing 0.05-second second-connection bound is preserved; the test still requires prompt reconnect and does not wait for server retirement before attempting it. |
| [`test_tcp_reset_retires_inflight_peer_and_allows_prompt_reconnect`, line 327](test_acp_daemon.py#L327) | c | Unchanged. Authentication, handler entry, transport death, peer-task retirement, and replacement response are explicitly observed. Existing 0.5-second handshakes, 1.5-second refusal read, and two-second retirement and replacement bounds remain. They bound prompt progress after specific events rather than substituting for those events. |
| [`test_eof_retires_write_only_inflight_peer_before_dispatcher_drain`, line 421](test_acp_daemon.py#L421) | c | Unchanged. Update delivery precedes EOF. Prompt transport-death notification rather than waiting for dispatcher drain is the contract. The 0.05-second notification bound remains below the fixture's 0.2-second dispatcher-stop bound; the 0.5-second readiness and update-read guards are unchanged. This coverage is not evidence of a current defect. |
| [`test_preauth_timeout_cancels_connection_runner`, line 502](test_acp_daemon.py#L502) | c | Unchanged. Actual auth-timeout cancellation and completed cleanup are the subject. The fixture's 0.01-second auth deadline remains; removing or widening it would change the tested path rather than fix synchronization. |
| [`test_admitted_peer_completion_retrieves_and_reports_failure`, line 528](test_acp_daemon.py#L528) | c | Unchanged. Waits for the owned task and completion callback without retrieving its exception through the test's wait operation. The 0.01-second auth timeout selects the timeout case; the one-second task wait bounds completion. Neither was changed. |
| [`test_shutdown_closes_at_most_four_peers_concurrently`, line 584](test_acp_daemon.py#L584) | a | Changed. Replaces the 0.01-second progress assumption with a saturation event. Maximum concurrency is also checked after shutdown completes. The exact concurrency requirement remains four, and production close deadlines are unchanged. |
| [`test_shutdown_aborts_after_cancel_deadline`, line 619](test_acp_daemon.py#L619) | c | Unchanged. Resistant peer and explicit release make abort and deadline behavior observable. The fixture's 0.01-second grace, cancel, and abort windows select escalation; these are the subject, not arbitrary readiness delays. No current defect was demonstrated. |
| [`test_total_shutdown_deadline_never_awaits_stuck_cleanup`, line 649](test_acp_daemon.py#L649) | c | Unchanged. The total shutdown deadline must interrupt stuck cleanup. The 0.01-second total deadline and 0.1-second outer guard remain. Widening them would hide or blur a violation of the bounded-stop contract. |
| [`test_authenticated_connection_outlives_preauth_deadline`, line 673](test_acp_daemon.py#L673) | c | Changed. Waits for completed authentication before the existing 0.03-second observation period. A not-yet-started task can no longer satisfy the survival assertion. The 0.01-second preauth deadline and observation duration are unchanged: survival past that deadline remains the subject. |
| [`test_postauth_watchdog_terminates_non_draining_peer`, line 708](test_acp_daemon.py#L708) | c | Unchanged. Non-draining authenticated-peer termination is the contract. The zero watchdog interval, 0.01-second drain deadline, and 0.1-second outer guard remain; they select and bound the real timeout path. This is not a demonstrated current product defect. |
| [`test_transport_dead_peer_skips_watchdog_drain_timeout`, line 751](test_acp_daemon.py#L751) | c | Unchanged. Drain-entry event precedes transport death. The 0.1-second completion guard remains below the one-second drain timeout, preserving the requirement to skip that timeout. The drain-entry guard also remains 0.1 seconds. |
| [`test_live_peer_watchdog_keeps_full_drain_window`, line 774](test_acp_daemon.py#L774) | c | Unchanged. Captures the exact 30-second configured timeout directly instead of measuring elapsed time. The full live-peer drain window is the contract; no relaxation is appropriate. |
| [`test_transport_death_retires_only_that_peer`, line 806](test_acp_daemon.py#L806) | c | Unchanged. Awaits peer completion before asserting close without transport abort. There is no sleep-based completion inference. |
| [`test_shutdown_grace_allows_peer_completion_without_cancellation`, line 846](test_acp_daemon.py#L846) | c | Unchanged. Writer close releases the peer via an event; awaited shutdown proves completion without cancellation. Production grace behavior is retained rather than shortened to force cancellation. |
| [`test_preauth_cancellation_resistance_is_post_abort_bounded`, line 919](test_acp_daemon.py#L919) | c | Unchanged. Abort explicitly releases the resistant runner. Existing 0.01-second auth and cancel windows, 0.02-second abort window, and 0.1-second outer guard preserve the post-abort boundedness contract. Owned-runner cleanup remains asserted. |
| [`test_connection_failure_leaves_separate_peer_and_runtime_turn_alive`, line 967](test_acp_daemon.py#L967) | c | Unchanged. Survivor readiness and owned-task completion order the isolation assertions. Event-loop yields place local task execution; they do not purport to prove cross-process startup. Existing operation behavior is unchanged. |
| [`test_close_bound_fits_inside_the_daemon_cancel_budget`, line 1089](test_acp_daemon.py#L1089) | c | Unchanged. Statically asserts that two SDK close intervals fit inside the daemon cancellation budget. This exact relationship is a contract, not an elapsed-time measurement or a demonstrated defect. |
| [`test_resistant_close_does_not_pin_a_cancelled_runner`, line 1104](test_acp_daemon.py#L1104) | c | Unchanged. Close-entry event precedes cancellation. Non-cancelling task wait checks the existing 0.5-second completion bound against the fixture's 0.01-second close intervals. One-second entry and two-second cleanup guards remain. Using `wait` avoids changing the subject with a second cancellation. |
| [`test_resistant_close_still_lets_the_daemon_retire_the_generation`, line 1140](test_acp_daemon.py#L1140) | c | Unchanged. Explicit close-entry handshake precedes `_finish_runner`; runner completion and absence of daemon failure are checked. The 0.01-second SDK close interval, 0.2-second daemon cancel and abort windows, one-second entry guard, and two-second completion and cleanup guards remain. Their budget relationship is intentional. |
| [`test_second_peer_is_refused_while_an_old_close_is_still_pending`, line 1183](test_acp_daemon.py#L1183) | c | Unchanged. Close is held through runner completion and a real second admission attempt. The 0.01-second close interval, one-second entry guard, 0.5-second runner bound, and two-second cleanup guard remain. The held close, not elapsed time, proves the old generation remains live. |
| [`test_capacity_returns_once_the_old_close_terminates`, line 1245](test_acp_daemon.py#L1245) | c | Unchanged. Explicit release and awaited task completion precede capacity checks. No timing threshold is used to infer termination. |
| [`test_an_abandoned_close_that_fails_keeps_admission_closed`, line 1271](test_acp_daemon.py#L1271) | c | Unchanged. Released failure is awaited before checking the fence and actual admission refusal. The one-second completion guard remains; `task.done()` is also asserted before accounting checks. |
| [`test_an_abandoned_close_that_honours_cancellation_returns_capacity`, line 1315](test_acp_daemon.py#L1315) | c | Unchanged. Started event precedes cancellation, and task cancellation is observed before checking capacity. Existing one-second start and completion guards are retained. |
| [`test_close_that_raises_on_forced_cancel_still_fences_admission`, line 1342](test_acp_daemon.py#L1342) | c | Unchanged. Close-entry handshake places cancellation; failure accounting precedes the real admission attempt. The 0.01-second close interval, one-second entry guard, 0.5-second runner bound, and one-second cleanup guard remain. Forced cancellation is the intended failure trigger. |
| [`test_close_that_raises_in_the_first_grace_interval_fences_admission`, line 1405](test_acp_daemon.py#L1405) | c | Changed. A single event-loop yield did not prove failure occurred inside the first grace interval. The close now releases only when the SDK waits on that specific close task after cancellation. The one-second grace interval, one-second entry guard, two-second runner bound, and one-second cleanup guard are unchanged. Synchronization selects the branch without widening its budget. |
| [`test_close_failure_surfaced_by_the_shield_fences_admission`, line 1474](test_acp_daemon.py#L1474) | c | Unchanged. Directly awaits the real runner's close failure before testing the admission fence. The two-second outer guard remains; no cancellation is injected because direct shield failure is the subject. |

## Dedicated Helpers

| Helper | Class | Disposition and Reason |
| --- | --- | --- |
| Shutdown [`_journal_source`, line 27](test_acp_shutdown.py#L27) | c | Unchanged. Journal writes use `os.write`; tee acknowledgements order forwarded wakeup bytes. `Timer.start()` returning does not order timer-thread diagnostics, which is why the diagnostic consumer needed an added wait. This audit did not redesign watchdog or tee behavior. |
| Shutdown embedded [`_journal_flush`, line 52](test_acp_shutdown.py#L52), install/close wrappers, signal wrapper, and exit wrapper | c | Unchanged. The tee protocol flushes signal-delivery evidence before the corresponding journal or exit observation. A main-thread marker must not be mistaken for proof of a separate timer-thread marker. No claim is made here that this audit resolved all possible watchdog/flush interactions; #1658 remains out of scope and was not reproduced. |
| Shutdown embedded `JournalTimer` and `InputTimer`, within [`_journal_source`](test_acp_shutdown.py#L27) | c | Unchanged. `JournalTimer` records real timer execution. `InputTimer` distinguishes explicit `b'x'` expiry from EOF or cancellation. Its controlled callback is reused where expiry ordering, rather than real elapsed time, is the test requirement. |
| Shutdown [`_shutdown_ceiling`, line 169](test_acp_shutdown.py#L169) | c | Unchanged. Diagnostic reads deliberately work while child pipes remain open. Default 120-second ceiling is retained; diagnostic self-tests shorten only their own ceiling after their stage handshake. |
| Shutdown [`_accept_unavailable_backend_risk_for_lifecycle`, line 196](test_acp_shutdown.py#L196) | c | Unchanged. Explicit test consent enables lifecycle coverage where confinement is unavailable. It does not synchronize or alter timers, and available backends still must confine. |
| Shutdown [`lifecycle_shell`, line 202](test_acp_shutdown.py#L202) | a | Added solely for the two generation tests. Controls the unrelated tool timer and prevents natural shell exit. Existing test and retirement ceilings remain unchanged, so only generation teardown can satisfy worker completion. |
| Shutdown [`_await_diagnostic`, line 587](test_acp_shutdown.py#L587) | c | Unchanged and reused. Polls for the required marker while the child is alive, under its existing 30-second bound. It is condition synchronization, not a retry of a failed test or a fixed-delay assumption. |
| Shutdown [`_signal_exit_protocol`, line 608](test_acp_shutdown.py#L608) | c | Unchanged. Ordered `ready`, `armed`, `terminated`, and `draining` observations precede controlled expiry or escalation. Existing `after_armed` hook synchronizes timer-thread evidence while the child is alive. The default 120-second ceiling remains unchanged. |
| Shutdown [`_assert_generation_empty`, line 1582](test_acp_shutdown.py#L1582) | c | Unchanged. Asserts state owned by the router and provider after caller-established quiescence. It does not compare ambient event-loop or process state. |
| Shutdown [`Partial`, line 1630](test_acp_shutdown.py#L1630) | c | Unchanged. Deterministically restricts each write to two bytes. The caller waits for terminal completion and joins delivery before asserting output. |
| Hosted [`_connected`, line 24](test_acp_hosted.py#L24) | c | Unchanged. Awaits initialization and initialized notification before returning the provider and connection. No readiness sleep is involved. |
| Hosted [`shell_timers`, line 94](test_acp_hosted.py#L94) | c | Unchanged. Supplies controlled tool timers so lifecycle tests do not race natural expiry. Tests explicitly reschedule when deadline behavior is their subject; existing outer guards remain. |
| Hosted [`_child_identity`, line 525](test_acp_hosted.py#L525) | c | Unchanged. Waits for the child's published identity rather than assuming process startup after a delay. Its callers retain their 120-second test guards. |
| Hosted [`_assert_process_stopped`, line 534](test_acp_hosted.py#L534) | c | Unchanged. Polls the identified process until it is absent or a zombie, rather than treating signal delivery as proof of process termination. Caller bounds remain unchanged. |
| Hosted [`_pipe_holding_grandchild_command`, line 545](test_acp_hosted.py#L545) | c | Unchanged. Explicitly flushes output, publishes identity by atomic replacement, and remains alive with `signal.pause()`. Output-capture synchronization is additionally performed by the test that asserts that output. |
| Hosted [`shell_deadlines`, line 615](test_acp_hosted.py#L615) | a | Unchanged. Observes deadline construction using the producer's clock read instead of elapsed subprocess startup time. Leaves asyncio's own clock alone and retains existing derived-value comparisons. |
| Hosted [`_unit_backend_on_unsupported_platform`, line 1063](test_acp_hosted.py#L1063) | c | Unchanged. Supplies the established non-macOS lifecycle backend and sanitized environment. It does not replace subprocess completion or timer synchronization. |
| Daemon [`_short_home`, line 32](test_acp_daemon.py#L32), `_bundle`, line 36, and `_Channels`, line 27 | c | Unchanged. Supply isolated short socket paths and minimal daemon dependencies. They do not establish readiness or use elapsed-time assumptions. |
| Daemon [`_Transport`, line 206](test_acp_daemon.py#L206), and [`_Writer`, line 214](test_acp_daemon.py#L214) | c | Unchanged. Record owned close/abort/write state. Async methods yield locally; callers await operations or explicit events instead of inferring remote flush completion from those yields. |
| Daemon [`_resistant_close`, line 1034](test_acp_daemon.py#L1034) | c | Unchanged. Entry event places cancellation, release event is the cleanup escape hatch, and captured tasks allow the caller to drain its own work. Existing caller bounds remain unchanged. |
| Daemon `_StubAgent`, line 1061, and `_eof_reader`, line 1066 | c | Unchanged. Supply deterministic connection setup and already-buffered EOF; EOF does not require a timing delay before the reader starts. |
| Daemon [`_spawn_stdio_runner`, line 1072](test_acp_daemon.py#L1072) | c | Unchanged. Starts the real runner and feeds EOF. Its callers wait for close-entry events before cancellation or retirement checks; the helper's yield is not used as proof of close readiness. |
| Daemon local `observe_grace` in [`test_close_that_raises_in_the_first_grace_interval_fences_admission`](test_acp_daemon.py#L1405) | c | Added. Releases only when the wait contains the captured close task, then delegates to real `asyncio.wait` with unchanged arguments. The patch is scoped to the SDK module's asyncio namespace rather than changing the shared asyncio module. |

## Other Inspected Tests

The inventory above includes every test identified as relevant to the
cross-process timing, flush, teardown, or related bound-contract audit in the
report. Remaining tests in the three files were inspected but were not classified
as timing/flush candidates: synchronous configuration and platform selection,
filesystem validation, schema validation, mock-only descriptor ownership, and
generation-identity bookkeeping. No changes were made to those tests.

## Verification

The following results belong to the completed code audit, before this
documentation-only addition:

| Command | Result |
| --- | --- |
| `uv run pytest -q tests/test_acp_shutdown.py` | 192 passed |
| `uv run pytest -q tests/test_acp_hosted.py` | 45 passed |
| `uv run pytest -q tests/test_acp_daemon.py` | 55 passed |
| `uv run pytest -q tests/test_acp_shutdown.py tests/test_acp_hosted.py tests/test_acp_daemon.py` | 292 passed |
| `uv run pytest -q` | 17,121 passed, 51 skipped, 100 warnings |
| Scoped `git diff --check` | Clean |

Verification ran on Linux with Python 3.11. The initial full-suite command hit
the terminal execution limit; a subsequent completed invocation had a longer
command allowance, not changed test bounds. The full suite emitted an unraisable
subprocess-transport warning during `test_acp_stdio.py`; its origin was not
established by this audit. That warning was not classified as a demonstrated
current ACP product defect.
