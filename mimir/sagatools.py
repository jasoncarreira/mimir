"""SAGA-facing MCP tools (SPEC §8.2).

Wraps the SagaClient as five tools:
  - saga_query              — explicit semantic atom retrieval
  - saga_store              — explicit atom store (rare; SAGA auto-extracts)
  - saga_feedback           — corrective signal on a single atom
  - saga_mark_contributions — manual variant of the post-message hook
  - saga_end_session        — synthesis-turn bookkeeping (SPEC §5.6)

Most SAGA activity is automatic via the pre/post-message hooks (SPEC §9.3).
Mid-turn ``saga_query`` results are auto-appended to the parent's
``TurnContext.saga_atom_ids`` so the post-message hook credits them — agent
doesn't have to remember.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ._context import resolve_active_ctx
from ._tool_helpers import _ArgError, _content_block, _need, _safe
from .event_logger import log_event
from .saga_client import SagaClient, SagaError
from .session_boundary_log import SessionBoundaryLog

log = logging.getLogger(__name__)

# Friendly mimir signal → SAGA /v1/outcome wire vocabulary.
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
    """Render SAGA hits as a brief bullet list — tag + content, no IDs.
    Used by the pre-message hook (SPEC §9.3) and the saga_query tool result."""
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


def _triples_in_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the triples list from a /v1/query response.

    P42 surfaces triples as a third response block alongside
    observations/raws. Empty when the saga config has
    ``[retrieval] include_triples_in_response = false`` (the default)
    or when there are no matching triples."""
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    for t in payload.get("triples") or []:
        if isinstance(t, dict):
            out.append(t)
    return out


def _source_atom_ids_from_triples(payload: dict[str, Any]) -> list[str]:
    """Pull each triple's ``source_atom_id`` so the post-message hook
    can credit those atoms via ``mark_contributions``. When the agent
    grounds its reply in a triple, the originating atom earned its
    keep — same contribution-credit logic as for surfaced atoms.

    De-dups and preserves first-seen order. Atoms missing the field
    (legacy rows or non-P42 responses) are skipped silently."""
    seen: set[str] = set()
    out: list[str] = []
    for t in _triples_in_payload(payload):
        atom_id = t.get("source_atom_id")
        if isinstance(atom_id, str) and atom_id and atom_id not in seen:
            out.append(atom_id)
            seen.add(atom_id)
    return out


def _fmt_iso_date(raw: object) -> str | None:
    """Render an ISO timestamp as a compact YYYY-MM-DD. Returns None for
    missing / unparseable values so the caller can omit the field."""
    if not isinstance(raw, str) or not raw:
        return None
    return raw[:10]  # ISO-8601 starts with YYYY-MM-DD


def _format_triples(triples: list[dict[str, Any]]) -> str:
    """Render P42 triples as a compact bullet list with valid-date
    range and confidence. The agent uses these as structured fact
    grounding alongside the prose atoms."""
    if not triples:
        return ""
    lines: list[str] = []
    for t in triples:
        subj = t.get("subject") or "?"
        pred = t.get("predicate") or "?"
        obj = t.get("object") or "?"
        valid_from = _fmt_iso_date(t.get("valid_from"))
        valid_until = _fmt_iso_date(t.get("valid_until"))
        if valid_from and valid_until:
            date_part = f" [valid {valid_from} → {valid_until}]"
        elif valid_from:
            date_part = f" [valid {valid_from} → present]"
        elif valid_until:
            date_part = f" [valid → {valid_until}]"
        else:
            date_part = ""
        conf = t.get("confidence")
        conf_part = ""
        if isinstance(conf, (int, float)) and conf < 1.0:
            conf_part = f" (conf {conf:.2f})"
        lines.append(f"- ({subj}, {pred}, {obj}){date_part}{conf_part}")
    return "\n".join(lines)


def _format_saga_payload(payload: dict[str, Any]) -> str:
    """Combined renderer: atoms first, then a Triples sub-section if
    P42 surfaced any. Used by the pre-message hook so the agent
    reads structured facts alongside prose hits."""
    atoms = _atoms_in_payload(payload)
    triples = _triples_in_payload(payload)
    parts: list[str] = []
    if atoms:
        parts.append(_format_atoms(atoms))
    if triples:
        triples_block = _format_triples(triples)
        if triples_block:
            if parts:
                parts.append("")
            parts.append("Triples:")
            parts.append(triples_block)
    if not parts:
        return "(no atoms)"
    return "\n".join(parts)


def _atoms_in_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the atoms list from a /v1/query response.

    Handles multiple wire shapes SAGA has used:

    - **Two-tier mode** (``[retrieval].two_tier_enabled = true`` —
      saga.core._two_tier_split): the response contains
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
    single shape, lists are concatenated in the order SAGA returned them
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


def build_saga_tools(
    client: SagaClient,
    session_boundary_log: SessionBoundaryLog | None = None,
) -> list[SdkMcpTool]:
    @tool(
        "saga_query",
        "Query SAGA for atoms relevant to a topic. Returns up to top_k hits, "
        "each tagged with memory_type (observation|raw) and confidence_tier "
        "(high|medium|low). Observations are SAGA's consolidated higher-"
        "confidence atoms; raws are the underlying evidence. Prefer hits "
        "with memory_type=observation and confidence_tier=high. Atom IDs "
        "are auto-tracked on the current turn so they get credited at "
        "post-message — you do not need to call saga_mark_contributions "
        "for these. Pass min_confidence_tier in {none|low|medium|high} to "
        "raise the per-atom floor; default is SAGA's server-side setting. "
        "Pass ``session_id`` (your current saga_session_id from the "
        "Current-message header) so the handler can scope retrieval and "
        "credit retrieved atoms back to your turn — without it, mid-turn "
        "atom auto-credit may silently fail under multi-channel concurrency.",
        # Explicit JSON schema so top_k + min_confidence_tier + session_id
        # are optional. Without this, the SDK marks every dict-style key as
        # required.
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "min_confidence_tier": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["query"],
        },
    )
    @_safe("saga_query")
    async def saga_query(args: dict[str, Any]) -> dict[str, Any]:
        q = _need(args, "query")
        try:
            top_k = max(1, min(int(args.get("top_k", 12)), 50))
        except (TypeError, ValueError):
            top_k = 12
        min_tier = (args.get("min_confidence_tier") or "").strip() or None
        # chainlink #23 #26: lookup chain replaces bare get_current_turn().
        # See resolve_active_ctx docstring for the three-level fallback order.
        ctx, resolution_path = resolve_active_ctx(args)
        sid = ctx.saga_session_id if ctx else None
        await log_event(
            "saga_query_ctx_resolution",
            resolution_path=resolution_path,
            saga_session_id=sid,
            turn_id=ctx.turn_id if ctx is not None else None,
        )
        try:
            payload = await client.query(
                q, top_k=top_k, session_id=sid, min_confidence_tier=min_tier
            )
        except SagaError as exc:
            return _content_block(f"saga_query failed: {exc}", is_error=True)

        ids = _atom_ids_from_response(payload)
        if ctx is not None and ids:
            # SPEC §9.3 mid-turn tracking — append (de-duped) to the union set.
            seen = set(ctx.saga_atom_ids)
            for aid in ids:
                if aid not in seen:
                    ctx.saga_atom_ids.append(aid)
                    seen.add(aid)

        hits = _hits_summary(payload)
        return _content_block(json.dumps(hits, indent=2, ensure_ascii=False))

    @tool(
        "saga_store",
        "Explicitly store a memory atom. SAGA auto-extracts atoms from message "
        "content, so you rarely need this — only call it for facts you want "
        "stored verbatim that wouldn't otherwise be picked up. Pass "
        "``session_id`` (your current saga_session_id from the Current-message "
        "header) so the stored atom is scoped to your turn.",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "stream": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["content", "stream"],
        },
    )
    @_safe("saga_store")
    async def saga_store(args: dict[str, Any]) -> dict[str, Any]:
        content = _need(args, "content")
        stream = (args.get("stream") or "").strip() or None
        # chainlink #23 #26: log ctx resolution for observability parity
        # with the other saga tools. SagaClient.store doesn't currently
        # accept session_id (storage is un-scoped at the client interface);
        # tracking resolution_path here keeps the event stream uniform so
        # future wire-up of session-scoped storage doesn't need a separate
        # observability rollout.
        ctx, resolution_path = resolve_active_ctx(args)
        sid = ctx.saga_session_id if ctx else None
        await log_event(
            "saga_store_ctx_resolution",
            resolution_path=resolution_path,
            saga_session_id=sid,
            turn_id=ctx.turn_id if ctx is not None else None,
        )
        try:
            payload = await client.store(content=content, stream=stream)
        except SagaError as exc:
            return _content_block(f"saga_store failed: {exc}", is_error=True)
        return _content_block(json.dumps(payload, ensure_ascii=False))

    @tool(
        "saga_feedback",
        "Mark a single atom as useful/incorrect/stale. Maps internally to "
        "SAGA's /v1/outcome (useful→positive, incorrect→negative, stale→negative). "
        "Pass ``session_id`` (your current saga_session_id from the Current-message "
        "header) so the outcome is recorded against your turn.",
        {
            "type": "object",
            "properties": {
                "atom_id": {"type": "string"},
                "signal": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["atom_id", "signal"],
        },
    )
    @_safe("saga_feedback")
    async def saga_feedback(args: dict[str, Any]) -> dict[str, Any]:
        atom_id = _need(args, "atom_id")
        signal = _need(args, "signal").strip().lower()
        wire = _FEEDBACK_MAP.get(signal)
        if wire is None:
            return _content_block(
                f"saga_feedback failed: signal must be useful|incorrect|stale (got {signal!r})",
                is_error=True,
            )
        ctx, resolution_path = resolve_active_ctx(args)
        sid = ctx.saga_session_id if ctx else None
        await log_event(
            "saga_feedback_ctx_resolution",
            resolution_path=resolution_path,
            saga_session_id=sid,
            turn_id=ctx.turn_id if ctx is not None else None,
        )
        try:
            await client.outcome([atom_id], feedback=wire, session_id=sid)
        except SagaError as exc:
            return _content_block(f"saga_feedback failed: {exc}", is_error=True)
        return _content_block(f"saga_feedback ok: {atom_id} → {wire}")

    @tool(
        "saga_mark_contributions",
        "Manually credit a list of atom_ids against the response text. Normally "
        "the post-message hook handles this automatically with the union of "
        "pre-injected and mid-turn-queried atoms. Use this only if you want to "
        "credit atoms outside the standard flow. Pass ``session_id`` (your "
        "current saga_session_id from the Current-message header) so the credit "
        "is recorded against your turn.",
        {
            "type": "object",
            "properties": {
                "atom_ids": {"type": "array", "items": {"type": "string"}},
                "response_text": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["atom_ids", "response_text"],
        },
    )
    @_safe("saga_mark_contributions")
    async def saga_mark_contributions(args: dict[str, Any]) -> dict[str, Any]:
        atom_ids = args.get("atom_ids") or []
        if not isinstance(atom_ids, list) or not all(isinstance(a, str) for a in atom_ids):
            return _content_block(
                "saga_mark_contributions failed: atom_ids must be a list of strings",
                is_error=True,
            )
        response_text = args.get("response_text") or ""
        if not isinstance(response_text, str):
            return _content_block(
                "saga_mark_contributions failed: response_text must be a string",
                is_error=True,
            )
        ctx, resolution_path = resolve_active_ctx(args)
        sid = ctx.saga_session_id if ctx else None
        await log_event(
            "saga_mark_contributions_ctx_resolution",
            resolution_path=resolution_path,
            saga_session_id=sid,
            turn_id=ctx.turn_id if ctx is not None else None,
        )
        try:
            await client.feedback(atom_ids, response_text, session_id=sid)
        except SagaError as exc:
            return _content_block(f"saga_mark_contributions failed: {exc}", is_error=True)
        return _content_block(
            f"saga_mark_contributions ok: credited {len(atom_ids)} atoms"
        )

    @tool(
        "saga_end_session",
        "Write a session_boundary atom for an SAGA session. Auto-invoked by "
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
    @_safe("saga_end_session")
    async def saga_end_session(args: dict[str, Any]) -> dict[str, Any]:
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
        except SagaError as exc:
            return _content_block(f"saga_end_session failed: {exc}", is_error=True)

        # CR#19: flag the per-turn ctx so the agent's post-message hook
        # can tell that step 3 of the synthesis prompt actually ran. The
        # check fires only on synthesis turns (trigger=saga_session_end);
        # for non-synthesis callers (rare — operator manually closes a
        # session) it's harmless extra bookkeeping.
        #
        # chainlink #23 subissue #25: the SDK dispatches MCP tool handlers
        # on a fresh asyncio task forked at first connect. ``_current_turn``
        # is invisible inside that task even when ``run_turn`` set it on
        # its own task — same pattern as hooks (CR#18). Use the shared
        # three-level lookup (saga_session_id → single_active → contextvar
        # → missing). ``saga_session_id`` is the load-bearing path here;
        # the middle step is harmless extra coverage when the model
        # forgets to pass session_id (synthesis turns are serialized so
        # single_active typically resolves to the right ctx).
        # ``resolution_path`` mirrors CR#18's pattern so the rate of each
        # path is visible in events.jsonl.
        ctx, resolution_path = resolve_active_ctx({"session_id": session_id})
        if ctx is not None:
            ctx.saga_end_session_called = True
        await log_event(
            "saga_synthesis_ctx_resolution",
            saga_session_id=session_id,
            resolution_path=resolution_path,
            turn_id=ctx.turn_id if ctx is not None else None,
            channel_id=ctx.channel_id if ctx is not None else None,
        )

        # v0.4 §3: append to local mirror so the prompt-time render still
        # has session summaries available if SAGA is briefly down. Best
        # effort — failures don't fail the tool turn.
        if session_boundary_log is not None:
            try:
                await session_boundary_log.append(
                    {
                        "channel_id": ctx.channel_id if ctx else None,
                        "saga_session_id": session_id,
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
            f"saga_end_session ok: session_id={session_id} atom_id={payload.get('atom_id')}"
        )

    @tool(
        "saga_forget",
        "Run saga's intentional-forgetting engine — review or remove "
        "atoms that the last decay cycle flagged as low-value (low "
        "retrieval, negative contribution, contradicted, or below the "
        "confidence floor and past grace_days). PREVIEW FIRST: keep "
        "dry_run=true (the default) to inspect the candidate list "
        "before acting. Set dry_run=false only after reviewing — "
        "forgetting is irreversible. Use this when the ## Self-state "
        "block reports pending forget candidates; a successful "
        "non-dry-run call clears that line until the next decay cycle.",
        {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean"},
                "min_retrievals": {"type": "integer"},
                "contribution_threshold": {"type": "number"},
                "contradiction_threshold": {"type": "number"},
                "confidence_floor": {"type": "number"},
                "grace_days": {"type": "integer"},
            },
            "required": [],
        },
    )
    @_safe("saga_forget")
    async def saga_forget(args: dict[str, Any]) -> dict[str, Any]:
        # dry_run defaults True both here and on the saga side — being
        # explicit lets us key the event-emit decision off it cleanly.
        dry_run = bool(args.get("dry_run", True))
        kwargs: dict[str, Any] = {"dry_run": dry_run}
        for key in (
            "min_retrievals",
            "contribution_threshold",
            "contradiction_threshold",
            "confidence_floor",
            "grace_days",
        ):
            if key in args and args[key] is not None:
                kwargs[key] = args[key]
        try:
            payload = await client.forget(**kwargs)
        except SagaError as exc:
            await log_event(
                "saga_forget_error", error=str(exc), dry_run=dry_run,
            )
            return _content_block(f"saga_forget failed: {exc}", is_error=True)

        # Only a non-dry-run call actually transitions atoms. The
        # ## Self-state pending-line clears on saga_forget_ok presence,
        # not count — emitting on dry_run would clear the nag without
        # the agent having acted, which is exactly the bug we're
        # avoiding. Dry-run results still come back in the tool reply
        # so the agent can review.
        if not dry_run:
            await log_event(
                "saga_forget_ok",
                actions_taken=payload.get("actions_taken"),
                total_candidates=payload.get("total_candidates"),
                dry_run=False,
            )

        return _content_block(
            json.dumps(payload, indent=2, ensure_ascii=False)
        )

    return [
        saga_query, saga_store, saga_feedback, saga_mark_contributions,
        saga_end_session, saga_forget,
    ]


def saga_tool_names() -> list[str]:
    return [
        "mcp__mimir__saga_query",
        "mcp__mimir__saga_store",
        "mcp__mimir__saga_feedback",
        "mcp__mimir__saga_mark_contributions",
        "mcp__mimir__saga_end_session",
        "mcp__mimir__saga_forget",
    ]
