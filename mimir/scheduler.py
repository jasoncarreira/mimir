"""APScheduler-backed scheduler (SPEC §3.5, §7.5).

Responsibilities:
- Load LLM-tick jobs from ``<home>/scheduler.yaml`` and register cron triggers.
- Provide ``add/list/remove_schedule`` so tools can mutate the file at runtime
  (atomic add-or-replace by name; serialized through a single asyncio lock).
- On each cron fire: build an ``AgentEvent`` with ``trigger="scheduled_tick"``
  and enqueue it via the dispatcher (the same path as inbound bridge messages).
- Run the SAGA consolidation cron (Phase 4) as a non-LLM job.

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
import os
import time
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from .commitments.store import CommitmentsStore

import yaml
from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .event_logger import log_event
from .models import AgentEvent
from .pollers import POLLER_CHANNEL_PREFIX, PollerConfig, discover_pollers, run_poller
from .saga_client import SagaClient, SagaError

log = logging.getLogger(__name__)

UTC = timezone.utc


def _resolve_tz(name: str) -> ZoneInfo:
    """Look up ``name`` as a ZoneInfo, falling back to UTC with a
    warning when the name is unknown / tzdata is missing the entry.

    Returning UTC on misconfiguration is safer than crashing the
    scheduler entirely — a wrong-but-functioning schedule degrades
    gracefully (jobs still fire, just offset), while a crashed
    scheduler takes the agent offline."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        log.warning(
            "invalid MIMIR_SCHEDULER_TZ=%r (%s); falling back to UTC",
            name, exc,
        )
        return ZoneInfo("UTC")


#: Channel-id prefix for synthetic scheduler-tick channels. Each named
#: scheduler job (heartbeat, reflect, saga-consolidate, etc.) without a
#: real channel uses ``scheduler:<job_name>`` as its event key. Exported
#: so other modules can recognize the prefix without duplicating the
#: literal — see :data:`mimir.pollers.POLLER_CHANNEL_PREFIX` for the
#: sibling convention.
SCHEDULER_CHANNEL_PREFIX = "scheduler:"

EnqueueFn = Callable[[AgentEvent], Awaitable[bool]]


# Optional arbiter — when present, the scheduler consults it before
# firing a tick and may suppress S4 work on plan-window or cost-rate
# pressure. Typed as ``Any`` to avoid an import cycle with budget.py.
HomeostaticArbiter = Any


# ---------------------------------------------------------------------------
# Job model + YAML round-trip
# ---------------------------------------------------------------------------


@dataclass
class SchedulerJob:
    name: str
    # An entry is one of three kinds:
    #
    # 1. **Prompt LLM-tick** — ``prompt`` (inline string) OR
    #    ``prompt_file`` (path under ``MIMIR_HOME/prompts/``). When both
    #    are set, ``prompt_file`` wins at fire time and ``prompt`` is the
    #    fallback if the file goes missing.
    # 2. **Named callable** — ``callable_name`` references a
    #    code-side-registered callable on the Scheduler (saga-consolidate,
    #    identities-populate, etc.). The yaml entry exists to override
    #    the env-var-default cron or disable the callable entirely.
    #
    # Exactly one of ``prompt`` / ``prompt_file`` / ``callable_name``
    # must be set per entry. ``callable`` is the on-disk yaml field
    # name; ``callable_name`` is the dataclass attribute (avoiding
    # the ``callable`` builtin shadow).
    prompt: str = ""
    prompt_file: str | None = None
    callable_name: str | None = None
    cron: str | None = None
    time_of_day: str | None = None
    channel_id: str | None = None

    def to_yaml_entry(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.prompt:
            out["prompt"] = self.prompt
        if self.prompt_file:
            out["prompt_file"] = self.prompt_file
        if self.callable_name:
            out["callable"] = self.callable_name
        if self.cron:
            out["cron"] = self.cron
        if self.time_of_day:
            out["time_of_day"] = self.time_of_day
        # Callable entries don't carry a channel_id (they're not
        # dispatched as AgentEvents); only emit it for prompt entries
        # to keep yaml uncluttered.
        if not self.callable_name:
            out["channel_id"] = self.channel_id
        return out

    @classmethod
    def from_yaml_entry(cls, raw: dict[str, Any]) -> "SchedulerJob":
        name = str(raw.get("name", "")).strip()
        prompt = str(raw.get("prompt", "")).strip()
        prompt_file_raw = str(raw.get("prompt_file", "")).strip() or None
        callable_name_raw = str(raw.get("callable", "")).strip() or None
        cron = str(raw.get("cron", "")).strip() or None
        time_of_day = str(raw.get("time_of_day", "")).strip() or None
        channel_id = raw.get("channel_id")
        if isinstance(channel_id, str) and not channel_id.strip():
            channel_id = None
        if not name:
            raise ValueError("scheduler job missing 'name'")
        # Exactly one of prompt / prompt_file / callable must be set.
        # The three are mutually exclusive — prompt+callable would
        # be ambiguous (do we run the callable or render the prompt?)
        # and silent precedence rules accumulate confusion over time.
        kind_count = sum(bool(x) for x in (prompt, prompt_file_raw, callable_name_raw))
        if kind_count == 0:
            raise ValueError(
                f"scheduler job {name!r}: one of 'prompt', 'prompt_file', "
                f"or 'callable' required"
            )
        if kind_count > 1:
            raise ValueError(
                f"scheduler job {name!r}: 'prompt', 'prompt_file', and "
                f"'callable' are mutually exclusive — exactly one"
            )
        # cron OR time_of_day required for prompt/prompt_file entries.
        # Callable entries with empty cron mean "explicitly disabled
        # for this deployment, regardless of env-var default" — we
        # still allow that as a deliberate operator action.
        if callable_name_raw is None:
            if bool(cron) == bool(time_of_day):
                raise ValueError(
                    f"scheduler job {name!r}: exactly one of cron / time_of_day required"
                )
        else:
            # Callable entry: time_of_day not supported (callables get
            # cron expressions only — they're typically internal
            # cadence, not user-facing schedules).
            if time_of_day:
                raise ValueError(
                    f"scheduler job {name!r}: callable entries use 'cron' "
                    f"only; 'time_of_day' is for prompt entries"
                )
        return cls(
            name=name,
            prompt=prompt,
            prompt_file=prompt_file_raw,
            callable_name=callable_name_raw,
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


def _resolve_prompt_file(home: Path | None, prompt_file: str) -> Path | None:
    """Resolve ``prompt_file`` against ``<home>/prompts/`` and verify
    the result stays inside that directory. Returns None on escape,
    missing home, or empty input. Caller logs and falls back to the
    inline prompt when None is returned."""
    if not home or not prompt_file or not prompt_file.strip():
        return None
    root = (home / "prompts").resolve()
    candidate = (root / prompt_file.strip().lstrip("/")).resolve()
    if root not in candidate.parents and candidate != root:
        return None
    return candidate


def _build_trigger(
    job: SchedulerJob, tz: ZoneInfo | None = None,
) -> CronTrigger:
    """Convert ``cron`` / ``time_of_day`` to an APScheduler trigger.
    Raises ``ValueError`` for malformed expressions.

    ``tz`` controls the timezone APScheduler interprets the cron
    expression in. Defaults to UTC for back-compat with bench / test
    call sites that construct triggers without a Scheduler instance.
    Production wiring threads through ``Scheduler._tz`` (from
    ``MIMIR_SCHEDULER_TZ``) so operators deploying in a non-UTC
    timezone don't have to mentally convert every cron expression.
    """
    if tz is None:
        tz = ZoneInfo("UTC")
    if job.cron:
        return CronTrigger.from_crontab(job.cron, timezone=tz)
    if job.time_of_day:
        try:
            hh, mm = str(job.time_of_day).split(":")
            return CronTrigger(hour=int(hh), minute=int(mm), timezone=tz)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid time_of_day {job.time_of_day!r}: {exc}") from exc
    raise ValueError("job must have cron or time_of_day")


def _scheduler_channel_id(job_name: str, channel_id: str | None) -> str:
    """Pick a channel_id for the dispatched event. Real channels go through
    their own queue; ``null`` channel jobs use a per-job synthetic key so they
    parallelize across jobs but serialize within a job."""
    if channel_id:
        return channel_id
    return f"{SCHEDULER_CHANNEL_PREFIX}{job_name}"


# ---------------------------------------------------------------------------
# Named-callable registry
# ---------------------------------------------------------------------------


@dataclass
class _CallableDef:
    """One registered non-LLM cron callable. The closure ``fn`` captures
    the binding context (SagaClient, channel registry, etc.) so the
    yaml side never needs to know about runtime handles. ``default_cron``
    is the env-var-derived fallback used when no yaml entry overrides;
    ``job_id`` is the APScheduler id (typically equal to ``name``)."""
    name: str
    fn: Callable[[], Awaitable[None]]
    default_cron: str
    job_id: str
    # Misfire grace + max-instance config varies per callable
    # (saga-consolidate tolerates 1h misfires; oauth poll wants 60s).
    # Captured here so reload() can re-install with the right knobs.
    misfire_grace_time: int = 3600
    max_instances: int = 1
    coalesce: bool = True


def _resolve_callable_cron(
    yaml_jobs: list[SchedulerJob],
    callable_name: str,
    default_cron: str,
) -> tuple[str, str]:
    """Resolve the effective cron for a registered callable.

    Returns ``(effective_cron, source)`` where ``source`` is one of
    ``"yaml"``, ``"env"``, or ``"yaml-disabled"``. ``yaml-disabled``
    means the yaml has an entry naming this callable but with empty
    cron — the operator's explicit "off" signal, which beats the
    env-var default.

    The yaml's match is by ``callable_name`` (the registered name),
    not by the entry's ``name`` field. Operator can give the yaml
    entry any human-readable name; the binding is via ``callable:``.
    """
    for job in yaml_jobs:
        if job.callable_name == callable_name:
            cron = (job.cron or "").strip()
            if cron:
                return cron, "yaml"
            # Empty cron + matching callable = explicit disable.
            return "", "yaml-disabled"
    return (default_cron or "").strip(), "env"


# ---------------------------------------------------------------------------
# Scheduler service
# ---------------------------------------------------------------------------


class Scheduler:
    """One AsyncIOScheduler. Owns LLM-tick jobs (from scheduler.yaml) plus the
    SAGA consolidation cron from Phase 4."""

    def __init__(
        self,
        scheduler_yaml: Path,
        enqueue: EnqueueFn,
        *,
        arbiter: HomeostaticArbiter | None = None,
        home: Path | None = None,
        scheduler_tz: str = "UTC",
    ) -> None:
        # APScheduler interprets cron expressions in the scheduler's
        # timezone. Default UTC keeps back-compat for mimirbot + bench
        # harnesses that don't set MIMIR_SCHEDULER_TZ. Operators
        # deploying in a non-UTC region (e.g., Muninn on ET) set this
        # via env so they can author scheduler.yaml in local-wall-
        # clock terms instead of mentally subtracting hours twice a
        # year for DST.
        self._tz = _resolve_tz(scheduler_tz)
        self._scheduler = AsyncIOScheduler(timezone=self._tz)
        self._yaml_path = scheduler_yaml
        self._enqueue = enqueue
        self._arbiter = arbiter
        self._mutate_lock = asyncio.Lock()
        self._started = False
        # Used to resolve ``SchedulerJob.prompt_file`` against
        # ``<home>/prompts/<file>`` at fire time. Optional for tests
        # and bench harnesses that construct Scheduler without a home.
        self._home = home
        # Named-callable registry. Populated by ``register_callable``
        # at startup (server.py wires each non-LLM cron). The yaml
        # is the override surface — entries naming a registered
        # callable change its cron without a restart; missing yaml
        # entry → env-var-default-cron is used. See
        # ``docs/internal/SCHEDULER_CALLABLE_JOBS.md`` for the design.
        self._callables: dict[str, _CallableDef] = {}
        # Pollers framework (chainlink #3). Discovered from
        # ``<home>/.claude/skills/**/pollers.json`` at startup +
        # on ``reload_pollers`` MCP tool. Skill directory drop is
        # the only install path; no mimir release required to add
        # a new poller. ``_pollers_dir`` is None until
        # ``add_poller_jobs`` runs (most installs no-op cleanly).
        self._pollers_dir: Path | None = None
        self._pollers: dict[str, PollerConfig] = {}
        # Snapshot of the most-recent reload's invalid-manifest events
        # (chainlink #84, PR #141 review). Cleared and re-populated on
        # each ``add_poller_jobs`` / ``reload_pollers`` call. Read by the
        # ``mcp__mimir__reload_pollers`` MCP tool to surface a warning
        # in the operator-visible reply (events.jsonl already carries
        # the full payload — this just lets the MCP layer flag
        # "1 manifest failed to parse — see events.jsonl" inline). An
        # empty list means "last reload had no parse failures."
        self._last_invalid_manifest_events: list[dict[str, Any]] = []
        # Global concurrency cap on poller subprocess fan-out.
        # ``MIMIR_MAX_CONCURRENT_POLLERS`` (default 8) bounds how many
        # ``run_poller`` invocations can be in-flight at once. Without a
        # cap, 50 skills with ``* * * * *`` crons would launch 50
        # subprocesses every minute, each up to 60s — no upper bound on
        # subprocess fork cost or aiohttp/disk pressure inside the
        # subprocesses. The dispatcher's ``max_concurrent_turns``
        # doesn't apply here because pollers run BEFORE events get
        # enqueued. Default 8 is generous for typical 5-10 skill
        # deployments while throttling buggy fork-bomb skills.
        try:
            cap = int(os.environ.get("MIMIR_MAX_CONCURRENT_POLLERS", "8"))
        except ValueError:
            cap = 8
        cap = max(1, cap)  # 0 / negative → 1 (degenerate single-fire)
        self._poller_semaphore = asyncio.Semaphore(cap)
        self._poller_concurrency_cap = cap

        # APScheduler ``EVENT_JOB_MISSED`` listener — emits a
        # ``poller_misfired`` algedonic event whenever a job's fire
        # time was missed (typically because a previous instance was
        # still running and the new one fell outside ``misfire_grace_time``).
        # Without this listener, missed fires were silently dropped:
        # a poller cron of ``* * * * *`` against a 60s subprocess
        # timeout would lose every other minute under load with no
        # operator-visible signal. The lowered ``misfire_grace_time``
        # (60→5s, see ``_reinstall_pollers``) makes the missed-fire
        # event reliable rather than rare.
        self._scheduler.add_listener(
            self._on_job_missed, EVENT_JOB_MISSED,
        )

    def _on_job_missed(self, event: JobExecutionEvent) -> None:
        """APScheduler listener for EVENT_JOB_MISSED.

        AsyncIOScheduler dispatches listener callbacks from within the
        event loop (its wakeup task IS a coroutine), so we can schedule
        the log emit via ``asyncio.create_task`` directly. If for some
        reason there's no running loop (e.g. called during shutdown),
        fall back to a sync ``log.warning`` so the event isn't silently
        lost.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning(
                "scheduled_job_misfired (no loop): job=%s scheduled=%s",
                event.job_id, event.scheduled_run_time,
            )
            return
        loop.create_task(self._log_misfire(event))

    async def _log_misfire(self, event: JobExecutionEvent) -> None:
        # Distinguish poller misses (operator-actionable: tune cron or
        # bump timeout) from other misses (LLM ticks, callable jobs).
        is_poller = event.job_id.startswith(POLLER_CHANNEL_PREFIX)
        kind = "poller_misfired" if is_poller else "scheduled_job_misfired"
        await log_event(
            kind,
            job_id=event.job_id,
            scheduled_run_time=(
                event.scheduled_run_time.isoformat()
                if event.scheduled_run_time else None
            ),
        )

    # ---- LLM-tick jobs ------------------------------------------------

    def reload(self) -> dict[str, int]:
        """Wipe LLM-tick registrations and re-register from scheduler.yaml.
        Re-resolves registered callables against the (potentially mutated)
        yaml so runtime cron overrides take effect on the next tick.
        Returns ``{registered, invalid}`` counts. Caller logs.

        ``registered`` and ``invalid`` only count prompt-style entries.
        Callable entries are tracked separately via the registry."""
        # Drop existing scheduler:* jobs; leave non-prefixed (e.g. saga-consolidate).
        for job in list(self._scheduler.get_jobs()):
            if job.id.startswith(SCHEDULER_CHANNEL_PREFIX):
                self._scheduler.remove_job(job.id)

        yaml_jobs = load_jobs(self._yaml_path)

        # Re-resolve registered callables against the new yaml. A yaml
        # mutation that adds / removes / changes a ``callable:`` entry
        # propagates to APScheduler here.
        for cdef in list(self._callables.values()):
            try:
                self._install_callable(cdef)
            except ValueError as exc:
                log.warning(
                    "reload: callable %r install failed: %s",
                    cdef.name, exc,
                )

        # Warn-skip yaml entries naming an unregistered callable.
        # Don't crash startup — could be a stale yaml after a refactor
        # removed the callable code-side.
        registered_names = set(self._callables.keys())

        registered = 0
        invalid = 0
        for job in yaml_jobs:
            # Skip callable-typed entries — they're handled above.
            if job.callable_name is not None:
                if job.callable_name not in registered_names:
                    log.warning(
                        "scheduler.yaml entry %r references unregistered "
                        "callable %r; skipping",
                        job.name, job.callable_name,
                    )
                continue
            try:
                trigger = _build_trigger(job, self._tz)
            except ValueError:
                invalid += 1
                continue
            self._scheduler.add_job(
                self._fire,
                trigger=trigger,
                kwargs={"job": job},
                id=f"{SCHEDULER_CHANNEL_PREFIX}{job.name}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            registered += 1
        return {"registered": registered, "invalid": invalid}

    # VSM: S4 (intelligence / foresight) — generic scheduled-tick
    #      dispatch. Heartbeat (loop 4.1) and reflection (loop 4.2)
    #      both ride this; the schedule_name in extra distinguishes
    #      which skill the agent should run.
    # loop_id: 4.1
    async def _fire(self, *, job: SchedulerJob) -> None:
        # Resolve the cron's prompt body. Precedence:
        #   1. ``prompt_file`` (relative to ``<home>/prompts/`` — escapes
        #      via ``..`` are rejected so an agent can't reference an
        #      arbitrary host file by setting prompt_file).
        #   2. Inline ``prompt`` field.
        #   3. Empty string (the agent falls back to HEARTBEAT_DEFAULT_PROMPT
        #      via build_turn_prompt's body fallback).
        content = job.prompt
        if job.prompt_file:
            resolved = _resolve_prompt_file(self._home, job.prompt_file)
            if resolved is not None and resolved.is_file():
                try:
                    content = resolved.read_text(encoding="utf-8").strip()
                except OSError as exc:
                    log.warning(
                        "scheduler %r: prompt_file %s read failed (%s); "
                        "falling back to inline prompt",
                        job.name, resolved, exc,
                    )
            else:
                log.warning(
                    "scheduler %r: prompt_file %r missing or escapes "
                    "<home>/prompts/; falling back to inline prompt",
                    job.name, job.prompt_file,
                )

        event = AgentEvent(
            trigger="scheduled_tick",
            channel_id=_scheduler_channel_id(job.name, job.channel_id),
            content=content,
            extra={
                "schedule_name": job.name,
                "configured_channel_id": job.channel_id,
                **({"prompt_file": job.prompt_file} if job.prompt_file else {}),
            },
        )

        # §12.4: ask the homeostat first. If S4 work is suppressed by
        # plan-window saturation or a cost-rate alert, drop the tick
        # with a structured reason so operator audit / dashboards can
        # explain the gap.
        if self._arbiter is not None:
            try:
                # CR#5: should_fire_heartbeat() reads turns.jsonl via
                # aggregate_usage and _partition_turns. Move off the
                # event loop so concurrent dispatcher work (other
                # channel turns, log writers) isn't stalled during the
                # per-tick scan.
                fire, reason = await asyncio.to_thread(
                    self._arbiter.should_fire_heartbeat,
                )
            except Exception:  # noqa: BLE001
                log.exception("arbiter.should_fire_heartbeat raised; firing anyway")
                fire, reason = True, "arbiter_error"
            if not fire:
                await log_event(
                    "scheduled_tick_suppressed",
                    schedule_name=job.name,
                    channel_id=event.channel_id,
                    reason=reason,
                )
                return

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
        persisting; raises ``ValueError`` on bad cron/time_of_day or
        unregistered callable references."""
        if job.callable_name is not None:
            # Callable entries: validate against the registry. Unknown
            # callable would write to yaml as a dead-on-arrival entry.
            if job.callable_name not in self._callables:
                raise ValueError(
                    f"callable {job.callable_name!r} is not registered "
                    f"(registered: {sorted(self._callables.keys())!r})"
                )
            # Cron may be empty (explicit-disable signal); only validate
            # if non-empty.
            if job.cron:
                try:
                    CronTrigger.from_crontab(job.cron, timezone=self._tz)
                except (ValueError, KeyError) as exc:
                    raise ValueError(
                        f"invalid cron expression {job.cron!r}: {exc}"
                    ) from exc
        else:
            _build_trigger(job, self._tz)  # validate up front
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

    # ---- Named-callable registry -------------------------------------

    def register_callable(
        self,
        name: str,
        fn: Callable[[], Awaitable[None]],
        default_cron: str,
        *,
        job_id: str | None = None,
        misfire_grace_time: int = 3600,
        max_instances: int = 1,
        coalesce: bool = True,
    ) -> bool:
        """Register a non-LLM cron callable + install it.

        Effective cron resolution:
          1. yaml entry with matching ``callable: <name>`` → yaml's cron
          2. else ``default_cron`` (typically from a ``MIMIR_*_CRON``
             env var)
          3. empty effective cron → no APScheduler job is installed

        Yaml mutation via ``add_job`` / ``remove_job`` triggers a
        ``reload()`` which re-resolves all registered callables, so
        runtime cron overrides via the ``add_schedule`` MCP tool take
        effect immediately.

        Returns True if a job was installed (cron was non-empty),
        False otherwise (empty default + no yaml override; or yaml
        explicit-disable). The boolean preserves the existing
        ``add_*_job`` return contract.

        Raises ``ValueError`` for invalid cron expressions (caller
        logs and continues; doesn't propagate to startup).
        """
        cdef = _CallableDef(
            name=name,
            fn=fn,
            default_cron=default_cron or "",
            job_id=job_id or name,
            misfire_grace_time=misfire_grace_time,
            max_instances=max_instances,
            coalesce=coalesce,
        )
        # Replace any prior registration under this name so re-calls
        # during tests / hot-reload-style server restarts don't leak
        # stale closures.
        self._callables[name] = cdef
        return self._install_callable(cdef)

    def _install_callable(self, cdef: _CallableDef) -> bool:
        """Resolve the effective cron for ``cdef`` and (re-)add the
        APScheduler job. Returns True if a job was installed."""
        # Drop any existing APScheduler job under this id. This makes
        # reload() idempotent — it can re-register without leaking
        # the prior job.
        try:
            self._scheduler.remove_job(cdef.job_id)
        except Exception:  # noqa: BLE001 — JobLookupError or other; both fine
            pass

        yaml_jobs: list[SchedulerJob]
        try:
            yaml_jobs = load_jobs(self._yaml_path)
        except Exception:  # noqa: BLE001 — already logged inside load_jobs
            yaml_jobs = []

        effective_cron, source = _resolve_callable_cron(
            yaml_jobs, cdef.name, cdef.default_cron,
        )
        if not effective_cron:
            log.info(
                "callable %r: no effective cron (source=%s); "
                "not installing",
                cdef.name, source,
            )
            return False

        try:
            trigger = CronTrigger.from_crontab(effective_cron, timezone=self._tz)
        except (ValueError, KeyError) as exc:
            raise ValueError(
                f"invalid cron expression {effective_cron!r} for "
                f"callable {cdef.name!r} (source={source}): {exc}"
            ) from exc

        self._scheduler.add_job(
            cdef.fn,
            trigger=trigger,
            id=cdef.job_id,
            replace_existing=True,
            misfire_grace_time=cdef.misfire_grace_time,
            max_instances=cdef.max_instances,
            coalesce=cdef.coalesce,
        )
        log.info(
            "callable %r installed at cron %r (source=%s)",
            cdef.name, effective_cron, source,
        )
        return True

    def registered_callables(self) -> list[str]:
        """Names of all registered callables. Used by add_schedule MCP
        tool validation and by the test harness."""
        return sorted(self._callables.keys())

    # ---- Pollers framework (chainlink #3) ----------------------------

    def add_poller_jobs(self, skills_dir: Path) -> int:
        """Discover all pollers under ``skills_dir/**/pollers.json`` and
        register each as an APScheduler cron job. Returns the number
        of pollers successfully installed. Subsequent calls
        (``reload_pollers``) wipe + re-discover.

        Pollers fire as subprocesses; their stdout JSONL becomes
        ``AgentEvent`` enqueues. The framework injects
        ``STATE_DIR=<home>/state/pollers/<poller_name>/`` so cursor
        files survive container rebuilds (filing-rules-aligned: skills
        are deployable artifacts, ``state/`` is persistent runtime).
        Falls back to the skill_dir when ``self._home`` isn't set
        (tests / niche call sites). See ``mimir/pollers.py`` for the
        full contract.

        Any ``poller_reload_invalid_manifest`` events produced by the
        underlying ``_reinstall_pollers`` call are scheduled via the
        running event loop when one is present (startup-time callers
        like ``server.py`` run this from within an awaited init
        coroutine), or dropped to ``log.warning`` otherwise (test
        harnesses without a loop). Mirrors the pattern in
        ``_on_job_missed``.
        """
        self._pollers_dir = skills_dir
        installed, invalid_events = self._reinstall_pollers()
        # Snapshot for the MCP reply (PR #141 review item #1+2).
        # ``installed`` returned here is the count of pollers
        # freshly registered in Phase 3 — at bootstrap time there
        # are no preserved entries, so this equals the live total.
        # ``reload_pollers`` (below) overrides with the live total
        # to handle the preserved case correctly.
        self._last_invalid_manifest_events = list(invalid_events)
        self._dispatch_invalid_manifest_events(invalid_events)
        return installed

    async def reload_pollers(self) -> dict:
        """Re-scan the pollers directory and re-install. Called by the
        ``mcp__mimir__reload_pollers`` MCP tool after the agent installs
        a new skill. No-op when ``add_poller_jobs`` was never called
        (no skills_dir wired).

        Returns a dict with keys ``registered`` (freshly installed this
        reload), ``replaced`` (0 — not tracked), ``removed`` (0 — not
        tracked), ``total`` (live count after reload).

        **Mutate lock (PR #107).** Wrapped with ``self._mutate_lock``
        so concurrent reload_pollers calls (e.g. operator triggers a
        reload while a yaml mutation is also reloading) serialize.
        Without the lock, two concurrent ``_reinstall_pollers``
        invocations could race the APScheduler ``add_job`` /
        ``remove_job`` mutations.

        **Algedonic on malformed manifests (chainlink #84).** A
        ``pollers.json`` that fails to JSON-parse mid-edit no longer
        silently drops the previously-installed poller — the prior
        entry is preserved AND a ``poller_reload_invalid_manifest``
        event lands in events.jsonl with the failing path + error +
        names of preserved pollers. The drop loop only fires for
        manifests that parsed cleanly but no longer contain the
        poller (i.e. operator intentionally removed it).
        """
        if self._pollers_dir is None:
            self._last_invalid_manifest_events = []
            return {"registered": 0, "replaced": 0, "removed": 0, "total": 0}
        async with self._mutate_lock:
            _installed_fresh, invalid_events = await asyncio.to_thread(
                self._reinstall_pollers,
            )
            # PR #141 review item #2: ``_reinstall_pollers`` returns
            # the count of pollers freshly registered in Phase 3, not
            # the total live count. Preserved pollers (chainlink #84:
            # entries whose manifest failed to parse this reload) skip
            # Phase 3, so a reload that preserved 1 and re-registered
            # 1 would return 1 even though ``registered_pollers()``
            # has 2 names. Return ``len(self._pollers)`` instead so
            # the count matches the names list in the MCP reply. Read
            # under the mutate lock so concurrent reloads can't
            # observe a torn snapshot.
            live_total = len(self._pollers)
        # PR #141 review item #1: snapshot for the MCP reply so the
        # operator-visible response can flag "X manifest(s) failed to
        # parse — see events.jsonl" inline. Set before emitting the
        # algedonic events to keep the snapshot consistent with what
        # the MCP layer reads.
        self._last_invalid_manifest_events = list(invalid_events)
        # Emit invalid-manifest algedonic events from the awaited
        # caller (we have a running loop here, unlike inside the
        # ``to_thread`` worker). Outside the mutate lock — the events
        # are observational, not mutating, and holding the lock
        # across awaited disk writes would just widen the contention
        # window.
        #
        # Concurrency note (PR #141 review inline): if a second
        # ``reload_pollers`` enters ``_reinstall_pollers`` between
        # this lock release and the ``await log_event`` loop, and
        # the manifest is still broken, both calls will emit events
        # for the same parse failure. The mutate lock prevents torn
        # state but not duplicate observational events; given reload
        # is operator-triggered (MCP tool / startup), the practical
        # risk is near-zero. Algedonic-event ordering across
        # concurrent reloads is intentionally non-deterministic.
        for payload in invalid_events:
            await log_event("poller_reload_invalid_manifest", **payload)
        return {
            "registered": _installed_fresh,
            "replaced": 0,
            "removed": 0,
            "total": live_total,
        }

    def _reinstall_pollers(self) -> tuple[int, list[dict[str, Any]]]:
        """Wipe + re-discover + re-register. Sync — runs on the
        APScheduler thread or via ``to_thread`` from async callers.

        Returns ``(installed_count, invalid_manifest_events)``. The
        second element is a list of event payloads (one per
        ``pollers.json`` whose JSON parse failed this reload). Callers
        in async contexts emit each as a
        ``poller_reload_invalid_manifest`` algedonic event;
        ``add_poller_jobs`` (sync) routes through
        ``_dispatch_invalid_manifest_events``.

        **Per-entry pre-population, not end-of-loop swap (PR #107
        review fix).** A previous version of this function built a
        new dict locally and swapped at the end. That left an
        add-then-swap window: ``add_job`` registers the poller's
        callback BEFORE the dict swap, so a cron fire that landed in
        that window would look up ``self._pollers.get(name)`` against
        the OLD dict, miss the freshly-registered poller, and emit a
        spurious ``poller_fire_dropped``.

        The fix flips the order: for each newly-validated poller,
        we (1) write it into ``self._pollers[name]`` first, then (2)
        call ``add_job``. A fire that lands between (2)'s callback
        registration and the next statement now finds the poller in
        the dict. Stale entries (pollers that disappeared) are
        cleaned up before the install loop, against the discovered
        name set, so the live dict stays an accurate snapshot
        throughout.

        **Preserve-on-parse-failure (chainlink #84).** When a
        previously-installed poller's manifest fails to JSON-parse on
        this reload, the prior entry is kept in ``self._pollers``
        AND the APScheduler job stays registered (never removed in
        Phase 2 for that manifest path) so an operator's mid-edit
        syntax error doesn't silently knock a working poller offline.
        Clean manifest deletion (file gone, not just broken) still
        drops the poller — only **parse** failures preserve. A
        ``poller_reload_invalid_manifest`` algedonic event surfaces
        the failing path + parse error + preserved names so the
        operator sees the situation in events.jsonl.
        """
        if self._pollers_dir is None:
            return 0, []

        # Phase 1: discover. ``list(...)`` materializes the iterator so
        # we know all pollers' names up-front for the stale-entry
        # cleanup below. Discovery is sync and bounded by the number
        # of skills' ``pollers.json`` files (~10s, not 1000s).
        # ``invalid_manifests`` (chainlink #84) collects ``pollers.json``
        # paths whose JSON parse failed — we preserve previously
        # -installed pollers from those manifests instead of dropping
        # them.
        state_root = (
            self._home / "state" / "pollers"
            if self._home is not None else None
        )
        invalid_manifests: list[tuple[Path, str]] = []
        discovered = list(
            discover_pollers(
                self._pollers_dir,
                state_root=state_root,
                invalid_manifests=invalid_manifests,
            )
        )
        new_names = {p.name for p in discovered}
        invalid_paths = {path for path, _err in invalid_manifests}

        # Map of failing manifest_path → previously-installed names,
        # so we can identify which existing entries belong to a manifest
        # that failed to parse on this reload and must be preserved.
        # Manifests that were already in ``self._pollers`` from a
        # pre-chainlink-#84 reload will have ``manifest_path=None`` and
        # fall through to the normal stale check — operator restart
        # picks up the new ``manifest_path`` for any future reloads.
        preserved_names_by_path: dict[Path, list[str]] = {}
        for existing_name, existing_cfg in self._pollers.items():
            if (
                existing_cfg.manifest_path is not None
                and existing_cfg.manifest_path in invalid_paths
            ):
                preserved_names_by_path.setdefault(
                    existing_cfg.manifest_path, [],
                ).append(existing_name)
        preserved_names = {
            name for names in preserved_names_by_path.values() for name in names
        }

        # Phase 2: drop stale APScheduler jobs and dict entries.
        # Removed before install so a fire that lands during this loop
        # against a removed poller correctly logs poller_fire_dropped
        # (the poller really IS gone). Pollers whose manifest failed
        # to parse this reload are PRESERVED — their job stays
        # registered with the prior cron, dict entry stays put.
        for job in list(self._scheduler.get_jobs()):
            if not job.id.startswith(POLLER_CHANNEL_PREFIX):
                continue
            # ``poller:<name>`` — strip the prefix to recover the name.
            job_poller_name = job.id[len(POLLER_CHANNEL_PREFIX):]
            if job_poller_name in preserved_names:
                continue
            try:
                self._scheduler.remove_job(job.id)
            except Exception:  # noqa: BLE001 — JobLookupError, fine
                pass
        for name in list(self._pollers):
            if name in new_names or name in preserved_names:
                continue
            del self._pollers[name]

        # Phase 3: validate cron + register. Pre-populate the dict
        # entry BEFORE add_job so a fire landing during job
        # registration finds the poller. Validation failure (bad cron)
        # leaves the dict unchanged for that name.
        installed = 0
        for poller in discovered:
            try:
                trigger = CronTrigger.from_crontab(poller.cron, timezone=self._tz)
            except (ValueError, KeyError) as exc:
                log.warning(
                    "poller_invalid_cron: %s — cron=%r error=%s",
                    poller.name, poller.cron, exc,
                )
                continue
            # Pre-populate FIRST. add_job below registers the
            # callback; if APScheduler fires immediately, the
            # callback's lookup succeeds.
            self._pollers[poller.name] = poller
            self._scheduler.add_job(
                self._fire_poller,
                trigger=trigger,
                kwargs={"poller_name": poller.name},
                id=f"{POLLER_CHANNEL_PREFIX}{poller.name}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                # PR #107 review fix: was 60s, lowered to 5s. The old
                # value was equal to ``POLLER_TIMEOUT_SECONDS`` (60),
                # so a poller that hit its timeout exactly stacked
                # against the next minute's fire and APScheduler
                # silently dropped the misfire. With 5s grace + the
                # ``EVENT_JOB_MISSED`` listener installed in __init__,
                # the missed fire emits a ``poller_misfired`` event so
                # the operator sees the cadence problem instead of
                # discovering it via missing data.
                misfire_grace_time=5,
            )
            installed += 1
        log.info("pollers reloaded: %d installed from %s", installed, self._pollers_dir)

        # Build algedonic event payloads (chainlink #84). One per
        # invalid manifest, with the names of pollers preserved from
        # that path so the operator can correlate the log line with
        # which working pollers were rescued.
        invalid_events: list[dict[str, Any]] = []
        for path, err in invalid_manifests:
            invalid_events.append({
                "manifest_path": str(path),
                "error": err,
                "preserved_pollers": sorted(
                    preserved_names_by_path.get(path, []),
                ),
            })
        return installed, invalid_events

    def _dispatch_invalid_manifest_events(
        self,
        events: list[dict[str, Any]],
    ) -> None:
        """Emit ``poller_reload_invalid_manifest`` events from a sync
        context (``add_poller_jobs``). When a running event loop is
        available (server.py startup is awaited init), schedule the
        async ``log_event`` via ``create_task``. Otherwise fall back
        to ``log.warning`` so the event isn't silently lost. Mirrors
        the pattern in ``_on_job_missed``.
        """
        if not events:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            for payload in events:
                log.warning(
                    "poller_reload_invalid_manifest (no loop): %s",
                    payload,
                )
            return
        for payload in events:
            loop.create_task(
                log_event("poller_reload_invalid_manifest", **payload),
            )

    async def _fire_poller(self, *, poller_name: str) -> None:
        """APScheduler-side cron callback. Looks up the live PollerConfig
        (re-fetched on each fire to reflect any reloads) and runs it
        under the global concurrency semaphore.

        **Concurrency cap (PR #107).** ``run_poller`` is wrapped in
        ``self._poller_semaphore`` so at most ``MIMIR_MAX_CONCURRENT_POLLERS``
        (default 8) subprocess fires are in flight at once. A skill
        with ``* * * * *`` cron + many sibling skills no longer
        fork-bombs the host. When the cap is hit, late fires wait for
        a slot — if the wait stretches more than ~5s, emit a
        ``poller_concurrency_throttled`` event so the operator sees
        the saturation.

        Logs ``poller_fire_dropped`` when the registry lookup misses.
        Per-entry pre-population in ``_reinstall_pollers`` (PR #107
        review fix) makes this branch reliable: it only fires when the
        poller has genuinely been removed (skill uninstalled, config
        invalidated). Was previously also reachable transiently during
        reload due to an add-then-swap race; that race is now closed.
        """
        poller = self._pollers.get(poller_name)
        if poller is None:
            await log_event(
                "poller_fire_dropped",
                poller=poller_name,
                reason="poller_not_in_registry",
            )
            return

        # Acquire under a 5s timeout; emit the throttle event once if
        # we time out, then re-acquire without a timeout. Single
        # ``wait_for`` instead of locked()-probe-then-acquire avoids
        # the small race where the probe sees locked, the slot frees,
        # and acquire succeeds — emitting no throttle event despite
        # the contention. (PR #107 review nit.)
        wait_start = time.monotonic()
        try:
            await asyncio.wait_for(
                self._poller_semaphore.acquire(), timeout=5.0,
            )
        except asyncio.TimeoutError:
            await log_event(
                "poller_concurrency_throttled",
                poller=poller_name,
                cap=self._poller_concurrency_cap,
                wait_seconds=time.monotonic() - wait_start,
            )
            await self._poller_semaphore.acquire()

        try:
            await run_poller(poller, enqueue=self._enqueue)
        finally:
            self._poller_semaphore.release()

    def registered_pollers(self) -> list[str]:
        """Names of all currently-registered pollers. Used by the
        ``reload_pollers`` MCP tool to render the post-reload count
        and by tests."""
        return sorted(self._pollers.keys())

    def last_invalid_manifest_events(self) -> list[dict[str, Any]]:
        """Snapshot of the most-recent reload's invalid-manifest event
        payloads (chainlink #84, PR #141 review item #1). Returns a
        copy so callers can't mutate scheduler state. Read by the
        ``mcp__mimir__reload_pollers`` MCP tool to flag parse
        failures in the operator-visible reply.

        Each payload has the same shape as the
        ``poller_reload_invalid_manifest`` event in events.jsonl:
        ``{"manifest_path": str, "error": str,
        "preserved_pollers": list[str]}``. Empty list when the last
        reload had no parse failures."""
        return list(self._last_invalid_manifest_events)

    # ---- SAGA consolidation cron -------------------------------------

    # VSM: S3 (saga-internal) — nightly cron triggers consolidation.
    #      Saga's hot path: clusters similar atoms, LLM-synthesizes
    #      observations from clustered raws.
    # loop_id: 4.3
    def add_saga_consolidate_job(
        self,
        saga_client: SagaClient,
        cron_expr: str,
        *,
        home: Path | None = None,
        job_id: str = "saga-consolidate",
    ) -> bool:
        """Register the saga consolidation cron.

        ``home`` (optional) — when set, the cron reads
        ``<home>/state/identities.yaml`` at fire time and threads the
        canonical names through ``consolidate(extra_canonical_subjects
        =[...])`` so saga's P48 vocab block surfaces operator-curated
        subjects to the consolidation LLM (Option A from the P48
        identities-injection design).

        Migrated to the named-callable registry (see
        ``docs/internal/SCHEDULER_CALLABLE_JOBS.md``). The yaml side may
        override ``cron_expr`` via a ``callable: saga-consolidate``
        entry; ``cron_expr`` here is the env-var-derived default."""
        async def _consolidate() -> None:
            # Saga's legacy state-transition decay (active → fading →
            # dormant) is gone post-mimir.saga rewrite — activation
            # is computed on-demand from access_events, no state to
            # transition. Consolidation runs against the live event
            # stream directly. Load identities.yaml at FIRE TIME
            # so operator edits between runs propagate without a server
            # restart. Best effort: a missing / unparseable file just
            # means seed-only behavior for this run.
            extra_canonical_subjects: list[str] | None = None
            if home is not None:
                try:
                    from .identities import IdentityResolver
                    resolver = IdentityResolver(home)
                    resolver.reload()
                    extra_canonical_subjects = [
                        ident.canonical for ident in resolver.all_identities()
                        if ident.canonical
                    ] or None
                except Exception:  # noqa: BLE001
                    log.exception(
                        "identities.yaml read failed; consolidation "
                        "will run without extra_canonical_subjects",
                    )
                    extra_canonical_subjects = None

            try:
                payload = await saga_client.consolidate(
                    dry_run=False,
                    extra_canonical_subjects=extra_canonical_subjects,
                )
                await log_event(
                    "saga_consolidate_ok",
                    dry_run=False,
                    result=_summarize_consolidate(payload),
                    extra_canonical_subjects_count=(
                        len(extra_canonical_subjects)
                        if extra_canonical_subjects else 0
                    ),
                )
            except SagaError as exc:
                await log_event("saga_consolidate_error", error=str(exc), status=exc.status)
            except Exception as exc:  # noqa: BLE001
                await log_event("saga_consolidate_error", error=f"{type(exc).__name__}: {exc}")

        return self.register_callable(
            name=job_id,
            fn=_consolidate,
            default_cron=cron_expr,
            job_id=job_id,
            misfire_grace_time=3600,
            # Existing behavior: APScheduler defaults (max_instances=1
            # is APScheduler default, but the consolidate job didn't
            # set coalesce explicitly — APScheduler default coalesce is
            # False. Preserve that.)
            max_instances=1,
            coalesce=False,
        )

    # ---- Viability-report cron ---------------------------------------

    def add_viability_report_job(
        self,
        home: Path,
        cron_expr: str = "0 5 * * 0",
        *,
        job_id: str = "viability-report",
    ) -> bool:
        """Register the weekly viability-report check (SPEC §16
        follow-up from the 2026-05-23 VSM eval). Computes the three
        collapse indicators + write-side curation rate; writes
        ``state/reports/viability-YYYY-MM-DD.md`` and emits one
        algedonic event per threshold-crossing.

        Default cron: ``0 5 * * 0`` — 5 AM Sunday, after the weekly
        introspection-report at 4 AM Sunday. Running AFTER
        introspection means the report sees the freshest reflection
        output the agent has produced for the week. Operator can
        override via the yaml's ``callable: viability-report`` entry.
        """
        from .viability_metrics import run_scheduled_viability_report

        async def _fire() -> None:
            await run_scheduled_viability_report(home)

        return self.register_callable(
            name=job_id,
            fn=_fire,
            default_cron=cron_expr,
            job_id=job_id,
            misfire_grace_time=3600,
            max_instances=1,
            coalesce=True,
        )

    # ---- Applied-proposals audit cron --------------------------------

    def add_applied_audit_job(
        self,
        home: Path,
        cron_expr: str = "0 8 1 * *",
        *,
        job_id: str = "applied-audit",
    ) -> bool:
        """Register the monthly applied-proposals audit (VSM S4-2 —
        double-loop closure). Computes before/after signals for
        proposals applied 1–4 weeks prior, writes a report to
        ``state/reports/applied-audit-YYYY-MM-DD.md``, and emits
        ``applied_audit_ok`` / ``applied_audit_error`` algedonic events.

        Default cron: ``0 8 1 * *`` — 08:00 UTC on the 1st of each
        month. Running monthly gives the 1–4 week window meaningful
        coverage of the previous month's merged proposals. Operator
        can override via the yaml's ``callable: applied-audit`` entry.
        """
        from .reflection.applied_audit import run_scheduled_applied_audit

        async def _fire() -> None:
            await run_scheduled_applied_audit(home)

        return self.register_callable(
            name=job_id,
            fn=_fire,
            default_cron=cron_expr,
            job_id=job_id,
            misfire_grace_time=3600,
            max_instances=1,
            coalesce=True,
        )

    # ---- Proposed-changes backlog cron --------------------------------

    def add_proposed_changes_backlog_job(
        self,
        home: Path,
        cron_expr: str = "0 7 * * *",
        *,
        job_id: str = "proposed-changes-backlog",
    ) -> bool:
        """Register the daily proposed-changes backlog check. Reads
        ``state/proposed-changes.md`` and emits
        ``proposed_changes_backlog`` (negative algedonic) when the
        pending count >= 10 OR the oldest pending proposal is >= 21
        days old. Below-threshold runs are silent.

        Default cron: ``0 7 * * *`` — 07:00 UTC daily. Lands before
        typical operator work hours so the signal is in the algedonic
        block at the start of the day. Sustained backlog will trigger
        Alg-3 escalation (``algedonic_escalation``) after 5 daily
        fires; the operator gets a persistent paper-trail event in
        addition to the per-turn block surfacing.
        """
        from .reflection.proposed_changes_health import run_scheduled_backlog_check

        async def _fire() -> None:
            await run_scheduled_backlog_check(home)

        return self.register_callable(
            name=job_id,
            fn=_fire,
            default_cron=cron_expr,
            job_id=job_id,
            misfire_grace_time=3600,
            max_instances=1,
            coalesce=True,
        )

    # ---- PyPI update-check cron --------------------------------------

    def add_update_check_job(
        self,
        home: Path,
        cron_expr: str = "0 8 * * *",
        *,
        job_id: str = "update-check",
    ) -> bool:
        """Register the daily PyPI version-check (``mimir/version_check.py``).
        Queries PyPI for the latest released ``mimir`` version and emits
        ``mimir_update_available`` (positive algedonic) when a newer
        version exists. Pre-releases filtered out by default.

        Default cron: ``0 8 * * *`` — 08:00 UTC daily. Cheap (one HTTP
        GET to a CDN-backed PyPI endpoint, 5s timeout); silent when on
        latest or PyPI lookup fails. The positive polarity matches the
        spirit of the algedonic channel — new code is generally a good
        thing, surface as pleasure-side signal not pain.
        """
        from .version_check import run_scheduled_update_check

        async def _fire() -> None:
            await run_scheduled_update_check(home)

        return self.register_callable(
            name=job_id,
            fn=_fire,
            default_cron=cron_expr,
            job_id=job_id,
            misfire_grace_time=3600,
            max_instances=1,
            coalesce=True,
        )

    # ---- Index-integrity cron ----------------------------------------

    def add_index_integrity_job(
        self,
        home: Path,
        cron_expr: str = "30 4 * * *",
        *,
        job_id: str = "index-integrity",
    ) -> bool:
        """Register the daily index-integrity check (SPEC §8.3,
        §16 item 16). Runs ``check_all(home)`` against both
        ``.mimir/index.db`` and ``.mimir/saga.db`` and emits
        ``index_integrity_ok`` / ``index_integrity_failed`` events
        (algedonic-wired in ``feedback.py``).

        Default cron: ``30 4 * * *`` — 30 min after saga-consolidate's
        4am default. Running AFTER consolidation means we catch any
        corruption consolidation may have introduced before the next
        agent turn sees stale retrieval results. Operator can override
        via the yaml's ``callable: index-integrity`` entry.
        """
        from .index_integrity import run_scheduled_integrity_check

        async def _fire() -> None:
            await run_scheduled_integrity_check(home)

        return self.register_callable(
            name=job_id,
            fn=_fire,
            default_cron=cron_expr,
            job_id=job_id,
            # An hour of grace is plenty — the check is cheap (no LLM)
            # and detection-only, so a late fire doesn't have downstream
            # consequences.
            misfire_grace_time=3600,
            max_instances=1,
            coalesce=True,
        )

    # ---- Introspection-report cron -----------------------------------

    # VSM: S3* (audit) — weekly behavioral / health snapshot. Non-LLM
    #      cron: aggregates turns.jsonl + events.jsonl, writes a
    #      report file, optionally emits heartbeat_health_degraded
    #      events when the scheduled-tick pipeline degrades.
    # loop_id: 4.7
    def add_introspection_report_job(
        self,
        home: Path,
        cron_expr: str,
        *,
        days: int = 7,
        emit_algedonic: bool = True,
        health_threshold: float = 0.80,
        job_id: str = "introspection-report",
    ) -> bool:
        async def _run() -> None:
            try:
                from datetime import datetime, timezone as _tz
                from .reflection.introspection_report import (
                    aggregate, render_markdown, maybe_emit_health_event,
                )
                turns_log = home / "logs" / "turns.jsonl"
                events_log = home / "logs" / "events.jsonl"
                report = await asyncio.to_thread(
                    aggregate, turns_log, events_log, days=days,
                )
                body = render_markdown(report)
                today = datetime.now(tz=_tz.utc).strftime("%Y-%m-%d")
                out = home / "state" / "reports" / f"introspection-{today}.md"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(body, encoding="utf-8")

                emitted = False
                if emit_algedonic:
                    emitted = maybe_emit_health_event(
                        report, events_log, threshold=health_threshold,
                    )

                await log_event(
                    "introspection_report_ok",
                    output=str(out),
                    days=days,
                    pipeline_success_rate=report.heartbeat.success_rate,
                    fired=report.heartbeat.fired,
                    successful=report.heartbeat.successful,
                    algedonic_emitted=emitted,
                )
            except Exception as exc:  # noqa: BLE001
                await log_event(
                    "introspection_report_error",
                    error=f"{type(exc).__name__}: {exc}",
                )

        return self.register_callable(
            name=job_id,
            fn=_run,
            default_cron=cron_expr,
            job_id=job_id,
            misfire_grace_time=3600,
            max_instances=1,
            coalesce=False,
        )

    # ---- Commitments due-check cron ----------------------------------

    # VSM: S3 (commitments-internal) — periodic sweep of the
    #      commitments store. Emits ``commitment_due`` (positive,
    #      first-occurrence-only) on records whose due window opened,
    #      and ``commitment_expired`` (negative, first-occurrence-only)
    #      on records whose due window has fully elapsed. Marks each
    #      via store.deliver() / store.expire() so the next sweep
    #      doesn't re-emit.
    # loop_id: 4.10 (commitments Phase 2b)
    def add_commitments_due_check_job(
        self,
        commitments_store: "CommitmentsStore",
        cron_expr: str,
        *,
        snooze_pileup_threshold: int = 3,
        job_id: str = "commitments-due-check",
    ) -> bool:
        """Register the commitments due-check cron.

        ``commitments_store`` is ``Agent._commitments`` — the per-agent
        ``CommitmentsStore`` instance (path = ``<home>/.mimir/
        commitments.jsonl``). The job is in-process; it reads the
        store and writes both to the store (deliver/expire) and to
        events.jsonl (algedonic events).

        Default cron is operator-tuneable via the env var
        ``MIMIR_COMMITMENTS_DUE_CHECK_CRON`` plumbed through
        ``Config.from_env``. ``*/5 * * * *`` (every 5 minutes) is the
        ship default — fine-grained enough that an operator who
        commits to "remind me at 14:00" sees the reminder within
        5 minutes of 14:00, coarse-grained enough that the sweep cost
        (replay + 0–N deliver/expire writes) is negligible relative
        to the rest of the agent loop.
        """
        async def _run() -> None:
            try:
                from .commitments.poller import check_due_and_expired
                result = await check_due_and_expired(
                    commitments_store,
                    snooze_pileup_threshold=snooze_pileup_threshold,
                )
                # Rollup event — single line per sweep, not per record.
                # Only emit when SOMETHING happened (due / expired /
                # pileup) to keep events.jsonl from accruing one no-op
                # record per poll tick.
                if (result.due_emitted or result.expired_emitted
                        or result.snooze_pileup_emitted):
                    await log_event(
                        "commitments_due_check_ok",
                        due_emitted=result.due_emitted,
                        expired_emitted=result.expired_emitted,
                        snooze_pileup_emitted=result.snooze_pileup_emitted,
                        scanned=result.scanned,
                    )
            except Exception as exc:  # noqa: BLE001
                await log_event(
                    "commitments_due_check_error",
                    error=f"{type(exc).__name__}: {exc}",
                )

        return self.register_callable(
            name=job_id,
            fn=_run,
            default_cron=cron_expr,
            job_id=job_id,
            misfire_grace_time=300,
            max_instances=1,
            coalesce=True,  # if the agent was paused, don't catch up
        )

    # ---- OAuth usage poller cron -------------------------------------

    # VSM: S3 — non-LLM background poll for plan-window quota.
    #      Reads ~/.claude/.credentials.json (operator-minted via
    #      ``claude /login``), GETs Anthropic's /api/oauth/usage, and
    #      writes per-window snapshots into the shared RateLimitStore.
    #      Refreshes its own access token via the standard OAuth2
    #      refresh_token grant (Claude Code CLI's auto-refresh is
    #      broken on headless / copied-creds boxes — see
    #      mimir/oauth_usage_poller.py for context).
    # loop_id: 4.9
    def add_oauth_usage_poll_job(
        self,
        rate_limit_store: Any,
        cron_expr: str,
        credentials_path: Path,
        *,
        refresh_warn_days: int = 25,
        turns_log_path: Path | None = None,
        job_id: str = "oauth-usage-poll",
    ) -> bool:
        """Register the plan-window quota poller. Returns False on
        empty effective cron (env-var default empty AND no yaml override).
        Migrated to the named-callable registry.

        ``turns_log_path`` (chainlink #17): when set, enables the
        cost-rate-back-derived 5h estimator that fires when the
        layer-(a) anomaly detector rejects an endpoint reading.
        Without it (None), the layer-(a) fallback persists the prior
        trusted 5h value indefinitely on long endpoint glitches —
        same as before chainlink #17."""
        from .oauth_usage_poller import PollerConfig, poll_once

        cfg = PollerConfig(
            credentials_path=credentials_path,
            refresh_warn_days=refresh_warn_days,
            turns_log_path=turns_log_path,
        )

        async def _run() -> None:
            try:
                await poll_once(cfg, rate_limit_store)
            except Exception as exc:  # noqa: BLE001
                # poll_once is meant to swallow its own errors via
                # log_event; this is a defensive belt — if something
                # leaks (e.g. import-time bug) we still surface it.
                await log_event(
                    "oauth_usage_failed",
                    stage="job_wrapper",
                    error=f"{type(exc).__name__}: {exc}",
                )

        return self.register_callable(
            name=job_id,
            fn=_run,
            default_cron=cron_expr,
            job_id=job_id,
            # Quota data is best-effort — don't backfill missed runs.
            misfire_grace_time=60,
            # If the poller is already mid-run when the next tick fires
            # (network slow), skip rather than overlap.
            max_instances=1,
            coalesce=True,
        )

    # ---- Minimax coding-plan usage poll cron -------------------------

    # Same shape as add_oauth_usage_poll_job but for Minimax deployments.
    # Polls Minimax's ``coding_plan/remains`` endpoint and writes
    # ``minimax_five_hour`` / ``minimax_seven_day`` snapshots that
    # :class:`mimir.billing.MinimaxQuotaProvider` reads. Independent
    # of the Anthropic OAuth poller — they can both be registered on
    # a hybrid deployment, but typical deployments register one or
    # the other based on which gateway the agent talks to.
    def add_minimax_usage_poll_job(
        self,
        rate_limit_store: Any,
        cron_expr: str,
        api_key: str,
        *,
        model_name: str = "MiniMax-M*",
        job_id: str = "minimax-usage-poll",
    ) -> bool:
        """Register the Minimax plan-window usage poller. Returns
        ``False`` on empty effective cron (env-var default empty AND
        no yaml override), matching ``add_oauth_usage_poll_job``'s
        opt-out shape.

        ``model_name`` defaults to ``"MiniMax-M*"`` — the chat-models
        bucket. Override for deployments on the speech / music / image
        plans (each has its own per-plan quota in the response).
        """
        from .minimax_usage_poller import MinimaxPollerConfig, poll_once

        cfg = MinimaxPollerConfig(
            api_key=api_key,
            model_name=model_name,
        )

        async def _run() -> None:
            try:
                await poll_once(cfg, rate_limit_store)
            except Exception as exc:  # noqa: BLE001
                # poll_once is meant to swallow its own errors via
                # log_event; this is a defensive belt — if something
                # leaks (e.g. import-time bug) we still surface it.
                await log_event(
                    "minimax_usage_failed",
                    stage="job_wrapper",
                    error=f"{type(exc).__name__}: {exc}",
                )

        return self.register_callable(
            name=job_id,
            fn=_run,
            default_cron=cron_expr,
            job_id=job_id,
            misfire_grace_time=60,
            max_instances=1,
            coalesce=True,
        )

    # ---- bind-mount health probe cron --------------------------------

    # VSM: S3 — non-LLM safety probe for the VirtioFS bind-mount stale-
    #      inode failure mode (see docs/internal/BIND_MOUNT_HEALTH_PROBE.md). Spawns a
    #      ``pwd`` subprocess in MIMIR_HOME; nonzero exit or "deleted"
    #      in stderr means the bind is broken and the agent should
    #      self-restart so Docker's restart policy can re-mount.
    # loop_id: 4.10
    def add_health_probe_job(
        self,
        home: Path,
        events_log: Path,
        cron_expr: str,
        *,
        max_restarts_per_hour: int = 3,
        job_id: str = "bind-mount-health-probe",
    ) -> bool:
        """Register the bind-mount health probe cron. Returns False on
        empty / unset cron expression so callers can no-op out without
        an exception.

        The probe is lightweight (a single subprocess.run) and self-
        gates on VirtioFS detection, so registering it on a non-
        VirtioFS host is harmless — every tick will short-circuit on
        the mountinfo check.

        Migrated to the named-callable registry."""
        from .health_probe import HealthProbeConfig, probe_once

        cfg = HealthProbeConfig(
            home=home,
            events_log=events_log,
            max_restarts_per_hour=max_restarts_per_hour,
        )

        async def _run() -> None:
            try:
                await probe_once(cfg)
            except Exception as exc:  # noqa: BLE001
                # probe_once is meant to swallow its own errors via
                # log_event; this is a defensive belt — if something
                # leaks (e.g. import-time bug) we still surface it
                # rather than letting the cron job die silently.
                await log_event(
                    "bind_mount_probe_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

        return self.register_callable(
            name=job_id,
            fn=_run,
            default_cron=cron_expr,
            job_id=job_id,
            # If the probe takes longer than 30s (kernel hang in the
            # subprocess.run path itself), let APScheduler skip the
            # next tick rather than queue them up.
            misfire_grace_time=30,
            # Never overlap probes.
            max_instances=1,
            coalesce=True,
        )

    # ---- identities-populate cron -----------------------------------

    # VSM: S3 — non-LLM background scrape of connected bridges into
    #      ``state/identities.yaml``. Fires daily; the populator is
    #      idempotent (rerun → zero deltas, operator-set fields
    #      preserved). Optional per chainlink #44; default empty cron
    #      means "operator opt-in via MIMIR_IDENTITIES_POPULATE_CRON".
    # loop_id: 4.11
    def add_identities_populate_job(
        self,
        home: Path,
        cron_expr: str,
        channel_registry: Any,
        *,
        job_id: str = "identities-populate",
    ) -> bool:
        """Register the identities-populator cron. Returns False on
        empty / unset cron expression so callers can no-op out without
        an exception.

        ``channel_registry`` is consulted at fire time (not registration
        time) so bridges that reconnect mid-day still get scraped on
        the next run. Bridges are looked up by ``bridge.name`` —
        ``"discord"`` and ``"slack"`` today; absent ones contribute
        nothing.

        Migrated to the named-callable registry."""
        async def _run() -> None:
            try:
                from .identities_populator import populate_all
                discord_bridge = None
                slack_bridge = None
                for bridge in channel_registry.bridges():
                    name = getattr(bridge, "name", None)
                    if name == "discord":
                        discord_bridge = bridge
                    elif name == "slack":
                        slack_bridge = bridge
                result = await populate_all(
                    home,
                    discord_bridge=discord_bridge,
                    slack_bridge=slack_bridge,
                    dry_run=False,
                )
                await log_event(
                    "identities_populate_ok",
                    discord=discord_bridge is not None,
                    slack=slack_bridge is not None,
                    result=result,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort scheduled job
                await log_event(
                    "identities_populate_error",
                    error=f"{type(exc).__name__}: {exc}",
                )

        return self.register_callable(
            name=job_id,
            fn=_run,
            default_cron=cron_expr,
            job_id=job_id,
            # Bridge scrapes can take a minute or two on large
            # workspaces — give them room but don't backfill missed runs.
            misfire_grace_time=300,
            max_instances=1,
            coalesce=True,
        )

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
    summary = {
        k: payload[k]
        for k in (
            "clusters_processed", "atoms_merged", "atoms_retired",
            "duration_s", "candidates_scanned", "clusters_found",
            "clusters_consolidated", "observations_created",
            "triples_stored", "contradicts_stored",
        )
        if k in payload
    }
    dedup = payload.get("dedup")
    if isinstance(dedup, dict):
        summary["dedup"] = {
            "candidates_scanned": dedup.get("candidates_scanned", 0),
            "clusters_formed": dedup.get("clusters_formed", 0),
            "canonicals_kept": len(dedup.get("canonicals_kept", []) or []),
            "duplicates_tombstoned": len(
                dedup.get("duplicates_tombstoned", []) or []
            ),
            "threshold": dedup.get("threshold"),
        }
    return summary


