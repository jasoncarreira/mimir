"""Skill outcome tracking + amplification (FUTURE_WORK §12.3).

Per-skill success / failure rates aggregated from turns.jsonl tool-call
events. Skills that consistently land get prompt-prominence; skills
that consistently fail get a quiet ⚠ marker; skills that have never
been tried sit in their own bucket so they don't crowd the proven ones.

Same shape as saga's ``mark_contributions`` but at the skill layer —
the second real amplification (positive feedback) loop in mimir.

Skill invocation pattern on the deepagents runtime is **inline load**:
the agent reads ``SKILL.md`` to pull the body into its own context and
improvises with the parent's full tool surface. Signal:
``tool_call(name="read_file", args.file_path=".../SKILL.md")``.
``tool_result.is_error`` reflects whether the FILE was readable
— **load** signal, not execution. Execution outcome of the
subsequent improvised work is muddled with the parent turn —
refined for some skills via the ``success_criteria`` frontmatter
block (see :class:`SkillSuccessCriteria`).

A second pattern — ``task(subagent_type=...)`` for compiled SubAgent
delegation — was tried in the PR #266 / #269 arc but removed
2026-05-23 (the LLM consistently routed around it; see the rip-out
commit). The ``execution``-kind path in the classifier remains since
the framework still auto-injects a ``general-purpose`` subagent the
agent can occasionally use; those calls just don't correspond to
any mimir skill.

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

import fnmatch
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml

log = logging.getLogger(__name__)

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


# ─── inline-skill success criteria ───────────────────────────────────────
#
# The load signal (``read_file(SKILL.md)`` is_error=False) only tells
# us the agent loaded the prompt. Whether the agent followed the
# procedure described in the SKILL.md body is invisible. For skills
# whose "done" state has a clear declarative shape, operators declare
# a ``success_criteria`` block in frontmatter:
#
#   success_criteria:
#     any_of:
#       - tool_call:
#           name: send_message
#       - tool_call:
#           name: memory_store
#           args:
#             tier: ATOMIC
#
# When the classifier sees a successful load whose skill has a
# ``success_criteria``, it scans the rest of the turn for events
# matching any of the patterns. If at least one matches, the load is
# classified as **success** (procedure completed). If none match, the
# load is classified as **incomplete** — the file opened, but the
# stated outcome never landed. Operators see ``incomplete`` distinct
# from ``failure`` (file errored) and from ``abandoned`` (no result
# pair); incomplete is a "drift" signal, the others are "broken"
# signals.
#
# Schema (intentionally minimal — extend per skill as needs surface):
#   any_of: list of patterns (success on FIRST match)
#   pattern:
#     tool_call:
#       name: str  (required)
#       args: dict (optional; subset-match — keys present in the
#                   pattern must equal the event's args[key])


@dataclass
class SkillSuccessCriteria:
    """Operator-declared "did the skill's procedure actually run?" test.

    Parsed from a skill's frontmatter ``success_criteria`` block.
    Used by :func:`_classify_skill_calls` to refine the load signal
    into success / incomplete based on subsequent tool calls in the
    same turn.
    """

    any_of: list[dict[str, Any]] = field(default_factory=list)

    def matches_any(self, events_after_load: Iterable[dict]) -> bool:
        """Return True iff at least one ``any_of`` pattern matches at
        least one event in the iterable. Empty ``any_of`` returns True
        — no criteria == nothing to check (backward compat for skills
        that have a ``success_criteria:`` block but no patterns yet)."""
        if not self.any_of:
            return True
        for ev in events_after_load:
            if not isinstance(ev, dict) or ev.get("type") != "tool_call":
                continue
            for pattern in self.any_of:
                if _pattern_matches_event(pattern, ev):
                    return True
        return False


def _pattern_matches_event(pattern: dict[str, Any], event: dict) -> bool:
    """Subset-match a success-criteria pattern against a tool_call event.

    Pattern shape: ``{"tool_call": {"name": str, "args": dict?}}``.
    Subset semantics: every key the pattern declares must be present
    in the event AND match. Keys the event has but the pattern
    doesn't declare are ignored.

    **Args matching** (per arg key):

    * ``<key>: <value>`` — exact equality. Use for enums, ids, etc.
    * ``<key>_glob: "<pattern>"`` — fnmatch glob applied to
      ``event.args[<key>]``. Use for path matches and other
      string-prefix patterns. The matched key in the event is the
      one *without* the ``_glob`` suffix; the suffix only exists in
      the pattern. Operators pick one form per arg.

    fnmatch semantics: ``*`` matches any chars *including* ``/``, so
    ``state/wiki/*.md`` matches ``state/wiki/entities/foo.md`` as
    well as ``state/wiki/index.md``. Operators wanting strict
    single-segment matching should use ``?`` plus literal segment
    boundaries.
    """
    pat_tc = pattern.get("tool_call")
    if not isinstance(pat_tc, dict):
        return False
    expected_name = pat_tc.get("name")
    if expected_name and event.get("name") != expected_name:
        return False
    expected_args = pat_tc.get("args")
    if isinstance(expected_args, dict) and expected_args:
        actual_args = event.get("args") or {}
        if not isinstance(actual_args, dict):
            return False
        for k, expected in expected_args.items():
            if k.endswith("_glob"):
                real_key = k[: -len("_glob")]
                actual_val = actual_args.get(real_key)
                if not isinstance(actual_val, str):
                    return False
                if not isinstance(expected, str):
                    return False
                if not fnmatch.fnmatch(actual_val, expected):
                    return False
            else:
                if actual_args.get(k) != expected:
                    return False
    return True


def load_skill_success_criteria(home: Path) -> dict[str, SkillSuccessCriteria]:
    """Scan ``<home>/.mimir_builtin_skills/`` and ``<home>/skills/`` for
    skills that declare ``success_criteria`` in frontmatter.

    Returns ``{skill_name: SkillSuccessCriteria}`` for the skills that
    have one. Skills without the block aren't in the result map —
    callers use ``.get(name)`` and treat absence as "no criteria check
    needed, just trust the load signal."

    Operator-installed skills (``<home>/skills/``) shadow bundled
    same-named entries, mirroring SkillsMiddleware's last-source-wins
    rule.
    """
    from .skill_defs import home_builtin_skills_dir, home_skills_dir
    out: dict[str, SkillSuccessCriteria] = {}
    for src in (home_builtin_skills_dir(home), home_skills_dir(home)):
        if not src.is_dir():
            continue
        for skill_md in src.glob("*/SKILL.md"):
            try:
                criteria = _parse_criteria_from_skill_md(skill_md)
            except (OSError, yaml.YAMLError) as exc:
                log.warning(
                    "load_skill_success_criteria: failed to parse %s: %s",
                    skill_md, exc,
                )
                continue
            if criteria is not None:
                out[skill_md.parent.name] = criteria
    return out


def _parse_criteria_from_skill_md(path: Path) -> SkillSuccessCriteria | None:
    """Read a SKILL.md frontmatter and extract ``success_criteria`` if
    present. Returns ``None`` when the field isn't declared (vs. an
    empty :class:`SkillSuccessCriteria` when declared-but-empty, which
    matches everything — operator probably mid-authoring)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    fm_str = text[4:end]
    fm = yaml.safe_load(fm_str)
    if not isinstance(fm, dict):
        return None
    block = fm.get("success_criteria")
    if not isinstance(block, dict):
        return None
    any_of_raw = block.get("any_of")
    any_of: list[dict[str, Any]] = []
    if isinstance(any_of_raw, list):
        any_of = [p for p in any_of_raw if isinstance(p, dict)]
    return SkillSuccessCriteria(any_of=any_of)


# VSM: S3 — skill amplification. Per-skill success-rate aggregator
#          shapes prompt prominence so the agent sees high-value
#          skills first and avoids ones that consistently fail.
# loop_id: 12.3
@dataclass
class SkillOutcome:
    """Per-skill outcome tallies across a window.

    The ``success`` / ``failure`` / ``abandoned`` fields are *totals*
    used for ranking (proven vs risky buckets). Two distinct invocation
    paths feed these totals — the per-path counters
    (``execution_*`` and ``load_*``) preserve the breakdown so an
    operator can tell apart "skill ran cleanly" from "skill loaded
    cleanly":

    * **execution** — ``task(subagent_type=X)`` call returns a
      ``tool_result`` whose ``is_error`` flag *directly* reflects
      whether the subagent's workflow finished cleanly. Clean signal.
    * **load** — ``read_file(.../SKILL.md)`` call's ``is_error`` only
      tells us the SKILL.md file opened, NOT whether the agent
      followed the procedure it described. Proxy signal coupled to
      the parent turn's overall outcome via the fallback in
      :func:`_classify_skill_calls`.

    Mixing the two into a single rate hides that distinction. The
    breakdown matters when an operator asks "is this skill genuinely
    succeeding, or is it just reading its own SKILL.md and then
    drifting?". Aggregate counters stay as the ranking input;
    breakdown counters are surface-level diagnostics.
    """

    skill: str
    success: int = 0
    failure: int = 0
    abandoned: int = 0
    # ``incomplete`` — load opened fine but the operator-declared
    # ``success_criteria`` didn't match anywhere in the post-load
    # tail. Distinct from ``failure`` (load errored) and from
    # ``abandoned`` (no tool_result pair). Only emitted for the
    # ``load`` kind — execution outcomes already have a clean signal
    # via ``tool_result.is_error`` on the ``task()`` result.
    incomplete: int = 0
    # Per-path breakdown of the totals above. Sum invariants:
    # ``execution_success + load_success == success`` (and same for
    # failure/abandoned/incomplete — though execution_incomplete is
    # always 0 since the criteria check only runs on loads).
    execution_success: int = 0
    execution_failure: int = 0
    execution_abandoned: int = 0
    load_success: int = 0
    load_failure: int = 0
    load_abandoned: int = 0
    load_incomplete: int = 0
    last_used: datetime | None = None

    @property
    def total(self) -> int:
        return self.success + self.failure + self.abandoned + self.incomplete

    @property
    def execution_total(self) -> int:
        return (
            self.execution_success
            + self.execution_failure
            + self.execution_abandoned
        )

    @property
    def load_total(self) -> int:
        return (
            self.load_success
            + self.load_failure
            + self.load_abandoned
            + self.load_incomplete
        )

    @property
    def success_rate(self) -> float | None:
        """Rate of successful invocations over the total. Abandoned and
        incomplete both count as "not success" for ranking purposes —
        abandoned because the agent gave up mid-skill, incomplete
        because it loaded but the procedure criteria didn't match. None
        when no usable data."""
        if self.total == 0:
            return None
        return self.success / self.total

    @property
    def execution_success_rate(self) -> float | None:
        """Success rate computed only from the clean ``task()`` path.
        ``None`` when the skill never ran via ``task()`` in window."""
        if self.execution_total == 0:
            return None
        return self.execution_success / self.execution_total

    @property
    def load_success_rate(self) -> float | None:
        """Success rate computed only from the proxy ``read_file()``
        path. ``None`` when the skill never loaded inline in window.
        Less reliable than ``execution_success_rate`` — read the
        :class:`SkillOutcome` docstring on the proxy/clean distinction
        before acting on this number."""
        if self.load_total == 0:
            return None
        return self.load_success / self.load_total


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
    skill_criteria: dict[str, SkillSuccessCriteria] | None = None,
) -> Iterable[tuple[str, str, datetime, str]]:
    """Walk a single turn's events list, recognize skill invocations,
    pair their tool_call ↔ tool_result by id, yield
    ``(skill_name, outcome, ts, kind)`` tuples.

    Outcome ∈ {"success", "failure", "abandoned"}.
    Kind ∈ {"execution", "load"} — see below.

    **Two invocation patterns on the deepagents runtime:**

    1. ``tool_call(name="task", args.subagent_type="<skill>")`` —
       still detected for the framework's auto-injected
       ``general-purpose`` subagent and any future operator-supplied
       subagent specs. Emitted with ``kind="execution"`` — a clean
       per-invocation signal. Mimir no longer compiles its own
       skills to SubAgents (the spike was removed 2026-05-23), so
       this path's ``skill`` field is usually ``general-purpose``
       and gets filtered downstream because it isn't in the seeded
       skill list.

    2. ``tool_call(name="read_file", args.file_path="…/SKILL.md")``
       — inline load. Skill stays in the catalog; agent reads
       SKILL.md and improvises with its full parent tool surface.
       Emitted with ``kind="load"`` — this measures only whether the
       file was readable, NOT whether the subsequent improvised
       workflow succeeded. Inline-skill execution outcomes are
       inherently coupled to the parent turn — refined per-skill
       via ``success_criteria`` frontmatter where the completion
       signal is declaratively defined.

    Downstream aggregation accumulates totals (used for ranking) and
    per-kind breakdowns (used for diagnostics) separately so an
    operator can tell "ran cleanly" from "loaded cleanly". See
    :class:`SkillOutcome` for the breakdown's semantics.

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
    pending: dict[str, tuple[str, str, int]] = {}   # tool_use_id → (skill name, kind, call_index)
    for idx, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "tool_call":
            name = ev.get("name")
            args = ev.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            skill: str | None = None
            kind: str = ""
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
                    kind = "execution"
            elif name == "read_file":
                # deepagents inline path: agent reads SKILL.md to load
                # the skill into parent context, then improvises.
                file_path = args.get("file_path") or ""
                if isinstance(file_path, str):
                    m = _SKILL_READ_RE.search(file_path)
                    if m:
                        skill = m.group(1)
                        kind = "load"
            if skill:
                tool_id = ev.get("id")
                if isinstance(tool_id, str):
                    pending[tool_id] = (skill, kind, idx)
        elif etype == "tool_result":
            tool_id = ev.get("id")
            if isinstance(tool_id, str) and tool_id in pending:
                skill, kind, call_idx = pending.pop(tool_id)
                is_error = bool(ev.get("is_error"))
                if is_error:
                    yield skill, "failure", turn_ts, kind
                else:
                    # Successful pair. For LOAD kind, refine via
                    # operator-declared success_criteria if any.
                    outcome = _refine_load_outcome(
                        skill, kind, events, call_idx, skill_criteria,
                    )
                    yield skill, outcome, turn_ts, kind

    # Anything left in pending has no matching tool_result.
    # Use the turn-level signal as a fallback (see classifier
    # docstring for the imprecision tradeoff). Criteria don't apply
    # here — without a tool_result anchor we don't know how to
    # bound the "after the load" window.
    for skill, kind, _ in pending.values():
        if turn_succeeded is True:
            yield skill, "success", turn_ts, kind
        elif turn_succeeded is False:
            yield skill, "failure", turn_ts, kind
        else:
            yield skill, "abandoned", turn_ts, kind


def _refine_load_outcome(
    skill: str, kind: str, events: list[dict], call_idx: int,
    skill_criteria: dict[str, SkillSuccessCriteria] | None,
) -> str:
    """For a successful tool_call/tool_result pair, refine to
    ``"success"`` or ``"incomplete"`` based on the skill's declared
    ``success_criteria`` (if any).

    Only LOAD kind gets refined — execution kind already has the
    clean ``task()`` signal. Skills without criteria stay at
    ``"success"`` (no refinement available; we trust the load signal).

    **Scope caveat — shared-tail false positives.** The scan window
    runs from the load's ``tool_call`` index to the end of the turn.
    If a turn loads two inline skills A and B back-to-back, skill A's
    criteria match against B's subsequent tool calls too — B's
    ``memory_store`` could credit A as ``success`` even though A's
    procedure was never followed. In practice this is mild because
    criteria patterns are typically distinctive (e.g., ``send_message``
    for alert, ``write_file → state/wiki/`` for wiki), but for skills
    whose criteria patterns match tools other skills also call, the
    confound is real. Operators authoring new criteria should pick
    patterns that uniquely identify their skill's completion signal
    where possible.
    """
    if kind != "load" or not skill_criteria:
        return "success"
    criteria = skill_criteria.get(skill)
    if criteria is None:
        return "success"
    # Scan events after the load's tool_call index. The tool_result
    # itself sits somewhere after call_idx; criteria patterns only
    # match tool_call events anyway so the tool_result event in
    # between is a no-op for matching.
    tail = events[call_idx + 1:]
    return "success" if criteria.matches_any(tail) else "incomplete"


def aggregate(
    turns_log: Path, *,
    window_hours: int = 24 * 7,   # 7d default
    now: datetime | None = None,
    skill_criteria: dict[str, SkillSuccessCriteria] | None = None,
) -> dict[str, SkillOutcome]:
    """Walk turns.jsonl, accumulate per-skill outcome counts within
    the window.

    ``skill_criteria`` opts into per-skill procedure-completion checks
    (see :class:`SkillSuccessCriteria`). Load events from skills with
    criteria get refined into ``success`` vs ``incomplete`` based on
    subsequent tool calls in the same turn. ``None`` (default) keeps
    the legacy load-only-signal behavior. Callers typically supply
    ``load_skill_success_criteria(home)``.
    """
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
        for skill, outcome, _, kind in _classify_skill_calls(
            events, ts,
            turn_succeeded=turn_succeeded,
            skill_criteria=skill_criteria,
        ):
            entry = out[skill]
            entry.skill = skill
            # Increment the aggregate total AND the per-kind counter
            # so totals stay correct for ranking and the breakdown
            # stays correct for diagnostics. Unknown kinds (legacy
            # records, future paths) only bump the aggregate; the
            # breakdown is best-effort.
            if outcome == "success":
                entry.success += 1
                if kind == "execution":
                    entry.execution_success += 1
                elif kind == "load":
                    entry.load_success += 1
            elif outcome == "failure":
                entry.failure += 1
                if kind == "execution":
                    entry.execution_failure += 1
                elif kind == "load":
                    entry.load_failure += 1
            elif outcome == "incomplete":
                entry.incomplete += 1
                if kind == "load":
                    entry.load_incomplete += 1
            else:  # abandoned
                entry.abandoned += 1
                if kind == "execution":
                    entry.execution_abandoned += 1
                elif kind == "load":
                    entry.load_abandoned += 1
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

    def _fmt_counts(agg: SkillOutcome) -> str:
        """Render the count summary. When a skill has activity on
        both paths (task() execution AND read_file() load), show the
        breakdown so the operator can tell apart the clean signal
        from the proxy one. Single-path skills render compactly. An
        ``incomplete`` suffix surfaces when the operator's
        success_criteria detected drift (file loaded but procedure
        didn't fire)."""
        et = agg.execution_total
        lt = agg.load_total
        incomplete_suffix = (
            f", {agg.incomplete} incomplete" if agg.incomplete else ""
        )
        if et and lt:
            return (
                f"{agg.success}/{agg.total} in window — "
                f"exec {agg.execution_success}/{et}, "
                f"load {agg.load_success}/{lt}{incomplete_suffix}"
            )
        if et:
            return f"{agg.success}/{agg.total} in window (exec)"
        if lt:
            return (
                f"{agg.success}/{agg.total} in window (load)"
                f"{incomplete_suffix}"
            )
        return f"{agg.success}/{agg.total} in window{incomplete_suffix}"

    lines: list[str] = []
    if proven:
        lines.append("- skills proven (recent success):")
        for name in proven:
            lines.append(f"  - {name} ({_fmt_counts(aggregates[name])})")
    if risky:
        lines.append("- skills risky ⚠ (recent failures > successes):")
        for name in risky:
            lines.append(f"  - {name} ({_fmt_counts(aggregates[name])})")
    return "\n".join(lines)
