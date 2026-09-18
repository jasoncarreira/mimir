from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mimir.worklink.backends.feature_factory import parse_factory_status
from mimir.worklink.compute import LaunchHandle
from mimir.worklink.attention import ClaimIdentity
from mimir.worklink.dispatch_failures import (
    bind_claim,
    confirm_claim_and_start,
    dispatch_failure_state_dir,
    load_outcome_state,
    reserve_dispatch,
)
from mimir.worklink.factory_state import (
    FactoryRecordError,
    FactoryRunRecord,
    archive_factory_record,
    factory_manifest_candidates,
    immutable_factory_snapshot,
    load_factory_records_for_issue,
    list_factory_records,
    load_factory_record,
    report_retained_factory_records,
    save_factory_record,
)


def record(tmp_path: Path) -> FactoryRunRecord:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(exist_ok=True)
    status = parse_factory_status(
        {
            "run_id": "1551",
            "issue_key": "1551",
            "valid": True,
            "sandbox_path": str(sandbox),
            "status": "running",
            "mode": "autonomous",
            "branch": "epic/1551",
            "pr_base": "main",
            "pr_draft": False,
            "lock": "fresh",
            "dead_lock": False,
            "lock_session": "session-1",
            "gates": {},
            "steps": [{"agent": "implementation", "status": "running", "attempts": 1}],
            "slices": [{"id": "factory-070-migration", "status": "ready", "attempts": 0}],
            "validator": None,
            "pr_url": None,
            "terminal_result": None,
            "next": "implementation",
        }
    )
    return FactoryRunRecord(
        run_id="1551",
        issue_id=1551,
        attempt=1,
        repository="owner/repo",
        base_ref="main",
        branch="epic/1551",
        launcher="/opt/mimir-opencode/lib/node_modules/feature-factory/bin/factory.js",
        sandbox=str(sandbox),
        session="session-1",
        handle=LaunchHandle("local_subprocess", "123", 456),
        status=status,
        observed_at="2026-08-18T12:00:00+00:00",
        controller_phase="running",
    )


def test_factory_record_round_trip_is_atomic_and_has_no_cost_fields(tmp_path: Path) -> None:
    expected = record(tmp_path)
    path = save_factory_record(tmp_path, expected)
    assert path == tmp_path / "state" / "worklink" / "factory-runs" / "1551.json"
    assert load_factory_record(tmp_path, "1551") == expected
    assert list_factory_records(tmp_path) == [expected]
    assert "cost" not in path.read_text(encoding="utf-8")
    assert not list(path.parent.glob("*.tmp"))


def test_retained_startup_observation_keeps_exact_reservation_recoverable(
    tmp_path: Path,
) -> None:
    retained = record(tmp_path)
    save_factory_record(tmp_path, retained)
    state_dir = dispatch_failure_state_dir(tmp_path)
    reservation = reserve_dispatch(
        state_dir, issue_id=retained.issue_id, target="factory", autonomous=True
    )
    claim = ClaimIdentity(
        retained.issue_id, retained.attempt, "original-agent",
        "2026-09-18T00:00:00+00:00",
    )
    bind_claim(
        state_dir,
        issue_id=retained.issue_id,
        reservation_id=reservation,
        claim=claim,
        confirmed=False,
    )
    confirm_claim_and_start(
        state_dir,
        issue_id=retained.issue_id,
        reservation_id=reservation,
        claim=claim,
        run_id=retained.run_id,
        sandbox=retained.sandbox,
    )

    report_retained_factory_records(tmp_path, event_logger=lambda *args, **kwargs: None)
    report_retained_factory_records(tmp_path, event_logger=lambda *args, **kwargs: None)

    state = load_outcome_state(state_dir)
    current = state["issues"][str(retained.issue_id)]["reservations"][reservation]
    observations = [
        occurrence
        for occurrence in state["issues"][str(retained.issue_id)]["occurrences"].values()
        if occurrence["source"] == "factory_startup_reconcile"
    ]
    assert current["state"] == "active"
    assert current["claim_state"] == "confirmed"
    assert current["disposition"] == "pending"
    assert len(observations) == 1
    assert observations[0]["accounting"]["scope"] == "deferred"
    assert observations[0]["accounting"]["claim"] == claim.to_json()


def test_factory_record_round_trip_preserves_structured_status(tmp_path: Path) -> None:
    expected = record(tmp_path)
    assert expected.status is not None
    status = replace(
        expected.status,
        steps=({"agent": "implementation", "status": "completed", "attempts": 2},),
        slices=({"id": "factory-070-migration", "status": "merged", "attempts": 1},),
        gates={
            "pre_pr": {
                "status": "passed",
                "at": "2026-08-18T12:00:00+00:00",
                "artifact": "reports/pre-pr.json",
                "reviewed_head": "abc123",
            },
            "post_pr": {
                "status": "pending", "at": None, "artifact": None, "reviewed_head": None,
            },
        },
        validator={
            "verdict": "GO", "report": "reports/validator.md", "reviewed_head": "abc123",
            "loops": 2,
        },
        next_action={"kind": "pr", "subject": None},
    )
    expected = replace(expected, status=status)

    path = save_factory_record(tmp_path, expected)

    assert load_factory_record(tmp_path, expected.run_id) == expected
    persisted = json.loads(path.read_text(encoding="utf-8"))["status"]
    assert persisted["steps"] == list(status.steps)
    assert persisted["slices"] == list(status.slices)
    assert persisted["gates"] == status.gates
    assert persisted["validator"] == status.validator
    assert persisted["next_action"] == status.next_action


def test_immutable_snapshot_precedes_observed_error_clearing(tmp_path: Path) -> None:
    original = replace(record(tmp_path), controller_error="retained controller failure")
    assert original.status is not None
    terminal = replace(
        original.status,
        status="partial",
        steps=({"agent": "spec", "status": "rejected", "attempts": 2},),
        slices=({"id": "build", "status": "merged", "attempts": 1},),
        next="resume:spec",
        next_present=True,
    )
    pre_observed = replace(original, status=terminal)
    snapshot = immutable_factory_snapshot(pre_observed, read_result="captured")
    observed = original.observed(terminal, "2026-09-18T00:00:00+00:00")

    assert observed.controller_error is None
    assert snapshot.controller_error == "retained controller failure"
    assert snapshot.status == "partial"
    assert snapshot.steps == terminal.steps
    assert snapshot.slices == terminal.slices
    assert snapshot.next == "resume:spec"
    assert snapshot.next_present is True


def test_issue_lookup_reads_both_keys_without_legacy_shadowing_canonical(
    tmp_path: Path,
) -> None:
    legacy = record(tmp_path)
    canonical = replace(
        legacy,
        run_id="chainlink-1551",
        attempt=legacy.attempt,
        branch="feature/chainlink-1551",
        sandbox=str(tmp_path / "chainlink-1551"),
        status=None,
    )
    save_factory_record(tmp_path, legacy)
    save_factory_record(tmp_path, canonical)

    assert load_factory_records_for_issue(tmp_path, 1551) == [canonical, legacy]


def test_archive_factory_record_preserves_evidence_and_vacates_active_slot(
    tmp_path: Path,
) -> None:
    expected = replace(record(tmp_path), controller_phase="failed", session=None)
    source = save_factory_record(tmp_path, expected)

    archived = archive_factory_record(tmp_path, expected)

    assert archived == source.parent / "archive" / "1551-attempt-1.json"
    assert FactoryRunRecord.from_json(json.loads(archived.read_text(encoding="utf-8"))) == expected
    assert load_factory_record(tmp_path, "1551") is None


def test_archive_factory_record_restores_active_record_when_event_persistence_fails(
    tmp_path: Path,
) -> None:
    expected = replace(record(tmp_path), controller_phase="failed", session=None)
    source = save_factory_record(tmp_path, expected)

    def fail_event(*args: object, **kwargs: object) -> None:
        raise OSError("event sink unavailable")

    with pytest.raises(FactoryRecordError, match="event could not be persisted"):
        archive_factory_record(
            tmp_path,
            expected,
            event_logger=fail_event,
            source_kind="dispatch_abandonment",
            reason="retained factory session is missing",
        )

    assert load_factory_record(tmp_path, "1551") == expected
    assert source.is_file()
    assert not list(source.parent.joinpath("archive").glob("*.json"))


def _retained_record(home: Path, repository: Path, *, sandbox_exists: bool = True) -> FactoryRunRecord:
    sandbox = repository / ".factory-sandboxes" / "1551"
    if sandbox_exists:
        sandbox.mkdir(parents=True)
    expected = replace(
        record(home),
        sandbox=str(sandbox),
        status=None,
        observed_at="2026-08-01T00:00:00+00:00",
        controller_phase="failed",
    )
    save_factory_record(home, expected)
    return expected


@pytest.mark.parametrize("verified_dead", [False, True])
def test_report_abandoned_running_record(tmp_path: Path, monkeypatch, verified_dead: bool) -> None:
    import mimir.worklink.factory_state as factory_state

    expected = record(tmp_path)
    save_factory_record(tmp_path, expected)
    monkeypatch.setattr(factory_state, "factory_process_is_verified_dead", lambda value: verified_dead)
    events = []
    report_retained_factory_records(
        tmp_path, event_logger=lambda event, **payload: events.append((event, payload))
    )
    assert bool(events) is verified_dead
    if verified_dead:
        assert events[0][1]["phase"] == "running"
        assert events[0][1]["run_id"] == expected.run_id
    assert load_factory_record(tmp_path, expected.run_id) == expected


def test_report_retained_factory_records_preserves_control_plane(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    expected = _retained_record(tmp_path, repository)
    root_manifest, sandbox_manifest = factory_manifest_candidates(expected)
    assert root_manifest == repository / ".factory" / "1551"
    assert sandbox_manifest == repository / ".factory-sandboxes" / "1551" / ".factory" / "1551"
    root_manifest.mkdir(parents=True)
    sandbox_manifest.mkdir(parents=True)
    events: list[tuple[str, dict[str, object]]] = []

    result = report_retained_factory_records(
        tmp_path,
        event_logger=lambda event, **payload: events.append((event, payload)),
    )

    assert result is None
    assert root_manifest.exists()
    assert sandbox_manifest.exists()
    assert Path(expected.sandbox).exists()
    assert load_factory_record(tmp_path, expected.run_id) == expected
    assert events[0][0] == "worklink_factory_run_retained"
    assert events[0][1]["sandbox"] == expected.sandbox


@pytest.mark.parametrize("reserved", [".parked", ".staging-1551", ".prior-1551"])
def test_factory_manifest_candidates_reject_reserved_control_plane_names(
    tmp_path: Path, reserved: str
) -> None:
    sandbox = tmp_path / "repo" / ".factory-sandboxes" / reserved
    forged = cast(
        FactoryRunRecord,
        SimpleNamespace(run_id=reserved, sandbox=str(sandbox)),
    )

    with pytest.raises(FactoryRecordError, match="retained-run layout"):
        factory_manifest_candidates(forged)


def test_report_retained_factory_records_never_touches_unresumed_sandbox(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    expected = _retained_record(tmp_path, repository)
    root_manifest, sandbox_manifest = factory_manifest_candidates(expected)
    root_manifest.mkdir(parents=True)
    sandbox_manifest.mkdir(parents=True)
    result = report_retained_factory_records(
        tmp_path,
        event_logger=lambda *args, **kwargs: None,
    )

    assert result is None
    assert root_manifest.is_dir()
    assert sandbox_manifest.is_dir()
    assert Path(expected.sandbox).is_dir()
    assert load_factory_record(tmp_path, expected.run_id) == expected


def test_report_retained_factory_records_keeps_missing_sandbox_record(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    expected = _retained_record(tmp_path, repository, sandbox_exists=False)
    root_manifest, _ = factory_manifest_candidates(expected)
    root_manifest.mkdir(parents=True)

    result = report_retained_factory_records(
        tmp_path,
        event_logger=lambda *args, **kwargs: None,
    )

    assert result is None
    assert root_manifest.exists()
    assert load_factory_record(tmp_path, expected.run_id) == expected


def test_factory_record_rejects_identity_and_sandbox_mismatch(tmp_path: Path) -> None:
    expected = record(tmp_path)
    with pytest.raises(FactoryRecordError, match="identity"):
        replace(expected, issue_id=1552)
    with pytest.raises(FactoryRecordError, match="sandbox mismatch"):
        replace(expected, sandbox=str(tmp_path / "different"))


def test_factory_record_rejects_sandbox_path_for_another_run(tmp_path: Path) -> None:
    expected = replace(
        record(tmp_path),
        run_id="chainlink-1551",
        sandbox=str(tmp_path / "chainlink-1551"),
        status=None,
    )

    with pytest.raises(FactoryRecordError, match="sandbox does not match run id"):
        replace(expected, sandbox=str(tmp_path / "chainlink-1552"))


def test_factory_record_binds_status_run_id_to_durable_identity(tmp_path: Path) -> None:
    expected = record(tmp_path)
    status = expected.status
    assert status is not None

    nullable_base = replace(
        expected,
        status=replace(
            status,
            pr_base=None,
            validator={"verdict": "GO", "report": None, "reviewed_head": None, "loops": 0},
            terminal_result={"reason": "opaque"},
        ),
    )
    assert FactoryRunRecord.from_json(nullable_base.to_json()) == nullable_base

    assert replace(expected, status=replace(status, issue_key=None)).status is not None
    assert replace(expected, status=replace(status, issue_key="display-only")).status is not None
    with pytest.raises(FactoryRecordError, match="identity mismatch"):
        replace(expected, status=replace(status, run_id="1552"))
    with pytest.raises(
        FactoryRecordError,
        match="observed 'develop', expected 'main'",
    ):
        replace(expected, status=replace(status, pr_base="develop"))


def test_factory_record_schema_remains_exact(tmp_path: Path) -> None:
    payload = record(tmp_path).to_json()
    payload["future_record_metadata"] = {"generation": 2}
    with pytest.raises(FactoryRecordError, match="fields are invalid"):
        FactoryRunRecord.from_json(payload)


def test_factory_record_upgrades_legacy_record_without_transcript(tmp_path: Path) -> None:
    payload = record(tmp_path).to_json()
    payload["version"] = 1
    del payload["transcript"]

    upgraded = FactoryRunRecord.from_json(payload)

    assert upgraded.version == 2
    assert upgraded.transcript is None


def test_factory_record_rejects_symlink_and_unbound_run_ids(tmp_path: Path) -> None:
    expected = record(tmp_path)
    path = save_factory_record(tmp_path, expected)
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    path.symlink_to(outside)
    with pytest.raises(FactoryRecordError, match="regular"):
        load_factory_record(tmp_path, "1551")
    with pytest.raises(FactoryRecordError, match="run id"):
        load_factory_record(tmp_path, "not/a/run-id")


@pytest.mark.parametrize("operation", ["load", "list", "save"])
def test_factory_record_rejects_symlink_in_complete_parent_chain(
    tmp_path: Path, operation: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    (outside / "worklink" / "factory-runs").mkdir(parents=True)
    (home / "state").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FactoryRecordError, match="not contained"):
        if operation == "load":
            load_factory_record(home, "1551")
        elif operation == "list":
            list_factory_records(home)
        elif operation == "save":
            save_factory_record(home, replace(record(tmp_path), sandbox=str(tmp_path / "sandbox")))
