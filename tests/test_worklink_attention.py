from __future__ import annotations

import pytest

from mimir.worklink.attention import (
    AccountingBasis,
    AttentionFacts,
    AttentionKind,
    AttentionOutcome,
    ClaimRelation,
    Settlement,
    classify_attention,
)


@pytest.mark.parametrize(
    ("facts", "outcome", "basis", "consumed", "settlement"),
    [
        (AttentionFacts(kind=AttentionKind.FACTORY_STARTED), AttentionOutcome.STARTED, AccountingBasis.PRECLAIM, None, Settlement.NOT_NEEDED),
        (AttentionFacts(exhaustion=True), AttentionOutcome.ATTEMPTS_EXHAUSTED, AccountingBasis.EXHAUSTION, False, Settlement.NOT_NEEDED),
        (AttentionFacts(preclaim=True), AttentionOutcome.INFRASTRUCTURE_FAILURE, AccountingBasis.PRECLAIM, False, Settlement.NOT_NEEDED),
        (AttentionFacts(primary_outcome=AttentionOutcome.PARTIAL), AttentionOutcome.PARTIAL, AccountingBasis.FACTORY_PARTIAL, True, Settlement.NOT_NEEDED),
        (AttentionFacts(verified_completion=True), AttentionOutcome.SUCCEEDED, AccountingBasis.VERIFIED_COMPLETION, True, Settlement.NOT_NEEDED),
        (AttentionFacts(factory_status={"status": "blocked", "pr_url": "https://example/pr/1", "steps": None, "slices": None}), AttentionOutcome.BLOCKED, AccountingBasis.FACTORY_PR, True, Settlement.NOT_NEEDED),
        (AttentionFacts(factory_status={"status": "blocked", "pr_url": None, "steps": [{"agent": "spec"}], "slices": None}), AttentionOutcome.BLOCKED, AccountingBasis.FACTORY_STEPS, True, Settlement.NOT_NEEDED),
        (AttentionFacts(factory_status={"status": "blocked", "pr_url": None, "steps": [], "slices": [{"id": "one"}]}), AttentionOutcome.BLOCKED, AccountingBasis.FACTORY_SLICES, True, Settlement.NOT_NEEDED),
        (AttentionFacts(normalized_leaf_result=True), AttentionOutcome.GENUINE_FAILURE, AccountingBasis.LEAF_EXECUTION, True, Settlement.NOT_NEEDED),
        (AttentionFacts(unpublished_commits=True), AttentionOutcome.GENUINE_FAILURE, AccountingBasis.UNPUBLISHED_COMMITS, True, Settlement.NOT_NEEDED),
        (AttentionFacts(factory_status={"status": "blocked", "pr_url": None, "steps": None, "slices": None}, claim_relation=ClaimRelation.CURRENT_CLAIM), AttentionOutcome.INFRASTRUCTURE_FAILURE, AccountingBasis.INFRASTRUCTURE, False, Settlement.PENDING),
    ],
)
def test_accounting_truth_table(facts, outcome, basis, consumed, settlement):
    decision = classify_attention(facts)
    assert decision.outcome is outcome
    assert decision.basis is basis
    assert decision.attempt_consumed is consumed
    assert decision.settlement is settlement
