from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess

import pytest


_PATH = Path(__file__).with_name("run_worklink_incident_verification.py")


def _load():
    spec = importlib.util.spec_from_file_location("incident_verification_runner", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_does_not_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("launched"))
    _load()


def test_invalid_arguments_do_not_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("launched"))
    assert module.main(["--fast"]) == 2


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([0, 0, 0], 0),
        ([3, 0, 0], 3),
        ([0, 4, 0], 4),
        ([0, 0, 5], 5),
        ([6, 7, 8], 6),
        ([-9, 0, 0], 137),
    ],
)
def test_runs_all_suites_and_returns_first_failure(
    monkeypatch: pytest.MonkeyPatch,
    codes: list[int],
    expected: int,
) -> None:
    module = _load()
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "0")
    monkeypatch.setenv("INCIDENT_RUNNER_SENTINEL", "kept")
    observed = []

    def run(command, **kwargs):
        observed.append((command, kwargs))
        return subprocess.CompletedProcess(command, codes[len(observed) - 1])

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.main([]) == expected
    assert len(observed) == 3
    root = _PATH.resolve().parents[1]
    assert observed[0][0] == ["uv", "run", "pytest", "-q", "--tb=short"]
    assert observed[1][0] == observed[2][0] == [
        "uv", "run", "--extra", "dev", "--extra", "bench", "pytest", "-q", "-n", "6",
    ]
    for _, kwargs in observed:
        assert kwargs["cwd"] == root
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        assert kwargs["env"]["INCIDENT_RUNNER_SENTINEL"] == "kept"
    assert observed[0][1]["env"]["MIMIR_ACCESS_CONTROL_ENFORCED"] == "0"
    assert "MIMIR_ACCESS_CONTROL_ENFORCED" not in observed[1][1]["env"]
    assert observed[2][1]["env"]["MIMIR_ACCESS_CONTROL_ENFORCED"] == "1"
    assert os.environ["MIMIR_ACCESS_CONTROL_ENFORCED"] == "0"
    assert all(observed[index][1]["env"] is not observed[other][1]["env"] for index in range(3) for other in range(index))


def test_launch_errors_are_reported_and_later_suites_run(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    calls = 0

    def run(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("uv unavailable")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.main([]) == 127
    assert calls == 3
