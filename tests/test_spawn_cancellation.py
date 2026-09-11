"""Cancellation stress test for ``ShellJobRegistry.spawn`` (chainlink #62
verification of #50 §F "Cancellation").

Pins the load-bearing invariant that the bundled ``claude`` CLI does NOT
need to propagate signals itself: the ShellJobRegistry puts every spawn
in its own session via ``start_new_session=True`` (mimir/shell_jobs.py:193),
so the parent harness can ``os.killpg(pid, SIGTERM)`` to take down the
whole process tree (the bundled CLI + every subprocess it spawned).

Without this invariant, a stuck spawn would leak grandchildren on
cancellation. Test forks a real process tree and verifies the entire
group dies on SIGTERM.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from mimir.shell_jobs import ShellJobRegistry


def _make_registry(tmp_path: Path) -> ShellJobRegistry:
    return ShellJobRegistry(jobs_dir=tmp_path / "shell-jobs")


def _wait_for_file(path: Path, job) -> None:
    # Poll only for readiness, not elapsed time. pytest bounds the full protocol.
    while True:
        if path.exists() and path.stat().st_size > 0:
            return
        assert job._process.poll() is None, "child exited before readiness"
        time.sleep(0.05)


def _wait_for_exit(registry: ShellJobRegistry, job_id: str) -> None:
    while True:
        job = registry.get(job_id)
        if job is not None and job.exit_code is not None:
            return
        time.sleep(0.05)


_HAS_PROC = Path("/proc").is_dir()


def _pid_alive(pid: int) -> bool:
    """True if ``kill -0`` reaches the pid AND the pid isn't a zombie.

    Cross-platform; the gotcha is that a SIGTERM'd grandchild whose parent
    is also dead becomes a zombie reparented to PID 1, and ``kill(0)``
    succeeds for zombies. Linux containers (mimir's runtime) often have a
    PID 1 that doesn't aggressively reap orphans, so the zombie can persist
    indefinitely. Path:

    1. ``os.kill(pid, 0)`` — fast cross-platform liveness probe.
       ``ProcessLookupError`` → definitely dead.
    2. On Linux (``/proc`` present), additionally read
       ``/proc/<pid>/status`` and reject ``State: Z`` (zombie). A
       ``FileNotFoundError`` here means we raced with the reaper —
       count as dead.
    3. On macOS (``/proc`` absent), there's no portable zombie
       filter, so trust ``kill(0)`` and wait for init to reap it.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists but we don't own it — counts as alive
        return True
    if not _HAS_PROC:
        # macOS: kill(0) succeeded; we cannot filter zombies. Trust it.
        return True
    # Linux: filter zombies via /proc.
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return False  # raced with reap; pid is gone
    for line in status.splitlines():
        if line.startswith("State:"):
            return "Z" not in line.split(maxsplit=1)[1]
    return True


# ─── tests ────────────────────────────────────────────────────────────


def test_session_leader_isolation_pid_equals_pgid(tmp_path: Path):
    """``start_new_session=True`` makes the spawn its own session+process-group
    leader. The kernel-level invariant is ``pid == pgid``; verifying it
    here pins that ``shell_jobs.py`` actually requested the session and
    didn't silently degrade to a regular fork."""
    registry = _make_registry(tmp_path)
    out_path = tmp_path / "pgid.txt"
    cmd = (
        "import os, signal;\n"
        f"open({str(out_path)!r}, 'w').write(f'{{os.getpid()}},{{os.getpgid(0)}}');\n"
        "signal.pause()\n"
    )
    job = registry.spawn(cmd, argv=[sys.executable, "-c", cmd])
    try:
        _wait_for_file(out_path, job)
        pid_str, pgid_str = out_path.read_text().split(",")
        pid, pgid = int(pid_str), int(pgid_str)
        assert pid == pgid, (
            f"spawn pid {pid} != pgid {pgid} — start_new_session did not take effect"
        )
        assert pid == job.pid, (
            f"reported job.pid {job.pid} != actual pid {pid}"
        )
    finally:
        # Kill only our direct child, even if session isolation regresses.
        job._process.kill()
        _wait_for_exit(registry, job.job_id)
        assert not _pid_alive(job.pid)


def test_sigterm_to_process_group_kills_grandchildren(tmp_path: Path):
    """The end-to-end cancellation invariant: a process tree spawned by
    the bundled CLI (whose grandchildren we don't directly control) is
    fully reaped when we ``killpg(SIGTERM)`` the session leader.

    Both processes block until signalled. Readiness includes the grandchild's
    group identity; no fixed sleep is used to keep either process alive."""
    registry = _make_registry(tmp_path)
    pids_path = tmp_path / "pids.txt"

    cmd = (
        "import os, signal;\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.pause()\n"
        "else:\n"
        f"    open({str(pids_path)!r}, 'w').write(f'{{os.getpid()}},{{child}}')\n"
        "    signal.pause()\n"
    )
    job = registry.spawn(cmd, argv=[sys.executable, "-c", cmd])
    child_pid = None
    try:
        _wait_for_file(pids_path, job)
        parent_pid, child_pid = map(int, pids_path.read_text().split(","))
        assert parent_pid == job.pid, "parent pid should match the spawn pid"
        assert _pid_alive(child_pid), "grandchild should be alive before SIGTERM"
        assert os.getpgid(parent_pid) == os.getpgid(child_pid) == job.pid

        os.killpg(job.pid, signal.SIGTERM)
        _wait_for_exit(registry, job.job_id)
        while _pid_alive(child_pid):
            time.sleep(0.05)
        assert job.exit_code == -signal.SIGTERM
        assert not _pid_alive(parent_pid)
        assert not _pid_alive(child_pid)
    finally:
        # Individual PIDs are safe even with a broken start_new_session flag.
        job._process.kill()
        if child_pid is not None and _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)
            while _pid_alive(child_pid):
                time.sleep(0.05)
        _wait_for_exit(registry, job.job_id)


def test_sigkill_fallback_when_sigterm_ignored(tmp_path: Path):
    """The CLI may install SIGTERM handlers that delay shutdown (it
    doesn't currently, but stress tests should cover the harder case).
    SIGKILL is the always-effective escalation. Verify it cleans up a
    process that explicitly ignores SIGTERM."""
    registry = _make_registry(tmp_path)
    ready = tmp_path / "ready"
    acknowledged = tmp_path / "acknowledged"
    cmd = (
        "import signal;\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})\n"
        f"open({str(ready)!r}, 'w').write('1')\n"
        "signal.sigwait({signal.SIGUSR1})\n"
        f"open({str(acknowledged)!r}, 'w').write('1')\n"
        "signal.pause()\n"
    )
    job = registry.spawn(cmd, argv=[sys.executable, "-c", cmd])
    try:
        _wait_for_file(ready, job)
        assert os.getpgid(job.pid) == job.pid
        os.killpg(job.pid, signal.SIGTERM)
        # A round trip proves continued execution, not survival of a sleep window.
        os.kill(job.pid, signal.SIGUSR1)
        _wait_for_file(acknowledged, job)
        assert _pid_alive(job.pid), "SIGTERM was supposed to be ignored"

        os.killpg(job.pid, signal.SIGKILL)
        _wait_for_exit(registry, job.job_id)
        assert job.exit_code == -signal.SIGKILL
        assert not _pid_alive(job.pid)
    finally:
        job._process.kill()
        _wait_for_exit(registry, job.job_id)
