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
    _live_untrusted_active_ingest,
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
    ALLOW_SESSION = "allow_session"
    REJECT_ONCE = "reject_once"
    CANCELLED = "cancelled"
    NOT_REQUESTED = "not_requested"


@dataclass(frozen=True)
class PermissionEligibility:
    tool_call_id: str
    title: str
    kind: str
    arguments: Mapping[str, Any]
    host_execution: ClientAuthorizedHostExecution | None = None


_CLIENT_AUTHORIZED_HOST_EXECUTION_ISSUER = object()


@dataclass(frozen=True)
class ClientAuthorizedHostExecution:
    operation: str
    wrapper_name: str
    tainted: bool
    request_identity: Any
    auth_context_identity: Any
    capability_context_identity: Any
    policy_identity: Any
    provider_identity: Any
    permission_broker_identity: Any
    lease_identity: Any
    connection_generation: int
    prompt_epoch: int
    _issuer: Any


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
    operation: str | None


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
    operation: str | None,
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
        operation=operation,
    )


_WIRE_TO_POLICY = {
    "read": ("resource_scoped", "source", None, "client-file"),
    "edit": ("admin_required", "both", "external_mcp", "client-file"),
    "shell": ("admin_required", "both", "shell_process", None),
    "python": ("admin_required", "both", "shell_process", None),
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
        operation=(
            "client_authorized_host_execution"
            if classification == "admin_required"
            else None
        ),
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


def issue_client_authorized_host_execution(
    *,
    request_identity: Any,
    auth_context_identity: Any,
    wrapper_name: str,
    tainted: bool,
) -> ClientAuthorizedHostExecution | None:
    context = get_turn_capability_context()
    policy = context.profile_policy.tool(wrapper_name) if context is not None and context.profile_policy is MIMIR_HANDS_V1 else None
    lease = getattr(context, "lease", None)
    if (
        policy is None
        or policy.operation != "client_authorized_host_execution"
        or wrapper_name not in {"hands_edit", "hands_shell", "hands_python"}
        or not isinstance(tainted, bool)
        or auth_context_identity is None
        or getattr(auth_context_identity, "is_service", False)
        or "admin" not in (getattr(auth_context_identity, "roles", ()) or ())
        or not isinstance(getattr(auth_context_identity, "principal", None), str)
        or not getattr(auth_context_identity, "principal", "")
        or not isinstance(getattr(auth_context_identity, "canonical_principal", None), str)
        or not getattr(auth_context_identity, "canonical_principal", "")
        or context is None
        or context.acp_delivery is not True
        or context.provider is None
        or getattr(context.provider, "closed", False)
        or context.permission_broker is None
        or not callable(getattr(context.permission_broker, "request_permission", None))
        or lease is None
        or getattr(lease, "closed", True)
        or getattr(lease, "generation", None) != context.connection_generation
        or getattr(lease, "epoch", None) != context.prompt_epoch
    ):
        return None
    return ClientAuthorizedHostExecution(
        operation=policy.operation,
        wrapper_name=wrapper_name,
        tainted=tainted,
        request_identity=request_identity,
        auth_context_identity=auth_context_identity,
        capability_context_identity=context,
        policy_identity=policy,
        provider_identity=context.provider,
        permission_broker_identity=context.permission_broker,
        lease_identity=lease,
        connection_generation=context.connection_generation,
        prompt_epoch=context.prompt_epoch,
        _issuer=_CLIENT_AUTHORIZED_HOST_EXECUTION_ISSUER,
    )


def client_authorized_host_execution_matches(
    execution: Any,
    *,
    request_identity: Any,
    auth_context_identity: Any,
    wrapper_name: str,
) -> bool:
    context = get_turn_capability_context()
    if not isinstance(execution, ClientAuthorizedHostExecution) or context is None:
        return False
    policy = context.profile_policy.tool(wrapper_name) if context.profile_policy is MIMIR_HANDS_V1 else None
    lease = context.lease
    return (
        execution._issuer is _CLIENT_AUTHORIZED_HOST_EXECUTION_ISSUER
        and execution.operation == "client_authorized_host_execution"
        and execution.wrapper_name == wrapper_name
        and execution.request_identity is request_identity
        and execution.auth_context_identity is auth_context_identity
        and execution.capability_context_identity is context
        and execution.policy_identity is policy
        and execution.provider_identity is context.provider
        and execution.permission_broker_identity is context.permission_broker
        and execution.lease_identity is lease
        and execution.connection_generation == context.connection_generation
        and execution.prompt_epoch == context.prompt_epoch
        and policy is not None
        and policy.operation == execution.operation
        and context.acp_delivery is True
        and context.provider is not None
        and getattr(context.provider, "closed", False) is False
        and context.permission_broker is not None
        and lease is not None
        and getattr(lease, "closed", True) is False
        and getattr(lease, "generation", None) == context.connection_generation
        and getattr(lease, "epoch", None) == context.prompt_epoch
    )


def client_authorized_host_execution_metadata(
    execution: Any,
) -> tuple[str, bool] | None:
    if not isinstance(execution, ClientAuthorizedHostExecution):
        return None
    if not client_authorized_host_execution_matches(
        execution,
        request_identity=execution.request_identity,
        auth_context_identity=execution.auth_context_identity,
        wrapper_name=execution.wrapper_name,
    ):
        return None
    auth_context = execution.auth_context_identity
    state = getattr(auth_context, "ifc_state", None)
    current = getattr(state, "current", None)
    try:
        labels = current(auth_context.ifc_labels) if callable(current) else None
    except Exception:
        return None
    tainted = _live_untrusted_active_ingest(auth_context, labels)
    if not isinstance(tainted, bool):
        return None
    return execution.wrapper_name, tainted


def _validate_result(result: Mapping[str, Any], wrapper_name: str) -> dict[str, Any]:
    provider_name = HANDS_WRAPPER_TO_PROVIDER[wrapper_name]
    try:
        return validate_tool_result(provider_name, result)
    except HandsContractError:
        raise ToolException(f"{wrapper_name} returned a malformed result")


_HANDS_WRAPPER_ARGUMENTS = MappingProxyType({
    "hands_read": MappingProxyType({"path": "path"}),
    "hands_edit": MappingProxyType({
        "path": "path",
        "old_text": "oldText",
        "new_text": "newText",
    }),
    "hands_shell": MappingProxyType({"command": "command"}),
    "hands_python": MappingProxyType({"code": "code"}),
})


def validate_hands_wrapper_arguments(
    wrapper_name: str, arguments: Any,
) -> dict[str, str] | None:
    names = _HANDS_WRAPPER_ARGUMENTS.get(wrapper_name)
    policy = MIMIR_HANDS_V1.tool(wrapper_name)
    if names is None or policy is None or not isinstance(arguments, dict):
        return None
    required = policy.input_schema.get("required")
    properties = policy.input_schema.get("properties")
    if (
        set(arguments) != set(names)
        or set(names.values()) != set(required or ())
        or not isinstance(properties, Mapping)
        or set(properties) != set(names.values())
        or any(properties[provider_name].get("type") != "string" for provider_name in names.values())
        or any(not isinstance(arguments[wrapper_name], str) for wrapper_name in names)
    ):
        return None
    return dict(arguments)


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


class _PythonArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str


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


@tool("hands_python", args_schema=_PythonArgs)
async def hands_python(code: str) -> dict[str, bool | str]:
    """Execute Python code using the admitted client-hosted hands provider."""
    context, policy = _active_policy("hands_python")
    if not isinstance(code, str):
        raise ToolException("hands_python code is malformed")
    result = await context.provider.call_tool(policy.provider_name, {"code": code})
    return _validate_result(result, "hands_python")


HANDS_TOOLS = (hands_read, hands_edit, hands_shell, hands_python)
