"""MSAM-facing MCP tools (SPEC §8.2).

Wraps the MsamClient as five tools:
  - msam_query              — explicit semantic atom retrieval
  - msam_store              — explicit atom store (rare; MSAM auto-extracts)
  - msam_feedback           — corrective signal on a single atom
  - msam_mark_contributions — manual variant of the post-message hook
  - msam_end_session        — synthesis-turn bookkeeping (SPEC §5.6)

Most MSAM activity is automatic via the pre/post-message hooks (SPEC §9.3).
Mid-turn ``msam_query`` results are auto-appended to the parent's
``TurnContext.msam_atom_ids`` so the post-message hook credits them — agent
doesn't have to remember.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ._context import get_current_turn
from ._tool_helpers import _ArgError, _content_block, _need, _safe
from .msam_client import MsamClient, MsamError
from .session_boundary_log import SessionBoundaryLog

log = logging.getLogger(__name__)

# Friendly mimir signal → MSAM /v1/outcome wire vocabulary.
_FEEDBACK_MAP = {
    "useful": "positive",
    "incorrect": "negative",
    "stale": "negative",
}


def _atom_label(atom: dict[str, Any]) -> str:
    """Pick the most descriptive tag for an atom in the rendered prompt.

    Format priority:
    - ``observation/<tier>`` when memory_type=observation and a per-atom
      confidence_tier is present (two-tier mode w/ per-atom gating).
    - ``raw/<tier>`` when memory_type=raw and a tier is present.
    - ``observation`` / ``<stream>`` / ``atom`` as fallbacks when the tier
      isn't on the wire (single-tier mode, legacy responses).

    The agent uses these tags to triage retrieval — observations beat raws
    on average, and within a tier high beats medium beats low.
    """
    mt = atom.get("memory_type")
    tier = atom.get("confidence_tier") or atom.get("_confidence_tier")
    base = "observation" if mt == "observation" else (atom.get("stream") or atom.get("kind") or mt or "atom")
    if tier and tier != "none":
        return f"{base}/{tier}"
    return base


def _format_atoms(hits: list[dict[str, Any]]) -> str:
    """Render MSAM hits as a brief bullet list — tag + content, no IDs.
    Used by the pre-message hook (SPEC §9.3) and the msam_query tool result."""
    if not hits:
        return "(no atoms)"
    lines: list[str] = []
    for h in hits:
        label = _atom_label(h)
        score = h.get("score") or h.get("similarity")
        content = (h.get("content") or "").strip().replace("\n", " ")
        if len(content) > 240:
            content = content[:240] + "…"
        score_str = f" ({score:.3f})" if isinstance(score, (int, float)) else ""
        lines.append(f"- [{label}{score_str}] {content}")
    return "\n".join(lines)


def _atoms_in_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the atoms list from a /v1/query response.

    Handles multiple wire shapes MSAM has used:

    - **Two-tier mode** (``[retrieval].two_tier_enabled = true`` —
      msam-hindsight-ideas/core.py:_two_tier_split): the response contains
      ``observations`` and ``raws`` as separate lists. Both contribute
      atoms; observations come first because they're the higher-level
      consolidated inferences and the agent benefits from seeing those
      before the raw evidence atoms.
    - **Single-tier mode** (current default): the server flattens to
      ``atoms`` (server.py:api_query line 366). Both raws and (when
      consolidation has run) observations land here, distinguished by
      ``memory_type``.
    - **Legacy / never-shipped shapes**: ``_raw_atoms`` / ``raw_atoms``
      were the keys in older internal docs; ``sections.*`` is the
      /v1/context-style flat list. Kept as fallbacks against schema drift.

    Order: observations → raws → atoms → legacy → sections. Within a
    single shape, lists are concatenated in the order MSAM returned them
    (which is already similarity-ranked).
    """
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    for key in ("observations", "raws", "atoms", "_raw_atoms", "raw_atoms"):
        for atom in payload.get(key) or []:
            if isinstance(atom, dict):
                out.append(atom)
    sections = payload.get("sections") or {}
    if isinstance(sections, dict):
        for atoms in sections.values():
            for a in atoms or []:
                if isinstance(a, dict):
                    out.append(a)
    return out


def _atom_ids_from_response(payload: dict[str, Any]) -> list[str]:
    """Extract atom IDs from a /v1/query response."""
    out: list[str] = []
    for atom in _atoms_in_payload(payload):
        aid = atom.get("id") or atom.get("atom_id")
        if aid:
            out.append(str(aid))
    return out


def _hits_summary(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Slim hits list for tool result text. Mirrors the pre-message hook shape.

    ``memory_type`` (observation vs raw, two-tier mode) and
    ``confidence_tier`` (per-atom, post per-atom-gating) surface when
    present so the agent can triage retrieval by source quality. Higher
    tiers beat lower tiers; observations beat raws on average.
    """
    out: list[dict[str, Any]] = []
    for atom in _atoms_in_payload(payload):
        item = {
            "atom_id": atom.get("id") or atom.get("atom_id"),
            "stream": atom.get("stream") or atom.get("kind"),
            "content": atom.get("content"),
            "score": atom.get("_activation") or atom.get("score") or atom.get("similarity"),
            "confidence": atom.get("encoding_confidence") or atom.get("confidence"),
        }
        mt = atom.get("memory_type")
        if mt:
            item["memory_type"] = mt
        tier = atom.get("confidence_tier") or atom.get("_confidence_tier")
        if tier:
            item["confidence_tier"] = tier
        ec = atom.get("evidence_count")
        if ec is not None:
            item["evidence_count"] = ec
        out.append(item)
    return out


def build_msam_tools(
    client: MsamClient,
    session_boundary_log: SessionBoundaryLog | None = None,
) -> list[SdkMcpTool]:
    @tool(
        "msam_query",
        "Query MSAM for atoms relevant to a topic. Returns up to top_k hits, "
        "each tagged with memory_type (observation|raw) and confidence_tier "
        "(high|medium|low). Observations are MSAM's consolidated higher-"
        "confidence atoms; raws are the underlying evidence. Prefer hits "
        "with memory_type=observation and confidence_tier=high. Atom IDs "
        "are auto-tracked on the current turn so they get credited at "
        "post-message — you do not need to call msam_mark_contributions "
        "for these. Pass min_confidence_tier in {none|low|medium|high} to "
        "raise the per-atom floor; default is MSAM's server-side setting.",
        # Explicit JSON schema so top_k + min_confidence_tier are optional.
        # Without this, the SDK marks every dict-style key as required.
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "min_confidence_tier": {"type": "string"},
            },
            "required": ["query"],
        },
    )
    @_safe("msam_query")
    async def msam_query(args: dict[str, Any]) -> dict[str, Any]:
        q = _need(args, "query")
        try:
            top_k = max(1, min(int(args.get("top_k", 12)), 50))
        except (TypeError, ValueError):
            top_k = 12
        min_tier = (args.get("min_confidence_tier") or "").strip() or None
        ctx = get_current_turn()
        sid = ctx.msam_session_id if ctx else None
        try:
            payload = await client.query(
                q, top_k=top_k, session_id=sid, min_confidence_tier=min_tier
            )
        except MsamError as exc:
            return _content_block(f"msam_query failed: {exc}", is_error=True)

        ids = _atom_ids_from_response(payload)
        if ctx is not None and ids:
            # SPEC §9.3 mid-turn tracking — append (de-duped) to the union set.
            seen = set(ctx.msam_atom_ids)
            for aid in ids:
                if aid not in seen:
                    ctx.msam_atom_ids.append(aid)
                    seen.add(aid)

        hits = _hits_summary(payload)
        return _content_block(json.dumps(hits, indent=2, ensure_ascii=False))

    @tool(
        "msam_store",
        "Explicitly store a memory atom. MSAM auto-extracts atoms from message "
        "content, so you rarely need this — only call it for facts you want "
        "stored verbatim that wouldn't otherwise be picked up.",
        {"content": str, "stream": str},
    )
    @_safe("msam_store")
    async def msam_store(args: dict[str, Any]) -> dict[str, Any]:
        content = _need(args, "content")
        stream = (args.get("stream") or "").strip() or None
        try:
            payload = await client.store(content=content, stream=stream)
        except MsamError as exc:
            return _content_block(f"msam_store failed: {exc}", is_error=True)
        return _content_block(json.dumps(payload, ensure_ascii=False))

    @tool(
        "msam_feedback",
        "Mark a single atom as useful/incorrect/stale. Maps internally to "
        "MSAM's /v1/outcome (useful→positive, incorrect→negative, stale→negative).",
        {"atom_id": str, "signal": str},
    )
    @_safe("msam_feedback")
    async def msam_feedback(args: dict[str, Any]) -> dict[str, Any]:
        atom_id = _need(args, "atom_id")
        signal = _need(args, "signal").strip().lower()
        wire = _FEEDBACK_MAP.get(signal)
        if wire is None:
            return _content_block(
                f"msam_feedback failed: signal must be useful|incorrect|stale (got {signal!r})",
                is_error=True,
            )
        ctx = get_current_turn()
        sid = ctx.msam_session_id if ctx else None
        try:
            await client.outcome([atom_id], feedback=wire, session_id=sid)
        except MsamError as exc:
            return _content_block(f"msam_feedback failed: {exc}", is_error=True)
        return _content_block(f"msam_feedback ok: {atom_id} → {wire}")

    @tool(
        "msam_mark_contributions",
        "Manually credit a list of atom_ids against the response text. Normally "
        "the post-message hook handles this automatically with the union of "
        "pre-injected and mid-turn-queried atoms. Use this only if you want to "
        "credit atoms outside the standard flow.",
        {"atom_ids": list[str], "response_text": str},
    )
    @_safe("msam_mark_contributions")
    async def msam_mark_contributions(args: dict[str, Any]) -> dict[str, Any]:
        atom_ids = args.get("atom_ids") or []
        if not isinstance(atom_ids, list) or not all(isinstance(a, str) for a in atom_ids):
            return _content_block(
                "msam_mark_contributions failed: atom_ids must be a list of strings",
                is_error=True,
            )
        response_text = args.get("response_text") or ""
        if not isinstance(response_text, str):
            return _content_block(
                "msam_mark_contributions failed: response_text must be a string",
                is_error=True,
            )
        ctx = get_current_turn()
        sid = ctx.msam_session_id if ctx else None
        try:
            await client.feedback(atom_ids, response_text, session_id=sid)
        except MsamError as exc:
            return _content_block(f"msam_mark_contributions failed: {exc}", is_error=True)
        return _content_block(
            f"msam_mark_contributions ok: credited {len(atom_ids)} atoms"
        )

    @tool(
        "msam_end_session",
        "Write a session_boundary atom for an MSAM session. Auto-invoked by "
        "the synthesis turn at idle timeout (SPEC §5.6); call explicitly if "
        "you know a session is wrapping (user says 'talk later'). Empty "
        "lists / None for the optional fields are dropped.",
        {
            "session_id": str,
            "summary": str,
            "topics_discussed": list[str],
            "decisions_made": list[str],
            "unfinished": list[str],
            "emotional_state": str,
        },
    )
    @_safe("msam_end_session")
    async def msam_end_session(args: dict[str, Any]) -> dict[str, Any]:
        session_id = _need(args, "session_id")
        summary = _need(args, "summary")

        def _opt_list(name: str) -> list[str] | None:
            val = args.get(name)
            if not val:
                return None
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                raise _ArgError(f"{name} must be a list of strings")
            return [x for x in val if x.strip()]

        topics = _opt_list("topics_discussed")
        decisions = _opt_list("decisions_made")
        unfinished = _opt_list("unfinished")
        emotional = (args.get("emotional_state") or "").strip() or None

        try:
            payload = await client.end_session(
                session_id=session_id,
                summary=summary,
                topics_discussed=topics,
                decisions_made=decisions,
                unfinished=unfinished,
                emotional_state=emotional,
            )
        except MsamError as exc:
            return _content_block(f"msam_end_session failed: {exc}", is_error=True)

        # v0.4 §3: append to local mirror so the prompt-time render still
        # has session summaries available if MSAM is briefly down. Best
        # effort — failures don't fail the tool turn.
        if session_boundary_log is not None:
            ctx = get_current_turn()
            try:
                await session_boundary_log.append(
                    {
                        "channel_id": ctx.channel_id if ctx else None,
                        "msam_session_id": session_id,
                        "atom_id": payload.get("atom_id"),
                        "summary": summary,
                        "topics_discussed": topics or [],
                        "decisions_made": decisions or [],
                        "unfinished": unfinished or [],
                        "emotional_state": emotional,
                    }
                )
            except Exception:  # noqa: BLE001
                log.exception("session_boundary_log append failed")

        return _content_block(
            f"msam_end_session ok: session_id={session_id} atom_id={payload.get('atom_id')}"
        )

    return [msam_query, msam_store, msam_feedback, msam_mark_contributions, msam_end_session]


def msam_tool_names() -> list[str]:
    return [
        "mcp__mimir__msam_query",
        "mcp__mimir__msam_store",
        "mcp__mimir__msam_feedback",
        "mcp__mimir__msam_mark_contributions",
        "mcp__mimir__msam_end_session",
    ]
