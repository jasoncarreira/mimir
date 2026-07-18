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
import os
import shlex
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .identities import AccessMetadata

HTTP_EVENT_INGRESS_EXTRA_KEY = "_mimir_event_ingress"

if TYPE_CHECKING:
    from .identities import IdentityResolver
    from .models import AgentEvent, AuthContext, InformationFlowLabels

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
    SAGA = "saga"
    SCHEDULER = "scheduler"
    PROPOSAL = "proposal"
    UNKNOWN = "unknown"


class ToolFlowDirection(StrEnum):
    """Whether a tool reads protected data, emits data, does both, or neither."""

    SOURCE = "source"
    SINK = "sink"
    BOTH = "both"
    NEITHER = "neither"
    UNKNOWN = "unknown"


_SINK_CATEGORY_MAP: dict[str, SinkCategory] = {
    "send_message": SinkCategory.SAME_CHANNEL,
    "react": SinkCategory.SAME_CHANNEL,
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
    "worklink_run": SinkCategory.SPAWN,
    "ntfy_send": SinkCategory.NOTIFICATION,
    "write_file": SinkCategory.FILE,
    "edit_file": SinkCategory.FILE,
    "memory_store": SinkCategory.SAGA,
    "saga_record_skill_learning": SinkCategory.SAGA,
    "saga_feedback": SinkCategory.SAGA,
    "saga_mark_contributions": SinkCategory.SAGA,
    "saga_forget": SinkCategory.SAGA,
    "saga_end_session": SinkCategory.SAGA,
    "add_schedule": SinkCategory.SCHEDULER,
    "set_schedule_priority": SinkCategory.SCHEDULER,
    "open_proposal": SinkCategory.PROPOSAL,
    "submit_proposal": SinkCategory.PROPOSAL,
    "abandon_proposal": SinkCategory.PROPOSAL,
}

_TOOL_FLOW_MAP: dict[str, ToolFlowDirection] = {
    **{name: ToolFlowDirection.SINK for name in _SINK_CATEGORY_MAP},
    "fetch_channel_history": ToolFlowDirection.SOURCE,
}

IFC_POLICY_VERSION = "ifc-v1"
DECLASSIFICATION_LIFETIME_SECONDS = 30.0


def get_sink_category(tool_name: str) -> SinkCategory:
    """Map a known egress operation to its sink category.

    Unknown operations are not presumed public: doing so would make a newly
    added harness send an implicit IFC bypass until the map was updated.
    """
    for prefix, category in _SINK_CATEGORY_MAP.items():
        if tool_name.startswith(prefix):
            return category
    return SinkCategory.UNKNOWN


def get_tool_flow_direction(tool_name: str) -> ToolFlowDirection:
    """Return explicit native-tool flow metadata without name-prefix inference."""
    for prefix, direction in _TOOL_FLOW_MAP.items():
        if tool_name.startswith(prefix):
            return direction
    return ToolFlowDirection.UNKNOWN


@dataclass(frozen=True)
class ResourceScope:
    """Defines a specific resource/domain that an operation scopes to."""
    domain: str
    capabilities: frozenset[str] = frozenset()
    sink_destinations: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ServiceSinkPolicy:
    """One executable, operation-specific service destination grant."""

    operation: str
    adapter: str
    destination: str


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
    sink_policies: tuple[ServiceSinkPolicy, ...] = ()
    creation_path: str | None = None

    def can_read_domain(self, domain: str) -> bool:
        return domain in self.readable_domains

    def can_write_sink(self, sink: str) -> bool:
        return sink in self.sink_destinations

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def sink_policy_for(self, operation: str) -> ServiceSinkPolicy | None:
        return next(
            (policy for policy in self.sink_policies if policy.operation == operation),
            None,
        )


def _configured_file_roots() -> list[Path]:
    """Return the same roots exposed by the live file-tool backend."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return []

    # Import lazily: config imports this module while defining Config. Reuse its
    # parser rather than maintaining a second env syntax/validation policy here.
    from .config import _parse_file_tool_roots

    extra_roots = _parse_file_tool_roots(
        os.environ.get("MIMIR_FILE_TOOL_ROOTS", ""), Path(home)
    )
    return [Path(home), *(Path(path) for path, _mode in extra_roots)]


def _configured_file_write_roots() -> list[Path]:
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return []

    from .config import _parse_file_tool_roots

    extra_roots = _parse_file_tool_roots(
        os.environ.get("MIMIR_FILE_TOOL_ROOTS", ""), Path(home)
    )
    return [Path(home), *(Path(path) for path, mode in extra_roots if mode == "rw")]


def _target_within_configured_roots(target: str, _destination: str) -> bool:
    from ._paths import PathOutsideHomeError, resolve_within_roots

    try:
        resolve_within_roots(_configured_file_roots(), target)
    except (OSError, PathOutsideHomeError):
        return False
    return True


def _target_within_configured_write_roots(target: str, _destination: str) -> bool:
    from ._paths import PathOutsideHomeError

    try:
        resolve_configured_write_target(target)
    except (OSError, PathOutsideHomeError):
        return False
    return True


def resolve_configured_write_target(target: str) -> Path:
    """Resolve a write sink exactly as the configured-roots adapter does."""
    from ._paths import resolve_within_roots

    return resolve_within_roots(_configured_file_write_roots(), target)


_SHELL_CONTROL_CHARACTERS = frozenset(";|&`$><{}[],*?~\n\r")


def _arguments_match_allowlist(
    arguments: list[str],
    *,
    exact_options: frozenset[str],
    option_prefixes: tuple[str, ...] = (),
) -> bool:
    """Reject every option not explicitly admitted by a command profile.

    Operands remain available after ``--``. An option-looking operand before
    that marker is rejected rather than guessed at; this keeps future binary
    flags from silently widening a trusted service's authority.
    """
    options_ended = False
    for argument in arguments:
        if options_ended:
            continue
        if argument == "--":
            options_ended = True
            continue
        if not argument.startswith("-") or argument == "-":
            continue
        if argument in exact_options or argument.startswith(option_prefixes):
            continue
        return False
    return True


def _target_matches_read_only_shell_command(argv: list[str]) -> bool:
    """Validate an argv against the scheduler/poller read-only profile."""
    # Do not accept ``/tmp/git`` merely because its basename is allow-listed.
    # The login shell may resolve bare names through its operator-controlled PATH,
    # but a model-supplied path must never select an arbitrary executable.
    command = argv[0]
    arguments = argv[1:]

    if command == "pwd":
        return set(arguments) <= {"-L", "-P"}
    if command == "ls":
        return _arguments_match_allowlist(
            arguments,
            exact_options=frozenset({
                "-1", "-A", "-a", "-d", "-F", "-h", "-l", "-la", "-al",
                "-lh", "-hl", "--all", "--almost-all", "--directory",
                "--classify", "--human-readable",
            }),
            option_prefixes=("--color=",),
        )
    if command == "wc":
        return _arguments_match_allowlist(
            arguments,
            exact_options=frozenset({
                "-c", "-l", "-L", "-m", "-w", "--bytes", "--chars",
                "--lines", "--max-line-length", "--words",
            }),
        )
    if command == "grep":
        return _arguments_match_allowlist(
            arguments,
            exact_options=frozenset({
                "-E", "-F", "-H", "-h", "-i", "-l", "-n", "-q", "-s",
                "-v", "-w", "-x", "--extended-regexp", "--fixed-strings",
                "--files-with-matches", "--ignore-case", "--line-number",
                "--no-messages", "--quiet", "--recursive", "--invert-match",
                "--with-filename", "--no-filename", "--word-regexp",
                "--line-regexp",
            }),
            option_prefixes=("--exclude=", "--include=", "--exclude-dir="),
        )
    if command == "jq":
        return _arguments_match_allowlist(
            arguments,
            exact_options=frozenset({
                "-C", "-M", "-R", "-S", "-c", "-e", "-j", "-r", "-s",
                "--ascii-output", "--compact-output", "--exit-status",
                "--join-output", "--monochrome-output", "--null-input",
                "--raw-input", "--raw-output", "--slurp", "--sort-keys",
            }),
        )
    if command == "rg":
        # ripgrep's config file can inject --pre. Require --no-config in the
        # command itself so the allowlist is independent of ambient process env.
        if not arguments or arguments[0] != "--no-config":
            return False
        return _arguments_match_allowlist(
            arguments[1:],
            exact_options=frozenset({
                "-F", "-H", "-L", "-S", "-g", "-h", "-i", "-l", "-n",
                "-s", "-u", "-v", "-w", "--case-sensitive", "--files",
                "--files-with-matches", "--fixed-strings", "--glob", "--hidden",
                "--ignore-case", "--line-number", "--no-heading", "--no-ignore",
                "--smart-case", "--type", "--type-not", "--word-regexp",
            }),
        )
    if command != "git" or not arguments:
        return False

    subcommand = arguments[0]
    subcommand_arguments = arguments[1:]
    if subcommand == "status":
        return _arguments_match_allowlist(
            subcommand_arguments,
            exact_options=frozenset({
                "-b", "-s", "--ahead-behind", "--branch", "--ignore-submodules",
                "--long", "--no-ahead-behind", "--porcelain", "--short",
                "--show-stash", "--untracked-files", "--verbose",
            }),
            option_prefixes=("--ignore-submodules=", "--porcelain=", "--untracked-files="),
        )
    if subcommand not in {"diff", "log", "show"}:
        return False

    # These commands can invoke repository-configured helpers unless both
    # controls are explicit. Requiring them makes the argv safe independently
    # of .gitconfig/.gitattributes in the inspected checkout.
    required_safety_options = {"--no-ext-diff", "--no-textconv"}
    if not required_safety_options.issubset(subcommand_arguments):
        return False
    return _arguments_match_allowlist(
        subcommand_arguments,
        exact_options=frozenset({
            "-p", "--abbrev-commit", "--cached", "--check", "--decorate",
            "--exit-code", "--full-index", "--name-only", "--name-status",
            "--no-color", "--no-ext-diff", "--no-merges", "--no-patch",
            "--no-textconv", "--oneline", "--quiet", "--raw", "--stat",
            "--staged",
        }),
        option_prefixes=("-U", "--max-count=", "--since=", "--until=", "--unified="),
    )


def parse_service_shell_argv(target: str, destination: str) -> list[str] | None:
    """Return the exact argv admitted by a trusted service shell profile.

    The returned argv is both the authorization artifact and the execution
    artifact. Callers must exec it directly with ``shell=False``; handing the
    original string to a shell would reintroduce an expansion layer the profile
    did not validate.
    """
    if any(character in target for character in _SHELL_CONTROL_CHARACTERS):
        return None
    try:
        argv = shlex.split(target)
    except ValueError:
        return None
    if not argv:
        return None

    allowed = False
    if destination == "scheduler_read_only":
        allowed = _target_matches_read_only_shell_command(argv)
    elif destination == "upgrade_workspace":
        allowed = _target_matches_read_only_shell_command(argv) or (
            argv[0] == "uv"
            and argv[1:] in (["lock"], ["sync"])
        )
    return argv if allowed else None


def _target_matches_shell_profile(target: str, destination: str) -> bool:
    """Authorization adapter for the service shell profile."""
    return parse_service_shell_argv(target, destination) is not None


def _target_matches_worklink_repo(target: str, destination: str) -> bool:
    """Authorize Worklink dispatch only to its operator-configured repository."""
    configured = os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO")
    if not configured:
        return False
    try:
        return Path(target).expanduser().resolve() == Path(configured).expanduser().resolve()
    except (OSError, RuntimeError):
        return False


_SERVICE_SINK_ADAPTERS: dict[str, Callable[[str, str], bool]] = {
    "configured_file_roots": _target_within_configured_write_roots,
    "shell_profile": _target_matches_shell_profile,
    "spawn_workspace": _target_within_configured_write_roots,
    "worklink_repo": _target_matches_worklink_repo,
}

_ACTIVE_SERVICE_SINK_DESTINATIONS: dict[SinkCategory, str] = {
    SinkCategory.SHELL_PROCESS: "shell_process",
    SinkCategory.SPAWN: "spawn_process",
    SinkCategory.FILE: "filesystem",
    SinkCategory.NOTIFICATION: "notification",
    SinkCategory.HTTP_WEBHOOK: "network",
    SinkCategory.NETWORK: "network",
    SinkCategory.EXTERNAL_MCP: "external_mcp",
}


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
        sink_category: SinkCategory | None = None,
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

        sink_category = sink_category or get_sink_category(tool_name)
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

        service_policy: ServiceSinkPolicy | None = None
        if service is not None and sink_category in {
            SinkCategory.SHELL_PROCESS,
            SinkCategory.SPAWN,
            SinkCategory.FILE,
            SinkCategory.NOTIFICATION,
            SinkCategory.HTTP_WEBHOOK,
            SinkCategory.NETWORK,
            SinkCategory.EXTERNAL_MCP,
        }:
            # Preserve #906's fail-closed treatment and decision reason before
            # considering any concrete poller destination.
            if "poller_payload" in service.readable_domains and sink_category in {
                SinkCategory.SHELL_PROCESS,
                SinkCategory.SPAWN,
                SinkCategory.FILE,
                SinkCategory.NOTIFICATION,
                SinkCategory.HTTP_WEBHOOK,
                SinkCategory.NETWORK,
                SinkCategory.EXTERNAL_MCP,
            }:
                return ToolAuthorization(
                    tool_name=tool_name,
                    decision=OperationDecision.ADMIN_REQUIRED,
                    allowed=not enforce,
                    reason=f"ifc_label_blocked:{sink_category.value}",
                    service_principal=service,
                    required_tier=AccessTier.ADMIN,
                    enforcement_enabled=enforce,
                    is_shadow_decision=not enforce,
                )
            service_policy = service.sink_policy_for(tool_name)
            adapter = (
                _SERVICE_SINK_ADAPTERS.get(service_policy.adapter)
                if service_policy is not None
                else None
            )
            if adapter is None or not adapter(target, service_policy.destination):
                return ToolAuthorization(
                    tool_name=tool_name,
                    decision=OperationDecision.ADMIN_REQUIRED,
                    allowed=not enforce,
                    reason="service_sink_destination_denied",
                    service_principal=service,
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
            sink_category,
            auth_context,
            ifc_labels=ifc_labels,
            service_policy=service_policy,
            target=target,
        )
        effective_target = (
            ChannelResourceAdapter._resolve_channel(target)
            if sink_category == SinkCategory.SAME_CHANNEL
            else target
        )

        can_flow = ifc_labels.can_flow_to(effective_target or "", allowed_sinks)

        if not can_flow:
            normalized_target = normalize_sink_destination(sink_category, target)
            state = getattr(auth_context, "ifc_state", None)
            canonical_principal = getattr(auth_context, "canonical_principal", None)
            if (
                enforce
                and normalized_target is not None
                and isinstance(canonical_principal, str)
                and state is not None
                and state.consume_sink_approval(
                    current=ifc_labels,
                    sink_category=sink_category.value,
                    destination=normalized_target,
                    canonical_principal=canonical_principal,
                )
            ):
                return ToolAuthorization(
                    tool_name=tool_name,
                    decision=OperationDecision.OPEN,
                    allowed=True,
                    reason="ifc_declassification_approved",
                    enforcement_enabled=enforce,
                )
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
        service_policy: ServiceSinkPolicy | None = None,
        target: str | None = None,
    ) -> frozenset[str]:
        """Return concrete destinations compatible with every current label.

        Ordinary admin authority deliberately does not widen this set. Admins
        must use the distinct audited declassification action before egress.
        """
        if auth_context is None:
            return frozenset()

        service = get_trusted_service_from_auth_context(auth_context)
        if service is not None and target is not None and category in {
            SinkCategory.SAGA,
            SinkCategory.SCHEDULER,
            SinkCategory.PROPOSAL,
        }:
            # Poller payloads are attacker-controlled external content (#906).
            # Persistent mutation categories must fail closed too, especially
            # proposal operations that are otherwise available to pollers.
            if "poller_payload" in service.readable_domains:
                return frozenset()
            source_channels = getattr(ifc_labels, "source_channels", None)
            service_channel = getattr(auth_context, "channel_id", None)
            if (
                isinstance(source_channels, frozenset)
                and source_channels
                and source_channels == frozenset({service_channel})
            ):
                return frozenset({target})
            return frozenset()
        if service is not None and service_policy is not None and target is not None:
            # Poller payloads remain attacker-controlled external content (#906).
            if "poller_payload" in service.readable_domains and category in {
                SinkCategory.SHELL_PROCESS,
                SinkCategory.SPAWN,
                SinkCategory.FILE,
                SinkCategory.NOTIFICATION,
                SinkCategory.HTTP_WEBHOOK,
                SinkCategory.NETWORK,
                SinkCategory.EXTERNAL_MCP,
            }:
                return frozenset()
            source_channels = getattr(ifc_labels, "source_channels", None)
            service_channel = getattr(auth_context, "channel_id", None)
            if (
                isinstance(source_channels, frozenset)
                and source_channels
                and service_channel
                and source_channels == frozenset({service_channel})
            ):
                return frozenset({target})
            return frozenset()

        if category != SinkCategory.SAME_CHANNEL:
            return frozenset()

        triggering_channel = getattr(auth_context, "channel_id", None)
        if not triggering_channel:
            return frozenset()
        resolved_triggering = ChannelResourceAdapter._resolve_channel(triggering_channel)
        if not resolved_triggering:
            return frozenset()

        canonical_principal = getattr(auth_context, "canonical_principal", None)
        service = get_trusted_service_from_auth_context(auth_context)
        service_source_principal = (
            f"service:{service.canonical}" if service is not None else None
        )
        domain = getattr(auth_context, "domain", None)
        resource_id = getattr(auth_context, "resource_id", None)
        bridge_instance = getattr(auth_context, "bridge_instance", None)
        sources = getattr(ifc_labels, "sources", None)
        effective_principal = service_source_principal or canonical_principal
        if not all((effective_principal, domain, resource_id, bridge_instance)):
            return frozenset()
        if not isinstance(sources, frozenset) or not sources:
            return frozenset()

        for source in sources:
            if not getattr(source, "is_complete", False):
                return frozenset()
            if effective_principal not in source.authorized_principals:
                return frozenset()
            source_kind = getattr(source, "source_kind", "channel")
            if source_kind == "channel":
                if source.principal != effective_principal:
                    return frozenset()
                if source.domain != domain or source.bridge_instance != bridge_instance:
                    return frozenset()
                if ChannelResourceAdapter._resolve_channel(source.resource_id) != resolved_triggering:
                    return frozenset()
            elif source_kind == "service":
                # Trusted service/derived data retains its input ACL. It may
                # return only to the triggering channel when the effective
                # destination principal remains in that intersection.
                if source.domain.startswith("channel"):
                    if source.bridge_instance != bridge_instance:
                        return frozenset()
                    if ChannelResourceAdapter._resolve_channel(source.resource_id) != resolved_triggering:
                        return frozenset()
            elif source_kind != "protected_prompt":
                # Other derived/tool sources require their own destination adapter;
                # an ACL alone must not silently widen arbitrary provenance kinds.
                return frozenset()
        if ChannelResourceAdapter._resolve_channel(resource_id) != resolved_triggering:
            return frozenset()

        return frozenset({resolved_triggering})


def normalize_sink_destination(
    sink_category: SinkCategory | str,
    destination: Any,
) -> str | None:
    """Return the canonical exact destination used by approval and enforcement."""
    try:
        category = SinkCategory(sink_category)
    except (TypeError, ValueError):
        return None
    if category is SinkCategory.UNKNOWN or not isinstance(destination, str):
        return None
    value = destination.strip()
    if not value or "\x00" in value:
        return None
    if category in {SinkCategory.SAME_CHANNEL, SinkCategory.CROSS_CHANNEL, SinkCategory.DIRECT_MESSAGE}:
        return ChannelResourceAdapter._resolve_channel(value) or None
    if category in {SinkCategory.FILE, SinkCategory.SPAWN}:
        try:
            return str(Path(value).expanduser().resolve())
        except (OSError, RuntimeError):
            return None
    if category in {SinkCategory.NETWORK, SinkCategory.HTTP_WEBHOOK}:
        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                return None
            if parsed.username is not None or parsed.password is not None:
                return None
            port = parsed.port
            host = parsed.hostname.lower()
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            default_port = 80 if parsed.scheme.lower() == "http" else 443
            netloc = host if port in {None, default_port} else f"{host}:{port}"
            return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
        except ValueError:
            return None
    return value


def approve_live_declassification(
    auth_context: Any,
    *,
    sink_category: Any,
    destination: Any,
    reason: Any,
) -> tuple[bool, str]:
    """Approve one exact sink on the exact live admin request carrier."""
    from .models import AuthContext, InformationFlowLabels, InformationFlowState

    if not isinstance(auth_context, AuthContext):
        return False, "missing_auth_context"
    if "admin" not in auth_context.roles:
        return False, "admin_required"
    principal = auth_context.principal
    canonical_principal = auth_context.canonical_principal
    if not isinstance(principal, str) or not principal.strip():
        return False, "missing_authenticated_admin"
    if not isinstance(canonical_principal, str) or not canonical_principal.strip():
        return False, "missing_authenticated_admin"
    if not isinstance(reason, str) or not reason.strip():
        return False, "invalid_reason"
    try:
        category = SinkCategory(sink_category)
    except (TypeError, ValueError):
        return False, "unknown_sink_category"
    normalized = normalize_sink_destination(category, destination)
    if normalized is None:
        return False, "malformed_destination"
    state = auth_context.ifc_state
    if not isinstance(state, InformationFlowState):
        return False, "missing_ifc_state"

    def durable_audit(
        labels: InformationFlowLabels, issued_at: float, expires_at: float,
    ) -> bool:
        source_labels = [
            {
                "principal": source.principal,
                "domain": source.domain,
                "resource_id": source.resource_id,
                "bridge_instance": source.bridge_instance,
                "sensitivity": source.sensitivity,
                "authorized_principals": sorted(source.authorized_principals),
                "source_kind": source.source_kind,
            }
            for source in sorted(
                labels.sources,
                key=lambda item: (
                    str(item.domain), str(item.resource_id), str(item.principal),
                    str(item.sensitivity),
                ),
            )
        ]
        try:
            from .event_logger import log_durable_event_sync

            log_durable_event_sync(
                "ifc_declassification",
                source_labels=source_labels,
                labels=sorted(labels.labels),
                source_channels=sorted(labels.source_channels),
                authenticated_admin={
                    "principal": principal,
                    "canonical_principal": canonical_principal,
                    "roles": sorted(auth_context.roles),
                },
                reason=reason.strip(),
                destination=normalized,
                sink_category=category.value,
                policy_version=IFC_POLICY_VERSION,
                outcome="approved",
                use_limit=1,
                lifetime_seconds=DECLASSIFICATION_LIFETIME_SECONDS,
                issued_at_monotonic=issued_at,
                expires_at_monotonic=expires_at,
            )
        except Exception as exc:
            log.warning("ifc declassification audit failed: %s", exc)
            return False
        return True

    approved = state.approve_sink_once(
        fallback=auth_context.ifc_labels,
        sink_category=category.value,
        destination=normalized,
        canonical_principal=canonical_principal,
        lifetime_seconds=DECLASSIFICATION_LIFETIME_SECONDS,
        durable_audit=durable_audit,
    )
    return (True, "approved") if approved else (False, "approval_failed")


def audit_declassification(
    labels: Any,
    declassification_reason: str,
    auth_context: Any,
    *,
    destination: str,
    policy_version: str = IFC_POLICY_VERSION,
) -> Any:
    """Deprecated no-op; only the live middleware action can authorize egress."""
    return labels


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
        "commitment_list",
        "memory_query",
        "memory_get",
        # Web research is available to authorized users; calls remain subject
        # to the NETWORK information-flow sink gate before authorization.
        "web_search",
        "fetch_url",
        "write_todos",
        "defer_injected_message",
        "commitment_complete",
        "commitment_snooze",
        "commitment_dismiss",
    })

    _ADMIN_REQUIRED_OPERATIONS: frozenset[str] = frozenset({
        "approve_declassification",
        "list_channels",
        "list_schedules",
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
        "bash_jobs_list",
        "bash_job_output",
        "spawn_claude_code",
        "spawn_codex",
        "spawn_open_code",
        "task",
        "memory_store",
        "saga_feedback",
        "saga_mark_contributions",
        "saga_end_session",
        "saga_record_skill_learning",
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

    # Global rows from these operations contain protected identities,
    # configuration, or process metadata and must never become OPEN.
    _PROTECTED_METADATA_OPERATIONS: frozenset[str] = frozenset({
        "list_channels",
        "list_schedules",
        "bash_jobs_list",
        "bash_job_output",
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
        saga_mutations = globals().get("_SAGA_MUTATION_OPERATIONS", frozenset())
        is_admin_catalogued = (
            name in self._ADMIN_REQUIRED_OPERATIONS
            or name in self._ADMIN_BUILTIN_TOOL_NAMES
            or any(
                name.endswith(f"__{catalogued}")
                or name.endswith(f"_{catalogued}")
                for catalogued in self._ADMIN_REQUIRED_OPERATIONS
            )
        )
        if (
            is_admin_catalogued or name in saga_mutations
        ) and decision != OperationDecision.ADMIN_REQUIRED:
            raise ValueError(
                f"cannot downgrade protected operation {name!r} from ADMIN_REQUIRED"
            )
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

        classification = getattr(provenance, "classification", "")
        if classification:
            try:
                return OperationDecision(classification)
            except ValueError:
                return OperationDecision.ADMIN_REQUIRED

        # Compatibility for pre-policy callers. Production approvals always
        # carry classification and never authorize resources through this path.
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

    @classmethod
    def authorize_call(
        cls,
        tool_name: str,
        tool: Any,
        arguments: dict[str, Any] | None,
        context: Any | None,
        *,
        enforce: bool,
        ifc_labels: Any = None,
    ) -> "ToolAuthorization":
        """Execute the provenance-bound adapter and IFC gate on one invocation."""
        from .mcp_client import (
            MCPAuthorizationRequest,
            MCPAuthorizationResult,
            get_tool_provenance,
        )

        provenance = get_tool_provenance(tool) if tool is not None else None
        decision = OperationDecision.ADMIN_REQUIRED
        reason = "mcp_missing_provenance"
        validated_result: MCPAuthorizationResult | None = None
        flow_direction = ToolFlowDirection.UNKNOWN
        sink_check: ToolAuthorization | None = None
        if provenance is not None and provenance.is_tombstoned:
            reason = "mcp_drift_detected"
        elif provenance is not None:
            try:
                decision = OperationDecision(provenance.classification)
            except ValueError:
                reason = "mcp_unclassified"
            else:
                adapter = cls._get_registered_adapter(provenance)
                if adapter is None:
                    decision = OperationDecision.ADMIN_REQUIRED
                    reason = "mcp_missing_adapter"
                elif arguments is None:
                    decision = OperationDecision.ADMIN_REQUIRED
                    reason = "mcp_malformed_arguments"
                else:
                    try:
                        flow_direction = ToolFlowDirection(adapter.flow_direction)
                    except ValueError:
                        flow_direction = ToolFlowDirection.UNKNOWN
                    if flow_direction is ToolFlowDirection.UNKNOWN:
                        decision = OperationDecision.ADMIN_REQUIRED
                        reason = "mcp_unknown_flow_direction"
                        adapter = None
                if adapter is not None and arguments is not None:
                    try:
                        result = adapter.classify(MCPAuthorizationRequest(
                            tool_name=tool_name,
                            arguments=arguments,
                            auth_context=context,
                            provenance=provenance,
                        ))
                    except Exception:
                        log.exception(
                            "MCP adapter %s failed while authorizing %s",
                            provenance.adapter_name,
                            tool_name,
                        )
                        decision = OperationDecision.ADMIN_REQUIRED
                        reason = "mcp_adapter_exception"
                    else:
                        if not isinstance(result, MCPAuthorizationResult):
                            decision = OperationDecision.ADMIN_REQUIRED
                            reason = "mcp_invalid_adapter_result"
                        elif result.decision is not decision:
                            decision = OperationDecision.ADMIN_REQUIRED
                            reason = "mcp_adapter_decision_mismatch"
                        elif result.allowed:
                            expected_source = flow_direction in {
                                ToolFlowDirection.SOURCE, ToolFlowDirection.BOTH,
                            }
                            expected_sink = flow_direction in {
                                ToolFlowDirection.SINK, ToolFlowDirection.BOTH,
                            }
                            if (
                                bool(result.source_resources) is not expected_source
                                or bool(result.sink_resources) is not expected_sink
                            ):
                                decision = OperationDecision.ADMIN_REQUIRED
                                reason = "mcp_flow_metadata_mismatch"
                                result = None
                        if isinstance(result, MCPAuthorizationResult) and result.allowed:
                            if ifc_labels is None and context is not None:
                                ifc_labels = getattr(context, "ifc_labels", None)
                            if result.sink_resources:
                                sink_check = SinkGate.check_sink_flow(
                                    tool_name,
                                    ",".join(result.sink_resources),
                                    ifc_labels,
                                    context,
                                    enforce=enforce,
                                    sink_category=SinkCategory.EXTERNAL_MCP,
                                )
                            if sink_check is not None and not sink_check.allowed:
                                return sink_check
                            validated_result = result
                            if decision is not OperationDecision.ADMIN_REQUIRED:
                                return ToolAuthorization(
                                    tool_name=tool_name,
                                    decision=decision,
                                    allowed=True,
                                    reason=(
                                        sink_check.reason
                                        if sink_check is not None and sink_check.is_shadow_decision
                                        else None
                                    ),
                                    enforcement_enabled=enforce,
                                    is_shadow_decision=(
                                        sink_check.is_shadow_decision if sink_check is not None else False
                                    ),
                                    protected_source_resources=result.source_resources,
                                    protected_sink_resources=result.sink_resources,
                                    flow_direction=flow_direction,
                                )
                            reason = "admin_required"
                        elif isinstance(result, MCPAuthorizationResult) and not result.allowed:
                            reason = result.reason or "mcp_resource_denied"

        is_admin = decision is OperationDecision.ADMIN_REQUIRED
        admin = context is not None and "admin" in (getattr(context, "roles", ()) or ())
        hard_failure = validated_result is None
        denied_by_policy = hard_failure or (is_admin and not admin)
        allowed = (admin and not hard_failure) or not enforce
        shadow_sink = sink_check is not None and sink_check.is_shadow_decision
        return ToolAuthorization(
            tool_name=tool_name,
            decision=decision,
            allowed=allowed,
            reason=(
                sink_check.reason
                if shadow_sink
                else None if admin and not hard_failure else reason
            ),
            required_tier=AccessTier.ADMIN if is_admin else AccessTier.USER,
            enforcement_enabled=enforce,
            is_shadow_decision=shadow_sink or (not enforce and denied_by_policy),
            protected_source_resources=(
                validated_result.source_resources if validated_result is not None else None
            ),
            protected_sink_resources=(
                validated_result.sink_resources if validated_result is not None else None
            ),
            flow_direction=flow_direction,
        )

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
    # ``None`` means provenance is unknown; ``()`` authoritatively classifies
    # the call as not reading a protected MCP source.
    protected_source_resources: tuple[str, ...] | None = None
    protected_sink_resources: tuple[str, ...] | None = None
    flow_direction: ToolFlowDirection = ToolFlowDirection.UNKNOWN

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
        mcp_tool: Any = None,
        arguments: dict[str, Any] | None = None,
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
        if tool_name.startswith(MCPResourceAdapter._MCP_TOOL_PREFIX) and mcp_tool is not None:
            auth = MCPResourceAdapter.authorize_call(
                tool_name,
                mcp_tool,
                arguments,
                auth_context,
                enforce=enforce,
                ifc_labels=ifc_labels,
            )
            if auth.is_shadow_decision:
                self._emit_shadow_decision(auth)
            return auth
        if tool_name.startswith(MCPResourceAdapter._MCP_TOOL_PREFIX):
            auth = ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason="mcp_unknown_flow_direction",
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
            )
            if auth.is_shadow_decision:
                self._emit_shadow_decision(auth)
            return auth

        flow_direction = get_tool_flow_direction(tool_name)
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
            and not service_can_invoke_operation(preliminary_service, tool_name)
        )
        service_allowed_preliminary = (
            service_can_invoke_operation(preliminary_service, tool_name)
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
        if not sink_target:
            sink_target = _OPERATION_SINK_DESTINATION.get(tool_name)
        is_ifc_sink = flow_direction in {
            ToolFlowDirection.SINK, ToolFlowDirection.BOTH,
        } or (
            ifc_labels is not None
            and flow_direction is ToolFlowDirection.UNKNOWN
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
            service_can_invoke_operation(service_principal, tool_name)
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
                channel_auth.flow_direction = flow_direction
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
            flow_direction=flow_direction,
        )

        if is_shadow:
            self._emit_shadow_decision(auth)

        return auth


_PROTECTED_RESULT_DOMAINS: dict[str, str] = {
    "list_channels": "channel_metadata",
    "list_schedules": "schedule_metadata",
    "bash_jobs_list": "shell_jobs",
    "bash_job_output": "shell_jobs",
    "read_file": "filesystem",
    "aread": "filesystem",
    "ls": "filesystem",
    "als": "filesystem",
    "glob": "filesystem",
    "aglob": "filesystem",
    "grep": "filesystem",
    "agrep": "filesystem",
    "download_files": "filesystem",
    "adownload_files": "filesystem",
    "Read": "filesystem",
    "Glob": "filesystem",
    "Grep": "filesystem",
    "file_search": "filesystem",
    "get_turn": "turn_history",
    "mimir_get_turn": "turn_history",
    "memory_query": "saga",
    "memory_get": "saga",
    "commitment_list": "commitments",
}


def classify_protected_result(
    tool_name: str,
    arguments: dict[str, Any] | None,
    auth_context: "AuthContext | None",
    authorization: ToolAuthorization,
) -> "InformationFlowLabels | None":
    """Return server-authoritative labels for content a protected call may expose.

    The contract is based only on the authorized operation and validated
    arguments. Tool success text, model assertions, and error wording cannot
    downgrade it. Unknown provenance is intentionally incomplete and therefore
    fails closed at every egress gate.
    """
    from .models import InformationFlowLabels, SourceLabel

    args = arguments or {}
    if tool_name == "fetch_channel_history":
        resource = args.get("channel_id") or getattr(auth_context, "channel_id", None)
        principal = getattr(auth_context, "canonical_principal", None)
        if getattr(auth_context, "is_service", False) and principal:
            principal = f"service:{principal}"
        source = SourceLabel(
            principal=principal,
            domain=getattr(auth_context, "domain", None),
            resource_id=ChannelResourceAdapter._resolve_channel(resource),
            bridge_instance=getattr(auth_context, "bridge_instance", None),
            sensitivity="private",
            authorized_principals=frozenset({principal}) if principal else frozenset(),
            source_kind="channel",
        )
        return InformationFlowLabels().with_source(source)

    if tool_name.startswith(MCPResourceAdapter._MCP_TOOL_PREFIX):
        resources = authorization.protected_source_resources
        if resources == ():
            return None
        principal = getattr(auth_context, "canonical_principal", None)
        labels = InformationFlowLabels()
        for resource in resources or ("unknown",):
            labels = labels.with_source(SourceLabel(
                principal=principal if resources is not None else None,
                domain="mcp",
                resource_id=resource,
                bridge_instance=tool_name.split("_", 2)[1] if "_" in tool_name else None,
                sensitivity="internal",
                authorized_principals=(
                    frozenset({principal}) if principal and resources is not None else frozenset()
                ),
                source_kind="mcp",
            ))
        return labels

    domain = _PROTECTED_RESULT_DOMAINS.get(tool_name)
    if domain is None:
        # Native aliases may be namespaced by a tool server. Do not apply this
        # suffix rule to MCP calls, which are classified above from provenance.
        for candidate, candidate_domain in _PROTECTED_RESULT_DOMAINS.items():
            if tool_name.endswith(f"__{candidate}"):
                domain = candidate_domain
                break
    if domain is None:
        return None

    resource = next(
        (
            args.get(key)
            for key in ("path", "file_path", "query", "turn_id", "atom_id", "job_id")
            if isinstance(args.get(key), str) and args.get(key)
        ),
        "unknown",
    )
    return InformationFlowLabels().with_source(SourceLabel(
        principal=None,
        domain=domain,
        resource_id=str(resource),
        bridge_instance=None,
        sensitivity="internal",
        authorized_principals=frozenset(),
        source_kind="protected_tool",
    ))


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
                "bash_jobs_list",
                "bash_job_output",
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
            readable_domains=(
                "configured_inputs",
                "filesystem",
                "turn_history",
                "shell_jobs",
            ),
            sink_destinations=(
                "configured_channel",
                "filesystem",
                "shell_process",
                "spawn_process",
                "proposal",
                "saga",
                "worklink",
            ),
            sink_policies=(
                ServiceSinkPolicy("write_file", "configured_file_roots", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("edit_file", "configured_file_roots", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("shell_exec", "shell_profile", "scheduler_read_only"),
                ServiceSinkPolicy("bash_async", "shell_profile", "scheduler_read_only"),
                ServiceSinkPolicy("spawn_claude_code", "spawn_workspace", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("spawn_codex", "spawn_workspace", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("spawn_open_code", "spawn_workspace", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("worklink_run", "worklink_repo", "WORKLINK_REPO/MIMIR_WORKLINK_REPO"),
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
                "list_channels",
            ),
            readable_domains=(
                "poller_payload",
                "filesystem",
                "turn_history",
                "channel_metadata",
            ),
            sink_destinations=(
                "configured_channel",
                "filesystem",
                "shell_process",
                "spawn_process",
                "proposal",
                "worklink",
                "message",
            ),
            sink_policies=(
                ServiceSinkPolicy("write_file", "configured_file_roots", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("edit_file", "configured_file_roots", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("shell_exec", "shell_profile", "scheduler_read_only"),
                ServiceSinkPolicy("bash_async", "shell_profile", "scheduler_read_only"),
                ServiceSinkPolicy("spawn_claude_code", "spawn_workspace", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("spawn_codex", "spawn_workspace", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("spawn_open_code", "spawn_workspace", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("worklink_run", "worklink_repo", "WORKLINK_REPO/MIMIR_WORKLINK_REPO"),
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
            sink_policies=(
                ServiceSinkPolicy("write_file", "configured_file_roots", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("edit_file", "configured_file_roots", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
            ),
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
                "list_schedules",
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
            readable_domains=(
                "defaults",
                "proposal",
                "filesystem",
                "schedule_metadata",
            ),
            sink_destinations=(
                "operator_alert",
                "filesystem",
                "shell_process",
                "proposal",
                "scheduler",
                "message",
            ),
            sink_policies=(
                ServiceSinkPolicy("write_file", "configured_file_roots", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("edit_file", "configured_file_roots", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("shell_exec", "shell_profile", "upgrade_workspace"),
                ServiceSinkPolicy("bash_async", "shell_profile", "upgrade_workspace"),
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
    "list_channels": "channel_metadata",
    "list_schedules": "schedule_metadata",
    "bash_jobs_list": "shell_jobs",
    "bash_job_output": "shell_jobs",
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
    "spawn_open_code": "spawn_process",
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

_SAGA_MUTATION_OPERATIONS: frozenset[str] = frozenset({
    "memory_store",
    "saga_feedback",
    "saga_mark_contributions",
    "saga_end_session",
    "saga_record_skill_learning",
    "saga_forget",
})


class CapabilityMatrixError(Exception):
    """Raised when enforcement is requested with an incomplete matrix."""


class ProviderEnforcementCompatibilityError(Exception):
    """Raised when the active model provider cannot safely enforce authz."""


def _capability_matrix_errors() -> list[str]:
    errors: list[str] = []
    for operation in sorted(_OPERATION_SINK_DESTINATION):
        if get_sink_category(operation) is SinkCategory.UNKNOWN:
            errors.append(
                f"Sink operation '{operation}' has no IFC sink category mapping"
            )
    for operation in sorted(_SAGA_MUTATION_OPERATIONS):
        if operation not in _OPERATION_SINK_DESTINATION:
            errors.append(
                f"SAGA mutation '{operation}' has no sink destination mapping"
            )
        effective_decision = _global_operation_catalog.get_decision(operation)
        if effective_decision == OperationDecision.OPEN:
            errors.append(f"SAGA mutation '{operation}' must not be cataloged OPEN")
        if effective_decision != OperationDecision.ADMIN_REQUIRED:
            errors.append(
                f"SAGA mutation '{operation}' must be cataloged ADMIN_REQUIRED"
            )
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
        policies_by_operation = {policy.operation: policy for policy in principal.sink_policies}
        if len(policies_by_operation) != len(principal.sink_policies):
            errors.append(
                f"Service principal '{principal.canonical}' has duplicate sink policies"
            )
        for policy in principal.sink_policies:
            if policy.operation not in principal.capabilities:
                errors.append(
                    f"Service principal '{principal.canonical}' sink policy "
                    f"'{policy.operation}' has no matching capability"
                )
            if policy.adapter not in _SERVICE_SINK_ADAPTERS:
                errors.append(
                    f"Service principal '{principal.canonical}' sink policy "
                    f"'{policy.operation}' has no executable destination adapter "
                    f"'{policy.adapter}'"
                )
        policy_sink_destinations = {
            _ACTIVE_SERVICE_SINK_DESTINATIONS[category]
            for policy in principal.sink_policies
            if (category := get_sink_category(policy.operation))
            in _ACTIVE_SERVICE_SINK_DESTINATIONS
        }
        for sink_destination in sorted(
            sink_destinations & set(_ACTIVE_SERVICE_SINK_DESTINATIONS.values())
        ):
            if sink_destination not in policy_sink_destinations:
                errors.append(
                    f"Service principal '{principal.canonical}' sink destination "
                    f"'{sink_destination}' has no executable destination policy"
                )
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
            if get_sink_category(operation) in {
                SinkCategory.SHELL_PROCESS,
                SinkCategory.SPAWN,
                SinkCategory.FILE,
                SinkCategory.NOTIFICATION,
                SinkCategory.HTTP_WEBHOOK,
                SinkCategory.NETWORK,
                SinkCategory.EXTERNAL_MCP,
            } and operation not in policies_by_operation:
                errors.append(
                    f"Service principal '{principal.canonical}' capability "
                    f"'{operation}' has no executable destination policy"
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


def assert_model_tool_inventory_cataloged(*, model_spec: str | None = None) -> None:
    """Raise if the assembled model-bound Mimir tool surface is uncataloged."""
    from .tools.registry import all_mimir_tools

    catalog = get_operation_catalog()
    unknown_tools = sorted({
        tool.name
        for tool in all_mimir_tools(model_spec=model_spec)
        if catalog.get_decision(tool.name) == OperationDecision.UNKNOWN
    })
    if unknown_tools:
        raise CapabilityMatrixError(
            "Access-control enforcement blocked by UNKNOWN model-bound tools: "
            + ", ".join(unknown_tools)
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
        assert_model_tool_inventory_cataloged(model_spec=model_spec)
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
            "sink_policies": [
                {
                    "operation": policy.operation,
                    "adapter": policy.adapter,
                    "destination": policy.destination,
                }
                for policy in principal.sink_policies
            ],
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


def service_can_invoke_operation(
    service: ServicePrincipal | None,
    operation: str,
) -> bool:
    """Check an exact service capability and its declared flow constraints."""
    if service is None or not service.has_capability(operation):
        return False
    required_domain = _OPERATION_READABLE_DOMAIN.get(operation)
    if required_domain and not service.can_read_domain(required_domain):
        return False
    required_sink = _OPERATION_SINK_DESTINATION.get(operation)
    if required_sink and not service.can_write_sink(required_sink):
        return False
    return True


def can_write_saga(auth_context: Any, operation: str) -> bool:
    """Authorize one canonical SAGA mutation for an admin or service."""
    if operation not in _SAGA_MUTATION_OPERATIONS:
        return False
    if is_admin(auth_context):
        return True
    service = get_trusted_service_from_auth_context(auth_context)
    return service_can_invoke_operation(service, operation)


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
    canonical = author
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

    canonical_resource = event.channel_id
    if resolver is not None:
        canonical_resource = resolver.resolve_channel(event.channel_id)
    extra = event.extra if isinstance(event.extra, dict) else {}
    visibility = extra.get("channel_visibility")
    domain = (
        f"channel:{visibility}"
        if isinstance(visibility, str) and visibility
        else "channel"
    )
    bridge_instance = extra.get("bridge_instance")
    if not isinstance(bridge_instance, str) or not bridge_instance:
        bridge_instance = event.source
    if (
        (not isinstance(bridge_instance, str) or not bridge_instance)
        and registered_service is not None
        and event.service_principal == registered_service.canonical
    ):
        bridge_instance = f"service:{registered_service.canonical}"

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
        source_session_acl=(
            event.source_session_acl
            if registered_service is not None
            and event.trigger == "saga_session_end"
            and event.service_principal == registered_service.canonical
            and event_ingress is None
            and not (
                isinstance(event.extra, dict)
                and event.extra.get(HTTP_EVENT_INGRESS_EXTRA_KEY) is not None
            )
            else None
        ),
        ifc_labels=ifc_labels,
        domain=domain,
        resource_id=canonical_resource,
        bridge_instance=bridge_instance,
    )
