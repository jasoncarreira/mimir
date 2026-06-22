"""Best-effort initialization of the Chainlink store at startup.

The Tasks board reads ``chainlink issue list``; without an initialized store
(``<home>/.chainlink/``) that command errors and the board reports the tracker
"unavailable". This bootstraps an empty store on first startup so the board
works out of the box. Kept separate from ``chainlink_board.py`` (which is
strictly read-only) because this writes to the home.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ``chainlink init`` creates the sqlite store + default rules + hooks, so give
# it a longer ceiling than the board's read-path queries.
_INIT_TIMEOUT_SECONDS = 30.0
_DISABLE_VALUES = {"0", "false", "no", "off"}


def ensure_chainlink_initialized(home: Path | None) -> None:
    """Run ``chainlink init`` in ``home`` if no store exists yet.

    No-op (and never raises) when any of these hold:
      * ``home`` is unset
      * ``MIMIR_CHAINLINK_AUTOINIT`` is falsey (``0``/``false``/``no``/``off``)
      * ``<home>/.chainlink`` already exists
      * the ``chainlink`` CLI isn't on PATH

    Gating on CLI presence means a plain ``pip install`` without the chainlink
    binary is unaffected — the board just stays "unavailable" — while operator
    images that ship the CLI get a working Tasks board on first boot.
    """
    if home is None:
        return
    if os.environ.get("MIMIR_CHAINLINK_AUTOINIT", "1").strip().lower() in _DISABLE_VALUES:
        return
    if (home / ".chainlink").exists():
        return
    chainlink_bin = shutil.which("chainlink")
    if not chainlink_bin:
        return
    try:
        proc = subprocess.run(
            [chainlink_bin, "init", "-q"],
            cwd=str(home),
            capture_output=True,
            text=True,
            timeout=_INIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # never fail startup
        logger.warning("chainlink auto-init failed to run: %s", exc)
        return
    if proc.returncode == 0:
        logger.info("chainlink store initialized at %s/.chainlink", home)
    else:
        logger.warning(
            "chainlink auto-init exited %s: %s",
            proc.returncode,
            (proc.stderr or proc.stdout or "").strip()[:300],
        )
