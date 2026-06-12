"""Worklink operator-run orchestrator.

The orchestrator owns deterministic state transitions around an untrusted tool
backend: validate the Chainlink leaf, claim it, create an attempt worktree,
render the work order, run the backend, observe evidence ourselves, push/open a
PR only after the evidence gate passes, then clean up and release the lock.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import warnings
from typing import Any, Callable, Mapping, Sequence

from .backends import BackendRegistry, RawResult, ToolBackend, WorkOrder, WorklinkConfig
from .claims import ChainlinkClaims
from .evidence import EvidenceValidation, WorklinkEvidence, observe_evidence
from .planning import (
    missing_leaf_template_parts,
    render_decompose_prompt,
    suggested_test_command,
    uses_strict_leaf_validation,
)
from .worktree import WorktreeLease, cleanup_worktree, create_worktree

Runner = Callable[..., subprocess.CompletedProcess[str]]


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
    ) -> WorklinkRunResult:
        runner = self.runner or _runner_for_home(self.home, self.chainlink_bin)
        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)
        validate_leaf(issue)
        config = WorklinkConfig.load(self.home / "worklink.yaml")
        registry = self.registry or BackendRegistry(config)
        backend = (
            registry.get(backend_name)
            if backend_name
            else registry.select(labels=issue.labels, repo=_repo_slug(self.repo))
        )
        selected_name = backend.name
        test_cmd = (
            test_command
            if test_command is not None
            else suggested_test_command(issue.description) or config.defaults.test_command
        )
        template_path = _template_path(self.home)

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
            return WorklinkRunResult(issue.issue_id, None, "dry_run", dry_run=True)

        claims = ChainlinkClaims(
            chainlink_bin=self.chainlink_bin,
            agent_id=self.agent_id,
            runner=_list_runner(runner),
        )
        # Re-read immediately before claiming so retries in a long-lived caller do
        # not use stale comments and collide with prior attempt-scoped branches.
        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)
        claim = claims.claim_issue(issue.issue_id, issue.comments)
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
            lease = create_worktree(
                self.repo,
                issue_id=issue.issue_id,
                attempt=record.attempt,
                runner=_list_runner(runner),
            )
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
            raw = await backend.run(order)
            validation = observe_evidence(
                issue=issue.issue_id,
                attempt=record.attempt,
                backend=selected_name,
                branch=lease.branch,
                worktree=lease.path,
                started_at=started,
                base_ref=lease.base_ref,
                backend_status=raw.backend_status,
                test_command=test_cmd,
                transcript=str(raw.transcript_path) if raw.transcript_path else None,
                runner=runner,
            )
            evidence_path = _write_evidence(self.home, validation.evidence)
            pr_url = None
            if validation.review_ready:
                _commit_worktree_changes(lease.path, issue, runner=runner)
                try:
                    _ensure_clean_worktree(lease.path, runner=runner)
                except WorklinkError as exc:
                    validation = _failed_validation(validation, str(exc))
                else:
                    validation = observe_evidence(
                        issue=issue.issue_id,
                        attempt=record.attempt,
                        backend=selected_name,
                        branch=lease.branch,
                        worktree=lease.path,
                        started_at=started,
                        base_ref=lease.base_ref,
                        backend_status=raw.backend_status,
                        test_command=test_cmd,
                        transcript=str(raw.transcript_path) if raw.transcript_path else None,
                        runner=runner,
                    )
                evidence_path = _write_evidence(self.home, validation.evidence)
            if validation.review_ready:
                _git_push(self.repo, lease.branch, runner=runner)
                pr_url = _open_pr(
                    self.repo, issue, lease.branch, validation.evidence, runner=runner
                )
                validation = _with_pr_url(validation, pr_url)
                evidence_path = _write_evidence(self.home, validation.evidence)
            _comment_evidence(claims, validation.evidence, validation, evidence_path)
            _log_event(
                "worklink_evidence",
                issue_id=issue.issue_id,
                attempt=record.attempt,
                status=validation.status,
                review_ready=validation.review_ready,
                reasons=list(validation.reasons),
            )
            claims.transition_issue(
                issue.issue_id,
                status=validation.status,
                review_ready=validation.review_ready,
                attempt=record.attempt,
                reason=", ".join(validation.reasons) if validation.reasons else None,
            )
            _log_event(
                "worklink_transition",
                issue_id=issue.issue_id,
                attempt=record.attempt,
                status=validation.status,
                review_ready=validation.review_ready,
                pr_url=pr_url,
            )
            cleanup_worktree(lease, outcome=validation.status, runner=_list_runner(runner))
            return WorklinkRunResult(
                issue.issue_id,
                record.attempt,
                validation.status,
                review_ready=validation.review_ready,
                pr_url=pr_url,
                evidence_path=evidence_path,
                worktree=lease.path,
                branch=lease.branch,
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


def run_worklink(
    *,
    home: Path,
    repo: Path,
    issue_id: int,
    backend: str | None = None,
    dry_run: bool = False,
    test_command: str | None = None,
) -> WorklinkRunResult:
    return asyncio.run(
        WorklinkRunner(home=home, repo=repo).run(
            issue_id,
            backend_name=backend,
            dry_run=dry_run,
            test_command=test_command,
        )
    )


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
) -> None:
    summary = (
        f"WORKLINK_EVIDENCE issue={evidence.issue} attempt={evidence.attempt} "
        f"status={validation.status} review_ready={str(validation.review_ready).lower()} "
        f"files={len(evidence.files_changed)} evidence={evidence_path}"
    )
    reasons = f"\nReasons: {', '.join(validation.reasons)}" if validation.reasons else ""
    claims._run(  # noqa: SLF001 - Chainlink wrapper owns quoting/checks.
        "issue", "comment", str(evidence.issue), summary + reasons
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
    repo: Path, issue: IssueContext, branch: str, evidence: WorklinkEvidence, *, runner: Runner
) -> str:
    body = (
        f"Closes chainlink #{issue.issue_id}.\n\n"
        f"Worklink evidence:\n"
        f"- Branch: `{branch}`\n"
        f"- Files changed: {len(evidence.files_changed)}\n"
        "- Tests: "
        f"`{evidence.tests.cmd if evidence.tests else '(none)'}` → "
        f"{evidence.tests.exit_code if evidence.tests else 'missing'}\n"
        f"- Transcript: `{evidence.transcript or '(none)'}`\n"
    )
    command = ["gh", "pr", "create", "--head", branch]
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


def _repo_slug(repo: Path, *, runner: Runner | None = None) -> str | None:
    run = runner or _run
    result = run(["git", "-C", str(repo), "config", "--get", "remote.origin.url"])
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    if url.startswith("git@github.com:"):
        return url.removeprefix("git@github.com:").removesuffix(".git")
    if "github.com/" in url:
        return url.rsplit("github.com/", 1)[1].removesuffix(".git")
    return None


def _with_pr_url(validation: EvidenceValidation, pr_url: str) -> EvidenceValidation:
    from dataclasses import replace

    evidence = replace(validation.evidence, pr_url=pr_url)
    return replace(validation, evidence=evidence)


def _failed_validation(validation: EvidenceValidation, reason: str) -> EvidenceValidation:
    from dataclasses import replace

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
