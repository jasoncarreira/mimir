"""Environment for uv subprocesses in read-only, non-owning-uid test runs."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def isolated_uv_env(scratch: Path) -> dict[str, str]:
    """Use writable test scratch and the active interpreter, not HOME discovery."""
    return {
        **os.environ,
        "UV_CACHE_DIR": str(scratch / "uv-cache"),
        "UV_PYTHON": sys.executable,
    }
