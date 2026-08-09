from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import mimir

from mimir.access_control import create_auth_context
from mimir.models import AgentEvent
from mimir.tools.client_provider import (
    MIMIR_HANDS_V1,
    PermissionDecision as ToolPermissionDecision,
    PermissionEligibility,
    ProviderConnection,
    ProviderDeclaration,
    ProviderProfile,
    TurnCapabilityContext,
    reset_turn_capability_context,
    set_turn_capability_context,
)

from .bridge import ACPBridge
from .journal import JournalCache, JournalLease
from .sdk import (
    AUTH_METHOD_ID,
    AcpProtocolError,
    AcpPeer,
    AcpRequestHandle,
    AgentCapabilities,
    AudioContentBlock,
    AuthenticateResponse,
    AuthMethodAgent,
    Client,
    ClientCapabilities,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    McpCapabilities,
    NewSessionResponse,
    PermissionSnapshot,
    PromptCapabilities,
    PromptResponse,
    RequestError,
    ResourceContentBlock,
    TextContentBlock,
    UserMessageChunk,
    auth_required_error,
    internal_error,
    invalid_params_error,
    method_not_found_error,
    validate_acp_mcp_server,
)
from .session_store import SessionRecord, SessionStore
from .updates import UpdateDispatcher

if TYPE_CHECKING:
    from mimir.models import AuthContext
    from mimir.runtime import AgentRuntimeBundle

_WEB_KEY_FIELD = "mimir.webKey"
ACP_PROMPT_CANCEL_GRACE_SECONDS = 2.0
ACP_DISCONNECT_TIMEOUT_SECONDS = 1.0
ACP_AUDIT_EVENT_LIMIT = 256
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionEnvironment:
    cwd: str
    mcp_servers: object | None


@dataclass
class ConnectionState:
    generation: int
    peer: Client
    principal: str | None = None
    auth_context: AuthContext | None = None
    display_name: str | None = None
    server_sessions: dict[str, SessionState] = field(default_factory=dict)
    connection_sessions: dict[str, SessionState] = field(default_factory=dict)
    used_connection_ids: set[str] = field(default_factory=set)
    bound_sessions: set[str] = field(default_factory=set)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    closed: bool = False
    transport_dead: bool = False
    retirement_task: asyncio.Task[Any] | None = None


@dataclass
class SessionState:
    record: SessionRecord
    environment: SessionEnvironment
    generation: int
    declaration: ProviderDeclaration | None = None
    profile_policy: ProviderProfile | None = None
    provider: _AcpProviderConnection | None = None
    prompt_epoch: int = 0
    execution_session_key: int = 0
    active_prompt: ActivePrompt | None = None
    dirty: bool = False


@dataclass
class ProgressTokenOwnership:
    provider: _AcpProviderConnection
    generation: int
    epoch: int
    request_key: object
    outer_id: Any = None


@dataclass
class ActivePrompt:
    session: SessionState
    generation: int
    epoch: int
    prompt_handler: asyncio.Task[Any] | None
    model_task: asyncio.Task[Any] | None
    exact_event_forwarder: asyncio.Task[Any]
    dispatcher: UpdateDispatcher
    journal_lease: JournalLease
    permission_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    permission_handles: list[AcpRequestHandle] = field(default_factory=list)
    mcp_request_ids: set[Any] = field(default_factory=set)
    mcp_handles: list[AcpRequestHandle] = field(default_factory=list)
    mcp_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    cancelling: bool = False
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    progress_tokens: dict[str, ProgressTokenOwnership] = field(default_factory=dict)

    async def request_permission(
        self, eligibility: PermissionEligibility
    ) -> ToolPermissionDecision:
        agent = self.session.provider.agent if self.session.provider is not None else None
        if agent is None:
            return ToolPermissionDecision.REJECT_ONCE
        async with agent._boundary_lock:
            if not self._is_current():
                return ToolPermissionDecision.REJECT_ONCE
            task = asyncio.create_task(self._request_permission(eligibility))
            self.permission_tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            return ToolPermissionDecision.REJECT_ONCE
        finally:
            self.permission_tasks.discard(task)

    def _is_current(self) -> bool:
        return (
            not self.cancelling
            and not self.journal_lease.closed
            and self.session.active_prompt is self
            and self.session.generation == self.generation
            and self.session.prompt_epoch == self.epoch
        )

    async def _request_permission(
        self, eligibility: PermissionEligibility
    ) -> ToolPermissionDecision:
        await self.dispatcher.drain()
        snapshot = self.dispatcher.permission_snapshot(eligibility.tool_call_id)
        expected = _strict_arguments(eligibility.arguments)
        if snapshot is None or _thaw(snapshot.raw_input) != expected:
            return ToolPermissionDecision.REJECT_ONCE
        provider = self.session.provider
        peer = provider.peer if provider is not None else None
        if peer is None:
            return ToolPermissionDecision.REJECT_ONCE
        handle: AcpRequestHandle | None = None
        agent = provider.agent
        try:
            async with agent._boundary_lock:
                if not self._is_current() or self.session.provider is not provider:
                    return ToolPermissionDecision.REJECT_ONCE
                if isinstance(peer, AcpPeer) and peer.supports_owned_requests:
                    handle = await peer.start_tool_permission(
                        self.session.record.session_id, snapshot
                    )
                    if not self._is_current():
                        handle.abandon()
                        return ToolPermissionDecision.REJECT_ONCE
                    self.permission_handles.append(handle)
                    completion_task = handle.task
                else:
                    completion_task = asyncio.create_task(
                        peer.request_tool_permission(self.session.record.session_id, snapshot)
                    )
            completion = await completion_task
        except asyncio.CancelledError:
            if handle is not None:
                handle.abandon()
            return ToolPermissionDecision.REJECT_ONCE
        finally:
            if handle is not None and handle in self.permission_handles:
                self.permission_handles.remove(handle)
        if not self._is_current() or completion.error is not None:
            return ToolPermissionDecision.REJECT_ONCE
        if completion.decision == "allow_once":
            return ToolPermissionDecision.ALLOW_ONCE
        if completion.decision == "cancelled":
            return ToolPermissionDecision.CANCELLED
        return ToolPermissionDecision.REJECT_ONCE



class _UnavailableProvider:
    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError("No client provider is admitted")


class _AcpProviderConnection:
    def __init__(self, agent: MimirAcpAgent, peer: AcpPeer, connection_id: str, session: SessionState) -> None:
        self.agent = agent
        self.peer = peer
        self.connection_id = connection_id
        self.session = session
        self.closed = False
        self.revalidation_lock = asyncio.Lock()

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        active = self.session.active_prompt
        async with self.agent._boundary_lock:
            if (
                self.closed
                or active is None
                or not active._is_current()
                or self.session.provider is not self
            ):
                raise RuntimeError("Client provider connection is closed")
            task = asyncio.create_task(self._call_tool(active, name, arguments))
            active.mcp_tasks.add(task)
        try:
            return await task
        finally:
            active.mcp_tasks.discard(task)

    async def _call_tool(
        self, active: ActivePrompt, name: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        handle: AcpRequestHandle | None = None
        token = uuid.uuid4().hex
        ownership = ProgressTokenOwnership(
            self, active.generation, active.epoch, object()
        )
        try:
            async with self.agent._boundary_lock:
                if self.closed or not active._is_current() or self.session.provider is not self:
                    raise RuntimeError("Client provider connection is closed")
                active.progress_tokens[token] = ownership
                params = {
                    "name": name,
                    "arguments": dict(arguments),
                    "_meta": {"progressToken": token},
                }
                if isinstance(self.peer, AcpPeer) and self.peer.supports_owned_requests:
                    handle = await self.peer.start_mcp_request(
                        self.connection_id, "tools/call", params
                    )
                    ownership.outer_id = handle.outer_id
                    if not active._is_current():
                        handle.abandon()
                        raise RuntimeError("Client provider connection is closed")
                    active.mcp_handles.append(handle)
                    active.mcp_request_ids.add(handle.outer_id)
                    request_task = handle.task
                else:
                    request_task = asyncio.create_task(
                        self.peer.message_mcp(self.connection_id, "tools/call", params)
                    )
            result = await request_task
        except asyncio.CancelledError:
            if handle is not None:
                handle.abandon()
            raise
        finally:
            async with self.agent._boundary_lock:
                if active.progress_tokens.get(token) is ownership:
                    active.progress_tokens.pop(token)
            if handle is not None:
                if handle in active.mcp_handles:
                    active.mcp_handles.remove(handle)
                active.mcp_request_ids.discard(handle.outer_id)
        if self.closed or not active._is_current() or self.session.provider is not self:
            raise RuntimeError("Stale client provider result")
        if not isinstance(result, Mapping):
            raise RuntimeError("Malformed client provider result")
        return dict(result)



class MimirAcpAgent:
    def __init__(self, bundle: AgentRuntimeBundle) -> None:
        self._bundle = bundle
        self._identity_resolver = bundle.core.identity_resolver
        self._auth_context: AuthContext | None = None
        self._display_name: str | None = None
        config = getattr(bundle, "config", None)
        home = getattr(config, "home", None)
        if home is None:
            home = Path(self._identity_resolver._yaml_path).parents[1]
        self._ttl_days = getattr(config, "acp_journal_ttl_days", 7)
        self._store = SessionStore(home)
        self._journals = JournalCache(self._store)
        self._client: Client | None = None
        self._generation = 0
        self._connection: ConnectionState | None = None
        self._connections: dict[int, ConnectionState] = {}
        self._retirement_tasks: set[asyncio.Task[Any]] = set()
        self._sessions: dict[str, SessionState] = {}
        self._environments: dict[str, tuple[int, SessionEnvironment]] = {}
        self._dispatchers: set[UpdateDispatcher] = set()
        self._active_prompts: dict[str, ActivePrompt] = {}
        self._execution_keys: dict[str, int] = {}
        self._audit_events: deque[dict[str, Any]] = deque(
            maxlen=ACP_AUDIT_EVENT_LIMIT
        )
        self._boundary_lock = asyncio.Lock()
        self._bridge = ACPBridge()
        adapters = getattr(bundle, "adapters", None)
        channels = getattr(adapters, "channels", None)
        if channels is not None:
            channels.register(self._bridge)

    def on_connect(self, conn: Client) -> int:
        old = self._connection
        self._generation += 1
        connection = ConnectionState(self._generation, conn)
        self._connection = connection
        self._connections[connection.generation] = connection
        self._client = conn
        self._auth_context = None
        self._display_name = None
        self._bridge._connected = True
        if old is not None and old.peer is not conn and not old.closed:
            for session_id, active in tuple(self._active_prompts.items()):
                if active.generation == old.generation:
                    self._active_prompts.pop(session_id, None)
            for session_id, (generation, _) in tuple(self._environments.items()):
                if generation == old.generation:
                    self._environments.pop(session_id, None)
            task = asyncio.create_task(self._retire_generation(old.generation))
            old.retirement_task = task
            self._retirement_tasks.add(task)
            task.add_done_callback(self._retirement_tasks.discard)
            task.add_done_callback(_consume_background_task)
        return connection.generation

    async def initialize(self, protocol_version: int, client_capabilities: ClientCapabilities | None = None, client_info: Implementation | None = None, **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=1,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(image=False, audio=False, embedded_context=False),
                mcp_capabilities=McpCapabilities(http=False, sse=False, acp=True),
            ),
            auth_methods=[AuthMethodAgent(id=AUTH_METHOD_ID, name="Mimir web key", description='Pass the web key in _meta["mimir.webKey"].')],
            agent_info=Implementation(name="mimir", title="Mimir", version=mimir.__version__),
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        self._auth_context = None
        self._display_name = None
        connection = self._connection
        if connection is not None:
            connection.principal = None
            connection.auth_context = None
            connection.display_name = None
        try:
            raw_key = kwargs.get(_WEB_KEY_FIELD)
            if method_id != AUTH_METHOD_ID or not isinstance(raw_key, str) or not raw_key:
                raise ValueError
            identity = self._identity_resolver.resolve_web_key(raw_key)
            if identity is None or not identity.access.is_admin or identity.access.is_service:
                raise ValueError
            event = AgentEvent(trigger="acp_authenticate", channel_id="acp:stdio", content="", author=identity.canonical, author_display=identity.display_name or identity.canonical, author_id=None, source_id=None, source="acp", extra={"channel_visibility": "private", "bridge_instance": "acp-stdio"})
            auth_context = create_auth_context(event, self._identity_resolver, enforce=True, event_ingress="acp")
            if auth_context.principal != identity.canonical or auth_context.canonical_principal != identity.canonical or "admin" not in auth_context.roles or auth_context.is_service or not auth_context.enforcement_enabled:
                raise ValueError
        except Exception:
            self._auth_context = None
            raise auth_required_error() from None
        self._auth_context = auth_context
        self._display_name = identity.display_name or identity.canonical
        if connection is not None:
            if self._connection is not connection or connection.closed:
                raise auth_required_error()
            connection.principal = identity.canonical
            connection.auth_context = auth_context
            connection.display_name = identity.display_name or identity.canonical
        return AuthenticateResponse()

    async def new_session(self, cwd: str, additional_directories: list[str] | None = None, mcp_servers: object | None = None, **kwargs: Any) -> NewSessionResponse:
        owner = self._begin_stateful()
        self._validate_directories(additional_directories)
        client = self._require_client()
        declaration = self._validate_declaration(cwd, mcp_servers)
        connection = self._require_connection()
        if declaration is not None and declaration.server_id in connection.server_sessions:
            raise invalid_params_error()
        record: SessionRecord | None = None
        state: SessionState | None = None
        try:
            record = self._store.create_owned_session(owner)
            self._journals.open(record, client)
            state = SessionState(record, SessionEnvironment(cwd, copy.deepcopy(mcp_servers)), self._generation, declaration, MIMIR_HANDS_V1 if declaration else None)
            await self._admit_provider(state)
        except RequestError:
            if state is not None:
                await self._discard_candidate(state)
            if record is not None:
                self._rollback_new_record(record)
            raise
        except BaseException:
            if state is not None:
                await self._discard_candidate(state)
            if record is not None:
                self._rollback_new_record(record)
            raise internal_error() from None
        self._install_state(state)
        return NewSessionResponse(sessionId=record.session_id)

    async def load_session(self, cwd: str, session_id: str, mcp_servers: object | None = None, additional_directories: list[str] | None = None, **kwargs: Any) -> LoadSessionResponse | None:
        owner = self._begin_stateful()
        self._validate_directories(additional_directories)
        client = self._require_client()
        declaration = self._validate_declaration(cwd, mcp_servers)
        connection = self._require_connection()
        existing_owner = connection.server_sessions.get(declaration.server_id) if declaration else None
        if existing_owner is not None and existing_owner.record.session_id != session_id:
            raise invalid_params_error()
        state: SessionState | None = None
        prior_state = self._sessions.get(session_id)
        try:
            record = self._store.load_owned(session_id, owner)
            journal = self._journals.open(record, client)
            state = SessionState(record, SessionEnvironment(cwd, copy.deepcopy(mcp_servers)), self._generation, declaration, MIMIR_HANDS_V1 if declaration else None, execution_session_key=max(self._execution_keys.get(session_id, 0), self._sessions.get(session_id, SessionState(record, SessionEnvironment(cwd, None), self._generation)).execution_session_key) + 1)
            await self._admit_provider(state)
            await journal.send_replay(client)
        except RequestError:
            if state is not None:
                await self._discard_candidate(state)
            if prior_state is not None:
                self._restore_state_indexes(prior_state)
            raise
        except BaseException:
            if state is not None:
                await self._discard_candidate(state)
            if prior_state is not None:
                self._restore_state_indexes(prior_state)
            raise internal_error() from None
        await self._detach_session(session_id)
        self._install_state(state)
        return LoadSessionResponse()

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        owner = self._begin_stateful()
        blocks = self._validate_prompt(prompt)
        client = self._require_client()
        state = self._sessions.get(session_id)
        if state is None or state.generation != self._generation or state.dirty:
            raise RequestError(-32602, "Invalid session")
        if state.active_prompt is not None:
            raise internal_error()
        return await self._run_prompt(state, owner, blocks, client)

    async def _run_prompt(self, state: SessionState, owner: str, blocks: list[Any], client: Client) -> PromptResponse:
        try:
            record = self._store.load_owned_live(state.record.session_id, owner)
            journal = self._journals.open(record, client)
        except RequestError:
            raise
        except BaseException:
            raise internal_error() from None
        state.prompt_epoch += 1
        epoch = state.prompt_epoch
        turn_id = str(uuid.uuid4())
        lease = JournalLease(turn_id, state.generation, epoch)
        publisher = _TurnPublisher(journal, client, lease)
        dispatcher = UpdateDispatcher(publisher, lease, epoch)
        queue = self._bundle.turn_event_bus.subscribe_exact_turn(turn_id)
        forwarder = asyncio.create_task(self._forward_updates(queue, dispatcher))
        bridge_publisher = _OrderedTurnPublisher(publisher, queue, dispatcher)
        active = ActivePrompt(state, state.generation, epoch, asyncio.current_task(), None, forwarder, dispatcher, lease)
        state.active_prompt = active
        self._active_prompts[record.session_id] = active
        self._dispatchers.add(dispatcher)
        self._bridge.bind(record.thread_id, bridge_publisher)
        provider: ProviderConnection = state.provider or _UnavailableProvider()
        context = TurnCapabilityContext(active, provider, state.profile_policy, state.generation, epoch, True, lease, state.environment.cwd)
        token = set_turn_capability_context(context)
        response = PromptResponse(stopReason="end_turn")
        failed: BaseException | None = None
        try:
            for block in blocks:
                await journal.publish_live(UserMessageChunk(sessionUpdate="user_message_chunk", content=block), client, turn_id=turn_id, lease=lease)
            event = AgentEvent(trigger="user_message", channel_id=record.thread_id, content=self._normalize_prompt(blocks), author=owner, author_display=self._display_name_for(state) or owner, author_id=owner, source_id=turn_id, source="acp", extra={"channel_visibility": "private"}, continuation_auth_context=self._auth_context_for(state))
            async with self._boundary_lock:
                if not active._is_current():
                    raise asyncio.CancelledError
                active.model_task = asyncio.create_task(self._bundle.agent.run_turn(event, turn_id=turn_id, session_id=record.thread_id, saga_session_id=record.thread_id))
            await active.model_task
            await queue.join()
            await dispatcher.drain()
        except asyncio.CancelledError as exc:
            if not active.cancelling:
                failed = exc
                try:
                    await queue.join()
                    await asyncio.sleep(0)
                    await dispatcher.terminalize_failure(exc)
                except BaseException:
                    pass
            else:
                await self._finish_cancel(active)
                response = PromptResponse(stopReason="cancelled")
        except BaseException as exc:
            failed = exc
            try:
                await queue.join()
                await asyncio.sleep(0)
                await dispatcher.terminalize_failure(exc)
            except BaseException:
                pass
        finally:
            reset_turn_capability_context(token)
            self._bundle.turn_event_bus.unsubscribe_exact_turn(turn_id, queue)
            forwarder.cancel()
            await _await_cancelled(forwarder)
            self._bridge.unbind(record.thread_id, bridge_publisher)
            self._dispatchers.discard(dispatcher)
            try:
                await dispatcher.close()
            except BaseException as exc:
                failed = failed or exc
            if state.active_prompt is active:
                state.active_prompt = None
            if self._active_prompts.get(record.session_id) is active:
                self._active_prompts.pop(record.session_id, None)
            active.progress_tokens.clear()
            active.completed.set()
        if failed is not None:
            raise internal_error() from None
        return response

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        state = self._sessions.get(session_id)
        active = state.active_prompt if state is not None else None
        if active is None or not await self._cancel_active(active, transport=False):
            event = {"event": "acp_cancel_noop", "session_id": session_id}
            self._audit_events.append(event)
            _LOGGER.info("acp_cancel_noop", extra={"acp_audit": event})
        return None

    async def _cancel_active(self, active: ActivePrompt, *, transport: bool) -> bool:
        async with self._boundary_lock:
            if active.cancelling:
                return False
            active.cancelling = True
            if transport:
                active.journal_lease.close()
            else:
                await active.journal_lease.close_boundary(active.dispatcher.publisher._journal.lock)
            for handle in tuple(active.permission_handles):
                handle.abandon()
            provider = active.session.provider
            for handle in tuple(active.mcp_handles):
                connection = self._connections.get(active.generation)
                if (
                    not transport
                    and provider is not None
                    and connection is not None
                    and not connection.transport_dead
                ):
                    task = asyncio.create_task(
                        provider.peer.notify_mcp(
                            provider.connection_id,
                            "notifications/cancelled",
                            {"requestId": handle.outer_id},
                        )
                    )
                    connection.tasks.add(task)
                    task.add_done_callback(connection.tasks.discard)
                    task.add_done_callback(_consume_background_task)
                handle.abandon()
            active.progress_tokens.clear()
            for task in tuple(active.permission_tasks | active.mcp_tasks):
                task.cancel()
            if active.model_task is not None:
                active.model_task.cancel()
            if transport and active.prompt_handler is not None and active.prompt_handler is not asyncio.current_task():
                active.prompt_handler.cancel()
        if transport or active.prompt_handler is asyncio.current_task():
            return True
        try:
            await asyncio.wait_for(active.completed.wait(), ACP_PROMPT_CANCEL_GRACE_SECONDS)
        except TimeoutError:
            state = active.session
            state.dirty = True
            state.execution_session_key += 1
            session_id = state.record.session_id
            self._execution_keys[session_id] = state.execution_session_key
            self._environments.pop(session_id, None)
            provider = state.provider
            connection = self._connections.get(state.generation)
            if provider is not None:
                provider.closed = True
                state.provider = None
                if connection is not None:
                    connection.connection_sessions.pop(provider.connection_id, None)
                    task = asyncio.create_task(self._disconnect_provider(provider, connection))
                    connection.tasks.add(task)
                    task.add_done_callback(connection.tasks.discard)
                    task.add_done_callback(_consume_background_task)
            if state.declaration is not None and connection is not None:
                if connection.server_sessions.get(state.declaration.server_id) is state:
                    connection.server_sessions.pop(state.declaration.server_id, None)
        return True

    async def _finish_cancel(self, active: ActivePrompt) -> None:
        await active.dispatcher.terminalize_cancelled()

    async def on_transport_closed(self, generation: int) -> None:
        connection = self._connections.get(generation)
        if connection is not None:
            connection.transport_dead = True
            retirement = connection.retirement_task
            if retirement is not None and retirement is not asyncio.current_task() and not retirement.done():
                retirement.cancel()
                await _await_cancelled(retirement)
                connection.closed = False
        await self._retire_generation(generation)

    async def _retire_generation(self, generation: int) -> None:
        connection = self._connections.get(generation)
        if connection is None or connection.closed:
            return
        connection.closed = True
        owned: dict[int, SessionState] = {}
        for state in (*connection.server_sessions.values(), *connection.connection_sessions.values()):
            if state.generation == generation:
                owned[id(state)] = state
        for session_id in connection.bound_sessions:
            state = self._sessions.get(session_id)
            if state is not None and state.generation == generation:
                owned[id(state)] = state
        for state in owned.values():
            active = state.active_prompt
            if active is not None and active.generation == generation:
                await self._cancel_active(active, transport=connection.transport_dead)
                self._dispatchers.discard(active.dispatcher)
                if self._active_prompts.get(state.record.session_id) is active:
                    self._active_prompts.pop(state.record.session_id, None)
            provider = state.provider
            if provider is not None:
                provider.closed = True
                state.provider = None
                connection.connection_sessions.pop(provider.connection_id, None)
                if not connection.transport_dead:
                    await self._disconnect_provider(provider, connection)
            if self._sessions.get(state.record.session_id) is state:
                self._sessions.pop(state.record.session_id, None)
                self._environments.pop(state.record.session_id, None)
        connection.server_sessions.clear()
        connection.connection_sessions.clear()
        connection.bound_sessions.clear()
        connection.used_connection_ids.clear()
        for task in tuple(connection.tasks):
            if task is not asyncio.current_task():
                task.cancel()
        self._connections.pop(generation, None)
        if self._connection is connection:
            self._connection = None
            self._client = None
            self._bridge._connected = False

    async def _disconnect_provider(
        self, provider: _AcpProviderConnection, connection: ConnectionState | None
    ) -> None:
        if connection is None or connection.transport_dead:
            return
        try:
            await asyncio.wait_for(
                provider.peer.disconnect_mcp(provider.connection_id),
                ACP_DISCONNECT_TIMEOUT_SECONDS,
            )
        except BaseException:
            pass

    async def on_mcp_notification(
        self, peer_generation: int, connection_id: str, method: str,
        params: dict[str, Any] | None,
    ) -> None:
        connection = self._connections.get(peer_generation)
        state = connection.connection_sessions.get(connection_id) if connection is not None else None
        if state is None or state.generation != peer_generation:
            return
        if method == "notifications/tools/list_changed":
            task = asyncio.create_task(self._revalidate_provider(state))
            connection.tasks.add(task)
            task.add_done_callback(connection.tasks.discard)
            task.add_done_callback(_consume_background_task)
            return
        if method == "notifications/progress":
            self._audit_progress(state, params)
            return
        if method == "notifications/message":
            self._audit_message(state, params)

    def _audit_progress(self, state: SessionState, params: object) -> None:
        token = params.get("progressToken") if isinstance(params, Mapping) else None
        active = state.active_prompt
        ownership = active.progress_tokens.get(token) if active is not None and isinstance(token, str) else None
        accepted = (
            ownership is not None
            and active is not None
            and active._is_current()
            and ownership.provider is state.provider
            and ownership.generation == state.generation
            and ownership.epoch == active.epoch
        )
        event = {
            "event": "acp_mcp_progress",
            "generation": state.generation,
            "status": "accepted" if accepted else "ignored",
        }
        if accepted and isinstance(params, Mapping):
            for key in ("progress", "total", "message"):
                if key in params:
                    event[key] = _bounded_audit_value(params[key])
        self._audit_events.append(event)
        _LOGGER.info("acp_mcp_progress", extra={"acp_audit": event})

    def _audit_message(self, state: SessionState, params: object) -> None:
        payload = params if isinstance(params, Mapping) else {}
        event = {
            "event": "acp_mcp_message",
            "generation": state.generation,
            "level": _bounded_audit_text(payload.get("level")),
            "logger": _bounded_audit_text(payload.get("logger")),
            "data": _bounded_audit_value(payload.get("data")),
        }
        self._audit_events.append(event)
        _LOGGER.info("acp_mcp_message", extra={"acp_audit": event})

    async def _revalidate_provider(self, state: SessionState) -> None:
        provider = state.provider
        if provider is None:
            return
        async with provider.revalidation_lock:
            connection = self._connections.get(state.generation)
            if (
                provider.closed
                or state.provider is not provider
                or connection is None
                or connection.closed
                or connection.connection_sessions.get(provider.connection_id) is not state
            ):
                return
            try:
                await self._validate_tools(state)
            except BaseException:
                await self._detach_state(state)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise method_not_found_error(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None

    def _begin_stateful(self) -> str:
        connection = self._connection
        if connection is None:
            auth_context = self._auth_context
        else:
            auth_context = connection.auth_context
        if (
            auth_context is None
            or auth_context.canonical_principal is None
            or (
                connection is not None
                and (
                    connection.closed
                    or connection.generation != self._generation
                    or connection.principal != auth_context.canonical_principal
                )
            )
        ):
            raise auth_required_error()
        try:
            self._store.sweep(self._ttl_days)
        except BaseException:
            raise internal_error() from None
        return auth_context.canonical_principal

    def _auth_context_for(self, state: SessionState) -> AuthContext | None:
        connection = self._connections.get(state.generation)
        if connection is None or connection.principal != state.record.owner_principal:
            return None
        return connection.auth_context

    def _display_name_for(self, state: SessionState) -> str | None:
        connection = self._connections.get(state.generation)
        if connection is None or connection.principal != state.record.owner_principal:
            return None
        return connection.display_name

    def _require_client(self) -> Client:
        if self._client is None:
            raise internal_error()
        return self._client

    def _require_connection(self) -> ConnectionState:
        if self._connection is None or self._connection.closed:
            raise internal_error()
        return self._connection

    def _validate_declaration(self, cwd: str, mcp_servers: object | None) -> ProviderDeclaration | None:
        if mcp_servers is None or mcp_servers == []:
            return None
        if not isinstance(mcp_servers, list) or len(mcp_servers) != 1:
            raise invalid_params_error()
        raw = mcp_servers[0]
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump(mode="json", by_alias=True, exclude_none=True)
        try:
            value = validate_acp_mcp_server(raw)
        except BaseException:
            raise invalid_params_error() from None
        if value.name != "mimir-hands":
            raise invalid_params_error()
        return ProviderDeclaration(value.name, value.server_id, cwd)

    async def _admit_provider(self, state: SessionState) -> None:
        if state.declaration is None:
            return
        connection = self._require_connection()
        peer = connection.peer
        if not isinstance(peer, AcpPeer) and not all(hasattr(peer, name) for name in ("connect_mcp", "message_mcp", "notify_mcp", "disconnect_mcp")):
            raise invalid_params_error()
        previous_server = connection.server_sessions.get(state.declaration.server_id)
        connection.server_sessions[state.declaration.server_id] = state
        connection.bound_sessions.add(state.record.session_id)
        connection_id: str | None = None
        try:
            connection_id = await peer.connect_mcp(state.declaration.server_id)
            if not connection_id or connection_id in connection.used_connection_ids or connection_id in connection.connection_sessions:
                raise AcpProtocolError("MCP connection ID is empty or reused")
            connection.used_connection_ids.add(connection_id)
            provider = _AcpProviderConnection(self, peer, connection_id, state)
            state.provider = provider
            connection.connection_sessions[connection_id] = state
            await self._initialize_provider(state)
            await self._validate_tools(state)
        except BaseException:
            if connection.server_sessions.get(state.declaration.server_id) is state:
                if previous_server is None:
                    connection.server_sessions.pop(state.declaration.server_id, None)
                else:
                    connection.server_sessions[state.declaration.server_id] = previous_server
            if connection_id is not None:
                connection.connection_sessions.pop(connection_id, None)
                if state.provider is not None:
                    state.provider.closed = True
                    state.provider = None
                temporary = _AcpProviderConnection(self, peer, connection_id, state)
                temporary.closed = True
                await self._disconnect_provider(temporary, connection)
            raise invalid_params_error() from None

    async def _initialize_provider(self, state: SessionState) -> None:
        provider = state.provider
        if provider is None:
            raise AcpProtocolError("Missing provider")
        initialized = await provider.peer.message_mcp(
            provider.connection_id,
            "initialize",
            {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "mimir", "version": mimir.__version__}},
        )
        if not isinstance(initialized, Mapping):
            raise AcpProtocolError("Malformed MCP initialize result")
        await provider.peer.notify_mcp(
            provider.connection_id, "notifications/initialized"
        )

    async def _validate_tools(self, state: SessionState) -> None:
        provider = state.provider
        if provider is None:
            raise AcpProtocolError("Missing provider")
        listed = await provider.peer.message_mcp(provider.connection_id, "tools/list", {})
        if not isinstance(listed, Mapping) or set(listed) - {"tools", "nextCursor"} or listed.get("nextCursor") not in {None, ""} or not isinstance(listed.get("tools"), list):
            raise AcpProtocolError("Malformed MCP tool list")
        expected = {tool.provider_name: tool for tool in MIMIR_HANDS_V1.tools}
        if len(listed["tools"]) != len(expected):
            raise AcpProtocolError("MCP tool profile mismatch")
        seen: set[str] = set()
        for tool in listed["tools"]:
            if not isinstance(tool, Mapping) or set(tool) - {"name", "description", "inputSchema", "outputSchema", "annotations", "_meta"}:
                raise AcpProtocolError("MCP tool profile mismatch")
            name = tool.get("name")
            policy = expected.get(name)
            if (
                policy is None
                or name in seen
                or tool.get("description") != policy.description
                or tool.get("inputSchema") != _thaw(policy.input_schema)
                or tool.get("outputSchema") != _thaw(policy.result_schema)
            ):
                raise AcpProtocolError("MCP tool profile mismatch")
            seen.add(name)
        if seen != set(expected):
            raise AcpProtocolError("MCP tool profile mismatch")

    async def _discard_candidate(self, state: SessionState) -> None:
        connection = self._connections.get(state.generation)
        provider = state.provider
        if provider is not None:
            provider.closed = True
            if connection is not None and connection.connection_sessions.get(provider.connection_id) is state:
                connection.connection_sessions.pop(provider.connection_id, None)
            await self._disconnect_provider(provider, connection)
        declaration = state.declaration
        if declaration is not None and connection is not None and connection.server_sessions.get(declaration.server_id) is state:
            connection.server_sessions.pop(declaration.server_id, None)
        if connection is not None:
            connection.bound_sessions.discard(state.record.session_id)

    def _restore_state_indexes(self, state: SessionState) -> None:
        connection = self._connections.get(state.generation)
        if connection is None or connection.closed:
            return
        connection.bound_sessions.add(state.record.session_id)
        declaration = state.declaration
        if declaration is not None:
            connection.server_sessions[declaration.server_id] = state
        provider = state.provider
        if provider is not None and not provider.closed:
            connection.connection_sessions[provider.connection_id] = state

    def _rollback_new_record(self, record: SessionRecord) -> None:
        self._journals.discard(record.session_id)
        record.journal_path.unlink(missing_ok=True)
        record.metadata_path.unlink(missing_ok=True)
        self._store._fsync_parent()

    def _install_state(self, state: SessionState) -> None:
        self._sessions[state.record.session_id] = state
        self._execution_keys[state.record.session_id] = state.execution_session_key
        self._environments[state.record.session_id] = (state.generation, state.environment)
        connection = self._require_connection()
        connection.bound_sessions.add(state.record.session_id)

    async def _detach_session(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is not None:
            await self._detach_state(state)

    async def _detach_state(self, state: SessionState) -> None:
        session_id = state.record.session_id
        connection = self._connections.get(state.generation)
        provider = state.provider
        if provider is not None:
            provider.closed = True
            if state.provider is provider:
                state.provider = None
            if connection is not None and connection.connection_sessions.get(provider.connection_id) is state:
                connection.connection_sessions.pop(provider.connection_id, None)
            await self._disconnect_provider(provider, connection)
        if state.declaration is not None and connection is not None and connection.server_sessions.get(state.declaration.server_id) is state:
            connection.server_sessions.pop(state.declaration.server_id, None)
        if connection is not None:
            connection.bound_sessions.discard(session_id)
        if self._sessions.get(session_id) is state:
            self._sessions.pop(session_id, None)
            self._environments.pop(session_id, None)

    @staticmethod
    def _validate_directories(additional_directories: list[str] | None) -> None:
        if additional_directories:
            raise invalid_params_error()

    @staticmethod
    def _validate_prompt(prompt: object) -> list[TextContentBlock | ResourceContentBlock]:
        if not isinstance(prompt, list) or any(not isinstance(block, (TextContentBlock, ResourceContentBlock)) for block in prompt):
            raise invalid_params_error()
        return list(prompt)

    @staticmethod
    def _normalize_prompt(blocks: list[TextContentBlock | ResourceContentBlock]) -> str:
        parts = []
        for block in blocks:
            if isinstance(block, TextContentBlock):
                parts.append(block.text)
            else:
                payload = block.model_dump(mode="json", by_alias=True, exclude_none=True)
                parts.append("[resource_link]" + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return "\n".join(parts)

    @staticmethod
    async def _forward_updates(queue: asyncio.Queue[dict[str, Any]], dispatcher: UpdateDispatcher) -> None:
        while True:
            event = await queue.get()
            try:
                await dispatcher.submit(event)
            finally:
                queue.task_done()


class _TurnPublisher:
    def __init__(self, journal: Any, client: Client, lease: JournalLease) -> None:
        self._journal = journal
        self._client = client
        self._lease = lease

    async def accept_event(self) -> bool:
        return await self._journal.accept_event(self._lease)

    async def publish_live(self, update: Any, *, accepted: bool = False) -> Any:
        return await self._journal.publish_live(update, self._client, turn_id=self._lease.turn_id, lease=self._lease, accepted=accepted)

    async def close_turn(self, terminal_updates: list[Any]) -> list[Any]:
        return await self._journal.close_turn(self._lease, terminal_updates, self._client)


class _OrderedTurnPublisher:
    def __init__(self, publisher: _TurnPublisher, queue: asyncio.Queue[dict[str, Any]], dispatcher: UpdateDispatcher) -> None:
        self._publisher = publisher
        self._queue = queue
        self._dispatcher = dispatcher

    async def publish_live(self, update: Any) -> Any:
        await self._queue.join()
        await self._dispatcher.drain()
        return await self._publisher.publish_live(update)


def _strict_arguments(value: Mapping[str, Any]) -> dict[str, Any]:
    from .updates import _strict_json
    result = _strict_json(value)
    return result if isinstance(result, dict) else {}


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _bounded_audit_text(value: Any) -> str | None:
    return value[:256] if isinstance(value, str) else None


def _bounded_audit_value(value: Any, depth: int = 0) -> Any:
    if depth >= 3:
        return "[truncated]"
    if isinstance(value, str):
        return value[:256]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:16]:
            name = str(key)[:64]
            if any(part in name.lower() for part in ("token", "secret", "password", "authorization", "key")):
                result[name] = "[redacted]"
            else:
                result[name] = _bounded_audit_value(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_audit_value(item, depth + 1) for item in value[:16]]
    return "[unsupported]"


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def _await_cancelled(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except BaseException:
        pass


__all__ = ["ACP_PROMPT_CANCEL_GRACE_SECONDS", "ActivePrompt", "ConnectionState", "MimirAcpAgent", "SessionEnvironment", "SessionState"]
