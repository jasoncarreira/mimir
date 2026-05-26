"""CLI helpers for ``mimir feedback`` subcommands (chainlink #198).

Currently ships: ``mark-resolved`` — writer side of resolved-incidents.jsonl.

The consumer side (filtering in FeedbackLog.recent) was shipped in PR #372
(chainlink #197).  This module provides the ergonomic writer so operators
don't have to hand-craft JSONL lines and risk timestamp format issues.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Known event types (for advisory --type validation)
# ---------------------------------------------------------------------------


def _known_event_types() -> frozenset[str]:
    """Derive valid event types from the canonical _EVENT_RULES dict.

    Lazy import so the CLI layer doesn't pull in all of feedback.py at
    module load time. Single source of truth: adding a new rule in
    feedback._EVENT_RULES automatically updates the advisory validator.
    """
    from .feedback import _EVENT_RULES  # noqa: PLC0415

    return frozenset(_EVENT_RULES.keys())


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _count_filtered_events_in_window(
    home: Path, rule: dict, hours: int = 24
) -> int:
    """Count events in the most recent ``hours``-hour window that would be
    filtered by ``rule`` via ``_is_event_resolved``.

    ``tail_jsonl_records`` yields newest-first; iteration stops as soon as
    the timestamp falls below the cutoff, so memory use is O(window) not
    O(file).
    """
    from .feedback import _is_event_resolved
    from ._jsonl_tail import tail_jsonl_records
    from datetime import timedelta

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    events_path = home / "logs" / "events.jsonl"
    count = 0
    if events_path.exists():
        for ev in tail_jsonl_records(events_path):
            ts_str = ev.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                break
            if _is_event_resolved(ev, [rule]):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_mark_resolved(
    home: Path,
    event_type: str,
    pattern: str,
    reason: str,
    resolved_at: str | None,
    dry_run: bool,
) -> int:
    """Implement ``mimir feedback mark-resolved``.

    Writes one JSON line to ``<home>/resolved-incidents.jsonl``.  On
    ``--dry-run`` prints how many events in the current events.jsonl tail
    would be filtered, without touching the file.

    Returns an integer exit code (0 = success, 1 = error).
    """
    # ── Resolve / default resolved_at ──────────────────────────────────────
    if resolved_at is not None:
        # Validate operator-supplied stamp — parse it now so we catch bad
        # format before writing to disk.
        try:
            dt = datetime.fromisoformat(resolved_at)
        except ValueError as exc:
            print(
                f"error: --resolved-at is not a valid ISO-8601 timestamp: {exc}",
                file=sys.stderr,
            )
            return 1
        if dt.tzinfo is None:
            # Attach UTC so the stored stamp is unambiguous.
            dt = dt.replace(tzinfo=timezone.utc)
        resolved_at_final = dt.isoformat()
    else:
        resolved_at_final = datetime.now(tz=timezone.utc).isoformat()

    # ── Advisory type check ─────────────────────────────────────────────────
    if event_type != "*" and event_type not in _known_event_types():
        print(
            f"warning: event type '{event_type}' not in known _EVENT_RULES keys — "
            f"the rule will still be written (unknown types are allowed), but check "
            f"for typos.  Known types: {', '.join(sorted(_known_event_types()))}",
            file=sys.stderr,
        )

    # ── Build the rule dict ──────────────────────────────────────────────────
    rule: dict = {
        "event_type": event_type,
        "pattern": pattern,
        "resolved_at": resolved_at_final,
        "reason": reason,
    }

    # ── Dry run: count matching events in the current 24h window ────────────
    if dry_run:
        would_filter = _count_filtered_events_in_window(home, rule)
        print(
            f"dry-run: rule would filter {would_filter} event(s) from the "
            f"current 24h window (not written)."
        )
        print(f"  event_type = {event_type!r}")
        print(f"  pattern    = {pattern!r}")
        print(f"  resolved_at= {resolved_at_final}")
        return 0

    # ── Write the rule ───────────────────────────────────────────────────────
    incidents_path = home / "resolved-incidents.jsonl"
    try:
        with incidents_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rule) + "\n")
    except OSError as exc:
        print(f"error: could not write to {incidents_path}: {exc}", file=sys.stderr)
        return 1

    # ── Confirm ─────────────────────────────────────────────────────────────
    would_filter = _count_filtered_events_in_window(home, rule)
    print(
        f"marked resolved: event_type={event_type!r} pattern={pattern!r} "
        f"resolved_at={resolved_at_final}"
    )
    print(
        f"  {would_filter} event(s) in the current 24h window now filtered."
    )
    print(f"  written to: {incidents_path}")
    return 0
