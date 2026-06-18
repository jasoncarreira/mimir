# React API Contracts For Existing Parity Surfaces

This inventory is intentionally scoped to endpoints already consumed by
`turn_viewer.html`, `ops_dashboard.html`, `saga_dashboard.html`,
`file_memory_dashboard.html`, and `mimir/bridges/web_chat.py`. It is not an
OpenAPI contract and does not define new global error, auth, pagination, or SSE
conventions.

All `/api/*` requests use `X-API-Key: <MIMIR_API_KEY>` when a key is configured.
The legacy dashboards store that value in `localStorage["mimir.api_key"]`.

## Turns

`GET /api/turns?limit=200`

`GET /api/turns?before=<turn_id>&limit=200`

`GET /api/turns?after=<turn_id>`

Response:

```json
{
  "turns": [
    {
      "turn_id": "turn-20260617-001",
      "ts": "2026-06-17T12:00:00Z",
      "trigger": "user_message",
      "kind": "user_message",
      "channel_id": "web-default",
      "input": "Summarize the current state.",
      "output": "State summary complete.",
      "duration_ms": 1842,
      "events": [{"type": "reasoning", "content": "Read state.", "t_ms": 120}],
      "saga_calls": [{"call_type": "query", "latency_ms": 31, "t_ms": 510}],
      "injected_inputs": [{"t_ms": 900, "text": "Also include recent ops."}]
    }
  ]
}
```

Notes: responses are oldest-first. The viewer reverses them for display. Unknown
event types are allowed and should remain visible.

## Ops

`GET /api/ops?days=7`

Response:

```json
{
  "generated_at": "2026-06-17T12:05:00+00:00",
  "window_days": 7,
  "summary": {"total_events": 12, "events_queued": 3, "failures": 1},
  "by_event": {"event_queued": 3, "tool_call": 4},
  "queued_by_trigger": {"user_message": 2},
  "queued_by_channel": {"web-default": 2},
  "resolution_paths": {"saga_query_ctx_resolution": {"saga_session_id": 3}},
  "shell_jobs": {"spawned": 1, "routed": 1, "no_channel": 0, "enqueue_failed": 0, "spawn_by_channel": {"web-default": 1}},
  "tools": [{"tool": "saga_query", "calls": 3, "errors": 1, "failure_rate": 0.3333, "avg_duration_ms": 24}],
  "failures_by_kind": {"saga_query_error": 1},
  "timeseries": [{"day": "2026-06-17", "events": 12, "queued": 3}],
  "recent_failures": [{"t": "2026-06-17T12:01:00+00:00", "kind": "saga_query_error", "detail": "query timed out"}],
  "backlog": [{"id": "turn-timing-histogram", "title": "Turn duration histogram", "status": "Partial", "blocker": "Turn duration lives in turns.jsonl."}],
  "chainlink_issues": {"available": true, "issues": [], "error": null},
  "usage_history": {},
  "token_usage_history": []
}
```

## SAGA

`GET /api/saga?view=stats`

`GET /api/saga?view=recent&channel=<channel_id>&limit=50`

`GET /api/saga?view=atom&id=<atom_id>`

`GET /api/saga?view=search&q=<query>&channel=<channel_id>&limit=100`

`GET /api/saga?view=activation_hist&days=7`

`GET /api/saga?view=clusters&sample_size=3`

`POST /api/saga/sql` with `{"sql": "SELECT ..."}`

Representative responses:

```json
{"ready": true, "atom_count": 42, "session_count": 8, "triple_count": 12, "schema_version": 6, "db_size_bytes": 1048576}
```

```json
{"atoms": [{"id": "atom-A", "content_preview": "User prefers terse updates.", "memory_type": "observation", "created_at": "2026-06-17T11:59:00Z"}], "total": 42, "limit": 50, "channel_filter": null, "channels": ["web-default"]}
```

```json
{"id": "atom-A", "content": "User prefers terse updates.", "topics": ["preferences"], "metadata": {}, "access_count": 3, "embedding": {"provider": "local", "model": "test", "dim": 384}, "relations_out": []}
```

```json
{"columns": ["id"], "rows": [["atom-A"]], "row_count": 1, "truncated": false}
```

Notes: `POST /api/saga/sql` exists only when `MIMIR_SAGA_SQL_ENABLED=1`; callers
must treat `404` as "expert SQL unavailable".

## State And File Memory

`GET /api/memory?view=tree`

`GET /api/memory?view=file&path=memory/INDEX.md`

`GET /api/memory?view=search&q=<query>`

`GET /api/memory?view=channels`

Representative responses:

```json
{"name": "home", "type": "dir", "path": "", "desc": null, "children": [{"name": "memory", "type": "dir", "path": "memory", "desc": null, "children": []}]}
```

```json
{"path": "memory/INDEX.md", "content": "# Memory", "size": 256, "modified": "2026-06-17T12:00:00+00:00"}
```

```json
{"query": "memory", "hits": [{"path": "memory/INDEX.md", "line_no": 1, "snippet": "# Memory"}], "total": 1, "truncated": false}
```

```json
{"channels": ["web-default", "discord-ops"]}
```

## Web Chat

`POST /chat`

Request:

```json
{"channel_id": "web-default", "content": "Hello", "author": "alice", "msg_id": "client-1", "extra": {"source": "react"}}
```

Accepted response:

```json
{"ok": true, "channel_id": "web-default"}
```

`GET /chat/stream`

Stream clients must use fetch-based SSE with `Accept: text/event-stream` and
`X-API-Key` when a key is configured. Native `EventSource` is not supported for
authenticated streams because it cannot set headers and API keys in URLs are not
accepted.

Server-sent event `data:` payloads:

```json
{"channel_id": "web-default", "text": "Reply", "message_id": "msg-abc123", "attachments": []}
```

```json
{"_event": "react", "channel_id": "web-default", "message_id": "msg-abc123", "emoji": "thumbs_up"}
```

Heartbeats are SSE comments (`: heartbeat`) and carry no JSON payload.

## Follow-Up Blockers

No additive backend endpoint was required for the parity surfaces inventoried
here. The first React chat/details work still has exact missing fields:

- `POST /chat` accepts `msg_id` but responds only with `ok` and `channel_id`.
  A React chat composer cannot correlate a pending local message with the
  server-created `AgentEvent.source_id` unless the response also returns
  `source_id` or echoes `msg_id`.
- `/chat/stream` outbound message events include `message_id`, but not the
  originating `turn_id`, `source_id`, `final`, or any chunk/sequence metadata.
  A React details pane cannot deep-link a chat reply to `/api/turns` detail data
  without a follow-up field such as `turn_id`.
- `/chat/stream` reaction events include `_event`, `channel_id`, `message_id`,
  and `emoji`, but no actor or timestamp. A first parity reaction display can
  render the emoji only; actor/timestamp display needs additive fields.
