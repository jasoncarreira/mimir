from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

import pytest
import asyncio

from mimir.event_logger import _reset_logger_for_tests, init_logger
from mimir.worklink.backends import Caps, ComputeCaps, ComputeResult, RawResult, WorkOrder
from mimir.worklink.backends.registry import BackendRegistry, WorklinkConfig, WorklinkDefaults
from mimir.worklink.compute import WorkSpec
from mimir.worklink.worker import WorkerPayload, run_worker_payload
from mimir.worklink.orchestrator import (
    IssueContext,
    LeafValidationError,
    WorklinkRunner,
    render_decomposition_prompt,
    validate_leaf,
)


class FakeCompute:
    name = "fake_compute"

    def __init__(self) -> None:
        self.specs: list[WorkSpec] = []
        self.cleaned: list[WorkSpec] = []

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(False, False, True, False)

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



class WorkerFakeBackend(FakeBackend):
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
            local_argv=("fake-tool", "--cd", str(order.worktree), order.prompt),
        )


def test_worker_payload_clone_branch_fake_backend_pushes_and_writes_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "worker-repo"
    evidence_path = tmp_path / "out" / "evidence.json"
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:2] == ["git", "clone"]:
            repo.mkdir(parents=True)
            (repo / ".git").mkdir()
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "fetch"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "checkout"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--name-only" in args:
            return cp(args, stdout="changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--stat" in args:
            return cp(args, stdout=" changed.txt | 1 +\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "status"]:
            return cp(args, stdout="?? changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "add"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "commit"]:
            return cp(args, stdout="[issue/456-a1 abc123] worklink\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "push"]:
            return cp(args)
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        return cp(args)

    backend = WorkerFakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    spec = backend.work_spec(
        WorkOrder(
            issue_id=456,
            worktree=tmp_path / "origin-local-worktree-is-ignored",
            prompt="Do worker handoff",
            rules=None,
            timeout_s=30,
            env={"MIMIR_HOME": str(tmp_path / "home")},
            transcript_root=tmp_path / "transcripts",
        ),
        attempt=1,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="origin/main",
        branch="issue/456-a1",
        test_command="echo ok",
    )
    payload = WorkerPayload(
        spec=spec,
        repo_dir=repo,
        evidence_path=evidence_path,
        transcript_root=tmp_path / "transcripts",
        safe_env={"PATH": "/bin"},
    )

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.status == "completed"
    assert validation.review_ready is True
    assert evidence_path.is_file()
    assert ["git", "clone", "git@github.com:jasoncarreira/mimir.git", str(repo)] in calls
    assert ["git", "-C", str(repo), "checkout", "-B", "issue/456-a1", "origin/main"] in calls
    assert ["git", "-C", str(repo), "push", "origin", "HEAD:issue/456-a1"] in calls
    assert backend.orders[0].worktree == repo

def test_orchestrator_passes_configured_compute_backend_to_tool_backend(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    compute = FakeCompute()
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
            441, backend_name="fake"
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
  "labels": ["worklink"],
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
    assert not any(isinstance(call, list) and call[:2] == ["chainlink", "locks"] for call in calls)
    assert backend.orders == []




def _orchestrator_runner(
    repo: Path,
    worktree: Path,
    *,
    files_stdout: str = "changed.txt\n",
    dirty_after_commit: bool = False,
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
        if isinstance(args, list) and args[:3] == ["git", "-C", str(repo)] and args[3] == "push":
            return cp(args)
        if isinstance(args, list) and args[:3] == ["gh", "pr", "create"]:
            return cp(args, stdout="https://github.com/jasoncarreira/mimir/pull/999\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "remove"]:
            return cp(args)
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
        "-b",
        "issue/441-a2",
        str(worktree),
        "main",
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
    assert any(isinstance(call, list) and call[:3] == ["gh", "pr", "create"] for call in calls)
    body = events.read_text(encoding="utf-8")
    assert "worklink_claimed" in body
    assert "worklink_evidence" in body
    assert "worklink_transition" in body
    _reset_logger_for_tests()



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
    skill = (root / "mimir" / "skills" / "chainlink-orchestrator" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert LEAF_TEMPLATE_MARKDOWN in skill


def test_worklink_uses_planner_suggested_test_command_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "443"]:
            return cp(args, stdout=STRICT_ISSUE_JSON)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(tmp_path / "repo"), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        return cp(args)

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
    assert "uv run pytest -q tests/test_worklink_orchestrator.py" in out


def test_decompose_prompt_teaches_chainlink_block_argument_order() -> None:
    prompt = (Path(__file__).parent.parent / "mimir" / "prompt_templates" / "decompose.md").read_text(
        encoding="utf-8"
    )

    assert "chainlink issue block <ID-that-is-blocked> <BLOCKER>" in prompt
    assert "blocked issue id comes first" in prompt
    assert "chainlink issue block <blocker> <blocked>" not in prompt
