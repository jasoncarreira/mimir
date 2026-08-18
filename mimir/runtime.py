from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .agent import Agent
    from .channel_registry import ChannelRegistry
    from .chat_skills import ChatSkillRegistry
    from .commitments import CommitmentsStore
    from .config import Config
    from .dispatcher import Dispatcher
    from .history import MessageBuffer
    from .identities import IdentityResolver
    from .index import IndexGenerator
    from .saga_client import SagaClient
    from .scheduler import Scheduler
    from .search import Indexer
    from .session_manager import SessionManager
    from .turn_event_bus import TurnEventBus
    from .turn_logger import TurnLogger

log = logging.getLogger(__name__)

RUNTIME_RESOURCE_CLOSE_TIMEOUT_SECONDS = 10.0
GITHUB_IDENTITY_RETRY_INTERVAL_SECONDS = 60.0
GITHUB_IDENTITY_TRANSIENT_ALERT_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class CoreServices:
    identity_resolver: IdentityResolver
    aliases_loaded: int
    saga_db_path: Path
    chat_skill_registry: ChatSkillRegistry


class PairingNotifier(Protocol):
    async def notify_operator(
        self,
        *,
        canonical: str,
        display: str,
        platform: str,
        channel_id: str,
        delivery: str,
    ) -> None: ...

    async def notify_pending_cap_reached(
        self,
        *,
        platform: str,
        channel_id: str,
        delivery: str,
    ) -> None: ...

    async def maybe_reply_dm(
        self,
        *,
        canonical: str,
        dm_channel_id: str,
    ) -> None: ...


BackgroundTaskSpawner = Callable[
    [Coroutine[Any, Any, None], str],
    asyncio.Task[Any],
]


@dataclass(frozen=True, slots=True)
class RuntimeAdapters:
    dispatcher: Dispatcher
    scheduler: Scheduler
    channels: ChannelRegistry
    pairing_notifier: PairingNotifier
    spawn_background_task: BackgroundTaskSpawner


@dataclass(slots=True)
class AgentRuntimeBundle:
    config: Config
    core: CoreServices
    adapters: RuntimeAdapters
    agent: Agent
    turn_logger: TurnLogger
    message_buffer: MessageBuffer
    indexes: IndexGenerator
    indexer: Indexer
    saga_client: SagaClient
    sessions: SessionManager
    commitments_store: CommitmentsStore
    turn_event_bus: TurnEventBus
    replayed_messages: int
    migrated_commitments: int
    _owned_closers: list[tuple[str, Callable[[], Awaitable[None]]]]
    _runtime_background_tasks: set[asyncio.Task[Any]]
    _close_task: asyncio.Task[None] | None

    def install_mcp_tools(self, tools: list[Any]) -> None:
        from .tools import set_mcp_tools

        set_mcp_tools(tools)

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                _close_bundle(self),
                name="agent-runtime-close",
            )
        await asyncio.shield(self._close_task)


def create_core_services(config: Config) -> CoreServices:
    from .chat_skills import ChatSkillRegistry
    from .identities import IdentityResolver

    identity_resolver = IdentityResolver(home=config.home)
    aliases_loaded = identity_resolver.reload()

    home_saga_toml = config.home / "saga.toml"
    if home_saga_toml.is_file() and not os.environ.get("SAGA_CONFIG"):
        os.environ["SAGA_CONFIG"] = str(home_saga_toml)

    from .saga._config_io import get_config as get_saga_config

    configured_db_path = get_saga_config()("storage", "db_path", "saga.db")
    saga_db_path = Path(configured_db_path)
    if not saga_db_path.is_absolute():
        saga_db_path = config.home / ".mimir" / saga_db_path

    chat_skill_registry = ChatSkillRegistry.from_config(config)
    return CoreServices(
        identity_resolver=identity_resolver,
        aliases_loaded=aliases_loaded,
        saga_db_path=saga_db_path,
        chat_skill_registry=chat_skill_registry,
    )


async def create_agent_runtime(
    config: Config,
    core: CoreServices,
    adapters: RuntimeAdapters,
) -> AgentRuntimeBundle:
    _validate_adapters(adapters)
    runtime_background_tasks: set[asyncio.Task[Any]] = set()
    owned_closers: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    sessions: SessionManager | None = None

    try:
        _clear_runtime_globals()

        from .tools import all_mimir_tools
        from .tools.forge import (
            github_identity_recovery_pending,
            initialize_github_forge_identity,
            set_github_identity_degraded_callback,
        )

        degradation_recorded = False
        degradation_alert_scheduled = False
        transient_failures = 0
        next_identity_retry_at = 0.0
        runtime_loop = asyncio.get_running_loop()

        def github_identity_degraded(exc: Exception) -> None:
            nonlocal degradation_recorded, degradation_alert_scheduled
            nonlocal transient_failures, next_identity_retry_at
            from .event_logger import log_event, log_event_sync
            from .forge.github import GitHubIdentityFailureKind

            declared_login = getattr(exc, "declared_login", "")
            authenticated_login = getattr(exc, "authenticated_login", "")
            transient = (
                getattr(exc, "failure_kind", None)
                == GitHubIdentityFailureKind.TRANSIENT
            )
            if transient:
                transient_failures += 1
                next_identity_retry_at = time.monotonic() + GITHUB_IDENTITY_RETRY_INTERVAL_SECONDS
            if not degradation_recorded:
                degradation_recorded = True
                log.warning(
                    "github_identity_degraded: authenticated=%r declared=%r; coding disabled%s",
                    authenticated_login or "unknown",
                    declared_login or "unknown",
                    " pending forge recovery" if transient else " until restart",
                )
                log_event_sync(
                    "github_identity_degraded",
                    declared_login=declared_login or None,
                    authenticated_login=authenticated_login or None,
                    reason=str(exc),
                )
            alert_channel = (config.operator_alert_channel or "").strip()
            if (
                not alert_channel
                or degradation_alert_scheduled
                or (transient and transient_failures < GITHUB_IDENTITY_TRANSIENT_ALERT_ATTEMPTS)
            ):
                return
            degradation_alert_scheduled = True
            text = (
                "GitHub identity verification failed; coding capability is disabled"
                + (
                    " while the forge remains unavailable. "
                    if transient
                    else " until the process is restarted with corrected credentials. "
                )
                + f"Authenticated login: {authenticated_login or 'unknown'}; "
                f"declared login: {declared_login or 'unknown'}."
            )

            async def alert() -> None:
                try:
                    await adapters.channels.send(alert_channel, text, final=True)
                    await log_event(
                        "github_identity_degraded_operator_alert_sent",
                        channel_id=alert_channel,
                    )
                except Exception as alert_exc:
                    log.warning("github identity degraded alert failed: %s", alert_exc)
                    await log_event(
                        "github_identity_degraded_operator_alert_failed",
                        channel_id=alert_channel,
                        error=str(alert_exc)[:500],
                    )

            def schedule_alert() -> None:
                alert_coroutine = alert()
                try:
                    task = adapters.spawn_background_task(
                        alert_coroutine,
                        "github-identity-degraded-alert",
                    )
                except BaseException:
                    alert_coroutine.close()
                    raise
                runtime_background_tasks.add(task)
                task.add_done_callback(runtime_background_tasks.discard)

            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is runtime_loop:
                schedule_alert()
            else:
                runtime_loop.call_soon_threadsafe(schedule_alert)

        set_github_identity_degraded_callback(
            github_identity_degraded,
            notify_current=True,
        )

        coding_enabled = getattr(config, "coding_enabled", False)
        if coding_enabled:
            coding_enabled = initialize_github_forge_identity()
        all_mimir_tools(coding_enabled=coding_enabled)

        from .agent import Agent
        from .commitments import CommitmentsStore
        from .history import MessageBuffer
        from .index import IndexGenerator
        from .saga_client import make_saga_client
        from .search import Indexer
        from .session_manager import SessionManager
        from .turn_event_bus import TurnEventBus
        from .turn_logger import TurnLogger

        turn_logger = TurnLogger(config.turns_log, max_turns=config.max_turns_kept)

        message_buffer = MessageBuffer(
            history_path=config.home / "messages" / "chat_history.jsonl",
            global_max=config.history_global_max,
            per_channel_max=config.history_per_channel_max,
            resolver=core.identity_resolver,
            cross_platform_pull=config.cross_platform_pull,
        )
        replayed_messages = message_buffer.replay()

        indexes = IndexGenerator(config.home)
        indexes.mark_dirty("all")

        saga_client = make_saga_client(db_path=core.saga_db_path)
        owned_closers.append(("saga client", saga_client.close))

        indexer = Indexer(config.home)
        owned_closers.append(("indexer", indexer.stop))

        sessions = SessionManager(
            idle_minutes=config.saga_session_idle_minutes,
            max_turns=config.saga_session_max_turns,
        )
        owned_closers.append(("sessions", sessions.shutdown))

        commitments_store = CommitmentsStore(
            path=config.commitments_log,
            provenance_db_path=core.saga_db_path,
        )
        migrated_commitments = commitments_store.migrate_ownership()
        if migrated_commitments:
            log.info(
                "backfilled ownership for %d commitment records",
                migrated_commitments,
            )

        turn_event_bus = TurnEventBus()

        agent = Agent(
            config,
            turn_logger,
            message_buffer,
            indexes,
            indexer=indexer,
            saga_client=saga_client,
            session_manager=sessions,
            scheduler=adapters.scheduler,
            channel_registry=adapters.channels,
            dispatcher=adapters.dispatcher,
            commitments_store=commitments_store,
            turn_event_bus=turn_event_bus,
            chat_skill_registry=core.chat_skill_registry,
        )

        bundle = AgentRuntimeBundle(
            config=config,
            core=core,
            adapters=adapters,
            agent=agent,
            turn_logger=turn_logger,
            message_buffer=message_buffer,
            indexes=indexes,
            indexer=indexer,
            saga_client=saga_client,
            sessions=sessions,
            commitments_store=commitments_store,
            turn_event_bus=turn_event_bus,
            replayed_messages=replayed_messages,
            migrated_commitments=migrated_commitments,
            _owned_closers=owned_closers,
            _runtime_background_tasks=runtime_background_tasks,
            _close_task=None,
        )

        def on_channel_idle(channel_id: str) -> bool:
            return message_buffer.evict_channel(channel_id)

        async def capture_dm_channel(event: Any) -> None:
            try:
                author = (event.author or "").strip()
                author_id = (event.author_id or "").strip()
                platform = (event.source or "").strip()
                if not (author and author_id and platform in ("slack", "discord")):
                    return
                if core.identity_resolver.dm_channel(author, platform):
                    return
                bridge = adapters.channels.find(event.channel_id)
                if bridge is None:
                    return
                dm_id = await bridge.resolve_dm_channel(author_id)
                if not dm_id:
                    return
                from .event_logger import log_event
                from .identities_populator import capture_dm_channel as persist_dm_channel

                wrote = await asyncio.to_thread(
                    persist_dm_channel,
                    config.home,
                    author,
                    platform,
                    dm_id,
                )
                if wrote:
                    await asyncio.to_thread(core.identity_resolver.reload)
                    await log_event(
                        "dm_channel_captured",
                        channel_id=event.channel_id,
                        author=author,
                        platform=platform,
                        dm_channel=dm_id,
                    )
            except Exception:
                log.debug("dm-channel capture failed", exc_info=True)

        async def request_dm_pairing(event: Any, decision: Any) -> None:
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
                from .event_logger import log_event
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
                    await adapters.pairing_notifier.notify_pending_cap_reached(
                        platform=platform,
                        channel_id=channel_id,
                        delivery=delivery,
                    )
                    return
                if status != "changed":
                    return
                await asyncio.to_thread(core.identity_resolver.reload)
                canonical = (
                    getattr(decision, "canonical_author", None) or author
                ).strip()
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
                await adapters.pairing_notifier.notify_operator(
                    canonical=canonical,
                    display=event.author_display or author,
                    platform=platform,
                    channel_id=channel_id,
                    delivery=delivery,
                )
                if is_dm:
                    await adapters.pairing_notifier.maybe_reply_dm(
                        canonical=canonical,
                        dm_channel_id=channel_id,
                    )
            except Exception:
                log.debug("dm-pairing request failed", exc_info=True)

        async def on_session_idle(session: Any) -> None:
            from .access_control import builtin_trigger_service_principal
            from .event_logger import log_event
            from .models import AgentEvent

            authority = builtin_trigger_service_principal("session-boundary", Path("."))
            synth_event = AgentEvent(
                trigger="saga_session_end",
                channel_id=session.channel_id,
                service_principal="synthesis",
                service_authority=authority,
                content="",
                extra={"saga_session_id": session.saga_session_id},
                source_session_acl=session.source_acl,
                ifc_labels=session.ifc_state.current(),
            )
            accepted = await adapters.dispatcher.enqueue(synth_event)
            if not accepted:
                await log_event(
                    "saga_synthesis_dispatch_failed",
                    channel_id=session.channel_id,
                    saga_session_id=session.saga_session_id,
                    reason="dispatcher_rejected",
                )

        on_injected = agent.on_message_injected
        is_busy = adapters.dispatcher.is_channel_busy

        adapters.dispatcher.set_on_channel_idle(on_channel_idle)
        adapters.dispatcher.set_on_inject(on_injected)
        adapters.dispatcher.set_on_event(capture_dm_channel)
        adapters.dispatcher.set_on_pairing_required(request_dm_pairing)
        sessions.set_on_idle(on_session_idle)
        sessions.set_is_busy(is_busy)

        _install_runtime_globals(bundle)
    except BaseException as original_exception:
        cleanup_errors = await _cleanup_runtime(
            adapters=adapters,
            sessions=sessions,
            runtime_background_tasks=runtime_background_tasks,
            owned_closers=owned_closers,
        )
        if cleanup_errors:
            _log_cleanup_errors(cleanup_errors)
            original_exception.add_note(_cleanup_note(cleanup_errors))
        raise

    identity_retry_lock = asyncio.Lock()

    async def run_turn_with_identity_preflight(event: Any) -> Any:
        nonlocal next_identity_retry_at
        if (
            getattr(config, "coding_enabled", False)
            and github_identity_recovery_pending()
            and time.monotonic() >= next_identity_retry_at
        ):
            async with identity_retry_lock:
                if (
                    github_identity_recovery_pending()
                    and time.monotonic() >= next_identity_retry_at
                ):
                    recovered = await asyncio.to_thread(
                        initialize_github_forge_identity
                    )
                    if recovered:
                        from .event_logger import log_event

                        await log_event(
                            "github_identity_recovered",
                            attempts=transient_failures,
                        )
        return await agent.run_turn(event)

    run_turn_with_identity_preflight.__wrapped__ = agent.run_turn  # type: ignore[attr-defined]
    adapters.dispatcher.set_run_turn(run_turn_with_identity_preflight)
    return bundle


def _validate_adapters(adapters: RuntimeAdapters) -> None:
    dispatcher = adapters.dispatcher
    if getattr(dispatcher, "_run_turn", None) is not None:
        raise ValueError("RuntimeAdapters.dispatcher must have run_turn=None")
    callback_names = (
        "_on_channel_idle",
        "_on_inject",
        "_on_event",
        "_on_pairing_required",
    )
    if any(getattr(dispatcher, name, None) is not None for name in callback_names):
        raise ValueError("RuntimeAdapters.dispatcher must not have runtime callbacks")
    scheduler = adapters.scheduler
    scheduler_started = bool(getattr(scheduler, "_started", False))
    scheduler_impl = getattr(scheduler, "_scheduler", None)
    if scheduler_started or bool(getattr(scheduler_impl, "running", False)):
        raise ValueError("RuntimeAdapters.scheduler must not have been started")


def _install_runtime_globals(bundle: AgentRuntimeBundle) -> None:
    from . import tools
    from .history import set_global_buffer
    from .tools import web as web_tools

    tools.set_indexer(bundle.indexer)
    tools.set_index_generator(bundle.indexes)
    tools.set_turns_log_path(bundle.config.turns_log)
    tools.set_channel_registry(bundle.adapters.channels)
    tools.set_identity_resolver(bundle.core.identity_resolver)
    tools.set_dispatcher(bundle.adapters.dispatcher)
    tools.set_scheduler(bundle.adapters.scheduler)
    tools.set_arbiter(bundle.agent._arbiter)
    set_global_buffer(bundle.message_buffer)
    tools.set_commitments_store(bundle.commitments_store)
    tools.set_spawn_config(
        {
            "default_cwd": bundle.config.home,
            "opencode_config_path": bundle.config.opencode_config_path,
        }
    )
    tools.set_shell_job_registry(
        bundle.agent._shell_jobs,
        on_complete=bundle.agent._handle_shell_job_complete,
    )
    web_tools.set_home(bundle.config.home)
    tools.set_mcp_tools([])


def _clear_runtime_globals() -> None:
    from . import tools
    from .history import set_global_buffer
    from .tools import web as web_tools
    from .tools.forge import (
        set_forge_client,
        set_github_identity_degraded_callback,
    )

    resetters: tuple[Callable[[], None], ...] = (
        lambda: set_github_identity_degraded_callback(None),
        lambda: set_forge_client(None),
        lambda: tools.set_memory_client(None),
        lambda: tools.set_indexer(None),
        lambda: tools.set_index_generator(None),
        lambda: tools.set_turns_log_path(None),
        lambda: tools.set_channel_registry(None),
        lambda: tools.set_identity_resolver(None),
        lambda: tools.set_dispatcher(None),
        lambda: tools.set_scheduler(None),
        lambda: tools.set_arbiter(None),
        lambda: set_global_buffer(None),
        lambda: tools.set_commitments_store(None),
        lambda: tools.set_spawn_config(None),
        lambda: tools.set_shell_job_registry(None),
        lambda: web_tools.set_home(None),
        lambda: tools.set_mcp_tools([]),
    )
    errors = _run_sync_cleanup(resetters)
    if errors:
        raise ExceptionGroup("agent runtime global reset failed", errors)


async def _close_bundle(bundle: AgentRuntimeBundle) -> None:
    errors = await _cleanup_runtime(
        adapters=bundle.adapters,
        sessions=bundle.sessions,
        runtime_background_tasks=bundle._runtime_background_tasks,
        owned_closers=bundle._owned_closers,
    )
    if errors:
        _log_cleanup_errors(errors)
        raise ExceptionGroup("agent runtime cleanup failed", errors)


async def _cleanup_runtime(
    *,
    adapters: RuntimeAdapters,
    sessions: SessionManager | None,
    runtime_background_tasks: set[asyncio.Task[Any]],
    owned_closers: list[tuple[str, Callable[[], Awaitable[None]]]],
) -> list[Exception]:
    errors: list[Exception] = []
    dispatcher = adapters.dispatcher
    callback_resetters: list[Callable[[], None]] = [
        lambda: dispatcher.set_run_turn(None),
        lambda: dispatcher.set_on_channel_idle(None),
        lambda: dispatcher.set_on_inject(None),
        lambda: dispatcher.set_on_event(None),
        lambda: dispatcher.set_on_pairing_required(None),
    ]
    if sessions is not None:
        callback_resetters.extend(
            (
                lambda: sessions.set_on_idle(None),
                lambda: sessions.set_is_busy(None),
            )
        )
    callback_resetters.append(lambda: adapters.scheduler.set_arbiter(None))
    errors.extend(_run_sync_cleanup(tuple(callback_resetters)))

    from .background_tasks import cancel_background_tasks

    try:
        task_errors = await cancel_background_tasks(
            runtime_background_tasks,
            label="agent runtime",
        )
    except BaseException as exc:
        errors.append(_cleanup_exception(exc))
    else:
        errors.extend(_cleanup_exception(exc) for exc in task_errors)

    try:
        _clear_runtime_globals()
    except BaseExceptionGroup as group:
        errors.extend(_flatten_cleanup_group(group))
    except BaseException as exc:
        errors.append(_cleanup_exception(exc))

    for label, closer in reversed(owned_closers):
        error = await _close_resource(label, closer)
        if error is not None:
            errors.append(error)
    return errors


def _run_sync_cleanup(
    operations: tuple[Callable[[], None], ...],
) -> list[Exception]:
    errors: list[Exception] = []
    for operation in operations:
        try:
            operation()
        except BaseException as exc:
            errors.append(_cleanup_exception(exc))
    return errors


async def _close_resource(
    label: str,
    closer: Callable[[], Awaitable[None]],
) -> Exception | None:
    async def invoke() -> None:
        await closer()

    task = asyncio.create_task(invoke(), name=f"agent-runtime-close-{label}")
    done, pending = await asyncio.wait(
        {task},
        timeout=RUNTIME_RESOURCE_CLOSE_TIMEOUT_SECONDS,
    )
    if pending:
        task.cancel()
        task.add_done_callback(_consume_late_result)
        return TimeoutError(
            f"agent runtime {label} did not close within "
            f"{RUNTIME_RESOURCE_CLOSE_TIMEOUT_SECONDS} seconds"
        )
    try:
        task.result()
    except BaseException as exc:
        return _cleanup_exception(exc)
    return None


def _consume_late_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


def _cleanup_exception(exc: BaseException) -> Exception:
    if isinstance(exc, Exception):
        return exc
    return RuntimeError(f"{type(exc).__name__}: {exc}")


def _flatten_cleanup_group(group: BaseExceptionGroup) -> list[Exception]:
    errors: list[Exception] = []
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            errors.extend(_flatten_cleanup_group(exc))
        else:
            errors.append(_cleanup_exception(exc))
    return errors


def _cleanup_note(errors: list[Exception]) -> str:
    details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
    return f"agent runtime rollback had {len(errors)} cleanup failure(s): {details}"[:2000]


def _log_cleanup_errors(errors: list[Exception]) -> None:
    for error in errors:
        log.error("agent runtime cleanup failed: %s", error)
