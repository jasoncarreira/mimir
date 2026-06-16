"""Codex CLI Worklink backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Sequence

from ..compute import ComputeResult, WorkSpec
from .base import Caps, RawResult, WorkOrder, blocked_reason_from_output


@dataclass(frozen=True)
class CodexBackend:
    """Adapter for ``codex exec`` Worklink jobs."""

    bin: str = "codex"
    extra_args: Sequence[str] = field(default_factory=tuple)
    name: str = "codex"

    def capabilities(self) -> Caps:
        return Caps(
            tool_category="coding-cli",
            persistent_sessions=False,
            json_output=True,
            native_pr_creation=False,
            worktree_safe=True,
            quota_pool="codex-subscription",
        )

    def work_spec(
        self,
        order: WorkOrder,
        *,
        attempt: int,
        repo_url: str,
        base_ref: str,
        branch: str,
        test_command: str,
    ) -> WorkSpec:
        prompt = _prompt_for_order(order)
        args = list(self.extra_args) or ["exec", "--json"]
        return WorkSpec(
            issue_id=order.issue_id,
            attempt=attempt,
            repo_url=repo_url,
            base_ref=base_ref,
            branch=branch,
            prompt=order.prompt,
            rules=order.rules,
            test_command=test_command,
            backend=self.name,
            timeout_s=order.timeout_s,
            env=order.env,
            backend_config={"bin": self.bin, "args": args},
            local_worktree=order.worktree,
            local_argv=_local_argv(self.bin, args, order.worktree, prompt),
        )

    async def interpret(self, order: WorkOrder, result: object) -> RawResult:
        if not isinstance(result, ComputeResult):
            raise TypeError("CodexBackend.interpret expects ComputeResult")
        transcript_path = _transcript_path(order.transcript_root, order.issue_id)
        if result.launch_error:
            _write_transcript(
                transcript_path,
                command=list(result.command),
                exit_code=None,
                status="backend_error",
                stdout=result.stdout,
                stderr=result.stderr,
                timed_out=False,
            )
            return RawResult(-1, transcript_path, "backend_error", result.launch_error)

        blocked_reason = blocked_reason_from_output(result.stdout, result.stderr)
        status = "blocked" if blocked_reason else (
            "timeout" if result.timed_out else _status_from_output(
                result.exit_code, result.stdout, result.stderr
            )
        )
        error = blocked_reason or _error_from_status(status, result.stdout, result.stderr)
        _write_transcript(
            transcript_path,
            command=list(result.command),
            exit_code=result.exit_code,
            status=status,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
        )
        return RawResult(result.exit_code, transcript_path, status, error, blocked_reason)



def _prompt_for_order(order: WorkOrder) -> str:
    return order.prompt if order.rules is None else f"{order.rules.rstrip()}\n\n{order.prompt}"


def _local_argv(bin_name: str, args: Sequence[str], worktree: Path, prompt: str) -> tuple[str, ...]:
    cd_args = ("-C", str(worktree))
    if args and args[0] == "exec":
        return (bin_name, "exec", *cd_args, *args[1:], prompt)
    return (bin_name, *args, *cd_args, prompt)


def _transcript_path(transcript_root: Path | None, issue_id: int) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = transcript_root or _default_transcript_root()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"codex-{issue_id}-{stamp}.json"


def _default_transcript_root() -> Path:
    import os

    home = Path(os.environ.get("MIMIR_HOME", ".")).resolve()
    return home / "state" / "worklink" / "transcripts"


def _write_transcript(
    path: Path,
    *,
    command: Sequence[str],
    exit_code: int | None,
    status: str,
    stdout: str,
    stderr: str,
    timed_out: bool,
) -> None:
    payload = {
        "backend": "codex",
        "command": list(command),
        "exit_code": exit_code,
        "status": status,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _status_from_output(exit_code: int, stdout: str, stderr: str) -> str:
    combined = f"{stdout}\n{stderr}".lower()
    if exit_code == 0:
        return "success"
    if "429" in combined or "quota" in combined or "rate limit" in combined:
        return "quota_exhausted"
    auth_text = stderr.lower()
    if re.search(r"\b(auth|authentication|oauth|login|credential|api key|permission)\b", auth_text):
        return "auth_error"
    return "failed"


def _error_from_status(status: str, stdout: str, stderr: str) -> str | None:
    if status == "success":
        return None
    detail = (stderr.strip() or stdout.strip()).splitlines()
    message = detail[-1] if detail else status
    if status == "timeout":
        return f"codex execution timed out: {message}"
    return message
