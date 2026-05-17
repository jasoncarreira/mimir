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
