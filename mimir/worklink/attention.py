"""Typed autonomous Worklink attention records and read-only inspection."""

from __future__ import annotations

import inspect
import json
from dataclasses import asdict, dataclass, field
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
    if facts.kind is AttentionKind.FACTORY_SUCCEEDED or facts.verified_completion:
        return AccountingDecision(AttentionOutcome.SUCCEEDED, AccountingBasis.VERIFIED_COMPLETION, True, Settlement.NOT_NEEDED)
    if facts.exhaustion or facts.primary_outcome is AttentionOutcome.ATTEMPTS_EXHAUSTED:
        return AccountingDecision(AttentionOutcome.ATTEMPTS_EXHAUSTED, AccountingBasis.EXHAUSTION, False, Settlement.NOT_NEEDED)
    if facts.preclaim or facts.claim_relation is ClaimRelation.RELATED_PRIOR_CLAIM:
        return AccountingDecision(AttentionOutcome.INFRASTRUCTURE_FAILURE, AccountingBasis.PRECLAIM, False, Settlement.NOT_NEEDED)
    status = facts.accepted_factory_status or facts.factory_status
    primary = facts.primary_outcome or _outcome_for_status(facts.original_result_status)
    if primary is AttentionOutcome.PARTIAL:
        return AccountingDecision(primary, AccountingBasis.FACTORY_PARTIAL, True, Settlement.NOT_NEEDED)
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
        if isinstance(steps, (list, tuple)) and steps:
            return AccountingDecision(selected, AccountingBasis.FACTORY_STEPS, True, Settlement.NOT_NEEDED)
        if isinstance(slices, (list, tuple)) and slices:
            return AccountingDecision(selected, AccountingBasis.FACTORY_SLICES, True, Settlement.NOT_NEEDED)
    if facts.normalized_leaf_result and not facts.launch_error:
        return AccountingDecision(primary or AttentionOutcome.GENUINE_FAILURE, AccountingBasis.LEAF_EXECUTION, True, Settlement.NOT_NEEDED)
    if facts.unpublished_commits:
        return AccountingDecision(primary or AttentionOutcome.GENUINE_FAILURE, AccountingBasis.UNPUBLISHED_COMMITS, True, Settlement.NOT_NEEDED)
    settlement = Settlement.PENDING if facts.claim_relation is ClaimRelation.CURRENT_CLAIM else Settlement.NOT_NEEDED
    return AccountingDecision(AttentionOutcome.INFRASTRUCTURE_FAILURE, AccountingBasis.INFRASTRUCTURE, False, settlement, facts.evidence_quality)


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
    schema_version: int = ATTENTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ATTENTION_SCHEMA_VERSION or self.issue_id <= 0:
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

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["kind"] = self.kind.value
        value["cause"] = self.cause.value if self.cause else None
        value["source"] = self.source.value
        value["outcome"] = self.outcome.value
        value["accounting_basis"] = self.accounting_basis.value
        value["settlement"] = self.settlement.value
        value["evidence_quality"] = self.evidence_quality.value
        value["primary_source"] = self.primary_source.value if self.primary_source else None
        value["handling_disposition"] = self.handling_disposition.value if self.handling_disposition else None
        value["retirement"] = self.retirement.value if self.retirement else None
        value["validation_detail"] = self.validation_detail.value if self.validation_detail else None
        if self.claim is not None:
            value["claim"] = {
                "issue_id": self.claim.issue_id,
                "attempt": self.claim.attempt,
                "agent_id": self.claim.agent_id,
                "claimed_at": self.claim.claimed_at.isoformat(),
            }
        value["reason"] = redact_text(self.reason)[:MAX_REASON_CHARS]
        value["refs"] = {str(key): redact_text(str(item))[:MAX_SAFE_REF_CHARS] if item is not None else None for key, item in self.refs.items()}
        return value

    @classmethod
    def from_json(cls, value: Mapping[str, Any], *, legacy: bool = False) -> AttentionRecord:
        claim_data = value.get("claim")
        claim = ClaimRecord.from_payload(dict(claim_data)) if isinstance(claim_data, Mapping) else None
        source = AttentionSource.LEGACY_V1 if legacy else AttentionSource(str(value["source"]))
        cause = AttentionCause.LEGACY_UNKNOWN if legacy else AttentionCause(str(value["cause"])) if value.get("cause") else None
        record = cls.__new__(cls)
        fields = {
            "occurrence_id": str(value["occurrence_id"]),
            "delivery_key": str(value["delivery_key"]),
            "kind": AttentionKind(str(value.get("kind") or AttentionKind.ATTENTION)),
            "issue_id": int(value["issue_id"]),
            "execution_id": str(value.get("execution_id") or value["occurrence_id"]),
            "source": source,
            "outcome": AttentionOutcome(str(value.get("outcome") or AttentionOutcome.LEGACY_UNKNOWN)),
            "accounting_basis": AccountingBasis(str(value.get("accounting_basis") or AccountingBasis.LEGACY_UNKNOWN)),
            "attempt_consumed": value.get("attempt_consumed"),
            "settlement": Settlement(str(value.get("settlement") or Settlement.NOT_NEEDED)),
            "cause": cause,
            "run_id": value.get("run_id"),
            "launch_id": value.get("launch_id"),
            "claim": claim,
            "error_signature": str(value.get("error_signature") or value.get("signature") or ""),
            "attempt": int(value["attempt"]) if value.get("attempt") is not None else None,
            "reason": str(value.get("reason") or value.get("terminal_error") or ""),
            "evidence_quality": EvidenceQuality(str(value.get("evidence_quality") or EvidenceQuality.VALID)),
            "original_result_status": value.get("original_result_status"),
            "original_factory_status": value.get("original_factory_status"),
            "primary_source": AttentionSource(str(value["primary_source"])) if value.get("primary_source") else None,
            "secondary_faults": tuple(value.get("secondary_faults") or ()),
            "refs": dict(value.get("refs") or {}),
            "factory_projection": value.get("factory_projection"),
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
            "schema_version": ATTENTION_SCHEMA_VERSION,
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
    ):
        try:
            values[name] = _invoke(reader, *args)
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")
    issue = values.get("issue")
    labels = _labels(issue)
    comments = _comments(issue)
    current["labels"] = sorted(labels) if labels is not None else None
    current["occurrence_comment"] = any(occurrence_id in text for text in comments) if comments is not None else None
    old_owner = values.get("run_state") or values.get("factory")
    process_dead: bool | None = None
    if old_owner is not None:
        try:
            observed = _invoke(readers.process, old_owner)
            process_dead = observed is False or observed == "verified_dead"
        except Exception as exc:
            errors.append(f"process:{type(exc).__name__}")
    else:
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
    return _tri(type(attempt) is int and attempt > record.attempt)


def _publication(record: AttentionRecord, evidence: Any, readers: AttentionReaders, errors: list[str]) -> Resolution:
    if not isinstance(evidence, Mapping) or evidence.get("status") != "completed":
        return Resolution.UNKNOWN if evidence is None else Resolution.UNRESOLVED
    url = evidence.get("pr_url")
    if not isinstance(url, str) or not url:
        return Resolution.UNRESOLVED
    try:
        pr = _invoke(readers.pull_request, url)
    except Exception as exc:
        errors.append(f"pull_request:{type(exc).__name__}")
        return Resolution.UNKNOWN
    state = pr.get("state") if isinstance(pr, Mapping) else None
    head = pr.get("headRefOid") if isinstance(pr, Mapping) else None
    expected = evidence.get("head") or evidence.get("head_sha")
    return _tri(state in {"OPEN", "MERGED"} and isinstance(expected, str) and head == expected)


def _factory_advanced(record: AttentionRecord, factory: Any, process_dead: bool | None) -> Resolution:
    if factory is None:
        return Resolution.UNKNOWN
    status = getattr(factory, "status", None)
    attempt = getattr(factory, "attempt", None)
    if isinstance(factory, Mapping):
        status = factory.get("status")
        attempt = factory.get("attempt")
    if status is not None and not isinstance(status, str):
        status = getattr(status, "status", None)
    advanced = type(attempt) is int and record.attempt is not None and attempt > record.attempt
    advanced = advanced or status in {"completed", "review_ready"}
    return _tri(bool(advanced and process_dead is True), process_dead is None)


def _budget_available(claims: Any) -> Resolution:
    if not isinstance(claims, Mapping):
        return Resolution.UNKNOWN
    used, maximum = claims.get("attempts_used"), claims.get("max_attempts")
    if type(used) is not int or type(maximum) is not int:
        return Resolution.UNKNOWN
    return _tri(used < maximum)


def _rearmed(record: AttentionRecord, issue: Any, claims: Any) -> Resolution:
    if isinstance(claims, Mapping) and claims.get("explicit_claim_after") is True:
        return Resolution.RESOLVED
    labels = _labels(issue)
    if labels is None:
        return Resolution.UNKNOWN
    witnessed = isinstance(claims, Mapping) and (claims.get("ready_removed_after") is True or claims.get("honored_reset_after") is True)
    return _tri("worklink:ready" in labels and witnessed)


def _applicable_predicates(record: AttentionRecord, values: Mapping[str, Resolution]) -> tuple[Resolution, ...]:
    if record.kind is not AttentionKind.ATTENTION:
        return (values["superseded"], values["publication_resolved"], values["factory_advanced"])
    if record.cause is AttentionCause.ATTEMPTS_EXHAUSTED:
        return (values["budget_available"], values["disarmed"], values["superseded"])
    if record.source.value.startswith("factory_") or record.source.value.startswith("epic_") or record.source is AttentionSource.ORPHAN_EPIC:
        return (values["factory_advanced"], values["publication_resolved"], values["disarmed"], values["superseded"], values["explicit_rearm"])
    return (values["publication_resolved"], values["disarmed"], values["superseded"], values["explicit_rearm"])


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
