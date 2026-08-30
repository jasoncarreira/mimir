"""Regression coverage for the repository's empty-parametrize guard."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _run_fixture_test(
    tmp_path: Path, argvalues: str, *args: str
) -> subprocess.CompletedProcess[str]:
    test_file = tmp_path / "test_fixture.py"
    test_file.write_text(
        "import pytest\n\n"
        f"@pytest.mark.parametrize('method', {argvalues})\n"
        "def test_fixture(method):\n"
        "    assert method == 'aread'\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(ROOT), env.get("PYTHONPATH")))
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            # The probe inspects a collection error that xdist does not relay.
            "-n",
            "0",
            "-c",
            str(ROOT / "pyproject.toml"),
            "-p",
            "conftest",
            *args,
            str(test_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_empty_parametrize_fails_collection_and_nonempty_passes(tmp_path: Path) -> None:
    empty = _run_fixture_test(tmp_path, "[]")
    output = empty.stdout + empty.stderr

    assert empty.returncode != 0
    assert "test_fixture.py::test_fixture[NOTSET]" in output
    assert "parametrize argnames: method" in output

    nonempty = _run_fixture_test(tmp_path, "['aread']")
    assert nonempty.returncode == 0, nonempty.stdout + nonempty.stderr
    assert "1 passed" in nonempty.stdout


def test_empty_parametrize_fails_before_k_deselection(tmp_path: Path) -> None:
    result = _run_fixture_test(tmp_path, "[]", "-k", "not test_fixture")

    assert result.returncode != 0
    assert "parametrize argnames: method" in result.stdout + result.stderr


def test_intentionally_empty_parametrize_can_opt_out(tmp_path: Path) -> None:
    test_file = tmp_path / "test_allowed.py"
    test_file.write_text(
        "import pytest\n\n"
        "@pytest.mark.allow_empty_parametrize\n"
        "@pytest.mark.parametrize('platform', [])\n"
        "def test_platform(platform):\n"
        "    raise AssertionError('empty parameter set should not run')\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(ROOT), env.get("PYTHONPATH")))
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            # Keep every collection-policy probe on the same serial path.
            "-n",
            "0",
            "-c",
            str(ROOT / "pyproject.toml"),
            "-p",
            "conftest",
            str(test_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout
