"""Keep the reviewed Python coverage and interpreter selection honest."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib

from packaging.specifiers import SpecifierSet
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
JOBS = ("pytest", "pytest-macos")


def _assert_matrix_matches(requires_python: str, versions: list[str]) -> None:
    # >=3.11 is open-ended; 3.13 is the reviewed coverage horizon for #1757.
    # Extend the horizon with future interpreter support, not the package bound.
    declared = SpecifierSet(requires_python)
    expected = {f"3.{minor}" for minor in range(14) if f"3.{minor}" in declared}
    assert expected <= set(versions), f"Missing Python coverage: {expected - set(versions)}"
    assert all(version in declared for version in versions), requires_python
    assert len(versions) == len(set(versions)), "Duplicate Python matrix entries"


@pytest.mark.parametrize("job_name", JOBS)
def test_ci_python_versions_match_package(job_name: str) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    job = workflow["jobs"][job_name]
    requires_python = project["project"]["requires-python"]
    assert requires_python == ">=3.11", "#1757 extends CI; do not narrow package support"
    _assert_matrix_matches(requires_python, job["strategy"]["matrix"]["python-version"])
    assert job["env"]["UV_PYTHON"] == "${{ matrix.python-version }}"
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12", "3.13"]
    for step in job["steps"]:
        assert "UV_PYTHON" not in step.get("env", {}), "Do not override matrix selection"

    steps = job["steps"]
    verification = next(step for step in steps if step.get("name") == "Verify selected Python")
    assert steps.index(verification) > next(
        i for i, step in enumerate(steps) if step.get("run", "").startswith("uv sync ")
    )
    assert steps.index(verification) < next(
        i for i, step in enumerate(steps) if step.get("name") == "Run mimir test suite"
    )
    command = shlex.split(verification["run"])
    assert command[:4] == ["uv", "run", "python", "-c"]
    # Exercise the actual CI assertion without needing every interpreter locally.
    for version, expected_returncode in ((f"{sys.version_info.major}.{sys.version_info.minor}", 0), ("0.0", 1)):
        result = subprocess.run(
            [sys.executable, *command[3:]],
            env={**os.environ, "UV_PYTHON": version},
            capture_output=True,
            text=True,
        )
        assert result.returncode == expected_returncode, result.stderr

    # Assert the whole-suite check shape without changing test collection counts.
    if job_name == "pytest":
        jobs = workflow["jobs"]
        assert set(jobs) == {
            "package", "skill-conformance", "chainlink-cli-audit", "pytest",
            "pytest-worker-uid", "pytest-macos", "pytest-fresh-resolve", "pytest-enforced",
            "worklink-image-identity", "claude-code-extra-smoke", "frontend",
        }
        suites = {
            "pytest": (15, "Run mimir test suite"),
            "pytest-macos": (25, "Run mimir test suite"),
            "pytest-worker-uid": (20, "Run mimir test suite as the non-owning worker uid"),
            "pytest-enforced": (15, "Run mimir test suite (enforcement on)"),
            "pytest-fresh-resolve": (20, "Run mimir test suite against the fresh resolution"),
        }
        for name, (cap, suite_name) in suites.items():
            suite_job = jobs[name]
            assert suite_job["timeout-minutes"] == cap
            assert "name" not in suite_job  # Default check contexts use the job ids.
            suite_steps = suite_job["steps"]
            report = next(step for step in suite_steps if step.get("name") == "Report pytest worker capacity")
            suite = next(step for step in suite_steps if step.get("name") == suite_name)
            assert suite_steps.index(report) < suite_steps.index(suite)
            report_command = shlex.split(report["run"])
            assert report_command[-2] == "-c"
            assert "os.cpu_count()" in report_command[-1]
            assert "pytest_xdist_auto_num_workers" in report_command[-1]
            reported = subprocess.run(
                [sys.executable, "-c", report_command[-1]],
                capture_output=True, text=True, check=True,
            )
            assert "logical CPUs:" in reported.stdout
            assert "xdist -n auto workers:" in reported.stdout
            assert "--durations=25" in suite["run"]


@pytest.mark.parametrize(
    "requires_python, versions",
    [
        (">=3.11", ["3.11", "3.12"]),
        (">=3.11", ["3.11", "3.13"]),
        (">=3.11", ["3.12", "3.13"]),
        (">=3.10", ["3.11", "3.12", "3.13"]),
        (">=3.12", ["3.11", "3.12", "3.13"]),
        (">=3.11,!=3.12.*", ["3.11", "3.12", "3.13"]),
    ],
)
def test_matrix_check_rejects_drift(requires_python: str, versions: list[str]) -> None:
    with pytest.raises(AssertionError):
        _assert_matrix_matches(requires_python, versions)
