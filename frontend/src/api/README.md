# React API Contracts

This inventory is intentionally scoped to the legacy parity surfaces that the
first React routes need. It mirrors the existing static pages and
`mimir/bridges/web_chat.py`; it is not a generated or global platform contract.

## Turns

`GET /api/turns`

Query params:

- `limit=N`: newest N turns, oldest-first in the response.
- `after=<turn_id>`: turns strictly newer than the cursor.
- `before=<turn_id>&limit=N`: up to N turns immediately older than the cursor.

Response:

```json
{"turns":[{"turn_id":"turn-001","ts":"2026-06-01T12:00:00Z","trigger":"user_message","kind":"user_message","channel_id":"web-default","input":"Show me recent memory","output":"Found two relevant notes.","duration_ms":1842,"events":[{"type":"tool_call","t_ms":180,"id":"call-memory-query","name":"saga_query","args":{"query":"recent memory"}}],"saga_calls":[{"call_type":"query","t_ms":190,"latency_ms":720,"args":{"query":"recent memory"},"result":{"count":2}}],"injected_inputs":[{"t_ms":650,"text":"Narrow that to today."}]}]}
```

`GET /api/events`

Query params:

- `since=<iso timestamp>`
- `type=<kind>` repeated or comma-separated
- `limit=N`

Response:

```json
{"events":[{"timestamp":"2026-06-01T12:00:01Z","type":"tool_call","tool":"saga_query","ok":true,"duration_ms":720}]}
```

## Ops

`GET /api/ops?days=N`

Response:

```json
{"generated_at":"2026-06-01T12:00:05Z","window_days":7,"summary":{"total_events":4,"events_queued":1,"messages_sent":1,"subagents_started":0,"subagents_completed":0,"shell_jobs_spawned":1,"shell_jobs_routed":1,"failures":1,"high_water_events":0,"client_pool_drains":0,"tool_calls":1,"tool_errors":0},"by_event":{"event_queued":1},"queued_by_trigger":{"user_message":1},"queued_by_channel":{"web-default":1},"resolution_paths":{"saga_query_ctx_resolution":{"saga_session_id":1}},"shell_jobs":{"spawned":1,"routed":1,"no_channel":0,"enqueue_failed":0,"spawn_by_channel":{"web-default":1}},"tools":[{"tool":"saga_query","calls":1,"errors":0,"failure_rate":0,"avg_duration_ms":720}],"failures_by_kind":{"git_push_failed":1},"timeseries":[{"day":"2026-06-01","events":4,"queued":1}],"recent_failures":[{"t":"2026-06-01T12:00:04Z","kind":"git_push_failed","channel_id":"web-default","trigger":"user_message","detail":"non-fast-forward"}],"backlog":[],"chainlink_issues":{"available":false,"issues":[],"error":null},"usage_history":{},"token_usage_history":{}}
```

## SAGA

All SAGA reads use `GET /api/saga` with a `view` query param.

- `view=recent&channel=<channel_id>&limit=N`
- `view=atom&id=<atom_id>`
- `view=stats`
- `view=search&q=<text>&channel=<channel_id>&limit=N`
- `view=activation_hist&days=N`
- `view=clusters&sample_size=N`

`POST /api/saga/sql` is registered only when `MIMIR_SAGA_SQL_ENABLED=1` and
accepts `{"sql":"SELECT ..."}`.

Representative responses:

```json
{"atoms":[{"id":"atom-001","content_preview":"User prefers concise implementation notes.","memory_type":"raw","stream":"semantic","source_type":"conversation","topics":["preferences"],"arousal":0.5,"valence":0.1,"encoding_confidence":0.8,"is_pinned":0,"created_at":"2026-06-01T11:59:00Z","session_id":"sess-001","channel_id":"web-default"}],"total":1,"limit":50,"channel_filter":null,"channels":["web-default"]}
```

```json
{"ready":true,"atom_count":12,"tombstoned_count":1,"session_count":3,"triple_count":4,"schema_version":6,"db_size_bytes":8192,"db_path":"/home/mimir/.mimir/saga.db"}
```

## State And Memory

`GET /api/memory`

- `view=tree`: virtual `home` directory with `memory` and `state` children.
- `view=file&path=memory/INDEX.md`: reads a `.md` file under an allowed root.
- `view=search&q=<text>`: searches `.md` files under both roots.
- `view=channels`: lists directories under `memory/channels`.

Representative responses:

```json
{"name":"home","type":"dir","path":"","desc":null,"children":[{"name":"memory","type":"dir","path":"memory","desc":null,"children":[{"name":"INDEX.md","type":"file","path":"memory/INDEX.md","size":42,"modified":"2026-06-01T12:00:00+00:00","desc":"memory index"}]}]}
```

```json
{"path":"memory/INDEX.md","content":"<!-- desc: memory index -->\n# Memory\n","size":42,"modified":"2026-06-01T12:00:00+00:00"}
```

## Chat

`POST /chat`

Request:

```json
{"channel_id":"web-default","content":"hello","author":"alice","author_id":"alice-1","msg_id":"client-msg-1","extra":{"source":"react"}}
```

Response:

```json
{"ok":true,"channel_id":"web-default"}
```

`GET /chat/stream`

SSE data payloads:

```json
{"channel_id":"web-default","text":"Hello from the agent.","message_id":"msg-001","attachments":[]}
```

```json
{"_event":"react","channel_id":"web-default","message_id":"msg-001","emoji":"thumbs_up"}
```

## Follow-Up Blockers

- `POST /chat` does not echo the accepted client `msg_id` or generated
  `AgentEvent.source_id`; React chat cannot reconcile optimistic outbound user
  messages to the queued server event without carrying `msg_id` client-side.
- `GET /chat/stream` send events do not include `timestamp`, `author`, or
  `final`; React chat can render parity text, attachments, and reactions, but
  cannot build a durable chronological transcript from the stream alone.
- `GET /api/turns` has no per-turn detail endpoint or server-side
  `turn_id` lookup. The legacy detail panel depends on whatever records are in
  the paged list response, so a direct React detail route must first load pages
  until the target turn appears or add `GET /api/turns/{turn_id}`.

Typed fixtures live in `frontend/src/fixtures/apiFixtures.ts`. Tests can use
`frontend/src/fixtures/mockApi.ts` to exercise the client modules with mock
`fetch` responses.
