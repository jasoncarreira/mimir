from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from mimir.worklink.backends import ComputeCaps, ComputeResult, RawResult, WorkOrder
from mimir.worklink.compute import WorkSpec
from mimir.worklink.epic import EpicRunner, EpicTestStatus, LeafIssue
from mimir.worklink.evidence import (
    CommandResult,
    EvidenceValidation,
    TestResult,
    WorklinkEvidence,
)
from mimir.worklink.orchestrator import IssueContext
from mimir.worklink.review import (
    DecomposeOutcome,
    IntegrationDecision,
    SliceDecision,
    WorklinkLeafSpec,
)
from mimir.worklink.worktree import IntegrationBranchLease, SliceMergeSuccess, WorktreeLease


def _leaf_description(title: str, test_command: str = "true") -> str:
    return f"""Problem:
{title}

Acceptance criteria:
- [ ] {title} works

Review criteria:
- Verify {title}

Worklink notes:
- Scope: {title.lower().replace(" ", "_")}.py
- Out of scope: unrelated work
- Suggested test command: {test_command}
"""


def _issue(
    issue_id: int,
    title: str,
    *,
    labels: set[str] | None = None,
    parent_id: int | None = 700,
) -> IssueContext:
    return IssueContext(
        issue_id=issue_id,
        title=title,
        description=_leaf_description(title),
        labels=labels or {"worklink:ready"},
        parent_id=parent_id,
        comments=(),
        created_at=datetime(2026, 7, 2, tzinfo=UTC),
    )


@dataclass
class RecordingChainlink:
    epic: IssueContext
    leaves: list[LeafIssue]
    events: list[tuple[Any, ...]]

    def read_issue(self, issue_id: int) -> IssueContext:
        assert issue_id == self.epic.issue_id
        return self.epic

    def child_leaves(self, epic_id: int) -> list[LeafIssue]:
        assert epic_id == self.epic.issue_id
        return list(self.leaves)

    def file_leaf(self, epic_id: int, leaf: WorklinkLeafSpec) -> int:
        assert epic_id == self.epic.issue_id
        new_id = 701 + len(self.leaves)
        self.events.append(("file", new_id, leaf.title))
        self.leaves.append(
            LeafIssue(
                _issue(new_id, leaf.title, parent_id=epic_id),
                scope_paths=tuple(leaf.scope_paths),
                suggested_test_command=leaf.suggested_test_command,
            )
        )
        return new_id

    def add_blocker(self, blocked_leaf: int, blocker_leaf: int, reason: str) -> None:
        self.events.append(("block", blocked_leaf, blocker_leaf, reason))
        self.leaves = [
            replace(leaf, blocked_by=leaf.blocked_by + (blocker_leaf,))
            if leaf.issue.issue_id == blocked_leaf
            else leaf
            for leaf in self.leaves
        ]

    def mark_merged(self, leaf_id: int) -> None:
        self.events.append(("merged", leaf_id))

    def mark_blocked(self, leaf_id: int, reason: str) -> None:
        self.events.append(("blocked", leaf_id, reason))

    def move_epic_to_review(self, epic_id: int) -> None:
        assert epic_id == self.epic.issue_id
        self.events.append(("epic-review", epic_id))


class RecordingRoles:
    """Action-based recording fake: decompose files the two slices via the
    chainlink client; reviews return recorded-style decisions."""

    def __init__(self, *, reject: set[int] | None = None) -> None:
        self.reject = reject or set()
        self.events: list[tuple[Any, ...]] = []

    async def run_decompose(self, epic: IssueContext, *, chainlink: Any) -> DecomposeOutcome:
        self.events.append(("decompose", epic.issue_id))
        leaves = [
            WorklinkLeafSpec(
                title="Slice A",
                acceptance_criteria=["A works"],
                review_criteria=["review A"],
                scope_paths=["a.py"],
                suggested_test_command="true",
            ),
            WorklinkLeafSpec(
                title="Slice B",
                acceptance_criteria=["B works"],
                review_criteria=["review B"],
                scope_paths=["b.py"],
                suggested_test_command="true",
                depends_on=["Slice A"],
            ),
        ]
        ids_by_title: dict[str, int] = {}
        for leaf in leaves:
            ids_by_title[leaf.title] = chainlink.file_leaf(epic.issue_id, leaf)
        for leaf in leaves:
            for dep_title in leaf.depends_on:
                chainlink.add_blocker(
                    ids_by_title[leaf.title],
                    ids_by_title[dep_title],
                    f"{leaf.title} depends on {dep_title}",
                )
        return DecomposeOutcome(filed_leaves=len(leaves))

    async def review_slice(
        self,
        *,
        leaf: LeafIssue,
        evidence: EvidenceValidation,
        mode: str,
        reviewer_count: int,
        chainlink: Any,
    ) -> SliceDecision:
        self.events.append(
            ("slice-review", leaf.issue.issue_id, mode, reviewer_count, evidence.review_ready)
        )
        if leaf.issue.issue_id in self.reject:
            return SliceDecision(
                approved=False,
                summary="blocked by reviewer",
                fixes=(f"{leaf.issue.title} stuck",),
            )
        return SliceDecision(approved=True, summary="ok")

    async def validate_integration(
        self,
        *,
        epic: IssueContext,
        manifest: object,
        partial: bool,
        blocked: dict[int, str],
        chainlink: Any,
    ) -> IntegrationDecision:
        self.events.append(("integrate", epic.issue_id, partial, tuple(sorted(blocked))))
        return IntegrationDecision(
            approved=True,
            summary="partial" if partial else "complete",
        )


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
        return RawResult(result.exit_code, None, "completed", None)


class RecordingCompute:
    name = "fake-compute"

    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.events = events
        self.launched: list[WorkSpec] = []

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(
            shared_filesystem=True,
            network_isolated=False,
            handle_cancel=True,
            persistent_after_disconnect=False,
        )

    async def launch(self, spec: WorkSpec) -> WorkSpec:
        self.events.append(("launch", spec.issue_id, spec.base_ref, spec.branch))
        self.launched.append(spec)
        return spec

    async def wait(self, handle: WorkSpec, timeout_s: int) -> ComputeResult:
        return ComputeResult(0, "ok", "")

    async def cleanup(self, handle: WorkSpec) -> None:
        self.events.append(("cleanup", handle.issue_id))


class RecordingRegistry:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.backend = FakeBackend()
        self.compute = RecordingCompute(events)

    def get(self, name: str) -> FakeBackend:
        return self.backend

    def select(self, **_: object) -> FakeBackend:
        return self.backend

    def select_compute(self, **_: object) -> RecordingCompute:
        return self.compute


def _cp(
    args: object,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def _runner(
    events: list[tuple[Any, ...]],
) -> Callable[[object], subprocess.CompletedProcess[str]]:
    def run(args: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        events.append(("cmd", tuple(args) if isinstance(args, (list, tuple)) else args))
        if isinstance(args, list) and args[:4] == ["git", "-C", "/repo"]:
            if args[4:7] == ["remote", "get-url", "origin"]:
                return _cp(args, stdout="git@github.com:org/repo.git\n")
            if args[4:7] == ["remote", "show", "-n"]:
                return _cp(args, stdout="  Fetch URL: git@github.com:org/repo.git\n")
        if isinstance(args, list) and args[:3] == ["gh", "pr", "create"]:
            events.append(("pr", args))
            return _cp(args, stdout="https://github.com/org/repo/pull/77\n")
        return _cp(args)

    return run


def _patch_epic_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, events: list[tuple[Any, ...]]
) -> None:
    import mimir.worklink.epic as epic_mod

    integration_path = tmp_path / "integration"
    integration_path.mkdir()

    def create_integration(*_: object, **__: object) -> IntegrationBranchLease:
        events.append(("integration-branch", "epic/700-integration"))
        return IntegrationBranchLease(
            700,
            Path("/repo"),
            integration_path,
            "epic/700-integration",
            "main",
            "main",
        )

    def create_slice(
        *_: object,
        leaf: LeafIssue,
        attempt: int,
        integration_branch: str,
        **__: object,
    ) -> WorktreeLease:
        path = tmp_path / f"slice-{leaf.issue.issue_id}-{attempt}"
        path.mkdir()
        branch = f"issue/{leaf.issue.issue_id}-a{attempt}"
        events.append(("slice-worktree", leaf.issue.issue_id, integration_branch, branch))
        return WorktreeLease(
            leaf.issue.issue_id,
            attempt,
            Path("/repo"),
            path,
            branch,
            integration_branch,
            "HEAD",
        )

    async def observe(**kw: object) -> EvidenceValidation:
        leaf = kw["leaf"]
        spec = kw["spec"]
        assert isinstance(leaf, LeafIssue)
        assert isinstance(spec, WorkSpec)
        evidence = WorklinkEvidence(
            issue=leaf.issue.issue_id,
            attempt=spec.attempt,
            backend="fake",
            branch=spec.branch,
            worktree=str(spec.local_worktree),
            started_at="2026-07-02T00:00:00+00:00",
            finished_at="2026-07-02T00:00:01+00:00",
            files_changed=[f"{leaf.issue.title}.txt"],
            diff_stat=f"{leaf.issue.title}.txt | 1 +",
            commands=[CommandResult("git diff", 0)],
            tests=TestResult("true", 0),
            pr_url=None,
            status="completed",
        )
        events.append(("observe", leaf.issue.issue_id, spec.branch))
        return EvidenceValidation("completed", True, (), evidence)

    def merge(
        repo: Path, *, slice_branch: str, integration_branch: str, runner: object
    ) -> SliceMergeSuccess:
        del repo, runner
        events.append(("merge", slice_branch, integration_branch))
        return SliceMergeSuccess(slice_branch, integration_branch, f"merge-{slice_branch}")

    def push(path: Path, branch: str, *, runner: object) -> None:
        del runner
        events.append(("push", str(path), branch))

    monkeypatch.setattr(epic_mod, "create_integration_branch", create_integration)
    monkeypatch.setattr(epic_mod, "_create_slice_checkout", create_slice)
    monkeypatch.setattr(epic_mod, "_observe_slice", observe)
    monkeypatch.setattr(epic_mod, "_commit_worktree_changes", lambda *a, **k: None)
    monkeypatch.setattr(epic_mod, "merge_slice_into_integration", merge)
    monkeypatch.setattr(epic_mod, "_git_push", push)
    monkeypatch.setattr(
        epic_mod,
        "_run_epic_tests",
        lambda *a, **k: EpicTestStatus("true", 0, "ok"),
    )


def _pr_bodies(events: list[tuple[Any, ...]]) -> list[str]:
    bodies = []
    for event in events:
        if event[0] != "pr":
            continue
        args = event[1]
        bodies.append(args[args.index("--body") + 1])
    return bodies


@pytest.mark.asyncio
async def test_integrated_epic_e2e_decomposes_merges_serially_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[Any, ...]] = []
    _patch_epic_io(monkeypatch, tmp_path, events)
    chainlink = RecordingChainlink(
        epic=_issue(
            700, "Epic", labels={"worklink:epic", "worklink:ready"}, parent_id=None
        ),
        leaves=[],
        events=events,
    )
    roles = RecordingRoles()
    registry = RecordingRegistry(events)
    runner = _runner(events)

    result = await EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=runner,
        registry=registry,  # type: ignore[arg-type]
        roles=roles,
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(700)

    assert result.status == "completed"
    assert result.pr_url == "https://github.com/org/repo/pull/77"
    assert registry.compute.launched[0].issue_id == 701
    assert registry.compute.launched[1].issue_id == 702
    assert [event for event in events if event[0] == "merge"] == [
        ("merge", "issue/701-a1", "epic/700-integration"),
        ("merge", "issue/702-a1", "epic/700-integration"),
    ]
    assert ("block", 702, 701, "Slice B depends on Slice A") in events
    assert [event for event in events if event[0] == "epic-review"] == [("epic-review", 700)]
    assert len(_pr_bodies(events)) == 1
    assert any(event[0] == "pr" and "--draft" in event[1] for event in events)
    assert not any(
        event[0] == "cmd" and event[1][:3] == ("gh", "pr", "merge")
        for event in events
    )
    assert not any(event[0] == "push" and event[2] == "main" for event in events)

    before_resume = len(events)
    resumed = await EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=runner,
        registry=registry,  # type: ignore[arg-type]
        roles=roles,
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(700)

    assert resumed.status == "completed"
    assert len(registry.compute.launched) == 2
    assert len([event for event in events if event[0] == "merge"]) == 2
    assert len(_pr_bodies(events)) == 1
    assert len(events) > before_resume


@pytest.mark.asyncio
async def test_integrated_epic_e2e_partial_pr_names_stuck_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[Any, ...]] = []
    _patch_epic_io(monkeypatch, tmp_path, events)
    (tmp_path / "worklink.yaml").write_text(
        "defaults:\n  max_review_retries: 1\n  test_command: true\n",
        encoding="utf-8",
    )
    chainlink = RecordingChainlink(
        epic=_issue(
            700, "Epic", labels={"worklink:epic", "worklink:ready"}, parent_id=None
        ),
        leaves=[],
        events=events,
    )
    roles = RecordingRoles(reject={702})

    result = await EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=_runner(events),
        registry=RecordingRegistry(events),  # type: ignore[arg-type]
        roles=roles,
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(700)

    assert result.status == "partial"
    assert result.blocked_leaves == (702,)
    assert [event for event in events if event[0] == "merge"] == [
        ("merge", "issue/701-a1", "epic/700-integration")
    ]
    body = _pr_bodies(events)[0]
    assert "- Epic status: partial" in body
    assert "- Blocked leaf #702: Slice B stuck" in body
    assert not any(
        event[0] == "cmd" and event[1][:3] == ("gh", "pr", "merge")
        for event in events
    )
    assert not any(event[0] == "push" and event[2] == "main" for event in events)
