"""The Cultivate copies own Worklink's persisted and observed record behavior."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.worklink.compute import LaunchHandle
from mimir_cultivate.worklink.backends.base import blocked_reason_from_output
from mimir_cultivate.worklink.evidence import (
    TestResult,
    WorklinkEvidence,
    validate_evidence,
)
from mimir_cultivate.worklink.factory_state import (
    FactoryRecordError,
    FactoryRunRecord,
    load_factory_record,
    save_factory_record,
)
from mimir_cultivate.worklink import identities
from mimir_cultivate.worklink.planning import target_branch_from_description
from mimir_cultivate.worklink.run_state import WorklinkRunState
from mimir_cultivate.worklink.safe_git import SafeGitError, _validated_branch_ref


def _factory_record(tmp_path: Path) -> FactoryRunRecord:
    return FactoryRunRecord(
        run_id="chainlink-1949", issue_id=1949, attempt=1,
        repository="owner/repo", base_ref="main", branch="feature/1949",
        launcher="/usr/local/bin/factory", sandbox=str(tmp_path / "chainlink-1949"),
        session=None, handle=LaunchHandle("local_subprocess", "123", 456),
        status=None, observed_at=None, controller_phase="running",
    )


def test_factory_record_round_trips_v2_and_reads_v1(tmp_path: Path) -> None:
    record = _factory_record(tmp_path)
    path = save_factory_record(tmp_path, record)
    assert load_factory_record(tmp_path, record.run_id) == record
    legacy = record.to_json()
    legacy["version"] = 1
    del legacy["transcript"]
    assert FactoryRunRecord.from_json(legacy) == record
    with pytest.raises(FactoryRecordError, match="fields are invalid"):
        FactoryRunRecord.from_json({**legacy, "untrusted": True})
    assert path.is_file()


def test_factory_record_refuses_symlinked_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "state" / "worklink").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(FactoryRecordError, match="not contained"):
        save_factory_record(home, _factory_record(tmp_path))


def test_run_state_accepts_legacy_pid_but_requires_worker_identity() -> None:
    state = WorklinkRunState(
        issue_id=1949, attempt=1, backend="opencode", compute_name="local_subprocess",
        handle_substrate="local_subprocess", handle_identifier="123", branch="feature/1949",
        base_ref="main", local_base="main", repo="owner/repo", repo_url="url",
        test_command=None, started_at="2026-01-01T00:00:00+00:00",
    )
    assert WorklinkRunState.from_json(state.to_json()) == state
    legacy = state.to_json()
    legacy["version"] = 1
    assert WorklinkRunState.from_json(legacy).version == 1
    with pytest.raises(ValueError, match="canonical UUIDv4"):
        WorklinkRunState.from_json({**state.to_json(), "shim_pid": 123})


def test_completed_evidence_needs_observed_tests_and_diff() -> None:
    evidence = WorklinkEvidence(
        issue=1949, attempt=1, backend="opencode", branch="feature/1949",
        checkout="/tmp/checkout", started_at="start", finished_at="finish",
        files_changed=["change.py"], diff_stat="1 file changed", commands=[],
        tests=TestResult("pytest", exit_code=0), pr_url=None, status="completed",
    )
    assert validate_evidence(evidence).review_ready
    unobserved = validate_evidence(replace(evidence, tests=replace(evidence.tests, observed=False)))
    assert not unobserved.review_ready
    assert "tests_not_observed" in unobserved.reasons
    assert not validate_evidence(replace(evidence, diff_observed=False)).review_ready


def test_planning_branch_and_git_publication_reject_unsafe_refs() -> None:
    assert target_branch_from_description("Worklink notes:\n- Target branch: feature/1949") == "feature/1949"
    with pytest.raises(ValueError, match="invalid Worklink target branch"):
        target_branch_from_description("Worklink notes:\n- Target branch: ../escape")
    with pytest.raises(SafeGitError, match="invalid"):
        _validated_branch_ref("../escape")


def test_backend_blocked_marker_must_end_the_output() -> None:
    assert blocked_reason_from_output("WORKLINK_BLOCKED: needs design\n", "") == "needs design"
    assert blocked_reason_from_output("WORKLINK_BLOCKED: example\ndone", "") is None


def test_identity_lookup_requires_both_accounts_and_group(monkeypatch: pytest.MonkeyPatch) -> None:
    identities.get_identities.cache_clear()
    monkeypatch.setattr(identities.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid={"mimir": 1001, "worklink": 1002}[name]))
    monkeypatch.setattr(identities.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=1002))
    try:
        assert identities.get_identities() == identities.WorklinkIdentities(1001, 1002, 1002)
    finally:
        identities.get_identities.cache_clear()
    def missing_group(name: str) -> None:
        raise KeyError(name)
    monkeypatch.setattr(identities.grp, "getgrnam", missing_group)
    with pytest.raises(RuntimeError, match="required group 'worklink'"):
        identities.get_identities()
    identities.get_identities.cache_clear()
