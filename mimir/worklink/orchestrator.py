"""Worklink operator-run orchestrator.

The orchestrator owns deterministic state transitions around an untrusted tool
backend: validate the Chainlink leaf, claim it, create an attempt worktree,
render the work order, run the backend, observe evidence ourselves, push/open a
PR only after the evidence gate passes, then clean up and release the lock.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
import warnings
from typing import Any, Callable, Mapping, Sequence

from .backends import BackendRegistry, WorkOrder, WorklinkConfig
from .compute import ComputeLaunchError, ComputeResult, LaunchHandle
from .claims import ChainlinkClaims, ClaimRecord
from .evidence import (
    EvidenceValidation,
    WorklinkEvidence,
    backend_completed,
    fold_remote_test_evidence,
    observe_evidence,
    observe_remote_evidence,
)
from .planning import (
    missing_leaf_template_parts,
    render_decompose_prompt,
    uses_strict_leaf_validation,
)
from .run_state import (
    WorklinkRunState,
    clear_run_state,
    load_run_state,
    save_run_state,
)
from .worker import extract_gate_test_tail
from .worktree import WorktreeLease, cleanup_worktree, create_isolated_checkout, create_worktree

Runner = Callable[..., subprocess.CompletedProcess[str]]
_CLAIM_HEARTBEAT_INTERVAL_S = 60.0


@dataclass(frozen=True)
class IssueContext:
    issue_id: int
    title: str
    description: str
    labels: set[str]
    parent_id: int | None = None
    comments: tuple[str, ...] = ()
    created_at: datetime | None = None


@dataclass(frozen=True)
class WorklinkRunResult:
    issue_id: int
    attempt: int | None
    status: str
    review_ready: bool = False
    pr_url: str | None = None
    evidence_path: Path | None = None
    worktree: Path | None = None
    branch: str | None = None
    dry_run: bool = False
    reason: str | None = None


class WorklinkError(RuntimeError):
    """Base error for operator-facing Worklink failures."""


class LeafValidationError(WorklinkError):
    """Issue is not structured enough to hand to a backend."""


def _heartbeat_claim_best_effort(claims: ChainlinkClaims, record: ClaimRecord) -> None:
    try:
        claims.heartbeat_issue(record)
    except Exception as exc:  # noqa: BLE001 - heartbeat loss must not fail the run.
        _log_event(
            "worklink_claim_heartbeat_failed",
            issue_id=record.issue_id,
            attempt=record.attempt,
            error=str(exc)[:300],
        )


async def _heartbeat_while(
    awaitable: Any,
    *,
    claims: ChainlinkClaims,
    record: ClaimRecord,
    interval_s: float = _CLAIM_HEARTBEAT_INTERVAL_S,
) -> Any:
    """Keep the Chainlink claim fresh while a long compute await is active."""

    async def beat_loop() -> None:
        _heartbeat_claim_best_effort(claims, record)
        while True:
            await asyncio.sleep(interval_s)
            _heartbeat_claim_best_effort(claims, record)

    task = asyncio.create_task(beat_loop())
    try:
        return await awaitable
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class ChainlinkIssueReader:
    def __init__(self, *, chainlink_bin: str = "chainlink", runner: Runner | None = None) -> None:
        self.chainlink_bin = chainlink_bin
        self.runner = runner or _run

    def read(self, issue_id: int) -> IssueContext:
        result = self.runner([self.chainlink_bin, "issue", "show", str(issue_id), "--json"])
        if result.returncode != 0:
            message = (
                (result.stderr or result.stdout).strip()
                or f"chainlink issue show {issue_id} failed"
            )
            raise WorklinkError(message)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise WorklinkError("chainlink issue show did not return JSON") from exc
        comments = tuple(_comment_text(item) for item in payload.get("comments") or ())
        return IssueContext(
            issue_id=int(payload.get("id") or issue_id),
            title=str(payload.get("title") or ""),
            description=str(payload.get("description") or ""),
            labels={str(label) for label in payload.get("labels") or ()},
            parent_id=int(payload["parent_id"]) if payload.get("parent_id") is not None else None,
            comments=tuple(comment for comment in comments if comment),
            created_at=_parse_chainlink_datetime(payload.get("created_at")),
        )


def validate_leaf(issue: IssueContext) -> None:
    if "worklink:epic" in issue.labels:
        return
    missing = missing_leaf_template_parts(issue.description)
    if not missing:
        return
    message = "issue missing planner template: " + ", ".join(missing)
    if uses_strict_leaf_validation(issue.created_at):
        raise LeafValidationError(message)
    warnings.warn(message + " (legacy pre-contract leaf; continuing)", RuntimeWarning, stacklevel=2)
    _log_event(
        "worklink_legacy_template_warning",
        issue_id=issue.issue_id,
        missing=missing,
        created_at=issue.created_at.isoformat() if issue.created_at else None,
    )


def _demote_template_invalid_ready_leaf(
    issue: IssueContext,
    *,
    reason: str,
    runner: Runner,
    chainlink_bin: str,
) -> None:
    """Best-effort demotion for strict-template invalid ready leaves.

    Template validation happens before Worklink claims the issue. If a ready
    leaf fails there and keeps ``worklink:ready``, the autonomous ready queue can
    keep redispatching the same lowest-id issue forever. Demote only leaves that
    are currently marked ready, and deliberately do not acquire a lock: this is
    a pre-claim validation transition, not a worker attempt.
    """

    if "worklink:epic" in issue.labels:
        return
    if "worklink:ready" not in issue.labels:
        return

    issue_id = str(issue.issue_id)
    comment = (
        "WORKLINK_BLOCKED leaf template validation failed before dispatch; "
        f"{reason}. Re-plan this issue, then remove worklink:blocked and "
        "re-add worklink:ready when the required checklist is present."
    )
    commands = (
        (chainlink_bin, "issue", "unlabel", issue_id, "worklink:ready"),
        (chainlink_bin, "issue", "label", issue_id, "worklink:blocked"),
        (chainlink_bin, "issue", "comment", issue_id, comment),
    )
    for command in commands:
        try:
            result = runner(list(command))
        except Exception as exc:  # pragma: no cover - defensive best-effort guard
            _log_event(
                "worklink_template_invalid_demote_failed",
                issue_id=issue.issue_id,
                command=list(command[:3]),
                error=str(exc),
            )
            continue
        if result.returncode != 0:
            _log_event(
                "worklink_template_invalid_demote_failed",
                issue_id=issue.issue_id,
                command=list(command[:3]),
                error=(result.stderr or result.stdout).strip()[:500],
            )
    _log_event(
        "worklink_template_invalid_demoted",
        issue_id=issue.issue_id,
        reason=reason,
    )


def render_work_order(
    issue: IssueContext, *, template_path: Path, backend_name: str, test_command: str
) -> str:
    template = template_path.read_text(encoding="utf-8")
    return template.format(
        issue_id=issue.issue_id,
        title=issue.title,
        description=issue.description.strip(),
        labels=", ".join(sorted(issue.labels)) or "(none)",
        parent_id=issue.parent_id if issue.parent_id is not None else "(none)",
        backend=backend_name,
        test_command=test_command,
    )


@dataclass(frozen=True)
class WorklinkRunner:
    home: Path
    repo: Path
    chainlink_bin: str = "chainlink"
    agent_id: str = "mimir-worklink"
    runner: Runner | None = None
    registry: BackendRegistry | None = None

    async def run(
        self,
        issue_id: int,
        *,
        backend_name: str | None = None,
        dry_run: bool = False,
        test_command: str | None = None,
        base_branch: str | None = None,
        autonomous: bool = False,
    ) -> WorklinkRunResult:
        runner = self.runner or _runner_for_home(self.home, self.chainlink_bin)
        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)
        try:
            validate_leaf(issue)
        except LeafValidationError as exc:
            if not dry_run:
                _demote_template_invalid_ready_leaf(
                    issue,
                    reason=str(exc),
                    runner=runner,
                    chainlink_bin=self.chainlink_bin,
                )
            raise
        config = WorklinkConfig.load(self.home / "worklink.yaml")
        registry = self.registry or BackendRegistry(config)
        repo_url = _repo_remote_url(self.repo, runner=runner)
        repo_slug = _repo_slug_from_url(repo_url)
        backend = (
            registry.get(backend_name)
            if backend_name
            else registry.select(labels=issue.labels, repo=repo_slug)
        )
        compute = registry.select_compute(labels=issue.labels, repo=repo_slug)
        selected_name = backend.name
        test_cmd = test_command if test_command is not None else config.defaults.test_command
        template_path = _template_path(self.home)
        # Per-run override beats worklink.yaml, which beats the built-in "main".
        base = base_branch or config.defaults.base_branch

        # Dry-run validates the issue and renders the exact prompt without claiming
        # or mutating Chainlink/git state.
        if dry_run:
            prompt = render_work_order(
                issue,
                template_path=template_path,
                backend_name=selected_name,
                test_command=test_cmd,
            )
            order = WorkOrder(
                issue_id=issue.issue_id,
                worktree=self.repo / ".worklink" / f"{issue.issue_id}-DRYRUN",
                prompt=prompt,
                rules=None,
                timeout_s=config.defaults.timeout_s,
                transcript_root=self.home / "state" / "worklink" / "transcripts",
            )
            print(_format_work_order(order, backend=selected_name))
            print(f"\nBase branch: {base} (worktree cut from it; PR targets it)")
            return WorklinkRunResult(issue.issue_id, None, "dry_run", dry_run=True)

        # Autonomy safety gate (#460): autonomous dispatch (poller / worklink_run
        # tool, which pass autonomous=True) refuses an unsandboxed compute
        # substrate unless the operator opted in. Decided here in core, before
        # any claim/mutation, so the posture can't be bypassed by a caller. The
        # operator CLI passes autonomous=False and is never gated.
        if autonomous:
            allowed, reason = config.autonomous_compute_allowed(compute.name, compute.capabilities())
            if not allowed:
                _log_event(
                    "worklink_autonomous_refused",
                    issue_id=issue.issue_id,
                    compute_backend=compute.name,
                )
                return WorklinkRunResult(issue.issue_id, None, "refused", reason=reason)

        claims = ChainlinkClaims(
            chainlink_bin=self.chainlink_bin,
            agent_id=self.agent_id,
            runner=_list_runner(runner),
        )
        # Re-read immediately before claiming so retries in a long-lived caller do
        # not use stale comments and collide with prior attempt-scoped branches.
        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)
        claim = claims.claim_issue(
            issue.issue_id,
            issue.comments,
            max_active_locks=config.defaults.max_concurrent if autonomous else None,
        )
        if claim.attempts_exhausted:
            _log_event("worklink_attempts_exhausted", issue_id=issue.issue_id)
            return WorklinkRunResult(issue.issue_id, None, "blocked", reason="attempts_exhausted")
        if not claim.claimed or claim.record is None:
            return WorklinkRunResult(
                issue.issue_id, None, "failed", reason=claim.reason or "claim_failed"
            )
        record = claim.record
        _log_event(
            "worklink_claimed",
            issue_id=issue.issue_id,
            attempt=record.attempt,
            backend=selected_name,
        )

        lease: WorktreeLease | None = None
        try:
            lease = _create_backend_checkout(
                self.repo,
                issue_id=issue.issue_id,
                attempt=record.attempt,
                base=base,
                backend_name=selected_name,
                compute_shared_filesystem=compute.capabilities().shared_filesystem,
                base_fetch=config.defaults.base_fetch,
                event_logger=_log_event,
                runner=_list_runner(runner),
            )
            # chainlink #517: codex resolves the git project root from the
            # filesystem, so when it executes on the CONTROLLER (a shared-filesystem
            # compute) it must be pointed at an isolated checkout with its own
            # ``.git``, never a parent-pointing worktree, or it edits the repo root
            # (observed on #512/#513). ``_create_backend_checkout`` already routes
            # codex + shared_filesystem to ``create_isolated_checkout``; this is a
            # fail-loud backstop against that routing regressing. It does NOT fire
            # for remote computes (docker_sibling/ecs report shared_filesystem=false
            # and run codex inside the worker's own clone, which is safe).
            if (
                selected_name == "codex"
                and compute.capabilities().shared_filesystem
                and not lease.isolated_checkout
            ):
                _log_event(
                    "worklink_unsafe_codex_checkout",
                    issue_id=issue.issue_id,
                    attempt=record.attempt,
                    compute_backend=compute.name,
                )
                return WorklinkRunResult(
                    issue.issue_id,
                    None,
                    "blocked",
                    reason=(
                        "codex on a shared-filesystem compute must run in an isolated "
                        "checkout (own .git), not a parent-pointing worktree, to avoid "
                        "leaking edits into the repo root (chainlink #517)"
                    ),
                )
            root_dirty_before = _dirty_paths(self.repo, runner=runner)
            prompt = render_work_order(
                issue,
                template_path=template_path,
                backend_name=selected_name,
                test_command=test_cmd,
            )
            order = WorkOrder(
                issue_id=issue.issue_id,
                worktree=lease.path,
                prompt=prompt,
                rules=None,
                timeout_s=config.defaults.timeout_s,
                env={"MIMIR_HOME": str(self.home)},
                transcript_root=self.home / "state" / "worklink" / "transcripts",
            )
            started = datetime.now(UTC)
            spec = backend.work_spec(
                order,
                attempt=record.attempt,
                repo_url=repo_url,
                base_ref=lease.base_ref,
                branch=lease.branch,
                test_command=test_cmd,
            )
            handle = None
            try:
                handle = await compute.launch(spec)
                # #561: persist the worker handle so a fresh controller can
                # reattach after a container restart. Only substrates that survive
                # a controller disconnect (docker_sibling/ecs) are recoverable;
                # local_subprocess work dies with us, so nothing is persisted.
                if compute.capabilities().persistent_after_disconnect:
                    _persist_run_state(
                        self.home,
                        issue=issue,
                        attempt=record.attempt,
                        backend_name=selected_name,
                        compute=compute,
                        handle=handle,
                        lease=lease,
                        repo=self.repo,
                        repo_url=repo_url,
                        test_command=test_cmd,
                        started_at=started,
                    )
                compute_result = await _heartbeat_while(
                    compute.wait(handle, spec.timeout_s),
                    claims=claims,
                    record=record,
                )
            except ComputeLaunchError as exc:
                compute_result = ComputeResult(
                    exit_code=-1,
                    stdout="",
                    stderr=str(exc),
                    launch_error=str(exc),
                )
            finally:
                if handle is not None:
                    await compute.cleanup(handle)
            return await self._finalize(
                issue=issue,
                claims=claims,
                claim_record=record,
                attempt=record.attempt,
                config=config,
                backend=backend,
                compute=compute,
                compute_result=compute_result,
                order=order,
                lease=lease,
                spec=spec,
                started=started,
                test_cmd=test_cmd,
                root_dirty_before=root_dirty_before,
                runner=runner,
            )
        except Exception as exc:
            try:
                claims.transition_issue(
                    issue.issue_id,
                    status="failed",
                    review_ready=False,
                    attempt=record.attempt,
                    reason=str(exc),
                )
            except Exception:
                pass
            _log_event(
                "worklink_transition",
                issue_id=issue.issue_id,
                attempt=record.attempt,
                status="failed",
                reason=str(exc),
            )
            return WorklinkRunResult(
                issue.issue_id,
                record.attempt,
                "failed",
                reason=str(exc),
                worktree=lease.path if lease else None,
                branch=lease.branch if lease else None,
            )
        finally:
            claims.release_issue(issue.issue_id)
            # The run reached a terminal transition (or failed): the worker no
            # longer needs reattaching. A process killed BEFORE here leaves the
            # state file in place for the startup reconcile.
            clear_run_state(self.home, issue.issue_id)

    async def _finalize(
        self,
        *,
        issue: IssueContext,
        claims: ChainlinkClaims,
        claim_record: ClaimRecord,
        attempt: int,
        config: WorklinkConfig,
        backend: Any,
        compute: Any,
        compute_result: ComputeResult,
        order: WorkOrder,
        lease: WorktreeLease,
        spec: Any,
        started: datetime,
        test_cmd: str | None,
        root_dirty_before: Sequence[str],
        runner: Runner,
    ) -> WorklinkRunResult:
        """Post-launch pipeline: interpret the worker result, observe evidence,
        open the PR on a passing gate, then transition + clean up.

        Extracted so both a fresh ``run`` and a post-restart ``reattach`` share
        the identical evidence/PR/transition path — the only difference between
        them is how ``compute_result`` was obtained (launch+wait vs. wait on a
        persisted handle)."""
        selected_name = backend.name
        raw = await backend.interpret(order, compute_result)
        remote_gate = not compute.capabilities().shared_filesystem
        pr_url = None
        if remote_gate:
            validation = observe_remote_evidence(
                issue=issue.issue_id,
                attempt=attempt,
                backend=selected_name,
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
            # chainlink #538: remote runs can't run the worker's (untrusted)
            # branch on the controller, so observe_remote_evidence stubs tests
            # observed=false → the gate fails closed. When the run COMPLETED and
            # the re-derived diff is real, run a fresh sandboxed test job on the
            # pushed branch and fold its exit code in (controller-orchestrated;
            # exit-code is the trust channel). Gate on backend_completed so we
            # don't re-test (or launder into review-ready) a run that already
            # failed/blocked but left a partial diff. A launch/timeout failure
            # leaves tests unobserved (still fail-closed).
            if (
                test_cmd
                and backend_completed(raw.backend_status)
                and validation.evidence.diff_observed
                and validation.evidence.files_changed
            ):
                test_exit = await _run_remote_test_job(
                    compute,
                    spec,
                    timeout_s=config.defaults.timeout_s,
                    claims=claims,
                    claim_record=claim_record,
                )
                if test_exit is not None:
                    validation = fold_remote_test_evidence(
                        validation, test_cmd, test_exit, backend_status=raw.backend_status
                    )
            evidence_path = _write_evidence(self.home, validation.evidence)
            if validation.review_ready:
                pr_url = _open_pr(
                    self.repo,
                    issue,
                    lease.branch,
                    validation.evidence,
                    base=lease.base_ref,
                    runner=runner,
                )
                validation = _with_pr_url(validation, pr_url)
                evidence_path = _write_evidence(self.home, validation.evidence)
        else:
            validation = observe_evidence(
                issue=issue.issue_id,
                attempt=attempt,
                backend=selected_name,
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
            validation = _with_outside_worktree_detection(
                validation,
                issue=issue.issue_id,
                attempt=attempt,
                root=self.repo,
                worktree=lease.path,
                runner=runner,
                root_dirty_before=root_dirty_before,
            )
            evidence_path = _write_evidence(self.home, validation.evidence)
            if validation.review_ready:
                _commit_worktree_changes(lease.path, issue, runner=runner)
                try:
                    _ensure_clean_worktree(lease.path, runner=runner)
                except WorklinkError as exc:
                    validation = _failed_validation(validation, str(exc))
                else:
                    validation = observe_evidence(
                        issue=issue.issue_id,
                        attempt=attempt,
                        backend=selected_name,
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
                    validation = _with_outside_worktree_detection(
                        validation,
                        issue=issue.issue_id,
                        attempt=attempt,
                        root=self.repo,
                        worktree=lease.path,
                        runner=runner,
                        root_dirty_before=root_dirty_before,
                    )
                evidence_path = _write_evidence(self.home, validation.evidence)
            if validation.review_ready:
                # chainlink #518: push from the checkout that OWNS the attempt
                # branch, not the parent repo. With the isolated-checkout shape
                # (#517) the branch + its commit live only inside ``lease.path``
                # (its own .git, with ``origin`` already pointed at the real
                # remote); pushing from ``self.repo`` fails with
                # "src refspec <branch> does not match any". This is also correct
                # for the legacy worktree shape, which shares the parent's refs.
                _git_push(lease.path, lease.branch, runner=runner)
                pr_url = _open_pr(
                    self.repo,
                    issue,
                    lease.branch,
                    validation.evidence,
                    base=lease.base_ref,
                    runner=runner,
                )
                validation = _with_pr_url(validation, pr_url)
                evidence_path = _write_evidence(self.home, validation.evidence)
        _comment_evidence(
            claims,
            validation.evidence,
            validation,
            evidence_path,
            gate_test_tail=(
                None if validation.review_ready else extract_gate_test_tail(compute_result.stdout)
            ),
        )
        _log_event(
            "worklink_evidence",
            issue_id=issue.issue_id,
            attempt=attempt,
            status=validation.status,
            review_ready=validation.review_ready,
            reasons=list(validation.reasons),
        )
        claims.transition_issue(
            issue.issue_id,
            status=validation.status,
            review_ready=validation.review_ready,
            attempt=attempt,
            reason=validation.evidence.blocked_reason
            if validation.status == "blocked"
            else (", ".join(validation.reasons) if validation.reasons else None),
        )
        _log_event(
            "worklink_transition",
            issue_id=issue.issue_id,
            attempt=attempt,
            status=validation.status,
            review_ready=validation.review_ready,
            pr_url=pr_url,
        )
        cleanup_error = _cleanup_worktree_after_transition(
            lease,
            outcome=validation.status,
            runner=_list_runner(runner),
            issue_id=issue.issue_id,
            attempt=attempt,
        )
        return WorklinkRunResult(
            issue.issue_id,
            attempt,
            validation.status,
            review_ready=validation.review_ready,
            pr_url=pr_url,
            evidence_path=evidence_path,
            worktree=lease.path,
            branch=lease.branch,
            reason=f"post-transition cleanup failed: {cleanup_error}" if cleanup_error else None,
        )

    async def reattach(self, issue_id: int) -> WorklinkRunResult:
        """Resume an in-flight run after a controller restart (#561).

        The worker (a docker-sibling/ecs container/task) survives the restart;
        the persisted handle lets a fresh controller wait on it, harvest evidence
        from the pushed branch, and open the PR — instead of orphaning the work
        and waiting for the TTL reaper to re-run it from scratch.

        Safe to lose: if the worker is unrecoverable (broker also restarted, job
        gone), we transition the leaf off ``in-progress`` so the ready queue
        re-dispatches it immediately rather than leaving it stuck. The chainlink
        lock + ``in-progress`` label survive the restart, so this never re-claims
        or bumps the attempt — it finishes the attempt already underway."""
        state = load_run_state(self.home, issue_id)
        if state is None:
            return WorklinkRunResult(issue_id, None, "failed", reason="reattach: no run state")

        runner = self.runner or _runner_for_home(self.home, self.chainlink_bin)
        claims = ChainlinkClaims(
            chainlink_bin=self.chainlink_bin,
            agent_id=self.agent_id,
            runner=_list_runner(runner),
        )
        # Only resume a leaf still in-progress. If the reaper already recovered it
        # (or a prior run transitioned it) the work is no longer ours to finish —
        # drop the stale state and stop. ``_issue_has_label`` fails open (assume
        # in-progress) when labels can't be read, so a transient read error
        # doesn't strand the worker.
        if not claims._issue_has_label(issue_id, "worklink:in-progress"):  # noqa: SLF001
            _log_event("worklink_reattach_skipped", issue_id=issue_id, reason="not_in_progress")
            clear_run_state(self.home, issue_id)
            return WorklinkRunResult(
                issue_id, state.attempt, "failed", reason="reattach: leaf no longer in-progress"
            )

        config = WorklinkConfig.load(self.home / "worklink.yaml")
        registry = self.registry or BackendRegistry(config)
        try:
            backend = registry.get(state.backend)
            compute = registry.get_compute(state.compute_name)
        except (KeyError, ValueError) as exc:
            _log_event("worklink_reattach_failed", issue_id=issue_id, reason=str(exc))
            clear_run_state(self.home, issue_id)
            return WorklinkRunResult(issue_id, state.attempt, "failed", reason=f"reattach: {exc}")
        if not compute.capabilities().persistent_after_disconnect:
            # Defensive: only persistent substrates are ever persisted.
            clear_run_state(self.home, issue_id)
            return WorklinkRunResult(
                issue_id, state.attempt, "failed", reason="reattach: compute not resumable"
            )

        handle = LaunchHandle(state.handle_substrate, state.handle_identifier)
        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)
        test_cmd = state.test_command
        _log_event(
            "worklink_reattach",
            issue_id=issue_id,
            attempt=state.attempt,
            compute_backend=compute.name,
            job=state.handle_identifier,
        )

        lease: WorktreeLease | None = None
        try:
            lease = _create_observation_worktree(
                self.repo,
                issue_id=issue_id,
                attempt=state.attempt,
                base=state.base_ref,
                local_base=state.local_base,
                branch=state.branch,
                runner=_list_runner(runner),
            )
            started = _parse_chainlink_datetime(state.started_at) or datetime.now(UTC)
            prompt = render_work_order(
                issue,
                template_path=_template_path(self.home),
                backend_name=backend.name,
                test_command=test_cmd or "",
            )
            order = WorkOrder(
                issue_id=issue_id,
                worktree=lease.path,
                prompt=prompt,
                rules=None,
                timeout_s=config.defaults.timeout_s,
                env={"MIMIR_HOME": str(self.home)},
                transcript_root=self.home / "state" / "worklink" / "transcripts",
            )
            spec = backend.work_spec(
                order,
                attempt=state.attempt,
                repo_url=state.repo_url,
                base_ref=state.local_base or state.base_ref,
                branch=state.branch,
                test_command=test_cmd or "",
            )
            claim_record = ClaimRecord(
                issue_id=issue_id,
                attempt=state.attempt,
                agent_id=self.agent_id,
                claimed_at=started,
            )
            try:
                compute_result = await _heartbeat_while(
                    compute.wait(handle, config.defaults.timeout_s),
                    claims=claims,
                    record=claim_record,
                )
            finally:
                await compute.cleanup(handle)
            if _reattach_worker_lost(compute_result):
                # Broker/substrate can no longer produce the result (e.g. it also
                # restarted): the compute is wasted. Fall back to redispatch
                # immediately so the leaf doesn't sit in-progress until the reaper.
                _log_event(
                    "worklink_reattach_lost",
                    issue_id=issue_id,
                    attempt=state.attempt,
                    error=(compute_result.launch_error or "")[:300],
                )
                claims.transition_issue(
                    issue_id,
                    status="failed",
                    review_ready=False,
                    attempt=state.attempt,
                    reason="reattach: worker lost after controller restart",
                )
                return WorklinkRunResult(
                    issue_id, state.attempt, "failed", reason="reattach: worker lost"
                )
            return await self._finalize(
                issue=issue,
                claims=claims,
                claim_record=claim_record,
                attempt=state.attempt,
                config=config,
                backend=backend,
                compute=compute,
                compute_result=compute_result,
                order=order,
                lease=lease,
                spec=spec,
                started=started,
                test_cmd=test_cmd,
                root_dirty_before=(),
                runner=runner,
            )
        except Exception as exc:
            try:
                claims.transition_issue(
                    issue_id,
                    status="failed",
                    review_ready=False,
                    attempt=state.attempt,
                    reason=f"reattach failed: {exc}",
                )
            except Exception:
                pass
            _log_event(
                "worklink_reattach_failed", issue_id=issue_id, attempt=state.attempt, error=str(exc)
            )
            return WorklinkRunResult(
                issue_id, state.attempt, "failed", reason=f"reattach failed: {exc}"
            )
        finally:
            if lease is not None:
                _remove_observation_worktree(self.repo, lease, runner=_list_runner(runner))
            claims.release_issue(issue_id)
            clear_run_state(self.home, issue_id)


def _cleanup_worktree_after_transition(
    lease: WorktreeLease,
    *,
    outcome: str,
    runner: Runner,
    issue_id: int,
    attempt: int,
) -> str | None:
    """Best-effort cleanup after the Chainlink terminal transition is durable.

    Cleanup failures must not re-enter the main failure handler: by this point
    evidence has been written, the PR may be open, and Chainlink already reflects
    the observed backend outcome. Reclassifying the issue as failed would corrupt
    that success path and can re-dispatch duplicate work.
    """
    try:
        cleanup_worktree(lease, outcome=outcome, runner=runner)
    except Exception as exc:  # pragma: no cover - exact exception type is platform/git dependent.
        error = str(exc)
        _log_event(
            "worklink_cleanup_failed",
            issue_id=issue_id,
            attempt=attempt,
            outcome=outcome,
            worktree=str(lease.path),
            branch=lease.branch,
            error=error,
        )
        return error
    return None


def run_worklink(
    *,
    home: Path,
    repo: Path,
    issue_id: int,
    backend: str | None = None,
    dry_run: bool = False,
    test_command: str | None = None,
    base_branch: str | None = None,
    autonomous: bool = False,
) -> WorklinkRunResult:
    return asyncio.run(
        WorklinkRunner(home=home, repo=repo).run(
            issue_id,
            backend_name=backend,
            dry_run=dry_run,
            test_command=test_command,
            base_branch=base_branch,
            autonomous=autonomous,
        )
    )


def run_worklink_reattach(*, home: Path, repo: Path, issue_id: int) -> WorklinkRunResult:
    """Resume one in-flight run after a controller restart (#561)."""
    return asyncio.run(WorklinkRunner(home=home, repo=repo).reattach(issue_id))


def _persist_run_state(
    home: Path,
    *,
    issue: IssueContext,
    attempt: int,
    backend_name: str,
    compute: Any,
    handle: LaunchHandle,
    lease: WorktreeLease,
    repo: Path,
    repo_url: str | None,
    test_command: str | None,
    started_at: datetime,
) -> None:
    """Record the worker handle so a fresh controller can reattach (#561).

    Best-effort: a persist failure must not abort an otherwise-healthy run — the
    only cost is falling back to the TTL reaper if this run is later interrupted."""
    try:
        save_run_state(
            home,
            WorklinkRunState(
                issue_id=issue.issue_id,
                attempt=attempt,
                backend=backend_name,
                compute_name=compute.name,
                handle_substrate=handle.substrate,
                handle_identifier=handle.identifier,
                branch=lease.branch,
                base_ref=lease.base_ref,
                local_base=lease.local_base or lease.base_ref,
                repo=str(repo),
                repo_url=repo_url or "",
                test_command=test_command,
                started_at=started_at.astimezone(UTC).isoformat(),
            ),
        )
    except OSError as exc:
        _log_event("worklink_run_state_persist_failed", issue_id=issue.issue_id, error=str(exc))


def _create_observation_worktree(
    repo: Path,
    *,
    issue_id: int,
    attempt: int,
    base: str,
    local_base: str,
    branch: str,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> WorktreeLease:
    """Throwaway detached worktree for re-deriving REMOTE evidence on reattach.

    ``observe_remote_evidence`` fetches ``origin/<base>`` and ``origin/<branch>``
    by refspec into this checkout and diffs the remote refs, so it only needs to
    be a valid checkout with an ``origin`` remote — the worker already pushed the
    branch. Detached + a dedicated ``reattach-`` path so it never collides with
    the (possibly surviving) original attempt worktree."""
    path = repo / ".worklink" / f"reattach-{issue_id}-{attempt}"
    # Clear any leftover from a previous reattach of the same leaf.
    runner(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)])
    shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    add = runner(["git", "-C", str(repo), "worktree", "add", "--detach", str(path)])
    if add.returncode != 0:
        raise WorklinkError(
            (add.stderr or add.stdout).strip() or "git worktree add (reattach observation) failed"
        )
    return WorktreeLease(
        issue_id=issue_id,
        attempt=attempt,
        repo=repo,
        path=path,
        branch=branch,
        base_ref=base,
        local_base=local_base or base,
        isolated_checkout=False,
    )


def _remove_observation_worktree(
    repo: Path,
    lease: WorktreeLease,
    *,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> None:
    """Best-effort removal of the throwaway reattach observation worktree.

    ``_finalize`` only removes the worktree on a ``completed`` outcome (it retains
    failed/blocked attempts for autopsy); the reattach worktree is disposable in
    every outcome, so force-remove whatever's left without raising."""
    runner(["git", "-C", str(repo), "worktree", "remove", "--force", str(lease.path)])
    shutil.rmtree(lease.path, ignore_errors=True)


def _reattach_worker_lost(result: ComputeResult) -> bool:
    """True when the substrate can no longer produce the worker's result on
    reattach — e.g. the broker container also restarted, or the job was already
    cleaned up. A genuine timeout (worker still running, or it hit its own bound)
    is NOT "lost": only a ``launch_error`` means we couldn't reach/find the job."""
    return result.launch_error is not None


def render_decomposition_prompt(
    *,
    template_path: Path,
    parent_id: int,
    title: str,
    labels: str,
    priority: str,
    description: str,
) -> str:
    template = template_path.read_text(encoding="utf-8")
    return render_decompose_prompt(
        template,
        parent_id=parent_id,
        title=title,
        labels=labels,
        priority=priority,
        description=description,
    )


def _template_path(home: Path) -> Path:
    custom = home / "prompts" / "worklink-order.md"
    if custom.exists():
        return custom
    return Path(__file__).resolve().parents[1] / "prompt_templates" / "worklink-order.md"


def _format_work_order(order: WorkOrder, *, backend: str) -> str:
    payload = {
        "backend": backend,
        "issue_id": order.issue_id,
        "worktree": str(order.worktree),
        "timeout_s": order.timeout_s,
        "transcript_root": str(order.transcript_root) if order.transcript_root else None,
        "prompt": order.prompt,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _write_evidence(home: Path, evidence: WorklinkEvidence) -> Path:
    path = home / "state" / "worklink" / "evidence" / f"{evidence.issue}-{evidence.attempt}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_evidence_json(evidence), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def _comment_evidence(
    claims: ChainlinkClaims,
    evidence: WorklinkEvidence,
    validation: EvidenceValidation,
    evidence_path: Path,
    *,
    gate_test_tail: str | None = None,
) -> None:
    summary = (
        f"WORKLINK_EVIDENCE issue={evidence.issue} attempt={evidence.attempt} "
        f"status={validation.status} review_ready={str(validation.review_ready).lower()} "
        f"files={len(evidence.files_changed)} evidence={evidence_path}"
    )
    reasons = f"\nReasons: {', '.join(validation.reasons)}" if validation.reasons else ""
    # chainlink #815: the failed gate-test output otherwise dies with the worker
    # container; the issue comment is the per-leaf surface the planner (and the
    # next dispatch's groomer) actually reads.
    tail = f"\nGate test output (failed):\n{gate_test_tail}" if gate_test_tail else ""
    claims._run(  # noqa: SLF001 - Chainlink wrapper owns quoting/checks.
        "issue", "comment", str(evidence.issue), summary + reasons + tail
    )


def _commit_worktree_changes(worktree: Path, issue: IssueContext, *, runner: Runner) -> None:
    add = runner(["git", "-C", str(worktree), "add", "-A"])
    if add.returncode != 0:
        raise WorklinkError((add.stderr or add.stdout).strip() or "git add failed")
    staged = runner(["git", "-C", str(worktree), "diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        raise WorklinkError("no staged Worklink changes to commit")
    commit = runner([
        "git",
        "-C",
        str(worktree),
        "commit",
        "-m",
        f"worklink: issue #{issue.issue_id}",
    ])
    if commit.returncode != 0:
        raise WorklinkError((commit.stderr or commit.stdout).strip() or "git commit failed")


def _create_backend_checkout(
    repo: Path,
    *,
    issue_id: int,
    attempt: int,
    base: str,
    backend_name: str,
    compute_shared_filesystem: bool,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    base_fetch: bool = True,
    event_logger: Callable[..., None] | None = None,
) -> WorktreeLease:
    if backend_name == "codex" and compute_shared_filesystem:
        return create_isolated_checkout(
            repo,
            issue_id=issue_id,
            attempt=attempt,
            base=base,
            base_fetch=base_fetch,
            event_logger=event_logger,
            runner=runner,
        )
    return create_worktree(
        repo,
        issue_id=issue_id,
        attempt=attempt,
        base=base,
        base_fetch=base_fetch,
        event_logger=event_logger,
        runner=runner,
    )


def _with_outside_worktree_detection(
    validation: EvidenceValidation,
    *,
    issue: int,
    attempt: int,
    root: Path,
    worktree: Path,
    runner: Runner,
    root_dirty_before: Sequence[str] = (),
) -> EvidenceValidation:
    # Local shared-filesystem backends are expected to write only under the
    # attempt checkout. If the attempt diff is empty but the parent checkout is
    # dirty, surface the containment failure explicitly instead of only reporting
    # ``completed_empty_diff``. This is the exact fingerprint from Worklink #512.
    if validation.evidence.files_changed:
        return validation
    root_paths = _new_dirty_paths(_dirty_paths(root, runner=runner), before=root_dirty_before)
    if not root_paths:
        return validation
    escaped = _paths_escape_worktree(root_paths, root=root, worktree=worktree)
    if not escaped:
        return validation

    _log_event(
        "worklink_backend_wrote_outside_worktree",
        issue_id=issue,
        attempt=attempt,
        root=str(root),
        worktree=str(worktree),
        files=escaped[:50],
    )
    stash = _quarantine_dirty_paths(root, escaped, issue=issue, attempt=attempt, runner=runner)
    reason = "backend_wrote_outside_worktree: " + ", ".join(escaped[:10])
    if stash:
        reason += f" (quarantined to git stash '{stash}' in the repo root)"
    return _failed_validation(validation, reason)


def _quarantine_dirty_paths(
    root: Path, paths: Sequence[str], *, issue: int, attempt: int, runner: Runner
) -> str | None:
    """Move leaked root edits into a recoverable, named ``git stash`` so the parent
    repo is left clean without destroying the work (#517).

    Recoverable on purpose: a hard ``git checkout`` would silently discard
    salvageable changes if containment ever regresses. The stash is path-scoped to
    the leaked paths, so pre-existing unrelated dirt in the root is untouched.
    Best-effort — a stash failure is logged and the containment failure is still
    surfaced. Returns the stash label on success, else ``None``.
    """
    if not paths:
        return None
    label = f"worklink-leak-{issue}-a{attempt}"
    result = runner(
        ["git", "-C", str(root), "stash", "push", "--include-untracked", "-m", label, "--", *paths]
    )
    if result.returncode != 0:
        _log_event(
            "worklink_quarantine_failed",
            issue_id=issue,
            attempt=attempt,
            error=(result.stderr or result.stdout).strip()[:500],
        )
        return None
    _log_event(
        "worklink_quarantined_outside_worktree",
        issue_id=issue,
        attempt=attempt,
        stash=label,
        files=list(paths)[:50],
    )
    return label


def _dirty_paths(repo: Path, *, runner: Runner) -> list[str]:
    status = runner(["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"])
    if status.returncode != 0:
        return []
    return _paths_from_status(status.stdout)


def _new_dirty_paths(paths: Sequence[str], *, before: Sequence[str]) -> list[str]:
    old = set(before)
    return [path for path in paths if path not in old]


def _paths_escape_worktree(paths: Sequence[str], *, root: Path, worktree: Path) -> list[str]:
    root_resolved = root.resolve()
    worktree_resolved = worktree.resolve()
    escaped: list[str] = []
    for path in paths:
        absolute = (root_resolved / path).resolve()
        if absolute == worktree_resolved or absolute.is_relative_to(worktree_resolved):
            continue
        escaped.append(path)
    return escaped


def _paths_from_status(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if path:
            paths.append(path.strip())
    return paths


def _ensure_clean_worktree(worktree: Path, *, runner: Runner) -> None:
    status = runner([
        "git", "-C", str(worktree), "status", "--porcelain=v1", "--untracked-files=all"
    ])
    if status.returncode != 0:
        raise WorklinkError((status.stderr or status.stdout).strip() or "git status failed")
    if status.stdout.strip():
        raise WorklinkError("worktree still dirty after Worklink commit")


async def _run_remote_test_job(
    compute: Any,
    spec: Any,
    *,
    timeout_s: int,
    claims: ChainlinkClaims | None = None,
    claim_record: ClaimRecord | None = None,
) -> int | None:
    """Dispatch a fresh sandboxed test job on the pushed branch and return its
    exit code (chainlink #538): 0 = tests passed.

    Reuses the same compute substrate as the implement run — the worker, in
    ``test_only`` mode, clones + checks out the pushed branch, runs
    ``test_command``, and exits with the test's code, which surfaces as the
    job's ``ComputeResult.exit_code``. Returns ``None`` when the job couldn't run
    cleanly (launch error / timeout) so the caller leaves tests unobserved and
    the gate stays fail-closed.
    """
    test_spec = replace(spec, test_only=True)
    handle = None
    try:
        handle = await compute.launch(test_spec)
        wait = compute.wait(handle, timeout_s)
        if claims is not None and claim_record is not None:
            result = await _heartbeat_while(wait, claims=claims, record=claim_record)
        else:
            result = await wait
    except ComputeLaunchError as exc:
        _log_event("worklink_remote_test_job_launch_failed", issue_id=spec.issue_id, error=str(exc)[:300])
        return None
    except Exception as exc:  # noqa: BLE001 — any dispatch failure is non-fatal: fail closed, leaving tests unobserved
        _log_event("worklink_remote_test_job_unobserved", issue_id=spec.issue_id, error=str(exc)[:300])
        return None
    finally:
        if handle is not None:
            await compute.cleanup(handle)
    if result.launch_error or result.timed_out:
        _log_event(
            "worklink_remote_test_job_unobserved",
            issue_id=spec.issue_id,
            timed_out=result.timed_out,
            error=(result.launch_error or "")[:300],
        )
        return None
    _log_event("worklink_remote_test_job", issue_id=spec.issue_id, exit_code=result.exit_code)
    return result.exit_code


def _git_push(repo: Path, branch: str, *, runner: Runner) -> None:
    result = runner(["git", "-C", str(repo), "push", "-u", "origin", branch])
    if result.returncode != 0:
        raise WorklinkError((result.stderr or result.stdout).strip() or "git push failed")


def _open_pr(
    repo: Path,
    issue: IssueContext,
    branch: str,
    evidence: WorklinkEvidence,
    *,
    base: str,
    runner: Runner,
) -> str:
    body = (
        f"Closes chainlink #{issue.issue_id}.\n\n"
        f"Worklink evidence:\n"
        f"- Base: `{base}`\n"
        f"- Branch: `{branch}`\n"
        f"- Files changed: {len(evidence.files_changed)}\n"
        "- Tests: "
        f"`{evidence.tests.cmd if evidence.tests else '(none)'}` → "
        f"{evidence.tests.exit_code if evidence.tests else 'missing'}\n"
        f"- Transcript: `{evidence.transcript or '(none)'}`\n"
    )
    command = ["gh", "pr", "create", "--base", base, "--head", branch]
    repo_slug = _repo_slug(repo, runner=runner)
    if repo_slug:
        command.extend(["--repo", repo_slug])
    command.extend([
        "--title", f"Worklink #{issue.issue_id}: {issue.title}",
        "--body", body,
    ])
    result = runner(command)
    if result.returncode != 0:
        raise WorklinkError((result.stderr or result.stdout).strip() or "gh pr create failed")
    return result.stdout.strip().splitlines()[-1]


def _repo_remote_url(repo: Path, *, runner: Runner | None = None) -> str | None:
    run = runner or _run
    result = run(["git", "-C", str(repo), "config", "--get", "remote.origin.url"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _repo_slug(repo: Path, *, runner: Runner | None = None) -> str | None:
    return _repo_slug_from_url(_repo_remote_url(repo, runner=runner))


def _repo_slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("git@github.com:"):
        return url.removeprefix("git@github.com:").removesuffix(".git")
    if "github.com/" in url:
        return url.rsplit("github.com/", 1)[1].removesuffix(".git")
    return None


def _with_pr_url(validation: EvidenceValidation, pr_url: str) -> EvidenceValidation:
    evidence = replace(validation.evidence, pr_url=pr_url)
    return replace(validation, evidence=evidence)


def _failed_validation(validation: EvidenceValidation, reason: str) -> EvidenceValidation:
    evidence = replace(validation.evidence, status="failed")
    return replace(
        validation,
        status="failed",
        review_ready=False,
        reasons=(*validation.reasons, reason),
        evidence=evidence,
    )


def _evidence_json(evidence: WorklinkEvidence) -> dict[str, Any]:
    data = asdict(evidence)
    data["commands"] = [asdict(command) for command in evidence.commands]
    data["tests"] = asdict(evidence.tests) if evidence.tests else None
    return data


def _parse_chainlink_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _comment_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("content", "body", "text", "comment"):
            if key in value:
                return str(value[key])
    return ""


def _list_runner(runner: Runner) -> Callable[[Sequence[str]], subprocess.CompletedProcess[str]]:
    return lambda args: runner(list(args))


def _runner_for_home(home: Path, chainlink_bin: str) -> Runner:
    def run(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if isinstance(args, str):
            return subprocess.run(args, shell=True, cwd=cwd, capture_output=True, text=True, check=False)
        command_cwd = cwd if cwd is not None else (home if args and args[0] == chainlink_bin else None)
        return subprocess.run(list(args), cwd=command_cwd, capture_output=True, text=True, check=False)

    return run


def _run(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    if isinstance(args, str):
        return subprocess.run(args, shell=True, cwd=cwd, capture_output=True, text=True, check=False)
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=False)


def _log_event(event_type: str, **payload: Any) -> None:
    try:
        from ..event_logger import log_event_sync

        log_event_sync(event_type, **payload)
    except RuntimeError:
        pass
