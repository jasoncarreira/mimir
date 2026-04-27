"""Turn-viewer + log API routes (SPEC §11).

Mounts onto the same aiohttp app that serves ``/event`` + ``/health`` and
hosts the WebUI bridge's ``/chat``. Routes:

  GET /turns            — single-file vanilla-JS turn viewer (HTML)
  GET /api/turns        — turns.jsonl as JSON (optional ``?after=<turn_id>``)
  GET /api/events       — events.jsonl as JSON (optional ``?since=<ts>``,
                          ``?type=<kind>``, ``?limit=<n>``); type may be
                          repeated to combine filters

The HTML page polls ``/api/turns`` every 5s for live updates. ``/api/events``
is exposed for the (deferred) Events tab + ad-hoc scripting.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)

_TURN_VIEWER_HTML: str | None = None


def _load_viewer_html() -> str:
    """Load the bundled HTML once and cache it."""
    global _TURN_VIEWER_HTML
    if _TURN_VIEWER_HTML is None:
        path = Path(__file__).parent / "turn_viewer.html"
        _TURN_VIEWER_HTML = path.read_text(encoding="utf-8")
    return _TURN_VIEWER_HTML


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read every well-formed JSON line; silently skip malformed records."""
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def register_routes(
    app: web.Application,
    *,
    turns_log: Path,
    events_log: Path,
) -> None:
    """Add viewer + API routes to an existing aiohttp app.

    Idempotent — skips routes that already exist (so calling this twice in a
    rebuild is harmless)."""

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

    if ("GET", "/turns") not in existing:
        app.router.add_get("/turns", turns_page)
    if ("GET", "/api/turns") not in existing:
        app.router.add_get("/api/turns", turns_data)
    if ("GET", "/api/events") not in existing:
        app.router.add_get("/api/events", events_data)
