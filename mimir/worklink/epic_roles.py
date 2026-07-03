"""Action-based EpicRoleRunner: epic roles act via epic-scoped tools.

Every role is a tool-calling agent, not a structured-output parser:

- The **decomposer** files child leaves directly (``file_leaf`` /
  ``add_dependency``) or reports a deficient brief (``comment_on_epic``).
- The **lead slice reviewer** records its decision via ``approve_slice`` /
  ``request_fixes`` (the latter also comments the fixes on the leaf). For
  high-risk (multi) slices it first fans out to independent, text-only
  sub-reviewers via ``spawn_reviewer`` and verifies any dissent itself.
- The **integration validator** records ``approve_integration`` /
  ``block_integration`` (the latter comments the reasons on the epic).

The orchestrator reads the recorded decisions and Chainlink state; all git,
merge, and PR machinery stays deterministic in :mod:`mimir.worklink.epic`.
Decisions are captured from tool closures — never parsed from model output —
and every review path fails closed (no decision recorded => not approved).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from pydantic import ValidationError

from .epic_state import EpicRunManifest
from .evidence import EvidenceValidation
from .orchestrator import IssueContext, WorklinkError
from .review import (
    INTEGRATION_VALIDATOR_PROMPT,
    LEAD_SLICE_REVIEWER_PROMPT,
    SUB_SLICE_REVIEWER_PROMPT,
    WORK_DECOMPOSER_PROMPT,
    DecomposeOutcome,
    IntegrationDecision,
    SliceDecision,
    WorklinkLeafSpec,
)

#: Lenses assigned to sub-reviewers in multi (high-risk) mode, in order.
REVIEW_LENSES: tuple[str, ...] = ("correctness", "scope", "testing", "security", "performance")

#: Factory that builds a tool-calling agent for a role. Injectable for tests.
AgentFactory = Callable[[str, str, Sequence[Any]], Any]


class ChainlinkEpicActions(Protocol):
    """The subset of ChainlinkEpicClient the role tools need."""

    def file_leaf(self, epic_id: int, leaf: Any) -> int: ...

    def add_blocker(self, blocked_leaf: int, blocker_leaf: int, reason: str) -> None: ...

    def comment(self, issue_id: int, text: str) -> None: ...


class _DecisionState:
    """Single-shot decision holder shared between tool closures and the runner."""

    def __init__(self) -> None:
        self.decision: Any | None = None

    def record(self, decision: Any) -> bool:
        if self.decision is not None:
            return False
        self.decision = decision
        return True


class _DecomposeState:
    def __init__(self) -> None:
        self.ids_by_title: dict[str, int] = {}
        self.filed = 0
        self.deficiency: str | None = None


def build_decompose_tools(
    *,
    epic_id: int,
    chainlink: ChainlinkEpicActions,
    state: _DecomposeState,
) -> list[Any]:
    """Epic-scoped tools for the decomposer: file leaves or report a bad brief."""

    from langchain.tools import tool

    @tool
    def file_leaf(
        title: str,
        acceptance_criteria: list[str],
        review_criteria: list[str],
        scope_paths: list[str],
        suggested_test_command: str,
        depends_on: list[str] | None = None,
        out_of_scope: list[str] | None = None,
        risk: str = "standard",
    ) -> str:
        """File one Worklink leaf as a child issue of this epic.

        depends_on may only reference the exact titles of leaves you have
        already filed in this session; file leaves in dependency order.
        """
        try:
            spec = WorklinkLeafSpec(
                title=title,
                acceptance_criteria=acceptance_criteria,
                review_criteria=review_criteria,
                scope_paths=scope_paths,
                suggested_test_command=suggested_test_command,
                depends_on=list(depends_on or []),
                out_of_scope=list(out_of_scope or []),
                risk="high" if str(risk).strip().lower() == "high" else "standard",
            )
        except ValidationError as exc:
            return f"ERROR: invalid leaf: {exc.errors(include_url=False)}"
        unknown = [dep for dep in spec.depends_on if dep not in state.ids_by_title]
        if unknown:
            return (
                f"ERROR: depends_on references titles not filed yet: {unknown}. "
                "File those leaves first, or fix the titles."
            )
        leaf_id = chainlink.file_leaf(epic_id, spec)
        state.ids_by_title[spec.title] = leaf_id
        state.filed += 1
        for dep_title in spec.depends_on:
            chainlink.add_blocker(
                leaf_id,
                state.ids_by_title[dep_title],
                f"{spec.title} depends on {dep_title}",
            )
        return f"Filed leaf #{leaf_id}: {spec.title}"

    @tool
    def add_dependency(blocked_title: str, blocker_title: str) -> str:
        """Add a missed dependency between two leaves you already filed."""
        blocked = state.ids_by_title.get(blocked_title)
        blocker = state.ids_by_title.get(blocker_title)
        if blocked is None or blocker is None:
            missing = [t for t, i in ((blocked_title, blocked), (blocker_title, blocker)) if i is None]
            return f"ERROR: unknown leaf title(s): {missing}. Use exact filed titles."
        if blocked == blocker:
            return "ERROR: a leaf cannot depend on itself."
        chainlink.add_blocker(blocked, blocker, f"{blocked_title} depends on {blocker_title}")
        return f"Dependency added: #{blocked} waits for #{blocker}."

    @tool
    def comment_on_epic(message: str) -> str:
        """Report that the epic brief is too deficient to plan from (file nothing)."""
        if state.deficiency is not None:
            return "ERROR: a deficiency was already reported."
        state.deficiency = message.strip() or "brief reported deficient (no detail given)"
        chainlink.comment(epic_id, f"WORKLINK_BRIEF_DEFICIENT: {state.deficiency}")
        return "Deficiency recorded on the epic."

    return [file_leaf, add_dependency, comment_on_epic]


def build_slice_decision_tools(
    *,
    leaf_id: int,
    chainlink: ChainlinkEpicActions,
    state: _DecisionState,
    spawn: Callable[[str], Awaitable[str]],
) -> list[Any]:
    """Decision + fan-out tools for the lead slice reviewer."""

    from langchain.tools import tool

    @tool
    def approve_slice(summary: str = "") -> str:
        """Approve this slice: the observed diff meets every acceptance criterion."""
        if not state.record(SliceDecision(approved=True, summary=summary.strip())):
            return "ERROR: a decision was already recorded for this slice."
        return "Decision recorded: APPROVED."

    @tool
    def request_fixes(fixes: list[str], summary: str = "") -> str:
        """Reject this slice and request concrete fixes (one per problem)."""
        cleaned = tuple(str(f).strip() for f in fixes if str(f).strip())
        if not cleaned:
            return "ERROR: provide at least one concrete fix."
        if not state.record(
            SliceDecision(approved=False, summary=summary.strip(), fixes=cleaned)
        ):
            return "ERROR: a decision was already recorded for this slice."
        chainlink.comment(
            leaf_id,
            "WORKLINK_REVIEW_FIXES requested by the slice reviewer:\n"
            + "\n".join(f"- {fix}" for fix in cleaned),
        )
        return "Decision recorded: fixes requested."

    @tool
    async def spawn_reviewer(lens: str) -> str:
        """Run one independent sub-reviewer with the given lens; returns its report."""
        return await spawn(lens)

    return [approve_slice, request_fixes, spawn_reviewer]


def build_integration_decision_tools(
    *,
    epic_id: int,
    chainlink: ChainlinkEpicActions,
    state: _DecisionState,
) -> list[Any]:
    """Decision tools for the integration validator."""

    from langchain.tools import tool

    @tool
    def approve_integration(summary: str = "") -> str:
        """Approve the integrated epic for a draft PR (note any nits in the summary)."""
        if not state.record(IntegrationDecision(approved=True, summary=summary.strip())):
            return "ERROR: a decision was already recorded."
        return "Decision recorded: APPROVED."

    @tool
    def block_integration(reasons: list[str], summary: str = "") -> str:
        """Block the integrated epic (one concrete reason per problem)."""
        cleaned = tuple(str(r).strip() for r in reasons if str(r).strip())
        if not cleaned:
            return "ERROR: provide at least one concrete reason."
        if not state.record(
            IntegrationDecision(approved=False, summary=summary.strip(), reasons=cleaned)
        ):
            return "ERROR: a decision was already recorded."
        chainlink.comment(
            epic_id,
            "WORKLINK_INTEGRATION_BLOCKED by the integration validator:\n"
            + "\n".join(f"- {reason}" for reason in cleaned),
        )
        return "Decision recorded: integration blocked."

    return [approve_integration, block_integration]


class EpicSubagentRoleRunner:
    """Run the epic roles as tool-calling agents over the repo (read-only FS)."""

    def __init__(
        self,
        *,
        home: Path,
        repo: Path | None = None,
        model: Any | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        self.home = home
        self.repo = repo or Path.cwd()
        self._model = model
        self._agent_factory = agent_factory

    async def run_decompose(
        self, epic: IssueContext, *, chainlink: ChainlinkEpicActions
    ) -> DecomposeOutcome:
        state = _DecomposeState()
        tools = build_decompose_tools(epic_id=epic.issue_id, chainlink=chainlink, state=state)
        await self._run_agent(
            "work-decomposer", WORK_DECOMPOSER_PROMPT, tools, _render_decompose_input(epic)
        )
        return DecomposeOutcome(filed_leaves=state.filed, deficiency=state.deficiency)

    async def review_slice(
        self,
        *,
        leaf: Any,
        evidence: EvidenceValidation,
        mode: str,
        reviewer_count: int,
        chainlink: ChainlinkEpicActions,
    ) -> SliceDecision:
        lenses = REVIEW_LENSES[: max(1, int(reviewer_count or 1))] if mode == "multi" else ()
        review_input = _render_slice_review_input(
            leaf=leaf, evidence=evidence, mode=mode, lenses=lenses
        )

        async def spawn(lens: str) -> str:
            result = await self._invoke_agent(
                "sub-slice-reviewer",
                SUB_SLICE_REVIEWER_PROMPT,
                [],
                f"Assigned lens: {lens}\n\n{review_input}",
            )
            return _final_text(result) or "(sub-reviewer returned no report)"

        state = _DecisionState()
        tools = build_slice_decision_tools(
            leaf_id=leaf.issue.issue_id, chainlink=chainlink, state=state, spawn=spawn
        )
        await self._run_agent(
            "lead-slice-reviewer", LEAD_SLICE_REVIEWER_PROMPT, tools, review_input
        )
        if isinstance(state.decision, SliceDecision):
            return state.decision
        # Fail closed: an adversarial gate that records nothing must not pass.
        return SliceDecision(
            approved=False,
            summary="reviewer completed without recording a decision",
            fixes=("re-run: the slice reviewer recorded no decision",),
        )

    async def validate_integration(
        self,
        *,
        epic: IssueContext,
        manifest: EpicRunManifest,
        partial: bool,
        blocked: Mapping[int, str],
        chainlink: ChainlinkEpicActions,
    ) -> IntegrationDecision:
        state = _DecisionState()
        tools = build_integration_decision_tools(
            epic_id=epic.issue_id, chainlink=chainlink, state=state
        )
        await self._run_agent(
            "integration-validator",
            INTEGRATION_VALIDATOR_PROMPT,
            tools,
            _render_integration_validation_input(
                epic=epic, manifest=manifest, partial=partial, blocked=blocked
            ),
        )
        if isinstance(state.decision, IntegrationDecision):
            return state.decision
        return IntegrationDecision(
            approved=False,
            summary="validator completed without recording a decision",
            reasons=("re-run: the integration validator recorded no decision",),
        )

    # ── agent plumbing ────────────────────────────────────────────────────

    async def _run_agent(
        self, name: str, system_prompt: str, tools: Sequence[Any], user_input: str
    ) -> None:
        await self._invoke_agent(name, system_prompt, tools, user_input)

    async def _invoke_agent(
        self, name: str, system_prompt: str, tools: Sequence[Any], user_input: str
    ) -> Any:
        from langchain_core.messages import HumanMessage

        factory = self._agent_factory or self._default_factory
        agent = factory(name, system_prompt, tools)
        return await agent.ainvoke({"messages": [HumanMessage(content=user_input)]})

    def _default_factory(self, name: str, system_prompt: str, tools: Sequence[Any]) -> Any:
        try:
            from langchain.agents import create_agent
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise WorklinkError(
                "langchain create_agent is unavailable; install a compatible "
                "langchain/deepagents version to run Worklink epics"
            ) from exc
        from deepagents.backends import FilesystemBackend
        from deepagents.middleware.filesystem import FilesystemMiddleware

        from mimir.subagents import readonly_filesystem_permissions

        if self._model is None:
            self._model = _resolve_epic_model(self.home)
        middleware = [
            FilesystemMiddleware(
                # virtual_mode=True confines the read-only role to the repo root
                # (blocks absolute/`..` escapes) and pins behavior across
                # deepagents' changing default.
                backend=FilesystemBackend(root_dir=self.repo, virtual_mode=True),
                _permissions=readonly_filesystem_permissions(),
            )
        ]
        return create_agent(
            self._model,
            system_prompt=system_prompt,
            tools=list(tools),
            middleware=middleware,
            name=name,
        )


def _resolve_epic_model(home: Path) -> Any:
    import os

    previous = os.environ.get("MIMIR_HOME")
    os.environ["MIMIR_HOME"] = str(home)
    try:
        from mimir.agent import resolve_model_from_config
        from mimir.config import Config

        return resolve_model_from_config(Config.from_env())
    finally:
        if previous is None:
            os.environ.pop("MIMIR_HOME", None)
        else:
            os.environ["MIMIR_HOME"] = previous


def _final_text(result: Any) -> str:
    """Best-effort extraction of the final assistant text from an agent run."""
    messages = result.get("messages") if isinstance(result, Mapping) else None
    if not messages:
        return ""
    content = getattr(messages[-1], "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        return "\n".join(p for p in parts if p)
    return str(content)


# ─── Role input rendering ───────────────────────────────────────────────────


def _render_decompose_input(epic: IssueContext) -> str:
    return "\n".join(
        [
            f"Epic #{epic.issue_id}: {epic.title}",
            "",
            "Labels:",
            ", ".join(sorted(epic.labels)) or "(none)",
            "",
            "Description:",
            epic.description,
            "",
            "Recent comments:",
            "\n".join(epic.comments[-10:]) or "(none)",
        ]
    )


def _render_slice_review_input(
    *, leaf: Any, evidence: EvidenceValidation, mode: str, lenses: Sequence[str] = ()
) -> str:
    observed = evidence.evidence
    tests = observed.tests
    test_lines = ["Observed test result:"]
    if tests is None:
        test_lines.append("- (missing)")
    else:
        test_lines.extend(
            [
                f"- Command: {tests.cmd or '(none)'}",
                f"- Exit code: {tests.exit_code}",
                f"- Summary: {tests.summary or '(none)'}",
                f"- Skipped reason: {tests.skipped_reason or '(none)'}",
                f"- Controller observed: {tests.observed}",
            ]
        )
    command_lines = [
        (
            f"- {cmd.cmd}: exit {cmd.exit_code}; "
            f"observed={cmd.observed}; summary={cmd.summary or '(none)'}"
        )
        for cmd in observed.commands
    ]
    if mode == "multi" and lenses:
        mode_line = (
            "Review mode: multi (high-risk) — call spawn_reviewer once for each of "
            f"these lenses before deciding: {', '.join(lenses)}."
        )
    else:
        mode_line = "Review mode: single — review directly yourself."
    return "\n".join(
        [
            f"Leaf #{leaf.issue.issue_id}: {leaf.issue.title}",
            "",
            "Leaf issue description:",
            leaf.issue.description,
            "",
            mode_line,
            "",
            (
                "Controller-OBSERVED evidence only. Do not use worker prose, "
                "worker summaries, or worker intent."
            ),
            f"Evidence validation status: {evidence.status}",
            f"Review ready: {evidence.review_ready}",
            f"Validation reasons: {', '.join(evidence.reasons) or '(none)'}",
            "",
            "Observed changed files:",
            "\n".join(f"- {path}" for path in observed.files_changed) or "- (none)",
            "",
            "Observed diff stat:",
            observed.diff_stat or "(none)",
            f"Diff controller observed: {observed.diff_observed}",
            "",
            "Observed controller commands:",
            "\n".join(command_lines) or "- (none)",
            "",
            *test_lines,
        ]
    )


def _render_integration_validation_input(
    *,
    epic: IssueContext,
    manifest: EpicRunManifest,
    partial: bool,
    blocked: Mapping[int, str],
) -> str:
    import json

    return "\n".join(
        [
            f"Epic #{epic.issue_id}: {epic.title}",
            "",
            "Epic description:",
            epic.description,
            "",
            f"Partial run: {partial}",
            "Blocked leaves:",
            json.dumps({str(k): v for k, v in sorted(blocked.items())}, indent=2),
            "",
            "Epic manifest JSON:",
            json.dumps(manifest.to_json(), indent=2, sort_keys=True),
        ]
    )


__all__ = [
    "EpicSubagentRoleRunner",
    "REVIEW_LENSES",
    "build_decompose_tools",
    "build_integration_decision_tools",
    "build_slice_decision_tools",
    "_render_slice_review_input",
]
