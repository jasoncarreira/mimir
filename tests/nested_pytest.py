"""Commands for pytest children whose temporary files belong to the outer test."""

from __future__ import annotations

from pathlib import Path
import sys


def pytest_command(tmp_path: Path, *args: str) -> list[str]:
    # Without an explicit base, xdist registers atexit cleanup of the shared
    # pytest-of-user tree, potentially deleting unrelated runs past the summary.
    return [sys.executable, "-m", "pytest", "--basetemp", str(tmp_path / "pytest-tmp"), *args]
