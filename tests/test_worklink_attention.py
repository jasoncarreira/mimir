from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
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
    ClaimFacts,
    FactorySnapshot,
    InputFacts,
    ReaderCode,
    SOURCE_RULES,
    clearance_requirements,
    inspect_clearance,
    positive_factory_proofs,
    rehydrate_attention,
    validate_occurrence_contract,
)
from mimir.worklink.dispatch_failures import (
    bind_claim,
    confirm_claim_and_start,
    dispatch_failure_state_dir,
    record_attention,
    record_success_witness,
    reserve_dispatch,
)


def test_source_contract_is_closed() -> None:
    assert set(SOURCE_RULES) == set(AttentionSource)
    assert all(rule.causes or source.value.endswith(("_start", "_success")) for source, rule in SOURCE_RULES.items())


def test_start_is_never_diagnosis_or_consumption() -> None:
    timestamp = "2026-09-18T00:00:00+00:00"
    claim = ClaimIdentity(42, 1, "agent", timestamp)
    facts = {
        "type": "lifecycle_start", "target": "leaf", "claim": claim.to_json(),
        "admitted_at": timestamp, "run_id": None, "sandbox": None,
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


def test_clearance_evaluator_requires_every_declared_reader() -> None:
    required = clearance_requirements(AttentionSource.LEAF_BACKEND_OUTCOME)
    occurrence = {
        "source": AttentionSource.LEAF_BACKEND_OUTCOME.value,
        "kind": "attention",
    }
    passing = {reader: "pass" for reader in required}
    assert inspect_clearance(occurrence, passing).read_errors == ()
    for reader in required:
        failed = dict(passing)
        failed[reader] = "error"
        inspection = inspect_clearance(occurrence, failed)
        assert inspection.read_errors == (reader,)
        assert inspection.result.value == "read_error"


def test_clearance_evaluator_distinguishes_positive_and_blocked_reads() -> None:
    required = clearance_requirements(AttentionSource.LEAF_BACKEND_OUTCOME)
    occurrence = {
        "source": AttentionSource.LEAF_BACKEND_OUTCOME.value,
        "kind": "attention",
    }
    passing = {reader: "pass" for reader in required}
    positive = inspect_clearance(occurrence, passing)
    assert positive.result.value == "resolved"
    blocked = dict(passing)
    blocked[required[0]] = "blocked"
    negative = inspect_clearance(occurrence, blocked)
    assert negative.result.value == "unresolved"


def test_rehydration_reads_tracker_locks_evidence_and_pr_from_real_boundaries(
    tmp_path: Path,
) -> None:
    state_dir = dispatch_failure_state_dir(tmp_path)
    reservation = reserve_dispatch(
        state_dir, issue_id=42, target="leaf", autonomous=True
    )
    claim = ClaimIdentity(42, 1, "agent", "2026-09-18T00:00:00+00:00")
    bind_claim(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim, confirmed=False
    )
    confirm_claim_and_start(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim
    )
    blocked_evidence = tmp_path / "blocked-evidence.json"
    blocked_payload = {
        "issue_id": 42,
        "status": "blocked",
    }
    blocked_evidence.write_text(json.dumps(blocked_payload), encoding="utf-8")
    blocked_digest = hashlib.sha256(blocked_evidence.read_bytes()).hexdigest()
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
            "validation_reason_codes": [], "evidence_id": str(blocked_evidence),
            "evidence_sha256": blocked_digest,
            "pr_url": None, "head_sha": None,
        },
        claim=claim,
        proof_ids=(f"leaf_outcome:{blocked_digest}",),
    )
    evidence = tmp_path / "completed-evidence.json"
    payload = {
        "issue_id": 42,
        "status": "completed",
        "branch": "issue/42-a2",
        "head_sha": "a" * 40,
        "pr_url": "https://github.com/o/r/pull/1",
    }
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    record_success_witness(
        state_dir,
        issue_id=42,
        target="leaf",
        origin="manual",
        claim=None,
        run_id=None,
        sandbox=None,
        completed_at="2026-09-18T01:00:00+00:00",
        evidence_path=str(evidence.resolve()),
        evidence_sha256=digest,
        branch="issue/42-a2",
        head_sha="a" * 40,
        pr_url="https://github.com/o/r/pull/1",
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

    evidence.write_text(json.dumps({**payload, "head_sha": "b" * 40}), encoding="utf-8")
    tampered = rehydrate_attention(
        tmp_path, occurrence["delivery_key"].rsplit(":", 1)[-1], runner=runner
    )
    assert tampered.result.value == "read_error"
    assert ReaderCode.EVIDENCE in tampered.read_errors


def test_rehydration_preserves_each_real_reader_error(tmp_path: Path) -> None:
    state_dir = dispatch_failure_state_dir(tmp_path)
    reservation = reserve_dispatch(
        state_dir, issue_id=42, target="leaf", autonomous=True
    )
    claim = ClaimIdentity(42, 1, "agent", "2026-09-18T00:00:00+00:00")
    bind_claim(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim, confirmed=False
    )
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


def test_factory_rehydration_uses_the_returned_exact_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mimir.worklink.backends.feature_factory import (
        FeatureFactoryBackend,
        parse_factory_status,
    )
    from mimir.worklink.compute import LaunchHandle
    from mimir.worklink.factory_state import FactoryRunRecord, save_factory_record

    sandbox = tmp_path / "chainlink-42"
    sandbox.mkdir()
    status = parse_factory_status({
        "run_id": "chainlink-42",
        "valid": True,
        "sandbox_path": str(sandbox),
        "status": "running",
        "mode": "autonomous",
        "branch": "epic/42",
        "pr_base": "main",
        "pr_draft": False,
        "lock": "fresh",
        "dead_lock": False,
        "lock_session": "session-42",
        "gates": {},
        "steps": [],
        "slices": [],
        "pr_url": None,
        "next": "implementation",
    })
    save_factory_record(tmp_path, FactoryRunRecord(
        run_id="chainlink-42",
        issue_id=42,
        attempt=1,
        repository="owner/repo",
        base_ref="main",
        branch="epic/42",
        launcher="/tmp/factory.js",
        sandbox=str(sandbox),
        session="session-42",
        handle=LaunchHandle("local_subprocess", "999999", 1),
        status=status,
        observed_at="2026-09-18T00:00:00+00:00",
        controller_phase="running",
    ))
    monkeypatch.setattr(
        FeatureFactoryBackend,
        "status",
        lambda self, run_id, **kwargs: status,
    )
    state_dir = dispatch_failure_state_dir(tmp_path)
    reservation = reserve_dispatch(
        state_dir, issue_id=42, target="factory", autonomous=True
    )
    claim = ClaimIdentity(42, 1, "agent", "2026-09-18T00:00:00+00:00")
    bind_claim(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim, confirmed=False
    )
    start = confirm_claim_and_start(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        claim=claim,
        run_id="chainlink-42",
        sandbox=str(sandbox),
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["chainlink", "issue", "show"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"id": 42, "labels": [], "comments": []}), ""
            )
        if argv[:3] == ["chainlink", "locks", "list"]:
            return subprocess.CompletedProcess(argv, 0, '{"locks":[]}', "")
        raise AssertionError(argv)

    inspection = rehydrate_attention(
        tmp_path, start["delivery_key"].rsplit(":", 1)[-1], runner=runner
    )

    assert ReaderCode.FACTORY_STATUS not in inspection.read_errors


def test_live_start_owner_and_own_lock_remain_current(tmp_path: Path) -> None:
    state_dir = dispatch_failure_state_dir(tmp_path)
    reservation = reserve_dispatch(
        state_dir,
        issue_id=42,
        target="leaf",
        autonomous=True,
        owner_pid=os.getpid(),
        owner_start_ticks=None,
    )
    claim = ClaimIdentity(42, 1, "agent", "2026-09-18T00:00:00+00:00")
    bind_claim(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim, confirmed=False
    )
    start = confirm_claim_and_start(
        state_dir, issue_id=42, reservation_id=reservation, claim=claim
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["chainlink", "issue", "show"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"id": 42, "labels": ["worklink:in-progress"], "comments": []}), ""
            )
        if argv[:3] == ["chainlink", "locks", "list"]:
            return subprocess.CompletedProcess(argv, 0, '{"locks":[{"issue_id":42}]}', "")
        raise AssertionError(argv)

    inspection = rehydrate_attention(
        tmp_path, start["delivery_key"].rsplit(":", 1)[-1], runner=runner
    )
    assert inspection.result.value == "current"
    assert inspection.current_state == "active"
    assert inspection.read_errors == ()


@pytest.mark.parametrize(
    "source",
    [
        AttentionSource.LEAF_BACKEND,
        AttentionSource.LEAF_COMPUTE,
        AttentionSource.FACTORY_INTERLOCK,
        AttentionSource.FACTORY_BACKEND,
        AttentionSource.FACTORY_COMPUTE,
        AttentionSource.FACTORY_LAUNCHER,
        AttentionSource.FACTORY_BASE_LOOKUP,
        AttentionSource.FACTORY_WORK_ITEM,
        AttentionSource.CLAIM_COMMAND,
        AttentionSource.CLAIM_GUARD,
        AttentionSource.CLAIM_STEAL,
        AttentionSource.CLAIM_CAPACITY_READ,
        AttentionSource.CLAIM_UNREADY,
        AttentionSource.CLAIM_INPROGRESS,
        AttentionSource.CLAIM_COMMENT,
        AttentionSource.CLAIM_CONTENTION,
    ],
    ids=lambda source: source.value,
)
def test_named_validator_rehydrates_through_its_read_only_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: AttentionSource,
) -> None:
    from mimir.worklink.backends.feature_factory import FeatureFactoryBackend
    from mimir.worklink.claims import ClaimRecord

    issue_id = 42
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("WORKLINK_REPO", str(repo))
    monkeypatch.setattr(
        FeatureFactoryBackend, "admit", lambda self: Path("/opt/factory.js")
    )
    (tmp_path / "worklink.yaml").write_text(
        "defaults:\n  test_command: uv run pytest\n", encoding="utf-8"
    )
    state_dir = dispatch_failure_state_dir(tmp_path)
    target = "factory" if source.value.startswith("factory_") else "leaf"
    reservation = reserve_dispatch(
        state_dir, issue_id=issue_id, target=target, autonomous=True
    )
    intended = ClaimIdentity(
        issue_id, 1, "agent", "2026-09-18T00:00:00+00:00"
    )
    if SOURCE_RULES[source].fact_type == "claim":
        facts: object = ClaimFacts(
            intended if source == AttentionSource.CLAIM_COMMENT else None,
            None,
            "blocked",
            mutation_stage="comment" if source == AttentionSource.CLAIM_COMMENT else None,
        )
    else:
        facts = InputFacts(validator=source.value, result="blocked")
    cause = next(iter(SOURCE_RULES[source].causes))
    occurrence = record_attention(
        state_dir,
        issue_id=issue_id,
        reservation_id=reservation,
        source=source,
        cause=cause,
        facts=facts,
    )
    labels = ["worklink:epic"]
    if source == AttentionSource.CLAIM_INPROGRESS:
        labels.append("worklink:in-progress")
    comments = []
    if source == AttentionSource.CLAIM_COMMENT:
        comments.append(
            ClaimRecord(
                intended.issue_id,
                intended.attempt,
                intended.agent_id,
                datetime.fromisoformat(intended.claimed_at),
            ).to_comment()
        )

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["chainlink", "issue", "show"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({
                    "id": issue_id,
                    "title": "Build the epic",
                    "description": "Target branch: main\n\nAcceptance criteria:\n- ship it",
                    "labels": labels,
                    "comments": comments,
                }),
                "",
            )
        if argv[:3] == ["chainlink", "locks", "list"]:
            return subprocess.CompletedProcess(argv, 0, '{"locks":[]}', "")
        if argv[:2] == ["git", "-C"] and "ls-remote" in argv:
            return subprocess.CompletedProcess(argv, 0, "a" * 40 + "\n", "")
        raise AssertionError(argv)

    inspection = rehydrate_attention(
        tmp_path,
        occurrence["delivery_key"].rsplit(":", 1)[-1],
        runner=runner,
    )

    assert inspection.result.value == "resolved"
    assert inspection.read_errors == ()


@pytest.mark.parametrize(
    "source",
    [
        AttentionSource.FACTORY_BASE_LOOKUP,
        AttentionSource.CLAIM_COMMAND,
        AttentionSource.CLAIM_STEAL,
        AttentionSource.CLAIM_CAPACITY_READ,
        AttentionSource.CLAIM_CONTENTION,
    ],
    ids=lambda source: source.value,
)
def test_named_validator_preserves_its_external_reader_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: AttentionSource
) -> None:
    monkeypatch.setenv("WORKLINK_REPO", str(tmp_path))
    (tmp_path / "worklink.yaml").write_text(
        "defaults:\n  test_command: uv run pytest\n", encoding="utf-8"
    )
    state_dir = dispatch_failure_state_dir(tmp_path)
    reservation = reserve_dispatch(
        state_dir,
        issue_id=43,
        target="factory" if source.value.startswith("factory_") else "leaf",
        autonomous=True,
    )
    occurrence = record_attention(
        state_dir,
        issue_id=43,
        reservation_id=reservation,
        source=source,
        cause=next(iter(SOURCE_RULES[source].causes)),
        facts=(
            InputFacts(validator=source.value, result="blocked")
            if SOURCE_RULES[source].fact_type == "input"
            else ClaimFacts(None, None, "blocked")
        ),
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["chainlink", "issue", "show"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"id": 43, "labels": [], "comments": []}), ""
            )
        return subprocess.CompletedProcess(argv, 1, "", "reader unavailable")

    inspection = rehydrate_attention(
        tmp_path,
        occurrence["delivery_key"].rsplit(":", 1)[-1],
        runner=runner,
    )

    assert inspection.result.value == "read_error"
    assert ReaderCode.VALIDATOR in inspection.read_errors
