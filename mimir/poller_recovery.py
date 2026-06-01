"""Framework-side recovery for poller turns whose triggered turn failed
(chainlink #262).

A poller fires events; each becomes an ``AgentEvent`` → an async turn.
The poller advances its cursor at poll time, decoupled from the turn it
triggers. If that turn dies (transient model 503, timeout, plain bug),
the item is silently dropped — the cursor already moved on (#299).

The github-poller closes this for *review requests* by reconciling
against live GitHub state (``requested_reviewers``, #516). For pollers
with **no live state to reconcile against** (gmail, github
issue/comment turns) this module closes it generically via the event
log:

* At enqueue, the framework stashes the ``AgentEvent`` keyed by its
  ``source_id`` (the poller batch's stable per-fire id).
* Turn outcomes are logged with that ``source_id``
  (``turn_failed`` / ``turn_completed``, #517). Each poll cycle the
  framework reads outcomes since the last reconcile and, per stashed
  event: **drops** it if its turn completed, **re-enqueues** it
  (capped) if its turn failed, and emits a ``poller_turn_gave_up``
  signal — negative algedonic via ``feedback.classify``'s ``*_gave_up``
  rule (#515) — once the cap is hit.

Opt-in per poller via ``recover_failed_turns`` in ``pollers.json`` —
**off by default**, and OFF for github-poller (it already recovers via
#516; framework re-enqueue on top would double-fire review turns).

State lives at ``<persist_dir>/.recovery.json`` so it survives container
restarts (unlike the in-memory circuit-breaker state).

Hardening (chainlink #305/#309/#310/#318/#329, 2026-06-01 review):
* Re-enqueue respects ``enqueue()``'s accepted/False return and never
  burns a wedge-guard attempt on a re-fire that didn't happen (#305).
* The watermark advances only past FULLY-handled outcomes, to the
  outcome's own timestamp — never wall-now — so an outcome written
  between the read and the save isn't skipped (#309).
* The in-flight stash is GC'd on a TTL so turns that vanished without an
  outcome (mid-turn crash/restart) can't grow ``.recovery.json``
  forever (#310).
* State writes are atomic (#329); the give-up signal is best-effort and
  never strands the entry (#318).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from ._atomic import atomic_write_json
from ._jsonl_tail import tail_jsonl_records
from .event_logger import log_event
from .models import AgentEvent

log = logging.getLogger(__name__)

#: Per-poller recovery state file, under the poller's persist_dir.
RECOVERY_STATE_FILE = ".recovery.json"

#: Max re-enqueue attempts for a failed poller turn before giving up.
#: Mirrors github-poller's ``REVIEW_REQUEST_MAX_ATTEMPTS`` (#516) — same
#: wedge-guard intent: a persistently-failing item can't re-fire forever.
DEFAULT_MAX_RECOVERY_ATTEMPTS = 3

#: In-flight stash entries with no terminal outcome within this window are
#: GC'd (#310). Covers turns that vanished — a mid-turn crash or container
#: restart that never logged turn_failed/turn_completed — so the stash
#: can't grow unbounded. Generous: an item still unresolved after two days
#: is abandoned, not in-flight.
DEFAULT_STASH_TTL_HOURS = 48.0

_TURN_OUTCOME_TYPES = ("turn_completed", "turn_failed")

# Re-enqueue callback shape, matching ``run_poller``'s ``enqueue`` param.
EnqueueFn = Callable[[AgentEvent], Awaitable[bool]]


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso(ts: Any) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _state_path(persist_dir: Path) -> Path:
    return persist_dir / RECOVERY_STATE_FILE


def _load_state(persist_dir: Path) -> dict:
    """Load ``{last_reconciled: iso, inflight: {source_id: {...}}}``.

    Tolerant of a missing / corrupt / hand-edited file — always returns a
    well-shaped dict so callers never have to guard the structure.
    """
    p = _state_path(persist_dir)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                last = data.get("last_reconciled")
                inflight = data.get("inflight")
                return {
                    "last_reconciled": last if isinstance(last, str) else "",
                    "inflight": inflight if isinstance(inflight, dict) else {},
                }
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_reconciled": "", "inflight": {}}


def _save_state(persist_dir: Path, state: dict) -> None:
    try:
        persist_dir.mkdir(parents=True, exist_ok=True)
        # Atomic write (#329): a crash mid-write must not corrupt
        # .recovery.json — atomic_write_json applies the fsync-file +
        # fsync-parent invariant used across the other state stores.
        atomic_write_json(_state_path(persist_dir), state)
    except OSError as exc:  # best-effort — never break the poll cycle
        log.warning("poller recovery: state save failed for %s: %s", persist_dir, exc)


def stash_enqueued_event(persist_dir: Path, event: AgentEvent) -> None:
    """Record an enqueued poller ``AgentEvent`` as in-flight, keyed by its
    ``source_id``, so a later failed turn can re-enqueue it.

    No-op when the event has no ``source_id`` — without it the outcome
    event can't be correlated back, so it isn't recoverable this way.
    """
    if not event.source_id:
        return
    state = _load_state(persist_dir)
    state["inflight"][event.source_id] = {
        "attempts": 0,
        # First-seen timestamp drives the GC TTL (#310).
        "stashed_at": _utc_now_iso(),
        "event": asdict(event),
    }
    _save_state(persist_dir, state)


def _event_from_stash(d: Any) -> AgentEvent | None:
    """Rebuild an ``AgentEvent`` from its stashed ``asdict`` form. Returns
    None (logged) on a shape mismatch — a stale .recovery.json written by
    an older mimir whose AgentEvent had different fields shouldn't crash
    the poll cycle."""
    if not isinstance(d, dict):
        return None
    try:
        return AgentEvent(**d)
    except TypeError as exc:
        log.warning("poller recovery: could not rebuild AgentEvent from stash: %s", exc)
        return None


def _gc_expired_inflight(inflight: dict, ttl_hours: float, now_dt: datetime) -> int:
    """Drop in-flight entries with no terminal outcome within ``ttl_hours``
    — turns that vanished (mid-turn crash/restart) without logging
    turn_failed/turn_completed, so ``.recovery.json`` can't grow unbounded
    (#310). Returns the number dropped.

    Backfills a missing/garbled ``stashed_at`` to now rather than GC-ing the
    entry on sight, so entries stashed by a pre-#310 mimir get a fresh TTL.
    """
    cutoff_secs = ttl_hours * 3600.0
    dropped = 0
    for source_id in list(inflight.keys()):
        entry = inflight.get(source_id)
        if not isinstance(entry, dict):
            del inflight[source_id]
            dropped += 1
            continue
        dt = _parse_iso(entry.get("stashed_at"))
        if dt is None:
            entry["stashed_at"] = now_dt.isoformat()  # backfill; GC next window
            continue
        if (now_dt - dt).total_seconds() > cutoff_secs:
            del inflight[source_id]
            dropped += 1
    return dropped


def _read_outcomes_since(
    events_path: Path, channel_id: str, since_iso: str,
) -> list[dict]:
    """Turn-outcome records (``turn_completed`` / ``turn_failed``) for
    ``channel_id`` strictly newer than ``since_iso``, returned oldest-first.

    ``tail_jsonl_records`` yields newest-first; we stop as soon as we cross
    the cutoff (everything older is already processed) so this stays O(new
    events) rather than O(whole log).

    NOTE (chainlink #316): this assumes events.jsonl is timestamp-ordered.
    Writers stamp the time before taking the append lock, so a record can
    in principle land slightly out of order near the cutoff and be missed
    by the early ``break``. In practice the window is sub-millisecond and
    the stash GC bounds any leak; a fully order-independent scan is tracked
    separately.
    """
    if not events_path.exists():
        return []
    out: list[dict] = []
    try:
        for rec in tail_jsonl_records(events_path):
            ts = rec.get("timestamp")
            if not isinstance(ts, str):
                continue
            if since_iso and ts <= since_iso:
                break  # reverse-chrono: the rest is already reconciled
            if rec.get("type") not in _TURN_OUTCOME_TYPES:
                continue
            if rec.get("channel_id") != channel_id:
                continue
            out.append(rec)
    except OSError as exc:
        log.warning("poller recovery: outcome read failed for %s: %s", events_path, exc)
        return []
    out.reverse()  # oldest-first so attempts increment in turn order
    return out


async def _emit_gave_up(poller_name: str, channel_id: str, entry: dict, source_id: str) -> None:
    """Emit the one-shot wedge-guard signal. ``poller_turn_gave_up`` ends
    in ``_gave_up`` so ``feedback.classify`` (#515) maps it to a negative
    ``gave_up`` algedonic signal; ``detail`` gives the renderer a target."""
    ev = entry.get("event") if isinstance(entry, dict) else None
    extra = (ev or {}).get("extra") if isinstance(ev, dict) else None
    items = extra.get("items") if isinstance(extra, dict) else None
    detail = poller_name
    if isinstance(items, list) and items and isinstance(items[0], dict):
        ref = items[0].get("url") or items[0].get("repo") or items[0].get("event_type")
        if ref:
            more = f" +{len(items) - 1} more" if len(items) > 1 else ""
            detail = f"{poller_name} ({ref}{more})"
    await log_event(
        "poller_turn_gave_up",
        poller=poller_name,
        channel_id=channel_id,
        source_id=source_id,
        attempts=int(entry.get("attempts", 0)) if isinstance(entry, dict) else 0,
        detail=detail,
        items=items,
    )


async def reconcile_failed_turns(
    *,
    poller_name: str,
    channel_id: str,
    persist_dir: Path,
    events_path: Path,
    enqueue: EnqueueFn,
    max_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS,
    stash_ttl_hours: float = DEFAULT_STASH_TTL_HOURS,
) -> dict:
    """Reconcile in-flight poller events against recent turn outcomes.

    For each turn-outcome event (since the last reconcile) whose
    ``source_id`` is still in-flight:
      * ``turn_completed`` → drop it (the item was processed OK).
      * ``turn_failed``    → re-enqueue the stashed event, up to
        ``max_attempts`` successful re-fires; once the cap is exceeded
        emit ``poller_turn_gave_up`` and drop it (wedge guard).

    Back-pressure (#305): a re-enqueue is only counted — and only burns a
    wedge-guard attempt — when ``enqueue()`` returns True. If the channel
    queue is full (False) or ``enqueue`` raises, the cycle stops without
    advancing the watermark past that outcome, so it's retried next cycle
    rather than silently re-dropped.

    Watermark (#309): advances only past FULLY-handled outcomes and to the
    outcome's own timestamp — never wall-now — so an outcome written
    between this read and the state save is picked up next cycle.

    Returns a ``{reenqueued, completed, gave_up, deferred, expired}``
    summary (for the ``poller_recovery`` log event + tests). Best-effort
    throughout — any I/O hiccup is logged and the poll cycle continues.
    """
    summary = {
        "reenqueued": 0, "completed": 0, "gave_up": 0,
        "deferred": 0, "expired": 0,
    }
    state = _load_state(persist_dir)
    inflight: dict = state["inflight"]
    now_dt = _utc_now()
    now_iso = now_dt.isoformat()

    # GC abandoned entries first so a vanished turn can't pin the stash
    # forever (#310).
    summary["expired"] = _gc_expired_inflight(inflight, stash_ttl_hours, now_dt)

    # Fast path: nothing stashed → nothing to reconcile. Advance the
    # watermark so the first real reconcile after events accrue doesn't
    # rescan history (safe with no in-flight items: any future outcome has
    # a future timestamp).
    if not inflight:
        state["last_reconciled"] = now_iso
        _save_state(persist_dir, state)
        return summary

    outcomes = _read_outcomes_since(
        events_path, channel_id, state.get("last_reconciled", ""),
    )
    # Advance the watermark only past outcomes we FULLY handle (#309). On
    # enqueue back-pressure we stop early and leave it before the un-handled
    # outcome so it's retried (#305).
    watermark = state.get("last_reconciled", "")
    for rec in outcomes:
        ts = rec.get("timestamp")
        source_id = rec.get("source_id")
        otype = rec.get("type")
        if isinstance(source_id, str) and source_id in inflight:
            entry = inflight[source_id]
            if otype == "turn_completed":
                del inflight[source_id]
                summary["completed"] += 1
            elif otype == "turn_failed":
                attempts = int(entry.get("attempts", 0)) + 1
                if attempts > max_attempts:
                    # Wedge guard hit. Emit best-effort (#318): a log
                    # failure must not strand the entry — we still give up.
                    try:
                        await _emit_gave_up(poller_name, channel_id, entry, source_id)
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "poller recovery: gave_up emit failed for %s: %s",
                            source_id, exc,
                        )
                    del inflight[source_id]
                    summary["gave_up"] += 1
                else:
                    event = _event_from_stash(entry.get("event"))
                    if event is None:
                        # Unreconstructable stash (older schema) — drop so we
                        # don't loop on it forever.
                        del inflight[source_id]
                    else:
                        try:
                            accepted = await enqueue(event)
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "poller recovery: re-enqueue raised for %s: %s",
                                source_id, exc,
                            )
                            accepted = False
                        if accepted:
                            # Burn the wedge-guard attempt only on a real
                            # re-fire (#305).
                            entry["attempts"] = attempts
                            summary["reenqueued"] += 1
                        else:
                            # Queue full / raised: defer. Stop WITHOUT
                            # advancing the watermark past this outcome so
                            # it's retried next cycle (#305).
                            summary["deferred"] += 1
                            break
        if isinstance(ts, str):
            watermark = ts

    if summary["deferred"]:
        # We stopped on back-pressure. Persist the watermark exactly where
        # it is (before the deferred outcome) — do NOT fall back to now_iso,
        # which would advance past the un-handled outcome and lose the retry
        # (#305).
        state["last_reconciled"] = watermark
    else:
        # No outcomes seen → keep advancing so we don't rescan history; any
        # future outcome for an in-flight item has a later timestamp anyway.
        state["last_reconciled"] = watermark or now_iso
    _save_state(persist_dir, state)
    return summary
