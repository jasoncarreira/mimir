"""Compute substrates for Worklink tool backends.

Tool backends decide *what* backend should build a Worklink issue. Compute
backends decide *where* that work unit runs.  The first substrate is local
subprocess execution, preserving Worklink's original operator-invoked behavior;
later substrates can launch the same git-handoff ``WorkSpec`` in a container or
remote task and still expose launch/wait/cancel/cleanup handles to the
orchestrator.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
from typing import Any, Mapping, Protocol, Sequence


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
    ``test_command``, and push evidence. ``local_worktree`` is a compatibility
    pointer used only by the ``local_subprocess`` fallback for today's manual
    in-container runs; remote substrates ignore it and use the git fields.
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
        command = tuple(_command_for_spec(spec))
        env = {"PATH": os.environ.get("PATH", "")}
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
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            await self.cancel(handle)
            stdout_b, stderr_b = await proc.communicate()

        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        exit_code = getattr(proc, "returncode", None)
        return ComputeResult(
            exit_code=exit_code if exit_code is not None else -1,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            handle=handle,
            command=command,
        )

    async def logs(self, handle: LaunchHandle) -> str:
        self._job(handle)
        return ""

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


def _command_for_spec(spec: WorkSpec) -> list[str]:
    if spec.backend != "codex":
        raise ComputeLaunchError(f"local_subprocess does not know backend {spec.backend!r}")
    if spec.local_worktree is None:
        raise ComputeLaunchError("codex local subprocess requires local_worktree")
    bin_name = str(spec.backend_config.get("bin", "codex"))
    raw_args = spec.backend_config.get("args", ["exec", "--json"])
    if not isinstance(raw_args, Sequence) or isinstance(raw_args, (str, bytes)):
        raise ComputeLaunchError("codex backend_config args must be a sequence")
    args = [str(arg) for arg in raw_args]
    prompt = spec.prompt if spec.rules is None else f"{spec.rules.rstrip()}\n\n{spec.prompt}"
    if args and args[0] == "exec":
        return [bin_name, "exec", "--cd", str(spec.local_worktree), *args[1:], prompt]
    return [bin_name, *args, "--cd", str(spec.local_worktree), prompt]
