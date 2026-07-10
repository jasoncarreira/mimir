"""Durable Worklink run state for controller resume-after-restart (#561).

The Worklink controller (``mimir worklink run``) is a *detached* subprocess the
ready-queue poller spawns. It survives the poller's own exit, but a container
restart (``docker restart`` / SIGTERM) kills it mid-run. Originally this module
persisted the minimal handle a fresh controller needed to reattach to a
surviving remote worker (docker_sibling / ecs-runtask); after the #832
substrate cleanup local_subprocess is the only Worklink compute substrate, its
runs die with the controller, and nothing is ever persisted.

State files at ``<home>/state/worklink/runs/<issue_id>.json`` from older
deployments are still readable so ``mimir worklink run --reattach`` and the
startup reconcile keep working unchanged against them; the write path is
inert.

State lives at ``<home>/state/worklink/runs/<issue_id>.json`` and is deleted on
terminal completion, so a file present at startup means "a worker we may still
be able to reattach to".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

RUN_STATE_VERSION = 1


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
