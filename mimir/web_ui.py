"""Turn-viewer + ops dashboard + log API + saga DB + memory routes (SPEC §11).

Mounts onto the same aiohttp app that serves ``/event`` + ``/health`` and
hosts the WebUI bridge's ``/chat``. Routes:

  GET /turns            — single-file vanilla-JS turn viewer (HTML)
  GET /api/turns        — turns.jsonl as JSON (optional ``?after=<turn_id>``)
  GET /api/events       — events.jsonl as JSON (optional ``?since=<ts>``,
                          ``?type=<kind>``, ``?limit=<n>``); type may be
                          repeated to combine filters
  GET /ops              — live ops dashboard (HTML, Chart.js)
  GET /api/ops          — JSON twin of /ops for ad-hoc scripting
  GET /saga             — saga DB operator viewer (HTML)
  GET /api/saga         — JSON twin of /saga; view= selects the payload
                          shape (recent, atom, stats)
  GET /state            — file-based memory browser (HTML, two-pane;
                          renamed from /memory — surfaces memory/ + state/)
  GET /api/memory       — JSON twin of /state; view=tree returns nested
                          dir/file tree (memory/ + state/); view=file&path=...
                          returns file content (only .md files);
                          view=search&q=... returns full-text search hits;
                          view=channels returns channel dir names
  GET /app              — built React app shell (additive migration route)

The HTML page polls ``/api/turns`` every 5s for live updates. ``/api/events``
is exposed for the (deferred) Events tab + ad-hoc scripting. ``/ops``
recomputes from events.jsonl on every request — no caching.
``/saga`` reads the saga SQLite DB on each request — no caching.
``/memory`` reads ``<home>/memory/`` and ``<home>/state/`` on each request — no caching.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from ._jsonl_tail import tail_jsonl_records
from .ops_dashboard import (
    build_dashboard_payload_async,
    parse_days_param,
    render_dashboard_html,
)
from .file_memory_dashboard import (
    list_channel_dirs,
    list_trees,
    read_file_safe_multi,
    render_memory_html,
    search_files,
)
from .saga_dashboard import (
    build_activation_hist_payload,
    build_atom_payload,
    build_clusters_payload,
    build_db_stats_payload,
    build_recent_atoms_payload,
    build_search_payload,
    build_sql_payload,
    render_saga_html,
)

log = logging.getLogger(__name__)

_TURN_VIEWER_HTML: str | None = None
_WEB_AUTH_JS: str | None = None


def _load_viewer_html() -> str:
    """Load the bundled HTML once and cache it."""
    global _TURN_VIEWER_HTML
    if _TURN_VIEWER_HTML is None:
        path = Path(__file__).parent / "turn_viewer.html"
        _TURN_VIEWER_HTML = path.read_text(encoding="utf-8")
    return _TURN_VIEWER_HTML


def _load_web_auth_js() -> str:
    """Load the shared browser auth helper once and cache it."""
    global _WEB_AUTH_JS
    if _WEB_AUTH_JS is None:
        path = Path(__file__).parent / "web_auth.js"
        _WEB_AUTH_JS = path.read_text(encoding="utf-8")
    return _WEB_AUTH_JS


def _no_store_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }


def _read_jsonl(path: Path, *, max_records: int = 5000) -> list[dict[str, Any]]:
    """Read up to ``max_records`` records from the tail of ``path``.

    Pre-2026-05-10 this forward-read the entire file synchronously per
    HTTP request. Combined with the turn-viewer polling every 5s and
    files capped at ~250 MB (turns) / ~300 MB (events), the loop got
    pinned re-parsing hundreds of MB per cycle. Now we tail-read up
    to a soft cap; older records past the cap are silently dropped
    from the response. The default ``max_records=5000`` matches the
    config-default cap for turns/events kept on disk, so on a normally-
    trimmed file the response shape is unchanged.

    Output is in chronological order (oldest-first) — matches the
    forward-read shape callers used to expect.

    Returns [] for missing or unreadable files.
    """
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for record in tail_jsonl_records(path):
            out.append(record)
            if len(out) >= max_records:
                break
    except OSError:
        return []
    out.reverse()  # tail yields newest-first; restore chronological
    return out


def register_routes(
    app: web.Application,
    *,
    turns_log: Path,
    events_log: Path,
    home: Path | None = None,
    saga_db: Path | None = None,
    active_usage_provider: str | None = None,
    react_app_dist: Path | None = None,
) -> None:
    """Add viewer + API routes to an existing aiohttp app.

    Idempotent — skips routes that already exist (so calling this twice in a
    rebuild is harmless).

    ``home`` is passed to ``build_dashboard_payload`` so the /ops
    dashboard's Chainlink tab can run ``chainlink issue list --json``
    against the right repo. None disables the Chainlink tab gracefully
    (renders an "unavailable" message).

    ``saga_db`` is the path to the saga SQLite DB for the /saga viewer.
    If None the viewer renders but API calls return an error payload.

    ``active_usage_provider`` collapses the /ops Usage chart to that single
    subscription provider (e.g. ``"codex_plus"``); None shows every provider
    that has quota data.

    ``react_app_dist`` points at the Vite build output. When omitted it defaults
    to the packaged ``mimir/react_app/dist`` directory."""

    existing = {(r.method, r.resource.canonical) for r in app.router.routes()}
    _react_app_dist = react_app_dist or (Path(__file__).parent / "react_app" / "dist")

    async def turns_page(_request: web.Request) -> web.Response:
        return web.Response(text=_load_viewer_html(), content_type="text/html")

    async def turns_data(request: web.Request) -> web.Response:
        # Records are oldest-first (append order). The viewer reverses to
        # newest-first for display. Pagination params (progressive loading —
        # the viewer no longer pulls the whole — now 140MB+ — file up front):
        #   ?after=<turn_id>           — turns strictly newer than turn_id (live poll)
        #   ?before=<turn_id>&limit=N  — up to N turns immediately OLDER than turn_id
        #                                (scroll-back page)
        #   ?limit=N                   — newest N turns (initial page)
        #   (none)                     — all turns (back-compat)
        records = _read_jsonl(turns_log)
        after = request.query.get("after", "")
        before = request.query.get("before", "")
        try:
            limit = int(request.query.get("limit") or 0)
        except ValueError:
            limit = 0
        if after:
            # Return everything strictly after the named turn_id. If the id
            # isn't found we return the empty list (consistent with open-strix).
            cut: list[dict[str, Any]] = []
            seen = False
            for r in records:
                if seen:
                    cut.append(r)
                elif r.get("turn_id") == after:
                    seen = True
            return web.json_response({"turns": cut})
        if before:
            # The page of records immediately older than ``before``. Empty list
            # when the id isn't found (e.g. rotated out) — the viewer treats that
            # as "no older page".
            idx = next(
                (i for i, r in enumerate(records) if r.get("turn_id") == before),
                None,
            )
            if idx is None:
                window: list[dict[str, Any]] = []
            else:
                start = max(0, idx - limit) if limit > 0 else 0
                window = records[start:idx]
            return web.json_response({"turns": window})
        if limit > 0:
            records = records[-limit:]
        return web.json_response({"turns": records})

    async def events_data(request: web.Request) -> web.Response:
        records = _read_jsonl(events_log)
        since = request.query.get("since", "").strip()
        types = request.query.getall("type", []) or []
        # ``type`` can also arrive as a single comma-joined string.
        type_filter: set[str] = set()
        for t in types:
            for tok in t.split(","):
                tok = tok.strip()
                if tok:
                    type_filter.add(tok)
        try:
            limit = int(request.query.get("limit") or 0)
        except ValueError:
            limit = 0

        out = records
        if since:
            out = [r for r in out if str(r.get("timestamp", "")) >= since]
        if type_filter:
            out = [r for r in out if r.get("type") in type_filter]
        if limit > 0:
            out = out[-limit:]
        return web.json_response({"events": out})

    async def react_app(request: web.Request) -> web.StreamResponse:
        if not _react_app_dist.is_dir():
            return web.Response(
                text=(
                    "React app build not found. Run `npm install` and "
                    "`npm run build` from the repo root."
                ),
                status=503,
                content_type="text/plain",
            )

        rel = request.match_info.get("path", "").strip("/")
        requested = (_react_app_dist / rel).resolve() if rel else _react_app_dist / "index.html"
        root = _react_app_dist.resolve()
        try:
            requested.relative_to(root)
        except ValueError:
            return web.Response(text="not found", status=404)

        if requested.is_file():
            headers = _no_store_headers() if requested.name == "index.html" else None
            return web.FileResponse(requested, headers=headers)
        return web.FileResponse(root / "index.html", headers=_no_store_headers())

    async def web_auth_js(_request: web.Request) -> web.Response:
        return web.Response(
            text=_load_web_auth_js(),
            content_type="application/javascript",
            headers=_no_store_headers(),
        )

    async def web_bootstrap(request: web.Request) -> web.Response:
        api_key = str(request.app.get("api_key") or "")
        config = request.app.get("config")
        web_host = str(getattr(config, "web_host", "") or "")
        public_bind = web_host not in ("", "127.0.0.1", "::1", "localhost")
        return web.json_response(
            {
                "auth": {
                    "required": bool(api_key),
                    "scheme": "x-api-key",
                    "storage": "browser-localStorage",
                },
                "server": {
                    "web_host": web_host,
                    "public_bind": public_bind,
                    "unauthenticated_allowed": not bool(api_key),
                },
                "stream_auth": {
                    "shape": "fetch-event-stream",
                    "header": "X-API-Key",
                    "native_eventsource_supported_when_auth_required": False,
                },
            },
            headers=_no_store_headers(),
        )

    async def ops_page(request: web.Request) -> web.Response:
        # Static HTML shell — frontend AJAX-fetches /api/ops via the
        # shared auth helper. We still validate ``?days=`` here
        # so a malformed value gets a clear error before the JS tries
        # to use the same query string against the data endpoint.
        try:
            parse_days_param(request.query.get("days"))
        except ValueError as exc:
            return web.Response(text=str(exc), status=400)
        return web.Response(
            text=render_dashboard_html(), content_type="text/html",
        )

    async def ops_data(request: web.Request) -> web.Response:
        try:
            days = parse_days_param(request.query.get("days"))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(
            await build_dashboard_payload_async(
                events_log, days, home=home,
                active_provider=active_usage_provider,
            ),
        )

    # ── /saga — saga DB viewer ───────────────────────────────────────

    # Resolve the DB path: use the explicit ``saga_db`` kwarg when
    # provided (server.py passes the saga.toml-resolved path); otherwise
    # derive from ``home``. The canonical location is
    # ``<home>/.mimir/saga.db`` (saga's default ``[storage].db_path``);
    # the older ``<home>/state/saga.db`` fallback predated the move to
    # ``.mimir/`` and pointed at a file that no longer exists.
    _saga_db: Path | None = saga_db
    if _saga_db is None and home is not None:
        _saga_db = home / ".mimir" / "saga.db"

    async def saga_page(_request: web.Request) -> web.Response:
        return web.Response(text=render_saga_html(), content_type="text/html")

    async def saga_data(request: web.Request) -> web.Response:
        view = request.query.get("view", "recent")

        if _saga_db is None:
            return web.json_response(
                {"error": "saga_db path not configured"}, status=503
            )

        if view == "stats":
            payload = await asyncio.to_thread(build_db_stats_payload, _saga_db)
        elif view == "atom":
            atom_id = request.query.get("id", "").strip()
            if not atom_id:
                return web.json_response({"error": "id param required"}, status=400)
            payload = await asyncio.to_thread(build_atom_payload, _saga_db, atom_id)
        elif view == "search":
            query = request.query.get("q", "").strip()
            if not query:
                return web.json_response({"error": "q param required"}, status=400)
            channel = request.query.get("channel", "").strip() or None
            try:
                limit = int(request.query.get("limit") or 100)
            except ValueError:
                limit = 100
            payload = await asyncio.to_thread(
                build_search_payload,
                _saga_db, query, channel=channel, limit=limit,  # type: ignore[arg-type]
            )
        elif view == "activation_hist":
            try:
                days = int(request.query.get("days") or 7)
            except ValueError:
                days = 7
            payload = await asyncio.to_thread(
                build_activation_hist_payload,
                _saga_db, days=days,  # type: ignore[arg-type]
            )
        elif view == "clusters":
            try:
                sample_size = int(request.query.get("sample_size") or 3)
            except ValueError:
                sample_size = 3
            payload = await asyncio.to_thread(
                build_clusters_payload,
                _saga_db, sample_size=sample_size,  # type: ignore[arg-type]
            )
        else:  # view == "recent" (default)
            channel = request.query.get("channel", "").strip() or None
            try:
                limit = int(request.query.get("limit") or 50)
            except ValueError:
                limit = 50
            payload = await asyncio.to_thread(
                build_recent_atoms_payload,
                _saga_db, channel=channel, limit=limit,  # type: ignore[arg-type]
            )

        if "error" in payload and not payload.get("atoms") and view not in ("recent", "clusters", "activation_hist"):
            return web.json_response(payload, status=404 if "not found" in str(payload["error"]) else 503)
        return web.json_response(payload)

    if ("GET", "/turns") not in existing:
        app.router.add_get("/turns", turns_page)
    if ("GET", "/api/turns") not in existing:
        app.router.add_get("/api/turns", turns_data)
    if ("GET", "/api/events") not in existing:
        app.router.add_get("/api/events", events_data)
    if ("GET", "/app") not in existing:
        app.router.add_get("/app", react_app)
    if ("GET", "/app/auth.js") not in existing:
        app.router.add_get("/app/auth.js", web_auth_js)
    if ("GET", "/api/web/bootstrap") not in existing:
        app.router.add_get("/api/web/bootstrap", web_bootstrap)
    if ("GET", "/app/") not in existing and ("GET", "/app/{path}") not in existing:
        app.router.add_get("/app/{path:.*}", react_app)
    if ("GET", "/ops") not in existing:
        app.router.add_get("/ops", ops_page)
    if ("GET", "/api/ops") not in existing:
        app.router.add_get("/api/ops", ops_data)
    async def saga_sql(request: web.Request) -> web.Response:
        """POST /api/saga/sql — read-only SQL passthrough.

        Accepts ``{"sql": "<SELECT ...>"}`` as a JSON body.
        Rejects any statement that is not a SELECT / EXPLAIN / WITH, and
        rejects any statement containing write keywords.  Results are
        capped at 1 000 rows.
        """
        if _saga_db is None:
            return web.json_response(
                {"error": "saga_db path not configured"}, status=503
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        sql = (body.get("sql") or "").strip()
        if not sql:
            return web.json_response({"error": "sql field is required"}, status=400)

        payload = await asyncio.to_thread(build_sql_payload, _saga_db, sql)
        if payload.get("rejected"):
            return web.json_response(payload, status=400)
        if "error" in payload:
            return web.json_response(payload, status=500)
        return web.json_response(payload)

    if ("GET", "/saga") not in existing:
        app.router.add_get("/saga", saga_page)
    if ("GET", "/api/saga") not in existing:
        app.router.add_get("/api/saga", saga_data)
    if os.environ.get("MIMIR_SAGA_SQL_ENABLED", "").strip() == "1":
        if ("POST", "/api/saga/sql") not in existing:
            app.router.add_post("/api/saga/sql", saga_sql)
            log.info("saga SQL passthrough enabled (MIMIR_SAGA_SQL_ENABLED=1)")
    else:
        log.debug("saga SQL passthrough disabled (set MIMIR_SAGA_SQL_ENABLED=1 to enable)")

    # ── /memory — file-based memory viewer ──────────────────────────────
    # Phase 3: also exposes view=channels for the channel-filter dropdown.

    _memory_roots: list[Any] = (
        [home / "memory", home / "state"] if home is not None else []
    )

    async def memory_page(_request: web.Request) -> web.Response:
        return web.Response(text=render_memory_html(), content_type="text/html")

    async def memory_data(request: web.Request) -> web.Response:
        view = request.query.get("view", "tree")
        if not _memory_roots:
            return web.json_response({"error": "home not configured"}, status=503)

        if view == "file":
            rel = request.query.get("path", "").strip()
            if not rel:
                return web.json_response({"error": "path param required"}, status=400)
            payload = await asyncio.to_thread(
                read_file_safe_multi, _memory_roots, rel,
            )
            if "error" in payload:
                err = payload["error"]
                if "traversal" in err or "only .md" in err or "not in any" in err:
                    return web.json_response(payload, status=400)
                if "not found" in err:
                    return web.json_response(payload, status=404)
            return web.json_response(payload)

        if view == "search":
            q = request.query.get("q", "").strip()
            if not q:
                return web.json_response({"error": "q param required"}, status=400)
            payload = await asyncio.to_thread(search_files, _memory_roots, q)
            return web.json_response(payload)

        if view == "channels":
            # memory/ root is first in _memory_roots; channel dirs live under it.
            mem_root = _memory_roots[0]
            channels = await asyncio.to_thread(list_channel_dirs, mem_root)
            return web.json_response({"channels": channels})

        # Default: view == "tree"
        payload = await asyncio.to_thread(list_trees, _memory_roots)
        return web.json_response(payload)

    # Renamed /memory → /state (the page surfaces <home>/state/ + memory/).
    if ("GET", "/state") not in existing:
        app.router.add_get("/state", memory_page)
    if ("GET", "/api/memory") not in existing:
        app.router.add_get("/api/memory", memory_data)
