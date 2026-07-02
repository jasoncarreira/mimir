"""Structured Worklink review roles and review-risk classification."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from mimir.worklink.backends.registry import TieredReviewConfig
from mimir.worklink.planning import LEAF_TEMPLATE_MARKDOWN


class WorklinkLeafSpec(BaseModel):
    """One strict Worklink leaf emitted by the decomposer."""

    title: str = Field(description="Short Chainlink subissue title.")
    acceptance_criteria: list[str] = Field(
        min_length=1,
        description="Observable checklist items for the leaf's Acceptance criteria section.",
    )
    review_criteria: list[str] = Field(
        min_length=1,
        description="Reviewer/operator checks for the leaf's Review criteria section.",
    )
    scope_paths: list[str] = Field(
        min_length=1,
        description="Real repo paths or narrow subsystems expected to change.",
    )
    out_of_scope: list[str] = Field(
        default_factory=list,
        description="Nearby work explicitly excluded from this leaf.",
    )
    suggested_test_command: str = Field(
        description="Advisory validation command or evidence requirement.",
    )
    labels: list[str] = Field(
        default_factory=lambda: ["worklink:ready"],
        description="Labels to apply to the leaf.",
    )


class WorklinkBlockerEdge(BaseModel):
    """DAG edge: ``blocked_leaf`` cannot run until ``blocker_leaf`` is done."""

    blocked_leaf: str = Field(description="Title or stable id of the blocked leaf.")
    blocker_leaf: str = Field(description="Title or stable id of the prerequisite leaf.")
    reason: str = Field(description="Why this dependency is required.")


class WorklinkWave(BaseModel):
    """Leaves that may run together because their scope paths are disjoint."""

    wave: int = Field(ge=1)
    leaves: list[str] = Field(description="Leaf titles or stable ids in this wave.")
    serialized_hotspots: list[str] = Field(
        default_factory=list,
        description="Hotspot paths intentionally excluded from parallel execution.",
    )


class WorkDecomposition(BaseModel):
    """Structured output for the Worklink work-decomposer role."""

    summary: str = Field(description="Concise decomposition rationale.")
    leaves: list[WorklinkLeafSpec] = Field(min_length=1)
    blocked_by: list[WorklinkBlockerEdge] = Field(default_factory=list)
    waves: list[WorklinkWave] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    """One structured Worklink review finding."""

    title: str
    severity: Literal["nit", "important", "blocker"]
    evidence: str = Field(description="Specific prompt, diff, path, or test evidence.")
    recommendation: str = Field(description="Concrete required or suggested action.")


class DecomposeReview(BaseModel):
    """Structured output for the decompose-reviewer role."""

    verdict: Literal["APPROVE", "REJECT"]
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)


class SliceAcceptanceMapping(BaseModel):
    """Per-AC judgment grounded in observed slice evidence."""

    acceptance_criterion: str
    status: Literal["met", "unmet", "unclear"]
    evidence: str


class SliceReview(BaseModel):
    """Structured output for the adversarial per-slice reviewer role."""

    verdict: Literal["APPROVE", "REJECT"]
    summary: str
    ac_coverage: list[SliceAcceptanceMapping] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)
    required_fixes: list[str] = Field(default_factory=list)


class IntegrationAcceptanceMapping(BaseModel):
    """Whole-epic AC mapping to code and tests in the integrated diff."""

    acceptance_criterion: str
    code_refs: list[str] = Field(default_factory=list)
    test_refs: list[str] = Field(default_factory=list)
    status: Literal["met", "met_with_nits", "unmet", "unclear"]
    evidence: str


class IntegrationValidation(BaseModel):
    """Structured output for the holistic integration-validator role."""

    verdict: Literal["GO", "GO-WITH-NITS", "NO-GO"]
    summary: str
    ac_mappings: list[IntegrationAcceptanceMapping] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)


WORK_DECOMPOSER_PROMPT = f"""You are work-decomposer, the Worklink epic planner.

Given an epic brief and read-only repository access, return only the structured
WorkDecomposition response requested by the runtime. Decompose the epic into
small Worklink leaves that each satisfy this strict leaf template:

{LEAF_TEMPLATE_MARKDOWN}

Ground every Scope entry in real repository paths or narrow existing subsystems.
Target file-disjoint waves wherever possible. Serialize hotspots by adding
blocked_by edges and by keeping hotspot leaves out of the same wave. The
blocked_by list is a DAG: blocked_leaf waits for blocker_leaf.
"""


DECOMPOSE_REVIEWER_PROMPT = """You are decompose-reviewer, a skeptical Worklink plan reviewer.

Given the epic brief, proposed leaves, blocked-by DAG, and waves, return only the
structured DecomposeReview response requested by the runtime. APPROVE only when:
every epic acceptance criterion maps to at least one leaf; every leaf uses the
strict Worklink template fields; same-wave leaves are file-disjoint; hotspots are
serialized by dependencies or separate waves; and the dependency graph is a DAG.
Use REJECT with findings for missing AC coverage, vague Scope, same-wave file
overlap, unserialized hotspots, or invalid dependency structure.
"""


PER_SLICE_REVIEWER_PROMPT = """You are per-slice-reviewer, an adversarial Worklink reviewer.

Your job is to find reasons to REJECT. Judge only controller-OBSERVED evidence:
the actual diff, changed files, and controller-observed test results.
Do not trust worker prose. Do not trust worker claims, summaries, or intentions
when they conflict with or go beyond the observed evidence. APPROVE only if the
observed diff clearly meets every leaf acceptance criterion, stays inside Scope,
respects Out of scope, and has adequate observed validation. Return
required_fixes for every blocker.
"""


INTEGRATION_VALIDATOR_PROMPT = """You are integration-validator, the holistic Worklink reviewer.

Given the epic brief and the integrated diff, return only the structured
IntegrationValidation response requested by the runtime. Decide GO,
GO-WITH-NITS, or NO-GO for the whole integrated change. Map every epic
acceptance criterion to concrete code_refs and test_refs, and mark unclear or
unmet coverage explicitly. Use NO-GO for missing AC coverage, integration
conflicts, scope creep, or unvalidated behavior.
"""


ReviewVoteMode = Literal["single", "multi"]


def classify_leaf_review_risk(
    *,
    scope_paths: list[str] | tuple[str, ...],
    labels: set[str] | frozenset[str] | list[str] | tuple[str, ...] = (),
    tiered_review: TieredReviewConfig | None = None,
) -> ReviewVoteMode:
    """Return ``multi`` when a leaf touches S1's configured high-risk set."""

    config = tiered_review or TieredReviewConfig()
    normalized_labels = {str(label).strip().lower() for label in labels}
    high_risk_labels = {label.lower() for label in config.high_risk_labels}
    if normalized_labels & high_risk_labels:
        return "multi"

    prefixes = tuple(
        _normalize_scope_path(prefix).rstrip("/")
        for prefix in config.high_risk_scope_prefixes
        if str(prefix).strip()
    )
    for raw_path in scope_paths:
        path = _normalize_scope_path(raw_path)
        if any(_path_has_prefix(path, prefix) for prefix in prefixes):
            return "multi"
    return "single"


def _normalize_scope_path(path: str) -> str:
    normalized = PurePosixPath(str(path).strip().replace("\\", "/")).as_posix()
    return normalized.removeprefix("./")


def _path_has_prefix(path: str, prefix: str) -> bool:
    return path.startswith(prefix)


def build_worklink_review_subagents() -> list[dict]:
    """Build Worklink structured review subagent specs for DeepAgents."""

    from mimir.subagents import readonly_filesystem_permissions

    base = {
        "tools": [],
        "permissions": readonly_filesystem_permissions(),
    }
    return [
        {
            **base,
            "name": "work-decomposer",
            "description": (
                "Worklink epic decomposer returning strict leaf specs, blocked-by DAG, "
                "and file-disjoint waves. Read-only filesystem profile."
            ),
            "system_prompt": WORK_DECOMPOSER_PROMPT,
            "response_format": WorkDecomposition,
        },
        {
            **base,
            "name": "decompose-reviewer",
            "description": (
                "Worklink plan reviewer checking AC coverage, file-disjoint waves, "
                "hotspot serialization, and DAG validity. Read-only filesystem profile."
            ),
            "system_prompt": DECOMPOSE_REVIEWER_PROMPT,
            "response_format": DecomposeReview,
        },
        {
            **base,
            "name": "per-slice-reviewer",
            "description": (
                "Adversarial Worklink slice reviewer that judges observed diff and "
                "test evidence, not worker prose. Read-only filesystem profile."
            ),
            "system_prompt": PER_SLICE_REVIEWER_PROMPT,
            "response_format": SliceReview,
        },
        {
            **base,
            "name": "integration-validator",
            "description": (
                "Holistic Worklink integration validator mapping epic ACs to code "
                "and tests. Read-only filesystem profile."
            ),
            "system_prompt": INTEGRATION_VALIDATOR_PROMPT,
            "response_format": IntegrationValidation,
        },
    ]
