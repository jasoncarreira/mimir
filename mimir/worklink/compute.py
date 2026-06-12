"""Compute substrates for Worklink tool backends.

Tool backends decide *what* command to run and how to interpret its output.
Compute backends decide *where/how* that command runs.  The first substrate is
local subprocess execution, preserving Worklink's original in-container
behavior; later substrates can launch the same work spec in a container or a
remote worker without changing tool-specific adapters.
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
    """Concrete process request produced by a Worklink tool backend."""

    argv: Sequence[str]
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_s: int = 1800


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

    async def run(self, spec: WorkSpec) -> ComputeResult: ...


@dataclass(frozen=True)
class LocalSubprocessComputeBackend:
    """Run a WorkSpec as a local subprocess in the current container."""

    name: str = "local_subprocess"

    async def run(self, spec: WorkSpec) -> ComputeResult:
        env = {"PATH": os.environ.get("PATH", "")}
        env.update(spec.env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(spec.cwd),
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            return ComputeResult(
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                launch_error=str(exc),
            )

        handle = LaunchHandle(self.name, str(getattr(proc, "pid", "unknown")))
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=spec.timeout_s
            )
        except TimeoutError:
            timed_out = True
            await _kill_process_group(proc)
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
