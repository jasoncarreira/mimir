"""Skill outcome tracking + amplification (FUTURE_WORK §12.3).

Per-skill success / failure rates aggregated from turns.jsonl tool-call
events. Skills that consistently land get prompt-prominence; skills
that consistently fail get a quiet ⚠ marker; skills that have never
been tried sit in their own bucket so they don't crowd the proven ones.

Same shape as saga's ``mark_contributions`` but at the skill layer —
the second real amplification (positive feedback) loop in mimir.

Two signals get folded into the per-skill counters:

1. **Claude Code runtime** (Letta-era / claude-agent-sdk): each
   AssistantMessage with a ``Skill`` tool_use block emits a
   ``tool_call`` event with ``name == "Skill"`` and
   ``args.skill == "<skill-name>"``. The matching ``tool_result``
   carries ``is_error``. We pair them by ``id``.

2. **deepagents runtime** (mimir today): there is no ``Skill`` tool —
   ``SkillsMiddleware`` injects prompt instructions telling the model
   to load a skill by calling ``read_file`` on its ``SKILL.md``. We
   recognise ``read_file`` calls whose ``file_path`` matches
   ``.claude/skills/<name>/SKILL.md`` (any prefix tolerated:
   ``/mimir-home/...``, ``./...``, bare relative) and treat them as
   skill loads. Even when ``SkillsMiddleware`` itself isn't wired
   (mimir's current state — see ``_assemble_skill_block`` for the
   in-house prompt-block equivalent), the agent still uses this same
   ``read_file`` pathway when invoking a skill from a prompt that
   says "load the `<name>` skill", so this signal works regardless.

Outcome classification:
  - **success**   — tool_result with is_error=False, no in-turn retry
  - **failure**   — tool_result with is_error=True
  - **abandoned** — tool_call with no matching tool_result in the
    same turn (rare; usually means the agent pivoted before the SDK
    routed back). Treated as failure for ranking purposes but
    counted separately for diagnostics.

Operator override via ``state/skill-pin.yaml``:
  ```yaml
  pin_top: [memory, wiki]
  hide: [legacy-thing]
  ```
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ``.claude/skills/<name>/SKILL.md`` with any prefix
# (``/mimir-home/...``, ``./...``, bare relative). The capture group
# is the skill name — exactly one path segment, no nested categories.
_SKILL_READ_RE = re.compile(
    r"(?:^|/)\.claude/skills/([^/]+)/SKILL\.md$",
)
from typing import Iterable

import yaml


# VSM: S3 — skill amplification. Per-skill success-rate aggregator
#          shapes prompt prominence so the agent sees high-value
#          skills first and avoids ones that consistently fail.
# loop_id: 12.3
@dataclass
class SkillOutcome:
    skill: str
    success: int = 0
    failure: int = 0
    abandoned: int = 0
    last_used: datetime | None = None

    @property
    def total(self) -> int:
        return self.success + self.failure + self.abandoned

    @property
    def success_rate(self) -> float | None:
        """Rate of successful invocations over all completed-or-abandoned
        ones. Abandoned counts as failure (matches the module docstring:
        "treated as failure for ranking purposes") since an unmatched
        tool_call usually means the agent gave up mid-skill — not a
        positive signal. None when no usable data."""
        denom = self.success + self.failure + self.abandoned
        if denom == 0:
            return None
        return self.success / denom


@dataclass
class SkillPinConfig:
    """state/skill-pin.yaml shape."""
    pin_top: list[str] = field(default_factory=list)
    hide: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "SkillPinConfig":
        if not path.is_file():
            return cls()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            pin_top=list(data.get("pin_top") or []),
            hide=list(data.get("hide") or []),
        )


def _iter_turns(path: Path) -> Iterable[dict]:
    """Yield turn records from ``turns.jsonl``. Best effort — skips
    malformed lines."""
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _classify_skill_calls(
    events: list[dict], turn_ts: datetime,
    *,
    turn_succeeded: bool | None = None,
) -> Iterable[tuple[str, str, datetime]]:
    """Walk a single turn's events list, pair Skill tool_call ↔
    tool_result by id, yield (skill_name, outcome, ts) tuples.

    Outcome ∈ {"success", "failure", "abandoned"}.

    ``turn_succeeded`` is the overall turn success signal (False when
    ``result_is_error`` is True). When a Skill call has no matching
    tool_result — which happens systematically on the ChatClaudeCode /
    deepagents streaming path because ToolResultBlocks for built-in
    Claude Code tools (Skill, Bash, Read …) are never surfaced in the
    Python-layer events list — we fall back to ``turn_succeeded``
    instead of always emitting "abandoned". Without this fallback,
    every Skill invocation on a heartbeat/reflection/poller turn
    lands in the risky bucket even when the turn completed cleanly.

    Resolution:
      - Matching tool_result present → use is_error (exact)
      - No tool_result + turn_succeeded=True  → "success" (inferred)
      - No tool_result + turn_succeeded=False → "failure" (inferred)
      - No tool_result + turn_succeeded=None  → "abandoned" (legacy /
        turn outcome unavailable)

    **Inference imprecision (aggregate-level correct, per-call-level
    imprecise).** When a turn has both a Skill call AND a later
    non-Skill tool call (Bash, Read, …), this fallback can't
    distinguish which tool drove the turn-level error. If the Skill
    succeeded but a later Bash crashed the turn, the Skill gets
    attributed "failure" via the fallback. Inverse case (Skill itself
    drove the failure) is correctly attributed. Heartbeat /
    reflection / poller turns are usually Skill-dominated so the
    bucket telemetry trends right in aggregate; rare turns with
    mixed tool-call patterns may misattribute.

    **Backfill vs forward-looking.** Once the
    ``enrich_streaming_metadata`` patch (PR #193) is live for new
    turns, ChatClaudeCode emits matching tool_result events for
    built-in tools and the exact-match branch above takes over. The
    inferred-from-turn fallback's value then shifts from "working
    around current breakage" to "working around historical
    breakage" — old turns in turns.jsonl from before #193 landed
    keep flowing through this path indefinitely.
    """
    pending: dict[str, str] = {}   # tool_use_id → skill name
    for ev in events:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "tool_call":
            name = ev.get("name")
            args = ev.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            skill: str | None = None
            if name == "Skill":
                # Claude Code runtime: structured Skill tool invocation.
                raw_skill = args.get("skill")
                if raw_skill:
                    skill = str(raw_skill)
            elif name == "read_file":
                # deepagents runtime: SKILL.md read = skill load.
                file_path = args.get("file_path") or ""
                if isinstance(file_path, str):
                    m = _SKILL_READ_RE.search(file_path)
                    if m:
                        skill = m.group(1)
            if skill:
                tool_id = ev.get("id")
                if isinstance(tool_id, str):
                    pending[tool_id] = skill
        elif etype == "tool_result":
            tool_id = ev.get("id")
            if isinstance(tool_id, str) and tool_id in pending:
                skill = pending.pop(tool_id)
                is_error = bool(ev.get("is_error"))
                yield skill, ("failure" if is_error else "success"), turn_ts

    # Anything left in pending has no matching tool_result.
    # In ChatClaudeCode turns the streaming path never surfaces
    # tool_result events for built-in tools, so ALL Skill calls
    # land here. Use the turn-level signal as a fallback.
    for skill in pending.values():
        if turn_succeeded is True:
            yield skill, "success", turn_ts
        elif turn_succeeded is False:
            yield skill, "failure", turn_ts
        else:
            yield skill, "abandoned", turn_ts


def aggregate(
    turns_log: Path, *,
    window_hours: int = 24 * 7,   # 7d default
    now: datetime | None = None,
) -> dict[str, SkillOutcome]:
    """Walk turns.jsonl, accumulate per-skill outcome counts within
    the window."""
    now = now or datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    out: dict[str, SkillOutcome] = defaultdict(lambda: SkillOutcome(skill="?"))

    for record in _iter_turns(turns_log):
        ts_raw = record.get("ts")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < cutoff:
            continue
        events = record.get("events") or []
        if not isinstance(events, list):
            continue
        # Derive turn-level success signal for the ChatClaudeCode fallback
        # (see _classify_skill_calls docstring). result_is_error=False means
        # the turn completed cleanly; None means the field is absent (older
        # records or error before model response — treat as unknown).
        raw_is_error = record.get("result_is_error")
        turn_succeeded: bool | None = (
            None if raw_is_error is None else not bool(raw_is_error)
        )
        for skill, outcome, _ in _classify_skill_calls(
            events, ts, turn_succeeded=turn_succeeded
        ):
            entry = out[skill]
            entry.skill = skill
            if outcome == "success":
                entry.success += 1
            elif outcome == "failure":
                entry.failure += 1
            else:
                entry.abandoned += 1
            if entry.last_used is None or ts > entry.last_used:
                entry.last_used = ts

    return dict(out)


def order_skills(
    seeded: list[str],
    aggregates: dict[str, SkillOutcome],
    pin: SkillPinConfig,
    *,
    now: datetime | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return three buckets in render order:

      1. **Proven** — used in window with success_rate ≥ 0.5.
         Pinned-top items get added first within this bucket;
         everything else sorts by (success_rate desc, last_used desc).
      2. **Untried** — seeded but no tool calls in window. Alphabetic.
      3. **Risky** — used but success_rate < 0.5. Alphabetic.

    ``hide``-listed skills are removed from all buckets.
    """
    hidden = set(pin.hide or [])
    proven: list[tuple[str, float, datetime | None]] = []
    untried: list[str] = []
    risky: list[str] = []

    for name in seeded:
        if name in hidden:
            continue
        agg = aggregates.get(name)
        # "Untried" means no completed/failed/abandoned invocations —
        # i.e. nothing we can rank against. Skill outcomes that exist
        # but failed are "risky"; skills missing from aggregates are
        # untried.
        if agg is None or agg.total == 0:
            untried.append(name)
            continue
        rate = agg.success_rate or 0.0
        if rate >= 0.5:
            proven.append((name, rate, agg.last_used))
        else:
            risky.append(name)

    pinned_first = [n for n in (pin.pin_top or []) if n in {p[0] for p in proven}]
    rest = sorted(
        [p for p in proven if p[0] not in set(pinned_first)],
        key=lambda x: (-x[1], -(x[2].timestamp() if x[2] else 0)),
    )
    proven_names = pinned_first + [r[0] for r in rest]

    return proven_names, sorted(untried), sorted(risky)


# Hard cap on the per-skill description we render into the system
# prompt's `## Skills` block. Triggers/descriptions in SKILL.md
# frontmatter are often 100-300 chars — that's fine for the catalog
# page, but in the every-turn system prompt we want one terminal-line
# of signal per skill, not three. Truncate with an ellipsis past this
# many chars. 120 is wide enough that "Use when X (a, b, c, ...)" -
# style descriptions retain their disambiguating examples; the earlier
# 80 cap routinely cut mid-list (flagged 2026-05-17 after PR #186
# rendered the block in the live prompt for the first time).
_SKILL_DESC_MAX_CHARS = 120


def _truncate_desc(text: str, limit: int = _SKILL_DESC_MAX_CHARS) -> str:
    """One-line truncate. Strips newlines, collapses inner whitespace,
    trims trailing punctuation, appends an ellipsis if the source
    exceeded ``limit``."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned.rstrip(".")
    # Reserve one char for the ellipsis. Try to break at a word
    # boundary inside the limit; fall back to a hard cut if no space.
    cut = cleaned[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(",;:- ") + "…"


def render_skill_catalog(
    seeded: list[str],
    pin: SkillPinConfig,
    *,
    descriptions: dict[str, str] | None = None,
) -> str | None:
    """Render the install-stable `## Skills` section for the system
    prompt — alphabetical list of every seeded skill name, with
    ``pin.hide``-listed skills filtered out. Pin order is NOT applied
    here (pinning is a per-turn ranking concern, not a catalog
    concern); rendered as a plain alphabetic list so the system prompt
    stays cacheable across turns.

    If ``descriptions`` is provided, each line is rendered as
    ``- <name> — <one-line description>`` (truncated to
    ``_SKILL_DESC_MAX_CHARS``); skills missing from the map render as
    bare ``- <name>``. SKILL.md frontmatter is install-stable, so
    descriptions don't bust the prompt cache.

    Volatile bucket assignment + ``(N/M in window)`` counts live in
    ``render_skill_telemetry`` (rendered into the per-turn
    ``## Self-state`` block) — keeping them out of the system prompt
    means a skill invocation no longer perturbs the prompt-cache prefix.

    Returns None when no skills survive the hide filter (don't print an
    empty header)."""
    hidden = set(pin.hide or [])
    visible = sorted(name for name in seeded if name not in hidden)
    if not visible:
        return None
    descs = descriptions or {}
    lines: list[str] = []
    for name in visible:
        desc = descs.get(name, "").strip()
        if desc:
            lines.append(f"- {name} — {_truncate_desc(desc)}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)


def render_skill_telemetry(
    seeded: list[str],
    aggregates: dict[str, SkillOutcome],
    pin: SkillPinConfig,
    *,
    now: datetime | None = None,
) -> str | None:
    """Render per-turn-variable skill bucket telemetry for the
    ``## Self-state`` block. Emits Proven (success ≥ 50%) and Risky
    (success < 50%) buckets with ``(N/M in window)`` counts.

    Untried skills (no completed invocations in window) are *not*
    enumerated here — the install-stable catalog in the system prompt
    already lists them, and re-listing them here would just bloat the
    block with names that have no telemetry to share. The model can
    infer "any skill in the catalog not mentioned in telemetry has no
    recent activity."

    Returns None when no skill has any in-window activity (don't print
    an empty header). The returned body has no leading ``## Self-state``
    header — caller composes it into a larger block."""
    proven, _untried, risky = order_skills(seeded, aggregates, pin, now=now)
    if not (proven or risky):
        return None

    lines: list[str] = []
    if proven:
        lines.append("- skills proven (recent success):")
        for name in proven:
            agg = aggregates[name]
            lines.append(f"  - {name} ({agg.success}/{agg.total} in window)")
    if risky:
        lines.append("- skills risky ⚠ (recent failures > successes):")
        for name in risky:
            agg = aggregates[name]
            lines.append(f"  - {name} ({agg.success}/{agg.total} in window)")
    return "\n".join(lines)
