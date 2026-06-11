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


Run = Callable[[Sequence[str] | str], subprocess.CompletedProcess[str]]


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

    if status == "completed" and not evidence.files_changed:
        reasons.append("completed_empty_diff")
        status = "failed"

    if not evidence.diff_observed:
        reasons.append("diff_not_observed")
        status = "failed"

    tests_ok = False
    if evidence.tests is None:
        reasons.append("tests_missing")
        if status == "completed":
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
    runner: Run | None = None,
) -> EvidenceValidation:
    """Build evidence by observing the worktree after a backend run."""
    runner = runner or _run
    committed = runner(["git", "-C", str(worktree), "diff", "--name-only", f"{base_ref}...HEAD"])
    stat = runner(["git", "-C", str(worktree), "diff", "--stat", f"{base_ref}...HEAD"])
    status = runner(["git", "-C", str(worktree), "status", "--porcelain=v1", "--untracked-files=all"])
    files_changed = _merge_paths(
        [line for line in committed.stdout.splitlines() if line.strip()],
        _paths_from_status(status.stdout),
    )
    commands: list[CommandResult] = [
        CommandResult(f"git diff --name-only {base_ref}...HEAD", committed.returncode, _summarize(committed)),
        CommandResult(f"git diff --stat {base_ref}...HEAD", stat.returncode, stat.stdout.strip()),
        CommandResult("git status --porcelain=v1 --untracked-files=all", status.returncode, _summarize(status)),
    ]

    tests: TestResult | None = None
    if test_command:
        test = runner(test_command)
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
        blocked_reason=None,
        transcript=transcript,
        diff_observed=committed.returncode == 0 and stat.returncode == 0 and status.returncode == 0,
    )
    return validate_evidence(evidence)


def _common_status(status: str) -> str:
    normalized = status.lower().strip()
    if normalized in {"completed", "success", "succeeded", "ok"}:
        return "completed"
    if normalized in {"blocked", "needs_human"}:
        return "blocked"
    return "failed"


def _run(args: Sequence[str] | str) -> subprocess.CompletedProcess[str]:
    if isinstance(args, str):
        # Operator-configured test commands are trusted input, equivalent to
        # poller.command; backend-generated text is never routed here.
        return subprocess.run(args, shell=True, capture_output=True, text=True, check=False)
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


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
