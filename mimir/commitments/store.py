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

    async def deliver(self, id: str) -> None:
        """Mark delivered (reminder fired). Bumps ``attempts``."""
        await self._append({
            "type": "commitment_delivered",
            "ts_unix": time.time(),
            "id": id,
            "at_unix": time.time(),
        })

    async def complete(
        self, id: str, *, message_id: str | None = None,
    ) -> None:
        """Mark completed (agent followed through). Terminal."""
        await self._append({
            "type": "commitment_completed",
            "ts_unix": time.time(),
            "id": id,
            "at_unix": time.time(),
            "message_id": message_id,
        })

    async def snooze(
        self,
        id: str,
        *,
        until_unix: float,
        reason: str | None = None,
    ) -> None:
        """Push out to a later time. ``until_unix`` becomes the new
        ``due_window_start_unix`` after replay (the original end stays
        unless explicitly re-snoozed past it)."""
        await self._append({
            "type": "commitment_snoozed",
            "ts_unix": time.time(),
            "id": id,
            "until_unix": until_unix,
            "reason": reason,
        })

    async def dismiss(
        self, id: str, *, reason: str | None = None,
    ) -> None:
        """Drop as no longer relevant. Terminal."""
        await self._append({
            "type": "commitment_dismissed",
            "ts_unix": time.time(),
            "id": id,
            "at_unix": time.time(),
            "reason": reason,
        })

    async def expire(self, id: str) -> None:
        """Mark expired (due_window_end passed without resolution).
        Typically called from a poller, not by the agent. Terminal."""
        await self._append({
            "type": "commitment_expired",
            "ts_unix": time.time(),
            "id": id,
            "at_unix": time.time(),
        })

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
        if now_unix is None:
            now_unix = time.time()
        retention_secs = self.terminal_retention_days * 86400

        # First pass (no lock): identify which ids to drop.
        state = self.current_state()
        drop_ids: set[str] = set()
        for rid, rec in state.items():
            if not rec.is_terminal():
                continue
            terminal_at = (
                rec.completed_at_unix
                or rec.dismissed_at_unix
                or rec.expired_at_unix
                or 0.0
            )
            if (now_unix - terminal_at) > retention_secs:
                drop_ids.add(rid)
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
