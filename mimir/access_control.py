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

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from langchain_core.tools import ToolException

from .channel_registry import OPERATOR_CHANNEL_SENTINEL, resolve_deliver_channel
from .identities import AccessMetadata
from .models import (
    NormalizedPullRequestSnapshot,
    RepoPRAction,
    RepoPRScopeProvenance,
)
from .read_policy import (
    READ_RESOURCE_OPERATIONS,
    framework_large_tool_results_root,
    read_target_from_arguments,
    requested_read_target_from_arguments,
    resolved_read_target_from_arguments,
    resolve_large_tool_results_target,
)

HTTP_EVENT_INGRESS_EXTRA_KEY = "_mimir_event_ingress"

if TYPE_CHECKING:
    from .identities import IdentityResolver
    from .models import AgentEvent, AuthContext, InformationFlowLabels, SourceLabel

log = logging.getLogger(__name__)

_persisted_file_integrity_lock = threading.Lock()

_MAX_REQUESTED_TARGET_LENGTH = 1024


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
    FORGE = "forge"
    HARNESS_DISPLAY = "harness_display"
    UNKNOWN = "unknown"


_SINK_CATEGORY_CAPABILITY_ELIGIBLE = frozenset({
    SinkCategory.SAME_CHANNEL,
    SinkCategory.CROSS_CHANNEL,
    SinkCategory.PUBLIC,
    SinkCategory.SHELL_PROCESS,
    SinkCategory.SPAWN,
    SinkCategory.NOTIFICATION,
    SinkCategory.FILE,
    SinkCategory.DIRECT_MESSAGE,
    SinkCategory.SAGA,
    SinkCategory.SCHEDULER,
    SinkCategory.PROPOSAL,
    SinkCategory.FORGE,
})

assert set(SinkCategory) == _SINK_CATEGORY_CAPABILITY_ELIGIBLE | {
    SinkCategory.NETWORK,
    SinkCategory.HTTP_WEBHOOK,
    SinkCategory.EXTERNAL_MCP,
    SinkCategory.HARNESS_DISPLAY,
    SinkCategory.UNKNOWN,
}


class CapabilityTier(StrEnum):
    """Blast-radius ceiling for authority declared by autonomous triggers."""

    SCOPE_CONTAINED = "scope-contained"
    SCOPED_WITH_PROVENANCE = "scoped-with-provenance"
    CODE_EXECUTION = "code-execution"
    UNBOUNDED = "unbounded"


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
    # These harness-only sinks accept metadata-only payloads. They are not
    # model-selected messages and intentionally do not share SAME_CHANNEL.
    "activity_panel_post": SinkCategory.HARNESS_DISPLAY,
    "activity_panel_edit": SinkCategory.HARNESS_DISPLAY,
    "post_message": SinkCategory.CROSS_CHANNEL,
    "webhook": SinkCategory.HTTP_WEBHOOK,
    "http_request": SinkCategory.HTTP_WEBHOOK,
    "fetch_url": SinkCategory.NETWORK,
    "web_search": SinkCategory.NETWORK,
    "shell_exec": SinkCategory.SHELL_PROCESS,
    "bash_async": SinkCategory.SHELL_PROCESS,
    "Bash": SinkCategory.SHELL_PROCESS,
    "bash": SinkCategory.SHELL_PROCESS,
    "bash_exec": SinkCategory.SHELL_PROCESS,
    "execute": SinkCategory.SHELL_PROCESS,
    "aexecute": SinkCategory.SHELL_PROCESS,
    "shell": SinkCategory.SHELL_PROCESS,
    "spawn_open_code": SinkCategory.SPAWN,
    "worklink_run": SinkCategory.SPAWN,
    "ntfy_send": SinkCategory.NOTIFICATION,
    "write_file": SinkCategory.FILE,
    "edit_file": SinkCategory.FILE,
    "Write": SinkCategory.FILE,
    "Edit": SinkCategory.FILE,
    "download_files": SinkCategory.FILE,
    "adownload_files": SinkCategory.FILE,
    "rebuild_index": SinkCategory.FILE,
    "request_mimir_update": SinkCategory.FILE,
    "memory_store": SinkCategory.SAGA,
    "saga_record_skill_learning": SinkCategory.SAGA,
    "saga_feedback": SinkCategory.SAGA,
    "saga_mark_contributions": SinkCategory.SAGA,
    "saga_forget": SinkCategory.SAGA,
    "saga_end_session": SinkCategory.SAGA,
    "add_schedule": SinkCategory.SCHEDULER,
    "set_schedule_priority": SinkCategory.SCHEDULER,
    "remove_schedule": SinkCategory.SCHEDULER,
    "set_poller_overrides": SinkCategory.SCHEDULER,
    "reload_pollers": SinkCategory.SCHEDULER,
    "commitment_complete": SinkCategory.SAGA,
    "commitment_snooze": SinkCategory.SAGA,
    "commitment_dismiss": SinkCategory.SAGA,
    "defer_injected_message": SinkCategory.SAGA,
    "open_proposal": SinkCategory.PROPOSAL,
    "submit_proposal": SinkCategory.PROPOSAL,
    "abandon_proposal": SinkCategory.PROPOSAL,
    "pr_submit_review": SinkCategory.FORGE,
    "pr_inline_review_comment": SinkCategory.FORGE,
    "pr_comment": SinkCategory.FORGE,
    "issue_comment": SinkCategory.FORGE,
    "pr_rerequest_review": SinkCategory.FORGE,
    "unsupported_operation": SinkCategory.FORGE,
    "repo_checkout": SinkCategory.FORGE,
    "repo_cleanup": SinkCategory.FORGE,
    "repo_fetch": SinkCategory.FORGE,
    "repo_test": SinkCategory.FORGE,
    "repo_stage": SinkCategory.FORGE,
    "repo_commit": SinkCategory.FORGE,
    "repo_merge": SinkCategory.FORGE,
    "repo_merge_abort": SinkCategory.FORGE,
    "repo_rebase": SinkCategory.FORGE,
    "repo_rebase_abort": SinkCategory.FORGE,
    "repo_revert": SinkCategory.FORGE,
    "repo_revert_abort": SinkCategory.FORGE,
    "repo_push": SinkCategory.FORGE,
}

_TOOL_FLOW_MAP: dict[str, ToolFlowDirection] = {
    # Native model tools. This is intentionally exhaustive rather than derived
    # from the sink map: startup checks the assembled surface against this map,
    # so adding a tool without making an IFC decision fails closed.
    # Declassification mutates the live authorization carrier but does not itself
    # read protected data or emit it; the subsequent exact sink remains gated.
    "approve_declassification": ToolFlowDirection.NEITHER,
    "request_operator_approval": ToolFlowDirection.NEITHER,
    "memory_query": ToolFlowDirection.SOURCE,
    "memory_get": ToolFlowDirection.SOURCE,
    "memory_store": ToolFlowDirection.SINK,
    "open_proposal": ToolFlowDirection.SINK,
    "submit_proposal": ToolFlowDirection.SINK,
    "abandon_proposal": ToolFlowDirection.SINK,
    "saga_feedback": ToolFlowDirection.SINK,
    "saga_mark_contributions": ToolFlowDirection.SINK,
    "saga_end_session": ToolFlowDirection.SINK,
    "saga_forget": ToolFlowDirection.SINK,
    "saga_record_skill_learning": ToolFlowDirection.SINK,
    "file_search": ToolFlowDirection.SOURCE,
    "rebuild_index": ToolFlowDirection.SINK,
    "mimir_get_turn": ToolFlowDirection.SOURCE,
    "get_turn": ToolFlowDirection.SOURCE,
    "shell_exec": ToolFlowDirection.BOTH,
    "bash_async": ToolFlowDirection.BOTH,
    "bash_jobs_list": ToolFlowDirection.SOURCE,
    "bash_job_output": ToolFlowDirection.SOURCE,
    "send_message": ToolFlowDirection.SINK,
    "react": ToolFlowDirection.SINK,
    "fetch_channel_history": ToolFlowDirection.SOURCE,
    "list_channels": ToolFlowDirection.SOURCE,
    "defer_injected_message": ToolFlowDirection.SINK,
    "list_schedules": ToolFlowDirection.SOURCE,
    "add_schedule": ToolFlowDirection.SINK,
    "set_schedule_priority": ToolFlowDirection.SINK,
    "remove_schedule": ToolFlowDirection.SINK,
    "set_poller_overrides": ToolFlowDirection.SINK,
    "reload_pollers": ToolFlowDirection.SINK,
    "commitment_complete": ToolFlowDirection.SINK,
    "commitment_snooze": ToolFlowDirection.SINK,
    "commitment_dismiss": ToolFlowDirection.SINK,
    "commitment_list": ToolFlowDirection.SOURCE,
    "worklink_run": ToolFlowDirection.BOTH,
    "request_mimir_update": ToolFlowDirection.SINK,
    "web_search": ToolFlowDirection.BOTH,
    "fetch_url": ToolFlowDirection.BOTH,
    "post_message": ToolFlowDirection.SINK,
    "webhook": ToolFlowDirection.SINK,
    "http_request": ToolFlowDirection.BOTH,
    "ntfy_send": ToolFlowDirection.SINK,
    "spawn_open_code": ToolFlowDirection.BOTH,
    # Deepagents model-bound built-ins and their async/compatibility aliases.
    "read_file": ToolFlowDirection.SOURCE,
    "aread": ToolFlowDirection.SOURCE,
    "ls": ToolFlowDirection.SOURCE,
    "als": ToolFlowDirection.SOURCE,
    "glob": ToolFlowDirection.SOURCE,
    "aglob": ToolFlowDirection.SOURCE,
    "grep": ToolFlowDirection.SOURCE,
    "agrep": ToolFlowDirection.SOURCE,
    "write_file": ToolFlowDirection.SINK,
    "edit_file": ToolFlowDirection.SINK,
    "download_files": ToolFlowDirection.BOTH,
    "adownload_files": ToolFlowDirection.BOTH,
    "write_todos": ToolFlowDirection.NEITHER,
    # Built-in subagents remain inside the current IFC carrier; delegation
    # propagation is handled separately and is not an external sink itself.
    "task": ToolFlowDirection.NEITHER,
    "Bash": ToolFlowDirection.BOTH,
    "bash": ToolFlowDirection.BOTH,
    "bash_exec": ToolFlowDirection.BOTH,
    "execute": ToolFlowDirection.BOTH,
    "aexecute": ToolFlowDirection.BOTH,
    "shell": ToolFlowDirection.BOTH,
    "Write": ToolFlowDirection.SINK,
    "Edit": ToolFlowDirection.SINK,
    "Read": ToolFlowDirection.SOURCE,
    "Glob": ToolFlowDirection.SOURCE,
    "Grep": ToolFlowDirection.SOURCE,
    # Harness egress is not model-bound but shares the same gate.
    "harness_auto_deliver": ToolFlowDirection.SINK,
    "harness_resend_nudge": ToolFlowDirection.SINK,
    "activity_panel_post": ToolFlowDirection.SINK,
    "activity_panel_edit": ToolFlowDirection.SINK,
    "pr_metadata": ToolFlowDirection.SOURCE,
    "pr_files": ToolFlowDirection.SOURCE,
    "pr_diff": ToolFlowDirection.SOURCE,
    "pr_checks": ToolFlowDirection.SOURCE,
    "pr_reviews": ToolFlowDirection.SOURCE,
    "pr_comments": ToolFlowDirection.SOURCE,
    "pr_review_requests": ToolFlowDirection.SOURCE,
    "pr_submit_review": ToolFlowDirection.SINK,
    "pr_inline_review_comment": ToolFlowDirection.SINK,
    "pr_comment": ToolFlowDirection.SINK,
    "issue_comment": ToolFlowDirection.SINK,
    "pr_rerequest_review": ToolFlowDirection.SINK,
    "unsupported_operation": ToolFlowDirection.SINK,
    "repo_checkout": ToolFlowDirection.BOTH,
    "repo_cleanup": ToolFlowDirection.SINK,
    "repo_fetch": ToolFlowDirection.BOTH,
    "repo_status": ToolFlowDirection.SOURCE,
    "repo_test": ToolFlowDirection.BOTH,
    "repo_diff": ToolFlowDirection.SOURCE,
    "repo_unmerged": ToolFlowDirection.SOURCE,
    "repo_stage": ToolFlowDirection.SINK,
    "repo_commit": ToolFlowDirection.SINK,
    "repo_merge": ToolFlowDirection.SINK,
    "repo_merge_abort": ToolFlowDirection.SINK,
    "repo_rebase": ToolFlowDirection.SINK,
    "repo_rebase_abort": ToolFlowDirection.SINK,
    "repo_revert": ToolFlowDirection.SINK,
    "repo_revert_abort": ToolFlowDirection.SINK,
    "repo_push": ToolFlowDirection.SINK,
}

IFC_POLICY_VERSION = "ifc-v1"
DECLASSIFICATION_LIFETIME_SECONDS = 30.0


def get_sink_category(tool_name: str) -> SinkCategory:
    """Map a known egress operation to its sink category.

    Unknown operations are not presumed public: doing so would make a newly
    added harness send an implicit IFC bypass until the map was updated.
    """
    return _SINK_CATEGORY_MAP.get(tool_name, SinkCategory.UNKNOWN)


def get_tool_flow_direction(tool_name: str) -> ToolFlowDirection:
    """Return explicit native-tool flow metadata without name-prefix inference."""
    return _TOOL_FLOW_MAP.get(tool_name, ToolFlowDirection.UNKNOWN)


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

    Defined by server-owned creation path, trigger, capabilities, readable
    domains, SAGA read breadth, and sink destinations. Unknown synthetic
    triggers receive no privilege.
    """
    canonical: str
    trigger: str
    capabilities: tuple[str, ...] = ()
    readable_domains: tuple[str, ...] = ()
    sink_destinations: tuple[str, ...] = ()
    sink_policies: tuple[ServiceSinkPolicy, ...] = ()
    filesystem_read_roots: tuple[str, ...] = ()
    owned_skill_directory: str | None = None
    channel_memory_directory: str | None = None
    saga_full_corpus_read: bool = False
    creation_path: str | None = None
    authority_profile: str | None = None
    capability_tier: CapabilityTier | None = None
    #: Per-job shell grants, additive on top of the profile's allowlist. Declared
    #: in scheduler.yaml or a poller manifest -- neither of which any service
    #: principal can write -- and validated into this shape before it lands here.
    declared_shell_commands: tuple["DeclaredShellCommand", ...] = ()

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


# This catalog is the executable tier table for trigger authority. Manifest
# parsing imports it rather than maintaining a second list of grantable names.
TRIGGER_CAPABILITY_TIERS: dict[str, CapabilityTier] = {
    "write_file": CapabilityTier.SCOPE_CONTAINED,
    "edit_file": CapabilityTier.SCOPE_CONTAINED,
    "rebuild_index": CapabilityTier.SCOPE_CONTAINED,
    "shell_exec": CapabilityTier.SCOPE_CONTAINED,
    "bash_async": CapabilityTier.SCOPE_CONTAINED,
    "bash_jobs_list": CapabilityTier.SCOPE_CONTAINED,
    "bash_job_output": CapabilityTier.SCOPE_CONTAINED,
    "read_file": CapabilityTier.SCOPE_CONTAINED,
    "aread": CapabilityTier.SCOPE_CONTAINED,
    "ls": CapabilityTier.SCOPE_CONTAINED,
    "als": CapabilityTier.SCOPE_CONTAINED,
    "glob": CapabilityTier.SCOPE_CONTAINED,
    "aglob": CapabilityTier.SCOPE_CONTAINED,
    "grep": CapabilityTier.SCOPE_CONTAINED,
    "agrep": CapabilityTier.SCOPE_CONTAINED,
    "file_search": CapabilityTier.SCOPE_CONTAINED,
    "get_turn": CapabilityTier.SCOPE_CONTAINED,
    "mimir_get_turn": CapabilityTier.SCOPE_CONTAINED,
    "memory_get": CapabilityTier.SCOPE_CONTAINED,
    "send_message": CapabilityTier.SCOPE_CONTAINED,
    "operator_alert": CapabilityTier.SCOPE_CONTAINED,
    "memory_store": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "saga_feedback": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "saga_mark_contributions": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "saga_end_session": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "saga_record_skill_learning": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "worklink_run": CapabilityTier.CODE_EXECUTION,
    "spawn_open_code": CapabilityTier.CODE_EXECUTION,
    "fetch_url": CapabilityTier.UNBOUNDED,
    "web_search": CapabilityTier.UNBOUNDED,
    "webhook": CapabilityTier.UNBOUNDED,
    "http_request": CapabilityTier.UNBOUNDED,
    "ntfy_send": CapabilityTier.UNBOUNDED,
    "task": CapabilityTier.SCOPE_CONTAINED,
    "list_schedules": CapabilityTier.SCOPE_CONTAINED,
    "pr_metadata": CapabilityTier.SCOPE_CONTAINED,
    "pr_files": CapabilityTier.SCOPE_CONTAINED,
    "pr_diff": CapabilityTier.SCOPE_CONTAINED,
    "pr_checks": CapabilityTier.SCOPE_CONTAINED,
    "pr_reviews": CapabilityTier.SCOPE_CONTAINED,
    "pr_comments": CapabilityTier.SCOPE_CONTAINED,
    "pr_review_requests": CapabilityTier.SCOPE_CONTAINED,
    "pr_submit_review": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "pr_inline_review_comment": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "pr_comment": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "issue_comment": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "pr_rerequest_review": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "unsupported_operation": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_checkout": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_cleanup": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_fetch": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_test": CapabilityTier.CODE_EXECUTION,
    "repo_status": CapabilityTier.SCOPE_CONTAINED,
    "repo_diff": CapabilityTier.SCOPE_CONTAINED,
    "repo_unmerged": CapabilityTier.SCOPE_CONTAINED,
    "repo_stage": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_commit": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_merge": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_merge_abort": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_rebase": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_rebase_abort": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_revert": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_revert_abort": CapabilityTier.SCOPED_WITH_PROVENANCE,
    "repo_push": CapabilityTier.SCOPED_WITH_PROVENANCE,
}

_CAPABILITY_COMPANIONS: dict[str, frozenset[str]] = {
    "shell_exec": frozenset({"bash_jobs_list", "bash_job_output"}),
    "bash_async": frozenset({"bash_jobs_list", "bash_job_output"}),
}


def _missing_capability_companions(capabilities: set[str]) -> set[str]:
    return {
        companion
        for capability in capabilities
        for companion in _CAPABILITY_COMPANIONS.get(capability, ())
        if companion not in capabilities
    }

# Built-in services predate manifest-declarable trigger authority. Keep their
# explicitly declared, review-bounded proposal workflow classified without
# making those capabilities grantable to custom trigger manifests.
_LEGACY_SERVICE_SINK_TIERS: dict[str, CapabilityTier] = {
    "open_proposal": CapabilityTier.SCOPE_CONTAINED,
    "submit_proposal": CapabilityTier.SCOPE_CONTAINED,
    "abandon_proposal": CapabilityTier.SCOPE_CONTAINED,
}

TRIGGER_AUTHORITY_PROFILES: dict[str, frozenset[str]] = {
    "research": frozenset({
        "write_file", "edit_file", "read_file", "aread", "ls", "als",
        "glob", "aglob", "grep", "agrep", "file_search", "memory_store",
        "saga_feedback", "saga_mark_contributions", "send_message",
        "saga_record_skill_learning", "operator_alert", "shell_exec",
        "bash_jobs_list", "bash_job_output",
    }),
    "github": frozenset({
        "worklink_run", "write_file", "edit_file", "shell_exec",
        "bash_async", "bash_jobs_list", "bash_job_output", "read_file",
        "aread", "ls", "als", "glob", "aglob", "grep", "agrep",
        "file_search", "get_turn", "mimir_get_turn", "send_message",
        "operator_alert", "task", "memory_store", "saga_mark_contributions",
        "saga_end_session", "saga_record_skill_learning", "fetch_url",
        "pr_metadata", "pr_files", "pr_diff", "pr_checks", "pr_reviews",
        "pr_comments", "pr_review_requests", "pr_submit_review",
        "pr_inline_review_comment", "pr_comment", "pr_rerequest_review",
        "issue_comment",
        "unsupported_operation", "repo_checkout", "repo_cleanup", "repo_fetch",
        "repo_status", "repo_test", "repo_diff", "repo_unmerged", "repo_stage", "repo_commit",
        "repo_merge", "repo_merge_abort", "repo_rebase", "repo_rebase_abort",
        "repo_revert", "repo_revert_abort", "repo_push",
    }),
    # Custom profiles remain tier-validated and cannot request unbounded sinks.
    "custom": frozenset(TRIGGER_CAPABILITY_TIERS) - {"issue_comment"},
    "heartbeat": frozenset({
        "write_file", "edit_file", "shell_exec", "bash_async",
        "bash_jobs_list", "bash_job_output", "read_file", "aread", "ls",
        "als", "glob", "aglob", "grep", "agrep", "file_search",
        "get_turn", "mimir_get_turn", "memory_store", "saga_feedback",
        "saga_mark_contributions", "worklink_run", "send_message",
        "operator_alert", "fetch_url", "task", "list_schedules",
        "pr_metadata", "pr_files", "pr_diff", "pr_checks", "pr_reviews",
        "pr_comments", "pr_review_requests", "pr_submit_review",
        "pr_inline_review_comment", "pr_comment", "pr_rerequest_review",
        "unsupported_operation", "repo_checkout", "repo_cleanup", "repo_fetch",
        "repo_status", "repo_test", "repo_diff", "repo_unmerged", "repo_stage", "repo_commit",
        "repo_merge", "repo_merge_abort", "repo_rebase", "repo_rebase_abort",
        "repo_revert", "repo_revert_abort", "repo_push",
    }),
    "session-boundary": frozenset({
        "memory_store", "saga_feedback", "saga_mark_contributions",
        "saga_end_session", "saga_record_skill_learning",
        "memory_get",
        "bash_jobs_list", "bash_job_output",
        "read_file", "aread", "ls", "als", "glob", "aglob", "grep",
        "agrep", "file_search", "get_turn", "mimir_get_turn",
        "write_file", "edit_file", "rebuild_index", "pr_metadata", "pr_checks",
        "pr_reviews",
    }),
}

# Scheduler records may opt into these built-in profiles. Keeping this set
# separate prevents a scheduler entry from selecting a profile whose trigger
# semantics require additional manifest data (for example a poller profile).
SCHEDULER_AUTHORITY_PROFILES = frozenset({"heartbeat"})

_BUILTIN_TRIGGER_PROFILE_CONFIG: dict[str, dict[str, Any]] = {
    "heartbeat": {
        "canonical": "heartbeat",
        "trigger": "scheduled_tick",
        "tier": CapabilityTier.UNBOUNDED,
        "root_parts": ("state", "triggers", "heartbeat"),
        "channel_memory_directory": "scheduler:heartbeat",
        "creation_path": "mimir.scheduler.Scheduler._fire:heartbeat",
        # Heartbeat is an autonomous agent turn that recalls accumulated memory.
        # This broadens SAGA reads only; its capabilities and sinks stay profile-bound.
        "saga_full_corpus_read": True,
    },
    "session-boundary": {
        "canonical": "synthesis",
        "trigger": "saga_session_end",
        "tier": CapabilityTier.SCOPED_WITH_PROVENANCE,
        "root_parts": None,
        "channel_memory_directory": None,
        "creation_path": "mimir.server._on_session_idle",
        "saga_full_corpus_read": True,
    },
}

_SHELL_PROFILE_BY_AUTHORITY_PROFILE = {
    "github": "repo_review",
    "heartbeat": "maintenance",
}
_FETCH_URL_POLICY_BY_AUTHORITY_PROFILE = {
    "heartbeat": ("approved_urls", "MIMIR_HEARTBEAT_APPROVED_URLS"),
    "github": ("github_pr_api", "GITHUB_REPOS"),
}
_PR_REVIEW_SCOPE_AUTHORITY_PROFILES = frozenset({"heartbeat"})

_CAPABILITY_TIER_RANK = {
    CapabilityTier.SCOPE_CONTAINED: 0,
    CapabilityTier.SCOPED_WITH_PROVENANCE: 1,
    CapabilityTier.CODE_EXECUTION: 2,
    CapabilityTier.UNBOUNDED: 3,
}


def build_trigger_service_principal(
    *,
    canonical: str,
    trigger: str,
    profile: str,
    tier: CapabilityTier,
    capabilities: tuple[str, ...],
    roots: tuple[Path, ...] = (),
    owned_skill_directory: Path | None = None,
    saga_full_corpus_read: bool = False,
    channel_memory_directory: str | None = None,
    declared_shell_commands: tuple["DeclaredShellCommand", ...] = (),
    creation_path: str,
) -> ServicePrincipal:
    """Build one immutable instance principal from already-validated authority."""
    capability_set = set(capabilities)
    missing = _missing_capability_companions(capability_set)
    if missing:
        raise ValueError(
            "capabilities require companions: "
            f"{', '.join(sorted(missing))}"
        )
    home = os.environ.get("MIMIR_HOME", "").strip()
    artifact_root = framework_large_tool_results_root(Path(home)) if home else None
    home_data_roots = (
        (Path(home) / "state", Path(home) / "memory", artifact_root) if home else ()
    )
    is_github_activity = canonical == "poller:github-activity"
    repo_roots = tuple(root.resolve() for root in _configured_repo_roots())
    fetch_cache_roots = (
        (Path(home) / "attachments" / "fetch-cache",)
        if is_github_activity and home
        else ()
    )
    service_work_roots = (
        (Path(home) / "scratch",)
        if is_github_activity and home
        else ()
    )
    write_roots = tuple(dict.fromkeys(
        root.resolve()
        for root in (
            *roots,
            *(() if is_github_activity else _configured_repo_write_roots()),
            *home_data_roots,
            *((Path(home) / "scratch",) if is_github_activity and home else ()),
        )
    ))
    operations = tuple(dict.fromkeys(
        "send_message" if capability == "operator_alert" else capability
        for capability in capabilities
    ))
    readable_domains = {
        "poller_payload" if trigger == "poller"
        else "session" if trigger == "saga_session_end"
        else "configured_inputs"
    }
    sink_destinations: set[str] = set()
    policies: list[ServiceSinkPolicy] = []
    for operation in operations:
        domain = _OPERATION_READABLE_DOMAIN.get(operation)
        if domain:
            readable_domains.add(domain)
        destination = _OPERATION_SINK_DESTINATION.get(operation)
        if destination:
            sink_destinations.add(destination)
        if operation in {"write_file", "edit_file"}:
            policies.append(ServiceSinkPolicy(
                operation,
                "trigger_service_write_roots",
                json.dumps([str(root) for root in write_roots]),
            ))
        elif operation in {"shell_exec", "bash_async"}:
            shell_profile = _SHELL_PROFILE_BY_AUTHORITY_PROFILE.get(
                profile, "scheduler_read_only",
            )
            policies.append(ServiceSinkPolicy(operation, "shell_profile", shell_profile))
        elif operation == "worklink_run":
            policies.append(ServiceSinkPolicy(operation, "worklink_repo", "WORKLINK_REPO/MIMIR_WORKLINK_REPO"))
        elif operation == "fetch_url":
            fetch_policy = _FETCH_URL_POLICY_BY_AUTHORITY_PROFILE.get(profile)
            if fetch_policy is not None:
                policies.append(ServiceSinkPolicy(operation, *fetch_policy))
    if "operator_alert" in capabilities:
        policies.append(ServiceSinkPolicy("send_message", "operator_alert", "MIMIR_OPERATOR_ALERT_CHANNEL"))
    return ServicePrincipal(
        canonical=canonical,
        trigger=trigger,
        capabilities=operations,
        readable_domains=tuple(sorted(readable_domains)),
        sink_destinations=tuple(sorted(sink_destinations)),
        sink_policies=tuple(policies),
        filesystem_read_roots=(
            tuple(str(root.resolve()) for root in (
                *repo_roots, *fetch_cache_roots, *service_work_roots,
                *((artifact_root,) if artifact_root is not None else ()),
            ))
        ),
        owned_skill_directory=(
            str(owned_skill_directory.resolve())
            if owned_skill_directory is not None
            else None
        ),
        channel_memory_directory=channel_memory_directory,
        saga_full_corpus_read=saga_full_corpus_read,
        creation_path=creation_path,
        authority_profile=profile,
        capability_tier=tier,
        declared_shell_commands=declared_shell_commands,
    )


def builtin_trigger_service_principal(
    profile: str, home: Path, *, scheduler_job_name: str | None = None,
) -> ServicePrincipal:
    """Return the authoritative built-in grant; manifests cannot replace it."""
    config = _BUILTIN_TRIGGER_PROFILE_CONFIG.get(profile)
    if config is None:
        raise ValueError(f"unknown built-in authority profile: {profile!r}")
    root_parts = config["root_parts"]
    roots: tuple[Path, ...] = ()
    if root_parts is not None:
        root = home.joinpath(*root_parts).resolve()
        if scheduler_job_name is not None:
            root.mkdir(parents=True, exist_ok=True)
        roots = (root,)
    channel_memory_directory = config["channel_memory_directory"]
    if scheduler_job_name is not None:
        channel_memory_directory = f"scheduler:{scheduler_job_name}"
    return build_trigger_service_principal(
        canonical=config["canonical"],
        trigger=config["trigger"],
        profile=profile,
        tier=config["tier"],
        capabilities=tuple(sorted(TRIGGER_AUTHORITY_PROFILES[profile])),
        roots=roots,
        channel_memory_directory=channel_memory_directory,
        creation_path=config["creation_path"],
        saga_full_corpus_read=config["saga_full_corpus_read"],
    )


def build_scheduled_tick_service_principal(
    job_name: str, home: Path | None,
) -> ServicePrincipal | None:
    """Bind the shared scheduled-tick authority to one scheduler job's reads."""
    base = get_service_principal("scheduled_tick")
    if base is None:
        return None
    script_roots = (
        (str((home / "scripts").resolve()),) if home is not None else ()
    )
    return replace(
        base,
        filesystem_read_roots=tuple(dict.fromkeys((
            *base.filesystem_read_roots,
            *script_roots,
        ))),
        channel_memory_directory=f"scheduler:{job_name}",
        creation_path="mimir.scheduler.Scheduler._fire:scheduled_tick",
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


def _configured_maintenance_git_roots() -> list[Path]:
    """Return home plus explicit file-tool roots, excluding implicit ``/tmp``."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return []

    from .config import _parse_file_tool_roots

    extra_roots = _parse_file_tool_roots(
        os.environ.get("MIMIR_FILE_TOOL_ROOTS", ""), Path(home), always_rw=(),
    )
    return [Path(home), *(Path(path) for path, _mode in extra_roots)]


def _upgrade_proposals_root() -> Path | None:
    """Return the upgrade service's bounded proposal workspace root."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return None
    return (Path(home).resolve() / "scratch" / "proposals").resolve()


def current_turn_scratch_root() -> Path | None:
    """Return the active turn's server-owned ordinary scratch workspace."""
    from ._context import get_current_turn

    home = os.environ.get("MIMIR_HOME", "").strip()
    turn_id = getattr(get_current_turn(), "turn_id", None)
    if not home or not isinstance(turn_id, str) or not turn_id:
        return None
    component = Path(turn_id)
    if component.name != turn_id or turn_id in {".", ".."}:
        return None
    return (Path(home).resolve() / "scratch" / "turns" / turn_id).resolve()


def service_filesystem_read_roots(service: ServicePrincipal | None) -> tuple[Path, ...]:
    """Resolve static read roots plus built-in service-owned workspace roots."""
    if service is None:
        return ()
    home = os.environ.get("MIMIR_HOME", "").strip()
    home_scratch = (Path(home).resolve() / "scratch").resolve() if home else None
    roots = [
        Path(root) for root in service.filesystem_read_roots
        if home_scratch is None or Path(root).resolve() != home_scratch
    ]
    if home:
        home_root = Path(home).resolve()
        roots.extend((
            home_root / "state",
            home_root / "skills",
            home_root / ".mimir_builtin_skills",
            home_root / ".mimir",
            home_root / "CHANGELOG.md",
        ))
    turn_scratch = current_turn_scratch_root()
    if turn_scratch is not None:
        roots.append(turn_scratch)
    if (
        getattr(service, "trigger", None) == "poller"
        and str(getattr(service, "canonical", "")).startswith("poller:")
        and getattr(service, "owned_skill_directory", None)
    ):
        roots.append(Path(service.owned_skill_directory))
    if (
        getattr(service, "canonical", None) == "system"
        and getattr(service, "trigger", None) == "upgrade"
    ):
        if home:
            roots.append(Path(home).resolve() / "docs")
        proposal_root = _upgrade_proposals_root()
        if proposal_root is not None:
            roots.append(proposal_root)
    return tuple(dict.fromkeys(roots))


def _configured_file_write_roots() -> list[Path]:
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return []

    from .config import _parse_file_tool_roots

    extra_roots = _parse_file_tool_roots(
        os.environ.get("MIMIR_FILE_TOOL_ROOTS", ""), Path(home)
    )
    return [Path(home), *(Path(path) for path, mode in extra_roots if mode == "rw")]


def _configured_repo_write_roots() -> list[Path]:
    """Return only explicit external RW roots, excluding home and implicit /tmp."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return []

    from .repository_config import RepositoryInventory

    inventory = RepositoryInventory.load(Path(home) / "repositories.yaml")
    if inventory.declared:
        return [repo.root for repo in inventory.repositories if repo.mode == "rw"]

    from .config import _parse_file_tool_roots

    extra_roots = _parse_file_tool_roots(
        os.environ.get("MIMIR_FILE_TOOL_ROOTS", ""), Path(home), always_rw=(),
    )
    return [Path(path) for path, mode in extra_roots if mode == "rw"]


def _configured_repo_roots() -> list[Path]:
    """Return explicit external file-tool roots in either access mode."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return []

    from .repository_config import RepositoryInventory

    inventory = RepositoryInventory.load(Path(home) / "repositories.yaml")
    if inventory.declared:
        return [repo.root for repo in inventory.repositories]

    from .config import _parse_file_tool_roots

    extra_roots = _parse_file_tool_roots(
        os.environ.get("MIMIR_FILE_TOOL_ROOTS", ""), Path(home), always_rw=(),
    )
    return [Path(path) for path, _mode in extra_roots]
def _github_repo_from_remote(remote: str) -> str | None:
    """Normalize a GitHub origin URL to its owner/repository slug."""
    value = remote.strip()
    match = re.fullmatch(
        r"(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)([^/\s]+)/([^/\s]+?)(?:\.git)?/?",
        value,
    )
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _valid_git_branch(value: object) -> bool:
    """Apply Git's relevant ref-name exclusions without consulting a repository."""
    if not isinstance(value, str) or not value or len(value) > 255:
        return False
    return not (
        value.startswith(("-", ".", "/"))
        or value.endswith((".", "/", ".lock"))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(character in value for character in " ~^:?*[\\\x7f")
        or any(ord(character) < 32 for character in value)
        or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
    )


_REPO_PR_REMEDIATION_ACTIONS = frozenset({
    RepoPRAction.INSPECT.value,
    RepoPRAction.CHECKOUT.value,
    RepoPRAction.TEST.value,
    RepoPRAction.WRITE.value,
    RepoPRAction.COMMIT.value,
    RepoPRAction.PUSH.value,
    RepoPRAction.PR_COMMENT.value,
    RepoPRAction.PR_EDIT.value,
    RepoPRAction.PR_REREQUEST.value,
})
_REPO_PR_CI_REMEDIATION_ACTIONS = _REPO_PR_REMEDIATION_ACTIONS - frozenset({
    RepoPRAction.PR_EDIT.value,
    RepoPRAction.PR_REREQUEST.value,
})
_REPO_PR_CONFLICT_RESOLUTION_ACTIONS = _REPO_PR_REMEDIATION_ACTIONS
_REPO_PR_REVIEW_ACTIONS = frozenset({
    RepoPRAction.INSPECT.value,
    RepoPRAction.CHECKOUT.value,
    RepoPRAction.TEST.value,
    RepoPRAction.PR_REVIEW.value,
    RepoPRAction.PR_COMMENT.value,
})
_FORGE_TOOL_ACTIONS: dict[str, str | None] = {
    "pr_metadata": RepoPRAction.INSPECT.value,
    "pr_files": RepoPRAction.INSPECT.value,
    "pr_diff": RepoPRAction.INSPECT.value,
    "pr_checks": RepoPRAction.INSPECT.value,
    "pr_reviews": RepoPRAction.INSPECT.value,
    "pr_comments": RepoPRAction.INSPECT.value,
    "pr_review_requests": RepoPRAction.INSPECT.value,
    "pr_submit_review": RepoPRAction.PR_REVIEW.value,
    "pr_inline_review_comment": RepoPRAction.PR_REVIEW.value,
    "pr_comment": RepoPRAction.PR_COMMENT.value,
    "pr_rerequest_review": RepoPRAction.PR_REREQUEST.value,
    "unsupported_operation": None,
}
_REPO_TOOL_ACTIONS: dict[str, str] = {
    "repo_checkout": RepoPRAction.CHECKOUT.value,
    "repo_cleanup": RepoPRAction.CHECKOUT.value,
    "repo_fetch": RepoPRAction.CHECKOUT.value,
    "repo_status": RepoPRAction.INSPECT.value,
    "repo_test": RepoPRAction.TEST.value,
    "repo_diff": RepoPRAction.INSPECT.value,
    "repo_unmerged": RepoPRAction.INSPECT.value,
    "repo_stage": RepoPRAction.WRITE.value,
    "repo_commit": RepoPRAction.COMMIT.value,
    "repo_merge": RepoPRAction.COMMIT.value,
    "repo_merge_abort": RepoPRAction.WRITE.value,
    "repo_rebase": RepoPRAction.COMMIT.value,
    "repo_rebase_abort": RepoPRAction.WRITE.value,
    "repo_revert": RepoPRAction.COMMIT.value,
    "repo_revert_abort": RepoPRAction.WRITE.value,
    "repo_push": RepoPRAction.PUSH.value,
}
_TYPED_REPO_PR_TOOL_ACTIONS = {**_FORGE_TOOL_ACTIONS, **_REPO_TOOL_ACTIONS}
_GITHUB_REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_GITHUB_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}")


@dataclass(frozen=True)
class RepoBindingResolution:
    binding: tuple[str, str] | None
    configured_roots: tuple[str, ...]
    match_count: int


@dataclass(frozen=True)
class RepoPRScopeResolution:
    scope: Any = None
    refusal_reason: str | None = None


def _configured_scope_github_repos() -> frozenset[str]:
    home = os.environ.get("MIMIR_HOME", "").strip()
    if home:
        from .repository_config import RepositoryInventory

        inventory = RepositoryInventory.load(Path(home) / "repositories.yaml")
        if inventory.declared:
            return frozenset(repo.slug for repo in inventory.repositories)
    return frozenset(
        f"{owner}/{name}" for owner, name in _configured_github_repos("GITHUB_REPOS")
    )


def is_configured_github_repo(repo: object) -> bool:
    """Return whether a model-supplied selector names server configuration."""
    return (
        isinstance(repo, str)
        and _GITHUB_REPO_PATTERN.fullmatch(repo) is not None
        and repo.lower() in _configured_scope_github_repos()
    )


def _canonical_repo_binding_resolution(repo: str) -> RepoBindingResolution:
    """Resolve a declared binding, with the legacy probe as migration fallback."""
    repo = repo.lower()
    home = os.environ.get("MIMIR_HOME", "").strip()
    if home:
        from .repository_config import RepositoryInventory

        inventory = RepositoryInventory.load(Path(home) / "repositories.yaml")
        if inventory.declared:
            configured_roots = tuple(str(item.root) for item in inventory.repositories)
            record = inventory.repository(repo)
            if record is None:
                return RepoBindingResolution(None, configured_roots, 0)
            return RepoBindingResolution(
                (str(record.root), record.origin), configured_roots, 1,
            )
    configured_roots = tuple(str(root) for root in _configured_repo_write_roots())
    if repo not in _configured_scope_github_repos():
        return RepoBindingResolution(None, configured_roots, 0)
    git = _maintenance_resolved_pin("git")
    if git is None:
        return RepoBindingResolution(None, configured_roots, 0)
    matching_roots: list[tuple[Path, str]] = []
    for configured in map(Path, configured_roots):
        try:
            root = configured.resolve(strict=True)
            result = subprocess.run(
                [str(git), "-C", str(root), *_MAINTENANCE_GIT_BASE_OVERRIDES,
                 "config", "--local", "--get", "remote.origin.url"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env=_maintenance_git_probe_env(),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            continue
        observed_origin = result.stdout.strip()
        remote_repo = _github_repo_from_remote(observed_origin)
        if result.returncode == 0 and remote_repo is not None and remote_repo.lower() == repo:
            matching_roots.append((root, observed_origin))
    if len(matching_roots) != 1:
        return RepoBindingResolution(None, configured_roots, len(matching_roots))
    root, observed_origin = matching_roots[0]
    return RepoBindingResolution((str(root), observed_origin), configured_roots, 1)


def _canonical_repo_binding(repo: str) -> tuple[str, str] | None:
    """Resolve one configured GitHub repo to its unique writable root and origin."""
    return _canonical_repo_binding_resolution(repo).binding


def _repo_binding_refusal_reason(repo: str, match_count: int) -> str:
    mode = (
        "zero roots matched"
        if match_count == 0
        else f"ambiguous: {match_count} roots matched"
    )
    return (
        f"pull-request operation rejected: no unique writable root matched repository "
        f"'{repo}' in MIMIR_FILE_TOOL_ROOTS ({mode}); configure exactly one :rw "
        "entry for the checkout directory itself, not its parent"
    )


def _repo_pr_scope_resolution(
    *,
    provenance: str,
    repo: object,
    principal: object,
    event_type: object,
    number: object,
    head_repo: object,
    head_remote: object,
    head_ref: object,
    head_sha: object,
    base_ref: object,
    base_sha: object,
    review_state: object = None,
) -> RepoPRScopeResolution:
    """Validate a PR snapshot, preserving whether state or configuration refused it."""
    self_login = os.environ.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
    is_fresh_changes_requested_remediation = (
        event_type == "pr_review"
        and review_state == "CHANGES_REQUESTED"
        and principal == self_login
    )
    is_remediation = is_fresh_changes_requested_remediation or event_type in {
        "pr_changes_requested_stale",
        "pr_ci_failure",
        "pr_mergeability_rebase",
        "pr_mergeability_conflicting",
    }
    if (
        not self_login
        or (is_remediation and principal != self_login)
        or not isinstance(repo, str)
        or _GITHUB_REPO_PATTERN.fullmatch(repo) is None
        or not isinstance(number, int)
        or isinstance(number, bool)
        or number < 1
        or not isinstance(head_repo, str)
        or _GITHUB_REPO_PATTERN.fullmatch(head_repo) is None
        or (is_remediation and head_repo.lower() != repo.lower())
        or (is_remediation and head_remote != "origin")
        or (not is_remediation and head_remote not in {"origin", "source"})
        or not _valid_git_branch(head_ref)
        or not _valid_git_branch(base_ref)
        or not isinstance(head_sha, str)
        or _GITHUB_SHA_PATTERN.fullmatch(head_sha) is None
        or not isinstance(base_sha, str)
        or _GITHUB_SHA_PATTERN.fullmatch(base_sha) is None
    ):
        return RepoPRScopeResolution(
            refusal_reason="pull-request operation rejected: live pull request is closed or invalid"
        )
    repo = repo.lower()
    head_repo = head_repo.lower()
    binding_resolution = _canonical_repo_binding_resolution(repo)
    if binding_resolution.binding is None:
        return RepoPRScopeResolution(
            refusal_reason=_repo_binding_refusal_reason(
                repo, binding_resolution.match_count,
            )
        )
    root, origin = binding_resolution.binding
    from .models import RepoPRActionScope

    return RepoPRScopeResolution(scope=RepoPRActionScope(
        provenance=provenance,
        canonical_repo=repo,
        canonical_root=root,
        canonical_origin=origin,
        principal=self_login,
        event_type=event_type,
        allowed_operations=(
            _REPO_PR_CONFLICT_RESOLUTION_ACTIONS
            if event_type == "pr_mergeability_conflicting"
            else _REPO_PR_CI_REMEDIATION_ACTIONS
            if event_type == "pr_ci_failure"
            else _REPO_PR_REMEDIATION_ACTIONS
            if is_remediation
            else _REPO_PR_REVIEW_ACTIONS
        ),
        pr_number=number,
        head_repo=head_repo,
        head_remote=head_remote if is_remediation else "origin",
        destination_ref=f"refs/heads/{head_ref}",
        observed_head_sha=head_sha.lower(),
        base_ref=base_ref,
        observed_base_sha=base_sha.lower(),
        checkout_ref=None if is_remediation else f"refs/pull/{number}/head",
    ))


def _repo_pr_scope(**kwargs: Any) -> Any:
    """Compatibility wrapper returning only an issued scope or ``None``."""
    return _repo_pr_scope_resolution(**kwargs).scope


def repo_binding_startup_alerts() -> tuple[dict[str, Any], ...]:
    """Return one non-fatal operator alert for each unbound configured repo."""
    alerts: list[dict[str, Any]] = []
    for repo in sorted(_configured_scope_github_repos()):
        resolution = _canonical_repo_binding_resolution(repo)
        if resolution.binding is not None:
            continue
        alerts.append({
            "repository": repo,
            "probed_roots": list(resolution.configured_roots),
            "match_count": resolution.match_count,
            "error": _repo_binding_refusal_reason(repo, resolution.match_count),
            "operator_visible": True,
        })
    return tuple(alerts)


def create_server_discovered_heartbeat_scope(
    repo: str,
    pull_request: NormalizedPullRequestSnapshot,
    *,
    event_type: str,
) -> Any:
    """Create heartbeat authority from one provider-normalized live PR snapshot."""
    if (
        not isinstance(pull_request, NormalizedPullRequestSnapshot)
        or pull_request.state != "open"
    ):
        return None
    return _repo_pr_scope(
        provenance=RepoPRScopeProvenance.SERVER_DISCOVERED,
        repo=repo,
        principal=pull_request.author,
        event_type=event_type,
        number=pull_request.number,
        head_repo=pull_request.head_repo,
        head_remote=pull_request.head_remote,
        head_ref=pull_request.head_ref,
        head_sha=pull_request.head_sha,
        base_ref=pull_request.base_ref,
        base_sha=pull_request.base_sha,
    )


def create_server_discovered_review_scope(
    repo: str,
    pull_request: NormalizedPullRequestSnapshot,
    *,
    review_state: object = None,
) -> Any:
    """Issue standing review or fresh-remediation authority from a live PR."""
    if (
        not isinstance(pull_request, NormalizedPullRequestSnapshot)
        or pull_request.state != "open"
    ):
        return None
    return _repo_pr_scope(
        provenance=RepoPRScopeProvenance.SERVER_DISCOVERED,
        repo=repo,
        principal=pull_request.author,
        event_type="pr_review",
        review_state=review_state,
        number=pull_request.number,
        head_repo=pull_request.head_repo,
        head_remote=pull_request.head_remote,
        head_ref=pull_request.head_ref,
        head_sha=pull_request.head_sha,
        base_ref=pull_request.base_ref,
        base_sha=pull_request.base_sha,
    )


def resolve_server_discovered_review_scope(
    repo: str,
    pull_request: NormalizedPullRequestSnapshot,
    *,
    review_state: object = None,
) -> RepoPRScopeResolution:
    """Resolve standing review or fresh-remediation authority with a refusal."""
    if (
        not isinstance(pull_request, NormalizedPullRequestSnapshot)
        or pull_request.state != "open"
    ):
        return RepoPRScopeResolution(
            refusal_reason="pull-request operation rejected: live pull request is closed or invalid"
        )
    return _repo_pr_scope_resolution(
        provenance=RepoPRScopeProvenance.SERVER_DISCOVERED,
        repo=repo,
        principal=pull_request.author,
        event_type="pr_review",
        review_state=review_state,
        number=pull_request.number,
        head_repo=pull_request.head_repo,
        head_remote=pull_request.head_remote,
        head_ref=pull_request.head_ref,
        head_sha=pull_request.head_sha,
        base_ref=pull_request.base_ref,
        base_sha=pull_request.base_sha,
    )


def _repo_review_state_from_event(event: "AgentEvent", service: ServicePrincipal | None) -> Any:
    """Derive exactly the valid immutable PR scopes in a trusted poller payload."""
    if (
        service is None
        or service.authority_profile != "github"
        or event.trigger != "poller"
        or not isinstance(event.extra, dict)
    ):
        return None
    items = event.extra.get("items")
    if not isinstance(items, list):
        return None
    from .models import RepoPRScopeRegistry, RepoReviewState

    by_target: dict[tuple[str, int], RepoReviewState | None] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        scope = _repo_pr_scope(
            provenance=RepoPRScopeProvenance.POLLER_PAYLOAD,
            repo=item.get("repo"),
            principal=item.get("author"),
            event_type=item.get("event_type"),
            review_state=item.get("state"),
            number=item.get("number"),
            head_repo=item.get("head_repo"),
            head_remote=item.get("head_remote"),
            head_ref=item.get("head_ref"),
            head_sha=item.get("head_sha"),
            base_ref=item.get("base_ref"),
            base_sha=item.get("base_sha"),
        )
        if scope is None:
            continue
        target = (scope.canonical_repo, scope.pr_number)
        previous = by_target.get(target)
        if previous is None and target in by_target:
            continue
        if previous is not None and previous.action_scope.scope_id != scope.scope_id:
            # Never choose between conflicting trusted snapshots of one target.
            by_target[target] = None
        elif previous is None:
            by_target[target] = RepoReviewState(scope)
    states = tuple(
        state for _target, state in sorted(by_target.items()) if state is not None
    )
    return RepoPRScopeRegistry(states) if states else None


def _static_service_write_roots() -> list[Path]:
    """Return the complete filesystem scope writable by static services.

    ``/tmp`` is intentionally shared and attacker-influenced; granting it here
    matches the file-tool backend's existing RW scope. Protected-name checks
    below still apply, and executable pins must remain outside every root.
    """
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return []
    home_root = Path(home).resolve()
    artifact_root = framework_large_tool_results_root(home_root)
    return list(dict.fromkeys([
        *(root.resolve() for root in _configured_repo_write_roots()),
        (home_root / "state").resolve(),
        (home_root / "memory").resolve(),
        (home_root / "scratch").resolve(),
        *((artifact_root,) if artifact_root is not None else ()),
        Path("/tmp").resolve(),
    ]))


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


def _target_within_configured_repo_write_roots(target: str, _destination: str) -> bool:
    from ._paths import PathOutsideHomeError, resolve_within_roots

    if not Path(target).is_absolute():
        return False
    try:
        resolve_within_roots(_configured_repo_write_roots(), target)
    except (OSError, PathOutsideHomeError, RuntimeError):
        return False
    return True


_STATIC_SERVICE_PROTECTED_WRITE_NAMES: frozenset[str] = frozenset({
    ".env", ".git", ".mimir", ".venv", "config", "credentials", "identities",
    "prompts", "secret", "secrets",
})


def _is_static_service_protected_write_path(
    path: Path,
    *,
    under_memory_root: bool = False,
    allow_git_metadata: bool = False,
) -> bool:
    """Keep live scheduler/system writes away from operator-controlled data."""
    if under_memory_root and (
        path == Path("core") or path.is_relative_to(Path("core"))
    ):
        return True
    for part in (part.lower() for part in path.parts):
        stem = Path(part).stem
        is_git_metadata = part == ".git" or part.rstrip(".") == ".git"
        if (
            (part in _STATIC_SERVICE_PROTECTED_WRITE_NAMES and part != ".git")
            or (is_git_metadata and not (part == ".git" and allow_git_metadata))
            or stem.split(".", 1)[0] in {
                "config", "credentials", "identities", "secret", "secrets",
            }
            or part.startswith(".env.")
            or part.startswith("oauth_") and part.endswith(".json")
            or Path(part).suffix in {".key", ".pem"}
        ):
            return True
    return False


def _target_within_static_service_write_roots(
    target: str, _destination: str, *, allow_shared_scratch: bool = False,
) -> bool:
    """Authorize static service writes to narrow roots and safe home data."""
    from ._paths import PathOutsideHomeError, resolve_within_roots

    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return False
    home_root = Path(home).resolve()
    state_root = (home_root / "state").resolve()
    memory_root = (home_root / "memory").resolve()
    scratch_root = (home_root / "scratch").resolve()
    artifact_root = framework_large_tool_results_root(home_root)
    home_write_roots = {
        state_root, memory_root, scratch_root,
        *((artifact_root,) if artifact_root is not None else ()),
    }
    roots = _static_service_write_roots()
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = home_root / candidate
    try:
        # Check both the lexical spelling and resolved destination.  The former
        # prevents a protected component that is itself a symlink (for example
        # ``state/credentials -> <repo>/data``) from laundering its name by
        # resolving into another otherwise-safe configured root.
        lexical_root, lexical_relative = max(
            (
                (root, candidate.relative_to(root))
                for root in roots
                if candidate == root or candidate.is_relative_to(root)
            ),
            key=lambda item: len(item[0].parts),
        )
        lexical_has_git_metadata = any(
            part.lower().rstrip(".") == ".git"
            for part in lexical_relative.parts
        )
        if (
            (candidate == home_root or candidate.is_relative_to(home_root))
            and lexical_root not in home_write_roots
        ):
            return False
        if lexical_root == scratch_root and not allow_shared_scratch:
            turn_scratch = current_turn_scratch_root()
            if turn_scratch is None or not (
                candidate == turn_scratch or candidate.is_relative_to(turn_scratch)
            ):
                return False
        # ``allow_git_metadata`` permits exactly ``.git`` under scratch — not
        # its trailing-dot laundering variants, and NOT ``.git/config``: the
        # ``config`` name is independently protected, which preserves #984's
        # invariant that .gitignore/.gitattributes may SELECT a filter or diff
        # driver while the definitions (in config) stay unwritable, even inside
        # scratch. Consequence: ``git init`` / ``git config`` in scratch are
        # refused. That does not break the proposals flow — ``proposals.py``
        # creates its worktrees through a direct server-side subprocess, not
        # through ``shell_exec``, so that path is not gated here. Do not
        # "fix" a git-init refusal by unprotecting ``config``.
        if _is_static_service_protected_write_path(
            lexical_relative,
            under_memory_root=lexical_root == memory_root,
            allow_git_metadata=lexical_root == scratch_root,
        ):
            return False
        resolved = resolve_within_roots(roots, str(candidate))
        root, relative = max(
            (
                (root, resolved.relative_to(root))
                for root in roots
                if resolved == root or resolved.is_relative_to(root)
            ),
            key=lambda item: len(item[0].parts),
        )
        if (
            (resolved == home_root or resolved.is_relative_to(home_root))
            and root not in home_write_roots
        ):
            return False
        if root == scratch_root and not allow_shared_scratch:
            turn_scratch = current_turn_scratch_root()
            if turn_scratch is None or not (
                resolved == turn_scratch or resolved.is_relative_to(turn_scratch)
            ):
                return False
        # A lexical scratch/.git alias may not launder writes into another
        # writable root. Both spellings must remain inside scratch.
        if lexical_has_git_metadata and root != scratch_root:
            return False
    except (OSError, PathOutsideHomeError, RuntimeError, StopIteration, ValueError):
        return False
    return not _is_static_service_protected_write_path(
        relative,
        under_memory_root=root == memory_root,
        allow_git_metadata=root == scratch_root,
    )


def _target_within_upgrade_proposals(target: str, _destination: str) -> bool:
    """Restrict upgrade file mutations to the proposal workspace it owns."""
    from ._paths import PathOutsideHomeError, resolve_within_roots

    root = _upgrade_proposals_root()
    if root is None:
        return False
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = Path(os.environ["MIMIR_HOME"]).resolve() / candidate
    try:
        resolve_within_roots([root], str(candidate))
    except (OSError, PathOutsideHomeError, RuntimeError):
        return False
    return _target_within_static_service_write_roots(
        str(candidate), _destination, allow_shared_scratch=True,
    )


def resolve_configured_write_target(target: str) -> Path:
    """Resolve a write sink exactly as the configured-roots adapter does."""
    from ._paths import resolve_within_roots

    return resolve_within_roots(_configured_file_write_roots(), target)


_SHELL_CONTROL_CHARACTERS = frozenset(";|&`$><{}[]*?\n\r")

_CHAINLINK_EXECUTABLES = frozenset({"chainlink", "/usr/local/bin/chainlink"})
_CHAINLINK_OUTPUT_OPTIONS = frozenset({"-q", "--quiet", "--json"})
_CHAINLINK_QUERY_SUBCOMMANDS = frozenset({
    "show", "list", "search", "ready", "blocked", "related", "cascade",
    "next", "tree",
})
_CHAINLINK_MUTATION_SUBCOMMANDS = frozenset({
    "create", "update", "comment", "label", "unlabel", "block", "unblock",
    "relate", "unrelate", "close", "reopen", "subissue", "quick",
})
# Audited against ``chainlink issue --help``. These commands are mutations that
# remain outside the bounded service surface rather than unclassified reads.
_CHAINLINK_REFUSED_ISSUE_SUBCOMMANDS = {
    "close-all": "bulk mutation",
    "delete": "irreversible mutation",
    "falsify": "dependency mutation",
    "tested": "test-reminder mutation",
}
# Audited against ``chainlink --help``. The service profile intentionally
# exposes only bounded issue orientation and session status; these other roots
# are recorded here so a CLI addition cannot disappear into a silent refusal.
_CHAINLINK_REFUSED_TOP_LEVEL_COMMANDS = {
    "init": "tracker initialization",
    "timer": "mixed read/write namespace outside issue orientation",
    "export": "bulk serialization with a filesystem output option",
    "import": "bulk tracker mutation",
    "archive": "mixed read/write namespace outside issue orientation",
    "milestone": "mixed read/write namespace outside issue orientation",
    "daemon": "daemon lifecycle namespace",
    "cpitd": "clone-detection lifecycle namespace",
    "usage": "mixed read/write accounting namespace",
    "agent": "agent identity and lock namespace",
    "locks": "Worklink-owned coordination lifecycle",
    "sync": "coordination fetch and local lock-state update",
    "cascade": "use the bounded issue cascade form",
    "falsify": "dependency mutation",
}


def _chainlink_issue_arguments_match(arguments: list[str]) -> bool:
    """Admit canonical tracker operations with exact option and operand shapes."""
    if not arguments:
        return False
    subcommand = arguments[0]
    if subcommand not in _CHAINLINK_QUERY_SUBCOMMANDS | _CHAINLINK_MUTATION_SUBCOMMANDS:
        return False

    flag_options = set(_CHAINLINK_OUTPUT_OPTIONS)
    value_options: set[str] = set()
    repeated_value_options: set[str] = set()
    positional_shape: tuple[str, ...]
    if subcommand == "list":
        value_options = {"-s", "--status", "-p", "--priority"}
        repeated_value_options = {"-l", "--label"}
        positional_shape = ()
    elif subcommand in {"ready", "blocked", "next"}:
        positional_shape = ()
    elif subcommand in {"show", "related", "cascade"}:
        positional_shape = ("id",)
    elif subcommand == "tree":
        value_options = {"-s", "--status"}
        # Current help renders the whole tree; deployed versions also accept a
        # root issue. Both forms are bounded, read-only queries.
        positional_shape = ("optional_id",)
    elif subcommand == "search":
        positional_shape = ("text",)
    elif subcommand in {"create", "quick"}:
        value_options = {
            "-d", "--description", "-p", "--priority", "-t", "--template",
        }
        repeated_value_options = {"-l", "--label"}
        if subcommand == "create":
            flag_options.add("-w")
            flag_options.add("--work")
        positional_shape = ("text",)
    elif subcommand == "subissue":
        value_options = {"-d", "--description", "-p", "--priority"}
        repeated_value_options = {"-l", "--label"}
        flag_options.update({"-w", "--work"})
        positional_shape = ("id", "text")
    elif subcommand == "update":
        value_options = {
            "-t", "--title", "-d", "--description", "-p", "--priority",
        }
        positional_shape = ("id",)
    elif subcommand == "comment":
        value_options = {"--kind"}
        positional_shape = ("id", "text")
    elif subcommand in {"label", "unlabel"}:
        positional_shape = ("id", "text")
    elif subcommand in {"block", "unblock", "relate", "unrelate"}:
        if subcommand in {"relate", "unrelate"}:
            value_options = {"-t", "--type"}
        positional_shape = ("id", "id")
    else:
        if subcommand == "close":
            flag_options.add("--no-changelog")
        positional_shape = ("id",)

    positionals: list[str] = []
    seen_single_options: set[str] = set()
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument in flag_options:
            index += 1
            continue
        if argument in value_options or argument in repeated_value_options:
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                return False
            if argument in value_options and argument in seen_single_options:
                return False
            seen_single_options.add(argument)
            index += 2
            continue
        if argument.startswith("-"):
            return False
        positionals.append(argument)
        index += 1

    if subcommand == "tree" and not positionals:
        positional_shape = ()
    if len(positionals) != len(positional_shape):
        return False
    return all(
        kind not in {"id", "optional_id"}
        or (value.isascii() and value.isdigit() and int(value) > 0)
        for kind, value in zip(positional_shape, positionals)
    )


def _target_matches_chainlink_command(argv: list[str]) -> bool:
    if not argv or argv[0] not in _CHAINLINK_EXECUTABLES:
        return False
    # Clap marks these options global, so accept them at the executable,
    # resource, or subcommand level without widening any operand shape.
    arguments = [
        argument for argument in argv[1:]
        if argument not in _CHAINLINK_OUTPUT_OPTIONS
    ]
    if arguments[:1] == ["issue"]:
        return _chainlink_issue_arguments_match(arguments[1:])
    if arguments[:2] == ["session", "status"]:
        return all(option in _CHAINLINK_OUTPUT_OPTIONS for option in arguments[2:])
    return False


def _chainlink_command_is_mutation(argv: list[str]) -> bool:
    """Classify one admitted Chainlink argv without consulting IFC state."""
    if not _target_matches_chainlink_command(argv):
        return False
    arguments = [
        argument for argument in argv[1:]
        if argument not in _CHAINLINK_OUTPUT_OPTIONS
    ]
    return (
        arguments[:1] == ["issue"]
        and len(arguments) >= 2
        and arguments[1] in _CHAINLINK_MUTATION_SUBCOMMANDS
    )


def _chainlink_target_argv(target: str | None) -> list[str] | None:
    """Parse one bounded Chainlink shell target without consulting IFC state."""
    if not isinstance(target, str) or set(target) & _SHELL_CONTROL_CHARACTERS:
        return None
    try:
        argv = shlex.split(target)
    except ValueError:
        return None
    return argv if _target_matches_chainlink_command(argv) else None


#: Refused even when quoted. A newline inside a command string is never a
#: legitimate argument value, and ``repo_review`` deliberately routes multi-line
#: review bodies through ``--body-file`` rather than inline ``--body``.
_REFUSED_INSIDE_ANY_QUOTE = frozenset("\n\r")

#: Single quotes make every character literal, but double quotes do NOT: a shell
#: still performs ``$(...)`` and backtick substitution inside them. Treating
#: ``"$(cat /etc/passwd)"`` as a literal because it is quoted would be wrong.
_EXPANDS_INSIDE_DOUBLE_QUOTES = frozenset("$`")


#: Executables that can take code from argv or stdin. They may only be declared
#: with a pinned ``script``, and never with an option that sources code inline --
#: otherwise a grant to "run one script" is a grant to run anything.
_INTERPRETER_EXECUTABLES = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish",
    "python", "python2", "python3", "perl", "ruby", "node", "deno", "bun",
    "php", "lua", "Rscript", "awk", "gawk", "env", "xargs", "eval",
})

#: Options that make an interpreter read code from argv or stdin. Refused on any
#: declaration, interpreter or not: no admitted command needs them, and one of
#: them turns a pinned executable back into an arbitrary one.
_CODE_FROM_ARGV_OPTIONS = frozenset({"-c", "-e", "-m", "--command", "--eval", "-"})


@dataclass(frozen=True)
class DeclaredShellCommand:
    """One operator-declared command a scheduled job or poller may run.

    Declared where the job itself is defined -- ``scheduler.yaml`` or a poller
    manifest -- so mimir needs no built-in catalogue of every CLI a deployment
    might install. What stays in code is the SHAPE: an absolute pinned path, a
    non-empty subcommand table (so a bare binary is inexpressible, because
    ``gog gmail search`` and ``gog gmail send`` are the same binary), an option
    allowlist, and the interpreter rule.
    """

    executable: str
    path: Path
    subcommands: tuple[tuple[str, ...], ...] = ()
    options: tuple[str, ...] = ()
    script: Path | None = None


def _declaration_error(name: str, detail: str) -> ValueError:
    return ValueError(f"shell_commands[{name!r}]: {detail}")


def agent_writable_roots(home: Path | str | None = None) -> tuple[Path, ...]:
    """Directories the agent can write through its file tools.

    Read from ``Config.folders`` so this tracks the real write guard rather than
    a second copy of the list. Path-specific restrictions inside those roots are
    applied by ``_agent_writable_root_for_path``.
    """
    root = Path(home or os.environ.get("MIMIR_HOME", "")).expanduser()
    if not str(root) or str(root) == ".":
        return ()
    root = root.resolve()
    try:
        from .config import Config

        names = Config.from_env().writable_dirs
    except Exception:  # noqa: BLE001 - fall back to the shipped default
        from .config import DEFAULT_FOLDERS

        names = [name for name, mode in DEFAULT_FOLDERS.items() if mode == "rw"]
    roots: list[Path] = []
    for name in names:
        candidate = (root / name)
        try:
            roots.append(candidate.resolve())
        except OSError:
            continue
    # Home folders are not the whole write surface: MIMIR_FILE_TOOL_ROOTS adds
    # ``path:rw`` routes (and the default /tmp route), and configured repos can
    # be rw. A declaration under any of them is agent-writable just the same.
    #
    # ``_configured_file_write_roots()`` leads with MIMIR_HOME itself -- it is the
    # backend's write SINK root, not a statement that everything under home is
    # writable. Unioning it whole classified the entire home as writable, which
    # made the documented operator-owned ``<home>/scripts/`` undeclarable and
    # silently negated the per-folder calculation above. Only the external routes
    # are real write surface; ``Config.folders`` already decides what inside home
    # is writable, and it lists neither ``scripts`` nor ``prompts`` as rw.
    external = [
        candidate for candidate in _configured_file_write_roots()
        if candidate.resolve() != root
    ]
    for extra in (*external, *_configured_repo_write_roots()):
        try:
            resolved_extra = Path(extra).resolve()
        except OSError:
            continue
        if resolved_extra not in roots:
            roots.append(resolved_extra)
    return tuple(roots)


def _agent_writable_root_for_path(
    path: Path | str,
    writable_roots: tuple[Path, ...],
    *,
    admin_operator_turn: bool,
) -> Path | None:
    """Return the root through which this turn may rewrite *path*, if any.

    Only an untainted, trusted admin operator turn may make anything under
    ``skills/`` writable. This is a file-tool boundary, not a filesystem
    sandbox: admitted shell commands and subprocesses can still write there.
    """
    try:
        candidate = Path(path).resolve()
    except (OSError, RuntimeError):
        return None
    matching: list[tuple[Path, Path]] = []
    for raw_root in writable_roots:
        try:
            root = Path(raw_root).resolve()
        except (OSError, RuntimeError):
            continue
        try:
            matching.append((root, candidate.relative_to(root)))
        except ValueError:
            continue
    if not matching:
        return None
    if any(root.name == "skills" for root, _relative in matching) and not admin_operator_turn:
        return None
    return max((root for root, _relative in matching), key=lambda root: len(root.parts))


def parse_declared_shell_commands(
    raw: object, *, writable_roots: tuple[Path, ...] = (),
) -> tuple[DeclaredShellCommand, ...]:
    """Validate declared shell grants, or raise ``ValueError``.

    ``writable_roots`` are the directories the agent can write through its file
    tools (``Config.writable_dirs``). A declared script must lie outside all of
    them: the danger of an interpreter is *arbitrary* code, not code, so a script
    the running agent cannot modify is equivalent to a binary the operator
    installed. Resolution follows symlinks before the check, or a link inside a
    writable root would launder the path.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("shell_commands must be a list")
    resolved_writable = tuple(Path(root).resolve() for root in writable_roots)
    out: list[DeclaredShellCommand] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("each shell_commands entry must be a mapping")
        name = entry.get("exec")
        if not isinstance(name, str) or not name or "/" in name or name != Path(name).name:
            raise ValueError(f"shell_commands exec must be a bare command name, got {name!r}")
        unknown = set(entry) - {"exec", "path", "subcommands", "options", "script"}
        if unknown:
            raise _declaration_error(name, f"unknown keys {sorted(unknown)}")

        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise _declaration_error(name, "path is required")
        path = Path(raw_path)
        if not path.is_absolute():
            raise _declaration_error(name, f"path must be absolute, got {raw_path!r}")
        if not path.exists():
            # Fail at load, matching the existing scoped-root precedent: a grant
            # naming a binary that is not installed is a config error, and
            # deferring it surfaces later as an unexplained refusal.
            raise _declaration_error(name, f"path does not exist: {raw_path}")
        # The EXECUTABLE gets the same immutability rule as a script. Without
        # this, declaring a CLI under an agent-writable location lets the agent
        # replace the binary and run anything through an admitted command shape,
        # which would make every other check here decorative.
        path = path.resolve()
        if not path.is_file():
            raise _declaration_error(name, f"path is not a regular file: {raw_path}")
        if not os.access(path, os.X_OK):
            raise _declaration_error(name, f"path is not executable: {raw_path}")
        writable_root = _agent_writable_root_for_path(
            path, resolved_writable, admin_operator_turn=False,
        )
        if writable_root is not None:
            raise _declaration_error(
                name,
                f"path {raw_path} is inside the agent-writable root {writable_root}; "
                "an executable the agent can replace is arbitrary code execution",
            )

        options_raw = entry.get("options") or []
        if not isinstance(options_raw, list) or not all(isinstance(o, str) for o in options_raw):
            raise _declaration_error(name, "options must be a list of strings")
        for option in options_raw:
            if not option.startswith("-"):
                raise _declaration_error(name, f"option must start with '-': {option!r}")
            if option in _CODE_FROM_ARGV_OPTIONS:
                raise _declaration_error(
                    name,
                    f"{option!r} sources code from argv or stdin and is never admitted",
                )

        subcommands_raw = entry.get("subcommands") or []
        if not isinstance(subcommands_raw, list):
            raise _declaration_error(name, "subcommands must be a list of lists")
        subcommands: list[tuple[str, ...]] = []
        for item in subcommands_raw:
            if not isinstance(item, list) or not item or not all(isinstance(s, str) for s in item):
                raise _declaration_error(name, "each subcommand must be a non-empty list of strings")
            subcommands.append(tuple(item))

        raw_script = entry.get("script")
        script: Path | None = None
        # Classified on the RESOLVED binary as well as the declared name: the
        # two need not match, so ``exec: gog`` with ``path: /usr/bin/python3``
        # would otherwise present as a non-interpreter and skip the pinned-script
        # rule entirely. Either name being an interpreter is enough.
        is_interpreter = (
            name in _INTERPRETER_EXECUTABLES
            or path.name in _INTERPRETER_EXECUTABLES
            or path.name.rstrip("0123456789.") in _INTERPRETER_EXECUTABLES
        )
        if raw_script is not None:
            if not isinstance(raw_script, str) or not raw_script:
                raise _declaration_error(name, "script must be a non-empty string")
            script = Path(raw_script)
            if not script.is_absolute():
                raise _declaration_error(name, f"script must be absolute, got {raw_script!r}")
            resolved = script.resolve()
            if not resolved.exists():
                raise _declaration_error(name, f"script does not exist: {raw_script}")
            writable_root = _agent_writable_root_for_path(
                resolved, resolved_writable, admin_operator_turn=False,
            )
            if writable_root is not None:
                raise _declaration_error(
                    name,
                    f"script {raw_script} is inside the agent-writable root {writable_root}; "
                    "a script the agent can rewrite is arbitrary code execution",
                )
            script = resolved
        if is_interpreter and script is None:
            raise _declaration_error(
                name, "an interpreter may only be declared with a pinned 'script'",
            )
        if script is None and not subcommands:
            raise _declaration_error(
                name, "declare at least one subcommand, or a script for an interpreter",
            )
        out.append(DeclaredShellCommand(
            executable=name,
            path=path,
            subcommands=tuple(subcommands),
            options=tuple(options_raw),
            script=script,
        ))
    return tuple(out)


def _declared_command_execution_argv(
    argv: list[str], declared: tuple[DeclaredShellCommand, ...],
) -> list[str] | None:
    """Return the pinned argv when *argv* matches a declaration, else ``None``."""
    if not argv or not declared:
        return None
    for command in declared:
        if argv[0] != command.executable:
            continue
        arguments = argv[1:]
        if command.script is not None:
            if not arguments or Path(arguments[0]).resolve() != command.script:
                continue
            arguments = arguments[1:]
        elif not any(
            tuple(arguments[:len(prefix)]) == prefix for prefix in command.subcommands
        ):
            continue
        if not _arguments_match_allowlist(
            arguments,
            exact_options=frozenset(command.options),
            option_prefixes=tuple(f"{o}=" for o in command.options if o.startswith("--")),
        ):
            continue
        rest = argv[1:]
        if command.script is not None:
            rest = [str(command.script), *rest[1:]]
        return [str(command.path), *rest]
    return None


def _unquoted_shell_control_characters(target: str) -> list[str]:
    """Return the metacharacters appearing OUTSIDE quotes in *target*, sorted.

    Quoting is what separates an operator from a value. ``grep -r '<<<<<<<'``
    searches for a conflict marker; nothing redirects. Scanning the raw string
    treated the two alike, so a turn whose job was finding merge conflicts in a
    workspace it had just written could not search for conflict markers -- 60 of
    190 service-shell refusals measured on muninn, 2026-08-03..06.

    Unquoted metacharacters are still refused, so a real pipe, redirection or
    command separator is rejected exactly as before. The admitted argv is exec'd
    with ``shell=False``, so a quoted metacharacter reaches the process as one
    literal argument and is never parsed by anything.
    """
    found: set[str] = set()
    quote: str | None = None
    escaped = False
    for character in target:
        # Checked FIRST, ahead of escape consumption: a backslash-newline
        # continuation would otherwise be swallowed by the ``escaped`` branch and
        # ``shlex.split`` would carry the newline into an argv element, which is
        # exactly the inline multi-line value this rule exists to refuse.
        if character in _REFUSED_INSIDE_ANY_QUOTE:
            found.add(character)
            escaped = False
            continue
        if escaped:
            escaped = False
            continue
        # A backslash escapes the next character everywhere except inside single
        # quotes, where POSIX shells pass it through literally.
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            elif quote == '"' and character in _EXPANDS_INSIDE_DOUBLE_QUOTES:
                found.add(character)
            continue
        if character in ("'", '"'):
            quote = character
            continue
        if character in _SHELL_CONTROL_CHARACTERS:
            found.add(character)
    return sorted(found)


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
    if _target_matches_chainlink_command(argv):
        return True
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
                "-ld", "-dl", "-lh", "-hl", "--all", "--almost-all", "--directory",
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
                "-E", "-F", "-H", "-h", "-i", "-l", "-n", "-q", "-r", "-s",
                "-v", "-w", "-x", "--extended-regexp", "--fixed-strings",
                "--files-with-matches", "--ignore-case", "--line-number",
                "--no-messages", "--quiet", "--recursive", "--invert-match",
                "--with-filename", "--no-filename", "--word-regexp",
                "--line-regexp",
            }),
            option_prefixes=("--exclude=", "--include=", "--exclude-dir="),
        )
    if command == "jq":
        # Filters are intentionally unconstrained: jq is useful precisely as a
        # JSON expression language. direct_exec_env() must therefore give the
        # pinned jq child only a minimal non-secret environment; never replace
        # that control with a filter-text denylist for env/$ENV.
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
    option_arguments = (
        subcommand_arguments[:subcommand_arguments.index("--")]
        if "--" in subcommand_arguments
        else subcommand_arguments
    )
    if not required_safety_options.issubset(option_arguments):
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


def _target_matches_npm_ci_command(arguments: list[str]) -> bool:
    """Require a script-free clean install with no operands or option terminator."""
    allowed_options = frozenset({
        "--ignore-scripts", "--include=dev", "--no-audit", "--no-fund",
        "--omit=optional", "--prefer-offline",
    })
    return (
        arguments.count("--ignore-scripts") == 1
        and all(argument in allowed_options for argument in arguments)
    )


#: Cap on a captured review body. GitHub rejects review bodies past ~65k, so a
#: larger file is a mistake or an attempt to wedge the exec, not a real review.
_REVIEW_BODY_MAX_BYTES = 65_536


def _capture_review_body_beneath_scratch(path_text: str) -> str | None:
    """Read a review body from scratch through descriptor-relative no-follow IO.

    Validating a pathname and then handing that same pathname to ``gh`` is a
    check/use race: ``Path.resolve()`` proves only what the path meant at check
    time, and any service-writable process can swap the file — or a parent
    component — for a symlink pointing outside scratch before ``gh`` opens it,
    publishing arbitrary readable content as a PR review.

    So the pathname never survives authorization. We anchor on the fully
    resolved scratch root (``resolve()`` leaves no symlinks in it), walk each
    component with ``O_NOFOLLOW`` (openat semantics), read the contents here,
    and the caller substitutes ``--body <captured>`` into the already-parsed
    argv. Inlining multiline text is safe at that point precisely because no
    shell reparses an argv list — the control-character rule that forced a file
    in the first place applies to the raw command string, not to argv.

    Mirrors the spawn-artifact hardening in ``tools/registry.py`` (#1134).
    Returns ``None`` when the body cannot be captured safely, which the caller
    turns into a refusal.
    """
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return None
    scratch_root = (Path(home).resolve() / "scratch").resolve()
    candidate = Path(path_text)
    if not candidate.is_absolute():
        candidate = Path(home).resolve() / candidate
    # Lexical containment first, so a traversal never reaches the walk.
    try:
        relative = candidate.relative_to(scratch_root)
    except ValueError:
        return None
    parts = relative.parts
    if not parts or ".." in parts:
        return None

    opened: list[int] = []
    try:
        opened.append(os.open(scratch_root, os.O_RDONLY | os.O_DIRECTORY))
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            opened.append(
                os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=opened[-1],
                )
            )
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=opened[-1])
    except OSError:
        return None
    finally:
        for fd_open in opened:
            try:
                os.close(fd_open)
            except OSError:
                pass
    try:
        with os.fdopen(fd, "rb") as handle:
            # Read one byte past the cap so an oversize file is detected rather
            # than silently truncated into a published review.
            raw = handle.read(_REVIEW_BODY_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _REVIEW_BODY_MAX_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _repo_review_argv_with_captured_body(argv: list[str]) -> list[str] | None:
    """Replace ``--body-file <path>`` with ``--body <captured contents>``.

    Returns ``argv`` unchanged when no body file is named, or ``None`` when a
    named body cannot be captured safely.
    """
    if "--body-file" not in argv:
        return argv
    out: list[str] = []
    index = 0
    while index < len(argv):
        if argv[index] != "--body-file":
            out.append(argv[index])
            index += 1
            continue
        if index + 1 >= len(argv):
            return None
        body = _capture_review_body_beneath_scratch(argv[index + 1])
        if body is None:
            return None
        out.extend(["--body", body])
        index += 2
    return out


def _git_arguments_without_restrictive_global_options(
    arguments: list[str],
) -> list[str]:
    """Strip global Git options that can only make inspection more restrictive."""
    while arguments[:1] and arguments[0] in {"--no-pager", "--no-ext-diff"}:
        arguments = arguments[1:]
    return arguments


def _repo_review_git_execution_argv(argv: list[str], state: Any) -> list[str] | None:
    """Return hardened argv for repo inspection or branch-scoped mutation."""
    if not argv or argv[0] != "git":
        return None
    git_arguments = _git_arguments_without_restrictive_global_options(argv[1:])
    if git_arguments[:1] == ["-C"]:
        if state is None or len(git_arguments) < 3:
            return None
        try:
            if Path(git_arguments[1]).resolve() != Path(state.root).resolve():
                return None
        except (OSError, RuntimeError):
            return None
        subcommand = git_arguments[2]
        arguments = git_arguments[3:]
    elif git_arguments:
        subcommand = git_arguments[0]
        arguments = git_arguments[1:]
    else:
        return None
    arguments = list(arguments)
    required_action = {
        "checkout": RepoPRAction.CHECKOUT,
        "add": RepoPRAction.WRITE,
        "commit": RepoPRAction.COMMIT,
        "worktree": RepoPRAction.CHECKOUT,
        "pull": RepoPRAction.CHECKOUT,
        "push": RepoPRAction.PUSH,
    }.get(subcommand, RepoPRAction.INSPECT)
    if state is not None and not _repo_review_action_allowed(state, required_action):
        return None
    if state is None and required_action is not RepoPRAction.INSPECT:
        return None
    branch = getattr(state, "head_ref", "")
    if subcommand == "status":
        allowed = (
            all(argument.startswith("-") and argument != "--" for argument in arguments)
            and _arguments_match_allowlist(
                arguments,
                exact_options=frozenset({
                    "-b", "-s", "--ahead-behind", "--branch",
                    "--ignore-submodules", "--long", "--no-ahead-behind",
                    "--porcelain", "--short", "--show-stash",
                    "--untracked-files",
                }),
                option_prefixes=(
                    "--ignore-submodules=", "--porcelain=", "--untracked-files=",
                ),
            )
        )
    elif _repo_review_git_read_arguments(subcommand, arguments):
        allowed = True
    elif subcommand == "checkout":
        allowed = arguments in ([branch], ["-B", branch])
    elif subcommand == "add":
        path_arguments = arguments[1:] if arguments[:1] == ["--"] else arguments
        allowed = state.checked_out and (
            arguments in (["--all"], ["-A"])
            or bool(path_arguments)
            and all(_repo_review_relative_path(path) for path in path_arguments)
        )
    elif subcommand == "commit":
        message = None
        if len(arguments) == 2 and arguments[0] == "-m":
            message = arguments[1] if arguments[1].strip() else None
        elif len(arguments) == 2 and arguments[0] == "--file":
            message = _capture_review_body_beneath_scratch(arguments[1])
        allowed = state.checked_out and message is not None
        if allowed:
            arguments = ["-m", message]
    elif subcommand == "worktree":
        allowed = (
            arguments[:1] == ["add"]
            and len(arguments) == 3
            and _repo_review_write_path(arguments[1], state.root)
            and arguments[2] == branch
        )
    elif subcommand == "pull":
        allowed = state.checked_out and arguments == ["--ff-only", "origin", branch]
    elif subcommand == "push":
        push_options = []
        while arguments[:1] and arguments[0] in {
            "--dry-run", "-u", "--set-upstream",
        }:
            push_options.append(arguments.pop(0))
        allowed = (
            state.checked_out
            and len(arguments) == 2
            and arguments[0] == "origin"
            and _repo_review_push_refspec(arguments[1], branch)
        )
        arguments = [*push_options, *arguments]
    else:
        return None
    if not allowed:
        return None

    git = _maintenance_resolved_pin("git")
    if git is None:
        return None
    root = Path(state.root).resolve() if state is not None else Path.cwd().resolve()
    filter_overrides = _maintenance_git_filter_overrides(root, str(git))
    if filter_overrides is None:
        return None
    transport_overrides = (
        ["-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always"]
        if subcommand == "push"
        else []
    )
    credential_overrides = (
        ["-c", "credential.helper="]
        if required_action is RepoPRAction.INSPECT
        else []
    )
    identity_overrides: list[str] = []
    if subcommand == "commit":
        from .git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME

        identity_overrides = [
            "-c", f"user.name={DEFAULT_USER_NAME}",
            "-c", f"user.email={DEFAULT_USER_EMAIL}",
        ]
    safety_options: list[str] = []
    if subcommand in {"diff", "log", "show"}:
        safety_options = ["--no-ext-diff", "--no-textconv"]
    elif subcommand in {"blame", "grep"}:
        safety_options = ["--no-textconv"]
    execution_argv = [
        str(git), "-C", str(root),
        *_MAINTENANCE_GIT_BASE_OVERRIDES,
        "-c", f"safe.directory={root}",
        *credential_overrides,
        *transport_overrides,
        *identity_overrides,
        *filter_overrides,
        "--no-pager", "--no-optional-locks", subcommand, *safety_options, *arguments,
    ]
    return execution_argv


def _repo_review_relative_path(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _repo_review_write_path(value: str, root: str) -> bool:
    """Confine a Git-created worktree to an explicit repository write root."""
    from ._paths import PathOutsideHomeError, resolve_within_roots

    try:
        candidate = Path(value)
        resolve_within_roots(
            _configured_repo_write_roots(),
            str(candidate if candidate.is_absolute() else Path(root) / candidate),
        )
    except (IndexError, OSError, PathOutsideHomeError, RuntimeError, ValueError):
        return False
    return True


def _repo_review_owned_branch(branch: str, event_branch: str) -> bool:
    """Admit only the event's own branch, inside a namespace the flow owns.

    Equality is required for every namespace, not just ``worklink/``. A
    namespace-only rule let one leaf's run push to a sibling's branch --
    ``issue/1029-a1`` to ``refs/heads/issue/1030-a1`` -- which fast-forwards
    commits into another leaf's PR while it is under review. Cross-leaf
    contamination is a demonstrated failure mode here (#1019: a build wrote
    into a concurrent sibling's worktree, twice in seven builds), and Worklink
    runs two ``issue/*`` builds concurrently by default.

    Requiring equality is safe because each ``RepoReviewState`` is constructed
    for one ``pr_changes_requested_stale`` item, so ``head_ref`` is the one PR
    branch that state can remediate; no selected state legitimately targets a
    different branch.
    """
    return (
        _valid_git_branch(branch)
        and branch == event_branch
        and (
            branch.startswith("issue/")
            or branch.startswith("fix/")
            or branch.startswith("worklink/")
        )
    )


def _repo_review_push_refspec(refspec: str, event_branch: str) -> bool:
    if refspec.startswith("+") or refspec.count(":") != 1:
        return False
    source, destination = refspec.split(":", 1)
    if not destination.startswith("refs/heads/"):
        # Preserve the existing exact event-branch form.
        return (
            source == event_branch
            and destination == event_branch
            and _repo_review_owned_branch(event_branch, event_branch)
        )
    destination_branch = destination.removeprefix("refs/heads/")
    return (
        _repo_review_owned_branch(destination_branch, event_branch)
        and source in {"FETCH_HEAD", destination_branch, event_branch}
    )


def _repo_review_gh_api_arguments(arguments: list[str]) -> bool:
    """Admit one parameterized API path and GET-only transport options."""
    path: str | None = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--paginate":
            index += 1
            continue
        if argument in {"-X", "--method"}:
            if index + 1 >= len(arguments) or arguments[index + 1] != "GET":
                return False
            index += 2
            continue
        if argument.startswith("-") or path is not None:
            return False
        path = argument
        index += 1
    if path is None or re.fullmatch(r"[A-Za-z0-9._~!()+,=:@%/-]+", path) is None:
        return False
    return all(segment not in {"", ".", ".."} for segment in path.split("/"))


def _repo_review_gh_issue_view_arguments(arguments: list[str]) -> bool:
    if not arguments or arguments[0].startswith("-"):
        return False
    index = 1
    while index < len(arguments):
        option = arguments[index]
        if option == "--comments":
            index += 1
            continue
        if option in {"--json", "--repo"}:
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                return False
            index += 2
            continue
        return False
    return True


def _repo_review_git_read_arguments(subcommand: str, arguments: list[str]) -> bool:
    # This allowlist is intentionally local-only. In particular, ls-remote is
    # excluded even though it is read-only: it contacts a remote and would need
    # a separate authenticated-network policy instead of INSPECT's disabled
    # credential helper.
    #
    # Options such as grep's ``-c`` are parsed only after the subcommand has
    # already been selected. They cannot become Git's top-level ``-c`` config
    # injection form.
    if subcommand == "log":
        before_separator, separator, pathspecs = arguments, [], []
        if "--" in arguments:
            position = arguments.index("--")
            before_separator = arguments[:position]
            separator = ["--"]
            pathspecs = arguments[position + 1:]
        counts_removed = [
            value for value in before_separator
            if not (value.startswith("-") and value[1:].isdigit())
        ]
        return (
            (not separator or bool(pathspecs))
            and _arguments_match_allowlist(
                counts_removed,
                exact_options=frozenset({
                    "--abbrev-commit", "--decorate", "--name-only", "--name-status",
                    "--no-color", "--no-merges", "--no-patch", "--oneline", "--stat",
                }),
                option_prefixes=("--max-count=", "--since=", "--until="),
            )
        )
    if subcommand in {"diff", "show"}:
        return _arguments_match_allowlist(
            arguments,
            exact_options=frozenset({
                "-p", "--abbrev-commit", "--cached", "--check", "--decorate",
                "--exit-code", "--full-index", "--name-only", "--name-status",
                "--no-color", "--no-merges", "--no-patch", "--oneline",
                "--quiet", "--raw", "--stat", "--staged",
            }),
            option_prefixes=("-U", "--max-count=", "--since=", "--until=", "--unified="),
        )
    if subcommand == "grep":
        operands = [value for value in arguments if value != "--" and not value.startswith("-")]
        return bool(operands) and _arguments_match_allowlist(
            arguments,
            exact_options=frozenset({
                "-E", "-F", "-I", "-i", "-l", "-n", "-q", "-v", "-w",
                "--cached", "--extended-regexp", "--files-with-matches",
                "--fixed-strings", "--ignore-case", "--line-number", "--quiet",
                "--untracked", "--invert-match", "--word-regexp",
            }),
            option_prefixes=("--max-count=",),
        )
    if subcommand == "blame":
        before_separator = arguments
        pathspecs: list[str] = []
        if "--" in arguments:
            position = arguments.index("--")
            before_separator = arguments[:position]
            pathspecs = arguments[position + 1:]
            if len(pathspecs) != 1:
                return False
        if any(value == "-L" for value in before_separator):
            return False
        operands = [value for value in before_separator if not value.startswith("-")]
        return (
            (len(operands) in ({0, 1} if pathspecs else {1, 2}))
            and _arguments_match_allowlist(
                before_separator,
                exact_options=frozenset({
                    "-b", "-l", "-p", "-s", "-w", "--first-parent",
                    "--line-porcelain", "--porcelain", "--root", "--show-stats",
                }),
                option_prefixes=("-L",),
            )
        )
    if subcommand == "merge-base":
        if arguments[:1] == ["--is-ancestor"]:
            arguments = arguments[1:]
        return len(arguments) == 2 and all(not value.startswith("-") for value in arguments)
    if subcommand == "rev-list":
        return (
            len(arguments) == 2
            and arguments[0] == "--count"
            and not arguments[1].startswith("-")
        )
    if subcommand == "rev-parse":
        return bool(arguments) and _arguments_match_allowlist(
            arguments,
            exact_options=frozenset({
                "--abbrev-ref", "--git-dir", "--is-inside-work-tree", "--short",
                "--show-prefix", "--show-toplevel", "--verify",
            }),
            option_prefixes=("--short=",),
        )
    if subcommand == "remote":
        return arguments == ["-v"]
    if subcommand == "branch":
        return bool(arguments) and arguments[0] == "--list" and all(
            not value.startswith("-") for value in arguments[1:]
        )
    if subcommand == "worktree":
        return arguments == ["list", "--porcelain"]
    return False


def _option_value(arguments: list[str], option: str) -> str | None:
    """Return one separated option value, rejecting absent or repeated options."""
    positions = [index for index, value in enumerate(arguments) if value == option]
    if len(positions) != 1 or positions[0] + 1 >= len(arguments):
        return None
    value = arguments[positions[0] + 1]
    return None if value.startswith("-") else value


def _repo_review_action_allowed(review_state: Any, action: RepoPRAction) -> bool:
    scope = getattr(review_state, "action_scope", None)
    return (
        scope is not None
        and action.value in getattr(scope, "allowed_operations", frozenset())
    )


def _target_matches_repo_review_shell_command(
    argv: list[str], review_state: Any = None,
) -> bool:
    """Validate commands needed to inspect and test a trusted repository PR."""
    if _target_matches_read_only_shell_command(argv):
        return True
    if not argv:
        return False

    # ``--jq`` is deliberately absent from every option set below, here and in
    # the maintenance profile. ``gh`` evaluates the filter in-process, and jq's
    # ``env`` / ``$ENV`` builtins read the process environment. ``gh`` is the one
    # service-shell executable explicitly given GITHUB_TOKEN, so ``gh pr view
    # <n> --repo <r> --json reviews --jq env`` was therefore an
    # admitted command that printed DISCORD_TOKEN, GITHUB_TOKEN, GPG_KEY,
    # MIMIR_API_KEY and the provider keys into the tool result, and from there
    # into the model's context and the turn transcript. Enforcement was no
    # defence: the command was ALLOWED, not merely unblocked.
    #
    # Nothing is lost by removing it. Every non-trivial filter was already
    # refused, because ``|``, ``[``, ``]`` and ``{`` are shell metacharacters and
    # the profile scans the raw command string before splitting it; only degenerate
    # forms like ``--jq .reviews`` ever got through. ``--json`` returns the same
    # data and the caller filters it itself.
    #
    # Do not "fix" this by blocklisting ``env`` and ``$ENV``: that is a denylist
    # over an expression language, and the next builtin that reaches process
    # state reopens it. ``--template`` is retained because gh's template function
    # set is fixed and exposes no environment accessor — verify that claim again
    # before adding any option that evaluates a caller-supplied expression.
    if argv[0] == "gh" and argv[1:2] == ["api"]:
        return _repo_review_gh_api_arguments(argv[2:])

    if argv[0] == "gh" and argv[1:] == ["auth", "status"]:
        return True

    if argv[0] == "gh" and len(argv) >= 3 and argv[1] == "issue":
        return argv[2] == "view" and _repo_review_gh_issue_view_arguments(argv[3:])

    if argv[0] == "gh" and len(argv) >= 3 and argv[1] == "pr":
        subcommand = argv[2]
        if subcommand == "checkout":
            return _repo_review_action_allowed(review_state, RepoPRAction.CHECKOUT) and argv[3:] == [
                str(review_state.pr_number),
                "--repo", review_state.repo,
                "--branch", review_state.head_ref,
            ]
        if subcommand in {"edit", "comment"}:
            required_actions = (
                (RepoPRAction.PR_EDIT, RepoPRAction.PR_REREQUEST)
                if subcommand == "edit"
                else (RepoPRAction.PR_COMMENT,)
            )
            if (
                not all(
                    _repo_review_action_allowed(review_state, action)
                    for action in required_actions
                )
                or argv[3:4] != [str(review_state.pr_number)]
            ):
                return False
            arguments = argv[4:]
            options = (
                frozenset({"--add-reviewer", "--body-file", "--repo"})
                if subcommand == "edit"
                else frozenset({"--body-file", "--repo"})
            )
            return (
                _arguments_match_allowlist(arguments, exact_options=options)
                and _option_value(arguments, "--repo") == review_state.repo
                and _option_value(arguments, "--body-file") is not None
                and (
                    subcommand != "edit"
                    or _option_value(arguments, "--add-reviewer") is not None
                )
            )
        options = {
            "view": frozenset({
                "-R", "--comments", "--json", "--repo", "--template",
            }),
            "diff": frozenset({"--color", "--name-only", "--patch", "--repo"}),
            "checks": frozenset({
                "--fail-fast", "--interval", "--json", "--repo",
                "--required", "--watch",
            }),
            # Submitting the review is the point of the repo_review profile —
            # without this the poller can read a PR and reach a verdict but has
            # no way to post it, which is exactly what happened when this
            # profile first went live (the agent reported every command
            # "exiting 1 with empty output" and messaged the operator instead).
            #
            # ``--body-file`` is required, not a convenience: a review body is
            # inherently multi-line, and ``\n`` is in
            # ``_SHELL_CONTROL_CHARACTERS`` (correctly — it separates commands),
            # so a multi-line ``--body`` can never be admitted. Command
            # substitution (``--body "$(cat ...)"``) is rejected for the same
            # reason. A file is therefore the only way to carry a real review.
            #
            # Its path is constrained to the scratch root below. Unconstrained,
            # ``--body-file <home>/.env`` would publish the operator's secrets
            # to GitHub as a review — egress wearing a review's clothes. Scoped
            # to scratch, a service can only publish what it wrote there itself.
            "review": frozenset({
                "--approve", "--body", "--body-file", "--comment", "--repo",
                "--request-changes",
            }),
        }.get(subcommand)
        if options is None or not _arguments_match_allowlist(
            argv[3:], exact_options=options,
        ):
            return False
        if subcommand == "review":
            if (
                not _repo_review_action_allowed(review_state, RepoPRAction.PR_REVIEW)
                or argv[3:4] != [str(review_state.pr_number)]
                or _option_value(argv[4:], "--repo") != review_state.repo
            ):
                return False
        # The body file's SAFETY is enforced at capture time, not here — see
        # ``_capture_review_body_beneath_scratch``. Admission only decides the
        # option is permitted.
        return True

    if argv[0] == "git":
        git_arguments = _git_arguments_without_restrictive_global_options(argv[1:])
        if review_state is not None and git_arguments[:1] == ["-C"]:
            return _repo_review_git_execution_argv(argv, review_state) is not None
        if not git_arguments:
            return False
        subcommand = git_arguments[0]
        arguments = git_arguments[1:]
        if review_state is not None and subcommand in {
            "add", "checkout", "commit", "pull", "push", "worktree",
        }:
            return _repo_review_git_execution_argv(argv, review_state) is not None
        if _repo_review_git_read_arguments(subcommand, arguments):
            return True
        if subcommand == "fetch":
            if not _arguments_match_allowlist(
                arguments,
                exact_options=frozenset({
                    "--append", "--atomic", "--dry-run", "--no-tags", "--prune",
                    "--quiet", "--tags", "--verbose",
                }),
                option_prefixes=("--depth=", "--deepen=", "--filter="),
            ):
                return False
            operands = [argument for argument in arguments if not argument.startswith("-")]
            # A URL or ext:: remote can select a helper executable. Review
            # fetches use only the checkout's conventional configured remotes.
            return not operands or operands[0] in {"origin", "upstream"}
        return False

    if argv[0] == "npm":
        if argv[1:2] == ["ci"]:
            return _target_matches_npm_ci_command(argv[2:])
        return False
    return False


_MAINTENANCE_PINNED_EXECUTABLE_DEFAULTS = {
    "git": Path("/usr/bin/git"),
    "gh": Path("/usr/bin/gh"),
    "npm": Path("/usr/lib/node_modules/npm/bin/npm-cli.js"),
    "node": Path("/usr/bin/node"),
    "uv": Path("/usr/local/bin/uv"),
    "ls": Path("/usr/bin/ls"),
    "grep": Path("/usr/bin/grep"),
    "wc": Path("/usr/bin/wc"),
    "pwd": Path("/usr/bin/pwd"),
    "jq": Path("/usr/bin/jq"),
    "rg": Path("/usr/bin/rg"),
    "chainlink": Path("/usr/local/bin/chainlink"),
    # Read-only inspection. Admitted because the shipped scheduled-tick prompts
    # already reach for them and had no admitted equivalent: 59 of 190 service
    # shell refusals measured on muninn over 2026-08-03..06 were these five.
    "cat": Path("/usr/bin/cat"),
    "head": Path("/usr/bin/head"),
    "tail": Path("/usr/bin/tail"),
    "stat": Path("/usr/bin/stat"),
    "date": Path("/usr/bin/date"),
}
_MAINTENANCE_PINNED_EXECUTABLES = _MAINTENANCE_PINNED_EXECUTABLE_DEFAULTS.copy()
_MAINTENANCE_PINNED_SCRIPT_INTERPRETERS = {
    "npm": "node",
}
_MAINTENANCE_GIT_BASE_OVERRIDES = (
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.external=",
    "-c", "protocol.allow=never",
)
_MAINTENANCE_GIT_ENV_DENY_PREFIXES = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_ATTR_NOSYSTEM",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_EXEC_PATH",
    "GIT_EXTERNAL_DIFF",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_OPTIONAL_LOCKS",
    "GIT_PREFIX",
    "GIT_QUARANTINE_PATH",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)


def _maintenance_git_probe_env() -> dict[str, str]:
    """Return a deterministic environment for authorization-time Git probes."""
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith(_MAINTENANCE_GIT_ENV_DENY_PREFIXES):
            env.pop(key, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_PAGER": "cat",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    return env


def _maintenance_git_filter_overrides(
    root: Path, git_executable: str,
) -> list[str] | None:
    """Return argv overrides that disable configured content filter drivers.

    Git has no generic "disable all filters" switch. A checkout-controlled
    ``.gitattributes`` file can select any filter driver whose command is defined
    in repository config, and even a read-only ``diff`` can invoke its clean
    side. Enumerate the effective command-bearing keys with the same hardened
    binary/config/environment contract as execution, validate the NUL-delimited
    names, then shadow each command with an empty command in the final argv.

    Repo-test snapshots are owned by the controller's ``mimir_uid`` while the
    suite runs as ``worklink_uid``. Git otherwise rejects this local-config read
    as dubious ownership. The profile has already bound and authorized ``root``,
    so trust that exact directory rather than inheriting caller identity.
    """
    command = [
        git_executable, "-C", str(root),
        *_MAINTENANCE_GIT_BASE_OVERRIDES,
        "-c", f"safe.directory={root}",
        "--no-pager", "config", "--local", "--null", "--name-only",
        "--get-regexp", r"^filter\..*\.(clean|smudge|process)$",
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
            env=_maintenance_git_probe_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 1 and not result.stdout:
        return []
    if result.returncode != 0:
        return None

    raw_names = result.stdout.split(b"\0")
    if raw_names[-1:] == [b""]:
        raw_names.pop()
    overrides: list[str] = []
    for raw_name in raw_names:
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not re.fullmatch(r"filter\.[^.\x00]+\.(?:clean|smudge|process)", name):
            return None
        overrides.extend(("-c", f"{name}="))
    return overrides


def _maintenance_git_subcommand_allowed(arguments: list[str]) -> bool:
    """Return whether arguments after Git's global options are admitted."""
    if not arguments:
        return False
    subcommand = arguments[0]
    subcommand_arguments = arguments[1:]
    if subcommand == "status":
        # ``--verbose``/``-v`` renders a diff and can execute a configured
        # textconv/filter helper. Keep maintenance status to metadata-only
        # forms; the hardened argv still disables fsmonitor and optional locks.
        return (
            "--" not in subcommand_arguments
            and not any(argument.startswith("-v") for argument in subcommand_arguments)
            and _arguments_match_allowlist(
                subcommand_arguments,
                exact_options=frozenset({
                    "-b", "-s", "--ahead-behind", "--branch",
                    "--ignore-submodules", "--long", "--no-ahead-behind",
                    "--porcelain", "--short", "--show-stash",
                    "--untracked-files",
                }),
                option_prefixes=(
                    "--ignore-submodules=", "--porcelain=", "--untracked-files=",
                ),
            )
        )
    if subcommand == "log":
        # ``-<digits>`` is Git's bounded max-count shorthand used by the
        # maintenance prompts (for example ``git log --oneline -5``).
        arguments_without_count_shorthand = [
            argument
            for argument in subcommand_arguments
            if not (argument.startswith("-") and argument[1:].isdigit())
        ]
        return (
            "--" not in subcommand_arguments
            and _arguments_match_allowlist(
                arguments_without_count_shorthand,
                exact_options=frozenset({
                    "-p", "--abbrev-commit", "--all", "--cached", "--check",
                    "--decorate", "--exit-code", "--full-index", "--grep",
                    "--name-only", "--name-status", "--no-color", "--no-ext-diff",
                    "--no-merges", "--no-patch", "--no-textconv", "--oneline",
                    "--quiet", "--raw", "--staged", "--stat",
                }),
                option_prefixes=(
                    "-U", "--grep=", "--max-count=", "--since=", "--unified=",
                    "--until=",
                ),
            )
        )
    if subcommand == "diff":
        return "--" not in subcommand_arguments and _arguments_match_allowlist(
            subcommand_arguments,
            exact_options=frozenset({
                "-p", "--abbrev-commit", "--cached", "--check", "--decorate",
                "--exit-code", "--full-index", "--grep", "--name-only",
                "--name-status", "--no-color", "--no-ext-diff", "--no-merges",
                "--no-patch", "--no-textconv", "--oneline", "--quiet", "--raw",
                "--staged", "--stat",
            }),
            option_prefixes=(
                "-U", "--grep=", "--max-count=", "--since=", "--unified=",
                "--until=",
            ),
        )
    if subcommand == "show":
        return "--" not in subcommand_arguments and _arguments_match_allowlist(
            subcommand_arguments,
            exact_options=frozenset({
                "-p", "--abbrev-commit", "--cached", "--check", "--decorate",
                "--exit-code", "--full-index", "--grep", "--name-only",
                "--name-status", "--no-color", "--no-ext-diff", "--no-merges",
                "--no-patch", "--no-textconv", "--oneline", "--quiet", "--raw",
                "--staged", "--stat",
            }),
            option_prefixes=(
                "-U", "--grep=", "--max-count=", "--since=", "--unified=",
                "--until=",
            ),
        )
    if subcommand == "rev-parse":
        return bool(subcommand_arguments) and _arguments_match_allowlist(
            subcommand_arguments,
            exact_options=frozenset(),
        )
    if subcommand == "branch":
        listing_options = frozenset({
            "-a", "-r", "--all", "--list", "--remotes", "--show-current",
        })
        return (
            any(argument in listing_options for argument in subcommand_arguments)
            and _arguments_match_allowlist(
                subcommand_arguments,
                exact_options=listing_options,
            )
        )
    if subcommand == "cat-file":
        return (
            len(subcommand_arguments) == 2
            and subcommand_arguments[0] == "-s"
            and not subcommand_arguments[1].startswith("-")
            and _arguments_match_allowlist(
                subcommand_arguments,
                exact_options=frozenset({"-s"}),
            )
        )
    if subcommand == "ls-tree":
        return bool(subcommand_arguments) and _arguments_match_allowlist(
            subcommand_arguments,
            exact_options=frozenset(),
        )
    if subcommand == "ls-files":
        return not subcommand_arguments and _arguments_match_allowlist(
            subcommand_arguments,
            exact_options=frozenset(),
        )
    return False


def _maintenance_git_execution_argv(argv: list[str]) -> list[str] | None:
    """Validate maintenance Git input and return its hardened execution argv.

    Git's ostensibly read-only commands can execute helpers selected by checkout
    configuration and attributes. Bind every invocation to a configured
    file-tool root, neutralize fsmonitor/external-diff/filter configuration in
    the returned execution artifact, and append explicit textconv/external-diff
    disables to diff-producing subcommands.
    """
    if not argv or argv[0] != "git":
        return None
    pinned_git = _maintenance_pinned_execution_argv(["git"])
    if pinned_git is None:
        return None

    arguments = _git_arguments_without_restrictive_global_options(argv[1:])
    requested_root: str | None = None
    if arguments[:1] == ["-C"]:
        if len(arguments) < 3 or arguments[1].startswith("-"):
            return None
        requested_root = arguments[1]
        arguments = arguments[2:]
    arguments = _git_arguments_without_restrictive_global_options(arguments)

    from ._paths import PathOutsideHomeError, resolve_within_roots

    roots = _configured_maintenance_git_roots()
    try:
        root = resolve_within_roots(
            roots,
            requested_root if requested_root is not None else str(roots[0]),
        )
    except (IndexError, OSError, PathOutsideHomeError, RuntimeError):
        return None

    if not _maintenance_git_subcommand_allowed(arguments):
        return None
    subcommand = arguments[0]
    subcommand_arguments = arguments[1:]

    filter_overrides = _maintenance_git_filter_overrides(root, pinned_git[0])
    if filter_overrides is None:
        return None
    execution_argv = [
        pinned_git[0], "-C", str(root),
        *_MAINTENANCE_GIT_BASE_OVERRIDES,
        "-c", f"safe.directory={root}",
        "-c", "credential.helper=",
        *filter_overrides, "--no-pager", "--no-optional-locks",
        subcommand, *subcommand_arguments,
    ]
    if subcommand in {"diff", "log", "show"}:
        execution_argv.extend(("--no-ext-diff", "--no-textconv"))
    return execution_argv


def _upgrade_workspace_git_execution_argv(argv: list[str]) -> list[str] | None:
    """Harden read-only Git after binding ``-C`` to ``scratch/proposals``."""
    if not argv or argv[0] != "git":
        return None
    arguments = _git_arguments_without_restrictive_global_options(argv[1:])
    if arguments[:1] != ["-C"] or len(arguments) < 3:
        return None
    requested_root = arguments[1]
    remaining = arguments[2:]
    if remaining[:1] == ["--no-pager"]:
        remaining = remaining[1:]

    from ._paths import PathOutsideHomeError, resolve_within_roots

    proposal_root = _upgrade_proposals_root()
    if proposal_root is None:
        return None
    try:
        resolve_within_roots([proposal_root], requested_root)
    except (OSError, PathOutsideHomeError, RuntimeError):
        return None
    return _maintenance_git_execution_argv(
        ["git", "-C", requested_root, *remaining]
    )


def _maintenance_pin_is_service_writable(expected: Path) -> bool:
    """Return whether a configured service-writable root contains *expected*."""
    try:
        resolved = expected.resolve(strict=True)
        return any(
            resolved == root or resolved.is_relative_to(root)
            for root in _static_service_write_roots()
        )
    except (OSError, RuntimeError):
        return True


def _maintenance_resolved_pin(command: str) -> Path | None:
    """Validate one pin as a trusted executable outside service-writable roots."""
    expected = _MAINTENANCE_PINNED_EXECUTABLES.get(command)
    if expected is None:
        from .tools.budget_gate import _emit_hard_boundary_denied

        _emit_hard_boundary_denied(
            tool="shell_exec",
            boundary="maintenance_pinned_executable",
            reason="maintenance_executable_pin_missing",
            target=command,
        )
        return None
    try:
        resolved = expected.resolve(strict=True)
        if resolved != expected:
            raise OSError("expected a non-symlink executable")
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise OSError("expected an executable regular file")
        if _maintenance_pin_is_service_writable(resolved):
            raise OSError("pin resolves within a configured service-writable root")
    except (OSError, RuntimeError) as exc:
        log.error(
            "maintenance_pinned_executable_missing command=%s expected=%s error=%s",
            command, expected, exc,
        )
        from .tools.budget_gate import _emit_hard_boundary_denied

        _emit_hard_boundary_denied(
            tool="shell_exec",
            boundary="maintenance_pinned_executable",
            reason="maintenance_executable_pin_invalid",
            target=str(expected),
        )
        return None
    return resolved


def _maintenance_pinned_execution_argv_with_reason(
    argv: list[str],
) -> tuple[list[str] | None, str]:
    """Replace an admitted service command with a trusted absolute program.

    Service commands execute with ``shell=False``, but a bare ``argv[0]``
    would still be selected through PATH. Bind every admitted executable to a
    fixed regular file outside every configured service-writable root. Script
    pins also name their pinned interpreter explicitly, so their shebang cannot
    reintroduce PATH resolution below the authorized argv.

    Returns the pinned argv and ``""``, or ``None`` and why pinning refused.
    ``_maintenance_pinned_execution_argv`` is the argv-only view of this.
    """
    if not argv:
        return None, "the command is empty."
    command = argv[0]
    expected = _maintenance_resolved_pin(command)
    if expected is None:
        return None, (
            f"the executable {command!r} has no trusted pinned path for service "
            "execution. A pin must resolve to a regular file that is not a "
            "symlink and lies outside every service-writable root, so the "
            "program cannot be swapped between authorization and execution."
        )
    interpreter_command = _MAINTENANCE_PINNED_SCRIPT_INTERPRETERS.get(command)
    if interpreter_command is not None:
        interpreter = _maintenance_resolved_pin(interpreter_command)
        if interpreter is None:
            return None, (
                f"the pinned interpreter {interpreter_command!r} required to run "
                f"the script {command!r} could not be resolved to a trusted path."
            )
        return [str(interpreter), str(expected), *argv[1:]], ""
    return [str(expected), *argv[1:]], ""


def _maintenance_pinned_execution_argv(argv: list[str]) -> list[str] | None:
    """Argv-only view of :func:`_maintenance_pinned_execution_argv_with_reason`."""
    return _maintenance_pinned_execution_argv_with_reason(argv)[0]


def _target_matches_maintenance_shell_command(argv: list[str]) -> bool:
    """Validate read/inspection commands used by scheduled maintenance."""
    if _target_matches_read_only_shell_command(argv):
        return True
    if not argv:
        return False

    if argv[0] == "git":
        return _maintenance_git_execution_argv(argv) is not None

    # File inspection. This widens which commands may READ, not what is
    # readable: ``grep`` and ``rg`` are already admitted and reach the same
    # bytes. Per-command option allowlists, so nothing here can mutate --
    # note ``date`` admits no ``-s``/``--set``, which would set the clock.
    if argv[0] == "cat":
        return _arguments_match_allowlist(
            argv[1:],
            exact_options=frozenset({
                "-n", "--number", "-s", "--squeeze-blank", "-E", "--show-ends",
            }),
        )
    if argv[0] in {"head", "tail"}:
        return _arguments_match_allowlist(
            argv[1:],
            exact_options=frozenset({"-q", "-v", "--quiet", "--verbose"}),
            option_prefixes=("-n", "-c", "--lines=", "--bytes="),
        )
    if argv[0] == "stat":
        return _arguments_match_allowlist(
            argv[1:],
            exact_options=frozenset({"-t", "--terse", "-L", "--dereference"}),
            option_prefixes=("-c", "--format=", "--printf="),
        )
    if argv[0] == "date":
        return _arguments_match_allowlist(
            argv[1:],
            exact_options=frozenset({
                "-u", "--utc", "-R", "--rfc-email", "-I", "--iso-8601",
            }),
            option_prefixes=("-d", "--date=", "--iso-8601=", "--rfc-3339="),
        )

    if argv[0] == "gh" and len(argv) >= 3 and argv[1] in {"pr", "issue"}:
        resource = argv[1]
        subcommand = argv[2]
        options = {
            ("pr", "list"): frozenset({
                "--app", "--assignee", "--author", "--base", "--draft", "--head",
                "--json", "--label", "--limit", "--repo", "--search",
                "--state", "--template",
            }),
            ("pr", "view"): frozenset({
                "--comments", "--json", "--repo", "--template",
            }),
            ("issue", "list"): frozenset({
                "--app", "--assignee", "--author", "--json", "--label",
                "--limit", "--mention", "--milestone", "--repo", "--search",
                "--state", "--template",
            }),
            ("issue", "view"): frozenset({
                "--comments", "--json", "--repo", "--template",
            }),
        }.get((resource, subcommand))
        return options is not None and _arguments_match_allowlist(
            argv[3:], exact_options=options,
        )

    return False


# Appended to refusals a rewrite cannot fix, so the caller is told what shape
# to use instead of retrying the same one. The three named substitutions are the
# ones observed in production: ``cd X && cmd``, an inline multi-line ``--body``,
# and a heredoc.
_SHELL_PROFILE_SINGLE_ARGV_HINT = (
    "A trusted-service profile execs one argv directly with shell=False, so "
    "shell syntax is never admitted and no quoting will change that. Issue one "
    "command per call; select the working directory with the command's own "
    "option (for example 'git -C <dir>') rather than 'cd <dir> && ...'; and pass "
    "multi-line text through a file option (for example 'gh pr review "
    "--body-file <path beneath the agent scratch root>') rather than an inline "
    "multi-line value or a heredoc."
)


_PROJECT_TEST_CONFIG_ENV = "MIMIR_PROJECT_TEST_COMMAND"
_PROJECT_TEST_MAX_SELECTORS = 32
_PROJECT_TEST_MAX_SELECTOR_LENGTH = 256
_PROJECT_TEST_MAX_SELECTOR_BYTES = 4096
_PROJECT_TEST_MAX_COMMAND_ARGS = 16
_PROJECT_TEST_MAX_COMMAND_ARG_LENGTH = 512
_PROJECT_TEST_SELECTOR_PATTERN = re.compile(r"[A-Za-z0-9._/,:+=-]+", re.ASCII)
_PROJECT_TEST_INTERPRETER_PATTERN = re.compile(
    r"(?:python(?:\d+(?:\.\d+)*)?|pypy\d*|node|ruby|perl|php|lua|luajit|"
    r"[bdkz]?sh|fish|rscript|java|env)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _ProjectTestCommand:
    argv: tuple[str, ...]
    cwd: Path


def _configured_project_test_command() -> tuple[_ProjectTestCommand | None, str]:
    """Load the operator-owned fixed test argv and project root from process config."""
    raw = os.environ.get(_PROJECT_TEST_CONFIG_ENV, "").strip()
    if not raw:
        return None, "project_test_not_configured"
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None, "project_test_config_invalid_json"
    if not isinstance(value, dict) or set(value) != {"argv", "cwd"}:
        return None, "project_test_config_invalid_shape"
    argv = value.get("argv")
    cwd_text = value.get("cwd")
    if (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= _PROJECT_TEST_MAX_COMMAND_ARGS
        or any(
            not isinstance(argument, str)
            or not argument
            or len(argument) > _PROJECT_TEST_MAX_COMMAND_ARG_LENGTH
            or "\x00" in argument
            or "\n" in argument
            or "\r" in argument
            for argument in argv
        )
        or not isinstance(cwd_text, str)
        or not cwd_text
    ):
        return None, "project_test_config_invalid_shape"
    executable = Path(argv[0])
    if not executable.is_absolute():
        return None, "project_test_config_executable_not_absolute"
    if any(
        _PROJECT_TEST_INTERPRETER_PATTERN.fullmatch(Path(argument).name)
        for argument in argv
    ):
        return None, "project_test_config_interpreter_refused"
    try:
        resolved_executable = executable.resolve(strict=True)
        cwd = Path(cwd_text).resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "project_test_config_path_unavailable"
    if (
        resolved_executable != executable
        or not resolved_executable.is_file()
        or not os.access(resolved_executable, os.X_OK)
        or _maintenance_pin_is_service_writable(resolved_executable)
    ):
        return None, "project_test_config_executable_untrusted"
    if not cwd.is_dir():
        return None, "project_test_config_path_unavailable"
    try:
        roots = tuple(root.resolve(strict=True) for root in _configured_repo_roots())
    except (OSError, RuntimeError):
        return None, "project_test_config_root_unauthorized"
    if not any(cwd == root or cwd.is_relative_to(root) for root in roots):
        return None, "project_test_config_root_unauthorized"
    return _ProjectTestCommand((str(resolved_executable), *argv[1:]), cwd), ""


def _project_test_execution_argv(
    argv: list[str],
) -> tuple[list[str] | None, str | None, bool]:
    """Return a configured test argv, refusal name, and whether its prefix matched."""
    configured, config_reason = _configured_project_test_command()
    if configured is None:
        return None, config_reason, False
    prefix = list(configured.argv)
    requested_prefix = [str(Path(argv[0]).resolve(strict=False)), *argv[1:len(prefix)]]
    if len(argv) < len(prefix) or requested_prefix != prefix:
        return None, None, False
    selectors = argv[len(prefix):]
    if len(selectors) > _PROJECT_TEST_MAX_SELECTORS:
        return None, "project_test_selector_count_exceeded", True
    if sum(len(selector.encode("utf-8")) for selector in selectors) > _PROJECT_TEST_MAX_SELECTOR_BYTES:
        return None, "project_test_selectors_too_large", True
    for selector in selectors:
        if len(selector) > _PROJECT_TEST_MAX_SELECTOR_LENGTH:
            return None, "project_test_selector_too_long", True
        path_text = selector.split("::", 1)[0]
        if ".." in Path(path_text).parts:
            return None, "project_test_selector_traversal", True
        if (
            not selector.isascii()
            or not _PROJECT_TEST_SELECTOR_PATTERN.fullmatch(selector)
            or selector.startswith(("-", "/", "@"))
            or selector in {".", ".."}
            or (selector.startswith(".") and not selector.startswith("./"))
        ):
            return None, "project_test_selector_invalid", True
    return [*prefix, *selectors], "", True


def configured_project_test_cwd(argv: list[str]) -> str | None:
    """Return the operator-selected cwd only for an admitted configured test argv."""
    execution_argv, _reason, matched = _project_test_execution_argv(argv)
    if not matched or execution_argv is None:
        return None
    configured, _reason = _configured_project_test_command()
    return str(configured.cwd) if configured is not None else None


# Display-only vocabulary for refusal messages. A token is echoed back to the
# caller ONLY if it is literally a member of one of these sets; anything else
# becomes a placeholder. That makes "no argument value is ever disclosed" a
# property of construction rather than of a pattern.
#
# Pattern matching cannot do this job. An attached short-option value
# (``-HAuthorization:Bearer-...``) has the same shape as a legitimate long
# option, and a secret can be bare alphanumerics or a plain-looking path
# (``private/path/...``), indistinguishable from a subcommand or an API resource.
#
# Absence from these sets can only make a message *less specific* — it can never
# change an authorization decision, admit a command, or deny one. That asymmetry
# is what makes a hand-maintained display list safe: its worst failure is a
# vaguer sentence. The executables are derived from the pin map so they cannot
# drift from the commands that can actually run.
_SERVICE_SHELL_DISPLAY_COMMANDS = frozenset(_MAINTENANCE_PINNED_EXECUTABLE_DEFAULTS)
_SERVICE_SHELL_DISPLAY_SUBCOMMANDS = frozenset({
    "add", "api", "branch", "checkout", "checks", "close", "comment", "commit",
    "block", "blocked", "close-all", "create", "delete", "describe", "diff",
    "fetch", "init", "issue", "label", "list", "lock", "locks", "log",
    "ls-files", "merge-base", "pr", "pull", "push", "quick", "ready", "relate",
    "remote", "reopen", "rev-list", "rev-parse", "review", "run", "search", "session", "show",
    "status", "subissue", "sync", "test", "unblock", "unlabel", "unrelate",
    "update", "view",
})
_SERVICE_SHELL_DISPLAY_OPTIONS = frozenset({
    "-C", "-a", "-c", "-l", "-m", "-n", "-p", "-q",
    "--all", "--app", "--approve", "--assignee", "--author", "--base", "--body",
    "--body-file", "--branch", "--comment", "--comments", "--draft", "--head",
    "--description", "--json", "--jq", "--kind", "--label", "--limit",
    "--mention", "--milestone", "--no-changelog", "--no-pager", "--oneline",
    "--priority", "--quiet", "--repo", "--request-changes",
    "--search", "--short", "--state", "--status", "--template",
})

_SERVICE_SHELL_LOG_MAX_ARGUMENTS = 32
_SERVICE_SHELL_LOG_MAX_ARGUMENT_LENGTH = 256
_SERVICE_SHELL_SECRET_OPTIONS = frozenset({
    "--api-key", "--apikey", "--auth", "--authorization", "--client-secret",
    "--credential", "--header", "--password", "--secret", "--token", "-c",
    "-H", "-t",
})


class ServiceShellBindingRule(StrEnum):
    """Stable aggregation keys for trusted-service shell binding refusals."""

    SHELL_CONTROL_CHARACTERS = "shell_control_characters"
    ARGV_UNBALANCED_QUOTING = "argv_unbalanced_quoting"
    ARGV_EMPTY = "argv_empty"
    SHELL_HOME_EXPANSION = "shell_home_expansion"
    PROJECT_TEST_POLICY = "project_test_policy"
    PROFILE_ALLOWLIST = "profile_allowlist"
    EXECUTABLE_PIN = "executable_pin"
    REPOSITORY_REVIEW_STATE = "repository_review_state"
    REVIEW_BODY_CAPTURE = "review_body_capture"
    UNKNOWN_PROFILE = "unknown_profile"
    DECLARED_COMMAND_MISMATCH = "declared_command_mismatch"


def service_shell_argv_for_log(target: str) -> tuple[list[str], bool]:
    """Return a bounded argv with credential-bearing argument values redacted.

    Redaction covers shared token shapes plus values supplied to credential/header
    options. Truncation is represented both in the argv and by the returned flag.
    """
    from .redaction import redact_text

    try:
        raw_argv = shlex.split(target)
    except ValueError:
        raw_argv = ["<unparseable argv>"]

    redacted: list[str] = []
    redact_next = False
    truncated = len(raw_argv) > _SERVICE_SHELL_LOG_MAX_ARGUMENTS
    for argument in raw_argv[:_SERVICE_SHELL_LOG_MAX_ARGUMENTS]:
        if redact_next:
            rendered = "[REDACTED]"
            redact_next = False
        else:
            option, separator, _value = argument.partition("=")
            if separator and option in _SERVICE_SHELL_SECRET_OPTIONS:
                rendered = f"{option}=[REDACTED]"
            elif argument.startswith("-H") and argument != "-H":
                rendered = "-H[REDACTED]"
            elif argument.startswith("-t") and argument != "-t":
                rendered = "-t[REDACTED]"
            else:
                rendered = redact_text(argument)
            redact_next = argument in _SERVICE_SHELL_SECRET_OPTIONS
        if len(rendered) > _SERVICE_SHELL_LOG_MAX_ARGUMENT_LENGTH:
            rendered = rendered[:_SERVICE_SHELL_LOG_MAX_ARGUMENT_LENGTH] + "[TRUNCATED]"
            truncated = True
        redacted.append(rendered)
    if len(raw_argv) > _SERVICE_SHELL_LOG_MAX_ARGUMENTS:
        redacted.append("[TRUNCATED]")
    return redacted, truncated


def _service_shell_command_shape(argv: list[str]) -> str:
    """Name the refused command surface without echoing any argument value.

    The executable plus up to two following non-option tokens, each passed
    through the display vocabulary — enough to identify which command and
    subcommand were refused, while a resource path or any other value renders as
    ``<value>``.
    """
    shape = [
        argv[0] if argv[0] in _SERVICE_SHELL_DISPLAY_COMMANDS else "<command>"
    ]
    for token in argv[1:]:
        if token.startswith("-"):
            break
        shape.append(
            token if token in _SERVICE_SHELL_DISPLAY_SUBCOMMANDS else "<value>"
        )
        if len(shape) >= 3:
            break
    return " ".join(shape)


def _service_shell_coding_enabled() -> bool:
    """Whether this deployment exposes coding tools, using config's bool syntax."""
    raw = os.environ.get("MIMIR_CODING_ENABLED")
    # Keep this truthy set aligned with config._env_bool without importing config
    # here: access_control is imported by config, so that would create a cycle.
    return bool(raw and raw.strip().lower() in {"1", "true", "yes", "on", "y"})


def _service_shell_typed_tool_guidance(
    argv: list[str], destination: str,
) -> str:
    """Name bounded tools for observed commands that must stay outside the shell."""
    # Provenance: poller:github-activity refusals recorded in events.jsonl after
    # argv logging landed were `npm run` (repository script), `python -c ...`
    # (arbitrary Python), and `python - <<PY ...` (attachment/HTML parsing).
    # All remain correctly refused: the first must use repo_test when available;
    # the latter two have no general bounded typed equivalent. `npm ci` and
    # `npm install` are inferred dependency-install shapes and remain refused
    # because this profile exposes no typed dependency-install capability.
    if not argv:
        return ""
    executable = Path(argv[0]).name
    operation = argv[1:2]
    if destination == "upgrade_workspace":
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
            return (
                " Python execution is arbitrary code execution and remains denied. "
                "Use read_file, glob, or grep for bounded inspection beneath the "
                "proposal workspace; do not retry with python -c or a script."
            )
        if executable in {"cat", "grep", "rg", "ls"}:
            return (
                " Use the typed read_file, grep, glob, or ls tool for bounded "
                "inspection beneath the proposal workspace."
            )
    if destination in {"maintenance", "upgrade_workspace"} and executable == "git":
        arguments = argv[1:]
        if destination == "upgrade_workspace":
            arguments = _git_arguments_without_restrictive_global_options(arguments)
        if arguments[:1] != ["-C"] and _maintenance_git_subcommand_allowed(arguments):
            example = {
                "branch": "git -C <dir> branch --show-current",
                "diff": "git -C <dir> diff --stat",
                "log": "git -C <dir> log --oneline",
                "show": "git -C <dir> show --stat",
                "status": "git -C <dir> status --short",
            }[arguments[0]]
            return (
                " This Git command is otherwise an admitted inspection shape, but "
                "the repository must be named in argv with -C; the process working "
                f"directory is not an authorized target. Use `{example}`."
            )
    if destination == "maintenance" and executable == "mimir" and argv[1:3] == [
        "wiki", "backlinks",
    ]:
        return (
            " `mimir wiki backlinks` remains denied in this shell profile. Wiki "
            "backlinks are regenerated by the bounded post-turn hook after wiki "
            "content changes; inspect its generated files with read_file instead "
            "of retrying this command through shell_exec."
        )
    if destination not in {"repo_review", "maintenance"}:
        return ""
    if destination == "repo_review" and executable == "npm" and operation in (["run"], ["test"]):
        if _service_shell_coding_enabled():
            return (
                " Repository scripts must run through the typed repo_test tool, "
                "which uses the deployment-configured test command in the bound PR "
                "checkout; shell_exec does not admit npm run or npm test."
            )
        return (
            " Repository scripts remain denied, and this deployment does not expose "
            "repo_test. Use whatever bounded verification capability the deployment "
            "provides, and state plainly when the tests could not be run."
        )
    if destination == "repo_review" and executable == "npm" and operation in (["ci"], ["install"]):
        return (
            " npm dependency installation remains denied and has no typed equivalent "
            "in this profile; do not retry it through shell_exec."
        )
    if (
        len(argv) >= 2
        and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable)
        and argv[1] in {"-c", "-"}
    ):
        mode = "python -c" if argv[1] == "-c" else "Python stdin/heredoc"
        guidance = (
            f" Inline {mode} is arbitrary code execution and remains denied. There "
            "is no general typed equivalent for arbitrary Python or attachment/HTML "
            "parsing in this profile. Use read_file or grep only when bounded text "
            "inspection suffices; otherwise report that the workload cannot be "
            "performed rather than retrying it through shell_exec."
        )
        if destination == "repo_review" and _service_shell_coding_enabled():
            guidance += " For repository tests only, use the typed repo_test tool."
        return guidance
    return ""


def _service_shell_not_admitted_reason(argv: list[str], destination: str) -> str:
    """Explain that a well-formed command is outside the profile's allowlist."""
    supplied = [token for token in argv[1:] if token.startswith("-")]
    named = sorted({
        token for token in supplied if token in _SERVICE_SHELL_DISPLAY_OPTIONS
    })
    # One placeholder stands in for every option that is not a known spelling,
    # including any that carries its value attached to it.
    if len(named) != len(set(supplied)):
        named.append("<option>")
    option_text = f" Options sent: {', '.join(named)}." if named else ""
    if argv and argv[0] in _CHAINLINK_EXECUTABLES:
        operation = argv[1:3]
        if argv[1:2] == ["locks"]:
            boundary = (
                " Lock lifecycle commands are reserved to Worklink because a shell-issued "
                "claim, steal, or release can orphan an in-flight build; use the coordinated "
                "Worklink path instead."
            )
        elif argv[1:2] == ["init"] or operation in (["issue", "delete"], ["issue", "close-all"]):
            boundary = (
                " Tracker initialization, deletion, and bulk close are deliberately denied; "
                "they can shadow the tracker or cause irreversible or bulk work loss."
            )
        else:
            boundary = ""
        admitted = (
            " Admitted Chainlink queries are issue show/list/search/ready/blocked/"
            "related/cascade/next/tree and session status; "
            "admitted mutations are issue create/update/comment/label/unlabel/block/unblock/"
            "relate/unrelate/close/reopen/subissue/quick, using only each command's "
            "documented bounded options (-q/--quiet and --json included)."
        )
    elif argv[:1] == ["git"] and destination == "repo_review":
        boundary = (
            " Git forms that can execute, write output, contact a remote, or enable "
            "textconv/external helpers are deliberately denied."
        )
        admitted = (
            " Admitted inspection alternatives include git status --porcelain; "
            "git log [options] [-- paths]; git diff [revisions] [-- paths]; "
            "git show [revision]; git grep [-n] pattern [-- paths]; git blame "
            "[-L<range>] [revision] [--] path; git merge-base [--is-ancestor] "
            "<revision> <revision>; git rev-list --count <revision>; git rev-parse, "
            "git remote -v, git branch --list, and git worktree list --porcelain."
        )
    else:
        boundary = admitted = ""
    guidance = _service_shell_typed_tool_guidance(argv, destination)
    return (
        f"the {destination!r} trusted-service shell profile does not admit "
        f"{_service_shell_command_shape(argv)!r}.{option_text} This profile "
        "admits a fixed set of commands, subcommands and options; anything "
        "outside it is refused for this principal regardless of how it is "
        f"written.{boundary}{admitted}{guidance}"
    )


def parse_service_shell_argv_with_diagnostics(
    target: str, destination: str, *, review_state: Any = None,
    declared: tuple["DeclaredShellCommand", ...] = (),
) -> tuple[list[str] | None, str, ServiceShellBindingRule | None]:
    """Return the admitted argv, refusal reason, and stable rejecting rule.

    The returned argv is both the authorization artifact and the execution
    artifact. Callers must exec it directly with ``shell=False``; handing the
    original string to a shell would reintroduce an expansion layer the profile
    did not validate.

    On refusal the second element says why. It is produced at the same branch
    that refuses, so an explanation can never disagree with the decision it
    describes — which is why this is one function with three outputs rather than a
    separate explainer that could drift out of step with the rule.

    Reasons are deliberately *structural*: metacharacters, the executable, the
    subcommand and option NAMES. They never quote option values, because a
    service command can carry a credential (``git -c http.extraheader=...``) and
    this text is returned to the model and recorded in the turn transcript.
    """
    found = _unquoted_shell_control_characters(target)
    if found:
        rendered = ", ".join(repr(character) for character in found)
        try:
            refused_argv = shlex.split(target)
        except ValueError:
            refused_argv = []
        guidance = _service_shell_typed_tool_guidance(refused_argv, destination)
        return None, (
            f"the command contains shell metacharacters ({rendered}), which the "
            f"{destination!r} trusted-service shell profile never admits. "
            + _SHELL_PROFILE_SINGLE_ARGV_HINT
            + guidance
        ), ServiceShellBindingRule.SHELL_CONTROL_CHARACTERS
    try:
        argv = shlex.split(target)
    except ValueError:
        return None, (
            "the command could not be split into an argv because its quoting is "
            "unbalanced. Close every quote, or pass the value through a file "
            "option instead of inline."
        ), ServiceShellBindingRule.ARGV_UNBALANCED_QUOTING
    if not argv:
        return None, "the command is empty.", ServiceShellBindingRule.ARGV_EMPTY
    # A leading tilde is shell home expansion; an embedded tilde such as
    # ``HEAD~1`` is a normal Git revision expression and is passed literally.
    if any(argument.startswith("~") for argument in argv):
        return None, (
            "an argument begins with '~', which requires the shell home "
            "expansion this profile does not perform. Use an absolute path."
        ), ServiceShellBindingRule.SHELL_HOME_EXPANSION

    if argv[0] == "/usr/local/bin/chainlink":
        argv[0] = "chainlink"

    test_argv, test_reason, test_matched = _project_test_execution_argv(argv)
    if test_matched:
        if test_argv is None:
            return (
                None,
                f"configured project test refused: {test_reason}",
                ServiceShellBindingRule.PROJECT_TEST_POLICY,
            )
        return test_argv, "", None

    # Consulted BEFORE the profile dispatch, not after it. Several branches
    # (repo_review, and the per-profile ``git`` handlers) return on mismatch, so
    # a check placed after them was reachable for some profiles and not others --
    # a GitHub poller could never have used a declared CLI. Union semantics are
    # what "additive" was meant to say; order here only decides which gate speaks
    # first, and an explicit per-job grant is the more specific statement.
    declared_argv = _declared_command_execution_argv(argv, declared)
    if declared_argv is not None:
        return declared_argv, "", None

    allowed = False
    if destination == "scheduler_read_only":
        # Unlike maintenance and repo_review, this profile is not bound to one
        # server-selected repository: scheduler/custom jobs may select any
        # authorized cwd. It therefore pins Git and requires effective safety
        # options in the input, while the root-bound profiles additionally add
        # -C, config, and discovered-filter overrides in their binders.
        allowed = _target_matches_read_only_shell_command(argv)
    elif destination == "repo_review":
        if not _target_matches_repo_review_shell_command(argv, review_state):
            return (
                None,
                _service_shell_not_admitted_reason(argv, destination),
                ServiceShellBindingRule.PROFILE_ALLOWLIST,
            )
        if argv[0] == "git" and argv[1:2] != ["fetch"]:
            git_argv = _repo_review_git_execution_argv(argv, review_state)
            if git_argv is not None:
                return git_argv, "", None
            return (
                None,
                _service_shell_not_admitted_reason(argv, destination),
                ServiceShellBindingRule.PROFILE_ALLOWLIST,
            )
        pinned, reason = _maintenance_pinned_execution_argv_with_reason(argv)
        if pinned is None:
            return None, reason, ServiceShellBindingRule.EXECUTABLE_PIN
        # Capture any review body HERE, so the returned artifact carries no
        # pathname for ``gh`` to look up again. See
        # ``_capture_review_body_beneath_scratch``: validating a path and
        # then passing that path on is a check/use race.
        captured = _repo_review_argv_with_captured_body(pinned)
        if captured is None:
            return None, (
                "the '--body-file' path could not be captured. It must resolve "
                "beneath the agent scratch root, be a regular file reached "
                "without traversing a symlink, and be at most "
                f"{_REVIEW_BODY_MAX_BYTES} bytes. The body is read once during "
                "authorization so the path is never re-opened at execution."
            ), ServiceShellBindingRule.REVIEW_BODY_CAPTURE
        return captured, "", None
    elif destination == "maintenance":
        if argv[0] == "git":
            git_argv = _maintenance_git_execution_argv(argv)
            if git_argv is None:
                return (
                    None,
                    _service_shell_not_admitted_reason(argv, destination),
                    ServiceShellBindingRule.PROFILE_ALLOWLIST,
                )
            return git_argv, "", None
        allowed = _target_matches_maintenance_shell_command(argv)
    elif destination == "upgrade_workspace":
        if argv[0] == "git":
            git_argv = _upgrade_workspace_git_execution_argv(argv)
            if git_argv is None:
                return (
                    None,
                    _service_shell_not_admitted_reason(argv, destination),
                    ServiceShellBindingRule.PROFILE_ALLOWLIST,
                )
            return git_argv, "", None
        allowed = _target_matches_read_only_shell_command(argv) or (
            argv[0] == "uv"
            and argv[1:] in (["lock"], ["sync"])
        )
    else:
        return None, (
            f"there is no trusted-service shell profile named {destination!r}, "
            "so no command can be admitted for this principal."
        ), ServiceShellBindingRule.UNKNOWN_PROFILE
    if not allowed:
        return (
            None,
            _service_shell_not_admitted_reason(argv, destination),
            # Distinguish "this job declared commands and none matched" from
            # "the profile refused it" so a denial event says which gate spoke.
            # ``destination`` keeps meaning the profile, so existing shadow-authz
            # classification is unaffected.
            ServiceShellBindingRule.DECLARED_COMMAND_MISMATCH if declared
            else ServiceShellBindingRule.PROFILE_ALLOWLIST,
        )
    pinned, reason = _maintenance_pinned_execution_argv_with_reason(argv)
    return (
        (pinned, "", None)
        if pinned is not None
        else (None, reason, ServiceShellBindingRule.EXECUTABLE_PIN)
    )


def parse_service_shell_argv_with_reason(
    target: str, destination: str, *, review_state: Any = None,
    declared: tuple["DeclaredShellCommand", ...] = (),
) -> tuple[list[str] | None, str]:
    """Compatibility view returning only the admitted argv and refusal prose."""
    argv, reason, _rule = parse_service_shell_argv_with_diagnostics(
        target, destination, review_state=review_state, declared=declared,
    )
    return argv, reason


def parse_service_shell_argv(
    target: str, destination: str, *, review_state: Any = None,
    declared: tuple["DeclaredShellCommand", ...] = (),
) -> list[str] | None:
    """Argv-only view of :func:`parse_service_shell_argv_with_reason`."""
    return parse_service_shell_argv_with_reason(
        target, destination, review_state=review_state, declared=declared,
    )[0]


def _service_shell_refusal_detail(
    target: object, policy: "ServiceSinkPolicy | None", review_state: Any = None,
) -> str | None:
    """Prose for a shell-profile refusal; ``None`` for every other adapter.

    Recomputes through the same parser the sink adapter just consulted, so the
    explanation is by construction the one that produced the refusal rather than
    a second reading of the rule.
    """
    if (
        policy is None
        or policy.adapter != "shell_profile"
        or not isinstance(target, str)
    ):
        return None
    argv, reason = parse_service_shell_argv_with_reason(
        target, policy.destination, review_state=review_state,
    )
    return None if argv is not None else reason


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


def _target_within_trigger_service_write_roots(target: str, destination: str) -> bool:
    """Confine dynamic trigger writes to frozen roots and safe home data paths."""
    from ._paths import PathOutsideHomeError, resolve_within_roots

    home = os.environ.get("MIMIR_HOME", "").strip()
    candidate = Path(target)
    if not home:
        return False
    home_root = Path(home).resolve()
    if not candidate.is_absolute():
        candidate = home_root / candidate
    try:
        raw = json.loads(destination)
        if not isinstance(raw, list) or not raw or not all(isinstance(p, str) for p in raw):
            return False
        roots = [Path(path).resolve() for path in raw]
        turn_scratch = current_turn_scratch_root()
        if turn_scratch is not None:
            roots.append(turn_scratch)
        scratch_root = (home_root / "scratch").resolve()
        if (candidate == scratch_root or candidate.is_relative_to(scratch_root)) and (
            turn_scratch is None
            or not (candidate == turn_scratch or candidate.is_relative_to(turn_scratch))
        ):
            return False
        memory_root = (home_root / "memory").resolve()
        lexical_relatives = tuple(
            (root, candidate.relative_to(root))
            for root in roots
            if candidate == root or candidate.is_relative_to(root)
        )
        if any(
            WriteResourceAdapter._is_protected_path(relative)
            or _is_static_service_protected_write_path(
                relative,
                under_memory_root=root == memory_root,
            )
            for root, relative in lexical_relatives
        ):
            return False
        resolved = resolve_within_roots(roots, str(candidate))
        resolved_relatives = tuple(
            (root, resolved.relative_to(root))
            for root in roots
            if resolved == root or resolved.is_relative_to(root)
        )
    except (
        json.JSONDecodeError, OSError, PathOutsideHomeError, RuntimeError, ValueError,
    ):
        return False
    return bool(resolved_relatives) and not any(
        WriteResourceAdapter._is_protected_path(relative)
        or _is_static_service_protected_write_path(
            relative,
            under_memory_root=root == memory_root,
        )
        for root, relative in resolved_relatives
    )


def _target_within_active_pr_checkout_lease(target: str, review_state: Any) -> bool:
    """Admit one file target only beneath this turn's exact active lease."""
    lease = getattr(review_state, "checkout_lease", None)
    scope = getattr(review_state, "action_scope", None)
    if lease is None or not getattr(lease, "is_active", False):
        return False
    if (
        getattr(lease, "scope_id", None) != getattr(scope, "scope_id", None)
        or getattr(lease, "owner", None) != getattr(scope, "principal", None)
    ):
        return False
    candidate = Path(target)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return False
    root = Path(lease.path)
    try:
        lexical = candidate.relative_to(root)
        lease_root = Path(lease.lease_root).resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        if resolved_root.parent != lease_root or resolved_root == lease_root:
            return False
        resolved = candidate.resolve(strict=False)
        relative = resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return not (
        WriteResourceAdapter._is_protected_path(lexical)
        or WriteResourceAdapter._is_protected_path(relative)
        or _is_static_service_protected_write_path(relative)
    )


def resolve_repository_review_state(
    auth_context: Any,
    *,
    command: object = None,
    cwd: object = None,
    path: object = None,
) -> tuple[Any, str | None]:
    """Resolve one request's PR state without relying on a batched singleton."""
    from .models import RepoPRScopeRegistry

    registry = getattr(auth_context, "repo_pr_scope_registry", None)
    if not isinstance(registry, RepoPRScopeRegistry):
        return getattr(auth_context, "repo_review_state", None), None

    if path is not None:
        state = registry.resolve_checkout_path(path)
        return state, None if state is not None else "no matching checkout lease was found for the requested path"

    argv: list[str] = []
    if isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = []
    if argv[:2] == ["gh", "pr"] and len(argv) >= 4:
        try:
            pull_request = int(argv[3])
        except ValueError:
            pull_request = None
        repository = _option_value(argv[4:], "--repo") or _option_value(argv[4:], "-R")
        state = registry.resolve(repository, pull_request)
        if state is not None:
            return state, None
    if argv[:2] == ["git", "-C"] and len(argv) >= 3:
        state = registry.resolve_checkout_path(argv[2])
        return state, None if state is not None else "no matching checkout lease was found for the repository command"
    if cwd is not None:
        state = registry.resolve_checkout_path(cwd)
        return state, None if state is not None else "no matching checkout lease was found for the repository command"
    if len(registry.review_states) == 1:
        return registry.review_states[0], None
    return None, "no matching checkout lease was found for the repository command"


def _synthesis_channel_target_matches_session(
    target: str, channel_id: str | None,
) -> bool:
    """Bind a channel-memory target to the synthesis turn's session channel."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home or not channel_id:
        return False
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = Path(home).resolve() / candidate
    try:
        resolved = candidate.resolve()
        relative = resolved.relative_to(
            (Path(home).resolve() / "memory" / "channels").resolve()
        )
    except (OSError, RuntimeError):
        # Resolution failure cannot prove channel ownership; fail closed.
        return False
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] == channel_id


def _synthesis_target_matches_session(target: str, channel_id: str | None) -> bool:
    """Prevent one session boundary from mutating another channel's memory."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home or not channel_id:
        return False
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = Path(home).resolve() / candidate
    try:
        candidate.resolve().relative_to(
            (Path(home).resolve() / "memory" / "channels").resolve()
        )
    except (OSError, RuntimeError):
        return False
    except ValueError:
        # The prompt also authorizes shared non-channel memory and state paths.
        return True
    return _synthesis_channel_target_matches_session(target, channel_id)


def resolve_trigger_service_write_target(target: str, destination: str) -> Path:
    """Resolve a trigger-service write exactly as its sink adapter checks it."""
    from ._paths import PathOutsideHomeError, resolve_within_roots

    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        raise PathOutsideHomeError("MIMIR_HOME is not configured")
    raw = json.loads(destination)
    if not isinstance(raw, list) or not raw or not all(isinstance(p, str) for p in raw):
        raise PathOutsideHomeError("trigger-service write roots are invalid")
    roots = [Path(path).resolve() for path in raw]
    turn_scratch = current_turn_scratch_root()
    if turn_scratch is not None:
        roots.append(turn_scratch)
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = Path(home).resolve() / candidate
    scratch_root = (Path(home).resolve() / "scratch").resolve()
    if (candidate == scratch_root or candidate.is_relative_to(scratch_root)) and (
        turn_scratch is None
        or not (candidate == turn_scratch or candidate.is_relative_to(turn_scratch))
    ):
        raise PathOutsideHomeError("scratch target is outside the current turn workspace")
    return resolve_within_roots(roots, str(candidate))


_TRIGGER_SERVICE_PROTECTED_READ_NAMES = frozenset({
    ".env", ".git", ".mimir", ".venv", "config", "credentials",
    "identities", "prompts", "secret", "secrets",
})


def _is_trigger_service_protected_read_path(path: Path) -> bool:
    return any(part.lower() in _TRIGGER_SERVICE_PROTECTED_READ_NAMES for part in path.parts)


def _is_service_protected_read_path(
    service: ServicePrincipal | None, root: Path, relative: Path,
) -> bool:
    """Apply protected names, except shipped prompt files in upgrade proposals."""
    protected = {
        part.lower() for part in relative.parts
        if part.lower() in _TRIGGER_SERVICE_PROTECTED_READ_NAMES
    }
    return bool(protected) and not (
        service is not None
        and getattr(service, "canonical", None) == "system"
        and getattr(service, "trigger", None) == "upgrade"
        and root == _upgrade_proposals_root()
        and protected == {"prompts"}
    )


def _trigger_service_read_target_is_allowed(
    service: ServicePrincipal,
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    auth_context: "AuthContext | None" = None,
) -> bool:
    """Authorize a service read against frozen roots and verified ownership."""
    from .read_policy import (
        _has_protected_read_name,
        file_contains_secret,
        is_memory_read_path,
        is_memory_read_path_allowed,
        is_operator_secret_read_path,
    )

    args = arguments if isinstance(arguments, dict) else {}
    raw = (
        args.get("file_path") or args.get("path")
        if tool_name in {"read_file", "aread"}
        else args.get("path")
    )
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return False
    candidate = Path(raw)
    roots = service_filesystem_read_roots(service)
    home = os.environ.get("MIMIR_HOME", "").strip()
    if home:
        home_root = Path(home).resolve()
        memory_candidate = candidate
        if not candidate.is_absolute() or not (
            candidate == home_root or candidate.is_relative_to(home_root)
        ):
            memory_candidate = home_root / raw.lstrip("/")
        try:
            resolved_memory_candidate = memory_candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            resolved_memory_candidate = None
        if (
            resolved_memory_candidate is not None
            and is_memory_read_path(resolved_memory_candidate)
        ):
            if not is_memory_read_path_allowed(
                memory_candidate, auth_context,
            ):
                return False
            if _is_trigger_service_protected_read_path(
                resolved_memory_candidate.relative_to(home_root / "memory")
            ) or is_operator_secret_read_path(resolved_memory_candidate):
                return False
            return not (
                tool_name in {"read_file", "aread"}
                and (
                    not resolved_memory_candidate.is_file()
                    or file_contains_secret(resolved_memory_candidate)
                )
            )
    if home:
        home_candidate = Path(home) / raw.lstrip("/")
        if any(
            home_candidate == root or home_candidate.is_relative_to(root)
            for root in roots
        ):
            candidate = home_candidate
    if not candidate.is_absolute():
        return False
    try:
        lexical_root, lexical_relative = max(
            (
                (root, candidate.relative_to(root))
                for root in roots
                if candidate == root or candidate.is_relative_to(root)
            ),
            key=lambda item: len(item[0].parts),
        )
        artifact_root = framework_large_tool_results_root()
        lexical_is_artifact = artifact_root is not None and lexical_root == artifact_root
        if not lexical_is_artifact and _is_service_protected_read_path(
            service, lexical_root, lexical_relative,
        ):
            return False
        resolved = candidate.resolve(strict=True)
        # Infrastructure roots are created lazily on first use. A missing
        # sibling root must not invalidate an otherwise valid existing scope.
        resolved_roots = tuple(root.resolve(strict=False) for root in roots)
        root = max(
            (
                root for root in resolved_roots
                if resolved == root or resolved.is_relative_to(root)
            ),
            key=lambda item: len(item.parts),
        )
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    resolved_is_artifact = artifact_root is not None and root == artifact_root
    if resolved_is_artifact:
        return True
    if (
        _is_service_protected_read_path(service, root, relative)
        or _has_protected_read_name(resolved)
    ):
        return False
    if is_operator_secret_read_path(resolved):
        return False
    if home:
        home_root = Path(home).resolve()
        shared_skill_roots = {
            home_root / "skills",
            home_root / ".mimir_builtin_skills",
        }
        owned_skill = getattr(service, "owned_skill_directory", None)
        owned_root = Path(owned_skill).resolve() if owned_skill else None
        is_owned_skill_path = owned_root is not None and (
            resolved == owned_root or resolved.is_relative_to(owned_root)
        )
        if root in shared_skill_roots and not is_owned_skill_path:
            if tool_name in {"read_file", "aread"} and resolved.name != "SKILL.md":
                return False
            if tool_name not in {"read_file", "aread"} and not resolved.is_dir():
                return False
    return not (
        tool_name in {"read_file", "aread"}
        and (not resolved.is_file() or file_contains_secret(resolved))
    )


def _target_matches_operator_alert(target: str, destination: str) -> bool:
    """Bind notify-only authority to one operator-selected destination."""
    configured = resolve_deliver_channel(
        OPERATOR_CHANNEL_SENTINEL, os.environ.get(destination, ""),
    )
    return bool(configured) and target == configured


def _target_matches_approved_url(target: str, destination: str) -> bool:
    """Match one exact URL from an operator-fixed URL or JSON list."""
    normalized = normalize_sink_destination(SinkCategory.NETWORK, target)
    return normalized is not None and normalized in _configured_exact_urls(destination)


_GITHUB_REPO_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_GITHUB_PR_API_PATH = re.compile(
    r"/repos/([^/]+)/([^/]+)/pulls/([1-9][0-9]*)(?:/(reviews|comments))?\Z"
)
_GITHUB_RAW_REPO_PATH = re.compile(r"/([^/]+)/([^/]+)/([^/]+)(?:/(.+))?\Z")


def _configured_github_repos(variable: str) -> frozenset[tuple[str, str]]:
    """Return syntactically safe owner/repo pairs from server-owned config."""
    repos: set[tuple[str, str]] = set()
    for item in os.environ.get(variable, "").split(","):
        parts = item.strip().split("/")
        if (
            len(parts) == 2
            and all(part not in {"", ".", ".."} for part in parts)
            and all(_GITHUB_REPO_SEGMENT.fullmatch(part) for part in parts)
        ):
            repos.add((parts[0].lower(), parts[1].lower()))
    return frozenset(repos)


def _target_matches_github_pr_api(target: str, destination: str) -> bool:
    """Match bounded GitHub PR API or raw-content reads for configured repos."""
    normalized = normalize_sink_destination(SinkCategory.NETWORK, target)
    if normalized is None:
        return False
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or parsed.query
        or "%" in parsed.path
        or "\\" in parsed.path
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
    ):
        return False
    if parsed.netloc == "api.github.com":
        match = _GITHUB_PR_API_PATH.fullmatch(parsed.path)
    elif parsed.netloc == "raw.githubusercontent.com":
        match = _GITHUB_RAW_REPO_PATH.fullmatch(parsed.path)
    else:
        return False
    if match is None:
        return False
    return (match[1].lower(), match[2].lower()) in _configured_github_repos(destination)


def _configured_exact_urls(variable: str) -> frozenset[str]:
    """Read one exact URL or a JSON array of exact URLs from an environment variable."""
    configured = os.environ.get(variable, "").strip()
    if not configured:
        return frozenset()
    if configured.startswith("["):
        try:
            parsed = json.loads(configured)
        except json.JSONDecodeError:
            return frozenset()
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            return frozenset()
        items = parsed
    else:
        if "," in configured:
            log.warning(
                "%s contains a comma but is not a JSON array; it will be treated "
                "as one exact URL. Configure multiple URLs as a JSON array.",
                variable,
            )
        items = [configured]

    urls: set[str] = set()
    for item in items:
        normalized = normalize_sink_destination(SinkCategory.NETWORK, item.strip())
        if normalized is not None:
            urls.add(normalized)
    return frozenset(urls)


def approved_fetch_urls(auth_context: Any) -> frozenset[str]:
    """Return exact fetch destinations authorized by config or this session."""
    approved = set(_configured_exact_urls("MIMIR_EGRESS_APPROVED_URLS"))
    service = get_trusted_service_from_auth_context(auth_context)
    policy = service.sink_policy_for("fetch_url") if service is not None else None
    if policy is not None and policy.adapter == "approved_urls":
        approved.update(_configured_exact_urls(policy.destination))
    state = getattr(auth_context, "egress_state", None)
    if state is not None and callable(getattr(state, "approved_urls", None)):
        approved.update(state.approved_urls())
    return frozenset(approved)


def _target_matches_configured_github_repo_fetch(target: str) -> bool:
    """Match HTTPS API or web reads scoped to a configured GitHub repository."""
    try:
        parsed = urlsplit(target.strip())
        port = parsed.port
    except (AttributeError, ValueError):
        return False
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.hostname is None
        or "%" in parsed.path
        or "\\" in parsed.path
    ):
        return False

    segments = parsed.path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        return False
    host = parsed.hostname.lower()
    if host == "api.github.com" and len(segments) >= 4 and segments[1] == "repos":
        owner, repo = segments[2:4]
    elif host == "github.com" and len(segments) >= 3:
        owner, repo = segments[1:3]
    else:
        return False
    return is_configured_github_repo(f"{owner}/{repo}")


def fetch_url_is_approved(target: str, auth_context: Any) -> bool:
    """Authorize an exact URL, configured GitHub repo read, or service adapter."""
    normalized = normalize_sink_destination(SinkCategory.NETWORK, target)
    if normalized is None:
        return False
    if normalized in approved_fetch_urls(auth_context):
        return True
    if _target_matches_configured_github_repo_fetch(target):
        return True
    service = get_trusted_service_from_auth_context(auth_context)
    policy = service.sink_policy_for("fetch_url") if service is not None else None
    if policy is None:
        return False
    adapter = _SERVICE_SINK_ADAPTERS.get(policy.adapter)
    return adapter is not None and adapter(normalized, policy.destination)


def _sink_adapter_admits(
    adapter: Any,
    target: str,
    destination: str,
    service: "ServicePrincipal | None" = None,
    *,
    review_state: Any = None,
) -> bool:
    """Invoke a sink adapter, handing the shell adapter the principal's grants.

    The adapter registry is ``(target, destination) -> bool`` and deliberately
    knows nothing about principals. Declared shell commands are per-principal, so
    the shell adapter -- and only it -- is called through the parser directly
    rather than through that narrow signature. Every other adapter is unchanged.
    """
    if adapter is None:
        return False
    if adapter is _target_matches_shell_profile:
        return parse_service_shell_argv(
            target, destination, review_state=review_state,
            declared=getattr(service, "declared_shell_commands", ()) or (),
        ) is not None
    return bool(adapter(target, destination))


_SERVICE_SINK_ADAPTERS: dict[str, Callable[[str, str], bool]] = {
    "configured_file_roots": _target_within_configured_write_roots,
    "configured_repo_write_roots": _target_within_configured_repo_write_roots,
    "static_service_write_roots": _target_within_static_service_write_roots,
    "upgrade_proposals": _target_within_upgrade_proposals,
    "shell_profile": _target_matches_shell_profile,
    "spawn_workspace": _target_within_configured_write_roots,
    "worklink_repo": _target_matches_worklink_repo,
    "trigger_service_write_roots": _target_within_trigger_service_write_roots,
    "operator_alert": _target_matches_operator_alert,
    "approved_urls": _target_matches_approved_url,
    "github_pr_api": _target_matches_github_pr_api,
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

# Operations in this set have no caller-supplied destination: each writes only
# fixed, derived paths, so the ordinary target adapter has nothing to validate.
# Taint gating still applies upstream at the capability-tier gate; membership
# here bypasses only destination checks. Keep membership narrow: a candidate
# must accept no caller-selected target and write only fixed derived destinations.
_FIXED_SERVICE_SINK_OPERATIONS = frozenset({"rebuild_index"})


_TAINT_INDEPENDENT_EGRESS_TOOLS = frozenset({"fetch_url", "web_search"})

_CHAINLINK_TAINT_REFUSAL = (
    "this turn carries untrusted active ingest, so Chainlink tracker mutations "
    "are unavailable for this turn. Read-only queries remain admitted: issue "
    "show, issue list, issue search, issue ready, issue blocked, issue related, "
    "issue cascade, issue next, issue tree, and session status."
)


def _live_untrusted_active_ingest(
    auth_context: Any, fallback: Any,
) -> bool | None:
    """Read taint from the server-owned live IFC state, or report indeterminate."""
    state = getattr(auth_context, "ifc_state", None)
    predicate = getattr(state, "has_untrusted_active_ingest", None)
    if not callable(predicate):
        return None
    try:
        result = predicate(fallback)
    except Exception:
        log.exception("ifc_untrusted_active_ingest_evaluation_failed")
        return None
    return result if isinstance(result, bool) else None


def _has_untrusted_active_ingest(
    auth_context: Any, fallback: Any, *, missing_is_tainted: bool = False,
) -> bool:
    """Resolve taint consistently, failing closed for indeterminate live state."""
    state = getattr(auth_context, "ifc_state", None)
    if callable(getattr(state, "has_untrusted_active_ingest", None)):
        live_taint = _live_untrusted_active_ingest(auth_context, fallback)
        return True if live_taint is None else live_taint
    return missing_is_tainted or bool(
        getattr(fallback, "has_untrusted_active_ingest", False)
    )


def _source_is_triggering_channel_compatible(
    source: Any,
    *,
    effective_principal: str,
    domain: str,
    bridge_instance: str,
    resolved_triggering: str,
) -> bool:
    """Return whether one IFC source may flow to the triggering channel."""
    if not getattr(source, "is_complete", False):
        return False
    # Fresh protected-result sources include the authenticated reader by
    # construction; inherited or externally supplied labels do not, so keep
    # this check as the fail-closed guard for those paths.
    if effective_principal not in source.authorized_principals:
        return False
    source_kind = getattr(source, "source_kind", "channel")
    if source_kind == "channel":
        return (
            source.principal == effective_principal
            and source.domain == domain
            and source.bridge_instance == bridge_instance
            and ChannelResourceAdapter._resolve_channel(source.resource_id)
            == resolved_triggering
        )
    if source_kind == "service":
        # Trusted service/derived data retains its input ACL. It may return only
        # to the triggering channel when its channel provenance matches.
        return not source.domain.startswith("channel") or (
            source.bridge_instance == bridge_instance
            and ChannelResourceAdapter._resolve_channel(source.resource_id)
            == resolved_triggering
        )
    return source_kind in {"protected_prompt", "protected_tool"}


def _ifc_blocking_source(
    ifc_labels: Any,
    auth_context: Any,
    sink_category: SinkCategory,
) -> tuple[Any | None, str]:
    """Classify one source for an IFC refusal and the certainty of the match."""
    from .models import InformationFlowLabels, Integrity, IntegrityEffect

    current = ifc_labels
    state = getattr(auth_context, "ifc_state", None)
    get_current = getattr(state, "current", None)
    if callable(get_current):
        candidate = get_current(ifc_labels)
        if isinstance(candidate, InformationFlowLabels):
            current = candidate
    if not isinstance(current, InformationFlowLabels):
        raise TypeError("IFC source classifier received invalid labels")
    if not current.sources:
        return None, "no_sources"

    if sink_category is SinkCategory.SAME_CHANNEL:
        resolved_triggering = ChannelResourceAdapter._resolve_channel(
            getattr(auth_context, "channel_id", None)
        )
        service = get_trusted_service_from_auth_context(auth_context)
        effective_principal = (
            f"service:{service.canonical}"
            if service is not None
            else getattr(auth_context, "canonical_principal", None)
        )
        domain = getattr(auth_context, "domain", None)
        bridge_instance = getattr(auth_context, "bridge_instance", None)
        if all((effective_principal, domain, bridge_instance, resolved_triggering)):
            for source in current.sources:
                if not _source_is_triggering_channel_compatible(
                    source,
                    effective_principal=effective_principal,
                    domain=domain,
                    bridge_instance=bridge_instance,
                    resolved_triggering=resolved_triggering,
                ):
                    return source, "causing_source"

    # This predicate is itself the gate for application egress and disables the
    # trusted-operator exemptions for shell, file, spawn, and channel sinks.
    for source in current.sources:
        if (
            source.integrity == Integrity.UNTRUSTED
            and source.integrity_effect == IntegrityEffect.ACTIVE_INGEST
        ):
            return source, "causing_source"

    # The remaining gate rules evaluate the labels as a set. Preserve a bounded
    # example without claiming that tuple order identifies the cause.
    return current.sources[0], "representative_source"


def _fixed_web_search_url() -> str | None:
    from .tools.web_search_destination import web_search_url

    return normalize_sink_destination(
        SinkCategory.NETWORK,
        web_search_url(),
    )


_REPOSITORY_RESULT_RESOURCE = re.compile(
    r"^(?P<repo>.+)#pull/(?P<pr>[1-9][0-9]*)@(?P<head>[0-9a-fA-F]{40})$",
)


def _forge_repository_scope_mismatch(
    ifc_labels: Any,
    scope: Any,
) -> tuple[str, str] | None:
    """Return the first repository result that is outside a forge sink scope."""
    expected_repo = getattr(scope, "canonical_repo", None)
    expected_issue = getattr(scope, "issue_number", None)
    expected_pr = getattr(scope, "pr_number", None)
    expected_head = getattr(scope, "observed_head_sha", None)
    for source in getattr(ifc_labels, "sources", ()):
        if getattr(source, "domain", None) != "repository":
            continue
        resource_id = getattr(source, "resource_id", None)
        match = (
            _REPOSITORY_RESULT_RESOURCE.fullmatch(resource_id)
            if isinstance(resource_id, str) else None
        )
        if match is None:
            return str(resource_id or "unknown"), "unknown"
        source_repo = match.group("repo")
        source_pr = int(match.group("pr"))
        source_head = match.group("head")
        if isinstance(expected_issue, int):
            if not (
                isinstance(expected_repo, str)
                and source_repo.casefold() == expected_repo.casefold()
            ):
                return source_repo, str(source_pr)
            continue
        if not (
            isinstance(expected_repo, str)
            and source_repo.casefold() == expected_repo.casefold()
            and source_pr == expected_pr
            and isinstance(expected_head, str)
            and source_head.casefold() == expected_head.casefold()
        ):
            return source_repo, str(source_pr)
    return None


def _sink_category_capability_turn_id(auth_context: Any) -> str | None:
    """Resolve reusable authority from the durable AuthContext IFC carrier."""
    from ._context import get_current_turn

    state = getattr(auth_context, "ifc_state", None)
    get_bound_turn_id = getattr(state, "sink_category_turn_id", None)
    if not callable(get_bound_turn_id):
        return None
    turn_id = get_bound_turn_id()
    if not isinstance(turn_id, str) or not turn_id:
        return None

    # Forked SDK/MCP tasks can legitimately lose the ContextVar. The immutable
    # request binding on the genuine IFC state remains authoritative there. If
    # ambient turn context is present, retain the stronger cross-check so a
    # carrier attached to another TurnContext still fails closed.
    turn = get_current_turn()
    if turn is None:
        return turn_id
    if (
        getattr(turn, "turn_id", None) != turn_id
        or getattr(getattr(turn, "auth_context", None), "ifc_state", None) is not state
    ):
        return None
    return turn_id


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

    @staticmethod
    def _service_tier_allows(
        tool_name: str,
        ifc_labels: Any,
        auth_context: Any,
        service: ServicePrincipal,
        target: str | None = None,
    ) -> tuple[bool, str | None]:
        """Apply the integrity axis to one exact declared service capability."""
        if not service.has_capability(tool_name):
            return False, None
        capability_tier = TRIGGER_CAPABILITY_TIERS.get(
            tool_name,
            _LEGACY_SERVICE_SINK_TIERS.get(tool_name, CapabilityTier.UNBOUNDED),
        )
        if tool_name in {"shell_exec", "bash_async"}:
            chainlink_argv = _chainlink_target_argv(target)
            if chainlink_argv is not None:
                has_untrusted_active_ingest = _has_untrusted_active_ingest(
                    auth_context, getattr(auth_context, "ifc_labels", None),
                    missing_is_tainted=True,
                )
                if (
                    _chainlink_command_is_mutation(chainlink_argv)
                    and has_untrusted_active_ingest
                ):
                    return False, _CHAINLINK_TAINT_REFUSAL
                # Bounded tracker queries remain available even where the
                # surrounding profile's shell is taint-gated.
                return True, None
        has_untrusted_active_ingest = _has_untrusted_active_ingest(
            auth_context, ifc_labels,
        )
        # Shell is an executable sink even when argv is tightly scoped. The
        # profile bounds capability; IFC independently prevents untrusted PR
        # content from exercising that capability on the same turn.
        if (
            service.authority_profile == "github"
            and tool_name in {"shell_exec", "bash_async"}
            and has_untrusted_active_ingest
        ):
            return False, None
        if capability_tier is CapabilityTier.CODE_EXECUTION:
            return (
                tool_name in {"worklink_run", "repo_test"}
                and not has_untrusted_active_ingest,
                None,
            )
        if capability_tier is CapabilityTier.UNBOUNDED:
            return (
                tool_name in _TAINT_INDEPENDENT_EGRESS_TOOLS
                or not has_untrusted_active_ingest,
                None,
            )
        return True, None

    @staticmethod
    def _is_trusted_operator_turn(ifc_labels: Any, auth_context: Any) -> bool:
        """Recognize a bridge-authenticated operator's own turn ingress."""
        from .models import Integrity, IntegrityEffect, TurnInteractivity

        if (
            auth_context is None
            or get_trusted_service_from_auth_context(auth_context) is not None
            or getattr(auth_context, "trigger", None) != "user_message"
            or getattr(auth_context, "interactivity", None) != TurnInteractivity.INTERACTIVE
            or getattr(auth_context, "event_ingress", None) is not None
        ):
            return False
        principal = getattr(auth_context, "canonical_principal", None)
        channel = getattr(auth_context, "resource_id", None)
        domain = getattr(auth_context, "domain", None)
        bridge = getattr(auth_context, "bridge_instance", None)
        if not all((principal, channel, domain, bridge)):
            return False
        return any(
            source.source_kind == "channel"
            and source.principal == principal
            and source.domain == domain
            and source.resource_id == channel
            and source.bridge_instance == bridge
            and principal in source.authorized_principals
            and source.integrity == Integrity.TRUSTED
            and source.integrity_effect == IntegrityEffect.ACTIVE_INGEST
            for source in getattr(ifc_labels, "sources", ())
        )

    @classmethod
    def _is_admin_operator_turn(cls, ifc_labels: Any, auth_context: Any) -> bool:
        """Recognize an untainted, bridge-authenticated admin operator turn."""
        has_untrusted_active_ingest = _has_untrusted_active_ingest(
            auth_context, ifc_labels,
        )
        return (
            cls._is_trusted_operator_turn(ifc_labels, auth_context)
            and has_untrusted_active_ingest is False
            and "admin" in (getattr(auth_context, "roles", ()) or ())
        )

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
        allow_untrusted_active_ingest: bool = False,
        repo_review_state: Any = None,
        repo_review_state_refusal: str | None = None,
        repo_pr_action_scope: Any = None,
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

        sink_category = sink_category or get_sink_category(tool_name)
        service = get_trusted_service_from_auth_context(auth_context)
        if not isinstance(ifc_labels, InformationFlowLabels):
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason="missing_ifc_labels",
                service_principal=service,
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
                would_block=True,
                resolved_sink_target=(
                    resolve_sink_target(tool_name, sink_category, target, service)
                    if target else None
                ),
            )

        if sink_category == SinkCategory.UNKNOWN:
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason="unknown_sink_category",
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
                would_block=True,
            )

        if not target:
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason="unknown_sink_destination",
                service_principal=service,
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
                would_block=True,
            )

        # Activity-panel payloads are constrained at their producer to fixed
        # harness metadata (status, sanitized tool names, and counts). They do
        # not carry turn content, so turn taint is not relevant to this display.
        if sink_category is SinkCategory.HARNESS_DISPLAY:
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.OPEN,
                allowed=True,
                reason="harness_metadata_display",
                service_principal=service,
                enforcement_enabled=enforce,
                resolved_sink_target=resolve_sink_target(
                    tool_name, sink_category, target, service,
                ),
            )

        is_application_egress = sink_category in {
            SinkCategory.NETWORK,
            SinkCategory.HTTP_WEBHOOK,
            SinkCategory.EXTERNAL_MCP,
        }
        normalized_target = normalize_sink_destination(sink_category, target)
        resolved_target = resolve_sink_target(
            tool_name, sink_category, target, service,
        )
        if (
            is_application_egress
            and tool_name not in _TAINT_INDEPENDENT_EGRESS_TOOLS
            and ifc_labels.labels
            and not ifc_labels.sources
        ):
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason=f"ifc_label_blocked:{sink_category.value}",
                service_principal=service,
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
                would_block=True,
                resolved_sink_target=resolved_target,
            )
        state = getattr(auth_context, "ifc_state", None)
        has_untrusted_active_ingest = _has_untrusted_active_ingest(
            auth_context, ifc_labels,
        )
        if (
            is_application_egress
            and tool_name not in _TAINT_INDEPENDENT_EGRESS_TOOLS
            and not allow_untrusted_active_ingest
            and has_untrusted_active_ingest
        ):
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
                    service_principal=service,
                    enforcement_enabled=enforce,
                    resolved_sink_target=resolved_target,
                )
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason=f"ifc_label_blocked:{sink_category.value}",
                service_principal=service,
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
                would_block=True,
                resolved_sink_target=resolved_target,
            )
        if tool_name == "web_search":
            fixed_web_search_url = _fixed_web_search_url()
            if fixed_web_search_url is None or normalized_target != fixed_web_search_url:
                return ToolAuthorization(
                    tool_name=tool_name,
                    decision=OperationDecision.ADMIN_REQUIRED,
                    allowed=not enforce,
                    reason="egress_destination_not_approved",
                    service_principal=service,
                    required_tier=AccessTier.ADMIN,
                    enforcement_enabled=enforce,
                    is_shadow_decision=not enforce,
                    would_block=True,
                    resolved_sink_target=resolved_target,
                )
        if tool_name == "fetch_url" and (
            normalized_target is None
            or not fetch_url_is_approved(target, auth_context)
        ):
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason="egress_destination_not_approved",
                service_principal=service,
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
                would_block=True,
                resolved_sink_target=resolved_target,
            )
        if tool_name in {"webhook", "http_request"} and (
            normalized_target is None
            or normalized_target not in _configured_exact_urls("MIMIR_EGRESS_APPROVED_URLS")
        ):
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason="egress_destination_not_approved",
                service_principal=service,
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=not enforce,
                would_block=True,
                resolved_sink_target=resolved_target,
            )
        service_policy: ServiceSinkPolicy | None = None
        if service is not None and sink_category is SinkCategory.SAME_CHANNEL:
            candidate = service.sink_policy_for(tool_name)
            if candidate is not None:
                adapter = _SERVICE_SINK_ADAPTERS.get(candidate.adapter)
                triggering = getattr(auth_context, "channel_id", None)
                if target != triggering:
                    if not _sink_adapter_admits(
                        adapter, target, candidate.destination, service,
                    ):
                        return ToolAuthorization(
                            tool_name=tool_name,
                            decision=OperationDecision.ADMIN_REQUIRED,
                            allowed=not enforce,
                            reason="service_sink_destination_denied",
                            service_principal=service,
                            required_tier=AccessTier.ADMIN,
                            enforcement_enabled=enforce,
                            is_shadow_decision=not enforce,
                            would_block=True,
                            resolved_sink_target=resolved_target,
                        )
                    service_policy = candidate
        if service is not None and sink_category in {
            SinkCategory.SHELL_PROCESS,
            SinkCategory.SPAWN,
            SinkCategory.FILE,
            SinkCategory.NOTIFICATION,
            SinkCategory.HTTP_WEBHOOK,
            SinkCategory.NETWORK,
            SinkCategory.EXTERNAL_MCP,
        }:
            tier_allowed, tier_refusal = cls._service_tier_allows(
                tool_name, ifc_labels, auth_context, service, target,
            )
            if not tier_allowed:
                return ToolAuthorization(
                    tool_name=tool_name,
                    decision=OperationDecision.ADMIN_REQUIRED,
                    allowed=not enforce,
                    reason=(
                        "chainlink_mutation_blocked_by_untrusted_ingest"
                        if tier_refusal else f"ifc_label_blocked:{sink_category.value}"
                    ),
                    service_principal=service,
                    required_tier=AccessTier.ADMIN,
                    enforcement_enabled=enforce,
                    is_shadow_decision=not enforce,
                    would_block=True,
                    resolved_sink_target=resolved_target,
                    refusal_detail=tier_refusal,
                )
            service_policy = service.sink_policy_for(tool_name)
            adapter = (
                _SERVICE_SINK_ADAPTERS.get(service_policy.adapter)
                if service_policy is not None
                else None
            )
            review_state = (
                repo_review_state
                if repo_review_state is not None
                else getattr(auth_context, "repo_review_state", None)
            )
            service_target_allowed = False
            if service_policy is not None and adapter is not None:
                service_target_allowed = (
                    not (
                        adapter is _target_matches_shell_profile
                        and service_policy.destination == "repo_review"
                        and repo_review_state_refusal is not None
                    )
                    and _sink_adapter_admits(
                        adapter, target, service_policy.destination, service,
                        review_state=review_state,
                    )
                )
            if (
                not service_target_allowed
                and sink_category is SinkCategory.FILE
                and service.canonical == "poller:github-activity"
            ):
                service_target_allowed = _target_within_active_pr_checkout_lease(
                    target, review_state,
                )
            fixed_file_destination = tool_name in _FIXED_SERVICE_SINK_OPERATIONS
            synthesis_scope_denied = (
                service.canonical == "synthesis"
                and sink_category is SinkCategory.FILE
                and not fixed_file_destination
                and resolve_large_tool_results_target(target) is None
                and not _synthesis_target_matches_session(
                    target, getattr(auth_context, "channel_id", None),
                )
            )
            github_repo_scope_refusal = None
            scope = None
            if (
                service.authority_profile == "github"
                and sink_category is SinkCategory.FILE
            ):
                review_state = (
                    repo_review_state
                    if repo_review_state is not None
                    else getattr(auth_context, "repo_review_state", None)
                )
                scope = getattr(review_state, "action_scope", None)
                target_in_lease = _target_within_active_pr_checkout_lease(
                    target, review_state,
                )
                if target_in_lease:
                    service_target_allowed = True
                    if not _repo_review_action_allowed(
                        review_state, RepoPRAction.WRITE,
                    ):
                        github_repo_scope_refusal = "repo_pr_write_not_granted"
                elif scope is not None:
                    lease = getattr(review_state, "checkout_lease", None)
                    roots = (
                        Path(scope.canonical_root),
                        *(
                            (Path(lease.lease_root),)
                            if lease is not None else ()
                        ),
                    )
                    try:
                        resolved_target_path = Path(target).resolve(strict=False)
                        targets_pr_checkout_area = any(
                            resolved_target_path.is_relative_to(root.resolve(strict=True))
                            for root in roots
                        )
                    except (OSError, RuntimeError):
                        targets_pr_checkout_area = True
                    if targets_pr_checkout_area:
                        service_target_allowed = False
                        github_repo_scope_refusal = (
                            "repo_pr_target_outside_active_lease"
                        )
            if (
                (adapter is None and not fixed_file_destination)
                or synthesis_scope_denied
                or (not service_target_allowed and not fixed_file_destination)
                or github_repo_scope_refusal is not None
            ):
                return ToolAuthorization(
                    tool_name=tool_name,
                    decision=OperationDecision.ADMIN_REQUIRED,
                    allowed=not enforce,
                    reason=(
                        github_repo_scope_refusal
                        if github_repo_scope_refusal is not None
                        else "service_sink_destination_denied"
                    ),
                    service_principal=service,
                    required_tier=AccessTier.ADMIN,
                    enforcement_enabled=enforce,
                    is_shadow_decision=not enforce,
                    would_block=True,
                    resolved_sink_target=resolved_target,
                    refusal_detail=repo_review_state_refusal or _service_shell_refusal_detail(
                        target, service_policy, review_state,
                    ),
                    repo_pr_action_scope=scope,
                )

        if not ifc_labels.labels:
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.OPEN,
                allowed=True,
                reason="no_labels",
                service_principal=service,
                enforcement_enabled=enforce,
                resolved_sink_target=resolved_target,
            )

        if sink_category is SinkCategory.FORGE:
            mismatch = _forge_repository_scope_mismatch(
                ifc_labels, repo_pr_action_scope,
            )
            if mismatch is not None:
                source_repo, source_pr = mismatch
                destination_repo = getattr(
                    repo_pr_action_scope, "canonical_repo", "unknown",
                )
                destination_pr = getattr(
                    repo_pr_action_scope, "pr_number", "unknown",
                )
                return ToolAuthorization(
                    tool_name=tool_name,
                    decision=OperationDecision.ADMIN_REQUIRED,
                    allowed=not enforce,
                    reason="ifc_label_blocked:forge",
                    service_principal=service,
                    required_tier=AccessTier.ADMIN,
                    enforcement_enabled=enforce,
                    is_shadow_decision=not enforce,
                    would_block=True,
                    resolved_sink_target=resolved_target,
                    refusal_detail=(
                        f"repository result from {source_repo}#{source_pr} cannot "
                        f"flow to {destination_repo}#{destination_pr}: repository, "
                        "pull request, and observed head must match"
                    ),
                )

        allowed_sinks = cls._get_allowed_sinks(
            tool_name,
            sink_category,
            auth_context,
            ifc_labels=ifc_labels,
            service_policy=service_policy,
            target=target,
        )
        effective_target = (
            ChannelResourceAdapter._resolve_channel(target)
            if sink_category in {
                SinkCategory.SAME_CHANNEL,
                SinkCategory.CROSS_CHANNEL,
                SinkCategory.DIRECT_MESSAGE,
            }
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
                    turn_id=(
                        _sink_category_capability_turn_id(auth_context)
                        if sink_category in _SINK_CATEGORY_CAPABILITY_ELIGIBLE
                        else None
                    ),
                )
            ):
                return ToolAuthorization(
                    tool_name=tool_name,
                    decision=OperationDecision.OPEN,
                    allowed=True,
                    reason="ifc_declassification_approved",
                    service_principal=service,
                    enforcement_enabled=enforce,
                    resolved_sink_target=resolved_target,
                )
            reason = f"ifc_label_blocked:{sink_category.value}"
            is_shadow = not enforce
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.ADMIN_REQUIRED,
                allowed=not enforce,
                reason=reason,
                service_principal=service,
                required_tier=AccessTier.ADMIN,
                enforcement_enabled=enforce,
                is_shadow_decision=is_shadow,
                would_block=True,
                resolved_sink_target=resolved_target,
            )

        return ToolAuthorization(
            tool_name=tool_name,
            decision=OperationDecision.OPEN,
            allowed=True,
            reason="ifc_allowed",
            service_principal=service,
            enforcement_enabled=enforce,
            resolved_sink_target=resolved_target,
        )

    @classmethod
    def _get_allowed_sinks(
        cls,
        tool_name: str,
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
        trusted_operator_turn = cls._is_trusted_operator_turn(
            ifc_labels, auth_context,
        )
        has_untrusted_active_ingest = _has_untrusted_active_ingest(
            auth_context, ifc_labels,
        )
        triggering_channel = getattr(auth_context, "channel_id", None)
        resolved_target_channel = (
            ChannelResourceAdapter._resolve_channel(target)
            if category in {
                SinkCategory.SAME_CHANNEL,
                SinkCategory.CROSS_CHANNEL,
                SinkCategory.DIRECT_MESSAGE,
            }
            else target
        )
        is_cross_channel_operation = category in {
            SinkCategory.CROSS_CHANNEL,
            SinkCategory.DIRECT_MESSAGE,
            SinkCategory.NOTIFICATION,
        } or (
            category is SinkCategory.SAME_CHANNEL
            and resolved_target_channel
            != ChannelResourceAdapter._resolve_channel(triggering_channel)
        )
        sources = getattr(ifc_labels, "sources", None)
        if (
            cls._is_admin_operator_turn(ifc_labels, auth_context)
            and is_cross_channel_operation
            and target is not None
            and isinstance(sources, tuple)
            and bool(sources)
            and all(
                source.is_complete
                and getattr(auth_context, "canonical_principal", None)
                in source.authorized_principals
                for source in sources
            )
        ):
            return frozenset({resolved_target_channel})
        if (
            trusted_operator_turn
            and not has_untrusted_active_ingest
            and target is not None
            and category in {SinkCategory.SHELL_PROCESS, SinkCategory.FILE}
        ):
            return frozenset({target})
        is_triggering_channel_reply = (
            service is not None
            and category is SinkCategory.SAME_CHANNEL
            and service_policy is None
        )
        if service is not None and not is_triggering_channel_reply:
            tier_allowed, _ = cls._service_tier_allows(
                tool_name, ifc_labels, auth_context, service, target,
            )
            if not tier_allowed:
                return frozenset()

        if service is not None and target is not None and category in {
            SinkCategory.SAGA,
            SinkCategory.SCHEDULER,
            SinkCategory.PROPOSAL,
            SinkCategory.FORGE,
        }:
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

        if category in {
            SinkCategory.NETWORK,
            SinkCategory.HTTP_WEBHOOK,
            SinkCategory.EXTERNAL_MCP,
        } and target is not None:
            return frozenset({target})
        if category != SinkCategory.SAME_CHANNEL:
            return frozenset()

        if not triggering_channel:
            return frozenset()
        resolved_triggering = ChannelResourceAdapter._resolve_channel(triggering_channel)
        if not resolved_triggering:
            return frozenset()

        if tool_name == "send_message" and target is not None:
            if _target_matches_operator_alert(
                resolved_target_channel or "", "MIMIR_OPERATOR_ALERT_CHANNEL",
            ):
                return frozenset({resolved_target_channel})
            from .models import TurnInteractivity

            resolved_resource = ChannelResourceAdapter._resolve_channel(
                getattr(auth_context, "resource_id", None),
            )
            canonical_principal = getattr(auth_context, "canonical_principal", None)
            domain = getattr(auth_context, "domain", None)
            bridge_instance = getattr(auth_context, "bridge_instance", None)
            sources = getattr(ifc_labels, "sources", ())
            has_authenticated_ingress = any(
                source.source_kind == "channel"
                and source.principal == canonical_principal
                and source.domain == domain
                and ChannelResourceAdapter._resolve_channel(source.resource_id)
                == resolved_resource
                and source.bridge_instance == bridge_instance
                and canonical_principal in source.authorized_principals
                and source.integrity == "trusted"
                and source.integrity_effect == "active_ingest"
                for source in sources
            )
            if (
                getattr(auth_context, "trigger", None) == "user_message"
                and getattr(auth_context, "interactivity", None)
                is TurnInteractivity.INTERACTIVE
                and getattr(auth_context, "event_ingress", None) is None
                and has_authenticated_ingress
                and resolved_resource == resolved_triggering == resolved_target_channel
            ):
                return frozenset({resolved_target_channel})

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
        if not isinstance(sources, tuple) or not sources:
            return frozenset()

        for source in sources:
            if not _source_is_triggering_channel_compatible(
                source,
                effective_principal=effective_principal,
                domain=domain,
                bridge_instance=bridge_instance,
                resolved_triggering=resolved_triggering,
            ):
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


def resolve_sink_target(
    tool_name: str,
    sink_category: SinkCategory,
    target: str,
    service: ServicePrincipal | None,
) -> str | None:
    """Return the concrete destination representation evaluated by the gate."""
    if sink_category is not SinkCategory.SHELL_PROCESS:
        return normalize_sink_destination(sink_category, target)
    try:
        argv = shlex.split(target)
    except ValueError:
        return None
    policy = service.sink_policy_for(tool_name) if service is not None else None
    if policy is not None and policy.adapter == "shell_profile":
        execution_argv = parse_service_shell_argv(
            target, policy.destination,
            declared=getattr(service, "declared_shell_commands", ()) or (),
        )
        if execution_argv is not None:
            argv = execution_argv
    return json.dumps(argv, ensure_ascii=True) if argv else None


def approve_live_declassification(
    auth_context: Any,
    *,
    sink_category: Any,
    destination: Any,
    reason: Any,
) -> tuple[bool, str]:
    """Approve one exact sink on the exact live admin request carrier."""
    from .models import AuthContext, InformationFlowState

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
                "integrity": source.integrity,
                "integrity_effect": source.integrity_effect,
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
    if approved and category is SinkCategory.NETWORK:
        # Destination approval persists for this server-owned session, while
        # the declassification capability remains one-use and turn-bound.
        auth_context.egress_state.approve_url(normalized)
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
        if tool_name not in cls._CHANNEL_OPERATIONS:  # Misuse guard; never shadow-emitted.
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
                would_block=True,
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
            would_block=not is_admin,
            resolved_sink_target=resolved_target,
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


class WriteResourceAdapter:
    """Scope write/code operations by the server-authenticated caller axis."""

    _WRITE_OPERATIONS: frozenset[str] = frozenset({"write_file", "edit_file"})
    _RESOURCE_OPERATIONS: frozenset[str] = _WRITE_OPERATIONS | {"worklink_run"}
    _PROTECTED_NAMES: frozenset[str] = frozenset({
        ".env", ".git", "compose.env", "rate_limits.json",
        "config", "credentials", "identities", "secrets", "secret",
        "core-memory", "core_memory", "corememory", "prompts",
    })

    @classmethod
    def get_decision(
        cls,
        tool_name: str,
        context: Any | None,
    ) -> OperationDecision | None:
        if tool_name in cls._RESOURCE_OPERATIONS:
            return OperationDecision.RESOURCE_SCOPED
        return None

    @classmethod
    def _is_protected_path(cls, path: Path) -> bool:
        parts = tuple(part.lower() for part in path.parts)
        for part in parts:
            stem = Path(part).stem
            if (
                part in cls._PROTECTED_NAMES
                or stem.split(".", 1)[0] in {
                    "config", "credentials", "identities", "secret", "secrets",
                }
                or part.startswith(".env.")
                or part.startswith("oauth_") and part.endswith(".json")
                or Path(part).suffix in {".key", ".pem"}
            ):
                return True
        return any(
            parts[index:index + 2] == ("memory", "core")
            for index in range(len(parts) - 1)
        )

    @classmethod
    def _human_target_is_allowed(cls, target: str | None) -> bool:
        home = os.environ.get("MIMIR_HOME", "").strip()
        if not home or not isinstance(target, str) or not target.strip() or "\x00" in target:
            return False
        from ._paths import PathOutsideHomeError, resolve_within_roots

        try:
            home_root = Path(home).resolve()
            state_root = (Path(home) / "state").resolve()
            resolved = resolve_within_roots([home_root], target)
            turn_scratch = current_turn_scratch_root()
            root = next(
                root for root in (state_root, turn_scratch) if root is not None
                and (resolved == root or resolved.is_relative_to(root))
            )
        except (OSError, PathOutsideHomeError, RuntimeError, ValueError):
            return False
        except StopIteration:
            return False
        return not cls._is_protected_path(resolved.relative_to(root))

    @classmethod
    def authorize_skill_write(
        cls,
        tool_name: str,
        target: str | None,
        auth_context: "AuthContext | None",
        ifc_labels: Any,
        *,
        enforce: bool,
    ) -> ToolAuthorization | None:
        """Apply the admin-operator boundary to all file-tool skill writes."""
        if tool_name not in cls._WRITE_OPERATIONS or not isinstance(target, str):
            return None
        home = os.environ.get("MIMIR_HOME", "").strip()
        if not home:
            return None
        roots = agent_writable_roots(home)
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = Path(home).resolve() / candidate
        writable_for_admin_operator = _agent_writable_root_for_path(
            candidate, roots, admin_operator_turn=True,
        )
        autonomously_writable = _agent_writable_root_for_path(
            candidate, roots, admin_operator_turn=False,
        )
        if writable_for_admin_operator is None or autonomously_writable is not None:
            return None

        admin_operator_turn = SinkGate._is_admin_operator_turn(
            ifc_labels, auth_context,
        )
        reason = None if admin_operator_turn else "skill_write_requires_admin_operator"
        return ToolAuthorization(
            tool_name=tool_name,
            decision=OperationDecision.RESOURCE_SCOPED,
            allowed=admin_operator_turn,
            reason=reason,
            required_tier=AccessTier.USER if admin_operator_turn else AccessTier.ADMIN,
            enforcement_enabled=enforce,
            is_shadow_decision=False,
            would_block=not admin_operator_turn,
            resolved_sink_target=normalize_sink_destination(SinkCategory.FILE, target),
            refusal_detail=(
                None
                if admin_operator_turn
                else "writes under skills/ require an untainted admin operator turn"
            ),
        )

    @classmethod
    def authorize_operation(
        cls,
        tool_name: str,
        target: str | None,
        auth_context: "AuthContext | None",
        *,
        enforce: bool,
        service_allowed: bool,
    ) -> ToolAuthorization:
        roles = (getattr(auth_context, "roles", ()) or ()) if auth_context else ()
        if "admin" in roles or service_allowed:
            return ToolAuthorization(
                tool_name=tool_name,
                decision=OperationDecision.RESOURCE_SCOPED,
                allowed=True,
                service_principal=(
                    get_trusted_service_from_auth_context(auth_context)
                    if service_allowed else None
                ),
                enforcement_enabled=enforce,
            )

        from .models import TurnInteractivity

        in_scope = (
            tool_name in cls._WRITE_OPERATIONS
            and auth_context is not None
            and getattr(auth_context, "interactivity", None) == TurnInteractivity.INTERACTIVE
            and cls._human_target_is_allowed(target)
        )
        reason = None if in_scope else (
            "admin_required" if tool_name == "worklink_run" else "write_scope"
        )
        return ToolAuthorization(
            tool_name=tool_name,
            decision=OperationDecision.RESOURCE_SCOPED,
            allowed=in_scope or not enforce,
            reason=reason,
            required_tier=AccessTier.USER if in_scope else AccessTier.ADMIN,
            enforcement_enabled=enforce,
            is_shadow_decision=not enforce and not in_scope,
            would_block=not in_scope,
            resolved_sink_target=(
                normalize_sink_destination(SinkCategory.FILE, target)
                if target is not None else None
            ),
        )


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
        "request_operator_approval",
        "commitment_complete",
        "commitment_snooze",
        "commitment_dismiss",
    })

    _RESOURCE_SCOPED_OPERATIONS: frozenset[str] = (
        READ_RESOURCE_OPERATIONS
        | WriteResourceAdapter._RESOURCE_OPERATIONS
        | frozenset(_TYPED_REPO_PR_TOOL_ACTIONS)
    )

    _ADMIN_REQUIRED_OPERATIONS: frozenset[str] = frozenset({
        "issue_comment",
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
        "shell_exec",
        "bash_async",
        "bash_jobs_list",
        "bash_job_output",
        "spawn_open_code",
        "task",
        "memory_store",
        "saga_feedback",
        "saga_mark_contributions",
        "saga_end_session",
        "saga_record_skill_learning",
        "saga_forget",
        "set_poller_overrides",
        "download_files",
        "adownload_files",
        "rebuild_index",
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
        protected_decision = (
            OperationDecision.RESOURCE_SCOPED
            if name in self._RESOURCE_SCOPED_OPERATIONS
            else OperationDecision.ADMIN_REQUIRED
        )
        is_protected_catalogued = (
            name in self._ADMIN_REQUIRED_OPERATIONS
            or name in self._RESOURCE_SCOPED_OPERATIONS
            or name in self._ADMIN_BUILTIN_TOOL_NAMES
            or any(
                name.endswith(f"__{catalogued}")
                or name.endswith(f"_{catalogued}")
                for catalogued in self._ADMIN_REQUIRED_OPERATIONS
            )
        )
        if name in saga_mutations:
            protected_decision = OperationDecision.ADMIN_REQUIRED
        if (is_protected_catalogued or name in saga_mutations) and decision != protected_decision:
            raise ValueError(
                f"cannot downgrade protected operation {name!r} "
                f"from {protected_decision.value}"
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

        if tool_name in self._RESOURCE_SCOPED_OPERATIONS:
            return OperationDecision.RESOURCE_SCOPED

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
_global_operation_catalog.register_adapter_hook(
    WriteResourceAdapter.get_decision,
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
                                    allow_untrusted_active_ingest=(
                                        provenance.argument_egress == "allowed"
                                    ),
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
                                    would_block=(
                                        sink_check.would_block if sink_check is not None else False
                                    ),
                                    resolved_sink_target=(
                                        sink_check.resolved_sink_target
                                        if sink_check is not None else None
                                    ),
                                    protected_source_resources=result.source_resources,
                                    protected_sink_resources=result.sink_resources,
                                    flow_direction=flow_direction,
                                    result_integrity=provenance.result_integrity,
                                    argument_egress=provenance.argument_egress,
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
            would_block=(
                sink_check.would_block if shadow_sink and sink_check is not None
                else denied_by_policy
            ),
            resolved_sink_target=(
                sink_check.resolved_sink_target if sink_check is not None else None
            ),
            protected_source_resources=(
                validated_result.source_resources if validated_result is not None else None
            ),
            protected_sink_resources=(
                validated_result.sink_resources if validated_result is not None else None
            ),
            flow_direction=flow_direction,
            result_integrity=(
                provenance.result_integrity if validated_result is not None else "untrusted"
            ),
            argument_egress=(
                provenance.argument_egress if validated_result is not None else "taint_gated"
            ),
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
                    would_block=True,
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
            would_block=not allowed,
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
    # Enforcement verdict, independent of whether compatibility mode lets the
    # current call proceed.
    would_block: bool = False
    resolved_sink_target: str | None = None
    # Why a refusal happened, in prose, for the caller's tool result. Separate
    # from ``reason`` because that stays a stable machine key the audit
    # classification groups on; this is the human/agent-facing explanation and
    # must never be parsed. Populated for shell-profile refusals, where
    # ``service_sink_destination_denied`` alone renders as "requires an admin
    # identity" and misdescribes a command-shape problem as a privilege problem.
    refusal_detail: str | None = None
    # ``None`` means provenance is unknown; ``()`` authoritatively classifies
    # the call as not reading a protected MCP source.
    protected_source_resources: tuple[str, ...] | None = None
    protected_sink_resources: tuple[str, ...] | None = None
    flow_direction: ToolFlowDirection = ToolFlowDirection.UNKNOWN
    # Resolved once from immutable MCP provenance. Non-MCP and error paths use
    # the fail-closed posture and never perform a mutable policy lookup.
    result_integrity: str = "untrusted"
    argument_egress: str = "taint_gated"
    repo_pr_action_scope: Any = field(default=None, repr=False)

    def as_log_fields(self) -> dict[str, Any]:
        """Return fields for audit logging."""
        scope = self.repo_pr_action_scope
        return {
            "tool": self.tool_name,
            "decision": self.decision.value,
            "allowed": self.allowed,
            "reason": self.reason,
            "required_tier": self.required_tier.value,
            "service_principal": self.service_principal.canonical if self.service_principal else None,
            "enforcement_enabled": self.enforcement_enabled,
            "is_shadow_decision": self.is_shadow_decision,
            "scope_provenance": getattr(scope, "provenance", None),
            "scope_id": getattr(scope, "scope_id", None),
            "granted_actions": sorted(
                getattr(scope, "allowed_operations", frozenset())
            ),
            "refusal_reason": self.reason if not self.allowed else None,
        }


def authorize_repo_pr_tool(
    tool_name: str,
    scope: Any,
    *,
    service_principal: ServicePrincipal | None,
    enforce: bool,
    flow_direction: ToolFlowDirection,
    required_actions: tuple[str, ...] | None = None,
) -> ToolAuthorization:
    """Make the sole policy decision for one typed PR/repository action."""
    if tool_name not in _TYPED_REPO_PR_TOOL_ACTIONS:
        raise ValueError(f"not a typed pull-request tool: {tool_name}")
    report_required_actions = required_actions is not None
    if required_actions is None:
        required_action = _TYPED_REPO_PR_TOOL_ACTIONS[tool_name]
        required_actions = () if required_action is None else (required_action,)
    granted_actions = getattr(scope, "allowed_operations", frozenset())
    missing_actions = tuple(
        action for action in required_actions if action not in granted_actions
    )
    in_scope = (
        scope is not None
        and not missing_actions
    )
    return ToolAuthorization(
        tool_name=tool_name,
        decision=OperationDecision.RESOURCE_SCOPED,
        allowed=in_scope or not enforce,
        reason=None if in_scope else "repo_pr_scope_denied",
        service_principal=service_principal,
        required_tier=AccessTier.USER,
        enforcement_enabled=enforce,
        is_shadow_decision=not enforce and not in_scope,
        would_block=not in_scope,
        refusal_detail=(
            f"scope does not grant {', '.join(missing_actions)}"
            if report_required_actions and missing_actions
            else None
        ),
        flow_direction=flow_direction,
        repo_pr_action_scope=scope,
    )


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
        *,
        auth_context: "AuthContext | None" = None,
        target: str | None = None,
        requested_target: Any = None,
        arguments: dict[str, Any] | None = None,
        ifc_labels: Any = None,
        sink_category: SinkCategory | None = None,
    ) -> None:
        """Emit shadow-decision audit log (when enabled)."""
        if not self._shadow_logging_enabled:
            return
        try:
            import asyncio

            from .event_logger import log_event
            fields = auth.as_log_fields()
            service = auth.service_principal
            if service is None and auth_context is not None:
                service = get_trusted_service_from_auth_context(auth_context)
            if target is None and auth.protected_sink_resources:
                target = ",".join(auth.protected_sink_resources)
            resolved_target = auth.resolved_sink_target
            if resolved_target is None and target is not None:
                category = get_sink_category(auth.tool_name)
                resolved_target = resolve_sink_target(
                    auth.tool_name, category, target, service,
                )
            if auth.reason == "read_scope":
                requested_target = requested_read_target_from_arguments(
                    auth.tool_name, arguments,
                )
                try:
                    resolved_target = resolved_read_target_from_arguments(
                        auth.tool_name, arguments,
                    )
                except Exception as exc:
                    # Audit diagnostics must never change the completed decision.
                    log.warning("read target audit resolution failed: %s", exc)
                    resolved_target = None
            from .redaction import redact_payload

            redacted_requested_target = redact_payload(requested_target)
            if redacted_requested_target is not None:
                redacted_requested_target = str(redacted_requested_target)[
                    :_MAX_REQUESTED_TARGET_LENGTH
                ]
            redacted_resolved_target = redact_payload(resolved_target)
            if redacted_resolved_target is not None:
                redacted_resolved_target = str(redacted_resolved_target)[
                    :_MAX_REQUESTED_TARGET_LENGTH
                ]

            if auth.reason and auth.reason.startswith("ifc_label_blocked:"):
                try:
                    if sink_category is None:
                        raise ValueError("IFC sink category was not supplied by gate")
                    source, scope = _ifc_blocking_source(
                        ifc_labels,
                        auth_context,
                        sink_category,
                    )
                    fields["ifc_source_scope"] = scope
                    if source is not None:
                        resource_id = redact_payload(source.resource_id)
                        fields["ifc_source"] = {
                            "source_kind": source.source_kind,
                            "domain": source.domain,
                            "integrity": source.integrity,
                            "integrity_effect": source.integrity_effect,
                            "resource_id": (
                                str(resource_id)[:_MAX_REQUESTED_TARGET_LENGTH]
                                if resource_id is not None else None
                            ),
                        }
                except Exception as exc:
                    # Diagnostics are best-effort and cannot affect authorization.
                    fields["ifc_source_scope"] = "classification_failed"
                    log.warning("IFC source audit classification failed: %s", exc)

            fields.update({
                # Shadow decisions cover both compatibility bypasses and trusted
                # service-capability grants. Bypasses carry the denial reason
                # that enforcement would apply; capability grants do not.
                "would_block": auth.would_block,
                "target": redacted_resolved_target,
                # Caller input is evidence only. It is never resolved, compared
                # with policy, or fed back into an authorization decision.
                "requested_target": redacted_requested_target,
                "trigger": (
                    getattr(auth_context, "origin_trigger", None)
                    or getattr(auth_context, "trigger", None)
                ),
                "service_principal": service.canonical if service else None,
            })
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                log_event("shadow_tool_decision", **fields)
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
            if ifc_labels is None and auth_context is not None:
                ifc_labels = getattr(auth_context, "ifc_labels", None)
            auth = MCPResourceAdapter.authorize_call(
                tool_name,
                mcp_tool,
                arguments,
                auth_context,
                enforce=enforce,
                ifc_labels=ifc_labels,
            )
            if auth.is_shadow_decision:
                self._emit_shadow_decision(
                    auth, auth_context=auth_context, target=target_channel,
                    requested_target=target_channel,
                    ifc_labels=ifc_labels,
                    sink_category=SinkCategory.EXTERNAL_MCP,
                )
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
                would_block=True,
            )
            if auth.is_shadow_decision:
                self._emit_shadow_decision(
                    auth, auth_context=auth_context, target=target_channel,
                    requested_target=target_channel,
                )
            return auth

        flow_direction = get_tool_flow_direction(tool_name)
        sink_category = get_sink_category(tool_name)
        if ifc_labels is None and auth_context is not None:
            ifc_labels = getattr(auth_context, "ifc_labels", None)
        skill_write = WriteResourceAdapter.authorize_skill_write(
            tool_name,
            target_channel,
            auth_context,
            ifc_labels,
            enforce=enforce,
        )
        if skill_write is not None:
            skill_write.flow_direction = flow_direction
            return skill_write
        catalog = get_operation_catalog()
        preliminary_decision = catalog.get_decision(tool_name, auth_context)
        preliminary_service = (
            get_trusted_service_from_auth_context(auth_context)
            if auth_context is not None
            else None
        )
        repo_pr_action_scope = None
        repo_review_state = None
        repo_review_state_refusal = None
        issue_target = None
        if auth_context is not None:
            if (
                tool_name == "issue_comment"
                and preliminary_service is not None
                and not service_can_invoke_operation(preliminary_service, tool_name)
            ):
                pass
            elif tool_name == "issue_comment":
                repository_sources = tuple(
                    source for source in getattr(ifc_labels, "sources", ())
                    if getattr(source, "domain", None) == "repository"
                )
                if not repository_sources:
                    return ToolAuthorization(
                        tool_name=tool_name,
                        decision=OperationDecision.ADMIN_REQUIRED,
                        allowed=not enforce,
                        reason="issue_repository_source_required",
                        required_tier=AccessTier.ADMIN,
                        enforcement_enabled=enforce,
                        is_shadow_decision=not enforce,
                        would_block=True,
                    )
                from .tools.forge import resolve_issue_comment_target

                try:
                    issue_target = resolve_issue_comment_target(
                        (arguments or {}).get("repository"),
                        (arguments or {}).get("issue"),
                    )
                except ToolException as exc:
                    return ToolAuthorization(
                        tool_name=tool_name,
                        decision=OperationDecision.ADMIN_REQUIRED,
                        allowed=not enforce,
                        reason="issue_destination_resolution_failed",
                        required_tier=AccessTier.ADMIN,
                        enforcement_enabled=enforce,
                        is_shadow_decision=not enforce,
                        would_block=True,
                        refusal_detail=str(exc),
                    )
                repo_pr_action_scope = issue_target
            elif tool_name in _TYPED_REPO_PR_TOOL_ACTIONS:
                tool_arguments = arguments or {}
                discovered = getattr(
                    auth_context, "server_discovered_pr_states", None,
                )
                state = (
                    discovered.resolve(
                        tool_arguments.get("repository"),
                        tool_arguments.get("pull_request"),
                    )
                    if discovered is not None
                    and isinstance(tool_arguments.get("repository"), str)
                    and isinstance(tool_arguments.get("pull_request"), int)
                    else None
                )
                if state is None:
                    registry = getattr(auth_context, "repo_pr_scope_registry", None)
                    state = (
                        registry.resolve(
                            tool_arguments.get("repository"),
                            tool_arguments.get("pull_request"),
                        )
                        if registry is not None and hasattr(registry, "resolve")
                        else None
                    )
                repo_pr_action_scope = (
                    state.action_scope if state is not None else None
                )
            else:
                repo_pr_action_scope = getattr(
                    auth_context, "repo_pr_action_scope", None,
                )
            if tool_name in {"shell_exec", "bash_async"}:
                repo_review_state, repo_review_state_refusal = (
                    resolve_repository_review_state(
                        auth_context,
                        command=target_channel,
                        cwd=(arguments or {}).get("cwd"),
                    )
                )
                if repo_review_state is not None:
                    repo_pr_action_scope = repo_review_state.action_scope
            elif tool_name in {"write_file", "edit_file"}:
                repo_review_state, repo_review_state_refusal = (
                    resolve_repository_review_state(
                        auth_context, path=target_channel,
                    )
                )
                if repo_review_state is not None:
                    repo_pr_action_scope = repo_review_state.action_scope
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
        sink_target = target_channel
        if issue_target is not None:
            sink_target = issue_target.sink_destination
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
        ):
            sink_check = SinkGate.check_sink_flow(
                tool_name,
                sink_target,
                ifc_labels,
                auth_context,
                enforce=enforce,
                repo_review_state=repo_review_state,
                repo_review_state_refusal=repo_review_state_refusal,
                repo_pr_action_scope=repo_pr_action_scope,
            )
            sink_check.repo_pr_action_scope = repo_pr_action_scope
            if not sink_check.allowed and enforce and not preliminary_admin_denied:
                return sink_check
            if sink_check.is_shadow_decision:
                self._emit_shadow_decision(
                    sink_check, auth_context=auth_context, target=sink_target,
                    requested_target=target_channel,
                    ifc_labels=ifc_labels,
                    sink_category=sink_category,
                )

        decision = preliminary_decision
        service_principal = None

        if auth_context is not None:
            service_principal = get_trusted_service_from_auth_context(auth_context)

        required_tier = AccessTier.USER
        reason = None
        is_shadow = False
        would_block = False
        service_allowed = (
            service_can_invoke_operation(service_principal, tool_name)
        )

        if decision == OperationDecision.OPEN:
            allowed = True
            would_block = False
        elif decision == OperationDecision.ADMIN_REQUIRED:
            required_tier = AccessTier.ADMIN
            if auth_context and "admin" in (getattr(auth_context, "roles", ()) or ()):
                allowed = True
                would_block = False
            elif service_allowed:
                allowed = True
                is_shadow = not enforce
                would_block = False
            elif enforce:
                allowed = False
                reason = "admin_required"
                would_block = True
            else:
                allowed = True
                reason = "admin_required"
                is_shadow = True
                would_block = True
        elif decision == OperationDecision.RESOURCE_SCOPED:
            if tool_name in _TYPED_REPO_PR_TOOL_ACTIONS:
                forge_auth = authorize_repo_pr_tool(
                    tool_name,
                    repo_pr_action_scope,
                    service_principal=service_principal,
                    enforce=enforce,
                    flow_direction=flow_direction,
                )
                if forge_auth.is_shadow_decision:
                    self._emit_shadow_decision(
                        forge_auth, auth_context=auth_context, target=None,
                        requested_target=None,
                    )
                return forge_auth
            if tool_name in ChannelResourceAdapter._CHANNEL_OPERATIONS:
                channel_auth = ChannelResourceAdapter.authorize_channel_operation(
                    tool_name,
                    target_channel,
                    auth_context,
                    enforce=enforce,
                )
                channel_auth.flow_direction = flow_direction
                return channel_auth
            if tool_name in WriteResourceAdapter._RESOURCE_OPERATIONS:
                write_auth = WriteResourceAdapter.authorize_operation(
                    tool_name,
                    sink_target,
                    auth_context,
                    enforce=enforce,
                    service_allowed=service_allowed,
                )
                write_auth.flow_direction = flow_direction
                write_auth.repo_pr_action_scope = repo_pr_action_scope
                if write_auth.is_shadow_decision:
                    self._emit_shadow_decision(
                        write_auth, auth_context=auth_context, target=sink_target,
                        requested_target=target_channel,
                    )
                return write_auth
            if tool_name in READ_RESOURCE_OPERATIONS:
                if auth_context and "admin" in (getattr(auth_context, "roles", ()) or ()):
                    allowed = True
                elif (
                    service_principal is not None
                    and tool_name in {
                        "read_file", "aread", "ls", "als", "glob", "aglob",
                        "grep", "agrep",
                    }
                ):
                    scoped_read_allowed = _trigger_service_read_target_is_allowed(
                        service_principal, tool_name, arguments,
                        auth_context=auth_context,
                    )
                    resolved_read_target = resolved_read_target_from_arguments(
                        tool_name, arguments,
                    )
                    from .read_policy import is_memory_read_path

                    targets_memory = (
                        resolved_read_target is not None
                        and is_memory_read_path(Path(resolved_read_target))
                    )
                    home = os.environ.get("MIMIR_HOME", "").strip()
                    scratch_root = (
                        (Path(home).resolve() / "scratch").resolve()
                        if home else None
                    )
                    resolved_target_path = (
                        Path(resolved_read_target).resolve(strict=False)
                        if resolved_read_target is not None else None
                    )
                    targets_scratch = (
                        scratch_root is not None
                        and resolved_target_path is not None
                        and (
                            resolved_target_path == scratch_root
                            or resolved_target_path.is_relative_to(scratch_root)
                        )
                    )
                    allowed = scoped_read_allowed or (
                        service_allowed
                        and not targets_memory
                        and not targets_scratch
                        and not (
                            service_principal.canonical == "system"
                            and service_principal.trigger == "upgrade"
                        )
                    )
                elif service_allowed:
                    allowed = True
                else:
                    allowed = (
                        auth_context is not None
                        and read_target_from_arguments(tool_name, arguments) is not None
                    )
                required_tier = AccessTier.USER if allowed else AccessTier.ADMIN
                if not allowed:
                    reason = "read_scope"
                    would_block = True
                    if not enforce:
                        allowed = True
                        is_shadow = True
            else:
                required_tier = AccessTier.ADMIN
                if enforce:
                    allowed = False
                    reason = "resource_scoped"
                    would_block = True
                else:
                    allowed = True
                    reason = "resource_scoped"
                    is_shadow = True
                    would_block = True
        else:
            # Explicit service capabilities are authoritative even if a newly
            # added operation has not reached the catalog yet. This is a narrow
            # exception to UNKNOWN's ordinary fail-closed rule: capabilities
            # are fixed per trusted service principal, not inferred from the
            # runtime inventory or supplied by the caller.
            if service_allowed:
                allowed = True
                would_block = False
            elif enforce:
                allowed = False
                reason = "unknown_operation"
                would_block = True
            else:
                allowed = True
                reason = "unknown_operation"
                is_shadow = True
                would_block = True

        auth = ToolAuthorization(
            tool_name=tool_name,
            decision=decision,
            allowed=allowed,
            reason=reason,
            service_principal=service_principal,
            required_tier=required_tier,
            enforcement_enabled=enforce,
            is_shadow_decision=is_shadow,
            would_block=would_block,
            resolved_sink_target=(
                resolve_sink_target(
                    tool_name, sink_category, sink_target, service_principal,
                )
                if sink_target is not None else None
            ),
            flow_direction=flow_direction,
            repo_pr_action_scope=repo_pr_action_scope,
        )

        if is_shadow:
            requested_target = (
                requested_read_target_from_arguments(tool_name, arguments)
                if reason == "read_scope"
                else target_channel
            )
            self._emit_shadow_decision(
                auth, auth_context=auth_context, target=sink_target,
                requested_target=requested_target, arguments=arguments,
            )

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
    "pr_metadata": "repository",
    "pr_files": "repository",
    "pr_diff": "repository",
    "pr_checks": "repository",
    "pr_reviews": "repository",
    "pr_comments": "repository",
    "pr_review_requests": "repository",
    "repo_checkout": "repository",
    "repo_fetch": "repository",
    "repo_status": "repository",
    "repo_test": "repository",
    "repo_diff": "repository",
    "repo_unmerged": "repository",
}

# These BOTH tools return only server-created metadata inline. Their external
# content remains behind a separately classified read boundary.
_METADATA_ONLY_RESULT_TOOLS = frozenset({"bash_async", "fetch_url"})

# Independent semantic inventory for tools whose results come from a read
# backend. Startup rejects drift toward SINK/NEITHER before it can suppress
# result taint. MCP reads have equivalent adapter/resource parity checks in
# MCPResourceAdapter.authorize_call.
_READ_BACKEND_RESULT_TOOLS = frozenset({
    "Read",
    "Glob",
    "Grep",
    "read_file",
    "aread",
    "ls",
    "als",
    "glob",
    "aglob",
    "grep",
    "agrep",
    "fetch_url",
    "pr_metadata",
    "pr_files",
    "pr_diff",
    "pr_checks",
    "pr_reviews",
    "pr_comments",
    "pr_review_requests",
    "repo_checkout",
    "repo_fetch",
    "repo_status",
    "repo_test",
    "repo_diff",
    "repo_unmerged",
})


@dataclass(frozen=True)
class ProtectedResultProvenance:
    """Non-model-visible provenance for the exact resources a native read returned."""

    sources: tuple["SourceLabel", ...]


_protected_result_provenance: ContextVar[ProtectedResultProvenance | None] = ContextVar(
    "protected_result_provenance", default=None,
)


def begin_protected_result_capture() -> Token[ProtectedResultProvenance | None]:
    """Start an isolated result-provenance capture around one tool execution."""
    return _protected_result_provenance.set(None)


def publish_protected_result(sources: tuple["SourceLabel", ...]) -> None:
    """Publish exact server-derived sources, including an authoritative empty set."""
    from .models import SourceLabel

    if not isinstance(sources, tuple) or not all(
        isinstance(source, SourceLabel) for source in sources
    ):
        raise TypeError("protected result provenance must be a tuple of SourceLabel")
    _protected_result_provenance.set(ProtectedResultProvenance(sources))


def end_protected_result_capture(
    token: Token[ProtectedResultProvenance | None],
) -> ProtectedResultProvenance | None:
    """Return the captured provenance and restore any enclosing capture."""
    captured = _protected_result_provenance.get()
    _protected_result_provenance.reset(token)
    return captured


def protected_result_source(
    auth_context: "AuthContext | None",
    *,
    principal: str | None,
    domain: str,
    resource_id: str | None,
    bridge_instance: str,
    sensitivity: str = "internal",
) -> "SourceLabel":
    """Build a result source from a resource owner and the exact authorized reader."""
    from .models import SourceLabel

    requester = getattr(auth_context, "canonical_principal", None)
    if getattr(auth_context, "is_service", False) and requester:
        requester = f"service:{requester}"
    acl = {principal} if principal else set()
    if requester:
        acl.add(requester)
    integrity = "untrusted"
    integrity_effect = "active_ingest"
    if domain == "filesystem" and isinstance(resource_id, str):
        integrity, integrity_effect = _filesystem_result_integrity(
            auth_context, resource_id,
        )
    return SourceLabel(
        principal=principal,
        domain=domain,
        resource_id=resource_id,
        bridge_instance=bridge_instance,
        sensitivity=sensitivity,
        authorized_principals=frozenset(acl),
        source_kind="protected_tool",
        integrity=integrity,
        integrity_effect=integrity_effect,
    )


def _filesystem_result_integrity(
    auth_context: "AuthContext | None",
    resource_id: str,
) -> tuple[str, str]:
    """Derive file trust only from resolved framework-owned paths and metadata."""
    home_value = os.environ.get("MIMIR_HOME", "").strip()
    if not home_value:
        return "untrusted", "active_ingest"
    try:
        home = Path(home_value).resolve(strict=True)
        resource = Path(resource_id).resolve(strict=True)
        relative = resource.relative_to(home)
    except (OSError, RuntimeError, ValueError):
        return "untrusted", "active_ingest"

    if relative.parts and relative.parts[0] in {"memory", "state"}:
        # Poller subprocesses write this tree directly, outside the protected
        # tool boundary, and may persist attacker-derived cursor/event fields.
        # A path under state/pollers is therefore not proof of self-authorship.
        if relative.parts[0:2] == ("state", "pollers"):
            return "untrusted", "active_ingest"
        persisted = _persisted_file_integrity(home, relative)
        return persisted, (
            "informational" if persisted == "trusted" else "active_ingest"
        )

    cache_root = home / "attachments" / "fetch-cache"
    try:
        resource.relative_to(cache_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return "untrusted", "active_ingest"

    sidecar = resource.with_name(f"{resource.name}.meta.json")
    try:
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "untrusted", "active_ingest"
    url = metadata.get("url") if isinstance(metadata, dict) else None
    file_path = metadata.get("file_path") if isinstance(metadata, dict) else None
    if not isinstance(url, str) or not isinstance(file_path, str):
        return "untrusted", "active_ingest"
    expected_digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    if not resource.name.startswith(f"{expected_digest}-"):
        return "untrusted", "active_ingest"
    try:
        recorded_resource = (home / file_path.lstrip("/")).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return "untrusted", "active_ingest"
    if recorded_resource != resource:
        return "untrusted", "active_ingest"
    # URL approval authorizes GET egress only. It never vouches for returned
    # bytes, including redirects or cached copies (#1139).
    return "untrusted", "active_ingest"


def _persisted_file_integrity(home: Path, relative: Path) -> str:
    """Return server-recorded integrity for a mutable self-authored file."""
    metadata_path = home / ".mimir" / "file-integrity.json"
    if not metadata_path.exists():
        return "trusted"
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "untrusted"
    if not isinstance(payload, dict):
        return "untrusted"
    value = payload.get(relative.as_posix())
    if value is None:
        return "trusted"
    return "trusted" if value == "trusted" else "untrusted"


def record_file_write_integrity(
    resource_id: str | None,
    labels: "InformationFlowLabels | None",
) -> bool:
    """Persist least-trust provenance before a model file write.

    Metadata lives under the protected ``.mimir`` root, so a later model turn
    cannot erase an untrusted mark. The destination chooses only which entry is
    updated; integrity comes exclusively from the server-owned live carrier.
    """
    home_value = os.environ.get("MIMIR_HOME", "").strip()
    if not home_value or not isinstance(resource_id, str) or not resource_id:
        return True
    home = Path(home_value).resolve(strict=False)
    requested = Path(resource_id)
    if requested.is_absolute():
        try:
            requested.relative_to(home)
            resource = requested
        except ValueError:
            if requested.parts[1:2] in {("memory",), ("state",)}:
                resource = home / requested.as_posix().lstrip("/")
            else:
                return True
    else:
        resource = home / requested
    try:
        resource = resource.resolve(strict=False)
        relative = resource.relative_to(home)
    except (OSError, RuntimeError, ValueError):
        return False
    if not relative.parts or relative.parts[0] not in {"memory", "state"}:
        return True
    if relative.parts[0:2] == ("state", "pollers"):
        return True
    integrity = "untrusted"
    sources = getattr(labels, "sources", ())
    if sources and all(source.integrity == "trusted" for source in sources):
        integrity = "trusted"

    metadata_path = home / ".mimir" / "file-integrity.json"
    with _persisted_file_integrity_lock:
        try:
            payload = (
                json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata_path.exists()
                else {}
            )
            if not isinstance(payload, dict):
                return False
            payload[relative.as_posix()] = integrity
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = metadata_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            tmp.replace(metadata_path)
            return True
        except (OSError, json.JSONDecodeError):
            log.exception("failed to persist file integrity for %s", relative)
            return False


def _incomplete_protected_result(
    domain: str,
    arguments: dict[str, Any],
) -> "InformationFlowLabels":
    from .models import InformationFlowLabels, SourceLabel

    resource = next(
        (
            arguments.get(key)
            for key in ("path", "file_path", "query", "turn_id", "atom_id", "job_id")
            if isinstance(arguments.get(key), str) and arguments.get(key)
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
        integrity="untrusted",
        integrity_effect="active_ingest",
    ))


def classify_protected_result(
    tool_name: str,
    arguments: dict[str, Any] | None,
    auth_context: "AuthContext | None",
    authorization: ToolAuthorization,
    *,
    result: Any = None,
    provenance: ProtectedResultProvenance | None = None,
    failed: bool = False,
) -> "InformationFlowLabels | None":
    """Return server-authoritative labels for content a protected call may expose.

    The contract is based only on the authorized operation and validated
    arguments. Tool success text, model assertions, and error wording cannot
    downgrade it. Unknown provenance is intentionally incomplete and therefore
    fails closed at every egress gate.
    """
    from .models import InformationFlowLabels, SourceLabel

    args = arguments or {}
    if tool_name in {
        "pr_metadata", "pr_files", "pr_diff", "pr_checks", "pr_reviews",
        "pr_comments", "pr_review_requests", "repo_checkout", "repo_fetch",
        "repo_status", "repo_test", "repo_diff", "repo_unmerged",
    }:
        scope = authorization.repo_pr_action_scope
        if scope is None or failed:
            return _incomplete_protected_result("repository", args)
        principal = getattr(auth_context, "canonical_principal", None)
        if getattr(auth_context, "is_service", False) and principal:
            principal = f"service:{principal}"
        source = SourceLabel(
            principal=principal,
            domain="repository",
            resource_id=(
                f"{scope.canonical_repo}#pull/{scope.pr_number}"
                f"@{scope.observed_head_sha}"
            ),
            bridge_instance="forge",
            sensitivity="internal",
            authorized_principals=(
                frozenset({principal}) if principal else frozenset()
            ),
            source_kind="protected_tool",
            integrity="untrusted",
            # The immutable authority record already selected this exact PR.
            # Preserve its confidentiality label without deadlocking the next
            # scope-bound edit/review operation as fresh active ingress.
            integrity_effect="informational",
        )
        labels = InformationFlowLabels().with_source(source)
        channel = getattr(auth_context, "channel_id", None)
        return labels.with_channel(channel) if channel else labels

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
            integrity="untrusted",
            integrity_effect="active_ingest",
        )
        return InformationFlowLabels().with_source(source)

    if tool_name.startswith(MCPResourceAdapter._MCP_TOOL_PREFIX):
        resources = authorization.protected_source_resources
        if resources == ():
            return None
        principal = getattr(auth_context, "canonical_principal", None)
        labels = InformationFlowLabels()
        integrity = (
            "trusted"
            if not failed and authorization.result_integrity == "trusted"
            else "untrusted"
        )
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
                integrity=integrity,
                integrity_effect="active_ingest",
            ))
        return labels

    if (
        tool_name in {"shell_exec", "bash_async"}
        and getattr(auth_context, "repo_review_state", None) is not None
        and not failed
    ):
        # A review turn necessarily needs several shell steps. Preserve the
        # output's untrusted integrity without treating each authorized command
        # as a new active external ingest that would deadlock the next step.
        principal = getattr(auth_context, "canonical_principal", None)
        if principal:
            principal = f"service:{principal}"
        labels = InformationFlowLabels().with_source(SourceLabel(
            principal=principal,
            domain="shell",
            resource_id="repo_review",
            bridge_instance=getattr(auth_context, "bridge_instance", None),
            sensitivity="internal",
            authorized_principals=(
                frozenset({principal}) if principal else frozenset()
            ),
            source_kind="protected_tool",
            integrity="untrusted",
            integrity_effect="informational",
        ))
        channel = getattr(auth_context, "channel_id", None)
        return labels.with_channel(channel) if channel else labels

    artifact = getattr(result, "artifact", None)
    if provenance is None and isinstance(artifact, ProtectedResultProvenance):
        provenance = artifact

    domain = _PROTECTED_RESULT_DOMAINS.get(tool_name)
    if domain is None:
        # Native aliases may be namespaced by a tool server. Do not apply this
        # suffix rule to MCP calls, which are classified above from provenance.
        for candidate, candidate_domain in _PROTECTED_RESULT_DOMAINS.items():
            if tool_name.endswith(f"__{candidate}"):
                domain = candidate_domain
                break
    if domain is None:
        if provenance is not None:
            if not provenance.sources:
                return None
            labels = InformationFlowLabels()
            for source in provenance.sources:
                labels = labels.with_source(source)
            return labels

        metadata_only = tool_name in _METADATA_ONLY_RESULT_TOOLS
        flow_direction = authorization.flow_direction
        if flow_direction is ToolFlowDirection.UNKNOWN:
            flow_direction = get_tool_flow_direction(tool_name)
        if metadata_only or flow_direction not in {
            ToolFlowDirection.SOURCE,
            ToolFlowDirection.BOTH,
        }:
            return None
        # An ingesting native tool without a confidentiality domain still
        # introduces model-visible content. Unknown provenance must taint the
        # turn rather than silently laundering integrity through the tool.
        domain = "unknown"

    if failed:
        return _incomplete_protected_result(domain, args)

    if provenance is not None:
        if not provenance.sources:
            return None
        labels = InformationFlowLabels()
        for source in provenance.sources:
            labels = labels.with_source(source)
        return labels

    return _incomplete_protected_result(domain, args)


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
                "list_schedules",
            ),
            readable_domains=(
                "configured_inputs",
                "filesystem",
                "turn_history",
                "shell_jobs",
                "schedule_metadata",
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
                ServiceSinkPolicy(
                    "write_file", "static_service_write_roots",
                    "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS",
                ),
                ServiceSinkPolicy(
                    "edit_file", "static_service_write_roots",
                    "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS",
                ),
                ServiceSinkPolicy("shell_exec", "shell_profile", "maintenance"),
                ServiceSinkPolicy("bash_async", "shell_profile", "maintenance"),
                ServiceSinkPolicy("spawn_open_code", "spawn_workspace", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
                ServiceSinkPolicy("worklink_run", "worklink_repo", "WORKLINK_REPO/MIMIR_WORKLINK_REPO"),
            ),
            saga_full_corpus_read=True,
            creation_path="mimir.scheduler.Scheduler._fire_job",
        ),
        ServicePrincipal(
            canonical="synthesis",
            trigger="saga_session_end",
            capabilities=tuple(sorted(TRIGGER_AUTHORITY_PROFILES["session-boundary"])),
            readable_domains=(
                "session", "saga", "filesystem", "turn_history", "shell_jobs",
                "repository",
            ),
            sink_destinations=("filesystem", "session_boundary", "saga"),
            sink_policies=(
                ServiceSinkPolicy(
                    "write_file", "static_service_write_roots",
                    "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS",
                ),
                ServiceSinkPolicy(
                    "edit_file", "static_service_write_roots",
                    "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS",
                ),
            ),
            saga_full_corpus_read=True,
            creation_path="mimir.server._on_session_idle",
            authority_profile="session-boundary",
            capability_tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
        ),
        ServicePrincipal(
            canonical="system",
            trigger="upgrade",
            capabilities=(
                "shell_exec",
                "bash_async",
                "bash_jobs_list",
                "bash_job_output",
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
                "shell_jobs",
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
                ServiceSinkPolicy(
                    "write_file", "upgrade_proposals",
                    "MIMIR_HOME/scratch/proposals",
                ),
                ServiceSinkPolicy(
                    "edit_file", "upgrade_proposals",
                    "MIMIR_HOME/scratch/proposals",
                ),
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
    **{
        operation: "repository"
        for operation, direction in _TOOL_FLOW_MAP.items()
        if operation in _TYPED_REPO_PR_TOOL_ACTIONS
        and direction in {ToolFlowDirection.SOURCE, ToolFlowDirection.BOTH}
    },
}

_OPERATION_SINK_DESTINATION: dict[str, str] = {
    "write_file": "filesystem",
    "edit_file": "filesystem",
    "shell_exec": "shell_process",
    "bash_async": "shell_process",
    "spawn_open_code": "spawn_process",
    "open_proposal": "proposal",
    "submit_proposal": "proposal",
    "abandon_proposal": "proposal",
    "add_schedule": "scheduler",
    "set_schedule_priority": "scheduler",
    "remove_schedule": "scheduler",
    "set_poller_overrides": "scheduler",
    "reload_pollers": "scheduler",
    "commitment_complete": "commitments",
    "commitment_snooze": "commitments",
    "commitment_dismiss": "commitments",
    "defer_injected_message": "injected_messages",
    "rebuild_index": "filesystem",
    "request_mimir_update": "filesystem",
    "saga_feedback": "saga",
    "saga_mark_contributions": "saga",
    "saga_record_skill_learning": "saga",
    "saga_forget": "saga",
    "memory_store": "saga",
    "send_message": "message",
    "saga_end_session": "session_boundary",
    "worklink_run": "worklink",
    "react": "message",
    "web_search": "network",
    "fetch_url": "network",
    "post_message": "message",
    "webhook": "network",
    "http_request": "network",
    "ntfy_send": "notification",
    "download_files": "filesystem",
    "adownload_files": "filesystem",
    "Bash": "shell_process",
    "bash": "shell_process",
    "bash_exec": "shell_process",
    "execute": "shell_process",
    "aexecute": "shell_process",
    "shell": "shell_process",
    "Write": "filesystem",
    "Edit": "filesystem",
    "harness_auto_deliver": "message",
    "harness_resend_nudge": "message",
    "activity_panel_post": "message",
    "activity_panel_edit": "message",
    "pr_submit_review": "bound_pull_request",
    "pr_inline_review_comment": "bound_pull_request",
    "pr_comment": "bound_pull_request",
    "issue_comment": "configured_repository_issue",
    "pr_rerequest_review": "bound_pull_request",
    "unsupported_operation": "bound_pull_request",
    "repo_checkout": "bound_pull_request",
    "repo_cleanup": "bound_pull_request",
    "repo_fetch": "bound_pull_request",
    "repo_test": "bound_pull_request",
    "repo_stage": "bound_pull_request",
    "repo_commit": "bound_pull_request",
    "repo_merge": "bound_pull_request",
    "repo_merge_abort": "bound_pull_request",
    "repo_rebase": "bound_pull_request",
    "repo_rebase_abort": "bound_pull_request",
    "repo_revert": "bound_pull_request",
    "repo_revert_abort": "bound_pull_request",
    "repo_push": "bound_pull_request",
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


def _capability_matrix_errors() -> list[str]:
    errors: list[str] = []
    for operation, direction in sorted(_TOOL_FLOW_MAP.items()):
        if direction not in {ToolFlowDirection.SINK, ToolFlowDirection.BOTH}:
            continue
        if get_sink_category(operation) is SinkCategory.UNKNOWN:
            errors.append(
                f"IFC {direction.value} operation '{operation}' has no sink category"
            )
        if operation not in _OPERATION_SINK_DESTINATION:
            errors.append(
                f"IFC {direction.value} operation '{operation}' has no destination extraction"
            )
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
    for trigger in sorted(
        _REQUIRED_SERVICE_PRINCIPALS - _TRUSTED_SERVICE_PRINCIPALS.keys()
    ):
        errors.append(f"Missing service principal for trigger: {trigger}")
    for trigger, principal in sorted(_TRUSTED_SERVICE_PRINCIPALS.items()):
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

        capability_set = set(principal.capabilities)
        missing = _missing_capability_companions(capability_set)
        if missing:
            errors.append(
                f"Service principal '{principal.canonical}' ({trigger}) has "
                f"capabilities without companions: {', '.join(sorted(missing))}"
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
            } and operation not in policies_by_operation and (
                operation not in _FIXED_SERVICE_SINK_OPERATIONS
            ):
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
        from .tools.budget_gate import _emit_hard_boundary_denied

        _emit_hard_boundary_denied(
            tool="startup",
            boundary="capability_matrix_preflight",
            reason="capability_matrix_incomplete",
            target=errors,
        )
        return (False, errors)
    return (True, [])


def assert_capability_matrix_complete() -> None:
    """Raise unless the enforcement matrix is complete and consistent."""
    errors = _capability_matrix_errors()
    if errors:
        from .tools.budget_gate import _emit_hard_boundary_denied

        _emit_hard_boundary_denied(
            tool="startup",
            boundary="capability_matrix_preflight",
            reason="capability_matrix_incomplete",
            target=errors,
        )
        raise CapabilityMatrixError(
            "Access-control enforcement blocked by incomplete capability matrix: "
            + "; ".join(errors)
        )


def _deepagents_builtin_tool_names() -> tuple[str, ...]:
    """Return the tools injected by the DeepAgents middleware stack."""
    from deepagents.backends import StateBackend
    from deepagents.middleware import SubAgentMiddleware
    from langchain.agents.middleware import TodoListMiddleware
    from langchain_core.runnables import RunnableLambda

    from .readonly_backend import MimirFilesystemMiddleware

    backend = StateBackend()
    middleware = (
        TodoListMiddleware(),
        MimirFilesystemMiddleware(backend=backend),
        SubAgentMiddleware(
            backend=backend,
            subagents=[{
                "name": "inventory-assertion",
                "description": "Inventory assertion placeholder.",
                "runnable": RunnableLambda(lambda state: state),
            }],
        ),
    )
    return tuple(tool.name for item in middleware for tool in item.tools)


def assert_model_tool_inventory_cataloged(
    *,
    model_spec: str | None = None,
    coding_enabled: bool = False,
) -> None:
    """Raise if the assembled model surface lacks authz or IFC metadata."""
    from .tools.registry import all_mimir_tools

    catalog = get_operation_catalog()
    mimir_tools = all_mimir_tools(
        model_spec=model_spec,
        coding_enabled=coding_enabled,
        require_coding_available=False,
    )
    tool_names = {
        *(tool.name for tool in mimir_tools),
        *_deepagents_builtin_tool_names(),
    }
    unknown_tools = sorted({
        tool_name for tool_name in tool_names
        if catalog.get_decision(tool_name) == OperationDecision.UNKNOWN
    })
    unknown_flows = sorted({
        tool_name for tool_name in tool_names
        if get_tool_flow_direction(tool_name) == ToolFlowDirection.UNKNOWN
    })
    incomplete_sinks = sorted({
        tool_name for tool_name in tool_names
        if get_tool_flow_direction(tool_name) in {
            ToolFlowDirection.SINK, ToolFlowDirection.BOTH,
        }
        and (
            get_sink_category(tool_name) == SinkCategory.UNKNOWN
            or tool_name not in _OPERATION_SINK_DESTINATION
        )
    })
    misclassified_read_backends = sorted({
        tool_name for tool_name in tool_names & _READ_BACKEND_RESULT_TOOLS
        if get_tool_flow_direction(tool_name) not in {
            ToolFlowDirection.SOURCE, ToolFlowDirection.BOTH,
        }
    })
    errors: list[str] = []
    if unknown_tools:
        errors.append("UNKNOWN model-bound tools: " + ", ".join(unknown_tools))
    if unknown_flows:
        errors.append("model-bound tools without explicit IFC flow metadata: " + ", ".join(unknown_flows))
    if incomplete_sinks:
        errors.append("model-bound IFC sinks without category/destination extraction: " + ", ".join(incomplete_sinks))
    if misclassified_read_backends:
        errors.append(
            "read-backend tools must be IFC SOURCE/BOTH: "
            + ", ".join(misclassified_read_backends)
        )
    if errors:
        raise CapabilityMatrixError(
            "Access-control enforcement blocked by incomplete model tool inventory: "
            + "; ".join(errors)
        )


def resolve_access_control_enforcement(
    requested: bool,
    *,
    model_spec: str | None = None,
    coding_enabled: bool = False,
) -> bool:
    """Fail closed at the enforcement enablement boundary."""
    if requested:
        assert_capability_matrix_complete()
        assert_model_tool_inventory_cataloged(
            model_spec=model_spec,
            coding_enabled=coding_enabled,
        )
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
            "filesystem_read_roots": list(principal.filesystem_read_roots),
            "saga_full_corpus_read": principal.saga_full_corpus_read,
            "creation_path": principal.creation_path,
        }
    return report


def get_service_principal(trigger: str) -> ServicePrincipal | None:
    """Get a service principal by trigger."""
    return _TRUSTED_SERVICE_PRINCIPALS.get(trigger)


def get_event_service_principal(event: Any) -> ServicePrincipal | None:
    """Resolve static built-ins or an exact per-instance event authority."""
    carried = getattr(event, "service_authority", None)
    if (
        isinstance(carried, ServicePrincipal)
        and carried.trigger == getattr(event, "trigger", None)
        and carried.canonical == getattr(event, "service_principal", None)
    ):
        return carried
    return _TRUSTED_SERVICE_PRINCIPALS.get(getattr(event, "trigger", None))


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
    service = getattr(auth_context, "service_authority", None)
    if not isinstance(service, ServicePrincipal):
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
    from .models import (
        AuthContext, RepoPRActionScope, RepoPRScopeRegistry, RepoReviewState,
        TurnInteractivity,
    )

    author = event.author
    canonical = author
    roles: tuple[str, ...] = ()
    is_service = False

    if author is not None and resolver is not None:
        canonical = resolver.resolve(author)
        access = resolver.access_metadata(author)
        roles = access.roles
        is_service = access.is_service

    registered_service = get_event_service_principal(event)
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

    repo_pr_scope_registry = _repo_review_state_from_event(event, registered_service)
    carried_scope = event.repo_pr_action_scope
    if not (
        isinstance(carried_scope, RepoPRActionScope)
        and carried_scope.provenance == "server_discovered"
        and registered_service is not None
        and registered_service.authority_profile in _PR_REVIEW_SCOPE_AUTHORITY_PROFILES
        and event.trigger == "scheduled_tick"
        and event.service_principal == registered_service.canonical
        and event_ingress is None
        and extra.get(HTTP_EVENT_INGRESS_EXTRA_KEY) is None
    ):
        carried_scope = None
    if repo_pr_scope_registry is None and carried_scope is not None:
        repo_pr_scope_registry = RepoPRScopeRegistry((RepoReviewState(carried_scope),))
    single_state = (
        repo_pr_scope_registry.review_states[0]
        if repo_pr_scope_registry is not None
        and len(repo_pr_scope_registry.review_states) == 1
        else None
    )
    action_scope = single_state.action_scope if single_state is not None else None

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
        service_authority=registered_service if is_service else None,
        repo_pr_scope_registry=repo_pr_scope_registry,
        repo_review_state=single_state,
        repo_pr_action_scope=action_scope,
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
        origin_trigger=(
            f"{registered_service.authority_profile}-poller:{extra.get('poller_name')}"
            if registered_service is not None
            and event.trigger == "poller"
            and registered_service.authority_profile
            and isinstance(extra.get("poller_name"), str)
            and extra.get("poller_name")
            else event.trigger
        ),
        origin_ref=event.source_id,
    )
