from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from mimir.worklink.compute import ComputeResult, LaunchHandle, WorkSpec, _enabled_child_env
from mimir.worklink.evidence import observe_evidence
from tests.test_worklink_evidence import _init_gate_repo


@pytest.mark.asyncio
async def test_enabled_gate_rerun_preserves_compute_spec(tmp_path, monkeypatch):
    repo = _init_gate_repo(tmp_path, "def test_passes():\n    pass\n")
    launcher = ["uv", "run", "--extra", "dev", "--extra", "bench", sys.executable, "-m", "pytest"]
    command = shlex.join([*launcher, "-q", "-n", "6", "tests/"])
    failed = ("tests/test_sample.py::test_one[semi; quoted ' argument]", "tests/test_sample.py::test_two")
    passed = "tests/test_sample.py::test_passes"
    spec = WorkSpec(
        1557, 1, "url", "main", "issue/1557-a1", "prompt", None,
        command, "opencode", 37,
        env={"OPENCODE_PERMISSION": '{"edit":"allow"}', "GATE_SENTINEL": "same", "PYTEST_ADDOPTS": "-W error -n 2"},
        backend_config={"pass_env": ("GATE_SENTINEL",), "sentinel": "retained"},
        local_checkout=repo, local_argv=("opencode",),
    )
    specs, reports, events = [], [], []

    class Compute:
        async def launch(self, gate_spec):
            _enabled_child_env(gate_spec, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            specs.append(gate_spec)
            handle = LaunchHandle("local_subprocess", f"gate-{len(specs)}")
            events.append(("launch", handle))
            options = shlex.split(gate_spec.env["PYTEST_ADDOPTS"])
            report = Path(next(part.split("=", 1)[1] for part in options if part.startswith("--junitxml="))).parent
            assert not report.is_absolute()
            assert report.name.startswith(".worklink-gate-")
            reports.append(report)
            first = len(specs) == 1
            (repo / report / "junit.xml").write_text(
                f'<testsuite tests="{3 if first else 2}" failures="{2 if first else 0}" errors="0" skipped="0" />'
            )
            cache = repo / report / "cache" / "v" / "cache"
            cache.mkdir(parents=True)
            (cache / "lastfailed").write_text(json.dumps(dict.fromkeys(failed if first else (), True)))
            (cache / "nodeids").write_text(json.dumps([*failed, passed] if first else failed))
            return handle

        async def wait(self, handle, timeout_s):
            assert timeout_s == spec.timeout_s
            events.append(("wait", handle))
            return ComputeResult(1 if len(specs) == 1 else 0, "gate output", "", handle=handle)

        async def cleanup(self, handle):
            events.append(("cleanup", handle))

    class Publication:
        def run(self, *args):
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")
    result = await observe_evidence(
        issue=1557, attempt=1, backend="opencode", branch=spec.branch,
        checkout=repo, started_at=datetime.now(UTC), base_ref="main",
        backend_status="completed", test_command=command, work_spec=spec,
        compute=Compute(), safe_git=Publication(),
        on_gate_launch=lambda handle: events.append(("callback", handle)),
    )

    assert len(specs) == 2
    assert events == [
        (event, LaunchHandle("local_subprocess", f"gate-{index}"))
        for index in (1, 2) for event in ("launch", "callback", "wait", "cleanup")
    ]
    assert reports[0] != reports[1]
    for gate_spec, report in zip(specs, reports):
        assert gate_spec == replace(
            spec, local_argv=gate_spec.local_argv,
            env={**spec.env, "PYTEST_ADDOPTS": f"-W error -n 2 --junitxml={report}/junit.xml -o cache_dir={report}/cache"},
            backend_config={**spec.backend_config, "pass_env": ("GATE_SENTINEL", "PYTEST_ADDOPTS")},
        )
        assert gate_spec.local_argv[:2] == ("/bin/sh", "-c")
        assert not (repo / report).exists()
    assert specs[0].local_argv[2] == command
    assert shlex.split(specs[1].local_argv[2]) == [
        *launcher, "-q", "-n", "0", "-k", "", "-m", "", "--", *failed,
    ]
    tests = result.evidence.tests
    assert result.status == "completed"
    assert result.review_ready is True
    assert tests.initial_run.exit_code == 1
    assert tests.initial_run.failed_tests == failed
    assert tests.rerun.exit_code == tests.exit_code == 0
    assert tests.failed_tests == ()
    assert tests.flaky_tests == failed


@pytest.mark.asyncio
async def test_gate_rerun_preserves_configured_warning_errors(tmp_path, monkeypatch):
    repo = _init_gate_repo(tmp_path, '''
from pathlib import Path
import warnings

def test_warning():
    path = Path("visits")
    path.write_text(str(int(path.read_text()) + 1) if path.exists() else "1")
    warnings.warn("persistent gate warning", UserWarning)

def test_passes():
    path = Path("passing-visits")
    path.write_text(str(int(path.read_text()) + 1) if path.exists() else "1")
''')
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -W error -n 2\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 2")
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "false")
    result = await observe_evidence(
        issue=1557, attempt=1, backend="opencode", branch="issue/1557-a1",
        checkout=repo, started_at=datetime.now(UTC), base_ref="main",
        backend_status="completed",
        test_command=f"{shlex.quote(sys.executable)} -m pytest -q test_gate_sample.py",
    )

    tests = result.evidence.tests
    assert result.status == "failed"
    assert result.review_ready is False
    assert tests.initial_run.exit_code == tests.rerun.exit_code == tests.exit_code == 1
    assert tests.initial_run.counts.total == 2
    assert tests.rerun.counts.total == tests.rerun.counts.failed == 1
    assert tests.failed_tests == ("test_gate_sample.py::test_warning",)
    assert tests.flaky_tests == ()
    assert "UserWarning: persistent gate warning" in tests.rerun.summary
    assert shlex.split(tests.rerun.cmd) == [
        sys.executable, "-m", "pytest", "-q", "-n", "0", "-k", "", "-m", "", "--",
        "test_gate_sample.py::test_warning",
    ]
    assert (repo / "visits").read_text() == "2"
    assert (repo / "passing-visits").read_text() == "1"
