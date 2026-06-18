"""Turn-viewer + ops dashboard + log API + saga DB + memory routes (SPEC §11).

Mounts onto the same aiohttp app that serves ``/event`` + ``/health`` and
hosts the WebUI bridge's ``/chat``. Routes:

  GET /turns            — single-file vanilla-JS turn viewer (HTML)
  GET /api/turns        — turns.jsonl as JSON (optional ``?after=<turn_id>``)
  GET /api/events       — events.jsonl as JSON (optional ``?since=<ts>``,
                          ``?type=<kind>``, ``?limit=<n>``); type may be
                          repeated to combine filters
  GET /api/v1/live-events — fetch-authenticated SSE stream for React live
                          dashboards with cursor backfill/dedup semantics
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
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from ._jsonl_tail import tail_jsonl_records
from .admin_config import build_admin_config_payload
from .dashboard_extensions import (
    DashboardBackendRoute,
    DashboardExtensionRegistry,
    add_backend_namespace_routes,
    first_party_dashboard_extensions,
)
from .chainlink_board import (
    build_chainlink_board_payload,
    resolve_worklink_artifact,
)
from .live_events import read_live_event_items_since
from .ops_dashboard import (
    build_dashboard_payload_async,
    parse_days_param,
    render_dashboard_html,
)
from .scheduler_dashboard import (
    build_scheduler_dashboard_payload,
    parse_due_window,
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
from .web_contracts import json_error, json_success, list_meta

log = logging.getLogger(__name__)

_TURN_VIEWER_HTML: str | None = None
_WEB_AUTH_JS: str | None = None
LIVE_EVENTS_HEARTBEAT_S = 15.0
LIVE_EVENTS_POLL_S = 1.0
LIVE_EVENTS_MAX_STREAMS = int(os.environ.get("MIMIR_LIVE_EVENTS_MAX_STREAMS", "8"))


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


def web_gate_active(api_key: str | None, resolver: Any) -> bool:
    """Whether the web auth gate is active (github #726).

    Single source of truth shared by the auth middleware (server.py) AND
    /web/bootstrap, so the auth state the browser is told never drifts from
    what the middleware actually enforces. The gate is on when a master key is
    set OR per-user web keys exist (the fail-safe: configuring users can't
    leave the server open even if MIMIR_API_KEY is unset)."""
    if api_key:
        return True
    return resolver is not None and resolver.has_web_keys()


def _whoami_payload(identity: Any, is_master: bool) -> dict[str, Any]:
    """The ``/api/v1/whoami`` body for a resolved auth identity (github #726).

    Pure (no request object) so it's unit-testable. ``identity`` is the
    ``Identity`` the auth middleware resolved from the presented key — ``None``
    for the admin master key or dev/open mode. The master key reports as a
    role-admin, non-chat operator; an unresolved identity reports empty."""
    if is_master:
        return {
            "canonical": None,
            "display_name": "operator (master key)",
            "roles": ["admin"],
            "is_admin": True,
            "is_master": True,
        }
    if identity is not None:
        return {
            "canonical": identity.canonical,
            "display_name": identity.display_name,
            "roles": list(identity.access.roles),
            "is_admin": identity.access.is_admin,
            "is_master": False,
        }
    return {
        "canonical": None,
        "display_name": None,
        "roles": [],
        "is_admin": False,
        "is_master": False,
    }


def register_routes(
    app: web.Application,
    *,
    turns_log: Path,
    events_log: Path,
    home: Path | None = None,
    saga_db: Path | None = None,
    commitments_store: Any | None = None,
    active_usage_provider: str | None = None,
    react_app_dist: Path | None = None,
    dashboard_extensions: DashboardExtensionRegistry | None = None,
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
    to the packaged ``mimir/react_app/dist`` directory.

    ``dashboard_extensions`` is the trusted first-party dashboard registry.
    Enabled manifests drive optional backend namespace hook registration; this
    is not a remote plugin loading mechanism."""

    existing = {(r.method, r.resource.canonical) for r in app.router.routes()}
    _react_app_dist = react_app_dist or (Path(__file__).parent / "react_app" / "dist")
    _dashboard_extensions = dashboard_extensions or first_party_dashboard_extensions()

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

    def _turns_window(request: web.Request) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        records = _read_jsonl(turns_log)
        after = request.query.get("after", "")
        before = request.query.get("before", "")
        try:
            limit = int(request.query.get("limit") or 0)
        except ValueError:
            limit = 0

        total = len(records)
        if after:
            cut = []
            seen = False
            for r in records:
                if seen:
                    cut.append(r)
                elif r.get("turn_id") == after:
                    seen = True
            cursor = str(cut[-1].get("turn_id")) if cut and cut[-1].get("turn_id") else None
            return cut, list_meta(cursor=cursor, limit=limit or None, total=total, truncated=False)
        if before:
            idx = next(
                (i for i, r in enumerate(records) if r.get("turn_id") == before),
                None,
            )
            if idx is None:
                window = []
            else:
                start = max(0, idx - limit) if limit > 0 else 0
                window = records[start:idx]
            cursor = str(window[0].get("turn_id")) if window and window[0].get("turn_id") else None
            return window, list_meta(
                cursor=cursor,
                limit=limit or None,
                total=total,
                truncated=idx is not None and bool(limit > 0 and idx > limit),
            )
        window = records[-limit:] if limit > 0 else records
        cursor = str(window[-1].get("turn_id")) if window and window[-1].get("turn_id") else None
        return window, list_meta(
            cursor=cursor,
            limit=limit or None,
            total=total,
            truncated=bool(limit > 0 and total > limit),
        )

    async def turns_data_v1(request: web.Request) -> web.Response:
        turns, meta = _turns_window(request)
        return json_success({"turns": turns}, meta=meta)

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

    def _events_window(request: web.Request) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        records = _read_jsonl(events_log)
        since = request.query.get("since", "").strip()
        types = request.query.getall("type", []) or []
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
        total = len(out)
        if limit > 0:
            out = out[-limit:]
        cursor = str(out[-1].get("timestamp")) if out and out[-1].get("timestamp") else None
        return out, list_meta(
            cursor=cursor,
            limit=limit or None,
            total=total,
            truncated=bool(limit > 0 and total > limit),
        )

    async def events_data_v1(request: web.Request) -> web.Response:
        events, meta = _events_window(request)
        return json_success({"events": events}, meta=meta)

    live_events_active = 0
    live_events_lock = asyncio.Lock()

    async def _try_acquire_live_event_slot() -> bool:
        nonlocal live_events_active
        async with live_events_lock:
            if live_events_active >= LIVE_EVENTS_MAX_STREAMS:
                return False
            live_events_active += 1
            return True

    async def _release_live_event_slot() -> None:
        nonlocal live_events_active
        async with live_events_lock:
            live_events_active = max(0, live_events_active - 1)

    async def _live_event_items(request: web.Request, since: str | None) -> list[dict[str, Any]]:
        try:
            limit = int(request.query.get("limit") or 0)
        except ValueError:
            limit = 0
        items = await asyncio.to_thread(
            read_live_event_items_since,
            turns_log,
            since=since,
            limit=limit or None,
        )
        return [item.as_dict() for item in items]

    async def live_events_stream(request: web.Request) -> web.StreamResponse:
        """Fetch-authenticated SSE stream for React live dashboards.

        Reconnect/backfill contract:
        - each payload is ``{"id", "cursor", "ts", "event"}``;
        - clients persist the highest delivered cursor and reconnect with
          ``?since=<cursor>``;
        - backfill uses strict ``cursor > since`` comparison, so the last
          acknowledged event is not duplicated.
        """
        once = request.query.get("once") in {"1", "true", "yes"}
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        if not await _try_acquire_live_event_slot():
            return web.Response(text="too many live event streams", status=429)

        await resp.prepare(request)

        delivered = request.query.get("since", "").strip() or None
        idle_for = 0.0
        try:
            while True:
                items = await _live_event_items(request, delivered)
                for item in items:
                    delivered = str(item["cursor"])
                    block = (
                        f"id: {item['cursor']}\n"
                        "event: live-event\n"
                        "data: "
                        + json.dumps(item, ensure_ascii=False)
                        + "\n\n"
                    )
                    await resp.write(block.encode("utf-8"))
                if once:
                    break
                if items:
                    idle_for = 0.0
                else:
                    idle_for += LIVE_EVENTS_POLL_S
                    if idle_for >= LIVE_EVENTS_HEARTBEAT_S:
                        idle_for = 0.0
                        await resp.write(b": heartbeat\n\n")
                await asyncio.sleep(LIVE_EVENTS_POLL_S)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await _release_live_event_slot()
        return resp

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
        gate = web_gate_active(api_key, request.app.get("identity_resolver"))
        config = request.app.get("config")
        web_host = str(getattr(config, "web_host", "") or "")
        public_bind = web_host not in ("", "127.0.0.1", "::1", "localhost")
        return web.json_response(
            {
                "auth": {
                    "required": gate,
                    "scheme": "x-api-key",
                    "storage": "browser-localStorage",
                },
                "server": {
                    "web_host": web_host,
                    "public_bind": public_bind,
                    "unauthenticated_allowed": not gate,
                },
                "stream_auth": {
                    "shape": "fetch-event-stream",
                    "header": "X-API-Key",
                    "native_eventsource_supported_when_auth_required": False,
                },
                "dashboard_extensions": _dashboard_extensions.navigation_payload(),
            },
            headers=_no_store_headers(),
        )

    async def web_bootstrap_v1(request: web.Request) -> web.Response:
        api_key = str(request.app.get("api_key") or "")
        gate = web_gate_active(api_key, request.app.get("identity_resolver"))
        config = request.app.get("config")
        web_host = str(getattr(config, "web_host", "") or "")
        public_bind = web_host not in ("", "127.0.0.1", "::1", "localhost")
        return json_success(
            {
                "auth": {
                    "required": gate,
                    "scheme": "x-api-key",
                    "storage": "browser-localStorage",
                },
                "server": {
                    "web_host": web_host,
                    "public_bind": public_bind,
                    "unauthenticated_allowed": not gate,
                },
                "stream_auth": {
                    "shape": "fetch-event-stream",
                    "header": "X-API-Key",
                    "native_eventsource_supported_when_auth_required": False,
                },
                "dashboard_extensions": _dashboard_extensions.navigation_payload(),
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

    async def ops_data_v1(request: web.Request) -> web.Response:
        try:
            days = parse_days_param(request.query.get("days"))
        except ValueError as exc:
            return json_error("invalid_days", str(exc), status=400)
        payload = await build_dashboard_payload_async(
            events_log,
            days,
            home=home,
            active_provider=active_usage_provider,
        )
        return json_success(payload)

    async def chainlink_board_data_v1(_request: web.Request) -> web.Response:
        payload = await build_chainlink_board_payload(home)
        return json_success(payload)

    async def chainlink_board_artifact_v1(request: web.Request) -> web.StreamResponse:
        if home is None:
            return json_error("home_not_configured", "home path not configured", status=503)
        artifact = resolve_worklink_artifact(home, request.query.get("path", ""))
        if artifact is None:
            return json_error("artifact_not_found", "artifact not found", status=404)
        return web.FileResponse(artifact, headers=_no_store_headers())

    async def scheduler_data_v1(request: web.Request) -> web.Response:
        try:
            due_window = parse_due_window(request.query.get("due_window"))
        except ValueError as exc:
            return json_error("invalid_due_window", str(exc), status=400)

        def _build_payload() -> dict[str, Any]:
            return build_scheduler_dashboard_payload(
                scheduler=request.app.get("scheduler"),
                commitments_store=commitments_store,
                events=_read_jsonl(events_log),
                due_window=due_window,
            )

        payload = await asyncio.to_thread(_build_payload)
        return json_success(
            payload,
            meta=list_meta(
                total=(
                    len(payload.get("schedules", []))
                    + len(payload.get("pollers", []))
                    + len(payload.get("commitments", []))
                ),
            ),
        )

    async def admin_config_v1(request: web.Request) -> web.Response:
        payload = await asyncio.to_thread(
            build_admin_config_payload,
            config=request.app.get("config"),
            scheduler=request.app.get("scheduler"),
            home=home,
        )
        return json_success(payload, headers=_no_store_headers())


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

    async def saga_data_v1(request: web.Request) -> web.Response:
        view = request.query.get("view", "recent")

        if _saga_db is None:
            return json_error("saga_db_not_configured", "saga_db path not configured", status=503)

        meta: dict[str, Any] | None = None
        if view == "stats":
            payload = await asyncio.to_thread(build_db_stats_payload, _saga_db)
        elif view == "atom":
            atom_id = request.query.get("id", "").strip()
            if not atom_id:
                return json_error("missing_id", "id param required", status=400)
            payload = await asyncio.to_thread(build_atom_payload, _saga_db, atom_id)
        elif view == "search":
            query = request.query.get("q", "").strip()
            if not query:
                return json_error("missing_query", "q param required", status=400)
            channel = request.query.get("channel", "").strip() or None
            try:
                limit = int(request.query.get("limit") or 100)
            except ValueError:
                limit = 100
            payload = await asyncio.to_thread(
                build_search_payload,
                _saga_db, query, channel=channel, limit=limit,  # type: ignore[arg-type]
            )
            total = int(payload.get("total_matched") or len(payload.get("atoms") or []))
            payload = dict(payload)
            payload.pop("total_matched", None)
            payload.pop("limit", None)
            meta = list_meta(
                cursor=None,
                limit=limit,
                total=total,
                truncated=total > limit,
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
            total = int(payload.pop("total", 0) or 0)
            meta = list_meta(cursor=None, limit=None, total=total, truncated=False)
        elif view == "clusters":
            try:
                sample_size = int(request.query.get("sample_size") or 3)
            except ValueError:
                sample_size = 3
            payload = await asyncio.to_thread(
                build_clusters_payload,
                _saga_db, sample_size=sample_size,  # type: ignore[arg-type]
            )
            total = int(payload.pop("total_clusters", 0) or 0)
            meta = list_meta(cursor=None, limit=None, total=total, truncated=False)
        else:
            channel = request.query.get("channel", "").strip() or None
            try:
                limit = int(request.query.get("limit") or 50)
            except ValueError:
                limit = 50
            payload = await asyncio.to_thread(
                build_recent_atoms_payload,
                _saga_db, channel=channel, limit=limit,  # type: ignore[arg-type]
            )
            total = int(payload.get("total") or len(payload.get("atoms") or []))
            payload = dict(payload)
            payload.pop("total", None)
            payload.pop("limit", None)
            atoms = payload.get("atoms") or []
            cursor = str(atoms[-1].get("id")) if atoms and atoms[-1].get("id") else None
            meta = list_meta(cursor=cursor, limit=limit, total=total, truncated=total > limit)

        if "error" in payload:
            status = 404 if "not found" in str(payload["error"]) else 503
            return json_error("saga_error", str(payload["error"]), status=status)
        return json_success(payload, meta=meta)

    async def whoami_v1(request: web.Request) -> web.Response:
        """GET /api/v1/whoami — the authenticated caller's identity + roles.

        Lets the React app adapt to the user (hide admin-only sections for
        non-admins; show who you're posting chat as). Auth-required; reads the
        identity the auth middleware resolved from the presented X-API-Key
        (github #726). The master key reports as a role-admin, non-chat
        operator; dev/open mode (no key configured) reports an empty identity."""
        return json_success(
            _whoami_payload(
                request.get("auth_identity"),
                bool(request.get("auth_is_master")),
            )
        )

    if ("GET", "/turns") not in existing:
        app.router.add_get("/turns", turns_page)
    if ("GET", "/api/turns") not in existing:
        app.router.add_get("/api/turns", turns_data)
    if ("GET", "/api/v1/turns") not in existing:
        app.router.add_get("/api/v1/turns", turns_data_v1)
    if ("GET", "/api/events") not in existing:
        app.router.add_get("/api/events", events_data)
    if ("GET", "/api/v1/events") not in existing:
        app.router.add_get("/api/v1/events", events_data_v1)
    if ("GET", "/api/v1/live-events") not in existing:
        app.router.add_get("/api/v1/live-events", live_events_stream)
    if ("GET", "/app") not in existing:
        app.router.add_get("/app", react_app)
    if ("GET", "/app/auth.js") not in existing:
        app.router.add_get("/app/auth.js", web_auth_js)
    if ("GET", "/api/web/bootstrap") not in existing:
        app.router.add_get("/api/web/bootstrap", web_bootstrap)
    if ("GET", "/api/v1/web/bootstrap") not in existing:
        app.router.add_get("/api/v1/web/bootstrap", web_bootstrap_v1)
    if ("GET", "/api/v1/whoami") not in existing:
        app.router.add_get("/api/v1/whoami", whoami_v1)
    if ("GET", "/app/") not in existing and ("GET", "/app/{path}") not in existing:
        app.router.add_get("/app/{path:.*}", react_app)
    if ("GET", "/ops") not in existing:
        app.router.add_get("/ops", ops_page)
        existing.add(("GET", "/ops"))

    def ops_backend_routes() -> list[DashboardBackendRoute]:
        return [
            DashboardBackendRoute("GET", "/api/ops", ops_data),
            DashboardBackendRoute("GET", "/api/v1/ops", ops_data_v1),
        ]

    def chainlink_board_backend_routes() -> list[DashboardBackendRoute]:
        return [
            DashboardBackendRoute("GET", "/api/v1/chainlink-board", chainlink_board_data_v1),
            DashboardBackendRoute("GET", "/api/v1/chainlink-board/artifact", chainlink_board_artifact_v1),
        ]

    def scheduler_backend_routes() -> list[DashboardBackendRoute]:
        return [
            DashboardBackendRoute("GET", "/api/v1/scheduler", scheduler_data_v1),
        ]

    def admin_config_backend_routes() -> list[DashboardBackendRoute]:
        return [
            DashboardBackendRoute("GET", "/api/v1/admin/config", admin_config_v1),
        ]

    add_backend_namespace_routes(
        app,
        registry=_dashboard_extensions,
        hooks={
            "ops": ops_backend_routes,
            "chainlink-board": chainlink_board_backend_routes,
            "scheduler": scheduler_backend_routes,
            "admin-config": admin_config_backend_routes,
        },
        existing=existing,
    )
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

    async def saga_sql_v1(request: web.Request) -> web.Response:
        if _saga_db is None:
            return json_error("saga_db_not_configured", "saga_db path not configured", status=503)
        try:
            body = await request.json()
        except Exception:
            return json_error("invalid_json", "invalid JSON body", status=400)
        sql = (body.get("sql") or "").strip()
        if not sql:
            return json_error("missing_sql", "sql field is required", status=400)

        payload = await asyncio.to_thread(build_sql_payload, _saga_db, sql)
        if payload.get("rejected"):
            return json_error("sql_rejected", str(payload.get("error") or "SQL rejected"), status=400)
        if "error" in payload:
            return json_error("sql_error", str(payload["error"]), status=500)
        meta = list_meta(
            cursor=None,
            limit=None,
            total=int(payload.get("row_count") or len(payload.get("rows") or [])),
            truncated=bool(payload.get("truncated")),
        )
        return json_success(payload, meta=meta)

    if ("GET", "/saga") not in existing:
        app.router.add_get("/saga", saga_page)
    if ("GET", "/api/saga") not in existing:
        app.router.add_get("/api/saga", saga_data)
    if ("GET", "/api/v1/saga") not in existing:
        app.router.add_get("/api/v1/saga", saga_data_v1)
    if os.environ.get("MIMIR_SAGA_SQL_ENABLED", "").strip() == "1":
        if ("POST", "/api/saga/sql") not in existing:
            app.router.add_post("/api/saga/sql", saga_sql)
        if ("POST", "/api/v1/saga/sql") not in existing:
            app.router.add_post("/api/v1/saga/sql", saga_sql_v1)
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

    async def memory_data_v1(request: web.Request) -> web.Response:
        view = request.query.get("view", "tree")
        if not _memory_roots:
            return json_error("home_not_configured", "home not configured", status=503)

        if view == "file":
            rel = request.query.get("path", "").strip()
            if not rel:
                return json_error("missing_path", "path param required", status=400)
            payload = await asyncio.to_thread(
                read_file_safe_multi, _memory_roots, rel,
            )
            if "error" in payload:
                err = str(payload["error"])
                status = 400
                if "not found" in err:
                    status = 404
                return json_error("memory_file_error", err, status=status)
            return json_success(payload)

        if view == "search":
            q = request.query.get("q", "").strip()
            if not q:
                return json_error("missing_query", "q param required", status=400)
            payload = await asyncio.to_thread(search_files, _memory_roots, q)
            total = int(payload.pop("total", 0) or 0)
            truncated = bool(payload.pop("truncated", False))
            return json_success(
                payload,
                meta=list_meta(cursor=None, limit=None, total=total, truncated=truncated),
            )

        if view == "channels":
            mem_root = _memory_roots[0]
            channels = await asyncio.to_thread(list_channel_dirs, mem_root)
            return json_success(
                {"channels": channels},
                meta=list_meta(cursor=None, limit=None, total=len(channels), truncated=False),
            )

        payload = await asyncio.to_thread(list_trees, _memory_roots)
        return json_success(payload)

    # Renamed /memory → /state (the page surfaces <home>/state/ + memory/).
    if ("GET", "/state") not in existing:
        app.router.add_get("/state", memory_page)
    if ("GET", "/api/memory") not in existing:
        app.router.add_get("/api/memory", memory_data)
    if ("GET", "/api/v1/memory") not in existing:
        app.router.add_get("/api/v1/memory", memory_data_v1)
