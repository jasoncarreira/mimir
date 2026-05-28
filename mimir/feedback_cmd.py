"""CLI helpers for ``mimir feedback`` subcommands (chainlink #198).

Currently ships:
- ``mark-resolved`` — writer side of resolved-incidents.jsonl (chainlink #198).
- ``emit`` — write a structured event to events.jsonl from a subprocess
  (chainlink #218).  Useful for Bash-side skill code that wants to emit
  auditable events without touching Python internals or requiring an in-process
  logger singleton.

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


# ---------------------------------------------------------------------------
# ``mimir feedback emit`` -- write a structured event from a subprocess
# (chainlink #218)
# ---------------------------------------------------------------------------


def _parse_kv_pairs(
    pairs: list[str],
    json_values: bool = False,
) -> tuple[dict, str | None]:
    """Parse ``KEY=VALUE`` strings into a dict.

    Returns ``(payload_dict, error_message)`` -- ``error_message`` is None
    on success and a human-readable string on parse failure.

    When *json_values* is ``True``, each value string is passed through
    ``json.loads()``.  A malformed JSON value is **rejected** with an error
    message (the caller explicitly opted in, so silent fall-back would hide
    bugs).  Without the flag values are stored as plain strings, which is the
    backwards-compatible default.
    """
    payload: dict = {}
    for pair in pairs:
        if "=" not in pair:
            return {}, f"key-value pair must use '=' separator: {pair!r}"
        k, v = pair.split("=", 1)
        k = k.strip()
        if not k:
            return {}, f"empty key in pair: {pair!r}"
        if json_values:
            try:
                v = json.loads(v)  # type: ignore[assignment]
            except json.JSONDecodeError as exc:
                return {}, (
                    f"--json-values: value for key {k!r} is not valid JSON "
                    f"({exc}); got {v!r}"
                )
        payload[k] = v
    return payload, None


def run_emit_event(
    home: Path,
    event_type: str,
    pairs: list[str],
    json_values: bool = False,
) -> int:
    """Implement ``mimir feedback emit <event_type> [KEY=VALUE ...]``.

    Appends one structured event record to ``<home>/logs/events.jsonl`` using
    a standalone ``EventLogger`` (no in-process singleton required).  The
    event is indistinguishable from a server-emitted event -- same JSON
    schema, same ``timestamp`` and ``session_id`` fields.

    ``session_id`` is set to ``"cli"`` to signal the event came from a
    subprocess rather than a live turn.

    Returns an integer exit code (0 = success, 1 = error).
    """
    # -- Parse KEY=VALUE pairs -----------------------------------------------
    payload, parse_err = _parse_kv_pairs(pairs, json_values=json_values)
    if parse_err is not None:
        print(f"error: {parse_err}", file=sys.stderr)
        return 1

    # -- Advisory type check -------------------------------------------------
    if event_type not in _known_event_types():
        print(
            f"warning: event type {event_type!r} not in known _EVENT_RULES keys -- "
            f"the event will still be written (unlisted types are silently ignored "
            f"by FeedbackLog.recent but ARE stored in events.jsonl).  "
            f"Known types: {', '.join(sorted(_known_event_types()))}",
            file=sys.stderr,
        )

    # -- Write the event -----------------------------------------------------
    from .event_logger import EventLogger  # noqa: PLC0415

    events_path = home / "logs" / "events.jsonl"
    logger = EventLogger(path=events_path, session_id="cli")
    logger.log_sync(event_type, **payload)

    # -- Confirm -------------------------------------------------------------
    print(f"emitted: {event_type!r} -> {events_path}")
    for k, v in payload.items():
        print(f"  {k} = {v!r}")
    return 0
