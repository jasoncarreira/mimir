"""Exercise the repository timeout policy in isolated pytest processes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import tomllib
import xml.etree.ElementTree as ET

import pytest


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
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            pytest.fail("Child pytest hit the 30s guard instead of reporting the timed-out node id")
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
