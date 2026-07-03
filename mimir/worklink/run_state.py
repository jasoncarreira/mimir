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
CONTINUATION_CHECKPOINT_VERSION = 1


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
class WorklinkContinuationCheckpoint:
    """Durable handoff for a Worklink/Chainlink turn that exhausted mid-work.

    This is intentionally separate from :class:`WorklinkRunState`: run state is
    for reattaching to a still-running worker, while continuation checkpoints are
    reviewable "resume this work safely" artifacts written after a turn exhausts.

    Known-issue checkpoints set ``kind="known_issue"`` and carry the related
    Chainlink issue id. If no issue can be inferred, callers still write a
    ``kind="generic"`` checkpoint; those are always high priority and identify
    the interrupted work by ``work_item_key`` plus ``exhausted_turn_id``.

    Deduplication is path-based. ``dedupe_key`` hashes the stable work identity
    for one exhausted turn/work item, not mutable resume notes or optional PR
    context, so retries of the same finalizer call suppress duplicate records
    even if they re-render prose.
    """

    kind: str
    work_item_key: str
    exhausted_turn_id: str
    worktree: str
    branch: str
    completed_edits: list[str]
    unrun_validation: list[str]
    next_commands: list[str]
    required_label_status_adjustments: list[str]
    created_at: str
    related_chainlink_issue_id: int | None = None
    related_pr: str | None = None
    repo: str = ""
    priority: str = "normal"
    dedupe_key: str = ""
    version: int = CONTINUATION_CHECKPOINT_VERSION

    def __post_init__(self) -> None:
        if self.kind not in {"known_issue", "generic"}:
            raise ValueError("continuation checkpoint kind must be known_issue or generic")
        if self.kind == "known_issue" and self.related_chainlink_issue_id is None:
            raise ValueError("known_issue checkpoints require related_chainlink_issue_id")
        if self.kind == "generic" and self.related_chainlink_issue_id is not None:
            raise ValueError("generic checkpoints cannot carry a Chainlink issue id")
        if self.kind == "generic" and self.priority != "high":
            raise ValueError("generic continuation checkpoints must be high priority")
        if self.dedupe_key and self.dedupe_key != continuation_checkpoint_dedupe_key(self):
            raise ValueError("continuation checkpoint dedupe_key does not match identity")

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["dedupe_key"] = self.dedupe_key or continuation_checkpoint_dedupe_key(self)
        return data

    @classmethod
    def known_issue(
        cls,
        *,
        issue_id: int,
        work_item_key: str,
        exhausted_turn_id: str,
        worktree: str,
        branch: str,
        completed_edits: list[str],
        unrun_validation: list[str],
        next_commands: list[str],
        required_label_status_adjustments: list[str],
        created_at: str,
        related_pr: str | None = None,
        repo: str = "",
        priority: str = "normal",
    ) -> "WorklinkContinuationCheckpoint":
        checkpoint = cls(
            kind="known_issue",
            related_chainlink_issue_id=issue_id,
            related_pr=related_pr,
            work_item_key=work_item_key,
            exhausted_turn_id=exhausted_turn_id,
            worktree=worktree,
            branch=branch,
            repo=repo,
            completed_edits=completed_edits,
            unrun_validation=unrun_validation,
            next_commands=next_commands,
            required_label_status_adjustments=required_label_status_adjustments,
            created_at=created_at,
            priority=priority,
        )
        return cls.from_json(checkpoint.to_json())

    @classmethod
    def generic(
        cls,
        *,
        work_item_key: str,
        exhausted_turn_id: str,
        worktree: str,
        branch: str,
        completed_edits: list[str],
        unrun_validation: list[str],
        next_commands: list[str],
        required_label_status_adjustments: list[str],
        created_at: str,
        related_pr: str | None = None,
        repo: str = "",
    ) -> "WorklinkContinuationCheckpoint":
        checkpoint = cls(
            kind="generic",
            related_pr=related_pr,
            work_item_key=work_item_key,
            exhausted_turn_id=exhausted_turn_id,
            worktree=worktree,
            branch=branch,
            repo=repo,
            completed_edits=completed_edits,
            unrun_validation=unrun_validation,
            next_commands=next_commands,
            required_label_status_adjustments=required_label_status_adjustments,
            created_at=created_at,
            priority="high",
        )
        return cls.from_json(checkpoint.to_json())

    @classmethod
    def from_json(cls, data: Any) -> "WorklinkContinuationCheckpoint":
        if not isinstance(data, dict):
            raise TypeError("worklink continuation checkpoint must be a JSON object")
        checkpoint = cls(
            kind=str(data["kind"]),
            related_chainlink_issue_id=(
                int(data["related_chainlink_issue_id"])
                if data.get("related_chainlink_issue_id") is not None
                else None
            ),
            related_pr=(str(data["related_pr"]) if data.get("related_pr") else None),
            work_item_key=str(data["work_item_key"]),
            exhausted_turn_id=str(data["exhausted_turn_id"]),
            worktree=str(data["worktree"]),
            branch=str(data["branch"]),
            repo=str(data.get("repo") or ""),
            completed_edits=[str(item) for item in data.get("completed_edits", [])],
            unrun_validation=[str(item) for item in data.get("unrun_validation", [])],
            next_commands=[str(item) for item in data.get("next_commands", [])],
            required_label_status_adjustments=[
                str(item) for item in data.get("required_label_status_adjustments", [])
            ],
            created_at=str(data["created_at"]),
            priority=str(data.get("priority") or "normal"),
            dedupe_key=str(data.get("dedupe_key") or ""),
            version=int(data.get("version") or CONTINUATION_CHECKPOINT_VERSION),
        )
        if not checkpoint.dedupe_key:
            return cls.from_json(checkpoint.to_json())
        return checkpoint


def runs_dir(home: Path) -> Path:
    return home / "state" / "worklink" / "runs"


def continuation_checkpoints_dir(home: Path) -> Path:
    return home / "state" / "worklink" / "continuations"


def run_state_path(home: Path, issue_id: int) -> Path:
    return runs_dir(home) / f"{issue_id}.json"


def continuation_checkpoint_dedupe_key(
    checkpoint: WorklinkContinuationCheckpoint,
) -> str:
    """Stable identity for duplicate-suppressing one exhausted turn/work item."""
    identity = {
        "version": CONTINUATION_CHECKPOINT_VERSION,
        "kind": checkpoint.kind,
        "related_chainlink_issue_id": checkpoint.related_chainlink_issue_id,
        "work_item_key": checkpoint.work_item_key,
        "exhausted_turn_id": checkpoint.exhausted_turn_id,
        "worktree": checkpoint.worktree,
        "branch": checkpoint.branch,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def continuation_checkpoint_path(
    home: Path, checkpoint_or_key: WorklinkContinuationCheckpoint | str
) -> Path:
    key = (
        checkpoint_or_key
        if isinstance(checkpoint_or_key, str)
        else continuation_checkpoint_dedupe_key(checkpoint_or_key)
    )
    return continuation_checkpoints_dir(home) / f"{key}.json"


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


def save_continuation_checkpoint(
    home: Path, checkpoint: WorklinkContinuationCheckpoint
) -> tuple[Path, bool]:
    """Persist a continuation checkpoint, suppressing duplicates by identity.

    Returns ``(path, created)``. A repeated save for the same exhausted
    turn/work item returns the existing path with ``created=False`` and leaves
    the first artifact intact.
    """
    checkpoint = WorklinkContinuationCheckpoint.from_json(checkpoint.to_json())
    path = continuation_checkpoint_path(home, checkpoint)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(checkpoint.to_json(), indent=2, sort_keys=True)
    try:
        with path.open("x", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
    except FileExistsError:
        return path, False
    return path, True


def load_continuation_checkpoint(
    home: Path, checkpoint_key: str
) -> WorklinkContinuationCheckpoint | None:
    path = continuation_checkpoint_path(home, checkpoint_key)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return WorklinkContinuationCheckpoint.from_json(data)
    except (KeyError, TypeError, ValueError):
        return None


def list_continuation_checkpoints(home: Path) -> list[WorklinkContinuationCheckpoint]:
    """All readable exhausted-turn continuation checkpoints, sorted by key."""
    directory = continuation_checkpoints_dir(home)
    if not directory.exists():
        return []
    checkpoints: list[WorklinkContinuationCheckpoint] = []
    for child in sorted(directory.glob("*.json")):
        try:
            data = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            checkpoints.append(WorklinkContinuationCheckpoint.from_json(data))
        except (KeyError, TypeError, ValueError):
            continue
    return checkpoints


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
