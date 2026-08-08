"""Worklink evidence schema, observation, and validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
from typing import Callable, Sequence

from .dispatch_failures import terminal_error


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
    checkout: str
    started_at: str
    finished_at: str
    files_changed: list[str]
    diff_stat: str
    commands: list[CommandResult]
    tests: TestResult | None
    pr_url: str | None
    status: str
    model: str | None = None
    failure_reason: str | None = None
    blocked_reason: str | None = None
    transcript: str | None = None
    diff_observed: bool = True
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
    checkout: Path,
    started_at: datetime,
    base_ref: str,
    backend_status: str,
    test_command: str | None,
    transcript: str | None = None,
    pr_url: str | None = None,
    blocked_reason: str | None = None,
    model: str | None = None,
    failure_reason: str | None = None,
    skip_test_reason: str | None = None,
    runner: Run | None = None,
) -> EvidenceValidation:
    """Build evidence by observing a checkout after a backend run."""
    return _observe_evidence_from_ref(
        issue=issue,
        attempt=attempt,
        backend=backend,
        branch=branch,
        checkout=checkout,
        started_at=started_at,
        base_ref=base_ref,
        head_ref="HEAD",
        backend_status=backend_status,
        test_command=test_command,
        transcript=transcript,
        pr_url=pr_url,
        blocked_reason=blocked_reason,
        model=model,
        failure_reason=failure_reason,
        skip_test_reason=skip_test_reason,
        runner=runner,
        include_checkout_status=True,
    )



def _observe_evidence_from_ref(
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
    transcript: str | None,
    pr_url: str | None,
    blocked_reason: str | None,
    model: str | None,
    failure_reason: str | None,
    skip_test_reason: str | None,
    runner: Run | None,
    include_checkout_status: bool,
    checkout_ref: str | None = None,
    pre_commands: list[CommandResult] | None = None,
    pre_observed: bool = True,
) -> EvidenceValidation:
    runner = runner or _run
    range_ref = f"{base_ref}...{head_ref}"
    committed = runner(["git", "-C", str(checkout), "diff", "--name-only", range_ref])
    stat = runner(["git", "-C", str(checkout), "diff", "--stat", range_ref])
    status = None
    if include_checkout_status:
        status = runner([
            "git",
            "-C",
            str(checkout),
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
    checkout_result = None
    if checkout_ref:
        checkout_result = runner(["git", "-C", str(checkout), "checkout", "--detach", checkout_ref])
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
            test = runner(test_command, cwd=checkout)
            tests = TestResult(test_command, test.returncode, _summarize_test_output(test))
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
        model=model,
        failure_reason=failure_reason,
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


def _checkout_of(args: Sequence[str] | str, cwd: Path | None) -> Path | None:
    """The attempt checkout a step operates on, for the contained request.

    Most git calls here pass the checkout as ``git -C <path>`` with no ``cwd``,
    so taking ``cwd`` alone would send them at the controller's directory.
    """
    if cwd is not None:
        return Path(cwd)
    if not isinstance(args, str):
        parts = [str(a) for a in args]
        if "-C" in parts:
            index = parts.index("-C")
            if index + 1 < len(parts):
                return Path(parts[index + 1])
    return None


def _run(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    from ..tools._shell_env import scrub_model_selection_env

    env = os.environ.copy()
    scrub_model_selection_env(env)

    # chainlink #1164. The comment this replaces argued the test command is
    # operator-configured and therefore trusted. That is true of the command
    # STRING and irrelevant to what it EXECUTES: `pytest` reads conftest.py,
    # `npm test` reads package.json scripts, and both were just written by the
    # build. The same applies to git over the checkout. So every step here runs
    # under the contained identity when containment is active.
    checkout = _checkout_of(args, cwd)
    contained = maybe_run_contained(args, checkout, env)
    if contained is not None:
        return contained

    if isinstance(args, str):
        return subprocess.run(
            args, shell=True, cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
    return subprocess.run(
        list(args), cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def maybe_run_contained(
    args: Sequence[str] | str, checkout: Path | None, env: dict[str, str],
) -> subprocess.CompletedProcess[str] | None:
    """Run one evidence step under the contained identity, or ``None`` to fall through.

    Returns ``None`` -- meaning "run as before" -- when containment is not
    required (no coding flag) or when the step has no attempt checkout to run in.
    """
    if checkout is None or not _is_attempt_checkout(checkout):
        return None
    from .containment import is_registered_attempt_checkout

    if not is_registered_attempt_checkout(checkout):
        # Two conditions, both necessary. The marker keeps the obvious cases out
        # cheaply; registration is the authority, because a configured repository
        # sitting under some `.worklink` directory would satisfy the marker alone
        # and the supervisor chowns whatever cwd it is handed, recursively.
        #
        # A deny-list came first and was worse: "not the agent home, not remote"
        # sent `git -C <parent repo> status` -- which _finalize runs on every
        # run -- to the supervisor, handing the operator's own source repository
        # to the worker uid.
        return None
    if _talks_to_the_remote(args):
        # Push, fetch and friends are CONTROLLER operations: they need the
        # GitHub credential, which is deliberately never projected into a
        # contained step. Routing them here would strip the credential and the
        # push would simply fail to authenticate.
        return None
    from .containment import (
        WorkerRequest,
        containment_required,
        resolve_containment,
        run_contained,
        worker_runtime_env,
    )

    if not containment_required():
        return None
    policy = resolve_containment()
    if not policy.contained:
        return None
    argv = ("sh", "-c", args) if isinstance(args, str) else tuple(str(a) for a in args)
    projected = worker_runtime_env(policy, env)
    result = run_contained(
        policy,
        WorkerRequest(
            attempt_id=f"evidence-{checkout.name}",
            argv=argv,
            cwd=checkout,
            env=projected,
            timeout_seconds=_evidence_step_timeout(),
        ),
    )
    return subprocess.CompletedProcess(
        args=list(argv), returncode=result.exit_status, stdout=result.stdout, stderr=result.stderr,
    )


#: Attempt checkouts are created at ``<repo>/.worklink/<issue>-<attempt>``. That
#: component is what marks a path as disposable, worker-owned build space rather
#: than a repository the operator cares about.
_ATTEMPT_CHECKOUT_MARKER = ".worklink"


def _is_attempt_checkout(path: Path) -> bool:
    """Whether ``path`` is inside a Worklink attempt checkout.

    The supervisor chowns whatever it is handed, so this is the guard that keeps
    it away from the parent repository and the agent home alike. Being wrong in
    the permissive direction here is destructive, not merely over-contained.
    """
    try:
        parts = path.resolve().parts
    except OSError:  # pragma: no cover
        parts = path.parts
    return _ATTEMPT_CHECKOUT_MARKER in parts


#: Git subcommands that contact the remote, and therefore need the controller's
#: credential. Everything else over an attempt checkout is local and contained.
_REMOTE_GIT_SUBCOMMANDS = frozenset({"push", "fetch", "pull", "clone", "ls-remote", "remote"})


def _talks_to_the_remote(args: Sequence[str] | str) -> bool:
    if isinstance(args, str):
        return False
    parts = [str(a) for a in args]
    if not parts or Path(parts[0]).name != "git":
        return False
    # Walk by index: the options that take a VALUE must consume it, or the value
    # gets read as the subcommand -- `git -C /path push` would resolve to
    # "/path" and a push would be routed into containment, stripping the
    # credential it needs.
    takes_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
    index = 1
    while index < len(parts):
        part = parts[index]
        if part in takes_value:
            index += 2
            continue
        if part.startswith("-"):
            index += 1
            continue
        return part in _REMOTE_GIT_SUBCOMMANDS
    return False


def _evidence_step_timeout() -> float:
    """Deadline for a contained evidence step.

    The gate's test command is the long one here; git calls finish in
    milliseconds. Without a deadline the supervisor would spawn with none.
    """
    raw = os.environ.get("MIMIR_WORKLINK_EVIDENCE_TIMEOUT_S", "")
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    return value if value > 0 else 3600.0


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
