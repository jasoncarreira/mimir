"""HTTP entrypoint + main loop.

Phase 4 surface:
  POST /event   — inject an AgentEvent
  GET  /health  — basic liveness

Wires together: dispatcher, agent, message buffer, index generator, search
indexer, MSAM client, session manager, scheduler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from typing import Any

from aiohttp import web

from .agent import Agent
from .bridges.bench import BenchBridge
from .channel_registry import ChannelRegistry
from .config import Config
from .dispatcher import Dispatcher
from .event_logger import init_logger, log_event
from .history import MessageBuffer
from .index import IndexGenerator
from .models import AgentEvent, make_process_session_id
from .msam_client import MsamClient
from .scheduler import Scheduler
from .search import Indexer
from .session_manager import ChannelSession, SessionManager
from .subagent_defs import seed_subagent_defs
from .subagent_inbox import SubagentInbox
from .turn_logger import TurnLogger

log = logging.getLogger(__name__)


async def _handle_event(request: web.Request) -> web.Response:
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


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_app(config: Config) -> web.Application:
    app = web.Application()

    config.logs_dir.mkdir(parents=True, exist_ok=True)
    (config.home / "memory" / "core").mkdir(parents=True, exist_ok=True)
    (config.home / "memory" / "channels").mkdir(parents=True, exist_ok=True)
    (config.home / "memory" / "shared").mkdir(parents=True, exist_ok=True)
    (config.home / "state").mkdir(parents=True, exist_ok=True)
    (config.home / "messages").mkdir(parents=True, exist_ok=True)
    (config.home / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
    seeded = seed_subagent_defs(config.home)

    init_logger(config.events_log, make_process_session_id(), max_events=config.max_events_kept)
    turn_logger = TurnLogger(config.turns_log, max_turns=config.max_turns_kept)

    history_path = config.home / "messages" / "chat_history.jsonl"
    message_buffer = MessageBuffer(
        history_path=history_path,
        global_max=config.history_global_max,
        per_channel_max=config.history_per_channel_max,
    )
    replayed = message_buffer.replay()

    indexes = IndexGenerator(config.home)
    indexes.mark_dirty("all")

    indexer = Indexer(config.home)
    msam_client = MsamClient(
        endpoint=config.msam_endpoint,
        api_key=config.msam_api_key or None,
    )
    sessions = SessionManager(idle_minutes=config.msam_session_idle_minutes)
    inbox = SubagentInbox()

    # Channel layer (SPEC §7.2). Phase 6.3 ships the BenchBridge by default
    # so the benchmark adapter has a working outbound surface; Slack/Discord/
    # Bluesky/WebUI bridges register here in later batches.
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
    )
    agent = Agent(
        config,
        turn_logger,
        message_buffer,
        indexes,
        indexer=indexer,
        msam_client=msam_client,
        session_manager=sessions,
        scheduler=scheduler,
        subagent_inbox=inbox,
        channel_registry=channels,
    )
    dispatcher.set_run_turn(agent.run_turn)

    # When a session goes idle, enqueue the synthesis turn through the same
    # dispatcher so it runs in channel-FIFO order alongside any new traffic.
    async def _on_session_idle(session: ChannelSession) -> None:
        synth_event = AgentEvent(
            trigger="msam_session_end",
            channel_id=session.channel_id,
            content="",
            extra={"msam_session_id": session.msam_session_id},
        )
        accepted = await dispatcher.enqueue(synth_event)
        if not accepted:
            await log_event(
                "msam_synthesis_dispatch_failed",
                channel_id=session.channel_id,
                msam_session_id=session.msam_session_id,
                reason="dispatcher_rejected",
            )

    sessions.set_on_idle(_on_session_idle)

    app["config"] = config
    app["agent"] = agent
    app["dispatcher"] = dispatcher
    app["turn_logger"] = turn_logger
    app["message_buffer"] = message_buffer
    app["indexes"] = indexes
    app["indexer"] = indexer
    app["msam_client"] = msam_client
    app["sessions"] = sessions
    app["scheduler"] = scheduler
    app["subagent_inbox"] = inbox
    app["channels"] = channels
    app["replayed_messages"] = replayed
    app["seeded_subagents"] = seeded

    app.router.add_post("/event", _handle_event)
    app.router.add_get("/health", _handle_health)

    async def _on_startup(app: web.Application) -> None:
        await indexer.start(run_initial_sweep=False, sweep_loop=True)
        await channels.connect_all()

        # Register MSAM weekly consolidation. Bad cron logs and continues.
        try:
            consolidate_registered = scheduler.add_msam_consolidate_job(
                msam_client, config.msam_consolidate_cron
            )
        except ValueError as exc:
            await log_event("scheduler_invalid_cron", error=str(exc))
            consolidate_registered = False

        # Load LLM-tick jobs from scheduler.yaml.
        reload_stats = scheduler.reload()

        if consolidate_registered or reload_stats["registered"] > 0:
            scheduler.start()

        await log_event(
            "app_started",
            home=str(config.home),
            web_port=config.web_port,
            replayed_messages=replayed,
            msam_endpoint=config.msam_endpoint,
            msam_consolidate_cron=config.msam_consolidate_cron if consolidate_registered else "",
            msam_session_idle_minutes=config.msam_session_idle_minutes,
            seeded_subagents=seeded,
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
        await msam_client.close()
        await channels.disconnect_all()

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
