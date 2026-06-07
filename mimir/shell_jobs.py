"""In-flight shell job registry for async long-running shell commands.

Ported from open-strix-base/open_strix/shell_jobs.py (commits 1ee9873 +
af03cf8). When the ``bash_async`` MCP tool is invoked, the command is
spawned via subprocess.Popen, registered here, and its stdout/stderr
are captured to files on disk via background drainer threads. The
agent can then use ``bash_jobs_list`` / ``bash_job_output`` to check
on progress; on exit, an ``on_complete`` callback fires a
``shell_job_complete`` AgentEvent into the dispatcher so the
spawning channel resumes with full context (no polling needed).

Design notes:

- Lifecycle cleanup via ``_evict_stale``. Kills happen via the agent
  issuing a regular ``Bash`` tool call (which has access to ``kill
  <pid>``). Finished jobs are evicted (and their on-disk output files
  are unlinked) after ``EVICT_AFTER_SECONDS`` (default 1 hour). Eviction
  runs eagerly on the next ``spawn()`` so no background thread is
  needed. Jobs still within the window remain accessible via
  ``bash_job_output`` / ``read_output``.
- No algedonic signals on routine completion — the wake-up turn IS
  the signal. ``shell_job_complete_enqueue_failed`` fires only when
  the bridge to the dispatcher itself breaks (rare).
- Running jobs are visible immediately via ``bash_jobs_list``;
  finished jobs only stay visible if they ran long enough to cross
  ``UI_VISIBILITY_THRESHOLD_SECONDS`` so trivial async tasks don't
  flood the listing.
- channel_id is captured at spawn time so the completion event can be
  routed back to the originating channel. Channel-less spawns (e.g. a
  scheduled tick that didn't carry a channel) fall back to the
  configured ``default_completion_channel``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


log = logging.getLogger(__name__)


UI_VISIBILITY_THRESHOLD_SECONDS = 10
POST_EXIT_GRACE_SECONDS = 15
EVICT_AFTER_SECONDS = 3600  # evict finished jobs + unlink output files after this window
# chainlink #387: a bounded window for the waiter to let the stdout/stderr
# drainers flush final bytes after the process exits. Never block longer — a
# backgrounded grandchild can hold the pipe open so a drainer never hits EOF.
DRAIN_JOIN_TIMEOUT_SECONDS = 10.0
# chainlink #387: refuse to start more than this many concurrently-live jobs so
# a flood can't spawn unbounded subprocesses/threads/FDs.
MAX_LIVE_SHELL_JOBS = 32
SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES = 1000
SHELL_JOB_OUTPUT_MAX_TAIL_LINES = 2000
DEFAULT_SHELL_JOB_SCOPE = "running"
VALID_SHELL_JOB_SCOPES = frozenset({"running", "visible", "all"})
DEFAULT_SHELL_JOB_STREAM = "both"
VALID_SHELL_JOB_STREAMS = frozenset({"stdout", "stderr", "both"})


@dataclass
class ShellJob:
    """A spawned async shell command with file-backed stdout/stderr."""

    job_id: str
    command: str
    pid: int
    started_at: float
    stdout_path: Path
    stderr_path: Path
    last_live_signal: float  # epoch seconds; updated by drainer threads
    exit_code: Optional[int] = None  # None while running
    finished_at: Optional[float] = None
    channel_id: Optional[str] = None
    _process: Optional[subprocess.Popen] = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def status(self) -> str:
        if self.exit_code is None:
            return "running"
        if self.exit_code == 0:
            return "exited_ok"
        return "exited_error"

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    @property
    def seconds_since_last_signal(self) -> float:
        return max(0.0, time.time() - self.last_live_signal)

    def touch(self) -> None:
        with self._lock:
            self.last_live_signal = time.time()

    def snapshot(self) -> dict:
        """JSON-serializable view for tools and event payloads."""
        return {
            "job_id": self.job_id,
            "pid": self.pid,
            "command": self.command,
            "started_at": self.started_at,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "last_live_signal": self.last_live_signal,
            "seconds_since_last_signal": round(self.seconds_since_last_signal, 2),
            "status": self.status,
            "exit_code": self.exit_code,
            "channel_id": self.channel_id,
        }


class ShellJobRegistry:
    """In-memory registry of spawned shell jobs.

    Constructed once on agent startup. Thread-safe because drainer
    threads update jobs while the asyncio loop reads them via the
    ``bash_jobs_list`` / ``bash_job_output`` tool handlers.
    """

    def __init__(self, jobs_dir: Path) -> None:
        self.jobs_dir = jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, ShellJob] = {}
        self._lock = threading.Lock()

    def _make_job_id(self) -> str:
        return "j_" + uuid.uuid4().hex[:10]

    def _evict_stale(self, *, now: Optional[float] = None) -> list[ShellJob]:
        """Evict finished jobs older than ``EVICT_AFTER_SECONDS``.

        Pops evicted entries from ``_jobs`` under ``self._lock``, then
        unlinks their stdout/stderr files outside the lock (I/O while
        holding a lock degrades throughput). Called from ``spawn()`` so
        the registry doesn't grow unbounded over daemon lifetime.

        ``now`` is injectable for deterministic testing.

        Returns the evicted jobs (callers rarely need this; exposed for
        test assertions).
        """
        now = now if now is not None else time.time()
        evicted: list[ShellJob] = []
        with self._lock:
            to_evict = [
                job_id
                for job_id, job in self._jobs.items()
                if job.exit_code is not None
                and job.finished_at is not None
                and (now - job.finished_at) >= EVICT_AFTER_SECONDS
            ]
            for job_id in to_evict:
                evicted.append(self._jobs.pop(job_id))
        for job in evicted:
            for path in (job.stdout_path, job.stderr_path):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    log.warning("failed to unlink %s during job eviction", path)
        return evicted

    def spawn(
        self,
        command: str,
        *,
        argv: list[str],
        channel_id: Optional[str] = None,
        on_complete: Optional[Callable[["ShellJob"], None]] = None,
        env_overlay: Optional[dict[str, Optional[str]]] = None,
        cwd: Optional[os.PathLike] = None,
    ) -> ShellJob:
        """Spawn argv as a subprocess and register it.

        ``argv`` is the platform-specific wrapped command (e.g.
        ``["bash", "-lc", cmd]``). ``command`` is the original string
        the agent provided — kept verbatim for display/logging.

        ``channel_id`` records which conversation spawned the job so
        the completion callback can resume it in-place.

        ``on_complete`` is invoked from the waiter thread once the
        subprocess exits and ``exit_code`` / ``finished_at`` are set.
        Exceptions raised by the callback are caught and dropped so
        the registry stays intact even if the bridge breaks.

        ``env_overlay`` merges into the inherited env after defaults
        are applied; keys map to None to *unset* an inherited var.
        ``cwd`` overrides the working directory the subprocess starts
        in. Both used by ``spawn_claude_code`` to run the bundled CLI
        with ``HOME=/mimir-home`` (so OAuth credentials resolve) and
        without ``CLAUDECODE`` (so the spawn doesn't think it's
        nested in a Claude Code session).
        """
        # Eagerly evict finished jobs that have aged out, bounding the
        # registry size and freeing their on-disk output files.
        self._evict_stale()

        # chainlink #387: cap concurrently-live jobs. The waiter fix stops stuck
        # jobs from leaking forever, but a flood of legitimately-running jobs
        # should still be refused rather than spawning unbounded resources. The
        # bash_async tool surfaces this as "bash_async failed: ...".
        with self._lock:
            live = sum(1 for j in self._jobs.values() if j.exit_code is None)
        if live >= MAX_LIVE_SHELL_JOBS:
            raise RuntimeError(
                f"too many live shell jobs ({live}/{MAX_LIVE_SHELL_JOBS}); wait "
                "for some to finish (see bash_jobs_list) before starting more"
            )

        job_id = self._make_job_id()
        stdout_path = self.jobs_dir / f"{job_id}.out"
        stderr_path = self.jobs_dir / f"{job_id}.err"

        stdout_f = stdout_path.open("wb")
        stderr_f = stderr_path.open("wb")

        try:
            env = os.environ.copy()
            # Python is a common job runner; force unbuffered output so
            # "print then sleep" jobs stream into the on-disk log while
            # still running (otherwise bash_job_output sees nothing
            # until the process ends).
            env.setdefault("PYTHONUNBUFFERED", "1")
            if env_overlay:
                for k, v in env_overlay.items():
                    if v is None:
                        env.pop(k, None)
                    else:
                        env[k] = v
            popen_kwargs: dict = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "bufsize": 0,
                "env": env,
            }
            if cwd is not None:
                popen_kwargs["cwd"] = os.fspath(cwd)
            # Put child in its own session so the agent can send signals
            # to it (via the regular Bash tool) without affecting the
            # parent harness. Linux/macOS only; the container always
            # runs Linux so the always-on path is fine.
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(argv, **popen_kwargs)
        except Exception:
            stdout_f.close()
            stderr_f.close()
            raise

        started_at = time.time()
        job = ShellJob(
            job_id=job_id,
            command=command,
            pid=proc.pid,
            started_at=started_at,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            last_live_signal=started_at,
            channel_id=channel_id,
            _process=proc,
        )

        def _drain(stream, outfile, on_signal):
            try:
                while True:
                    try:
                        chunk = stream.read(4096)
                    except (ValueError, OSError):
                        # Our pipe end was closed by _waiter to reclaim a drainer
                        # wedged on a backgrounded grandchild's held pipe (#387).
                        break
                    if not chunk:
                        break
                    outfile.write(chunk)
                    outfile.flush()
                    on_signal()
            finally:
                try:
                    outfile.close()
                except Exception:
                    pass

        def _waiter():
            try:
                rc = proc.wait()
            except Exception:
                rc = -1
            # proc.wait() can return before the stdout/stderr drainer threads
            # have copied the final pipe bytes into the files read_output()
            # tails. Give them a BOUNDED window to flush (so short commands like
            # ``echo two`` don't lose their tail) — but never block forever: a
            # backgrounded grandchild can hold the pipe open so a drainer never
            # hits EOF, which pre-fix left the job stuck status=running and never
            # evicted, leaking the job + its threads + pipe FDs (chainlink #387).
            deadline = time.time() + DRAIN_JOIN_TIMEOUT_SECONDS
            for thread in drain_threads:
                remaining = deadline - time.time()
                if remaining > 0:
                    thread.join(timeout=remaining)
            # Mark finished regardless, so status/eviction reflect the real exit
            # even if a drainer is still wedged.
            with job._lock:
                job.exit_code = rc
                job.finished_at = time.time()
            # If a drainer is still alive (wedged on a held pipe), close our pipe
            # ends so its blocked read() unblocks and the thread + FDs are
            # reclaimed. The detached grandchild keeps its own write end; we only
            # reap OUR resources here.
            if any(t.is_alive() for t in drain_threads):
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
            if on_complete is not None:
                try:
                    on_complete(job)
                except Exception:
                    # Never let a callback error break the registry.
                    log.exception("on_complete callback raised for job %s", job_id)

        drain_threads = [
            threading.Thread(
                target=_drain,
                args=(proc.stdout, stdout_f, job.touch),
                daemon=True,
                name=f"shelljob-out-{job_id}",
            ),
            threading.Thread(
                target=_drain,
                args=(proc.stderr, stderr_f, job.touch),
                daemon=True,
                name=f"shelljob-err-{job_id}",
            ),
        ]

        for thread in drain_threads:
            thread.start()
        threading.Thread(
            target=_waiter,
            daemon=True,
            name=f"shelljob-wait-{job_id}",
        ).start()

        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[ShellJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def all_jobs(self) -> list[ShellJob]:
        with self._lock:
            return list(self._jobs.values())

    def _sorted_jobs(self, jobs: list[ShellJob]) -> list[ShellJob]:
        # Running first, then most-recently-started.
        return sorted(
            jobs,
            key=lambda job: (job.exit_code is not None, -job.started_at),
        )

    def running_jobs(self) -> list[ShellJob]:
        return self._sorted_jobs([job for job in self.all_jobs() if job.exit_code is None])

    def visible_jobs(self, *, now: Optional[float] = None) -> list[ShellJob]:
        """Jobs that should appear in tool listings.

        Running jobs are always visible. Finished jobs stay visible for
        ``POST_EXIT_GRACE_SECONDS`` only if they ran long enough to
        cross ``UI_VISIBILITY_THRESHOLD_SECONDS`` — a 1-second async
        task that exited 30s ago shouldn't clutter the listing."""
        now = now if now is not None else time.time()
        visible: list[ShellJob] = []
        for job in self.all_jobs():
            if job.exit_code is None:
                visible.append(job)
                continue
            elapsed = (job.finished_at or now) - job.started_at
            if elapsed < UI_VISIBILITY_THRESHOLD_SECONDS:
                continue
            if (
                job.finished_at is not None
                and (now - job.finished_at) > POST_EXIT_GRACE_SECONDS
            ):
                continue
            visible.append(job)
        return self._sorted_jobs(visible)

    def read_output(
        self,
        job_id: str,
        *,
        tail_lines: int = SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES,
        stream: str = DEFAULT_SHELL_JOB_STREAM,
    ) -> dict:
        """Return tail of stdout/stderr for ``job_id``.

        ``stream`` ∈ {"stdout", "stderr", "both"}. Returns
        ``{"error": ...}`` for unknown jobs.

        **Sync I/O note (PR #111 review).** This method does file IO
        (seek-from-end tail, up to 10 MiB) and is intentionally
        synchronous — matches the codebase pattern for "small bounded
        IO that fits a loop tick." The MCP tool handler that calls
        this from an async context (``shelltools.bash_job_output``)
        wraps the call in ``asyncio.to_thread`` so the IO doesn't
        block the loop. Direct sync callers (tests, CLI scripts)
        keep working without async churn. If multiple async sites
        end up needing this, the right escalation is to split into
        ``_read_output_sync`` private + async ``read_output`` public,
        not to async-ify the whole call chain."""
        job = self.get(job_id)
        if job is None:
            return {"error": f"unknown job_id: {job_id}"}

        def _tail(path: Path, n: int) -> str:
            # CR2 (external I/O / Pattern A residual) fix: seek-from-end
            # streaming tail. Pre-fix this read the entire file with
            # ``read_bytes()``; a 1GB runaway log spiked memory on the
            # event loop. The "bounded by skill discipline" claim wasn't
            # a guarantee — any one buggy script (e.g. ``find /``) could
            # land the agent process OOM. Now we read backward in
            # 64 KiB chunks until N newlines are seen (or BOF), then
            # decode just the tail. Memory is O(n × avg_line_size)
            # regardless of file size.
            CHUNK = 65536
            MAX_BYTES = 10 * 1024 * 1024  # 10 MiB hard cap on tail bytes
            try:
                with path.open("rb") as f:
                    f.seek(0, 2)  # SEEK_END
                    pos = f.tell()
                    if n <= 0:
                        # Caller wants "all of it" — still cap at MAX_BYTES.
                        size = min(pos, MAX_BYTES)
                        f.seek(max(0, pos - size))
                        data = f.read(size)
                        prefix = "" if pos <= MAX_BYTES else (
                            f"[…truncated {pos - MAX_BYTES} bytes from head…]\n"
                        )
                        return prefix + data.decode("utf-8", errors="replace")
                    # Bounded line-count tail: read backward chunks
                    # until we have n+1 newlines (so we can drop the
                    # first partial line), hit BOF, or hit MAX_BYTES.
                    buf = b""
                    newline_count = 0
                    hit_byte_cap = False
                    while pos > 0 and newline_count <= n:
                        if len(buf) >= MAX_BYTES:
                            hit_byte_cap = True
                            break
                        read_size = min(CHUNK, pos)
                        pos -= read_size
                        f.seek(pos)
                        chunk = f.read(read_size)
                        buf = chunk + buf
                        newline_count = buf.count(b"\n")
                    text = buf.decode("utf-8", errors="replace")
                    lines = text.splitlines()
                    # PR #111 review fix: include a truncation marker
                    # whenever output is shorter than what's actually
                    # on disk. Pre-fix the line-count branch silently
                    # returned the trailing n lines with no signal —
                    # operator couldn't tell whether the file had
                    # exactly n lines or N >> n. Marker shape mirrors
                    # the n<=0 branch's "[...truncated X bytes...]"
                    # format above.
                    if pos == 0 and len(lines) <= n:
                        return text
                    kept = lines[-n:]
                    dropped_lines = len(lines) - len(kept)
                    # PR #111 review-fix-2: gate on ``dropped_lines``
                    # directly, not on ``hit_byte_cap or pos > 0``.
                    # The previous gate skipped the marker when the
                    # file fit in one CHUNK (pos==0, hit_byte_cap=False)
                    # and had >n lines — exactly the silent-truncation
                    # shape the original review flagged.
                    if dropped_lines > 0 or pos > 0:
                        prefix_parts = []
                        if dropped_lines > 0:
                            prefix_parts.append(
                                f"{dropped_lines} earlier line(s)"
                            )
                        if pos > 0:
                            prefix_parts.append(
                                f"{pos} earlier byte(s) on disk"
                            )
                        return (
                            f"[…truncated; {', '.join(prefix_parts)} "
                            f"not shown…]\n" + "\n".join(kept)
                        )
                    return "\n".join(kept)
            except FileNotFoundError:
                return ""

        out = _tail(job.stdout_path, tail_lines) if stream in ("stdout", "both") else ""
        err = _tail(job.stderr_path, tail_lines) if stream in ("stderr", "both") else ""
        result = job.snapshot()
        result["stdout_tail"] = out
        result["stderr_tail"] = err
        result["stdout_path"] = str(job.stdout_path)
        result["stderr_path"] = str(job.stderr_path)
        return result


# ─── argument normalization helpers (used by shelltools.py) ─────────────


def normalize_shell_job_scope(
    scope: str | None,
    *,
    default: str = DEFAULT_SHELL_JOB_SCOPE,
) -> str:
    resolved = (scope or default).strip().lower() or default
    if resolved not in VALID_SHELL_JOB_SCOPES:
        allowed = ", ".join(f'"{item}"' for item in sorted(VALID_SHELL_JOB_SCOPES))
        raise ValueError(f"scope must be one of: {allowed}")
    return resolved


def normalize_shell_job_stream(
    stream: str | None,
    *,
    default: str = DEFAULT_SHELL_JOB_STREAM,
) -> str:
    resolved = (stream or default).strip().lower() or default
    if resolved not in VALID_SHELL_JOB_STREAMS:
        allowed = ", ".join(f'"{item}"' for item in sorted(VALID_SHELL_JOB_STREAMS))
        raise ValueError(f"stream must be one of: {allowed}")
    return resolved


def parse_shell_job_tail_lines(
    raw_value: int | str | None,
    *,
    default: int = SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES,
) -> int:
    if raw_value is None:
        return default
    if isinstance(raw_value, str):
        if not raw_value.strip():
            return default
        try:
            tail_lines = int(raw_value)
        except ValueError as exc:
            raise ValueError("tail_lines must be an integer") from exc
    else:
        tail_lines = int(raw_value)
    if tail_lines <= 0:
        raise ValueError("tail_lines must be > 0")
    return min(tail_lines, SHELL_JOB_OUTPUT_MAX_TAIL_LINES)


def shell_job_snapshots(
    registry: ShellJobRegistry | None,
    *,
    scope: str = DEFAULT_SHELL_JOB_SCOPE,
) -> list[dict]:
    if registry is None:
        return []

    resolved_scope = normalize_shell_job_scope(scope)
    if resolved_scope == "running":
        jobs = registry.running_jobs()
    elif resolved_scope == "visible":
        jobs = registry.visible_jobs()
    else:
        jobs = registry._sorted_jobs(registry.all_jobs())
    return [job.snapshot() for job in jobs]


__all__: tuple[str, ...] = (
    "DEFAULT_SHELL_JOB_SCOPE",
    "DEFAULT_SHELL_JOB_STREAM",
    "EVICT_AFTER_SECONDS",
    "POST_EXIT_GRACE_SECONDS",
    "SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES",
    "SHELL_JOB_OUTPUT_MAX_TAIL_LINES",
    "UI_VISIBILITY_THRESHOLD_SECONDS",
    "VALID_SHELL_JOB_SCOPES",
    "VALID_SHELL_JOB_STREAMS",
    "ShellJob",
    "ShellJobRegistry",
    "normalize_shell_job_scope",
    "normalize_shell_job_stream",
    "parse_shell_job_tail_lines",
    "shell_job_snapshots",
)
