from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import pytest
import asyncio

from mimir.event_logger import _reset_logger_for_tests, init_logger
from mimir.forge.github import GitHubIdentityVerificationError
from mimir.worklink.backends import (
    Caps,
    ComputeCaps,
    ComputeResult,
    RawResult,
    WorkOrder,
)
from mimir.worklink.evidence import EvidenceValidation, TestResult, WorklinkEvidence
from mimir.worklink.backends.registry import BackendRegistry, WorklinkConfig, WorklinkDefaults
from mimir.worklink.claims import ChainlinkClaims, ClaimRecord, ClaimResult, claim_records_from_comments
from mimir.worklink.compute import LaunchHandle, WorkSpec
from mimir.worklink.checkout import CheckoutLease
from mimir.worklink.backends.feature_factory import FeatureFactoryBackend, parse_factory_status
from mimir.worklink.factory_state import FactoryRunRecord, load_factory_record, save_factory_record
from mimir.worklink.run_state import load_run_state
from mimir.worklink.orchestrator import (
    IssueContext,
    LeafValidationError,
    WorklinkError,
    WorklinkRunner,
    _PR_BODY_SECTION_MAX_BYTES,
    _demote_template_invalid_ready_leaf,
    _epic_prompt,
    _epic_run_timeout_s,
    _epic_stale_heartbeat_s,
    _read_checkout_git_identity,
    _read_factory_publishing_identity,
    _read_pr_body_section,
    _resolve_factory_github_credential,
    read_work_item,
    render_decomposition_prompt,
    render_work_item,
    render_work_order,
    run_worklink,
    run_worklink_epic,
    validate_leaf,
)


@pytest.mark.parametrize("value", [None, "invalid", "0", "-1"])
def test_factory_timeout_defaults_and_falls_back_to_twelve_hours(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("MIMIR_FACTORY_RUN_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("MIMIR_FACTORY_RUN_TIMEOUT_S", value)
    assert _epic_run_timeout_s() == 43200.0


@pytest.mark.parametrize("value", [None, "invalid", "0", "-1"])
def test_factory_stale_diagnostic_defaults_and_falls_back_to_fifteen_minutes(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("MIMIR_FACTORY_STALE_HEARTBEAT_S", raising=False)
    else:
        monkeypatch.setenv("MIMIR_FACTORY_STALE_HEARTBEAT_S", value)
    assert _epic_stale_heartbeat_s() == 900.0


def test_run_worklink_epic_records_unhandled_failure_at_sync_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    recorded: list[dict[str, object]] = []

    async def fail(self: WorklinkRunner, issue_id: int, *, autonomous: bool = False):
        raise RuntimeError("factory crashed")

    monkeypatch.setattr(WorklinkRunner, "run_epic", fail)
    monkeypatch.setattr(
        orchestrator,
        "_record_run_failure",
        lambda **fields: recorded.append(fields),
    )

    with pytest.raises(RuntimeError, match="factory crashed"):
        run_worklink_epic(home=tmp_path, repo=tmp_path, issue_id=1395, autonomous=True)

    assert len(recorded) == 1
    assert recorded[0]["home"] == tmp_path
    assert recorded[0]["issue_id"] == 1395
    assert recorded[0]["attempt"] is None
    assert recorded[0]["exit_status"] == 1
    assert recorded[0]["autonomous"] is True
    assert isinstance(recorded[0]["error"], RuntimeError)


class FakeCompute:
    name = "fake_compute"

    def __init__(self, *, shared_filesystem: bool = False) -> None:
        self.shared_filesystem = shared_filesystem
        self.specs: list[WorkSpec] = []
        self.cleaned: list[LaunchHandle] = []

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(self.shared_filesystem, False, True, False)

    async def launch(self, spec: WorkSpec) -> LaunchHandle:
        self.specs.append(spec)
        return LaunchHandle(self.name, f"job-{len(self.specs)}")

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        return ComputeResult(exit_code=0, stdout="ok", stderr="")

    async def logs(self, handle: LaunchHandle) -> str:
        return ""

    async def cancel(self, handle: LaunchHandle) -> None:
        return None

    async def cleanup(self, handle: LaunchHandle) -> None:
        self.cleaned.append(handle)


class SlowTestCompute(FakeCompute):
    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        await asyncio.sleep(0)
        return ComputeResult(exit_code=0, stdout="tests ok", stderr="")


class FakeBackend:
    name = "fake"

    def __init__(
        self,
        status: str = "success",
        *,
        write_change: bool = True,
        pr_body_section: str | None = None,
    ) -> None:
        self.status = status
        self.write_change = write_change
        self.pr_body_section = pr_body_section
        self.orders: list[WorkOrder] = []

    def capabilities(self) -> Caps:
        return Caps("fake", False, False, False, True, None)

    def work_spec(
        self,
        order: WorkOrder,
        *,
        attempt: int,
        repo_url: str,
        base_ref: str,
        branch: str,
        test_command: str,
    ) -> WorkSpec:
        return WorkSpec(
            issue_id=order.issue_id,
            attempt=attempt,
            repo_url=repo_url,
            base_ref=base_ref,
            branch=branch,
            prompt=order.prompt,
            rules=order.rules,
            test_command=test_command,
            backend=self.name,
            timeout_s=order.timeout_s,
            env=order.env,
            local_checkout=order.checkout,
        )

    async def interpret(self, order: WorkOrder, result: object) -> RawResult:
        self.orders.append(order)
        if self.write_change:
            (order.checkout / "changed.txt").write_text("hello\n", encoding="utf-8")
        if self.pr_body_section is not None:
            (order.checkout / ".worklink-pr-body.md").write_text(
                self.pr_body_section, encoding="utf-8"
            )
        return RawResult(
            0 if self.status == "success" else 1,
            order.transcript_root / "fake.json",
            self.status,
            None,
        )





def test_orchestrator_passes_configured_compute_backend_to_tool_backend(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    compute = FakeCompute(shared_filesystem=True)
    calls: list[Sequence[str] | str] = []

    def runner(
        args: Sequence[str] | str, **_: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        checkout_result = _isolated_checkout_result(args, repo, worktree)
        if checkout_result is not None:
            return checkout_result
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "claim"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "release"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "comment"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "close"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "label"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "unlabel"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[3:] == [
            "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRTUXB"
        ]:
            return cp(args, stdout=b"changed.txt\0")
        if isinstance(args, list) and args[3:5] == ["cat-file", "blob"]:
            return cp(args, stdout=b"clean content\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "diff"]:
            if "--cached" in args and "--quiet" in args:
                return cp(args, returncode=1)
            return cp(args, stdout=" changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "status"]:
            return cp(args)
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        if isinstance(args, list) and args[:3] == ["gh", "pr", "create"]:
            return cp(args, stdout="https://github.com/jasoncarreira/mimir/pull/999\n")
        return cp(args)

    class ComputeAwareBackend(FakeBackend):
        def work_spec(
            self,
            order: WorkOrder,
            *,
            attempt: int,
            repo_url: str,
            base_ref: str,
            branch: str,
            test_command: str,
        ) -> WorkSpec:
            return WorkSpec(
                issue_id=order.issue_id,
                attempt=attempt,
                repo_url=repo_url,
                base_ref=base_ref,
                branch=branch,
                prompt=order.prompt,
                rules=order.rules,
                test_command=test_command,
                backend=self.name,
                timeout_s=order.timeout_s,
                env=order.env,
                backend_config={"bin": "fake-tool", "args": []},
                local_checkout=order.checkout,
            )

        async def interpret(self, order: WorkOrder, result: object) -> RawResult:
            self.orders.append(order)
            assert isinstance(result, ComputeResult)
            (order.checkout / "changed.txt").write_text(result.stdout + "\n", encoding="utf-8")
            return RawResult(result.exit_code, order.transcript_root / "fake.json", "success", None)


    backend = ComputeAwareBackend(status="success")
    registry = BackendRegistry(WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_compute")))
    registry.register(backend)
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed", (result.reason, calls)
    assert compute.specs
    assert compute.specs[0].issue_id == 441
    assert compute.specs[0].attempt == 1
    assert compute.specs[0].branch == "issue/441-a1"
    assert compute.specs[0].repo_url == "git@github.com:jasoncarreira/mimir.git"
    assert compute.specs[0].base_ref == "main"
    assert compute.specs[0].test_command == "echo ok"
    assert compute.specs[0].local_checkout == worktree
    assert compute.specs[0].env["MIMIR_HOME"] == str(tmp_path)
    assert compute.cleaned == [LaunchHandle("fake_compute", "job-1")]


def cp(
    args: Sequence[str] | str,
    returncode: int = 0,
    stdout: str | bytes = "",
    stderr: str | bytes = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def _isolated_checkout_result(
    args: Sequence[str] | str, repo: Path, checkout: Path
) -> subprocess.CompletedProcess[str] | None:
    if not isinstance(args, list):
        return None
    if args[:4] == ["git", "clone", "--local", "--quiet"]:
        checkout.mkdir(parents=True, exist_ok=True)
        (checkout / ".git" / "objects" / "info").mkdir(parents=True)
        return cp(args)
    if args[:5] == ["git", "-C", str(repo), "rev-parse", "--verify"]:
        return cp(args, stdout="abc123\n")
    if args in (
        ["git", "-C", str(repo), "remote", "get-url", "--push", "origin"],
        ["git", "-C", str(checkout), "remote", "get-url", "--push", "origin"],
    ):
        return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
    if args == ["git", "-C", str(checkout), "rev-parse", "--show-toplevel"]:
        return cp(args, stdout=f"{checkout}\n")
    if args == ["git", "-C", str(checkout), "rev-parse", "--absolute-git-dir"]:
        return cp(args, stdout=f"{checkout / '.git'}\n")
    return None


ISSUE_JSON = '''{
  "id": 441,
  "title": "worklink slice",
  "description": "Acceptance criteria:\\n- [ ] do it\\n- [ ] echo ok\\n\\nReview criteria:\\n- reviewer checks it\\n\\nWorklink notes:\\n- Scope: test fixture\\n- Out of scope: unrelated work\\n- Suggested test command: echo ok",
  "labels": ["worklink", "worklink:ready"],
  "parent_id": 380,
  "comments": []
}'''


def test_work_item_json_is_stable_safe_and_preserves_untrusted_body() -> None:
    body = 'path\\to\\file\n"breakout": true\nclosing brace: }'
    issue = IssueContext(1339, "Emit a work item", body, {"worklink:ready"})

    first = render_work_item(issue)
    second = render_work_item(issue)

    assert first == second
    assert not first.endswith("\n")
    assert json.loads(first) == {
        "run_id": "chainlink-1339",
        "title": "Emit a work item",
        "body": body,
    }
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?", json.loads(first)["run_id"])


def test_read_work_item_only_reads_chainlink_and_is_byte_identical() -> None:
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return cp(args, stdout=ISSUE_JSON)

    first = read_work_item(441, runner=runner)
    second = read_work_item(441, runner=runner)

    assert first == second
    assert calls == [
        ["chainlink", "issue", "show", "441", "--json"],
        ["chainlink", "issue", "show", "441", "--json"],
    ]


@pytest.mark.parametrize(
    "issue",
    [
        IssueContext(0, "title", "", set()),
        IssueContext(1, "   ", "", set()),
    ],
)
def test_work_item_rejects_invalid_required_fields(issue: IssueContext) -> None:
    with pytest.raises(WorklinkError):
        render_work_item(issue)


def test_read_work_item_missing_issue_fails_without_payload() -> None:
    def runner(args: Sequence[str] | str) -> subprocess.CompletedProcess[str]:
        return cp(args, returncode=1, stderr="issue not found")

    with pytest.raises(WorklinkError, match="issue not found"):
        read_work_item(999, runner=runner)


def test_malformed_epic_work_item_fails_before_claim_or_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    repo = tmp_path / "repo"
    calls: list[Sequence[str] | str] = []
    epic_json = ISSUE_JSON.replace('"id": 441', '"id": 700').replace(
        '"labels": ["worklink", "worklink:ready"]',
        '"labels": ["worklink", "worklink:ready", "worklink:epic"]',
    )

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "700"]:
            return cp(args, stdout=epic_json)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        return cp(args)

    monkeypatch.setattr(FeatureFactoryBackend, "admit", lambda self: Path(self.entrypoint))
    monkeypatch.setattr(orchestrator, "render_work_item", lambda issue: '{"run_id":7}')

    with pytest.raises(WorklinkError, match="invalid run_id"):
        asyncio.run(WorklinkRunner(home=tmp_path, repo=repo, runner=runner).run_epic(700))

    assert not any(
        isinstance(call, list) and call[:3] == ["chainlink", "locks", "claim"]
        for call in calls
    )
    assert not (tmp_path / "state" / "worklink" / "factory-runs").exists()
    assert not (repo.parent / ".worklink").exists()


def test_factory_launch_binding_rejects_argv_controller_disagreement(tmp_path: Path) -> None:
    import mimir.worklink.orchestrator as orchestrator

    spec = WorkSpec(
        issue_id=700,
        attempt=1,
        repo_url="https://github.com/owner/repo.git",
        base_ref="main",
        branch="feature/chainlink-700",
        prompt="",
        rules=None,
        test_command="pytest",
        backend="feature_factory",
        timeout_s=30,
        backend_config={"run_id": "chainlink-700"},
        local_checkout=tmp_path,
        local_argv=("opencode", "run", " --autonomous chainlink-701"),
    )

    with pytest.raises(WorklinkError, match="does not match the supervised run_id"):
        orchestrator._require_factory_launch_binding(spec, "chainlink-700")


def test_preclaim_registry_crash_emits_scrubbed_failure_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    init_logger(events, session_id="test-worklink")

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON)
        return cp(args)

    class CrashingRegistry:
        def __init__(self, _config: WorklinkConfig) -> None:
            raise ValueError("unknown Worklink backend config: codex password=hunter2")

    monkeypatch.setattr(orchestrator, "_runner_for_home", lambda *_: runner)
    monkeypatch.setattr(orchestrator, "BackendRegistry", CrashingRegistry)

    with pytest.raises(ValueError, match="unknown Worklink backend config"):
        run_worklink(home=tmp_path, repo=tmp_path, issue_id=441, autonomous=True)

    records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    failure = next(record for record in records if record["type"] == "worklink_run_failed")
    assert failure["issue_id"] == 441
    assert failure["attempt"] is None
    assert failure["attempt_consumed"] is False
    assert failure["exit_status"] == 1
    assert failure["terminal_error"] == (
        "ValueError: unknown Worklink backend config: codex password=[REDACTED]"
    )
    assert "hunter2" not in events.read_text(encoding="utf-8")
    _reset_logger_for_tests()


def test_preclaim_multiline_git_contention_does_not_persist_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator
    from mimir.worklink.dispatch_failures import load_failure_state

    state_dir = tmp_path / "state" / "pollers" / "worklink-ready-queue"
    ambient_state_dir = tmp_path / "ambient-state"

    async def contended_preclaim(self: WorklinkRunner, issue_id: int, **_: object):
        raise RuntimeError(
            "fatal: Unable to create '/repo/.git/index.lock': File exists.\n"
            "Another git process seems to be running in this repository."
        )

    monkeypatch.setenv("STATE_DIR", str(ambient_state_dir))
    monkeypatch.setattr(WorklinkRunner, "run", contended_preclaim)

    with pytest.raises(RuntimeError, match="Unable to create"):
        run_worklink(home=tmp_path, repo=tmp_path, issue_id=441, autonomous=True)

    assert load_failure_state(state_dir)["issues"] == {}
    assert not ambient_state_dir.exists()


def test_postclaim_failure_emits_same_failure_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator
    from mimir.worklink.dispatch_failures import load_failure_state

    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    state_dir = tmp_path / "state" / "pollers" / "worklink-ready-queue"
    ambient_state_dir = tmp_path / "ambient-state"
    init_logger(events, session_id="test-worklink")

    async def failed_after_claim(self: WorklinkRunner, issue_id: int, **_: object):
        return orchestrator.WorklinkRunResult(
            issue_id, 2, "failed", reason="backend exploded api_key=super-secret"
        )

    monkeypatch.setattr(WorklinkRunner, "run", failed_after_claim)
    monkeypatch.setenv("STATE_DIR", str(ambient_state_dir))
    result = run_worklink(home=tmp_path, repo=tmp_path, issue_id=441, autonomous=True)

    assert result.status == "failed"
    records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    failure = next(record for record in records if record["type"] == "worklink_run_failed")
    assert failure["attempt"] == 2
    assert failure["attempt_consumed"] is True
    assert failure["terminal_error"] == "backend exploded api_key=[REDACTED]"
    entry = load_failure_state(state_dir)["issues"]["441"]
    assert entry["attempt"] == 2
    assert entry["terminal_error"] == "backend exploded api_key=[REDACTED]"
    assert not ambient_state_dir.exists()
    _reset_logger_for_tests()


def test_non_autonomous_failure_does_not_persist_dispatch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    async def failed_after_claim(self: WorklinkRunner, issue_id: int, **_: object):
        return orchestrator.WorklinkRunResult(issue_id, 2, "failed", reason="backend exploded")

    monkeypatch.setattr(WorklinkRunner, "run", failed_after_claim)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "ambient-state"))

    result = run_worklink(home=tmp_path, repo=tmp_path, issue_id=441, autonomous=False)

    assert result.status == "failed"
    assert not (tmp_path / "state" / "pollers" / "worklink-ready-queue").exists()
    assert not (tmp_path / "ambient-state").exists()


def test_manual_success_clears_autonomous_failure_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator
    from mimir.worklink.dispatch_failures import load_failure_state, record_failure

    state_dir = tmp_path / "state" / "pollers" / "worklink-ready-queue"
    record_failure(
        state_dir,
        issue_id=441,
        attempt=None,
        exit_status=1,
        error="ValueError: persistent config failure",
        log_path=None,
    )

    async def successful_manual_run(self: WorklinkRunner, issue_id: int, **_: object):
        return orchestrator.WorklinkRunResult(issue_id, 2, "completed")

    monkeypatch.setenv("STATE_DIR", str(tmp_path / "ambient-state"))
    monkeypatch.setattr(WorklinkRunner, "run", successful_manual_run)

    result = run_worklink(home=tmp_path, repo=tmp_path, issue_id=441, autonomous=False)

    assert result.status == "completed"
    assert load_failure_state(state_dir)["issues"]["441"]["active"] is False
    assert not (tmp_path / "ambient-state").exists()


def test_validate_leaf_refuses_missing_planner_template() -> None:
    issue = IssueContext(1, "vague", "please do thing", set())

    with pytest.raises(LeafValidationError, match="Acceptance criteria"):
        validate_leaf(issue)


def test_validate_leaf_checks_epic_target_without_requiring_leaf_template() -> None:
    validate_leaf(IssueContext(1, "epic", "build the thing", {"worklink:epic"}))

    issue = IssueContext(
        1,
        "epic",
        "Worklink notes:\n- Target branch:\n- Target branch: feature/acp",
        {"worklink:epic"},
    )
    with pytest.raises(LeafValidationError, match="multiple Target branch bullets"):
        validate_leaf(issue)


def test_dry_run_prints_rendered_work_order_without_mutations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(tmp_path / "repo"), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        return cp(args)

    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    result = asyncio.run(
        WorklinkRunner(
            home=tmp_path, repo=tmp_path / "repo", runner=runner, registry=registry
        ).run(441, backend_name="fake", dry_run=True)
    )

    out = capsys.readouterr().out
    assert result.dry_run is True
    assert "worklink slice" in out
    assert "Acceptance criteria" in out
    # The work order teaches backends how to signal a design-level block.
    assert "WORKLINK_BLOCKED:" in out
    assert not any(isinstance(call, list) and call[:2] == ["chainlink", "locks"] for call in calls)
    assert backend.orders == []




def _orchestrator_runner(
    repo: Path,
    worktree: Path,
    *,
    files_stdout: str = "changed.txt\n",
    dirty_after_commit: bool = False,
    cleanup_returncode: int = 0,
    release_returncode: int = 0,
    issue_json: str = ISSUE_JSON,
    fetch_failure_base: str | None = None,
):
    calls: list[Sequence[str] | str] = []
    commit_seen = False

    def runner(
        args: Sequence[str] | str,
        *,
        cwd: Path | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        nonlocal commit_seen
        calls.append(args)
        checkout_result = _isolated_checkout_result(args, repo, worktree)
        if checkout_result is not None:
            return checkout_result
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=issue_json)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "claim"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "release"]:
            return cp(
                args,
                returncode=release_returncode,
                stderr="release denied\n" if release_returncode else "",
            )
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "comment"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "close"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "label"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "unlabel"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if (
            fetch_failure_base is not None
            and isinstance(args, list)
            and args == ["git", "-C", str(repo), "fetch", "origin", fetch_failure_base]
        ):
            return cp(args, returncode=128, stderr=f"fatal: couldn't find remote ref {fetch_failure_base}\n")
        if isinstance(args, list) and args[3:] == [
            "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRTUXB"
        ]:
            paths = files_stdout.rstrip("\n").encode() + (b"\0" if files_stdout else b"")
            return cp(args, stdout=paths)
        if isinstance(args, list) and args[3:5] == ["cat-file", "blob"]:
            return cp(args, stdout=b"clean content\n")
        if (
            isinstance(args, list)
            and args[:4] == ["git", "-C", str(worktree), "diff"]
            and "--name-only" in args
        ):
            return cp(args, stdout=files_stdout)
        if (
            isinstance(args, list)
            and args[:4] == ["git", "-C", str(worktree), "diff"]
            and "--stat" in args
        ):
            return cp(args, stdout=" changed.txt | 1 +\n" if files_stdout else "")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "status"]:
            if commit_seen:
                return cp(args, stdout="?? generated.log\n" if dirty_after_commit else "")
            return cp(args, stdout="?? changed.txt\n" if files_stdout else "")
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "add"]:
            return cp(args)
        # `--quiet` uses the exit code to signal staged changes.
        if isinstance(args, list) and args[:6] == ["git", "-C", str(worktree), "diff", "--cached", "--quiet"]:
            return cp(args, returncode=1 if files_stdout else 0)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "commit"]:
            commit_seen = True
            return cp(args, stdout="[issue/441-a1 abc123] worklink\n")
        # #518: the attempt branch is pushed from the checkout that owns it
        # (lease.path == worktree here), not the parent repo.
        if isinstance(args, list) and args[:3] == ["git", "-C", str(worktree)] and args[3] == "push":
            return cp(args)
        if isinstance(args, list) and args[:3] == ["gh", "pr", "create"]:
            return cp(args, stdout="https://github.com/jasoncarreira/mimir/pull/999\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "remove"]:
            return cp(
                args,
                returncode=cleanup_returncode,
                stderr="worktree cleanup failed\n" if cleanup_returncode else "",
            )
        return cp(args)

    return calls, runner


def test_worklink_rereads_issue_comments_before_claiming(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-2"
    calls: list[Sequence[str] | str] = []

    issue_with_prior_claim = ISSUE_JSON.replace(
        '"comments": []',
        '"comments": [{"content": "WORKLINK_CLAIM {\\"agent_id\\": \\"mimir-worklink\\", \\"attempt\\": 1, \\"claimed_at\\": \\"2026-06-12T12:04:29+00:00\\", \\"heartbeat_at\\": null, \\"issue_id\\": 441}"}]',
    )

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        checkout_result = _isolated_checkout_result(args, repo, worktree)
        if checkout_result is not None:
            return checkout_result
        show_count = sum(
            1
            for call in calls
            if isinstance(call, list) and call[:4] == ["chainlink", "issue", "show", "441"]
        )
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON if show_count == 1 else issue_with_prior_claim)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "claim"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "release"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "comment"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "close"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "label"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "unlabel"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "diff"]:
            return cp(args, stdout=" changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "status"]:
            return cp(args)
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        return cp(args)

    backend = FakeBackend(status="success", write_change=False)
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.attempt == 2
    assert result.branch == "issue/441-a2"
    assert ["git", "clone", "--local", "--quiet", str(repo), str(worktree)] in calls
    assert ["git", "-C", str(worktree), "checkout", "-B", "issue/441-a2", "abc123"] in calls


def test_worklink_runner_happy_path_fake_backend(tmp_path: Path) -> None:
    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    init_logger(events, session_id="test-worklink")
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree)

    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    assert result.review_ready is True
    assert result.pr_url == "https://github.com/jasoncarreira/mimir/pull/999"
    assert (tmp_path / "state" / "worklink" / "evidence" / "441-1.json").is_file()
    evidence = json.loads(
        (tmp_path / "state" / "worklink" / "evidence" / "441-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["base_ref"] == "main"
    assert ["git", "-C", str(worktree), "commit", "-m", "worklink: issue #441"] in calls
    assert ["chainlink", "locks", "release", "441"] in calls
    # #518: the attempt branch is pushed from the checkout that owns it (lease.path),
    # never from the parent repo — the isolated-checkout shape has the branch only
    # inside lease.path, so a parent-repo push fails "src refspec ... does not match".
    assert ["git", "-C", str(worktree), "push", "-u", "origin", "issue/441-a1"] in calls
    assert not any(
        isinstance(c, list) and c[:3] == ["git", "-C", str(repo)] and len(c) > 3 and c[3] == "push"
        for c in calls
    )
    # Default base: checkout cut from main, PR targets main explicitly.
    assert ["git", "-C", str(repo), "fetch", "origin", "main"] in calls
    assert ["git", "clone", "--local", "--quiet", str(repo), str(worktree)] in calls
    assert ["git", "-C", str(worktree), "checkout", "-B", "issue/441-a1", "abc123"] in calls
    pr_calls = [c for c in calls if isinstance(c, list) and c[:3] == ["gh", "pr", "create"]]
    assert pr_calls and pr_calls[0][pr_calls[0].index("--base") + 1] == "main"
    body = events.read_text(encoding="utf-8")
    assert "worklink_claimed" in body
    assert "worklink_evidence" in body
    assert "worklink_transition" in body
    _reset_logger_for_tests()


def test_post_pr_comment_failure_does_not_demote_completed_run(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, base_runner = _orchestrator_runner(repo, worktree)
    pr_opened = False

    def runner(
        args: Sequence[str] | str,
        *,
        cwd: Path | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        nonlocal pr_opened
        if isinstance(args, list) and args[:3] == ["gh", "pr", "create"]:
            pr_opened = True
        if (
            pr_opened
            and isinstance(args, list)
            and args[:3] == ["chainlink", "issue", "comment"]
            and args[-1].startswith("WORKLINK_EVIDENCE ")
        ):
            calls.append(args)
            return cp(args, returncode=1, stderr="temporary Chainlink failure")
        return base_runner(args, cwd=cwd, text=text)

    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    assert result.review_ready is True
    assert result.pr_url == "https://github.com/jasoncarreira/mimir/pull/999"
    assert result.reason == (
        "post-publication bookkeeping failed: evidence comment: temporary Chainlink failure"
    )
    assert ["chainlink", "issue", "label", "441", "worklink:review"] in calls
    assert ["chainlink", "issue", "label", "441", "worklink:failed"] not in calls
    assert ["chainlink", "issue", "label", "441", "worklink:ready"] not in calls
    evidence = json.loads(
        (tmp_path / "state" / "worklink" / "evidence" / "441-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["status"] == "completed"
    assert evidence["pr_url"] == result.pr_url


def test_report_cleanup_failure_cannot_skip_lock_release_or_state_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree)
    report_dir = tmp_path / "executor-report"
    report_dir.mkdir()
    monkeypatch.setattr(
        "mimir.worklink.orchestrator._make_executor_report_dir",
        lambda issue_id, attempt: report_dir,
    )

    def deny_report_removal(path: Path) -> None:
        if path == report_dir:
            raise PermissionError("executor-owned report is not removable")

    monkeypatch.setattr(
        "mimir.worklink.orchestrator.rmtree_missing_ok",
        deny_report_removal,
    )
    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    assert ["chainlink", "locks", "release", "441"] in calls
    assert load_run_state(tmp_path, 441) is None


def test_failed_lock_release_is_logged_and_retains_run_state(tmp_path: Path) -> None:
    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    init_logger(events, session_id="test-worklink")
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    _, runner = _orchestrator_runner(repo, worktree, release_returncode=1)
    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    assert load_run_state(tmp_path, 441) is not None
    records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    failure = next(
        record
        for record in records
        if record["type"] == "worklink_cleanup_failed"
        and record["cleanup"] == "lock_release"
    )
    assert failure["issue_id"] == 441
    assert failure["attempt"] == 1
    assert failure["error"] == "Chainlink did not confirm lock release"
    _reset_logger_for_tests()


def test_worklink_pr_body_includes_build_section_and_intact_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree)
    section = (
        "Mechanism: `.worklink-pr-body.md`, a designated checkout file. "
        "It keeps PR publication under harness control while letting the build report findings."
    )
    backend = FakeBackend(pr_body_section=section)
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    pr_call = next(
        call for call in calls if isinstance(call, list) and call[:3] == ["gh", "pr", "create"]
    )
    body = pr_call[pr_call.index("--body") + 1]
    assert f"Build summary:\n\n{section}\n\n" in body
    assert body.endswith(
        "Worklink evidence:\n"
        "- Base: `main`\n"
        "- Branch: `issue/441-a1`\n"
        "- Files changed: 1\n"
        "- Tests: `echo ok` → 0\n"
        f"- Transcript: `{tmp_path / 'state' / 'worklink' / 'transcripts' / 'fake.json'}`\n"
    )
    assert not (worktree / ".worklink-pr-body.md").exists()


def test_worklink_pr_body_without_build_section_is_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree)
    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    pr_call = next(
        call for call in calls if isinstance(call, list) and call[:3] == ["gh", "pr", "create"]
    )
    assert pr_call[pr_call.index("--body") + 1] == (
        "Closes chainlink #441.\n\n"
        "Worklink evidence:\n"
        "- Base: `main`\n"
        "- Branch: `issue/441-a1`\n"
        "- Files changed: 1\n"
        "- Tests: `echo ok` → 0\n"
        f"- Transcript: `{tmp_path / 'state' / 'worklink' / 'transcripts' / 'fake.json'}`\n"
    )


def test_chainlink_rendering_rewrites_every_github_closing_keyword(
    tmp_path: Path,
) -> None:
    # GitHub's supported set is documented at:
    # https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
    keywords = (
        "close",
        "closes",
        "closed",
        "fix",
        "fixes",
        "fixed",
        "resolve",
        "resolves",
        "resolved",
    )
    description = "\n".join(
        f"{keyword.upper()}: #{number}"
        for number, keyword in enumerate(keywords, 1)
    )
    description += "\nFixes octo-org/octo-repo#100"
    issue = IssueContext(441, "render safely", description, {"worklink:ready"})
    template = tmp_path / "order.md"
    template.write_text("{description}", encoding="utf-8")

    rendered = (
        render_work_order(
            issue,
            template_path=template,
            backend_name="fake",
            test_command="echo ok",
        ),
        _epic_prompt(issue),
    )
    actionable = re.compile(
        r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
        r"(?:\s*:\s*|\s+)#[0-9]+\b"
    )
    for text in rendered:
        assert actionable.search(text) is None
        for number, keyword in enumerate(keywords, 1):
            assert f"{keyword.upper()}: chainlink #{number}" in text
        assert "Fixes octo-org/octo-repo#100" in text


def test_pr_build_summary_rewrites_ambiguous_closing_reference(tmp_path: Path) -> None:
    section_path = tmp_path / ".worklink-pr-body.md"
    section_path.write_text(
        "Closes #1327\nFixes octo-org/octo-repo#100",
        encoding="utf-8",
    )

    section = _read_pr_body_section(tmp_path)

    assert section == "Closes chainlink #1327\nFixes octo-org/octo-repo#100"


def test_pr_body_section_is_scrubbed_and_visibly_truncated(tmp_path: Path) -> None:
    section_path = tmp_path / ".worklink-pr-body.md"
    section_path.write_text(
        "Token: ghp_supersecret\nWorklink evidence:\nunsafe\x00text\n"
        + "x" * _PR_BODY_SECTION_MAX_BYTES,
        encoding="utf-8",
    )

    section = _read_pr_body_section(tmp_path)

    assert section is not None
    assert "ghp_supersecret" not in section
    assert "[REDACTED]" in section
    assert "\x00" not in section
    assert "\nWorklink evidence:\n" not in section
    assert section.endswith("[Build summary truncated by Worklink.]")
    assert len(section.encode("utf-8")) <= _PR_BODY_SECTION_MAX_BYTES
    assert not section_path.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_pr_body_section_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    section_path = tmp_path / ".worklink-pr-body.md"
    os.mkfifo(section_path)

    assert _read_pr_body_section(tmp_path) is None
    assert not section_path.exists()


def test_pr_body_section_rejects_directory_without_raising(tmp_path: Path) -> None:
    section_path = tmp_path / ".worklink-pr-body.md"
    section_path.mkdir()

    assert _read_pr_body_section(tmp_path) is None
    assert section_path.is_dir()


def test_backend_failure_with_zero_exit_still_names_its_reason(tmp_path: Path) -> None:
    """A backend-reported failure carries its reason even when the process exits 0.

    Chainlink #1152: ``failure_reason`` keyed off ``raw.exit_code != 0`` alone, so
    a backend that judged the run failed while the executor exited cleanly
    produced status=failed with reason=null — and validate_evidence had to
    synthesize "reported failure without a reason" (#1108/#1349). The status and
    the reason were reading different inputs and disagreeing.
    """
    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    init_logger(events, session_id="test-worklink-zero-exit-failure")
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    _calls, runner = _orchestrator_runner(repo, worktree, files_stdout="")
    backend_reason = "backend judged the run failed while the executor exited cleanly"

    class QuietFailureBackend(FakeBackend):
        async def interpret(self, order: WorkOrder, result: object) -> RawResult:
            return RawResult(
                0,
                order.transcript_root / "opencode-quiet.json",
                "failed",
                backend_reason,
            )

    registry = BackendRegistry(WorklinkConfig())
    registry.register(QuietFailureBackend(write_change=False))

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "failed"
    evidence = json.loads(
        (tmp_path / "state" / "worklink" / "evidence" / "441-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["failure_reason"] == backend_reason
    assert "without a reason" not in (evidence["failure_reason"] or "")
    # The exit code still decides the test-gate message; only the reason changed.
    assert evidence["tests"]["skipped_reason"] != (
        "executor exited nonzero before the test gate"
    )


def test_zero_exit_executor_and_failed_gate_record_structured_reason_and_divergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #1330: the executor passes, but Worklink's gate fails."""
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, base_runner = _orchestrator_runner(repo, worktree)
    executor_report_dir = tmp_path / "executor-report"

    def make_executor_report_dir(issue: int, attempt: int) -> Path:
        executor_report_dir.mkdir()
        return executor_report_dir

    monkeypatch.setattr(
        "mimir.worklink.orchestrator._make_executor_report_dir",
        make_executor_report_dir,
    )

    def write_report(report_dir: Path, *, total: int, failed: tuple[str, ...]) -> None:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "junit.xml").write_text(
            '<testsuites><testsuite tests="{}" failures="{}" errors="0" skipped="0" />'
            "</testsuites>".format(total, len(failed)),
            encoding="utf-8",
        )
        cache = report_dir / "cache" / "v" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "lastfailed").write_text(
            json.dumps({node_id: True for node_id in failed}),
            encoding="utf-8",
        )

    class PassingExecutorBackend(FakeBackend):
        async def interpret(self, order: WorkOrder, result: object) -> RawResult:
            (order.checkout / "changed.txt").write_text("hello\n", encoding="utf-8")
            write_report(
                executor_report_dir,
                total=9906,
                failed=(),
            )
            return RawResult(0, order.transcript_root / "fake.json", "success", None)

    failed_nodes = (
        "tests/test_alpha.py::test_environment_boundary",
        "tests/test_beta.py::test_gate_uses_clean_state",
    )

    def runner(
        args: Sequence[str] | str,
        *,
        cwd: Path | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        if isinstance(args, str) and args.endswith("pytest -q"):
            assignment = shlex.split(args)[0]
            addopts = shlex.split(assignment.partition("=")[2])
            junit = Path(
                next(
                    option.split("=", 1)[1]
                    for option in addopts
                    if option.startswith("--junitxml=")
                )
            )
            cache_dir = Path(
                next(
                    option.split("=", 1)[1]
                    for option in addopts
                    if option.startswith("cache_dir=")
                )
            )
            write_report(junit.parent, total=9908, failed=failed_nodes)
            assert cache_dir == junit.parent / "cache"
            return cp(args, returncode=1, stdout="gate failed without prose identifiers\n")
        return base_runner(args, cwd=cwd, text=text)

    registry = BackendRegistry(WorklinkConfig())
    registry.register(PassingExecutorBackend())

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="pytest -q"
        )
    )

    assert result.status == "failed"
    evidence = json.loads(
        (tmp_path / "state" / "worklink" / "evidence" / "441-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["tests"]["failed_tests"] == list(failed_nodes)
    assert evidence["tests"]["counts"] == {
        "errors": 0,
        "failed": 2,
        "passed": 9906,
        "skipped": 0,
        "total": 9908,
    }
    assert evidence["executor_tests"]["counts"]["passed"] == 9906
    assert evidence["gate_result_diverged"] is True
    assert all(node_id in evidence["failure_reason"] for node_id in failed_nodes)
    assert "2 failed" in evidence["failure_reason"]
    assert "gate failed without prose identifiers" not in evidence["failure_reason"]


def test_executor_crash_publishes_only_scrubbed_bounded_failure_reason(tmp_path: Path) -> None:
    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    init_logger(events, session_id="test-worklink-crash")
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree, files_stdout="")
    unsafe_reason = "ignored earlier line\nprovider token=top-secret " + ("x" * 1200)

    class CrashBackend(FakeBackend):
        def work_spec(self, *args: Any, **kwargs: Any) -> WorkSpec:
            spec = super().work_spec(*args, **kwargs)
            return replace(spec, backend_config={"model": "openai/gpt-5.6-sol"})

        async def interpret(self, order: WorkOrder, result: object) -> RawResult:
            return RawResult(
                1,
                order.transcript_root / "opencode-crash.json",
                "failed",
                unsafe_reason,
            )

    registry = BackendRegistry(WorklinkConfig())
    registry.register(CrashBackend(write_change=False))

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "failed"
    assert result.reason is not None
    assert result.reason.startswith("provider token=[REDACTED] ")
    assert len(result.reason) == 1000
    assert "top-secret" not in result.reason
    assert "echo ok" not in calls
    evidence = json.loads(
        (tmp_path / "state" / "worklink" / "evidence" / "441-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["model"] == "openai/gpt-5.6-sol"
    assert evidence["failure_reason"] == result.reason
    assert evidence["tests"]["skipped_reason"] == "executor exited nonzero before the test gate"
    records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    evidence_event = next(record for record in records if record["type"] == "worklink_evidence")
    assert evidence_event["model"] == "openai/gpt-5.6-sol"
    assert evidence_event["failure_reason"] == result.reason
    published_comments = [
        call[-1]
        for call in calls
        if isinstance(call, list) and call[:3] == ["chainlink", "issue", "comment"]
    ]
    assert any(result.reason in comment for comment in published_comments)
    assert "top-secret" not in json.dumps(evidence)
    assert "top-secret" not in json.dumps(records)
    assert "top-secret" not in "\n".join(published_comments)
    _reset_logger_for_tests()


def test_worklink_runner_retries_transient_claim_contention(tmp_path: Path) -> None:
    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    init_logger(events, session_id="test-worklink-contention")
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    _, base_runner = _orchestrator_runner(repo, worktree)
    claim_calls = 0

    def runner(
        args: Sequence[str] | str,
        *,
        cwd: Path | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        nonlocal claim_calls
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "claim"]:
            claim_calls += 1
            if claim_calls == 1:
                return cp(
                    args,
                    returncode=128,
                    stderr=(
                        "fatal: Unable to create '/mimir-home/.git/worktrees/"
                        "-locks-cache/index.lock': File exists."
                    ),
                )
        return base_runner(args, cwd=cwd, text=text)

    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    assert result.attempt == 1
    assert claim_calls == 2
    records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    contention = [record for record in records if record["type"] == "worklink_claim_contention"]
    assert [record["outcome"] for record in contention] == ["retrying", "succeeded"]
    assert all(record["resource"] == "chainlink_locks_worktree" for record in contention)
    _reset_logger_for_tests()


def test_isolated_checkout_cleanup_does_not_use_git_worktree_remove(
    tmp_path: Path,
) -> None:
    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    init_logger(events, session_id="test-worklink")
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree, cleanup_returncode=128)

    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    assert result.review_ready is True
    assert result.pr_url == "https://github.com/jasoncarreira/mimir/pull/999"
    assert result.reason is None
    assert ["chainlink", "issue", "label", "441", "worklink:review"] in calls
    assert ["chainlink", "issue", "label", "441", "worklink:failed"] not in calls
    assert ["chainlink", "issue", "label", "441", "worklink:ready"] not in calls
    assert not any(
        isinstance(call, list) and "worktree" in call and "remove" in call for call in calls
    )
    _reset_logger_for_tests()



def test_worklink_runner_cuts_worktree_and_pr_from_configured_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    # worklink.yaml in the home points Worklink at a long-running feature branch.
    (tmp_path / "worklink.yaml").write_text(
        "defaults:\n  base_branch: integration/worklink\n"
    )
    calls, runner = _orchestrator_runner(repo, worktree)
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    # Checkout is cut from the configured base, not main.
    assert ["git", "-C", str(repo), "fetch", "origin", "integration/worklink"] in calls
    assert ["git", "clone", "--local", "--quiet", str(repo), str(worktree)] in calls
    assert ["git", "-C", str(worktree), "checkout", "-B", "issue/441-a1", "abc123"] in calls
    # And the PR targets that base (the feature-branch / stacking model).
    pr_calls = [c for c in calls if isinstance(c, list) and c[:3] == ["gh", "pr", "create"]]
    assert pr_calls
    assert pr_calls[0][pr_calls[0].index("--base") + 1] == "integration/worklink"


def test_leaf_target_branch_selects_checkout_pr_work_spec_and_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    issue_json = ISSUE_JSON.replace(
        "- Suggested test command: echo ok",
        "- Target branch: feature/acp\\n- Suggested test command: echo ok",
    )
    calls, runner = _orchestrator_runner(repo, worktree, issue_json=issue_json)
    compute = FakeCompute(shared_filesystem=True)
    backend = FakeBackend()
    registry = BackendRegistry(
        WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_compute"))
    )
    registry.register(backend)
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    assert ["git", "-C", str(repo), "fetch", "origin", "feature/acp"] in calls
    assert compute.specs[0].base_ref == "feature/acp"
    pr_call = next(call for call in calls if isinstance(call, list) and call[:3] == ["gh", "pr", "create"])
    assert pr_call[pr_call.index("--base") + 1] == "feature/acp"
    evidence = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert evidence["base_ref"] == "feature/acp"


def test_unknown_leaf_target_branch_fails_without_falling_back_to_main(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    issue_json = ISSUE_JSON.replace(
        "- Suggested test command: echo ok",
        "- Target branch: feature/does-not-exist\\n- Suggested test command: echo ok",
    )
    calls, runner = _orchestrator_runner(
        repo,
        worktree,
        issue_json=issue_json,
        fetch_failure_base="feature/does-not-exist",
    )
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "failed"
    assert result.reason == "base repo fetch failed for origin/feature/does-not-exist"
    assert backend.orders == []
    assert ["git", "-C", str(repo), "fetch", "origin", "main"] not in calls


def test_worklink_runner_uses_repository_base_over_deployment_default(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    (tmp_path / "worklink.yaml").write_text(
        "defaults:\n  base_branch: deployment-default\nrepository: jasoncarreira/mimir\n",
        encoding="utf-8",
    )
    (tmp_path / "repositories.yaml").write_text(
        f"""
repositories:
  - slug: jasoncarreira/mimir
    root: {repo}
    mode: rw
    origin: https://github.com/jasoncarreira/mimir.git
    base_branch: repository-base
    test_command: echo repository-tests
""".strip(),
        encoding="utf-8",
    )
    calls, runner = _orchestrator_runner(repo, worktree)
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake"
        )
    )

    assert result.status == "completed"
    assert ["git", "-C", str(repo), "fetch", "origin", "repository-base"] in calls
    pr_calls = [call for call in calls if isinstance(call, list) and call[:3] == ["gh", "pr", "create"]]
    assert pr_calls[0][pr_calls[0].index("--base") + 1] == "repository-base"
    assert "echo repository-tests" in backend.orders[0].prompt


def test_worklink_run_base_override_beats_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    # Config says one base; the per-run override must win for both worktree + PR.
    (tmp_path / "worklink.yaml").write_text("defaults:\n  base_branch: develop\n")
    calls, runner = _orchestrator_runner(repo, worktree)
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok", base_branch="release/2.0"
        )
    )

    assert result.status == "completed"
    assert ["git", "-C", str(repo), "fetch", "origin", "release/2.0"] in calls
    assert ["git", "clone", "--local", "--quiet", str(repo), str(worktree)] in calls
    assert ["git", "-C", str(worktree), "checkout", "-B", "issue/441-a1", "abc123"] in calls
    assert not any(
        isinstance(c, list) and c[:5] == ["git", "-C", str(repo), "worktree", "add"] and c[-1] == "develop"
        for c in calls
    )
    pr_calls = [c for c in calls if isinstance(c, list) and c[:3] == ["gh", "pr", "create"]]
    assert pr_calls and pr_calls[0][pr_calls[0].index("--base") + 1] == "release/2.0"


def test_worklink_disabled_base_fetch_fails_before_backend_dispatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    (tmp_path / "worklink.yaml").write_text("defaults:\n  base_fetch: false\n")
    calls, runner = _orchestrator_runner(repo, worktree)
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "failed"
    assert result.reason == "base repo fetch is disabled; refusing to build on an unverified base"
    assert backend.orders == []
    assert not any(
        isinstance(c, list) and c[:4] == ["git", "-C", str(repo), "fetch"] for c in calls
    )
    assert not any(
        isinstance(c, list) and c[:5] == ["git", "-C", str(repo), "worktree", "add"]
        for c in calls
    )




def test_backend_blocked_result_routes_leaf_to_blocked_with_reason(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree)

    class BlockingBackend(FakeBackend):
        async def interpret(self, order: WorkOrder, result: object) -> RawResult:
            self.orders.append(order)
            return RawResult(
                1,
                order.transcript_root / "fake.json",
                "blocked",
                "planner gave contradictory acceptance criteria",
                "planner gave contradictory acceptance criteria",
            )

    backend = BlockingBackend(write_change=False)
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "blocked"
    assert result.review_ready is False
    assert ["chainlink", "issue", "label", "441", "worklink:blocked"] in calls
    assert [
        "chainlink",
        "issue",
        "comment",
        "441",
        "WORKLINK_BLOCKED planner gave contradictory acceptance criteria",
    ] in calls
    assert not any(isinstance(call, list) and call[:3] == ["gh", "pr", "create"] for call in calls)
    evidence = (tmp_path / "state" / "worklink" / "evidence" / "441-1.json").read_text(
        encoding="utf-8"
    )
    assert '"status": "blocked"' in evidence
    assert "planner gave contradictory acceptance criteria" in evidence



def test_worklink_runner_backend_nonzero_transitions_failed_without_pr(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree)
    backend = FakeBackend(status="backend_error")
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "failed"
    assert result.review_ready is False
    assert not any(
        isinstance(call, list) and call[:3] == ["gh", "pr", "create"]
        for call in calls
    )
    assert ["chainlink", "issue", "label", "441", "worklink:ready"] in calls
    assert ["chainlink", "locks", "release", "441"] in calls


def test_worklink_runner_timeout_transitions_failed_without_pr(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree)
    backend = FakeBackend(status="timeout")
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "failed"
    assert not any(
        isinstance(call, list) and call[:3] == ["gh", "pr", "create"]
        for call in calls
    )
    assert ["chainlink", "issue", "label", "441", "worklink:ready"] in calls


def test_worklink_runner_dirty_after_commit_fails_before_push(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo.parent / ".worklink" / repo.name / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree, dirty_after_commit=True)
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "failed"
    assert result.reason is None
    assert not any(
        isinstance(call, list)
        and call[:3] == ["git", "-C", str(repo)]
        and call[3] == "push"
        for call in calls
    )
    assert ["chainlink", "issue", "label", "441", "worklink:ready"] in calls

STRICT_ISSUE_JSON = '''{
  "id": 443,
  "title": "strict worklink leaf",
  "description": "Acceptance criteria:\\n- [ ] implement it\\n- [ ] uv run pytest -q tests/test_worklink_orchestrator.py\\n\\nReview criteria:\\n- reviewer verifies scope\\n\\nWorklink notes:\\n- Scope: mimir/worklink\\n- Out of scope: docs-only cleanup\\n- Suggested test command: uv run pytest -q tests/test_worklink_orchestrator.py",
  "labels": ["worklink", "worklink:ready"],
  "parent_id": 380,
  "comments": []
}'''


INVALID_STRICT_ISSUE_JSON = (
    '{\n'
    '  "id": 443,\n'
    '  "title": "strict malformed worklink leaf",\n'
    '  "description": "Acceptance criteria:\\nplain bullet without checklist\\n\\nReview criteria:\\n- reviewer verifies scope",\n'
    '  "labels": ["worklink", "worklink:ready"],\n'
    '  "parent_id": 380,\n'
    '  "created_at": "2026-06-18T11:58:52Z",\n'
    '  "comments": []\n'
    '}'
)


def test_worklink_runner_demotes_template_invalid_ready_leaf_before_claim(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "443"]:
            return cp(args, stdout=INVALID_STRICT_ISSUE_JSON)
        if isinstance(args, list) and args[:3] in (
            ["chainlink", "issue", "unlabel"],
            ["chainlink", "issue", "label"],
            ["chainlink", "issue", "comment"],
        ):
            return cp(args)
        raise AssertionError(f"unexpected call after validation failure: {args}")

    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())

    with pytest.raises(LeafValidationError, match="acceptance checklist item"):
        asyncio.run(
            WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
                443, backend_name="fake", test_command="echo ok"
            )
        )

    assert ["chainlink", "issue", "unlabel", "443", "worklink:ready"] in calls
    assert ["chainlink", "issue", "label", "443", "worklink:blocked"] in calls
    comments = [
        call
        for call in calls
        if isinstance(call, list) and call[:4] == ["chainlink", "issue", "comment", "443"]
    ]
    assert comments and "acceptance checklist item" in comments[0][4]
    # The invalid leaf is removed from the ready queue before any worker claim,
    # so the poller cannot redispatch this same lowest-id leaf forever.
    assert not any(
        isinstance(call, list) and call[:3] == ["chainlink", "locks", "claim"]
        for call in calls
    )


def test_worklink_runner_does_not_demote_epic_brief_for_leaf_template(tmp_path: Path) -> None:
    issue = IssueContext(
        774,
        "epic brief",
        "Build integrated epic mode as the default routing path.",
        {"worklink:ready", "worklink:epic"},
        created_at=datetime(2026, 6, 18, tzinfo=UTC),
    )
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return cp(args)

    validate_leaf(issue)
    _demote_template_invalid_ready_leaf(
        issue,
        reason="issue missing planner template: Acceptance criteria",
        runner=runner,
        chainlink_bin="chainlink",
    )

    assert calls == []


def test_worklink_runner_dry_run_reports_template_error_without_demoting(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "443"]:
            return cp(args, stdout=INVALID_STRICT_ISSUE_JSON)
        raise AssertionError(f"dry-run must not mutate after validation failure: {args}")

    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())

    with pytest.raises(LeafValidationError, match="acceptance checklist item"):
        asyncio.run(
            WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
                443, backend_name="fake", test_command="echo ok", dry_run=True
            )
        )

    assert calls == [["chainlink", "issue", "show", "443", "--json"]]


def test_validate_leaf_requires_worklink_notes_template_for_new_issues() -> None:
    issue = IssueContext(
        443,
        "new loose leaf",
        "Acceptance criteria:\n- [ ] do it\n\nReview criteria: reviewer checks it",
        {"worklink"},
    )

    with pytest.raises(LeafValidationError, match="Worklink notes"):
        validate_leaf(issue)


def test_validate_leaf_warns_for_legacy_leaves_without_orphaning_them() -> None:
    issue = IssueContext(
        445,
        "legacy queued leaf",
        "Acceptance criteria:\n- [ ] do it\n\nReview criteria: reviewer checks it",
        {"worklink"},
        created_at=datetime(2026, 6, 11, tzinfo=UTC),
    )

    with pytest.warns(RuntimeWarning, match="legacy pre-contract leaf"):
        validate_leaf(issue)


def test_planner_prompt_renders_single_leaf_template_constant() -> None:
    from mimir.prompt_templates import bundled_defaults
    from mimir.worklink.planning import LEAF_TEMPLATE_MARKDOWN

    root = Path(__file__).parent.parent
    prompt_path = root / "mimir" / "prompt_templates" / "decompose.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    rendered = render_decomposition_prompt(
        template_path=prompt_path,
        parent_id=380,
        title="parent",
        labels="worklink",
        priority="normal",
        description="parent body",
    )

    assert "{leaf_template}" in prompt
    assert LEAF_TEMPLATE_MARKDOWN not in prompt
    assert LEAF_TEMPLATE_MARKDOWN in rendered
    assert LEAF_TEMPLATE_MARKDOWN in bundled_defaults()["decompose.md"]
    assert "{leaf_template}" not in bundled_defaults()["decompose.md"]


def test_skill_embeds_single_leaf_template_constant() -> None:
    from mimir.worklink.planning import LEAF_TEMPLATE_MARKDOWN

    root = Path(__file__).parent.parent
    skill = (root / "mimir" / "optional-skills" / "chainlink-orchestrator" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert LEAF_TEMPLATE_MARKDOWN in skill


def test_chainlink_orchestrator_passes_controller_environment_overrides() -> None:
    root = Path(__file__).parent.parent
    manifest = json.loads(
        (
            root
            / "mimir"
            / "optional-skills"
            / "chainlink-orchestrator"
            / "pollers.json"
        ).read_text(encoding="utf-8")
    )

    pass_env = manifest["pollers"][0]["pass_env"]
    assert "MIMIR_FACTORY_PUBLISHING_IDENTITY" in pass_env
    assert "MIMIR_CODING_ENABLED" in pass_env


def test_worklink_ignores_planner_suggested_test_command_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[Sequence[str] | str] = []

    issue_json = STRICT_ISSUE_JSON.replace(
        "- Suggested test command: uv run pytest -q tests/test_worklink_orchestrator.py",
        "- Suggested test command: echo planner-controlled; touch /tmp/owned",
    )

    def runner(args: Sequence[str] | str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "443"]:
            return cp(args, stdout=issue_json)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(tmp_path / "repo"), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        return cp(args)

    (tmp_path / "worklink.yaml").write_text("defaults:\n  test_command: echo safe\n", encoding="utf-8")
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(
            home=tmp_path, repo=tmp_path / "repo", runner=runner, registry=registry
        ).run(443, backend_name="fake", dry_run=True)
    )

    out = capsys.readouterr().out
    assert result.dry_run is True
    assert "echo planner-controlled; touch /tmp/owned" in out
    assert "NOT done until the gate command below passes" in out
    assert "  echo safe" in out


def test_decompose_prompt_teaches_chainlink_block_argument_order() -> None:
    prompt = (Path(__file__).parent.parent / "mimir" / "prompt_templates" / "decompose.md").read_text(
        encoding="utf-8"
    )

    assert "chainlink issue block <ID-that-is-blocked> <BLOCKER>" in prompt
    assert "blocked issue id comes first" in prompt
    assert "chainlink issue block <blocker> <blocked>" not in prompt



def test_worklink_prompt_keeps_planner_suggestion_advisory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[Sequence[str] | str] = []

    issue_json = STRICT_ISSUE_JSON.replace(
        "- Suggested test command: uv run pytest -q tests/test_worklink_orchestrator.py",
        "- Suggested test command: `cd /workspace/mimir && pytest -q tests/test_identities.py`",
    )

    def runner(args: Sequence[str] | str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "443"]:
            return cp(args, stdout=issue_json)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(tmp_path / "repo"), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        return cp(args)

    (tmp_path / "worklink.yaml").write_text("defaults:\n  test_command: echo safe\n", encoding="utf-8")
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(
            home=tmp_path, repo=tmp_path / "repo", runner=runner, registry=registry
        ).run(443, backend_name="fake", dry_run=True)
    )

    out = capsys.readouterr().out
    assert result.dry_run is True
    assert "NOT done until the gate command below passes" in out
    assert "Treat it as advisory only" in out
    assert "  echo safe" in out
    assert "  cd /workspace/mimir && pytest -q tests/test_identities.py" not in out

@pytest.mark.parametrize("backend_name", ["feature_factory", "opencode", "dummy"])
def test_registered_backends_use_isolated_checkout_by_default(
    backend_name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    registry = BackendRegistry(WorklinkConfig())
    if backend_name == "dummy":
        registry.register(FakeBackend())
        backend_name = "fake"
    backend = registry.get(backend_name)
    lease = CheckoutLease(
        issue_id=517,
        attempt=2,
        repo=tmp_path,
        path=tmp_path / "checkout",
        branch="issue/517-a2",
        base_ref="main",
        isolated_checkout=True,
    )
    calls: list[tuple[Path, dict[str, object]]] = []

    def create_checkout(repo: Path, **kwargs: object) -> CheckoutLease:
        calls.append((repo, kwargs))
        return lease

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator, "create_isolated_checkout", create_checkout)

    result = orchestrator._create_backend_checkout(
        tmp_path,
        issue_id=517,
        attempt=2,
        base="main",
        backend=backend,
        runner=runner,
    )

    assert result is lease
    assert calls == [
        (
            tmp_path,
            {
                "issue_id": 517,
                "attempt": 2,
                "base": "main",
                "base_fetch": True,
                "event_logger": None,
                "runner": runner,
                "worker_eligible": False,
            },
        )
    ]


def test_concurrent_opencode_checkouts_do_not_share_parent_git_project(tmp_path: Path) -> None:
    """#1019: linked attempts share the parent's git project, making a sibling
    reachable to OpenCode's repo-wide search before external_directory applies.

    Exercise the real backend checkout route twice: each OpenCode attempt must be
    a standalone repository, so neither the sibling nor parent repo is under its
    git toplevel/common directory while the full base history remains readable.
    """
    from mimir.worklink.orchestrator import _create_backend_checkout

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "shared.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "HEAD:main"], check=True)

    leases = [
        _create_backend_checkout(
            repo,
            issue_id=issue,
            attempt=1,
            base="main",
            backend=BackendRegistry(WorklinkConfig()).get("opencode"),
            runner=lambda args: subprocess.run(args, capture_output=True, text=True, check=False),
        )
        for issue in (1018, 1014)
    ]

    assert all(lease.isolated_checkout for lease in leases)
    assert not any(lease.path.is_relative_to(repo) for lease in leases)
    assert leases[0].path.parent == leases[1].path.parent
    for lease, sibling in ((leases[0], leases[1]), (leases[1], leases[0])):
        top = subprocess.run(
            ["git", "-C", str(lease.path), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        common = subprocess.run(
            ["git", "-C", str(lease.path), "rev-parse", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        history = subprocess.run(
            ["git", "-C", str(lease.path), "rev-list", "--count", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert Path(top).resolve() == lease.path.resolve()
        assert (lease.path / common).resolve().is_relative_to(lease.path.resolve())
        assert not sibling.path.resolve().is_relative_to(Path(top).resolve())
        assert history == "1"
        assert subprocess.run(
            ["git", "-C", str(lease.path), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == str(origin)


def test_outside_checkout_detection_marks_root_leak_failed(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _with_outside_checkout_detection

    validation = EvidenceValidation(
        status="failed",
        review_ready=False,
        reasons=("completed_empty_diff",),
        evidence=WorklinkEvidence(
            issue=517,
            attempt=1,
            backend="codex",
            branch="issue/517-a1",
            checkout=str(tmp_path / ".worklink" / "517-1"),
            started_at="2026-06-16T20:00:00+00:00",
            finished_at="2026-06-16T20:05:00+00:00",
            files_changed=[],
            diff_stat="",
            commands=[],
            tests=None,
            pr_url=None,
            status="failed",
        ),
    )

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=" M mimir/identities.py\n?? scratch.txt\n", stderr="")

    result = _with_outside_checkout_detection(
        validation,
        issue=517,
        attempt=1,
        root=tmp_path,
        checkout=tmp_path / ".worklink" / "517-1",
        runner=runner,
    )

    assert result.status == "failed"
    assert result.review_ready is False
    assert "completed_empty_diff" in result.reasons
    assert any(reason.startswith("backend_wrote_outside_checkout:") for reason in result.reasons)


def test_outside_checkout_leak_is_quarantined_recoverably(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _dirty_paths, _with_outside_checkout_detection

    def git(*args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(tmp_path), *args], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(args), capture_output=True, text=True, check=False)

    git("init", "-q")
    git("config", "user.email", "t@e.com")
    git("config", "user.name", "t")
    (tmp_path / "keep.txt").write_text("orig\n")
    (tmp_path / "mod.py").write_text("v1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")

    # Pre-existing, unrelated operator dirt that MUST survive quarantine.
    (tmp_path / "keep.txt").write_text("operator-work\n")
    root_dirty_before = _dirty_paths(tmp_path, runner=runner)
    assert root_dirty_before == ["keep.txt"]

    # The leak: codex wrote into the repo root (a new file + a tracked edit) while
    # the attempt diff is empty. The isolated checkout lives OUTSIDE the repo.
    (tmp_path / "leaked.py").write_text("escaped\n")
    (tmp_path / "mod.py").write_text("v1\nCODEX\n")
    worktree = tmp_path.parent / ".worklink" / tmp_path.name / "517-1"

    validation = EvidenceValidation(
        status="failed",
        review_ready=False,
        reasons=("completed_empty_diff",),
        evidence=WorklinkEvidence(
            issue=517, attempt=1, backend="codex", branch="issue/517-a1",
            checkout=str(worktree), started_at="2026-06-16T20:00:00+00:00",
            finished_at="2026-06-16T20:05:00+00:00", files_changed=[], diff_stat="",
            commands=[], tests=None, pr_url=None, status="failed",
        ),
    )

    result = _with_outside_checkout_detection(
        validation, issue=517, attempt=1, root=tmp_path, checkout=worktree,
        runner=runner, root_dirty_before=root_dirty_before,
    )

    assert result.status == "failed"
    assert any(r.startswith("backend_wrote_outside_checkout:") for r in result.reasons)
    assert any("worklink-leak-517-a1" in r for r in result.reasons)

    # The leaked paths are gone from the working tree; pre-existing dirt survives.
    assert not (tmp_path / "leaked.py").exists()
    assert (tmp_path / "mod.py").read_text() == "v1\n"
    assert (tmp_path / "keep.txt").read_text() == "operator-work\n"
    # ...and the leak is recoverable, not destroyed.
    assert "worklink-leak-517-a1" in git("stash", "list")


# ─── chainlink #517: fail loud on unsafe codex/compute combo ──────────


class _CodexNamedBackend(FakeBackend):
    name = "codex"



from mimir.worklink.compute import LaunchHandle as _LaunchHandle


@pytest.mark.parametrize(
    ("target_bullets", "error"),
    [
        pytest.param("- Target branch:", "invalid Worklink target branch", id="malformed"),
        pytest.param(
            "- Target branch: feature/one\n- Target branch: feature/two",
            "multiple Target branch bullets",
            id="multiple",
        ),
    ],
)
def test_run_epic_refuses_invalid_target_branch_before_dispatch(
    tmp_path: Path,
    target_bullets: str,
    error: str,
) -> None:
    epic_json = json.dumps(
        {
            "id": 701,
            "title": "invalid target",
            "description": f"Worklink notes:\n{target_bullets}",
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "comments": [],
        }
    )
    calls: list[list[str]] = []

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        assert isinstance(args, list)
        calls.append(args)
        if args[:4] == ["chainlink", "issue", "show", "701"]:
            return cp(args, stdout=epic_json)
        return cp(args)

    with pytest.raises(LeafValidationError, match=error):
        asyncio.run(WorklinkRunner(home=tmp_path, repo=tmp_path, runner=runner).run_epic(701))

    assert not any(call[1:3] == ["locks", "claim"] for call in calls)


def test_run_epic_refuses_nonexistent_target_branch_before_claim_or_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    repo = tmp_path / "repo"
    repo.mkdir()
    epic_json = json.dumps(
        {
            "id": 701,
            "title": "missing target",
            "description": (
                "Worklink notes:\n- Target branch: feature/does-not-exist"
            ),
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "comments": [],
        }
    )
    calls: list[list[str]] = []
    events: list[tuple[str, dict[str, object]]] = []

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        assert isinstance(args, list)
        calls.append(args)
        if args[:4] == ["chainlink", "issue", "show", "701"]:
            return cp(args, stdout=epic_json)
        if args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        if "ls-remote" in args:
            return cp(args, returncode=2)
        return cp(args)

    def unexpected_checkout(*args: object, **kwargs: object) -> object:
        raise AssertionError("checkout creation reached for a nonexistent base")

    monkeypatch.setattr(FeatureFactoryBackend, "admit", lambda self: Path(self.entrypoint))
    monkeypatch.setattr(orchestrator, "_create_backend_checkout", unexpected_checkout)
    monkeypatch.setattr(
        orchestrator,
        "_log_event",
        lambda name, **fields: events.append((name, fields)),
    )

    result = asyncio.run(WorklinkRunner(home=tmp_path, repo=repo, runner=runner).run_epic(701))

    assert result.status == "refused"
    assert result.reason == "base branch does not exist in origin: feature/does-not-exist"
    assert events == [
        (
            "worklink_epic_refused",
            {
                "issue_id": 701,
                "reason": "base branch does not exist in origin: feature/does-not-exist",
            },
        )
    ]
    assert not any(call[1:3] == ["locks", "claim"] for call in calls)


def test_run_epic_non_epic_issue_emits_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    issue_json = json.dumps(
        {
            "id": 701,
            "title": "leaf",
            "description": "build",
            "labels": ["worklink", "worklink:ready"],
            "comments": [],
        }
    )
    events: list[tuple[str, dict[str, object]]] = []

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        return cp(args, stdout=issue_json)

    monkeypatch.setattr(
        orchestrator,
        "_log_event",
        lambda name, **fields: events.append((name, fields)),
    )

    result = asyncio.run(WorklinkRunner(home=tmp_path, repo=tmp_path, runner=runner).run_epic(701))

    assert result.reason == "not an epic issue"
    assert events == [
        ("worklink_epic_refused", {"issue_id": 701, "reason": "not an epic issue"})
    ]


def test_run_epic_autonomy_refusal_emits_leaf_event_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    repo = tmp_path / "repo"
    repo.mkdir()
    epic_json = json.dumps(
        {
            "id": 701,
            "title": "epic",
            "description": "build",
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "comments": [],
        }
    )
    events: list[tuple[str, dict[str, object]]] = []

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "701"]:
            return cp(args, stdout=epic_json)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        return cp(args)

    monkeypatch.setattr(
        orchestrator,
        "_log_event",
        lambda name, **fields: events.append((name, fields)),
    )

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner).run_epic(701, autonomous=True)
    )

    assert result.status == "refused"
    assert events == [
        (
            "worklink_autonomous_refused",
            {
                "issue_id": 701,
                "compute_backend": "local_subprocess",
                "reason": result.reason,
            },
        )
    ]


@pytest.mark.parametrize(
    ("claim", "event_name", "reason"),
    [
        (ClaimResult(False, attempts_exhausted=True), "worklink_attempts_exhausted", "attempts_exhausted"),
        (ClaimResult(False, reason="lock held"), "worklink_claim_failed", "lock held"),
    ],
)
def test_run_epic_claim_refusals_emit_leaf_event_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claim: ClaimResult,
    event_name: str,
    reason: str,
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    repo = tmp_path / "repo"
    repo.mkdir()
    epic_json = json.dumps(
        {
            "id": 701,
            "title": "epic",
            "description": "build",
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "comments": [],
        }
    )
    events: list[tuple[str, dict[str, object]]] = []

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "701"]:
            return cp(args, stdout=epic_json)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        return cp(args)

    monkeypatch.setattr(FeatureFactoryBackend, "admit", lambda self: Path(self.entrypoint))
    monkeypatch.setattr(orchestrator.ChainlinkClaims, "claim_issue", lambda *args, **kwargs: claim)
    monkeypatch.setattr(
        orchestrator,
        "_log_event",
        lambda name, **fields: events.append((name, fields)),
    )

    result = asyncio.run(WorklinkRunner(home=tmp_path, repo=repo, runner=runner).run_epic(701))

    assert result.reason == reason
    assert events == [(event_name, {"issue_id": 701, "reason": reason})]


def test_run_epic_reports_target_branch_lookup_failure_without_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    epic_json = json.dumps(
        {
            "id": 701,
            "title": "unreachable target",
            "description": "Worklink notes:\n- Target branch: feature/acp",
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "comments": [],
        }
    )
    secret_stderr = "fatal: credential github-secret rejected"

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        assert isinstance(args, list)
        if args[:4] == ["chainlink", "issue", "show", "701"]:
            return cp(args, stdout=epic_json)
        if args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        if "ls-remote" in args:
            return cp(args, returncode=128, stderr=secret_stderr)
        return cp(args)

    monkeypatch.setattr(FeatureFactoryBackend, "admit", lambda self: Path(self.entrypoint))

    result = asyncio.run(WorklinkRunner(home=tmp_path, repo=repo, runner=runner).run_epic(701))

    assert result.status == "refused"
    assert result.reason == (
        "base branch lookup failed for origin: feature/acp "
        "(git ls-remote exit code 128)"
    )
    assert secret_stderr not in result.reason


def test_run_epic_refuses_review_state_before_claim_or_factory_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    epic_json = json.dumps(
        {
            "id": 701,
            "title": "already under review",
            "description": "build the thing",
            "labels": ["worklink", "worklink:epic", "worklink:review", "worklink:ready"],
            "parent_id": None,
            "comments": [],
        }
    )
    calls: list[list[str]] = []

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list):
            calls.append(args)
            if args[:4] == ["chainlink", "issue", "show", "701"]:
                return cp(args, stdout=epic_json)
            if args[:4] == ["git", "-C", str(repo), "config"]:
                return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        return cp(args)

    monkeypatch.setattr(
        "mimir.worklink.backends.feature_factory.FeatureFactoryBackend.admit",
        lambda self: Path(self.entrypoint),
    )
    registry = BackendRegistry(WorklinkConfig())

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run_epic(701)
    )

    assert result.status == "failed"
    assert result.reason == "lifecycle_state_incompatible"
    assert not any(call[1:3] == ["locks", "claim"] for call in calls)
    assert not (repo / ".worklink").exists()


def test_checkout_git_identity_reads_both_effective_values_with_fixed_argv(
    tmp_path: Path,
) -> None:
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[-1] == "user.name":
            return cp(args, stdout=" Factory Author \n")
        return cp(args, stdout=" factory@example.com \n")

    assert _read_checkout_git_identity(tmp_path, runner) == (
        "Factory Author",
        "factory@example.com",
    )
    assert calls == [
        ["git", "-C", str(tmp_path), "config", "--get", "user.name"],
        ["git", "-C", str(tmp_path), "config", "--get", "user.email"],
    ]


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ({"user.name": cp([], 1), "user.email": cp([], 1)}, "missing user.name, user.email"),
        ({"user.name": cp([], stdout=" \n"), "user.email": cp([], stdout="a@b.test\n")}, "missing user.name"),
        (
            {"user.name": cp([], 128), "user.email": cp([], 1)},
            "missing user.email; failed user.name (git exit 128)",
        ),
    ],
)
def test_checkout_git_identity_collects_all_failures_before_refusing(
    tmp_path: Path,
    results: dict[str, subprocess.CompletedProcess[str]],
    expected: str,
) -> None:
    calls: list[str] = []

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        assert isinstance(args, list)
        key = args[-1]
        calls.append(key)
        result = results[key]
        return cp(args, result.returncode, result.stdout, result.stderr)

    with pytest.raises(RuntimeError, match=re.escape(expected)):
        _read_checkout_git_identity(tmp_path, runner)

    assert calls == ["user.name", "user.email"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"publishing_identity": " factory-owner "}, "factory-owner"),
        ({"publishing_identity": ""}, None),
        ({"publishing_identity": 7}, None),
        ({"bootstrap": "token=should-not-leak"}, None),
    ],
)
def test_factory_publishing_identity_is_read_only_and_nonblank(
    tmp_path: Path, payload: dict[str, object], expected: str | None
) -> None:
    declaration = tmp_path / ".factory.json"
    declaration.write_text(json.dumps(payload), encoding="utf-8")
    before = declaration.read_bytes()

    if expected is None:
        with pytest.raises(RuntimeError, match="factory publishing identity is missing") as exc:
            _read_factory_publishing_identity(tmp_path, {})
        assert "should-not-leak" not in str(exc.value)
    else:
        assert _read_factory_publishing_identity(tmp_path, {}) == (expected, ".factory.json")

    assert declaration.read_bytes() == before


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("{not-json token=should-not-leak", "declaration is invalid"),
        ("[]", "declaration is invalid"),
    ],
)
def test_factory_publishing_identity_errors_are_secret_safe(
    tmp_path: Path, content: str, expected: str
) -> None:
    (tmp_path / ".factory.json").write_text(content, encoding="utf-8")

    with pytest.raises(RuntimeError, match=expected) as exc:
        _read_factory_publishing_identity(tmp_path, {})

    assert "should-not-leak" not in str(exc.value)


def test_factory_publishing_identity_missing_declaration_is_secret_safe(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as exc:
        _read_factory_publishing_identity(tmp_path, {})

    assert str(exc.value) == "factory publishing identity declaration is unreadable"


def test_factory_publishing_identity_environment_override_wins_without_reading_file(
    tmp_path: Path,
) -> None:
    assert _read_factory_publishing_identity(
        tmp_path, {"MIMIR_FACTORY_PUBLISHING_IDENTITY": " deployment-owner "}
    ) == (
        "deployment-owner",
        "environment variable MIMIR_FACTORY_PUBLISHING_IDENTITY",
    )


@pytest.mark.parametrize("value", ["", "   ", 7, None])
def test_factory_publishing_identity_invalid_environment_override_fails_closed(
    tmp_path: Path, value: object
) -> None:
    (tmp_path / ".factory.json").write_text(
        json.dumps({"publishing_identity": "file-owner"}), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="MIMIR_FACTORY_PUBLISHING_IDENTITY"):
        _read_factory_publishing_identity(
            tmp_path, {"MIMIR_FACTORY_PUBLISHING_IDENTITY": value}
        )


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"GITHUB_TOKEN": "github-token"}, "github-token"),
        ({"GH_TOKEN": "same-token", "GITHUB_TOKEN": "same-token"}, "same-token"),
        ({"GITHUB_TOKEN": " github-token "}, "github-token"),
        ({"GH_TOKEN": " github-token ", "GITHUB_TOKEN": " github-token "}, "github-token"),
    ],
)
def test_factory_github_credential_inherits_process_token_and_normalizes_aliases(
    environ: dict[str, str], expected: str
) -> None:
    token, child = _resolve_factory_github_credential(environ)

    assert token == expected
    assert child == {"GH_TOKEN": expected, "GITHUB_TOKEN": expected}


@pytest.mark.parametrize(
    "environ",
    [
        {"GH_TOKEN": " ", "GITHUB_TOKEN": ""},
        {},
        {"GH_TOKEN": "gh-token-only"},
    ],
)
def test_factory_github_credential_requires_the_process_token(environ: dict[str, str]) -> None:
    with pytest.raises(RuntimeError) as exc:
        _resolve_factory_github_credential(environ)

    assert str(exc.value) == "factory publication requires GITHUB_TOKEN"
    assert "gh-token-only" not in str(exc.value)


def test_factory_github_credential_refuses_conflicting_aliases_without_values() -> None:
    with pytest.raises(RuntimeError) as exc:
        _resolve_factory_github_credential(
            {"GH_TOKEN": "gh-secret", "GITHUB_TOKEN": "github-secret"}
        )

    message = str(exc.value)
    assert "GH_TOKEN" in message
    assert "GITHUB_TOKEN" in message
    assert "gh-secret" not in message
    assert "github-secret" not in message


def test_inherited_credential_clears_a_prebound_identity_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preflight must not collide with a memo an earlier forge call bound.

    `verify_identity` memoizes `(login, fingerprint)` process-wide and refuses a
    fingerprint change before it ever reaches `/user`. Selecting `GH_TOKEN` while
    the rest of the process was bound to `GITHUB_TOKEN` therefore failed the
    preflight even when both credentials belonged to the declared publisher.
    Inheriting the process credential removes the second fingerprint entirely.
    """
    from mimir.forge import github as github_module

    token, child = _resolve_factory_github_credential(
        {"GITHUB_TOKEN": "process-token", "GH_TOKEN": "process-token"}
    )
    assert child == {"GH_TOKEN": "process-token", "GITHUB_TOKEN": "process-token"}

    monkeypatch.setattr(
        github_module,
        "_verified_identity",
        ("factory-owner", github_module._credential_fingerprint("process-token")),
    )

    def unexpected_request(*args: object, **kwargs: object) -> object:
        raise AssertionError("a bound memo must answer without re-querying /user")

    monkeypatch.setattr(github_module.GitHubForgeClient, "_request", unexpected_request)

    assert (
        github_module.GitHubForgeClient(token=token).verify_identity("factory-owner")
        == "factory-owner"
    )

    # The other half of the pair: a second credential is what the memo refuses,
    # so this test fails if the guard it is protecting against stops existing.
    with pytest.raises(RuntimeError, match="does not match active credential"):
        github_module.GitHubForgeClient(token="a-second-token").verify_identity("factory-owner")


def _configure_opencode_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:gpt-5.6-luna")
    auth = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(
        json.dumps({"openai": {"type": "oauth", "refresh": "subscription"}}),
        encoding="utf-8",
    )


def _run_factory_preflight_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    identity_results: dict[str, subprocess.CompletedProcess[str]] | None = None,
    credentials: dict[str, str] | None = None,
    publishing_identity: object = "factory-owner",
    publishing_identity_override: str | None = None,
    verify: Any = None,
) -> tuple[object, list[WorkSpec], list[str], list[list[str]]]:
    import mimir.worklink.orchestrator as orchestrator

    _configure_opencode_oauth(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    checkout = tmp_path / "factory-checkout"
    checkout.mkdir()
    (repo / ".factory.json").write_text(
        json.dumps({"publishing_identity": publishing_identity, "bootstrap": "uv sync"}),
        encoding="utf-8",
    )
    epic = json.dumps(
        {
            "id": 700,
            "title": "epic",
            "description": "build",
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "comments": [],
        }
    )
    commands: list[list[str]] = []
    defaults = {
        "user.name": cp([], stdout="Factory Author\n"),
        "user.email": cp([], stdout="factory@example.com\n"),
    }
    configured_results = identity_results or defaults

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list):
            commands.append(args)
            if args[:4] == ["chainlink", "issue", "show", "700"]:
                return cp(args, stdout=epic)
            if args[:4] == ["git", "-C", str(repo), "config"]:
                return cp(args, stdout="git@github.com:owner/repo.git\n")
            if args[:5] == ["git", "-C", str(checkout), "config", "--get"]:
                result = configured_results[args[-1]]
                return cp(args, result.returncode, result.stdout, result.stderr)
        return cp(args)

    claim = ClaimRecord(700, 1, "agent", datetime.now(UTC))
    lease = CheckoutLease(
        issue_id=700,
        attempt=1,
        repo=repo,
        path=checkout,
        branch="issue/700-a1",
        base_ref="main",
        local_base="origin/main",
        isolated_checkout=True,
    )
    launched: list[WorkSpec] = []

    async def launch(self: object, spec: WorkSpec) -> LaunchHandle:
        launched.append(spec)
        raise RuntimeError("launch reached")

    verified_tokens: list[str] = []

    class Client:
        def __init__(self, *, token: str) -> None:
            self.token = token
            verified_tokens.append(token)

        def verify_identity(self, declared: str) -> str:
            if verify is not None:
                return verify(self.token, declared)
            return declared

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("MIMIR_FACTORY_PUBLISHING_IDENTITY", raising=False)
    if publishing_identity_override is not None:
        monkeypatch.setenv("MIMIR_FACTORY_PUBLISHING_IDENTITY", publishing_identity_override)
    for key, value in (credentials or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(FeatureFactoryBackend, "admit", lambda self: Path(self.entrypoint))
    monkeypatch.setattr(
        orchestrator.ChainlinkClaims,
        "claim_issue",
        lambda self, *args, **kwargs: ClaimResult(True, claim),
    )
    monkeypatch.setattr(
        orchestrator.ChainlinkClaims, "transition_issue", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        orchestrator.ChainlinkClaims, "release_issue", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(orchestrator, "_create_backend_checkout", lambda *args, **kwargs: lease)
    monkeypatch.setattr(orchestrator, "GitHubForgeClient", Client)
    monkeypatch.setattr(orchestrator.LocalSubprocessComputeBackend, "launch", launch)
    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, agent_id="agent").run_epic(700)
    )
    return result, launched, verified_tokens, commands


@pytest.mark.parametrize(
    ("credentials", "selected"),
    [
        ({"GITHUB_TOKEN": "github-token"}, "github-token"),
        ({"GH_TOKEN": "same-token", "GITHUB_TOKEN": "same-token"}, "same-token"),
    ],
)
def test_factory_dispatch_verifies_process_token_and_normalizes_child_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credentials: dict[str, str],
    selected: str,
) -> None:
    result, launched, verified_tokens, commands = _run_factory_preflight_case(
        tmp_path, monkeypatch, credentials=credentials
    )

    assert result.reason == "launch reached"
    assert verified_tokens == [selected]
    assert len(launched) == 1
    assert launched[0].env["GH_TOKEN"] == selected
    assert launched[0].env["GITHUB_TOKEN"] == selected
    assert json.loads(launched[0].env["MIMIR_WORK_ITEM_JSON"]) == {
        "run_id": "chainlink-700",
        "title": "epic",
        "body": "build",
    }
    assert launched[0].env["GIT_AUTHOR_NAME"] == "Factory Author"
    assert launched[0].env["GIT_AUTHOR_EMAIL"] == "factory@example.com"
    assert launched[0].env["GIT_COMMITTER_NAME"] == "Factory Author"
    assert launched[0].env["GIT_COMMITTER_EMAIL"] == "factory@example.com"
    assert launched[0].branch == "feature/chainlink-700"
    assert launched[0].local_checkout == tmp_path / "factory-checkout"
    checkout_configs = [
        command
        for command in commands
        if command[:4] == ["git", "-C", str(tmp_path / "factory-checkout"), "config"]
    ]
    assert checkout_configs == [
        ["git", "-C", str(tmp_path / "factory-checkout"), "config", "--get", "user.name"],
        ["git", "-C", str(tmp_path / "factory-checkout"), "config", "--get", "user.email"],
    ]


def test_factory_dispatched_environment_sets_real_commit_author_and_committer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, launched, _, _ = _run_factory_preflight_case(
        tmp_path, monkeypatch, credentials={"GITHUB_TOKEN": "github-token"}
    )
    checkout = tmp_path / "factory-checkout"
    subprocess.run(["git", "-C", str(checkout), "init", "-q"], check=True)
    (checkout / "change.txt").write_text("factory change\n", encoding="utf-8")
    child_env = dict(os.environ)
    child_env.update(launched[0].env)
    child_env["GIT_CONFIG_NOSYSTEM"] = "1"
    child_env["GIT_CONFIG_GLOBAL"] = os.devnull
    subprocess.run(["git", "-C", str(checkout), "add", "change.txt"], check=True, env=child_env)
    subprocess.run(
        ["git", "-C", str(checkout), "commit", "-q", "-m", "factory commit"],
        check=True,
        env=child_env,
    )

    identity = subprocess.run(
        ["git", "-C", str(checkout), "show", "-s", "--format=%an <%ae>%n%cn <%ce>", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=child_env,
    ).stdout.strip()
    assert result.reason == "launch reached"
    assert identity == "Factory Author <factory@example.com>\nFactory Author <factory@example.com>"
    for key in ("user.name", "user.email"):
        local = subprocess.run(
            ["git", "-C", str(checkout), "config", "--local", "--get", key],
            capture_output=True,
            text=True,
            check=False,
        )
        assert local.returncode == 1


@pytest.mark.parametrize(
    ("identity_results", "missing"),
    [
        (
            {"user.name": cp([], 1), "user.email": cp([], stdout="factory@example.com\n")},
            "user.name",
        ),
        (
            {"user.name": cp([], stdout="Factory Author\n"), "user.email": cp([], 1)},
            "user.email",
        ),
        ({"user.name": cp([], 1), "user.email": cp([], 1)}, "user.name, user.email"),
    ],
)
def test_missing_checkout_git_identity_prevents_factory_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_results: dict[str, subprocess.CompletedProcess[str]],
    missing: str,
) -> None:
    result, launched, verified_tokens, commands = _run_factory_preflight_case(
        tmp_path,
        monkeypatch,
        identity_results=identity_results,
        credentials={"GITHUB_TOKEN": "unused-token"},
    )

    assert result.reason is not None
    assert f"missing {missing}" in result.reason
    assert launched == []
    assert verified_tokens == []
    identity_commands = [command for command in commands if command[-1:] in [["user.name"], ["user.email"]]]
    assert identity_commands == [
        ["git", "-C", str(tmp_path / "factory-checkout"), "config", "--get", "user.name"],
        ["git", "-C", str(tmp_path / "factory-checkout"), "config", "--get", "user.email"],
    ]


def test_missing_factory_github_credential_prevents_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, launched, verified_tokens, _ = _run_factory_preflight_case(tmp_path, monkeypatch)

    assert result.reason == "factory publication requires GITHUB_TOKEN"
    assert launched == []
    assert verified_tokens == []


def test_missing_factory_publishing_identity_prevents_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, launched, verified_tokens, _ = _run_factory_preflight_case(
        tmp_path,
        monkeypatch,
        credentials={"GITHUB_TOKEN": "unused-token"},
        publishing_identity=" ",
    )

    assert result.reason == "factory publishing identity is missing"
    assert launched == []
    assert verified_tokens == []


def test_blank_factory_publishing_identity_override_prevents_launch_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, launched, verified_tokens, _ = _run_factory_preflight_case(
        tmp_path,
        monkeypatch,
        credentials={"GITHUB_TOKEN": "unused-token"},
        publishing_identity="file-owner",
        publishing_identity_override=" ",
    )

    assert result.reason == "MIMIR_FACTORY_PUBLISHING_IDENTITY is set but blank"
    assert launched == []
    assert verified_tokens == []


def test_factory_identity_override_mismatch_refuses_with_selected_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mismatch(_token: str, declared: str) -> str:
        raise GitHubIdentityVerificationError(
            f"github identity mismatch: authenticated as token-owner, declared as {declared}",
            declared_login=declared,
            authenticated_login="token-owner",
        )

    result, launched, _, _ = _run_factory_preflight_case(
        tmp_path,
        monkeypatch,
        credentials={"GITHUB_TOKEN": "secret-token"},
        publishing_identity="file-owner",
        publishing_identity_override="deployment-owner",
        verify=mismatch,
    )

    assert result.reason == (
        "github identity mismatch: authenticated as token-owner, declared as deployment-owner; "
        "selected identity deployment-owner from environment variable "
        "MIMIR_FACTORY_PUBLISHING_IDENTITY"
    )
    assert launched == []
    assert "secret-token" not in result.reason


def test_factory_identity_cache_mismatch_preserves_diagnosis_and_adds_selected_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cache_mismatch(_token: str, declared: str) -> str:
        raise GitHubIdentityVerificationError(
            "github identity verification cache does not match active credential",
            declared_login=declared,
            authenticated_login="cached-owner",
        )

    result, launched, _, _ = _run_factory_preflight_case(
        tmp_path,
        monkeypatch,
        credentials={"GITHUB_TOKEN": "secret-token"},
        publishing_identity="file-owner",
        publishing_identity_override="deployment-owner",
        verify=cache_mismatch,
    )

    assert result.reason == (
        "github identity verification cache does not match active credential; "
        "selected identity deployment-owner from environment variable "
        "MIMIR_FACTORY_PUBLISHING_IDENTITY"
    )
    assert launched == []
    assert "cached-owner" not in result.reason
    assert "secret-token" not in result.reason


def test_conflicting_factory_tokens_refuse_dispatch_before_any_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different credentials are an operator ambiguity, not a precedence question.

    Nothing is verified: preferring one silently is how publication proceeds under
    the wrong identity, which is what `publishing_identity` exists to catch.
    """
    result, launched, verified_tokens, _ = _run_factory_preflight_case(
        tmp_path,
        monkeypatch,
        credentials={"GH_TOKEN": "gh-secret", "GITHUB_TOKEN": "github-secret"},
    )

    assert result.reason is not None
    assert "GH_TOKEN" in result.reason
    assert "GITHUB_TOKEN" in result.reason
    assert launched == []
    assert verified_tokens == []
    assert "gh-secret" not in result.reason
    assert "github-secret" not in result.reason


@pytest.mark.parametrize(
    ("description", "expected_base"),
    [
        pytest.param("build", "main", id="configured-default"),
        pytest.param(
            "build\n\nWorklink notes:\n- Target branch: feature/acp",
            "feature/acp",
            id="declared-target",
        ),
    ],
)
def test_factory_new_run_uses_resolved_base_for_single_checkout_placement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    description: str,
    expected_base: str,
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    _configure_opencode_oauth(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    epic = json.dumps(
        {
            "id": 700,
            "title": "epic",
            "description": description,
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "comments": [],
        }
    )
    placement_calls: list[tuple[Path, dict[str, object]]] = []
    preflight_events: list[str] = []
    worklink_events: list[tuple[str, dict[str, object]]] = []
    claim = ClaimRecord(700, 1, "agent", datetime.now(UTC))
    lease = CheckoutLease(
        issue_id=700,
        attempt=1,
        repo=repo,
        path=tmp_path / "factory-checkout",
        branch="issue/700-a1",
        base_ref=expected_base,
        local_base=f"origin/{expected_base}",
        isolated_checkout=True,
    )
    declaration = repo / ".factory.json"
    declaration.write_text(
        json.dumps({"publishing_identity": "factory-owner", "bootstrap": "uv sync"}),
        encoding="utf-8",
    )
    declaration_before = declaration.read_bytes()
    lease.path.mkdir()
    sandbox_declaration = lease.path / ".factory.json"
    sandbox_declaration.write_text(
        json.dumps({"publishing_identity": "sandbox-owner"}), encoding="utf-8"
    )
    sandbox_declaration_before = sandbox_declaration.read_bytes()

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "700"]:
            return cp(args, stdout=epic)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        if args == ["git", "-C", str(lease.path), "config", "--get", "user.name"]:
            return cp(args, stdout="Factory Author\n")
        if args == ["git", "-C", str(lease.path), "config", "--get", "user.email"]:
            return cp(args, stdout="factory@example.com\n")
        return cp(args)

    def place(checkout_repo: Path, **kwargs: object) -> CheckoutLease:
        preflight_events.append("checkout")
        placement_calls.append((checkout_repo, kwargs))
        return lease

    async def stop_after_placement(self: object, spec: WorkSpec) -> LaunchHandle:
        preflight_events.append("launch")
        assert spec.branch == "feature/chainlink-700"
        assert spec.env == {
            "MIMIR_HOME": str(tmp_path),
            "MIMIR_WORK_ITEM_JSON": json.dumps(
                {"body": description, "run_id": "chainlink-700", "title": "epic"},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "GH_TOKEN": "selected-token",
            "GITHUB_TOKEN": "selected-token",
            "GIT_AUTHOR_NAME": "Factory Author",
            "GIT_AUTHOR_EMAIL": "factory@example.com",
            "GIT_COMMITTER_NAME": "Factory Author",
            "GIT_COMMITTER_EMAIL": "factory@example.com",
        }
        raise RuntimeError("stop after placement")

    original_read_git_identity = orchestrator._read_checkout_git_identity
    original_read_publishing_identity = orchestrator._read_factory_publishing_identity
    original_resolve_credential = orchestrator._resolve_factory_github_credential

    def read_git_identity(checkout: Path, command_runner: object) -> tuple[str, str]:
        preflight_events.append("git-identity")
        return original_read_git_identity(checkout, command_runner)

    def read_publishing_identity(checkout_repo: Path) -> str:
        preflight_events.append("publishing-identity")
        return original_read_publishing_identity(checkout_repo)

    def resolve_credential(environ: object) -> tuple[str, dict[str, str]]:
        preflight_events.append("credential")
        return original_resolve_credential(environ)

    class VerifiedClient:
        def __init__(self, *, token: str) -> None:
            assert token == "selected-token"

        def verify_identity(self, declared: str) -> str:
            preflight_events.append("verify")
            assert declared == "factory-owner"
            return declared

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", " selected-token ")
    monkeypatch.setattr(FeatureFactoryBackend, "admit", lambda self: Path(self.entrypoint))
    monkeypatch.setattr(
        orchestrator.ChainlinkClaims,
        "claim_issue",
        lambda self, *args, **kwargs: ClaimResult(True, claim),
    )
    monkeypatch.setattr(
        orchestrator.ChainlinkClaims, "transition_issue", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        orchestrator.ChainlinkClaims, "release_issue", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(orchestrator, "_create_backend_checkout", place)
    monkeypatch.setattr(orchestrator, "_read_checkout_git_identity", read_git_identity)
    monkeypatch.setattr(orchestrator, "_read_factory_publishing_identity", read_publishing_identity)
    monkeypatch.setattr(orchestrator, "_resolve_factory_github_credential", resolve_credential)
    monkeypatch.setattr(orchestrator, "GitHubForgeClient", VerifiedClient)
    monkeypatch.setattr(
        orchestrator.LocalSubprocessComputeBackend, "launch", stop_after_placement
    )
    monkeypatch.setattr(
        orchestrator,
        "_log_event",
        lambda name, **fields: worklink_events.append((name, fields)),
    )

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, agent_id="agent").run_epic(700)
    )

    assert result.status == "failed"
    assert result.reason == "stop after placement"
    assert len(placement_calls) == 1
    checkout_repo, kwargs = placement_calls[0]
    assert checkout_repo == repo
    assert kwargs["issue_id"] == 700
    assert kwargs["attempt"] == 1
    assert kwargs["base"] == expected_base
    assert isinstance(kwargs["backend"], FeatureFactoryBackend)
    assert preflight_events == [
        "checkout",
        "git-identity",
        "publishing-identity",
        "credential",
        "verify",
        "launch",
    ]
    assert worklink_events[-1] == (
        "worklink_transition",
        {
            "issue_id": 700,
            "attempt": 1,
            "status": "failed",
            "review_ready": False,
            "pr_url": None,
            "reason": "stop after placement",
        },
    )
    assert declaration.read_bytes() == declaration_before
    assert sandbox_declaration.read_bytes() == sandbox_declaration_before


def test_factory_identity_preflight_is_not_repeated_for_retained_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    repo = tmp_path / "repo"
    repo.mkdir()
    epic = json.dumps(
        {
            "id": 700,
            "title": "epic",
            "description": "build",
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "comments": [],
        }
    )

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "700"]:
            return cp(args, stdout=epic)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        if isinstance(args, list) and args[-2:] == ["rev-parse", "--show-toplevel"]:
            return cp(args, stdout=f"{tmp_path / 'sandbox'}\n")
        if isinstance(args, list) and args[-2:] == ["rev-parse", "--absolute-git-dir"]:
            return cp(args, stdout=f"{tmp_path / 'sandbox' / '.git'}\n")
        if isinstance(args, list) and args[-3:] == ["config", "--get", "remote.origin.url"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        if isinstance(args, list) and args[-2:] == ["branch", "--show-current"]:
            return cp(args, stdout="epic/700\n")
        if isinstance(args, list) and "rev-parse" in args:
            return cp(args, stdout="a" * 40 + "\n")
        return cp(args)

    claim = ClaimRecord(700, 1, "agent", datetime.now(UTC))
    retained = FactoryRunRecord(
        run_id="700",
        issue_id=700,
        attempt=1,
        repository="owner/repo",
        base_ref="main",
        branch="epic/700",
        launcher="/opt/factory/bin/factory.js",
        sandbox=str(tmp_path / "sandbox"),
        session="session-1",
        handle=None,
        status=None,
        observed_at=None,
        controller_phase="running",
    )
    (tmp_path / "sandbox").mkdir()
    save_factory_record(tmp_path, retained)

    async def recover(self: object, **kwargs: object) -> object:
        assert kwargs["retained"] == retained
        return orchestrator.WorklinkRunResult(700, 1, "needs-human")

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("new-run identity preflight reached during recovery")

    monkeypatch.setattr(FeatureFactoryBackend, "admit", lambda self: Path(retained.launcher))

    def claim_issue(self: object, *args: object, **kwargs: object) -> ClaimResult:
        kwargs["before_claim"]()
        return ClaimResult(True, claim)

    monkeypatch.setattr(orchestrator.ChainlinkClaims, "claim_issue", claim_issue)
    monkeypatch.setattr(
        orchestrator.ChainlinkClaims, "release_issue", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(WorklinkRunner, "_recover_factory_070", recover)
    monkeypatch.setattr(orchestrator, "_read_checkout_git_identity", unexpected)
    monkeypatch.setattr(orchestrator, "_read_factory_publishing_identity", unexpected)
    monkeypatch.setattr(orchestrator, "_resolve_factory_github_credential", unexpected)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, agent_id="agent").run_epic(700)
    )

    assert result.status == "needs-human", result.reason


@pytest.mark.parametrize("refusal", ["sandbox", "launcher", "base", "session", "lifecycle"])
def test_unbindable_factory_record_is_archived_before_claim_and_fresh_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    _configure_opencode_oauth(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    old_sandbox = tmp_path / "factory-checkout-1"
    old_sandbox.mkdir()
    fresh_sandbox = tmp_path / "factory-checkout-2"
    fresh_sandbox.mkdir()
    epic = json.dumps(
        {
            "id": 700,
            "title": "epic",
            "description": "build",
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "comments": [],
        }
    )
    retained = FactoryRunRecord(
        run_id="700",
        issue_id=700,
        attempt=1,
        repository="owner/repo",
        base_ref="main",
        branch="issue/700-a1",
        launcher="/opt/factory/bin/factory.js",
        sandbox=str(old_sandbox),
        session="session-1",
        handle=LaunchHandle("local_subprocess", "999999999", 1),
        status=None,
        observed_at=None,
        controller_phase="failed",
        controller_error="factory status missing field: branch",
    )
    if refusal == "sandbox":
        old_sandbox.rmdir()
    elif refusal == "launcher":
        retained = replace(retained, launcher="/opt/old/factory.js")
    elif refusal == "base":
        retained = replace(retained, base_ref="develop")
    elif refusal == "session":
        retained = replace(retained, session=None)
    elif refusal == "lifecycle":
        retained = replace(retained, controller_phase="stopped")
    save_factory_record(tmp_path, retained)
    claim = ClaimRecord(700, 2, "agent", datetime.now(UTC), budget_attempt=2)
    lease = CheckoutLease(
        issue_id=700,
        attempt=2,
        repo=repo,
        path=fresh_sandbox,
        branch="issue/700-a2",
        base_ref="main",
        local_base="origin/main",
        isolated_checkout=True,
    )
    transitions: list[dict[str, object]] = []
    claimed_after_archive: list[bool] = []
    archive_events: list[tuple[str, dict[str, object]]] = []

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "700"]:
            return cp(args, stdout=epic)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        return cp(args)

    async def launch(self: object, spec: WorkSpec) -> LaunchHandle:
        root = fresh_sandbox / ".factory-sandboxes"
        sandbox = fresh_sandbox / ".factory-sandboxes" / "chainlink-700"
        assert root.is_dir()
        assert root.stat().st_mode & 0o777 == 0o700
        assert not sandbox.exists()
        assert not sandbox.is_symlink()
        assert spec.local_argv is not None
        assert spec.local_argv[-1].split()[-1] == "chainlink-700"
        assert json.loads(spec.env["MIMIR_WORK_ITEM_JSON"])["run_id"] == "chainlink-700"
        return LaunchHandle("local_subprocess", "123", 456)

    async def supervise(self: object, **kwargs: object) -> object:
        current = kwargs["factory_record"]
        assert isinstance(current, FactoryRunRecord)
        assert current.attempt == 2
        assert current.run_id == "chainlink-700"
        assert current.sandbox == str(
            fresh_sandbox / ".factory-sandboxes" / "chainlink-700"
        )
        assert current.branch == "feature/chainlink-700"
        return orchestrator.WorklinkRunResult(700, 2, "needs-human")

    def recover(*args: object, **kwargs: object) -> object:
        raise AssertionError("sessionless record routed to recovery")

    class VerifiedClient:
        def __init__(self, *, token: str) -> None:
            pass

        def verify_identity(self, expected: str) -> None:
            pass

    monkeypatch.setattr(
        FeatureFactoryBackend, "admit", lambda self: Path("/opt/factory/bin/factory.js")
    )

    def claim_issue(self: object, *args: object, **kwargs: object) -> ClaimResult:
        kwargs["before_claim"]()
        claimed_after_archive.append(load_factory_record(tmp_path, "700") is None)
        return ClaimResult(True, claim)

    monkeypatch.setattr(orchestrator.ChainlinkClaims, "claim_issue", claim_issue)
    monkeypatch.setattr(
        orchestrator.ChainlinkClaims, "release_issue", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        orchestrator.ChainlinkClaims,
        "transition_issue",
        lambda self, *args, **kwargs: transitions.append(kwargs),
    )
    monkeypatch.setattr(orchestrator, "_create_backend_checkout", lambda *args, **kwargs: lease)
    monkeypatch.setattr(
        orchestrator, "_read_checkout_git_identity", lambda *args: ("Factory", "factory@test")
    )
    monkeypatch.setattr(
        orchestrator, "_read_factory_publishing_identity", lambda *args: ("owner", "test")
    )
    monkeypatch.setattr(
        orchestrator,
        "_resolve_factory_github_credential",
        lambda *args: ("token", {"GH_TOKEN": "token", "GITHUB_TOKEN": "token"}),
    )
    monkeypatch.setattr(orchestrator, "GitHubForgeClient", VerifiedClient)
    monkeypatch.setattr(
        orchestrator,
        "_log_durable_event",
        lambda event, **payload: archive_events.append((event, payload)),
    )
    monkeypatch.setattr(orchestrator.LocalSubprocessComputeBackend, "launch", launch)
    monkeypatch.setattr(WorklinkRunner, "_supervise_factory_070", supervise)
    monkeypatch.setattr(WorklinkRunner, "_recover_factory_070", recover)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, agent_id="agent").run_epic(700)
    )

    assert result.status == "needs-human"
    assert claimed_after_archive == [True]
    assert transitions == []
    assert load_factory_record(tmp_path, "chainlink-700").attempt == 2
    archives = list(
        (tmp_path / "state" / "worklink" / "factory-runs" / "archive").glob("*.json")
    )
    assert len(archives) == 1
    assert FactoryRunRecord.from_json(
        json.loads(archives[0].read_text(encoding="utf-8"))
    ) == retained
    expected_reasons = {
        "sandbox": "retained factory sandbox is unavailable",
        "launcher": "retained factory launcher does not match recovery request",
        "base": "retained factory base does not match recovery request",
        "session": "retained factory session is missing",
        "lifecycle": "retained factory lifecycle is not recoverable",
    }
    assert archive_events == [
        (
            "worklink_factory_record_archived",
            {
                "source": "dispatch_abandonment",
                "issue_id": 700,
                "run_id": "700",
                "attempt": 1,
                "session": retained.session,
                "phase": retained.controller_phase,
                "reason": expected_reasons[refusal],
                "archive_path": str(archives[0]),
            },
        )
    ]
    assert old_sandbox.is_dir() is (refusal != "sandbox")


def test_factory_launch_preflight_refuses_existing_run_sandbox(tmp_path: Path) -> None:
    import mimir.worklink.orchestrator as orchestrator

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    root = checkout / ".factory-sandboxes"
    root.mkdir(mode=0o700)
    sandbox = root / "chainlink-700"
    sandbox.mkdir(mode=0o700)
    lease = CheckoutLease(
        issue_id=700,
        attempt=2,
        repo=tmp_path / "repo",
        path=checkout,
        branch="issue/700-a2",
        base_ref="main",
        local_base="origin/main",
        isolated_checkout=True,
    )
    record = FactoryRunRecord(
        run_id="chainlink-700",
        issue_id=700,
        attempt=2,
        repository="owner/repo",
        base_ref="main",
        branch="feature/chainlink-700",
        launcher="/opt/factory/bin/factory.js",
        sandbox=str(sandbox),
        session=None,
        handle=None,
        status=None,
        observed_at=None,
        controller_phase="running",
    )

    with pytest.raises(WorklinkError, match=re.escape(str(sandbox))):
        orchestrator._create_factory_sandbox(record, lease)

    assert sandbox.is_dir()


@pytest.mark.parametrize("phase", ["running", "parked", "failed", "terminal"])
def test_factory_recovery_phases_are_explicit(phase: str) -> None:
    import mimir.worklink.orchestrator as orchestrator

    assert phase in orchestrator._RECOVERABLE_FACTORY_PHASES
    assert "stopped" not in orchestrator._RECOVERABLE_FACTORY_PHASES


@pytest.mark.parametrize("autonomous", [False, True])
def test_every_epic_claim_uses_factory_concurrency_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    autonomous: bool,
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "worklink.yaml").write_text(
        "defaults:\n  allow_autonomous_local_subprocess: true\n",
        encoding="utf-8",
    )
    epic = json.dumps(
        {
            "id": 700,
            "title": "epic",
            "description": "build",
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "comments": [],
        }
    )
    observed: list[dict[str, object]] = []

    def runner(args: Sequence[str] | str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "700"]:
            return cp(args, stdout=epic)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        return cp(args)

    def claim_issue(self: ChainlinkClaims, issue_id: int, comments: object, **kwargs: object):
        observed.append(kwargs)
        return ClaimResult(False, reason="concurrency cap reached (1/1 active claims)")

    monkeypatch.setenv("MIMIR_FACTORY_MAX_CONCURRENT", "1")
    monkeypatch.setattr(FeatureFactoryBackend, "admit", lambda self: Path(self.entrypoint))
    monkeypatch.setattr(orchestrator.ChainlinkClaims, "claim_issue", claim_issue)
    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner).run_epic(
            700, autonomous=autonomous
        )
    )

    assert result.reason == "concurrency cap reached (1/1 active claims)"
    before_claim = observed[0].pop("before_claim")
    assert callable(before_claim)
    assert observed == [{
        "labels": {"worklink", "worklink:epic", "worklink:ready"},
        "max_active_locks": 1,
        "active_label": "worklink:epic",
    }]


def _factory_lifecycle_status(
    sandbox: Path,
    *,
    status: str,
    lock: str = "fresh",
    session: str | None = "session-1",
    pr_base: str | None = "main",
) -> Any:
    return parse_factory_status(
        {
            "run_id": "700",
            "issue_key": "700",
            "valid": True,
            "sandbox_path": str(sandbox),
            "status": status,
            "mode": "autonomous",
            "branch": "epic/700",
            "pr_base": pr_base,
            "pr_draft": False,
            "lock": lock,
            "dead_lock": False,
            "lock_session": session,
            "gates": {},
            "steps": ["implementation"],
            "slices": ["factory-070-migration"],
            "validator": None,
            "pr_url": None,
            "terminal_result": None,
            "next": "implementation",
        }
    )


def _factory_lifecycle_record(sandbox: Path, handle: LaunchHandle) -> FactoryRunRecord:
    return FactoryRunRecord(
        run_id="700",
        issue_id=700,
        attempt=1,
        repository="owner/repo",
        base_ref="main",
        branch="epic/700",
        launcher="/opt/factory/bin/factory.js",
        sandbox=str(sandbox),
        session="session-1",
        handle=handle,
        status=None,
        observed_at=None,
        controller_phase="running",
    )


@pytest.mark.parametrize(
    ("factory_status", "expected_status", "review_ready", "pr_url"),
    [
        ("needs-human", "needs-human", False, None),
        ("blocked", "blocked", False, None),
        ("completed", "review_ready", True, "https://github.com/owner/repo/pull/42"),
    ],
)
def test_factory_terminal_outcomes_emit_transition_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory_status: str,
    expected_status: str,
    review_ready: bool,
    pr_url: str | None,
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    record = _factory_lifecycle_record(
        sandbox,
        LaunchHandle("local_subprocess", "123", 456),
    ).observed(
        _factory_lifecycle_status(sandbox, status=factory_status),
        datetime.now(UTC).isoformat(),
    )
    events: list[tuple[str, dict[str, object]]] = []

    class Claims:
        def transition_issue(self, *args: object, **kwargs: object) -> None:
            return None

    async def verify(**kwargs: object) -> tuple[Path, str]:
        return tmp_path / "evidence.json", "https://github.com/owner/repo/pull/42"

    monkeypatch.setattr(orchestrator, "_verify_factory_completion", verify)
    monkeypatch.setattr(
        orchestrator,
        "_log_event",
        lambda name, **fields: events.append((name, fields)),
    )

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=tmp_path)._finish_factory_070(
            issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
            claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
            claims=Claims(),
            backend=object(),
            compute=object(),
            factory_record=record,
            test_cmd="pytest -q",
            runner=lambda args: cp(args),
            started_at=datetime.now(UTC),
        )
    )

    assert result.status == expected_status
    assert events == [
        (
            "worklink_transition",
            {
                "issue_id": 700,
                "attempt": 1,
                "status": expected_status,
                "review_ready": review_ready,
                "pr_url": pr_url,
                **(
                    {"reason": result.reason}
                    if result.reason is not None
                    else {}
                ),
            },
        )
    ]


@pytest.mark.parametrize("lifecycle", ["running", "needs-human", "blocked", "partial"])
def test_factory_status_binding_allows_null_base_before_completion(
    tmp_path: Path,
    lifecycle: str,
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    record = _factory_lifecycle_record(
        sandbox,
        LaunchHandle("local_subprocess", "123", 456),
    )
    status = _factory_lifecycle_status(sandbox, status=lifecycle, pr_base=None)

    orchestrator._require_factory_status(status, record)
    observed = record.observed(status, datetime.now(UTC).isoformat())
    assert observed.status is not None
    assert observed.status.pr_base is None


def test_factory_status_binding_rejects_populated_base_mismatch(tmp_path: Path) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    record = _factory_lifecycle_record(
        sandbox,
        LaunchHandle("local_subprocess", "123", 456),
    )
    status = _factory_lifecycle_status(sandbox, status="running", pr_base="develop")

    with pytest.raises(orchestrator.WorklinkError, match="base mismatch"):
        orchestrator._require_factory_status(status, record)


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("issue_key", "issue key is missing"),
        ("status", "missing lifecycle status"),
        ("mode", "mode is missing"),
        ("branch", "branch is missing"),
        ("pr_draft", "PR draft state is missing"),
        ("lock", "lock state is missing"),
        ("dead_lock", "dead-lock state is missing"),
    ],
)
def test_factory_status_binding_names_missing_consumed_lifecycle_field(
    tmp_path: Path,
    field: str,
    reason: str,
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    record = _factory_lifecycle_record(
        sandbox,
        LaunchHandle("local_subprocess", "123", 456),
    )
    status = replace(
        _factory_lifecycle_status(sandbox, status="running"),
        **{field: None},
    )

    with pytest.raises(orchestrator.WorklinkError, match=reason):
        orchestrator._require_factory_status(status, record)


def test_factory_status_binding_refuses_pre_manifest_diagnostic(tmp_path: Path) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    record = _factory_lifecycle_record(
        sandbox,
        LaunchHandle("local_subprocess", "123", 456),
    )
    status = parse_factory_status(
        {
            "run_id": "700",
            "valid": False,
            "sandbox_path": str(sandbox),
            "error": "run.json does not exist",
        }
    )

    with pytest.raises(orchestrator.WorklinkError, match="factory status is invalid"):
        orchestrator._require_factory_status(status, record)


def test_factory_supervision_drains_exact_handle_while_status_is_polled(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    handle = LaunchHandle("local_subprocess", "123", 456)
    wait_started = threading.Event()
    cancelled = asyncio.Event()
    waited: list[tuple[LaunchHandle, int]] = []
    lifecycle: list[tuple[str, LaunchHandle]] = []

    class Compute:
        async def wait(self, selected: LaunchHandle, timeout_s: int) -> ComputeResult:
            waited.append((selected, timeout_s))
            wait_started.set()
            await cancelled.wait()
            return ComputeResult(-15, "debug output", "cancelled", handle=selected)

        def job_alive(self, selected: LaunchHandle) -> bool:
            return not cancelled.is_set()

        async def cancel(self, selected: LaunchHandle) -> None:
            lifecycle.append(("cancel", selected))
            cancelled.set()

        async def cleanup(self, selected: LaunchHandle) -> None:
            lifecycle.append(("cleanup", selected))

    class Backend:
        poll_interval_s = 0

        def status(self, *args: object, **kwargs: object) -> Any:
            assert wait_started.wait(1)
            return _factory_lifecycle_status(sandbox, status="needs-human")

        def heartbeat(self, *args: object, **kwargs: object) -> None:
            return None

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=tmp_path)._supervise_factory_070(
            issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
            claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
            claims=object(),
            backend=Backend(),
            compute=Compute(),
            factory_record=_factory_lifecycle_record(sandbox, handle),
            test_cmd="pytest -q",
            runner=lambda args: cp(args),
            started_at=datetime.now(UTC),
        )
    )

    assert result.status == "needs-human"
    assert waited and waited[0][0] == handle
    assert lifecycle == [("cancel", handle), ("cleanup", handle)]


def test_factory_supervision_waits_for_manifest_then_proceeds(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    handle = LaunchHandle("local_subprocess", "123", 456)
    stopped = asyncio.Event()
    statuses = [
        parse_factory_status(
            {
                "run_id": "700",
                "valid": False,
                "sandbox_path": str(sandbox),
                "error": "run.json does not exist",
            }
        ),
        _factory_lifecycle_status(sandbox, status="needs-human"),
    ]

    class Compute:
        async def wait(self, selected: LaunchHandle, timeout_s: int) -> ComputeResult:
            await stopped.wait()
            return ComputeResult(-15, "", "cancelled", handle=selected)

        def job_alive(self, selected: LaunchHandle) -> bool:
            return not stopped.is_set()

        async def cancel(self, selected: LaunchHandle) -> None:
            stopped.set()

        async def cleanup(self, selected: LaunchHandle) -> None:
            return None

    class Backend:
        poll_interval_s = 0

        def status(self, *args: object, **kwargs: object) -> Any:
            return statuses.pop(0)

        def heartbeat(self, *args: object, **kwargs: object) -> None:
            return None

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=tmp_path)._supervise_factory_070(
            issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
            claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
            claims=object(),
            backend=Backend(),
            compute=Compute(),
            factory_record=_factory_lifecycle_record(sandbox, handle),
            test_cmd="pytest -q",
            runner=lambda args: cp(args),
            started_at=datetime.now(UTC),
        )
    )

    assert result.status == "needs-human"
    assert statuses == []


def test_factory_executor_exit_retains_scrubbed_tail_and_record_pointer(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    handle = LaunchHandle("local_subprocess", "123", 456)
    cleaned: list[LaunchHandle] = []

    class Compute:
        async def wait(self, selected: LaunchHandle, timeout_s: int) -> ComputeResult:
            return ComputeResult(
                23,
                "executor stdout tail",
                "api_key=super-secret-value\nexecutor captured tail",
                handle=selected,
                command=("opencode", "run"),
            )

        def job_alive(self, selected: LaunchHandle) -> bool:
            return False

        async def cancel(self, selected: LaunchHandle) -> None:
            raise AssertionError("exited executor was cancelled")

        async def cleanup(self, selected: LaunchHandle) -> None:
            cleaned.append(selected)

    class Backend:
        poll_interval_s = 0

        def status(self, *args: object, **kwargs: object) -> Any:
            return _factory_lifecycle_status(sandbox, status="running")

        def heartbeat(self, *args: object, **kwargs: object) -> None:
            return None

    with pytest.raises(WorklinkError, match="OpenCode process exited"):
        asyncio.run(
            WorklinkRunner(home=tmp_path, repo=tmp_path)._supervise_factory_070(
                issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
                claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
                claims=object(),
                backend=Backend(),
                compute=Compute(),
                factory_record=_factory_lifecycle_record(sandbox, handle),
                test_cmd="pytest -q",
                runner=lambda args: cp(args),
                started_at=datetime.now(UTC),
            )
        )

    retained = load_factory_record(tmp_path, "700")
    assert retained is not None
    assert retained.transcript is not None
    transcript_path = Path(retained.transcript)
    assert transcript_path.parent == tmp_path / "state" / "worklink" / "transcripts"
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    assert transcript["exit_code"] == 23
    assert transcript["status"] == "failed"
    assert transcript["stderr"].endswith("executor captured tail")
    assert "super-secret-value" not in transcript["stderr"]
    assert cleaned == [handle]


@pytest.mark.parametrize("failure", ["status", "heartbeat", "persistence", "timeout"])
def test_factory_supervision_cancels_and_cleans_on_every_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    handle = LaunchHandle("local_subprocess", "123", 456)
    stopped = asyncio.Event()
    lifecycle: list[str] = []

    class Compute:
        async def wait(self, selected: LaunchHandle, timeout_s: int) -> ComputeResult:
            await stopped.wait()
            return ComputeResult(-15, "", "cancelled", handle=selected)

        def job_alive(self, selected: LaunchHandle) -> bool:
            return not stopped.is_set()

        async def cancel(self, selected: LaunchHandle) -> None:
            lifecycle.append("cancel")
            stopped.set()

        async def cleanup(self, selected: LaunchHandle) -> None:
            lifecycle.append("cleanup")

    class Backend:
        poll_interval_s = 0.01

        def status(self, *args: object, **kwargs: object) -> Any:
            if failure == "status":
                raise ValueError("malformed status")
            return _factory_lifecycle_status(sandbox, status="running")

        def heartbeat(self, *args: object, **kwargs: object) -> None:
            if failure == "heartbeat":
                raise RuntimeError("heartbeat failed")

    if failure == "persistence":
        monkeypatch.setattr(
            orchestrator,
            "save_factory_record",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("persist failed")),
        )
    if failure == "timeout":
        monkeypatch.setattr(orchestrator, "_epic_run_timeout_s", lambda: 0.02)

    with pytest.raises((ValueError, RuntimeError, OSError, orchestrator.WorklinkError)):
        asyncio.run(
            WorklinkRunner(home=tmp_path, repo=tmp_path)._supervise_factory_070(
                issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
                claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
                claims=object(),
                backend=Backend(),
                compute=Compute(),
                factory_record=_factory_lifecycle_record(sandbox, handle),
                test_cmd="pytest -q",
                runner=lambda args: cp(args),
                started_at=datetime.now(UTC),
            )
        )

    assert lifecycle == ["cancel", "cleanup"]


def test_factory_pre_manifest_status_is_bounded_by_startup_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    handle = LaunchHandle("local_subprocess", "123", 456)
    stopped = asyncio.Event()
    lifecycle: list[str] = []
    status_calls = 0

    class Compute:
        async def wait(self, selected: LaunchHandle, timeout_s: int) -> ComputeResult:
            await stopped.wait()
            return ComputeResult(-15, "", "cancelled", handle=selected)

        def job_alive(self, selected: LaunchHandle) -> bool:
            return not stopped.is_set()

        async def cancel(self, selected: LaunchHandle) -> None:
            lifecycle.append("cancel")
            stopped.set()

        async def cleanup(self, selected: LaunchHandle) -> None:
            lifecycle.append("cleanup")

    class Backend:
        poll_interval_s = 0

        def status(self, *args: object, **kwargs: object) -> Any:
            nonlocal status_calls
            status_calls += 1
            return parse_factory_status(
                {
                    "run_id": "700",
                    "valid": False,
                    "sandbox_path": str(sandbox),
                    "error": "run.json does not exist",
                }
            )

    monkeypatch.setattr(orchestrator, "_FACTORY_STARTUP_STATUS_TIMEOUT_S", 0.03)
    with pytest.raises(orchestrator.WorklinkError, match="factory never initialised"):
        asyncio.run(
            WorklinkRunner(home=tmp_path, repo=tmp_path)._supervise_factory_070(
                issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
                claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
                claims=object(),
                backend=Backend(),
                compute=Compute(),
                factory_record=_factory_lifecycle_record(sandbox, handle),
                test_cmd="pytest -q",
                runner=lambda args: cp(args),
                started_at=datetime.now(UTC),
            )
        )

    assert lifecycle == ["cancel", "cleanup"]
    assert status_calls >= 2


@pytest.mark.parametrize("after_valid", [False, True])
def test_factory_supervision_immediately_refuses_other_invalid_statuses(
    tmp_path: Path,
    after_valid: bool,
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    handle = LaunchHandle("local_subprocess", "123", 456)
    stopped = asyncio.Event()
    invalid = replace(
        _factory_lifecycle_status(sandbox, status="running"),
        valid=False,
    )
    statuses = (
        [_factory_lifecycle_status(sandbox, status="running"), invalid]
        if after_valid
        else [invalid]
    )

    class Compute:
        async def wait(self, selected: LaunchHandle, timeout_s: int) -> ComputeResult:
            await stopped.wait()
            return ComputeResult(-15, "", "cancelled", handle=selected)

        def job_alive(self, selected: LaunchHandle) -> bool:
            return not stopped.is_set()

        async def cancel(self, selected: LaunchHandle) -> None:
            stopped.set()

        async def cleanup(self, selected: LaunchHandle) -> None:
            return None

    class Backend:
        poll_interval_s = 0

        def status(self, *args: object, **kwargs: object) -> Any:
            return statuses.pop(0)

        def heartbeat(self, *args: object, **kwargs: object) -> None:
            return None

    with pytest.raises(orchestrator.WorklinkError, match="factory status is invalid"):
        asyncio.run(
            WorklinkRunner(home=tmp_path, repo=tmp_path)._supervise_factory_070(
                issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
                claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
                claims=object(),
                backend=Backend(),
                compute=Compute(),
                factory_record=_factory_lifecycle_record(sandbox, handle),
                test_cmd="pytest -q",
                runner=lambda args: cp(args),
                started_at=datetime.now(UTC),
            )
        )


def _completion_record(sandbox: Path) -> FactoryRunRecord:
    status = parse_factory_status(
        {
            **_factory_lifecycle_status(sandbox, status="completed").to_json(),
            "lock": "absent",
            "lock_session": None,
            "pr_url": "https://github.com/owner/repo/pull/42",
        }
    )
    return FactoryRunRecord(
        run_id="700",
        issue_id=700,
        attempt=1,
        repository="owner/repo",
        base_ref="main",
        branch="epic/700",
        launcher="/opt/factory/bin/factory.js",
        sandbox=str(sandbox),
        session="session-1",
        handle=None,
        status=status,
        observed_at="2026-08-18T12:00:00+00:00",
        controller_phase="terminal",
    )


def _completion_validation(
    sandbox: Path,
    *,
    files: list[str] | None = None,
    tests: TestResult | None = None,
    review_ready: bool = True,
    diff_observed: bool = True,
) -> EvidenceValidation:
    evidence = WorklinkEvidence(
        issue=700,
        attempt=1,
        backend="feature_factory",
        branch="epic/700",
        checkout=str(sandbox),
        started_at="2026-08-18T12:00:00+00:00",
        finished_at="2026-08-18T12:05:00+00:00",
        files_changed=["changed.py"] if files is None else files,
        diff_stat=" changed.py | 1 +",
        commands=[],
        tests=tests if tests is not None else TestResult("pytest -q", 0, "ok"),
        pr_url="https://github.com/owner/repo/pull/42",
        status="completed",
        diff_observed=diff_observed,
    )
    return EvidenceValidation(
        status="completed" if review_ready else "failed",
        review_ready=review_ready,
        reasons=(),
        evidence=evidence,
    )


def _completion_runner(
    sandbox: Path,
    *,
    case: str,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    head_reads = 0

    def runner(args: Sequence[str] | str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal head_reads
        if isinstance(args, list) and args[-2:] == ["rev-parse", "--show-toplevel"]:
            return cp(args, stdout=f"{sandbox}\n")
        if isinstance(args, list) and args[-2:] == ["rev-parse", "--absolute-git-dir"]:
            return cp(args, stdout=f"{sandbox / '.git'}\n")
        if isinstance(args, list) and args[-2:] == ["branch", "--show-current"]:
            return cp(args, stdout="epic/700\n")
        if isinstance(args, list) and args[-3:] == ["config", "--get", "remote.origin.url"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        if isinstance(args, list) and args[-2:] == ["rev-parse", "HEAD"]:
            head_reads += 1
            value = "a" * 40
            if case == "moved_evidence" and head_reads >= 2:
                value = "b" * 40
            if case == "moved_publication" and head_reads >= 3:
                value = "b" * 40
            return cp(args, stdout=value + "\n")
        if isinstance(args, list) and "rev-parse" in args:
            return cp(args, stdout="c" * 40 + "\n")
        if isinstance(args, list) and args[-3:] == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return cp(args, stdout="?? dirty.txt\n" if case == "dirty" else "")
        if isinstance(args, list) and args[:2] == ["gh", "api"]:
            if case == "github_failure":
                return cp(args, returncode=1, stderr="api failed")
            payload: dict[str, Any] = {
                "html_url": "https://github.com/owner/repo/pull/42",
                "state": "open",
                "draft": False,
                "base": {"repo": {"full_name": "owner/repo"}, "ref": "main"},
                "head": {
                    "repo": {"full_name": "owner/repo"},
                    "ref": "epic/700",
                    "sha": "a" * 40,
                },
            }
            if case == "github_url":
                payload["html_url"] = "https://github.com/owner/repo/pull/43"
            elif case == "github_state":
                payload["state"] = "closed"
            elif case == "github_draft":
                payload["draft"] = True
            elif case == "base_repository":
                payload["base"]["repo"]["full_name"] = "other/repo"
            elif case == "base_ref":
                payload["base"]["ref"] = "develop"
            elif case == "head_repository":
                payload["head"]["repo"]["full_name"] = "fork/repo"
            elif case == "head_ref":
                payload["head"]["ref"] = "wrong"
            elif case == "head_sha":
                payload["head"]["sha"] = "b" * 40
            elif case.startswith("missing_"):
                path = {
                    "missing_html_url": ("html_url",),
                    "missing_state": ("state",),
                    "missing_draft": ("draft",),
                    "missing_base": ("base",),
                    "missing_base_repo": ("base", "repo"),
                    "missing_base_repo_full_name": ("base", "repo", "full_name"),
                    "missing_base_ref": ("base", "ref"),
                    "missing_head": ("head",),
                    "missing_head_repo": ("head", "repo"),
                    "missing_head_repo_full_name": ("head", "repo", "full_name"),
                    "missing_head_ref": ("head", "ref"),
                    "missing_head_sha": ("head", "sha"),
                }[case]
                target: dict[str, Any] = payload
                for part in path[:-1]:
                    target = target[part]
                target.pop(path[-1], None)
            return cp(args, stdout=json.dumps(payload))
        return cp(args)

    return runner


@pytest.mark.parametrize(
    "case",
    [
        "missing_status",
        "status",
        "valid",
        "run",
        "issue",
        "sandbox",
        "mode",
        "branch",
        "base",
        "draft",
        "url_missing",
        "url_noncanonical",
        "url_repository",
        "empty_diff",
        "diff_unobserved",
        "tests_missing",
        "tests_unobserved",
        "tests_skipped",
        "tests_red",
        "evidence_rejected",
        "dirty",
        "moved_evidence",
        "moved_publication",
        "github_failure",
        "github_url",
        "github_state",
        "github_draft",
        "base_repository",
        "base_ref",
        "head_repository",
        "head_ref",
        "head_sha",
        "missing_html_url",
        "missing_state",
        "missing_draft",
        "missing_base",
        "missing_base_repo",
        "missing_base_repo_full_name",
        "missing_base_ref",
        "missing_head",
        "missing_head_repo",
        "missing_head_repo_full_name",
        "missing_head_ref",
        "missing_head_sha",
    ],
)
def test_factory_completion_rejection_matrix_persists_failed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    record = _completion_record(sandbox)
    status = record.status
    assert status is not None
    status_overrides: dict[str, Any] = {}
    if case == "missing_status":
        object.__setattr__(record, "status", None)
    elif case == "status":
        status_overrides["status"] = "partial"
    elif case == "valid":
        status_overrides["valid"] = False
    elif case == "run":
        status_overrides["run_id"] = "701"
    elif case == "issue":
        status_overrides["issue_key"] = "701"
    elif case == "sandbox":
        status_overrides["sandbox_path"] = str(tmp_path / "other")
    elif case == "mode":
        status_overrides["mode"] = "interactive"
    elif case == "branch":
        status_overrides["branch"] = "wrong"
    elif case == "base":
        status_overrides["pr_base"] = "develop"
    elif case == "draft":
        status_overrides["pr_draft"] = True
    elif case == "url_missing":
        status_overrides["pr_url"] = None
    elif case == "url_noncanonical":
        status_overrides["pr_url"] = "http://github.com/owner/repo/pull/42"
    elif case == "url_repository":
        status_overrides["pr_url"] = "https://github.com/other/repo/pull/42"
    if status_overrides:
        object.__setattr__(record, "status", parse_factory_status({**status.to_json(), **status_overrides}))

    validation = _completion_validation(sandbox)
    if case == "empty_diff":
        validation = _completion_validation(sandbox, files=[])
    elif case == "diff_unobserved":
        validation = _completion_validation(sandbox, diff_observed=False)
    elif case == "tests_missing":
        validation = replace(validation, evidence=replace(validation.evidence, tests=None))
    elif case == "tests_unobserved":
        validation = _completion_validation(
            sandbox,
            tests=TestResult("pytest -q", 0, "ok", observed=False),
        )
    elif case == "tests_skipped":
        validation = _completion_validation(
            sandbox,
            tests=TestResult("pytest -q", None, skipped_reason="disabled"),
        )
    elif case == "tests_red":
        validation = _completion_validation(
            sandbox,
            tests=TestResult("pytest -q", 1, "failed"),
        )
    elif case == "evidence_rejected":
        validation = _completion_validation(sandbox, review_ready=False)

    async def observed(**kwargs: object) -> EvidenceValidation:
        return validation

    monkeypatch.setattr(orchestrator, "observe_evidence", observed)
    with pytest.raises(Exception):
        asyncio.run(
            orchestrator._verify_factory_completion(
                home=tmp_path,
                issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
                record=record,
                test_command="pytest -q",
                started_at=datetime.now(UTC),
                runner=_completion_runner(sandbox, case=case),
            )
        )

    evidence_path = tmp_path / "state" / "worklink" / "evidence" / "700-1.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["failure_reason"]


def test_factory_completion_requires_entire_success_conjunction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    validation = _completion_validation(sandbox)

    async def observed(**kwargs: object) -> EvidenceValidation:
        return validation

    monkeypatch.setattr(orchestrator, "observe_evidence", observed)
    evidence_path, pr_url = asyncio.run(
        orchestrator._verify_factory_completion(
            home=tmp_path,
            issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
            record=_completion_record(sandbox),
            test_command="pytest -q",
            started_at=datetime.now(UTC),
            runner=_completion_runner(sandbox, case="success"),
        )
    )

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert pr_url == "https://github.com/owner/repo/pull/42"
    assert evidence["status"] == "completed"
    assert evidence["head_sha"] == "a" * 40


@pytest.mark.parametrize("pr_base", [None, "develop"])
def test_factory_completion_rejects_unbound_base_before_external_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pr_base: str | None,
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    record = _completion_record(sandbox)
    status = record.status
    assert status is not None
    object.__setattr__(record, "status", replace(status, pr_base=pr_base))
    external_calls: list[str] = []

    async def observed(**kwargs: object) -> EvidenceValidation:
        external_calls.append("evidence")
        return _completion_validation(sandbox)

    def runner(
        args: Sequence[str] | str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        external_calls.append("runner")
        return cp(args)

    monkeypatch.setattr(orchestrator, "observe_evidence", observed)
    with pytest.raises(orchestrator.WorklinkError, match="base mismatch"):
        asyncio.run(
            orchestrator._verify_factory_completion(
                home=tmp_path,
                issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
                record=record,
                test_command="pytest -q",
                started_at=datetime.now(UTC),
                runner=runner,
            )
        )

    assert external_calls == []


def test_factory_completion_failure_never_transitions_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    transitions: list[dict[str, object]] = []

    class Claims:
        def transition_issue(self, *args: object, **kwargs: object) -> None:
            transitions.append(kwargs)

    async def reject(**kwargs: object) -> tuple[Path, str]:
        raise orchestrator.WorklinkError("verification rejected")

    monkeypatch.setattr(orchestrator, "_verify_factory_completion", reject)
    with pytest.raises(orchestrator.WorklinkError, match="verification rejected"):
        asyncio.run(
            WorklinkRunner(home=tmp_path, repo=tmp_path)._finish_factory_070(
                issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
                claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
                claims=Claims(),
                backend=object(),
                compute=object(),
                factory_record=_completion_record(sandbox),
                test_cmd="pytest -q",
                runner=lambda args: cp(args),
                started_at=datetime.now(UTC),
            )
        )

    assert transitions == []


@pytest.mark.parametrize(
    ("lock", "dead_lock", "action"),
    [("absent", False, "claim"), ("stale", True, "steal"), ("fresh", False, None)],
)
def test_factory_recovery_uses_run_id_first_lock_resume_and_authoritative_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock: str,
    dead_lock: bool,
    action: str | None,
) -> None:
    _configure_opencode_oauth(tmp_path, monkeypatch)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    historical = {"reason": "opaque and nonauthoritative"}

    def status(value: str, lock_value: str, next_value: str | None = None):
        payload: dict[str, Any] = {
            "run_id": "700",
            "issue_key": "700",
            "valid": True,
            "sandbox_path": str(sandbox),
            "status": value,
            "mode": "autonomous",
            "branch": "epic/700",
            "pr_base": "main",
            "pr_draft": False,
            "lock": lock_value,
            "dead_lock": dead_lock if lock_value == lock else False,
            "lock_session": None if lock_value == "absent" else "session-1",
            "gates": {},
            "steps": ["implementation"],
            "slices": ["factory-070-migration"],
            "validator": None,
            "pr_url": None,
            "terminal_result": historical,
        }
        if next_value is not None:
            payload["next"] = next_value
        return parse_factory_status(payload)

    statuses = [status("needs-human", lock)]
    if action is not None:
        statuses.append(status("needs-human", "fresh"))
    statuses.extend([
        status("running", "fresh", "implementation"),
        status("needs-human", "fresh"),
    ])
    calls: list[tuple[str, ...]] = []

    class Backend:
        poll_interval_s = 0

        def status(self, run_id: str, *, sandbox: Path, launcher: str):
            calls.append(("status", run_id, str(sandbox), launcher))
            return statuses.pop(0)

        def lock(self, run_id: str, selected_action: str, **kwargs: Any) -> None:
            calls.append(("lock", run_id, selected_action, kwargs["session"], str(kwargs["sandbox"])))

        def resume(self, run_id: str, **kwargs: Any):
            calls.append(("resume", run_id, kwargs["session"], str(kwargs["sandbox"])))
            return self.status(
                run_id,
                sandbox=kwargs["sandbox"],
                launcher=kwargs["launcher"],
            )

        def heartbeat(self, run_id: str, **kwargs: Any) -> None:
            calls.append(("heartbeat", run_id, kwargs["session"], str(kwargs["sandbox"])))

        def work_spec(self, order: WorkOrder, **kwargs: Any) -> WorkSpec:
            return FeatureFactoryBackend(entrypoint="/opt/factory/bin/factory.js").work_spec(
                order, **kwargs
            )

    class Compute:
        def __init__(self) -> None:
            self.handle = LaunchHandle("local_subprocess", "123", 456)
            self.cancelled = False
            self.cleaned = False
            self.launches = 0

        async def launch(self, spec: WorkSpec) -> LaunchHandle:
            self.launches += 1
            return self.handle

        async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
            while not self.cancelled:
                await asyncio.sleep(0)
            return ComputeResult(-15, "", "cancelled", handle=handle)

        def job_alive(self, handle: LaunchHandle) -> bool:
            return True

        async def cancel(self, handle: LaunchHandle) -> None:
            self.cancelled = True

        async def cleanup(self, handle: LaunchHandle) -> None:
            self.cleaned = True

    def runner(args: Sequence[str] | str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[-2:] == ["rev-parse", "--show-toplevel"]:
            return cp(args, stdout=f"{sandbox}\n")
        if isinstance(args, list) and args[-2:] == ["rev-parse", "--absolute-git-dir"]:
            return cp(args, stdout=f"{sandbox / '.git'}\n")
        if isinstance(args, list) and args[-2:] == ["branch", "--show-current"]:
            return cp(args, stdout="epic/700\n")
        if isinstance(args, list) and args[-3:] == ["config", "--get", "remote.origin.url"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        if isinstance(args, list) and "rev-parse" in args:
            return cp(args, stdout="a" * 40 + "\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(tmp_path / "repo"), "config"]:
            return cp(args, stdout="git@github.com:owner/repo.git\n")
        return cp(args)

    retained = FactoryRunRecord(
        run_id="700",
        issue_id=700,
        attempt=1,
        repository="owner/repo",
        base_ref="main",
        branch="epic/700",
        launcher="/opt/factory/bin/factory.js",
        sandbox=str(sandbox),
        session="session-1",
        handle=LaunchHandle("local_subprocess", "99999999", 456),
        status=status("needs-human", lock),
        observed_at="2026-08-18T12:00:00+00:00",
        controller_phase="parked",
    )
    claim = ClaimRecord(700, 1, "agent", datetime.now(UTC))
    class Claims:
        agent_id = "agent"

        def _lock_still_held_by(self, record: ClaimRecord) -> bool:
            return record is claim

    compute = Compute()
    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=tmp_path / "repo", agent_id="agent")._recover_factory_070(
            issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
            claim_record=claim,
            claims=Claims(),
            backend=Backend(),
            compute=compute,
            retained=retained,
            launcher=Path(retained.launcher),
            repo_slug="owner/repo",
            base="main",
            test_cmd="uv run pytest -q",
            runner=runner,
        )
    )
    assert result.status == "needs-human"
    expected = [("status", "700", str(sandbox), retained.launcher)]
    if action is not None:
        expected.extend([
            ("lock", "700", action, "session-1", str(sandbox)),
            ("status", "700", str(sandbox), retained.launcher),
        ])
    expected.extend([
        ("resume", "700", "session-1", str(sandbox)),
        ("status", "700", str(sandbox), retained.launcher),
    ])
    assert calls[:len(expected)] == expected
    assert calls[-1] == ("heartbeat", "700", "session-1", str(sandbox))
    assert compute.cancelled and compute.cleaned
    assert compute.launches == 1


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("fresh_foreign", ["status"]),
        ("changed_owner", ["status", "lock:claim", "status"]),
        ("changed_history", ["status", "lock:claim", "status"]),
        ("missing_next", ["status", "resume", "status"]),
        ("live_process", ["status"]),
    ],
)
def test_factory_recovery_rejection_command_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected: list[str],
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    history = {"reason": "opaque"}

    def status(
        *,
        lock: str,
        owner: str | None,
        current: str = "needs-human",
        next_present: bool = True,
        changed_history: bool = False,
    ) -> Any:
        payload = {
            **_factory_lifecycle_status(
                sandbox,
                status=current,
                lock=lock,
                session=owner,
            ).to_json(),
            "terminal_result": {"reason": "changed"} if changed_history else history,
        }
        if not next_present:
            payload.pop("next", None)
        return parse_factory_status(payload)

    if case == "fresh_foreign":
        statuses = [status(lock="fresh", owner="foreign")]
    elif case == "changed_owner":
        statuses = [
            status(lock="absent", owner=None),
            status(lock="fresh", owner="foreign"),
        ]
    elif case == "changed_history":
        statuses = [
            status(lock="absent", owner=None),
            status(lock="fresh", owner="session-1", changed_history=True),
        ]
    elif case == "missing_next":
        statuses = [
            status(lock="fresh", owner="session-1"),
            status(
                lock="fresh",
                owner="session-1",
                current="running",
                next_present=False,
            ),
        ]
    else:
        statuses = [status(lock="fresh", owner="session-1")]
    events: list[str] = []

    class Backend:
        poll_interval_s = 0

        def status(self, *args: object, **kwargs: object) -> Any:
            events.append("status")
            return statuses.pop(0)

        def lock(self, run_id: str, action: str, **kwargs: object) -> None:
            events.append(f"lock:{action}")

        def resume(self, run_id: str, **kwargs: object) -> Any:
            events.append("resume")
            return self.status(run_id, **kwargs)

    class Compute:
        async def launch(self, spec: WorkSpec) -> LaunchHandle:
            raise AssertionError("rejected recovery launched a duplicate process")

    retained = _factory_lifecycle_record(
        sandbox,
        LaunchHandle("local_subprocess", "999999999", 1),
    )
    retained = replace(
        retained,
        status=status(lock="fresh", owner="session-1"),
        session="session-1",
        observed_at="2026-08-18T12:00:00+00:00",
        controller_phase="parked",
    )
    monkeypatch.setattr(
        orchestrator,
        "_verify_factory_recovery_binding",
        lambda **kwargs: sandbox,
    )
    monkeypatch.setattr(
        orchestrator,
        "factory_process_is_alive",
        lambda record: case == "live_process",
    )
    monkeypatch.setattr(
        orchestrator,
        "factory_process_is_verified_dead",
        lambda record: case != "live_process",
    )

    with pytest.raises(Exception):
        asyncio.run(
            WorklinkRunner(home=tmp_path, repo=tmp_path)._recover_factory_070(
                issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
                claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
                claims=object(),
                backend=Backend(),
                compute=Compute(),
                retained=retained,
                launcher=Path(retained.launcher),
                repo_slug="owner/repo",
                base="main",
                test_cmd="pytest -q",
                runner=lambda args: cp(args),
            )
        )

    assert events == expected


def test_factory_terminal_recovery_cancels_retained_live_process_without_lock_or_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    terminal = parse_factory_status(
        {
            **_factory_lifecycle_status(
                sandbox,
                status="blocked",
                lock="absent",
                session=None,
            ).to_json(),
            "terminal_result": {"reason": "opaque"},
        }
    )
    handle = LaunchHandle("local_subprocess", "123", 456)
    retained = replace(
        _factory_lifecycle_record(sandbox, handle),
        status=terminal,
        observed_at="2026-08-18T12:00:00+00:00",
        controller_phase="terminal",
    )
    events: list[str] = []

    class Backend:
        def status(self, *args: object, **kwargs: object) -> Any:
            events.append("status")
            return terminal

        def lock(self, *args: object, **kwargs: object) -> None:
            events.append("lock")

        def resume(self, *args: object, **kwargs: object) -> Any:
            events.append("resume")
            return terminal

    class Compute:
        async def cancel(self, selected: LaunchHandle) -> None:
            events.append("cancel")

        async def cleanup(self, selected: LaunchHandle) -> None:
            events.append("cleanup")

    class Claims:
        def transition_issue(self, *args: object, **kwargs: object) -> None:
            events.append("transition")

    monkeypatch.setattr(
        orchestrator,
        "_verify_factory_recovery_binding",
        lambda **kwargs: sandbox,
    )
    monkeypatch.setattr(orchestrator, "factory_process_is_alive", lambda record: True)
    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=tmp_path)._recover_factory_070(
            issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
            claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
            claims=Claims(),
            backend=Backend(),
            compute=Compute(),
            retained=retained,
            launcher=Path(retained.launcher),
            repo_slug="owner/repo",
            base="main",
            test_cmd="pytest -q",
            runner=lambda args: cp(args),
        )
    )

    assert result.status == "blocked"
    assert events == ["status", "cancel", "cleanup", "transition"]


@pytest.mark.parametrize(
    "case",
    [
        "repository",
        "base",
        "launcher",
        "lifecycle",
        "session",
        "claim_issue",
        "claim_owner",
        "claim_missing",
        "sandbox",
        "checkout_branch",
        "checkout_repository",
    ],
)
def test_factory_recovery_binding_rejects_every_precontrol_mismatch(
    tmp_path: Path, case: str
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    retained = _factory_lifecycle_record(
        sandbox,
        LaunchHandle("local_subprocess", "999999999", 1),
    )
    replacements: dict[str, object] = {}
    if case == "repository":
        replacements["repository"] = "other/repo"
    elif case == "base":
        replacements["base_ref"] = "develop"
    elif case == "launcher":
        replacements["launcher"] = "/opt/other/factory.js"
    elif case == "lifecycle":
        replacements["controller_phase"] = "stopped"
    elif case == "session":
        replacements["session"] = None
    elif case == "sandbox":
        replacements["sandbox"] = str(tmp_path / "missing")
    retained = replace(retained, **replacements)
    claim = ClaimRecord(
        701 if case == "claim_issue" else 700,
        1,
        "foreign" if case == "claim_owner" else "agent",
        datetime.now(UTC),
    )

    class Claims:
        agent_id = "agent"

        def _lock_still_held_by(self, record: ClaimRecord) -> bool:
            return case != "claim_missing"

    def runner(args: Sequence[str] | str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[-2:] == ["rev-parse", "--show-toplevel"]:
            return cp(args, stdout=f"{sandbox}\n")
        if isinstance(args, list) and args[-2:] == ["rev-parse", "--absolute-git-dir"]:
            return cp(args, stdout=f"{sandbox / '.git'}\n")
        if isinstance(args, list) and args[-3:] == ["config", "--get", "remote.origin.url"]:
            remote = "other/repo" if case == "checkout_repository" else "owner/repo"
            return cp(args, stdout=f"git@github.com:{remote}.git\n")
        if isinstance(args, list) and args[-2:] == ["branch", "--show-current"]:
            branch = "wrong" if case == "checkout_branch" else "epic/700"
            return cp(args, stdout=branch + "\n")
        if isinstance(args, list) and "rev-parse" in args:
            return cp(args, stdout="a" * 40 + "\n")
        return cp(args)

    with pytest.raises(orchestrator.WorklinkError):
        orchestrator._verify_factory_recovery_binding(
            runner=WorklinkRunner(home=tmp_path, repo=tmp_path, agent_id="agent"),
            issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
            claim_record=claim,
            claims=Claims(),
            retained=retained,
            launcher=Path("/opt/factory/bin/factory.js"),
            repo_slug="owner/repo",
            base="main",
            command_runner=runner,
        )


def _commit_runner(
    staged_files: dict[str, bytes],
    committed: dict,
    *,
    list_returncode: int = 0,
    unreadable_path: str | None = None,
    text_blob_path: str | None = None,
) -> object:
    """Fake runner for the commit path with controllable staged index blobs."""
    def runner(
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        tail = list(args)[3:]
        if tail[:2] == ["add", "-A"]:
            return cp(args)
        if tail == ["diff", "--cached", "--quiet"]:
            return cp(args, returncode=1)  # something is staged
        if tail == [
            "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRTUXB"
        ]:
            paths = b"\0".join(os.fsencode(path) for path in staged_files) + b"\0"
            return cp(args, returncode=list_returncode, stdout=paths)
        if tail[:2] == ["cat-file", "blob"]:
            path = tail[2][1:]
            if path == unreadable_path:
                return cp(args, returncode=128, stdout=b"", stderr=b"missing blob")
            blob = staged_files[path]
            if path == text_blob_path:
                return cp(args, stdout=blob.decode("ascii"))
            return cp(args, stdout=blob)
        if tail[:1] == ["commit"]:
            committed["ran"] = True
            return cp(args, stdout="[issue/441-a1 abc123] worklink\n")
        return cp(args)
    return runner


def _real_git_runner(
    args: Sequence[str], *, text: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=text, check=False)


def _init_commit_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True
    )


def _commit_seed(repo: Path, files: dict[str, bytes]) -> None:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)


def _stage_file(repo: Path, name: str, content: bytes) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    subprocess.run(["git", "-C", str(repo), "add", "--", name], check=True)


def test_staged_secret_guard_allows_match_already_in_base(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _assert_staged_diff_has_no_secret

    repo = tmp_path / "repo"
    _init_commit_repo(repo)
    secret = b"ghp_" + b"A" * 36
    _commit_seed(repo, {"fixture.txt": b"credential=" + secret + b"\nold\n"})
    _stage_file(repo, "fixture.txt", b"credential=" + secret + b"\nnew\n")

    _assert_staged_diff_has_no_secret(repo, runner=_real_git_runner)


def test_staged_secret_guard_allows_reusing_base_match(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _assert_staged_diff_has_no_secret

    repo = tmp_path / "repo"
    _init_commit_repo(repo)
    secret = b"ghp_" + b"A" * 36
    _commit_seed(repo, {"fixture.txt": secret + b"\n"})
    _stage_file(repo, "fixture.txt", secret + b"\ncopy=" + secret + b"\n")

    _assert_staged_diff_has_no_secret(repo, runner=_real_git_runner)


def test_staged_secret_guard_refuses_new_match_without_echoing_it(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import WorklinkError, _assert_staged_diff_has_no_secret

    repo = tmp_path / "repo"
    _init_commit_repo(repo)
    existing = b"ghp_" + b"A" * 36
    introduced = b"ghp_" + b"B" * 36
    _commit_seed(repo, {"fixture.txt": existing + b"\n"})
    _stage_file(repo, "fixture.txt", existing + b"\ncredential=" + introduced + b"\n")

    with pytest.raises(WorklinkError, match="fixture.txt.*secret-shaped") as raised:
        _assert_staged_diff_has_no_secret(repo, runner=_real_git_runner)

    assert introduced.decode() not in str(raised.value)
    assert "credential=" not in str(raised.value)


def test_staged_secret_guard_refuses_secret_in_added_file(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import WorklinkError, _assert_staged_diff_has_no_secret

    repo = tmp_path / "repo"
    _init_commit_repo(repo)
    _commit_seed(repo, {"tracked.txt": b"clean\n"})
    secret = b"ghp_" + b"A" * 36
    _stage_file(repo, "added.txt", secret + b"\n")

    with pytest.raises(WorklinkError, match="added.txt.*secret-shaped"):
        _assert_staged_diff_has_no_secret(repo, runner=_real_git_runner)


def test_staged_secret_guard_scans_binary_blob_marked_no_diff(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _assert_staged_diff_has_no_secret

    repo = tmp_path / "repo"
    _init_commit_repo(repo)
    secret = b"ghp_" + b"A" * 36
    _commit_seed(
        repo,
        {
            ".gitattributes": b"fixture.bin -diff\n",
            "fixture.bin": b"\x00\xff" + secret + b"\nold\n",
        },
    )
    _stage_file(repo, "fixture.bin", b"\x00\xff" + secret + b"\nnew\n")

    _assert_staged_diff_has_no_secret(repo, runner=_real_git_runner)


@pytest.mark.parametrize("failure", ["unreadable", "non-byte"])
def test_staged_secret_guard_fails_closed_for_base_blob(failure: str) -> None:
    from mimir.worklink.orchestrator import WorklinkError, _assert_staged_diff_has_no_secret

    secret = b"ghp_" + b"A" * 36

    def runner(
        args: Sequence[str], *, text: bool = True
    ) -> subprocess.CompletedProcess:
        tail = list(args)[3:]
        if tail[:2] == ["diff", "--cached"]:
            return cp(args, stdout=b"fixture.txt\0")
        if tail == ["cat-file", "blob", ":fixture.txt"]:
            return cp(args, stdout=secret)
        if tail == ["cat-file", "blob", "HEAD:fixture.txt"]:
            if failure == "non-byte":
                return cp(args, stdout=secret.decode())
            return cp(args, returncode=128, stdout=b"")
        if tail == ["rev-parse", "--verify", "HEAD"]:
            return cp(args, stdout=b"a" * 40 + b"\n")
        if tail == ["ls-tree", "-z", "HEAD", "--", "fixture.txt"]:
            return cp(args, stdout=b"100644 blob abc\tfixture.txt\0")
        raise AssertionError(args)

    with pytest.raises(WorklinkError, match="base Worklink path 'fixture.txt'"):
        _assert_staged_diff_has_no_secret(Path("/tmp/wt"), runner=runner)


@pytest.mark.parametrize("case", ["plain", "nul", "attributes"])
def test_commit_checkout_changes_refuses_secret_in_staged_blob(
    tmp_path: Path, case: str
) -> None:
    from mimir.worklink.orchestrator import WorklinkError, _commit_checkout_changes

    repo = tmp_path / "repo"
    _init_commit_repo(repo)
    issue = IssueContext(441, "worklink slice", "do it", {"worklink"})
    secret = b"ghp_" + b"B" * 36
    path = repo / f"{case}.txt"
    content = b'API_TOKEN = "' + secret + b'"\n'
    if case == "nul":
        content = b"\x00" + content
    if case == "attributes":
        (repo / ".gitattributes").write_text(f"{path.name} -diff\n")
    path.write_bytes(content)

    with pytest.raises(WorklinkError, match=rf"{case}\.txt.*secret-shaped"):
        _commit_checkout_changes(repo, issue, runner=_real_git_runner)
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        check=False,
    ).returncode != 0


def test_commit_checkout_changes_fails_closed_when_scan_command_fails() -> None:
    # A security gate must not silently pass when it cannot verify: a non-zero
    # `git diff --cached -U0` (bad index/config/permissions) must refuse the
    # commit, not accept empty stdout as "clean".
    from mimir.worklink.orchestrator import WorklinkError, _commit_checkout_changes

    issue = IssueContext(441, "worklink slice", "do it", {"worklink"})
    committed = {"ran": False}
    runner = _commit_runner({}, committed, list_returncode=128)

    with pytest.raises(WorklinkError, match="cannot scan"):
        _commit_checkout_changes(Path("/tmp/wt"), issue, runner=runner)
    assert committed["ran"] is False


def test_commit_checkout_changes_allows_benign_low_signal_token() -> None:
    # The scan uses high-signal, length-floored patterns (mirroring the
    # pre-commit hook), NOT the broad log redactor — so a benign placeholder
    # `token=` in generated content must not block the commit.
    from mimir.worklink.orchestrator import _commit_checkout_changes

    issue = IssueContext(441, "worklink slice", "do it", {"worklink"})
    committed = {"ran": False}
    runner = _commit_runner(
        {"docs/example.md": b"Set the header: token=YOUR_TOKEN_HERE\n"}, committed
    )

    _commit_checkout_changes(Path("/tmp/wt"), issue, runner=runner)
    assert committed["ran"] is True


def test_commit_checkout_changes_commits_clean_diff() -> None:
    from mimir.worklink.orchestrator import _commit_checkout_changes

    issue = IssueContext(441, "worklink slice", "do it", {"worklink"})
    committed = {"ran": False}
    runner = _commit_runner({"app.py": b"def hello():\n    return 42\n"}, committed)

    _commit_checkout_changes(Path("/tmp/wt"), issue, runner=runner)
    assert committed["ran"] is True


def test_commit_checkout_changes_names_unreadable_staged_path() -> None:
    from mimir.worklink.orchestrator import WorklinkError, _commit_checkout_changes

    committed = {"ran": False}
    runner = _commit_runner(
        {"unreadable.dat": b"content"}, committed, unreadable_path="unreadable.dat"
    )

    with pytest.raises(WorklinkError, match="unreadable\\.dat"):
        _commit_checkout_changes(
            Path("/tmp/wt"),
            IssueContext(441, "worklink slice", "do it", {"worklink"}),
            runner=runner,
        )
    assert committed["ran"] is False


def test_commit_checkout_changes_names_staged_path_with_invalid_blob_output() -> None:
    from mimir.worklink.orchestrator import WorklinkError, _commit_checkout_changes

    committed = {"ran": False}
    runner = _commit_runner(
        {"undecodable.dat": b"content"}, committed, text_blob_path="undecodable.dat"
    )

    with pytest.raises(WorklinkError, match="undecodable\\.dat"):
        _commit_checkout_changes(
            Path("/tmp/wt"),
            IssueContext(441, "worklink slice", "do it", {"worklink"}),
            runner=runner,
        )
    assert committed["ran"] is False


def test_commit_checkout_changes_commits_clean_binary_blob(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _commit_checkout_changes

    repo = tmp_path / "repo"
    _init_commit_repo(repo)
    # PNG signature plus invalid UTF-8 demonstrates that scanning is byte-safe.
    (repo / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xffclean binary\x00")

    _commit_checkout_changes(
        repo,
        IssueContext(441, "worklink slice", "do it", {"worklink"}),
        runner=_real_git_runner,
    )

    assert subprocess.run(
        ["git", "-C", str(repo), "show", "--format=", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == "image.png"


def test_authorized_startup_snapshot_uses_publication_only(tmp_path: Path) -> None:
    import mimir.worklink.orchestrator as orchestrator

    calls = []

    class Publication:
        def run(self, *args):
            calls.append(args)
            if args[0] == "rev-parse":
                return cp(args, stdout="abc123\n")
            return cp(args, stdout="?? changed.txt\n")

    def forbidden_runner(args):
        raise AssertionError(f"controller Git fallback used: {args}")

    result = orchestrator._checkout_snapshot(
        tmp_path,
        runner=forbidden_runner,
        publication=Publication(),
    )

    assert result == ("abc123", "?? changed.txt\n")
    assert calls == [
        ("rev-parse", "HEAD"),
        ("status", "--porcelain=v1", "--untracked-files=all"),
    ]


def test_authorized_publication_helpers_never_use_checkout_runner(tmp_path: Path) -> None:
    import mimir.worklink.orchestrator as orchestrator

    calls = []

    class Publication:
        def run(self, *args, **kwargs):
            calls.append(args)
            if args == ("diff", "--cached", "--quiet"):
                return cp(args, returncode=1)
            if args == (
                "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRTUXB"
            ):
                return cp(args, stdout=b"changed.txt\0")
            if args[:2] == ("cat-file", "blob"):
                return cp(args, stdout=b"clean content\n")
            if args == ("rev-parse", "HEAD"):
                return cp(args, stdout="a" * 40 + "\n")
            return cp(args)

        def push(self):
            calls.append(("push",))
            return cp(["git", "push"])

    def forbidden_runner(args):
        raise AssertionError(f"controller Git fallback used: {args}")

    publication = Publication()
    issue = IssueContext(1, "title", "body", set())
    orchestrator._commit_checkout_changes(
        tmp_path,
        issue,
        runner=forbidden_runner,
        publication=publication,
    )
    orchestrator._ensure_clean_checkout(
        tmp_path,
        runner=forbidden_runner,
        publication=publication,
    )
    orchestrator._git_push(
        tmp_path,
        "issue/1-a1",
        runner=forbidden_runner,
        publication=publication,
    )
    validation = EvidenceValidation(
        "completed",
        True,
        (),
        WorklinkEvidence(
            1,
            1,
            "opencode",
            "issue/1-a1",
            str(tmp_path),
            "start",
            "finish",
            ["changed.txt"],
            "stat",
            [],
            None,
            None,
            "completed",
        ),
    )
    updated = orchestrator._with_head_sha(
        validation,
        tmp_path,
        runner=forbidden_runner,
        publication=publication,
    )

    assert updated.evidence.head_sha == "a" * 40
    assert ("add", "-A") in calls
    assert ("commit", "-m", "worklink: issue #1") in calls
    assert ("push",) in calls


def test_authorized_publication_ignores_hostile_checkout_git_metadata(tmp_path: Path) -> None:
    from mimir.worklink.safe_git import ControllerGitPublication
    import mimir.worklink.orchestrator as orchestrator

    trusted = tmp_path / "trusted"
    checkout = tmp_path / "checkout"
    remote = tmp_path / "remote.git"
    metadata = tmp_path / "metadata"
    subprocess.run(["git", "init", "-q", "-b", "main", str(trusted)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "-C", str(trusted), "config", "user.name", "controller"], check=True)
    subprocess.run(["git", "-C", str(trusted), "config", "user.email", "controller@example.com"], check=True)
    subprocess.run(["git", "-C", str(trusted), "remote", "add", "origin", str(remote)], check=True)
    (trusted / "tracked.txt").write_text("old\n")
    subprocess.run(["git", "-C", str(trusted), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(trusted), "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(["git", "clone", "-q", str(trusted), str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "-b", "issue/1-a1"], check=True)
    fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)
    try:
        publication = ControllerGitPublication.capture(
            fd,
            trusted,
            "issue/1-a1",
            metadata,
        )
    finally:
        os.close(fd)
    marker = tmp_path / "executed"
    hooks = checkout / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    included = checkout / "hostile.config"
    included.write_text(f"[core]\n\tfsmonitor = touch {marker}\n")
    subprocess.run(["git", "-C", str(checkout), "config", "core.hooksPath", str(hooks)], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "include.path", str(included)], check=True)
    (checkout / "tracked.txt").write_text("new\n")

    try:
        orchestrator._commit_checkout_changes(
            checkout,
            IssueContext(1, "title", "body", set()),
            runner=lambda args: (_ for _ in ()).throw(AssertionError(args)),
            publication=publication,
        )
        orchestrator._ensure_clean_checkout(
            checkout,
            runner=lambda args: (_ for _ in ()).throw(AssertionError(args)),
            publication=publication,
        )
    finally:
        publication.close()

    assert not marker.exists()


@pytest.mark.parametrize(
    ("scenario", "delete_checkout"),
    [
        ("success", True),
        ("blocked", False),
        ("failed", False),
        ("pre_launch_exception", False),
        ("work_spec_exception", False),
        ("evidence_exception", False),
        ("commit_push_pr_exception", False),
        ("publication_exception", False),
    ],
)
def test_worker_capabilities_close_in_order_and_retain_non_success(
    tmp_path: Path,
    scenario: str,
    delete_checkout: bool,
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    boundary = tmp_path / scenario
    checkout = boundary / "checkout"
    checkout.mkdir(parents=True)
    (checkout / "output.txt").write_text("worker output\n")
    closed = []

    class Publication:
        def close(self):
            closed.append("publication")
            if scenario == "publication_exception":
                raise RuntimeError("publication close failed")

    class Authorization:
        def close(self):
            closed.append("authorization")

    if scenario == "publication_exception":
        with pytest.raises(RuntimeError, match="publication close failed"):
            orchestrator._close_attempt_capabilities(
                Publication(), Authorization(), checkout, delete_checkout=delete_checkout
            )
    else:
        orchestrator._close_attempt_capabilities(
            Publication(), Authorization(), checkout, delete_checkout=delete_checkout
        )

    assert closed == ["publication", "authorization"]
    assert checkout.exists() is (not delete_checkout)
    assert boundary.exists() is (not delete_checkout)
    if not delete_checkout:
        assert (checkout / "output.txt").read_text() == "worker output\n"


def test_worker_capability_cleanup_tolerates_entry_removed_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.orchestrator as orchestrator

    boundary = tmp_path / "attempt"
    checkout = boundary / "checkout"
    checkout.mkdir(parents=True)
    victim = checkout / "maintenance.lock"
    victim.write_text("lock\n")
    real_unlink = os.unlink
    raced = False

    def unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        nonlocal raced
        if not raced and os.fsdecode(path) == victim.name:
            raced = True
            real_unlink(path, dir_fd=dir_fd)
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(orchestrator.os, "unlink", unlink)
    orchestrator._close_attempt_capabilities(None, None, checkout, delete_checkout=True)

    assert raced
    assert not boundary.exists()


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "blocked",
        "failed",
        "work_spec_exception",
        "pre_launch_exception",
        "evidence_exception",
        "commit_exception",
        "push_exception",
        "pr_exception",
        "publication_exception",
        "branch_cleanup_exception",
    ],
)
def test_authorized_runner_closes_real_attempt_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    import mimir.worklink.orchestrator as orchestrator
    from mimir.worklink.compute import (
        ComputeLaunchError,
        LocalSubprocessComputeBackend,
        _enabled_child_env,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    checkout = tmp_path / ("a" * 64) / "1410-1" / "checkout"
    checkout.mkdir(parents=True)
    (checkout / ".git" / "objects").mkdir(parents=True)
    lifecycle = []
    checkout_kwargs = {}
    bound_specs = []
    persisted_gate_handles = []

    class Authorization:
        def __init__(self):
            self.closed = 0
            self.fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)

        def duplicate_fd(self):
            assert self.closed == 0
            return os.dup(self.fd)

        def close(self):
            self.closed += 1
            lifecycle.append("authorization")
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1

    authorization = Authorization()

    class Publication:
        def __init__(self):
            self.closed = 0
            self.calls = []
            self.commit_seen = False

        def run(self, *args, check=False, text=True):
            assert self.closed == 0
            self.calls.append(args)
            if scenario == "publication_exception" and args[:2] == ("diff", "--name-only"):
                raise RuntimeError("publication failed")
            if args == (
                "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRTUXB"
            ):
                return cp(args, stdout=b"changed.txt\0")
            if args[:2] == ("cat-file", "blob"):
                return cp(args, stdout=b"clean content\n")
            if args[:2] == ("diff", "--name-only"):
                return cp(args, stdout="changed.txt\n")
            if args[:2] == ("diff", "--stat"):
                return cp(args, stdout=" changed.txt | 1 +\n")
            if args[0] == "status":
                return cp(args, stdout="" if self.commit_seen else "?? changed.txt\n")
            if args == ("diff", "--cached", "--quiet"):
                return cp(args, returncode=1)
            if args[0] == "commit":
                if scenario == "commit_exception":
                    return cp(args, returncode=1, stderr="commit failed")
                self.commit_seen = True
                return cp(args)
            if args == ("rev-parse", "HEAD"):
                return cp(args, stdout="a" * 40 + "\n")
            if (
                args[:2] == ("update-ref", "-d")
                and scenario == "branch_cleanup_exception"
            ):
                raise RuntimeError("branch cleanup failed")
            return cp(args)

        def push(self):
            assert self.closed == 0
            self.calls.append(("push",))
            if scenario == "push_exception":
                return cp(["git", "push"], returncode=1, stderr="push failed")
            return cp(["git", "push"])

        def close(self):
            self.closed += 1
            lifecycle.append("publication")

    publication = Publication()

    class BoundCompute:
        name = "local_subprocess"

        def capabilities(self):
            return ComputeCaps(True, False, True, False)

        async def launch(self, spec):
            bound_specs.append(spec)
            if scenario == "pre_launch_exception" and len(bound_specs) == 1:
                raise ComputeLaunchError("launch failed")
            identifiers = (
                "123e4567-e89b-42d3-a456-426614174001",
                "123e4567-e89b-42d3-a456-426614174002",
                "123e4567-e89b-42d3-a456-426614174003",
            )
            index = len(bound_specs) - 1
            _enabled_child_env(spec, identifiers[index])
            return LaunchHandle(
                "local_subprocess",
                identifiers[index],
                process_start_ticks=100 + index,
                shim_pid=200 + index,
            )

        async def wait(self, handle, timeout_s):
            if len(bound_specs) > 1:
                from mimir.worklink.run_state import load_run_state

                state = load_run_state(tmp_path, 1410)
                assert state is not None
                persisted_gate_handles.append(
                    (state.handle_identifier, state.shim_pid, state.process_start_ticks)
                )
            return ComputeResult(0, "build ok", "", handle=handle)

        async def cleanup(self, handle):
            return None

        async def cancel(self, handle):
            return None

    bound = BoundCompute()

    class WorkerBackend(FakeBackend):
        name = "opencode"

        def work_spec(self, order, **kwargs):
            if scenario == "work_spec_exception":
                raise RuntimeError("work spec failed")
            return WorkSpec(
                order.issue_id,
                kwargs["attempt"],
                kwargs["repo_url"],
                kwargs["base_ref"],
                kwargs["branch"],
                order.prompt,
                order.rules,
                kwargs["test_command"],
                self.name,
                order.timeout_s,
                env={"OPENCODE_PERMISSION": '{"edit":"allow"}'},
                backend_config={"pass_env": ()},
                local_checkout=order.checkout,
                local_argv=("opencode", "run"),
            )

        async def invoke_with_startup_retry(self, invoke, **kwargs):
            return await invoke()

        async def interpret(self, order, result):
            (order.checkout / "changed.txt").write_text("generated\n")
            if scenario == "blocked":
                return RawResult(
                    0,
                    order.transcript_root / "run.json",
                    "blocked",
                    None,
                    blocked_reason="design conflict",
                )
            if scenario in {"failed", "pre_launch_exception"}:
                return RawResult(
                    1, order.transcript_root / "run.json", "failed", "build failed"
                )
            return RawResult(0, order.transcript_root / "run.json", "success", None)

    backend = WorkerBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    def create_checkout(_repo, **kwargs):
        checkout_kwargs.update(kwargs)
        return CheckoutLease(
            1410,
            1,
            repo,
            checkout,
            "issue/1410-a1",
            "main",
            local_base="base-sha",
            isolated_checkout=True,
            worker_authorized=True,
            authorization=authorization,
        )

    def capture(*args, **kwargs):
        lifecycle.append("publication-acquired")
        return publication

    def bind(cls, auth, **kwargs):
        assert auth is authorization
        lifecycle.append("authorization-bound")
        return bound

    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "1410"]:
            return cp(args, stdout=ISSUE_JSON.replace('"id": 441', '"id": 1410'))
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:3] == ["gh", "pr", "create"]:
            if scenario == "pr_exception":
                return cp(args, returncode=1, stderr="PR failed")
            return cp(args, stdout="https://github.com/example/repo/pull/1\n")
        return cp(args)

    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")
    monkeypatch.setattr(orchestrator, "OpenCodeBackend", WorkerBackend)
    monkeypatch.setattr(orchestrator, "create_isolated_checkout", create_checkout)
    monkeypatch.setattr(orchestrator.ControllerGitPublication, "capture", capture)
    monkeypatch.setattr(LocalSubprocessComputeBackend, "for_authorized_checkout", classmethod(bind))
    if scenario == "evidence_exception":
        async def fail_evidence(**kwargs):
            raise RuntimeError("evidence failed")
        monkeypatch.setattr(orchestrator, "observe_evidence", fail_evidence)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            1410, backend_name="opencode", test_command="pytest -q"
        )
    )

    assert checkout_kwargs["worker_eligible"] is True
    assert lifecycle.count("publication-acquired") == 1
    assert lifecycle.count("authorization-bound") == 1
    assert lifecycle[-2:] == ["publication", "authorization"]
    assert publication.closed == 1
    assert authorization.closed == 1
    expected_published = scenario in {"success", "branch_cleanup_exception"}
    assert result.status == (
        "completed" if expected_published else ("blocked" if scenario == "blocked" else "failed")
    )
    assert checkout.exists() is (not expected_published)
    expected_launches = {
        "work_spec_exception": 0,
        "pre_launch_exception": 1,
        "failed": 1,
        "evidence_exception": 1,
        "publication_exception": 1,
        "blocked": 2,
        "commit_exception": 2,
        "success": 3,
        "branch_cleanup_exception": 3,
        "push_exception": 3,
        "pr_exception": 3,
    }
    assert len(bound_specs) == expected_launches[scenario]
    if bound_specs:
        assert "PYTEST_ADDOPTS" in bound_specs[0].env
        assert bound_specs[0].backend_config["pass_env"] == ("PYTEST_ADDOPTS",)
    if expected_published:
        assert bound_specs[0].local_argv == ("opencode", "run")
        assert all(
            spec.local_argv == ("/bin/sh", "-c", "pytest -q")
            for spec in bound_specs[1:]
        )
        assert persisted_gate_handles == [
            ("123e4567-e89b-42d3-a456-426614174002", 201, 101),
            ("123e4567-e89b-42d3-a456-426614174003", 202, 102),
        ]
        assert ("push",) in publication.calls
    if scenario == "branch_cleanup_exception":
        assert result.review_ready is True
        assert result.pr_url == "https://github.com/example/repo/pull/1"
        assert result.reason == (
            "post-publication bookkeeping failed: "
            "authorized branch cleanup: branch cleanup failed"
        )
        assert ["chainlink", "issue", "label", "1410", "worklink:review"] in calls
        assert ["chainlink", "issue", "label", "1410", "worklink:failed"] not in calls
        assert ["chainlink", "issue", "label", "1410", "worklink:ready"] not in calls
