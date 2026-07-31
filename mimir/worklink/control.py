"""Operator status, stop, and startup reconciliation for leaf Worklink runs."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Iterator, Sequence

from .compute import LaunchHandle, LocalSubprocessComputeBackend
from .run_state import (
    WorklinkRunState,
    clear_run_state,
    elapsed_seconds,
    list_run_states,
    load_run_state,
    process_identity_verified,
    process_is_alive,
    runs_dir,
)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
EventLogger = Callable[..., None]


@dataclass(frozen=True)
class WorklinkStatus:
    issue_id: int
    classification: str
    label_in_progress: bool
    state: WorklinkRunState | None
    elapsed_s: float | None
    disagreement: str | None = None


@dataclass(frozen=True)
class WorklinkStopResult:
    issue_id: int
    stopped: bool
    state_cleared: bool = False
    claim_released: bool = False
    label_cleared: bool = False
    reason: str | None = None


def _runner(home: Path, chainlink_bin: str = "chainlink") -> Runner:
    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(args), cwd=home, capture_output=True, text=True, check=False)

    return run


def _open_worklink_issues(run: Runner, chainlink_bin: str) -> dict[int, set[str]]:
    result = run([chainlink_bin, "issue", "list", "--status", "open", "--json"])
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout).strip() or "chainlink issue list failed"
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("chainlink issue list returned invalid JSON") from exc
    items = payload if isinstance(payload, list) else payload.get("issues", [])
    issues: dict[int, set[str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            issue_id = int(item.get("id", item.get("number")))
        except (TypeError, ValueError):
            continue
        labels = {
            str(label.get("name") if isinstance(label, dict) else label)
            for label in (item.get("labels") or ())
        }
        if any(label.startswith("worklink:") for label in labels):
            issues[issue_id] = labels
    return issues


def worklink_status(
    home: Path,
    *,
    issue_ids: Sequence[int] = (),
    runner: Runner | None = None,
    chainlink_bin: str = "chainlink",
    now: datetime | None = None,
) -> list[WorklinkStatus]:
    """Classify Worklink leaves from run state and current Chainlink labels."""
    states = {state.issue_id: state for state in list_run_states(home)}
    issues = _open_worklink_issues(runner or _runner(home, chainlink_bin), chainlink_bin)
    selected = set(issue_ids) if issue_ids else set(states) | set(issues)
    rows: list[WorklinkStatus] = []
    for issue_id in sorted(selected):
        state = states.get(issue_id)
        in_progress = "worklink:in-progress" in issues.get(issue_id, set())
        disagreement = None
        if state is not None:
            classification = "running" if process_is_alive(state) else "orphaned"
            if not in_progress:
                disagreement = "run state present but worklink:in-progress label absent"
            elapsed = elapsed_seconds(state, now=now)
        elif in_progress:
            classification = "unrecorded"
            disagreement = "worklink:in-progress label present but run state absent"
            elapsed = None
        else:
            classification = "clean"
            elapsed = None
        rows.append(
            WorklinkStatus(
                issue_id=issue_id,
                classification=classification,
                label_in_progress=in_progress,
                state=state,
                elapsed_s=elapsed,
                disagreement=disagreement,
            )
        )
    return rows


@contextmanager
def _claim_mutex(home: Path) -> Iterator[None]:
    path = home / "state" / "worklink" / "chainlink-claim.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def stop_worklink(
    home: Path,
    issue_id: int,
    *,
    runner: Runner | None = None,
    chainlink_bin: str = "chainlink",
) -> WorklinkStopResult:
    """Safely cancel one verified live run and clear its operator state."""
    run = runner or _runner(home, chainlink_bin)
    with _claim_mutex(home):
        state = load_run_state(home, issue_id)
        if state is None or state.phase != "spawned" or not process_is_alive(state):
            return WorklinkStopResult(issue_id, False, reason="no live run")
        if not process_identity_verified(state):
            return WorklinkStopResult(
                issue_id, False, reason="live PID could not be verified; refusing to signal it"
            )

        handle = LaunchHandle(
            state.handle_substrate,
            state.handle_identifier,
            state.process_start_ticks,
        )
        try:
            asyncio.run(LocalSubprocessComputeBackend().cancel(handle))
        except (KeyError, RuntimeError, OSError) as exc:
            return WorklinkStopResult(issue_id, False, reason=str(exc))

        clear_run_state(home, issue_id)
        state_cleared = load_run_state(home, issue_id) is None
        release = run([chainlink_bin, "locks", "release", str(issue_id)])
        unlabel = run(
            [chainlink_bin, "issue", "unlabel", str(issue_id), "worklink:in-progress"]
        )
        return WorklinkStopResult(
            issue_id,
            True,
            state_cleared=state_cleared,
            claim_released=release.returncode == 0,
            label_cleared=unlabel.returncode == 0,
        )


def reconcile_run_states(
    home: Path,
    *,
    event_logger: EventLogger | None = None,
    now: datetime | None = None,
) -> list[WorklinkRunState]:
    """Clear dead local run records without allowing one bad file to fail startup."""
    alive: list[WorklinkRunState] = []
    known_paths = {str(state.issue_id): state for state in list_run_states(home)}
    directory = runs_dir(home)
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            if path.stem not in known_paths:
                _emit_reconcile_event(
                    event_logger,
                    "worklink_run_state_reconcile_failed",
                    path=str(path),
                    reason="unparseable run state",
                )
    for state in known_paths.values():
        if state.compute_name != "local_subprocess" or process_is_alive(state):
            alive.append(state)
            continue
        clear_run_state(home, state.issue_id)
        _emit_reconcile_event(
            event_logger,
            "worklink_run_orphaned",
            issue_id=state.issue_id,
            attempt=state.attempt,
            elapsed_s=round(elapsed_seconds(state, now=now), 3),
            reaped=load_run_state(home, state.issue_id) is None,
        )
    return alive


def _emit_reconcile_event(
    event_logger: EventLogger | None, event: str, **payload: Any
) -> None:
    if event_logger is None:
        return
    try:
        event_logger(event, **payload)
    except Exception:
        # Reconciliation is diagnostic recovery and must never fail startup.
        return
