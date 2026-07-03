from __future__ import annotations

import pytest
from pydantic import ValidationError

from mimir.worklink.backends.registry import TieredReviewConfig
from mimir.worklink.review import (
    DECOMPOSE_REVIEWER_PROMPT,
    INTEGRATION_VALIDATOR_PROMPT,
    PER_SLICE_REVIEWER_PROMPT,
    WORK_DECOMPOSER_PROMPT,
    DecomposeReview,
    IntegrationValidation,
    SliceReview,
    WorkDecomposition,
    WorklinkLeafSpec,
    build_worklink_review_subagents,
    classify_leaf_review_risk,
)


def test_work_decomposer_schema_carries_strict_leaf_fields_and_dependencies() -> None:
    result = WorkDecomposition(
        summary="Split into implementation and wiring leaves.",
        leaves=[
            WorklinkLeafSpec(
                title="Add review schemas",
                acceptance_criteria=["Schemas validate all Worklink review roles"],
                review_criteria=["Verify structured response classes are registered"],
                scope_paths=["mimir/worklink/review.py"],
                out_of_scope=["Invoking roles from the orchestrator"],
                suggested_test_command='pytest -q tests/ -k "review"',
            ),
            WorklinkLeafSpec(
                title="Wire registration",
                acceptance_criteria=["Roles registered via build_mimir_subagents"],
                review_criteria=["Verify the four roles appear"],
                scope_paths=["mimir/subagents.py"],
                suggested_test_command='pytest -q tests/ -k "subagent"',
                depends_on=["Add review schemas"],
                risk="high",
            ),
        ],
    )

    payload = result.model_dump()
    assert payload["leaves"][0]["risk"] == "standard"
    assert payload["leaves"][0]["scope_paths"] == ["mimir/worklink/review.py"]
    assert payload["leaves"][0]["depends_on"] == []
    # Dependencies live on the leaf; ordering is expressed via depends_on (title).
    assert payload["leaves"][1]["depends_on"] == ["Add review schemas"]
    assert payload["leaves"][1]["risk"] == "high"
    # No separate edge-list or wave structures anymore.
    assert "blocked_by" not in payload
    assert "waves" not in payload


def test_work_decomposer_leaf_requires_template_acceptance_scope_and_tests() -> None:
    with pytest.raises(ValidationError):
        WorklinkLeafSpec(
            title="Too vague",
            acceptance_criteria=[],
            review_criteria=["review it"],
            scope_paths=["mimir/worklink/review.py"],
            suggested_test_command="pytest",
        )

    with pytest.raises(ValidationError):
        WorklinkLeafSpec(
            title="No scope",
            acceptance_criteria=["observable"],
            review_criteria=["review it"],
            scope_paths=[],
            suggested_test_command="pytest",
        )


def test_decompose_reviewer_schema_uses_approve_reject_flat_findings() -> None:
    result = DecomposeReview(
        verdict="REJECT",
        summary="One parallel file overlap remains.",
        findings=[
            "Two parallel leaves both include mimir/subagents.py; serialize one via depends_on.",
        ],
    )

    assert result.verdict == "REJECT"
    # findings are flat strings (no nested finding objects).
    assert result.findings and all(isinstance(f, str) for f in result.findings)


def test_per_slice_reviewer_schema_and_prompt_are_adversarial_observed_evidence_only() -> None:
    result = SliceReview(
        verdict="REJECT",
        summary="Observed tests did not run.",
        findings=["Controller-observed test result is missing for the classifier AC."],
        required_fixes=["Run the focused pytest command and attach the observed result."],
    )

    assert result.verdict == "REJECT"
    assert result.findings and isinstance(result.findings[0], str)
    assert result.required_fixes and isinstance(result.required_fixes[0], str)
    assert "adversarial" in PER_SLICE_REVIEWER_PROMPT.lower()
    assert "controller-observed" in PER_SLICE_REVIEWER_PROMPT.lower()
    assert "trust worker prose" in PER_SLICE_REVIEWER_PROMPT.lower()
    assert "approve only if" in PER_SLICE_REVIEWER_PROMPT.lower()


def test_integration_validator_schema_is_flat_verdict_summary_findings() -> None:
    result = IntegrationValidation(
        verdict="GO-WITH-NITS",
        summary="All ACs are covered with one naming nit.",
        findings=["AC 'register four roles' is covered; minor naming nit in review.py."],
    )

    assert result.verdict == "GO-WITH-NITS"
    assert result.findings and all(isinstance(f, str) for f in result.findings)


def test_worklink_review_subagent_specs_are_readonly_and_structured() -> None:
    specs = build_worklink_review_subagents()

    assert [spec["name"] for spec in specs] == [
        "work-decomposer",
        "decompose-reviewer",
        "per-slice-reviewer",
        "integration-validator",
    ]
    assert [spec["response_format"] for spec in specs] == [
        WorkDecomposition,
        DecomposeReview,
        SliceReview,
        IntegrationValidation,
    ]
    assert all(spec["tools"] == [] for spec in specs)
    assert all(spec["permissions"][0].operations == ["write"] for spec in specs)
    assert all(spec["permissions"][0].mode == "deny" for spec in specs)


def test_role_prompts_cover_required_review_contracts() -> None:
    decomposer = WORK_DECOMPOSER_PROMPT.lower()
    assert "scope_paths" in decomposer
    assert "depends_on" in decomposer
    assert "file-disjoint" in decomposer
    assert "hotspot" in decomposer
    assert "architecturally central" in decomposer
    assert "hard to reverse" in decomposer

    reviewer = DECOMPOSE_REVIEWER_PROMPT.lower()
    assert "acceptance criterion maps" in reviewer
    assert "file-disjoint" in reviewer
    assert "depends_on" in reviewer
    assert "dag" in reviewer

    assert "acceptance criterion" in INTEGRATION_VALIDATOR_PROMPT
    assert "GO-WITH-NITS" in INTEGRATION_VALIDATOR_PROMPT
    assert "plain-text" in INTEGRATION_VALIDATOR_PROMPT


def test_classifier_returns_single_for_low_risk_leaf() -> None:
    assert (
        classify_leaf_review_risk(
            scope_paths=["docs/internal/WORKLINK.md"],
            labels={"worklink:ready", "docs"},
            assigned_risk="standard",
        )
        == "single"
    )


def test_classifier_combines_decomposer_risk_with_scope_and_labels() -> None:
    assert (
        classify_leaf_review_risk(
            scope_paths=["docs/internal/WORKLINK.md"],
            labels={"worklink:ready"},
            assigned_risk="high",
        )
        == "multi"
    )
    assert (
        classify_leaf_review_risk(
            scope_paths=["services/billing/db/migrations/001_add_table.sql"],
            labels={"worklink:ready"},
            assigned_risk="standard",
        )
        == "multi"
    )
    assert (
        classify_leaf_review_risk(
            scope_paths=["docs/internal/WORKLINK.md"],
            labels={"worklink:ready"},
            assigned_risk="standard",
        )
        == "single"
    )


@pytest.mark.parametrize(
    ("scope_path", "label"),
    [
        ("services/billing/db/migrations/001_add_table.sql", "worklink:ready"),
        ("apps/web/src/oauth_callback.ts", "worklink:ready"),
        ("platform/config/secret_store.py", "worklink:ready"),
        ("infra/prod/terraform.lock", "worklink:ready"),
        ("packages/client/src/generated/api.ts", "worklink:ready"),
        ("docs/internal/WORKLINK.md", "risk:high"),
    ],
)
def test_classifier_returns_multi_for_default_tiered_review_config(
    scope_path: str, label: str
) -> None:
    assert classify_leaf_review_risk(scope_paths=[scope_path], labels={label}) == "multi"


def test_default_tiered_review_scope_patterns_are_not_mimir_specific() -> None:
    assert all("mimir/" not in pattern for pattern in TieredReviewConfig().high_risk_scope_patterns)


def test_classifier_uses_supplied_tiered_review_config_scope_patterns_and_labels() -> None:
    config = TieredReviewConfig(
        high_risk_scope_patterns=("**/custom/hotspot/**",),
        high_risk_labels=("review:multi",),
        multi_vote_reviewer_count=5,
    )

    assert (
        classify_leaf_review_risk(
            scope_paths=["src/custom/hotspot/file.py"],
            labels={"worklink:ready"},
            tiered_review=config,
        )
        == "multi"
    )
    assert (
        classify_leaf_review_risk(
            scope_paths=["services/billing/db/migrations/001_add_table.sql"],
            labels={"worklink:ready"},
            tiered_review=config,
        )
        == "single"
    )
    assert (
        classify_leaf_review_risk(
            scope_paths=["docs/notes.md"],
            labels={"review:multi"},
            tiered_review=config,
        )
        == "multi"
    )
