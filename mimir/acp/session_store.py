from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from mimir._atomic import atomic_write_json
from mimir.acp.sdk import RequestError

Replayability = Literal["replayable", "overflowed", "expired", "deleted", "io_failed"]
Lifecycle = Literal["active", "deleted"]


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    thread_id: str
    owner_principal: str
    lifecycle: Lifecycle
    replayability: Replayability
    journal_path: Path
    metadata_path: Path


class SessionStore:
    def __init__(self, home: Path | str) -> None:
        self.root = Path(home).expanduser() / ".mimir" / "acp"
        self._ensure_root()

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("unsafe ACP session directory")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise OSError("ACP session directory has a different owner")

    @staticmethod
    def canonical_session_id(value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError, TypeError):
            raise _invalid_session() from None
        if parsed.version != 4 or str(parsed) != value:
            raise _invalid_session()
        return value

    def paths(self, session_id: str) -> tuple[Path, Path]:
        session_id = self.canonical_session_id(session_id)
        return self.root / f"{session_id}.jsonl", self.root / f"{session_id}.meta.json"

    def create_session(self, owner_principal: str) -> SessionRecord:
        session_id = str(uuid.uuid4())
        journal, metadata = self.paths(session_id)
        fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        payload = self._payload(session_id, owner_principal, "active", "replayable")
        try:
            atomic_write_json(metadata, payload, mode=0o600, indent=None)
            self._fsync_parent()
        except BaseException:
            journal.unlink(missing_ok=True)
            raise
        return self._record(payload, journal, metadata)

    create_owned_session = create_session

    def load_owned(self, session_id: str, owner_principal: str) -> SessionRecord:
        journal, metadata = self.paths(session_id)
        try:
            self._validate_file(metadata)
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            self._validate_payload(payload, session_id)
        except RequestError:
            raise
        except BaseException:
            raise _invalid_session() from None
        if payload["owner_principal"] != owner_principal:
            raise _invalid_session()
        if payload["lifecycle"] == "deleted":
            raise _unavailable("deleted")
        reason = payload["replayability"]
        if reason != "replayable":
            raise _unavailable(reason)
        try:
            self._validate_file(journal)
        except BaseException:
            self.try_mark(session_id, owner_principal, "io_failed")
            raise _unavailable("io_failed") from None
        return self._record(payload, journal, metadata)

    def load_owned_live(self, session_id: str, owner_principal: str) -> SessionRecord:
        journal, metadata = self.paths(session_id)
        try:
            self._validate_file(metadata)
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            self._validate_payload(payload, session_id)
        except RequestError:
            raise
        except BaseException:
            raise _invalid_session() from None
        if payload["owner_principal"] != owner_principal:
            raise _invalid_session()
        if payload["lifecycle"] != "active" or payload["replayability"] not in {"replayable", "overflowed"}:
            raise _unavailable(str(payload["replayability"]))
        if payload["replayability"] == "replayable":
            try:
                self._validate_file(journal)
            except BaseException:
                self.try_mark(session_id, owner_principal, "io_failed")
                raise _unavailable("io_failed") from None
        return self._record(payload, journal, metadata)

    def mark(self, session_id: str, owner_principal: str, reason: Replayability) -> SessionRecord:
        journal, metadata = self.paths(session_id)
        self._validate_file(metadata)
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        self._validate_payload(payload, session_id)
        if payload["owner_principal"] != owner_principal:
            raise _invalid_session()
        lifecycle: Lifecycle = "deleted" if reason == "deleted" else "active"
        updated = self._payload(session_id, owner_principal, lifecycle, reason)
        atomic_write_json(metadata, updated, mode=0o600, indent=None)
        return self._record(updated, journal, metadata)

    def try_mark(self, session_id: str, owner_principal: str, reason: Replayability) -> bool:
        try:
            self.mark(session_id, owner_principal, reason)
            return True
        except BaseException:
            return False

    def delete_owned_session(self, session_id: str, owner_principal: str) -> None:
        record = self._load_for_cleanup(session_id, owner_principal)
        if record.lifecycle != "deleted":
            self.mark(session_id, owner_principal, "deleted")
        record.journal_path.unlink(missing_ok=True)
        self._fsync_parent()

    def sweep_expired(self, ttl_days: int, *, now_ns: int | None = None) -> None:
        if ttl_days <= 0:
            raise ValueError("ttl_days must be positive")
        now_ns = now_ns if now_ns is not None else __import__("time").time_ns()
        ttl_ns = ttl_days * 86_400_000_000_000
        for metadata in self.root.glob("*.meta.json"):
            session_id = metadata.name.removesuffix(".meta.json")
            try:
                journal, _ = self.paths(session_id)
                self._validate_file(metadata)
                payload = json.loads(metadata.read_text(encoding="utf-8"))
                self._validate_payload(payload, session_id)
                reason = payload["replayability"]
                if reason == "replayable":
                    self._validate_file(journal)
                    if now_ns > journal.stat().st_mtime_ns + ttl_ns:
                        self.mark(session_id, payload["owner_principal"], "expired")
                        journal.unlink(missing_ok=True)
                        self._fsync_parent()
                elif reason in {"expired", "deleted", "overflowed", "io_failed"}:
                    journal.unlink(missing_ok=True)
                    self._fsync_parent()
            except BaseException:
                continue

    def _load_for_cleanup(self, session_id: str, owner: str) -> SessionRecord:
        journal, metadata = self.paths(session_id)
        try:
            self._validate_file(metadata)
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            self._validate_payload(payload, session_id)
        except BaseException:
            raise _invalid_session() from None
        if payload["owner_principal"] != owner:
            raise _invalid_session()
        return self._record(payload, journal, metadata)

    @staticmethod
    def _payload(session_id: str, owner: str, lifecycle: Lifecycle, replayability: Replayability) -> dict[str, object]:
        return {"schema_version": 1, "session_id": session_id, "thread_id": f"acp:{session_id}", "owner_principal": owner, "lifecycle": lifecycle, "replayability": replayability}

    @staticmethod
    def _validate_payload(payload: object, session_id: str) -> None:
        if not isinstance(payload, dict):
            raise ValueError
        expected = {"schema_version", "session_id", "thread_id", "owner_principal", "lifecycle", "replayability"}
        if set(payload) != expected or payload["schema_version"] != 1 or payload["session_id"] != session_id or payload["thread_id"] != f"acp:{session_id}" or not isinstance(payload["owner_principal"], str):
            raise ValueError
        pair = (payload["lifecycle"], payload["replayability"])
        if pair not in {("active", "replayable"), ("active", "overflowed"), ("active", "expired"), ("active", "io_failed"), ("deleted", "deleted")}:
            raise ValueError

    @staticmethod
    def _record(payload: dict[str, object], journal: Path, metadata: Path) -> SessionRecord:
        return SessionRecord(
            str(payload["session_id"]),
            str(payload["thread_id"]),
            str(payload["owner_principal"]),
            cast(Lifecycle, payload["lifecycle"]),
            cast(Replayability, payload["replayability"]),
            journal,
            metadata,
        )

    @staticmethod
    def _validate_file(path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise OSError("unsafe ACP session file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise OSError("ACP session file has a different owner")

    load_owned_session = load_owned
    sweep = sweep_expired

    def _fsync_parent(self) -> None:
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _invalid_session() -> RequestError:
    return RequestError(-32602, "Invalid session")


def _unavailable(reason: str) -> RequestError:
    return RequestError(-32603, f"Session replay unavailable: {reason}")
