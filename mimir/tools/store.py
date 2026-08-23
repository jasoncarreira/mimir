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

from typing import Optional

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from ..access_control import (
    can_write_saga,
    get_provenance_from_auth_context,
    is_trusted_service,
)
from ..models import AuthContext, InformationFlowLabels, Integrity
from .memory import _MEMORY_STATE


@tool
async def memory_store(
    content: str,
    stream: str,
    session_id: Optional[str] = None,
    source_type: str = "agent_authored",
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
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
        session_id: Optional saga_session_id attribution label. It never
            participates in authorization or ownership selection.
        source_type: How this atom was created. Defaults to
            ``"agent_authored"``.

    Returns:
        A short string with the resulting atom_id, or an error message.

    Note:
        ``origin_domain`` is unset because a service can span multiple readable
        domains and this call has no source-domain context. Service self-access
        is authorized by the stamped ``service:{canonical}`` owner instead.
    """
    client = _MEMORY_STATE["client"]
    if client is None:
        return "memory_store failed: no SagaStore configured"

    auth_context = (
        runtime.context
        if runtime is not None and isinstance(runtime.context, AuthContext)
        else None
    )
    if not can_write_saga(auth_context, "memory_store"):
        return (
            "memory_store failed: write access denied. "
            "Shared-memory writes require server-provided admin or trusted-service authority."
        )

    # ``session_id`` is attribution-only. It may label the write, but cannot
    # select a TurnContext or influence any authority/ownership field.
    effective_session_id = (session_id or "").strip() or None
    provenance = get_provenance_from_auth_context(auth_context)
    owner_principal = provenance["created_by"]
    origin_channel = auth_context.channel_id
    visibility = "service" if is_trusted_service(auth_context) else "private"
    labels = auth_context.ifc_state.current(auth_context.ifc_labels)
    integrity = (
        Integrity.TRUSTED
        if isinstance(labels, InformationFlowLabels)
        and labels.sources
        and all(source.integrity == Integrity.TRUSTED for source in labels.sources)
        else Integrity.UNTRUSTED
    )

    try:
        result = await client.store(
            content,
            stream=stream,
            source_type=source_type,
            session_id=effective_session_id,
            owner_principal=owner_principal,
            origin_channel=origin_channel,
            integrity=integrity,
            origin_trigger=auth_context.origin_trigger or auth_context.trigger,
            origin_ref=auth_context.origin_ref,
            origin_domain=None,
            visibility=visibility,
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
