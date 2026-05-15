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
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

from mimir.memory.client import MemoryClient
from .memory_tool import memory_query, set_memory_client
from .store_tool import memory_store


def resolve_model(spec: str | BaseChatModel) -> str | BaseChatModel:
    """Translate a mimir-friendly model spec into something
    ``create_deep_agent`` accepts.

    Supported specs (in order of preference for parity / cost):

    - ``"claude-code:<model>"`` → ``ChatClaudeCode(model=<model>)`` via
      ``langchain-claude-code`` package. Uses Claude Pro/Max OAuth
      (claude auth login); **no API key required**. Best for parity
      with saga 81.6 canonical (sonnet-4-6) without paying API rates.
      Caveats: counts against Max usage limits, subprocess spawn per
      call adds ~500ms-2s latency.

    - ``"<provider>:<model>"`` (e.g. ``"openai:gpt-5.4-nano"``,
      ``"anthropic:claude-haiku-4-5"``, ``"google:gemini-2.5-pro"``)
      → ``init_chat_model(spec)`` via the standard LangChain registry.
      Requires the corresponding API key env var
      (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.).

    - Pre-instantiated ``BaseChatModel`` → passed through unchanged.

    The string ``"<provider>:<model>"`` form is what ``create_deep_agent``
    itself supports for everything except claude-code; we only need
    custom handling for the Max-OAuth case (and any future bespoke
    providers — Bedrock + Titan, AWS roadmap item).
    """
    if isinstance(spec, BaseChatModel):
        return spec
    if not isinstance(spec, str):
        raise TypeError(f"unexpected model spec type: {type(spec).__name__}")
    if spec.startswith("claude-code:"):
        try:
            from langchain_claude_code import ChatClaudeCode  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "claude-code: model spec requested but langchain-claude-code "
                "is not installed. Run `uv add langchain-claude-code` first. "
                "Also ensure the Claude Code CLI is installed and "
                "`claude auth login` has been run."
            ) from exc
        model_name = spec.split(":", 1)[1]
        return ChatClaudeCode(model=model_name)
    # Everything else: pass through; create_deep_agent's init_chat_model
    # handles "openai:...", "anthropic:...", etc.
    return spec


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
    model: str | BaseChatModel = "openai:gpt-5.4-nano",
    extra_system: str | None = None,
) -> Any:
    """Build a compiled deepagent wired to ``memory_client``.

    ``model`` accepts:

    - ``"openai:gpt-5.4-nano"`` (default) — cheapest, works with our
      OPENAI_API_KEY today
    - ``"openai:gpt-5.4"`` / ``"openai:gpt-5.4-mini"`` — quality dial
    - ``"anthropic:claude-sonnet-4-6"`` — requires ANTHROPIC_API_KEY
    - ``"anthropic:claude-haiku-4-5"`` — same, faster/cheaper
    - ``"claude-code:claude-sonnet-4-6"`` — Max OAuth via Claude Code
      CLI subprocess (no API key, counts against your Max usage; install
      ``langchain-claude-code``). Best for saga 81.6 parity runs.
    - any pre-instantiated ``BaseChatModel`` — passed through

    See ``resolve_model`` for the routing logic.

    ``extra_system`` is appended to SYSTEM_PROMPT — useful for the bench
    harness to inject per-question date anchoring.
    """
    set_memory_client(memory_client)
    system_prompt = SYSTEM_PROMPT
    if extra_system:
        system_prompt = system_prompt + "\n\n" + extra_system
    return create_deep_agent(
        model=resolve_model(model),
        tools=[memory_query, memory_store],
        system_prompt=system_prompt,
    )
