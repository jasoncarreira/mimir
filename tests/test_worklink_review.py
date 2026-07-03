from __future__ import annotations

import pytest
from pydantic import ValidationError

from mimir.worklink.backends.registry import TieredReviewConfig
from mimir.worklink.review import (
    INTEGRATION_VALIDATOR_PROMPT,
    LEAD_SLICE_REVIEWER_PROMPT,
    SUB_SLICE_REVIEWER_PROMPT,
    WORK_DECOMPOSER_PROMPT,
    IntegrationDecision,
    SliceDecision,
    WorklinkLeafSpec,
    classify_leaf_review_risk,
)


def test_leaf_spec_carries_strict_fields_and_dependencies() -> None:
    leaf = WorklinkLeafSpec(
        title="Wire registration",
        acceptance_criteria=["Roles registered"],
        review_criteria=["Verify the roles appear"],
        scope_paths=["mimir/subagents.py"],
        suggested_test_command='pytest -q tests/ -k "subagent"',
        depends_on=["Add review schemas"],
        risk="high",
    )

    payload = leaf.model_dump()
    assert payload["depends_on"] == ["Add review schemas"]
    assert payload["risk"] == "high"
    assert payload["labels"] == ["worklink:ready"]


def test_leaf_spec_requires_acceptance_scope_and_tests() -> None:
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


def test_decisions_are_plain_dataclasses_defaulting_safe() -> None:
    rejected = SliceDecision(approved=False, fixes=("run tests",))
    assert rejected.fixes == ("run tests",) and rejected.summary == ""
    blocked = IntegrationDecision(approved=False, reasons=("AC 2 uncovered",))
    assert blocked.reasons == ("AC 2 uncovered",)


def test_role_prompts_cover_required_contracts() -> None:
    decomposer = WORK_DECOMPOSER_PROMPT.lower()
    assert "file_leaf" in decomposer
    assert "comment_on_epic" in decomposer
    assert "depends_on" in decomposer
    assert "file-disjoint" in decomposer
    assert "hotspot" in decomposer
    assert "architecturally central" in decomposer
    assert "hard to reverse" in decomposer
    assert "final text reply is ignored" in decomposer

    lead = LEAD_SLICE_REVIEWER_PROMPT.lower()
    assert "adversarial" in lead
    assert "controller-observed" in lead
    assert "trust worker prose" in lead
    assert "approve_slice" in lead
    assert "request_fixes" in lead
    assert "spawn_reviewer" in lead
    assert "never decide by counting" in lead
    assert "verify that problem yourself" in lead

    sub = SUB_SLICE_REVIEWER_PROMPT.lower()
    assert "lens" in sub
    assert "plain" in sub and "text" in sub

    validator = INTEGRATION_VALIDATOR_PROMPT.lower()
    assert "acceptance criterion" in validator
    assert "approve_integration" in validator
    assert "block_integration" in validator


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
