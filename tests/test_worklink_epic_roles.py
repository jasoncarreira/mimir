from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from mimir.worklink.epic import LeafIssue
from mimir.worklink.epic_roles import (
    REVIEW_LENSES,
    EpicSubagentRoleRunner,
    _DecisionState,
    _DecomposeState,
    build_decompose_tools,
    build_integration_decision_tools,
    build_slice_decision_tools,
)
from mimir.worklink.evidence import EvidenceValidation, TestResult, WorklinkEvidence
from mimir.worklink.orchestrator import IssueContext
from mimir.worklink.review import IntegrationDecision, SliceDecision


class FakeChainlinkActions:
    def __init__(self) -> None:
        self.filed: list[tuple[int, str]] = []
        self.blockers: list[tuple[int, int, str]] = []
        self.comments: list[tuple[int, str]] = []
        self._next_id = 100

    def file_leaf(self, epic_id: int, leaf: Any) -> int:
        self._next_id += 1
        self.filed.append((epic_id, leaf.title))
        return self._next_id

    def add_blocker(self, blocked_leaf: int, blocker_leaf: int, reason: str) -> None:
        self.blockers.append((blocked_leaf, blocker_leaf, reason))

    def comment(self, issue_id: int, text: str) -> None:
        self.comments.append((issue_id, text))


class ScriptedAgent:
    """Stub agent: runs a script against its tools, returns a fixed reply."""

    def __init__(self, tools: list[Any], script: Callable[..., Any] | None, reply: str = "") -> None:
        self.tools = {tool.name: tool for tool in tools}
        self.script = script
        self.reply = reply
        self.inputs: list[dict] = []

    async def ainvoke(self, state: dict) -> dict:
        self.inputs.append(state)
        if self.script is not None:
            result = self.script(self.tools)
            if inspect.isawaitable(result):
                await result
        return {"messages": [SimpleNamespace(content=self.reply)]}


def make_factory(
    scripts: dict[str, Callable[..., Any] | None], replies: dict[str, str] | None = None
):
    built: dict[str, ScriptedAgent] = {}

    def factory(name: str, system_prompt: str, tools: Any) -> ScriptedAgent:
        agent = ScriptedAgent(list(tools), scripts.get(name), (replies or {}).get(name, ""))
        agent.system_prompt = system_prompt  # type: ignore[attr-defined]
        built[name] = agent
        return agent

    return factory, built


def leaf_issue(issue_id: int = 101) -> LeafIssue:
    return LeafIssue(
        issue=IssueContext(
            issue_id=issue_id,
            title="Leaf",
            description="Acceptance criteria:\n- [ ] works\n",
            labels={"worklink:ready"},
            parent_id=100,
            comments=(),
        ),
        scope_paths=("a.py",),
        suggested_test_command="true",
    )


def ready_validation(issue_id: int = 101) -> EvidenceValidation:
    evidence = WorklinkEvidence(
        issue=issue_id,
        attempt=1,
        backend="codex",
        branch=f"issue/{issue_id}-a1",
        worktree="/tmp/wt",
        started_at="2026-07-03T00:00:00Z",
        finished_at="2026-07-03T00:01:00Z",
        files_changed=["a.py"],
        diff_stat="1 file changed",
        commands=[],
        tests=TestResult(cmd="true", exit_code=0, summary="ok", observed=True),
        pr_url=None,
        status="completed",
    )
    return EvidenceValidation("completed", True, (), evidence)


# ─── decompose tools ─────────────────────────────────────────────────────────


def test_file_leaf_files_and_wires_depends_on() -> None:
    chainlink = FakeChainlinkActions()
    state = _DecomposeState()
    tools = {t.name: t for t in build_decompose_tools(epic_id=99, chainlink=chainlink, state=state)}

    first = tools["file_leaf"].invoke(
        {
            "title": "Leaf A",
            "acceptance_criteria": ["a works"],
            "review_criteria": ["check a"],
            "scope_paths": ["a.py"],
            "suggested_test_command": "true",
        }
    )
    second = tools["file_leaf"].invoke(
        {
            "title": "Leaf B",
            "acceptance_criteria": ["b works"],
            "review_criteria": ["check b"],
            "scope_paths": ["b.py"],
            "suggested_test_command": "true",
            "depends_on": ["Leaf A"],
            "risk": "high",
        }
    )

    assert "Filed leaf #101" in first and "Filed leaf #102" in second
    assert chainlink.filed == [(99, "Leaf A"), (99, "Leaf B")]
    assert chainlink.blockers == [(102, 101, "Leaf B depends on Leaf A")]
    assert state.filed == 2 and state.deficiency is None


def test_file_leaf_returns_errors_instead_of_raising() -> None:
    chainlink = FakeChainlinkActions()
    state = _DecomposeState()
    tools = {t.name: t for t in build_decompose_tools(epic_id=99, chainlink=chainlink, state=state)}

    invalid = tools["file_leaf"].invoke(
        {
            "title": "Bad",
            "acceptance_criteria": [],
            "review_criteria": ["r"],
            "scope_paths": ["a.py"],
            "suggested_test_command": "true",
        }
    )
    unknown_dep = tools["file_leaf"].invoke(
        {
            "title": "Needs missing",
            "acceptance_criteria": ["x"],
            "review_criteria": ["r"],
            "scope_paths": ["c.py"],
            "suggested_test_command": "true",
            "depends_on": ["Nope"],
        }
    )

    assert invalid.startswith("ERROR: invalid leaf")
    assert unknown_dep.startswith("ERROR: depends_on references titles not filed yet")
    assert chainlink.filed == [] and state.filed == 0


def test_add_dependency_and_deficiency_comment() -> None:
    chainlink = FakeChainlinkActions()
    state = _DecomposeState()
    tools = {t.name: t for t in build_decompose_tools(epic_id=99, chainlink=chainlink, state=state)}
    tools["file_leaf"].invoke(
        {
            "title": "A",
            "acceptance_criteria": ["a"],
            "review_criteria": ["r"],
            "scope_paths": ["a.py"],
            "suggested_test_command": "true",
        }
    )
    tools["file_leaf"].invoke(
        {
            "title": "B",
            "acceptance_criteria": ["b"],
            "review_criteria": ["r"],
            "scope_paths": ["b.py"],
            "suggested_test_command": "true",
        }
    )

    ok = tools["add_dependency"].invoke({"blocked_title": "B", "blocker_title": "A"})
    bad = tools["add_dependency"].invoke({"blocked_title": "B", "blocker_title": "Zed"})

    assert "Dependency added" in ok and bad.startswith("ERROR: unknown leaf title")
    assert chainlink.blockers[-1][:2] == (102, 101)


def test_deficiency_comment_records_once_on_fresh_run() -> None:
    chainlink = FakeChainlinkActions()
    state = _DecomposeState()
    tools = {t.name: t for t in build_decompose_tools(epic_id=99, chainlink=chainlink, state=state)}

    note = tools["comment_on_epic"].invoke({"message": "no acceptance criteria in brief"})
    again = tools["comment_on_epic"].invoke({"message": "second"})

    assert "Deficiency recorded" in note
    assert state.deficiency == "no acceptance criteria in brief"
    assert again.startswith("ERROR: a deficiency was already reported")
    assert any("WORKLINK_BRIEF_DEFICIENT" in text for _, text in chainlink.comments)


def test_decompose_mode_is_mutually_exclusive_leaf_then_deficiency() -> None:
    chainlink = FakeChainlinkActions()
    state = _DecomposeState()
    tools = {t.name: t for t in build_decompose_tools(epic_id=99, chainlink=chainlink, state=state)}
    tools["file_leaf"].invoke(
        {
            "title": "A",
            "acceptance_criteria": ["a"],
            "review_criteria": ["r"],
            "scope_paths": ["a.py"],
            "suggested_test_command": "true",
        }
    )

    refused = tools["comment_on_epic"].invoke({"message": "actually the brief is bad"})

    assert refused.startswith("ERROR: leaves were already filed")
    assert state.deficiency is None
    assert not any("WORKLINK_BRIEF_DEFICIENT" in text for _, text in chainlink.comments)


def test_decompose_mode_is_mutually_exclusive_deficiency_then_leaf() -> None:
    chainlink = FakeChainlinkActions()
    state = _DecomposeState()
    tools = {t.name: t for t in build_decompose_tools(epic_id=99, chainlink=chainlink, state=state)}
    tools["comment_on_epic"].invoke({"message": "no acceptance criteria"})

    refused = tools["file_leaf"].invoke(
        {
            "title": "A",
            "acceptance_criteria": ["a"],
            "review_criteria": ["r"],
            "scope_paths": ["a.py"],
            "suggested_test_command": "true",
        }
    )

    assert refused.startswith("ERROR: a brief deficiency was already reported")
    assert chainlink.filed == [] and state.filed == 0
    assert state.deficiency == "no acceptance criteria"


# ─── decision tools ──────────────────────────────────────────────────────────


def test_slice_decision_records_once_and_comments_fixes() -> None:
    chainlink = FakeChainlinkActions()
    state = _DecisionState()

    async def no_spawn(lens: str) -> str:
        return ""

    tools = {
        t.name: t
        for t in build_slice_decision_tools(
            leaf_id=101, chainlink=chainlink, state=state, spawn=no_spawn
        )
    }
    empty = tools["request_fixes"].invoke({"fixes": ["  "]})
    recorded = tools["request_fixes"].invoke({"fixes": ["run the tests"], "summary": "not ready"})
    after = tools["approve_slice"].invoke({"summary": "late"})

    assert empty.startswith("ERROR: provide at least one concrete fix")
    assert "Decision recorded" in recorded
    assert after.startswith("ERROR: a decision was already recorded")
    assert state.decision == SliceDecision(
        approved=False, summary="not ready", fixes=("run the tests",)
    )
    assert any(
        "WORKLINK_REVIEW_FIXES" in text and "run the tests" in text
        for _, text in chainlink.comments
    )


@pytest.mark.asyncio
async def test_approve_slice_is_tool_gated_on_required_lenses() -> None:
    state = _DecisionState()

    async def spawn(lens: str) -> str:
        return "report"

    tools = {
        t.name: t
        for t in build_slice_decision_tools(
            leaf_id=101,
            chainlink=FakeChainlinkActions(),
            state=state,
            spawn=spawn,
            required_lenses=("correctness", "scope"),
        )
    }

    early = tools["approve_slice"].invoke({"summary": "lgtm"})
    assert early.startswith("ERROR: approval requires an independent sub-review")
    assert "correctness" in early and "scope" in early
    assert state.decision is None

    await tools["spawn_reviewer"].ainvoke({"lens": "correctness"})
    partial = tools["approve_slice"].invoke({"summary": "lgtm"})
    assert partial.startswith("ERROR") and "scope" in partial and "correctness" not in partial

    await tools["spawn_reviewer"].ainvoke({"lens": "scope"})
    done = tools["approve_slice"].invoke({"summary": "lgtm"})
    assert "Decision recorded: APPROVED" in done
    assert state.decision == SliceDecision(approved=True, summary="lgtm")


def test_request_fixes_is_never_lens_gated() -> None:
    state = _DecisionState()

    async def spawn(lens: str) -> str:
        return "report"

    tools = {
        t.name: t
        for t in build_slice_decision_tools(
            leaf_id=101,
            chainlink=FakeChainlinkActions(),
            state=state,
            spawn=spawn,
            required_lenses=("correctness", "scope", "testing"),
        )
    }

    rejected = tools["request_fixes"].invoke({"fixes": ["broken import"]})

    assert "Decision recorded" in rejected
    assert state.decision == SliceDecision(approved=False, summary="", fixes=("broken import",))


def test_audit_comment_failure_does_not_void_decision() -> None:
    class FailingComments(FakeChainlinkActions):
        def comment(self, issue_id: int, text: str) -> None:
            raise RuntimeError("chainlink unavailable")

    state = _DecisionState()

    async def spawn(lens: str) -> str:
        return ""

    tools = {
        t.name: t
        for t in build_slice_decision_tools(
            leaf_id=101, chainlink=FailingComments(), state=state, spawn=spawn
        )
    }

    recorded = tools["request_fixes"].invoke({"fixes": ["fix it"]})

    assert "Decision recorded" in recorded
    assert "WARNING: audit comment failed" in recorded
    assert state.decision == SliceDecision(approved=False, summary="", fixes=("fix it",))


def test_integration_decision_block_comments_epic() -> None:
    chainlink = FakeChainlinkActions()
    state = _DecisionState()
    tools = {
        t.name: t
        for t in build_integration_decision_tools(epic_id=99, chainlink=chainlink, state=state)
    }

    blocked = tools["block_integration"].invoke({"reasons": ["AC 2 uncovered"]})

    assert "Decision recorded" in blocked
    assert state.decision == IntegrationDecision(
        approved=False, summary="", reasons=("AC 2 uncovered",)
    )
    assert any("WORKLINK_INTEGRATION_BLOCKED" in text for _, text in chainlink.comments)


# ─── runner ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_decompose_returns_outcome_from_tool_state(tmp_path) -> None:
    chainlink = FakeChainlinkActions()

    def script(tools: dict) -> None:
        tools["file_leaf"].invoke(
            {
                "title": "Only leaf",
                "acceptance_criteria": ["works"],
                "review_criteria": ["check"],
                "scope_paths": ["x.py"],
                "suggested_test_command": "true",
            }
        )

    factory, built = make_factory({"work-decomposer": script})
    runner = EpicSubagentRoleRunner(home=tmp_path, repo=tmp_path, agent_factory=factory)
    epic = IssueContext(99, "Epic", "brief", {"worklink:epic"}, None, ())

    outcome = await runner.run_decompose(epic, chainlink=chainlink)

    assert outcome.filed_leaves == 1 and outcome.deficiency is None
    assert chainlink.filed == [(99, "Only leaf")]
    assert "work-decomposer" in built


@pytest.mark.asyncio
async def test_review_slice_returns_recorded_decision(tmp_path) -> None:
    def script(tools: dict) -> None:
        tools["approve_slice"].invoke({"summary": "meets every AC"})

    factory, built = make_factory({"lead-slice-reviewer": script})
    runner = EpicSubagentRoleRunner(home=tmp_path, repo=tmp_path, agent_factory=factory)

    decision = await runner.review_slice(
        leaf=leaf_issue(),
        evidence=ready_validation(),
        mode="single",
        reviewer_count=1,
        chainlink=FakeChainlinkActions(),
    )

    assert decision == SliceDecision(approved=True, summary="meets every AC")
    lead_input = built["lead-slice-reviewer"].inputs[0]["messages"][0].content
    assert "Review mode: single" in lead_input


@pytest.mark.asyncio
async def test_review_slice_fails_closed_without_decision(tmp_path) -> None:
    factory, _ = make_factory({"lead-slice-reviewer": None})
    runner = EpicSubagentRoleRunner(home=tmp_path, repo=tmp_path, agent_factory=factory)

    decision = await runner.review_slice(
        leaf=leaf_issue(),
        evidence=ready_validation(),
        mode="single",
        reviewer_count=1,
        chainlink=FakeChainlinkActions(),
    )

    assert decision.approved is False
    assert "no decision" in decision.summary or decision.fixes


@pytest.mark.asyncio
async def test_review_slice_multi_mode_spawns_lensed_subreviewers(tmp_path) -> None:
    reports: list[str] = []

    async def script(tools: dict) -> None:
        for lens in ("correctness", "scope", "testing"):
            reports.append(await tools["spawn_reviewer"].ainvoke({"lens": lens}))
        tools["approve_slice"].invoke({"summary": "dissent unsupported"})

    factory, built = make_factory(
        {"lead-slice-reviewer": script, "sub-slice-reviewer": None},
        replies={"sub-slice-reviewer": "no problems found through this lens"},
    )
    runner = EpicSubagentRoleRunner(home=tmp_path, repo=tmp_path, agent_factory=factory)

    decision = await runner.review_slice(
        leaf=leaf_issue(),
        evidence=ready_validation(),
        mode="multi",
        reviewer_count=3,
        chainlink=FakeChainlinkActions(),
    )

    assert decision.approved is True
    assert reports == ["no problems found through this lens"] * 3
    lead_input = built["lead-slice-reviewer"].inputs[0]["messages"][0].content
    assert "Review mode: multi" in lead_input
    assert ", ".join(REVIEW_LENSES[:3]) in lead_input
    # built[] keeps the last-spawned sub-reviewer (lens order is deterministic).
    sub_input = built["sub-slice-reviewer"].inputs[0]["messages"][0].content
    assert sub_input.startswith("Assigned lens: testing")
    assert "Leaf #101" in sub_input


@pytest.mark.asyncio
async def test_validate_integration_fail_closed_and_recorded(tmp_path) -> None:
    from mimir.worklink.epic_state import EpicRunManifest, EpicSliceRecord

    manifest = EpicRunManifest(
        epic_id=99,
        integration_branch="epic/99-integration",
        integration_worktree="/tmp/epic-99",
        base_ref="main",
        phase="integrate",
        slices=[EpicSliceRecord(id=101, status="merged", merge_commit="abc")],
    )
    epic = IssueContext(99, "Epic", "brief", {"worklink:epic"}, None, ())

    factory, _ = make_factory({"integration-validator": None})
    runner = EpicSubagentRoleRunner(home=tmp_path, repo=tmp_path, agent_factory=factory)
    silent = await runner.validate_integration(
        epic=epic, manifest=manifest, partial=False, blocked={}, chainlink=FakeChainlinkActions()
    )
    assert silent.approved is False

    def script(tools: dict) -> None:
        tools["approve_integration"].invoke({"summary": "all ACs covered"})

    factory2, _ = make_factory({"integration-validator": script})
    runner2 = EpicSubagentRoleRunner(home=tmp_path, repo=tmp_path, agent_factory=factory2)
    approved = await runner2.validate_integration(
        epic=epic, manifest=manifest, partial=False, blocked={}, chainlink=FakeChainlinkActions()
    )
    assert approved == IntegrationDecision(approved=True, summary="all ACs covered")
