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


#: proposed-changes.md ``## `` headings that are real section boundaries:
#: the three top-level buckets, plus date-prefixed proposal headings
#: (``## 2026-05-27 — …``). Everything else at ``## `` level is body.
_PROPOSAL_BUCKETS = {"pending", "applied", "rejected"}
_DATE_HEADING_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\b")


def _is_section_boundary(heading_text: str) -> bool:
    """True when a ``## `` heading is a real proposed-changes.md boundary —
    a bucket name or a date-prefixed proposal heading (#497).

    LLM-authored proposal bodies routinely contain prose subheadings at
    ``## `` level (e.g. ``## Risks``). Treating those as boundaries made
    ``mark_applied`` / ``mark_reject`` move only up to the subheading —
    stranding the remainder in Pending and creating a phantom, un-date-parseable
    ``## Risks`` entry that inflated backlog-health counts. Folding non-boundary
    headings into the current section closes that."""
    h = heading_text.strip()
    return h.lower() in _PROPOSAL_BUCKETS or bool(_DATE_HEADING_RE.match(h))


def _split_md_sections(body: str) -> list[tuple[str, str]]:
    """Return [(heading_text_without_##, section_body), ...] keeping
    everything in document order. Section body excludes the heading.

    Fence-aware: ``##`` lines inside fenced code blocks (``` ... ```) are
    treated as part of the surrounding section body, not as new section
    boundaries. Proposal bodies routinely contain fenced samples whose
    own headings would otherwise mis-split the entry — chainlink #114.

    Boundary-aware (#497): only bucket headings and date-prefixed proposal
    headings start a new section (see ``_is_section_boundary``); other ``## ``
    lines (prose subheadings inside an LLM-authored proposal body) stay with
    the current section.
    """
    out: list[tuple[str, str]] = []
    cur_head: str | None = None
    cur_buf: list[str] = []
    in_fence = False
    for line in body.splitlines():
        stripped = line.lstrip()
        # Toggle on any ``` line. Keep the fence line itself in whichever
        # bucket (prelude is dropped before the first heading; section
        # body otherwise).
        if stripped.startswith("```"):
            in_fence = not in_fence
            if cur_head is not None:
                cur_buf.append(line)
            continue
        if (
            not in_fence
            and stripped.startswith("## ")
            and _is_section_boundary(stripped[3:])
        ):
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


def _list_pending_proposals(path: Path) -> list[tuple[int, str, str]]:
    """Return ``[(num, heading, excerpt), ...]`` for every proposal in the
    ``## Pending`` bucket of *path*, numbered 1..N in document order.

    *heading* is the full heading text (e.g. ``"2026-05-27 — promote X"``).
    *excerpt* is the first non-empty, non-``##`` line of the proposal body,
    truncated to 120 chars.

    Raises ``FileNotFoundError`` when *path* does not exist.
    Raises ``ValueError`` when the file has no ``## Pending`` section.
    """
    if not path.is_file():
        raise FileNotFoundError(path)

    raw = path.read_text(encoding="utf-8")
    sections = _split_md_sections(raw)

    pending_idx = None
    for i, (head, _) in enumerate(sections):
        if head.lower() == "pending":
            pending_idx = i
            break
    if pending_idx is None:
        raise ValueError("missing '## Pending' section in proposed-changes.md")

    # Find the end of the Pending bucket.
    end_idx = len(sections)
    for j in range(pending_idx + 1, len(sections)):
        if sections[j][0].lower() in ("applied", "rejected"):
            end_idx = j
            break

    out: list[tuple[int, str, str]] = []
    num = 0
    for j in range(pending_idx + 1, end_idx):
        head, body = sections[j]
        num += 1
        # Excerpt: first non-empty non-## line in body.
        excerpt = ""
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("##"):
                excerpt = stripped[:120]
                break
        out.append((num, head, excerpt))
    return out


# ─── Reflection digest formatting ──────────────────────────────────────

_DIGEST_HEADING_MAX = 60
_DIGEST_REPLY_HINT = (
    "Reply: `accept 1 3` to apply, `reject 2 \"reason\"` to decline, `defer 1`"
    " to re-surface at next reflection. Multiple items OK: `accept 1 3 / reject 2`."
)


def format_reflection_digest(
    proposals: list[tuple[int, str, str]],
) -> str | None:
    """Format *proposals* into an operator digest message.

    Returns ``None`` when *proposals* is empty (silent reflection —
    no proposals written, no message needed).

    Each tuple is ``(num, heading, excerpt)`` as returned by
    ``_list_pending_proposals``.  The heading is truncated to
    ``_DIGEST_HEADING_MAX`` (60) chars; the excerpt is shown on the
    same line after a ``: ``.

    Format::

        Reflection complete — N pending proposals:

        1. **<heading[:60]>**: <excerpt>
        2. …

        Reply: `accept 1 3` to apply, …
    """
    if not proposals:
        return None

    n = len(proposals)
    header = f"Reflection complete — {n} pending proposal{'s' if n != 1 else ''}:\n"

    lines: list[str] = [header]
    for num, heading, excerpt in proposals:
        truncated = heading[:_DIGEST_HEADING_MAX]
        if len(heading) > _DIGEST_HEADING_MAX:
            truncated = truncated.rstrip() + "…"
        item = f"{num}. **{truncated}**"
        if excerpt:
            item += f": {excerpt}"
        lines.append(item)

    lines.append("")
    lines.append(_DIGEST_REPLY_HINT)

    return "\n".join(lines)


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


# ─── mark_reject + resolve parser ─────────────────────────────────────


def mark_reject(
    proposed_changes_path: Path,
    id_match: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> str:
    """Find a proposal in ``## Pending`` whose heading contains
    ``id_match`` (case-insensitive substring), move it to ``## Rejected``,
    and annotate it with a rejection timestamp + reason.

    Returns the matched heading text. Raises ``LookupError`` when no
    matching pending entry is found, ``ValueError`` when the file
    structure isn't recognised.

    Note: does NOT append to ``applied-proposals.jsonl``.
    """
    now = now or datetime.now(tz=timezone.utc)
    reason = reason.strip() or "operator declined"

    if not proposed_changes_path.is_file():
        raise FileNotFoundError(proposed_changes_path)

    raw = proposed_changes_path.read_text(encoding="utf-8")
    sections = _split_md_sections(raw)
    if not sections:
        raise ValueError("no '## …' sections found in proposed-changes.md")

    pending_idx = None
    for i, (head, _) in enumerate(sections):
        if head.lower() == "pending":
            pending_idx = i
            break
    if pending_idx is None:
        raise ValueError("missing '## Pending' section")

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
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    annotation = f"<!-- rejected: {ts} — {reason} -->"
    new_body = (body.rstrip() + "\n\n" + annotation) if body.strip() else annotation

    # Reassemble: drop from Pending, append under Rejected.
    prelude = ""
    first_section_pos = raw.find("\n## ")
    if first_section_pos == -1 and raw.lstrip().startswith("## "):
        first_section_pos = 0
    if first_section_pos > 0:
        prelude = raw[:first_section_pos].rstrip() + "\n\n"

    new_sections = list(sections)
    new_sections.pop(match_pos)

    # Find or create Rejected bucket (re-scan after pop).
    rejected_idx2: int | None = None
    for i, (h, _) in enumerate(new_sections):
        if h.lower() == "rejected":
            rejected_idx2 = i
            break
    if rejected_idx2 is None:
        new_sections.append(("Rejected", ""))
        rejected_idx2 = len(new_sections) - 1

    # Insert at end of Rejected bucket (before next top-level bucket or EOF).
    insert_at = len(new_sections)
    for j in range(rejected_idx2 + 1, len(new_sections)):
        if new_sections[j][0].lower() in ("pending", "applied"):
            insert_at = j
            break
    new_sections.insert(insert_at, (head, new_body))

    rebuilt = prelude + "\n\n".join(
        f"## {h}" + (("\n" + b) if b else "")
        for h, b in new_sections
    ).rstrip() + "\n"

    proposed_changes_path.write_text(rebuilt, encoding="utf-8")
    return head


# ─── resolve string parser ─────────────────────────────────────────────

_RESOLVE_CLAUSE_RE = re.compile(
    r"(accept|reject)\s+"       # action keyword
    r"([\d\s]+?)"               # one or more space-separated numbers
    r"(?:[\"']([^\"']*)[\"'])?" # optional quoted reason
    r"(?=\s*(?:/|$))",          # lookahead: clause separator or end
    re.IGNORECASE,
)


def parse_resolve_string(
    decision: str,
) -> list[tuple[str, int, str]]:
    """Parse an operator resolve string into ``[(action, num, reason), ...]``.

    Supports::

        "accept 1 3"
        "reject 2 'not now'"
        "accept 1 3 / reject 2 \"reason\""
        "reject 2"          → reason defaults to empty string (CLI fills default)

    Returns a list of ``(action, num, reason)`` triples where *action* is
    ``"accept"`` or ``"reject"``, *num* is the proposal number (1-based),
    and *reason* is the quoted string (or ``""`` when omitted).

    Raises ``ValueError`` when no valid clauses are found.
    """
    ops: list[tuple[str, int, str]] = []
    for m in _RESOLVE_CLAUSE_RE.finditer(decision):
        action = m.group(1).lower()
        nums_raw = m.group(2).split()
        reason = m.group(3) or ""
        for n_str in nums_raw:
            try:
                num = int(n_str)
            except ValueError:
                continue
            if num < 1:
                continue
            ops.append((action, num, reason))
    if not ops:
        raise ValueError(
            f"no valid accept/reject clauses found in {decision!r}"
        )
    return ops


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
    """Count events.jsonl records whose ``timestamp`` is in [start, end)
    and match ``type_filter`` (if set).

    chainlink #244: iterates newest-first via
    :func:`tail_jsonl_records` and early-breaks once ``ts < start`` —
    O(window) reads instead of O(file). The events log is capped at
    ~300 MB; the prior ``read_text()`` shape was loading the whole
    thing per call.
    """
    from .._jsonl_tail import tail_jsonl_records

    n = 0
    for rec in tail_jsonl_records(events_log):
        ts_raw = rec.get("timestamp")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < start:
            # Newest-first: every subsequent record is older → done.
            break
        if ts >= end:
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
    """Count ``tool_call`` events in turns.jsonl turns whose ``ts`` is
    in [start, end). Filter by tool name when given.

    chainlink #244: same shape as :func:`_count_events_in_window` —
    newest-first iteration + early-break on ``ts < start``.
    """
    from .._jsonl_tail import tail_jsonl_records

    n = 0
    for rec in tail_jsonl_records(turns_log):
        ts_raw = rec.get("ts")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < start:
            break
        if ts >= end:
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

# Tool-name allowlist for the prose-heuristic fallback. The original
# implementation matched ANY ``[A-Z][A-Za-z0-9_]+`` token, which fires
# on capitalized English words ("Adding", "Future", "With", "Unblocks")
# and produces phantom ``tool_calls:Adding 0 → 0`` rows in the audit
# block. Restrict to:
#   - snake_case names (anything with an underscore — covers every
#     mimir-registered tool: send_message, memory_query, saga_*, etc.)
#   - a small set of single-word PascalCase tools that surface through
#     deepagents / claude-code's built-in toolset
# New PascalCase tools added through deepagents need an entry here.
_KNOWN_PASCAL_TOOLS: frozenset[str] = frozenset({
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "Task",
})
_TOOL_NAME_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*(?:_[A-Za-z0-9_]+)+|[A-Z][a-z]+)\b")


# Structured ``Expect:`` line. Recommended for new proposals — gives the
# §12.2 audit a parseable signal instead of relying on prose heuristics.
# Format:
#   Expect: <kind>[:<target>] <direction>
# Examples:
#   Expect: error_events drops
#   Expect: tool_calls:memory_query rises
#   Expect: events:saga_synthesis_skipped_boundary drops
_EXPECT_LINE_RE = re.compile(
    r"^expect:\s*([a-z_]+)(?::([A-Za-z0-9_]+))?\s+"
    r"(drop|drops|fall|falls|down|decrease|decreases|fewer|less|"
    r"rise|rises|up|increase|increases|more)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_expect_line(text: str) -> tuple[str, str | None]:
    """Parse an ``Expect: <kind>[:<target>] <direction>`` line.

    Returns ``(kind, target)`` or ``("unknown", None)``. Direction
    parses successfully but isn't returned — we measure the actual
    delta and let the reader compare against the prediction's English.
    """
    m = _EXPECT_LINE_RE.search(text)
    if not m:
        return ("unknown", None)
    return (m.group(1).lower(), m.group(2))


def _signal_kind_from_predicted(text: str) -> tuple[str, str | None]:
    """Map ``Predicted effect:`` text to (kind, target). Kinds:

    - ``error_events`` — error-event count over the window
    - ``tool_calls``   — count of a named tool's invocations (target=name)
    - ``events``       — count of a named event type (target=event_type)
    - ``unknown``      — couldn't classify; audit row carries no signals

    Resolution order:
      1. Structured ``Expect: <kind>[:<target>] <direction>`` line wins
         when present (the recommended convention going forward).
      2. Prose error-rate fallback — "error rate would drop / rise /
         decrease / fewer / less / more / up / down" with "error".
      3. Prose tool-frequency fallback — restricted to known tool-name
         shapes (snake_case or known single-word PascalCase). The
         original any-CamelCase match produced false positives on
         English words like "Adding" / "Future" / "Unblocks".
    """
    kind, target = _parse_expect_line(text)
    if kind != "unknown":
        return (kind, target)

    t = text.lower()
    if "error" in t and any(w in t for w in (
        "drop", "fall", "decrease", "fewer", "less", "down",
        "rise", "increase", "more", "up",
    )):
        return ("error_events", None)

    if any(w in t for w in ("tool", "skill", "invoke", "call")):
        for m in _TOOL_NAME_RE.finditer(text):
            name = m.group(1)
            if "_" in name or name in _KNOWN_PASCAL_TOOLS:
                return ("tool_calls", name)
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
    if kind == "error_events":
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
    elif kind == "tool_calls" and target:
        before = _count_tool_calls_in_window(
            turns_log, start=before_start, end=before_end, tool_name=target,
        )
        after = _count_tool_calls_in_window(
            turns_log, start=after_start, end=after_end, tool_name=target,
        )
        # Drop 0→0 — usually a sign the prose heuristic matched a word
        # that isn't actually a tool name (defense in depth on top of
        # the allowlist in ``_TOOL_NAME_RE``).
        if before > 0 or after > 0:
            out.append(Signal(
                name=f"tool_calls:{target}", before=before, after=after,
                unit="count",
            ))
    elif kind == "events" and target:
        before = _count_events_in_window(
            events_log, start=before_start, end=before_end,
            type_filter=lambda r: r.get("type") == target,
        )
        after = _count_events_in_window(
            events_log, start=after_start, end=after_end,
            type_filter=lambda r: r.get("type") == target,
        )
        out.append(Signal(
            name=f"events:{target}", before=before, after=after,
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


# ─── Scheduled-job entry point ─────────────────────────────────────────


async def run_scheduled_applied_audit(home: Path) -> None:
    """Scheduled-job callable (VSM S4-2). Closes the double-loop:
    reads ``state/applied-proposals.jsonl``, computes before/after
    signals for proposals applied 1–4 weeks ago, writes a report
    to ``state/reports/applied-audit-YYYY-MM-DD.md``, and emits
    one ``applied_audit_ok`` event per run (with row count) or
    ``applied_audit_error`` if an exception escapes.

    Runs once per month (first of the month at 08:00 UTC) so the
    1–4 week window captures the previous month's merged proposals.
    When no proposals fall in the window (common early on), the run
    still succeeds and writes an empty-window report — this confirms
    the job is firing even when there is nothing to audit.
    """
    from ..event_logger import log_event  # mimir.event_logger; avoid circular

    try:
        rows = audit_window(home)
        block = render_audit_block(rows)

        report_dir = home / "state" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        out_path = report_dir / f"applied-audit-{today}.md"

        lines: list[str] = [
            f"# Applied-proposals audit — {today}",
            "",
            f"_Window: proposals applied 1–4 weeks before {today}._",
            f"_Proposals in window: {len(rows)}_",
            "",
        ]
        if block:
            lines.append("## Effects of prior proposals")
            lines.append("")
            lines.append(block)
        else:
            lines.append("_(No proposals in window — nothing to audit.)_")

        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        await log_event(
            "applied_audit_ok",
            report_path=str(out_path),
            rows_audited=len(rows),
        )
    except Exception as exc:  # noqa: BLE001 — defensive scheduler boundary
        log.exception("applied_audit scheduled run failed")
        await log_event(
            "applied_audit_error",
            error=f"{type(exc).__name__}: {exc}",
        )
