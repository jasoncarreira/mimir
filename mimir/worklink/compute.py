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
from dataclasses import dataclass, field, replace
import json
import os
import posixpath
import re
from pathlib import Path
import sys
import uuid
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..output_capture import OutputSink, open_output_pair
from ..contained_execution import (
    CollectedExecutionResult,
    base_worker_environment,
    execute_contained,
    opencode_worker_environment,
)


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
    output_root: Path | None = None


def with_worker_environment(
    spec: WorkSpec, additions: Mapping[str, str]
) -> WorkSpec:
    """Supply injected environment and request it from a closed worker together.

    Specs without ``pass_env`` use the legacy direct environment unchanged. For a
    closed worker, deriving requested names here means every future injection made
    through this boundary automatically participates in the worker contract.
    """
    if not additions:
        return spec
    env = {**spec.env, **additions}
    if "pass_env" not in spec.backend_config:
        return replace(spec, env=env)
    requested = spec.backend_config["pass_env"]
    if isinstance(requested, (str, bytes)) or not isinstance(requested, Sequence):
        raise ComputeLaunchError("worker pass_env must be a sequence")
    pass_env = tuple(dict.fromkeys((*requested, *additions)))
    return replace(
        spec,
        env=env,
        backend_config={**spec.backend_config, "pass_env": pass_env},
    )


@dataclass(frozen=True)
class LaunchHandle:
    """Opaque handle for a launched compute job."""

    substrate: str
    identifier: str
    process_start_ticks: int | None = None
    shim_pid: int | None = None


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
    stdout_path: Path | None = None
    stderr_path: Path | None = None


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
_TERMINATED_DRAIN_TIMEOUT_S = 5.0
_WORKER_CANCEL_TIMEOUT_S = 25.0
_WORKER_COLLECTION_TIMEOUT_S = 10.0
_DIRECT_PROCESS_WAIT_TIMEOUT_S = 5.0


async def _cancel_worker(client: object, identifier: str) -> None:
    try:
        await asyncio.wait_for(
            getattr(client, "cancel")(identifier), _WORKER_CANCEL_TIMEOUT_S
        )
    except TimeoutError as exc:
        raise RuntimeError(
            f"worker cancellation exceeded {_WORKER_CANCEL_TIMEOUT_S:g}s"
        ) from exc


def _output_limit(env_name: str, default: int) -> int:
    """Return a positive output cap, falling back on invalid overrides."""
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return min(value, default) if value > 0 else default


def _worklink_output_limits() -> tuple[int, int]:
    return (
        _output_limit("MIMIR_WORKLINK_MAX_STDOUT_BYTES", DEFAULT_WORKLINK_STDOUT_BYTES),
        _output_limit("MIMIR_WORKLINK_MAX_STDERR_BYTES", DEFAULT_WORKLINK_STDERR_BYTES),
    )


_OUTPUT_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _output_paths(spec: WorkSpec, identifier: str) -> tuple[Path | None, Path | None]:
    root = spec.output_root
    if root is None:
        return None, None
    if _OUTPUT_COMPONENT.fullmatch(spec.backend) is None or spec.backend in {".", ".."}:
        raise ComputeLaunchError("backend name is not a safe output path component")
    stem = f"{spec.backend}-{spec.issue_id}-a{spec.attempt}-{identifier}-{uuid.uuid4()}"
    return root / f"{stem}.stdout.log", root / f"{stem}.stderr.log"


@dataclass
class _DirectCapture:
    stdout_sink: OutputSink
    stderr_sink: OutputSink
    collect_task: asyncio.Task[None] | None = None
    monitor_task: asyncio.Task[None] | None = None
    kill_task: asyncio.Task[None] | None = None
    output_overflow: bool = False


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


_ENABLED_SYNTHESIZED_NAMES = frozenset({
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "PATH",
    "LANG",
    "LC_ALL",
    "OPENCODE_CONFIG",
    "OPENCODE_PERMISSION",
})
_ENABLED_DENIED_PREFIXES = (
    "MIMIR_",
    "GIT_",
    "GH_",
    "GITHUB_",
    "XDG_",
    "LD_",
    "DYLD_",
    "PYTHON",
    "BASH_",
)
_ENABLED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_ENABLED_VALUE_BYTES = 64 * 1024


def _refers_to_controller_home(value: str) -> bool:
    for component in re.split(r"[=,;\s:]", value):
        if not component.startswith("/"):
            continue
        normalized = posixpath.normpath(component)
        if normalized == "/home/mimir" or normalized.startswith("/home/mimir/"):
            return True
    if re.search(r"(?:^|[=,;\s])/?home/mimir(?:/|$)", value):
        return True
    return bool(
        re.search(r"(?:^|[=,;\s])(?:\.\./)+home/mimir(?:/|$)", value)
    )


def _validate_worker_value(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ComputeLaunchError(f"worker environment {name} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ComputeLaunchError(f"worker environment {name} is not UTF-8") from exc
    if len(encoded) > _ENABLED_VALUE_BYTES:
        raise ComputeLaunchError(f"worker environment {name} exceeds size limit")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ComputeLaunchError(f"worker environment {name} contains a forbidden character")
    if _refers_to_controller_home(value):
        raise ComputeLaunchError(f"worker environment {name} refers to controller home")
    return value


def _validate_dynamic_worker_name(name: object) -> str:
    if not isinstance(name, str) or _ENABLED_NAME.fullmatch(name) is None:
        raise ComputeLaunchError("worker environment name is invalid")
    upper = name.upper()
    if (
        name in _ENABLED_SYNTHESIZED_NAMES
        or upper.startswith(_ENABLED_DENIED_PREFIXES)
        or "GITHUB" in upper
    ):
        raise ComputeLaunchError(f"worker environment {name} is denied")
    return name


def _enabled_child_env(spec: WorkSpec, identifier: str) -> dict[str, str]:
    home = f"/var/lib/mimir-worklink/homes/{identifier}"
    additions: dict[str, str] = {}
    permission = spec.env.get("OPENCODE_PERMISSION")
    if permission is not None:
        additions["OPENCODE_PERMISSION"] = _validate_worker_value(
            "OPENCODE_PERMISSION", permission
        )
    env = opencode_worker_environment(base_worker_environment(identifier), additions)
    env["HOME"] = home
    if permission is not None:
        try:
            parsed = json.loads(additions["OPENCODE_PERMISSION"])
        except (TypeError, ValueError) as exc:
            raise ComputeLaunchError("invalid OPENCODE_PERMISSION") from exc
        if not isinstance(parsed, dict):
            raise ComputeLaunchError("invalid OPENCODE_PERMISSION")
    permitted = spec.backend_config.get("pass_env", ())
    if isinstance(permitted, (str, bytes)) or not isinstance(permitted, Sequence):
        raise ComputeLaunchError("worker pass_env must be a sequence")
    seen: set[str] = set()
    for raw_name in permitted:
        name = _validate_dynamic_worker_name(raw_name)
        if name in seen:
            raise ComputeLaunchError(f"worker environment {name} is duplicated")
        seen.add(name)
        if name not in spec.env:
            raise ComputeLaunchError(f"worker environment {name} is missing")
        env[name] = _validate_worker_value(name, spec.env[name])
    unknown = set(spec.env) - seen - {"OPENCODE_PERMISSION"}
    if unknown:
        name = sorted(str(item) for item in unknown)[0]
        raise ComputeLaunchError(f"worker environment {name} was not requested")
    return env


def _fd_anchored_opencode_argv(
    command: tuple[str, ...], checkout: Path | None
) -> tuple[str, ...]:
    """Keep enabled OpenCode inside the checkout reached through its issued FD."""
    if checkout is None:
        raise ComputeLaunchError("local_subprocess requires spec.local_checkout")
    for index, arg in enumerate(command[:-1]):
        if arg != "--dir":
            continue
        if command[index + 1] == ".":
            return command
        if command[index + 1] != str(checkout):
            raise ComputeLaunchError("enabled OpenCode --dir must name the issued checkout")
        return (*command[: index + 1], ".", *command[index + 2 :])
    return command


class _ClientBoundCapability:
    def __init__(
        self,
        capability: object,
        client: object,
        started: asyncio.Future[object],
    ) -> None:
        self._capability = capability
        self._contained_worker_client = client
        self._started = started

    def _contained_started(self, process: object) -> None:
        if not self._started.done():
            self._started.set_result(process)

    def __getattr__(self, name: str) -> object:
        return getattr(self._capability, name)


@dataclass
class LocalSubprocessComputeBackend:
    """Run a WorkSpec as a local subprocess in the current container."""

    name: str = "local_subprocess"
    _authorized_checkout: object | None = field(default=None, repr=False)
    _worker_client: object | None = field(default=None, repr=False)

    @classmethod
    def for_authorized_checkout(
        cls,
        authorization: object,
        *,
        worker_client: object | None = None,
    ) -> LocalSubprocessComputeBackend:
        if not all(
            hasattr(authorization, member) for member in ("verify", "duplicate_fd", "path")
        ):
            raise TypeError("authorization is not a checkout capability")
        return cls(_authorized_checkout=authorization, _worker_client=worker_client)

    def __post_init__(self) -> None:
        self._jobs: dict[str, tuple[object, WorkSpec, tuple[str, ...]]] = {}
        self._handles: dict[str, LaunchHandle] = {}
        self._worker_clients: dict[str, object] = {}
        self._output_files: dict[str, tuple[Path | None, Path | None]] = {}
        self._direct_captures: dict[str, _DirectCapture] = {}

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
        from .checkout import coding_enabled
        if coding_enabled() and spec.backend == "opencode":
            return await self._launch_enabled(spec, command)
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
        output_identifier = str(uuid.uuid4())
        stdout_path, stderr_path = _output_paths(spec, output_identifier)
        stdout_limit, stderr_limit = _worklink_output_limits()
        try:
            stdout_sink, stderr_sink = open_output_pair(
                stdout_path,
                stdout_limit,
                stderr_path,
                stderr_limit,
            )
        except OSError as exc:
            raise ComputeLaunchError(str(exc)) from exc
        try:
            from .worker_exec import _arm_parent_death_signal

            launch_options: dict[str, Any] = {}
            if sys.platform.startswith("linux"):
                parent_pid = os.getpid()
                launch_options["preexec_fn"] = (
                    lambda: _arm_parent_death_signal(parent_pid)
                )
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout_sink.file,
                stderr=stderr_sink.file,
                cwd=str(spec.local_checkout),
                env=env,
                start_new_session=True,
                **launch_options,
            )
        except BaseException as exc:
            stdout_sink.close()
            stderr_sink.close()
            for path in (stdout_path, stderr_path):
                if path is not None:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ComputeLaunchError(str(exc)) from exc
        pid = getattr(proc, "pid", None)
        start_ticks = None
        if isinstance(pid, int):
            from .run_state import process_start_ticks

            start_ticks = process_start_ticks(pid)
        handle = LaunchHandle(self.name, str(pid if pid is not None else "unknown"), start_ticks)
        self._jobs[handle.identifier] = (proc, spec, command)
        self._handles[handle.identifier] = handle
        self._output_files[handle.identifier] = (stdout_path, stderr_path)
        capture = _DirectCapture(stdout_sink, stderr_sink)

        async def monitor_output() -> None:
            while True:
                overflow = await asyncio.to_thread(
                    lambda: stdout_sink.overflowed() or stderr_sink.overflowed()
                )
                if overflow and not capture.output_overflow:
                    capture.output_overflow = True
                    capture.kill_task = asyncio.create_task(self.cancel(handle))
                if overflow:
                    stdout_sink.truncate_to_limit()
                    stderr_sink.truncate_to_limit()
                await asyncio.sleep(0.01)

        async def collect() -> None:
            await getattr(proc, "wait")()

        capture.collect_task = asyncio.create_task(collect())
        capture.monitor_task = asyncio.create_task(monitor_output())
        self._direct_captures[handle.identifier] = capture
        return handle

    async def _launch_enabled(self, spec: WorkSpec, command: tuple[str, ...]) -> LaunchHandle:
        from .worker_client import WorkerClient, WorkerProjection

        # The executor has already fchdir'd through the issued checkout FD. An
        # absolute --dir would re-traverse the controller-owned 0700 boundary.
        command = _fd_anchored_opencode_argv(command, spec.local_checkout)
        authorization = self._authorized_checkout
        if authorization is None or not all(
            hasattr(authorization, member) for member in ("verify", "duplicate_fd", "path")
        ):
            raise ComputeLaunchError("enabled local_subprocess requires an AuthorizedCheckout")
        projections_raw = spec.backend_config.get("worker_projections", ())
        if isinstance(projections_raw, (str, bytes)) or not isinstance(
            projections_raw, Sequence
        ):
            raise ComputeLaunchError("worker projections must be a sequence")
        projections: list[WorkerProjection] = []
        try:
            for item in projections_raw:
                projections.append(WorkerProjection(path=item.path, document=item.document))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ComputeLaunchError("worker projection is invalid") from exc
        identifier = str(uuid.uuid4())
        client = self._worker_client or WorkerClient(authorization)
        started: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        capability = _ClientBoundCapability(authorization, client, started)
        stdout_limit, stderr_limit = _worklink_output_limits()
        stdout_path, stderr_path = _output_paths(spec, identifier)
        try:
            stdout_sink, stderr_sink = open_output_pair(
                stdout_path,
                stdout_limit,
                stderr_path,
                stderr_limit,
            )
        except OSError as exc:
            raise ComputeLaunchError(str(exc)) from exc

        async def collect_contained() -> CollectedExecutionResult:
            try:
                return await execute_contained(
                    command,
                    capability,
                    {
                        key: value
                        for key, value in _enabled_child_env(spec, identifier).items()
                        if key != "HOME"
                    },
                    projections,
                    identifier=identifier,
                    timeout_s=spec.timeout_s,
                    stdout_limit=stdout_limit,
                    stderr_limit=stderr_limit,
                    stdout_sink=stdout_sink,
                    stderr_sink=stderr_sink,
                )
            except asyncio.CancelledError:
                if not started.done():
                    started.cancel()
                raise
            except BaseException as exc:
                if not started.done():
                    started.set_exception(exc)
                raise
            finally:
                stdout_sink.close()
                stderr_sink.close()

        task = asyncio.create_task(collect_contained())
        try:
            process = await asyncio.shield(started)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            await asyncio.gather(task, return_exceptions=True)
            raise ComputeLaunchError(str(exc)) from exc
        pid = getattr(process, "pid", None)
        ticks = None
        if isinstance(pid, int):
            from .run_state import process_start_ticks

            ticks = process_start_ticks(pid)
        handle = LaunchHandle(
            substrate=self.name,
            identifier=identifier,
            process_start_ticks=ticks,
            shim_pid=pid,
        )
        self._jobs[identifier] = (task, spec, command)
        self._handles[identifier] = handle
        self._worker_clients[identifier] = client
        self._output_files[identifier] = (stdout_path, stderr_path)
        return handle

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        job, _spec, command = self._job(handle)
        stdout_path, stderr_path = self._output_files.get(handle.identifier, (None, None))
        if isinstance(job, asyncio.Task):
            timed_out = False
            try:
                collected = await asyncio.wait_for(
                    asyncio.shield(job), timeout=timeout_s
                )
            except TimeoutError:
                timed_out = True
                client = self._worker_clients[handle.identifier]
                await _cancel_worker(client, handle.identifier)
                try:
                    collected = await asyncio.wait_for(
                        job, _WORKER_COLLECTION_TIMEOUT_S
                    )
                except TimeoutError as exc:
                    raise RuntimeError(
                        "worker did not report a terminal result within "
                        f"{_WORKER_COLLECTION_TIMEOUT_S:g}s of cancellation"
                    ) from exc
            if not isinstance(collected, CollectedExecutionResult):
                raise RuntimeError("contained execution returned an invalid result")
            return ComputeResult(
                exit_code=collected.exit_code if collected.exit_code is not None else -1,
                stdout=collected.stdout.decode(errors="replace"),
                stderr=collected.stderr.decode(errors="replace"),
                timed_out=timed_out or collected.timed_out,
                output_overflow=collected.output_overflow,
                handle=handle,
                command=command,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )

        proc = job
        timed_out = False
        capture = self._direct_captures[handle.identifier]
        collect_task = capture.collect_task
        if collect_task is None:
            raise RuntimeError("direct output capture did not start")

        async def collect_after_termination() -> None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(collect_task), _TERMINATED_DRAIN_TIMEOUT_S
                )
            except TimeoutError:
                collect_task.cancel()
                await asyncio.gather(collect_task, return_exceptions=True)

        try:
            await asyncio.wait_for(asyncio.shield(collect_task), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            await self.cancel(handle)
            await collect_after_termination()
        if capture.kill_task is not None:
            await capture.kill_task
            if not collect_task.done():
                await collect_after_termination()
        if capture.monitor_task is not None:
            capture.monitor_task.cancel()
            await asyncio.gather(capture.monitor_task, return_exceptions=True)

        if capture.stdout_sink.overflowed() or capture.stderr_sink.overflowed():
            capture.output_overflow = True
            if getattr(proc, "returncode", None) is None:
                await self.cancel(handle)
                await collect_after_termination()
        stdout_b, _stdout_dropped = capture.stdout_sink.read_bounded()
        stderr_b, _stderr_dropped = capture.stderr_sink.read_bounded()
        capture.output_overflow = (
            capture.output_overflow
            or capture.stdout_sink.did_overflow
            or capture.stderr_sink.did_overflow
        )
        capture.stdout_sink.truncate_to_limit()
        capture.stderr_sink.truncate_to_limit()
        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        exit_code = getattr(proc, "returncode", None)
        return ComputeResult(
            exit_code=exit_code if exit_code is not None else -1,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_overflow=capture.output_overflow,
            handle=handle,
            command=command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    async def logs(self, handle: LaunchHandle) -> str:
        self._job(handle)
        return ""

    def job_alive(self, handle: LaunchHandle) -> bool:
        """Whether the launched subprocess is still running (liveness probe for
        the feature-factory observe loop). Unknown/gone handles read as dead."""
        try:
            job, _spec, _command = self._job(handle)
        except KeyError:
            return False
        if isinstance(job, asyncio.Task):
            return not job.done()
        return getattr(job, "returncode", 0) is None

    async def cancel(self, handle: LaunchHandle) -> None:
        try:
            proc, _spec, _command = self._job(handle)
        except KeyError:
            proc = _verified_external_process(handle, self.name)
        client = self._worker_clients.get(handle.identifier)
        if client is not None:
            await _cancel_worker(client, handle.identifier)
            return
        if handle.shim_pid is not None:
            from .worker_client import WorkerClient

            await _cancel_worker(WorkerClient(None), handle.identifier)  # type: ignore[arg-type]
            return
        await _kill_process_group(proc)

    async def cleanup(self, handle: LaunchHandle) -> None:
        known = self._handles.get(handle.identifier)
        if known is not None and known != handle:
            raise KeyError(f"unknown {self.name} handle: {handle.identifier}")
        if known is None:
            return
        self._jobs.pop(handle.identifier, None)
        self._handles.pop(handle.identifier)
        self._worker_clients.pop(handle.identifier, None)
        self._output_files.pop(handle.identifier, None)
        capture = self._direct_captures.pop(handle.identifier, None)
        if capture is not None:
            capture.stdout_sink.close()
            capture.stderr_sink.close()

    def _job(self, handle: LaunchHandle) -> tuple[object, WorkSpec, tuple[str, ...]]:
        if (
            handle.substrate != self.name
            or handle.identifier not in self._jobs
            or self._handles.get(handle.identifier) != handle
        ):
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
    if handle.shim_pid is None:
        try:
            pid = int(handle.identifier)
        except ValueError as exc:
            raise KeyError(f"invalid {substrate} pid: {handle.identifier}") from exc
    else:
        try:
            parsed = uuid.UUID(handle.identifier, version=4)
        except ValueError as exc:
            raise KeyError(f"invalid {substrate} UUID: {handle.identifier}") from exc
        if str(parsed) != handle.identifier:
            raise KeyError(f"invalid {substrate} UUID: {handle.identifier}")
        pid = handle.shim_pid
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
    except ProcessLookupError:
        return
    except OSError:
        kill = getattr(proc, "kill", None)
        if kill:
            kill()
        return
    from .worker_exec import _terminate_process_group_pid

    await asyncio.to_thread(_terminate_process_group_pid, pgid)
    wait = getattr(proc, "wait", None)
    if wait is not None:
        try:
            await asyncio.wait_for(wait(), _DIRECT_PROCESS_WAIT_TIMEOUT_S)
        except TimeoutError as exc:
            raise RuntimeError(
                f"process group {pgid} leader was not reaped after SIGKILL"
            ) from exc
