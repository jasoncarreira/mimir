"""LangChain tool wrapping mimir.saga.SagaStore for the PoC.

Direct translation of the saga_query MCP tool to LangChain's @tool
decorator. The function body remains essentially identical — we still
call SagaStore.query and render its response via _format_saga_payload.

Surface change: the @tool decorator from langchain_core takes a
docstring + type hints; deepagents picks these up via the standard
LangChain Tool introspection (no custom registry, no MCP server).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from langchain_core.tools import tool

from mimir.saga.client import SagaStore
from mimir.sagatools import _format_saga_payload


# The PoC uses a single SagaStore instance per-process. Production
# would wire this via the agent's context_schema or a closure; for the
# smoke we set it via _set_memory_client() before agent invocation.
_MEMORY_STATE: dict[str, SagaStore | None] = {"client": None}


def set_memory_client(client: SagaStore) -> None:
    """Inject the SagaStore instance the tool will query against."""
    _MEMORY_STATE["client"] = client


@tool
async def memory_query(query: str, top_k: int = 12) -> str:
    """Search the user's persistent memory for atoms relevant to the query.

    Returns a structured block containing observations (synthesized from
    multiple turns), raw chat history excerpts, and structured triples
    (subject, predicate, object) facts. Prefer raw evidence for specific
    dates / names / numbers / direct quotes; observations for patterns.

    Args:
        query: Natural-language query for retrieval.
        top_k: Max raw atoms to return (default 12). Observations are
            additionally surfaced when relevant.

    Returns:
        Formatted memory block — atoms + observations + triples — or
        "(no atoms)" if no relevant memories exist.
    """
    client = _MEMORY_STATE["client"]
    if client is None:
        return "memory_query failed: no SagaStore configured"
    try:
        payload = await client.query(query, top_k=top_k)
    except Exception as exc:
        return f"memory_query failed: {exc}"
    return _format_saga_payload(payload)
