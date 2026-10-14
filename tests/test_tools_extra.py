from __future__ import annotations

import errno
import io
import os
import signal
import subprocess
from unittest.mock import Mock, call

import pytest

from mimir.tools import extra


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize("error", [PermissionError, ProcessLookupError, OSError],
                         ids=["EPERM", "ESRCH", "EINVAL"])
def test_bounded_project_test_killpg_guard(monkeypatch, error):
    timeout = subprocess.TimeoutExpired(["pytest"], 1)
    proc = Mock(pid=12345, stdout=io.BytesIO(b"out"), stderr=io.BytesIO(b"err"))
    proc.wait.side_effect = [timeout, -signal.SIGKILL]
    monkeypatch.setattr(extra.subprocess, "Popen", Mock(return_value=proc))
    exc = error(errno.EINVAL if error is OSError else
                errno.EPERM if error is PermissionError else errno.ESRCH, "signal failed")
    killpg = Mock(side_effect=exc)
    monkeypatch.setattr(extra.os, "killpg", killpg)
    monkeypatch.setattr(extra.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(extra.time, "sleep", lambda _seconds: None)

    expected = exc if error is OSError else timeout
    with pytest.raises(type(expected)) as caught:
        extra._run_bounded_project_test(["pytest"], cwd=None, timeout=1, env={})
    assert caught.value is expected
    assert proc.wait.call_args_list == [call(timeout=1), call(timeout=1)]
    if error is PermissionError:
        proc.kill.assert_called_once()
        assert "wrapper-only" in timeout.cleanup_message
        assert "descendants may still be running" in timeout.cleanup_message
    if error is OSError:
        killpg.assert_called_once_with(proc.pid, signal.SIGTERM)
    else:
        assert killpg.call_args_list == [call(proc.pid, signal.SIGTERM)]
    assert proc.stdout.closed and proc.stderr.closed


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_shell_timeout_never_signals_server_process_group(monkeypatch):
    killpg = Mock()
    monkeypatch.setattr(extra.os, "killpg", killpg)
    proc = Mock(pid=os.getpgrp() + 100000)
    monkeypatch.setattr(extra.os, "getpgid", lambda pid: os.getpgrp())

    message = extra._kill_shell_process_group(proc)

    assert "not isolated" in message
    assert "wrapper-only" in message
    proc.kill.assert_called_once()
    killpg.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_shell_timeout_sends_term_then_kill_even_if_wrapper_exited(monkeypatch):
    proc = Mock(pid=os.getpgrp() + 100000)
    killpg = Mock()
    monkeypatch.setattr(extra.os, "killpg", killpg)
    monkeypatch.setattr(extra.time, "sleep", lambda _seconds: None)

    extra._kill_shell_process_group(proc)

    assert killpg.call_args_list == [
        call(proc.pid, signal.SIGTERM), call(proc.pid, signal.SIGKILL),
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize("runner", [extra._run_shell_command, extra._run_bounded_project_test])
@pytest.mark.parametrize("failure", ["permission", "shared-group"])
def test_timeout_fallback_reaps_wrapper_without_waiting_for_descendant_pipes(
    tmp_path, monkeypatch, runner, failure,
):
    import sys
    import time

    pidfile = tmp_path / "child.pid"
    processes = []
    popen = subprocess.Popen

    def launch(*args, **kwargs):
        if failure == "shared-group":
            kwargs["start_new_session"] = False
        proc = popen(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(extra.subprocess, "Popen", launch)
    killpg = Mock(side_effect=PermissionError("denied"))
    monkeypatch.setattr(extra.os, "killpg", killpg)
    argv = [sys.executable, "-c",
            "import subprocess, sys, time; from pathlib import Path; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)", str(pidfile)]
    try:
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            runner(argv, cwd=tmp_path, timeout=1, env=dict(os.environ))
        assert time.monotonic() - started < 10, "cleanup waited for the 30s descendant"
        assert "wrapper-only" in caught.value.cleanup_message
        assert "descendants may still be running" in caught.value.cleanup_message
        assert processes[0].poll() is not None
        assert pidfile.exists(), "descendant must hold pipes during fallback"
        os.kill(int(pidfile.read_text()), 0)
        if failure == "shared-group":
            killpg.assert_not_called()
        else:
            killpg.assert_called_once()
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize("stage", ["lookup", "term", "kill"])
def test_permission_error_at_every_group_cleanup_stage(monkeypatch, stage):
    proc = Mock(pid=os.getpgrp() + 100000)
    monkeypatch.setattr(extra.os, "getpgid", Mock(
        side_effect=PermissionError() if stage == "lookup" else None,
        return_value=proc.pid,
    ))
    monkeypatch.setattr(extra.os, "killpg", Mock(
        side_effect=[None, PermissionError()] if stage == "kill" else PermissionError(),
    ))
    monkeypatch.setattr(extra.time, "sleep", lambda _: None)
    message = extra._kill_shell_process_group(proc)
    assert "wrapper-only" in message
    assert "its process group was killed" not in message
    proc.kill.assert_called_once()
