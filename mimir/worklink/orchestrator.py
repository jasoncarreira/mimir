"""Worklink operator-run orchestrator.

The orchestrator owns deterministic state transitions around an untrusted tool
backend: validate the Chainlink leaf, claim it, create an attempt checkout,
render the work order, run the backend, observe evidence ourselves, push/open a
PR only after the evidence gate passes, then clean up and release the lock.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import warnings
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .._atomic import atomic_write_json
from .._rmtree import rmtree_missing_ok
from ..forge.github import GitHubForgeClient, GitHubIdentityVerificationError
from .backends import (
    BackendRegistry,
    CheckoutShape,
    OpenCodeBackend,
    ToolBackend,
    WorkOrder,
    WorklinkConfig,
    checkout_shape_for_backend,
)
from .compute import (
    ComputeLaunchError,
    ComputeResult,
    LaunchHandle,
    LocalSubprocessComputeBackend,
    with_worker_environment,
)
from .claims import ChainlinkClaims, ClaimRecord, WORKLINK_EPIC_LABEL
from .evidence import (
    EvidenceValidation,
    TestResult,
    WorklinkEvidence,
    observe_evidence,
    pytest_report_environment,
    read_pytest_result,
)
from .identities import get_identities
from .planning import (
    missing_leaf_template_parts,
    render_decompose_prompt,
    target_branch_from_description,
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
from .backends.feature_factory import (
    FACTORY_PUBLISHING_IDENTITY_ENV,
    FactoryStatus,
    FeatureFactoryBackend,
    epic_run_id,
)
from .backends.registry import factory_run_timeout_s
from .backends.opencode import transcript_path, write_transcript
from .factory_state import (
    FactoryRunRecord,
    factory_checkout_interlock,
    factory_process_is_alive,
    factory_process_is_verified_dead,
    factory_record_run_ids,
    load_factory_records_for_issue,
    save_factory_record,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]
_CLAIM_HEARTBEAT_INTERVAL_S = 60.0
_PR_BODY_SECTION_FILE = ".worklink-pr-body.md"
_PR_BODY_SECTION_MAX_BYTES = 4000
_PR_BODY_SECTION_TRUNCATED = "\n\n[Build summary truncated by Worklink.]"
_FACTORY_CONTROLLER_ERROR_MAX_BYTES = 4000
_RECOVERABLE_FACTORY_PHASES = {"running", "parked", "failed", "terminal"}
_EVIDENCE_HEADING_RE = re.compile(r"(?im)^Worklink evidence:\s*$")
# GitHub's documented closing-keyword set:
# https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
_GITHUB_CLOSING_KEYWORDS = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)
_GITHUB_BARE_CLOSING_REFERENCE_RE = re.compile(
    rf"(?P<keyword>\b(?:{'|'.join(_GITHUB_CLOSING_KEYWORDS)}))"
    r"(?P<separator>\s*:\s*|\s+)#(?P<number>[0-9]+)\b",
    re.IGNORECASE,
)
_FACTORY_STARTUP_STATUS_TIMEOUT_S = 120.0
_FACTORY_PUBLISHING_IDENTITY_ENV = "MIMIR_FACTORY_PUBLISHING_IDENTITY"
_WORK_ITEM_RUN_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")


def _epic_run_timeout_s() -> float:
    return factory_run_timeout_s()


def _epic_stale_heartbeat_s() -> float:
    try:
        value = float(os.environ.get("MIMIR_FACTORY_STALE_HEARTBEAT_S", "900"))
        return value if value > 0 else 900.0
    except ValueError:
        return 900.0


def _epic_prompt(issue: "IssueContext") -> str:
    header = f"Build chainlink #{issue.issue_id}: {issue.title}".strip()
    body = _render_chainlink_text(issue.description.strip())
    base = f"{header}\n\n{body}".strip() if body else header
    return base


def _render_chainlink_text(text: str) -> str:
    """Give ambiguous bare closing references their originating namespace.

    A bare ``#N`` cannot say whether its author meant Chainlink or GitHub. Text
    rendered from a Chainlink work item defaults to Chainlink; an intentional
    GitHub reference remains expressible as GitHub's explicit ``owner/repo#N``.
    """
    return _GITHUB_BARE_CLOSING_REFERENCE_RE.sub(
        r"\g<keyword>\g<separator>chainlink #\g<number>", text
    )


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
    preserved_ref: str | None = None
    preservation_error: str | None = None
    next: str | None = None
    next_present: bool = False


@dataclass
class _TerminalClaimRelease:
    claims: ChainlinkClaims
    home: Path
    issue_id: int
    attempt: int
    trigger_ready_scan: bool = False
    attempted: bool = False
    confirmed: bool = False
    retain_for_recovery: bool = False
    label_transition_applied: bool = False

    def __call__(self) -> bool:
        if self.retain_for_recovery:
            return False
        if self.attempted:
            return self.confirmed
        self.attempted = True
        try:
            self.confirmed = _release_issue_and_clear_run_state(
                self.claims,
                home=self.home,
                issue_id=self.issue_id,
                attempt=self.attempt,
                trigger_ready_scan=self.trigger_ready_scan,
            )
        except Exception:
            self.confirmed = False
        if not self.confirmed:
            _log_terminal_recovery_failed(
                issue_id=self.issue_id,
                attempt=self.attempt,
                outcome="terminal",
                label_transition_applied=self.label_transition_applied,
                state_retained=load_run_state(self.home, self.issue_id) is not None,
            )
        return self.confirmed


class WorklinkError(RuntimeError):
    """Base error for operator-facing Worklink failures."""


class LeafValidationError(WorklinkError):
    """Issue is not structured enough to hand to a backend."""


def _read_factory_publishing_identity(
    repo: Path, environ: Mapping[str, object] = os.environ
) -> tuple[str, str]:
    if _FACTORY_PUBLISHING_IDENTITY_ENV in environ:
        identity = environ[_FACTORY_PUBLISHING_IDENTITY_ENV]
        if not isinstance(identity, str):
            raise WorklinkError(
                f"{_FACTORY_PUBLISHING_IDENTITY_ENV} must be a string when set"
            )
        if not identity.strip():
            raise WorklinkError(f"{_FACTORY_PUBLISHING_IDENTITY_ENV} is set but blank")
        return identity.strip(), f"environment variable {_FACTORY_PUBLISHING_IDENTITY_ENV}"

    declaration = repo / ".factory.json"
    try:
        payload = json.loads(declaration.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorklinkError("factory publishing identity declaration is unreadable") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorklinkError("factory publishing identity declaration is invalid") from exc
    if not isinstance(payload, Mapping):
        raise WorklinkError("factory publishing identity declaration is invalid")
    identity = payload.get("publishing_identity")
    if not isinstance(identity, str) or not identity.strip():
        raise WorklinkError("factory publishing identity is missing")
    return identity.strip(), ".factory.json"


def _read_checkout_git_identity(checkout: Path, runner: Runner) -> tuple[str, str]:
    values: dict[str, str] = {}
    missing: list[str] = []
    failed: list[str] = []
    for key in ("user.name", "user.email"):
        argv = ["git", "-C", str(checkout), "config", "--get", key]
        try:
            result = runner(argv)
        except Exception as exc:
            failed.append(f"{key} ({type(exc).__name__})")
            continue
        value = result.stdout.strip()
        if result.returncode == 1 or (result.returncode == 0 and not value):
            missing.append(key)
        elif result.returncode != 0:
            failed.append(f"{key} (git exit {result.returncode})")
        else:
            values[key] = value
    if missing or failed:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if failed:
            details.append("failed " + ", ".join(failed))
        raise WorklinkError("factory checkout Git identity preflight failed: " + "; ".join(details))
    return values["user.name"], values["user.email"]


def _resolve_factory_github_credential(
    environ: Mapping[str, str],
) -> tuple[str, dict[str, str]]:
    """Return the credential this process is already bound to, plus child aliases.

    The parent does not select among candidate credentials. ``GITHUB_TOKEN`` is
    what both the forge client and configuration read, so it is *the* process
    credential; ``GH_TOKEN`` exists only because the factory's child shells out
    to ``gh``, which prefers that name. Borrowing gh's precedence rule for the
    parent's own verification would introduce a second credential into a process
    that already verified one, and the forge identity memo refuses a fingerprint
    change - so the preflight would fail without ever reaching ``/user``.

    Two different non-blank values are an operator ambiguity, not a precedence
    question. Silently preferring one is how publication proceeds under the
    wrong identity, which is the failure ``.factory.json`` ``publishing_identity``
    exists to catch, so this refuses and names the variables to reconcile.
    """
    github_token = environ.get("GITHUB_TOKEN", "").strip()
    gh_token = environ.get("GH_TOKEN", "").strip()
    if gh_token and github_token and gh_token != github_token:
        raise WorklinkError(
            "factory publication credentials conflict: GH_TOKEN and GITHUB_TOKEN "
            "are both set to different values; unset GH_TOKEN or set it to the "
            "same credential"
        )
    if not github_token:
        raise WorklinkError("factory publication requires GITHUB_TOKEN")
    return github_token, {"GH_TOKEN": github_token, "GITHUB_TOKEN": github_token}


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
    interval_s: float | None = None,
) -> Any:
    """Keep the claim fresh until the awaited phase returns, fails, or is cancelled."""
    if interval_s is None:
        interval_s = _CLAIM_HEARTBEAT_INTERVAL_S

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


def render_work_item(issue: IssueContext) -> str:
    """Render a Chainlink issue as the factory's deterministic JSON input."""
    if issue.issue_id <= 0:
        raise WorklinkError("chainlink issue id must be a positive integer")
    if not issue.title.strip():
        raise WorklinkError(f"chainlink issue {issue.issue_id} has an empty title")

    run_id = epic_run_id(issue.issue_id)
    if _WORK_ITEM_RUN_ID_RE.fullmatch(run_id) is None:
        raise WorklinkError(f"chainlink issue {issue.issue_id} produced an invalid run_id")
    return json.dumps(
        {"run_id": run_id, "title": issue.title, "body": issue.description},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_epic_work_item(payload: str, issue_id: int) -> str:
    """Validate the rendered factory payload and return its declared run ID."""
    try:
        work_item = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise WorklinkError("rendered factory work item is malformed") from exc
    if not isinstance(work_item, dict):
        raise WorklinkError("rendered factory work item must be a JSON object")
    run_id = work_item.get("run_id")
    if not isinstance(run_id, str) or _WORK_ITEM_RUN_ID_RE.fullmatch(run_id) is None:
        raise WorklinkError("rendered factory work item has an invalid run_id")
    if run_id != epic_run_id(issue_id):
        raise WorklinkError("rendered factory work item run_id does not match the issue")
    title = work_item.get("title")
    if not isinstance(title, str) or not title.strip():
        raise WorklinkError("rendered factory work item has an invalid title")
    if not isinstance(work_item.get("body"), str):
        raise WorklinkError("rendered factory work item has an invalid body")
    return run_id


def _require_factory_launch_binding(
    spec: WorkSpec, run_id: str, publishing_identity: str
) -> None:
    argv = spec.local_argv
    if (
        spec.backend_config.get("run_id") != run_id
        or argv is None
        or not argv
        or not argv[-1].split()
        or argv[-1].split()[-1] != run_id
    ):
        raise WorklinkError("factory launch request run_id does not match the supervised run_id")
    # Fail closed on a launch that would publish as some other account. Losing the
    # variable between here and the child is silent otherwise: the driver falls back
    # to the sandbox's .factory.json and parks at Gate 1 naming the FILE's identity,
    # which reads as a misconfiguration rather than a dropped variable.
    if spec.env.get(FACTORY_PUBLISHING_IDENTITY_ENV) != publishing_identity:
        raise WorklinkError(
            "factory launch environment does not carry the resolved publishing identity"
        )


def read_work_item(
    issue_id: int,
    *,
    chainlink_bin: str = "chainlink",
    runner: Runner | None = None,
) -> str:
    """Read one local Chainlink issue and return its factory work-item JSON."""
    if issue_id <= 0:
        raise WorklinkError("chainlink issue id must be a positive integer")
    issue = ChainlinkIssueReader(chainlink_bin=chainlink_bin, runner=runner).read(issue_id)
    if issue.issue_id != issue_id:
        raise WorklinkError(
            f"chainlink issue show {issue_id} returned mismatched issue {issue.issue_id}"
        )
    return render_work_item(issue)


def validate_leaf(issue: IssueContext) -> None:
    try:
        target_branch_from_description(issue.description)
    except ValueError as exc:
        raise LeafValidationError(str(exc)) from exc
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
        description=_render_chainlink_text(issue.description.strip()),
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
    outcome_reservation_id: str | None = None

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
        reservation_id = self.outcome_reservation_id or _outcome_reservation(
            self.home, issue_id, target="leaf", autonomous=autonomous and not dry_run
        )
        runner = self.runner or _runner_for_home(self.home, self.chainlink_bin)
        try:
            issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)
        except Exception as exc:
            _record_preclaim_input(
                self.home, issue_id, reservation_id,
                source="leaf_issue_read", cause="read_failed",
                validator="chainlink_issue", result=type(exc).__name__,
            )
            raise
        try:
            validate_leaf(issue)
        except LeafValidationError as exc:
            _record_preclaim_input(
                self.home, issue_id, reservation_id,
                source=("leaf_target_branch" if "target branch" in str(exc).lower() else "leaf_template"),
                cause=("invalid_target_branch" if "target branch" in str(exc).lower() else "template_missing"),
                validator="leaf_contract", result="invalid",
            )
            if not dry_run:
                _demote_template_invalid_ready_leaf(
                    issue,
                    reason=str(exc),
                    runner=runner,
                    chainlink_bin=self.chainlink_bin,
                )
            raise
        try:
            config = WorklinkConfig.load(self.home / "worklink.yaml")
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="leaf_config", cause="configuration_invalid", validator="worklink_config", result=type(exc).__name__)
            raise
        try:
            inventory = RepositoryInventory.load(self.home / "repositories.yaml")
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="leaf_inventory", cause="configuration_invalid", validator="repository_inventory", result=type(exc).__name__)
            raise
        registry = self.registry or BackendRegistry(config)
        try:
            repo_url = _repo_remote_url(self.repo, runner=runner)
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="leaf_repository", cause="configuration_invalid", validator="repository_origin", result=type(exc).__name__)
            raise
        repo_slug = _repo_slug_from_url(repo_url)
        repository_config = inventory.repository(repo_slug) if inventory.declared else None
        try:
            backend = (
                registry.get(backend_name)
                if backend_name
                else registry.select(labels=issue.labels, repo=repo_slug)
            )
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="leaf_backend", cause="configuration_invalid", validator="backend_selection", result=type(exc).__name__)
            raise
        try:
            compute = registry.select_compute(labels=issue.labels, repo=repo_slug)
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="leaf_compute", cause="configuration_invalid", validator="compute_selection", result=type(exc).__name__)
            raise
        selected_name = backend.name
        worker_uid_drop = (
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
        # An explicit operator override wins; otherwise the leaf can select its
        # integration branch ahead of repository/deployment defaults.
        base = (
            base_branch
            or target_branch_from_description(issue.description)
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
            max_attempts=config.defaults.max_claim_attempts,
        )
        # Re-read immediately before claiming so retries in a long-lived caller do
        # not use stale comments and collide with prior attempt-scoped branches.
        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)
        predicted_attempt = claims.next_attempt(issue.comments)
        claiming_state_written = False

        def record_claiming() -> None:
            nonlocal claiming_state_written
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue.issue_id,
                reservation_id=reservation_id, source="leaf_claim_preparation",
                cause="state_write_failed", operation="claim_preparation",
            ):
                existing = load_run_state(self.home, issue.issue_id)
                if existing is not None and process_is_alive(existing):
                    raise WorklinkError(
                        f"live run state already exists for issue {issue.issue_id}"
                    )
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
                exclude_active_label=WORKLINK_EPIC_LABEL,
                before_claim=record_claiming,
                **({"reservation_id": reservation_id} if reservation_id is not None else {}),
            )
        except Exception:
            if claiming_state_written:
                clear_run_state(self.home, issue.issue_id)
            raise
        if claim.attempts_exhausted:
            if claiming_state_written:
                clear_run_state(self.home, issue.issue_id)
            _log_event("worklink_attempts_exhausted", issue_id=issue.issue_id)
            _record_claim_refusal_outcome(
                self.home,
                issue.issue_id,
                reservation_id,
                reason="attempts_exhausted",
            )
            return WorklinkRunResult(issue.issue_id, None, "blocked", reason="attempts_exhausted")
        if not claim.claimed or claim.record is None:
            if claiming_state_written:
                clear_run_state(self.home, issue.issue_id)
            _log_event(
                "worklink_claim_failed",
                issue_id=issue.issue_id,
                reason=claim.reason or "claim_failed",
            )
            _record_claim_refusal_outcome(
                self.home,
                issue.issue_id,
                reservation_id,
                reason=claim.reason or "claim_failed",
            )
            return WorklinkRunResult(
                issue.issue_id, None, _claim_refusal_status(claim.reason),
                reason=claim.reason or "claim_failed"
            )
        record = claim.record
        if reservation_id is not None:
            _record_lifecycle_start(
                self.home,
                issue_id=issue.issue_id,
                reservation_id=reservation_id,
                record=record,
            )
        terminal_release = _TerminalClaimRelease(
            claims,
            home=self.home,
            issue_id=issue.issue_id,
            attempt=record.attempt,
            trigger_ready_scan=autonomous,
        )
        _log_event(
            "worklink_claimed",
            issue_id=issue.issue_id,
            attempt=record.attempt,
            backend=selected_name,
        )

        lease: CheckoutLease | None = None
        publication: ControllerGitPublication | None = None
        delete_authorized_checkout = False
        executor_report_dir: Path | None = None
        try:
            with _typed_outcome_boundary(
                home=self.home,
                issue_id=issue.issue_id,
                reservation_id=reservation_id,
                source="leaf_checkout_create",
                cause="checkout_failed",
                claim_record=record,
                operation="create_checkout",
            ):
                lease = _create_backend_checkout(
                    self.repo,
                    issue_id=issue.issue_id,
                    attempt=record.attempt,
                    base=base,
                    backend=backend,
                    base_fetch=config.defaults.base_fetch,
                    event_logger=_log_event,
                    runner=_list_runner(runner),
                    worker_eligible=worker_uid_drop,
                )
            if worker_uid_drop:
                if not isinstance(compute, LocalSubprocessComputeBackend):
                    raise WorklinkError("worker uid drop requires local subprocess compute")
                compute = LocalSubprocessComputeBackend.for_path_checkout(
                    get_identities().worklink_uid
                )
                with _typed_outcome_boundary(
                    home=self.home,
                    issue_id=issue.issue_id,
                    reservation_id=reservation_id,
                    source="leaf_publication_capture",
                    cause="publication_boundary_failed",
                    claim_record=record,
                    checkout=lease.path,
                    branch=lease.branch,
                    operation="capture_publication",
                ):
                    checkout_fd = os.open(
                        lease.path,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | getattr(os, "O_NOFOLLOW", 0),
                    )
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
                _record_direct_leaf_outcome(
                    home=self.home,
                    issue_id=issue.issue_id,
                    reservation_id=reservation_id,
                    claim_record=record,
                    source="leaf_checkout_isolation",
                    cause="unsafe_checkout",
                    checkout=lease.path,
                    branch=lease.branch,
                    reason="not_isolated",
                )
                return WorklinkRunResult(
                    issue.issue_id,
                    record.attempt,
                    "blocked",
                    reason=(
                        f"{selected_name} must run in an isolated checkout (own .git), "
                        "not a parent-pointing worktree, to avoid exposing other checkouts "
                        "(chainlink #517/#1019)"
                    ),
                )
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue.issue_id,
                reservation_id=reservation_id, source="leaf_dirty_snapshot",
                cause="checkout_read_failed", claim_record=record,
                checkout=lease.path, branch=lease.branch,
            ):
                root_dirty_before = _dirty_paths(self.repo, runner=runner)
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue.issue_id,
                reservation_id=reservation_id, source="leaf_prompt",
                cause="prompt_render_failed", claim_record=record,
                checkout=lease.path, branch=lease.branch,
                operation="render_prompt",
            ):
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
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue.issue_id,
                reservation_id=reservation_id, source="leaf_work_spec",
                cause="work_spec_failed", claim_record=record,
                checkout=lease.path, branch=lease.branch,
                operation="work_spec",
            ):
                spec = backend.work_spec(
                    order,
                    attempt=record.attempt,
                    repo_url=repo_url,
                    base_ref=lease.base_ref,
                    branch=lease.branch,
                    test_command=test_cmd,
                )
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue.issue_id,
                reservation_id=reservation_id, source="leaf_report_setup",
                cause="report_setup_failed", claim_record=record,
                checkout=lease.path, branch=lease.branch,
                operation="report_setup",
            ):
                executor_report_dir = _make_executor_report_dir(
                    issue.issue_id, record.attempt, worker_uid_drop=worker_uid_drop
                )
            report_env = pytest_report_environment(
                test_cmd,
                executor_report_dir,
                existing=spec.env.get("PYTEST_ADDOPTS"),
            )
            if report_env:
                spec = with_worker_environment(spec, report_env)
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
                    with _typed_outcome_boundary(
                        home=self.home, issue_id=issue.issue_id,
                        reservation_id=reservation_id, source="leaf_compute_launch",
                        cause="launch_failed", claim_record=record,
                        checkout=lease.path, branch=lease.branch,
                        operation="compute_launch",
                    ):
                        handle = await compute.launch(spec)
                    # Atomically replace the provisional controller record with
                    # the real cancellable worker handle immediately after spawn.
                    try:
                        with _typed_outcome_boundary(
                            home=self.home, issue_id=issue.issue_id,
                            reservation_id=reservation_id, source="leaf_handle_save",
                            cause="state_write_failed", claim_record=record,
                            checkout=lease.path, branch=lease.branch,
                            operation="save_handle",
                        ):
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
                                test_env=spec.backend_config.get("test_env", {}),
                            )
                    except OSError as exc:
                        _log_event(
                            "worklink_run_state_persist_failed",
                            issue_id=issue.issue_id,
                            error=str(exc),
                        )
                        await compute.cancel(handle)
                        raise
                    with _typed_outcome_boundary(
                        home=self.home, issue_id=issue.issue_id,
                        reservation_id=reservation_id, source="leaf_compute_wait",
                        cause="worker_failed", claim_record=record,
                        checkout=lease.path, branch=lease.branch,
                        operation="compute_wait",
                    ):
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
            # Gates (including reruns) can outlast the claim TTL independently
            # of the backend. Keep the claim alive through publication as well.
            result = await _heartbeat_while(
                self._finalize(
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
                    executor_report_dir=executor_report_dir,
                    terminal_release=terminal_release,
                    outcome_reservation_id=reservation_id,
                ),
                claims=claims,
                record=record,
            )
            delete_authorized_checkout = bool(
                worker_uid_drop and result.review_ready and result.pr_url
            )
            if delete_authorized_checkout and publication is not None:
                cleanup_errors: list[str] = []
                _run_post_publication_bookkeeping(
                    "authorized branch cleanup",
                    lambda: publication.run(
                        "update-ref", "-d", f"refs/heads/{lease.branch}", check=True
                    ),
                    issue_id=issue.issue_id,
                    attempt=record.attempt,
                    pr_url=result.pr_url,
                    errors=cleanup_errors,
                )
                if cleanup_errors:
                    result = replace(
                        result,
                        reason=_append_post_publication_error(
                            result.reason, "; ".join(cleanup_errors)
                        ),
                    )
            return result
        except Exception as exc:
            transition_applied = False
            transition_error = None
            terminal_release.retain_for_recovery = True
            try:
                claims.transition_issue(
                    issue.issue_id,
                    status="failed",
                    review_ready=False,
                    attempt=record.budget_attempt or record.attempt,
                    reason=str(exc),
                )
                transition_applied = True
                terminal_release.label_transition_applied = True
                terminal_release.retain_for_recovery = False
                terminal_release()
            except Exception as transition_exc:
                transition_error = str(transition_exc)
            _log_event(
                "worklink_transition",
                issue_id=issue.issue_id,
                attempt=record.attempt,
                status="failed",
                review_ready=False,
                pr_url=None,
                reason=str(exc),
                transition_applied=transition_applied,
                error=transition_error,
            )
            return WorklinkRunResult(
                issue.issue_id,
                record.attempt,
                "failed",
                reason=str(exc),
                checkout=lease.path if lease else None,
                branch=lease.branch if lease else None,
            )
        except BaseException as exc:
            # No terminal routing occurred. Keep both recovery handles, even
            # when the finally block below requests release after teardown.
            terminal_release.retain_for_recovery = True
            _log_event(
                "worklink_run_interrupted",
                issue_id=issue.issue_id,
                attempt=record.attempt,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            try:
                try:
                    _close_attempt_capabilities(
                        publication,
                        lease.authorization if lease is not None else None,
                        lease.path if lease is not None else None,
                        delete_checkout=delete_authorized_checkout,
                    )
                except Exception as exc:  # noqa: BLE001 - teardown must continue
                    _log_event(
                        "worklink_cleanup_failed",
                        issue_id=issue.issue_id,
                        attempt=record.attempt,
                        cleanup="attempt_capabilities",
                        error=str(exc),
                    )
                if executor_report_dir is not None:
                    _remove_executor_report_dir_best_effort(
                        executor_report_dir,
                        issue_id=issue.issue_id,
                        attempt=record.attempt,
                    )
            finally:
                terminal_release()

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
        executor_report_dir: Path | None = None,
        terminal_release: _TerminalClaimRelease,
        outcome_reservation_id: str | None = None,
    ) -> WorklinkRunResult:
        """Post-launch pipeline: interpret the worker result, observe evidence,
        open the PR on a passing gate, then transition + clean up.

        Extracted so both a fresh ``run`` and a post-restart ``reattach`` share
        the identical evidence/PR/transition path — the only difference between
        them is how ``compute_result`` was obtained (launch+wait vs. wait on a
        persisted handle)."""
        selected_name = backend.name
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue.issue_id,
            reservation_id=outcome_reservation_id, source="leaf_interpret",
            cause="interpretation_failed", claim_record=claim_record,
            checkout=lease.path, branch=lease.branch,
            operation="interpret",
        ):
            raw = await backend.interpret(order, compute_result)
        executor_tests: TestResult | None = None
        if executor_report_dir is not None:
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue.issue_id,
                reservation_id=outcome_reservation_id, source="leaf_test_report",
                cause="evidence_read_failed", claim_record=claim_record,
                checkout=lease.path, branch=lease.branch,
                operation="test_report",
            ):
                executor_tests = read_pytest_result(test_cmd or "", executor_report_dir)
            _remove_executor_report_dir_best_effort(
                executor_report_dir,
                issue_id=issue.issue_id,
                attempt=attempt,
            )
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue.issue_id,
            reservation_id=outcome_reservation_id, source="leaf_pr_body",
            cause="evidence_read_failed", claim_record=claim_record,
            checkout=lease.path, branch=lease.branch,
            operation="pr_body",
        ):
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
                test_env=spec.backend_config.get("test_env", {}),
            )

        # After the #832 substrate cleanup local_subprocess is the only Worklink
        # compute substrate. Its capabilities declare shared_filesystem=True, so
        # the controller runs the diff/test re-derivation itself (no remote-fetch
        # gate, no folded trusted-test job).
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue.issue_id,
            reservation_id=outcome_reservation_id, source="leaf_gate",
            cause="validation_failed", claim_record=claim_record,
            checkout=lease.path, branch=lease.branch,
            operation="evidence_gate",
        ):
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
                gate_rerun_max_failures=config.defaults.gate_rerun_max_failures,
                blocked_reason=raw.blocked_reason,
                model=invocation_model,
                failure_reason=raw.error if (executor_failed or backend_reported_failure) else None,
                executor_tests=executor_tests,
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
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue.issue_id,
            reservation_id=outcome_reservation_id, source="leaf_evidence_write",
            cause="evidence_write_failed", claim_record=claim_record,
            checkout=lease.path, branch=lease.branch,
            operation="evidence_write",
        ):
            evidence_path = _write_evidence(self.home, validation.evidence)
        if validation.review_ready:
            try:
                _commit_checkout_changes(
                    lease.path, issue, runner=runner, publication=publication
                )
                _ensure_clean_checkout(lease.path, runner=runner, publication=publication)
            except Exception as exc:
                _record_direct_leaf_outcome(
                    home=self.home,
                    issue_id=issue.issue_id,
                    reservation_id=outcome_reservation_id,
                    claim_record=claim_record,
                    source="leaf_commit",
                    cause="publication_failed",
                    checkout=lease.path,
                    branch=lease.branch,
                    reason=type(exc).__name__,
                    evidence_path=evidence_path,
                )
                validation = _publication_failed_validation(
                    validation,
                    step="commit",
                    error=exc,
                    issue_id=issue.issue_id,
                    attempt=attempt,
                )
            else:
                first_tests = validation.evidence.tests
                with _typed_outcome_boundary(
                    home=self.home, issue_id=issue.issue_id,
                    reservation_id=outcome_reservation_id, source="leaf_regate",
                    cause="validation_failed", claim_record=claim_record,
                    checkout=lease.path, branch=lease.branch,
                    operation="post_commit_gate",
                ):
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
                    gate_rerun_max_failures=config.defaults.gate_rerun_max_failures,
                    blocked_reason=raw.blocked_reason,
                    model=invocation_model,
                    failure_reason=raw.error if (executor_failed or backend_reported_failure) else None,
                    executor_tests=executor_tests,
                    skip_test_reason=(
                        "executor exited nonzero before the test gate" if executor_failed else None
                    ),
                    runner=runner,
                    safe_git=publication,
                    work_spec=spec,
                    compute=compute,
                        on_gate_launch=persist_gate_handle,
                    )
                if first_tests is not None and first_tests.flaky_tests:
                    tests = validation.evidence.tests
                    if tests is not None:
                        # Retain flake history, not the earlier passing gate verdict.
                        validation = replace(
                            validation,
                            evidence=replace(
                                validation.evidence,
                                tests=replace(
                                    tests,
                                    flaky_tests=tuple(dict.fromkeys(
                                        (*first_tests.flaky_tests, *tests.flaky_tests)
                                    )),
                                    initial_run=tests.initial_run or first_tests.initial_run,
                                    rerun=tests.rerun or first_tests.rerun,
                                    previous_observation=first_tests,
                                ),
                            ),
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
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue.issue_id,
                reservation_id=outcome_reservation_id, source="leaf_evidence_write",
                cause="evidence_write_failed", claim_record=claim_record,
                checkout=lease.path, branch=lease.branch,
                operation="regate_evidence_write",
            ):
                evidence_path = _write_evidence(self.home, validation.evidence)
        if validation.review_ready:
            # chainlink #518: push from the checkout that OWNS the attempt
            # branch, not the parent repo. With the isolated-checkout shape
            # (#517) the branch + its commit live only inside ``lease.path``
            # (its own .git, with ``origin`` already pointed at the real
            # remote); pushing from ``self.repo`` fails with
            # "src refspec <branch> does not match any". This is also correct
            # for the legacy worktree shape, which shares the parent's refs.
            step = "fence"
            try:
                with _leaf_publication(self.home, issue.issue_id, attempt) as intent:
                    step = "push"
                    _git_push(lease.path, lease.branch, runner=runner, publication=publication)
                    validation = _with_head_sha(
                        validation, lease.path, runner=runner, publication=publication
                    )
                    step = "pull request"
                    intent.pr_started = True
                    pr_url = _open_pr(
                        self.repo, issue, lease.branch, validation.evidence,
                        pr_body_section=pr_body_section, base=lease.base_ref, runner=runner,
                    )
                    validation = _with_pr_url(validation, pr_url)
                    step = "completed evidence"
                    evidence_path = _write_evidence(self.home, validation.evidence)
                    intent.completed = True
            except Exception as exc:
                source = {
                    "fence": "leaf_publication_fence",
                    "push": "leaf_push",
                    "pull request": "leaf_pr_open",
                    "completed evidence": "leaf_evidence_write",
                }[step]
                cause = (
                    "evidence_write_failed"
                    if step == "completed evidence"
                    else "publication_failed"
                )
                _record_direct_leaf_outcome(
                    home=self.home,
                    issue_id=issue.issue_id,
                    reservation_id=outcome_reservation_id,
                    claim_record=claim_record,
                    source=source,
                    cause=cause,
                    checkout=lease.path,
                    branch=lease.branch,
                    reason=type(exc).__name__,
                    evidence_path=evidence_path,
                )
                validation = _publication_failed_validation(
                    validation, step=step, error=exc,
                    issue_id=issue.issue_id, attempt=attempt,
                )
            evidence_path = _write_evidence(self.home, validation.evidence)
        post_publication_errors: list[str] = []

        def run_bookkeeping(name: str, action: Callable[[], Any]) -> Any | None:
            return _run_post_publication_bookkeeping(
                name,
                action,
                issue_id=issue.issue_id,
                attempt=attempt,
                pr_url=pr_url,
                errors=post_publication_errors,
            )

        if pr_url:
            written_path = run_bookkeeping(
                "completed evidence write",
                lambda: _write_evidence(self.home, validation.evidence),
            )
            if isinstance(written_path, Path):
                evidence_path = written_path

        def comment_evidence() -> None:
            _comment_evidence(
                claims,
                validation.evidence,
                validation,
                evidence_path,
                gate_test_tail=(
                    None if validation.review_ready else _local_gate_failure_tail(validation)
                ),
            )

        def log_evidence() -> None:
            _log_gate_flaky_tests(validation.evidence)
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

        if pr_url:
            run_bookkeeping("evidence comment", comment_evidence)
            run_bookkeeping("evidence event", log_evidence)
        else:
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue.issue_id,
                reservation_id=outcome_reservation_id,
                source="leaf_evidence_comment", cause="bookkeeping_failed",
                claim_record=claim_record, checkout=lease.path,
                branch=lease.branch, operation="evidence_comment",
            ):
                comment_evidence()
            log_evidence()
        transition_status = (
            "blocked"
            if raw.output_overflow or {"gate_timed_out", "gate_command_not_found"}.intersection(validation.reasons)
            else validation.status
        )
        transition_reason = (
            validation.evidence.failure_reason
            if raw.exit_code != 0
            else validation.evidence.blocked_reason
            if validation.status == "blocked"
            else (", ".join(validation.reasons) if validation.reasons else None)
        )
        transition_applied = False
        transition_error = None

        if outcome_reservation_id is not None and not validation.review_ready:
            _record_leaf_outcome_before_routing(
                home=self.home,
                issue=issue,
                reservation_id=outcome_reservation_id,
                claim_record=claim_record,
                status=transition_status,
                reason=transition_reason,
                evidence_path=evidence_path,
                checkout=lease.path,
                branch=lease.branch,
                pr_url=pr_url,
            )

        # Keep the lock until routing succeeds, including through outer finally.
        terminal_release.retain_for_recovery = True
        if pr_url:
            # Execution is finished and publication evidence is durable. Retire
            # the worker pointer so startup orphan reconciliation cannot demote
            # this PR; terminal recovery now belongs to the stale-lock reaper.
            run_bookkeeping("completed run state clear", lambda: clear_run_state(self.home, issue.issue_id))

        def transition_issue() -> None:
            nonlocal transition_applied, transition_error
            try:
                claims.transition_issue(
                    issue.issue_id,
                    status=transition_status,
                    review_ready=validation.review_ready,
                    attempt=claim_record.budget_attempt or attempt,
                    reason=transition_reason,
                )
            except Exception as exc:
                transition_error = str(exc)
                _record_direct_leaf_outcome(
                    home=self.home,
                    issue_id=issue.issue_id,
                    reservation_id=outcome_reservation_id,
                    claim_record=claim_record,
                    source="leaf_terminal_labels",
                    cause="terminal_routing_failed",
                    checkout=lease.path,
                    branch=lease.branch,
                    reason=type(exc).__name__,
                    evidence_path=evidence_path,
                )
                raise
            transition_applied = True
            terminal_release.label_transition_applied = True
            terminal_release.retain_for_recovery = False

        def log_transition() -> None:
            _log_event(
                "worklink_transition",
                issue_id=issue.issue_id,
                attempt=attempt,
                status=transition_status,
                review_ready=validation.review_ready,
                pr_url=pr_url,
                transition_applied=transition_applied,
                error=transition_error,
            )

        if pr_url:
            run_bookkeeping("issue transition", transition_issue)
            run_bookkeeping("transition event", log_transition)
        else:
            try:
                transition_issue()
            finally:
                # The transition event is the observable record of this failure;
                # emit it without turning a failed mutation into a successful run.
                log_transition()
        if transition_applied and not terminal_release():
            _record_direct_leaf_outcome(
                home=self.home,
                issue_id=issue.issue_id,
                reservation_id=outcome_reservation_id,
                claim_record=claim_record,
                source="leaf_terminal_release",
                cause="terminal_routing_failed",
                checkout=lease.path,
                branch=lease.branch,
                reason="release_failed",
                evidence_path=evidence_path,
            )
            return WorklinkRunResult(
                issue.issue_id,
                attempt,
                "failed",
                review_ready=validation.review_ready,
                pr_url=pr_url,
                evidence_path=evidence_path,
                checkout=lease.path,
                branch=lease.branch,
                reason="terminal recovery incomplete: Chainlink lock release failed",
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
            if cleanup_error and pr_url:
                post_publication_errors.append(f"checkout cleanup: {cleanup_error}")
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
                _append_post_publication_error(None, "; ".join(post_publication_errors))
                if post_publication_errors
                else f"post-transition cleanup failed: {cleanup_error}"
                if cleanup_error
                else validation.evidence.failure_reason if raw.exit_code != 0
                else validation.evidence.blocked_reason if validation.status == "blocked"
                else None
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
        state_path = self.home / "state" / "worklink" / "runs" / f"{issue_id}.json"
        state_present = state_path.exists() or state_path.is_symlink()
        state = load_run_state(self.home, issue_id)
        if state is None:
            _record_reconcile_outcome(
                home=self.home,
                issue_id=issue_id,
                reservation_id=self.outcome_reservation_id,
                source="reattach_state",
                cause="state_unreadable" if state_present else "state_missing",
                stage="state_read",
                result="unreadable" if state_present else "missing",
            )
            return WorklinkRunResult(issue_id, None, "failed", reason="reattach: no run state")

        runner = self.runner or _runner_for_home(self.home, self.chainlink_bin)
        config = WorklinkConfig.load(self.home / "worklink.yaml")
        claims = ChainlinkClaims(
            chainlink_bin=self.chainlink_bin,
            agent_id=self.agent_id,
            runner=_list_runner(runner),
            home_path=self.home,
            event_logger=_log_event,
            max_attempts=config.defaults.max_claim_attempts,
        )
        terminal_release = _TerminalClaimRelease(
            claims,
            home=self.home,
            issue_id=issue_id,
            attempt=state.attempt,
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
            _record_reconcile_outcome(
                home=self.home,
                issue_id=issue_id,
                reservation_id=self.outcome_reservation_id,
                source="reattach_shim",
                cause=(
                    "cleanup_failed"
                    if "cleanup failed" in reason
                    else "identity_stale"
                    if "stale" in reason
                    else "worker_interrupted"
                ),
                stage="shim_reconcile",
                result=reason,
            )
            if terminal_release():
                claims.transition_issue(
                    issue_id,
                    status="failed",
                    review_ready=False,
                    attempt=state.attempt,
                    reason=reason,
                )
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
                if restored_review and terminal_release():
                    claims.transition_issue(
                        issue_id,
                        status="completed",
                        review_ready=True,
                        attempt=state.attempt,
                    )
                elif restored_review:
                    _record_reconcile_outcome(
                        home=self.home,
                        issue_id=issue_id,
                        reservation_id=self.outcome_reservation_id,
                        source="reattach_pr",
                        cause="terminal_routing_failed",
                        stage="release",
                        result="release_failed",
                    )
                    return WorklinkRunResult(
                        issue_id,
                        state.attempt,
                        "failed",
                        pr_url=pr_url,
                        evidence_path=review_ready.path,
                        branch=state.branch,
                        reason="terminal recovery incomplete: Chainlink lock release failed",
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
                terminal_release()
        # Only resume a leaf still in-progress. If the reaper already recovered it
        # (or a prior run transitioned it) the work is no longer ours to finish —
        # drop the stale state and stop. ``_issue_has_label`` fails open (assume
        # in-progress) when labels can't be read, so a transient read error
        # doesn't strand the worker.
        if not claims._issue_has_label(issue_id, "worklink:in-progress"):  # noqa: SLF001
            _log_event("worklink_reattach_skipped", issue_id=issue_id, reason="not_in_progress")
            terminal_release()
            return WorklinkRunResult(
                issue_id, state.attempt, "failed", reason="reattach: leaf no longer in-progress"
            )

        registry = self.registry or BackendRegistry(config)
        try:
            backend = registry.get(state.backend)
            compute = registry.get_compute(state.compute_name)
        except (KeyError, ValueError) as exc:
            _record_direct_reattach_leaf(
                self.home, issue_id, self.outcome_reservation_id, state,
                source="reattach_backend", cause="backend_unavailable",
                reason=type(exc).__name__,
            )
            _log_event("worklink_reattach_failed", issue_id=issue_id, reason=str(exc))
            clear_run_state(self.home, issue_id)
            return WorklinkRunResult(issue_id, state.attempt, "failed", reason=f"reattach: {exc}")
        if not compute.capabilities().persistent_after_disconnect:
            # Defensive: only persistent substrates are ever persisted.
            _record_direct_reattach_leaf(
                self.home, issue_id, self.outcome_reservation_id, state,
                source="reattach_compute", cause="not_resumable",
                reason="not_persistent",
            )
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
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue_id,
            reservation_id=self.outcome_reservation_id, source="reattach_issue",
            cause="read_failed", checkout=Path(state.checkout) if state.checkout else None,
            branch=state.branch, operation="issue_read",
        ):
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
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue_id,
                reservation_id=self.outcome_reservation_id, source="reattach_checkout",
                cause="checkout_failed", checkout=Path(state.checkout) if state.checkout else None,
                branch=state.branch, operation="observation_checkout",
            ):
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
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue_id,
                reservation_id=self.outcome_reservation_id, source="reattach_prompt",
                cause="prompt_render_failed", checkout=lease.path,
                branch=state.branch, operation="render_prompt",
            ):
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
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue_id,
                reservation_id=self.outcome_reservation_id, source="reattach_spec",
                cause="work_spec_failed", checkout=lease.path,
                branch=state.branch, operation="work_spec",
            ):
                spec = backend.work_spec(
                    order,
                    attempt=state.attempt,
                    repo_url=state.repo_url,
                    base_ref=state.local_base or state.base_ref,
                    branch=state.branch,
                    test_command=test_cmd or "",
                )
            # Recovery records the original selection, not current backend defaults.
            spec = replace(
                spec,
                backend_config={**spec.backend_config, "test_env": dict(state.test_env)},
            )
            claim_record = ClaimRecord(
                issue_id=issue_id,
                attempt=state.attempt,
                agent_id=self.agent_id,
                claimed_at=started,
            )
            try:
                with _typed_outcome_boundary(
                    home=self.home, issue_id=issue_id,
                    reservation_id=self.outcome_reservation_id, source="reattach_wait",
                    cause="worker_failed", claim_record=claim_record,
                    checkout=lease.path, branch=state.branch,
                    operation="worker_wait",
                ):
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
                if terminal_release():
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
            return await _heartbeat_while(
                self._finalize(
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
                    terminal_release=terminal_release,
                    outcome_reservation_id=self.outcome_reservation_id,
                ),
                claims=claims,
                record=claim_record,
            )
        except Exception as exc:
            if terminal_release():
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
            try:
                if lease is not None:
                    try:
                        _remove_observation_worktree(self.repo, lease, runner=_list_runner(runner))
                    except Exception as exc:  # noqa: BLE001 - teardown must continue
                        _log_event(
                            "worklink_cleanup_failed",
                            issue_id=issue_id,
                            attempt=state.attempt,
                            cleanup="reattach_observation_worktree",
                            error=str(exc),
                        )
            finally:
                terminal_release()

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
        reservation_id = self.outcome_reservation_id or _outcome_reservation(
            self.home, issue_id, target="factory", autonomous=autonomous
        )
        with factory_checkout_interlock(self.home) as acquired:
            if not acquired:
                _record_preclaim_claim(
                    self.home, issue_id, reservation_id,
                    source="factory_interlock", cause="interlock_unavailable",
                    result="unavailable",
                )
                return WorklinkRunResult(
                    issue_id, None, "refused", reason="factory checkout interlock unavailable"
                )
            # Keep checkout protection even after the worker dies, until failure
            # preservation, terminal handling and claim cleanup have finished.
            bound_runner = (
                self
                if reservation_id == self.outcome_reservation_id
                else replace(self, outcome_reservation_id=reservation_id)
            )
            return await bound_runner._run_factory_070_locked(
                issue_id, autonomous=autonomous
            )

    async def _run_factory_070_locked(
        self,
        issue_id: int,
        *,
        autonomous: bool,
    ) -> WorklinkRunResult:
        from .autonomy import factory_max_concurrent

        reservation_id = self.outcome_reservation_id or _outcome_reservation(
            self.home, issue_id, target="factory", autonomous=autonomous
        )
        runner = self.runner or _runner_for_home(self.home, self.chainlink_bin)
        issue_reader = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner)
        try:
            issue = issue_reader.read(issue_id)
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_issue_read", cause="read_failed", validator="chainlink_issue", result=type(exc).__name__)
            raise
        if "worklink:epic" not in issue.labels:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_issue_kind", cause="not_epic", validator="epic_label", result="not_epic")
            _log_event(
                "worklink_epic_refused",
                issue_id=issue_id,
                reason="not an epic issue",
            )
            return WorklinkRunResult(issue_id, None, "failed", reason="not an epic issue")
        try:
            validate_leaf(issue)
        except LeafValidationError as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_target_branch", cause="invalid_target_branch", validator="target_branch", result=type(exc).__name__)
            raise
        try:
            config = WorklinkConfig.load(self.home / "worklink.yaml")
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_config", cause="configuration_invalid", validator="worklink_config", result=type(exc).__name__)
            raise
        registry = self.registry or BackendRegistry(config)
        try:
            selected = registry.get("feature_factory")
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_backend", cause="configuration_invalid", validator="backend_selection", result=type(exc).__name__)
            raise
        if not isinstance(selected, FeatureFactoryBackend):
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_backend", cause="configuration_invalid", validator="backend_implementation", result="invalid")
            raise WorklinkError("feature_factory backend has an invalid implementation")
        try:
            repo_url = _repo_remote_url(self.repo, runner=runner)
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_repository", cause="configuration_invalid", validator="repository_origin", result=type(exc).__name__)
            raise
        repo_slug = _repo_slug_from_url(repo_url)
        if repo_slug is None:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_repository", cause="configuration_invalid", validator="repository_origin", result="invalid")
            raise WorklinkError("factory repository must have a canonical GitHub origin")
        try:
            compute = registry.select_compute(labels=issue.labels, repo=repo_slug)
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_compute", cause="configuration_invalid", validator="compute_selection", result=type(exc).__name__)
            raise
        if compute.name != "local_subprocess":
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_compute", cause="configuration_invalid", validator="compute_selection", result="not_local_subprocess")
            raise WorklinkError("factory runs require local_subprocess supervision")
        if isinstance(compute, LocalSubprocessComputeBackend):
            runner = _factory_git_runner(runner)
        if autonomous:
            allowed, reason = config.autonomous_compute_allowed(
                compute.name, compute.capabilities()
            )
            if not allowed:
                _log_event(
                    "worklink_autonomous_refused",
                    issue_id=issue_id,
                    compute_backend=compute.name,
                    reason=reason,
                )
                return WorklinkRunResult(issue_id, None, "refused", reason=reason)
        try:
            launcher = selected.admit()
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_launcher", cause="launcher_unavailable", validator="factory_launcher", result=type(exc).__name__)
            raise
        try:
            inventory = RepositoryInventory.load(self.home / "repositories.yaml")
        except Exception as exc:
            _record_preclaim_input(self.home, issue_id, reservation_id, source="factory_inventory", cause="configuration_invalid", validator="repository_inventory", result=type(exc).__name__)
            raise
        repository_config = inventory.repository(repo_slug) if inventory.declared else None
        base = (
            target_branch_from_description(issue.description)
            or (repository_config.base_branch if repository_config is not None else None)
            or config.defaults.base_branch
        )
        base_check = runner(
            [
                "git",
                "-C",
                str(self.repo),
                "ls-remote",
                "--exit-code",
                "origin",
                f"refs/heads/{base.removeprefix('origin/')}",
            ]
        )
        if base_check.returncode != 0:
            reason = (
                f"base branch does not exist in origin: {base}"
                if base_check.returncode == 2
                else (
                    "base branch lookup failed for origin: "
                    f"{base} (git ls-remote exit code {base_check.returncode})"
                )
            )
            _log_event(
                "worklink_epic_refused",
                issue_id=issue_id,
                reason=reason,
            )
            _record_preclaim_input(
                self.home,
                issue_id,
                reservation_id,
                source="factory_base_lookup",
                cause=("base_missing" if base_check.returncode == 2 else "base_read_failed"),
                validator="git_ls_remote",
                result=f"returncode_{base_check.returncode}",
            )
            return WorklinkRunResult(
                issue_id,
                None,
                "refused",
                reason=reason,
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
            max_attempts=config.defaults.max_claim_attempts,
        )
        issue = issue_reader.read(issue_id)
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue_id, reservation_id=reservation_id,
            source="factory_work_item", cause="work_item_invalid",
            operation="work_item",
        ):
            work_item_json = render_work_item(issue)
            run_id = _validate_epic_work_item(work_item_json, issue.issue_id)
        retained: FactoryRunRecord | None = None

        class FactoryRecoveryBlocked(WorklinkError):
            def __init__(self, record: FactoryRunRecord, reason: str) -> None:
                super().__init__(reason)
                self.record = record

        def prepare_factory_claim() -> None:
            nonlocal retained
            candidates = load_factory_records_for_issue(self.home, issue_id)
            if not candidates:
                return
            candidate = candidates[0]
            try:
                _verify_factory_recovery_target(
                    runner=self,
                    issue=issue,
                    retained=candidate,
                    launcher=launcher,
                    repo_slug=repo_slug,
                    base=base,
                    command_runner=runner,
                )
            except WorklinkError as exc:
                _record_factory_stage_failure(
                    home=self.home,
                    issue_id=issue_id,
                    reservation_id=reservation_id,
                    claim_record=None,
                    source="factory_retained_binding",
                    cause="recovery_binding_invalid",
                    record=candidate,
                    checkout=Path(candidate.sandbox),
                    error=exc,
                )
                raise FactoryRecoveryBlocked(candidate, str(exc)) from exc
            retained = candidate

        try:
            claim = claims.claim_issue(
                issue_id,
                issue.comments,
                labels=issue.labels,
                max_active_locks=factory_max_concurrent(),
                active_label=WORKLINK_EPIC_LABEL,
                before_claim=prepare_factory_claim,
                **({"reservation_id": reservation_id} if reservation_id is not None else {}),
            )
        except FactoryRecoveryBlocked as exc:
            reason = f"retained factory sandbox {exc.record.sandbox}: {exc}"
            _log_event(
                "worklink_factory_recovery_blocked",
                issue_id=issue_id,
                attempt=exc.record.attempt,
                sandbox=exc.record.sandbox,
                reason=str(exc),
            )
            current_issue = issue_reader.read(issue_id)
            if "worklink:in-progress" not in current_issue.labels:
                claims.transition_issue(
                    issue_id,
                    status="blocked",
                    review_ready=False,
                    attempt=exc.record.attempt,
                    reason=reason,
                )
            else:
                reason += "; current in-progress owner was left unchanged"
            return WorklinkRunResult(
                issue_id,
                exc.record.attempt,
                "blocked",
                checkout=Path(exc.record.sandbox),
                branch=exc.record.branch,
                reason=reason,
            )
        if claim.attempts_exhausted:
            _log_event(
                "worklink_attempts_exhausted",
                issue_id=issue_id,
                reason="attempts_exhausted",
            )
            _record_claim_refusal_outcome(
                self.home,
                issue_id,
                reservation_id,
                reason="attempts_exhausted",
            )
            return WorklinkRunResult(issue_id, None, "blocked", reason="attempts_exhausted")
        if not claim.claimed or claim.record is None:
            reason = claim.reason or "claim_failed"
            _log_event(
                "worklink_claim_failed",
                issue_id=issue_id,
                reason=reason,
            )
            _record_claim_refusal_outcome(
                self.home,
                issue_id,
                reservation_id,
                reason=reason,
            )
            return WorklinkRunResult(
                issue_id, None, _claim_refusal_status(reason), reason=reason
            )
        claim_record = claim.record
        _log_event(
            "worklink_claimed",
            issue_id=issue_id,
            attempt=claim_record.attempt,
            backend=selected.name,
        )
        lease: CheckoutLease | None = None
        factory_stage = (
            "factory_recovery_binding", "recovery_binding_invalid"
        ) if retained is not None else ("factory_checkout", "checkout_failed")
        try:
            if retained is not None:
                if reservation_id is not None:
                    _record_lifecycle_start(
                        self.home,
                        issue_id=issue_id,
                        reservation_id=reservation_id,
                        record=claim_record,
                        run_id=run_id,
                        sandbox=retained.sandbox,
                    )
                result = await self._recover_factory_070(
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
            else:
                factory_stage = ("factory_checkout", "checkout_failed")
                lease = _create_backend_checkout(
                    self.repo,
                    issue_id=issue_id,
                    attempt=claim_record.attempt,
                    base=base,
                    backend=selected,
                    base_fetch=config.defaults.base_fetch,
                    event_logger=_log_event,
                    runner=_list_runner(runner),
                    worker_eligible=isinstance(compute, LocalSubprocessComputeBackend),
                )
                factory_stage = ("factory_git_identity", "identity_unavailable")
                git_name, git_email = _read_checkout_git_identity(lease.path, runner)
                factory_stage = ("factory_publishing_identity", "identity_unavailable")
                publishing_identity, publishing_identity_source = (
                    _read_factory_publishing_identity(self.repo)
                )
                factory_stage = ("factory_credential", "identity_unavailable")
                github_token, github_env = _resolve_factory_github_credential(os.environ)
                try:
                    factory_stage = ("factory_identity_verify", "identity_mismatch")
                    GitHubForgeClient(token=github_token).verify_identity(publishing_identity)
                except GitHubIdentityVerificationError as exc:
                    raise WorklinkError(
                        f"{exc}; selected identity {publishing_identity} "
                        f"from {publishing_identity_source}"
                    ) from exc
                order = WorkOrder(
                    issue_id=issue_id,
                    checkout=lease.path,
                    prompt=_epic_prompt(issue),
                    rules=None,
                    timeout_s=int(_epic_run_timeout_s()),
                    env={
                        "MIMIR_HOME": str(self.home),
                        "MIMIR_WORK_ITEM_JSON": work_item_json,
                        **github_env,
                        "GIT_AUTHOR_NAME": git_name,
                        "GIT_AUTHOR_EMAIL": git_email,
                        "GIT_COMMITTER_NAME": git_name,
                        "GIT_COMMITTER_EMAIL": git_email,
                        # The factory child does the publishing, and 0.8.0+ compares this
                        # declared identity against ``gh api /user`` at Gate 1.
                        #
                        # Two DIFFERENT names are in play: the operator selects the identity
                        # with MIMIR_FACTORY_PUBLISHING_IDENTITY, the child reads the
                        # factory's own FACTORY_PUBLISHING_IDENTITY. Forward the value the
                        # controller already resolved and verified above, under the child's
                        # name -- inheritance alone would carry nothing in the local case,
                        # where the identity comes from .factory.json and no variable is
                        # exported at all. This also keeps the controller authoritative over
                        # whatever .factory.json the sandbox happens to hold.
                        FACTORY_PUBLISHING_IDENTITY_ENV: publishing_identity,
                    },
                    transcript_root=self.home / "state" / "worklink" / "transcripts",
                )
                factory_stage = ("factory_spec", "work_spec_failed")
                spec = selected.work_spec(
                    order,
                    attempt=claim_record.attempt,
                    repo_url=repo_url,
                    base_ref=lease.base_ref,
                    branch=f"feature/{run_id}",
                    test_command=test_cmd,
                )
                factory_stage = ("factory_launch_binding", "recovery_binding_invalid")
                _require_factory_launch_binding(spec, run_id, publishing_identity)
                factory_record = FactoryRunRecord(
                    run_id=run_id,
                    issue_id=issue_id,
                    attempt=claim_record.attempt,
                    repository=repo_slug,
                    base_ref=base,
                    branch=f"feature/{run_id}",
                    launcher=str(launcher),
                    sandbox=str(lease.path / ".factory-sandboxes" / run_id),
                    session=None,
                    handle=None,
                    status=None,
                    observed_at=None,
                    controller_phase="running",
                    transcript=None,
                )
                if reservation_id is not None:
                    _record_lifecycle_start(
                        self.home,
                        issue_id=issue_id,
                        reservation_id=reservation_id,
                        record=claim_record,
                        run_id=run_id,
                        sandbox=factory_record.sandbox,
                    )
                factory_stage = ("factory_sandbox", "sandbox_failed")
                _create_factory_sandbox(factory_record, lease)
                factory_stage = ("factory_permissions", "permissions_failed")
                _prepare_factory_sandbox_permissions(
                    lease.path / ".factory-sandboxes",
                    worker_uid_drop=isinstance(compute, LocalSubprocessComputeBackend),
                )
                factory_stage = ("factory_launch", "launch_failed")
                handle = await compute.launch(spec)
                factory_record = replace(factory_record, handle=handle)
                try:
                    factory_stage = ("factory_handle_save", "state_write_failed")
                    save_factory_record(self.home, factory_record)
                except BaseException:
                    await _cancel_and_cleanup_factory_handle(compute, handle)
                    raise
                factory_stage = ("factory_wait_start", "supervision_failed")
                result = await self._supervise_factory_070(
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
            original_reason = str(exc)
            try:
                records = load_factory_records_for_issue(self.home, issue_id)
                current = records[0] if records else None
            except Exception:
                current = None
            _record_factory_stage_failure(
                home=self.home,
                issue_id=issue_id,
                reservation_id=reservation_id,
                claim_record=claim_record,
                source=factory_stage[0],
                cause=factory_stage[1],
                record=current,
                checkout=lease.path if lease is not None else None,
                error=exc,
            )
            preserved_ref, preservation_error = _preserve_failed_factory_run(
                home=self.home,
                trusted_repo=self.repo,
                record=current,
            )
            reason = original_reason
            if preserved_ref is not None:
                reason += f"; completed work preserved at {preserved_ref}"
            elif preservation_error is not None:
                reason += f"; completed work preservation failed: {preservation_error}"
            if current is not None:
                save_factory_record(
                    self.home,
                    replace(
                        current,
                        controller_phase="failed",
                        controller_error=_factory_controller_error(reason),
                    ),
                )
            claims.transition_issue(
                issue_id,
                status="failed",
                review_ready=False,
                attempt=claim_record.budget_attempt or claim_record.attempt,
                reason=reason,
            )
            _log_event(
                "worklink_transition",
                issue_id=issue_id,
                attempt=claim_record.attempt,
                status="failed",
                review_ready=False,
                pr_url=None,
                reason=reason,
                **(
                    {
                        "preserved_ref": preserved_ref,
                        "preservation_error": preservation_error,
                    }
                    if preserved_ref is not None or preservation_error is not None
                    else {}
                ),
            )
            result = WorklinkRunResult(
                issue_id,
                claim_record.attempt,
                "failed",
                checkout=Path(current.sandbox) if current is not None else None,
                branch=current.branch if current is not None else None,
                reason=reason,
                preserved_ref=preserved_ref,
                preservation_error=preservation_error,
            )
        finally:
            released = _release_issue_and_clear_run_state(
                claims,
                home=self.home,
                issue_id=issue_id,
                attempt=claim_record.attempt,
                trigger_ready_scan=autonomous,
            )
        if not released:
            _record_factory_terminal_outcome(
                home=self.home,
                reservation_id=reservation_id,
                claim_record=claim_record,
                record=None,
                source="factory_terminal_release",
                cause="terminal_routing_failed",
            )
            reason = "terminal recovery incomplete: Chainlink lock release failed"
            if result.reason:
                reason = f"{result.reason}; {reason}"
            result = replace(result, status="failed", reason=reason)
        return result

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
        sandbox = _verify_factory_recovery_binding(
            runner=self,
            issue=issue,
            claim_record=claim_record,
            claims=claims,
            retained=retained,
            launcher=launcher,
            repo_slug=repo_slug,
            base=base,
            command_runner=runner,
        )
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue.issue_id,
            reservation_id=self.outcome_reservation_id,
            source="factory_recovery_status", cause="recovery_state_invalid",
            claim_record=claim_record, checkout=sandbox,
            branch=retained.branch, factory_record=retained,
            operation="recovery_status",
        ):
            pre = backend.status(retained.run_id, sandbox=sandbox, launcher=retained.launcher)
        _require_factory_status(pre, retained)
        historical_result = _opaque_json_bytes(pre.terminal_result)
        if retained.controller_phase == "terminal" and not pre.is_terminal:
            raise WorklinkError("factory recovery terminal lifecycle regressed")
        if pre.lock_session not in {None, retained.session}:
            raise WorklinkError("factory recovery lock owner changed")
        if pre.lock == "absent" and pre.lock_session is not None:
            raise WorklinkError("factory recovery absent lock has an owner")
        if pre.lock == "stale" and pre.lock_session != retained.session:
            raise WorklinkError("factory recovery lock owner does not match retained session")
        if pre.lock == "fresh" and pre.lock_session != retained.session:
            raise WorklinkError("factory recovery found a fresh foreign lock owner")
        if pre.is_terminal:
            if retained.handle is not None:
                if factory_process_is_alive(retained):
                    await _cancel_and_cleanup_factory_handle(compute, retained.handle)
                elif not factory_process_is_verified_dead(retained):
                    raise WorklinkError("factory terminal process identity cannot be verified")
            _retain_factory_observation(
                self.home,
                replace(retained, status=pre),
                self.outcome_reservation_id,
            )
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
        if pre.status not in {"running", "needs-human"}:
            raise WorklinkError("factory recovery status is not resumable")
        if factory_process_is_alive(retained):
            raise WorklinkError("factory recovery refuses a live retained process")
        if not factory_process_is_verified_dead(retained):
            raise WorklinkError("factory recovery cannot verify the retained process is dead")
        session = retained.session
        if pre.lock == "stale" or pre.dead_lock:
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue.issue_id,
                reservation_id=self.outcome_reservation_id,
                source="factory_recovery_lock", cause="lock_reconciliation_failed",
                claim_record=claim_record, checkout=sandbox,
                branch=retained.branch, factory_record=retained,
                operation="steal_lock",
            ):
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
        elif pre.lock == "absent":
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue.issue_id,
                reservation_id=self.outcome_reservation_id,
                source="factory_recovery_lock", cause="lock_reconciliation_failed",
                claim_record=claim_record, checkout=sandbox,
                branch=retained.branch, factory_record=retained,
                operation="claim_lock",
            ):
                backend.lock(
                    retained.run_id,
                    "claim",
                    session=session,
                    sandbox=sandbox,
                    launcher=retained.launcher,
                )
                locked = backend.status(
                    retained.run_id, sandbox=sandbox, launcher=retained.launcher
                )
        else:
            locked = pre
        _require_factory_status(locked, retained)
        if (
            locked.lock != "fresh"
            or locked.dead_lock
            or locked.lock_session != session
            or _opaque_json_bytes(locked.terminal_result) != historical_result
        ):
            raise WorklinkError("factory recovery lock reconciliation failed")
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue.issue_id,
            reservation_id=self.outcome_reservation_id,
            source="factory_resume", cause="resume_failed",
            claim_record=claim_record, checkout=sandbox,
            branch=retained.branch, factory_record=retained,
            operation="resume",
        ):
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
            or resumed.dead_lock
            or resumed.lock_session != session
            or _opaque_json_bytes(resumed.terminal_result) != historical_result
        ):
            raise WorklinkError("factory resume did not return an owned running status")
        resumed.require_recovery_next()
        _verify_factory_recovery_binding(
            runner=self,
            issue=issue,
            claim_record=claim_record,
            claims=claims,
            retained=retained,
            launcher=launcher,
            repo_slug=repo_slug,
            base=base,
            command_runner=runner,
        )
        order = WorkOrder(
            issue_id=issue.issue_id,
            checkout=sandbox,
            prompt=_epic_prompt(issue),
            rules=None,
            timeout_s=int(_epic_run_timeout_s()),
            env={"MIMIR_HOME": str(self.home)},
            transcript_root=self.home / "state" / "worklink" / "transcripts",
        )
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue.issue_id,
            reservation_id=self.outcome_reservation_id,
            source="factory_recovery_repository", cause="repository_changed",
            claim_record=claim_record, checkout=sandbox,
            branch=retained.branch, factory_record=retained,
            operation="repository",
        ):
            recovery_repo_url = _repo_remote_url(sandbox, runner=runner)
        if (
            _factory_checkout_repository(sandbox, runner) or ""
        ).lower() != retained.repository.lower():
            raise WorklinkError("factory recovery sandbox repository changed before launch")
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue.issue_id,
            reservation_id=self.outcome_reservation_id,
            source="factory_recovery_spec", cause="work_spec_failed",
            claim_record=claim_record, checkout=sandbox,
            branch=retained.branch, factory_record=retained,
            operation="work_spec",
        ):
            spec = backend.work_spec(
                order,
                attempt=retained.attempt,
                repo_url=recovery_repo_url,
                base_ref=retained.base_ref,
                branch=retained.branch,
                test_command=test_cmd,
                session=session,
                run_id=retained.run_id,
            )
        if isinstance(compute, LocalSubprocessComputeBackend):
            # Authorization stays at the original inner checkout; --dir still
            # selects the retained sandbox validated above. Session data is
            # attempt-scoped as on the initial launch.
            spec = replace(spec, local_checkout=sandbox.parent.parent)
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue.issue_id,
            reservation_id=self.outcome_reservation_id,
            source="factory_recovery_launch", cause="launch_failed",
            claim_record=claim_record, checkout=sandbox,
            branch=retained.branch, factory_record=retained,
            operation="recovery_launch",
        ):
            handle = await compute.launch(spec)
        _retain_factory_observation(
            self.home,
            replace(retained, status=resumed),
            self.outcome_reservation_id,
        )
        relaunched = replace(
            retained.observed(resumed, datetime.now(UTC).isoformat()),
            handle=handle,
            controller_phase="running",
            transcript=None,
        )
        try:
            with _typed_outcome_boundary(
                home=self.home, issue_id=issue.issue_id,
                reservation_id=self.outcome_reservation_id,
                source="factory_recovery_save", cause="state_write_failed",
                claim_record=claim_record, checkout=sandbox,
                branch=retained.branch, factory_record=relaunched,
                operation="recovery_save",
            ):
                save_factory_record(self.home, relaunched)
        except BaseException:
            await _cancel_and_cleanup_factory_handle(compute, handle)
            raise
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
            initial_status=resumed,
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
        initial_status: FactoryStatus | None = None,
    ) -> WorklinkRunResult:
        handle = factory_record.handle
        if handle is None:
            raise WorklinkError("factory supervision requires a launch handle")
        loop = asyncio.get_running_loop()
        run_timeout = _epic_run_timeout_s()
        deadline = loop.time() + run_timeout
        startup_deadline = min(deadline, loop.time() + _FACTORY_STARTUP_STATUS_TIMEOUT_S)
        stale_after = _epic_stale_heartbeat_s()
        last_status: FactoryStatus | None = None
        last_change = loop.time()
        try:
            wait_task = asyncio.create_task(
                compute.wait(
                    handle,
                    max(1, math.ceil(run_timeout + _FACTORY_STARTUP_STATUS_TIMEOUT_S)),
                )
            )
        except BaseException:
            await _cancel_and_cleanup_factory_handle(compute, handle)
            raise
        status = initial_status
        failed = True
        cancel_attempted = False
        wait_result: ComputeResult | None = None
        supervision_stage = ("factory_wait_start", "supervision_failed")

        def retain_result(result: ComputeResult | None, outcome: str) -> None:
            nonlocal factory_record, supervision_stage
            if factory_record.transcript is not None:
                return
            path = transcript_path(
                self.home / "state" / "worklink" / "transcripts",
                issue.issue_id,
            )
            supervision_stage = ("factory_transcript", "transcript_failed")
            write_transcript(
                path,
                command=() if result is None else result.command,
                exit_code=None if result is None else result.exit_code,
                status=outcome,
                stdout="" if result is None else result.stdout,
                stderr=(
                    "OpenCode supervision result was not drainable"
                    if result is None
                    else result.stderr
                ),
                timed_out=False if result is None else result.timed_out,
                output_overflow=False if result is None else result.output_overflow,
                stdout_path=None if result is None else result.stdout_path,
                stderr_path=None if result is None else result.stderr_path,
            )
            factory_record = replace(factory_record, transcript=str(path))
            save_factory_record(self.home, factory_record)

        async def cancel_once() -> None:
            nonlocal cancel_attempted
            if cancel_attempted:
                return
            cancel_attempted = True
            await compute.cancel(handle)

        try:
            while True:
                if status is None:
                    remaining = startup_deadline - loop.time() if last_status is None else deadline - loop.time()
                    if remaining <= 0:
                        if last_status is None:
                            supervision_stage = (
                                "factory_startup_deadline", "startup_timeout"
                            )
                            raise WorklinkError("factory never initialised before startup deadline")
                        supervision_stage = ("factory_run_deadline", "run_timeout")
                        raise WorklinkError(f"factory exceeded run timeout ({run_timeout:.0f}s)")
                    try:
                        supervision_stage = ("factory_status_read", "status_read_failed")
                        status = await asyncio.wait_for(
                            asyncio.to_thread(
                                backend.status,
                                factory_record.run_id,
                                sandbox=Path(factory_record.sandbox),
                                launcher=factory_record.launcher,
                            ),
                            timeout=remaining,
                        )
                    except TimeoutError as exc:
                        if last_status is None:
                            supervision_stage = (
                                "factory_startup_deadline", "startup_timeout"
                            )
                            raise WorklinkError(
                                "factory never initialised before startup deadline"
                            ) from exc
                        supervision_stage = ("factory_run_deadline", "run_timeout")
                        raise WorklinkError(f"factory exceeded run timeout ({run_timeout:.0f}s)") from exc
                pre_manifest = last_status is None and status == FactoryStatus(
                    run_id=factory_record.run_id,
                    valid=False,
                    sandbox_path=factory_record.sandbox,
                )
                if pre_manifest:
                    if loop.time() >= startup_deadline:
                        supervision_stage = (
                            "factory_startup_deadline", "startup_timeout"
                        )
                        raise WorklinkError("factory never initialised before startup deadline")
                else:
                    supervision_stage = (
                        "factory_status_binding", "status_binding_invalid"
                    )
                    _require_factory_status(status, factory_record)
                    if status != last_status:
                        last_status = status
                        last_change = loop.time()
                    if factory_record.session is not None and status.lock_session not in {
                        None,
                        factory_record.session,
                    }:
                        supervision_stage = ("factory_owner", "owner_changed")
                        raise WorklinkError("factory lock owner changed")
                    supervision_stage = (
                        "factory_observation_save", "state_write_failed"
                    )
                    _retain_factory_observation(
                        self.home,
                        replace(factory_record, status=status),
                        self.outcome_reservation_id,
                    )
                    factory_record = factory_record.observed(status, datetime.now(UTC).isoformat())
                    phase = (
                        "parked"
                        if status.is_parked
                        else "terminal" if status.is_terminal else "running"
                    )
                    factory_record = replace(factory_record, controller_phase=phase)
                    save_factory_record(self.home, factory_record)
                    if status.lock == "fresh" and factory_record.session == status.lock_session:
                        supervision_stage = ("factory_heartbeat", "heartbeat_failed")
                        await asyncio.to_thread(
                            backend.heartbeat,
                            factory_record.run_id,
                            session=factory_record.session,
                            sandbox=Path(factory_record.sandbox),
                            launcher=factory_record.launcher,
                        )
                    if loop.time() - last_change >= stale_after:
                        _log_event(
                            "worklink_factory_stale_status",
                            issue_id=issue.issue_id,
                            diagnostic_after_s=stale_after,
                            lock=status.lock,
                            process_alive=compute.job_alive(handle),
                        )
                    if status.is_terminal or status.is_parked:
                        if not wait_task.done():
                            await cancel_once()
                            supervision_stage = (
                                "factory_wait_drain", "result_unavailable"
                            )
                            wait_result = await _finish_factory_wait_task(wait_task)
                        else:
                            wait_result = await wait_task
                        retain_result(wait_result, status.status or phase)
                        failed = False
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
                if wait_task.done() or not compute.job_alive(handle):
                    try:
                        supervision_stage = (
                            "factory_wait_drain", "result_unavailable"
                        )
                        result = await asyncio.wait_for(asyncio.shield(wait_task), timeout=5)
                        wait_result = result
                    except TimeoutError as exc:
                        raise WorklinkError(
                            "OpenCode process stopped without a drainable supervision result"
                        ) from exc
                    retain_result(result, "failed")
                    supervision_stage = ("factory_driver_exit", "unfinished_exit")
                    detail = result.stderr.strip() or result.stdout.strip()
                    if _factory_lock_refusal(result):
                        reason = "factory driver did not acquire the retained run lock"
                        factory_record = replace(
                            factory_record,
                            controller_phase="parked",
                            controller_error=_factory_controller_error(reason),
                        )
                        save_factory_record(self.home, factory_record)
                        claims.transition_issue(
                            issue.issue_id,
                            status="blocked",
                            review_ready=False,
                            attempt=claim_record.budget_attempt or claim_record.attempt,
                            reason=f"{reason}; retained sandbox: {factory_record.sandbox}",
                        )
                        _log_event(
                            "worklink_factory_lock_refused",
                            issue_id=issue.issue_id,
                            attempt=factory_record.attempt,
                            sandbox=factory_record.sandbox,
                            reason=detail[:300] or reason,
                        )
                        failed = False
                        return WorklinkRunResult(
                            issue.issue_id,
                            factory_record.attempt,
                            "needs-human",
                            checkout=Path(factory_record.sandbox),
                            branch=factory_record.branch,
                            reason=reason,
                        )
                    suffix = f": {detail[:300]}" if detail else ""
                    raise WorklinkError(
                        f"OpenCode process exited while factory status was running{suffix}"
                    )
                if loop.time() >= deadline:
                    supervision_stage = ("factory_run_deadline", "run_timeout")
                    raise WorklinkError(f"factory exceeded run timeout ({run_timeout:.0f}s)")
                _heartbeat_claim_best_effort(claims, claim_record)
                poll_delay = max(0.01, float(backend.poll_interval_s))
                if pre_manifest:
                    poll_delay = min(poll_delay, max(0, startup_deadline - loop.time()))
                await asyncio.sleep(poll_delay)
                status = None
        except Exception as exc:
            _record_factory_stage_failure(
                home=self.home,
                issue_id=issue.issue_id,
                reservation_id=self.outcome_reservation_id,
                claim_record=claim_record,
                source=supervision_stage[0],
                cause=supervision_stage[1],
                record=factory_record,
                checkout=Path(factory_record.sandbox),
                error=exc,
            )
            raise
        finally:
            try:
                if failed and not wait_task.done():
                    try:
                        await cancel_once()
                    finally:
                        wait_result = await _finish_factory_wait_task(wait_task)
                        retain_result(wait_result, "refused")
                elif wait_task.done():
                    drained = await asyncio.gather(wait_task, return_exceptions=True)
                    if wait_result is None and drained and isinstance(drained[0], ComputeResult):
                        wait_result = drained[0]
                    retain_result(wait_result, "refused" if failed else "completed")
            finally:
                try:
                    await compute.cleanup(handle)
                except Exception as exc:
                    _record_factory_stage_failure(
                        home=self.home,
                        issue_id=issue.issue_id,
                        reservation_id=self.outcome_reservation_id,
                        claim_record=claim_record,
                        source="factory_cleanup",
                        cause="cleanup_failed",
                        record=factory_record,
                        checkout=Path(factory_record.sandbox),
                        error=exc,
                        allow_after_stop=True,
                    )
                    raise

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
            park_report = (
                f"factory run is parked; control-plane snapshot: {status.park_snapshot}"
                if status.park_snapshot is not None
                else "factory run is parked; no control-plane snapshot published"
            )
            _record_factory_terminal_outcome(
                home=self.home,
                reservation_id=self.outcome_reservation_id,
                claim_record=claim_record,
                record=factory_record,
                source="factory_parked",
                cause="needs_human",
            )
            claims.transition_issue(
                issue.issue_id,
                status="blocked",
                review_ready=False,
                attempt=claim_record.budget_attempt or claim_record.attempt,
                reason=park_report,
            )
            result = WorklinkRunResult(
                issue.issue_id,
                factory_record.attempt,
                "needs-human",
                checkout=Path(factory_record.sandbox),
                branch=factory_record.branch,
                reason=park_report,
                next=status.next,
                next_present=status.next_present,
            )
            _log_event(
                "worklink_transition",
                issue_id=issue.issue_id,
                attempt=factory_record.attempt,
                status=result.status,
                review_ready=result.review_ready,
                pr_url=result.pr_url,
                reason=result.reason,
                sandbox=factory_record.sandbox,
            )
            return result
        if status.status in {"blocked", "partial"}:
            _record_factory_terminal_outcome(
                home=self.home,
                reservation_id=self.outcome_reservation_id,
                claim_record=claim_record,
                record=factory_record,
                source=("factory_partial" if status.status == "partial" else "factory_blocked"),
                cause=status.status,
            )
            claims.transition_issue(
                issue.issue_id,
                status="blocked",
                review_ready=False,
                attempt=claim_record.budget_attempt or claim_record.attempt,
                reason=f"factory status: {status.status}",
            )
            result = WorklinkRunResult(
                issue.issue_id,
                factory_record.attempt,
                status.status,
                pr_url=status.pr_url,
                checkout=Path(factory_record.sandbox),
                branch=factory_record.branch,
                reason=f"factory status: {status.status}",
                next=status.next,
                next_present=status.next_present,
            )
            _log_event(
                "worklink_transition",
                issue_id=issue.issue_id,
                attempt=factory_record.attempt,
                status=result.status,
                review_ready=result.review_ready,
                pr_url=result.pr_url,
                reason=result.reason,
            )
            return result
        with _typed_outcome_boundary(
            home=self.home, issue_id=issue.issue_id,
            reservation_id=self.outcome_reservation_id,
            source="factory_completion_verify", cause="completion_invalid",
            claim_record=claim_record, checkout=Path(factory_record.sandbox),
            branch=factory_record.branch, factory_record=factory_record,
            operation="completion_verify",
        ):
            evidence_path, pr_url = await _verify_factory_completion(
                home=self.home,
                issue=issue,
                record=factory_record,
                test_command=test_cmd,
                gate_rerun_max_failures=WorklinkConfig.load(
                    self.home / "worklink.yaml"
                ).defaults.gate_rerun_max_failures,
                started_at=started_at,
                runner=runner,
            )
        claims.transition_issue(
            issue.issue_id,
            status="review",
            review_ready=True,
            attempt=claim_record.budget_attempt or claim_record.attempt,
        )
        result = WorklinkRunResult(
            issue.issue_id,
            factory_record.attempt,
            "review_ready",
            review_ready=True,
            pr_url=pr_url,
            evidence_path=evidence_path,
            checkout=Path(factory_record.sandbox),
            branch=factory_record.branch,
            next=status.next,
            next_present=status.next_present,
        )
        _log_event(
            "worklink_transition",
            issue_id=issue.issue_id,
            attempt=factory_record.attempt,
            status=result.status,
            review_ready=result.review_ready,
            pr_url=result.pr_url,
        )
        return result

async def _finish_factory_wait_task(
    task: asyncio.Task[ComputeResult],
) -> ComputeResult | None:
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=5)
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return None


def _factory_controller_error(error: BaseException | str) -> str:
    encoded = redact_text(str(error)).encode("utf-8")
    if len(encoded) <= _FACTORY_CONTROLLER_ERROR_MAX_BYTES:
        return encoded.decode("utf-8")
    marker = b"[controller error truncated; see transcript]"
    prefix = encoded[: _FACTORY_CONTROLLER_ERROR_MAX_BYTES - len(marker)].decode(
        "utf-8", errors="ignore"
    )
    return prefix.rstrip() + marker.decode("ascii")


def _preserve_failed_factory_run(
    *,
    home: Path,
    trusted_repo: Path,
    record: FactoryRunRecord | None,
) -> tuple[str | None, str | None]:
    """Best-effort publication for post-merge failures before terminal release."""
    if (
        record is None
        or record.status is None
        or record.status.pr_url is not None
        or record.status.slices is None
        or not any(row["status"] == "merged" for row in record.status.slices)
    ):
        return None, None
    try:
        checkout_fd = os.open(
            Path(record.sandbox),
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            publication = ControllerGitPublication.capture(
                checkout_fd,
                trusted_repo,
                record.branch,
                home / "state" / "worklink" / "publication",
            )
        finally:
            os.close(checkout_fd)
        with publication:
            publication.push(check=True)
        return f"origin/{record.branch}", None
    except Exception as exc:  # noqa: BLE001 - preservation must not replace the refusal
        return None, _factory_controller_error(exc)


async def _cancel_and_cleanup_factory_handle(compute: Any, handle: LaunchHandle) -> None:
    try:
        await compute.cancel(handle)
    finally:
        await compute.cleanup(handle)


def _opaque_json_bytes(value: dict[str, Any] | None) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _verify_factory_recovery_binding(
    *,
    runner: WorklinkRunner,
    issue: IssueContext,
    claim_record: ClaimRecord,
    claims: ChainlinkClaims,
    retained: FactoryRunRecord,
    launcher: Path,
    repo_slug: str,
    base: str,
    command_runner: Runner,
) -> Path:
    sandbox = _verify_factory_recovery_target(
        runner=runner,
        issue=issue,
        retained=retained,
        launcher=launcher,
        repo_slug=repo_slug,
        base=base,
        command_runner=command_runner,
    )
    if claim_record.issue_id != issue.issue_id:
        raise WorklinkError("factory recovery claim issue does not match")
    if claim_record.agent_id != claims.agent_id or claims.agent_id != runner.agent_id:
        raise WorklinkError("factory recovery claim owner does not match")
    if not getattr(claims, "_lock_still_held_by")(claim_record):
        raise WorklinkError("factory recovery claim is not retained")
    return sandbox


def _verify_factory_recovery_target(
    *,
    runner: WorklinkRunner,
    issue: IssueContext,
    retained: FactoryRunRecord,
    launcher: Path,
    repo_slug: str,
    base: str,
    command_runner: Runner,
) -> Path:
    if retained.issue_id != issue.issue_id or retained.run_id not in factory_record_run_ids(
        issue.issue_id
    ):
        raise WorklinkError("retained factory issue identity does not match recovery request")
    if retained.repository.lower() != repo_slug.lower():
        raise WorklinkError("retained factory repository does not match recovery request")
    current_repo_slug = _repo_slug_from_url(
        _repo_remote_url(runner.repo, runner=command_runner)
    )
    if current_repo_slug is None or current_repo_slug.lower() != retained.repository.lower():
        raise WorklinkError("factory recovery controller repository changed")
    if retained.base_ref != base:
        raise WorklinkError("retained factory base does not match recovery request")
    if retained.launcher != str(launcher):
        raise WorklinkError("retained factory launcher does not match recovery request")
    if retained.controller_phase not in _RECOVERABLE_FACTORY_PHASES:
        raise WorklinkError("retained factory lifecycle is not recoverable")
    if not retained.session:
        raise WorklinkError("retained factory session is missing")
    sandbox = Path(retained.sandbox)
    from .worker_client import WORKLINK_CHECKOUT_ROOT, factory_checkout_for_path

    if sandbox.is_relative_to(WORKLINK_CHECKOUT_ROOT) and factory_checkout_for_path(sandbox) is None:
        raise WorklinkError(
            "legacy factory checkout has no private ownership-transfer boundary; "
            "retain it for offline migration, not privileged in-place normalization"
        )
    if not sandbox.is_absolute() or not sandbox.is_dir() or sandbox.is_symlink():
        raise WorklinkError("retained factory sandbox is unavailable")
    _verify_factory_checkout(
        sandbox,
        retained.branch,
        retained.base_ref,
        command_runner,
        repository=retained.repository,
    )
    return sandbox


def _factory_git_runner(controller_runner: Runner) -> Runner:
    """Read retained Git metadata as the worker, not by trusting its repo as mimir."""
    from .backends.feature_factory import _control_environment
    from .worker_client import factory_checkout_for_path, run_factory_control

    def run(
        args: Sequence[str] | str,
        cwd: Path | None = None,
        *,
        text: bool = True,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        if len(args) >= 3 and list(args[:2]) == ["git", "-C"]:
            binding = factory_checkout_for_path(Path(args[2]))
            if binding is not None:
                root, _, _ = binding
                boundary = root.parent.stat(follow_symlinks=False)
                if boundary.st_uid != get_identities().mimir_uid or not stat.S_ISDIR(boundary.st_mode):
                    raise WorklinkError("factory checkout boundary is not controller-owned")
                # Initial clone setup still belongs to the controller. After
                # exposure, even read-only Git commands use the worker executor.
                if stat.S_IMODE(boundary.st_mode) != 0o2700:
                    result = run_factory_control(root, args, env=_control_environment())
                    return subprocess.CompletedProcess(
                        args, result.returncode,
                        result.stdout.decode() if text else result.stdout,
                        result.stderr.decode() if text else result.stderr,
                    )
        return controller_runner(args, cwd=cwd, text=text, **kwargs)

    return run


def _create_factory_sandbox(record: FactoryRunRecord, lease: CheckoutLease) -> Path:
    """Create the parent for a validated sandbox that the factory will create."""
    sandbox = Path(record.sandbox)
    root = lease.path / ".factory-sandboxes"
    if sandbox != root / record.run_id:
        raise WorklinkError("factory sandbox does not match the validated run identity")
    if lease.path.is_symlink() or root.is_symlink() or sandbox.is_symlink():
        raise WorklinkError("factory sandbox path may not be a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    if sandbox.exists():
        raise WorklinkError(f"factory sandbox already exists: {sandbox}")
    return sandbox


def _require_factory_status(
    status: FactoryStatus,
    record: FactoryRunRecord,
    *,
    require_pr_base: bool = False,
) -> None:
    if not status.valid:
        raise WorklinkError("factory status is invalid")
    if status.run_id != record.run_id:
        raise WorklinkError("factory status run id mismatch")
    if status.sandbox_path != record.sandbox:
        raise WorklinkError("factory status sandbox mismatch")
    if status.status is None:
        raise WorklinkError("factory status is missing lifecycle status")
    if status.mode is None:
        raise WorklinkError("factory status mode is missing")
    if status.mode != "autonomous":
        raise WorklinkError("factory status mode mismatch")
    if status.branch is None:
        raise WorklinkError("factory status branch is missing")
    if status.branch != record.branch:
        raise WorklinkError("factory status branch mismatch")
    if status.pr_draft is None:
        raise WorklinkError("factory status PR draft state is missing")
    if status.lock is None:
        raise WorklinkError("factory status lock state is missing")
    if status.dead_lock is None:
        raise WorklinkError("factory status dead-lock state is missing")
    if status.pr_base is not None and status.pr_base != record.base_ref:
        raise WorklinkError(
            "factory status base mismatch: "
            f"observed {status.pr_base!r}, expected {record.base_ref!r}"
        )
    if require_pr_base and status.pr_base is None:
        raise WorklinkError(
            "factory status base mismatch: "
            f"observed {status.pr_base!r}, expected {record.base_ref!r}"
        )


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
    *,
    repository: str | None = None,
) -> str:
    top = _fixed_command(
        runner,
        ["git", "-C", str(sandbox), "rev-parse", "--show-toplevel"],
        error="cannot read factory checkout root",
    ).stdout.strip()
    try:
        if Path(top).resolve(strict=True) != sandbox.resolve(strict=True):
            raise WorklinkError("factory checkout root mismatch")
    except OSError as exc:
        raise WorklinkError("factory checkout root is unavailable") from exc
    git_dir = _fixed_command(
        runner,
        ["git", "-C", str(sandbox), "rev-parse", "--absolute-git-dir"],
        error="cannot read factory checkout git directory",
    ).stdout.strip()
    try:
        if not Path(git_dir).resolve(strict=False).is_relative_to(sandbox.resolve(strict=True)):
            raise WorklinkError("factory checkout is not isolated")
    except OSError as exc:
        raise WorklinkError("factory checkout git directory is unavailable") from exc
    if repository is not None:
        if (_factory_checkout_repository(sandbox, runner) != repository.lower()):
            raise WorklinkError("factory checkout repository mismatch")
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


def _factory_checkout_repository(checkout: Path, runner: Runner) -> str | None:
    """Resolve a factory clone through its local lease without changing remotes."""
    current = checkout
    visited: set[Path] = set()
    for _ in range(4):
        try:
            resolved = current.resolve(strict=True)
        except OSError:
            return None
        if resolved in visited or not resolved.is_dir():
            return None
        visited.add(resolved)
        remote_result = runner(
            ["git", "-C", str(resolved), "config", "--get", "remote.origin.url"]
        )
        if remote_result.returncode != 0:
            return None
        remote = remote_result.stdout.strip()
        slug = _repo_slug_from_url(remote)
        if slug is not None:
            return slug.lower()
        push_result = runner(
            ["git", "-C", str(resolved), "config", "--get", "remote.origin.pushurl"]
        )
        push_slug = _repo_slug_from_url(
            push_result.stdout.strip() if push_result.returncode == 0 else None
        )
        if push_slug is not None:
            return push_slug.lower()
        if remote.startswith("file://"):
            remote = remote.removeprefix("file://")
        elif "://" in remote or not remote:
            return None
        candidate = Path(remote).expanduser()
        current = candidate if candidate.is_absolute() else resolved / candidate
    return None


def _factory_lock_refusal(result: ComputeResult) -> bool:
    if (
        result.exit_code != 0
        or result.timed_out
        or result.output_overflow
        or result.launch_error is not None
    ):
        return False
    stdout_lines = [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]
    if not stdout_lines:
        return False
    report = stdout_lines[-1]
    return any(
        marker in report
        for marker in (
            "did not acquire the lock",
            "did not acquire lock",
            "another live session holds",
            "different live session holds",
        )
    )


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
    gate_rerun_max_failures: int = 10,
) -> tuple[Path, str]:
    status = record.status
    evidence = WorklinkEvidence(
        issue=issue.issue_id,
        attempt=record.attempt,
        backend="feature_factory",
        branch=record.branch,
        checkout=record.sandbox,
        started_at=started_at.astimezone(UTC).isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
        files_changed=[],
        diff_stat="",
        commands=[],
        tests=None,
        pr_url=status.pr_url if status is not None else None,
        status="failed",
        base_ref=record.base_ref,
        diff_observed=False,
    )
    try:
        if status is None:
            raise WorklinkError("factory completion status is missing")
        if status.status != "completed" or not status.is_terminal:
            raise WorklinkError("factory completion status is not authoritative")
        _require_factory_status(status, record, require_pr_base=True)
        if status.slices is None or not any(row["status"] == "merged" for row in status.slices):
            raise WorklinkError("factory completion has no merged slice")
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
            sandbox,
            record.branch,
            record.base_ref,
            runner,
            repository=record.repository,
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
            gate_rerun_max_failures=gate_rerun_max_failures,
            runner=runner,
        )
        evidence = validation.evidence
        _log_gate_flaky_tests(evidence)
        if evidence.issue != issue.issue_id:
            raise WorklinkError("factory evidence issue mismatch")
        if evidence.branch != record.branch:
            raise WorklinkError("factory evidence branch mismatch")
        if evidence.checkout != record.sandbox:
            raise WorklinkError("factory evidence sandbox mismatch")
        if evidence.pr_url != status.pr_url:
            raise WorklinkError("factory evidence PR URL mismatch")
        if not evidence.diff_observed:
            raise WorklinkError("factory completion diff was not observed")
        if not evidence.files_changed:
            raise WorklinkError("factory completion diff is empty")
        tests = evidence.tests
        if tests is None:
            raise WorklinkError("factory completion test evidence is missing")
        if not tests.observed:
            raise WorklinkError("factory completion tests were not observed")
        if tests.skipped_reason is not None:
            raise WorklinkError("factory completion tests were skipped")
        if tests.exit_code != 0:
            raise WorklinkError("factory completion tests did not pass")
        if not validation.review_ready:
            raise WorklinkError("factory completion evidence was rejected")
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
            sandbox,
            record.branch,
            record.base_ref,
            runner,
            repository=record.repository,
        )
        if before_head != after_head:
            raise WorklinkError("factory checkout HEAD moved during evidence collection")
        evidence = replace(evidence, head_sha=after_head)
        api = _fixed_command(
            runner,
            ["gh", "api", f"repos/{record.repository}/pulls/{match.group(3)}"],
            error="GitHub PR verification failed",
        )
        try:
            payload = json.loads(
                api.stdout,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON value: {value}")
                ),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise WorklinkError("GitHub PR verification returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise WorklinkError("GitHub PR verification returned invalid data")
        html_url = _require_pr_string(payload, "html_url")
        state = _require_pr_string(payload, "state")
        draft = payload.get("draft")
        if not isinstance(draft, bool):
            raise WorklinkError("GitHub PR verification omitted draft")
        base_data = _require_pr_object(payload, "base")
        head_data = _require_pr_object(payload, "head")
        base_repo = _require_pr_object(base_data, "repo")
        head_repo = _require_pr_object(head_data, "repo")
        base_name = _require_pr_string(base_repo, "full_name")
        head_name = _require_pr_string(head_repo, "full_name")
        base_ref = _require_pr_string(base_data, "ref")
        head_ref = _require_pr_string(head_data, "ref")
        head_sha = _require_pr_string(head_data, "sha")
        if html_url != status.pr_url:
            raise WorklinkError("GitHub PR URL mismatch")
        if state != "open":
            raise WorklinkError("GitHub PR is not open")
        if draft:
            raise WorklinkError("GitHub PR is draft")
        if base_name.lower() != record.repository.lower():
            raise WorklinkError("GitHub PR base repository mismatch")
        if base_ref != record.base_ref:
            raise WorklinkError("GitHub PR base ref mismatch")
        if head_name.lower() != record.repository.lower():
            raise WorklinkError("GitHub PR head repository mismatch")
        if head_ref != record.branch:
            raise WorklinkError("GitHub PR head ref mismatch")
        if head_sha != after_head or evidence.head_sha != after_head:
            raise WorklinkError("GitHub PR head SHA mismatch")
        final_clean = _fixed_command(
            runner,
            [
                "git",
                "-C",
                str(sandbox),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            error="cannot reverify factory checkout cleanliness",
        )
        if final_clean.stdout.strip():
            raise WorklinkError("factory checkout became dirty during publication verification")
        final_head = _verify_factory_checkout(
            sandbox,
            record.branch,
            record.base_ref,
            runner,
            repository=record.repository,
        )
        if final_head != after_head:
            raise WorklinkError("factory checkout HEAD moved during publication verification")
        evidence_path = _write_evidence(home, evidence)
        return evidence_path, status.pr_url
    except Exception as exc:
        _write_evidence(
            home,
            replace(
                evidence,
                status="failed",
                failure_reason=str(exc),
                finished_at=datetime.now(UTC).isoformat(),
            ),
        )
        raise


def _require_pr_object(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise WorklinkError(f"GitHub PR verification omitted {field}")
    return value


def _require_pr_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise WorklinkError(f"GitHub PR verification omitted {field}")
    return value


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
                # Path-addressed attempts are siblings under one repository root.
                # Delete only this attempt; removing the parent destroys concurrent runs.
                rmtree_missing_ok(checkout)


def _release_issue_and_clear_run_state(
    claims: ChainlinkClaims,
    *,
    home: Path,
    issue_id: int,
    attempt: int,
    trigger_ready_scan: bool = False,
) -> bool:
    """Release a claim, clear local state, then notify waiting autonomous work."""
    try:
        released = claims.release_issue(issue_id)
    except BaseException as exc:
        _log_event(
            "worklink_cleanup_failed",
            issue_id=issue_id,
            attempt=attempt,
            cleanup="lock_release",
            error=str(exc),
        )
        raise
    if not released:
        _log_event(
            "worklink_cleanup_failed",
            issue_id=issue_id,
            attempt=attempt,
            cleanup="lock_release",
            error="Chainlink did not confirm lock release",
        )
        return False
    try:
        clear_run_state(home, issue_id)
    finally:
        if trigger_ready_scan:
            _trigger_ready_scan_after_release(home)
    return True


def _log_terminal_recovery_failed(
    *, issue_id: int, attempt: int, outcome: str, label_transition_applied: bool,
    state_retained: bool,
) -> None:
    _log_event(
        "worklink_terminal_recovery_failed",
        issue_id=issue_id,
        attempt=attempt,
        outcome=outcome,
        lock_released=False,
        label_transition_applied=label_transition_applied,
        state_retained=state_retained,
        error="Chainlink did not confirm lock release",
    )


def _remove_executor_report_dir_best_effort(
    report_dir: Path,
    *,
    issue_id: int,
    attempt: int,
) -> None:
    """Remove executor-owned report data without interrupting teardown."""
    try:
        rmtree_missing_ok(report_dir)
    except Exception as exc:  # noqa: BLE001 - executor-owned paths are disposable
        _log_event(
            "worklink_cleanup_failed",
            issue_id=issue_id,
            attempt=attempt,
            cleanup="executor_report_dir",
            path=str(report_dir),
            error=str(exc),
        )


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


def _run_post_publication_bookkeeping(
    name: str,
    action: Callable[[], Any],
    *,
    issue_id: int,
    attempt: int,
    pr_url: str | None,
    errors: list[str],
) -> Any | None:
    """Run secondary publication work without reopening the failure path."""
    try:
        return action()
    except Exception as exc:
        error = f"{name}: {exc}"
        errors.append(error)
        _record_post_publication_error(
            error,
            issue_id=issue_id,
            attempt=attempt,
            pr_url=pr_url,
        )
        return None


def _append_post_publication_error(reason: str | None, error: str) -> str:
    detail = f"post-publication bookkeeping failed: {error}"
    return f"{reason}; {detail}" if reason else detail


def _record_post_publication_error(
    error: str,
    *,
    issue_id: int,
    attempt: int,
    pr_url: str | None,
) -> None:
    """Report secondary publication work without reopening the failure path."""
    try:
        _log_event(
            "worklink_post_publication_failed",
            level="warning",
            issue_id=issue_id,
            attempt=attempt,
            pr_url=pr_url,
            error=error,
        )
    except Exception:
        pass


def _claim_refusal_status(reason: str | None) -> str:
    # Only expected admission outcomes are benign; lock/guard faults still page.
    if reason in {
        "duplicate_run_live",
        "claim_contention_exhausted",
        "lifecycle_state_incompatible",
        "review_ready_evidence_exists",
        "publication_intent_exists",
    } or (reason or "").startswith("concurrency cap reached ("):
        return "refused"
    return "failed"


def _outcome_reservation(
    home: Path, issue_id: int, *, target: str, autonomous: bool
) -> str | None:
    if not autonomous:
        return None
    from .dispatch_failures import (
        RESERVATION_ENV,
        dispatch_failure_state_dir,
        reservation_from_environment,
    )

    reservation_id = reservation_from_environment(
        dispatch_failure_state_dir(home),
        issue_id=issue_id,
        target=target,
        autonomous=True,
    )
    if reservation_id is None and not (
        RESERVATION_ENV in os.environ and not os.environ[RESERVATION_ENV]
    ):
        raise WorklinkError("autonomous outcome reservation was not created")
    return reservation_id


def _record_lifecycle_start(
    home: Path,
    *,
    issue_id: int,
    reservation_id: str,
    record: ClaimRecord,
    run_id: str | None = None,
    sandbox: str | None = None,
) -> None:
    from .attention import ClaimIdentity
    from .dispatch_failures import confirm_claim_and_start, dispatch_failure_state_dir

    confirm_claim_and_start(
        dispatch_failure_state_dir(home),
        issue_id=issue_id,
        reservation_id=reservation_id,
        claim=ClaimIdentity(
            issue_id=record.issue_id,
            attempt=record.attempt,
            agent_id=record.agent_id,
            claimed_at=record.claimed_at.isoformat(),
        ),
        run_id=run_id,
        sandbox=sandbox,
    )


def _record_preclaim_input(
    home: Path,
    issue_id: int,
    reservation_id: str | None,
    *,
    source: str,
    cause: str,
    validator: str,
    result: str,
) -> None:
    if reservation_id is None:
        return
    from .attention import InputFacts
    from .dispatch_failures import (
        dispatch_failure_state_dir,
        issue_dispatch_disposition,
        record_attention,
    )

    state_dir = dispatch_failure_state_dir(home)
    if issue_dispatch_disposition(state_dir, issue_id) in {"stop", "success"}:
        return

    record_attention(
        state_dir,
        issue_id=issue_id,
        reservation_id=reservation_id,
        source=source,
        cause=cause,
        facts=InputFacts(validator=validator, result=result),
    )


def _record_preclaim_claim(
    home: Path,
    issue_id: int,
    reservation_id: str | None,
    *,
    source: str,
    cause: str,
    result: str,
) -> None:
    if reservation_id is None:
        return
    from .attention import ClaimFacts
    from .dispatch_failures import dispatch_failure_state_dir, record_attention

    record_attention(
        dispatch_failure_state_dir(home),
        issue_id=issue_id,
        reservation_id=reservation_id,
        source=source,
        cause=cause,
        facts=ClaimFacts(None, None, result),
    )


def _record_claim_refusal_outcome(
    home: Path,
    issue_id: int,
    reservation_id: str | None,
    *,
    reason: str,
) -> None:
    if reservation_id is None:
        return
    benign = {
        "duplicate_run_live",
        "lifecycle_state_incompatible",
        "review_ready_evidence_exists",
        "publication_intent_exists",
    }
    if reason in benign or reason.startswith("concurrency cap reached ("):
        return
    from .attention import AttentionCause, AttentionSource, ClaimFacts
    from .dispatch_failures import (
        dispatch_failure_state_dir,
        error_signature,
        record_attention,
        record_contention_recurrence,
        reservation_has_source,
    )

    state_dir = dispatch_failure_state_dir(home)
    if reservation_has_source(
        state_dir,
        issue_id=issue_id,
        reservation_id=reservation_id,
        sources=frozenset({
            "claim_command", "claim_guard", "claim_steal", "claim_budget",
            "claim_capacity_read", "claim_unready", "claim_inprogress",
            "claim_comment", "claim_contention",
        }),
    ):
        return
    if reason == "claim_contention_exhausted":
        disposition, retry_after = record_contention_recurrence(
            state_dir,
            issue_id=issue_id,
            signature=error_signature(reason),
        )
        source = AttentionSource.CLAIM_CONTENTION
        cause = AttentionCause.CONTENTION_EXHAUSTED
    elif reason == "attempts_exhausted":
        disposition, retry_after = "stop", None
        source = AttentionSource.CLAIM_BUDGET
        cause = AttentionCause.ATTEMPTS_EXHAUSTED
    elif reason.startswith("claim_guard_"):
        disposition, retry_after = "stop", None
        source = AttentionSource.CLAIM_GUARD
        cause = AttentionCause.OWNER_READ_FAILED
    else:
        disposition, retry_after = "stop", None
        source = AttentionSource.CLAIM_COMMAND
        cause = AttentionCause.CLAIM_COMMAND_FAILED
    record_attention(
        state_dir,
        issue_id=issue_id,
        reservation_id=reservation_id,
        source=source,
        cause=cause,
        facts=ClaimFacts(
            intended=None,
            confirmed=None,
            result=reason,
            command_operation="claim",
        ),
        disposition=disposition,
        retry_after=retry_after,
    )


@contextmanager
def _typed_outcome_boundary(
    *,
    home: Path,
    issue_id: int,
    reservation_id: str | None,
    source: str,
    cause: str,
    claim_record: ClaimRecord | None = None,
    checkout: Path | None = None,
    branch: str | None = None,
    operation: str | None = None,
    factory_record: FactoryRunRecord | None = None,
) -> Iterator[None]:
    """Record the exact originating operation when a real boundary raises."""
    try:
        yield
    except Exception as exc:
        if reservation_id is not None:
            from .attention import (
                ClaimIdentity,
                InputFacts,
                LaunchFacts,
                LeafFacts,
                FactorySnapshot,
                ReconcileFacts,
                SOURCE_RULES,
                AttentionSource,
            )
            from .dispatch_failures import (
                dispatch_failure_state_dir,
                issue_dispatch_disposition,
                record_attention,
                retained_positive_proofs,
            )

            state_dir = dispatch_failure_state_dir(home)
            if issue_dispatch_disposition(state_dir, issue_id) not in {"stop", "success"}:
                rule = SOURCE_RULES[AttentionSource(source)]
                if rule.fact_type == "input":
                    facts: object = InputFacts(
                        validator=operation or source,
                        result=type(exc).__name__,
                    )
                elif rule.fact_type == "launch":
                    facts = LaunchFacts(
                        executable=None,
                        compute=None,
                        checkout=str(checkout) if checkout else None,
                        operation=operation or source,
                        returned_handle=None,
                        pid=None,
                        start_ticks=None,
                        launch_result=type(exc).__name__,
                        state_save_result=None,
                    )
                elif rule.fact_type == "factory":
                    if factory_record is not None:
                        from .factory_state import immutable_factory_snapshot

                        facts = immutable_factory_snapshot(
                            factory_record, read_result=type(exc).__name__
                        )
                    else:
                        facts = FactorySnapshot(
                            run_id=None, issue_id=issue_id,
                            attempt=claim_record.attempt if claim_record else None,
                            sandbox=str(checkout) if checkout else None,
                            session=None, controller_phase="failed",
                            controller_error=None, status=None, valid=None,
                            lock=None, dead_lock=None, lock_session=None,
                            gates=(), steps=(), slices=(), pr_url=None,
                            next=None, next_present=False, park_snapshot=None,
                            read_result=type(exc).__name__,
                        )
                elif rule.fact_type == "reconcile":
                    facts = ReconcileFacts(
                        original_run_id=factory_record.run_id if factory_record else None,
                        original_claim=None,
                        process_verdict="unknown",
                        lock_verdict="unknown",
                        publication_id=None,
                        evidence_id=None,
                        automatic_handling_stage=operation or source,
                        automatic_handling_result=type(exc).__name__,
                    )
                else:
                    facts = LeafFacts(
                        backend=None,
                        checkout=str(checkout) if checkout else None,
                        base=None,
                        branch=branch,
                        isolated=None,
                        compute_result=type(exc).__name__,
                        backend_status=None,
                        validation_reason_codes=(),
                        evidence_id=None,
                        evidence_sha256=None,
                        pr_url=None,
                        head_sha=None,
                    )
                claim = (
                    ClaimIdentity(
                        issue_id=claim_record.issue_id,
                        attempt=claim_record.attempt,
                        agent_id=claim_record.agent_id,
                        claimed_at=claim_record.claimed_at.isoformat(),
                    )
                    if claim_record is not None
                    else None
                )
                proofs = (
                    retained_positive_proofs(
                        state_dir,
                        issue_id=issue_id,
                        reservation_id=reservation_id,
                    )
                    if rule.work_capable and claim is not None
                    else ()
                )
                record_attention(
                    state_dir,
                    issue_id=issue_id,
                    reservation_id=reservation_id,
                    source=source,
                    cause=cause,
                    facts=facts,
                    claim=claim,
                    proof_ids=proofs,
                )
        raise


def _record_direct_leaf_outcome(
    *,
    home: Path,
    issue_id: int,
    reservation_id: str | None,
    claim_record: ClaimRecord,
    source: str,
    cause: str,
    checkout: Path | None,
    branch: str | None,
    reason: str,
    evidence_path: Path | None = None,
) -> None:
    if reservation_id is None:
        return
    from .attention import (
        AttentionSource,
        ClaimIdentity,
        LeafFacts,
        ReconcileFacts,
        SOURCE_RULES,
    )
    from .dispatch_failures import dispatch_failure_state_dir, record_attention

    evidence_sha = _file_sha256(evidence_path)
    claim = ClaimIdentity(
        issue_id=claim_record.issue_id,
        attempt=claim_record.attempt,
        agent_id=claim_record.agent_id,
        claimed_at=claim_record.claimed_at.isoformat(),
    )
    source_value = AttentionSource(source)
    facts: object
    if SOURCE_RULES[source_value].fact_type == "reconcile":
        facts = ReconcileFacts(
            original_run_id=None,
            original_claim=claim,
            process_verdict="finished",
            lock_verdict=reason,
            publication_id=branch,
            evidence_id=str(evidence_path) if evidence_path else None,
            automatic_handling_stage=source,
            automatic_handling_result=reason,
        )
    else:
        facts = LeafFacts(
            backend=None,
            checkout=str(checkout) if checkout else None,
            base=None,
            branch=branch,
            isolated=False,
            compute_result=reason,
            backend_status=None,
            validation_reason_codes=(reason,),
            evidence_id=str(evidence_path.resolve()) if evidence_path else None,
            evidence_sha256=evidence_sha,
            pr_url=None,
            head_sha=None,
        )
    record_attention(
        dispatch_failure_state_dir(home),
        issue_id=issue_id,
        reservation_id=reservation_id,
        source=source,
        cause=cause,
        facts=facts,
        claim=claim,
        proof_ids=(
            ()
            if SOURCE_RULES[source_value].fact_type == "reconcile"
            else ((f"leaf_outcome:{evidence_sha}",) if evidence_sha else ())
        ),
    )


def _record_factory_stage_failure(
    *,
    home: Path,
    issue_id: int,
    reservation_id: str | None,
    claim_record: ClaimRecord | None,
    source: str,
    cause: str,
    record: FactoryRunRecord | None,
    checkout: Path | None,
    error: BaseException,
    allow_after_stop: bool = False,
) -> None:
    if reservation_id is None:
        return
    from .attention import (
        AttentionSource,
        ClaimIdentity,
        FactorySnapshot,
        LaunchFacts,
        ReconcileFacts,
        SOURCE_RULES,
    )
    from .dispatch_failures import (
        dispatch_failure_state_dir,
        issue_dispatch_disposition,
        record_attention,
        retained_positive_proofs,
    )
    from .factory_state import immutable_factory_snapshot

    state_dir = dispatch_failure_state_dir(home)
    if not allow_after_stop and issue_dispatch_disposition(
        state_dir, issue_id
    ) in {"stop", "success"}:
        return
    source_value = AttentionSource(source)
    if SOURCE_RULES[source_value].fact_type == "launch":
        facts: object = LaunchFacts(
            executable=None,
            compute="local_subprocess",
            checkout=str(checkout) if checkout else None,
            operation=source,
            returned_handle=None,
            pid=None,
            start_ticks=None,
            launch_result=type(error).__name__,
            state_save_result=None,
        )
    elif SOURCE_RULES[source_value].fact_type == "reconcile":
        facts = ReconcileFacts(
            original_run_id=record.run_id if record else None,
            original_claim=ClaimIdentity(
                issue_id=claim_record.issue_id,
                attempt=claim_record.attempt,
                agent_id=claim_record.agent_id,
                claimed_at=claim_record.claimed_at.isoformat(),
            ),
            process_verdict="unknown",
            lock_verdict="release_failed",
            publication_id=None,
            evidence_id=None,
            automatic_handling_stage=source,
            automatic_handling_result=type(error).__name__,
        )
    elif record is not None:
        facts = immutable_factory_snapshot(record, read_result=type(error).__name__)
    else:
        facts = FactorySnapshot(
            run_id=None, issue_id=issue_id,
            attempt=claim_record.attempt if claim_record else None,
            sandbox=str(checkout) if checkout else None, session=None,
            controller_phase="failed", controller_error=None, status=None,
            valid=None, lock=None, dead_lock=None, lock_session=None,
            gates=(), steps=(), slices=(), pr_url=None, next=None,
            next_present=False, park_snapshot=None,
            read_result=type(error).__name__,
        )
    proofs = retained_positive_proofs(
        state_dir, issue_id=issue_id, reservation_id=reservation_id
    )
    claim = (
        ClaimIdentity(
            issue_id=claim_record.issue_id,
            attempt=claim_record.attempt,
            agent_id=claim_record.agent_id,
            claimed_at=claim_record.claimed_at.isoformat(),
        )
        if claim_record is not None
        else None
    )
    record_attention(
        state_dir,
        issue_id=issue_id,
        reservation_id=reservation_id,
        source=source,
        cause=cause,
        facts=facts,
        claim=claim,
        proof_ids=proofs,
    )


def _record_factory_terminal_outcome(
    *,
    home: Path,
    reservation_id: str | None,
    claim_record: ClaimRecord,
    record: FactoryRunRecord | None,
    source: str,
    cause: str,
) -> None:
    if reservation_id is None:
        return
    if record is None:
        try:
            records = load_factory_records_for_issue(home, claim_record.issue_id)
            record = records[0] if records else None
        except Exception:
            record = None
    _record_factory_stage_failure(
        home=home,
        issue_id=claim_record.issue_id,
        reservation_id=reservation_id,
        claim_record=claim_record,
        source=source,
        cause=cause,
        record=record,
        checkout=Path(record.sandbox) if record is not None else None,
        error=WorklinkError(cause),
        allow_after_stop=source in {"factory_terminal_release", "factory_terminal_labels"},
    )


def _record_reconcile_outcome(
    *,
    home: Path,
    issue_id: int,
    reservation_id: str | None,
    source: str,
    cause: str,
    stage: str,
    result: str,
) -> None:
    if reservation_id is None:
        return
    from .attention import ReconcileFacts
    from .dispatch_failures import (
        dispatch_failure_state_dir,
        record_attention,
        reservation_binding,
        reservation_claim,
    )

    state_dir = dispatch_failure_state_dir(home)
    binding = reservation_binding(
        state_dir, issue_id=issue_id, reservation_id=reservation_id
    ) or {}
    claim = reservation_claim(
        state_dir, issue_id=issue_id, reservation_id=reservation_id
    )
    record_attention(
        state_dir,
        issue_id=issue_id,
        reservation_id=reservation_id,
        source=source,
        cause=cause,
        facts=ReconcileFacts(
            original_run_id=(
                str(binding["run_id"]) if binding.get("run_id") is not None else None
            ),
            original_claim=claim,
            process_verdict="unknown",
            lock_verdict="unknown",
            publication_id=None,
            evidence_id=None,
            automatic_handling_stage=stage,
            automatic_handling_result=result,
        ),
        claim=claim,
        deferred=False,
    )


def _record_direct_reattach_leaf(
    home: Path,
    issue_id: int,
    reservation_id: str | None,
    state: WorklinkRunState,
    *,
    source: str,
    cause: str,
    reason: str,
) -> None:
    if reservation_id is None:
        return
    from .attention import LeafFacts
    from .dispatch_failures import (
        dispatch_failure_state_dir,
        record_attention,
        reservation_claim,
    )

    state_dir = dispatch_failure_state_dir(home)
    record_attention(
        state_dir,
        issue_id=issue_id,
        reservation_id=reservation_id,
        source=source,
        cause=cause,
        facts=LeafFacts(
            backend=state.backend,
            checkout=state.checkout,
            base=state.base_ref,
            branch=state.branch,
            isolated=None,
            compute_result=reason,
            backend_status=None,
            validation_reason_codes=(),
            evidence_id=None,
            evidence_sha256=None,
            pr_url=None,
            head_sha=None,
        ),
        claim=reservation_claim(
            state_dir, issue_id=issue_id, reservation_id=reservation_id
        ),
    )


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
    reservation_id = _outcome_reservation(
        home, issue_id, target="leaf", autonomous=autonomous and not dry_run
    )
    try:
        result = asyncio.run(
            WorklinkRunner(
                home=home, repo=repo, outcome_reservation_id=reservation_id
            ).run(
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
            home=home,
            issue_id=issue_id,
            attempt=None,
            error=exc,
            exit_status=2 if isinstance(exc, LeafValidationError) else 1,
            autonomous=autonomous,
        )
        raise
    if result.status == "failed":
        _record_run_failure(
            home=home,
            issue_id=issue_id,
            attempt=result.attempt,
            error=result.reason or "Worklink run failed",
            exit_status=1,
            autonomous=autonomous,
            preserved_ref=result.preserved_ref,
            preservation_error=result.preservation_error,
        )
        _record_typed_terminal(
            home=home,
            issue_id=issue_id,
            reservation_id=reservation_id,
            target="leaf",
            result=result,
        )
    elif result.status == "blocked":
        _record_typed_terminal(
            home=home,
            issue_id=issue_id,
            reservation_id=reservation_id,
            target="leaf",
            result=result,
        )
    elif result.status == "completed":
        _record_run_success(home, issue_id, reservation_id=reservation_id, result=result, target="leaf")
    # Parked and refused outcomes do not resolve or replace prior failure attention.
    # Leaving it active preserves backoff without inflating its consecutive count.
    return result


def _record_run_failure(
    *,
    home: Path,
    issue_id: int,
    attempt: int | None,
    error: BaseException | str,
    exit_status: int,
    autonomous: bool,
    preserved_ref: str | None = None,
    preservation_error: str | None = None,
) -> None:
    from .dispatch_failures import dispatch_failure_state_dir, record_failure, terminal_error

    safe_error = terminal_error(error)
    _log_event(
        "worklink_run_failed",
        issue_id=issue_id,
        attempt=attempt,
        attempt_consumed=attempt is not None,
        exit_status=exit_status,
        terminal_error=safe_error,
        preserved_ref=preserved_ref,
        preservation_error=preservation_error,
    )
    if autonomous:
        try:
            record_failure(
                dispatch_failure_state_dir(home),
                issue_id=issue_id,
                attempt=attempt,
                exit_status=exit_status,
                error=error,
                log_path=os.environ.get("WORKLINK_RUN_LOG"),
                preserved_ref=preserved_ref,
                preservation_error=preservation_error,
            )
        except OSError:
            pass


def _record_typed_terminal(
    *,
    home: Path,
    issue_id: int,
    reservation_id: str | None,
    target: str,
    result: WorklinkRunResult,
) -> None:
    if reservation_id is None:
        return
    from .attention import (
        AttentionCause,
        AttentionSource,
        FactorySnapshot,
        LeafFacts,
        positive_factory_proofs,
    )
    from .dispatch_failures import (
        dispatch_failure_state_dir,
        issue_dispatch_disposition,
        record_attention,
        retained_positive_proofs,
        reservation_binding,
        reservation_claim,
    )

    state_dir = dispatch_failure_state_dir(home)
    if issue_dispatch_disposition(state_dir, issue_id) in {"stop", "success"}:
        return
    claim = reservation_claim(state_dir, issue_id=issue_id, reservation_id=reservation_id)
    if target == "factory":
        is_partial = result.status == "partial"
        source = (
            AttentionSource.FACTORY_PARTIAL
            if is_partial
            else AttentionSource.FACTORY_DRIVER_EXIT
            if result.status == "failed"
            else AttentionSource.FACTORY_PARKED
            if result.status in {"parked", "needs-human"}
            else AttentionSource.FACTORY_BLOCKED
        )
        cause = (
            AttentionCause.PARTIAL
            if is_partial
            else AttentionCause.UNFINISHED_EXIT
            if result.status == "failed"
            else AttentionCause.NEEDS_HUMAN
            if result.status in {"parked", "needs-human"}
            else AttentionCause.BLOCKED
        )
        facts: object = FactorySnapshot(
            run_id=None,
            issue_id=issue_id,
            attempt=result.attempt,
            sandbox=str(result.checkout) if result.checkout else None,
            session=None,
            controller_phase="terminal",
            controller_error=result.reason,
            status=result.status,
            valid=None,
            lock=None,
            dead_lock=None,
            lock_session=None,
            gates=(),
            steps=(),
            slices=(),
            pr_url=result.pr_url,
            next=result.next,
            next_present=result.next_present,
            park_snapshot=None,
            read_result="unavailable",
        )
        try:
            from .factory_state import (
                immutable_factory_snapshot,
                load_factory_records_for_issue,
            )

            binding = reservation_binding(
                state_dir, issue_id=issue_id, reservation_id=reservation_id
            ) or {}
            records = [
                record
                for record in load_factory_records_for_issue(home, issue_id)
                if (binding.get("run_id") is None or record.run_id == binding["run_id"])
                and (
                    binding.get("sandbox") is None
                    or record.sandbox == binding["sandbox"]
                )
            ]
            if len(records) == 1:
                facts = immutable_factory_snapshot(records[0], read_result="captured")
        except Exception:
            # The terminal occurrence must preserve the originating result; a
            # later strict inspection reports the record read failure.
            pass
        proofs = positive_factory_proofs(facts)
        proofs = tuple(
            dict.fromkeys([
                *retained_positive_proofs(
                    state_dir,
                    issue_id=issue_id,
                    reservation_id=reservation_id,
                ),
                *proofs,
            ])
        )
    else:
        source = AttentionSource.LEAF_BACKEND_OUTCOME
        cause = (
            AttentionCause.BACKEND_BLOCKED
            if result.status == "blocked"
            else AttentionCause.BACKEND_FAILED
        )
        evidence_sha = _file_sha256(result.evidence_path)
        proofs = ((f"leaf_outcome:{evidence_sha}",) if evidence_sha is not None else ())
        facts = LeafFacts(
            backend=None,
            checkout=str(result.checkout) if result.checkout else None,
            base=None,
            branch=result.branch,
            isolated=result.checkout is not None,
            compute_result=result.status,
            backend_status=result.status,
            validation_reason_codes=(),
            evidence_id=str(result.evidence_path) if result.evidence_path else None,
            evidence_sha256=evidence_sha,
            pr_url=result.pr_url,
            head_sha=None,
        )
    record_attention(
        state_dir,
        issue_id=issue_id,
        reservation_id=reservation_id,
        source=source,
        cause=cause,
        facts=facts,
        claim=claim,
        proof_ids=proofs,
    )


def _retain_factory_observation(
    home: Path,
    record: FactoryRunRecord,
    reservation_id: str | None,
) -> None:
    if reservation_id is None:
        return
    from .attention import positive_factory_proofs
    from .dispatch_failures import (
        dispatch_failure_state_dir,
        retain_positive_proofs,
    )
    from .factory_state import immutable_factory_snapshot

    snapshot = immutable_factory_snapshot(record, read_result="captured")
    proofs = positive_factory_proofs(snapshot)
    if proofs:
        retain_positive_proofs(
            dispatch_failure_state_dir(home),
            issue_id=record.issue_id,
            reservation_id=reservation_id,
            proof_ids=proofs,
        )


def _record_leaf_outcome_before_routing(
    *,
    home: Path,
    issue: IssueContext,
    reservation_id: str,
    claim_record: ClaimRecord,
    status: str,
    reason: str | None,
    evidence_path: Path,
    checkout: Path,
    branch: str,
    pr_url: str | None,
) -> None:
    from .attention import AttentionCause, AttentionSource, ClaimIdentity, LeafFacts
    from .dispatch_failures import (
        dispatch_failure_state_dir,
        issue_dispatch_disposition,
        record_attention,
    )

    state_dir = dispatch_failure_state_dir(home)
    if issue_dispatch_disposition(state_dir, issue.issue_id) in {"stop", "success"}:
        return
    evidence_sha = _file_sha256(evidence_path)
    proof_ids = (
        (f"leaf_outcome:{evidence_sha}",) if evidence_sha is not None else ()
    )
    record_attention(
        state_dir,
        issue_id=issue.issue_id,
        reservation_id=reservation_id,
        source=AttentionSource.LEAF_BACKEND_OUTCOME,
        cause=(
            AttentionCause.BACKEND_BLOCKED
            if status == "blocked"
            else AttentionCause.BACKEND_FAILED
        ),
        facts=LeafFacts(
            backend=None,
            checkout=str(checkout),
            base=None,
            branch=branch,
            isolated=True,
            compute_result=status,
            backend_status=status,
            validation_reason_codes=(reason,) if reason else (),
            evidence_id=str(evidence_path),
            evidence_sha256=evidence_sha,
            pr_url=pr_url,
            head_sha=None,
        ),
        claim=ClaimIdentity(
            issue_id=claim_record.issue_id,
            attempt=claim_record.attempt,
            agent_id=claim_record.agent_id,
            claimed_at=claim_record.claimed_at.isoformat(),
        ),
        proof_ids=proof_ids,
    )


def _record_run_success(
    home: Path,
    issue_id: int,
    *,
    reservation_id: str | None = None,
    result: WorklinkRunResult | None = None,
    target: str = "leaf",
) -> None:
    from .dispatch_failures import (
        clear_contention_after_verified_success,
        dispatch_failure_state_dir,
        record_success,
        record_success_witness,
        reservation_binding,
        reservation_claim,
    )

    try:
        state_dir = dispatch_failure_state_dir(home)
        record_success(state_dir, issue_id)
        if result is None or result.evidence_path is None:
            return
        evidence_sha = _file_sha256(result.evidence_path)
        if evidence_sha is None or result.branch is None:
            return
        head_sha = evidence_sha
        try:
            payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("head_sha"), str):
                head_sha = payload["head_sha"]
        except (OSError, json.JSONDecodeError):
            return
        if reservation_id is None:
            record_success_witness(
                state_dir,
                issue_id=issue_id,
                target=target,
                origin="manual",
                claim=None,
                run_id=(
                    str(payload["run_id"])
                    if isinstance(payload, dict) and payload.get("run_id") is not None
                    else None
                ),
                sandbox=(
                    str(payload["sandbox"])
                    if isinstance(payload, dict) and payload.get("sandbox") is not None
                    else None
                ),
                completed_at=datetime.now(UTC).isoformat(),
                evidence_path=str(result.evidence_path.resolve()),
                evidence_sha256=evidence_sha,
                branch=result.branch,
                head_sha=head_sha,
                pr_url=result.pr_url,
                next_value=result.next,
                next_present=result.next_present,
            )
            clear_contention_after_verified_success(state_dir, issue_id)
            return
        claim = reservation_claim(
            state_dir, issue_id=issue_id, reservation_id=reservation_id
        )
        if claim is None:
            return
        binding = reservation_binding(
            state_dir, issue_id=issue_id, reservation_id=reservation_id
        ) or {}
        record_success_witness(
            state_dir,
            issue_id=issue_id,
            target=target,
            origin="autonomous",
            claim=claim,
            run_id=(str(binding["run_id"]) if binding.get("run_id") is not None else None),
            sandbox=(
                str(binding["sandbox"])
                if binding.get("sandbox") is not None
                else str(result.checkout)
                if target == "factory" and result.checkout
                else None
            ),
            completed_at=datetime.now(UTC).isoformat(),
            evidence_path=str(result.evidence_path.resolve()),
            evidence_sha256=evidence_sha,
            branch=result.branch,
            head_sha=head_sha,
            pr_url=result.pr_url,
            next_value=result.next,
            next_present=result.next_present,
            reservation_id=reservation_id,
            proof_ids=(f"{target}_completion:{evidence_sha}",),
        )
        clear_contention_after_verified_success(state_dir, issue_id)
    except OSError:
        pass


def _file_sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _trigger_ready_scan_after_release(home: Path) -> None:
    """Signal the ready queue only after Chainlink confirmed a slot release."""
    # The poller performs the issue-specific terminal-disposition check before
    # dispatch.  This notification is only a scan wakeup and grants no retry.
    from ..poller_triggers import notify_poller

    notify_poller(home, "worklink-ready-queue", reason="worklink_slot_released")


def run_worklink_reattach(
    *,
    home: Path,
    repo: Path,
    issue_id: int,
    autonomous: bool = False,
    reservation_id: str | None = None,
) -> WorklinkRunResult:
    """Resume one in-flight run after a controller restart (#561)."""
    if autonomous and reservation_id is None:
        # Legacy recovery remains functional but cannot fabricate an autonomous
        # typed occurrence for a reservation that never existed.
        reservation_id = None
    result = asyncio.run(
        WorklinkRunner(
            home=home,
            repo=repo,
            outcome_reservation_id=reservation_id,
        ).reattach(issue_id)
    )
    if reservation_id is not None:
        if result.status == "completed":
            _record_run_success(
                home,
                issue_id,
                reservation_id=reservation_id,
                result=result,
                target="leaf",
            )
        elif result.status in {"failed", "blocked"}:
            _record_typed_terminal(
                home=home,
                issue_id=issue_id,
                reservation_id=reservation_id,
                target="leaf",
                result=result,
            )
    return result


def run_worklink_epic(
    *,
    home: Path,
    repo: Path,
    issue_id: int,
    autonomous: bool = False,
) -> WorklinkRunResult:
    reservation_id = _outcome_reservation(
        home, issue_id, target="factory", autonomous=autonomous
    )
    try:
        result = asyncio.run(
            WorklinkRunner(
                home=home, repo=repo, outcome_reservation_id=reservation_id
            ).run_epic(
                issue_id,
                autonomous=autonomous,
            )
        )
    except Exception as exc:
        _record_run_failure(
            home=home,
            issue_id=issue_id,
            attempt=None,
            error=exc,
            exit_status=1,
            autonomous=autonomous,
        )
        raise
    if result.status == "failed":
        _record_run_failure(
            home=home,
            issue_id=issue_id,
            attempt=result.attempt,
            error=result.reason or "Worklink epic run failed",
            exit_status=1,
            autonomous=autonomous,
            preserved_ref=result.preserved_ref,
            preservation_error=result.preservation_error,
        )
        _record_typed_terminal(
            home=home,
            issue_id=issue_id,
            reservation_id=reservation_id,
            target="factory",
            result=result,
        )
    elif result.status in {"blocked", "parked", "needs-human", "partial"}:
        _record_typed_terminal(
            home=home,
            issue_id=issue_id,
            reservation_id=reservation_id,
            target="factory",
            result=result,
        )
    elif result.status in {"completed", "review_ready"}:
        _record_run_success(
            home,
            issue_id,
            reservation_id=reservation_id,
            result=result,
            target="factory",
        )
    return result


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
    test_env: Mapping[str, str] | None = None,
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
            test_env=dict(test_env or {}),
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


def _prepare_factory_sandbox_permissions(path: Path, *, worker_uid_drop: bool) -> None:
    """Share local factory sandboxes with the mandatory contained worker.

    Unlike leaf builds, local factory workloads always drop uid, even when
    coding_enabled() is false. Missing deployment identities or group-change
    permission must fail before launch, never select an agent-user fallback.
    Non-local compute owns its permission contract; leave its sandbox untouched.
    """
    if not worker_uid_drop:
        return
    try:
        os.chown(path, -1, get_identities().worklink_gid)
        os.chmod(path, 0o2770)
    except Exception as exc:
        raise WorklinkError(
            "cannot share the factory sandbox directory with the worklink group; "
            "local factory execution requires configured worker identities and "
            "group-change permission; no agent-user fallback "
            f"({type(exc).__name__}: {exc})"
        ) from exc


def _make_executor_report_dir(issue: int, attempt: int, *, worker_uid_drop: bool) -> Path:
    """Create the executor's pytest report directory.

    The path this returns is injected into the executor's ``PYTEST_ADDOPTS`` as
    ``--junitxml`` and ``cache_dir``. ``mkdtemp`` creates 0700 owned by the
    CONTROLLER, so when the executor is dropped to the WORKER uid every pytest it
    runs raises ``PermissionError`` from ``pytest_sessionfinish`` -- AFTER the suite
    has already passed:

        PermissionError: [Errno 13] Permission denied:
            '/tmp/worklink-1475-3-executor-4m8dxhzg/junit.xml'

    On the worker path, sharing the directory with the worklink group is a
    PRECONDITION, not a best effort: if it cannot be arranged the launch must fail
    here, loudly and before the backend starts, rather than proceed to the same
    post-suite PermissionError with worse diagnostics. The half-made directory is
    removed so a failed launch leaves nothing behind.

    On the controller path the directory stays 0700 and private. There is no worker
    uid to accommodate, so no group is granted.
    """
    path = Path(tempfile.mkdtemp(prefix=f"worklink-{issue}-{attempt}-executor-"))
    if not worker_uid_drop:
        return path
    try:
        # Permitted for the owner because ``mimir`` is a member of ``worklink``.
        os.chown(path, -1, get_identities().worklink_gid)
        os.chmod(path, 0o770)
    except Exception as exc:
        rmtree_missing_ok(path)
        raise WorklinkError(
            "cannot share the executor report directory with the worklink group; "
            "the worker would fail writing junit.xml after the suite runs "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    return path


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


@dataclass
class _PublicationIntent:
    pr_started: bool = False
    completed: bool = False


@contextmanager
def _leaf_publication(home: Path, issue_id: int, attempt: int) -> Iterator[_PublicationIntent]:
    """Fence even already-admitted replacements, independently of claim liveness.

    Exclusive creation is cross-process coordination; the file is deliberately
    NOT removed on process death or an ambiguous PR result. Operators must
    reconcile such intents before retrying. All controllers share Worklink home.
    """
    directory = home / "state" / "worklink" / "publications"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{issue_id}.json"
    intent = _PublicationIntent()
    # Never overwrite an existing intent, including an empty/corrupt one.
    with path.open("x", encoding="utf-8") as handle:
        json.dump({"issue": issue_id, "attempt": attempt}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    try:
        # Admission's latest-only check is insufficient for an already-admitted
        # replacement whose newer, pre-publication evidence masks the original.
        evidence_dir = home / "state" / "worklink" / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for evidence_path in evidence_dir.iterdir():
            if not re.fullmatch(rf"{issue_id}-\d+\.json", evidence_path.name):
                continue
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("status") not in {
                "completed", "failed", "blocked",
            }:
                raise WorklinkError(f"publication evidence unavailable: {evidence_path}")
            if payload.get("pr_url"):
                raise WorklinkError(f"publication already recorded: {evidence_path}")
        yield intent
    finally:
        # A failed push cannot have created a PR. Once PR creation starts, only
        # durable completed evidence can take over the barrier. Exceptions and
        # cancellation otherwise retain the intent, including gh nonzero exits.
        if not intent.pr_started or intent.completed:
            path.unlink()


def _write_evidence(home: Path, evidence: WorklinkEvidence) -> Path:
    path = home / "state" / "worklink" / "evidence" / f"{evidence.issue}-{evidence.attempt}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, _evidence_json(evidence))
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


def _log_gate_flaky_tests(evidence: WorklinkEvidence) -> None:
    if evidence.tests is not None and evidence.tests.flaky_tests:
        _log_event(
            "worklink_gate_flaky_tests",
            issue_id=evidence.issue,
            attempt=evidence.attempt,
            flaky_tests=[redact_text(node) for node in evidence.tests.flaky_tests],
        )


def _comment_evidence(
    claims: ChainlinkClaims,
    evidence: WorklinkEvidence,
    validation: EvidenceValidation,
    evidence_path: Path,
    *,
    gate_test_tail: str | None = None,
) -> None:
    tests = evidence.tests
    summary = (
        f"WORKLINK_EVIDENCE issue={evidence.issue} attempt={evidence.attempt} "
        f"status={validation.status} review_ready={str(validation.review_ready).lower()} "
        f"files={len(evidence.files_changed)} evidence={evidence_path}"
        f" failed_tests={json.dumps([redact_text(node) for node in tests.failed_tests] if tests else [])}"
        f" flaky_tests={json.dumps([redact_text(node) for node in tests.flaky_tests] if tests else [])}"
        f" test_env={json.dumps(evidence.test_env, sort_keys=True)}"
    )
    reasons = f"\nReasons: {', '.join(validation.reasons)}" if validation.reasons else ""
    if tests is not None and tests.skipped_reason is not None:
        summary += f" skipped_reason={json.dumps(redact_text(tests.skipped_reason)[:1000])}"
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
        checkout_branch=base if isinstance(backend, FeatureFactoryBackend) else None,
        base_fetch=base_fetch,
        event_logger=event_logger,
        runner=runner,
        worker_eligible=worker_eligible,
        factory_worker=worker_eligible and isinstance(backend, FeatureFactoryBackend),
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
    # attempt checkout. A legitimate attempt diff must not mask escaped writes;
    # ``completed_empty_diff`` only distinguishes the existing validation reason.
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
    if evidence.tests is not None:
        tests = evidence.tests
        if tests.initial_run is not None:
            evidence_block += (
                f"- Original gate: `{tests.initial_run.cmd}` → {tests.initial_run.exit_code}\n"
            )
        if tests.rerun is not None:
            evidence_block += (
                f"- Diagnostic rerun (serial, failed nodes only): `{tests.rerun.cmd}` → "
                f"{tests.rerun.exit_code}\n"
            )
        if tests.flaky_tests:
            evidence_block += (
                "- flaky_tests (passed in isolation; not proof of flakiness): "
                f"{json.dumps([redact_text(node) for node in tests.flaky_tests])}\n"
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
    text = _render_chainlink_text(text)
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
        publication.run("rev-parse", "--verify", "HEAD^{commit}")
        if publication is not None
        else runner(["git", "-C", str(checkout), "rev-parse", "--verify", "HEAD^{commit}"])
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


def _publication_failed_validation(
    validation: EvidenceValidation,
    *,
    step: str,
    error: Exception,
    issue_id: int,
    attempt: int,
) -> EvidenceValidation:
    detail = str(error).strip() or error.__class__.__name__
    reason = f"publication {step} failed: {detail}"
    _log_event(
        "worklink_publication_failed",
        issue_id=issue_id,
        attempt=attempt,
        step=step,
        error=detail,
    )
    evidence = replace(
        validation.evidence,
        status="blocked",
        blocked_reason=reason,
        head_sha=None,
    )
    return replace(
        validation,
        status="blocked",
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
        timeout: float = 1800,
    ) -> subprocess.CompletedProcess:
        if isinstance(args, str):
            from .evidence import _run as run_gate

            return run_gate(args, cwd=cwd, text=text, timeout=timeout)
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
    timeout: float = 1800,
) -> subprocess.CompletedProcess:
    if isinstance(args, str):
        from .evidence import _run as run_gate

        return run_gate(args, cwd=cwd, text=text, timeout=timeout)
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=text, check=False)


def _log_event(event_type: str, **payload: Any) -> None:
    try:
        from ..event_logger import log_event_sync

        log_event_sync(event_type, **payload)
    except RuntimeError:
        pass


def _log_durable_event(event_type: str, **payload: Any) -> None:
    """Persist a load-bearing Worklink state transition before continuing."""
    from ..event_logger import log_durable_event_sync

    log_durable_event_sync(event_type, **payload)
