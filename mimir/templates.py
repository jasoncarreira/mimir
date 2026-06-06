"""Prompt templates loaded from disk with bundled defaults.

The spec lists templates under ``mimir/prompts/`` but that path collides with
``mimir/prompts.py`` (assembly module) under Python's import system, so the
bundled defaults live here as constants. ``MIMIR_PROMPTS_DIR`` (SPEC §14)
still overrides per-deployment — looked up by template name (e.g.
``<dir>/saga_session_end.md``) and falls back to the default below.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


SAGA_SESSION_END_DEFAULT = """\
The SAGA session for channel {channel_id} has been idle for {idle_minutes}
minutes and is being closed. Below is a metadata-only summary of the
turns from this session — the full transcripts are NOT embedded here
(that ballooned the prompt 30-50× when sessions ran long). Each turn
line carries cost, tool-call count, output preview, and atom IDs cited
so you can score atoms and write memory without re-reading anything.

If you do need the full content of a specific turn (its tool sequence,
reasoning, or full output) for memory capture, call:

  mimir_get_turn(turn_id="<id>")

It returns the turn's `output` and `events` (no `input` — the prompt
that fed that turn isn't useful for synthesis and re-embedding it is
exactly the cost path we're avoiding). Be surgical — most turns won't
be worth re-reading.

Do three things, in order (plus optional Steps 1b and 1c — inline atom
storage and skill-specific learnings — see below):

### 1. Capture memories worth keeping

Skim the turn summaries below. If anything is worth remembering long-term
— facts about people in this channel, decisions, recurring patterns,
useful context for future sessions — write or edit files under:

  memory/channels/{channel_id}/   # channel-specific notes
  memory/issues/                  # operational gotchas (every-turn-INDEX surfacing)
  state/wiki/concepts/            # cross-channel patterns / frameworks
  state/wiki/topics/              # cross-channel long-form synthesis
  memory/learnings-pending.md     # candidate learned behaviors (see below)

If the session surfaced something that *might* be a durable behavior
worth remembering across all future turns — a heuristic that worked, a
failure mode worth avoiding, an approach that beat the default — append
it to `memory/learnings-pending.md` in the canonical 4-field shape
(`What I noticed / What works / Trigger / Source:`). The weekly
reflection turn reviews that buffer and *proposes* promoting durable
entries into `memory/core/40-learned-behaviors.md` (core memory is
read-only at runtime — the operator merges the change as a PR), and
drops one-offs. **Do NOT write directly to
`memory/core/40-learned-behaviors.md`** — it's blocked at runtime, and
synthesis turns have narrow context (one session) and have been observed
confabulating durable rules from one-off events. The pending buffer is
the safe path.

Use bash and the file-op tools. Call `mimir_get_turn` only for turns
whose summary suggests they're worth a closer look. Skip this step
entirely if nothing notable came up — no need to manufacture content.

### 1b. Store SAGA atoms for cross-session semantic facts

If the session surfaced concrete, positive, world-facts that benefit
from embedding-based cross-session retrieval (and aren't already going
into a file under Step 1), call saga_store for each.

**Good shapes:**

- **semantic** — facts, preferences, knowledge about people, places,
  things, concepts ("Alice prefers Slack DMs over email for urgent
  asks"; "Brander's actors thesis: LLM agents map to Hewitt's actor
  model"; "The Mariana Trench is the deepest known oceanic trench").
- **episodic** — dated events about specific entities ("Alice joined
  the Atlas project on 2025-03-12"; "The Hindenburg disaster occurred
  on 1937-05-06"). Dates verbatim where they appear.
- **procedural** — recurring how-tos / workflow patterns ("When
  summarizing a long document, lead with the thesis and supporting
  evidence"; "Use a hot pan and high heat for searing meat").

**Do NOT store:**

- Meta-observations about this turn or the runtime itself ("the
  synthesis prompt ran"; "the scheduler fired silently")
- Self-state claims ("I'm uncertain about X", "no info about Y")
- Negative / absence claims ("nothing happened today")
- Generic session-retell — the boundary's `summary` field handles
  that
- Duplicates of content already going into a file under Step 1, or
  already covered by a recent boundary's summary/topics/decisions

One fact per call. Single self-contained sentence. Dates and numbers
verbatim. If nothing fits, skip this step entirely — silence is fine.

### 1c. Record skill-specific learnings

If running a **skill** this session taught you something its *next* run
should know — a gotcha that cost you time, an input quirk, a performance
caveat, a tip, or an approach that worked — capture it with:

  saga_record_skill_learning(
    skill="<skill name>",      # e.g. "memory", "github-poller"
    kind="<kind>",             # failure-mode | input-quirk | perf-caveat
                               #   (cautionary) ·· tip | success-pattern (how-to)
    content="<one self-contained sentence>",
  )

This is *scoped* memory: the learning resurfaces automatically the next
time that skill loads and never leaks into unrelated turns — which is why
it goes here, not in Step 1b's general atoms. Record the cautionary ones
especially: a `failure-mode` you actually hit is the single most valuable
thing to leave for the next run. One learning per call. Skip entirely if
no skill taught you anything this session — don't manufacture entries.

### 2. Score SAGA atoms

The atom feedback section below lists every atom_id cited in this
session along with which turns cited it — that citation context is
usually enough to score without re-reading turns. When it isn't — you
need an atom's actual content to decide whether it was useful, wrong, or
stale — load the cited ids in ONE call with:

  memory_get(["<atom_id>", "<atom_id>", ...])   # batch by-id, exact load

Do NOT score atoms blind, and do NOT pass ids to memory_query or fan out
one lookup per id. For each atom, call:

  saga_feedback(atom_id, "useful")     # genuinely informed a reply
  saga_feedback(atom_id, "incorrect")  # was wrong or misleading
  saga_feedback(atom_id, "stale")      # outdated, should decay

Skip atoms that were neutral / not applicable — silence is a valid signal.

### 3. Record the session boundary

Synthesize and call:

  saga_end_session(
    session_id="{saga_session_id}",
    summary="<one-sentence summary>",
    topics_discussed=["..."],         # omit if nothing concrete
    decisions_made=["..."],           # omit if nothing concrete
    unfinished=["..."],               # omit if nothing was left dangling
    closed_since=["..."],             # see below
    emotional_state="<one phrase>",   # omit if neutral / unclear
  )

`closed_since` is the corrective-overrides list. Look at the "Recent
session summaries" block in your system prompt and check each prior
boundary's Unfinished items: did any get resolved during *this*
session? If so, list the specific identifiers — PR refs like `#71`,
chainlink IDs like `chainlink #29 G17`, file paths, etc. The prompt
builder substring-matches these against earlier Unfinished items and
drops any that contain one of these refs, so future prompts won't
keep showing them as live work.

If a prior Unfinished item like "PRs #71 + #72 awaiting" is partially
resolved (#71 merged but #72 still open), put the resolved ref in
`closed_since=["#71"]` AND re-list the still-open piece in this
boundary's `unfinished=["PR #72 still awaiting"]`. The older item
gets dropped via substring match; your new item carries the live
state forward.

Omit `closed_since` entirely if nothing from prior summaries was
resolved during this session.

After step 3, do not send any user-facing message — this is a bookkeeping turn.

## Turns in this session

{turn_summary_block}

## Atoms cited across the session

{atom_feedback_block}
"""


# chainlink #7: lean synthesis prompt for sessions where no SAGA atoms
# were created or touched. The full template above pays $1-3 per
# bookkeeping turn for the contribution-credit + atom-scoring
# scaffolding; when the session genuinely has zero atoms cited there's
# nothing to credit and the scaffolding is pure cost. The lean
# variant keeps memory capture (step 1), the optional inline atom
# storage step (step 1b — added 2026-05-10 per operator discussion on
# why mimir rarely reaches for saga_store), and the boundary record
# (step 2 — renumbered from step 3 in the full template) and drops:
#   - the dedicated atom-scoring step (Score SAGA atoms)
#   - the trailing ``## Atoms cited across the session`` block
# The session summary is still the valuable artifact (it feeds the
# Recent session summaries block in every subsequent prompt) — we
# just stop paying for the parts of synthesis that have no input.
SAGA_SESSION_END_LEAN_DEFAULT = """\
The SAGA session for channel {channel_id} has been idle for {idle_minutes}
minutes and is being closed. No SAGA atoms were created or cited
during this session, so the contribution-credit scoring is skipped —
this is a leaner bookkeeping turn focused on memory capture and the
session boundary record. Each turn line below carries cost,
tool-call count, and output preview.

If you do need the full content of a specific turn (its tool sequence,
reasoning, or full output) for memory capture, call:

  mimir_get_turn(turn_id="<id>")

It returns the turn's `output` and `events` (no `input` — the prompt
that fed that turn isn't useful for synthesis and re-embedding it is
exactly the cost path we're avoiding). Be surgical — most turns won't
be worth re-reading.

Do two things, in order (plus optional Steps 1b and 1c — inline atom
storage and skill-specific learnings — see below):

### 1. Capture memories worth keeping

Skim the turn summaries below. If anything is worth remembering long-term
— facts about people in this channel, decisions, recurring patterns,
useful context for future sessions — write or edit files under:

  memory/channels/{channel_id}/   # channel-specific notes
  memory/issues/                  # operational gotchas (every-turn-INDEX surfacing)
  state/wiki/concepts/            # cross-channel patterns / frameworks
  state/wiki/topics/              # cross-channel long-form synthesis
  memory/learnings-pending.md     # candidate learned behaviors (see below)

If the session surfaced something that *might* be a durable behavior
worth remembering across all future turns — a heuristic that worked, a
failure mode worth avoiding, an approach that beat the default — append
it to `memory/learnings-pending.md` in the canonical 4-field shape
(`What I noticed / What works / Trigger / Source:`). The weekly
reflection turn reviews that buffer and *proposes* promoting durable
entries into `memory/core/40-learned-behaviors.md` (core memory is
read-only at runtime — the operator merges the change as a PR), and
drops one-offs. **Do NOT write directly to
`memory/core/40-learned-behaviors.md`** — it's blocked at runtime, and
synthesis turns have narrow context (one session) and have been observed
confabulating durable rules from one-off events. The pending buffer is
the safe path.

Use bash and the file-op tools. Call `mimir_get_turn` only for turns
whose summary suggests they're worth a closer look. Skip this step
entirely if nothing notable came up — no need to manufacture content.

### 1b. Store SAGA atoms for cross-session semantic facts

If the session surfaced concrete, positive, world-facts that benefit
from embedding-based cross-session retrieval (and aren't already going
into a file under Step 1), call saga_store for each.

**Good shapes:**

- **semantic** — facts, preferences, knowledge about people, places,
  things, concepts ("Alice prefers Slack DMs over email for urgent
  asks"; "Brander's actors thesis: LLM agents map to Hewitt's actor
  model"; "The Mariana Trench is the deepest known oceanic trench").
- **episodic** — dated events about specific entities ("Alice joined
  the Atlas project on 2025-03-12"; "The Hindenburg disaster occurred
  on 1937-05-06"). Dates verbatim where they appear.
- **procedural** — recurring how-tos / workflow patterns ("When
  summarizing a long document, lead with the thesis and supporting
  evidence"; "Use a hot pan and high heat for searing meat").

**Do NOT store:**

- Meta-observations about this turn or the runtime itself ("the
  synthesis prompt ran"; "the scheduler fired silently")
- Self-state claims ("I'm uncertain about X", "no info about Y")
- Negative / absence claims ("nothing happened today")
- Generic session-retell — the boundary's `summary` field handles
  that
- Duplicates of content already going into a file under Step 1, or
  already covered by a recent boundary's summary/topics/decisions

One fact per call. Single self-contained sentence. Dates and numbers
verbatim. If nothing fits, skip this step entirely — silence is fine.

### 1c. Record skill-specific learnings

If running a **skill** this session taught you something its *next* run
should know — a gotcha that cost you time, an input quirk, a performance
caveat, a tip, or an approach that worked — capture it with:

  saga_record_skill_learning(
    skill="<skill name>",      # e.g. "memory", "github-poller"
    kind="<kind>",             # failure-mode | input-quirk | perf-caveat
                               #   (cautionary) ·· tip | success-pattern (how-to)
    content="<one self-contained sentence>",
  )

This is *scoped* memory: the learning resurfaces automatically the next
time that skill loads and never leaks into unrelated turns — which is why
it goes here, not in Step 1b's general atoms. Record the cautionary ones
especially: a `failure-mode` you actually hit is the single most valuable
thing to leave for the next run. One learning per call. Skip entirely if
no skill taught you anything this session — don't manufacture entries.

### 2. Record the session boundary

Synthesize and call:

  saga_end_session(
    session_id="{saga_session_id}",
    summary="<one-sentence summary>",
    topics_discussed=["..."],         # omit if nothing concrete
    decisions_made=["..."],           # omit if nothing concrete
    unfinished=["..."],               # omit if nothing was left dangling
    closed_since=["..."],             # see below
    emotional_state="<one phrase>",   # omit if neutral / unclear
  )

`closed_since` is the corrective-overrides list. Look at the "Recent
session summaries" block in your system prompt and check each prior
boundary's Unfinished items: did any get resolved during *this*
session? If so, list the specific identifiers — PR refs like `#71`,
chainlink IDs like `chainlink #29 G17`, file paths, etc. The prompt
builder substring-matches these against earlier Unfinished items and
drops any that contain one of these refs, so future prompts won't
keep showing them as live work.

If a prior Unfinished item like "PRs #71 + #72 awaiting" is partially
resolved (#71 merged but #72 still open), put the resolved ref in
`closed_since=["#71"]` AND re-list the still-open piece in this
boundary's `unfinished=["PR #72 still awaiting"]`. The older item
gets dropped via substring match; your new item carries the live
state forward.

Omit `closed_since` entirely if nothing from prior summaries was
resolved during this session.

After step 2, do not send any user-facing message — this is a bookkeeping turn.

## Turns in this session

{turn_summary_block}
"""


def load_template(name: str, default: str, prompts_dir: Path | None) -> str:
    """Read ``<prompts_dir>/<name>.md`` if set and present, else return default."""
    if prompts_dir is None:
        return default
    candidate = prompts_dir / f"{name}.md"
    if candidate.is_file():
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("failed to read template %s: %s — falling back to default", candidate, exc)
    return default


# Output preview cap — characters, not tokens. Synthesis only needs a
# hint at what the turn was about; details get fetched via
# ``mimir_get_turn`` if step 1 (memory capture) wants to look closer.
_OUTPUT_PREVIEW_CHARS = 200


def _output_preview(output: str) -> str:
    """Single-line preview of a turn's output, ≤ _OUTPUT_PREVIEW_CHARS.

    Strips newlines (so the preview fits one line in the prompt), collapses
    runs of whitespace, and truncates with an ellipsis suffix that names
    the original char count (so the agent can decide if it's worth fetching).
    """
    if not output:
        return "(empty)"
    flat = " ".join(output.split())
    if len(flat) <= _OUTPUT_PREVIEW_CHARS:
        return flat
    return f"{flat[:_OUTPUT_PREVIEW_CHARS]}… ({len(output)} chars total)"


def _turn_summary_lines(turns_window: list[dict]) -> str:
    """Render one summary line per turn — the metadata-only replacement
    for the JSON-dumped transcript.

    Each line names: turn_id, trigger, cost (if known), tool-call count,
    output preview, and the atoms this turn cited. The agent uses this
    to decide whether to fetch the full turn via ``mimir_get_turn``.
    """
    if not turns_window:
        return "(no turns recorded for this session)"
    lines: list[str] = []
    for t in turns_window:
        turn_id = t.get("turn_id") or "?"
        trigger = t.get("trigger") or "?"
        cost = t.get("total_cost_usd")
        cost_part = f"${cost:.3f}" if isinstance(cost, (int, float)) else "$?"
        events = t.get("events") or []
        # Tool-call count: each tool_call event in the captured stream
        # (turn_logger tags them with type=="tool_call"). Older fixtures
        # without per-event type tagging fall back to the raw count.
        tool_calls = sum(
            1 for e in events if isinstance(e, dict) and e.get("type") == "tool_call"
        )
        if tool_calls == 0 and events and not any(
            isinstance(e, dict) and "type" in e for e in events
        ):
            tool_calls = len(events)
        atoms = t.get("saga_atom_ids") or []
        atoms_part = (
            f"atoms: {', '.join(atoms)}" if atoms else "atoms: (none)"
        )
        preview = _output_preview(t.get("output") or "")
        line = (
            f"- turn {turn_id} ({trigger}, {cost_part}, {tool_calls} tool calls, {atoms_part})\n"
            f"    output: {preview}"
        )
        # chainlink #376: surface mid-turn messages folded into this turn so
        # session-end synthesis + commitment extraction see the user's
        # follow-ups — not just the original prompt (the model saw them, but
        # this summary is the synthesis-visible projection). Entries are
        # ``{t_ms, text}`` (PR 4); tolerate bare strings from PR-3-era records.
        injected = t.get("injected_inputs") or []
        if injected:
            texts = [m.get("text", "") if isinstance(m, dict) else m for m in injected]
            previews = " | ".join(_output_preview(x) for x in texts)
            line += f"\n    injected mid-turn ({len(injected)}): {previews}"
        lines.append(line)
    return "\n".join(lines)


def _atom_feedback_lines(turns_window: list[dict]) -> str:
    """Render the atom→[turn_ids] map as one line per atom.

    Atoms cited in multiple turns appear once with all citing turn_ids
    listed. Order: by atom_id within first-citing-turn order, so the
    block's stable across re-renders of the same session.
    """
    if not turns_window:
        return "(no atoms cited in this session)"
    # Preserve insertion order: dict is ordered in Py3.7+.
    citations: dict[str, list[str]] = {}
    for t in turns_window:
        turn_id = t.get("turn_id") or "?"
        for atom_id in t.get("saga_atom_ids") or []:
            citations.setdefault(atom_id, []).append(turn_id)
    if not citations:
        return "(no atoms cited in this session)"
    lines = [
        f"- {atom_id}: cited in turn(s) {', '.join(turn_ids)}"
        for atom_id, turn_ids in citations.items()
    ]
    return "\n".join(lines)


def _session_has_atoms(turns_window: list[dict]) -> bool:
    """True iff at least one turn in the session cited a SAGA atom.
    Drives the lean-vs-full synthesis-prompt selection (chainlink #7):
    when False, the synthesis turn doesn't need the contribution-credit
    scaffolding because there's nothing to credit.

    Note: ``saga_atom_ids`` is the union of pre-injected (S3 retrieval
    via the pre-message hook) and mid-turn-queried (saga_query) atoms.
    Storage-only turns — turns that called ``saga_store`` to create
    new atoms but never queried/cited any — register as zero atoms
    here, so the lean path fires for them. This is intentional:
    the contribution-credit scoring is about marking RETRIEVALS as
    useful/incorrect/stale (atom utility for future surfacing); a turn
    that only stored has nothing to score, so the lean prompt is
    correct. New atoms are the agent's own writes — saga's own
    pipelines (consolidation, decay) handle their lifecycle, not
    the synthesis pass."""
    for t in turns_window:
        atoms = t.get("saga_atom_ids") or []
        if atoms:
            return True
    return False


def render_saga_session_end(
    *,
    channel_id: str,
    saga_session_id: str,
    idle_minutes: int,
    turns_window: list[dict],
    prompts_dir: Path | None,
) -> str:
    """Render the synthesis-turn prompt. When the session cited zero
    SAGA atoms across all turns, the lean variant is used — drops the
    atom-scoring step + the trailing atoms-cited block to save the
    bookkeeping cost on no-atom sessions (chainlink #7).

    The selection is content-driven, not config-driven: an operator
    override at ``<prompts_dir>/saga_session_end.md`` always wins
    (full template), and ``<prompts_dir>/saga_session_end_lean.md``
    overrides the lean default. Both share the same set of placeholders
    EXCEPT the lean variant has no ``{atom_feedback_block}`` slot —
    rendering passes only the placeholders the active template actually
    uses, so an operator's full-template override still works after
    this change."""
    if _session_has_atoms(turns_window):
        template = load_template(
            "saga_session_end", SAGA_SESSION_END_DEFAULT, prompts_dir,
        )
        return template.format(
            channel_id=channel_id,
            saga_session_id=saga_session_id,
            idle_minutes=idle_minutes,
            turn_summary_block=_turn_summary_lines(turns_window),
            atom_feedback_block=_atom_feedback_lines(turns_window),
        )
    template = load_template(
        "saga_session_end_lean", SAGA_SESSION_END_LEAN_DEFAULT, prompts_dir,
    )
    return template.format(
        channel_id=channel_id,
        saga_session_id=saga_session_id,
        idle_minutes=idle_minutes,
        turn_summary_block=_turn_summary_lines(turns_window),
    )
