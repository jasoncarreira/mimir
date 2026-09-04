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
    load_factory_records_for_issue,
    save_factory_record,
)
from .run_state import (
    OrphanBlockRecord,
    WorklinkRunState,
    clear_orphan_block_record,
    clear_run_state,
    elapsed_seconds,
    list_run_states,
    load_run_state,
    process_identity_verified,
    process_is_alive,
    runs_dir,
    save_orphan_block_record,
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


def archive_worklink_factory_records(
    home: Path,
    issue_id: int,
    *,
    event_logger: EventLogger | None = None,
) -> list[Path]:
    """Archive canonical and legacy records for an epic under the claim mutex."""
    archived: list[Path] = []
    with _claim_mutex(home):
        for record in load_factory_records_for_issue(home, issue_id):
            destination = archive_factory_record(
                home,
                record,
                event_logger=event_logger,
                source_kind="operator_command",
                reason="operator requested archival",
            )
            archived.append(destination)
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
            factory = next(
                (
                    record
                    for record in load_factory_records_for_issue(home, issue_id)
                    if factory_process_is_alive(record) and record.handle is not None
                ),
                None,
            )
            if factory is None or factory.handle is None:
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
    runner: Runner | None = None,
    git_runner: Runner | None = None,
    chainlink_bin: str = "chainlink",
    now: datetime | None = None,
) -> list[WorklinkRunState]:
    """Reconcile dead local runs without discarding their only recovery pointer.

    A dead run with no unpublished commits is returned to ``worklink:ready``.
    A checkout with commits absent from its remote branch is instead parked at
    ``worklink:blocked`` so autonomous dispatch cannot redo or overwrite finished
    work and is excluded from TTL pruning. An existing checkout whose publication
    status cannot be determined is blocked only until that checkout is pruned; a
    missing checkout returns directly to ready because no recovery pointer remains.
    The Chainlink lock is released before labels or local state are changed; if
    release fails, the state remains as an actionable pointer and a failure event
    records the partial recovery.
    """
    alive: list[WorklinkRunState] = []
    run = runner or _runner(home, chainlink_bin)
    run_git = git_runner or _git_runner(home)
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
        with _claim_mutex(home):
            publication_outcome, publication_reason = _checkout_has_unpublished_commits(
                state, run_git
            )
            release = run([chainlink_bin, "locks", "release", str(state.issue_id)])
            if release.returncode != 0:
                _emit_orphan_reconcile_failed(
                    event_logger, state, "lock_release_failed", release
                )
                continue

            checkout_exists = bool(state.checkout and Path(state.checkout).is_dir())
            would_rearm = publication_outcome != "determined-unpublished" and not (
                publication_outcome == "undetermined" and checkout_exists
            )
            labels_unknown = False
            is_epic = False
            if would_rearm:
                labels_by_issue, label_errors = _worklink_issue_labels(
                    run, chainlink_bin, {state.issue_id}
                )
                labels_unknown = state.issue_id in label_errors
                is_epic = "worklink:epic" in labels_by_issue.get(state.issue_id, set())
            target = (
                "worklink:blocked"
                if is_epic
                or labels_unknown
                or publication_outcome == "determined-unpublished"
                or (publication_outcome == "undetermined" and checkout_exists)
                else "worklink:ready"
            )
            comment_text = _orphan_reconcile_comment(
                state,
                publication_outcome=publication_outcome,
                publication_reason=publication_reason,
                target=target,
                is_epic=is_epic,
                labels_unknown=labels_unknown,
            )
            comment = run(
                [chainlink_bin, "issue", "comment", str(state.issue_id), comment_text]
            )
            if comment.returncode != 0:
                _emit_orphan_reconcile_failed(
                    event_logger, state, "orphan_comment_failed", comment
                )
                continue
            if target == "worklink:blocked":
                save_orphan_block_record(
                    home,
                    OrphanBlockRecord(
                        issue_id=state.issue_id,
                        attempt=state.attempt,
                        checkout=state.checkout,
                        publication_outcome=publication_outcome,
                        comment=comment_text,
                    ),
                )
            label = run(
                [chainlink_bin, "issue", "label", str(state.issue_id), target]
            )
            if label.returncode != 0:
                clear_orphan_block_record(home, state.issue_id)
                _emit_orphan_reconcile_failed(
                    event_logger, state, f"{target}_label_failed", label
                )
                continue
            unlabel = run(
                [
                    chainlink_bin,
                    "issue",
                    "unlabel",
                    str(state.issue_id),
                    "worklink:in-progress",
                ]
            )
            if unlabel.returncode != 0:
                _emit_orphan_reconcile_failed(
                    event_logger, state, "in_progress_unlabel_failed", unlabel
                )
                continue

            clear_run_state(home, state.issue_id)
            _emit_reconcile_event(
                event_logger,
                "worklink_run_orphaned",
                issue_id=state.issue_id,
                attempt=state.attempt,
                branch=state.branch,
                checkout=state.checkout,
                elapsed_s=round(elapsed_seconds(state, now=now), 3),
                publication_outcome=publication_outcome,
                unpublished_commits=(
                    publication_outcome == "determined-unpublished"
                    if publication_outcome != "undetermined"
                    else None
                ),
                publication_reason=publication_reason,
                resulting_label=target,
                lock_released=True,
                reaped=load_run_state(home, state.issue_id) is None,
            )
    return alive


def _git_runner(home: Path) -> Runner:
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
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(args, 124, stdout="", stderr=str(exc))

    return run


def _checkout_has_unpublished_commits(
    state: WorklinkRunState, run: Runner
) -> tuple[str, str]:
    """Determine whether commits are absent from this attempt's remote branch."""
    checkout = Path(state.checkout)
    if not state.checkout or not checkout.is_dir():
        return "undetermined", "checkout unavailable"
    head = run(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    if head.returncode != 0 or not head.stdout.strip():
        return "undetermined", "checkout HEAD unavailable"
    head_sha = head.stdout.strip().lower()
    ahead = run(
        ["git", "-C", str(checkout), "rev-list", "--count", f"{state.local_base}..HEAD"]
    )
    if ahead.returncode != 0:
        return "undetermined", "checkout base comparison failed"
    try:
        ahead_count = int(ahead.stdout.strip())
    except ValueError:
        return "undetermined", "checkout base comparison invalid"
    if ahead_count == 0:
        return "determined-clean", "checkout has no commits beyond its base"
    remote = run(
        [
            "git",
            "-C",
            str(checkout),
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{state.branch}",
        ]
    )
    if remote.returncode != 0:
        return "undetermined", "remote branch lookup failed"
    remote_sha = (remote.stdout.strip().split() or [""])[0].lower()
    if remote_sha == head_sha:
        return "determined-clean", "checkout HEAD is published on its remote branch"
    return "determined-unpublished", "checkout HEAD is absent from its remote branch"


def _orphan_reconcile_comment(
    state: WorklinkRunState,
    *,
    publication_outcome: str,
    publication_reason: str,
    target: str,
    is_epic: bool = False,
    labels_unknown: bool = False,
) -> str:
    identity = f"issue={state.issue_id} attempt={state.attempt} checkout={state.checkout}"
    if is_epic:
        return (
            f"WORKLINK_BLOCKED orphaned epic run {identity}: {publication_reason}. "
            "worklink:epic issues are armed by an operator only, so orphan "
            "reconciliation did not apply worklink:ready."
        )
    if labels_unknown:
        return (
            f"WORKLINK_BLOCKED orphaned run {identity}: issue labels could not be "
            "verified, so reconciliation failed closed and did not apply "
            "worklink:ready."
        )
    if publication_outcome == "determined-unpublished":
        return (
            f"WORKLINK_BLOCKED orphaned run {identity}: unpublished commits were verified "
            f"({publication_reason}). This checkout is excluded from automatic pruning; "
            "recover and publish its work before manually re-queuing the issue."
        )
    if target == "worklink:blocked":
        return (
            f"WORKLINK_BLOCKED orphaned run {identity}: publication status is undetermined "
            f"({publication_reason}). Dispatch remains blocked until the attempt-checkout "
            "pruner removes this checkout, then it restores worklink:ready."
        )
    if publication_outcome == "determined-clean":
        return (
            f"WORKLINK_ORPHAN_RECOVERED orphaned run {identity}: the checkout was "
            f"verified clean ({publication_reason}); the issue was returned to "
            "worklink:ready."
        )
    return (
        f"WORKLINK_ORPHAN_RECOVERED orphaned run {identity}: publication status is "
        f"undetermined ({publication_reason}), but no checkout remains to preserve; "
        "the issue was returned to worklink:ready."
    )


def _emit_orphan_reconcile_failed(
    event_logger: EventLogger | None,
    state: WorklinkRunState,
    reason: str,
    result: subprocess.CompletedProcess[str],
) -> None:
    _emit_reconcile_event(
        event_logger,
        "worklink_run_orphan_reconcile_failed",
        issue_id=state.issue_id,
        attempt=state.attempt,
        branch=state.branch,
        checkout=state.checkout,
        reason=reason,
        error=(result.stderr or result.stdout).strip()[:500],
        state_retained=True,
    )


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
