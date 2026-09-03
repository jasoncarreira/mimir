"""Server-owned policy and turn routing for client-hosted tools."""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Awaitable, Mapping, Protocol, runtime_checkable

from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, ConfigDict

from ..acp.hands_contract import (
    HANDS_PROVIDER_TO_WRAPPER,
    HANDS_V1_WIRE_TOOLS,
    HANDS_WRAPPER_TO_PROVIDER,
    HandsContractError,
    validate_tool_result,
)
from ..access_control import (
    CLIENT_FILE_RESOURCE_POLICY,
    ClientFileResourcePolicy,
    canonical_client_file_resource,
)


class ProviderPolicyError(RuntimeError):
    pass


class ProviderSchemaError(RuntimeError):
    def __init__(self, dimension: str) -> None:
        super().__init__("MCP tool profile mismatch")
        self.dimension = dimension


class ClientProviderResultError(ToolException, RuntimeError):
    """A rejected client result that must be returned to the model as a tool error."""


class PermissionDecision(StrEnum):
    ALLOW_ONCE = "allow_once"
    REJECT_ONCE = "reject_once"
    CANCELLED = "cancelled"
    NOT_REQUESTED = "not_requested"


@dataclass(frozen=True)
class PermissionEligibility:
    tool_call_id: str
    title: str
    kind: str
    arguments: Mapping[str, Any]


@runtime_checkable
class PermissionBroker(Protocol):
    async def request_permission(
        self, eligibility: PermissionEligibility
    ) -> PermissionDecision: ...


@runtime_checkable
class ProviderConnection(Protocol):
    async def call_tool(
        self, name: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class ClientProvider(Protocol):
    async def connect(self, server_id: str) -> ProviderConnection: ...


@dataclass(frozen=True)
class ProviderDeclaration:
    name: str
    server_id: str
    cwd: str | None = None


@dataclass(frozen=True)
class ProviderToolPolicy:
    wrapper_name: str
    provider_name: str
    classification: str
    flow: str
    sink: str | None
    resource_namespace: str | None
    input_schema: Mapping[str, Any]
    result_schema: Mapping[str, Any]
    input_schema_digest: str
    result_schema_digest: str
    description: str


@dataclass(frozen=True)
class ProviderProfile:
    profile_id: str
    declaration_name: str
    provenance: str
    adapter: str
    tools: tuple[ProviderToolPolicy, ...]
    resource_policy: ClientFileResourcePolicy

    def tool(self, wrapper_name: str) -> ProviderToolPolicy | None:
        return next(
            (policy for policy in self.tools if policy.wrapper_name == wrapper_name),
            None,
        )


@dataclass(frozen=True)
class TurnCapabilityContext:
    permission_broker: PermissionBroker | None
    provider: ProviderConnection
    profile_policy: ProviderProfile | None
    connection_generation: int
    prompt_epoch: int
    acp_delivery: bool
    lease: Any
    cwd: str | None = None


_TURN_CAPABILITY_CONTEXT: ContextVar[TurnCapabilityContext | None] = ContextVar(
    "mimir_turn_capability_context", default=None
)


def set_turn_capability_context(
    context: TurnCapabilityContext,
) -> Token[TurnCapabilityContext | None]:
    return _TURN_CAPABILITY_CONTEXT.set(context)


def reset_turn_capability_context(token: Token[TurnCapabilityContext | None]) -> None:
    _TURN_CAPABILITY_CONTEXT.reset(token)


def get_turn_capability_context() -> TurnCapabilityContext | None:
    return _TURN_CAPABILITY_CONTEXT.get()


def _schema_digest(schema: Mapping[str, Any]) -> str:
    def thaw(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {key: thaw(value) for key, value in item.items()}
        if isinstance(item, tuple):
            return [thaw(value) for value in item]
        return item

    encoded = json.dumps(thaw(schema), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _tool_policy(
    *,
    wrapper_name: str,
    provider_name: str,
    classification: str,
    flow: str,
    sink: str | None,
    resource_namespace: str | None,
    input_schema: Mapping[str, Any],
    result_schema: Mapping[str, Any],
    description: str,
) -> ProviderToolPolicy:
    return ProviderToolPolicy(
        wrapper_name=wrapper_name,
        provider_name=provider_name,
        classification=classification,
        flow=flow,
        sink=sink,
        resource_namespace=resource_namespace,
        input_schema=input_schema,
        result_schema=result_schema,
        input_schema_digest=_schema_digest(input_schema),
        result_schema_digest=_schema_digest(result_schema),
        description=description,
    )


_WIRE_TO_POLICY = {
    "read": ("resource_scoped", "source", None, "client-file"),
    "edit": ("admin_required", "both", "external_mcp", "client-file"),
    "shell": ("admin_required", "both", "shell_process", None),
}


def _hands_policy_from_wire(descriptor: Mapping[str, Any]) -> ProviderToolPolicy:
    provider_name = descriptor["name"]
    classification, flow, sink, namespace = _WIRE_TO_POLICY[provider_name]
    return _tool_policy(
        wrapper_name=HANDS_PROVIDER_TO_WRAPPER[provider_name],
        provider_name=provider_name,
        classification=classification,
        flow=flow,
        sink=sink,
        resource_namespace=namespace,
        input_schema=descriptor["inputSchema"],
        result_schema=descriptor["outputSchema"],
        description=descriptor["description"],
    )

MIMIR_HANDS_V1 = ProviderProfile(
    profile_id="mimir.hands.v1",
    declaration_name="mimir-hands",
    provenance="mimir.server.provider-policy",
    adapter="acp-mcp",
    resource_policy=CLIENT_FILE_RESOURCE_POLICY,
    tools=tuple(_hands_policy_from_wire(descriptor) for descriptor in HANDS_V1_WIRE_TOOLS),
)

class ProviderRegistry:
    def __init__(self, profiles: tuple[ProviderProfile, ...]) -> None:
        by_name = {profile.declaration_name: profile for profile in profiles}
        if len(by_name) != len(profiles):
            raise ValueError("Provider declaration names must be unique")
        self._profiles: Mapping[str, ProviderProfile] = MappingProxyType(by_name)

    @property
    def profiles(self) -> tuple[ProviderProfile, ...]:
        return tuple(self._profiles.values())

    def resolve(self, declaration_name: str) -> ProviderProfile | None:
        return self._profiles.get(declaration_name)


PROVIDER_REGISTRY = ProviderRegistry((MIMIR_HANDS_V1,))
PROVIDER_PROFILES: Mapping[str, ProviderProfile] = MappingProxyType({
    profile.declaration_name: profile for profile in PROVIDER_REGISTRY.profiles
})


def get_provider_profile(declaration_name: str) -> ProviderProfile | None:
    return PROVIDER_REGISTRY.resolve(declaration_name)


def require_provider_profile(declaration_name: str) -> ProviderProfile:
    profile = get_provider_profile(declaration_name)
    if profile is None:
        raise ProviderPolicyError("Unknown client provider profile")
    return profile


def _active_policy(wrapper_name: str) -> tuple[TurnCapabilityContext, ProviderToolPolicy]:
    context = get_turn_capability_context()
    if context is None or context.provider is None:
        raise ToolException("Client provider is unavailable for this turn")
    if context.profile_policy is not MIMIR_HANDS_V1:
        raise ToolException("Client provider policy is missing or mismatched")
    if context.profile_policy.resource_policy is not CLIENT_FILE_RESOURCE_POLICY:
        raise ToolException("Client provider resource policy is missing or mismatched")
    lease = context.lease
    if lease is None or getattr(lease, "closed", False):
        raise ToolException("Client provider turn is closed")
    policy = context.profile_policy.tool(wrapper_name)
    if policy is None:
        raise ToolException("Client provider tool is not admitted")
    return context, policy


def _validate_result(result: Mapping[str, Any], wrapper_name: str) -> dict[str, Any]:
    provider_name = HANDS_WRAPPER_TO_PROVIDER[wrapper_name]
    try:
        return validate_tool_result(provider_name, result)
    except HandsContractError:
        raise ToolException(f"{wrapper_name} returned a malformed result")


class _ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class _EditArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    old_text: str
    new_text: str


class _ShellArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str


@tool("hands_read", args_schema=_ReadArgs)
async def hands_read(path: str) -> dict[str, str]:
    """Read client-hosted file content using the admitted hands provider."""
    context, policy = _active_policy("hands_read")
    resource = canonical_client_file_resource(path)
    if resource is None or not context.profile_policy.resource_policy.allows(resource):
        raise ToolException("hands_read path is not authorized")
    result = await context.provider.call_tool(policy.provider_name, {"path": path})
    return _validate_result(result, "hands_read")


@tool("hands_edit", args_schema=_EditArgs)
async def hands_edit(path: str, old_text: str, new_text: str) -> dict[str, bool]:
    """Replace exact text in a client-hosted file using the admitted hands provider."""
    context, policy = _active_policy("hands_edit")
    resource = canonical_client_file_resource(path)
    if resource is None or not context.profile_policy.resource_policy.allows(resource):
        raise ToolException("hands_edit path is not authorized")
    result = await context.provider.call_tool(
        policy.provider_name,
        {"path": path, "oldText": old_text, "newText": new_text},
    )
    return _validate_result(result, "hands_edit")


@tool("hands_shell", args_schema=_ShellArgs)
async def hands_shell(command: str) -> dict[str, str | int]:
    """Run a command using the admitted client-hosted hands provider."""
    context, policy = _active_policy("hands_shell")
    if not isinstance(command, str):
        raise ToolException("hands_shell command is malformed")
    result = await context.provider.call_tool(policy.provider_name, {"command": command})
    return _validate_result(result, "hands_shell")


HANDS_TOOLS = (hands_read, hands_edit, hands_shell)
