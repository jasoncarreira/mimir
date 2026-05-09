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
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .event_logger import log_event
from .models import AgentEvent
from .pollers import PollerConfig, discover_pollers, run_poller
from .saga_client import SagaClient, SagaError

log = logging.getLogger(__name__)

UTC = timezone.utc

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
    ) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
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
        # ``SCHEDULER_CALLABLE_JOBS.md`` for the design.
        self._callables: dict[str, _CallableDef] = {}
        # Pollers framework (chainlink #3). Discovered from
        # ``<home>/.claude/skills/**/pollers.json`` at startup +
        # on ``reload_pollers`` MCP tool. Skill directory drop is
        # the only install path; no mimir release required to add
        # a new poller. ``_pollers_dir`` is None until
        # ``add_poller_jobs`` runs (most installs no-op cleanly).
        self._pollers_dir: Path | None = None
        self._pollers: dict[str, PollerConfig] = {}

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
            if job.id.startswith("scheduler:"):
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
                    CronTrigger.from_crontab(job.cron, timezone=UTC)
                except (ValueError, KeyError) as exc:
                    raise ValueError(
                        f"invalid cron expression {job.cron!r}: {exc}"
                    ) from exc
        else:
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
            trigger = CronTrigger.from_crontab(effective_cron, timezone=UTC)
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
        full contract."""
        self._pollers_dir = skills_dir
        return self._reinstall_pollers()

    async def reload_pollers(self) -> int:
        """Re-scan the pollers directory and re-install. Called by the
        ``mcp__mimir__reload_pollers`` MCP tool after the agent installs
        a new skill. No-op (returns 0) when ``add_poller_jobs`` was
        never called (no skills_dir wired)."""
        if self._pollers_dir is None:
            return 0
        return await asyncio.to_thread(self._reinstall_pollers)

    def _reinstall_pollers(self) -> int:
        """Wipe + re-discover + re-register. Sync — runs on the
        APScheduler thread or via ``to_thread`` from async callers."""
        if self._pollers_dir is None:
            return 0
        # Drop existing poller jobs by id-prefix.
        for job in list(self._scheduler.get_jobs()):
            if job.id.startswith("poller:"):
                try:
                    self._scheduler.remove_job(job.id)
                except Exception:  # noqa: BLE001 — JobLookupError, fine
                    pass
        self._pollers = {}

        installed = 0
        # Filing-rules-aligned: cursor files live under
        # ``<home>/state/pollers/<name>/``, NOT inside the skill dir.
        # Skill dir is the deployable artifact (resettable via
        # seed_skills); state/pollers/ is the persistent runtime data.
        # When ``self._home`` is None (tests), fall back to skill_dir.
        state_root = (
            self._home / "state" / "pollers"
            if self._home is not None else None
        )
        for poller in discover_pollers(self._pollers_dir, state_root=state_root):
            try:
                trigger = CronTrigger.from_crontab(poller.cron, timezone=UTC)
            except (ValueError, KeyError) as exc:
                log.warning(
                    "poller_invalid_cron: %s — cron=%r error=%s",
                    poller.name, poller.cron, exc,
                )
                continue
            self._scheduler.add_job(
                self._fire_poller,
                trigger=trigger,
                kwargs={"poller_name": poller.name},
                id=f"poller:{poller.name}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                # A poller that takes 65s and fires every minute would
                # otherwise stack up; with max_instances=1+coalesce the
                # next tick is dropped instead of overlapping. Same
                # shape as our oauth-usage-poll registration.
                misfire_grace_time=60,
            )
            self._pollers[poller.name] = poller
            installed += 1
        log.info("pollers reloaded: %d installed from %s", installed, self._pollers_dir)
        return installed

    async def _fire_poller(self, *, poller_name: str) -> None:
        """APScheduler-side cron callback. Looks up the live PollerConfig
        (re-fetched on each fire to reflect any reloads) and runs it.
        Always logs a ``poller_fire_dropped`` event when the lookup
        misses — should never happen but it's the diagnosable case."""
        poller = self._pollers.get(poller_name)
        if poller is None:
            await log_event(
                "poller_fire_dropped",
                poller=poller_name,
                reason="poller_not_in_registry",
            )
            return
        await run_poller(poller, enqueue=self._enqueue)

    def registered_pollers(self) -> list[str]:
        """Names of all currently-registered pollers. Used by the
        ``reload_pollers`` MCP tool to render the post-reload count
        and by tests."""
        return sorted(self._pollers.keys())

    # ---- SAGA consolidation cron -------------------------------------

    # VSM: S3 (saga-internal) — nightly cron triggers consolidation.
    #      Saga's hot path: clusters similar atoms, LLM-synthesizes
    #      observations, decays source stability.
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
        ``SCHEDULER_CALLABLE_JOBS.md``). The yaml side may
        override ``cron_expr`` via a ``callable: saga-consolidate``
        entry; ``cron_expr`` here is the env-var-derived default."""
        async def _consolidate() -> None:
            # Step 1: decay BEFORE consolidation. Decay recomputes
            # retrievability, runs state transitions (active → fading →
            # dormant), compacts profiles, and surfaces forgetting
            # candidates. Running it first means consolidation sees
            # the fresher stability signal when deciding which atoms
            # to merge. Forgetting itself stays manual (/v1/forget) so
            # an operator or the agent reviews candidates first.
            try:
                decay_payload = await saga_client.decay()
                await log_event(
                    "saga_decay_ok",
                    result=_summarize_decay(decay_payload),
                )
            except SagaError as exc:
                await log_event(
                    "saga_decay_error",
                    error=str(exc),
                    status=getattr(exc, "status", None),
                )
                # Don't bail — consolidation can still run on the
                # un-decayed state; we just log and continue.
            except Exception as exc:  # noqa: BLE001
                await log_event(
                    "saga_decay_error",
                    error=f"{type(exc).__name__}: {exc}",
                )

            # Step 2: consolidation. Load identities.yaml at FIRE TIME
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
                from .skills.reflection.introspection_report import (
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
        job_id: str = "oauth-usage-poll",
    ) -> bool:
        """Register the plan-window quota poller. Returns False on
        empty effective cron (env-var default empty AND no yaml override).
        Migrated to the named-callable registry."""
        from .oauth_usage_poller import PollerConfig, poll_once

        cfg = PollerConfig(
            credentials_path=credentials_path,
            refresh_warn_days=refresh_warn_days,
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

    # ---- bind-mount health probe cron --------------------------------

    # VSM: S3 — non-LLM safety probe for the VirtioFS bind-mount stale-
    #      inode failure mode (see BIND_MOUNT_HEALTH_PROBE.md). Spawns a
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
    return {
        k: payload[k]
        for k in ("clusters_processed", "atoms_merged", "atoms_retired", "duration_s")
        if k in payload
    }


def _summarize_decay(payload: Any) -> dict:
    """Pick the salient fields out of saga.decay.run_decay_cycle's
    return dict for the saga_decay_ok event payload. Forgetting
    candidate counts surface here when present so the agent's
    feedback block can hint at review-worthy items."""
    if not isinstance(payload, dict):
        return {"raw": str(payload)[:200]}
    # Keys come from saga/decay.py:run_decay_cycle's `summary` dict.
    keys = (
        "atoms_retrievability_updated",
        "atoms_faded", "atoms_dormanted", "atoms_protected",
        "atoms_compacted", "tokens_freed",
        "budget_before_pct", "budget_after_pct",
        "total_active", "total_fading", "total_dormant",
        "forgetting_candidates", "forgetting_actions",
        "elapsed_seconds",
    )
    out: dict = {}
    for k in keys:
        if k in payload:
            v = payload[k]
            if isinstance(v, list):
                out[k] = len(v)
            elif isinstance(v, dict):
                out[k] = {kk: vv for kk, vv in v.items()
                          if not isinstance(vv, (list, dict))}
            else:
                out[k] = v
    return out
