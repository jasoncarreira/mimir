"""Typed, closed Worklink outcome and attention contract.

This module intentionally has no imports from the Worklink runtime.  Detached
launchers, the ledger, and the eventual prompt consumer all import these schema
definitions, so importing it must not initialize claims or agent machinery.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
import re
import json
import os
import stat
import subprocess
from typing import Any, Mapping


class AttentionSchemaError(ValueError):
    """A persisted outcome does not satisfy the closed attention contract."""


class AttentionKind(StrEnum):
    START = "start"
    SUCCESS = "success"
    ATTENTION = "attention"
    LEGACY_ATTENTION = "legacy_attention"


class AttentionTarget(StrEnum):
    LEAF = "leaf"
    FACTORY = "factory"


class AttentionSource(StrEnum):
    # Queue, startup, and CLI admission.
    QUEUE_LEAF_SPAWN = "queue_leaf_spawn"
    QUEUE_FACTORY_SPAWN = "queue_factory_spawn"
    STARTUP_LEAF_SPAWN = "startup_leaf_spawn"
    STARTUP_FACTORY_SPAWN = "startup_factory_spawn"
    LEAF_CLI_REPOSITORY = "leaf_cli_repository"
    FACTORY_CLI_REPOSITORY = "factory_cli_repository"
    LEAF_ISSUE_READ = "leaf_issue_read"
    LEAF_TARGET_BRANCH = "leaf_target_branch"
    LEAF_TEMPLATE = "leaf_template"
    LEAF_CONFIG = "leaf_config"
    LEAF_INVENTORY = "leaf_inventory"
    LEAF_REPOSITORY = "leaf_repository"
    LEAF_BACKEND = "leaf_backend"
    LEAF_COMPUTE = "leaf_compute"
    LEAF_CLAIM_PREPARATION = "leaf_claim_preparation"
    FACTORY_INTERLOCK = "factory_interlock"
    FACTORY_ISSUE_READ = "factory_issue_read"
    FACTORY_ISSUE_KIND = "factory_issue_kind"
    FACTORY_TARGET_BRANCH = "factory_target_branch"
    FACTORY_CONFIG = "factory_config"
    FACTORY_BACKEND = "factory_backend"
    FACTORY_REPOSITORY = "factory_repository"
    FACTORY_COMPUTE = "factory_compute"
    FACTORY_LAUNCHER = "factory_launcher"
    FACTORY_INVENTORY = "factory_inventory"
    FACTORY_BASE_LOOKUP = "factory_base_lookup"
    FACTORY_WORK_ITEM = "factory_work_item"
    FACTORY_RETAINED_BINDING = "factory_retained_binding"
    # Claim protocol.
    CLAIM_COMMAND = "claim_command"
    CLAIM_GUARD = "claim_guard"
    CLAIM_STEAL = "claim_steal"
    CLAIM_BUDGET = "claim_budget"
    CLAIM_CAPACITY_READ = "claim_capacity_read"
    CLAIM_UNREADY = "claim_unready"
    CLAIM_INPROGRESS = "claim_inprogress"
    CLAIM_COMMENT = "claim_comment"
    CLAIM_CONTENTION = "claim_contention"
    # Leaf execution.
    LEAF_CHECKOUT_CREATE = "leaf_checkout_create"
    LEAF_PUBLICATION_CAPTURE = "leaf_publication_capture"
    LEAF_CHECKOUT_ISOLATION = "leaf_checkout_isolation"
    LEAF_DIRTY_SNAPSHOT = "leaf_dirty_snapshot"
    LEAF_PROMPT = "leaf_prompt"
    LEAF_WORK_SPEC = "leaf_work_spec"
    LEAF_REPORT_SETUP = "leaf_report_setup"
    LEAF_COMPUTE_LAUNCH = "leaf_compute_launch"
    LEAF_HANDLE_SAVE = "leaf_handle_save"
    LEAF_COMPUTE_WAIT = "leaf_compute_wait"
    LEAF_INTERPRET = "leaf_interpret"
    LEAF_TEST_REPORT = "leaf_test_report"
    LEAF_PR_BODY = "leaf_pr_body"
    LEAF_GATE = "leaf_gate"
    LEAF_REGATE = "leaf_regate"
    LEAF_EVIDENCE_WRITE = "leaf_evidence_write"
    LEAF_COMMIT = "leaf_commit"
    LEAF_PUBLICATION_FENCE = "leaf_publication_fence"
    LEAF_PUSH = "leaf_push"
    LEAF_PR_OPEN = "leaf_pr_open"
    LEAF_EVIDENCE_COMMENT = "leaf_evidence_comment"
    LEAF_BACKEND_OUTCOME = "leaf_backend_outcome"
    LEAF_TERMINAL_LABELS = "leaf_terminal_labels"
    LEAF_TERMINAL_RELEASE = "leaf_terminal_release"
    LEAF_INTERRUPT = "leaf_interrupt"
    REATTACH_STATE = "reattach_state"
    REATTACH_SHIM = "reattach_shim"
    REATTACH_PR = "reattach_pr"
    REATTACH_BACKEND = "reattach_backend"
    REATTACH_COMPUTE = "reattach_compute"
    REATTACH_ISSUE = "reattach_issue"
    REATTACH_CHECKOUT = "reattach_checkout"
    REATTACH_PROMPT = "reattach_prompt"
    REATTACH_SPEC = "reattach_spec"
    REATTACH_WAIT = "reattach_wait"
    LEAF_STARTUP_RECONCILE = "leaf_startup_reconcile"
    # Factory execution.
    FACTORY_CHECKOUT = "factory_checkout"
    FACTORY_GIT_IDENTITY = "factory_git_identity"
    FACTORY_PUBLISHING_IDENTITY = "factory_publishing_identity"
    FACTORY_CREDENTIAL = "factory_credential"
    FACTORY_IDENTITY_VERIFY = "factory_identity_verify"
    FACTORY_SPEC = "factory_spec"
    FACTORY_LAUNCH_BINDING = "factory_launch_binding"
    FACTORY_SANDBOX = "factory_sandbox"
    FACTORY_PERMISSIONS = "factory_permissions"
    FACTORY_LAUNCH = "factory_launch"
    FACTORY_HANDLE_SAVE = "factory_handle_save"
    FACTORY_RECOVERY_BINDING = "factory_recovery_binding"
    FACTORY_RECOVERY_STATUS = "factory_recovery_status"
    FACTORY_RECOVERY_LOCK = "factory_recovery_lock"
    FACTORY_RESUME = "factory_resume"
    FACTORY_RECOVERY_REPOSITORY = "factory_recovery_repository"
    FACTORY_RECOVERY_SPEC = "factory_recovery_spec"
    FACTORY_RECOVERY_LAUNCH = "factory_recovery_launch"
    FACTORY_RECOVERY_SAVE = "factory_recovery_save"
    FACTORY_WAIT_START = "factory_wait_start"
    FACTORY_STATUS_READ = "factory_status_read"
    FACTORY_STARTUP_DEADLINE = "factory_startup_deadline"
    FACTORY_RUN_DEADLINE = "factory_run_deadline"
    FACTORY_STATUS_BINDING = "factory_status_binding"
    FACTORY_OWNER = "factory_owner"
    FACTORY_OBSERVATION_SAVE = "factory_observation_save"
    FACTORY_HEARTBEAT = "factory_heartbeat"
    FACTORY_WAIT_DRAIN = "factory_wait_drain"
    FACTORY_DRIVER_EXIT = "factory_driver_exit"
    FACTORY_TRANSCRIPT = "factory_transcript"
    FACTORY_CLEANUP = "factory_cleanup"
    FACTORY_PARKED = "factory_parked"
    FACTORY_BLOCKED = "factory_blocked"
    FACTORY_PARTIAL = "factory_partial"
    FACTORY_COMPLETION_VERIFY = "factory_completion_verify"
    FACTORY_TERMINAL_LABELS = "factory_terminal_labels"
    FACTORY_TERMINAL_RELEASE = "factory_terminal_release"
    FACTORY_INTERRUPT = "factory_interrupt"
    FACTORY_STARTUP_RECONCILE = "factory_startup_reconcile"
    # Lifecycle and compatibility.
    LEAF_START = "leaf_start"
    FACTORY_START = "factory_start"
    LEAF_SUCCESS = "leaf_success"
    FACTORY_SUCCESS = "factory_success"
    LEGACY_V1 = "legacy_v1"


class AttentionCause(StrEnum):
    SPAWN_FAILED = "spawn_failed"
    REPOSITORY_UNAVAILABLE = "repository_unavailable"
    READ_FAILED = "read_failed"
    INVALID_TARGET_BRANCH = "invalid_target_branch"
    TEMPLATE_MISSING = "template_missing"
    CONFIGURATION_INVALID = "configuration_invalid"
    STATE_WRITE_FAILED = "state_write_failed"
    INTERLOCK_UNAVAILABLE = "interlock_unavailable"
    NOT_EPIC = "not_epic"
    LAUNCHER_UNAVAILABLE = "launcher_unavailable"
    BASE_MISSING = "base_missing"
    BASE_READ_FAILED = "base_read_failed"
    WORK_ITEM_INVALID = "work_item_invalid"
    RECOVERY_BINDING_INVALID = "recovery_binding_invalid"
    CLAIM_COMMAND_FAILED = "claim_command_failed"
    OWNER_READ_FAILED = "owner_read_failed"
    STEAL_FAILED = "steal_failed"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    LOCK_INVENTORY_FAILED = "lock_inventory_failed"
    CLAIM_PUBLICATION_FAILED = "claim_publication_failed"
    CONTENTION_EXHAUSTED = "contention_exhausted"
    CHECKOUT_FAILED = "checkout_failed"
    PUBLICATION_BOUNDARY_FAILED = "publication_boundary_failed"
    UNSAFE_CHECKOUT = "unsafe_checkout"
    CHECKOUT_READ_FAILED = "checkout_read_failed"
    PROMPT_RENDER_FAILED = "prompt_render_failed"
    WORK_SPEC_FAILED = "work_spec_failed"
    REPORT_SETUP_FAILED = "report_setup_failed"
    LAUNCH_FAILED = "launch_failed"
    WORKER_FAILED = "worker_failed"
    INTERPRETATION_FAILED = "interpretation_failed"
    EVIDENCE_READ_FAILED = "evidence_read_failed"
    VALIDATION_FAILED = "validation_failed"
    EVIDENCE_WRITE_FAILED = "evidence_write_failed"
    PUBLICATION_FAILED = "publication_failed"
    BOOKKEEPING_FAILED = "bookkeeping_failed"
    BACKEND_BLOCKED = "backend_blocked"
    BACKEND_FAILED = "backend_failed"
    TERMINAL_ROUTING_FAILED = "terminal_routing_failed"
    INTERRUPTED = "interrupted"
    STATE_MISSING = "state_missing"
    STATE_UNREADABLE = "state_unreadable"
    WORKER_INTERRUPTED = "worker_interrupted"
    IDENTITY_STALE = "identity_stale"
    CLEANUP_FAILED = "cleanup_failed"
    PR_READ_FAILED = "pr_read_failed"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    NOT_RESUMABLE = "not_resumable"
    WORKER_LOST = "worker_lost"
    CONTROLLER_LOST = "controller_lost"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    IDENTITY_MISMATCH = "identity_mismatch"
    SANDBOX_FAILED = "sandbox_failed"
    PERMISSIONS_FAILED = "permissions_failed"
    RECOVERY_STATE_INVALID = "recovery_state_invalid"
    LOCK_RECONCILIATION_FAILED = "lock_reconciliation_failed"
    RESUME_FAILED = "resume_failed"
    REPOSITORY_CHANGED = "repository_changed"
    SUPERVISION_FAILED = "supervision_failed"
    STATUS_READ_FAILED = "status_read_failed"
    STARTUP_TIMEOUT = "startup_timeout"
    RUN_TIMEOUT = "run_timeout"
    STATUS_BINDING_INVALID = "status_binding_invalid"
    OWNER_CHANGED = "owner_changed"
    HEARTBEAT_FAILED = "heartbeat_failed"
    RESULT_UNAVAILABLE = "result_unavailable"
    UNFINISHED_EXIT = "unfinished_exit"
    TRANSCRIPT_FAILED = "transcript_failed"
    NEEDS_HUMAN = "needs_human"
    BLOCKED = "blocked"
    PARTIAL = "partial"
    COMPLETION_INVALID = "completion_invalid"
    LEGACY_UNKNOWN = "legacy_unknown"


class AccountingScope(StrEnum):
    NO_NEW_CLAIM = "no_new_claim"
    BOUND_CLAIM = "bound_claim"
    DEFERRED = "deferred"
    UNKNOWN = "unknown"


class SettlementBasis(StrEnum):
    POSITIVE_WORK = "positive_work"
    NO_WORK = "no_work"
    UNKNOWN = "unknown"


class ClearancePolicy(StrEnum):
    SUCCESSOR_LEAF = "successor_leaf"
    SUCCESSOR_FACTORY = "successor_factory"
    VALIDATOR = "validator"
    RECOVERY = "recovery"
    LIFECYCLE = "lifecycle"
    LEGACY = "legacy"


class ReaderCode(StrEnum):
    TRACKER = "tracker"
    LEAF_RECORD = "leaf_record"
    LEAF_PROCESS = "leaf_process"
    FACTORY_RECORD = "factory_record"
    FACTORY_STATUS = "factory_status"
    FACTORY_PROCESS = "factory_process"
    CLAIM_OWNERS = "claim_owners"
    CHAINLINK_LOCKS = "chainlink_locks"
    EVIDENCE = "evidence"
    PULL_REQUEST = "pull_request"
    VALIDATOR = "validator"


class InspectionResult(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    READ_ERROR = "read_error"
    CURRENT = "current"
    SUPERSEDED = "superseded"
    CHANGED = "changed"


@dataclass(frozen=True)
class ClearanceInspection:
    source: AttentionSource
    result: InspectionResult
    required_readers: tuple[ReaderCode, ...]
    read_errors: tuple[ReaderCode, ...]
    current_state: str
    witness_id: str | None = None


_COMMON_READERS = (
    ReaderCode.TRACKER,
    ReaderCode.CLAIM_OWNERS,
    ReaderCode.CHAINLINK_LOCKS,
    ReaderCode.EVIDENCE,
    ReaderCode.PULL_REQUEST,
)


def clearance_requirements(source: AttentionSource | str) -> tuple[ReaderCode, ...]:
    """Return the closed required-reader set for one immutable source."""
    source_value = AttentionSource(source)
    rule = SOURCE_RULES[source_value]
    if source_value == AttentionSource.LEGACY_V1:
        return (*_COMMON_READERS, ReaderCode.LEAF_RECORD, ReaderCode.LEAF_PROCESS,
                ReaderCode.FACTORY_RECORD, ReaderCode.FACTORY_STATUS, ReaderCode.FACTORY_PROCESS)
    target_factory = source_value.value.startswith("factory_")
    if rule.clearance == ClearancePolicy.VALIDATOR:
        return (*_COMMON_READERS, ReaderCode.VALIDATOR)
    if target_factory:
        return (*_COMMON_READERS, ReaderCode.FACTORY_RECORD,
                ReaderCode.FACTORY_STATUS, ReaderCode.FACTORY_PROCESS)
    return (*_COMMON_READERS, ReaderCode.LEAF_RECORD, ReaderCode.LEAF_PROCESS)


def inspect_clearance(
    occurrence: Mapping[str, object],
    reads: Mapping[ReaderCode | str, str],
    *,
    witness_id: str | None = None,
) -> ClearanceInspection:
    """Evaluate source-specific clearance from strict, typed reader verdicts.

    Reader values are ``pass``, ``blocked``, ``current``, ``superseded``,
    ``changed``, or ``error``.  Every required read must be present; missing is
    an error, and any error wins over otherwise positive evidence.
    """
    source = AttentionSource(occurrence["source"])
    kind = AttentionKind(occurrence["kind"])
    required = clearance_requirements(source)
    normalized = {
        ReaderCode(key): value for key, value in reads.items()
    }
    errors = tuple(
        reader for reader in required if normalized.get(reader) in {None, "error"}
    )
    if errors:
        result = InspectionResult.READ_ERROR
        current_state = "unknown"
    elif kind in {AttentionKind.START, AttentionKind.SUCCESS}:
        values = {normalized[reader] for reader in required}
        if "superseded" in values:
            result = InspectionResult.SUPERSEDED
            current_state = "terminal_success"
        elif "changed" in values or "blocked" in values:
            result = InspectionResult.CHANGED
            current_state = "terminal_other"
        else:
            result = InspectionResult.CURRENT
            current_state = "active" if kind == AttentionKind.START else "terminal_success"
    else:
        resolved = all(normalized[reader] in {"pass", "superseded"} for reader in required)
        result = InspectionResult.RESOLVED if resolved else InspectionResult.UNRESOLVED
        current_state = "terminal_success" if resolved else "active"
    return ClearanceInspection(
        source=source,
        result=result,
        required_readers=required,
        read_errors=errors,
        current_state=current_state,
        witness_id=witness_id,
    )


def rehydrate_attention(
    home: Path,
    occurrence_id: str,
    *,
    runner: Any = None,
    validators: Mapping[AttentionSource, Any] | None = None,
) -> ClearanceInspection:
    """Strictly read all authorities needed to inspect one durable occurrence.

    This is deliberately read-only.  It never labels, claims, resumes, pushes,
    or publishes.  Every required authority is read even when an earlier
    predicate is false, so absence is not inferred from a partial view.
    """
    from .dispatch_failures import dispatch_failure_state_dir, load_outcome_state

    state = load_outcome_state(dispatch_failure_state_dir(home))
    issue_id: int | None = None
    occurrence: Mapping[str, object] | None = None
    issue_state: Mapping[str, Any] | None = None
    for key, candidate in state["issues"].items():
        found = candidate["occurrences"].get(occurrence_id)
        if found is not None:
            issue_id = int(key)
            occurrence = found
            issue_state = candidate
            break
    if issue_id is None or occurrence is None or issue_state is None:
        raise AttentionSchemaError("attention occurrence does not exist")

    source = AttentionSource(occurrence["source"])
    kind = AttentionKind(occurrence["kind"])
    required = clearance_requirements(source)
    run = runner or _strict_subprocess_runner(home)
    verdicts: dict[ReaderCode, str] = {}

    tracker: Mapping[str, object] | None = None
    try:
        response = run(["chainlink", "issue", "show", str(issue_id), "--json"])
        if response.returncode != 0:
            raise RuntimeError("tracker read failed")
        value = json.loads(response.stdout or "{}")
        if (
            not isinstance(value, dict)
            or int(value.get("id", value.get("number", 0))) != issue_id
            or not isinstance(value.get("labels", []), list)
            or not isinstance(value.get("comments", []), list)
        ):
            raise RuntimeError("tracker projection is invalid")
        tracker = value
        verdicts[ReaderCode.TRACKER] = "pass"
    except Exception:
        verdicts[ReaderCode.TRACKER] = "error"

    live_leaf = False
    leaf_state = None
    try:
        leaf_state = _strict_leaf_record(home, issue_id)
        if leaf_state is not None:
            from .run_state import process_is_alive

            live_leaf = process_is_alive(leaf_state)
        verdicts[ReaderCode.LEAF_RECORD] = "pass"
        verdicts[ReaderCode.LEAF_PROCESS] = "blocked" if live_leaf else "pass"
    except Exception:
        verdicts[ReaderCode.LEAF_RECORD] = "error"
        verdicts[ReaderCode.LEAF_PROCESS] = "error"

    factory_records: list[Any] = []
    live_factory = False
    try:
        from .factory_state import (
            factory_process_is_alive,
            load_factory_records_for_issue,
        )

        factory_records = load_factory_records_for_issue(home, issue_id)
        live_factory = any(factory_process_is_alive(record) for record in factory_records)
        verdicts[ReaderCode.FACTORY_RECORD] = "pass"
        verdicts[ReaderCode.FACTORY_PROCESS] = "blocked" if live_factory else "pass"
    except Exception:
        verdicts[ReaderCode.FACTORY_RECORD] = "error"
        verdicts[ReaderCode.FACTORY_PROCESS] = "error"

    factory_status = factory_records[0].status if factory_records else None
    if ReaderCode.FACTORY_STATUS in required:
        try:
            if factory_records:
                from .backends.feature_factory import FeatureFactoryBackend
                from .backends.registry import BackendRegistry, WorklinkConfig

                backend = BackendRegistry(WorklinkConfig.load(home / "worklink.yaml")).get(
                    "feature_factory"
                )
                if not isinstance(backend, FeatureFactoryBackend):
                    raise RuntimeError("factory backend is unavailable")
                record = factory_records[0]
                factory_status = backend.status(
                    record.run_id,
                    sandbox=Path(record.sandbox),
                    launcher=record.launcher,
                )
            verdicts[ReaderCode.FACTORY_STATUS] = "pass"
        except Exception:
            verdicts[ReaderCode.FACTORY_STATUS] = "error"

    # Both owner inventories are mandatory before claiming that no executor or
    # lock conflicts with clearance.
    try:
        owner_conflict = live_leaf or live_factory
        verdicts[ReaderCode.CLAIM_OWNERS] = "blocked" if owner_conflict else "pass"
    except Exception:
        verdicts[ReaderCode.CLAIM_OWNERS] = "error"
    try:
        response = run(["chainlink", "locks", "list", "--json"])
        if response.returncode != 0:
            raise RuntimeError("lock inventory failed")
        lock_data = json.loads(response.stdout or "{}")
        lock_ids = _strict_lock_ids(lock_data)
        verdicts[ReaderCode.CHAINLINK_LOCKS] = (
            "blocked" if issue_id in lock_ids else "pass"
        )
    except Exception:
        verdicts[ReaderCode.CHAINLINK_LOCKS] = "error"

    evidence: Mapping[str, object] | None = None
    evidence_path = _occurrence_evidence_path(occurrence, issue_state)
    try:
        if evidence_path is not None:
            evidence = _read_strict_json(evidence_path)
            if int(evidence.get("issue_id", 0)) != issue_id:
                raise RuntimeError("evidence issue mismatch")
        verdicts[ReaderCode.EVIDENCE] = (
            "pass"
            if _evidence_is_success(evidence)
            or (evidence is None and kind == AttentionKind.START)
            or (evidence is None and SOURCE_RULES[source].clearance == ClearancePolicy.VALIDATOR)
            else "blocked"
        )
    except Exception:
        verdicts[ReaderCode.EVIDENCE] = "error"

    pr_url = (
        str(evidence.get("pr_url"))
        if evidence is not None and evidence.get("pr_url")
        else _occurrence_pr_url(occurrence, issue_state)
    )
    try:
        if pr_url is None:
            if evidence is not None and evidence.get("pr_url") is not None:
                raise RuntimeError("evidence PR is malformed")
            verdicts[ReaderCode.PULL_REQUEST] = "pass"
        else:
            response = run([
                "gh", "pr", "view", pr_url,
                "--json", "state,headRefOid,baseRefName,url",
            ])
            if response.returncode != 0:
                raise RuntimeError("pull request read failed")
            pr = json.loads(response.stdout or "{}")
            expected_head = evidence.get("head_sha") if evidence else None
            if (
                not isinstance(pr, dict)
                or pr.get("url") != pr_url
                or pr.get("state") not in {"OPEN", "MERGED"}
                or (expected_head is not None and pr.get("headRefOid") != expected_head)
            ):
                verdicts[ReaderCode.PULL_REQUEST] = "blocked"
            else:
                verdicts[ReaderCode.PULL_REQUEST] = "pass"
    except Exception:
        verdicts[ReaderCode.PULL_REQUEST] = "error"

    if ReaderCode.VALIDATOR in required:
        validator = (validators or {}).get(source)
        if validator is None:
            verdicts[ReaderCode.VALIDATOR] = "error"
        else:
            try:
                verdicts[ReaderCode.VALIDATOR] = (
                    "pass" if validator(dict(occurrence["facts"])) else "blocked"
                )
            except Exception:
                verdicts[ReaderCode.VALIDATOR] = "error"

    reservation = issue_state["reservations"][occurrence["reservation_id"]]
    witness_id = None
    if kind == AttentionKind.START:
        if reservation["state"] == "active":
            lifecycle = "current"
        elif reservation["disposition"] == "success":
            lifecycle = "superseded"
        else:
            lifecycle = "changed"
        for reader in required:
            if verdicts.get(reader) not in {"error", "blocked"}:
                verdicts[reader] = lifecycle
    elif kind == AttentionKind.SUCCESS:
        witness_id = str(occurrence["facts"]["witness_id"])
        strict_success = (
            witness_id in issue_state["success_witnesses"]
            and verdicts.get(ReaderCode.EVIDENCE) == "pass"
            and verdicts.get(ReaderCode.PULL_REQUEST) == "pass"
        )
        lifecycle = "current" if strict_success else "changed"
        for reader in required:
            if verdicts.get(reader) not in {"error", "blocked"}:
                verdicts[reader] = lifecycle

    return inspect_clearance(
        occurrence,
        verdicts,
        witness_id=witness_id,
    )


def _strict_subprocess_runner(home: Path) -> Any:
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=home,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    return run


def _strict_leaf_record(home: Path, issue_id: int) -> Any:
    from .run_state import WorklinkRunState, runs_dir

    path = runs_dir(home) / f"{issue_id}.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    payload = _read_strict_json(path)
    state = WorklinkRunState.from_json(payload)
    if state.issue_id != issue_id:
        raise RuntimeError("leaf record identity mismatch")
    return state


def _read_strict_json(path: Path) -> Mapping[str, object]:
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode) or value.st_size > 4 * 1024 * 1024:
        raise RuntimeError("required JSON is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        raw = os.read(fd, 4 * 1024 * 1024 + 1)
    finally:
        os.close(fd)
    if len(raw) > 4 * 1024 * 1024 or b"\x00" in raw:
        raise RuntimeError("required JSON exceeds bounds")
    parsed = json.loads(raw.decode("utf-8", "strict"))
    if not isinstance(parsed, dict):
        raise RuntimeError("required JSON is not an object")
    return parsed


def _strict_lock_ids(value: object) -> set[int]:
    rows = value.get("locks", value if isinstance(value, list) else None) if isinstance(value, dict) else value
    if isinstance(rows, dict):
        iterable = rows.items()
    elif isinstance(rows, list):
        iterable = enumerate(rows)
    else:
        raise RuntimeError("lock inventory is malformed")
    result: set[int] = set()
    for key, row in iterable:
        raw = row.get("issue_id") if isinstance(row, dict) else key
        if type(raw) not in {int, str}:
            raise RuntimeError("lock identity is malformed")
        result.add(int(raw))
    return result


def _occurrence_evidence_path(
    occurrence: Mapping[str, object], issue_state: Mapping[str, Any]
) -> Path | None:
    facts = occurrence["facts"]
    raw = facts.get("evidence_id") if isinstance(facts, dict) else None
    if isinstance(raw, str) and Path(raw).is_absolute():
        return Path(raw)
    if occurrence["kind"] == "success" and isinstance(facts, dict):
        raw = facts.get("evidence_path")
        if isinstance(raw, str) and Path(raw).is_absolute():
            return Path(raw)
    return None


def _occurrence_pr_url(
    occurrence: Mapping[str, object], issue_state: Mapping[str, Any]
) -> str | None:
    facts = occurrence["facts"]
    raw = facts.get("pr_url") if isinstance(facts, dict) else None
    return raw if isinstance(raw, str) and raw.startswith("https://") else None


def _evidence_is_success(evidence: Mapping[str, object] | None) -> bool:
    return bool(
        evidence is not None
        and evidence.get("status") == "completed"
        and isinstance(evidence.get("head_sha"), str)
        and re.fullmatch(r"[0-9a-f]{40,64}", str(evidence["head_sha"]))
    )


@dataclass(frozen=True)
class ClaimIdentity:
    issue_id: int
    attempt: int
    agent_id: str
    claimed_at: str

    def __post_init__(self) -> None:
        if self.issue_id <= 0 or self.attempt <= 0:
            raise AttentionSchemaError("claim identity numbers must be positive")
        _bounded_text(self.agent_id, "agent_id", 512)
        _bounded_text(self.claimed_at, "claimed_at", 128)

    @property
    def key(self) -> str:
        return f"{self.issue_id}:{self.attempt}:{self.agent_id}:{self.claimed_at}"

    @classmethod
    def from_json(cls, value: object) -> ClaimIdentity:
        data = _exact_dict(value, {"issue_id", "attempt", "agent_id", "claimed_at"}, "claim")
        return cls(
            issue_id=_integer(data["issue_id"], "claim issue_id", positive=True),
            attempt=_integer(data["attempt"], "claim attempt", positive=True),
            agent_id=_string(data["agent_id"], "claim agent_id"),
            claimed_at=_string(data["claimed_at"], "claim claimed_at"),
        )

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Accounting:
    scope: AccountingScope
    claim: ClaimIdentity | None
    consumed: bool | None
    settlement_key: str | None

    def __post_init__(self) -> None:
        if self.scope == AccountingScope.NO_NEW_CLAIM:
            if self.claim is not None or self.settlement_key is not None:
                raise AttentionSchemaError("no-new-claim accounting cannot bind a claim")
        elif self.scope == AccountingScope.BOUND_CLAIM:
            if self.claim is None or self.consumed is None or not self.settlement_key:
                raise AttentionSchemaError("bound accounting requires claim and settlement")
        elif self.scope == AccountingScope.DEFERRED:
            if self.consumed is not None or self.settlement_key is not None:
                raise AttentionSchemaError("deferred accounting cannot be settled")
        elif self.scope == AccountingScope.UNKNOWN:
            if self.consumed is not None:
                raise AttentionSchemaError("legacy accounting consumption is unknown")

    def to_json(self) -> dict[str, object]:
        return {
            "scope": self.scope.value,
            "claim": self.claim.to_json() if self.claim else None,
            "consumed": self.consumed,
            "settlement_key": self.settlement_key,
        }

    @classmethod
    def from_json(cls, value: object) -> Accounting:
        data = _exact_dict(value, {"scope", "claim", "consumed", "settlement_key"}, "accounting")
        claim = None if data["claim"] is None else ClaimIdentity.from_json(data["claim"])
        consumed = data["consumed"]
        if consumed is not None and type(consumed) is not bool:
            raise AttentionSchemaError("accounting consumed must be boolean or null")
        settlement = data["settlement_key"]
        if settlement is not None:
            settlement = _string(settlement, "settlement_key")
        return cls(AccountingScope(data["scope"]), claim, consumed, settlement)


@dataclass(frozen=True)
class InputFacts:
    repository: str | None = None
    config: str | None = None
    inventory: str | None = None
    template: str | None = None
    issue_snapshot_sha256: str | None = None
    validator: str | None = None
    result: str | None = None
    type: str = "input"


@dataclass(frozen=True)
class ClaimFacts:
    intended: ClaimIdentity | None
    confirmed: ClaimIdentity | None
    result: str
    lock_identity: str | None = None
    command_operation: str | None = None
    return_code: int | None = None
    mutation_stage: str | None = None
    history_read: str | None = None
    type: str = "claim"


@dataclass(frozen=True)
class LaunchFacts:
    executable: str | None
    compute: str | None
    checkout: str | None
    operation: str
    returned_handle: bool | None
    pid: int | None
    start_ticks: int | None
    launch_result: str
    state_save_result: str | None
    type: str = "launch"


@dataclass(frozen=True)
class LeafFacts:
    backend: str | None
    checkout: str | None
    base: str | None
    branch: str | None
    isolated: bool | None
    compute_result: str | None
    backend_status: str | None
    validation_reason_codes: tuple[str, ...]
    evidence_id: str | None
    evidence_sha256: str | None
    pr_url: str | None
    head_sha: str | None
    type: str = "leaf"


@dataclass(frozen=True)
class FactorySnapshot:
    run_id: str | None
    issue_id: int
    attempt: int | None
    sandbox: str | None
    session: str | None
    controller_phase: str | None
    controller_error: str | None
    status: str | None
    valid: bool | None
    lock: object | None
    dead_lock: object | None
    lock_session: str | None
    gates: tuple[Mapping[str, object], ...]
    steps: tuple[Mapping[str, object], ...]
    slices: tuple[Mapping[str, object], ...]
    pr_url: str | None
    next: str | None
    next_present: bool
    park_snapshot: Mapping[str, object] | None
    read_result: str
    type: str = "factory"


@dataclass(frozen=True)
class ReconcileFacts:
    original_run_id: str | None
    original_claim: ClaimIdentity | None
    process_verdict: str
    lock_verdict: str
    publication_id: str | None
    evidence_id: str | None
    automatic_handling_stage: str
    automatic_handling_result: str
    type: str = "reconcile"


@dataclass(frozen=True)
class InterruptFacts:
    interrupted_source: str
    process_verdict: str
    handle_verdict: str
    retained_binding: bool
    positive_proof_ids: tuple[str, ...]
    type: str = "interrupt"


@dataclass(frozen=True)
class LegacyFacts:
    original_key: str
    signature: str | None
    occurrence: str | None
    row: Mapping[str, object]
    type: str = "legacy"


@dataclass(frozen=True)
class LifecycleStartFacts:
    target: AttentionTarget
    claim: ClaimIdentity
    admitted_at: str
    run_id: str | None = None
    sandbox: str | None = None
    admission: str = "confirmed_claim"
    type: str = "lifecycle_start"


@dataclass(frozen=True)
class LifecycleSuccessFacts:
    target: AttentionTarget
    witness_id: str
    completed_at: str
    evidence_path: str
    evidence_sha256: str
    branch: str
    head_sha: str
    pr_url: str | None
    run_id: str | None = None
    sandbox: str | None = None
    next: str | None = None
    next_present: bool = False
    type: str = "lifecycle_success"


FACT_TYPES = {
    "input": InputFacts,
    "claim": ClaimFacts,
    "launch": LaunchFacts,
    "leaf": LeafFacts,
    "factory": FactorySnapshot,
    "reconcile": ReconcileFacts,
    "interrupt": InterruptFacts,
    "legacy": LegacyFacts,
    "lifecycle_start": LifecycleStartFacts,
    "lifecycle_success": LifecycleSuccessFacts,
}

_FACT_FIELDS = {
    "input": {"type", "repository", "config", "inventory", "template", "issue_snapshot_sha256", "validator", "result"},
    "claim": {"type", "intended", "confirmed", "result", "lock_identity", "command_operation", "return_code", "mutation_stage", "history_read"},
    "launch": {"type", "executable", "compute", "checkout", "operation", "returned_handle", "pid", "start_ticks", "launch_result", "state_save_result"},
    "leaf": {"type", "backend", "checkout", "base", "branch", "isolated", "compute_result", "backend_status", "validation_reason_codes", "evidence_id", "evidence_sha256", "pr_url", "head_sha"},
    "factory": {"type", "run_id", "issue_id", "attempt", "sandbox", "session", "controller_phase", "controller_error", "status", "valid", "lock", "dead_lock", "lock_session", "gates", "steps", "slices", "pr_url", "next", "next_present", "park_snapshot", "read_result"},
    "reconcile": {"type", "original_run_id", "original_claim", "process_verdict", "lock_verdict", "publication_id", "evidence_id", "automatic_handling_stage", "automatic_handling_result"},
    "interrupt": {"type", "interrupted_source", "process_verdict", "handle_verdict", "retained_binding", "positive_proof_ids"},
    "legacy": {"type", "original_key", "signature", "occurrence", "row"},
    "lifecycle_start": {"type", "target", "claim", "admitted_at", "run_id", "sandbox", "admission"},
    "lifecycle_success": {"type", "target", "witness_id", "completed_at", "evidence_path", "evidence_sha256", "branch", "head_sha", "pr_url", "run_id", "sandbox", "next", "next_present"},
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROOF_ID = re.compile(
    r"(?:leaf_outcome:[0-9a-f]{64}|factory_partial|factory_merged_slice:[0-9]+|"
    r"factory_step:[0-9]+:(?:accepted|rejected):[1-9][0-9]*|"
    r"(?:leaf|factory)_completion:[0-9a-f]{64})"
)


@dataclass(frozen=True)
class SourceRule:
    causes: frozenset[AttentionCause]
    fact_type: str
    clearance: ClearancePolicy
    work_capable: bool


_CAUSES: dict[AttentionSource, tuple[str, tuple[AttentionCause, ...], ClearancePolicy, bool]] = {}


def _rules(
    names: tuple[AttentionSource, ...], fact_type: str, causes: tuple[AttentionCause, ...],
    clearance: ClearancePolicy, work_capable: bool = False,
) -> None:
    for name in names:
        _CAUSES[name] = (fact_type, causes, clearance, work_capable)


_rules((AttentionSource.QUEUE_LEAF_SPAWN, AttentionSource.QUEUE_FACTORY_SPAWN,
        AttentionSource.STARTUP_LEAF_SPAWN, AttentionSource.STARTUP_FACTORY_SPAWN),
       "launch", (AttentionCause.SPAWN_FAILED,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.LEAF_CLI_REPOSITORY, AttentionSource.FACTORY_CLI_REPOSITORY),
       "input", (AttentionCause.REPOSITORY_UNAVAILABLE,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.LEAF_ISSUE_READ, AttentionSource.FACTORY_ISSUE_READ,
        AttentionSource.REATTACH_ISSUE), "input", (AttentionCause.READ_FAILED,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.LEAF_TARGET_BRANCH, AttentionSource.FACTORY_TARGET_BRANCH),
       "input", (AttentionCause.INVALID_TARGET_BRANCH,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.LEAF_TEMPLATE,), "input", (AttentionCause.TEMPLATE_MISSING,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.LEAF_CONFIG, AttentionSource.LEAF_INVENTORY,
        AttentionSource.LEAF_REPOSITORY, AttentionSource.LEAF_BACKEND,
        AttentionSource.LEAF_COMPUTE, AttentionSource.FACTORY_CONFIG,
        AttentionSource.FACTORY_BACKEND, AttentionSource.FACTORY_REPOSITORY,
        AttentionSource.FACTORY_COMPUTE, AttentionSource.FACTORY_INVENTORY),
       "input", (AttentionCause.CONFIGURATION_INVALID,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.FACTORY_INTERLOCK,), "claim", (AttentionCause.INTERLOCK_UNAVAILABLE,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.FACTORY_ISSUE_KIND,), "input", (AttentionCause.NOT_EPIC,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.FACTORY_LAUNCHER,), "input", (AttentionCause.LAUNCHER_UNAVAILABLE,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.FACTORY_BASE_LOOKUP,), "input",
       (AttentionCause.BASE_MISSING, AttentionCause.BASE_READ_FAILED), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.FACTORY_WORK_ITEM,), "input", (AttentionCause.WORK_ITEM_INVALID,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.FACTORY_RETAINED_BINDING, AttentionSource.FACTORY_RECOVERY_BINDING),
       "factory", (AttentionCause.RECOVERY_BINDING_INVALID,), ClearancePolicy.VALIDATOR, True)
_rules((AttentionSource.CLAIM_COMMAND,), "claim", (AttentionCause.CLAIM_COMMAND_FAILED,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.CLAIM_GUARD,), "claim", (AttentionCause.OWNER_READ_FAILED,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.CLAIM_STEAL,), "claim", (AttentionCause.STEAL_FAILED,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.CLAIM_BUDGET,), "claim", (AttentionCause.ATTEMPTS_EXHAUSTED,), ClearancePolicy.SUCCESSOR_LEAF)
_rules((AttentionSource.CLAIM_CAPACITY_READ,), "claim", (AttentionCause.LOCK_INVENTORY_FAILED,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.CLAIM_UNREADY, AttentionSource.CLAIM_INPROGRESS, AttentionSource.CLAIM_COMMENT),
       "claim", (AttentionCause.CLAIM_PUBLICATION_FAILED,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.CLAIM_CONTENTION,), "claim", (AttentionCause.CONTENTION_EXHAUSTED,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.LEAF_CHECKOUT_CREATE,), "leaf", (AttentionCause.CHECKOUT_FAILED,), ClearancePolicy.SUCCESSOR_LEAF, True)
_rules((AttentionSource.LEAF_PUBLICATION_CAPTURE,), "leaf", (AttentionCause.PUBLICATION_BOUNDARY_FAILED,), ClearancePolicy.SUCCESSOR_LEAF)
_rules((AttentionSource.LEAF_CHECKOUT_ISOLATION,), "leaf", (AttentionCause.UNSAFE_CHECKOUT,), ClearancePolicy.VALIDATOR)
_rules((AttentionSource.LEAF_DIRTY_SNAPSHOT,), "leaf", (AttentionCause.CHECKOUT_READ_FAILED,), ClearancePolicy.SUCCESSOR_LEAF)
_rules((AttentionSource.LEAF_PROMPT, AttentionSource.REATTACH_PROMPT), "input", (AttentionCause.PROMPT_RENDER_FAILED,), ClearancePolicy.SUCCESSOR_LEAF, True)
_rules((AttentionSource.LEAF_WORK_SPEC, AttentionSource.REATTACH_SPEC, AttentionSource.FACTORY_SPEC,
        AttentionSource.FACTORY_RECOVERY_SPEC), "launch", (AttentionCause.WORK_SPEC_FAILED,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.LEAF_REPORT_SETUP,), "launch", (AttentionCause.REPORT_SETUP_FAILED,), ClearancePolicy.RECOVERY)
_rules((AttentionSource.LEAF_COMPUTE_LAUNCH, AttentionSource.FACTORY_LAUNCH,
        AttentionSource.FACTORY_RECOVERY_LAUNCH), "launch", (AttentionCause.LAUNCH_FAILED,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.LEAF_HANDLE_SAVE, AttentionSource.FACTORY_HANDLE_SAVE,
        AttentionSource.FACTORY_RECOVERY_SAVE, AttentionSource.LEAF_CLAIM_PREPARATION,
        AttentionSource.FACTORY_OBSERVATION_SAVE), "launch", (AttentionCause.STATE_WRITE_FAILED,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.LEAF_COMPUTE_WAIT, AttentionSource.REATTACH_WAIT), "leaf", (AttentionCause.WORKER_FAILED,), ClearancePolicy.SUCCESSOR_LEAF, True)
_rules((AttentionSource.LEAF_INTERPRET,), "leaf", (AttentionCause.INTERPRETATION_FAILED,), ClearancePolicy.SUCCESSOR_LEAF, True)
_rules((AttentionSource.LEAF_TEST_REPORT, AttentionSource.LEAF_PR_BODY), "leaf", (AttentionCause.EVIDENCE_READ_FAILED,), ClearancePolicy.SUCCESSOR_LEAF, True)
_rules((AttentionSource.LEAF_GATE, AttentionSource.LEAF_REGATE), "leaf", (AttentionCause.VALIDATION_FAILED,), ClearancePolicy.SUCCESSOR_LEAF, True)
_rules((AttentionSource.LEAF_EVIDENCE_WRITE,), "leaf", (AttentionCause.EVIDENCE_WRITE_FAILED,), ClearancePolicy.SUCCESSOR_LEAF, True)
_rules((AttentionSource.LEAF_COMMIT, AttentionSource.LEAF_PUBLICATION_FENCE,
        AttentionSource.LEAF_PUSH, AttentionSource.LEAF_PR_OPEN), "leaf", (AttentionCause.PUBLICATION_FAILED,), ClearancePolicy.SUCCESSOR_LEAF, True)
_rules((AttentionSource.LEAF_EVIDENCE_COMMENT,), "leaf", (AttentionCause.BOOKKEEPING_FAILED,), ClearancePolicy.SUCCESSOR_LEAF, True)
_rules((AttentionSource.LEAF_BACKEND_OUTCOME,), "leaf",
       (AttentionCause.BACKEND_BLOCKED, AttentionCause.BACKEND_FAILED, AttentionCause.VALIDATION_FAILED), ClearancePolicy.SUCCESSOR_LEAF, True)
_rules((AttentionSource.LEAF_TERMINAL_LABELS, AttentionSource.LEAF_TERMINAL_RELEASE,
        AttentionSource.REATTACH_PR, AttentionSource.FACTORY_TERMINAL_LABELS,
        AttentionSource.FACTORY_TERMINAL_RELEASE), "reconcile", (AttentionCause.TERMINAL_ROUTING_FAILED,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.LEAF_INTERRUPT, AttentionSource.FACTORY_INTERRUPT), "interrupt", (AttentionCause.INTERRUPTED,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.REATTACH_STATE,), "reconcile", (AttentionCause.STATE_MISSING, AttentionCause.STATE_UNREADABLE), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.REATTACH_SHIM,), "reconcile", (AttentionCause.WORKER_INTERRUPTED, AttentionCause.IDENTITY_STALE, AttentionCause.CLEANUP_FAILED), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.REATTACH_BACKEND,), "leaf", (AttentionCause.BACKEND_UNAVAILABLE,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.REATTACH_COMPUTE,), "leaf", (AttentionCause.NOT_RESUMABLE,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.REATTACH_CHECKOUT,), "leaf", (AttentionCause.CHECKOUT_FAILED,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.LEAF_STARTUP_RECONCILE, AttentionSource.FACTORY_STARTUP_RECONCILE),
       "reconcile", (AttentionCause.CONTROLLER_LOST,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.FACTORY_CHECKOUT,), "factory", (AttentionCause.CHECKOUT_FAILED,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_GIT_IDENTITY, AttentionSource.FACTORY_PUBLISHING_IDENTITY,
        AttentionSource.FACTORY_CREDENTIAL), "factory", (AttentionCause.IDENTITY_UNAVAILABLE,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_IDENTITY_VERIFY,), "factory", (AttentionCause.IDENTITY_MISMATCH,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_LAUNCH_BINDING,), "factory", (AttentionCause.RECOVERY_BINDING_INVALID,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_SANDBOX,), "factory", (AttentionCause.SANDBOX_FAILED,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_PERMISSIONS,), "factory", (AttentionCause.PERMISSIONS_FAILED,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_RECOVERY_STATUS,), "factory", (AttentionCause.RECOVERY_STATE_INVALID,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.FACTORY_RECOVERY_LOCK,), "factory", (AttentionCause.LOCK_RECONCILIATION_FAILED,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.FACTORY_RESUME,), "factory", (AttentionCause.RESUME_FAILED,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.FACTORY_RECOVERY_REPOSITORY,), "factory", (AttentionCause.REPOSITORY_CHANGED,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.FACTORY_WAIT_START,), "factory", (AttentionCause.SUPERVISION_FAILED,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_STATUS_READ,), "factory", (AttentionCause.STATUS_READ_FAILED,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_STARTUP_DEADLINE,), "factory", (AttentionCause.STARTUP_TIMEOUT,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_RUN_DEADLINE,), "factory", (AttentionCause.RUN_TIMEOUT,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_STATUS_BINDING,), "factory", (AttentionCause.STATUS_BINDING_INVALID,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_OWNER,), "factory", (AttentionCause.OWNER_CHANGED,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_HEARTBEAT,), "factory", (AttentionCause.HEARTBEAT_FAILED,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_WAIT_DRAIN,), "factory", (AttentionCause.RESULT_UNAVAILABLE,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_DRIVER_EXIT,), "factory", (AttentionCause.UNFINISHED_EXIT,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_TRANSCRIPT,), "factory", (AttentionCause.TRANSCRIPT_FAILED,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_CLEANUP,), "factory", (AttentionCause.CLEANUP_FAILED,), ClearancePolicy.RECOVERY, True)
_rules((AttentionSource.FACTORY_PARKED,), "factory", (AttentionCause.NEEDS_HUMAN,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_BLOCKED,), "factory", (AttentionCause.BLOCKED,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_PARTIAL,), "factory", (AttentionCause.PARTIAL,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.FACTORY_COMPLETION_VERIFY,), "factory", (AttentionCause.COMPLETION_INVALID,), ClearancePolicy.SUCCESSOR_FACTORY, True)
_rules((AttentionSource.LEAF_START, AttentionSource.FACTORY_START), "lifecycle_start", (), ClearancePolicy.LIFECYCLE)
_rules((AttentionSource.LEAF_SUCCESS, AttentionSource.FACTORY_SUCCESS), "lifecycle_success", (), ClearancePolicy.LIFECYCLE, True)
_rules((AttentionSource.LEGACY_V1,), "legacy", (AttentionCause.LEGACY_UNKNOWN,), ClearancePolicy.LEGACY)

SOURCE_RULES: Mapping[AttentionSource, SourceRule] = {
    source: SourceRule(frozenset(causes), facts, clearance, work)
    for source, (facts, causes, clearance, work) in _CAUSES.items()
}
if set(SOURCE_RULES) != set(AttentionSource):  # pragma: no cover - import invariant
    missing = sorted(source.value for source in set(AttentionSource) - set(SOURCE_RULES))
    raise RuntimeError(f"attention source rules incomplete: {missing}")


def validate_occurrence_contract(
    *, kind: AttentionKind, source: AttentionSource, cause: AttentionCause | None,
    facts: Mapping[str, object], accounting: Accounting, proof_ids: tuple[str, ...] = (),
) -> None:
    """Validate the closed source/cause/facts/accounting matrix."""
    rule = SOURCE_RULES[source]
    fact_type = facts.get("type")
    if fact_type != rule.fact_type:
        raise AttentionSchemaError(
            f"{source.value} requires {rule.fact_type} facts, got {fact_type!r}"
        )
    if set(facts) != _FACT_FIELDS[rule.fact_type]:
        raise AttentionSchemaError(f"{rule.fact_type} facts fields are invalid")
    _json_safe_mapping(facts, "facts")
    lifecycle_kind = (
        AttentionKind.START if source in {AttentionSource.LEAF_START, AttentionSource.FACTORY_START}
        else AttentionKind.SUCCESS if source in {AttentionSource.LEAF_SUCCESS, AttentionSource.FACTORY_SUCCESS}
        else AttentionKind.LEGACY_ATTENTION if source == AttentionSource.LEGACY_V1
        else AttentionKind.ATTENTION
    )
    if kind != lifecycle_kind:
        raise AttentionSchemaError(f"{source.value} cannot produce {kind.value}")
    if kind in {AttentionKind.START, AttentionKind.SUCCESS}:
        if cause is not None:
            raise AttentionSchemaError("lifecycle occurrences cannot have a cause")
    elif cause is None or cause not in rule.causes:
        raise AttentionSchemaError(f"illegal cause for {source.value}")
    if kind == AttentionKind.START:
        if accounting != Accounting(AccountingScope.NO_NEW_CLAIM, None, None, None):
            raise AttentionSchemaError("START cannot charge work")
        if proof_ids:
            raise AttentionSchemaError("START cannot carry work proof")
    if kind == AttentionKind.SUCCESS and (
        accounting.scope != AccountingScope.BOUND_CLAIM or accounting.consumed is not True
    ):
        raise AttentionSchemaError("success requires a consuming original-work settlement")
    if accounting.consumed is True and not proof_ids:
        raise AttentionSchemaError("consumption requires positive proof")
    if len(proof_ids) != len(set(proof_ids)) or not all(
        isinstance(proof, str) and _PROOF_ID.fullmatch(proof) for proof in proof_ids
    ):
        raise AttentionSchemaError("positive proof ids are invalid")
    if source == AttentionSource.FACTORY_PARTIAL and accounting.consumed is not True:
        raise AttentionSchemaError("factory partial must consume its proven work claim")
    if kind in {AttentionKind.START, AttentionKind.SUCCESS}:
        expected_target = "leaf" if source.value.startswith("leaf_") else "factory"
        target = facts.get("target")
        target_value = target.value if isinstance(target, AttentionTarget) else target
        if target_value != expected_target:
            raise AttentionSchemaError("lifecycle source and target do not match")
    if fact_type == "lifecycle_success":
        if not isinstance(facts.get("evidence_path"), str) or not Path(str(facts["evidence_path"])).is_absolute():
            raise AttentionSchemaError("lifecycle success evidence path is invalid")
        if not isinstance(facts.get("evidence_sha256"), str) or _SHA256.fullmatch(str(facts["evidence_sha256"])) is None:
            raise AttentionSchemaError("lifecycle success evidence hash is invalid")
        if type(facts.get("next_present")) is not bool:
            raise AttentionSchemaError("lifecycle success next presence is invalid")


def facts_to_json(value: object) -> dict[str, object]:
    if not hasattr(value, "type") or getattr(value, "type") not in FACT_TYPES:
        raise AttentionSchemaError("facts must be a typed facts variant")
    data = asdict(value)
    return _json_safe_mapping(data, "facts")


def positive_factory_proofs(snapshot: FactorySnapshot) -> tuple[str, ...]:
    """Derive only structured, completed factory work proofs.

    Gates, running/blocked steps, bare PRs, and attempt allocation do not prove
    work.  Accepted/rejected steps with a positive attempt and merged slices do.
    """
    proofs: list[str] = []
    for index, step in enumerate(snapshot.steps):
        status = step.get("status")
        attempts = step.get("attempts")
        if status in {"accepted", "rejected"} and type(attempts) is int and attempts > 0:
            proofs.append(f"factory_step:{index}:{status}:{attempts}")
    for index, slice_ in enumerate(snapshot.slices):
        if slice_.get("status") == "merged":
            proofs.append(f"factory_merged_slice:{index}")
    if snapshot.status == "partial":
        proofs.append("factory_partial")
    return tuple(proofs)


def require_absolute_evidence(path: str) -> None:
    if not Path(path).is_absolute() or "\x00" in path:
        raise AttentionSchemaError("evidence path must be absolute")


def _exact_dict(value: object, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise AttentionSchemaError(f"{name} fields are invalid")
    return value


def _bounded_text(value: str, name: str, maximum: int = 4096) -> None:
    if not isinstance(value, str) or not value or "\x00" in value or len(value.encode()) > maximum:
        raise AttentionSchemaError(f"{name} is invalid")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise AttentionSchemaError(f"{name} must be a string")
    _bounded_text(value, name)
    return value


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or (positive and value <= 0):
        raise AttentionSchemaError(f"{name} must be an integer")
    return value


def _json_safe_mapping(value: Mapping[str, object], name: str) -> dict[str, object]:
    def visit(item: object, depth: int = 0) -> object:
        if depth > 12:
            raise AttentionSchemaError(f"{name} exceeds nesting limit")
        if item is None or type(item) in {bool, int, str}:
            if isinstance(item, str) and ("\x00" in item or len(item.encode()) > 65536):
                raise AttentionSchemaError(f"{name} contains invalid text")
            return item
        if isinstance(item, float):
            if item != item or item in {float("inf"), float("-inf")}:
                raise AttentionSchemaError(f"{name} contains a non-finite number")
            return item
        if isinstance(item, (list, tuple)):
            if len(item) > 1024:
                raise AttentionSchemaError(f"{name} contains an oversized list")
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, Mapping):
            if len(item) > 1024 or not all(isinstance(key, str) for key in item):
                raise AttentionSchemaError(f"{name} contains an invalid object")
            return {key: visit(child, depth + 1) for key, child in item.items()}
        if isinstance(item, StrEnum):
            return item.value
        raise AttentionSchemaError(f"{name} contains unsupported value {type(item).__name__}")

    return {key: visit(item) for key, item in value.items()}
