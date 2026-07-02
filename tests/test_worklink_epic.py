from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from typing import Any, Sequence

import pytest

from mimir.worklink.backends import ComputeCaps, ComputeResult, RawResult, WorkOrder
from mimir.worklink.compute import WorkSpec
from mimir.worklink.epic import ChainlinkEpicClient, EpicRunner, LeafIssue, compute_waves
from mimir.worklink.evidence import (
    CommandResult,
    EvidenceValidation,
    TestResult,
    WorklinkEvidence,
)
from mimir.worklink.orchestrator import IssueContext
from mimir.worklink.review import (
    DecomposeReview,
    IntegrationValidation,
    SliceReview,
    WorkDecomposition,
    WorklinkBlockerEdge,
    WorklinkLeafSpec,
)
from mimir.worklink.worktree import IntegrationBranchLease, SliceMergeSuccess, WorktreeLease


def leaf_description(test_command: str = "true") -> str:
    return f"""Acceptance criteria:
- [ ] Works

Review criteria:
- Verify it

Worklink notes:
- Scope: file.txt
- Out of scope: unrelated
- Suggested test command: {test_command}
"""


def issue(issue_id: int, *, parent_id: int | None = 100, labels: set[str] | None = None) -> IssueContext:
    return IssueContext(
        issue_id=issue_id,
        title=f"Issue {issue_id}",
        description=leaf_description(),
        labels=labels or {"worklink:ready"},
        parent_id=parent_id,
        comments=(),
        created_at=datetime(2026, 6, 13, tzinfo=UTC),
    )


def test_compute_waves_from_blocked_by_dag() -> None:
    leaves = [
        LeafIssue(issue(1), blocked_by=(), scope_paths=("a.py",)),
        LeafIssue(issue(2), blocked_by=(1,), scope_paths=("b.py",)),
        LeafIssue(issue(3), blocked_by=(1,), scope_paths=("c.py",)),
        LeafIssue(issue(4), blocked_by=(2, 3), scope_paths=("d.py",)),
    ]

    assert [[leaf.issue.issue_id for leaf in wave] for wave in compute_waves(leaves)] == [
        [1],
        [2, 3],
        [4],
    ]


def test_chainlink_child_leaves_parse_scope_and_test_from_leaf_template() -> None:
    def fake_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return cp(
            args,
            stdout=json.dumps(
                [
                    {
                        "id": 101,
                        "title": "Leaf",
                        "description": leaf_description("pytest -q tests/test_x.py"),
                        "labels": ["worklink:ready"],
                        "parent_id": 100,
                        "blocked_by": [],
                    }
                ]
            ),
        )

    leaves = ChainlinkEpicClient(runner=fake_runner).child_leaves(100)

    assert leaves[0].scope_paths == ("file.txt",)
    assert leaves[0].suggested_test_command == "pytest -q tests/test_x.py"


@dataclass
class FakeChainlink:
    epic: IssueContext
    leaves: list[LeafIssue]
    filed: list[str]
    blocked: dict[int, str]
    merged: list[int]
    moved_to_review: bool = False

    def read_issue(self, issue_id: int) -> IssueContext:
        assert issue_id == self.epic.issue_id
        return self.epic

    def child_leaves(self, epic_id: int) -> list[LeafIssue]:
        assert epic_id == self.epic.issue_id
        return list(self.leaves)

    def file_leaf(self, epic_id: int, leaf: WorklinkLeafSpec) -> int:
        new_id = 200 + len(self.filed)
        self.filed.append(leaf.title)
        self.leaves.append(
            LeafIssue(
                issue(new_id),
                scope_paths=tuple(leaf.scope_paths),
                suggested_test_command=leaf.suggested_test_command,
            )
        )
        return new_id

    def add_blocker(self, blocked_leaf: int, blocker_leaf: int, reason: str) -> None:
        self.blocked[blocked_leaf] = f"blocked by {blocker_leaf}: {reason}"

    def mark_merged(self, leaf_id: int) -> None:
        self.merged.append(leaf_id)

    def mark_blocked(self, leaf_id: int, reason: str) -> None:
        self.blocked[leaf_id] = reason

    def move_epic_to_review(self, epic_id: int) -> None:
        assert epic_id == self.epic.issue_id
        self.moved_to_review = True


class FakeRoles:
    def __init__(self, *, reviews: list[str] | None = None, decomposition: WorkDecomposition | None = None) -> None:
        self.reviews = reviews or ["APPROVE"]
        self.decomposition = decomposition
        self.slice_review_calls: list[dict[str, Any]] = []
        self.validations = 0

    async def decompose(self, epic: IssueContext) -> WorkDecomposition:
        assert self.decomposition is not None
        return self.decomposition

    async def review_decomposition(
        self, epic: IssueContext, decomposition: WorkDecomposition
    ) -> DecomposeReview:
        return DecomposeReview(verdict="APPROVE", summary="ok")

    async def review_slice(
        self,
        *,
        leaf: LeafIssue,
        evidence: EvidenceValidation,
        mode: str,
        reviewer_count: int,
    ) -> SliceReview:
        self.slice_review_calls.append({"leaf": leaf.issue.issue_id, "mode": mode, "count": reviewer_count})
        verdict = self.reviews.pop(0) if self.reviews else "APPROVE"
        return SliceReview(
            verdict=verdict, summary=verdict.lower(), required_fixes=["fix it"] if verdict == "REJECT" else []
        )

    async def validate_integration(
        self,
        *,
        epic: IssueContext,
        manifest: object,
        partial: bool,
        blocked: dict[int, str],
    ) -> IntegrationValidation:
        self.validations += 1
        return IntegrationValidation(verdict="GO-WITH-NITS" if partial else "GO", summary="integrated")


class FakeBackend:
    name = "fake"

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
            local_worktree=order.worktree,
        )

    async def interpret(self, order: WorkOrder, result: ComputeResult) -> RawResult:
        return RawResult(0, None, "completed", None)


class FakeCompute:
    name = "fake_compute"

    def __init__(self) -> None:
        self.launched: list[WorkSpec] = []

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(shared_filesystem=True, network_isolated=False, handle_cancel=True, persistent_after_disconnect=False)

    async def launch(self, spec: WorkSpec) -> WorkSpec:
        self.launched.append(spec)
        return spec

    async def wait(self, handle: WorkSpec, timeout_s: int) -> ComputeResult:
        return ComputeResult(0, "ok", "")

    async def cleanup(self, handle: WorkSpec) -> None:
        return None


class FakeRegistry:
    def __init__(self) -> None:
        self.backend = FakeBackend()
        self.compute = FakeCompute()

    def get(self, name: str) -> FakeBackend:
        return self.backend

    def select(self, **_: object) -> FakeBackend:
        return self.backend

    def select_compute(self, **_: object) -> FakeCompute:
        return self.compute


def cp(args: Sequence[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout=stdout, stderr=stderr)


def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    if list(args)[:4] == ["git", "-C", "/repo"]:
        return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
    return cp(args)


def patch_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import mimir.worklink.epic as epic_mod

    integration_path = tmp_path / "integration"
    integration_path.mkdir()
    monkeypatch.setattr(
        epic_mod,
        "create_integration_branch",
        lambda *a, **k: IntegrationBranchLease(100, Path("/repo"), integration_path, "epic/100-integration", "main", "main"),
    )
    counter = {"n": 0}

    def fake_slice(*_: object, slice_id: int, integration_branch: str, **__: object) -> WorktreeLease:
        counter["n"] += 1
        path = tmp_path / f"slice-{slice_id}-{counter['n']}"
        path.mkdir()
        return WorktreeLease(slice_id, counter["n"], Path("/repo"), path, f"issue/{slice_id}-a{counter['n']}", integration_branch, "HEAD")

    monkeypatch.setattr(epic_mod, "create_slice_worktree", fake_slice)
    monkeypatch.setattr(epic_mod, "_commit_worktree_changes", lambda *a, **k: None)
    monkeypatch.setattr(epic_mod, "_git_push", lambda *a, **k: None)
    monkeypatch.setattr(epic_mod, "_run_epic_tests", lambda *a, **k: None)
    monkeypatch.setattr(
        epic_mod,
        "merge_slice_into_integration",
        lambda repo, *, slice_branch, integration_branch, runner: SliceMergeSuccess(slice_branch, integration_branch, "abc123"),
    )
    monkeypatch.setattr(epic_mod, "_open_epic_pr", lambda *a, **k: "https://github.com/o/r/pull/1")


def ready_validation(leaf_id: int, attempt: int = 1) -> EvidenceValidation:
    evidence = WorklinkEvidence(
        issue=leaf_id,
        attempt=attempt,
        backend="fake",
        branch=f"issue/{leaf_id}-a{attempt}",
        worktree="/tmp/wt",
        started_at="2026-07-02T00:00:00+00:00",
        finished_at="2026-07-02T00:00:01+00:00",
        files_changed=["file.txt"],
        diff_stat="file.txt | 1 +",
        commands=[CommandResult("git diff", 0)],
        tests=TestResult("true", 0),
        pr_url=None,
        status="completed",
    )
    return EvidenceValidation("completed", True, (), evidence)


async def fake_observe_slice(**kw: object) -> EvidenceValidation:
    leaf = kw["leaf"]
    spec = kw["spec"]
    assert isinstance(leaf, LeafIssue)
    assert isinstance(spec, WorkSpec)
    return ready_validation(leaf.issue.issue_id, spec.attempt)


@pytest.mark.asyncio
async def test_per_slice_observe_review_merge_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_git(monkeypatch, tmp_path)
    import mimir.worklink.epic as epic_mod

    monkeypatch.setattr(epic_mod, "_observe_slice", fake_observe_slice)
    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:epic", "worklink:ready"}),
        leaves=[LeafIssue(issue(101), scope_paths=("a.py",), suggested_test_command="true")],
        filed=[],
        blocked={},
        merged=[],
    )
    roles = FakeRoles()
    result = await EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=runner,
        registry=FakeRegistry(),  # type: ignore[arg-type]
        roles=roles,
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(100)

    assert result.status == "completed"
    assert chainlink.merged == [101]
    assert chainlink.moved_to_review is True
    assert roles.slice_review_calls == [{"leaf": 101, "mode": "single", "count": 1}]


@pytest.mark.asyncio
async def test_reject_retry_then_block_opens_partial_pr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_git(monkeypatch, tmp_path)
    (tmp_path / "worklink.yaml").write_text("defaults:\n  max_review_retries: 2\n  test_command: true\n", encoding="utf-8")
    import mimir.worklink.epic as epic_mod

    monkeypatch.setattr(epic_mod, "_observe_slice", fake_observe_slice)
    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:epic"}),
        leaves=[LeafIssue(issue(101), scope_paths=("a.py",), suggested_test_command="true")],
        filed=[],
        blocked={},
        merged=[],
    )
    result = await EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=runner,
        registry=FakeRegistry(),  # type: ignore[arg-type]
        roles=FakeRoles(reviews=["REJECT", "REJECT"]),
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(100)

    assert result.status == "partial"
    assert result.blocked_leaves == (101,)
    assert chainlink.blocked[101] == "fix it"
    assert chainlink.moved_to_review is True


@pytest.mark.asyncio
async def test_decompose_then_build_files_child_leaves_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_git(monkeypatch, tmp_path)
    import mimir.worklink.epic as epic_mod

    monkeypatch.setattr(epic_mod, "_observe_slice", fake_observe_slice)
    decomposition = WorkDecomposition(
        summary="split",
        leaves=[
            WorklinkLeafSpec(
                title="Leaf A",
                acceptance_criteria=["A works"],
                review_criteria=["check A"],
                scope_paths=["a.py"],
                suggested_test_command="true",
            ),
            WorklinkLeafSpec(
                title="Leaf B",
                acceptance_criteria=["B works"],
                review_criteria=["check B"],
                scope_paths=["b.py"],
                suggested_test_command="true",
            ),
        ],
        blocked_by=[WorklinkBlockerEdge(blocked_leaf="Leaf B", blocker_leaf="Leaf A", reason="A first")],
    )
    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:epic"}),
        leaves=[],
        filed=[],
        blocked={},
        merged=[],
    )
    result = await EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=runner,
        registry=FakeRegistry(),  # type: ignore[arg-type]
        roles=FakeRoles(decomposition=decomposition),
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(100)

    assert result.status == "completed"
    assert chainlink.filed == ["Leaf A", "Leaf B"]
    assert chainlink.merged == [200, 201]


@pytest.mark.asyncio
async def test_bare_ready_without_epic_label_is_not_epic(tmp_path: Path) -> None:
    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:ready"}),
        leaves=[],
        filed=[],
        blocked={},
        merged=[],
    )

    with pytest.raises(Exception, match="worklink:epic"):
        await EpicRunner(
            home=tmp_path,
            repo=Path("/repo"),
            runner=runner,
            registry=FakeRegistry(),  # type: ignore[arg-type]
            roles=FakeRoles(),
            chainlink=chainlink,  # type: ignore[arg-type]
        ).run(100)
