"""Periodic due-check sweep over the commitments store.

Phase 2b. Runs on an APScheduler cron (default every 5 min — see
``Scheduler.add_commitments_due_check_job``). For each active
commitment:

- If ``now ∈ [due_window_start_unix, due_window_end_unix]`` AND status
  is still ``pending`` (never delivered) → emit ``commitment_due``
  event AND mark ``delivered`` via ``store.deliver()``. This is the
  algedonic "follow through" nudge — positive polarity, surfaces in
  the agent's feedback block until the agent acts.

- If ``now > due_window_end_unix`` AND status is ``pending``,
  ``delivered``, or ``snoozed`` → emit ``commitment_expired`` event
  AND mark ``expired`` via ``store.expire()``. Negative polarity —
  the actual miss signal.

- Commitments with no ``due_window_start_unix`` (open-ended) are
  skipped entirely — they're surfaced via the Phase 3 prompt-builder
  block, not via time-based delivery. The agent decides when to act
  on those.

- ``snoozed`` commitments: ``snoozed_until_unix`` was applied to
  ``due_window_start_unix`` at snooze time (PR #120 fix #3), so the
  same "now ≥ start" check naturally respects the snooze. No special-
  case needed here.

Idempotence: ``store.deliver()`` and ``store.expire()`` are guarded
by ``VALID_TRANSITIONS`` (PR #120 fix #1). A commitment that's already
been delivered transitions ``delivered → delivered`` (re-emit allowed,
attempts bumps) — but we gate at *this* layer on status==pending to
avoid the re-emit. An expired commitment terminates and rejects
further events. Both protections together mean the poller is safe to
run on overlapping ticks.

Why a poller and not an inline event-firing on store mutations: the
"due" condition is time-driven, not event-driven. The store doesn't
know when its commitments cross the due-window threshold; only a
periodic check can surface that transition. APScheduler is the
existing in-process scheduler in mimir, so we slot in there.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

from ..event_logger import log_event
from .models import CommitmentRecord, CommitmentStatus
from .store import CommitmentsStore

log = logging.getLogger(__name__)


@dataclass
class DueCheckResult:
    """Summary of one due-check sweep. Returned to the caller so the
    scheduler can log a single rollup event instead of one per record."""

    due_emitted: int = 0
    expired_emitted: int = 0
    snooze_pileup_emitted: int = 0
    scanned: int = 0
    skipped_no_due_window: int = 0
    skipped_not_yet_due: int = 0


# Per-commitment snooze count above which we emit a pileup alarm.
# Operator-tunable via ``MIMIR_COMMITMENTS_SNOOZE_PILEUP_THRESHOLD``
# (plumbed through Config + passed to the poller). 3 = "you've punted
# this thing 3 times already, time to either commit or dismiss."
DEFAULT_SNOOZE_PILEUP_THRESHOLD = 3


async def check_due_and_expired(
    store: CommitmentsStore,
    *,
    now_unix: float | None = None,
    snooze_pileup_threshold: int = DEFAULT_SNOOZE_PILEUP_THRESHOLD,
) -> DueCheckResult:
    """Sweep the store; emit ``commitment_due`` / ``commitment_expired``
    / ``commitment_snooze_pileup`` events as appropriate and update each
    record's lifecycle.

    ``now_unix`` defaults to ``time.time()``. Pass an explicit value for
    deterministic tests.

    Best-effort: per-record exceptions are logged and the sweep
    continues. A whole-sweep failure (store read raise) propagates so
    the scheduler-level log_event records it.
    """
    if now_unix is None:
        now_unix = time.time()

    result = DueCheckResult()
    state = store.current_state()

    for rec in state.values():
        result.scanned += 1
        if rec.is_terminal():
            continue
        # Pileup check runs independent of the due-window logic — a
        # commitment with no due_window (open-ended, Phase 3-only) can
        # still be snoozed too many times and warrant the algedonic
        # signal. Run this check FIRST so we surface the pattern
        # regardless of due-window state. First-occurrence-only dedup
        # at the algedonic layer means the per-tick re-emission only
        # surfaces one line per 24h window.
        if rec.snooze_count >= snooze_pileup_threshold:
            try:
                result.snooze_pileup_emitted += 1
                await log_event(
                    "commitment_snooze_pileup",
                    commitment_id=rec.id,
                    channel_id=rec.channel_id,
                    text=rec.text,
                    snooze_count=rec.snooze_count,
                    threshold=snooze_pileup_threshold,
                    kind=rec.kind,
                    sensitivity=rec.sensitivity,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "commitment snooze pileup emit failed for %s",
                    rec.id,
                )
        if rec.due_window_start_unix is None:
            result.skipped_no_due_window += 1
            continue

        # Expired check first — even if we'd also fire a "due" event on
        # the same tick (rare: commitment due window fully elapsed
        # between two poll ticks), the expire is the load-bearing
        # signal. Mark expired and skip the due emit.
        end = rec.due_window_end_unix
        if end is not None and now_unix > end:
            try:
                ok = await store.expire(rec.id)
                if ok:
                    result.expired_emitted += 1
                    await log_event(
                        "commitment_expired",
                        commitment_id=rec.id,
                        channel_id=rec.channel_id,
                        text=rec.text,
                        recipient_identity=rec.recipient_identity,
                        due_window_end_unix=end,
                        kind=rec.kind,
                        sensitivity=rec.sensitivity,
                    )
            except Exception:  # noqa: BLE001
                log.exception(
                    "commitment expire failed for %s; continuing sweep",
                    rec.id,
                )
            continue

        # Due check: must be in the window AND not yet delivered. A
        # record already in ``delivered`` status has been surfaced
        # once; re-emitting on every poll tick would crowd the
        # algedonic block. The agent acts on it via complete/snooze
        # and the next sweep finds it terminal or in a new snooze
        # window.
        #
        # SNOOZED records ARE eligible — snooze slides
        # due_window_start_unix forward (PR #120 fix #3), so once
        # the new window opens, we deliver again. That's the point
        # of snooze: "remind me later."
        if rec.status == CommitmentStatus.DELIVERED.value:
            continue
        if now_unix < rec.due_window_start_unix:
            result.skipped_not_yet_due += 1
            continue

        try:
            ok = await store.deliver(rec.id)
            if ok:
                result.due_emitted += 1
                await log_event(
                    "commitment_due",
                    commitment_id=rec.id,
                    channel_id=rec.channel_id,
                    text=rec.text,
                    recipient_identity=rec.recipient_identity,
                    suggested_reminder=rec.suggested_reminder,
                    due_window_start_unix=rec.due_window_start_unix,
                    due_window_end_unix=rec.due_window_end_unix,
                    kind=rec.kind,
                    sensitivity=rec.sensitivity,
                )
        except Exception:  # noqa: BLE001
            log.exception(
                "commitment deliver failed for %s; continuing sweep",
                rec.id,
            )

    return result
