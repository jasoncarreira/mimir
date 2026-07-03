"""Structured Worklink review roles and review-risk classification."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from mimir.worklink.backends.registry import TieredReviewConfig


class WorklinkLeafSpec(BaseModel):
    """One strict Worklink leaf emitted by the decomposer.

    Identity is the ``title``: other leaves reference it in their ``depends_on``.
    Dependencies live on the leaf — there is no separate edge list or wave
    structure. The orchestrator derives execution waves from the resulting
    Chainlink blocked-by DAG.
    """

    title: str = Field(
        description=(
            "Short, UNIQUE Chainlink subissue title. Other leaves reference this "
            "exact title in their depends_on."
        ),
    )
    acceptance_criteria: list[str] = Field(
        min_length=1,
        description="Non-empty list of observable checklist items (Acceptance criteria).",
    )
    review_criteria: list[str] = Field(
        min_length=1,
        description="Non-empty list of reviewer/operator checks (Review criteria).",
    )
    scope_paths: list[str] = Field(
        min_length=1,
        description=(
            "REQUIRED, never empty. Real repo paths or dirs this leaf changes "
            "(e.g. 'mimir/worklink/foo.py'), grounded in the actual repository."
        ),
    )
    suggested_test_command: str = Field(
        description="REQUIRED. A single validation command or evidence requirement.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "Exact titles of other leaves in THIS decomposition that must finish "
            "before this one (this is how ordering/serialization is expressed). "
            "Empty list if none. Must form a DAG."
        ),
    )
    out_of_scope: list[str] = Field(
        default_factory=list,
        description="Nearby work explicitly excluded from this leaf.",
    )
    risk: Literal["standard", "high"] = Field(
        default="standard",
        description=(
            "Review risk. 'high' => security/auth/secrets, migrations/prod-data, "
            "generated code, or architecturally central/hard-to-reverse/hotspot; "
            "otherwise 'standard'."
        ),
    )
    labels: list[str] = Field(
        default_factory=lambda: ["worklink:ready"],
        description="Labels to apply to the leaf.",
    )


class WorkDecomposition(BaseModel):
    """Structured output for the Worklink work-decomposer role.

    Exactly two fields: ``summary`` and a non-empty ``leaves`` list. Ordering and
    serialization are expressed per-leaf via ``depends_on`` — there is no ``waves``
    or ``blocked_by`` edge structure.
    """

    summary: str = Field(description="Concise decomposition rationale.")
    leaves: list[WorklinkLeafSpec] = Field(min_length=1)


class DecomposeReview(BaseModel):
    """Structured output for the decompose-reviewer role.

    Flat by design: ``verdict`` + ``summary`` drive the decision; ``findings`` is
    a plain list of short strings (no nested objects) so the model can conform
    reliably.
    """

    verdict: Literal["APPROVE", "REJECT"]
    summary: str
    findings: list[str] = Field(
        default_factory=list,
        description="Short plain-text findings explaining the verdict (empty on a clean APPROVE).",
    )


class SliceReview(BaseModel):
    """Structured output for the adversarial per-slice reviewer role."""

    verdict: Literal["APPROVE", "REJECT"]
    summary: str
    findings: list[str] = Field(
        default_factory=list,
        description="Short plain-text findings (empty on a clean APPROVE).",
    )
    required_fixes: list[str] = Field(
        default_factory=list,
        description="Concrete fixes the builder must make; non-empty on REJECT.",
    )


class IntegrationValidation(BaseModel):
    """Structured output for the holistic integration-validator role."""

    verdict: Literal["GO", "GO-WITH-NITS", "NO-GO"]
    summary: str
    findings: list[str] = Field(
        default_factory=list,
        description="Short plain-text findings (empty on a clean GO). Note AC gaps here.",
    )


WORK_DECOMPOSER_PROMPT = """You are work-decomposer, the Worklink epic planner.

Given an epic brief and read-only repository access, return ONLY the structured
WorkDecomposition response the runtime requests. It has EXACTLY two top-level
fields: `summary` (string) and `leaves` (a non-empty list). There is NO
`blocked_by`, `waves`, `id`, `leaf_ids`, or any other top-level field.

Each item in `leaves` MUST be an object with these EXACT field names:
- `title`: short, UNIQUE string. Other leaves reference this exact title in
  their `depends_on`. There is no `id` field — the title IS the identifier.
- `acceptance_criteria`: non-empty list of observable strings.
- `review_criteria`: non-empty list of reviewer-check strings.
- `scope_paths`: non-empty list of REAL repo paths/dirs this leaf changes
  (e.g. "mimir/worklink/foo.py", "frontend/src/Chat.tsx"). Never omit or leave
  empty; ground each path in the actual repository.
- `suggested_test_command`: a single string (validation command or evidence
  requirement). Never omit.
- `depends_on`: list of the EXACT titles of other leaves that must finish before
  this one (empty list if none). This is how ordering and hotspot serialization
  are expressed — do NOT emit waves or edge objects.
- `out_of_scope`: list of strings (may be empty).
- `risk`: "high" or "standard". Use "high" when the slice touches
  security/auth/secrets, migrations/prod-data, or generated code, OR is
  architecturally central, hard to reverse, or a shared hotspot; else "standard".

Keep leaves small. Leaves that can run in parallel must be file-disjoint (no
shared scope_paths); serialize a hotspot by putting its prerequisite leaf's title
in the dependent leaf's `depends_on`. `depends_on` must form a DAG (no cycles)
and reference only titles that appear in `leaves`. Every epic acceptance
criterion must map to at least one leaf.
"""


DECOMPOSE_REVIEWER_PROMPT = """You are decompose-reviewer, a skeptical Worklink plan reviewer.

Given the epic brief and the proposed leaves (each carrying its own `depends_on`),
return only the structured DecomposeReview response. APPROVE only when: every
epic acceptance criterion maps to at least one leaf; every leaf has a non-empty
`scope_paths` and a `suggested_test_command`; leaves that can run together (those
with no unmet `depends_on`) are file-disjoint (no shared scope_paths); hotspots
are serialized via `depends_on`; and `depends_on` forms a DAG that references only
titles present in the plan. Use REJECT with findings for missing AC coverage,
vague or empty Scope, parallel file overlap, unserialized hotspots, or
invalid/cyclic dependencies.
"""


PER_SLICE_REVIEWER_PROMPT = """You are per-slice-reviewer, an adversarial Worklink reviewer.

Your job is to find reasons to REJECT. Judge ONLY controller-OBSERVED evidence:
the actual diff, changed files, and controller-observed test results. Do not
trust worker prose, claims, summaries, or intentions that go beyond the observed
evidence. APPROVE only if the observed diff clearly meets every leaf acceptance
criterion, stays inside Scope, respects Out of scope, and has adequate observed
validation.

Return only the structured SliceReview with these fields: `verdict` ("APPROVE"
or "REJECT"), `summary` (string), `findings` (a flat list of short plain-text
strings, empty on a clean APPROVE), and `required_fixes` (a flat list of strings;
include a concrete fix for every blocker when you REJECT). Every list field holds
plain strings — do NOT emit nested finding/mapping objects.
"""


INTEGRATION_VALIDATOR_PROMPT = """You are integration-validator, the holistic Worklink reviewer.

Given the epic brief and the integrated diff, decide GO, GO-WITH-NITS, or NO-GO
for the whole integrated change. Check that every epic acceptance criterion is
covered by the integrated code and tests, and call out any unclear or unmet
coverage. Use NO-GO for missing AC coverage, integration conflicts, scope creep,
or unvalidated behavior.

Return only the structured IntegrationValidation with these fields: `verdict`
("GO", "GO-WITH-NITS", or "NO-GO"), `summary` (string), and `findings` (a flat
list of short plain-text strings — note any AC gap or concern here; empty on a
clean GO). `findings` are plain strings — do NOT emit nested mapping objects.
"""


ReviewVoteMode = Literal["single", "multi"]


def classify_leaf_review_risk(
    *,
    scope_paths: list[str] | tuple[str, ...],
    labels: set[str] | frozenset[str] | list[str] | tuple[str, ...] = (),
    assigned_risk: Literal["standard", "high"] = "standard",
    tiered_review: TieredReviewConfig | None = None,
) -> ReviewVoteMode:
    """Return ``multi`` when assigned risk or config marks a leaf high-risk."""

    config = tiered_review or TieredReviewConfig()
    if assigned_risk == "high":
        return "multi"

    normalized_labels = {str(label).strip().lower() for label in labels}
    high_risk_labels = {label.lower() for label in config.high_risk_labels}
    if normalized_labels & high_risk_labels:
        return "multi"

    patterns = tuple(
        _normalize_scope_path(pattern)
        for pattern in config.high_risk_scope_patterns
        if str(pattern).strip()
    )
    for raw_path in scope_paths:
        path = _normalize_scope_path(raw_path)
        if any(_path_matches_pattern(path, pattern) for pattern in patterns):
            return "multi"
    return "single"


def _normalize_scope_path(path: str) -> str:
    normalized = PurePosixPath(str(path).strip().replace("\\", "/")).as_posix()
    return normalized.removeprefix("./")


def _path_matches_pattern(path: str, pattern: str) -> bool:
    return fnmatchcase(path, pattern) or fnmatchcase(f"/{path}", pattern)


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
                "Worklink epic decomposer returning strict leaf specs with per-leaf "
                "depends_on (file-disjoint parallel leaves, hotspots serialized). "
                "Read-only filesystem profile."
            ),
            "system_prompt": WORK_DECOMPOSER_PROMPT,
            "response_format": WorkDecomposition,
        },
        {
            **base,
            "name": "decompose-reviewer",
            "description": (
                "Worklink plan reviewer checking AC coverage, file-disjoint parallel "
                "leaves, hotspot serialization via depends_on, and DAG validity. "
                "Read-only filesystem profile."
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
