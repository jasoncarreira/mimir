# React Frontend Cutover

The default operator frontend is the React app served at `/app`; `GET /`
redirects there. Legacy vanilla HTML routes remain only as compatibility
surfaces while parity is verified:

| Route | Status |
| --- | --- |
| `/turns` | Legacy HTML turn viewer; response includes `X-Mimir-Frontend: legacy-html`. |
| `/ops` | Legacy HTML ops dashboard; response includes `X-Mimir-Frontend: legacy-html`. |
| `/saga` | Legacy HTML SAGA viewer; response includes `X-Mimir-Frontend: legacy-html`. |
| `/state` | Legacy HTML state/memory viewer; response includes `X-Mimir-Frontend: legacy-html`. |

Do not remove a legacy route unless the corresponding `/app/...` route has
route-level parity and the removal is covered by its own review.

## Run And Build

For frontend development from a checkout:

```bash
npm ci
npm run dev
```

Vite serves the React app for local frontend work. The aiohttp server still
serves APIs from `mimir run`; use the configured `MIMIR_WEB_PORT` for backend
requests.

For non-Docker/source-checkout operation through aiohttp:

```bash
npm ci
npm run build
uv run mimir run --home ~/mimir-home
```

`npm run build` writes `mimir/react_app/dist`, and aiohttp serves that directory
at `/app`.

For Docker/PyPI operation, the release artifact includes
`mimir/react_app/dist` as package data. The container serves `/app` from the
installed wheel; no Node process is expected at runtime.

## Smoke Checklist

Run the focused automated checks first:

```bash
env -u MIMIR_MODEL_SPEC uv run pytest -q tests/test_web_ui.py tests/test_web_chat_bridge.py --tb=short
npm ci
npm test
```

Then smoke a running server:

- `GET /` returns `302 Location: /app`.
- `/app` loads the React shell and shows `Default Retro` in the chrome; inspect
  the root skin wrapper for `data-skin="default-retro"`.
- `/app/chat` can submit a message through the web-chat bridge and receive a
  streamed reply or reaction event when the bridge emits one.
- `/app/turns` lists turns from `/api/v1/turns`; selecting a turn opens the
  right-side details panel with messages, reasoning, tool calls/results,
  related context, events, and metadata sections.
- `/app/ops` loads operational data from `/api/v1/ops` and handles bad `days`
  parameters with a visible error instead of a blank page.
- `/app/saga` loads stats/recent atoms/search/cluster views from
  `/api/v1/saga`; SQL controls remain gated by `MIMIR_SAGA_SQL_ENABLED=1`.
- `/app/memory` loads tree/search/channel/file views from `/api/v1/memory`.
- Retained legacy routes `/turns`, `/ops`, `/saga`, and `/state` still load,
  but each response is marked `X-Mimir-Frontend: legacy-html`.

## PR Evidence

GitHub issue #726 implementation PRs should reference Chainlink parent #524 and
the cutover leaf #536. Include the focused validation command output, manual
smoke notes for every checklist item above, and explicit close/follow-up notes
for completed subissues. Deferred behavior must be tracked in follow-up issues
instead of being silently dropped.
