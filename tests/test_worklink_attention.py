from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from mimir.worklink.attention import (
    Accounting,
    AccountingScope,
    AttentionCause,
    AttentionKind,
    AttentionSchemaError,
    AttentionSource,
    ClaimIdentity,
    FactorySnapshot,
    ReaderCode,
    SOURCE_RULES,
    clearance_requirements,
    inspect_clearance,
    positive_factory_proofs,
    rehydrate_attention,
    validate_occurrence_contract,
)
from mimir.worklink.dispatch_failures import (
    confirm_claim_and_start,
    dispatch_failure_state_dir,
    record_attention,
    reserve_dispatch,
)


def test_source_contract_is_closed() -> None:
    assert set(SOURCE_RULES) == set(AttentionSource)
    assert all(rule.causes or source.value.endswith(("_start", "_success")) for source, rule in SOURCE_RULES.items())


def test_start_is_never_diagnosis_or_consumption() -> None:
    claim = ClaimIdentity(42, 1, "agent", "now")
    facts = {
        "type": "lifecycle_start", "target": "leaf", "claim": claim.to_json(),
        "admitted_at": "now", "run_id": None, "sandbox": None,
        "admission": "confirmed_claim",
    }
    validate_occurrence_contract(
        kind=AttentionKind.START,
        source=AttentionSource.LEAF_START,
        cause=None,
        facts=facts,
        accounting=Accounting(AccountingScope.NO_NEW_CLAIM, None, None, None),
    )
    with pytest.raises(AttentionSchemaError, match="cannot have a cause"):
        validate_occurrence_contract(
            kind=AttentionKind.START,
            source=AttentionSource.LEAF_START,
            cause=AttentionCause.BACKEND_FAILED,
            facts=facts,
            accounting=Accounting(AccountingScope.NO_NEW_CLAIM, None, None, None),
        )


def test_factory_proofs_accept_completed_steps_not_gates_or_allocations() -> None:
    snapshot = FactorySnapshot(
        run_id="chainlink-42", issue_id=42, attempt=2, sandbox="/tmp/s", session=None,
        controller_phase="running", controller_error=None, status="running", valid=True,
        lock=None, dead_lock=None, lock_session=None, gates=({"status": "approved"},),
        steps=(
            {"name": "story", "status": "accepted", "attempts": 1},
            {"name": "spec", "status": "rejected", "attempts": 2},
            {"name": "build", "status": "running", "attempts": 4},
        ),
        slices=({"id": "a", "status": "merged"},), pr_url=None, next="resume",
        next_present=True, park_snapshot=None, read_result="ok",
    )
    assert positive_factory_proofs(snapshot) == (
        "factory_step:0:accepted:1",
        "factory_step:1:rejected:2",
        "factory_merged_slice:0",
    )


@pytest.mark.parametrize("source", list(AttentionSource))
def test_each_source_clearance_requires_all_strict_readers(source: AttentionSource) -> None:
    required = clearance_requirements(source)
    occurrence = {
        "source": source.value,
        "kind": (
            "start" if source.value.endswith("_start")
            else "success" if source.value.endswith("_success")
            else "legacy_attention" if source == AttentionSource.LEGACY_V1
            else "attention"
        ),
    }
    passing = {reader: "pass" for reader in required}
    assert inspect_clearance(occurrence, passing).read_errors == ()
    for reader in required:
        failed = dict(passing)
        failed[reader] = "error"
        inspection = inspect_clearance(occurrence, failed)
        assert inspection.read_errors == (reader,)
        assert inspection.result.value == "read_error"


def test_rehydration_reads_tracker_locks_evidence_and_pr_from_real_boundaries(
    tmp_path: Path,
) -> None:
    state_dir = dispatch_failure_state_dir(tmp_path)
    reservation = reserve_dispatch(
        state_dir, issue_id=42, target="leaf", autonomous=True
    )
    claim = ClaimIdentity(42, 1, "agent", "2026-09-18T00:00:00+00:00")
    confirm_claim_and_start(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim
    )
    evidence = tmp_path / "evidence.json"
    payload = {
        "issue_id": 42,
        "status": "completed",
        "head_sha": "a" * 40,
        "pr_url": "https://github.com/o/r/pull/1",
    }
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    occurrence = record_attention(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        source=AttentionSource.LEAF_BACKEND_OUTCOME,
        cause=AttentionCause.BACKEND_BLOCKED,
        facts={
            "type": "leaf", "backend": "fake", "checkout": str(tmp_path),
            "base": "main", "branch": "issue/42-a1", "isolated": True,
            "compute_result": "blocked", "backend_status": "blocked",
            "validation_reason_codes": [], "evidence_id": str(evidence),
            "evidence_sha256": digest,
            "pr_url": "https://github.com/o/r/pull/1", "head_sha": "a" * 40,
        },
        claim=claim,
        proof_ids=(f"leaf_outcome:{digest}",),
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["chainlink", "issue", "show"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"id": 42, "labels": [], "comments": []}), ""
            )
        if argv[:3] == ["chainlink", "locks", "list"]:
            return subprocess.CompletedProcess(argv, 0, '{"locks":[]}', "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({
                    "state": "OPEN", "headRefOid": "a" * 40,
                    "baseRefName": "main", "url": "https://github.com/o/r/pull/1",
                }),
                "",
            )
        raise AssertionError(argv)

    inspection = rehydrate_attention(
        tmp_path, occurrence["delivery_key"].rsplit(":", 1)[-1], runner=runner
    )
    assert inspection.result.value == "resolved"
    assert inspection.read_errors == ()


def test_rehydration_preserves_each_real_reader_error(tmp_path: Path) -> None:
    state_dir = dispatch_failure_state_dir(tmp_path)
    reservation = reserve_dispatch(
        state_dir, issue_id=42, target="leaf", autonomous=True
    )
    claim = ClaimIdentity(42, 1, "agent", "2026-09-18T00:00:00+00:00")
    start = confirm_claim_and_start(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim
    )

    def failed(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "failed")

    inspection = rehydrate_attention(
        tmp_path, start["delivery_key"].rsplit(":", 1)[-1], runner=failed
    )
    assert set(inspection.read_errors) == {
        ReaderCode.TRACKER,
        ReaderCode.CHAINLINK_LOCKS,
    }
    assert inspection.result.value == "read_error"
