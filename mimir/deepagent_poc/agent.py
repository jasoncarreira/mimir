"""Minimal deepagent factory for the PoC.

Builds a deepagent with:
  - claude-sonnet-4-6 (parity with saga 81.6 canonical reader)
  - one tool: memory_query (wraps MemoryClient.query)
  - bench-shaped system prompt (no production cost cues / algedonic /
    self-state blocks — same shape that lifted via_mimir+MemoryClient
    out of the prompt-bias regression)

Returns a compiled LangGraph state machine. Caller invokes via:
    agent = make_agent(memory_client)
    result = await agent.ainvoke({"messages": [HumanMessage(content=...)]})
"""
from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent
from langchain_core.messages import HumanMessage

from mimir.memory.client import MemoryClient
from .memory_tool import memory_query, set_memory_client


SYSTEM_PROMPT = """\
You are a memory-augmented assistant. The user is asking about facts \
from their own past conversation history.

Use the ``memory_query`` tool to search the user's persistent memory. \
The tool returns three types of evidence:

- Observations: synthesized beliefs from multiple turns. Use them for \
  patterns and preferences. Secondary evidence.
- Evidence (raw atoms): verbatim user/assistant chat history with \
  dates. Prefer this for specific dates, names, numbers, direct quotes. \
  Evidence wins over Observations when they conflict.
- Triples: structured (subject, predicate, object) facts. Each carries \
  a valid-date range. Use them for high-confidence factual lookups.

When the memory tool's result is truncated (you see "…[truncated]" or \
ellipsis mid-sentence at the answer point), call ``memory_query`` again \
with a more specific query to retrieve the missing detail.

Think step by step:
1. Which atoms / triples answer the question?
2. If multiple conflict, which is most recent?
3. If no evidence answers, say so plainly.

Then give the final answer on its own line. Be concise — direct \
factual answer, no preamble."""


def make_agent(
    memory_client: MemoryClient,
    *,
    # PoC default: gpt-5.4-nano via OPENAI_API_KEY (we have the key
    # in .env; ANTHROPIC_API_KEY isn't set because mimir uses Max OAuth
    # via claude_code subprocess, which LangChain doesn't grok natively).
    # For sonnet-4-6 parity with the saga 81.6 canonical reader, install
    # ``langchain-claude-code-cli`` and switch to its provider.
    model: str = "openai:gpt-5.4-nano",
    extra_system: str | None = None,
) -> Any:
    """Build a compiled deepagent wired to ``memory_client``.

    ``model`` defaults to sonnet-4-6 for parity with the saga 81.6
    canonical reader. Pass other providers (``"openai:gpt-5.4-nano"``,
    ``"anthropic:claude-haiku-4-5"``) for cost/speed experiments.

    ``extra_system`` is appended to SYSTEM_PROMPT — useful for the bench
    harness to inject per-question date anchoring.
    """
    set_memory_client(memory_client)
    system_prompt = SYSTEM_PROMPT
    if extra_system:
        system_prompt = system_prompt + "\n\n" + extra_system
    return create_deep_agent(
        model=model,
        tools=[memory_query],
        system_prompt=system_prompt,
    )
