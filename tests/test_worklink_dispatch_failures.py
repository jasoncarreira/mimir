from __future__ import annotations

from mimir.worklink.attention import (
    AccountingBasis,
    AttentionCause,
    AttentionKind,
    AttentionOutcome,
    AttentionRecord,
    AttentionSource,
    Settlement,
)
from mimir.worklink.dispatch_failures import (
    close_reservation_excluded,
    load_failure_state,
    pending_attention_records,
    promote_reservation,
    reserve_execution,
)


def test_reservation_exclusion_is_a_tombstone_without_occurrence(tmp_path):
    reservation = reserve_execution(
        tmp_path,
        issue_id=17,
        source="leaf_claim",
        operation_stage="claim",
        execution_id="execution",
    )
    close_reservation_excluded(
        tmp_path, 17, reservation["reservation_id"], witness="benign refusal"
    )
    issue = load_failure_state(tmp_path)["issues"]["17"]
    assert issue["reservations"][reservation["reservation_id"]]["closure"] == "excluded"
    assert issue["occurrences"] == {}
    assert pending_attention_records(tmp_path) == []


def test_atomic_promotion_replays_same_occurrence(tmp_path):
    reservation = reserve_execution(
        tmp_path,
        issue_id=17,
        source="leaf_run_boundary",
        operation_stage="run",
        execution_id="execution",
    )
    record = AttentionRecord(
        occurrence_id="occurrence",
        delivery_key="worklink-attention:17:signature:occurrence",
        kind=AttentionKind.ATTENTION,
        cause=AttentionCause.CONTROLLER_FAILED,
        issue_id=17,
        execution_id="execution",
        source=AttentionSource.LEAF_RUN_BOUNDARY,
        outcome=AttentionOutcome.INFRASTRUCTURE_FAILURE,
        accounting_basis=AccountingBasis.INFRASTRUCTURE,
        attempt_consumed=False,
        settlement=Settlement.NOT_NEEDED,
        error_signature="signature",
    )
    first = promote_reservation(
        tmp_path, 17, reservation["reservation_id"], record
    )
    second = promote_reservation(
        tmp_path, 17, reservation["reservation_id"], record
    )
    assert first == second
    assert [item["occurrence_id"] for item in pending_attention_records(tmp_path)] == [
        "occurrence"
    ]
