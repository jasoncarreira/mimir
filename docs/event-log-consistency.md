# Event-log read consistency

Audit for #1698 / PR #2023 (2026-09-13).

## Contract

`EventLogger.log_sync` is a synchronous *API*, not an on-loop durability
barrier. On an event-loop thread it queues best-effort telemetry; off-loop it
waits for the accepted write. Overflow can drop a record. `await log_event`
waits for its own append, not all previous sync submissions. Durable/security
records use `log_durable_event_sync`, with propagated failures and fsync.

A read-after-write dependency on queued telemetry must explicitly await
`asyncio.to_thread(logger.flush_sync)` before reading. A synchronous consumer
already running off-loop may call `logger.flush_sync()` directly. This drains
accepted sync submissions in this process, not other processes or future
submissions, and cannot resurrect dropped records. It does not invalidate a
`JsonlSnapshot` cache. Do not add a global production flush to Path.open,
_jsonl_tail, or JsonlSnapshot: a dashboard read must not stall the event loop
behind a contended logger, and the writer itself uses these primitives.

## Reader-first inventory and disposition

Search both literal `events.jsonl` and aliases (`events_log`, `events_path`,
`get_events_path`, `iter_window_records`, `tail_jsonl_records`). A literal match
is not automatically a read: config declarations, comments and writers are
also returned. Conversely tests use custom filenames and `logger._path`.

| Reader family | Consistency requirement / disposition |
| --- | --- |
| `web_ui`, `ops_dashboard`, `usage_history` | Observational snapshots. Queued sink-denial, model-callback and background-failure telemetry may appear on the next request. No security decision is reconstructed from these logs; harness egress returns its decision independently of logging. No blanket drain added. |
| `agent`, `budget`, `usage_stats`, `poller_budget` | Windowed prompt/accounting reads; snapshots may lag. The agent's cost/off-pace emissions are deferred events, not the callback sync queue. Quota enforcement uses the rate-limit store, not quota-event visibility. Missing latest best-effort telemetry is not permission to spend or send. |
| `feedback` including escalation and cross-turn loop scans | Reads windowed telemetry and may emit dedup records through log_event_sync. The agent invokes recent_prompt_block via asyncio.to_thread, so those emissions retain off-loop completion. Cross-turn input send_message_sent and turn outcomes use awaited logging. Cached snapshots and concurrent readers do not provide a transactional once-only guarantee; this PR does not claim one. |
| `poller_recovery`, `pollers` | Recovery watermarks are correctness-sensitive. Agent terminal turn_completed/turn_failed records use awaited log_event, not queued log_event_sync. Recovery advances over handled outcome timestamps, not wall-clock now. Do not convert these outcome writes to queued telemetry. |
| `ntfy` scheduler-health reader, `health_probe` | Scheduler tick/suppression and bind-mount stale records use the awaited path. health_probe awaits the stale record before fsync/restart. Their evidence is not a queued sync callback. |
| `resend_nudge` / agent no-send count | Windowed historical count; agent explicitly adds its current not-yet-emitted occurrence. No dependency on an immediate sync append. |
| `reflection/applied_audit`, `reflection/introspection_report`, `loops_cmd`, `viability_metrics`, `memory_doctor`, `feedback_cmd`, predictions CLI | Historical reports / CLI scans. These are not same-loop enqueue-then-assert consumers. CLI feedback emit runs off-loop and retains completion. Reports can lag live best-effort telemetry. |
| `event_logger` retention/startup reads | Writer-internal reads; must never wait for the writer queue from inside that queue. Locks continue to serialize append/trim. |
| `jsonl_snapshot`, `_jsonl_tail` | Shared reader mechanisms, not consistency authorities. Keep free of hidden writer waits. |

Accepted sync records are FIFO **with each other**, not with the independent
awaited/durable writers. Occurrence timestamps need not match append order.
Window readers already have chronological-tail assumptions (recovery has a
scan grace interval); a long sync backlog can make observational counts late
or incomplete. This is a best-effort telemetry limitation, not an exact audit
ledger. Any new cursor or state transition relying on these queued records
must instead use an awaited/durable path or its own authoritative state.

## Tests: enforce at observation, not at individual call sites

`tests/conftest.py::_event_log_read_barrier` tracks every EventLogger constructed
by the test, including non-singletons and custom filenames. It drains the FIFO
before test-thread `Path.open` (therefore read_text/read_bytes), builtin `open`,
and `Path.stat` (therefore exists and the JSONL tail reader's early-missing
check). This fixes the class of immediate JSONL assertions rather than only
files that happened to fail. A teardown-only fixture would run too late.

This is intentionally a **test-only synchronous observation boundary**. It
does not replace log_sync with a blocking implementation. Worker threads
bypass interception to avoid writer self-deadlock. Reads delegated to worker
threads, raw os.open/io.open calls, child processes, and already-open handles
must use an explicit barrier when asserting a queued write. Literal-file scans
alone cannot establish those dependencies.

`test_event_logger.py` is exempt because queue/overflow tests deliberately
inspect undrained state under held locks; those tests retain explicit off-loop
barriers. The harness egress contention test still exercises real queued
logging: its loop-progress assertions occur before any read barrier.

The parameterized regression in `test_background_tasks.py` holds the append
worker behind a threading event and verifies text, bytes, Path.open, builtin
open, exists and tail observations on a custom filename. Without the shared
barrier these observations run before the timer releases the writer. No
production logger behavior is monkeypatched to synchronous completion.
