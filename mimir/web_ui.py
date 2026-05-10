"""Turn-viewer + ops dashboard + log API routes (SPEC §11).

Mounts onto the same aiohttp app that serves ``/event`` + ``/health`` and
hosts the WebUI bridge's ``/chat``. Routes:

  GET /turns            — single-file vanilla-JS turn viewer (HTML)
  GET /api/turns        — turns.jsonl as JSON (optional ``?after=<turn_id>``)
  GET /api/events       — events.jsonl as JSON (optional ``?since=<ts>``,
                          ``?type=<kind>``, ``?limit=<n>``); type may be
                          repeated to combine filters
  GET /ops              — live ops dashboard (HTML, Chart.js)
  GET /api/ops          — JSON twin of /ops for ad-hoc scripting

The HTML page polls ``/api/turns`` every 5s for live updates. ``/api/events``
is exposed for the (deferred) Events tab + ad-hoc scripting. ``/ops``
recomputes from events.jsonl on every request — no caching.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from ._jsonl_tail import tail_jsonl_records
from .ops_dashboard import (
    build_dashboard_payload_async,
    parse_days_param,
    render_dashboard_html,
)

log = logging.getLogger(__name__)

_TURN_VIEWER_HTML: str | None = None


def _load_viewer_html() -> str:
    """Load the bundled HTML once and cache it."""
    global _TURN_VIEWER_HTML
    if _TURN_VIEWER_HTML is None:
        path = Path(__file__).parent / "turn_viewer.html"
        _TURN_VIEWER_HTML = path.read_text(encoding="utf-8")
    return _TURN_VIEWER_HTML


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
) -> None:
    """Add viewer + API routes to an existing aiohttp app.

    Idempotent — skips routes that already exist (so calling this twice in a
    rebuild is harmless).

    ``home`` is passed to ``build_dashboard_payload`` so the /ops
    dashboard's Chainlink tab can run ``chainlink issue list --json``
    against the right repo. None disables the Chainlink tab gracefully
    (renders an "unavailable" message)."""

    existing = {(r.method, r.resource.canonical) for r in app.router.routes()}

    async def turns_page(_request: web.Request) -> web.Response:
        return web.Response(text=_load_viewer_html(), content_type="text/html")

    async def turns_data(request: web.Request) -> web.Response:
        records = _read_jsonl(turns_log)
        after = request.query.get("after", "")
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

    async def ops_page(request: web.Request) -> web.Response:
        try:
            days = parse_days_param(request.query.get("days"))
        except ValueError as exc:
            return web.Response(text=str(exc), status=400)
        stats = await build_dashboard_payload_async(
            events_log, days, home=home,
        )
        return web.Response(
            text=render_dashboard_html(stats), content_type="text/html",
        )

    async def ops_data(request: web.Request) -> web.Response:
        try:
            days = parse_days_param(request.query.get("days"))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(
            await build_dashboard_payload_async(events_log, days, home=home),
        )

    if ("GET", "/turns") not in existing:
        app.router.add_get("/turns", turns_page)
    if ("GET", "/api/turns") not in existing:
        app.router.add_get("/api/turns", turns_data)
    if ("GET", "/api/events") not in existing:
        app.router.add_get("/api/events", events_data)
    if ("GET", "/ops") not in existing:
        app.router.add_get("/ops", ops_page)
    if ("GET", "/api/ops") not in existing:
        app.router.add_get("/api/ops", ops_data)
