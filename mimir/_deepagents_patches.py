"""Mimir runtime patches for DeepAgents middleware behavior."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


_DEEPAGENTS_TOKEN_COUNTER_PATCH_MARKER = "_mimir_token_counter_tool_schema_cache"
_DEEPAGENTS_GREP_CONTEXT_PATCH_MARKER = "_mimir_grep_context_tool"


def install_deepagents_grep_context_tool() -> None:
    """Use Mimir's bounded-context grep schema in every DeepAgents stack."""
    try:
        from deepagents import graph as deepagents_graph
        from deepagents import middleware as deepagents_middleware
        from deepagents.middleware import filesystem as filesystem_middleware

        from .readonly_backend import MimirFilesystemMiddleware
    except ImportError:
        return
    already_installed = getattr(
        deepagents_graph, _DEEPAGENTS_GREP_CONTEXT_PATCH_MARKER, False
    )
    deepagents_graph.FilesystemMiddleware = MimirFilesystemMiddleware
    deepagents_middleware.FilesystemMiddleware = MimirFilesystemMiddleware
    filesystem_middleware.FilesystemMiddleware = MimirFilesystemMiddleware
    setattr(deepagents_graph, _DEEPAGENTS_GREP_CONTEXT_PATCH_MARKER, True)
    if not already_installed:
        log.debug("installed bounded-context grep tool schema")


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
        from langchain_core.utils.function_calling import convert_to_openai_tool
        import deepagents.middleware.summarization as summarization
    except ImportError:
        return

    current = getattr(summarization, "count_tokens_approximately", None)
    lc_current = lc_summarization.count_tokens_approximately
    if getattr(current, _DEEPAGENTS_TOKEN_COUNTER_PATCH_MARKER, False):
        patched_counter = current
    elif getattr(lc_current, _DEEPAGENTS_TOKEN_COUNTER_PATCH_MARKER, False):
        patched_counter = lc_current
    else:
        original_counter = current or message_utils.count_tokens_approximately
        counter_to_wrap = lc_current if original_counter is lc_current else original_counter
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
                    schema = convert_to_openai_tool(tool)
                    try:
                        ref = weakref.ref(
                            tool,
                            lambda _ref, tid=tool_id: _drop_cached_tool(tid),
                        )
                        cache[tool_id] = (ref, schema)
                    except TypeError:
                        pass
                converted.append(copy.deepcopy(schema))
            return converted

        def _patched_count_tokens_approximately(  # type: ignore[no-untyped-def]
            messages,
            *args,
            tools=None,
            **kwargs,
        ):
            return counter_to_wrap(messages, *args, tools=_cached_tools(tools), **kwargs)

        setattr(
            _patched_count_tokens_approximately,
            _DEEPAGENTS_TOKEN_COUNTER_PATCH_MARKER,
            True,
        )
        _patched_count_tokens_approximately.__wrapped__ = counter_to_wrap  # type: ignore[attr-defined]
        patched_counter = _patched_count_tokens_approximately

    summarization.count_tokens_approximately = patched_counter
    lc_summarization.count_tokens_approximately = patched_counter

    kwdefaults = getattr(summarization.SummarizationMiddleware.__init__, "__kwdefaults__", None)
    if isinstance(kwdefaults, dict) and "token_counter" in kwdefaults:
        kwdefaults["token_counter"] = patched_counter
    lc_kwdefaults = getattr(
        lc_summarization.SummarizationMiddleware.__init__, "__kwdefaults__", None
    )
    if isinstance(lc_kwdefaults, dict) and "token_counter" in lc_kwdefaults:
        lc_kwdefaults["token_counter"] = patched_counter
    factory_kwdefaults = getattr(
        summarization.create_summarization_middleware, "__kwdefaults__", None
    )
    if isinstance(factory_kwdefaults, dict) and "token_counter" in factory_kwdefaults:
        factory_kwdefaults["token_counter"] = patched_counter

    log.debug("patched DeepAgents token counter to cache BaseTool schema conversion")
