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
import logging
from typing import Any

log = logging.getLogger(__name__)

_PATCH_MARKER = "_mimir_arun_config_patched"


def apply_patches() -> None:
    """Apply the ``_arun`` config-kwarg patch to ChatClaudeCode.
    Idempotent + import-safe.

    Detects when the upstream ``_wrap_langchain_tool`` has been
    fixed (signature change or body no longer calling ``_arun``
    without ``config``) and skips the patch in that case — pre-fix
    the ``_PATCH_MARKER`` guard only handled re-running the patch,
    not an upstream change that rendered it obsolete (or worse,
    re-broken by us). On signature mismatch we bail out with a
    warning rather than silently replacing the upstream fix with
    our stale shim.
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

    def _patched_wrap_langchain_tool(self, tool, schema):
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
                    # The patch — pass an empty RunnableConfig so the
                    # required-kwarg validation in langchain-core 1.x
                    # doesn't raise.
                    result = await tool._arun(**args, config=RunnableConfig())
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
    log.debug("applied langchain-claude-code _arun config-kwarg patch")


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
