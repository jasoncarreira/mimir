"""Cultivate repository policy, checkout leases, typed Git and contained tests."""

from __future__ import annotations


# Repository inventory
import re
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from mimir._paths import live_loader_path_allowed


@dataclass(frozen=True)
class RepositoryTestSuite:
    name: str
    command: str
    default: bool = False
    selector_prefixes: tuple[str, ...] = ()
    selector_suffixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryConfig:
    slug: str
    root: Path
    mode: str
    origin: str
    base_branch: str
    test_command: str | None = None
    test_suites: tuple[RepositoryTestSuite, ...] = ()


@dataclass(frozen=True)
class AllowedRootConfig:
    root: Path
    mode: str


@dataclass(frozen=True)
class RepositoryInventory:
    repositories: tuple[RepositoryConfig, ...] = ()
    allowed_roots: tuple[AllowedRootConfig, ...] = ()
    declared: bool = False

    @classmethod
    def load(cls, path: Path) -> "RepositoryInventory":
        # Check before the missing-file fallback: a dangling scratch alias
        # must not turn a declared policy into permissive legacy defaults.
        if not live_loader_path_allowed(path):
            raise ValueError(f"repository inventory cannot be loaded from scratch: {path}")
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("repository inventory root must be a mapping")
        repositories = _parse_repositories(data.get("repositories") or ())
        allowed_roots = _parse_allowed_roots(data.get("allowed_roots") or ())
        overlap = {repo.root for repo in repositories}.intersection(
            root.root for root in allowed_roots
        )
        if overlap:
            paths = ", ".join(str(path) for path in sorted(overlap))
            raise ValueError(f"repository roots cannot also be allowed_roots: {paths}")
        return cls(
            repositories=repositories,
            allowed_roots=allowed_roots,
            declared=("repositories" in data or "allowed_roots" in data),
        )

    def repository(self, slug: str) -> RepositoryConfig | None:
        normalized = slug.strip().lower()
        return next((repo for repo in self.repositories if repo.slug == normalized), None)

    def repository_for_root(self, root: Path) -> RepositoryConfig | None:
        resolved = root.resolve()
        return next((repo for repo in self.repositories if repo.root == resolved), None)


_REPOSITORY_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_TEST_SUITE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_GITHUB_ORIGIN = re.compile(
    r"(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?"
)


def repo_binding_startup_alerts() -> tuple[dict[str, object], ...]:
    """Report configured GitHub repositories without a unique writable checkout."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    inventory = RepositoryInventory.load(Path(home) / "repositories.yaml") if home else RepositoryInventory()
    if inventory.declared:
        repos = {repo.slug for repo in inventory.repositories}
        configured_roots = tuple(str(repo.root) for repo in inventory.repositories)
    else:
        repos = {
            item.lower() for part in os.environ.get("GITHUB_REPOS", "").split(",")
            if (item := part.strip()) and valid_repository_slug(item)
            and all(segment not in {".", ".."} for segment in item.split("/"))
        }
        from mimir.config import _parse_file_tool_roots

        configured_roots = tuple(
            path for path, mode in _parse_file_tool_roots(
                os.environ.get("MIMIR_FILE_TOOL_ROOTS", ""), Path(home), always_rw=(),
            ) if mode == "rw"
        ) if home else ()
    alerts: list[dict[str, object]] = []
    for repo in sorted(repos):
        matches = 0
        if inventory.declared:
            matches = int(inventory.repository(repo) is not None)
        else:
            from mimir.access_control import (
                _MAINTENANCE_GIT_BASE_OVERRIDES, _maintenance_git_probe_env,
                _maintenance_resolved_pin,
            )

            git = _maintenance_resolved_pin("git")
            for configured in map(Path, configured_roots):
                if git is None:
                    break
                try:
                    root = configured.resolve(strict=True)
                    result = subprocess.run(
                        [str(git), "-C", str(root), *_MAINTENANCE_GIT_BASE_OVERRIDES,
                         "config", "--local", "--get", "remote.origin.url"],
                        stdin=subprocess.DEVNULL, capture_output=True, text=True,
                        timeout=5, check=False, env=_maintenance_git_probe_env(),
                    )
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    continue
                remote = _GITHUB_ORIGIN.fullmatch(result.stdout.strip())
                if result.returncode == 0 and remote is not None and (
                    f"{remote.group('owner')}/{remote.group('repo')}".lower() == repo
                ):
                    matches += 1
        if matches == 1:
            continue
        mode = "zero roots matched" if matches == 0 else f"ambiguous: {matches} roots matched"
        alerts.append({
            "repository": repo, "probed_roots": list(configured_roots),
            "match_count": matches, "operator_visible": True,
            "error": (
                f"pull-request operation rejected: no unique writable root matched repository "
                f"'{repo}' in MIMIR_FILE_TOOL_ROOTS ({mode}); configure exactly one :rw "
                "entry for the checkout directory itself, not its parent"
            ),
        })
    return tuple(alerts)


def valid_repository_slug(value: object) -> bool:
    return isinstance(value, str) and _REPOSITORY_SLUG.fullmatch(value) is not None


def _parse_mode(value: Any, label: str) -> str:
    if value not in {"ro", "rw"}:
        raise ValueError(f"{label}.mode must be 'ro' or 'rw'")
    return str(value)


def _parse_absolute_root(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "~" in value:
        raise ValueError(f"{label}.root must be an absolute path")
    root = Path(value)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError(f"{label}.root must be an absolute path without ~ or ..")
    return root.resolve()


def _parse_test_suites(
    value: Any, label: str, test_command: str | None,
) -> tuple[RepositoryTestSuite, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    suites: list[RepositoryTestSuite] = []
    names: set[str] = set()
    has_default = False
    for index, item in enumerate(value):
        suite_label = f"{label}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{suite_label} must be a mapping")
        missing = [key for key in ("name", "command") if key not in item]
        if missing:
            raise ValueError(f"{suite_label} missing required field(s): {', '.join(missing)}")
        if set(item) - {"name", "command", "default", "selector_prefixes", "selector_suffixes"}:
            raise ValueError(f"{suite_label} contains unknown fields")
        name = item["name"]
        if not isinstance(name, str) or _TEST_SUITE_NAME.fullmatch(name) is None:
            raise ValueError(f"{suite_label}.name must be a safe name matching {_TEST_SUITE_NAME.pattern}")
        if name in names:
            raise ValueError(f"{suite_label} duplicate test suite name: {name}")
        if name == "default" and test_command is not None:
            raise ValueError(f"{suite_label}.name 'default' is reserved when test_command is nonnull")
        command = item["command"]
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{suite_label}.command must be a non-empty string")
        default = item.get("default", False)
        if not isinstance(default, bool):
            raise ValueError(f"{suite_label}.default must be a boolean")
        if default and has_default:
            raise ValueError(f"{label} may have at most one default suite")
        selectors: dict[str, tuple[str, ...]] = {}
        for field in ("selector_prefixes", "selector_suffixes"):
            entries = item.get(field, [])
            if not isinstance(entries, list) or any(
                not isinstance(entry, str) or not entry.strip() for entry in entries
            ):
                raise ValueError(f"{suite_label}.{field} must be a list of non-empty strings")
            selectors[field] = tuple(entries)
        names.add(name)
        has_default = has_default or default
        suites.append(RepositoryTestSuite(name, command, default, **selectors))
    return tuple(suites)


def _parse_repositories(value: Any) -> tuple[RepositoryConfig, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("repository inventory repositories must be a list")
    repositories: list[RepositoryConfig] = []
    slugs: set[str] = set()
    roots: set[Path] = set()
    for index, item in enumerate(value):
        label = f"repository inventory repositories[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be a mapping")
        missing = [
            key
            for key in ("slug", "root", "mode", "origin", "base_branch")
            if key not in item
        ]
        if missing:
            raise ValueError(f"{label} missing required field(s): {', '.join(missing)}")
        slug = item["slug"]
        if not valid_repository_slug(slug):
            raise ValueError(f"{label}.slug must be owner/repository")
        slug = slug.lower()
        root = _parse_absolute_root(item["root"], label)
        origin = item["origin"]
        origin_match = _GITHUB_ORIGIN.fullmatch(origin) if isinstance(origin, str) else None
        if origin_match is None:
            raise ValueError(f"{label}.origin must be a GitHub repository URL")
        origin_slug = f"{origin_match.group('owner')}/{origin_match.group('repo')}".lower()
        if origin_slug != slug:
            raise ValueError(
                f"{label}.origin names {origin_slug}, which disagrees with slug {slug}"
            )
        base_branch = item["base_branch"]
        if not isinstance(base_branch, str) or not base_branch.strip():
            raise ValueError(f"{label}.base_branch must be a non-empty string")
        test_command = item.get("test_command")
        if test_command is not None and not isinstance(test_command, str):
            raise ValueError(f"{label}.test_command must be a string or null")
        test_suites = _parse_test_suites(
            item.get("test_suites", []), f"{label}.test_suites", test_command,
        )
        if set(item) - {"slug", "root", "mode", "origin", "base_branch", "test_command", "test_suites"}:
            raise ValueError(f"{label} contains unknown fields")
        if slug in slugs:
            raise ValueError(f"duplicate repository slug: {slug}")
        if root in roots:
            raise ValueError(f"duplicate repository root: {root}")
        slugs.add(slug)
        roots.add(root)
        repositories.append(
            RepositoryConfig(
                slug=slug,
                root=root,
                mode=_parse_mode(item["mode"], label),
                origin=origin.strip(),
                base_branch=base_branch.strip(),
                test_command=test_command,
                test_suites=test_suites,
            )
        )
    return tuple(repositories)


def _parse_allowed_roots(value: Any) -> tuple[AllowedRootConfig, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("repository inventory allowed_roots must be a list")
    roots: set[Path] = set()
    parsed: list[AllowedRootConfig] = []
    for index, item in enumerate(value):
        label = f"repository inventory allowed_roots[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be a mapping")
        if set(item) != {"root", "mode"}:
            raise ValueError(f"{label} must contain exactly root and mode")
        root = _parse_absolute_root(item["root"], label)
        if root in roots:
            raise ValueError(f"duplicate allowed root: {root}")
        roots.add(root)
        parsed.append(AllowedRootConfig(root, _parse_mode(item["mode"], label)))
    return tuple(parsed)

# Offline shadow parity
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Literal

from mimir.models import RepoPRAction


ParityDecision = Literal["allow", "refuse", "escalate", "unavailable"]
MismatchCategory = Literal[
    "decision", "result", "effect_evidence", "scope_binding",
]

_SCOPE_ID = re.compile(r"[0-9a-f]{64}")
_HEAD_SHA = re.compile(r"[0-9a-f]{40}")
_WRITE_OPERATIONS = frozenset({
    "pr_submit_review", "pr_comment", "pr_rerequest_review",
    "pr_edit_body",
    "repo_stage", "repo_commit", "repo_push", "unsupported_operation",
})
_OPERATION_ACTION = {
    "pr_metadata": RepoPRAction.INSPECT.value,
    "repo_status": RepoPRAction.INSPECT.value,
    "repo_test": RepoPRAction.TEST.value,
    "pr_submit_review": RepoPRAction.PR_REVIEW.value,
    "pr_comment": RepoPRAction.PR_COMMENT.value,
    "pr_edit_body": RepoPRAction.PR_EDIT.value,
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
        "pr_edit_body": "body_updated",
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
    if _OPERATION_ACTION[probe.operation] not in actions:
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
        ParityProbe("ordinary_review", "pr_edit_body", "review", _scope("ordinary_review"), head,
                    LegacyObservation("refuse", "scope_action_denied")),
        ParityProbe("own_pr_remediation", "pr_comment", "remediation", _scope("own_pr_remediation"), head,
                    LegacyObservation("allow", "comment_created", effect("own_pr_remediation", "pr_comment"), audit("own_pr_remediation", "pr_comment"))),
        ParityProbe("own_pr_remediation", "pr_edit_body", "remediation", _scope("own_pr_remediation"), head,
                    LegacyObservation("allow", "body_updated", effect("own_pr_remediation", "pr_edit_body"), audit("own_pr_remediation", "pr_edit_body"))),
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


# Scope-bound checkout leases
from dataclasses import dataclass, replace
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from typing import Callable, Iterator, Sequence
import uuid

from mimir._rmtree import rmtree_missing_ok
from mimir.models import (
    NormalizedPullRequestSnapshot,
    RepoPRAction,
    RepoPRActionScope,
    RepoReviewState,
)
from .worklink.checkout import (
    _assert_self_contained_checkout,
    _clone_attempt_checkout,
    _default_runner,
)


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
_METADATA = ".git/mimir-pr-checkout-lease.json"
_RECLAMATION_METADATA = ".git/mimir-pr-checkout-lease-reclamation.json"
_RECOVERY_DIRECTORY = ".recovery"
_TOMBSTONE_PREFIX = ".reclaiming-"
PUBLISHED_HEAD_REF = "refs/mimir/pr-checkout-lease/published"
_LEASE_ROOT_ENV = "MIMIR_PR_CHECKOUT_LEASE_ROOT"
_CLEANUP_IDENTITY_FIELDS = (
    "canonical_repo",
    "canonical_origin",
    "head_sha",
    "destination_ref",
    "owner",
    "scope_id",
    "path",
    "lease_root",
    "recovery_id",
)


def _report_retained_candidates(
    event_type: str,
    scope: RepoPRActionScope,
    candidates: Sequence[tuple[Path, str]],
) -> None:
    """Best-effort structured evidence for retained work and reconciliation refusals."""
    try:
        from mimir.event_logger import log_event_sync

        log_event_sync(
            event_type,
            repository=scope.canonical_repo,
            pull_request=scope.pr_number,
            scope_id=scope.scope_id,
            candidates=[
                {"commit": head, "path": str(path)} for path, head in candidates
            ],
        )
    except Exception:  # noqa: BLE001 - evidence failure must not change lease safety
        pass


def _report_lease_acquired(
    lease: PRCheckoutLease,
    *,
    acquisition: str,
    runner: Runner,
) -> None:
    """Best-effort evidence of the checkout head bound by an established lease."""
    try:
        from mimir.event_logger import log_event_sync

        head_sha = _run(
            runner,
            ["git", "-C", str(lease.path), "rev-parse", "--verify", "HEAD"],
            "acquired PR checkout lease has no HEAD",
        ).lower()
        log_event_sync(
            "pr_checkout_lease_acquired",
            repository=lease.canonical_repo,
            pull_request=lease.pr_number,
            scope_id=lease.scope_id,
            head_sha=head_sha,
            path=str(lease.path),
            owner=lease.owner,
            acquisition=acquisition,
        )
    except Exception:  # noqa: BLE001 - evidence failure must not change lease safety
        pass




@dataclass(frozen=True)
class PRCheckoutLease:
    canonical_repo: str
    canonical_origin: str
    source_root: Path
    scope_base_sha: str
    base_sha: str
    head_sha: str
    destination_ref: str
    owner: str
    scope_id: str
    path: Path
    lease_root: Path
    created_at: datetime
    expires_at: datetime
    recovery_id: str
    recovered: bool = False
    revoked: bool = False
    pr_number: int = 0
    # A missing or malformed lineage is never evidence of a clean local commit.
    lineage: dict[str, bool] | None = None
    verified_base_sha: str | None = None

    @property
    def write_root(self) -> Path:
        return self.path

    @property
    def is_active(self) -> bool:
        return not self.revoked and datetime.now(UTC) < self.expires_at and self.path.is_dir()

    def revoke(self) -> None:
        object.__setattr__(self, "revoked", True)


def _observe_current_pr_head(scope: RepoPRActionScope) -> str:
    """Read the current head from the server-configured forge adapter."""
    from .forge import _client_for_repository

    snapshot = _client_for_repository(scope.canonical_repo).get_pull_request_snapshot(
        scope.canonical_repo.lower(), scope.pr_number,
    )
    if (
        not isinstance(snapshot, NormalizedPullRequestSnapshot)
        or snapshot.number != scope.pr_number
        or snapshot.state != "open"
        or snapshot.head_ref != scope.head_ref
        or len(snapshot.head_sha) != 40
        or any(character not in "0123456789abcdef" for character in snapshot.head_sha.lower())
    ):
        raise RuntimeError("forge returned ambiguous pull-request head metadata")
    return snapshot.head_sha.lower()


def _report_superseded_lease(
    scope: RepoPRActionScope,
    lease: PRCheckoutLease,
    observed_head: str,
) -> None:
    try:
        from mimir.event_logger import log_event_sync

        log_event_sync(
            "pr_checkout_lease_superseded",
            repository=scope.canonical_repo,
            pull_request=scope.pr_number,
            scope_id=lease.scope_id,
            superseded_head=lease.head_sha,
            observed_head=observed_head,
            path=str(lease.path),
        )
    except Exception:  # noqa: BLE001 - evidence failure must not change lease safety
        pass


@dataclass(frozen=True)
class PRCheckoutLeaseReclamation:
    lease: str
    path: Path
    expired_by_s: float
    size_bytes: int
    publication_proof_present: bool
    reclaimed: bool
    error: str | None = None
    recovery_bundle: Path | None = None
    canonical_origin_contains_head: bool | None = None


def configured_pr_checkout_lease_root() -> Path:
    raw = os.environ.get(_LEASE_ROOT_ENV, "").strip()
    if not raw:
        raise RuntimeError(f"{_LEASE_ROOT_ENV} is not configured")
    root = Path(raw)
    if not root.is_absolute():
        raise RuntimeError(f"{_LEASE_ROOT_ENV} must be an absolute path")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    return root.resolve(strict=True)


def active_pr_checkout_lease_for_path(path: str | Path) -> PRCheckoutLease | None:
    """Return the active recorded lease containing a path, even if its leaf is missing."""
    try:
        root = configured_pr_checkout_lease_root()
        # Resolve existing ancestors and symlinks, but permit a missing leaf (or
        # missing descendants). The lease directory and record remain strict.
        resource = Path(path).resolve(strict=False)
        relative = resource.relative_to(root)
        if not relative.parts:
            return None
        lease_path = root / relative.parts[0]
        if lease_path.is_symlink() or not lease_path.is_dir():
            return None
        lease_path = lease_path.resolve(strict=True)
        if lease_path.parent != root or not resource.is_relative_to(lease_path):
            return None
        metadata_path = lease_path / _METADATA
        git_directory = metadata_path.parent
        if (
            (lease_path / _RECLAMATION_METADATA).exists()
            or git_directory.is_symlink()
            or not git_directory.is_dir()
            or metadata_path.is_symlink()
            or not metadata_path.is_file()
        ):
            return None
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        required_strings = (
            "canonical_repo",
            "canonical_origin",
            "owner",
            "head_sha",
            "scope_id",
            "path",
            "lease_root",
            "expires_at",
        )
        if any(
            not isinstance(raw.get(key), str) or not raw[key]
            for key in required_strings
        ):
            return None
        canonical_repo = raw["canonical_repo"]
        head_sha = raw["head_sha"]
        pr_number = raw.get("pr_number")
        if (
            canonical_repo.count("/") != 1
            or any(part in {"", ".", ".."} for part in canonical_repo.split("/"))
            or "#" in canonical_repo
            or not isinstance(pr_number, int)
            or isinstance(pr_number, bool)
            or pr_number <= 0
            or len(head_sha) != 40
            or any(character not in "0123456789abcdef" for character in head_sha.lower())
        ):
            return None
        if Path(raw["path"]).resolve(strict=True) != lease_path:
            return None
        if Path(raw["lease_root"]).resolve(strict=True) != root:
            return None
        lease = _lease_from_recorded_metadata(lease_path, root)
        if lease.expires_at <= datetime.now(UTC) or not lease.path.is_dir():
            return None
        return lease
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return None


def _run(runner: Runner, args: list[str], failure: str) -> str:
    result = runner(args)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or failure)
    return result.stdout.strip()


def _metadata(lease: PRCheckoutLease) -> dict[str, object]:
    return {
        "version": 3,
        "canonical_repo": lease.canonical_repo,
        "canonical_origin": lease.canonical_origin,
        "source_root": str(lease.source_root),
        "scope_base_sha": lease.scope_base_sha,
        "base_sha": lease.base_sha,
        "head_sha": lease.head_sha,
        "destination_ref": lease.destination_ref,
        "owner": lease.owner,
        "scope_id": lease.scope_id,
        "path": str(lease.path),
        "lease_root": str(lease.lease_root),
        "created_at": lease.created_at.isoformat(),
        "expires_at": lease.expires_at.isoformat(),
        "recovery_id": lease.recovery_id,
        "pr_number": lease.pr_number,
        "lineage": lease.lineage,
        "verified_base_sha": lease.verified_base_sha,
    }


def _recorded_lineage(raw: dict[str, object]) -> dict[str, bool] | None:
    value = raw.get("lineage")
    if not isinstance(value, dict) or len(value) > 1000 or any(
        not isinstance(sha, str) or len(sha) != 40
        or any(c not in "0123456789abcdef" for c in sha)
        or type(clean) is not bool for sha, clean in value.items()
    ):
        return None
    return value


def _recorded_verified_base(raw: dict[str, object]) -> str | None:
    value = raw.get("verified_base_sha")
    return value if isinstance(value, str) and len(value) == 40 and all(
        c in "0123456789abcdef" for c in value
    ) else None


def _safe_lease_path(root: Path, path: Path, *, must_exist: bool) -> Path:
    root = root.resolve(strict=True)
    if path.parent.resolve(strict=True) != root or path == root or path.is_symlink():
        raise RuntimeError(
            "PR checkout lease path escapes its configured root: "
            f"expected child of {str(root)!r}; actual {str(path)!r}"
        )
    if must_exist and not path.is_dir():
        raise RuntimeError("PR checkout lease path is missing")
    return path


def assert_live_pr_checkout_lease(lease: PRCheckoutLease) -> None:
    """Verify the recorded identity and expiry while the root is locked."""
    if lease.lease_root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    root = lease.lease_root.resolve(strict=True)
    path = _safe_lease_path(root, lease.path, must_exist=True)
    recorded = _lease_from_recorded_metadata(path, root)
    if (
        not lease.is_active
        or (path / _RECLAMATION_METADATA).exists()
        or any(getattr(recorded, key) != getattr(lease, key) for key in (
            "scope_id", "owner", "recovery_id", "expires_at", "head_sha",
        ))
    ):
        raise RuntimeError("PR checkout lease is no longer live")


@contextmanager
def hold_pr_checkout_lease(lease: PRCheckoutLease) -> Iterator[PRCheckoutLease]:
    """Keep the host's root lock from binding through the handler's last observation.

    Acquisition and reclamation take an exclusive lock on this same inode. A
    shared lock prevents either from rebinding or removing a checkout while its
    handler runs; metadata alone cannot provide that guarantee.
    """
    if lease.lease_root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    root = lease.lease_root.resolve(strict=True)
    directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_SH)
        assert_live_pr_checkout_lease(lease)
        yield lease
    finally:
        fcntl.flock(directory_fd, fcntl.LOCK_UN)
        os.close(directory_fd)


def create_pr_checkout_lease(
    scope: RepoPRActionScope,
    *,
    owner: str,
    lease_root: Path | None = None,
    ttl: timedelta = timedelta(hours=2),
    review_state: RepoReviewState | None = None,
    runner: Runner = _default_runner,
) -> PRCheckoutLease:
    """Publish a self-contained checkout only after exact scope verification."""
    if RepoPRAction.CHECKOUT.value not in scope.allowed_operations:
        raise RuntimeError("scope does not grant PR checkout")
    if owner != scope.principal:
        raise RuntimeError("PR checkout lease owner does not match scope")
    if ttl <= timedelta(0):
        raise ValueError("PR checkout lease ttl must be positive")

    if lease_root is not None and lease_root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    root = (lease_root.resolve(strict=True) if lease_root is not None
            else configured_pr_checkout_lease_root())
    source = Path(scope.canonical_root).resolve(strict=True)
    observed_origin = _run(
        runner, ["git", "-C", str(source), "config", "--get", "remote.origin.url"],
        "source checkout has no origin",
    )
    if observed_origin != scope.canonical_origin:
        raise RuntimeError("source checkout origin does not match PR scope")

    recovery_id = uuid.uuid4().hex
    name = f"{scope.scope_id[:16]}-{recovery_id}"
    path = root / name
    staging = root / f".{name}.staging"
    _safe_lease_path(root, staging, must_exist=False)
    if path.exists() or path.is_symlink() or staging.exists() or staging.is_symlink():
        raise RuntimeError("PR checkout lease collision")

    now = datetime.now(UTC)
    try:
        _clone_attempt_checkout(source, staging, runner=runner, event_logger=None)
        _run(
            runner,
            ["git", "-C", str(staging), "remote", "set-url", "origin", scope.canonical_origin],
            "git remote set-url failed",
        )
        _run(
            runner,
            ["git", "-C", str(staging), "fetch", "--no-tags", "origin",
             scope.checkout_ref or scope.destination_ref],
            "PR head fetch failed",
        )
        actual_head = _run(
            runner, ["git", "-C", str(staging), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
            "fetched PR head is missing",
        ).lower()
        if actual_head != scope.observed_head_sha.lower():
            raise RuntimeError(
                f"PR head advanced: scoped head {scope.observed_head_sha.lower()} is stale; "
                f"fetched head is {actual_head}"
            )
        _run(
            runner,
            ["git", "-C", str(staging), "fetch", "--no-tags", "origin", scope.base_ref],
            "PR base fetch failed",
        )
        actual_base = _run(
            runner, ["git", "-C", str(staging), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
            "fetched PR base is missing",
        ).lower()
        merge_base_result = runner([
            "git", "-C", str(staging), "merge-base", actual_head, actual_base,
        ])
        merge_base = (
            merge_base_result.stdout.strip().lower()
            if merge_base_result.returncode == 0 else "none"
        )
        scoped_base = scope.observed_base_sha.lower()
        if merge_base == actual_head:
            raise RuntimeError(
                "PR base already contains the authorized head: fetched base "
                f"{actual_base}; authorized head {actual_head} is stale as an open PR"
            )
        base_ancestry = runner([
            "git", "-C", str(staging), "merge-base", "--is-ancestor",
            scoped_base, actual_base,
        ])
        if base_ancestry.returncode == 1:
            raise RuntimeError(
                "PR base history rewritten: scoped base "
                f"{scoped_base} is unreachable from fetched base {actual_base}"
            )
        if base_ancestry.returncode != 0:
            raise RuntimeError(
                (base_ancestry.stderr or base_ancestry.stdout).strip()
                or "PR base ancestry check failed"
            )
        lease = PRCheckoutLease(
            canonical_repo=scope.canonical_repo,
            canonical_origin=scope.canonical_origin,
            source_root=source,
            scope_base_sha=scoped_base,
            base_sha=actual_base,
            head_sha=scope.observed_head_sha,
            destination_ref=scope.destination_ref,
            owner=owner,
            scope_id=scope.scope_id,
            path=path,
            lease_root=root,
            created_at=now,
            expires_at=now + ttl,
            recovery_id=recovery_id,
            pr_number=scope.pr_number,
            lineage={},
            verified_base_sha=actual_base,
        )
        _run(
            runner,
            ["git", "-C", str(staging), "checkout", "-B", scope.head_ref, actual_head],
            "PR head checkout failed",
        )
        checked_out = _run(
            runner, ["git", "-C", str(staging), "rev-parse", "--verify", "HEAD"],
            "checked-out PR head is missing",
        ).lower()
        if checked_out != scope.observed_head_sha.lower():
            raise RuntimeError("checked-out HEAD does not match immutable scope")
        _run(
            runner,
            ["git", "-C", str(staging), "update-ref", PUBLISHED_HEAD_REF, actual_head],
            "could not record the published PR head",
        )
        _assert_self_contained_checkout(staging, runner=runner)
        (staging / _METADATA).write_text(
            json.dumps(_metadata(lease), sort_keys=True) + "\n", encoding="utf-8",
        )
        os.replace(staging, path)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if review_state is not None:
        review_state.attach_checkout_lease(lease)
    return lease


def recover_pr_checkout_lease(
    path: Path, scope: RepoPRActionScope, *, owner: str,
    lease_root: Path | None = None, renew_expired: bool = False,
    ttl: timedelta = timedelta(hours=2), review_state: RepoReviewState | None = None,
    runner: Runner = _default_runner,
) -> PRCheckoutLease:
    """Rebind an existing checkout only while no handler can hold its old identity."""
    root = lease_root if lease_root is not None else configured_pr_checkout_lease_root()
    if root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    directory_fd = os.open(root.resolve(strict=True), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        return _recover_pr_checkout_lease_unlocked(
            path, scope, owner=owner, lease_root=lease_root,
            renew_expired=renew_expired, ttl=ttl,
            review_state=review_state, runner=runner,
        )
    finally:
        fcntl.flock(directory_fd, fcntl.LOCK_UN)
        os.close(directory_fd)


def _recover_pr_checkout_lease_unlocked(
    path: Path,
    scope: RepoPRActionScope,
    *,
    owner: str,
    lease_root: Path | None = None,
    renew_expired: bool = False,
    ttl: timedelta = timedelta(hours=2),
    review_state: RepoReviewState | None = None,
    runner: Runner = _default_runner,
) -> PRCheckoutLease:
    """Recover exact-scope work after revalidating its original remote PR head."""
    if RepoPRAction.CHECKOUT.value not in scope.allowed_operations:
        raise RuntimeError("scope does not grant PR checkout")
    if owner != scope.principal:
        raise RuntimeError("PR checkout lease owner does not match scope")
    if ttl <= timedelta(0):
        raise ValueError("PR checkout lease ttl must be positive")
    if lease_root is not None and lease_root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    root = (lease_root.resolve(strict=True) if lease_root is not None
            else configured_pr_checkout_lease_root())
    path = _safe_lease_path(root, path, must_exist=True)
    if (path / _RECLAMATION_METADATA).exists():
        raise RuntimeError("PR checkout lease is marked unreclaimable")
    try:
        raw = json.loads((path / _METADATA).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("PR checkout lease recovery metadata is invalid") from exc
    expected = {
        "canonical_repo": scope.canonical_repo,
        "canonical_origin": scope.canonical_origin,
        "source_root": str(Path(scope.canonical_root).resolve(strict=True)),
        "scope_base_sha": scope.observed_base_sha,
        "destination_ref": scope.destination_ref,
        "owner": owner,
        "scope_id": scope.scope_id,
        "path": str(path),
        "lease_root": str(root),
    }
    if not isinstance(raw, dict) or any(raw.get(key) != value for key, value in expected.items()):
        raise RuntimeError("PR checkout lease recovery scope mismatch")
    recorded_head = raw.get("head_sha")
    if not isinstance(recorded_head, str) or len(recorded_head) != 40 or any(
        c not in "0123456789abcdef" for c in recorded_head.lower()
    ):
        raise RuntimeError("PR checkout lease recovery metadata is invalid")
    if "pr_number" in raw and raw.get("pr_number") != scope.pr_number:
        raise RuntimeError("PR checkout lease recovery scope mismatch")
    base_sha = raw.get("base_sha")
    if not isinstance(base_sha, str) or len(base_sha) != 40 or any(
        character not in "0123456789abcdef" for character in base_sha.lower()
    ):
        raise RuntimeError("PR checkout lease recovery metadata is invalid")
    try:
        lease = PRCheckoutLease(
            **{
                **expected,
                "source_root": Path(str(expected["source_root"])),
                "base_sha": base_sha.lower(),
                "head_sha": recorded_head.lower(),
                "path": path,
                "lease_root": root,
            },
            created_at=datetime.fromisoformat(str(raw["created_at"])),
            expires_at=datetime.fromisoformat(str(raw["expires_at"])),
            recovery_id=str(raw["recovery_id"]),
            recovered=True,
            pr_number=scope.pr_number,
            lineage=_recorded_lineage(raw),
            verified_base_sha=_recorded_verified_base(raw),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("PR checkout lease recovery metadata is invalid") from exc
    expired = datetime.now(UTC) >= lease.expires_at
    if (lease.revoked or not path.is_dir() or expired) and not (expired and renew_expired):
        raise RuntimeError("PR checkout lease is expired")
    head = _run(
        runner, ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
        "recovered PR checkout has no HEAD",
    ).lower()
    origin = _run(
        runner, ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
        "recovered PR checkout has no origin",
    )
    branch = _run(
        runner, ["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"],
        "recovered PR checkout is detached",
    )
    # Recovery grants no push authority: preserve divergent fixes for explicit
    # reconciliation, enforcing checkout identity and a fresh remote head instead.
    if (
        origin != lease.canonical_origin
        or branch != scope.head_ref
    ):
        raise RuntimeError("recovered PR checkout identity mismatch")
    _assert_self_contained_checkout(path, runner=runner)
    _run(
        runner,
        ["git", "-C", str(path), "fetch", "--no-tags", "origin",
         scope.checkout_ref or scope.destination_ref],
        "PR head fetch failed during lease recovery",
    )
    remote_head = _run(
        runner, ["git", "-C", str(path), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
        "fetched PR head is missing during lease recovery",
    ).lower()
    if remote_head != scope.observed_head_sha.lower():
        raise RuntimeError(
            f"PR head advanced: scoped head {scope.observed_head_sha.lower()} is stale; "
            f"fetched head is {remote_head}"
        )
    lease = replace(
        lease, expires_at=datetime.now(UTC) + ttl,
        recovery_id=uuid.uuid4().hex,
    )
    (path / _METADATA).write_text(
        json.dumps(_metadata(lease), sort_keys=True) + "\n", encoding="utf-8",
    )
    if review_state is not None:
        review_state.attach_checkout_lease(lease)
        review_state.record_git_head(scope.scope_id, head)
    return lease


def replace_lease_expiry(lease: PRCheckoutLease, expires_at: datetime) -> PRCheckoutLease:
    """Renew a validated retained lease without changing any identity field."""
    values = {field: getattr(lease, field) for field in lease.__dataclass_fields__}
    values["expires_at"] = expires_at
    return PRCheckoutLease(**values)


def _lease_from_recorded_metadata(path: Path, root: Path) -> PRCheckoutLease:
    try:
        raw = json.loads((path / _METADATA).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("PR checkout lease reclamation metadata is invalid") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("PR checkout lease reclamation metadata is invalid")
    try:
        created_at = datetime.fromisoformat(str(raw["created_at"]))
        expires_at = datetime.fromisoformat(str(raw["expires_at"]))
        lease = PRCheckoutLease(
            canonical_repo=str(raw["canonical_repo"]),
            canonical_origin=str(raw["canonical_origin"]),
            source_root=Path(str(raw["source_root"])),
            scope_base_sha=str(raw.get("scope_base_sha", raw["base_sha"])),
            base_sha=str(raw["base_sha"]),
            head_sha=str(raw["head_sha"]),
            destination_ref=str(raw["destination_ref"]),
            owner=str(raw["owner"]),
            scope_id=str(raw["scope_id"]),
            path=path,
            lease_root=root,
            created_at=created_at,
            expires_at=expires_at,
            recovery_id=str(raw["recovery_id"]),
            pr_number=int(raw.get("pr_number", 0)),
            lineage=_recorded_lineage(raw),
            verified_base_sha=_recorded_verified_base(raw),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("PR checkout lease reclamation metadata is invalid") from exc
    if created_at.tzinfo is None or expires_at.tzinfo is None:
        raise RuntimeError("PR checkout lease reclamation metadata is invalid")
    return lease


def _checkout_size_bytes(path: Path) -> int:
    total = 0
    for current_root, directories, files in os.walk(path, followlinks=False):
        entries = [*(Path(current_root) / name for name in directories),
                   *(Path(current_root) / name for name in files)]
        for entry in entries:
            try:
                total += entry.lstat().st_blocks * 512
            except OSError:
                pass
    return total


def _origin_contains_head(lease: PRCheckoutLease, head: str, runner: Runner) -> bool | None:
    """Classify whether the current HEAD is reachable from a fetched origin ref."""
    fetched = runner([
        "git", "-C", str(lease.path), "fetch", "--no-tags", "origin",
    ])
    if fetched.returncode != 0:
        return None
    refs = runner([
        "git", "-C", str(lease.path), "for-each-ref", f"--contains={head}",
        "--format=%(refname)", "refs/remotes/origin",
    ])
    if refs.returncode != 0:
        return None
    return bool(refs.stdout.strip())


def _tree_blob(
    path: Path,
    revision: str,
    name: str,
    runner: Runner,
) -> str | None:
    """Return one path's blob object id, with absence represented explicitly."""
    result = runner([
        "git", "-C", str(path), "ls-tree", "-z", revision, "--", f":(literal){name}",
    ])
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout).strip()
            or f"PR checkout lease blob inspection failed for {name!r}"
        )
    if not result.stdout:
        return None
    entries = [entry for entry in result.stdout.split("\0") if entry]
    if len(entries) != 1 or "\t" not in entries[0]:
        raise RuntimeError(f"PR checkout lease blob inspection was ambiguous for {name!r}")
    metadata, observed_name = entries[0].split("\t", 1)
    fields = metadata.split()
    if observed_name != name or len(fields) != 3 or fields[1] != "blob":
        raise RuntimeError(f"PR checkout lease blob inspection was ambiguous for {name!r}")
    return fields[2].lower()


def _unexplained_publication_paths(
    lease: PRCheckoutLease,
    *,
    head: str,
    published_head: str,
    base_ref: str,
    runner: Runner,
) -> tuple[str, ...]:
    """Name HEAD paths whose content is in neither publication nor tracked base."""
    base_name = base_ref.removeprefix("refs/heads/")
    valid_base = runner(["git", "check-ref-format", "--branch", base_name])
    if valid_base.returncode != 0:
        raise RuntimeError("PR checkout lease tracked base ref is invalid")
    tracked_base = _run(
        runner,
        [
            "git", "-C", str(lease.path), "rev-parse", "--verify",
            f"refs/remotes/origin/{base_name}^{{commit}}",
        ],
        "PR checkout lease tracked base is unavailable",
    ).lower()
    bases = _run(
        runner,
        ["git", "-C", str(lease.path), "merge-base", "--all", head, tracked_base],
        "PR checkout lease HEAD has no verifiable tracked base",
    ).splitlines()
    if len(bases) != 1:
        raise RuntimeError("PR checkout lease HEAD has no unique tracked base")
    # A merge base computed against tracked_base is its ancestor by construction;
    # _run above and the uniqueness check are the fail-closed verification gates.
    base = bases[0].lower()
    changed = runner([
        "git", "-C", str(lease.path), "diff", "--name-only", "-z",
        published_head, head, "--",
    ])
    if changed.returncode != 0:
        raise RuntimeError(
            (changed.stderr or changed.stdout).strip()
            or "PR checkout lease changed-path inspection failed"
        )
    names = tuple(name for name in changed.stdout.split("\0") if name)
    unexplained = []
    for name in names:
        head_blob = _tree_blob(lease.path, head, name, runner)
        published_blob = _tree_blob(lease.path, published_head, name, runner)
        base_blob = _tree_blob(lease.path, base, name, runner)
        if head_blob not in {published_blob, base_blob}:
            unexplained.append(name)
    return tuple(unexplained)


def _preserve_checkout_head(lease: PRCheckoutLease, head: str, runner: Runner) -> Path:
    """Atomically preserve the complete history reachable from a lease HEAD."""
    recovery_root = lease.lease_root / _RECOVERY_DIRECTORY
    if recovery_root.is_symlink():
        raise RuntimeError("PR checkout lease recovery directory may not be a symlink")
    recovery_root.mkdir(mode=0o700, exist_ok=True)
    recovery_root = recovery_root.resolve(strict=True)
    if recovery_root.parent != lease.lease_root.resolve(strict=True):
        raise RuntimeError("PR checkout lease recovery directory escapes its lease root")
    bundle = recovery_root / f"{lease.path.name}-{lease.recovery_id}.bundle"
    if bundle.exists():
        verified = runner([
            "git", "-C", str(lease.path), "bundle", "verify", str(bundle),
        ])
        if verified.returncode != 0 or bundle.stat().st_size == 0:
            raise RuntimeError("existing PR checkout lease recovery bundle is invalid")
        heads = _run(
            runner,
            ["git", "-C", str(lease.path), "bundle", "list-heads", str(bundle)],
            "existing PR checkout lease recovery bundle has no heads",
        )
        if head not in {line.split()[0].lower() for line in heads.splitlines() if line}:
            raise RuntimeError("existing PR checkout lease recovery bundle has another HEAD")
        return bundle
    staging = recovery_root / f".{bundle.name}.{uuid.uuid4().hex}.tmp"
    try:
        _run(
            runner,
            ["git", "-C", str(lease.path), "bundle", "create", str(staging), "HEAD"],
            "PR checkout lease recovery bundle creation failed",
        )
        _run(
            runner,
            ["git", "-C", str(lease.path), "bundle", "verify", str(staging)],
            "PR checkout lease recovery bundle verification failed",
        )
        if staging.stat().st_size == 0:
            raise RuntimeError("PR checkout lease recovery bundle is empty")
        os.replace(staging, bundle)
    finally:
        staging.unlink(missing_ok=True)
    return bundle


def _preserve_dirty_worktree(lease: PRCheckoutLease, bundle: Path) -> Path:
    """Atomically archive dirty tracked and untracked files without changing them."""
    archive = bundle.with_suffix(".worktree.tar.gz")
    if archive.exists():
        if not archive.is_file() or archive.stat().st_size == 0:
            raise RuntimeError("existing dirty-worktree recovery archive is invalid")
        with tarfile.open(archive, "r:gz") as retained:
            if not retained.getmembers():
                raise RuntimeError("existing dirty-worktree recovery archive is empty")
        return archive

    staging = archive.with_name(f".{archive.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tarfile.open(staging, "w:gz") as retained:
            for entry in lease.path.iterdir():
                if entry.name != ".git":
                    retained.add(entry, arcname=str(Path("worktree") / entry.name))
        with tarfile.open(staging, "r:gz") as retained:
            if not retained.getmembers():
                raise RuntimeError("dirty-worktree recovery archive is empty")
        if staging.stat().st_size == 0:
            raise RuntimeError("dirty-worktree recovery archive is empty")
        os.replace(staging, archive)
    finally:
        staging.unlink(missing_ok=True)
    return archive


def _recover_retained_checkout(
    lease: PRCheckoutLease,
    *,
    head: str,
    dirty: bool,
    runner: Runner,
) -> Path:
    """Preserve all retained state before releasing its checkout directory."""
    try:
        bundle = _preserve_checkout_head(lease, head, runner)
        if not bundle.is_file() or bundle.stat().st_size == 0:
            raise RuntimeError("recovery bundle was not confirmed present and non-empty")
        if dirty:
            archive = _preserve_dirty_worktree(lease, bundle)
            if not archive.is_file() or archive.stat().st_size == 0:
                raise RuntimeError("dirty-worktree recovery archive was not confirmed")
    except (OSError, RuntimeError, tarfile.TarError) as exc:
        raise RuntimeError(
            "PR checkout lease recovery failed; lease retained at "
            f"{lease.path} with HEAD {head}. Operator must preserve the checkout "
            f"and recover or publish its work manually: {exc}"
        ) from exc
    return bundle


def _mark_lease_unreclaimable(
    lease: PRCheckoutLease,
    *,
    observed_at: datetime,
    error: str,
) -> None:
    marker = lease.path / _RECLAMATION_METADATA
    staging = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    record = {
        "version": 1,
        "classification": "unreclaimable",
        "lease": lease.path.name,
        "path": str(lease.path),
        "recovery_id": lease.recovery_id,
        "decided_at": observed_at.isoformat(),
        "error": error,
    }
    try:
        staging.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(staging, marker)
    finally:
        staging.unlink(missing_ok=True)


class _LeaseBecameActive(Exception):
    """A live turn attached to a lease during reclamation proof."""


def _reclaim_tombstone(lease: PRCheckoutLease, active_paths: Callable[[], set[Path]],
                       on_step: Callable[[str], None]) -> None:
    # The caller holds the root flock. Recheck immediately before removing the
    # live name: Git commands during proof/bundling can attach a live turn.
    on_step("rename")
    if lease.path.resolve(strict=False) in active_paths():
        raise _LeaseBecameActive
    tombstone = lease.lease_root / f"{_TOMBSTONE_PREFIX}{lease.path.name}-{uuid.uuid4().hex}"
    os.rename(lease.path, tombstone)
    on_step("delete")
    rmtree_missing_ok(tombstone)


def reclaim_expired_pr_checkout_leases(
    lease_root: Path,
    *,
    active_paths: set[Path] | Callable[[], set[Path]] | None = None,
    now: datetime | None = None,
    runner: Runner = _default_runner,
    event_logger: Callable[..., None] | None = None,
) -> list[PRCheckoutLeaseReclamation]:
    """Safely reclaim expired leases not attached to a live turn."""
    if lease_root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    root = lease_root.resolve(strict=True)
    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        raise ValueError("PR checkout lease reclamation time must be timezone-aware")
    if event_logger is None:
        from mimir.event_logger import log_event_sync

        event_logger = log_event_sync
    results: list[PRCheckoutLeaseReclamation] = []
    directory_fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        def active() -> set[Path]:
            current = active_paths() if callable(active_paths) else active_paths
            return {path.resolve(strict=False) for path in (current or set())}

        for tombstone in sorted(root.glob(f"{_TOMBSTONE_PREFIX}*")):
            if tombstone.is_symlink() or not tombstone.is_dir():
                continue
            try:
                rmtree_missing_ok(tombstone)
            except OSError as exc:
                name = tombstone.name.removeprefix(_TOMBSTONE_PREFIX).rsplit("-", 1)[0]
                event_logger(
                    "pr_checkout_lease_reclaim_failed", lease=name,
                    recovery_id=name.rsplit("-", 1)[-1], step="delete", error=str(exc),
                )
        for path in sorted(root.iterdir()):
            if path.name.startswith(".") or not path.is_dir() or path.is_symlink():
                continue
            if (path / _RECLAMATION_METADATA).is_file():
                continue
            try:
                lease = _lease_from_recorded_metadata(path, root)
            except RuntimeError:
                continue
            if observed_at < lease.expires_at or path.resolve(strict=False) in active():
                continue
            size_bytes = 0
            proof = False
            error: str | None = None
            reclaimed = False
            recovery_bundle: Path | None = None
            canonical_origin_contains_head: bool | None = None
            step = "proof"

            def on_step(value: str) -> None:
                nonlocal step
                step = value

            def delete_verified(_lease: PRCheckoutLease) -> None:
                _reclaim_tombstone(_lease, active, on_step)

            try:
                size_bytes = _checkout_size_bytes(path)
                proof = runner([
                    "git", "-C", str(path), "rev-parse", "--verify",
                    f"{PUBLISHED_HEAD_REF}^{{commit}}",
                ]).returncode == 0
                head = _run(
                    runner,
                    ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
                    "PR checkout lease reclamation found no HEAD",
                ).lower()
                canonical_origin_contains_head = _origin_contains_head(lease, head, runner)
                reclaimed = _cleanup_pr_checkout_lease_unlocked(
                    lease, runner=runner, deleter=delete_verified,
                    before_bundle=lambda: on_step("bundle"),
                )
                candidate = (
                    root / _RECOVERY_DIRECTORY
                    / f"{lease.path.name}-{lease.recovery_id}.bundle"
                )
                if candidate.is_file():
                    recovery_bundle = candidate
            except _LeaseBecameActive:
                continue
            except (OSError, RuntimeError) as exc:
                error = str(exc)
                if error in {
                    "PR checkout lease cleanup found no publication proof",
                } or error.startswith("PR checkout lease cleanup publication mismatch:"):
                    try:
                        on_step("proof")
                        head = _run(
                            runner,
                            ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
                            "PR checkout lease reclamation found no HEAD",
                        ).lower()
                        canonical_origin_contains_head = _origin_contains_head(
                            lease, head, runner,
                        )
                        on_step("bundle")
                        recovery_bundle = _preserve_checkout_head(lease, head, runner)
                        delete_verified(lease)
                        reclaimed = True
                        error = None
                    except _LeaseBecameActive:
                        continue
                    except (OSError, RuntimeError) as preservation_exc:
                        error = (
                            f"{error}; preservation failed: {preservation_exc}"
                        )
                if not reclaimed:
                    event_logger(
                        "pr_checkout_lease_reclaim_failed", lease=path.name,
                        recovery_id=lease.recovery_id, step=step, error=error,
                    )
                    if path.is_dir():
                        try:
                            _mark_lease_unreclaimable(
                                lease, observed_at=observed_at, error=error,
                            )
                        except OSError as marker_exc:
                            error = f"{error}; terminal marker failed: {marker_exc}"
            result = PRCheckoutLeaseReclamation(
                lease=path.name,
                path=path,
                expired_by_s=max(0.0, (observed_at - lease.expires_at).total_seconds()),
                size_bytes=size_bytes,
                publication_proof_present=proof,
                reclaimed=reclaimed,
                error=error,
                recovery_bundle=recovery_bundle,
                canonical_origin_contains_head=canonical_origin_contains_head,
            )
            results.append(result)
            event_logger(
                "pr_checkout_lease_reclaimed" if reclaimed else "pr_checkout_lease_unreclaimable",
                lease=result.lease,
                path=str(result.path),
                expired_by_s=round(result.expired_by_s, 3),
                size_bytes=result.size_bytes,
                publication_proof_present=result.publication_proof_present,
                error=result.error,
                recovery_bundle=(
                    str(result.recovery_bundle) if result.recovery_bundle else None
                ),
                canonical_origin_contains_head=result.canonical_origin_contains_head,
            )
    finally:
        fcntl.flock(directory_fd, fcntl.LOCK_UN)
        os.close(directory_fd)
    event_logger(
        "pr_checkout_lease_reaper_sweep",
        expired_count=len(results),
        reclaimed_count=sum(result.reclaimed for result in results),
        retained_count=sum(not result.reclaimed for result in results),
    )
    return results


def _retained_candidate_head(
    path: Path,
    scope: RepoPRActionScope,
    *,
    root: Path,
    owner: str,
    runner: Runner,
) -> str:
    """Validate retained metadata without renewing or granting publication rights."""
    path = _safe_lease_path(root, path, must_exist=True)
    try:
        raw = json.loads((path / _METADATA).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("PR checkout lease recovery metadata is invalid") from exc
    expected = {
        "canonical_repo": scope.canonical_repo,
        "canonical_origin": scope.canonical_origin,
        "owner": owner,
        "scope_id": scope.scope_id,
        "destination_ref": scope.destination_ref,
        "path": str(path),
        "lease_root": str(root),
    }
    if not isinstance(raw, dict) or any(raw.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"PR checkout lease recovery scope mismatch at {path}")
    recorded_head = raw.get("head_sha")
    if not isinstance(recorded_head, str) or len(recorded_head) != 40 or any(
        c not in "0123456789abcdef" for c in recorded_head.lower()
    ):
        raise RuntimeError("PR checkout lease recovery metadata is invalid")
    if "pr_number" in raw and raw.get("pr_number") != scope.pr_number:
        raise RuntimeError(f"PR checkout lease recovery scope mismatch at {path}")
    head = _run(
        runner, ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
        "retained PR checkout has no HEAD",
    ).lower()
    return head


def _foreign_candidate_head(
    lease: PRCheckoutLease,
    scope: RepoPRActionScope,
    *,
    owner: str,
    runner: Runner,
) -> tuple[str, bool]:
    """Classify same-PR work from another turn without trusting its scope id."""
    head = _run(
        runner,
        ["git", "-C", str(lease.path), "rev-parse", "--verify", "HEAD"],
        "retained PR checkout has no HEAD",
    ).lower()
    expected_source = Path(scope.canonical_root).resolve(strict=True)
    if (
        lease.canonical_repo != scope.canonical_repo
        or lease.canonical_origin != scope.canonical_origin
        or lease.source_root.resolve(strict=True) != expected_source
        or lease.owner != owner
        or lease.pr_number != scope.pr_number
        or lease.destination_ref != scope.destination_ref
    ):
        return head, False
    origin = _run(
        runner,
        ["git", "-C", str(lease.path), "config", "--get", "remote.origin.url"],
        "retained PR checkout has no origin",
    )
    branch = _run(
        runner,
        ["git", "-C", str(lease.path), "symbolic-ref", "--quiet", "--short", "HEAD"],
        "retained PR checkout is detached",
    )
    if origin != scope.canonical_origin or branch != scope.head_ref:
        return head, False
    _assert_self_contained_checkout(lease.path, runner=runner)
    observed_head = scope.observed_head_sha.lower()
    # Same-identity work must remain resumable even after rewriting its patches.
    # This grants no push authority; rebind freshly verifies the remote head and
    # publication must separately reconcile it. Keep stale-head cleanup below.
    published = runner([
        "git", "-C", str(lease.path), "rev-parse", "--verify",
        f"{PUBLISHED_HEAD_REF}^{{commit}}",
    ])
    if (lease.head_sha.lower() == observed_head or (
        published.returncode == 0 and published.stdout.strip().lower() == observed_head
        and isinstance(lease.lineage, dict)
        and lease.head_sha.lower() in lease.lineage
    )):
        return head, True
    if runner([
        "git", "-C", str(lease.path), "cat-file", "-e", f"{observed_head}^{{commit}}",
    ]).returncode != 0:
        _run(
            runner,
            ["git", "-C", str(lease.path), "fetch", "--no-tags", "origin",
             scope.checkout_ref or scope.destination_ref],
            "PR head fetch failed during candidate ancestry inspection",
        )
        fetched_head = _run(
            runner,
            ["git", "-C", str(lease.path), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
            "fetched PR head is missing during candidate ancestry inspection",
        ).lower()
        if fetched_head != observed_head:
            raise RuntimeError("PR head advanced during candidate ancestry inspection")
    ancestor = runner([
        "git", "-C", str(lease.path), "merge-base", "--is-ancestor",
        scope.observed_head_sha.lower(), head,
    ])
    if ancestor.returncode == 0:
        return head, True
    if ancestor.returncode != 1:
        raise RuntimeError(
            (ancestor.stderr or ancestor.stdout).strip()
            or "PR checkout lease candidate ancestry inspection failed"
        )
    return head, False


def _rebind_foreign_candidate(
    lease: PRCheckoutLease,
    scope: RepoPRActionScope,
    *,
    head: str,
    ttl: timedelta,
    review_state: RepoReviewState | None,
    runner: Runner,
) -> PRCheckoutLease:
    """Move a proven same-PR lease onto the current immutable turn scope."""
    _run(
        runner,
        [
            "git", "-C", str(lease.path), "fetch", "--no-tags", "origin",
            scope.checkout_ref or scope.destination_ref,
        ],
        "PR head fetch failed during lease recovery",
    )
    remote_head = _run(
        runner,
        ["git", "-C", str(lease.path), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
        "fetched PR head is missing during lease recovery",
    ).lower()
    if remote_head != scope.observed_head_sha.lower():
        raise RuntimeError(
            f"PR head advanced: scoped head {scope.observed_head_sha.lower()} is stale; "
            f"fetched head is {remote_head}"
        )
    _run(
        runner,
        ["git", "-C", str(lease.path), "fetch", "--no-tags", "origin", scope.base_ref],
        "PR base fetch failed during lease recovery",
    )
    actual_base = _run(
        runner,
        ["git", "-C", str(lease.path), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
        "fetched PR base is missing during lease recovery",
    ).lower()
    base_ancestry = runner([
        "git", "-C", str(lease.path), "merge-base", "--is-ancestor",
        scope.observed_base_sha.lower(), actual_base,
    ])
    if base_ancestry.returncode != 0:
        raise RuntimeError(
            (base_ancestry.stderr or base_ancestry.stdout).strip()
            or "PR checkout lease current base ancestry check failed"
        )
    if _run(
        runner,
        ["git", "-C", str(lease.path), "rev-parse", "--verify", "HEAD"],
        "retained PR checkout has no HEAD",
    ).lower() != head:
        raise RuntimeError("retained PR checkout HEAD changed during lease recovery")

    rebound = replace(
        lease,
        recovery_id=uuid.uuid4().hex,
        scope_base_sha=scope.observed_base_sha.lower(),
        base_sha=actual_base,
        head_sha=(head if lease.head_sha.lower() == head else scope.observed_head_sha.lower()),
        scope_id=scope.scope_id,
        expires_at=datetime.now(UTC) + ttl,
        recovered=True,
        pr_number=scope.pr_number,
        lineage=lease.lineage,
        verified_base_sha=actual_base,
    )
    metadata_path = lease.path / _METADATA
    staging = metadata_path.with_name(f".{metadata_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        staging.write_text(
            json.dumps(_metadata(rebound), sort_keys=True) + "\n", encoding="utf-8",
        )
        os.replace(staging, metadata_path)
    finally:
        staging.unlink(missing_ok=True)
    if review_state is not None:
        review_state.attach_checkout_lease(rebound)
        review_state.record_git_head(scope.scope_id, head)
    return rebound


def acquire_pr_checkout_lease(
    scope: RepoPRActionScope,
    *,
    owner: str,
    lease_root: Path | None = None,
    ttl: timedelta = timedelta(hours=2),
    review_state: RepoReviewState | None = None,
    runner: Runner = _default_runner,
) -> tuple[PRCheckoutLease, tuple[str, ...]]:
    """Resume the sole exact-scope lease, refusing divergent retained fixes."""
    if RepoPRAction.CHECKOUT.value not in scope.allowed_operations:
        raise RuntimeError("scope does not grant PR checkout")
    if owner != scope.principal:
        raise RuntimeError("PR checkout lease owner does not match scope")
    if ttl <= timedelta(0):
        raise ValueError("PR checkout lease ttl must be positive")
    if lease_root is not None and lease_root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    root = (lease_root.resolve(strict=True) if lease_root is not None
            else configured_pr_checkout_lease_root())
    from mimir.tools.budget_gate import borrowed_pr_checkout_lock

    directory_fd = os.open(root, os.O_RDONLY)
    borrowed = borrowed_pr_checkout_lock(root)
    try:
        if not borrowed:
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
        prefix = f"{scope.scope_id[:16]}-"
        paths: list[Path] = []
        foreign_leases: list[PRCheckoutLease] = []
        for path in sorted(root.iterdir()):
            if path.name.startswith(".") or not path.is_dir() or path.is_symlink():
                continue
            try:
                raw = json.loads((path / _METADATA).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                if path.name.startswith(prefix):
                    raise RuntimeError("PR checkout lease recovery metadata is invalid")
                continue
            if not isinstance(raw, dict):
                if path.name.startswith(prefix):
                    raise RuntimeError("PR checkout lease recovery metadata is invalid")
                continue
            same_pr = (
                raw.get("canonical_repo") == scope.canonical_repo
                and raw.get("owner") == owner
                and (
                    raw.get("pr_number") == scope.pr_number
                    or (
                        "pr_number" not in raw
                        and raw.get("destination_ref") == scope.destination_ref
                    )
                )
            )
            if (path / _RECLAMATION_METADATA).exists():
                if raw.get("scope_id") == scope.scope_id or same_pr:
                    _report_retained_candidates(
                        "pr_checkout_lease_retained", scope, [(path, "<unverifiable>")],
                    )
                continue
            if raw.get("scope_id") == scope.scope_id or same_pr:
                if runner([
                    "git", "-C", str(path), "rev-parse", "--verify", "HEAD",
                ]).returncode != 0:
                    _report_retained_candidates(
                        "pr_checkout_lease_retained", scope, [(path, "<unverifiable>")],
                    )
                    continue
            if raw.get("scope_id") == scope.scope_id:
                paths.append(path)
                continue
            if path.name.startswith(prefix):
                raise RuntimeError(f"PR checkout lease recovery scope mismatch at {path}")
            if same_pr:
                foreign_leases.append(_lease_from_recorded_metadata(path, root))
        candidates = [
            (path, _retained_candidate_head(
                path, scope, root=root, owner=owner, runner=runner,
            ))
            for path in paths
        ]
        foreign_candidates: list[tuple[Path, str]] = []
        rebound_candidates: dict[Path, PRCheckoutLease] = {}
        if foreign_leases:
            from mimir._context import active_pr_checkout_lease_paths

            active_paths = {
                path.resolve(strict=False) for path in active_pr_checkout_lease_paths()
            }
            live_leases = [
                lease for lease in foreign_leases
                if lease.path.resolve(strict=False) in active_paths
            ]
            for lease in live_leases:
                head = _run(
                    runner,
                    ["git", "-C", str(lease.path), "rev-parse", "--verify", "HEAD"],
                    "live PR checkout lease has no HEAD",
                ).lower()
                foreign_candidates.append((lease.path, head))
            foreign_leases = [
                lease for lease in foreign_leases if lease not in live_leases
            ]

            try:
                observed_head = _observe_current_pr_head(scope)
            except Exception:  # noqa: BLE001 - observation failures must fail closed
                observed_head = None

            classified = {
                lease.path: _foreign_candidate_head(
                    lease, scope, owner=owner, runner=runner,
                )
                for lease in foreign_leases
                if observed_head == scope.observed_head_sha.lower()
            }
            stale = [
                lease for lease in foreign_leases
                if observed_head is not None and lease.head_sha.lower() != observed_head
                and not classified.get(lease.path, ("", False))[1]
            ]
            retained: list[tuple[Path, str]] = []
            for lease in stale:
                head = _run(
                    runner,
                    ["git", "-C", str(lease.path), "rev-parse", "--verify", "HEAD"],
                    "retained PR checkout has no HEAD",
                ).lower()
                status = _run(
                    runner,
                    ["git", "-C", str(lease.path), "status", "--porcelain=v1",
                     "--untracked-files=all"],
                    "retained PR checkout status inspection failed",
                )
                if status:
                    retained.append((lease.path, head))
                    continue
                if head != lease.head_sha.lower():
                    published_head = _run(
                        runner,
                        [
                            "git", "-C", str(lease.path), "rev-parse", "--verify",
                            f"{PUBLISHED_HEAD_REF}^{{commit}}",
                        ],
                        "PR checkout lease cleanup found no publication proof",
                    ).lower()
                    ancestor = runner([
                        "git", "-C", str(lease.path), "merge-base", "--is-ancestor",
                        head, published_head,
                    ])
                    if ancestor.returncode not in {0, 1}:
                        raise RuntimeError(
                            (ancestor.stderr or ancestor.stdout).strip()
                            or "PR checkout lease publication ancestry inspection failed"
                        )
                    unexplained = () if ancestor.returncode == 0 else (
                        _unexplained_publication_paths(
                            lease,
                            head=head,
                            published_head=published_head,
                            base_ref=scope.base_ref,
                            runner=runner,
                        )
                    )
                    if unexplained and _origin_contains_head(lease, head, runner) is not True:
                        retained.append((lease.path, head))

            if retained:
                _report_retained_candidates("pr_checkout_lease_retained", scope, retained)

            for lease in stale:
                _cleanup_pr_checkout_lease_unlocked(
                    lease, base_ref=scope.base_ref, runner=runner,
                )
                _report_superseded_lease(scope, lease, observed_head)
            foreign_leases = [lease for lease in foreign_leases if lease not in stale]
            for lease in foreign_leases:
                head, reusable = classified.get(lease.path) or (
                    _run(
                        runner,
                        ["git", "-C", str(lease.path), "rev-parse", "--verify", "HEAD"],
                        "retained PR checkout has no HEAD",
                    ).lower(), False,
                )
                if reusable and observed_head == scope.observed_head_sha.lower():
                    candidates.append((lease.path, head))
                    rebound_candidates[lease.path] = lease
                else:
                    foreign_candidates.append((lease.path, head))

        unpublished = [
            (path, head) for path, head in candidates
            if head != scope.observed_head_sha.lower()
        ]
        if foreign_candidates:
            _report_retained_candidates(
                "pr_checkout_lease_scope_conflict",
                scope,
                [*unpublished, *foreign_candidates],
            )
            named = ", ".join(
                f"{head} ({path}, scope mismatch)"
                for path, head in [*unpublished, *foreign_candidates]
            )
            raise RuntimeError(
                "unpublished PR checkout lease candidates include another scope; "
                f"refusing reuse: {named}"
            )
        distinct = sorted({head for _path, head in unpublished})
        if len(distinct) > 1:
            _report_retained_candidates(
                "pr_checkout_lease_candidates_diverged", scope, unpublished,
            )
            named = ", ".join(f"{head} ({path})" for path, head in unpublished)
            raise RuntimeError(
                "divergent unpublished PR checkout lease candidates; "
                f"refusing implicit selection: {named}"
            )
        if candidates:
            chosen_path, chosen_head = unpublished[0] if unpublished else candidates[0]
            if chosen_path in rebound_candidates:
                lease = _rebind_foreign_candidate(
                    rebound_candidates[chosen_path],
                    scope,
                    head=chosen_head,
                    ttl=ttl,
                    review_state=review_state,
                    runner=runner,
                )
            else:
                lease = _recover_pr_checkout_lease_unlocked(
                    chosen_path,
                    scope,
                    owner=owner,
                    lease_root=root,
                    renew_expired=True,
                    ttl=ttl,
                    review_state=review_state,
                    runner=runner,
                )
            if unpublished:
                _report_retained_candidates(
                    "pr_checkout_lease_resumed", scope, unpublished,
                )
            _report_lease_acquired(lease, acquisition="resume", runner=runner)
            return lease, tuple(distinct)
        lease = create_pr_checkout_lease(
            scope,
            owner=owner,
            lease_root=root,
            ttl=ttl,
            review_state=review_state,
            runner=runner,
        )
        _report_lease_acquired(lease, acquisition="fresh", runner=runner)
        return lease, ()
    finally:
        if not borrowed:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)
        os.close(directory_fd)


def cleanup_pr_checkout_lease(
    lease: PRCheckoutLease, *, base_ref: str | None = None,
    review_state: RepoReviewState | None = None,
    runner: Runner = _default_runner,
    deleter: Callable[[PRCheckoutLease], None] | None = None,
    before_bundle: Callable[[], None] | None = None,
) -> bool:
    """Reclaim a checkout without racing an active repository handler."""
    if lease.lease_root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    directory_fd = os.open(
        lease.lease_root.resolve(strict=True), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        return _cleanup_pr_checkout_lease_unlocked(
            lease, base_ref=base_ref, review_state=review_state,
            runner=runner, deleter=deleter, before_bundle=before_bundle,
        )
    finally:
        fcntl.flock(directory_fd, fcntl.LOCK_UN)
        os.close(directory_fd)


def _cleanup_pr_checkout_lease_unlocked(
    lease: PRCheckoutLease,
    *,
    base_ref: str | None = None,
    review_state: RepoReviewState | None = None,
    runner: Runner = _default_runner,
    deleter: Callable[[PRCheckoutLease], None] | None = None,
    before_bundle: Callable[[], None] | None = None,
) -> bool:
    """Remove an exact lease, preserving any work not proven published."""
    if not lease.path.exists() and not lease.path.is_symlink():
        if review_state is not None:
            review_state.revoke_checkout_lease(lease)
        lease.revoke()
        return False
    _safe_lease_path(lease.lease_root, lease.path, must_exist=True)
    try:
        raw = json.loads((lease.path / _METADATA).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("PR checkout lease cleanup metadata is invalid") from exc
    expected = _metadata(lease)
    if not isinstance(raw, dict):
        raise RuntimeError(
            "PR checkout lease cleanup metadata type mismatch: "
            f"expected dict; actual {type(raw).__name__}"
        )
    for key in _CLEANUP_IDENTITY_FIELDS:
        if key not in raw:
            raise RuntimeError(
                f"PR checkout lease cleanup metadata {key} mismatch: "
                f"expected {expected[key]!r}; actual <missing>"
            )
    for key, value in expected.items():
        # Version describes metadata shape, not checkout identity. Older shapes
        # remain safe when all identity fields are present and match.
        if key != "version" and key in raw and raw[key] != value:
            raise RuntimeError(
                f"PR checkout lease cleanup metadata {key} mismatch: "
                f"expected {value!r}; actual {raw[key]!r}"
            )
    head = _run(
        runner,
        ["git", "-C", str(lease.path), "rev-parse", "--verify", "HEAD"],
        "PR checkout lease cleanup found no HEAD",
    ).lower()
    origin = _run(
        runner,
        ["git", "-C", str(lease.path), "config", "--get", "remote.origin.url"],
        "PR checkout lease cleanup found no origin",
    )
    if origin != lease.canonical_origin:
        raise RuntimeError(
            "PR checkout lease cleanup origin mismatch: "
            f"expected {lease.canonical_origin!r}; actual {origin!r}"
        )
    expected_branch = lease.destination_ref.removeprefix("refs/heads/")
    branch_result = runner([
        "git", "-C", str(lease.path), "symbolic-ref", "--quiet", "--short", "HEAD",
    ])
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "<detached>"
    if branch != expected_branch:
        raise RuntimeError(
            "PR checkout lease cleanup branch mismatch: "
            f"expected {expected_branch!r}; actual {branch!r}"
        )
    status = _run(
        runner,
        ["git", "-C", str(lease.path), "status", "--porcelain=v1",
         "--untracked-files=all"],
        "PR checkout lease cleanup status inspection failed",
    )
    published = runner([
        "git", "-C", str(lease.path), "rev-parse", "--verify",
        f"{PUBLISHED_HEAD_REF}^{{commit}}",
    ])
    ancestor_returncode = 1
    published_head = "<missing>"
    if published.returncode == 0:
        published_head = published.stdout.strip().lower()
        ancestor = runner([
            "git", "-C", str(lease.path), "merge-base", "--is-ancestor", head,
            published_head,
        ])
        if ancestor.returncode not in {0, 1}:
            raise RuntimeError(
                (ancestor.stderr or ancestor.stdout).strip()
                or "PR checkout lease publication ancestry inspection failed"
            )
        ancestor_returncode = ancestor.returncode
    unexplained: tuple[str, ...] = ()
    if published.returncode == 0 and ancestor_returncode == 1 and base_ref is not None:
        unexplained = _unexplained_publication_paths(
            lease,
            head=head,
            published_head=published_head,
            base_ref=base_ref,
            runner=runner,
        )
    retained_head = (
        published.returncode != 0
        or (ancestor_returncode == 1 and (base_ref is None or bool(unexplained)))
    )
    if retained_head and not status and _origin_contains_head(lease, head, runner) is True:
        retained_head = False
    if status or retained_head:
        if before_bundle is not None:
            before_bundle()
        _recover_retained_checkout(
            lease, head=head, dirty=bool(status), runner=runner,
        )
    if deleter is None:
        rmtree_missing_ok(lease.path)
    else:
        deleter(lease)
    if review_state is not None:
        review_state.revoke_checkout_lease(lease)
    lease.revoke()
    return True

# Typed Git operations
import base64
import json
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, replace
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import subprocess
import threading
import time
from typing import Any, Iterator, Literal, Protocol, TypeAlias
import uuid
from urllib.parse import urlsplit

from mimir.access_control import ToolFlowDirection, authorize_repo_pr_tool
from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME
from mimir.models import RetainedFactoryScope, RepoPRAction, RepoPRActionScope, RepoReviewState

from mimir.redaction import redact_text


_DEFAULT_GIT = Path("/usr/bin/git")
_DEFAULT_TIMEOUT_SECONDS = 20.0
_DEFAULT_OUTPUT_BYTES = 1_048_576
_MAX_PATHS = 256
_MAX_MESSAGE_BYTES = 64 * 1024
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_REF_RE = re.compile(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*")
_FILTER_KEY_RE = re.compile(r"filter\.([^.\x00]+)\.(clean|smudge|process)")
_MERGE_KEY_RE = re.compile(r"merge\.([^.\x00]+)\.driver")
_RECENT_AGENT_PUSH_LIMIT = 1024
_URL_USERINFO_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]{0,31}://)[^/@\s]+@")
_HISTORY_REWRITE_EVENT_TYPES = frozenset({
    # Own-PR heartbeat maintenance has the same rebase publication authority as
    # poller remediation, still bounded by exact-head leases and protected refs.
    "heartbeat_pr_maintenance",
    "pr_changes_requested_stale",
    "pr_ci_failure",
    "pr_mergeability_rebase",
    "pr_mergeability_conflicting",
})
_PROTECTED_BRANCH_REFS = frozenset({"refs/heads/main", "refs/heads/master"})
_recent_agent_pushes: OrderedDict[tuple[str, int, str, str], None] = OrderedDict()
_recent_agent_pushes_lock = threading.Lock()
_verified_pushes: OrderedDict[tuple[str, int, str, str], None] = OrderedDict()
_verified_pushes_lock = threading.Lock()
_ancestral_verified_pushes: OrderedDict[tuple[str, int, str], None] = OrderedDict()


def _history_rewrite_push_permitted(scope: RepoPRActionScope) -> bool:
    return (
        scope.event_type in _HISTORY_REWRITE_EVENT_TYPES
        and scope.destination_ref not in _PROTECTED_BRANCH_REFS
    )


def _record_agent_push(scope: RepoPRActionScope, pushed_head: str) -> None:
    if scope.event_type != "pr_changes_requested_stale":
        return
    key = (
        scope.canonical_repo.lower(), scope.pr_number,
        scope.observed_head_sha.lower(), pushed_head.lower(),
    )
    with _recent_agent_pushes_lock:
        _recent_agent_pushes[key] = None
        _recent_agent_pushes.move_to_end(key)
        while len(_recent_agent_pushes) > _RECENT_AGENT_PUSH_LIMIT:
            _recent_agent_pushes.popitem(last=False)


def was_agent_push(
    repository: str, pull_request: int, previous_head: str, current_head: str,
) -> bool:
    """Return whether this process verified the exact remediation push."""
    key = (repository.lower(), pull_request, previous_head.lower(), current_head.lower())
    with _recent_agent_pushes_lock:
        return key in _recent_agent_pushes


def _record_verified_push(
    scope: RepoPRActionScope, previous_head: str, pushed_head: str, *,
    fast_forward: bool = False,
) -> None:
    key = (scope.canonical_repo.casefold(), scope.pr_number,
           previous_head.casefold(), pushed_head.casefold())
    with _verified_pushes_lock:
        _verified_pushes[key] = None
        _verified_pushes.move_to_end(key)
        while len(_verified_pushes) > _RECENT_AGENT_PUSH_LIMIT:
            _verified_pushes.popitem(last=False)
        if fast_forward:
            _ancestral_verified_pushes[(key[0], key[1], key[2])] = None
            while len(_ancestral_verified_pushes) > _RECENT_AGENT_PUSH_LIMIT:
                _ancestral_verified_pushes.popitem(last=False)


def was_verified_push(
    repository: str, pull_request: int, previous_head: str, current_head: str,
) -> bool:
    """Recognize an exact verified publication, independently of review policy."""
    key = (repository.casefold(), pull_request,
           previous_head.casefold(), current_head.casefold())
    with _verified_pushes_lock:
        return key in _verified_pushes


def was_superseded_by_own_push(repository: str, pull_request: int, head: str) -> bool:
    """Recognize only an ancestor displaced by a verified fast-forward push."""
    with _verified_pushes_lock:
        return (repository.casefold(), pull_request, head.casefold()) in _ancestral_verified_pushes

_BASE_CONFIG = (
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "diff.external=",
    "-c", "credential.helper=",
    "-c", "http.extraHeader=",
    "-c", "http.proxy=",
    "-c", "http.followRedirects=false",
    "-c", "commit.gpgSign=false",
    "-c", "merge.gpgSign=false",
    "-c", "push.pushOption=",
    "-c", "protocol.allow=never",
    "-c", "submodule.recurse=false",
)


class GitRefusal(RuntimeError):
    """A named policy refusal, distinct from a Git command failure.

    Omitted phase information deliberately fails closed to the post-execution
    verdict; known pre-execution sites pass ``execution_started=False``.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        execution_started: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.execution_started = execution_started


def _redact_git_output(text: str) -> str:
    """Redact token-shaped secrets and URL userinfo from Git diagnostics."""
    return _URL_USERINFO_RE.sub(r"\1[REDACTED]@", redact_text(text))


@dataclass(frozen=True)
class GitProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    output_limited: bool = False


class GitRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
        output_limit: int,
    ) -> GitProcessResult: ...


@dataclass(frozen=True)
class GitOperationResult:
    ok: bool
    code: str
    stdout: str = ""
    stderr: str = ""


def _git_failure_detail(result: GitProcessResult) -> str:
    """Return all scrubbed Git diagnostics, or an explicit silent-failure reason."""
    detail = "\n".join(
        output.strip() for output in (result.stderr, result.stdout) if output.strip()
    )
    return detail or f"Git exited with status {result.returncode} without diagnostic output"


@dataclass(frozen=True)
class GitFetch:
    """Fetch only the current observed head and fixed base bound into the scope.

    The observed head advances only after verified own-publication; it is not
    an immutable snapshot for the lifetime of the turn.
    """


@dataclass(frozen=True)
class GitStatus:
    include_untracked: bool = True


@dataclass(frozen=True)
class GitDiff:
    mode: Literal["working", "staged", "base"] = "working"
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class GitUnmerged:
    """Inspect the unmerged index, returning Git's NUL-delimited records."""


@dataclass(frozen=True)
class GitStage:
    paths: tuple[str, ...]


@dataclass(frozen=True)
class GitCommit:
    paths: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class GitMerge:
    """Merge the server-bound observed base commit."""


@dataclass(frozen=True)
class GitMergeAbort:
    """Abort an in-progress merge; never inferred from GitMerge."""


@dataclass(frozen=True)
class GitRebase:
    """Rebase onto the server-bound observed base commit."""

    base_property: str = ""
    base_verification: str = ""
    head_property: str = ""
    head_verification: str = ""


@dataclass(frozen=True)
class GitRebaseAbort:
    """Abort an in-progress rebase; never inferred from GitRebase."""


@dataclass(frozen=True)
class GitRevert:
    commit: str


@dataclass(frozen=True)
class GitRevertAbort:
    """Abort an in-progress revert; separately selected and authorized."""


@dataclass(frozen=True)
class GitPush:
    """Push HEAD to the single destination ref bound into the scope."""


GitOperation: TypeAlias = (
    GitFetch | GitStatus | GitDiff | GitUnmerged | GitStage | GitCommit
    | GitMerge | GitMergeAbort | GitRebase | GitRebaseAbort
    | GitRevert | GitRevertAbort | GitPush
)


def _sanitized_git_env() -> dict[str, str]:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
    }
    return env


def hardened_git_command(
    root: Path,
    arguments: tuple[str, ...],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    output_limit: int = _DEFAULT_OUTPUT_BYTES,
    runner: GitRunner | None = None,
) -> GitProcessResult:
    """Run a local Git read with the repository tools' hardened configuration."""
    argv = (
        str(_DEFAULT_GIT), "-C", str(root), *_BASE_CONFIG,
        "--no-pager", "--no-optional-locks", *arguments,
    )
    run = runner or _bounded_subprocess_runner
    return run(
        argv,
        env=_sanitized_git_env(),
        timeout=timeout,
        output_limit=output_limit,
    )


def _bounded_subprocess_runner(
    argv: tuple[str, ...],
    *,
    env: dict[str, str],
    timeout: float,
    output_limit: int,
) -> GitProcessResult:
    """Run one argv without a shell, bounding wall time and captured bytes."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        close_fds=True,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    timed_out = False
    output_limited = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _events in selector.select(min(remaining, 0.1)):
                stream = key.fileobj
                chunk = os.read(stream.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(stream)
                    continue
                target = streams[stream]
                room = output_limit - len(target)
                target.extend(chunk[:max(room, 0)])
                if len(chunk) > room:
                    output_limited = True
                    break
            if output_limited:
                break
        if timed_out or output_limited:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # A departed or no-longer-owned group must not mask the result.
                pass
        returncode = process.wait(timeout=1)
    finally:
        selector.close()
        for stream in streams:
            stream.close()
    stdout = bytes(streams[process.stdout]).decode("utf-8", "replace")
    stderr = bytes(streams[process.stderr]).decode("utf-8", "replace")
    return GitProcessResult(returncode, stdout, stderr, timed_out, output_limited)


def retained_factory_subprocess_runner(
    argv: tuple[str, ...],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    output_limit: int = _DEFAULT_OUTPUT_BYTES,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one retained-tree argv through the checkout owner's control path."""
    from .worklink.worker_client import run_factory_control

    try:
        if "clone" in argv and "--" in argv:
            separator = argv.index("--")
            checkout = Path(argv[separator + 1])
        else:
            checkout_index = argv.index("-C") + 1
            checkout = Path(argv[checkout_index])
    except (ValueError, IndexError) as exc:
        raise ValueError("hardened Git argv has no checkout") from exc
    return run_factory_control(
        checkout,
        argv,
        env=env or _sanitized_git_env(),
        timeout=timeout,
        output_limit=output_limit,
    )


@contextmanager
def retained_factory_snapshot_bundle(source: Path) -> Iterator[Path]:
    """Export retained history owner-side and remove the handoff owner-side."""
    from .worklink.worker_client import run_factory_control

    artifact = source / f".mimir-repo-test-{uuid.uuid4().hex}.bundle"
    try:
        create = retained_factory_subprocess_runner((
            "git", "-C", str(source), *_BASE_CONFIG,
            "bundle", "create", str(artifact), "--all",
        ))
        if create.returncode != 0:
            raise OSError("retained snapshot bundle creation failed")
        yield artifact
    finally:
        removed = run_factory_control(
            source,
            ("/bin/rm", "-f", "--", str(artifact)),
            env=_sanitized_git_env(),
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            output_limit=_DEFAULT_OUTPUT_BYTES,
        )
        if removed.returncode != 0:
            raise OSError("retained snapshot bundle cleanup failed")


def retained_factory_git_runner(
    argv: tuple[str, ...],
    *,
    env: dict[str, str],
    timeout: float,
    output_limit: int,
) -> GitProcessResult:
    """Adapt owner-side retained execution to the repository tool result type."""
    result = retained_factory_subprocess_runner(
        argv, env=env, timeout=timeout, output_limit=output_limit,
    )
    return GitProcessResult(
        result.returncode,
        result.stdout.decode("utf-8", "replace"),
        result.stderr.decode("utf-8", "replace"),
    )


def _validate_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        not path
        or "\x00" in path
        or path.startswith(("-", ":"))
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or "\\" in path
    ):
        raise GitRefusal("invalid_path", "Git paths must be literal repository-relative paths")
    return f":(literal){path}"


def _validated_paths(paths: tuple[str, ...], *, required: bool) -> tuple[str, ...]:
    if not isinstance(paths, tuple):
        raise GitRefusal("invalid_shape", "Git paths must be a tuple")
    if required and not paths:
        raise GitRefusal("explicit_paths_required", "at least one explicit path is required")
    if len(paths) > _MAX_PATHS or len(set(paths)) != len(paths):
        raise GitRefusal("invalid_paths", "Git paths are duplicated or exceed the server limit")
    for path in paths:
        _validate_path(path)
    return paths


def _out_of_scope_paths_message(paths: set[str]) -> str:
    """Bound index path names and escape controls before adding them to a refusal."""
    if not paths:
        return "nothing staged"
    # These names come from the index, not from a requested Git operation.
    # Render them as data without changing the dirty_out_of_scope refusal code.
    shown = sorted(paths)[:10]
    names = ", ".join(repr(path) for path in shown)
    more = len(paths) - len(shown)
    return f"{names} … and {more} more" if more else names


class RepoGitTools:
    """Execute closed Git operations against one active repository scope."""

    def __init__(
        self,
        review_state: RepoReviewState | None = None,
        *,
        retained_scope: RetainedFactoryScope | None = None,
        git_executable: Path = _DEFAULT_GIT,
        runner: GitRunner = _bounded_subprocess_runner,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        output_limit: int = _DEFAULT_OUTPUT_BYTES,
        enforce: bool = True,
        auth_context: Any = None,
    ) -> None:
        if (review_state is None) == (retained_scope is None):
            raise ValueError("exactly one repository scope is required")
        if timeout <= 0 or output_limit <= 0:
            raise ValueError("Git timeout and output limit must be positive")
        if not git_executable.is_absolute():
            raise ValueError("Git executable pin must be absolute")
        try:
            executable = git_executable.resolve(strict=True)
        except OSError as exc:
            raise ValueError("Git executable pin is missing") from exc
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("Git executable pin is not executable")
        self._state = review_state
        self._scope = (
            review_state.action_scope if review_state is not None else retained_scope
        )
        self._retained = retained_scope is not None
        self._git = str(executable)
        self._runner = runner
        self._timeout = timeout
        self._output_limit = output_limit
        self._enforce = enforce
        self._auth_context = auth_context
        self._env = _sanitized_git_env()
        self._execution_started = False
        self._root = self._validate_lease()
        self._validate_scope()
        self._expected_head = (
            review_state.git_expected_head or self._scope.observed_head_sha
        ).lower() if review_state is not None else ""

    @property
    def environment(self) -> dict[str, str]:
        """Expose a copy for audit/tests; mutations cannot affect execution."""
        return self._env.copy()

    @property
    def execution_started(self) -> bool:
        """Return whether this instance attempted a Git subprocess."""
        return self._execution_started

    def _validate_scope(self) -> None:
        if self._retained:
            return
        assert isinstance(self._scope, RepoPRActionScope)
        if not _SHA_RE.fullmatch(self._scope.observed_head_sha):
            raise GitRefusal("invalid_scope", "scope head is not a full commit id", execution_started=False)
        if not _SHA_RE.fullmatch(self._scope.observed_base_sha):
            raise GitRefusal("invalid_scope", "scope base is not a full commit id", execution_started=False)
        if not _REF_RE.fullmatch(self._scope.destination_ref) or any(
            token in self._scope.destination_ref for token in ("..", "//", "@{", "\\")
        ):
            raise GitRefusal("invalid_scope", "scope destination is not a branch ref", execution_started=False)
        base_ref = f"refs/heads/{self._scope.base_ref}"
        if not _REF_RE.fullmatch(base_ref) or any(
            token in base_ref for token in ("..", "//", "@{", "\\")
        ):
            raise GitRefusal("invalid_scope", "scope base is not a branch ref", execution_started=False)
        if self._scope.head_remote != "origin" or not self._scope.canonical_origin:
            raise GitRefusal("invalid_scope", "scope remote is not server-bound origin", execution_started=False)

    def _validate_lease(self) -> Path:
        if self._retained:
            assert isinstance(self._scope, RetainedFactoryScope)
            from .worklink.worker_client import factory_checkout_for_path

            path = Path(self._scope.sandbox)
            binding = factory_checkout_for_path(path)
            try:
                root = path.resolve(strict=True)
            except OSError as exc:
                raise GitRefusal("invalid_checkout", "retained checkout is unavailable", execution_started=False) from exc
            if (
                root != path
                or not root.is_dir()
                or root.parent.name != ".factory-sandboxes"
                or root.name != self._scope.run_id
                or binding is None
                or binding[1:] != (self._scope.issue_id, self._scope.attempt)
            ):
                raise GitRefusal("invalid_checkout", "retained checkout identity is invalid", execution_started=False)
            return root
        assert self._state is not None
        assert isinstance(self._scope, RepoPRActionScope)
        lease = self._state.checkout_lease
        if (
            lease is None
            or lease.scope_id != self._scope.scope_id
            or lease.owner != self._scope.principal
            or not lease.is_active
        ):
            raise GitRefusal("inactive_checkout", "an active matching checkout lease is required", execution_started=False)
        path = Path(lease.path)
        if path.is_symlink():
            raise GitRefusal("invalid_checkout", "checkout lease may not be a symlink", execution_started=False)
        try:
            root = path.resolve(strict=True)
            lease_root = Path(lease.lease_root).resolve(strict=True)
        except OSError as exc:
            raise GitRefusal("invalid_checkout", "checkout lease path is unavailable", execution_started=False) from exc
        if root.parent != lease_root or root == lease_root:
            raise GitRefusal("invalid_checkout", "checkout lease escapes its server root", execution_started=False)
        return root

    def _require(self, tool_name: str, *actions: RepoPRAction) -> None:
        if self._retained:
            if tool_name not in {
                "repo_status", "repo_diff", "repo_stage", "repo_commit",
            }:
                raise GitRefusal(
                    "scope_action_denied",
                    "retained factory scope does not grant this repository operation",
                    execution_started=False,
                )
            return
        assert isinstance(self._scope, RepoPRActionScope)
        decision = authorize_repo_pr_tool(
            tool_name,
            self._scope,
            service_principal=None,
            enforce=self._enforce,
            flow_direction=ToolFlowDirection.UNKNOWN,
            required_actions=tuple(action.value for action in actions),
        )
        if not decision.allowed:
            raise GitRefusal(
                "scope_action_denied",
                decision.refusal_detail or "scope does not grant the required action",
            )

    def _raw(
        self,
        arguments: tuple[str, ...],
        *,
        network: bool = False,
        network_remote: str | None = None,
        env: dict[str, str] | None = None,
        sensitive_values: tuple[str, ...] = (),
    ) -> GitProcessResult:
        transport: tuple[str, ...] = ()
        if network:
            origin = network_remote or self._scope.canonical_origin
            if origin.startswith(("https://", "http://")):
                transport = (
                    "-c", "protocol.https.allow=always",
                    "-c", "protocol.http.allow=always",
                )
            elif origin.startswith(("ssh://", "git@")):
                transport = ("-c", "protocol.ssh.allow=always", "-c", "core.sshCommand=/usr/bin/ssh")
            elif origin.startswith("file://") or Path(origin).is_absolute():
                transport = ("-c", "protocol.file.allow=always")
            else:
                raise GitRefusal("unsupported_transport", "scope origin transport is not allowed")
        argv = (
            self._git, "-C", str(self._root), *_BASE_CONFIG, *transport,
            "--no-pager", "--no-optional-locks", *arguments,
        )
        try:
            child_env = self._env.copy()
            if env:
                child_env.update(env)
            self._execution_started = True
            result = self._runner(
                argv, env=child_env, timeout=self._timeout,
                output_limit=self._output_limit,
            )
            for value in sorted(filter(None, sensitive_values), key=len, reverse=True):
                result = replace(
                    result,
                    stdout=result.stdout.replace(value, "[REDACTED]"),
                    stderr=result.stderr.replace(value, "[REDACTED]"),
                )
            return replace(
                result,
                stdout=_redact_git_output(result.stdout),
                stderr=_redact_git_output(result.stderr),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitRefusal(
                "git_failed",
                "pinned Git execution failed",
                execution_started=True,
            ) from exc

    def _checked(
        self,
        arguments: tuple[str, ...],
        *,
        network: bool = False,
        network_remote: str | None = None,
        env: dict[str, str] | None = None,
        sensitive_values: tuple[str, ...] = (),
    ) -> GitProcessResult:
        result = self._raw(
            arguments,
            network=network,
            network_remote=network_remote,
            env=env,
            sensitive_values=sensitive_values,
        )
        if result.timed_out:
            raise GitRefusal("timeout", "Git operation exceeded its time limit")
        if result.output_limited:
            raise GitRefusal("output_limit", "Git operation exceeded its output limit")
        if result.returncode != 0:
            raise GitRefusal("git_failed", _git_failure_detail(result))
        return result

    def _config_overrides(self) -> tuple[str, ...]:
        result = self._raw((
            "config", "--local", "--null", "--name-only", "--get-regexp",
            r"^(filter\..*\.(clean|smudge|process)|merge\..*\.driver|url\..*\.(insteadOf|pushInsteadOf))$",
        ))
        if result.timed_out:
            raise GitRefusal("timeout", "Git config inspection exceeded its time limit")
        if result.output_limited:
            raise GitRefusal("output_limit", "Git config inspection exceeded its output limit")
        if result.returncode == 1 and not result.stdout:
            return ()
        if result.returncode != 0:
            raise GitRefusal("unsafe_repo_config", "repository configuration could not be inspected safely")
        names = result.stdout.split("\x00")
        if names[-1:] == [""]:
            names.pop()
        overrides: list[str] = []
        for name in names:
            if _FILTER_KEY_RE.fullmatch(name) or _MERGE_KEY_RE.fullmatch(name):
                overrides.extend(("-c", f"{name}="))
            elif name:
                # URL rewrites can redirect a server-bound remote and have no
                # safe generic neutral value.
                raise GitRefusal("unsafe_repo_config", "repository URL rewrite configuration is refused")
        return tuple(overrides)

    def _command(
        self,
        arguments: tuple[str, ...],
        *,
        network: bool = False,
        network_remote: str | None = None,
        diff: bool = False,
        identity: bool = False,
        overrides: tuple[str, ...] | None = None,
        env: dict[str, str] | None = None,
        sensitive_values: tuple[str, ...] = (),
    ) -> GitProcessResult:
        local_overrides = self._config_overrides() if overrides is None else overrides
        prefix = local_overrides
        if identity:
            prefix += (
                "-c", f"user.name={DEFAULT_USER_NAME}",
                "-c", f"user.email={DEFAULT_USER_EMAIL}",
            )
        suffix = ("--no-ext-diff", "--no-textconv") if diff else ()
        return self._checked(
            (*prefix, *arguments, *suffix),
            network=network,
            network_remote=network_remote,
            env=env,
            sensitive_values=sensitive_values,
        )

    def _has_in_progress_merge_or_rebase(self) -> bool:
        git_dir = Path(
            self._command(("rev-parse", "--absolute-git-dir")).stdout.strip()
        )
        return any(
            (git_dir / marker).exists()
            for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD")
        )

    def _assert_checkout_identity(self, *, allow_in_progress: bool = False) -> None:
        top = self._command(("rev-parse", "--show-toplevel")).stdout.strip()
        if Path(top).resolve(strict=False) != self._root:
            raise GitRefusal("cross_pr_checkout", "checkout identity no longer matches the scope")
        if self._retained:
            assert isinstance(self._scope, RetainedFactoryScope)
            branch = self._command(("symbolic-ref", "--quiet", "--short", "HEAD")).stdout.strip()
            if branch != self._scope.branch:
                raise GitRefusal("cross_pr_checkout", "checkout identity no longer matches the retained scope")
            return
        assert isinstance(self._scope, RepoPRActionScope)
        origin = self._command(("remote", "get-url", "origin")).stdout.strip()
        if origin != self._scope.canonical_origin:
            raise GitRefusal("cross_pr_checkout", "checkout identity no longer matches the PR scope")
        if allow_in_progress and self._has_in_progress_merge_or_rebase():
            return
        branch = self._command(("symbolic-ref", "--quiet", "--short", "HEAD")).stdout.strip()
        head = self._command(("rev-parse", "--verify", "HEAD")).stdout.strip().lower()
        if branch != self._scope.head_ref or head != self._expected_head:
            raise GitRefusal("cross_pr_checkout", "checkout identity no longer matches the PR scope")

    def _refresh_expected_head(self) -> None:
        self._expected_head = self._command(("rev-parse", "--verify", "HEAD")).stdout.strip().lower()
        if self._state is not None:
            self._state.record_git_head(self._scope.scope_id, self._expected_head)

    def _record_local_head(
        self, previous: str, *, verified_base: str | None = None,
        rewrite: bool = False,
    ) -> None:
        """Persist the actual new commits and the live turn's integrity verdict."""
        if self._state is None or self._expected_head == previous:
            return
        from mimir.access_control import _turn_has_untrusted_active_ingest

        lease = self._state.checkout_lease
        metadata_path = lease.path / _METADATA
        try:
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or any(raw.get(key) != getattr(lease, key) for key in (
                "scope_id", "canonical_repo", "pr_number", "head_sha", "base_sha",
            )):
                raise ValueError("lease metadata identity mismatch")
            lineage = _recorded_lineage(raw)
            # Older leases are resumable but their unrecorded commits cannot be
            # promoted into clean history by a later operation.
            if lineage is None:
                lineage = {}
            commits = self._command((
                "rev-list", "--max-count=501", self._expected_head, "--not", previous,
                *((verified_base or lease.base_sha,) if verified_base or lease.base_sha else ()), "--",
            )).stdout.splitlines()
            if len(commits) > 500 or (not commits and self._expected_head != (verified_base or lease.base_sha)):
                raise ValueError("local commit range is missing or oversized")
            clean = self._auth_context is not None and not _turn_has_untrusted_active_ingest(
                self._auth_context, None,
            )
            if rewrite and previous != self._scope.observed_head_sha.lower():
                prior = self._command((
                    "rev-list", "--max-count=501", previous, "--not",
                    self._scope.observed_head_sha, lease.base_sha, "--",
                )).stdout.splitlines()
                clean = clean and bool(prior) and len(prior) <= 500 and all(
                    lineage.get(commit) is True for commit in prior
                )
            for commit in commits:
                lineage[commit] = lineage.get(commit, True) and clean
            advanced = replace(
                lease, lineage=lineage, head_sha=self._expected_head,
                base_sha=verified_base or lease.base_sha,
                verified_base_sha=verified_base or lease.verified_base_sha,
            )
            staging = metadata_path.with_suffix(".tmp")
            try:
                staging.write_text(json.dumps(_metadata(advanced), sort_keys=True) + "\n", encoding="utf-8")
                os.replace(staging, metadata_path)
            finally:
                staging.unlink(missing_ok=True)
            object.__setattr__(lease, "lineage", lineage)
            object.__setattr__(lease, "head_sha", self._expected_head)
            if verified_base is not None:
                object.__setattr__(lease, "base_sha", verified_base)
                object.__setattr__(lease, "verified_base_sha", verified_base)
        except (OSError, ValueError, TypeError) as exc:
            raise GitRefusal("lineage_record_failed", "local commit lineage could not be recorded") from exc

    @staticmethod
    def _conflict_evidence(operation: GitRebase) -> tuple[str, str, str, str]:
        values = (
            operation.base_property,
            operation.base_verification,
            operation.head_property,
            operation.head_verification,
        )
        if any(
            not isinstance(value, str)
            or not value.strip()
            or "\x00" in value
            or len(value.encode("utf-8")) > 2_048
            for value in values
        ):
            raise GitRefusal(
                "conflict_evidence_required",
                "continuing a conflicting rebase requires non-empty base/head properties "
                "and a verification for each",
            )
        return tuple(value.strip() for value in values)  # type: ignore[return-value]

    def _stranded_work_message(self) -> str:
        if self._expected_head == self._scope.observed_head_sha.lower():
            return "the checkout lease was preserved for retry"
        return (
            f"local commit {self._expected_head} remains unpushed in preserved "
            f"checkout lease {self._root}"
        )

    def _push_remote(self) -> tuple[str, dict[str, str], tuple[str, ...]]:
        origin = self._scope.canonical_origin
        if origin.startswith("https://"):
            parsed = urlsplit(origin)
            try:
                port = parsed.port
            except ValueError:
                port = -1
            expected_paths = {
                f"/{self._scope.canonical_repo}",
                f"/{self._scope.canonical_repo}.git",
            }
            if (
                parsed.scheme != "https"
                or parsed.hostname != "github.com"
                or parsed.path not in expected_paths
                or parsed.username
                or parsed.password
                or port
                or parsed.query
                or parsed.fragment
            ):
                raise GitRefusal(
                    "push_auth_unavailable",
                    f"push credential is unavailable for the scoped origin; "
                    f"{self._stranded_work_message()}",
                )
            token = os.environ.get("GITHUB_TOKEN", "").strip()
            if not token:
                raise GitRefusal(
                    "push_auth_unavailable",
                    f"push credential is unavailable; {self._stranded_work_message()}",
                )
            from .forge import ForgeError, confirm_github_identity

            try:
                confirm_github_identity(self._scope.principal, token)
            except ForgeError as exc:
                raise GitRefusal(
                    "push_identity_unverified",
                    f"push identity could not be verified: {exc}; {self._stranded_work_message()}",
                ) from exc
            encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            authorization = f"Authorization: Basic {encoded}"
            return origin, {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{origin}.extraheader",
                "GIT_CONFIG_VALUE_0": authorization,
            }, (authorization, encoded, token)
        if origin.startswith("http://"):
            raise GitRefusal(
                "push_auth_unavailable",
                f"authenticated pushes require HTTPS; {self._stranded_work_message()}",
            )
        return origin, {}, ()

    def _unmerged_paths(self) -> set[str]:
        output = self._command(("ls-files", "--unmerged", "-z")).stdout
        paths: set[str] = set()
        for record in output.split("\x00"):
            if record:
                _metadata, separator, path = record.partition("\t")
                if not separator:
                    raise GitRefusal("invalid_git_output", "malformed unmerged index output")
                paths.add(path)
        return paths

    def is_tracked_file(self, path: Path) -> bool:
        """Return whether ``path`` is an index entry in this exact active lease."""
        root = self._validate_lease()
        if root != self._root:
            raise GitRefusal("cross_pr_checkout", "checkout lease changed during Git access")
        try:
            if not path.is_absolute() or path.is_symlink():
                return False
            path.relative_to(root)
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return False
        if not resolved.is_file():
            return False

        self._assert_checkout_identity()
        pathspec = _validate_path(relative)
        overrides = self._config_overrides()
        result = self._raw((
            *overrides, "ls-files", "--cached", "--error-unmatch", "-z", "--", pathspec,
        ))
        if result.timed_out:
            raise GitRefusal("timeout", "Git tracked-file inspection exceeded its time limit")
        if result.output_limited:
            raise GitRefusal("output_limit", "Git tracked-file inspection exceeded its output limit")
        if result.returncode == 1 and not result.stdout:
            return False
        if result.returncode != 0:
            raise GitRefusal("git_failed", "Git tracked-file inspection failed")
        return result.stdout == f"{relative}\x00"

    def validated_checkout_root(self) -> Path:
        """Revalidate the lease and Git identity immediately before non-Git use."""
        root = self._validate_lease()
        if root != self._root:
            raise GitRefusal("cross_pr_checkout", "checkout lease changed during access")
        self._assert_checkout_identity()
        return root

    def _stage(self, paths: tuple[str, ...]) -> None:
        unmerged = self._unmerged_paths()
        if unmerged and not set(paths).issubset(unmerged):
            raise GitRefusal(
                "unproven_conflict_path",
                "while conflicts exist, every staged path must be proven by the unmerged index",
            )
        pathspecs = tuple(_validate_path(path) for path in paths)
        self._command(("add", "--", *pathspecs))

    def execute(self, operation: GitOperation) -> GitOperationResult:
        """Authorize one typed operation and execute only server-built argv."""
        if self._retained and not isinstance(
            operation, (GitStatus, GitDiff, GitStage, GitCommit),
        ):
            raise GitRefusal(
                "scope_action_denied",
                "retained factory scope does not grant this repository operation",
                execution_started=False,
            )
        self._root = self._validate_lease()
        self._assert_checkout_identity(allow_in_progress=isinstance(
            operation,
            (
                GitStatus, GitDiff, GitUnmerged,
                GitStage, GitMergeAbort, GitRebase, GitRebaseAbort, GitRevertAbort,
            ),
        ))

        if isinstance(operation, GitFetch):
            self._require("repo_fetch", RepoPRAction.CHECKOUT)
            lease = self._state.checkout_lease
            for ref, expected, identity in (
                (self._scope.checkout_ref or self._scope.destination_ref,
                 self._scope.observed_head_sha, "head"),
                (f"refs/heads/{self._scope.base_ref}", lease.base_sha, "base"),
            ):
                self._command((
                    "fetch", "--no-tags", "--no-recurse-submodules",
                    self._scope.canonical_origin, ref,
                ), network=True)
                actual = self._command(("rev-parse", "--verify", "FETCH_HEAD^{commit}")).stdout.strip()
                if actual.lower() != expected.lower():
                    if identity == "head":
                        raise GitRefusal(
                            "stale_scope",
                            f"PR head advanced: scoped head {expected.lower()} is stale; "
                            f"fetched head is {actual.lower()}",
                        )
                    ancestry = self._raw((
                        "merge-base", "--is-ancestor", expected.lower(), actual.lower(),
                    ))
                    if ancestry.returncode == 0:
                        raise GitRefusal(
                            "base_advanced",
                            f"PR base advanced during remediation: checked-out base "
                            f"{expected.lower()} is stale; fetched base is {actual.lower()}; "
                            "restart checkout before rebasing",
                        )
                    raise GitRefusal(
                        "base_history_rewritten",
                        f"PR base history was rewritten during remediation: checked-out base "
                        f"{expected.lower()} is stale; fetched base is {actual.lower()}",
                    )
            return GitOperationResult(True, "ok")

        if isinstance(operation, GitStatus):
            self._require("repo_status", RepoPRAction.INSPECT)
            untracked = "all" if operation.include_untracked else "no"
            result = self._command(("status", "--porcelain=v2", "-z", f"--untracked-files={untracked}"))
        elif isinstance(operation, GitDiff):
            self._require("repo_diff", RepoPRAction.INSPECT)
            paths = _validated_paths(operation.paths, required=False)
            if operation.mode not in {"working", "staged", "base"}:
                raise GitRefusal("invalid_shape", "unsupported diff mode")
            revisions: tuple[str, ...] = ()
            if operation.mode == "staged":
                revisions = ("--cached",)
            elif operation.mode == "base":
                if self._retained:
                    raise GitRefusal(
                        "invalid_shape",
                        "base diff is unavailable for a retained factory scope",
                        execution_started=False,
                    )
                assert self._state is not None
                revisions = (f"{self._state.checkout_lease.base_sha}...HEAD",)
            pathspecs = tuple(_validate_path(path) for path in paths)
            separator = ("--", *pathspecs) if pathspecs else ()
            result = self._command((
                "diff", "--no-color", "--no-ext-diff", "--no-textconv",
                *revisions, *separator,
            ))
        elif isinstance(operation, GitUnmerged):
            self._require("repo_unmerged", RepoPRAction.INSPECT)
            result = self._command(("ls-files", "--unmerged", "-z"))
        elif isinstance(operation, GitStage):
            self._require("repo_stage", RepoPRAction.WRITE)
            paths = _validated_paths(operation.paths, required=True)
            self._stage(paths)
            result = GitProcessResult(0)
        elif isinstance(operation, GitCommit):
            self._require("repo_commit", RepoPRAction.WRITE, RepoPRAction.COMMIT)
            paths = _validated_paths(operation.paths, required=True)
            if (
                not isinstance(operation.message, str)
                or not operation.message.strip()
                or "\x00" in operation.message
                or len(operation.message.encode("utf-8")) > _MAX_MESSAGE_BYTES
            ):
                raise GitRefusal("invalid_message", "commit message is empty or exceeds the server limit")
            staged_before = set(filter(None, self._command((
                "diff", "--cached", "--name-only", "-z", "--no-ext-diff", "--no-textconv",
            )).stdout.split("\x00")))
            if not staged_before.issubset(paths):
                raise GitRefusal(
                    "dirty_out_of_scope",
                    "the index contains paths outside this commit: "
                    f"{_out_of_scope_paths_message(staged_before - set(paths))}; "
                    "include them in `paths`, or unstage them",
                )
            self._stage(paths)
            staged_after = set(filter(None, self._command((
                "diff", "--cached", "--name-only", "-z", "--no-ext-diff", "--no-textconv",
            )).stdout.split("\x00")))
            if not staged_after or not staged_after.issubset(paths):
                raise GitRefusal(
                    "dirty_out_of_scope",
                    "staged paths do not match the explicit commit scope: "
                    f"{_out_of_scope_paths_message(staged_after - set(paths))}; "
                    "include them in `paths`, or unstage them",
                )
            previous = self._expected_head
            result = self._command(("commit", "-m", operation.message), identity=True)
            self._refresh_expected_head()
            self._record_local_head(previous)
        elif isinstance(operation, GitMerge):
            self._require("repo_merge", RepoPRAction.WRITE, RepoPRAction.COMMIT)
            previous = self._expected_head
            try:
                result = self._command(
                    ("merge", "--no-edit", "--", self._state.checkout_lease.base_sha),
                    identity=True,
                )
                # A recovered candidate may already contain base but not the
                # published PR head. Merge that immutable head too: recovery
                # must offer a fast-forward path without granting rewrite authority.
                self._refresh_expected_head()
                self._record_local_head(previous)
                previous = self._expected_head
                publication_merge = self._command(
                    ("merge", "--no-edit", "--", self._scope.observed_head_sha),
                    identity=True,
                )
                result = GitProcessResult(
                    0, result.stdout + publication_merge.stdout,
                    result.stderr + publication_merge.stderr,
                )
            except GitRefusal as exc:
                if exc.code != "git_failed":
                    raise
                unmerged = self._unmerged_paths()
                if not unmerged:
                    raise
                paths = ", ".join(repr(path) for path in sorted(unmerged))
                raise GitRefusal(
                    "merge_conflict",
                    f"merge conflict in unmerged path(s): {paths}; inspect the unmerged "
                    f"index with repo_unmerged, or abort the merge with repo_merge_abort; "
                    f"Git output: {exc}",
                ) from exc
            self._refresh_expected_head()
            self._record_local_head(previous)
        elif isinstance(operation, GitMergeAbort):
            self._require("repo_merge_abort", RepoPRAction.WRITE)
            result = self._command(("merge", "--abort"))
        elif isinstance(operation, GitRebase):
            self._require("repo_rebase", RepoPRAction.WRITE, RepoPRAction.COMMIT)
            previous = self._expected_head
            if not _history_rewrite_push_permitted(self._scope):
                raise GitRefusal(
                    "rebase_in_lease_refused",
                    "rebase inside a PR checkout lease is refused because this scope "
                    "cannot publish rewritten history; use repo_merge to merge the base "
                    "and scoped published PR head, resolve any conflicts, then repo_push",
                )
            continuing = self._has_in_progress_merge_or_rebase()
            verified_base = None
            if not continuing:
                # A scope's base can move while the turn runs. Only the freshly
                # fetched protected destination tip may become the rebase base.
                if f"refs/heads/{self._scope.base_ref}" not in _PROTECTED_BRANCH_REFS:
                    raise GitRefusal("unverified_base", "rebase requires a protected base")
                self._command((
                    "fetch", "--no-tags", "--no-recurse-submodules",
                    self._scope.canonical_origin, f"refs/heads/{self._scope.base_ref}",
                ), network=True)
                verified_base = self._command((
                    "rev-parse", "--verify", "FETCH_HEAD^{commit}",
                )).stdout.strip().lower()
                ancestry = self._raw((
                    "merge-base", "--is-ancestor", self._scope.observed_base_sha,
                    verified_base,
                ))
                if ancestry.returncode != 0 or ancestry.timed_out or ancestry.output_limited:
                    raise GitRefusal("unverified_base", "protected base does not descend from scoped base")
            if continuing:
                if self._scope.event_type != "pr_mergeability_conflicting":
                    raise GitRefusal("rebase_continue_refused", "this scope may not continue a rebase")
                base_property, base_verification, head_property, head_verification = (
                    self._conflict_evidence(operation)
                )
                result = self._command(
                    ("rebase", "--continue"), identity=True,
                    env={"GIT_EDITOR": "/usr/bin/true"},
                )
                message = self._command(("log", "-1", "--format=%B")).stdout.rstrip()
                evidence = (
                    f"{message}\n\nConflict resolution evidence:\n"
                    f"Base property: {base_property}\n"
                    f"Base verification: {base_verification}\n"
                    f"Head property: {head_property}\n"
                    f"Head verification: {head_verification}"
                )
                self._command(("commit", "--amend", "-m", evidence), identity=True)
            else:
                result = self._command(
                    ("rebase", "--", verified_base), identity=True,
                )
            self._refresh_expected_head()
            self._record_local_head(previous, verified_base=verified_base, rewrite=True)
            if continuing:
                self._state.record_conflict_evidence(
                    self._scope.scope_id, self._expected_head,
                )
        elif isinstance(operation, GitRebaseAbort):
            self._require("repo_rebase_abort", RepoPRAction.WRITE)
            result = self._command(("rebase", "--abort"))
        elif isinstance(operation, GitRevert):
            self._require("repo_revert", RepoPRAction.WRITE, RepoPRAction.COMMIT)
            previous = self._expected_head
            if not _SHA_RE.fullmatch(operation.commit):
                raise GitRefusal("invalid_commit", "revert requires one full commit id")
            allowed = set(filter(None, self._command((
                "rev-list", "HEAD", f"^{self._state.checkout_lease.base_sha}", "--",
            )).stdout.splitlines()))
            if operation.commit.lower() not in {commit.lower() for commit in allowed}:
                raise GitRefusal("invalid_revert_ancestry", "revert commit is outside head ^base")
            result = self._command(("revert", "--no-edit", operation.commit), identity=True)
            self._refresh_expected_head()
            self._record_local_head(previous)
        elif isinstance(operation, GitRevertAbort):
            self._require("repo_revert_abort", RepoPRAction.WRITE)
            result = self._command(("revert", "--abort"))
        elif isinstance(operation, GitPush):
            self._require("repo_push", RepoPRAction.PUSH)
            if self._scope.event_type == "pr_mergeability_conflicting":
                if self._state.conflict_evidence_head != self._expected_head:
                    raise GitRefusal(
                        "conflict_evidence_required",
                        "conflict push refused until the resulting commit names the preserved "
                        "base/head properties and verification for each",
                    )
                if self._state.full_tested_head != self._expected_head:
                    raise GitRefusal(
                        "full_test_required",
                        "conflict push refused until the configured full suite passes against HEAD",
                    )
            overrides = self._config_overrides()
            ancestry = self._raw((
                "merge-base", "--is-ancestor", self._scope.observed_head_sha,
                self._expected_head,
            ))
            rewritten_history = ancestry.returncode != 0
            if rewritten_history and not _history_rewrite_push_permitted(self._scope):
                raise GitRefusal(
                    "force_push_refused",
                    "push would not be a fast-forward; use repo_merge to merge the base "
                    "and scoped published PR head, resolve any conflicts, then repo_push; "
                    "retained work can also be resumed by a later turn for this PR",
                )
            try:
                push_remote, auth_env, sensitive_values = self._push_remote()
                remote = self._command((
                    "ls-remote", "--heads", push_remote,
                    self._scope.destination_ref,
                ), network=True, network_remote=push_remote, overrides=overrides,
                    env=auth_env, sensitive_values=sensitive_values).stdout
                records = [line.split("\t", 1) for line in remote.splitlines() if line]
                if (
                    len(records) != 1
                    or len(records[0]) != 2
                    or records[0][0].lower() != self._scope.observed_head_sha.lower()
                    or records[0][1] != self._scope.destination_ref
                ):
                    raise GitRefusal(
                        "stale_scope", self._stranded_work_message(),
                    )
                push_args = ["push", "--porcelain"]
                if rewritten_history:
                    push_args.append(
                        f"--force-with-lease={self._scope.destination_ref}:"
                        f"{self._scope.observed_head_sha}"
                    )
                push_args.extend((push_remote, f"HEAD:{self._scope.destination_ref}"))
                result = self._command(tuple(push_args), network=True,
                    network_remote=push_remote, overrides=overrides,
                    env=auth_env, sensitive_values=sensitive_values)
                self._command((
                    "fetch", "--no-tags", "--no-recurse-submodules", push_remote,
                    self._scope.destination_ref,
                ), network=True, network_remote=push_remote, overrides=overrides,
                    env=auth_env, sensitive_values=sensitive_values)
                observed = self._command((
                    "rev-parse", "--verify", "FETCH_HEAD^{commit}",
                ), overrides=overrides).stdout.strip().lower()
                reachability = self._raw((
                    *overrides, "merge-base", "--is-ancestor",
                    self._expected_head, observed,
                ))
                if reachability.timed_out:
                    raise GitRefusal("timeout", "push verification exceeded its time limit")
                if reachability.output_limited:
                    raise GitRefusal("output_limit", "push verification exceeded its output limit")
                if reachability.returncode == 1:
                    raise GitRefusal(
                        "push_not_applied",
                        f"push did not update {self._scope.destination_ref} to contain expected "
                        f"commit {self._expected_head}; observed remote commit {observed}; "
                        f"{self._stranded_work_message()}",
                    )
                if reachability.returncode != 0:
                    raise GitRefusal(
                        "git_failed",
                        reachability.stderr.strip() or "push reachability verification failed",
                    )
                self._command(("update-ref", PUBLISHED_HEAD_REF, observed))
                previous_head = self._scope.observed_head_sha
                _record_agent_push(self._scope, observed)
                # Keep the turn identity fixed; only our own verified publication
                # advances its observation, never a concurrent writer's commit.
                lease = self._state.checkout_lease
                advanced_lease = replace(lease, head_sha=self._expected_head)
                metadata_path = lease.path / _METADATA
                staging = metadata_path.with_suffix(".tmp")
                try:
                    staging.write_text(
                        json.dumps(_metadata(advanced_lease), sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    os.replace(staging, metadata_path)
                finally:
                    staging.unlink(missing_ok=True)
                # Deliberately bypass frozen dataclasses in place: callers retain
                # these lease/scope objects and must observe the head advance;
                # replacing them with frozen copies would leave callers stale.
                # Only our verified own-publication reaches this point, and the
                # persisted lease metadata is atomically replaced above before
                # either in-memory observation advances. All other scope fields
                # stay fixed; do not cache these objects as immutable head snapshots.
                object.__setattr__(lease, "head_sha", self._expected_head)
                object.__setattr__(self._scope, "observed_head_sha", self._expected_head)
                _record_verified_push(
                    self._scope, previous_head, self._expected_head,
                    fast_forward=not rewritten_history,
                )
            except GitRefusal as exc:
                if exc.code == "git_failed":
                    raise GitRefusal(
                        "git_failed", f"push failed: {exc}; {self._stranded_work_message()}",
                    ) from exc
                raise
        else:
            raise GitRefusal("invalid_shape", "unsupported typed Git operation")
        return GitOperationResult(True, "ok", result.stdout, result.stderr)


# Contained project tests
import ast
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
from collections.abc import Awaitable, Callable
import uuid

from .contained_checkout import ContainedCheckout, create_repo_test_checkout
from .contained_execution import (
    CollectedExecutionResult,
    SensitiveMaterialScrubber,
    base_worker_environment,
    execute_contained,
)
from mimir.contained_snapshot import (
    ContainedSnapshotError,
    SnapshotCredentialsRefused,
    SnapshotEmbeddedRepository,
)
from mimir.event_logger import safe_log_event
from mimir.models import RepoPRAction, RepoReviewState, RetainedFactoryScope
from mimir.redaction import redact_text


from importlib.util import find_spec

if find_spec("mimir_cultivate.worklink.backends.registry") is None:
    # The Worklink registry is installed by a later importer-safe slice.
    from mimir.worklink.backends.registry import WorklinkConfig
else:
    from .worklink.backends.registry import WorklinkConfig
from .worklink.identities import get_identities
from .worklink.worker_client import StaleWorkerExecutorError


#: Wall-clock ceiling for one project-test invocation.
#:
#: 300s could not run this repository's own suite. Every measured full-suite run
#: sits between 492s and 597s, so ``repo_test`` timed out on the gate command
#: every time -- not intermittently, arithmetically. The agent reviewed pull
#: requests reporting "the bounded runner timed out with empty output" and fell
#: back to whatever counts the author supplied.
#:
#: 900s was then chosen from those same measurements, and that was the wrong
#: basis: every one of them was taken on an idle machine, while the runner's
#: normal condition is a busy one. The agent and the worklink builds share a
#: host, so the suite competes with whatever the factory is doing. Measured at
#: 1023s with two concurrent builds running -- it passed, load stretches the
#: run rather than breaking it, but 1023s is already past 900s, so the bound
#: reintroduced the same arithmetic timeout under the load the runner actually
#: sees.
#:
#: 1800s is sized against the loaded case instead, and matches the upper bound
#: CI already allows its own pytest jobs. A generous bound fails safe here: too
#: high merely delays a report that something is wrong, while too low silently
#: denies the agent any full-suite evidence of its own, which is what happened
#: through nine review rounds on #1594.
_TIMEOUT_SECONDS = 1800.0
# A full suite includes wheel builds and dependency setup, not just one command:
# allow 16x shell_exec's 1 MiB per stream, matching Worklink's 16 MiB stderr
# budget while staying at one quarter of its 64 MiB stdout budget. Overflow
# still stops execution; the smaller returned excerpts remain unchanged.
_CAPTURE_BYTES = 16 * 1024 * 1024
_RETURN_STDOUT_CHARS = 8_000
_RETURN_STDERR_CHARS = 4_000
_LIVE_OUTPUT_ROOT = Path("state/worklink/transcripts")
_MAX_SELECTORS = 32
_MAX_SELECTOR_LENGTH = 256
_MAX_SELECTOR_BYTES = 4_096
_SELECTOR_PATTERN = re.compile(r"[A-Za-z0-9._/,:+=-]+", re.ASCII)
_PERMISSION_PATH_PATTERN = re.compile(
    rb"(?:failed to open file |failed to access )?[`'](?P<path>/[^`'\r\n]+)[`']"
    rb"[^\r\n]*(?:Permission denied|os error 13|EACCES)",
    re.IGNORECASE,
)
_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_PYTEST_NODE_ID = re.compile(r"[A-Za-z0-9_./:\[\]=,+-]{1,256}", re.ASCII)
_PYTEST_SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
_PYTEST_SECTION_END = re.compile(r"^=+ .+ =+$")
_PYTEST_COUNTS = re.compile(r"\b(\d+) (failed|errors?|passed|skipped)\b")
_PYTEST_FINAL_LINE = re.compile(r"^=+ (.+) =+$")
_PYTEST_QUIET_FINAL = re.compile(
    r"(?:\d+ (?:failed|errors?|passed|skipped|deselected|xfailed|xpassed|warnings?)"
    r"(?:, )?)+ in \d+(?:\.\d+)?s(?: \([^\r\n]*\))?"
)
_PYTEST_DURATION = re.compile(r"\bin (\d+(?:\.\d+)?s)(?: \([^\r\n]*\))?$")
_PYTEST_FAILING_BYTES = 4_096
_RETURN_FAILING_NODES = 10


def pytest_node_inventory(root: Path) -> frozenset[str]:
    """Read definitions from controller-owned source, never importing test code.

    No symlink or hidden/cache tree is followed. Resource limits fail closed;
    unsupported dynamic test definitions are omitted rather than trusted.
    """
    nodes: set[str] = set()
    entries = 0
    total_bytes = 0
    try:
        if root.is_symlink() or not root.is_dir():
            return frozenset()
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.')
                             and d not in {'node_modules', '__pycache__', 'venv', 'site-packages'}
                             and not (Path(directory) / d).is_symlink())
            entries += len(dirs) + len(files)
            if entries > 100_000:
                return frozenset()
            for name in sorted(files):
                if not (name.startswith('test_') or name.endswith('_test.py')) or not name.endswith('.py'):
                    continue
                path = Path(directory) / name
                if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                    continue
                size = path.stat().st_size
                total_bytes += size
                if size > 2_000_000 or total_bytes > 64_000_000:
                    return frozenset()
                relative = path.relative_to(root).as_posix()
                try:
                    tree = ast.parse(path.read_bytes())
                except (SyntaxError, ValueError, UnicodeError, MemoryError, RecursionError):
                    # A parse bomb (deep nesting) must not crash repo_test; its
                    # definitions are simply never trusted.
                    continue
                for definition in tree.body:
                    if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)) and definition.name.startswith('test'):
                        nodes.add(f'{relative}::{definition.name}')
                    elif isinstance(definition, ast.ClassDef) and definition.name.startswith('Test'):
                        for method in definition.body:
                            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith('test'):
                                nodes.add(f'{relative}::{definition.name}::{method.name}')
    except (OSError, ValueError):
        return frozenset()
    return frozenset(nodes)


_NODE_INVENTORY_CACHE_SIZE = 32
_NODE_INVENTORIES: OrderedDict[tuple[str, str], frozenset[str]] = OrderedDict()


def _inventory_key(root: Path, scope_id: str) -> tuple[str, str]:
    # Normalise so the runner's resolved root and the classifier's lease path
    # always agree; a mismatch would silently make every failure untrusted.
    return (str(Path(root).resolve()), scope_id)


def remember_node_inventory(root: Path, scope_id: str, inventory: frozenset[str]) -> None:
    """Record the inventory captured before a run for the result classifier."""
    try:
        key = _inventory_key(root, scope_id)
    except (OSError, RuntimeError):
        return
    _NODE_INVENTORIES[key] = inventory
    _NODE_INVENTORIES.move_to_end(key)
    while len(_NODE_INVENTORIES) > _NODE_INVENTORY_CACHE_SIZE:
        _NODE_INVENTORIES.popitem(last=False)


def recorded_node_inventory(root: Path, scope_id: str) -> frozenset[str] | None:
    """Return the pre-run inventory, or ``None`` when no run recorded one.

    ``None`` is distinct from an empty inventory: a miss must make the whole
    result untrusted, including a summary whose ``failing`` list is empty.
    """
    try:
        key = _inventory_key(root, scope_id)
    except (OSError, RuntimeError):
        # e.g. a symlink loop planted out of band: fail closed, never crash
        # result classification.
        return None
    return _NODE_INVENTORIES.get(key)


def validated_pytest_node(candidate: str, inventory: frozenset[str]) -> str | None:
    """Remove parameter prose; only return a real leased-source definition."""
    if _PYTEST_NODE_ID.fullmatch(candidate) is None:
        return None
    base, bracket, parameter = candidate.partition('[')
    if bracket and (not parameter.endswith(']') or len(parameter[:-1]) > 64
                    or '[' in parameter or ']' in parameter[:-1]):
        return None
    if base not in inventory:
        return None
    return base


def pytest_failure_summary(
    stdout: bytes, inventory: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Extract numeric observations and source-validated ids from untrusted output.

    A plugin may forge sections/counts, so section position is not an attestation.
    Only source definitions cross the boundary; counts remain observations.
    """
    lines = stdout.decode("utf-8", errors="replace").splitlines()
    failing: list[str] = []
    dropped = 0
    # Read the last section, but validate every id against pre-execution source.
    failing_bytes = 0
    starts = [i for i, line in enumerate(lines) if _PYTEST_SUMMARY_HEADER.fullmatch(line)]
    if starts:
        for line in lines[starts[-1] + 1:]:
            if _PYTEST_SECTION_END.fullmatch(line):
                break
            if line.startswith(("FAILED ", "ERROR ")):
                # Pytest may append ' - reason'; never parse the reason as an id.
                candidate = line.split(" ", 1)[1].split(" - ", 1)[0]
                node = validated_pytest_node(candidate, inventory)
                if node is None:
                    dropped += 1
                elif node in failing:
                    continue
                elif len(failing) < 50 and failing_bytes + len(node) <= _PYTEST_FAILING_BYTES:
                    failing.append(node)
                    failing_bytes += len(node)
                else:
                    dropped += 1
    counts: dict[str, int | None] = dict.fromkeys(("failed", "errors", "passed", "skipped"))
    for line in reversed(lines):
        match = _PYTEST_FINAL_LINE.fullmatch(line)
        if match is not None:
            text = match[1]
        elif _PYTEST_QUIET_FINAL.fullmatch(line):
            text = line
        else:
            continue
        found = _PYTEST_COUNTS.findall(text)
        if found:
            for amount, kind in found:
                if len(amount) <= 9:
                    counts["errors" if kind in {"error", "errors"} else kind] = int(amount)
            break
    return {**counts, "failing": failing, "failing_dropped": dropped}


def _pytest_terminal_summary(stdout: bytes) -> dict[str, int | str] | None:
    """Return only numeric pytest footer observations, never arbitrary output text."""
    for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
        match = _PYTEST_FINAL_LINE.fullmatch(line)
        text = match[1] if match is not None else line
        if match is None and _PYTEST_QUIET_FINAL.fullmatch(line) is None:
            continue
        duration = _PYTEST_DURATION.search(text)
        counts = _PYTEST_COUNTS.findall(text)
        if duration is None or not counts:
            continue
        summary: dict[str, int | str] = {
            "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
            "duration": duration[1],
        }
        for amount, kind in counts:
            if len(amount) <= 9:
                summary["errors" if kind in {"error", "errors"} else kind] = int(amount)
        return summary
    return None


class ProjectTestRefusal(RuntimeError):
    """A named policy refusal, distinct from a red test suite.

    Omitted phase information deliberately fails closed to the post-execution
    verdict; genuinely pre-execution sites must opt out explicitly.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        execution_started: bool = True,
        fixed_message: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.execution_started = execution_started
        self.fixed_message = fixed_message


@dataclass(frozen=True)
class ProjectTestProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    output_limited: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0


@dataclass(frozen=True)
class ProjectTestResult:
    ok: bool
    code: str
    returncode: int | None
    pytest_summary: dict[str, int | str] | None = field(default=None, kw_only=True)
    failing_node_ids: list[str] = field(default_factory=list, kw_only=True)
    stdout: str = ""
    stderr: str = ""
    command: tuple[str, ...] = ()
    command_source: str = ""
    output_limited: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0
    stdout_return_dropped_bytes: int = 0
    stderr_return_dropped_bytes: int = 0
    git_context: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    suite: str = "default"
    failure_summary: dict[str, object] | None = None


ContainedRunner = Callable[..., Awaitable[CollectedExecutionResult]]
CheckoutFactory = Callable[..., ContainedCheckout]


def _selector_matches(path: str, suite: RepositoryTestSuite) -> bool:
    """Use the same path rule for suite inference and refusal diagnostics."""
    selector_path = path.partition("::")[0]
    return (
        selector_path.startswith(suite.selector_prefixes)
        or selector_path.endswith(suite.selector_suffixes)
    )


def _configured_command(
    repo_slug: str, selectors: tuple[str, ...] = (), suite: str | None = None,
) -> tuple[tuple[str, ...], dict[str, str], str, str, bool]:
    """Resolve Worklink's deployment command into one shell-free fixed argv."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        raise ProjectTestRefusal(
            "test_not_configured",
            "MIMIR_HOME is not configured",
            execution_started=False,
        )
    try:
        config = WorklinkConfig.load(Path(home) / "worklink.yaml")
        inventory = RepositoryInventory.load(Path(home) / "repositories.yaml")
        record = inventory.repository(repo_slug) if inventory.declared else None
        if record is not None and record.test_command is not None:
            command = record.test_command
            source = "repository"
        else:
            command = config.defaults.test_command
            source = "deployment"
        suites = record.test_suites if record is not None else ()
        legacy = not suites
        if legacy or (record.test_command is not None and not any(s.default for s in suites)):
            suites = (RepositoryTestSuite("default", command, default=True), *suites)
        default = next((s for s in suites if s.default), None)
        if suite is not None:
            matches = [s for s in suites if s.name == suite]
        elif (
            selectors and not legacy and isinstance(selectors, tuple)
            and all(isinstance(s, str) for s in selectors)
        ):
            matches = [
                s for s in suites
                if all(_selector_matches(path, s) for path in selectors)
            ]
        else:
            matches = [default] if default is not None else []
        if len(matches) != 1:
            message = "select exactly one declared test suite: " + ", ".join(s.name for s in suites)
            if not matches and selectors and not legacy and suite is None:
                unmatched = [
                    path for path in selectors
                    if not any(_selector_matches(path, s) for s in suites)
                ]
                if unmatched:
                    options = " (or ".join(f'suite="{s.name}"' for s in suites)
                    message += f"; unmatched selectors: {unmatched!r}; pass {options}" + ")" * (len(suites) - 1)
            raise ProjectTestRefusal(
                "test_suite_selection_refused",
                message,
                execution_started=False,
            )
        chosen = matches[0]
        command = chosen.command
        if not legacy and chosen in record.test_suites:
            source = "repository"
        words = shlex.split(command, posix=True)
    except ProjectTestRefusal:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectTestRefusal(
            "test_config_invalid",
            "project test command is invalid",
            execution_started=False,
        ) from exc
    if not words:
        raise ProjectTestRefusal(
            "test_command_unresolvable",
            f"project test command from {source} configuration is empty",
            execution_started=False,
        )

    env = {
        "PATH": _PATH,
        # HOME is deliberately absent here. A writable HOME is required -- every
        # mainstream runner initialises a cache under it, and "/nonexistent" made
        # uv fail before reaching pytest ("Permission denied" creating
        # /nonexistent/.cache/uv) -- but it must be created FRESH PER EXECUTION and
        # removed afterwards, so ``execute`` owns it rather than this function.
        #
        # A shared HOME would be a channel between executions: the tests running in
        # it are PR-controlled, so one PR could plant ambient config (a .npmrc, a
        # pip.conf -- only Git configuration is neutralised below) for a later PR to
        # pick up. Caching is not worth a cross-PR write channel.
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if Path(words[0]).name == "env":
        index = 1
        while index < len(words) and words[index] in {"-u", "--unset"}:
            if index + 1 >= len(words) or not words[index + 1].isidentifier():
                raise ProjectTestRefusal(
                    "test_config_invalid",
                    "invalid env unset directive",
                    execution_started=False,
                )
            env.pop(words[index + 1], None)
            index += 2
        words = words[index:]
        if not words:
            raise ProjectTestRefusal(
                "test_config_invalid", "test runner is missing", execution_started=False
            )

    if not legacy and selectors and Path(words[0]).name == "npm" and words[1:2] == ["run"]:
        if "--" not in words:
            words.append("--")
    executable = Path(words[0])
    try:
        resolved_text = (
            str(executable.resolve(strict=True))
            if executable.is_absolute()
            else shutil.which(words[0], path=_PATH)
        )
    except OSError as exc:
        raise ProjectTestRefusal(
            "test_command_unresolvable",
            "configured test runner is unavailable",
            execution_started=False,
        ) from exc
    if not resolved_text:
        raise ProjectTestRefusal(
            "test_command_unresolvable",
            "configured test runner is unavailable",
            execution_started=False,
        )
    resolved = Path(resolved_text)
    try:
        resolved = resolved.resolve(strict=True)
    except OSError as exc:
        raise ProjectTestRefusal(
            "test_command_unresolvable",
            "configured test runner is unavailable",
            execution_started=False,
        ) from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ProjectTestRefusal(
            "test_command_unresolvable",
            "configured test runner is unavailable",
            execution_started=False,
        )
    return (str(resolved), *words[1:]), env, source, chosen.name, chosen == default


def _validated_selectors(
    root: Path, selectors: tuple[str, ...], *, suite: str = "default",
) -> tuple[str, ...]:
    if not isinstance(selectors, tuple):
        raise ProjectTestRefusal(
            "test_selector_invalid",
            "test selectors must be a tuple",
            execution_started=False,
        )
    if len(selectors) > _MAX_SELECTORS:
        raise ProjectTestRefusal(
            "test_selector_count_exceeded",
            "too many test selectors",
            execution_started=False,
        )
    if any(not isinstance(item, str) for item in selectors):
        raise ProjectTestRefusal(
            "test_selector_invalid",
            "test selectors must be strings",
            execution_started=False,
        )
    if sum(len(item.encode("utf-8")) for item in selectors) > _MAX_SELECTOR_BYTES:
        raise ProjectTestRefusal(
            "test_selectors_too_large",
            "test selectors are too large",
            execution_started=False,
        )
    validated: list[str] = []
    for item in selectors:
        path_text, separator, node_id = item.partition("::")
        candidate = PurePosixPath(path_text)
        if suite == "frontend" and (separator or candidate.suffix not in {".ts", ".tsx"}):
            raise ProjectTestRefusal(
                "test_selector_invalid",
                "frontend selectors must be relative .ts or .tsx paths without node ids",
                execution_started=False,
            )
        if (
            not item
            or len(item) > _MAX_SELECTOR_LENGTH
            or not item.isascii()
            or _SELECTOR_PATTERN.fullmatch(item) is None
            or item.startswith(("-", "@"))
            or "\\" in path_text
        ):
            raise ProjectTestRefusal(
                "test_selector_invalid",
                "test selector is not a relative path or node id",
                execution_started=False,
            )
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ProjectTestRefusal(
                "test_selector_outside_checkout",
                "test selector does not resolve inside the authorized checkout",
                execution_started=False,
            )
        if any(part in {"", "."} for part in candidate.parts):
            raise ProjectTestRefusal(
                "test_selector_invalid",
                "test selector is not a relative path or node id",
                execution_started=False,
            )
        unresolved = root / path_text
        if any(
            (root.joinpath(*candidate.parts[:index])).is_symlink()
            for index in range(1, len(candidate.parts) + 1)
        ):
            raise ProjectTestRefusal(
                "test_selector_symlink",
                "test selector traverses a symlink in the authorized checkout",
                execution_started=False,
            )
        try:
            resolved = unresolved.resolve(strict=True)
            canonical = resolved.relative_to(root).as_posix()
        except FileNotFoundError as exc:
            raise ProjectTestRefusal(
                "test_selector_not_found",
                f"test selector was not found in checkout: {item}",
                execution_started=False,
            ) from exc
        except ValueError as exc:
            raise ProjectTestRefusal(
                "test_selector_outside_checkout",
                "test selector does not resolve inside the authorized checkout",
                execution_started=False,
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise ProjectTestRefusal(
                "test_selector_unresolvable",
                "test selector could not be resolved in the authorized checkout",
                execution_started=False,
            ) from exc
        validated.append(f"{canonical}::{node_id}" if separator else canonical)
    return tuple(validated)


def _remap_command(root: Path, command: tuple[str, ...]) -> tuple[str, ...]:
    executable = Path(command[0])
    try:
        relative = executable.relative_to(root)
    except ValueError:
        return command
    return (f"./{relative.as_posix()}", *command[1:])


def _safe_output(
    value: bytes,
    scrubber: SensitiveMaterialScrubber,
    limit: int,
) -> tuple[str, int]:
    scrubbed = redact_text(scrubber.scrub_text(value))
    return _bounded_scrubbed_output(scrubbed, limit)


def _bounded_scrubbed_output(scrubbed: str, limit: int) -> tuple[str, int]:
    if len(scrubbed) <= limit:
        return scrubbed, 0
    suffix = scrubbed
    # Reserve a short opening for context and keep the terminal diagnostics.
    # Count UTF-8 bytes in the *scrubbed* text that the returned excerpt omits.
    head_size = min(limit // 4, len(suffix))
    prefix = suffix[:head_size]
    # Reserve enough space for the longest possible byte count, then use any
    # remaining space for the tail (the count and marker stay self-consistent).
    marker_budget = len(f"…[{len(scrubbed.encode('utf-8'))} bytes omitted]…")
    tail_size = min(max(0, limit - head_size - marker_budget), len(suffix) - head_size)
    tail = suffix[-tail_size:] if tail_size else ""
    dropped = len(suffix[head_size:len(suffix) - tail_size].encode("utf-8"))
    return prefix + f"…[{dropped} bytes omitted]…" + tail, dropped


def _safe_stderr_output(
    value: bytes,
    scrubber: SensitiveMaterialScrubber,
    limit: int,
) -> tuple[str, int]:
    """Prefer a pytest faulthandler dump over positional stderr context."""
    scrubbed = redact_text(scrubber.scrub_text(value))
    marker = re.search(r"Timeout \([^\r\n]+\)!", scrubbed)
    if marker is not None and len(scrubbed) > limit:
        # Faulthandler lists stuck-thread frames first: unlike ordinary output,
        # keep a contiguous dump opening, not a head/tail excerpt of the dump.
        start = marker.start()
        omitted_prefix = len(scrubbed[:start].encode("utf-8"))
        prefix = f"…[{omitted_prefix} bytes omitted]…" if start else ""
        dump = scrubbed[start:]
        kept_chars = max(0, limit - len(prefix))
        dropped = omitted_prefix + len(dump[kept_chars:].encode("utf-8"))
        return prefix + dump[:kept_chars], dropped
    return _bounded_scrubbed_output(scrubbed, limit)


def _live_output_paths(home: str, identifier: str) -> tuple[Path, Path, str, str]:
    root = Path(home).resolve() / _LIVE_OUTPUT_ROOT
    stem = f"repo-test-{identifier}"
    stdout_relative = (_LIVE_OUTPUT_ROOT / f"{stem}.stdout.log").as_posix()
    stderr_relative = (_LIVE_OUTPUT_ROOT / f"{stem}.stderr.log").as_posix()
    return (
        root / f"{stem}.stdout.log",
        root / f"{stem}.stderr.log",
        stdout_relative,
        stderr_relative,
    )


def _remove_live_output(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            # Checkpoint cleanup must not change a completed suite's verdict.
            # The transcript janitor removes any file that survives this attempt.
            pass


def _worker_can_search(metadata: os.stat_result) -> bool:
    identities = get_identities()
    mode = metadata.st_mode
    if metadata.st_uid == identities.worklink_uid:
        return bool(mode & stat.S_IXUSR)
    if metadata.st_gid == identities.worklink_gid:
        return bool(mode & stat.S_IXGRP)
    return bool(mode & stat.S_IXOTH)


def _permission_diagnostic(path: Path) -> dict[str, object] | None:
    identities = get_identities()
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    diagnostic: dict[str, object] | None = None
    for part in absolute.parts[1:-1]:
        current /= part
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError:
            return diagnostic
        if stat.S_ISDIR(metadata.st_mode) and not _worker_can_search(metadata):
            diagnostic = {
                "path": redact_text(str(absolute)),
                "path_mode": f"0o{stat.S_IMODE(metadata.st_mode):03o}",
                "path_uid": metadata.st_uid,
                "path_gid": metadata.st_gid,
                "runner_effective_uid": identities.worklink_uid,
                "runner_effective_gid": identities.worklink_gid,
                "traversal_failed": redact_text(str(current)),
            }
    return diagnostic


def _permission_diagnostic_from_error(
    error: BaseException | bytes,
) -> dict[str, object] | None:
    path: str | None = None
    if isinstance(error, OSError) and error.errno == 13 and error.filename is not None:
        path = os.fsdecode(error.filename)
    elif isinstance(error, bytes):
        match = _PERMISSION_PATH_PATTERN.search(error)
        if match is not None:
            path = os.fsdecode(match.group("path"))
    if path is None or not os.path.isabs(path):
        return None
    return _permission_diagnostic(Path(path))


def _permission_refusal_message(diagnostic: dict[str, object]) -> str:
    fields = " ".join(f"{key}={value}" for key, value in diagnostic.items())
    return f"contained project test path permission denied: {fields}"


def _git_execution_context() -> str:
    identities = get_identities()
    return (
        "contained Git context: runner=worklink "
        f"uid={identities.worklink_uid} gid={identities.worklink_gid}; "
        # The repo-test checkout is owned by the RUNNER, not the controller, so a
        # repository's own provisioning can set modes on tracked files. Naming the
        # wrong owner here sends a reader after a permission problem they do not have.
        f"checkout_owner=worklink uid={identities.worklink_uid} "
        f"gid={identities.worklink_gid}; global_config=/dev/null; "
        "system_config=disabled; safe.directory=*"
    )


class RepoProjectTests:
    """Execute the fixed project test command in a disposable contained checkout."""

    def __init__(
        self,
        review_state: RepoReviewState | None = None,
        *,
        retained_scope: RetainedFactoryScope | None = None,
        runner: ContainedRunner = execute_contained,
        checkout_factory: CheckoutFactory = create_repo_test_checkout,
        timeout: float = _TIMEOUT_SECONDS,
        output_limit: int = _CAPTURE_BYTES,
    ) -> None:
        if (review_state is None) == (retained_scope is None):
            raise ValueError("exactly one repository scope is required")
        if timeout <= 0 or output_limit <= 0:
            raise ValueError("project test timeout and output limit must be positive")
        self._state = review_state
        self._retained_scope = retained_scope
        self._runner = runner
        self._checkout_factory = checkout_factory
        self._timeout = timeout
        self._output_limit = output_limit

    async def execute(
        self, selectors: tuple[str, ...] = (), *, suite: str | None = None,
    ) -> ProjectTestResult:
        scope = (
            self._state.action_scope
            if self._state is not None else self._retained_scope
        )
        assert scope is not None
        retained = isinstance(scope, RetainedFactoryScope)
        if not retained and RepoPRAction.TEST.value not in scope.allowed_operations:
            raise ProjectTestRefusal("scope_action_denied", "scope does not grant repo.test", execution_started=False)
        git_tools: RepoGitTools | None = None
        try:
            git_tools = (
                RepoGitTools(
                    retained_scope=scope, runner=retained_factory_git_runner,
                )
                if retained else RepoGitTools(self._state)
            )
            root = git_tools.validated_checkout_root()
        except GitRefusal as exc:
            raise ProjectTestRefusal(
                exc.code,
                str(exc),
                execution_started=bool(
                    git_tools is not None and git_tools.execution_started
                ),
            ) from exc
        try:
            repository = scope.repository if retained else scope.canonical_repo
            issue = scope.issue_id if retained else scope.pr_number
            command, configured_env, command_source, suite_name, is_default = _configured_command(
                repository, selectors, suite
            )
            selected = _validated_selectors(root, selectors, suite=suite_name)
        except ProjectTestRefusal as exc:
            raise ProjectTestRefusal(
                exc.code,
                str(exc),
                execution_started=exc.execution_started,
                fixed_message=exc.fixed_message,
            ) from exc
        # Capture controller source before any worker/plugin can execute.
        node_inventory = await asyncio.to_thread(pytest_node_inventory, root)
        remember_node_inventory(root, scope.scope_id, node_inventory)
        scrubber = SensitiveMaterialScrubber(
            checkout=root,
            source_paths=(os.environ.get("MIMIR_HOME", ""),),
        )
        try:
            checkout_arguments = {
                "scope_id": scope.scope_id,
                "pr_number": issue,
                "known_sensitive": (),
            }
            if retained:
                checkout_arguments.update(
                    excluded_prefixes=(b".factory",),
                    source_git_runner=retained_factory_subprocess_runner,
                    bundle_provider=retained_factory_snapshot_bundle,
                )
            checkout = self._checkout_factory(root, **checkout_arguments)
        except SnapshotCredentialsRefused as exc:
            await safe_log_event(
                "repo_test_containment_refused",
                reason_code="snapshot_credentials",
                repository=repository,
                pull_request=issue,
            )
            raise ProjectTestRefusal(
                "test_snapshot_credentials_refused",
                "project test snapshot contains credential-like material",
                execution_started=True,
                fixed_message=True,
            ) from exc
        except SnapshotEmbeddedRepository as exc:
            # Distinct from the generic branch below on purpose: this one is an
            # ordinary tree layout, not a broken snapshot, and the fix is to point
            # at a tree without a nested checkout. Folded into "unavailable" it
            # reads as a fault in the containment path itself.
            await safe_log_event(
                "repo_test_containment_refused",
                reason_code="snapshot_embedded_repository",
                repository=repository,
                pull_request=issue,
            )
            raise ProjectTestRefusal(
                "test_snapshot_embedded_repository",
                "project test snapshot source contains an embedded Git repository",
                execution_started=True,
                fixed_message=True,
            ) from exc
        except (ContainedSnapshotError, OSError, RuntimeError, ValueError) as exc:
            await safe_log_event(
                "repo_test_containment_refused",
                reason_code="snapshot_unavailable",
                repository=repository,
                pull_request=issue,
            )
            raise ProjectTestRefusal(
                "test_snapshot_unavailable",
                "project test snapshot is unavailable",
                execution_started=True,
                fixed_message=True,
            ) from exc

        identifier = str(uuid.uuid4())
        stdout_path, stderr_path, stdout_relative, stderr_relative = _live_output_paths(
            os.environ["MIMIR_HOME"], identifier
        )
        scrubber.add_path(checkout.path)
        command = _remap_command(root, command)
        environment = base_worker_environment(identifier)
        environment.update(configured_env)
        environment.pop("HOME", None)
        environment.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        })
        if Path(command[0]).name == "uv":
            # uv otherwise searches above the fd-entered checkout for uv.toml.
            # The snapshot's 0700 isolation boundary deliberately cannot be
            # traversed by the worker, so ambient config discovery must be off.
            environment["UV_NO_CONFIG"] = "1"
            # The executor may seed this disposable cache from its image-owned
            # read-only cache. Misses remain writable here and can download
            # normally without giving executions write access to shared state.
            environment["UV_CACHE_DIR"] = f'{environment["XDG_CACHE_HOME"]}/uv'
        if any(scrubber.contains_sensitive(value) for value in (*command, *environment.values())):
            try:
                checkout.close()
            except (OSError, RuntimeError, ValueError) as exc:
                await safe_log_event(
                    "repo_test_containment_refused",
                    reason_code="cleanup_failed",
                    repository=repository,
                    pull_request=issue,
                )
                raise ProjectTestRefusal(
                    "test_snapshot_cleanup_failed",
                    "project test snapshot cleanup failed",
                    execution_started=True,
                    fixed_message=True,
                ) from exc
            raise ProjectTestRefusal(
                "test_config_invalid",
                "project test command or environment contains a controller path",
                execution_started=True,
                fixed_message=True,
            )
        result: CollectedExecutionResult | None = None
        try:
            try:
                result = await self._runner(
                    (*command, *selected),
                    checkout.capability,
                    environment,
                    (),
                    identifier=identifier,
                    timeout_s=self._timeout,
                    stdout_limit=self._output_limit,
                    stderr_limit=self._output_limit,
                    scrubber=scrubber,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
            except StaleWorkerExecutorError as exc:
                await safe_log_event(
                    "repo_test_containment_refused",
                    reason_code="stale_root_executor",
                    repository=repository,
                    pull_request=issue,
                )
                raise ProjectTestRefusal(
                    "test_stale_root_executor",
                    str(exc),
                    execution_started=True,
                ) from exc
            except (OSError, RuntimeError, ValueError) as exc:
                diagnostic = _permission_diagnostic_from_error(exc)
                if diagnostic is not None:
                    await safe_log_event(
                        "repo_test_containment_refused",
                        reason_code="path_permission_denied",
                        repository=repository,
                        pull_request=issue,
                        **diagnostic,
                    )
                    raise ProjectTestRefusal(
                        "test_path_permission_denied",
                        _permission_refusal_message(diagnostic),
                        execution_started=True,
                    ) from exc
                await safe_log_event(
                    "repo_test_containment_refused",
                    reason_code="containment_unavailable",
                    repository=repository,
                    pull_request=issue,
                )
                raise ProjectTestRefusal(
                    "test_containment_unavailable",
                    "contained project test execution is unavailable",
                    execution_started=True,
                    fixed_message=True,
                ) from exc
            if result.exit_code not in {None, 0}:
                diagnostic = _permission_diagnostic_from_error(result.stderr)
                if diagnostic is not None:
                    await safe_log_event(
                        "repo_test_containment_refused",
                        reason_code="path_permission_denied",
                        repository=repository,
                        pull_request=issue,
                        **diagnostic,
                    )
                    raise ProjectTestRefusal(
                        "test_path_permission_denied",
                        _permission_refusal_message(diagnostic),
                        execution_started=True,
                    )
        finally:
            try:
                checkout.close()
            except (OSError, RuntimeError, ValueError) as exc:
                await safe_log_event(
                    "repo_test_containment_refused",
                    reason_code="cleanup_failed",
                    repository=repository,
                    pull_request=issue,
                )
                raise ProjectTestRefusal(
                    "test_snapshot_cleanup_failed",
                    "project test snapshot cleanup failed",
                    execution_started=True,
                    fixed_message=True,
                ) from exc
            finally:
                if result is None or not result.timed_out:
                    _remove_live_output(stdout_path, stderr_path)

        assert result is not None

        stdout, stdout_return_dropped = _safe_output(result.stdout, scrubber, _RETURN_STDOUT_CHARS)
        stderr, stderr_return_dropped = _safe_stderr_output(result.stderr, scrubber, _RETURN_STDERR_CHARS)
        summary = _pytest_terminal_summary(result.stdout)
        failure_summary = (
            pytest_failure_summary(result.stdout, node_inventory)
            if result.exit_code not in {None, 0} and not result.output_overflow and not result.timed_out
            else None
        )
        truncation = {
            "suite": suite_name,
            "output_limited": result.output_overflow or bool(stdout_return_dropped or stderr_return_dropped),
            "stdout_dropped_bytes": result.stdout_dropped_bytes,
            "stderr_dropped_bytes": result.stderr_dropped_bytes,
            "stdout_return_dropped_bytes": stdout_return_dropped,
            "stderr_return_dropped_bytes": stderr_return_dropped,
        }
        parsed = {
            "pytest_summary": summary,
            "failing_node_ids": (failure_summary["failing"][:_RETURN_FAILING_NODES]
                                 if failure_summary is not None else []),
        }
        if result.timed_out:
            return ProjectTestResult(
                False, "test_timeout", None, stdout, stderr,
                command, command_source, **truncation, **parsed,
                git_context=_git_execution_context(),
                stdout_path=stdout_relative,
                stderr_path=stderr_relative,
            )
        if result.exit_code != 0 or result.output_overflow:
            code = "tests_failed"
            if result.output_overflow:
                # Preserve the exit distinction without treating truncated evidence
                # as a pass, even when the child managed to exit successfully.
                code = "test_output_overflow" if result.exit_code == 0 else "tests_failed_output_overflow"
            return ProjectTestResult(
                False, code, result.exit_code, stdout, stderr,
                command, command_source, **truncation, **parsed,
                git_context=_git_execution_context(),
                failure_summary=(failure_summary if code == "tests_failed" else None),
            )
        # A non-default suite must not satisfy the existing default-test push gate.
        if not retained and not selectors and is_default:
            assert self._state is not None
            head = self._state.git_expected_head
            if head is None:
                raise ProjectTestRefusal(
                    "inactive_checkout",
                    "the checkout has no current HEAD",
                    execution_started=True,
                    fixed_message=True,
                )
            self._state.record_full_test(scope.scope_id, head)
        return ProjectTestResult(
            True, "tests_passed", 0, stdout, stderr, command, command_source,
            **truncation, **parsed, git_context=_git_execution_context(),
        )

# Model-facing repository tools
import logging
from contextlib import ExitStack, nullcontext
from dataclasses import asdict
import os
import re
from pathlib import Path
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool, ToolException, tool
from langchain_core.tools.base import create_schema_from_function

from mimir.models import AuthContext, RepoReviewState, RetainedFactoryScope, SourceLabel



from mimir.tools.refusals import ToolPolicyRefusal


log = logging.getLogger(__name__)


_GIT_BINDING_REFUSAL_CODES = frozenset({
    "cross_pr_checkout",
    "inactive_checkout",
    "invalid_checkout",
    "invalid_scope",
})
_CONTENT_FREE_GIT_REFUSALS = {
    "inactive_checkout": frozenset({"an active matching checkout lease is required"}),
    "invalid_checkout": frozenset({
        "retained checkout is unavailable", "retained checkout identity is invalid",
        "checkout lease may not be a symlink", "checkout lease path is unavailable",
        "checkout lease escapes its server root",
    }),
    "invalid_scope": frozenset({
        "scope head is not a full commit id", "scope base is not a full commit id",
        "scope destination is not a branch ref", "scope base is not a branch ref",
        "scope remote is not server-bound origin",
    }),
    "scope_action_denied": frozenset({
        "retained factory scope does not grant this repository operation",
    }),
}
_REPOSITORY_AUTHORIZATION_REFUSED = "repository_authorization_refused"
_REPOSITORY_BINDING_INVALID = "repository_binding_invalid"
_REPOSITORY_GIT_FAILED = "repository_git_failed"
_FIXED_TEST_REFUSALS = {
    "test_snapshot_unavailable": frozenset({"project test snapshot is unavailable"}),
    "test_containment_unavailable": frozenset({"contained project test execution is unavailable"}),
    "test_snapshot_cleanup_failed": frozenset({"project test snapshot cleanup failed"}),
    "test_config_invalid": frozenset({
        "project test command is invalid", "invalid env unset directive",
        "test runner is missing",
        "project test command or environment contains a controller path",
    }),
    "test_snapshot_credentials_refused": frozenset({
        "project test snapshot contains credential-like material",
    }),
    "test_snapshot_embedded_repository": frozenset({
        "project test snapshot source contains an embedded Git repository",
    }),
    "inactive_checkout": frozenset({"the checkout has no current HEAD"}),
}
_SUMMARY_SELECTOR = re.compile(r"[A-Za-z0-9._/,:+=-]{1,256}", re.ASCII)
_SUMMARY_SUITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", re.ASCII)
_SUMMARY_HEAD = re.compile(r"[0-9a-f]{40,64}", re.ASCII)


def _tool_refusal(
    message: str,
    exc: BaseException,
    *,
    execution_started: bool = True,
) -> ToolException:
    """Preserve pre-execution policy refusals without downgrading execution faults."""
    if isinstance(exc, ToolPolicyRefusal) and not execution_started:
        return ToolPolicyRefusal(message)
    if not execution_started and isinstance(exc, (GitRefusal, ProjectTestRefusal)):
        return ToolPolicyRefusal(message)
    return ToolException(message)


def _state(
    runtime: ToolRuntime[AuthContext] | None,
    repository: str,
    pull_request: int,
) -> RepoReviewState:
    from .forge import resolve_review_state

    try:
        return resolve_review_state(runtime, repository, pull_request)
    except ToolException as exc:
        detail = str(exc).removeprefix("pull-request operation rejected: ")
        message = f"repository operation rejected: {detail}"
        raise _tool_refusal(message, exc) from exc


def _retained_scope(
    runtime: ToolRuntime[AuthContext] | None,
    repository: str,
    issue: int,
) -> RetainedFactoryScope | None:
    context = getattr(runtime, "context", None) if runtime is not None else None
    scope = getattr(context, "retained_factory_scope", None)
    if not isinstance(scope, RetainedFactoryScope):
        return None
    if scope.repository != repository or scope.issue_id != issue:
        return None
    return scope


def _publish_retained_result(
    runtime: ToolRuntime[AuthContext] | None, scope: RetainedFactoryScope,
) -> None:
    from mimir.access_control import publish_protected_result

    context = getattr(runtime, "context", None) if runtime is not None else None
    principal = getattr(context, "canonical_principal", None)
    if getattr(context, "is_service", False) and principal:
        principal = f"service:{principal}"
    publish_protected_result((SourceLabel(
        principal=principal,
        domain="repository",
        resource_id=(
            f"{scope.repository}#issue/{scope.issue_id}"
            f"@retained/{scope.scope_id}"
        ),
        bridge_instance="worklink",
        sensitivity="internal",
        authorized_principals=frozenset({principal}) if principal else frozenset(),
        source_kind="protected_tool",
        integrity="trusted",
        integrity_effect="active_ingest",
    ),))


def _retained_result(
    runtime: ToolRuntime[AuthContext] | None,
    scope: RetainedFactoryScope,
    result: dict[str, Any],
) -> dict[str, Any]:
    _publish_retained_result(runtime, scope)
    result["provenance"] = {
        "kind": "retained_factory_scope",
        "scope_id": scope.scope_id,
        "repository": scope.repository,
        "issue_id": scope.issue_id,
        "run_id": scope.run_id,
    }
    return result


def _retained_refusal(operation: str) -> ToolPolicyRefusal:
    return ToolPolicyRefusal(
        f"repository operation rejected: retained factory scope does not grant {operation}"
    )


def _publish_attested_lease_result(
    runtime: ToolRuntime[AuthContext] | None,
    state: RepoReviewState,
) -> None:
    """Publish exact-scope provenance when the checkout remains author-attested."""
    from mimir.access_control import (
        log_pr_checkout_lease_attestation_refused, pr_checkout_lease_attestation,
    )

    context = getattr(runtime, "context", None) if runtime is not None else None
    if context is None:
        return
    scope = state.action_scope
    from mimir.access_control import _github_repo_from_remote

    # The failed command may contain remote server text. Bind that text to our
    # origin and the exact allowed refs before inheriting the lease verdict.
    # The lease predicate itself pins the original head independently of the
    # base branch; local descendants retain their separate lineage checks.
    if (
        scope.head_remote != "origin"
        or scope.head_repo != scope.canonical_repo
        or _github_repo_from_remote(scope.canonical_origin) != scope.canonical_repo
        or not scope.destination_ref.startswith("refs/heads/")
        or scope.destination_ref != f"refs/heads/{scope.head_ref}"
        or scope.checkout_ref not in (None, f"refs/pull/{scope.pr_number}/head")
    ):
        return
    attestation = pr_checkout_lease_attestation(context, scope, state.checkout_lease)
    if not attestation.ok:
        log_pr_checkout_lease_attestation_refused(context, scope, attestation.reason)
        return
    _publish_attested_scope_result(runtime, scope, scope.observed_head_sha)


def _publish_attested_scope_result(
    runtime: ToolRuntime[AuthContext] | None, scope: Any, head_sha: str,
) -> None:
    """Publish the exact repository source shared by checkout and Git results."""
    from mimir.access_control import publish_protected_result

    context = getattr(runtime, "context", None) if runtime is not None else None
    if context is None:
        return
    principal = getattr(context, "canonical_principal", None)
    if getattr(context, "is_service", False) and principal:
        principal = f"service:{principal}"
    publish_protected_result((SourceLabel(
        principal=principal,
        domain="repository",
        resource_id=f"{scope.canonical_repo}#pull/{scope.pr_number}@{head_sha}",
        bridge_instance="forge",
        sensitivity="internal",
        authorized_principals=frozenset({principal}) if principal else frozenset(),
        source_kind="protected_tool",
        integrity="trusted",
        integrity_effect="active_ingest",
    ),))


def _enforcement_enabled(
    runtime: ToolRuntime[AuthContext] | None,
    *,
    repository: str,
    pull_request: int,
) -> bool:
    """Resolve the central flag, failing closed and reporting unknown state."""
    context = getattr(runtime, "context", None) if runtime is not None else None
    enforcement = getattr(context, "enforcement_enabled", None)
    if isinstance(enforcement, bool):
        return enforcement

    try:
        from mimir.event_logger import log_event_sync

        log_event_sync(
            "repo_enforcement_state_unknown",
            repository=repository,
            pull_request=pull_request,
            fallback_enforcement=True,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must not alter the decision
        log.warning(
            "repo_enforcement_state_unknown: repository=%s pull_request=%s; "
            "failing closed (event logging failed: %s)",
            repository,
            pull_request,
            exc,
        )
    return True


def _execute(
    runtime: ToolRuntime[AuthContext] | None,
    repository: str,
    pull_request: int,
    operation: Any,
) -> dict[str, Any]:
    git_tools: RepoGitTools | None = None
    state: RepoReviewState | None = None
    lease_guard = ExitStack()
    try:
        retained = _retained_scope(runtime, repository, pull_request)
        if retained is not None:
            if not isinstance(operation, (GitStatus, GitDiff, GitStage, GitCommit)):
                raise _retained_refusal(type(operation).__name__)
            home_value = os.environ.get("MIMIR_HOME", "").strip()
            if not home_value:
                raise ToolPolicyRefusal("repository operation rejected: MIMIR_HOME is unavailable")
            from .worklink.retained_scope import retained_factory_effect_lease
            from mimir.access_control import retained_invocation_lease_active

            host_held = retained_invocation_lease_active(
                retained, getattr(runtime, "context", None),
            )
            lease_context = (nullcontext(retained) if host_held else
                             retained_factory_effect_lease(Path(home_value), retained))
            with lease_context as lease:
                if not host_held and lease.scope is None:
                    raise ToolPolicyRefusal(
                        f"repository operation rejected: {lease.refusal_reason}"
                    )
                git_tools = RepoGitTools(
                    retained_scope=retained, runner=retained_factory_git_runner,
                )
                return _retained_result(
                    runtime, retained, asdict(git_tools.execute(operation)),
                )
        state = _state(runtime, repository, pull_request)
        if isinstance(getattr(state, "checkout_lease", None), PRCheckoutLease) and state.checkout_lease.is_active:
            lease_guard.enter_context(hold_pr_checkout_lease(state.checkout_lease))
        git_tools = RepoGitTools(
            state,
            auth_context=getattr(runtime, "context", None) if runtime is not None else None,
            enforce=_enforcement_enabled(
                runtime,
                repository=repository,
                pull_request=pull_request,
            ),
        )
        previous_head = state.action_scope.observed_head_sha
        result = asdict(git_tools.execute(operation))
        if (
            isinstance(operation, GitPush)
            and result["ok"]
            and state.action_scope.observed_head_sha != previous_head
        ):
            context = getattr(runtime, "context", None) if runtime is not None else None
            if context is not None and context.ifc_state is not None:
                context.ifc_state.record_own_push(
                    state.action_scope.canonical_repo, state.action_scope.pr_number,
                    previous_head,
                )
        _publish_attested_lease_result(runtime, state)
        return result
    except (GitRefusal, ToolException, RuntimeError, ValueError) as exc:
        cause_code = getattr(exc, "code", None)
        execution_started = bool(
            (exc.execution_started if isinstance(exc, GitRefusal) else False)
            or (git_tools is not None and git_tools.execution_started)
        )
        if isinstance(exc, ToolPolicyRefusal):
            code = _REPOSITORY_AUTHORIZATION_REFUSED
        elif execution_started:
            code = _REPOSITORY_GIT_FAILED
        elif isinstance(exc, ValueError) or cause_code in _GIT_BINDING_REFUSAL_CODES or (
            isinstance(exc, ToolException) and not isinstance(exc, GitRefusal)
        ):
            code = _REPOSITORY_BINDING_INVALID
        elif isinstance(exc, GitRefusal):
            code = _REPOSITORY_AUTHORIZATION_REFUSED
        else:
            code = _REPOSITORY_GIT_FAILED
        cause = f" [{cause_code}]" if cause_code else ""
        detail = _redact_git_output(str(exc))
        message = f"repository operation rejected ({code}){cause}: {detail}"
        if execution_started and state is not None:
            _publish_attested_lease_result(runtime, state)
        elif (
            isinstance(exc, GitRefusal)
            and not execution_started
            and str(exc) in _CONTENT_FREE_GIT_REFUSALS.get(exc.code, ())
        ):
            from mimir.access_control import publish_protected_result

            publish_protected_result((), content_free_error=message)
        raise _tool_refusal(
            message,
            exc,
            execution_started=execution_started,
        ) from None
    finally:
        lease_guard.close()


@tool
def repo_checkout(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Create the exact checkout lease bound to this turn's immutable PR scope."""
    if _retained_scope(runtime, repository, pull_request) is not None:
        raise _retained_refusal("repo_checkout")
    from .forge import _call, _client, _pr_content_authors, remediation_checkout_preflight

    context = getattr(runtime, "context", None) if runtime is not None else None
    state, stopped = remediation_checkout_preflight(context, repository, pull_request)
    if stopped is not None:
        return {"status": "stopped", "message": stopped}
    if state is None:
        raise ToolPolicyRefusal("repository checkout rejected: no authorized pull request state")
    try:
        lease, candidates = acquire_pr_checkout_lease(
            state.action_scope,
            owner=state.action_scope.principal,
            review_state=state,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        detail = _redact_git_output(str(exc))
        _publish_attested_lease_result(runtime, state)
        raise ToolException(f"repository checkout rejected: {detail}") from exc
    scope = state.action_scope
    # Record only turn-local authority, never trust in checkout-controlled metadata.
    # Clear an earlier verdict before fetching metadata, so failures stay closed.
    context.ifc_state.pr_checkout_author_trust[scope.scope_id] = None
    client = _client(scope)
    from .forge import _BOT_LOGIN, _author_verdict

    if callable(getattr(client, "author_is_trusted", None)) or (
        isinstance(scope.pull_request_author, str) and _BOT_LOGIN.fullmatch(scope.pull_request_author)
    ):
        authors, _ = _call(lambda: _pr_content_authors(client, scope))
        if authors == (scope.pull_request_author,) and scope.pull_request_author:
            verdict = _author_verdict(context, scope, scope.pull_request_author, client)
            context.ifc_state.pr_checkout_author_trust[scope.scope_id] = verdict
    _publish_attested_lease_result(runtime, state)
    return {
        "status": "resumed" if candidates else "checked_out",
        "path": str(lease.path),
        "scope_id": lease.scope_id,
        "head_sha": lease.head_sha,
        "candidate_commits": candidates,
    }


@tool
def repo_cleanup(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Revoke and remove this turn's exact active checkout lease."""
    if _retained_scope(runtime, repository, pull_request) is not None:
        raise _retained_refusal("repo_cleanup")
    state = _state(runtime, repository, pull_request)
    lease = state.checkout_lease
    if lease is None:
        raise ToolPolicyRefusal("repository cleanup rejected: no active checkout lease")
    _publish_attested_lease_result(runtime, state)
    try:
        removed = cleanup_pr_checkout_lease(lease, review_state=state)
    except (OSError, RuntimeError, ValueError) as exc:
        detail = _redact_git_output(str(exc))
        raise ToolException(f"repository cleanup rejected: {detail}") from exc
    return {"status": "cleaned", "removed": removed, "scope_id": state.action_scope.scope_id}


@tool
def repo_fetch(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Fetch only the immutable head and base refs bound to this turn."""
    return _execute(runtime, repository, pull_request, GitFetch())


@tool
def repo_status(
    repository: str,
    pull_request: int,
    include_untracked: bool = True,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read porcelain status from the active bound checkout.

    On a retained Worklink remediation turn, ``repository`` is the retained
    run's repository and ``pull_request`` is the incident's Chainlink issue id.
    """
    return _execute(runtime, repository, pull_request, GitStatus(include_untracked))


async def _repo_test_unlocked(
    repository: str,
    pull_request: int,
    selectors: tuple[str, ...] = (),
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
    suite: str | None = None,
    include_output: bool = False,
) -> dict[str, Any]:
    """Run configured tests in a contained repository snapshot.

    Omit ``suite`` to infer it only when every selector matches one declared
    suite's prefixes or suffixes (no selectors: default). Otherwise pass
    ``suite`` explicitly, e.g. ``suite="python"`` or ``suite="frontend"``.
    On a retained Worklink remediation turn,
    ``repository`` is the retained run's repository and ``pull_request`` is the
    incident's Chainlink issue id.
    """
    try:
        state: RepoReviewState | None = None
        retained = _retained_scope(runtime, repository, pull_request)
        if retained is not None:
            home_value = os.environ.get("MIMIR_HOME", "").strip()
            if not home_value:
                raise ToolPolicyRefusal("project test rejected: MIMIR_HOME is unavailable")
            from .worklink.retained_scope import retained_factory_effect_lease
            from mimir.access_control import retained_invocation_lease_active

            host_held = retained_invocation_lease_active(
                retained, getattr(runtime, "context", None),
            )
            lease_context = (nullcontext(retained) if host_held else
                             retained_factory_effect_lease(Path(home_value), retained))
            with lease_context as lease:
                if not host_held and lease.scope is None:
                    raise ToolPolicyRefusal(f"project test rejected: {lease.refusal_reason}")
                result = asdict(
                    await RepoProjectTests(retained_scope=retained).execute(
                        selectors, suite=suite,
                    )
                )
                result.pop("failure_summary", None)
                if result["pytest_summary"] is None:
                    result.pop("pytest_summary")
                result = _retained_result(runtime, retained, result)
                result["remediation_guidance"] = _remediation_test_guidance(
                    result["code"], scoped=bool(selectors),
                )
                return result
        state = _state(runtime, repository, pull_request)
        result = asdict(
            await RepoProjectTests(state).execute(selectors, suite=suite)
        )
        failure_summary = result.pop("failure_summary", None)
        if result["pytest_summary"] is None:
            result.pop("pytest_summary")
        _publish_attested_lease_result(runtime, state)
        if (
            result["code"] == "tests_failed" and not include_output
            and failure_summary is not None
            and type(result["returncode"]) is int and result["returncode"] != 0
            and _SUMMARY_SUITE.fullmatch(result["suite"]) is not None
            and _SUMMARY_HEAD.fullmatch(state.action_scope.observed_head_sha) is not None
            and isinstance(selectors, tuple)
            and len(selectors) <= 32
            and all(isinstance(item, str) and _SUMMARY_SELECTOR.fullmatch(item) for item in selectors)
        ):
            return {
                "ok": False, "code": "tests_failed", "exit_code": result["returncode"],
                "suite": result["suite"], "selectors": list(selectors),
                "summary": {**failure_summary, "head": state.action_scope.observed_head_sha},
                "remediation_guidance": (
                    "The summary lists failing node ids. Prefer reading the lease's test source "
                    "and rerunning selected ids. include_output=true reveals bounded raw output."
                ),
            }
        result["remediation_guidance"] = _remediation_test_guidance(result["code"], scoped=bool(selectors))
        return result
    except (ProjectTestRefusal, RuntimeError, ValueError) as exc:
        if state is not None and (
            not isinstance(exc, ProjectTestRefusal) or exc.execution_started
        ) and not (
            isinstance(exc, ProjectTestRefusal) and exc.fixed_message
            and str(exc) in _FIXED_TEST_REFUSALS.get(exc.code, ())
        ):
            _publish_attested_lease_result(runtime, state)
        code = getattr(exc, "code", "project_test_failed")
        message = f"project test rejected ({code}): {exc}"
        message += "\n" + _remediation_test_guidance(code, scoped=bool(selectors))
        if (
            isinstance(exc, ProjectTestRefusal)
            and exc.fixed_message
            and str(exc) in _FIXED_TEST_REFUSALS.get(code, ())
        ):
            raise ToolPolicyRefusal(message) from exc
        raise _tool_refusal(
            message,
            exc,
            execution_started=(
                exc.execution_started
                if isinstance(exc, ProjectTestRefusal)
                else True
            ),
        ) from exc


@tool
async def repo_test(
    repository: str,
    pull_request: int,
    selectors: tuple[str, ...] = (),
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
    suite: str | None = None,
    include_output: bool = False,
) -> dict[str, Any]:
    """Run configured tests in a contained repository snapshot under the live lease."""
    retained = _retained_scope(runtime, repository, pull_request)
    if retained is not None:
        return await _repo_test_unlocked(
            repository, pull_request, selectors, runtime, suite, include_output,
        )
    state = _state(runtime, repository, pull_request)
    lease = getattr(state, "checkout_lease", None)
    if not isinstance(lease, PRCheckoutLease) or not lease.is_active:
        return await _repo_test_unlocked(
            repository, pull_request, selectors, runtime, suite, include_output,
        )
    with hold_pr_checkout_lease(lease):
        return await _repo_test_unlocked(
            repository, pull_request, selectors, runtime, suite, include_output,
        )


def _remediation_test_guidance(code: str, *, scoped: bool) -> str:
    """Publication advice, not authorization or a replacement for CI checks."""
    publish = (
        "After scoped tests pass, commit, push the fix to the PR branch, and "
        "re-request review. In the PR comment state that the in-runner full suite "
        "did not complete (not failed) and CI is the validation surface. "
        "Do not wait for another contained full-suite run."
    )
    if scoped and code != "tests_passed":
        return "Hold publication: scoped tests must pass before pushing; this scoped run did not pass."
    # Overflow stops execution, so even a nonzero exit is incomplete evidence,
    # not an observed completed failing suite. Scoped runs remain blocked above.
    if code in {"test_timeout", "test_output_overflow", "tests_failed_output_overflow"}:
        return (
            "the contained runner did not complete the suite; this is not test evidence "
            "either way — push and rely on CI. " + publish
        )
    if code in {"test_snapshot_unavailable", "test_containment_unavailable"}:
        return "Infrastructure unavailable, not a failed suite. " + publish
    if code == "tests_failed":
        return (
            "Hold publication for a completed failing full-suite run with counts observed "
            "and failed_tests non-empty. If only the runner failed without test evidence, "
            "report the infrastructure failure, not a failed suite; " + publish
        )
    if code == "tests_passed":
        return (
            "Scoped tests must pass before pushing. Only a completed failing full-suite "
            "run with counts observed and failed_tests non-empty blocks publication; "
            "a full-suite timeout or unavailable snapshot/runner does not. "
            "Commit, push to the PR branch, and re-request review when these conditions hold."
        )
    return "Resolve this refusal; it does not authorize publication or bypass repository policy."


@tool
def repo_diff(
    repository: str,
    pull_request: int,
    mode: Literal["working", "staged", "base"] = "working",
    paths: tuple[str, ...] = (),
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read a bounded working, staged, or base diff from the bound checkout.

    On a retained Worklink remediation turn, ``repository`` is the retained
    run's repository and ``pull_request`` is the incident's Chainlink issue id;
    retained scopes do not support ``mode='base'``.
    """
    return _execute(runtime, repository, pull_request, GitDiff(mode, paths))


@tool
def repo_unmerged(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read unmerged-index records from the bound checkout."""
    return _execute(runtime, repository, pull_request, GitUnmerged())


@tool
def repo_stage(
    repository: str,
    pull_request: int,
    paths: tuple[str, ...],
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Stage only explicit repository-relative paths in the bound checkout.

    On a retained Worklink remediation turn, ``repository`` is the retained
    run's repository and ``pull_request`` is the incident's Chainlink issue id.
    """
    return _execute(runtime, repository, pull_request, GitStage(paths))


@tool
def repo_commit(
    repository: str,
    pull_request: int,
    paths: tuple[str, ...],
    message: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Stage explicit paths and commit them with server-owned Git identity.

    On a retained Worklink remediation turn, ``repository`` is the retained
    run's repository and ``pull_request`` is the incident's Chainlink issue id.
    """
    return _execute(runtime, repository, pull_request, GitCommit(paths, message))


@tool
def repo_merge(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """The default way to update a PR branch with its base in every PR-remediation scope.

    Merge the immutable observed base into the bound checkout. If it merges
    cleanly, the merge commit already exists; next call repo_push.
    On conflicts: repo_unmerged, resolve, repo_stage, then repo_commit listing
    every staged path (the merge stages auto-merged files too), then repo_push.
    """
    return _execute(runtime, repository, pull_request, GitMerge())


@tool
def repo_merge_abort(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Abort an in-progress merge in the bound checkout."""
    return _execute(runtime, repository, pull_request, GitMergeAbort())


@tool
def repo_rebase(
    repository: str,
    pull_request: int,
    base_property: str = "",
    base_verification: str = "",
    head_property: str = "",
    head_verification: str = "",
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Start a bound rebase, or continue it with two-sided preservation evidence.

    Available only on turns for history-rewrite event types in
    _HISTORY_REWRITE_EVENT_TYPES (mimir/repo_tools.py):
    `heartbeat_pr_maintenance`, `pr_changes_requested_stale`, `pr_ci_failure`,
    `pr_mergeability_rebase`, `pr_mergeability_conflicting`.
    In other PR-remediation scopes, use repo_merge to update the branch with its base.
    """
    return _execute(runtime, repository, pull_request, GitRebase(
        base_property, base_verification, head_property, head_verification,
    ))


@tool
def repo_rebase_abort(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Abort an in-progress rebase in the bound checkout."""
    return _execute(runtime, repository, pull_request, GitRebaseAbort())


@tool
def repo_revert(
    repository: str,
    pull_request: int,
    commit: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Revert one full commit ID proven to be within bound head minus base."""
    return _execute(runtime, repository, pull_request, GitRevert(commit))


@tool
def repo_revert_abort(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Abort an in-progress revert in the bound checkout."""
    return _execute(runtime, repository, pull_request, GitRevertAbort())


@tool
def repo_push(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Push HEAD to the one destination ref in the immutable PR scope."""
    return _execute(runtime, repository, pull_request, GitPush())


def _bind_injected_runtime(repo_tool: StructuredTool) -> StructuredTool:
    callable_ = repo_tool.func or repo_tool.coroutine
    if callable_ is None:
        raise RuntimeError(f"repository tool {repo_tool.name!r} has no callable")
    callable_.__annotations__["runtime"] = ToolRuntime
    repo_tool.args_schema = create_schema_from_function(
        repo_tool.name, callable_, filter_args=(), include_injected=True,
    )
    repo_tool.__dict__.pop("_injected_args_keys", None)
    return repo_tool


REPO_TOOLS = tuple(_bind_injected_runtime(repo_tool) for repo_tool in (
    repo_checkout,
    repo_cleanup,
    repo_fetch,
    repo_status,
    repo_test,
    repo_diff,
    repo_unmerged,
    repo_stage,
    repo_commit,
    repo_merge,
    repo_merge_abort,
    repo_rebase,
    repo_rebase_abort,
    repo_revert,
    repo_revert_abort,
    repo_push,
))

__all__ = [
    "PRCheckoutLease", "acquire_pr_checkout_lease", "create_pr_checkout_lease",
    "recover_pr_checkout_lease", "cleanup_pr_checkout_lease",
    "hold_pr_checkout_lease", "active_pr_checkout_lease_for_path",
    "reclaim_expired_pr_checkout_leases", "configured_pr_checkout_lease_root",
    "RepoGitTools", "RepoProjectTests", "GitRefusal", "ProjectTestRefusal",
    "GitFetch", "GitStatus", "GitDiff", "GitUnmerged", "GitStage",
    "GitCommit", "GitMerge", "GitMergeAbort", "GitRebase",
    "GitRebaseAbort", "GitRevert", "GitRevertAbort", "GitPush",
    "GitOperationResult", "ProjectTestResult", "REPO_TOOLS",
    "hardened_git_command", "retained_factory_git_runner",
    "retained_factory_snapshot_bundle", "retained_factory_subprocess_runner",
    "RepositoryConfig", "RepositoryInventory", "RepositoryTestSuite",
    "AllowedRootConfig", "valid_repository_slug", "ParityProbe",
    "LegacyObservation", "EffectEvidence", "AuditEvidence", "TypedProjection",
    "evaluate_typed_shadow", "run_parity",
    "offline_canary_probes", "write_parity_report", "was_agent_push",
    "was_verified_push", "was_superseded_by_own_push",
]
