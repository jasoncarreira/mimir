from __future__ import annotations

import ast
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from mimir.commands.worklink import _record_interruption
from mimir.worklink.attention import (
    AttentionSource,
    ClaimIdentity,
    ClearancePolicy,
    SOURCE_RULES,
)
from mimir.worklink.claims import ClaimRecord
from mimir.worklink.dispatch_failures import (
    bind_claim,
    confirm_claim_and_start,
    dispatch_failure_state_dir,
    load_outcome_state,
    reserve_dispatch,
)
from mimir.worklink.orchestrator import (
    WorklinkRunResult,
    _record_run_success,
)


PRODUCTION_SOURCES = frozenset({
    "queue_leaf_spawn", "queue_factory_spawn", "startup_leaf_spawn",
    "startup_factory_spawn", "leaf_cli_repository", "factory_cli_repository",
    "leaf_issue_read", "leaf_target_branch", "leaf_template", "leaf_config",
    "leaf_inventory", "leaf_repository", "leaf_backend", "leaf_compute",
    "leaf_claim_preparation", "factory_interlock", "factory_issue_read",
    "factory_issue_kind", "factory_target_branch", "factory_config",
    "factory_backend", "factory_repository", "factory_compute", "factory_launcher",
    "factory_inventory", "factory_base_lookup", "factory_work_item",
    "factory_retained_binding", "claim_command", "claim_guard", "claim_steal",
    "claim_budget", "claim_capacity_read", "claim_unready", "claim_inprogress",
    "claim_comment", "claim_contention", "leaf_checkout_create",
    "leaf_publication_capture", "leaf_checkout_isolation", "leaf_dirty_snapshot",
    "leaf_prompt", "leaf_work_spec", "leaf_report_setup", "leaf_compute_launch",
    "leaf_handle_save", "leaf_compute_wait", "leaf_interpret", "leaf_test_report",
    "leaf_pr_body", "leaf_gate", "leaf_regate", "leaf_evidence_write", "leaf_commit",
    "leaf_publication_fence", "leaf_push", "leaf_pr_open", "leaf_evidence_comment",
    "leaf_backend_outcome", "leaf_terminal_labels", "leaf_terminal_release",
    "leaf_interrupt", "reattach_state", "reattach_shim", "reattach_pr",
    "reattach_backend", "reattach_compute", "reattach_issue", "reattach_checkout",
    "reattach_prompt", "reattach_spec", "reattach_wait", "leaf_startup_reconcile",
    "factory_checkout", "factory_git_identity", "factory_publishing_identity",
    "factory_credential", "factory_identity_verify", "factory_spec",
    "factory_launch_binding", "factory_sandbox", "factory_permissions",
    "factory_launch", "factory_handle_save", "factory_recovery_binding",
    "factory_recovery_status", "factory_recovery_lock", "factory_resume",
    "factory_recovery_repository", "factory_recovery_spec", "factory_recovery_launch",
    "factory_recovery_save", "factory_wait_start", "factory_status_read",
    "factory_startup_deadline", "factory_run_deadline", "factory_status_binding",
    "factory_owner", "factory_observation_save", "factory_heartbeat",
    "factory_wait_drain", "factory_driver_exit", "factory_transcript",
    "factory_cleanup", "factory_parked", "factory_blocked", "factory_partial",
    "factory_completion_verify", "factory_terminal_labels", "factory_terminal_release",
    "factory_interrupt", "factory_startup_reconcile", "leaf_start", "factory_start",
    "leaf_success", "factory_success", "legacy_v1",
})

APPROVED_EXCLUSION_TESTS = {
    "manual": ("test_worklink_orchestrator.py", "test_manual_success_clears_autonomous_failure_ledger"),
    "dry_run": ("test_worklink_orchestrator.py", "test_dry_run_prints_rendered_work_order_without_mutations"),
    "review_lifecycle": ("test_worklink_claims.py", "test_claim_issue_refuses_worklink_review_label"),
    "publication_fence": ("test_worklink_claims.py", "test_retained_publication_refuses_without_mutation_and_allows_retry_after_clear"),
    "completed_evidence": ("test_worklink_claims.py", "test_claim_issue_refuses_when_review_ready_evidence_exists"),
    "fresh_duplicate": ("test_worklink_claims.py", "test_duplicate_vs_live_final_attempt_never_labels_blocked"),
    "capacity": ("test_worklink_claims.py", "test_claim_issue_enforces_max_active_locks_after_reservation"),
    "registry_failure": ("test_worklink_autonomy.py", "test_poller_degrades_before_dispatch_for_invalid_backend_reference"),
    "poller_nonactionable": ("test_worklink_autonomy.py", "test_poller_filters_worklink_ready_through_chainlink_actionable_set"),
    "poller_blocked": ("test_worklink_autonomy.py", "test_poller_leaves_blocked_worklink_ready_issues_untouched"),
    "poller_active": ("test_worklink_autonomy.py", "test_poller_excludes_actively_locked_issue_from_candidates"),
    "poller_backoff": ("test_worklink_autonomy.py", "test_epic_dispatch_backoff_prevents_attempt_each_poll_cycle"),
    "poller_capacity": ("test_worklink_autonomy.py", "test_poller_no_dispatch_when_cap_reached"),
    "reattach_inactive": ("test_worklink_reattach.py", "test_reattach_skips_when_leaf_no_longer_in_progress"),
}


def _claim(issue_id: int = 42) -> tuple[ClaimIdentity, ClaimRecord]:
    claimed_at = datetime(2026, 9, 18, tzinfo=UTC)
    return (
        ClaimIdentity(issue_id, 1, "boundary-agent", claimed_at.isoformat()),
        ClaimRecord(issue_id, 1, "boundary-agent", claimed_at),
    )


def _prepare_claimed_reservation(
    home: Path, source: AttentionSource
) -> tuple[Path, str, ClaimIdentity, ClaimRecord]:
    state_dir = dispatch_failure_state_dir(home)
    target = "factory" if source.value.startswith("factory_") else "leaf"
    reservation = reserve_dispatch(
        state_dir, issue_id=42, target=target, autonomous=True
    )
    identity, record = _claim()
    bind_claim(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        claim=identity,
        confirmed=False,
    )
    confirm_claim_and_start(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        claim=identity,
        run_id="chainlink-42" if target == "factory" else None,
        sandbox=str(home / "sandbox") if target == "factory" else None,
    )
    return state_dir, reservation, identity, record


def _produce_lifecycle(home: Path, source: AttentionSource) -> None:
    state_dir = dispatch_failure_state_dir(home)
    target = "factory" if source.value.startswith("factory_") else "leaf"
    reservation = reserve_dispatch(state_dir, issue_id=42, target=target, autonomous=True)
    identity, _record = _claim()
    bind_claim(
        state_dir, issue_id=42, reservation_id=reservation, claim=identity, confirmed=False
    )
    confirm_claim_and_start(
        state_dir,
        issue_id=42,
        reservation_id=reservation,
        claim=identity,
        run_id="chainlink-42" if target == "factory" else None,
        sandbox=str(home / "sandbox") if target == "factory" else None,
    )
    if source in {AttentionSource.LEAF_START, AttentionSource.FACTORY_START}:
        return
    evidence = home / f"{target}-success.json"
    payload = {
        "issue": 42,
        "status": "completed",
        "branch": "feature/42",
        "head_sha": "a" * 40,
        "pr_url": None,
    }
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    result = WorklinkRunResult(
        42,
        1,
        "completed",
        evidence_path=evidence,
        checkout=home / "sandbox",
        branch="feature/42",
    )
    _record_run_success(
        home,
        42,
        reservation_id=reservation,
        result=result,
        target=target,
    )


def test_closed_sources_are_bound_at_named_production_sites() -> None:
    root = Path(__file__).resolve().parents[1]
    names = {source.name: source.value for source in AttentionSource}
    sites: dict[str, set[tuple[str, str]]] = {source.value: set() for source in AttentionSource}
    for path in (root / "mimir").rglob("*.py"):
        if path.name == "attention.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack: list[str] = []

        class SourceVisitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "AttentionSource"
                    and node.attr in names
                ):
                    sites[names[node.attr]].add(
                        (path.relative_to(root).as_posix(), stack[-1] if stack else "<module>")
                    )
                self.generic_visit(node)

            def visit_Constant(self, node: ast.Constant) -> None:
                if isinstance(node.value, str) and node.value in sites:
                    sites[node.value].add(
                        (path.relative_to(root).as_posix(), stack[-1] if stack else "<module>")
                    )

        SourceVisitor().visit(tree)

    assert set(sites) == PRODUCTION_SOURCES
    assert all(boundaries for boundaries in sites.values())
    generic_recorders = {
        "_typed_outcome_boundary",
        "_record_factory_stage_failure",
        "_record_preclaim_claim",
        "record_attention",
    }
    assert all(
        any(function not in generic_recorders for _path, function in boundaries)
        for boundaries in sites.values()
    )


@pytest.mark.parametrize(
    "source",
    [
        AttentionSource.LEAF_START,
        AttentionSource.FACTORY_START,
        AttentionSource.LEAF_SUCCESS,
        AttentionSource.FACTORY_SUCCESS,
    ],
    ids=lambda source: source.value,
)
def test_lifecycle_sources_use_the_real_admission_and_success_writers(
    tmp_path: Path, source: AttentionSource
) -> None:
    _produce_lifecycle(tmp_path, source)
    occurrences = load_outcome_state(
        dispatch_failure_state_dir(tmp_path)
    )["issues"]["42"]["occurrences"].values()
    assert source.value in {occurrence["source"] for occurrence in occurrences}


@pytest.mark.parametrize(
    "source", [AttentionSource.LEAF_INTERRUPT, AttentionSource.FACTORY_INTERRUPT]
)
def test_interrupt_sources_use_the_command_boundary(
    tmp_path: Path, source: AttentionSource
) -> None:
    state_dir, reservation, _identity, _record = _prepare_claimed_reservation(
        tmp_path, source
    )
    _record_interruption(
        tmp_path,
        42,
        reservation,
        target="factory" if source == AttentionSource.FACTORY_INTERRUPT else "leaf",
        source=source.value,
        retained=True,
    )
    occurrences = load_outcome_state(state_dir)["issues"]["42"]["occurrences"].values()
    assert source.value in {occurrence["source"] for occurrence in occurrences}


def test_legacy_source_uses_the_real_v1_migration_boundary(tmp_path: Path) -> None:
    state_dir = dispatch_failure_state_dir(tmp_path)
    state_dir.mkdir(parents=True)
    row = {
        "active": True,
        "issue_id": 42,
        "failed_at": "2026-09-18T00:00:00+00:00",
        "signature": "legacy",
        "occurrence_id": "legacy-occurrence",
        "retry_after": None,
    }
    (state_dir / "dispatch_failures.json").write_text(
        json.dumps({"version": 1, "issues": {"42": row}}), encoding="utf-8"
    )
    occurrences = load_outcome_state(state_dir)["issues"]["42"]["occurrences"].values()
    assert "legacy_v1" in {occurrence["source"] for occurrence in occurrences}


def test_approved_exclusions_have_real_boundary_tests_in_the_ratified_suite() -> None:
    tests = Path(__file__).resolve().parent
    observed: set[tuple[str, str]] = set()
    for filename, _name in APPROVED_EXCLUSION_TESTS.values():
        tree = ast.parse((tests / filename).read_text(encoding="utf-8"))
        observed.update(
            (filename, node.name)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
    assert set(APPROVED_EXCLUSION_TESTS.values()).issubset(observed)


def test_production_inventory_equals_closed_source_and_clearance_inventory() -> None:
    assert PRODUCTION_SOURCES == {source.value for source in AttentionSource}
    assert set(SOURCE_RULES) == set(AttentionSource)
    assert all(
        rule.clearance in set(ClearancePolicy)
        for rule in SOURCE_RULES.values()
    )
