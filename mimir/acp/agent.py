from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

import mimir

from mimir.access_control import create_auth_context
from mimir.models import AgentEvent

from .sdk import (
    AUTH_METHOD_ID,
    AgentCapabilities,
    AudioContentBlock,
    AuthenticateResponse,
    AuthMethodAgent,
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
    ResourceContentBlock,
    TextContentBlock,
    auth_required_error,
    method_not_found_error,
)

if TYPE_CHECKING:
    from mimir.models import AuthContext
    from mimir.runtime import AgentRuntimeBundle


_WEB_KEY_FIELD = "mimir.webKey"


class MimirAcpAgent:
    def __init__(self, bundle: AgentRuntimeBundle) -> None:
        self._identity_resolver = bundle.core.identity_resolver
        self._auth_context: AuthContext | None = None

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
        try:
            raw_key = kwargs.get(_WEB_KEY_FIELD)
            if method_id != AUTH_METHOD_ID or not isinstance(raw_key, str) or not raw_key:
                raise ValueError

            identity = self._identity_resolver.resolve_web_key(raw_key)
            if (
                identity is None
                or not identity.access.is_admin
                or identity.access.is_service
            ):
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
            ):
                raise ValueError
        except Exception:
            self._auth_context = None
            raise auth_required_error() from None

        self._auth_context = auth_context
        return AuthenticateResponse()

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: object | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        self._raise_stateful_method_error("session/new")

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: object | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        self._raise_stateful_method_error("session/load")

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
        self._raise_stateful_method_error("session/prompt")

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

    def _raise_stateful_method_error(self, method: str) -> NoReturn:
        if self._auth_context is None:
            raise auth_required_error()
        raise method_not_found_error(method)


__all__ = ["MimirAcpAgent"]
