"""Shared helpers for shell subprocess argv and environment handling.

Interactive/admin shell calls preserve the full ``bash -lc`` surface. Trusted
service calls are different: their access-control profile validates one parsed
argv, so execution must use that exact argv with no shell expansion layer.
"""

from __future__ import annotations

import os
import shlex
import sys


def login_shell_command(command: str) -> str:
    """Wrap an interactive/admin command so the venv bin survives login init."""
    venv_bin = os.path.dirname(sys.executable or "")
    if not venv_bin:
        return command
    return f'export PATH={shlex.quote(venv_bin)}:"$PATH"\n{command}'


def direct_exec_env() -> dict[str, str]:
    """Return a child environment with the running interpreter's bin first.

    Service-shell execution deliberately avoids a login shell. Put the venv bin
    on PATH through the subprocess environment instead, so environment setup
    cannot change the validated argv.
    """
    env = os.environ.copy()
    venv_bin = os.path.dirname(sys.executable or "")
    if venv_bin:
        current_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(part for part in (venv_bin, current_path) if part)
    return env
