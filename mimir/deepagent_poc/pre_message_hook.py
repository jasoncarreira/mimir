"""Pre-message hook as an external wrapper around `agent.ainvoke`.

This is the open-strix pattern (open_strix/hooks.py) applied to mimir's
existing pre-message-hook semantics:

  agent.py:_pre_message_hook (SDK path):
    1. Build context from recent channel messages
    2. Call saga_client.query(question, context=context)
    3. Format via _format_saga_payload
    4. Return content block; SDK injects into the turn

  deepagents path (this module):
    1. Build context from recent channel messages
    2. Call memory_client.query(question, context=context)
    3. Format via mimir.sagatools._format_saga_payload
    4. Prepend the formatted block to the HumanMessage before
       agent.ainvoke

The wrapper preserves two key invariants:
- ``rewritten_query`` survival: if MemoryClient.query returns a
  rewritten_query (contextual rewrite fired), we surface it for
  the bench/turn-logger to record alongside the answer.
- Atom ID capture: ``saga_atom_ids`` get returned so the post-message
  hook's mark_contributions equivalent can credit retrieval hits.

External wrapper is the right shape (not deepagents middleware)
because: pre-message hook semantics are "fire ONCE per turn before
the first model call." Middleware ``before_model`` fires before
EVERY model call inside the agent loop — wrong cadence here.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage

from mimir.memory.client import MemoryClient
from mimir.sagatools import (
    _atom_ids_from_response,
    _format_saga_payload,
    _source_atom_ids_from_triples,
)


@dataclass
class PreMessageResult:
    """What the wrapper returns to the caller. Mirrors the contract
    of mimir's existing _pre_message_hook return shape."""
    augmented_messages: list[Any]
    saga_atom_ids: list[str]
    rewritten_query: str | None
    memory_block: str
    pre_message_ms: int


async def run_pre_message(
    *,
    memory_client: MemoryClient,
    question: str,
    context_messages: list[dict[str, str]] | None = None,
    top_k: int = 12,
    session_id: str | None = None,
    min_confidence_tier: str | None = None,
    reference_date: Any = None,
) -> PreMessageResult:
    """Call memory_client.query, format the payload, prepend it to a
    HumanMessage suitable for ``agent.ainvoke({"messages": [...]})``.

    ``context_messages``: list of ``{"role": "user"|"assistant",
    "content": str}`` dicts. Saga's contextual-rewrite path uses
    these to disambiguate referential queries; empty means rewrite is
    a no-op (matches the bench's behavior — no chat history per probe).
    """
    t0 = time.monotonic()
    try:
        payload = await memory_client.query(
            question,
            top_k=top_k,
            session_id=session_id,
            min_confidence_tier=min_confidence_tier,
            context=context_messages,
            reference_date=reference_date,
        )
    except Exception as exc:
        # Match mimir's existing fail-soft behavior: turn proceeds
        # without injected memory; agent can still call memory_query
        # tool if it's registered.
        return PreMessageResult(
            augmented_messages=[HumanMessage(content=question)],
            saga_atom_ids=[],
            rewritten_query=None,
            memory_block=f"(pre_message memory_client.query failed: {exc})",
            pre_message_ms=int((time.monotonic() - t0) * 1000),
        )
    memory_block = _format_saga_payload(payload)
    atom_ids = _atom_ids_from_response(payload)
    triple_source_ids = _source_atom_ids_from_triples(payload)
    # Atom IDs the post-message credit pass should consider — atoms
    # the pre-hook surfaced AND atoms whose triples got surfaced.
    seen: set[str] = set()
    combined_ids: list[str] = []
    for aid in list(atom_ids) + list(triple_source_ids):
        if aid not in seen:
            seen.add(aid)
            combined_ids.append(aid)

    rewritten = payload.get("rewritten_query") or None

    # Construct the augmented user message. Mimir's current prompt
    # template puts memory in a labeled section under the question;
    # we mirror that shape so the agent sees the same prompt structure
    # as the production SDK path.
    augmented_content = (
        f"## Possibly relevant memories (from SAGA)\n\n{memory_block}\n\n"
        f"---\n\n{question}"
    )
    return PreMessageResult(
        augmented_messages=[HumanMessage(content=augmented_content)],
        saga_atom_ids=combined_ids,
        rewritten_query=rewritten,
        memory_block=memory_block,
        pre_message_ms=int((time.monotonic() - t0) * 1000),
    )


async def invoke_with_pre_message(
    agent: Any,
    *,
    memory_client: MemoryClient,
    question: str,
    context_messages: list[dict[str, str]] | None = None,
    top_k: int = 12,
    session_id: str | None = None,
    min_confidence_tier: str | None = None,
    reference_date: Any = None,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], PreMessageResult]:
    """Bundle ``run_pre_message`` + ``agent.ainvoke`` into one call.

    Equivalent to mimir's full ``Agent._run_query_loop`` pre-hook +
    SDK call but for the deepagents/LangGraph path. Returns
    ``(agent_result, pre_message_result)`` so the turn logger can
    capture both surfaces.
    """
    pre = await run_pre_message(
        memory_client=memory_client,
        question=question,
        context_messages=context_messages,
        top_k=top_k,
        session_id=session_id,
        min_confidence_tier=min_confidence_tier,
        reference_date=reference_date,
    )
    result = await agent.ainvoke(
        {"messages": pre.augmented_messages},
        config=config,
    )
    return result, pre
