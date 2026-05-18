"""Runtime patches for the vendored ``langchain-claude-code`` package.

The version we depend on (0.1.0) was written against langchain-core 0.x.
In langchain-core 1.x, ``StructuredTool._arun`` made its ``config`` kwarg
required — so the upstream tool-wrapper call

    result = await tool._arun(**args)

now raises ``TypeError: _arun() missing 1 required keyword-only argument:
'config'`` on every tool invocation. The bench / production agent can't
call ANY langchain tool until this is patched.

This module monkey-patches the upstream ``_wrap_langchain_tool`` to pass
an empty ``RunnableConfig`` when calling ``_arun``. Applied at import
time when the ``langchain_claude_code`` package is present. Removable
once upstream lands the fix (PR filed at
https://github.com/agentmish/langchain-claude-code).

Idempotent: re-running ``apply_patches()`` after the first call is a
no-op. Safe to import unconditionally — if langchain-claude-code isn't
installed (e.g. operator only has the anthropic extra), the patch
function silently returns.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

_PATCH_MARKER = "_mimir_arun_config_patched"


# ContextVar carrying the per-call ``tool_events`` list. The hook
# callbacks installed by ``install_tool_event_hooks`` look up this
# value to know where to record events. ``None`` (the default) means
# "no active capture context" — hooks silently no-op.
_tool_events_var: contextvars.ContextVar[list[dict[str, Any]] | None] = (
    contextvars.ContextVar("mimir_claude_code_tool_events", default=None)
)


def apply_patches() -> None:
    """Apply runtime patches to ``ChatClaudeCode``. Idempotent + import-safe.

    Patches applied:

    1. **``_wrap_langchain_tool``** — passes an empty ``RunnableConfig`` to
       ``tool._arun`` so langchain-core 1.x's required-kwarg validation
       doesn't raise on every tool invocation.

    2. **``_get_tool_schema``** — uses ``tool.tool_call_schema`` (which
       correctly excludes ``InjectedToolArg`` parameters like ``config``)
       instead of ``tool.args_schema`` (which includes them). Without this,
       the MCP schema exposed to Claude Code lists ``config`` as a callable
       parameter; the model passes it; ``_arun`` then receives ``config``
       from both the caller args AND from patch #1's explicit injection →
       "got multiple values for keyword argument 'config'". The three
       affected tools are ``send_message``, ``react``, and
       ``fetch_channel_history`` (all use ``InjectedToolArg`` for their
       ``config: RunnableConfig`` parameter).

    Detects when the upstream ``_wrap_langchain_tool`` has been fixed
    (signature change or body no longer calling ``_arun`` without ``config``)
    and skips the patch in that case.
    """
    try:
        from langchain_claude_code import claude_chat_model as ccm
    except ImportError:
        # Operator hasn't installed the claude-code extra — nothing to
        # patch. Other model providers (anthropic, openai) are untouched.
        return

    if getattr(ccm.ClaudeCodeChatModel, _PATCH_MARKER, False):
        return

    _orig_wrap = ccm.ClaudeCodeChatModel._wrap_langchain_tool
    _orig_get_schema = ccm.ClaudeCodeChatModel._get_tool_schema

    # Upstream-fix detection: the original method we're patching had
    # this exact signature when the bug existed: ``(self, tool, schema)``.
    # If upstream renames the method, changes its parameters, or fixes
    # the underlying ``_arun`` call to pass ``config`` itself, our
    # shim is either irrelevant or actively harmful. Bail with a
    # warning so the operator notices and removes this module.
    import inspect as _inspect
    try:
        sig = _inspect.signature(_orig_wrap)
        params = list(sig.parameters.keys())
        expected = ["self", "tool", "schema"]
        if params != expected:
            log.warning(
                "langchain-claude-code _wrap_langchain_tool signature "
                "changed (got %s, expected %s) — skipping mimir's "
                "stale patch. Remove _langchain_claude_code_patches.py "
                "after verifying upstream behavior.", params, expected,
            )
            setattr(ccm.ClaudeCodeChatModel, _PATCH_MARKER, True)
            return
        # Heuristic: if upstream source already passes ``config=`` to
        # ``_arun``, the bug is fixed; skip.
        src = _inspect.getsource(_orig_wrap)
        if "_arun" in src and "config=" in src and "_arun(**args, config=" in src:
            log.info(
                "langchain-claude-code already passes config= to _arun — "
                "skipping mimir's now-redundant patch.",
            )
            setattr(ccm.ClaudeCodeChatModel, _PATCH_MARKER, True)
            return
    except (TypeError, OSError):
        # signature/getsource may fail for C-extension or oddly-wrapped
        # functions; fall through to apply the patch (current behavior).
        pass

    # ── Patch 1: _get_tool_schema ────────────────────────────────────
    # Use tool_call_schema (excludes InjectedToolArg fields) instead of
    # args_schema (includes them). This prevents config from appearing in
    # the MCP schema that Claude Code sees — Claude Code won't pass it,
    # so _arun won't receive it twice.
    def _patched_get_tool_schema(self, tool: Any) -> dict[str, Any]:
        if hasattr(tool, "tool_call_schema") and tool.tool_call_schema is not None:
            try:
                return tool.tool_call_schema.model_json_schema()
            except Exception:
                pass
        return _orig_get_schema(self, tool)

    ccm.ClaudeCodeChatModel._get_tool_schema = _patched_get_tool_schema

    # ── Patch 2: _wrap_langchain_tool ────────────────────────────────
    def _patched_wrap_langchain_tool(self, tool: Any, schema: dict[str, Any]) -> Any:
        from langchain_core.runnables import RunnableConfig
        try:
            from claude_agent_sdk import tool as sdk_tool
        except ImportError:
            # claude_agent_sdk is a dep of langchain-claude-code itself.
            # If we can't import it, the original wrap will fail anyway —
            # fall through to upstream for a consistent error message.
            return _orig_wrap(self, tool, schema)

        props = schema.get("properties", {})
        type_map = {
            "string": str, "integer": int, "number": float,
            "boolean": bool, "array": list, "object": dict,
        }
        param_types = {}
        for name, prop in props.items():
            json_type = prop.get("type", "string")
            param_types[name] = type_map.get(json_type, str)

        @sdk_tool(tool.name, tool.description or "", param_types)
        async def wrapped_tool(args: dict[str, Any]) -> dict[str, Any]:
            try:
                if hasattr(tool, "_arun") and asyncio.iscoroutinefunction(tool._arun):
                    # Strip InjectedToolArg params that may have leaked
                    # through the schema (belt-and-suspenders for the
                    # _get_tool_schema fix above). If config is already in
                    # args, passing it again via config=RunnableConfig()
                    # raises "got multiple values for keyword argument".
                    clean_args = {k: v for k, v in args.items() if k != "config"}
                    result = await tool._arun(**clean_args, config=RunnableConfig())
                else:
                    result = tool._run(**args)

                captured = (
                    self._tool_results_var.get(None) if self._tool_results_var else None
                )
                if captured is not None:
                    captured.append({
                        "name": tool.name, "args": args, "result": result,
                    })
                return {"content": [{"type": "text", "text": str(result)}]}
            except Exception as e:
                return {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "is_error": True,
                }

        return wrapped_tool

    ccm.ClaudeCodeChatModel._wrap_langchain_tool = _patched_wrap_langchain_tool
    setattr(ccm.ClaudeCodeChatModel, _PATCH_MARKER, True)
    log.debug(
        "applied langchain-claude-code patches: "
        "_get_tool_schema (tool_call_schema) + _wrap_langchain_tool (config-kwarg)"
    )


_DEEPAGENTS_BASE_PROMPT_MARKER = "_mimir_base_prompt_stripped"


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


_STREAMING_METADATA_MARKER = "_mimir_streaming_metadata_enriched"


def enrich_streaming_metadata() -> None:
    """Monkey-patch ``ChatClaudeCode._astream`` to preserve SDK
    ``ResultMessage`` fields that the upstream streaming code drops.

    The original ``_astream`` (``claude_chat_model.py:_astream``)
    builds the final result chunk's ``generation_info`` from
    ``msg.total_cost_usd``, ``duration_ms``, ``session_id``, and a
    BINARY ``finish_reason`` (``"stop"`` or ``"error"``). It DROPS:

      - ``msg.stop_reason`` — the granular SDK reason (``"max_turns"``,
        ``"max_tokens"``, ``"end_turn"``, etc). Collapsed to binary
        ``finish_reason``, losing the distinction.
      - ``msg.is_error`` — collapsed into ``finish_reason`` and never
        surfaced as its own field.
      - ``msg.num_turns`` — the SDK's per-request model-turn count.
        Not preserved at all; downstream code falls back to counting
        AIMessage chunks (different value, different semantics).

    ``mimir.turn_logger.derive_result_fields`` reads all three. Without
    this patch:
      - ``result_subtype`` defaults to ``"success"`` even on
        ``max_turns`` truncation (we'd need ``stop_reason`` to detect).
      - ``result_is_error`` is ``False`` on subprocess errors that
        manifested as ``is_error=True`` in the original ``ResultMessage``
        (the binary ``finish_reason="error"`` is at least recoverable;
        see fallback in ``derive_result_fields``).
      - ``num_turns`` is approximated by ``count(AIMessage)``.

    The streaming code DOES store the original ``ResultMessage`` on
    ``self._last_result`` (line 504 of ``_astream``). We wrap the
    method to detect the result chunk (identified by ``finish_reason``
    in ``generation_info``) and copy the missing fields from
    ``_last_result`` into the chunk's ``generation_info``. Pure
    additive enrichment — no behavior change for callers that don't
    read the new keys.

    Idempotent + import-safe: a no-op when ``langchain-claude-code``
    isn't installed. The class-attribute marker prevents double-
    wrapping on repeated calls.
    """
    try:
        from langchain_claude_code import claude_chat_model as ccm
    except ImportError:
        return

    if getattr(ccm.ClaudeCodeChatModel, _STREAMING_METADATA_MARKER, False):
        return

    _orig_astream = ccm.ClaudeCodeChatModel._astream

    async def _patched_astream(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        async for chunk in _orig_astream(self, *args, **kwargs):
            # The result chunk is the only one with ``finish_reason``
            # in generation_info (set inside the ``elif isinstance(msg,
            # ResultMessage):`` branch of the upstream loop). Other
            # chunks carry text content with no generation_info.
            gi = getattr(chunk, "generation_info", None)
            if gi and "finish_reason" in gi:
                last = getattr(self, "_last_result", None)
                if last is not None:
                    for fld in ("stop_reason", "num_turns", "is_error"):
                        if fld in gi:
                            continue
                        val = getattr(last, fld, None)
                        if val is not None:
                            gi[fld] = val
                    chunk.generation_info = gi
            yield chunk

    ccm.ClaudeCodeChatModel._astream = _patched_astream
    setattr(ccm.ClaudeCodeChatModel, _STREAMING_METADATA_MARKER, True)
    log.debug(
        "patched ChatClaudeCode._astream to preserve "
        "stop_reason/num_turns/is_error from ResultMessage",
    )


_TOOL_EVENT_HOOKS_MARKER = "_mimir_tool_event_hooks_installed"


# Third hook-callback param is the SDK's ``HookContext`` TypedDict —
# currently just ``{"signal": None}`` (reserved for future abort-signal
# support, see claude_agent_sdk/types.py:508). We don't use it; the
# leading-underscore name signals "unused" to future readers.
async def _pre_tool_use_hook(input_data: dict, tool_use_id: str, _ctx: Any) -> dict:
    """Append a tool_call event to the active capture list."""
    events = _tool_events_var.get()
    if events is None:
        return {}
    events.append({
        "type": "tool_call",
        "ts_mono_ns": time.monotonic_ns(),
        "tool_use_id": tool_use_id,
        "name": input_data.get("tool_name", ""),
        "input": input_data.get("tool_input", {}),
    })
    return {}


async def _post_tool_use_hook(input_data: dict, tool_use_id: str, _ctx: Any) -> dict:
    """Append a tool_result event (success) to the active capture list."""
    events = _tool_events_var.get()
    if events is None:
        return {}
    events.append({
        "type": "tool_result",
        "ts_mono_ns": time.monotonic_ns(),
        "tool_use_id": tool_use_id,
        "name": input_data.get("tool_name", ""),
        "result": input_data.get("tool_response"),
        "is_error": False,
    })
    return {}


async def _post_tool_use_failure_hook(
    input_data: dict, tool_use_id: str, _ctx: Any,
) -> dict:
    """Append a tool_result event (failure) to the active capture list."""
    events = _tool_events_var.get()
    if events is None:
        return {}
    events.append({
        "type": "tool_result",
        "ts_mono_ns": time.monotonic_ns(),
        "tool_use_id": tool_use_id,
        "name": input_data.get("tool_name", ""),
        "error": input_data.get("error"),
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

    Idempotent + import-safe: no-op when ``langchain-claude-code`` or
    ``claude-agent-sdk`` isn't installed. Re-running ``apply_patches``
    skips application via the class-attribute marker.
    """
    try:
        from langchain_claude_code import claude_chat_model as ccm
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
