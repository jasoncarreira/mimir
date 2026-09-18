from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from mimir.worklink.attention import (
    AttentionCause,
    AttentionSource,
    ClaimFacts,
    ClaimIdentity,
)
from mimir.worklink.dispatch_failures import (
    FailureStateError,
    authorized_retry_reservation_id,
    active_reservation_id,
    bind_claim,
    confirm_claim_and_start,
    load_outcome_state,
    occurrence_identity,
    pending_attention,
    record_attention,
    reserve_dispatch,
    issue_dispatch_disposition,
    mark_claim_absent,
    record_contention_recurrence,
    reservation_from_environment,
    validate_claim_retry_authorization,
)


def claim(issue_id: int = 42) -> ClaimIdentity:
    return ClaimIdentity(issue_id, 1, "agent", "2026-09-18T00:00:00+00:00")


def test_prepared_refusal_can_later_activate_with_same_identity(tmp_path: Path) -> None:
    state_dir = tmp_path / "ledger"
    reservation = reserve_dispatch(
        state_dir, issue_id=42, target="leaf", autonomous=True
    )

    refusal = record_attention(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        source=AttentionSource.CLAIM_CONTENTION,
        cause=AttentionCause.CONTENTION_EXHAUSTED,
        facts=ClaimFacts(None, None, "contention"),
        disposition="transient_retry",
        retry_after="2026-09-18T00:00:00+00:00",
    )

    # A bounded transient refusal is an immutable occurrence, but its prepared
    # reservation remains the identity used by the authorized later attempt.
    bind_claim(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim(), confirmed=False
    )
    started = confirm_claim_and_start(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim()
    )
    assert refusal["kind"] == "attention"
    assert started["kind"] == "start"
    assert started["reservation_id"] == refusal["reservation_id"]
    assert active_reservation_id(state_dir, issue_id=42, target="leaf") == reservation


def test_claim_settlement_is_exactly_once_and_proof_controls_consumption(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "ledger"
    reservation = reserve_dispatch(
        state_dir, issue_id=42, target="leaf", autonomous=True
    )
    bind_claim(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim(), confirmed=False
    )
    confirm_claim_and_start(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim()
    )
    occurrence = record_attention(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        source=AttentionSource.CLAIM_COMMENT,
        cause=AttentionCause.CLAIM_PUBLICATION_FAILED,
        facts=ClaimFacts(claim(), claim(), "comment_ambiguous", mutation_stage="comment"),
        claim=claim(),
    )
    state = load_outcome_state(state_dir)
    settlement = state["issues"]["42"]["settlements"][claim().key]
    assert settlement["consumed"] is False
    assert occurrence["accounting"]["settlement_key"] == claim().key

    with pytest.raises(FailureStateError, match="does not match terminal evidence"):
        record_attention(
            state_dir,
            issue_id=42,
            reservation_id=reservation,
            source=AttentionSource.LEAF_BACKEND_OUTCOME,
            cause=AttentionCause.BACKEND_FAILED,
            facts={
                "type": "leaf", "backend": None, "checkout": None, "base": None,
                "branch": None, "isolated": None, "compute_result": "failed",
                "backend_status": "failed", "validation_reason_codes": [],
                "evidence_id": None, "evidence_sha256": None, "pr_url": None,
                "head_sha": None,
            },
            claim=claim(),
            proof_ids=("leaf_outcome:proof",),
        )


def test_v1_migration_preserves_row_and_does_not_treat_signal_receipt_as_handled(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "ledger"
    state_dir.mkdir()
    row = {
        "active": True,
        "issue_id": 42,
        "attempt": None,
        "attempt_consumed": False,
        "exit_status": 1,
        "terminal_error": "old",
        "signature": "sig",
        "occurrence_id": "old-occurrence",
        "consecutive": 1,
        "failed_at": datetime.now(UTC).isoformat(),
        "retry_after": None,
        "log_path": "",
        "preserved_ref": None,
        "preservation_error": None,
        "notified_signatures": ["sig"],
    }
    (state_dir / "dispatch_failures.json").write_text(
        json.dumps({"version": 1, "issues": {"42": row}}), encoding="utf-8"
    )
    reserve_dispatch(state_dir, issue_id=43, target="leaf", autonomous=True)
    state = load_outcome_state(state_dir)
    assert state["version"] == 2
    assert state["issues"]["42"]["legacy"]["row"] == row
    assert [item["kind"] for item in pending_attention(state_dir)] == ["legacy_attention"]


def test_unknown_or_duplicate_schema_fails_closed(tmp_path: Path) -> None:
    state_dir = tmp_path / "ledger"
    state_dir.mkdir()
    path = state_dir / "dispatch_failures.json"
    path.write_text('{"version":2,"version":2,"issues":{}}', encoding="utf-8")
    with pytest.raises(FailureStateError, match="duplicate"):
        load_outcome_state(state_dir)
    path.write_text('{"version":99,"issues":{}}', encoding="utf-8")
    with pytest.raises(FailureStateError, match="top-level"):
        load_outcome_state(state_dir)


def test_occurrence_identity_has_independent_lifecycle_slots() -> None:
    reservation = "fa21f7d8-7f41-4dd8-83a9-64be68f02889"
    assert occurrence_identity(reservation, "op-1", "start") != occurrence_identity(
        reservation, "op-1", "terminal"
    )


def test_unknown_inherited_reservation_refuses_and_empty_legacy_does_not_invent(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "ledger"
    with pytest.raises(FailureStateError, match="does not exist"):
        reservation_from_environment(
            state_dir,
            issue_id=42,
            target="leaf",
            autonomous=True,
            environ={"MIMIR_WORKLINK_RESERVATION_ID": "fa21f7d8-7f41-4dd8-83a9-64be68f02889"},
        )
    assert reservation_from_environment(
        state_dir,
        issue_id=42,
        target="leaf",
        autonomous=True,
        environ={"MIMIR_WORKLINK_RESERVATION_ID": ""},
    ) is None
    assert load_outcome_state(state_dir)["issues"] == {}


def test_prepared_reservation_cannot_hide_prior_stop(tmp_path: Path) -> None:
    state_dir = tmp_path / "ledger"
    first = reserve_dispatch(state_dir, issue_id=42, target="leaf", autonomous=True)
    record_attention(
        state_dir,
        issue_id=42,
        reservation_id=first,
        source=AttentionSource.CLAIM_COMMAND,
        cause=AttentionCause.CLAIM_COMMAND_FAILED,
        facts=ClaimFacts(None, None, "failed"),
    )
    reserve_dispatch(state_dir, issue_id=42, target="leaf", autonomous=True)
    assert issue_dispatch_disposition(state_dir, 42) == "stop"


def test_contention_recurrence_is_30_120_then_stop_and_success_only_reset(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "ledger"
    now = datetime(2026, 9, 18, tzinfo=UTC)
    first = record_contention_recurrence(
        state_dir, issue_id=42, signature="same", now=now
    )
    second = record_contention_recurrence(
        state_dir, issue_id=42, signature="same", now=now
    )
    third = record_contention_recurrence(
        state_dir, issue_id=42, signature="same", now=now
    )
    assert first == ("transient_retry", "2026-09-18T00:00:30+00:00")
    assert second == ("transient_retry", "2026-09-18T00:02:00+00:00")
    assert third == ("stop", None)
    record_contention_recurrence(
        state_dir,
        issue_id=42,
        signature="same",
        verified_work_success=True,
        now=now,
    )
    assert record_contention_recurrence(
        state_dir, issue_id=42, signature="same", now=now
    ) == first


def test_unconfirmed_and_strictly_absent_claims_cannot_settle(tmp_path: Path) -> None:
    state_dir = tmp_path / "ledger"
    reservation = reserve_dispatch(state_dir, issue_id=42, target="leaf", autonomous=True)
    identity = claim()
    bind_claim(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        claim=identity,
        confirmed=False,
    )
    with pytest.raises(FailureStateError, match="unconfirmed claim"):
        record_attention(
            state_dir,
            issue_id=42,
            reservation_id=reservation,
            source=AttentionSource.CLAIM_COMMENT,
            cause=AttentionCause.CLAIM_PUBLICATION_FAILED,
            facts=ClaimFacts(identity, None, "ambiguous", history_read="error"),
            claim=identity,
        )
    mark_claim_absent(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        claim=identity,
    )
    occurrence = record_attention(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        source=AttentionSource.CLAIM_COMMENT,
        cause=AttentionCause.CLAIM_PUBLICATION_FAILED,
        facts=ClaimFacts(identity, None, "absent", history_read="exact"),
    )
    state = load_outcome_state(state_dir)
    current = state["issues"]["42"]["reservations"][reservation]
    assert current["claim_state"] == "absent"
    assert state["issues"]["42"]["settlements"] == {}
    assert occurrence["accounting"] == {
        "scope": "no_new_claim", "claim": None, "consumed": False,
        "settlement_key": None,
    }


def test_retry_deadline_requires_the_exact_prepared_reservation(tmp_path: Path) -> None:
    state_dir = tmp_path / "ledger"
    now = datetime(2026, 9, 18, tzinfo=UTC)
    retry = reserve_dispatch(state_dir, issue_id=42, target="leaf", autonomous=True)
    record_attention(
        state_dir,
        issue_id=42,
        reservation_id=retry,
        source=AttentionSource.CLAIM_CONTENTION,
        cause=AttentionCause.CONTENTION_EXHAUSTED,
        facts=ClaimFacts(None, None, "contention"),
        disposition="transient_retry",
        retry_after=(now + timedelta(seconds=30)).isoformat(),
        now=now,
    )
    fresh = reserve_dispatch(state_dir, issue_id=42, target="leaf", autonomous=True)
    with pytest.raises(FailureStateError, match="reuse"):
        validate_claim_retry_authorization(
            state_dir,
            issue_id=42,
            reservation_id=fresh,
            target="leaf",
            now=now + timedelta(seconds=31),
        )
    with pytest.raises(FailureStateError, match="not due"):
        authorized_retry_reservation_id(
            state_dir, issue_id=42, target="leaf", now=now
        )
    assert authorized_retry_reservation_id(
        state_dir, issue_id=42, target="leaf", now=now + timedelta(seconds=31)
    ) == retry


def test_positive_leaf_proof_reads_bytes_and_rejects_tampering(tmp_path: Path) -> None:
    state_dir = tmp_path / "ledger"
    reservation = reserve_dispatch(state_dir, issue_id=42, target="leaf", autonomous=True)
    identity = claim()
    bind_claim(
        state_dir, issue_id=42, reservation_id=reservation, claim=identity, confirmed=False
    )
    confirm_claim_and_start(
        state_dir, issue_id=42, reservation_id=reservation, claim=identity
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"issue": 42, "status": "blocked"}), encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    evidence.write_text(json.dumps({"issue": 42, "status": "failed"}), encoding="utf-8")
    with pytest.raises(FailureStateError, match="hash mismatch"):
        record_attention(
            state_dir,
            issue_id=42,
            reservation_id=reservation,
            source=AttentionSource.LEAF_BACKEND_OUTCOME,
            cause=AttentionCause.BACKEND_BLOCKED,
            facts={
                "type": "leaf", "backend": "fake", "checkout": str(tmp_path),
                "base": "main", "branch": "issue/42-a1", "isolated": True,
                "compute_result": "blocked", "backend_status": "blocked",
                "validation_reason_codes": [], "evidence_id": str(evidence.resolve()),
                "evidence_sha256": digest, "pr_url": None, "head_sha": None,
            },
            claim=identity,
            proof_ids=(f"leaf_outcome:{digest}",),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda state, reservation: state["issues"]["42"]["reservations"][reservation].update(
                state="terminal"
            ),
            id="terminal-running-operation",
        ),
        pytest.param(
            lambda state, reservation: state["issues"]["42"]["occurrences"][
                next(iter(state["issues"]["42"]["occurrences"]))
            ]["accounting"].update(consumed=True),
            id="uncharged-occurrence-consumed",
        ),
        pytest.param(
            lambda state, reservation: state["issues"]["42"]["reservations"][reservation].update(
                disposition="success"
            ),
            id="success-without-witness",
        ),
    ],
)
def test_cross_record_illegal_combinations_fail_closed(
    tmp_path: Path, mutate: object
) -> None:
    state_dir = tmp_path / "ledger"
    reservation = reserve_dispatch(state_dir, issue_id=42, target="leaf", autonomous=True)
    identity = claim()
    bind_claim(
        state_dir, issue_id=42, reservation_id=reservation, claim=identity, confirmed=False
    )
    confirm_claim_and_start(
        state_dir, issue_id=42, reservation_id=reservation, claim=identity
    )
    path = state_dir / "dispatch_failures.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    mutate(state, reservation)  # type: ignore[operator]
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(FailureStateError):
        load_outcome_state(state_dir)


def test_absent_claim_without_its_retained_identity_fails_closed(tmp_path: Path) -> None:
    state_dir = tmp_path / "ledger"
    reservation = reserve_dispatch(state_dir, issue_id=42, target="leaf", autonomous=True)
    path = state_dir / "dispatch_failures.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["issues"]["42"]["reservations"][reservation]["claim_state"] = "absent"
    path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(FailureStateError, match="claim identity"):
        load_outcome_state(state_dir)


def test_settled_reservation_without_its_exact_settlement_fails_closed(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "ledger"
    reservation = reserve_dispatch(state_dir, issue_id=42, target="leaf", autonomous=True)
    identity = claim()
    bind_claim(
        state_dir, issue_id=42, reservation_id=reservation, claim=identity, confirmed=False
    )
    confirm_claim_and_start(
        state_dir, issue_id=42, reservation_id=reservation, claim=identity
    )
    record_attention(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        source=AttentionSource.LEAF_BACKEND_OUTCOME,
        cause=AttentionCause.BACKEND_FAILED,
        facts={
            "type": "leaf", "backend": "fake", "checkout": str(tmp_path),
            "base": "main", "branch": "issue/42-a1", "isolated": True,
            "compute_result": "failed", "backend_status": "failed",
            "validation_reason_codes": [], "evidence_id": None,
            "evidence_sha256": None, "pr_url": None, "head_sha": None,
        },
        claim=identity,
    )
    path = state_dir / "dispatch_failures.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["issues"]["42"]["settlements"].clear()
    terminal = next(
        occurrence
        for occurrence in state["issues"]["42"]["occurrences"].values()
        if occurrence["kind"] == "attention"
    )
    terminal["accounting"] = {
        "scope": "no_new_claim",
        "claim": None,
        "consumed": False,
        "settlement_key": None,
    }
    path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(FailureStateError, match="exact settlement"):
        load_outcome_state(state_dir)


def test_finished_operation_without_its_exact_occurrence_fails_closed(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "ledger"
    reservation = reserve_dispatch(state_dir, issue_id=42, target="leaf", autonomous=True)
    record_attention(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        source=AttentionSource.CLAIM_COMMAND,
        cause=AttentionCause.CLAIM_COMMAND_FAILED,
        facts={
            "type": "claim", "intended": None, "confirmed": None,
            "result": "claim_failed", "lock_identity": None,
            "command_operation": "locks claim", "return_code": 1,
            "mutation_stage": None, "history_read": None,
        },
    )
    path = state_dir / "dispatch_failures.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["issues"]["42"]["occurrences"].clear()
    path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(FailureStateError, match="exact occurrence"):
        load_outcome_state(state_dir)
