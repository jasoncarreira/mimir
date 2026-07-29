from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from mimir.repo_pr_parity import (
    AuditEvidence,
    EffectEvidence,
    LegacyObservation,
    ParityProbe,
    evaluate_typed_shadow,
    offline_canary_probes,
    run_parity,
    write_parity_report,
)


def _probe(**changes) -> ParityProbe:
    base = ParityProbe(
        scenario="test",
        operation="repo_status",
        workflow="remediation",
        scope_id="1" * 64,
        head_sha="a" * 40,
        legacy=LegacyObservation("allow", "ok"),
    )
    return replace(base, **changes)


def test_offline_canary_covers_all_scenarios_and_has_observed_primary_evidence() -> None:
    probes = offline_canary_probes()
    report = run_parity(probes)

    assert {probe.scenario for probe in probes} == {
        "ordinary_review", "own_pr_remediation", "stale_head_refusal",
        "conflict_resolution", "unsupported_operation_escalation",
        "fork_refusal", "mixed_input_denial", "coding_disabled",
    }
    assert report["cutover_blocked"] is False
    assert report["counts"] == {
        "operations_total": 12,
        "operations_matched": 12,
        "operations_mismatched": 0,
        "scenarios_total": 8,
        "scenarios_matched": 8,
        "scenarios_mismatched": 0,
        "primary_observed_effects": 7,
        "shadow_observed_effects": 0,
    }
    writes = [row for row in report["operations"] if row["legacy"]["effects"]]
    assert {row["operation"] for row in writes} >= {
        "pr_submit_review", "pr_comment", "pr_rerequest_review", "repo_push",
    }
    for row in writes:
        assert row["shadow_observed_effect_count"] == 0
        assert {item["scope_id"] for item in row["legacy"]["effects"]} == {row["scope_id"]}
        assert {item["scope_id"] for item in row["legacy"]["audits"]} == {row["scope_id"]}


@pytest.mark.parametrize(
    ("changes", "decision", "result"),
    [
        ({"coding_enabled": False}, "unavailable", "coding_disabled"),
        ({"isolated_input": False}, "refuse", "mixed_input"),
        ({"same_repository": False}, "refuse", "fork_refused"),
        ({"operation": "repo_push", "head_is_current": False}, "refuse", "stale_scope"),
        ({"operation": "pr_submit_review"}, "refuse", "scope_action_denied"),
        ({"operation": "pr_resolve_thread", "supported": False}, "escalate", "unsupported_operation"),
        ({"operation": "repo_stage", "conflict_paths": ("conflict.py",), "requested_paths": ("other.py",)}, "refuse", "unproven_conflict_path"),
    ],
)
def test_each_shadow_authority_and_refusal_check_is_pinned(changes, decision, result) -> None:
    projection = evaluate_typed_shadow(_probe(**changes))
    assert (projection.decision, projection.result, projection.planned_effects) == (
        decision,
        result,
        ("escalation",) if decision == "escalate" else (),
    )


def test_mismatches_are_counted_categorized_and_block_unless_accepted() -> None:
    probe = _probe(legacy=LegacyObservation("allow", "different"))

    blocked = run_parity((probe,))
    accepted = run_parity(
        (probe,),
        accepted_mismatch_categories=frozenset({"result"}),
        acceptance_operator="security-reviewer",
    )

    assert blocked["counts"]["operations_mismatched"] == 1
    assert blocked["mismatch_category_counts"] == {"result": 1}
    assert blocked["cutover_blocked"] is True
    assert accepted["cutover_blocked"] is False
    assert accepted["counts"]["operations_mismatched"] == 1


def test_effect_or_audit_not_bound_to_immutable_scope_blocks_cutover() -> None:
    effect = EffectEvidence("repo_push", "push:1", "2" * 64, "a" * 40)
    audit = AuditEvidence("repo_push", "audit:1", "1" * 64, "a" * 40)
    probe = _probe(
        operation="repo_push",
        legacy=LegacyObservation("allow", "pushed", (effect,), (audit,)),
    )

    report = run_parity((probe,))

    assert report["cutover_blocked"] is True
    assert report["mismatch_category_counts"] == {"scope_binding": 1}


@pytest.mark.parametrize(
    "probe,match",
    [
        (_probe(scenario=""), "must be named"),
        (_probe(workflow="other"), "workflow"),
        (_probe(scope_id="short"), "scope id"),
        (_probe(head_sha="short"), "head SHA"),
        (_probe(operation="repo_stage", requested_paths=["x"]), "tuple shape"),
        (_probe(operation="repo_stage", conflict_paths=["x"]), "tuple shape"),
        (_probe(legacy=LegacyObservation("refuse", "denied", (EffectEvidence("x", "x", "1" * 64, "a" * 40),))), "cannot claim"),
    ],
)
def test_parity_input_shape_refusals_are_pinned(probe, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        run_parity((probe,))


def test_duplicate_operations_and_unknown_acceptance_are_refused() -> None:
    probe = _probe()
    with pytest.raises(ValueError, match="duplicate"):
        run_parity((probe, probe))
    with pytest.raises(ValueError, match="unknown parity"):
        run_parity((probe,), accepted_mismatch_categories=frozenset({"anything"}))
    with pytest.raises(ValueError, match="identified operator"):
        run_parity((probe,), accepted_mismatch_categories=frozenset({"result"}))


def test_report_is_written_as_durable_local_artifact(tmp_path: Path) -> None:
    path = tmp_path / "evidence" / "parity.json"
    report = run_parity(offline_canary_probes())

    write_parity_report(path, report)

    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert not path.with_suffix(".json.tmp").exists()


def test_checked_in_evidence_matches_offline_canary() -> None:
    expected = run_parity(offline_canary_probes())
    path = Path(__file__).parents[1] / "evidence" / "repo-pr-parity.json"
    assert json.loads(path.read_text(encoding="utf-8")) == expected
