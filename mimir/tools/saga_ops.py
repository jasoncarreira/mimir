"""SAGA agent-callable ops: feedback / mark_contributions / end_session / forget.

The PoC ``memory_query`` + ``memory_store`` tools (in
``mimir.tools.memory`` + ``mimir.tools.store``) cover the read +
write surface. This module adds the remaining four agent-facing
SAGA verbs the SDK build exposed:

* ``saga_feedback``           — outcome marker on a single atom
* ``saga_mark_contributions`` — manual credit pass against a response
* ``saga_end_session``        — write a session boundary atom
* ``saga_forget``             — preview/run the intentional-forgetting engine

All four route to the SagaStore instance installed by
``mimir.tools.memory.set_memory_client``. They reach the active
TurnContext via ``_context.get_current_turn()`` so the
``saga_session_id`` is threaded through transparently — the model
doesn't have to remember to pass it explicitly.

Best-effort failures: every tool surfaces SagaError + generic
exception messages as a human-readable string. Failures never crash
the turn.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from langchain_core.tools import tool

from .memory import _MEMORY_STATE

log = logging.getLogger(__name__)


_FEEDBACK_MAP: dict[str, str] = {
    "useful": "positive",
    "incorrect": "negative",
    "stale": "negative",
}


def _resolve_session_id(explicit: str | None) -> str | None:
    """Prefer the model-supplied ``session_id``; fall back to the
    active TurnContext's ``saga_session_id``."""
    explicit = (explicit or "").strip()
    if explicit:
        return explicit
    from .._context import get_current_turn
    ctx = get_current_turn()
    return getattr(ctx, "saga_session_id", None) if ctx is not None else None


async def _emit_feedback_sent(
    atom_count: int, feedback: str, session_id: str | None,
) -> None:
    """Best-effort ``saga_feedback_sent`` emit for the agent-curated
    feedback path.

    The per-turn auto-credit pass in ``agent.run_turn`` that used to emit
    this was removed (operator decision 2026-05-29): activation should
    rise only from the retrieval access event + DELIBERATE agent feedback.
    This event now marks that deliberate feedback — driving viability loop
    1.1 and the self-state feedback line off real curation rather than a
    blanket "the turn didn't fail" boost. Never raises."""
    try:
        from ..event_logger import log_event
        await log_event(
            "saga_feedback_sent",
            atom_count=atom_count,
            feedback=feedback,
            session_id=session_id,
        )
    except Exception:  # noqa: BLE001 — observability emit is best-effort
        pass


@tool
async def saga_feedback(
    atom_id: str,
    signal: str,
    session_id: Optional[str] = None,
) -> str:
    """Mark a single atom as useful/incorrect/stale.

    Maps to SAGA's outcome API: useful→positive, incorrect→negative,
    stale→negative. Pass ``session_id`` (your current
    saga_session_id) so the outcome is recorded against your turn.

    Args:
        atom_id: The SAGA atom id (16-char hex).
        signal: One of ``useful``, ``incorrect``, ``stale``.
        session_id: Optional override; defaults to the active turn's.
    """
    client = _MEMORY_STATE["client"]
    if client is None:
        return "saga_feedback failed: no SagaStore configured"
    if not atom_id:
        return "saga_feedback failed: atom_id is required"
    wire = _FEEDBACK_MAP.get((signal or "").strip().lower())
    if wire is None:
        return (
            f"saga_feedback failed: signal must be useful|incorrect|stale "
            f"(got {signal!r})"
        )
    sid = _resolve_session_id(session_id)
    try:
        await client.outcome([atom_id], feedback=wire, session_id=sid)
    except Exception as exc:  # noqa: BLE001 — SagaError surfaces via str
        return f"saga_feedback failed: {exc}"
    await _emit_feedback_sent(1, wire, sid)
    return f"saga_feedback ok: {atom_id} → {wire}"


@tool
async def saga_mark_contributions(
    atom_ids: list[str],
    response_text: str,
    session_id: Optional[str] = None,
) -> str:
    """Manually credit a list of atom_ids against a response.

    Credit is now agent-curated only — the per-turn auto-credit pass was
    removed (operator decision 2026-05-29), so call this when atoms
    genuinely informed your response and you want their activation lifted
    (a ``feedback_positive`` event). Don't blanket-credit everything that
    was merely in context.

    Args:
        atom_ids: SAGA atom ids to credit.
        response_text: The response body the atoms contributed to.
        session_id: Optional override; defaults to the active turn's.
    """
    client = _MEMORY_STATE["client"]
    if client is None:
        return "saga_mark_contributions failed: no SagaStore configured"
    if not isinstance(atom_ids, list) or not all(isinstance(a, str) for a in atom_ids):
        return "saga_mark_contributions failed: atom_ids must be a list of strings"
    if not isinstance(response_text, str):
        return "saga_mark_contributions failed: response_text must be a string"
    sid = _resolve_session_id(session_id)
    try:
        await client.feedback(atom_ids, response_text, session_id=sid)
    except Exception as exc:  # noqa: BLE001
        return f"saga_mark_contributions failed: {exc}"
    await _emit_feedback_sent(len(atom_ids), "positive", sid)
    return f"saga_mark_contributions ok: credited {len(atom_ids)} atoms"


@tool
async def saga_end_session(
    session_id: str,
    summary: str,
    topics_discussed: Optional[list[str]] = None,
    decisions_made: Optional[list[str]] = None,
    unfinished: Optional[list[str]] = None,
    emotional_state: Optional[str] = None,
    closed_since: Optional[list[str]] = None,
) -> str:
    """Close a SAGA session by writing the rendered boundary fields to
    the ``sessions`` table (replaces the legacy session_boundary atom).

    Auto-invoked by the synthesis turn at idle timeout (SPEC §5.6);
    call explicitly if you know a session is wrapping ("talk later").
    Empty lists / None for optional fields are dropped.

    ``closed_since`` carries refs (PRs, chainlinks, paths) from
    prior boundaries' Unfinished lists you've confirmed resolved
    during this session — the prompt builder substring-matches them
    and drops resolved items from later renderings.
    """
    client = _MEMORY_STATE["client"]
    if client is None:
        return "saga_end_session failed: no SagaStore configured"
    if not session_id:
        return "saga_end_session failed: session_id is required"
    if not summary:
        return "saga_end_session failed: summary is required"

    def _clean(lst: list[str] | None) -> list[str] | None:
        if not lst:
            return None
        if not isinstance(lst, list) or not all(isinstance(x, str) for x in lst):
            return None
        kept = [x for x in lst if x.strip()]
        return kept or None

    from .._context import get_current_turn
    ctx = get_current_turn()
    channel_id = getattr(ctx, "channel_id", None) if ctx is not None else None

    try:
        payload = await client.end_session(
            session_id=session_id,
            summary=summary,
            topics_discussed=_clean(topics_discussed),
            decisions_made=_clean(decisions_made),
            unfinished=_clean(unfinished),
            emotional_state=(emotional_state or "").strip() or None,
            closed_since=_clean(closed_since),
            channel_id=channel_id,
        )
    except Exception as exc:  # noqa: BLE001
        return f"saga_end_session failed: {exc}"

    # Flag the ctx so the synthesis-turn post-message hook can tell
    # the model actually called this tool (Mimir review noted that
    # the synthesis path needs this signal).
    if ctx is not None:
        ctx.saga_end_session_called = True

    written = (
        payload.get("session_summary_written")
        if isinstance(payload, dict) else None
    )
    return (
        f"saga_end_session ok: session_id={session_id} "
        f"summary_written={bool(written)}"
    )


@tool
async def saga_forget(
    dry_run: bool = True,
    min_retrievals: Optional[int] = None,
    contribution_threshold: Optional[float] = None,
    contradiction_threshold: Optional[float] = None,
    confidence_floor: Optional[float] = None,
    grace_days: Optional[int] = None,
) -> str:
    """Run SAGA's intentional-forgetting engine.

    PREVIEW FIRST: keep ``dry_run=True`` (default) to inspect the
    candidate list before acting. Set ``dry_run=False`` only after
    reviewing — forgetting is irreversible. Use this when ``## Self-
    state`` reports pending forget candidates; a successful non-dry-
    run call clears that line until the next decay cycle.
    """
    client = _MEMORY_STATE["client"]
    if client is None:
        return "saga_forget failed: no SagaStore configured"
    kwargs: dict[str, Any] = {"dry_run": bool(dry_run)}
    if min_retrievals is not None:
        kwargs["min_retrievals"] = min_retrievals
    if contribution_threshold is not None:
        kwargs["contribution_threshold"] = contribution_threshold
    if contradiction_threshold is not None:
        kwargs["contradiction_threshold"] = contradiction_threshold
    if confidence_floor is not None:
        kwargs["confidence_floor"] = confidence_floor
    if grace_days is not None:
        kwargs["grace_days"] = grace_days
    try:
        payload = await client.forget(**kwargs)
    except Exception as exc:  # noqa: BLE001
        return f"saga_forget failed: {exc}"
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


@tool
async def saga_record_skill_learning(
    skill: str,
    kind: str,
    content: str,
    session_id: Optional[str] = None,
) -> str:
    """Record a durable, skill-specific learning as a SAGA atom (#266).

    Use this when running a skill taught you something reusable that the
    *next* run of that skill should know — a gotcha, an input quirk, a
    performance caveat, a tip, or a pattern that worked. The learning is
    scoped to the skill: it surfaces automatically the next time that
    skill loads and never leaks into unrelated turns.

    Capture skill learnings here (NOT plain ``memory_store``) so they
    ride the per-skill recall, decay, and dedup built for skill memory.
    Record the cautionary ones especially — a ``failure-mode`` you hit is
    the most valuable thing to leave for the next run. One learning per
    call; a single self-contained sentence.

    Args:
        skill: The skill name (its SKILL.md directory / identifier), e.g.
            ``"memory"``, ``"github-poller"``.
        kind: The learning's type/valence — one of:
            NEGATIVE (cautionary): ``"failure-mode"``, ``"input-quirk"``,
            ``"perf-caveat"``; POSITIVE (how-to): ``"tip"``,
            ``"success-pattern"``.
        content: The learning, one self-contained sentence — written so a
            future run understands it without this session's context.
        session_id: Optional override; defaults to the active turn's.

    Returns:
        A short confirmation with the atom_id, or an error message.
    """
    client = _MEMORY_STATE["client"]
    if client is None:
        return "saga_record_skill_learning failed: no SagaStore configured"
    from .. import skill_memory
    try:
        metadata = skill_memory.build_metadata(skill, kind)
    except ValueError as exc:
        return f"saga_record_skill_learning failed: {exc}"
    if not content or not content.strip():
        return "saga_record_skill_learning failed: content is required"
    try:
        result = await client.store(
            content.strip(),
            stream="procedural",
            source_type=skill_memory.SKILL_LEARNING_SOURCE_TYPE,
            metadata=metadata,
            session_id=_resolve_session_id(session_id),
        )
    except Exception as exc:  # noqa: BLE001
        return f"saga_record_skill_learning failed: {exc}"
    if not isinstance(result, dict):
        return f"saga_record_skill_learning unexpected return: {result!r}"
    atom_id = result.get("atom_id")
    if result.get("stored") is False:
        return (
            f"saga_record_skill_learning: learning already present "
            f"(atom_id={atom_id})"
        )
    return f"saga_record_skill_learning ok: {skill}/{kind} atom_id={atom_id}"


__all__ = (
    "saga_feedback",
    "saga_mark_contributions",
    "saga_end_session",
    "saga_forget",
    "saga_record_skill_learning",
)
