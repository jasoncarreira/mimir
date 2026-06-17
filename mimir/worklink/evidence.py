"""Worklink evidence schema, observation, and validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
import subprocess
from typing import Callable, Sequence


@dataclass(frozen=True)
class CommandResult:
    cmd: str
    exit_code: int
    summary: str | None = None
    observed: bool = True


@dataclass(frozen=True)
class TestResult:
    __test__ = False

    cmd: str | None
    exit_code: int | None = None
    summary: str | None = None
    skipped_reason: str | None = None
    observed: bool = True


@dataclass(frozen=True)
class WorklinkEvidence:
    issue: int
    attempt: int
    backend: str
    branch: str
    worktree: str
    started_at: str
    finished_at: str
    files_changed: list[str]
    diff_stat: str
    commands: list[CommandResult]
    tests: TestResult | None
    pr_url: str | None
    status: str
    blocked_reason: str | None = None
    transcript: str | None = None
    diff_observed: bool = True


@dataclass(frozen=True)
class EvidenceValidation:
    status: str
    review_ready: bool
    reasons: tuple[str, ...]
    evidence: WorklinkEvidence


Run = Callable[..., subprocess.CompletedProcess[str]]


def validate_evidence(evidence: WorklinkEvidence) -> EvidenceValidation:
    """Validate and normalize backend-independent evidence.

    The review gate is intentionally based on observed diff/test data. A backend
    transcript saying "tests passed" is not enough: callers must provide a
    ``TestResult`` produced by the executor's own command run.
    """
    reasons: list[str] = []
    status = evidence.status

    if status not in {"completed", "blocked", "failed"}:
        reasons.append("invalid_status")
        status = "failed"

    if status == "blocked" and not evidence.blocked_reason:
        reasons.append("blocked_missing_reason")
        status = "failed"

    if status == "blocked":
        return EvidenceValidation(status="blocked", review_ready=False, reasons=tuple(reasons), evidence=evidence)

    if status == "completed" and not evidence.files_changed:
        reasons.append("completed_empty_diff")
        status = "failed"

    if not evidence.diff_observed:
        reasons.append("diff_not_observed")
        status = "failed"

    tests_ok = False
    if evidence.tests is None:
        if status == "completed":
            reasons.append("tests_missing")
            status = "failed"
    elif not evidence.tests.observed:
        reasons.append("tests_not_observed")
        status = "failed"
    elif evidence.tests.skipped_reason:
        tests_ok = True
    elif evidence.tests.exit_code == 0:
        tests_ok = True
    else:
        reasons.append("tests_failed")
        if status == "completed":
            status = "failed"

    review_ready = status == "completed" and bool(evidence.files_changed) and tests_ok and evidence.diff_observed
    if status != evidence.status:
        evidence = replace(evidence, status=status)
    return EvidenceValidation(status=status, review_ready=review_ready, reasons=tuple(reasons), evidence=evidence)


def observe_evidence(
    *,
    issue: int,
    attempt: int,
    backend: str,
    branch: str,
    worktree: Path,
    started_at: datetime,
    base_ref: str,
    backend_status: str,
    test_command: str | None,
    transcript: str | None = None,
    pr_url: str | None = None,
    blocked_reason: str | None = None,
    runner: Run | None = None,
) -> EvidenceValidation:
    """Build evidence by observing a worktree after a backend run."""
    return _observe_evidence_from_ref(
        issue=issue,
        attempt=attempt,
        backend=backend,
        branch=branch,
        worktree=worktree,
        started_at=started_at,
        base_ref=base_ref,
        head_ref="HEAD",
        backend_status=backend_status,
        test_command=test_command,
        transcript=transcript,
        pr_url=pr_url,
        blocked_reason=blocked_reason,
        runner=runner,
        include_worktree_status=True,
    )


def observe_remote_evidence(
    *,
    issue: int,
    attempt: int,
    backend: str,
    branch: str,
    worktree: Path,
    started_at: datetime,
    base_ref: str,
    backend_status: str,
    test_command: str | None,
    transcript: str | None = None,
    pr_url: str | None = None,
    blocked_reason: str | None = None,
    runner: Run | None = None,
) -> EvidenceValidation:
    """Build evidence from fetched refs after a remote compute run.

    Remote workers are untrusted for transition gating.  The orchestrator
    fetches both refs into a fresh/local observation worktree and derives diff
    and test evidence from ``origin/<base_ref>..origin/<branch>`` instead of
    trusting worker-reported evidence JSON or a shared filesystem worktree.
    """
    runner = runner or _run
    fetch_base = runner([
        "git",
        "-C",
        str(worktree),
        "fetch",
        "origin",
        f"+{base_ref}:refs/remotes/origin/{base_ref}",
    ])
    fetch_branch = runner([
        "git",
        "-C",
        str(worktree),
        "fetch",
        "origin",
        f"+{branch}:refs/remotes/origin/{branch}",
    ])
    head_ref = f"origin/{branch}"
    validation = _observe_evidence_from_ref(
        issue=issue,
        attempt=attempt,
        backend=backend,
        branch=branch,
        worktree=worktree,
        started_at=started_at,
        base_ref=f"origin/{base_ref}",
        head_ref=head_ref,
        backend_status=backend_status,
        test_command=None,
        transcript=transcript,
        pr_url=pr_url,
        blocked_reason=blocked_reason,
        runner=runner,
        include_worktree_status=False,
        checkout_ref=None,
        pre_commands=[
            CommandResult(
                f"git fetch origin {base_ref}", fetch_base.returncode, _summarize(fetch_base)
            ),
            CommandResult(
                f"git fetch origin {branch}", fetch_branch.returncode, _summarize(fetch_branch)
            ),
        ],
        pre_observed=fetch_base.returncode == 0 and fetch_branch.returncode == 0,
    )
    if test_command:
        evidence = replace(
            validation.evidence,
            tests=TestResult(
                test_command,
                None,
                "remote test re-run requires sandboxed compute",
                observed=False,
            ),
        )
        return validate_evidence(evidence)
    return validation


def fold_remote_test_evidence(
    validation: EvidenceValidation, test_command: str, exit_code: int
) -> EvidenceValidation:
    """Replace the stubbed remote test result with an OBSERVED one (chainlink
    #538), then re-validate.

    ``exit_code`` is the exit of a fresh sandboxed test job the controller ran on
    the pushed branch (``worker --test-only``): 0 = tests passed. This is how a
    docker-sibling/ecs run stops fail-closing on ``tests_not_observed`` — the
    controller orchestrated the test execution and reads its exit code, the same
    trust basis as the local path running tests itself."""
    evidence = replace(
        validation.evidence,
        # Reset to the completed baseline before re-validating: the first pass
        # downgraded status to "failed" solely on tests_not_observed and persisted
        # it, and validate_evidence only ever downgrades. The caller folds only
        # when there's a real observed diff (which exists only if the worker
        # completed + pushed), so "completed" is the correct base; validate_evidence
        # re-downgrades to "failed" if the folded test result is non-zero.
        status="completed",
        tests=TestResult(
            test_command,
            exit_code,
            f"remote sandboxed test job: exit {exit_code}",
            observed=True,
        ),
    )
    return validate_evidence(evidence)


def _observe_evidence_from_ref(
    *,
    issue: int,
    attempt: int,
    backend: str,
    branch: str,
    worktree: Path,
    started_at: datetime,
    base_ref: str,
    head_ref: str,
    backend_status: str,
    test_command: str | None,
    transcript: str | None,
    pr_url: str | None,
    blocked_reason: str | None,
    runner: Run | None,
    include_worktree_status: bool,
    checkout_ref: str | None = None,
    pre_commands: list[CommandResult] | None = None,
    pre_observed: bool = True,
) -> EvidenceValidation:
    runner = runner or _run
    range_ref = f"{base_ref}...{head_ref}"
    committed = runner(["git", "-C", str(worktree), "diff", "--name-only", range_ref])
    stat = runner(["git", "-C", str(worktree), "diff", "--stat", range_ref])
    status = None
    if include_worktree_status:
        status = runner([
            "git",
            "-C",
            str(worktree),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ])
    path_groups = [[line for line in committed.stdout.splitlines() if line.strip()]]
    if status is not None:
        path_groups.append(_paths_from_status(status.stdout))
    files_changed = _merge_paths(*path_groups)
    commands: list[CommandResult] = list(pre_commands or [])
    commands.extend([
        CommandResult(f"git diff --name-only {range_ref}", committed.returncode, _summarize(committed)),
        CommandResult(f"git diff --stat {range_ref}", stat.returncode, stat.stdout.strip()),
    ])
    if status is not None:
        commands.append(
            CommandResult(
                "git status --porcelain=v1 --untracked-files=all",
                status.returncode,
                _summarize(status),
            )
        )

    tests: TestResult | None = None
    checkout = None
    if checkout_ref:
        checkout = runner(["git", "-C", str(worktree), "checkout", "--detach", checkout_ref])
        commands.append(
            CommandResult(
                f"git checkout --detach {checkout_ref}", checkout.returncode, _summarize(checkout)
            )
        )
    if test_command:
        if checkout is not None and checkout.returncode != 0:
            tests = TestResult(test_command, None, "checkout failed before test", observed=False)
        else:
            test = runner(test_command, cwd=worktree)
            tests = TestResult(test_command, test.returncode, _summarize(test))
            commands.append(CommandResult(test_command, test.returncode, _summarize(test)))

    evidence = WorklinkEvidence(
        issue=issue,
        attempt=attempt,
        backend=backend,
        branch=branch,
        worktree=str(worktree),
        started_at=started_at.astimezone(UTC).isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
        files_changed=files_changed,
        diff_stat=stat.stdout.strip(),
        commands=commands,
        tests=tests,
        pr_url=pr_url,
        status=_common_status(backend_status),
        blocked_reason=blocked_reason,
        transcript=transcript,
        diff_observed=pre_observed
        and committed.returncode == 0
        and stat.returncode == 0
        and (status is None or status.returncode == 0),
    )
    return validate_evidence(evidence)


def _common_status(status: str) -> str:
    normalized = status.lower().strip()
    if normalized in {"completed", "success", "succeeded", "ok"}:
        return "completed"
    if normalized in {"blocked", "needs_human"}:
        return "blocked"
    return "failed"


def _run(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    if isinstance(args, str):
        # Operator-configured test commands are trusted input, equivalent to
        # poller.command; backend-generated text is never routed here.
        return subprocess.run(args, shell=True, cwd=cwd, capture_output=True, text=True, check=False)
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=False)


def _merge_paths(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            if path and path not in seen:
                seen.add(path)
                merged.append(path)
    return merged


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


def _summarize(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stdout or result.stderr or "").strip()
    if len(text) > 500:
        return text[:497] + "..."
    return text
