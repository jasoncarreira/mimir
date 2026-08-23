"""Operator status, stop, and startup reconciliation for leaf Worklink runs."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Iterator, Sequence

from .compute import LaunchHandle, LocalSubprocessComputeBackend
from .factory_state import (
    archive_factory_record,
    factory_process_is_alive,
    load_factory_record,
    save_factory_record,
)
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

_CHAINLINK_COMMAND_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class WorklinkStatus:
    issue_id: int
    classification: str
    label_in_progress: bool | None
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
        try:
            return subprocess.run(
                list(args),
                cwd=home,
                capture_output=True,
                text=True,
                check=False,
                timeout=_CHAINLINK_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode("utf-8", "replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", "replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            timeout_message = (
                f"chainlink timed out after {_CHAINLINK_COMMAND_TIMEOUT_SECONDS}s"
            )
            stderr = f"{stderr.rstrip()}\n{timeout_message}" if stderr else timeout_message
            return subprocess.CompletedProcess(args, 124, stdout=stdout, stderr=stderr)

    return run


def _open_issue_ids(run: Runner, chainlink_bin: str) -> tuple[set[int], str | None]:
    result = run([chainlink_bin, "issue", "list", "--status", "open", "--json"])
    if result.returncode != 0:
        return set(), (
            (result.stderr or result.stdout).strip() or "chainlink issue list failed"
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return set(), "chainlink issue list returned invalid JSON"
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("issues", [])
    else:
        return set(), "chainlink issue list returned invalid JSON payload"
    if not isinstance(items, list):
        return set(), "chainlink issue list returned invalid issues"
    issue_ids: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            issue_ids.add(int(item.get("id", item.get("number"))))
        except (TypeError, ValueError):
            continue
    return issue_ids, None


def _worklink_issue_labels(
    run: Runner, chainlink_bin: str, issue_ids: set[int]
) -> tuple[dict[int, set[str]], dict[int, str]]:
    labels_by_issue: dict[int, set[str]] = {}
    errors: dict[int, str] = {}
    # `issue list --json` omits labels and blocked_by. Only `issue show` is a
    # valid source for reconciliation data.
    for issue_id in sorted(issue_ids):
        result = run([chainlink_bin, "issue", "show", str(issue_id), "--json"])
        if result.returncode != 0:
            errors[issue_id] = (
                (result.stderr or result.stdout).strip()
                or f"chainlink issue show {issue_id} failed"
            )
            continue
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            errors[issue_id] = f"chainlink issue show {issue_id} returned invalid JSON"
            continue
        if not isinstance(payload, dict) or "labels" not in payload:
            errors[issue_id] = f"chainlink issue show {issue_id} omitted labels"
            continue
        raw_labels = payload["labels"]
        if not isinstance(raw_labels, (list, dict)):
            errors[issue_id] = f"chainlink issue show {issue_id} returned invalid labels"
            continue
        labels: set[str] = set()
        malformed = False
        for label in raw_labels:
            if isinstance(label, str):
                labels.add(label)
            elif isinstance(label, dict) and label.get("name") is not None:
                labels.add(str(label["name"]))
            else:
                malformed = True
                break
        if malformed:
            errors[issue_id] = f"chainlink issue show {issue_id} returned invalid labels"
            continue
        labels_by_issue[issue_id] = labels
    return labels_by_issue, errors


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
    run = runner or _runner(home, chainlink_bin)
    explicit = set(issue_ids)
    if explicit:
        candidates = explicit
    else:
        open_issue_ids, discovery_error = _open_issue_ids(run, chainlink_bin)
        if discovery_error is not None:
            raise RuntimeError(discovery_error)
        candidates = set(states) | open_issue_ids
    labels_by_issue, label_errors = _worklink_issue_labels(
        run, chainlink_bin, candidates
    )
    selected = (
        explicit
        if explicit
        else (
            set(states)
            | set(label_errors)
            | {
                issue_id
                for issue_id, labels in labels_by_issue.items()
                if any(label.startswith("worklink:") for label in labels)
            }
        )
    )
    rows: list[WorklinkStatus] = []
    for issue_id in sorted(selected):
        state = states.get(issue_id)
        labels = labels_by_issue.get(issue_id)
        in_progress = None if labels is None else "worklink:in-progress" in labels
        disagreement = None
        if state is not None:
            classification = "running" if process_is_alive(state) else "orphaned"
            if in_progress is None:
                disagreement = f"labels unavailable: {label_errors[issue_id]}"
            elif not in_progress:
                disagreement = "run state present but worklink:in-progress label absent"
            elapsed = elapsed_seconds(state, now=now)
        elif in_progress:
            classification = "unrecorded"
            disagreement = "worklink:in-progress label present but run state absent"
            elapsed = None
        elif in_progress is None:
            classification = "unknown"
            disagreement = f"labels unavailable: {label_errors[issue_id]}"
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


def archive_worklink_factory_records(home: Path, issue_id: int) -> list[Path]:
    """Archive canonical and legacy records for an epic under the claim mutex."""
    archived: list[Path] = []
    with _claim_mutex(home):
        for run_id in (f"chainlink-{issue_id}", str(issue_id)):
            record = load_factory_record(home, run_id)
            if record is not None:
                archived.append(archive_factory_record(home, record))
    return archived


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
        if state is None:
            factory = load_factory_record(home, str(issue_id))
            if factory is None or not factory_process_is_alive(factory) or factory.handle is None:
                return WorklinkStopResult(issue_id, False, reason="no live run")
            try:
                asyncio.run(LocalSubprocessComputeBackend().cancel(factory.handle))
            except (KeyError, RuntimeError, OSError) as exc:
                return WorklinkStopResult(issue_id, False, reason=str(exc))
            save_factory_record(
                home,
                replace(factory, controller_phase="stopped", controller_error=None),
            )
            release = run([chainlink_bin, "locks", "release", str(issue_id)])
            unlabel = run(
                [chainlink_bin, "issue", "unlabel", str(issue_id), "worklink:in-progress"]
            )
            return WorklinkStopResult(
                issue_id,
                True,
                state_cleared=False,
                claim_released=release.returncode == 0,
                label_cleared=unlabel.returncode == 0,
            )
        if not process_is_alive(state):
            clear_run_state(home, issue_id)
            return WorklinkStopResult(
                issue_id,
                False,
                state_cleared=load_run_state(home, issue_id) is None,
                reason="no live run",
            )
        if state.phase != "spawned":
            return WorklinkStopResult(issue_id, False, reason="no live run")
        if not process_identity_verified(state):
            return WorklinkStopResult(
                issue_id, False, reason="live PID could not be verified; refusing to signal it"
            )

        handle = LaunchHandle(
            substrate=state.handle_substrate,
            identifier=state.handle_identifier,
            process_start_ticks=state.process_start_ticks,
            shim_pid=state.shim_pid,
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
