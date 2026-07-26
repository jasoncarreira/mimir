"""Shared helpers for shell subprocess argv and environment handling.

Interactive/admin shell calls preserve the full ``bash -lc`` surface. Trusted
service calls are different: their access-control profile validates one parsed
argv, so execution must use that exact argv with no shell expansion layer.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


def login_shell_command(command: str) -> str:
    """Wrap an interactive/admin command so the venv bin survives login init."""
    venv_bin = os.path.dirname(sys.executable or "")
    if not venv_bin:
        return command
    return f'export PATH={shlex.quote(venv_bin)}:"$PATH"\n{command}'


def _is_pytest_argv(argv: list[str] | None) -> bool:
    """Return whether *argv* invokes the profile's admitted pytest shapes."""
    if not argv:
        return False
    command = Path(argv[0]).name
    return (
        command == "pytest"
        or argv[1:3] == ["-m", "pytest"]
        or command == "uv" and argv[1:3] == ["run", "pytest"]
    )


def _is_git_argv(argv: list[str] | None) -> bool:
    """Return whether *argv* invokes the server-pinned maintenance Git binary."""
    return bool(argv) and argv[0] == "/usr/bin/git"


def direct_exec_env(argv: list[str] | None = None) -> dict[str, str]:
    """Return a child environment safe for the server-authorized direct argv.

    Service-shell execution deliberately avoids a login shell. Put the venv bin
    on PATH through the subprocess environment instead, so environment setup
    cannot change the validated argv. Pytest's explicit environment controls are
    also an argument/plugin-injection surface, so remove them when the authorized
    executable is direct pytest or ``uv run pytest``. Installed entry-point
    plugins remain available because ordinary repository test suites depend on
    them; they are operator-installed code rather than PR-selected argv.
    """
    env = os.environ.copy()
    if _is_pytest_argv(argv):
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_PLUGINS", None)
    if _is_git_argv(argv):
        # The maintenance profile binds Git to a configured -C root and injects
        # config-neutralizing argv. Inherited GIT_* variables must not select a
        # different repository, config source, helper executable, or diff tool.
        for key in tuple(env):
            if key.startswith("GIT_"):
                env.pop(key, None)
        env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
        })
    venv_bin = os.path.dirname(sys.executable or "")
    if venv_bin:
        current_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(part for part in (venv_bin, current_path) if part)
    return env


def direct_exec_env_overlay(argv: list[str] | None = None) -> dict[str, str | None]:
    """Return an inherited-environment overlay for an authorized async argv.

    ``ShellJobRegistry`` overlays values onto its own inherited environment, so
    pytest injection variables must be explicit ``None`` deletion markers rather
    than merely absent from the mapping returned by :func:`direct_exec_env`.
    """
    overlay: dict[str, str | None] = direct_exec_env(argv)
    if _is_pytest_argv(argv):
        overlay["PYTEST_ADDOPTS"] = None
        overlay["PYTEST_PLUGINS"] = None
    if _is_git_argv(argv):
        for key in os.environ:
            if key.startswith("GIT_") and key not in overlay:
                overlay[key] = None
    return overlay


__all__ = ["direct_exec_env", "direct_exec_env_overlay", "login_shell_command"]
