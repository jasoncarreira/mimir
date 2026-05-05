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
import signal
from typing import Any

from aiohttp import web

from .agent import Agent
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
from .saga_client import SagaClient, make_saga_client
from .scheduler import Scheduler
from .search import Indexer
from .session_manager import ChannelSession, SessionManager
from .skill_defs import seed_skills
from .subagent_defs import seed_subagent_defs
from .subagent_inbox import SubagentInbox
from .turn_logger import TurnLogger
from . import web_ui

log = logging.getLogger(__name__)


async def _handle_event(request: web.Request) -> web.Response:
    # Auth gate: when MIMIR_API_KEY is set, the request must carry a
    # matching X-API-Key header. The server binds to 0.0.0.0 and the
    # /event payload accepts arbitrary trigger strings, so an attacker
    # who can reach the port without auth can steer the agent into the
    # synthesis path against an unrelated session and call
    # saga_end_session. Empty key = no auth (dev / localhost-only).
    expected_key = request.app.get("api_key") or ""
    if expected_key:
        provided = request.headers.get("X-API-Key", "")
        if not _safe_str_eq(provided, expected_key):
            return web.json_response(
                {"error": "unauthorized"}, status=401,
            )

    try:
        body: dict[str, Any] = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    channel_id = body.get("channel_id")
    if not channel_id:
        return web.json_response({"error": "channel_id required"}, status=400)

    event = AgentEvent(
        trigger=body.get("trigger", "user_message"),
        channel_id=channel_id,
        content=body.get("content", ""),
        author=body.get("author"),
        author_id=body.get("author_id"),
        source_id=body.get("source_id"),
        source=body.get("source"),
        attachment_names=body.get("attachment_names") or [],
        extra=body.get("extra") or {},
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


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_app(config: Config) -> web.Application:
    # 10MB body cap (aiohttp default is 1MB). Mimir takes JSON-only bodies on
    # /event and /chat — long bluesky transcripts and seed payloads can run
    # well past 1MB. Bridges read attachment bytes from disk via filesystem
    # paths (``attachment_names``), not from the request body, so the cap
    # doesn't need to accommodate binary uploads.
    app = web.Application(client_max_size=10 * 1024 * 1024)

    config.logs_dir.mkdir(parents=True, exist_ok=True)
    (config.home / "memory" / "core").mkdir(parents=True, exist_ok=True)
    (config.home / "memory" / "channels").mkdir(parents=True, exist_ok=True)
    (config.home / "memory" / "shared").mkdir(parents=True, exist_ok=True)
    (config.home / "state").mkdir(parents=True, exist_ok=True)
    (config.home / "messages").mkdir(parents=True, exist_ok=True)
    (config.home / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
    seeded = seed_subagent_defs(config.home)
    seeded_skills_map = seed_skills(config.home)

    init_logger(config.events_log, make_process_session_id(), max_events=config.max_events_kept)
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
    saga_client = make_saga_client(
        endpoint=config.saga_endpoint,
        api_key=config.saga_api_key or None,
    )
    sessions = SessionManager(idle_minutes=config.saga_session_idle_minutes)
    inbox = SubagentInbox()

    # Channel layer (SPEC §7.2). BenchBridge always registers — it's how the
    # benchmark adapter gets outbound. WebChatBridge registers if a
    # web_chat-friendly aiohttp app is hosting us; routes mount below in
    # _on_startup. Discord / Slack / Bluesky bridges register based on env
    # tokens (DISCORD_TOKEN etc.).
    channels = ChannelRegistry()
    channels.register(BenchBridge(home=config.home))

    # Wiring order to break the (dispatcher → agent → scheduler → dispatcher)
    # cycle: dispatcher first with no runner, then scheduler bound to its
    # enqueue, then agent (which builds the MCP server with all of them
    # wired up), then late-bind agent.run_turn onto the dispatcher.
    dispatcher = Dispatcher(config)
    scheduler = Scheduler(
        scheduler_yaml=config.home / "scheduler.yaml",
        enqueue=dispatcher.enqueue,
        home=config.home,
    )
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
    )
    dispatcher.set_run_turn(agent.run_turn)

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
    app["identity_resolver"] = identity_resolver
    app["replayed_messages"] = replayed
    app["aliases_loaded"] = aliases_loaded
    app["seeded_subagents"] = seeded
    app["seeded_skills"] = seeded_skills_map
    app["api_key"] = config.api_key

    if not config.api_key:
        log.warning(
            "MIMIR_API_KEY is unset — POST /event accepts unauthenticated "
            "requests. Set the env var before exposing the port beyond "
            "localhost."
        )

    app.router.add_post("/event", _handle_event)
    app.router.add_get("/health", _handle_health)
    # Turn viewer + log API (SPEC §11).
    web_ui.register_routes(app, turns_log=config.turns_log, events_log=config.events_log)
    # Web chat bridge — POST /chat + GET /chat/stream for the local UI.
    web_chat.register_routes(app)

    async def _on_startup(app: web.Application) -> None:
        await indexer.start(run_initial_sweep=False, sweep_loop=True)
        await channels.connect_all()

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

        # Plan-window capture: per-turn call to
        # ``ClaudeSDKClient.get_context_usage()`` from inside ``run_turn``
        # populates Max-plan window utilization in rate_limits.json.
        # Stage 5 of CLAUDE_SDK_CLIENT_MIGRATION.md retired the cron-
        # based ``quota_poller`` once the agent loop owned a persistent
        # client that could be queried for free at end-of-turn.

        # Load LLM-tick jobs from scheduler.yaml.
        reload_stats = scheduler.reload()

        if (
            consolidate_registered
            or introspection_registered
            or reload_stats["registered"] > 0
        ):
            scheduler.start()

        await log_event(
            "app_started",
            home=str(config.home),
            web_port=config.web_port,
            replayed_messages=replayed,
            saga_endpoint=config.saga_endpoint,
            saga_consolidate_cron=config.saga_consolidate_cron if consolidate_registered else "",
            saga_session_idle_minutes=config.saga_session_idle_minutes,
            seeded_subagents=seeded,
            seeded_skills=seeded_skills_map,
            scheduled_jobs_registered=reload_stats["registered"],
            scheduled_jobs_invalid=reload_stats["invalid"],
        )
        await log_event("api_started", port=config.web_port)
        asyncio.create_task(indexer.sweep())

    async def _on_cleanup(app: web.Application) -> None:
        await log_event("shutdown", reason="cleanup")
        await dispatcher.drain()
        scheduler.stop()
        await sessions.shutdown()
        await indexer.stop()
        await saga_client.close()
        await channels.disconnect_all()
        # Stage 1 of CLAUDE_SDK_CLIENT_MIGRATION.md: release the shared
        # ClaudeSDKClient subprocess on graceful shutdown. No-op if no
        # client was ever connected (test shutdowns, query()-failed
        # bring-up, etc.).
        from .agent import shutdown_sdk_client
        await shutdown_sdk_client()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    app = build_app(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host="0.0.0.0", port=config.web_port)
    loop.run_until_complete(site.start())
    log.info("mimir listening on :%d", config.web_port)

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
