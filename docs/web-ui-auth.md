# Web UI Auth and Bootstrap

The web UI has one browser bootstrap path:

- `GET /api/web/bootstrap` returns public, secret-free JSON describing whether `MIMIR_API_KEY` is required, the bind posture, and the stream auth shape.
- `GET /app/auth.js` is the shared browser helper for legacy static pages during migration. It stores a user-entered key under `mimir.api_key` in browser localStorage and sends it as `X-API-Key`.
- React uses one `AuthProvider` backed by `/api/web/bootstrap` and the same localStorage key.

Both `/api/web/bootstrap` and the dynamic React app shell are served with `Cache-Control: no-store`. The bootstrap payload never includes `MIMIR_API_KEY`.

## Server Auth Policy

When `MIMIR_API_KEY` is set, every non-exempt route requires:

```http
X-API-Key: <MIMIR_API_KEY>
```

API keys in URLs are not accepted. Do not put keys in share links, copied links, bookmarks, or bootstrap HTML.

When `MIMIR_API_KEY` is unset, the server allows unauthenticated requests only for local development. Startup refuses public binds such as `0.0.0.0` or `::` without a key; loopback binds (`127.0.0.1`, `::1`, `localhost`) are allowed.

## Host, Origin, and CORS

The server does not emit wildcard CORS headers for web UI APIs or streams. Browser access is same-origin by default. Public binds require `MIMIR_API_KEY`; localhost binds may run unauthenticated for development.

## Stream Auth Shape

Native `EventSource` cannot set `Authorization` or `X-API-Key` headers. For authenticated live streams, use fetch-based SSE:

```ts
fetch("/chat/stream", {
  headers: {
    "Accept": "text/event-stream",
    "X-API-Key": apiKey
  }
});
```

The shared browser helper exposes `MimirAuth.fetchEventStream()` for this pattern. This is the stream-auth shape to reuse for live-events work in #542.
