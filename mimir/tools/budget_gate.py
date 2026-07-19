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
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ..models import AuthContext
from ..worklink.continuation import HTTP_EVENT_INGRESS_EXTRA_VALUE
from ..access_control import (
    OperationDecision,
    ToolAuthorization,
    approve_live_declassification,
    classify_protected_result,
    get_tool_registry,
    get_trusted_service_from_auth_context,
    parse_service_shell_argv,
)
from .prohibited_action_guard import check_prohibited_bash, is_bash_tool

log = logging.getLogger(__name__)


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
        "spawn_claude_code",
        "spawn_codex",
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
        from ..event_logger import log_event  # lazy: supports monkeypatching in tests
        loop = asyncio.get_running_loop()
        task = loop.create_task(log_event(kind, **kwargs))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        log.debug("budget event %s dropped: no running loop", kind)


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


def _check_and_increment_or_deny(tool_name: str, ctx: Any | None = None) -> str | None:
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
    if tool_name in {"send_message", "react", "fetch_channel_history"}:
        explicit_channel = args.get("channel_id")
        if explicit_channel:
            return str(explicit_channel)
        return auth_context.channel_id if auth_context is not None else None
    if tool_name in {"write_file", "edit_file"}:
        target = args.get("file_path") or args.get("path")
    elif tool_name in {"shell_exec", "bash_async"}:
        target = args.get("command")
    elif tool_name in {"spawn_claude_code", "spawn_codex", "spawn_open_code"}:
        target = args.get("cwd") or os.environ.get("MIMIR_HOME")
    elif tool_name == "worklink_run":
        target = os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO")
    elif tool_name in {"fetch_url", "http_request", "webhook"}:
        target = args.get("url")
    elif tool_name == "web_search":
        from .web import DEFAULT_TAVILY_SEARCH_URL

        target = os.environ.get("TAVILY_SEARCH_URL", "").strip() or DEFAULT_TAVILY_SEARCH_URL
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


# Compatibility alias for callers that only exercise channel operations.
_extract_channel_from_args = _extract_sink_target


def _request_for_authorized_execution(
    request: ToolCallRequest,
    tool_name: str,
    auth_context: AuthContext | None,
) -> ToolCallRequest:
    """Bind trusted-service shell execution to the argv authorization checked.

    Ordinary user/admin shell tools keep their documented full-shell surface.
    A trusted service receives only the direct argv admitted by its operation-
    specific sink policy; the handler never sees the original command string.
    """
    if tool_name not in {"shell_exec", "bash_async"}:
        return request
    args = dict((getattr(request, "tool_call", None) or {}).get("args") or {})
    # Never trust a model-supplied internal execution override. Ordinary calls
    # discard it; trusted-service calls below replace it with server-parsed argv.
    had_model_override = "mimir_direct_argv" in args
    args.pop("mimir_direct_argv", None)
    sanitized_request = (
        request.override(tool_call={**request.tool_call, "args": args})
        if had_model_override
        else request
    )
    service = get_trusted_service_from_auth_context(auth_context)
    policy = service.sink_policy_for(tool_name) if service is not None else None
    if policy is None or policy.adapter != "shell_profile":
        return sanitized_request
    target = args.get("command")
    if not isinstance(target, str):
        return sanitized_request
    argv = parse_service_shell_argv(target, policy.destination)
    if argv is None:
        return sanitized_request
    args["mimir_direct_argv"] = argv
    tool_call = {**request.tool_call, "args": args}
    return request.override(tool_call=tool_call)


def _request_with_resolved_spawn_paths(
    request: ToolCallRequest,
    tool_name: str,
    auth_context: AuthContext | None,
) -> ToolCallRequest:
    """Bind service spawn execution to the paths checked by authorization."""
    if tool_name not in {"spawn_claude_code", "spawn_codex", "spawn_open_code"}:
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


def _validated_arguments(request: ToolCallRequest) -> dict[str, Any] | None:
    """Validate and normalize the concrete call arguments before authz."""
    tool_call = getattr(request, "tool_call", None) or {}
    arguments = tool_call.get("args", {})
    if not isinstance(arguments, dict):
        return None
    tool = getattr(request, "tool", None)
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return dict(arguments)
    try:
        validated = schema.model_validate(arguments)
    except Exception:
        return None
    return validated.model_dump(exclude_unset=False)


_IFC_DELEGATION_TOOLS = frozenset({
    "task",
    "spawn_claude_code",
    "spawn_codex",
    "spawn_open_code",
    "bash_async",
})


def _get_current_turn_context() -> Any:
    from .._context import get_current_turn

    return get_current_turn()


def _current_ifc_labels(auth_context: AuthContext | None) -> Any:
    """Read live labels from this exact request, including fork-visible updates."""
    active_ctx = _get_current_turn_context()
    if (
        active_ctx is not None
        and auth_context is not None
        and getattr(active_ctx, "auth_context", None) is not None
        and active_ctx.auth_context.ifc_state is auth_context.ifc_state
    ):
        labels = getattr(active_ctx, "ifc_labels", None)
        if labels is not None:
            return auth_context.ifc_state.current(labels)
    if auth_context is None:
        return None
    return auth_context.ifc_state.current(auth_context.ifc_labels)


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
    failed: bool = False,
) -> Any:
    return classify_protected_result(
        tool_name,
        _validated_arguments(request),
        auth_context,
        authorization,
        result=result,
        provenance=provenance,
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


def _admin_denial_message(tool_name: str, reason: str | None) -> str:
    reason_text = f" ({reason})" if reason else ""
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
    return _admin_denial_message(tool_name, reason)


def _check_admin_authorized(
    tool_name: str,
    ctx: Any | None = None,
    target_channel: str | None = None,
    ifc_labels: Any = None,
    mcp_tool: Any = None,
    arguments: dict[str, Any] | None = None,
) -> str | None:
    _, denial = _authorize_tool_call(
        tool_name,
        ctx,
        target_channel,
        ifc_labels,
        mcp_tool,
        arguments,
    )
    return denial


def _authorize_tool_call(
    tool_name: str,
    ctx: Any | None = None,
    target_channel: str | None = None,
    ifc_labels: Any = None,
    mcp_tool: Any = None,
    arguments: dict[str, Any] | None = None,
) -> tuple[ToolAuthorization, str | None]:
    """Return the exact authorization and any middleware denial text."""
    enforce = (
        bool(getattr(ctx, "enforcement_enabled", False))
        if ctx is not None
        else _env_access_control_enforced()
    )
    auth = get_tool_registry().authorize_tool(
        tool_name,
        ctx,
        enforce=enforce,
        target_channel=target_channel,
        ifc_labels=ifc_labels,
        mcp_tool=mcp_tool,
        arguments=arguments,
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
    )


def _emit_tool_call_sync(
    tool_name: str,
    *,
    ok: bool,
    duration_ms: float | None = None,
    error: str | None = None,
    denied: bool = False,
) -> None:
    payload = {"tool": tool_name, "ok": ok}
    if duration_ms is not None:
        payload["duration_ms"] = round(duration_ms, 3)
    if error:
        payload["error"] = error[:500]
    if denied:
        payload["denied"] = True
    _emit_event_sync("tool_call", **payload)
    if not ok:
        error_payload = {"tool": tool_name}
        if error:
            error_payload["error"] = error[:500]
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


def _result_is_error(result: ToolMessage | Command) -> bool:
    if isinstance(result, ToolMessage):
        return getattr(result, "status", None) == "error"
    update = getattr(result, "update", None)
    messages = update.get("messages", ()) if isinstance(update, dict) else ()
    return any(
        isinstance(message, ToolMessage) and getattr(message, "status", None) == "error"
        for message in messages
    )


def _result_error_text(result: ToolMessage | Command) -> str | None:
    if not isinstance(result, ToolMessage):
        return None
    content = getattr(result, "content", "")
    text = content if isinstance(content, str) else str(content)
    return text[:500] if text else None


def _check_prohibited(tool_name: str, request: "ToolCallRequest") -> str | None:
    """Return a prohibition message if this bash call is prohibited, else None."""
    if not is_bash_tool(tool_name):
        return None
    tc = getattr(request, "tool_call", None) or {}
    args = tc.get("args") or {}
    command = args.get("command", "")
    if not command:
        return None
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
        request = _request_with_resolved_spawn_paths(request, tool_name, auth_context)
        target_channels = _extract_sink_targets(request, auth_context)
        ifc_labels = _current_ifc_labels(auth_context)
        validated_arguments = _validated_arguments(request)

        for target_channel in target_channels:
            authorization, admin_denial = _authorize_tool_call(
                tool_name,
                auth_context,
                target_channel,
                ifc_labels,
                getattr(request, "tool", None),
                validated_arguments,
            )
            if admin_denial is not None:
                break
        if admin_denial is not None:
            _emit_tool_call_sync(tool_name, ok=False, error=admin_denial, denied=True)
            return ToolMessage(
                content=admin_denial,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )
        if tool_name == "approve_declassification":
            denial = _check_and_increment_or_deny(tool_name)
            if denial is not None:
                _emit_tool_call_sync(tool_name, ok=False, error=denial, denied=True)
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

        # Delegation inherits the current turn's monotonic IFC carrier. Built-in
        # subagents execute under this same context; detached spawn/async tools
        # preserve it for their continuation metadata.
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

        # Destructive-action guardrail (chainlink #259): an accident
        # deterrent against force-push-to-main/master, NOT a security
        # boundary — the regex screens the command arg and is bypassable
        # (vars, $()); see prohibited_action_guard.py. Catches the honest
        # mistake, doesn't claim to stop a determined caller.
        prohibition = _check_prohibited(tool_name, request)
        if prohibition is not None:
            _emit_event_sync("prohibited_action_blocked", tool=tool_name,
                             reason=prohibition[:200])
            _emit_tool_call_sync(
                tool_name, ok=False, error=prohibition, denied=True,
            )
            return ToolMessage(
                content=prohibition,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )

        denial = _check_and_increment_or_deny(tool_name)
        if denial is not None:
            _emit_tool_call_sync(tool_name, ok=False, error=denial, denied=True)
            return ToolMessage(
                content=denial,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )
        started = time.monotonic()
        execution_request = _request_for_authorized_execution(
            request, tool_name, auth_context,
        )
        from ..access_control import (
            begin_protected_result_capture,
            end_protected_result_capture,
        )

        capture_token = begin_protected_result_capture()
        try:
            result = handler(execution_request)
        except Exception as exc:
            end_protected_result_capture(capture_token)
            result_labels = _result_labels_for_call(
                tool_name, request, auth_context, authorization, failed=True,
            )
            _merge_result_labels(auth_context, result_labels)
            _emit_tool_call_sync(
                tool_name,
                ok=False,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error=str(exc),
            )
            raise
        provenance = end_protected_result_capture(capture_token)
        is_error = _result_is_error(result)
        result_labels = _result_labels_for_call(
            tool_name,
            request,
            auth_context,
            authorization,
            result=result,
            provenance=provenance,
            failed=is_error,
        )
        _merge_result_labels(auth_context, result_labels)
        duration_ms = (time.monotonic() - started) * 1000.0
        _emit_tool_call_sync(
            tool_name,
            ok=not is_error,
            duration_ms=duration_ms,
            error=_result_error_text(result) if is_error else None,
        )
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = _tool_name_from_request(request)
        auth_context = _auth_context_from_request(request)
        request = _request_with_resolved_spawn_paths(request, tool_name, auth_context)
        target_channels = _extract_sink_targets(request, auth_context)
        ifc_labels = _current_ifc_labels(auth_context)
        validated_arguments = _validated_arguments(request)

        for target_channel in target_channels:
            authorization, admin_denial = _authorize_tool_call(
                tool_name,
                auth_context,
                target_channel,
                ifc_labels,
                getattr(request, "tool", None),
                validated_arguments,
            )
            if admin_denial is not None:
                break
        if admin_denial is not None:
            _emit_tool_call_sync(tool_name, ok=False, error=admin_denial, denied=True)
            return ToolMessage(
                content=admin_denial,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )
        if tool_name == "approve_declassification":
            denial = _check_and_increment_or_deny(tool_name)
            if denial is not None:
                _emit_tool_call_sync(tool_name, ok=False, error=denial, denied=True)
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

        # Delegation inherits the current turn's monotonic IFC carrier. Built-in
        # subagents execute under this same context; detached spawn/async tools
        # preserve it for their continuation metadata.
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

        # Destructive-action guardrail (chainlink #259): an accident
        # deterrent against force-push-to-main/master, NOT a security
        # boundary — the regex screens the command arg and is bypassable
        # (vars, $()); see prohibited_action_guard.py. Catches the honest
        # mistake, doesn't claim to stop a determined caller.
        prohibition = _check_prohibited(tool_name, request)
        if prohibition is not None:
            _emit_event_sync("prohibited_action_blocked", tool=tool_name,
                             reason=prohibition[:200])
            _emit_tool_call_sync(
                tool_name, ok=False, error=prohibition, denied=True,
            )
            return ToolMessage(
                content=prohibition,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )

        denial = _check_and_increment_or_deny(tool_name)
        if denial is not None:
            _emit_tool_call_sync(tool_name, ok=False, error=denial, denied=True)
            return ToolMessage(
                content=denial,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )
        started = time.monotonic()
        execution_request = _request_for_authorized_execution(
            request, tool_name, auth_context,
        )
        from ..access_control import (
            begin_protected_result_capture,
            end_protected_result_capture,
        )

        capture_token = begin_protected_result_capture()
        try:
            result = await handler(execution_request)
        except Exception as exc:
            end_protected_result_capture(capture_token)
            result_labels = _result_labels_for_call(
                tool_name, request, auth_context, authorization, failed=True,
            )
            _merge_result_labels(auth_context, result_labels)
            _emit_tool_call_sync(
                tool_name,
                ok=False,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error=str(exc),
            )
            raise
        provenance = end_protected_result_capture(capture_token)
        is_error = _result_is_error(result)
        result_labels = _result_labels_for_call(
            tool_name,
            request,
            auth_context,
            authorization,
            result=result,
            provenance=provenance,
            failed=is_error,
        )
        _merge_result_labels(auth_context, result_labels)
        duration_ms = (time.monotonic() - started) * 1000.0
        _emit_tool_call_sync(
            tool_name,
            ok=not is_error,
            duration_ms=duration_ms,
            error=_result_error_text(result) if is_error else None,
        )
        return result
