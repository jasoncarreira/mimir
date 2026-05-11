"""Append-only JSONL store for commitments + replay-to-state.

Lives at ``<home>/.mimir/commitments.jsonl`` (alongside
``session_boundaries.jsonl``) so the indexer doesn't walk it as
"knowledge" content.

Trim policy is **status-aware**, not tail-bounded by line count:
records whose current status is ``completed | dismissed | expired``
AND whose terminal event is older than ``terminal_retention_days``
(default 30) get dropped on ``trim()``. Active records
(``pending | delivered | snoozed``) live forever — that's how a
60-day commitment survives across multiple trim cycles.

Concurrency: an ``asyncio.Lock`` serializes appends. The current-
state replay is read-only and uses a streaming tail-friendly reader
(``tail_jsonl_records``); replays don't hold the lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    DEFAULT_SNOOZE_WINDOW_SECS,
    EVENT_TO_TARGET_STATUS,
    VALID_TRANSITIONS,
    CommitmentRecord,
    CommitmentStatus,
    make_commitment_id,
    make_dedupe_key,
)

log = logging.getLogger(__name__)


# Number of days terminal records are retained before ``trim()`` drops them.
DEFAULT_TERMINAL_RETENTION_DAYS = 30


#: Current schema version for every event appended to the JSONL.
#: Stamped on every appended event under the ``v`` key so future
#: schema changes (field rename, required-field add, event-type
#: split) can be implemented as a versioned replay path without
#: breaking deployments that already have events on disk.
#:
#: Replay treatment (see :meth:`CommitmentsStore._apply_event`):
#:
#: - ``v`` absent → treated as v1 (legacy events appended before
#:   chainlink #82 sub #86 landed). The current event shape IS v1,
#:   so this is the safe default.
#: - ``v == 1`` → current shape; replay as-is.
#: - ``v > 1`` → unknown major; log a warning and skip the event.
#:   The next agent build that knows about that version will replay
#:   it correctly.
#:
#: When evolving the schema:
#:
#: 1. Bump ``COMMITMENTS_JSONL_SCHEMA_VERSION``.
#: 2. Add per-version handling in ``_apply_event`` (or a dispatch
#:    table keyed on ``v``).
#: 3. Land a migration test that asserts the new version replays
#:    correctly AND that old (v < new) events still replay.
COMMITMENTS_JSONL_SCHEMA_VERSION = 1


@dataclass
class CommitmentsStore:
    """Owns ``<path>`` — typically ``<home>/.mimir/commitments.jsonl``.

    Each line is one lifecycle event. The ``commitment_added`` event
    carries the full initial record under a ``record`` key; lifecycle
    events (``_delivered`` / ``_completed`` / ``_snoozed`` / ...)
    carry just the commitment id + delta fields.
    """

    path: Path
    terminal_retention_days: int = DEFAULT_TERMINAL_RETENTION_DAYS

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    # ─── Appenders ──────────────────────────────────────────────────

    async def _append(self, event: dict[str, Any]) -> None:
        # Stamp the schema version (chainlink #82 sub #86): every
        # appended event carries ``"v": COMMITMENTS_JSONL_SCHEMA_VERSION``
        # so a future schema change can be implemented as a versioned
        # replay path. Callers must not pre-set ``v`` — _append is the
        # single source of truth for the current version. Defensive
        # overwrite handles a stray copy-paste in the lifecycle methods.
        event = {**event, "v": COMMITMENTS_JSONL_SCHEMA_VERSION}
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=True, default=str) + "\n")

    async def add(self, record: CommitmentRecord) -> CommitmentRecord:
        """Append ``commitment_added`` with the full initial record.

        Caller can pre-populate ``id`` (idempotent re-extraction) or
        let it default; ``created_at_unix`` defaults to now when 0.0.
        The ``dedupe_key`` is auto-filled from
        (channel_id, text, due_window_start_unix) when empty.
        """
        if not record.id:
            record.id = make_commitment_id()
        if not record.created_at_unix:
            record.created_at_unix = time.time()
        if not record.dedupe_key:
            record.dedupe_key = make_dedupe_key(
                channel_id=record.channel_id,
                text=record.text,
                due_window_start_unix=record.due_window_start_unix,
                recipient_identity=record.recipient_identity,
            )
        # Ensure starting status is pending — caller setting status to
        # anything else on add() is a contract violation; we coerce
        # so a stray copy-paste mistake doesn't poison the store.
        record.status = CommitmentStatus.PENDING.value
        await self._append({
            "type": "commitment_added",
            "ts_unix": time.time(),
            "id": record.id,
            "record": record.to_dict(),
        })
        return record

    def _can_apply(self, id: str, target_status: str) -> bool:
        """Pre-write transition check. PR #120 re-review N2.

        Replay's ``VALID_TRANSITIONS`` guard already protects state
        correctness — an invalid lifecycle event is rejected on read.
        But without a pre-write check, the JSONL accumulates no-op
        events and the CLI gives no feedback when an operator does
        ``commitments complete <id>`` against an already-terminal
        record. This helper reads current state once, consults the
        adjacency, and returns False (with a warning log) on rejection.
        Public lifecycle methods consult it before appending.

        Returns True if the transition is valid; False if the record
        is unknown or the transition violates ``VALID_TRANSITIONS``.

        Note on TOCTOU: a concurrent writer can land an event between
        this check and the append. Replay's invariant still holds —
        the append is rejected at read time. So the worst case under
        a race is one stray no-op event in the JSONL; state stays
        correct. The simple read-then-append shape is fine for Phase 1.
        """
        state = self.current_state()
        rec = state.get(id)
        if rec is None:
            log.warning(
                "commitments: lifecycle write rejected — %s not found "
                "(target %s)",
                id, target_status,
            )
            return False
        allowed = VALID_TRANSITIONS.get(rec.status, frozenset())
        if target_status not in allowed:
            log.warning(
                "commitments: lifecycle write rejected — invalid "
                "transition %s → %s for %s",
                rec.status, target_status, id,
            )
            return False
        return True

    async def deliver(self, id: str) -> bool:
        """Mark delivered (reminder fired). Bumps ``attempts``.

        Returns True if the event was appended; False if the
        transition was rejected (unknown id, terminal record)."""
        if not self._can_apply(id, CommitmentStatus.DELIVERED.value):
            return False
        await self._append({
            "type": "commitment_delivered",
            "ts_unix": time.time(),
            "id": id,
            "at_unix": time.time(),
        })
        return True

    async def complete(
        self, id: str, *, message_id: str | None = None,
    ) -> bool:
        """Mark completed (agent followed through). Terminal.

        Returns True if the event was appended; False if the
        transition was rejected."""
        if not self._can_apply(id, CommitmentStatus.COMPLETED.value):
            return False
        await self._append({
            "type": "commitment_completed",
            "ts_unix": time.time(),
            "id": id,
            "at_unix": time.time(),
            "message_id": message_id,
        })
        return True

    async def snooze(
        self,
        id: str,
        *,
        until_unix: float,
        reason: str | None = None,
    ) -> bool:
        """Push out to a later time. ``until_unix`` becomes the new
        ``due_window_start_unix`` after replay (the original end stays
        unless explicitly re-snoozed past it).

        Returns True if the event was appended; False if the
        transition was rejected."""
        if not self._can_apply(id, CommitmentStatus.SNOOZED.value):
            return False
        await self._append({
            "type": "commitment_snoozed",
            "ts_unix": time.time(),
            "id": id,
            "until_unix": until_unix,
            "reason": reason,
        })
        return True

    async def dismiss(
        self, id: str, *, reason: str | None = None,
    ) -> bool:
        """Drop as no longer relevant. Terminal.

        Returns True if the event was appended; False if the
        transition was rejected."""
        if not self._can_apply(id, CommitmentStatus.DISMISSED.value):
            return False
        await self._append({
            "type": "commitment_dismissed",
            "ts_unix": time.time(),
            "id": id,
            "at_unix": time.time(),
            "reason": reason,
        })
        return True

    async def expire(self, id: str) -> bool:
        """Mark expired (due_window_end passed without resolution).
        Typically called from a poller, not by the agent. Terminal.

        Returns True if the event was appended; False if the
        transition was rejected."""
        if not self._can_apply(id, CommitmentStatus.EXPIRED.value):
            return False
        await self._append({
            "type": "commitment_expired",
            "ts_unix": time.time(),
            "id": id,
            "at_unix": time.time(),
        })
        return True

    async def alarm_pileup(self, id: str) -> bool:
        """Record that a ``commitment_snooze_pileup`` algedonic event
        was emitted for this record. PR #126 review #2: bumps
        ``pileup_alarmed_at_unix`` so the poller's 24h cooldown
        suppresses re-emission. Idempotent — calling again on an
        already-alarmed record just updates the timestamp.

        Annotational, not a status transition: ``VALID_TRANSITIONS``
        isn't consulted; the record's status is untouched. Returns
        True if appended, False on unknown id."""
        if id not in self.current_state():
            log.warning(
                "commitments: alarm_pileup for unknown id %s", id,
            )
            return False
        await self._append({
            "type": "commitment_pileup_alarmed",
            "ts_unix": time.time(),
            "id": id,
            "at_unix": time.time(),
        })
        return True

    # ─── Replay-to-state ────────────────────────────────────────────

    def current_state(self) -> dict[str, CommitmentRecord]:
        """Read the JSONL, apply all events in order, return
        ``id → CommitmentRecord``. Missing file → empty dict."""
        records: dict[str, CommitmentRecord] = {}
        if not self.path.exists():
            return records
        # JSONL is append-chronological; we read full-file (small,
        # bounded by trim policy). tail-streaming the FULL file via
        # ``tail_jsonl_records`` reverses order; use a forward scan.
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("commitments: skipping bad jsonl line")
                    continue
                self._apply_event(records, event)
        return records

    @staticmethod
    def _apply_event(
        records: dict[str, CommitmentRecord], event: dict,
    ) -> None:
        # Schema version gate (chainlink #82 sub #86).
        #
        # - Absent ``v`` field → legacy event appended before the
        #   version stamp landed; treat as v1 (current shape).
        # - ``v`` present and known → replay as-is (current code path).
        # - ``v`` greater than the version this build knows → log a
        #   warning and skip. The next build that recognizes that
        #   version will replay it correctly. We deliberately do NOT
        #   silently down-grade — that risks lossy replay of events
        #   the new schema considers load-bearing.
        v = event.get("v", COMMITMENTS_JSONL_SCHEMA_VERSION)
        try:
            v_int = int(v)
        except (TypeError, ValueError):
            log.warning(
                "commitments: skipping event with malformed v=%r", v,
            )
            return
        if v_int > COMMITMENTS_JSONL_SCHEMA_VERSION:
            log.warning(
                "commitments: skipping event with future schema "
                "v=%d (this build understands up to v=%d). Upgrade "
                "to a build that knows this version to replay.",
                v_int, COMMITMENTS_JSONL_SCHEMA_VERSION,
            )
            return
        if v_int < 1:
            # PR #137 review: ``int(0)`` / ``int(-1)`` succeed and would
            # otherwise silently fall through to the v1 replay path.
            # No legitimate writer produces these — flag them loud
            # rather than silently degrading. Rounds out the defensive
            # gate alongside the malformed and future-version arms.
            log.warning(
                "commitments: skipping event with non-positive schema "
                "v=%d (versions start at 1)", v_int,
            )
            return
        et = event.get("type")
        rid = event.get("id")
        if not et or not rid:
            return
        if et == "commitment_added":
            # PR #120 review finding #2: a duplicate ``commitment_added``
            # for an id already in records would wipe in-progress state
            # (status, attempts, delivered_at) with the re-added baseline.
            # First-write-wins — log + skip the duplicate.
            if rid in records:
                log.warning(
                    "commitments: duplicate commitment_added for %s, "
                    "keeping first (status=%s)",
                    rid, records[rid].status,
                )
                return
            rec_data = event.get("record") or {}
            try:
                records[rid] = CommitmentRecord(**rec_data)
            except TypeError as exc:
                log.warning(
                    "commitments: malformed commitment_added skipped: %s",
                    exc,
                )
            return
        rec = records.get(rid)
        if rec is None:
            # Lifecycle event for unknown id — log + skip (the add
            # event may have been trimmed; ignoring is safer than
            # crashing the replay).
            log.debug("commitments: lifecycle event for unknown id %s", rid)
            return
        # PR #126 review #2: annotational events (no status change,
        # just field update). Handle BEFORE the VALID_TRANSITIONS
        # guard since they don't model a transition. Currently just
        # ``commitment_pileup_alarmed`` — fires the 24h cooldown
        # bookkeeping for the snooze-pileup poller.
        if et == "commitment_pileup_alarmed":
            rec.pileup_alarmed_at_unix = event.get("at_unix")
            return
        # PR #120 review finding #1: reject transitions not in the
        # ``VALID_TRANSITIONS`` adjacency. The common case this
        # defends against: a late ``commitment_expired`` from the
        # Phase 2 expire-poller arriving after the agent has already
        # ``commitment_completed`` the same id — without the guard
        # the status flips and ``completion_message_id`` becomes a
        # lie. Terminal records reject everything; the lifecycle
        # invariant lives here in code, not just in prose.
        target_status = EVENT_TO_TARGET_STATUS.get(et)
        if target_status is None:
            log.debug("commitments: unknown lifecycle event type %r", et)
            return
        allowed = VALID_TRANSITIONS.get(rec.status, frozenset())
        if target_status not in allowed:
            log.warning(
                "commitments: invalid transition %s → %s for %s; "
                "skipping (event type %s)",
                rec.status, target_status, rid, et,
            )
            return
        if et == "commitment_delivered":
            rec.status = CommitmentStatus.DELIVERED.value
            rec.delivered_at_unix = event.get("at_unix")
            rec.attempts += 1
        elif et == "commitment_completed":
            rec.status = CommitmentStatus.COMPLETED.value
            rec.completed_at_unix = event.get("at_unix")
            rec.completion_message_id = event.get("message_id")
        elif et == "commitment_snoozed":
            rec.status = CommitmentStatus.SNOOZED.value
            rec.snoozed_until_unix = event.get("until_unix")
            rec.snooze_reason = event.get("reason")
            # Phase 2b: bump per-record snooze counter. The poller
            # uses this to detect commitments that keep getting
            # punted (≥ threshold → ``commitment_snooze_pileup``).
            rec.snooze_count += 1
            # Slide the due window so the snoozed_until becomes the
            # new "earliest deliver" anchor for surfacing logic.
            # PR #120 review finding #3: also bump the end so a
            # long snooze past the current end doesn't produce an
            # inverted window (start > end). Match the CLI's
            # default-end shape (start + 7d).
            if event.get("until_unix") is not None:
                rec.due_window_start_unix = event["until_unix"]
                current_end = rec.due_window_end_unix or 0
                min_end = event["until_unix"] + DEFAULT_SNOOZE_WINDOW_SECS
                if current_end < min_end:
                    rec.due_window_end_unix = min_end
        elif et == "commitment_dismissed":
            rec.status = CommitmentStatus.DISMISSED.value
            rec.dismissed_at_unix = event.get("at_unix")
            rec.dismiss_reason = event.get("reason")
        elif et == "commitment_expired":
            rec.status = CommitmentStatus.EXPIRED.value
            rec.expired_at_unix = event.get("at_unix")

    def list(
        self,
        *,
        channel_id: str | None = None,
        status: str | None = None,
        include_unbound: bool = True,
    ) -> list[CommitmentRecord]:
        """Replay + filter.

        - ``channel_id``: only commitments bound to this channel
          (``record.channel_id == channel_id``). Combined with
          ``include_unbound=True`` (default), unbound commitments
          (``channel_id is None``) are also returned — matches the
          design's "surface unbound everywhere" rule.
        - ``status``: filter by current status. ``None`` returns all.
        Sorted by created_at_unix ascending."""
        state = self.current_state()
        out: list[CommitmentRecord] = []
        for rec in state.values():
            if channel_id is not None:
                ch_ok = rec.channel_id == channel_id
                if not ch_ok and not (include_unbound and rec.channel_id is None):
                    continue
            if status is not None and rec.status != status:
                continue
            out.append(rec)
        out.sort(key=lambda r: r.created_at_unix)
        return out

    def find_by_dedupe_key(
        self, dedupe_key: str,
    ) -> CommitmentRecord | None:
        """Return the (non-terminal) commitment with this dedupe key,
        or None. Used by the future extractor to skip re-adding the
        same commitment surfaced in a later session."""
        state = self.current_state()
        for rec in state.values():
            if rec.dedupe_key == dedupe_key and not rec.is_terminal():
                return rec
        return None

    # ─── Trim ───────────────────────────────────────────────────────

    def find_trim_candidates(
        self, *, now_unix: float | None = None,
    ) -> list[tuple[str, CommitmentRecord]]:
        """Return ``(id, record)`` pairs for terminal records whose
        terminal event is older than ``terminal_retention_days``.

        Single source of truth for the trim predicate. Used by both
        ``trim()`` (to determine which ids to drop) and the CLI's
        dry-run path (to preview what would be dropped). PR #120
        re-review N1: keeping the definition here avoids drift if a
        future change adds a new terminal status that updates only
        one of the two paths.
        """
        if now_unix is None:
            now_unix = time.time()
        retention_secs = self.terminal_retention_days * 86400
        out: list[tuple[str, CommitmentRecord]] = []
        for rid, rec in self.current_state().items():
            if not rec.is_terminal():
                continue
            terminal_at = (
                rec.completed_at_unix
                or rec.dismissed_at_unix
                or rec.expired_at_unix
                or 0.0
            )
            if (now_unix - terminal_at) > retention_secs:
                out.append((rid, rec))
        return out

    async def trim(self, *, now_unix: float | None = None) -> int:
        """Rewrite the file dropping terminal records whose terminal
        event is older than ``terminal_retention_days`` (default 30).

        Active records (``pending``, ``delivered``, ``snoozed``) are
        ALWAYS kept regardless of age — a 60-day pending commitment
        survives every trim until it terminates.

        Returns the number of records dropped. Uses atomic
        rename (temp file + os.replace) so an interrupted trim never
        leaves the store in a half-written state. Holds the lock for
        the entire operation; the file is briefly unavailable to
        appenders during the rewrite.
        """
        if not self.path.exists():
            return 0

        # First pass (no lock): identify which ids to drop via the
        # shared predicate helper. PR #120 re-review N1.
        candidates = self.find_trim_candidates(now_unix=now_unix)
        drop_ids: set[str] = {rid for rid, _ in candidates}
        if not drop_ids:
            return 0

        # Second pass (under lock): rewrite, dropping all events for
        # the chosen ids. We re-read the file under the lock to catch
        # any appends that landed between the state-read and trim.
        async with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            kept_lines = 0
            dropped_events = 0
            with self.path.open("r", encoding="utf-8") as src, \
                 tmp.open("w", encoding="utf-8") as dst:
                for line in src:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        # Pass-through unknown lines — don't drop data
                        # we can't parse.
                        dst.write(line)
                        kept_lines += 1
                        continue
                    if event.get("id") in drop_ids:
                        dropped_events += 1
                        continue
                    dst.write(line if line.endswith("\n") else line + "\n")
                    kept_lines += 1
                # PR #120 review finding #4a: flush + fsync the tmp
                # file's contents to disk BEFORE the atomic rename.
                # ``os.replace`` is atomic w.r.t. the directory entry,
                # but if the host crashes after rename, the tmp file's
                # contents may not be fully on disk yet — the original
                # claim "interrupted trim never leaves the store
                # half-written" needs this to hold. Page-cache → disk.
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp, self.path)
        log.info(
            "commitments trim: dropped %d records (%d events), kept %d lines",
            len(drop_ids), dropped_events, kept_lines,
        )
        return len(drop_ids)
