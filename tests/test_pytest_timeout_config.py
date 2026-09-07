"""Exercise the repository timeout policy in isolated pytest processes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
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
    # Scale the real policy, not a CLI override: deleting it must hit our guard.
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
    (tmp_path / "test_hang.py").write_text(
        "import asyncio\n"
        "async def test_hangs_in_event_loop():\n"
        "    await asyncio.sleep(999)\n"
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
    assert "selectors.py" in stderr
    cases = ET.parse(tmp_path / "report.xml").findall(".//testcase")
    assert len(cases) == 2
    failed = next(case for case in cases if case.get("name") == "test_hangs_in_event_loop")
    assert "Timeout (>2.0s)" in failed.find("failure").get("message")
    following = next(case for case in cases if case.get("name") == "test_following")
    assert following.find("failure") is None
    assert json.loads((tmp_path / ".pytest_cache/v/cache/lastfailed").read_text()) == {
        nodeid: True,
    }
