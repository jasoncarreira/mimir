"""Cultivate's server lifecycle contributions."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from mimir.event_logger import log_event_sync

log = logging.getLogger(__name__)


async def capture_controller_source_commit(app: Any) -> None:
    if "worklink_controller_source_commit" in app:
        return
    controller_source_commit = None
    if app.get("check_worker_executor_health"):
        marker = Path(sys.prefix) / "controller-source-commit"
        if marker.exists():
            try:
                commit = marker.read_text(encoding="utf-8").strip()
                if re.fullmatch(r"[0-9a-f]{40}", commit):
                    controller_source_commit = commit
            except (OSError, UnicodeError):
                pass
        else:
            repo = os.environ.get("WORKLINK_REPO")
            if repo:
                try:
                    result = await asyncio.to_thread(
                        subprocess.run, ["git", "-C", repo, "rev-parse", "--verify", "HEAD"],
                        capture_output=True, text=True, check=True, timeout=5,
                    )
                    commit = result.stdout.strip()
                    if re.fullmatch(r"[0-9a-f]{40}", commit):
                        controller_source_commit = commit
                except (OSError, subprocess.SubprocessError):
                    pass
    app["worklink_controller_source_commit"] = controller_source_commit


async def executor_health(app: Any) -> dict[str, object]:
    from .worklink.worker_client import DEFAULT_EXECUTOR_SOCKET, verify_executor_identity

    if not app.get("check_worker_executor_health"):
        return {}
    executor_source_commit = None
    result: dict[str, object] = {}
    if DEFAULT_EXECUTOR_SOCKET.exists():
        try:
            executor_source_commit = await verify_executor_identity()
        except (OSError, RuntimeError, ValueError) as exc:
            result.update(ok=False, error=str(exc))
    controller_source_commit = app.get("worklink_controller_source_commit")
    source_status = "unknown"
    if executor_source_commit is not None and controller_source_commit is not None:
        source_status = "matched" if executor_source_commit == controller_source_commit else "mismatched"
    if source_status == "mismatched":
        log.warning(
            "Worklink executor source drift: executor=%s controller=%s; rebuild the root executor image",
            executor_source_commit, controller_source_commit,
        )
    result.update(
        worklink_executor_source_commit=executor_source_commit,
        worklink_controller_source_commit=controller_source_commit,
        worklink_executor_source_status=source_status,
    )
    return result


def process_identity(process_session_id: str) -> str:
    agent_id = f"mimir-worklink:{process_session_id}"
    os.environ["MIMIR_WORKLINK_AGENT_ID"] = agent_id
    return agent_id


async def repository_binding_alerts(log_event: Any) -> None:
    from mimir.access_control import repo_binding_startup_alerts

    for alert in await asyncio.to_thread(repo_binding_startup_alerts):
        await log_event("github_repo_binding_attention_required", **alert)


def channel_drained(channel_id: str, scheduler: Any) -> None:
    if channel_id == "poller:github-activity":
        scheduler.trigger_poller("github-activity", reason="remediation_queue_drained")


async def configure_worklink(scheduler: Any, home: Path, alert_channel: str | None, channels: Any, log_event: Any) -> None:
    from . import scheduler as jobs

    for job, install in (
        ("worklink-reaper", lambda: jobs.add_reaper_job(
            scheduler, home, cron_expr=os.environ.get("MIMIR_WORKLINK_REAPER_CRON", "*/30 * * * *"))),
        ("worklink-factory-park-alert", lambda: jobs.add_factory_park_alert_job(
            scheduler, home, alert_channel, channels)),
    ):
        if job == "worklink-reaper" and not (home / "worklink.yaml").is_file():
            continue
        try:
            install()
        except ValueError as exc:
            await log_event("scheduler_invalid_cron", error=str(exc), job=job)
    lease_root = os.environ.get("MIMIR_PR_CHECKOUT_LEASE_ROOT", "").strip()
    if lease_root:
        try:
            from .repository import configured_pr_checkout_lease_root

            jobs.add_pr_checkout_lease_reaper_job(
                scheduler,
                lease_root=configured_pr_checkout_lease_root(),
                cron_expr=os.environ.get("MIMIR_PR_CHECKOUT_LEASE_REAPER_CRON", "*/15 * * * *"),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            await log_event("scheduler_invalid_pr_checkout_lease_reaper", error=str(exc), job="pr-checkout-lease-reaper")


async def validate_worklink(home: Path, log_event: Any) -> None:
    config_path = home / "worklink.yaml"
    if not config_path.exists():
        return
    try:
        from .worklink.backends.registry import BackendRegistry, WorklinkConfig

        BackendRegistry(WorklinkConfig.load(config_path))
    except Exception as exc:  # noqa: BLE001 - invalid operator config must not abort boot
        log.warning("Worklink disabled by invalid config %s: %s", config_path, exc)
        await log_event(
            "worklink_config_invalid", path=str(config_path), error=str(exc)[:500],
            fix=f"correct {config_path} before Worklink dispatch can resume",
        )


def reattach_inflight_worklink_runs(
    home: Path, *, popen: Any = None, event_logger: Any = None,
) -> list[int]:
    """Reconcile dead workers and dispatch retained runs after a restart."""
    from .worklink.control import reconcile_run_states
    from .worklink.factory_state import (
        factory_process_is_verified_dead, list_factory_records, report_retained_factory_records,
    )
    from .worklink.run_state import reattach_dispatch_argv
    from .worklink.dispatch_failures import dispatch_failure_state_dir, record_failure
    from .worklink.detached_dispatch import launch_detached_worklink

    spawn = popen or subprocess.Popen
    emit_event = event_logger or log_event_sync
    failures: list[dict[str, Any]] = []

    def emit(event: str, **payload: Any) -> None:
        try:
            emit_event(event, **payload)
        except Exception:  # noqa: BLE001 - telemetry cannot block startup recovery
            pass

    states = reconcile_run_states(home, event_logger=emit)
    try:
        listing = list_factory_records(home)
        report_retained_factory_records(home, event_logger=emit, records=listing.records)
        for load_failure in listing.failures:
            failure = {
                "issue_id": None, "reason": "factory_record_load_failed",
                "path": str(load_failure.path), "error": load_failure.reason[:500],
            }
            failures.append(failure)
            emit("worklink_reattach_dispatch_failed", **failure)
        factory_records = [
            record for record in listing.records
            if factory_process_is_verified_dead(record)
            and record.controller_phase not in {"failed", "parked", "terminal", "stopped"}
            and (record.status is None or not record.status.is_terminal)
        ]
    except Exception as exc:
        factory_records = []
        failure = {"issue_id": None, "reason": "factory_record_enumeration_failed", "error": str(exc)[:500]}
        failures.append(failure)
        emit("worklink_reattach_dispatch_failed", **failure)
    repo = os.environ.get("WORKLINK_REPO")
    if not repo or (not states and not factory_records):
        emit("worklink_reattach_attempted", examined=0, dispatched_issue_ids=[], failures=failures)
        return []
    run_bin = shlex.split(os.environ.get("WORKLINK_RUN_BIN") or "mimir")
    state_dir = home / "state" / "worklink" / "runs"
    dispatched: list[int] = []
    for state in states:
        if state.compute_name == "local_subprocess":
            continue
        argv = reattach_dispatch_argv(run_bin, home, repo, state.issue_id)
        log_path = state_dir / f"reattach-{state.issue_id}.log"
        try:
            launch_detached_worklink(
                command="run", issue_id=state.issue_id, home=home, repo=repo,
                state_dir=state_dir, run_bin=run_bin, popen=spawn, argv=argv,
                log_name=f"reattach-{state.issue_id}.log",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            record_failure(
                dispatch_failure_state_dir(home), issue_id=state.issue_id,
                attempt=state.attempt, exit_status=1,
                error=f"retained Worklink reattach spawn failed: {exc}",
                log_path=str(log_path), preserved_ref=state.branch or None,
                work_path=state.checkout or None,
            )
            failure = {"issue_id": state.issue_id, "reason": "reattach_spawn_failed", "error": str(exc)[:500]}
            failures.append(failure)
            emit("worklink_reattach_dispatch_failed", **failure)
            continue
        dispatched.append(state.issue_id)
    for record in factory_records:
        argv = [*run_bin, "worklink", "run-epic", str(record.issue_id), "--autonomous",
                "--home", str(home), "--repo", repo]
        log_path = state_dir / f"factory-recover-{record.issue_id}.log"
        try:
            launch_detached_worklink(
                command="run-epic", issue_id=record.issue_id, home=home, repo=repo,
                state_dir=state_dir, run_bin=run_bin, popen=spawn, argv=argv,
                log_name=f"factory-recover-{record.issue_id}.log",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            record_failure(
                dispatch_failure_state_dir(home), issue_id=record.issue_id,
                attempt=record.attempt, exit_status=1,
                error=f"retained factory recovery spawn failed: {exc}; transcript={record.transcript or '(none)'}",
                log_path=str(log_path), preserved_ref=record.branch,
                run_id=record.run_id, work_path=record.sandbox,
                transcript_path=record.transcript,
            )
            failure = {"issue_id": record.issue_id, "reason": "factory_recovery_spawn_failed", "error": str(exc)[:500]}
            failures.append(failure)
            emit("worklink_reattach_dispatch_failed", **failure)
            continue
        dispatched.append(record.issue_id)
    emit("worklink_reattach_attempted", examined=len(states) + len(factory_records),
         dispatched_issue_ids=dispatched, failures=failures)
    return dispatched


async def reconcile_startup(home: Path, log_event: Any) -> None:
    try:
        resumed = reattach_inflight_worklink_runs(home)
        await log_event("worklink_reattach_dispatched", issues=resumed)
    except Exception as exc:  # noqa: BLE001 - restart reconciliation must not abort boot
        await log_event("worklink_reattach_dispatch_failed", error=str(exc))


def release_claims(home: Path, drain_timeout_seconds: float, agent_id: str | None) -> tuple[list[Any], list[Any]]:
    from .worklink.autonomy import release_claims_for_graceful_shutdown

    timeout = min(5.0, float(drain_timeout_seconds)) if drain_timeout_seconds > 0 else 5.0
    if not agent_id:
        raise ValueError("Worklink server generation identity is unavailable")
    return release_claims_for_graceful_shutdown(home, agent_id=agent_id, timeout_s=timeout)
