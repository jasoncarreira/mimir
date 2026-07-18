"""Pure access-control policy for inbound and action authorization.

This module deliberately has no dispatcher or bridge side effects. Runtime
callers pass the inbound ``AgentEvent`` (or an author id), an optional
``IdentityResolver``, and an explicit enforcement flag; the policy returns a
structured decision suitable for logs and tool errors.

New in chainlink #865:
- OperationCatalog: stable open/admin-required/resource-scoped decisions for tools
- ServicePrincipal: explicit trusted-autonomous service entries with capabilities
- ToolAuthorization: runtime tool surface inventory with shadow-decision logging

New in chainlink #866:
- ChannelResourceAdapter: resource-scoped authorization for send_message/react/
  fetch_channel_history based on server-resolved triggering channel and bridge resources
- Same-scope operations (target matches triggering channel) pass; cross-channel/
  public/unknown operations require admin
- Structured redacted denials without relying on model-supplied channel fields
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable

from .identities import AccessMetadata

HTTP_EVENT_INGRESS_EXTRA_KEY = "_mimir_event_ingress"

if TYPE_CHECKING:
    from .identities import IdentityResolver
    from .models import AgentEvent, AuthContext

log = logging.getLogger(__name__)


class AccessTier(StrEnum):
    USER = "user"
    ADMIN = "admin"


class AccessStatus(StrEnum):
    LEGACY_ALLOWED = "legacy_allowed"
    USER_ALLOWED = "user_allowed"
    ADMIN_ALLOWED = "admin_allowed"
    DENIED = "denied"


class DenialReason(StrEnum):
    MISSING_AUTHOR = "missing_author"
    UNKNOWN_AUTHOR = "unknown_author"
    USER_NOT_ALLOWLISTED = "user_not_allowlisted"
    ADMIN_REQUIRED = "admin_required"


class OperationDecision(StrEnum):
    """Authorization decision for a tool/operation.

    - OPEN: operation is accessible to any authorized user (no admin required)
    - ADMIN_REQUIRED: operation requires admin role
    - RESOURCE_SCOPED: operation requires specific resource/domain capability
    - UNKNOWN: operation is unknown - denied by default when enforcement is on
    """
    OPEN = "open"
    ADMIN_REQUIRED = "admin_required"
    RESOURCE_SCOPED = "resource_scoped"
    UNKNOWN = "unknown"


class SinkCategory(StrEnum):
    """Sink categories for information flow control (chainlink #871).

    Used to determine which sinks are compatible with which IFC labels.
    """

    SAME_CHANNEL = "same_channel"
    CROSS_CHANNEL = "cross_channel"
    PUBLIC = "public"
    EXTERNAL_MCP = "external_mcp"
    HTTP_WEBHOOK = "http_webhook"
    SHELL_PROCESS = "shell_process"
    NETWORK = "network"
    SPAWN = "spawn"
    NOTIFICATION = "notification"
    FILE = "file"
    DIRECT_MESSAGE = "direct_message"
    UNKNOWN = "unknown"


_SINK_CATEGORY_MAP: dict[str, SinkCategory] = {
    "send_message": SinkCategory.SAME_CHANNEL,
    "react": SinkCategory.SAME_CHANNEL,
    "fetch_channel_history": SinkCategory.SAME_CHANNEL,
    # Harness-owned egress paths bypass model tool middleware, so they are
    # named explicitly and checked at their final send/edit boundary.
    "harness_auto_deliver": SinkCategory.SAME_CHANNEL,
    "harness_resend_nudge": SinkCategory.SAME_CHANNEL,
    "activity_panel_post": SinkCategory.SAME_CHANNEL,
    "activity_panel_edit": SinkCategory.SAME_CHANNEL,
    "post_message": SinkCategory.CROSS_CHANNEL,
    "webhook": SinkCategory.HTTP_WEBHOOK,
    "http_request": SinkCategory.HTTP_WEBHOOK,
    "fetch_url": SinkCategory.NETWORK,
    "web_search": SinkCategory.NETWORK,
    "shell_exec": SinkCategory.SHELL_PROCESS,
    "bash_async": SinkCategory.SHELL_PROCESS,
    "spawn_claude_code": SinkCategory.SPAWN,
    "spawn_codex": SinkCategory.SPAWN,
    "spawn_open_code": SinkCategory.SPAWN,
    "ntfy_send": SinkCategory.NOTIFICATION,
    "write_file": SinkCategory.FILE,
    "edit_file": SinkCategory.FILE,
    "mcp_": SinkCategory.EXTERNAL_MCP,
}


def get_sink_category(tool_name: str) -> SinkCategory:
    """Map a known egress operation to its sink category.

    Unknown operations are not presumed public: doing so would make a newly
    added harness send an implicit IFC bypass until the map was updated.
    """
    for prefix, category in _SINK_CATEGORY_MAP.items():
        if tool_name.startswith(prefix):
            return category
    return SinkCategory.UNKNOWN


@dataclass(frozen=True)
class ResourceScope:
    """Defines a specific resource/domain that an operation scopes to."""
    domain: str
    capabilities: frozenset[str] = frozenset()
    sink_destinations: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ServicePrincipal:
    """Trusted autonomous service principal (chainlink #865).

    Defined by server-owned creation path, trigger, capabilities,
    readable domains, and sink destinations. Unknown synthetic triggers
    receive no privilege.
    """
    canonical: str
    trigger: str
    capabilities: tuple[str, ...] = ()
    readable_domains: tuple[str, ...] = ()
    sink_destinations: tuple[str, ...] = ()
    creation_path: str | None = None

    def can_read_domain(self, domain: str) -> bool:
        return domain in self.readable_domains

    def can_write_sink(self, sink: str) -> bool:
        return sink in self.sink_destinations

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities


class SinkGate:
    """Information flow control sink gate (chainlink #871).

    Enforces that private/confidential data cannot flow to incompatible sinks.
    Unknown labels/destinations fail closed (deny).

    Propagation: Labels propagate to subagents, spawns, continuations, and
    resumed turns. Same-principal/same-channel flows pass only when every
    label is destination-compatible.
    """

    _global_resolver: Any = None

    @classmethod
    def set_identity_resolver(cls, resolver: Any) -> None:
        # PRODUCTION-DEAD (chainlink #895): Never called in production.
        # Retained for API stability; the resolver is not used by check_sink_flow.
        cls._global_resolver = resolver

    @classmethod
    def check_sink_flow(
        cls,
        tool_name: str,
        target: str | None,
        ifc_labels: Any,
        auth_context: Any,
        *,
        enforce: bool = False,
    ) -> "ToolAuthorization":
        """Check if IFC labels permit flow to the given sink.

        Args:
            tool_name: Name of the tool being called
            target: Target destination (channel, file path, URL, etc.)
            ifc_labels: InformationFlowLabels from the turn context
            auth_context: AuthContext with principal and roles
            enforce: Whether to enforce or allow in shadow mode

        Returns:
            ToolAuthorization with allowed/reason fields populated
        """
        from .models import InformationFlowLabels

        if not isinstance(ifc_labels, InformationFlowLabels):
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason="missing_ifc_labels",
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
            )

        sink_category = get_sink_category(tool_name)
        if sink_category == SinkCategory.UNKNOWN:
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason="unknown_sink_category",
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
            )

        service = get_trusted_service_from_auth_context(auth_context)
        service_sink = {
            SinkCategory.SHELL_PROCESS: "shell_process",
            SinkCategory.SPAWN: "spawn_process",
            SinkCategory.FILE: "filesystem",
            SinkCategory.NOTIFICATION: "notification",
            SinkCategory.HTTP_WEBHOOK: "network",
            SinkCategory.NETWORK: "network",
            SinkCategory.EXTERNAL_MCP: "external_mcp",
        }.get(sink_category)
        if (
            service is not None
            and service_sink is not None
            and service.can_write_sink(service_sink)
            # Poller payloads contain attacker-controlled external content, so
            # their service authority must not bypass IFC on active sinks.
            and not (
                "poller_payload" in service.readable_domains
                and sink_category in {
                    SinkCategory.SHELL_PROCESS,
                    SinkCategory.SPAWN,
                    SinkCategory.FILE,
                }
            )
        ):
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.OPEN,
                allowed=True,
                reason="ifc_service_sink_allowed",
                service_principal=service,
                enforcement_enabled=enforce,
            )

        if not target:
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason="unknown_sink_destination",
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
            )

        if not ifc_labels.labels:
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.OPEN,
                allowed=True,
                reason="no_labels",
                enforcement_enabled=enforce,
            )

        allowed_sinks = cls._get_allowed_sinks(
            sink_category, auth_context, ifc_labels=ifc_labels,
        )
        effective_target = (
            ChannelResourceAdapter._resolve_channel(target)
            if sink_category == SinkCategory.SAME_CHANNEL
            else target
        )

        can_flow = ifc_labels.can_flow_to(effective_target or "", allowed_sinks)

        if not can_flow:
            reason = f"ifc_label_blocked:{sink_category.value}"
            is_shadow = not enforce
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason=reason,
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=is_shadow,
            )

        return ToolAuthorization(
            tool_name=tool_name,
            decision=OperationDecision.OPEN,
            allowed=True,
            reason="ifc_allowed",
            enforcement_enabled=enforce,
        )

    @classmethod
    def _get_allowed_sinks(
        cls,
        category: SinkCategory,
        auth_context: Any,
        *,
        ifc_labels: Any,
    ) -> frozenset[str]:
        """Return concrete destinations compatible with every current label.

        Ordinary admin authority deliberately does not widen this set. Admins
        must use the distinct audited declassification action before egress.
        """
        if category != SinkCategory.SAME_CHANNEL or auth_context is None:
            return frozenset()

        triggering_channel = getattr(auth_context, "channel_id", None)
        if not triggering_channel:
            return frozenset()
        resolved_triggering = ChannelResourceAdapter._resolve_channel(triggering_channel)
        if not resolved_triggering:
            return frozenset()

        source_channels = getattr(ifc_labels, "source_channels", None)
        if not isinstance(source_channels, frozenset) or not source_channels:
            return frozenset()
        resolved_sources = {
            ChannelResourceAdapter._resolve_channel(channel)
            for channel in source_channels
            if channel
        }
        if not resolved_sources or None in resolved_sources:
            return frozenset()
        if resolved_sources != {resolved_triggering}:
            return frozenset()

        source_principals = getattr(ifc_labels, "source_principals", None)
        if source_principals:
            effective_principal = (
                getattr(auth_context, "canonical_principal", None)
                or getattr(auth_context, "principal", None)
            )
            if not effective_principal or source_principals != {effective_principal}:
                return frozenset()

        return frozenset({resolved_triggering})


def audit_declassification(
    labels: Any,
    declassification_reason: str,
    auth_context: Any,
) -> Any:
    """Audit admin declassification of IFC labels (chainlink #871).

    This is the ONLY way to remove immutable/monotonic labels. Summarization,
    model assertions, failures, and ordinary admin status do NOT erase labels.

    Args:
        labels: Current InformationFlowLabels
        declassification_reason: Human-readable reason for declassification
        auth_context: AuthContext with admin role

    Returns:
        New labels instance with declassification applied, or original if not admin
    """
    from .models import InformationFlowLabels

    if not isinstance(labels, InformationFlowLabels):
        return labels

    is_admin = False
    if auth_context is not None:
        roles = getattr(auth_context, "roles", ()) or ()
        is_admin = "admin" in roles

    if not is_admin:
        return labels

    log.info(
        "ifc_declassification",
        reason=declassification_reason,
        principal=getattr(auth_context, "principal", None),
    )

    return InformationFlowLabels(
        labels=frozenset(),
        source_channels=labels.source_channels,
        source_principals=labels.source_principals,
        source_domains=labels.source_domains,
        source_resources=labels.source_resources,
        source_bridges=labels.source_bridges,
        created_at=labels.created_at,
    )


class ChannelResourceAdapter:
    """Resource-scoped adapter for channel messaging tools (chainlink #866).

    Authorizes send_message/react/fetch_channel_history based on server-resolved
    triggering channel and bridge resources. Same-scope operations (target matches
    triggering channel) pass; cross-channel/public/unknown operations require admin.

    Key invariants:
    - Channel equality alone is not authority across bridge instances
    - Aliases resolve server-side via IdentityResolver
    - Cross-channel sends cannot inherit triggering-channel authority
    - Denials are structured and redacted without relying on model-supplied fields
    """

    _CHANNEL_OPERATIONS: frozenset[str] = frozenset({
        "send_message",
        "react",
        "fetch_channel_history",
    })

    _global_resolver: Any = None

    @classmethod
    def set_identity_resolver(cls, resolver: Any) -> None:
        cls._global_resolver = resolver

    @classmethod
    def get_decision(
        cls,
        tool_name: str,
        context: Any | None,
    ) -> OperationDecision | None:
        """Get resource-scoped decision for channel operations.

        Returns RESOURCE_SCOPED for channel operations, or None to fall through
        to catalog defaults.
        """
        if tool_name not in cls._CHANNEL_OPERATIONS:
            return None

        return OperationDecision.RESOURCE_SCOPED

    @classmethod
    def authorize_channel_operation(
        cls,
        tool_name: str,
        target_channel: str | None,
        auth_context: "AuthContext | None",
        *,
        enforce: bool = False,
    ) -> ToolAuthorization:
        """Authorize a channel operation against the triggering channel.

        Same-scope (target matches triggering channel after server-side resolution)
        passes for regular users. Cross-channel or unknown targets require admin.

        Args:
            tool_name: The channel operation (send_message/react/fetch_channel_history)
            target_channel: The model-supplied target channel (may be None/empty)
            auth_context: Server-created AuthContext with triggering channel
            enforce: Whether to enforce or allow in shadow mode

        Returns:
            ToolAuthorization with allowed/reason fields populated
        """
        if tool_name not in cls._CHANNEL_OPERATIONS:
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.UNKNOWN,
                allowed=False,
                reason="not_a_channel_operation",
            )

        triggering_channel = None
        if auth_context is not None:
            triggering_channel = getattr(auth_context, "channel_id", None)

        if not triggering_channel:
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.RESOURCE_SCOPED,
                allowed=not enforce,
                reason="missing_triggering_channel",
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
            )

        # Channel tools resolve an omitted/empty target to the current turn's
        # channel. Authorization must mirror that runtime behavior: an implicit
        # reply-to-trigger is same-scope, not a missing-resource denial.
        effective_target = target_channel or triggering_channel
        resolved_target = cls._resolve_channel(effective_target)
        resolved_triggering = cls._resolve_channel(triggering_channel)

        same_scope = resolved_target == resolved_triggering

        if same_scope:
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.RESOURCE_SCOPED,
                allowed=True,
                reason="same_scope_channel",
                enforcement_enabled=enforce,
            )

        is_admin = False
        if auth_context is not None:
            roles = getattr(auth_context, "roles", ()) or ()
            is_admin = "admin" in roles

        allowed = is_admin if enforce else True
        is_shadow = not enforce and not is_admin and not same_scope
        reason = "cross_channel_scope" if not is_admin else None

        return ToolAuthorization(
            tool_name=tool_name,
            decision=OperationDecision.RESOURCE_SCOPED,
            allowed=allowed,
            reason=reason,
            required_tier=AccessTier.ADMIN if not is_admin else AccessTier.USER,
            enforcement_enabled=enforce,
            is_shadow_decision=is_shadow,
        )

    @classmethod
    def _resolve_channel(cls, channel_id: str | None) -> str | None:
        """Resolve channel_id to canonical form using server-side IdentityResolver.

        Unknown channels fall through unchanged - this is intentional so that
        cross-channel operations to truly unknown channels require admin.
        """
        if not channel_id:
            return None

        if cls._global_resolver is not None:
            resolved = getattr(cls._global_resolver, "resolve_channel", None)
            if resolved:
                return resolved(channel_id)

        return channel_id


class OperationCatalog:
    """Catalog of tool/operation authorization decisions (chainlink #865).

    Replaces the old allow-through admin-name matching. Unknown native,
    built-in, dynamic, and external operations are never implicitly open -
    they are denied by default when enforcement is on.
    """

    _OPEN_OPERATIONS: frozenset[str] = frozenset({
        "list_channels",
        "list_schedules",
        "commitment_list",
        "memory_store",
        "memory_query",
        "memory_get",
        "saga_feedback",
        "saga_mark_contributions",
        "saga_end_session",
        "saga_record_skill_learning",
        "bash_jobs_list",
        "write_todos",
        "defer_injected_message",
        "commitment_complete",
        "commitment_snooze",
        "commitment_dismiss",
    })

    _ADMIN_REQUIRED_OPERATIONS: frozenset[str] = frozenset({
        "add_schedule",
        "set_schedule_priority",
        "remove_schedule",
        "reload_pollers",
        "open_proposal",
        "submit_proposal",
        "abandon_proposal",
        "request_mimir_update",
        "worklink_run",
        "shell_exec",
        "bash_async",
        "bash_job_output",
        "spawn_claude_code",
        "spawn_codex",
        "spawn_open_code",
        "task",
        "saga_forget",
        "write_file",
        "edit_file",
        "set_poller_overrides",
        "read_file",
        "aread",
        "ls",
        "als",
        "glob",
        "aglob",
        "grep",
        "agrep",
        "download_files",
        "adownload_files",
        "file_search",
        "rebuild_index",
        "get_turn",
        "mimir_get_turn",
    })

    _ADMIN_BUILTIN_TOOL_NAMES: frozenset[str] = frozenset({
        "Bash",
        "bash",
        "bash_exec",
        "execute",
        "aexecute",
        "shell",
        "Write",
        "Edit",
        "Read",
        "Glob",
        "Grep",
        "download_files",
    })

    def __init__(self) -> None:
        self._custom_decisions: dict[str, OperationDecision] = {}
        self._resource_scoped_operations: dict[str, list[ResourceScope]] = {}
        self._adapter_hooks: list[Callable[[str, Any], OperationDecision | None]] = []

    def register_operation(
        self,
        name: str,
        decision: OperationDecision,
        scopes: list[ResourceScope] | None = None,
    ) -> None:
        """Register a custom decision for an operation."""
        self._custom_decisions[name] = decision
        if decision == OperationDecision.RESOURCE_SCOPED and scopes:
            self._resource_scoped_operations[name] = scopes

    def register_adapter_hook(
        self,
        hook: Callable[[str, Any], OperationDecision | None],
    ) -> None:
        """Register an adapter hook for custom authorization logic.

        The hook receives (tool_name, context) and returns an OperationDecision
        or None to fall through to catalog defaults.
        """
        self._adapter_hooks.append(hook)

    def get_decision(
        self,
        tool_name: str,
        context: Any | None = None,
    ) -> OperationDecision:
        """Get the authorization decision for a tool.

        Order of resolution:
        1. Custom registered decisions
        2. Adapter hook results
        3. Built-in OPEN operations
        4. Built-in ADMIN_REQUIRED operations
        5. MCP name variations (admin required)
        6. Unknown operations -> UNKNOWN (fail closed when enforcement on)
        """
        if tool_name in self._custom_decisions:
            return self._custom_decisions[tool_name]

        for hook in self._adapter_hooks:
            result = hook(tool_name, context)
            if result is not None:
                return result

        if tool_name in self._OPEN_OPERATIONS:
            return OperationDecision.OPEN

        if tool_name in self._ADMIN_REQUIRED_OPERATIONS:
            return OperationDecision.ADMIN_REQUIRED

        if tool_name in self._ADMIN_BUILTIN_TOOL_NAMES:
            return OperationDecision.ADMIN_REQUIRED

        if any(
            tool_name.endswith(f"__{name}") or tool_name.endswith(f"_{name}")
            for name in self._ADMIN_REQUIRED_OPERATIONS
        ):
            return OperationDecision.ADMIN_REQUIRED

        return OperationDecision.UNKNOWN

    def get_scopes(
        self,
        tool_name: str,
    ) -> list[ResourceScope] | None:
        """Get resource scopes for a RESOURCE_SCOPED operation."""
        return self._resource_scoped_operations.get(tool_name)

    def is_known(self, tool_name: str) -> bool:
        """Check if a tool is known (has a non-UNKNOWN decision)."""
        return self.get_decision(tool_name) != OperationDecision.UNKNOWN


_global_operation_catalog = OperationCatalog()

_global_operation_catalog.register_adapter_hook(
    ChannelResourceAdapter.get_decision,
)


class MCPResourceAdapter:
    """MCP tool resource adapter for authorization (chainlink #870).

    Handles MCP tool classification:
    - Missing provenance -> ADMIN_REQUIRED
    - Tombstoned (drifted) provenance -> ADMIN_REQUIRED
    - Unclassified MCP tools -> ADMIN_REQUIRED
    - Resource-scoped classification requires registered adapter

    This ensures bare regular-scoped tier cannot authorize arbitrary
    MCP arguments without proper classification and provenance.
    """

    _MCP_TOOL_PREFIX = "mcp_"
    _global_resolver: Any = None

    @classmethod
    def set_identity_resolver(cls, resolver: Any) -> None:
        # PRODUCTION-DEAD (chainlink #895): Never called in production.
        # Retained for API stability; the resolver is not used by get_decision.
        cls._global_resolver = resolver

    @classmethod
    def get_decision(
        cls,
        tool_name: str,
        context: Any | None,
    ) -> OperationDecision | None:
        """Get decision for MCP tools.

        Returns ADMIN_REQUIRED for MCP tools that have no provenance,
        tombstoned provenance, or no matching registered classifier.
        A registered classifier supplies the explicit OPEN,
        RESOURCE_SCOPED, or ADMIN_REQUIRED decision.
        Returns None for non-MCP tools to fall through to other adapters.
        """
        if not tool_name.startswith(cls._MCP_TOOL_PREFIX):
            return None

        provenance = cls._get_provenance_from_context(tool_name, context)

        if provenance is None:
            log.debug(
                "MCP tool %s has no provenance - requiring admin", tool_name
            )
            return OperationDecision.ADMIN_REQUIRED

        if provenance.is_tombstoned:
            log.warning(
                "MCP tool %s has tombstoned provenance (drift detected) - requiring admin",
                tool_name,
            )
            return OperationDecision.ADMIN_REQUIRED

        adapter = cls._get_registered_adapter(provenance)
        if adapter is None:
            log.debug(
                "MCP tool %s has no matching registered adapter - requiring admin",
                tool_name,
            )
            return OperationDecision.ADMIN_REQUIRED

        try:
            decision = adapter.classify(tool_name, context)
        except Exception:
            log.exception(
                "MCP adapter %s failed while classifying %s - requiring admin",
                provenance.adapter_name,
                tool_name,
            )
            return OperationDecision.ADMIN_REQUIRED

        if not isinstance(decision, OperationDecision):
            log.error(
                "MCP adapter %s returned invalid decision for %s - requiring admin",
                provenance.adapter_name,
                tool_name,
            )
            return OperationDecision.ADMIN_REQUIRED
        return decision

    @staticmethod
    def _get_registered_adapter(provenance: Any) -> Any | None:
        """Resolve only the adapter registration named by preserved provenance."""
        adapter_name = getattr(provenance, "adapter_name", "")
        adapter_version = getattr(provenance, "adapter_version", "")
        policy_version = getattr(provenance, "policy_version", "")
        if not adapter_name or not adapter_version or not policy_version:
            return None

        from .mcp_client import get_mcp_adapter_info

        adapter = get_mcp_adapter_info(adapter_name)
        if adapter is None:
            return None
        if adapter.version != adapter_version:
            return None
        if adapter.policy_version != policy_version:
            return None
        return adapter

    @classmethod
    def _get_provenance_from_context(
        cls,
        tool_name: str,
        context: Any | None,
    ) -> Any | None:
        """Extract MCP provenance from auth context or tool registry."""
        if context is not None:
            provenance = getattr(context, "mcp_provenance", None)
            if provenance is not None:
                return provenance

        try:
            from .mcp_client import get_tool_provenance
            from .tools.mcp import get_mcp_tools

            for tool in get_mcp_tools():
                if getattr(tool, "name", None) == tool_name:
                    return get_tool_provenance(tool)
        except Exception:
            pass

        return None

    @classmethod
    def authorize_mcp_tool(
        cls,
        tool_name: str,
        context: Any | None,
        *,
        enforce: bool = False,
    ) -> "ToolAuthorization":
        """Authorize an MCP tool call with full provenance checking.

        Args:
            tool_name: The namespaced MCP tool name (e.g., 'mcp_github_search')
            context: AuthContext with provenance if available
            enforce: Whether to enforce or allow in shadow mode

        Returns:
            ToolAuthorization with decision and reason fields populated
        """
        decision = cls.get_decision(tool_name, context)

        if decision is None:
            if not tool_name.startswith(cls._MCP_TOOL_PREFIX):
                return ToolAuthorization(
                    tool_name=tool_name,
                    decision=OperationDecision.ADMIN_REQUIRED,
                    allowed=False,
                    reason="non_mcp_tool_name",
                    enforcement_enabled=enforce,
                    is_shadow_decision=not enforce,
                )
            decision = OperationDecision.ADMIN_REQUIRED

        provenance = cls._get_provenance_from_context(tool_name, context)

        allowed = decision != OperationDecision.ADMIN_REQUIRED or not enforce

        reason = None
        if decision == OperationDecision.ADMIN_REQUIRED:
            if provenance is None:
                reason = "mcp_missing_provenance"
            elif provenance.is_tombstoned:
                reason = "mcp_drift_detected"
            else:
                reason = "mcp_unclassified"

        return ToolAuthorization(
            tool_name=tool_name,
            decision=decision,
            allowed=allowed,
            reason=reason,
            enforcement_enabled=enforce,
            is_shadow_decision=not enforce and not allowed,
        )


_global_operation_catalog.register_adapter_hook(
    MCPResourceAdapter.get_decision,
)


def get_operation_catalog() -> OperationCatalog:
    """Get the global operation catalog instance."""
    return _global_operation_catalog


@dataclass
class ToolAuthorization:
    """Authorization decision for a tool call (chainlink #865).

    Carries the tool name, operation decision, service principal context,
    and shadow-decision audit fields.
    """
    tool_name: str
    decision: OperationDecision
    allowed: bool
    reason: str | None = None
    service_principal: ServicePrincipal | None = None
    required_tier: AccessTier = AccessTier.USER
    enforcement_enabled: bool = False
    is_shadow_decision: bool = False

    def as_log_fields(self) -> dict[str, Any]:
        """Return fields for audit logging."""
        return {
            "tool": self.tool_name,
            "decision": self.decision.value,
            "allowed": self.allowed,
            "reason": self.reason,
            "required_tier": self.required_tier.value,
            "service_principal": self.service_principal.canonical if self.service_principal else None,
            "enforcement_enabled": self.enforcement_enabled,
            "is_shadow_decision": self.is_shadow_decision,
        }


def _consume_task_exception(task: Any) -> None:
    """Retrieve background logging failures so asyncio does not warn."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("shadow decision logging failed", exc_info=exc)


class ToolRegistry:
    """Registry of runtime tools for inventory and authorization (chainlink #865).

    Maintains an executable inventory of the final assembled runtime tool surface.
    Supports shadow-decision audit logging when compatibility enforcement is off.
    """

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}
        self._shadow_logging_enabled: bool = False

    def register_tool(
        self,
        name: str,
        *,
        description: str | None = None,
        category: str | None = None,
        is_native: bool = False,
        is_builtin: bool = False,
        is_dynamic: bool = False,
        is_external: bool = False,
    ) -> None:
        """Register a tool in the runtime inventory."""
        self._tools[name] = {
            "name": name,
            "description": description,
            "category": category,
            "is_native": is_native,
            "is_builtin": is_builtin,
            "is_dynamic": is_dynamic,
            "is_external": is_external,
        }

    def unregister_tool(self, name: str) -> None:
        """Remove a tool from the inventory."""
        self._tools.pop(name, None)

    def get_tool(self, name: str) -> dict[str, Any] | None:
        """Get tool metadata from inventory."""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """List all registered tool names."""
        return list(self._tools.keys())

    def clear(self) -> None:
        """Clear the inventory before registering a newly assembled surface."""
        self._tools.clear()

    def register_runtime_tools(self, tools: Any) -> None:
        """Atomically replace inventory from a model-bound runtime tool sequence.

        Authorization does not consult this observational inventory.  Callers
        that maintain inventory may therefore publish a complete snapshot
        without creating a transient empty or partially populated surface.
        """
        runtime_tools: dict[str, dict[str, Any]] = {}
        for tool in tools or ():
            name = getattr(tool, "name", None)
            if not isinstance(name, str) or not name:
                continue
            runtime_tools[name] = {
                "name": name,
                "description": getattr(tool, "description", None),
                "category": "runtime",
                "is_native": False,
                "is_builtin": False,
                "is_dynamic": False,
                "is_external": False,
            }
        self._tools = runtime_tools

    def list_by_category(self, category: str) -> list[str]:
        """List tools in a specific category."""
        return [
            name for name, meta in self._tools.items()
            if meta.get("category") == category
        ]

    @property
    def tool_count(self) -> int:
        """Total number of registered tools."""
        return len(self._tools)

    def enable_shadow_logging(self) -> None:
        """Enable shadow-decision audit logging."""
        self._shadow_logging_enabled = True

    def disable_shadow_logging(self) -> None:
        """Disable shadow-decision audit logging."""
        self._shadow_logging_enabled = False

    @property
    def is_shadow_logging_enabled(self) -> bool:
        """Check if shadow logging is enabled."""
        return self._shadow_logging_enabled

    def _emit_shadow_decision(
        self,
        auth: ToolAuthorization,
    ) -> None:
        """Emit shadow-decision audit log (when enabled)."""
        if not self._shadow_logging_enabled:
            return
        try:
            from .event_logger import log_event
            import asyncio
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                log_event("shadow_tool_decision", **auth.as_log_fields())
            )
            task.add_done_callback(_consume_task_exception)
        except RuntimeError:
            log.debug("shadow decision logging skipped: no running event loop")

    def authorize_tool(
        self,
        tool_name: str,
        auth_context: "AuthContext | None" = None,
        *,
        enforce: bool = False,
        target_channel: str | None = None,
        ifc_labels: Any = None,
    ) -> ToolAuthorization:
        """Authorize a tool call using the operation catalog.

        When enforce=False (legacy mode), unknown operations are allowed but
        logged as shadow decisions. When enforce=True, unknown operations
        are denied.

        For channel operations (send_message, react, fetch_channel_history),
        resource-scoped authorization always compares the effective target against
        the triggering channel. An omitted target means reply-to-trigger.

        The ifc_labels parameter enables information flow control sink gate
        checks (chainlink #871).
        """
        sink_category = get_sink_category(tool_name)
        if ifc_labels is None and auth_context is not None:
            ifc_labels = getattr(auth_context, "ifc_labels", None)
        catalog = get_operation_catalog()
        preliminary_decision = catalog.get_decision(tool_name, auth_context)
        preliminary_service = (
            get_trusted_service_from_auth_context(auth_context)
            if auth_context is not None
            else None
        )
        service_capability_denied = (
            preliminary_service is not None
            and preliminary_decision == OperationDecision.ADMIN_REQUIRED
            and not preliminary_service.has_capability(tool_name)
        )
        service_allowed_preliminary = (
            preliminary_service is not None
            and preliminary_service.has_capability(tool_name)
        )
        preliminary_admin_denied = (
            preliminary_decision == OperationDecision.ADMIN_REQUIRED
            and not service_allowed_preliminary
            and "admin" not in (
                (getattr(auth_context, "roles", ()) or ()) if auth_context else ()
            )
        )
        trigger = getattr(auth_context, "trigger", None) if auth_context else None
        attempted_service = (
            trigger is not None
            and trigger in _TRUSTED_SERVICE_PRINCIPALS
            and preliminary_service is None
        )
        sink_target = target_channel
        if (
            sink_category == SinkCategory.SAME_CHANNEL
            and not sink_target
            and auth_context is not None
        ):
            sink_target = getattr(auth_context, "channel_id", None)
        is_ifc_sink = sink_category != SinkCategory.UNKNOWN or (
            ifc_labels is not None
            and preliminary_decision == OperationDecision.UNKNOWN
            and not service_allowed_preliminary
        )
        if (
            is_ifc_sink
            and not service_capability_denied
            and not attempted_service
        ):
            sink_check = SinkGate.check_sink_flow(
                tool_name,
                sink_target,
                ifc_labels,
                auth_context,
                enforce=enforce,
            )
            if not sink_check.allowed and enforce and not preliminary_admin_denied:
                return sink_check
            if sink_check.is_shadow_decision:
                self._emit_shadow_decision(sink_check)

        decision = preliminary_decision
        service_principal = None

        if auth_context is not None:
            service_principal = get_trusted_service_from_auth_context(auth_context)

        required_tier = AccessTier.USER
        reason = None
        is_shadow = False
        service_allowed = (
            service_principal is not None
            and service_principal.has_capability(tool_name)
        )

        if decision == OperationDecision.OPEN:
            allowed = True
        elif decision == OperationDecision.ADMIN_REQUIRED:
            required_tier = AccessTier.ADMIN
            if auth_context and "admin" in (getattr(auth_context, "roles", ()) or ()):
                allowed = True
            elif service_allowed:
                allowed = True
                is_shadow = not enforce
            elif enforce:
                allowed = False
                reason = "admin_required"
            else:
                allowed = True
                is_shadow = True
        elif decision == OperationDecision.RESOURCE_SCOPED:
            if tool_name in ChannelResourceAdapter._CHANNEL_OPERATIONS:
                channel_auth = ChannelResourceAdapter.authorize_channel_operation(
                    tool_name,
                    target_channel,
                    auth_context,
                    enforce=enforce,
                )
                return channel_auth
            required_tier = AccessTier.ADMIN
            if enforce:
                allowed = False
                reason = "resource_scoped"
            else:
                allowed = True
                is_shadow = True
        else:
            # Explicit service capabilities are authoritative even if a newly
            # added operation has not reached the catalog yet. This is a narrow
            # exception to UNKNOWN's ordinary fail-closed rule: capabilities
            # are fixed per trusted service principal, not inferred from the
            # runtime inventory or supplied by the caller.
            if service_allowed:
                allowed = True
            elif enforce:
                allowed = False
                reason = "unknown_operation"
            else:
                allowed = True
                is_shadow = True

        auth = ToolAuthorization(
            tool_name=tool_name,
            decision=decision,
            allowed=allowed,
            reason=reason,
            service_principal=service_principal,
            required_tier=required_tier,
            enforcement_enabled=enforce,
            is_shadow_decision=is_shadow,
        )

        if is_shadow:
            self._emit_shadow_decision(auth)

        return auth


_global_tool_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    """Get the global tool registry instance."""
    return _global_tool_registry


_TRUSTED_SERVICE_PRINCIPALS: dict[str, ServicePrincipal] = {
    service.trigger: service
    for service in (
        ServicePrincipal(
            canonical="scheduler",
            trigger="scheduled_tick",
            capabilities=(
                "shell_exec",
                "bash_async",
                "spawn_claude_code",
                "spawn_codex",
                "spawn_open_code",
                "task",
                "saga_forget",
                "write_file",
                "edit_file",
                "open_proposal",
                "submit_proposal",
                "abandon_proposal",
                "worklink_run",
                "read_file",
                "aread",
                "ls",
                "als",
                "glob",
                "aglob",
                "grep",
                "agrep",
                "file_search",
                "get_turn",
                "mimir_get_turn",
            ),
            readable_domains=("configured_inputs", "filesystem", "turn_history"),
            sink_destinations=(
                "configured_channel",
                "filesystem",
                "shell_process",
                "spawn_process",
                "proposal",
                "saga",
                "worklink",
            ),
            creation_path="mimir.scheduler.Scheduler._fire_job",
        ),
        ServicePrincipal(
            canonical="poller",
            trigger="poller",
            capabilities=(
                "shell_exec",
                "bash_async",
                "spawn_claude_code",
                "spawn_codex",
                "spawn_open_code",
                "task",
                "write_file",
                "edit_file",
                "open_proposal",
                "submit_proposal",
                "abandon_proposal",
                "worklink_run",
                "read_file",
                "aread",
                "ls",
                "als",
                "glob",
                "aglob",
                "grep",
                "agrep",
                "file_search",
                "get_turn",
                "mimir_get_turn",
                "send_message",
            ),
            readable_domains=("poller_payload", "filesystem", "turn_history"),
            sink_destinations=(
                "configured_channel",
                "filesystem",
                "shell_process",
                "spawn_process",
                "proposal",
                "worklink",
                "message",
            ),
            creation_path="mimir.pollers.run_poller",
        ),
        ServicePrincipal(
            canonical="synthesis",
            trigger="saga_session_end",
            capabilities=(
                "saga_end_session",
                "saga_mark_contributions",
                "saga_feedback",
                "saga_record_skill_learning",
                "memory_get",
                "memory_store",
                "mimir_get_turn",
                "get_turn",
                "read_file",
                "aread",
                "ls",
                "als",
                "glob",
                "aglob",
                "grep",
                "agrep",
                "write_file",
                "edit_file",
            ),
            readable_domains=("session", "saga", "filesystem", "turn_history"),
            sink_destinations=("session_boundary", "saga", "filesystem"),
            creation_path="mimir.server._on_session_idle",
        ),
        ServicePrincipal(
            canonical="system",
            trigger="upgrade",
            capabilities=(
                "shell_exec",
                "bash_async",
                "write_file",
                "edit_file",
                "open_proposal",
                "submit_proposal",
                "abandon_proposal",
                "add_schedule",
                "set_schedule_priority",
                "read_file",
                "aread",
                "ls",
                "als",
                "glob",
                "aglob",
                "grep",
                "agrep",
                "send_message",
            ),
            readable_domains=("defaults", "proposal", "filesystem"),
            sink_destinations=(
                "operator_alert",
                "filesystem",
                "shell_process",
                "proposal",
                "scheduler",
                "message",
            ),
            creation_path="mimir.defaults_upgrade.enqueue_upgrade_prompt_turns",
        ),
    )
}


def register_service_principal(service: ServicePrincipal) -> None:
    """Register a trusted autonomous service principal."""
    _TRUSTED_SERVICE_PRINCIPALS[service.trigger] = service


_REQUIRED_SERVICE_PRINCIPALS: frozenset[str] = frozenset({
    "scheduled_tick",
    "poller",
    "saga_session_end",
    "upgrade",
})


# Executable capabilities and information-flow metadata are one policy.
_OPERATION_READABLE_DOMAIN: dict[str, str] = {
    "read_file": "filesystem",
    "aread": "filesystem",
    "ls": "filesystem",
    "als": "filesystem",
    "glob": "filesystem",
    "aglob": "filesystem",
    "grep": "filesystem",
    "agrep": "filesystem",
    "file_search": "filesystem",
    "get_turn": "turn_history",
    "mimir_get_turn": "turn_history",
    "memory_query": "saga",
    "memory_get": "saga",
}

_OPERATION_SINK_DESTINATION: dict[str, str] = {
    "write_file": "filesystem",
    "edit_file": "filesystem",
    "shell_exec": "shell_process",
    "bash_async": "shell_process",
    "spawn_claude_code": "spawn_process",
    "spawn_codex": "spawn_process",
    "open_proposal": "proposal",
    "submit_proposal": "proposal",
    "abandon_proposal": "proposal",
    "add_schedule": "scheduler",
    "set_schedule_priority": "scheduler",
    "saga_feedback": "saga",
    "saga_mark_contributions": "saga",
    "saga_record_skill_learning": "saga",
    "saga_forget": "saga",
    "memory_store": "saga",
    "send_message": "message",
    "saga_end_session": "session_boundary",
    "worklink_run": "worklink",
}


class CapabilityMatrixError(Exception):
    """Raised when enforcement is requested with an incomplete matrix."""


class ProviderEnforcementCompatibilityError(Exception):
    """Raised when the active model provider cannot safely enforce authz."""


def _capability_matrix_errors() -> list[str]:
    errors: list[str] = []
    for trigger in sorted(_REQUIRED_SERVICE_PRINCIPALS):
        principal = _TRUSTED_SERVICE_PRINCIPALS.get(trigger)
        if principal is None:
            errors.append(f"Missing service principal for trigger: {trigger}")
            continue
        if principal.trigger != trigger:
            errors.append(
                f"Service principal '{principal.canonical}' is registered for "
                f"{trigger} but declares trigger {principal.trigger}"
            )
        if not principal.capabilities:
            errors.append(
                f"Service principal '{principal.canonical}' ({trigger}) "
                "has no capabilities defined"
            )
        if not principal.readable_domains:
            errors.append(
                f"Service principal '{principal.canonical}' ({trigger}) "
                "has no readable domains defined"
            )
        if not principal.sink_destinations:
            errors.append(
                f"Service principal '{principal.canonical}' ({trigger}) "
                "has no sink destinations defined"
            )

        readable_domains = set(principal.readable_domains)
        sink_destinations = set(principal.sink_destinations)
        for operation in sorted(set(principal.capabilities)):
            required_domain = _OPERATION_READABLE_DOMAIN.get(operation)
            if required_domain and required_domain not in readable_domains:
                errors.append(
                    f"Service principal '{principal.canonical}' capability "
                    f"'{operation}' requires readable domain '{required_domain}'"
                )
            required_sink = _OPERATION_SINK_DESTINATION.get(operation)
            if required_sink and required_sink not in sink_destinations:
                errors.append(
                    f"Service principal '{principal.canonical}' capability "
                    f"'{operation}' requires sink destination '{required_sink}'"
                )
    return errors


def check_capability_matrix_complete(
    fail_closed: bool = True,
) -> tuple[bool, list[str]]:
    """Verify required principals and capability/domain/sink consistency.

    When fail_closed=True (default), returns (False, errors) if any errors exist.
    When fail_closed=False, still returns (False, errors) if errors exist - the
    fail_closed parameter only controls whether an exception is raised in the
    assert_capability_matrix_complete() variant. A matrix with errors is never
    considered complete, regardless of fail_closed setting.
    """
    errors = _capability_matrix_errors()
    if errors:
        for error in errors:
            log.warning("capability_matrix_incomplete: %s", error)
        return (False, errors)
    return (True, [])


def assert_capability_matrix_complete() -> None:
    """Raise unless the enforcement matrix is complete and consistent."""
    errors = _capability_matrix_errors()
    if errors:
        raise CapabilityMatrixError(
            "Access-control enforcement blocked by incomplete capability matrix: "
            + "; ".join(errors)
        )


def resolve_access_control_enforcement(
    requested: bool,
    *,
    model_spec: str | None = None,
) -> bool:
    """Fail closed at the enforcement enablement boundary.

    Claude Code executes tools in an SDK subprocess whose hook API does not
    carry Mimir's server-created per-turn ``AuthContext``. Refuse this provider
    combination at startup rather than enabling enforcement that denies every
    non-open subprocess tool and leaves the agent unusable.
    """
    if requested:
        provider = (model_spec or "").partition(":")[0].strip().lower().replace("_", "-")
        if provider == "claude-code":
            raise ProviderEnforcementCompatibilityError(
                "MIMIR_ACCESS_CONTROL_ENFORCED=true is incompatible with "
                f"MIMIR_MODEL_SPEC={model_spec!r}: the claude-code subprocess "
                "tool hook cannot receive Mimir's server-created per-turn "
                "AuthContext. Disable enforcement or select anthropic:, openai:, "
                "or codex-plus:."
            )
        assert_capability_matrix_complete()
    return requested


def get_capability_matrix_report() -> dict[str, dict[str, Any]]:
    """Generate a report of the current capability matrix for audit purposes.

    Returns:
        A dictionary mapping trigger names to their principal configuration.
    """
    report: dict[str, dict[str, Any]] = {}
    for trigger, principal in _TRUSTED_SERVICE_PRINCIPALS.items():
        report[trigger] = {
            "canonical": principal.canonical,
            "capabilities": list(principal.capabilities),
            "readable_domains": list(principal.readable_domains),
            "sink_destinations": list(principal.sink_destinations),
            "creation_path": principal.creation_path,
        }
    return report


def get_service_principal(trigger: str) -> ServicePrincipal | None:
    """Get a service principal by trigger."""
    return _TRUSTED_SERVICE_PRINCIPALS.get(trigger)


def is_admin(auth_context: Any) -> bool:
    """Check if the auth context has admin role."""
    if auth_context is None:
        return False
    roles = getattr(auth_context, "roles", None)
    if not roles:
        return False
    return "admin" in roles


def get_trusted_service_from_auth_context(
    auth_context: Any,
) -> ServicePrincipal | None:
    """Resolve a registered service from the server-owned auth carrier.

    Service authority exists only for internally-created events: public HTTP
    ingress is stamped in ``event_ingress`` and therefore cannot gain service
    authority merely by choosing a registered trigger string.
    """
    if auth_context is None or getattr(auth_context, "event_ingress", None) is not None:
        return None
    if not getattr(auth_context, "is_service", False):
        return None
    trigger = getattr(auth_context, "trigger", None)
    if not isinstance(trigger, str):
        return None
    service = _TRUSTED_SERVICE_PRINCIPALS.get(trigger)
    if service is None or getattr(auth_context, "canonical_principal", None) != service.canonical:
        return None
    return service


def is_trusted_service(auth_context: Any) -> bool:
    """Check whether the exact auth carrier maps to a trusted service."""
    return get_trusted_service_from_auth_context(auth_context) is not None


def can_write_saga(auth_context: Any) -> bool:
    """Check if the auth context can write to SAGA (memory_store, saga_end_session).

    Writes are allowed for:
    - Admin users
    - Trusted service principals

    Regular users cannot write to shared memory.
    """
    return is_admin(auth_context) or is_trusted_service(auth_context)


def get_provenance_from_auth_context(
    auth_context: Any,
) -> dict[str, Any]:
    """Extract provenance metadata from a frozen AuthContext.

    Returns a dict with:
    - created_by: canonical principal or service name
    - trigger: the event trigger
    - event_ingress: server-owned ingress point
    - is_service: whether this is a service principal
    """
    if auth_context is None:
        return {}
    service = get_trusted_service_from_auth_context(auth_context)
    created_by = (
        f"service:{service.canonical}"
        if service is not None
        else getattr(auth_context, "canonical_principal", None)
        or getattr(auth_context, "principal", None)
    )
    return {
        "created_by": created_by,
        "trigger": getattr(auth_context, "trigger", None),
        "event_ingress": getattr(auth_context, "event_ingress", None),
        "is_service": service is not None,
    }


def _find_service_principal_for_trigger(trigger: str) -> ServicePrincipal | None:
    """Find a service principal that matches the given trigger."""
    return _TRUSTED_SERVICE_PRINCIPALS.get(trigger)


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    status: AccessStatus
    required_tier: AccessTier
    reason: DenialReason | None = None
    author: str | None = None
    canonical_author: str | None = None
    roles: tuple[str, ...] = ()
    enforcement_enabled: bool = False

    @property
    def denial_reason(self) -> str | None:
        return self.reason.value if self.reason else None

    def as_log_fields(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "status": self.status.value,
            "required_tier": self.required_tier.value,
            "denial_reason": self.denial_reason,
            "author": self.author,
            "canonical_author": self.canonical_author,
            "roles": list(self.roles),
            "enforcement_enabled": self.enforcement_enabled,
        }


def _author_from_event(event: "AgentEvent | str | None") -> str | None:
    if event is None or isinstance(event, str):
        return event
    return event.author


def _metadata_for(
    author: str | None,
    resolver: "IdentityResolver | None",
) -> tuple[str | None, bool, AccessMetadata]:
    if author is None:
        return None, False, AccessMetadata()
    if resolver is None:
        return author, False, AccessMetadata()
    canonical = resolver.resolve(author)
    return (
        canonical,
        resolver.identity(author) is not None,
        resolver.access_metadata(author),
    )


def authorize(
    event_or_author: "AgentEvent | str | None",
    resolver: "IdentityResolver | None" = None,
    *,
    required_tier: AccessTier | str = AccessTier.USER,
    enforce: bool = False,
) -> AccessDecision:
    """Authorize an event/author for a user or admin tier.

    ``enforce=False`` is the backwards-compatible default: the decision is
    allowed even if the author is unknown or lacks roles, while still carrying
    the stable reason that enforcement would use.
    """
    tier = AccessTier(required_tier)
    author = _author_from_event(event_or_author)
    canonical, known_identity, access = _metadata_for(author, resolver)
    roles = access.roles

    reason: DenialReason | None = None
    if author is None:
        reason = DenialReason.MISSING_AUTHOR
    elif resolver is not None and not known_identity:
        reason = DenialReason.UNKNOWN_AUTHOR
    elif not access.is_authorized:
        reason = DenialReason.USER_NOT_ALLOWLISTED
    elif tier == AccessTier.ADMIN and not access.is_admin:
        reason = DenialReason.ADMIN_REQUIRED

    allowed = reason is None or not enforce
    if reason is None:
        status = (
            AccessStatus.ADMIN_ALLOWED
            if access.is_admin
            else AccessStatus.USER_ALLOWED
        )
    elif not enforce:
        status = AccessStatus.LEGACY_ALLOWED
    else:
        status = AccessStatus.DENIED

    return AccessDecision(
        allowed=allowed,
        status=status,
        required_tier=tier,
        reason=reason,
        author=author,
        canonical_author=canonical,
        roles=roles,
        enforcement_enabled=enforce,
    )


def authorize_inbound(
    event: "AgentEvent",
    resolver: "IdentityResolver | None" = None,
    *,
    enforce: bool = False,
) -> AccessDecision:
    """Authorize an inbound event at the normal allowlisted-user tier."""
    return authorize(event, resolver, required_tier=AccessTier.USER, enforce=enforce)


def authorize_action(
    event_or_author: "AgentEvent | str | None",
    resolver: "IdentityResolver | None" = None,
    *,
    admin: bool = False,
    enforce: bool = False,
) -> AccessDecision:
    """Authorize an action-tier operation.

    Set ``admin=True`` for operator/admin-only actions; otherwise the action
    requires ordinary allowlisted user access.
    """
    tier = AccessTier.ADMIN if admin else AccessTier.USER
    return authorize(event_or_author, resolver, required_tier=tier, enforce=enforce)


def create_auth_context(
    event: "AgentEvent",
    resolver: "IdentityResolver | None" = None,
    policy_version: str | None = None,
    *,
    enforce: bool = False,
    event_ingress: str | None = None,
    ifc_labels: "InformationFlowLabels | None" = None,
) -> "AuthContext":
    """Create a frozen AuthContext from an inbound event (chainlink #864).

    This is the server-owned authorization carrier created at ingress BEFORE
    model execution. It carries immutable authorization state that cannot be
    widened or mutated by the model, tools, or downstream handlers.

    Authority is derived ONLY from this carrier - NOT from:
    - Model-passed session_id
    - ContextVar fallback heuristics
    - Single-active-turn heuristics
    """
    from .models import AuthContext, TurnInteractivity

    author = event.author
    canonical = None
    roles: tuple[str, ...] = ()
    is_service = False

    if author is not None and resolver is not None:
        canonical = resolver.resolve(author)
        access = resolver.access_metadata(author)
        roles = access.roles
        is_service = access.is_service

    registered_service = _TRUSTED_SERVICE_PRINCIPALS.get(event.trigger)
    if (
        registered_service is not None
        and event.service_principal == registered_service.canonical
        and event_ingress is None
        and not (
            isinstance(event.extra, dict)
            and event.extra.get(HTTP_EVENT_INGRESS_EXTRA_KEY) is not None
        )
    ):
        canonical = registered_service.canonical
        is_service = True

    return AuthContext(
        principal=author,
        canonical_principal=canonical,
        roles=roles,
        event_ingress=(
            event_ingress
            if event_ingress is not None
            else event.extra.get(HTTP_EVENT_INGRESS_EXTRA_KEY) if isinstance(event.extra, dict) else None
        ),
        trigger=event.trigger,
        channel_id=event.channel_id,
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        policy_version=policy_version,
        is_service=is_service,
        enforcement_enabled=enforce,
        ifc_labels=ifc_labels,
    )
