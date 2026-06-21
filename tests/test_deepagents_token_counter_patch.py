from __future__ import annotations

from typing import Any

import deepagents.middleware.summarization as summarization
import langchain.agents.middleware.summarization as lc_summarization
from langchain.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool

from mimir import _langchain_claude_code_patches as patches


class FakeAnthropicChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "anthropic-chat"

    def _generate(  # type: ignore[no-untyped-def]
        self, messages, stop=None, run_manager=None, **kwargs
    ):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


def _restore_deepagents_token_counter_patch(original_deepagents_counter, original_lc_counter):
    summarization.count_tokens_approximately = original_deepagents_counter
    lc_summarization.count_tokens_approximately = original_lc_counter

    kwdefaults = getattr(summarization.SummarizationMiddleware.__init__, "__kwdefaults__", None)
    if isinstance(kwdefaults, dict) and "token_counter" in kwdefaults:
        kwdefaults["token_counter"] = original_deepagents_counter

    lc_kwdefaults = getattr(
        lc_summarization.SummarizationMiddleware.__init__, "__kwdefaults__", None
    )
    if isinstance(lc_kwdefaults, dict) and "token_counter" in lc_kwdefaults:
        lc_kwdefaults["token_counter"] = original_lc_counter


def test_patch_deepagents_token_counter_caches_base_tool_schema(monkeypatch, request):
    """Repeated DeepAgents token counts should not rebuild unchanged tool schemas."""
    original_deepagents_counter = summarization.count_tokens_approximately
    original_lc_counter = lc_summarization.count_tokens_approximately
    request.addfinalizer(
        lambda: _restore_deepagents_token_counter_patch(
            original_deepagents_counter, original_lc_counter
        )
    )
    calls: list[list[object] | None] = []

    def fake_counter(
        messages: object,
        *args: object,
        tools: list[object] | None = None,
        **kwargs: object,
    ) -> int:
        calls.append(tools)
        return 1

    monkeypatch.setattr(summarization, "count_tokens_approximately", fake_counter)

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

    tool = CountingTool()
    patches.patch_deepagents_token_counter_tool_schema_cache()
    patched = summarization.count_tokens_approximately

    assert getattr(patched, "_mimir_token_counter_tool_schema_cache") is True

    assert summarization.SummarizationMiddleware.__init__.__kwdefaults__["token_counter"] is patched

    assert patched([], tools=[tool]) == 1
    reads_after_first_count = tool.schema_reads
    assert reads_after_first_count > 0
    assert patched([], tools=[tool]) == 1

    assert tool.schema_reads == reads_after_first_count
    assert len(calls) == 2
    assert calls[0] is not calls[1]
    assert calls[0] == calls[1]
    assert isinstance(calls[0][0], dict)  # type: ignore[index]

    # Idempotent: a second patch call keeps the same wrapper rather than
    # stacking wrappers or resetting the cache.
    patches.patch_deepagents_token_counter_tool_schema_cache()
    assert summarization.count_tokens_approximately is patched


def test_patch_deepagents_token_counter_leaves_dict_tools_unconverted(monkeypatch, request):
    original_deepagents_counter = summarization.count_tokens_approximately
    original_lc_counter = lc_summarization.count_tokens_approximately
    request.addfinalizer(
        lambda: _restore_deepagents_token_counter_patch(
            original_deepagents_counter, original_lc_counter
        )
    )
    calls: list[list[object] | None] = []

    def fake_counter(
        messages: object,
        *args: object,
        tools: list[object] | None = None,
        **kwargs: object,
    ) -> int:
        calls.append(tools)
        return 1

    monkeypatch.setattr(summarization, "count_tokens_approximately", fake_counter)
    patches.patch_deepagents_token_counter_tool_schema_cache()

    schema = {"type": "function", "function": {"name": "already_converted"}}
    summarization.count_tokens_approximately([], tools=[schema])

    assert calls == [[schema]]


def test_patch_deepagents_token_counter_preserves_model_tuned_counts(request):
    original_deepagents_counter = summarization.count_tokens_approximately
    original_lc_counter = lc_summarization.count_tokens_approximately
    request.addfinalizer(
        lambda: _restore_deepagents_token_counter_patch(
            original_deepagents_counter, original_lc_counter
        )
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
    patched = lc_summarization.SummarizationMiddleware(
        model=FakeAnthropicChatModel(),
        trigger=("tokens", 100_000),
    )

    assert patched.token_counter(messages) == expected
    assert patched.token_counter(messages) != original_lc_counter(messages)
    assert patched.token_counter.keywords == {
        "use_usage_metadata_scaling": True,
        "chars_per_token": 3.3,
    }
    assert getattr(
        patched.token_counter.func, patches._DEEPAGENTS_TOKEN_COUNTER_PATCH_MARKER
    ) is True
