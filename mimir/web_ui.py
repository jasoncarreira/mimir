"""Turn-viewer + ops dashboard + log API + saga DB + memory routes (SPEC §11).

Mounts onto the same aiohttp app that serves ``/event`` + ``/health`` and
hosts the WebUI bridge's ``/chat``. Routes:

  GET /turns            — legacy single-file vanilla-JS turn viewer (HTML)
  GET /api/turns        — turns.jsonl as JSON (optional ``?after=<turn_id>``)
  GET /api/events       — events.jsonl as JSON (optional ``?since=<ts>``,
                          ``?type=<kind>``, ``?limit=<n>``); type may be
                          repeated to combine filters
  GET /api/v1/live-events — fetch-authenticated SSE stream for React live
                          dashboards with cursor backfill/dedup semantics
  GET /ops              — legacy live ops dashboard (HTML, Chart.js)
  GET /api/ops          — JSON twin of /ops for ad-hoc scripting
  GET /saga             — legacy saga DB operator viewer (HTML)
  GET /api/saga         — JSON twin of /saga; view= selects the payload
                          shape (recent, atom, stats)
  GET /state            — legacy file-based memory browser (HTML, two-pane;
                          renamed from /memory — surfaces memory/ + state/)
  GET /api/memory       — JSON twin of /state; view=tree returns nested
                          dir/file tree (memory/ + state/); view=file&path=...
                          returns file content (only .md files);
                          view=search&q=... returns full-text search hits;
                          view=channels returns channel dir names
  GET /app              — built React app shell (default frontend)

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
from typing import Any, Callable

from aiohttp import web

from . import __version__
from ._jsonl_tail import _tail_lines, count_lines_chunked, tail_jsonl_records
from .admin_config import build_admin_config_payload
from .admin_users import build_users_payload, roles_for_request
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
from .web_channels import DEFAULT_WEB_CHANNEL, web_channel_for_identity
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
from .session_browser import build_sessions_payload
from .web_contracts import json_error, json_success, list_meta
from .wiki_backlinks import (
    _category_of,
    _posix,
    _title_from_markdown,
    build_graph,
    build_wiki_payload,
)

log = logging.getLogger(__name__)

_TURN_VIEWER_HTML: str | None = None
_WEB_AUTH_JS: str | None = None
# Under the agent-writable ``state/`` root (DEFAULT_FOLDERS) so the agent can
# edit it with its own Write/Edit tools — a top-level <home> file is outside the
# WriteGuardBackend's writable roots and would be refused.
WEB_UI_CONFIG_RELPATH = ("state", "web_ui.json")
DEFAULT_AGENT_NAME = "Mimir"
DEFAULT_WEB_SKIN = "neon-terminal"
BUILT_IN_WEB_SKIN_IDS = frozenset({"default-retro", "neon-terminal", "cosmic-nebula"})
OPERATOR_SKINS_RELPATH = ("skins",)
SKIN_TOKEN_NAMES = frozenset(
    {
        "colorText",
        "colorTextMuted",
        "colorBackground",
        "colorChromeBackground",
        "colorChromeBorder",
        "colorChromeAccent",
        "colorChromeAccentText",
        "colorPanelBackground",
        "colorPanelBackgroundMuted",
        "colorPanelBorder",
        "colorPanelBorderHover",
        "colorPanelShadow",
        "colorStatusInfo",
        "colorStatusInfoBackground",
        "colorStatusSuccess",
        "colorStatusSuccessBackground",
        "colorStatusWarning",
        "colorStatusWarningBackground",
        "colorStatusDanger",
        "colorStatusDangerBackground",
        "colorTimelineReasoning",
        "colorTimelineReasoningBackground",
        "colorTimelineToolCall",
        "colorTimelineToolCallBackground",
        "colorTimelineToolResult",
        "colorTimelineToolResultBackground",
        "colorCodeBackground",
        "colorCodeText",
        "colorFocusRing",
        "fontFamilyBase",
        "fontFamilyMono",
        "fontSizeXs",
        "fontSizeSm",
        "fontSizeMd",
        "fontSizeLg",
        "fontWeightRegular",
        "fontWeightStrong",
        "lineHeightTight",
        "lineHeightBody",
        "radiusPanel",
        "radiusControl",
        "space2xs",
        "spaceXs",
        "spaceSm",
        "spaceMd",
        "spaceLg",
        "spaceXl",
        "spaceShellInline",
        "spaceShellBlock",
        "elevationPanel",
        "elevationOverlay",
        "borderWidthHairline",
        "borderWidthChrome",
        "interactionHoverBackground",
        "interactionActiveBackground",
        "interactionDisabledOpacity",
        "motionDurationFast",
        "motionDurationNormal",
    }
)
LIVE_EVENTS_HEARTBEAT_S = 15.0
LIVE_EVENTS_POLL_S = 1.0
LIVE_EVENTS_MAX_STREAMS = int(os.environ.get("MIMIR_LIVE_EVENTS_MAX_STREAMS", "8"))
# The scheduler dashboard needs older persisted state than the generic 5k event
# tail, but it must stay bounded: newly-added or monthly jobs may have no event
# yet, so "scan until every configured job is found" can otherwise become a
# full-file scan on every dashboard request.
SCHEDULER_STATE_EVENT_SCAN_RECORDS = 20_000


def _wiki_dir_for_home(home: Path | None) -> tuple[Path | None, web.Response | None]:
    if home is None:
        return None, json_error(
            "home_not_configured", "home path not configured", status=503
        )
    wiki_dir = home / "state" / "wiki"
    if not wiki_dir.is_dir():
        return None, json_error(
            "wiki_not_found", "wiki directory not found", status=404
        )
    return wiki_dir, None


def _wiki_health_flags(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "has_orphans": bool(payload.get("orphans")),
        "has_dangling_links": bool(payload.get("dangling_links")),
        "has_slug_collisions": bool(payload.get("slug_collisions")),
    }


def _build_wiki_index_payload(wiki_dir: Path) -> dict[str, Any]:
    payload = build_wiki_payload(wiki_dir)
    payload["health"] = _wiki_health_flags(payload)
    return payload


def _build_wiki_page_payload(wiki_dir: Path, slug: str) -> dict[str, Any] | None:
    """Return one wiki page detail payload, resolving by slug or wiki path."""
    graph = build_graph(wiki_dir)
    requested = slug.strip().strip("/")
    if requested.endswith(".md"):
        requested = requested[:-3]

    matches: list[tuple[str, dict[str, Any]]] = []
    for path_str, data in graph.pages.items():
        path_without_ext = path_str[:-3] if path_str.endswith(".md") else path_str
        if requested in {str(data.get("slug") or ""), path_str, path_without_ext}:
            matches.append((path_str, data))
    if not matches:
        return None

    path_str, data = sorted(matches, key=lambda item: item[0])[0]
    rel_path = Path(path_str)
    full_path = wiki_dir / rel_path
    try:
        stat = full_path.stat()
    except OSError:
        mtime = None
    else:
        from datetime import datetime, timezone

        mtime = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc,
        ).isoformat(timespec="seconds")
    try:
        markdown = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        markdown = ""

    collision_paths = {
        _posix(path)
        for paths in graph.collisions.values()
        for path in paths
    }
    dangling_links = [
        dict(item) for item in graph.dangling if item.get("source") == path_str
    ]
    backlinks = list(data.get("inbound") or [])
    outlinks = list(data.get("outbound") or [])
    return {
        "slug": str(data.get("slug") or rel_path.stem),
        "title": _title_from_markdown(markdown, str(data.get("slug") or rel_path.stem)),
        "category": _category_of(rel_path),
        "path": path_str,
        "mtime": mtime,
        "markdown": markdown,
        "backlinks": backlinks,
        "outlinks": outlinks,
        "dangling_links": dangling_links,
        "flags": {
            "is_orphan": path_str in graph.orphans,
            "has_dangling_links": bool(dangling_links),
            "has_slug_collision": path_str in collision_paths,
        },
    }


def read_web_ui_config(home: Path | None) -> dict[str, str]:
    """Agent-owned UI config: ``<home>/state/web_ui.json``.

    The agent edits this file itself (e.g. during onboarding) to set its display
    name and active skin — ``state/`` is an agent-writable root. Read per-request
    like the other ``<home>/`` sources; a missing file or malformed JSON falls
    back to defaults. The skin value is passed through verbatim — the frontend
    validates it against its registry.
    """
    agent_name = DEFAULT_AGENT_NAME
    skin = DEFAULT_WEB_SKIN
    if home is not None:
        try:
            raw = json.loads(
                (home.joinpath(*WEB_UI_CONFIG_RELPATH)).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            name = raw.get("agent_name")
            if isinstance(name, str) and name.strip():
                agent_name = name.strip()
            configured_skin = raw.get("skin")
            if isinstance(configured_skin, str) and configured_skin.strip():
                skin = configured_skin.strip()
    return {"agent_name": agent_name, "skin": skin}


def _validate_operator_skin_manifest(
    path: Path,
    raw: Any,
    seen_ids: set[str],
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        log.warning("skipping operator skin %s: manifest must be a json object", path)
        return None
    required = {
        "id",
        "name",
        "version",
        "tokens",
        "chrome",
        "panel",
        "characterRenderer",
    }
    missing = sorted(required - raw.keys())
    if missing:
        log.warning(
            "skipping operator skin %s: missing required fields %s", path, missing
        )
        return None
    skin_id = raw.get("id")
    if not isinstance(skin_id, str) or not skin_id.strip():
        log.warning("skipping operator skin %s: id must be a non-empty string", path)
        return None
    skin_id = skin_id.strip()
    if skin_id in BUILT_IN_WEB_SKIN_IDS:
        log.warning(
            "skipping operator skin %s: id %r collides with a built-in skin",
            path,
            skin_id,
        )
        return None
    if skin_id in seen_ids:
        log.warning("skipping operator skin %s: duplicate skin id %r", path, skin_id)
        return None
    for field in ("name", "version"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            log.warning(
                "skipping operator skin %s: %s must be a non-empty string",
                path,
                field,
            )
            return None
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict) or not tokens:
        log.warning("skipping operator skin %s: tokens must be a non-empty object", path)
        return None
    unknown_tokens = sorted(
        str(key) for key in tokens.keys() if key not in SKIN_TOKEN_NAMES
    )
    if unknown_tokens:
        log.warning(
            "skipping operator skin %s: unknown token keys %s", path, unknown_tokens
        )
        return None
    non_string_tokens = sorted(
        str(key)
        for key, value in tokens.items()
        if not isinstance(key, str) or not isinstance(value, str)
    )
    if non_string_tokens:
        log.warning("skipping operator skin %s: token values must be strings", path)
        return None
    for field in ("chrome", "panel", "characterRenderer"):
        if not isinstance(raw.get(field), dict):
            log.warning("skipping operator skin %s: %s must be an object", path, field)
            return None
    fonts = raw.get("fonts")
    if fonts is not None:
        if not isinstance(fonts, list):
            log.warning(
                "skipping operator skin %s: fonts must be an array when present", path
            )
            return None
        for index, font in enumerate(fonts):
            if not isinstance(font, dict):
                log.warning(
                    "skipping operator skin %s: fonts[%s] must be an object",
                    path,
                    index,
                )
                return None
            family = font.get("family")
            if not isinstance(family, str) or not family.strip():
                log.warning(
                    "skipping operator skin %s: fonts[%s].family must be a non-empty string",
                    path,
                    index,
                )
                return None
            src = font.get("src")
            if not isinstance(src, list) or not src:
                log.warning(
                    "skipping operator skin %s: fonts[%s].src must be a non-empty array",
                    path,
                    index,
                )
                return None
            for src_index, source in enumerate(src):
                if not isinstance(source, dict):
                    log.warning(
                        "skipping operator skin %s: fonts[%s].src[%s] must be an object",
                        path,
                        index,
                        src_index,
                    )
                    return None
                url = source.get("url")
                fmt = source.get("format")
                if not isinstance(url, str) or not url.strip():
                    log.warning(
                        "skipping operator skin %s: fonts[%s].src[%s].url must be a non-empty string",
                        path,
                        index,
                        src_index,
                    )
                    return None
                if fmt not in {"woff2", "woff", "truetype"}:
                    log.warning(
                        "skipping operator skin %s: fonts[%s].src[%s].format is unsupported",
                        path,
                        index,
                        src_index,
                    )
                    return None
            weight = font.get("weight")
            if weight is not None and not isinstance(weight, (int, str)):
                log.warning(
                    "skipping operator skin %s: fonts[%s].weight must be a string or integer",
                    path,
                    index,
                )
                return None
            for field in ("style", "display", "unicodeRange"):
                value = font.get(field)
                if value is not None and not isinstance(value, str):
                    log.warning(
                        "skipping operator skin %s: fonts[%s].%s must be a string",
                        path,
                        index,
                        field,
                    )
                    return None
    manifest = dict(raw)
    manifest["id"] = skin_id
    seen_ids.add(skin_id)
    return manifest


def read_operator_skin_manifests(home: Path | None) -> list[dict[str, Any]]:
    """Read ``<home>/skins/*.json`` manifests, skipping invalid files.

    Operator skins are deployment-owned config. A bad manifest should never
    prevent the built-in UI from loading, so validation is per-file and
    fail-open for missing/unreadable directories.
    """
    if home is None:
        return []
    skins_dir = home.joinpath(*OPERATOR_SKINS_RELPATH)
    try:
        paths = sorted(skins_dir.glob("*.json"))
    except OSError:
        log.warning(
            "could not read operator skins directory %s", skins_dir, exc_info=True
        )
        return []
    seen_ids: set[str] = set()
    manifests: list[dict[str, Any]] = []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning(
                "skipping operator skin %s: invalid json or unreadable file",
                path,
                exc_info=True,
            )
            continue
        manifest = _validate_operator_skin_manifest(path, raw, seen_ids)
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def available_web_skin_ids(home: Path | None) -> set[str]:
    return set(BUILT_IN_WEB_SKIN_IDS) | {
        str(manifest["id"]) for manifest in read_operator_skin_manifests(home)
    }


def read_turns_total(turns_log: Path) -> int:
    """Running turn total for the dossier = the newest turn record's ``seq``.

    TurnLogger keeps ``seq`` monotonic across retention trims, so the newest
    record carries the count. Falls back to a line count for records that
    predate ``seq`` (until TurnLogger backfills them on startup).
    """
    for record in tail_jsonl_records(turns_log):  # newest-first
        seq = record.get("seq")
        if isinstance(seq, int):
            return seq
        break
    try:
        return count_lines_chunked(turns_log)
    except OSError:
        return 0


def ensure_web_ui_config(home: Path | None) -> None:
    """Create ``<home>/state/web_ui.json`` with defaults if it doesn't exist yet.

    Run once at startup so the agent has a discoverable, well-formed file to edit
    (e.g. during onboarding). Existing files are never overwritten.
    """
    if home is None:
        return
    path = home.joinpath(*WEB_UI_CONFIG_RELPATH)
    if path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"agent_name": DEFAULT_AGENT_NAME, "skin": DEFAULT_WEB_SKIN}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        log.warning("could not seed %s", path, exc_info=True)


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


def _legacy_frontend_headers() -> dict[str, str]:
    return {
        "X-Mimir-Frontend": "legacy-html",
        "Link": "</app>; rel=\"alternate\"",
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
    return _read_jsonl_matching(path, max_records=max_records)


def _read_jsonl_matching(
    path: Path,
    *,
    max_records: int | None = 5000,
    include: Callable[[dict[str, Any]], bool] | None = None,
    stop_when: Callable[[list[dict[str, Any]]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Read tail JSONL records until ``max_records`` scanned or ``stop_when``.

    ``tail_jsonl_records`` yields decoded records newest-first but hides scan
    progress. This helper uses the underlying line tailer directly so callers can
    keep only relevant records and stop after finding a small set of persistent
    state events even when those events are older than the generic dashboard
    window. Output remains chronological.
    """
    if not path.is_file():
        return []
    out_newest_first: list[dict[str, Any]] = []
    scanned = 0
    try:
        for line in _tail_lines(path):
            scanned += 1
            if max_records is not None and scanned > max_records:
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if include is not None and not include(record):
                continue
            out_newest_first.append(record)
            if stop_when is not None and stop_when(out_newest_first):
                break
    except OSError:
        return []
    return list(reversed(out_newest_first))


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
            "prefs": {},
        }
    if identity is not None:
        return {
            "canonical": identity.canonical,
            "display_name": identity.display_name,
            "roles": list(identity.access.roles),
            "is_admin": identity.access.is_admin,
            "is_master": False,
            "prefs": dict(identity.prefs),
        }
    return {
        "canonical": None,
        "display_name": None,
        "roles": [],
        "is_admin": False,
        "is_master": False,
        "prefs": {},
    }


def _web_channel_for(canonical: str) -> str:
    return web_channel_for_identity(canonical)


def _normalize_web_channel(raw: str, *, default_web: bool = True) -> str:
    channel = raw.strip()
    if default_web and channel and channel != "*" and not channel.startswith("web-"):
        channel = "web-" + channel
    return channel


def _request_user_web_channel(request: web.Request) -> str | None:
    """Return the only channel a non-admin web identity may see, if scoped.

    Admin/master and dev-open requests return None, which means unrestricted.
    Per-user web keys are scoped to ``web-<canonical>`` so dashboard/log APIs do
    not rely on React filtering as the security boundary (chainlink #591).
    """
    if request.get("auth_is_admin") or request.get("auth_is_master"):
        return None
    identity = request.get("auth_identity")
    if identity is None:
        return None
    return _web_channel_for(str(getattr(identity, "canonical", "") or ""))


def _scoped_channel_from_query(
    request: web.Request,
) -> tuple[str | None, web.Response | None]:
    """Resolve an optional ``?channel=`` under per-user web RBAC.

    ``None`` means unrestricted for admins/dev-open mode. For non-admin web keys,
    omitted/default/self channel resolves to the caller's own channel; wildcard
    or another user's channel is a 403.
    """
    raw = request.query.get("channel")
    allowed = _request_user_web_channel(request)
    if allowed is None:
        if raw is None or not raw.strip():
            return None, None
        channel = _normalize_web_channel(raw, default_web=False)
        return (None if channel == "*" else channel), None

    if raw is None or not raw.strip():
        return allowed, None
    channel = _normalize_web_channel(raw)
    if channel == DEFAULT_WEB_CHANNEL:
        return allowed, None
    if channel == allowed:
        return allowed, None
    return None, json_error(
        "forbidden",
        "channel is not allowed for this web key",
        status=403,
        details={"channel": channel},
    )


def _record_matches_channel(record: dict[str, Any], channel: str | None) -> bool:
    if channel is None:
        return True
    return str(record.get("channel_id") or "") == channel


def _filter_records_by_channel(
    records: list[dict[str, Any]],
    channel: str | None,
) -> list[dict[str, Any]]:
    if channel is None:
        return records
    return [r for r in records if _record_matches_channel(r, channel)]


def _turns_tail_page(
    path: Path,
    *,
    limit: int,
    channel: str | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    out: list[dict[str, Any]] = []
    try:
        for record in tail_jsonl_records(path):
            if not isinstance(record, dict) or not _record_matches_channel(record, channel):
                continue
            out.append(record)
            if len(out) >= limit:
                break
    except OSError:
        return []
    out.reverse()
    return out


def _turns_after_page(
    path: Path,
    *,
    after: str,
    channel: str | None = None,
) -> list[dict[str, Any]]:
    if not after:
        return []
    newest_first: list[dict[str, Any]] = []
    try:
        for record in tail_jsonl_records(path):
            if not isinstance(record, dict) or not _record_matches_channel(record, channel):
                continue
            if record.get("turn_id") == after:
                newest_first.reverse()
                return newest_first
            newest_first.append(record)
    except OSError:
        return []
    return []


def _turns_before_page(
    path: Path,
    *,
    before: str,
    limit: int,
    channel: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    if not before or limit <= 0:
        return [], False
    out: list[dict[str, Any]] = []
    found = False
    has_more = False
    try:
        for record in tail_jsonl_records(path):
            if not isinstance(record, dict) or not _record_matches_channel(record, channel):
                continue
            if not found:
                found = record.get("turn_id") == before
                continue
            if len(out) < limit:
                out.append(record)
            else:
                has_more = True
                break
    except OSError:
        return [], False
    if not found:
        return [], False
    out.reverse()
    return out, has_more


def _event_record_matches_channel(record: dict[str, Any], channel: str) -> bool:
    for key in ("channel_id", "source_channel_id"):
        if str(record.get(key) or "") == channel:
            return True
    extra = record.get("extra")
    if isinstance(extra, dict):
        for key in ("channel_id", "source_channel_id"):
            if str(extra.get(key) or "") == channel:
                return True
    return False


def _filter_event_records_by_channel(
    records: list[dict[str, Any]],
    channel: str | None,
) -> list[dict[str, Any]]:
    if channel is None:
        return records
    return [r for r in records if _event_record_matches_channel(r, channel)]


def _live_event_item_channel(item: dict[str, Any]) -> str:
    event = item.get("event") if isinstance(item, dict) else None
    if isinstance(event, dict):
        return str(event.get("channel_id") or "")
    return ""


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
    turn_event_bus: Any | None = None,
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

    # Seed the agent-owned UI config on startup so there's a discoverable file
    # for the agent to edit (its name + skin).
    ensure_web_ui_config(home)

    existing = {(r.method, r.resource.canonical) for r in app.router.routes()}
    _react_app_dist = react_app_dist or (Path(__file__).parent / "react_app" / "dist")
    _dashboard_extensions = dashboard_extensions or first_party_dashboard_extensions()

    async def turns_page(_request: web.Request) -> web.Response:
        return web.Response(
            text=_load_viewer_html(),
            content_type="text/html",
            headers=_legacy_frontend_headers(),
        )

    def _turns_response(
        request: web.Request,
        *,
        channel: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        # Pagination params (progressive loading — the viewers must not pull
        # the whole — now hundreds-of-MB — turns.jsonl up front):
        #   ?after=<turn_id>           — turns strictly newer than turn_id (live poll)
        #   ?before=<turn_id>&limit=N  — up to N turns immediately OLDER than turn_id
        #                                (scroll-back page)
        #   ?limit=N                   — newest N turns (initial page)
        #   (none)                     — all retained turns (back-compat only)
        #
        # Keep cursor pages streaming tail-first. The previous React-v1 path
        # still called _read_jsonl(), which tail-decoded every retained record
        # before slicing to 200 rows; on live logs that meant reparsing the
        # whole turns.jsonl (~hundreds of MB) on every refresh.
        after = request.query.get("after", "")
        before = request.query.get("before", "")
        try:
            limit = int(request.query.get("limit") or 0)
        except ValueError:
            limit = 0

        if after:
            window = _turns_after_page(turns_log, after=after, channel=channel)
            cursor = str(window[-1].get("turn_id")) if window and window[-1].get("turn_id") else None
            return window, list_meta(cursor=cursor, limit=limit or None, total=None, truncated=False)

        if before:
            window, has_more = _turns_before_page(
                turns_log, before=before, limit=limit, channel=channel
            )
            cursor = str(window[0].get("turn_id")) if window and window[0].get("turn_id") else None
            return window, list_meta(
                cursor=cursor,
                limit=limit or None,
                total=None,
                truncated=has_more,
            )

        if limit > 0:
            window = _turns_tail_page(turns_log, limit=limit, channel=channel)
            cursor = str(window[-1].get("turn_id")) if window and window[-1].get("turn_id") else None
            return window, list_meta(
                cursor=cursor,
                limit=limit,
                total=None,
                truncated=len(window) >= limit,
            )

        # Back-compat endpoint behavior for ad-hoc callers that omit a limit.
        # This remains bounded by _read_jsonl's retained-record cap, but UI
        # callers use explicit cursor/limit params and avoid this path.
        records = _filter_records_by_channel(_read_jsonl(turns_log), channel)
        total = len(records)
        cursor = str(records[-1].get("turn_id")) if records and records[-1].get("turn_id") else None
        return records, list_meta(cursor=cursor, limit=None, total=total, truncated=False)

    async def turns_data(request: web.Request) -> web.Response:
        records, _meta = await asyncio.to_thread(
            _turns_response,
            request,
            channel=_request_user_web_channel(request),
        )
        return web.json_response({"turns": records})

    async def turns_data_v1(request: web.Request) -> web.Response:
        channel, error = _scoped_channel_from_query(request)
        if error is not None:
            return error
        turns, meta = await asyncio.to_thread(_turns_response, request, channel=channel)
        return json_success({"turns": turns}, meta=meta)

    async def sessions_data_v1(request: web.Request) -> web.Response:
        channel, error = _scoped_channel_from_query(request)
        if error is not None:
            return error
        try:
            limit = int(request.query.get("limit") or 200)
        except ValueError:
            limit = 200
        payload = await asyncio.to_thread(
            build_sessions_payload,
            turns_log=turns_log,
            chat_history=(home / "messages" / "chat_history.jsonl") if home is not None else None,
            saga_db=_saga_db,
            limit=limit,
            query=request.query.get("q", ""),
            channel=(
                channel
                if channel is not None
                else request.query.get("channel", "").strip() or None
            ),
            trigger=request.query.get("trigger", "").strip() or None,
            date_from=request.query.get("from", "").strip() or None,
            date_to=request.query.get("to", "").strip() or None,
        )
        total = int(payload.pop("total", 0) or 0)
        effective_limit = int(payload.pop("limit", limit) or limit)
        sessions = payload.get("sessions") or []
        cursor = str(sessions[-1].get("id")) if sessions and sessions[-1].get("id") else None
        return json_success(
            payload,
            meta=list_meta(
                cursor=cursor,
                limit=effective_limit,
                total=total,
                truncated=total > effective_limit,
            ),
        )

    def _parse_events_query(
        request: web.Request,
    ) -> tuple[str, set[str], int, str | None]:
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
        return since, type_filter, limit, _request_user_web_channel(request)

    def _events_window(
        *,
        since: str,
        type_filter: set[str],
        limit: int,
        channel: str | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        records = _filter_event_records_by_channel(_read_jsonl(events_log), channel)

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

    async def events_data(request: web.Request) -> web.Response:
        since, type_filter, limit, channel = _parse_events_query(request)
        out, _meta = await asyncio.to_thread(
            _events_window,
            since=since,
            type_filter=type_filter,
            limit=limit,
            channel=channel,
        )
        return web.json_response({"events": out})

    async def events_data_v1(request: web.Request) -> web.Response:
        since, type_filter, limit, channel = _parse_events_query(request)
        events, meta = await asyncio.to_thread(
            _events_window,
            since=since,
            type_filter=type_filter,
            limit=limit,
            channel=channel,
        )
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

    async def _live_event_items(
        request: web.Request,
        since: str | None,
        *,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            limit = int(request.query.get("limit") or 0)
        except ValueError:
            limit = 0
        items = await asyncio.to_thread(
            read_live_event_items_since,
            turns_log,
            since=since,
            # Scope before applying the client limit. Otherwise a non-admin
            # caller could receive an empty page whenever other channels have
            # newer events than their own.
            limit=None if channel is not None else limit or None,
        )
        out = [item.as_dict() for item in items]
        if channel is not None:
            out = [item for item in out if _live_event_item_channel(item) == channel]
            if limit > 0:
                out = out[-limit:]
        return out

    async def live_events_stream(request: web.Request) -> web.StreamResponse:
        """Fetch-authenticated SSE stream for React live dashboards.

        Reconnect/backfill contract:
        - each payload is ``{"id", "cursor", "ts", "event"}``;
        - clients persist the highest delivered cursor and reconnect with
          ``?since=<cursor>``;
        - backfill uses strict ``cursor > since`` comparison, so the last
          acknowledged event is not duplicated.
        """
        channel, error = _scoped_channel_from_query(request)
        if error is not None:
            return error
        once = request.query.get("once") in {"1", "true", "yes"}
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        if not await _try_acquire_live_event_slot():
            return web.Response(text="too many live event streams", status=429)

        delivered = request.query.get("since", "").strip() or None
        idle_for = 0.0
        try:
            await resp.prepare(request)
            while True:
                items = await _live_event_items(request, delivered, channel=channel)
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

    async def turn_events_stream(request: web.Request) -> web.StreamResponse:
        """Live SSE stream of in-turn events (chainlink #583 slice 1).

        Ephemeral — no backfill/cursor. ``?channel=web-foo`` subscribes to one
        channel; omitted subscribes to all. The dossier character consumes this
        so it animates DURING a turn, vs the post-hoc ``/api/v1/live-events``
        stream (derived from turns.jsonl at turn end) used for history.
        """
        if turn_event_bus is None:
            return web.json_response({"error": "turn events unavailable"}, status=503)
        channel, error = _scoped_channel_from_query(request)
        if error is not None:
            return error
        channel = channel if channel is not None else request.query.get("channel") or "*"
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        queue = turn_event_bus.subscribe(channel)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=LIVE_EVENTS_HEARTBEAT_S
                    )
                except asyncio.TimeoutError:
                    await resp.write(b": heartbeat\n\n")
                    continue
                block = (
                    "event: turn-event\n"
                    "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                )
                await resp.write(block.encode("utf-8"))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            turn_event_bus.unsubscribe(channel, queue)
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
        # The running model is the model part of the "provider:model" spec
        # (e.g. "codex_plus:gpt-5.5" -> "gpt-5.5"); fall back to the bare model.
        model_spec = str(getattr(config, "model_spec", "") or "")
        model = model_spec.split(":", 1)[1] if ":" in model_spec else (
            model_spec or str(getattr(config, "model", "") or "")
        )
        return json_success(
            {
                "version": __version__,
                "model": model,
                "turns_total": await asyncio.to_thread(read_turns_total, turns_log),
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
                "ui": read_web_ui_config(home),
                "skins": {
                    "built_in_ids": sorted(BUILT_IN_WEB_SKIN_IDS),
                    "operator": read_operator_skin_manifests(home),
                },
                "dashboard_extensions": _dashboard_extensions.navigation_payload(),
            },
            headers=_no_store_headers(),
        )

    # Resolve the DB path before sessions_data_v1 handles requests. It is
    # referenced by both the session browser and the SAGA dashboard handlers.
    _saga_db: Path | None = saga_db
    if _saga_db is None and home is not None:
        _saga_db = home / ".mimir" / "saga.db"

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
            text=render_dashboard_html(),
            content_type="text/html",
            headers=_legacy_frontend_headers(),
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
            scheduler = request.app.get("scheduler")
            expected_schedule_names = set()
            expected_poller_names = set()
            if scheduler is not None:
                scheduler_jobs = list(scheduler._scheduler.get_jobs())  # noqa: SLF001
                if scheduler_jobs:
                    expected_schedule_names = {
                        str(getattr((job.kwargs or {}).get("job"), "name", ""))
                        for job in scheduler_jobs
                        if job.id.startswith("scheduler:") and (job.kwargs or {}).get("job") is not None
                    }
                    expected_schedule_names.discard("")
                    expected_poller_names = set(scheduler._pollers)  # noqa: SLF001

            scheduler_event_types = {
                "scheduled_tick",
                "scheduled_tick_suppressed",
                "scheduled_tick_dropped",
                "scheduled_job_misfired",
            }
            poller_event_types = {
                "poller_complete",
                "poller_fire_suppressed",
                "poller_misfired",
                "poller_nonzero_exit",
                "poller_timeout",
                "poller_exec_error",
                "poller_enqueue_error",
                "poller_event_rejected",
                "poller_circuit_open",
                "poller_missing_required_env",
            }

            def _scheduler_event_name(record: dict[str, Any]) -> str:
                name = str(record.get("schedule_name") or record.get("job_id") or "")
                if name.startswith("scheduler:"):
                    name = name[len("scheduler:"):]
                return name

            def _poller_event_name(record: dict[str, Any]) -> str:
                name = str(record.get("poller") or record.get("job_id") or "")
                if name.startswith("poller:"):
                    name = name[len("poller:"):]
                return name

            def _is_scheduler_state_event(record: dict[str, Any]) -> bool:
                event_type = str(record.get("type") or "")
                if event_type in scheduler_event_types:
                    return _scheduler_event_name(record) in expected_schedule_names
                if event_type in poller_event_types:
                    return _poller_event_name(record) in expected_poller_names
                return False

            def _found_all_scheduler_state(records: list[dict[str, Any]]) -> bool:
                found_schedules = {
                    _scheduler_event_name(record)
                    for record in records
                    if str(record.get("type") or "") in scheduler_event_types
                }
                found_pollers = {
                    _poller_event_name(record)
                    for record in records
                    if str(record.get("type") or "") in poller_event_types
                }
                return (
                    found_schedules >= expected_schedule_names
                    and found_pollers >= expected_poller_names
                )

            return build_scheduler_dashboard_payload(
                scheduler=scheduler,
                commitments_store=commitments_store,
                events=_read_jsonl_matching(
                    events_log,
                    max_records=SCHEDULER_STATE_EVENT_SCAN_RECORDS,
                    include=_is_scheduler_state_event,
                    stop_when=_found_all_scheduler_state,
                ),
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

    # ── admin Users page: list + mint/rotate/revoke keys (#563) ───────
    # All under /api/v1/admin/ → admin-gated by the auth middleware. The list
    # NEVER returns key material; mint returns the raw key ONCE (out-of-band
    # hand-off), and after a mint/revoke the live resolver is reloaded so the
    # change takes effect for auth immediately.

    async def admin_users_v1(request: web.Request) -> web.Response:
        resolver = request.app.get("identity_resolver")
        if resolver is None:
            return json_error("unavailable", "identity resolver not configured", status=503)
        return json_success(build_users_payload(resolver), headers=_no_store_headers())

    async def admin_users_issue_key_v1(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return json_error("bad_request", "invalid json", status=400)
        canonical = str((body or {}).get("canonical") or "").strip()
        if not canonical:
            return json_error("bad_request", "canonical required", status=400)
        try:
            roles = roles_for_request((body or {}).get("role"))
        except ValueError as exc:
            return json_error("bad_request", str(exc), status=400)
        from .identities_populator import issue_web_key

        raw_key = await asyncio.to_thread(issue_web_key, home, canonical, roles=roles)
        resolver = request.app.get("identity_resolver")
        if resolver is not None:
            await asyncio.to_thread(resolver.reload)  # make the new key live now
        # The raw key is returned ONCE — never persisted, never re-fetchable.
        return json_success(
            {"canonical": canonical, "key": raw_key}, headers=_no_store_headers()
        )

    async def admin_users_revoke_key_v1(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return json_error("bad_request", "invalid json", status=400)
        canonical = str((body or {}).get("canonical") or "").strip()
        if not canonical:
            return json_error("bad_request", "canonical required", status=400)
        from .identities_populator import revoke_web_key

        revoked = await asyncio.to_thread(revoke_web_key, home, canonical)
        resolver = request.app.get("identity_resolver")
        if resolver is not None:
            await asyncio.to_thread(resolver.reload)
        return json_success(
            {"canonical": canonical, "revoked": revoked}, headers=_no_store_headers()
        )

    # ── /saga — saga DB viewer ───────────────────────────────────────

    # Resolve the DB path: use the explicit ``saga_db`` kwarg when
    # provided (server.py passes the saga.toml-resolved path); otherwise
    # derive from ``home``. The canonical location is
    # ``<home>/.mimir/saga.db`` (saga's default ``[storage].db_path``);
    # the older ``<home>/state/saga.db`` fallback predated the move to
    # ``.mimir/`` and pointed at a file that no longer exists.
    async def saga_page(_request: web.Request) -> web.Response:
        return web.Response(
            text=render_saga_html(),
            content_type="text/html",
            headers=_legacy_frontend_headers(),
        )

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

        if "error" in payload:
            return web.json_response(
                payload,
                status=404 if "not found" in str(payload["error"]) else 503,
            )
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

    async def user_prefs_v1(request: web.Request) -> web.Response:
        identity = request.get("auth_identity")
        if identity is None or request.get("auth_is_master"):
            return json_error(
                "identity_required",
                "per-user preferences require a user web key",
                status=403,
            )
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return json_error("bad_request", "invalid json", status=400)
        if not isinstance(body, dict):
            return json_error("bad_request", "json object required", status=400)
        prefs = body.get("prefs", body)
        if not isinstance(prefs, dict):
            return json_error("bad_request", "prefs must be an object", status=400)
        allowed: dict[str, Any] = {}
        if "skin" in prefs:
            skin = prefs.get("skin")
            if skin is not None and not isinstance(skin, str):
                return json_error("bad_request", "skin must be a string or null", status=400)
            if isinstance(skin, str) and skin.strip():
                skin = skin.strip()
                if skin not in await asyncio.to_thread(available_web_skin_ids, home):
                    return json_error(
                        "bad_request",
                        f"unknown skin '{skin}'",
                        status=400,
                    )
                allowed["skin"] = skin
            else:
                allowed["skin"] = None
        if not allowed:
            return json_error("bad_request", "no supported preferences supplied", status=400)
        from .identities_populator import set_user_prefs

        await asyncio.to_thread(set_user_prefs, home, identity.canonical, allowed)
        resolver = request.app.get("identity_resolver")
        if resolver is not None:
            await asyncio.to_thread(resolver.reload)
            identity = resolver.identity(identity.canonical) or identity
        return json_success(_whoami_payload(identity, False), headers=_no_store_headers())

    async def wiki_index_v1(_request: web.Request) -> web.Response:
        wiki_dir, error = _wiki_dir_for_home(home)
        if error is not None:
            return error
        assert wiki_dir is not None
        payload = await asyncio.to_thread(_build_wiki_index_payload, wiki_dir)
        return json_success(payload)

    async def wiki_page_v1(request: web.Request) -> web.Response:
        wiki_dir, error = _wiki_dir_for_home(home)
        if error is not None:
            return error
        assert wiki_dir is not None
        slug = request.match_info.get("slug", "").strip()
        if not slug:
            return json_error("missing_wiki_slug", "wiki slug required", status=400)
        payload = await asyncio.to_thread(_build_wiki_page_payload, wiki_dir, slug)
        if payload is None:
            return json_error("wiki_page_not_found", "wiki page not found", status=404)
        return json_success(payload)

    if ("GET", "/turns") not in existing:
        app.router.add_get("/turns", turns_page)
    if ("GET", "/api/turns") not in existing:
        app.router.add_get("/api/turns", turns_data)
    if ("GET", "/api/v1/turns") not in existing:
        app.router.add_get("/api/v1/turns", turns_data_v1)
    if ("GET", "/api/v1/sessions") not in existing:
        app.router.add_get("/api/v1/sessions", sessions_data_v1)
    if ("GET", "/api/events") not in existing:
        app.router.add_get("/api/events", events_data)
    if ("GET", "/api/v1/events") not in existing:
        app.router.add_get("/api/v1/events", events_data_v1)
    if ("GET", "/api/v1/live-events") not in existing:
        app.router.add_get("/api/v1/live-events", live_events_stream)
    if turn_event_bus is not None and ("GET", "/api/v1/turn-events") not in existing:
        app.router.add_get("/api/v1/turn-events", turn_events_stream)
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
    if ("POST", "/api/v1/user/prefs") not in existing:
        app.router.add_post("/api/v1/user/prefs", user_prefs_v1)
    if ("GET", "/api/v1/wiki") not in existing:
        app.router.add_get("/api/v1/wiki", wiki_index_v1)
    if ("GET", "/api/v1/wiki/{slug}") not in existing:
        app.router.add_get("/api/v1/wiki/{slug:.+}", wiki_page_v1)
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

    def admin_users_backend_routes() -> list[DashboardBackendRoute]:
        return [
            DashboardBackendRoute("GET", "/api/v1/admin/users", admin_users_v1),
            DashboardBackendRoute("POST", "/api/v1/admin/users/key", admin_users_issue_key_v1),
            DashboardBackendRoute("POST", "/api/v1/admin/users/revoke", admin_users_revoke_key_v1),
        ]

    add_backend_namespace_routes(
        app,
        registry=_dashboard_extensions,
        hooks={
            "ops": ops_backend_routes,
            "chainlink-board": chainlink_board_backend_routes,
            "scheduler": scheduler_backend_routes,
            "admin-config": admin_config_backend_routes,
            "admin-users": admin_users_backend_routes,
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
        return web.Response(
            text=render_memory_html(),
            content_type="text/html",
            headers=_legacy_frontend_headers(),
        )

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
