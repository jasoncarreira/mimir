"""APScheduler-backed scheduler (SPEC §3.5, §7.5).

Responsibilities:
- Load LLM-tick jobs from ``<home>/scheduler.yaml`` and register cron triggers.
- Provide ``add/list/remove_schedule`` so tools can mutate the file at runtime
  (atomic add-or-replace by name; serialized through a single asyncio lock).
- On each cron fire: build an ``AgentEvent`` with ``trigger="scheduled_tick"``
  and enqueue it via the dispatcher (the same path as inbound bridge messages).
- Run the SAGA weekly consolidation cron (Phase 4) as a non-LLM job.

Schedule jobs are persisted as YAML for human readability:
::
    - name: morning-review
      prompt: "Review yesterday's notes."
      cron: "0 8 * * *"
      channel_id: null

Exactly one of ``cron`` (5-field) or ``time_of_day`` (``"HH:MM"`` daily) must
be set per job. Empty ``channel_id`` means "global tick" — the dispatcher
keys on a synthetic ``scheduler:<name>`` channel so two jobs run in parallel
but back-to-back ticks of the same job serialize naturally.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .event_logger import log_event
from .models import AgentEvent
from .saga_client import SagaClient, SagaError

log = logging.getLogger(__name__)

UTC = timezone.utc

EnqueueFn = Callable[[AgentEvent], Awaitable[bool]]


# ---------------------------------------------------------------------------
# Job model + YAML round-trip
# ---------------------------------------------------------------------------


@dataclass
class SchedulerJob:
    name: str
    prompt: str
    cron: str | None = None
    time_of_day: str | None = None
    channel_id: str | None = None

    def to_yaml_entry(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "prompt": self.prompt}
        if self.cron:
            out["cron"] = self.cron
        if self.time_of_day:
            out["time_of_day"] = self.time_of_day
        out["channel_id"] = self.channel_id
        return out

    @classmethod
    def from_yaml_entry(cls, raw: dict[str, Any]) -> "SchedulerJob":
        name = str(raw.get("name", "")).strip()
        prompt = str(raw.get("prompt", "")).strip()
        cron = str(raw.get("cron", "")).strip() or None
        time_of_day = str(raw.get("time_of_day", "")).strip() or None
        channel_id = raw.get("channel_id")
        if isinstance(channel_id, str) and not channel_id.strip():
            channel_id = None
        if not name:
            raise ValueError("scheduler job missing 'name'")
        if not prompt:
            raise ValueError(f"scheduler job {name!r} missing 'prompt'")
        if bool(cron) == bool(time_of_day):
            raise ValueError(
                f"scheduler job {name!r}: exactly one of cron / time_of_day required"
            )
        return cls(
            name=name,
            prompt=prompt,
            cron=cron,
            time_of_day=time_of_day,
            channel_id=channel_id,
        )


def load_jobs(path: Path) -> list[SchedulerJob]:
    """Read scheduler.yaml. Returns [] for missing/empty/invalid files —
    one bad job shouldn't take the whole list down."""
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        log.warning("scheduler.yaml parse failed: %s", exc)
        return []
    if not isinstance(raw, list):
        return []
    out: list[SchedulerJob] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(SchedulerJob.from_yaml_entry(entry))
        except ValueError as exc:
            log.warning("invalid scheduler job: %s", exc)
    return out


def write_jobs(path: Path, jobs: list[SchedulerJob]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        [j.to_yaml_entry() for j in jobs],
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(body or "[]\n", encoding="utf-8")
    tmp.replace(path)


def _build_trigger(job: SchedulerJob) -> CronTrigger:
    """Convert ``cron`` / ``time_of_day`` to an APScheduler trigger.
    Raises ``ValueError`` for malformed expressions."""
    if job.cron:
        return CronTrigger.from_crontab(job.cron, timezone=UTC)
    if job.time_of_day:
        try:
            hh, mm = str(job.time_of_day).split(":")
            return CronTrigger(hour=int(hh), minute=int(mm), timezone=UTC)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid time_of_day {job.time_of_day!r}: {exc}") from exc
    raise ValueError("job must have cron or time_of_day")


def _scheduler_channel_id(job_name: str, channel_id: str | None) -> str:
    """Pick a channel_id for the dispatched event. Real channels go through
    their own queue; ``null`` channel jobs use a per-job synthetic key so they
    parallelize across jobs but serialize within a job."""
    if channel_id:
        return channel_id
    return f"scheduler:{job_name}"


# ---------------------------------------------------------------------------
# Scheduler service
# ---------------------------------------------------------------------------


class Scheduler:
    """One AsyncIOScheduler. Owns LLM-tick jobs (from scheduler.yaml) plus the
    SAGA weekly consolidation cron from Phase 4."""

    def __init__(
        self,
        scheduler_yaml: Path,
        enqueue: EnqueueFn,
    ) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._yaml_path = scheduler_yaml
        self._enqueue = enqueue
        self._mutate_lock = asyncio.Lock()
        self._started = False

    # ---- LLM-tick jobs ------------------------------------------------

    def reload(self) -> dict[str, int]:
        """Wipe LLM-tick registrations and re-register from scheduler.yaml.
        Returns ``{registered, invalid}`` counts. Caller logs."""
        # Drop existing scheduler:* jobs; leave non-prefixed (e.g. saga-consolidate).
        for job in list(self._scheduler.get_jobs()):
            if job.id.startswith("scheduler:"):
                self._scheduler.remove_job(job.id)

        registered = 0
        invalid = 0
        for job in load_jobs(self._yaml_path):
            try:
                trigger = _build_trigger(job)
            except ValueError:
                invalid += 1
                continue
            self._scheduler.add_job(
                self._fire,
                trigger=trigger,
                kwargs={"job": job},
                id=f"scheduler:{job.name}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            registered += 1
        return {"registered": registered, "invalid": invalid}

    async def _fire(self, *, job: SchedulerJob) -> None:
        event = AgentEvent(
            trigger="scheduled_tick",
            channel_id=_scheduler_channel_id(job.name, job.channel_id),
            content=job.prompt,
            extra={"schedule_name": job.name, "configured_channel_id": job.channel_id},
        )
        await log_event("scheduled_tick", schedule_name=job.name, channel_id=event.channel_id)
        accepted = await self._enqueue(event)
        if not accepted:
            await log_event(
                "scheduled_tick_dropped",
                schedule_name=job.name,
                channel_id=event.channel_id,
                reason="dispatcher_rejected",
            )

    async def add_job(self, job: SchedulerJob) -> SchedulerJob:
        """Atomic add-or-replace by name. Validates the trigger before
        persisting; raises ``ValueError`` on bad cron/time_of_day."""
        _build_trigger(job)  # validate up front
        async with self._mutate_lock:
            current = await asyncio.to_thread(load_jobs, self._yaml_path)
            current = [j for j in current if j.name != job.name]
            current.append(job)
            await asyncio.to_thread(write_jobs, self._yaml_path, current)
            await asyncio.to_thread(self.reload)
        return job

    async def remove_job(self, name: str) -> bool:
        async with self._mutate_lock:
            current = await asyncio.to_thread(load_jobs, self._yaml_path)
            kept = [j for j in current if j.name != name]
            if len(kept) == len(current):
                return False
            await asyncio.to_thread(write_jobs, self._yaml_path, kept)
            await asyncio.to_thread(self.reload)
        return True

    async def list_jobs(self) -> list[SchedulerJob]:
        return await asyncio.to_thread(load_jobs, self._yaml_path)

    # ---- SAGA consolidation cron -------------------------------------

    def add_saga_consolidate_job(
        self,
        saga_client: SagaClient,
        cron_expr: str,
        *,
        job_id: str = "saga-consolidate",
    ) -> bool:
        cron_expr = (cron_expr or "").strip()
        if not cron_expr:
            return False
        try:
            trigger = CronTrigger.from_crontab(cron_expr, timezone=UTC)
        except (ValueError, KeyError) as exc:
            raise ValueError(f"invalid cron expression {cron_expr!r}: {exc}") from exc

        async def _consolidate() -> None:
            try:
                payload = await saga_client.consolidate(dry_run=False)
                await log_event(
                    "saga_consolidate_ok",
                    dry_run=False,
                    result=_summarize_consolidate(payload),
                )
            except SagaError as exc:
                await log_event("saga_consolidate_error", error=str(exc), status=exc.status)
            except Exception as exc:  # noqa: BLE001
                await log_event("saga_consolidate_error", error=f"{type(exc).__name__}: {exc}")

        self._scheduler.add_job(
            _consolidate,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        return True

    # ---- lifecycle ---------------------------------------------------

    def start(self) -> None:
        if not self._started:
            self._scheduler.start()
            self._started = True

    def stop(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False


def _summarize_consolidate(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {"raw": str(payload)[:200]}
    return {
        k: payload[k]
        for k in ("clusters_processed", "atoms_merged", "atoms_retired", "duration_s")
        if k in payload
    }
