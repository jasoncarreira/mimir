"""Typed, closed Worklink outcome and attention contract.

This module intentionally has no imports from the Worklink runtime.  Detached
launchers, the ledger, and the eventual prompt consumer all import these schema
definitions, so importing it must not initialize claims or agent machinery.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
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

    reservation = issue_state["reservations"][occurrence["reservation_id"]]
    binding = reservation["binding"]
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
    exact_factory_record: Any = None
    live_factory = False
    try:
        from .factory_state import (
            factory_process_is_alive,
            load_factory_records_for_issue,
        )

        factory_records = load_factory_records_for_issue(home, issue_id)
        matching = [
            record
            for record in factory_records
            if (binding.get("run_id") is None or record.run_id == binding["run_id"])
            and (binding.get("sandbox") is None or record.sandbox == binding["sandbox"])
            and (
                binding.get("claim") is None
                or record.attempt == binding["claim"].get("attempt")
            )
        ]
        if source.value.startswith("factory_") and binding.get("run_id") is not None:
            if len(matching) == 1:
                exact_factory_record = matching[0]
                verdicts[ReaderCode.FACTORY_RECORD] = "pass"
            else:
                verdicts[ReaderCode.FACTORY_RECORD] = "blocked"
        else:
            exact_factory_record = matching[0] if len(matching) == 1 else None
            verdicts[ReaderCode.FACTORY_RECORD] = "pass"
        live_factory = any(factory_process_is_alive(record) for record in matching)
        verdicts[ReaderCode.FACTORY_PROCESS] = "blocked" if live_factory else "pass"
    except Exception:
        verdicts[ReaderCode.FACTORY_RECORD] = "error"
        verdicts[ReaderCode.FACTORY_PROCESS] = "error"

    factory_status = exact_factory_record.status if exact_factory_record is not None else None
    if ReaderCode.FACTORY_STATUS in required:
        try:
            if exact_factory_record is not None:
                from .backends.feature_factory import FeatureFactoryBackend
                from .backends.registry import BackendRegistry, WorklinkConfig

                backend = BackendRegistry(WorklinkConfig.load(home / "worklink.yaml")).get(
                    "feature_factory"
                )
                if not isinstance(backend, FeatureFactoryBackend):
                    raise RuntimeError("factory backend is unavailable")
                record = exact_factory_record
                factory_status = backend.status(
                    record.run_id,
                    sandbox=Path(record.sandbox),
                    launcher=record.launcher,
                )
                if (
                    factory_status.run_id != record.run_id
                    or factory_status.sandbox_path != record.sandbox
                ):
                    raise RuntimeError("factory status binding mismatch")
            verdicts[ReaderCode.FACTORY_STATUS] = (
                "pass" if exact_factory_record is not None else "blocked"
            )
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
        lock_present = issue_id in lock_ids
        verdicts[ReaderCode.CHAINLINK_LOCKS] = "blocked" if lock_present else "pass"
    except Exception:
        verdicts[ReaderCode.CHAINLINK_LOCKS] = "error"

    witness_id, witness, later_witness_id, later_witness = _select_success_witnesses(
        issue_id, occurrence, issue_state
    )
    evidence: Mapping[str, object] | None = None
    try:
        selected = witness if witness is not None else later_witness
        if selected is not None:
            evidence = _read_verified_witness_evidence(issue_id, selected)
            verdicts[ReaderCode.EVIDENCE] = "pass"
        elif kind == AttentionKind.START or SOURCE_RULES[source].clearance == ClearancePolicy.VALIDATOR:
            verdicts[ReaderCode.EVIDENCE] = "pass"
        else:
            verdicts[ReaderCode.EVIDENCE] = "blocked"
    except Exception:
        verdicts[ReaderCode.EVIDENCE] = "error"

    selected_witness = witness if witness is not None else later_witness
    pr_url = selected_witness.get("pr_url") if selected_witness is not None else None
    try:
        if pr_url is None:
            if selected_witness is not None and evidence is not None and evidence.get("pr_url") is not None:
                raise RuntimeError("evidence PR is malformed")
            verdicts[ReaderCode.PULL_REQUEST] = (
                "pass"
                if kind == AttentionKind.START
                or SOURCE_RULES[source].clearance == ClearancePolicy.VALIDATOR
                or selected_witness is not None
                else "blocked"
            )
        else:
            if not isinstance(pr_url, str) or not pr_url.startswith("https://"):
                raise RuntimeError("witness PR is malformed")
            response = run([
                "gh", "pr", "view", pr_url,
                "--json", "state,headRefOid,baseRefName,url",
            ])
            if response.returncode != 0:
                raise RuntimeError("pull request read failed")
            pr = json.loads(response.stdout or "{}")
            expected_head = selected_witness.get("head_sha") if selected_witness else None
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
        try:
            if validator is not None:
                verdicts[ReaderCode.VALIDATOR] = (
                    "pass" if validator(dict(occurrence["facts"])) else "blocked"
                )
            else:
                verdicts[ReaderCode.VALIDATOR] = _run_internal_validator(
                    home=home,
                    source=source,
                    facts=occurrence["facts"],
                    tracker=tracker,
                    factory_record=exact_factory_record,
                    binding=binding,
                    runner=run,
                )
        except Exception:
            verdicts[ReaderCode.VALIDATOR] = "error"

    if kind == AttentionKind.START:
        if witness is not None or later_witness is not None or reservation["disposition"] == "success":
            lifecycle = "superseded"
        elif reservation["state"] == "active" and not _owner_is_verified_dead(
            reservation.get("owner")
        ):
            # The exact live owner and its own issue lock are affirmative START
            # state, not a conflicting owner that changes the historical event.
            lifecycle = "current"
        else:
            lifecycle = "changed"
        for reader in required:
            if verdicts.get(reader) != "error":
                verdicts[reader] = lifecycle
    elif kind == AttentionKind.SUCCESS:
        own_id = str(occurrence["facts"]["witness_id"])
        strict_success = witness_id == own_id and all(
            verdicts.get(reader) == "pass"
            for reader in (ReaderCode.EVIDENCE, ReaderCode.PULL_REQUEST)
        )
        lifecycle = (
            "current" if strict_success
            else "superseded" if later_witness_id is not None
            else "changed"
        )
        for reader in required:
            if verdicts.get(reader) != "error":
                verdicts[reader] = lifecycle

    return inspect_clearance(
        occurrence,
        verdicts,
        witness_id=witness_id,
    )


def _select_success_witnesses(
    issue_id: int,
    occurrence: Mapping[str, object],
    issue_state: Mapping[str, Any],
) -> tuple[str | None, Mapping[str, object] | None, str | None, Mapping[str, object] | None]:
    source = AttentionSource(occurrence["source"])
    target = "factory" if source.value.startswith("factory_") else "leaf"
    reservation = issue_state["reservations"][occurrence["reservation_id"]]
    raw_claim = occurrence["accounting"].get("claim") or reservation["binding"].get("claim")
    attempt = raw_claim.get("attempt") if isinstance(raw_claim, dict) else None
    candidates: list[tuple[int, str, Mapping[str, object]]] = []
    for candidate_id, candidate in issue_state["success_witnesses"].items():
        if candidate.get("issue_id") != issue_id or candidate.get("target") != target:
            continue
        candidate_claim = candidate.get("claim")
        candidate_attempt = (
            candidate_claim.get("attempt") if isinstance(candidate_claim, dict) else None
        )
        if (
            type(attempt) is int
            and type(candidate_attempt) is int
            and candidate_attempt < attempt
        ):
            continue
        candidates.append((candidate_attempt or 2**31, candidate_id, candidate))
    candidates.sort(key=lambda item: (item[0], str(item[2].get("observed_at")), item[1]))
    own_id = (
        occurrence["facts"].get("witness_id")
        if occurrence["kind"] == AttentionKind.SUCCESS.value
        and isinstance(occurrence["facts"], dict)
        else None
    )
    if own_id is None:
        if not candidates:
            return None, None, None, None
        _attempt, candidate_id, candidate = candidates[0]
        return candidate_id, candidate, None, None
    own = issue_state["success_witnesses"].get(own_id)
    later = next(
        (
            (candidate_id, candidate)
            for _attempt, candidate_id, candidate in candidates
            if candidate_id != own_id
        ),
        (None, None),
    )
    return (
        (str(own_id), own) if isinstance(own, dict) else (None, None)
    ) + later


def _owner_is_verified_dead(owner: object) -> bool:
    if not isinstance(owner, dict):
        return False
    pid = owner.get("pid")
    expected_ticks = owner.get("start_ticks")
    if type(pid) is not int or type(expected_ticks) is not int:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        observed_ticks = int(fields[21])
    except (OSError, ValueError, IndexError):
        return False
    return observed_ticks != expected_ticks


def _read_verified_witness_evidence(
    issue_id: int, witness: Mapping[str, object]
) -> Mapping[str, object]:
    path_value = witness.get("evidence_path")
    digest = witness.get("evidence_sha256")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise RuntimeError("witness evidence path is invalid")
    path = Path(path_value)
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode) or value.st_size > 4 * 1024 * 1024:
        raise RuntimeError("witness evidence is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        raw = os.read(fd, 4 * 1024 * 1024 + 1)
    finally:
        os.close(fd)
    import hashlib

    if not isinstance(digest, str) or hashlib.sha256(raw).hexdigest() != digest:
        raise RuntimeError("witness evidence hash mismatch")
    parsed = json.loads(raw.decode("utf-8", "strict"))
    if not isinstance(parsed, dict):
        raise RuntimeError("witness evidence is not an object")
    evidence_issue = parsed.get("issue", parsed.get("issue_id"))
    if evidence_issue != issue_id or parsed.get("status") != "completed":
        raise RuntimeError("witness evidence is not a matching completion")
    for field in ("branch", "head_sha", "pr_url"):
        observed = parsed.get(field)
        expected = witness.get(field)
        if observed is not None and observed != expected:
            raise RuntimeError(f"witness evidence {field} mismatch")
    return parsed


def _run_internal_validator(
    *,
    home: Path,
    source: AttentionSource,
    facts: object,
    tracker: Mapping[str, object] | None,
    factory_record: object | None,
    binding: Mapping[str, object],
    runner: Any,
) -> str:
    """Run the finite read-only validator assigned to a validator source."""
    if SOURCE_RULES[source].clearance != ClearancePolicy.VALIDATOR or not isinstance(facts, dict):
        raise RuntimeError("source has no internal validator")
    if source in {
        AttentionSource.LEAF_ISSUE_READ,
        AttentionSource.FACTORY_ISSUE_READ,
        AttentionSource.REATTACH_ISSUE,
    }:
        return "pass" if tracker is not None else "blocked"
    if source == AttentionSource.FACTORY_ISSUE_KIND:
        labels = tracker.get("labels", []) if tracker is not None else []
        return "pass" if "worklink:epic" in labels else "blocked"
    if source in {AttentionSource.LEAF_TARGET_BRANCH, AttentionSource.FACTORY_TARGET_BRANCH}:
        branch = tracker.get("target_branch") if tracker is not None else None
        if branch is None and tracker is not None and isinstance(
            tracker.get("description"), str
        ):
            from .planning import target_branch_from_description

            branch = target_branch_from_description(str(tracker["description"]))
        return "pass" if isinstance(branch, str) and branch.strip() else "blocked"
    if source in {AttentionSource.FACTORY_RETAINED_BINDING, AttentionSource.FACTORY_RECOVERY_BINDING}:
        return _validate_retained_factory_binding(
            home, facts, tracker, factory_record, binding, runner
        )
    if source in {
        AttentionSource.QUEUE_LEAF_SPAWN,
        AttentionSource.QUEUE_FACTORY_SPAWN,
        AttentionSource.STARTUP_LEAF_SPAWN,
        AttentionSource.STARTUP_FACTORY_SPAWN,
    }:
        executable = facts.get("executable")
        checkout = facts.get("checkout")
        executable_ok = (
            isinstance(executable, str)
            and (Path(executable).is_file() if Path(executable).is_absolute() else True)
        )
        checkout_ok = checkout is None or (
            isinstance(checkout, str) and Path(checkout).is_dir()
        )
        return "pass" if executable_ok and checkout_ok else "blocked"
    path_fields = {
        AttentionSource.LEAF_CLI_REPOSITORY: "repository",
        AttentionSource.FACTORY_CLI_REPOSITORY: "repository",
        AttentionSource.LEAF_TEMPLATE: "template",
        AttentionSource.LEAF_CONFIG: "config",
        AttentionSource.FACTORY_CONFIG: "config",
        AttentionSource.LEAF_INVENTORY: "inventory",
        AttentionSource.FACTORY_INVENTORY: "inventory",
        AttentionSource.LEAF_REPOSITORY: "repository",
        AttentionSource.FACTORY_REPOSITORY: "repository",
    }
    if source in path_fields:
        raw = facts.get(path_fields[source])
        if raw is None and path_fields[source] == "config":
            raw = str(home / "worklink.yaml")
        elif raw is None and path_fields[source] == "inventory":
            raw = str(home / "repositories.yaml")
        elif raw is None and path_fields[source] == "repository":
            raw = os.environ.get("WORKLINK_REPO") or os.environ.get(
                "MIMIR_WORKLINK_REPO"
            )
        elif raw is None and path_fields[source] == "template":
            raw = str(home / "worklink-prompt.md")
        return "pass" if isinstance(raw, str) and Path(raw).exists() else "blocked"
    if source == AttentionSource.LEAF_CHECKOUT_ISOLATION:
        checkout = facts.get("checkout")
        isolated = facts.get("isolated")
        return "pass" if isolated is True and isinstance(checkout, str) and Path(checkout).is_dir() else "blocked"
    if source in {
        AttentionSource.LEAF_BACKEND,
        AttentionSource.LEAF_COMPUTE,
        AttentionSource.FACTORY_BACKEND,
        AttentionSource.FACTORY_COMPUTE,
        AttentionSource.FACTORY_LAUNCHER,
    }:
        from .backends.feature_factory import FactoryContractError, FeatureFactoryBackend
        from .backends.registry import BackendRegistry, WorklinkConfig

        try:
            config = WorklinkConfig.load(home / "worklink.yaml")
            registry = BackendRegistry(config)
            labels = set(tracker.get("labels", ())) if tracker is not None else set()
            if source == AttentionSource.LEAF_BACKEND:
                registry.select(labels=labels, repo=config.repository)
            elif source == AttentionSource.LEAF_COMPUTE:
                registry.select_compute(labels=labels, repo=config.repository)
            elif source == AttentionSource.FACTORY_BACKEND:
                if not isinstance(registry.get("feature_factory"), FeatureFactoryBackend):
                    return "blocked"
            elif source == AttentionSource.FACTORY_COMPUTE:
                if registry.select_compute(labels=labels, repo=config.repository).name != "local_subprocess":
                    return "blocked"
            else:
                backend = registry.get("feature_factory")
                if not isinstance(backend, FeatureFactoryBackend):
                    return "blocked"
                backend.admit()
        except (FactoryContractError, KeyError, ValueError):
            return "blocked"
        return "pass"
    if source == AttentionSource.FACTORY_INTERLOCK:
        from .factory_state import factory_checkout_interlock

        with factory_checkout_interlock(home) as acquired:
            return "pass" if acquired else "blocked"
    if source == AttentionSource.FACTORY_BASE_LOOKUP:
        from .backends.registry import WorklinkConfig
        from .planning import target_branch_from_description

        config = WorklinkConfig.load(home / "worklink.yaml")
        description = tracker.get("description") if tracker is not None else None
        base = (
            target_branch_from_description(description)
            if isinstance(description, str)
            else None
        ) or config.defaults.base_branch
        repo = os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO")
        if not repo:
            return "blocked"
        result = runner([
            "git", "-C", repo, "ls-remote", "--exit-code", "origin",
            f"refs/heads/{base.removeprefix('origin/')}",
        ])
        if result.returncode == 2:
            return "blocked"
        if result.returncode != 0:
            raise RuntimeError("factory base lookup failed")
        return "pass"
    if source == AttentionSource.FACTORY_WORK_ITEM:
        if tracker is None:
            return "blocked"
        from .orchestrator import (
            IssueContext,
            WorklinkError,
            _validate_epic_work_item,
            render_work_item,
        )

        issue = IssueContext(
            issue_id=int(tracker.get("id", tracker.get("number", 0))),
            title=str(tracker.get("title", "")),
            description=str(tracker.get("description", "")),
            labels=set(tracker.get("labels", ())),
            comments=tuple(str(item) for item in tracker.get("comments", ())),
        )
        try:
            _validate_epic_work_item(render_work_item(issue), issue.issue_id)
        except (ValueError, WorklinkError):
            return "blocked"
        return "pass"
    if source == AttentionSource.CLAIM_GUARD:
        return "pass" if tracker is not None else "blocked"
    if source in {
        AttentionSource.CLAIM_COMMAND,
        AttentionSource.CLAIM_STEAL,
        AttentionSource.CLAIM_CAPACITY_READ,
        AttentionSource.CLAIM_CONTENTION,
    }:
        result = runner(["chainlink", "locks", "list", "--json"])
        if result.returncode != 0:
            raise RuntimeError("claim validator lock inventory failed")
        _strict_lock_ids(json.loads(result.stdout or "{}"))
        return "pass"
    if source in {
        AttentionSource.CLAIM_UNREADY,
        AttentionSource.CLAIM_INPROGRESS,
        AttentionSource.CLAIM_COMMENT,
    }:
        if tracker is None:
            return "blocked"
        labels = set(tracker.get("labels", ()))
        if source == AttentionSource.CLAIM_UNREADY:
            return "pass" if "worklink:ready" not in labels else "blocked"
        if source == AttentionSource.CLAIM_INPROGRESS:
            return "pass" if "worklink:in-progress" in labels else "blocked"
        intended = facts.get("intended")
        if not isinstance(intended, dict):
            return "blocked"
        from .claims import claim_records_from_comments

        expected = ClaimIdentity.from_json(intended)
        comments = tuple(str(item) for item in tracker.get("comments", ()))
        return "pass" if any(
            record.issue_id == expected.issue_id
            and record.attempt == expected.attempt
            and record.agent_id == expected.agent_id
            and record.claimed_at.isoformat() == expected.claimed_at
            for record in claim_records_from_comments(comments)
        ) else "blocked"
    raise RuntimeError(f"no read-only validator implemented for {source.value}")


_RETAINED_BINDING_MEMBERS = frozenset({
    "issue", "repository", "controller_repository", "base", "launcher", "lifecycle",
    "session", "ownership_boundary", "sandbox", "checkout_root", "git_directory",
    "checkout_repository", "branch", "base_object", "head_object", "claim_issue",
    "claim_owner", "claim_lock",
})


def _validate_retained_factory_binding(
    home: Path,
    facts: Mapping[str, object],
    tracker: Mapping[str, object] | None,
    factory_record: object | None,
    binding: Mapping[str, object],
    runner: Any,
) -> str:
    read_result = facts.get("read_result")
    if not isinstance(read_result, str) or not read_result.startswith("failed:"):
        raise RuntimeError("retained binding occurrence lacks its failed member")
    member = read_result.removeprefix("failed:")
    if member not in _RETAINED_BINDING_MEMBERS:
        raise RuntimeError("retained binding member is unknown")
    if tracker is None or factory_record is None:
        return "blocked"
    from .orchestrator import (
        IssueContext,
        WorklinkRunner,
        FactoryRecoveryBindingError,
        _list_runner,
        _repo_remote_url,
        _repo_slug_from_url,
        _verify_factory_recovery_target,
    )
    repo_value = os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO")
    repo = Path(repo_value) if repo_value else home
    issue = IssueContext(
        issue_id=int(tracker.get("id", tracker.get("number", 0))),
        title=str(tracker.get("title", "")),
        description=str(tracker.get("description", "")),
        labels=set(tracker.get("labels", ())),
        comments=tuple(str(item) for item in tracker.get("comments", ())),
    )
    if not member.startswith("claim_"):
        if member in {"repository", "controller_repository"} and not repo_value:
            return "blocked"
        launcher = Path(getattr(factory_record, "launcher"))
        repo_slug = str(getattr(factory_record, "repository"))
        base = str(getattr(factory_record, "base_ref"))
        if member == "launcher":
            from .backends.feature_factory import FeatureFactoryBackend
            from .backends.registry import BackendRegistry, WorklinkConfig

            backend = BackendRegistry(WorklinkConfig.load(home / "worklink.yaml")).get(
                "feature_factory"
            )
            if not isinstance(backend, FeatureFactoryBackend):
                return "blocked"
            launcher = backend.admit()
        elif member == "repository":
            observed_slug = _repo_slug_from_url(_repo_remote_url(repo, runner=runner))
            if observed_slug is None:
                return "blocked"
            repo_slug = observed_slug
        elif member == "base":
            from .backends.registry import WorklinkConfig
            from .planning import target_branch_from_description

            config = WorklinkConfig.load(home / "worklink.yaml")
            base = target_branch_from_description(issue.description) or config.defaults.base_branch
        try:
            _verify_factory_recovery_target(
                runner=WorklinkRunner(home=home, repo=repo, runner=runner),
                issue=issue,
                retained=factory_record,
                launcher=launcher,
                repo_slug=repo_slug,
                base=base,
                command_runner=runner,
                member=member,
            )
        except FactoryRecoveryBindingError as exc:
            if exc.reader_error:
                raise
            return "blocked"
    else:
        raw_claim = binding.get("claim")
        if not isinstance(raw_claim, dict):
            return "blocked"
        claim = ClaimIdentity.from_json(raw_claim)
        if member == "claim_issue":
            return "pass" if claim.issue_id == issue.issue_id else "blocked"
        from .claims import ChainlinkClaims, ClaimRecord

        configured_runner = WorklinkRunner(home=home, repo=repo, runner=runner)
        if member == "claim_owner":
            return "pass" if claim.agent_id == configured_runner.agent_id else "blocked"
        claims = ChainlinkClaims(
            agent_id=configured_runner.agent_id,
            runner=_list_runner(runner),
            home_path=home,
        )
        retained_claim = ClaimRecord(
            issue_id=claim.issue_id,
            attempt=claim.attempt,
            agent_id=claim.agent_id,
            claimed_at=datetime.fromisoformat(claim.claimed_at),
        )
        return "pass" if getattr(claims, "_lock_still_held_by")(retained_claim) else "blocked"
    return "pass"


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
        _timestamp(self.claimed_at, "claimed_at")

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
            if (
                self.claim is not None
                or self.settlement_key is not None
                or self.consumed not in {None, False}
            ):
                raise AttentionSchemaError("no-new-claim accounting cannot bind a claim")
        elif self.scope == AccountingScope.BOUND_CLAIM:
            if self.claim is None or self.consumed is None or not self.settlement_key:
                raise AttentionSchemaError("bound accounting requires claim and settlement")
        elif self.scope == AccountingScope.DEFERRED:
            if self.consumed is not None or self.settlement_key is not None:
                raise AttentionSchemaError("deferred accounting cannot be settled")
        elif self.scope == AccountingScope.UNKNOWN:
            if self.claim is not None or self.consumed is not None or self.settlement_key is not None:
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
    _validate_fact_semantics(rule.fact_type, facts)
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


def _optional_text(value: object, name: str, maximum: int = 4096) -> None:
    if value is not None:
        _bounded_text(value, name, maximum)  # type: ignore[arg-type]


def _optional_bool(value: object, name: str) -> None:
    if value is not None and type(value) is not bool:
        raise AttentionSchemaError(f"{name} must be boolean or null")


def _optional_integer(value: object, name: str, *, positive: bool = False) -> None:
    if value is not None:
        _integer(value, name, positive=positive)


def _optional_claim(value: object, name: str) -> ClaimIdentity | None:
    if value is None:
        return None
    try:
        return ClaimIdentity.from_json(value)
    except AttentionSchemaError as exc:
        raise AttentionSchemaError(f"{name} is invalid") from exc


def _timestamp(value: object, name: str) -> None:
    text = _string(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttentionSchemaError(f"{name} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AttentionSchemaError(f"{name} must include a timezone")


def _validate_fact_semantics(fact_type: str, facts: Mapping[str, object]) -> None:
    """Validate values, not only keys, for every member of the facts union."""
    if fact_type == "input":
        for field in ("repository", "config", "inventory", "template", "validator", "result"):
            _optional_text(facts[field], f"input {field}")
        digest = facts["issue_snapshot_sha256"]
        if digest is not None and (
            not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
        ):
            raise AttentionSchemaError("input issue snapshot hash is invalid")
        if not any(
            facts[field] is not None
            for field in ("repository", "config", "inventory", "template", "validator")
        ):
            raise AttentionSchemaError("input facts do not identify the read boundary")
        return
    if fact_type == "claim":
        intended = _optional_claim(facts["intended"], "intended claim")
        confirmed = _optional_claim(facts["confirmed"], "confirmed claim")
        if confirmed is not None and intended != confirmed:
            raise AttentionSchemaError("confirmed claim must equal intended claim")
        _bounded_text(facts["result"], "claim result", 512)  # type: ignore[arg-type]
        for field in ("lock_identity", "command_operation", "mutation_stage", "history_read"):
            _optional_text(facts[field], f"claim {field}", 1024)
        _optional_integer(facts["return_code"], "claim return code")
        return
    if fact_type == "launch":
        for field in ("executable", "compute", "checkout", "state_save_result"):
            _optional_text(facts[field], f"launch {field}")
        _bounded_text(facts["operation"], "launch operation", 1024)  # type: ignore[arg-type]
        _bounded_text(facts["launch_result"], "launch result", 1024)  # type: ignore[arg-type]
        _optional_bool(facts["returned_handle"], "launch returned_handle")
        _optional_integer(facts["pid"], "launch pid", positive=True)
        _optional_integer(facts["start_ticks"], "launch start ticks", positive=True)
        if facts["start_ticks"] is not None and facts["pid"] is None:
            raise AttentionSchemaError("launch start ticks require a pid")
        return
    if fact_type == "leaf":
        for field in (
            "backend", "checkout", "base", "branch", "compute_result", "backend_status",
            "evidence_id", "pr_url", "head_sha",
        ):
            _optional_text(facts[field], f"leaf {field}")
        _optional_bool(facts["isolated"], "leaf isolated")
        reasons = facts["validation_reason_codes"]
        if (
            not isinstance(reasons, (list, tuple))
            or len(reasons) > 128
            or not all(isinstance(reason, str) for reason in reasons)
        ):
            raise AttentionSchemaError("leaf validation reason codes are invalid")
        for reason in reasons:
            _bounded_text(reason, "leaf validation reason", 512)
        digest = facts["evidence_sha256"]
        evidence = facts["evidence_id"]
        if (digest is None) != (evidence is None):
            raise AttentionSchemaError("leaf evidence path and hash must be paired")
        if digest is not None:
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise AttentionSchemaError("leaf evidence hash is invalid")
            require_absolute_evidence(str(evidence))
        head = facts["head_sha"]
        if head is not None and (
            not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40,64}", head) is None
        ):
            raise AttentionSchemaError("leaf head is invalid")
        return
    if fact_type == "factory":
        _integer(facts["issue_id"], "factory issue_id", positive=True)
        _optional_integer(facts["attempt"], "factory attempt", positive=True)
        for field in (
            "run_id", "sandbox", "session", "controller_phase", "controller_error", "status",
            "lock_session", "pr_url", "next", "read_result",
        ):
            _optional_text(
                facts[field], f"factory {field}",
                65536 if field == "controller_error" else 4096,
            )
        _optional_bool(facts["valid"], "factory valid")
        if type(facts["next_present"]) is not bool:
            raise AttentionSchemaError("factory next_present must be boolean")
        if facts["next"] is not None and facts["next_present"] is not True:
            raise AttentionSchemaError("factory next presence is contradictory")
        for field in ("gates", "steps", "slices"):
            rows = facts[field]
            if (
                not isinstance(rows, (list, tuple))
                or len(rows) > 1024
                or not all(isinstance(row, Mapping) for row in rows)
            ):
                raise AttentionSchemaError(f"factory {field} are invalid")
        if facts["park_snapshot"] is not None and not isinstance(
            facts["park_snapshot"], Mapping
        ):
            raise AttentionSchemaError("factory park snapshot is invalid")
        return
    if fact_type == "reconcile":
        _optional_text(facts["original_run_id"], "reconcile run id")
        _optional_claim(facts["original_claim"], "reconcile original claim")
        for field in (
            "process_verdict", "lock_verdict", "automatic_handling_stage",
            "automatic_handling_result",
        ):
            _bounded_text(facts[field], f"reconcile {field}", 1024)  # type: ignore[arg-type]
        for field in ("publication_id", "evidence_id"):
            _optional_text(facts[field], f"reconcile {field}")
        return
    if fact_type == "interrupt":
        for field in ("interrupted_source", "process_verdict", "handle_verdict"):
            _bounded_text(facts[field], f"interrupt {field}", 1024)  # type: ignore[arg-type]
        if type(facts["retained_binding"]) is not bool:
            raise AttentionSchemaError("interrupt retained binding must be boolean")
        proofs = facts["positive_proof_ids"]
        if (
            not isinstance(proofs, (list, tuple))
            or not all(isinstance(item, str) and _PROOF_ID.fullmatch(item) for item in proofs)
        ):
            raise AttentionSchemaError("interrupt proof ids are invalid")
        return
    if fact_type == "legacy":
        _bounded_text(facts["original_key"], "legacy original key", 1024)  # type: ignore[arg-type]
        _optional_text(facts["signature"], "legacy signature")
        _optional_text(facts["occurrence"], "legacy occurrence")
        if not isinstance(facts["row"], Mapping):
            raise AttentionSchemaError("legacy row is invalid")
        return
    if fact_type == "lifecycle_start":
        target = (
            facts["target"].value
            if isinstance(facts["target"], AttentionTarget)
            else facts["target"]
        )
        try:
            AttentionTarget(target)
        except (TypeError, ValueError) as exc:
            raise AttentionSchemaError("lifecycle target is invalid") from exc
        if _optional_claim(facts["claim"], "lifecycle claim") is None:
            raise AttentionSchemaError("lifecycle claim is required")
        _timestamp(facts["admitted_at"], "lifecycle admitted_at")
        for field in ("run_id", "sandbox"):
            _optional_text(facts[field], f"lifecycle {field}")
        if facts["admission"] != "confirmed_claim":
            raise AttentionSchemaError("lifecycle admission is invalid")
        return
    if fact_type == "lifecycle_success":
        target = (
            facts["target"].value
            if isinstance(facts["target"], AttentionTarget)
            else facts["target"]
        )
        try:
            AttentionTarget(target)
        except (TypeError, ValueError) as exc:
            raise AttentionSchemaError("success target is invalid") from exc
        witness = facts["witness_id"]
        if not isinstance(witness, str) or re.fullmatch(r"[0-9a-f]{64}", witness) is None:
            raise AttentionSchemaError("success witness id is invalid")
        _timestamp(facts["completed_at"], "success completed_at")
        for field in ("evidence_path", "branch", "head_sha"):
            _bounded_text(facts[field], f"success {field}")  # type: ignore[arg-type]
        for field in ("pr_url", "run_id", "sandbox", "next"):
            _optional_text(facts[field], f"success {field}")
        if facts["next"] is not None and facts["next_present"] is not True:
            raise AttentionSchemaError("success next presence is contradictory")
        if re.fullmatch(r"[0-9a-f]{40,64}", str(facts["head_sha"])) is None:
            raise AttentionSchemaError("success head is invalid")
        return
    raise AttentionSchemaError(f"unknown facts variant {fact_type}")


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
