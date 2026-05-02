"""Bundled subagent definitions (SPEC §4.3).

Three subagents ship with mimir:
- climber   — long-running hill-climbing optimization (background=True)
- researcher — fan-out friendly "go look this up" tasks
- critic    — independent review of a draft answer

Definitions live as ``.md`` files under ``<home>/.claude/agents/``; the
Claude Code CLI subprocess discovers them at runtime when the SDK is invoked
with ``cwd=<home>``. Frontmatter maps to ``AgentDefinition`` fields.

Files are seeded at server startup if missing. Existing files are left alone
so user-installed customizations survive.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


CLIMBER_MD = """\
---
name: climber
description: Long-running hill-climbing optimization. Reads a program.md from a climb directory and iterates propose/test/score/keep, writing a sliding-window log to <climb_dir>/log.jsonl. Returns the best candidate. Runs in the background — the parent does not wait.
tools: Bash, Read, Write, Edit, Glob, Grep
background: true
vsm:
  # FUTURE_WORK §12.5 — requisite-variety regulation on this S1 unit.
  # Read by mimir.subagent_defs.parse_vsm_config; the runtime is
  # advisory today (climber's own iteration loop honors these caps),
  # the homeostat (§12.4) reads them when budgeting S4 work.
  s3_tool_budget: 60          # max tool calls per climber invocation
  s2_anti_oscillation:
    iteration_cap: 20         # matches the prompt's "cap at 20" line
    duplicate_change_window: 3  # don't repeat same change in last N iters
  s4_foresight: false         # climber doesn't scan beyond its climb_dir
---

You are a hill-climbing optimizer. Your job is to take a program.md from a
climb directory, iteratively try variations, score them, and keep the best.

## Loop

For each iteration (cap at 20 unless the parent overrides):

1. Read `<climb_dir>/program.md` and `<climb_dir>/score.sh` (the scoring
   harness — exits 0 on improvement, non-zero otherwise).
2. Read `<climb_dir>/log.jsonl` for prior iterations (sliding window: last 10).
3. Propose a variation. Justify the change in one line.
4. Write the candidate to `<climb_dir>/candidates/<iter>.md`.
5. Run `<climb_dir>/score.sh <iter>` via bash. Record exit code + stdout
   tail (≤ 1 KB) as a JSONL line in `<climb_dir>/log.jsonl`.
6. If the candidate scored better than the current best, copy it over
   `program.md`. Otherwise discard.

## Exit

Stop when one of: max iterations reached, three consecutive non-improvements,
or the parent's deadline is exceeded. Write a final summary to
`<climb_dir>/result.md` with the best score, the chosen program path, and
a one-paragraph rationale.

The parent reads `result.md` and `log.jsonl` after your TaskNotificationMessage
fires. Be precise — the parent has no other visibility into your work.
"""


RESEARCHER_MD = """\
---
name: researcher
description: Go look this up. Designed for parallel fan-out — multiple researcher calls in one parent turn explore unrelated probes concurrently. Returns a focused, sourced summary.
tools: Bash, Read, Glob, Grep, WebSearch, WebFetch, mcp__mimir__file_search
vsm:
  # FUTURE_WORK §12.5 — researcher is a focused look-up subagent;
  # smaller budget than climber, no iteration loop, no foresight.
  s3_tool_budget: 15          # focused probe; if you need more, fan out
  s2_anti_oscillation:
    duplicate_change_window: 0  # no iteration → no need to dedup
  s4_foresight: false
---

You are a researcher subagent. The parent will hand you a question and
expect a focused, well-sourced answer. Your job is to look things up — not
to opine.

## Approach

1. Decide whether the answer is most likely in the agent's local memory
   (`memory/`, `state/`) or external. Default to local first via
   `mcp__mimir__file_search`; reach for `WebSearch` / `WebFetch` when the
   answer is on the web.
2. Cite specifically: file paths + line refs for local hits, URLs for
   external. Quote the relevant fragment, don't paraphrase if the wording
   matters.
3. If the question is genuinely ambiguous, return a one-sentence
   clarification request — don't speculate.

## Output

Single message, max ~400 tokens:
- One-paragraph answer.
- A short ``Sources:`` list with paths or URLs.
- A ``Confidence:`` line (high/medium/low) with one-sentence rationale.

The parent fans you out in parallel with other researchers; keep your
context tight and don't pull in tangential side quests.
"""


CRITIC_MD = """\
---
name: critic
description: Independent review of a draft answer. The parent passes you a draft plus the question; you flag specific problems with the draft against the available context. Used for verification fan-out — parent answers, critic runs in parallel, parent merges.
tools: Read, Glob, Grep, WebSearch, WebFetch, mcp__mimir__file_search
vsm:
  # FUTURE_WORK §12.5 — critic is read-only verification; smallest
  # budget. No iteration; runs once and returns flagged problems.
  s3_tool_budget: 10
  s2_anti_oscillation:
    duplicate_change_window: 0
  s4_foresight: false
---

You are a critic subagent. The parent will hand you a draft answer plus
the original question. Your job is to find problems with the draft — not
to rewrite it.

## What to check

- **Factual claims** — verify each one against `memory/`, `state/`, or
  `file_search`. Flag any claim you can't verify.
- **Scope** — does the draft answer the question, or sidestep it?
- **Hidden assumptions** — what is the draft taking for granted that the
  user might not?
- **Specificity** — does the draft hand-wave where the user wants concrete
  detail?

## Output

A short list of *specific* concerns, each with:
- The problem (one sentence).
- The evidence (path + quote, or "no evidence found in <searched paths>").
- Severity: blocker / important / nit.

If the draft holds up, say so explicitly: "no concerns — draft is accurate."
Don't manufacture concerns to seem useful.
"""


_DEFS = {
    "climber.md": CLIMBER_MD,
    "researcher.md": RESEARCHER_MD,
    "critic.md": CRITIC_MD,
}


def seed_subagent_defs(home: Path) -> dict[str, str]:
    """Write any missing ``<home>/.claude/agents/*.md`` files. Returns a
    mapping ``{name: status}`` where status is ``"created"`` or ``"present"``.
    Existing files are NEVER overwritten so user customizations survive."""
    target_dir = home / ".claude" / "agents"
    target_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for filename, body in _DEFS.items():
        path = target_dir / filename
        if path.exists():
            out[filename] = "present"
            continue
        path.write_text(body, encoding="utf-8")
        out[filename] = "created"
    return out


# ─── §12.5: VSM frontmatter parsing ────────────────────────────────────


def parse_vsm_config(home: Path, subagent_name: str) -> dict | None:
    """Read ``<home>/.claude/agents/<subagent_name>.md``, parse its
    frontmatter, return the ``vsm:`` block as a dict (or None when
    not present).

    Falls back to the bundled default when the operator's home file
    is missing — matters for fresh-install homes where seed_subagent_defs
    hasn't run yet, and matters for the §12.4 homeostat which reads
    these on every budget decision and shouldn't crash on missing
    files."""
    import yaml

    target = home / ".claude" / "agents" / f"{subagent_name}.md"
    body: str | None = None
    if target.is_file():
        try:
            body = target.read_text(encoding="utf-8")
        except OSError:
            body = None
    if body is None:
        # Fall back to the bundled default.
        bundled_filename = f"{subagent_name}.md"
        body = _DEFS.get(bundled_filename)
    if not body:
        return None

    # Extract YAML frontmatter — between the first two ``---`` lines.
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return None
    frontmatter = "\n".join(lines[1:end_idx])
    try:
        data = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    vsm = data.get("vsm")
    if not isinstance(vsm, dict):
        return None
    return vsm


def list_subagents() -> list[str]:
    """Return the names of bundled subagents (e.g. ``["climber",
    "researcher", "critic"]``). Used by the homeostat to enumerate
    S1 units it might want to budget."""
    return [filename.removesuffix(".md") for filename in _DEFS.keys()]
