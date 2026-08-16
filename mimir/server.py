"""HTTP entrypoint + main loop.

Phase 4 surface:
  POST /event   — inject an AgentEvent
  GET  /health  — basic liveness

Wires together: dispatcher, agent, message buffer, index generator, search
indexer, SAGA client, session manager, scheduler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from .background_tasks import cancel_background_tasks, spawn_background
from .bridges.bench import BenchBridge
from .bridges.web_chat import WebChatBridge
from .chat_skills import strip_chat_skill_extra
from .channel_registry import ChannelRegistry
from .config import Config
from .dispatcher import Dispatcher
from .event_logger import init_logger, log_event, log_event_sync
from .http_ingress import strip_bridge_authority_extra
from .models import AgentEvent, make_process_session_id
from .access_control import builtin_trigger_service_principal, repo_binding_startup_alerts
from .rate_limits import RateLimitStore
from .scheduler import Scheduler
from .session_manager import ChannelSession
from .web_channels import web_channel_for_identity
from .skill_defs import (
    home_skills_dir,
    migrate_legacy_skills_dir,
    refresh_builtin_skills,
    seed_scheduler,
)
from .chainlink_bootstrap import ensure_chainlink_initialized
from .prompt_templates import seed_prompts
from .subagent_defs import seed_subagent_defs
from .worklink.continuation import (
    stamp_http_event_ingress_extra,
    strip_worklink_hint_extra,
)
from . import web_ui

log = logging.getLogger(__name__)

async def _start_mcp_servers(
    mcp_manager: Any,
    mcp_configs: list[Any],
    *,
    home: Path | None = None,
) -> tuple[Any | None, list[Any]]:
    """Wire configured MCP policy, tools, startup validation, and lifecycle."""
    from .mcp_client import (
        check_stale_policy_on_startup,
        get_tool_provenance,
        validate_mcp_policy,
    )

    try:
        mcp_tools = await mcp_manager.start_servers(mcp_configs)
    except Exception as exc:  # noqa: BLE001 — log + continue
        log.warning("MCP startup failed: %s", exc)
        mcp_tools = []
        try:
            await mcp_manager.shutdown()
        except Exception as shutdown_exc:  # noqa: BLE001
            log.warning("MCP shutdown after startup failure failed: %s", shutdown_exc)
            return mcp_manager, []
        return None, []
    for failure in getattr(mcp_manager, "startup_failures", []):
        await log_event("mcp_server_start_failed", **failure)
    if mcp_tools:
        await log_event(
            "mcp_servers_ready",
            count=len(mcp_tools),
            tool_names=[tool.name for tool in mcp_tools],
        )
    policy_issues = validate_mcp_policy(mcp_tools)
    for mcp_config in mcp_configs:
        if not mcp_config.policy_version:
            continue
        matching_tools = [
            tool for tool in mcp_tools
            if (
                (provenance := get_tool_provenance(tool)) is not None
                and provenance.server_config_id == mcp_config.server_config_id
            )
        ]
        policy_issues.extend(check_stale_policy_on_startup(
            matching_tools,
            mcp_config.policy_version,
        ))
    if policy_issues:
        await log_event(
            "mcp_policy_attention_required",
            count=len(policy_issues),
            issues=policy_issues,
        )
    return mcp_manager, mcp_tools


def _skill_auto_update_event(result: Any) -> tuple[str, dict[str, Any]] | None:
    """Return the startup event payload for optional-skill auto-refresh."""
    if not getattr(result, "any_updates", False):
        return None
    fields = {
        "updated": result.updated,
        "failed": result.failed,
        "pollers_json_updated": result.pollers_json_updated,
        "remaining_drift": result.remaining_drift,
    }
    event_kind = "skills_auto_update_failed" if result.failed else "skills_auto_update"
    return event_kind, fields


class _PairingNotifier:
    """Coalesced operator alerts plus DM-only fixed auto-replies."""

    def __init__(self, config: Config, channels: ChannelRegistry) -> None:
        self._config = config
        self._channels = channels
        self._operator_pending: list[dict[str, str]] = []
        self._operator_task: asyncio.Task[Any] | None = None
        self._operator_notified: set[str] = set()
        self._operator_cap_notified = False
        self._dm_reply_sent: set[str] = set()
        self._dm_reply_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._dm_reply_task: asyncio.Task[Any] | None = None

    async def aclose(self) -> None:
        tasks = {
            task
            for task in (self._operator_task, self._dm_reply_task)
            if task is not None
        }
        self._operator_task = None
        self._dm_reply_task = None
        self._operator_pending.clear()
        while True:
            try:
                self._dm_reply_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._dm_reply_queue.task_done()
        errors = await cancel_background_tasks(tasks, label="pairing notifier")
        if errors:
            raise ExceptionGroup(
                "pairing notifier cleanup failed",
                [_cleanup_exception(error) for error in errors],
            )

    async def notify_operator(
        self,
        *,
        canonical: str,
        display: str,
        platform: str,
        channel_id: str,
        delivery: str,
    ) -> None:
        canonical = canonical.strip()
        if not canonical or canonical in self._operator_notified:
            return
        alert_channel = (self._config.operator_alert_channel or "").strip()
        if not alert_channel:
            return
        self._operator_notified.add(canonical)
        self._operator_pending.append(
            {
                "canonical": canonical,
                "display": display.strip() or canonical,
                "platform": platform.strip() or "unknown",
                "channel_id": channel_id.strip(),
                "delivery": delivery,
            }
        )
        if self._operator_task is None or self._operator_task.done():
            self._operator_task = asyncio.create_task(self._flush_operator_later())

    async def flush_operator_alerts(self) -> None:
        if not self._operator_pending:
            return
        pending, self._operator_pending = self._operator_pending, []
        lines = ["Pairing approval needed:"]
        for item in pending:
            where = "DM" if item["delivery"] == "dm" else item["channel_id"]
            lines.append(
                "- "
                f"{item['canonical']} ({item['display']}; {item['platform']}; {where}) "
                f"- approve: mimir identities approve-pairing {item['canonical']}"
            )
        try:
            await self._channels.send(
                self._config.operator_alert_channel,
                "\n".join(lines),
                final=True,
            )
            await log_event(
                "pairing_operator_alert_sent",
                count=len(pending),
                channel_id=self._config.operator_alert_channel,
            )
        except Exception as exc:  # noqa: BLE001 — notification must not affect access
            log.debug("pairing operator alert send failed", exc_info=True)
            await log_event(
                "pairing_operator_alert_failed",
                channel_id=self._config.operator_alert_channel,
                error=str(exc)[:500],
            )

    async def notify_pending_cap_reached(
        self,
        *,
        platform: str,
        channel_id: str,
        delivery: str,
    ) -> None:
        if self._operator_cap_notified:
            return
        alert_channel = (self._config.operator_alert_channel or "").strip()
        if not alert_channel:
            return
        self._operator_cap_notified = True
        where = "DM" if delivery == "dm" else channel_id
        text = (
            "Pairing pending cap reached: new unknown contacts are being "
            f"dropped without pending entries (max={self._config.pairing_pending_max}). "
            f"Latest dropped contact came from {platform or 'unknown'} via {where}. "
            "Clear/approve pending pairings or raise MIMIR_PAIRING_PENDING_MAX."
        )
        try:
            await self._channels.send(alert_channel, text, final=True)
            await log_event(
                "pairing_pending_cap_alert_sent",
                channel_id=alert_channel,
                platform=platform,
                source_channel_id=channel_id,
                delivery=delivery,
                max_pending=self._config.pairing_pending_max,
            )
        except Exception as exc:  # noqa: BLE001 — notification must not affect access
            log.debug("pairing pending-cap alert send failed", exc_info=True)
            await log_event(
                "pairing_pending_cap_alert_failed",
                channel_id=alert_channel,
                platform=platform,
                source_channel_id=channel_id,
                delivery=delivery,
                error=str(exc)[:500],
            )

    async def _flush_operator_later(self) -> None:
        delay = max(
            0.0,
            float(self._config.pairing_operator_digest_delay_seconds or 0.0),
        )
        if delay:
            await asyncio.sleep(delay)
        await self.flush_operator_alerts()

    async def maybe_reply_dm(self, *, canonical: str, dm_channel_id: str) -> None:
        if not self._config.pairing_dm_auto_reply_enabled:
            return
        canonical = canonical.strip()
        dm_channel_id = dm_channel_id.strip()
        if not canonical or not dm_channel_id.startswith("dm-"):
            return
        if canonical in self._dm_reply_sent:
            return
        self._dm_reply_sent.add(canonical)
        await self._dm_reply_queue.put((canonical, dm_channel_id))
        if self._dm_reply_task is None or self._dm_reply_task.done():
            self._dm_reply_task = asyncio.create_task(self._dm_reply_worker())

    async def _dm_reply_worker(self) -> None:
        interval = max(
            0.0,
            float(self._config.pairing_dm_auto_reply_interval_seconds or 0.0),
        )
        while not self._dm_reply_queue.empty():
            canonical, dm_channel_id = await self._dm_reply_queue.get()
            try:
                await self._channels.send(
                    dm_channel_id,
                    self._config.pairing_dm_auto_reply_text,
                    final=True,
                )
                await log_event(
                    "pairing_dm_auto_reply_sent",
                    author=canonical,
                    channel_id=dm_channel_id,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort notification
                log.debug("pairing DM auto-reply failed", exc_info=True)
                await log_event(
                    "pairing_dm_auto_reply_failed",
                    author=canonical,
                    channel_id=dm_channel_id,
                    error=str(exc)[:500],
                )
            finally:
                self._dm_reply_queue.task_done()
            if interval and not self._dm_reply_queue.empty():
                await asyncio.sleep(interval)


@dataclass(slots=True)
class _StartupState:
    runtime_attempted: bool = False
    bundle: Any | None = None
    runtime_published: bool = False
    activity_panel: Any | None = None
    activity_panel_start_attempted: bool = False
    indexer_start_attempted: bool = False
    bridges_connect_attempted: bool = False
    mcp_start_attempted: bool = False
    mcp_manager: Any | None = None
    scheduler_start_attempted: bool = False
    scheduler_started: bool = False
    liveness_mark_attempted: bool = False
    compensated: bool = False


@dataclass(slots=True)
class _RuntimeSlot:
    bundle: Any | None = None


class _RuntimeFieldProxy:
    def __init__(self, slot: _RuntimeSlot, field: str) -> None:
        self._slot = slot
        self._field = field

    def __getattr__(self, name: str) -> Any:
        bundle = self._slot.bundle
        if bundle is None:
            raise RuntimeError("agent runtime is not initialized")
        return getattr(getattr(bundle, self._field), name)


_RUNTIME_APP_FIELDS = (
    "agent",
    "turn_logger",
    "message_buffer",
    "indexes",
    "indexer",
    "saga_client",
    "sessions",
    "subagent_inbox",
    "agent_runtime",
    "replayed_messages",
)


def _cleanup_exception(exc: BaseException) -> Exception:
    if isinstance(exc, Exception):
        return exc
    return RuntimeError(f"{type(exc).__name__}: {exc}")


def _cleanup_note(errors: list[Exception]) -> str:
    details = "; ".join(f"{type(error).__name__}: {error}" for error in errors)
    return f"server startup compensation had {len(errors)} cleanup failure(s): {details}"[:2000]


def _clear_runtime_app_state(
    app: web.Application,
    slot: _RuntimeSlot,
    state: _StartupState,
) -> None:
    slot.bundle = None
    for field in _RUNTIME_APP_FIELDS:
        app[field] = None
    if "activity_panel" in app:
        app["activity_panel"] = None
    if "mcp_manager" in app:
        app["mcp_manager"] = None
    state.bundle = None
    state.activity_panel = None
    state.mcp_manager = None
    state.runtime_published = False


def _publish_runtime(
    app: web.Application,
    slot: _RuntimeSlot,
    state: _StartupState,
    bundle: Any,
) -> None:
    slot.bundle = bundle
    app["turn_logger"] = bundle.turn_logger
    app["message_buffer"] = bundle.message_buffer
    app["indexes"] = bundle.indexes
    app["indexer"] = bundle.indexer
    app["saga_client"] = bundle.saga_client
    app["sessions"] = bundle.sessions
    app["subagent_inbox"] = bundle.subagent_inbox
    app["replayed_messages"] = bundle.replayed_messages
    app["agent_runtime"] = bundle
    app["agent"] = bundle.agent
    state.bundle = bundle
    state.runtime_published = True


def _session_synthesis_event(session: ChannelSession) -> AgentEvent:
    """Build the server-owned session synthesis event."""
    authority = builtin_trigger_service_principal("session-boundary", Path("."))
    return AgentEvent(
        trigger="saga_session_end",
        channel_id=session.channel_id,
        service_principal="synthesis",
        service_authority=authority,
        content="",
        extra={"saga_session_id": session.saga_session_id},
        source_session_acl=session.source_acl,
        ifc_labels=session.ifc_state.current(),
    )


async def _handle_event(request: web.Request) -> web.Response:
    # Auth: gated at the app-level middleware. See ``_make_auth_middleware``.
    try:
        body: dict[str, Any] = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    channel_id = body.get("channel_id")
    if not channel_id:
        return web.json_response({"error": "channel_id required"}, status=400)
    if not isinstance(channel_id, str):
        return web.json_response({"error": "channel_id must be a string"}, status=400)

    # #487: type-check structured fields, don't coerce. A truthy non-dict
    # ``extra`` (or non-list ``attachment_names``) survives ``or {}``/``or []``
    # and later ``event.extra.get(...)`` raises AttributeError → an unguarded
    # 500 in enqueue or a silently-dropped turn on the worker path. Reachable by
    # any client when MIMIR_API_KEY is unset. Reject with 400 instead.
    extra = body.get("extra")
    if extra is not None and not isinstance(extra, dict):
        return web.json_response({"error": "extra must be an object"}, status=400)
    # chainlink #783 / #740 / #926 / #928 (security): HTTP ingress is
    # client-controlled, so strip server-owned chat-skill keys, Worklink
    # continuation hints, and bridge-owned authority metadata before constructing
    # the AgentEvent. Otherwise a client could forge privileged metadata or
    # declassify its synthesized durable outputs as public.
    extra = strip_bridge_authority_extra(
        strip_worklink_hint_extra(strip_chat_skill_extra(extra))
    )
    extra = stamp_http_event_ingress_extra(extra)
    attachment_names = body.get("attachment_names")
    if attachment_names is not None and not isinstance(attachment_names, list):
        return web.json_response(
            {"error": "attachment_names must be an array"}, status=400,
        )

    # Per-user HTTP attribution is server-owned. The request body is not an
    # identity assertion and must not select another user's roles.
    identity = request.get("auth_identity")
    if identity is not None:
        author = identity.canonical
        author_display = identity.display_name
        author_id = identity.canonical
        # Security: a per-user web key may target ONLY its own channel. The
        # request-body channel_id is spoofable (the modern /chat path binds it
        # to the authenticated identity for exactly this reason — web_chat.py).
        # Without this bind, one authorized non-admin key could POST /event with
        # another user's channel_id, run a turn on that channel, and pull that
        # user's private history into context (or steer its egress). The master
        # key (identity is None, below) and admins may target any channel for
        # automation/operator use.
        if not request.get("auth_is_admin", False):
            own_channel = web_channel_for_identity(identity.canonical)
            if channel_id != own_channel:
                return web.json_response(
                    {"error": "channel_id not permitted for this identity"},
                    status=403,
                )
    else:
        # Preserve master-key and dev/open automation compatibility. Neither
        # path has a per-user identity to bind here.
        author = body.get("author")
        author_display = body.get("author_display")
        author_id = body.get("author_id")

    event = AgentEvent(
        trigger=body.get("trigger", "user_message"),
        channel_id=channel_id,
        content=body.get("content", ""),
        author=author,
        author_display=author_display,
        author_id=author_id,
        source_id=body.get("source_id"),
        source=body.get("source"),
        attachment_names=attachment_names or [],
        extra=extra or {},
    )

    dispatcher: Dispatcher = request.app["dispatcher"]
    accepted = await dispatcher.enqueue(event)
    if not accepted:
        return web.json_response(
            {"error": "queue_full_or_closed", "channel_id": channel_id},
            status=503,
        )
    return web.json_response({"ok": True, "channel_id": channel_id})


def _safe_str_eq(a: str, b: str) -> bool:
    """Constant-time string compare. Avoids leaking key length/prefix
    via response-time differences."""
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# (method, path) tuples exempt from the auth middleware. HTML page
# shells and the shared browser auth/bootstrap script are public-shaped:
# they carry no operator data or secrets, and browser code sends the key
# in ``X-API-Key`` for protected JSON/stream routes. The data behind these
# surfaces is auth-required — /turns and /ops serve only static-shaped HTML;
# their data comes from /api/turns, /api/events, /api/ops which DO require auth.
#
# Method-keyed (PR #104 review fix): if a future ``POST /turns`` is
# ever added (e.g. for a server-side form), it inherits NO exemption.
#
# ``GET /`` is exempt too: it's a bare convenience redirect to /app
# (``_handle_root``) that carries no data of its own — and its target is
# itself an exempt HTML shell whose data APIs require auth.
_AUTH_EXEMPT: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/app"),
    ("GET", "/app/auth.js"),
    ("GET", "/api/web/bootstrap"),
    ("GET", "/api/v1/web/bootstrap"),
    ("GET", "/turns"),
    ("GET", "/ops"),
    ("GET", "/saga"),
    ("GET", "/state"),
})

_AUTH_EXEMPT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("GET", "/app/"),
)


def _is_auth_exempt(method: str, path: str) -> bool:
    if (method, path) in _AUTH_EXEMPT:
        return True
    return any(
        method == prefix_method and path.startswith(prefix)
        for prefix_method, prefix in _AUTH_EXEMPT_PREFIXES
    )


# Route prefixes that require the ``admin`` role (server-side RBAC boundary,
# github #726). Any method on these paths is admin-only. The admin config/user
# management endpoints live under ``/api/v1/admin``; the ops/scheduler/task
# dashboards expose global operational/project state and Worklink artifacts
# (chainlink #593). SAGA and file-backed memory/state dashboards expose global
# cross-channel history and raw markdown content (chainlink #592); wiki viewer
# APIs expose global markdown state and graph health (chainlink #690). The
# factory-runs dashboard exposes global Worklink factory artifacts — run.json,
# prompts, transcripts, PR URLs — across all runs (not per-user scoped). This
# is the SECURITY gate; React section-hiding (a manifest ``requires_role``) is
# UX only and must never be the sole control.
_ADMIN_REQUIRED_PREFIXES: tuple[str, ...] = (
    "/api/v1/admin",
    "/api/ops",
    "/api/v1/ops",
    "/api/v1/scheduler",
    "/api/v1/chainlink-board",
    "/api/v1/factory-runs",
    "/api/v1/saga",
    "/api/v1/memory",
    "/api/v1/wiki",
    "/api/saga",
    "/api/memory",
)


def _matches_admin_required_prefix(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(f"{prefix}/")


def _is_admin_required(path: str) -> bool:
    return any(
        _matches_admin_required_prefix(path, prefix)
        for prefix in _ADMIN_REQUIRED_PREFIXES
    )


_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_UNAUTHENTICATED_WARNING = (
    "MIMIR_API_KEY is not set — local clients can use POST /event and "
    "POST /chat without authentication. Loopback binding limits network "
    "exposure but is not an authentication boundary; browser cross-site "
    "writes and DNS-rebinding Host headers are rejected, while local "
    "processes can still inject messages or trigger saga_end_session. "
    "Set MIMIR_API_KEY before exposing to a network. "
    "For development on localhost, set MIMIR_ALLOW_UNAUTHENTICATED=true "
    "to suppress this warning."
)


def _request_authority(request: web.Request) -> tuple[str, int | None] | None:
    """Return a normalized Host header, or None when it is malformed."""
    host_header = request.headers.get("Host", "")
    try:
        parsed = urlsplit(f"//{host_header}")
        port = parsed.port
    except ValueError:
        return None
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        return None
    return parsed.hostname.rstrip(".").lower(), port


def _origin_matches_request(request: web.Request, origin: str) -> bool:
    """Return whether a serialized browser Origin matches this request."""
    authority = _request_authority(request)
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return False
    if (
        authority is None
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False

    origin_port = port or (443 if parsed.scheme == "https" else 80)
    request_port = authority[1] or (443 if request.scheme == "https" else 80)
    return (
        parsed.scheme == request.scheme
        and parsed.hostname.rstrip(".").lower() == authority[0]
        and origin_port == request_port
    )


def _make_auth_middleware(expected_key: str, web_host: str | None = None):
    """Build an aiohttp middleware that gates every non-exempt route on
    a matching ``X-API-Key`` header.

    Browser-origin and loopback Host validation always run before the API-key
    gate. Empty ``expected_key`` (``MIMIR_API_KEY`` unset) disables only that
    gate; any non-empty key activates it.

    Why middleware (vs per-handler checks):

    - The original code only gated ``POST /event``. Every other route —
      ``/api/turns``, ``/api/events``, ``/api/ops``, ``/chat`` — was
      open. Centralizing the gate here means new routes inherit
      protection by default; opting OUT requires adding the path to
      the exempt set, which is operator-visible.
    - One source of truth for the safe-eq compare and the 401 response
      shape. Per-handler implementations had drifted (``/event``
      returned a JSON ``error`` body; the others would return whatever
      ad hoc shape the next author picked).
    """
    async def _auth_middleware(request: web.Request, handler):
        authority = _request_authority(request)
        if web_host in _LOOPBACK_HOSTS and (
            authority is None or authority[0] not in _LOOPBACK_HOSTS
        ):
            return web.json_response({"error": "invalid_host"}, status=403)

        if request.method in _STATE_CHANGING_METHODS:
            fetch_site = request.headers.get("Sec-Fetch-Site", "").lower()
            origin = request.headers.get("Origin")
            if fetch_site == "cross-site" or (
                origin is not None and not _origin_matches_request(request, origin)
            ):
                return web.json_response({"error": "cross_site_request"}, status=403)

        if _is_auth_exempt(request.method, request.path):
            return await handler(request)

        # Per-user resolution reads the live resolver from the app (constructed
        # after this middleware; populated by request time). github #726.
        resolver = request.app.get("identity_resolver")

        # The gate activates when EITHER a master key is set OR per-user web
        # keys exist — so configuring users can't leave the server open even if
        # MIMIR_API_KEY is unset. Neither → legacy dev/open path (no identity,
        # no RBAC), preserving localhost behavior. Shared with /web/bootstrap
        # (web_ui.web_gate_active) so the browser's reported auth state can't
        # drift from what's enforced here (#770 review).
        if not web_ui.web_gate_active(expected_key, resolver):
            return await handler(request)

        provided = request.headers.get("X-API-Key", "")
        identity = None
        is_master = False
        if expected_key and provided and _safe_str_eq(provided, expected_key):
            # Admin master key (MIMIR_API_KEY): admin for admin/automation
            # routes, but NOT a chat/user identity (enforced per-route).
            is_master = True
        elif resolver is not None and provided:
            identity = resolver.resolve_web_key(provided)

        authorized = is_master or (
            identity is not None and identity.access.is_authorized
        )
        if not authorized:
            return web.json_response({"error": "unauthorized"}, status=401)

        is_admin = is_master or (identity is not None and identity.access.is_admin)
        # Attach the resolved identity for downstream handlers (web-chat
        # attribution, /whoami). ``auth_identity`` is None for the master key.
        request["auth_identity"] = identity
        request["auth_is_master"] = is_master
        request["auth_is_admin"] = is_admin

        if _is_admin_required(request.path) and not is_admin:
            return web.json_response(
                {"error": "forbidden", "detail": "admin role required"}, status=403,
            )
        return await handler(request)

    return web.middleware(_auth_middleware)


# Regex for the access-log filter — ``?api_key=...`` or ``&api_key=...``
# in URL query strings. Replaces the value with ``REDACTED`` so the
# server does not preserve stale URL-carried secrets in stdout / log files.
_API_KEY_QUERY_RE = re.compile(
    r"([?&]api_key=)[^\s&]+",
    flags=re.IGNORECASE,
)


class _MaskApiKeyInAccessLog(logging.Filter):
    """Logging filter for ``aiohttp.access`` that masks ``api_key=``
    query values in formatted records. URL API keys are no longer accepted
    for auth, but the filter remains as defense-in-depth for stale clients,
    bookmarks, and access logs. PR #104 review note (mimir-carreira)."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Both the raw msg and the formatted message can carry the
        # query string depending on aiohttp version + format string.
        if isinstance(record.msg, str):
            record.msg = _API_KEY_QUERY_RE.sub(r"\1REDACTED", record.msg)
        if record.args:
            record.args = tuple(
                _API_KEY_QUERY_RE.sub(r"\1REDACTED", a)
                if isinstance(a, str) else a
                for a in record.args
            )
        return True


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _handle_root(request: web.Request) -> web.Response:
    """Redirect the bare web root to the React frontend.

    The root has no content of its own; ``/app`` is the default operator
    landing page. 302 (Found), not 301 — so we can repoint this or add a real
    landing page later without fighting browsers that cached a permanent
    redirect. Auth-exempt (see ``_AUTH_EXEMPT``): it leaks nothing."""
    raise web.HTTPFound("/app")


# chainlink #233: bound for caller-supplied max_clusters on the
# /api/memory/consolidate endpoint. Each cluster fans out to one LLM
# call in the thematic pass — 100 is high enough for any legitimate
# bench/operator run, low enough to keep a misconfigured caller from
# burning the budget in one shot.
_CONSOLIDATE_MAX_CLUSTERS_CEILING = 100


class _ConsolidateGuard:
    """Single-flight guard for ``POST /api/memory/consolidate``.

    Carried on ``app["consolidate_guard"]`` so the inflight bit can be
    mutated post-startup without tripping aiohttp's "changing state of
    started application is deprecated" warning.
    """

    def __init__(self) -> None:
        self.inflight = False


async def _handle_consolidate(request: web.Request) -> web.Response:
    # Bench surface: trigger one SagaStore.consolidate() pass on demand.
    # Replaces the legacy MSAM-sidecar /v1/consolidate at port 3002.
    #
    # chainlink #233: consolidate is the most expensive saga operation
    # (LLM fan-out per cluster). A single inflight-guard prevents a
    # legitimate API-keyed caller — or a runaway retry loop — from
    # firing N parallel passes and burning the budget before
    # ``cost-runaway`` ntfy fires. Also plumbs ``max_clusters`` and
    # ``extra_canonical_subjects`` from the request body (previously
    # silently dropped) with a 100-cluster ceiling on ``max_clusters``.
    saga_client: SagaClient = request.app["saga_client"]
    guard: _ConsolidateGuard = request.app["consolidate_guard"]
    try:
        body: dict[str, Any] = await request.json()
    except json.JSONDecodeError:
        body = {}

    raw_max = body.get("max_clusters")
    max_clusters: int | None
    if raw_max is None:
        max_clusters = None
    else:
        if isinstance(raw_max, bool) or not isinstance(raw_max, int):
            return web.json_response(
                {"error": "max_clusters must be a positive integer"},
                status=400,
            )
        if raw_max < 1 or raw_max > _CONSOLIDATE_MAX_CLUSTERS_CEILING:
            return web.json_response(
                {
                    "error": (
                        f"max_clusters must be between 1 and "
                        f"{_CONSOLIDATE_MAX_CLUSTERS_CEILING}"
                    )
                },
                status=400,
            )
        max_clusters = raw_max

    raw_subjects = body.get("extra_canonical_subjects")
    extra_canonical_subjects: list[str] | None
    if raw_subjects is None:
        extra_canonical_subjects = None
    elif isinstance(raw_subjects, list) and all(
        isinstance(s, str) for s in raw_subjects
    ):
        extra_canonical_subjects = raw_subjects
    else:
        return web.json_response(
            {"error": "extra_canonical_subjects must be a list of strings"},
            status=400,
        )

    if guard.inflight:
        return web.json_response(
            {"error": "consolidate already running"},
            status=429,
        )
    guard.inflight = True
    try:
        result = await saga_client.consolidate(
            dry_run=bool(body.get("dry_run", False)),
            max_clusters=max_clusters,
            extra_canonical_subjects=extra_canonical_subjects,
        )
    except Exception as exc:
        log.exception("consolidate failed: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        guard.inflight = False
    return web.json_response(result or {})


def reattach_inflight_worklink_runs(
    home: Path,
    *,
    popen: Any = None,
) -> list[int]:
    """Startup reconcile (#561): resume Worklink runs orphaned by a restart.

    Dead local workers are reaped and reported. Persistent worker records from
    older deployments retain the detached ``mimir worklink run <id> --reattach``
    recovery path.

    Gated on ``WORKLINK_REPO`` (the same env the ready-queue poller needs); no-op
    on non-Worklink homes. Best-effort and non-blocking: each resume runs
    detached so a long worker wait never delays startup; a spawn failure for one
    leaf is logged and the rest still proceed."""
    import shlex
    import subprocess

    from .event_logger import log_event_sync
    from .worklink.control import reconcile_run_states
    from .worklink.run_state import reattach_dispatch_argv

    spawn = popen or subprocess.Popen
    # Local workers cannot survive a restart. Reap their records first, even on
    # homes without a configured reattach repository; malformed records emit an
    # event and never abort startup.
    states = reconcile_run_states(home, event_logger=log_event_sync)
    repo = os.environ.get("WORKLINK_REPO")
    if not repo:
        return []
    if not states:
        return []
    run_bin = shlex.split(os.environ.get("WORKLINK_RUN_BIN") or "mimir")
    state_dir = home / "state" / "worklink" / "runs"
    dispatched: list[int] = []
    for state in states:
        if state.compute_name == "local_subprocess":
            # A live local worker already has its original controller; unlike a
            # persistent remote substrate it cannot be reattached by a new one.
            continue
        argv = reattach_dispatch_argv(run_bin, home, repo, state.issue_id)
        log_path = state_dir / f"reattach-{state.issue_id}.log"
        try:
            log_fh: Any = log_path.open("ab")
        except OSError:
            log_fh = subprocess.DEVNULL
        try:
            spawn(
                argv,
                cwd=repo,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,  # detach: survive this startup + outlive it
            )
        except (OSError, subprocess.SubprocessError):
            continue
        finally:
            if log_fh not in (subprocess.DEVNULL, None):
                try:
                    log_fh.close()
                except OSError:
                    pass
        dispatched.append(state.issue_id)
    return dispatched


def build_app(config: Config) -> web.Application:
    # Preserve the early dependency preflight: explicit coding opt-in still
    # fails loudly before broader server construction when the runtime is absent.
    from .tools import all_mimir_tools
    requested_coding = getattr(config, "coding_enabled", False)
    all_mimir_tools(coding_enabled=requested_coding)

    process_session_id = make_process_session_id()

    config.logs_dir.mkdir(parents=True, exist_ok=True)
    init_logger(
        config.events_log,
        process_session_id,
        max_events=config.max_events_kept,
        agent_id=config.agent_id,
    )

    worklink_agent_id = f"mimir-worklink:{process_session_id}"
    # Detached controllers inherit this process generation. The Chainlink
    # tracker identity stays static; structured claim ownership does not.
    os.environ["MIMIR_WORKLINK_AGENT_ID"] = worklink_agent_id

    # 10MB body cap (aiohttp default is 1MB). Mimir takes JSON-only bodies on
    # /event and /chat — long bluesky transcripts and seed payloads can run
    # well past 1MB. Bridges read attachment bytes from disk via filesystem
    # paths (``attachment_names``), not from the request body, so the cap
    # doesn't need to accommodate binary uploads.
    #
    # Request middleware always rejects cross-site writes and DNS-rebinding
    # Host headers on loopback binds. It additionally gates every non-exempt
    # route on ``X-API-Key`` when ``MIMIR_API_KEY`` is set.
    app = web.Application(
        client_max_size=10 * 1024 * 1024,
        middlewares=[
            _make_auth_middleware(config.api_key or "", web_host=config.web_host)
        ],
    )

    if not config.api_key:
        if getattr(config, "allow_unauthenticated", False):
            log.debug("unauthenticated mode acknowledged: %s", _UNAUTHENTICATED_WARNING)
        else:
            log.warning(_UNAUTHENTICATED_WARNING)

    # Access-log filter: mask stale ``?api_key=`` query values so accidental
    # URL secrets do not land in stdout / log files. Idempotent — multiple
    # calls don't stack the filter because aiohttp.access is a singleton logger.
    _access_log = logging.getLogger("aiohttp.access")
    if not any(isinstance(f, _MaskApiKeyInAccessLog) for f in _access_log.filters):
        _access_log.addFilter(_MaskApiKeyInAccessLog())

    config.logs_dir.mkdir(parents=True, exist_ok=True)
    (config.home / "memory" / "core").mkdir(parents=True, exist_ok=True)
    (config.home / "memory" / "channels").mkdir(parents=True, exist_ok=True)
    (config.home / "memory" / "shared").mkdir(parents=True, exist_ok=True)
    (config.home / "state").mkdir(parents=True, exist_ok=True)
    (config.home / "messages").mkdir(parents=True, exist_ok=True)
    (config.home / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
    seeded = seed_subagent_defs(config.home)
    # One-shot migration: existing deployments with skills under
    # ``<home>/.claude/skills/`` get their content moved to the
    # ``<home>/skills/`` operator location. Idempotent — no-op once
    # done, since the source dir is gone after the first run.
    migrate_legacy_skills_dir(config.home)
    # Refresh bundled skills into ``<home>/.mimir_builtin_skills/``.
    # Unconditional overwrite — the bundle is read-only by convention,
    # always matches mimir source.
    seeded_skills_map = refresh_builtin_skills(config.home)
    # Seed default operator prompts (heartbeat.md, reflect.md) and the
    # default scheduler.yaml if missing. Idempotent — only writes when
    # the target is absent, so operator customizations persist. Existing-file
    # default updates are handled by the startup defaults-upgrade proposal path.
    seed_prompts(config.home)
    seed_scheduler(config.home)
    # Refresh reference docs on upgrade: rewrite docs still present in
    # <home>/docs/ to the shipped version, seed docs new in this release, and
    # leave operator-deleted ones deleted. No-ops unless the version changed
    # (mimir/doc_seed.py). Also seeds docs into a pre-existing home that never
    # had them (e.g. created before this feature).
    _doc_changes: dict[str, str] = {}
    try:
        from .doc_seed import refresh_docs
        _doc_changes = refresh_docs(config.home)
        if _doc_changes:
            log.info("docs refreshed on upgrade: %s", _doc_changes)
    except Exception as exc:  # never block startup on doc seeding
        log.warning("doc refresh skipped: %s", exc)
    # Initialize the Chainlink store if absent (+ the CLI is installed) so the
    # Tasks board works out of the box instead of reporting "unavailable".
    # Best-effort and gated on the binary, so plain pip installs are unaffected.
    ensure_chainlink_initialized(config.home)

    from .runtime import create_core_services

    core = create_core_services(config)
    identity_resolver = core.identity_resolver
    aliases_loaded = core.aliases_loaded
    _db_path = core.saga_db_path

    # Channel layer (SPEC §7.2). BenchBridge always registers — it's how the
    # benchmark adapter gets outbound. WebChatBridge registers if a
    # web_chat-friendly aiohttp app is hosting us; routes mount below in
    # _on_startup. Discord / Slack / Bluesky bridges register based on env
    # tokens (DISCORD_TOKEN etc.).
    channels = ChannelRegistry()
    channels.register(BenchBridge(home=config.home))
    pairing_notifier = _PairingNotifier(config, channels)

    dispatcher = Dispatcher(config, resolver=identity_resolver)
    scheduler = Scheduler(
        scheduler_yaml=config.home / "scheduler.yaml",
        enqueue=dispatcher.enqueue,
        home=config.home,
        scheduler_tz=config.scheduler_tz,
    )
    # WebChatBridge needs the dispatcher (for inbound) — built after dispatcher
    # exists, registered before channels.connect_all() runs at startup.
    web_chat = WebChatBridge(
        enqueue=dispatcher.enqueue,
        home=config.home,
        chat_skill_registry=core.chat_skill_registry,
    )
    web_chat.max_subscribers = config.chat_stream_max_subscribers
    channels.register(web_chat)

    # Inbound attachments land here; the agent reads files by path. The
    # outbound counterpart (<send-file path="..."> directives) resolves
    # paths under attachments/outbound/ — created lazily on first use.
    attachments_inbound = config.home / "attachments" / "inbound"

    # DiscordBridge — opt-in via DISCORD_TOKEN. Import is deferred so absent
    # discord-py doesn't crash deployments that don't use Discord.
    if config.discord_token:
        try:
            from .bridges.discord import DiscordBridge

            channels.register(
                DiscordBridge(
                    token=config.discord_token,
                    enqueue=dispatcher.enqueue,
                    attachments_dir=attachments_inbound,
                    attachments_max_bytes=config.attachments_max_bytes,
                )
            )
        except ImportError as exc:
            log.warning(
                "DISCORD_TOKEN set but discord-py not installed (%s); "
                "skipping DiscordBridge. Install with `pip install mimir[discord]`.",
                exc,
            )

    # SlackBridge — opt-in via SLACK_BOT_TOKEN + SLACK_APP_TOKEN. Both required
    # because we use Socket Mode (no public webhook needed). Same deferred-
    # import pattern as Discord.
    if config.slack_bot_token and config.slack_app_token:
        try:
            from .bridges.slack import SlackBridge

            channels.register(
                SlackBridge(
                    bot_token=config.slack_bot_token,
                    app_token=config.slack_app_token,
                    enqueue=dispatcher.enqueue,
                    attachments_dir=attachments_inbound,
                    attachments_max_bytes=config.attachments_max_bytes,
                )
            )
        except ImportError as exc:
            log.warning(
                "SLACK_BOT_TOKEN/SLACK_APP_TOKEN set but slack-bolt not installed (%s); "
                "skipping SlackBridge. Install with `pip install mimir[slack]`.",
                exc,
            )
    elif config.slack_bot_token or config.slack_app_token:
        log.warning(
            "Slack tokens partially configured (bot=%s, app=%s) — both required for "
            "Socket Mode. Skipping SlackBridge.",
            bool(config.slack_bot_token),
            bool(config.slack_app_token),
        )

    startup_background_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_runtime_task(
        coroutine: Any,
        name: str,
    ) -> asyncio.Task[Any]:
        return spawn_background(
            startup_background_tasks,
            coroutine,
            name=name,
        )

    from .runtime import RuntimeAdapters, create_agent_runtime

    runtime_adapters = RuntimeAdapters(
        dispatcher=dispatcher,
        scheduler=scheduler,
        channels=channels,
        pairing_notifier=pairing_notifier,
        spawn_background_task=_spawn_runtime_task,
    )
    runtime_slot = _RuntimeSlot()
    startup_state = _StartupState()

    app["config"] = config
    app["dispatcher"] = dispatcher
    app["scheduler"] = scheduler
    app["channels"] = channels
    app["pairing_notifier"] = pairing_notifier
    app["identity_resolver"] = identity_resolver
    app["aliases_loaded"] = aliases_loaded
    app["seeded_subagents"] = seeded
    app["seeded_skills"] = seeded_skills_map
    app["api_key"] = config.api_key
    app["runtime_slot"] = runtime_slot
    app["runtime_adapters"] = runtime_adapters
    app["startup_background_tasks"] = startup_background_tasks
    app["startup_state"] = startup_state
    for field in _RUNTIME_APP_FIELDS:
        app[field] = None
    # chainlink #233: single-flight guard for POST /api/memory/consolidate.
    app["consolidate_guard"] = _ConsolidateGuard()

    if not config.api_key:
        log.warning(
            "MIMIR_API_KEY is unset — every route accepts unauthenticated "
            "requests (POST /event, GET /api/turns, GET /api/events, GET "
            "/api/ops, POST /chat, GET /chat/stream, plus the HTML shells "
            "at /turns and /ops). Set the env var before exposing the "
            "port beyond localhost."
        )

    app.router.add_get("/", _handle_root)
    app.router.add_post("/event", _handle_event)
    app.router.add_get("/health", _handle_health)
    app.router.add_post("/api/memory/consolidate", _handle_consolidate)
    # Turn viewer + log API (SPEC §11).
    from .usage_history import active_provider_for_spec
    web_ui.register_routes(
        app,
        turns_log=config.turns_log,
        events_log=config.events_log,
        home=config.home,
        # Hand the /saga dashboard the SAME saga.toml-resolved path the
        # saga client uses (``<home>/.mimir/saga.db`` by default), so it
        # reads the live DB instead of web_ui's stale
        # ``<home>/state/saga.db`` fallback — which no longer exists and
        # produced "saga db not found or unreadable" on the page.
        saga_db=_db_path,
        commitments_store=_RuntimeFieldProxy(runtime_slot, "commitments_store"),
        # Collapse the /ops Usage chart to the live subscription provider so
        # stale windows from a prior provider (e.g. Anthropic after a Codex
        # cutover, chainlink #301) don't render a second chart.
        active_usage_provider=active_provider_for_spec(
            config.model_spec,
            getattr(config, "anthropic_base_url", ""),
        ),
        turn_event_bus=_RuntimeFieldProxy(runtime_slot, "turn_event_bus"),
    )
    if config.activity_panel_channels:
        app["activity_panel"] = None
    # Web chat bridge — POST /chat + GET /chat/stream for the local UI.
    web_chat.register_routes(app)

    async def _start_application(app: web.Application) -> None:
        startup_state.runtime_attempted = True
        bundle = await create_agent_runtime(config, core, runtime_adapters)
        startup_state.bundle = bundle
        _publish_runtime(app, runtime_slot, startup_state, bundle)
        agent = bundle.agent
        indexer = bundle.indexer
        saga_client = bundle.saga_client
        replayed = bundle.replayed_messages
        if config.activity_panel_channels:
            from .bridges._activity_panel import ActivityPanel

            activity_panel = ActivityPanel(
                bundle.turn_event_bus,
                channels,
                config.activity_panel_channels,
            )
            startup_state.activity_panel = activity_panel
            app["activity_panel"] = activity_panel
            startup_state.activity_panel_start_attempted = True
            activity_panel.start()
        for alert in await asyncio.to_thread(repo_binding_startup_alerts):
            await log_event("github_repo_binding_attention_required", **alert)
        # PR 4b (docs/internal/MIMIR_HOME_GIT_TRACKING.md): idempotent bootstrap. Runs
        # before the agent starts processing turns so the post-turn
        # commit hook lands on a real repo. Sync function dispatched to
        # a thread because subprocess.run blocks the loop. Bootstrap
        # failures are logged but never fatal — the agent can still
        # serve turns; the post-turn hook self-skips when .git is
        # missing.
        if config.git_tracking_enabled:
            try:
                from .git_bootstrap import bootstrap_git_repo

                async def _bootstrap_log(event_kind: str, **fields: Any) -> None:
                    await log_event(event_kind, **fields)

                # log_event is async; wrap a sync shim for the bootstrap
                # callback that schedules the awaitable on the running
                # loop.
                running_loop = asyncio.get_running_loop()

                def _sync_log_event(event_kind: str, **fields: Any) -> None:
                    asyncio.run_coroutine_threadsafe(
                        _bootstrap_log(event_kind, **fields),
                        running_loop,
                    )

                await asyncio.to_thread(
                    bootstrap_git_repo,
                    config.home,
                    state_repo=config.git_state_repo,
                    github_token=config.git_state_token,
                    log_event=_sync_log_event,
                )
                try:
                    from .defaults_upgrade import (
                        UPGRADE_PROMPT_DISPATCH_ACTIONS,
                        check_and_open_defaults_upgrade,
                        enqueue_upgrade_prompt_turns,
                        enqueue_upgrade_reconciliation_turn,
                        read_last_synced_version,
                    )

                    # Capture the version we're upgrading FROM before the check
                    # advances last-synced-version, so version-specific upgrade
                    # prompts (chainlink #645) know the transition.
                    prev_defaults_version = await asyncio.to_thread(
                        read_last_synced_version, config.home,
                    )
                    defaults_result = await asyncio.to_thread(
                        check_and_open_defaults_upgrade,
                        config.home,
                    )
                    await log_event(
                        "defaults_upgrade_checked",
                        action=defaults_result.action,
                        version=defaults_result.version,
                        proposal_branch=(defaults_result.proposal.branch if defaults_result.proposal else None),
                        conflicts=defaults_result.conflicts,
                    )
                    upgrade_enqueued = await enqueue_upgrade_reconciliation_turn(
                        config.home,
                        defaults_result,
                        dispatcher.enqueue,
                        doc_changes=_doc_changes,
                    )
                    if upgrade_enqueued:
                        await log_event(
                            "defaults_upgrade_turn_enqueued",
                            version=defaults_result.version,
                            proposal_branch=(
                                defaults_result.proposal.branch if defaults_result.proposal else None
                            ),
                            conflicts=defaults_result.conflicts,
                        )
                    # Version-specific upgrade prompts (chainlink #645): one-shot
                    # migration nudges for the version(s) crossed in this bump.
                    upgrade_prompts_enqueued = await enqueue_upgrade_prompt_turns(
                        config.home,
                        previous=prev_defaults_version,
                        current=defaults_result.version,
                        action=defaults_result.action,
                        enqueue=dispatcher.enqueue,
                    )
                    if defaults_result.action in UPGRADE_PROMPT_DISPATCH_ACTIONS:
                        # Log on every consumed bump, count=0 included, so a
                        # "no upgrade prompt matched" run is observable (#645)
                        # — not spammed on the common already_synced startup.
                        await log_event(
                            "upgrade_prompts_enqueued",
                            version=defaults_result.version,
                            from_version=prev_defaults_version,
                            count=upgrade_prompts_enqueued,
                        )
                except Exception as exc:  # noqa: BLE001
                    await log_event(
                        "defaults_upgrade_failed",
                        home=str(config.home),
                        error=str(exc)[:500],
                    )
            except Exception as exc:  # noqa: BLE001
                await log_event(
                    "git_bootstrap_failed",
                    home=str(config.home),
                    error=str(exc)[:500],
                )

        # Install pre-push staleness-gate hook to source repo.
        # Independent of git_tracking_enabled — protects pushes from
        # any heartbeat, not just state commits. Non-fatal if missing.
        # See: chainlink #249, mimir/skills/github/SKILL.md §"Pre-push staleness gate"
        # Source-repo path for the pre-push staleness gate. Configurable via
        # MIMIR_SOURCE_REPO; defaults to the container checkout for back-compat.
        # Gated on is_dir() so PyPI / non-Docker installs (no source checkout)
        # silently skip it instead of erroring. Resolved BEFORE the try so the
        # non-fatal except handler can always reference it (an import failure
        # must not turn this into an UnboundLocalError that fails startup).
        _src = os.environ.get("MIMIR_SOURCE_REPO", "/workspace/mimir")
        try:
            from pathlib import Path as _Path
            from .git_bootstrap import ensure_workspace_hooks as _ensure_ws_hooks
            _source_repo = _Path(_src)
            if _source_repo.is_dir():
                await asyncio.to_thread(_ensure_ws_hooks, _source_repo)
        except Exception as exc:  # noqa: BLE001
            log.warning("pre-push hook install failed for %s: %s", _src, exc)

        startup_state.indexer_start_attempted = True
        await indexer.start(run_initial_sweep=False, sweep_loop=True)
        startup_state.bridges_connect_attempted = True
        await channels.connect_all()

        # MCP servers (opt-in via MIMIR_MCP_SERVERS_JSON / _PATH).
        # Bridged tools are appended to the agent's surface via the
        # mimir.tools.mcp setter. A single server failing to start is
        # logged + skipped — the agent still boots with native tools.
        # Lifecycle owner stored on app so _on_cleanup can shut it down.
        from .mcp_client import MCPManager, MCPPolicyStore

        try:
            stored_mcp_configs = MCPPolicyStore(
                config.home / "state" / "mcp-policy.json"
            ).load_server_configs()
        except ValueError as exc:
            log.warning("UI-managed MCP configuration ignored: %s", exc)
            stored_mcp_configs = []
        mcp_configs = [*config.mcp_servers, *stored_mcp_configs]
        if mcp_configs:
            startup_state.mcp_start_attempted = True
            mcp_manager = MCPManager(
                policy_store_path=config.home / "state" / "mcp-policy.json"
            )
            startup_state.mcp_manager = mcp_manager
            mcp_manager, mcp_tools = await _start_mcp_servers(
                mcp_manager,
                mcp_configs,
                home=config.home,
            )
            startup_state.mcp_manager = mcp_manager
            app["mcp_manager"] = mcp_manager
            bundle.install_mcp_tools(mcp_tools)

        # Register SAGA weekly consolidation. Bad cron logs and continues.
        # Pass home so the closure can read identities.yaml at fire time
        # and thread canonical names into the consolidation prompt's
        # P48 vocab block (Option A — operator-curated canonical subjects).
        try:
            consolidate_registered = scheduler.add_saga_consolidate_job(
                saga_client, config.saga_consolidate_cron,
                home=config.home,
            )
        except ValueError as exc:
            await log_event("scheduler_invalid_cron", error=str(exc))
            consolidate_registered = False

        # Check FAISS soft-removal fragmentation independently of the
        # consolidation and forgetting schedules.
        try:
            scheduler.add_saga_index_rebuild_job(saga_client)
        except ValueError as exc:
            await log_event(
                "scheduler_invalid_cron", error=str(exc), job="saga-index-rebuild"
            )

        # Register the daily index-integrity check (SPEC §8.3,
        # §16 item 16). Runs 30 min after saga-consolidate so any
        # consolidation-induced corruption surfaces before agent
        # turns hit stale retrieval. Bad cron logs and continues —
        # this is a detection-only check; missing it isn't fatal.
        try:
            scheduler.add_index_integrity_job(home=config.home)
        except ValueError as exc:
            await log_event("scheduler_invalid_cron", error=str(exc), job="index-integrity")

        # Register the Worklink stale-claim TTL reaper (#444). Opt-in:
        # MIMIR_WORKLINK_REAPER_CRON empty -> no job installed (non-Worklink
        # homes register nothing). Recovers leaves whose worker died back to
        # the ready queue. Detection/recovery-only; bad cron logs and continues.
        try:
            scheduler.add_worklink_reaper_job(
                home=config.home,
                cron_expr=os.environ.get("MIMIR_WORKLINK_REAPER_CRON", ""),
            )
        except ValueError as exc:
            await log_event("scheduler_invalid_cron", error=str(exc), job="worklink-reaper")

        # Enforce each PR checkout lease's recorded expiry. The root itself is
        # opt-in, but configured deployments get a default-on periodic sweep.
        lease_root_value = os.environ.get("MIMIR_PR_CHECKOUT_LEASE_ROOT", "").strip()
        if lease_root_value:
            try:
                from .pr_checkout_lease import configured_pr_checkout_lease_root

                scheduler.add_pr_checkout_lease_reaper_job(
                    lease_root=configured_pr_checkout_lease_root(),
                    cron_expr=os.environ.get(
                        "MIMIR_PR_CHECKOUT_LEASE_REAPER_CRON", "*/15 * * * *",
                    ),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                await log_event(
                    "scheduler_invalid_pr_checkout_lease_reaper",
                    error=str(exc),
                    job="pr-checkout-lease-reaper",
                )

        # Scratch retention janitor: scratch/ is ephemeral by contract
        # (config.py writable-dirs table) but nothing deleted it — poller-
        # driven per-task clones left a live home with 140 GB in six weeks.
        # Default-ON daily sweep; opt out with MIMIR_SCRATCH_JANITOR_CRON=""
        # or MIMIR_SCRATCH_TTL_DAYS=0. Bad cron logs and continues.
        try:
            scheduler.add_scratch_janitor_job(
                home=config.home,
                cron_expr=os.environ.get("MIMIR_SCRATCH_JANITOR_CRON", "13 4 * * *"),
            )
        except ValueError as exc:
            await log_event("scheduler_invalid_cron", error=str(exc), job="scratch-janitor")

        # Resume Worklink runs orphaned by a restart (#561). After the #832
        # substrate cleanup local_subprocess is the only Worklink compute and
        # its runs die with the controller, so no run state is persisted today
        # and this is a no-op in production. It remains in place so a stale
        # run-state file from a pre-#832 docker-sibling / ecs-runtask run still
        # triggers a (then-no-op) resume instead of orphaning the work.
        # Best-effort + non-blocking (each resume runs detached).
        try:
            resumed = reattach_inflight_worklink_runs(config.home)
            if resumed:
                await log_event("worklink_reattach_dispatched", issues=resumed)
        except Exception as exc:  # noqa: BLE001 — startup reconcile must never abort boot
            await log_event("worklink_reattach_dispatch_failed", error=str(exc))

        # Register the weekly viability report (SPEC §16 follow-up
        # from the 2026-05-23 VSM eval — collapse detection + curation
        # rate). Runs Sunday 5 AM, after introspection-report at 4 AM
        # so the report sees the week's fresh reflection output.
        # Detection-only; bad cron logs and continues.
        try:
            scheduler.add_viability_report_job(home=config.home)
        except ValueError as exc:
            await log_event("scheduler_invalid_cron", error=str(exc), job="viability-report")

        # Register monthly applied-proposals audit (VSM S4-2 double-loop
        # closure). Runs on the 1st of each month at 08:00 UTC; computes
        # before/after signals for proposals applied 1-4 weeks prior.
        # Detection-only; bad cron logs and continues.
        try:
            scheduler.add_applied_audit_job(home=config.home)
        except ValueError as exc:
            await log_event("scheduler_invalid_cron", error=str(exc), job="applied-audit")

        # Register daily proposed-changes backlog check. Surfaces
        # operator review backlog (>= 10 pending OR oldest >= 21d old)
        # as a negative algedonic event the next turn after the cron
        # fires. Detection-only; bad cron logs and continues.
        try:
            scheduler.add_proposed_changes_backlog_job(home=config.home)
        except ValueError as exc:
            await log_event(
                "scheduler_invalid_cron", error=str(exc),
                job="proposed-changes-backlog",
            )

        # Register daily PyPI update-check. Surfaces newer mimir
        # releases as a positive algedonic event so operators see
        # "newer version available" in the agent's per-turn block
        # and via the /ops dashboard. Detection-only — operator
        # runs ``mimir update --apply`` to actually install + then
        # ``docker compose restart`` to engage.
        try:
            scheduler.add_update_check_job(home=config.home)
        except ValueError as exc:
            await log_event(
                "scheduler_invalid_cron", error=str(exc),
                job="update-check",
            )

        # Register weekly introspection-report cron (FEEDBACK-LOOPS §4.7
        # + §4.8). Non-LLM: aggregates turns/events, writes report,
        # emits heartbeat_health_degraded events when scheduled-tick
        # success rate drops below threshold.
        try:
            introspection_registered = scheduler.add_introspection_report_job(
                config.home,
                config.introspection_report_cron,
                days=config.introspection_report_days,
                emit_algedonic=config.introspection_report_emit_algedonic,
                health_threshold=config.introspection_report_health_threshold,
            )
        except ValueError as exc:
            await log_event(
                "scheduler_invalid_cron",
                job="introspection-report",
                error=str(exc),
            )
            introspection_registered = False

        # Commitments Phase 2b — periodic due-check sweep. Reuses the
        # agent's CommitmentsStore so deliver/expire calls land in
        # the same JSONL as Phase 1's manual operator entries +
        # Phase 2a's extracted commitments.
        #
        # PR #126 review #1: the store is wired in Agent.__init__
        # by Phase 2a (PR #125). If 2b lands before 2a, the attribute
        # is missing and the registration block would silently no-op.
        # Path 2 (observable no-op): emit ``scheduler_skipped`` when
        # the cron is configured but the store isn't ready — operator
        # sees "poller didn't run because the agent doesn't have the
        # store" instead of wondering why commitments never expire.
        if config.commitments_due_check_cron:
            commitments_store = getattr(agent, "_commitments", None)
            if commitments_store is None:
                await log_event(
                    "scheduler_skipped",
                    job="commitments-due-check",
                    reason="agent_commitments_attr_missing",
                    note=(
                        "Phase 2b cron configured but Agent._commitments "
                        "not wired; merge Phase 2a (PR #125) first or "
                        "clear MIMIR_COMMITMENTS_DUE_CHECK_CRON."
                    ),
                )
            else:
                try:
                    scheduler.add_commitments_due_check_job(
                        commitments_store,
                        config.commitments_due_check_cron,
                        snooze_pileup_threshold=(
                            config.commitments_snooze_pileup_threshold
                        ),
                    )
                except ValueError as exc:
                    await log_event(
                        "scheduler_invalid_cron",
                        job="commitments-due-check",
                        error=str(exc),
                    )

        # Stage 5 of docs/internal/CLAUDE_SDK_CLIENT_MIGRATION.md retired the original
        # quota-poll cron because the plan was to use the shared
        # persistent client's get_context_usage(). That endpoint turned
        # out to be context-window data; its apiUsage side-channel is
        # session-scoped and consistently empty on Claude Max OAuth
        # (chainlink #9). Plan-window utilization% lives at
        # ``GET /api/oauth/usage`` and requires the user:profile OAuth
        # scope, which the headless setup-token flow doesn't grant.
        # The new oauth_usage_poller fills the gap by reading
        # ``credentials.json`` (operator-minted via ``claude /login``)
        # and refreshing tokens itself, bypassing Claude Code CLI's
        # broken auto-refresh on headless / copied-creds boxes.
        # Shared RateLimitStore used by both the Anthropic OAuth
        # poller and the Minimax poller below. Constructed
        # unconditionally so the Minimax path doesn't depend on the
        # OAuth path's gating. Single writer per poller instance, the
        # store's own asyncio.Lock serializes concurrent writes — fine
        # for two pollers on different cron cadences.
        rate_limit_store = getattr(agent, "_rate_limits", None) or RateLimitStore(
            path=config.home / ".mimir" / "rate_limits.json",
        )
        # Only run the Anthropic OAuth usage poller when Anthropic is the
        # ACTIVE quota provider. On a Codex / Minimax deployment it would
        # otherwise keep refreshing stale Anthropic keys every few minutes
        # (and spam refresh-token-age warnings), burying the live provider's
        # quota in the Resource-usage view (chainlink #301).
        from .providers import provider_for_quota

        _active_quota_provider = provider_for_quota(
            config.model_spec, config.anthropic_base_url
        ).quota_provider_key
        oauth_poll_registered = False
        if (
            config.oauth_credentials_path is not None
            and _active_quota_provider == "anthropic"
        ):
            try:
                # Post-cutover (2026-05-15): agent._rate_limits is a no-op stub
                # because the deepagents path no longer streams SDK
                # RateLimitEvent messages. The poller owns its own
                # RateLimitStore here — single writer, single asyncio.Lock,
                # no race. The path is the same JSON file the SDK-era
                # agent wrote to so operators get continuity.
                oauth_poll_registered = scheduler.add_oauth_usage_poll_job(
                    rate_limit_store,
                    config.oauth_usage_poll_cron,
                    config.oauth_credentials_path,
                    refresh_warn_days=config.oauth_refresh_warn_days,
                    # chainlink #17: enable the cost-rate-back-derived
                    # 5h estimator so endpoint glitches don't leave the
                    # arbiter blind to actual usage during a long
                    # outage. Falls back to "keep prior trusted value"
                    # when derive math can't run (no observable cost,
                    # no prior 7d util).
                    turns_log_path=config.turns_log,
                )
            except ValueError as exc:
                await log_event(
                    "scheduler_invalid_cron",
                    job="oauth-usage-poll",
                    error=str(exc),
                )

        # Codex account usage is a named callable and bypasses the LLM
        # arbiter, allowing a reset reading to recover TIGHT/BLOCKED work.
        # Gate on the actual subscription route: public ``openai:`` API-key
        # traffic shares the quota-provider key but not this account endpoint.
        codex_poll_registered = False
        if config.model_spec.strip().lower().startswith("codex-plus:"):
            try:
                codex_poll_registered = scheduler.add_codex_usage_poll_job(
                    rate_limit_store,
                    config.codex_usage_poll_cron,
                )
            except ValueError as exc:
                await log_event(
                    "scheduler_invalid_cron",
                    job="codex-usage-poll",
                    error=str(exc),
                )
        # Minimax usage poller. Opt-in: requires both
        # MIMIR_MINIMAX_USAGE_POLL_CRON (non-empty) AND
        # MINIMAX_API_KEY in env. We don't gate on billing_mode here
        # — the poller is harmless on a pay-as-you-go account (just
        # writes utilization snapshots; arbiter consumes them only if
        # MinimaxQuotaProvider is registered, which is gated on
        # billing_mode + ANTHROPIC_BASE_URL in mimir.billing).
        minimax_poll_registered = False
        if config.minimax_usage_poll_cron.strip():
            minimax_api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
            if not minimax_api_key:
                await log_event(
                    "scheduler_invalid_cron",
                    job="minimax-usage-poll",
                    error=(
                        "MIMIR_MINIMAX_USAGE_POLL_CRON is set but "
                        "MINIMAX_API_KEY is unset — poller not registered"
                    ),
                )
            else:
                try:
                    minimax_poll_registered = scheduler.add_minimax_usage_poll_job(
                        rate_limit_store,
                        config.minimax_usage_poll_cron,
                        minimax_api_key,
                        model_name=config.minimax_usage_model_name,
                    )
                except ValueError as exc:
                    await log_event(
                        "scheduler_invalid_cron",
                        job="minimax-usage-poll",
                        error=str(exc),
                    )

        # Identities populator (chainlink #44). Daily scrape of
        # connected bridges into state/identities.yaml. Default cron
        # is empty (disabled) — operator opt-in via
        # MIMIR_IDENTITIES_POPULATE_CRON so bridge API hits don't
        # surprise environments. Channel registry is passed in (not
        # the bridges themselves) so reconnects mid-day still get
        # picked up on the next tick.
        identities_populate_registered = False
        try:
            identities_populate_registered = scheduler.add_identities_populate_job(
                config.home,
                config.identities_populate_cron,
                channels,
            )
        except ValueError as exc:
            await log_event(
                "scheduler_invalid_cron",
                job="identities-populate",
                error=str(exc),
            )

        # Bind-mount health probe (docs/internal/BIND_MOUNT_HEALTH_PROBE.md).
        # Detects VirtioFS stale-inode failures and self-restarts via
        # SIGTERM to PID 1. The probe self-gates on
        # ``/proc/self/mountinfo`` containing a virtiofs entry, so
        # registering it on bare-metal Linux / OrbStack-without-virtiofs
        # / CI is harmless — it short-circuits per tick.
        health_probe_registered = False
        try:
            health_probe_registered = scheduler.add_health_probe_job(
                config.home,
                config.events_log,
                config.health_probe_cron,
                max_restarts_per_hour=config.health_probe_max_restarts_per_hour,
            )
        except ValueError as exc:
            await log_event(
                "scheduler_invalid_cron",
                job="bind-mount-health-probe",
                error=str(exc),
            )

        # Scheduler-health check (chainlink #66 — scheduler wedge).
        # Fires every 10 min; reads events.jsonl + scheduler.yaml to detect
        # a stale heartbeat and pushes an ntfy alarm if elapsed time exceeds
        # (heartbeat cron period × 2.0).  Threshold auto-adapts when an
        # operator changes the heartbeat cadence.
        scheduler_health_registered = False
        try:
            scheduler_health_registered = scheduler.add_scheduler_health_check_job(
                config.events_log,
                config.home / "scheduler.yaml",
            )
        except ValueError as exc:
            await log_event(
                "scheduler_invalid_cron",
                job="scheduler-health-check",
                error=str(exc),
            )

        # Auto-refresh installed optional skills from shipped source before
        # poller registration.  This closes the deploy gap where
        # ``mimir/optional-skills/<name>/`` changed but the operator-installed
        # ``<home>/skills/<name>/`` copy stayed stale until someone manually ran
        # ``mimir skills update --apply`` (chainlink #557).  The helper uses
        # the safe update path: source-changed/source-added files are applied
        # with backups; installed-only files and per-skill .env are preserved.
        skill_update_result = None
        try:
            from .skill_install import auto_update_installed_optional_skills

            skill_update_result = await asyncio.to_thread(
                auto_update_installed_optional_skills,
                config.home,
            )
            update_event = _skill_auto_update_event(skill_update_result)
            if update_event is not None:
                event_kind, fields = update_event
                await log_event(event_kind, **fields)
        except Exception as exc:  # noqa: BLE001 — skill sync must not block boot
            await log_event(
                "skills_auto_update_failed",
                error=str(exc)[:500],
            )

        # Validate Worklink before its ready-queue poller can consume a leaf.
        # A mismatch degrades only Worklink; the server and unrelated pollers boot.
        worklink_config_path = config.home / "worklink.yaml"
        if worklink_config_path.exists():
            try:
                from .worklink.backends.registry import BackendRegistry, WorklinkConfig

                BackendRegistry(WorklinkConfig.load(worklink_config_path))
            except Exception as exc:  # noqa: BLE001 — operator config must not abort boot
                log.warning("Worklink disabled by invalid config %s: %s", worklink_config_path, exc)
                await log_event(
                    "worklink_config_invalid",
                    path=str(worklink_config_path),
                    error=str(exc)[:500],
                    fix=f"correct {worklink_config_path} before Worklink dispatch can resume",
                )

        # Load LLM-tick jobs from scheduler.yaml.
        reload_stats = scheduler.reload()

        # Pollers framework (chainlink #3). Discovers any
        # ``<home>/skills/**/pollers.json`` and registers each as a
        # cron-fired subprocess. Most installs have no pollers and
        # ``installed_pollers`` is 0 (no-ops cleanly). Bundled
        # built-ins under ``<home>/.mimir_builtin_skills/`` are NOT
        # scanned — pollers are deployment-specific operator config,
        # never part of the mimir bundle.
        installed_pollers = scheduler.add_poller_jobs(
            home_skills_dir(config.home),
        )

        if (
            consolidate_registered
            or introspection_registered
            or oauth_poll_registered
            or codex_poll_registered
            or health_probe_registered
            or scheduler_health_registered
            or identities_populate_registered
            or reload_stats["registered"] > 0
            or installed_pollers > 0
        ):
            startup_state.scheduler_start_attempted = True
            scheduler.start()
            startup_state.scheduler_started = True

        await log_event(
            "app_started",
            home=str(config.home),
            web_port=config.web_port,
            replayed_messages=replayed,
            saga_consolidate_cron=config.saga_consolidate_cron if consolidate_registered else "",
            saga_session_idle_minutes=config.saga_session_idle_minutes,
            seeded_subagents=seeded,
            seeded_skills=seeded_skills_map,
            scheduled_jobs_registered=reload_stats["registered"],
            scheduled_jobs_invalid=reload_stats["invalid"],
        )
        await log_event("api_started", port=config.web_port)

        # Drain any startup-events recorded by the pre-init pending-
        # update pre-flight in this process boot. ``init_logger`` is
        # now up, so ``mimir_update_starting`` / ``_applied`` /
        # ``_failed`` events queued in ``<home>/.mimir/startup-events.jsonl``
        # land in events.jsonl and surface in the algedonic feedback
        # block on the next turn. No-op when no pending-update flow
        # ran on this boot (the common case).
        from .update_on_start import (
            consume_startup_events,
            consume_update_digest,
            emit_version_bump_digest,
        )
        try:
            drained = await consume_startup_events(config.home, log_event)
            if drained:
                log.info("drained %d startup-update event(s) into events.jsonl", drained)
        except Exception:  # noqa: BLE001 — drain is best-effort
            log.exception("startup-events drain failed")
        drained_digest = 0
        try:
            drained_digest = await consume_update_digest(config.home, log_event)
            if drained_digest:
                log.info("drained post-update digest into events.jsonl")
        except Exception:  # noqa: BLE001 — drain is best-effort
            log.exception("post-update digest drain failed")
        # chainlink #363 / #557: operator deploys (pip install / git pull +
        # docker restart) bump the version WITHOUT the self-update path's
        # digest. Detect the bump here, safely auto-refresh installed optional
        # skills from shipped source, and emit the same mimir_update_digest so
        # the agent sees what changed and what still needs inspection.
        try:
            bumped = await emit_version_bump_digest(
                config.home,
                log_event,
                already_drained=bool(drained_digest),
                skill_update_result=skill_update_result,
            )
            if bumped:
                log.info("emitted version-bump digest (operator deploy)")
        except Exception:  # noqa: BLE001 — best-effort
            log.exception("version-bump digest emit failed")

        spawn_background(
            startup_background_tasks,
            indexer.sweep(),
            name="mimir-startup-indexer-sweep",
        )

        # Clean-shutdown / unclean-restart detection (chainlink #507). A
        # complementary, sidecar-free signal to the out-of-process watchdog:
        # mark_session_running writes a clean=false marker now; _on_cleanup
        # flips it to clean=true on a graceful (SIGTERM-initiated) stop. If the
        # prior marker is still clean=false at this boot, the last run died
        # without cleanup — crash, OOM-kill, hard restart, or a wedge that got
        # killed. Log it (so it surfaces in the algedonic feedback block) and
        # push an out-of-band notice on the same sinks as the watchdog. First
        # boot (no marker) and a clean prior stop both no-op.
        from .liveness import (
            UNCLEAN_NOTIFY_WINDOW,
            detect_unclean_restart,
            mark_session_running,
            notify_unclean_restart,
        )
        _now = time.time()
        _prior_session = detect_unclean_restart(config.home)
        # Coalesce notices across a crash-loop: notify only if we haven't
        # already paged within UNCLEAN_NOTIFY_WINDOW. The event is logged every
        # time regardless; only the out-of-band notify is rate-limited.
        _notify = False
        _carry_ts: float | None = None
        if _prior_session is not None:
            _last = _prior_session.get("last_unclean_notify_ts")
            _within = isinstance(_last, (int, float)) and (_now - _last) < UNCLEAN_NOTIFY_WINDOW
            _notify = not _within
            _carry_ts = _now if _notify else _last
        startup_state.liveness_mark_attempted = True
        mark_session_running(
            config.home, started_at=_now, last_unclean_notify_ts=_carry_ts,
        )
        if _prior_session is not None:
            await log_event(
                "liveness_unclean_restart",
                prior_started_iso=_prior_session.get("started_iso"),
                prior_pid=_prior_session.get("pid"),
                notified=_notify,
            )
            if _notify:
                # Background — the notify POSTs to ntfy/webhook (up to 8s) and
                # must not block startup.
                spawn_background(
                    startup_background_tasks,
                    notify_unclean_restart(config.home, prior=_prior_session),
                    name="mimir-unclean-restart-notify",
                )

        # Liveness beat (chainlink #507): periodically rewrite
        # .mimir/liveness.json so the out-of-process ``mimir watchdog`` can
        # detect a dead/wedged agent. As an event-loop task it also stops on
        # a wedge — the watchdog keys on the beat's *absence*, not on errors.
        if config.liveness_beat_seconds > 0:
            from .liveness import liveness_beat_loop
            spawn_background(
                startup_background_tasks,
                liveness_beat_loop(
                    config.home,
                    interval=config.liveness_beat_seconds,
                    started_at=time.time(),
                ),
                name="mimir-liveness-beat",
            )

    async def _compensate_startup(app: web.Application) -> list[Exception]:
        errors: list[Exception] = []

        async def attempt(operation: Any) -> None:
            try:
                await operation()
            except BaseException as exc:
                errors.append(_cleanup_exception(exc))

        def attempt_sync(operation: Any) -> None:
            try:
                operation()
            except BaseException as exc:
                errors.append(_cleanup_exception(exc))

        try:
            task_errors = await cancel_background_tasks(
                startup_background_tasks,
                label="server startup compensation",
            )
        except BaseException as exc:
            errors.append(_cleanup_exception(exc))
        else:
            errors.extend(_cleanup_exception(error) for error in task_errors)
        if startup_state.liveness_mark_attempted:
            from .liveness import mark_clean_shutdown

            attempt_sync(lambda: mark_clean_shutdown(config.home))
        if startup_state.scheduler_start_attempted:
            attempt_sync(scheduler.stop)
        if startup_state.mcp_manager is not None:
            await attempt(startup_state.mcp_manager.shutdown)
        if startup_state.bridges_connect_attempted:
            await attempt(channels.disconnect_all)
        if startup_state.bundle is not None:
            await attempt(startup_state.bundle.aclose)
        if (
            startup_state.activity_panel_start_attempted
            and startup_state.activity_panel is not None
        ):
            await attempt(startup_state.activity_panel.stop)
        await attempt(pairing_notifier.aclose)
        try:
            _clear_runtime_app_state(app, runtime_slot, startup_state)
        except BaseException as exc:
            errors.append(_cleanup_exception(exc))
        finally:
            startup_state.compensated = True
        return errors

    async def _on_startup(app: web.Application) -> None:
        try:
            await _start_application(app)
        except BaseException as original_exception:
            errors = await _compensate_startup(app)
            for error in errors:
                log.error("server startup compensation failed: %s", error)
            if errors:
                original_exception.add_note(_cleanup_note(errors))
            raise

    async def _on_cleanup(app: web.Application) -> None:
        errors: list[Exception] = []

        async def attempt(operation: Any) -> None:
            try:
                await operation()
            except BaseException as exc:
                errors.append(_cleanup_exception(exc))

        def attempt_sync(operation: Any) -> Any:
            try:
                return operation()
            except BaseException as exc:
                errors.append(_cleanup_exception(exc))
                return None

        if startup_state.compensated:
            try:
                task_errors = await cancel_background_tasks(
                    startup_background_tasks,
                    label="server cleanup",
                )
            except BaseException as exc:
                errors.append(_cleanup_exception(exc))
            else:
                errors.extend(_cleanup_exception(error) for error in task_errors)
            try:
                _clear_runtime_app_state(app, runtime_slot, startup_state)
            except BaseException as exc:
                errors.append(_cleanup_exception(exc))
        else:
            cleanup_started = time.monotonic()
            from .liveness import mark_clean_shutdown

            attempt_sync(lambda: mark_clean_shutdown(config.home))
            from .worklink.autonomy import release_claims_for_graceful_shutdown

            release_timeout = 5.0
            if config.drain_timeout_seconds > 0:
                release_timeout = min(
                    release_timeout,
                    float(config.drain_timeout_seconds),
                )
            released = attempt_sync(
                lambda: release_claims_for_graceful_shutdown(
                    config.home,
                    agent_id=worklink_agent_id,
                    timeout_s=release_timeout,
                )
            )
            if released is not None:
                await attempt(
                    lambda: log_event(
                        "worklink_shutdown_claims_released",
                        issue_ids=[record.issue_id for record in released],
                    )
                )
            await attempt(lambda: log_event("shutdown", reason="cleanup"))
            drain_timeout = config.drain_timeout_seconds
            if drain_timeout > 0:
                drain_timeout = max(
                    0.0,
                    drain_timeout - (time.monotonic() - cleanup_started),
                )
            await attempt(lambda: dispatcher.drain(timeout=drain_timeout))
            try:
                task_errors = await cancel_background_tasks(
                    startup_background_tasks,
                    label="server cleanup",
                )
            except BaseException as exc:
                errors.append(_cleanup_exception(exc))
            else:
                errors.extend(_cleanup_exception(error) for error in task_errors)
            attempt_sync(scheduler.stop)
            if startup_state.bundle is not None:
                await attempt(startup_state.bundle.aclose)
            if startup_state.activity_panel is not None:
                await attempt(startup_state.activity_panel.stop)
            await attempt(pairing_notifier.aclose)
            await attempt(channels.disconnect_all)
            if startup_state.mcp_manager is not None:
                await attempt(startup_state.mcp_manager.shutdown)
            try:
                _clear_runtime_app_state(app, runtime_slot, startup_state)
            except BaseException as exc:
                errors.append(_cleanup_exception(exc))

        for error in errors:
            log.error("server cleanup failed: %s", error)
        if errors:
            raise ExceptionGroup("server cleanup failed", errors)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _validate_bind_security(host: str, api_key: str) -> None:
    """Refuse to bind a non-loopback interface without an API key.

    Pre-OSS hardening (review item #2). The prior default bound
    ``0.0.0.0`` regardless of whether ``MIMIR_API_KEY`` was set, so
    any container with a published port was accessible to any
    network peer with no auth at all. We now refuse the unsafe
    combination at startup with an actionable message.

    Loopback binds (``127.0.0.1``, ``::1``, ``localhost``) are allowed
    without an API key for local development. This limits network exposure but
    does not authenticate local processes; HTTP middleware separately rejects
    browser cross-site writes and DNS-rebinding Host headers. Any other host
    requires ``MIMIR_API_KEY`` to be set.
    """
    if not api_key and host not in _LOOPBACK_HOSTS:
        raise SystemExit(
            f"refusing to bind {host!r} without MIMIR_API_KEY set — "
            f"any host that can reach the port would be able to inject "
            f"events, drive the agent, and read conversation history. "
            f"Either set MIMIR_API_KEY=<a random secret> or set "
            f"MIMIR_WEB_HOST=127.0.0.1 (the default) for loopback-only "
            f"binding."
        )


def main() -> None:
    # Pre-flight: if the operator approved a mimir-package update via
    # the ``request_mimir_update`` tool, apply it now — BEFORE any
    # asyncio / logger / config import-chain that would lock the
    # current process to the old code. On install success the call
    # ``execv``'s away (same PID, fresh Python import); on failure
    # the flag is deleted and we continue on the old version. The
    # function is a no-op when no flag is present (the common path).
    # See ``mimir/update_on_start.py`` for the full design rationale.
    from .update_on_start import apply_pending_update
    _home_for_flag = Path(os.environ.get("MIMIR_HOME") or os.getcwd())
    apply_pending_update(_home_for_flag)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    _validate_bind_security(config.web_host, config.api_key)
    app = build_app(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host=config.web_host, port=config.web_port)
    loop.run_until_complete(site.start())
    log.info("mimir listening on %s:%d", config.web_host, config.web_port)

    stop = loop.create_future()

    def _on_signal() -> None:
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(stop)
    finally:
        loop.run_until_complete(runner.cleanup())
        loop.close()


if __name__ == "__main__":
    main()
