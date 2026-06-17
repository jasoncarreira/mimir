"""Portable Worklink worker entrypoint for git-handoff compute substrates."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

from .backends import BackendRegistry, WorkOrder, WorklinkConfig
from .compute import ComputeLaunchError, ComputeResult, LocalSubprocessComputeBackend, WorkSpec
from .evidence import EvidenceValidation, observe_evidence


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
        if _backend_completed(raw.backend_status):
            _check(runner(["git", "-C", str(repo), "add", "-A"]), "git add")
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
        return validation
    except Exception as exc:
        failed = _failed_worker_evidence(payload, repo, started, str(exc), runner=runner)
        _write_worker_evidence(payload.evidence_path, failed)
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
    if not spec.base_ref.startswith("origin/"):
        # Materialize the fetched base under a local ref so both the checkout
        # below and the later `base...HEAD` diff can name it. Git allows slashes
        # in branch names, so this works for simple ("main") and long-running
        # feature ("integration/worklink") bases alike. origin/-prefixed bases
        # are skipped: they resolve via the remote-tracking ref instead.
        _check(runner(["git", "-C", str(repo), "branch", "-f", spec.base_ref, "FETCH_HEAD"]), "git branch")
    checkout_ref = f"origin/{remote_ref}" if spec.base_ref.startswith("origin/") else spec.base_ref
    _check(runner(["git", "-C", str(repo), "checkout", "-B", spec.branch, checkout_ref]), "git checkout")
    return repo


def _remote_ref(base_ref: str) -> str:
    return base_ref.removeprefix("origin/")


def _backend_completed(status: str) -> bool:
    return status.lower().strip() in {"completed", "success", "succeeded", "ok"}


def _failed_worker_evidence(
    payload: WorkerPayload,
    repo: Path,
    started: datetime,
    reason: str,
    *,
    runner: Any,
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
    return replace(validation, status="failed", review_ready=False, reasons=(*validation.reasons, "worker_error"), evidence=evidence)


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
