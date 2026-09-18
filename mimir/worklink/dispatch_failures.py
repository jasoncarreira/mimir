"""Durable accounting for autonomous Worklink dispatches and outcomes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from .._atomic import atomic_write_json
from ..redaction import redact_text
from .attention import (
    Accounting,
    AccountingScope,
    AttentionCause,
    AttentionKind,
    AttentionSchemaError,
    AttentionSource,
    AttentionTarget,
    ClaimIdentity,
    SettlementBasis,
    SOURCE_RULES,
    facts_to_json,
    validate_occurrence_contract,
)

STATE_FILE = "dispatch_failures.json"
POLLER_NAME = "worklink-ready-queue"
# Private parent/child transport, not an operator configuration surface.
RESERVATION_ENV = "MIMIR_" + "WORKLINK_RESERVATION_ID"
SCHEMA_VERSION = 2
INITIAL_BACKOFF_MINUTES = 15
MAX_BACKOFF_MINUTES = 240
MAX_NOTIFIED_SIGNATURES = 32
MAX_TRANSIENT_RECURRENCES = 2
TRANSIENT_RETRY_SECONDS = (30, 120)
_MAX_STATE_BYTES = 16 * 1024 * 1024
_DELIVERY_RECEIPTS_DIR = ".delivery-receipts"
_TRANSIENT_CONTENTION_MARKERS = (
    ("unable to create", "index.lock"),
    ("cannot lock ref",),
    ("could not write new index file",),
)


class FailureStateError(RuntimeError):
    """The accounting ledger is missing safety or schema guarantees."""


def dispatch_failure_state_dir(home: Path) -> Path:
    return home / "state" / "pollers" / POLLER_NAME


def terminal_error(value: BaseException | str) -> str:
    if isinstance(value, BaseException):
        text = f"{type(value).__name__}: {value}"
    else:
        text = value
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return redact_text(lines[-1] if lines else "Worklink run failed")[:1000]


def error_signature(error: str) -> str:
    return hashlib.sha256(error.encode("utf-8")).hexdigest()[:16]


def _empty_state() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "issues": {}}


def _empty_issue() -> dict[str, Any]:
    return {
        "next_sequence": 1,
        "reservations": {},
        "settlements": {},
        "occurrences": {},
        "contention": {},
        "success_witnesses": {},
        "legacy": None,
    }


def _json_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FailureStateError(f"duplicate ledger key: {key}")
        result[key] = value
    return result


def _read_state_file(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir / STATE_FILE
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FailureStateError("outcome ledger is unavailable") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode) or value.st_size > _MAX_STATE_BYTES:
        raise FailureStateError("outcome ledger is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            raw = os.read(fd, _MAX_STATE_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise FailureStateError("outcome ledger cannot be read") from exc
    if len(raw) > _MAX_STATE_BYTES or b"\x00" in raw:
        raise FailureStateError("outcome ledger exceeds its size limit")
    try:
        payload = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_json_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FailureStateError("outcome ledger is malformed") from exc
    if not isinstance(payload, dict):
        raise FailureStateError("outcome ledger must be an object")
    return payload


def load_failure_state(state_dir: Path) -> dict[str, Any]:
    """Load the version-1 telemetry view used during the serial rollout."""
    payload = _read_state_file(state_dir)
    if payload is None:
        return {"version": 1, "issues": {}}
    if payload.get("version") == 1:
        _validate_v1(payload)
        return payload
    _validate_v2(payload)
    return {
        "version": 1,
        "issues": {
            key: issue["legacy"]["row"]
            for key, issue in payload["issues"].items()
            if isinstance(issue.get("legacy"), dict)
        },
    }


def load_outcome_state(state_dir: Path) -> dict[str, Any]:
    """Load the strict v2 accounting view without masking any read failure."""
    payload = _read_state_file(state_dir)
    if payload is None:
        return _empty_state()
    if payload.get("version") == 1:
        _validate_v1(payload)
        return _migrate_v1(payload)
    _validate_v2(payload)
    return payload


def save_failure_state(state_dir: Path, state: dict[str, Any]) -> None:
    if state.get("version") == 1:
        _validate_v1(state)
    else:
        _validate_v2(state)
    atomic_write_json(state_dir / STATE_FILE, state, mode=0o600)


def delivery_receipt_exists(state_dir: Path, delivery_key: str) -> bool:
    digest = hashlib.sha256(delivery_key.encode()).hexdigest()
    return (state_dir / _DELIVERY_RECEIPTS_DIR / digest).is_file()


@contextmanager
def failure_state_transaction(state_dir: Path) -> Iterator[dict[str, Any]]:
    """Serialize ledger changes and never replace an unreadable/unknown ledger."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / f"{STATE_FILE}.lock"
    if lock_path.exists() and lock_path.is_symlink():
        raise FailureStateError("outcome ledger lock cannot be a symlink")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        raw = _read_state_file(state_dir)
        if raw is None:
            state = {"version": 1, "issues": {}}
            raw_v2 = None
        elif raw.get("version") == 1:
            _validate_v1(raw)
            state = raw
            raw_v2 = None
        else:
            _validate_v2(raw)
            raw_v2 = raw
            state = {
                "version": 1,
                "issues": {
                    key: issue["legacy"]["row"]
                    for key, issue in raw["issues"].items()
                    if isinstance(issue.get("legacy"), dict)
                },
            }
        try:
            yield state
        except BaseException:
            raise
        else:
            if raw_v2 is None:
                save_failure_state(state_dir, state)
            else:
                for key, row in state["issues"].items():
                    issue = raw_v2["issues"].setdefault(key, _empty_issue())
                    issue["legacy"] = {"key": key, "row": row}
                for key, issue in raw_v2["issues"].items():
                    if key not in state["issues"]:
                        issue["legacy"] = None
                save_failure_state(state_dir, raw_v2)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def outcome_state_transaction(state_dir: Path) -> Iterator[dict[str, Any]]:
    """Open the accounting ledger and atomically migrate valid v1 state."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / f"{STATE_FILE}.lock"
    if lock_path.exists() and lock_path.is_symlink():
        raise FailureStateError("outcome ledger lock cannot be a symlink")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_outcome_state(state_dir)
        try:
            yield state
        except BaseException:
            raise
        else:
            save_failure_state(state_dir, state)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _validate_v1(state: Mapping[str, object]) -> None:
    if set(state) != {"version", "issues"} or state.get("version") != 1:
        raise FailureStateError("legacy outcome ledger fields are invalid")
    issues = state.get("issues")
    if not isinstance(issues, dict):
        raise FailureStateError("legacy outcome ledger issues are invalid")
    for key, row in issues.items():
        if not isinstance(key, str) or not isinstance(row, dict):
            raise FailureStateError("legacy outcome row is invalid")
        if row.get("issue_id") is not None and row.get("issue_id") != int(key):
            raise FailureStateError("legacy outcome identity is invalid")


def _validate_v2(state: Mapping[str, object]) -> None:
    if set(state) != {"version", "issues"} or state.get("version") != SCHEMA_VERSION:
        raise FailureStateError("outcome ledger top-level fields are invalid")
    issues = state.get("issues")
    if not isinstance(issues, dict):
        raise FailureStateError("outcome ledger issues are invalid")
    for issue_key, issue in issues.items():
        if not isinstance(issue_key, str) or not issue_key.isascii() or not issue_key.isdecimal():
            raise FailureStateError("outcome ledger issue key is invalid")
        if not isinstance(issue, dict) or set(issue) != {
            "next_sequence", "reservations", "settlements", "occurrences",
            "contention", "success_witnesses", "legacy",
        }:
            raise FailureStateError("outcome ledger issue fields are invalid")
        if type(issue["next_sequence"]) is not int or issue["next_sequence"] < 1:
            raise FailureStateError("outcome ledger sequence is invalid")
        for field in ("reservations", "settlements", "occurrences", "contention", "success_witnesses"):
            if not isinstance(issue[field], dict):
                raise FailureStateError(f"outcome ledger {field} is invalid")
        if issue["legacy"] is not None and not isinstance(issue["legacy"], dict):
            raise FailureStateError("outcome ledger legacy row is invalid")
        for reservation_id, reservation in issue["reservations"].items():
            _validate_reservation(reservation_id, reservation, int(issue_key))
        sequences = [item["sequence"] for item in issue["reservations"].values()]
        if len(sequences) != len(set(sequences)) or any(
            sequence >= issue["next_sequence"] for sequence in sequences
        ):
            raise FailureStateError("reservation sequences are inconsistent")
        for key, settlement in issue["settlements"].items():
            _validate_settlement(key, settlement, int(issue_key), issue)
        for occurrence_id, occurrence in issue["occurrences"].items():
            _validate_occurrence(occurrence_id, occurrence, int(issue_key), issue)
        for witness_id, witness in issue["success_witnesses"].items():
            _validate_witness(witness_id, witness, int(issue_key))
        for signature, recurrence in issue["contention"].items():
            if (
                not isinstance(signature, str)
                or not isinstance(recurrence, dict)
                or set(recurrence) != {"count", "last_at", "retry_after", "disposition"}
                or type(recurrence["count"]) is not int
                or recurrence["count"] < 1
                or recurrence["disposition"] not in {"transient_retry", "stop"}
            ):
                raise FailureStateError("contention recurrence is invalid")
            if _parse_required_time(recurrence["last_at"]) is None:
                raise FailureStateError("contention recurrence time is invalid")
            if recurrence["disposition"] == "transient_retry":
                if _parse_required_time(recurrence["retry_after"]) is None:
                    raise FailureStateError("contention retry deadline is invalid")
            elif recurrence["retry_after"] is not None:
                raise FailureStateError("stopped contention carries a retry deadline")
        for settlement in issue["settlements"].values():
            reservation = issue["reservations"][settlement["reservation_id"]]
            if reservation["binding"]["claim"] != settlement["claim"]:
                raise FailureStateError("settlement does not match reservation claim")
            if reservation["claim_state"] != "settled":
                raise FailureStateError("settlement reservation claim is not settled")
            operation = reservation["operations"].get(settlement["terminal_operation_id"])
            if operation is None or operation["state"] != "finished":
                raise FailureStateError("settlement terminal operation is invalid")
            if not set(settlement["proof_ids"]).issubset(reservation["positive_proofs"]):
                raise FailureStateError("settlement proof was not retained by reservation")
        for occurrence in issue["occurrences"].values():
            if occurrence["kind"] == "success":
                witness_id = occurrence["facts"]["witness_id"]
                if witness_id not in issue["success_witnesses"]:
                    raise FailureStateError("success occurrence witness is absent")
                witness = issue["success_witnesses"][witness_id]
                reservation = issue["reservations"][occurrence["reservation_id"]]
                if (
                    witness["origin"] != "autonomous"
                    or witness["target"] != reservation["target"]
                    or witness["claim"] != reservation["binding"]["claim"]
                    or witness["run_id"] != reservation["binding"]["run_id"]
                    or witness["sandbox"] != reservation["binding"]["sandbox"]
                    or occurrence["facts"]["evidence_sha256"]
                    != witness["evidence_sha256"]
                ):
                    raise FailureStateError("success occurrence witness binding is invalid")
            reservation = issue["reservations"][occurrence["reservation_id"]]
            operation = reservation["operations"][occurrence["operation_id"]]
            linked = issue["occurrences"].get(operation["occurrence_id"])
            if linked is None or linked["operation_id"] != occurrence["operation_id"]:
                raise FailureStateError("operation occurrence linkage is invalid")
        for reservation_id, reservation in issue["reservations"].items():
            if reservation["state"] == "terminal" and any(
                operation["state"] == "running"
                for operation in reservation["operations"].values()
            ):
                raise FailureStateError("terminal reservation retains a running operation")
            lifecycle_id = reservation["lifecycle_operation_id"]
            lifecycle = reservation["operations"].get(lifecycle_id)
            start_id = occurrence_identity(reservation_id, lifecycle_id, "start")
            success_id = occurrence_identity(reservation_id, lifecycle_id, "terminal")
            if reservation["state"] == "active" and (
                lifecycle is None or start_id not in issue["occurrences"]
            ):
                raise FailureStateError("active reservation lacks its START occurrence")
            if reservation["disposition"] == "success" and (
                reservation["state"] != "terminal"
                or success_id not in issue["occurrences"]
                or issue["occurrences"][success_id]["kind"] != "success"
            ):
                raise FailureStateError("successful reservation lacks its success occurrence")
            if reservation["claim_state"] == "settled":
                claim = ClaimIdentity.from_json(reservation["binding"]["claim"])
                settlement = issue["settlements"].get(claim.key)
                if (
                    settlement is None
                    or settlement["reservation_id"] != reservation_id
                    or settlement["claim"] != reservation["binding"]["claim"]
                ):
                    raise FailureStateError("settled reservation lacks its exact settlement")
            for operation_id, operation in reservation["operations"].items():
                if operation["state"] != "finished":
                    continue
                linked = issue["occurrences"].get(operation["occurrence_id"])
                if (
                    linked is None
                    or linked["reservation_id"] != reservation_id
                    or linked["operation_id"] != operation_id
                    or (
                        linked["source"] != operation["source"]
                        and not (
                            linked["kind"] == "success"
                            and operation["source"] in {"leaf_start", "factory_start"}
                        )
                    )
                ):
                    raise FailureStateError("finished operation lacks its exact occurrence")


def _validate_reservation(reservation_id: object, value: object, issue_id: int) -> None:
    if not _is_uuid(reservation_id) or not isinstance(value, dict) or set(value) != {
        "sequence", "issue_id", "target", "autonomous", "state", "owner", "binding",
        "claim_state", "operations", "positive_proofs", "disposition", "retry_after",
        "lifecycle_operation_id",
    }:
        raise FailureStateError("reservation fields are invalid")
    if value["issue_id"] != issue_id or type(value["sequence"]) is not int or value["sequence"] < 1:
        raise FailureStateError("reservation identity is invalid")
    if value["target"] not in {item.value for item in AttentionTarget} or type(value["autonomous"]) is not bool:
        raise FailureStateError("reservation target is invalid")
    if value["state"] not in {"prepared", "active", "terminal"}:
        raise FailureStateError("reservation state is invalid")
    if value["claim_state"] not in {"none", "intent", "confirmed", "absent", "settled"}:
        raise FailureStateError("reservation claim state is invalid")
    if value["disposition"] not in {"pending", "success", "stop", "transient_retry"}:
        raise FailureStateError("reservation disposition is invalid")
    legal_dispositions = {
        "prepared": {"pending", "transient_retry"},
        "active": {"pending"},
        "terminal": {"success", "stop"},
    }
    if value["disposition"] not in legal_dispositions[value["state"]]:
        raise FailureStateError("reservation state and disposition are inconsistent")
    if not isinstance(value["operations"], dict) or not isinstance(value["positive_proofs"], list):
        raise FailureStateError("reservation operations/proofs are invalid")
    if not all(
        isinstance(proof, str)
        and re.fullmatch(
            r"(?:leaf_outcome:[0-9a-f]{64}|factory_partial|"
            r"factory_merged_slice:[0-9]+|factory_step:[0-9]+:"
            r"(?:accepted|rejected):[1-9][0-9]*|"
            r"(?:leaf|factory)_completion:[0-9a-f]{64})",
            proof,
        )
        for proof in value["positive_proofs"]
    ):
        raise FailureStateError("reservation proof is invalid")
    owner = value["owner"]
    if owner is not None and (
        not isinstance(owner, dict) or set(owner) != {"pid", "start_ticks"}
        or type(owner["pid"]) is not int or owner["pid"] <= 0
        or (
            owner["start_ticks"] is not None
            and (type(owner["start_ticks"]) is not int or owner["start_ticks"] <= 0)
        )
    ):
        raise FailureStateError("reservation owner is invalid")
    binding = value["binding"]
    if not isinstance(binding, dict) or set(binding) != {"run_id", "sandbox", "claim"}:
        raise FailureStateError("reservation binding is invalid")
    if binding["claim"] is not None:
        try:
            ClaimIdentity.from_json(binding["claim"])
        except AttentionSchemaError as exc:
            raise FailureStateError(str(exc)) from exc
    for field in ("run_id", "sandbox"):
        if binding[field] is not None and (
            not isinstance(binding[field], str)
            or not binding[field]
            or "\x00" in binding[field]
            or len(binding[field].encode()) > 4096
        ):
            raise FailureStateError(f"reservation {field} binding is invalid")
    if binding["sandbox"] is not None and not Path(binding["sandbox"]).is_absolute():
        raise FailureStateError("reservation sandbox binding must be absolute")
    if value["claim_state"] == "none" and binding["claim"] is not None:
        raise FailureStateError("unclaimed reservation carries a claim")
    if value["claim_state"] in {"intent", "confirmed", "absent", "settled"} and binding["claim"] is None:
        raise FailureStateError("claimed reservation has no claim identity")
    if value["state"] == "active" and value["claim_state"] != "confirmed":
        raise FailureStateError("active reservation claim is not confirmed")
    if value["claim_state"] == "absent" and value["state"] == "active":
        raise FailureStateError("absent claim cannot own active work")
    if value["claim_state"] == "settled" and value["state"] != "terminal":
        raise FailureStateError("settled claim reservation is not terminal")
    retry_after = value["retry_after"]
    if value["disposition"] == "transient_retry":
        if value["state"] != "prepared" or _parse_required_time(retry_after) is None:
            raise FailureStateError("transient retry reservation lacks a deadline")
    elif retry_after is not None:
        raise FailureStateError("non-retry reservation carries a retry deadline")
    lifecycle = value["lifecycle_operation_id"]
    if not isinstance(lifecycle, str) or not lifecycle:
        raise FailureStateError("reservation lifecycle operation is invalid")
    for operation_id, operation in value["operations"].items():
        if (
            not isinstance(operation_id, str)
            or (
                operation_id not in {value["lifecycle_operation_id"], "legacy-v1"}
                and re.fullmatch(r"op-[1-9][0-9]*", operation_id) is None
            )
            or not isinstance(operation, dict)
            or set(operation) != {
            "source", "owner", "state", "started_at", "finished_at", "occurrence_id",
            }
        ):
            raise FailureStateError("operation fields are invalid")
        try:
            AttentionSource(operation["source"])
        except (ValueError, TypeError) as exc:
            raise FailureStateError("operation source is invalid") from exc
        if operation["state"] not in {"running", "finished"}:
            raise FailureStateError("operation state is invalid")
        operation_owner = operation["owner"]
        if operation_owner is not None and (
            not isinstance(operation_owner, dict)
            or set(operation_owner) != {"pid", "start_ticks"}
            or type(operation_owner["pid"]) is not int
            or operation_owner["pid"] <= 0
            or (
                operation_owner["start_ticks"] is not None
                and (
                    type(operation_owner["start_ticks"]) is not int
                    or operation_owner["start_ticks"] <= 0
                )
            )
        ):
            raise FailureStateError("operation owner is invalid")
        if _parse_required_time(operation["started_at"]) is None:
            raise FailureStateError("operation start time is invalid")
        if operation["state"] == "running" and (
            operation["finished_at"] is not None
            or operation["occurrence_id"] is not None
        ):
            raise FailureStateError("running operation has terminal fields")
        if operation["state"] == "finished" and (
            not isinstance(operation["finished_at"], str)
            or not isinstance(operation["occurrence_id"], str)
        ):
            raise FailureStateError("finished operation lacks terminal fields")
        if operation["state"] == "finished" and _parse_required_time(
            operation["finished_at"]
        ) is None:
            raise FailureStateError("operation finish time is invalid")


def _validate_settlement(key: object, value: object, issue_id: int, issue: Mapping[str, Any]) -> None:
    if not isinstance(key, str) or not isinstance(value, dict) or set(value) != {
        "claim", "reservation_id", "consumed", "basis", "proof_ids",
        "terminal_operation_id", "settled_at",
    }:
        raise FailureStateError("settlement fields are invalid")
    try:
        claim = ClaimIdentity.from_json(value["claim"])
    except AttentionSchemaError as exc:
        raise FailureStateError(str(exc)) from exc
    if claim.key != key or claim.issue_id != issue_id or value["reservation_id"] not in issue["reservations"]:
        raise FailureStateError("settlement identity is invalid")
    if type(value["consumed"]) is not bool or value["basis"] not in {item.value for item in SettlementBasis}:
        raise FailureStateError("settlement accounting is invalid")
    proofs = value["proof_ids"]
    if not isinstance(proofs, list) or not all(isinstance(item, str) and item for item in proofs):
        raise FailureStateError("settlement proofs are invalid")
    if value["consumed"] != bool(proofs) or (value["consumed"] and value["basis"] != "positive_work"):
        raise FailureStateError("settlement consumption lacks positive proof")
    if not value["consumed"] and value["basis"] != "no_work":
        raise FailureStateError("nonconsuming settlement basis is invalid")
    if _parse_required_time(value["settled_at"]) is None:
        raise FailureStateError("settlement time is invalid")


def _validate_occurrence(key: object, value: object, issue_id: int, issue: Mapping[str, Any]) -> None:
    if not isinstance(key, str) or not isinstance(value, dict) or set(value) != {
        "reservation_id", "operation_id", "kind", "source", "cause", "created_at",
        "facts", "proof_ids", "accounting", "delivery_key", "execution", "handling",
    }:
        raise FailureStateError("occurrence fields are invalid")
    reservation = issue["reservations"].get(value["reservation_id"])
    if reservation is None or value["operation_id"] not in reservation["operations"]:
        raise FailureStateError("occurrence reservation operation is invalid")
    expected = occurrence_identity(value["reservation_id"], value["operation_id"], _kind_slot(value["kind"]))
    if key != expected:
        raise FailureStateError("occurrence identity is invalid")
    try:
        accounting = Accounting.from_json(value["accounting"])
        validate_occurrence_contract(
            kind=AttentionKind(value["kind"]), source=AttentionSource(value["source"]),
            cause=None if value["cause"] is None else AttentionCause(value["cause"]),
            facts=value["facts"], accounting=accounting, proof_ids=tuple(value["proof_ids"]),
        )
    except (AttentionSchemaError, ValueError, TypeError) as exc:
        raise FailureStateError(str(exc)) from exc
    if accounting.claim is not None and accounting.claim.issue_id != issue_id:
        raise FailureStateError("occurrence claim issue mismatch")
    if accounting.scope == AccountingScope.BOUND_CLAIM:
        if issue["settlements"].get(accounting.settlement_key) is None:
            raise FailureStateError("bound occurrence settlement is absent")
    if value["delivery_key"] != f"worklink-attention:{issue_id}:{key}":
        raise FailureStateError("occurrence delivery key is invalid")
    if _parse_required_time(value["created_at"]) is None:
        raise FailureStateError("occurrence creation time is invalid")
    if value["facts"].get("type") == "factory" and value["facts"].get("issue_id") != issue_id:
        raise FailureStateError("factory occurrence issue mismatch")
    if value["facts"].get("type") == "lifecycle_start":
        if value["facts"]["claim"] != reservation["binding"]["claim"]:
            raise FailureStateError("START facts do not match reservation claim")
        if value["facts"].get("run_id") != reservation["binding"]["run_id"] or value[
            "facts"
        ].get("sandbox") != reservation["binding"]["sandbox"]:
            raise FailureStateError("START facts do not match reservation binding")
    operation = reservation["operations"][value["operation_id"]]
    if operation["source"] != value["source"] and not (
        value["kind"] == "success"
        and operation["source"] in {"leaf_start", "factory_start"}
    ):
        raise FailureStateError("occurrence source does not match its operation")
    if accounting.scope == AccountingScope.BOUND_CLAIM:
        settlement = issue["settlements"][accounting.settlement_key]
        if (
            accounting.claim.to_json() != settlement["claim"]
            or accounting.consumed != settlement["consumed"]
            or value["reservation_id"] != settlement["reservation_id"]
            or list(value["proof_ids"]) != settlement["proof_ids"]
        ):
            raise FailureStateError("occurrence accounting does not match settlement")
    elif accounting.scope == AccountingScope.NO_NEW_CLAIM and value["proof_ids"]:
        raise FailureStateError("unclaimed occurrence cannot carry work proof")
    execution = value["execution"]
    if execution is not None:
        if not isinstance(execution, dict) or set(execution) != {
            "event_source_id", "turn_id", "inspection_id", "inspection_sha256",
            "inspection", "owner", "starts", "state", "park_reason",
        }:
            raise FailureStateError("occurrence execution fields are invalid")
        if type(execution["starts"]) is not int or execution["starts"] not in {1, 2}:
            raise FailureStateError("occurrence execution bound is invalid")
        if execution["state"] not in {"running", "recorded", "parked"}:
            raise FailureStateError("occurrence execution state is invalid")
        if execution["park_reason"] not in {
            None, "turn_failed", "blank_report", "execution_bound", "integrity_error"
        }:
            raise FailureStateError("occurrence execution park reason is invalid")
        if execution["state"] == "parked" and execution["park_reason"] is None:
            raise FailureStateError("parked occurrence execution needs a reason")
        if execution["state"] != "parked" and execution["park_reason"] is not None:
            raise FailureStateError("non-parked execution carries a park reason")
        for field in ("event_source_id", "turn_id", "inspection_id"):
            if (
                not isinstance(execution[field], str)
                or not execution[field]
                or "\x00" in execution[field]
                or len(execution[field].encode()) > 1024
            ):
                raise FailureStateError(f"occurrence execution {field} is invalid")
        if execution["event_source_id"] != value["delivery_key"]:
            raise FailureStateError("execution source does not match occurrence delivery")
        if not isinstance(execution["inspection"], dict):
            raise FailureStateError("occurrence execution inspection is invalid")
        inspection_hash = hashlib.sha256(
            json.dumps(
                execution["inspection"], sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        if execution["inspection_sha256"] != inspection_hash:
            raise FailureStateError("occurrence execution inspection hash mismatch")
        execution_owner = execution["owner"]
        if (
            not isinstance(execution_owner, dict)
            or set(execution_owner) != {"pid", "start_ticks"}
            or type(execution_owner["pid"]) is not int
            or execution_owner["pid"] <= 0
            or (
                execution_owner["start_ticks"] is not None
                and type(execution_owner["start_ticks"]) is not int
            )
        ):
            raise FailureStateError("occurrence execution owner is invalid")
    handling = value["handling"]
    if handling is not None:
        if execution is None or execution["state"] != "recorded":
            raise FailureStateError("handled occurrence has no accepted recorded execution")
        if not isinstance(handling, dict) or set(handling) != {
            "event_source_id", "turn_id", "inspection_id", "record_sha256",
            "result", "report", "handled_at",
        }:
            raise FailureStateError("occurrence handling fields are invalid")
        lifecycle_results = {
            "lifecycle_reported", "lifecycle_noop", "lifecycle_read_error"
        }
        diagnosis_results = {
            "resolved_noop", "reported_unresolved", "reported_read_error"
        }
        allowed = lifecycle_results if value["kind"] in {"start", "success"} else diagnosis_results
        if handling["result"] not in allowed:
            raise FailureStateError("occurrence handling result is invalid for kind")
        if not isinstance(handling["report"], str) or not handling["report"].strip():
            raise FailureStateError("occurrence handling report is blank")
        if any(
            handling[field] != execution[field]
            for field in ("event_source_id", "turn_id", "inspection_id")
        ):
            raise FailureStateError("handling correlation does not match execution")
        if not isinstance(handling["record_sha256"], str) or re.fullmatch(
            r"[0-9a-f]{64}", handling["record_sha256"]
        ) is None:
            raise FailureStateError("handling durable record hash is invalid")
        if _parse_required_time(handling["handled_at"]) is None:
            raise FailureStateError("handling timestamp is invalid")


def _validate_witness(key: object, value: object, issue_id: int) -> None:
    if not isinstance(key, str) or not isinstance(value, dict) or set(value) != {
        "issue_id", "target", "origin", "claim", "run_id", "sandbox", "completed_at",
        "observed_at", "evidence_path", "evidence_sha256", "branch", "head_sha", "pr_url",
        "next", "next_present",
    }:
        raise FailureStateError("success witness fields are invalid")
    if value["issue_id"] != issue_id or value["target"] not in {"leaf", "factory"}:
        raise FailureStateError("success witness identity is invalid")
    if value["origin"] not in {"autonomous", "manual"} or type(value["next_present"]) is not bool:
        raise FailureStateError("success witness origin is invalid")
    path = value["evidence_path"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise FailureStateError("success witness evidence path is invalid")
    if success_witness_identity(value) != key:
        raise FailureStateError("success witness digest is invalid")
    if not isinstance(value["evidence_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["evidence_sha256"]
    ):
        raise FailureStateError("success witness evidence hash is invalid")
    if value["next"] is not None and value["next_present"] is not True:
        raise FailureStateError("success witness next presence is contradictory")
    if value["origin"] == "manual" and value["claim"] is not None:
        raise FailureStateError("manual success witness cannot claim autonomous work")
    if value["origin"] == "autonomous" and value["claim"] is None:
        raise FailureStateError("autonomous success witness lacks its claim")
    if value["claim"] is not None:
        try:
            claim = ClaimIdentity.from_json(value["claim"])
        except AttentionSchemaError as exc:
            raise FailureStateError(str(exc)) from exc
        if claim.issue_id != issue_id:
            raise FailureStateError("success witness claim issue mismatch")
    for field in ("completed_at", "observed_at"):
        if _parse_required_time(value[field]) is None:
            raise FailureStateError(f"success witness {field} is invalid")
    for field in ("branch", "head_sha"):
        if not isinstance(value[field], str) or not value[field] or "\x00" in value[field]:
            raise FailureStateError(f"success witness {field} is invalid")
    if re.fullmatch(r"[0-9a-f]{40,64}", value["head_sha"]) is None:
        raise FailureStateError("success witness head is invalid")
    if value["target"] == "leaf" and (
        value["run_id"] is not None
        or value["sandbox"] is not None
        or value["next"] is not None
        or value["next_present"] is not False
    ):
        raise FailureStateError("leaf success witness carries factory binding")
    if value["target"] == "factory" and value["origin"] == "autonomous" and (
        not isinstance(value["run_id"], str)
        or not value["run_id"]
        or not isinstance(value["sandbox"], str)
        or not Path(value["sandbox"]).is_absolute()
    ):
        raise FailureStateError("factory success witness binding is invalid")


def _migrate_v1(state: Mapping[str, Any]) -> dict[str, Any]:
    migrated = _empty_state()
    now = datetime.now(UTC).isoformat()
    for issue_key, original in state["issues"].items():
        row = json.loads(json.dumps(original))
        issue = _empty_issue()
        issue["legacy"] = {"key": issue_key, "row": row}
        if row.get("active") is True:
            reservation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mimir-worklink-v1:{issue_key}"))
            operation_id = "legacy-v1"
            occurrence_id = occurrence_identity(reservation_id, operation_id, "terminal")
            issue["reservations"][reservation_id] = {
                "sequence": 1, "issue_id": int(issue_key), "target": "leaf", "autonomous": True,
                "state": "terminal", "owner": None,
                "binding": {"run_id": None, "sandbox": None, "claim": None},
                "claim_state": "none", "operations": {
                    operation_id: {"source": "legacy_v1", "owner": None, "state": "finished",
                                   "started_at": row.get("failed_at") or now,
                                   "finished_at": row.get("failed_at") or now,
                                   "occurrence_id": occurrence_id}
                },
                "positive_proofs": [], "disposition": "stop", "retry_after": None,
                "lifecycle_operation_id": "lifecycle",
            }
            facts = {
                "type": "legacy", "original_key": issue_key,
                "signature": row.get("signature"), "occurrence": row.get("occurrence_id"),
                "row": row,
            }
            issue["occurrences"][occurrence_id] = {
                "reservation_id": reservation_id, "operation_id": operation_id,
                "kind": "legacy_attention", "source": "legacy_v1", "cause": "legacy_unknown",
                "created_at": row.get("failed_at") or now, "facts": facts, "proof_ids": [],
                "accounting": Accounting(AccountingScope.UNKNOWN, None, None, None).to_json(),
                "delivery_key": f"worklink-attention:{issue_key}:{occurrence_id}",
                "execution": None, "handling": None,
            }
            issue["next_sequence"] = 2
        migrated["issues"][issue_key] = issue
    return migrated


def reserve_dispatch(
    state_dir: Path, *, issue_id: int, target: AttentionTarget | str, autonomous: bool,
    reservation_id: str | None = None, owner_pid: int | None = None,
    owner_start_ticks: int | None = None, binding: Mapping[str, object] | None = None,
) -> str:
    """Persist a prepared dispatch identity before repository reads or spawn."""
    if issue_id <= 0:
        raise ValueError("issue_id must be positive")
    target_value = AttentionTarget(target).value
    reservation_id = reservation_id or str(uuid.uuid4())
    if not _is_uuid(reservation_id):
        raise FailureStateError("reservation id must be a canonical UUID")
    with outcome_state_transaction(state_dir) as state:
        issue = state["issues"].setdefault(str(issue_id), _empty_issue())
        existing = issue["reservations"].get(reservation_id)
        if existing is not None:
            if (existing["issue_id"], existing["target"], existing["autonomous"]) != (
                issue_id, target_value, autonomous
            ):
                raise FailureStateError("inherited reservation identity mismatch")
            return reservation_id
        normalized_binding = {"run_id": None, "sandbox": None, "claim": None}
        if binding:
            unknown = set(binding) - set(normalized_binding)
            if unknown:
                raise FailureStateError("reservation binding fields are invalid")
            normalized_binding.update(binding)
        owner = None
        if owner_pid is not None:
            owner = {"pid": owner_pid, "start_ticks": owner_start_ticks}
        lifecycle_operation_id = "lifecycle"
        issue["reservations"][reservation_id] = {
            "sequence": issue["next_sequence"], "issue_id": issue_id, "target": target_value,
            "autonomous": autonomous, "state": "prepared", "owner": owner,
            "binding": normalized_binding, "claim_state": "none", "operations": {},
            "positive_proofs": [], "disposition": "pending", "retry_after": None,
            "lifecycle_operation_id": lifecycle_operation_id,
        }
        issue["next_sequence"] += 1
    return reservation_id


def reservation_from_environment(
    state_dir: Path, *, issue_id: int, target: AttentionTarget | str,
    autonomous: bool, environ: Mapping[str, str] = os.environ,
) -> str | None:
    if not autonomous:
        return None
    if RESERVATION_ENV in environ:
        inherited = environ[RESERVATION_ENV]
        # Startup recovery for a pre-v2 run deliberately carries an empty
        # private reference.  It remains autonomous for policy purposes, but it
        # must not manufacture provenance for work the ledger never reserved.
        if not inherited:
            return None
        if not _is_uuid(inherited):
            raise FailureStateError("inherited reservation id is invalid")
        with outcome_state_transaction(state_dir) as state:
            reservation = _reservation(state, issue_id, inherited)
            if reservation["target"] != AttentionTarget(target).value:
                raise FailureStateError("inherited reservation target mismatch")
            if reservation["autonomous"] is not True:
                raise FailureStateError("inherited reservation is not autonomous")
            if reservation["state"] == "terminal":
                raise FailureStateError("terminal reservation cannot be inherited")
            if reservation["disposition"] == "transient_retry":
                deadline = parse_time(reservation["retry_after"])
                if deadline is None or datetime.now(UTC) < deadline:
                    raise FailureStateError("inherited transient retry is not due")
        bind_reservation_owner(
            state_dir,
            issue_id=issue_id,
            reservation_id=inherited,
            pid=os.getpid(),
            start_ticks=_process_start_ticks(os.getpid()),
            recovery=True,
        )
        return inherited
    retry_id = authorized_retry_reservation_id(
        state_dir, issue_id=issue_id, target=target
    )
    if retry_id is not None:
        bind_reservation_owner(
            state_dir,
            issue_id=issue_id,
            reservation_id=retry_id,
            pid=os.getpid(),
            start_ticks=_process_start_ticks(os.getpid()),
            recovery=True,
        )
        return retry_id
    return reserve_dispatch(
        state_dir,
        issue_id=issue_id,
        target=target,
        autonomous=True,
        owner_pid=os.getpid(),
        owner_start_ticks=_process_start_ticks(os.getpid()),
    )


def bind_reservation_owner(
    state_dir: Path,
    *,
    issue_id: int,
    reservation_id: str,
    pid: int,
    start_ticks: int | None,
    recovery: bool = False,
) -> None:
    """Bind a prepared launch to the exact spawned process without guessing."""
    if pid <= 0:
        raise FailureStateError("reservation owner pid is invalid")
    with outcome_state_transaction(state_dir) as state:
        reservation = _reservation(state, issue_id, reservation_id)
        if reservation["state"] == "terminal":
            raise FailureStateError("terminal reservation cannot acquire an owner")
        owner = {"pid": pid, "start_ticks": start_ticks}
        existing = reservation["owner"]
        if existing is not None and existing != owner:
            fresh_handoff = (
                reservation["state"] == "prepared"
                and reservation["disposition"] == "pending"
                and not reservation["operations"]
            )
            retry_handoff = (
                reservation["state"] == "prepared"
                and reservation["disposition"] == "transient_retry"
                and _owner_verified_dead(existing)
            )
            recovery_handoff = recovery and _owner_verified_dead(existing)
            # A fresh prepared poller owner may hand off once to its child; a
            # bounded transient retry may hand off again only after verified
            # death of the prior child.
            if (
                reservation["operations"]
                and not fresh_handoff
                and not retry_handoff
                and not recovery_handoff
            ) or existing["pid"] == pid:
                raise FailureStateError("reservation owner changed")
        reservation["owner"] = owner


def _process_start_ticks(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return int(fields[21]) if len(fields) > 21 else None
    except (OSError, ValueError):
        return None


def _owner_verified_dead(owner: Mapping[str, object]) -> bool:
    pid = owner.get("pid")
    expected = owner.get("start_ticks")
    if type(pid) is not int or type(expected) is not int:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    observed = _process_start_ticks(pid)
    return observed is not None and observed != expected


def bind_claim(
    state_dir: Path, *, issue_id: int, reservation_id: str, claim: ClaimIdentity,
    confirmed: bool,
) -> None:
    with outcome_state_transaction(state_dir) as state:
        reservation = _reservation(state, issue_id, reservation_id)
        existing = reservation["binding"]["claim"]
        if existing is not None and existing != claim.to_json():
            raise FailureStateError("reservation cannot bind a different claim")
        if reservation["state"] == "terminal":
            raise FailureStateError("terminal reservation cannot bind a claim")
        current = reservation["claim_state"]
        if confirmed:
            if current not in {"intent", "confirmed"}:
                raise FailureStateError("claim confirmation is not valid from current state")
        elif current not in {"none", "intent"}:
            raise FailureStateError("claim intent is not valid from current state")
        reservation["binding"]["claim"] = claim.to_json()
        reservation["claim_state"] = "confirmed" if confirmed else "intent"


def mark_claim_absent(
    state_dir: Path, *, issue_id: int, reservation_id: str, claim: ClaimIdentity
) -> None:
    """Record a strict history negative without forgiving or settling the intent."""
    with outcome_state_transaction(state_dir) as state:
        reservation = _reservation(state, issue_id, reservation_id)
        if reservation["state"] == "terminal":
            raise FailureStateError("terminal reservation cannot change claim state")
        if (
            reservation["claim_state"] != "intent"
            or reservation["binding"]["claim"] != claim.to_json()
        ):
            raise FailureStateError("only the exact pending claim intent can be absent")
        reservation["claim_state"] = "absent"


def confirm_claim_and_start(
    state_dir: Path, *, issue_id: int, reservation_id: str, claim: ClaimIdentity,
    run_id: str | None = None, sandbox: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically confirm the exact claim, activate, and emit its sole START."""
    from .attention import LifecycleStartFacts

    timestamp = (now or datetime.now(UTC)).isoformat()
    with outcome_state_transaction(state_dir) as state:
        issue = state["issues"].get(str(issue_id))
        reservation = _reservation(state, issue_id, reservation_id)
        if not reservation["autonomous"]:
            raise FailureStateError("manual reservations cannot emit START")
        existing_claim = reservation["binding"]["claim"]
        if existing_claim is not None and existing_claim != claim.to_json():
            raise FailureStateError("reservation claim confirmation conflicts")
        for name, supplied in (("run_id", run_id), ("sandbox", sandbox)):
            existing = reservation["binding"][name]
            if existing is not None and supplied != existing:
                raise FailureStateError(f"reservation {name} binding changed")
            if supplied is not None:
                reservation["binding"][name] = supplied
        if reservation["claim_state"] not in {"intent", "confirmed"}:
            raise FailureStateError("claim cannot activate from current state")
        if reservation["disposition"] == "transient_retry":
            deadline = parse_time(reservation["retry_after"])
            if deadline is None or (now or datetime.now(UTC)) < deadline:
                raise FailureStateError("transient retry cannot activate before its deadline")
            reservation["disposition"] = "pending"
            reservation["retry_after"] = None
        reservation["binding"]["claim"] = claim.to_json()
        reservation["claim_state"] = "confirmed"
        lifecycle_operation_id = reservation["lifecycle_operation_id"]
        operation = reservation["operations"].get(lifecycle_operation_id)
        if operation is not None and operation["occurrence_id"] is not None:
            return issue["occurrences"][operation["occurrence_id"]]
        if reservation["state"] == "terminal":
            raise FailureStateError("terminal reservation cannot reactivate")
        reservation["state"] = "active"
        operation_id = lifecycle_operation_id
        source = AttentionSource.LEAF_START if reservation["target"] == "leaf" else AttentionSource.FACTORY_START
        reservation["operations"][operation_id] = {
            "source": source.value,
            "owner": reservation["owner"],
            "state": "running",
            "started_at": timestamp,
            "finished_at": None,
            "occurrence_id": None,
        }
        facts = facts_to_json(LifecycleStartFacts(
            target=AttentionTarget(reservation["target"]), claim=claim,
            admitted_at=timestamp, run_id=run_id, sandbox=sandbox,
        ))
        occurrence = _commit_occurrence(
            issue, reservation_id, operation_id, source=source, kind=AttentionKind.START,
            cause=None, facts=facts,
            accounting=Accounting(AccountingScope.NO_NEW_CLAIM, None, None, None),
            proof_ids=(), created_at=timestamp,
        )
        return occurrence


def record_attention(
    state_dir: Path, *, issue_id: int, reservation_id: str,
    source: AttentionSource | str, cause: AttentionCause | str, facts: object,
    claim: ClaimIdentity | None = None, proof_ids: tuple[str, ...] = (),
    deferred: bool = False, disposition: str = "stop", now: datetime | None = None,
    retry_after: str | None = None,
) -> dict[str, Any]:
    """Freeze one originating failure and its exact claim settlement atomically."""
    source_value = AttentionSource(source)
    cause_value = AttentionCause(cause)
    timestamp = (now or datetime.now(UTC)).isoformat()
    facts_json = facts_to_json(facts) if not isinstance(facts, dict) else dict(facts)
    with outcome_state_transaction(state_dir) as state:
        issue = state["issues"].get(str(issue_id))
        reservation = _reservation(state, issue_id, reservation_id)
        operation_id = _next_operation_id(reservation)
        _validate_positive_proof_evidence(
            source_value,
            facts_json,
            proof_ids,
            issue_id=issue_id,
            retained=tuple(reservation["positive_proofs"]),
        )
        if deferred:
            accounting = Accounting(AccountingScope.DEFERRED, claim, None, None)
        elif claim is None:
            accounting = Accounting(AccountingScope.NO_NEW_CLAIM, None, False, None)
        else:
            if (
                reservation["claim_state"] not in {"confirmed", "settled"}
                or reservation["binding"]["claim"] != claim.to_json()
            ):
                raise FailureStateError("unconfirmed claim cannot be settled")
            settlement = issue["settlements"].get(claim.key)
            if settlement is None:
                settlement = _settle(
                    issue, reservation_id, claim, proof_ids, operation_id, timestamp
                )
            else:
                # Settlement is immutable.  Later routing/recovery occurrences
                # reference the original accounting rather than upgrading it
                # from newly observed evidence.
                proof_ids = tuple(settlement["proof_ids"])
            accounting = Accounting(
                AccountingScope.BOUND_CLAIM, claim, settlement["consumed"], claim.key
            )
            reservation["claim_state"] = "settled"
        occurrence = _commit_occurrence(
            issue, reservation_id, operation_id, source=source_value,
            kind=AttentionKind.ATTENTION, cause=cause_value, facts=facts_json,
            accounting=accounting, proof_ids=proof_ids, created_at=timestamp,
        )
        if proof_ids:
            reservation["positive_proofs"] = list(dict.fromkeys([
                *reservation["positive_proofs"], *proof_ids
            ]))
        if not deferred:
            if disposition == "pending":
                if reservation["state"] != "prepared":
                    raise FailureStateError("only a prepared reservation can remain pending")
            else:
                reservation["state"] = (
                    "prepared" if disposition == "transient_retry" else "terminal"
                )
            reservation["disposition"] = disposition
            reservation["retry_after"] = retry_after
        return occurrence


def _validate_positive_proof_evidence(
    source: AttentionSource,
    facts: Mapping[str, object],
    proof_ids: tuple[str, ...],
    *,
    issue_id: int,
    retained: tuple[str, ...],
) -> None:
    if not proof_ids:
        return
    if SOURCE_RULES[source].work_capable and set(proof_ids).issubset(retained):
        return
    if facts.get("type") == "leaf":
        evidence_hash = facts.get("evidence_sha256")
        evidence_path = facts.get("evidence_id")
        expected = f"leaf_outcome:{evidence_hash}"
        if (
            proof_ids != (expected,)
            or not isinstance(evidence_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence_hash) is None
            or not isinstance(evidence_path, str)
            or not Path(evidence_path).is_absolute()
            or not SOURCE_RULES[source].work_capable
        ):
            raise FailureStateError("leaf work proof does not match terminal evidence")
        path = Path(evidence_path)
        try:
            value = path.lstat()
            if (
                stat.S_ISLNK(value.st_mode)
                or not stat.S_ISREG(value.st_mode)
                or value.st_size > 4 * 1024 * 1024
            ):
                raise FailureStateError("leaf work proof is not a bounded regular file")
            raw = path.read_bytes()
        except OSError as exc:
            raise FailureStateError("leaf work proof cannot be read") from exc
        if hashlib.sha256(raw).hexdigest() != evidence_hash:
            raise FailureStateError("leaf work proof hash mismatch")
        try:
            evidence = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_json_no_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FailureStateError("leaf work proof is malformed") from exc
        if not isinstance(evidence, dict):
            raise FailureStateError("leaf work proof must be an object")
        evidence_issue = evidence.get("issue", evidence.get("issue_id"))
        if evidence_issue != issue_id or evidence.get("status") not in {
            "completed", "blocked", "failed"
        }:
            raise FailureStateError("leaf work proof is not a matching terminal outcome")
        expected_status = facts.get("backend_status") or facts.get("compute_result")
        if expected_status not in {None, evidence.get("status")}:
            raise FailureStateError("leaf work proof status contradicts occurrence facts")
        if evidence.get("launch_error") is not None or evidence.get("status") == "launch_error":
            raise FailureStateError("launch failure is not positive work proof")
        return
    if facts.get("type") == "factory":
        from .attention import FactorySnapshot, positive_factory_proofs

        # Reconstruct only the fields used by the structured proof derivation.
        snapshot = FactorySnapshot(
            **{key: value for key, value in facts.items() if key != "type"}  # type: ignore[arg-type]
        )
        allowed = {*retained, *positive_factory_proofs(snapshot)}
        if not set(proof_ids).issubset(allowed):
            raise FailureStateError("factory work proof is not present in structured history")
        return
    raise FailureStateError("source facts cannot carry positive work proof")


def record_success_witness(
    state_dir: Path, *, issue_id: int, target: AttentionTarget | str, origin: str,
    claim: ClaimIdentity | None, run_id: str | None, sandbox: str | None,
    completed_at: str, evidence_path: str, evidence_sha256: str, branch: str,
    head_sha: str, pr_url: str | None, next_value: str | None = None,
    next_present: bool | None = None,
    reservation_id: str | None = None, proof_ids: tuple[str, ...] = (),
    observed_at: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Persist a verified completion; manual completions never produce prompts."""
    target_value = AttentionTarget(target)
    evidence_file = Path(evidence_path)
    try:
        value = evidence_file.lstat()
        if (
            stat.S_ISLNK(value.st_mode)
            or not stat.S_ISREG(value.st_mode)
            or value.st_size > 4 * 1024 * 1024
        ):
            raise FailureStateError("success evidence is not a regular file")
        evidence_bytes = evidence_file.read_bytes()
        observed_hash = hashlib.sha256(evidence_bytes).hexdigest()
    except OSError as exc:
        raise FailureStateError("success evidence cannot be read") from exc
    if observed_hash != evidence_sha256:
        raise FailureStateError("success evidence hash mismatch")
    try:
        evidence = json.loads(
            evidence_bytes.decode("utf-8", "strict"), object_pairs_hook=_json_no_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FailureStateError("success evidence is malformed") from exc
    if not isinstance(evidence, dict):
        raise FailureStateError("success evidence must be an object")
    evidence_issue = evidence.get("issue", evidence.get("issue_id"))
    if evidence_issue != issue_id or evidence.get("status") != "completed":
        raise FailureStateError("success evidence is not a matching completion")
    for field, expected in (("branch", branch), ("head_sha", head_sha)):
        observed = evidence.get(field)
        if observed is not None and observed != expected:
            raise FailureStateError(f"success evidence {field} mismatch")
    if evidence.get("pr_url") is not None and evidence.get("pr_url") != pr_url:
        raise FailureStateError("success evidence PR mismatch")
    if re.fullmatch(r"[0-9a-f]{40,64}", head_sha) is None:
        raise FailureStateError("success witness head is invalid")
    if not branch or len(branch.encode()) > 1024 or "\x00" in branch:
        raise FailureStateError("success witness branch is invalid")
    if pr_url is not None and not pr_url.startswith("https://"):
        raise FailureStateError("success witness PR URL is invalid")
    witness = {
        "issue_id": issue_id, "target": target_value.value, "origin": origin,
        "claim": claim.to_json() if claim else None, "run_id": run_id, "sandbox": sandbox,
        "completed_at": completed_at, "observed_at": observed_at or datetime.now(UTC).isoformat(),
        "evidence_path": evidence_path, "evidence_sha256": evidence_sha256,
        "branch": branch, "head_sha": head_sha, "pr_url": pr_url, "next": next_value,
        "next_present": next_value is not None if next_present is None else next_present,
    }
    witness_id = success_witness_identity(witness)
    with outcome_state_transaction(state_dir) as state:
        issue = state["issues"].setdefault(str(issue_id), _empty_issue())
        existing = issue["success_witnesses"].get(witness_id)
        if existing is not None and existing != witness:
            raise FailureStateError("success witness changed")
        issue["success_witnesses"][witness_id] = witness
        if origin == "manual":
            return witness_id, None
        if origin != "autonomous" or reservation_id is None or claim is None:
            raise FailureStateError("autonomous success requires reservation and claim")
        reservation = _reservation(state, issue_id, reservation_id)
        if (
            reservation["claim_state"] != "confirmed"
            or reservation["binding"]["claim"] != claim.to_json()
        ):
            raise FailureStateError("success requires the confirmed original claim")
        if reservation["target"] != target_value.value:
            raise FailureStateError("success target does not match reservation")
        if reservation["binding"]["run_id"] != run_id:
            raise FailureStateError("success run binding mismatch")
        if reservation["binding"]["sandbox"] != sandbox:
            raise FailureStateError("success sandbox binding mismatch")
        if reservation["state"] == "terminal" and reservation["disposition"] == "success":
            lifecycle = reservation["lifecycle_operation_id"]
            terminal_id = occurrence_identity(reservation_id, lifecycle, "terminal")
            return witness_id, issue["occurrences"].get(terminal_id)
        operation_id = reservation["lifecycle_operation_id"]
        if operation_id is None:
            raise FailureStateError("success cannot precede START")
        settlement = _settle(
            issue, reservation_id, claim, proof_ids or (f"completion:{witness_id}",),
            operation_id, witness["observed_at"],
        )
        from .attention import LifecycleSuccessFacts
        facts = facts_to_json(LifecycleSuccessFacts(
            target=target_value, witness_id=witness_id, completed_at=completed_at,
            evidence_path=evidence_path, evidence_sha256=evidence_sha256, branch=branch,
            head_sha=head_sha, pr_url=pr_url, run_id=run_id, sandbox=sandbox,
            next=next_value,
            next_present=witness["next_present"],
        ))
        occurrence = _commit_occurrence(
            issue, reservation_id, operation_id,
            source=AttentionSource.LEAF_SUCCESS if target_value == AttentionTarget.LEAF else AttentionSource.FACTORY_SUCCESS,
            kind=AttentionKind.SUCCESS, cause=None, facts=facts,
            accounting=Accounting(AccountingScope.BOUND_CLAIM, claim, True, claim.key),
            proof_ids=tuple(settlement["proof_ids"]), created_at=witness["observed_at"],
        )
        reservation["state"] = "terminal"
        reservation["claim_state"] = "settled"
        reservation["disposition"] = "success"
        reservation["positive_proofs"] = list(settlement["proof_ids"])
        return witness_id, occurrence


def settlement_for_claim(state_dir: Path, claim: ClaimIdentity) -> Mapping[str, object] | None:
    state = load_outcome_state(state_dir)
    if state.get("version") != SCHEMA_VERSION:
        return None
    issue = state["issues"].get(str(claim.issue_id))
    return issue["settlements"].get(claim.key) if issue else None


def reservation_claim(
    state_dir: Path, *, issue_id: int, reservation_id: str
) -> ClaimIdentity | None:
    state = load_outcome_state(state_dir)
    if state.get("version") != SCHEMA_VERSION:
        return None
    issue = state["issues"].get(str(issue_id))
    reservation = issue["reservations"].get(reservation_id) if issue else None
    raw = reservation["binding"]["claim"] if reservation else None
    return ClaimIdentity.from_json(raw) if raw is not None else None


def reservation_claim_state(
    state_dir: Path, *, issue_id: int, reservation_id: str
) -> tuple[str, ClaimIdentity | None]:
    state = load_outcome_state(state_dir)
    reservation = _reservation(state, issue_id, reservation_id)
    raw = reservation["binding"]["claim"]
    claim = ClaimIdentity.from_json(raw) if raw is not None else None
    return str(reservation["claim_state"]), claim


def reservation_binding(
    state_dir: Path, *, issue_id: int, reservation_id: str
) -> Mapping[str, object] | None:
    state = load_outcome_state(state_dir)
    issue = state["issues"].get(str(issue_id))
    reservation = issue["reservations"].get(reservation_id) if issue else None
    return dict(reservation["binding"]) if reservation else None


def retain_positive_proofs(
    state_dir: Path,
    *,
    issue_id: int,
    reservation_id: str,
    proof_ids: tuple[str, ...],
) -> None:
    """Monotonically retain structured proofs before projections are replaced."""
    if not all(
        proof.startswith(("factory_step:", "factory_merged_slice:", "factory_partial"))
        for proof in proof_ids
    ):
        raise FailureStateError("factory proof kind is invalid")
    with outcome_state_transaction(state_dir) as state:
        reservation = _reservation(state, issue_id, reservation_id)
        reservation["positive_proofs"] = list(
            dict.fromkeys([*reservation["positive_proofs"], *proof_ids])
        )


def retained_positive_proofs(
    state_dir: Path, *, issue_id: int, reservation_id: str
) -> tuple[str, ...]:
    state = load_outcome_state(state_dir)
    reservation = _reservation(state, issue_id, reservation_id)
    return tuple(reservation["positive_proofs"])


def reservation_has_source(
    state_dir: Path,
    *,
    issue_id: int,
    reservation_id: str,
    sources: frozenset[str],
) -> bool:
    state = load_outcome_state(state_dir)
    reservation = _reservation(state, issue_id, reservation_id)
    return any(
        operation["source"] in sources
        for operation in reservation["operations"].values()
    )


def nonconsuming_claim_keys(state_dir: Path, issue_id: int) -> set[str]:
    state = load_outcome_state(state_dir)
    if state.get("version") != SCHEMA_VERSION:
        return set()
    issue = state["issues"].get(str(issue_id))
    if not issue:
        return set()
    return {key for key, value in issue["settlements"].items() if value["consumed"] is False}


def issue_dispatch_disposition(state_dir: Path, issue_id: int) -> str | None:
    """Return the effective terminal disposition for every admission guard.

    A later prepared reservation cannot hide an earlier stop.  Only a verified
    success terminal that is causally newer clears prior stops.
    """
    state = load_outcome_state(state_dir)
    if state.get("version") != SCHEMA_VERSION:
        return None
    issue = state["issues"].get(str(issue_id))
    if not issue or not issue["reservations"]:
        return None
    terminal = sorted(
        (
            reservation
            for reservation in issue["reservations"].values()
            if reservation["state"] == "terminal"
        ),
        key=lambda item: item["sequence"],
    )
    if not terminal:
        return None
    latest_success = max(
        (item["sequence"] for item in terminal if item["disposition"] == "success"),
        default=0,
    )
    if any(
        item["disposition"] == "stop" and item["sequence"] > latest_success
        for item in terminal
    ):
        return "stop"
    return terminal[-1]["disposition"]


def active_reservation_id(
    state_dir: Path, *, issue_id: int, target: AttentionTarget | str
) -> str | None:
    """Return the newest exact autonomous recovery binding, never invent one."""
    state = load_outcome_state(state_dir)
    if state.get("version") != SCHEMA_VERSION:
        return None
    issue = state["issues"].get(str(issue_id))
    if not issue:
        return None
    candidates = [
        (value["sequence"], key)
        for key, value in issue["reservations"].items()
        if value["target"] == AttentionTarget(target).value
        and value["autonomous"] is True
        and value["state"] in {"prepared", "active"}
    ]
    return max(candidates)[1] if candidates else None


def recovery_reservation_id(
    state_dir: Path,
    *,
    issue_id: int,
    target: AttentionTarget | str,
    run_id: str | None,
    sandbox: str | None,
    claim_attempt: int,
) -> str | None:
    binding = recovery_reservation_binding(
        state_dir,
        issue_id=issue_id,
        target=target,
        run_id=run_id,
        sandbox=sandbox,
        claim_attempt=claim_attempt,
    )
    return binding[0] if binding is not None else None


def recovery_reservation_binding(
    state_dir: Path,
    *,
    issue_id: int,
    target: AttentionTarget | str,
    run_id: str | None,
    sandbox: str | None,
    claim_attempt: int,
) -> tuple[str, ClaimIdentity] | None:
    """Resolve startup recovery to one reservation and its complete original claim."""
    state = load_outcome_state(state_dir)
    issue = state["issues"].get(str(issue_id))
    if not issue:
        return None
    candidates: list[tuple[int, str, Mapping[str, object]]] = []
    for reservation_id, reservation in issue["reservations"].items():
        binding = reservation["binding"]
        claim = binding["claim"]
        if (
            reservation["target"] != AttentionTarget(target).value
            or reservation["autonomous"] is not True
            or reservation["state"] not in {"prepared", "active"}
            or binding["run_id"] != run_id
            or binding["sandbox"] != sandbox
        ):
            continue
        if not isinstance(claim, dict):
            raise FailureStateError("recovery reservation lost its original claim")
        candidates.append((reservation["sequence"], reservation_id, claim))
    if len(candidates) > 1:
        raise FailureStateError("recovery binding is ambiguous")
    if not candidates:
        return None
    _sequence, reservation_id, raw_claim = candidates[0]
    claim = ClaimIdentity.from_json(raw_claim)
    if claim.issue_id != issue_id or claim.attempt != claim_attempt:
        raise FailureStateError("recovery record does not match the original claim identity")
    return reservation_id, claim


def has_nonterminal_reservation(
    state_dir: Path, *, issue_id: int, target: AttentionTarget | str
) -> bool:
    state = load_outcome_state(state_dir)
    issue = state["issues"].get(str(issue_id))
    if issue is None:
        return False
    target_value = AttentionTarget(target).value
    return any(
        reservation["target"] == target_value
        and reservation["autonomous"] is True
        and reservation["state"] in {"prepared", "active"}
        for reservation in issue["reservations"].values()
    )


def has_reservation_history(
    state_dir: Path, *, issue_id: int, target: AttentionTarget | str
) -> bool:
    """Return whether this issue/target has any v2 identity, including terminal work."""
    state = load_outcome_state(state_dir)
    issue = state["issues"].get(str(issue_id))
    if issue is None:
        return False
    target_value = AttentionTarget(target).value
    return any(
        reservation["target"] == target_value
        for reservation in issue["reservations"].values()
    )


def issue_retry_after(state_dir: Path, issue_id: int) -> datetime | None:
    state = load_outcome_state(state_dir)
    issue = state["issues"].get(str(issue_id))
    if not issue:
        return None
    values = [
        parse_time(item["retry_after"])
        for item in issue["reservations"].values()
        if item["state"] == "prepared"
        and item["disposition"] == "transient_retry"
        and item["retry_after"] is not None
    ]
    parsed = [item for item in values if item is not None]
    return max(parsed) if parsed else None


def authorized_retry_reservation_id(
    state_dir: Path,
    *,
    issue_id: int,
    target: AttentionTarget | str,
    now: datetime | None = None,
) -> str | None:
    """Return only the due prepared reservation authorized by contention policy."""
    state = load_outcome_state(state_dir)
    issue = state["issues"].get(str(issue_id))
    if not issue:
        return None
    target_value = AttentionTarget(target).value
    candidates = sorted(
        (
            (reservation["sequence"], reservation_id, reservation)
            for reservation_id, reservation in issue["reservations"].items()
            if reservation["target"] == target_value
            and reservation["autonomous"] is True
            and reservation["state"] == "prepared"
            and reservation["disposition"] == "transient_retry"
        ),
        reverse=True,
    )
    if not candidates:
        return None
    _sequence, reservation_id, reservation = candidates[0]
    deadline = parse_time(reservation["retry_after"])
    if deadline is None:
        raise FailureStateError("prepared retry has no valid deadline")
    instant = now or datetime.now(UTC)
    if instant < deadline:
        raise FailureStateError(f"transient retry is not due before {deadline.isoformat()}")
    owner = reservation["owner"]
    if owner is not None and owner.get("pid") != os.getpid() and not _owner_verified_dead(owner):
        raise FailureStateError("prepared retry owner is live or unknown")
    return reservation_id


def validate_claim_retry_authorization(
    state_dir: Path,
    *,
    issue_id: int,
    reservation_id: str,
    target: AttentionTarget | str | None = None,
    now: datetime | None = None,
) -> None:
    """Prevent fresh reservations from bypassing a retained retry identity/deadline."""
    state = load_outcome_state(state_dir)
    issue = state["issues"].get(str(issue_id))
    if not issue:
        return
    retained = [
        (candidate_id, reservation)
        for candidate_id, reservation in issue["reservations"].items()
        if (target is None or reservation["target"] == AttentionTarget(target).value)
        and reservation["state"] == "prepared"
        and reservation["disposition"] == "transient_retry"
    ]
    if not retained:
        return
    candidate_id, reservation = max(retained, key=lambda item: item[1]["sequence"])
    if candidate_id != reservation_id:
        raise FailureStateError("transient retry must reuse its prepared reservation")
    deadline = parse_time(reservation["retry_after"])
    if deadline is None or (now or datetime.now(UTC)) < deadline:
        raise FailureStateError("transient retry deadline has not elapsed")


def pending_attention(state_dir: Path) -> list[dict[str, Any]]:
    state = load_outcome_state(state_dir)
    if state.get("version") != SCHEMA_VERSION:
        return []
    pending: list[tuple[int, int, dict[str, Any]]] = []
    for issue_key, issue in state["issues"].items():
        sequences = {key: value["sequence"] for key, value in issue["reservations"].items()}
        for occurrence in issue["occurrences"].values():
            if occurrence["handling"] is None:
                pending.append((int(issue_key), sequences[occurrence["reservation_id"]], occurrence))
    pending.sort(key=lambda item: (item[0], item[1], item[2]["created_at"]))
    return [item[2] for item in pending]


def _reservation(state: Mapping[str, Any], issue_id: int, reservation_id: str) -> dict[str, Any]:
    issue = state["issues"].get(str(issue_id))
    if issue is None or reservation_id not in issue["reservations"]:
        raise FailureStateError("reservation does not exist for issue")
    return issue["reservations"][reservation_id]


def _next_operation_id(reservation: Mapping[str, Any]) -> str:
    return f"op-{len(reservation['operations']) + 1}"


def _settle(
    issue: dict[str, Any], reservation_id: str, claim: ClaimIdentity,
    proof_ids: tuple[str, ...], operation_id: str, settled_at: str,
) -> dict[str, Any]:
    reservation = issue["reservations"].get(reservation_id)
    if reservation is None or (
        reservation["claim_state"] != "confirmed"
        or reservation["binding"]["claim"] != claim.to_json()
    ):
        raise FailureStateError("only a confirmed exact claim can be settled")
    proofs = list(dict.fromkeys(proof_ids))
    settlement = {
        "claim": claim.to_json(), "reservation_id": reservation_id,
        "consumed": bool(proofs),
        "basis": SettlementBasis.POSITIVE_WORK.value if proofs else SettlementBasis.NO_WORK.value,
        "proof_ids": proofs, "terminal_operation_id": operation_id, "settled_at": settled_at,
    }
    existing = issue["settlements"].get(claim.key)
    if existing is not None and existing != settlement:
        raise FailureStateError("claim already has a conflicting settlement")
    issue["settlements"][claim.key] = settlement
    return settlement


def _commit_occurrence(
    issue: dict[str, Any], reservation_id: str, operation_id: str, *,
    source: AttentionSource, kind: AttentionKind, cause: AttentionCause | None,
    facts: Mapping[str, object], accounting: Accounting, proof_ids: tuple[str, ...],
    created_at: str,
) -> dict[str, Any]:
    reservation = issue["reservations"][reservation_id]
    slot = _kind_slot(kind.value)
    occurrence_id = occurrence_identity(reservation_id, operation_id, slot)
    occurrence = {
        "reservation_id": reservation_id, "operation_id": operation_id, "kind": kind.value,
        "source": source.value, "cause": cause.value if cause else None,
        "created_at": created_at, "facts": dict(facts), "proof_ids": list(proof_ids),
        "accounting": accounting.to_json(),
        "delivery_key": f"worklink-attention:{reservation['issue_id']}:{occurrence_id}",
        "execution": None, "handling": None,
    }
    validate_occurrence_contract(
        kind=kind, source=source, cause=cause, facts=facts,
        accounting=accounting, proof_ids=proof_ids,
    )
    existing = issue["occurrences"].get(occurrence_id)
    if existing is not None:
        if existing != occurrence:
            raise FailureStateError("immutable occurrence changed")
        return existing
    operation = reservation["operations"].get(operation_id)
    operation_value = {
        "source": source.value, "owner": reservation["owner"], "state": "finished",
        "started_at": created_at, "finished_at": created_at, "occurrence_id": occurrence_id,
    }
    if operation is not None and operation != operation_value:
        # START and success deliberately share the lifecycle operation.  Preserve
        # the operation's START identity while allowing its terminal slot.
        if not (
            kind in {AttentionKind.START, AttentionKind.SUCCESS}
            and operation["source"] in {"leaf_start", "factory_start"}
            and operation["state"] in {"running", "finished"}
        ):
            raise FailureStateError("operation was already committed")
        if kind == AttentionKind.START:
            reservation["operations"][operation_id] = operation_value
    else:
        reservation["operations"][operation_id] = operation_value
    issue["occurrences"][occurrence_id] = occurrence
    return occurrence


def occurrence_identity(reservation_id: str, operation_id: str, slot: str) -> str:
    return hashlib.sha256(f"{reservation_id}\0{operation_id}\0{slot}".encode()).hexdigest()


def success_witness_identity(witness: Mapping[str, object]) -> str:
    claim = witness.get("claim")
    identity = json.dumps(claim, sort_keys=True, separators=(",", ":")) if claim else str(witness.get("run_id"))
    material = "\0".join((
        str(witness.get("issue_id")), str(witness.get("target")), identity,
        str(witness.get("evidence_sha256")),
    ))
    return hashlib.sha256(material.encode()).hexdigest()


def _kind_slot(kind: object) -> str:
    if kind == "start":
        return "start"
    if kind in {"success", "legacy_attention"}:
        return "terminal"
    if kind == "attention":
        return "terminal"
    raise FailureStateError("occurrence kind is invalid")


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def is_transient_contention(error: str) -> bool:
    normalized = error.casefold()
    return any(all(marker in normalized for marker in markers) for markers in _TRANSIENT_CONTENTION_MARKERS)


def record_contention_recurrence(
    state_dir: Path,
    *,
    issue_id: int,
    signature: str,
    verified_work_success: bool = False,
    now: datetime | None = None,
) -> tuple[str, str | None]:
    """Apply the only automatic work-retry policy: bounded exact contention."""
    if not signature or len(signature.encode()) > 256:
        raise ValueError("contention signature is invalid")
    timestamp = now or datetime.now(UTC)
    with outcome_state_transaction(state_dir) as state:
        issue = state["issues"].setdefault(str(issue_id), _empty_issue())
        if verified_work_success:
            issue["contention"] = {}
            return "success", None
        row = issue["contention"].get(signature)
        count = (row["count"] if isinstance(row, dict) and type(row.get("count")) is int else 0) + 1
        if count <= MAX_TRANSIENT_RECURRENCES:
            retry_after = (
                timestamp + timedelta(seconds=TRANSIENT_RETRY_SECONDS[count - 1])
            ).isoformat()
            disposition = "transient_retry"
        else:
            retry_after = None
            disposition = "stop"
        issue["contention"][signature] = {
            "count": count,
            "last_at": timestamp.isoformat(),
            "retry_after": retry_after,
            "disposition": disposition,
        }
        return disposition, retry_after


def clear_contention_after_verified_success(state_dir: Path, issue_id: int) -> None:
    with outcome_state_transaction(state_dir) as state:
        issue = state["issues"].get(str(issue_id))
        if issue is not None:
            issue["contention"] = {}


# Version-1 telemetry compatibility.  These functions remain for the ready queue
# during the serial delivery rollout.  They operate on ``legacy`` when the ledger
# has already migrated and never suppress a v2 attention occurrence.
def record_failure(
    state_dir: Path, *, issue_id: int, attempt: int | None, exit_status: int,
    error: BaseException | str, log_path: str | None, preserved_ref: str | None = None,
    preservation_error: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    full_error = redact_text(str(error))[:4000]
    safe_error = terminal_error(error)
    signature = error_signature(safe_error)
    if attempt is None and is_transient_contention(full_error):
        return {
            "active": False, "issue_id": issue_id, "attempt": None,
            "attempt_consumed": False, "exit_status": exit_status,
            "terminal_error": safe_error, "signature": signature,
            "transient_contention": True, "failed_at": now.isoformat(),
            "retry_after": None, "log_path": redact_text(log_path or ""),
            "preserved_ref": redact_text(preserved_ref or "")[:1000] or None,
            "preservation_error": redact_text(preservation_error or "")[:1000] or None,
            "notified_signatures": [],
        }
    with failure_state_transaction(state_dir) as state:
        key = str(issue_id)
        if state.get("version") == 1:
            prior = state["issues"].get(key)
        else:
            issue = state["issues"].setdefault(key, _empty_issue())
            prior = issue.get("legacy", {}).get("row") if issue.get("legacy") else None
        prior = prior if isinstance(prior, dict) else {}
        consecutive = int(prior.get("consecutive", 0)) + 1 if prior.get("signature") == signature and prior.get("active") is True else 1
        delay = min(INITIAL_BACKOFF_MINUTES * (2 ** min(consecutive - 1, 8)), MAX_BACKOFF_MINUTES)
        entry = {
            "active": True, "issue_id": issue_id, "attempt": attempt,
            "attempt_consumed": attempt is not None, "exit_status": exit_status,
            "terminal_error": safe_error, "signature": signature,
            "occurrence_id": uuid.uuid4().hex, "consecutive": consecutive,
            "failed_at": now.isoformat(), "retry_after": (now + timedelta(minutes=delay)).isoformat(),
            "log_path": redact_text(log_path or ""),
            "preserved_ref": redact_text(preserved_ref or "")[:1000] or None,
            "preservation_error": redact_text(preservation_error or "")[:1000] or None,
            "notified_signatures": list(prior.get("notified_signatures") or [])[-MAX_NOTIFIED_SIGNATURES:],
        }
        if state.get("version") == 1:
            state["issues"][key] = entry
        else:
            state["issues"][key]["legacy"] = {"key": key, "row": entry}
    return entry


def _legacy_rows(state: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    for issue in state["issues"].values():
        if state.get("version") == 1:
            row = issue
        else:
            row = issue.get("legacy", {}).get("row") if issue.get("legacy") else None
        if isinstance(row, dict):
            yield row


def pending_failure_alerts(
    state_dir: Path, *, now: datetime | None = None,
) -> tuple[set[int], list[dict[str, object]]]:
    now = now or datetime.now(UTC)
    backed_off: set[int] = set()
    alerts: list[dict[str, object]] = []
    with failure_state_transaction(state_dir) as state:
        for entry in _legacy_rows(state):
            if entry.get("active") is not True:
                continue
            issue_id = entry.get("issue_id")
            if type(issue_id) is not int:
                continue
            # A terminal failure never becomes dispatchable merely because a
            # legacy telemetry backoff timestamp elapsed.
            backed_off.add(issue_id)
            signature = str(entry.get("signature") or "")
            notified = entry.get("notified_signatures")
            notified = list(notified) if isinstance(notified, list) else []
            if signature and signature not in notified:
                alerts.append({
                    "signal": "worklink_run_failure_escalated",
                    "source_id": f"worklink-run-failure:{issue_id}:{signature}",
                    "issue_id": issue_id, "attempt": entry.get("attempt"),
                    "attempt_consumed": entry.get("attempt_consumed"),
                    "exit_status": entry.get("exit_status"), "terminal_error": entry.get("terminal_error"),
                    "error_signature": signature, "failure_occurrence_id": entry.get("occurrence_id"),
                    "log": entry.get("log_path"), "preserved_ref": entry.get("preserved_ref"),
                    "preservation_error": entry.get("preservation_error"), "retry_after": entry.get("retry_after"),
                    "routing_instructions": "Notify the operator that a detached Worklink run failed. Include the run-log path, terminal error, and any preserved ref or preservation error.",
                })
    return backed_off, alerts


def mark_failure_notified(
    state_dir: Path, issue_id: int, signature: str, occurrence_id: str | None,
) -> None:
    with failure_state_transaction(state_dir) as state:
        issue = state["issues"].get(str(issue_id))
        if state.get("version") == 1:
            entry = issue
        else:
            entry = issue.get("legacy", {}).get("row") if issue and issue.get("legacy") else None
        if not isinstance(entry, dict) or entry.get("active") is not True or entry.get("signature") != signature or entry.get("occurrence_id") != occurrence_id:
            return
        notified = list(entry.get("notified_signatures") or [])
        if signature not in notified:
            notified.append(signature)
        entry["notified_signatures"] = notified[-MAX_NOTIFIED_SIGNATURES:]


def record_success(state_dir: Path, issue_id: int) -> None:
    with failure_state_transaction(state_dir) as state:
        issue = state["issues"].get(str(issue_id))
        if state.get("version") == 1:
            entry = issue
        else:
            entry = issue.get("legacy", {}).get("row") if issue and issue.get("legacy") else None
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


def _parse_required_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 128:
        return None
    parsed = parse_time(value)
    if parsed is None:
        return None
    try:
        original = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if original.tzinfo is not None else None
