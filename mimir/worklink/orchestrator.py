"""Worklink operator-run orchestrator.

The orchestrator owns deterministic state transitions around an untrusted tool
backend: validate the Chainlink leaf, claim it, create an attempt checkout,
render the work order, run the backend, observe evidence ourselves, push/open a
PR only after the evidence gate passes, then clean up and release the lock.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import unicodedata
import warnings
from typing import Any, Callable, Iterable, Mapping, Sequence

from .._rmtree import rmtree_missing_ok
from .backends import (
    BackendRegistry,
    CheckoutShape,
    OpenCodeBackend,
    ToolBackend,
    WorkOrder,
    WorklinkConfig,
    checkout_shape_for_backend,
)
from .compute import ComputeLaunchError, ComputeResult, LaunchHandle, LocalSubprocessComputeBackend
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
    process_is_alive,
    process_start_ticks,
    save_run_state,
)
from .checkout import CheckoutLease, cleanup_checkout, coding_enabled, create_isolated_checkout
from ..redaction import redact_text
from ..repository_config import RepositoryInventory
from ..secret_scan import secret_matches
from .safe_git import ControllerGitPublication
from .backends.feature_factory import FactoryStatus, FeatureFactoryBackend
from .factory_state import (
    FactoryRunRecord,
    factory_process_is_alive,
    factory_process_is_verified_dead,
    load_factory_record,
    save_factory_record,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]
_CLAIM_HEARTBEAT_INTERVAL_S = 60.0
_PR_BODY_SECTION_FILE = ".worklink-pr-body.md"
_PR_BODY_SECTION_MAX_BYTES = 4000
_PR_BODY_SECTION_TRUNCATED = "\n\n[Build summary truncated by Worklink.]"
_EVIDENCE_HEADING_RE = re.compile(r"(?im)^Worklink evidence:\s*$")

def _epic_run_timeout_s() -> float:
    try:
        value = float(os.environ.get("MIMIR_FACTORY_RUN_TIMEOUT_S", "43200"))
        return value if value > 0 else 43200.0
    except ValueError:
        return 43200.0


def _epic_stale_heartbeat_s() -> float:
    try:
        value = float(os.environ.get("MIMIR_FACTORY_STALE_HEARTBEAT_S", "900"))
        return value if value > 0 else 900.0
    except ValueError:
        return 900.0


def _epic_prompt(issue: "IssueContext") -> str:
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
    checkout: Path | None = None
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
    agent_id: str = field(
        default_factory=lambda: os.environ.get("MIMIR_WORKLINK_AGENT_ID") or "mimir-worklink"
    )
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
        inventory = RepositoryInventory.load(self.home / "repositories.yaml")
        registry = self.registry or BackendRegistry(config)
        repo_url = _repo_remote_url(self.repo, runner=runner)
        repo_slug = _repo_slug_from_url(repo_url)
        repository_config = inventory.repository(repo_slug) if inventory.declared else None
        backend = (
            registry.get(backend_name)
            if backend_name
            else registry.select(labels=issue.labels, repo=repo_slug)
        )
        compute = registry.select_compute(labels=issue.labels, repo=repo_slug)
        selected_name = backend.name
        worker_required = (
            coding_enabled()
            and isinstance(backend, OpenCodeBackend)
            and compute.name == "local_subprocess"
        )
        test_cmd = (
            test_command
            if test_command is not None
            else repository_config.test_command
            if repository_config is not None and repository_config.test_command is not None
            else config.defaults.test_command
        )
        template_path = _template_path(self.home)
        # Per-run override beats worklink.yaml, which beats the built-in "main".
        base = (
            base_branch
            or (repository_config.base_branch if repository_config is not None else None)
            or config.defaults.base_branch
        )

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
                checkout=self.repo / ".worklink" / f"{issue.issue_id}-DRYRUN",
                prompt=prompt,
                rules=None,
                timeout_s=config.defaults.timeout_s,
                transcript_root=self.home / "state" / "worklink" / "transcripts",
            )
            print(_format_work_order(order, backend=selected_name))
            print(f"\nBase branch: {base} (checkout cut from it; PR targets it)")
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
            event_logger=_log_event,
        )
        # Re-read immediately before claiming so retries in a long-lived caller do
        # not use stale comments and collide with prior attempt-scoped branches.
        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)
        predicted_attempt = claims.next_attempt(issue.comments)
        claiming_state_written = False

        def record_claiming() -> None:
            nonlocal claiming_state_written
            existing = load_run_state(self.home, issue.issue_id)
            if existing is not None and process_is_alive(existing):
                raise WorklinkError(f"live run state already exists for issue {issue.issue_id}")
            save_run_state(
                self.home,
                WorklinkRunState(
                    issue_id=issue.issue_id,
                    attempt=predicted_attempt,
                    backend=selected_name,
                    compute_name=compute.name,
                    handle_substrate="controller",
                    handle_identifier=str(os.getpid()),
                    branch="",
                    base_ref=base,
                    local_base=base,
                    repo=str(self.repo),
                    repo_url=repo_url,
                    test_command=test_cmd,
                    started_at=datetime.now(UTC).isoformat(),
                    process_start_ticks=process_start_ticks(os.getpid()),
                    phase="claiming",
                ),
            )
            claiming_state_written = True

        try:
            claim = claims.claim_issue(
                issue.issue_id,
                issue.comments,
                labels=issue.labels,
                max_active_locks=config.defaults.max_concurrent if autonomous else None,
                exclude_active_label="worklink:epic",
                before_claim=record_claiming,
            )
        except Exception:
            if claiming_state_written:
                clear_run_state(self.home, issue.issue_id)
            raise
        if claim.attempts_exhausted:
            if claiming_state_written:
                clear_run_state(self.home, issue.issue_id)
            _log_event("worklink_attempts_exhausted", issue_id=issue.issue_id)
            return WorklinkRunResult(issue.issue_id, None, "blocked", reason="attempts_exhausted")
        if not claim.claimed or claim.record is None:
            if claiming_state_written:
                clear_run_state(self.home, issue.issue_id)
            _log_event(
                "worklink_claim_failed",
                issue_id=issue.issue_id,
                reason=claim.reason or "claim_failed",
            )
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

        lease: CheckoutLease | None = None
        publication: ControllerGitPublication | None = None
        delete_authorized_checkout = False
        try:
            lease = _create_backend_checkout(
                self.repo,
                issue_id=issue.issue_id,
                attempt=record.attempt,
                base=base,
                backend=backend,
                base_fetch=config.defaults.base_fetch,
                event_logger=_log_event,
                runner=_list_runner(runner),
                worker_eligible=worker_required,
            )
            if worker_required:
                if lease.authorization is None:
                    raise WorklinkError("worker checkout did not provide authorization")
                if not isinstance(compute, LocalSubprocessComputeBackend):
                    raise WorklinkError("worker checkout requires local subprocess compute")
                compute = LocalSubprocessComputeBackend.for_authorized_checkout(lease.authorization)
                checkout_fd = lease.authorization.duplicate_fd()
                try:
                    publication = ControllerGitPublication.capture(
                        checkout_fd,
                        self.repo,
                        lease.branch,
                        self.home / "state" / "worklink" / "publication",
                    )
                finally:
                    os.close(checkout_fd)
            if not lease.isolated_checkout:
                _log_event(
                    "worklink_unsafe_backend_checkout",
                    issue_id=issue.issue_id,
                    attempt=record.attempt,
                    backend=selected_name,
                    compute_backend=compute.name,
                )
                return WorklinkRunResult(
                    issue.issue_id,
                    None,
                    "blocked",
                    reason=(
                        f"{selected_name} must run in an isolated checkout (own .git), "
                        "not a parent-pointing worktree, to avoid exposing other checkouts "
                        "(chainlink #517/#1019)"
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
                checkout=lease.path,
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
            invocation_model = spec.backend_config.get("model")
            _log_event(
                "worklink_backend_invocation",
                issue_id=issue.issue_id,
                attempt=record.attempt,
                backend=selected_name,
                model=invocation_model,
            )
            if spec.backend_config.get("model_diverged"):
                _log_event(
                    "worklink_model_divergence",
                    issue_id=issue.issue_id,
                    attempt=record.attempt,
                    backend=selected_name,
                    model=invocation_model,
                    configured_model=spec.backend_config.get("configured_model"),
                )
            async def invoke_backend() -> ComputeResult:
                handle = None
                try:
                    handle = await compute.launch(spec)
                    # Atomically replace the provisional controller record with
                    # the real cancellable worker handle immediately after spawn.
                    try:
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
                    except OSError as exc:
                        _log_event(
                            "worklink_run_state_persist_failed",
                            issue_id=issue.issue_id,
                            error=str(exc),
                        )
                        await compute.cancel(handle)
                        raise
                    return await _heartbeat_while(
                        compute.wait(handle, spec.timeout_s),
                        claims=claims,
                        record=record,
                    )
                except ComputeLaunchError as exc:
                    return ComputeResult(
                        exit_code=-1,
                        stdout="",
                        stderr=str(exc),
                        launch_error=str(exc),
                    )
                finally:
                    if handle is not None:
                        await compute.cleanup(handle)

            if isinstance(backend, OpenCodeBackend):
                compute_result = await backend.invoke_with_startup_retry(
                    invoke_backend,
                    issue_id=issue.issue_id,
                    checkout_snapshot=lambda: _checkout_snapshot(lease.path, runner=runner, publication=publication),
                    event_logger=_log_event,
                )
            else:
                compute_result = await invoke_backend()
            result = await self._finalize(
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
                publication=publication,
            )
            delete_authorized_checkout = bool(
                worker_required and result.review_ready and result.pr_url
            )
            if delete_authorized_checkout and publication is not None:
                publication.run("update-ref", "-d", f"refs/heads/{lease.branch}", check=True)
            return result
        except Exception as exc:
            try:
                claims.transition_issue(
                    issue.issue_id,
                    status="failed",
                    review_ready=False,
                    attempt=record.budget_attempt or record.attempt,
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
                checkout=lease.path if lease else None,
                branch=lease.branch if lease else None,
            )
        finally:
            try:
                _close_attempt_capabilities(
                    publication,
                    lease.authorization if lease is not None else None,
                    lease.path if lease is not None else None,
                    delete_checkout=delete_authorized_checkout,
                )
            finally:
                claims.release_issue(issue.issue_id)
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
        lease: CheckoutLease,
        spec: Any,
        started: datetime,
        test_cmd: str | None,
        root_dirty_before: Sequence[str],
        runner: Runner,
        publication: ControllerGitPublication | None = None,
    ) -> WorklinkRunResult:
        """Post-launch pipeline: interpret the worker result, observe evidence,
        open the PR on a passing gate, then transition + clean up.

        Extracted so both a fresh ``run`` and a post-restart ``reattach`` share
        the identical evidence/PR/transition path — the only difference between
        them is how ``compute_result`` was obtained (launch+wait vs. wait on a
        persisted handle)."""
        selected_name = backend.name
        raw = await backend.interpret(order, compute_result)
        pr_body_section = _read_pr_body_section(lease.path)
        invocation_model = spec.backend_config.get("model")
        executor_failed = raw.exit_code != 0
        # A backend may report failure without the executor process exiting
        # nonzero (chainlink #1152). ``failure_reason`` previously keyed off the
        # exit code alone, so such a run recorded status=failed with reason=null
        # and validate_evidence had to synthesize "reported failure without a
        # reason" (#1108/#1349). Whatever the backend judged, its own error text
        # is the reason; the exit code still decides whether the TEST GATE was
        # skipped, which is a separate question.
        backend_reported_failure = raw.backend_status not in {"success", "blocked"}
        if raw.output_overflow:
            _log_event(
                "worklink_output_overflow",
                issue_id=issue.issue_id,
                attempt=attempt,
                backend=selected_name,
                transcript=str(raw.transcript_path) if raw.transcript_path else None,
            )
        pr_url = None

        def persist_gate_handle(handle: LaunchHandle) -> None:
            _persist_run_state(
                self.home,
                issue=issue,
                attempt=attempt,
                backend_name=selected_name,
                compute=compute,
                handle=handle,
                lease=lease,
                repo=self.repo,
                repo_url=spec.repo_url,
                test_command=test_cmd,
                started_at=started,
            )

        # After the #832 substrate cleanup local_subprocess is the only Worklink
        # compute substrate. Its capabilities declare shared_filesystem=True, so
        # the controller runs the diff/test re-derivation itself (no remote-fetch
        # gate, no folded trusted-test job).
        validation = await observe_evidence(
            issue=issue.issue_id,
            attempt=attempt,
            backend=selected_name,
            branch=lease.branch,
            checkout=lease.path,
            started_at=started,
            base_ref=lease.local_base or lease.base_ref,
            backend_status=raw.backend_status,
            test_command=test_cmd,
            transcript=str(raw.transcript_path) if raw.transcript_path else None,
            blocked_reason=raw.blocked_reason,
            model=invocation_model,
            failure_reason=raw.error if (executor_failed or backend_reported_failure) else None,
            skip_test_reason="executor exited nonzero before the test gate" if executor_failed else None,
            runner=runner,
            safe_git=publication,
            work_spec=spec,
            compute=compute,
            on_gate_launch=persist_gate_handle,
        )
        validation = _with_outside_checkout_detection(
            validation,
            issue=issue.issue_id,
            attempt=attempt,
            root=self.repo,
            checkout=lease.path,
            runner=runner,
            root_dirty_before=root_dirty_before,
        )
        evidence_path = _write_evidence(self.home, validation.evidence)
        if validation.review_ready:
            _commit_checkout_changes(lease.path, issue, runner=runner, publication=publication)
            try:
                _ensure_clean_checkout(lease.path, runner=runner, publication=publication)
            except WorklinkError as exc:
                validation = _failed_validation(validation, str(exc))
            else:
                validation = await observe_evidence(
                    issue=issue.issue_id,
                    attempt=attempt,
                    backend=selected_name,
                    branch=lease.branch,
                    checkout=lease.path,
                    started_at=started,
                    base_ref=lease.local_base or lease.base_ref,
                    backend_status=raw.backend_status,
                    test_command=test_cmd,
                    transcript=str(raw.transcript_path) if raw.transcript_path else None,
                    blocked_reason=raw.blocked_reason,
                    model=invocation_model,
                    failure_reason=raw.error if (executor_failed or backend_reported_failure) else None,
                    skip_test_reason=(
                        "executor exited nonzero before the test gate" if executor_failed else None
                    ),
                    runner=runner,
                    safe_git=publication,
                    work_spec=spec,
                    compute=compute,
                    on_gate_launch=persist_gate_handle,
                )
                validation = _with_outside_checkout_detection(
                    validation,
                    issue=issue.issue_id,
                    attempt=attempt,
                    root=self.repo,
                    checkout=lease.path,
                    runner=runner,
                    root_dirty_before=root_dirty_before,
                )
                validation = _with_head_sha(validation, lease.path, runner=runner, publication=publication)
            evidence_path = _write_evidence(self.home, validation.evidence)
        if validation.review_ready:
            # chainlink #518: push from the checkout that OWNS the attempt
            # branch, not the parent repo. With the isolated-checkout shape
            # (#517) the branch + its commit live only inside ``lease.path``
            # (its own .git, with ``origin`` already pointed at the real
            # remote); pushing from ``self.repo`` fails with
            # "src refspec <branch> does not match any". This is also correct
            # for the legacy worktree shape, which shares the parent's refs.
            _git_push(lease.path, lease.branch, runner=runner, publication=publication)
            pr_url = _open_pr(
                self.repo,
                issue,
                lease.branch,
                validation.evidence,
                pr_body_section=pr_body_section,
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
            model=validation.evidence.model,
            failure_reason=validation.evidence.failure_reason,
        )
        transition_status = "blocked" if raw.output_overflow else validation.status
        transition_reason = (
            validation.evidence.failure_reason
            if raw.exit_code != 0
            else validation.evidence.blocked_reason
            if validation.status == "blocked"
            else (", ".join(validation.reasons) if validation.reasons else None)
        )
        claims.transition_issue(
            issue.issue_id,
            status=transition_status,
            review_ready=validation.review_ready,
            attempt=claim_record.budget_attempt or attempt,
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
        cleanup_error = None
        if publication is None:
            cleanup_error = _cleanup_checkout_after_transition(
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
            checkout=lease.path,
            branch=lease.branch,
            reason=(
                f"post-transition cleanup failed: {cleanup_error}"
                if cleanup_error
                else validation.evidence.failure_reason if raw.exit_code != 0 else None
            ),
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
            event_logger=_log_event,
        )
        if state.shim_pid is not None:
            handle = LaunchHandle(
                state.handle_substrate,
                state.handle_identifier,
                state.process_start_ticks,
                state.shim_pid,
            )
            reason = "reattach: worker interrupted by controller restart"
            try:
                if not process_is_alive(state):
                    reason = "reattach: worker shim identity is stale"
                else:
                    await LocalSubprocessComputeBackend().cancel(handle)
            except (KeyError, RuntimeError, OSError, ValueError) as exc:
                reason = f"reattach: worker cleanup failed: {exc}"
            try:
                claims.transition_issue(
                    issue_id,
                    status="failed",
                    review_ready=False,
                    attempt=state.attempt,
                    reason=reason,
                )
            finally:
                claims.release_issue(issue_id)
                clear_run_state(self.home, issue_id)
            _log_event(
                "worklink_reattach_cleanup",
                issue_id=issue_id,
                attempt=state.attempt,
                reason=reason,
            )
            return WorklinkRunResult(
                issue_id,
                state.attempt,
                "failed",
                checkout=Path(state.checkout) if state.checkout else None,
                branch=state.branch,
                reason=reason,
            )

        review_ready = claims.review_ready_evidence(issue_id)
        if review_ready is not None:
            try:
                pr_url = str(review_ready.payload["pr_url"])
                pr_state, remote_head = _reattach_pr_state(pr_url, runner=runner)
                expected_head = review_ready.payload.get("head_sha")
                if not expected_head or not remote_head or remote_head != expected_head:
                    _log_event(
                        "worklink_reattach_branch_mismatch",
                        level="warning",
                        issue_id=issue_id,
                        attempt=state.attempt,
                        branch=state.branch,
                        expected_head=expected_head,
                        remote_head=remote_head,
                        reason=(
                            "final_state_not_recorded"
                            if not expected_head
                            else "remote_head_mismatch"
                        ),
                    )
                restored_review = pr_state == "OPEN"
                if restored_review:
                    claims.transition_issue(
                        issue_id,
                        status="completed",
                        review_ready=True,
                        attempt=state.attempt,
                    )
                _log_event(
                    "worklink_reattach_reconciled",
                    issue_id=issue_id,
                    attempt=state.attempt,
                    evidence_path=str(review_ready.path),
                    pr_url=pr_url,
                    pr_state=pr_state,
                    review_label_restored=restored_review,
                )
                return WorklinkRunResult(
                    issue_id,
                    state.attempt,
                    "completed",
                    review_ready=restored_review,
                    pr_url=pr_url,
                    evidence_path=review_ready.path,
                    branch=state.branch,
                    reason="reattach: reconciled completed evidence",
                )
            finally:
                claims.release_issue(issue_id)
                clear_run_state(self.home, issue_id)
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

        handle = LaunchHandle(
            state.handle_substrate,
            state.handle_identifier,
            state.process_start_ticks,
            state.shim_pid,
        )
        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)
        test_cmd = state.test_command
        _log_event(
            "worklink_reattach",
            issue_id=issue_id,
            attempt=state.attempt,
            compute_backend=compute.name,
            job=state.handle_identifier,
        )

        lease: CheckoutLease | None = None
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
                checkout=lease.path,
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
        return await self._run_factory_070(issue_id, autonomous=autonomous)

    async def _run_factory_070(
        self,
        issue_id: int,
        *,
        autonomous: bool,
    ) -> WorklinkRunResult:
        from .autonomy import factory_max_concurrent

        runner = self.runner or _runner_for_home(self.home, self.chainlink_bin)
        issue_reader = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner)
        issue = issue_reader.read(issue_id)
        if "worklink:epic" not in issue.labels:
            return WorklinkRunResult(issue_id, None, "failed", reason="not an epic issue")
        config = WorklinkConfig.load(self.home / "worklink.yaml")
        registry = self.registry or BackendRegistry(config)
        selected = registry.get("feature_factory")
        if not isinstance(selected, FeatureFactoryBackend):
            raise WorklinkError("feature_factory backend has an invalid implementation")
        repo_url = _repo_remote_url(self.repo, runner=runner)
        repo_slug = _repo_slug_from_url(repo_url)
        compute = registry.select_compute(labels=issue.labels, repo=repo_slug)
        if compute.name != "local_subprocess":
            raise WorklinkError("factory runs require local_subprocess supervision")
        if autonomous:
            allowed, reason = config.autonomous_compute_allowed(
                compute.name, compute.capabilities()
            )
            if not allowed:
                return WorklinkRunResult(issue_id, None, "refused", reason=reason)
        launcher = selected.admit()
        inventory = RepositoryInventory.load(self.home / "repositories.yaml")
        repository_config = inventory.repository(repo_slug) if inventory.declared else None
        base = (
            repository_config.base_branch
            if repository_config is not None
            else config.defaults.base_branch
        )
        test_cmd = (
            repository_config.test_command
            if repository_config is not None and repository_config.test_command is not None
            else config.defaults.test_command
        )
        claims = ChainlinkClaims(
            chainlink_bin=self.chainlink_bin,
            agent_id=self.agent_id,
            runner=_list_runner(runner),
            home_path=self.home,
            event_logger=_log_event,
        )
        issue = issue_reader.read(issue_id)
        claim = claims.claim_issue(
            issue_id,
            issue.comments,
            labels=issue.labels,
            max_active_locks=factory_max_concurrent() if autonomous else None,
            active_label="worklink:epic",
        )
        if claim.attempts_exhausted:
            return WorklinkRunResult(issue_id, None, "blocked", reason="attempts_exhausted")
        if not claim.claimed or claim.record is None:
            return WorklinkRunResult(
                issue_id, None, "failed", reason=claim.reason or "claim_failed"
            )
        claim_record = claim.record
        retained = load_factory_record(self.home, str(issue_id))
        lease: CheckoutLease | None = None
        try:
            if retained is not None:
                return await self._recover_factory_070(
                    issue=issue,
                    claim_record=claim_record,
                    claims=claims,
                    backend=selected,
                    compute=compute,
                    retained=retained,
                    launcher=launcher,
                    repo_slug=repo_slug,
                    base=base,
                    test_cmd=test_cmd,
                    runner=runner,
                )
            lease = _create_backend_checkout(
                self.repo,
                issue_id=issue_id,
                attempt=claim_record.attempt,
                base=base,
                backend=selected,
                base_fetch=config.defaults.base_fetch,
                event_logger=_log_event,
                runner=_list_runner(runner),
            )
            order = WorkOrder(
                issue_id=issue_id,
                checkout=lease.path,
                prompt=_epic_prompt(issue),
                rules=None,
                timeout_s=int(_epic_run_timeout_s()),
                env={"MIMIR_HOME": str(self.home)},
                transcript_root=self.home / "state" / "worklink" / "transcripts",
            )
            spec = selected.work_spec(
                order,
                attempt=claim_record.attempt,
                repo_url=repo_url,
                base_ref=lease.base_ref,
                branch=lease.branch,
                test_command=test_cmd,
            )
            handle = await compute.launch(spec)
            factory_record = FactoryRunRecord(
                run_id=str(issue_id),
                issue_id=issue_id,
                attempt=claim_record.attempt,
                repository=repo_slug,
                base_ref=base,
                branch=lease.branch,
                launcher=str(launcher),
                sandbox=str(lease.path),
                session=None,
                handle=handle,
                status=None,
                observed_at=None,
                controller_phase="running",
            )
            save_factory_record(self.home, factory_record)
            return await self._supervise_factory_070(
                issue=issue,
                claim_record=claim_record,
                claims=claims,
                backend=selected,
                compute=compute,
                factory_record=factory_record,
                test_cmd=test_cmd,
                runner=runner,
                started_at=datetime.now(UTC),
            )
        except Exception as exc:
            current = load_factory_record(self.home, str(issue_id))
            if current is not None:
                save_factory_record(
                    self.home,
                    replace(
                        current,
                        controller_phase="failed",
                        controller_error=str(exc),
                    ),
                )
            claims.transition_issue(
                issue_id,
                status="failed",
                review_ready=False,
                attempt=claim_record.budget_attempt or claim_record.attempt,
                reason=str(exc),
            )
            return WorklinkRunResult(
                issue_id,
                claim_record.attempt,
                "failed",
                checkout=Path(current.sandbox) if current is not None else None,
                branch=current.branch if current is not None else None,
                reason=str(exc),
            )
        finally:
            claims.release_issue(issue_id)

    async def _recover_factory_070(
        self,
        *,
        issue: IssueContext,
        claim_record: ClaimRecord,
        claims: ChainlinkClaims,
        backend: FeatureFactoryBackend,
        compute: Any,
        retained: FactoryRunRecord,
        launcher: Path,
        repo_slug: str,
        base: str,
        test_cmd: str,
        runner: Runner,
    ) -> WorklinkRunResult:
        if (
            retained.issue_id != issue.issue_id
            or retained.run_id != str(issue.issue_id)
            or retained.repository != repo_slug
            or retained.base_ref != base
            or retained.launcher != str(launcher)
        ):
            raise WorklinkError("retained factory identity does not match recovery request")
        sandbox = Path(retained.sandbox)
        if not sandbox.is_absolute() or not sandbox.is_dir() or sandbox.is_symlink():
            raise WorklinkError("retained factory sandbox is unavailable")
        pre = backend.status(retained.run_id, sandbox=sandbox, launcher=retained.launcher)
        _require_factory_status(pre, retained)
        historical_result = pre.terminal_result
        if pre.is_terminal:
            retained = retained.observed(pre, datetime.now(UTC).isoformat())
            save_factory_record(self.home, retained)
            return await self._finish_factory_070(
                issue=issue,
                claim_record=claim_record,
                claims=claims,
                backend=backend,
                compute=compute,
                factory_record=retained,
                test_cmd=test_cmd,
                runner=runner,
                started_at=datetime.now(UTC),
            )
        if factory_process_is_alive(retained):
            raise WorklinkError("factory recovery refuses a live retained process")
        if not factory_process_is_verified_dead(retained):
            raise WorklinkError("factory recovery cannot verify the retained process is dead")
        session = retained.session
        if not session:
            raise WorklinkError("retained factory session is missing")
        if pre.lock == "absent":
            backend.lock(
                retained.run_id,
                "claim",
                session=session,
                sandbox=sandbox,
                launcher=retained.launcher,
            )
        elif pre.lock == "fresh" and pre.lock_session != session:
            raise WorklinkError("factory recovery found a fresh foreign lock owner")
        elif pre.lock == "stale" or pre.dead_lock:
            backend.lock(
                retained.run_id,
                "steal",
                session=session,
                sandbox=sandbox,
                launcher=retained.launcher,
            )
        locked = backend.status(
            retained.run_id, sandbox=sandbox, launcher=retained.launcher
        )
        _require_factory_status(locked, retained)
        if (
            locked.lock != "fresh"
            or locked.lock_session != session
            or locked.terminal_result != historical_result
        ):
            raise WorklinkError("factory recovery lock reconciliation failed")
        resumed = backend.resume(
            retained.run_id,
            session=session,
            sandbox=sandbox,
            launcher=retained.launcher,
        )
        _require_factory_status(resumed, retained)
        if (
            resumed.status != "running"
            or resumed.lock != "fresh"
            or resumed.lock_session != session
            or resumed.terminal_result != historical_result
        ):
            raise WorklinkError("factory resume did not return an owned running status")
        resumed.require_recovery_next()
        _verify_factory_checkout(sandbox, retained.branch, retained.base_ref, runner)
        order = WorkOrder(
            issue_id=issue.issue_id,
            checkout=sandbox,
            prompt=_epic_prompt(issue),
            rules=None,
            timeout_s=int(_epic_run_timeout_s()),
            env={"MIMIR_HOME": str(self.home)},
            transcript_root=self.home / "state" / "worklink" / "transcripts",
        )
        spec = backend.work_spec(
            order,
            attempt=retained.attempt,
            repo_url=_repo_remote_url(self.repo, runner=runner),
            base_ref=retained.base_ref,
            branch=retained.branch,
            test_command=test_cmd,
        )
        handle = await compute.launch(spec)
        relaunched = replace(
            retained.observed(resumed, datetime.now(UTC).isoformat()),
            handle=handle,
            controller_phase="running",
        )
        save_factory_record(self.home, relaunched)
        return await self._supervise_factory_070(
            issue=issue,
            claim_record=claim_record,
            claims=claims,
            backend=backend,
            compute=compute,
            factory_record=relaunched,
            test_cmd=test_cmd,
            runner=runner,
            started_at=datetime.now(UTC),
        )

    async def _supervise_factory_070(
        self,
        *,
        issue: IssueContext,
        claim_record: ClaimRecord,
        claims: ChainlinkClaims,
        backend: FeatureFactoryBackend,
        compute: Any,
        factory_record: FactoryRunRecord,
        test_cmd: str,
        runner: Runner,
        started_at: datetime,
    ) -> WorklinkRunResult:
        deadline = asyncio.get_running_loop().time() + _epic_run_timeout_s()
        stale_after = _epic_stale_heartbeat_s()
        last_status: FactoryStatus | None = None
        last_change = asyncio.get_running_loop().time()
        while True:
            status = backend.status(
                factory_record.run_id,
                sandbox=Path(factory_record.sandbox),
                launcher=factory_record.launcher,
            )
            _require_factory_status(status, factory_record)
            if status != last_status:
                last_status = status
                last_change = asyncio.get_running_loop().time()
            if factory_record.session is not None and status.lock_session not in {
                None,
                factory_record.session,
            }:
                raise WorklinkError("factory lock owner changed")
            factory_record = factory_record.observed(status, datetime.now(UTC).isoformat())
            phase = "parked" if status.is_parked else "terminal" if status.is_terminal else "running"
            factory_record = replace(factory_record, controller_phase=phase)
            save_factory_record(self.home, factory_record)
            if status.lock == "fresh" and factory_record.session == status.lock_session:
                backend.heartbeat(
                    factory_record.run_id,
                    session=factory_record.session,
                    sandbox=Path(factory_record.sandbox),
                    launcher=factory_record.launcher,
                )
            if asyncio.get_running_loop().time() - last_change >= stale_after:
                _log_event(
                    "worklink_factory_stale_status",
                    issue_id=issue.issue_id,
                    diagnostic_after_s=stale_after,
                    lock=status.lock,
                    process_alive=(
                        factory_record.handle is not None
                        and compute.job_alive(factory_record.handle)
                    ),
                )
            if status.is_terminal or status.is_parked:
                if factory_record.handle is not None and compute.job_alive(factory_record.handle):
                    await compute.cancel(factory_record.handle)
                try:
                    return await self._finish_factory_070(
                        issue=issue,
                        claim_record=claim_record,
                        claims=claims,
                        backend=backend,
                        compute=compute,
                        factory_record=factory_record,
                        test_cmd=test_cmd,
                        runner=runner,
                        started_at=started_at,
                    )
                finally:
                    if factory_record.handle is not None:
                        await compute.cleanup(factory_record.handle)
            if factory_record.handle is None or not compute.job_alive(factory_record.handle):
                raise WorklinkError("OpenCode process exited while factory status was running")
            if asyncio.get_running_loop().time() >= deadline:
                await compute.cancel(factory_record.handle)
                raise WorklinkError(
                    f"factory exceeded run timeout ({_epic_run_timeout_s():.0f}s)"
                )
            _heartbeat_claim_best_effort(claims, claim_record)
            await asyncio.sleep(max(0.01, float(backend.poll_interval_s)))

    async def _finish_factory_070(
        self,
        *,
        issue: IssueContext,
        claim_record: ClaimRecord,
        claims: ChainlinkClaims,
        backend: FeatureFactoryBackend,
        compute: Any,
        factory_record: FactoryRunRecord,
        test_cmd: str,
        runner: Runner,
        started_at: datetime,
    ) -> WorklinkRunResult:
        status = factory_record.status
        if status is None:
            raise WorklinkError("factory terminal projection is missing")
        if status.is_parked:
            return WorklinkRunResult(
                issue.issue_id,
                factory_record.attempt,
                "needs-human",
                checkout=Path(factory_record.sandbox),
                branch=factory_record.branch,
                reason="factory run is parked",
            )
        if status.status in {"blocked", "partial"}:
            claims.transition_issue(
                issue.issue_id,
                status="blocked",
                review_ready=False,
                attempt=claim_record.budget_attempt or claim_record.attempt,
                reason=f"factory status: {status.status}",
            )
            return WorklinkRunResult(
                issue.issue_id,
                factory_record.attempt,
                "blocked",
                pr_url=status.pr_url,
                checkout=Path(factory_record.sandbox),
                branch=factory_record.branch,
                reason=f"factory status: {status.status}",
            )
        evidence_path, pr_url = await _verify_factory_completion(
            home=self.home,
            issue=issue,
            record=factory_record,
            test_command=test_cmd,
            started_at=started_at,
            runner=runner,
        )
        claims.transition_issue(
            issue.issue_id,
            status="review",
            review_ready=True,
            attempt=claim_record.budget_attempt or claim_record.attempt,
        )
        return WorklinkRunResult(
            issue.issue_id,
            factory_record.attempt,
            "review_ready",
            review_ready=True,
            pr_url=pr_url,
            evidence_path=evidence_path,
            checkout=Path(factory_record.sandbox),
            branch=factory_record.branch,
        )

    async def _run_detached_epic(
        self,
        *,
        issue: IssueContext,
        claims: ChainlinkClaims,
        record: ClaimRecord,
        backend: Any,
        compute: Any,
        order: WorkOrder,
        lease: CheckoutLease,
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
        from .backends.feature_factory import epic_run_id

        run_id = epic_run_id(issue.issue_id)

        # Resume check (before launching): a prior dispatch's detached factory may
        # already be terminal (finalize now) or still running (resume polling).
        existing = None
        if existing is not None and existing.is_terminal:
            _log_event(
                "worklink_epic_resume_terminal", issue_id=issue.issue_id, run_id=run_id
            )
            return await self._finalize_epic(
                issue=issue,
                claims=claims,
                attempt=record.attempt,
                budget_attempt=record.budget_attempt or record.attempt,
                checkout=order.checkout,
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
                checkout=order.checkout,
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
            budget_attempt=record.budget_attempt or record.attempt,
            checkout=order.checkout,
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
        checkout: Path,
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
        via ``_detached_factory_alive`` (scan for the child by checkout); a live
        child keeps waiting even when momentarily quiet (the pre_pr review panel),
        and only if liveness can't be determined does ``_epic_stuck_reason`` fall
        back to the file-activity signal. It heartbeats the claim best-effort each
        tick.
        """
        memo = _new_factory_mirror_memo()
        stale_threshold_s = _epic_stale_heartbeat_s()
        probe_window_s = _epic_probe_window_s()
        run_timeout_s = _epic_run_timeout_s()
        started_at = datetime.now(UTC).timestamp()

        _heartbeat_claim_best_effort(claims, record)
        while True:
            state: Any = None
            try:
                state = None
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
            # survives (reparented). Probe for it directly by checkout so a quiet
            # review panel isn't mistaken for a hang; None (can't tell) falls back
            # to run-dir / process-log file activity.
            reason = _epic_stuck_reason(
                state=state,
                recent_activity_s=_run_dir_recent_activity_s(checkout, run_id),
                job_alive=_detached_factory_alive(checkout),
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
        budget_attempt: int,
        lease: CheckoutLease | None,
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
            attempt=budget_attempt,
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
            _cleanup_checkout_after_transition(
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
            checkout=lease.path if lease else None,
            branch=lease.branch if lease else None,
            reason=reason,
        )

    async def _finalize_epic(
        self,
        *,
        issue: IssueContext,
        claims: ChainlinkClaims,
        attempt: int,
        budget_attempt: int,
        checkout: Path,
        lease: CheckoutLease | None,
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
                budget_attempt=budget_attempt,
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
                budget_attempt=budget_attempt,
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
                attempt=budget_attempt,
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
                # attempt checkout is disposable. ``completed`` is cleanup_checkout's
                # remove-on-success sentinel (same as the leaf happy path).
                _cleanup_checkout_after_transition(
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
                checkout=lease.path if lease else None,
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
            budget_attempt=budget_attempt,
            lease=lease,
            status="blocked",
            reason=reason,
            pr_url=pr_url,
            runner=runner,
        )


def _require_factory_status(status: FactoryStatus, record: FactoryRunRecord) -> None:
    if not status.valid:
        raise WorklinkError("factory status is invalid")
    if status.run_id != record.run_id or status.issue_key != str(record.issue_id):
        raise WorklinkError("factory status identity mismatch")
    if status.sandbox_path != record.sandbox:
        raise WorklinkError("factory status sandbox mismatch")
    if status.mode != "autonomous":
        raise WorklinkError("factory status mode mismatch")
    if status.branch != record.branch or status.pr_base != record.base_ref:
        raise WorklinkError("factory status branch or base mismatch")


def _fixed_command(
    runner: Runner,
    args: Sequence[str],
    *,
    error: str,
) -> subprocess.CompletedProcess[str]:
    result = runner(list(args))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise WorklinkError(detail or error)
    return result


def _verify_factory_checkout(
    sandbox: Path,
    branch: str,
    base_ref: str,
    runner: Runner,
) -> str:
    observed_branch = _fixed_command(
        runner,
        ["git", "-C", str(sandbox), "branch", "--show-current"],
        error="cannot read factory checkout branch",
    ).stdout.strip()
    if observed_branch != branch:
        raise WorklinkError("factory checkout branch mismatch")
    base = _fixed_command(
        runner,
        ["git", "-C", str(sandbox), "rev-parse", "--verify", base_ref],
        error="cannot resolve factory checkout base",
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base):
        raise WorklinkError("factory checkout base is invalid")
    head = _fixed_command(
        runner,
        ["git", "-C", str(sandbox), "rev-parse", "HEAD"],
        error="cannot read factory checkout HEAD",
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise WorklinkError("factory checkout HEAD is invalid")
    return head


_CANONICAL_PR_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)\Z"
)


async def _verify_factory_completion(
    *,
    home: Path,
    issue: IssueContext,
    record: FactoryRunRecord,
    test_command: str,
    started_at: datetime,
    runner: Runner,
) -> tuple[Path, str]:
    status = record.status
    if status is None or status.status != "completed" or not status.is_terminal:
        raise WorklinkError("factory completion status is not authoritative")
    _require_factory_status(status, record)
    if status.pr_draft:
        raise WorklinkError("factory completed with a draft PR")
    if status.pr_url is None:
        raise WorklinkError("factory completed without a PR URL")
    match = _CANONICAL_PR_URL.fullmatch(status.pr_url)
    if match is None:
        raise WorklinkError("factory completed with a noncanonical PR URL")
    expected_repo = f"{match.group(1)}/{match.group(2)}".lower()
    if expected_repo != record.repository.lower():
        raise WorklinkError("factory PR repository mismatch")
    sandbox = Path(record.sandbox)
    before_head = _verify_factory_checkout(
        sandbox, record.branch, record.base_ref, runner
    )
    validation = await observe_evidence(
        issue=issue.issue_id,
        attempt=record.attempt,
        backend="feature_factory",
        branch=record.branch,
        checkout=sandbox,
        started_at=started_at,
        base_ref=record.base_ref,
        backend_status="completed",
        test_command=test_command,
        pr_url=status.pr_url,
        runner=runner,
    )
    tests = validation.evidence.tests
    if (
        not validation.review_ready
        or tests is None
        or not tests.observed
        or tests.skipped_reason is not None
        or tests.exit_code != 0
        or not validation.evidence.files_changed
    ):
        _write_evidence(
            home,
            replace(
                validation.evidence,
                status="failed",
                failure_reason="factory completion lacks passing repository test evidence",
            ),
        )
        raise WorklinkError("factory completion lacks passing repository test evidence")
    clean = _fixed_command(
        runner,
        [
            "git",
            "-C",
            str(sandbox),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        error="cannot verify factory checkout cleanliness",
    )
    if clean.stdout.strip():
        raise WorklinkError("factory checkout is not clean")
    after_head = _verify_factory_checkout(
        sandbox, record.branch, record.base_ref, runner
    )
    if before_head != after_head:
        raise WorklinkError("factory checkout HEAD moved during evidence collection")
    evidence = replace(validation.evidence, head_sha=after_head)
    api = _fixed_command(
        runner,
        ["gh", "api", f"repos/{record.repository}/pulls/{match.group(3)}"],
        error="GitHub PR verification failed",
    )
    try:
        payload = json.loads(api.stdout)
    except json.JSONDecodeError as exc:
        raise WorklinkError("GitHub PR verification returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise WorklinkError("GitHub PR verification returned invalid data")
    base = payload.get("base")
    head = payload.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        raise WorklinkError("GitHub PR verification omitted refs")
    base_repo = base.get("repo")
    head_repo = head.get("repo")
    if not isinstance(base_repo, dict) or not isinstance(head_repo, dict):
        raise WorklinkError("GitHub PR verification omitted repositories")
    checks = (
        payload.get("html_url") == status.pr_url,
        payload.get("state") == "open",
        payload.get("draft") is False,
        str(base_repo.get("full_name") or "").lower() == record.repository.lower(),
        base.get("ref") == record.base_ref,
        str(head_repo.get("full_name") or "").lower() == record.repository.lower(),
        head.get("ref") == record.branch,
        head.get("sha") == after_head,
        evidence.head_sha == after_head,
    )
    if not all(checks):
        raise WorklinkError("GitHub PR identity verification failed")
    final_head = _verify_factory_checkout(
        sandbox, record.branch, record.base_ref, runner
    )
    if final_head != after_head:
        raise WorklinkError("factory checkout HEAD moved during publication verification")
    evidence_path = _write_evidence(home, evidence)
    return evidence_path, status.pr_url


def _close_attempt_capabilities(
    publication: ControllerGitPublication | None,
    authorization: Any | None,
    checkout: Path | None,
    *,
    delete_checkout: bool,
) -> None:
    try:
        if publication is not None:
            publication.close()
    finally:
        try:
            if authorization is not None:
                authorization.close()
        finally:
            if delete_checkout and checkout is not None:
                rmtree_missing_ok(checkout.parent)


def _cleanup_checkout_after_transition(
    lease: CheckoutLease,
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
        cleanup_checkout(lease, outcome=outcome, runner=runner)
    except Exception as exc:  # pragma: no cover - exact exception type is platform/git dependent.
        error = str(exc)
        _log_event(
            "worklink_cleanup_failed",
            issue_id=issue_id,
            attempt=attempt,
            outcome=outcome,
            checkout=str(lease.path),
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
    try:
        result = asyncio.run(
            WorklinkRunner(home=home, repo=repo).run(
                issue_id,
                backend_name=backend,
                dry_run=dry_run,
                test_command=test_command,
                base_branch=base_branch,
                autonomous=autonomous,
            )
        )
    except Exception as exc:
        _record_run_failure(
            issue_id=issue_id,
            attempt=None,
            error=exc,
            exit_status=2 if isinstance(exc, LeafValidationError) else 1,
            autonomous=autonomous,
        )
        raise
    if result.status == "failed":
        _record_run_failure(
            issue_id=issue_id,
            attempt=result.attempt,
            error=result.reason or "Worklink run failed",
            exit_status=1,
            autonomous=autonomous,
        )
    elif result.status in {"completed", "blocked"}:
        _record_run_success(issue_id)
    return result


def _record_run_failure(
    *,
    issue_id: int,
    attempt: int | None,
    error: BaseException | str,
    exit_status: int,
    autonomous: bool,
) -> None:
    from .dispatch_failures import record_failure, terminal_error

    safe_error = terminal_error(error)
    _log_event(
        "worklink_run_failed",
        issue_id=issue_id,
        attempt=attempt,
        attempt_consumed=attempt is not None,
        exit_status=exit_status,
        terminal_error=safe_error,
    )
    state_dir = os.environ.get("STATE_DIR")
    if autonomous and state_dir:
        try:
            record_failure(
                Path(state_dir),
                issue_id=issue_id,
                attempt=attempt,
                exit_status=exit_status,
                error=error,
                log_path=os.environ.get("WORKLINK_RUN_LOG"),
            )
        except OSError:
            pass


def _record_run_success(issue_id: int) -> None:
    from .dispatch_failures import record_success

    state_dir = os.environ.get("STATE_DIR")
    if state_dir:
        try:
            record_success(Path(state_dir), issue_id)
        except OSError:
            pass


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
    lease: CheckoutLease,
    repo: Path,
    repo_url: str | None,
    test_command: str | None,
    started_at: datetime,
) -> None:
    """Record the worker handle so a fresh controller can reattach (#561).

    A failed write is fatal to this launch: the caller cancels the worker rather
    than allowing a claimed run with no operator-visible liveness record."""
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
            checkout=str(lease.path),
            process_start_ticks=handle.process_start_ticks,
            shim_pid=handle.shim_pid,
            phase="spawned",
        ),
    )


def _create_observation_worktree(
    repo: Path,
    *,
    issue_id: int,
    attempt: int,
    base: str,
    local_base: str,
    branch: str,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> CheckoutLease:
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
    return CheckoutLease(
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
    lease: CheckoutLease,
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
        "checkout": str(order.checkout),
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


def _assert_staged_diff_has_no_secret(
    checkout: Path,
    *,
    runner: Runner,
    publication: ControllerGitPublication | None = None,
) -> None:
    """Refuse if a staged blob adds a secret-shaped token or cannot be scanned.

    The Worklink factory runs an untrusted backend and then commits, pushes,
    and opens a PR autonomously — so a token the backend emitted into a file
    would otherwise reach a public branch/PR with no human in the loop. The
    factory writes to the target repo, which does NOT carry the /mimir-home
    pre-commit secret hook, so this scan is the guard for that path.

    Read index blobs directly: rendered diffs omit content for paths Git treats
    as binary, including paths marked ``-diff`` by an untrusted attributes file.
    Byte output also makes arbitrary binary content scannable without relying on
    subprocess' strict text decoding. Use the shared high-signal patterns
    (``secret_scan.secret_matches``), not the broader log redactor. Compare
    exact matches with the base blob so existing credential fixtures remain
    editable without allowing a different credential-shaped value.
    """

    def run_git_bytes(*args: str) -> subprocess.CompletedProcess:
        if publication is not None:
            return publication.run(*args, text=False)
        return runner(["git", "-C", str(checkout), *args], text=False)

    staged = run_git_bytes(
        "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRTUXB"
    )
    if staged.returncode != 0:
        raise WorklinkError(
            "cannot scan staged Worklink changes for secrets "
            f"(listing staged paths exited {staged.returncode}); refusing to commit/push"
        )
    if not isinstance(staged.stdout, bytes):
        raise WorklinkError(
            "cannot scan staged Worklink changes for secrets "
            "(staged path list was not byte output); refusing to commit/push"
        )

    for raw_path in staged.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = os.fsdecode(raw_path)
        blob = run_git_bytes("cat-file", "blob", f":{path}")
        if blob.returncode != 0 or not isinstance(blob.stdout, bytes):
            raise WorklinkError(
                "cannot scan staged Worklink path "
                f"{path!r} for secrets; refusing to commit/push"
            )
        # surrogateescape preserves every byte while leaving ASCII secret shapes
        # unchanged, so legitimate binary blobs remain scannable rather than
        # being blanket-refused or silently skipped.
        text = blob.stdout.decode("utf-8", errors="surrogateescape")
        staged_matches = secret_matches(text)
        if not staged_matches:
            continue

        base_blob = run_git_bytes("cat-file", "blob", f"HEAD:{path}")
        if base_blob.returncode == 0:
            if not isinstance(base_blob.stdout, bytes):
                raise WorklinkError(
                    "cannot scan base Worklink path "
                    f"{path!r} for secrets; refusing to commit/push"
                )
            base_text = base_blob.stdout.decode("utf-8", errors="surrogateescape")
            base_matches = secret_matches(base_text)
        else:
            # A missing path is an added file and therefore has an empty base.
            # Verify absence from HEAD's tree so other blob-read failures remain
            # fail-closed. An unborn repository has no HEAD and no base paths.
            head = run_git_bytes("rev-parse", "--verify", "HEAD")
            if head.returncode != 0:
                unborn = run_git_bytes("symbolic-ref", "-q", "HEAD")
                if unborn.returncode != 0 or not isinstance(unborn.stdout, bytes):
                    raise WorklinkError(
                        "cannot scan base Worklink path "
                        f"{path!r} for secrets; refusing to commit/push"
                    )
                base_matches = set()
            elif not isinstance(head.stdout, bytes):
                raise WorklinkError(
                    "cannot scan base Worklink path "
                    f"{path!r} for secrets; refusing to commit/push"
                )
            else:
                base_entry = run_git_bytes("ls-tree", "-z", "HEAD", "--", path)
                if (
                    base_entry.returncode != 0
                    or not isinstance(base_entry.stdout, bytes)
                    or base_entry.stdout
                ):
                    raise WorklinkError(
                        "cannot scan base Worklink path "
                        f"{path!r} for secrets; refusing to commit/push"
                    )
                base_matches = set()

        if staged_matches - base_matches:
            # Do not echo the offending line — it holds the secret.
            raise WorklinkError(
                f"staged Worklink path {path!r} contains a secret-shaped token; refusing "
                "to commit/push — remove the credential from the changes"
            )


def _commit_checkout_changes(
    checkout: Path,
    issue: IssueContext,
    *,
    runner: Runner,
    publication: ControllerGitPublication | None = None,
) -> None:
    def run_git(*args: str) -> subprocess.CompletedProcess[str]:
        if publication is not None:
            return publication.run(*args)
        return runner(["git", "-C", str(checkout), *args])

    add = run_git("add", "-A")
    if add.returncode != 0:
        raise WorklinkError((add.stderr or add.stdout).strip() or "git add failed")
    staged = run_git("diff", "--cached", "--quiet")
    if staged.returncode == 0:
        raise WorklinkError("no staged Worklink changes to commit")
    # Fail closed before commit/push/PR if the backend staged a secret (or if
    # the scan cannot run).
    _assert_staged_diff_has_no_secret(checkout, runner=runner, publication=publication)
    commit = run_git("commit", "-m", f"worklink: issue #{issue.issue_id}")
    if commit.returncode != 0:
        raise WorklinkError((commit.stderr or commit.stdout).strip() or "git commit failed")


def _create_backend_checkout(
    repo: Path,
    *,
    issue_id: int,
    attempt: int,
    base: str,
    backend: ToolBackend,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    base_fetch: bool = True,
    event_logger: Callable[..., None] | None = None,
    worker_eligible: bool = False,
) -> CheckoutLease:
    shape = checkout_shape_for_backend(backend)
    if shape is not CheckoutShape.ISOLATED_CLONE:
        raise WorklinkError(f"unsupported checkout shape for backend {backend.name}: {shape}")
    return create_isolated_checkout(
        repo,
        issue_id=issue_id,
        attempt=attempt,
        base=base,
        base_fetch=base_fetch,
        event_logger=event_logger,
        runner=runner,
        worker_eligible=worker_eligible,
    )


def _with_outside_checkout_detection(
    validation: EvidenceValidation,
    *,
    issue: int,
    attempt: int,
    root: Path,
    checkout: Path,
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
    escaped = _paths_escape_checkout(root_paths, root=root, checkout=checkout)
    if not escaped:
        return validation

    _log_event(
        "worklink_backend_wrote_outside_checkout",
        issue_id=issue,
        attempt=attempt,
        root=str(root),
        checkout=str(checkout),
        files=escaped[:50],
    )
    stash = _quarantine_dirty_paths(root, escaped, issue=issue, attempt=attempt, runner=runner)
    reason = "backend_wrote_outside_checkout: " + ", ".join(escaped[:10])
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
        "worklink_quarantined_outside_checkout",
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


def _checkout_snapshot(
    checkout: Path,
    *,
    runner: Runner,
    publication: ControllerGitPublication | None = None,
) -> tuple[str, str]:
    """Capture committed and working state so startup retry never repeats work."""
    if publication is not None:
        head = publication.run("rev-parse", "HEAD")
        status = publication.run("status", "--porcelain=v1", "--untracked-files=all")
    else:
        head = runner(["git", "-C", str(checkout), "rev-parse", "HEAD"])
        status = runner([
            "git", "-C", str(checkout), "status", "--porcelain=v1", "--untracked-files=all"
        ])
    if head.returncode != 0 or status.returncode != 0:
        raise WorklinkError(
            (head.stderr or status.stderr or head.stdout or status.stdout).strip()
            or "could not snapshot Worklink checkout"
        )
    return head.stdout.strip(), status.stdout


def _new_dirty_paths(paths: Sequence[str], *, before: Sequence[str]) -> list[str]:
    old = set(before)
    return [path for path in paths if path not in old]


def _paths_escape_checkout(paths: Sequence[str], *, root: Path, checkout: Path) -> list[str]:
    root_resolved = root.resolve()
    checkout_resolved = checkout.resolve()
    escaped: list[str] = []
    for path in paths:
        absolute = (root_resolved / path).resolve()
        if absolute == checkout_resolved or absolute.is_relative_to(checkout_resolved):
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


def _ensure_clean_checkout(
    checkout: Path,
    *,
    runner: Runner,
    publication: ControllerGitPublication | None = None,
) -> None:
    status = (
        publication.run("status", "--porcelain=v1", "--untracked-files=all")
        if publication is not None
        else runner([
            "git", "-C", str(checkout), "status", "--porcelain=v1", "--untracked-files=all"
        ])
    )
    if status.returncode != 0:
        raise WorklinkError((status.stderr or status.stdout).strip() or "git status failed")
    if status.stdout.strip():
        raise WorklinkError("checkout still dirty after Worklink commit")


def _git_push(
    repo: Path,
    branch: str,
    *,
    runner: Runner,
    publication: ControllerGitPublication | None = None,
) -> None:
    result = (
        publication.push()
        if publication is not None
        else runner(["git", "-C", str(repo), "push", "-u", "origin", branch])
    )
    if result.returncode != 0:
        raise WorklinkError((result.stderr or result.stdout).strip() or "git push failed")


def _open_pr(
    repo: Path,
    issue: IssueContext,
    branch: str,
    evidence: WorklinkEvidence,
    *,
    pr_body_section: str | None = None,
    base: str,
    runner: Runner,
) -> str:
    evidence_block = (
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
    body = evidence_block
    if pr_body_section:
        body = (
            f"Closes chainlink #{issue.issue_id}.\n\n"
            f"Build summary:\n\n{pr_body_section}\n\n"
            + evidence_block.split("\n\n", 1)[1]
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


def _read_pr_body_section(checkout: Path) -> str | None:
    """Consume the build's optional PR narrative without adding it to the diff."""
    path = checkout / _PR_BODY_SECTION_FILE
    try:
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError:
        if path.is_symlink():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, _PR_BODY_SECTION_MAX_BYTES + 1)
    finally:
        os.close(fd)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    text = raw.decode("utf-8", errors="replace")
    text = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = "".join(
        char if char in "\n\t" or not unicodedata.category(char).startswith("C") else " "
        for char in text
    )
    text = redact_text(text).strip()
    # A build-authored lookalike must not precede the canonical parser anchor.
    text = _EVIDENCE_HEADING_RE.sub("[Build-authored evidence heading removed]", text)
    encoded = text.encode("utf-8")
    truncated = len(raw) > _PR_BODY_SECTION_MAX_BYTES or len(encoded) > _PR_BODY_SECTION_MAX_BYTES
    if truncated:
        prefix_limit = _PR_BODY_SECTION_MAX_BYTES - len(
            _PR_BODY_SECTION_TRUNCATED.encode("utf-8")
        )
        text = encoded[:prefix_limit].decode("utf-8", errors="ignore").rstrip()
        text += _PR_BODY_SECTION_TRUNCATED
    return text or None


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


def _with_head_sha(
    validation: EvidenceValidation,
    checkout: Path,
    *,
    runner: Runner,
    publication: ControllerGitPublication | None = None,
) -> EvidenceValidation:
    result = (
        publication.run("rev-parse", "HEAD")
        if publication is not None
        else runner(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    )
    head_sha = result.stdout.strip() if result.returncode == 0 else ""
    if not head_sha:
        return validation
    return replace(validation, evidence=replace(validation.evidence, head_sha=head_sha))


def _reattach_pr_state(pr_url: str, *, runner: Runner) -> tuple[str | None, str | None]:
    """Read PR state and head only on the cold restart-reconciliation path."""
    try:
        result = runner(["gh", "pr", "view", pr_url, "--json", "state,headRefOid"])
    except Exception:  # noqa: BLE001 - reconciliation must still release the claim.
        return None, None
    if result.returncode != 0:
        return None, None
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None, None
    state = str(payload.get("state") or "").upper() or None
    head = str(payload.get("headRefOid") or "") or None
    return state, head


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
    def run(
        args: Sequence[str] | str,
        cwd: Path | None = None,
        *,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        if isinstance(args, str):
            return subprocess.run(
                args, shell=True, cwd=cwd, capture_output=True, text=text, check=False
            )
        # Chainlink discovers its repository from cwd. Its configured home is
        # authoritative even when a caller also supplies a backend checkout.
        command_cwd = home if args and args[0] == chainlink_bin else cwd
        return subprocess.run(
            list(args), cwd=command_cwd, capture_output=True, text=text, check=False
        )

    return run


def _run(
    args: Sequence[str] | str,
    *,
    cwd: Path | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    if isinstance(args, str):
        return subprocess.run(
            args, shell=True, cwd=cwd, capture_output=True, text=text, check=False
        )
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=text, check=False)


def _log_event(event_type: str, **payload: Any) -> None:
    try:
        from ..event_logger import log_event_sync

        log_event_sync(event_type, **payload)
    except RuntimeError:
        pass
