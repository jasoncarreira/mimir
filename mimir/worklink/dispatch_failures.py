"""Durable accounting for detached Worklink dispatch failures."""

from __future__ import annotations

import fcntl
import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .._atomic import atomic_write_json
from ..redaction import redact_text

STATE_FILE = "dispatch_failures.json"
POLLER_NAME = "worklink-ready-queue"
INITIAL_BACKOFF_MINUTES = 15
MAX_BACKOFF_MINUTES = 240
MAX_NOTIFIED_SIGNATURES = 32
_DELIVERY_RECEIPTS_DIR = ".delivery-receipts"
_TRANSIENT_CONTENTION_MARKERS = (
    ("unable to create", "index.lock"),
    ("cannot lock ref",),
    ("could not write new index file",),
)


def dispatch_failure_state_dir(home: Path) -> Path:
    """Return the durable failure ledger owned by a Worklink home."""
    return home / "state" / "pollers" / POLLER_NAME


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


def delivery_receipt_exists(state_dir: Path, delivery_key: str) -> bool:
    """Return whether the framework durably accepted a poller record."""
    digest = hashlib.sha256(delivery_key.encode()).hexdigest()
    return (state_dir / _DELIVERY_RECEIPTS_DIR / digest).is_file()


@contextmanager
def failure_state_transaction(state_dir: Path):
    """Serialize read-modify-write updates from concurrent detached runs."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / f"{STATE_FILE}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_failure_state(state_dir)
        try:
            yield state
        except Exception:
            raise
        else:
            save_failure_state(state_dir, state)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def is_transient_contention(error: str) -> bool:
    """Return whether a pre-claim failure is short-lived Git lock contention."""
    normalized = error.casefold()
    return any(
        all(marker in normalized for marker in markers)
        for markers in _TRANSIENT_CONTENTION_MARKERS
    )


def record_failure(
    state_dir: Path,
    *,
    issue_id: int,
    attempt: int | None,
    exit_status: int,
    error: BaseException | str,
    log_path: str | None,
    preserved_ref: str | None = None,
    preservation_error: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    full_error = redact_text(str(error))[:4000]
    safe_error = terminal_error(error)
    signature = error_signature(safe_error)
    if attempt is None and is_transient_contention(full_error):
        return {
            "active": False,
            "issue_id": issue_id,
            "attempt": None,
            "attempt_consumed": False,
            "exit_status": exit_status,
            "terminal_error": safe_error,
            "signature": signature,
            "transient_contention": True,
            "failed_at": now.isoformat(),
            "retry_after": None,
            "log_path": redact_text(log_path or ""),
            "preserved_ref": redact_text(preserved_ref or "")[:1000] or None,
            "preservation_error": redact_text(preservation_error or "")[:1000] or None,
            "notified_signatures": [],
        }
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
            "occurrence_id": uuid.uuid4().hex,
            "consecutive": consecutive,
            "failed_at": now.isoformat(),
            "retry_after": (now + timedelta(minutes=delay)).isoformat(),
            "log_path": redact_text(log_path or ""),
            "preserved_ref": redact_text(preserved_ref or "")[:1000] or None,
            "preservation_error": redact_text(preservation_error or "")[:1000] or None,
            "notified_signatures": list(prior.get("notified_signatures") or [])[
                -MAX_NOTIFIED_SIGNATURES:
            ],
        }
        state["issues"][key] = entry
    return entry


def pending_failure_alerts(
    state_dir: Path, *, now: datetime | None = None
) -> tuple[set[int], list[dict[str, object]]]:
    """Return active backoffs and undelivered alerts without mutating delivery state."""
    now = now or datetime.now(UTC)
    backed_off: set[int] = set()
    alerts: list[dict[str, object]] = []
    with failure_state_transaction(state_dir) as state:
        for entry in state["issues"].values():
            if not isinstance(entry, dict) or entry.get("active") is not True:
                continue
            try:
                issue_id = int(entry["issue_id"])
            except (KeyError, TypeError, ValueError):
                continue
            retry_after = parse_time(entry.get("retry_after"))
            if retry_after is not None and now < retry_after:
                backed_off.add(issue_id)
            signature = str(entry.get("signature") or "")
            notified = entry.get("notified_signatures")
            notified = list(notified) if isinstance(notified, list) else []
            if signature and signature not in notified:
                alerts.append({
                    "signal": "worklink_run_failure_escalated",
                    "source_id": f"worklink-run-failure:{issue_id}:{signature}",
                    "issue_id": issue_id,
                    "attempt": entry.get("attempt"),
                    "attempt_consumed": entry.get("attempt_consumed"),
                    "exit_status": entry.get("exit_status"),
                    "terminal_error": entry.get("terminal_error"),
                    "error_signature": signature,
                    "failure_occurrence_id": entry.get("occurrence_id"),
                    "log": entry.get("log_path"),
                    "preserved_ref": entry.get("preserved_ref"),
                    "preservation_error": entry.get("preservation_error"),
                    "retry_after": entry.get("retry_after"),
                    "routing_instructions": (
                        "Notify the operator that a detached Worklink run failed. "
                        "Include the run-log path, terminal error, and any preserved "
                        "ref or preservation error."
                    ),
                })
    return backed_off, alerts


def mark_failure_notified(
    state_dir: Path,
    issue_id: int,
    signature: str,
    occurrence_id: str | None,
) -> None:
    """Record delivery only if the emitted failure occurrence remains current."""
    with failure_state_transaction(state_dir) as state:
        entry = state["issues"].get(str(issue_id))
        if (
            not isinstance(entry, dict)
            or entry.get("active") is not True
            or entry.get("signature") != signature
            or entry.get("occurrence_id") != occurrence_id
        ):
            return
        notified = entry.get("notified_signatures")
        notified = list(notified) if isinstance(notified, list) else []
        if signature not in notified:
            notified.append(signature)
        entry["notified_signatures"] = notified[-MAX_NOTIFIED_SIGNATURES:]


def record_success(state_dir: Path, issue_id: int) -> None:
    with failure_state_transaction(state_dir) as state:
        entry = state["issues"].get(str(issue_id))
        if not isinstance(entry, dict) or entry.get("active") is not True:
            return
        entry["active"] = False
        entry["consecutive"] = 0
        entry["notified_signatures"] = []


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
