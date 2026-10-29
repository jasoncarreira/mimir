"""Tracker evidence: lifecycle requests and notices, held as ids, enums and states only."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class EvidenceError(RuntimeError):
    """The evidence file exists but cannot be trusted; it is never overwritten."""


def tracker_dir(home: Path) -> Path:
    return home / "state" / "worklink" / "tracker"


def evidence_path(home: Path) -> Path:
    return tracker_dir(home) / "evidence.json"


def _empty() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "items": {}, "notices": []}


def _load(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except OSError as exc:
        raise EvidenceError("tracker evidence is unreadable") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvidenceError("tracker evidence is not valid JSON") from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != SCHEMA_VERSION
        or not isinstance(data.get("items"), dict)
        or not isinstance(data.get("notices"), list)
    ):
        raise EvidenceError("tracker evidence has an unexpected shape")
    return data


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_evidence(home: Path) -> dict[str, Any]:
    """Return a snapshot without taking the lock; writers replace the file atomically."""

    return _load(evidence_path(home))


@contextmanager
def evidence_transaction(home: Path) -> Iterator[dict[str, Any]]:
    """Hold the evidence lock; the mutated document is persisted on a clean exit when it changed."""

    directory = tracker_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "evidence.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = _load(evidence_path(home))
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) == before:
                return
            fd, tmp_name = tempfile.mkstemp(dir=directory, prefix="evidence.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, evidence_path(home))
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
            _fsync_directory(directory)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
