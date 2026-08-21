from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import stat
from typing import Any

from .._atomic import atomic_write_json
from .backends.feature_factory import FactoryStatus, parse_factory_status
from .compute import LaunchHandle
from .run_state import process_is_zombie, process_start_ticks


FACTORY_RECORD_VERSION = 1
_MAX_RECORD_BYTES = 2 * 1024 * 1024


class FactoryRecordError(RuntimeError):
    pass


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
    version: int = FACTORY_RECORD_VERSION

    def __post_init__(self) -> None:
        if self.version != FACTORY_RECORD_VERSION:
            raise FactoryRecordError("unsupported factory record version")
        if not self.run_id.isascii() or not self.run_id.isdecimal() or int(self.run_id) <= 0:
            raise FactoryRecordError("factory run id must be a positive decimal string")
        if self.issue_id != int(self.run_id) or self.issue_id <= 0 or self.attempt <= 0:
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
            if (
                self.status.run_id != self.run_id
                or self.status.issue_key is None
                or self.status.issue_key != str(self.issue_id)
            ):
                raise FactoryRecordError("factory record status identity mismatch")
            if self.status.sandbox_path != self.sandbox:
                raise FactoryRecordError("factory record status sandbox mismatch")
            if self.status.pr_base is not None and self.status.pr_base != self.base_ref:
                raise FactoryRecordError("factory record status base mismatch")
        if self.controller_error is not None and len(self.controller_error.encode("utf-8")) > 65536:
            raise FactoryRecordError("factory record error exceeds size limit")

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
        }
        if set(data) != expected:
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
        return cls(
            version=int(data["version"]),
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


def factory_record_path(home: Path, run_id: str) -> Path:
    if not run_id.isascii() or not run_id.isdecimal() or int(run_id) <= 0:
        raise FactoryRecordError("factory run id must be a positive decimal string")
    return factory_records_dir(home) / f"{run_id}.json"


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


def load_factory_record(home: Path, run_id: str) -> FactoryRunRecord | None:
    if not _require_safe_directory(factory_records_dir(home), create=False):
        return None
    path = factory_record_path(home, run_id)
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FactoryRecordError("factory record is unavailable") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode) or value.st_size > _MAX_RECORD_BYTES:
        raise FactoryRecordError("factory record is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            raw = os.read(fd, _MAX_RECORD_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise FactoryRecordError("factory record cannot be read") from exc
    if len(raw) > _MAX_RECORD_BYTES or b"\x00" in raw:
        raise FactoryRecordError("factory record exceeds size limit")
    try:
        data = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactoryRecordError("factory record is malformed") from exc
    return FactoryRunRecord.from_json(data)


def list_factory_records(home: Path) -> list[FactoryRunRecord]:
    directory = factory_records_dir(home)
    if not _require_safe_directory(directory, create=False):
        return []
    records: list[FactoryRunRecord] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(".json") or not path.stem.isdecimal():
            continue
        records.append(load_factory_record(home, path.stem))
    return [record for record in records if record is not None]


def clear_factory_record(home: Path, run_id: str) -> None:
    if not _require_safe_directory(factory_records_dir(home), create=False):
        return
    path = factory_record_path(home, run_id)
    try:
        value = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise FactoryRecordError("refusing to remove non-regular factory record")
    path.unlink()


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
    except (OSError, ProcessLookupError):
        return False
    if process_is_zombie(pid) or handle.process_start_ticks is None:
        return False
    return process_start_ticks(pid) == handle.process_start_ticks


def factory_process_is_verified_dead(record: FactoryRunRecord) -> bool:
    handle = record.handle
    if (
        handle is None
        or handle.substrate != "local_subprocess"
        or handle.process_start_ticks is None
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
    except (PermissionError, OSError):
        return False
    observed = process_start_ticks(pid)
    return process_is_zombie(pid) or (
        observed is not None and observed != handle.process_start_ticks
    )
