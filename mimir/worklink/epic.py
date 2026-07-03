"""Epic-scoped Worklink orchestration.

The epic runner owns one integration branch for a Chainlink parent issue and
drives decomposed leaf slices into that branch. It deliberately does not reuse
``WorklinkRunner`` because the leaf runner opens one PR per leaf; epic mode must
observe/review/merge each slice and open exactly one draft PR at the end.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .backends import BackendRegistry, WorkOrder, WorklinkConfig
from .backends.registry import WORKLINK_MERGED_LABEL
from .claims import ChainlinkClaims, ClaimRecord
from .compute import ComputeLaunchError, ComputeResult
from .epic_state import (
    EpicRunManifest,
    EpicSliceRecord,
    load_epic_state,
    load_or_init_epic_state,
    resume_epic_run,
    save_epic_state,
)
from .evidence import (
    EvidenceValidation,
    backend_completed,
    fold_remote_test_evidence,
    observe_evidence,
    observe_remote_evidence,
)
from .orchestrator import (
    IssueContext,
    WorklinkError,
    _commit_worktree_changes,
    _git_push,
    _repo_remote_url,
    _parse_chainlink_datetime,
    _repo_slug,
    _runner_for_home,
    _run_remote_test_job,
    _create_backend_checkout,
    _template_path,
    _write_evidence,
    render_work_order,
    validate_leaf,
)
from .review import (
    DecomposeOutcome,
    IntegrationDecision,
    SliceDecision,
    classify_leaf_review_risk,
)
from .worker import gate_failure_detail
from .worktree import (
    IntegrationBranchLease,
    SliceMergeConflict,
    SliceMergeSuccess,
    WorktreeLease,
    create_integration_branch,
    create_slice_worktree,
    merge_slice_into_integration,
)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class LeafIssue:
    issue: IssueContext
    blocked_by: tuple[int, ...] = ()
    scope_paths: tuple[str, ...] = ()
    suggested_test_command: str | None = None


@dataclass(frozen=True)
class EpicRunResult:
    epic_id: int
    status: str
    pr_url: str | None = None
    manifest_path: Path | None = None
    blocked_leaves: tuple[int, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class EpicTestStatus:
    command: str
    exit_code: int
    summary: str


class EpicRoleRunner(Protocol):
    """Action-based epic roles: agents act via tools; the runner returns the
    recorded outcome/decision (see mimir.worklink.epic_roles)."""

    async def run_decompose(
        self, epic: IssueContext, *, chainlink: "ChainlinkEpicClient"
    ) -> DecomposeOutcome: ...

    async def review_slice(
        self,
        *,
        leaf: LeafIssue,
        evidence: EvidenceValidation,
        mode: str,
        reviewer_count: int,
        chainlink: "ChainlinkEpicClient",
    ) -> SliceDecision: ...

    async def validate_integration(
        self,
        *,
        epic: IssueContext,
        manifest: EpicRunManifest,
        partial: bool,
        blocked: Mapping[int, str],
        chainlink: "ChainlinkEpicClient",
    ) -> IntegrationDecision: ...


class MissingEpicRoleRunner:
    async def run_decompose(
        self, epic: IssueContext, *, chainlink: "ChainlinkEpicClient"
    ) -> DecomposeOutcome:
        raise WorklinkError("epic decompose role runner is not configured")

    async def review_slice(
        self,
        *,
        leaf: LeafIssue,
        evidence: EvidenceValidation,
        mode: str,
        reviewer_count: int,
        chainlink: "ChainlinkEpicClient",
    ) -> SliceDecision:
        raise WorklinkError("epic per-slice reviewer role runner is not configured")

    async def validate_integration(
        self,
        *,
        epic: IssueContext,
        manifest: EpicRunManifest,
        partial: bool,
        blocked: Mapping[int, str],
        chainlink: "ChainlinkEpicClient",
    ) -> IntegrationDecision:
        raise WorklinkError("epic integration-validator role runner is not configured")


class ChainlinkEpicClient:
    def __init__(self, *, chainlink_bin: str = "chainlink", runner: Runner) -> None:
        self.chainlink_bin = chainlink_bin
        self.runner = runner

    def _show_issue(self, issue_id: int) -> Mapping[str, Any]:
        result = self.runner([self.chainlink_bin, "issue", "show", str(issue_id), "--json"])
        if result.returncode != 0:
            raise WorklinkError((result.stderr or result.stdout).strip() or "chainlink issue show failed")
        payload = json.loads(result.stdout)
        if not isinstance(payload, Mapping):
            raise WorklinkError("chainlink issue show did not return an object")
        return payload

    def read_issue(self, issue_id: int) -> IssueContext:
        return _issue_from_payload(self._show_issue(issue_id), fallback_id=issue_id)

    def child_leaves(self, epic_id: int) -> list[LeafIssue]:
        result = self.runner([self.chainlink_bin, "issue", "list", "--json"])
        if result.returncode != 0:
            raise WorklinkError((result.stderr or result.stdout).strip() or "chainlink issue list failed")
        payload = json.loads(result.stdout)
        if not isinstance(payload, list):
            raise WorklinkError("chainlink issue list did not return a list")
        # `issue list --json` omits blocked_by and labels entirely; only `issue show`
        # carries them. Hydrate every child from show or the DAG flattens to one wave
        # and risk labels never reach the review classifier.
        child_ids: list[int] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            if item.get("parent_id") is None or item.get("id") is None:
                continue
            if int(item["parent_id"]) != epic_id:
                continue
            child_ids.append(int(item["id"]))
        leaves = []
        missing_keys: set[str] = set()
        for child_id in child_ids:
            detail = self._show_issue(child_id)
            missing_keys.update(key for key in ("blocked_by", "labels") if key not in detail)
            issue = _issue_from_payload(detail, fallback_id=child_id)
            scope_paths = tuple(_str_list(_first_present(detail, ("scope_paths", "scope", "paths"))))
            suggested_test_command = _optional_str(
                _first_present(detail, ("suggested_test_command", "test_command"))
            )
            leaves.append(
                LeafIssue(
                    issue=issue,
                    blocked_by=tuple(_int_list(_first_present(detail, ("blocked_by", "blockers", "blocked_by_ids")))),
                    scope_paths=scope_paths or tuple(_parse_scope_paths(issue.description)),
                    suggested_test_command=suggested_test_command
                    or _parse_suggested_test_command(issue.description),
                )
            )
        if missing_keys:
            print(
                "worklink epic: chainlink issue show payload missing "
                f"{sorted(missing_keys)}; dependency waves/risk labels may be wrong",
                file=sys.stderr,
            )
        return leaves

    def file_leaf(self, epic_id: int, leaf: Any) -> int:
        title = str(getattr(leaf, "title"))
        for existing in self.child_leaves(epic_id):
            if existing.issue.title == title:
                return existing.issue.issue_id
        body = _leaf_body(leaf)
        labels = list(getattr(leaf, "labels", []) or ["worklink:ready"])
        if getattr(leaf, "risk", "standard") == "high":
            normalized_labels = {str(label).strip().lower() for label in labels}
            if "risk:high" not in normalized_labels:
                labels.append("risk:high")
        cmd = [
            self.chainlink_bin,
            "issue",
            "subissue",
            str(epic_id),
            title,
            "--description",
            body,
            "--json",
        ]
        for label in labels:
            cmd.extend(["--label", str(label)])
        result = self.runner(cmd)
        if result.returncode != 0:
            raise WorklinkError((result.stderr or result.stdout).strip() or "chainlink issue subissue failed")
        return _created_issue_id(result.stdout)

    def add_blocker(self, blocked_leaf: int, blocker_leaf: int, reason: str) -> None:
        result = self.runner([
            self.chainlink_bin,
            "issue",
            "block",
            str(blocked_leaf),
            str(blocker_leaf),
        ])
        if result.returncode != 0:
            raise WorklinkError((result.stderr or result.stdout).strip() or "chainlink issue block failed")
        if reason:
            comment = self.runner([
                self.chainlink_bin,
                "issue",
                "comment",
                str(blocked_leaf),
                f"WORKLINK_BLOCKED_BY #{blocker_leaf}: {reason}",
            ])
            if comment.returncode != 0:
                raise WorklinkError((comment.stderr or comment.stdout).strip() or "chainlink issue comment failed")

    def mark_merged(self, leaf_id: int) -> None:
        self.runner([self.chainlink_bin, "issue", "unlabel", str(leaf_id), "worklink:review"])
        self.runner([self.chainlink_bin, "issue", "unlabel", str(leaf_id), "worklink:in-progress"])
        self.runner([self.chainlink_bin, "issue", "label", str(leaf_id), WORKLINK_MERGED_LABEL])

    def mark_blocked(self, leaf_id: int, reason: str) -> None:
        self.runner([self.chainlink_bin, "issue", "unlabel", str(leaf_id), "worklink:ready"])
        self.runner([self.chainlink_bin, "issue", "unlabel", str(leaf_id), "worklink:in-progress"])
        self.runner([self.chainlink_bin, "issue", "label", str(leaf_id), "worklink:blocked"])
        self.runner([self.chainlink_bin, "issue", "comment", str(leaf_id), f"WORKLINK_BLOCKED {reason}"])

    def comment(self, issue_id: int, text: str) -> None:
        """Post a plain comment (role tools use this for fixes/deficiency notes).

        Raises on failure so the tool closures' ``_safe_comment`` can surface a
        real audit-comment failure as a warning instead of dropping it silently
        (mirrors ``add_blocker``).
        """
        result = self.runner([self.chainlink_bin, "issue", "comment", str(issue_id), text])
        if result.returncode != 0:
            raise WorklinkError(
                (result.stderr or result.stdout).strip() or "chainlink issue comment failed"
            )

    def move_epic_to_review(self, epic_id: int) -> None:
        self.runner([self.chainlink_bin, "issue", "unlabel", str(epic_id), "worklink:ready"])
        self.runner([self.chainlink_bin, "issue", "unlabel", str(epic_id), "worklink:in-progress"])
        self.runner([self.chainlink_bin, "issue", "label", str(epic_id), "worklink:review"])

    def move_epic_to_blocked(self, epic_id: int) -> None:
        self.runner([self.chainlink_bin, "issue", "unlabel", str(epic_id), "worklink:ready"])
        self.runner([self.chainlink_bin, "issue", "unlabel", str(epic_id), "worklink:in-progress"])
        self.runner([self.chainlink_bin, "issue", "label", str(epic_id), "worklink:blocked"])

    def mark_epic_failed(self, epic_id: int, *, retryable: bool, reason: str) -> None:
        self.runner([self.chainlink_bin, "issue", "comment", str(epic_id), f"WORKLINK_EPIC_FAILED {reason}"])
        self.runner([self.chainlink_bin, "issue", "unlabel", str(epic_id), "worklink:in-progress"])
        self.runner([self.chainlink_bin, "issue", "unlabel", str(epic_id), "worklink:ready"])
        self.runner([self.chainlink_bin, "issue", "unlabel", str(epic_id), "worklink:blocked"])
        self.runner([
            self.chainlink_bin,
            "issue",
            "label",
            str(epic_id),
            "worklink:ready" if retryable else "worklink:blocked",
        ])


@dataclass(frozen=True)
class EpicRunner:
    home: Path
    repo: Path
    chainlink_bin: str = "chainlink"
    agent_id: str = "mimir-worklink-epic"
    runner: Runner | None = None
    registry: BackendRegistry | None = None
    roles: EpicRoleRunner | None = None
    chainlink: ChainlinkEpicClient | None = None
    _manifest_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False, compare=False)

    async def run(
        self,
        epic_id: int,
        *,
        backend_name: str | None = None,
        base_branch: str | None = None,
        autonomous: bool = False,
    ) -> EpicRunResult:
        runner = self.runner or _runner_for_home(self.home, self.chainlink_bin)
        chainlink = self.chainlink or ChainlinkEpicClient(chainlink_bin=self.chainlink_bin, runner=runner)
        if self.roles is None:
            from .epic_roles import EpicSubagentRoleRunner

            roles = EpicSubagentRoleRunner(home=self.home, repo=self.repo)
        else:
            roles = self.roles
        config = WorklinkConfig.load(self.home / "worklink.yaml")
        registry = self.registry or BackendRegistry(config)
        repo_url = _repo_remote_url(self.repo, runner=runner)
        repo_slug = _repo_slug(self.repo, runner=runner)
        base = base_branch or config.defaults.base_branch
        claims = ChainlinkClaims(
            chainlink_bin=self.chainlink_bin,
            agent_id=self.agent_id,
            runner=runner,
        )

        epic = chainlink.read_issue(epic_id)
        if "worklink:epic" not in epic.labels:
            raise WorklinkError("epic run requires the worklink:epic label")
        claim = claims.claim_issue(epic_id, epic.comments, max_active_locks=None)
        if not claim.claimed or claim.record is None:
            return EpicRunResult(epic_id, "failed", reason=claim.reason or "claim_failed")

        claim_record = claim.record
        heartbeat_task = asyncio.create_task(
            _epic_claim_heartbeat_loop(
                claims,
                lambda: claim_record,
                interval_s=_epic_heartbeat_interval_s(config),
            )
        )

        def heartbeat() -> None:
            nonlocal claim_record
            claim_record = claims.heartbeat_issue(claim_record)

        try:
            heartbeat()
            existing_manifest = load_epic_state(self.home, epic_id)
            created_manifest = existing_manifest is None
            if existing_manifest is None:
                integration = create_integration_branch(
                    self.repo,
                    epic_id=epic_id,
                    base_ref=base,
                    epic_branch_prefix=config.defaults.epic_branch_prefix,
                    base_fetch=config.defaults.base_fetch,
                    runner=runner,
                )
                _git_push(integration.path, integration.branch, runner=runner)
                manifest = load_or_init_epic_state(
                    self.home,
                    epic_id=epic_id,
                    integration_branch=integration.branch,
                    integration_worktree=integration.path,
                    base_ref=base,
                    phase="decompose",
                )
                heartbeat()
            else:
                manifest = existing_manifest
                if _manifest_is_terminal_blocked(manifest):
                    chainlink.move_epic_to_blocked(epic_id)
                    return EpicRunResult(
                        epic_id,
                        "blocked",
                        blocked_leaves=tuple(sorted(item.id for item in manifest.slices)),
                        manifest_path=self.home / "state" / "worklink" / "epics" / f"{epic_id}.json",
                        reason="all slices blocked",
                    )
                resume_point = resume_epic_run(manifest)
                if resume_point.complete:
                    return EpicRunResult(
                        epic_id,
                        manifest.status,
                        manifest_path=self.home / "state" / "worklink" / "epics" / f"{epic_id}.json",
                    )
                manifest = _recover_crashed_running_slices(
                    self.home,
                    manifest,
                    max_attempts=config.defaults.max_review_retries,
                    chainlink=chainlink,
                )
                integration = _ensure_integration_worktree(
                    self.repo, manifest, home=self.home, runner=runner
                )
                base = manifest.base_ref
                heartbeat()
            leaves = chainlink.child_leaves(epic_id)
            if manifest.phase == "decompose" and (not leaves or not created_manifest):
                leaves = await self._decompose(epic, chainlink, roles, config)
                manifest = await self._save_manifest_update(
                    manifest,
                    lambda current: replace(
                        current,
                        phase="build",
                        status="running",
                        slices=[EpicSliceRecord(id=leaf.issue.issue_id) for leaf in leaves],
                    ),
                )
                heartbeat()
            elif manifest.phase == "decompose":
                manifest = await self._save_manifest_update(
                    manifest,
                    lambda current: replace(
                        current,
                        phase="build",
                        slices=[EpicSliceRecord(id=leaf.issue.issue_id) for leaf in leaves],
                    ),
                )
                heartbeat()
            elif not leaves:
                raise WorklinkError("epic manifest is past decompose but has no child leaves")
            elif not manifest.slices:
                manifest = await self._save_manifest_update(
                    manifest,
                    lambda current: replace(
                        current,
                        phase="build",
                        slices=[EpicSliceRecord(id=leaf.issue.issue_id) for leaf in leaves],
                    ),
                )
                heartbeat()
            leaf_by_id = {leaf.issue.issue_id: leaf for leaf in leaves}
            waves = compute_waves(leaves)
            blocked: dict[int, str] = {
                item.id: "previously blocked" for item in manifest.slices if item.status == "blocked"
            }
            for wave in waves:
                runnable = [
                    leaf
                    for leaf in wave
                    if _slice(manifest, leaf.issue.issue_id).status not in {"merged", "blocked"}
                    and not any(blocker in blocked for blocker in leaf.blocked_by)
                ]
                skipped = [
                    leaf
                    for leaf in wave
                    if _slice(manifest, leaf.issue.issue_id).status not in {"merged", "blocked"}
                    and any(blocker in blocked for blocker in leaf.blocked_by)
                ]
                for leaf in skipped:
                    reason = "blocked by failed prerequisite"
                    blocked[leaf.issue.issue_id] = reason
                    chainlink.mark_blocked(leaf.issue.issue_id, reason)
                    manifest = await self._save_manifest_update(
                        manifest,
                        lambda current, leaf_id=leaf.issue.issue_id: _update_slice(
                            current,
                            leaf_id,
                            status="blocked",
                        ),
                    )
                    heartbeat()
                batches = _file_disjoint_batches(runnable)
                for batch in batches:
                    for chunk in _chunks(batch, config.defaults.max_concurrent):
                        tasks = [
                            self._build_review_merge_slice(
                                leaf=leaf,
                                epic=epic,
                                manifest=manifest,
                                integration=integration,
                                config=config,
                                registry=registry,
                                repo_url=repo_url,
                                repo_slug=repo_slug,
                                backend_name=backend_name,
                                roles=roles,
                                chainlink=chainlink,
                                runner=runner,
                                autonomous=autonomous,
                            )
                            for leaf in chunk
                        ]
                        for outcome in await asyncio.gather(*tasks):
                            manifest = load_or_init_epic_state(
                                self.home,
                                epic_id=epic_id,
                                integration_branch=integration.branch,
                                integration_worktree=integration.path,
                                base_ref=base,
                            )
                            if outcome.blocked_reason:
                                blocked[outcome.leaf_id] = outcome.blocked_reason
                                dependents = _dependents(leaf_by_id.values(), outcome.leaf_id)
                                for dependent in dependents:
                                    blocked[dependent.issue.issue_id] = "blocked by failed prerequisite"
                                    chainlink.mark_blocked(
                                        dependent.issue.issue_id, "blocked by failed prerequisite"
                                    )
                                manifest = await self._save_manifest_update(
                                    manifest,
                                    lambda current, blocked_ids=tuple(
                                        dependent.issue.issue_id for dependent in dependents
                                    ): _mark_slices_blocked(current, blocked_ids),
                                )
                                heartbeat()
            manifest = load_or_init_epic_state(
                self.home,
                epic_id=epic_id,
                integration_branch=integration.branch,
                integration_worktree=integration.path,
                base_ref=base,
            )
            partial = any(item.status == "blocked" for item in manifest.slices)
            manifest = await self._save_manifest_update(
                manifest,
                lambda current: replace(
                    current,
                    phase="integrate",
                    status="partial" if partial else "running",
                ),
            )
            heartbeat()
            integration_decision = await roles.validate_integration(
                epic=epic,
                manifest=manifest,
                partial=partial,
                blocked=blocked,
                chainlink=chainlink,
            )
            if not integration_decision.approved:
                manifest = await self._save_manifest_update(
                    manifest,
                    lambda current: replace(current, status="blocked"),
                )
                heartbeat()
                chainlink.move_epic_to_blocked(epic_id)
                return EpicRunResult(
                    epic_id,
                    "blocked",
                    blocked_leaves=tuple(blocked),
                    manifest_path=self.home / "state" / "worklink" / "epics" / f"{epic_id}.json",
                    reason=integration_decision.summary
                    or "; ".join(integration_decision.reasons)
                    or "integration validation blocked",
                )
            test_status = _run_epic_tests(integration.path, config.defaults.test_command, runner=runner)
            if test_status.exit_code != 0 and not partial:
                manifest = await self._save_manifest_update(
                    manifest,
                    lambda current: replace(current, status="blocked"),
                )
                heartbeat()
                chainlink.move_epic_to_blocked(epic_id)
                return EpicRunResult(
                    epic_id,
                    "blocked",
                    blocked_leaves=tuple(blocked),
                    manifest_path=self.home / "state" / "worklink" / "epics" / f"{epic_id}.json",
                    reason=f"epic tests failed: {test_status.summary}",
                )
            _git_push(integration.path, integration.branch, runner=runner)
            pr_url = _open_epic_pr(
                self.repo,
                epic,
                branch=integration.branch,
                base=base,
                manifest=manifest,
                partial=partial,
                blocked=blocked,
                decision=integration_decision,
                test_status=test_status,
                runner=runner,
            )
            manifest = await self._save_manifest_update(
                manifest,
                lambda current: replace(current, phase="pr", status="partial" if partial else "completed"),
            )
            heartbeat()
            chainlink.move_epic_to_review(epic_id)
            return EpicRunResult(
                epic_id,
                manifest.status,
                pr_url=pr_url,
                manifest_path=self.home / "state" / "worklink" / "epics" / f"{epic_id}.json",
                blocked_leaves=tuple(sorted(blocked)),
            )
        except Exception as exc:
            reason = _failure_reason(exc)
            retryable = claim_record.attempt < claims.max_attempts
            try:
                _persist_epic_crash_checkpoint(
                    self.home,
                    epic_id,
                    retryable=retryable,
                )
            except Exception:
                pass
            try:
                chainlink.mark_epic_failed(epic_id, retryable=retryable, reason=reason)
            except Exception:
                pass
            raise
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            claims.release_issue(epic_id)

    async def _decompose(
        self,
        epic: IssueContext,
        chainlink: ChainlinkEpicClient,
        roles: EpicRoleRunner,
        config: WorklinkConfig,
    ) -> list[LeafIssue]:
        """Run the action-based decomposer until child leaves exist in Chainlink.

        The decompose agent files leaves directly through epic-scoped tools; the
        gate is real state (children exist), not a parsed verdict. A reported
        brief deficiency (already commented on the epic by the agent's tool)
        stops the run instead of burning retries.
        """
        for _attempt in range(config.defaults.max_review_retries):
            outcome = await roles.run_decompose(epic, chainlink=chainlink)
            # Deficiency wins over any existing leaves: the tool closures make
            # file-vs-deficiency exclusive within one run, but leaves filed by
            # an earlier crashed attempt can coexist with a fresh deficiency
            # report — that mixed state must halt, not build a partial plan.
            if outcome.deficiency:
                raise WorklinkError(
                    f"epic brief reported deficient by work-decomposer: {outcome.deficiency}"
                )
            leaves = chainlink.child_leaves(epic.issue_id)
            if leaves:
                return leaves
        raise WorklinkError("work-decomposer filed no leaves")

    async def _save_manifest_update(
        self,
        fallback: EpicRunManifest,
        update: Callable[[EpicRunManifest], EpicRunManifest],
    ) -> EpicRunManifest:
        async with self._manifest_lock:
            current = load_epic_state(self.home, fallback.epic_id) or fallback
            updated = update(current)
            save_epic_state(self.home, updated)
            return updated

    async def _build_review_merge_slice(
        self,
        *,
        leaf: LeafIssue,
        epic: IssueContext,
        manifest: EpicRunManifest,
        integration: IntegrationBranchLease,
        config: WorklinkConfig,
        registry: BackendRegistry,
        repo_url: str | None,
        repo_slug: str | None,
        backend_name: str | None,
        roles: EpicRoleRunner,
        chainlink: ChainlinkEpicClient,
        runner: Runner,
        autonomous: bool,
    ) -> "_SliceOutcome":
        del epic
        validate_leaf(leaf.issue)
        selected_backend = registry.get(backend_name) if backend_name else registry.select(
            labels=leaf.issue.labels,
            repo=repo_slug,
        )
        compute = registry.select_compute(labels=leaf.issue.labels, repo=repo_slug)
        if autonomous:
            allowed, reason = config.autonomous_compute_allowed(compute.name, compute.capabilities())
            if not allowed:
                return _SliceOutcome(leaf.issue.issue_id, reason or "autonomous compute refused")
        # chainlink #820: the gate is ALWAYS the operator-declared test_command —
        # the environment-known-good invocation. The planner-written
        # suggested_test_command is advisory prompt context only (matching the
        # per-leaf runner and the work-order template); executing it as the gate
        # let a bare `pytest ...` suggestion fail every attempt with exit 127
        # ("pytest: not found") across epic #783 runs 9-11.
        test_cmd = config.defaults.test_command
        attempts = _slice(manifest, leaf.issue.issue_id).attempts
        last_reason = "review rejected"
        pending_fixes: tuple[str, ...] = ()
        while attempts < config.defaults.max_review_retries:
            attempts += 1
            manifest = await self._save_manifest_update(
                manifest,
                lambda current: _update_slice(
                    current,
                    leaf.issue.issue_id,
                    status="running",
                    attempts=attempts,
                ),
            )
            lease = _create_slice_checkout(
                self.repo,
                leaf=leaf,
                attempt=attempts,
                integration_branch=integration.branch,
                backend_name=selected_backend.name,
                compute_shared_filesystem=compute.capabilities().shared_filesystem,
                runner=runner,
            )
            work_prompt = render_work_order(
                leaf.issue,
                template_path=_template_path(self.home),
                backend_name=selected_backend.name,
                test_command=test_cmd,
            )
            if pending_fixes:
                work_prompt += (
                    "\n\nReviewer feedback from the previous attempt — address ALL of these:\n"
                    + "\n".join(f"- {fix}" for fix in pending_fixes)
                )
            order = WorkOrder(
                issue_id=leaf.issue.issue_id,
                worktree=lease.path,
                prompt=work_prompt,
                rules=None,
                timeout_s=config.defaults.timeout_s,
                env={"MIMIR_HOME": str(self.home)},
                transcript_root=self.home / "state" / "worklink" / "transcripts",
            )
            started = datetime.now(UTC)
            spec = selected_backend.work_spec(
                order,
                attempt=attempts,
                repo_url=repo_url or "",
                base_ref=lease.base_ref,
                branch=lease.branch,
                test_command=test_cmd,
            )
            spec = replace(spec, gate_repair_rounds=config.defaults.gate_repair_rounds)
            # chainlink #823: a prior run may have already built and pushed this
            # exact attempt branch (e.g. it died between push and review).
            # Adoption skips the COMPUTE only — the branch goes through the
            # identical observe/re-derive/remote-test/review path below, so the
            # trust model is unchanged.
            adopted = (
                not pending_fixes
                and not compute.capabilities().shared_filesystem
                and _adopt_slice_branch(self.repo, lease=lease, runner=runner)
            )
            if adopted:
                compute_result = ComputeResult(0, "adopted pushed slice branch", "")
                raw = _AdoptedRaw()
            else:
                try:
                    handle = await compute.launch(spec)
                    try:
                        compute_result = await compute.wait(handle, spec.timeout_s)
                    finally:
                        await compute.cleanup(handle)
                except ComputeLaunchError as exc:
                    compute_result = ComputeResult(-1, "", str(exc), launch_error=str(exc))
                raw = await selected_backend.interpret(order, compute_result)
            no_push_reason = None
            if not adopted and not compute.capabilities().shared_filesystem and backend_completed(raw.backend_status):
                if _slice_branch_has_changes(lease.path, lease.local_base or lease.base_ref, runner=runner):
                    _git_push(lease.path, lease.branch, runner=runner)
                else:
                    no_push_reason = "backend_produced_no_changes"
                    raw = replace(raw, backend_status="failed", error="backend produced no changes")
            validation = await _observe_slice(
                home=self.home,
                leaf=leaf,
                backend_name=selected_backend.name,
                compute=compute,
                spec=spec,
                lease=lease,
                started=started,
                raw=raw,
                test_cmd=test_cmd,
                config=config,
                runner=runner,
            )
            if no_push_reason:
                validation = _with_validation_reason(
                    validation,
                    no_push_reason,
                    blocked_reason="backend produced no changes",
                )
            evidence_path = _write_evidence(self.home, validation.evidence)
            if validation.review_ready:
                mode = classify_leaf_review_risk(
                    scope_paths=list(leaf.scope_paths),
                    labels=leaf.issue.labels,
                    tiered_review=config.defaults.tiered_review,
                )
                reviewer_count = (
                    config.defaults.tiered_review.multi_vote_reviewer_count
                    if mode == "multi"
                    else 1
                )
                decision = await roles.review_slice(
                    leaf=leaf,
                    evidence=validation,
                    mode=mode,
                    reviewer_count=reviewer_count,
                    chainlink=chainlink,
                )
                review_ref = decision.summary or ("approved" if decision.approved else "fixes requested")
            else:
                reasons = validation.reasons or ("evidence_not_review_ready",)
                fixes = list(reasons)
                # chainlink #815: gate-test output only survives the container via
                # stdout (worker's local gate) or the folded remote-test evidence
                # (controller's trusted gate); surface it so the next attempt fixes
                # the actual failures instead of retrying blind on "tests_failed".
                gate_tail = gate_failure_detail(validation, compute_result.stdout)
                if gate_tail:
                    fixes.append(
                        "the previous attempt FAILED the full test gate — fix these failures:\n"
                        + gate_tail
                    )
                decision = SliceDecision(
                    approved=False,
                    summary="review skipped: evidence not review-ready",
                    fixes=tuple(fixes),
                )
                review_ref = "review skipped: " + ", ".join(reasons)
            manifest = await self._save_manifest_update(
                manifest,
                lambda current: _update_slice(
                    current,
                    leaf.issue.issue_id,
                    status="review",
                    attempts=attempts,
                    evidence_ref=str(evidence_path),
                    review_ref=review_ref,
                ),
            )
            if validation.review_ready and decision.approved:
                if compute.capabilities().shared_filesystem:
                    _commit_worktree_changes(lease.path, leaf.issue, runner=runner)
                    _git_push(lease.path, lease.branch, runner=runner)
                merged = merge_slice_into_integration(
                    self.repo,
                    slice_branch=lease.branch,
                    integration_branch=integration.branch,
                    runner=runner,
                )
                if isinstance(merged, SliceMergeConflict):
                    manifest = await self._save_manifest_update(
                        manifest,
                        lambda current: replace(current, status="needs-human"),
                    )
                    raise WorklinkError(
                        "same-wave merge conflict requires human/decomposition review"
                    )
                assert isinstance(merged, SliceMergeSuccess)
                _git_push(integration.path, integration.branch, runner=runner)
                chainlink.mark_merged(leaf.issue.issue_id)
                manifest = await self._save_manifest_update(
                    manifest,
                    lambda current: _update_slice(
                        current,
                        leaf.issue.issue_id,
                        status="merged",
                        merge_commit=merged.merge_commit,
                    ),
                )
                return _SliceOutcome(leaf.issue.issue_id)
            pending_fixes = decision.fixes
            last_reason = "; ".join(decision.fixes) or ", ".join(validation.reasons) or last_reason
            backoff_s = _transient_provider_backoff_s(
                raw=raw,
                compute_result=compute_result,
                started=started,
                timeout_s=config.defaults.timeout_s,
            )
            if attempts < config.defaults.max_review_retries and backoff_s > 0:
                await asyncio.sleep(backoff_s)
        chainlink.mark_blocked(leaf.issue.issue_id, last_reason)
        manifest = await self._save_manifest_update(
            manifest,
            lambda current: _update_slice(
                current,
                leaf.issue.issue_id,
                status="blocked",
                attempts=attempts,
            ),
        )
        return _SliceOutcome(leaf.issue.issue_id, last_reason)


@dataclass(frozen=True)
class _SliceOutcome:
    leaf_id: int
    blocked_reason: str | None = None


def compute_waves(leaves: Iterable[LeafIssue]) -> list[list[LeafIssue]]:
    pending = {leaf.issue.issue_id: leaf for leaf in leaves}
    blockers = {leaf.issue.issue_id: set(leaf.blocked_by) for leaf in pending.values()}
    waves: list[list[LeafIssue]] = []
    merged: set[int] = set()
    while pending:
        ready_ids = sorted(
            leaf_id
            for leaf_id, deps in blockers.items()
            if leaf_id in pending and deps <= merged
        )
        if not ready_ids:
            raise WorklinkError("epic blocked-by graph contains a cycle")
        waves.append([pending.pop(leaf_id) for leaf_id in ready_ids])
        merged.update(ready_ids)
    return waves


async def _observe_slice(
    *,
    home: Path,
    leaf: LeafIssue,
    backend_name: str,
    compute: Any,
    spec: Any,
    lease: WorktreeLease,
    started: datetime,
    raw: Any,
    test_cmd: str,
    config: WorklinkConfig,
    runner: Runner,
) -> EvidenceValidation:
    if compute.capabilities().shared_filesystem:
        return observe_evidence(
            issue=leaf.issue.issue_id,
            attempt=spec.attempt,
            backend=backend_name,
            branch=lease.branch,
            worktree=lease.path,
            started_at=started,
            base_ref=lease.local_base or lease.base_ref,
            backend_status=raw.backend_status,
            test_command=test_cmd,
            transcript=str(raw.transcript_path) if raw.transcript_path else None,
            blocked_reason=raw.blocked_reason,
            runner=runner,
        )
    validation = observe_remote_evidence(
        issue=leaf.issue.issue_id,
        attempt=spec.attempt,
        backend=backend_name,
        branch=lease.branch,
        worktree=lease.path,
        started_at=started,
        base_ref=lease.base_ref,
        backend_status=raw.backend_status,
        test_command=test_cmd,
        transcript=str(raw.transcript_path) if raw.transcript_path else None,
        blocked_reason=raw.blocked_reason,
        runner=runner,
    )
    if test_cmd and backend_completed(raw.backend_status) and validation.evidence.files_changed:
        test_outcome = await _run_remote_test_job(compute, spec, timeout_s=config.defaults.timeout_s, claims=_NoopClaims(), claim_record=_NoopClaim(leaf.issue.issue_id, spec.attempt))
        if test_outcome.exit_code is not None:
            validation = fold_remote_test_evidence(
                validation,
                test_cmd,
                test_outcome.exit_code,
                backend_status=raw.backend_status,
                failure_tail=test_outcome.failure_tail,
            )
    return validation


def _create_slice_checkout(
    repo: Path,
    *,
    leaf: LeafIssue,
    attempt: int,
    integration_branch: str,
    backend_name: str,
    compute_shared_filesystem: bool,
    runner: Runner,
) -> WorktreeLease:
    if backend_name == "codex" and compute_shared_filesystem:
        return _create_backend_checkout(
            repo,
            issue_id=leaf.issue.issue_id,
            attempt=attempt,
            base=integration_branch,
            backend_name=backend_name,
            compute_shared_filesystem=compute_shared_filesystem,
            base_fetch=False,
            runner=runner,
        )
    return create_slice_worktree(
        repo,
        slice_id=leaf.issue.issue_id,
        integration_branch=integration_branch,
        runner=runner,
    )


def _file_disjoint_batches(leaves: list[LeafIssue]) -> list[list[LeafIssue]]:
    batches: list[list[LeafIssue]] = []
    for leaf in leaves:
        paths = set(leaf.scope_paths)
        for batch in batches:
            used = {path for item in batch for path in item.scope_paths}
            if paths.isdisjoint(used):
                batch.append(leaf)
                break
        else:
            batches.append([leaf])
    return batches


def _chunks(items: list[LeafIssue], size: int) -> Iterable[list[LeafIssue]]:
    size = max(1, size)
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _epic_heartbeat_interval_s(config: WorklinkConfig) -> float:
    return float(max(30, min(300, config.defaults.timeout_s // 4)))


@dataclass(frozen=True)
class _AdoptedRaw:
    """RawResult stand-in for an adopted, already-pushed slice branch (#823)."""

    backend_status: str = "completed"
    transcript_path: Path | None = None
    blocked_reason: str | None = None


def _adopt_slice_branch(repo: Path, *, lease: WorktreeLease, runner: Runner) -> bool:
    """Adopt an already-pushed attempt branch built on the CURRENT integration
    base — a prior run pushed it but died before review (#823). On success the
    LOCAL lease branch is hard-reset to the fetched remote ref: the later
    ``merge_slice_into_integration`` merges the local branch, so without the
    reset the adopted work would be silently dropped (review catch on PR
    #1014)."""
    listed = runner(["git", "-C", str(repo), "ls-remote", "--heads", "origin", lease.branch])
    if listed.returncode != 0 or not listed.stdout.strip():
        return False
    fetched = runner(["git", "-C", str(repo), "fetch", "origin", lease.branch])
    if fetched.returncode != 0:
        return False
    base = lease.local_base or lease.base_ref
    ancestor = runner(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", base, f"origin/{lease.branch}"]
    )
    if ancestor.returncode != 0:
        return False
    reset = runner(
        ["git", "-C", str(lease.path), "reset", "--hard", f"origin/{lease.branch}"]
    )
    return reset.returncode == 0


def _slice_branch_has_changes(worktree: Path, base_ref: str, *, runner: Runner) -> bool:
    diff = runner(["git", "-C", str(worktree), "diff", "--name-only", f"{base_ref}...HEAD"])
    if diff.returncode != 0:
        raise WorklinkError((diff.stderr or diff.stdout).strip() or "git diff failed")
    return bool(diff.stdout.strip())


def _with_validation_reason(
    validation: EvidenceValidation,
    reason: str,
    *,
    blocked_reason: str | None = None,
) -> EvidenceValidation:
    evidence = validation.evidence
    if blocked_reason:
        evidence = replace(evidence, blocked_reason=blocked_reason)
    if reason in validation.reasons:
        return replace(validation, evidence=evidence)
    return replace(validation, reasons=(*validation.reasons, reason), evidence=evidence)


def _transient_provider_backoff_s(
    *,
    raw: Any,
    compute_result: ComputeResult,
    started: datetime,
    timeout_s: int,
) -> float:
    text = " ".join(
        str(part or "")
        for part in (
            getattr(raw, "backend_status", ""),
            getattr(raw, "error", ""),
            compute_result.launch_error,
            compute_result.stdout,
            compute_result.stderr,
        )
    ).lower()
    if not _looks_like_transient_provider_error(text):
        return 0.0
    remaining = float(timeout_s) - (datetime.now(UTC) - started).total_seconds()
    if remaining <= 0:
        return 0.0
    return max(0.0, min(60.0, remaining))


def _looks_like_transient_provider_error(text: str) -> bool:
    patterns = (
        "server overloaded",
        "servers are currently overloaded",
        "currently overloaded",
        "overloaded",
        "rate limit",
        "rate-limit",
        "rate_limited",
        "too many requests",
        "429",
        "temporarily unavailable",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "internal server error",
        "5xx",
    )
    return any(pattern in text for pattern in patterns) or re.search(r"\b(?:429|5\d\d)\b", text) is not None


async def _epic_claim_heartbeat_loop(
    claims: ChainlinkClaims,
    current_record: Callable[[], ClaimRecord],
    *,
    interval_s: float,
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        claims.heartbeat_issue(current_record())


def _update_slice(manifest: EpicRunManifest, leaf_id: int, **changes: Any) -> EpicRunManifest:
    records = []
    found = False
    for record in manifest.slices:
        if record.id == leaf_id:
            records.append(replace(record, **changes))
            found = True
        else:
            records.append(record)
    if not found:
        records.append(EpicSliceRecord(id=leaf_id, **changes))
    return replace(manifest, slices=records)


def _mark_slices_blocked(manifest: EpicRunManifest, leaf_ids: Iterable[int]) -> EpicRunManifest:
    updated = manifest
    for leaf_id in leaf_ids:
        updated = _update_slice(updated, leaf_id, status="blocked")
    return updated


def _current_manifest(home: Path, fallback: EpicRunManifest) -> EpicRunManifest:
    return load_epic_state(home, fallback.epic_id) or fallback


def _recover_crashed_running_slices(
    home: Path,
    manifest: EpicRunManifest,
    *,
    max_attempts: int,
    chainlink: ChainlinkEpicClient,
) -> EpicRunManifest:
    records: list[EpicSliceRecord] = []
    changed = False
    for record in manifest.slices:
        if record.status != "running":
            records.append(record)
            continue
        changed = True
        next_attempt_floor = max_attempts - 1 if record.attempts >= max_attempts else record.attempts - 1
        records.append(
            replace(
                record,
                status="pending",
                attempts=max(0, next_attempt_floor),
                evidence_ref=None,
                review_ref=None,
            )
        )
        try:
            chainlink.comment(
                record.id,
                "WORKLINK_EPIC_FAILED crashed running slice re-armed for retry",
            )
        except Exception:
            pass
    if not changed:
        return manifest
    recovered = replace(manifest, slices=records, status="running")
    save_epic_state(home, recovered)
    return recovered


def _persist_epic_crash_checkpoint(home: Path, epic_id: int, *, retryable: bool) -> None:
    manifest = load_epic_state(home, epic_id)
    if manifest is None:
        return
    records: list[EpicSliceRecord] = []
    for record in manifest.slices:
        if record.status == "running":
            records.append(
                replace(
                    record,
                    status="pending" if retryable else "blocked",
                    attempts=max(0, record.attempts - 1) if retryable else record.attempts,
                    evidence_ref=None,
                    review_ref=None,
                )
            )
        else:
            records.append(record)
    save_epic_state(
        home,
        replace(
            manifest,
            status="running" if retryable else "needs-human",
            slices=records,
        ),
    )


def _failure_reason(exc: Exception) -> str:
    text = str(exc).strip()
    return (text or exc.__class__.__name__)[:1000]


def _manifest_is_terminal_blocked(manifest: EpicRunManifest) -> bool:
    return bool(
        manifest.status == "blocked"
        and manifest.slices
        and all(record.status == "blocked" for record in manifest.slices)
    )


def _slice(manifest: EpicRunManifest, leaf_id: int) -> EpicSliceRecord:
    for record in manifest.slices:
        if record.id == leaf_id:
            return record
    return EpicSliceRecord(id=leaf_id)


def _dependents(leaves: Iterable[LeafIssue], blocker_id: int) -> list[LeafIssue]:
    return [leaf for leaf in leaves if blocker_id in leaf.blocked_by]


def _ensure_integration_worktree(
    repo: Path, manifest: EpicRunManifest, *, runner: Runner, home: Path | None = None
) -> IntegrationBranchLease:
    path = Path(manifest.integration_worktree)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        start_point = _last_merge_commit(manifest) or manifest.integration_branch
        result = runner([
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            str(path),
            start_point,
        ])
        if result.returncode != 0:
            raise WorklinkError(
                (result.stderr or result.stdout).strip()
                or f"epic integration worktree is missing and could not be recreated: {path}"
            )
        last_merge_commit = _last_merge_commit(manifest)
        if last_merge_commit:
            checkout = runner([
                "git",
                "-C",
                str(path),
                "checkout",
                "-B",
                manifest.integration_branch,
                last_merge_commit,
            ])
            if checkout.returncode != 0:
                raise WorklinkError(
                    (checkout.stderr or checkout.stdout).strip()
                    or "git checkout integration branch failed"
                )
    head = runner(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"])
    if head.returncode != 0:
        raise WorklinkError(
            (head.stderr or head.stdout).strip()
            or "epic integration worktree is not a git checkout"
        )
    expected = _last_merge_commit(manifest)
    if expected and head.stdout.strip() != expected:
        reset = runner(["git", "-C", str(path), "reset", "--hard", expected])
        if reset.returncode != 0:
            raise WorklinkError(
                (reset.stderr or reset.stdout).strip()
                or "failed to reset integration worktree to manifest merge commit"
            )
    updated_manifest = _refresh_stale_integration_base(repo, path, manifest, runner=runner)
    if updated_manifest != manifest and home is not None:
        save_epic_state(home, updated_manifest)
        manifest = updated_manifest
    return IntegrationBranchLease(
        epic_id=manifest.epic_id,
        repo=repo,
        path=path,
        branch=manifest.integration_branch,
        base_ref=manifest.base_ref,
        local_base=manifest.base_ref,
    )


def _refresh_stale_integration_base(
    repo: Path,
    path: Path,
    manifest: EpicRunManifest,
    *,
    runner: Runner,
) -> EpicRunManifest:
    remote_base = _origin_base_ref(manifest.base_ref)
    _fetch_origin_base(repo, manifest.base_ref, runner=runner)
    base_rev = runner(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", remote_base])
    if base_rev.returncode != 0 or not base_rev.stdout.strip():
        return manifest
    ancestor = runner(["git", "-C", str(path), "merge-base", "--is-ancestor", remote_base, "HEAD"])
    if ancestor.returncode == 0:
        return manifest
    if ancestor.returncode not in (1,):
        raise WorklinkError(
            (ancestor.stderr or ancestor.stdout).strip()
            or f"failed to validate integration branch base against {remote_base}"
        )
    if _last_merge_commit(manifest) is None:
        checkout = runner(["git", "-C", str(path), "checkout", "-B", manifest.integration_branch, remote_base])
        if checkout.returncode != 0:
            raise WorklinkError(
                (checkout.stderr or checkout.stdout).strip()
                or f"failed to recreate stale integration branch from {remote_base}"
            )
        _force_push_branch(path, manifest.integration_branch, runner=runner)
        return manifest

    old_base = runner(["git", "-C", str(path), "merge-base", "HEAD", remote_base])
    if old_base.returncode != 0 or not old_base.stdout.strip():
        raise WorklinkError(
            (old_base.stderr or old_base.stdout).strip()
            or f"failed to find old integration branch base for {remote_base}"
        )
    rebase = runner([
        "git",
        "-C",
        str(path),
        "rebase",
        "--rebase-merges",
        "--onto",
        remote_base,
        old_base.stdout.strip(),
    ])
    if rebase.returncode != 0:
        runner(["git", "-C", str(path), "rebase", "--abort"])
        raise WorklinkError(
            (rebase.stderr or rebase.stdout).strip()
            or f"failed to rebase integration branch onto {remote_base}"
        )
    head = runner(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"])
    if head.returncode != 0 or not head.stdout.strip():
        raise WorklinkError(
            (head.stderr or head.stdout).strip()
            or "failed to read rebased integration branch HEAD"
        )
    _force_push_branch(path, manifest.integration_branch, runner=runner)
    return _replace_last_merge_commit(manifest, head.stdout.strip())


def _origin_base_ref(base_ref: str) -> str:
    return base_ref if base_ref.startswith("origin/") else f"origin/{base_ref}"


def _fetch_origin_base(repo: Path, base_ref: str, *, runner: Runner) -> None:
    if base_ref.startswith("origin/"):
        branch = base_ref.removeprefix("origin/")
    else:
        branch = base_ref
    if branch:
        runner(["git", "-C", str(repo), "fetch", "origin", branch])


def _force_push_branch(path: Path, branch: str, *, runner: Runner) -> None:
    result = runner(["git", "-C", str(path), "push", "-u", "--force-with-lease", "origin", branch])
    if result.returncode != 0:
        raise WorklinkError((result.stderr or result.stdout).strip() or "git push failed")


def _replace_last_merge_commit(manifest: EpicRunManifest, merge_commit: str) -> EpicRunManifest:
    records = list(manifest.slices)
    for index in range(len(records) - 1, -1, -1):
        if records[index].merge_commit:
            records[index] = replace(records[index], merge_commit=merge_commit)
            return replace(manifest, slices=records)
    return manifest


def _last_merge_commit(manifest: EpicRunManifest) -> str | None:
    for record in reversed(manifest.slices):
        if record.merge_commit:
            return record.merge_commit
    return None


def _run_epic_tests(worktree: Path, test_command: str, *, runner: Runner) -> EpicTestStatus:
    result = runner(test_command, cwd=worktree)
    summary = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
    return EpicTestStatus(test_command, result.returncode, summary[:1000])


def _open_epic_pr(
    repo: Path,
    epic: IssueContext,
    *,
    branch: str,
    base: str,
    manifest: EpicRunManifest,
    partial: bool,
    blocked: Mapping[int, str],
    decision: IntegrationDecision,
    test_status: EpicTestStatus,
    runner: Runner,
) -> str:
    body = [
        f"Closes chainlink #{epic.issue_id}.",
        "",
        "Worklink integrated epic:",
        f"- Base: `{base}`",
        f"- Branch: `{branch}`",
        f"- Merged slices: {', '.join('#' + str(s.id) for s in manifest.slices if s.status == 'merged') or '(none)'}",
        f"- Integration validation: APPROVED - {decision.summary or '(no summary)'}",
        f"- Epic tests: `{test_status.command}` → {test_status.exit_code}",
    ]
    if partial:
        body.append("- Epic status: partial")
        if test_status.exit_code != 0:
            body.append(f"- Partial-run test status: {test_status.summary}")
        for leaf_id, reason in sorted(blocked.items()):
            body.append(f"- Blocked leaf #{leaf_id}: {reason}")
    command = ["gh", "pr", "create", "--draft", "--base", base, "--head", branch]
    repo_slug = _repo_slug(repo, runner=runner)
    if repo_slug:
        command.extend(["--repo", repo_slug])
    command.extend(["--title", f"Worklink epic #{epic.issue_id}: {epic.title}", "--body", "\n".join(body) + "\n"])
    result = runner(command)
    if result.returncode != 0:
        raise WorklinkError((result.stderr or result.stdout).strip() or "gh pr create failed")
    return result.stdout.strip().splitlines()[-1]


def _issue_from_payload(payload: Mapping[str, Any], *, fallback_id: int | None = None) -> IssueContext:
    return IssueContext(
        issue_id=int(payload.get("id") or fallback_id or 0),
        title=str(payload.get("title") or ""),
        description=str(payload.get("description") or payload.get("body") or ""),
        labels={str(label) for label in payload.get("labels") or ()},
        parent_id=int(payload["parent_id"]) if payload.get("parent_id") is not None else None,
        comments=tuple(_comment_text(item) for item in payload.get("comments") or ()),
        created_at=_parse_chainlink_datetime(payload.get("created_at")),
    )


def _comment_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        # chainlink stores comment text under "content" (see
        # ChainlinkClaims._issue_comments); missing it here fed the #822
        # duplicate-run guard an empty comment list, so every duplicate saw
        # zero claim records, treated the live run as dead, and stole its
        # lock — the guard's first live test failed on THIS parse, not its
        # own logic. It also pinned next_attempt at 1 forever.
        return str(item.get("content") or item.get("body") or item.get("text") or "")
    return ""


def _first_present(data: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        return [int(item) for item in value]
    return [int(value)]


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _leaf_body(leaf: Any) -> str:
    scope = ", ".join(str(item) for item in leaf.scope_paths)
    out_of_scope = ", ".join(str(item) for item in getattr(leaf, "out_of_scope", []) or ["(none)"])
    return (
        "Acceptance criteria:\n"
        + "\n".join(f"- [ ] {item}" for item in leaf.acceptance_criteria)
        + "\n\nReview criteria:\n"
        + "\n".join(f"- {item}" for item in leaf.review_criteria)
        + "\n\nWorklink notes:\n"
        + f"- Scope: {scope}\n"
        + f"- Out of scope: {out_of_scope}\n"
        + f"- Suggested test command: {leaf.suggested_test_command}\n"
    )


def _parse_scope_paths(description: str) -> list[str]:
    match = re.search(
        r"(?ims)^-\s*Scope:\s*(?P<body>.*?)(?:^-\s*Out of scope:|^-\s*Suggested test command:|\Z)",
        description,
    )
    if not match:
        return []
    body = match.group("body").strip()
    paths: list[str] = []
    for line in body.splitlines() or [body]:
        cleaned = line.strip().removeprefix("-").strip()
        for part in cleaned.split(","):
            path = part.strip()
            if path:
                paths.append(path)
    return paths


def _parse_suggested_test_command(description: str) -> str | None:
    match = re.search(r"(?im)^-\s*Suggested test command:\s*(?P<cmd>.+)$", description)
    return match.group("cmd").strip() if match else None


def _created_issue_id(text: str) -> int:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, Mapping):
        return int(data["id"])
    if isinstance(data, list) and data and isinstance(data[0], Mapping):
        return int(data[0]["id"])
    for pattern in (
        r"created\s+(?:issue|subissue)\s+#(?P<id>\d+)",
        r"(?:issue|subissue)\s+#(?P<id>\d+)\s+created",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group("id"))
    raise WorklinkError("chainlink subissue output did not include a deterministic issue id")


class _NoopClaim:
    def __init__(self, issue_id: int, attempt: int) -> None:
        self.issue_id = issue_id
        self.attempt = attempt


class _NoopClaims:
    def heartbeat_issue(self, record: object) -> object:
        return record


def run_epic(
    *,
    home: Path,
    repo: Path,
    epic_id: int,
    backend: str | None = None,
    base_branch: str | None = None,
    autonomous: bool = False,
) -> EpicRunResult:
    from .epic_roles import EpicSubagentRoleRunner

    return asyncio.run(
        EpicRunner(
            home=home,
            repo=repo,
            roles=EpicSubagentRoleRunner(home=home, repo=repo),
        ).run(
            epic_id,
            backend_name=backend,
            base_branch=base_branch,
            autonomous=autonomous,
        )
    )
