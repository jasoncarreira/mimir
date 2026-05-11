"""LLM extraction of commitments from session-boundary synthesis output.

Phase 2a of commitments. Fires on ``trigger="saga_session_end"`` turns —
the session synthesis already distills "what happened" + carried-forward
unfinished items into 100-1100 chars of prose; this module asks Claude
(haiku-tier) to structure that prose into ``CommitmentRecord`` shape.

**Why this lives in extractor.py, not the agent loop or a poller**:

- Not the agent loop: the session-end synthesis turn is already an
  LLM call; we don't want to wrap that in another LLM call from the
  same loop. The extraction runs in the finalize hook on a separate
  ``claude_agent_sdk.query()`` (one-shot, OAuth path, haiku-tier).
- Not a poller: session-end output is event-driven, not time-driven.
  A poller would either miss output (debounced) or re-extract the
  same content (idempotent only via the store's dedupe-key gate).

**Prompt design**: see ``scratch/commitments_backtest_report.md`` for
the Phase 0 backtest (~95% precision / ~85% recall over 30 historical
session-ends with the v3 prompt). The system prompt + user template
below are the v3 verbatim, with the dynamic fields filled at extraction
time. Future-tuning lands here; bump the constant version comment so
the backtest can be rerun on the new prompt against the same corpus.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .models import (
    CommitmentKind,
    CommitmentRecord,
    CommitmentSensitivity,
    make_commitment_id,
    make_dedupe_key,
)

log = logging.getLogger(__name__)


# v3 — validated 2026-05-10 against the last 30 saga_session_end turns
# from /mimir-home/logs/turns.jsonl. ~95% precision, ~85% recall.
# Bump this version + re-run the backtest on any non-trivial edit.
EXTRACTION_PROMPT_VERSION = "v3"


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
      "text": "natural-language description, <=120 chars",
      "kind": "agent_promise | user_request | deadline_check | open_loop",
      "sensitivity": "routine | personal | care",
      "due_window_hint": "ISO 8601 datetime OR relative phrase OR null",
      "confidence": 0.0-1.0,
      "channel_bound": true | false,
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


# Default model. ``haiku`` is the current cheapest tier; the extraction
# is well within haiku's reasoning band per the backtest. The CLI test
# in scratch/ used the full ``haiku`` alias which routes to the latest.
DEFAULT_EXTRACTOR_MODEL = "haiku"


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
    channel_bound = bool(item.get("channel_bound", True))
    bound_channel = channel_id if channel_bound else None

    # We don't parse the due_window_hint into unix seconds at extraction
    # time — natural-language phrases like "next sprint" or "soon"
    # don't have a single right answer. Future-Phase 3 surfacing can
    # apply heuristics or ask the agent. For now we leave start/end as
    # None and store the hint via the record's text/reminder fields.
    rec = CommitmentRecord(
        id=make_commitment_id(),
        channel_id=bound_channel,
        text=text[:200],  # generous cap; the prompt asks for ≤120
        kind=kind,
        sensitivity=sensitivity,
        suggested_reminder=suggested_reminder[:300],
        due_window_start_unix=None,
        due_window_end_unix=None,
        confidence=confidence,
        source_turn_id=source_turn_id,
        saga_session_id=saga_session_id,
        created_at_unix=time.time(),
    )
    rec.dedupe_key = make_dedupe_key(
        channel_id=bound_channel,
        text=rec.text,
        due_window_start_unix=None,
        recipient_identity=None,
    )
    return rec


async def extract_commitments(
    session_end_output: str,
    *,
    channel_id: str | None,
    saga_session_id: str | None,
    source_turn_id: str | None,
    model: str = DEFAULT_EXTRACTOR_MODEL,
) -> list[CommitmentRecord]:
    """Run the LLM extraction on a session-end synthesis output, return
    a list of ``CommitmentRecord``s ready for ``store.add()``.

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
    """
    if not session_end_output or len(session_end_output) < MIN_OUTPUT_LEN:
        return []

    # Import the SDK at call time (not module load) so the test path
    # can monkeypatch ``claude_agent_sdk.query`` without dragging in
    # the live transport.
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    user_msg = USER_TEMPLATE.format(
        channel_id=channel_id or "(none)",
        ts="",  # injected at the LLM's discretion if it needs it
        saga_session_id=saga_session_id or "(none)",
        output=session_end_output,
    )
    options = ClaudeAgentOptions(
        system_prompt=EXTRACTION_SYSTEM,
        model=model,
        # No tools — text-only extraction. Constrains the loop to a
        # single assistant turn.
        allowed_tools=[],
        max_turns=1,
    )

    raw_text = ""
    try:
        async for msg in query(prompt=user_msg, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        raw_text += block.text
    except Exception:  # noqa: BLE001
        log.exception("commitments extractor: SDK query failed")
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
