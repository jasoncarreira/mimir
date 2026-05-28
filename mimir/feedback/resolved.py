"""Resolved-incident filtering (chainlink #197).

Reads resolved-incidents.jsonl and filters already-resolved events from
the algedonic block so stale noise doesn't crowd out active signals.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from pathlib import Path

# Resolved-incident filtering (chainlink #197)
# ---------------------------------------------------------------------------

def _load_resolved_incidents(path: Path) -> list[dict]:
    """Read ``resolved-incidents.jsonl`` from ``path``.

    Each line is a JSON object::

        {"event_type": "dispatcher_error", "pattern": "langchain-claude-code",
         "resolved_at": "2026-05-25T19:30:00+00:00", "reason": "start.sh fix"}

    ``event_type`` may be ``"*"`` to match any event type.
    ``pattern`` is matched as a substring of ``json.dumps(event)``; an empty
    string matches every event of the matching type.
    ``resolved_at`` is an ISO-8601 timestamp string; events timestamped
    *before* this value are suppressed.

    Returns an empty list if the file does not exist or is unreadable.
    """
    if not path.exists():
        return []
    rules: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rules.append(obj)
    except OSError:
        pass
    return rules


def _parse_resolved_ts(s: str) -> "datetime | None":
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime.

    Accepts both offset-aware (``2026-05-25T19:30:00+00:00``) and naive
    (``2026-05-25T19:30:00``) forms.  Naive stamps are assumed UTC — the
    operator-curated file follows mimir's UTC convention throughout.
    Returns ``None`` on any parse error so callers can skip bad entries
    gracefully.
    """
    from datetime import datetime, timezone  # local import — same module
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_event_resolved(ev: dict, rules: list[dict]) -> bool:
    """Return True if ``ev`` should be hidden because a resolved-incident
    rule covers it.

    A rule covers an event when ALL three conditions hold:
    1. ``rule["event_type"]`` is ``"*"`` or matches ``ev["type"]``.
    2. ``rule["pattern"]`` (if non-empty) is a substring of the JSON
       serialisation of the event.
    3. ``ev["timestamp"] < rule["resolved_at"]`` (the event predates the fix).

    Timestamps are compared as timezone-aware ``datetime`` objects rather
    than raw strings (chainlink #199) so operator-written naive stamps like
    ``2026-05-25T19:30:00`` compare correctly against the microsecond-bearing
    event timestamps in ``events.jsonl``.
    """
    ev_type = ev.get("type", "")
    ev_ts_str = ev.get("timestamp", "")
    if not isinstance(ev_ts_str, str):
        return False
    ev_dt = _parse_resolved_ts(ev_ts_str)
    if ev_dt is None:
        return False
    ev_json: str | None = None  # lazy — only serialise if needed
    for rule in rules:
        rule_type = rule.get("event_type", "*")
        rule_pattern = rule.get("pattern", "")
        rule_resolved_at = rule.get("resolved_at", "")
        if not isinstance(rule_resolved_at, str) or not rule_resolved_at:
            continue
        rule_dt = _parse_resolved_ts(rule_resolved_at)
        if rule_dt is None:
            continue
        # 1. Type match
        if rule_type != "*" and ev_type != rule_type:
            continue
        # 2. Pattern match (substring of JSON — covers nested fields)
        if rule_pattern:
            if ev_json is None:
                ev_json = json.dumps(ev)
            if rule_pattern not in ev_json:
                continue
        # 3. Timestamp: event must predate the resolution
        if ev_dt >= rule_dt:
            continue
        return True
    return False
