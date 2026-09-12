# Cross-process timing inventory: issue 1669 (partial fix)

This records the source-level audit of the remaining process, lifecycle, and
protocol timing candidates discussed in issue 1669. It documents a **partial
fix**, not completion of the issue, proof that every test is race-free, or a
claim that the changes suggested here have been implemented.

## Scope, method, and evidence

- Read CONTRIBUTING.md, test bodies, relevant shared helpers, and relevant
  production deadline, signal, capture, and lock implementations. Searches for
  process creation, sleeps, waits, deadlines, and helper consumers identified
  candidates; historical timing-audit documents were leads, not evidence.
- This document preserves every per-test reference in the consolidated report.
  It is an exhaustive transcription of that report's candidate inventory, not
  a machine-verified enumeration of every timing dependency in the repository.
- No tests, mutation checks, load experiments, or platform-specific reproductions
  were run for this audit. No new product defect was demonstrated. Historical
  regression comments and negative controls do not prove a current product bug.
- The audit made no code changes. This document is its only requested output.
  Passing scoped and full suites would be required evidence for subsequent test
  changes; this source-only record supplies neither result.
- Paths below are repository-relative. `line test_name` means
  `tests/<file>:line`, at the source snapshot reviewed. All parameterized variants
  are included unless a qualification narrows the finding. Lines can drift.
- Ordinary synchronous Git setup, static policy/formatting checks, and completed
  import/CLI probes were not classified as timing-sensitive merely because they
  use subprocesses. Some adjacent fake-process tests are retained to distinguish
  simulated contracts from real child startup.
- The seven separately audited files are excluded here:
  `test_acp_shutdown.py`, `test_acp_hosted.py`, `test_acp_daemon.py`,
  `test_worklink_worker_parity.py`, `test_worklink_factory_supervisor.py`,
  `test_pollers.py`, and `test_pytest_timeout_config.py`. Other agents own their
  audit documents. Shared-helper conclusions below do not extend into them.

## Classification

Only **a** and **c** are assigned. **b** would require a proven current product
defect; none is established here.

- **a**: unnecessary timing, overspecification, or an insufficient test witness.
- **c**: real synchronization, lifecycle, timeout, or protocol contract.
- **a/c**: a real contract with a residual test assumption; this is not a claim
  that the product is defective or that a failure was reproduced.
- **Synchronized**: owned event/pipe/queue/process completion, controlled time,
  or a directly injected outcome establishes the relevant ordering.
- **Watchdog**: finite failure guard around that ordering. A short watchdog alone
  is not evidence of a race; it may still be sensitive to extreme scheduling.
- **Yield**: an intermediate observation uses event-loop scheduling rather than
  an explicit entry event. This limitation alone does not prove a race.
- **Residual**: finite lifetime, competing behavioral deadlines, elapsed-time
  oracle, missing state witness, or a claim stronger than the helper proves.

In each file section, a group label applies to every listed test. Exceptions and
mixed aspects are stated explicitly. A test can have synchronized readiness and
still retain a residual completion/lifetime assumption. `sleep(0)` is not the
same as a wall-clock delay; polling cadence is not necessarily the ordering
mechanism. Cleanup success does not automatically prove natural completion.

## Residual inventory

### tests/test_repo_tools.py

**a/c, residual:**

- `3270 test_real_subprocess_runner_enforces_wall_time_and_capture_cap`: timeout
  case uses `sleep 2` against 50ms; product deadline begins after Popen/setup.
  Output-cap case must produce enough output before a separate 2s deadline.
- `2042 test_project_test_retains_builtin_hang_dump_after_stderr_truncation`:
  nested pytest diagnostic threshold 0.1s versus synthetic sleep 3s; the sleep
  is margin, not a witness that the required worker stack exists at dump time.
- `2223 test_builtin_hang_diagnostic_configuration_leaves_fast_test_quiet`:
  same helper; quiet output depends on finishing before the 0.1s threshold.

**c, synchronized + watchdog; residual harness caveat:**

- `2773 test_project_test_real_executor_preserves_active_lease_for_later_commit`:
  readiness/result pipes establish ordering. It installs SIGALRM 30s and restores
  the handler, not a previously armed alarm. This is a harness interaction to
  review, not a demonstrated product defect.

### tests/test_acp_ssh.py

**a/c, residual:**

- `406 test_ssh_signal_kills_owned_child_before_slow_teardown`: readiness orders
  the signal, but child and injected teardown use finite 60s sleeps.
- `602 test_actual_subprocess_cancellation_terminates_child`: handler installation
  is witnessed and lifetime is indefinite, but requiring its marker assumes
  handler execution before TERM grace permits KILL.
- `641 test_stubborn_child_is_killed_and_reaped`: same acknowledgment/grace
  competition. The 60s handler delay is not the child's lifetime: it returns to
  an infinite loop.
- `696 test_cancellation_during_writer_cleanup_reaps_child`: cleanup entry is
  witnessed; real child lifetime is a finite 60s sleep.
- `744 test_unread_stdout_backpressure_cleanup_reaps_child`: PID publication
  plus 50ms does not witness actual downstream backpressure before cancellation.

**c, synchronized + watchdog; limited claim coverage:**

- `227 test_actual_subprocess_pumps_without_first_output_timeout_and_sanitizes_environment`:
  80ms delays first output, not child lifetime. It proves tolerance of that delay,
  not absence of every possible first-output deadline.

### tests/test_acp_proxy.py

**a/c, residual:**

- `1306 test_disconnect_and_cancel_kill_active_execution_but_release_idle_kernel`:
  active execution markers precede finite 30s sleeps; activity can expire
  independently. Five-second stage/reap waits are additional watchdogs.
- `1456 test_owned_child_cleans_on_atexit_and_each_supported_signal`: ownership
  JSON orders shutdown, but shell child is `sleep 30`; eventual absence can cease
  to prove shutdown killed a still-live child.

Permission-helper consumers and their yield limitation are enumerated below.

### tests/test_acp_sessions.py

**a/c, residual:**

- `627 test_admin_hands_permissions_precede_execution_and_preserve_raw_arguments`:
  synchronous `.result(timeout=1)` and bounded polling impose short stage windows.
- `883 test_permission_wait_survives_until_answer_or_prompt_cleanup`: approval
  path waits 35s and asserts elapsed time, with 3s thread/stage joins. Permission
  delivery itself now uses an owned queue get without that 3s timeout.
- `1785 test_slow_replay_renews_waiting_prompt_drain_budget`: repeated 100ms
  timers and elapsed lower bound compete with the real stall deadline.
- `2356 test_list_changed_revalidates_only_fresh_tools_list_serially`: fake
  tools/list sleeps 10ms to manufacture overlap instead of holding an entry gate.
- `3832 test_daemon_emits_permission_for_every_call_and_stores_no_grant`:
  queue-backed wire ordering, but audit visibility has a fixed 20 x 10ms window.

### tests/test_acp_updates.py

- **a/c, residual** `75 test_slow_dispatcher_progress_renews_drain_budget`:
  repeated 100ms timers, 15s guard, and elapsed lower bound exercise renewal
  through real wall time rather than controlled progress/expiry.
- **c, synchronized + yield + watchdog**
  `441 test_close_suspends_then_times_out_when_full_worker_is_blocked`: publisher
  entry and full queue are witnessed. Intermediate close-suspended assertion
  follows one yield; product timeout is 20ms, guards 1s. Not fully entry-gated,
  but not a missing-backpressure-witness case.

### tests/test_worklink_cli.py

**a/c, residual:**

- `568 test_stop_clears_state_claim_and_label_and_missing_is_noop`: finite 60s
  child supplies intended live state.
- `1226 test_factory_stop_cancels_verified_process_group`: finite 60s child,
  followed by a 5s wait.

### tests/test_worklink_continuation.py

- **a/c, residual** `1065 test_default_runner_bounds_hung_external_commands`:
  100ms timeout versus real child sleeping 60s. This is a command-timeout test,
  not an active-run-association test (correction to an earlier audit summary).

### tests/test_worklink_orchestrator.py

- **a/c, residual for local-compute variants**
  `6934 test_factory_recovery_uses_run_id_first_lock_resume_and_authoritative_status`:
  replacement uses `sleep(300)` despite explicit pruning/process observations.
  Fake-compute variants do not inherit the child-lifetime assumption.

### tests/test_worklink_autonomy.py

- **a/c, residual** `1323 test_worklink_run_arbiter_gate_does_not_block_loop`:
  100ms threading.Timer releases arbiter; loop progress must be seen first.
  One-second fallback also returns normally.
- Shared real-poller helper consumers are exhaustively listed below. Their
  dispatch contracts are **c**; claiming locally bounded reads or natural child
  exit from the helper is **a/c, residual**. `_run_poller` reads stdout to EOF
  before `communicate(timeout=30)`, so 30s does not bound the read loop.
  `_wait_for_fake_run_bin_exit` can TERM children, then return successfully:
  eventual cleanup is not necessarily natural exit.

### tests/test_commitments_store.py

- **a/c, residual**
  `764 test_writer_lock_is_bounded_and_released_on_process_death`: child reports
  lock acquisition and blocks on stdin; readiness/lifetime are synchronized.
  Extra `elapsed < 2` imposes a wall-clock scheduling requirement.

### tests/test_git_tracking.py

- **a, residual** `161 test_empty_porcelain_no_commit_no_push`: unnecessary
  100ms wait before a negative event assertion after establishing no task.

## Shell and kernel: synchronized inventory

### tests/test_shell_jobs.py

`_held_job` at line 39 owns readiness/release FIFOs, live-child observation,
process/callback completion and pipe cleanup, without a finite lifetime.

**c, synchronized** (capture/completion or controlled visibility/eviction):

```text
180  test_undeclared_output_is_unchanged
202  test_redacted_capture_error_does_not_log_values
242  test_redacted_callback_error_does_not_log_values
273  test_spawn_captures_stdout_and_stderr
289  test_finished_jobs_release_all_pipe_descriptors
312  test_output_write_failure_keeps_draining_and_completes
377  test_output_write_bound_discards_excess_without_wedging
421  test_nonzero_exit_marked_exited_error
434  test_on_complete_fires_after_exit
457  test_on_complete_error_isolated_from_registry
476  test_on_complete_runs_for_nonzero_exit
496  test_spawn_captures_channel_id
511  test_env_overlay_sets_value_visible_to_child
524  test_env_overlay_none_unsets_inherited_var
546  test_env_overlay_default_inherits_parent_env
561  test_cwd_kwarg_honored_by_subprocess
582  test_running_jobs_visible_immediately
591  test_short_finished_job_not_visible_after_grace
603  test_finished_job_persists_in_all_jobs_after_grace
625  test_read_output_supports_stream_filter
640  test_read_output_tail_lines_truncates
710  test_shell_job_snapshots_running_scope_filters
749  test_read_output_marker_fires_when_file_fits_one_chunk_but_has_extra_lines
782  test_read_output_no_marker_when_file_has_fewer_lines_than_tail
803  test_evict_stale_removes_old_finished_job_and_unlinks_files
828  test_evict_stale_preserves_running_job
840  test_evict_stale_preserves_recently_finished_job
853  test_registry_startup_reclaims_stale_restart_residue
873  test_scheduled_eviction_does_not_require_later_spawn
935  test_spawn_triggers_eviction_of_old_jobs
961  test_backgrounded_grandchild_does_not_block_waiter
1056 test_spawn_refuses_beyond_live_job_cap
```

At 961 the descendant holds pipes until release; completion is asserted while
it remains alive, bounded drain joins are checked, and owned threads are joined.

**c, synchronized fake spawn/stream failure:**

```text
102 test_redaction_precedes_capture_and_tail_limits
145 test_running_output_withholds_split_secret
190 test_redacted_spawn_exception_does_not_echo_values
927 test_spawn_failure_removes_opened_output_files
```

### tests/test_spawn_cancellation.py

**c, synchronized**: readiness follows group/handler setup; polling is observation
cadence, not the child's lifetime.

```text
91  test_session_leader_isolation_pid_equals_pgid
121 test_sigterm_to_process_group_kills_grandchildren
166 test_sigkill_fallback_when_sigterm_ignored
```

### tests/test_shell_exec.py

**c, synchronized:**

- `339 test_configured_project_test_timeout_is_named_and_output_is_bounded`:
  injected TimeoutExpired.
- `386 test_project_test_capture_discards_output_past_hard_byte_cap`: real
  producer with timeout=None; cap and reap are the oracle.

### tests/test_shell_async_wiring.py

**c, synchronized fake registry or directly instrumented refusal:**

```text
98   test_bash_async_spawns_and_returns_job_id
113  test_bash_async_accepts_explicit_cwd
1167 test_repo_review_bash_async_graph_refuses_legacy_commands_before_spawn
```

In the first two, `sleep 5` is argv data, not an actual sleeper.

### tests/test_shell_env.py

**c, synchronized**: fork/exec pipe and completion or completed reader result.

```text
522 test_admitted_file_readers_cannot_disclose_child_environment
544 test_admitted_file_readers_still_read_repository_files
571 test_real_jq_cannot_read_parent_credentials_and_still_filters_json
```

### tests/test_acp_python_kernel.py

**c, synchronized, with module watchdog**: owned worker/task state, markers,
joins, or controlled expiry. Module-local execution timers do not demand cold
interpreter startup inside a tiny execution deadline.

```text
35   test_stopped_handles_process_exit_during_proc_read
64   test_repl_executes_statements_and_reprs_final_expression
85   test_exception_preserves_partial_namespace_and_traceback
103  test_stream_and_text_byte_bounds
119  test_multibyte_text_bound_keeps_codepoints_and_counts_raw_bytes
170  test_exception_utf8_bound_and_omitted_byte_count_are_exact
198  test_timeout_and_crash_discard_namespace
238  test_timeout_and_crash_retain_streams_exactly
281  test_crash_result_survives_killpg_permission_error
339  test_live_control_eof_is_killed_without_waiting_for_worker
379  test_direct_exit_still_kills_owned_process_group_descendant
411  test_sessions_parallel_and_namespaces_isolated
447  test_release_reuses_live_namespace_through_canonical_symlink
498  test_bound_symlink_cwd_stays_frozen_and_replacement_is_rejected
532  test_other_session_cannot_execute_kill_or_release_owned_cwd
570  test_kernels_command_after_close_is_rejected
589  test_control_command_waiting_admission_is_rejected_when_close_begins
620  test_execution_waiting_state_lock_is_rejected_when_close_begins
666  test_operator_listing_release_and_kill
697  test_capacity_refuses_when_all_kernels_owned
723  test_capacity_evicts_least_active_detached_kernel_and_descendant
773  test_function_import_and_loaded_data_persist
792  test_queue_wait_is_outside_timeout_and_other_session_is_parallel
891  test_queued_and_active_cancellation_have_distinct_worker_effects
947  test_worker_and_idle_task_are_lazy
1016 test_idle_retirement_discards_worker_and_next_call_is_fresh
1040 test_registered_waiter_wins_idle_retirement_race
1081 test_real_waiter_registered_at_idle_expiry_keeps_worker
1116 test_late_background_output_is_discarded_between_calls
1145 test_launch_socket_modes_cwd_and_environment_are_exact
1205 test_output_setup_and_protocol_failures_discard_worker
1240 test_deadline_expires_during_spawn_handshake_and_output_setup
1306 test_bounded_wait_retains_ownership_until_eventual_direct_reap
1395 test_adoption_compares_complete_spawn_policy
1421 test_retire_owned_does_not_kill_another_sessions_kernel
1454 test_parent_output_reads_retained_inode_not_worker_symlink
1487 test_parent_output_creation_uses_pinned_scratch_directory
1507 test_directory_substituted_for_output_never_retains_kernel_lock
1531 test_kernel_signal_denial_requires_confirmed_process_exit
1577 test_kernel_repeated_cancel_does_not_repeat_successful_signal
```

Static plain-exec/no-ipykernel and pure text-formatting tests are not lifecycle
timing candidates.

### tests/test_acp_unconfined_fallback.py

**c, synchronized / watchdog**: permission ownership, consent, lifecycle
completion, or module-local controlled execution expiry.

```text
66  test_operator_acceptance_enables_both_engines_with_hardened_environment
112 test_pending_risk_prompt_bounded_and_cancelled_reply_cannot_install
139 test_stale_risk_approval_never_installs
167 test_risk_grant_is_not_shared_with_second_session_or_rebind
187 test_every_unconfined_python_result_labels_mode
217 test_adopted_kernel_warning_uses_this_calls_mode
280 test_unconfined_shell_failure_and_timeout_label_mode
337 test_risk_rejection_remains_final_across_connection_reset
356 test_risk_timeout_is_final_and_audited_without_command
374 test_child_runtime_failure_never_downgrades
393 test_unconfined_startup_errors_are_labelled
410 test_backend_loss_retires_existing_kernel_only_after_consent
```

## Proxy and transport: synchronized inventory

### tests/test_acp_ssh.py

**c, synchronized fake process/protocol or controlled timeout:**

```text
115 test_stop_child_waits_then_terminates_and_kills
126 test_stop_child_reports_finite_kill_failure
255 test_local_spawn_bound
322 test_ssh_proxy_propagates_only_selected_local_profile_timeout
376 test_signal_does_not_hide_already_observed_ssh_failure
```

**c, synchronized + watchdog**, real protocol/process completion:

```text
166 test_hosted_shell_allows_terminal_environment_but_strips_proxy_secrets
477 test_real_ssh_proxy_uses_local_router_and_preserves_foreign_daemon_bytes
516 test_ssh_proxy_uses_local_session_grants
574 test_actual_subprocess_failure_is_sanitized_and_reaped
587 test_early_child_failure_cancels_open_client_stdin
```

Hosted-shell setup includes a yield and state polling, not a cold-start execution
deadline. Other SSH candidates appear in the residual inventory.

### tests/test_acp_proxy.py

**c, synchronized / watchdog**, hosted lifecycle or protocol completion; shared
setup includes scheduling yields:

```text
618  test_hosted_disconnect_revokes_session_grants
1046 test_plain_client_without_mcp_capability_can_call_hosted_hands
1063 test_session_new_first_python_call_has_fresh_empty_namespace
1097 test_router_intercepts_only_ids_minted_by_current_generation
1118 test_session_new_captures_cwd_for_hosted_operations
1226 test_load_always_retires_hosted_state_and_failed_provisional_state
1275 test_load_releases_kernel_and_next_python_reuses_namespace
1532 test_typed_directional_ids_boolean_rejection_and_hosted_cancellation
1588 test_hosted_failures_are_supervised_and_generation_state_is_bounded
1621 test_client_session_cancel_tombstones_hosted_request
1655 test_hosted_error_response_writer_failure_fails_generation
1681 test_grace_period_stream_failure_is_propagated
1695 test_live_and_cumulative_hosted_connection_bounds
1723 test_local_proxy_connects_and_stdout_contains_only_protocol
1759 test_local_proxy_propagates_only_selected_profile_timeout
1817 test_local_proxy_start_and_protocol_failures_close_connection
2008 test_real_command_path_flow_and_secret_negative_surfaces
2086 test_invalid_key_reaches_actual_daemon_rejection_through_credential_path
```

- At 1588/1655 the 1s waits observe explicit failure signals, not assumed startup.
- At 1621 the producer is now indefinite, not an expiring shell sleep.
- `owned_process_reaped` (185) has a 5s polling guard; it is the reap helper,
  not the hosted-reply helper. `hosted_router` (67), `call_hosted_python` (125),
  and `connect_hosted` (153) supply setup/reply state observations.

**c, yield limitation**, owned permission tasks: `start_scope_permission` (2116)
retains the owned task and yields once at 2143; no explicit permission-publication
entry event is awaited by that helper.

```text
2149 test_scope_permission_is_local_path_only_and_separate_from_wrapper_grants
2213 test_scope_permission_rejects_unoffered_cancelled_and_malformed_answers
2233 test_scope_permission_timeout_and_late_answer_fail_closed
2259 test_scope_permission_cannot_outlive_its_hosted_request
2299 test_scope_permission_requires_owned_hosted_task_and_bounds_requests
2332 test_scope_permission_allows_only_one_outstanding_request_per_session
2459 test_unconfined_risk_warning_is_fixed_separate_and_session_local
2539 test_unconfined_permission_is_bounded_and_needs_owned_client_request
```

At 2233 the short timeout is a permission contract, not real child startup.

**c, synchronized / watchdog**, actual scope/fallback round trips:

```text
2372 test_hosted_scope_tool_round_trip_queries_grants_and_final_rejection
2569 test_unconfined_execution_real_operator_round_trip_and_final_state
```

### tests/test_acp_transport.py

**c, synchronized / watchdog**: controlled writers, EOF, gates, cancellation,
and explicit before/after deadline witnesses.

```text
39  test_parse_error_response_preserves_connection_for_next_request
61  test_repeated_parse_errors_hit_connection_limit
75  test_pump_stream_copies_bytes_and_half_closes
86  test_bidirectional_pump_awaits_both_directions
131 test_close_writer_drain_timeout_escalates_to_close
143 test_close_writer_close_timeout_escalates_to_abort
206 test_pump_allows_slow_reader_until_peer_eof
253 test_peer_eof_cancels_blocked_opposite_pump
272 test_force_close_deadline_bounds_writer_cleanup
307 test_drain_deadline_before_and_after_witnesses
339 test_close_deadline_before_and_after_witnesses
366 test_abort_wait_deadline_before_and_after_witnesses
414 test_force_close_deadline_before_and_after_witnesses
```

At 206 blocked-drain entry and attempted deadline introduction are observed;
backpressure is not inferred from elapsed time.

### tests/test_acp_relay.py

**c, synchronized:**

```text
23 test_relay_output_retries_partial_writes
30 test_relay_pipe_output_uses_nonblocking_event_loop_transport
52 test_relay_round_trip_and_cleanup
85 test_dead_relay_client_promptly_closes_daemon_connection
```

- **c, simulated timeout contract** `158 test_relay_connect_is_bounded`:
  same-loop fake connector sleeps 10s against 10ms timeout; no real child startup.

### tests/test_acp_stdio.py

**c, synchronized**: owned descriptors, writes, flushes, capture.

```text
10 test_stdout_reservation_routes_python_output_to_stderr
27 test_direct_fd_and_import_banner_cannot_contaminate_protocol_stdout
```

### tests/test_acp_credentials.py

**c, synchronized + watchdog**: real PTY prompt bytes precede secret input;
prompt/select and cleanup deadlines guard observed protocol states.

```text
152 test_read_secret_from_tty_prompts_and_reads_over_a_real_terminal
177 test_default_prompt_reads_the_controlling_terminal_and_suppresses_echo
```

## Sessions, updates, and confinement: synchronized inventory

### tests/test_acp_sessions.py

**c, synchronized / watchdog**: owned core/provider gates, queue/drain completion,
generation identities, cancellation boundaries, or explicit transport state.

```text
301  test_dead_update_forwarder_fails_prompt_without_wedging_session
352  test_reauthenticate_fence_still_dispatches_session_cancel
1114 test_cancelled_bound_turn_terminalizes_open_tools
1144 test_one_active_prompt_per_session_and_guard_releases
1162 test_connection_replacement_old_cleanup_preserves_successor_ownership
1199 test_replacement_retirement_waits_for_grace_then_completes_cancel_window
1273 test_replacement_retirement_force_quarantines_old_and_preserves_successor
1418 test_transport_death_releases_prompt_blocked_on_update_delivery
1451 test_transport_death_terminalizes_provider_call_in_persisted_session
1557 test_sweep_runs_off_loop_once_per_interval
1615 test_detach_waits_for_prompt_before_reloading
1712 test_stalled_read_peer_prompt_then_load_is_bounded
1832 test_detaching_state_blocks_prompts_without_unbinding_replacement
2396 test_transport_teardown_is_generation_scoped_and_requires_load
2414 test_active_prompt_permission_uses_admitted_snapshot_and_is_once_scoped
2972 test_progress_token_collision_cleanup_preserves_successor_ownership
3049 test_explicit_cancel_drains_accepted_tool_and_terminalizes_before_response
3097 test_journal_boundary_linearizes_prepared_delivery_sent_and_close
3146 test_cancel_timeout_dirties_execution_and_requires_fresh_load
3249 test_idle_and_repeated_cancel_are_structured_owned_noops
3265 test_oversized_cancel_bounds_retained_and_logged_id_without_cancelling_live_turn
3297 test_repeated_generation_retirement_releases_maps_and_peers_but_keeps_replay
3327 test_detach_cancels_turn_and_reload_reconstructs_released_journal
3571 test_real_resistant_provider_cancel_requires_authenticated_load_with_fresh_key
3686 test_journal_send_and_fsync_failures_preserve_prepared_state
3718 test_journal_multi_event_cancel_terminals_are_durable_and_ordered
3742 test_cancel_boundary_prevents_post_boundary_model_registration
3777 test_cancel_boundary_prevents_permission_and_mcp_registration
```

- At 1418 cleanup guards are tight, but blocked delivery is witnessed.
- At 1712 shielded tasks prevent a harness timeout from rescuing a missing bound.
- At 3571 provider entry is witnessed and cancellation held on a release event.
- **c, yield limitation** `1868 test_load_replay_excludes_live_prompt_publication`:
  replay entry is witnessed; new-prompt-not-entered assertion follows one yield.

### tests/test_acp_updates.py

**c, synchronized / watchdog:**

```text
37  test_stalled_dispatcher_join_is_bounded
59  test_unusable_update_client_rejects_further_writes
480 test_close_backstop_reports_a_cancellation_resistant_publisher
515 test_first_delivery_failure_is_retained_and_remaining_queue_is_acked
530 test_terminal_failure_is_recorded_before_nominal_success_queue_drains
548 test_terminal_publish_failure_stops_publication_but_closes_all_tools
672 test_submit_overflow_cancels_owner_quarantines_follow_on_events
```

- **c, synchronized + yield** `423 test_fifo_pressure_and_drain_wait_for_every_update`:
  publisher entry/release are owned; intermediate drain.done() follows a yield,
  then full ordered output is asserted.
- Tests 75 and 441 are classified in the residual inventory. Presentation/mapping
  tests use completed drain/close and add no separate timing scenario.

### tests/test_acp_execution_scope.py

**c, synchronized / watchdog:**

```text
72  test_file_kind_is_frozen_before_permission_reply
104 test_pending_bounded_and_late_reply_cannot_install
139 test_real_shell_and_persistent_python_scope
220 test_adopter_cannot_inherit_extra_path
308 test_approved_path_replaced_by_symlink_does_not_widen_python
331 test_children_do_not_inherit_parent_protocol_input
```

At 331 input/EOF and communicate establish completion, not a readiness sleep.

### tests/test_acp_confinement.py

**c, synchronized / watchdog**: threaded preparation gates or joined native
sandbox execution; command watchdogs do not supply a finite live-child fixture.

```text
32  test_prepare_off_loop
140 test_prepare_off_loop_preserves_approval_serialization
259 test_shell_scope_exact_file_and_symlink_escape
307 test_directory_addition_grants_subtree_without_escape
404 test_persistent_python_native_scope
443 test_module_runtime_and_private_scratch
460 test_suppressed_denials_are_not_observable
469 test_invalid_profile_does_not_execute
480 test_replaced_approved_file_does_not_grant_new_symlink_target
491 test_replaced_session_cwd_fails_closed
501 test_replaced_scratch_directory_fails_closed
512 test_scratch_root_cannot_be_removed_by_child
```

## Worker, stop, and poller inventory

### tests/test_worklink_worker_exec.py

**c, synchronized**: controlled protocol, fake process, or controlled clock.

```text
297  test_client_names_stale_old_launch_contract_with_timeout
350  test_path_client_names_unsupported_operation_as_stale_executor
392  test_worker_process_requires_identity_bound_terminal_result
997  test_duplicate_worker_id_is_refused_before_popen_without_touching_live_job
1135 test_executor_enforces_worker_deadline
1221 test_executor_truncates_and_terminates_on_output_overflow
1255 test_arm_parent_death_signal_closes_pre_prctl_race
1280 test_arm_parent_death_signal_is_noop_off_linux
1294 test_terminate_process_group_sigterm_guard
1315 test_terminate_process_group_sigkill_guard
1335 test_process_group_cancellation_reports_unreapable_member
1544 test_factory_boundary_lock_is_nonblocking
1730 test_factory_cancel_uses_supervisor_stop_not_legacy_group
1749 test_factory_drops_identity_before_payload_exec_or_spawn
1927 test_wait_factory_unreapable_supervisor_has_finite_stop_bound
1963 test_worker_process_forwards_factory_events_to_owned_logger
2017 test_factory_stop_refuses_missing_monitor_acknowledgement
2038 test_wait_factory_forwards_supervisor_event_before_terminal
2145 test_wait_factory_rejects_invalid_terminal
```

**c, synchronized real lifecycle** (watchdogs where noted):

- `1040 test_terminal_waits_for_in_group_writers_before_cleanup`: readiness after
  TERM-ignore setup; leader exit and monitor entry observed.
- `1845 test_wait_factory_real_supervisor`: complete/cancel/timeout/stdout/stderr/
  death variants; controlled clock, expiration after payload readiness.
- `2057 test_factory_connection_error_does_not_block_on_full_controller_socket`:
  actual full-socket event; peer remains open and undrained during assertion.
  2s entry and 1s join are watchdogs. Earlier missing-backpressure suspicion is
  retracted for this test.
- `2167 test_factory_descendant_cannot_write_controller_canary_and_negative_control_is_live`:
  real root/controller/worker boundary and terminal results; 5/10/15s completion
  guards. Includes git-intake and negative-control variants.
- `2394 test_worker_payload_cannot_reach_controller_canary_and_detector_is_live`:
  real identity boundary and terminal result; 5s request budget.
- `2522 test_worker_cannot_cross_attempt_boundary_and_negative_control_is_live`:
  result pipe and waitpid, no local startup/lifetime timing window.

Requiring privileged execution does not make all root tests residual races.

### tests/test_worklink_stop.py

- **c, synchronized + watchdog**
  `197 test_stop_live_factory_reaps_tree_despite_stale_leaf`: escaped descendant
  installs TERM-ignore, acknowledges by pipe, then pauses indefinitely; leader
  readiness follows acknowledgment. Cleanup guard is separate from lifetime.

### tests/test_worklink_cli.py

**c, synchronized injected timeout/identity:**

```text
533  test_status_bounds_chainlink_subprocesses
1179 test_factory_stop_refuses_unverified_or_reused_process_identity
```

Finite-sleeper stop tests are in the residual inventory.

### tests/test_worklink_compute.py

**c, synchronized:**

```text
16  test_external_process_wait_kill_guard
37  test_external_process_cancel_propagates_probe_permission_error
138 test_cleanup_drains_kill_task_published_during_monitor_teardown
```

- **c, synchronized + watchdog + yield**
  `58 test_cleanup_after_wait_error_drains_live_monitor`: monitor-check thread
  entry and release are explicit; 5s fallback raises rather than completing
  normal work. Intermediate cleanup-pending observation uses a yield.

### tests/test_worklink_backends.py

**c, synchronized fake subprocess or recorded retry delay:**

```text
543 test_local_subprocess_compute_backend_preserves_subprocess_shape
612 test_local_subprocess_compute_omits_parent_death_hook_off_linux
650 test_local_subprocess_compute_caps_output_and_kills_on_overflow
825 test_opencode_retries_transient_sqlite_startup_contention
863 test_opencode_persistent_sqlite_contention_exhausts_with_named_reason
```

### tests/test_worklink_autonomy.py

**c, dispatch/receipt/result contracts; a/c residual helper claim limitations**
described above. Complete shared real-poller/fake-child-helper consumer list:

```text
1785 test_poller_reads_cap_from_worklink_yaml
1820 test_poller_excludes_actively_locked_issue_from_candidates
1848 test_poller_reads_flow_style_cap_through_worklink_config
1871 test_poller_degrades_before_dispatch_for_invalid_backend_reference
1899 test_poller_dispatches_up_to_free_slots
1924 test_poller_dispatch_reports_and_propagates_coding_state
1949 test_poller_failure_escalation_dedupes_by_signature_and_recovers
2193 test_epic_dispatch_backoff_prevents_attempt_each_poll_cycle
2400 test_poller_does_not_dispatch_worklink_epic_issues
2432 test_poller_dispatches_only_ready_epic_when_factory_epics_enabled
2465 test_poller_excludes_worklink_blocked_from_leaf_and_epic_dispatch
2504 test_poller_keeps_bare_ready_leaf_on_per_leaf_run
2536 test_poller_skips_leaf_under_active_epic_lock
2565 test_poller_filters_worklink_ready_through_chainlink_actionable_set
2597 test_poller_leaves_blocked_worklink_ready_issues_untouched
2623 test_poller_dispatches_worklink_ready_after_blocker_closes
2654 test_poller_no_dispatch_when_cap_reached
2673 test_poller_accepts_chainlink_ready_text_when_json_flag_is_ignored
2720 test_poller_fails_closed_when_chainlink_errors
```

- At 1785 poller completion is joined directly before the fake-child helper.
- At 2504 `_wait_for_dispatch_lines` also polls for exact owned output.
- Forced cleanup limits natural-exit claims, not automatically dispatch assertions.
  No-dispatch variants do not inherit a nonexistent child-lifetime race.
- Helpers: `_fake_run_bin` 1644, `_wait_for_dispatch_lines` 1677,
  `_wait_for_fake_run_bin_exit` 1689, `_run_poller` 1755.

**c, synchronized + watchdog:**

```text
1381 test_worklink_run_cancellation_leaves_executor_running
1586 test_poller_import_path_repair_uses_explicit_source_dir_without_editable_install
1613 test_poller_import_path_repair_adds_source_venv_site_packages
```

At 1381 entered/release/finished events own executor lifetime; 10s fallback
raises, not successful completion. At 1586/1613 startup probes are joined.

**c, synchronized in-process ordering/budget**, not real-poller-helper consumers:

```text
2051 test_poller_dispatches_only_after_failure_alert_is_durably_acked
2131 test_poller_reports_scan_when_alert_delivery_leaves_insufficient_budget
2231 test_poller_stops_after_emitting_when_later_failure_ack_errors
```

### tests/test_worklink_orchestrator.py

**c, synchronized / watchdog:**

```text
69   test_factory_checkout_interlock_cross_process
122  test_factory_run_interlock_refuses_busy_and_releases_on_exit
1654 test_publication_replacement_interleaving
```

At 1654 events/queues hold first publisher through replacement or deliberate
death; queue/join deadlines are watchdogs. Recovery test 6934 is above.

## Git, locks, bounded execution, and fixture cleanup

### tests/test_git_tracking.py

**c, synchronized / watchdog:**

```text
190  test_commit_and_schedule_push
230  test_commit_holds_per_home_lock_during_stage_and_commit
263  test_retry_push_holds_per_home_lock_during_remote_sync
304  test_debounced_push_pulls_rebase_before_push
358  test_debounced_push_aborts_rebase_and_skips_push_on_pull_failure
436  test_debounce_coalesces_burst_to_single_push
488  test_debounce_reset_cancels_prior_task
554  test_git_retries_transient_index_lock_collision
584  test_commit_succeeds_after_real_index_lock_is_released
650  test_git_does_not_retry_non_collision_failure
675  test_git_retry_scope_covers_index_touching_commands
697  test_exhausted_index_lock_retries_report_attempts_without_deleting_lock
760  test_push_timeout_logs_git_push_failed
828  test_push_success_emits_git_push_ok
875  test_no_remote_skips_push_silently
917  test_debounced_push_reconciles_squash_merged_proposal_surface
993  test_debounced_push_restores_stale_main_upstream_and_pushes_explicit_branch
1250 test_push_failure_schedules_retry
1287 test_debounced_push_schedules_retry_under_home_lock
1333 test_retry_success_emits_git_push_ok
1386 test_retry_exhaustion_emits_git_push_stale
1430 test_new_commit_cancels_retry
```

At 584 real failed git add is witnessed before lock release; retry intervals do
not erase that witness. Other retry/debounce tests use gates or delay recording.
The unnecessary negative sleep at 161 is above.

### tests/test_repo_tools.py

**c, synchronized injected result or held runner:**

```text
77   test_bounded_subprocess_runner_killpg_guard
1811 test_project_test_output_overflow_verdict_and_accounting
1992 test_project_tests_scrub_checkout_home_and_sensitive_output
2107 test_project_test_timeout_returns_captured_hang_diagnostic
2141 test_project_test_hang_is_observable_before_runner_completes
2194 test_project_test_fast_run_removes_live_output_files
2497 test_project_test_names_stale_root_executor_and_rebuild_action
3147 test_git_execution_refuses_timeout_and_output_overflow
3289 test_project_test_timeout_can_actually_run_this_repository_suite
```

At 2141 runner writes output, signals output_written, holds release_runner;
parent reads output and observes pending task before release. Residual runner,
diagnostic, and root-executor entries appear above.

### tests/test_event_logger.py

**c, synchronized / watchdog:**

```text
524 test_process_append_survives_trim_rename_window
580 test_process_lock_timeout_degrades_to_append
602 test_durable_process_lock_timeout_fails_instead_of_claiming_success
```

At 524 child encounters held lock and reports blocked before rename advances.
At 580 expiry uses controlled monotonic time.

### tests/test_commitments_store.py

- **c, synchronized + watchdog**
  `708 test_rewrite_preserves_concurrent_process_append`: child append completes
  inside paused rewrite hook before replacement. Test 764 is above.

### tests/test_assert_installed_acp.py

**c, synchronized + watchdog:**

```text
49  test_fixture_cleanup_preserves_failure_and_adds_captured_output
95  test_fixture_cleanup_reports_captured_client_output
113 test_fixture_server_failure_reports_captured_output
```

At 49 stdout/stderr publication is explicitly ordered before raising the original
exception. First two contain a 60s fixture sleeper, but output/error-propagation
assertions do not themselves claim termination of a still-live child. They are
not classified as lifecycle races merely for that sleep; an indefinite fixture
hold would express ownership more clearly.

### tests/test_contained_execution.py

**c, synchronized fake processes**, owned release/cancel state:

```text
78  test_execute_contained_returns_only_a_capped_collected_value
114 test_execute_contained_timeout_cancels_and_reaps
138 test_execute_contained_timeout_reaps_when_cancel_reports_failure
168 test_execute_contained_cancellation_sends_cancel_before_reraising
```

### tests/test_worklink_feature_factory.py

- **c, synchronized**
  `485 test_bounded_runner_stops_oversize_output_during_execution`: real producer,
  timeout=None, cap/reap assertions.

### tests/test_spawn_opencode.py

- **c, synchronized**
  `494 test_generated_payload_containment_and_unsafe_negative_control`:
  process-shaped wrapper holds an already completed real subprocess result,
  not a concurrently running child.

### tests/test_index.py

**c, synchronized:**

```text
489 test_sweep_removes_orphaned_tmps
512 test_sweep_preserves_live_process_tmp
```

First waits for child exit; second uses current owned process, not a sleeper.

### tests/test_mcp_client.py

**c, simulated timeout contracts**, not real server-startup deadlines:

```text
531 test_manager_initialize_timeout
544 test_bridge_tool_surfaces_call_timeout
```

### tests/test_health_probe.py

- **c, synchronized + watchdog**
  `171 test_probe_pwd_real_subprocess_against_real_cwd`: completed pwd; timeout
  belongs to the health-check contract.

### tests/test_scratch_alternates_interlock.py

**c, synchronized / watchdog**: synchronous Git completion and directly
controlled reclamation ordering.

```text
44  test_janitor_refuses_to_reclaim_a_borrowed_object_store
72  test_base_fetch_repairs_refs_left_dangling_by_a_reclaimed_alternate
107 test_repair_never_deletes_a_local_branch_or_tag
```

## Follow-up, not implemented here

1. Add actual state witnesses for SSH backpressure and diagnostic-worker entry.
2. Replace finite lifecycle sleepers in proxy/SSH cleanup, CLI stop, and local
   factory recovery with owned release mechanisms.
3. Separate timeout/cap branches from startup; control progress/expiry rather
   than elapsed-time oracles in renewal, permission, and lock tests.
4. Distinguish poller natural exit from forced cleanup and accurately bound the
   stdout-reading phase. Preserve any pre-existing alarm state when applicable.
5. Improve yield-only entry observations where useful and remove the Git negative
   sleep, without discarding legitimate completion or bounded-cleanup contracts.

These are source-supported improvement directions, not reproduced failures.
Preserve the already synchronized kernel, shell-job, transport, supervisor, and
lock-interleaving contracts. Integration with the seven separate audits and
runtime verification remains outside this document's evidence.
