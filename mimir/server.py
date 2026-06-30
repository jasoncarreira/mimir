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
from pathlib import Path
from typing import Any

from aiohttp import web

from .agent import Agent
from .background_tasks import spawn_background
from .bridges.bench import BenchBridge
from .bridges.web_chat import WebChatBridge
from .channel_registry import ChannelRegistry
from .config import Config
from .dispatcher import Dispatcher
from .event_logger import init_logger, log_event
from .history import MessageBuffer
from .identities import IdentityResolver
from .index import IndexGenerator
from .models import AgentEvent, make_process_session_id
from .rate_limits import RateLimitStore
from .saga_client import SagaClient, make_saga_client
from .scheduler import Scheduler
from .search import Indexer
from .session_manager import ChannelSession, SessionManager
from .skill_defs import (
    home_skills_dir,
    migrate_legacy_skills_dir,
    refresh_builtin_skills,
    seed_scheduler,
)
from .chainlink_bootstrap import ensure_chainlink_initialized
from .prompt_templates import seed_prompts
from .subagent_defs import seed_subagent_defs
from .subagent_inbox import SubagentInbox
from .turn_logger import TurnLogger
from . import web_ui

log = logging.getLogger(__name__)

_STARTUP_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


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


async def _handle_event(request: web.Request) -> web.Response:
    # Auth: gated at the app-level middleware. See ``_make_auth_middleware``.
    try:
        body: dict[str, Any] = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    channel_id = body.get("channel_id")
    if not channel_id:
        return web.json_response({"error": "channel_id required"}, status=400)

    # #487: type-check structured fields, don't coerce. A truthy non-dict
    # ``extra`` (or non-list ``attachment_names``) survives ``or {}``/``or []``
    # and later ``event.extra.get(...)`` raises AttributeError → an unguarded
    # 500 in enqueue or a silently-dropped turn on the worker path. Reachable by
    # any client when MIMIR_API_KEY is unset. Reject with 400 instead.
    extra = body.get("extra")
    if extra is not None and not isinstance(extra, dict):
        return web.json_response({"error": "extra must be an object"}, status=400)
    attachment_names = body.get("attachment_names")
    if attachment_names is not None and not isinstance(attachment_names, list):
        return web.json_response(
            {"error": "attachment_names must be an array"}, status=400,
        )

    event = AgentEvent(
        trigger=body.get("trigger", "user_message"),
        channel_id=channel_id,
        content=body.get("content", ""),
        author=body.get("author"),
        author_id=body.get("author_id"),
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
# APIs expose global markdown state and graph health (chainlink #690). This is
# the SECURITY gate; React section-hiding is UX only and must never be the sole
# control.
_ADMIN_REQUIRED_PREFIXES: tuple[str, ...] = (
    "/api/v1/admin",
    "/api/ops",
    "/api/v1/ops",
    "/api/v1/scheduler",
    "/api/v1/chainlink-board",
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


def _make_auth_middleware(expected_key: str):
    """Build an aiohttp middleware that gates every non-exempt route on
    a matching ``X-API-Key`` header.

    Empty ``expected_key`` (``MIMIR_API_KEY`` unset) disables the gate
    entirely — the warning at startup tells the operator they're
    running open. Any non-empty key activates the middleware.

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

    A container restart kills the detached ``mimir worklink run`` controllers but
    NOT the docker-sibling/ecs worker containers they launched. For each persisted
    run state, spawn a detached ``mimir worklink run <id> --reattach`` that waits
    on the surviving worker, harvests evidence, and opens the PR — instead of
    orphaning the compute and waiting for the TTL reaper to re-run from scratch.

    Gated on ``WORKLINK_REPO`` (the same env the ready-queue poller needs); no-op
    on non-Worklink homes. Best-effort and non-blocking: each resume runs
    detached so a long worker wait never delays startup; a spawn failure for one
    leaf is logged and the rest still proceed."""
    import shlex
    import subprocess

    from .worklink.run_state import list_run_states, reattach_dispatch_argv

    spawn = popen or subprocess.Popen
    repo = os.environ.get("WORKLINK_REPO")
    if not repo:
        return []
    states = list_run_states(home)
    if not states:
        return []
    run_bin = shlex.split(os.environ.get("WORKLINK_RUN_BIN") or "mimir")
    state_dir = home / "state" / "worklink" / "runs"
    dispatched: list[int] = []
    for state in states:
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
    # 10MB body cap (aiohttp default is 1MB). Mimir takes JSON-only bodies on
    # /event and /chat — long bluesky transcripts and seed payloads can run
    # well past 1MB. Bridges read attachment bytes from disk via filesystem
    # paths (``attachment_names``), not from the request body, so the cap
    # doesn't need to accommodate binary uploads.
    #
    # Auth middleware: gates every non-exempt route on ``X-API-Key`` when
    # ``MIMIR_API_KEY`` is set. Empty key → middleware passes through
    # unconditionally (dev / localhost). See ``_make_auth_middleware``
    # and ``_AUTH_EXEMPT``.
    app = web.Application(
        client_max_size=10 * 1024 * 1024,
        middlewares=[_make_auth_middleware(config.api_key or "")],
    )

    if not config.api_key:
        _msg = (
            "MIMIR_API_KEY is not set — POST /event and POST /chat are "
            "unauthenticated. Any host that can reach this server can inject "
            "messages or trigger saga_end_session. "
            "Set MIMIR_API_KEY before exposing to a network. "
            "For development on localhost, set MIMIR_ALLOW_UNAUTHENTICATED=true "
            "to suppress this warning."
        )
        if getattr(config, "allow_unauthenticated", False):
            log.debug("unauthenticated mode acknowledged: %s", _msg)
        else:
            log.warning(_msg)

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
    # Initialize the Chainlink store if absent (+ the CLI is installed) so the
    # Tasks board works out of the box instead of reporting "unavailable".
    # Best-effort and gated on the binary, so plain pip installs are unaffected.
    ensure_chainlink_initialized(config.home)

    init_logger(
        config.events_log,
        make_process_session_id(),
        max_events=config.max_events_kept,
        agent_id=config.agent_id,
    )
    turn_logger = TurnLogger(config.turns_log, max_turns=config.max_turns_kept)

    # Identity reconciliation (FUTURE_WORK §6.1). Loads
    # state/identities.yaml if present; gracefully empty otherwise.
    identity_resolver = IdentityResolver(home=config.home)
    aliases_loaded = identity_resolver.reload()

    history_path = config.home / "messages" / "chat_history.jsonl"
    message_buffer = MessageBuffer(
        history_path=history_path,
        global_max=config.history_global_max,
        per_channel_max=config.history_per_channel_max,
        resolver=identity_resolver,
        cross_platform_pull=config.cross_platform_pull,
    )
    replayed = message_buffer.replay()

    indexes = IndexGenerator(config.home)
    indexes.mark_dirty("all")

    indexer = Indexer(config.home)
    # v0.5 §2: point saga at the per-home saga.toml. saga's config search
    # checks ``$SAGA_CONFIG`` first (then ``$SAGA_DATA_DIR/saga.toml``,
    # ``~/.saga/saga.toml``, package-default). mimir setup writes
    # ``<home>/saga.toml``, which doesn't match any of those defaults, so
    # set the env var here before the in-process saga adapter does its
    # first import. No-op if the operator already exported SAGA_CONFIG.
    home_saga_toml = config.home / "saga.toml"
    if home_saga_toml.is_file() and not os.environ.get("SAGA_CONFIG"):
        os.environ["SAGA_CONFIG"] = str(home_saga_toml)
    # ``[storage].db_path`` from saga.toml — overridable by operators
    # without touching code. Relative paths resolve under ``<home>/.mimir/``;
    # absolute paths (e.g. mimirbot's ``/mimir-home/.mimir/saga.db``) are
    # honored as-is. The default in ``mimir.saga._config_io._DEFAULTS`` is
    # ``"saga.db"`` (relative) so a saga.toml without an explicit
    # ``[storage]`` block lands at ``<home>/.mimir/saga.db`` — back to
    # the historical naming after the brief ``memory.db`` interlude.
    from .saga._config_io import get_config as _get_saga_config
    _toml_db = _get_saga_config()("storage", "db_path", "saga.db")
    _db_path = Path(_toml_db)
    if not _db_path.is_absolute():
        _db_path = config.home / ".mimir" / _db_path
    saga_client = make_saga_client(db_path=_db_path)
    sessions = SessionManager(
        idle_minutes=config.saga_session_idle_minutes,
        max_turns=config.saga_session_max_turns,
    )
    inbox = SubagentInbox()

    # Channel layer (SPEC §7.2). BenchBridge always registers — it's how the
    # benchmark adapter gets outbound. WebChatBridge registers if a
    # web_chat-friendly aiohttp app is hosting us; routes mount below in
    # _on_startup. Discord / Slack / Bluesky bridges register based on env
    # tokens (DISCORD_TOKEN etc.).
    channels = ChannelRegistry()
    channels.register(BenchBridge(home=config.home))
    pairing_notifier = _PairingNotifier(config, channels)

    # Wiring order to break the (dispatcher → agent → scheduler → dispatcher)
    # cycle: dispatcher first with no runner, then scheduler bound to its
    # enqueue, then agent (which builds the MCP server with all of them
    # wired up), then late-bind agent.run_turn onto the dispatcher.
    dispatcher = Dispatcher(config, resolver=identity_resolver)
    scheduler = Scheduler(
        scheduler_yaml=config.home / "scheduler.yaml",
        enqueue=dispatcher.enqueue,
        home=config.home,
        scheduler_tz=config.scheduler_tz,
    )
    # Commitments store — Phase 2b due-check poller + the four
    # commitment_* langchain tools both need this. Pre-fix it was
    # never constructed, so getattr(agent, "_commitments", None) was
    # always None and every commitment tool returned the "no store"
    # error. Build once, hand to Agent + the tool registry.
    from .commitments import CommitmentsStore
    commitments_store = CommitmentsStore(path=config.commitments_log)

    # chainlink #583 slice 1: one live turn-event bus shared by the agent
    # (producer) and the web SSE layer (consumer) so the dashboard character
    # animates mid-turn instead of replaying post-hoc from turns.jsonl.
    from .turn_event_bus import TurnEventBus
    turn_event_bus = TurnEventBus()

    agent = Agent(
        config,
        turn_logger,
        message_buffer,
        indexes,
        indexer=indexer,
        saga_client=saga_client,
        session_manager=sessions,
        scheduler=scheduler,
        subagent_inbox=inbox,
        channel_registry=channels,
        dispatcher=dispatcher,
        commitments_store=commitments_store,
        turn_event_bus=turn_event_bus,
    )
    dispatcher.set_run_turn(agent.run_turn)
    # chainlink #376 (PR 4): record mid-turn injected messages in chat history at
    # inject time so they thread chronologically with the turn's mid-flight replies.
    dispatcher.set_on_inject(agent.on_message_injected)

    # First-contact DM-channel capture: the first time a user messages us on a
    # bridge, resolve their DM channel from that bridge and cache it in
    # identities.yaml so the agent can DM them by name later (and so it shows
    # up in the channel registry + identity context). Fire-and-forget via the
    # dispatcher's per-event observer — never blocks or fails a turn.
    #
    # Write coordination lives in the writer: ``capture_dm_channel`` shares
    # ``_IDENTITIES_WRITE_LOCK`` with the scheduled populator (``merge_into_yaml``)
    # so the read-modify-write of identities.yaml is atomic across both — no
    # per-observer lock needed here.
    async def _capture_dm_channel(event: AgentEvent) -> None:
        try:
            author = (event.author or "").strip()
            author_id = (event.author_id or "").strip()
            platform = (event.source or "").strip()
            if not (author and author_id and platform in ("slack", "discord")):
                return
            # Cheap in-memory gate — first-contact only; no API call once cached.
            if identity_resolver.dm_channel(author, platform):
                return
            bridge = channels.find(event.channel_id)
            if bridge is None:
                return
            dm_id = await bridge.resolve_dm_channel(author_id)
            if not dm_id:
                return
            from .identities_populator import capture_dm_channel
            wrote = await asyncio.to_thread(
                capture_dm_channel, config.home, author, platform, dm_id
            )
            if wrote:
                await asyncio.to_thread(identity_resolver.reload)
                await log_event(
                    "dm_channel_captured",
                    channel_id=event.channel_id,
                    author=author,
                    platform=platform,
                    dm_channel=dm_id,
                )
        except Exception:  # noqa: BLE001 — best-effort; never disrupt inbound
            log.debug("dm-channel capture failed", exc_info=True)

    dispatcher.set_on_event(_capture_dm_channel)

    async def _request_dm_pairing(event: AgentEvent, decision) -> None:
        try:
            author = (event.author or "").strip()
            platform = (event.source or "").strip()
            channel_id = (event.channel_id or "").strip()
            is_dm = channel_id.startswith("dm-")
            if not (
                author
                and platform in ("slack", "discord")
                and channel_id
            ):
                return
            from .identities_populator import request_pairing_status
            status = await asyncio.to_thread(
                request_pairing_status,
                config.home,
                author,
                platform,
                channel_id=channel_id,
                author_display=event.author_display,
                is_dm=is_dm,
                max_pending=config.pairing_pending_max,
            )
            delivery = "dm" if is_dm else "public_shared_channel"
            if status == "capped":
                await log_event(
                    "pairing_pending_cap_reached",
                    channel_id=event.channel_id,
                    author=author,
                    author_id=event.author_id,
                    platform=platform,
                    delivery=delivery,
                    max_pending=config.pairing_pending_max,
                    reason=getattr(decision, "denial_reason", None),
                )
                await pairing_notifier.notify_pending_cap_reached(
                    platform=platform,
                    channel_id=channel_id,
                    delivery=delivery,
                )
                return
            if status != "changed":
                return
            await asyncio.to_thread(identity_resolver.reload)
            canonical = (getattr(decision, "canonical_author", None) or author).strip()
            await log_event(
                "pairing_requested",
                channel_id=event.channel_id,
                author=author,
                author_id=event.author_id,
                canonical_author=canonical,
                platform=platform,
                delivery=delivery,
                reason=getattr(decision, "denial_reason", None),
            )
            if is_dm:
                await log_event(
                    "dm_pairing_requested",
                    channel_id=event.channel_id,
                    author=author,
                    author_id=event.author_id,
                    canonical_author=canonical,
                    platform=platform,
                    dm_channel=channel_id,
                    reason=getattr(decision, "denial_reason", None),
                )
            await pairing_notifier.notify_operator(
                canonical=canonical,
                display=event.author_display or author,
                platform=platform,
                channel_id=channel_id,
                delivery=delivery,
            )
            if is_dm:
                await pairing_notifier.maybe_reply_dm(
                    canonical=canonical,
                    dm_channel_id=channel_id,
                )
        except Exception:  # noqa: BLE001 — best-effort; never disrupt inbound
            log.debug("dm-pairing request failed", exc_info=True)

    dispatcher.set_on_pairing_required(_request_dm_pairing)

    # Wire dep-injection setters on the production tool surface so
    # langchain @tool functions can reach the same singletons the SDK
    # tool builders received as args. Each setter is idempotent and
    # process-scoped (module-level state). Memory-client injection is
    # handled inside Agent.__init__ (it requires unwrapping the
    # RecordingSagaClient chain), so it's not re-done here.
    from . import tools as _agent_tools
    from .tools import web as _web_tools
    _agent_tools.set_indexer(indexer)
    # Wire the human-readable IndexGenerator (built above) into the
    # rebuild_index tool — without this the tool is dead and always returns
    # "no IndexGenerator configured". Mirrors set_indexer for the search Indexer.
    _agent_tools.set_index_generator(indexes)
    _agent_tools.set_turns_log_path(config.turns_log)
    _agent_tools.set_channel_registry(channels)
    _agent_tools.set_identity_resolver(identity_resolver)
    _agent_tools.set_dispatcher(dispatcher)
    _agent_tools.set_scheduler(scheduler)
    # Worklink slice-3 (#444): give the in-turn ``worklink_run`` tool the
    # HomeostaticArbiter so autonomous dispatch sheds under resource pressure
    # (TIGHT). The operator CLI never sets this, so ``mimir worklink run``
    # stays un-gated by design.
    _agent_tools.set_arbiter(agent._arbiter)
    # Register the process's MessageBuffer globally so the
    # ``send_message`` tool can append outbound replies. Restored
    # after PR #181 (deepagents migration) lost the inline
    # ``buffer.append`` calls from the SDK-era pre/post hooks.
    from .history import set_global_buffer
    set_global_buffer(message_buffer)
    # Pre-fix these setters existed but weren't called from build_app,
    # so the four commitment_* tools all returned "no store" and
    # spawn_claude_code had no resolved config. Wired now.
    _agent_tools.set_commitments_store(commitments_store)
    # spawn_claude_code reads ``.get("default_cwd")`` from this dict.
    # The current Config dataclass doesn't fit that shape; we pass a
    # purpose-built mapping rather than refactor the tool to accept
    # the full Config. Keep this in sync if spawn_claude_code grows
    # additional knobs.
    _agent_tools.set_spawn_config({"default_cwd": config.home})
    # Async shell-job tools (bash_async / bash_jobs_list / bash_job_output)
    # share the Agent's ShellJobRegistry; the on_complete bridge fires
    # ``shell_job_complete`` AgentEvents back through the dispatcher
    # so the spawning channel wakes when the subprocess exits.
    _agent_tools.set_shell_job_registry(
        agent._shell_jobs,
        on_complete=agent._handle_shell_job_complete,
    )
    # fetch_url caches downloaded bodies under <home>/attachments/fetch-cache/.
    # The tool itself is only registered when the active provider isn't
    # claude_code (see all_mimir_tools); set_home is harmless when unused.
    _web_tools.set_home(config.home)

    # WebChatBridge needs the dispatcher (for inbound) — built after dispatcher
    # exists, registered before channels.connect_all() runs at startup.
    web_chat = WebChatBridge(enqueue=dispatcher.enqueue, home=config.home)
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

    # When a session goes idle, enqueue the synthesis turn through the same
    # dispatcher so it runs in channel-FIFO order alongside any new traffic.
    async def _on_session_idle(session: ChannelSession) -> None:
        synth_event = AgentEvent(
            trigger="saga_session_end",
            channel_id=session.channel_id,
            content="",
            extra={"saga_session_id": session.saga_session_id},
        )
        accepted = await dispatcher.enqueue(synth_event)
        if not accepted:
            await log_event(
                "saga_synthesis_dispatch_failed",
                channel_id=session.channel_id,
                saga_session_id=session.saga_session_id,
                reason="dispatcher_rejected",
            )

    sessions.set_on_idle(_on_session_idle)
    # Busy-defer (SPEC §5.6): when the session timer fires while a turn is
    # in flight or events are queued for the channel, re-arm rather than
    # synthesize behind the in-flight work.
    sessions.set_is_busy(dispatcher.is_channel_busy)

    app["config"] = config
    app["agent"] = agent
    app["dispatcher"] = dispatcher
    app["turn_logger"] = turn_logger
    app["message_buffer"] = message_buffer
    app["indexes"] = indexes
    app["indexer"] = indexer
    app["saga_client"] = saga_client
    app["sessions"] = sessions
    app["scheduler"] = scheduler
    app["subagent_inbox"] = inbox
    app["channels"] = channels
    app["pairing_notifier"] = pairing_notifier
    app["identity_resolver"] = identity_resolver
    app["replayed_messages"] = replayed
    app["aliases_loaded"] = aliases_loaded
    app["seeded_subagents"] = seeded
    app["seeded_skills"] = seeded_skills_map
    app["api_key"] = config.api_key
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
        commitments_store=commitments_store,
        # Collapse the /ops Usage chart to the live subscription provider so
        # stale windows from a prior provider (e.g. Anthropic after a Codex
        # cutover, chainlink #301) don't render a second chart.
        active_usage_provider=active_provider_for_spec(
            config.model_spec,
            getattr(config, "anthropic_base_url", ""),
        ),
        turn_event_bus=turn_event_bus,
    )
    activity_panel = None
    if config.activity_panel_channels:
        from .bridges._activity_panel import ActivityPanel

        activity_panel = ActivityPanel(
            turn_event_bus,
            channels,
            config.activity_panel_channels,
        )
        app["activity_panel"] = activity_panel
    # Web chat bridge — POST /chat + GET /chat/stream for the local UI.
    web_chat.register_routes(app)

    async def _on_startup(app: web.Application) -> None:
        if activity_panel is not None:
            activity_panel.start()
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

        await indexer.start(run_initial_sweep=False, sweep_loop=True)
        await channels.connect_all()

        # MCP servers (opt-in via MIMIR_MCP_SERVERS_JSON / _PATH).
        # Bridged tools are appended to the agent's surface via the
        # mimir.tools.mcp setter. A single server failing to start is
        # logged + skipped — the agent still boots with native tools.
        # Lifecycle owner stored on app so _on_cleanup can shut it down.
        if config.mcp_servers:
            from .mcp_client import MCPManager
            from .tools import set_mcp_tools

            mcp_manager = MCPManager()
            try:
                mcp_tools = await mcp_manager.start_servers(config.mcp_servers)
            except Exception as exc:  # noqa: BLE001 — log + continue
                log.warning("MCP startup failed: %s", exc)
                mcp_tools = []
                await mcp_manager.shutdown()
                mcp_manager = None
            if mcp_tools:
                set_mcp_tools(mcp_tools)
                await log_event(
                    "mcp_servers_ready",
                    count=len(mcp_tools),
                    tool_names=[t.name for t in mcp_tools],
                )
            app["mcp_manager"] = mcp_manager

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

        # Resume Worklink runs orphaned by a restart (#561). The detached
        # controllers died with the old container, but the docker-sibling/ecs
        # workers they launched survive; reattach to them (wait + harvest + open
        # the PR) instead of orphaning the compute and waiting for the reaper.
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
        rate_limit_store = RateLimitStore(
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
            or health_probe_registered
            or scheduler_health_registered
            or identities_populate_registered
            or reload_stats["registered"] > 0
            or installed_pollers > 0
        ):
            scheduler.start()

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
            _STARTUP_BACKGROUND_TASKS,
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
                    _STARTUP_BACKGROUND_TASKS,
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
                _STARTUP_BACKGROUND_TASKS,
                liveness_beat_loop(
                    config.home,
                    interval=config.liveness_beat_seconds,
                    started_at=time.time(),
                ),
                name="mimir-liveness-beat",
            )

    async def _on_cleanup(app: web.Application) -> None:
        # Mark this stop as clean as the VERY FIRST action — before ANY await.
        # Reaching _on_cleanup means we received SIGTERM/SIGINT and are tearing
        # down in order (an intended stop). mark_clean_shutdown is a fast, sync,
        # local file write; doing it ahead of `await log_event` and the drain
        # means a stalled await during an active turn can't let the
        # stop_grace_period expire (SIGKILL) before the clean marker lands. That
        # ordering bug produced spurious "unclean restart" pages on deploy
        # recreates of a busy agent (muninn). A hard kill never reaches here, so
        # its marker stays clean=false (→ unclean-restart notice on next boot).
        # chainlink #507.
        from .liveness import mark_clean_shutdown
        mark_clean_shutdown(config.home)
        await log_event("shutdown", reason="cleanup")
        # chainlink #510: bounded graceful drain — finish in-flight turns up to
        # the configured timeout, then exit. Keeps a deploy SIGTERM from killing
        # live turns while staying within the compose stop_grace_period.
        await dispatcher.drain(timeout=config.drain_timeout_seconds)
        scheduler.stop()
        await sessions.shutdown()
        await indexer.stop()
        await saga_client.close()
        panel = app.get("activity_panel")
        if panel is not None:
            await panel.stop()
        await channels.disconnect_all()
        mcp_manager = app.get("mcp_manager")
        if mcp_manager is not None:
            await mcp_manager.shutdown()

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

    Loopback binds (``127.0.0.1``, ``::1``, ``localhost``) are
    allowed without an API key — the safe local-dev posture. Any
    other host requires ``MIMIR_API_KEY`` to be set.
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
