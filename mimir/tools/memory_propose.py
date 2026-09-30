"""Queue tainted-turn memory for later operator review, without SAGA writes."""

from __future__ import annotations

import os
from pathlib import Path

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from ..access_control import (
    get_trusted_service_from_auth_context, is_admin,
    saga_mutation_taint_refusal, service_can_invoke_operation,
)
from ..memory_proposals import ProposalRefusal, queue_proposal
from ..models import AuthContext, InformationFlowLabels
from ..read_policy import text_contains_secret


@tool
async def memory_propose(
    content: str, stream: str, rationale: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Propose one memory fact for operator review on a tainted turn.

    This queues untrusted text only; no durable memory atom is created.
    Args:
        content: One self-contained fact, at most 500 characters.
        stream: semantic, episodic, or procedural.
        rationale: Why this fact may be useful after operator review.
    """
    auth = runtime.context if runtime is not None and isinstance(runtime.context, AuthContext) else None
    if auth is None:
        return "memory_propose refused: missing turn authority"
    if saga_mutation_taint_refusal(auth) is None:
        return "memory_propose refused: this turn can store directly; use memory_store"
    if not (is_admin(auth) or service_can_invoke_operation(
        get_trusted_service_from_auth_context(auth), "memory_propose",
    )):
        return "memory_propose refused: write access denied"
    labels = auth.ifc_state.current(auth.ifc_labels)
    if not isinstance(labels, InformationFlowLabels) or not labels.sources:
        return "memory_propose refused: missing IFC source provenance"
    if text_contains_secret(content):
        return "memory_propose refused: credential-shaped content"
    from .._context import get_current_turn

    turn = get_current_turn()
    if turn is None or turn.auth_context is not auth:
        return "memory_propose refused: missing authoritative turn id"
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return "memory_propose refused: MIMIR_HOME is not set"
    try:
        proposal_id = queue_proposal(
            Path(home), content=content, stream=stream, rationale=rationale,
            proposed_by=auth.canonical_principal or auth.principal,
            turn_id=turn.turn_id, origin_trigger=auth.origin_trigger or auth.trigger,
            origin_ref=auth.origin_ref, sources=labels.sources,
        )
    except (ProposalRefusal, OSError, ValueError) as exc:
        return f"memory_propose refused: {exc}"
    return (
        f"Proposed memory {proposal_id} queued for operator review; nothing is stored yet. "
        "Do not retry memory_store and do not notify the operator — the review digest will."
    )
