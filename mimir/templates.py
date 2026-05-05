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

Do three things, in order:

### 1. Capture memories worth keeping

Skim the turn summaries below. If anything is worth remembering long-term
— facts about people in this channel, decisions, recurring patterns,
useful context for future sessions — write or edit files under:

  memory/channels/{channel_id}/   # channel-specific notes
  memory/shared/                  # cross-channel facts

Use bash and the file-op tools. Call `mimir_get_turn` only for turns
whose summary suggests they're worth a closer look. Skip this step
entirely if nothing notable came up — no need to manufacture content.

### 2. Score SAGA atoms

The atom feedback section below lists every atom_id cited in this
session along with which turns cited it — that citation context is
enough to score without re-reading turns. For each atom, call:

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
    emotional_state="<one phrase>",   # omit if neutral / unclear
  )

After step 3, do not send any user-facing message — this is a bookkeeping turn.

## Turns in this session

{turn_summary_block}

## Atoms cited across the session

{atom_feedback_block}
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
        lines.append(
            f"- turn {turn_id} ({trigger}, {cost_part}, {tool_calls} tool calls, {atoms_part})\n"
            f"    output: {preview}"
        )
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


def render_saga_session_end(
    *,
    channel_id: str,
    saga_session_id: str,
    idle_minutes: int,
    turns_window: list[dict],
    prompts_dir: Path | None,
) -> str:
    template = load_template("saga_session_end", SAGA_SESSION_END_DEFAULT, prompts_dir)
    return template.format(
        channel_id=channel_id,
        saga_session_id=saga_session_id,
        idle_minutes=idle_minutes,
        turn_summary_block=_turn_summary_lines(turns_window),
        atom_feedback_block=_atom_feedback_lines(turns_window),
    )
