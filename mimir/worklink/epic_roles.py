"""Concrete EpicRoleRunner bridge for Worklink structured review roles."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, TypeVar

from langchain.tools import ToolRuntime
from pydantic import BaseModel

from .epic_state import EpicRunManifest
from .evidence import EvidenceValidation
from .orchestrator import IssueContext, WorklinkError
from .review import (
    DecomposeReview,
    IntegrationValidation,
    SliceReview,
    WorkDecomposition,
    build_worklink_review_subagents,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
SubagentInvoker = Callable[[str, str, type[ModelT]], Awaitable[ModelT]]


class EpicSubagentRoleRunner:
    """Run epic roles through DeepAgents structured subagents."""

    def __init__(
        self,
        *,
        home: Path,
        repo: Path | None = None,
        invoker: SubagentInvoker | None = None,
        model: Any | None = None,
    ) -> None:
        self.home = home
        self.repo = repo or Path.cwd()
        self._invoker = invoker
        self._model = model

    async def decompose(self, epic: IssueContext) -> WorkDecomposition:
        return await self._invoke(
            "work-decomposer",
            _render_decompose_input(epic),
            WorkDecomposition,
        )

    async def review_decomposition(
        self, epic: IssueContext, decomposition: WorkDecomposition
    ) -> DecomposeReview:
        return await self._invoke(
            "decompose-reviewer",
            _render_decompose_review_input(epic, decomposition),
            DecomposeReview,
        )

    async def review_slice(
        self,
        *,
        leaf: Any,
        evidence: EvidenceValidation,
        mode: str,
        reviewer_count: int,
    ) -> SliceReview:
        count = max(1, int(reviewer_count or 1))
        prompt = _render_slice_review_input(leaf=leaf, evidence=evidence, mode=mode)
        if count == 1:
            return await self._invoke("per-slice-reviewer", prompt, SliceReview)

        # Multi-reviewer mode is not a majority vote. Reusing the same role N
        # times is valuable because it can surface different candidate bugs; a
        # focused verify-dissent pass decides whether any dissent is real.
        reviews = await asyncio.gather(
            *(
                self._invoke(
                    "per-slice-reviewer",
                    (
                        f"{prompt}\n\nReviewer vote: {idx + 1} of {count}. "
                        "Run an independent adversarial review."
                    ),
                    SliceReview,
                )
                for idx in range(count)
            )
        )
        return await _aggregate_slice_reviews(
            reviews,
            verify_dissent=lambda verify_prompt: self._invoke(
                "per-slice-reviewer",
                verify_prompt,
                SliceReview,
            ),
        )

    async def validate_integration(
        self,
        *,
        epic: IssueContext,
        manifest: EpicRunManifest,
        partial: bool,
        blocked: Mapping[int, str],
    ) -> IntegrationValidation:
        return await self._invoke(
            "integration-validator",
            _render_integration_validation_input(
                epic=epic,
                manifest=manifest,
                partial=partial,
                blocked=blocked,
            ),
            IntegrationValidation,
        )

    async def _invoke(self, role: str, prompt: str, model_type: type[ModelT]) -> ModelT:
        invoker = self._invoker
        if invoker is None:
            invoker = DeepAgentsTaskSubagentInvoker(
                home=self.home,
                repo=self.repo,
                model=self._model,
            )
            self._invoker = invoker
        return await invoker(role, prompt, model_type)


class DeepAgentsTaskSubagentInvoker:
    """Invoke Worklink role subagents through DeepAgents' ``task`` tool."""

    def __init__(
        self,
        *,
        home: Path,
        repo: Path | None = None,
        model: Any | None = None,
    ) -> None:
        self.home = home
        self.repo = repo or Path.cwd()
        self._model = model
        self._tool: Any | None = None

    async def __call__(
        self, role: str, prompt: str, model_type: type[ModelT]
    ) -> ModelT:
        tool = self._tool or self._build_tool()
        self._tool = tool
        runtime = ToolRuntime(
            state={"messages": []},
            context=None,
            config={},
            stream_writer=lambda _: None,
            tool_call_id=f"worklink-epic-{role}",
            store=None,
        )
        try:
            result = await tool.coroutine(
                description=prompt,
                subagent_type=role,
                runtime=runtime,
            )
        except AttributeError as exc:
            raise WorklinkError(
                "DeepAgents task tool shape changed; cannot run Worklink epic "
                "role subagents. Update DeepAgents integration or pin a "
                "compatible deepagents version."
            ) from exc
        if isinstance(result, str):
            raise WorklinkError(result)
        update = getattr(result, "update", None)
        if not isinstance(update, Mapping):
            raise WorklinkError(
                "DeepAgents task tool returned an unexpected result shape; "
                "missing mapping update.messages"
            )
        messages = update.get("messages", [])
        if not messages:
            raise WorklinkError(f"{role} subagent returned no structured response")
        content = getattr(messages[0], "content", None)
        if content is None:
            raise WorklinkError(
                "DeepAgents task tool returned an unexpected message shape; "
                "missing message.content"
            )
        return model_type.model_validate_json(str(content))

    def _build_tool(self) -> Any:
        try:
            from deepagents.middleware import subagents as deepagents_subagents
        except ImportError as exc:
            raise WorklinkError(
                "DeepAgents subagent middleware is unavailable; install a "
                "compatible deepagents version to run Worklink epics"
            ) from exc

        build_task_tool = getattr(deepagents_subagents, "_build_task_tool", None)
        if build_task_tool is None:
            raise WorklinkError(
                "DeepAgents private _build_task_tool API is unavailable; "
                "update Worklink's epic role bridge or pin a compatible "
                "deepagents version"
            )

        model = self._model if self._model is not None else _resolve_epic_model(self.home)
        specs = [
            _structured_subagent_spec(spec, model=model, repo=self.repo)
            for spec in build_worklink_review_subagents()
        ]
        return build_task_tool(specs)


def _structured_subagent_spec(
    spec: dict[str, Any], *, model: Any, repo: Path
) -> dict[str, Any]:
    from deepagents.backends import FilesystemBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware

    return {
        **spec,
        "model": model,
        "middleware": [
            FilesystemMiddleware(
                backend=FilesystemBackend(root_dir=repo),
                _permissions=spec.get("permissions"),
            )
        ],
    }


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


async def _aggregate_slice_reviews(
    reviews: list[SliceReview],
    *,
    verify_dissent: Callable[[str], Awaitable[SliceReview]] | None = None,
) -> SliceReview:
    approvals = sum(1 for review in reviews if review.verdict == "APPROVE")
    dissents = [review for review in reviews if review.verdict == "REJECT"]
    findings = [finding for review in reviews for finding in review.findings]
    coverage = [mapping for review in reviews for mapping in review.ac_coverage]
    if not dissents:
        return SliceReview(
            verdict="APPROVE",
            summary=f"All {len(reviews)} slice reviewer(s) APPROVED; no dissent verification needed.",
            ac_coverage=coverage,
            findings=findings,
            required_fixes=[],
        )

    dissent_fixes = _unique(fix for review in dissents for fix in review.required_fixes)
    if verify_dissent is None:
        return SliceReview(
            verdict="REJECT",
            summary=(
                f"{len(dissents)}/{len(reviews)} slice reviewer(s) REJECTED; "
                "no dissent verifier was configured, so the dissent blocks."
            ),
            ac_coverage=coverage,
            findings=findings,
            required_fixes=dissent_fixes,
        )

    verification = await verify_dissent(_render_dissent_verification_input(reviews, dissents))
    if verification.verdict == "REJECT":
        verified_fixes = _unique([*verification.required_fixes, *dissent_fixes])
        return SliceReview(
            verdict="REJECT",
            summary=(
                f"{len(dissents)}/{len(reviews)} reviewer dissent(s) were double-checked; "
                "at least one dissenting finding was verified as real. "
                f"Verifier summary: {verification.summary}"
            ),
            ac_coverage=coverage or verification.ac_coverage,
            findings=[*findings, *verification.findings],
            required_fixes=verified_fixes,
        )

    return SliceReview(
        verdict="APPROVE",
        summary=(
            f"{len(dissents)}/{len(reviews)} reviewer dissent(s) were double-checked; "
            f"none survived verification. Verifier summary: {verification.summary}"
        ),
        ac_coverage=coverage or verification.ac_coverage,
        findings=findings,
        required_fixes=[],
    )


def _render_dissent_verification_input(
    reviews: list[SliceReview], dissents: list[SliceReview]
) -> str:
    return "\n".join(
        [
            "Verify the dissenting per-slice review findings below against only the controller-observed evidence already present in the review context.",
            "Return REJECT only if at least one dissenting finding is real and grounded in the observed diff/tests.",
            "Return APPROVE if every dissenting finding is spurious, unsupported, or not grounded in observed evidence.",
            "Do not decide by vote count; judge each dissenting finding on evidence.",
            "",
            "All slice reviews JSON:",
            json.dumps([review.model_dump(mode="json") for review in reviews], indent=2, sort_keys=True),
            "",
            "Dissenting reviews JSON:",
            json.dumps([review.model_dump(mode="json") for review in dissents], indent=2, sort_keys=True),
        ]
    )


def _unique(items: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


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


def _render_decompose_review_input(
    epic: IssueContext, decomposition: WorkDecomposition
) -> str:
    return "\n".join(
        [
            _render_decompose_input(epic),
            "",
            "Proposed WorkDecomposition JSON:",
            decomposition.model_dump_json(indent=2),
        ]
    )


def _render_slice_review_input(
    *, leaf: Any, evidence: EvidenceValidation, mode: str
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
    return "\n".join(
        [
            f"Leaf #{leaf.issue.issue_id}: {leaf.issue.title}",
            "",
            "Leaf issue description:",
            leaf.issue.description,
            "",
            f"Review mode: {mode}",
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
    "DeepAgentsTaskSubagentInvoker",
    "EpicSubagentRoleRunner",
    "_aggregate_slice_reviews",
    "_render_slice_review_input",
]
