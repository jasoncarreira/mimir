"""LLM extraction of commitments from session-boundary synthesis output.

Phase 2a of commitments. Fires on ``trigger="saga_session_end"`` turns —
the session synthesis already distills "what happened" + carried-forward
unfinished items into 100-1100 chars of prose; this module asks Claude
(haiku-tier) to structure that prose into ``CommitmentRecord`` shape.

**Why this lives in extractor.py, not the agent loop or a poller**:

- Not the agent loop: the session-end synthesis turn is already an
  LLM call; we don't want to wrap that in another LLM call from the
  same loop. The extraction runs in the finalize hook on a separate
  one-shot LLM call via saga's ``call_llm`` (same provider-dispatch
  chain as query rewrite, consolidation, atom annotation, etc.).
  Operators control via ``[commitments]`` section in saga.toml,
  falling back to ``[llm]`` for the global default.
- Not a poller: session-end output is event-driven, not time-driven.
  A poller would either miss output (debounced) or re-extract the
  same content (idempotent only via the store's dedupe-key gate).

**Prompt design**: see ``scratch/commitments_backtest_report.md`` for
the Phase 0 backtest (~95% precision / ~85% recall over 30 historical
session-ends with the v3 prompt). The system prompt + user template
below are the v4 verbatim, with the dynamic fields filled at extraction
time. v4 adds the self-containment rubric: every ``text`` value must
preserve concrete artifact identifiers (PR/issue/chainlink numbers,
file paths, branch names) and disposition flags ("Optional", "blocker",
"deferred-until-X") from the source bullet. The 120-char budget should
be used, not minimised. See chainlink #137 for the v3→v4 motivation
and the backtest plan. Future-tuning lands here; bump the constant
version comment so the backtest can be rerun on the new prompt.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .models import (
    CommitmentKind,
    CommitmentRecord,
    CommitmentSensitivity,
    make_commitment_id,
    make_dedupe_key,
)

if TYPE_CHECKING:
    from mimir.models import SessionACL

log = logging.getLogger(__name__)


# v4 — adds self-containment rubric (chainlink #137): text must preserve
# artifact identifiers and disposition flags from the source bullet so
# a future turn can evaluate "done/not done?" without backtracking to
# the source turn. v3 baseline: ~95% precision / ~85% recall (validated
# 2026-05-10 against 30 saga_session_end turns from turns.jsonl).
# Bump this version + re-run the backtest on any non-trivial edit.
EXTRACTION_PROMPT_VERSION = "v4"


EXTRACTION_SYSTEM = """\
You extract commitments from an AI agent's session-boundary synthesis output.

A commitment is a future obligation the agent has accepted: either an agent
promise ("I'll review the PR Thursday") or a user request the agent agreed
to follow up on ("let me know how the deploy goes"). Open loops the agent
will revisit also count. Reminders, tasks, "unfinished items," and items
explicitly carried forward all qualify.

DOES NOT count as a commitment:
- Things the agent already did this session ("captured X to memory")
- Vague intentions without a clear action ("we should think about Y")
- Observational notes / patterns / learnings
- Closed-since items (these are completed, not commitments)
- Internal bookkeeping the agent does every session (atom scoring,
  learnings file maintenance)

Output strictly-valid JSON matching this schema:

{
  "commitments": [
    {
      "text": "self-contained description <=120 chars — include artifact refs (PR#, chainlink#, file path) and disposition flags from source",
      "kind": "agent_promise | user_request | deadline_check | open_loop",
      "sensitivity": "routine | personal | care",
      "due_window_hint": "ISO 8601 datetime OR relative phrase OR null",
      "confidence": 0.0-1.0,
      "channel_bound": true | false,
      "recipient_name": "person's name or handle this commitment is FOR, or null",
      "suggested_reminder": "what to say at delivery, <=200 chars"
    }
  ]
}

Rules:
- If no commitments found, return {"commitments": []}.
- Confidence: 1.0 = explicit promise with deadline; 0.7 = clear obligation,
  fuzzy deadline; 0.4 = implied or weak; below 0.4 -> skip (don't emit).
- channel_bound=true when the commitment is intrinsic to a specific
  conversation (e.g., "send Bob the draft"). False when channel-agnostic
  (e.g., "read the paper" -- agent-internal).
- sensitivity: "care" for personal-wellbeing follow-ups (a friend's hard
  week, a health thing); "personal" for individual-but-mundane
  (someone's PR, draft post); "routine" for ops/work tracking.
- due_window_hint: pass through what the source says ("Thursday",
  "next sprint", "soon", "tomorrow"). Null only if truly absent.
- recipient_name: set to the person's name/handle when the commitment
  is explicitly directed at someone ("send Bob the draft", "remind
  Alice about the PR"). Null for agent-internal obligations or when
  no specific person is named.

Recognize passive phrasing. "Remain live", "still-gated", "still-unfinished",
"carry forward", "carried over", "carries forward" -- these signal commitments
even when not under an explicit "unfinished:" bullet.

Be wary of monitoring verbs alone. "Monitor X" / "follow up on X" /
"address X" / "check X" by themselves are too vague to emit -- they don't
say what action will be taken. EXCEPTIONS that make them valid commitments:
- An explicit deadline or trigger ("check PRs #111/#112 at 21:00 UTC",
  "address X once Y merges") -- emit with the deadline preserved.
- A specific deliverable mentioned alongside ("address review comments
  on PR #106 by patching the validator") -- emit.

Without one of those, skip. Prefer concrete verbs (apply, write, send,
review, fix, merge, tighten, resolve, complete, add) or a specific
deliverable as evidence of a real commitment.

text self-containment (REQUIRED): a future turn must be able to decide
"done/not done?" from the text field alone, without looking at the source
turn. Preserve every concrete artifact identifier in the source bullet —
PR #N, issue #N, chainlink #N, file paths, branch names, function names —
and every disposition flag ("Optional", "blocker", "deferred-until-X",
"wontfix-candidate"). You have 120 characters — USE them; do not
over-compress. Under-truncation example to avoid: source says "Cluster B
subissues #115/#116/#117 under chainlink #29 still unimplemented" → do NOT
emit "Cluster B subissues" (stripped identifiers); DO emit "Cluster B
subissues #115/#116/#117 under chainlink #29 unimplemented" (self-contained).

Output ONLY the JSON object. No prose, no code fences.
"""


USER_TEMPLATE = """\
Session metadata:
- channel: {channel_id}
- timestamp: {ts}
- session_id: {saga_session_id}

The agent's session-boundary synthesis output:

<synthesis>
{output}
</synthesis>

Extract commitments per the rules. Return only the JSON object."""


# Default minimum output length to bother running extraction on.
# Trivially-short session-ends (single-turn no-ops, "boundary recorded,
# nothing to capture") never carry commitments and cost an LLM call to
# confirm zero. The 100-char floor matches the backtest's filter.
MIN_OUTPUT_LEN = 100


# Default model name (no provider prefix). Provider dispatch comes from
# saga's resolved llm config — same path as consolidation, query rewrite,
# atom annotation, and the rest of saga's LLM call sites. Operators set
# per-subsystem overrides via ``[commitments] provider = ...`` in
# saga.toml, falling back to ``[llm] provider`` for the global default.
#
# Pre-fix this was ``"anthropic:claude-haiku-4-5"`` and the extractor
# called ``langchain.chat_models.init_chat_model`` directly — which
# requires ``ANTHROPIC_API_KEY`` even when the rest of the deploy is
# running on OAuth via Claude Code (mimirbot's setup). That bypassed
# saga's whole config system. Now we route through ``saga._llm.call_llm``
# so the same provider selection chain governs the extractor's call.
DEFAULT_EXTRACTOR_MODEL: str | None = None


def _strip_code_fence(body: str) -> str:
    """Strip a leading ```json fence if Claude returned one despite the
    'no code fences' rule. Defensive — backtest showed the v3 prompt
    almost never emits fences, but the parse-error rate is non-zero."""
    body = body.strip()
    if body.startswith("```"):
        parts = body.split("```")
        if len(parts) >= 2:
            body = parts[1]
            if body.startswith("json"):
                body = body[4:]
            body = body.strip()
    return body


def _parse_extraction_json(raw: str) -> dict[str, Any] | None:
    """Parse the LLM's response into a dict, or return None on failure.
    Logs but doesn't raise — extraction is best-effort."""
    body = _strip_code_fence(raw)
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        log.warning(
            "commitments extractor: JSON parse failed (%s); raw[:200]=%r",
            exc, raw[:200],
        )
        return None


_VALID_KINDS = {k.value for k in CommitmentKind}
_VALID_SENSITIVITIES = {s.value for s in CommitmentSensitivity}


def assign_extraction_acl(
    record: CommitmentRecord,
    source_acl: "SessionACL | None",
    *,
    service_name: str,
) -> None:
    """Stamp authoritative source ownership, then compute owner-scoped dedupe."""
    if source_acl is not None and source_acl.provenance_complete:
        record.owner_principal = source_acl.owner_principal
        record.originating_channel = source_acl.origin_channel
        record.origin_domain = source_acl.origin_domain
        record.visibility = source_acl.visibility
    else:
        record.owner_principal = "legacy_admin"
        record.originating_channel = None
        record.origin_domain = None
        record.visibility = "service"
    record.service_name = service_name
    record.dedupe_key = make_dedupe_key(
        channel_id=record.channel_id,
        text=record.text,
        due_window_start_unix=record.due_window_start_unix,
        recipient_identity=record.recipient_identity,
        owner_principal=record.owner_principal,
    )


def _coerce_to_record(
    item: dict[str, Any],
    *,
    channel_id: str | None,
    saga_session_id: str | None,
    source_turn_id: str | None,
) -> CommitmentRecord | None:
    """Map one extracted item dict → ``CommitmentRecord``. Returns
    None on schema-violation (missing required field, unknown enum
    value, confidence below floor).

    Field-level validation is loose: unknown ``kind`` defaults to
    ``open_loop``; unknown ``sensitivity`` defaults to ``routine``;
    missing ``suggested_reminder`` falls back to ``text``. The
    backtest showed the LLM follows the schema reliably for the
    big-three fields (text, kind, confidence); the rest tolerate
    drift without breaking the record."""
    text = (item.get("text") or "").strip()
    if not text:
        return None
    confidence = float(item.get("confidence") or 0.0)
    if confidence < 0.4:
        # Below the floor — backtest showed sub-0.4 commitments were
        # mostly false positives. Drop silently.
        return None
    kind = item.get("kind") or CommitmentKind.OPEN_LOOP.value
    if kind not in _VALID_KINDS:
        kind = CommitmentKind.OPEN_LOOP.value
    sensitivity = item.get("sensitivity") or CommitmentSensitivity.ROUTINE.value
    if sensitivity not in _VALID_SENSITIVITIES:
        sensitivity = CommitmentSensitivity.ROUTINE.value
    suggested_reminder = (item.get("suggested_reminder") or text).strip()
    # Chainlink #96: extract recipient_name from schema (raw display name
    # or handle the LLM picked up from conversation context).  Not a
    # resolved canonical identity — just whatever name appeared in the
    # synthesis text ("Bob", "Jason", "mimir-carreira").  Store as
    # recipient_identity so the delivery layer can @-mention it.
    recipient_identity: str | None = None
    raw_recipient = item.get("recipient_name")
    if isinstance(raw_recipient, str) and raw_recipient.strip():
        recipient_identity = raw_recipient.strip()[:100]
    # PR #125 review #5: default-False is safer than default-True for
    # poller-driven session-ends. Most poller channels (e.g.
    # ``poller:github-activity``) are non-personal; a missed
    # ``channel_bound`` would otherwise scope a generic commitment
    # (``read the paper``) to a poller channel where it never
    # surfaces to the operator. Default-False = "if uncertain,
    # surface cross-channel" — over-surface > under-surface for
    # extracted commitments.
    channel_bound = bool(item.get("channel_bound", False))
    bound_channel = channel_id if channel_bound else None

    # PR #125 review #4: preserve the LLM's natural-language time
    # anchor verbatim. We don't parse "Thursday" / "next sprint" /
    # "tomorrow" into unix seconds at extraction time — those don't
    # have a single right answer — but the hint goes onto the record
    # so Phase 2b's poller + Phase 3's prompt block can render the
    # operator-facing phrasing and a future hint-to-unix parser has
    # the raw source available. ``None`` when the LLM omits it.
    due_window_hint = item.get("due_window_hint")
    if isinstance(due_window_hint, str):
        due_window_hint = due_window_hint.strip() or None
    else:
        due_window_hint = None

    # Chainlink #97: if the hint is an ISO 8601 datetime string, parse
    # it into due_window_start_unix so the due-check poller can fire on
    # schedule. Relative phrases ("Thursday", "next sprint", "tomorrow")
    # will raise ValueError and leave due_window_start_unix=None — the
    # hint is still preserved verbatim for operator-facing rendering.
    # Python <3.11: fromisoformat doesn't accept Z suffix; replace it.
    due_window_start_unix: float | None = None
    if due_window_hint:
        try:
            _hint_iso = due_window_hint.replace("Z", "+00:00")
            dt = datetime.fromisoformat(_hint_iso)
            if dt.tzinfo is None:
                # No timezone info — treat as UTC (consistent with mimir's
                # convention; the prompt asks for ISO but doesn't mandate tz).
                dt = dt.replace(tzinfo=timezone.utc)
            due_window_start_unix = dt.timestamp()
        except (ValueError, OverflowError, AttributeError):
            # Not an ISO datetime string; leave hint-only, start_unix = None.
            pass

    rec = CommitmentRecord(
        id=make_commitment_id(),
        channel_id=bound_channel,
        text=text[:200],  # generous cap; the prompt asks for ≤120
        kind=kind,
        sensitivity=sensitivity,
        suggested_reminder=suggested_reminder[:300],
        due_window_start_unix=due_window_start_unix,
        due_window_end_unix=None,
        due_window_hint=due_window_hint,
        recipient_identity=recipient_identity,
        confidence=confidence,
        source_turn_id=source_turn_id,
        saga_session_id=saga_session_id,
        created_at_unix=time.time(),
        # PR #125 review #1: provenance — which prompt version produced
        # this record. Filterable in backtest comparisons.
        extraction_prompt_version=EXTRACTION_PROMPT_VERSION,
    )
    rec.dedupe_key = make_dedupe_key(
        channel_id=bound_channel,
        text=rec.text,
        due_window_start_unix=due_window_start_unix,
        recipient_identity=recipient_identity,
        owner_principal=rec.owner_principal,
    )
    return rec


async def extract_commitments(
    session_end_output: str,
    *,
    channel_id: str | None,
    saga_session_id: str | None,
    source_turn_id: str | None,
    model: str | None = DEFAULT_EXTRACTOR_MODEL,
) -> list[CommitmentRecord]:
    """Run the LLM extraction on a session-end synthesis output, return
    a list of ``CommitmentRecord``s ready for ``store.add()``.

    Provider dispatch follows saga's ``call_llm`` selection chain —
    same path as query rewrite, consolidation, and the rest of saga's
    LLM call sites. Operators control via ``saga.toml`` (per-subsystem
    ``[commitments]`` section overrides global ``[llm]``).

    Best-effort:
    - Short outputs (<100 chars) → skip without LLM call (no commitments
      worth extracting).
    - LLM-call failures → log + return ``[]`` (don't bubble; the agent
      finalize hook can't recover from a bad extraction anyway).
    - JSON parse failures → log + return ``[]``.
    - Per-item validation failures → drop the item silently (low
      confidence, missing text).

    Caller (the ``CommitmentExtractionHook``) is responsible for
    dedupe gating via ``store.find_by_dedupe_key()`` before adding.
    Each record's ``dedupe_key`` is filled in by ``_coerce_to_record``.

    Args:
        model: Optional override for the resolved llm config's ``model``
            field. Accepts a bare name (``"claude-haiku-4-5"``); a
            ``provider:`` prefix is stripped if present (the provider
            still comes from saga's resolved config). ``None`` uses
            the configured model verbatim.
    """
    if not session_end_output or len(session_end_output) < MIN_OUTPUT_LEN:
        return []

    user_msg = USER_TEMPLATE.format(
        channel_id=channel_id or "(none)",
        ts="",
        saga_session_id=saga_session_id or "(none)",
        output=session_end_output,
    )

    raw_text = ""
    try:
        from mimir.saga._llm import call_llm
        from mimir.saga._config_io import resolve_llm_config

        cfg = dict(resolve_llm_config("commitments"))
        if model:
            # Strip a ``provider:`` prefix if present — provider always
            # comes from saga config, model name from the override.
            cfg["model"] = model.split(":", 1)[-1]

        raw_text = await call_llm(
            cfg,
            prompt=user_msg,
            system=EXTRACTION_SYSTEM,
            # Extraction is JSON; deterministic output wanted.
            temperature=0.0,
            # Multiple commitments per session = a few hundred tokens
            # of JSON. 2000 leaves headroom for long sessions without
            # blowing the budget.
            max_tokens=2000,
        )
    except Exception:  # noqa: BLE001
        log.exception("commitments extractor: LLM call failed")
        return []

    parsed = _parse_extraction_json(raw_text)
    if not parsed:
        return []

    items = parsed.get("commitments") or []
    out: list[CommitmentRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rec = _coerce_to_record(
            item,
            channel_id=channel_id,
            saga_session_id=saga_session_id,
            source_turn_id=source_turn_id,
        )
        if rec is not None:
            out.append(rec)
    return out
