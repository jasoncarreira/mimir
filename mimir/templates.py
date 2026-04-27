"""Prompt templates loaded from disk with bundled defaults.

The spec lists templates under ``mimir/prompts/`` but that path collides with
``mimir/prompts.py`` (assembly module) under Python's import system, so the
bundled defaults live here as constants. ``MIMIR_PROMPTS_DIR`` (SPEC §14)
still overrides per-deployment — looked up by template name (e.g.
``<dir>/msam_session_end.md``) and falls back to the default below.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


MSAM_SESSION_END_DEFAULT = """\
The MSAM session for channel {channel_id} has been idle for {idle_minutes}
minutes and is being closed. Below are the turns from this session, filtered
by msam_session_id. Each turn record carries `msam_atom_ids` — the atoms
MSAM injected pre-message plus any you queried mid-turn.

Do three things, in order:

### 1. Capture memories worth keeping

Review the turns. If anything is worth remembering long-term — facts about
people in this channel, decisions, recurring patterns, useful context for
future sessions — write or edit files under:

  memory/channels/{channel_id}/   # channel-specific notes
  memory/shared/                  # cross-channel facts

Use bash and the file-op tools. Skip this step entirely if nothing notable
came up — no need to manufacture content.

### 2. Score MSAM atoms

For each atom_id in the union of `msam_atom_ids` across the turns below,
decide whether it actually helped:

  msam_feedback(atom_id, "useful")     # genuinely informed a reply
  msam_feedback(atom_id, "incorrect")  # was wrong or misleading
  msam_feedback(atom_id, "stale")      # outdated, should decay

Skip atoms that were neutral / not applicable — silence is a valid signal.

### 3. Record the session boundary

Synthesize and call:

  msam_end_session(
    session_id="{msam_session_id}",
    summary="<one-sentence summary>",
    topics_discussed=["..."],         # omit if nothing concrete
    decisions_made=["..."],           # omit if nothing concrete
    unfinished=["..."],               # omit if nothing was left dangling
    emotional_state="<one phrase>",   # omit if neutral / unclear
  )

After step 3, do not send any user-facing message — this is a bookkeeping turn.

## Turns from this session

{turns_window_jsonl}
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


def render_msam_session_end(
    *,
    channel_id: str,
    msam_session_id: str,
    idle_minutes: int,
    turns_window: list[dict],
    prompts_dir: Path | None,
) -> str:
    template = load_template("msam_session_end", MSAM_SESSION_END_DEFAULT, prompts_dir)
    import json as _json

    serialized = "\n".join(
        _json.dumps(t, ensure_ascii=False, default=str) for t in turns_window
    )
    return template.format(
        channel_id=channel_id,
        msam_session_id=msam_session_id,
        idle_minutes=idle_minutes,
        turns_window_jsonl=serialized or "(no turns recorded for this session)",
    )
