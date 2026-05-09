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
import time
from pathlib import Path

from mimir.shell_jobs import ShellJobRegistry


def _make_registry(tmp_path: Path) -> ShellJobRegistry:
    return ShellJobRegistry(jobs_dir=tmp_path / "shell-jobs")


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"file {path} did not appear within {timeout}s")


def _wait_for_exit(registry: ShellJobRegistry, job_id: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = registry.get(job_id)
        if job is not None and job.exit_code is not None:
            return
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not exit within {timeout}s")


_HAS_PROC = Path("/proc").is_dir()


def _pid_alive(pid: int) -> bool:
    """True if ``kill -0`` reaches the pid AND the pid isn't a zombie.

    Cross-platform; the gotcha is that a SIGTERM'd grandchild whose parent
    is also dead becomes a zombie reparented to PID 1, and ``kill(0)``
    succeeds for zombies. Linux containers (mimir's runtime) often have a
    PID 1 that doesn't aggressively reap orphans, so the zombie can persist
    well past the test's 5s wait window. Path:

    1. ``os.kill(pid, 0)`` — fast cross-platform liveness probe.
       ``ProcessLookupError`` → definitely dead.
    2. On Linux (``/proc`` present), additionally read
       ``/proc/<pid>/status`` and reject ``State: Z`` (zombie). A
       ``FileNotFoundError`` here means we raced with the reaper —
       count as dead.
    3. On macOS (``/proc`` absent), there's no portable zombie
       filter — trust ``kill(0)``. Tests on macOS need the wait
       window to outlast init's reap latency; on Darwin's launchd
       this is generally <100ms so the existing 5s window is plenty.
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
        "import os, sys, time;\n"
        f"open({str(out_path)!r}, 'w').write(f'{{os.getpid()}},{{os.getpgid(0)}}');\n"
        "time.sleep(30)\n"
    )
    job = registry.spawn(cmd, argv=["python3", "-c", cmd])
    try:
        _wait_for_file(out_path)
        pid_str, pgid_str = out_path.read_text().split(",")
        pid, pgid = int(pid_str), int(pgid_str)
        assert pid == pgid, (
            f"spawn pid {pid} != pgid {pgid} — start_new_session did not take effect"
        )
        assert pid == job.pid, (
            f"reported job.pid {job.pid} != actual pid {pid}"
        )
    finally:
        # Cleanup: SIGKILL the group so the test doesn't leave a 30s sleep behind.
        try:
            os.killpg(job.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _wait_for_exit(registry, job.job_id)


def test_sigterm_to_process_group_kills_grandchildren(tmp_path: Path):
    """The end-to-end cancellation invariant: a process tree spawned by
    the bundled CLI (whose grandchildren we don't directly control) is
    fully reaped when we ``killpg(SIGTERM)`` the session leader.

    Synthesizes the tree by having the spawned python fork a child that
    sleeps 60s. SIGTERM the group; verify both parent and child die
    within a generous window (no zombies, no orphan grandchildren)."""
    registry = _make_registry(tmp_path)
    pids_path = tmp_path / "pids.txt"

    # Parent forks a child that sleeps; both write their PIDs and then
    # block. Both exits must be observable from the test.
    cmd = (
        "import os, sys, time;\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    time.sleep(60)\n"
        "else:\n"
        f"    open({str(pids_path)!r}, 'w').write(f'{{os.getpid()}},{{child}}')\n"
        "    time.sleep(60)\n"
    )
    job = registry.spawn(cmd, argv=["python3", "-c", cmd])

    _wait_for_file(pids_path)
    parent_pid_str, child_pid_str = pids_path.read_text().split(",")
    parent_pid, child_pid = int(parent_pid_str), int(child_pid_str)
    assert parent_pid == job.pid, "parent pid should match the spawn pid"
    assert _pid_alive(child_pid), "grandchild should be alive before SIGTERM"

    # The whole group dies in one signal because they share the session.
    os.killpg(job.pid, signal.SIGTERM)

    # Parent gets reaped through the registry's waiter thread.
    _wait_for_exit(registry, job.job_id, timeout=5.0)

    # Grandchild has no waiter — give the kernel a moment to deliver the
    # signal and let init reap the orphan.
    deadline = time.time() + 5.0
    while time.time() < deadline and _pid_alive(child_pid):
        time.sleep(0.05)
    assert not _pid_alive(child_pid), (
        f"grandchild pid {child_pid} survived SIGTERM to its process group — "
        "process-group containment broken"
    )


def test_sigkill_fallback_when_sigterm_ignored(tmp_path: Path):
    """The CLI may install SIGTERM handlers that delay shutdown (it
    doesn't currently, but stress tests should cover the harder case).
    SIGKILL is the always-effective escalation. Verify it cleans up a
    process that explicitly ignores SIGTERM."""
    registry = _make_registry(tmp_path)
    ready = tmp_path / "ready"
    cmd = (
        "import os, signal, sys, time;\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(ready)!r}, 'w').write('1')\n"
        "time.sleep(60)\n"
    )
    job = registry.spawn(cmd, argv=["python3", "-c", cmd])
    _wait_for_file(ready)

    # SIGTERM is ignored — process keeps running.
    os.killpg(job.pid, signal.SIGTERM)
    time.sleep(0.3)
    assert _pid_alive(job.pid), "SIGTERM was supposed to be ignored"

    # SIGKILL cannot be ignored. This is the cancellation fallback.
    os.killpg(job.pid, signal.SIGKILL)
    _wait_for_exit(registry, job.job_id, timeout=5.0)
    assert not _pid_alive(job.pid)
