from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable

from .._atomic import atomic_write_json
from .backends.feature_factory import FactoryStatus, epic_run_id, parse_factory_status
from .compute import LaunchHandle
from .run_state import process_is_zombie, process_start_ticks


FACTORY_RECORD_VERSION = 2
_MAX_RECORD_BYTES = 2 * 1024 * 1024
_RETAINED_CONTROLLER_PHASES = frozenset({"failed", "parked", "stopped", "terminal"})


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
    version: int = FACTORY_RECORD_VERSION

    def __post_init__(self) -> None:
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


def load_factory_records_for_issue(home: Path, issue_id: int) -> list[FactoryRunRecord]:
    """Load both live-store key shapes, newest attempt first and canonical on ties."""
    canonical, legacy = factory_record_run_ids(issue_id)
    # Canonical-first construction is deliberate: it is a stable-sort fallback for
    # the explicit canonical tie-break below, not an interchangeable iteration order.
    records = [
        record
        for run_id in (canonical, legacy)
        if (record := load_factory_record(home, run_id)) is not None
    ]
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
    source = factory_record_path(home, record.run_id)
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


def list_factory_records(home: Path) -> list[FactoryRunRecord]:
    directory = factory_records_dir(home)
    if not _require_safe_directory(directory, create=False):
        return []
    records: list[FactoryRunRecord] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(".json") or not _valid_record_run_id(path.stem):
            continue
        records.append(load_factory_record(home, path.stem))
    return [record for record in records if record is not None]


def factory_manifest_candidates(record: FactoryRunRecord) -> tuple[Path, Path]:
    """Return the root-checkout and sandbox manifests that block factory init."""
    sandbox = Path(record.sandbox)
    if sandbox.parent.name != ".factory-sandboxes" or sandbox.name != record.run_id:
        raise FactoryRecordError("factory sandbox is outside the retained-run layout")
    repository = sandbox.parent.parent
    return repository / ".factory" / record.run_id, sandbox / ".factory" / record.run_id


def report_retained_factory_records(
    home: Path,
    *,
    event_logger: Callable[..., None] | None = None,
) -> None:
    """Report factory runs whose control plane remains retained for recovery."""
    if event_logger is None:
        from ..event_logger import log_event_sync

        event_logger = log_event_sync
    for record in list_factory_records(home):
        if record.controller_phase not in _RETAINED_CONTROLLER_PHASES:
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
