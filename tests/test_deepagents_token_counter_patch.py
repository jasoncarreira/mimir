from __future__ import annotations

import gc
import inspect
import weakref
from functools import partial
from typing import Any

import deepagents.graph as deepagents_graph
import deepagents.middleware as deepagents_middleware
import deepagents.middleware.filesystem as filesystem_middleware
import deepagents.middleware.summarization as summarization
import langchain.agents.middleware.summarization as lc_summarization
import pytest
from deepagents.backends import StateBackend
from langchain.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool

from mimir import _deepagents_patches as patches
from mimir.readonly_backend import MimirFilesystemMiddleware
from mimir.subagents import build_mimir_subagents


class FakeAnthropicChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "anthropic-chat"

    def _generate(  # type: ignore[no-untyped-def]
        self, messages, stop=None, run_manager=None, **kwargs
    ):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


class CountingTool(BaseTool):
    name: str = "counting_tool"
    description: str = "Tool whose schema conversion is expensive."
    schema_reads: int = 0

    @property
    def tool_call_schema(self) -> Any:
        self.schema_reads += 1
        return super().tool_call_schema

    def _run(self, value: str) -> str:
        return value


def _token_counter_kwdefaults() -> list[dict[str, Any]]:
    functions = [
        summarization.SummarizationMiddleware.__init__,
        lc_summarization.SummarizationMiddleware.__init__,
        summarization.create_summarization_middleware,
    ]
    defaults: list[dict[str, Any]] = []
    for function in functions:
        kwdefaults = getattr(function, "__kwdefaults__", None)
        assert isinstance(kwdefaults, dict) and "token_counter" in kwdefaults
        defaults.append(kwdefaults)
    return defaults


@pytest.fixture
def token_counter_state():
    original_deepagents_counter = summarization.count_tokens_approximately
    original_lc_counter = lc_summarization.count_tokens_approximately
    kwdefaults = _token_counter_kwdefaults()
    original_defaults = [defaults["token_counter"] for defaults in kwdefaults]
    yield
    summarization.count_tokens_approximately = original_deepagents_counter
    lc_summarization.count_tokens_approximately = original_lc_counter
    for defaults, original in zip(kwdefaults, original_defaults, strict=True):
        defaults["token_counter"] = original


def _patch_with_counter(counter):
    summarization.count_tokens_approximately = counter
    lc_summarization.count_tokens_approximately = counter
    patches.patch_deepagents_token_counter_tool_schema_cache()
    return summarization.count_tokens_approximately


def test_install_deepagents_grep_context_tool_patches_all_real_aliases(monkeypatch):
    stock = object()
    monkeypatch.setattr(deepagents_graph, "FilesystemMiddleware", stock)
    monkeypatch.setattr(deepagents_middleware, "FilesystemMiddleware", stock)
    monkeypatch.setattr(filesystem_middleware, "FilesystemMiddleware", stock)
    monkeypatch.delattr(
        deepagents_graph,
        patches._DEEPAGENTS_GREP_CONTEXT_PATCH_MARKER,
        raising=False,
    )

    patches.install_deepagents_grep_context_tool()

    assert deepagents_graph.FilesystemMiddleware is MimirFilesystemMiddleware
    assert deepagents_middleware.FilesystemMiddleware is MimirFilesystemMiddleware
    assert filesystem_middleware.FilesystemMiddleware is MimirFilesystemMiddleware

    deepagents_graph.FilesystemMiddleware = stock
    deepagents_middleware.FilesystemMiddleware = stock
    filesystem_middleware.FilesystemMiddleware = stock
    patches.install_deepagents_grep_context_tool()

    assert deepagents_graph.FilesystemMiddleware is MimirFilesystemMiddleware
    assert deepagents_middleware.FilesystemMiddleware is MimirFilesystemMiddleware
    assert filesystem_middleware.FilesystemMiddleware is MimirFilesystemMiddleware


def test_patch_deepagents_token_counter_passes_through_arguments(token_counter_state):
    calls: list[tuple[object, tuple[object, ...], list[object] | None, dict[str, object]]] = []

    def fake_counter(
        messages: object,
        *args: object,
        tools: list[object] | None = None,
        **kwargs: object,
    ) -> int:
        calls.append((messages, args, tools, kwargs))
        return 17

    patched = _patch_with_counter(fake_counter)
    messages = [HumanMessage(content="hello")]
    schema = {"type": "function", "function": {"name": "ready"}}
    opaque_tool = object()

    assert patched(messages, "extra", tools=[schema, opaque_tool], mode="test") == 17
    assert calls == [(messages, ("extra",), [schema, opaque_tool], {"mode": "test"})]
    assert calls[0][2] is not None
    assert calls[0][2][0] is schema
    assert calls[0][2][1] is opaque_tool


def test_patch_deepagents_token_counter_reuses_and_defends_cached_schema(
    token_counter_state,
):
    calls: list[list[object] | None] = []

    def fake_counter(
        messages: object,
        *args: object,
        tools: list[object] | None = None,
        **kwargs: object,
    ) -> int:
        calls.append(tools)
        return 1

    patched = _patch_with_counter(fake_counter)
    tool = CountingTool()

    assert patched([], tools=[tool]) == 1
    reads_after_first_count = tool.schema_reads
    assert reads_after_first_count > 0
    first_tools = calls[0]
    assert first_tools is not None
    first_schema = first_tools[0]
    assert isinstance(first_schema, dict)
    first_schema["function"]["name"] = "mutated"

    assert patched([], tools=[tool]) == 1

    second_tools = calls[1]
    assert second_tools is not None
    second_schema = second_tools[0]
    assert isinstance(second_schema, dict)
    assert tool.schema_reads == reads_after_first_count
    assert second_schema["function"]["name"] == "counting_tool"
    assert second_schema is not first_schema


def test_patch_deepagents_token_counter_evicts_weakref_cache(token_counter_state):
    def fake_counter(
        messages: object,
        *args: object,
        tools: list[object] | None = None,
        **kwargs: object,
    ) -> int:
        return 1

    patched = _patch_with_counter(fake_counter)
    tool = CountingTool()
    tool_id = id(tool)
    tool_ref = weakref.ref(tool)

    assert patched([], tools=[tool]) == 1
    cached_tools = inspect.getclosurevars(patched).nonlocals["_cached_tools"]
    cache = inspect.getclosurevars(cached_tools).nonlocals["cache"]
    assert cache[tool_id][0]() is tool

    del tool
    gc.collect()

    assert tool_ref() is None
    assert tool_id not in cache


def test_patch_deepagents_token_counter_sets_and_repairs_all_defaults(
    token_counter_state,
):
    def fake_counter(
        messages: object,
        *args: object,
        tools: list[object] | None = None,
        **kwargs: object,
    ) -> int:
        return 1

    patched = _patch_with_counter(fake_counter)
    defaults = _token_counter_kwdefaults()

    assert getattr(patched, patches._DEEPAGENTS_TOKEN_COUNTER_PATCH_MARKER) is True
    assert patched.__wrapped__ is fake_counter
    assert summarization.count_tokens_approximately is patched
    assert lc_summarization.count_tokens_approximately is patched
    assert all(kwdefaults["token_counter"] is patched for kwdefaults in defaults)

    summarization.count_tokens_approximately = fake_counter
    for kwdefaults in defaults:
        kwdefaults["token_counter"] = fake_counter

    patches.patch_deepagents_token_counter_tool_schema_cache()

    assert summarization.count_tokens_approximately is patched
    assert lc_summarization.count_tokens_approximately is patched
    assert all(kwdefaults["token_counter"] is patched for kwdefaults in defaults)


def test_patch_deepagents_token_counter_factory_uses_shared_model_tuned_wrapper(
    token_counter_state,
):
    summarization.count_tokens_approximately = count_tokens_approximately
    lc_summarization.count_tokens_approximately = count_tokens_approximately
    lc_summarization.SummarizationMiddleware.__init__.__kwdefaults__["token_counter"] = (
        count_tokens_approximately
    )
    messages = [
        HumanMessage(content="x" * 120),
        AIMessage(
            content="y" * 120,
            response_metadata={"model_provider": "anthropic"},
            usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        ),
        HumanMessage(content="z" * 120),
    ]
    stock = lc_summarization.SummarizationMiddleware(
        model=FakeAnthropicChatModel(),
        trigger=("tokens", 100_000),
    )
    expected = stock.token_counter(messages)

    patches.patch_deepagents_token_counter_tool_schema_cache()
    patched = summarization.count_tokens_approximately
    middleware = summarization.create_summarization_middleware(
        FakeAnthropicChatModel(),
        StateBackend(),
    )

    assert isinstance(middleware.token_counter, partial)
    assert middleware.token_counter.func is patched
    assert middleware.token_counter(messages) == expected
    assert middleware.token_counter(messages) != count_tokens_approximately(messages)
    assert middleware.token_counter.keywords == {
        "use_usage_metadata_scaling": True,
        "chars_per_token": 3.3,
    }
    tool = CountingTool()
    middleware.token_counter([], tools=[tool])
    reads_after_first_count = tool.schema_reads
    middleware.token_counter([], tools=[tool])
    assert reads_after_first_count > 0
    assert tool.schema_reads == reads_after_first_count


def test_real_main_general_and_critic_graphs_share_schema_cache(
    monkeypatch,
    token_counter_state,
):
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    patched = _patch_with_counter(count_tokens_approximately)
    created_middleware = []
    original_factory = deepagents_graph.create_summarization_middleware

    def capture_factory(*args, **kwargs):
        middleware = original_factory(*args, **kwargs)
        created_middleware.append(middleware)
        return middleware

    monkeypatch.setattr(
        deepagents_graph,
        "create_summarization_middleware",
        capture_factory,
    )
    subagents = build_mimir_subagents()

    create_deep_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="done")])),
        backend=StateBackend(),
        tools=[],
        system_prompt="token counter construction test",
        subagents=subagents,
    )

    assert [subagent["name"] for subagent in subagents] == [
        "general-purpose",
        "critic-structured",
    ]
    assert len(created_middleware) == 3
    assert summarization.count_tokens_approximately is patched
    assert lc_summarization.count_tokens_approximately is patched
    assert all(
        defaults["token_counter"] is patched
        for defaults in _token_counter_kwdefaults()
    )

    for middleware in created_middleware:
        assert isinstance(middleware.token_counter, partial)
        assert middleware.token_counter.func is patched
        tool = CountingTool()
        middleware.token_counter([], tools=[tool])
        reads_after_first_count = tool.schema_reads
        middleware.token_counter([], tools=[tool])
        assert reads_after_first_count > 0
        assert tool.schema_reads == reads_after_first_count
