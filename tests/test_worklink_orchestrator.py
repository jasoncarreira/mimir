from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

import pytest
import asyncio

from mimir.event_logger import _reset_logger_for_tests, init_logger
from mimir.worklink.backends import Caps, RawResult, WorkOrder
from mimir.worklink.backends.registry import BackendRegistry, WorklinkConfig
from mimir.worklink.orchestrator import (
    IssueContext,
    LeafValidationError,
    WorklinkRunner,
    validate_leaf,
)


class FakeBackend:
    name = "fake"

    def __init__(self, status: str = "success", *, write_change: bool = True) -> None:
        self.status = status
        self.write_change = write_change
        self.orders: list[WorkOrder] = []

    def capabilities(self) -> Caps:
        return Caps("fake", False, False, False, True, None)

    async def run(self, order: WorkOrder) -> RawResult:
        self.orders.append(order)
        if self.write_change:
            (order.worktree / "changed.txt").write_text("hello\n", encoding="utf-8")
        return RawResult(
            0 if self.status == "success" else 1,
            order.transcript_root / "fake.json",
            self.status,
            None,
        )


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
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True)
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "diff"]:
            return cp(args)
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


def test_validate_leaf_requires_worklink_notes_template() -> None:
    issue = IssueContext(
        443,
        "old loose leaf",
        "Acceptance criteria:\n- [ ] do it\n\nReview criteria: reviewer checks it",
        {"worklink"},
    )

    with pytest.raises(LeafValidationError, match="Worklink notes"):
        validate_leaf(issue)


def test_planner_prompt_and_skill_embed_single_leaf_template() -> None:
    from mimir.worklink.planning import LEAF_TEMPLATE_MARKDOWN

    root = Path(__file__).parent.parent
    prompt = (root / "mimir" / "prompt_templates" / "decompose.md").read_text(
        encoding="utf-8"
    )
    skill = (root / "mimir" / "skills" / "chainlink-orchestrator" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert LEAF_TEMPLATE_MARKDOWN in prompt
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
