"""Append access_events and update the per-atom summary cache.

The lowest-level building block in mimir.saga. Both ``store()`` (which
fires a 'store' event) and ``recall()`` (which fires 'retrieval' events
for returned atoms) call into here. ``reflect()`` fires 'consolidation'
events. The agent's explicit ``mark_contributions()`` fires
'feedback_positive' events.

Contract:

1. Atomic per-call: all events in one batch commit together, OR none do.
   We never want a half-applied batch where some atoms see their summary
   updated and others don't — the activation read path would return
   inconsistent values.

2. Idempotent per-event: the events table is append-only. Replaying a
   batch would create duplicate rows (bad), so callers must not retry
   on success. On exception before commit, nothing landed.

3. Summary maintenance: atom_access_summary is denormalized for read
   speed. Every event_insert also updates the summary's recent-K + old
   aggregate. The activation read path trusts the summary; if it's stale,
   activations are wrong (but bounded — the summary is invariant under
   incremental updates, see test_activation::test_summary_invariant_under_incremental_updates).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .activation import RECENT_K, SOURCE_WEIGHTS, update_summary_on_access


@dataclass(frozen=True)
class AccessEvent:
    """One access event to log. Caller assembles; mark_access persists."""
    atom_id: str
    source: str                      # 'store' | 'retrieval' | 'feedback_positive' | 'consolidation' | 'pinned_init'
    weight: float | None = None      # if None, looked up from SOURCE_WEIGHTS
    session_id: str | None = None
    metadata: dict | None = None     # extras (e.g., retrieval mode, contribution role)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_weight(source: str, override: float | None) -> float:
    """Source tag → weight, with explicit override allowed.

    Override is used by the migration importer (which carries forward
    historical contributed-flag→feedback_positive translation).
    """
    if override is not None:
        return override
    return SOURCE_WEIGHTS.get(source, 1.0)


def mark_access(
    conn: sqlite3.Connection,
    events: Iterable[AccessEvent],
    *,
    now: "datetime | str | None" = None,
) -> int:
    """Persist one or more access events as a batch of statements.

    DOES NOT manage transactions. Caller wraps in `with conn:` or
    explicit BEGIN/COMMIT. This lets reflect/store/etc. control the
    transaction boundary at the operation level rather than having
    it buried per-helper. (Earlier sketch versions opened BEGIN
    IMMEDIATE here, which collided with callers' implicit
    transactions and produced "cannot start a transaction within a
    transaction" errors.)

    Returns the number of events written. Caller decides what counts
    as an event (no dedupe).

    Each event:
      1. Inserts one row into access_events
      2. Updates atom_access_summary for the affected atom

    ``now`` (chainlink #236): timestamp to use for the events. Defaults
    to wall clock when None — the normal production case. Bench replays
    pass an explicit datetime so historical-corpus runs (LongMemEval-S
    in 2023 replayed under a 2026 wall clock) write access_events with
    the corpus's epoch rather than the bench-run clock. Mirrors the
    ``reference_date`` plumbing in ``forget.py``.
    """
    events_list = list(events)
    if not events_list:
        return 0

    # Resolve the timestamp string. Accept datetime, ISO string, or None.
    if now is None:
        now_iso = _utc_now_iso()
    elif isinstance(now, datetime):
        now_iso = now.isoformat()
    else:
        now_iso = str(now)

    # Group events by atom_id so we update each summary once per atom
    # even when one atom has multiple events in the batch.
    by_atom: dict[str, list[AccessEvent]] = {}
    for ev in events_list:
        by_atom.setdefault(ev.atom_id, []).append(ev)

    # Insert all events first (preserves caller-provided order).
    rows = []
    for ev in events_list:
        weight = _resolve_weight(ev.source, ev.weight)
        metadata_json = json.dumps(ev.metadata) if ev.metadata else "{}"
        rows.append((
            ev.atom_id, now_iso, ev.source, weight,
            ev.session_id, metadata_json,
        ))
    conn.executemany(
        "INSERT INTO access_events "
        "(atom_id, ts, source, weight, session_id, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )

    # Update summaries — one read+write per affected atom.
    for atom_id, atom_events in by_atom.items():
        current = conn.execute(
            "SELECT recent_ts_json, recent_weights_json, "
            "old_count, old_weight_sum, old_oldest_ts "
            "FROM atom_access_summary WHERE atom_id = ?",
            (atom_id,),
        ).fetchone()

        if current is None:
            summary = None
        else:
            summary = {
                "recent_ts_json": current[0],
                "recent_weights_json": current[1],
                "old_count": current[2],
                "old_weight_sum": current[3],
                "old_oldest_ts": current[4],
            }

        for ev in atom_events:
            weight = _resolve_weight(ev.source, ev.weight)
            summary = update_summary_on_access(
                current_summary=summary,
                new_ts=now_iso,
                new_weight=weight,
                recent_k=RECENT_K,
            )

        conn.execute(
            "INSERT OR REPLACE INTO atom_access_summary "
            "(atom_id, recent_ts_json, recent_weights_json, "
            "old_count, old_weight_sum, old_oldest_ts, last_updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                atom_id,
                summary["recent_ts_json"],
                summary["recent_weights_json"],
                summary["old_count"],
                summary["old_weight_sum"],
                summary["old_oldest_ts"],
                now_iso,
            ),
        )

    return len(events_list)
