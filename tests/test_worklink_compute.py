from __future__ import annotations

import asyncio
import errno
import os
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from mimir.worklink import compute, run_state


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [errno.EPERM, errno.ESRCH, errno.EINVAL], ids=["EPERM", "ESRCH", "EINVAL"])
async def test_external_process_wait_kill_guard(monkeypatch, error) -> None:
    failure = OSError(error, os.strerror(error))
    kill = Mock(side_effect=failure)
    sleep = AsyncMock(side_effect=AssertionError("wait must stop after signal error"))
    monkeypatch.setattr(compute.os, "kill", kill)
    monkeypatch.setattr(run_state, "process_is_zombie", Mock(return_value=False))
    monkeypatch.setattr(compute.asyncio, "sleep", sleep)

    process = compute._ExternalProcess(4321)
    if error != errno.ESRCH:
        # EPERM must fail loudly, not return a successful wait for a live PID.
        with pytest.raises(PermissionError if error == errno.EPERM else OSError) as raised:
            await process.wait()
        assert raised.value is failure
    else:
        assert await process.wait() is None
    kill.assert_called_once_with(4321, 0)
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_external_process_cancel_propagates_probe_permission_error(monkeypatch) -> None:
    from mimir.worklink import worker_exec

    failure = PermissionError(errno.EPERM, "not our process")
    kill = Mock(side_effect=failure)
    terminate = Mock()
    monkeypatch.setattr(compute.os, "getpgid", Mock(return_value=4321))
    monkeypatch.setattr(compute.os, "kill", kill)
    monkeypatch.setattr(run_state, "process_is_zombie", Mock(return_value=False))
    monkeypatch.setattr(worker_exec, "_terminate_process_group_pid", terminate)

    with pytest.raises(PermissionError) as raised:
        await compute._kill_process_group(compute._ExternalProcess(4321))

    assert raised.value is failure
    terminate.assert_called_once_with(4321)
    kill.assert_called_once_with(4321, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("check_raises", [False, True], ids=["inflight", "inflight-error"])
async def test_cleanup_after_wait_error_drains_live_monitor(
    monkeypatch, tmp_path, check_raises
) -> None:
    backend = compute.LocalSubprocessComputeBackend()
    failure = RuntimeError("process wait failed")
    proc = Mock(pid=None, wait=AsyncMock(side_effect=failure))
    monkeypatch.setattr(compute.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    spec = compute.WorkSpec(
        issue_id=1, attempt=1, repo_url="", base_ref="main", branch="test",
        prompt="", rules=None, test_command="", backend="test", timeout_s=10,
        local_checkout=tmp_path, local_argv=("unused",),
    )
    handle = await backend.launch(spec)
    capture = backend._direct_captures[handle.identifier]
    loop = asyncio.get_running_loop()
    checking = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    check_errors = []
    original_overflowed = capture.stdout_sink.overflowed

    def blocked_check():
        loop.call_soon_threadsafe(checking.set)
        try:
            if not release.wait(5):
                raise AssertionError("test did not release output check")
            original_overflowed()
            if check_raises:
                raise RuntimeError("output check failed")
            return False
        except OSError as exc:
            check_errors.append(exc)
            raise
        finally:
            finished.set()

    monkeypatch.setattr(capture.stdout_sink, "overflowed", blocked_check)
    close_observations = []
    for sink in (capture.stdout_sink, capture.stderr_sink):
        original_close = sink.close

        def close(original_close=original_close):
            close_observations.append((
                capture.monitor_task.done(), capture.collect_task.done(), finished.is_set(),
            ))
            original_close()

        monkeypatch.setattr(sink, "close", close)

    cleanup_task = None
    try:
        await asyncio.wait_for(checking.wait(), 5)
        with pytest.raises(RuntimeError, match="process wait failed"):
            await backend.wait(handle, 10)
        assert not capture.monitor_task.done()
        cleanup_task = asyncio.create_task(backend.cleanup(handle))
        await asyncio.sleep(0)
        assert not cleanup_task.done()
        assert capture.stdout_sink.fd >= 0
        release.set()
        await asyncio.wait_for(cleanup_task, 5)
        assert close_observations == [(True, True, True), (True, True, True)]
        assert capture.monitor_task.cancelled()
        assert not capture.collect_task._log_traceback
        assert not check_errors
        assert handle.identifier not in backend._direct_captures
    finally:
        release.set()
        tasks = [capture.monitor_task, capture.collect_task]
        if cleanup_task is not None:
            tasks.append(cleanup_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        capture.stdout_sink.close()
        capture.stderr_sink.close()


@pytest.mark.asyncio
async def test_cleanup_drains_kill_task_published_during_monitor_teardown() -> None:
    backend = compute.LocalSubprocessComputeBackend()
    stdout, stderr = compute.open_output_pair(None, 1024, None, 1024)
    capture = compute._DirectCapture(stdout, stderr)
    handle = compute.LaunchHandle(backend.name, "test")
    backend._handles[handle.identifier] = handle
    backend._direct_captures[handle.identifier] = capture
    started = asyncio.Event()
    stopped = []

    async def pending(name):
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            assert stdout.fd >= 0 and stderr.fd >= 0
            stopped.append(name)
            raise RuntimeError(f"{name} teardown failed")

    async def monitor():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            capture.kill_task = asyncio.create_task(pending("kill"))
            await asyncio.sleep(0)

    capture.collect_task = asyncio.create_task(pending("collect"))
    capture.monitor_task = asyncio.create_task(monitor())
    try:
        await started.wait()
        await backend.cleanup(handle)
        assert sorted(stopped) == ["collect", "kill"]
        assert capture.monitor_task.cancelled()
        # A task with an unretrieved exception still has _log_traceback set.
        for task in (capture.collect_task, capture.kill_task):
            assert task.done()
            assert not task._log_traceback
        assert stdout.fd == stderr.fd == -1
    finally:
        tasks = [capture.monitor_task, capture.collect_task]
        if capture.kill_task is not None:
            tasks.append(capture.kill_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stdout.close()
        stderr.close()
