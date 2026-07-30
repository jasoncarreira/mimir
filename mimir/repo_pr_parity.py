"""Offline shadow parity for the legacy and typed repository/PR paths.

The primary (legacy) observation is supplied as data after it has run.  The
typed path is evaluated as a pure projection: this module has no Forge client,
Git runner, or callback through which it could repeat a write.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Literal

from .models import RepoPRAction


ParityDecision = Literal["allow", "refuse", "escalate", "unavailable"]
MismatchCategory = Literal[
    "decision", "result", "effect_evidence", "scope_binding",
]

_SCOPE_ID = re.compile(r"[0-9a-f]{64}")
_HEAD_SHA = re.compile(r"[0-9a-f]{40}")
_WRITE_OPERATIONS = frozenset({
    "pr_submit_review", "pr_comment", "pr_rerequest_review",
    "repo_stage", "repo_commit", "repo_push", "unsupported_operation",
})
_OPERATION_ACTION = {
    "pr_metadata": RepoPRAction.INSPECT.value,
    "repo_status": RepoPRAction.INSPECT.value,
    "repo_test": RepoPRAction.TEST.value,
    "pr_submit_review": RepoPRAction.PR_REVIEW.value,
    "pr_comment": RepoPRAction.PR_COMMENT.value,
    "pr_rerequest_review": RepoPRAction.PR_REREQUEST.value,
    "repo_stage": RepoPRAction.WRITE.value,
    "repo_commit": RepoPRAction.COMMIT.value,
    "repo_push": RepoPRAction.PUSH.value,
}
_REVIEW_ACTIONS = frozenset({
    RepoPRAction.INSPECT.value,
    RepoPRAction.CHECKOUT.value,
    RepoPRAction.TEST.value,
    RepoPRAction.PR_REVIEW.value,
    RepoPRAction.PR_COMMENT.value,
})
_REMEDIATION_ACTIONS = frozenset({
    RepoPRAction.INSPECT.value,
    RepoPRAction.CHECKOUT.value,
    RepoPRAction.TEST.value,
    RepoPRAction.WRITE.value,
    RepoPRAction.COMMIT.value,
    RepoPRAction.PUSH.value,
    RepoPRAction.PR_COMMENT.value,
    RepoPRAction.PR_EDIT.value,
    RepoPRAction.PR_REREQUEST.value,
})


@dataclass(frozen=True)
class EffectEvidence:
    """Observed primary-path effect receipt bound to immutable authority."""

    kind: str
    receipt: str
    scope_id: str
    head_sha: str


@dataclass(frozen=True)
class AuditEvidence:
    """Durable primary audit record corresponding to an observed effect."""

    kind: str
    record_id: str
    scope_id: str
    head_sha: str


@dataclass(frozen=True)
class LegacyObservation:
    decision: ParityDecision
    result: str
    effects: tuple[EffectEvidence, ...] = ()
    audits: tuple[AuditEvidence, ...] = ()


@dataclass(frozen=True)
class ParityProbe:
    """One recorded/synthesized operation presented to the typed shadow."""

    scenario: str
    operation: str
    workflow: Literal["review", "remediation"]
    scope_id: str
    head_sha: str
    legacy: LegacyObservation
    coding_enabled: bool = True
    isolated_input: bool = True
    same_repository: bool = True
    head_is_current: bool = True
    supported: bool = True
    conflict_paths: tuple[str, ...] = ()
    requested_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class TypedProjection:
    decision: ParityDecision
    result: str
    planned_effects: tuple[str, ...] = ()


def _effect_result(operation: str) -> str:
    return {
        "pr_submit_review": "review_submitted",
        "pr_comment": "comment_created",
        "pr_rerequest_review": "review_rerequested",
        "repo_stage": "staged",
        "repo_commit": "committed",
        "repo_push": "pushed",
    }.get(operation, "ok")


def evaluate_typed_shadow(probe: ParityProbe) -> TypedProjection:
    """Evaluate typed policy/result semantics without executing an operation."""
    if not probe.coding_enabled:
        return TypedProjection("unavailable", "coding_disabled")
    if not probe.isolated_input:
        return TypedProjection("refuse", "mixed_input")
    if probe.workflow == "remediation" and not probe.same_repository:
        return TypedProjection("refuse", "fork_refused")
    if not probe.supported or probe.operation not in _OPERATION_ACTION:
        return TypedProjection("escalate", "unsupported_operation", ("escalation",))

    actions = _REVIEW_ACTIONS if probe.workflow == "review" else _REMEDIATION_ACTIONS
    if (
        _OPERATION_ACTION[probe.operation] not in actions
        and not (
            probe.operation == "pr_rerequest_review"
            and RepoPRAction.PR_REVIEW.value in actions
        )
    ):
        return TypedProjection("refuse", "scope_action_denied")
    if probe.operation == "repo_push" and not probe.head_is_current:
        return TypedProjection("refuse", "stale_scope")
    if (
        probe.operation == "repo_stage"
        and probe.conflict_paths
        and not set(probe.requested_paths).issubset(probe.conflict_paths)
    ):
        return TypedProjection("refuse", "unproven_conflict_path")

    planned = (probe.operation,) if probe.operation in _WRITE_OPERATIONS else ()
    return TypedProjection("allow", _effect_result(probe.operation), planned)


def _validate_probe(probe: ParityProbe) -> None:
    if not probe.scenario or not probe.operation:
        raise ValueError("parity scenario and operation must be named")
    if probe.workflow not in {"review", "remediation"}:
        raise ValueError("parity workflow must be review or remediation")
    if _SCOPE_ID.fullmatch(probe.scope_id) is None:
        raise ValueError("parity probe requires a full immutable scope id")
    if _HEAD_SHA.fullmatch(probe.head_sha) is None:
        raise ValueError("parity probe requires a full immutable head SHA")
    if probe.operation == "repo_stage" and not all(
        isinstance(paths, tuple) for paths in (probe.conflict_paths, probe.requested_paths)
    ):
        raise ValueError("parity conflict paths must use the typed tuple shape")
    if probe.legacy.decision in {"refuse", "unavailable"} and probe.legacy.effects:
        raise ValueError("a refused primary operation cannot claim observed effects")


def run_parity(
    probes: tuple[ParityProbe, ...],
    *,
    accepted_mismatch_categories: frozenset[str] = frozenset(),
    acceptance_operator: str | None = None,
) -> dict[str, object]:
    """Compare primary observations with typed projections and count partial success."""
    unknown_acceptances = accepted_mismatch_categories - {
        "decision", "result", "effect_evidence", "scope_binding",
    }
    if unknown_acceptances:
        raise ValueError("unknown parity mismatch acceptance category")
    if accepted_mismatch_categories and not (acceptance_operator or "").strip():
        raise ValueError("parity mismatch acceptance requires an identified operator")
    keys: set[tuple[str, str]] = set()
    rows: list[dict[str, object]] = []
    scenario_matches: dict[str, bool] = {}
    category_counts: dict[str, int] = {}

    for probe in probes:
        _validate_probe(probe)
        key = (probe.scenario, probe.operation)
        if key in keys:
            raise ValueError("duplicate parity scenario operation")
        keys.add(key)
        typed = evaluate_typed_shadow(probe)
        mismatches: list[str] = []
        if typed.decision != probe.legacy.decision:
            mismatches.append("decision")
        if typed.result != probe.legacy.result:
            mismatches.append("result")

        observed_kinds = tuple(effect.kind for effect in probe.legacy.effects)
        if typed.planned_effects != observed_kinds:
            mismatches.append("effect_evidence")
        evidence = (*probe.legacy.effects, *probe.legacy.audits)
        if any(
            item.scope_id != probe.scope_id or item.head_sha != probe.head_sha
            for item in evidence
        ) or (
            probe.legacy.effects
            and {effect.kind for effect in probe.legacy.effects}
            != {audit.kind for audit in probe.legacy.audits}
        ):
            mismatches.append("scope_binding")

        for category in set(mismatches):
            category_counts[category] = category_counts.get(category, 0) + 1
        matched = not mismatches
        scenario_matches[probe.scenario] = scenario_matches.get(probe.scenario, True) and matched
        rows.append({
            "scenario": probe.scenario,
            "operation": probe.operation,
            "scope_id": probe.scope_id,
            "head_sha": probe.head_sha,
            "legacy": json.loads(json.dumps(asdict(probe.legacy))),
            "typed_shadow": json.loads(json.dumps(asdict(typed))),
            "shadow_observed_effect_count": 0,
            "matched": matched,
            "mismatch_categories": sorted(set(mismatches)),
        })

    mismatches = sum(1 for row in rows if not row["matched"])
    blocked_categories = set(category_counts) - accepted_mismatch_categories
    return {
        "schema_version": 1,
        "mode": "offline_recorded_or_synthesized",
        "cutover_blocked": bool(blocked_categories),
        "accepted_mismatch_categories": sorted(accepted_mismatch_categories),
        "acceptance_operator": acceptance_operator,
        "counts": {
            "operations_total": len(rows),
            "operations_matched": len(rows) - mismatches,
            "operations_mismatched": mismatches,
            "scenarios_total": len(scenario_matches),
            "scenarios_matched": sum(scenario_matches.values()),
            "scenarios_mismatched": sum(not value for value in scenario_matches.values()),
            "primary_observed_effects": sum(len(probe.legacy.effects) for probe in probes),
            "shadow_observed_effects": 0,
        },
        "mismatch_category_counts": dict(sorted(category_counts.items())),
        "operations": rows,
        "remaining_gaps": [
            "Live review/remediation validation is the reviewer-executed gate in #1050.",
            "This offline leaf does not invoke gh, chainlink, GitHub, or a live pull request.",
        ],
    }


def write_parity_report(path: Path, report: dict[str, object]) -> None:
    """Atomically persist the local parity artifact without an external sink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    temporary.replace(path)


def _scope(name: str) -> str:
    return hashlib.sha256(f"offline-parity:{name}".encode()).hexdigest()


def offline_canary_probes() -> tuple[ParityProbe, ...]:
    """Recorded/synthesized coverage set for the #1049 offline canary."""
    head = "a" * 40

    def effect(scenario: str, kind: str) -> tuple[EffectEvidence, ...]:
        return (EffectEvidence(kind, f"recorded:{scenario}:{kind}", _scope(scenario), head),)

    def audit(scenario: str, kind: str) -> tuple[AuditEvidence, ...]:
        return (AuditEvidence(kind, f"audit:{scenario}:{kind}", _scope(scenario), head),)

    return (
        ParityProbe("ordinary_review", "pr_metadata", "review", _scope("ordinary_review"), head,
                    LegacyObservation("allow", "ok")),
        ParityProbe("ordinary_review", "pr_submit_review", "review", _scope("ordinary_review"), head,
                    LegacyObservation("allow", "review_submitted", effect("ordinary_review", "pr_submit_review"), audit("ordinary_review", "pr_submit_review"))),
        ParityProbe("own_pr_remediation", "pr_comment", "remediation", _scope("own_pr_remediation"), head,
                    LegacyObservation("allow", "comment_created", effect("own_pr_remediation", "pr_comment"), audit("own_pr_remediation", "pr_comment"))),
        ParityProbe("own_pr_remediation", "pr_rerequest_review", "remediation", _scope("own_pr_remediation"), head,
                    LegacyObservation("allow", "review_rerequested", effect("own_pr_remediation", "pr_rerequest_review"), audit("own_pr_remediation", "pr_rerequest_review"))),
        ParityProbe("stale_head_refusal", "repo_push", "remediation", _scope("stale_head_refusal"), head,
                    LegacyObservation("refuse", "stale_scope"), head_is_current=False),
        ParityProbe("conflict_resolution", "repo_stage", "remediation", _scope("conflict_resolution"), head,
                    LegacyObservation("allow", "staged", effect("conflict_resolution", "repo_stage"), audit("conflict_resolution", "repo_stage")),
                    conflict_paths=("src/conflict.py",), requested_paths=("src/conflict.py",)),
        ParityProbe("conflict_resolution", "repo_commit", "remediation", _scope("conflict_resolution"), head,
                    LegacyObservation("allow", "committed", effect("conflict_resolution", "repo_commit"), audit("conflict_resolution", "repo_commit"))),
        ParityProbe("unsupported_operation_escalation", "pr_resolve_thread", "review", _scope("unsupported_operation_escalation"), head,
                    LegacyObservation("escalate", "unsupported_operation", effect("unsupported_operation_escalation", "escalation"), audit("unsupported_operation_escalation", "escalation")), supported=False),
        ParityProbe("fork_refusal", "repo_status", "remediation", _scope("fork_refusal"), head,
                    LegacyObservation("refuse", "fork_refused"), same_repository=False),
        ParityProbe("mixed_input_denial", "pr_comment", "remediation", _scope("mixed_input_denial"), head,
                    LegacyObservation("refuse", "mixed_input"), isolated_input=False),
        ParityProbe("coding_disabled", "pr_metadata", "review", _scope("coding_disabled"), head,
                    LegacyObservation("unavailable", "coding_disabled"), coding_enabled=False),
        ParityProbe("own_pr_remediation", "repo_push", "remediation", _scope("own_pr_remediation"), head,
                    LegacyObservation("allow", "pushed", effect("own_pr_remediation", "repo_push"), audit("own_pr_remediation", "repo_push"))),
    )


__all__ = [
    "AuditEvidence", "EffectEvidence", "LegacyObservation", "ParityProbe",
    "TypedProjection", "evaluate_typed_shadow", "offline_canary_probes",
    "run_parity", "write_parity_report",
]
