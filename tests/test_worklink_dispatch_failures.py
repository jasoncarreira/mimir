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
    delegate_reservation,
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


EXHAUSTIVE_EXCLUSION_MATRIX = (
    ("dependency_wait", "leaf_run_boundary"),
    ("capacity_wait", "leaf_run_boundary"),
    ("registry_concurrency", "leaf_run_boundary"),
    ("arbiter_suppression", "leaf_run_boundary"),
    ("poller_quota", "leaf_run_boundary"),
    ("duplicate_run_live", "leaf_claim"),
    ("lifecycle_state_incompatible", "leaf_claim"),
    ("review_ready_evidence_exists", "leaf_claim"),
    ("publication_intent_exists", "leaf_claim"),
    ("concurrency_cap", "leaf_claim"),
    ("checkout_interlock", "epic_factory_admit"),
    ("persistent_block", "leaf_run_boundary"),
    ("clean_or_missing_orphan", "orphan_ambiguous"),
    ("unidentifiable_factory_inventory", "startup_factory_record_read"),
    ("unidentifiable_run_record", "startup_run_record_read"),
    ("legacy_or_manual_startup", "startup_run_record_read"),
    ("manual_invocation", "leaf_run_boundary"),
    ("reattach_invocation", "leaf_run_boundary"),
    ("dry_run_invocation", "leaf_run_boundary"),
    ("startup_no_repo", "startup_leaf_spawn"),
    ("healthy_parked_terminal_startup", "startup_factory_spawn"),
    ("evidence_event_telemetry", "leaf_completed_evidence_write"),
    ("transition_event_telemetry", "leaf_transition"),
    ("error_event_telemetry", "epic_error_transition"),
    ("log_io", "detached_spawn"),
    ("temporary_report", "leaf_postclaim"),
    ("heartbeat_diagnostic", "epic_supervision"),
    ("shutdown_marker", "leaf_run_boundary"),
    ("ordinary_leaf_success", "leaf_postclaim"),
    ("continuation_delivery", "leaf_run_boundary"),
)


@pytest.mark.parametrize(
    ("surface", "source"),
    EXHAUSTIVE_EXCLUSION_MATRIX,
    ids=[item[0] for item in EXHAUSTIVE_EXCLUSION_MATRIX],
)
def test_exhaustive_exclusion_matrix_closes_without_occurrence_or_feature_accounting(
    tmp_path, surface, source,
):
    reservation = reserve_execution(
        tmp_path,
        issue_id=17,
        source=source,
        operation_stage=surface,
        execution_id=f"execution-{surface}",
    )
    close_reservation_excluded(
        tmp_path,
        17,
        reservation["reservation_id"],
        witness=surface,
    )
    issue = load_failure_state(tmp_path)["issues"]["17"]
    stored = issue["reservations"][reservation["reservation_id"]]
    assert stored["closure"] == "excluded"
    assert stored["exclusion_witness"] == surface
    assert issue["occurrences"] == {}
    assert "attempt_consumed" not in issue
    assert "settlement" not in issue


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


def test_source_delegation_is_atomic_and_replays_from_the_wrapper(tmp_path, monkeypatch):
    import mimir.worklink.dispatch_failures as failures

    wrapper = reserve_execution(
        tmp_path,
        issue_id=17,
        source="leaf_run_boundary",
        operation_stage="run",
        execution_id="execution",
        run_id="run-17",
        invocation_id="invocation-17",
    )
    original_save = failures.save_failure_state

    def fail_save(*args, **kwargs):
        raise OSError("replace unavailable")

    monkeypatch.setattr(failures, "save_failure_state", fail_save)
    with pytest.raises(OSError, match="replace unavailable"):
        delegate_reservation(
            tmp_path,
            17,
            wrapper["reservation_id"],
            delegated_reservation_id="exact-reservation",
            source="leaf_claim",
            operation_stage="terminal",
        )
    monkeypatch.setattr(failures, "save_failure_state", original_save)
    state = load_failure_state(tmp_path)
    assert state["issues"]["17"]["reservations"][wrapper["reservation_id"]]["state"] == "reserved"
    assert "exact-reservation" not in state["issues"]["17"]["reservations"]

    exact = delegate_reservation(
        tmp_path,
        17,
        wrapper["reservation_id"],
        delegated_reservation_id="exact-reservation",
        source="leaf_claim",
        operation_stage="terminal",
    )
    replay = delegate_reservation(
        tmp_path,
        17,
        wrapper["reservation_id"],
        delegated_reservation_id="exact-reservation",
        source="leaf_claim",
        operation_stage="terminal",
    )
    assert replay == exact
    promoted = promote_reservation(
        tmp_path, 17, exact["reservation_id"], replace(_record(), run_id="run-17")
    )
    assert delegate_reservation(
        tmp_path,
        17,
        wrapper["reservation_id"],
        delegated_reservation_id="exact-reservation",
        source="leaf_claim",
        operation_stage="terminal",
    )["promoted_occurrence_id"] == promoted["occurrence_id"]


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("operation_stage", "other"),
        ("run_id", "other-run"),
        ("launch_id", "other-launch"),
        ("invocation_id", "other-invocation"),
    ],
)
def test_reservation_replay_rejects_changed_bindings(tmp_path, change, value):
    reservation = reserve_execution(
        tmp_path,
        issue_id=17,
        source="leaf_claim",
        operation_stage="claim",
        execution_id="execution",
        run_id="run",
        launch_id="launch",
        invocation_id="invocation",
        reservation_id="stable",
    )
    arguments = {
        "issue_id": 17,
        "source": "leaf_claim",
        "operation_stage": "claim",
        "execution_id": "execution",
        "run_id": "run",
        "launch_id": "launch",
        "invocation_id": "invocation",
        "reservation_id": reservation["reservation_id"],
    }
    arguments[change] = value
    with pytest.raises(FailureStateError, match="replay identity mismatch"):
        reserve_execution(tmp_path, **arguments)


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
    with pytest.raises(FailureStateError, match="replay identity mismatch"):
        promote_reservation(
            tmp_path, 17, reservation["reservation_id"],
            replace(
                _record(reason="second"),
                outcome=AttentionOutcome.BLOCKED,
                accounting_basis=AccountingBasis.LEAF_EXECUTION,
                attempt_consumed=True,
                settlement=Settlement.NOT_NEEDED,
            ),
        )
    with pytest.raises(FailureStateError, match="replay identity mismatch"):
        promote_reservation(
            tmp_path, 17, reservation["reservation_id"], _record(reason="second")
        )
    replay = promote_reservation(
        tmp_path, 17, reservation["reservation_id"], _record(reason="first")
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


def test_v2_loader_rejects_non_native_and_crosslinked_corruption(tmp_path):
    reservation = reserve_execution(
        tmp_path, issue_id=17, source="leaf_claim", operation_stage="claim",
        execution_id="execution",
    )
    promote_reservation(tmp_path, 17, reservation["reservation_id"], _record())
    path = tmp_path / "dispatch_failures.json"
    valid = json.loads(path.read_text(encoding="utf-8"))

    variants = []
    for revision in ("1", True, -1):
        payload = json.loads(json.dumps(valid))
        payload["revision"] = revision
        variants.append(payload)
    payload = json.loads(json.dumps(valid))
    del payload["revision"]
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    del payload["issues"]["17"]["occurrences"]["occurrence"]["schema_version"]
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["occurrences"]["occurrence"]["schema_version"] = "2"
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["occurrences"]["occurrence"]["kind"] = "factory_started"
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["occurrences"]["occurrence"]["cause"] = 1
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["occurrences"]["occurrence"]["refs"] = {"log": False}
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["occurrences"]["occurrence"]["outcome"] = "blocked"
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["occurrences"]["occurrence"]["claim_relation"] = "current_claim"
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["occurrences"]["occurrence"]["claim_binding_state"] = "prepared"
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["occurrences"]["occurrence"]["handled_at"] = "2026-09-18T00:00:00+00:00"
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["occurrences"]["occurrence"]["handling_disposition"] = "observed"
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["occurrences"]["occurrence"]["retirement"] = "retry_exhausted"
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["active"] = "true"
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    stored_reservation = payload["issues"]["17"]["reservations"][reservation["reservation_id"]]
    stored_reservation["claim_binding_state"] = "prepared"
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["reservations"][reservation["reservation_id"]][
        "promoted_occurrence_id"
    ] = "missing"
    variants.append(payload)
    payload = json.loads(json.dumps(valid))
    payload["issues"]["17"]["occurrences"]["occurrence"]["execution_id"] = "foreign"
    variants.append(payload)

    for corrupt in variants:
        path.write_text(json.dumps(corrupt), encoding="utf-8")
        original = path.read_bytes()
        with pytest.raises(FailureStateError):
            load_failure_state(tmp_path)
        with pytest.raises(FailureStateError):
            reserve_execution(
                tmp_path, issue_id=17, source="leaf_claim", operation_stage="claim"
            )
        assert path.read_bytes() == original


def test_v2_loader_requires_exclusion_closure_witness(tmp_path):
    reservation = reserve_execution(
        tmp_path, issue_id=17, source="leaf_claim", operation_stage="claim",
        execution_id="execution",
    )
    close_reservation_excluded(
        tmp_path, 17, reservation["reservation_id"], witness="benign refusal"
    )
    path = tmp_path / "dispatch_failures.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["issues"]["17"]["reservations"][reservation["reservation_id"]][
        "exclusion_witness"
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FailureStateError, match="exclusion witness"):
        load_failure_state(tmp_path)


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
