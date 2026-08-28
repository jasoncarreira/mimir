"""Per-turn tool-call budget enforcement.

The SDK build gated tool calls via a ``PreToolUse`` HookMatcher that
checked ``TurnContext.tool_call_count`` against
``ctx.tool_call_budget`` before allowing each invocation. The hook ran
on EVERY tool call, including the SDK's built-in tools (read/write/bash).

Post-181 the deepagents agent has a langchain ``AgentMiddleware`` layer
that intercepts every tool invocation via ``wrap_tool_call`` /
``awrap_tool_call``. That's the right level — built-ins included.

Prior implementation (replaced 2026-05-23): we monkey-patched each
mimir tool's ``coroutine``/``func`` via ``apply_budget_gate`` and
added the list to ``create_deep_agent(tools=...)``. That missed
deepagents' built-in tools (``shell_exec``, ``read_file``,
``write_file``, ``glob``, ``edit_file``, ``write_todos``) which are
added by deepagents internally and never went through the mimir
tools list. Production heartbeats hit 142 tool_calls vs a budget of
120 with zero budget events firing — the gap that motivated this
rewrite.

Soft + hard semantics (unchanged):

* Below ``soft_threshold = max(1, int(budget * 0.75))``: silent.
* At soft threshold: log a one-time-per-turn
  ``tool_call_budget_soft_warning`` event. The tool still runs.
* At or above ``hard_threshold = budget``: refuse the call,
  return a ``ToolMessage`` with the denial text, emit
  ``tool_call_budget_denied``.

A ``budget`` of 0 disables enforcement entirely (matches the SDK
contract — operators set ``MIMIR_TOOL_CALL_BUDGET=0`` for benchmarks
that need uncapped exploration).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Awaitable, Callable, Union, get_args, get_origin

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from langgraph.types import Command

from ..models import AuthContext
from .refusals import ToolPolicyRefusal
from ..worklink.continuation import HTTP_EVENT_INGRESS_EXTRA_VALUE
from ..access_control import (
    OperationDecision,
    OPERATOR_SHELL_PROFILE,
    OperatorShellBinding,
    SinkCategory,
    ToolAuthorization,
    approve_live_declassification,
    approved_fetch_urls,
    fetch_url_is_approved,
    classify_protected_result,
    configured_project_test_cwd,
    get_tool_registry,
    get_trusted_service_from_auth_context,
    normalize_sink_destination,
    parse_service_shell_argv_with_diagnostics,
    resolve_repository_review_state,
    ServicePrincipal,
    ServiceShellBindingRule,
    _OPERATOR_SHELL_BINDING_ISSUER,
    _issue_operator_shell_binding,
    _live_untrusted_active_ingest,
    _operator_can_invoke_admin_shell,
    _operator_git_execution_argv_with_diagnostics,
    _operator_read_execution_argv_with_diagnostics,
    _project_test_execution_argv,
    _resolve_operator_bounded_cwd,
    _validated_operator_shell_argv_artifact,
    service_filesystem_read_roots,
    service_shell_argv_for_log,
)
from .prohibited_action_guard import check_prohibited_bash, is_bash_tool
from .web_search_destination import web_search_url

log = logging.getLogger(__name__)

_STANDING_REVIEW_TOOLS = frozenset({
    "pr_metadata", "pr_files", "pr_diff", "pr_checks", "pr_reviews",
    "pr_comments", "pr_review_requests", "pr_submit_review",
    "pr_inline_review_comment", "pr_comment", "pr_rerequest_review",
    "repo_checkout", "repo_cleanup", "repo_fetch", "repo_status", "repo_test",
    "repo_diff", "repo_unmerged",
})
_PULL_REQUEST_TOOLS = _STANDING_REVIEW_TOOLS | frozenset({
    "unsupported_operation", "repo_stage", "repo_commit", "repo_merge",
    "repo_merge_abort", "repo_rebase", "repo_rebase_abort", "repo_revert",
    "repo_revert_abort", "repo_push",
})
_TOOL_EVENT_ARGUMENT_ALLOWLIST = (
    "command",
    "path",
    "file_path",
    "paths",
    "pattern",
    "channel_id",
    "repository",
    "pull_request",
    "session_id",
    "job_id",
)
_TOOL_EVENT_ARGUMENT_VALUE_LIMIT = 200
_TOOL_EVENT_ERROR_LIMIT = 500
_TOOL_EVENT_ELISION = "...[truncated]..."
_GIT_OPERATION_RESULT_TOOLS = frozenset({
    "repo_fetch", "repo_status", "repo_diff", "repo_unmerged", "repo_stage",
    "repo_commit", "repo_merge", "repo_merge_abort", "repo_rebase",
    "repo_rebase_abort", "repo_revert", "repo_revert_abort", "repo_push",
})
_GIT_OPERATION_RESULT_FIELDS = frozenset({"ok", "code", "stdout", "stderr"})
_PROJECT_TEST_RESULT_FIELDS = frozenset({
    "ok", "code", "returncode", "stdout", "stderr", "command",
    "command_source", "output_limited", "stdout_dropped_bytes",
    "stderr_dropped_bytes", "git_context",
})
_SPAWN_OPEN_CODE_RESULT_FIELDS = frozenset({
    "run_id", "status", "exit_code", "stdout", "result", "stderr",
    "artifact_dir", "name", "proposal",
})
_SPAWN_OPEN_CODE_ERROR_STATUSES = frozenset({
    "artifact_unavailable", "configuration_refused", "authentication_refused",
    "prompt_refused", "containment_unavailable", "timeout", "output_overflow",
    "authentication_required", "failed", "proposal_unavailable",
})
_REMEDIATION_EFFECT_TOOLS = frozenset({
    "repo_commit", "repo_push", "pr_comment", "pr_inline_review_comment",
    "pr_rerequest_review",
})


def _resolve_standing_review(
    tool_name: str,
    auth_context: AuthContext | None,
    arguments: Mapping[str, Any] | None,
) -> str | None:
    """Resolve safe review authority before resource and IFC authorization."""
    if (
        tool_name not in _STANDING_REVIEW_TOOLS
        or auth_context is None
        or not isinstance(arguments, Mapping)
    ):
        return None
    from .forge import (
        revalidate_review_head_for_context,
        resolve_review_state_for_context,
    )

    try:
        resolve_review_state_for_context(
            auth_context, arguments.get("repository"), arguments.get("pull_request"),
        )
        if tool_name == "pr_submit_review":
            revalidate_review_head_for_context(
                auth_context,
                arguments.get("repository"),
                arguments.get("pull_request"),
            )
    except ToolException as exc:
        return str(exc)
    return None


# Tools exempt from the per-turn cap. They neither consume a slot nor
# get refused after the cap is hit. The driving case is ``send_message``:
# when the budget is exhausted the denial path tells the model to
# "finish the turn", but the final assistant text does NOT auto-deliver
# to channels (an explicit send_message call is the only delivery path
# — see SPEC §7.1). Without exempting it, the agent would hit the cap,
# get told to stop, but have no way to actually tell the operator. ``react``
# is exempt for the same operator-facing-acknowledgement reason.
_BUDGET_EXEMPT_TOOLS = frozenset({"send_message", "react"})

_ADMIN_TOOL_NAMES: frozenset[str] = frozenset(
    {
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
        "saga_forget",
        # Deepagents built-in write tools mutate tracked state / repo files.
        # Under access-control enforcement they have comparable blast radius
        # to reload_pollers and proposal tools, so gate them explicitly rather
        # than leaving file writes as a prompt-policy-only boundary.
        "write_file",
        "edit_file",
    }
)

# PRODUCTION-DEAD (chainlink #895): This frozenset is never consulted in
# the production code path. The authoritative admin-tool set lives in
# access_control.py OperationCatalog._ADMIN_REQUIRED_OPERATIONS. Retained
# for backwards compatibility with any external callers that might reference it.

def _auth_context_from_request(request: ToolCallRequest) -> AuthContext | None:
    """Return the exact graph invocation's valid server-created auth carrier.

    LangGraph constructs ``ToolCallRequest.runtime`` for the tool request being
    executed.  Do not fall back to model arguments, active-turn registries, or
    ContextVars here: none of those identify this exact request. Malformed
    non-``None`` carriers are treated as missing so process-level enforcement
    fails closed rather than trusting arbitrary lookalike objects.
    """
    runtime = getattr(request, "runtime", None)
    context = getattr(runtime, "context", None) if runtime is not None else None
    return context if isinstance(context, AuthContext) else None


_ADMIN_BUILTIN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "Bash",
        "bash",
        "bash_exec",
        "execute",
        "aexecute",
        "shell",
        "Write",
        "Edit",
    }
)

# PRODUCTION-DEAD (chainlink #895): This frozenset diverges from
# access_control.py OperationCatalog._ADMIN_BUILTIN_TOOL_NAMES (which includes
# "Read", "Glob", "Grep", "download_files") and is never consulted in the
# production code path. The authoritative set is in access_control.py.
# Retained for test compatibility but marked as deprecated.

_HTTP_EVENT_ADMIN_DENIAL_REASON = "http_event_author_untrusted"


def _resolve_budget_state(ctx: Any | None = None) -> tuple[Any, int] | None:
    """Return ``(ctx, budget)`` if a TurnContext with a non-zero
    ``tool_call_budget`` is active. ``None`` means: no enforcement
    (no active ctx, or budget=0). Avoids hard-coupling this module
    to the import chain for tests."""
    if ctx is None:
        from .._context import get_current_turn
        ctx = get_current_turn()
    if ctx is None:
        return None
    budget = getattr(ctx, "tool_call_budget", 0) or 0
    if budget <= 0:
        return None
    return ctx, int(budget)


# Strong references to fire-and-forget background tasks (chainlink #118).
# Module-level set holds tasks spawned by _emit_event_sync until completion.
# The done-callback discards each entry so the set stays bounded to in-flight
# tasks only.  See cpython docs "Coroutines and Tasks / Important" callout.
_background_tasks: set["asyncio.Task[Any]"] = set()


def _emit_event_sync(kind: str, **kwargs: Any) -> None:
    """Fire-and-forget log_event from inside the middleware sync path.

    The middleware's ``wrap_tool_call`` is sync; ``log_event`` is async.
    We schedule it on the running loop when available, drop otherwise
    (the denial text on the returned ToolMessage is still load-bearing).
    """
    try:
        from ..event_logger import safe_log_event
        loop = asyncio.get_running_loop()
        task = loop.create_task(safe_log_event(kind, **kwargs))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        log.debug("budget event %s dropped: no running loop", kind)


def _emit_hard_boundary_denied(
    *,
    tool: str,
    boundary: str,
    reason: str,
    target: Any = None,
    auth_context: AuthContext | None = None,
    turn_context: Any | None = None,
    event_fields: dict[str, Any] | None = None,
) -> None:
    """Record an action that an always-on boundary actually refused."""
    active_turn = turn_context or _get_current_turn_context()
    if active_turn is not None:
        denials = getattr(active_turn, "hard_boundary_denials", None)
        denial = {"tool": tool, "boundary": boundary, "reason": reason}
        if isinstance(denials, list):
            denials.append(denial)
        else:
            active_turn.hard_boundary_denials = [denial]
    if auth_context is None and turn_context is not None:
        candidate = getattr(turn_context, "auth_context", None)
        auth_context = candidate if isinstance(candidate, AuthContext) else None
    if auth_context is None:
        candidate = getattr(active_turn, "auth_context", None)
        auth_context = candidate if isinstance(candidate, AuthContext) else None

    from ..redaction import redact_payload

    service = get_trusted_service_from_auth_context(auth_context)
    payload = {
        "tool": tool,
        "boundary": boundary,
        "reason": reason,
        # Pre-scrub for replaced emitters and other pre-persistence consumers.
        "target": redact_payload(target),
        "trigger": (
            getattr(auth_context, "origin_trigger", None)
            or getattr(auth_context, "trigger", None)
            or getattr(turn_context, "trigger", None)
        ),
        "channel_id": getattr(auth_context, "channel_id", None),
        "service_principal": service.canonical if service is not None else None,
    }
    if event_fields:
        payload.update(redact_payload(event_fields))
    _emit_event_sync(
        "hard_boundary_denied",
        **payload,
    )


def _record_tool_outcome(
    tool_name: str,
    *,
    refused_reason: str = "",
    operator_shell_audit: Mapping[str, str] | None = None,
) -> None:
    """Attach server-observed remediation evidence to the active turn."""
    if refused_reason:
        if operator_shell_audit is not None:
            _emit_hard_boundary_denied(
                tool=tool_name,
                boundary="operator_shell_policy",
                reason="operator_shell_tool_refused",
                target=None,
                event_fields=dict(operator_shell_audit),
            )
            return
        _emit_hard_boundary_denied(
            tool=tool_name,
            boundary="tool_policy",
            reason=refused_reason[:240],
        )
        return
    if tool_name == "unsupported_operation":
        _emit_hard_boundary_denied(
            tool=tool_name,
            boundary="typed_action_set",
            reason="unsupported_operation",
        )
        return
    if tool_name not in _REMEDIATION_EFFECT_TOOLS:
        return
    active_turn = _get_current_turn_context()
    effects = getattr(active_turn, "remediation_effects", None)
    if isinstance(effects, list):
        effects.append(tool_name)


def _budget_denied_message(tool_name: str, count: int, budget: int) -> str:
    return (
        f"Tool-call budget exhausted: {count}/{budget} calls used "
        f"this turn. ``{tool_name}`` was refused. ``send_message`` and "
        f"``react`` remain available so you can still reply or "
        f"acknowledge — use them to wrap up the turn rather than "
        f"firing another tool."
    )


def _mark_budget_denied(ctx: Any, tool_name: str, count: int) -> None:
    """Persist hard-denial markers on the active turn context."""
    ctx.tool_call_budget_exhausted = True
    ctx.tool_call_budget_denied_count = (
        int(getattr(ctx, "tool_call_budget_denied_count", 0) or 0) + 1
    )
    denied_tools = getattr(ctx, "tool_call_budget_denied_tools", None)
    if isinstance(denied_tools, list):
        denied_tools.append(tool_name)
    elif denied_tools is None:
        ctx.tool_call_budget_denied_tools = [tool_name]
    else:
        ctx.tool_call_budget_denied_tools = [*denied_tools, tool_name]
    if getattr(ctx, "tool_call_budget_first_denied_at_count", None) is None:
        ctx.tool_call_budget_first_denied_at_count = count


def _check_and_increment_or_deny(
    tool_name: str,
    ctx: Any | None = None,
    *,
    target: Any = None,
    auth_context: AuthContext | None = None,
    operator_shell_audit: Mapping[str, str] | None = None,
) -> str | None:
    """Returns a denial message (str) if the call should be refused,
    or ``None`` if the call should proceed. Shared between the sync
    and async middleware paths so the bookkeeping stays identical."""
    # Exempt tools (send_message, react) bypass both the count
    # increment AND the cap check — see ``_BUDGET_EXEMPT_TOOLS``
    # docstring for why. Free passage, no bookkeeping.
    if tool_name in _BUDGET_EXEMPT_TOOLS:
        return None
    state = _resolve_budget_state(ctx)
    if state is None:
        return None
    ctx, budget = state
    count = getattr(ctx, "tool_call_count", 0) or 0
    if count >= budget:
        _mark_budget_denied(ctx, tool_name, count)
        _emit_event_sync(
            "tool_call_budget_denied",
            tool=tool_name,
            count=count,
            budget=budget,
            turn_id=getattr(ctx, "turn_id", None),
            **(operator_shell_audit or {}),
        )
        _emit_hard_boundary_denied(
            tool=tool_name,
            boundary="tool_call_budget",
            reason="tool_call_budget_exhausted",
            target=None if operator_shell_audit is not None else target,
            auth_context=auth_context,
            turn_context=ctx,
            event_fields=(
                dict(operator_shell_audit)
                if operator_shell_audit is not None
                else None
            ),
        )
        return _budget_denied_message(tool_name, count, budget)
    new_count = count + 1
    ctx.tool_call_count = new_count
    soft = max(1, int(budget * 0.75))
    if new_count >= soft and not getattr(
        ctx, "_tool_call_soft_warning_emitted", False,
    ):
        ctx._tool_call_soft_warning_emitted = True
        _emit_event_sync(
            "tool_call_budget_soft_warning",
            tool=tool_name,
            count=new_count,
            budget=budget,
            soft_threshold=soft,
            turn_id=getattr(ctx, "turn_id", None),
            **(operator_shell_audit or {}),
        )
    return None


def _tool_name_from_request(request: ToolCallRequest) -> str:
    """Pull a usable name off the ToolCallRequest. ``request.tool``
    is the BaseTool when registered, ``None`` for un-registered calls
    (e.g. typos the model generates). The ``tool_call`` dict always
    carries the name the model used."""
    tc = getattr(request, "tool_call", None) or {}
    return str(tc.get("name") or "<unknown>")


def _tool_call_id(request: ToolCallRequest) -> str:
    tc = getattr(request, "tool_call", None) or {}
    return str(tc.get("id") or "")


def _extract_sink_target(
    request: ToolCallRequest,
    auth_context: AuthContext | None = None,
) -> str | None:
    """Return the concrete operation destination for sink authorization.

    Channel tools default an omitted/empty ``channel_id`` to the current turn's
    channel. Mirror that server-owned resolution here so the gate authorizes an
    implicit reply-to-trigger as same-scope rather than as a missing resource.
    """
    tc = getattr(request, "tool_call", None) or {}
    args = tc.get("args") or {}
    tool_name = _tool_name_from_request(request)
    if tool_name in {
        "pr_submit_review", "pr_inline_review_comment", "pr_comment",
        "pr_rerequest_review", "unsupported_operation", "repo_checkout",
        "repo_cleanup", "repo_fetch", "repo_test", "repo_stage", "repo_commit", "repo_merge",
        "repo_merge_abort", "repo_rebase", "repo_rebase_abort", "repo_revert",
        "repo_revert_abort", "repo_push",
    }:
        discovered = getattr(auth_context, "server_discovered_pr_states", None)
        state = (
            discovered.resolve(args.get("repository"), args.get("pull_request"))
            if discovered is not None
            and isinstance(args.get("repository"), str)
            and isinstance(args.get("pull_request"), int)
            else None
        )
        if state is None:
            registry = getattr(auth_context, "repo_pr_scope_registry", None)
            state = (
                registry.resolve(args.get("repository"), args.get("pull_request"))
                if registry is not None and hasattr(registry, "resolve")
                else None
            )
        if state is None:
            return None
        scope = state.action_scope
        return (
            f"{scope.canonical_repo}#pull/{scope.pr_number}"
            f"@{scope.observed_head_sha}:{scope.scope_id}"
        )
    if tool_name == "operator_alert":
        from ..channel_registry import OPERATOR_CHANNEL_SENTINEL, resolve_deliver_channel

        return resolve_deliver_channel(
            OPERATOR_CHANNEL_SENTINEL,
            os.environ.get("MIMIR_OPERATOR_ALERT_CHANNEL", ""),
        )
    if tool_name in {"send_message", "react", "fetch_channel_history"}:
        explicit_channel = args.get("channel_id")
        if explicit_channel:
            return str(explicit_channel)
        return auth_context.channel_id if auth_context is not None else None
    if tool_name in {"write_file", "edit_file"}:
        target = args.get("file_path") or args.get("path")
    elif tool_name in {"shell_exec", "bash_async"}:
        target = args.get("command")
    elif tool_name == "spawn_open_code":
        target = args.get("cwd") or os.environ.get("MIMIR_HOME")
    elif tool_name == "worklink_run":
        target = os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO")
    elif tool_name in {"fetch_url", "http_request", "webhook"}:
        target = args.get("url")
    elif tool_name == "web_search":
        target = web_search_url()
    elif tool_name in {"add_schedule", "set_schedule_priority", "remove_schedule"}:
        name = str(args.get("name") or "").strip()
        target = f"scheduler:job:{name}" if name else "scheduler:jobs"
    elif tool_name == "set_poller_overrides":
        home = os.environ.get("MIMIR_HOME", "").strip()
        target = str(Path(home) / "pollers-overrides.yaml") if home else "scheduler:poller-overrides"
    elif tool_name == "reload_pollers":
        target = "scheduler:pollers"
    elif tool_name in {
        "commitment_complete", "commitment_snooze", "commitment_dismiss",
    }:
        commitment_id = str(args.get("commitment_id") or "").strip()
        target = f"commitment:{commitment_id}" if commitment_id else "commitments"
    elif tool_name == "defer_injected_message":
        message_id = str(args.get("message_id") or "").strip()
        target = f"injected-message:{message_id}" if message_id else "injected_messages"
    elif tool_name == "request_mimir_update":
        home = os.environ.get("MIMIR_HOME", "").strip()
        target = str(Path(home) / ".mimir" / "pending-update.flag") if home else "pending-update.flag"
    elif tool_name == "rebuild_index":
        scope = str(args.get("scope") or "all").strip().lower()
        target = f"index:{scope}"
    elif tool_name.startswith("mcp_"):
        target = tool_name
    else:
        target = args.get("target") or args.get("destination")
    return str(target) if target else None


def _extract_sink_targets(
    request: ToolCallRequest,
    auth_context: AuthContext | None = None,
) -> list[str | None]:
    """Return every independently writable destination in a tool call."""
    target = _extract_sink_target(request, auth_context)
    if _tool_name_from_request(request) != "spawn_open_code":
        return [target]

    args = (getattr(request, "tool_call", None) or {}).get("args") or {}
    artifact_root = args.get("artifact_root")
    return [target, str(artifact_root)] if artifact_root else [target]


def _authorized_fetch_urls_for_tool(
    tool_name: str,
    auth_context: AuthContext | None,
    target: str | None = None,
) -> frozenset[str] | None:
    if tool_name == "fetch_url":
        approved = set(approved_fetch_urls(auth_context))
        if target is not None and fetch_url_is_approved(target, auth_context):
            normalized = normalize_sink_destination(SinkCategory.NETWORK, target)
            if normalized is not None:
                approved.add(normalized)
        return frozenset(approved)
    if tool_name == "web_search":
        from ..access_control import _fixed_web_search_url

        fixed_url = _fixed_web_search_url()
        return frozenset({fixed_url}) if fixed_url is not None else frozenset()
    return None


# Compatibility alias for callers that only exercise channel operations.
_extract_channel_from_args = _extract_sink_target


# Internal execution arguments only this module may set. A model-supplied value
# is stripped before authorization: ``mimir_direct_argv`` would choose what runs,
# and ``mimir_shell_refusal`` would let a model author text that reads as a
# server authorization verdict.
_SERVER_ONLY_SHELL_ARGS = frozenset({
    "mimir_direct_argv",
    "mimir_shell_refusal",
    "mimir_operator_shell_binding",
    "mimir_operator_shell_profile",
    "mimir_operator_shell_request_identity",
})


def _strip_server_only_shell_args(request: ToolCallRequest) -> ToolCallRequest:
    tool_call = getattr(request, "tool_call", None) or {}
    arguments = tool_call.get("args")
    if not isinstance(arguments, dict) or not (_SERVER_ONLY_SHELL_ARGS & arguments.keys()):
        return request
    sanitized = dict(arguments)
    for name in _SERVER_ONLY_SHELL_ARGS:
        sanitized.pop(name, None)
    return request.override(tool_call={**tool_call, "args": sanitized})


class OperatorShellPreparationOutcome(StrEnum):
    BOUND = "bound"
    SOFT_UNBOUND = "soft_unbound"
    HARD_REFUSED = "hard_refused"


_OPERATOR_SHELL_COMMAND_FAMILIES = frozenset({
    "invalid_command",
    "parser",
    "project_test",
    "profile_miss",
    "chainlink",
    "pwd",
    "ls",
    "wc",
    "grep",
    "jq",
    "rg",
    "git",
})


@dataclass(frozen=True)
class _OperatorShellPreparation:
    outcome: OperatorShellPreparationOutcome
    binding: OperatorShellBinding | None
    refusal: str | None
    binding_rule: ServiceShellBindingRule | None
    command_family: str

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, OperatorShellPreparationOutcome):
            raise ValueError("unknown operator shell preparation outcome")
        if self.command_family not in _OPERATOR_SHELL_COMMAND_FAMILIES:
            raise ValueError("unknown operator shell command family")
        if self.outcome is OperatorShellPreparationOutcome.BOUND:
            if (
                not isinstance(self.binding, OperatorShellBinding)
                or self.binding.profile != OPERATOR_SHELL_PROFILE
                or self.binding.tool_name != "shell_exec"
                or not self.binding.argv
                or Path(self.binding.argv[0]).name != self.command_family
                or self.refusal is not None
                or self.binding_rule is not None
            ):
                raise ValueError("bound operator shell preparation requires one valid binding")
        elif self.outcome not in {
            OperatorShellPreparationOutcome.SOFT_UNBOUND,
            OperatorShellPreparationOutcome.HARD_REFUSED,
        } or (
            self.binding is not None
            or not isinstance(self.refusal, str)
            or not self.refusal
            or not isinstance(self.binding_rule, ServiceShellBindingRule)
        ):
            raise ValueError("unbound operator shell preparation requires a fixed refusal")


_OPERATOR_INVALID_COMMAND_REFUSAL = "operator shell command is invalid"
_OPERATOR_PARSER_FAILURE_REFUSAL = "operator shell parser failed closed"
_OPERATOR_CWD_FAILURE_REFUSAL = "operator shell cwd confinement failed"
_OPERATOR_BINDING_FAILURE_REFUSAL = "operator shell binding failed closed"
_OPERATOR_SHELL_HARD_REFUSAL = (
    "shell_exec was refused before execution: operator shell preparation failed closed"
)
_OPERATOR_SHELL_BINDING_REFUSAL = (
    "shell_exec was refused before execution: operator shell binding failed closed"
)
_OPERATOR_SHELL_LIVE_TAINT_REFUSAL = (
    "shell_exec was refused before execution (ifc_label_blocked:shell_process): "
    "operator shell fallback requires exactly untainted live IFC"
)


def _operator_shell_unbound(
    outcome: OperatorShellPreparationOutcome,
    refusal: str,
    binding_rule: ServiceShellBindingRule,
    command_family: str,
) -> _OperatorShellPreparation:
    return _OperatorShellPreparation(
        outcome=outcome,
        binding=None,
        refusal=refusal,
        binding_rule=binding_rule,
        command_family=command_family,
    )


def _operator_shell_audit_summary(
    preparation: _OperatorShellPreparation | None,
) -> dict[str, str] | None:
    if preparation is None:
        return None
    return {
        "shell_profile": OPERATOR_SHELL_PROFILE,
        "preparation_outcome": preparation.outcome.value,
        "command_family": preparation.command_family,
        "binding_rule": (
            preparation.binding_rule.value
            if preparation.binding_rule is not None
            else "exact_argv_binding"
        ),
    }


def _operator_shell_hard_refusal(
    request: ToolCallRequest,
    preparation: _OperatorShellPreparation | None,
    auth_context: AuthContext | None,
) -> ToolMessage | None:
    if (
        preparation is None
        or preparation.outcome is not OperatorShellPreparationOutcome.HARD_REFUSED
    ):
        return None
    audit = _operator_shell_audit_summary(preparation)
    assert audit is not None
    _emit_hard_boundary_denied(
        tool="shell_exec",
        boundary="operator_shell_preparation",
        reason="operator_shell_hard_refused",
        target=None,
        auth_context=auth_context,
        event_fields=audit,
    )
    _emit_tool_call_sync(
        "shell_exec",
        ok=False,
        error=_OPERATOR_SHELL_HARD_REFUSAL,
        denied=True,
        operator_shell_audit=audit,
    )
    return ToolMessage(
        content=_OPERATOR_SHELL_HARD_REFUSAL,
        tool_call_id=_tool_call_id(request),
        name="shell_exec",
        status="error",
    )


def _operator_shell_chainlink_mutation_refusal(
    request: ToolCallRequest,
    preparation: _OperatorShellPreparation | None,
    auth_context: AuthContext | None,
) -> ToolMessage | None:
    if (
        preparation is None
        or preparation.outcome is not OperatorShellPreparationOutcome.BOUND
        or preparation.binding is None
        or not preparation.binding.chainlink_mutation
        or _live_untrusted_active_ingest(
            auth_context, _current_ifc_labels(auth_context),
        ) is False
    ):
        return None
    from ..access_control import _CHAINLINK_TAINT_REFUSAL

    audit = _operator_shell_audit_summary(preparation)
    assert audit is not None
    _emit_hard_boundary_denied(
        tool="shell_exec",
        boundary="operator_shell_chainlink_mutation",
        reason="chainlink_mutation_blocked_by_untrusted_ingest",
        target=None,
        auth_context=auth_context,
        event_fields=audit,
    )
    _emit_tool_call_sync(
        "shell_exec",
        ok=False,
        error=_CHAINLINK_TAINT_REFUSAL,
        denied=True,
        operator_shell_audit=audit,
    )
    return ToolMessage(
        content=_CHAINLINK_TAINT_REFUSAL,
        tool_call_id=_tool_call_id(request),
        name="shell_exec",
        status="error",
    )


def _operator_shell_execution_binding_matches(
    request: ToolCallRequest,
    auth_context: AuthContext | None,
    binding: OperatorShellBinding,
) -> bool:
    arguments = (getattr(request, "tool_call", None) or {}).get("args")
    return (
        isinstance(arguments, dict)
        and binding._issuer is _OPERATOR_SHELL_BINDING_ISSUER
        and binding.profile == OPERATOR_SHELL_PROFILE
        and binding.tool_name == "shell_exec"
        and binding._request_identity is request
        and binding._auth_context_identity is auth_context
        and binding.tool_call_id == _tool_call_id(request)
        and binding.command == arguments.get("command")
        and binding.requested_cwd == arguments.get("cwd")
        and isinstance(binding.resolved_cwd, str)
        and bool(binding.resolved_cwd)
        and bool(binding.argv)
    )


def _prepare_operator_shell_execution(
    request: ToolCallRequest,
    tool_name: str,
    auth_context: AuthContext | None,
    ifc_labels: Any,
) -> _OperatorShellPreparation | None:
    if not _operator_can_invoke_admin_shell(tool_name, ifc_labels, auth_context):
        return None
    arguments = (getattr(request, "tool_call", None) or {}).get("args")
    if not isinstance(arguments, dict):
        return _operator_shell_unbound(
            OperatorShellPreparationOutcome.HARD_REFUSED,
            _OPERATOR_INVALID_COMMAND_REFUSAL,
            ServiceShellBindingRule.PROFILE_ALLOWLIST,
            "invalid_command",
        )
    command = arguments.get("command")
    if not isinstance(command, str):
        return _operator_shell_unbound(
            OperatorShellPreparationOutcome.HARD_REFUSED,
            _OPERATOR_INVALID_COMMAND_REFUSAL,
            ServiceShellBindingRule.PROFILE_ALLOWLIST,
            "invalid_command",
        )
    try:
        parsed_argv, refusal, binding_rule = parse_service_shell_argv_with_diagnostics(
            command,
            OPERATOR_SHELL_PROFILE,
            declared=(),
            service=None,
            auth_context=None,
            review_state=None,
            allow_project_test=False,
        )
    except Exception:
        return _operator_shell_unbound(
            OperatorShellPreparationOutcome.HARD_REFUSED,
            _OPERATOR_PARSER_FAILURE_REFUSAL,
            ServiceShellBindingRule.UNKNOWN_PROFILE,
            "parser",
        )
    if parsed_argv is None:
        if binding_rule is None:
            return _operator_shell_unbound(
                OperatorShellPreparationOutcome.HARD_REFUSED,
                _OPERATOR_PARSER_FAILURE_REFUSAL,
                ServiceShellBindingRule.UNKNOWN_PROFILE,
                "parser",
            )
        command_family = "profile_miss"
        if binding_rule is ServiceShellBindingRule.PROFILE_ALLOWLIST:
            try:
                import shlex

                candidate = shlex.split(command)
                _test_argv, _test_reason, project_test_matched = (
                    _project_test_execution_argv(candidate)
                )
            except (OSError, RuntimeError, ValueError):
                project_test_matched = False
            if project_test_matched:
                return _operator_shell_unbound(
                    OperatorShellPreparationOutcome.SOFT_UNBOUND,
                    "operator project test is not eligible for binding",
                    ServiceShellBindingRule.OPERATOR_PROJECT_TEST_EXCLUDED,
                    "project_test",
                )
        soft_rules = {
            ServiceShellBindingRule.SHELL_CONTROL_CHARACTERS,
            ServiceShellBindingRule.ARGV_UNBALANCED_QUOTING,
            ServiceShellBindingRule.ARGV_EMPTY,
            ServiceShellBindingRule.SHELL_HOME_EXPANSION,
            ServiceShellBindingRule.PROFILE_ALLOWLIST,
        }
        return _operator_shell_unbound(
            (
                OperatorShellPreparationOutcome.SOFT_UNBOUND
                if binding_rule in soft_rules
                else OperatorShellPreparationOutcome.HARD_REFUSED
            ),
            refusal or _OPERATOR_PARSER_FAILURE_REFUSAL,
            binding_rule,
            command_family,
        )

    family = Path(parsed_argv[0]).name if parsed_argv else "parser"
    if family not in _OPERATOR_SHELL_COMMAND_FAMILIES or family in {
        "invalid_command", "parser", "project_test", "profile_miss",
    }:
        return _operator_shell_unbound(
            OperatorShellPreparationOutcome.HARD_REFUSED,
            _OPERATOR_PARSER_FAILURE_REFUSAL,
            ServiceShellBindingRule.UNKNOWN_PROFILE,
            "parser",
        )
    requested_cwd = arguments.get("cwd")
    resolved_cwd = _resolve_operator_bounded_cwd(requested_cwd, git=family == "git")
    if resolved_cwd is None:
        return _operator_shell_unbound(
            OperatorShellPreparationOutcome.HARD_REFUSED,
            _OPERATOR_CWD_FAILURE_REFUSAL,
            ServiceShellBindingRule.OPERATOR_CWD_POLICY,
            family,
        )
    final_argv = parsed_argv
    if family in {"ls", "wc", "grep", "jq", "rg"}:
        final_argv, refusal, binding_rule = _operator_read_execution_argv_with_diagnostics(
            parsed_argv, resolved_cwd=resolved_cwd,
        )
        if final_argv is None:
            return _operator_shell_unbound(
                (
                    OperatorShellPreparationOutcome.SOFT_UNBOUND
                    if binding_rule is ServiceShellBindingRule.OPERATOR_READER_EXCLUDED
                    else OperatorShellPreparationOutcome.HARD_REFUSED
                ),
                refusal or _OPERATOR_BINDING_FAILURE_REFUSAL,
                binding_rule or ServiceShellBindingRule.OPERATOR_READ_OPERAND_POLICY,
                family,
            )
    elif family == "git":
        final_argv, refusal, binding_rule = _operator_git_execution_argv_with_diagnostics(
            parsed_argv, resolved_cwd=resolved_cwd,
        )
        if final_argv is None:
            soft = refusal == "operator shell Git form is not eligible for binding"
            return _operator_shell_unbound(
                (
                    OperatorShellPreparationOutcome.SOFT_UNBOUND
                    if soft
                    else OperatorShellPreparationOutcome.HARD_REFUSED
                ),
                refusal or _OPERATOR_BINDING_FAILURE_REFUSAL,
                binding_rule or ServiceShellBindingRule.OPERATOR_GIT_HARDENING,
                family,
            )
    artifact = _validated_operator_shell_argv_artifact(
        parsed_argv, final_argv, resolved_cwd=resolved_cwd,
    )
    if artifact is None:
        return _operator_shell_unbound(
            OperatorShellPreparationOutcome.HARD_REFUSED,
            _OPERATOR_BINDING_FAILURE_REFUSAL,
            ServiceShellBindingRule.OPERATOR_BINDING_MISMATCH,
            family,
        )
    binding = _issue_operator_shell_binding(
        request_identity=request,
        auth_context_identity=auth_context,
        tool_call_id=_tool_call_id(request),
        command=command,
        requested_cwd=requested_cwd,
        resolved_cwd=str(resolved_cwd),
        argv_artifact=artifact,
    )
    if binding is None:
        return _operator_shell_unbound(
            OperatorShellPreparationOutcome.HARD_REFUSED,
            _OPERATOR_BINDING_FAILURE_REFUSAL,
            ServiceShellBindingRule.OPERATOR_BINDING_MISMATCH,
            family,
        )
    return _OperatorShellPreparation(
        outcome=OperatorShellPreparationOutcome.BOUND,
        binding=binding,
        refusal=None,
        binding_rule=None,
        command_family=family,
    )


def _service_shell_refusal(request: ToolCallRequest) -> str | None:
    """The server-authored refusal bound to this request, if it was refused."""
    refusal = (request.tool_call.get("args") or {}).get("mimir_shell_refusal")
    return refusal if isinstance(refusal, str) and refusal else None


def _duplicate_review_result(request: ToolCallRequest, claim: Any) -> ToolMessage:
    """Return and record the successful no-op without exposing review text."""
    _emit_event_sync(
        "github_review_duplicate_suppressed",
        repo=claim.repo,
        pr=claim.number,
        head=claim.head,
        reviewer=claim.reviewer,
        review_state=claim.state,
    )
    return ToolMessage(
        content=(
            "GitHub review submission was not repeated: the existing "
            f"{claim.state} review by {claim.reviewer} on exact head "
            f"{claim.head} already satisfies this submission."
        ),
        tool_call_id=_tool_call_id(request),
        name=_tool_name_from_request(request),
        status="success",
    )


async def _claim_review_submission_async(claim: Callable[[], Any]) -> Any:
    """Keep ownership of a thread-acquired claim when the caller is cancelled."""
    task = asyncio.create_task(asyncio.to_thread(claim))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        def release_late_claim(completed: asyncio.Task[Any]) -> None:
            try:
                late_claim = completed.result()
            except BaseException:
                return
            if late_claim is not None:
                late_claim.release()

        task.add_done_callback(release_late_claim)
        raise


def _record_repo_review_checkout(
    request: ToolCallRequest, auth_context: AuthContext | None, *, failed: bool,
) -> None:
    """Record only a successfully executed checkout of this turn's bound head."""
    args = request.tool_call.get("args") or {}
    state, _ = resolve_repository_review_state(
        auth_context,
        command=args.get("command"),
        cwd=args.get("cwd"),
    )
    if failed or state is None:
        return
    argv = args.get("mimir_direct_argv")
    if not isinstance(argv, list):
        return
    if argv[-7:] == [
        "pr", "checkout", str(state.pr_number),
        "--repo", state.repo, "--branch", state.head_ref,
    ] or (
        len(argv) >= 2
        and argv[-2:] == ["checkout", state.head_ref]
        and argv[1:3] == ["-C", state.root]
    ):
        state.mark_checked_out()


def _resolve_service_shell_cwd(
    raw_cwd: object,
    service: ServicePrincipal | None = None,
) -> tuple[str | None, str | None]:
    """Resolve an explicit service cwd within that principal's read roots."""
    if raw_cwd is None:
        # Keep cwd omitted. Direct service execution deliberately bypasses
        # interactive per-session cwd; configured project/Chainlink commands
        # are assigned their server-authorized cwd by their branches below.
        return None, None
    if not isinstance(raw_cwd, str) or not raw_cwd.strip() or "\x00" in raw_cwd:
        return None, "working directory must be a non-empty absolute path"
    candidate = Path(raw_cwd)
    if not candidate.is_absolute():
        return None, "working directory must be a non-empty absolute path"
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "working directory is not an accessible directory"
    if not resolved.is_dir():
        return None, "working directory is not an accessible directory"

    # Deployment roots keep the ordinary file-tool cwd contract, while the
    # principal's instance roots cover server-issued workspaces such as a bound
    # repository checkout. Neither set widens the other principal: both are
    # server-owned grants and the read-operand boundary applies the principal's
    # narrower path/content veto before execution.
    from ..read_policy import configured_non_admin_read_roots

    roots: list[Path] = []
    for root in (
        *configured_non_admin_read_roots(),
        *service_filesystem_read_roots(service),
    ):
        try:
            resolved_root = root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved_root.is_dir() and resolved_root not in roots:
            roots.append(resolved_root)
    if not any(resolved.is_relative_to(root) for root in roots):
        return None, "working directory is outside the trusted service's authorized read roots"
    return str(resolved), None


def _resolve_chainlink_service_cwd() -> tuple[str | None, str | None]:
    """Select an authorized cwd from which Chainlink can discover its tracker."""
    from ..read_policy import configured_non_admin_read_roots

    home_raw = os.environ.get("MIMIR_HOME", "").strip()
    if not home_raw:
        return None, "the configured Chainlink tracker root is unavailable"
    try:
        home = Path(home_raw).resolve(strict=True)
        tracker = (home / ".chainlink").resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "the configured Chainlink tracker root is unavailable"
    if not home.is_dir() or not tracker.is_dir() or tracker != home / ".chainlink":
        return None, "the configured Chainlink tracker root is unavailable"

    for root in configured_non_admin_read_roots():
        try:
            resolved = root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_dir() or not resolved.is_relative_to(home):
            continue
        candidate = resolved
        shadowed = False
        while candidate != home:
            if (candidate / ".chainlink").is_dir():
                shadowed = True
                break
            candidate = candidate.parent
        if not shadowed and candidate == home:
            return str(resolved), None
    return None, (
        "the Chainlink tracker cannot be reached from any authorized read root; "
        "the home root remains intentionally excluded"
    )


def _request_for_authorized_execution(
    request: ToolCallRequest,
    tool_name: str,
    auth_context: AuthContext | None,
    *,
    operator_shell_preparation: _OperatorShellPreparation | None = None,
) -> ToolCallRequest:
    """Bind trusted-service shell execution to the argv authorization checked.

    Ordinary user/admin shell tools keep their documented full-shell surface.
    A trusted service receives only the direct argv admitted by its operation-
    specific sink policy; the handler never sees the original command string.
    """
    args = dict((getattr(request, "tool_call", None) or {}).get("args") or {})
    # Never trust a model-supplied internal execution override. Ordinary calls
    # discard it; trusted-service calls below replace it with server-parsed argv
    # and, on refusal, a server-authored explanation.
    had_model_override = bool(_SERVER_ONLY_SHELL_ARGS & args.keys())
    for server_only_key in _SERVER_ONLY_SHELL_ARGS:
        args.pop(server_only_key, None)
    sanitized_request = (
        request.override(tool_call={**request.tool_call, "args": args})
        if had_model_override
        else request
    )
    if operator_shell_preparation is not None:
        preparation = operator_shell_preparation
        if preparation.outcome is OperatorShellPreparationOutcome.SOFT_UNBOUND:
            if _live_untrusted_active_ingest(
                auth_context, _current_ifc_labels(auth_context),
            ) is False:
                return sanitized_request
            args["mimir_shell_refusal"] = _OPERATOR_SHELL_LIVE_TAINT_REFUSAL
            return sanitized_request.override(
                tool_call={**sanitized_request.tool_call, "args": args},
            )
        binding = preparation.binding
        if (
            preparation.outcome is not OperatorShellPreparationOutcome.BOUND
            or binding is None
            or tool_name != "shell_exec"
            or not _operator_shell_execution_binding_matches(
                request, auth_context, binding,
            )
        ):
            args["mimir_shell_refusal"] = _OPERATOR_SHELL_BINDING_REFUSAL
            args["mimir_direct_argv"] = [
                "/usr/bin/false", "operator shell binding failed closed",
            ]
            return sanitized_request.override(
                tool_call={**sanitized_request.tool_call, "args": args},
            )
        args["cwd"] = binding.resolved_cwd
        args["mimir_direct_argv"] = list(binding.argv)
        return sanitized_request.override(
            tool_call={**sanitized_request.tool_call, "args": args},
        )
    if tool_name not in {"shell_exec", "bash_async"}:
        return sanitized_request
    service = get_trusted_service_from_auth_context(auth_context)
    policy = service.sink_policy_for(tool_name) if service is not None else None
    if policy is None or policy.adapter != "shell_profile":
        return sanitized_request
    target = args.get("command")
    if not isinstance(target, str):
        return sanitized_request
    if policy.destination == "repo_review":
        review_state, state_refusal = resolve_repository_review_state(
            auth_context, command=target, cwd=args.get("cwd"),
        )
        argv, refusal, binding_rule = parse_service_shell_argv_with_diagnostics(
            target,
            policy.destination,
            review_state=review_state,
            declared=getattr(service, "declared_shell_commands", ()) or (),
        )
        if state_refusal is not None:
            argv = None
            refusal = state_refusal
            binding_rule = ServiceShellBindingRule.REPOSITORY_REVIEW_STATE
    else:
        argv, refusal, binding_rule = parse_service_shell_argv_with_diagnostics(
            target, policy.destination,
            declared=getattr(service, "declared_shell_commands", ()) or (),
        )
    if argv is None:
        refused_argv, argv_truncated = service_shell_argv_for_log(target)
        # The authorization adapter already admitted this call. Failing to bind
        # a direct argv here must not fall back to the original ``bash -lc``
        # surface if a config probe races, times out, or otherwise changes.
        #
        # Log under a distinct fingerprint so the cause stays greppable for an
        # operator, naming the profile and stable rejecting rule. The command is
        # represented only by the separately redacted argv used by the event.
        log.error(
            "service_shell_argv_binding_failed profile=%s binding_rule=%s",
            policy.destination,
            binding_rule,
        )
        # Record it for the audit stream: this boundary fails closed whether or
        # not enforcement is on, so without an event the refusal is invisible to
        # the enablement evidence (#1012).
        _emit_hard_boundary_denied(
            tool=tool_name,
            boundary="service_shell_argv_binding",
            reason="service_shell_argv_binding_failed",
            target=None,
            auth_context=auth_context,
            event_fields={
                "argv": refused_argv,
                "argv_truncated": argv_truncated,
                "shell_profile": policy.destination,
                "binding_rule": binding_rule,
            },
        )
        # ...and tell the CALLER why, in the tool result. Binding
        # ``/usr/bin/false`` alone made every refusal look identical — it ignores
        # its arguments, so the agent saw "exit 1, empty output" and could not
        # distinguish a profile refusal from a broken binary or a dead runtime.
        # It retried the same rejected shape, and diagnosed a stale deployment
        # that was in fact current. The refusal is served from here without
        # executing anything; the argv below stays as defense in depth, so a
        # future refactor that drops this channel still fails closed rather than
        # reaching a shell.
        args["mimir_shell_refusal"] = (
            f"{tool_name} was refused before execution: {refusal} "
            f"binding_rule={binding_rule.value if binding_rule is not None else 'unknown'}"
        )
        args["mimir_direct_argv"] = [
            "/usr/bin/false",
            "trusted-service shell argv binding failed closed",
        ]
        tool_call = {**request.tool_call, "args": args}
        return request.override(tool_call=tool_call)
    project_test_cwd = configured_project_test_cwd(argv)
    if project_test_cwd is not None and tool_name == "bash_async":
        args["mimir_shell_refusal"] = (
            "bash_async was refused before execution: "
            "project_test_async_refused; configured project tests use shell_exec "
            "so their wall-clock and returned output stay bounded."
        )
        return sanitized_request.override(
            tool_call={**request.tool_call, "args": args}
        )
    if project_test_cwd is not None:
        resolved_cwd, cwd_refusal = project_test_cwd, None
    elif Path(argv[0]).name == "chainlink":
        resolved_cwd, cwd_refusal = _resolve_chainlink_service_cwd()
    else:
        resolved_cwd, cwd_refusal = _resolve_service_shell_cwd(
            args.get("cwd"), service,
        )
    if cwd_refusal is not None:
        args["mimir_shell_refusal"] = (
            f"{tool_name} was refused before execution: {cwd_refusal}."
        )
        return sanitized_request.override(
            tool_call={**request.tool_call, "args": args}
        )
    confined_argv, confinement_refusal, confinement_rule = (
        parse_service_shell_argv_with_diagnostics(
            target,
            policy.destination,
            review_state=review_state if policy.destination == "repo_review" else None,
            declared=getattr(service, "declared_shell_commands", ()) or (),
            service=service,
            auth_context=auth_context,
            read_cwd=resolved_cwd,
        )
    )
    if confined_argv is None:
        args["mimir_shell_refusal"] = (
            f"{tool_name} was refused before execution: {confinement_refusal} "
            f"binding_rule={confinement_rule.value if confinement_rule is not None else 'unknown'}"
        )
        return sanitized_request.override(
            tool_call={**request.tool_call, "args": args}
        )
    argv = confined_argv
    if resolved_cwd is not None:
        args["cwd"] = resolved_cwd
    args["mimir_direct_argv"] = argv
    tool_call = {**request.tool_call, "args": args}
    return request.override(tool_call=tool_call)


def _request_with_resolved_service_write_path(
    request: ToolCallRequest,
    tool_name: str,
    auth_context: AuthContext | None,
) -> ToolCallRequest:
    """Bind a trigger-service file write to the path checked by authorization."""
    if tool_name not in {"write_file", "edit_file"}:
        return request
    service = get_trusted_service_from_auth_context(auth_context)
    policy = service.sink_policy_for(tool_name) if service is not None else None
    if policy is None or policy.adapter != "trigger_service_write_roots":
        return request

    from ..access_control import (
        _target_within_active_pr_checkout_lease,
        resolve_trigger_service_write_target,
    )

    args = dict((getattr(request, "tool_call", None) or {}).get("args") or {})
    argument_name = "file_path" if "file_path" in args else "path"
    raw_path = args.get(argument_name)
    if not isinstance(raw_path, str) or not raw_path:
        return request
    try:
        review_state, _ = resolve_repository_review_state(
            auth_context, path=raw_path,
        )
        if _target_within_active_pr_checkout_lease(raw_path, review_state):
            args[argument_name] = str(Path(raw_path).resolve(strict=False))
        else:
            args[argument_name] = str(
                resolve_trigger_service_write_target(raw_path, policy.destination)
            )
    except (OSError, RuntimeError, ValueError):
        # Leave the original destination intact so the sink adapter denies it.
        return request
    return request.override(tool_call={**request.tool_call, "args": args})


def _request_with_resolved_spawn_paths(
    request: ToolCallRequest,
    tool_name: str,
    auth_context: AuthContext | None,
) -> ToolCallRequest:
    """Bind service spawn execution to the paths checked by authorization."""
    if tool_name != "spawn_open_code":
        return request
    service = get_trusted_service_from_auth_context(auth_context)
    policy = service.sink_policy_for(tool_name) if service is not None else None
    if policy is None or policy.adapter != "spawn_workspace":
        return request

    from .._paths import PathOutsideHomeError
    from ..access_control import resolve_configured_write_target

    args = dict((getattr(request, "tool_call", None) or {}).get("args") or {})
    paths = ["cwd"]
    if tool_name == "spawn_open_code":
        paths.append("artifact_root")
    try:
        for name in paths:
            raw_path = args.get(name)
            if name == "cwd" and not raw_path:
                raw_path = os.environ.get("MIMIR_HOME")
            if raw_path:
                args[name] = str(resolve_configured_write_target(str(raw_path)))
    except (OSError, PathOutsideHomeError):
        # Leave the original destination intact so the sink adapter denies it.
        return request
    return request.override(tool_call={**request.tool_call, "args": args})


def _is_sequence_annotation(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _is_sequence_annotation(get_args(annotation)[0])
    if origin in (Union, UnionType):
        return any(_is_sequence_annotation(item) for item in get_args(annotation))
    candidate = origin or annotation
    return (
        isinstance(candidate, type)
        and issubclass(candidate, Sequence)
        and candidate not in (str, bytes, bytearray)
    )


def _record_argument_validation_failure(
    exc: Exception, parameter_names: set[str],
) -> None:
    """Log validation structure without rendering argument values."""
    try:
        details: list[str] = []
        errors = getattr(exc, "errors", None)
        if callable(errors):
            try:
                entries = errors(
                    include_url=False, include_context=False, include_input=False,
                )
            except TypeError:
                entries = errors()
            for entry in entries:
                location = next(
                    (
                        part for part in entry.get("loc", ())
                        if isinstance(part, str) and part in parameter_names
                    ),
                    "<arguments>",
                )
                error_type = str(entry.get("type", "validation_error"))
                if re.fullmatch(r"[A-Za-z0-9_.-]+", error_type) is None:
                    error_type = "validation_error"
                details.append(f"{location}: {error_type}")
        reason = "; ".join(details) or "schema validation failed"
        log.warning("tool argument validation failed: %s: %s", type(exc).__name__, reason)
    except Exception:  # noqa: BLE001 - diagnostics must not affect authorization
        pass


def _validated_arguments(request: ToolCallRequest) -> dict[str, Any] | None:
    """Validate model-supplied arguments before authz, excluding injected fields."""
    tool_call = getattr(request, "tool_call", None) or {}
    arguments = tool_call.get("args", {})
    if not isinstance(arguments, dict):
        return None
    tool = getattr(request, "tool", None)
    schema = getattr(tool, "tool_call_schema", None)
    if schema is None:
        schema = getattr(tool, "args_schema", None)
    if schema is None:
        return dict(arguments)
    normalized = dict(arguments)
    for name, field in schema.model_fields.items():
        value = normalized.get(name)
        if (
            _is_sequence_annotation(field.annotation)
            and isinstance(value, Mapping)
            and set(value) == {"item"}
        ):
            normalized[name] = value["item"]
    try:
        validated = schema.model_validate(normalized)
    except Exception as exc:
        _record_argument_validation_failure(exc, set(schema.model_fields))
        return None
    return validated.model_dump(exclude_unset=False)


def _argument_validation_refusal(request: ToolCallRequest) -> str:
    """Describe a schema refusal without rendering model-supplied values."""
    tool_call = getattr(request, "tool_call", None) or {}
    arguments = tool_call.get("args", {})
    if not isinstance(arguments, dict):
        return "Tool arguments must be an object. Fix the error and try again."
    tool = getattr(request, "tool", None)
    schema = getattr(tool, "tool_call_schema", None)
    if schema is None:
        schema = getattr(tool, "args_schema", None)
    if schema is None:
        return "Tool argument validation failed. Fix the error and try again."
    try:
        schema.model_validate(arguments)
    except Exception as exc:
        errors = getattr(exc, "errors", None)
        if callable(errors):
            try:
                entries = errors(
                    include_url=False, include_context=False, include_input=False,
                )
            except TypeError:
                entries = errors()
            details = []
            for entry in entries:
                field = next(
                    (part for part in entry.get("loc", ()) if isinstance(part, str)),
                    "arguments",
                )
                reason = "Field required" if entry.get("type") == "missing" else "Invalid value"
                details.append(f"{field}: {reason}")
            if details:
                return (
                    f"Tool argument validation failed: {'; '.join(details)}. "
                    "Fix the error and try again."
                )
    return "Tool argument validation failed. Fix the error and try again."


_IFC_DELEGATION_TOOLS = frozenset({
    "task",
    "spawn_open_code",
    "bash_async",
})


def _get_current_turn_context() -> Any:
    from .._context import get_current_turn

    return get_current_turn()


def _current_ifc_labels(auth_context: AuthContext | None) -> Any:
    """Read live labels from this exact request, including fork-visible updates."""
    if auth_context is None:
        return None
    state = getattr(auth_context, "ifc_state", None)
    current = getattr(state, "current", None)
    if not callable(current):
        return None
    active_ctx = _get_current_turn_context()
    if (
        active_ctx is not None
        and getattr(active_ctx, "auth_context", None) is not None
        and getattr(active_ctx.auth_context, "ifc_state", None) is state
    ):
        labels = getattr(active_ctx, "ifc_labels", None)
        if labels is not None:
            try:
                return current(labels)
            except Exception:
                log.exception("ifc_current_state_evaluation_failed")
                return None
    try:
        return current(auth_context.ifc_labels)
    except Exception:
        log.exception("ifc_current_state_evaluation_failed")
        return None


def _merge_result_labels(auth_context: AuthContext | None, added: Any) -> None:
    """Monotonically taint the exact turn and rebind harness egress."""
    if auth_context is None or added is None:
        return
    merged = auth_context.ifc_state.merge(added, fallback=auth_context.ifc_labels)
    active_ctx = _get_current_turn_context()
    if active_ctx is None:
        return
    active_auth = getattr(active_ctx, "auth_context", None)
    if active_auth is not None and active_auth.ifc_state is not auth_context.ifc_state:
        return
    from dataclasses import replace

    active_ctx.ifc_labels = merged
    active_ctx.auth_context = replace(active_auth or auth_context, ifc_labels=merged)
    emitter = getattr(active_ctx, "turn_event_emitter", None)
    if emitter is not None:
        emitter.bind_information_flow(merged, active_ctx.auth_context)


def _result_labels_for_call(
    tool_name: str,
    request: ToolCallRequest,
    auth_context: AuthContext | None,
    authorization: ToolAuthorization,
    *,
    result: ToolMessage | Command | None = None,
    provenance: Any = None,
    policy_refusal: ToolPolicyRefusal | None = None,
    failed: bool = False,
) -> Any:
    if not failed and tool_name == "repo_checkout" and auth_context is not None:
        args = _validated_arguments(request) or {}
        original_scope = authorization.repo_pr_action_scope
        repository = args.get("repository")
        pull_request = args.get("pull_request")
        discovered = auth_context.server_discovered_pr_states.resolve(
            repository, pull_request,
        ) if isinstance(repository, str) and isinstance(pull_request, int) else None
        state = discovered
        if state is None and auth_context.repo_pr_scope_registry is not None:
            state = auth_context.repo_pr_scope_registry.resolve(repository, pull_request)
        current_scope = state.action_scope if state is not None else None
        if (
            original_scope is not None
            and current_scope is not None
            and original_scope.canonical_repo == current_scope.canonical_repo
            and original_scope.pr_number == current_scope.pr_number
        ):
            # Checkout may revalidate a stale poller snapshot while it executes.
            # Label the returned bytes with the scope that actually produced them.
            from dataclasses import replace

            authorization = replace(
                authorization, repo_pr_action_scope=current_scope,
            )
    return classify_protected_result(
        tool_name,
        _validated_arguments(request),
        auth_context,
        authorization,
        result=result,
        provenance=provenance,
        policy_refusal=policy_refusal,
        failed=failed,
    )


def _is_admin_sensitive_tool(
    tool_name: str,
    ctx: AuthContext | None = None,
    target_channel: str | None = None,
) -> bool:
    """Return whether the live decision surface requires a privileged check."""
    auth = get_tool_registry().authorize_tool(
        tool_name,
        ctx,
        enforce=bool(ctx is not None and ctx.enforcement_enabled),
        target_channel=target_channel,
    )
    return auth.required_tier.value == "admin" or not auth.allowed


def _admin_denial_message(
    tool_name: str, reason: str | None, detail: str | None = None,
) -> str:
    # A shell-profile refusal is a command-shape problem, not a privilege
    # problem: no identity can run the command as written. Leading with
    # "requires an admin identity" and appending the real cause gives two
    # incompatible diagnoses and keeps pointing the caller at an identity
    # change, which is the failure mode this exists to remove. So when a detail
    # identifies the actual refusal, it replaces the admin wording rather than
    # trailing it — and the enforced path then reads identically to the
    # argv-binding path, which is the same refusal seen at a different gate.
    reason_text = f" ({reason})" if reason else ""
    if detail:
        return f"{tool_name} was refused before execution{reason_text}: {detail}"
    return (
        f"{tool_name} requires an admin identity{reason_text}. "
        "The tool call was refused before execution."
    )


def _env_access_control_enforced() -> bool:
    raw = os.environ.get("MIMIR_ACCESS_CONTROL_ENFORCED")
    return bool(
        raw is not None
        and raw != ""
        and raw.strip().lower() in {"1", "true", "yes", "on", "y"}
    )


def _turn_has_http_event_ingress(ctx: Any) -> bool:
    ingress = getattr(ctx, "event_ingress", None)
    return isinstance(ingress, str) and ingress.strip() == HTTP_EVENT_INGRESS_EXTRA_VALUE


def _admin_identity_fields(ctx: Any | None) -> tuple[str | None, str | None, list[str]]:
    if ctx is None:
        return None, None, []

    return (
        getattr(ctx, "principal", None),
        getattr(ctx, "canonical_principal", None),
        list(getattr(ctx, "roles", ()) or ()),
    )


def _deny_admin_tool(
    tool_name: str,
    reason: str,
    *,
    ctx: Any | None,
    enforcement_enabled: bool,
    target: Any = None,
    detail: str | None = None,
) -> str:
    author, canonical_author, roles = _admin_identity_fields(ctx)
    _emit_event_sync(
        "admin_tool_call_denied",
        tool=tool_name,
        allowed=False,
        status="denied",
        required_tier="admin",
        denial_reason=reason,
        author=author,
        canonical_author=canonical_author,
        roles=roles,
        enforcement_enabled=enforcement_enabled,
    )
    _emit_event_sync(
        "tool_call_denied",
        tool=tool_name,
        reason=reason,
        required_tier="admin",
        author=author,
        canonical_author=canonical_author,
    )
    if reason == _HTTP_EVENT_ADMIN_DENIAL_REASON:
        _emit_hard_boundary_denied(
            tool=tool_name,
            boundary="http_event_ingress",
            reason=reason,
            target=target,
            auth_context=ctx if isinstance(ctx, AuthContext) else None,
        )
    # ``reason`` stays the machine key on both events; the prose detail is for
    # the caller's tool result only, so the audit stream keeps grouping cleanly.
    return _admin_denial_message(tool_name, reason, detail)


def _check_admin_authorized(
    tool_name: str,
    ctx: Any | None = None,
    target_channel: str | None = None,
    ifc_labels: Any = None,
    mcp_tool: Any = None,
    arguments: dict[str, Any] | None = None,
    *,
    operator_shell_binding: OperatorShellBinding | None = None,
    operator_shell_refusal: str | None = None,
    operator_shell_request_identity: Any = None,
    operator_shell_audit: Mapping[str, str] | None = None,
    tool_call_id: str | None = None,
) -> str | None:
    _, denial = _authorize_tool_call(
        tool_name,
        ctx,
        target_channel,
        ifc_labels,
        mcp_tool,
        arguments,
        operator_shell_binding=operator_shell_binding,
        operator_shell_refusal=operator_shell_refusal,
        operator_shell_request_identity=operator_shell_request_identity,
        operator_shell_audit=operator_shell_audit,
        tool_call_id=tool_call_id,
    )
    return denial


def _authorize_tool_call(
    tool_name: str,
    ctx: Any | None = None,
    target_channel: str | None = None,
    ifc_labels: Any = None,
    mcp_tool: Any = None,
    arguments: dict[str, Any] | None = None,
    *,
    operator_shell_binding: OperatorShellBinding | None = None,
    operator_shell_refusal: str | None = None,
    operator_shell_request_identity: Any = None,
    operator_shell_audit: Mapping[str, str] | None = None,
    tool_call_id: str | None = None,
) -> tuple[ToolAuthorization, str | None]:
    """Return the exact authorization and any middleware denial text."""
    enforce = (
        bool(getattr(ctx, "enforcement_enabled", False))
        if ctx is not None
        else _env_access_control_enforced()
    )
    operator_parameters = {}
    if operator_shell_request_identity is not None:
        operator_parameters = {
            "operator_shell_binding": operator_shell_binding,
            "operator_shell_refusal": operator_shell_refusal,
            "operator_shell_request_identity": operator_shell_request_identity,
            "operator_shell_audit": operator_shell_audit,
            "tool_call_id": tool_call_id,
        }
    auth = get_tool_registry().authorize_tool(
        tool_name,
        ctx,
        enforce=enforce,
        target_channel=target_channel,
        ifc_labels=ifc_labels,
        mcp_tool=mcp_tool,
        arguments=arguments,
        **operator_parameters,
    )
    # Generic HTTP credentials authenticate transport only.  Check operation
    # class before compatibility-mode shadow allowances: resource-scoped and
    # unknown calls are non-open even when their shadow decision says allowed.
    if (
        ctx is not None
        and _turn_has_http_event_ingress(ctx)
        and auth.decision is not OperationDecision.OPEN
    ):
        return auth, _deny_admin_tool(
            tool_name,
            _HTTP_EVENT_ADMIN_DENIAL_REASON,
            ctx=ctx,
            enforcement_enabled=enforce,
            target=target_channel,
        )

    privileged = auth.required_tier.value == "admin" or not auth.allowed
    if not privileged:
        return auth, None

    if ctx is None and enforce:
        return auth, _deny_admin_tool(
            tool_name,
            "missing_auth_context",
            ctx=None,
            enforcement_enabled=True,
        )

    if auth.allowed:
        return auth, None
    return auth, _deny_admin_tool(
        tool_name,
        auth.reason or "admin_required",
        ctx=ctx,
        enforcement_enabled=enforce,
        detail=auth.refusal_detail,
    )


def _bounded_tool_event_error(error: str) -> str:
    if len(error) <= _TOOL_EVENT_ERROR_LIMIT:
        return error
    remaining = _TOOL_EVENT_ERROR_LIMIT - len(_TOOL_EVENT_ELISION)
    head_length = remaining // 2
    return error[:head_length] + _TOOL_EVENT_ELISION + error[-(remaining - head_length):]


def _tool_event_arguments(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    if arguments is None:
        return {}
    from ..redaction import redact_payload

    summary: dict[str, Any] = {}
    for key in _TOOL_EVENT_ARGUMENT_ALLOWLIST:
        if key not in arguments:
            continue
        value = redact_payload(arguments[key])
        if isinstance(value, str):
            summary[key] = value[:_TOOL_EVENT_ARGUMENT_VALUE_LIMIT]
        elif value is None or isinstance(value, (bool, int, float)):
            summary[key] = value
        else:
            summary[key] = str(value)[:_TOOL_EVENT_ARGUMENT_VALUE_LIMIT]
    return summary


def _emit_tool_call_sync(
    tool_name: str,
    *,
    ok: bool,
    duration_ms: float | None = None,
    error: str | None = None,
    denied: bool = False,
    arguments: dict[str, Any] | None = None,
    operator_shell_audit: Mapping[str, str] | None = None,
) -> None:
    payload = {"tool": tool_name, "ok": ok}
    argument_summary = (
        {} if operator_shell_audit is not None else _tool_event_arguments(arguments)
    )
    if operator_shell_audit is not None:
        payload.update(operator_shell_audit)
    if argument_summary:
        payload["arguments"] = argument_summary
    if tool_name in _PULL_REQUEST_TOOLS and arguments is not None:
        payload["repository"] = arguments.get("repository")
        payload["pull_request"] = arguments.get("pull_request")
    if duration_ms is not None:
        payload["duration_ms"] = round(duration_ms, 3)
    if error:
        payload["error"] = (
            "operator_shell_tool_error"
            if operator_shell_audit is not None
            else _bounded_tool_event_error(error)
        )
    if denied:
        payload["denied"] = True
    _emit_event_sync("tool_call", **payload)
    if not ok:
        error_payload = {"tool": tool_name}
        if operator_shell_audit is not None:
            error_payload.update(operator_shell_audit)
        if argument_summary:
            error_payload["arguments"] = argument_summary
        if tool_name in _PULL_REQUEST_TOOLS and arguments is not None:
            error_payload["repository"] = arguments.get("repository")
            error_payload["pull_request"] = arguments.get("pull_request")
        if error:
            error_payload["error"] = (
                "operator_shell_tool_error"
                if operator_shell_audit is not None
                else _bounded_tool_event_error(error)
            )
        if denied:
            error_payload["denied"] = True
        # The companion ``tool_call(ok=false)`` event owns the dashboard's
        # error numerator. Mark this branch as paired so consumers that read
        # both event types don't double-count the same failed invocation.
        error_payload["paired_tool_call"] = True
        _emit_event_sync("tool_error", **error_payload)


def _execute_declassification_action(
    request: ToolCallRequest,
    auth_context: AuthContext | None,
    arguments: dict[str, Any] | None,
) -> ToolMessage:
    arguments = arguments or {}
    approved, outcome = approve_live_declassification(
        auth_context,
        sink_category=arguments.get("sink_category"),
        destination=arguments.get("destination"),
        reason=arguments.get("reason"),
    )
    content = (
        "One-use declassification approved for the exact destination."
        if approved
        else f"approve_declassification denied: {outcome}"
    )
    _emit_tool_call_sync(
        "approve_declassification",
        ok=approved,
        error=None if approved else content,
        denied=not approved,
    )
    return ToolMessage(
        content=content,
        tool_call_id=_tool_call_id(request),
        name="approve_declassification",
        status="success" if approved else "error",
    )


def _returned_value_is_error(tool_name: str, content: Any) -> bool:
    """Recognize only first-party prose and exact typed-result contracts."""
    text = content if isinstance(content, str) else str(content)
    result_prefixes = {tool_name}
    if tool_name == "mimir_get_turn":
        result_prefixes.add("get_turn")
    if any(
        text.startswith(f"{prefix} {outcome}")
        for prefix in result_prefixes
        for outcome in ("failed", "refused", "timed out")
    ):
        return True
    if tool_name == "worklink_run" and text.startswith(
        ("worklink_run shed:", "worklink_run skipped:")
    ):
        return True
    if tool_name == "worklink_run" and re.match(
        r"worklink_run #\d+: (?:blocked|failed)(?:\s|$)", text,
    ):
        return True
    if tool_name == "bash_job_output" and text.startswith("unknown job_id:"):
        return True
    if tool_name == "web_search" and text.startswith((
        "query is required.", "limit must be > 0.", "topic must be one of:",
        "time_range must be one of:", "timeout_seconds must be > 0.",
        "web_search is disabled ",
    )):
        return True
    if tool_name == "fetch_url" and text.startswith((
        "url is required.", "timeout_seconds must be > 0.",
        "max_bytes must be > 0.",
    )):
        return True
    if tool_name == "shell_exec" and text.startswith("exit="):
        first_line = text.partition("\n")[0]
        try:
            return int(first_line.removeprefix("exit=")) != 0
        except ValueError:
            return False

    if tool_name == "spawn_open_code":
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return False
        return (
            isinstance(value, dict)
            and frozenset(value) == _SPAWN_OPEN_CODE_RESULT_FIELDS
            and value.get("status") in _SPAWN_OPEN_CODE_ERROR_STATUSES
        )

    expected_fields: frozenset[str] | None = None
    if tool_name in _GIT_OPERATION_RESULT_TOOLS:
        expected_fields = _GIT_OPERATION_RESULT_FIELDS
    elif tool_name == "repo_test":
        expected_fields = _PROJECT_TEST_RESULT_FIELDS
    if expected_fields is None:
        return False
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and frozenset(value) == expected_fields
        and value.get("ok") is False
    )


def _result_is_error(tool_name: str, result: ToolMessage | Command) -> bool:
    if isinstance(result, ToolMessage):
        return (
            getattr(result, "status", None) == "error"
            or _returned_value_is_error(tool_name, getattr(result, "content", ""))
        )
    update = getattr(result, "update", None)
    messages = update.get("messages", ()) if isinstance(update, dict) else ()
    return any(
        isinstance(message, ToolMessage)
        and (
            getattr(message, "status", None) == "error"
            or _returned_value_is_error(tool_name, getattr(message, "content", ""))
        )
        for message in messages
    )


def _result_error_text(result: ToolMessage | Command) -> str | None:
    if not isinstance(result, ToolMessage):
        return None
    content = getattr(result, "content", "")
    text = content if isinstance(content, str) else str(content)
    return text[:500] if text else None


def _tool_refusal_message(request: ToolCallRequest, tool_name: str, exc: ToolException) -> ToolMessage:
    """Return an explicit tool refusal to the model without masking faults."""
    return ToolMessage(
        content=str(exc),
        tool_call_id=_tool_call_id(request),
        name=tool_name,
        status="error",
    )


def _check_prohibited(tool_name: str, request: "ToolCallRequest") -> str | None:
    """Return a prohibition message if this bash call is prohibited, else None."""
    if not is_bash_tool(tool_name):
        return None
    tc = getattr(request, "tool_call", None) or {}
    args = tc.get("args") if isinstance(tc, dict) else None
    command = None
    if isinstance(args, dict):
        command = next(
            (args[name] for name in ("command", "cmd", "script") if name in args),
            None,
        )
    if not isinstance(command, str) or not command.strip():
        return (
            "PROHIBITED_ACTION: shell tool call has no non-empty string "
            "command, cmd, or script argument; refused because it cannot be screened"
        )
    return check_prohibited_bash(command)


class BudgetGateMiddleware(AgentMiddleware):
    """Intercept model and tool calls at their exact LangGraph boundaries.

    Ordinary, built-in, and LangGraph-wrapped MCP tools authorize from
    ``ToolCallRequest.runtime.context``. Claude SDK tools have no exact carrier
    in the current hook API and therefore fail closed under enforcement.
    """

    def __init__(self) -> None:
        # Compatibility mode remains permissive, but every non-open decision is
        # emitted so operators can inspect what enforcement would have done.
        get_tool_registry().enable_shadow_logging()

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """Publish the final model-bound tool surface, then invoke the model.

        Authorization decisions do not consult this observational inventory, so
        replacing the snapshot cannot widen or narrow the current call's access.
        """
        get_tool_registry().register_runtime_tools(getattr(request, "tools", ()))
        return handler(request)

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Async counterpart to :meth:`wrap_model_call`."""
        get_tool_registry().register_runtime_tools(getattr(request, "tools", ()))
        return await handler(request)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        tool_name = _tool_name_from_request(request)
        auth_context = _auth_context_from_request(request)
        request = _strip_server_only_shell_args(request)
        request = _request_with_resolved_service_write_path(
            request, tool_name, auth_context,
        )
        request = _request_with_resolved_spawn_paths(request, tool_name, auth_context)
        validated_arguments = _validated_arguments(request)
        review_denial = _resolve_standing_review(
            tool_name, auth_context, validated_arguments,
        )
        if review_denial is not None:
            _record_tool_outcome(tool_name, refused_reason=review_denial)
            _emit_tool_call_sync(
                tool_name, ok=False, error=review_denial, denied=True,
                arguments=validated_arguments,
            )
            return ToolMessage(
                content=review_denial, tool_call_id=_tool_call_id(request),
                name=tool_name, status="error",
            )
        if validated_arguments is None and tool_name in _STANDING_REVIEW_TOOLS:
            refusal = _argument_validation_refusal(request)
            _record_tool_outcome(tool_name, refused_reason=refusal)
            _emit_tool_call_sync(
                tool_name, ok=False, error=refusal, denied=True, arguments=None,
            )
            return ToolMessage(
                content=refusal, tool_call_id=_tool_call_id(request),
                name=tool_name, status="error",
            )
        target_channels = _extract_sink_targets(request, auth_context)
        ifc_labels = _current_ifc_labels(auth_context)
        operator_shell_preparation = _prepare_operator_shell_execution(
            request, tool_name, auth_context, ifc_labels,
        )
        operator_shell_audit = _operator_shell_audit_summary(
            operator_shell_preparation,
        )

        authorization = None
        for target_channel in target_channels:
            target_authorization, admin_denial = _authorize_tool_call(
                tool_name,
                auth_context,
                target_channel,
                ifc_labels,
                getattr(request, "tool", None),
                validated_arguments,
                operator_shell_binding=(
                    operator_shell_preparation.binding
                    if operator_shell_preparation is not None
                    else None
                ),
                operator_shell_refusal=(
                    operator_shell_preparation.refusal
                    if operator_shell_preparation is not None
                    else None
                ),
                operator_shell_request_identity=(
                    request if operator_shell_preparation is not None else None
                ),
                operator_shell_audit=operator_shell_audit,
                tool_call_id=(
                    _tool_call_id(request)
                    if operator_shell_preparation is not None
                    else None
                ),
            )
            if authorization is None:
                # The first target is the operation target; later targets are
                # additional writable destinations that must also be admitted.
                authorization = target_authorization
            if admin_denial is not None:
                break
        hard_refusal = _operator_shell_hard_refusal(
            request, operator_shell_preparation, auth_context,
        )
        if hard_refusal is not None:
            return hard_refusal
        mutation_refusal = _operator_shell_chainlink_mutation_refusal(
            request, operator_shell_preparation, auth_context,
        )
        if mutation_refusal is not None:
            return mutation_refusal
        if admin_denial is not None:
            _emit_tool_call_sync(
                tool_name, ok=False, error=admin_denial, denied=True,
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            return ToolMessage(
                content=admin_denial,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )
        if validated_arguments is None:
            refusal = _argument_validation_refusal(request)
            _record_tool_outcome(tool_name, refused_reason=refusal)
            _emit_tool_call_sync(
                tool_name, ok=False, error=refusal, denied=True, arguments=None,
                operator_shell_audit=operator_shell_audit,
            )
            return ToolMessage(
                content=refusal, tool_call_id=_tool_call_id(request),
                name=tool_name, status="error",
            )
        if tool_name == "approve_declassification":
            denial = _check_and_increment_or_deny(tool_name)
            if denial is not None:
                _emit_tool_call_sync(
                    tool_name, ok=False, error=denial, denied=True,
                    arguments=validated_arguments,
                )
                return ToolMessage(
                    content=denial,
                    tool_call_id=_tool_call_id(request),
                    name=tool_name,
                    status="error",
                )
            return _execute_declassification_action(
                request, auth_context, validated_arguments,
            )
        result_labels = _result_labels_for_call(
            tool_name, request, auth_context, authorization,
        )

        # Destructive-action guardrail (chainlink #259): an accident
        # deterrent against force-push-to-main/master, NOT a security
        # boundary — the regex screens the command arg and is bypassable
        # (vars, $()); see prohibited_action_guard.py. Catches the honest
        # mistake, doesn't claim to stop a determined caller.
        prohibition = _check_prohibited(tool_name, request)
        if prohibition is not None:
            _emit_event_sync(
                "prohibited_action_blocked",
                tool=tool_name,
                reason=(
                    "prohibited_action"
                    if operator_shell_audit is not None
                    else prohibition[:200]
                ),
                **(operator_shell_audit or {}),
            )
            _emit_hard_boundary_denied(
                tool=tool_name,
                boundary="prohibited_action_guard",
                reason="prohibited_action",
                target=(
                    None
                    if operator_shell_audit is not None
                    else _extract_sink_target(request, auth_context)
                ),
                auth_context=auth_context,
                event_fields=(
                    dict(operator_shell_audit)
                    if operator_shell_audit is not None
                    else None
                ),
            )
            _emit_tool_call_sync(
                tool_name, ok=False, error=prohibition, denied=True,
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            return ToolMessage(
                content=prohibition,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )

        denial = _check_and_increment_or_deny(
            tool_name,
            target=_extract_sink_target(request, auth_context),
            auth_context=auth_context,
            operator_shell_audit=operator_shell_audit,
        )
        if denial is not None:
            _emit_tool_call_sync(
                tool_name, ok=False, error=denial, denied=True,
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            return ToolMessage(
                content=denial,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )

        # Delegation inherits the current turn's monotonic IFC carrier only
        # after every pre-execution gate admits the call.
        active_ctx = _get_current_turn_context()
        if active_ctx is not None and tool_name in _IFC_DELEGATION_TOOLS:
            from ..agent import _propagate_ifc_labels

            propagated = _propagate_ifc_labels(
                active_ctx.ifc_labels,
                getattr(auth_context, "channel_id", None),
                auth_context,
                derived_by=tool_name,
            )
            _merge_result_labels(auth_context, propagated)
        started = time.monotonic()
        execution_request = (
            _request_for_authorized_execution(
                request,
                tool_name,
                auth_context,
                operator_shell_preparation=operator_shell_preparation,
            )
            if operator_shell_preparation is not None
            else _request_for_authorized_execution(request, tool_name, auth_context)
        )
        # A profile refusal is served as the tool result. Nothing is executed,
        # so the caller reads why instead of an unexplained exit 1.
        service_shell_refusal = _service_shell_refusal(execution_request)
        if service_shell_refusal is not None:
            _record_tool_outcome(
                tool_name,
                refused_reason=service_shell_refusal,
                **(
                    {"operator_shell_audit": operator_shell_audit}
                    if operator_shell_audit is not None
                    else {}
                ),
            )
            _emit_tool_call_sync(
                tool_name, ok=False, error=service_shell_refusal, denied=True,
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            return ToolMessage(
                content=service_shell_refusal,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )
        if tool_name in {"write_file", "edit_file"}:
            from ..access_control import record_file_write_integrity

            if not record_file_write_integrity(
                _extract_sink_target(execution_request, auth_context),
                _current_ifc_labels(auth_context),
            ):
                refusal = "file write refused: integrity metadata could not be persisted"
                _record_tool_outcome(tool_name, refused_reason=refusal)
                _emit_tool_call_sync(
                    tool_name, ok=False, error=refusal, denied=True,
                    arguments=validated_arguments,
                )
                return ToolMessage(
                    content=refusal, tool_call_id=_tool_call_id(request),
                    name=tool_name, status="error",
                )
        direct_argv = execution_request.tool_call.get("args", {}).get("mimir_direct_argv")
        direct_argv_token = None
        review_claim = None
        capture_token = None
        provenance = None
        read_refusal_token = None
        policy_refusal = None
        fetch_token = None
        try:
            mutation_refusal = _operator_shell_chainlink_mutation_refusal(
                request, operator_shell_preparation, auth_context,
            )
            if mutation_refusal is not None:
                return mutation_refusal
            from .github_review_guard import (
                claim_review_submission,
                review_submission_from_request,
            )

            review_spec = review_submission_from_request(execution_request)
            review_claim = (
                claim_review_submission(review_spec)
                if review_spec is not None
                else None
            )
            operator_direct_argv = (
                list(operator_shell_preparation.binding.argv)
                if operator_shell_preparation is not None
                and operator_shell_preparation.outcome is OperatorShellPreparationOutcome.BOUND
                and operator_shell_preparation.binding is not None
                else None
            )
            if operator_direct_argv is not None:
                from ._shell_env import bind_direct_exec_argv

                direct_argv_token = bind_direct_exec_argv(operator_direct_argv)
            elif (
                tool_name in {"shell_exec", "bash_async"}
                and isinstance(direct_argv, list)
            ):
                from ._shell_env import bind_direct_exec_argv

                direct_argv_token = bind_direct_exec_argv(direct_argv)
            from ..access_control import (
                begin_protected_result_capture,
                end_protected_result_capture,
            )

            capture_token = begin_protected_result_capture()
            from ..read_policy import begin_read_policy_refusal_capture

            read_refusal_token = begin_read_policy_refusal_capture()
            authorized_fetch_urls = _authorized_fetch_urls_for_tool(
                tool_name, auth_context, _extract_sink_target(request, auth_context),
            )
            if authorized_fetch_urls is not None:
                from .web import begin_authorized_fetch

                fetch_token = begin_authorized_fetch(authorized_fetch_urls)
            if review_claim is not None and review_claim.duplicate:
                result = _duplicate_review_result(request, review_claim)
            else:
                result = handler(execution_request)
        except ToolException as exc:
            if capture_token is not None:
                provenance = end_protected_result_capture(capture_token)
                capture_token = None
            if read_refusal_token is not None:
                from ..read_policy import end_read_policy_refusal_capture

                policy_refusal = end_read_policy_refusal_capture(read_refusal_token)
                read_refusal_token = None
            _record_repo_review_checkout(
                execution_request, auth_context, failed=True,
            )
            if isinstance(exc, ToolPolicyRefusal):
                _record_tool_outcome(
                    tool_name,
                    refused_reason=str(exc),
                    **(
                        {"operator_shell_audit": operator_shell_audit}
                        if operator_shell_audit is not None
                        else {}
                    ),
                )
                # A server-authored refusal adds no result provenance, but it
                # still traverses the common label-accounting boundary.
                _merge_result_labels(auth_context, None)
            else:
                result_labels = _result_labels_for_call(
                    tool_name,
                    request,
                    auth_context,
                    authorization,
                    provenance=provenance,
                    failed=True,
                )
                _merge_result_labels(auth_context, result_labels)
            _emit_tool_call_sync(
                tool_name,
                ok=False,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error=str(exc),
                denied=True,
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            return _tool_refusal_message(request, tool_name, exc)
        except Exception as exc:
            if capture_token is not None:
                provenance = end_protected_result_capture(capture_token)
                capture_token = None
            if read_refusal_token is not None:
                from ..read_policy import end_read_policy_refusal_capture

                end_read_policy_refusal_capture(read_refusal_token)
                read_refusal_token = None
            result_labels = _result_labels_for_call(
                tool_name,
                request,
                auth_context,
                authorization,
                provenance=provenance,
                failed=True,
            )
            _merge_result_labels(auth_context, result_labels)
            _emit_tool_call_sync(
                tool_name,
                ok=False,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error=str(exc),
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            raise
        finally:
            if review_claim is not None:
                review_claim.release()
            if direct_argv_token is not None:
                from ._shell_env import reset_direct_exec_argv

                reset_direct_exec_argv(direct_argv_token)
            if fetch_token is not None:
                from .web import end_authorized_fetch

                end_authorized_fetch(fetch_token)
            if capture_token is not None:
                provenance = end_protected_result_capture(capture_token)
            if read_refusal_token is not None:
                from ..read_policy import end_read_policy_refusal_capture

                policy_refusal = end_read_policy_refusal_capture(read_refusal_token)
        is_error = _result_is_error(tool_name, result)
        if not is_error:
            _record_tool_outcome(tool_name)
        _record_repo_review_checkout(
            execution_request, auth_context, failed=is_error,
        )
        result_labels = _result_labels_for_call(
            tool_name,
            request,
            auth_context,
            authorization,
            result=result,
            provenance=provenance,
            policy_refusal=policy_refusal,
            failed=is_error,
        )
        _merge_result_labels(auth_context, result_labels)
        duration_ms = (time.monotonic() - started) * 1000.0
        _emit_tool_call_sync(
            tool_name,
            ok=not is_error,
            duration_ms=duration_ms,
            error=_result_error_text(result) if is_error else None,
            arguments=validated_arguments,
            operator_shell_audit=operator_shell_audit,
        )
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = _tool_name_from_request(request)
        auth_context = _auth_context_from_request(request)
        request = _strip_server_only_shell_args(request)
        request = _request_with_resolved_service_write_path(
            request, tool_name, auth_context,
        )
        request = _request_with_resolved_spawn_paths(request, tool_name, auth_context)
        validated_arguments = _validated_arguments(request)
        review_denial = await asyncio.to_thread(
            _resolve_standing_review, tool_name, auth_context, validated_arguments,
        )
        if review_denial is not None:
            _record_tool_outcome(tool_name, refused_reason=review_denial)
            _emit_tool_call_sync(
                tool_name, ok=False, error=review_denial, denied=True,
                arguments=validated_arguments,
            )
            return ToolMessage(
                content=review_denial, tool_call_id=_tool_call_id(request),
                name=tool_name, status="error",
            )
        if validated_arguments is None and tool_name in _STANDING_REVIEW_TOOLS:
            refusal = _argument_validation_refusal(request)
            _record_tool_outcome(tool_name, refused_reason=refusal)
            _emit_tool_call_sync(
                tool_name, ok=False, error=refusal, denied=True, arguments=None,
            )
            return ToolMessage(
                content=refusal, tool_call_id=_tool_call_id(request),
                name=tool_name, status="error",
            )
        target_channels = _extract_sink_targets(request, auth_context)
        ifc_labels = _current_ifc_labels(auth_context)
        operator_shell_preparation = _prepare_operator_shell_execution(
            request, tool_name, auth_context, ifc_labels,
        )
        operator_shell_audit = _operator_shell_audit_summary(
            operator_shell_preparation,
        )

        authorization = None
        for target_channel in target_channels:
            target_authorization, admin_denial = _authorize_tool_call(
                tool_name,
                auth_context,
                target_channel,
                ifc_labels,
                getattr(request, "tool", None),
                validated_arguments,
                operator_shell_binding=(
                    operator_shell_preparation.binding
                    if operator_shell_preparation is not None
                    else None
                ),
                operator_shell_refusal=(
                    operator_shell_preparation.refusal
                    if operator_shell_preparation is not None
                    else None
                ),
                operator_shell_request_identity=(
                    request if operator_shell_preparation is not None else None
                ),
                operator_shell_audit=operator_shell_audit,
                tool_call_id=(
                    _tool_call_id(request)
                    if operator_shell_preparation is not None
                    else None
                ),
            )
            if authorization is None:
                # The first target is the operation target; later targets are
                # additional writable destinations that must also be admitted.
                authorization = target_authorization
            if admin_denial is not None:
                break
        hard_refusal = _operator_shell_hard_refusal(
            request, operator_shell_preparation, auth_context,
        )
        if hard_refusal is not None:
            return hard_refusal
        mutation_refusal = _operator_shell_chainlink_mutation_refusal(
            request, operator_shell_preparation, auth_context,
        )
        if mutation_refusal is not None:
            return mutation_refusal
        if admin_denial is not None:
            _emit_tool_call_sync(
                tool_name, ok=False, error=admin_denial, denied=True,
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            return ToolMessage(
                content=admin_denial,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )
        if validated_arguments is None:
            refusal = _argument_validation_refusal(request)
            _record_tool_outcome(tool_name, refused_reason=refusal)
            _emit_tool_call_sync(
                tool_name, ok=False, error=refusal, denied=True, arguments=None,
                operator_shell_audit=operator_shell_audit,
            )
            return ToolMessage(
                content=refusal, tool_call_id=_tool_call_id(request),
                name=tool_name, status="error",
            )
        if tool_name == "approve_declassification":
            denial = _check_and_increment_or_deny(tool_name)
            if denial is not None:
                _emit_tool_call_sync(
                    tool_name, ok=False, error=denial, denied=True,
                    arguments=validated_arguments,
                )
                return ToolMessage(
                    content=denial,
                    tool_call_id=_tool_call_id(request),
                    name=tool_name,
                    status="error",
                )
            return _execute_declassification_action(
                request, auth_context, validated_arguments,
            )
        result_labels = _result_labels_for_call(
            tool_name, request, auth_context, authorization,
        )

        # Destructive-action guardrail (chainlink #259): an accident
        # deterrent against force-push-to-main/master, NOT a security
        # boundary — the regex screens the command arg and is bypassable
        # (vars, $()); see prohibited_action_guard.py. Catches the honest
        # mistake, doesn't claim to stop a determined caller.
        prohibition = _check_prohibited(tool_name, request)
        if prohibition is not None:
            _emit_event_sync(
                "prohibited_action_blocked",
                tool=tool_name,
                reason=(
                    "prohibited_action"
                    if operator_shell_audit is not None
                    else prohibition[:200]
                ),
                **(operator_shell_audit or {}),
            )
            _emit_hard_boundary_denied(
                tool=tool_name,
                boundary="prohibited_action_guard",
                reason="prohibited_action",
                target=(
                    None
                    if operator_shell_audit is not None
                    else _extract_sink_target(request, auth_context)
                ),
                auth_context=auth_context,
                event_fields=(
                    dict(operator_shell_audit)
                    if operator_shell_audit is not None
                    else None
                ),
            )
            _emit_tool_call_sync(
                tool_name, ok=False, error=prohibition, denied=True,
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            return ToolMessage(
                content=prohibition,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )

        denial = _check_and_increment_or_deny(
            tool_name,
            target=_extract_sink_target(request, auth_context),
            auth_context=auth_context,
            operator_shell_audit=operator_shell_audit,
        )
        if denial is not None:
            _emit_tool_call_sync(
                tool_name, ok=False, error=denial, denied=True,
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            return ToolMessage(
                content=denial,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )

        # Delegation inherits the current turn's monotonic IFC carrier only
        # after every pre-execution gate admits the call.
        active_ctx = _get_current_turn_context()
        if active_ctx is not None and tool_name in _IFC_DELEGATION_TOOLS:
            from ..agent import _propagate_ifc_labels

            propagated = _propagate_ifc_labels(
                active_ctx.ifc_labels,
                getattr(auth_context, "channel_id", None),
                auth_context,
                derived_by=tool_name,
            )
            _merge_result_labels(auth_context, propagated)
        started = time.monotonic()
        execution_request = (
            _request_for_authorized_execution(
                request,
                tool_name,
                auth_context,
                operator_shell_preparation=operator_shell_preparation,
            )
            if operator_shell_preparation is not None
            else _request_for_authorized_execution(request, tool_name, auth_context)
        )
        # A profile refusal is served as the tool result. Nothing is executed,
        # so the caller reads why instead of an unexplained exit 1.
        service_shell_refusal = _service_shell_refusal(execution_request)
        if service_shell_refusal is not None:
            _record_tool_outcome(
                tool_name,
                refused_reason=service_shell_refusal,
                **(
                    {"operator_shell_audit": operator_shell_audit}
                    if operator_shell_audit is not None
                    else {}
                ),
            )
            _emit_tool_call_sync(
                tool_name, ok=False, error=service_shell_refusal, denied=True,
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            return ToolMessage(
                content=service_shell_refusal,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )
        if tool_name in {"write_file", "edit_file"}:
            from ..access_control import record_file_write_integrity

            recorded = await asyncio.to_thread(
                record_file_write_integrity,
                _extract_sink_target(execution_request, auth_context),
                _current_ifc_labels(auth_context),
            )
            if not recorded:
                refusal = "file write refused: integrity metadata could not be persisted"
                _record_tool_outcome(tool_name, refused_reason=refusal)
                _emit_tool_call_sync(
                    tool_name, ok=False, error=refusal, denied=True,
                    arguments=validated_arguments,
                )
                return ToolMessage(
                    content=refusal, tool_call_id=_tool_call_id(request),
                    name=tool_name, status="error",
                )
        direct_argv = execution_request.tool_call.get("args", {}).get("mimir_direct_argv")
        direct_argv_token = None
        review_claim = None
        capture_token = None
        provenance = None
        read_refusal_token = None
        policy_refusal = None
        fetch_token = None
        try:
            mutation_refusal = _operator_shell_chainlink_mutation_refusal(
                request, operator_shell_preparation, auth_context,
            )
            if mutation_refusal is not None:
                return mutation_refusal
            from .github_review_guard import (
                claim_review_submission,
                review_submission_from_request,
            )

            review_spec = review_submission_from_request(execution_request)
            review_claim = (
                await _claim_review_submission_async(
                    lambda: claim_review_submission(review_spec)
                )
                if review_spec is not None
                else None
            )
            operator_direct_argv = (
                list(operator_shell_preparation.binding.argv)
                if operator_shell_preparation is not None
                and operator_shell_preparation.outcome is OperatorShellPreparationOutcome.BOUND
                and operator_shell_preparation.binding is not None
                else None
            )
            if operator_direct_argv is not None:
                from ._shell_env import bind_direct_exec_argv

                direct_argv_token = bind_direct_exec_argv(operator_direct_argv)
            elif (
                tool_name in {"shell_exec", "bash_async"}
                and isinstance(direct_argv, list)
            ):
                from ._shell_env import bind_direct_exec_argv

                direct_argv_token = bind_direct_exec_argv(direct_argv)
            from ..access_control import (
                begin_protected_result_capture,
                end_protected_result_capture,
            )

            capture_token = begin_protected_result_capture()
            from ..read_policy import begin_read_policy_refusal_capture

            read_refusal_token = begin_read_policy_refusal_capture()
            authorized_fetch_urls = _authorized_fetch_urls_for_tool(
                tool_name, auth_context, _extract_sink_target(request, auth_context),
            )
            if authorized_fetch_urls is not None:
                from .web import begin_authorized_fetch

                fetch_token = begin_authorized_fetch(authorized_fetch_urls)
            if review_claim is not None and review_claim.duplicate:
                result = _duplicate_review_result(request, review_claim)
            else:
                result = await handler(execution_request)
        except ToolException as exc:
            if capture_token is not None:
                provenance = end_protected_result_capture(capture_token)
                capture_token = None
            if read_refusal_token is not None:
                from ..read_policy import end_read_policy_refusal_capture

                policy_refusal = end_read_policy_refusal_capture(read_refusal_token)
                read_refusal_token = None
            _record_repo_review_checkout(
                execution_request, auth_context, failed=True,
            )
            if isinstance(exc, ToolPolicyRefusal):
                _record_tool_outcome(
                    tool_name,
                    refused_reason=str(exc),
                    **(
                        {"operator_shell_audit": operator_shell_audit}
                        if operator_shell_audit is not None
                        else {}
                    ),
                )
                # A server-authored refusal adds no result provenance, but it
                # still traverses the common label-accounting boundary.
                _merge_result_labels(auth_context, None)
            else:
                result_labels = _result_labels_for_call(
                    tool_name,
                    request,
                    auth_context,
                    authorization,
                    provenance=provenance,
                    failed=True,
                )
                _merge_result_labels(auth_context, result_labels)
            _emit_tool_call_sync(
                tool_name,
                ok=False,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error=str(exc),
                denied=True,
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            return _tool_refusal_message(request, tool_name, exc)
        except Exception as exc:
            if capture_token is not None:
                provenance = end_protected_result_capture(capture_token)
                capture_token = None
            if read_refusal_token is not None:
                from ..read_policy import end_read_policy_refusal_capture

                end_read_policy_refusal_capture(read_refusal_token)
                read_refusal_token = None
            result_labels = _result_labels_for_call(
                tool_name,
                request,
                auth_context,
                authorization,
                provenance=provenance,
                failed=True,
            )
            _merge_result_labels(auth_context, result_labels)
            _emit_tool_call_sync(
                tool_name,
                ok=False,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error=str(exc),
                arguments=validated_arguments,
                operator_shell_audit=operator_shell_audit,
            )
            raise
        finally:
            if review_claim is not None:
                review_claim.release()
            if direct_argv_token is not None:
                from ._shell_env import reset_direct_exec_argv

                reset_direct_exec_argv(direct_argv_token)
            if fetch_token is not None:
                from .web import end_authorized_fetch

                end_authorized_fetch(fetch_token)
            if capture_token is not None:
                provenance = end_protected_result_capture(capture_token)
            if read_refusal_token is not None:
                from ..read_policy import end_read_policy_refusal_capture

                policy_refusal = end_read_policy_refusal_capture(read_refusal_token)
        is_error = _result_is_error(tool_name, result)
        if not is_error:
            _record_tool_outcome(tool_name)
        _record_repo_review_checkout(
            execution_request, auth_context, failed=is_error,
        )
        result_labels = _result_labels_for_call(
            tool_name,
            request,
            auth_context,
            authorization,
            result=result,
            provenance=provenance,
            policy_refusal=policy_refusal,
            failed=is_error,
        )
        _merge_result_labels(auth_context, result_labels)
        duration_ms = (time.monotonic() - started) * 1000.0
        _emit_tool_call_sync(
            tool_name,
            ok=not is_error,
            duration_ms=duration_ms,
            error=_result_error_text(result) if is_error else None,
            arguments=validated_arguments,
            operator_shell_audit=operator_shell_audit,
        )
        return result
