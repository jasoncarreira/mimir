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
import os
from pathlib import Path
import shutil
import subprocess
import warnings
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .backends import BackendRegistry, WorkOrder, WorklinkConfig
from .compute import ComputeLaunchError, ComputeResult, LaunchHandle
from .claims import ChainlinkClaims, ClaimRecord
from .evidence import (
    EvidenceValidation,
    WorklinkEvidence,
    observe_evidence,
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
from .worktree import WorktreeLease, cleanup_worktree, create_isolated_checkout, create_worktree

Runner = Callable[..., subprocess.CompletedProcess[str]]
_CLAIM_HEARTBEAT_INTERVAL_S = 60.0

# --- feature-factory autonomous adapter (chainlink #833) --------------------
# The factory self-drives every gate and writes run.json.terminal_result at a
# terminal state. Under --detached the launcher backgrounds opencode and exits
# immediately, so the adapter launches ONCE (detached), then POLLS run.json to a
# terminal state, mirroring meaningful transitions. Liveness is probe-based: a
# stale heartbeat is a TRIGGER TO PROBE (recent run-dir / process-log file
# activity), never an auto-fail; with --detached the launcher process is gone so
# job-liveness reads as unknown and the file-activity signal governs. There is no
# held compute.wait timeout anymore, so _epic_run_timeout_s() is the run's hard
# wall-clock ceiling.


def _epic_run_timeout_s() -> float:
    """Wall-clock ceiling (seconds) for a whole DETACHED autonomous run.

    With ``--detached`` the launcher returns immediately, so there is no held
    ``compute.wait`` timeout to bound the run — the poll loop enforces this bound
    instead. Generous default (~4h); a run whose ``run.json`` never reaches a
    terminal state within it is marked failed ("exceeded run timeout").
    """
    try:
        return max(0.0, float(os.environ.get("MIMIR_FACTORY_RUN_TIMEOUT_S", "14400")))
    except ValueError:
        return 14400.0


def _epic_stale_heartbeat_s() -> float:
    """Heartbeat age (seconds) that TRIGGERS a liveness probe — not an auto-fail.

    Generous default (~15 min): the factory bumps ``heartbeat_at`` far more
    often, so exceeding this only means "look closer", not "give up". Also gates
    the startup grace for "no run.json yet" (a fresh detached launch has none).
    """
    try:
        return max(0.0, float(os.environ.get("MIMIR_FACTORY_STALE_HEARTBEAT_S", "900")))
    except ValueError:
        return 900.0


def _epic_probe_window_s() -> float:
    """Window (seconds) within which SOME run-dir file must have advanced for a
    stale-heartbeat run to still count as making progress."""
    try:
        return max(1.0, float(os.environ.get("MIMIR_FACTORY_PROBE_WINDOW_S", "300")))
    except ValueError:
        return 300.0


def _epic_prompt(issue: "IssueContext") -> str:
    """The factory's START prompt for a worklink:epic issue.

    The run-id is passed as an argv boundary (``--run-id chainlink-<issue>``),
    not as prompt text. The factory namespaces its control plane under
    ``.opencode/factory/<run-id>/`` at the id the adapter observes.
    """
    header = f"Build chainlink #{issue.issue_id}: {issue.title}".strip()
    body = issue.description.strip()
    base = f"{header}\n\n{body}".strip() if body else header
    return base


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
            home_path=self.home,
        )
        # Re-read immediately before claiming so retries in a long-lived caller do
        # not use stale comments and collide with prior attempt-scoped branches.
        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)
        claim = claims.claim_issue(
            issue.issue_id,
            issue.comments,
            labels=issue.labels,
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
            # fail-loud backstop against that routing regressing. After #832 the
            # only compute substrate is local_subprocess (shared_filesystem=true),
            # so this guard fires for every codex run.
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
                # reattach after a container restart. local_subprocess work dies
                # with the controller, so nothing is persisted today; the
                # reaper remains the recovery net. After #832 no live compute
                # substrate is persistent, so this branch is dormant.
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
        if raw.output_overflow:
            _log_event(
                "worklink_output_overflow",
                issue_id=issue.issue_id,
                attempt=attempt,
                backend=selected_name,
                transcript=str(raw.transcript_path) if raw.transcript_path else None,
            )
        pr_url = None
        # After the #832 substrate cleanup local_subprocess is the only Worklink
        # compute substrate. Its capabilities declare shared_filesystem=True, so
        # the controller runs the diff/test re-derivation itself (no remote-fetch
        # gate, no folded trusted-test job).
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
                None if validation.review_ready else _local_gate_failure_tail(validation)
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
        transition_status = "blocked" if raw.output_overflow else validation.status
        transition_reason = (
            raw.error
            if raw.output_overflow
            else validation.evidence.blocked_reason
            if validation.status == "blocked"
            else (", ".join(validation.reasons) if validation.reasons else None)
        )
        claims.transition_issue(
            issue.issue_id,
            status=transition_status,
            review_ready=validation.review_ready,
            attempt=attempt,
            reason=transition_reason,
        )
        _log_event(
            "worklink_transition",
            issue_id=issue.issue_id,
            attempt=attempt,
            status=transition_status,
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
            transition_status,
            review_ready=validation.review_ready,
            pr_url=pr_url,
            evidence_path=evidence_path,
            worktree=lease.path,
            branch=lease.branch,
            reason=f"post-transition cleanup failed: {cleanup_error}" if cleanup_error else None,
        )

    async def reattach(self, issue_id: int) -> WorklinkRunResult:
        """Resume an in-flight run after a controller restart (#561).

        After the #832 substrate cleanup local_subprocess is the only Worklink
        compute substrate; its runs die with the controller, so no run state is
        ever persisted and ``reattach`` always returns ``failed`` with reason
        ``reattach: no run state``. The startup reconcile honors the same
        return — it has nothing to re-dispatch and the TTL reaper remains the
        recovery net. Kept as a no-op entry point so the CLI flag and the
        server-side reconcile API stay stable for older deployments that may
        still hold a ``<home>/state/worklink/runs/<id>.json`` from a prior
        docker-sibling / ecs-runtask run."""
        state = load_run_state(self.home, issue_id)
        if state is None:
            return WorklinkRunResult(issue_id, None, "failed", reason="reattach: no run state")

        runner = self.runner or _runner_for_home(self.home, self.chainlink_bin)
        claims = ChainlinkClaims(
            chainlink_bin=self.chainlink_bin,
            agent_id=self.agent_id,
            runner=_list_runner(runner),
            home_path=self.home,
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

    async def run_epic(
        self,
        issue_id: int,
        *,
        autonomous: bool = False,
    ) -> WorklinkRunResult:
        """Run a worklink:epic issue via the feature-factory adapter (#833).

        This is a separate path from run() because epics use the feature_factory
        backend, mirror state from the factory's run.json, and don't create leaf
        issues.
        """
        from .backends.feature_factory import (
            epic_run_id,
            has_concurrent_factory_session,
        )

        runner = self.runner or _runner_for_home(self.home, self.chainlink_bin)
        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)

        if "worklink:epic" not in issue.labels:
            _log_event(
                "worklink_epic_invalid",
                issue_id=issue_id,
                reason="issue does not have worklink:epic label",
            )
            return WorklinkRunResult(issue_id, None, "failed", reason="not an epic issue")

        config = WorklinkConfig.load(self.home / "worklink.yaml")
        registry = self.registry or BackendRegistry(config)
        repo_url = _repo_remote_url(self.repo, runner=runner)
        repo_slug = _repo_slug_from_url(repo_url)

        backend = registry.get("feature_factory")
        compute = registry.select_compute(labels=issue.labels, repo=repo_slug)

        if autonomous:
            allowed, reason = config.autonomous_compute_allowed(compute.name, compute.capabilities())
            if not allowed:
                _log_event(
                    "worklink_epic_autonomous_refused",
                    issue_id=issue_id,
                    compute_backend=compute.name,
                )
                return WorklinkRunResult(issue_id, None, "refused", reason=reason)

        claims = ChainlinkClaims(
            chainlink_bin=self.chainlink_bin,
            agent_id=self.agent_id,
            runner=_list_runner(runner),
            home_path=self.home,
        )

        if has_concurrent_factory_session(self.repo, exclude_run_id=epic_run_id(issue_id)):
            _log_event(
                "worklink_epic_concurrent",
                issue_id=issue_id,
                reason="another factory session is already running",
            )
            return WorklinkRunResult(issue_id, None, "blocked", reason="concurrent factory session")

        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)
        claim = claims.claim_issue(
            issue.issue_id,
            issue.comments,
            labels=issue.labels,
            max_active_locks=config.defaults.max_concurrent if autonomous else None,
        )
        if claim.attempts_exhausted:
            _log_event("worklink_epic_attempts_exhausted", issue_id=issue.issue_id)
            return WorklinkRunResult(issue.issue_id, None, "blocked", reason="attempts_exhausted")
        if not claim.claimed or claim.record is None:
            return WorklinkRunResult(
                issue.issue_id, None, "failed", reason=claim.reason or "claim_failed"
            )
        record = claim.record
        _log_event(
            "worklink_epic_claimed",
            issue_id=issue.issue_id,
            attempt=record.attempt,
        )

        try:
            base = config.defaults.base_branch
            lease = _create_backend_checkout(
                self.repo,
                issue_id=issue.issue_id,
                attempt=record.attempt,
                base=base,
                backend_name="feature_factory",
                compute_shared_filesystem=compute.capabilities().shared_filesystem,
                base_fetch=config.defaults.base_fetch,
                event_logger=_log_event,
                runner=_list_runner(runner),
            )

            order = WorkOrder(
                issue_id=issue.issue_id,
                worktree=lease.path,
                prompt=_epic_prompt(issue),
                rules=None,
                timeout_s=config.defaults.timeout_s,
                env={"MIMIR_HOME": str(self.home)},
                transcript_root=self.home / "state" / "worklink" / "transcripts",
            )
            return await self._run_detached_epic(
                issue=issue,
                claims=claims,
                record=record,
                backend=backend,
                compute=compute,
                order=order,
                lease=lease,
                repo_url=repo_url,
                test_cmd=config.defaults.test_command,
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
                "worklink_epic_transition",
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
            )
        finally:
            claims.release_issue(issue.issue_id)

    async def _run_detached_epic(
        self,
        *,
        issue: IssueContext,
        claims: ChainlinkClaims,
        record: ClaimRecord,
        backend: Any,
        compute: Any,
        order: WorkOrder,
        lease: WorktreeLease,
        repo_url: str | None,
        test_cmd: str,
        runner: Runner,
    ) -> WorklinkRunResult:
        """Launch the factory DETACHED once, poll run.json to terminal, finalize.

        ``factory start --autonomous --detached`` backgrounds opencode and the
        launcher returns immediately, so ``compute.wait`` completes fast (the
        launcher's exit, NOT the run's). The run-completion detector is therefore
        the poll loop over ``run.json``, not ``compute.wait``.

        Flow:
        - **Resume check (pre-launch):** read the current ``run.json``. If it is
          already TERMINAL, a prior dispatch's detached run finished → finalize
          straight away. If it is non-terminal AND non-stale, a detached factory
          is still running from a prior interrupted dispatch → SKIP the launch and
          just resume polling. Otherwise → launch.
        - **Launch (detached):** ``compute.launch`` + ``compute.wait`` (fast). A
          launch error or a non-zero launcher exit is a launch FAILURE (raise);
          the launcher's clean exit is NOT run completion.
        - **Poll:** ``_poll_factory_to_terminal`` mirrors transitions + probes
          liveness until the run is terminal / stuck / over the run-timeout, then
          ``_finalize_epic`` maps the outcome to Chainlink.
        """
        from .backends.feature_factory import epic_run_id, read_factory_run_state

        run_id = epic_run_id(issue.issue_id)

        # Resume check (before launching): a prior dispatch's detached factory may
        # already be terminal (finalize now) or still running (resume polling).
        existing = read_factory_run_state(order.worktree, run_id)
        if existing is not None and existing.is_terminal:
            _log_event(
                "worklink_epic_resume_terminal", issue_id=issue.issue_id, run_id=run_id
            )
            return await self._finalize_epic(
                issue=issue,
                claims=claims,
                attempt=record.attempt,
                worktree=order.worktree,
                lease=lease,
                state=existing,
                stuck_reason=None,
                runner=runner,
            )

        handle = None
        try:
            if existing is not None and not existing.is_stale:
                # A detached factory from a prior interrupted dispatch is still
                # live for this epic — resume polling instead of relaunching.
                _log_event(
                    "worklink_epic_resume_running",
                    issue_id=issue.issue_id,
                    run_id=run_id,
                    heartbeat_at=existing.heartbeat_at,
                )
            else:
                spec = backend.work_spec(
                    order,
                    attempt=record.attempt,
                    repo_url=repo_url,
                    base_ref=lease.base_ref,
                    branch=lease.branch,
                    test_command=test_cmd,
                )
                try:
                    handle = await compute.launch(spec)
                    launch_result = await compute.wait(handle, spec.timeout_s)
                except ComputeLaunchError as exc:
                    launch_result = ComputeResult(
                        exit_code=-1, stdout="", stderr=str(exc), launch_error=str(exc)
                    )
                # The detached launcher backgrounds opencode and exits ~immediately;
                # a launch error or non-zero launcher exit means it never spawned
                # the run. Do NOT treat the launcher's clean exit as run completion.
                if launch_result.launch_error or launch_result.exit_code != 0:
                    detail = (
                        launch_result.launch_error
                        or (launch_result.stderr or "").strip()
                        or f"launcher exited {launch_result.exit_code}"
                    )
                    raise WorklinkError(f"feature-factory launch failed: {detail}")

            state, stuck_reason = await self._poll_factory_to_terminal(
                claims=claims,
                record=record,
                worktree=order.worktree,
                run_id=run_id,
                issue=issue,
                poll_interval_s=float(getattr(backend, "poll_interval_s", 10) or 0),
            )
        finally:
            if handle is not None:
                await compute.cleanup(handle)

        return await self._finalize_epic(
            issue=issue,
            claims=claims,
            attempt=record.attempt,
            worktree=order.worktree,
            lease=lease,
            state=state,
            stuck_reason=stuck_reason,
            runner=runner,
        )

    async def _poll_factory_to_terminal(
        self,
        *,
        claims: ChainlinkClaims,
        record: ClaimRecord,
        worktree: Path,
        run_id: str,
        issue: IssueContext,
        poll_interval_s: float,
    ) -> tuple[Any, str | None]:
        """Poll ``run.json`` to a terminal state — the detached run's completion
        detector — mirroring transitions and probing liveness each tick.

        Returns ``(state, stuck_reason)``: on a terminal run.json, ``(state, None)``
        (``_finalize_epic`` prefers ``terminal_result``); on a probe-declared stuck
        run or an exceeded run-timeout, ``(state, reason)`` → finalized as failed.

        Each tick it (a) mirrors meaningful run.json transitions (gate approved,
        slice progress, panel verdict, PR opened) to Chainlink — only on CHANGE;
        (b) does probe-based liveness — a stale ``heartbeat_at`` TRIGGERS a probe,
        never an auto-fail. With ``--detached`` the launcher process is gone but
        the backgrounded factory child survives, so liveness is probed directly
        via ``_detached_factory_alive`` (scan for the child by worktree); a live
        child keeps waiting even when momentarily quiet (the pre_pr review panel),
        and only if liveness can't be determined does ``_epic_stuck_reason`` fall
        back to the file-activity signal. It heartbeats the claim best-effort each
        tick.
        """
        from .backends.feature_factory import read_factory_run_state

        memo = _new_factory_mirror_memo()
        stale_threshold_s = _epic_stale_heartbeat_s()
        probe_window_s = _epic_probe_window_s()
        run_timeout_s = _epic_run_timeout_s()
        started_at = datetime.now(UTC).timestamp()

        _heartbeat_claim_best_effort(claims, record)
        while True:
            state: Any = None
            try:
                state = read_factory_run_state(worktree, run_id)
                if state is not None:
                    for line in _factory_mirror_lines(state, memo):
                        _epic_comment(claims, issue.issue_id, line)
                        _log_event(
                            "worklink_epic_mirror", issue_id=issue.issue_id, note=line
                        )
                    if state.is_terminal or state.terminal_result is not None:
                        return state, None
            except Exception as exc:  # noqa: BLE001 - observation must not fail the run
                _log_event(
                    "worklink_epic_observe_error",
                    issue_id=issue.issue_id,
                    error=str(exc)[:300],
                )

            elapsed_s = max(0.0, datetime.now(UTC).timestamp() - started_at)
            # Detached: the launcher is gone, but the backgrounded factory child
            # survives (reparented). Probe for it directly by worktree so a quiet
            # review panel isn't mistaken for a hang; None (can't tell) falls back
            # to run-dir / process-log file activity.
            reason = _epic_stuck_reason(
                state=state,
                recent_activity_s=_run_dir_recent_activity_s(worktree, run_id),
                job_alive=_detached_factory_alive(worktree),
                elapsed_s=elapsed_s,
                stale_threshold_s=stale_threshold_s,
                probe_window_s=probe_window_s,
            )
            if reason is not None:
                _log_event("worklink_epic_stuck", issue_id=issue.issue_id, reason=reason)
                return state, reason
            if elapsed_s >= run_timeout_s:
                timeout_reason = f"factory exceeded run timeout ({run_timeout_s:.0f}s)"
                _log_event(
                    "worklink_epic_run_timeout",
                    issue_id=issue.issue_id,
                    reason=timeout_reason,
                )
                return state, timeout_reason

            await asyncio.sleep(poll_interval_s)
            _heartbeat_claim_best_effort(claims, record)

    def _epic_terminal_result(
        self,
        *,
        issue: IssueContext,
        claims: ChainlinkClaims,
        attempt: int,
        lease: WorktreeLease | None,
        status: str,
        reason: str | None,
        pr_url: str | None,
        runner: Runner,
    ) -> WorklinkRunResult:
        """Transition the epic to a non-shippable terminal status and clean up."""
        claims.transition_issue(
            issue.issue_id,
            status=status,
            review_ready=False,
            attempt=attempt,
            reason=reason,
        )
        _log_event(
            "worklink_epic_transition",
            issue_id=issue.issue_id,
            attempt=attempt,
            status=status,
            reason=reason,
            pr_url=pr_url,
        )
        if lease is not None:
            _cleanup_worktree_after_transition(
                lease,
                outcome=status,
                runner=_list_runner(runner),
                issue_id=issue.issue_id,
                attempt=attempt,
            )
        return WorklinkRunResult(
            issue.issue_id,
            attempt,
            status,
            review_ready=False,
            pr_url=pr_url,
            worktree=lease.path if lease else None,
            branch=lease.branch if lease else None,
            reason=reason,
        )

    async def _finalize_epic(
        self,
        *,
        issue: IssueContext,
        claims: ChainlinkClaims,
        attempt: int,
        worktree: Path,
        lease: WorktreeLease | None,
        state: Any,
        stuck_reason: str | None,
        runner: Runner,
    ) -> WorklinkRunResult:
        """Mirror the factory's terminal outcome to Chainlink, then clean up.

        Outcome (preferring ``run.json.terminal_result``, falling back to
        ``status``/``pr_url``/gates when it is absent):

        - ``completed`` (or run status completed) WITH a ``pr_url`` → the factory
          already opened + (via ``--ready``/``--reviewer``) promoted/requested
          review, so the adapter only MIRRORS: transition the epic to review,
          record the PR URL, and clean up. It does NOT re-run ``gh pr ready`` /
          ``--add-reviewer``.
        - ``blocked`` / ``needs-human`` / ``partial`` (or completed-without-PR) →
          transition to ``blocked`` with the terminal reason/summary (keeping any
          ``pr_url`` on the result for visibility).
        - probe-declared stuck, or no run.json → ``failed`` with the reason.
        """
        list_runner = _list_runner(runner)

        # Probe-declared stuck / no run.json → failed (the hard-ceiling wait or
        # the liveness probe fired).
        if stuck_reason:
            return self._epic_terminal_result(
                issue=issue,
                claims=claims,
                attempt=attempt,
                lease=lease,
                status="failed",
                reason=stuck_reason,
                pr_url=state.pr_url if state else None,
                runner=runner,
            )
        if state is None:
            return self._epic_terminal_result(
                issue=issue,
                claims=claims,
                attempt=attempt,
                lease=lease,
                status="failed",
                reason="factory produced no run.json",
                pr_url=None,
                runner=runner,
            )

        terminal = state.terminal_result
        outcome_status = (terminal.status if terminal else state.status).strip().lower()
        pr_url = (terminal.pr_url if terminal and terminal.pr_url else None) or state.pr_url

        if outcome_status == "completed" and pr_url:
            # The factory already opened + promoted/requested review on the PR;
            # just mirror the outcome to Chainlink (no duplicate gh calls).
            claims.transition_issue(
                issue.issue_id,
                status="review",
                review_ready=True,
                attempt=attempt,
            )
            _epic_comment(
                claims, issue.issue_id, f"factory completed; PR ready for review: {pr_url}"
            )
            _epic_comment(
                claims,
                issue.issue_id,
                f"WORKLINK_EVIDENCE issue={issue.issue_id} attempt={attempt} status=completed review_ready=true pr_url={pr_url}",
            )
            _log_event("worklink_epic_pr_opened", issue_id=issue.issue_id, pr_url=pr_url)
            if lease is not None:
                # The factory already pushed the branch + opened the PR; the local
                # attempt checkout is disposable. ``completed`` is cleanup_worktree's
                # remove-on-success sentinel (same as the leaf happy path).
                _cleanup_worktree_after_transition(
                    lease,
                    outcome="completed",
                    runner=list_runner,
                    issue_id=issue.issue_id,
                    attempt=attempt,
                )
            return WorklinkRunResult(
                issue.issue_id,
                attempt,
                "review_ready",
                review_ready=True,
                pr_url=pr_url,
                worktree=lease.path if lease else None,
                branch=lease.branch if lease else None,
            )

        # blocked / needs-human / partial / completed-without-PR → blocked, with
        # the factory's own terminal reason/summary as an actionable comment.
        reason = (
            (terminal.reason or terminal.summary if terminal else None)
            or state.error
            or (
                f"factory ended in status '{outcome_status or 'unknown'}'"
                + (" with a PR URL but not review-ready" if pr_url else " with no PR")
            )
        )
        return self._epic_terminal_result(
            issue=issue,
            claims=claims,
            attempt=attempt,
            lease=lease,
            status="blocked",
            reason=reason,
            pr_url=pr_url,
            runner=runner,
        )


def _epic_comment(claims: ChainlinkClaims, issue_id: int, text: str) -> None:
    """Mirror a factory-progress note to the Chainlink epic issue (best-effort)."""
    try:
        claims._run(  # noqa: SLF001 - Chainlink wrapper owns quoting/checks.
            "issue", "comment", str(issue_id), f"WORKLINK_EPIC {text}", check=False
        )
    except Exception as exc:  # noqa: BLE001 - a lost mirror comment must not fail the run.
        _log_event("worklink_epic_comment_failed", issue_id=issue_id, error=str(exc)[:300])


_FACTORY_SLICE_DONE_STATUSES = frozenset({"merged", "completed", "done"})


def _new_factory_mirror_memo() -> dict[str, Any]:
    """Fresh memo of what has already been mirrored, so transitions fire once."""
    return {
        "gates_approved": set(),
        "slices": None,
        "validator": None,
        "security": None,
        "pr_url": None,
    }


def _factory_mirror_lines(state: Any, memo: dict[str, Any]) -> list[str]:
    """Comment lines for meaningful run.json transitions since the last poll.

    Compares ``state`` against ``memo`` (which it mutates) so each transition —
    gate approved (story/brief/pre_pr), slice progress, panel verdict, draft PR
    opened — is mirrored exactly ONCE, even if intermediate polls are skipped
    (the comparison is net-change, and the tracked transitions are monotonic).
    """
    lines: list[str] = []

    for name, status in state.gate_statuses:
        if (status or "").strip().lower() == "approved" and name not in memo["gates_approved"]:
            memo["gates_approved"].add(name)
            lines.append(f"gate approved: {name}")

    if state.slices:
        total = len(state.slices)
        merged = sum(
            1
            for _sid, status in state.slices
            if (status or "").strip().lower() in _FACTORY_SLICE_DONE_STATUSES
        )
        summary = f"{merged}/{total}"
        if summary != memo["slices"]:
            memo["slices"] = summary
            lines.append(f"slices: {merged}/{total} merged")

    if state.validator_verdict and state.validator_verdict != memo["validator"]:
        memo["validator"] = state.validator_verdict
        lines.append(f"validator verdict: {state.validator_verdict}")
    if state.security_verdict and state.security_verdict != memo["security"]:
        memo["security"] = state.security_verdict
        lines.append(f"security verdict: {state.security_verdict}")

    if state.pr_url and state.pr_url != memo["pr_url"]:
        memo["pr_url"] = state.pr_url
        lines.append(f"draft PR opened: {state.pr_url}")

    return lines


def _heartbeat_age_s(state: Any) -> float | None:
    """Seconds since ``state.heartbeat_at``; None if absent/unparseable."""
    if state is None:
        return None
    raw = getattr(state, "heartbeat_at", "") or ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (datetime.now(UTC) - parsed).total_seconds()


def _fmt_age(age_s: float | None) -> str:
    return "unknown" if age_s is None else f"{age_s:.0f}s ago"


def _run_dir_recent_activity_s(worktree: Path, run_id: str) -> float | None:
    """Seconds since the most recently modified factory file. None if there is
    nothing to point to — i.e. no activity.

    Two sources, because with ``--detached`` the run's file activity shows up in
    two places: the per-run control plane
    (``.opencode/factory/<run-id>/`` — run.json + artifacts/reviews/evidence) AND
    the detached factory's process log
    (``.opencode/factory/processes/<ts>.log``, which advances as the backgrounded
    opencode writes to it while it runs). The most recent mtime across both is the
    liveness signal, so the probe keeps waiting on a stale ``heartbeat_at`` as long
    as EITHER is advancing.
    """
    from .backends.feature_factory import factory_run_dir

    run_dir = factory_run_dir(worktree, run_id)
    # processes/ is a sibling of the per-run dir under the factory root:
    # <worktree>/.opencode/factory/processes/ (run_dir is .../factory/<run-id>).
    processes_dir = run_dir.parent / "processes"
    newest = 0.0
    try:
        if run_dir.exists():
            for path in run_dir.rglob("*"):
                try:
                    if path.is_file():
                        newest = max(newest, path.stat().st_mtime)
                except OSError:
                    continue
        if processes_dir.is_dir():
            for log in processes_dir.glob("*.log"):
                try:
                    newest = max(newest, log.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        return None
    if newest <= 0.0:
        return None
    return max(0.0, datetime.now(UTC).timestamp() - newest)


# Factory runtime whose process we scan for detached-mode liveness. With
# ``--detached`` the launcher backgrounds this child and reparents it, so no
# compute job handle survives; we recognise the live child by its
# ``--dir <worktree>``.
_FACTORY_RUNTIME_HINT = "opencode"


def _cmdline_is_factory_child(cmdline: str, worktree: str) -> bool:
    """True if a process command line is the detached factory runtime working in
    ``worktree`` — the opencode child the launcher backgrounded with
    ``--dir <worktree>``."""
    return _FACTORY_RUNTIME_HINT in cmdline and worktree in cmdline


def _iter_proc_cmdlines() -> Iterator[str]:
    """Yield each process's command line from ``/proc`` (Linux). Best-effort:
    unreadable entries (permissions / exited mid-scan) are skipped."""
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        yield raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _detached_factory_alive(
    worktree: Path,
    *,
    cmdlines: Iterable[str] | None = None,
) -> bool | None:
    """Liveness for a DETACHED factory child, which the launcher backgrounds and
    reparents so no compute job handle survives.

    Scans process command lines for the factory runtime working in ``worktree``.
    Returns True when found. Returns None ("unknown") when it can't tell —
    non-Linux / no ``/proc``, or simply no match — so a scan miss is NEVER
    reported as dead (that would re-introduce the very false-positive this
    guards against). Genuine death is still caught by the file-activity fallback
    in ``_epic_stuck_reason`` and, ultimately, by the run timeout.
    """
    if cmdlines is None:
        if not Path("/proc").is_dir():
            return None
        cmdlines = _iter_proc_cmdlines()
    needle = str(worktree)
    for cmdline in cmdlines:
        if _cmdline_is_factory_child(cmdline, needle):
            return True
    return None


def _epic_stuck_reason(
    *,
    state: Any,
    recent_activity_s: float | None,
    job_alive: bool | None,
    elapsed_s: float,
    stale_threshold_s: float,
    probe_window_s: float,
) -> str | None:
    """Probe-based liveness decision. Returns a stuck reason, or None to keep
    waiting.

    A stale ``heartbeat_at`` (older than ``stale_threshold_s``) is a TRIGGER TO
    PROBE, never an auto-fail. On a stale heartbeat:

    - ``job_alive is False`` (KNOWN dead) → stuck immediately.
    - ``job_alive is True`` (KNOWN alive) → keep waiting. A live process is doing
      work even when it is momentarily quiet — the classic case is the pre_pr
      review panel, where the reviewer sub-agents run for minutes without writing
      run.json or the process log. Only the run timeout (enforced by the caller)
      bounds a live-but-slow process; file quiet must NOT fail it.
    - ``job_alive is None`` (UNKNOWN — e.g. a substrate with no liveness probe)
      → fall back to the file-activity signal: stuck only if no run-dir/process
      file advanced within ``probe_window_s``.

    A fresh heartbeat always keeps waiting.
    """
    advancing = recent_activity_s is not None and recent_activity_s <= probe_window_s

    if state is None:
        # No run.json yet: give the factory a startup grace window equal to the
        # stale threshold before probing.
        if elapsed_s <= stale_threshold_s:
            return None
        if job_alive is False:
            return "factory exited before writing run.json"
        if job_alive is True:
            return None  # alive, still starting up → run timeout is the ceiling
        if not advancing:
            return f"factory wrote no run.json within {stale_threshold_s:.0f}s of launch"
        return None

    age = _heartbeat_age_s(state)
    if age is not None and age <= stale_threshold_s:
        return None  # fresh heartbeat → healthy

    # Stale (or unparseable) heartbeat → probe, don't auto-fail.
    if job_alive is False:
        return f"factory process is not alive (heartbeat {_fmt_age(age)})"
    if job_alive is True:
        # Demonstrably alive — a quiet review panel is not a hang. Only the run
        # timeout bounds a live process.
        return None
    if not advancing:
        return (
            f"factory heartbeat stale ({_fmt_age(age)}) and no run-dir file advanced "
            f"within {probe_window_s:.0f}s"
        )
    return None


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


def run_worklink_epic(
    *,
    home: Path,
    repo: Path,
    issue_id: int,
    autonomous: bool = False,
) -> WorklinkRunResult:
    """Run a worklink:epic issue via the feature-factory adapter (#833).

    This is a separate entry point from run_worklink because epics:
    - Use the feature_factory backend instead of regular backends
    - Don't create leaf issues in Chainlink
    - Mirror progress from the factory's run.json
    - Handle gates through the factory's file protocol
    """
    return asyncio.run(
        WorklinkRunner(home=home, repo=repo).run_epic(
            issue_id,
            autonomous=autonomous,
        )
    )


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
    """Throwaway detached worktree reserved for post-restart reattach (#561).

    After the #832 substrate cleanup local_subprocess is the only Worklink
    compute, so this worktree is never actually written into by ``reattach``
    (the controller never reaches the live-worker branch-fetch path). Kept as
    a defensive shape so older deployments that hold a run-state file pointing
    at a docker-sibling / ecs worker can still resolve the observation
    worktree. Detached + a dedicated ``reattach-`` path so it never collides
    with the (possibly surviving) original attempt worktree."""
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


def _local_gate_failure_tail(validation: EvidenceValidation) -> str | None:
    """Best available gate-failure detail for the next dispatch's groomer (#815).

    After the #832 substrate cleanup the only compute substrate is
    local_subprocess, so the orchestrator itself runs the gate test and the
    failure detail lives in the folded evidence's TestResult summary. Returns
    ``None`` when nothing is known — review-ready runs and observation-skipped
    runs both reach here without a tail."""
    tests = validation.evidence.tests
    if tests is None or not tests.observed or not tests.exit_code or not tests.summary:
        return None
    return tests.summary


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
