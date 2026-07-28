"""Adapter validation and Mimir safety-plane hooks for Claude Code.

Upstream repo: https://github.com/thehumanworks/langchain-claude-code
(transferred from agentmish/langchain-claude-code)

This module validates that the installed adapter distribution is supported.
It also installs the SDK PreToolUse/PostToolUse safety-plane hooks that capture
every Claude Code tool invocation and enforce Mimir's budget and prohibited-
action policies for built-in, bridged, and MCP tools. Those hooks deliberately
remain here because they depend on Mimir's authorization model; they do not
belong in the general-purpose adapter distribution.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib.metadata as importlib_metadata
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

CONTROLLED_LANGCHAIN_CLAUDE_CODE_DIST = "langchain-claude-code-mimir"
UPSTREAM_LANGCHAIN_CLAUDE_CODE_DIST = "langchain-claude-code"

# These flags describe fixes already shipped by the controlled distribution.
# Keep the declaration path so a future adapter can assert native support without
# Mimir reintroducing the retired patches; the current distribution does not use it.
_REQUIRED_ADAPTER_FEATURES = frozenset(
    {
        "arun_config",
        "tool_call_schema",
        "streaming_result_metadata",
        "sdk_tool_events",
    }
)


@dataclass(frozen=True)
class AdapterCompatibility:
    supported: bool
    reason: str


def _module_declares_compatibility(module: Any) -> bool:
    """Return True when the adapter package explicitly advertises fixes.

    A future adapter can advertise support by exposing ``MIMIR_COMPATIBILITY``
    or ``__mimir_compatibility__`` as a mapping whose ``features`` iterable
    contains the required compatibility flags. This validates adapter features
    only; Mimir's safety-plane hooks are still always required and installed.
    """
    for attr in ("MIMIR_COMPATIBILITY", "__mimir_compatibility__"):
        compat = getattr(module, attr, None)
        if not isinstance(compat, dict):
            continue
        features = compat.get("features") or compat.get("adapter_features") or ()
        try:
            if _REQUIRED_ADAPTER_FEATURES.issubset(set(features)):
                return True
        except TypeError:
            continue
    return False


def _distribution_version(dist_name: str) -> str | None:
    try:
        return importlib_metadata.version(dist_name)
    except importlib_metadata.PackageNotFoundError:
        return None



def langchain_claude_code_adapter_compatibility(module: Any | None = None) -> AdapterCompatibility:
    """Validate that ``langchain_claude_code`` is a supported adapter build.

    Upstream/PyPI ``langchain-claude-code==0.1.0`` is known stale for Mimir:
    it lacks the LangChain Core 1.x ``_arun(config=...)`` fix, exposes
    ``InjectedToolArg`` fields through schemas, and drops metadata/hook data
    Mimir consumes. Supported paths are:

    * a controlled distribution named ``langchain-claude-code-mimir``;
    * an adapter module explicitly declaring all required compatibility
      features.
    """
    if module is None:
        try:
            import langchain_claude_code as module  # type: ignore[import-untyped,no-redef]
        except ImportError:
            return AdapterCompatibility(False, "langchain_claude_code is not installed")

    if _module_declares_compatibility(module):
        return AdapterCompatibility(True, "adapter declares Mimir compatibility features")

    controlled_version = _distribution_version(CONTROLLED_LANGCHAIN_CLAUDE_CODE_DIST)
    if controlled_version is not None:
        return AdapterCompatibility(
            True,
            f"{CONTROLLED_LANGCHAIN_CLAUDE_CODE_DIST}=={controlled_version} is installed",
        )

    upstream_version = _distribution_version(UPSTREAM_LANGCHAIN_CLAUDE_CODE_DIST)
    if upstream_version == "0.1.0":
        return AdapterCompatibility(
            False,
            "langchain-claude-code==0.1.0 is the stale PyPI adapter and is unsupported",
        )

    if upstream_version is not None:
        return AdapterCompatibility(
            False,
            f"{UPSTREAM_LANGCHAIN_CLAUDE_CODE_DIST}=={upstream_version} is not a verified Mimir adapter",
        )

    return AdapterCompatibility(
        False,
        "langchain_claude_code is importable but no supported distribution metadata was found",
    )


def assert_supported_langchain_claude_code_adapter(module: Any | None = None) -> None:
    status = langchain_claude_code_adapter_compatibility(module)
    if status.supported:
        return
    raise ImportError(
        "MIMIR_MODEL_SPEC=claude-code:* requires a maintained "
        "langchain_claude_code adapter. "
        f"{status.reason}. Install the controlled adapter distribution "
        f"with `pip install 'mimir-agent[claude-code]'` or "
        f"`pip install {CONTROLLED_LANGCHAIN_CLAUDE_CODE_DIST}`. Then "
        "install/authenticate the Claude Code CLI with `claude setup-token` "
        "or `claude login` and verify with `claude -p 'ping'`."
    )


# ContextVar carrying the per-call ``tool_events`` list. The hook
# callbacks installed by ``install_tool_event_hooks`` look up this
# value to know where to record events. ``None`` (the default) means
# "no active capture context" — hooks silently no-op.
_tool_events_var: contextvars.ContextVar[list[dict[str, Any]] | None] = (
    contextvars.ContextVar("mimir_claude_code_tool_events", default=None)
)


class _InvocationAuthCarrier:
    """Server-owned bridge from an SDK hook task to the current invocation.

    The random token is a generation guard, not a presented credential: the SDK
    hook signature has no place to carry a capability token, so ``resolve()``
    reads the active generation directly. Authority instead rests on the carrier
    ContextVar reaching only tasks forked after it is set, rebinding for every
    invocation with cleanup in ``finally``, and ``lock`` preventing overlapping
    invocations. ``clear(token)`` uses the generation guard so stale teardown
    cannot clear a newer binding.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._active_token: bytes | None = None
        self._bindings: dict[bytes, Any] = {}

    def bind(self, auth_context: Any) -> bytes:
        from .models import AuthContext

        token = secrets.token_bytes(32)
        self._bindings.clear()
        if isinstance(auth_context, AuthContext):
            self._bindings[token] = auth_context
        self._active_token = token
        return token

    def resolve(self) -> Any | None:
        if self._active_token is None:
            return None
        return self._bindings.get(self._active_token)

    def clear(self, token: bytes) -> None:
        self._bindings.pop(token, None)
        if self._active_token == token:
            self._active_token = None


_auth_carrier_var: contextvars.ContextVar[_InvocationAuthCarrier | None] = (
    contextvars.ContextVar("mimir_claude_code_auth_carrier", default=None)
)

_TOOL_EVENT_HOOKS_MARKER = "_mimir_tool_event_hooks_installed"


def _claude_code_permission_denial(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _record_claude_code_tool_result_denial(
    tool_name: str,
    tool_use_id: str,
    reason: str,
) -> None:
    events = _tool_events_var.get()
    if events is None:
        return
    events.append({
        "type": "tool_result",
        "ts_mono_ns": time.monotonic_ns(),
        "tool_use_id": tool_use_id,
        "name": tool_name,
        "error": reason,
        "is_error": True,
        "denied": True,
    })


def _claude_code_tool_duration_ms(tool_use_id: str) -> float | None:
    events = _tool_events_var.get()
    if events is None:
        return None
    for event in reversed(events):
        if (
            event.get("type") == "tool_call"
            and event.get("tool_use_id") == tool_use_id
        ):
            started = event.get("ts_mono_ns")
            if isinstance(started, int):
                return (time.monotonic_ns() - started) / 1_000_000.0
            return None
    return None


def _claude_code_pre_tool_enforcement(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    *,
    session_id: str | None = None,
    auth_context: Any | None = None,
) -> dict[str, Any]:
    """Run mimir's pre-execution gates for Claude Code SDK tools.

    This is the same boundary as the SDK's ``PreToolUse`` hook, so a deny
    result prevents the Claude Code subprocess/tool runtime from receiving the
    tool call. Keep the order aligned with ``BudgetGateMiddleware``: admin,
    prohibited bash, then budget.
    """
    from types import SimpleNamespace

    from .tools.budget_gate import (
        _check_admin_authorized,
        _check_and_increment_or_deny,
        _current_ifc_labels,
        _emit_event_sync,
        _emit_hard_boundary_denied,
        _emit_tool_call_sync,
        _extract_sink_target,
    )
    from .tools.prohibited_action_guard import check_prohibited_bash, is_bash_tool

    request = SimpleNamespace(tool_call={"name": tool_name, "args": tool_input})
    sink_target = _extract_sink_target(request, auth_context)
    admin_denial = _check_admin_authorized(
        tool_name,
        auth_context,
        sink_target,
        _current_ifc_labels(auth_context),
        arguments=tool_input,
    )
    if admin_denial is not None:
        _emit_tool_call_sync(tool_name, ok=False, error=admin_denial, denied=True)
        _record_claude_code_tool_result_denial(tool_name, tool_use_id, admin_denial)
        return _claude_code_permission_denial(admin_denial)

    if is_bash_tool(tool_name):
        command = tool_input.get("command", "")
        if isinstance(command, str) and command:
            prohibition = check_prohibited_bash(command)
            if prohibition is not None:
                _emit_event_sync(
                    "prohibited_action_blocked",
                    tool=tool_name,
                    reason=prohibition[:200],
                )
                _emit_hard_boundary_denied(
                    tool=tool_name,
                    boundary="prohibited_action_guard",
                    reason="prohibited_action",
                    target=command,
                    auth_context=auth_context,
                )
                _emit_tool_call_sync(
                    tool_name,
                    ok=False,
                    error=prohibition,
                    denied=True,
                )
                _record_claude_code_tool_result_denial(
                    tool_name,
                    tool_use_id,
                    prohibition,
                )
                return _claude_code_permission_denial(prohibition)

    # Budget accounting still uses TurnContext bookkeeping. It is not an
    # authorization decision; the exact frozen carrier above is the sole authz
    # source. The SDK session id may therefore recover only this non-authority
    # counter when the hook callback runs in a task without the turn ContextVar.
    from ._context import get_current_turn, get_turn_by_session_id

    budget_ctx = get_current_turn() or get_turn_by_session_id(session_id)
    denial = _check_and_increment_or_deny(
        tool_name,
        budget_ctx,
        target=sink_target,
        auth_context=auth_context,
    )
    if denial is not None:
        _emit_tool_call_sync(tool_name, ok=False, error=denial, denied=True)
        _record_claude_code_tool_result_denial(tool_name, tool_use_id, denial)
        return _claude_code_permission_denial(denial)

    return {}


# Third hook-callback param is the SDK's ``HookContext`` TypedDict —
# currently just ``{"signal": None}`` (reserved for future abort-signal
# support, see claude_agent_sdk/types.py:508). We don't use it; the
# leading-underscore name signals "unused" to future readers.
async def _pre_tool_use_hook(input_data: dict, tool_use_id: str, _ctx: Any) -> dict:
    """Append a tool_call event and deny unsafe calls before execution."""
    tool_name = str(input_data.get("tool_name") or "")
    session_id = input_data.get("session_id")
    session_id = session_id if isinstance(session_id, str) else None
    raw_input = input_data.get("tool_input", {})
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    events = _tool_events_var.get()
    if events is not None:
        events.append({
            "type": "tool_call",
            "ts_mono_ns": time.monotonic_ns(),
            "tool_use_id": tool_use_id,
            "name": tool_name,
            "input": tool_input,
        })
    return _claude_code_pre_tool_enforcement(
        tool_name,
        tool_input,
        tool_use_id,
        session_id=session_id,
        auth_context=(
            carrier.resolve()
            if (carrier := _auth_carrier_var.get()) is not None
            else None
        ),
    )


async def _post_tool_use_hook(input_data: dict, tool_use_id: str, _ctx: Any) -> dict:
    """Append a tool_result event (success) to the active capture list."""
    tool_name = str(input_data.get("tool_name") or "")
    duration_ms = _claude_code_tool_duration_ms(tool_use_id)
    from .tools.budget_gate import _emit_tool_call_sync

    _emit_tool_call_sync(tool_name, ok=True, duration_ms=duration_ms)
    events = _tool_events_var.get()
    if events is None:
        return {}
    events.append({
        "type": "tool_result",
        "ts_mono_ns": time.monotonic_ns(),
        "tool_use_id": tool_use_id,
        "name": tool_name,
        "result": input_data.get("tool_response"),
        "is_error": False,
    })
    return {}


async def _post_tool_use_failure_hook(
    input_data: dict, tool_use_id: str, _ctx: Any,
) -> dict:
    """Append a tool_result event (failure) to the active capture list."""
    tool_name = str(input_data.get("tool_name") or "")
    duration_ms = _claude_code_tool_duration_ms(tool_use_id)
    error = input_data.get("error")
    error_text = error if isinstance(error, str) else str(error) if error else None
    from .tools.budget_gate import _emit_tool_call_sync

    _emit_tool_call_sync(
        tool_name,
        ok=False,
        duration_ms=duration_ms,
        error=error_text,
    )
    events = _tool_events_var.get()
    if events is None:
        return {}
    events.append({
        "type": "tool_result",
        "ts_mono_ns": time.monotonic_ns(),
        "tool_use_id": tool_use_id,
        "name": tool_name,
        "error": error,
        "is_error": True,
    })
    return {}


def _auth_context_from_config(config: Any) -> Any | None:
    """Extract only the exact AuthContext attached to this graph invocation."""
    from .models import AuthContext

    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    runtime = configurable.get("__pregel_runtime")
    context = getattr(runtime, "context", None)
    return context if isinstance(context, AuthContext) else None


def _invocation_auth_context(config: Any = None) -> Any | None:
    """Resolve the graph runtime at dispatch, before an SDK hook task forks."""
    if config is None:
        try:
            from langgraph.config import get_config

            config = get_config()
        except RuntimeError:
            return None
    return _auth_context_from_config(config)


def _model_auth_carrier(model: Any) -> _InvocationAuthCarrier:
    carrier = model.__dict__.get("_mimir_invocation_auth_carrier")
    if not isinstance(carrier, _InvocationAuthCarrier):
        carrier = _InvocationAuthCarrier()
        model.__dict__["_mimir_invocation_auth_carrier"] = carrier
    return carrier


def _aquery_config(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if "config" in kwargs:
        return kwargs["config"]
    return args[1] if len(args) > 1 else None


def install_tool_event_hooks() -> None:
    """Monkey-patch ``ChatClaudeCode`` so every tool invocation —
    built-in (Bash/Read/Edit/Write/Glob/ToolSearch), langchain-bridged,
    or MCP — is recorded as a ``tool_events`` list in the result's
    ``generation_info``, ordered by arrival, paired by ``tool_use_id``.

    Three upstream gaps motivate this patch:

    * **Built-in tools never surface results.**  ``_aquery``/``_astream``
      only handle ``AssistantMessage`` + ``ResultMessage`` from the
      SDK. ``UserMessage`` — which carries ``ToolResultBlock``s for
      built-in tools — is dropped on the floor. The downstream
      ``turn_logger.extract_turn_events`` then records 60 ``tool_call``
      events with 0 corresponding ``tool_result`` events for a typical
      Bash/Read/Edit-heavy autonomous turn.

    * **Langchain-bridged tools pair by name, not id.**  The bridged
      tool wrapper (``_wrap_langchain_tool``) records results via a
      ContextVar with the bare ``@tool`` name (``"saga_feedback"``);
      the tool_call event carries the claude-code-bridged name
      (``"mcp__langchain-tools__saga_feedback"``). The ``tc_name_by_id``
      reverse-lookup added in turn_logger relies on ``tool_use_id`` —
      but the bridged capture path doesn't include one.

    * **Events arrive bunched, not interleaved.**  Within a single
      ``AssistantMessage``, ``_parse_assistant_message`` splits content
      blocks into parallel ``tool_calls`` / ``tool_results`` lists,
      losing the original block order.

    The SDK has explicit ``PreToolUse`` / ``PostToolUse`` /
    ``PostToolUseFailure`` hooks (claude_agent_sdk/types.py:265-292).
    Each hook fires from the CLI subprocess via control_protocol
    (``_internal/query.py:389``) for EVERY tool invocation regardless of
    origin, and carries ``tool_name``, ``tool_input``/``tool_response``,
    and ``tool_use_id``. Registering them gives us:

    * Full coverage: built-in + bridged + MCP tools all fire hooks.
    * Authoritative pairing: ``tool_use_id`` is on both pre and post.
    * Correct order: events are appended at arrival time, monotonic.

    Implementation:

    1. A ``ContextVar`` (``_tool_events_var``) carries the per-call
       events list. Set by the patched ``_aquery``/``_astream`` at entry,
       reset at exit. The hook callbacks look up the active list via
       ``ContextVar.get`` — no global state, no cross-call leakage.
    2. ``_build_options`` is wrapped to merge our three hook callbacks
       into ``options.hooks`` whenever an active capture context exists.
       User-provided hooks (e.g. permission gates) are preserved and
       appended to, not replaced.
    3. ``_aquery`` and ``_astream`` are wrapped: each call creates a
       fresh events list, runs the original method, and attaches the
       list to ``generation_info["tool_events"]`` on completion.
       ``_astream`` injects on the final chunk (the one carrying
       ``finish_reason``) so the result chunk's metadata is complete.

    Idempotent + import-safe: no-op when ``langchain-claude-code-mimir`` or
    ``claude-agent-sdk`` isn't installed. Re-running the installer skips
    application via the class-attribute marker.
    """
    try:
        from langchain_claude_code import claude_chat_model as ccm
    except ImportError:
        return

    try:
        from claude_agent_sdk import HookMatcher
    except ImportError:
        return

    if getattr(ccm.ClaudeCodeChatModel, _TOOL_EVENT_HOOKS_MARKER, False):
        return

    _orig_build_options = ccm.ClaudeCodeChatModel._build_options
    _orig_aquery = ccm.ClaudeCodeChatModel._aquery
    _orig_astream = ccm.ClaudeCodeChatModel._astream

    def _patched_build_options(self, **overrides: Any):  # type: ignore[no-untyped-def]
        options = _orig_build_options(self, **overrides)
        # Only inject hooks when there's an active capture context — keeps
        # behavior unchanged for any caller that builds options without
        # going through our patched _aquery / _astream.
        if _tool_events_var.get() is None:
            return options

        our_hooks: dict[str, list[Any]] = {
            "PreToolUse": [HookMatcher(hooks=[_pre_tool_use_hook])],
            "PostToolUse": [HookMatcher(hooks=[_post_tool_use_hook])],
            "PostToolUseFailure": [
                HookMatcher(hooks=[_post_tool_use_failure_hook])
            ],
        }

        # Preserve any user-supplied hooks (e.g. permission gates); our
        # callbacks always return ``{}`` so they don't influence control
        # flow even when chained with others.
        existing = dict(options.hooks) if options.hooks else {}
        for event, matchers in our_hooks.items():
            existing[event] = list(existing.get(event, [])) + matchers
        options.hooks = existing
        return options

    async def _patched_aquery(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        carrier = _model_auth_carrier(self)
        async with carrier.lock:
            binding = carrier.bind(_invocation_auth_context(_aquery_config(args, kwargs)))
            carrier_token = _auth_carrier_var.set(carrier)
            events: list[dict[str, Any]] = []
            events_token = _tool_events_var.set(events)
            try:
                content, tool_calls, generation_info = await _orig_aquery(
                    self, *args, **kwargs,
                )
                if events:
                    generation_info["tool_events"] = events
                return content, tool_calls, generation_info
            finally:
                carrier.clear(binding)
                _auth_carrier_var.reset(carrier_token)
                _tool_events_var.reset(events_token)

    async def _patched_astream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        carrier = _model_auth_carrier(self)
        async with carrier.lock:
            binding = carrier.bind(_invocation_auth_context(kwargs.get("_config")))
            carrier_token = _auth_carrier_var.set(carrier)
            events: list[dict[str, Any]] = []
            events_token = _tool_events_var.set(events)
            try:
                async for chunk in _orig_astream(self, *args, **kwargs):
                    gi = getattr(chunk, "generation_info", None)
                    # The result chunk is the one with ``finish_reason``; by
                    # the time it's yielded, all hooks for this stream have
                    # fired (SDK emits ResultMessage after the tool loop).
                    if gi and "finish_reason" in gi and events:
                        gi["tool_events"] = events
                        chunk.generation_info = gi
                    yield chunk
            finally:
                carrier.clear(binding)
                _auth_carrier_var.reset(carrier_token)
                _tool_events_var.reset(events_token)

    ccm.ClaudeCodeChatModel._build_options = _patched_build_options
    ccm.ClaudeCodeChatModel._aquery = _patched_aquery
    ccm.ClaudeCodeChatModel._astream = _patched_astream
    setattr(ccm.ClaudeCodeChatModel, _TOOL_EVENT_HOOKS_MARKER, True)
    log.debug(
        "installed tool-event hooks on ChatClaudeCode "
        "(_build_options, _aquery, _astream)",
    )


def ensure_tool_enforcement_hooks_installed(module: Any | None = None) -> None:
    """Fail closed unless Claude Code tool calls have a pre-execution guard.

    ``claude-code:*`` executes built-in, bridged LangChain, and MCP tools inside
    the Claude Code SDK subprocess path, bypassing LangGraph's tool middleware.
    Model resolution calls this before constructing ``ChatClaudeCode`` so the
    supported provider stays fail-closed whenever the SDK/adapter no longer
    exposes the hook surface Mimir needs.
    """
    try:
        from langchain_claude_code import claude_chat_model as ccm
    except ImportError as exc:
        raise RuntimeError(
            "claude-code tool enforcement unavailable: "
            "langchain_claude_code is not installed"
        ) from exc

    install_tool_event_hooks()
    if getattr(ccm.ClaudeCodeChatModel, _TOOL_EVENT_HOOKS_MARKER, False):
        return

    raise RuntimeError(
        "MIMIR_MODEL_SPEC=claude-code:* cannot start safely: Mimir could not "
        "install the Claude Code PreToolUse enforcement hook required to run "
        "the per-turn tool budget and prohibited-action guard before built-in, "
        "bridged, and MCP tools execute. Install a supported "
        "langchain_claude_code/claude_agent_sdk adapter or use anthropic:, "
        "openai:, or codex-plus:."
    )
