"""Durable Worklink dispatch reservations and attention occurrences."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from .._atomic import atomic_write_json
from ..redaction import redact_text
from .attention import AttentionRecord, AttentionSource, HandlingDisposition, RecoveryRetirement

STATE_FILE = "dispatch_failures.json"
POLLER_NAME = "worklink-ready-queue"
ATTENTION_POLLER_NAME = "worklink-attention"
STATE_VERSION = 2
INITIAL_BACKOFF_MINUTES = 15
MAX_BACKOFF_MINUTES = 240
MAX_NOTIFIED_SIGNATURES = 32
_DELIVERY_RECEIPTS_DIR = ".delivery-receipts"
_TRANSIENT_CONTENTION_MARKERS = (
    ("unable to create", "index.lock"),
    ("cannot lock ref",),
    ("could not write new index file",),
)


class FailureStateError(RuntimeError):
    pass


def dispatch_failure_state_dir(home: Path) -> Path:
    return home / "state" / "pollers" / POLLER_NAME


def terminal_error(value: BaseException | str) -> str:
    text = f"{type(value).__name__}: {value}" if isinstance(value, BaseException) else value
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return redact_text(lines[-1] if lines else "Worklink run failed")[:1000]


def error_signature(error: str) -> str:
    return hashlib.sha256(error.encode("utf-8")).hexdigest()[:16]


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "revision": 0, "issues": {}}


def _legacy_occurrence(issue: str, entry: Mapping[str, Any]) -> dict[str, Any] | None:
    if not entry.get("occurrence_id"):
        return None
    occurrence = str(entry["occurrence_id"])
    signature = str(entry.get("signature") or "legacy")
    return {
        "schema_version": 2,
        "kind": "attention",
        "cause": "legacy_unknown",
        "source": "legacy_v1",
        "issue_id": int(entry.get("issue_id") or issue),
        "run_id": None,
        "execution_id": occurrence,
        "launch_id": None,
        "claim": None,
        "error_signature": signature,
        "occurrence_id": occurrence,
        "delivery_key": f"worklink-attention:{issue}:{signature}:{occurrence}",
        "attempt": entry.get("attempt"),
        "outcome": "legacy_unknown",
        "attempt_consumed": entry.get("attempt_consumed"),
        "accounting_basis": "legacy_unknown",
        "settlement": "not_needed",
        "evidence_quality": "missing",
        "reason": entry.get("terminal_error") or "legacy dispatch failure",
        "refs": {"log": entry.get("log_path"), "preserved_ref": entry.get("preserved_ref")},
        "autonomous": False,
        "inhibited": entry.get("active") is True,
        "created_at": entry.get("failed_at") or datetime.now(UTC).isoformat(),
        "delivered_at": None,
        "handled_at": None,
        "handling_disposition": None,
        "retirement": None,
        "legacy_notified": signature in (entry.get("notified_signatures") or ()),
    }


def _normalize_issue(key: str, raw: Any, *, legacy: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FailureStateError(f"dispatch failure issue {key} is invalid")
    if not key.isascii() or not key.isdecimal() or int(key) <= 0:
        raise FailureStateError(f"dispatch failure issue {key} identity is invalid")
    entry = dict(raw)
    if legacy:
        entry.setdefault("issue_id", int(key))
    reservations = entry.get("reservations", {})
    occurrences = entry.get("occurrences", {})
    if legacy:
        migrated = _legacy_occurrence(key, entry)
        occurrences = {migrated["occurrence_id"]: migrated} if migrated else {}
    if not isinstance(reservations, dict) or not isinstance(occurrences, dict):
        raise FailureStateError(f"dispatch failure issue {key} collections are invalid")
    entry["reservations"] = reservations
    entry["occurrences"] = occurrences
    entry.setdefault("arming_generation", 0)
    entry.setdefault("ready_cycle_generation", 0)
    entry.setdefault("inhibited", any(isinstance(item, dict) and item.get("inhibited") is True and not item.get("handled_at") for item in occurrences.values()))
    if type(entry.get("issue_id")) is not int or entry["issue_id"] != int(key):
        raise FailureStateError(f"dispatch failure issue {key} identity is invalid")
    if (
        type(entry.get("arming_generation")) is not int
        or entry["arming_generation"] < 0
        or type(entry.get("ready_cycle_generation")) is not int
        or entry["ready_cycle_generation"] < 0
        or type(entry.get("inhibited")) is not bool
    ):
        raise FailureStateError(f"dispatch failure issue {key} state is invalid")
    if entry.get("reset_generation") is not None and (
        type(entry["reset_generation"]) is not int or entry["reset_generation"] < 0
    ):
        raise FailureStateError(f"dispatch failure issue {key} reset witness is invalid")
    if entry.get("manual_claim_witness") is not None and not _valid_claim(
        entry["manual_claim_witness"], int(key)
    ):
        raise FailureStateError(f"dispatch failure issue {key} manual witness is invalid")
    for reservation_id, reservation in reservations.items():
        _validate_reservation(str(reservation_id), reservation)
    for occurrence_id, occurrence in occurrences.items():
        _validate_occurrence(key, str(occurrence_id), occurrence, legacy=occurrence.get("source") == "legacy_v1" if isinstance(occurrence, dict) else False)
    for reservation_id, reservation in reservations.items():
        if reservation.get("closure") != "promoted":
            continue
        occurrence = occurrences.get(reservation["promoted_occurrence_id"])
        if not isinstance(occurrence, dict) or (
            occurrence.get("issue_id") != reservation["issue_id"]
            or occurrence.get("execution_id") != reservation["execution_id"]
            or occurrence.get("source") != reservation["source"]
            or reservation.get("run_id") is not None
            and occurrence.get("run_id") != reservation["run_id"]
            or reservation.get("launch_id") is not None
            and occurrence.get("launch_id") != reservation["launch_id"]
        ):
            raise FailureStateError(
                f"execution reservation {reservation_id} promotion witness is invalid"
            )
    return entry


def _normalize_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("issues"), dict):
        raise FailureStateError("dispatch failure state has invalid shape")
    version = payload.get("version")
    if version not in {1, STATE_VERSION}:
        raise FailureStateError("unsupported dispatch failure state version")
    result = dict(payload)
    result["version"] = STATE_VERSION
    revision = payload.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise FailureStateError("dispatch failure state revision is invalid")
    result["revision"] = revision
    result["issues"] = {str(key): _normalize_issue(str(key), value, legacy=version == 1) for key, value in payload["issues"].items()}
    return result


def load_failure_state(state_dir: Path) -> dict[str, Any]:
    path = state_dir / STATE_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_state()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FailureStateError("dispatch failure state is unreadable") from exc
    return _normalize_state(payload)


def save_failure_state(state_dir: Path, state: dict[str, Any]) -> None:
    normalized = _normalize_state(state)
    normalized["revision"] = normalized["revision"] + 1
    atomic_write_json(state_dir / STATE_FILE, normalized, mode=0o600)
    try:
        directory_fd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        if load_failure_state(state_dir) != normalized:
            raise
        directory_fd = None
    try:
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                if load_failure_state(state_dir) != normalized:
                    raise
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    state.clear()
    state.update(normalized)


def delivery_receipt_exists(state_dir: Path, delivery_key: str, *, poller_name: str = POLLER_NAME) -> bool:
    digest = hashlib.sha256(delivery_key.encode()).hexdigest()
    directory = state_dir if poller_name == POLLER_NAME else state_dir.parent / poller_name
    return (directory / _DELIVERY_RECEIPTS_DIR / digest).is_file()


@contextmanager
def failure_state_transaction(state_dir: Path) -> Iterator[dict[str, Any]]:
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / f"{STATE_FILE}.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
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


def _issue(state: dict[str, Any], issue_id: int) -> dict[str, Any]:
    key = str(issue_id)
    raw = state["issues"].get(key)
    if raw is None:
        raw = {"issue_id": issue_id, "reservations": {}, "occurrences": {}, "arming_generation": 0, "inhibited": False}
        state["issues"][key] = raw
    if not isinstance(raw, dict):
        raise FailureStateError("dispatch failure issue is invalid")
    raw.setdefault("reservations", {})
    raw.setdefault("occurrences", {})
    return raw


def _validate_reservation(reservation_id: str, value: Any) -> None:
    if not isinstance(value, dict) or value.get("reservation_id") != reservation_id:
        raise FailureStateError(f"execution reservation {reservation_id} is invalid")
    required = {
        "reservation_id", "execution_id", "issue_id", "run_id", "launch_id",
        "autonomous", "invocation_id", "source", "operation_stage",
        "prepared_claim", "confirmed_claim", "claim_binding_state", "observations",
        "state", "closure", "promoted_occurrence_id", "created_at", "updated_at",
        "closed_at",
    }
    if not required.issubset(value):
        raise FailureStateError(f"execution reservation {reservation_id} fields are incomplete")
    state, closure, occurrence = value.get("state"), value.get("closure"), value.get("promoted_occurrence_id")
    valid = (state, closure, occurrence is None) in {("reserved", None, True), ("closed", "excluded", True)} or (state == "closed" and closure == "promoted" and isinstance(occurrence, str) and bool(occurrence))
    if not valid:
        raise FailureStateError(f"execution reservation {reservation_id} invariant failed")
    if type(value.get("issue_id")) is not int or value["issue_id"] <= 0:
        raise FailureStateError(f"execution reservation {reservation_id} issue is invalid")
    if not isinstance(value.get("execution_id"), str) or not value["execution_id"]:
        raise FailureStateError(f"execution reservation {reservation_id} execution is invalid")
    if value.get("autonomous") is not True:
        raise FailureStateError(f"execution reservation {reservation_id} provenance is invalid")
    if not isinstance(value.get("operation_stage"), str) or not value["operation_stage"].strip():
        raise FailureStateError(f"execution reservation {reservation_id} stage is invalid")
    for name in ("created_at", "updated_at"):
        if parse_time(value.get(name)) is None:
            raise FailureStateError(f"execution reservation {reservation_id} timestamp is invalid")
    if value["state"] == "closed" and parse_time(value.get("closed_at")) is None:
        raise FailureStateError(f"execution reservation {reservation_id} closure time is invalid")
    if value["state"] == "reserved" and value.get("closed_at") is not None:
        raise FailureStateError(f"execution reservation {reservation_id} closure time is invalid")
    if value["closure"] == "excluded":
        if not isinstance(value.get("exclusion_witness"), str) or not value["exclusion_witness"].strip():
            raise FailureStateError(f"execution reservation {reservation_id} exclusion witness is invalid")
    elif value.get("exclusion_witness") is not None:
        raise FailureStateError(f"execution reservation {reservation_id} exclusion witness is invalid")
    for name in ("run_id", "launch_id", "invocation_id"):
        if value.get(name) is not None and (
            not isinstance(value[name], str) or not value[name]
        ):
            raise FailureStateError(f"execution reservation {reservation_id} binding is invalid")
    if not isinstance(value.get("observations"), dict):
        raise FailureStateError(f"execution reservation {reservation_id} observations are invalid")
    try:
        AttentionSource(str(value.get("source")))
    except ValueError as exc:
        raise FailureStateError(f"execution reservation {reservation_id} source is invalid") from exc
    if value.get("claim_binding_state") not in {"none", "prepared", "confirmed"}:
        raise FailureStateError(f"execution reservation {reservation_id} claim binding is invalid")
    for name in ("prepared_claim", "confirmed_claim"):
        claim = value.get(name)
        if claim is not None and not _valid_claim(claim, value["issue_id"]):
            raise FailureStateError(f"execution reservation {reservation_id} {name} is invalid")
    if value.get("claim_binding_state") == "prepared" and value.get("prepared_claim") is None:
        raise FailureStateError(f"execution reservation {reservation_id} prepared claim is missing")
    if value.get("claim_binding_state") == "confirmed" and value.get("confirmed_claim") is None:
        raise FailureStateError(f"execution reservation {reservation_id} confirmed claim is missing")


def _valid_claim(value: Any, issue_id: int) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("issue_id") == issue_id
        and type(value.get("attempt")) is int
        and value["attempt"] > 0
        and isinstance(value.get("agent_id"), str)
        and value["agent_id"]
        and parse_time(value.get("claimed_at")) is not None
    )


def _validate_occurrence(issue_key: str, occurrence_id: str, value: Any, *, legacy: bool) -> None:
    if not isinstance(value, dict) or value.get("occurrence_id") != occurrence_id:
        raise FailureStateError(f"attention occurrence {occurrence_id} is invalid")
    if not legacy:
        required = {item.name for item in dataclass_fields(AttentionRecord)}
        if not required.issubset(value):
            raise FailureStateError(f"attention occurrence {occurrence_id} fields are incomplete")
        if type(value["schema_version"]) is not int or value["schema_version"] != 2:
            raise FailureStateError(f"attention occurrence {occurrence_id} schema is invalid")
        if type(value["issue_id"]) is not int or value["issue_id"] <= 0:
            raise FailureStateError(f"attention occurrence {occurrence_id} issue is invalid")
        if type(value["autonomous"]) is not bool or type(value["inhibited"]) is not bool:
            raise FailureStateError(f"attention occurrence {occurrence_id} flags are invalid")
        if type(value["attempt_consumed"]) is not bool and value["attempt_consumed"] is not None:
            raise FailureStateError(f"attention occurrence {occurrence_id} accounting is invalid")
    try:
        record = AttentionRecord.from_json(value, legacy=legacy)
    except (KeyError, TypeError, ValueError) as exc:
        raise FailureStateError(f"attention occurrence {occurrence_id} contract is invalid") from exc
    if record.issue_id != int(issue_key) or record.publication_state.value != "ready":
        raise FailureStateError(f"attention occurrence {occurrence_id} identity is invalid")
    expected = f"worklink-attention:{record.issue_id}:{record.error_signature}:{record.occurrence_id}"
    if not legacy and record.delivery_key != expected:
        raise FailureStateError(f"attention occurrence {occurrence_id} delivery identity is invalid")
    if value.get("handled_at") is not None and value.get("handling_disposition") not in {item.value for item in HandlingDisposition}:
        raise FailureStateError(f"attention occurrence {occurrence_id} handling is invalid")
    lease = value.get("handling_lease")
    if lease is not None and (
        not isinstance(lease, dict)
        or not isinstance(lease.get("lease_id"), str)
        or not lease["lease_id"]
        or not isinstance(lease.get("owner"), str)
        or parse_time(lease.get("expires_at")) is None
    ):
        raise FailureStateError(f"attention occurrence {occurrence_id} lease is invalid")


def reserve_execution(
    state_dir: Path,
    *,
    issue_id: int,
    source: str,
    operation_stage: str,
    execution_id: str | None = None,
    run_id: str | None = None,
    launch_id: str | None = None,
    invocation_id: str | None = None,
    autonomous: bool = True,
    reservation_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not autonomous:
        raise ValueError("execution reservations require autonomous provenance")
    now_iso = (now or datetime.now(UTC)).isoformat()
    reservation_id = reservation_id or uuid.uuid4().hex
    execution_id = execution_id or uuid.uuid4().hex
    with failure_state_transaction(state_dir) as state:
        entry = _issue(state, issue_id)
        existing = entry["reservations"].get(reservation_id)
        if existing is not None:
            _validate_reservation(reservation_id, existing)
            if existing.get("issue_id") != issue_id or existing.get("execution_id") != execution_id or existing.get("source") != source:
                raise FailureStateError("execution reservation replay identity mismatch")
            return dict(existing)
        reservation = {
            "reservation_id": reservation_id,
            "execution_id": execution_id,
            "issue_id": issue_id,
            "run_id": run_id,
            "launch_id": launch_id,
            "autonomous": True,
            "invocation_id": invocation_id,
            "source": source,
            "operation_stage": operation_stage,
            "prepared_claim": None,
            "confirmed_claim": None,
            "claim_binding_state": "none",
            "observations": {},
            "state": "reserved",
            "closure": None,
            "promoted_occurrence_id": None,
            "created_at": now_iso,
            "updated_at": now_iso,
            "closed_at": None,
        }
        entry["reservations"][reservation_id] = reservation
    return reservation


def checkpoint_reservation(state_dir: Path, issue_id: int, reservation_id: str, **updates: Any) -> dict[str, Any]:
    permitted = {"operation_stage", "prepared_claim", "confirmed_claim", "claim_binding_state", "observations", "launch_id", "run_id"}
    if set(updates) - permitted:
        raise ValueError("unsupported execution reservation checkpoint")
    with failure_state_transaction(state_dir) as state:
        reservation = _issue(state, issue_id)["reservations"].get(reservation_id)
        _validate_reservation(reservation_id, reservation)
        if reservation["state"] != "reserved":
            raise FailureStateError("closed execution reservation cannot be changed")
        reservation.update(updates)
        reservation["updated_at"] = datetime.now(UTC).isoformat()
        result = dict(reservation)
    return result


def close_reservation_excluded(state_dir: Path, issue_id: int, reservation_id: str, *, witness: str) -> dict[str, Any]:
    if not witness.strip():
        raise ValueError("excluded reservation closure requires a witness")
    with failure_state_transaction(state_dir) as state:
        reservation = _issue(state, issue_id)["reservations"].get(reservation_id)
        _validate_reservation(reservation_id, reservation)
        if reservation["state"] == "closed":
            if reservation["closure"] != "excluded":
                raise FailureStateError("promoted reservation cannot be excluded")
            return dict(reservation)
        now = datetime.now(UTC).isoformat()
        reservation.update(state="closed", closure="excluded", exclusion_witness=redact_text(witness)[:500], closed_at=now, updated_at=now)
        result = dict(reservation)
    return result


def promote_reservation(state_dir: Path, issue_id: int, reservation_id: str, record: AttentionRecord) -> dict[str, Any]:
    if record.issue_id != issue_id:
        raise ValueError("attention occurrence issue mismatch")
    payload = json.loads(json.dumps(record.to_json()))
    with failure_state_transaction(state_dir) as state:
        entry = _issue(state, issue_id)
        reservation = entry["reservations"].get(reservation_id)
        _validate_reservation(reservation_id, reservation)
        if reservation["execution_id"] != record.execution_id:
            raise FailureStateError("attention occurrence execution mismatch")
        if reservation["state"] == "closed":
            if reservation["closure"] != "promoted" or reservation["promoted_occurrence_id"] != record.occurrence_id:
                raise FailureStateError("execution reservation closure mismatch")
            existing = entry["occurrences"].get(record.occurrence_id)
            _validate_replay(existing, record)
            return dict(existing)
        existing = entry["occurrences"].get(record.occurrence_id)
        if existing is not None:
            _validate_replay(existing, record)
        count_recurrence = record.original_result_status in {None, "failed"}
        consecutive = int(entry.get("consecutive", 0)) or 1
        if count_recurrence:
            consecutive = (
                consecutive + 1
                if entry.get("signature") == record.error_signature
                and entry.get("active") is True
                else 1
            )
        entry["occurrences"][record.occurrence_id] = payload
        now = datetime.now(UTC).isoformat()
        reservation.update(state="closed", closure="promoted", promoted_occurrence_id=record.occurrence_id, closed_at=now, updated_at=now)
        entry["inhibited"] = entry.get("inhibited") is True or record.inhibited
        _set_compatibility_fields(entry, payload)
        entry["consecutive"] = consecutive
        delay = min(
            INITIAL_BACKOFF_MINUTES * (2 ** min(consecutive - 1, 8)),
            MAX_BACKOFF_MINUTES,
        )
        created = parse_time(record.created_at) or datetime.now(UTC)
        if count_recurrence or "retry_after" not in entry:
            entry["retry_after"] = (created + timedelta(minutes=delay)).isoformat()
        entry.setdefault("exit_status", 1)
    return payload


def _validate_replay(existing: Any, record: AttentionRecord) -> None:
    if not isinstance(existing, dict):
        raise FailureStateError("attention occurrence replay is missing")
    frozen = AttentionRecord.from_json(existing, legacy=existing.get("source") == "legacy_v1")
    identity = (
        "issue_id", "execution_id", "occurrence_id", "delivery_key", "source",
        "kind", "primary_source", "outcome", "accounting_basis",
        "attempt_consumed", "claim", "prior_claim", "claim_relation",
        "claim_binding_state", "evidence_quality", "run_id", "launch_id", "attempt",
        "original_result_status", "original_factory_status", "validation_detail", "cause",
        "error_signature", "reason", "refs", "factory_projection", "controller_phase",
        "controller_error", "pr_url", "pr_state", "pr_head", "next", "next_present",
        "autonomous", "inhibited", "publication_state", "reset_generation_baseline",
        "ready_cycle_baseline", "manual_claim_baseline",
    )
    if any(getattr(frozen, name) != getattr(record, name) for name in identity):
        raise FailureStateError("attention occurrence replay identity mismatch")


def append_secondary_fault(state_dir: Path, issue_id: int, occurrence_id: str, *, source: str, cause: str, reason: str) -> None:
    with failure_state_transaction(state_dir) as state:
        occurrence = _issue(state, issue_id)["occurrences"].get(occurrence_id)
        if not isinstance(occurrence, dict):
            raise FailureStateError("attention occurrence not found")
        faults = occurrence.setdefault("secondary_faults", [])
        item = {"source": source, "cause": cause, "reason": terminal_error(reason)}
        if item not in faults:
            faults.append(item)


def mark_attention_settlement(
    state_dir: Path,
    issue_id: int,
    occurrence_id: str,
    settlement: str,
) -> bool:
    if settlement not in {"pending", "applied", "not_needed"}:
        raise ValueError("invalid attention settlement")
    with failure_state_transaction(state_dir) as state:
        occurrence = _issue(state, issue_id)["occurrences"].get(occurrence_id)
        if not isinstance(occurrence, dict):
            return False
        prior = occurrence.get("settlement")
        if prior == "applied" and settlement != "applied":
            return False
        occurrence["settlement"] = settlement
        return True


def pending_attention_records(state_dir: Path, *, limit: int = 32) -> list[dict[str, Any]]:
    state = load_failure_state(state_dir)
    pending = []
    for entry in state["issues"].values():
        for occurrence in entry.get("occurrences", {}).values():
            if not isinstance(occurrence, dict) or occurrence.get("handled_at") or occurrence.get("retirement"):
                continue
            if occurrence.get("kind") not in {"attention", "factory_started", "factory_succeeded"}:
                continue
            pending.append(dict(occurrence))
    pending.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("occurrence_id") or "")))
    return pending[: max(0, min(limit, 32))]


def pending_execution_reservations(
    state_dir: Path,
    *,
    execution_id: str | None = None,
    limit: int = 32,
) -> list[dict[str, Any]]:
    state = load_failure_state(state_dir)
    pending = []
    for entry in state["issues"].values():
        for reservation in entry.get("reservations", {}).values():
            if not isinstance(reservation, dict) or reservation.get("state") != "reserved":
                continue
            if execution_id is not None and reservation.get("execution_id") != execution_id:
                continue
            pending.append(dict(reservation))
    pending.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("reservation_id") or "")))
    return pending[: max(0, min(limit, 32))]


def mark_attention_delivered(state_dir: Path, issue_id: int, occurrence_id: str, *, origin_ref: str | None = None, delivered_at: str | None = None) -> bool:
    with failure_state_transaction(state_dir) as state:
        occurrence = _issue(state, issue_id)["occurrences"].get(occurrence_id)
        if not isinstance(occurrence, dict) or occurrence.get("handled_at") or occurrence.get("retirement"):
            return False
        if occurrence.get("delivered_at") is None:
            occurrence["delivered_at"] = delivered_at or datetime.now(UTC).isoformat()
        if origin_ref:
            bound = occurrence.get("origin_ref")
            if bound not in {None, origin_ref}:
                raise FailureStateError("attention occurrence is bound to another origin")
            occurrence["origin_ref"] = origin_ref
        return True


def bind_attention_origin(
    state_dir: Path,
    issue_id: int,
    signature: str,
    occurrence_id: str,
    origin_ref: str,
    channel_id: str,
) -> bool:
    if not origin_ref or channel_id != "poller:worklink-attention":
        return False
    with failure_state_transaction(state_dir) as state:
        occurrence = _issue(state, issue_id)["occurrences"].get(occurrence_id)
        if not isinstance(occurrence, dict) or occurrence.get("error_signature") != signature or occurrence.get("handled_at") or occurrence.get("retirement"):
            return False
        binding = occurrence.get("delivery_binding")
        expected = {"origin_ref": origin_ref, "channel_id": channel_id}
        if binding is not None and binding != expected:
            return False
        occurrence["delivery_binding"] = expected
        return True


def acquire_handling_lease(state_dir: Path, issue_id: int, occurrence_id: str, *, owner: str, ttl_seconds: int = 120) -> str | None:
    now = datetime.now(UTC)
    with failure_state_transaction(state_dir) as state:
        occurrence = _issue(state, issue_id)["occurrences"].get(occurrence_id)
        if not isinstance(occurrence, dict) or occurrence.get("handled_at") or occurrence.get("retirement"):
            return None
        lease = occurrence.get("handling_lease")
        if isinstance(lease, dict) and (expires := parse_time(lease.get("expires_at"))) is not None and expires > now:
            return None
        lease_id = uuid.uuid4().hex
        occurrence["handling_lease"] = {"lease_id": lease_id, "owner": owner, "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat()}
        return lease_id


def mark_attention_handled(
    state_dir: Path,
    issue_id: int,
    signature: str,
    occurrence_id: str,
    disposition: HandlingDisposition | str,
    *,
    lease_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    disposition = HandlingDisposition(disposition)
    with failure_state_transaction(state_dir) as state:
        entry = _issue(state, issue_id)
        occurrence = entry["occurrences"].get(occurrence_id)
        if not isinstance(occurrence, dict) or occurrence.get("error_signature") != signature:
            return False
        if occurrence.get("handled_at"):
            return occurrence.get("handling_disposition") == disposition.value
        lease = occurrence.get("handling_lease")
        if lease_id is not None and (not isinstance(lease, dict) or lease.get("lease_id") != lease_id):
            return False
        occurrence["handling_disposition"] = disposition.value
        occurrence["handled_at"] = datetime.now(UTC).isoformat()
        occurrence["handling"] = {"lease_id": lease_id, **dict(metadata or {})}
        occurrence.pop("handling_lease", None)
        return True


def release_handling_lease(state_dir: Path, issue_id: int, occurrence_id: str, lease_id: str) -> bool:
    with failure_state_transaction(state_dir) as state:
        occurrence = _issue(state, issue_id)["occurrences"].get(occurrence_id)
        lease = occurrence.get("handling_lease") if isinstance(occurrence, dict) else None
        if not isinstance(lease, dict) or lease.get("lease_id") != lease_id:
            return False
        occurrence.pop("handling_lease", None)
        return True


def retire_attention(state_dir: Path, issue_id: int, occurrence_id: str, retirement: RecoveryRetirement | str) -> bool:
    retirement = RecoveryRetirement(retirement)
    with failure_state_transaction(state_dir) as state:
        occurrence = _issue(state, issue_id)["occurrences"].get(occurrence_id)
        if not isinstance(occurrence, dict) or occurrence.get("handled_at"):
            return False
        prior = occurrence.get("retirement")
        if prior not in {None, retirement.value}:
            return False
        occurrence["retirement"] = retirement.value
        occurrence["retired_at"] = occurrence.get("retired_at") or datetime.now(UTC).isoformat()
        return True


def get_attention_record(home_or_state_dir: Path, issue_id: int, signature: str, occurrence_id: str) -> AttentionRecord:
    state_dir = home_or_state_dir
    if state_dir.name != POLLER_NAME:
        state_dir = dispatch_failure_state_dir(home_or_state_dir)
    state = load_failure_state(state_dir)
    entry = state["issues"].get(str(issue_id))
    occurrence = entry.get("occurrences", {}).get(occurrence_id) if isinstance(entry, dict) else None
    if not isinstance(occurrence, dict) or str(occurrence.get("error_signature") or "") != signature:
        raise KeyError("attention occurrence not found")
    return AttentionRecord.from_json(occurrence, legacy=occurrence.get("source") == "legacy_v1")


def issue_is_inhibited(state_dir: Path, issue_id: int) -> bool:
    state = load_failure_state(state_dir)
    entry = state["issues"].get(str(issue_id))
    return isinstance(entry, dict) and entry.get("inhibited") is True


def issue_has_unsettled_attention(state_dir: Path, issue_id: int) -> bool:
    state = load_failure_state(state_dir)
    entry = state["issues"].get(str(issue_id))
    if not isinstance(entry, dict):
        return False
    if any(
        isinstance(reservation, dict) and reservation.get("state") == "reserved"
        for reservation in entry.get("reservations", {}).values()
    ):
        return True
    return entry.get("inhibited") is True or any(
        isinstance(occurrence, dict) and occurrence.get("settlement") == "pending"
        for occurrence in entry.get("occurrences", {}).values()
    )


def attention_terminal_ready(state_dir: Path, issue_id: int, occurrence_id: str) -> bool:
    state = load_failure_state(state_dir)
    entry = state["issues"].get(str(issue_id))
    occurrence = entry.get("occurrences", {}).get(occurrence_id) if isinstance(entry, dict) else None
    return bool(
        isinstance(occurrence, dict)
        and occurrence.get("inhibited") is True
        and occurrence.get("settlement") in {"applied", "not_needed"}
    )


def observe_rearm_state(
    state_dir: Path,
    *,
    ready_issue_ids: set[int],
    blocked_issue_ids: set[int],
) -> set[int]:
    rearmed: set[int] = set()
    with failure_state_transaction(state_dir) as state:
        for raw_issue, entry in state["issues"].items():
            if not isinstance(entry, dict) or entry.get("inhibited") is not True:
                continue
            issue_id = int(raw_issue)
            ready = issue_id in ready_issue_ids and issue_id not in blocked_issue_ids
            if not ready:
                entry["rearm_observation"] = "disarmed"
                continue
            if entry.get("rearm_observation") == "disarmed" and not _has_pending_settlement(entry):
                entry["inhibited"] = False
                entry["rearm_observation"] = "ready_after_disarmed"
                entry["ready_cycle_generation"] = entry.get("ready_cycle_generation", 0) + 1
                entry["arming_generation"] = int(entry.get("arming_generation", 0)) + 1
                rearmed.add(issue_id)
    return rearmed


def observe_reset_rearm(state_dir: Path, issue_id: int, reset_generation: int) -> bool:
    if str(issue_id) not in load_failure_state(state_dir)["issues"]:
        return False
    with failure_state_transaction(state_dir) as state:
        entry = _issue(state, issue_id)
        prior = entry.get("reset_generation")
        entry["reset_generation"] = reset_generation
        if reset_generation > (prior if type(prior) is int else 0) and entry.get("inhibited") is True and not _has_pending_settlement(entry):
            entry["inhibited"] = False
            entry["arming_generation"] = int(entry.get("arming_generation", 0)) + 1
            return True
        return False


def observe_manual_claim_rearm(state_dir: Path, issue_id: int, claim_identity: Mapping[str, Any]) -> bool:
    if not _valid_claim(claim_identity, issue_id):
        raise ValueError("manual rearm requires an exact claim identity")
    if str(issue_id) not in load_failure_state(state_dir)["issues"]:
        return False
    with failure_state_transaction(state_dir) as state:
        entry = _issue(state, issue_id)
        prior = entry.get("manual_claim_witness")
        witness = dict(claim_identity)
        entry["manual_claim_witness"] = witness
        if prior != witness and entry.get("inhibited") is True and not _has_pending_settlement(entry):
            entry["inhibited"] = False
            entry["arming_generation"] = int(entry.get("arming_generation", 0)) + 1
            return True
        return False


def _has_pending_settlement(entry: Mapping[str, Any]) -> bool:
    return any(
        isinstance(item, Mapping) and item.get("settlement") == "pending"
        for item in entry.get("occurrences", {}).values()
    )


def is_transient_contention(error: str) -> bool:
    normalized = error.casefold()
    return any(all(marker in normalized for marker in markers) for markers in _TRANSIENT_CONTENTION_MARKERS)


def record_transient_contention(
    state_dir: Path,
    issue_id: int,
    reservation_id: str,
    error: str,
    *,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    with failure_state_transaction(state_dir) as state:
        entry = _issue(state, issue_id)
        reservation = entry["reservations"].get(reservation_id)
        _validate_reservation(reservation_id, reservation)
        generation = int(entry.get("arming_generation", 0))
        signature = error_signature(terminal_error(error))
        key = f"{generation}:{reservation['source']}:{signature}"
        observations = entry.setdefault("transient_contention", {})
        if not isinstance(observations, dict):
            raise FailureStateError("transient contention state is invalid")
        generation_retry = entry.get("transient_retry_generation") == generation
        observations[key] = int(observations.get(key, 0)) + 1
        entry["transient_contention_observations"] = int(
            entry.get("transient_contention_observations", 0)
        ) + 1
        if generation_retry:
            return True
        entry["transient_retry_generation"] = generation
        timestamp = now.isoformat()
        reservation.update(
            state="closed",
            closure="excluded",
            exclusion_witness="bounded_transient_contention_retry",
            closed_at=timestamp,
            updated_at=timestamp,
        )
        entry.update(
            active=True,
            issue_id=issue_id,
            signature="",
            terminal_error=terminal_error(error),
            retry_after=(now + timedelta(minutes=INITIAL_BACKOFF_MINUTES)).isoformat(),
            occurrence_id=None,
            notified_signatures=[],
            consecutive=1,
            inhibited=False,
        )
        return False


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
        return {"active": False, "issue_id": issue_id, "attempt": None, "attempt_consumed": False, "exit_status": exit_status, "terminal_error": safe_error, "signature": signature, "transient_contention": True, "failed_at": now.isoformat(), "retry_after": None, "log_path": redact_text(log_path or ""), "preserved_ref": redact_text(preserved_ref or "")[:1000] or None, "preservation_error": redact_text(preservation_error or "")[:1000] or None, "notified_signatures": []}
    with failure_state_transaction(state_dir) as state:
        entry = _issue(state, issue_id)
        consecutive = int(entry.get("consecutive", 0)) + 1 if entry.get("signature") == signature and entry.get("active") is True else 1
        delay = min(INITIAL_BACKOFF_MINUTES * (2 ** min(consecutive - 1, 8)), MAX_BACKOFF_MINUTES)
        compatibility = {
            "active": True, "issue_id": issue_id, "attempt": attempt,
            "attempt_consumed": attempt is not None, "exit_status": exit_status,
            "terminal_error": safe_error, "signature": signature,
            "occurrence_id": uuid.uuid4().hex, "consecutive": consecutive,
            "failed_at": now.isoformat(), "retry_after": (now + timedelta(minutes=delay)).isoformat(),
            "log_path": redact_text(log_path or ""),
            "preserved_ref": redact_text(preserved_ref or "")[:1000] or None,
            "preservation_error": redact_text(preservation_error or "")[:1000] or None,
            "notified_signatures": list(entry.get("notified_signatures") or [])[-MAX_NOTIFIED_SIGNATURES:],
        }
        entry.update(compatibility)
    return compatibility


def pending_failure_alerts(state_dir: Path, *, now: datetime | None = None) -> tuple[set[int], list[dict[str, object]]]:
    now = now or datetime.now(UTC)
    backed_off: set[int] = set()
    alerts: list[dict[str, object]] = []
    state = load_failure_state(state_dir)
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
        notified = entry.get("notified_signatures") if isinstance(entry.get("notified_signatures"), list) else []
        if signature and signature not in notified:
            alerts.append({"signal": "worklink_run_failure_escalated", "source_id": f"worklink-run-failure:{issue_id}:{signature}", "issue_id": issue_id, "attempt": entry.get("attempt"), "attempt_consumed": entry.get("attempt_consumed"), "exit_status": entry.get("exit_status"), "terminal_error": entry.get("terminal_error"), "error_signature": signature, "failure_occurrence_id": entry.get("occurrence_id"), "log": entry.get("log_path"), "preserved_ref": entry.get("preserved_ref"), "preservation_error": entry.get("preservation_error"), "retry_after": entry.get("retry_after"), "routing_instructions": "Notify the operator that a detached Worklink run failed. Include the run-log path, terminal error, and any preserved ref or preservation error."})
    return backed_off, alerts


def mark_failure_notified(state_dir: Path, issue_id: int, signature: str, occurrence_id: str | None) -> None:
    with failure_state_transaction(state_dir) as state:
        entry = state["issues"].get(str(issue_id))
        if not isinstance(entry, dict) or entry.get("active") is not True or entry.get("signature") != signature or entry.get("occurrence_id") != occurrence_id:
            return
        notified = list(entry.get("notified_signatures") or [])
        if signature not in notified:
            notified.append(signature)
        entry["notified_signatures"] = notified[-MAX_NOTIFIED_SIGNATURES:]


def record_success(state_dir: Path, issue_id: int) -> None:
    with failure_state_transaction(state_dir) as state:
        entry = state["issues"].get(str(issue_id))
        if not isinstance(entry, dict):
            return
        entry["active"] = False
        entry["consecutive"] = 0
        entry["notified_signatures"] = []
        entry["arming_generation"] = int(entry.get("arming_generation", 0)) + 1
        entry["transient_contention"] = {}
        entry["transient_contention_observations"] = 0
        entry.pop("transient_retry_generation", None)


def _set_compatibility_fields(entry: dict[str, Any], occurrence: Mapping[str, Any]) -> None:
    refs = occurrence.get("refs")
    refs = refs if isinstance(refs, Mapping) else {}
    entry.update(
        {
            "active": occurrence.get("inhibited") is True,
            "issue_id": occurrence["issue_id"],
            "attempt": occurrence.get("attempt"),
            "attempt_consumed": occurrence.get("attempt_consumed"),
            "terminal_error": occurrence.get("reason"),
            "signature": occurrence.get("error_signature"),
            "occurrence_id": occurrence.get("occurrence_id"),
            "failed_at": occurrence.get("created_at"),
            "log_path": refs.get("log"),
            "preserved_ref": refs.get("preserved_ref"),
            "preservation_error": refs.get("preservation_error"),
            "notified_signatures": list(entry.get("notified_signatures") or []),
        }
    )


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


reserve_execution_reservation = reserve_execution
checkpoint_execution_reservation = checkpoint_reservation
close_execution_reservation = close_reservation_excluded
promote_execution_reservation = promote_reservation
mark_attention_retired = retire_attention
