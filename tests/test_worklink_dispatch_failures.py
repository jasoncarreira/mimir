from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
import json

import pytest

from mimir.worklink.attention import (
    AccountingBasis,
    AttentionCause,
    AttentionKind,
    AttentionOutcome,
    AttentionRecord,
    AttentionSource,
    Settlement,
    HandlingDisposition,
)
from mimir.worklink.dispatch_failures import (
    FailureStateError,
    acquire_handling_lease,
    close_reservation_excluded,
    issue_is_inhibited,
    load_failure_state,
    mark_attention_handled,
    observe_manual_claim_rearm,
    observe_rearm_state,
    observe_reset_rearm,
    pending_attention_records,
    promote_reservation,
    record_transient_contention,
    reserve_execution,
    save_failure_state,
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


def _record(issue_id: int = 17, **updates):
    values = {
        "occurrence_id": "occurrence",
        "delivery_key": f"worklink-attention:{issue_id}:signature:occurrence",
        "kind": AttentionKind.ATTENTION,
        "cause": AttentionCause.CLAIM_FAILED,
        "issue_id": issue_id,
        "execution_id": "execution",
        "source": AttentionSource.LEAF_CLAIM,
        "outcome": AttentionOutcome.INFRASTRUCTURE_FAILURE,
        "accounting_basis": AccountingBasis.PRECLAIM,
        "attempt_consumed": False,
        "settlement": Settlement.NOT_NEEDED,
        "error_signature": "signature",
    }
    values.update(updates)
    return AttentionRecord(**values)


def test_promoted_replay_returns_frozen_primary(tmp_path):
    reservation = reserve_execution(
        tmp_path, issue_id=17, source="leaf_claim", operation_stage="claim",
        execution_id="execution",
    )
    first = promote_reservation(tmp_path, 17, reservation["reservation_id"], _record(reason="first"))
    replay = promote_reservation(
        tmp_path, 17, reservation["reservation_id"],
        replace(_record(reason="second"), outcome=AttentionOutcome.BLOCKED),
    )
    assert replay == first
    assert replay["reason"] == "first"
    assert replay["outcome"] == "infrastructure_failure"


def test_handling_does_not_rearm_and_same_owner_cannot_double_lease(tmp_path):
    reservation = reserve_execution(
        tmp_path, issue_id=17, source="leaf_claim", operation_stage="claim",
        execution_id="execution",
    )
    promote_reservation(tmp_path, 17, reservation["reservation_id"], _record())
    lease = acquire_handling_lease(tmp_path, 17, "occurrence", owner="same-turn")
    assert lease
    assert acquire_handling_lease(tmp_path, 17, "occurrence", owner="same-turn") is None
    assert mark_attention_handled(
        tmp_path, 17, "signature", "occurrence",
        HandlingDisposition.OPERATOR_REQUIRED, lease_id=lease,
        metadata={"origin_ref": "origin", "turn_id": "turn"},
    )
    state = load_failure_state(tmp_path)
    occurrence = state["issues"]["17"]["occurrences"]["occurrence"]
    assert issue_is_inhibited(tmp_path, 17)
    assert occurrence["handling"]["lease_id"] == lease


def test_rearm_witnesses_are_independent_and_generation_monotonic(tmp_path):
    reservation = reserve_execution(
        tmp_path, issue_id=17, source="leaf_claim", operation_stage="claim",
        execution_id="execution",
    )
    promote_reservation(tmp_path, 17, reservation["reservation_id"], _record())
    assert observe_rearm_state(tmp_path, ready_issue_ids=set(), blocked_issue_ids=set()) == set()
    assert observe_rearm_state(tmp_path, ready_issue_ids={17}, blocked_issue_ids=set()) == {17}
    state = load_failure_state(tmp_path)
    assert state["issues"]["17"]["arming_generation"] == 1
    state["issues"]["17"]["inhibited"] = True
    state["issues"]["17"]["reset_generation"] = 1
    save_failure_state(tmp_path, state)
    assert observe_reset_rearm(tmp_path, 17, 2)
    state = load_failure_state(tmp_path)
    state["issues"]["17"]["inhibited"] = True
    save_failure_state(tmp_path, state)
    claim = {
        "issue_id": 17, "attempt": 2, "agent_id": "operator",
        "claimed_at": datetime.now(UTC).isoformat(),
    }
    assert observe_manual_claim_rearm(tmp_path, 17, claim)
    assert load_failure_state(tmp_path)["issues"]["17"]["arming_generation"] == 3


def test_transient_contention_allows_one_retry_per_generation(tmp_path):
    first = reserve_execution(
        tmp_path, issue_id=17, source="leaf_claim", operation_stage="claim",
        execution_id="first",
    )
    assert not record_transient_contention(
        tmp_path, 17, first["reservation_id"], "unable to create index.lock"
    )
    second = reserve_execution(
        tmp_path, issue_id=17, source="leaf_claim", operation_stage="claim",
        execution_id="second",
    )
    assert record_transient_contention(
        tmp_path, 17, second["reservation_id"], "unable to create index.lock"
    )
    state = load_failure_state(tmp_path)
    state["issues"]["17"]["inhibited"] = True
    save_failure_state(tmp_path, state)
    observe_rearm_state(tmp_path, ready_issue_ids=set(), blocked_issue_ids=set())
    observe_rearm_state(tmp_path, ready_issue_ids={17}, blocked_issue_ids=set())
    third = reserve_execution(
        tmp_path, issue_id=17, source="leaf_claim", operation_stage="claim",
        execution_id="third",
    )
    assert not record_transient_contention(
        tmp_path, 17, third["reservation_id"], "unable to create index.lock"
    )


def test_strict_state_rejects_corrupt_occurrence_without_overwrite(tmp_path):
    state = {
        "version": 2,
        "revision": 1,
        "issues": {"17": {"issue_id": 17, "reservations": {}, "occurrences": {
            "bad": {"occurrence_id": "bad", "issue_id": 17}
        }}},
    }
    path = tmp_path / "dispatch_failures.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(FailureStateError):
        reserve_execution(
            tmp_path, issue_id=17, source="leaf_claim", operation_stage="claim"
        )
    assert path.read_bytes() == original


def test_concurrent_reservations_and_promotions_keep_complete_pairs(tmp_path):
    def publish(index: int):
        execution = f"execution-{index}"
        occurrence = f"occurrence-{index}"
        reservation = reserve_execution(
            tmp_path, issue_id=17, source="leaf_claim", operation_stage="claim",
            execution_id=execution,
        )
        return promote_reservation(
            tmp_path, 17, reservation["reservation_id"],
            _record(
                occurrence_id=occurrence,
                delivery_key=f"worklink-attention:17:signature:{occurrence}",
                execution_id=execution,
            ),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        published = list(executor.map(publish, range(16)))
    state = load_failure_state(tmp_path)["issues"]["17"]
    assert len(published) == len(state["occurrences"]) == len(state["reservations"]) == 16
    assert all(
        reservation["state"] == "closed"
        and reservation["promoted_occurrence_id"] in state["occurrences"]
        for reservation in state["reservations"].values()
    )
