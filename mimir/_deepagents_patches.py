"""Mimir runtime patches for DeepAgents prompt and token-counting behavior."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


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
