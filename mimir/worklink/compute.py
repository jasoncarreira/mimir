"""Compute substrates for Worklink tool backends.

Tool backends decide *what* backend should build a Worklink issue. Compute
backends decide *where* that work unit runs.  After the #832 substrate
cleanup, ``local_subprocess`` is the sole Worklink compute substrate: the
backend runs as a local subprocess in the per-issue worktree, with the
shared-filesystem capabilities and (for autonomous dispatch) the explicit
opt-in gate that the rest of the executor already enforces.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
from typing import Any, Callable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ComputeCaps:
    """Capabilities of a Worklink execution substrate."""

    shared_filesystem: bool
    network_isolated: bool
    handle_cancel: bool
    persistent_after_disconnect: bool


@dataclass(frozen=True)
class WorkSpec:
    """Portable work unit handed to a Worklink compute substrate.

    The durable handoff is git-shaped: a worker can clone ``repo_url``, check out
    ``base_ref``/``branch``, run ``backend`` with ``prompt``/``rules``, execute
    ``test_command``, and push evidence. ``local_worktree`` and ``local_argv``
    are compatibility pointers used only by the ``local_subprocess`` substrate
    for today's manual in-container runs.
    """

    issue_id: int
    attempt: int
    repo_url: str
    base_ref: str
    branch: str
    prompt: str
    rules: str | None
    test_command: str
    backend: str
    timeout_s: int
    creds_ref: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    backend_config: Mapping[str, Any] = field(default_factory=dict)
    local_worktree: Path | None = None
    local_argv: Sequence[str] | None = None


@dataclass(frozen=True)
class LaunchHandle:
    """Opaque handle for a launched compute job."""

    substrate: str
    identifier: str


@dataclass(frozen=True)
class ComputeResult:
    """Observed result from a launched compute job."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_overflow: bool = False
    launch_error: str | None = None
    handle: LaunchHandle | None = None
    command: tuple[str, ...] = ()


class ComputeLaunchError(RuntimeError):
    """Raised when a compute substrate cannot launch a work unit."""


class ComputeBackend(Protocol):
    name: str

    def capabilities(self) -> ComputeCaps: ...

    async def launch(self, spec: WorkSpec) -> LaunchHandle: ...

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult: ...

    async def logs(self, handle: LaunchHandle) -> str: ...

    async def cancel(self, handle: LaunchHandle) -> None: ...

    async def cleanup(self, handle: LaunchHandle) -> None: ...


_LOCAL_ENV_INFRA = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM",
    "TMPDIR", "TMP", "TEMP", "TZ", "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)
_LOCAL_ENV_INFRA_PREFIXES = ("LC_", "XDG_")
# Provider-credential families a routed coding CLI (codex / claude / opencode,
# which is provider-agnostic) may legitimately need. Union kept broad on
# purpose — opencode routes to whichever provider its config selects.
_LOCAL_ENV_CRED_PREFIXES = (
    "OPENAI_", "CODEX_", "ANTHROPIC_", "CLAUDE_", "OPENCODE_",
    "MINIMAX_", "OPENROUTER_", "GROQ_", "GEMINI_", "GOOGLE_",
    "VOYAGE_", "GITHUB_TOKEN", "GH_TOKEN",
)

MAX_WORKLINK_STDOUT_BYTES = 16 * 1024 * 1024
MAX_WORKLINK_STDERR_BYTES = 1 * 1024 * 1024


async def _drain_capped(
    stream: asyncio.StreamReader | None,
    limit: int,
    on_overflow: Callable[[], None],
) -> bytes:
    """Retain at most ``limit`` bytes while always draining the pipe."""
    if stream is None:
        return b""
    retained = bytearray()
    overflowed = False
    while chunk := await stream.read(64 * 1024):
        remaining = limit - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > remaining and not overflowed:
            overflowed = True
            on_overflow()
    return bytes(retained)


def _local_child_env() -> dict[str, str]:
    """Allowlisted env for an autonomous local_subprocess worker (#830).

    Infra vars + provider-credential families from the parent process; nothing
    else (no bridge/operator secrets). Mirrors ``tools.registry`` spawn-env
    philosophy on the worklink compute path."""
    env: dict[str, str] = {}
    for key in _LOCAL_ENV_INFRA:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    for key, val in os.environ.items():
        if key.startswith(_LOCAL_ENV_INFRA_PREFIXES) or key.startswith(_LOCAL_ENV_CRED_PREFIXES):
            env[key] = val
    return env


@dataclass
class LocalSubprocessComputeBackend:
    """Run a WorkSpec as a local subprocess in the current container."""

    name: str = "local_subprocess"

    def __post_init__(self) -> None:
        self._jobs: dict[str, tuple[object, WorkSpec, tuple[str, ...]]] = {}

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(
            shared_filesystem=True,
            network_isolated=False,
            handle_cancel=True,
            persistent_after_disconnect=False,
        )

    async def launch(self, spec: WorkSpec) -> LaunchHandle:
        if spec.local_worktree is None:
            raise ComputeLaunchError("local_subprocess requires spec.local_worktree")
        if spec.local_argv is None:
            raise ComputeLaunchError("local_subprocess requires spec.local_argv")
        if isinstance(spec.local_argv, (str, bytes)):
            raise ComputeLaunchError("local_subprocess spec.local_argv must be a sequence")
        command = tuple(str(arg) for arg in spec.local_argv)
        if not command:
            raise ComputeLaunchError("local_subprocess spec.local_argv must not be empty")
        # chainlink #830: autonomous local_subprocess builds an allowlisted env
        # from the parent process — infra vars (HOME so a coding CLI finds its
        # config/plugins + provider auth files; locale/cert vars) plus provider
        # credential families. Bridge/operator secrets (DISCORD_/SLACK_/
        # MIMIR_API_KEY, ...) are NEVER passed. This was inert while docker was
        # the only autonomous substrate (creds arrived via broker policy) and
        # local_subprocess was operator-CLI-only (full env inherited); the
        # opencode-on-worktrees pivot makes it the live path. ``spec.env`` (the
        # orchestrator's per-run vars, e.g. MIMIR_HOME) wins over the passthrough.
        env = _local_child_env()
        env.update(spec.env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(spec.local_worktree),
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise ComputeLaunchError(str(exc)) from exc
        handle = LaunchHandle(self.name, str(getattr(proc, "pid", "unknown")))
        self._jobs[handle.identifier] = (proc, spec, command)
        return handle

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        proc, _spec, command = self._job(handle)
        timed_out = False
        output_overflow = False
        kill_task: asyncio.Task[None] | None = None

        def overflow() -> None:
            nonlocal output_overflow, kill_task
            if output_overflow:
                return
            output_overflow = True
            kill_task = asyncio.create_task(self.cancel(handle))

        async def collect() -> tuple[bytes, bytes]:
            stdout_task = asyncio.create_task(
                _drain_capped(
                    getattr(proc, "stdout", None), MAX_WORKLINK_STDOUT_BYTES, overflow
                )
            )
            stderr_task = asyncio.create_task(
                _drain_capped(
                    getattr(proc, "stderr", None), MAX_WORKLINK_STDERR_BYTES, overflow
                )
            )
            await getattr(proc, "wait")()
            return await asyncio.gather(stdout_task, stderr_task)

        collect_task = asyncio.create_task(collect())
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                asyncio.shield(collect_task), timeout=timeout_s
            )
        except TimeoutError:
            timed_out = True
            await self.cancel(handle)
            stdout_b, stderr_b = await collect_task
        if kill_task is not None:
            await kill_task

        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        exit_code = getattr(proc, "returncode", None)
        return ComputeResult(
            exit_code=exit_code if exit_code is not None else -1,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_overflow=output_overflow,
            handle=handle,
            command=command,
        )

    async def logs(self, handle: LaunchHandle) -> str:
        self._job(handle)
        return ""

    def job_alive(self, handle: LaunchHandle) -> bool:
        """Whether the launched subprocess is still running (liveness probe for
        the feature-factory observe loop). Unknown/gone handles read as dead."""
        try:
            proc, _spec, _command = self._job(handle)
        except KeyError:
            return False
        return getattr(proc, "returncode", 0) is None

    async def cancel(self, handle: LaunchHandle) -> None:
        proc, _spec, _command = self._job(handle)
        await _kill_process_group(proc)

    async def cleanup(self, handle: LaunchHandle) -> None:
        self._jobs.pop(handle.identifier, None)

    def _job(self, handle: LaunchHandle) -> tuple[object, WorkSpec, tuple[str, ...]]:
        if handle.substrate != self.name or handle.identifier not in self._jobs:
            raise KeyError(f"unknown {self.name} handle: {handle.identifier}")
        return self._jobs[handle.identifier]


async def _kill_process_group(proc: object) -> None:
    pid = getattr(proc, "pid", None)
    if pid is None:
        kill = getattr(proc, "kill", None)
        if kill:
            kill()
        return
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        kill = getattr(proc, "kill", None)
        if kill:
            kill()
        return
    wait = getattr(proc, "wait", None)
    if wait is None:
        return
    try:
        await asyncio.wait_for(wait(), timeout=5)
    except TimeoutError:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await wait()
