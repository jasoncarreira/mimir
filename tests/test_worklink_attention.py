from __future__ import annotations

import asyncio
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
    AttentionSource,
    EvidenceQuality,
    Resolution,
    AttentionSnapshot,
    Settlement,
    _SOURCE_POLICIES,
    classify_attention,
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
        (AttentionFacts(primary_outcome=AttentionOutcome.PARTIAL), AttentionOutcome.PARTIAL, AccountingBasis.FACTORY_PARTIAL, True, Settlement.NOT_NEEDED),
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
        cause=None if lifecycle else AttentionCause.CONTROLLER_FAILED,
        issue_id=17,
        execution_id=f"execution-{source.value}",
        source=source,
        outcome=(
            AttentionOutcome.SUCCEEDED if source is AttentionSource.FACTORY_SUCCESS
            else AttentionOutcome.STARTED if lifecycle
            else AttentionOutcome.INFRASTRUCTURE_FAILURE
        ),
        accounting_basis=AccountingBasis.PRECLAIM,
        attempt_consumed=None if lifecycle else False,
        settlement=Settlement.NOT_NEEDED,
        error_signature="signature",
        attempt=1,
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


def test_every_production_source_declares_clearance_predicates():
    assert set(_SOURCE_POLICIES) == set(AttentionSource) - {AttentionSource.LEGACY_V1}


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
