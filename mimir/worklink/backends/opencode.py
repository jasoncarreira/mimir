"""OpenCode CLI Worklink backend (chainlink #830)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import re
from typing import Sequence

from ...config import model_spec_at_call_time
from ...opencode_config import opencode_model_from_agent_spec, resolve_opencode_invocation
from ..compute import ComputeResult, WorkSpec
from .base import Caps, RawResult, WorkOrder, blocked_reason_from_output


DEFAULT_BASH_ALLOWLIST: tuple[str, ...] = ("git *", "uv *")
_INJECTED_FLAGS: tuple[str, ...] = ("-m", "--model", "--dir")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenCodeBackend:
    """Adapter for ``opencode run`` Worklink jobs.

    The provider-agnostic coding substrate from the #830 pivot: opencode
    routes to whichever model provider its own config selects, so per-leaf
    worklink no longer cares which subscription executes the build. Runs
    non-interactively in the leaf worktree via ``opencode run --dir``.
    """

    bin: str = "opencode"
    extra_args: Sequence[str] = field(default_factory=tuple)
    bash_allowlist: Sequence[str] = field(default_factory=lambda: DEFAULT_BASH_ALLOWLIST)
    name: str = "opencode"

    def __post_init__(self) -> None:
        validate_extra_args(self.extra_args)

    def capabilities(self) -> Caps:
        return Caps(
            tool_category="coding-cli",
            persistent_sessions=False,
            json_output=False,
            native_pr_creation=False,
            worktree_safe=True,
            quota_pool="opencode",
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
        args = list(self.extra_args)
        env = dict(order.env)
        model_home = Path(env["MIMIR_HOME"]) if env.get("MIMIR_HOME") else None
        dotenv_model_spec = model_spec_at_call_time(model_home)
        configured_model_spec = env.get("MIMIR_MODEL_SPEC", dotenv_model_spec)
        resolution_env = {**os.environ, **env}
        resolution_env.setdefault("MIMIR_MODEL_SPEC", configured_model_spec)
        invocation = resolve_opencode_invocation(env=resolution_env)
        args.extend(("-m", invocation.model))
        configured_model = opencode_model_from_agent_spec(configured_model_spec)
        model_diverged = invocation.model != configured_model
        if model_diverged:
            log.warning(
                "Worklink OpenCode model %s differs from configured agent model %s",
                invocation.model,
                configured_model,
            )
        if invocation.config_path.exists() or "OPENCODE_CONFIG" in env:
            env["OPENCODE_CONFIG"] = str(invocation.config_path)
        for key in invocation.pass_env:
            if key in os.environ:
                env[key] = os.environ[key]
        for key in invocation.remove_env:
            # WorkSpec overrides an allowlisted parent environment; an empty
            # value is required here because omission would inherit it again.
            env[key] = ""
        env["OPENCODE_PERMISSION"] = _permission_override(self.bash_allowlist)
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
            env=env,
            backend_config={
                "bin": self.bin,
                "args": args,
                "bash_allowlist": list(self.bash_allowlist),
                "model": invocation.model,
                "configured_model": configured_model,
                "model_diverged": model_diverged,
                "model_source": invocation.model_source,
            },
            local_worktree=order.worktree,
            local_argv=_local_argv(self.bin, args, order.worktree, prompt),
        )
    async def interpret(self, order: WorkOrder, result: object) -> RawResult:
        if not isinstance(result, ComputeResult):
            raise TypeError("OpenCodeBackend.interpret expects ComputeResult")
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
                output_overflow=False,
            )
            return RawResult(-1, transcript_path, "backend_error", result.launch_error)

        blocked_reason = blocked_reason_from_output(result.stdout, result.stderr)
        status = "output_overflow" if result.output_overflow else (
            "blocked" if blocked_reason else (
                "timeout" if result.timed_out else _status_from_output(
                    result.exit_code, result.stdout, result.stderr
                )
            )
        )
        error = (
            "backend output exceeded configured Worklink limit"
            if result.output_overflow
            else blocked_reason or _error_from_status(
                status, result.stdout, result.stderr, result.command
            )
        )
        _write_transcript(
            transcript_path,
            command=list(result.command),
            exit_code=result.exit_code,
            status=status,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            output_overflow=result.output_overflow,
        )
        return RawResult(
            result.exit_code,
            transcript_path,
            status,
            error,
            blocked_reason,
            output_overflow=result.output_overflow,
        )


def validate_extra_args(args: Sequence[str]) -> None:
    for arg in args:
        flag = arg.split("=", 1)[0]
        if flag in _INJECTED_FLAGS or (
            flag == arg and arg.startswith("-m") and not arg.startswith("--") and arg != "-m"
        ):
            raise ValueError(
                f"worklink opencode args cannot contain {arg!r}: the backend injects "
                f"{flag!r} itself; remove it from backends.opencode.args"
            )


def _prompt_for_order(order: WorkOrder) -> str:
    return order.prompt if order.rules is None else f"{order.rules.rstrip()}\n\n{order.prompt}"


def _local_argv(bin_name: str, args: Sequence[str], worktree: Path, prompt: str) -> tuple[str, ...]:
    # ``--`` so a prompt that begins with ``-`` is never parsed as a flag.
    return (bin_name, "run", "--dir", str(worktree), *args, "--", prompt)


def _permission_override(bash_allowlist: Sequence[str]) -> str:
    # OpenCode evaluates the last matching rule. Keep the deny first and append
    # only explicit operator grants so unmatched commands cannot prompt or run.
    bash = {"*": "deny"}
    bash.update((pattern, "allow") for pattern in bash_allowlist)
    return json.dumps({
        "external_directory": {"/**": "deny"},
        "bash": bash,
    }, separators=(",", ":"))


def _transcript_path(transcript_root: Path | None, issue_id: int) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = transcript_root or _default_transcript_root()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"opencode-{issue_id}-{stamp}.json"


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
    output_overflow: bool,
) -> None:
    payload = {
        "backend": "opencode",
        "command": list(command),
        "exit_code": exit_code,
        "status": status,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "output_overflow": output_overflow,
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
    if re.search(r"\b(auth|authentication|oauth|login|credential|api key|unauthorized|permission)\b", auth_text):
        return "auth_error"
    return "failed"


def _error_from_status(
    status: str,
    stdout: str,
    stderr: str,
    command: Sequence[str] = (),
) -> str | None:
    if status == "success":
        return None
    detail = (stderr.strip() or stdout.strip()).splitlines()
    message = detail[-1] if detail else status
    if status == "timeout":
        return f"opencode execution timed out: {message}"
    if status == "auth_error":
        provider = _provider_from_command(command)
        return f"OpenCode provider {provider!r} authentication failed: {message}"
    return message


def _provider_from_command(command: Sequence[str]) -> str:
    for index in range(len(command) - 2, -1, -1):
        if command[index] in {"-m", "--model"}:
            return command[index + 1].partition("/")[0] or "unknown"
    return "unknown"
