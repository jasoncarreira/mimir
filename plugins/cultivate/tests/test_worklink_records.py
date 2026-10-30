"""The Cultivate copies own Worklink's persisted and observed record behavior."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mimir_cultivate.worklink.backends.base import (
    ComputeLaunchError, ComputeResult, LaunchHandle, WorkSpec, blocked_reason_from_output,
)
from mimir_cultivate.worklink import evidence as provider_evidence
from mimir_cultivate.worklink.evidence import (
    TestResult,
    WorklinkEvidence,
    observe_evidence,
    validate_evidence,
)
from mimir_cultivate.worklink.factory_state import (
    FactoryRecordError,
    FactoryRunRecord,
    load_factory_record,
    parse_factory_status,
    save_factory_record,
)
from mimir_cultivate.worklink import identities
from mimir_cultivate.worklink.planning import target_branch_from_description
from mimir_cultivate.worklink.run_state import WorklinkRunState
from mimir_cultivate.worklink.safe_git import (
    ControllerGitPublication, SafeGitError, _validated_branch_ref,
)


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


def test_factory_record_keeps_pre_prompt_change_v2_schema(tmp_path: Path) -> None:
    record = _factory_record(tmp_path)
    payload = record.to_json()
    # Pin the existing strict reader's accepted keys independently of the writer.
    assert payload["version"] == 2
    assert set(payload) == {
        "version", "run_id", "issue_id", "attempt", "repository", "base_ref",
        "branch", "launcher", "sandbox", "session", "handle", "status",
        "observed_at", "controller_phase", "controller_error", "transcript",
        "children_reaped",
    }
    assert FactoryRunRecord.from_json(payload) == record
    with pytest.raises(FactoryRecordError, match="fields are invalid"):
        FactoryRunRecord.from_json({**payload, "issue_title": "Display only"})


def test_factory_record_refuses_symlinked_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "state" / "worklink").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(FactoryRecordError, match="not contained"):
        save_factory_record(home, _factory_record(tmp_path))


def test_populated_factory_record_recovers_legacy_status_and_handle(tmp_path: Path) -> None:
    record = _factory_record(tmp_path)
    status = parse_factory_status({
        "run_id": record.run_id, "issue_key": str(record.issue_id), "valid": True,
        "sandbox_path": record.sandbox, "status": "running", "mode": "autonomous",
        "branch": record.branch, "pr_base": record.base_ref, "pr_draft": False,
        "lock": "fresh", "dead_lock": False, "lock_session": "session-1",
        "gates": {}, "steps": [], "slices": [], "validator": None, "pr_url": None,
        "terminal_result": None, "next": "implementation",
    })
    populated = replace(record, status=status, observed_at="2026-10-01T12:00:00+00:00")
    assert load_factory_record(tmp_path, populated.run_id) is None
    save_factory_record(tmp_path, populated)
    assert load_factory_record(tmp_path, populated.run_id) == populated
    legacy = populated.to_json()
    legacy["version"] = 1
    del legacy["transcript"]
    assert FactoryRunRecord.from_json(legacy) == populated
    assert FactoryRunRecord.from_json(populated.to_json()).handle == populated.handle
    with pytest.raises(FactoryRecordError, match="status identity mismatch"):
        FactoryRunRecord.from_json({**populated.to_json(), "run_id": str(populated.issue_id)})


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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_provider_publication_pins_head_and_refuses_replaced_git_fd(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Trusted User")
    _git(repo, "config", "user.email", "trusted@example.test")
    _git(repo, "remote", "add", "origin", str(tmp_path / "remote.git"))
    (repo / "tracked.txt").write_text("original\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "original")
    branch = _git(repo, "branch", "--show-current")
    initial_head = _git(repo, "rev-parse", "HEAD")
    fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    try:
        publication = ControllerGitPublication.capture(
            fd, trusted_repo=repo, branch=branch, metadata_root=tmp_path / "metadata",
        )
        try:
            (repo / "tracked.txt").write_text("worker change\n")
            publication.run("add", "tracked.txt", check=True)
            publication.run("commit", "-m", "private commit", check=True)
            assert publication.initial_head == initial_head
            assert publication.run("rev-parse", "HEAD", check=True).stdout.strip() != initial_head
            assert _git(repo, "rev-parse", "HEAD") == initial_head
            (repo / ".git").rename(repo / "git-original")
            (repo / ".git").symlink_to(repo / "git-original", target_is_directory=True)
            with pytest.raises(SafeGitError, match="unavailable or unsafe"):
                publication.run("status")
            (repo / ".git").unlink()
            (repo / "git-original").rename(repo / ".git")
            (repo / ".git").rename(repo / "git-original")
            (repo / ".git").mkdir()
            with pytest.raises(SafeGitError, match="replaced"):
                publication.run("status")
        finally:
            publication.close()
    finally:
        os.close(fd)


def test_backend_blocked_marker_must_end_the_output() -> None:
    assert blocked_reason_from_output("WORKLINK_BLOCKED: needs design\n", "") == "needs design"
    assert blocked_reason_from_output("WORKLINK_BLOCKED: example\ndone", "") is None


def test_provider_records_import_without_retiring_core_modules() -> None:
    script = """
import importlib.abc
import sys

retiring = (
    'mimir.worklink.compute', 'mimir.worklink.dispatch_failures',
    'mimir.worklink.backends.feature_factory', 'mimir.worklink.backends.registry',
    'mimir.coding', 'mimir.tools._shell_env',
)
class BlockRetired(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in retiring):
            raise ImportError('retired dependency: ' + fullname)
        return None
sys.meta_path.insert(0, BlockRetired())
import mimir_cultivate.worklink.backends.base
import mimir_cultivate.worklink.factory_state
import mimir_cultivate.worklink.evidence
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# The real accessor is the subject (pwd/grp are faked below), so opt out of the
# suite's synthetic replacement of it.
@pytest.mark.real_worklink_identities
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


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_selected_provider_gate_never_runs_controller_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authorized: bool,
) -> None:
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    called: list[object] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        called.append((args, kwargs))
        raise AssertionError("checkout test command ran as controller")

    monkeypatch.setattr(provider_evidence, "_run", forbidden)
    class GitObservation:
        def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, "changed.py\n" if "--name-only" in args else "")

    compute = SimpleNamespace(name="local_subprocess", _authorized_checkout=None)
    spec = WorkSpec(
        issue_id=1949, attempt=1, repo_url="repo", base_ref="main",
        branch="feature/1949", prompt="", rules=None, test_command="pytest -q",
        backend="opencode", timeout_s=30, local_checkout=tmp_path,
    )
    if authorized:
        compute._authorized_checkout = SimpleNamespace(
            verify=lambda checkout: None, duplicate_fd=lambda: None, path=tmp_path,
            issue_id=1949, attempt=1,
        )
        compute._launch_contained = lambda *_: called.append("should not launch")
    with pytest.raises(ComputeLaunchError, match="worker-bound checkout"):
        await observe_evidence(
            issue=1949, attempt=1, backend="opencode", branch="feature/1949",
            checkout=tmp_path, started_at=datetime.now(UTC), base_ref="main",
            backend_status="completed", test_command="touch controller-canary",
            safe_git=GitObservation(), compute=compute,
            work_spec=None if authorized else spec,
        )
    assert not called
    assert not (tmp_path / "controller-canary").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_exit", [0, 1])
async def test_selected_provider_observes_gate_through_authorized_compute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_exit: int,
) -> None:
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    calls: list[object] = []
    authorization = SimpleNamespace(
        verify=lambda checkout: calls.append("verified"), duplicate_fd=lambda: None,
        path=tmp_path, issue_id=1949, attempt=1,
    )
    compute = SimpleNamespace(name="local_subprocess", _authorized_checkout=authorization)
    handle = LaunchHandle("local_subprocess", "worker-handle")

    async def launch(spec: WorkSpec, command: tuple[str, ...]) -> LaunchHandle:
        calls.append(spec)
        assert command == ("/bin/sh", "-c", "pytest -q")
        return handle

    async def wait(observed: LaunchHandle, timeout: int) -> ComputeResult:
        assert observed == handle
        return ComputeResult(exit_code=gate_exit, stdout="passed" if not gate_exit else "1 failed: test_worker_gate", stderr="")

    async def cleanup(observed: LaunchHandle) -> None:
        assert observed == handle

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("controller shell must not run the gate")

    class GitObservation:
        def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, "changed.py\n" if "--name-only" in args else "")

    compute._launch_contained = launch
    compute.wait = wait
    compute.cleanup = cleanup
    monkeypatch.setattr(provider_evidence, "_run", forbidden)
    result = await observe_evidence(
        issue=1949, attempt=1, backend="opencode", branch="feature/1949",
        checkout=tmp_path, started_at=datetime.now(UTC), base_ref="main",
        backend_status="completed", test_command="pytest -q", safe_git=GitObservation(),
        compute=compute,
        work_spec=WorkSpec(
            issue_id=1949, attempt=1, repo_url="repo", base_ref="main",
            branch="feature/1949", prompt="", rules=None, test_command="pytest -q",
            backend="opencode", timeout_s=30, local_checkout=tmp_path,
        ),
    )
    assert result.review_ready is (gate_exit == 0)
    assert result.evidence.tests is not None and result.evidence.tests.exit_code == gate_exit
    assert len(calls) == 2
    assert calls[0] == "verified"
    assert calls[1].local_argv == ("/bin/sh", "-c", "pytest -q")
    if gate_exit:
        assert result.status == result.evidence.status == "failed"
        assert "tests_failed" in result.reasons
        assert "test_worker_gate" in result.evidence.failure_reason


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_exit", [0, 1])
async def test_factory_completion_records_observed_gate_evidence(
    tmp_path: Path, gate_exit: int,
) -> None:
    checkout = tmp_path / "factory-sandbox"
    checkout.mkdir()
    calls: list[tuple[str, Path]] = []

    class GitObservation:
        def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, "changed.py\n" if "--name-only" in args else "")

    def runner(command: str, *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        assert timeout == 30
        return subprocess.CompletedProcess(command, gate_exit, "gate output", "")

    spec = WorkSpec(
        issue_id=1949, attempt=1, repo_url="repo", base_ref="main",
        branch="feature/1949", prompt="", rules=None, test_command="check-gate",
        backend="feature_factory", timeout_s=30, local_checkout=checkout,
    )
    arguments = dict(
        issue=1949, attempt=1, backend="feature_factory", branch="feature/1949",
        checkout=checkout, started_at=datetime.now(UTC), base_ref="main",
        backend_status="completed", test_command="check-gate", safe_git=GitObservation(),
        work_spec=spec, runner=runner,
    )
    provider = await observe_evidence(**arguments)
    assert calls == [("check-gate", checkout)]
    assert provider.evidence.files_changed == ["changed.py"]
    assert provider.evidence.tests.exit_code == gate_exit
    assert provider.evidence.tests.observed
    assert provider.review_ready is (gate_exit == 0)
