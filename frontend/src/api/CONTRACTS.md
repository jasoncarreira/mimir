# React Dashboard API Contracts

## Strategy

The aiohttp contract source of truth is `mimir/web_contracts.py`. React types
are generated from that module into `frontend/src/api/generated/contracts.ts`;
dashboard code must import generated types or compatibility aliases built from
them, not copy Python response dictionaries by hand.

Regenerate after contract edits with:

```sh
python -m mimir.web_contracts > frontend/src/api/generated/contracts.ts
```

This is deliberately not a full OpenAPI migration. The current scope is the
React dashboard parity surface inventoried from the legacy pages:

- `GET /api/v1/web/bootstrap`
- `GET /api/v1/turns`
- `GET /api/v1/events`
- `GET /api/v1/live-events`
- `GET /api/v1/admin/config`
- `GET /api/v1/ops`
- `GET /api/v1/saga`
- `POST /api/v1/saga/sql` when `MIMIR_SAGA_SQL_ENABLED=1`
- `GET /api/v1/memory`
- `POST /api/v1/chat`

Legacy `/api/*` and `/chat` routes remain compatible with the static HTML pages
until those pages are cut over.

## Admin Config

`GET /api/v1/admin/config` is read-only in v1. It returns effective
model/provider/context-window/resource-window state, typed config schema
sections, configured scheduler jobs and pollers, categorized environment
presence, and redacted raw config. Secret-bearing fields and env vars return
presence plus `[REDACTED]`, never the stored value.

The v1 admin contract intentionally has no reveal or mutation endpoint.
`capabilities.secret_reveal.available` and `capabilities.edits.available` are
`false`; future editable settings must add an explicit field allowlist, auth
gate, audit trail, and rate limit before a write path is exposed.

## Envelope

Successful v1 REST responses use:

```json
{"ok": true, "version": "v1", "data": {}, "meta": {"cursor": null, "limit": 50, "total": 100, "truncated": true}}
```

Errors use:

```json
{"ok": false, "version": "v1", "error": {"code": "missing_query", "message": "q param required"}}
```

List-like responses use the same metadata fields whenever applicable:
`cursor`, `limit`, `total`, and `truncated`.

## Live Events

Live stream payloads are a separate discriminated union because SSE transport
details are outside the REST contract. The Python constructors and validator
live in `mimir/web_contracts.py`; TypeScript receives the generated `LiveEvent`
union with `kind` discriminators:

- `chat.message`
- `chat.reaction`
- `turn.event`
- `turn.lifecycle`

Existing `/chat/stream` payloads also keep legacy fields such as `_event` for
backward compatibility.

`GET /api/v1/live-events` is the shared React dashboard SSE substrate. It uses
fetch-based SSE with `X-API-Key` auth, never URL-carried keys. Each SSE data
payload is a `LiveEventStreamItem`:

```json
{"id": "turn:t1:event:1", "cursor": "2026-01-01T00:00:00Z:t1:000001", "ts": "2026-01-01T00:00:00Z", "event": {"kind": "turn.event", "turn_id": "t1", "event": {"type": "tool_call"}}}
```

Reconnect with `?since=<last cursor>`; cursors are timestamp-prefixed so lexical order matches delivery order, and backfill is strict (`cursor > since`) so
the last acknowledged item is not replayed. Clients should also deduplicate by
`id`, because reconnects and log rewrites can overlap.

TanStack Query integration policy: high-frequency events for the selected turn
must update local/query state directly. Aggregate views should debounce and
coalesce invalidation instead of calling `invalidateQueries` for every event.
`LiveEventsProvider` implements this policy with a structural query-client
interface so the frontend does not need to add TanStack Query before pages are
cut over.

## Versioning

`/api/v1/*` may add optional fields without a version bump. Removing fields,
renaming fields, changing discriminator values, or changing envelope semantics
requires a new versioned path. Legacy non-v1 endpoints are compatibility
surfaces for the static pages only and should not gain new React dependencies.
