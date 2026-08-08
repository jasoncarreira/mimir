from __future__ import annotations

import asyncio
import copy
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import mimir

from mimir.access_control import create_auth_context
from mimir.models import AgentEvent

from .bridge import ACPBridge
from .journal import JournalCache
from .sdk import (
    AUTH_METHOD_ID,
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
)
from .session_store import SessionStore
from .updates import UpdateDispatcher

if TYPE_CHECKING:
    from mimir.models import AuthContext
    from mimir.runtime import AgentRuntimeBundle


_WEB_KEY_FIELD = "mimir.webKey"


@dataclass(frozen=True)
class SessionEnvironment:
    cwd: str
    mcp_servers: object | None


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
        self._environments: dict[str, tuple[int, SessionEnvironment]] = {}
        self._dispatchers: set[UpdateDispatcher] = set()
        self._active_prompts: dict[str, object] = {}
        self._bridge = ACPBridge()
        adapters = getattr(bundle, "adapters", None)
        channels = getattr(adapters, "channels", None)
        if channels is not None:
            channels.register(self._bridge)

    def on_connect(self, conn: Client) -> None:
        if self._client is not None and self._client is not conn:
            for dispatcher in tuple(self._dispatchers):
                dispatcher.invalidate()
        self._client = conn
        self._generation += 1
        self._environments.clear()
        self._active_prompts.clear()
        self._bridge._connected = True

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=1,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=False,
                    audio=False,
                    embedded_context=False,
                ),
                mcp_capabilities=McpCapabilities(http=False, sse=False),
            ),
            auth_methods=[
                AuthMethodAgent(
                    id=AUTH_METHOD_ID,
                    name="Mimir web key",
                    description='Pass the web key in _meta["mimir.webKey"].',
                )
            ],
            agent_info=Implementation(
                name="mimir",
                title="Mimir",
                version=mimir.__version__,
            ),
        )

    async def authenticate(
        self,
        method_id: str,
        **kwargs: Any,
    ) -> AuthenticateResponse | None:
        self._auth_context = None
        self._display_name = None
        try:
            raw_key = kwargs.get(_WEB_KEY_FIELD)
            if method_id != AUTH_METHOD_ID or not isinstance(raw_key, str) or not raw_key:
                raise ValueError

            identity = self._identity_resolver.resolve_web_key(raw_key)
            if identity is None or not identity.access.is_admin or identity.access.is_service:
                raise ValueError

            event = AgentEvent(
                trigger="acp_authenticate",
                channel_id="acp:stdio",
                content="",
                author=identity.canonical,
                author_display=identity.display_name or identity.canonical,
                author_id=None,
                source_id=None,
                source="acp",
                extra={
                    "channel_visibility": "private",
                    "bridge_instance": "acp-stdio",
                },
            )
            auth_context = create_auth_context(
                event,
                self._identity_resolver,
                enforce=True,
                event_ingress="acp",
            )
            if (
                auth_context.principal != identity.canonical
                or auth_context.canonical_principal != identity.canonical
                or "admin" not in auth_context.roles
                or auth_context.is_service
                or not auth_context.enforcement_enabled
            ):
                raise ValueError
        except Exception:
            self._auth_context = None
            raise auth_required_error() from None

        self._auth_context = auth_context
        self._display_name = identity.display_name or identity.canonical
        return AuthenticateResponse()

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: object | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        owner = self._begin_stateful()
        self._validate_directories(additional_directories)
        client = self._require_client()
        try:
            environment = self._environment(cwd, mcp_servers)
            record = self._store.create_owned_session(owner)
            self._journals.open(record, client)
        except RequestError:
            raise
        except BaseException:
            raise internal_error() from None
        self._environments[record.session_id] = (self._generation, environment)
        return NewSessionResponse(sessionId=record.session_id)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: object | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        owner = self._begin_stateful()
        self._validate_directories(additional_directories)
        client = self._require_client()
        try:
            environment = self._environment(cwd, mcp_servers)
            record = self._store.load_owned(session_id, owner)
            journal = self._journals.open(record, client)
            await journal.send_replay(client)
        except RequestError:
            raise
        except BaseException:
            raise internal_error() from None
        self._environments[record.session_id] = (self._generation, environment)
        return LoadSessionResponse()

    async def prompt(
        self,
        session_id: str,
        prompt: list[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
        **kwargs: Any,
    ) -> PromptResponse:
        owner = self._begin_stateful()
        blocks = self._validate_prompt(prompt)
        client = self._require_client()
        binding = self._environments.get(session_id)
        if binding is None or binding[0] != self._generation:
            raise RequestError(-32602, "Invalid session")
        prompt_token = object()
        if session_id in self._active_prompts:
            raise internal_error()
        self._active_prompts[session_id] = prompt_token
        try:
            return await self._run_prompt(session_id, owner, blocks, client)
        finally:
            if self._active_prompts.get(session_id) is prompt_token:
                self._active_prompts.pop(session_id, None)

    async def _run_prompt(
        self,
        session_id: str,
        owner: str,
        blocks: list[TextContentBlock | ResourceContentBlock],
        client: Client,
    ) -> PromptResponse:
        try:
            record = self._store.load_owned_live(session_id, owner)
            journal = self._journals.open(record, client)
        except RequestError:
            raise
        except BaseException:
            raise internal_error() from None

        turn_id = str(uuid.uuid4())
        channel_id = record.thread_id
        publisher = _TurnPublisher(journal, client, turn_id)
        dispatcher = UpdateDispatcher(publisher)
        queue = self._bundle.turn_event_bus.subscribe_exact_turn(turn_id)
        forwarder = asyncio.create_task(self._forward_updates(queue, dispatcher))
        bridge_publisher = _OrderedTurnPublisher(publisher, queue, dispatcher)
        self._dispatchers.add(dispatcher)
        self._bridge.bind(channel_id, bridge_publisher)
        failed = False
        try:
            for block in blocks:
                await journal.publish_live(
                    UserMessageChunk(sessionUpdate="user_message_chunk", content=block),
                    client,
                    turn_id=turn_id,
                )
            event = AgentEvent(
                trigger="user_message",
                channel_id=channel_id,
                content=self._normalize_prompt(blocks),
                author=owner,
                author_display=self._display_name or owner,
                author_id=owner,
                source_id=turn_id,
                source="acp",
                extra={"channel_visibility": "private"},
                continuation_auth_context=self._auth_context,
            )
            await self._bundle.agent.run_turn(
                event,
                turn_id=turn_id,
                session_id=channel_id,
                saga_session_id=record.thread_id,
            )
            await queue.join()
            await dispatcher.drain()
        except BaseException as exc:
            failed = True
            try:
                await queue.join()
                await dispatcher.terminalize_failure(exc)
            except BaseException:
                pass
            raise internal_error() from None
        finally:
            self._bundle.turn_event_bus.unsubscribe_exact_turn(turn_id, queue)
            forwarder.cancel()
            try:
                await forwarder
            except BaseException:
                pass
            self._bridge.unbind(channel_id, bridge_publisher)
            self._dispatchers.discard(dispatcher)
            try:
                await dispatcher.close()
            except BaseException:
                if not failed:
                    raise internal_error() from None
        return PromptResponse(stopReason="end_turn")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        return None

    async def ext_method(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        raise method_not_found_error(f"_{method}")

    async def ext_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:
        return None

    def _begin_stateful(self) -> str:
        if self._auth_context is None or self._auth_context.canonical_principal is None:
            raise auth_required_error()
        try:
            self._store.sweep(self._ttl_days)
        except BaseException:
            raise internal_error() from None
        return self._auth_context.canonical_principal

    def _require_client(self) -> Client:
        if self._client is None:
            raise internal_error()
        return self._client

    @staticmethod
    def _validate_directories(additional_directories: list[str] | None) -> None:
        if additional_directories:
            raise invalid_params_error()

    @staticmethod
    def _environment(cwd: str, mcp_servers: object | None) -> SessionEnvironment:
        try:
            return SessionEnvironment(cwd=cwd, mcp_servers=copy.deepcopy(mcp_servers))
        except BaseException:
            raise invalid_params_error() from None

    @staticmethod
    def _validate_prompt(prompt: object) -> list[TextContentBlock | ResourceContentBlock]:
        if not isinstance(prompt, list) or any(
            not isinstance(block, (TextContentBlock, ResourceContentBlock))
            for block in prompt
        ):
            raise invalid_params_error()
        return list(prompt)

    @staticmethod
    def _normalize_prompt(blocks: list[TextContentBlock | ResourceContentBlock]) -> str:
        parts: list[str] = []
        for block in blocks:
            if isinstance(block, TextContentBlock):
                parts.append(block.text)
            else:
                payload = block.model_dump(mode="json", by_alias=True, exclude_none=True)
                parts.append("[resource_link]" + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return "\n".join(parts)

    @staticmethod
    async def _forward_updates(
        queue: asyncio.Queue[dict[str, Any]],
        dispatcher: UpdateDispatcher,
    ) -> None:
        while True:
            event = await queue.get()
            try:
                dispatcher.enqueue(event)
            finally:
                queue.task_done()


class _TurnPublisher:
    def __init__(self, journal: Any, client: Client, turn_id: str) -> None:
        self._journal = journal
        self._client = client
        self._turn_id = turn_id

    async def publish_live(self, update: Any) -> Any:
        return await self._journal.publish_live(
            update, self._client, turn_id=self._turn_id
        )


class _OrderedTurnPublisher:
    def __init__(
        self,
        publisher: _TurnPublisher,
        queue: asyncio.Queue[dict[str, Any]],
        dispatcher: UpdateDispatcher,
    ) -> None:
        self._publisher = publisher
        self._queue = queue
        self._dispatcher = dispatcher

    async def publish_live(self, update: Any) -> Any:
        await self._queue.join()
        await self._dispatcher.drain()
        return await self._publisher.publish_live(update)


__all__ = ["MimirAcpAgent", "SessionEnvironment"]
