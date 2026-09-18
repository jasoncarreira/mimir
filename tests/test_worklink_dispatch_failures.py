from __future__ import annotations

from datetime import UTC, datetime
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
    active_reservation_id,
    confirm_claim_and_start,
    load_outcome_state,
    occurrence_identity,
    pending_attention,
    record_attention,
    reserve_dispatch,
    issue_dispatch_disposition,
    record_contention_recurrence,
    reservation_from_environment,
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
    )

    # A bounded transient refusal is an immutable occurrence, but its prepared
    # reservation remains the identity used by the authorized later attempt.
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
