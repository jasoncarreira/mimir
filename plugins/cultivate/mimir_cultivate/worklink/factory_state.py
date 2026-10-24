from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable, ClassVar, Iterator, Mapping, Sequence

from mimir._atomic import atomic_write_json
from .backends.base import LaunchHandle
from .run_state import process_is_zombie, process_start_ticks


class FactoryContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class FactoryStatus:
    run_id: str
    valid: bool
    sandbox_path: str
    issue_key: str | None = None
    status: str | None = None
    mode: str | None = None
    branch: str | None = None
    pr_base: str | None = None
    pr_draft: bool | None = None
    lock: str | None = None
    dead_lock: bool | None = None
    lock_session: str | None = None
    gates: dict[str, Any] | None = None
    steps: tuple[dict[str, Any], ...] | None = None
    slices: tuple[dict[str, Any], ...] | None = None
    validator: dict[str, Any] | None = None
    next_action: dict[str, Any] | None = None
    pr_url: str | None = None
    terminal_result: dict[str, Any] | None = None
    park_snapshot: str | None = None
    next: str | None = None
    next_present: bool = False

    TERMINAL_STATUSES: ClassVar[frozenset[str]] = frozenset({"completed", "blocked", "partial"})

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    @property
    def is_parked(self) -> bool:
        return self.status == "needs-human"

    def require_recovery_next(self) -> str:
        if not self.next_present or self.next is None:
            raise FactoryContractError("factory status recovery requires a nonblank next action")
        return self.next

    def to_json(self) -> dict[str, Any]:
        payload = {name: list(value) if name in {"steps", "slices"} and value is not None else value
                   for name, value in vars(self).items() if name not in {"next", "next_present"}}
        if self.next_present:
            payload["next"] = self.next
        return payload


_STATUS_MAX_BYTES = 1024 * 1024
_STATUS_ROW_FIELDS = {
    "steps": {"agent": "text", "status": "text", "attempts": "count"},
    "slices": {"id": "text", "status": "text", "attempts": "count",
               "extra_attempts": "count", "retry_limit": "count"},
    "gates": {"status": "text", "at": "nullable", "artifact": "nullable", "reviewed_head": "nullable"},
    "validator": {"verdict": "text", "report": "nullable", "reviewed_head": "nullable", "loops": "count"},
    "next_action": {"kind": "text", "subject": "nullable"},
}


def _status_text(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise FactoryContractError(f"factory status {name} must be a nonblank string")
    text = value.strip()
    if len(text.encode("utf-8")) > 16 * 1024 or "\x00" in text:
        raise FactoryContractError(f"factory status {name} is invalid")
    return text


def _status_row(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or len(json.dumps(value).encode()) > 16 * 1024:
        raise FactoryContractError(f"factory status {name} must be a bounded object")
    fields = _STATUS_ROW_FIELDS[name]
    # Factory 0.12.1 adds an optional validator.override; records retained before it omit the key.
    if set(value) - ({"override"} if name == "validator" else set()) != set(fields):
        raise FactoryContractError(f"factory status {name} fields are invalid")
    result = {}
    for key, kind in fields.items():
        item = value[key]
        if kind == "count":
            if type(item) is not int or item < 0:
                raise FactoryContractError(f"factory status {name}.{key} must be a nonnegative integer")
        else:
            item = _status_text(item, f"{name}.{key}", nullable=kind == "nullable")
        result[key] = item
    if name == "validator" and result["verdict"] not in {"GO", "GO-WITH-NITS", "NO-GO"}:
        raise FactoryContractError("factory status validator.verdict has an unknown verdict")
    if name == "validator" and "override" in value:
        override = value["override"]
        if override is not None:
            if not isinstance(override, dict) or set(override) != {"reviewed_head", "reason", "session", "at"}:
                raise FactoryContractError(
                    "factory status validator.override must be an object with reviewed_head, reason, session and at"
                )
            override = {key: _status_text(override[key], f"validator.override.{key}") for key in sorted(override)}
            if result["verdict"] != "NO-GO":
                raise FactoryContractError("factory status validator.override requires a NO-GO verdict")
            if override["reviewed_head"] != result["reviewed_head"]:
                raise FactoryContractError("factory status validator.override must name the verdict's reviewed_head")
        result["override"] = override
    if name == "next_action" and result["kind"] not in {
        "terminal", "gate", "step", "observe-slice", "blocked-slice", "dispatch-slice",
        "seed-slices", "pr", "complete",
    }:
        raise FactoryContractError("factory status next_action.kind is unknown")
    return result


def parse_factory_status(payload: bytes | str | Mapping[str, Any]) -> FactoryStatus:
    if isinstance(payload, bytes):
        if len(payload) > _STATUS_MAX_BYTES or b"\x00" in payload:
            raise FactoryContractError("factory status output exceeds bounds")
        payload = payload.decode("utf-8", "strict")
    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > _STATUS_MAX_BYTES or "\x00" in payload:
            raise FactoryContractError("factory status output exceeds bounds")
        try:
            payload = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, json.JSONDecodeError) as exc:
            raise FactoryContractError("factory status output is not one JSON object") from exc
    if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
        raise FactoryContractError("factory status must be a JSON object")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise FactoryContractError("factory status output is not one JSON object") from exc
    if len(encoded) > _STATUS_MAX_BYTES:
        raise FactoryContractError("factory status output exceeds bounds")
    if not {"run_id", "valid", "sandbox_path"} <= payload.keys():
        raise FactoryContractError("factory status missing required field")
    if type(payload["valid"]) is not bool:
        raise FactoryContractError("factory status valid must be a boolean")
    values = {name: _status_text(payload.get(name), name, nullable=name not in {
        "run_id", "sandbox_path",
    }) for name in (
        "run_id", "sandbox_path", "issue_key", "status", "mode", "branch", "pr_base",
        "lock_session", "pr_url", "park_snapshot",
    )}
    for name in ("pr_draft", "dead_lock"):
        value = payload.get(name)
        if value is not None and type(value) is not bool:
            raise FactoryContractError(f"factory status {name} must be a boolean")
        values[name] = value
    lock = _status_text(payload.get("lock"), "lock", nullable=True)
    if lock is not None and lock not in {"fresh", "stale", "absent"}:
        raise FactoryContractError("factory status lock must be fresh, stale, or absent")
    values["lock"] = lock
    for name in ("steps", "slices"):
        rows = payload.get(name)
        if rows is not None:
            if not isinstance(rows, list) or len(rows) > 1000:
                raise FactoryContractError(f"factory status {name} must be a bounded object list")
            rows = tuple(_status_row(item, name) for item in rows)
        values[name] = rows
    gates = payload.get("gates")
    if gates is not None:
        if not isinstance(gates, dict) or len(gates) > 1000:
            raise FactoryContractError("factory status gates must be a bounded object")
        gates = {_status_text(key, "gates key"): _status_row(row, "gates") for key, row in gates.items()}
    values["gates"] = gates
    for name in ("validator", "next_action"):
        values[name] = _status_row(payload[name], name) if payload.get(name) is not None else None
    terminal = payload.get("terminal_result")
    if terminal is not None and not isinstance(terminal, dict):
        raise FactoryContractError("factory status terminal_result must be an object")
    values["terminal_result"] = terminal
    values["next_present"] = "next" in payload
    values["next"] = _status_text(payload.get("next"), "next", nullable=True)
    return FactoryStatus(valid=payload["valid"], **values)


def epic_run_id(issue_id: int) -> str:
    if type(issue_id) is not int or issue_id < 1:
        raise ValueError("epic issue id must be positive")
    return f"chainlink-{issue_id}"


# Records are available before the executor and factory adapter are installed.
# Once those provider modules arrive, use their canonical types so a record
# created by the controller compares equal to one recovered from disk.
try:
    from .compute import LaunchHandle as LaunchHandle
except ModuleNotFoundError as exc:
    if exc.name != "mimir_cultivate.worklink.compute":
        raise

try:
    from .backends.feature_factory import (
        FactoryStatus as FactoryStatus,
        epic_run_id as epic_run_id,
        parse_factory_status as parse_factory_status,
    )
except ModuleNotFoundError as exc:
    if exc.name != "mimir_cultivate.worklink.backends.feature_factory":
        raise


FACTORY_RECORD_VERSION = 2
FACTORY_STOP_GRACE_S = 900
_MAX_RECORD_BYTES = 2 * 1024 * 1024
# Emitted by the factory controller and the operator stop path.
LIVE_CONTROLLER_PHASES = frozenset({"running"})
RETAINED_CONTROLLER_PHASES = frozenset({"failed", "parked", "stopped", "terminal"})


def _valid_record_run_id(run_id: str) -> bool:
    if run_id.isascii() and run_id.isdecimal():
        return bool(run_id.strip("0"))
    prefix = "chainlink-"
    issue_id = run_id.removeprefix(prefix)
    return (
        run_id.startswith(prefix)
        and issue_id.isascii()
        and issue_id.isdecimal()
        and issue_id[0] != "0"
    )


class FactoryRecordError(RuntimeError):
    pass


@dataclass(frozen=True)
class FactoryRecordLoadFailure:
    path: Path
    reason: str


@dataclass(frozen=True)
class FactoryRecordListing:
    records: tuple[FactoryRunRecord, ...]
    failures: tuple[FactoryRecordLoadFailure, ...]


@dataclass(frozen=True)
class FactoryRunRecord:
    run_id: str
    issue_id: int
    attempt: int
    repository: str
    base_ref: str
    branch: str
    launcher: str
    sandbox: str
    session: str | None
    handle: LaunchHandle | None
    status: FactoryStatus | None
    observed_at: str | None
    controller_phase: str
    controller_error: str | None = None
    transcript: str | None = None
    # Set only after the executor's successful subreaper terminal acknowledgement.
    # Driver PID death alone does not exclude detached specialists.
    children_reaped: bool = False
    version: int = FACTORY_RECORD_VERSION

    def __post_init__(self) -> None:
        if type(self.children_reaped) is not bool:
            raise FactoryRecordError("factory record children_reaped is invalid")
        if self.version != FACTORY_RECORD_VERSION:
            raise FactoryRecordError("unsupported factory record version")
        if not _valid_record_run_id(self.run_id):
            raise FactoryRecordError("factory run id is invalid")
        expected_run_ids = set(factory_record_run_ids(self.issue_id))
        if self.run_id not in expected_run_ids or self.issue_id <= 0 or self.attempt <= 0:
            raise FactoryRecordError("factory record identity is invalid")
        if not Path(self.launcher).is_absolute() or not Path(self.sandbox).is_absolute():
            raise FactoryRecordError("factory launcher and sandbox must be absolute")
        for name, value in (
            ("repository", self.repository),
            ("base_ref", self.base_ref),
            ("branch", self.branch),
            ("controller_phase", self.controller_phase),
        ):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise FactoryRecordError(f"factory record {name} is invalid")
        if self.session is not None and (not self.session.strip() or "\x00" in self.session):
            raise FactoryRecordError("factory record session is invalid")
        if self.status is not None:
            if self.status.run_id != self.run_id:
                raise FactoryRecordError("factory record status identity mismatch")
            if self.status.sandbox_path != self.sandbox:
                raise FactoryRecordError("factory record status sandbox mismatch")
            if self.status.pr_base is not None and self.status.pr_base != self.base_ref:
                raise FactoryRecordError(
                    "factory record status base mismatch: "
                    f"observed {self.status.pr_base!r}, expected {self.base_ref!r}"
                )
        if self.run_id.startswith("chainlink-") and Path(self.sandbox).name != self.run_id:
            raise FactoryRecordError("factory record sandbox does not match run id")
        if self.controller_error is not None and len(self.controller_error.encode("utf-8")) > 65536:
            raise FactoryRecordError("factory record error exceeds size limit")
        if self.transcript is not None and (
            not Path(self.transcript).is_absolute() or "\x00" in self.transcript
        ):
            raise FactoryRecordError("factory record transcript path is invalid")

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "issue_id": self.issue_id,
            "attempt": self.attempt,
            "repository": self.repository,
            "base_ref": self.base_ref,
            "branch": self.branch,
            "launcher": self.launcher,
            "sandbox": self.sandbox,
            "session": self.session,
            "handle": asdict(self.handle) if self.handle is not None else None,
            "status": self.status.to_json() if self.status is not None else None,
            "observed_at": self.observed_at,
            "controller_phase": self.controller_phase,
            "controller_error": self.controller_error,
            "transcript": self.transcript,
            "children_reaped": self.children_reaped,
        }

    @classmethod
    def from_json(cls, data: object) -> FactoryRunRecord:
        if not isinstance(data, dict):
            raise FactoryRecordError("factory record must be a JSON object")
        expected = {
            "version",
            "run_id",
            "issue_id",
            "attempt",
            "repository",
            "base_ref",
            "branch",
            "launcher",
            "sandbox",
            "session",
            "handle",
            "status",
            "observed_at",
            "controller_phase",
            "controller_error",
            "transcript",
        }
        data = dict(data)
        children_reaped = data.pop("children_reaped", False)
        if type(children_reaped) is not bool:
            raise FactoryRecordError("factory record children_reaped is invalid")
        version = data.get("version")
        legacy = version == 1 and set(data) == expected - {"transcript"}
        if version not in {1, FACTORY_RECORD_VERSION}:
            raise FactoryRecordError("unsupported factory record version")
        if (version == 1 and not legacy) or (
            version == FACTORY_RECORD_VERSION and set(data) != expected
        ):
            raise FactoryRecordError("factory record fields are invalid")
        handle_data = data["handle"]
        if handle_data is None:
            handle = None
        elif isinstance(handle_data, dict) and set(handle_data) == {
            "substrate",
            "identifier",
            "process_start_ticks",
            "shim_pid",
        }:
            handle = LaunchHandle(
                substrate=str(handle_data["substrate"]),
                identifier=str(handle_data["identifier"]),
                process_start_ticks=(
                    int(handle_data["process_start_ticks"])
                    if handle_data["process_start_ticks"] is not None
                    else None
                ),
                shim_pid=int(handle_data["shim_pid"]) if handle_data["shim_pid"] is not None else None,
            )
        else:
            raise FactoryRecordError("factory record handle is invalid")
        status_data = data["status"]
        status = parse_factory_status(status_data) if status_data is not None else None
        session = data["session"]
        if session is not None and not isinstance(session, str):
            raise FactoryRecordError("factory record session is invalid")
        observed_at = data["observed_at"]
        controller_error = data["controller_error"]
        if observed_at is not None and not isinstance(observed_at, str):
            raise FactoryRecordError("factory record observation time is invalid")
        if controller_error is not None and not isinstance(controller_error, str):
            raise FactoryRecordError("factory record controller error is invalid")
        transcript = None if legacy else data["transcript"]
        if transcript is not None and not isinstance(transcript, str):
            raise FactoryRecordError("factory record transcript path is invalid")
        return cls(
            version=FACTORY_RECORD_VERSION,
            run_id=str(data["run_id"]),
            issue_id=int(data["issue_id"]),
            attempt=int(data["attempt"]),
            repository=str(data["repository"]),
            base_ref=str(data["base_ref"]),
            branch=str(data["branch"]),
            launcher=str(data["launcher"]),
            sandbox=str(data["sandbox"]),
            session=session,
            handle=handle,
            status=status,
            observed_at=observed_at,
            controller_phase=str(data["controller_phase"]),
            controller_error=controller_error,
            transcript=transcript,
            children_reaped=children_reaped,
        )

    def observed(self, status: FactoryStatus, observed_at: str) -> FactoryRunRecord:
        return replace(
            self,
            status=status,
            session=status.lock_session or self.session,
            observed_at=observed_at,
            controller_error=None,
        )


def factory_records_dir(home: Path) -> Path:
    return home / "state" / "worklink" / "factory-runs"


@contextmanager
def factory_checkout_interlock(home: Path, *, pruning: bool = False) -> Iterator[bool]:
    """Try a home-wide, cross-process checkout lock without waiting.

    Controllers hold a shared lock from before the authoritative retained read
    through supervision/finalization (not just durable handle publication).
    Pruners hold an exclusive lock across record reads and checkout deletion,
    returning without doing either when acquisition fails. Never unlink this
    stable lock file: replacing its inode would split the interlock.
    """
    directory_fd: int | None = None
    lock_fd: int | None = None
    acquired = False
    try:
        try:
            # Walk without following symlinks, including in the home ancestors.
            directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            directory = home.absolute() / "state" / "worklink"
            for component in directory.parts[1:]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = child_fd
            lock_fd = os.open(
                "factory-checkouts.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600,
                dir_fd=directory_fd,
            )
            value = os.fstat(lock_fd)
            if stat.S_ISREG(value.st_mode) and value.st_nlink == 1:
                fcntl.flock(lock_fd, (fcntl.LOCK_EX if pruning else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                acquired = True
        except OSError:
            # Busy, unsafe or unavailable state must never authorize deletion.
            pass
        yield acquired
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if directory_fd is not None:
            os.close(directory_fd)


@contextmanager
def factory_issue_resource_lock(home: Path, issue_id: int) -> Iterator[bool]:
    """Try the stable per-issue factory resource lock without waiting."""
    if not isinstance(issue_id, int) or isinstance(issue_id, bool) or issue_id < 1:
        raise ValueError("factory resource lock issue id must be positive")
    directory_fd: int | None = None
    lock_fd: int | None = None
    acquired = False
    try:
        try:
            directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            directory = home.absolute() / "state" / "worklink"
            for component in directory.parts[1:]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = child_fd
            lock_fd = os.open(
                f"factory-issue-{issue_id}.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600,
                dir_fd=directory_fd,
            )
            value = os.fstat(lock_fd)
            if (
                stat.S_ISREG(value.st_mode)
                and value.st_nlink == 1
                and value.st_uid == os.geteuid()
                and stat.S_IMODE(value.st_mode) == 0o600
            ):
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
        except OSError:
            pass
        yield acquired
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def factory_record_run_ids(issue_id: int) -> tuple[str, str]:
    """Return canonical and legacy record keys for an epic issue."""
    return epic_run_id(issue_id), str(issue_id)


def factory_record_path(home: Path, run_id: str) -> Path:
    if not _valid_record_run_id(run_id):
        raise FactoryRecordError("factory run id is invalid")
    return factory_records_dir(home) / f"{run_id}.json"


def factory_record_archive_dir(home: Path) -> Path:
    return factory_records_dir(home) / "archive"


def _require_safe_directory(path: Path, *, create: bool) -> bool:
    absolute = path.absolute()
    chain = tuple(reversed((absolute, *absolute.parents)))
    available = True
    for current in chain:
        try:
            value = current.lstat()
        except FileNotFoundError:
            available = False
            if not create:
                continue
            try:
                current.mkdir(mode=0o700)
                value = current.lstat()
            except OSError as exc:
                raise FactoryRecordError("factory record directory is unavailable") from exc
        except OSError as exc:
            raise FactoryRecordError("factory record directory is unavailable") from exc
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            raise FactoryRecordError("factory record directory is not contained")
    return available


def save_factory_record(home: Path, record: FactoryRunRecord) -> Path:
    directory = factory_records_dir(home)
    _require_safe_directory(directory, create=True)
    path = factory_record_path(home, record.run_id)
    if path.exists() or path.is_symlink():
        value = path.lstat()
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
            raise FactoryRecordError("factory record path is not a regular file")
    payload = record.to_json()
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_RECORD_BYTES:
        raise FactoryRecordError("factory record exceeds size limit")
    atomic_write_json(path, payload, mode=0o600)
    return path


def _read_factory_record(home: Path, run_id: str) -> tuple[dict[str, Any], bytes] | None:
    if not _require_safe_directory(factory_records_dir(home), create=False):
        return None
    path = factory_record_path(home, run_id)
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FactoryRecordError("factory record is unavailable") from exc
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISREG(value.st_mode)
        or value.st_size > _MAX_RECORD_BYTES
        or value.st_uid != os.geteuid()
    ):
        raise FactoryRecordError("factory record is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > _MAX_RECORD_BYTES
                or opened.st_uid != os.geteuid()
                or (opened.st_dev, opened.st_ino) != (value.st_dev, value.st_ino)
            ):
                raise FactoryRecordError("factory record changed before it could be read")
            raw = os.read(fd, _MAX_RECORD_BYTES + 1)
        finally:
            os.close(fd)
    except FactoryRecordError:
        raise
    except OSError as exc:
        raise FactoryRecordError("factory record cannot be read") from exc
    if len(raw) > _MAX_RECORD_BYTES or b"\x00" in raw:
        raise FactoryRecordError("factory record exceeds size limit")
    try:
        data = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactoryRecordError("factory record is malformed") from exc
    if not isinstance(data, dict):
        raise FactoryRecordError("factory record must be a JSON object")
    return data, raw


def load_factory_record(home: Path, run_id: str) -> FactoryRunRecord | None:
    loaded = _read_factory_record(home, run_id)
    if loaded is None:
        return None
    data, _ = loaded
    return FactoryRunRecord.from_json(data)


def load_factory_records_for_issue(home: Path, issue_id: int) -> list[FactoryRunRecord]:
    """Load both live-store key shapes, newest attempt first and canonical on ties."""
    canonical, legacy = factory_record_run_ids(issue_id)
    # Canonical-first construction is deliberate: it is a stable-sort fallback for
    # the explicit canonical tie-break below, not an interchangeable iteration order.
    records = []
    for run_id in (canonical, legacy):
        try:
            record = load_factory_record(home, run_id)
        except Exception as exc:
            path = factory_record_path(home, run_id)
            raise FactoryRecordError(
                f"factory record {path} cannot be loaded: {type(exc).__name__}: {exc}; "
                f"archive it with 'mimir worklink archive-factory-run {issue_id} "
                f"--home {home}'"
            ) from exc
        if record is not None:
            records.append(record)
    records.sort(
        key=lambda record: (record.attempt, record.run_id == canonical),
        reverse=True,
    )
    return records


def archive_factory_record(
    home: Path,
    record: FactoryRunRecord,
    *,
    event_logger: Callable[..., None] | None = None,
    source_kind: str | None = None,
    reason: str | None = None,
) -> Path:
    with factory_checkout_interlock(home) as checkout_acquired:
        if not checkout_acquired:
            raise FactoryRecordError("factory checkout interlock unavailable")
        with factory_issue_resource_lock(home, record.issue_id) as resource_acquired:
            if not resource_acquired:
                raise FactoryRecordError("factory issue resource lock unavailable")
            return _archive_factory_record(
                home,
                record,
                event_logger=event_logger,
                source_kind=source_kind,
                reason=reason,
                verify_loaded=True,
            )


def archive_factory_record_for_issue(
    home: Path,
    run_id: str,
    issue_id: int,
    *,
    event_logger: Callable[..., None] | None = None,
    source_kind: str | None = None,
    reason: str | None = None,
) -> Path | None:
    """Archive a record by trusted identity without parsing its cached status."""
    if run_id not in factory_record_run_ids(issue_id):
        raise FactoryRecordError("factory record identity is invalid")
    loaded = _read_factory_record(home, run_id)
    if loaded is None:
        return None
    data, raw = loaded
    if (
        not isinstance(data.get("run_id"), str)
        or data["run_id"] != run_id
        or not isinstance(data.get("issue_id"), int)
        or isinstance(data["issue_id"], bool)
        or data["issue_id"] != issue_id
        or not isinstance(data.get("attempt"), int)
        or isinstance(data["attempt"], bool)
        or data["attempt"] <= 0
    ):
        raise FactoryRecordError("factory record identity is invalid")
    metadata = dict(data)
    metadata["status"] = None
    record = FactoryRunRecord.from_json(metadata)
    current = _read_factory_record(home, run_id)
    if current is None or current[1] != raw:
        raise FactoryRecordError("factory record changed before archival")
    return _archive_factory_record(
        home,
        record,
        event_logger=event_logger,
        source_kind=source_kind,
        reason=reason,
        verify_loaded=False,
    )


def _archive_factory_record(
    home: Path,
    record: FactoryRunRecord,
    *,
    event_logger: Callable[..., None] | None,
    source_kind: str | None,
    reason: str | None,
    verify_loaded: bool,
) -> Path:
    source = factory_record_path(home, record.run_id)
    if verify_loaded:
        loaded = load_factory_record(home, record.run_id)
        if loaded != record:
            raise FactoryRecordError("factory record changed before archival")
    directory = factory_record_archive_dir(home)
    _require_safe_directory(directory, create=True)
    stem = f"{record.run_id}-attempt-{record.attempt}"
    destination = directory / f"{stem}.json"
    suffix = 1
    while destination.exists() or destination.is_symlink():
        destination = directory / f"{stem}-{suffix}.json"
        suffix += 1
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise FactoryRecordError("factory record cannot be archived") from exc
    if event_logger is not None:
        if not source_kind or not reason:
            os.replace(destination, source)
            raise FactoryRecordError("factory archive event context is incomplete")
        try:
            # Shared with the silent-path inventory in Chainlink #1395: that work
            # should reuse this event rather than introduce a second archive name.
            event_logger(
                "worklink_factory_record_archived",
                source=source_kind,
                issue_id=record.issue_id,
                run_id=record.run_id,
                attempt=record.attempt,
                session=record.session,
                phase=record.controller_phase,
                reason=reason,
                archive_path=str(destination),
            )
        except Exception as exc:
            try:
                os.replace(destination, source)
            except OSError as rollback_exc:
                raise FactoryRecordError(
                    "factory archive event failed and the record could not be restored"
                ) from rollback_exc
            raise FactoryRecordError("factory archive event could not be persisted") from exc
    return destination


def list_factory_records(home: Path) -> FactoryRecordListing:
    directory = factory_records_dir(home)
    if not _require_safe_directory(directory, create=False):
        return FactoryRecordListing((), ())
    records: list[FactoryRunRecord] = []
    failures: list[FactoryRecordLoadFailure] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(".json") or not _valid_record_run_id(path.stem):
            continue
        try:
            record = load_factory_record(home, path.stem)
        except Exception as exc:
            failures.append(
                FactoryRecordLoadFailure(path, f"{type(exc).__name__}: {exc}")
            )
            continue
        if record is not None:
            records.append(record)
    return FactoryRecordListing(tuple(records), tuple(failures))


def factory_manifest_candidates(record: FactoryRunRecord) -> tuple[Path, Path]:
    """Return the root-checkout and sandbox manifests that block factory init."""
    sandbox = Path(record.sandbox)
    if (
        not _valid_record_run_id(record.run_id)
        or sandbox.parent.name != ".factory-sandboxes"
        or sandbox.name != record.run_id
    ):
        raise FactoryRecordError("factory sandbox is outside the retained-run layout")
    repository = sandbox.parent.parent
    return repository / ".factory" / record.run_id, sandbox / ".factory" / record.run_id


def report_retained_factory_records(
    home: Path,
    *,
    event_logger: Callable[..., None] | None = None,
    records: Sequence[FactoryRunRecord] | None = None,
) -> None:
    """Report factory runs whose control plane remains retained for recovery."""
    if event_logger is None:
        from mimir.event_logger import log_event_sync

        event_logger = log_event_sync
    if records is None:
        listing = list_factory_records(home)
        records = listing.records
        for failure in listing.failures:
            event_logger(
                "worklink_factory_record_load_failed",
                path=str(failure.path),
                error=failure.reason,
            )
    for record in records:
        if record.controller_phase not in RETAINED_CONTROLLER_PHASES and not (
            record.controller_phase in LIVE_CONTROLLER_PHASES
            and factory_process_is_verified_dead(record)
        ):
            continue
        event_logger(
            "worklink_factory_run_retained",
            issue_id=record.issue_id,
            run_id=record.run_id,
            attempt=record.attempt,
            phase=record.controller_phase,
            sandbox=record.sandbox,
            reason="factory handoff has not archived and verified the control plane",
        )


def factory_process_is_alive(record: FactoryRunRecord) -> bool:
    handle = record.handle
    if handle is None or handle.substrate != "local_subprocess":
        return False
    pid = handle.shim_pid
    if pid is None:
        try:
            pid = int(handle.identifier)
        except ValueError:
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass  # EPERM proves existence, not identity; check the birth marker below.
    except OSError:
        return False
    if process_is_zombie(pid) or handle.process_start_ticks is None:
        return False
    return process_start_ticks(pid) == handle.process_start_ticks


def factory_process_is_verified_dead(
    record: FactoryRunRecord, *, allow_missing_start_ticks: bool = False,
) -> bool:
    """Verify exit, optionally allowing legacy identities for operator cleanup.

    Automatic recovery still requires a recorded birth marker. An operator can
    clean a missing PID or zombie without one, but cannot infer PID reuse.
    """
    handle = record.handle
    if (
        handle is None
        or handle.substrate != "local_subprocess"
        or (handle.process_start_ticks is None and not allow_missing_start_ticks)
    ):
        return False
    pid = handle.shim_pid
    if pid is None:
        try:
            pid = int(handle.identifier)
        except ValueError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass  # Still compare birth markers; EPERM alone cannot verify exit.
    except OSError:
        return False
    observed = process_start_ticks(pid)
    return process_is_zombie(pid) or (
        handle.process_start_ticks is not None
        and observed is not None and observed != handle.process_start_ticks
    )


def _factory_observation_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
        if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed
    except (TypeError, ValueError):
        pass
    return None


def factory_run_still_going(
    record: FactoryRunRecord, status: FactoryStatus | None, *, now: datetime,
) -> bool:
    """An unverifiable driver or a current observed session prevents reclamation."""
    if not factory_process_is_verified_dead(record):
        return True
    observed = _factory_observation_time(record.observed_at)
    return bool(
        status is not None and status.lock == "fresh" and observed is not None
        and now < observed + timedelta(seconds=FACTORY_STOP_GRACE_S)
    )


def factory_run_stopped_since(
    record: FactoryRunRecord, status: FactoryStatus | None,
) -> datetime | None:
    """Latest durable observation of this stopped driver (including its session)."""
    if not factory_process_is_verified_dead(record):
        return None
    # There is no separate persisted death timestamp: observed_at is the last
    # durable death/session observation. Never infer one from checkout mtime.
    return _factory_observation_time(record.observed_at)
