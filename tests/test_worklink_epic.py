from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from typing import Any, Sequence

import pytest

from mimir.worklink.backends import ComputeCaps, ComputeResult, RawResult, WorkOrder, WorklinkConfig
from mimir.worklink.compute import WorkSpec
from mimir.worklink.epic import (
    ChainlinkEpicClient,
    EpicRunResult,
    EpicRunner,
    EpicTestStatus,
    LeafIssue,
    MissingEpicRoleRunner,
    compute_waves,
)
from mimir.worklink.epic_state import EpicRunManifest, EpicSliceRecord
from mimir.worklink.epic_roles import EpicSubagentRoleRunner
from mimir.worklink.evidence import (
    CommandResult,
    EvidenceValidation,
    TestResult,
    WorklinkEvidence,
)
from mimir.worklink.orchestrator import IssueContext, validate_leaf
from mimir.worklink.planning import missing_leaf_template_parts
from mimir.worklink.review import (
    DecomposeOutcome,
    IntegrationDecision,
    SliceDecision,
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
    comments: list[tuple[int, str]] | None = None
    failed_epics: list[tuple[int, bool, str]] | None = None
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

    def comment(self, issue_id: int, text: str) -> None:
        if self.comments is not None:
            self.comments.append((issue_id, text))

    def mark_epic_failed(self, epic_id: int, *, retryable: bool, reason: str) -> None:
        if self.failed_epics is not None:
            self.failed_epics.append((epic_id, retryable, reason))
        self.comment(epic_id, f"WORKLINK_EPIC_FAILED {reason}")

    def move_epic_to_review(self, epic_id: int) -> None:
        assert epic_id == self.epic.issue_id
        self.moved_to_review = True


class FakeRoles:
    """Action-based fake: decompose files leaves via the chainlink client;
    reviews return recorded-style decisions."""

    def __init__(
        self, *, reviews: list[str] | None = None, decomposition: list[WorklinkLeafSpec] | None = None
    ) -> None:
        self.reviews = reviews or ["APPROVE"]
        self.decomposition = decomposition
        self.slice_review_calls: list[dict[str, Any]] = []
        self.validations = 0

    async def run_decompose(self, epic: IssueContext, *, chainlink: Any) -> DecomposeOutcome:
        assert self.decomposition is not None
        ids_by_title: dict[str, int] = {}
        for leaf in self.decomposition:
            ids_by_title[leaf.title] = chainlink.file_leaf(epic.issue_id, leaf)
        for leaf in self.decomposition:
            for dep_title in leaf.depends_on:
                chainlink.add_blocker(
                    ids_by_title[leaf.title],
                    ids_by_title[dep_title],
                    f"{leaf.title} depends on {dep_title}",
                )
        return DecomposeOutcome(filed_leaves=len(self.decomposition))

    async def review_slice(
        self,
        *,
        leaf: LeafIssue,
        evidence: EvidenceValidation,
        mode: str,
        reviewer_count: int,
        chainlink: Any,
    ) -> SliceDecision:
        self.slice_review_calls.append({"leaf": leaf.issue.issue_id, "mode": mode, "count": reviewer_count})
        verdict = self.reviews.pop(0) if self.reviews else "APPROVE"
        approved = verdict == "APPROVE"
        return SliceDecision(
            approved=approved,
            summary=verdict.lower(),
            fixes=() if approved else ("fix it",),
        )

    async def validate_integration(
        self,
        *,
        epic: IssueContext,
        manifest: object,
        partial: bool,
        blocked: dict[int, str],
        chainlink: Any,
    ) -> IntegrationDecision:
        self.validations += 1
        return IntegrationDecision(approved=True, summary="integrated")


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


def _label_values(args: Sequence[str]) -> list[str]:
    values: list[str] = []
    items = list(args)
    for index, item in enumerate(items):
        if item == "--label":
            values.append(items[index + 1])
    return values


def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    if list(args)[:4] == ["git", "-C", "/repo"]:
        return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
    return cp(args)



def test_run_epic_wires_real_role_runner_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import mimir.worklink.epic as epic_mod

    async def fake_run(self: EpicRunner, epic_id: int, **kwargs: object) -> EpicRunResult:
        assert isinstance(self.roles, EpicSubagentRoleRunner)
        assert not isinstance(self.roles, MissingEpicRoleRunner)
        return EpicRunResult(epic_id, "completed")

    monkeypatch.setattr(EpicRunner, "run", fake_run)

    result = epic_mod.run_epic(home=tmp_path, repo=Path("/repo"), epic_id=100)

    assert result.status == "completed"


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
    monkeypatch.setattr(epic_mod, "_run_epic_tests", lambda *a, **k: EpicTestStatus("true", 0, "ok"))
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


def empty_diff_validation(leaf_id: int, attempt: int = 1) -> EvidenceValidation:
    evidence = WorklinkEvidence(
        issue=leaf_id,
        attempt=attempt,
        backend="fake",
        branch=f"issue/{leaf_id}-a{attempt}",
        worktree="/tmp/wt",
        started_at="2026-07-02T00:00:00+00:00",
        finished_at="2026-07-02T00:00:01+00:00",
        files_changed=[],
        diff_stat="",
        commands=[CommandResult("git diff", 0)],
        tests=TestResult("true", 0),
        pr_url=None,
        status="failed",
    )
    return EvidenceValidation("failed", False, ("completed_empty_diff",), evidence)


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
async def test_epic_runner_claims_heartbeats_and_releases_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_git(monkeypatch, tmp_path)
    import mimir.worklink.epic as epic_mod

    monkeypatch.setattr(epic_mod, "_observe_slice", fake_observe_slice)
    calls: list[list[str]] = []

    def recording_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return runner(args)

    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:epic", "worklink:ready"}),
        leaves=[LeafIssue(issue(101), scope_paths=("a.py",), suggested_test_command="true")],
        filed=[],
        blocked={},
        merged=[],
    )

    result = await EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=recording_runner,
        registry=FakeRegistry(),  # type: ignore[arg-type]
        roles=FakeRoles(),
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(100)

    claim_comments = [
        call[-1]
        for call in calls
        if call[:4] == ["chainlink", "issue", "comment", "100"]
        and "WORKLINK_CLAIM" in call[-1]
    ]
    assert result.status == "completed"
    assert ["chainlink", "locks", "claim", "100"] in calls
    assert ["chainlink", "issue", "label", "100", "worklink:in-progress"] in calls
    assert ["chainlink", "locks", "release", "100"] in calls
    assert len(claim_comments) >= 2
    assert any('"heartbeat_at":' in comment and "null" not in comment for comment in claim_comments)


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
async def test_empty_diff_evidence_skips_slice_reviewer_and_records_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_git(monkeypatch, tmp_path)
    (tmp_path / "worklink.yaml").write_text("defaults:\n  max_review_retries: 1\n  test_command: true\n", encoding="utf-8")
    import mimir.worklink.epic as epic_mod

    async def observe_empty(**kw: object) -> EvidenceValidation:
        leaf = kw["leaf"]
        spec = kw["spec"]
        assert isinstance(leaf, LeafIssue)
        assert isinstance(spec, WorkSpec)
        return empty_diff_validation(leaf.issue.issue_id, spec.attempt)

    monkeypatch.setattr(epic_mod, "_observe_slice", observe_empty)
    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:epic"}),
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

    assert result.status == "partial"
    assert roles.slice_review_calls == []
    assert chainlink.blocked[101] == "completed_empty_diff"
    manifest = EpicRunManifest.from_json(json.loads(Path(result.manifest_path).read_text(encoding="utf-8")))
    record = manifest.slices[0]
    assert record.attempts == 1
    assert record.review_ref == "review skipped: completed_empty_diff"


@pytest.mark.asyncio
async def test_transient_provider_error_backs_off_within_attempt_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_git(monkeypatch, tmp_path)
    import mimir.worklink.epic as epic_mod

    class OverloadedThenReadyBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def interpret(self, order: WorkOrder, result: ComputeResult) -> RawResult:
            self.calls += 1
            if self.calls == 1:
                return RawResult(1, order.transcript_root / "fake.json", "backend_error", "servers are currently overloaded")
            return RawResult(0, order.transcript_root / "fake.json", "completed", None)

    async def observe_by_attempt(**kw: object) -> EvidenceValidation:
        leaf = kw["leaf"]
        spec = kw["spec"]
        assert isinstance(leaf, LeafIssue)
        assert isinstance(spec, WorkSpec)
        if spec.attempt == 1:
            return empty_diff_validation(leaf.issue.issue_id, spec.attempt)
        return ready_validation(leaf.issue.issue_id, spec.attempt)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    registry = FakeRegistry()
    registry.backend = OverloadedThenReadyBackend()
    monkeypatch.setattr(epic_mod, "_observe_slice", observe_by_attempt)
    monkeypatch.setattr(epic_mod.asyncio, "sleep", fake_sleep)
    integration = IntegrationBranchLease(100, Path("/repo"), tmp_path / "integration", "epic/100-integration", "main", "main")
    manifest = EpicRunManifest(
        epic_id=100,
        integration_branch=integration.branch,
        integration_worktree=str(integration.path),
        base_ref="main",
        phase="build",
        slices=[EpicSliceRecord(id=101)],
    )
    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:epic"}),
        leaves=[LeafIssue(issue(101), scope_paths=("a.py",), suggested_test_command="true")],
        filed=[],
        blocked={},
        merged=[],
    )
    roles = FakeRoles()

    outcome = await EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=runner,
        registry=registry,  # type: ignore[arg-type]
        roles=roles,
        chainlink=chainlink,  # type: ignore[arg-type]
    )._build_review_merge_slice(
        leaf=chainlink.leaves[0],
        epic=chainlink.epic,
        manifest=manifest,
        integration=integration,
        config=WorklinkConfig.load(tmp_path / "missing-worklink.yaml"),
        registry=registry,  # type: ignore[arg-type]
        repo_url="git@github.com:jasoncarreira/mimir.git",
        repo_slug="jasoncarreira/mimir",
        backend_name=None,
        roles=roles,
        chainlink=chainlink,  # type: ignore[arg-type]
        runner=runner,
        autonomous=False,
    )

    assert outcome.blocked_reason is None
    assert sleeps
    assert 0 < sleeps[0] <= 60
    assert registry.backend.calls == 2


@pytest.mark.asyncio
async def test_parallel_slice_manifest_updates_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_git(monkeypatch, tmp_path)
    import mimir.worklink.epic as epic_mod
    from mimir.worklink.epic_state import load_epic_state, save_epic_state

    # This simulates the stale fallback reads that used to make whole-manifest
    # writes from parallel slices clobber each other.
    monkeypatch.setattr(epic_mod, "_current_manifest", lambda home, fallback: fallback)
    arrived: set[int] = set()
    both_observing = asyncio.Event()

    async def observe_after_both_slices_start(**kw: object) -> EvidenceValidation:
        leaf = kw["leaf"]
        spec = kw["spec"]
        assert isinstance(leaf, LeafIssue)
        assert isinstance(spec, WorkSpec)
        arrived.add(leaf.issue.issue_id)
        if len(arrived) == 2:
            both_observing.set()
        await asyncio.wait_for(both_observing.wait(), timeout=1)
        return ready_validation(leaf.issue.issue_id, spec.attempt)

    monkeypatch.setattr(epic_mod, "_observe_slice", observe_after_both_slices_start)
    integration = IntegrationBranchLease(
        100,
        Path("/repo"),
        tmp_path / "integration",
        "epic/100-integration",
        "main",
        "main",
    )
    integration.path.mkdir(exist_ok=True)
    manifest = EpicRunManifest(
        epic_id=100,
        integration_branch=integration.branch,
        integration_worktree=str(integration.path),
        base_ref="main",
        phase="build",
        slices=[EpicSliceRecord(id=101), EpicSliceRecord(id=102)],
    )
    save_epic_state(tmp_path, manifest)
    leaves = [
        LeafIssue(issue(101), scope_paths=("a.py",), suggested_test_command="true"),
        LeafIssue(issue(102), scope_paths=("b.py",), suggested_test_command="true"),
    ]
    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:epic"}),
        leaves=leaves,
        filed=[],
        blocked={},
        merged=[],
    )
    registry = FakeRegistry()
    roles = FakeRoles()
    runner_instance = EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=runner,
        registry=registry,  # type: ignore[arg-type]
        roles=roles,
        chainlink=chainlink,  # type: ignore[arg-type]
    )

    outcomes = await asyncio.gather(
        *[
            runner_instance._build_review_merge_slice(
                leaf=leaf,
                epic=chainlink.epic,
                manifest=manifest,
                integration=integration,
                config=WorklinkConfig.load(tmp_path / "missing-worklink.yaml"),
                registry=registry,  # type: ignore[arg-type]
                repo_url="git@github.com:jasoncarreira/mimir.git",
                repo_slug="jasoncarreira/mimir",
                backend_name=None,
                roles=roles,
                chainlink=chainlink,  # type: ignore[arg-type]
                runner=runner,
                autonomous=False,
            )
            for leaf in leaves
        ]
    )

    saved = load_epic_state(tmp_path, 100)
    assert saved is not None
    records = {record.id: record for record in saved.slices}
    assert [outcome.blocked_reason for outcome in outcomes] == [None, None]
    assert set(chainlink.merged) == {101, 102}
    assert set(records) == {101, 102}
    assert {record.status for record in records.values()} == {"merged"}
    assert {record.attempts for record in records.values()} == {1}
    assert all(record.review_ref == "approve" for record in records.values())
    assert all(record.evidence_ref for record in records.values())


@pytest.mark.asyncio
async def test_decompose_then_build_files_child_leaves_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_git(monkeypatch, tmp_path)
    import mimir.worklink.epic as epic_mod

    monkeypatch.setattr(epic_mod, "_observe_slice", fake_observe_slice)
    decomposition = [
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
            depends_on=["Leaf A"],
        ),
    ]
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
    # Leaf B's depends_on=["Leaf A"] was wired into a chainlink blocker.
    assert chainlink.blocked


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


def test_chainlink_file_leaf_uses_strict_template_and_real_subissue_cli() -> None:
    calls: list[list[str]] = []

    def fake_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if list(args) == ["chainlink", "issue", "list", "--json"]:
            return cp(args, stdout="[]")
        if list(args)[:3] == ["chainlink", "issue", "subissue"]:
            return cp(args, stdout=json.dumps({"id": 123}))
        return cp(args, returncode=99, stderr="unexpected command")

    leaf = WorklinkLeafSpec(
        title="Leaf A",
        risk="high",
        acceptance_criteria=["A works"],
        review_criteria=["review A"],
        scope_paths=["mimir/worklink/epic.py", "tests/test_worklink_epic.py"],
        out_of_scope=["role runner"],
        suggested_test_command="env -u MIMIR_MODEL_SPEC uv run pytest -q tests/test_worklink_epic.py",
        labels=["worklink:ready"],
    )

    created = ChainlinkEpicClient(runner=fake_runner).file_leaf(100, leaf)

    assert created == 123
    subissue_call = calls[1]
    assert subissue_call[:5] == ["chainlink", "issue", "subissue", "100", "Leaf A"]
    assert "--parent" not in subissue_call
    assert "--title" not in subissue_call
    assert "--json" in subissue_call
    assert subissue_call.count("--label") == 2
    assert _label_values(subissue_call) == ["worklink:ready", "risk:high"]
    body = subissue_call[subissue_call.index("--description") + 1]
    assert missing_leaf_template_parts(body) == []
    issue_ctx = IssueContext(
        issue_id=123,
        title="Leaf A",
        description=body,
        labels={"worklink:ready"},
        parent_id=100,
        created_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    validate_leaf(issue_ctx)


def test_decomposer_high_risk_round_trips_through_file_leaf_labels_and_classifier() -> None:
    created: dict[str, Any] = {}

    def fake_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(args) == ["chainlink", "issue", "list", "--json"]:
            if not created:
                return cp(args, stdout="[]")
            return cp(
                args,
                stdout=json.dumps(
                    [
                        {
                            "id": 123,
                            "title": "Leaf A",
                            "description": created["description"],
                            "labels": created["labels"],
                            "parent_id": 100,
                            "blocked_by": [],
                            "created_at": "2026-07-02T00:00:00Z",
                        }
                    ]
                ),
            )
        if list(args)[:3] == ["chainlink", "issue", "subissue"]:
            created["description"] = list(args)[list(args).index("--description") + 1]
            created["labels"] = _label_values(list(args))
            return cp(args, stdout=json.dumps({"id": 123}))
        return cp(args, returncode=99, stderr="unexpected command")

    leaf = WorklinkLeafSpec(
        title="Leaf A",
        risk="high",
        acceptance_criteria=["A works"],
        review_criteria=["review A"],
        scope_paths=["docs/internal/WORKLINK.md"],
        suggested_test_command="true",
    )
    client = ChainlinkEpicClient(runner=fake_runner)

    assert client.file_leaf(100, leaf) == 123
    child = client.child_leaves(100)[0]

    assert "risk:high" in child.issue.labels
    assert child.scope_paths == ("docs/internal/WORKLINK.md",)
    from mimir.worklink.review import classify_leaf_review_risk

    assert (
        classify_leaf_review_risk(
            scope_paths=list(child.scope_paths),
            labels=child.issue.labels,
        )
        == "multi"
    )


def test_chainlink_move_epic_to_review_clears_parent_in_progress_label() -> None:
    calls: list[list[str]] = []

    def fake_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return cp(args)

    ChainlinkEpicClient(runner=fake_runner).move_epic_to_review(100)

    assert ["chainlink", "issue", "unlabel", "100", "worklink:ready"] in calls
    assert ["chainlink", "issue", "unlabel", "100", "worklink:in-progress"] in calls
    assert ["chainlink", "issue", "label", "100", "worklink:review"] in calls


def test_chainlink_file_leaf_is_idempotent_by_title() -> None:
    calls: list[list[str]] = []

    def fake_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        assert list(args) == ["chainlink", "issue", "list", "--json"]
        return cp(
            args,
            stdout=json.dumps(
                [
                    {
                        "id": 222,
                        "title": "Leaf A",
                        "description": leaf_description(),
                        "labels": ["worklink:ready"],
                        "parent_id": 100,
                        "created_at": "2026-07-02T00:00:00Z",
                    }
                ]
            ),
        )

    leaf = WorklinkLeafSpec(
        title="Leaf A",
        acceptance_criteria=["A works"],
        review_criteria=["review A"],
        scope_paths=["a.py"],
        suggested_test_command="true",
    )

    assert ChainlinkEpicClient(runner=fake_runner).file_leaf(100, leaf) == 222
    assert calls == [["chainlink", "issue", "list", "--json"]]


def test_chainlink_add_blocker_uses_real_cli_and_comments_reason() -> None:
    calls: list[list[str]] = []

    def fake_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return cp(args)

    ChainlinkEpicClient(runner=fake_runner).add_blocker(201, 200, "A first")

    assert calls[0] == ["chainlink", "issue", "block", "201", "200"]
    assert "--reason" not in calls[0]
    assert calls[1] == ["chainlink", "issue", "comment", "201", "WORKLINK_BLOCKED_BY #200: A first"]


def test_child_leaves_preserve_created_at_and_classify_high_risk_scope_multi() -> None:
    def fake_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return cp(
            args,
            stdout=json.dumps(
                [
                    {
                        "id": 101,
                        "title": "Leaf",
                        "description": """Acceptance criteria:\n- [ ] Works\n\nReview criteria:\n- Verify it\n\nWorklink notes:\n- Scope: services/auth/session.py, tests/test_worklink_epic.py\n- Out of scope: unrelated\n- Suggested test command: pytest -q\n""",
                        "labels": ["worklink:ready"],
                        "parent_id": 100,
                        "blocked_by": [],
                        "created_at": "2026-07-02T00:00:00Z",
                    }
                ]
            ),
        )

    leaf = ChainlinkEpicClient(runner=fake_runner).child_leaves(100)[0]

    assert leaf.issue.created_at == datetime(2026, 7, 2, tzinfo=UTC)
    assert leaf.scope_paths == ("services/auth/session.py", "tests/test_worklink_epic.py")
    from mimir.worklink.review import classify_leaf_review_risk

    assert classify_leaf_review_risk(scope_paths=list(leaf.scope_paths)) == "multi"


def test_created_issue_id_rejects_ambiguous_numeric_text() -> None:
    import mimir.worklink.epic as epic_mod

    with pytest.raises(Exception, match="deterministic issue id"):
        epic_mod._created_issue_id("1 issue created under parent #100")


@pytest.mark.asyncio
async def test_remote_epic_pushes_integration_before_observing_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.epic as epic_mod

    integration_path = tmp_path / "integration"
    integration_path.mkdir()
    monkeypatch.setattr(
        epic_mod,
        "create_integration_branch",
        lambda *a, **k: IntegrationBranchLease(100, Path("/repo"), integration_path, "epic/100-integration", "main", "main"),
    )
    slice_path = tmp_path / "slice-101"
    slice_path.mkdir()
    monkeypatch.setattr(
        epic_mod,
        "create_slice_worktree",
        lambda *a, **k: WorktreeLease(101, 1, Path("/repo"), slice_path, "issue/101-a1", "epic/100-integration", "HEAD"),
    )
    events: list[str] = []

    def fake_push(path: Path, branch: str, *, runner: object) -> None:
        events.append(f"push:{branch}")

    async def observed_remote(**kw: object) -> EvidenceValidation:
        events.append("observe")
        return ready_validation(101, 1)

    monkeypatch.setattr(epic_mod, "_git_push", fake_push)
    monkeypatch.setattr(epic_mod, "_observe_slice", observed_remote)
    monkeypatch.setattr(epic_mod, "merge_slice_into_integration", lambda *a, **k: SliceMergeSuccess("issue/101-a1", "epic/100-integration", "abc123"))
    monkeypatch.setattr(epic_mod, "_run_epic_tests", lambda *a, **k: EpicTestStatus("true", 0, "ok"))
    monkeypatch.setattr(epic_mod, "_open_epic_pr", lambda *a, **k: "https://github.com/o/r/pull/1")

    class RemoteCompute(FakeCompute):
        name = "remote_compute"

        def capabilities(self) -> ComputeCaps:
            return ComputeCaps(shared_filesystem=False, network_isolated=True, handle_cancel=True, persistent_after_disconnect=True)

    class RemoteRegistry(FakeRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.compute = RemoteCompute()

    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:epic", "worklink:ready"}),
        leaves=[LeafIssue(issue(101), scope_paths=("a.py",), suggested_test_command="true")],
        filed=[],
        blocked={},
        merged=[],
    )

    def remote_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(args)[:4] == ["git", "-C", str(slice_path), "diff"]:
            return cp(args, stdout="changed.txt\n")
        return runner(args)

    result = await EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=remote_runner,
        registry=RemoteRegistry(),  # type: ignore[arg-type]
        roles=FakeRoles(),
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(100)

    assert result.status == "completed"
    assert events[:3] == ["push:epic/100-integration", "push:issue/101-a1", "observe"]
    assert "push:epic/100-integration" in events[3:]


@pytest.mark.asyncio
async def test_no_go_blocks_even_for_partial_epic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_git(monkeypatch, tmp_path)
    (tmp_path / "worklink.yaml").write_text("defaults:\n  max_review_retries: 1\n  test_command: true\n", encoding="utf-8")
    import mimir.worklink.epic as epic_mod

    monkeypatch.setattr(epic_mod, "_observe_slice", fake_observe_slice)

    class NoGoRoles(FakeRoles):
        async def validate_integration(self, **kwargs: object) -> IntegrationDecision:
            self.validations += 1
            return IntegrationDecision(approved=False, summary="missing required slice")

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
        roles=NoGoRoles(reviews=["REJECT"]),
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(100)

    assert result.status == "blocked"
    assert result.reason == "missing required slice"
    assert chainlink.moved_to_review is False


@pytest.mark.asyncio
async def test_partial_epic_records_failing_tests_in_pr_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_git(monkeypatch, tmp_path)
    (tmp_path / "worklink.yaml").write_text("defaults:\n  max_review_retries: 1\n  test_command: false\n", encoding="utf-8")
    import mimir.worklink.epic as epic_mod

    bodies: list[str] = []
    monkeypatch.setattr(epic_mod, "_observe_slice", fake_observe_slice)
    monkeypatch.setattr(epic_mod, "_run_epic_tests", lambda *a, **k: EpicTestStatus("false", 1, "blocked leaf absent"))

    def capture_pr(*_: object, **kwargs: object) -> str:
        test_status = kwargs["test_status"]
        blocked = kwargs["blocked"]
        assert isinstance(test_status, EpicTestStatus)
        bodies.append(f"{test_status.exit_code}:{test_status.summary}:{sorted(blocked)}")
        return "https://github.com/o/r/pull/1"

    monkeypatch.setattr(epic_mod, "_open_epic_pr", capture_pr)
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
        roles=FakeRoles(reviews=["REJECT"]),
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(100)

    assert result.status == "partial"
    assert bodies == ["1:blocked leaf absent:[101]"]


@pytest.mark.asyncio
async def test_resume_missing_integration_worktree_rematerializes_to_merge_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.epic as epic_mod
    from mimir.worklink.epic_state import EpicRunManifest, EpicSliceRecord, save_epic_state

    manifest = EpicRunManifest(
        epic_id=100,
        integration_branch="epic/100-integration",
        integration_worktree=str(tmp_path / "missing-integration"),
        base_ref="main",
        phase="build",
        slices=[EpicSliceRecord(id=101, status="merged", merge_commit="abc123"), EpicSliceRecord(id=102)],
    )
    save_epic_state(tmp_path, manifest)
    calls: list[list[str]] = []

    def resume_runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if isinstance(args, str):
            return cp([args], stdout="ok")
        calls.append(list(args))
        if list(args)[:4] == ["git", "-C", "/repo", "worktree"]:
            Path(args[6]).mkdir(parents=True, exist_ok=True)
            return cp(args)
        if list(args)[:3] == ["git", "-C", str(tmp_path / "missing-integration")]:
            if "rev-parse" in args:
                return cp(args, stdout="abc123\n")
            return cp(args)
        return runner(args)

    monkeypatch.setattr(epic_mod, "create_slice_worktree", lambda *a, **k: WorktreeLease(102, 1, Path("/repo"), tmp_path / "slice-102", "issue/102-a1", "epic/100-integration", "HEAD"))
    (tmp_path / "slice-102").mkdir()
    monkeypatch.setattr(epic_mod, "_observe_slice", fake_observe_slice)
    monkeypatch.setattr(epic_mod, "_commit_worktree_changes", lambda *a, **k: None)
    monkeypatch.setattr(epic_mod, "_git_push", lambda *a, **k: None)
    monkeypatch.setattr(epic_mod, "merge_slice_into_integration", lambda *a, **k: SliceMergeSuccess("issue/102-a1", "epic/100-integration", "def456"))
    monkeypatch.setattr(epic_mod, "_run_epic_tests", lambda *a, **k: EpicTestStatus("true", 0, "ok"))
    monkeypatch.setattr(epic_mod, "_open_epic_pr", lambda *a, **k: "https://github.com/o/r/pull/1")

    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:epic"}),
        leaves=[LeafIssue(issue(101), scope_paths=("a.py",), suggested_test_command="true"), LeafIssue(issue(102), scope_paths=("b.py",), suggested_test_command="true")],
        filed=[],
        blocked={},
        merged=[],
    )

    result = await EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=resume_runner,
        registry=FakeRegistry(),  # type: ignore[arg-type]
        roles=FakeRoles(),
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(100)

    assert result.status == "completed"
    assert any(cmd[:5] == ["git", "-C", "/repo", "worktree", "add"] and cmd[-1] == "abc123" for cmd in calls)


@pytest.mark.asyncio
async def test_run_crash_mid_build_relabels_epic_and_rearms_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_git(monkeypatch, tmp_path)
    (tmp_path / "worklink.yaml").write_text(
        "defaults:\n  max_review_retries: 2\n  test_command: true\n",
        encoding="utf-8",
    )
    import mimir.worklink.epic as epic_mod
    from mimir.worklink.epic_state import load_epic_state

    async def crash_observe(**_: object) -> EvidenceValidation:
        raise RuntimeError("worker vanished")

    monkeypatch.setattr(epic_mod, "_observe_slice", crash_observe)
    calls: list[list[str]] = []

    def recording_runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if isinstance(args, str):
            return cp([args])
        calls.append(list(args))
        if list(args) == ["chainlink", "issue", "show", "100", "--json"]:
            return cp(
                args,
                stdout=json.dumps(
                    {
                        "id": 100,
                        "title": "Epic",
                        "description": "epic",
                        "labels": ["worklink:epic", "worklink:ready"],
                        "comments": [],
                    }
                ),
            )
        if list(args) == ["chainlink", "issue", "list", "--json"]:
            return cp(
                args,
                stdout=json.dumps(
                    [
                        {
                            "id": 101,
                            "title": "Leaf",
                            "description": leaf_description(),
                            "labels": ["worklink:ready"],
                            "parent_id": 100,
                            "blocked_by": [],
                        }
                    ]
                ),
            )
        return runner(args)

    with pytest.raises(RuntimeError, match="worker vanished"):
        await EpicRunner(
            home=tmp_path,
            repo=Path("/repo"),
            runner=recording_runner,
            registry=FakeRegistry(),  # type: ignore[arg-type]
            roles=FakeRoles(),
            chainlink=ChainlinkEpicClient(runner=recording_runner),
        ).run(100)

    manifest = load_epic_state(tmp_path, 100)
    assert manifest is not None
    assert manifest.status == "running"
    assert manifest.slices[0].status == "pending"
    assert manifest.slices[0].attempts == 0
    assert ["chainlink", "issue", "unlabel", "100", "worklink:in-progress"] in calls
    assert ["chainlink", "issue", "label", "100", "worklink:ready"] in calls
    assert any(
        call[:4] == ["chainlink", "issue", "comment", "100"]
        and call[-1].startswith("WORKLINK_EPIC_FAILED worker vanished")
        for call in calls
    )


@pytest.mark.asyncio
async def test_resume_exhausted_running_slice_rearms_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_git(monkeypatch, tmp_path)
    (tmp_path / "worklink.yaml").write_text(
        "defaults:\n  max_review_retries: 2\n  test_command: true\n",
        encoding="utf-8",
    )
    import mimir.worklink.epic as epic_mod
    from mimir.worklink.epic_state import EpicSliceRecord, save_epic_state

    integration_path = tmp_path / "integration"
    integration_path.mkdir(exist_ok=True)
    save_epic_state(
        tmp_path,
        EpicRunManifest(
            epic_id=100,
            integration_branch="epic/100-integration",
            integration_worktree=str(integration_path),
            base_ref="main",
            phase="build",
            slices=[EpicSliceRecord(id=101, status="running", attempts=2)],
        ),
    )
    monkeypatch.setattr(epic_mod, "_observe_slice", fake_observe_slice)
    comments: list[tuple[int, str]] = []
    registry = FakeRegistry()
    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:epic"}),
        leaves=[LeafIssue(issue(101), scope_paths=("a.py",), suggested_test_command="true")],
        filed=[],
        blocked={},
        merged=[],
        comments=comments,
    )

    result = await EpicRunner(
        home=tmp_path,
        repo=Path("/repo"),
        runner=runner,
        registry=registry,  # type: ignore[arg-type]
        roles=FakeRoles(),
        chainlink=chainlink,  # type: ignore[arg-type]
    ).run(100)

    assert result.status == "completed"
    assert [spec.attempt for spec in registry.compute.launched] == [2]
    assert (101, "WORKLINK_EPIC_FAILED crashed running slice re-armed for retry") in comments


def test_stale_integration_base_without_merged_slices_recreates_branch(tmp_path: Path) -> None:
    import mimir.worklink.epic as epic_mod
    from mimir.worklink.epic_state import EpicSliceRecord

    path = tmp_path / "integration"
    path.mkdir()
    manifest = EpicRunManifest(
        epic_id=100,
        integration_branch="epic/100-integration",
        integration_worktree=str(path),
        base_ref="main",
        phase="build",
        slices=[EpicSliceRecord(id=101)],
    )
    calls: list[list[str]] = []

    def stale_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if list(args) == ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"]:
            return cp(args, stdout="oldbase\n")
        if list(args) == ["git", "-C", "/repo", "rev-parse", "--verify", "--quiet", "origin/main"]:
            return cp(args, stdout="newbase\n")
        if list(args) == ["git", "-C", str(path), "merge-base", "--is-ancestor", "origin/main", "HEAD"]:
            return cp(args, returncode=1)
        return cp(args)

    lease = epic_mod._ensure_integration_worktree(Path("/repo"), manifest, runner=stale_runner)

    assert lease.branch == "epic/100-integration"
    assert ["git", "-C", str(path), "checkout", "-B", "epic/100-integration", "origin/main"] in calls
    assert ["git", "-C", str(path), "push", "-u", "--force-with-lease", "origin", "epic/100-integration"] in calls


def test_stale_integration_base_with_merged_slices_rebases_and_updates_manifest(tmp_path: Path) -> None:
    import mimir.worklink.epic as epic_mod
    from mimir.worklink.epic_state import EpicSliceRecord, load_epic_state, save_epic_state

    path = tmp_path / "integration"
    path.mkdir()
    manifest = EpicRunManifest(
        epic_id=100,
        integration_branch="epic/100-integration",
        integration_worktree=str(path),
        base_ref="main",
        phase="build",
        slices=[
            EpicSliceRecord(id=101, status="merged", merge_commit="oldmerge"),
            EpicSliceRecord(id=102),
        ],
    )
    save_epic_state(tmp_path, manifest)
    calls: list[list[str]] = []
    head_reads = 0

    def stale_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        nonlocal head_reads
        calls.append(list(args))
        if list(args) == ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"]:
            head_reads += 1
            return cp(args, stdout=("oldmerge\n" if head_reads == 1 else "newmerge\n"))
        if list(args) == ["git", "-C", "/repo", "rev-parse", "--verify", "--quiet", "origin/main"]:
            return cp(args, stdout="newbase\n")
        if list(args) == ["git", "-C", str(path), "merge-base", "--is-ancestor", "origin/main", "HEAD"]:
            return cp(args, returncode=1)
        if list(args) == ["git", "-C", str(path), "merge-base", "HEAD", "origin/main"]:
            return cp(args, stdout="oldbase\n")
        return cp(args)

    epic_mod._ensure_integration_worktree(Path("/repo"), manifest, home=tmp_path, runner=stale_runner)

    assert [
        "git",
        "-C",
        str(path),
        "rebase",
        "--rebase-merges",
        "--onto",
        "origin/main",
        "oldbase",
    ] in calls
    saved = load_epic_state(tmp_path, 100)
    assert saved is not None
    assert saved.slices[0].merge_commit == "newmerge"


def test_chainlink_comment_raises_on_failure() -> None:
    def failing_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(args)[1:3] == ["issue", "comment"]:
            return cp(args, returncode=1, stderr="comment failed")
        return cp(args)

    client = ChainlinkEpicClient(runner=failing_runner)

    with pytest.raises(Exception, match="comment failed"):
        client.comment(101, "WORKLINK_REVIEW_FIXES ...")


@pytest.mark.asyncio
async def test_decompose_deficiency_wins_over_leaves_from_prior_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (PR #1000 review): a mixed state — leaves present (e.g. from a
    crashed earlier attempt) AND a deficiency reported — must halt as deficient,
    not proceed to build a partial plan."""
    patch_git(monkeypatch, tmp_path)
    import mimir.worklink.epic as epic_mod

    monkeypatch.setattr(epic_mod, "_observe_slice", fake_observe_slice)

    class MixedRoles(FakeRoles):
        async def run_decompose(self, epic: IssueContext, *, chainlink: Any) -> DecomposeOutcome:
            chainlink.file_leaf(
                epic.issue_id,
                WorklinkLeafSpec(
                    title="Partial leaf",
                    acceptance_criteria=["works"],
                    review_criteria=["check"],
                    scope_paths=["a.py"],
                    suggested_test_command="true",
                ),
            )
            return DecomposeOutcome(filed_leaves=1, deficiency="brief has no usable outcome")

    chainlink = FakeChainlink(
        epic=issue(100, parent_id=None, labels={"worklink:epic"}),
        leaves=[],
        filed=[],
        blocked={},
        merged=[],
    )

    with pytest.raises(Exception, match="deficient"):
        await EpicRunner(
            home=tmp_path,
            repo=Path("/repo"),
            runner=runner,
            registry=FakeRegistry(),  # type: ignore[arg-type]
            roles=MixedRoles(decomposition=[]),
            chainlink=chainlink,  # type: ignore[arg-type]
        ).run(100)

    # Nothing was merged and the epic was not moved to review.
    assert chainlink.merged == []
