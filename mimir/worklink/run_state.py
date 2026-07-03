"""Durable Worklink run state for controller resume-after-restart (#561).

The Worklink controller (``mimir worklink run``) is a *detached* subprocess the
ready-queue poller spawns. It survives the poller's own exit, but a container
restart (``docker restart`` / SIGTERM) kills it mid-run. For remote compute
substrates (``docker_sibling``/``ecs``) the *worker* runs in a separate
container/task that survives that restart, so a fresh controller can reattach to
the live job — wait for it, harvest evidence, open the PR — instead of orphaning
the work and letting the TTL reaper (#444) re-run it from scratch.

This module persists the minimal handle a fresh controller needs to find that
worker again: the issue/attempt, the compute substrate name + opaque launch
handle, and the git coordinates (branch/base/repo) the post-launch evidence and
PR steps re-derive from the *pushed* branch.

Only substrates whose ``ComputeCaps.persistent_after_disconnect`` is true are
ever persisted — ``local_subprocess`` work dies with the controller and has
nothing to reattach to (the reaper remains its safety net).

State lives at ``<home>/state/worklink/runs/<issue_id>.json`` and is deleted on
terminal completion, so a file present at startup means "a worker we may still
be able to reattach to".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

RUN_STATE_VERSION = 1
EXHAUSTION_CHECKPOINT_VERSION = 1


@dataclass(frozen=True)
class WorklinkRunState:
    """Everything a fresh controller needs to reattach to an in-flight run."""

    issue_id: int
    attempt: int
    backend: str
    compute_name: str
    handle_substrate: str
    handle_identifier: str
    branch: str
    base_ref: str
    local_base: str
    repo: str
    repo_url: str
    test_command: str | None
    started_at: str
    version: int = RUN_STATE_VERSION

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> "WorklinkRunState":
        if not isinstance(data, dict):
            raise TypeError("worklink run state must be a JSON object")
        return cls(
            issue_id=int(data["issue_id"]),
            attempt=int(data["attempt"]),
            backend=str(data["backend"]),
            compute_name=str(data["compute_name"]),
            handle_substrate=str(data["handle_substrate"]),
            handle_identifier=str(data["handle_identifier"]),
            branch=str(data["branch"]),
            base_ref=str(data["base_ref"]),
            local_base=str(data.get("local_base") or data["base_ref"]),
            repo=str(data.get("repo") or ""),
            repo_url=str(data.get("repo_url") or ""),
            test_command=(str(data["test_command"]) if data.get("test_command") else None),
            started_at=str(data["started_at"]),
            version=int(data.get("version") or RUN_STATE_VERSION),
        )


def runs_dir(home: Path) -> Path:
    return home / "state" / "worklink" / "runs"


def exhaustion_checkpoints_dir(home: Path) -> Path:
    """Directory for turn-exhaustion continuation checkpoints.

    These artifacts are distinct from ``runs/`` reattach handles: they do not
    describe a live compute worker. They describe enough execution context for a
    later Worklink/Chainlink continuation to resume safely after the model turn
    ended before the worker could finish normal label/comment/PR transitions.
    """
    return home / "state" / "worklink" / "exhaustion-checkpoints"


def run_state_path(home: Path, issue_id: int) -> Path:
    return runs_dir(home) / f"{issue_id}.json"


def exhaustion_checkpoint_path(home: Path, dedupe_key: str) -> Path:
    return exhaustion_checkpoints_dir(home) / f"{dedupe_key}.json"


@dataclass(frozen=True)
class WorklinkExhaustionCheckpoint:
    """Structured continuation artifact for an exhausted Worklink turn.

    ``mode`` separates the two supported schemas:

    * ``"known_issue"``: ``issue_id`` is known and optional ``pr_url`` links the
      continuation to a Chainlink issue/PR.
    * ``"unknown_issue"``: no issue could be inferred; the artifact remains a
      generic high-priority continuation record with repo/worktree/branch and
      next commands so a human or future controller can resume without guessing.

    ``dedupe_key`` is the SHA-256 hex digest returned by
    :func:`exhaustion_checkpoint_dedupe_key`. It is computed from the exhausted
    turn/work-item identity and repo coordinates, not from mutable notes such as
    completed edits. Repeated finalizer attempts for the same turn therefore
    target the same durable file and suppress duplicate continuation records.
    """

    mode: str
    dedupe_key: str
    turn_id: str
    work_item_id: str | None
    issue_id: int | None
    pr_url: str | None
    repo: str
    worktree: str
    branch: str
    completed_edits: tuple[str, ...]
    unrun_validation: tuple[str, ...]
    next_commands: tuple[str, ...]
    label_status_adjustments: tuple[str, ...]
    created_at: str
    priority: str = "high"
    version: int = EXHAUSTION_CHECKPOINT_VERSION

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> "WorklinkExhaustionCheckpoint":
        if not isinstance(data, dict):
            raise TypeError("worklink exhaustion checkpoint must be a JSON object")
        return cls(
            mode=str(data["mode"]),
            dedupe_key=str(data["dedupe_key"]),
            turn_id=str(data["turn_id"]),
            work_item_id=(str(data["work_item_id"]) if data.get("work_item_id") else None),
            issue_id=(int(data["issue_id"]) if data.get("issue_id") is not None else None),
            pr_url=(str(data["pr_url"]) if data.get("pr_url") else None),
            repo=str(data.get("repo") or ""),
            worktree=str(data.get("worktree") or ""),
            branch=str(data.get("branch") or ""),
            completed_edits=_string_tuple(data.get("completed_edits")),
            unrun_validation=_string_tuple(data.get("unrun_validation")),
            next_commands=_string_tuple(data.get("next_commands")),
            label_status_adjustments=_string_tuple(data.get("label_status_adjustments")),
            created_at=str(data["created_at"]),
            priority=str(data.get("priority") or "high"),
            version=int(data.get("version") or EXHAUSTION_CHECKPOINT_VERSION),
        )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise TypeError("checkpoint list field must be an array")
    return tuple(str(item) for item in value)


def exhaustion_checkpoint_dedupe_key(
    *,
    mode: str,
    turn_id: str,
    work_item_id: str | None,
    issue_id: int | None,
    repo: str,
    worktree: str,
    branch: str,
) -> str:
    """Stable identity for one exhausted turn/work item.

    The key intentionally excludes mutable continuation details so a retrying
    finalizer can enrich the same checkpoint without creating duplicates.
    Known-issue checkpoints include ``issue_id``; unknown-issue checkpoints use
    ``work_item_id``/``turn_id`` plus repo coordinates and keep ``issue_id`` null.
    """
    identity = {
        "branch": branch,
        "issue_id": issue_id,
        "mode": mode,
        "repo": repo,
        "turn_id": turn_id,
        "work_item_id": work_item_id,
        "worktree": worktree,
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def known_issue_exhaustion_checkpoint(
    *,
    issue_id: int,
    turn_id: str,
    repo: str,
    worktree: str,
    branch: str,
    created_at: str,
    work_item_id: str | None = None,
    pr_url: str | None = None,
    completed_edits: tuple[str, ...] = (),
    unrun_validation: tuple[str, ...] = (),
    next_commands: tuple[str, ...] = (),
    label_status_adjustments: tuple[str, ...] = (),
) -> WorklinkExhaustionCheckpoint:
    mode = "known_issue"
    dedupe_key = exhaustion_checkpoint_dedupe_key(
        mode=mode,
        turn_id=turn_id,
        work_item_id=work_item_id,
        issue_id=issue_id,
        repo=repo,
        worktree=worktree,
        branch=branch,
    )
    return WorklinkExhaustionCheckpoint(
        mode=mode,
        dedupe_key=dedupe_key,
        turn_id=turn_id,
        work_item_id=work_item_id,
        issue_id=issue_id,
        pr_url=pr_url,
        repo=repo,
        worktree=worktree,
        branch=branch,
        completed_edits=tuple(completed_edits),
        unrun_validation=tuple(unrun_validation),
        next_commands=tuple(next_commands),
        label_status_adjustments=tuple(label_status_adjustments),
        created_at=created_at,
    )


def unknown_issue_exhaustion_checkpoint(
    *,
    turn_id: str,
    repo: str,
    worktree: str,
    branch: str,
    created_at: str,
    work_item_id: str | None = None,
    completed_edits: tuple[str, ...] = (),
    unrun_validation: tuple[str, ...] = (),
    next_commands: tuple[str, ...] = (),
    label_status_adjustments: tuple[str, ...] = (),
) -> WorklinkExhaustionCheckpoint:
    mode = "unknown_issue"
    dedupe_key = exhaustion_checkpoint_dedupe_key(
        mode=mode,
        turn_id=turn_id,
        work_item_id=work_item_id,
        issue_id=None,
        repo=repo,
        worktree=worktree,
        branch=branch,
    )
    return WorklinkExhaustionCheckpoint(
        mode=mode,
        dedupe_key=dedupe_key,
        turn_id=turn_id,
        work_item_id=work_item_id,
        issue_id=None,
        pr_url=None,
        repo=repo,
        worktree=worktree,
        branch=branch,
        completed_edits=tuple(completed_edits),
        unrun_validation=tuple(unrun_validation),
        next_commands=tuple(next_commands),
        label_status_adjustments=tuple(label_status_adjustments),
        created_at=created_at,
        priority="high",
    )


def save_run_state(home: Path, state: WorklinkRunState) -> Path:
    """Persist ``state`` atomically (tmp + replace) so a crash mid-write can't
    leave a half-written file a startup reconcile would choke on."""
    path = run_state_path(home, state.issue_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state.to_json(), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def save_exhaustion_checkpoint(
    home: Path, checkpoint: WorklinkExhaustionCheckpoint
) -> tuple[Path, bool]:
    """Persist a continuation checkpoint and suppress duplicate records.

    Returns ``(path, created)``. ``created`` is false when a checkpoint with the
    same dedupe identity already existed. The file is still atomically replaced
    so a retry can fill in richer context without adding a second artifact.
    """
    path = exhaustion_checkpoint_path(home, checkpoint.dedupe_key)
    created = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(checkpoint.to_json(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path, created


def load_run_state(home: Path, issue_id: int) -> WorklinkRunState | None:
    """Load one run state; ``None`` if absent or unparseable."""
    path = run_state_path(home, issue_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return WorklinkRunState.from_json(data)
    except (KeyError, TypeError, ValueError):
        return None


def load_exhaustion_checkpoint(
    home: Path, dedupe_key: str
) -> WorklinkExhaustionCheckpoint | None:
    """Load one checkpoint; ``None`` if absent or unparseable."""
    path = exhaustion_checkpoint_path(home, dedupe_key)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return WorklinkExhaustionCheckpoint.from_json(data)
    except (KeyError, TypeError, ValueError):
        return None


def clear_run_state(home: Path, issue_id: int) -> None:
    """Best-effort delete of an issue's run state (no-op if already gone)."""
    try:
        run_state_path(home, issue_id).unlink()
    except OSError:
        return


def list_exhaustion_checkpoints(home: Path) -> list[WorklinkExhaustionCheckpoint]:
    """All readable exhaustion checkpoints, sorted by dedupe key.

    Like run states, corrupt checkpoint files are skipped so one bad artifact
    cannot block operators or future continuation consumers from reading the
    remaining durable context.
    """
    directory = exhaustion_checkpoints_dir(home)
    if not directory.exists():
        return []
    checkpoints: list[WorklinkExhaustionCheckpoint] = []
    for child in sorted(directory.glob("*.json")):
        try:
            data = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            checkpoints.append(WorklinkExhaustionCheckpoint.from_json(data))
        except (KeyError, TypeError, ValueError):
            continue
    return checkpoints


def list_run_states(home: Path) -> list[WorklinkRunState]:
    """All readable run states under ``<home>/state/worklink/runs`` (id-ordered).

    Unparseable files are skipped, not raised on: a single corrupt file must not
    block the startup reconcile from recovering the others.
    """
    directory = runs_dir(home)
    if not directory.exists():
        return []
    states: list[WorklinkRunState] = []
    for child in sorted(directory.glob("*.json")):
        try:
            data = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            states.append(WorklinkRunState.from_json(data))
        except (KeyError, TypeError, ValueError):
            continue
    return states


def reattach_dispatch_argv(run_bin: list[str], home: Path, repo: str, issue_id: int) -> list[str]:
    """Argv to resume one in-flight run as a detached subprocess on startup.

    Mirrors the ready-queue poller's dispatch shape (``--autonomous`` so the
    compute-backend autonomy gate still applies), with ``--reattach`` selecting
    the resume path instead of a fresh claim+launch."""
    return [
        *run_bin,
        "worklink",
        "run",
        str(issue_id),
        "--reattach",
        "--autonomous",
        "--home",
        str(home),
        "--repo",
        repo,
    ]
