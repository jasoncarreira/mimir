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


@dataclass(frozen=True)
class WorklinkExhaustionCheckpoint:
    """Durable continuation artifact for a Worklink/Chainlink exhausted turn.

    Storage format:
      ``<home>/state/worklink/exhaustion-checkpoints/known-issue/<key>.json``
      when ``issue_id`` is known, otherwise
      ``<home>/state/worklink/exhaustion-checkpoints/unknown-issue/<key>.json``.

    Known-issue checkpoints carry concrete Chainlink/PR coordinates. Unknown
    checkpoints deliberately remain valid artifacts with ``priority="high"`` so
    a finalizer can preserve enough context for a human or later controller to
    resume safely instead of silently dropping the turn.

    ``dedupe_key`` is the duplicate-suppression identity for one exhausted
    turn/work item. Use ``for_known_issue``/``for_unknown_issue`` so it is
    computed consistently from the case, work item, issue coordinate, and
    exhausted turn id. PR context is stored but excluded from identity.
    """

    case: str
    dedupe_key: str
    work_item: str
    exhausted_turn_id: str
    worktree: str
    branch: str
    completed_edits: tuple[str, ...]
    unrun_validation: tuple[str, ...]
    next_commands: tuple[str, ...]
    label_status_adjustments: tuple[str, ...]
    created_at: str
    issue_id: int | None = None
    pr_url: str | None = None
    priority: str = "normal"
    version: int = EXHAUSTION_CHECKPOINT_VERSION

    @classmethod
    def for_known_issue(
        cls,
        *,
        issue_id: int,
        work_item: str,
        exhausted_turn_id: str,
        worktree: str,
        branch: str,
        completed_edits: list[str] | tuple[str, ...] = (),
        unrun_validation: list[str] | tuple[str, ...] = (),
        next_commands: list[str] | tuple[str, ...] = (),
        label_status_adjustments: list[str] | tuple[str, ...] = (),
        created_at: str,
        pr_url: str | None = None,
        priority: str = "normal",
    ) -> "WorklinkExhaustionCheckpoint":
        dedupe_key = exhaustion_checkpoint_dedupe_key(
            case="known-issue",
            work_item=work_item,
            exhausted_turn_id=exhausted_turn_id,
            issue_id=issue_id,
            pr_url=pr_url,
        )
        return cls(
            case="known-issue",
            dedupe_key=dedupe_key,
            issue_id=issue_id,
            pr_url=pr_url,
            priority=priority,
            work_item=work_item,
            exhausted_turn_id=exhausted_turn_id,
            worktree=worktree,
            branch=branch,
            completed_edits=tuple(completed_edits),
            unrun_validation=tuple(unrun_validation),
            next_commands=tuple(next_commands),
            label_status_adjustments=tuple(label_status_adjustments),
            created_at=created_at,
        )

    @classmethod
    def for_unknown_issue(
        cls,
        *,
        work_item: str,
        exhausted_turn_id: str,
        worktree: str,
        branch: str,
        completed_edits: list[str] | tuple[str, ...] = (),
        unrun_validation: list[str] | tuple[str, ...] = (),
        next_commands: list[str] | tuple[str, ...] = (),
        label_status_adjustments: list[str] | tuple[str, ...] = (),
        created_at: str,
    ) -> "WorklinkExhaustionCheckpoint":
        dedupe_key = exhaustion_checkpoint_dedupe_key(
            case="unknown-issue",
            work_item=work_item,
            exhausted_turn_id=exhausted_turn_id,
        )
        return cls(
            case="unknown-issue",
            dedupe_key=dedupe_key,
            issue_id=None,
            pr_url=None,
            priority="high",
            work_item=work_item,
            exhausted_turn_id=exhausted_turn_id,
            worktree=worktree,
            branch=branch,
            completed_edits=tuple(completed_edits),
            unrun_validation=tuple(unrun_validation),
            next_commands=tuple(next_commands),
            label_status_adjustments=tuple(label_status_adjustments),
            created_at=created_at,
        )

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["completed_edits"] = list(self.completed_edits)
        data["unrun_validation"] = list(self.unrun_validation)
        data["next_commands"] = list(self.next_commands)
        data["label_status_adjustments"] = list(self.label_status_adjustments)
        return data

    @classmethod
    def from_json(cls, data: Any) -> "WorklinkExhaustionCheckpoint":
        if not isinstance(data, dict):
            raise TypeError("worklink exhaustion checkpoint must be a JSON object")
        case = str(data["case"])
        if case not in {"known-issue", "unknown-issue"}:
            raise ValueError(f"unsupported exhaustion checkpoint case: {case}")
        issue_id = data.get("issue_id")
        priority = str(data.get("priority") or "normal")
        if case == "known-issue":
            issue_id = int(issue_id)
        else:
            issue_id = None
            priority = "high"
        return cls(
            case=case,
            dedupe_key=str(data["dedupe_key"]),
            issue_id=issue_id,
            pr_url=(str(data["pr_url"]) if data.get("pr_url") else None),
            priority=priority,
            work_item=str(data["work_item"]),
            exhausted_turn_id=str(data["exhausted_turn_id"]),
            worktree=str(data["worktree"]),
            branch=str(data["branch"]),
            completed_edits=_str_tuple(data.get("completed_edits")),
            unrun_validation=_str_tuple(data.get("unrun_validation")),
            next_commands=_str_tuple(data.get("next_commands")),
            label_status_adjustments=_str_tuple(data.get("label_status_adjustments")),
            created_at=str(data["created_at"]),
            version=int(data.get("version") or EXHAUSTION_CHECKPOINT_VERSION),
        )


@dataclass(frozen=True)
class ExhaustionCheckpointSave:
    """Result of an idempotent checkpoint save."""

    path: Path
    checkpoint: WorklinkExhaustionCheckpoint
    created: bool


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise TypeError("checkpoint list field must be a JSON array")
    return tuple(str(item) for item in value)


def exhaustion_checkpoint_dedupe_key(
    *,
    case: str,
    work_item: str,
    exhausted_turn_id: str,
    issue_id: int | None = None,
    pr_url: str | None = None,
) -> str:
    """Stable identity for duplicate-suppressing one exhausted turn/work item.

    The identity is ``case + work_item + exhausted_turn_id + issue_id``. PR URLs
    are stored as context, but intentionally excluded: a repeated finalizer may
    learn PR context after the first write, and that must not create a second
    continuation artifact for the same exhausted turn.
    """
    if case not in {"known-issue", "unknown-issue"}:
        raise ValueError(f"unsupported exhaustion checkpoint case: {case}")
    issue_part = str(issue_id) if issue_id is not None else "unknown"
    raw = "\0".join([case, work_item, exhausted_turn_id, issue_part])
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"worklink-exhaustion:{case}:{digest}"


def exhaustion_checkpoints_dir(home: Path) -> Path:
    return home / "state" / "worklink" / "exhaustion-checkpoints"


def exhaustion_checkpoint_path(
    home: Path, checkpoint: WorklinkExhaustionCheckpoint
) -> Path:
    filename = checkpoint.dedupe_key.replace(":", "-") + ".json"
    return exhaustion_checkpoints_dir(home) / checkpoint.case / filename


def save_exhaustion_checkpoint(
    home: Path, checkpoint: WorklinkExhaustionCheckpoint
) -> ExhaustionCheckpointSave:
    """Persist an exhaustion checkpoint once per ``dedupe_key``.

    If a valid checkpoint already exists at the deterministic path, it is
    returned with ``created=False`` and left untouched. That first-writer-wins
    behavior makes repeated exhausted-turn finalizer attempts idempotent and
    prevents duplicate continuation records/comments/issues from being derived
    from repeated writes.
    """
    path = exhaustion_checkpoint_path(home, checkpoint)
    existing = load_exhaustion_checkpoint_path(path)
    if existing is not None:
        return ExhaustionCheckpointSave(path=path, checkpoint=existing, created=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(checkpoint.to_json(), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return ExhaustionCheckpointSave(path=path, checkpoint=checkpoint, created=True)


def load_exhaustion_checkpoint_path(path: Path) -> WorklinkExhaustionCheckpoint | None:
    """Load one checkpoint by path; ``None`` if absent or unparseable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return WorklinkExhaustionCheckpoint.from_json(data)
    except (KeyError, TypeError, ValueError):
        return None


def list_exhaustion_checkpoints(home: Path) -> list[WorklinkExhaustionCheckpoint]:
    """All readable exhaustion checkpoints, ordered by case then dedupe key."""
    directory = exhaustion_checkpoints_dir(home)
    if not directory.exists():
        return []
    checkpoints: list[WorklinkExhaustionCheckpoint] = []
    for child in sorted(directory.glob("*/*.json")):
        checkpoint = load_exhaustion_checkpoint_path(child)
        if checkpoint is not None:
            checkpoints.append(checkpoint)
    return checkpoints


def runs_dir(home: Path) -> Path:
    return home / "state" / "worklink" / "runs"


def run_state_path(home: Path, issue_id: int) -> Path:
    return runs_dir(home) / f"{issue_id}.json"


def save_run_state(home: Path, state: WorklinkRunState) -> Path:
    """Persist ``state`` atomically (tmp + replace) so a crash mid-write can't
    leave a half-written file a startup reconcile would choke on."""
    path = run_state_path(home, state.issue_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state.to_json(), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


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


def clear_run_state(home: Path, issue_id: int) -> None:
    """Best-effort delete of an issue's run state (no-op if already gone)."""
    try:
        run_state_path(home, issue_id).unlink()
    except OSError:
        return


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
