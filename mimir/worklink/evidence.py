"""Worklink evidence schema, observation, and validation."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat as stat_module
import subprocess
import tempfile
from typing import Callable, Iterator, Protocol, Sequence
import uuid
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


log = logging.getLogger(__name__)


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
    report_error: str | None = None
    # Failed in the gate but passed in isolation; diagnostic, not proof of flakiness.
    flaky_tests: tuple[str, ...] = ()
    initial_run: TestResult | None = None
    rerun: TestResult | None = None
    previous_observation: TestResult | None = None
    timed_out: bool = False
    retained_artifacts: tuple[str, ...] = ()
    retention_manifest: str | None = None
    retention_truncated: bool = False
    retention_error: str | None = None
    gate_run_id: str | None = None
    gate_phase: str | None = None


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
    test_env: dict[str, str] = field(default_factory=dict)


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
    elif evidence.tests.skipped_reason is not None:
        # A recorded skip explains the missing gate; it does not pass it.
        tests_ok = False
    elif evidence.tests.timed_out:
        # A gate budget/configuration fault, not a failing assertion to repair.
        reasons.append("gate_timed_out")
        if status == "completed":
            status = "failed"
        if not evidence.failure_reason:
            evidence = replace(evidence, failure_reason="test gate timed out; check gate timeout configuration")
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
    gate_rerun_max_failures: int = 10,
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
        gate_rerun_max_failures=gate_rerun_max_failures,
        include_checkout_status=True,
        checkout_ref=checkout_ref,
    )


@contextlib.contextmanager
def _gate_report_directory(
    checkout: Path, worker_uid_drop: bool, *, cleanup_errors: list[str] | None = None,
) -> Iterator[Path]:
    """Yield a report directory the process that runs the gate can write.

    ``tempfile.TemporaryDirectory`` creates 0700 owned by the CONTROLLER. When the
    gate is dropped to the worker uid that directory is unwritable, and pytest
    raises ``PermissionError`` from ``pytest_sessionfinish`` while writing
    ``--junitxml`` -- AFTER every test has already passed. The gate then exits 1 on
    a green run, and ``read_pytest_result`` finds no junit.xml, so the failure is
    reported as ``exit 1, structured counts unavailable`` and names nothing.

    On the worker path the directory is created inside the attempt checkout, which
    is group-owned by the worker and setgid (2770), so the new directory inherits
    that group and both identities can use it: the worker writes the report, the
    controller reads it back. This is narrower than widening a temp directory to
    0777, and needs no shared group membership between the two identities.
    """
    if worker_uid_drop:
        checkout.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(
        prefix=".worklink-gate-" if worker_uid_drop else "worklink-gate-",
        dir=checkout if worker_uid_drop else None,
    )
    try:
        report_dir = Path(temporary.name).resolve(strict=True)
        if worker_uid_drop:
            # setgid on the checkout supplies the group; make it group-usable.
            report_dir.chmod(0o770)
        yield report_dir
    finally:
        # Only cleanup is caught here, never an exception from the gate body.
        try:
            temporary.cleanup()
        except Exception as exc:
            if cleanup_errors is not None:
                cleanup_errors.append(f"report_cleanup_failed:{type(exc).__name__}")
            with contextlib.suppress(Exception):
                log.warning("Worklink gate report cleanup incomplete (%s)", type(exc).__name__)


_GATE_RETENTION_BYTES = 32 * 1024 * 1024
_GATE_RETENTION_ENTRIES = 2048
_GATE_RETENTION_DEPTH = 32


def _gate_tmp_directory(report_dir: Path) -> Path:
    # Pytest makes basetemp private. It must never be a child of the controller's
    # TemporaryDirectory: that identity cannot chmod/remove a worker's 0700 tree.
    return report_dir.with_name(report_dir.name + "-tmp")


def _export_gate_tmp(root: str, max_bytes: int, max_entries: int, max_depth: int) -> dict:
    """Export regular whole files, also executable under the worker identity.

    Keep imports local: the worker runs this source with isolated Python, without
    importing code from the untrusted checkout or the controller's environment.
    """
    import base64
    import os
    import stat

    result = {"files": [], "reasons": [], "entries": 0}
    try:
        metadata = os.stat(root, follow_symlinks=False)
    except FileNotFoundError:
        return result
    except OSError:
        result["reasons"].append("tmp_unavailable")
        return result
    if not stat.S_ISDIR(metadata.st_mode):
        result["reasons"].append("tmp_not_directory")
        return result

    remaining = max_bytes

    def omit(reason):
        if reason not in result["reasons"]:
            result["reasons"].append(reason)

    def visit(fd, prefix, depth):
        nonlocal remaining
        with os.scandir(fd) as entries:
            for entry in entries:
                if result["entries"] >= max_entries:
                    omit("entry_limit")
                    break
                result["entries"] += 1
                name = prefix + entry.name
                try:
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        if depth >= max_depth:
                            omit("depth_limit")
                            continue
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            visit(child, name + "/", depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(metadata.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                        with os.fdopen(child, "rb") as source:
                            metadata = os.fstat(source.fileno())
                            if not stat.S_ISREG(metadata.st_mode):
                                omit("unsafe_entry")
                                continue
                            if metadata.st_size > remaining:
                                omit("byte_limit")
                                continue
                            data = source.read(remaining + 1)
                            if len(data) > remaining or len(data) != metadata.st_size or os.fstat(source.fileno()).st_size != len(data):
                                omit("file_changed_or_oversize")
                                continue
                        remaining -= len(data)
                        result["files"].append({"name": name, "data": base64.b64encode(data).decode("ascii")})
                    else:
                        omit("unsafe_entry")
                except OSError:
                    omit("tmp_read_error")

    # Anchor every component, including ancestors of the export root.
    absolute = os.path.isabs(root)
    components = root.split("/")[1:] if absolute else root.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("invalid export root")
    fd = os.open("/" if absolute else ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in components:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        visit(fd, "", 1)
    finally:
        os.close(fd)
    return result


async def _retain_gate_failure(
    observation: TestResult,
    output: subprocess.CompletedProcess[str],
    *,
    report_dir: Path,
    issue: int,
    attempt: int,
    run_id: str,
    phase: str,
    checkout: Path,
    work_spec: WorkSpec | None,
    compute: ComputeBackend | None,
    compute_result: ComputeResult | None,
    worker_uid_drop: bool,
) -> TestResult:
    """Best-effort diagnostics only. Gate execution is outside this boundary."""
    artifacts: list[str] = []
    records: list[dict] = []
    reasons: list[str] = []
    manifest_path = None
    remaining = _GATE_RETENTION_BYTES
    entries = 1  # Reserve an entry for the manifest in the shared budget.
    try:
        import base64
        import inspect
        from ..output_capture import open_output_sink

        root = (work_spec.output_root if work_spec else None) or (
            Path(os.environ.get("MIMIR_HOME", ".")).resolve() / "state/worklink/transcripts"
        )
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("retention destination must be an absolute normalized path")
        if any(root.is_relative_to(directory) for directory in (report_dir, _gate_tmp_directory(report_dir))):
            raise ValueError("retention destination is inside a temporary gate directory")
        stem = f"gate-{issue}-a{attempt}-{run_id}-{phase}"

        def write(name: str, data: bytes) -> None:
            nonlocal remaining, entries
            if entries >= _GATE_RETENTION_ENTRIES:
                reasons.append("entry_limit")
                return
            entries += 1
            try:
                data = redact_text(data.decode("utf-8")).encode("utf-8")
            except UnicodeDecodeError:
                pass
            if len(data) > remaining:
                reasons.append("byte_limit")
                return
            destination = root / f"{stem}-{len(artifacts):04d}.artifact"
            sink = open_output_sink(destination, max(1, len(data)))
            try:
                sink.file.write(data)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            finally:
                sink.close()
            remaining -= len(data)
            artifacts.append(str(destination))
            records.append({"source": redact_text(name), "path": str(destination), "bytes": len(data)})

        def copy(name: str, source: Path) -> None:
            try:
                data = _gate_read(source, remaining)
            except (OSError, ValueError) as exc:
                reasons.append(f"{name}:{type(exc).__name__}")
                return
            write(name, data)

        if _is_pytest_command(observation.cmd or ""):
            copy("junit.xml", report_dir / "junit.xml")
        for stream in ("stdout", "stderr"):
            path = getattr(compute_result, f"{stream}_path", None)
            if path is not None:
                copy(stream, path)
            else:
                write(stream, (getattr(output, stream) or "").encode("utf-8"))
        if compute_result is not None and compute_result.output_overflow:
            reasons.append("output_overflow")

        tmp = _gate_tmp_directory(report_dir)
        # A missing basetemp means pytest never used tmp_path. Permission/I/O
        # failures instead mean its contents are unknown, not known empty.
        try:
            tmp.lstat()
        except FileNotFoundError:
            tmp_available = False
        except OSError:
            tmp_available = False
            reasons.append("tmp_unavailable")
        else:
            tmp_available = True
        exported_tmp = {"files": [], "reasons": [], "entries": 0}
        if tmp_available and worker_uid_drop:
            # Execute only the exporter in this guard, never the test command.
            script = (
                inspect.getsource(_export_gate_tmp)
                + "\nimport json, shutil\n"
                + f"root = {str(tmp.relative_to(checkout.resolve()))!r}\n"
                + "try:\n"
                + f" print(json.dumps(_export_gate_tmp(root, {remaining}, {_GATE_RETENTION_ENTRIES - entries}, {_GATE_RETENTION_DEPTH})))\n"
                + "finally:\n shutil.rmtree(root, ignore_errors=True)\n"
            )
            # ComputeResult.stdout may be an excerpt. The controller owns these
            # sinks; consume the full bounded export before deleting them.
            with tempfile.TemporaryDirectory(prefix="worklink-gate-export-") as text:
                export_root = Path(text).resolve(strict=True)
                exported = await _run_compute_gate(
                    shlex.join(["python3", "-I", "-c", script]),
                    checkout=checkout, work_spec=replace(work_spec, output_root=export_root), compute=compute,
                    diagnostic=True,
                )
                if exported.exit_code or exported.timed_out or exported.output_overflow:
                    raise ValueError("tmp_export_failed")
                if exported.stdout_path is None or exported.stdout_path.parent != export_root:
                    raise ValueError("tmp_export_output_unavailable")
                document = _gate_read(exported.stdout_path, 2 * _GATE_RETENTION_BYTES)
                exported_tmp = json.loads(document)
        elif tmp_available:
            exported_tmp = _export_gate_tmp(
                str(tmp), remaining, _GATE_RETENTION_ENTRIES - entries, _GATE_RETENTION_DEPTH,
            )
        reasons.extend(exported_tmp["reasons"])
        for item in exported_tmp["files"]:
            if entries >= _GATE_RETENTION_ENTRIES:
                reasons.append("entry_limit")
                break
            # Names are metadata only, never destination paths.
            name = item["name"]
            if len(Path(name).parts) > _GATE_RETENTION_DEPTH:
                reasons.append("depth_limit")
                continue
            write("tmp/" + name, base64.b64decode(item["data"], validate=True))
        document = json.dumps({
            "issue": issue, "attempt": attempt, "run_id": run_id, "phase": phase,
            "exit_code": observation.exit_code, "timed_out": observation.timed_out,
            "artifacts": records, "truncated": bool(reasons),
            "reasons": sorted(set(reasons)),
        }, ensure_ascii=True).encode("utf-8")
        destination = root / f"{stem}.json"
        if len(document) > remaining:
            reasons.append("manifest_byte_limit")
            raise ValueError("manifest_byte_limit")
        if entries > _GATE_RETENTION_ENTRIES:
            reasons.append("manifest_entry_limit")
            raise ValueError("manifest_entry_limit")
        sink = open_output_sink(destination, max(1, len(document)))
        try:
            sink.file.write(document)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            sink.close()
        manifest_path = str(destination)
    except Exception as exc:
        reasons.append(f"retention_failed:{type(exc).__name__}")
    if reasons:
        with contextlib.suppress(Exception):
            log.warning("Worklink gate retention incomplete issue=%s attempt=%s run=%s phase=%s (%s)",
                        issue, attempt, run_id, phase, ", ".join(sorted(set(reasons))))
    return replace(
        observation, retained_artifacts=tuple(artifacts), retention_manifest=manifest_path,
        retention_truncated=bool(reasons), retention_error=", ".join(sorted(set(reasons))) or None,
        gate_run_id=run_id, gate_phase=phase,
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
    gate_rerun_max_failures: int = 10,
) -> EvidenceValidation:
    runner = runner or _run
    from .checkout import coding_enabled

    worker_uid_drop = coding_enabled() and backend == "opencode"
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
            run_id = uuid.uuid4().hex

            async def run_gate(command: str, report_dir: Path, phase: str) -> TestResult:
                timed_out = False
                result = None
                if worker_uid_drop:
                    if compute is None:
                        raise ValueError("enabled worker evidence requires a compute backend")
                    if work_spec is None:
                        raise ValueError("worker evidence requires the originating WorkSpec")
                    result = await _run_compute_gate(
                        command,
                        checkout=checkout,
                        work_spec=work_spec,
                        compute=compute,
                        on_launch=on_gate_launch,
                        report_dir=report_dir,
                    )
                    timed_out = result.timed_out
                    test = subprocess.CompletedProcess(
                        ["/bin/sh", "-c", command],
                        result.exit_code,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )
                else:
                    observed_command = _command_with_pytest_report(command, report_dir)
                    from .backends.registry import WorklinkDefaults

                    timeout_s = work_spec.timeout_s if work_spec is not None else WorklinkDefaults().timeout_s
                    try:
                        test = await asyncio.to_thread(
                            runner, observed_command, cwd=checkout, timeout=timeout_s,
                        )
                    except subprocess.TimeoutExpired as exc:
                        timed_out = True
                        # TimeoutExpired may carry bytes even with text=True.
                        stdout = exc.stdout or ""
                        stderr = exc.stderr or ""
                        test = subprocess.CompletedProcess(
                            observed_command, 124,
                            stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout,
                            stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr,
                        )
                structured = read_pytest_result(command, report_dir)
                commands.append(CommandResult(redact_text(command), test.returncode, redact_text(_summarize(test))))
                observation = replace(
                    structured or TestResult(redact_text(command)),
                    cmd=redact_text(command),
                    exit_code=test.returncode,
                    summary=redact_text(_summarize_test_output(test)),
                    timed_out=timed_out,
                    gate_run_id=run_id,
                    gate_phase=phase,
                )
                if test.returncode != 0 or timed_out or phase == "rerun":
                    try:
                        observation = await _retain_gate_failure(
                            observation, test, report_dir=report_dir, issue=issue,
                            attempt=attempt, run_id=run_id, phase=phase, checkout=checkout,
                            work_spec=work_spec, compute=compute, compute_result=result,
                            worker_uid_drop=worker_uid_drop,
                        )
                    except Exception as exc:
                        with contextlib.suppress(Exception):
                            log.warning("Worklink gate retention failed (%s)", type(exc).__name__)
                        observation = replace(
                            observation, retention_truncated=True,
                            retention_error=f"retention_failed:{type(exc).__name__}",
                        )
                # Cleanup is also diagnostic-only and must not mask the verdict.
                try:
                    tmp = _gate_tmp_directory(report_dir)
                    if not worker_uid_drop:
                        try:
                            shutil.rmtree(tmp)
                        except FileNotFoundError:
                            pass
                    else:
                        try:
                            tmp.lstat()
                        except FileNotFoundError:
                            pass
                        else:
                            cleanup = await _run_compute_gate(
                                shlex.join(["python3", "-I", "-c", "import shutil; shutil.rmtree(" + repr(str(tmp.relative_to(checkout.resolve()))) + ")"]),
                                checkout=checkout, work_spec=replace(work_spec, output_root=None), compute=compute,
                                diagnostic=True,
                            )
                            if cleanup.exit_code != 0:
                                raise OSError("tmp_cleanup_failed")
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        log.warning("Worklink gate tmp cleanup incomplete (%s)", type(exc).__name__)
                    observation = replace(
                        observation, retention_truncated=True,
                        retention_error=", ".join(filter(None, (observation.retention_error, "tmp_cleanup_failed"))),
                    )
                return observation

            cleanup_errors: list[str] = []
            with _gate_report_directory(checkout, worker_uid_drop, cleanup_errors=cleanup_errors) as report_dir:
                tests = await run_gate(test_command, report_dir, "initial")
                failed_ids = _pytest_cache_ids(report_dir, "lastfailed")
                rerun_command = _pytest_rerun_command(test_command, failed_ids)
                eligible = (
                    tests.exit_code == 1
                    and not tests.timed_out
                    and tests.report_error is None
                    and tests.counts is not None
                    and tests.counts.errors == 0
                    and tests.counts.failed == len(failed_ids)
                    and 0 < len(failed_ids) <= gate_rerun_max_failures
                    and rerun_command is not None
                )
            if cleanup_errors:
                tests = replace(tests, retention_truncated=True, retention_error=", ".join(
                    filter(None, (tests.retention_error, *cleanup_errors)),
                ))
            if eligible:
                cleanup_errors = []
                with _gate_report_directory(checkout, worker_uid_drop, cleanup_errors=cleanup_errors) as rerun_dir:
                    rerun = await run_gate(rerun_command, rerun_dir, "rerun")
                    remaining = _pytest_cache_ids(rerun_dir, "lastfailed")
                    collected = _pytest_cache_ids(rerun_dir, "nodeids")
                    counts = rerun.counts
                    # An exit-zero rerun alone is not proof: selection, skips,
                    # missing reports or a different collected set must fail closed.
                    complete = (
                        rerun.exit_code in {0, 1}
                        and not rerun.timed_out
                        and rerun.report_error is None
                        and counts is not None
                        and counts.total == len(failed_ids)
                        and counts.errors == 0
                        and counts.skipped == 0
                        and counts.failed == len(remaining)
                        and counts.passed + counts.failed == counts.total
                        and set(collected) == set(failed_ids)
                        and set(remaining) <= set(failed_ids)
                        and (rerun.exit_code == 0) == (counts.failed == 0)
                    )
                if cleanup_errors:
                    rerun = replace(rerun, retention_truncated=True, retention_error=", ".join(
                        filter(None, (rerun.retention_error, *cleanup_errors)),
                    ))
                initial = tests
                tests = replace(tests, initial_run=initial, rerun=rerun, timed_out=rerun.timed_out)
                if complete:
                    # Deliberately supersedes Chainlink #1557's verdict-flip:
                    # parallel-only failures (xdist ordering/shared state) are real
                    # defects; passing in isolation must not hide them. Preserve
                    # the gate's verdict, counts and failures; rerun is evidence only.
                    tests = replace(
                        tests,
                        flaky_tests=tuple(
                            redact_text(node)[:1000] for node in failed_ids if node not in remaining
                        ),
                    )

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
        test_env=dict(work_spec.backend_config.get("test_env", {})) if work_spec else {},
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
    diagnostic: bool = False,
) -> ComputeResult:
    # Diagnostics get their own job budget, never the test's full allowance.
    # Keep deadline enforcement and bounded process teardown in compute.wait;
    # the worker-side timeout must agree with that supervisor too.
    gate_spec = replace(
        work_spec,
        local_checkout=checkout,
        local_argv=("/bin/sh", "-c", command),
        timeout_s=min(work_spec.timeout_s, 60) if diagnostic else work_spec.timeout_s,
    )
    if report_dir is not None:
        report_option_dir = report_dir
        try:
            # Contained workers enter the checkout through an authorized fd and
            # may not be able to traverse the controller's absolute parent path.
            report_option_dir = report_dir.relative_to(checkout.resolve())
        except ValueError:
            pass
        gate_spec = with_worker_environment(
            gate_spec,
            pytest_report_environment(
                command,
                report_option_dir,
                existing=gate_spec.env.get("PYTEST_ADDOPTS"),
                create_directory=False,
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


_PYTEST_REPORT_MAX_BYTES = 20_000_000


@contextlib.contextmanager
def _gate_open(path: Path):
    """Open a fully resolved absolute path without following any symlinks."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("gate path must be absolute and resolved")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(child, "rb") as source:
            if not stat_module.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise OSError("gate report is not a regular file")
            yield source
    finally:
        os.close(fd)


def _gate_read(path: Path, max_bytes: int) -> bytes:
    with _gate_open(path) as source:
        before = os.fstat(source.fileno())
        if before.st_size > max_bytes:
            raise ValueError("gate report exceeds byte limit")
        data = source.read(max_bytes + 1)
        after = os.fstat(source.fileno())
        if len(data) > max_bytes or len(data) != before.st_size or after.st_size != len(data):
            raise ValueError("gate report changed or exceeds byte limit")
        return data


def _pytest_cache_ids(report_dir: Path, name: str) -> tuple[str, ...]:
    """Keep executable selectors private; evidence IDs are separately scrubbed."""
    try:
        path = report_dir / "cache" / "v" / "cache" / name
        payload = json.loads(_gate_read(path, _PYTEST_REPORT_MAX_BYTES))
        if name == "lastfailed":
            if not isinstance(payload, dict) or any(value is not True for value in payload.values()):
                return ()
            payload = list(payload)
        if not isinstance(payload, list) or not all(isinstance(node, str) for node in payload):
            return ()
        return tuple(payload)
    except (OSError, ValueError):
        return ()


def _pytest_rerun_command(command: str, node_ids: tuple[str, ...]) -> str | None:
    """Rewrite only simple pytest invocations, preserving their launcher/extras.

    Unknown options and shell programs are not safe to reinterpret as selectors.
    Refusing them leaves the original gate failure intact.
    """
    if not node_ids or any(not node or "::" not in node or node.startswith("-") or "\x00" in node for node in node_ids):
        return None
    if any(char in command for char in "$`\n\r"):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        tokens = list(lexer)
        if any(token in {";", "&&", "||", "|", "&", "<", ">", "(", ")"} for token in tokens):
            return None
        args = shlex.split(command)
        index = next(i for i, token in enumerate(args) if Path(token).name in {"pytest", "py.test"})
    except (ValueError, StopIteration):
        return None
    prefix = args[:index + 1]
    launcher_args = args[:index]
    if launcher_args and Path(launcher_args[0]).name == "uv":
        if launcher_args[1:2] != ["run"]:
            return None
        launcher_args = launcher_args[2:]
        while launcher_args and launcher_args[0].startswith("-"):
            option = launcher_args.pop(0)
            if option in {"--extra", "--group", "--python"} and launcher_args:
                launcher_args.pop(0)
            elif option not in {"--no-sync", "--locked", "--frozen", "--all-extras"}:
                return None
    if launcher_args and not (
        len(launcher_args) == 2
        and Path(launcher_args[0]).name in {"python", "python3", "python3.11", "python3.12", "python3.13", "python3.14"}
        and launcher_args[1] == "-m"
    ):
        return None
    # Drop original paths, selection and parallelism; preserve execution options
    # only when their argument shape is known. Never carry --lf/--ff or -x.
    retained: list[str] = []
    index += 1
    while index < len(args):
        token = args[index]
        if token in {"-n", "--numprocesses", "--dist", "-k", "-m", "--maxfail"}:
            index += 2
            continue
        if token.startswith(("--numprocesses=", "--dist=", "--maxfail=", "-n=")) or (token.startswith("-n") and token[2:].isdigit()):
            index += 1
            continue
        if token in {"-q", "-v", "-vv", "-s", "--disable-warnings", "--strict-markers", "--strict-config"}:
            retained.append(token)
        elif token in {"-x", "--exitfirst", "--lf", "--last-failed", "--ff", "--failed-first"}:
            pass
        elif token.startswith("-"):
            return None
        index += 1
    return shlex.join([*prefix, *retained, "-n", "0", "-k", "", "-m", "", "--", *node_ids])


def pytest_report_environment(
    command: str,
    report_dir: Path,
    *,
    existing: str | None = None,
    create_directory: bool = True,
) -> dict[str, str]:
    """Configure pytest's machine reports without changing the retained output."""
    if not _is_pytest_command(command):
        return {}
    if create_directory:
        report_dir.mkdir(parents=True, exist_ok=True)
    options = (
        f"--junitxml={shlex.quote(str(report_dir / 'junit.xml'))} "
        f"--basetemp={shlex.quote(str(_gate_tmp_directory(report_dir)))} "
        "-o tmp_path_retention_policy=all "
        f"-o cache_dir={shlex.quote(str(report_dir / 'cache'))}"
    )
    return {"PYTEST_ADDOPTS": " ".join(part for part in (existing, options) if part)}


def read_pytest_result(command: str, report_dir: Path) -> TestResult | None:
    """Read counts and exact failed node IDs from pytest-owned machine files."""
    if not _is_pytest_command(command):
        return None
    junit_path = report_dir / "junit.xml"
    lastfailed_path = report_dir / "cache" / "v" / "cache" / "lastfailed"
    try:
        try:
            document = _gate_read(junit_path, _PYTEST_REPORT_MAX_BYTES)
        except ValueError:
            return TestResult(command, report_error="junit_oversize")
        root = ET.fromstring(document)
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        total = sum(_xml_count(suite, "tests") for suite in suites)
        failed = sum(_xml_count(suite, "failures") for suite in suites)
        errors = sum(_xml_count(suite, "errors") for suite in suites)
        skipped = sum(_xml_count(suite, "skipped") for suite in suites)
    except FileNotFoundError:
        return TestResult(command, report_error="junit_missing")
    except ET.ParseError:
        return TestResult(command, report_error="junit_parse_error")
    except ValueError:
        return TestResult(command, report_error="junit_invalid_counts")
    except OSError:
        return TestResult(command, report_error="junit_read_error")

    failed_tests: tuple[str, ...] = ()
    try:
        payload = json.loads(_gate_read(lastfailed_path, _PYTEST_REPORT_MAX_BYTES))
        if isinstance(payload, dict):
            failed_tests = tuple(
                redact_text(node_id)[:1000]
                for node_id, is_failed in payload.items()
                if isinstance(node_id, str) and is_failed is True
            )
    except (OSError, ValueError):
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
    environment = pytest_report_environment(command, report_dir, existing=os.environ.get("PYTEST_ADDOPTS"))
    if not environment:
        return command
    return f"PYTEST_ADDOPTS={shlex.quote(environment['PYTEST_ADDOPTS'])} {command}"


def _gate_failure_reason(tests: TestResult) -> str:
    counts = tests.counts
    if counts is None:
        return terminal_error(
            f"test gate failed (exit {tests.exit_code}); structured counts unavailable"
            + (f" ({tests.report_error})" if tests.report_error else "")
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
        executor.report_error,
        executor.timed_out,
    ) != (
        measured.exit_code,
        measured.counts,
        measured.failed_tests,
        measured.report_error,
        measured.timed_out,
    )


@contextlib.contextmanager
def shell_gate_environment() -> Iterator[dict[str, str]]:
    """Provision a private home owned by the uid executing a local shell gate.

    Unlike contained workers, these runners have no executor-provisioned UUID
    home. Keep their caches/config outside the checkout and never fall back to
    the controller's home. This bounds environment inheritance, not filesystem
    access: the local gate still executes repository code as the current uid.
    """
    with tempfile.TemporaryDirectory(prefix="worklink-gate-home-") as text:
        home = Path(text)
        paths = {
            "XDG_CONFIG_HOME": home / ".config",
            "XDG_DATA_HOME": home / ".local" / "share",
            "XDG_CACHE_HOME": home / ".cache",
        }
        for path in paths.values():
            path.mkdir(parents=True, mode=0o700)
        yield {
            "USER": "worklink",
            "LOGNAME": "worklink",
            "SHELL": "/bin/sh",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": str(home),
            **{name: str(path) for name, path in paths.items()},
        }


def _run(
    args: Sequence[str] | str, *, cwd: Path | None = None, timeout: float = 1800,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    from ..tools._shell_env import scrub_model_selection_env

    env = os.environ.copy()
    scrub_model_selection_env(env)
    if isinstance(args, str):
        # Shell syntax supports configured commands and the report env prefix.
        # Configuration must be trusted; checkout code still gets a bounded env.
        with shell_gate_environment() as gate_env:
            with subprocess.Popen(
                args, shell=True, cwd=cwd, env=gate_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text,
                start_new_session=True,
            ) as process:
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    # Kill the whole gate, not just the shell that launched it.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise
                return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
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
