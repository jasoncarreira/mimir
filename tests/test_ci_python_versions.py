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
