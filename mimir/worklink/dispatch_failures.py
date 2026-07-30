"""Durable accounting for detached Worklink dispatch failures."""

from __future__ import annotations

import fcntl
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .._atomic import atomic_write_json
from ..redaction import redact_text

STATE_FILE = "dispatch_failures.json"
INITIAL_BACKOFF_MINUTES = 15
MAX_BACKOFF_MINUTES = 240


def terminal_error(value: BaseException | str) -> str:
    """Return one bounded, scrubbed terminal line suitable for durable output."""
    if isinstance(value, BaseException):
        text = f"{type(value).__name__}: {value}"
    else:
        text = value
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return redact_text(lines[-1] if lines else "Worklink run failed")[:1000]


def error_signature(error: str) -> str:
    return hashlib.sha256(error.encode("utf-8")).hexdigest()[:16]


def load_failure_state(state_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((state_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "issues": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("issues"), dict):
        return {"version": 1, "issues": {}}
    return payload


def save_failure_state(state_dir: Path, state: dict[str, Any]) -> None:
    atomic_write_json(state_dir / STATE_FILE, state)


@contextmanager
def failure_state_transaction(state_dir: Path):
    """Serialize read-modify-write updates from concurrent detached runs."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / f"{STATE_FILE}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_failure_state(state_dir)
        try:
            yield state
        finally:
            save_failure_state(state_dir, state)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def record_failure(
    state_dir: Path,
    *,
    issue_id: int,
    attempt: int | None,
    exit_status: int,
    error: str,
    log_path: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    safe_error = terminal_error(error)
    signature = error_signature(safe_error)
    with failure_state_transaction(state_dir) as state:
        key = str(issue_id)
        prior = state["issues"].get(key)
        prior = prior if isinstance(prior, dict) else {}
        consecutive = (
            int(prior.get("consecutive", 0)) + 1
            if prior.get("signature") == signature and prior.get("active") is True
            else 1
        )
        delay = min(
            INITIAL_BACKOFF_MINUTES * (2 ** min(consecutive - 1, 8)),
            MAX_BACKOFF_MINUTES,
        )
        entry = {
            "active": True,
            "issue_id": issue_id,
            "attempt": attempt,
            "attempt_consumed": attempt is not None,
            "exit_status": exit_status,
            "terminal_error": safe_error,
            "signature": signature,
            "consecutive": consecutive,
            "failed_at": now.isoformat(),
            "retry_after": (now + timedelta(minutes=delay)).isoformat(),
            "log_path": redact_text(log_path or ""),
            "notified_signatures": list(prior.get("notified_signatures") or []),
        }
        state["issues"][key] = entry
    return entry


def record_success(state_dir: Path, issue_id: int) -> None:
    with failure_state_transaction(state_dir) as state:
        entry = state["issues"].get(str(issue_id))
        if not isinstance(entry, dict) or entry.get("active") is not True:
            return
        entry["active"] = False
        entry["consecutive"] = 0


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
