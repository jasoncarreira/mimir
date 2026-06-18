"""Read-only scheduler, poller, and commitments dashboard payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .commitments.models import CommitmentRecord, CommitmentStatus
from .commitments.store import CommitmentsStore
from .pollers import POLLER_CHANNEL_PREFIX
from .scheduler import SCHEDULER_CHANNEL_PREFIX, Scheduler


ACTIVE_COMMITMENT_STATUSES = frozenset({
    CommitmentStatus.PENDING.value,
    CommitmentStatus.DELIVERED.value,
    CommitmentStatus.SNOOZED.value,
})


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _event_ts(event: dict[str, Any]) -> str:
    return str(event.get("timestamp") or event.get("ts") or "")


def _event_detail(event: dict[str, Any]) -> str:
    for key in ("reason", "error", "detail"):
        value = event.get(key)
        if value:
            return str(value)
    event_type = str(event.get("type") or "event")
    if event_type == "poller_complete":
        emitted = event.get("events_emitted", 0)
        rejected = event.get("events_rejected", 0)
        return f"emitted={emitted} rejected={rejected}"
    return event_type


def _recent_schedule_events(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    by_name: dict[str, dict[str, dict[str, Any]]] = {}
    interesting = {
        "scheduled_tick",
        "scheduled_tick_suppressed",
        "scheduled_tick_dropped",
        "scheduled_job_misfired",
    }
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type not in interesting:
            continue
        name = str(event.get("schedule_name") or event.get("job_id") or "")
        if name.startswith(SCHEDULER_CHANNEL_PREFIX):
            name = name[len(SCHEDULER_CHANNEL_PREFIX):]
        if not name:
            continue
        slot = by_name.setdefault(name, {})
        slot[event_type] = event
    return by_name


def _recent_poller_events(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    by_name: dict[str, dict[str, dict[str, Any]]] = {}
    interesting = {
        "poller_complete",
        "poller_fire_suppressed",
        "poller_misfired",
        "poller_nonzero_exit",
        "poller_timeout",
        "poller_exec_error",
        "poller_enqueue_error",
        "poller_event_rejected",
        "poller_circuit_open",
        "poller_missing_required_env",
    }
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type not in interesting:
            continue
        name = str(event.get("poller") or event.get("job_id") or "")
        if name.startswith(POLLER_CHANNEL_PREFIX):
            name = name[len(POLLER_CHANNEL_PREFIX):]
        if not name:
            continue
        slot = by_name.setdefault(name, {})
        slot[event_type] = event
    return by_name


def _prompt_source(job: Any) -> str:
    if getattr(job, "prompt_file", None):
        return f"file:{job.prompt_file}"
    if getattr(job, "prompt", ""):
        return "inline"
    if getattr(job, "callable_name", None):
        return f"callable:{job.callable_name}"
    return "none"


def _job_lookup(scheduler: Scheduler) -> dict[str, Any]:
    return {job.id: job for job in scheduler._scheduler.get_jobs()}  # noqa: SLF001


def _schedule_rows(
    scheduler: Scheduler | None,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if scheduler is None:
        return []
    jobs = scheduler._scheduler.get_jobs()  # noqa: SLF001
    aps_by_id = {job.id: job for job in jobs}
    recent = _recent_schedule_events(events)
    rows: list[dict[str, Any]] = []
    # Load persisted LLM schedules via the public async method is not possible
    # from this sync builder; use the already-installed APScheduler jobs plus
    # their kwargs as the live view.
    for aps_job in aps_by_id.values():
        if not aps_job.id.startswith(SCHEDULER_CHANNEL_PREFIX):
            continue
        config_job = (aps_job.kwargs or {}).get("job")
        if config_job is None:
            continue
        name = str(getattr(config_job, "name", aps_job.id))
        last_ok = recent.get(name, {}).get("scheduled_tick")
        suppressed = recent.get(name, {}).get("scheduled_tick_suppressed")
        dropped = recent.get(name, {}).get("scheduled_tick_dropped")
        misfired = recent.get(name, {}).get("scheduled_job_misfired")
        recent_error = dropped or misfired
        rows.append({
            "id": aps_job.id,
            "name": name,
            "kind": "schedule",
            "cron": getattr(config_job, "cron", None),
            "time_of_day": getattr(config_job, "time_of_day", None),
            "next_run_at": _iso(getattr(aps_job, "next_run_time", None)),
            "last_run_at": _event_ts(last_ok) if last_ok else None,
            "channel": getattr(config_job, "channel_id", None) or aps_job.id,
            "deliver": getattr(config_job, "deliver", None),
            "priority": getattr(config_job, "priority", "low"),
            "prompt_source": _prompt_source(config_job),
            "recent_result": _event_detail(last_ok) if last_ok else None,
            "recent_error": _event_detail(recent_error) if recent_error else None,
            "suppression_reason": _event_detail(suppressed) if suppressed else None,
            "suppression_severity": str(suppressed.get("severity")) if suppressed and suppressed.get("severity") else None,
        })
    return sorted(rows, key=lambda row: row["name"])


def _poller_rows(
    scheduler: Scheduler | None,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if scheduler is None:
        return []
    aps_by_id = _job_lookup(scheduler)
    recent = _recent_poller_events(events)
    rows: list[dict[str, Any]] = []
    for poller in sorted(scheduler._pollers.values(), key=lambda item: item.name):  # noqa: SLF001
        aps_job = aps_by_id.get(f"{POLLER_CHANNEL_PREFIX}{poller.name}")
        last_ok = recent.get(poller.name, {}).get("poller_complete")
        suppressed = recent.get(poller.name, {}).get("poller_fire_suppressed")
        recent_error = next(
            (
                recent.get(poller.name, {}).get(kind)
                for kind in (
                    "poller_nonzero_exit",
                    "poller_timeout",
                    "poller_exec_error",
                    "poller_enqueue_error",
                    "poller_event_rejected",
                    "poller_misfired",
                    "poller_circuit_open",
                    "poller_missing_required_env",
                )
                if recent.get(poller.name, {}).get(kind)
            ),
            None,
        )
        rows.append({
            "id": f"{POLLER_CHANNEL_PREFIX}{poller.name}",
            "name": poller.name,
            "kind": "poller",
            "cron": poller.cron,
            "time_of_day": None,
            "next_run_at": _iso(getattr(aps_job, "next_run_time", None)) if aps_job else None,
            "last_run_at": _event_ts(last_ok) if last_ok else None,
            "channel": poller.channel_id(),
            "deliver": poller.deliver,
            "priority": poller.priority,
            "prompt_source": "poller stdout",
            "recent_result": _event_detail(last_ok) if last_ok else None,
            "recent_error": _event_detail(recent_error) if recent_error else None,
            "suppression_reason": _event_detail(suppressed) if suppressed else None,
            "suppression_severity": str(suppressed.get("severity")) if suppressed and suppressed.get("severity") else None,
            "manifest_path": str(poller.manifest_path) if poller.manifest_path else None,
        })
    return rows


def _commitment_due_bucket(rec: CommitmentRecord, *, now_unix: float) -> str:
    start = rec.due_window_start_unix
    if start is None:
        return "unanchored"
    if start < now_unix:
        return "overdue"
    delta = start - now_unix
    if delta <= 86400:
        return "today"
    if delta <= 7 * 86400:
        return "7d"
    if delta <= 30 * 86400:
        return "30d"
    return "later"


def _commitment_rows(
    commitments_store: CommitmentsStore | None,
    *,
    due_window: str,
    now_unix: float,
) -> list[dict[str, Any]]:
    if commitments_store is None:
        return []
    rows: list[dict[str, Any]] = []
    for rec in commitments_store.list(include_unbound=True):
        if rec.status not in ACTIVE_COMMITMENT_STATUSES:
            continue
        bucket = _commitment_due_bucket(rec, now_unix=now_unix)
        if due_window != "all" and bucket != due_window:
            continue
        rows.append({
            "id": rec.id,
            "text": rec.text,
            "status": rec.status,
            "kind": rec.kind,
            "sensitivity": rec.sensitivity,
            "channel": rec.channel_id,
            "recipient_identity": rec.recipient_identity,
            "due_window_start": _iso(datetime.fromtimestamp(rec.due_window_start_unix, tz=timezone.utc)) if rec.due_window_start_unix is not None else None,
            "due_window_end": _iso(datetime.fromtimestamp(rec.due_window_end_unix, tz=timezone.utc)) if rec.due_window_end_unix is not None else None,
            "due_window_hint": rec.due_window_hint,
            "due_bucket": bucket,
            "attempts": rec.attempts,
            "snooze_count": rec.snooze_count,
            "snoozed_until": _iso(datetime.fromtimestamp(rec.snoozed_until_unix, tz=timezone.utc)) if rec.snoozed_until_unix is not None else None,
            "suggested_reminder": rec.suggested_reminder,
            "source_turn_id": rec.source_turn_id,
        })
    return sorted(rows, key=lambda row: (row["due_window_start"] or "9999", row["id"]))


def parse_due_window(raw: str | None) -> str:
    due_window = (raw or "all").strip().lower()
    if due_window not in {"all", "overdue", "today", "7d", "30d", "later", "unanchored"}:
        raise ValueError("due_window must be one of all, overdue, today, 7d, 30d, later, unanchored")
    return due_window


def build_scheduler_dashboard_payload(
    *,
    scheduler: Scheduler | None,
    commitments_store: CommitmentsStore | None,
    events: list[dict[str, Any]],
    due_window: str = "all",
    now_unix: float | None = None,
) -> dict[str, Any]:
    if now_unix is None:
        now_unix = datetime.now(tz=timezone.utc).timestamp()
    schedules = _schedule_rows(scheduler, events)
    pollers = _poller_rows(scheduler, events)
    commitments = _commitment_rows(
        commitments_store,
        due_window=due_window,
        now_unix=now_unix,
    )
    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "available": scheduler is not None,
        "due_window": due_window,
        "schedules": schedules,
        "pollers": pollers,
        "commitments": commitments,
        "actions": {
            "mutations_enabled": False,
            "policy": (
                "pause, trigger, complete, and snooze require explicit "
                "confirmation plus audit; this v1 dashboard is read-only"
            ),
            "deferred": ["pause", "trigger", "complete", "snooze", "create_schedule", "remove_schedule"],
        },
    }
