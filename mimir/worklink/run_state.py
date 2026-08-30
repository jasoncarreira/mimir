"""Durable Worklink leaf-run state and process liveness helpers.

The Worklink controller (``mimir worklink run``) is a *detached* subprocess the
ready-queue poller spawns. It survives the poller's own exit, but a container
restart (``docker restart`` / SIGTERM) kills it mid-run. State now covers both
the claim controller and the local worker so operators can distinguish a live
run from stale Chainlink labels and safely cancel a verified process.

State lives at ``<home>/state/worklink/runs/<issue_id>.json`` and is deleted on
terminal completion. Older version-1 files remain readable for the persistent
worker reattach path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import errno
import json
import os
import uuid
from pathlib import Path
from typing import Any

RUN_STATE_VERSION = 2


@dataclass(frozen=True)
class OrphanBlockRecord:
    """Provenance for a block owned by one orphaned attempt checkout."""

    issue_id: int
    attempt: int
    checkout: str
    publication_outcome: str
    comment: str

    @classmethod
    def from_json(cls, data: Any) -> "OrphanBlockRecord":
        if not isinstance(data, dict):
            raise TypeError("orphan block record must be a JSON object")
        return cls(
            issue_id=int(data["issue_id"]),
            attempt=int(data["attempt"]),
            checkout=str(data["checkout"]),
            publication_outcome=str(data["publication_outcome"]),
            comment=str(data["comment"]),
        )


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
    checkout: str = ""
    process_start_ticks: int | None = None
    shim_pid: int | None = None
    phase: str = "spawned"
    version: int = RUN_STATE_VERSION

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> "WorklinkRunState":
        if not isinstance(data, dict):
            raise TypeError("worklink run state must be a JSON object")
        version = int(data.get("version") or 1)
        if version not in (1, 2):
            raise ValueError("unsupported worklink run state version")
        identifier = str(data["handle_identifier"])
        shim_pid = int(data["shim_pid"]) if data.get("shim_pid") is not None else None
        ticks = int(data["process_start_ticks"]) if data.get("process_start_ticks") is not None else None
        phase = str(data.get("phase") or "spawned")
        local_handle = str(data["handle_substrate"]) == "local_subprocess"
        if phase == "spawned" and local_handle:
            if shim_pid is None:
                if not identifier.isascii() or not identifier.isdecimal():
                    raise ValueError("direct run state requires a decimal PID handle")
            else:
                try:
                    parsed = uuid.UUID(identifier, version=4)
                except ValueError as exc:
                    raise ValueError("worker run state requires a canonical UUIDv4 handle") from exc
                if (
                    version != 2
                    or str(parsed) != identifier
                    or shim_pid <= 0
                    or ticks is None
                ):
                    raise ValueError("worker run state requires UUID, shim PID, and process start ticks")
        elif shim_pid is not None:
            raise ValueError("shim PID is only valid for a version-2 worker handle")
        if phase == "claiming" and identifier != str(int(identifier)):
            raise ValueError("claiming run state requires a controller PID")
        return cls(
            issue_id=int(data["issue_id"]),
            attempt=int(data["attempt"]),
            backend=str(data["backend"]),
            compute_name=str(data["compute_name"]),
            handle_substrate=str(data["handle_substrate"]),
            handle_identifier=identifier,
            branch=str(data["branch"]),
            base_ref=str(data["base_ref"]),
            local_base=str(data.get("local_base") or data["base_ref"]),
            repo=str(data.get("repo") or ""),
            repo_url=str(data.get("repo_url") or ""),
            test_command=(str(data["test_command"]) if data.get("test_command") else None),
            started_at=str(data["started_at"]),
            checkout=str(data.get("checkout") or ""),
            process_start_ticks=ticks,
            shim_pid=shim_pid,
            phase=phase,
            version=version,
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
    clear_orphan_block_record(home, state.issue_id)
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


def orphan_blocks_dir(home: Path) -> Path:
    return home / "state" / "worklink" / "orphan-blocks"


def orphan_block_path(home: Path, issue_id: int) -> Path:
    return orphan_blocks_dir(home) / f"{issue_id}.json"


def save_orphan_block_record(home: Path, record: OrphanBlockRecord) -> Path:
    path = orphan_block_path(home, record.issue_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(record), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def load_orphan_block_record(home: Path, issue_id: int) -> OrphanBlockRecord | None:
    try:
        data = json.loads(orphan_block_path(home, issue_id).read_text(encoding="utf-8"))
        return OrphanBlockRecord.from_json(data)
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return None


def list_orphan_block_records(home: Path) -> list[OrphanBlockRecord]:
    directory = orphan_blocks_dir(home)
    if not directory.exists():
        return []
    records: list[OrphanBlockRecord] = []
    for path in sorted(directory.glob("*.json")):
        record = load_orphan_block_record(home, int(path.stem)) if path.stem.isdigit() else None
        if record is not None:
            records.append(record)
    return records


def clear_orphan_block_record(home: Path, issue_id: int) -> None:
    try:
        orphan_block_path(home, issue_id).unlink()
    except OSError:
        return


def _process_stat(pid: int) -> tuple[str, int] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # The comm field is parenthesized and may contain spaces or parentheses.
        fields = stat[stat.rfind(")") + 2 :].split()
        return fields[0], int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def process_start_ticks(pid: int) -> int | None:
    """Read Linux's stable process birth marker (field 22 of ``/proc/PID/stat``)."""
    observed = _process_stat(pid)
    return observed[1] if observed is not None else None


def process_is_zombie(pid: int) -> bool:
    observed = _process_stat(pid)
    return observed is not None and observed[0] == "Z"


def _state_pid(state: WorklinkRunState) -> int | None:
    if state.shim_pid is not None:
        return state.shim_pid
    try:
        return int(state.handle_identifier)
    except ValueError:
        return None


def process_is_alive(state: WorklinkRunState) -> bool:
    """Probe a recorded local process without shelling out to ``ps``.

    Permission errors mean the process exists. A start-tick mismatch is dead for
    this run even though the reused PID itself is alive.
    """
    pid = _state_pid(state)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return True
        return False
    if process_is_zombie(pid):
        return False
    if state.process_start_ticks is None:
        return True
    observed = process_start_ticks(pid)
    return observed is not None and observed == state.process_start_ticks


def process_identity_verified(state: WorklinkRunState) -> bool:
    """Whether it is safe to signal the PID recorded by ``state``."""
    if state.compute_name != "local_subprocess" or state.process_start_ticks is None:
        return False
    pid = _state_pid(state)
    if pid is None:
        return False
    return process_start_ticks(pid) == state.process_start_ticks and process_is_alive(state)


def elapsed_seconds(state: WorklinkRunState, *, now: datetime | None = None) -> float:
    try:
        started = datetime.fromisoformat(state.started_at)
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, ((now or datetime.now(UTC)) - started.astimezone(UTC)).total_seconds())


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
