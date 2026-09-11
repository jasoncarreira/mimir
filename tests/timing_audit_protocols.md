# Issue 1616: ACP Protocol Timing Audit

## Status And Scope

Partial implementation, blocked on product-mutation ownership. There is no
`mimir/acp/sessions.py` in this checkout (repository-scoped `**/sessions.py`
search returned no matches). Session state and permission ownership live in
`mimir/acp/agent.py`: `SessionState`, `ActivePrompt.request_permission`, and
`ActivePrompt._request_permission`; the owned request implementation lives in
`mimir/acp/sdk.py`. In particular, `agent.py:276` awaits permission completion
without a deadline. Neither file is in the permitted mutation list. A meaningful
permission-lifetime mutation requires permission to edit the actual owner, not
creation of a new `sessions.py` or an unrelated mutation that fails during setup.

Only five owned test files have been edited. `test_acp_daemon.py` and
`test_acp_sessions.py` remain unchanged. The findings below are not a claim that
all remaining conversions have been implemented.

Read `CONTRIBUTING.md` and inspected commits `88056ea44` and `3885ec64d`.
Preserved their converted sites: proxy real-command/credential and owned-child
protocol ceilings, hosted Python lifecycle and unconfined operator round trips;
transport peer-EOF and force-close cleanup witnesses; SSH hosted environment,
signal watchdog, early child failure, cancellation and cleanup protocol ceilings.
Residual small waits inside those protocols are listed below, not represented as
new conversions or silently considered safe because an outer ceiling exists.

No commits. No product constants changed. All temporary mutations were applied
and restored with `apply_patch`; the final diff of the seven permitted product
paths is empty. Other agents' changes, including temporary mutations outside
these paths, were not modified.

## Converted Protocols

All nine converted test functions (eleven parameterized cases) use the existing
300-second whole-test pytest-timeout ceiling. No per-stage harness deadlines
were added. A zero timeout below deliberately expires an already-observed
operation; it is not a readiness deadline.

| Test | Producer and ordering evidence | Release or expiry |
| --- | --- | --- |
| Proxy `test_plain_client_without_mcp_capability_can_call_hosted_hands` | Capture and await the router-owned request task for typed ID `(int, 3)`; assert the returned read content | Real request completion replaces 50ms sleep |
| Proxy `test_session_new_captures_cwd_for_hosted_operations` | Await the router-owned read task for `(int, 4)`; assert captured-cwd content | Real request completion replaces 50ms sleep |
| Proxy `test_typed_directional_ids_boolean_rejection_and_hosted_cancellation` | Real hosted shell writes `started`; additionally observe provider process registration before cancellation; await the exact request after cancellation | Shell blocks opening an unreleased FIFO, not `sleep 30`; selected session execution budget is 600s, beyond the 300s harness ceiling |
| Proxy `test_client_session_cancel_tombstones_hosted_request` | Same real child readiness and ownership observations; await request settlement before checking silence and tombstone | Same FIFO and 600s selected execution budget; actual session cancellation owns teardown |
| Transport `test_pump_allows_slow_reader_until_peer_eof` | Observe real writer drain entry; module-local `asyncio` facade records any attempted deadline while live backpressure is held | Explicit drain release and both EOFs; assert no live drain budget was used |
| Transport `test_abort_wait_deadline_before_and_after_witnesses` | Observe each close wait entering; record drain/close/abort budgets and completed/cancelled outcomes | Expire first close at zero only after entry; abort releases positive case, negative case remains held and is explicitly expired after entry |
| Updates `test_close_suspends_then_times_out_when_full_worker_is_blocked` | Real publisher enters and blocks; observe real queue join or premature close completion; assert graceful close is suspended | Controlled expiry after join entry; assert retained TimeoutError and emptied queue; replace ticker-count bound |
| Relay `test_dead_relay_client_promptly_closes_daemon_connection` | Observe actual stdin read entry and actual output protocol `connection_lost`; then send stdin EOF | Await relay completion and assert product close policy selected immediate close, not half-close grace |
| SSH `test_local_spawn_bound` | Real spawn wrapper announces entry before release; observe configured spawn deadline and cancellation | Explicitly release successful spawn or expire held spawn at zero; real subprocess runs only in positive case |

The proxy marker can precede provider registration because the OS child may run
before the parent finishes spawn bookkeeping. Both observations are required;
the first trial exposed this ordering, and the final tests wait for both.

## Measured Margins

Linux/Python 3.11, `uv run --extra dev --extra bench pytest -q -n6`, targeted
eleven-case run with `--durations=0`: 11 passed in 7.74s. Individual calls:
SSH positive spawn 0.16s, negative spawn 0.02s; each proxy cancellation 0.02s;
captured-cwd read 0.01s; slow-reader witness 0.01s. Remaining converted calls
were below pytest's 0.005s display threshold. These are measurements, not test
assertions or new per-stage limits.

The proxy FIFO has no natural completion timer. Its selected 600s product
execution budget gives 300s separation from the whole-test ceiling; measured
cancellation protocols took about 0.02s before increasing that selected budget
from the default. All other converted held producers use Events or real pipe
readiness, with no competing release timer. Controlled SSH expiry is independent
of interpreter startup (measured successful startup protocol 0.16s versus the
300s harness ceiling). Relay's deliberately wrong policy settled via the real
5s peer grace and then failed its policy assertion, well before the ceiling.

## Confirmed Product Mutations

Every row below was actually applied to product source and executed, not merely
suggested. All were restored. Failures were assertion-level, not harness expiry.

| Temporary mutation | Converted test failures |
| --- | --- |
| `transport.py`: omit `abort()` | Both abort-wait cases fail `assert writer.aborted` |
| `updates.py`: return immediately from `_close_gracefully` | Fails `graceful close did not wait for queued publication` |
| `proxy.py`: discard a result containing `structuredContent` | Both readfile tests fail returned-content equality |
| `proxy.py`: omit tombstone insertion | Directional cancellation fails expected duplicate-ID rejection; session cancellation fails raw-frame prefix assertion because a cancelled response leaked |
| `transport.py`: put a live chunk drain under `asyncio.wait_for(..., WRITER_DRAIN_TIMEOUT)` | Slow reader fails `live backpressure must not have a drain deadline`, observed `[2.0]` |
| `relay.py`: pass `close_on_left_exit=False` | Fails immediate-close decision assertion, observed `[False]` instead of `[True]` |
| `ssh.py`: pass `None` instead of `SPAWN_TIMEOUT` at spawn wait | Both spawn cases fail configured-budget equality, `None != 30.0` |

Mutation runs used the owned files and `-k` selection with `-n6`. Confirmed
groups: transport/updates 3 failed in 12.97s; proxy group 4 failed in 4.10s,
followed by the corrected dual-readiness directional case failing its intended
duplicate-ID assertion in 4.17s; slow reader/relay/SSH 4 failed in 8.37s.

Discarded trials are not evidence: an overly broad proxy result mutation failed
setup with `KeyError`, so it was narrowed to tool results and rerun. The first
updates mutation trial printed failures but hung in teardown until the 120s
tool limit; added failure-path worker cleanup, then reran to the confirmed
assertion failure above. No ceiling failure is counted as a successful mutation.

## Remaining Timing Families

Repository-scoped searches included sleep, wait_for, asyncio.wait, timeout,
timeout_at/deadline, finite polling loops, monotonic/perf-counter observations,
thread-future result timeouts, and timeout constants. This inventory separates
ordering defects from product expiry tests and from harness guards.

| File/family | Finding and remaining work |
| --- | --- |
| Proxy response/readfile and cancellation | Four functions converted above. Remaining helper initialization `sleep(0)` calls are scheduling assumptions, not explicit response observations |
| Proxy owned-process polling | `owned_process_reaped` has a 5s local ceiling; hosted Python lifecycle retains inner 5s marker/process polls and a 2s disconnect bound within prior 120s conversions. Preserve prior converted sites, but residual stages need separate work |
| Proxy supervision, grace, permission | Failure waits use 1s; failed reader delays 10ms to land in grace; local cleanup waits 5s; scope permission expiry uses 10ms product timeout plus 1s harness wait. Still pending producer/controlled-expiry conversions |
| Transport abort and live backpressure | Converted above; no competing 10/20/30ms timers remain in these cases |
| Transport drain/close boundaries | Existing pre-released positive gates and unreleased negatives do not race a timer release. Existing controlled close/force-close wait seams already record entry/cancellation. Peer-EOF and force-close outer 120s changes from prior commit preserved |
| Relay death | Converted read entry, pipe-loss callback, EOF, immediate-close decision and completion |
| Relay connect/pipe cleanup | Connect fake sleeps 10s against 10ms expiry; should become an entered, never-released operation with controlled expiry. Pipe-output cleanup uses `sleep(0)` instead of confirmed protocol closure. Not converted |
| Updates blocked close | Converted ticker `<50` and 100ms wait to producer join and controlled expiry |
| Updates FIFO drain | Existing publisher entry and release are explicit; `sleep(0)` used to assume the separate drain task started remains an observation gap |
| Daemon admission/reconnect | 50ms negative read does not prove admission. Use a positive admitted-runner response. TCP reset handshake stages use 0.5/1.5/2s bounds; full protocol needs a shared ceiling and explicit retirement observation |
| Daemon dead-before-drain | 50ms transport-death wait competes with 200ms dispatcher drain. Need an observed drain/transport-death ordering witness; dispatcher implementation belongs to out-of-scope `sdk.py` |
| Daemon close concurrency | 10ms sleep before max-four assertion does not prove all eight attempts reached the semaphore. Need acquire-attempt and writer-entry observations plus explicit release |
| Daemon authentication | 30ms sleep versus 10ms preauth deadline should observe successful-auth transition and untimed postauth watch. Preauth expiry has an unreleased runner, but needs controlled entry/expiry and cleanup witnesses |
| Daemon watchdog | Dead peer uses 100ms harness waits versus 1s drain; postauth failure uses 100ms harness versus 10ms drain. Need drain-entry, transport-death and controlled wait observations; existing live-window fake only returns empty `done`, without producer entry |
| Daemon shutdown/grace | Grace/cancel/abort 10ms product phases, 100ms total-shutdown wrapper, 0.5/1/2s cancellation-resistant close guards, runner-entry 1s bounds and repeated-cancellation `sleep(0)` remain. Some tests directly exercise `sdk.py` close ownership, also outside allowed mutation scope |
| Sessions snapshot/audit polling | Snapshot loop 100 x 10ms, audit loops 20 x 10ms: replace with owned permission-delivery and notification-dispatch observations, not elapsed polling bounds |
| Sessions permission lifetime | 35s sleep/elapsed assertion, handle registration 3s polling, 3s thread future and per-stage cleanup waits remain. Actual owner is `agent.py`/`sdk.py`, not nonexistent `sessions.py`; blocked for meaningful mutation verification |
| Sessions cancellation/revalidation | Ordered-dispatch uses 1s waits plus a 1s loop deadline; blocked update cancellation uses 100ms; concurrent tools/list uses 10ms sleep; detached forwarder uses 3600s sleep. Existing generation retirement controlled release/expiry witnesses are already event-driven. Other `sleep(0)` scheduling assumptions require producer-specific review |
| Sessions wire integration | `next_outgoing` and prompt/runner cleanup retain 3s stage timeouts; audit polling is part of this same multi-stage protocol |
| SSH spawn | Both 200ms-versus-10ms cases converted above |
| SSH stop-child | Unreleased Future-based wait/terminate/kill fakes have no competing release; retain explicit terminate/kill counts, but entry/controlled-expiry would strengthen them |
| SSH real-child protocol | First-output fake sleeps 80ms; existing prior-converted cancellation/cleanup protocols retain some 10s stages and 50ms unread-output assumption. Signal teardown 60s held producers and inner wait/cancel margins need separate audit before claiming complete coverage |

`time.monotonic()` used as session event metadata, schema timeout values and
profile timeout propagation assertions are not elapsed-time readiness checks.
`sleep(0, result=...)` used solely as an awaitable fixture return is also distinct
from a scheduling assumption. No claims are based on ambient `all_tasks()` or
process-wide task/module deltas.

## Verification

Baseline owned-file run: 338 passed, 3 skipped in 41.59s.
Post-restoration owned-file run: 338 passed, 3 skipped in 48.48s.
Targeted converted protocols: 11 passed in 7.74s.
Final owned-file run after selecting the 600s shell execution budgets:
338 passed, 3 skipped in 51.29s. `git diff --check` passed for the owned test
files, and the explicit diff of permitted product paths remained empty.

The seven-file command is:

```sh
uv run --extra dev --extra bench pytest -q -n6 tests/test_acp_proxy.py tests/test_acp_transport.py tests/test_acp_relay.py tests/test_acp_updates.py tests/test_acp_daemon.py tests/test_acp_sessions.py tests/test_acp_ssh.py
```

Both broad owned-file runs reported the existing subprocess destructor
`RuntimeError: Event loop is closed` warning, also present before these edits.
No full repository suite was run, as explicitly requested during concurrent
mutations. These results are scoped evidence, not a claim of full-suite green.
