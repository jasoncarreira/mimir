"""Runtime checks and tool-safety hooks for ``langchain_claude_code``.

Upstream repo: https://github.com/thehumanworks/langchain-claude-code
(transferred from agentmish/langchain-claude-code)

The public PyPI distribution ``langchain-claude-code-mimir`` carries the
adapter-level fixes Mimir used to monkeypatch locally (LangChain Core 1.x
``_arun(config=...)``, injected-tool schema filtering, and streaming result
metadata). This module keeps Mimir-side validation plus the safety-plane hooks
that are deliberately *not* part of the adapter package: SDK PreToolUse /
PostToolUse callbacks that capture every Claude Code tool invocation and keep
pre-execution enforcement available for built-in, bridged, and MCP tools.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib.metadata as importlib_metadata
import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_PATCH_MARKER = "_mimir_arun_config_patched"

CONTROLLED_LANGCHAIN_CLAUDE_CODE_DIST = "langchain-claude-code-mimir"
UPSTREAM_LANGCHAIN_CLAUDE_CODE_DIST = "langchain-claude-code"

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

    The controlled adapter can avoid all Mimir-side monkeypatches by exposing
    either ``MIMIR_COMPATIBILITY`` or ``__mimir_compatibility__`` as a mapping
    whose ``features`` iterable contains the required compatibility flags.
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


def _adapter_has_native_mimir_compatibility() -> bool:
    try:
        import langchain_claude_code as lcc  # type: ignore[import-untyped]
    except ImportError:
        return False
    return _module_declares_compatibility(lcc)


# ContextVar carrying the per-call ``tool_events`` list. The hook
# callbacks installed by ``install_tool_event_hooks`` look up this
# value to know where to record events. ``None`` (the default) means
# "no active capture context" — hooks silently no-op.
_tool_events_var: contextvars.ContextVar[list[dict[str, Any]] | None] = (
    contextvars.ContextVar("mimir_claude_code_tool_events", default=None)
)




_DEEPAGENTS_BASE_PROMPT_MARKER = "_mimir_base_prompt_stripped"


_DEEPAGENTS_TOKEN_COUNTER_PATCH_MARKER = "_mimir_token_counter_tool_schema_cache"


def patch_deepagents_token_counter_tool_schema_cache() -> None:
    """Cache tool-schema conversion during DeepAgents token counting.

    DeepAgents' summarization middleware calls LangChain's approximate token
    counter with ``tools=request.tools`` on every model boundary. LangChain
    converts each ``BaseTool`` to an OpenAI tool dict for that count; for
    structured tools this walks ``tool_call_schema`` and builds Pydantic
    subset models. On large tool surfaces that synchronous schema conversion
    has shown up directly in scheduler event-loop lag stack captures.

    The conversion is pure for a stable tool object, so cache the converted
    dict per tool object (falling back to pass-through for already-converted
    dict schemas) before the counter runs. The patch is deliberately narrow:
    it wraps only the DeepAgents summarization module's imported
    ``count_tokens_approximately`` name, leaving LangChain's public helper
    unchanged for other callers/tests.
    """
    try:
        import copy
        import weakref

        from langchain.agents.middleware import summarization as lc_summarization
        from langchain_core.messages import utils as message_utils
        from langchain_core.tools import BaseTool
        import deepagents.middleware.summarization as summarization
    except ImportError:
        return

    current = getattr(summarization, "count_tokens_approximately", None)
    if getattr(current, _DEEPAGENTS_TOKEN_COUNTER_PATCH_MARKER, False):
        return

    original_counter = current or message_utils.count_tokens_approximately
    original_lc_counter = lc_summarization.count_tokens_approximately
    # Stock DeepAgents imports LangChain Core's helper into its module and
    # passes that exact function object through to LangChain's summarization
    # middleware. LangChain then uses object identity to detect the default and
    # replace it with a model-tuned partial. Keep the patched DeepAgents default
    # and LangChain module global as the SAME wrapper object so that identity
    # branch still fires.
    counter_to_wrap = (
        original_lc_counter if original_counter is original_lc_counter else original_counter
    )
    cache: dict[int, tuple[weakref.ReferenceType[BaseTool], dict[str, Any]]] = {}

    def _drop_cached_tool(tool_id: int) -> None:
        cache.pop(tool_id, None)

    def _cached_tools(tools: list[Any] | None) -> list[Any] | None:
        if not tools:
            return tools
        converted: list[Any] = []
        for tool in tools:
            if isinstance(tool, dict):
                converted.append(tool)
                continue
            if not isinstance(tool, BaseTool):
                converted.append(tool)
                continue
            tool_id = id(tool)
            cached = cache.get(tool_id)
            schema = None
            if cached is not None:
                ref, cached_schema = cached
                if ref() is tool:
                    schema = cached_schema
                else:
                    cache.pop(tool_id, None)
            if schema is None:
                schema = message_utils.convert_to_openai_tool(tool)
                try:
                    ref = weakref.ref(
                        tool,
                        lambda _ref, tid=tool_id: _drop_cached_tool(tid),
                    )
                    cache[tool_id] = (ref, schema)
                except TypeError:
                    # Extremely defensive: BaseTool instances are weakrefable
                    # in supported LangChain versions, but counting should still
                    # work if a custom tool subclass refuses weakrefs.
                    pass
            # The approximate counter only reads/json-dumps schemas today,
            # but return a defensive copy so downstream mutation cannot poison
            # the cached canonical schema.
            converted.append(copy.deepcopy(schema))
        return converted

    def _patched_count_tokens_approximately(  # type: ignore[no-untyped-def]
        messages,
        *args,
        tools=None,
        **kwargs,
    ):
        return counter_to_wrap(messages, *args, tools=_cached_tools(tools), **kwargs)

    setattr(_patched_count_tokens_approximately, _DEEPAGENTS_TOKEN_COUNTER_PATCH_MARKER, True)
    _patched_count_tokens_approximately.__wrapped__ = counter_to_wrap  # type: ignore[attr-defined]
    summarization.count_tokens_approximately = _patched_count_tokens_approximately
    lc_summarization.count_tokens_approximately = _patched_count_tokens_approximately

    # ``SummarizationMiddleware.__init__`` captured the module-level helper as
    # a keyword-only default when deepagents.middleware.summarization was
    # imported, so replacing the module global alone is not enough for the
    # stock ``create_summarization_middleware()`` factory. Update the default
    # used by future middleware instances too.
    kwdefaults = getattr(summarization.SummarizationMiddleware.__init__, "__kwdefaults__", None)
    if isinstance(kwdefaults, dict) and "token_counter" in kwdefaults:
        kwdefaults["token_counter"] = _patched_count_tokens_approximately
    lc_kwdefaults = getattr(
        lc_summarization.SummarizationMiddleware.__init__, "__kwdefaults__", None
    )
    if (
        isinstance(lc_kwdefaults, dict)
        and lc_kwdefaults.get("token_counter") is original_lc_counter
    ):
        lc_kwdefaults["token_counter"] = _patched_count_tokens_approximately

    log.debug("patched DeepAgents token counter to cache BaseTool schema conversion")

def strip_deepagents_base_prompt() -> None:
    """Empty out ``deepagents.graph.BASE_AGENT_PROMPT`` so it is NOT
    appended to mimir's system prompt.

    ``create_deep_agent`` composes the final system prompt as
    ``user_system_prompt + "\\n\\n" + BASE_AGENT_PROMPT`` (graph.py:754).
    The base block is a generic "be concise, do tasks well" framing
    that competes with mimir's own persona + filing-rules guidance.
    For mimir the user-supplied prompt is the entire contract: core
    memory blocks, memory-index, conventions, skill catalog, operator
    config — there is no value to bolting a second agent-shape framing
    onto the end of it. Match the SDK-era invariant where mimir's
    system_prompt was the only system_prompt the model saw.

    Idempotent + import-safe: a no-op when deepagents isn't installed
    (operators on the anthropic-only extra path don't pull it in)."""
    try:
        from deepagents import graph as _dg_graph
    except ImportError:
        return
    if getattr(_dg_graph, _DEEPAGENTS_BASE_PROMPT_MARKER, False):
        return
    _dg_graph.BASE_AGENT_PROMPT = ""
    setattr(_dg_graph, _DEEPAGENTS_BASE_PROMPT_MARKER, True)
    log.debug("stripped deepagents BASE_AGENT_PROMPT (mimir owns system prompt)")





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
) -> dict[str, Any]:
    """Run mimir's pre-execution gates for Claude Code SDK tools.

    This is the same boundary as the SDK's ``PreToolUse`` hook, so a deny
    result prevents the Claude Code subprocess/tool runtime from receiving the
    tool call. Keep the order aligned with ``BudgetGateMiddleware``: admin,
    prohibited bash, then budget.
    """
    from .tools.budget_gate import (
        _check_admin_authorized,
        _check_and_increment_or_deny,
        _emit_event_sync,
        _emit_tool_call_sync,
    )
    from .tools.prohibited_action_guard import check_prohibited_bash, is_bash_tool
    # The SDK hook API does not expose LangGraph Runtime.context, and its
    # callback task may be detached. Never substitute SDK/model session_id,
    # active-turn registries, or inherited ContextVars as authorization. Under
    # enforcement this missing exact carrier fails closed; in unenforced legacy
    # mode behavior remains open. Adapter-level carrier plumbing is follow-up
    # work before Claude SDK admin tools can be enabled under enforcement.
    admin_denial = _check_admin_authorized(tool_name, None)
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
    denial = _check_and_increment_or_deny(tool_name, budget_ctx)
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
        events: list[dict[str, Any]] = []
        token = _tool_events_var.set(events)
        try:
            content, tool_calls, generation_info = await _orig_aquery(
                self, *args, **kwargs,
            )
            if events:
                generation_info["tool_events"] = events
            return content, tool_calls, generation_info
        finally:
            _tool_events_var.reset(token)

    async def _patched_astream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        events: list[dict[str, Any]] = []
        token = _tool_events_var.set(events)
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
            _tool_events_var.reset(token)

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
