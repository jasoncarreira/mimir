from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import importlib
import json
from types import SimpleNamespace

import pytest

from mimir.worklink.attention import (
    AccountingBasis,
    AttentionFacts,
    AttentionKind,
    AttentionOutcome,
    ClaimRelation,
    AttentionCause,
    AttentionRecord,
    AttentionReaders,
    AttentionSource,
    EvidenceQuality,
    Resolution,
    AttentionSnapshot,
    Settlement,
    _SOURCE_POLICIES,
    _exact_evidence,
    _expected_source_cause,
    _rearmed,
    _source_clearance,
    classify_attention,
    inspect_attention,
)
from mimir.worklink.dispatch_failures import (
    dispatch_failure_state_dir,
    load_failure_state,
    promote_reservation,
    reserve_execution,
)


@pytest.mark.parametrize(
    ("facts", "outcome", "basis", "consumed", "settlement"),
    [
        (AttentionFacts(kind=AttentionKind.FACTORY_STARTED), AttentionOutcome.STARTED, AccountingBasis.PRECLAIM, None, Settlement.NOT_NEEDED),
        (AttentionFacts(exhaustion=True), AttentionOutcome.ATTEMPTS_EXHAUSTED, AccountingBasis.EXHAUSTION, False, Settlement.NOT_NEEDED),
        (AttentionFacts(preclaim=True), AttentionOutcome.INFRASTRUCTURE_FAILURE, AccountingBasis.PRECLAIM, False, Settlement.NOT_NEEDED),
        (AttentionFacts(primary_outcome=AttentionOutcome.PARTIAL, factory_status={"status": "partial", "pr_url": None, "steps": [{"agent": "build", "status": "merged", "attempts": 1}], "slices": None}), AttentionOutcome.PARTIAL, AccountingBasis.FACTORY_PARTIAL, True, Settlement.NOT_NEEDED),
        (AttentionFacts(verified_completion=True), AttentionOutcome.SUCCEEDED, AccountingBasis.VERIFIED_COMPLETION, True, Settlement.NOT_NEEDED),
        (AttentionFacts(factory_status={"status": "blocked", "pr_url": "https://example/pr/1", "steps": None, "slices": None}), AttentionOutcome.BLOCKED, AccountingBasis.FACTORY_PR, True, Settlement.NOT_NEEDED),
        (AttentionFacts(factory_status={"status": "blocked", "pr_url": None, "steps": [{"agent": "spec", "status": "rejected", "attempts": 1}], "slices": None}), AttentionOutcome.BLOCKED, AccountingBasis.FACTORY_STEPS, True, Settlement.NOT_NEEDED),
        (AttentionFacts(factory_status={"status": "blocked", "pr_url": None, "steps": [], "slices": [{"id": "one", "status": "merged", "attempts": 1}]}), AttentionOutcome.BLOCKED, AccountingBasis.FACTORY_SLICES, True, Settlement.NOT_NEEDED),
        (AttentionFacts(normalized_leaf_result=True), AttentionOutcome.GENUINE_FAILURE, AccountingBasis.LEAF_EXECUTION, True, Settlement.NOT_NEEDED),
        (AttentionFacts(unpublished_commits=True), AttentionOutcome.GENUINE_FAILURE, AccountingBasis.UNPUBLISHED_COMMITS, True, Settlement.NOT_NEEDED),
        (AttentionFacts(factory_status={"status": "blocked", "pr_url": None, "steps": None, "slices": None}, claim_relation=ClaimRelation.CURRENT_CLAIM), AttentionOutcome.INFRASTRUCTURE_FAILURE, AccountingBasis.INFRASTRUCTURE, False, Settlement.PENDING),
    ],
)
def test_accounting_truth_table(facts, outcome, basis, consumed, settlement):
    decision = classify_attention(facts)
    assert decision.outcome is outcome
    assert decision.basis is basis
    assert decision.attempt_consumed is consumed
    assert decision.settlement is settlement


@pytest.mark.parametrize("phase", ["running", "failed", "parked", "stopped", "terminal", "unknown"])
@pytest.mark.parametrize("controller_error", [None, "opaque diagnostic"])
def test_factory_phase_and_error_do_not_invent_work(phase, controller_error):
    decision = classify_attention(
        AttentionFacts(
            factory_status={
                "status": "blocked",
                "pr_url": None,
                "steps": [],
                "slices": [],
                "controller_phase": phase,
                "controller_error": controller_error,
            },
            claim_relation=ClaimRelation.CURRENT_CLAIM,
        )
    )
    assert decision.outcome is AttentionOutcome.INFRASTRUCTURE_FAILURE
    assert decision.basis is AccountingBasis.INFRASTRUCTURE
    assert decision.attempt_consumed is False
    assert decision.settlement is Settlement.PENDING


@pytest.mark.parametrize(
    "quality",
    [
        EvidenceQuality.NULL,
        EvidenceQuality.EMPTY,
        EvidenceQuality.MISSING,
        EvidenceQuality.MALFORMED,
        EvidenceQuality.UNAVAILABLE,
        EvidenceQuality.FOREIGN,
        EvidenceQuality.STALE,
    ],
)
def test_invalid_or_foreign_evidence_is_nonconsuming(quality):
    decision = classify_attention(
        AttentionFacts(evidence_quality=quality, claim_relation=ClaimRelation.CURRENT_CLAIM)
    )
    assert decision == decision.__class__(
        AttentionOutcome.INFRASTRUCTURE_FAILURE,
        AccountingBasis.INFRASTRUCTURE,
        False,
        Settlement.PENDING,
        quality,
    )


def test_partial_precedes_verified_completion_and_malformed_rows_do_not_count():
    partial = classify_attention(
        AttentionFacts(
            kind=AttentionKind.FACTORY_SUCCEEDED,
            primary_outcome=AttentionOutcome.PARTIAL,
            accepted_factory_status={
                "status": "partial", "pr_url": None,
                "steps": [{"agent": "build", "status": "merged", "attempts": 1}],
                "slices": None,
            },
            verified_completion=True,
        )
    )
    malformed = classify_attention(
        AttentionFacts(
            factory_status={
                "status": "blocked", "pr_url": None,
                "steps": [{"agent": "spec"}], "slices": [{"id": "slice"}],
            },
            claim_relation=ClaimRelation.CURRENT_CLAIM,
        )
    )
    assert (partial.outcome, partial.basis) == (
        AttentionOutcome.PARTIAL, AccountingBasis.FACTORY_PARTIAL,
    )
    assert (malformed.basis, malformed.attempt_consumed) == (
        AccountingBasis.INFRASTRUCTURE, False,
    )


def _attention_record(source: AttentionSource) -> AttentionRecord:
    lifecycle = source in {
        AttentionSource.FACTORY_INITIAL_START,
        AttentionSource.FACTORY_RECOVERY_START,
        AttentionSource.FACTORY_SUCCESS,
    }
    kind = (
        AttentionKind.FACTORY_SUCCEEDED
        if source is AttentionSource.FACTORY_SUCCESS
        else AttentionKind.FACTORY_STARTED if lifecycle
        else AttentionKind.ATTENTION
    )
    return AttentionRecord(
        occurrence_id=f"occurrence-{source.value}",
        delivery_key=f"worklink-attention:17:signature:{'occurrence-' + source.value}",
        kind=kind,
        cause=_expected_source_cause(source),
        issue_id=17,
        execution_id=f"execution-{source.value}",
        source=source,
        outcome=(
            AttentionOutcome.SUCCEEDED if source is AttentionSource.FACTORY_SUCCESS
            else AttentionOutcome.STARTED if lifecycle
            else AttentionOutcome.INFRASTRUCTURE_FAILURE
        ),
        accounting_basis=(
            AccountingBasis.VERIFIED_COMPLETION
            if kind is AttentionKind.FACTORY_SUCCEEDED
            else AccountingBasis.PRECLAIM
        ),
        attempt_consumed=True if kind is AttentionKind.FACTORY_SUCCEEDED else None if lifecycle else False,
        settlement=Settlement.NOT_NEEDED,
        error_signature="signature",
        attempt=1,
        run_id="chainlink-17" if lifecycle else None,
        launch_id="launch-17" if lifecycle else None,
        refs={"target_label": "worklink:blocked"},
        next=None,
        next_present=True,
        controller_phase="failed",
        controller_error="diagnostic",
        pr_url="https://example.test/pr/1",
        pr_state="OPEN",
        pr_head="abc",
        inhibited=not lifecycle,
    )


def test_publication_clearance_requires_complete_exact_execution_identity():
    record = replace(
        _attention_record(AttentionSource.LEAF_PUBLICATION_EVIDENCE),
        refs={"branch": "feature/17"},
    )
    incomplete = {
        "status": "completed",
        "branch": "feature/17",
        "pr_url": record.pr_url,
        "head_sha": record.pr_head,
    }
    exact = {**incomplete, "issue": 17, "attempt": 1}

    assert _exact_evidence(record, incomplete) is Resolution.UNRESOLVED
    assert _exact_evidence(record, {**exact, "attempt": 2}) is Resolution.UNRESOLVED
    assert _exact_evidence(record, exact) is Resolution.RESOLVED


@pytest.mark.parametrize(
    "source",
    [
        AttentionSource.EPIC_REPOSITORY,
        AttentionSource.EPIC_COMPUTE,
        AttentionSource.EPIC_BASE,
    ],
)
def test_repository_compute_and_base_clear_only_from_their_current_predicate(source):
    record = _attention_record(source)
    unrelated_factory = {
        "run_id": "other",
        "issue_id": record.issue_id,
        "attempt": record.attempt,
        "execution_id": record.execution_id,
        "launch_id": record.launch_id,
    }

    assert _source_clearance(
        record,
        {"factory": unrelated_factory, "source_state": {"predicate_matches": False}},
        {"worklink:in-progress"},
        (),
        True,
    ) is Resolution.UNRESOLVED
    assert _source_clearance(
        record,
        {"factory": unrelated_factory, "source_state": {"predicate_matches": True}},
        {"worklink:in-progress"},
        (),
        True,
    ) is Resolution.RESOLVED


@pytest.mark.parametrize(
    "source",
    [
        AttentionSource.EPIC_CONTROLLER_RELOAD,
        AttentionSource.EPIC_DRIVER_LOCK_SAVE,
        AttentionSource.FACTORY_SUCCESS,
    ],
)
def test_factory_clearance_requires_exact_identity_and_original_predicate(source):
    record = replace(_attention_record(source), run_id="chainlink-17")
    factory = {
        "run_id": record.run_id,
        "issue_id": record.issue_id,
        "attempt": record.attempt,
        "execution_id": record.execution_id,
        "launch_id": record.launch_id,
        "controller_phase": "parked",
        "controller_error": "saved refusal",
        "status": "completed",
    }

    assert _source_clearance(
        record,
        {"factory": factory, "source_state": {"original_predicate_resolved": False}},
        {"worklink:review"},
        (),
        True,
    ) is Resolution.UNRESOLVED
    assert _source_clearance(
        record,
        {"factory": factory, "source_state": {"original_predicate_resolved": True}},
        {"worklink:review"},
        (),
        True,
    ) is Resolution.RESOLVED


def test_orphan_label_uncertainty_requires_a_fresh_nonblocked_lifecycle():
    record = _attention_record(AttentionSource.ORPHAN_LABELS_UNKNOWN)

    assert _source_clearance(
        record, {"issue": object()}, {"worklink:blocked"}, (), True,
    ) is Resolution.UNRESOLVED
    assert _source_clearance(
        record, {"issue": object()}, {"worklink:ready"}, (), True,
    ) is Resolution.RESOLVED


def test_rearm_requires_a_witness_later_than_the_occurrence_baseline():
    baseline = {
        "issue_id": 17,
        "attempt": 1,
        "agent_id": "manual",
        "claimed_at": "2026-09-17T00:00:00+00:00",
    }
    record = replace(
        _attention_record(AttentionSource.LEAF_CLAIM),
        reset_generation_baseline=3,
        ready_cycle_baseline=5,
        manual_claim_baseline=baseline,
    )
    claims = {
        "reset_generation": 3,
        "ready_cycle_generation": 5,
        "manual_claim_witness": baseline,
    }

    assert _rearmed(record, {"labels": ["worklink:blocked"]}, claims) is Resolution.UNRESOLVED
    assert _rearmed(
        record, {"labels": ["worklink:blocked"]}, {**claims, "reset_generation": 4},
    ) is Resolution.RESOLVED
    assert _rearmed(
        record, {"labels": ["worklink:blocked"]}, {**claims, "ready_cycle_generation": 6},
    ) is Resolution.RESOLVED
    assert _rearmed(
        record,
        {"labels": ["worklink:blocked"]},
        {**claims, "manual_claim_witness": {**baseline, "claimed_at": "2026-09-18T00:00:00+00:00"}},
    ) is Resolution.RESOLVED
    assert _rearmed(
        record,
        {"labels": ["worklink:blocked"]},
        {
            **claims,
            "latest": {**baseline, "claimed_at": "2026-09-19T00:00:00+00:00"},
        },
    ) is Resolution.UNRESOLVED


@pytest.mark.parametrize(
    "source",
    [AttentionSource.ORPHAN_UNPUBLISHED, AttentionSource.ORPHAN_AMBIGUOUS],
)
def test_orphan_publication_clearance_requires_clean_checkout_and_dead_owner(source):
    record = _attention_record(source)
    values = {
        "issue": {"labels": ["worklink:ready"]},
        "run_state": None,
        "source_state": {
            "publication_outcome": "determined-clean",
            "old_owner_verified_dead": True,
        },
    }
    assert _source_clearance(
        record, values, {"worklink:ready"}, (), True,
    ) is Resolution.RESOLVED
    assert _source_clearance(
        record,
        {
            **values,
            "source_state": {
                "publication_outcome": "determined-unpublished",
                "old_owner_verified_dead": True,
            },
        },
        {"worklink:ready"},
        (),
        True,
    ) is Resolution.UNRESOLVED
    assert _source_clearance(
        record,
        {**values, "source_state": {"old_owner_verified_dead": True}},
        {"worklink:ready"},
        (),
        True,
    ) is Resolution.UNKNOWN


def test_completed_state_clear_requires_publication_and_old_owner_clearance():
    record = _attention_record(AttentionSource.LEAF_COMPLETED_STATE_CLEAR)
    values = {
        "run_state": None,
        "claims": {"lock_absent": True},
        "publication_resolution": Resolution.RESOLVED,
        "source_state": {"old_owner_verified_dead": True},
    }
    assert _source_clearance(
        record, values, {"worklink:blocked"}, (), True,
    ) is Resolution.RESOLVED
    assert _source_clearance(
        record,
        {**values, "publication_resolution": Resolution.UNRESOLVED},
        {"worklink:blocked"},
        (),
        True,
    ) is Resolution.UNRESOLVED
    assert _source_clearance(
        record,
        {**values, "source_state": {}},
        {"worklink:blocked"},
        (),
        True,
    ) is Resolution.UNKNOWN


def test_preservation_clearance_requires_current_ref_to_match_captured_head():
    record = _attention_record(AttentionSource.EPIC_PRESERVATION)
    assert _source_clearance(
        record,
        {"source_state": {"preserved_ref_matches": True}},
        {"worklink:blocked"},
        (),
        True,
    ) is Resolution.RESOLVED
    assert _source_clearance(
        record,
        {"source_state": {"preserved_ref_matches": False}},
        {"worklink:blocked"},
        (),
        True,
    ) is Resolution.UNRESOLVED
    assert _source_clearance(
        record,
        {},
        {"worklink:blocked"},
        (),
        True,
    ) is Resolution.UNKNOWN


def test_every_production_source_declares_clearance_predicates():
    assert set(_SOURCE_POLICIES) == set(AttentionSource) - {AttentionSource.LEGACY_V1}


PRODUCER_RESOLUTION_SOURCES = (
    AttentionSource.DETACHED_SPAWN,
    AttentionSource.LEAF_TEMPLATE,
    AttentionSource.TEMPLATE_UNREADY,
    AttentionSource.TEMPLATE_BLOCK_LABEL,
    AttentionSource.TEMPLATE_COMMENT,
    AttentionSource.LEAF_COMPUTE,
    AttentionSource.LEAF_CLAIM,
    AttentionSource.LEAF_EXHAUSTION,
    AttentionSource.LEAF_CHECKOUT,
    AttentionSource.LEAF_LAUNCH,
    AttentionSource.LEAF_RUNSTATE_SAVE,
    AttentionSource.LEAF_COMPUTE_CLEANUP,
    AttentionSource.LEAF_BACKEND_BLOCKED,
    AttentionSource.LEAF_GATE_TIMEOUT,
    AttentionSource.LEAF_GATE_MISSING,
    AttentionSource.LEAF_OUTPUT_OVERFLOW,
    AttentionSource.LEAF_WORK_FAILED,
    AttentionSource.LEAF_TRANSITION,
    AttentionSource.LEAF_RELEASE,
    AttentionSource.LEAF_CHECKOUT_CLEANUP,
    AttentionSource.LEAF_CAPABILITY_CLEANUP,
    AttentionSource.LEAF_POSTCLAIM,
    AttentionSource.LEAF_ERROR_TRANSITION,
    AttentionSource.LEAF_RUN_BOUNDARY,
    AttentionSource.LEAF_PUBLICATION_FENCE,
    AttentionSource.LEAF_PUBLICATION_PUSH,
    AttentionSource.LEAF_PUBLICATION_PR,
    AttentionSource.LEAF_PUBLICATION_EVIDENCE,
    AttentionSource.LEAF_COMPLETED_EVIDENCE_WRITE,
    AttentionSource.LEAF_EVIDENCE_COMMENT,
    AttentionSource.LEAF_COMPLETED_STATE_CLEAR,
    AttentionSource.EPIC_LABEL,
    AttentionSource.EPIC_TEMPLATE,
    AttentionSource.EPIC_BACKEND,
    AttentionSource.EPIC_REPOSITORY,
    AttentionSource.EPIC_COMPUTE,
    AttentionSource.EPIC_FACTORY_ADMIT,
    AttentionSource.EPIC_BASE,
    AttentionSource.EPIC_RETAINED_BIND,
    AttentionSource.EPIC_RETAINED_ISSUE_RELOAD,
    AttentionSource.EPIC_RETAINED_TRANSITION,
    AttentionSource.EPIC_CLAIM,
    AttentionSource.EPIC_EXHAUSTION,
    AttentionSource.EPIC_LAUNCH,
    AttentionSource.EPIC_RECOVERY,
    AttentionSource.EPIC_SUPERVISION,
    AttentionSource.EPIC_DRIVER_LOCK,
    AttentionSource.EPIC_DRIVER_LOCK_SAVE,
    AttentionSource.EPIC_DRIVER_LOCK_TRANSITION,
    AttentionSource.FACTORY_NEEDS_HUMAN,
    AttentionSource.FACTORY_BLOCKED,
    AttentionSource.FACTORY_PARTIAL,
    AttentionSource.FACTORY_COMPLETION_VERIFY,
    AttentionSource.FACTORY_TERMINAL_TRANSITION,
    AttentionSource.EPIC_CONTROLLER,
    AttentionSource.EPIC_CONTROLLER_RELOAD,
    AttentionSource.EPIC_PRESERVATION,
    AttentionSource.EPIC_ERROR_SAVE,
    AttentionSource.EPIC_ERROR_TRANSITION,
    AttentionSource.EPIC_CANCEL,
    AttentionSource.EPIC_WAIT_DRAIN,
    AttentionSource.EPIC_TRANSCRIPT_SAVE,
    AttentionSource.EPIC_COMPUTE_CLEANUP,
    AttentionSource.EPIC_RELEASE,
    AttentionSource.EPIC_RUN_BOUNDARY,
    AttentionSource.ORPHAN_UNPUBLISHED,
    AttentionSource.ORPHAN_AMBIGUOUS,
    AttentionSource.ORPHAN_EPIC,
    AttentionSource.ORPHAN_LABELS_UNKNOWN,
    AttentionSource.ORPHAN_LOCK_RELEASE,
    AttentionSource.ORPHAN_COMMENT,
    AttentionSource.ORPHAN_TARGET_LABEL,
    AttentionSource.ORPHAN_INPROGRESS_UNLABEL,
    AttentionSource.ORPHAN_STATE_UPDATE,
    AttentionSource.STARTUP_LEAF_SPAWN,
    AttentionSource.STARTUP_FACTORY_SPAWN,
    AttentionSource.STARTUP_RUN_RECORD_READ,
    AttentionSource.STARTUP_FACTORY_RECORD_READ,
    AttentionSource.EXECUTION_RECOVERY,
    AttentionSource.FACTORY_INITIAL_START,
    AttentionSource.FACTORY_RECOVERY_START,
    AttentionSource.FACTORY_SUCCESS,
)


@pytest.mark.parametrize("source", PRODUCER_RESOLUTION_SOURCES, ids=lambda item: item.value)
def test_producer_resolution_matrix_executes_ready_occurrence_positive_negative_error(
    tmp_path, source,
):
    import mimir.worklink.orchestrator as orchestrator
    from mimir.worklink.factory_state import FactoryRunRecord

    state_dir = dispatch_failure_state_dir(tmp_path)
    if source in {
        AttentionSource.FACTORY_INITIAL_START,
        AttentionSource.FACTORY_RECOVERY_START,
    }:
        sandbox = tmp_path / "chainlink-17"
        sandbox.mkdir()
        orchestrator._record_factory_started(
            tmp_path,
            FactoryRunRecord(
                run_id="chainlink-17",
                issue_id=17,
                attempt=1,
                repository="owner/repo",
                base_ref="main",
                branch="feature/17",
                launcher="/opt/factory/factory.js",
                sandbox=str(sandbox),
                session=None,
                handle=None,
                status=None,
                observed_at=None,
                controller_phase="starting",
                autonomous=True,
                execution_id=f"execution-{source.value}",
                launch_id="launch-17",
            ),
            recovery=source is AttentionSource.FACTORY_RECOVERY_START,
        )
        payload = next(iter(load_failure_state(state_dir)["issues"]["17"]["occurrences"].values()))
        record = AttentionRecord.from_json(payload)
    else:
        reservation = reserve_execution(
            state_dir,
            issue_id=17,
            source=source.value,
            operation_stage="terminal",
            execution_id=f"execution-{source.value}",
            run_id="chainlink-17" if source is AttentionSource.FACTORY_SUCCESS else None,
            launch_id="launch-17" if source is AttentionSource.FACTORY_SUCCESS else None,
        )
        record = orchestrator._record_attention_result(
            tmp_path,
            orchestrator.WorklinkRunResult(
                17,
                1,
                "review_ready" if source is AttentionSource.FACTORY_SUCCESS else "failed",
                reason=f"{source.value} boundary",
                run_id="chainlink-17" if source is AttentionSource.FACTORY_SUCCESS else None,
                launch_id="launch-17" if source is AttentionSource.FACTORY_SUCCESS else None,
                target_label="worklink:blocked",
            ),
            reservation,
            source=source.value,
            lifecycle_success=source is AttentionSource.FACTORY_SUCCESS,
        )

    def readers(*, newer: bool, errors: bool = False):
        if errors:
            def unavailable(*args):
                raise OSError("read unavailable")

            return AttentionReaders(
                issue=unavailable,
                claims=unavailable,
                run_state=unavailable,
                factory_record=unavailable,
                process=unavailable,
                evidence=unavailable,
                pull_request=unavailable,
                source_state=unavailable,
            )
        return AttentionReaders(
            issue=lambda issue_id: orchestrator.IssueContext(
                issue_id,
                "invalid template",
                "missing target branch and planner sections",
                {"worklink:blocked", "worklink:in-progress", "worklink:ready"},
            ),
            claims=lambda issue_id: {
                "locks": [issue_id],
                "lock_absent": False,
                "latest": {
                    "issue_id": issue_id,
                    "attempt": 2 if newer else 1,
                    "agent_id": "other" if newer else "agent",
                    "claimed_at": "2026-09-18T00:00:00+00:00",
                },
                "attempts_used": 3,
                "max_attempts": 3,
                "reset_generation": record.reset_generation_baseline,
                "ready_cycle_generation": record.ready_cycle_baseline,
                "manual_claim_witness": record.manual_claim_baseline,
            },
            run_state=lambda issue_id: {"issue_id": issue_id, "attempt": 1},
            factory_record=lambda run_id, issue_id: {
                "run_id": "foreign",
                "issue_id": issue_id,
                "attempt": 1,
                "execution_id": "foreign",
                "status": "failed",
            },
            process=lambda owner: "verified_dead" if newer else "alive",
            evidence=lambda occurrence: {},
            pull_request=lambda url: {"state": "CLOSED", "headRefOid": "foreign"},
            source_state=lambda occurrence: {
                "predicate_matches": False,
                "original_predicate_resolved": False,
                "preserved_ref_matches": False,
                "publication_outcome": "determined-unpublished",
                "old_owner_verified_dead": False,
            },
        )

    positive = inspect_attention(
        tmp_path, 17, record.error_signature, record.occurrence_id,
        readers(newer=True),
    )
    negative = inspect_attention(
        tmp_path, 17, record.error_signature, record.occurrence_id,
        readers(newer=False),
    )
    failed_read = inspect_attention(
        tmp_path, 17, record.error_signature, record.occurrence_id,
        readers(newer=False, errors=True),
    )
    assert positive.resolution is Resolution.RESOLVED
    assert positive.predicates["superseded"] is Resolution.RESOLVED
    assert negative.resolution is Resolution.UNRESOLVED
    assert failed_read.resolution is Resolution.UNKNOWN


def _ack_context(tmp_path):
    context = SimpleNamespace(origin_ref="origin")
    turn = SimpleNamespace(turn_id="turn")
    service = SimpleNamespace()
    return turn, context, service, tmp_path, SimpleNamespace(), lambda *_args: True


@pytest.mark.asyncio
async def test_resolved_operator_ack_is_noop_without_delivery(tmp_path, monkeypatch):
    from mimir.tools import registry

    state_dir = dispatch_failure_state_dir(tmp_path)
    record = _attention_record(AttentionSource.LEAF_CLAIM)
    reservation = reserve_execution(
        state_dir, issue_id=17, source="leaf_claim", operation_stage="terminal",
        execution_id=record.execution_id,
    )
    promote_reservation(state_dir, 17, reservation["reservation_id"], record)
    snapshot = AttentionSnapshot(record, Resolution.RESOLVED, {}, {})
    monkeypatch.setattr(registry, "_worklink_attention_context", lambda *args: _ack_context(tmp_path))
    monkeypatch.setattr("mimir.worklink.attention.inspect_attention", lambda *args: snapshot)
    alert_module = importlib.import_module("mimir.tools.operator_alert")
    monkeypatch.setattr(
        alert_module,
        "deliver_operator_alert",
        lambda *args, **kwargs: pytest.fail("resolved occurrence sent an alert"),
    )
    result = json.loads(await registry.worklink_attention_ack.coroutine(
        17, "signature", record.occurrence_id, "operator_required", "do work"
    ))
    assert result["status"] == "handled"
    assert result["disposition"] == "noop_resolved"
    stored = load_failure_state(state_dir)["issues"]["17"]["occurrences"][record.occurrence_id]
    assert stored["handling_disposition"] == "noop_resolved"


@pytest.mark.asyncio
async def test_identical_committed_ack_repeats_without_send(tmp_path, monkeypatch):
    from mimir.tools import registry
    from mimir.worklink.dispatch_failures import get_attention_record

    state_dir = dispatch_failure_state_dir(tmp_path)
    record = _attention_record(AttentionSource.LEAF_CLAIM)
    reservation = reserve_execution(
        state_dir, issue_id=17, source="leaf_claim", operation_stage="terminal",
        execution_id=record.execution_id,
    )
    promote_reservation(state_dir, 17, reservation["reservation_id"], record)

    def inspect(home, issue_id, signature, occurrence_id, _readers):
        current = get_attention_record(home, issue_id, signature, occurrence_id)
        return AttentionSnapshot(current, Resolution.RESOLVED, {}, {})

    monkeypatch.setattr(registry, "_worklink_attention_context", lambda *args: _ack_context(tmp_path))
    monkeypatch.setattr("mimir.worklink.attention.inspect_attention", inspect)
    alert_module = importlib.import_module("mimir.tools.operator_alert")
    monkeypatch.setattr(
        alert_module,
        "deliver_operator_alert",
        lambda *args, **kwargs: pytest.fail("committed noop acknowledgement sent an alert"),
    )

    first = json.loads(await registry.worklink_attention_ack.coroutine(
        17, "signature", record.occurrence_id, "noop_resolved", ""
    ))
    second = json.loads(await registry.worklink_attention_ack.coroutine(
        17, "signature", record.occurrence_id, "noop_resolved", ""
    ))
    assert first == second == {
        "disposition": "noop_resolved",
        "issue_id": 17,
        "occurrence_id": record.occurrence_id,
        "status": "handled",
    }


@pytest.mark.asyncio
async def test_operator_ack_serializes_send_and_redacts_note(tmp_path, monkeypatch):
    from mimir.tools import registry
    from mimir.tools.operator_alert import OperatorAlertReceipt

    state_dir = dispatch_failure_state_dir(tmp_path)
    record = _attention_record(AttentionSource.LEAF_CLAIM)
    reservation = reserve_execution(
        state_dir, issue_id=17, source="leaf_claim", operation_stage="terminal",
        execution_id=record.execution_id,
    )
    promote_reservation(state_dir, 17, reservation["reservation_id"], record)
    snapshot = AttentionSnapshot(record, Resolution.UNRESOLVED, {}, {})
    entered = asyncio.Event()
    release = asyncio.Event()
    sent = []

    async def deliver(text, *, authorize=False):
        sent.append((text, authorize))
        entered.set()
        await release.wait()
        return OperatorAlertReceipt("operator", "message", "2026-09-17T00:00:00+00:00", "hash")

    monkeypatch.setattr(registry, "_worklink_attention_context", lambda *args: _ack_context(tmp_path))
    monkeypatch.setattr("mimir.worklink.attention.inspect_attention", lambda *args: snapshot)
    alert_module = importlib.import_module("mimir.tools.operator_alert")
    monkeypatch.setattr(alert_module, "deliver_operator_alert", deliver)
    first = asyncio.create_task(registry.worklink_attention_ack.coroutine(
        17, "signature", record.occurrence_id, "operator_required",
        "token=top-secret requires repair",
    ))
    await entered.wait()
    second = json.loads(await registry.worklink_attention_ack.coroutine(
        17, "signature", record.occurrence_id, "operator_required", "same owner retry"
    ))
    release.set()
    completed = json.loads(await first)
    assert second["status"] == "handling_in_progress"
    assert completed["status"] == "handled"
    assert len(sent) == 1 and sent[0][1] is True
    assert "top-secret" not in sent[0][0]
    stored = load_failure_state(state_dir)["issues"]["17"]["occurrences"][record.occurrence_id]
    assert stored["handling"]["note"] == "token=[REDACTED] requires repair"
    assert stored["handling"]["lease_id"]
    assert stored["handling"]["origin_ref"] == "origin"
    assert stored["handling"]["destination"] == "operator"


@pytest.mark.asyncio
async def test_operator_ack_delivery_failure_releases_handling_lease(tmp_path, monkeypatch):
    from mimir.tools import registry
    from mimir.tools.operator_alert import OperatorAlertReceipt

    state_dir = dispatch_failure_state_dir(tmp_path)
    record = _attention_record(AttentionSource.LEAF_CLAIM)
    reservation = reserve_execution(
        state_dir, issue_id=17, source="leaf_claim", operation_stage="terminal",
        execution_id=record.execution_id,
    )
    promote_reservation(state_dir, 17, reservation["reservation_id"], record)
    snapshot = AttentionSnapshot(record, Resolution.UNRESOLVED, {}, {})
    attempts = 0

    async def deliver(_text, *, authorize=False):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("delivery failed")
        assert authorize is True
        return OperatorAlertReceipt("operator", "message", "2026-09-17T00:00:00+00:00", "hash")

    monkeypatch.setattr(registry, "_worklink_attention_context", lambda *args: _ack_context(tmp_path))
    monkeypatch.setattr("mimir.worklink.attention.inspect_attention", lambda *args: snapshot)
    alert_module = importlib.import_module("mimir.tools.operator_alert")
    monkeypatch.setattr(alert_module, "deliver_operator_alert", deliver)
    with pytest.raises(RuntimeError, match="delivery failed"):
        await registry.worklink_attention_ack.coroutine(
            17, "signature", record.occurrence_id, "operator_required", "repair"
        )
    stored = load_failure_state(state_dir)["issues"]["17"]["occurrences"][record.occurrence_id]
    assert "handling_lease" not in stored
    completed = json.loads(await registry.worklink_attention_ack.coroutine(
        17, "signature", record.occurrence_id, "operator_required", "repair"
    ))
    assert completed["status"] == "handled"
    assert attempts == 2


def test_reserved_execution_recovery_promotes_one_stable_occurrence(tmp_path):
    from mimir.worklink.control import reconcile_reserved_executions

    state_dir = dispatch_failure_state_dir(tmp_path)
    reservation = reserve_execution(
        state_dir,
        issue_id=18,
        source="leaf_run_boundary",
        operation_stage="claimed",
        execution_id="abandoned-execution",
    )
    first = reconcile_reserved_executions(
        tmp_path, active_execution_ids=set(), recover_recent=True
    )
    second = reconcile_reserved_executions(
        tmp_path, active_execution_ids=set(), recover_recent=True
    )
    issue = load_failure_state(state_dir)["issues"]["18"]
    assert len(first) == 1
    assert second == []
    assert list(issue["occurrences"]) == first
    occurrence = issue["occurrences"][first[0]]
    assert occurrence["source"] == "execution_recovery"
    assert occurrence["execution_id"] == reservation["execution_id"]
