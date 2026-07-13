from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

import pytest
import asyncio

from mimir.event_logger import _reset_logger_for_tests, init_logger
from mimir.worklink.backends import Caps, ComputeCaps, ComputeResult, RawResult, WorkOrder
from mimir.worklink.evidence import EvidenceValidation, WorklinkEvidence
from mimir.worklink.backends.registry import BackendRegistry, WorklinkConfig, WorklinkDefaults
from mimir.worklink.claims import ChainlinkClaims, ClaimRecord, claim_records_from_comments
from mimir.worklink.compute import WorkSpec
from mimir.worklink.worktree import WorktreeLease
from mimir.worklink.orchestrator import (
    IssueContext,
    LeafValidationError,
    WorklinkRunner,
    _demote_template_invalid_ready_leaf,
    render_decomposition_prompt,
    validate_leaf,
)


class FakeCompute:
    name = "fake_compute"

    def __init__(self, *, shared_filesystem: bool = False) -> None:
        self.shared_filesystem = shared_filesystem
        self.specs: list[WorkSpec] = []
        self.cleaned: list[WorkSpec] = []

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(self.shared_filesystem, False, True, False)

    async def launch(self, spec: WorkSpec) -> WorkSpec:
        self.specs.append(spec)
        return spec

    async def wait(self, handle: WorkSpec, timeout_s: int) -> ComputeResult:
        return ComputeResult(exit_code=0, stdout="ok", stderr="")

    async def logs(self, handle: WorkSpec) -> str:
        return ""

    async def cancel(self, handle: WorkSpec) -> None:
        return None

    async def cleanup(self, handle: WorkSpec) -> None:
        self.cleaned.append(handle)


class SlowTestCompute(FakeCompute):
    async def wait(self, handle: WorkSpec, timeout_s: int) -> ComputeResult:
        await asyncio.sleep(0)
        return ComputeResult(exit_code=0, stdout="tests ok", stderr="")


class FakeBackend:
    name = "fake"

    def __init__(self, status: str = "success", *, write_change: bool = True) -> None:
        self.status = status
        self.write_change = write_change
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
            local_worktree=order.worktree,
        )

    async def interpret(self, order: WorkOrder, result: object) -> RawResult:
        self.orders.append(order)
        if self.write_change:
            (order.worktree / "changed.txt").write_text("hello\n", encoding="utf-8")
        return RawResult(
            0 if self.status == "success" else 1,
            order.transcript_root / "fake.json",
            self.status,
            None,
        )





def test_orchestrator_passes_configured_compute_backend_to_tool_backend(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    compute = FakeCompute(shared_filesystem=True)
    calls: list[Sequence[str] | str] = []

    def runner(
        args: Sequence[str] | str, **_: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
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
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True)
            return cp(args)
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
                local_worktree=order.worktree,
            )

        async def interpret(self, order: WorkOrder, result: object) -> RawResult:
            self.orders.append(order)
            assert isinstance(result, ComputeResult)
            (order.worktree / "changed.txt").write_text(result.stdout + "\n", encoding="utf-8")
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
    assert compute.specs[0].local_worktree == worktree
    assert compute.specs[0].env["MIMIR_HOME"] == str(tmp_path)
    assert compute.cleaned == [compute.specs[0]]


def cp(
    args: Sequence[str] | str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


ISSUE_JSON = '''{
  "id": 441,
  "title": "worklink slice",
  "description": "Acceptance criteria:\\n- [ ] do it\\n- [ ] echo ok\\n\\nReview criteria:\\n- reviewer checks it\\n\\nWorklink notes:\\n- Scope: test fixture\\n- Out of scope: unrelated work\\n- Suggested test command: echo ok",
  "labels": ["worklink", "worklink:ready"],
  "parent_id": 380,
  "comments": []
}'''


def test_validate_leaf_refuses_missing_planner_template() -> None:
    issue = IssueContext(1, "vague", "please do thing", set())

    with pytest.raises(LeafValidationError, match="Acceptance criteria"):
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
):
    calls: list[Sequence[str] | str] = []
    commit_seen = False

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        nonlocal commit_seen
        calls.append(args)
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
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True)
            return cp(args)
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
        if isinstance(args, list) and args[:5] == ["git", "-C", str(worktree), "diff", "--cached"]:
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
    worktree = repo / ".worklink" / "441-2"
    calls: list[Sequence[str] | str] = []

    issue_with_prior_claim = ISSUE_JSON.replace(
        '"comments": []',
        '"comments": [{"content": "WORKLINK_CLAIM {\\"agent_id\\": \\"mimir-worklink\\", \\"attempt\\": 1, \\"claimed_at\\": \\"2026-06-12T12:04:29+00:00\\", \\"heartbeat_at\\": null, \\"issue_id\\": 441}"}]',
    )

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
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
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True)
            return cp(args)
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
    assert [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "--no-track",
        "-b",
        "issue/441-a2",
        str(worktree),
        "origin/main",
    ] in calls


def test_worklink_runner_happy_path_fake_backend(tmp_path: Path) -> None:
    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    init_logger(events, session_id="test-worklink")
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
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
    # Default base: worktree cut from main, PR targets main explicitly.
    assert ["git", "-C", str(repo), "fetch", "origin", "main"] in calls
    assert [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "--no-track",
        "-b",
        "issue/441-a1",
        str(worktree),
        "origin/main",
    ] in calls
    pr_calls = [c for c in calls if isinstance(c, list) and c[:3] == ["gh", "pr", "create"]]
    assert pr_calls and pr_calls[0][pr_calls[0].index("--base") + 1] == "main"
    body = events.read_text(encoding="utf-8")
    assert "worklink_claimed" in body
    assert "worklink_evidence" in body
    assert "worklink_transition" in body
    _reset_logger_for_tests()


def test_post_success_cleanup_failure_does_not_retransition_review_ready_issue(
    tmp_path: Path,
) -> None:
    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    init_logger(events, session_id="test-worklink")
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
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
    assert result.reason == "post-transition cleanup failed: worktree cleanup failed"
    assert ["chainlink", "issue", "label", "441", "worklink:review"] in calls
    assert ["chainlink", "issue", "label", "441", "worklink:failed"] not in calls
    assert ["chainlink", "issue", "label", "441", "worklink:ready"] not in calls
    records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert any(record["type"] == "worklink_cleanup_failed" for record in records)
    _reset_logger_for_tests()



def test_worklink_runner_cuts_worktree_and_pr_from_configured_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
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
    # Worktree is cut from the configured base, not main.
    assert ["git", "-C", str(repo), "fetch", "origin", "integration/worklink"] in calls
    assert [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "--no-track",
        "-b",
        "issue/441-a1",
        str(worktree),
        "origin/integration/worklink",
    ] in calls
    # And the PR targets that base (the feature-branch / stacking model).
    pr_calls = [c for c in calls if isinstance(c, list) and c[:3] == ["gh", "pr", "create"]]
    assert pr_calls
    assert pr_calls[0][pr_calls[0].index("--base") + 1] == "integration/worklink"


def test_worklink_run_base_override_beats_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
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
    assert [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "--no-track",
        "-b",
        "issue/441-a1",
        str(worktree),
        "origin/release/2.0",
    ] in calls
    assert not any(
        isinstance(c, list) and c[:5] == ["git", "-C", str(repo), "worktree", "add"] and c[-1] == "develop"
        for c in calls
    )
    pr_calls = [c for c in calls if isinstance(c, list) and c[:3] == ["gh", "pr", "create"]]
    assert pr_calls and pr_calls[0][pr_calls[0].index("--base") + 1] == "release/2.0"


def test_worklink_base_fetch_can_be_disabled_by_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
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

    assert result.status == "completed"
    assert not any(
        isinstance(c, list) and c[:4] == ["git", "-C", str(repo), "fetch"] for c in calls
    )
    assert [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "--no-track",
        "-b",
        "issue/441-a1",
        str(worktree),
        "main",
    ] in calls




def test_backend_blocked_result_routes_leaf_to_blocked_with_reason(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
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
    worktree = repo / ".worklink" / "441-1"
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
    worktree = repo / ".worklink" / "441-1"
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
    worktree = repo / ".worklink" / "441-1"
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

def test_codex_local_subprocess_uses_isolated_checkout(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _create_backend_checkout

    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if (
            args[:3] == ["git", "-C", str(tmp_path)]
            and args[3:6]
            in (["rev-parse", "--verify", "main"], ["rev-parse", "--verify", "origin/main"])
        ):
            return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")
        # Self-containment assert (#517): report the checkout as rooted at itself.
        if args[3:5] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{args[2]}\n", stderr="")
        if args[3:5] == ["rev-parse", "--absolute-git-dir"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{args[2]}/.git\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    lease = _create_backend_checkout(
        tmp_path,
        issue_id=517,
        attempt=2,
        base="main",
        backend_name="codex",
        compute_shared_filesystem=True,
        runner=runner,
    )

    assert lease.isolated_checkout is True
    assert any(call[:3] == ["git", "clone", "--local"] and "--no-hardlinks" not in call for call in calls)
    assert ["git", "-C", str(lease.path), "checkout", "-B", "issue/517-a2", "abc123"] in calls


def test_outside_worktree_detection_marks_root_leak_failed(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _with_outside_worktree_detection

    validation = EvidenceValidation(
        status="failed",
        review_ready=False,
        reasons=("completed_empty_diff",),
        evidence=WorklinkEvidence(
            issue=517,
            attempt=1,
            backend="codex",
            branch="issue/517-a1",
            worktree=str(tmp_path / ".worklink" / "517-1"),
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

    result = _with_outside_worktree_detection(
        validation,
        issue=517,
        attempt=1,
        root=tmp_path,
        worktree=tmp_path / ".worklink" / "517-1",
        runner=runner,
    )

    assert result.status == "failed"
    assert result.review_ready is False
    assert "completed_empty_diff" in result.reasons
    assert any(reason.startswith("backend_wrote_outside_worktree:") for reason in result.reasons)


def test_outside_worktree_leak_is_quarantined_recoverably(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _dirty_paths, _with_outside_worktree_detection

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
            worktree=str(worktree), started_at="2026-06-16T20:00:00+00:00",
            finished_at="2026-06-16T20:05:00+00:00", files_changed=[], diff_stat="",
            commands=[], tests=None, pr_url=None, status="failed",
        ),
    )

    result = _with_outside_worktree_detection(
        validation, issue=517, attempt=1, root=tmp_path, worktree=worktree,
        runner=runner, root_dirty_before=root_dirty_before,
    )

    assert result.status == "failed"
    assert any(r.startswith("backend_wrote_outside_worktree:") for r in result.reasons)
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

def test_run_epic_waits_on_launch_handle_and_finalizes(tmp_path: Path) -> None:
    """Regression (mimir review on #1030): run_epic must call
    ``compute.wait(handle, timeout_s)`` — the same 2-arg signature run() uses —
    after launching the factory compute job. A prior bug passed only
    ``timeout_s``; it bound to ``handle`` and dropped ``timeout_s``, so run-epic
    crashed with a TypeError right after launch and transitioned the epic to
    failed. This drives run_epic through launch/wait with a fake compute whose
    autonomous factory writes a completed run.json (with terminal_result) into
    the WORKTREE, and asserts review_ready mirroring plus the exact wait() args.

    Under the autonomous model the factory opens + promotes/requests review on
    the PR itself (via --ready/--reviewer), so the adapter only MIRRORS the
    outcome — it must NOT re-run ``gh pr ready`` / ``--add-reviewer``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = repo / ".worklink" / "700-1"

    epic_json = json.dumps(
        {
            "id": 700,
            "title": "epic",
            "description": "build the thing",
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "parent_id": None,
            "comments": [],
        }
    )

    class FactoryCompute(FakeCompute):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.waited: tuple[object, int] | None = None

        async def wait(self, handle: WorkSpec, timeout_s: int) -> ComputeResult:
            self.waited = (handle, timeout_s)
            # The factory namespaces run.json under .opencode/factory/<run-id>/
            # (run-id = chainlink-<issue_id>) inside its ``--repo`` worktree.
            factory_run = (
                handle.local_worktree / ".opencode" / "factory" / "chainlink-700" / "run.json"
            )
            factory_run.parent.mkdir(parents=True, exist_ok=True)
            factory_run.write_text(
                json.dumps(
                    {
                        "run_id": "chainlink-700",
                        "heartbeat_at": datetime.now(UTC).isoformat(),
                        "status": "completed",
                        "gates": {
                            "story": {"status": "approved"},
                            "brief": {"status": "approved"},
                            "pre_pr": {"status": "approved"},
                        },
                        "pr_url": "https://github.com/jasoncarreira/mimir/pull/999",
                        "terminal_result": {
                            "status": "completed",
                            "run_id": "chainlink-700",
                            "pr_url": "https://github.com/jasoncarreira/mimir/pull/999",
                            "summary": "shipped",
                        },
                    }
                ),
                encoding="utf-8",
            )
            return ComputeResult(exit_code=0, stdout="factory ok", stderr="")

    class FactoryBackend(FakeBackend):
        name = "feature_factory"

    compute = FactoryCompute(shared_filesystem=True)
    gh_calls: list[list[str]] = []

    def runner(
        args: Sequence[str] | str, **_: object
    ) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "700"]:
            return cp(args, stdout=epic_json)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True, exist_ok=True)
            return cp(args)
        if isinstance(args, list) and args[:2] == ["gh", "pr"]:
            gh_calls.append(list(args))
            return cp(args)
        return cp(args)

    registry = BackendRegistry(
        WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_compute"))
    )
    registry.register(FactoryBackend(status="success"))
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(
            home=tmp_path, repo=repo, runner=runner, registry=registry
        ).run_epic(700)
    )

    # With the bug (compute.wait(spec.timeout_s)) run_epic caught a TypeError and
    # returned status="failed"; the fix reaches wait() with the launch handle and
    # mirrors the completed run's PR to review-ready.
    assert result.status == "review_ready", (result.status, result.reason)
    assert result.review_ready is True
    assert result.pr_url == "https://github.com/jasoncarreira/mimir/pull/999"
    # The factory already opened/promoted the PR; the adapter must NOT re-run gh.
    assert not any(c[:2] == ["gh", "pr"] for c in gh_calls)
    assert compute.specs, "compute.launch was never called"
    assert compute.waited is not None, "compute.wait was never reached"
    handle, waited_timeout = compute.waited
    assert handle is compute.specs[0], "wait() did not receive the launch handle"
    assert waited_timeout == compute.specs[0].timeout_s
