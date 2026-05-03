"""Applied-proposals audit (FUTURE_WORK §12.2).

Closes the double-loop: when the operator merges a reflection-proposed
change, ``mark_applied()`` records the proposal + its predicted effect
into ``state/applied-proposals.jsonl``. A later reflection turn calls
``audit_window()`` to compare the predicted effect against measured
signals (error rate, tool-call frequency) before vs after the apply
timestamp.

Without this loop, reflection drafts plausible-sounding proposals but
nothing measures whether merging them helped — single-loop only. With
it, the agent gets a feedback signal on its own policy changes.

Storage format: ``state/applied-proposals.jsonl`` is append-only JSONL.
One record per applied proposal:

    {
      "id": "2026-04-12 — split persona block",
      "applied_at": "2026-04-12T15:22:00+00:00",
      "source": "reflection 2026-04-12",
      "proposal": "Split memory/core/00-persona.md into …",
      "rationale": "...",
      "affected": "memory/core/00-persona.md",
      "predicted_effect": "Drift indicator (generic-assistant patterns) drops"
    }
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


# ─── Data model ────────────────────────────────────────────────────────


@dataclass
class AppliedProposal:
    """One ``state/applied-proposals.jsonl`` record."""

    id: str
    applied_at: str            # ISO timestamp
    source: str = ""
    proposal: str = ""
    rationale: str = ""
    affected: str = ""
    predicted_effect: str = ""


@dataclass
class Signal:
    """One predicted-vs-actual signal for an audit row."""

    name: str
    before: float
    after: float
    unit: str = ""

    @property
    def delta(self) -> float:
        return self.after - self.before

    @property
    def delta_pct(self) -> float | None:
        if self.before <= 0:
            return None
        return (self.after - self.before) / self.before


@dataclass
class AuditRow:
    proposal: AppliedProposal
    signals: list[Signal] = field(default_factory=list)


# ─── Parsing proposed-changes.md ────────────────────────────────────────


def _split_md_sections(body: str) -> list[tuple[str, str]]:
    """Return [(heading_text_without_##, section_body), ...] keeping
    everything in document order. Section body excludes the heading."""
    out: list[tuple[str, str]] = []
    cur_head: str | None = None
    cur_buf: list[str] = []
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("## "):
            if cur_head is not None:
                out.append((cur_head, "\n".join(cur_buf).rstrip()))
            cur_head = stripped[3:].strip()
            cur_buf = []
        elif cur_head is not None:
            cur_buf.append(line)
    if cur_head is not None:
        out.append((cur_head, "\n".join(cur_buf).rstrip()))
    return out


def _parse_proposal_body(heading: str, body: str) -> AppliedProposal:
    """Pull ``Source:`` / ``Proposal:`` / etc. lines out of a proposal
    section. Lenient — missing fields default to empty strings."""
    fields: dict[str, str] = {}
    for line in body.splitlines():
        m = re.match(r"^(Source|Proposal|Rationale|Affected|Predicted effect):\s*(.*)$",
                     line.strip(), re.IGNORECASE)
        if m:
            key = m.group(1).lower().replace(" ", "_")
            fields[key] = m.group(2).strip()
    return AppliedProposal(
        id=heading,
        applied_at="",
        source=fields.get("source", ""),
        proposal=fields.get("proposal", ""),
        rationale=fields.get("rationale", ""),
        affected=fields.get("affected", ""),
        predicted_effect=fields.get("predicted_effect", ""),
    )


def mark_applied(
    proposed_changes_path: Path,
    applied_log_path: Path,
    id_match: str,
    *,
    now: datetime | None = None,
) -> AppliedProposal:
    """Find a proposal in ``## Pending`` whose heading contains
    ``id_match`` (case-insensitive substring), move it to ``## Applied``,
    and append a record to ``applied-proposals.jsonl``.

    Returns the matched proposal. Raises ``LookupError`` when no
    matching pending entry is found, ``ValueError`` when the file
    structure isn't recognized."""
    now = now or datetime.now(tz=timezone.utc)
    if not proposed_changes_path.is_file():
        raise FileNotFoundError(proposed_changes_path)

    raw = proposed_changes_path.read_text(encoding="utf-8")

    # We treat the file as a flat sequence of `##` sections. Pending /
    # Applied / Rejected are themselves `##` sections; nested
    # YYYY-MM-DD proposals also start with `##`. Disambiguate by the
    # heading text — top-level buckets are exactly "Pending" / "Applied"
    # / "Rejected".
    sections = _split_md_sections(raw)
    if not sections:
        raise ValueError("no '## …' sections found in proposed-changes.md")

    pending_idx = None
    applied_idx = None
    for i, (head, _) in enumerate(sections):
        if head.lower() == "pending":
            pending_idx = i
        elif head.lower() == "applied":
            applied_idx = i
    if pending_idx is None:
        raise ValueError("missing '## Pending' section")

    # Proposals belonging to Pending are the dated `##` sections that
    # follow the Pending header in document order, up to the next
    # bucket header (Applied / Rejected) or EOF.
    end_idx = len(sections)
    for j in range(pending_idx + 1, len(sections)):
        if sections[j][0].lower() in ("applied", "rejected"):
            end_idx = j
            break

    match_pos: int | None = None
    needle = id_match.lower()
    for j in range(pending_idx + 1, end_idx):
        if needle in sections[j][0].lower():
            match_pos = j
            break
    if match_pos is None:
        raise LookupError(
            f"no pending proposal heading contains {id_match!r}"
        )

    head, body = sections[match_pos]
    proposal = _parse_proposal_body(head, body)
    proposal.applied_at = now.isoformat()

    # Reassemble the file: drop the matched section from where it was;
    # append it under Applied. Keep prelude (anything before the first
    # `##`) intact.
    prelude = ""
    first_section_pos = raw.find("\n## ")
    if first_section_pos == -1 and raw.lstrip().startswith("## "):
        first_section_pos = 0
    if first_section_pos > 0:
        prelude = raw[:first_section_pos].rstrip() + "\n\n"
    elif raw.lstrip().startswith("## "):
        prelude = ""

    new_sections = list(sections)
    moved = new_sections.pop(match_pos)

    # Re-find applied_idx (may shift after pop if it was after match_pos).
    applied_idx2 = None
    for i, (h, _) in enumerate(new_sections):
        if h.lower() == "applied":
            applied_idx2 = i
            break
    if applied_idx2 is None:
        # No Applied section; append one.
        new_sections.append(("Applied", ""))
        applied_idx2 = len(new_sections) - 1

    # Insert moved at the END of the Applied bucket — i.e. just before
    # the next top-level bucket (Rejected) or at EOF.
    insert_at = len(new_sections)
    for j in range(applied_idx2 + 1, len(new_sections)):
        if new_sections[j][0].lower() in ("pending", "rejected"):
            insert_at = j
            break
    new_sections.insert(insert_at, moved)

    rebuilt = prelude + "\n\n".join(
        f"## {h}" + (("\n" + b) if b else "")
        for h, b in new_sections
    ).rstrip() + "\n"

    proposed_changes_path.write_text(rebuilt, encoding="utf-8")

    # Append the JSONL record.
    applied_log_path.parent.mkdir(parents=True, exist_ok=True)
    with applied_log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(proposal), ensure_ascii=False) + "\n")

    return proposal


def load_applied_proposals(path: Path) -> list[AppliedProposal]:
    if not path.is_file():
        return []
    out: list[AppliedProposal] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            out.append(AppliedProposal(**data))
        except TypeError:
            continue
    return out


# ─── Signal queries ────────────────────────────────────────────────────


def _count_events_in_window(
    events_log: Path,
    *,
    start: datetime,
    end: datetime,
    type_filter: callable | None = None,
) -> int:
    """Walk events.jsonl, count records whose ``timestamp`` is in
    [start, end) and match ``type_filter`` (if set)."""
    if not events_log.is_file():
        return 0
    n = 0
    try:
        text = events_log.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_raw = rec.get("timestamp")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < start or ts >= end:
            continue
        if type_filter is not None and not type_filter(rec):
            continue
        n += 1
    return n


def _count_tool_calls_in_window(
    turns_log: Path,
    *,
    start: datetime,
    end: datetime,
    tool_name: str | None = None,
) -> int:
    """Walk turns.jsonl, count tool_call events in turns whose ``ts``
    is in [start, end). Filter by tool name when given."""
    if not turns_log.is_file():
        return 0
    n = 0
    try:
        text = turns_log.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_raw = rec.get("ts")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < start or ts >= end:
            continue
        for ev in rec.get("events") or []:
            if not isinstance(ev, dict):
                continue
            if ev.get("type") != "tool_call":
                continue
            if tool_name is not None and ev.get("name") != tool_name:
                continue
            n += 1
    return n


_ERROR_EVENT_TYPES = {
    "tool_denied", "scheduled_tick_dropped", "scheduled_tick_suppressed",
    "rate_limit_off_pace", "cost_rate_alert",
}
_TOOL_NAME_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]+)\b")


def _signal_kind_from_predicted(text: str) -> tuple[str, str | None]:
    """Map ``Predicted effect:`` text to (kind, target). Kinds:

    - ``error_rate`` — "error rate would drop / fall / decrease"
    - ``tool_freq`` — "<TOOL> would be invoked more / less often"
    - ``unknown``   — couldn't classify; audit row carries no signals

    Heuristic — operators write predicted effects in plain English; we
    parse the obvious cases and leave the rest for v2."""
    t = text.lower()
    if "error" in t and ("drop" in t or "fall" in t or "decrease" in t
                          or "fewer" in t or "less" in t or "down" in t):
        return ("error_rate", None)
    if "error" in t and ("rise" in t or "increase" in t or "more" in t
                          or "up" in t):
        return ("error_rate", None)
    # Tool frequency: look for a CamelCase token followed by frequency words.
    if "tool" in t or "skill" in t or "invoke" in t or "call" in t:
        m = _TOOL_NAME_RE.search(text)  # original casing
        if m:
            return ("tool_freq", m.group(1))
    return ("unknown", None)


def compute_signals(
    proposal: AppliedProposal,
    *,
    events_log: Path,
    turns_log: Path,
    window_days: int = 7,
    now: datetime | None = None,
) -> list[Signal]:
    """Return measured signals for one applied proposal: same-length
    windows before and after ``proposal.applied_at``."""
    now = now or datetime.now(tz=timezone.utc)
    try:
        applied_at = datetime.fromisoformat(
            proposal.applied_at.replace("Z", "+00:00")
        )
    except ValueError:
        return []

    before_start = applied_at - timedelta(days=window_days)
    before_end = applied_at
    after_start = applied_at
    after_end = min(applied_at + timedelta(days=window_days), now)
    if after_end <= after_start:
        return []

    kind, target = _signal_kind_from_predicted(proposal.predicted_effect)
    if kind == "unknown":
        return []

    out: list[Signal] = []
    if kind == "error_rate":
        before = _count_events_in_window(
            events_log, start=before_start, end=before_end,
            type_filter=lambda r: r.get("type") in _ERROR_EVENT_TYPES,
        )
        after = _count_events_in_window(
            events_log, start=after_start, end=after_end,
            type_filter=lambda r: r.get("type") in _ERROR_EVENT_TYPES,
        )
        out.append(Signal(name="error_events", before=before, after=after,
                          unit="count"))
    elif kind == "tool_freq":
        before = _count_tool_calls_in_window(
            turns_log, start=before_start, end=before_end, tool_name=target,
        )
        after = _count_tool_calls_in_window(
            turns_log, start=after_start, end=after_end, tool_name=target,
        )
        out.append(Signal(
            name=f"tool_calls:{target}", before=before, after=after,
            unit="count",
        ))
    return out


def audit_window(
    home: Path,
    *,
    weeks_back_min: int = 1,
    weeks_back_max: int = 4,
    now: datetime | None = None,
    window_days: int = 7,
) -> list[AuditRow]:
    """Read ``state/applied-proposals.jsonl``, return one AuditRow per
    proposal applied between ``weeks_back_max`` and ``weeks_back_min``
    weeks ago. Each row carries computed before/after signals."""
    now = now or datetime.now(tz=timezone.utc)
    # weeks_back_min is the *newest* age we accept (e.g. 1w ago);
    # weeks_back_max is the *oldest* (e.g. 4w ago). Naming the actual
    # time bounds avoids the cutoff_min/cutoff_max trap where "min" of
    # an age range is the *newer* end of the time range.
    newest_ts = now - timedelta(weeks=weeks_back_min)
    oldest_ts = now - timedelta(weeks=weeks_back_max)
    applied = load_applied_proposals(home / "state" / "applied-proposals.jsonl")
    rows: list[AuditRow] = []
    for p in applied:
        try:
            ts = datetime.fromisoformat(p.applied_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if not (oldest_ts <= ts <= newest_ts):
            continue
        signals = compute_signals(
            p,
            events_log=home / "logs" / "events.jsonl",
            turns_log=home / "logs" / "turns.jsonl",
            window_days=window_days,
            now=now,
        )
        rows.append(AuditRow(proposal=p, signals=signals))
    return rows


def render_audit_block(rows: Iterable[AuditRow]) -> str | None:
    """Format audit rows as the body of a ``## Effects of prior
    proposals`` reflection section. Returns None when no rows."""
    rows = list(rows)
    if not rows:
        return None
    lines: list[str] = []
    for row in rows:
        lines.append(f"### {row.proposal.id}")
        if row.proposal.predicted_effect:
            lines.append(f"_Predicted:_ {row.proposal.predicted_effect}")
        if not row.signals:
            lines.append("_Measured:_ no parseable predicted-effect signal.")
        else:
            for s in row.signals:
                pct = ""
                if s.delta_pct is not None:
                    pct = f" ({s.delta_pct * 100:+.0f}%)"
                lines.append(
                    f"_Measured:_ **{s.name}** {s.before:.0f} → {s.after:.0f}"
                    f"{pct}"
                )
        lines.append("")
    return "\n".join(lines).rstrip()
