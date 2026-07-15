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

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from mimir.models import AuthContext
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
async def memory_query(
    query: str,
    top_k: int = 12,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Search the user's persistent memory for atoms relevant to the query.

    Returns a structured block containing observations (synthesized from
    multiple turns), raw chat history excerpts, and structured triples
    (subject, predicate, object) facts. Prefer raw evidence for specific
    dates / names / numbers / direct quotes; observations for patterns.

    This is SEMANTIC search. If you already know the atom ids you want
    (e.g. ids cited in an observation), use ``memory_get`` to load them by
    id in one call — don't pass ids here and don't fan out one query per id.

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

    auth_context = (
        runtime.context
        if runtime is not None and isinstance(runtime.context, AuthContext)
        else None
    )

    try:
        payload = await client.query(query, top_k=top_k, auth_context=auth_context)
    except Exception as exc:
        return f"memory_query failed: {exc}"
    return _format_saga_payload(payload)


# memory_get shows MORE content than the semantic-recall display cap — the
# whole point is to read the atom to judge it — but still bounded so loading
# a large set can't blow the context window.
_GET_CONTENT_CAP = 2000


def _format_get_atoms(payload: dict) -> str:
    """Render a memory_get result: one bullet per atom WITH its id (so the
    agent can map each result back to the id it asked for and judge it),
    plus a trailing note listing any ids that weren't found."""
    atoms = payload.get("atoms") or []
    missing = payload.get("missing") or []
    lines: list[str] = []
    for a in atoms:
        label = a.get("memory_type") or a.get("stream") or "raw"
        content = (a.get("content") or "").strip().replace("\n", " ")
        if len(content) > _GET_CONTENT_CAP:
            content = content[:_GET_CONTENT_CAP] + "…"
        lines.append(f"- [{a.get('id')}] [{label}] {content}")
    body = "\n".join(lines) if lines else "(no atoms found)"
    if missing:
        body += (
            "\n\n(not found — deleted, unknown, or out of scope: "
            f"{', '.join(missing)})"
        )
    return body


@tool
async def memory_get(
    atom_ids: list[str],
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Load specific memory atoms by their exact id — a batch by-id lookup.

    Use this when you ALREADY know the atom ids you want (e.g. ids cited in
    an observation, or listed in the session-boundary "atoms cited" block)
    and just need their content. This is an exact-key load, NOT semantic
    search: pass ids, not natural-language queries (use ``memory_query`` to
    search), and pass ALL the ids in ONE call — never fan out one query per id.

    Unlike ``memory_query``, this fires no access events: a by-id load is
    fetching atoms you already know about (often to judge their usefulness),
    so it must not reinforce their activation.

    Args:
        atom_ids: saga atom ids to load.

    Returns:
        One line per atom (``[id] [type] content``), plus a note listing any
        ids that weren't found (deleted, unknown, or out of your scope).
        "(no atoms found)" if none of the ids resolved.
    """
    client = _MEMORY_STATE["client"]
    if client is None:
        return "memory_get failed: no SagaStore configured"
    if not isinstance(atom_ids, list) or not all(
        isinstance(a, str) for a in atom_ids
    ):
        return "memory_get failed: atom_ids must be a list of id strings"
    ids = [a for a in atom_ids if a]
    if not ids:
        return "memory_get failed: atom_ids is empty"

    auth_context = (
        runtime.context
        if runtime is not None and isinstance(runtime.context, AuthContext)
        else None
    )

    try:
        payload = await client.get_atoms(ids, auth_context=auth_context)
    except Exception as exc:  # noqa: BLE001 — SagaError surfaces via str
        return f"memory_get failed: {exc}"
    return _format_get_atoms(payload)
