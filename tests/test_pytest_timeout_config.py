"""Exercise the repository timeout policy in isolated pytest processes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import textwrap
import time
import tomllib
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest


def _kill_child_group(process):
    """Signal the owned group without mistaking EPERM for a live child's exit."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as error:
        # An exited group leader can leave an unsignalable group on macOS.
        # EPERM while the direct child is still live must remain an error.
        if isinstance(error, PermissionError) and process.returncode is None:
            raise


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
@pytest.mark.parametrize("returncode", [None, 0, -signal.SIGKILL])
@pytest.mark.parametrize("error_type", [ProcessLookupError, PermissionError])
def test_kill_child_group_cleanup_races(monkeypatch, returncode, error_type):
    process = SimpleNamespace(pid=12345, returncode=returncode)
    calls = []

    def denied_killpg(pid, sig):
        calls.append((pid, sig))
        raise error_type("cleanup race")

    monkeypatch.setattr(os, "killpg", denied_killpg)
    if error_type is PermissionError and returncode is None:
        with pytest.raises(PermissionError, match="cleanup race"):
            _kill_child_group(process)
    else:
        _kill_child_group(process)
    assert calls == [(process.pid, signal.SIGKILL)]


def _wait_for_child(process, *, timeout=30, drain_timeout=1):
    """Collect output without requiring inherited writers to reach EOF.

    The caller starts a new session; this helper also cleans up that owned
    process group, including descendants left behind by an exited controller.
    """
    output = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    drain_deadline = None
    try:
        with selectors.DefaultSelector() as selector:
            for stream in output:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while True:
                returncode = process.poll()
                now = time.monotonic()
                if returncode is not None:
                    if drain_deadline is None:
                        drain_deadline = now + drain_timeout
                    if not selector.get_map():
                        break
                    if now >= drain_deadline:
                        print(
                            f"Child pytest stream guard ({drain_timeout}s): mechanism (a); "
                            f"direct child had exited (returncode={returncode}), "
                            "but captured streams are still open; checking collected output",
                            file=sys.stderr,
                        )
                        break
                elif now >= deadline:
                    pytest.fail(
                        f"Child pytest hit the {timeout}s guard: mechanism (b); "
                        "direct child had not exited (returncode=None); "
                        "possible nested-pytest shutdown hang\n"
                        + "\n".join(data.decode(errors="replace") for data in output.values())
                    )
                bound = drain_deadline if drain_deadline is not None else deadline
                # Drain during execution too: wait() alone can deadlock on a full pipe.
                interval = min(0.05, max(0, bound - now))
                if not selector.get_map():
                    try:
                        process.wait(timeout=interval)
                    except subprocess.TimeoutExpired:
                        pass
                    continue
                for key, _ in selector.select(interval):
                    chunk = os.read(key.fd, 65536)
                    if chunk:
                        output[key.fileobj].extend(chunk)
                    else:
                        selector.unregister(key.fileobj)
        return tuple(data.decode(errors="replace") for data in output.values())
    finally:
        _kill_child_group(process)
        # Reap only the direct child; never reintroduce an unbounded communicate().
        process.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and pipes")
def test_wait_for_child_does_not_require_descendant_pipe_eof(capfd):
    release_reader, release_writer = os.pipe()
    try:
        # The descendant cannot finish until we release it. No timing race or
        # sleep is needed to keep both inherited output pipes open.
        command = [sys.executable, "-c", textwrap.dedent(f"""\
            import os, subprocess, sys
            subprocess.Popen(
                [sys.executable, '-c', 'import os; os.read({release_reader}, 1)'],
                pass_fds=({release_reader},),
            )
            os.write(1, b'child stdout\\n')
            os.write(2, b'child stderr\\n')
            """)]
        # Separate children avoid communicate() consuming the new helper's output.
        for legacy in (True, False):
            with subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(release_reader,), start_new_session=True,
            ) as process:
                try:
                    if legacy:
                        assert process.wait(timeout=5) == 0
                        with pytest.raises(subprocess.TimeoutExpired):
                            process.communicate(timeout=1)
                    else:
                        stdout, stderr = _wait_for_child(process, drain_timeout=0.1)
                        assert process.returncode == 0
                        assert stdout == 'child stdout\n'
                        assert stderr == 'child stderr\n'
                finally:
                    _kill_child_group(process)
                    process.wait(timeout=5)
    finally:
        os.close(release_reader)
        os.close(release_writer)
    diagnostic = capfd.readouterr().err
    assert "mechanism (a)" in diagnostic
    assert "direct child had exited (returncode=0)" in diagnostic
    assert "captured streams are still open" in diagnostic


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and pipes")
@pytest.mark.parametrize("close_streams", [False, True], ids=["open-pipes", "closed-pipes"])
def test_wait_for_child_reports_live_child(close_streams):
    ready_reader, ready_writer = os.pipe()
    command = "import os; os.write(2, b'before shutdown\\n'); "
    if close_streams:
        command += "os.close(1); os.close(2); "
    command += f"os.write({ready_writer}, b'1'); os.close({ready_writer}); os.read(0, 1)"
    try:
        with subprocess.Popen(
            [sys.executable, "-c", command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(ready_writer,), start_new_session=True,
        ) as process:
            os.close(ready_writer)
            ready_writer = None
            try:
                # Arm the live-child guard only after the diagnostic write and
                # optional pipe closures, not against interpreter startup.
                assert os.read(ready_reader, 1) == b"1"
                with pytest.raises(pytest.fail.Exception) as failure:
                    _wait_for_child(process, timeout=1)
                assert "hit the 1s guard: mechanism (b)" in str(failure.value)
                assert "direct child had not exited (returncode=None)" in str(failure.value)
                assert "before shutdown" in str(failure.value)
                assert process.returncode == -signal.SIGKILL
            finally:
                _kill_child_group(process)
                process.wait(timeout=5)
    finally:
        os.close(ready_reader)
        if ready_writer is not None:
            os.close(ready_writer)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and pipes")
def test_wait_for_child_drains_output_while_child_runs():
    with subprocess.Popen(
        [sys.executable, "-c", "import os; os.write(1, b'x' * 1000000); os.write(2, b'y' * 1000000)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    ) as process:
        stdout, stderr = _wait_for_child(process)
        assert process.returncode == 0
        assert stdout == "x" * 1000000
        assert stderr == "y" * 1000000


def test_timeout_policy():
    config = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )["tool"]["pytest"]["ini_options"]
    assert config["timeout"] == 300
    assert config["timeout_method"] == "signal"
    assert config["faulthandler_timeout"] == 300
    assert config["faulthandler_exit_on_timeout"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGALRM policy")
@pytest.mark.parametrize("workers", [0, 1], ids=["serial", "xdist"])
def test_hanging_async_test_fails_and_session_continues(tmp_path, workers):
    config = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )["tool"]["pytest"]["ini_options"]
    # Derive the child policy, but control delivery rather than racing setup.
    if "timeout" in config:
        config["timeout"] /= 150
    config["faulthandler_timeout"] /= 1500
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\n" + "\n".join(
            f"{key} = {str(value).lower() if isinstance(value, bool) else value}"
            for key, value in config.items()
            if key not in {"addopts", "markers", "testpaths"}
        ) + "\n"
    )
    (tmp_path / "conftest.py").write_text(textwrap.dedent('''\
        import faulthandler
        import os
        import signal
        import threading

        import pytest

        alarm = None
        diagnostic = None
        real_dump_later = faulthandler.dump_traceback_later

        def record_alarm(which, seconds, interval=0):
            global alarm
            assert which == signal.ITIMER_REAL
            assert interval == 0
            alarm = seconds
            return (0.0, 0.0)

        def record_diagnostic(timeout, *, file, exit=False):
            global diagnostic
            diagnostic = (timeout, file, exit)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(item):
            global alarm, diagnostic
            alarm = diagnostic = None
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(signal, "setitimer", record_alarm)
                patch.setattr(faulthandler, "dump_traceback_later", record_diagnostic)
                yield

        def blocked_select(task, future):
            # The loop has returned from the coroutine to its selector: the
            # await is suspended, not merely about to run during setup.
            assert not task.done() and task.get_coro().cr_await is not None
            assert not future.done()
            assert alarm == 2.0, f"pytest-timeout did not arm SIGALRM: {alarm!r}"
            assert diagnostic is not None
            timeout, destination, exit = diagnostic
            assert timeout == 0.2 and exit is False

            # Drain concurrently so even a large real stack dump cannot fill
            # the pipe. Wait for our blocked frame, not an elapsed-time margin.
            reader, writer = os.pipe()
            dumped = threading.Event()

            def forward_dump():
                output = b""
                with os.fdopen(reader, "rb", buffering=0) as stream:
                    while chunk := stream.read(4096):
                        os.write(destination, chunk)
                        output += chunk
                        if b"in blocked_select" in output:
                            dumped.set()

            thread = threading.Thread(target=forward_dump)
            thread.start()
            try:
                real_dump_later(timeout, file=writer, exit=exit)
                dumped.wait()
            finally:
                faulthandler.cancel_dump_traceback_later()
                os.close(writer)
                thread.join()
            # Real POSIX delivery into the handler installed by pytest-timeout.
            os.kill(os.getpid(), signal.SIGALRM)
            raise AssertionError("SIGALRM did not fail the test")
        '''))
    (tmp_path / "test_hang.py").write_text(
        "import asyncio\n"
        "from conftest import blocked_select\n"
        "async def test_hangs_in_event_loop(monkeypatch):\n"
        "    loop = asyncio.get_running_loop()\n"
        "    task = asyncio.current_task()\n"
        "    future = loop.create_future()\n"
        "    original_select = loop._selector.select\n"
        "    def select(timeout=None):\n"
        "        monkeypatch.setattr(loop._selector, 'select', original_select)\n"
        "        return blocked_select(task, future)\n"
        "    monkeypatch.setattr(loop._selector, 'select', select)\n"
        "    await future\n"
        "def test_following():\n"
        "    pass\n"
    )
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("PYTEST_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["COLUMNS"] = "200"
    command = [
        sys.executable, "-m", "pytest", "-p", "pytest_asyncio.plugin",
        "-p", "pytest_timeout", "-p", "xdist.plugin", "-n", str(workers),
        "-q", "--tb=short", "--junitxml=report.xml", "test_hang.py",
    ]
    with subprocess.Popen(
        command, cwd=tmp_path, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True,
    ) as process:
        stdout, stderr = _wait_for_child(process)
    assert process.returncode == 1, stdout + stderr
    nodeid = "test_hang.py::test_hangs_in_event_loop"
    assert f"FAILED {nodeid} - Failed: Timeout (>2.0s) from pytest-timeout." in stdout
    assert "1 failed, 1 passed" in stdout
    assert "Timeout (0:00:00.200000)!" in stderr
    assert "in blocked_select" in stderr
    cases = ET.parse(tmp_path / "report.xml").findall(".//testcase")
    assert len(cases) == 2
    failed = next(case for case in cases if case.get("name") == "test_hangs_in_event_loop")
    assert "Timeout (>2.0s)" in failed.find("failure").get("message")
    following = next(case for case in cases if case.get("name") == "test_following")
    assert following.find("failure") is None
    assert json.loads((tmp_path / ".pytest_cache/v/cache/lastfailed").read_text()) == {
        nodeid: True,
    }
