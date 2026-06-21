from __future__ import annotations

from typing import Any

import deepagents.middleware.summarization as summarization
from langchain_core.tools import BaseTool

from mimir import _langchain_claude_code_patches as patches


def test_patch_deepagents_token_counter_caches_base_tool_schema(monkeypatch):
    """Repeated DeepAgents token counts should not rebuild unchanged tool schemas."""
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


def test_patch_deepagents_token_counter_leaves_dict_tools_unconverted(monkeypatch):
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
