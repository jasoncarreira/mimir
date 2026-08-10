"""OpenCode CLI Worklink backend (chainlink #830)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Awaitable, Callable, Sequence

from ...config import model_spec_at_call_time
from ...opencode_config import (
    opencode_model_from_agent_spec,
    opencode_worker_documents,
    resolve_opencode_invocation,
)
from ..compute import ComputeResult, WorkSpec
from .base import (
    Caps,
    CheckoutShape,
    RawResult,
    WorkOrder,
    blocked_reason_from_output,
    last_nonempty_line,
)


DEFAULT_BASH_ALLOWLIST: tuple[str, ...] = ("git *", "uv *")
DERIVABLE_TEST_RUNNERS: frozenset[str] = frozenset({
    "bun",
    "cargo",
    "go",
    "gradle",
    "gradlew",
    "mvn",
    "mvnw",
    "npm",
    "pnpm",
    "uv",
    "yarn",
})
_INJECTED_FLAGS: tuple[str, ...] = ("-m", "--model", "--dir", "--")
# Five total attempts wait 0.1 + 0.2 + 0.4 + 0.8 = 1.5 seconds. That
# comfortably spans the observed sub-second SQLite startup lock while bounding
# a genuinely stuck session store to a short delay.
STARTUP_CONTENTION_MAX_ATTEMPTS = 5
STARTUP_CONTENTION_INITIAL_BACKOFF_S = 0.1
STARTUP_CONTENTION_WINDOW_S = 5.0
STARTUP_CONTENTION_RESOURCE = "opencode_sqlite_session_store"
STARTUP_CONTENTION_EXHAUSTED_REASON = "opencode_startup_sqlite_contention_exhausted"
_SQLITE_CONTENTION_PATTERNS = (
    re.compile(r"\bdatabase(?: table)? is locked\b", re.IGNORECASE),
    re.compile(r"\bdatabase is busy\b", re.IGNORECASE),
    re.compile(r"\bSQLITE_BUSY\b", re.IGNORECASE),
)
log = logging.getLogger(__name__)

EventLogger = Callable[..., None]
Sleeper = Callable[[float], Awaitable[None]]
CheckoutSnapshot = Callable[[], object]
Clock = Callable[[], float]


@dataclass(frozen=True)
class WorkerProjection:
    path: str
    document: bytes

    def __post_init__(self) -> None:
        if self.path not in {
            ".config/opencode/opencode.json",
            ".local/share/opencode/auth.json",
        }:
            raise ValueError("worker projection destination is not permitted")
        if len(self.document) > 1024 * 1024:
            raise ValueError("worker projection exceeds size limit")
        if not isinstance(json.loads(self.document), dict):
            raise ValueError("worker projection must be a JSON object")


@dataclass(frozen=True)
class OpenCodeBackend:
    """Adapter for ``opencode run`` Worklink jobs.

    The provider-agnostic coding substrate from the #830 pivot: opencode
    routes to whichever model provider its own config selects, so per-leaf
    worklink no longer cares which subscription executes the build. Runs
    non-interactively in the leaf checkout via ``opencode run --dir``.
    """

    bin: str = "opencode"
    extra_args: Sequence[str] = field(default_factory=tuple)
    bash_allowlist: Sequence[str] = field(default_factory=lambda: DEFAULT_BASH_ALLOWLIST)
    name: str = "opencode"
    checkout_shape: CheckoutShape = CheckoutShape.ISOLATED_CLONE

    def __post_init__(self) -> None:
        validate_extra_args(self.extra_args)

    def capabilities(self) -> Caps:
        return Caps(
            tool_category="coding-cli",
            persistent_sessions=False,
            json_output=False,
            native_pr_creation=False,
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
        # Model selection is consumed above by the backend. Do not expose it to
        # the executor or repository-controlled test processes it launches.
        from ...tools._shell_env import scrub_model_selection_env

        scrub_model_selection_env(env)
        env["OPENCODE_PERMISSION"] = _permission_override(self.bash_allowlist)
        backend_config: dict[str, object] = {
            "bin": self.bin,
            "args": args,
            "bash_allowlist": list(self.bash_allowlist),
            "model": invocation.model,
            "configured_model": configured_model,
            "model_diverged": model_diverged,
            "model_source": invocation.model_source,
        }
        if _coding_enabled():
            config_document, auth_document = opencode_worker_documents(invocation)
            projections = [
                WorkerProjection(".config/opencode/opencode.json", config_document)
            ]
            if auth_document is not None:
                projections.append(
                    WorkerProjection(".local/share/opencode/auth.json", auth_document)
                )
            env = {
                key: resolution_env[key]
                for key in invocation.pass_env
                if key in resolution_env
            }
            env["OPENCODE_PERMISSION"] = _permission_override(self.bash_allowlist)
            backend_config["worker_projections"] = projections
            backend_config["pass_env"] = invocation.pass_env
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
            backend_config=backend_config,
            local_checkout=order.checkout,
            local_argv=_local_argv(self.bin, args, order.checkout, prompt),
        )

    async def invoke_with_startup_retry(
        self,
        invoke: Callable[[], Awaitable[ComputeResult]],
        *,
        issue_id: int,
        checkout_snapshot: CheckoutSnapshot,
        event_logger: EventLogger | None = None,
        max_attempts: int = STARTUP_CONTENTION_MAX_ATTEMPTS,
        initial_backoff_s: float = STARTUP_CONTENTION_INITIAL_BACKOFF_S,
        startup_window_s: float = STARTUP_CONTENTION_WINDOW_S,
        sleeper: Sleeper = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> ComputeResult:
        """Retry transient SQLite contention only before the run can mutate work."""
        max_attempts = max(1, max_attempts)
        initial_backoff_s = max(0.0, initial_backoff_s)
        startup_window_s = max(0.0, startup_window_s)
        initial_checkout = checkout_snapshot()
        result: ComputeResult | None = None
        for attempt in range(1, max_attempts + 1):
            invoked_at = clock()
            result = await invoke()
            elapsed_s = max(0.0, clock() - invoked_at)
            retryable = (
                _is_sqlite_contention(result)
                and elapsed_s <= startup_window_s
                and checkout_snapshot() == initial_checkout
            )
            if not retryable:
                if attempt > 1 and result.exit_code == 0:
                    _emit_startup_contention(
                        event_logger, issue_id, attempt, max_attempts, "succeeded"
                    )
                return result

            if attempt < max_attempts:
                _emit_startup_contention(
                    event_logger, issue_id, attempt, max_attempts, "retrying"
                )
                await sleeper(initial_backoff_s * (2 ** (attempt - 1)))

        assert result is not None
        _emit_startup_contention(
            event_logger, issue_id, max_attempts, max_attempts, "exhausted"
        )
        detail = result.stderr.rstrip()
        stderr = f"{detail}\n{STARTUP_CONTENTION_EXHAUSTED_REASON}" if detail else (
            STARTUP_CONTENTION_EXHAUSTED_REASON
        )
        return replace(result, stderr=stderr)

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
        permission_refusal = _permission_refusal_reason(
            result.stdout, result.stderr, self.bash_allowlist, result.exit_code
        )
        status = "output_overflow" if result.output_overflow else (
            "blocked" if blocked_reason else (
                "failed" if permission_refusal else (
                    "timeout" if result.timed_out else _status_from_output(
                        result.exit_code, result.stdout, result.stderr
                    )
                )
            )
        )
        error = (
            "backend output exceeded configured Worklink limit"
            if result.output_overflow
            else blocked_reason or permission_refusal or _error_from_status(
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


def _coding_enabled() -> bool:
    from .. import checkout

    resolver = getattr(checkout, "coding_enabled", None)
    if resolver is not None:
        return bool(resolver())
    return os.environ.get("MIMIR_CODING_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _is_sqlite_contention(result: ComputeResult) -> bool:
    if result.exit_code == 0:
        return False
    return any(pattern.search(result.stderr) for pattern in _SQLITE_CONTENTION_PATTERNS)


def _emit_startup_contention(
    event_logger: EventLogger | None,
    issue_id: int,
    attempt: int,
    max_attempts: int,
    outcome: str,
) -> None:
    if event_logger is None:
        return
    event_logger(
        "worklink_backend_startup_contention",
        issue_id=issue_id,
        backend="opencode",
        resource=STARTUP_CONTENTION_RESOURCE,
        retry_attempt=attempt,
        max_attempts=max_attempts,
        outcome=outcome,
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


def _local_argv(bin_name: str, args: Sequence[str], checkout: Path, prompt: str) -> tuple[str, ...]:
    # ``--`` so a prompt that begins with ``-`` is never parsed as a flag.
    return (bin_name, "run", "--dir", str(checkout), *args, "--", prompt)


def _permission_override(bash_allowlist: Sequence[str]) -> str:
    # OpenCode evaluates the last matching rule. Keep the deny first and append
    # only explicit operator grants so unmatched commands cannot prompt or run.
    bash = {"*": "deny"}
    bash.update((pattern, "allow") for pattern in bash_allowlist)
    return json.dumps({
        "external_directory": {"/**": "deny"},
        "bash": bash,
    }, separators=(",", ":"))


def _permission_refusal_reason(
    stdout: str, stderr: str, bash_allowlist: Sequence[str], exit_code: int
) -> str | None:
    """Report an executor permission refusal, distinguished by position or exit.

    The refusal text is free-form OpenCode output, so presence alone cannot
    distinguish "the executor was refused a command" from "the executor wrote the
    words permission denied" — a build editing authorization code necessarily
    writes them into source, tests and docs. Chainlink #1152: nine of the ten
    retained transcripts (#1123, #1149 and #1152's own attempts) matched this way
    while exiting 0 with their work committed and their gate green, because the
    phrase appeared in a Python literal they were editing.

    A refusal counts when it is *positioned as a signal* — the final non-empty
    line of a stream — or when the executor also exited nonzero. This is the same
    discipline ``blocked_reason_from_output`` already applies to its own marker,
    and for the same stated reason: a backend that echoes the phrase mid-stream
    and then completes normally must not be mislabeled.

    Both fail-closed shapes are retained. A refusal that halted the executor
    surfaces via the nonzero exit wherever it appears; a refusal the executor
    reported last surfaces via position even at exit 0. Measured against the
    discarded builds, the nearest false positive sat four non-empty lines from
    the end of its stream, so neither rule admits them.
    """
    pattern = re.compile(
        r"(?:permission.{0,40}(?:denied|reject)|(?:denied|reject).{0,40}permission)",
        re.IGNORECASE,
    )
    positioned = any(
        (last := last_nonempty_line(stream)) is not None and pattern.search(last)
        for stream in (stdout, stderr)
    )
    if not positioned and not (
        exit_code != 0 and pattern.search(f"{stdout}\n{stderr}")
    ):
        return None
    return (
        "OpenCode refused an executor shell command because it is not allowed by "
        "backends.opencode.bash_allowlist; effective patterns: "
        f"{list(bash_allowlist)!r}"
    )


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
