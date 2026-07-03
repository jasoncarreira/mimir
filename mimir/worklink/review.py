"""Worklink epic role contracts: leaf spec, decisions, prompts, risk classification.

The epic roles are ACTION-based, not structured-output-based: each role agent
acts through small epic-scoped tools (file a leaf, approve a slice, request
fixes, block the integration) and the orchestrator reads the recorded decisions
plus Chainlink state. There is deliberately no large structured response schema
for any role — live runs showed models do not reliably conform to nested
one-shot schemas, while small tool calls are retryable and robust.

``WorklinkLeafSpec`` remains as the validated per-leaf carrier (it doubles as
the ``file_leaf`` tool's argument contract). The decision dataclasses below are
constructed by tool closures in :mod:`mimir.worklink.epic_roles`, never parsed
from model output.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from mimir.worklink.backends.registry import TieredReviewConfig


class WorklinkLeafSpec(BaseModel):
    """One strict Worklink leaf filed by the decomposer.

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
            "Exact titles of other leaves ALREADY FILED for this epic that must "
            "finish before this one (this is how ordering/serialization is "
            "expressed). Empty list if none. Must form a DAG."
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


# ─── Role decisions (constructed from tool calls, never model-parsed) ──────


@dataclass(frozen=True)
class DecomposeOutcome:
    """What the decompose agent did: filed leaves, or reported the brief deficient."""

    filed_leaves: int = 0
    deficiency: str | None = None


@dataclass(frozen=True)
class SliceDecision:
    """Lead slice reviewer's recorded decision for one built slice."""

    approved: bool
    summary: str = ""
    fixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegrationDecision:
    """Integration validator's recorded decision for the whole epic diff."""

    approved: bool
    summary: str = ""
    reasons: tuple[str, ...] = ()


# ─── Role prompts ───────────────────────────────────────────────────────────


WORK_DECOMPOSER_PROMPT = """You are work-decomposer, the Worklink epic planner.

You are given an epic brief and read-only repository access, plus these tools:
- file_leaf(...): file ONE Worklink leaf as a child issue of this epic.
- add_dependency(blocked_title, blocker_title): add a missed dependency between
  two leaves you already filed.
- comment_on_epic(message): report that the epic brief is too deficient to plan
  from (use at most once, and file nothing if you use it).

Study the brief and explore the repository to ground your plan in real paths.

If the brief is actionable, file EVERY leaf of your plan with file_leaf, in
dependency order — a leaf's depends_on may only name titles you have already
filed. Keep leaves small. Leaves that can run in parallel must be file-disjoint
(no shared scope_paths); serialize a shared hotspot by putting the prerequisite
leaf's title in the dependent leaf's depends_on. Set risk="high" for leaves
touching security/auth/secrets, migrations/prod-data, or generated code, or
that are architecturally central, hard to reverse, or a shared hotspot. Every
epic acceptance criterion must map to at least one leaf. If a tool call returns
an error, fix the arguments and retry it.

If (and only if) the brief lacks the essentials to plan from — no clear
outcome, no way to derive acceptance criteria, or internal contradictions —
call comment_on_epic once explaining exactly what is missing, and do not file
any leaves.

Your final text reply is ignored; only your tool calls have effect.
"""


LEAD_SLICE_REVIEWER_PROMPT = """You are the lead per-slice reviewer, an adversarial Worklink reviewer.

Your job is to find reasons to REJECT a built slice. Judge ONLY the
controller-OBSERVED evidence provided: the actual diff, changed files, and
controller-observed test results. Do not trust worker prose, claims, or
intentions that go beyond the observed evidence.

You MUST finish by calling exactly one decision tool:
- approve_slice(summary): the observed diff clearly meets every leaf acceptance
  criterion, stays inside Scope, respects Out of scope, and has adequate
  observed validation.
- request_fixes(fixes, summary): anything less. Include one concrete fix per
  problem.

If the review input says the mode is "multi" (a high-risk slice), FIRST call
spawn_reviewer once for each lens named in the review input. Each returns an
independent reviewer's plain-text report. Their reports are advisory: if ANY
report raises a concrete problem, verify that problem YOURSELF against the
observed evidence — if it is real and grounded, request_fixes; if it is
unsupported or speculative, do not let it block. Never decide by counting
votes. If the mode is "single", review directly yourself.

Your final text reply is ignored; only your decision tool call has effect.
"""


SUB_SLICE_REVIEWER_PROMPT = """You are an independent adversarial Worklink slice reviewer.

You are assigned a review lens and given controller-OBSERVED evidence for one
built slice: the leaf's acceptance criteria and scope, the observed diff and
changed files, and the controller-observed test results. Judge only that
evidence — not worker prose or intent.

Examine the slice strictly through your assigned lens and report, as plain
text: every concrete problem you find (with the specific evidence that shows
it), or a clear statement that you found no problems through this lens. Be
specific and terse; do not pad. Your entire reply is your report.
"""


INTEGRATION_VALIDATOR_PROMPT = """You are integration-validator, the holistic Worklink reviewer.

You are given the epic brief and the integrated result (manifest, merged
slices, blocked leaves). Decide whether the integrated change as a whole is
ready for a draft PR: every epic acceptance criterion covered by the integrated
code and tests, no integration conflicts, no scope creep, no unvalidated
behavior.

You MUST finish by calling exactly one decision tool:
- approve_integration(summary): ready (minor nits may be noted in the summary).
- block_integration(reasons, summary): not ready — give one concrete reason per
  problem (missing AC coverage, conflicts, scope creep, unvalidated behavior).

Your final text reply is ignored; only your decision tool call has effect.
"""


# ─── Review-risk classification ─────────────────────────────────────────────


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
