"""Skill outcome tracking + amplification (FUTURE_WORK §12.3).

Per-skill success / failure rates aggregated from turns.jsonl tool-call
events. Skills that consistently land get prompt-prominence; skills
that consistently fail get a quiet ⚠ marker; skills that have never
been tried sit in their own bucket so they don't crowd the proven ones.

Same shape as saga's ``mark_contributions`` but at the skill layer —
the second real amplification (positive feedback) loop in mimir.

Two signals get folded into the per-skill counters, matching the
two invocation patterns on the deepagents runtime:

1. **Subagent execution** (delegatable skills): ``Skill`` declares
   ``allowed-tools`` in frontmatter → ``mimir.subagent_compiler``
   produces a SubAgent spec → ``create_deep_agent(subagents=...)``
   registers it → agent invokes via the framework's ``task`` tool.
   Signal: ``tool_call(name="task", args.subagent_type="<skill>")``.
   ``tool_result.is_error`` directly reflects whether the
   subagent's workflow succeeded — **execution** signal, clean.

2. **Inline load** (reflective / no-allowed-tools skills): skill
   stays in the catalog; agent reads ``SKILL.md`` to load body
   into parent context and improvises. Signal:
   ``tool_call(name="read_file", args.file_path=".../SKILL.md")``.
   ``tool_result.is_error`` reflects whether the FILE was readable
   — **load** signal, not execution. Execution outcome of the
   subsequent improvised work is muddled with the parent turn
   (see plan in ``docs/skill-as-tool-architecture.md``).

The pre-deepagents ``Skill`` tool from claude-agent-sdk used to be
a third pattern; that runtime is gone, and the matching code has
been removed.

Outcome classification:
  - **success**   — tool_result with is_error=False, no in-turn retry
  - **failure**   — tool_result with is_error=True
  - **abandoned** — tool_call with no matching tool_result in the
    same turn (rare; usually means the agent pivoted before the SDK
    routed back). Treated as failure for ranking purposes but
    counted separately for diagnostics.

Skill catalog rendering is handled by deepagents' ``SkillsMiddleware``
(per the architecture restoration in PR #265). This module focuses on
per-skill telemetry — success/failure counts surfaced into the
``## Self-state`` block of the system prompt — not catalog rendering.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

# ``skills/<name>/SKILL.md`` (post-2026-05-22 relocation) or the
# legacy ``.claude/skills/<name>/SKILL.md``, either with any prefix
# (``/mimir-home/...``, ``./...``, bare relative). Also matches the
# read-only bundled location ``.mimir_builtin_skills/<name>/SKILL.md``.
# The capture group is the skill name — exactly one path segment.
#
# The legacy ``.claude/skills/`` alternative stays matched indefinitely
# because old turns.jsonl records (from before the relocation) carry
# the pre-migration path; dropping it would lose historical telemetry.
_SKILL_READ_RE = re.compile(
    r"(?:^|/)(?:\.claude/skills|\.mimir_builtin_skills|skills)/([^/]+)/SKILL\.md$",
)


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
    """Walk a single turn's events list, recognize skill invocations,
    pair their tool_call ↔ tool_result by id, yield
    ``(skill_name, outcome, ts)`` tuples.

    Outcome ∈ {"success", "failure", "abandoned"}.

    **Two invocation patterns on the deepagents runtime:**

    1. ``tool_call(name="task", args.subagent_type="<skill>")`` —
       delegated execution. Skill compiled to a SubAgent (see
       ``mimir.subagent_compiler``); parent agent invokes via the
       framework's ``task`` tool. ``tool_result.is_error`` directly
       reflects whether the subagent's workflow succeeded. This is
       the **execution** signal — clean, structured.

    2. ``tool_call(name="read_file", args.file_path="…/SKILL.md")``
       — inline load. Skill stays in the catalog; agent reads
       SKILL.md and improvises with its full parent tool surface.
       This is a **load** signal — measures whether the file was
       readable, NOT whether the subsequent improvised workflow
       succeeded. Inline-skill execution outcomes are inherently
       coupled to the parent turn (see plan in
       ``docs/skill-as-tool-architecture.md``).

    The classifier doesn't distinguish — it produces the same
    ``(skill, outcome, ts)`` tuples for both. Downstream
    aggregation treats them identically. Future enhancement: split
    counters into ``execution_success`` (task path) vs
    ``load_success`` (read_file path) so operators can see the
    difference.

    ``turn_succeeded`` is the overall turn signal (False when
    ``result_is_error`` is True). When a skill invocation has no
    matching tool_result — happens on the ChatClaudeCode streaming
    path for built-in tools, and on agent-pivots — we fall back to
    ``turn_succeeded`` instead of always emitting "abandoned".
    Without this fallback, every read_file load on a heartbeat
    turn lands in the risky bucket even when the turn completed
    cleanly.

    Resolution:
      - Matching tool_result present → use is_error (exact)
      - No tool_result + turn_succeeded=True  → "success" (inferred)
      - No tool_result + turn_succeeded=False → "failure" (inferred)
      - No tool_result + turn_succeeded=None  → "abandoned" (legacy /
        turn outcome unavailable)

    **Inference imprecision** (aggregate-level correct, per-call-
    level imprecise): turns with multiple tool kinds can't attribute
    a turn-level error to the specific skill that caused it. Inline
    skills are particularly affected (read_file load + N improvised
    tool calls; turn fails → all loads in that turn get attributed
    failure). Subagent-mode skills aren't affected — they have
    explicit per-invocation tool_result outcomes.
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
            if name == "task":
                # deepagents subagent-execution path: ``task`` tool
                # invokes a registered SubAgent. For mimir-skill-derived
                # SubAgents the subagent_type IS the skill name.
                # Framework's ``general-purpose`` subagent task calls
                # also land here but get filtered downstream because
                # they're not in the seeded skill list.
                sub = args.get("subagent_type")
                if isinstance(sub, str) and sub:
                    skill = sub
            elif name == "read_file":
                # deepagents inline path: agent reads SKILL.md to load
                # the skill into parent context, then improvises.
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
    # Use the turn-level signal as a fallback (see classifier
    # docstring for the imprecision tradeoff).
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
    *,
    now: datetime | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return three buckets in render order:

      1. **Proven** — used in window with success_rate ≥ 0.5.
         Sorted by (success_rate desc, last_used desc).
      2. **Untried** — seeded but no tool calls in window. Alphabetic.
      3. **Risky** — used but success_rate < 0.5. Alphabetic.
    """
    proven: list[tuple[str, float, datetime | None]] = []
    untried: list[str] = []
    risky: list[str] = []

    for name in seeded:
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

    proven.sort(key=lambda x: (-x[1], -(x[2].timestamp() if x[2] else 0)))
    return [p[0] for p in proven], sorted(untried), sorted(risky)


def render_skill_telemetry(
    seeded: list[str],
    aggregates: dict[str, SkillOutcome],
    *,
    now: datetime | None = None,
) -> str | None:
    """Render per-turn skill bucket telemetry for the ``## Self-state``
    block. Emits Proven (success ≥ 50%) and Risky (success < 50%)
    buckets with ``(N/M in window)`` counts.

    Untried skills (no completed invocations in window) are *not*
    enumerated here — the framework's ``SkillsMiddleware`` catalog
    already lists every available skill, and re-listing them here
    would just bloat the block with names that have no telemetry to
    share. The model can infer "any skill in the catalog not
    mentioned in telemetry has no recent activity."

    Returns None when no skill has any in-window activity (don't
    print an empty header). The returned body has no leading
    ``## Self-state`` header — caller composes it into a larger
    block.
    """
    proven, _untried, risky = order_skills(seeded, aggregates, now=now)
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
