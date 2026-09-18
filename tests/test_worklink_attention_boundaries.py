from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import pytest

from mimir.commands.worklink import _record_interruption
from mimir.worklink.attention import (
    AttentionSource,
    ClaimIdentity,
    ClearancePolicy,
    FactorySnapshot,
    SOURCE_RULES,
)
from mimir.worklink.claims import ClaimRecord
from mimir.worklink.dispatch_failures import (
    bind_claim,
    confirm_claim_and_start,
    dispatch_failure_state_dir,
    load_outcome_state,
    record_attention,
    reserve_dispatch,
    retain_positive_proofs,
)
from mimir.worklink.orchestrator import (
    WorklinkRunResult,
    _record_preclaim_claim,
    _record_run_success,
    _typed_outcome_boundary,
)


PRODUCTION_SOURCES = frozenset({
    "queue_leaf_spawn", "queue_factory_spawn", "startup_leaf_spawn",
    "startup_factory_spawn", "leaf_cli_repository", "factory_cli_repository",
    "leaf_issue_read", "leaf_target_branch", "leaf_template", "leaf_config",
    "leaf_inventory", "leaf_repository", "leaf_backend", "leaf_compute",
    "leaf_claim_preparation", "factory_interlock", "factory_issue_read",
    "factory_issue_kind", "factory_target_branch", "factory_config",
    "factory_backend", "factory_repository", "factory_compute", "factory_launcher",
    "factory_inventory", "factory_base_lookup", "factory_work_item",
    "factory_retained_binding", "claim_command", "claim_guard", "claim_steal",
    "claim_budget", "claim_capacity_read", "claim_unready", "claim_inprogress",
    "claim_comment", "claim_contention", "leaf_checkout_create",
    "leaf_publication_capture", "leaf_checkout_isolation", "leaf_dirty_snapshot",
    "leaf_prompt", "leaf_work_spec", "leaf_report_setup", "leaf_compute_launch",
    "leaf_handle_save", "leaf_compute_wait", "leaf_interpret", "leaf_test_report",
    "leaf_pr_body", "leaf_gate", "leaf_regate", "leaf_evidence_write", "leaf_commit",
    "leaf_publication_fence", "leaf_push", "leaf_pr_open", "leaf_evidence_comment",
    "leaf_backend_outcome", "leaf_terminal_labels", "leaf_terminal_release",
    "leaf_interrupt", "reattach_state", "reattach_shim", "reattach_pr",
    "reattach_backend", "reattach_compute", "reattach_issue", "reattach_checkout",
    "reattach_prompt", "reattach_spec", "reattach_wait", "leaf_startup_reconcile",
    "factory_checkout", "factory_git_identity", "factory_publishing_identity",
    "factory_credential", "factory_identity_verify", "factory_spec",
    "factory_launch_binding", "factory_sandbox", "factory_permissions",
    "factory_launch", "factory_handle_save", "factory_recovery_binding",
    "factory_recovery_status", "factory_recovery_lock", "factory_resume",
    "factory_recovery_repository", "factory_recovery_spec", "factory_recovery_launch",
    "factory_recovery_save", "factory_wait_start", "factory_status_read",
    "factory_startup_deadline", "factory_run_deadline", "factory_status_binding",
    "factory_owner", "factory_observation_save", "factory_heartbeat",
    "factory_wait_drain", "factory_driver_exit", "factory_transcript",
    "factory_cleanup", "factory_parked", "factory_blocked", "factory_partial",
    "factory_completion_verify", "factory_terminal_labels", "factory_terminal_release",
    "factory_interrupt", "factory_startup_reconcile", "leaf_start", "factory_start",
    "leaf_success", "factory_success", "legacy_v1",
})


def _claim(issue_id: int = 42) -> tuple[ClaimIdentity, ClaimRecord]:
    claimed_at = datetime(2026, 9, 18, tzinfo=UTC)
    return (
        ClaimIdentity(issue_id, 1, "boundary-agent", claimed_at.isoformat()),
        ClaimRecord(issue_id, 1, "boundary-agent", claimed_at),
    )


def _prepare_claimed_reservation(
    home: Path, source: AttentionSource
) -> tuple[Path, str, ClaimIdentity, ClaimRecord]:
    state_dir = dispatch_failure_state_dir(home)
    target = "factory" if source.value.startswith("factory_") else "leaf"
    reservation = reserve_dispatch(
        state_dir, issue_id=42, target=target, autonomous=True
    )
    identity, record = _claim()
    bind_claim(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        claim=identity,
        confirmed=False,
    )
    confirm_claim_and_start(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        claim=identity,
        run_id="chainlink-42" if target == "factory" else None,
        sandbox=str(home / "sandbox") if target == "factory" else None,
    )
    return state_dir, reservation, identity, record


def _produce_lifecycle(home: Path, source: AttentionSource) -> None:
    state_dir = dispatch_failure_state_dir(home)
    target = "factory" if source.value.startswith("factory_") else "leaf"
    reservation = reserve_dispatch(state_dir, issue_id=42, target=target, autonomous=True)
    identity, _record = _claim()
    bind_claim(
        state_dir, issue_id=42, reservation_id=reservation, claim=identity, confirmed=False
    )
    confirm_claim_and_start(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        claim=identity,
        run_id="chainlink-42" if target == "factory" else None,
        sandbox=str(home / "sandbox") if target == "factory" else None,
    )
    if source in {AttentionSource.LEAF_START, AttentionSource.FACTORY_START}:
        return
    evidence = home / f"{target}-success.json"
    payload = {
        "issue": 42,
        "status": "completed",
        "branch": "feature/42",
        "head_sha": "a" * 40,
        "pr_url": None,
    }
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    result = WorklinkRunResult(
        42,
        1,
        "completed",
        evidence_path=evidence,
        checkout=home / "sandbox",
        branch="feature/42",
    )
    _record_run_success(
        home,
        42,
        reservation_id=reservation,
        result=result,
        target=target,
    )


@pytest.mark.parametrize("source", list(AttentionSource), ids=lambda source: source.value)
def test_every_finite_source_commits_through_its_production_boundary(
    tmp_path: Path, source: AttentionSource
) -> None:
    if source == AttentionSource.LEGACY_V1:
        state_dir = dispatch_failure_state_dir(tmp_path)
        state_dir.mkdir(parents=True)
        row = {
            "active": True,
            "issue_id": 42,
            "failed_at": "2026-09-18T00:00:00+00:00",
            "signature": "legacy",
            "occurrence_id": "legacy-occurrence",
            "retry_after": None,
        }
        (state_dir / "dispatch_failures.json").write_text(
            json.dumps({"version": 1, "issues": {"42": row}}), encoding="utf-8"
        )
        state = load_outcome_state(state_dir)
    elif source in {
        AttentionSource.LEAF_START,
        AttentionSource.FACTORY_START,
        AttentionSource.LEAF_SUCCESS,
        AttentionSource.FACTORY_SUCCESS,
    }:
        _produce_lifecycle(tmp_path, source)
        state = load_outcome_state(dispatch_failure_state_dir(tmp_path))
    elif source in {AttentionSource.LEAF_INTERRUPT, AttentionSource.FACTORY_INTERRUPT}:
        state_dir, reservation, _identity, _record = _prepare_claimed_reservation(
            tmp_path, source
        )
        _record_interruption(
            tmp_path,
            42,
            reservation,
            target="factory" if source == AttentionSource.FACTORY_INTERRUPT else "leaf",
            source=source.value,
            retained=True,
        )
        state = load_outcome_state(state_dir)
    elif SOURCE_RULES[source].fact_type == "claim":
        state_dir = dispatch_failure_state_dir(tmp_path)
        reservation = reserve_dispatch(
            state_dir, issue_id=42, target="leaf", autonomous=True
        )
        cause = min(SOURCE_RULES[source].causes, key=lambda item: item.value)
        _record_preclaim_claim(
            tmp_path,
            42,
            reservation,
            source=source.value,
            cause=cause.value,
            result="boundary_failure",
        )
        state = load_outcome_state(state_dir)
    elif source == AttentionSource.FACTORY_PARTIAL:
        state_dir, reservation, identity, _record = _prepare_claimed_reservation(
            tmp_path, source
        )
        retain_positive_proofs(
            state_dir,
            issue_id=42,
            reservation_id=reservation,
            proof_ids=("factory_partial",),
        )
        snapshot = FactorySnapshot(
            run_id="chainlink-42", issue_id=42, attempt=1,
            sandbox=str(tmp_path / "sandbox"), session=None,
            controller_phase="terminal", controller_error=None, status="partial",
            valid=True, lock=None, dead_lock=None, lock_session=None, gates=(),
            steps=(), slices=(), pr_url=None, next="resume", next_present=True,
            park_snapshot=None, read_result="captured",
        )
        record_attention(
            state_dir,
            issue_id=42,
            reservation_id=reservation,
            source=source,
            cause=next(iter(SOURCE_RULES[source].causes)),
            facts=snapshot,
            claim=identity,
            proof_ids=("factory_partial",),
        )
        state = load_outcome_state(state_dir)
    else:
        state_dir, reservation, _identity, record = _prepare_claimed_reservation(
            tmp_path, source
        )
        cause = min(SOURCE_RULES[source].causes, key=lambda item: item.value)
        with pytest.raises(RuntimeError, match="boundary failure"):
            with _typed_outcome_boundary(
                home=tmp_path,
                issue_id=42,
                reservation_id=reservation,
                source=source.value,
                cause=cause.value,
                claim_record=record,
                checkout=tmp_path / "checkout",
                branch="feature/42",
                operation="boundary_test",
            ):
                raise RuntimeError("boundary failure")
        state = load_outcome_state(state_dir)

    occurrences = state["issues"]["42"]["occurrences"].values()
    assert source.value in {occurrence["source"] for occurrence in occurrences}


def test_production_inventory_equals_closed_source_and_clearance_inventory() -> None:
    assert PRODUCTION_SOURCES == {source.value for source in AttentionSource}
    assert set(SOURCE_RULES) == set(AttentionSource)
    assert all(
        rule.clearance in set(ClearancePolicy)
        for rule in SOURCE_RULES.values()
    )
