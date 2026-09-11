"""ShellJobRegistry behavior — spawn/capture/visibility/output.

Mirrors the upstream open-strix test patterns; mimir-side adaptations
drop ``channel_name`` (mimir uses just ``channel_id``)."""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import mimir.shell_jobs as shell_jobs
from mimir.shell_jobs import (
    EVICT_AFTER_SECONDS,
    POST_EXIT_GRACE_SECONDS,
    SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES,
    SHELL_JOB_OUTPUT_MAX_TAIL_LINES,
    UI_VISIBILITY_THRESHOLD_SECONDS,
    ShellJobRegistry,
    normalize_shell_job_scope,
    normalize_shell_job_stream,
    parse_shell_job_tail_lines,
    shell_job_snapshots,
)


def _make_registry(tmp_path: Path) -> ShellJobRegistry:
    return ShellJobRegistry(jobs_dir=tmp_path / "shell-jobs")


@contextmanager
def _held_job(registry: ShellJobRegistry, name: str):
    """Hold a real child until release; pytest-timeout bounds the whole protocol."""
    ready_path = registry.jobs_dir / f"{name}.ready"
    release_path = registry.jobs_dir / f"{name}.release"
    os.mkfifo(ready_path)
    os.mkfifo(release_path)
    ready = os.open(ready_path, os.O_RDWR)
    release = os.open(release_path, os.O_RDWR)
    completed = threading.Event()
    job = None
    try:
        job = registry.spawn(
            name,
            argv=[sys.executable, "-c",
                  "import sys; "
                  "release = open(sys.argv[2], 'rb', buffering=0); "
                  "ready = open(sys.argv[1], 'wb', buffering=0); "
                  "ready.write(b'R'); release.read(1)",
                  str(ready_path), str(release_path)],
            on_complete=lambda _job: completed.set(),
        )
        assert os.read(ready, 1) == b"R"
        assert job._process.poll() is None
        yield job
    finally:
        os.write(release, b"X")
        try:
            if job is not None:
                job._process.wait()
                completed.wait()
                assert job._process.poll() == 0
                assert job._process.stdout.closed and job._process.stderr.closed
        finally:
            if job is not None:
                job._process.stdout.close()
                job._process.stderr.close()
            os.close(ready)
            os.close(release)
            ready_path.unlink()
            release_path.unlink()


def _wait_until_done(registry: ShellJobRegistry, job_id: str) -> None:
    """Join only this job's lifecycle; pytest bounds the whole protocol."""
    job = registry.get(job_id)
    assert job is not None
    process = job._process
    assert process is not None
    process.wait()
    names = {f"shelljob-{role}-{job_id}" for role in ("wait", "out", "err")}
    owned = [thread for thread in threading.enumerate() if thread.name in names]
    for thread in owned:
        thread.join()
    assert not any(thread.is_alive() for thread in owned)
    assert process.poll() is not None
    assert job.exit_code == process.returncode
    assert process.stdout.closed and process.stderr.closed


# ─── basic spawn + capture ────────────────────────────────────────────


@pytest.mark.parametrize("limit", [10 * 1024 * 1024, 25])
def test_redaction_precedes_capture_and_tail_limits(tmp_path, monkeypatch, limit):
    import io

    secret = "prefix\nprivate-suffix"
    released = threading.Event()
    completed = threading.Event()
    events = []

    class ChunkedPipe(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 3))

    payload = ("header\n" + secret + "\nprivate-suffix\n").encode()
    proc = SimpleNamespace(
        pid=123,
        stdout=ChunkedPipe(payload),
        stderr=ChunkedPipe(payload),
        wait=lambda: (released.wait(), 0)[1],
    )
    monkeypatch.setattr("mimir.shell_jobs.subprocess.Popen", lambda *a, **kw: proc)
    monkeypatch.setattr("mimir.shell_jobs.SHELL_JOB_OUTPUT_MAX_BYTES_PER_STREAM", limit)
    registry = _make_registry(tmp_path)

    def on_complete(job):
        events.append(registry.read_job_output(job, tail_lines=1))
        completed.set()

    job = registry.spawn(
        "declared command", argv=["unused"], on_complete=on_complete,
        redact_values=("", "private-suffix", secret),
    )
    released.set()
    completed.wait()
    expected = b"header\n[REDACTED]\n[REDACTED]\n"[:limit]
    assert job.stdout_path.read_bytes() == expected
    assert job.stderr_path.read_bytes() == expected
    assert secret not in repr(job)
    assert "redact_values" not in job.snapshot()
    for result in [events[0], registry.read_job_output(job, tail_lines=1)]:
        assert "private-suffix" not in str(result)
        assert "prefix" not in str(result)


def test_running_output_withholds_split_secret(tmp_path, monkeypatch):
    import io

    paused = threading.Event()
    resume = threading.Event()
    completed = threading.Event()

    class PausedPipe(io.BytesIO):
        def read(self, size=-1):
            if self.tell() == 7:
                paused.set()
                resume.wait()
            return super().read(7)

    proc = SimpleNamespace(
        pid=123,
        stdout=PausedPipe(b"private-secret"),
        stderr=io.BytesIO(),
        wait=lambda: (resume.wait(), 0)[1],
    )
    monkeypatch.setattr("mimir.shell_jobs.subprocess.Popen", lambda *a, **kw: proc)
    registry = _make_registry(tmp_path)
    job = registry.spawn(
        "safe", argv=["unused"], redact_values=("private-secret",),
        on_complete=lambda job: completed.set(),
    )
    try:
        paused.wait()
        assert registry.read_job_output(job)["stdout_tail"] == ""
    finally:
        resume.set()
    completed.wait()
    assert registry.read_job_output(job)["stdout_tail"] == "[REDACTED]"


def test_undeclared_output_is_unchanged(tmp_path):
    registry = _make_registry(tmp_path)
    job = registry.spawn(
        "unscoped command", argv=[sys.executable, "-c", "print('private-value')"],
        env_overlay={"TOKEN": "private-value"},
    )
    _wait_until_done(registry, job.job_id)
    assert registry.read_job_output(job)["stdout_tail"] == "private-value\n"


def test_redacted_spawn_exception_does_not_echo_values(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private-value")

    monkeypatch.setattr("mimir.shell_jobs.subprocess.Popen", fail)
    registry = _make_registry(tmp_path)
    with pytest.raises(RuntimeError, match="spawn failed") as exc:
        registry.spawn("safe", argv=["unused"], redact_values=("private-value",))
    assert "private-value" not in str(exc.value)
    assert exc.value.__suppress_context__


def test_redacted_capture_error_does_not_log_values(tmp_path, monkeypatch, caplog):
    import io

    secret = "private-capture-secret"
    real_open = Path.open
    completed = threading.Event()
    fired = []

    class FailingOutput(io.BytesIO):
        def write(self, data):
            raise OSError(secret)

    def failing_open(path, mode="r", *args, **kwargs):
        if mode == "wb" and path.parent == registry.jobs_dir:
            return FailingOutput()
        return real_open(path, mode, *args, **kwargs)

    def on_complete(job):
        fired.append(job.job_id)
        completed.set()

    registry = _make_registry(tmp_path)
    monkeypatch.setattr(Path, "open", failing_open)
    job = registry.spawn(
        "safe", argv=[sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        redact_values=(secret,), on_complete=on_complete,
    )
    _wait_until_done(registry, job.job_id)
    assert completed.is_set()
    assert fired == [job.job_id]
    assert job.exit_code == 0
    assert job.stdout_truncated and job.stderr_truncated
    for stream in ("stdout", "stderr"):
        assert (
            f"shell job {job.job_id} {stream} capture failed; "
            "discarding remaining output: OSError"
        ) in caplog.text
    assert secret not in caplog.text


def test_redacted_callback_error_does_not_log_values(tmp_path, caplog):
    secret = "private-callback-secret"
    invoked = threading.Event()
    fired = []
    waiter_threads = []

    def on_complete(job):
        fired.append(job.job_id)
        waiter_threads.append(threading.current_thread())
        invoked.set()
        raise RuntimeError(secret)

    registry = _make_registry(tmp_path)
    job = registry.spawn(
        "safe", argv=[sys.executable, "-c", "pass"],
        redact_values=(secret,), on_complete=on_complete,
    )
    _wait_until_done(registry, job.job_id)
    assert invoked.is_set()
    assert not waiter_threads[0].is_alive()
    assert fired == [job.job_id]
    assert job.exit_code == 0
    assert f"on_complete callback raised for job {job.job_id}" in caplog.text
    assert secret not in caplog.text
    assert all(
        record.exc_info is None
        for record in caplog.records
        if record.name == "mimir.shell_jobs" and job.job_id in record.getMessage()
    )


def test_spawn_captures_stdout_and_stderr(tmp_path: Path):
    registry = _make_registry(tmp_path)
    cmd = "echo out; echo err 1>&2"
    job = registry.spawn(cmd, argv=["bash", "-c", cmd])
    _wait_until_done(registry, job.job_id)

    result = registry.read_job_output(job, tail_lines=10)
    assert result["status"] == "exited_ok"
    assert result["exit_code"] == 0
    assert "out" in result["stdout_tail"]
    assert "err" in result["stderr_tail"]
    assert result["output_truncated"] is False
    assert result["truncated_streams"] == []


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires procfs")
def test_finished_jobs_release_all_pipe_descriptors(tmp_path: Path):
    """Track the owned pipe identities, not an ambient descriptor-count delta."""
    registry = _make_registry(tmp_path)
    with ExitStack() as stack:
        jobs = [stack.enter_context(_held_job(registry, f"fd-{i}")) for i in range(10)]
        job_pipes = {
            os.readlink(f"/proc/self/fd/{stream.fileno()}")
            for job in jobs
            for stream in (job._process.stdout, job._process.stderr)
        }
        assert len(job_pipes) == 20

    # _held_job asserts closure at completion, before its defensive cleanup.
    assert all(job.exit_code == 0 for job in jobs)
    targets = set()
    for name in os.listdir("/proc/self/fd"):
        try:
            targets.add(os.readlink(f"/proc/self/fd/{name}"))
        except FileNotFoundError:
            pass
    assert job_pipes.isdisjoint(targets)


def test_output_write_failure_keeps_draining_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    real_open = Path.open
    failed_writes: list[int] = []

    class FailSecondWrite:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.writes = 0

        def write(self, data):
            self.writes += 1
            if self.writes == 2:
                failed_writes.append(len(data))
                raise OSError(28, "No space left on device")
            return self.wrapped.write(data)

        def flush(self):
            return self.wrapped.flush()

        def close(self):
            return self.wrapped.close()

    def failing_open(path: Path, mode: str = "r", *args, **kwargs):
        opened = real_open(path, mode, *args, **kwargs)
        if mode == "wb" and path.suffix == ".out":
            return FailSecondWrite(opened)
        return opened

    monkeypatch.setattr(Path, "open", failing_open)
    monkeypatch.setattr("mimir.shell_jobs.MAX_LIVE_SHELL_JOBS", 1)
    registry = _make_registry(tmp_path)
    completed = threading.Event()
    produced = tmp_path / "produced"
    job = registry.spawn(
        "large producer",
        argv=[
            sys.executable,
            "-c",
            "import os, sys; from pathlib import Path; "
            "count = sum(os.write(1, b'x' * 4096) for _ in range(64)); "
            "Path(sys.argv[1]).write_text(str(count))", str(produced),
        ],
        on_complete=lambda _job: completed.set(),
    )

    _wait_until_done(registry, job.job_id)
    assert completed.is_set()
    assert produced.read_text() == "262144"
    assert failed_writes, "the injected write failure did not execute"
    assert job.exit_code == 0
    assert job._process is not None and job._process.poll() == 0
    result = registry.read_job_output(job, stream="stdout")
    assert result["output_truncated"] is True
    assert result["truncated_streams"] == ["stdout"]
    assert result["stdout_tail"].startswith("[shell job output incomplete;")

    # Completion releases the sole live-job slot and makes the record evictable.
    next_job = registry.spawn("true", argv=[sys.executable, "-c", "pass"])
    _wait_until_done(registry, next_job.job_id)
    evicted = registry._evict_stale(now=job.finished_at + EVICT_AFTER_SECONDS)
    assert evicted[0].job_id == job.job_id


def test_output_write_bound_discards_excess_without_wedging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    limit = 8192
    monkeypatch.setattr(
        "mimir.shell_jobs.SHELL_JOB_OUTPUT_MAX_BYTES_PER_STREAM", limit,
    )
    registry = _make_registry(tmp_path)
    completed = threading.Event()
    produced = tmp_path / "produced"
    job = registry.spawn(
        "bounded producer",
        argv=[
            sys.executable,
            "-c",
            "import os, sys; from pathlib import Path; "
            "counts = [(os.write(1, b'y' * 4096), os.write(2, b'z' * 4096)) "
            "for _ in range(64)]; "
            "Path(sys.argv[1]).write_text(str(tuple(map(sum, zip(*counts)))))",
            str(produced),
        ],
        on_complete=lambda _job: completed.set(),
    )

    _wait_until_done(registry, job.job_id)
    assert completed.is_set()
    assert produced.read_text() == "(262144, 262144)"
    assert job.exit_code == 0
    assert job.stdout_path.stat().st_size == limit
    assert job.stderr_path.stat().st_size == limit
    assert (
        job.stdout_path.stat().st_size + job.stderr_path.stat().st_size
        == 2 * limit
    )
    result = registry.read_job_output(job, stream="both")
    assert result["output_truncated"] is True
    assert result["truncated_streams"] == ["stdout", "stderr"]
    warning = (
        "[shell job output incomplete; capture truncated for stdout, stderr]"
    )
    assert warning in result["stdout_tail"]
    assert warning in result["stderr_tail"]


def test_nonzero_exit_marked_exited_error(tmp_path: Path):
    registry = _make_registry(tmp_path)
    job = registry.spawn("false", argv=["bash", "-c", "exit 7"])
    _wait_until_done(registry, job.job_id)

    snapshot = job.snapshot()
    assert snapshot["status"] == "exited_error"
    assert snapshot["exit_code"] == 7


# ─── on_complete callback ─────────────────────────────────────────────


def test_on_complete_fires_after_exit(tmp_path: Path):
    registry = _make_registry(tmp_path)
    fired: list[str] = []
    event = threading.Event()

    def on_complete(job):
        fired.append(job.job_id)
        event.set()

    job = registry.spawn(
        "echo done",
        argv=["bash", "-c", "echo done"],
        on_complete=on_complete,
    )
    _wait_until_done(registry, job.job_id)
    assert event.is_set(), "on_complete didn't fire"
    assert fired == [job.job_id]
    # Snapshot has both fields populated by the time the callback runs.
    snap = job.snapshot()
    assert snap["exit_code"] == 0
    assert snap["status"] == "exited_ok"


def test_on_complete_error_isolated_from_registry(tmp_path: Path):
    """A misbehaving callback must not break the registry — subsequent
    spawns and reads still work."""
    registry = _make_registry(tmp_path)
    finished = threading.Event()

    def bad_callback(job):
        finished.set()
        raise RuntimeError("boom")

    job1 = registry.spawn("true", argv=["bash", "-c", "true"], on_complete=bad_callback)
    _wait_until_done(registry, job1.job_id)
    assert finished.is_set()
    # And the registry can still spawn / read.
    job2 = registry.spawn("echo two", argv=["bash", "-c", "echo two"])
    _wait_until_done(registry, job2.job_id)
    assert "two" in registry.read_job_output(job2)["stdout_tail"]


def test_on_complete_runs_for_nonzero_exit(tmp_path: Path):
    registry = _make_registry(tmp_path)
    fired = threading.Event()

    def on_complete(job):
        fired.set()

    job = registry.spawn(
        "exit-3",
        argv=["bash", "-c", "exit 3"],
        on_complete=on_complete,
    )
    _wait_until_done(registry, job.job_id)
    assert fired.is_set(), "on_complete must fire on nonzero exits"
    assert job.exit_code == 3


# ─── channel_id captured at spawn time ────────────────────────────────


def test_spawn_captures_channel_id(tmp_path: Path):
    registry = _make_registry(tmp_path)
    job = registry.spawn(
        "true",
        argv=["bash", "-c", "true"],
        channel_id="discord-99",
    )
    _wait_until_done(registry, job.job_id)
    assert job.channel_id == "discord-99"
    assert job.snapshot()["channel_id"] == "discord-99"


# ─── env_overlay + cwd ─────────────────────────────────────────────


def test_env_overlay_sets_value_visible_to_child(tmp_path: Path):
    """``env_overlay={"FOO": "bar"}`` → child sees ``FOO=bar``."""
    registry = _make_registry(tmp_path)
    job = registry.spawn(
        "echo $SHELL_JOB_TEST_VAR",
        argv=["bash", "-c", "echo $SHELL_JOB_TEST_VAR"],
        env_overlay={"SHELL_JOB_TEST_VAR": "from-overlay"},
    )
    _wait_until_done(registry, job.job_id)
    out = registry.read_job_output(job)["stdout_tail"]
    assert "from-overlay" in out


def test_env_overlay_none_unsets_inherited_var(tmp_path: Path, monkeypatch):
    """``env_overlay={"FOO": None}`` → child does NOT see ``FOO`` even
    when the parent inherited one. The None-means-unset semantic is
    load-bearing for subprocess callers: parent-only variables must be
    removable from the child environment."""
    registry = _make_registry(tmp_path)
    monkeypatch.setenv("SHELL_JOB_INHERIT_ME", "should-be-stripped")
    job = registry.spawn(
        "probe",
        argv=[
            "bash", "-c",
            'if [ -n "${SHELL_JOB_INHERIT_ME+x}" ]; then echo SET; '
            'else echo UNSET; fi',
        ],
        env_overlay={"SHELL_JOB_INHERIT_ME": None},
    )
    _wait_until_done(registry, job.job_id)
    out = registry.read_job_output(job)["stdout_tail"]
    assert "UNSET" in out
    assert "SET" not in out.replace("UNSET", "")


def test_env_overlay_default_inherits_parent_env(tmp_path: Path, monkeypatch):
    """No ``env_overlay`` argument → parent env passed through. Inverse
    of the above; protects against silently dropping the inherited env
    when the param is None vs an empty dict."""
    registry = _make_registry(tmp_path)
    monkeypatch.setenv("SHELL_JOB_INHERIT_PASSTHRU", "kept")
    job = registry.spawn(
        "probe",
        argv=["bash", "-c", "echo $SHELL_JOB_INHERIT_PASSTHRU"],
    )
    _wait_until_done(registry, job.job_id)
    out = registry.read_job_output(job)["stdout_tail"]
    assert "kept" in out


def test_cwd_kwarg_honored_by_subprocess(tmp_path: Path):
    """``cwd=path`` makes the child's working directory ``path``."""
    work = tmp_path / "work"
    work.mkdir()
    registry = _make_registry(tmp_path)
    job = registry.spawn(
        "pwd",
        argv=["bash", "-c", "pwd"],
        cwd=work,
    )
    _wait_until_done(registry, job.job_id)
    out = registry.read_job_output(job)["stdout_tail"].strip()
    # macOS resolves /var → /private/var; compare via realpath so the
    # test passes on both Linux and macOS dev environments.
    import os as _os
    assert _os.path.realpath(out) == _os.path.realpath(str(work))


# ─── visibility threshold ─────────────────────────────────────────────


def test_running_jobs_visible_immediately(tmp_path: Path):
    registry = _make_registry(tmp_path)
    with _held_job(registry, "visible") as job:
        visible = registry.visible_jobs()
        assert any(j.job_id == job.job_id for j in visible)
        running = registry.running_jobs()
        assert any(j.job_id == job.job_id for j in running)


def test_short_finished_job_not_visible_after_grace(tmp_path: Path):
    """A 100ms job that exited 30s ago must not appear in visible_jobs:
    it's below the 10s threshold AND past the post-exit grace."""
    registry = _make_registry(tmp_path)
    job = registry.spawn("quick", argv=["bash", "-c", "echo ok"])
    _wait_until_done(registry, job.job_id)
    job.started_at = job.finished_at - 0.1
    later = job.finished_at + POST_EXIT_GRACE_SECONDS + UI_VISIBILITY_THRESHOLD_SECONDS
    visible = registry.visible_jobs(now=later)
    assert not any(j.job_id == job.job_id for j in visible)


def test_finished_job_persists_in_all_jobs_after_grace(tmp_path: Path):
    """visible_jobs hides past-grace jobs; all_jobs and read_output still
    return them so the agent can retrieve old output."""
    registry = _make_registry(tmp_path)
    job = registry.spawn("persisted", argv=["bash", "-c", "echo persisted"])
    _wait_until_done(registry, job.job_id)
    later = job.finished_at + POST_EXIT_GRACE_SECONDS + UI_VISIBILITY_THRESHOLD_SECONDS
    assert any(j.job_id == job.job_id for j in registry.visible_jobs(now=later)) is False
    assert any(j.job_id == job.job_id for j in registry.all_jobs()) is True
    assert "persisted" in registry.read_job_output(job)["stdout_tail"]


# ─── read_output ──────────────────────────────────────────────────────


def test_read_output_returns_error_for_unknown_job(tmp_path: Path):
    registry = _make_registry(tmp_path)
    result = registry.read_output("j_doesnotexist", auth_context=None)
    assert "error" in result
    assert "j_doesnotexist" in result["error"]


def test_read_output_supports_stream_filter(tmp_path: Path):
    registry = _make_registry(tmp_path)
    cmd = "echo only-out; echo only-err 1>&2"
    job = registry.spawn(cmd, argv=["bash", "-c", cmd])
    _wait_until_done(registry, job.job_id)

    out_only = registry.read_job_output(job, stream="stdout")
    assert "only-out" in out_only["stdout_tail"]
    assert out_only["stderr_tail"] == ""

    err_only = registry.read_job_output(job, stream="stderr")
    assert err_only["stdout_tail"] == ""
    assert "only-err" in err_only["stderr_tail"]


def test_read_output_tail_lines_truncates(tmp_path: Path):
    """tail_lines=2 returns the last 2 lines of a 5-line stream,
    prefixed by the truncation marker (PR #111 review-fix-2)."""
    registry = _make_registry(tmp_path)
    cmd = "for i in 1 2 3 4 5; do echo line-$i; done"
    job = registry.spawn(cmd, argv=["bash", "-c", cmd])
    _wait_until_done(registry, job.job_id)

    result = registry.read_job_output(job, tail_lines=2, stream="stdout")
    lines = result["stdout_tail"].splitlines()
    # First line is the truncation marker; followed by the kept tail.
    assert lines[0].startswith("[…truncated;")
    assert "3 earlier line(s)" in lines[0]
    assert lines[1:] == ["line-4", "line-5"]


# ─── normalization helpers ────────────────────────────────────────────


def test_normalize_shell_job_scope_accepts_valid():
    assert normalize_shell_job_scope("running") == "running"
    assert normalize_shell_job_scope("VISIBLE") == "visible"
    assert normalize_shell_job_scope("  all  ") == "all"
    assert normalize_shell_job_scope(None) == "running"
    assert normalize_shell_job_scope("") == "running"


def test_normalize_shell_job_scope_rejects_invalid():
    with pytest.raises(ValueError, match="scope must be one of"):
        normalize_shell_job_scope("nope")


def test_normalize_shell_job_stream_accepts_valid():
    assert normalize_shell_job_stream("stdout") == "stdout"
    assert normalize_shell_job_stream("STDERR") == "stderr"
    assert normalize_shell_job_stream(None) == "both"


def test_normalize_shell_job_stream_rejects_invalid():
    with pytest.raises(ValueError, match="stream must be one of"):
        normalize_shell_job_stream("messages")


def test_parse_shell_job_tail_lines_accepts_valid():
    assert parse_shell_job_tail_lines(50) == 50
    assert parse_shell_job_tail_lines("100") == 100
    assert parse_shell_job_tail_lines(None) == SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES
    assert parse_shell_job_tail_lines("") == SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES


def test_parse_shell_job_tail_lines_clamps_to_max():
    huge = SHELL_JOB_OUTPUT_MAX_TAIL_LINES + 1000
    assert parse_shell_job_tail_lines(huge) == SHELL_JOB_OUTPUT_MAX_TAIL_LINES


def test_parse_shell_job_tail_lines_rejects_zero_and_negative():
    with pytest.raises(ValueError, match="must be > 0"):
        parse_shell_job_tail_lines(0)
    with pytest.raises(ValueError, match="must be > 0"):
        parse_shell_job_tail_lines(-5)


def test_parse_shell_job_tail_lines_rejects_non_numeric_string():
    with pytest.raises(ValueError, match="must be an integer"):
        parse_shell_job_tail_lines("abc")


# ─── shell_job_snapshots scope dispatch ───────────────────────────────


def test_shell_job_snapshots_running_scope_filters(tmp_path: Path):
    registry = _make_registry(tmp_path)
    auth = SimpleNamespace(
        channel_id="test-channel", canonical_principal="tester",
        principal="tester", is_service=False,
    )
    completed = threading.Event()
    finished = registry.spawn(
        "done", argv=["bash", "-c", "true"],
        on_complete=lambda _job: completed.set(),
    )
    completed.wait()
    assert finished.exit_code == 0
    with _held_job(registry, "running") as running:
        for job in (finished, running):
            job.channel_id = auth.channel_id
            job.auth_context = auth

        running_snaps = shell_job_snapshots(
            registry, auth_context=auth, scope="running",
        )
        running_ids = [s["job_id"] for s in running_snaps]
        assert running.job_id in running_ids
        assert finished.job_id not in running_ids

        all_snaps = shell_job_snapshots(registry, auth_context=auth, scope="all")
        all_ids = [s["job_id"] for s in all_snaps]
        assert running.job_id in all_ids
        assert finished.job_id in all_ids


def test_shell_job_snapshots_returns_empty_when_no_registry():
    # Caller passes None when shell jobs are disabled — must not blow up.
    assert shell_job_snapshots(None, auth_context=None) == []


# ─── PR #111 review-fix-2: read_output truncation marker ──────────────


def test_read_output_marker_fires_when_file_fits_one_chunk_but_has_extra_lines(
    tmp_path: Path,
):
    """PR #111 re-review regression: pre-fix the marker was gated on
    ``hit_byte_cap or pos > 0``, which skipped the marker when the
    whole file fit in one 64 KiB chunk AND had more than `n` lines
    (pos==0 after seeking, no byte cap hit, dropped_lines>0). The
    original review flagged this exact silent-truncation shape; the
    fix-push reintroduced it in different form. This test pins the
    correct gate ``dropped_lines > 0 or pos > 0``."""
    registry = _make_registry(tmp_path)
    job = registry.spawn(
        "small-many-lines",
        argv=["bash", "-c", "for i in $(seq 1 100); do echo line_$i; done"],
    )
    _wait_until_done(registry, job.job_id)
    # File is 100 lines × ~10 bytes ≈ 1 KB — fits in one CHUNK (64KB).
    out = registry.read_job_output(job, tail_lines=5, stream="stdout")
    assert out["status"] == "exited_ok"
    tail = out["stdout_tail"]
    # We asked for 5 lines; file has 100 → 95 dropped → marker MUST fire.
    assert "[…truncated;" in tail
    assert "earlier line(s)" in tail
    # The kept tail must be exactly the last 5 lines.
    lines = [
        line for line in tail.splitlines()
        if not line.startswith("[…truncated;")
    ]
    assert len(lines) == 5
    assert lines[-1] == "line_100"
    assert lines[0] == "line_96"


def test_read_output_no_marker_when_file_has_fewer_lines_than_tail(
    tmp_path: Path,
):
    """When the file is shorter than the requested tail, no marker —
    nothing was actually truncated."""
    registry = _make_registry(tmp_path)
    job = registry.spawn(
        "few-lines",
        argv=["bash", "-c", "for i in $(seq 1 5); do echo line_$i; done"],
    )
    _wait_until_done(registry, job.job_id)
    out = registry.read_job_output(job, tail_lines=100, stream="stdout")
    tail = out["stdout_tail"]
    assert "[…truncated;" not in tail
    assert "line_1" in tail
    assert "line_5" in tail


# ─── ShellJobRegistry._evict_stale (chainlink #256) ──────────────────


def test_evict_stale_removes_old_finished_job_and_unlinks_files(tmp_path: Path):
    """A finished job whose ``finished_at`` is >= EVICT_AFTER_SECONDS ago
    must be removed from ``_jobs`` and its output files must be unlinked."""
    registry = _make_registry(tmp_path)
    job = registry.spawn("echo evict-me", argv=["bash", "-c", "echo evict-me"])
    _wait_until_done(registry, job.job_id)

    # Confirm output files exist before eviction.
    assert job.stdout_path.exists()
    assert job.stderr_path.exists()

    # Wind the clock forward past the eviction window.
    stale_now = job.finished_at + EVICT_AFTER_SECONDS
    evicted = registry._evict_stale(now=stale_now)

    assert len(evicted) == 1
    assert evicted[0].job_id == job.job_id
    # Job must be gone from the registry.
    assert registry.get(job.job_id) is None
    assert not any(j.job_id == job.job_id for j in registry.all_jobs())
    # Output files must be unlinked.
    assert not job.stdout_path.exists()
    assert not job.stderr_path.exists()


def test_evict_stale_preserves_running_job(tmp_path: Path):
    """A running job (exit_code is None) must never be evicted, regardless
    of how far the clock advances."""
    registry = _make_registry(tmp_path)
    with _held_job(registry, "preserved") as job:
        far_future = time.time() + EVICT_AFTER_SECONDS * 10
        evicted = registry._evict_stale(now=far_future)

        assert len(evicted) == 0
        assert registry.get(job.job_id) is not None


def test_evict_stale_preserves_recently_finished_job(tmp_path: Path):
    """A finished job whose ``finished_at`` is well within EVICT_AFTER_SECONDS
    must not be evicted — the agent still has a retrieval window."""
    registry = _make_registry(tmp_path)
    job = registry.spawn("echo keep-me", argv=["bash", "-c", "echo keep-me"])
    _wait_until_done(registry, job.job_id)

    evicted = registry._evict_stale(now=job.finished_at)

    assert len(evicted) == 0
    assert registry.get(job.job_id) is not None


def test_registry_startup_reclaims_stale_restart_residue(tmp_path: Path):
    jobs_dir = tmp_path / "shell-jobs"
    jobs_dir.mkdir()
    stale = [jobs_dir / "j_pre_restart.out", jobs_dir / "j_pre_restart.err"]
    recent = jobs_dir / "j_recent.out"
    unrelated = jobs_dir / "keep.txt"
    for path in (*stale, recent, unrelated):
        path.write_text("log", encoding="utf-8")
    old = time.time() - EVICT_AFTER_SECONDS - 1
    for path in stale:
        os.utime(path, (old, old))

    registry = ShellJobRegistry(jobs_dir)

    assert registry.all_jobs() == []
    assert all(not path.exists() for path in stale)
    assert recent.exists()
    assert unrelated.exists()


def test_scheduled_eviction_does_not_require_later_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    ticks = queue.Queue()
    waiting = queue.Queue()
    clock = [1000.0]

    class ManualCondition:
        # The test serializes registry changes before issuing each sweep tick.
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def notify(self):
            pass

        def wait(self, timeout=None):
            waiting.put(timeout)
            if ticks.get() == "stop":
                raise StopIteration

    class SteppedSweeper(shell_jobs._RegistrySweeper):
        def _run(self):
            try:
                super()._run()
            except StopIteration:
                pass

    sweeper = SteppedSweeper()
    sweeper._condition = ManualCondition()
    monkeypatch.setattr(shell_jobs, "_REGISTRY_SWEEPER", sweeper)
    monkeypatch.setattr(shell_jobs, "time", SimpleNamespace(time=lambda: clock[0]))
    registry = _make_registry(tmp_path)
    try:
        waiting.get()
        job = registry.spawn("done", argv=[sys.executable, "-c", "pass"])
        _wait_until_done(registry, job.job_id)
        assert registry.get(job.job_id) is job
        assert job.stdout_path.exists() and job.stderr_path.exists()

        clock[0] = job.finished_at + EVICT_AFTER_SECONDS
        ticks.put("sweep")
        waiting.get()  # The entire sweep, including unlinking, has returned.
        assert registry.get(job.job_id) is None
        assert not job.stdout_path.exists()
        assert not job.stderr_path.exists()
    finally:
        ticks.put("stop")
        sweeper._thread.join()
        assert not sweeper._thread.is_alive()


def test_spawn_failure_removes_opened_output_files(tmp_path: Path):
    registry = _make_registry(tmp_path)
    with pytest.raises(FileNotFoundError):
        registry.spawn("missing", argv=[str(tmp_path / "does-not-exist")])

    assert list(registry.jobs_dir.iterdir()) == []


def test_spawn_triggers_eviction_of_old_jobs(tmp_path: Path, monkeypatch):
    """spawn() must call _evict_stale so stale entries are removed as a
    side-effect of adding new work (no separate background thread needed)."""
    # Only eager eviction is under test; a background sweep must not satisfy it.
    monkeypatch.setattr(shell_jobs, "_REGISTRY_SWEEPER", SimpleNamespace(
        register=lambda registry: None, wake=lambda: None,
    ))
    clock = [1000.0]
    monkeypatch.setattr(shell_jobs, "time", SimpleNamespace(time=lambda: clock[0]))
    registry = _make_registry(tmp_path)
    old_job = registry.spawn("echo old", argv=["bash", "-c", "echo old"])
    _wait_until_done(registry, old_job.job_id)
    clock[0] = old_job.finished_at + EVICT_AFTER_SECONDS

    # A new spawn must trigger eviction of the old job.
    new_job = registry.spawn("echo new", argv=["bash", "-c", "echo new"])
    _wait_until_done(registry, new_job.job_id)

    assert registry.get(old_job.job_id) is None, "old job must be evicted by spawn()"
    assert registry.get(new_job.job_id) is not None, "new job must still be in registry"


# ─── chainlink #387: stuck-job leak + job cap ──────────────────────────


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires procfs for descendant liveness")
def test_backgrounded_grandchild_does_not_block_waiter(tmp_path: Path, monkeypatch):
    """chainlink #387: a job whose process backgrounds a grandchild that keeps
    the stdout/stderr pipe open must still be marked finished within the bounded
    drain-join window — not stuck status=running forever (which pre-fix also
    leaked the job + its drainer threads + pipe FDs)."""
    registry = _make_registry(tmp_path)
    job_id = registry._make_job_id()
    monkeypatch.setattr(registry, "_make_job_id", lambda: job_id)
    waiter_done = threading.Event()
    joins = []
    real_join = threading.Thread.join

    def observe_join(thread, timeout=None):
        if threading.current_thread().name == f"shelljob-wait-{job_id}":
            joins.append(timeout)
            # An unbounded-join regression is an assertion failure, not a hang.
            # Normal bounded joins still execute against the real held pipes.
            if timeout is None:
                return
        return real_join(thread, timeout)

    monkeypatch.setattr(threading.Thread, "join", observe_join)
    ready_path, release_path = tmp_path / "ready", tmp_path / "release"
    os.mkfifo(ready_path)
    os.mkfifo(release_path)
    ready = os.open(ready_path, os.O_RDWR)
    release = os.open(release_path, os.O_RDWR)
    grandchild = None
    job = None
    owned = []

    def descendant_alive():
        try:
            status = Path(f"/proc/{grandchild}/status").read_text()
        except FileNotFoundError:
            return False
        return not any(
            line.startswith("State:") and "Z" in line for line in status.splitlines()
        )

    try:
        job = registry.spawn(
            "bg", argv=[sys.executable, "-c",
                "import os, sys\n"
                "if os.fork(): os._exit(0)\n"
                "release = open(sys.argv[2], 'rb', buffering=0)\n"
                "with open(sys.argv[1], 'w') as ready:\n"
                "    ready.write(str(os.getpid()) + '\\n')\n"
                "release.read(1)\n",
                str(ready_path), str(release_path)],
            on_complete=lambda _job: waiter_done.set(),
        )
        with os.fdopen(os.dup(ready)) as reader:
            grandchild = int(reader.readline())
        names = {f"shelljob-{role}-{job_id}" for role in ("wait", "out", "err")}
        owned = [thread for thread in threading.enumerate() if thread.name in names]
        assert descendant_alive()
        waiter_done.wait()
        assert joins and all(
            timeout is not None and 0 < timeout <= shell_jobs.DRAIN_JOIN_TIMEOUT_SECONDS
            for timeout in joins
        ), f"waiter requested unbounded drain joins: {joins}"
        assert job.exit_code == job._process.poll() == 0
        assert descendant_alive(), "pipe holder exited before the completion assertion"
        assert job._process.stdout.closed and job._process.stderr.closed
    finally:
        os.write(release, b"X")
        if job is not None:
            job._process.wait()
        for thread in owned:
            real_join(thread)
        if grandchild is not None:
            while descendant_alive():
                time.sleep(0.05)
            assert not descendant_alive()
        assert not any(thread.is_alive() for thread in owned)
        os.close(ready)
        os.close(release)
        ready_path.unlink()
        release_path.unlink()


def test_spawn_refuses_beyond_live_job_cap(tmp_path: Path, monkeypatch):
    """chainlink #387: spawning past the concurrently-live cap is refused with a
    clear error (the bash_async tool surfaces it)."""
    monkeypatch.setattr("mimir.shell_jobs.MAX_LIVE_SHELL_JOBS", 2)
    registry = _make_registry(tmp_path)
    with _held_job(registry, "s1"), _held_job(registry, "s2"):
        with pytest.raises(RuntimeError, match="too many live shell jobs"):
            # If admission regresses, this extra child still exits on its own.
            completed = threading.Event()
            registry.spawn(
                "s3", argv=[sys.executable, "-c", "pass"],
                on_complete=lambda _job: completed.set(),
            )
            completed.wait()
