from __future__ import annotations

from dataclasses import asdict

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
    validate_occurrence_contract,
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
