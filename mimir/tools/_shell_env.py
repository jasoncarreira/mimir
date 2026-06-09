"""Shared helper: keep the project venv bin on PATH for agent shell commands.

The agent's shell tools (``shell_exec``, ``bash_async``) run commands through
a login shell (``bash -lc``), which re-sources the login profile and resets
PATH — dropping the project venv's ``bin`` dir, where the ``mimir`` console
script and the venv ``python`` live. Framework features assume ``mimir`` is on
PATH (e.g. the reflection skill's ``mimir reflection introspection-report``
calls). To make that hold regardless of how the server was launched or what
the login profile sets, prepend the running interpreter's bin dir to PATH
*inside* the command, so the export runs after the profile load.
"""

from __future__ import annotations

import os
import shlex
import sys


def login_shell_command(command: str) -> str:
    """Wrap ``command`` (destined for ``bash -lc``) so the venv bin — where
    ``mimir`` and the venv ``python`` live — is on PATH when it runs.

    The prepended ``export`` runs *after* the login profile, so it survives a
    profile that resets PATH. Returns ``command`` unchanged if the interpreter
    path can't be determined (defensive; never breaks the command).
    """
    venv_bin = os.path.dirname(sys.executable or "")
    if not venv_bin:
        return command
    return f'export PATH={shlex.quote(venv_bin)}:"$PATH"\n{command}'
