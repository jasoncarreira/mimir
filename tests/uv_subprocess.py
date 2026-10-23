"""Environment for tests that shell out to ``uv``.

The non-owning worker legs run with ``HOME=/nonexistent`` and a read-only
checkout, so uv's default cache under ``$HOME/.cache/uv`` cannot be created and
nothing may be written into the tree. Give each uv subprocess a private cache
under the test's own temporary directory, and pin the interpreter so uv does not
try to discover or download one. Build outputs go to ``--out-dir`` under the
test's temporary directory as well.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def isolated_uv_env(scratch: Path) -> dict[str, str]:
    """Return ``os.environ`` with every uv write confined to ``scratch``."""
    scratch.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "UV_CACHE_DIR": str(scratch / "uv-cache"), "UV_PYTHON": sys.executable}
