"""Portable Worklink worker entrypoint for git-handoff compute substrates."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence

from .backends import BackendRegistry, WorkOrder, WorklinkConfig
from .compute import ComputeLaunchError, ComputeResult, LocalSubprocessComputeBackend, WorkSpec
from .evidence import EvidenceValidation, _summarize_test_output, observe_evidence


@dataclass(frozen=True)
class WorkerPayload:
    """Serialized git-handoff work unit executed by a Worklink worker."""

    spec: WorkSpec
    repo_dir: Path
    evidence_path: Path
    transcript_root: Path | None = None
    safe_env: Mapping[str, str] = field(default_factory=dict)


def payload_to_json(payload: WorkerPayload) -> dict[str, Any]:
    return {
        "spec": _spec_to_json(payload.spec),
        "repo_dir": str(payload.repo_dir),
        "evidence_path": str(payload.evidence_path),
        "transcript_root": str(payload.transcript_root) if payload.transcript_root else None,
        "safe_env": dict(payload.safe_env),
    }


def payload_from_json(data: Mapping[str, Any]) -> WorkerPayload:
    if not isinstance(data, Mapping):
        raise ValueError("worker payload must be a JSON object")
    spec_data = data.get("spec")
    if not isinstance(spec_data, Mapping):
        raise ValueError("worker payload missing spec object")
    return WorkerPayload(
        spec=_spec_from_json(spec_data),
        repo_dir=Path(str(data.get("repo_dir") or ".")).resolve(),
        evidence_path=Path(str(data.get("evidence_path") or "evidence.json")).resolve(),
        transcript_root=(Path(str(data["transcript_root"])).resolve() if data.get("transcript_root") else None),
        safe_env=_string_mapping(data.get("safe_env") or {}),
    )


async def run_worker_payload(
    payload: WorkerPayload,
    *,
    registry: BackendRegistry | None = None,
    runner: Any | None = None,
) -> EvidenceValidation:
    """Execute one Worklink worker payload and write observed evidence JSON.

    The worker's contract is git-shaped rather than bind-mount-shaped: clone or
    fetch the repo, check out the requested base ref, create/reset the attempt
    branch, run the selected tool backend via a local compute substrate, run the
    requested tests, push the branch, and persist an evidence document. The
    orchestrator must still re-derive evidence for non-local substrates before
    advancing Chainlink state; worker evidence is a handoff artifact, not a
    trust boundary.
    """

    registry = registry or BackendRegistry(WorklinkConfig())
    runner = runner or _run
    spec = payload.spec
    repo = _prepare_repo(payload, runner=runner)
    # Honor the backend config the orchestrator already resolved (bin + args,
    # e.g. codex `--sandbox danger-full-access`). On a non-shared substrate
    # (docker-sibling / ecs) the worker has no worklink.yaml of its own, so
    # without this it falls back to the backend's default args and runs codex
    # sandboxed → no file writes → empty diff → not review-ready → no push.
    if spec.backend_config:
        backend = BackendRegistry(
            WorklinkConfig(backend_settings={spec.backend: dict(spec.backend_config)})
        ).get(spec.backend)
    else:
        backend = registry.get(spec.backend)
    order = WorkOrder(
        issue_id=spec.issue_id,
        worktree=repo,
        prompt=spec.prompt,
        rules=spec.rules,
        timeout_s=spec.timeout_s,
        env={**dict(payload.safe_env), **dict(spec.env)},
        transcript_root=payload.transcript_root,
    )
    local_spec = backend.work_spec(
        order,
        attempt=spec.attempt,
        repo_url=spec.repo_url,
        base_ref=spec.base_ref,
        branch=spec.branch,
        test_command=spec.test_command,
    )
    started = datetime.now(UTC)
    compute = LocalSubprocessComputeBackend()
    handle = None
    compute_result: ComputeResult | None = None
    transcript_path: Path | None = None
    try:
        try:
            handle = await compute.launch(local_spec)
            compute_result = await compute.wait(handle, local_spec.timeout_s)
        except ComputeLaunchError as exc:
            compute_result = ComputeResult(exit_code=-1, stdout="", stderr=str(exc), launch_error=str(exc))
        finally:
            if handle is not None:
                await compute.cleanup(handle)
        raw = await backend.interpret(order, compute_result)
        transcript_path = raw.transcript_path
        if _backend_completed(raw.backend_status):
            _check(runner(["git", "-C", str(repo), "add", "-A"]), "git add")
            staged = runner(["git", "-C", str(repo), "diff", "--cached", "--name-only"])
            if staged.returncode != 0:
                raise RuntimeError((staged.stderr or staged.stdout).strip() or "git staged diff failed")
            if not staged.stdout.strip():
                validation = _failed_worker_evidence(
                    payload,
                    repo,
                    started,
                    "backend produced no changes",
                    runner=runner,
                    extra_reasons=("backend_produced_no_changes",),
                )
                _write_worker_evidence(payload.evidence_path, validation)
                _emit_failure_diagnostics(payload, validation, compute_result, transcript_path)
                return validation
            commit = runner([
                "git",
                "-C",
                str(repo),
                "commit",
                "-m",
                f"worklink: issue #{spec.issue_id}",
            ])
            if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
                raise RuntimeError((commit.stderr or commit.stdout).strip() or "git commit failed")
        validation = observe_evidence(
            issue=spec.issue_id,
            attempt=spec.attempt,
            backend=spec.backend,
            branch=spec.branch,
            worktree=repo,
            started_at=started,
            base_ref=spec.base_ref,
            backend_status=raw.backend_status,
            test_command=spec.test_command,
            transcript=str(raw.transcript_path) if raw.transcript_path else None,
            blocked_reason=raw.blocked_reason,
            runner=runner,
        )
        _write_worker_evidence(payload.evidence_path, validation)
        if validation.review_ready:
            _check(runner(["git", "-C", str(repo), "push", "origin", f"HEAD:{spec.branch}"]), "git push")
        else:
            _emit_gate_test_failure(payload, validation)
            _emit_failure_diagnostics(payload, validation, compute_result, transcript_path)
        return validation
    except Exception as exc:
        failed = _failed_worker_evidence(payload, repo, started, str(exc), runner=runner)
        _write_worker_evidence(payload.evidence_path, failed)
        _emit_failure_diagnostics(payload, failed, compute_result, transcript_path)
        return failed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a portable Worklink worker payload.")
    parser.add_argument("payload", type=Path, nargs="?", help="Path to worker payload JSON.")
    parser.add_argument("--payload-json", default=None, help="Inline worker payload JSON.")
    args = parser.parse_args(argv)
    if args.payload_json is not None:
        payload_data = json.loads(args.payload_json)
    elif args.payload is not None:
        payload_data = json.loads(args.payload.read_text(encoding="utf-8"))
    else:
        parser.error("payload path or --payload-json is required")
    payload = payload_from_json(payload_data)
    if payload.spec.test_only:
        # Exit code IS the result the controller reads (chainlink #538).
        return asyncio.run(_run_test_only(payload))
    validation = asyncio.run(run_worker_payload(payload))
    print(json.dumps({"status": validation.status, "review_ready": validation.review_ready, "reasons": list(validation.reasons)}))
    return 0 if validation.status in {"completed", "failed", "blocked"} else 1


async def _run_test_only(payload: WorkerPayload, *, runner: Any | None = None) -> int:
    """Test-only worker run (chainlink #538): clone + check out the already-pushed
    ``branch`` and run ``test_command``, returning the test's exit code.

    No backend, no commit, no push — this is the controller's REMOTE test
    re-derivation, run in a fresh sandboxed compute job so the test result is the
    container's exit code (the standard ``ComputeResult`` channel) rather than a
    worker self-report. A setup failure (clone/fetch/checkout) returns a distinct
    non-zero sentinel; either way a non-zero exit fails the review gate closed.
    """
    runner = runner or _run
    spec = payload.spec
    repo = payload.repo_dir
    repo.parent.mkdir(parents=True, exist_ok=True)
    branch_refspec = f"+{spec.branch}:refs/remotes/origin/{spec.branch}"
    try:
        if not (repo / ".git").exists():
            _check(runner(["git", "clone", spec.repo_url, str(repo)]), "git clone")
        _check(runner(["git", "-C", str(repo), "fetch", "origin", branch_refspec]), "git fetch")
        _check(
            runner(["git", "-C", str(repo), "checkout", "--detach", f"origin/{spec.branch}"]),
            "git checkout",
        )
    except Exception as exc:
        print(json.dumps({"test_only": True, "setup_error": str(exc)}))
        return 70  # setup failure — distinct from a test pass(0)/fail(non-zero)
    test = runner(spec.test_command, cwd=repo)
    if test.returncode != 0:
        # chainlink #815: this fresh sandboxed job is the REMOTE trusted gate —
        # its pytest output would otherwise die with the container, leaving the
        # next attempt only an exit code (the original blind-retry loop).
        _print_tests_tail(
            spec.test_command,
            test.returncode,
            _summarize_test_output(test),
            _spec_secret_values(spec),
        )
    print(json.dumps({
        "test_only": True,
        "branch": spec.branch,
        "test_command": spec.test_command,
        "exit_code": test.returncode,
    }))
    return test.returncode


def _prepare_repo(payload: WorkerPayload, *, runner: Any) -> Path:
    spec = payload.spec
    repo = payload.repo_dir
    repo.parent.mkdir(parents=True, exist_ok=True)
    remote_ref = _remote_ref(spec.base_ref)
    if (repo / ".git").exists():
        _check(runner(["git", "-C", str(repo), "fetch", "origin", remote_ref]), "git fetch")
    else:
        _check(runner(["git", "clone", spec.repo_url, str(repo)]), "git clone")
        _check(runner(["git", "-C", str(repo), "fetch", "origin", remote_ref]), "git fetch")
    # Check out the attempt branch off the fetched base FIRST. On a fresh clone
    # (the non-shared docker-sibling / ecs path) the repo's HEAD is on base_ref
    # (e.g. "main"); materializing the local base ref before moving off it fails
    # with "cannot force update the branch '<base>' used by worktree". The local
    # worktree path never hit this because its checkout isn't on base_ref.
    checkout_ref = f"origin/{remote_ref}" if spec.base_ref.startswith("origin/") else "FETCH_HEAD"
    _check(runner(["git", "-C", str(repo), "checkout", "-B", spec.branch, checkout_ref]), "git checkout")
    if not spec.base_ref.startswith("origin/"):
        # Materialize the fetched base under a local ref so the later
        # `base...HEAD` diff can name it. Safe now that the attempt branch — not
        # base_ref — is the checked-out branch. Git allows slashes, so simple
        # ("main") and long-running ("integration/worklink") bases both work.
        # origin/-prefixed bases resolve via the remote-tracking ref instead.
        _check(runner(["git", "-C", str(repo), "branch", "-f", spec.base_ref, "FETCH_HEAD"]), "git branch")
    return repo


def _remote_ref(base_ref: str) -> str:
    return base_ref.removeprefix("origin/")


def _backend_completed(status: str) -> bool:
    return status.lower().strip() in {"completed", "success", "succeeded", "ok"}


TESTS_TAIL_BEGIN = "WORKLINK_TESTS_TAIL_BEGIN"
TESTS_TAIL_END = "WORKLINK_TESTS_TAIL_END"
_TESTS_TAIL_MAX_CHARS = 6000


def _spec_secret_values(spec: WorkSpec) -> list[str]:
    return sorted(
        {value for value in (*spec.env.values(), *spec.creds_ref.values()) if len(value) >= 8},
        key=len,
        reverse=True,
    )


def _print_tests_tail(cmd: str | None, exit_code: int | None, body: str, secret_values: Sequence[str]) -> None:
    body = (body or "(no output captured)").strip()[-_TESTS_TAIL_MAX_CHARS:]
    print(TESTS_TAIL_BEGIN)
    print(f"command: {cmd}")
    print(f"exit: {exit_code}")
    print(_redact_diagnostics(body, secret_values))
    print(TESTS_TAIL_END, flush=True)


def _emit_gate_test_failure(payload: WorkerPayload, validation: EvidenceValidation) -> None:
    """Print the failed gate-test output as a delimited stdout section (chainlink
    #815). Stdout is the only worker artifact that survives a docker-sibling
    container; the controller parses this section out of the broker-wait output
    and feeds it to the next attempt's work order, so retries act on the actual
    failures instead of a bare ``tests_failed``."""
    tests = validation.evidence.tests
    if tests is None or not tests.observed or not tests.exit_code:
        return
    _print_tests_tail(tests.cmd, tests.exit_code, tests.summary or "", _spec_secret_values(payload.spec))


def extract_gate_test_tail(stdout: str | None) -> str | None:
    """Controller-side parser for the section ``_print_tests_tail`` emits.
    Tolerant of a missing/unterminated section; bounded to the emit cap."""
    if not stdout or TESTS_TAIL_BEGIN not in stdout:
        return None
    section = stdout.split(TESTS_TAIL_BEGIN, 1)[1]
    section = section.split(TESTS_TAIL_END, 1)[0].strip()
    return section[:_TESTS_TAIL_MAX_CHARS] or None


def gate_failure_detail(validation: EvidenceValidation, stdout: str | None) -> str | None:
    """Best available gate-failure detail for retry feedback (chainlink #815).

    Prefers the WORKLINK_TESTS_TAIL section from the implementation worker's
    stdout (local in-worker gate). When the worker's internal gate passed but
    the controller's fresh sandboxed test job failed (the remote trusted gate),
    the tail lives in the folded evidence's TestResult summary instead."""
    tail = extract_gate_test_tail(stdout)
    if tail:
        return tail
    tests = validation.evidence.tests
    if tests is not None and tests.observed and tests.exit_code and tests.summary:
        return tests.summary[:_TESTS_TAIL_MAX_CHARS]
    return None


_DIAG_BEGIN = "WORKLINK_WORKER_DIAG_BEGIN"
_DIAG_END = "WORKLINK_WORKER_DIAG_END"
_DIAG_STDERR_TAIL_LINES = 50
_DIAG_STDOUT_TAIL_LINES = 30
_DIAG_TRANSCRIPT_TAIL_LINES = 30
_DIAG_TOTAL_MAX_CHARS = 8000

_DIAG_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)\S.*"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(bearer)\s+\S+"), r"\1 <redacted>"),
    (
        re.compile(r"(?i)([\"']?\w*(?:api[_-]?key|access[_-]?token|token|secret|password|passwd)[\"']?\s*[:=]\s*)\S+"),
        r"\1<redacted>",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "<redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "<redacted>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"), "<redacted>"),
)


def _tail_lines(text: str, limit: int) -> str:
    return "\n".join(text.splitlines()[-limit:])


def _redact_diagnostics(text: str, secret_values: Sequence[str]) -> str:
    for value in secret_values:
        text = text.replace(value, "<redacted>")
    for pattern, replacement in _DIAG_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _emit_failure_diagnostics(
    payload: WorkerPayload,
    validation: EvidenceValidation,
    compute_result: ComputeResult | None,
    transcript_path: Path | None,
) -> None:
    """Print a bounded, redacted diagnostic block on worker stdout (chainlink #809).

    On non-shared substrates (docker-sibling / ecs) stdout is the only worker
    artifact that outlives the reaped container — the controller's transcript
    wrapper records the broker-wait output, so this block is the durable record
    of WHY the backend failed. Additive only: evidence JSON and the final status
    line are unchanged, and review-ready runs print nothing.
    """
    spec = payload.spec
    # Values injected via the work order env / creds are credentials by
    # construction; safe_env is the sanitized set (PATH, HOME, ...) and would
    # mangle tracebacks if redacted.
    secret_values = sorted(
        {value for value in (*spec.env.values(), *spec.creds_ref.values()) if len(value) >= 8},
        key=len,
        reverse=True,
    )
    exit_code = compute_result.exit_code if compute_result is not None else "n/a"
    lines = [
        f"issue=#{spec.issue_id} attempt={spec.attempt} backend={spec.backend} "
        f"status={validation.status} backend_exit={exit_code} "
        f"reasons={','.join(validation.reasons) or '-'}"
    ]
    if compute_result is not None:
        lines.append(f"--- backend stderr (last {_DIAG_STDERR_TAIL_LINES} lines) ---")
        lines.append(_tail_lines(compute_result.stderr, _DIAG_STDERR_TAIL_LINES) or "(empty)")
        lines.append(f"--- backend stdout (last {_DIAG_STDOUT_TAIL_LINES} lines) ---")
        lines.append(_tail_lines(compute_result.stdout, _DIAG_STDOUT_TAIL_LINES) or "(empty)")
    if transcript_path is not None:
        try:
            transcript_text = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            transcript_text = f"(transcript unreadable: {exc})"
        lines.append(f"--- transcript tail (last {_DIAG_TRANSCRIPT_TAIL_LINES} lines of {transcript_path}) ---")
        lines.append(_tail_lines(transcript_text, _DIAG_TRANSCRIPT_TAIL_LINES) or "(empty)")
    body = _redact_diagnostics("\n".join(lines), secret_values)
    if len(body) > _DIAG_TOTAL_MAX_CHARS:
        body = "(head truncated)\n" + body[-_DIAG_TOTAL_MAX_CHARS:]
    print(_DIAG_BEGIN)
    print(body)
    print(_DIAG_END, flush=True)


def _failed_worker_evidence(
    payload: WorkerPayload,
    repo: Path,
    started: datetime,
    reason: str,
    *,
    runner: Any,
    extra_reasons: tuple[str, ...] = (),
) -> EvidenceValidation:
    from dataclasses import replace

    validation = observe_evidence(
        issue=payload.spec.issue_id,
        attempt=payload.spec.attempt,
        backend=payload.spec.backend,
        branch=payload.spec.branch,
        worktree=repo,
        started_at=started,
        base_ref=payload.spec.base_ref,
        backend_status="failed",
        test_command=None,
        transcript=None,
        runner=runner,
    )
    evidence = replace(validation.evidence, status="failed", blocked_reason=reason)
    return replace(
        validation,
        status="failed",
        review_ready=False,
        reasons=(*validation.reasons, "worker_error", *extra_reasons),
        evidence=evidence,
    )


def _write_worker_evidence(path: Path, validation: EvidenceValidation) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_evidence_json(validation.evidence), indent=2, sort_keys=True), encoding="utf-8")


def _evidence_json(evidence: Any) -> dict[str, Any]:
    data = asdict(evidence)
    data["commands"] = [asdict(command) for command in evidence.commands]
    data["tests"] = asdict(evidence.tests) if evidence.tests else None
    return data


def _spec_to_json(spec: WorkSpec) -> dict[str, Any]:
    data = asdict(spec)
    data["local_worktree"] = str(spec.local_worktree) if spec.local_worktree else None
    data["local_argv"] = list(spec.local_argv) if spec.local_argv else None
    return data


def _spec_from_json(data: Mapping[str, Any]) -> WorkSpec:
    return WorkSpec(
        issue_id=int(data["issue_id"]),
        attempt=int(data["attempt"]),
        repo_url=str(data["repo_url"]),
        base_ref=str(data["base_ref"]),
        branch=str(data["branch"]),
        prompt=str(data["prompt"]),
        rules=str(data["rules"]) if data.get("rules") is not None else None,
        test_command=str(data["test_command"]),
        backend=str(data["backend"]),
        timeout_s=int(data["timeout_s"]),
        creds_ref=_string_mapping(data.get("creds_ref") or {}),
        env=_string_mapping(data.get("env") or {}),
        backend_config=dict(data.get("backend_config") or {}),
        local_worktree=(Path(str(data["local_worktree"])) if data.get("local_worktree") else None),
        local_argv=tuple(str(arg) for arg in data.get("local_argv") or ()) or None,
        test_only=bool(data.get("test_only", False)),
    )


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("expected mapping")
    return {str(key): str(item) for key, item in value.items()}


def _check(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed: {(result.stderr or result.stdout).strip()}")


def _run(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    if isinstance(args, str):
        return subprocess.run(args, shell=True, cwd=cwd, capture_output=True, text=True, check=False)
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=False)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
