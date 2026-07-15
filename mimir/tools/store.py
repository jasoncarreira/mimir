"""LangChain tool wrapping SagaStore.store — write surface.

Migration target for ``saga_store`` (mimir/sagatools.py:337). Shape
maps directly from the SDK's @tool decorator to LangChain's @tool:

  SDK:                                  LangChain:
  ────────────────────────────────────────────────────────────────
  @tool(name, description, schema)      @tool (decorator, no args)
   async def f(args: dict) -> dict      async def f(content, stream, ...)
   return {"content": [...]}            return string

LangChain reads the function signature + docstring for the schema —
no separate JSON schema definition needed. The docstring becomes the
tool description shown to the model. This is meaningfully terser than
the SDK's API; we lose nothing from the production tool definition.

The actual storage call goes through the same SagaStore instance
the memory_tool.py uses (shared via _MEMORY_STATE).
"""
from __future__ import annotations

from typing import Any, Optional

from langchain_core.tools import tool

from ..access_control import (
    can_write_saga,
    get_provenance_from_auth_context,
)
from .memory import _MEMORY_STATE


def _resolve_active_context() -> tuple[Any | None, str]:
    """Resolve the active turn context for authorization checks."""
    from .._context import resolve_active_ctx

    return resolve_active_ctx({})


@tool
async def memory_store(
    content: str,
    stream: str,
    session_id: Optional[str] = None,
    source_type: str = "agent_authored",
) -> str:
    """Store a memory atom in persistent memory for cross-session retrieval.

    Reach for this when you encounter:
      - semantic facts, preferences, knowledge about people, places, things
        ("Alice prefers Slack DMs over email for urgent asks")
      - episodic dated events ("Alice joined the Atlas project on 2025-03-12")
      - procedural workflow patterns ("When summarizing a long document,
        lead with the thesis and supporting evidence")

    One fact per call. Single self-contained sentence. Dates and numbers
    verbatim.

    Do NOT store: meta-observations about the runtime ("the prompt fired"),
    self-state claims ("I'm uncertain about X"), absence claims ("nothing
    happened"), duplicates of content already in a file, or session-retell
    content.

    Args:
        content: The fact / event / pattern, one self-contained sentence.
        stream: One of ``"semantic"``, ``"episodic"``, ``"procedural"``.
        session_id: Optional saga_session_id for scoping. If provided, must
            match the current turn's saga_session_id. If not provided, the
            server will derive the session from the active turn context.
        source_type: How this atom was created. Defaults to
            ``"agent_authored"``.

    Returns:
        A short string with the resulting atom_id, or an error message.
    """
    client = _MEMORY_STATE["client"]
    if client is None:
        return "memory_store failed: no SagaStore configured"

    ctx, _resolution = _resolve_active_context()
    auth_context = getattr(ctx, "auth_context", None) if ctx is not None else None

    if ctx is not None and not can_write_saga(auth_context):
        return (
            "memory_store failed: write access denied. "
            "Shared-memory writes require admin role or trusted service principal."
        )

    effective_session_id = session_id
    if session_id is not None and ctx is not None:
        current_saga_session_id = getattr(ctx, "saga_session_id", None) if ctx is not None else None
        if session_id != current_saga_session_id:
            return (
                f"memory_store failed: session_id mismatch. "
                f"Provided '{session_id}' does not match current turn's "
                f"saga_session_id '{current_saga_session_id}'. "
                f"Model-supplied session_id is attribution-only and cannot "
                f"choose an owner, channel, visibility, or another active turn's authority."
            )
    elif ctx is not None:
        effective_session_id = getattr(ctx, "saga_session_id", None)

    provenance = get_provenance_from_auth_context(auth_context)
    owner_principal = provenance.get("created_by")
    origin_channel = getattr(ctx, "channel_id", None) if ctx is not None else None

    try:
        result = await client.store(
            content,
            stream=stream,
            source_type=source_type,
            session_id=effective_session_id,
            owner_principal=owner_principal,
            origin_channel=origin_channel,
            origin_domain=None,
            visibility="service" if auth_context and getattr(auth_context, "is_service", False) else "private",
            provenance=provenance,
        )
    except Exception as exc:
        return f"memory_store failed: {exc}"
    if not isinstance(result, dict):
        return f"memory_store unexpected return: {result!r}"
    atom_id = result.get("atom_id")
    stored = result.get("stored")
    if stored is False:
        return f"memory_store: atom already present (atom_id={atom_id})"
    return f"memory_store: stored atom_id={atom_id}"
