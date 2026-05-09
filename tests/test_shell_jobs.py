"""ShellJobRegistry behavior — spawn/capture/visibility/output.

Mirrors the upstream open-strix test patterns; mimir-side adaptations
drop ``channel_name`` (mimir uses just ``channel_id``)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from mimir.shell_jobs import (
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


def _wait_until_done(registry: ShellJobRegistry, job_id: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = registry.get(job_id)
        if job is not None and job.exit_code is not None:
            return
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not exit within {timeout}s")


# ─── basic spawn + capture ────────────────────────────────────────────


def test_spawn_captures_stdout_and_stderr(tmp_path: Path):
    registry = _make_registry(tmp_path)
    cmd = "echo out; echo err 1>&2"
    job = registry.spawn(cmd, argv=["bash", "-c", cmd])
    _wait_until_done(registry, job.job_id)

    result = registry.read_output(job.job_id, tail_lines=10)
    assert result["status"] == "exited_ok"
    assert result["exit_code"] == 0
    assert "out" in result["stdout_tail"]
    assert "err" in result["stderr_tail"]


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
    assert event.wait(timeout=5.0), "on_complete didn't fire"
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
    assert finished.wait(timeout=5.0)
    # Brief wait for the waiter thread to handle the post-callback
    # registry update — exit_code is set BEFORE the callback fires, so
    # the job is already marked done.
    _wait_until_done(registry, job1.job_id)
    # And the registry can still spawn / read.
    job2 = registry.spawn("echo two", argv=["bash", "-c", "echo two"])
    _wait_until_done(registry, job2.job_id)
    assert "two" in registry.read_output(job2.job_id)["stdout_tail"]


def test_on_complete_runs_for_nonzero_exit(tmp_path: Path):
    registry = _make_registry(tmp_path)
    fired = threading.Event()

    def on_complete(job):
        fired.set()

    registry.spawn(
        "exit-3",
        argv=["bash", "-c", "exit 3"],
        on_complete=on_complete,
    )
    assert fired.wait(timeout=5.0), "on_complete must fire on nonzero exits"


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


# ─── env_overlay + cwd (chainlink #60: spawn_claude_code uses these) ──


def test_env_overlay_sets_value_visible_to_child(tmp_path: Path):
    """``env_overlay={"FOO": "bar"}`` → child sees ``FOO=bar``."""
    registry = _make_registry(tmp_path)
    job = registry.spawn(
        "echo $SHELL_JOB_TEST_VAR",
        argv=["bash", "-c", "echo $SHELL_JOB_TEST_VAR"],
        env_overlay={"SHELL_JOB_TEST_VAR": "from-overlay"},
    )
    _wait_until_done(registry, job.job_id)
    out = registry.read_output(job.job_id)["stdout_tail"]
    assert "from-overlay" in out


def test_env_overlay_none_unsets_inherited_var(tmp_path: Path, monkeypatch):
    """``env_overlay={"FOO": None}`` → child does NOT see ``FOO`` even
    when the parent inherited one. The None-means-unset semantic is
    load-bearing for ``spawn_claude_code``: ``CLAUDECODE`` is set in
    mimir's container and must be stripped so the spawn doesn't think
    it's nested in a parent Claude Code session."""
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
    out = registry.read_output(job.job_id)["stdout_tail"]
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
    out = registry.read_output(job.job_id)["stdout_tail"]
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
    out = registry.read_output(job.job_id)["stdout_tail"].strip()
    # macOS resolves /var → /private/var; compare via realpath so the
    # test passes on both Linux and macOS dev environments.
    import os as _os
    assert _os.path.realpath(out) == _os.path.realpath(str(work))


# ─── visibility threshold ─────────────────────────────────────────────


def test_running_jobs_visible_immediately(tmp_path: Path):
    registry = _make_registry(tmp_path)
    job = registry.spawn(
        "sleep-job",
        argv=["bash", "-c", "sleep 2"],
    )
    # Visible right away — even though the visibility threshold for
    # exited jobs is 10s, running jobs surface immediately.
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
    # Walk the clock forward so the visibility-threshold + grace logic
    # treats this job as past-the-grace-window.
    later = time.time() + POST_EXIT_GRACE_SECONDS + UI_VISIBILITY_THRESHOLD_SECONDS + 5
    visible = registry.visible_jobs(now=later)
    assert not any(j.job_id == job.job_id for j in visible)


def test_finished_job_persists_in_all_jobs_after_grace(tmp_path: Path):
    """visible_jobs hides past-grace jobs; all_jobs and read_output still
    return them so the agent can retrieve old output."""
    registry = _make_registry(tmp_path)
    job = registry.spawn("persisted", argv=["bash", "-c", "echo persisted"])
    _wait_until_done(registry, job.job_id)
    later = time.time() + 9999
    assert any(j.job_id == job.job_id for j in registry.visible_jobs(now=later)) is False
    assert any(j.job_id == job.job_id for j in registry.all_jobs()) is True
    assert "persisted" in registry.read_output(job.job_id)["stdout_tail"]


# ─── read_output ──────────────────────────────────────────────────────


def test_read_output_returns_error_for_unknown_job(tmp_path: Path):
    registry = _make_registry(tmp_path)
    result = registry.read_output("j_doesnotexist")
    assert "error" in result
    assert "j_doesnotexist" in result["error"]


def test_read_output_supports_stream_filter(tmp_path: Path):
    registry = _make_registry(tmp_path)
    cmd = "echo only-out; echo only-err 1>&2"
    job = registry.spawn(cmd, argv=["bash", "-c", cmd])
    _wait_until_done(registry, job.job_id)

    out_only = registry.read_output(job.job_id, stream="stdout")
    assert "only-out" in out_only["stdout_tail"]
    assert out_only["stderr_tail"] == ""

    err_only = registry.read_output(job.job_id, stream="stderr")
    assert err_only["stdout_tail"] == ""
    assert "only-err" in err_only["stderr_tail"]


def test_read_output_tail_lines_truncates(tmp_path: Path):
    """tail_lines=2 returns the last 2 lines of a 5-line stream."""
    registry = _make_registry(tmp_path)
    cmd = "for i in 1 2 3 4 5; do echo line-$i; done"
    job = registry.spawn(cmd, argv=["bash", "-c", cmd])
    _wait_until_done(registry, job.job_id)

    result = registry.read_output(job.job_id, tail_lines=2, stream="stdout")
    lines = result["stdout_tail"].splitlines()
    assert lines == ["line-4", "line-5"]


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
    finished = registry.spawn("done", argv=["bash", "-c", "true"])
    _wait_until_done(registry, finished.job_id)
    sleeping = registry.spawn("sleeping", argv=["bash", "-c", "sleep 2"])

    running_snaps = shell_job_snapshots(registry, scope="running")
    running_ids = [s["job_id"] for s in running_snaps]
    assert sleeping.job_id in running_ids
    assert finished.job_id not in running_ids

    all_snaps = shell_job_snapshots(registry, scope="all")
    all_ids = [s["job_id"] for s in all_snaps]
    assert sleeping.job_id in all_ids
    assert finished.job_id in all_ids


def test_shell_job_snapshots_returns_empty_when_no_registry():
    # Caller passes None when shell jobs are disabled — must not blow up.
    assert shell_job_snapshots(None) == []
