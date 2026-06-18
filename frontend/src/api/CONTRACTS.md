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
- `GET /api/v1/ops`
- `GET /api/v1/saga`
- `POST /api/v1/saga/sql` when `MIMIR_SAGA_SQL_ENABLED=1`
- `GET /api/v1/memory`
- `POST /api/v1/chat`

Legacy `/api/*` and `/chat` routes remain compatible with the static HTML pages
until those pages are cut over.

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

## Versioning

`/api/v1/*` may add optional fields without a version bump. Removing fields,
renaming fields, changing discriminator values, or changing envelope semantics
requires a new versioned path. Legacy non-v1 endpoints are compatibility
surfaces for the static pages only and should not gain new React dependencies.
