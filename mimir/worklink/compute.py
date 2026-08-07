"""Compute substrates for Worklink tool backends.

Tool backends decide *what* backend should build a Worklink issue. Compute
backends decide *where* that work unit runs.  After the #832 substrate
cleanup, ``local_subprocess`` is the sole Worklink compute substrate: the
backend runs as a local subprocess in the per-issue checkout, with the
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
    ``test_command``, and push evidence. ``local_checkout`` and ``local_argv``
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
    local_checkout: Path | None = None
    local_argv: Sequence[str] | None = None


@dataclass(frozen=True)
class LaunchHandle:
    """Opaque handle for a launched compute job."""

    substrate: str
    identifier: str
    process_start_ticks: int | None = None


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

DEFAULT_WORKLINK_STDOUT_BYTES = 64 * 1024 * 1024
DEFAULT_WORKLINK_STDERR_BYTES = 16 * 1024 * 1024


def _output_limit(env_name: str, default: int) -> int:
    """Return a positive output cap, falling back on invalid overrides."""
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _worklink_output_limits() -> tuple[int, int]:
    return (
        _output_limit("MIMIR_WORKLINK_MAX_STDOUT_BYTES", DEFAULT_WORKLINK_STDOUT_BYTES),
        _output_limit("MIMIR_WORKLINK_MAX_STDERR_BYTES", DEFAULT_WORKLINK_STDERR_BYTES),
    )


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


def _containment_policy() -> object | None:
    """The containment policy for a local build, or ``None`` to run as today.

    INERT unless ``MIMIR_CODING_ENABLED`` is set. A deployment with no coding
    tools never runs a build, so there is nothing to contain: ``resolve_containment``
    returns its ``not_required`` state, ``contained`` is False, and this path
    behaves exactly as it did before chainlink #1164. Nothing is imported at
    module scope for it and no spool is consulted.

    When the flag IS set, a missing or world-writable spool raises
    ``ContainmentUnavailable`` out of this function, which fails the launch
    closed rather than silently running the build as the agent user.
    """
    from .containment import containment_required, resolve_containment

    if not containment_required():
        return None
    return resolve_containment()


class _SpooledJob:
    """A build handed to the root-supervised worklink service.

    Satisfies the same duck-typed contract the local backend already uses for a
    subprocess -- ``stdout``/``stderr`` readers, ``wait()``, ``returncode``,
    ``pid`` -- so ``wait``, ``job_alive`` and ``cancel`` need no special-casing.

    The agent cannot drop privilege itself (``CapEff=0``), so it cannot simply
    prefix the command: the supervisor runs as root and does that per step.
    """

    def __init__(self, policy: object, spec: object, command: tuple[str, ...], env: dict[str, str]) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.pid: int | None = None
        self.result: object | None = None
        self._policy = policy
        self._spec = spec
        self._command = command
        self._env = env
        self._request_id: str | None = None

    async def submit(self) -> None:
        from .containment import WorkerRequest, submit_request

        request = WorkerRequest(
            attempt_id=f"{getattr(self._spec, 'issue_id', 'unknown')}-"
            f"{getattr(self._spec, 'attempt', 0)}",
            argv=self._command,
            cwd=Path(str(self._spec.local_checkout)),  # type: ignore[attr-defined]
            env=self._env,
            # The controller pushes the oid the SUPERVISOR read, so a process
            # surviving past the verdict cannot change what gets pushed.
            report_head=True,
        )
        self._request_id = await asyncio.to_thread(submit_request, self._policy, request)

    async def wait(self) -> None:
        """Block until the supervisor publishes what it observed.

        Feeds the captured output into the readers before returning, because the
        backend's ``collect()`` drains them concurrently and then awaits this.
        """
        from .containment import await_result

        if self._request_id is None:  # pragma: no cover - submit() always runs first
            self.returncode = -1
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            return
        result = await asyncio.to_thread(
            await_result, self._policy, self._request_id, timeout_seconds=_SPOOL_WAIT_SECONDS,
        )
        self.result = result
        self.stdout.feed_data(result.stdout.encode())
        self.stdout.feed_eof()
        self.stderr.feed_data(result.stderr.encode())
        self.stderr.feed_eof()
        self.returncode = result.exit_status

    def cancel_request(self) -> None:
        """Best effort: drop the request if the supervisor has not taken it yet."""
        if self._request_id is None:
            return
        from .containment import request_dir

        path = request_dir(self._policy.spool_root) / f"{self._request_id}.json"  # type: ignore[attr-defined]
        path.unlink(missing_ok=True)


#: How long the controller waits for a supervised build. Generous: the backend's
#: own ``wait(timeout_s)`` is the real deadline, and this only bounds the case
#: where the supervisor is not running at all.
_SPOOL_WAIT_SECONDS = 86400.0


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
        if spec.local_checkout is None:
            raise ComputeLaunchError("local_subprocess requires spec.local_checkout")
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
        # opencode-on-checkouts pivot makes it the live path. ``spec.env`` (the
        # orchestrator's per-run vars, e.g. MIMIR_HOME) wins over the passthrough.
        env = _local_child_env()
        env.update(spec.env)

        # chainlink #1164: a build has a model generate code and then executes
        # it, so what runs was reviewed by nobody. When containment is active the
        # step is handed to the root-supervised worklink service, which runs it
        # as a user that cannot write the agent home.
        #
        # Resolving the policy here rather than at the call site keeps this the
        # ONE place a local build can be launched, so a future caller cannot
        # quietly opt out by constructing its own subprocess.
        policy = _containment_policy()
        if policy is not None and policy.contained:
            # Push and PR are controller-side operations. _local_child_env()
            # passes GITHUB_TOKEN/GH_TOKEN through as "provider credentials",
            # which predates this boundary -- the worker has no use for them and
            # submit_request refuses to project them.
            env = {
                key: value
                for key, value in env.items()
                if key not in {"GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN"}
            }
            proc = _SpooledJob(policy, spec, command, env)
            await proc.submit()
        else:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(spec.local_checkout),
                    env=env,
                    start_new_session=True,
                )
            except OSError as exc:
                raise ComputeLaunchError(str(exc)) from exc
        pid = getattr(proc, "pid", None)
        start_ticks = None
        if isinstance(pid, int):
            from .run_state import process_start_ticks

            start_ticks = process_start_ticks(pid)
        handle = LaunchHandle(self.name, str(pid if pid is not None else "unknown"), start_ticks)
        self._jobs[handle.identifier] = (proc, spec, command)
        return handle

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        proc, _spec, command = self._job(handle)
        timed_out = False
        output_overflow = False
        kill_task: asyncio.Task[None] | None = None
        stdout_limit, stderr_limit = _worklink_output_limits()

        def overflow() -> None:
            nonlocal output_overflow, kill_task
            if output_overflow:
                return
            output_overflow = True
            kill_task = asyncio.create_task(self.cancel(handle))

        async def collect() -> tuple[bytes, bytes]:
            stdout_task = asyncio.create_task(
                _drain_capped(getattr(proc, "stdout", None), stdout_limit, overflow)
            )
            stderr_task = asyncio.create_task(
                _drain_capped(getattr(proc, "stderr", None), stderr_limit, overflow)
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
        try:
            proc, _spec, _command = self._job(handle)
        except KeyError:
            proc = _verified_external_process(handle, self.name)
        if isinstance(proc, _SpooledJob):
            # A supervised build has no pid in this process to signal. Drop the
            # request if it is still queued; one already running is bounded by
            # the supervisor's own timeout.
            proc.cancel_request()
            return
        await _kill_process_group(proc)

    async def cleanup(self, handle: LaunchHandle) -> None:
        self._jobs.pop(handle.identifier, None)

    def _job(self, handle: LaunchHandle) -> tuple[object, WorkSpec, tuple[str, ...]]:
        if handle.substrate != self.name or handle.identifier not in self._jobs:
            raise KeyError(f"unknown {self.name} handle: {handle.identifier}")
        return self._jobs[handle.identifier]


class _ExternalProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    async def wait(self) -> None:
        while True:
            from .run_state import process_is_zombie

            if process_is_zombie(self.pid):
                return
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.05)


def _verified_external_process(handle: LaunchHandle, substrate: str) -> _ExternalProcess:
    """Reconstruct a local handle only when its PID birth marker still matches."""
    if handle.substrate != substrate or handle.process_start_ticks is None:
        raise KeyError(f"unknown {substrate} handle: {handle.identifier}")
    try:
        pid = int(handle.identifier)
    except ValueError as exc:
        raise KeyError(f"invalid {substrate} pid: {handle.identifier}") from exc
    from .run_state import process_start_ticks

    if process_start_ticks(pid) != handle.process_start_ticks:
        raise RuntimeError("refusing to cancel: recorded process identity no longer matches")
    return _ExternalProcess(pid)


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
