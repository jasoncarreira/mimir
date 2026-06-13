"""Compute substrates for Worklink tool backends.

Tool backends decide *what* tool should do the work. Compute backends decide
*where/how* that work runs.  The WorkSpec is intentionally at work-unit
altitude (issue/attempt + git handoff + prompt/test coordinates), not merely a
local ``argv``/``cwd`` pair; local subprocess execution carries optional local
hints only to preserve today's in-container behavior while remote substrates use
the git coordinates.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class WorkSpec:
    """Portable Worklink work unit handed to a compute substrate."""

    issue_id: int
    attempt: int
    repo_url: str | None
    base_ref: str
    branch: str
    prompt: str
    rules: str | None
    test_command: str
    backend: str
    timeout_s: int = 1800
    creds_ref: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    # Compatibility hints for the explicit local-subprocess fallback. Remote
    # substrates must use the git-handoff fields above instead of these paths.
    local_argv: Sequence[str] = field(default_factory=tuple)
    local_worktree: Path | None = None


@dataclass(frozen=True)
class LaunchHandle:
    """Opaque handle for a launched compute job."""

    substrate: str
    identifier: str


@dataclass(frozen=True)
class ComputeResult:
    """Observed result from running a ``WorkSpec`` on a compute substrate."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    launch_error: str | None = None
    handle: LaunchHandle | None = None


class ComputeBackend(Protocol):
    name: str

    async def launch(self, spec: WorkSpec) -> LaunchHandle: ...

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult: ...

    async def cancel(self, handle: LaunchHandle) -> None: ...

    async def cleanup(self, handle: LaunchHandle) -> None: ...


@dataclass
class LocalSubprocessComputeBackend:
    """Run a WorkSpec as a local subprocess in the current container."""

    name: str = "local_subprocess"

    def __post_init__(self) -> None:
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._launch_errors: dict[str, str] = {}
        self._specs: dict[str, WorkSpec] = {}
        self._next_error_id = 0

    async def launch(self, spec: WorkSpec) -> LaunchHandle:
        if not spec.local_argv or spec.local_worktree is None:
            raise ValueError("local_subprocess requires local_argv and local_worktree hints")
        env = {"PATH": os.environ.get("PATH", "")}
        env.update(spec.env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.local_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(spec.local_worktree),
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            self._next_error_id += 1
            identifier = f"launch-error-{self._next_error_id}"
            self._launch_errors[identifier] = str(exc)
            self._specs[identifier] = spec
            return LaunchHandle(self.name, identifier)

        identifier = str(getattr(proc, "pid", "unknown"))
        self._processes[identifier] = proc
        self._specs[identifier] = spec
        return LaunchHandle(self.name, identifier)

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        if handle.identifier in self._launch_errors:
            error = self._launch_errors[handle.identifier]
            return ComputeResult(
                exit_code=-1,
                stdout="",
                stderr=error,
                launch_error=error,
                handle=handle,
            )
        proc = self._processes[handle.identifier]
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            await self.cancel(handle)
            stdout_b, stderr_b = await proc.communicate()

        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        exit_code = proc.returncode if proc.returncode is not None else -1
        return ComputeResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            handle=handle,
        )

    async def cancel(self, handle: LaunchHandle) -> None:
        proc = self._processes.get(handle.identifier)
        if proc is None:
            return
        await _kill_process_group(proc)

    async def cleanup(self, handle: LaunchHandle) -> None:
        self._processes.pop(handle.identifier, None)
        self._launch_errors.pop(handle.identifier, None)
        self._specs.pop(handle.identifier, None)

    async def run(self, spec: WorkSpec) -> ComputeResult:
        """Compatibility helper for callers that do not need handle control."""

        handle = await self.launch(spec)
        try:
            return await self.wait(handle, spec.timeout_s)
        finally:
            await self.cleanup(handle)


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
