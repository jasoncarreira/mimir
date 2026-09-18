"""Typed autonomous Worklink attention records and read-only inspection."""

from __future__ import annotations

import inspect
import json
from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping

from ..redaction import redact_text
from .backends.feature_factory import FactoryStatus
from .claims import ClaimRecord


ATTENTION_SCHEMA_VERSION = 2
MAX_REASON_CHARS = 2000
MAX_SAFE_REF_CHARS = 1000


class AttentionKind(StrEnum):
    ATTENTION = "attention"
    FACTORY_STARTED = "factory_started"
    FACTORY_SUCCEEDED = "factory_succeeded"


class AttentionCause(StrEnum):
    LAUNCH_FAILED = "launch_failed"
    TEMPLATE_BLOCKED = "template_blocked"
    ADMISSION_BLOCKED = "admission_blocked"
    CLAIM_FAILED = "claim_failed"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    CHECKOUT_UNSAFE = "checkout_unsafe"
    BACKEND_BLOCKED = "backend_blocked"
    VALIDATION_BLOCKED = "validation_blocked"
    WORK_FAILED = "work_failed"
    CONTROLLER_FAILED = "controller_failed"
    RECOVERY_BLOCKED = "recovery_blocked"
    FACTORY_NEEDS_HUMAN = "factory_needs_human"
    FACTORY_BLOCKED = "factory_blocked"
    FACTORY_PARTIAL = "factory_partial"
    DETACHED_BLOCKED = "detached_blocked"
    RECONCILE_FAILED = "reconcile_failed"
    LEGACY_UNKNOWN = "legacy_unknown"


class AttentionSource(StrEnum):
    DETACHED_SPAWN = "detached_spawn"
    LEAF_TEMPLATE = "leaf_template"
    TEMPLATE_UNREADY = "template_unready"
    TEMPLATE_BLOCK_LABEL = "template_block_label"
    TEMPLATE_COMMENT = "template_comment"
    LEAF_COMPUTE = "leaf_compute"
    LEAF_CLAIM = "leaf_claim"
    LEAF_EXHAUSTION = "leaf_exhaustion"
    LEAF_CHECKOUT = "leaf_checkout"
    LEAF_LAUNCH = "leaf_launch"
    LEAF_RUNSTATE_SAVE = "leaf_runstate_save"
    LEAF_COMPUTE_CLEANUP = "leaf_compute_cleanup"
    LEAF_BACKEND_BLOCKED = "leaf_backend_blocked"
    LEAF_GATE_TIMEOUT = "leaf_gate_timeout"
    LEAF_GATE_MISSING = "leaf_gate_missing"
    LEAF_OUTPUT_OVERFLOW = "leaf_output_overflow"
    LEAF_WORK_FAILED = "leaf_work_failed"
    LEAF_TRANSITION = "leaf_transition"
    LEAF_RELEASE = "leaf_release"
    LEAF_CHECKOUT_CLEANUP = "leaf_checkout_cleanup"
    LEAF_CAPABILITY_CLEANUP = "leaf_capability_cleanup"
    LEAF_POSTCLAIM = "leaf_postclaim"
    LEAF_ERROR_TRANSITION = "leaf_error_transition"
    LEAF_RUN_BOUNDARY = "leaf_run_boundary"
    LEAF_PUBLICATION_FENCE = "leaf_publication_fence"
    LEAF_PUBLICATION_PUSH = "leaf_publication_push"
    LEAF_PUBLICATION_PR = "leaf_publication_pr"
    LEAF_PUBLICATION_EVIDENCE = "leaf_publication_evidence"
    LEAF_COMPLETED_EVIDENCE_WRITE = "leaf_completed_evidence_write"
    LEAF_EVIDENCE_COMMENT = "leaf_evidence_comment"
    LEAF_COMPLETED_STATE_CLEAR = "leaf_completed_state_clear"
    EPIC_LABEL = "epic_label"
    EPIC_TEMPLATE = "epic_template"
    EPIC_BACKEND = "epic_backend"
    EPIC_REPOSITORY = "epic_repository"
    EPIC_COMPUTE = "epic_compute"
    EPIC_FACTORY_ADMIT = "epic_factory_admit"
    EPIC_BASE = "epic_base"
    EPIC_RETAINED_BIND = "epic_retained_bind"
    EPIC_RETAINED_ISSUE_RELOAD = "epic_retained_issue_reload"
    EPIC_RETAINED_TRANSITION = "epic_retained_transition"
    EPIC_CLAIM = "epic_claim"
    EPIC_EXHAUSTION = "epic_exhaustion"
    EPIC_LAUNCH = "epic_launch"
    EPIC_RECOVERY = "epic_recovery"
    EPIC_SUPERVISION = "epic_supervision"
    EPIC_DRIVER_LOCK = "epic_driver_lock"
    EPIC_DRIVER_LOCK_SAVE = "epic_driver_lock_save"
    EPIC_DRIVER_LOCK_TRANSITION = "epic_driver_lock_transition"
    FACTORY_NEEDS_HUMAN = "factory_needs_human"
    FACTORY_BLOCKED = "factory_blocked"
    FACTORY_PARTIAL = "factory_partial"
    FACTORY_COMPLETION_VERIFY = "factory_completion_verify"
    FACTORY_TERMINAL_TRANSITION = "factory_terminal_transition"
    EPIC_CONTROLLER = "epic_controller"
    EPIC_CONTROLLER_RELOAD = "epic_controller_reload"
    EPIC_PRESERVATION = "epic_preservation"
    EPIC_ERROR_SAVE = "epic_error_save"
    EPIC_ERROR_TRANSITION = "epic_error_transition"
    EPIC_CANCEL = "epic_cancel"
    EPIC_WAIT_DRAIN = "epic_wait_drain"
    EPIC_TRANSCRIPT_SAVE = "epic_transcript_save"
    EPIC_COMPUTE_CLEANUP = "epic_compute_cleanup"
    EPIC_RELEASE = "epic_release"
    EPIC_RUN_BOUNDARY = "epic_run_boundary"
    ORPHAN_UNPUBLISHED = "orphan_unpublished"
    ORPHAN_AMBIGUOUS = "orphan_ambiguous"
    ORPHAN_EPIC = "orphan_epic"
    ORPHAN_LABELS_UNKNOWN = "orphan_labels_unknown"
    ORPHAN_LOCK_RELEASE = "orphan_lock_release"
    ORPHAN_COMMENT = "orphan_comment"
    ORPHAN_TARGET_LABEL = "orphan_target_label"
    ORPHAN_INPROGRESS_UNLABEL = "orphan_inprogress_unlabel"
    ORPHAN_STATE_UPDATE = "orphan_state_update"
    STARTUP_LEAF_SPAWN = "startup_leaf_spawn"
    STARTUP_FACTORY_SPAWN = "startup_factory_spawn"
    STARTUP_RUN_RECORD_READ = "startup_run_record_read"
    STARTUP_FACTORY_RECORD_READ = "startup_factory_record_read"
    EXECUTION_RECOVERY = "execution_recovery"
    FACTORY_INITIAL_START = "factory_initial_start"
    FACTORY_RECOVERY_START = "factory_recovery_start"
    FACTORY_SUCCESS = "factory_success"
    LEGACY_V1 = "legacy_v1"


class AttentionOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    PARTIAL = "partial"
    GENUINE_FAILURE = "genuine_failure"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    LEGACY_UNKNOWN = "legacy_unknown"


class AccountingBasis(StrEnum):
    PRECLAIM = "preclaim"
    EXHAUSTION = "exhaustion"
    LEAF_EXECUTION = "leaf_execution"
    VERIFIED_COMPLETION = "verified_completion"
    FACTORY_PARTIAL = "factory_partial"
    FACTORY_PR = "factory_pr"
    FACTORY_STEPS = "factory_steps"
    FACTORY_SLICES = "factory_slices"
    UNPUBLISHED_COMMITS = "unpublished_commits"
    INFRASTRUCTURE = "infrastructure"
    LEGACY_UNKNOWN = "legacy_unknown"


class EvidenceQuality(StrEnum):
    VALID = "valid"
    NULL = "null"
    EMPTY = "empty"
    MISSING = "missing"
    MALFORMED = "malformed"
    UNAVAILABLE = "unavailable"
    FOREIGN = "foreign"
    STALE = "stale"


class ClaimRelation(StrEnum):
    NONE = "none"
    CURRENT_CLAIM = "current_claim"
    RELATED_PRIOR_CLAIM = "related_prior_claim"


class ClaimBindingState(StrEnum):
    NONE = "none"
    PREPARED = "prepared"
    CONFIRMED = "confirmed"


class PublicationState(StrEnum):
    READY = "ready"


class Settlement(StrEnum):
    NOT_NEEDED = "not_needed"
    PENDING = "pending"
    APPLIED = "applied"


class Resolution(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    UNKNOWN = "unknown"


class HandlingDisposition(StrEnum):
    NOOP_RESOLVED = "noop_resolved"
    REMEDIATED = "remediated"
    OPERATOR_REQUIRED = "operator_required"
    OBSERVED = "observed"


class RecoveryRetirement(StrEnum):
    RETRY_EXHAUSTED = "retry_exhausted"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    HARD_REFUSAL = "hard_refusal"
    ENQUEUE_STALL = "enqueue_stall"
    STASH_EXPIRED = "stash_expired"
    PENDING_OVERFLOW = "pending_overflow"
    UNRECOVERABLE_STASH = "unrecoverable_stash"


class ValidationDetail(StrEnum):
    GATE_TIMED_OUT = "gate_timed_out"
    GATE_COMMAND_NOT_FOUND = "gate_command_not_found"
    OUTPUT_OVERFLOW = "output_overflow"


@dataclass(frozen=True)
class AccountingDecision:
    outcome: AttentionOutcome
    basis: AccountingBasis
    attempt_consumed: bool | None
    settlement: Settlement
    evidence_quality: EvidenceQuality = EvidenceQuality.VALID


@dataclass(frozen=True)
class AttentionFacts:
    kind: AttentionKind = AttentionKind.ATTENTION
    primary_outcome: AttentionOutcome | None = None
    original_result_status: str | None = None
    factory_status: FactoryStatus | Mapping[str, Any] | None = None
    accepted_factory_status: FactoryStatus | Mapping[str, Any] | None = None
    verified_completion: bool = False
    normalized_leaf_result: bool = False
    launch_error: bool = False
    unpublished_commits: bool = False
    preclaim: bool = False
    exhaustion: bool = False
    claim_relation: ClaimRelation = ClaimRelation.NONE
    evidence_quality: EvidenceQuality = EvidenceQuality.VALID


def _factory_field(status: FactoryStatus | Mapping[str, Any], name: str) -> Any:
    return status.get(name) if isinstance(status, Mapping) else getattr(status, name)


def classify_attention(facts: AttentionFacts) -> AccountingDecision:
    if facts.kind is AttentionKind.FACTORY_STARTED:
        return AccountingDecision(AttentionOutcome.STARTED, AccountingBasis.PRECLAIM, None, Settlement.NOT_NEEDED)
    if facts.exhaustion or facts.primary_outcome is AttentionOutcome.ATTEMPTS_EXHAUSTED:
        return AccountingDecision(AttentionOutcome.ATTEMPTS_EXHAUSTED, AccountingBasis.EXHAUSTION, False, Settlement.NOT_NEEDED)
    if facts.preclaim or facts.claim_relation is ClaimRelation.RELATED_PRIOR_CLAIM:
        return AccountingDecision(AttentionOutcome.INFRASTRUCTURE_FAILURE, AccountingBasis.PRECLAIM, False, Settlement.NOT_NEEDED)
    status = facts.accepted_factory_status or facts.factory_status
    primary = facts.primary_outcome or _outcome_for_status(facts.original_result_status)
    if primary is AttentionOutcome.PARTIAL:
        return AccountingDecision(primary, AccountingBasis.FACTORY_PARTIAL, True, Settlement.NOT_NEEDED)
    if facts.kind is AttentionKind.FACTORY_SUCCEEDED or facts.verified_completion:
        return AccountingDecision(AttentionOutcome.SUCCEEDED, AccountingBasis.VERIFIED_COMPLETION, True, Settlement.NOT_NEEDED)
    if status is not None:
        factory_result = _outcome_for_status(_factory_field(status, "status"))
        if factory_result is AttentionOutcome.PARTIAL:
            return AccountingDecision(factory_result, AccountingBasis.FACTORY_PARTIAL, True, Settlement.NOT_NEEDED)
        pr_url = _factory_field(status, "pr_url")
        steps = _factory_field(status, "steps")
        slices = _factory_field(status, "slices")
        selected = primary if primary not in {None, AttentionOutcome.INFRASTRUCTURE_FAILURE} else factory_result
        selected = selected or AttentionOutcome.GENUINE_FAILURE
        if isinstance(pr_url, str) and pr_url.strip():
            return AccountingDecision(selected, AccountingBasis.FACTORY_PR, True, Settlement.NOT_NEEDED)
        if _valid_factory_rows(steps, "agent"):
            return AccountingDecision(selected, AccountingBasis.FACTORY_STEPS, True, Settlement.NOT_NEEDED)
        if _valid_factory_rows(slices, "id"):
            return AccountingDecision(selected, AccountingBasis.FACTORY_SLICES, True, Settlement.NOT_NEEDED)
    if facts.normalized_leaf_result and not facts.launch_error:
        return AccountingDecision(primary or AttentionOutcome.GENUINE_FAILURE, AccountingBasis.LEAF_EXECUTION, True, Settlement.NOT_NEEDED)
    if facts.unpublished_commits:
        return AccountingDecision(primary or AttentionOutcome.GENUINE_FAILURE, AccountingBasis.UNPUBLISHED_COMMITS, True, Settlement.NOT_NEEDED)
    settlement = Settlement.PENDING if facts.claim_relation is ClaimRelation.CURRENT_CLAIM else Settlement.NOT_NEEDED
    return AccountingDecision(AttentionOutcome.INFRASTRUCTURE_FAILURE, AccountingBasis.INFRASTRUCTURE, False, settlement, facts.evidence_quality)


def _valid_factory_rows(rows: Any, identity: str) -> bool:
    return bool(
        isinstance(rows, (list, tuple))
        and rows
        and all(
            isinstance(row, Mapping)
            and isinstance(row.get(identity), str)
            and bool(row[identity].strip())
            and isinstance(row.get("status"), str)
            and bool(row["status"].strip())
            and type(row.get("attempts")) is int
            and row["attempts"] >= 0
            for row in rows
        )
    )


def _outcome_for_status(status: object) -> AttentionOutcome | None:
    normalized = str(status or "").strip().replace("-", "_")
    return {
        "partial": AttentionOutcome.PARTIAL,
        "needs_human": AttentionOutcome.NEEDS_HUMAN,
        "blocked": AttentionOutcome.BLOCKED,
        "completed": AttentionOutcome.SUCCEEDED,
        "review_ready": AttentionOutcome.SUCCEEDED,
        "failed": AttentionOutcome.GENUINE_FAILURE,
    }.get(normalized)


def _valid_claim_payload(value: Mapping[str, Any], issue_id: int) -> bool:
    if (
        value.get("issue_id") != issue_id
        or type(value.get("attempt")) is not int
        or value["attempt"] <= 0
        or not isinstance(value.get("agent_id"), str)
        or not value["agent_id"]
        or not isinstance(value.get("claimed_at"), str)
    ):
        return False
    try:
        datetime.fromisoformat(value["claimed_at"])
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class AttentionRecord:
    occurrence_id: str
    delivery_key: str
    kind: AttentionKind
    issue_id: int
    execution_id: str
    source: AttentionSource
    outcome: AttentionOutcome
    accounting_basis: AccountingBasis
    attempt_consumed: bool | None
    settlement: Settlement
    cause: AttentionCause | None = None
    run_id: str | None = None
    launch_id: str | None = None
    claim: ClaimRecord | None = None
    prior_claim: ClaimRecord | None = None
    claim_relation: ClaimRelation = ClaimRelation.NONE
    claim_binding_state: ClaimBindingState = ClaimBindingState.NONE
    error_signature: str = ""
    attempt: int | None = None
    reason: str = ""
    evidence_quality: EvidenceQuality = EvidenceQuality.VALID
    original_result_status: str | None = None
    original_factory_status: str | None = None
    primary_source: AttentionSource | None = None
    secondary_faults: tuple[Mapping[str, Any], ...] = ()
    refs: Mapping[str, str | None] = field(default_factory=dict)
    factory_projection: Mapping[str, Any] | None = None
    controller_phase: str | None = None
    controller_error: str | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    pr_head: str | None = None
    next: str | None = None
    next_present: bool = False
    autonomous: bool = True
    inhibited: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    delivered_at: str | None = None
    handled_at: str | None = None
    handling_disposition: HandlingDisposition | None = None
    retirement: RecoveryRetirement | None = None
    validation_detail: ValidationDetail | None = None
    publication_state: PublicationState = PublicationState.READY
    reset_generation_baseline: int = 0
    ready_cycle_baseline: int = 0
    manual_claim_baseline: Mapping[str, Any] | None = None
    schema_version: int = ATTENTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != ATTENTION_SCHEMA_VERSION
            or type(self.issue_id) is not int
            or self.issue_id <= 0
        ):
            raise ValueError("invalid attention record identity")
        if not self.autonomous:
            raise ValueError("attention records require autonomous provenance")
        if self.kind is AttentionKind.ATTENTION and self.cause is None:
            raise ValueError("attention records require a cause")
        if self.kind is not AttentionKind.ATTENTION and self.cause is not None:
            raise ValueError("lifecycle records cannot have a cause")
        if self.source is AttentionSource.LEGACY_V1 or self.cause is AttentionCause.LEGACY_UNKNOWN:
            raise ValueError("legacy identities cannot be produced")
        if not self.occurrence_id or not self.execution_id or not self.delivery_key:
            raise ValueError("attention record identifiers must be nonblank")
        if self.claim_relation is ClaimRelation.CURRENT_CLAIM and self.claim is None:
            raise ValueError("current claim relation requires an exact claim")
        if self.claim_binding_state is ClaimBindingState.CONFIRMED and self.claim is None:
            raise ValueError("confirmed claim binding requires an exact claim")
        if self.claim_relation is ClaimRelation.RELATED_PRIOR_CLAIM and self.prior_claim is None:
            raise ValueError("prior claim relation requires an exact prior claim")
        if self.claim_relation is ClaimRelation.NONE and (self.claim is not None or self.prior_claim is not None):
            raise ValueError("unrelated attention cannot carry claims")
        if self.claim is not None and self.claim.issue_id != self.issue_id:
            raise ValueError("attention claim identity mismatch")
        if self.prior_claim is not None and self.prior_claim.issue_id != self.issue_id:
            raise ValueError("attention prior claim identity mismatch")
        if type(self.attempt_consumed) is not bool and self.attempt_consumed is not None:
            raise ValueError("invalid attempt consumption")
        lifecycle = self.kind is not AttentionKind.ATTENTION
        if lifecycle and self.outcome not in {AttentionOutcome.STARTED, AttentionOutcome.SUCCEEDED}:
            raise ValueError("attention kind and outcome are inconsistent")
        lifecycle_sources = {
            AttentionSource.FACTORY_INITIAL_START,
            AttentionSource.FACTORY_RECOVERY_START,
            AttentionSource.FACTORY_SUCCESS,
        }
        if (self.source in lifecycle_sources) is not lifecycle:
            raise ValueError("attention source and kind are inconsistent")
        if self.kind is AttentionKind.FACTORY_STARTED and (
            self.source not in {
                AttentionSource.FACTORY_INITIAL_START,
                AttentionSource.FACTORY_RECOVERY_START,
            }
            or self.outcome is not AttentionOutcome.STARTED
            or self.accounting_basis is not AccountingBasis.PRECLAIM
        ):
            raise ValueError("invalid factory start record")
        if self.kind is AttentionKind.FACTORY_SUCCEEDED and (
            self.source is not AttentionSource.FACTORY_SUCCESS
            or self.outcome is not AttentionOutcome.SUCCEEDED
            or self.accounting_basis is not AccountingBasis.VERIFIED_COMPLETION
            or self.attempt_consumed is not True
        ):
            raise ValueError("invalid factory success record")
        if not lifecycle and self.outcome is AttentionOutcome.STARTED:
            raise ValueError("terminal attention cannot be a start record")
        if lifecycle and (
            self.settlement is not Settlement.NOT_NEEDED
            or self.attempt_consumed is not None and self.kind is AttentionKind.FACTORY_STARTED
            or self.inhibited
        ):
            raise ValueError("invalid lifecycle accounting")
        if self.kind is AttentionKind.ATTENTION and self.attempt_consumed is None:
            raise ValueError("terminal attention requires a consumption decision")
        terminal_accounting = {
            AttentionOutcome.INFRASTRUCTURE_FAILURE: {
                AccountingBasis.PRECLAIM,
                AccountingBasis.INFRASTRUCTURE,
            },
            AttentionOutcome.ATTEMPTS_EXHAUSTED: {AccountingBasis.EXHAUSTION},
            AttentionOutcome.PARTIAL: {AccountingBasis.FACTORY_PARTIAL},
            AttentionOutcome.SUCCEEDED: {
                AccountingBasis.VERIFIED_COMPLETION,
                AccountingBasis.FACTORY_PR,
                AccountingBasis.FACTORY_STEPS,
                AccountingBasis.FACTORY_SLICES,
                AccountingBasis.LEAF_EXECUTION,
            },
            AttentionOutcome.BLOCKED: {
                AccountingBasis.FACTORY_PR,
                AccountingBasis.FACTORY_STEPS,
                AccountingBasis.FACTORY_SLICES,
                AccountingBasis.LEAF_EXECUTION,
                AccountingBasis.UNPUBLISHED_COMMITS,
            },
            AttentionOutcome.NEEDS_HUMAN: {
                AccountingBasis.FACTORY_PR,
                AccountingBasis.FACTORY_STEPS,
                AccountingBasis.FACTORY_SLICES,
                AccountingBasis.LEAF_EXECUTION,
                AccountingBasis.UNPUBLISHED_COMMITS,
            },
            AttentionOutcome.GENUINE_FAILURE: {
                AccountingBasis.FACTORY_PR,
                AccountingBasis.FACTORY_STEPS,
                AccountingBasis.FACTORY_SLICES,
                AccountingBasis.LEAF_EXECUTION,
                AccountingBasis.UNPUBLISHED_COMMITS,
            },
        }
        if self.kind is AttentionKind.ATTENTION and (
            self.outcome not in terminal_accounting
            or self.accounting_basis not in terminal_accounting[self.outcome]
            or self.attempt_consumed is not (
                self.outcome not in {
                    AttentionOutcome.INFRASTRUCTURE_FAILURE,
                    AttentionOutcome.ATTEMPTS_EXHAUSTED,
                }
            )
        ):
            raise ValueError("attention outcome and accounting are inconsistent")
        if self.settlement in {Settlement.PENDING, Settlement.APPLIED} and (
            self.claim_relation is not ClaimRelation.CURRENT_CLAIM
            or self.attempt_consumed is not False
        ):
            raise ValueError("settlement requires a nonconsuming current claim")
        if self.primary_source is not None and self.primary_source is not self.source:
            raise ValueError("attention primary source mismatch")
        if not self.error_signature or not self.created_at:
            raise ValueError("attention signature and timestamp are required")
        datetime.fromisoformat(self.created_at)
        if (
            type(self.reset_generation_baseline) is not int
            or self.reset_generation_baseline < 0
            or type(self.ready_cycle_baseline) is not int
            or self.ready_cycle_baseline < 0
        ):
            raise ValueError("invalid attention rearm baseline")
        if self.manual_claim_baseline is not None and not _valid_claim_payload(
            self.manual_claim_baseline, self.issue_id
        ):
            raise ValueError("invalid attention manual claim baseline")
        for fault in self.secondary_faults:
            if not isinstance(fault, Mapping) or not isinstance(fault.get("reason"), str):
                raise ValueError("invalid attention secondary fault")
            AttentionSource(str(fault.get("source")))
            AttentionCause(str(fault.get("cause")))

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["kind"] = self.kind.value
        value["cause"] = self.cause.value if self.cause else None
        value["source"] = self.source.value
        value["outcome"] = self.outcome.value
        value["accounting_basis"] = self.accounting_basis.value
        value["settlement"] = self.settlement.value
        value["evidence_quality"] = self.evidence_quality.value
        value["claim_relation"] = self.claim_relation.value
        value["claim_binding_state"] = self.claim_binding_state.value
        value["primary_source"] = self.primary_source.value if self.primary_source else None
        value["handling_disposition"] = self.handling_disposition.value if self.handling_disposition else None
        value["retirement"] = self.retirement.value if self.retirement else None
        value["validation_detail"] = self.validation_detail.value if self.validation_detail else None
        value["publication_state"] = self.publication_state.value
        if self.claim is not None:
            value["claim"] = {
                "issue_id": self.claim.issue_id,
                "attempt": self.claim.attempt,
                "agent_id": self.claim.agent_id,
                "claimed_at": self.claim.claimed_at.isoformat(),
            }
        if self.prior_claim is not None:
            value["prior_claim"] = {
                "issue_id": self.prior_claim.issue_id,
                "attempt": self.prior_claim.attempt,
                "agent_id": self.prior_claim.agent_id,
                "claimed_at": self.prior_claim.claimed_at.isoformat(),
            }
        value["reason"] = redact_text(self.reason)[:MAX_REASON_CHARS]
        value["refs"] = {str(key): redact_text(str(item))[:MAX_SAFE_REF_CHARS] if item is not None else None for key, item in self.refs.items()}
        return value

    @classmethod
    def from_json(cls, value: Mapping[str, Any], *, legacy: bool = False) -> AttentionRecord:
        if not isinstance(value, Mapping):
            raise TypeError("attention record must be an object")
        if not legacy:
            required = {item.name for item in dataclass_fields(cls)}
            missing = required - set(value)
            if missing:
                raise ValueError(f"attention record fields are incomplete: {sorted(missing)}")
            if type(value["schema_version"]) is not int or value["schema_version"] != ATTENTION_SCHEMA_VERSION:
                raise ValueError("invalid attention schema version")
            if type(value["issue_id"]) is not int or type(value["attempt_consumed"]) not in {bool, type(None)}:
                raise TypeError("invalid attention scalar type")
            if value["cause"] is not None and not isinstance(value["cause"], str):
                raise TypeError("attention cause must be a string or null")
            for name in (
                "occurrence_id", "delivery_key", "kind", "execution_id", "source",
                "outcome", "accounting_basis", "settlement", "error_signature",
                "reason", "evidence_quality", "claim_relation", "claim_binding_state",
                "created_at", "publication_state",
            ):
                if not isinstance(value[name], str):
                    raise TypeError(f"attention {name} must be a string")
            for name in (
                "run_id", "launch_id", "original_result_status", "original_factory_status",
                "controller_phase", "controller_error", "pr_url", "pr_state", "pr_head",
                "next", "delivered_at", "handled_at", "handling_disposition", "retirement",
                "validation_detail", "primary_source",
            ):
                if value[name] is not None and not isinstance(value[name], str):
                    raise TypeError(f"attention {name} must be a string or null")
            if value["attempt"] is not None and type(value["attempt"]) is not int:
                raise TypeError("attention attempt must be an integer or null")
            for name in ("next_present", "autonomous", "inhibited"):
                if type(value[name]) is not bool:
                    raise TypeError(f"attention {name} must be boolean")
            for name in ("reset_generation_baseline", "ready_cycle_baseline"):
                if type(value[name]) is not int:
                    raise TypeError(f"attention {name} must be an integer")
            if not isinstance(value["secondary_faults"], (list, tuple)):
                raise TypeError("attention secondary_faults must be an array")
            if not isinstance(value["refs"], Mapping):
                raise TypeError("attention refs must be an object")
            if any(
                not isinstance(key, str)
                or item is not None and not isinstance(item, str)
                for key, item in value["refs"].items()
            ):
                raise TypeError("attention refs must contain string or null values")
            if value["factory_projection"] is not None and not isinstance(value["factory_projection"], Mapping):
                raise TypeError("attention factory_projection must be an object or null")
        claim_data = value.get("claim")
        if not legacy and claim_data is not None and not isinstance(claim_data, Mapping):
            raise TypeError("attention claim must be an object or null")
        if not legacy and isinstance(claim_data, Mapping) and not _valid_claim_payload(
            claim_data, value["issue_id"]
        ):
            raise ValueError("attention claim identity is invalid")
        claim = ClaimRecord.from_payload(dict(claim_data)) if isinstance(claim_data, Mapping) else None
        prior_claim_data = value.get("prior_claim")
        if not legacy and prior_claim_data is not None and not isinstance(prior_claim_data, Mapping):
            raise TypeError("attention prior_claim must be an object or null")
        if not legacy and isinstance(prior_claim_data, Mapping) and not _valid_claim_payload(
            prior_claim_data, value["issue_id"]
        ):
            raise ValueError("attention prior claim identity is invalid")
        prior_claim = ClaimRecord.from_payload(dict(prior_claim_data)) if isinstance(prior_claim_data, Mapping) else None
        source = AttentionSource.LEGACY_V1 if legacy else AttentionSource(str(value["source"]))
        cause = AttentionCause.LEGACY_UNKNOWN if legacy else AttentionCause(str(value["cause"])) if value.get("cause") else None
        record = cls.__new__(cls)
        fields = {
            "occurrence_id": str(value["occurrence_id"]),
            "delivery_key": str(value["delivery_key"]),
            "kind": AttentionKind(str(value["kind"] if not legacy else value.get("kind") or AttentionKind.ATTENTION)),
            "issue_id": int(value["issue_id"]),
            "execution_id": str(value["execution_id"] if not legacy else value.get("execution_id") or value["occurrence_id"]),
            "source": source,
            "outcome": AttentionOutcome(str(value["outcome"] if not legacy else value.get("outcome") or AttentionOutcome.LEGACY_UNKNOWN)),
            "accounting_basis": AccountingBasis(str(value["accounting_basis"] if not legacy else value.get("accounting_basis") or AccountingBasis.LEGACY_UNKNOWN)),
            "attempt_consumed": value.get("attempt_consumed"),
            "settlement": Settlement(str(value["settlement"] if not legacy else value.get("settlement") or Settlement.NOT_NEEDED)),
            "cause": cause,
            "run_id": value.get("run_id"),
            "launch_id": value.get("launch_id"),
            "claim": claim,
            "prior_claim": prior_claim,
            "claim_relation": ClaimRelation(str(value["claim_relation"] if not legacy else value.get("claim_relation") or ClaimRelation.NONE)),
            "claim_binding_state": ClaimBindingState(str(value["claim_binding_state"] if not legacy else value.get("claim_binding_state") or ClaimBindingState.NONE)),
            "error_signature": str(value.get("error_signature") or value.get("signature") or ""),
            "attempt": int(value["attempt"]) if value.get("attempt") is not None else None,
            "reason": str(value.get("reason") or value.get("terminal_error") or ""),
            "evidence_quality": EvidenceQuality(str(value["evidence_quality"] if not legacy else value.get("evidence_quality") or EvidenceQuality.MISSING)),
            "original_result_status": value.get("original_result_status"),
            "original_factory_status": value.get("original_factory_status"),
            "primary_source": AttentionSource(str(value["primary_source"])) if value.get("primary_source") else None,
            "secondary_faults": tuple(value.get("secondary_faults") or ()),
            "refs": dict(value.get("refs") or {}),
            "factory_projection": value.get("factory_projection"),
            "controller_phase": value.get("controller_phase"),
            "controller_error": value.get("controller_error"),
            "pr_url": value.get("pr_url"),
            "pr_state": value.get("pr_state"),
            "pr_head": value.get("pr_head"),
            "next": value.get("next"),
            "next_present": value.get("next_present") is True,
            "autonomous": value.get("autonomous", False if legacy else True) is True,
            "inhibited": value.get("inhibited", True) is True,
            "created_at": str(value.get("created_at") or value.get("failed_at") or ""),
            "delivered_at": value.get("delivered_at"),
            "handled_at": value.get("handled_at"),
            "handling_disposition": HandlingDisposition(str(value["handling_disposition"])) if value.get("handling_disposition") else None,
            "retirement": RecoveryRetirement(str(value["retirement"])) if value.get("retirement") else None,
            "validation_detail": ValidationDetail(str(value["validation_detail"])) if value.get("validation_detail") else None,
            "publication_state": PublicationState(str(value.get("publication_state") or PublicationState.READY)),
            "reset_generation_baseline": value.get("reset_generation_baseline", 0),
            "ready_cycle_baseline": value.get("ready_cycle_baseline", 0),
            "manual_claim_baseline": value.get("manual_claim_baseline"),
            "schema_version": value.get("schema_version", ATTENTION_SCHEMA_VERSION),
        }
        for name, item in fields.items():
            object.__setattr__(record, name, item)
        if not legacy:
            record.__post_init__()
        return record


@dataclass(frozen=True)
class AttentionReaders:
    issue: Callable[[int], Any]
    claims: Callable[[int], Any]
    run_state: Callable[[int], Any]
    factory_record: Callable[[str | None, int], Any]
    process: Callable[[Any], Any]
    evidence: Callable[[AttentionRecord], Any]
    pull_request: Callable[[str], Any]
    source_state: Callable[[AttentionRecord], Any] = lambda _record: None


@dataclass(frozen=True)
class AttentionSnapshot:
    record: AttentionRecord
    resolution: Resolution
    predicates: Mapping[str, Resolution]
    current: Mapping[str, Any]
    errors: tuple[str, ...] = ()
    decision: str = "operator_required"

    def to_json(self) -> dict[str, Any]:
        return {
            "identity": {
                "issue_id": self.record.issue_id,
                "signature": self.record.error_signature,
                "occurrence_id": self.record.occurrence_id,
            },
            "kind": self.record.kind.value,
            "cause": self.record.cause.value if self.record.cause else None,
            "source": self.record.source.value,
            "outcome": self.record.outcome.value,
            "accounting": {
                "basis": self.record.accounting_basis.value,
                "attempt_consumed": self.record.attempt_consumed,
                "settlement": self.record.settlement.value,
            },
            "resolution": self.resolution.value,
            "predicates": {key: value.value for key, value in self.predicates.items()},
            "current": dict(self.current),
            "errors": list(self.errors),
            "decision": self.decision,
            "next": self.record.next,
            "next_present": self.record.next_present,
            "refs": dict(self.record.refs),
        }


_SOURCE_POLICIES: dict[AttentionSource, tuple[str, ...]] = {
    AttentionSource.DETACHED_SPAWN: ("source_clear", "disarmed", "superseded"),
    AttentionSource.LEAF_TEMPLATE: ("source_clear", "disarmed", "superseded"),
    AttentionSource.TEMPLATE_UNREADY: ("source_clear", "disarmed", "superseded"),
    AttentionSource.TEMPLATE_BLOCK_LABEL: ("source_clear", "disarmed", "superseded"),
    AttentionSource.TEMPLATE_COMMENT: ("source_clear", "disarmed", "superseded"),
    AttentionSource.LEAF_COMPUTE: ("source_clear", "disarmed", "superseded"),
    AttentionSource.LEAF_CLAIM: ("disarmed", "superseded", "explicit_rearm"),
    AttentionSource.LEAF_EXHAUSTION: ("budget_available", "disarmed", "superseded"),
    AttentionSource.LEAF_CHECKOUT: ("disarmed", "superseded"),
    AttentionSource.LEAF_LAUNCH: ("disarmed", "superseded"),
    AttentionSource.LEAF_RUNSTATE_SAVE: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_COMPUTE_CLEANUP: ("lock_and_owner_absent", "superseded", "publication_resolved"),
    AttentionSource.LEAF_BACKEND_BLOCKED: ("disarmed", "superseded", "publication_resolved", "explicit_rearm"),
    AttentionSource.LEAF_GATE_TIMEOUT: ("disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_GATE_MISSING: ("disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_OUTPUT_OVERFLOW: ("disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_WORK_FAILED: ("disarmed", "superseded", "publication_resolved", "explicit_rearm"),
    AttentionSource.LEAF_TRANSITION: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_RELEASE: ("lock_and_owner_absent", "superseded"),
    AttentionSource.LEAF_CHECKOUT_CLEANUP: ("disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_CAPABILITY_CLEANUP: ("disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_POSTCLAIM: ("disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_ERROR_TRANSITION: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_RUN_BOUNDARY: ("disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_PUBLICATION_FENCE: ("disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_PUBLICATION_PUSH: ("disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_PUBLICATION_PR: ("disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_PUBLICATION_EVIDENCE: ("source_clear", "publication_resolved", "disarmed", "superseded"),
    AttentionSource.LEAF_COMPLETED_EVIDENCE_WRITE: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_EVIDENCE_COMMENT: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.LEAF_COMPLETED_STATE_CLEAR: ("source_clear", "publication_resolved", "superseded"),
    AttentionSource.EPIC_LABEL: ("source_clear", "disarmed", "superseded"),
    AttentionSource.EPIC_TEMPLATE: ("source_clear", "disarmed", "superseded"),
    AttentionSource.EPIC_BACKEND: ("disarmed", "superseded"),
    AttentionSource.EPIC_REPOSITORY: ("source_clear", "disarmed", "superseded"),
    AttentionSource.EPIC_COMPUTE: ("source_clear", "disarmed", "superseded"),
    AttentionSource.EPIC_FACTORY_ADMIT: ("disarmed", "superseded"),
    AttentionSource.EPIC_BASE: ("source_clear", "disarmed", "superseded"),
    AttentionSource.EPIC_RETAINED_BIND: ("disarmed", "superseded", "factory_advanced"),
    AttentionSource.EPIC_RETAINED_ISSUE_RELOAD: ("source_clear", "disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.EPIC_RETAINED_TRANSITION: ("source_clear", "disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.EPIC_CLAIM: ("disarmed", "superseded", "explicit_rearm"),
    AttentionSource.EPIC_EXHAUSTION: ("budget_available", "disarmed", "superseded"),
    AttentionSource.EPIC_LAUNCH: ("disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.EPIC_RECOVERY: ("disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.EPIC_SUPERVISION: ("disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.EPIC_DRIVER_LOCK: ("disarmed", "superseded", "factory_advanced"),
    AttentionSource.EPIC_DRIVER_LOCK_SAVE: ("source_clear", "disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.EPIC_DRIVER_LOCK_TRANSITION: ("source_clear", "disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.FACTORY_NEEDS_HUMAN: ("factory_advanced", "disarmed", "superseded", "publication_resolved", "explicit_rearm"),
    AttentionSource.FACTORY_BLOCKED: ("factory_advanced", "disarmed", "superseded", "publication_resolved", "explicit_rearm"),
    AttentionSource.FACTORY_PARTIAL: ("factory_advanced", "disarmed", "superseded", "publication_resolved", "explicit_rearm"),
    AttentionSource.FACTORY_COMPLETION_VERIFY: ("publication_resolved", "disarmed", "superseded", "factory_advanced"),
    AttentionSource.FACTORY_TERMINAL_TRANSITION: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.EPIC_CONTROLLER: ("disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.EPIC_CONTROLLER_RELOAD: ("source_clear", "disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.EPIC_PRESERVATION: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.EPIC_ERROR_SAVE: ("source_clear", "disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.EPIC_ERROR_TRANSITION: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.EPIC_CANCEL: ("source_clear", "superseded", "publication_resolved"),
    AttentionSource.EPIC_WAIT_DRAIN: ("source_clear", "disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.EPIC_TRANSCRIPT_SAVE: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.EPIC_COMPUTE_CLEANUP: ("lock_and_owner_absent", "superseded", "publication_resolved"),
    AttentionSource.EPIC_RELEASE: ("lock_and_owner_absent", "superseded"),
    AttentionSource.EPIC_RUN_BOUNDARY: ("disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.ORPHAN_UNPUBLISHED: ("source_clear", "publication_resolved", "disarmed", "superseded"),
    AttentionSource.ORPHAN_AMBIGUOUS: ("source_clear", "disarmed", "superseded", "explicit_rearm"),
    AttentionSource.ORPHAN_EPIC: ("factory_advanced", "publication_resolved", "disarmed", "superseded", "explicit_rearm"),
    AttentionSource.ORPHAN_LABELS_UNKNOWN: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.ORPHAN_LOCK_RELEASE: ("lock_and_owner_absent", "superseded"),
    AttentionSource.ORPHAN_COMMENT: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.ORPHAN_TARGET_LABEL: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.ORPHAN_INPROGRESS_UNLABEL: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.ORPHAN_STATE_UPDATE: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.STARTUP_LEAF_SPAWN: ("source_clear", "publication_resolved", "disarmed", "superseded"),
    AttentionSource.STARTUP_FACTORY_SPAWN: ("factory_advanced", "publication_resolved", "disarmed", "superseded"),
    AttentionSource.STARTUP_RUN_RECORD_READ: ("source_clear", "disarmed", "superseded", "publication_resolved"),
    AttentionSource.STARTUP_FACTORY_RECORD_READ: ("source_clear", "disarmed", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.EXECUTION_RECOVERY: ("disarmed", "superseded", "publication_resolved", "factory_advanced"),
    AttentionSource.FACTORY_INITIAL_START: ("source_clear", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.FACTORY_RECOVERY_START: ("source_clear", "superseded", "factory_advanced", "publication_resolved"),
    AttentionSource.FACTORY_SUCCESS: ("source_clear", "superseded", "factory_advanced", "publication_resolved"),
}


def _invoke(reader: Callable[..., Any], *args: Any) -> Any:
    value = reader(*args)
    if inspect.isawaitable(value):
        raise TypeError("attention readers must be synchronous")
    return value


def inspect_attention(
    home: Path,
    issue_id: int,
    signature: str,
    occurrence_id: str,
    readers: AttentionReaders,
) -> AttentionSnapshot:
    from .dispatch_failures import get_attention_record

    record = get_attention_record(home, issue_id, signature, occurrence_id)
    errors: list[str] = []
    current: dict[str, Any] = {}
    values: dict[str, Any] = {}
    for name, reader, args in (
        ("issue", readers.issue, (issue_id,)),
        ("claims", readers.claims, (issue_id,)),
        ("run_state", readers.run_state, (issue_id,)),
        ("factory", readers.factory_record, (record.run_id, issue_id)),
        ("evidence", readers.evidence, (record,)),
        ("source_state", readers.source_state, (record,)),
    ):
        try:
            values[name] = _invoke(reader, *args)
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")
    issue = values.get("issue")
    labels = _labels(issue)
    comments = _comments(issue)
    current["labels"] = sorted(labels) if labels is not None else None
    current["occurrence_comment"] = _occurrence_comment(comments, occurrence_id)
    old_owner = values.get("run_state") or values.get("factory")
    process_dead: bool | None = None
    if old_owner is not None:
        try:
            observed = _invoke(readers.process, old_owner)
            process_dead = True if observed is False or observed == "verified_dead" else False if observed is True or observed == "alive" else None
        except Exception as exc:
            errors.append(f"process:{type(exc).__name__}")
    elif record.attempt is None and record.run_id is None and record.launch_id is None:
        process_dead = True
    current["old_process_verified_dead"] = process_dead
    claims = values.get("claims")
    lock_absent = _lock_absent(claims)
    current["lock_absent"] = lock_absent
    no_lifecycle = labels is not None and not labels.intersection({"worklink:ready", "worklink:in-progress", "worklink:blocked"})
    disarmed = _tri(no_lifecycle and lock_absent is True and process_dead is True, labels is None or lock_absent is None or process_dead is None)
    superseded = _superseded(record, claims, process_dead)
    publication = _publication(record, values.get("evidence"), readers, errors)
    factory = _factory_advanced(record, values.get("factory"), process_dead)
    budget = _budget_available(claims)
    rearmed = _rearmed(record, issue, claims)
    predicates = {
        "disarmed": disarmed,
        "superseded": superseded,
        "publication_resolved": publication,
        "factory_advanced": factory,
        "budget_available": budget,
        "lock_and_owner_absent": _tri(lock_absent is True and process_dead is True, lock_absent is None or process_dead is None),
        "explicit_rearm": rearmed,
        "source_clear": _source_clearance(record, values, labels, comments, process_dead),
    }
    applicable = _applicable_predicates(record, predicates)
    resolution = Resolution.RESOLVED if Resolution.RESOLVED in applicable else Resolution.UNKNOWN if errors or Resolution.UNKNOWN in applicable else Resolution.UNRESOLVED
    decision = "noop_resolved" if resolution is Resolution.RESOLVED else "operator_required"
    return AttentionSnapshot(record, resolution, predicates, current, tuple(errors), decision)


def _tri(value: bool, unknown: bool = False) -> Resolution:
    return Resolution.UNKNOWN if unknown else Resolution.RESOLVED if value else Resolution.UNRESOLVED


def _labels(issue: Any) -> set[str] | None:
    raw = issue.get("labels") if isinstance(issue, Mapping) else getattr(issue, "labels", None)
    if isinstance(raw, Mapping):
        return {str(key) for key in raw}
    if isinstance(raw, (list, tuple, set, frozenset)):
        return {str(item.get("name") if isinstance(item, Mapping) else item) for item in raw}
    return None


def _comments(issue: Any) -> tuple[str, ...] | None:
    raw = issue.get("comments") if isinstance(issue, Mapping) else getattr(issue, "comments", None)
    if not isinstance(raw, (list, tuple)):
        return None
    return tuple(str(item.get("body") or item.get("text") or "") if isinstance(item, Mapping) else str(item) for item in raw)


def _occurrence_comment(comments: tuple[str, ...] | None, occurrence_id: str) -> bool | None:
    if comments is None:
        return None
    marker = f"WORKLINK_ATTENTION:{occurrence_id}"
    for text in comments:
        if marker in text.split():
            return True
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping) and payload.get("occurrence_id") == occurrence_id:
            return True
    return False


def _lock_absent(claims: Any) -> bool | None:
    if isinstance(claims, Mapping):
        if "lock_absent" in claims:
            return claims["lock_absent"] if isinstance(claims["lock_absent"], bool) else None
        locks = claims.get("locks")
        return not bool(locks) if isinstance(locks, (list, tuple, dict, set)) else None
    return None


def _superseded(record: AttentionRecord, claims: Any, process_dead: bool | None) -> Resolution:
    if not isinstance(claims, Mapping) or process_dead is not True:
        return Resolution.UNKNOWN if claims is None or process_dead is None else Resolution.UNRESOLVED
    latest = claims.get("latest")
    if not isinstance(latest, (ClaimRecord, Mapping)) or record.attempt is None:
        return Resolution.UNRESOLVED
    attempt = latest.attempt if isinstance(latest, ClaimRecord) else latest.get("attempt")
    agent = latest.agent_id if isinstance(latest, ClaimRecord) else latest.get("agent_id")
    claimed_at = latest.claimed_at.isoformat() if isinstance(latest, ClaimRecord) else latest.get("claimed_at")
    newer = type(attempt) is int and attempt > record.attempt
    if record.claim is not None and attempt == record.claim.attempt:
        newer = agent != record.claim.agent_id or claimed_at != record.claim.claimed_at.isoformat()
    return _tri(newer)


def _publication(record: AttentionRecord, evidence: Any, readers: AttentionReaders, errors: list[str]) -> Resolution:
    exact = _exact_evidence(record, evidence)
    if exact is not Resolution.RESOLVED:
        return exact
    url = record.pr_url
    if not isinstance(url, str) or not url:
        return Resolution.UNRESOLVED
    try:
        pr = _invoke(readers.pull_request, url)
    except Exception as exc:
        errors.append(f"pull_request:{type(exc).__name__}")
        return Resolution.UNKNOWN
    state = pr.get("state") if isinstance(pr, Mapping) else None
    head = pr.get("headRefOid") if isinstance(pr, Mapping) else None
    return _tri(state in {"OPEN", "MERGED"} and head == record.pr_head)


def _exact_evidence(record: AttentionRecord, evidence: Any) -> Resolution:
    if evidence is None:
        return Resolution.UNKNOWN
    if not isinstance(evidence, Mapping):
        return Resolution.UNKNOWN
    branch = record.refs.get("branch")
    if (
        record.attempt is None
        or not isinstance(branch, str)
        or not branch
        or not isinstance(record.pr_url, str)
        or not record.pr_url
        or not isinstance(record.pr_head, str)
        or not record.pr_head
    ):
        return Resolution.UNRESOLVED
    return _tri(
        evidence.get("status") == "completed"
        and type(evidence.get("issue")) is int
        and evidence["issue"] == record.issue_id
        and type(evidence.get("attempt")) is int
        and evidence["attempt"] == record.attempt
        and evidence.get("branch") == branch
        and evidence.get("pr_url") == record.pr_url
        and evidence.get("head_sha") == record.pr_head
    )


def _factory_identity(record: AttentionRecord, factory: Any) -> bool | None:
    if factory is None:
        return None
    field = lambda name: (
        factory.get(name) if isinstance(factory, Mapping) else getattr(factory, name, None)
    )
    if record.run_id is None or field("run_id") != record.run_id:
        return False
    if field("issue_id") != record.issue_id:
        return False
    if record.attempt is not None and field("attempt") != record.attempt:
        return False
    if record.execution_id and field("execution_id") != record.execution_id:
        return False
    if record.launch_id is not None and field("launch_id") != record.launch_id:
        return False
    return True


def _factory_advanced(record: AttentionRecord, factory: Any, process_dead: bool | None) -> Resolution:
    identity = _factory_identity(record, factory)
    if identity is None:
        return Resolution.UNKNOWN
    if not identity:
        return Resolution.UNRESOLVED
    status = getattr(factory, "status", None)
    attempt = getattr(factory, "attempt", None)
    if isinstance(factory, Mapping):
        status = factory.get("status")
        attempt = factory.get("attempt")
        run_id = factory.get("run_id")
    else:
        run_id = getattr(factory, "run_id", None)
    if status is not None and not isinstance(status, str):
        status = getattr(status, "status", None)
    advanced = type(attempt) is int and record.attempt is not None and attempt > record.attempt
    advanced = advanced or (
        status in {"completed", "review_ready"}
        and record.original_factory_status not in {"completed", "review_ready"}
    )
    return _tri(bool(advanced and process_dead is True), process_dead is None)


def _budget_available(claims: Any) -> Resolution:
    if not isinstance(claims, Mapping):
        return Resolution.UNKNOWN
    used, maximum = claims.get("attempts_used"), claims.get("max_attempts")
    if type(used) is not int or type(maximum) is not int:
        return Resolution.UNKNOWN
    return _tri(used < maximum)


def _rearmed(record: AttentionRecord, issue: Any, claims: Any) -> Resolution:
    if not isinstance(claims, Mapping):
        return Resolution.UNKNOWN
    reset_generation = claims.get("reset_generation")
    ready_cycle_generation = claims.get("ready_cycle_generation")
    latest = claims.get("latest")
    latest_payload = (
        {
            "issue_id": latest.issue_id,
            "attempt": latest.attempt,
            "agent_id": latest.agent_id,
            "claimed_at": latest.claimed_at.isoformat(),
        }
        if isinstance(latest, ClaimRecord)
        else dict(latest) if isinstance(latest, Mapping) else None
    )
    current_claim = (
        {
            "issue_id": record.claim.issue_id,
            "attempt": record.claim.attempt,
            "agent_id": record.claim.agent_id,
            "claimed_at": record.claim.claimed_at.isoformat(),
        }
        if record.claim is not None else None
    )
    if type(reset_generation) is int and reset_generation > record.reset_generation_baseline:
        return Resolution.RESOLVED
    if type(ready_cycle_generation) is int and ready_cycle_generation > record.ready_cycle_baseline:
        return Resolution.RESOLVED
    if (
        latest_payload is not None
        and latest_payload != record.manual_claim_baseline
        and latest_payload != current_claim
    ):
        return Resolution.RESOLVED
    labels = _labels(issue)
    if labels is None:
        return Resolution.UNKNOWN
    return Resolution.UNRESOLVED


def _source_clearance(
    record: AttentionRecord,
    values: Mapping[str, Any],
    labels: set[str] | None,
    comments: tuple[str, ...] | None,
    process_dead: bool | None,
) -> Resolution:
    issue = values.get("issue")
    run_state = values.get("run_state")
    factory = values.get("factory")
    evidence = values.get("evidence")
    claims = values.get("claims")
    source_state = values.get("source_state")
    if record.source is AttentionSource.TEMPLATE_UNREADY:
        return _tri(labels is not None and "worklink:ready" not in labels, labels is None)
    if record.source is AttentionSource.TEMPLATE_BLOCK_LABEL:
        return _tri(labels is not None and "worklink:blocked" in labels and "worklink:ready" not in labels, labels is None)
    if record.source in {AttentionSource.TEMPLATE_COMMENT, AttentionSource.ORPHAN_COMMENT, AttentionSource.LEAF_EVIDENCE_COMMENT}:
        found = _occurrence_comment(comments, record.occurrence_id)
        return _tri(found is True, found is None)
    if record.source in {AttentionSource.LEAF_TRANSITION, AttentionSource.LEAF_ERROR_TRANSITION, AttentionSource.EPIC_RETAINED_TRANSITION, AttentionSource.EPIC_DRIVER_LOCK_TRANSITION, AttentionSource.FACTORY_TERMINAL_TRANSITION, AttentionSource.EPIC_ERROR_TRANSITION, AttentionSource.ORPHAN_TARGET_LABEL, AttentionSource.ORPHAN_INPROGRESS_UNLABEL}:
        target = record.refs.get("target_label")
        return _tri(labels is not None and isinstance(target, str) and target in labels and "worklink:in-progress" not in labels, labels is None or not isinstance(target, str))
    if record.source in {AttentionSource.EPIC_CANCEL, AttentionSource.STARTUP_LEAF_SPAWN}:
        return _tri(process_dead is True, process_dead is None)
    if record.source is AttentionSource.EPIC_RETAINED_ISSUE_RELOAD:
        original_resolved = (
            source_state.get("original_predicate_resolved")
            if isinstance(source_state, Mapping) else None
        )
        return _tri(
            labels is not None and original_resolved is True,
            "issue" not in values or "source_state" not in values
            or not isinstance(original_resolved, bool),
        )
    if record.source in {AttentionSource.LEAF_TEMPLATE, AttentionSource.EPIC_TEMPLATE}:
        try:
            from .orchestrator import LeafValidationError, validate_leaf

            validate_leaf(issue)
        except (LeafValidationError, TypeError, ValueError):
            return Resolution.UNRESOLVED
        except Exception:
            return Resolution.UNKNOWN
        return Resolution.RESOLVED
    if record.source in {
        AttentionSource.DETACHED_SPAWN,
        AttentionSource.LEAF_RUNSTATE_SAVE, AttentionSource.STARTUP_RUN_RECORD_READ,
    }:
        field = lambda name: (
            run_state.get(name) if isinstance(run_state, Mapping) else getattr(run_state, name, None)
        )
        exact = (
            run_state is not None
            and field("issue_id") == record.issue_id
            and field("execution_id") == record.execution_id
            and (record.attempt is None or field("attempt") == record.attempt)
            and (record.launch_id is None or field("launch_id") == record.launch_id)
        )
        return _tri(exact, "run_state" not in values)
    if record.source is AttentionSource.LEAF_COMPUTE:
        exact = (
            source_state.get("predicate_matches")
            if isinstance(source_state, Mapping) else None
        )
        return _tri(exact is True, "source_state" not in values or not isinstance(exact, bool))
    if record.source in {
        AttentionSource.LEAF_PUBLICATION_EVIDENCE,
        AttentionSource.LEAF_COMPLETED_EVIDENCE_WRITE,
    }:
        return _exact_evidence(record, evidence)
    if record.source in {
        AttentionSource.LEAF_COMPLETED_STATE_CLEAR, AttentionSource.ORPHAN_STATE_UPDATE,
    }:
        target = record.refs.get("target_label")
        exact = (
            run_state is None
            and isinstance(target, str)
            and labels is not None
            and target in labels
            and "worklink:in-progress" not in labels
            and _lock_absent(claims) is True
        )
        return _tri(
            exact,
            "run_state" not in values or labels is None or _lock_absent(claims) is None,
        )
    if record.source in {
        AttentionSource.EPIC_CONTROLLER_RELOAD,
        AttentionSource.EPIC_DRIVER_LOCK_SAVE,
        AttentionSource.EPIC_ERROR_SAVE,
        AttentionSource.EPIC_TRANSCRIPT_SAVE,
        AttentionSource.STARTUP_FACTORY_RECORD_READ,
        AttentionSource.FACTORY_INITIAL_START,
        AttentionSource.FACTORY_RECOVERY_START,
        AttentionSource.FACTORY_SUCCESS,
    }:
        exact = _factory_identity(record, factory) is True
        if exact and record.source is AttentionSource.EPIC_ERROR_SAVE:
            phase = factory.get("controller_phase") if isinstance(factory, Mapping) else getattr(factory, "controller_phase", None)
            exact = phase in {"failed", "parked", "terminal", "stopped"}
        if exact and record.source is AttentionSource.EPIC_TRANSCRIPT_SAVE:
            transcript = factory.get("transcript") if isinstance(factory, Mapping) else getattr(factory, "transcript", None)
            exact = isinstance(transcript, str) and bool(transcript)
        original_required = record.source in {
            AttentionSource.EPIC_CONTROLLER_RELOAD,
            AttentionSource.EPIC_DRIVER_LOCK_SAVE,
            AttentionSource.FACTORY_SUCCESS,
        }
        if exact and original_required:
            exact = (
                isinstance(source_state, Mapping)
                and source_state.get("original_predicate_resolved") is True
            )
        return _tri(
            exact,
            "factory" not in values or original_required and "source_state" not in values,
        )
    if record.source is AttentionSource.EPIC_LABEL:
        return _tri(labels is not None and "worklink:epic" in labels, labels is None)
    if record.source is AttentionSource.ORPHAN_LABELS_UNKNOWN:
        return _tri(
            labels is not None
            and "worklink:blocked" not in labels
            and bool(labels.intersection({"worklink:ready", "worklink:in-progress"})),
            "issue" not in values,
        )
    if record.source in {
        AttentionSource.EPIC_REPOSITORY, AttentionSource.EPIC_COMPUTE,
        AttentionSource.EPIC_BASE,
    }:
        exact = (
            source_state.get("predicate_matches")
            if isinstance(source_state, Mapping) else None
        )
        return _tri(exact is True, "source_state" not in values or not isinstance(exact, bool))
    if record.source is AttentionSource.EPIC_PRESERVATION:
        return _tri(bool(record.refs.get("preserved_ref")))
    if record.source is AttentionSource.ORPHAN_UNPUBLISHED:
        return _tri(run_state is None and labels is not None and "worklink:blocked" not in labels, "run_state" not in values or labels is None)
    return Resolution.UNKNOWN


def _applicable_predicates(record: AttentionRecord, values: Mapping[str, Resolution]) -> tuple[Resolution, ...]:
    policy = _SOURCE_POLICIES.get(record.source)
    if policy is None:
        return (Resolution.UNKNOWN,)
    return tuple(values[name] for name in policy)


def render_attention_prompt(record: AttentionRecord) -> str:
    identity = f"issue {record.issue_id}, occurrence {record.occurrence_id}"
    return (
        f"Handle autonomous Worklink {record.kind.value} for {identity}. "
        "Use worklink_attention_inspect with the exact identity before deciding. "
        "Acknowledge only with worklink_attention_ack. For operator_required, provide "
        "a concrete bounded note; acknowledgement sends the configured operator alert."
    )[:4000]


def bounded_json(value: Mapping[str, Any], limit: int = 16000) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return encoded if len(encoded) <= limit else json.dumps({"status": "refused", "reason": "result_too_large"})
