"""Worklink evidence schema, observation, and validation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from typing import Callable, Protocol, Sequence
import xml.etree.ElementTree as ET

from ..redaction import redact_text
from .compute import (
    ComputeBackend,
    ComputeResult,
    LaunchHandle,
    WorkSpec,
    with_worker_environment,
)
from .dispatch_failures import terminal_error


@dataclass(frozen=True)
class CommandResult:
    cmd: str
    exit_code: int
    summary: str | None = None
    observed: bool = True


@dataclass(frozen=True)
class TestCounts:
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int


@dataclass(frozen=True)
class TestResult:
    __test__ = False

    cmd: str | None
    exit_code: int | None = None
    summary: str | None = None
    skipped_reason: str | None = None
    observed: bool = True
    counts: TestCounts | None = None
    failed_tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorklinkEvidence:
    issue: int
    attempt: int
    backend: str
    branch: str
    checkout: str
    started_at: str
    finished_at: str
    files_changed: list[str]
    diff_stat: str
    commands: list[CommandResult]
    tests: TestResult | None
    pr_url: str | None
    status: str
    base_ref: str = "main"
    model: str | None = None
    failure_reason: str | None = None
    blocked_reason: str | None = None
    transcript: str | None = None
    diff_observed: bool = True
    executor_tests: TestResult | None = None
    gate_result_diverged: bool | None = None
    # chainlink #817: in-attempt gate-repair rounds this evidence reflects
    # (0 = the gate passed/failed without repair).
    repair_rounds: int = 0
    # Commit pushed for this completed attempt. Recovery uses it to detect PR
    # branch updates made after Worklink finished.
    head_sha: str | None = None


@dataclass(frozen=True)
class EvidenceValidation:
    status: str
    review_ready: bool
    reasons: tuple[str, ...]
    evidence: WorklinkEvidence


Run = Callable[..., subprocess.CompletedProcess[str]]


class EvidenceGit(Protocol):
    def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]: ...


EVIDENCE_SAFE_GIT_OPERATIONS = frozenset({
    "diff_name_only",
    "diff_stat",
    "status",
    "checkout_detach",
})


def validate_evidence(evidence: WorklinkEvidence) -> EvidenceValidation:
    """Validate and normalize backend-independent evidence.

    The review gate is intentionally based on observed diff/test data. A backend
    transcript saying "tests passed" is not enough: callers must provide a
    ``TestResult`` produced by the executor's own command run.
    """
    reasons: list[str] = []
    status = evidence.status

    if evidence.failure_reason:
        evidence = replace(
            evidence,
            failure_reason=terminal_error(evidence.failure_reason),
        )

    if status not in {"completed", "blocked", "failed"}:
        reasons.append("invalid_status")
        status = "failed"

    if status == "blocked" and not evidence.blocked_reason:
        reasons.append("blocked_missing_reason")
        status = "failed"

    if status == "blocked":
        return EvidenceValidation(status="blocked", review_ready=False, reasons=tuple(reasons), evidence=evidence)

    if status == "failed":
        if evidence.failure_reason:
            reasons.append(evidence.failure_reason)
        else:
            # A backend that reports "failed" without supplying text used to
            # produce a record with status=failed, failure_reason=null and an
            # EMPTY reasons list: every other transition below names itself,
            # but this one only did so when the backend happened to provide a
            # message. Chainlink #1108 was diagnosed from three such records —
            # each had committed work and a passing gate, and nothing said why
            # the run failed. `blocked` already has `blocked_missing_reason`
            # for exactly this; `failed` now has its counterpart.
            reasons.append("failed_missing_reason")
            evidence = replace(
                evidence,
                failure_reason=(
                    f"{evidence.backend} reported failure without a reason"
                ),
            )

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
    elif evidence.tests.exit_code == 127:
        # chainlink #820: `sh -c` 127 means the gate COMMAND was not found — an
        # environment/config error no code change can fix. Distinct reason so
        # retries and #817 repair rounds are not spent on it.
        reasons.append("gate_command_not_found")
        if status == "completed":
            status = "failed"
        if not evidence.failure_reason:
            evidence = replace(
                evidence,
                failure_reason="test gate command was not found (exit 127)",
            )
    else:
        reasons.append("tests_failed")
        if status == "completed":
            status = "failed"
        if not evidence.failure_reason:
            evidence = replace(evidence, failure_reason=_gate_failure_reason(evidence.tests))

    review_ready = status == "completed" and bool(evidence.files_changed) and tests_ok and evidence.diff_observed
    if status != evidence.status:
        evidence = replace(evidence, status=status)
    return EvidenceValidation(status=status, review_ready=review_ready, reasons=tuple(reasons), evidence=evidence)


async def observe_evidence(
    *,
    issue: int,
    attempt: int,
    backend: str,
    branch: str,
    checkout: Path,
    started_at: datetime,
    base_ref: str,
    backend_status: str,
    test_command: str | None,
    safe_git: EvidenceGit | None = None,
    head_ref: str = "HEAD",
    checkout_ref: str | None = None,
    work_spec: WorkSpec | None = None,
    compute: ComputeBackend | None = None,
    on_gate_launch: Callable[[LaunchHandle], None] | None = None,
    transcript: str | None = None,
    pr_url: str | None = None,
    blocked_reason: str | None = None,
    model: str | None = None,
    failure_reason: str | None = None,
    executor_tests: TestResult | None = None,
    skip_test_reason: str | None = None,
    runner: Run | None = None,
) -> EvidenceValidation:
    """Build evidence by observing a normalized checkout after a backend run."""
    return await _observe_evidence_from_ref(
        issue=issue,
        attempt=attempt,
        backend=backend,
        branch=branch,
        checkout=checkout,
        started_at=started_at,
        base_ref=base_ref,
        head_ref=head_ref,
        backend_status=backend_status,
        test_command=test_command,
        safe_git=safe_git,
        work_spec=work_spec,
        compute=compute,
        on_gate_launch=on_gate_launch,
        transcript=transcript,
        pr_url=pr_url,
        blocked_reason=blocked_reason,
        model=model,
        failure_reason=failure_reason,
        executor_tests=executor_tests,
        skip_test_reason=skip_test_reason,
        runner=runner,
        include_checkout_status=True,
        checkout_ref=checkout_ref,
    )


async def _observe_evidence_from_ref(
    *,
    issue: int,
    attempt: int,
    backend: str,
    branch: str,
    checkout: Path,
    started_at: datetime,
    base_ref: str,
    head_ref: str,
    backend_status: str,
    test_command: str | None,
    safe_git: EvidenceGit | None,
    work_spec: WorkSpec | None,
    compute: ComputeBackend | None,
    on_gate_launch: Callable[[LaunchHandle], None] | None,
    transcript: str | None,
    pr_url: str | None,
    blocked_reason: str | None,
    model: str | None,
    failure_reason: str | None,
    executor_tests: TestResult | None,
    skip_test_reason: str | None,
    runner: Run | None,
    include_checkout_status: bool,
    checkout_ref: str | None = None,
    pre_commands: list[CommandResult] | None = None,
    pre_observed: bool = True,
) -> EvidenceValidation:
    runner = runner or _run
    from .checkout import coding_enabled

    worker_required = coding_enabled() and backend == "opencode"
    if worker_required and safe_git is None:
        raise ValueError("enabled worker evidence requires controller Git publication")
    range_ref = f"{base_ref}...{head_ref}"
    def git_run(*args: str) -> subprocess.CompletedProcess[str]:
        if safe_git is not None:
            return safe_git.run(*args)
        return runner(["git", "-C", str(checkout), *args])

    committed = git_run("diff", "--name-only", range_ref)
    stat = git_run("diff", "--stat", range_ref)
    status = None
    if include_checkout_status:
        status = git_run(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
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
    checkout_result = None
    if checkout_ref:
        checkout_result = git_run("checkout", "--detach", checkout_ref)
        commands.append(
            CommandResult(
                f"git checkout --detach {checkout_ref}",
                checkout_result.returncode,
                _summarize(checkout_result),
            )
        )
    if test_command and skip_test_reason:
        tests = TestResult(test_command, skipped_reason=skip_test_reason)
    elif test_command:
        if checkout_result is not None and checkout_result.returncode != 0:
            tests = TestResult(test_command, None, "checkout failed before test", observed=False)
        else:
            with tempfile.TemporaryDirectory(prefix="worklink-gate-") as report_dir_text:
                report_dir = Path(report_dir_text)
                if worker_required:
                    if compute is None:
                        raise ValueError("enabled worker evidence requires a compute backend")
                    if work_spec is None:
                        raise ValueError("worker evidence requires the originating WorkSpec")
                    result = await _run_compute_gate(
                        test_command,
                        checkout=checkout,
                        work_spec=work_spec,
                        compute=compute,
                        on_launch=on_gate_launch,
                        report_dir=report_dir,
                    )
                    test = subprocess.CompletedProcess(
                        ["/bin/sh", "-c", test_command],
                        result.exit_code,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )
                else:
                    observed_command = _command_with_pytest_report(test_command, report_dir)
                    test = runner(observed_command, cwd=checkout)
                structured = read_pytest_result(test_command, report_dir)
                tests = replace(
                    structured or TestResult(test_command),
                    exit_code=test.returncode,
                    summary=_summarize_test_output(test),
                )
            commands.append(CommandResult(test_command, test.returncode, _summarize(test)))

    evidence = WorklinkEvidence(
        issue=issue,
        attempt=attempt,
        backend=backend,
        branch=branch,
        checkout=str(checkout),
        started_at=started_at.astimezone(UTC).isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
        files_changed=files_changed,
        diff_stat=stat.stdout.strip(),
        commands=commands,
        tests=tests,
        pr_url=pr_url,
        status=_common_status(backend_status),
        base_ref=work_spec.base_ref if work_spec is not None else base_ref,
        model=model,
        failure_reason=failure_reason,
        blocked_reason=blocked_reason,
        transcript=transcript,
        executor_tests=executor_tests,
        gate_result_diverged=_gate_results_diverge(executor_tests, tests),
        diff_observed=pre_observed
        and committed.returncode == 0
        and stat.returncode == 0
        and (status is None or status.returncode == 0),
    )
    return validate_evidence(evidence)


async def _run_compute_gate(
    command: str,
    *,
    checkout: Path,
    work_spec: WorkSpec,
    compute: ComputeBackend,
    on_launch: Callable[[LaunchHandle], None] | None = None,
    report_dir: Path | None = None,
) -> ComputeResult:
    gate_spec = replace(
        work_spec,
        local_checkout=checkout,
        local_argv=("/bin/sh", "-c", command),
    )
    if report_dir is not None:
        gate_spec = with_worker_environment(
            gate_spec,
            pytest_report_environment(
                command,
                report_dir,
                existing=gate_spec.env.get("PYTEST_ADDOPTS"),
            ),
        )
    handle = await compute.launch(gate_spec)
    try:
        if on_launch is not None:
            try:
                on_launch(handle)
            except BaseException:
                await compute.cancel(handle)
                raise
        return await compute.wait(handle, gate_spec.timeout_s)
    except asyncio.CancelledError:
        await compute.cancel(handle)
        raise
    finally:
        await compute.cleanup(handle)


def _common_status(status: str) -> str:
    normalized = status.lower().strip()
    if normalized in {"completed", "success", "succeeded", "ok"}:
        return "completed"
    if normalized in {"blocked", "needs_human"}:
        return "blocked"
    return "failed"


_PYTEST_REPORT_MAX_BYTES = 2_000_000


def pytest_report_environment(
    command: str,
    report_dir: Path,
    *,
    existing: str | None = None,
) -> dict[str, str]:
    """Configure pytest's machine reports without changing the retained output."""
    if not _is_pytest_command(command):
        return {}
    report_dir.mkdir(parents=True, exist_ok=True)
    options = (
        f"--junitxml={shlex.quote(str(report_dir / 'junit.xml'))} "
        f"-o cache_dir={shlex.quote(str(report_dir / 'cache'))}"
    )
    return {"PYTEST_ADDOPTS": " ".join(part for part in (existing, options) if part)}


def read_pytest_result(command: str, report_dir: Path) -> TestResult | None:
    """Read counts and exact failed node IDs from pytest-owned machine files."""
    junit_path = report_dir / "junit.xml"
    lastfailed_path = report_dir / "cache" / "v" / "cache" / "lastfailed"
    try:
        if junit_path.stat().st_size > _PYTEST_REPORT_MAX_BYTES:
            return None
        root = ET.parse(junit_path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        total = sum(_xml_count(suite, "tests") for suite in suites)
        failed = sum(_xml_count(suite, "failures") for suite in suites)
        errors = sum(_xml_count(suite, "errors") for suite in suites)
        skipped = sum(_xml_count(suite, "skipped") for suite in suites)
    except (OSError, ET.ParseError, ValueError):
        return None

    failed_tests: tuple[str, ...] = ()
    try:
        if lastfailed_path.stat().st_size <= _PYTEST_REPORT_MAX_BYTES:
            payload = json.loads(lastfailed_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                failed_tests = tuple(
                    redact_text(node_id)[:1000]
                    for node_id, is_failed in payload.items()
                    if isinstance(node_id, str) and is_failed is True
                )
    except (OSError, json.JSONDecodeError):
        pass

    counts = TestCounts(
        total=total,
        passed=max(0, total - failed - errors - skipped),
        failed=failed,
        errors=errors,
        skipped=skipped,
    )
    return TestResult(
        command,
        exit_code=0 if failed == 0 and errors == 0 else 1,
        counts=counts,
        failed_tests=failed_tests,
    )


def _xml_count(suite: ET.Element, name: str) -> int:
    return int(suite.attrib.get(name, "0"))


def _is_pytest_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return any(Path(token).name in {"pytest", "py.test"} for token in tokens)


def _command_with_pytest_report(command: str, report_dir: Path) -> str:
    environment = pytest_report_environment(command, report_dir)
    if not environment:
        return command
    return f"PYTEST_ADDOPTS={shlex.quote(environment['PYTEST_ADDOPTS'])} {command}"


def _gate_failure_reason(tests: TestResult) -> str:
    counts = tests.counts
    if counts is None:
        return terminal_error(
            f"test gate failed (exit {tests.exit_code}); structured counts unavailable"
        )
    count_text = (
        f"{counts.failed} failed, {counts.errors} errors, {counts.passed} passed, "
        f"{counts.skipped} skipped, {counts.total} total"
    )
    failures = ", ".join(tests.failed_tests) or "no failing node IDs reported"
    return terminal_error(f"test gate failed; counts: {count_text}; failures: {failures}")


def _gate_results_diverge(
    executor: TestResult | None,
    measured: TestResult | None,
) -> bool | None:
    if executor is None or measured is None:
        return None
    return (
        executor.exit_code,
        executor.counts,
        executor.failed_tests,
    ) != (
        measured.exit_code,
        measured.counts,
        measured.failed_tests,
    )


def _run(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    from ..tools._shell_env import scrub_model_selection_env

    env = os.environ.copy()
    scrub_model_selection_env(env)
    if isinstance(args, str):
        # Operator-configured test commands are trusted input, equivalent to
        # poller.command; backend-generated text is never routed here.
        return subprocess.run(
            args, shell=True, cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
    return subprocess.run(
        list(args), cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


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


_TEST_OUTPUT_TAIL_LINES = 60
_TEST_OUTPUT_TAIL_CHARS = 6000


def _summarize_test_output(result: subprocess.CompletedProcess[str]) -> str:
    """Tail-based summary for the gate test run (chainlink #815). Test runners
    print the failure list LAST — a head-truncated summary loses exactly the
    detail a retry needs to act on."""
    parts = [part for part in (result.stdout, result.stderr) if part and part.strip()]
    text = "\n".join(part.strip() for part in parts)
    if not text:
        return ""
    clipped = "\n".join(text.splitlines()[-_TEST_OUTPUT_TAIL_LINES:])
    if len(clipped) > _TEST_OUTPUT_TAIL_CHARS:
        clipped = clipped[-_TEST_OUTPUT_TAIL_CHARS:]
    return clipped
