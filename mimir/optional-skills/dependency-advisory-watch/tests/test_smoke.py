"""Smoke test for standalone poller execution.

Verifies that the poller can be run from the skill directory without
importing mimir or undeclared third-party packages.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


SKILL_DIR = Path(__file__).parent.parent


def test_poller_does_not_import_mimir():
    """Verify poller.py does not import mimir."""
    poller_path = SKILL_DIR / "poller.py"
    content = poller_path.read_text()
    assert "import mimir" not in content, "poller.py must not import mimir"
    assert "from mimir" not in content, "poller.py must not import from mimir"


def test_scanner_does_not_import_mimir():
    """Verify scanner.py does not import mimir."""
    scanner_path = SKILL_DIR / "scanner.py"
    content = scanner_path.read_text()
    assert "import mimir" not in content, "scanner.py must not import mimir"
    assert "from mimir" not in content, "scanner.py must not import from mimir"


ALLOWED_IMPORTS = (
    "json", "os", "sys", "pathlib", "typing", "subprocess", "dataclasses",
    "__future__",
)


def test_poller_only_uses_stdlib():
    """Verify poller.py uses only stdlib imports (no third-party)."""
    poller_path = SKILL_DIR / "poller.py"
    content = poller_path.read_text()

    non_stdlib = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            if not any(
                stripped.startswith(f"import {pkg}") or stripped.startswith(f"from {pkg}")
                for pkg in ALLOWED_IMPORTS
            ):
                if not stripped.startswith("import scanner") and not stripped.startswith("from scanner"):
                    if not stripped.startswith("#"):
                        non_stdlib.append(stripped)

    assert not non_stdlib, f"poller.py uses non-stdlib imports: {non_stdlib}"


def test_scanner_only_uses_stdlib():
    """Verify scanner.py uses only stdlib imports (no third-party)."""
    scanner_path = SKILL_DIR / "scanner.py"
    content = scanner_path.read_text()

    non_stdlib = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            if not any(
                stripped.startswith(f"import {pkg}") or stripped.startswith(f"from {pkg}")
                for pkg in ALLOWED_IMPORTS
            ):
                if not stripped.startswith("#"):
                    non_stdlib.append(stripped)

    assert not non_stdlib, f"scanner.py uses non-stdlib imports: {non_stdlib}"


def test_poller_runs_without_mimir(tmp_path):
    """Verify poller.py can be executed as a standalone script without mimir.

    This simulates running `python3 poller.py` from the skill directory
    after installation, verifying it doesn't crash on import.
    """
    poller_path = SKILL_DIR / "poller.py"

    result = subprocess.run(
        [sys.executable, str(poller_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode in (0, 2), (
        f"poller.py should exit 0 (no lockfiles) or 2 (scanner not found), "
        f"got {result.returncode}. stderr: {result.stderr}"
    )
