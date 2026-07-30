"""Shared helpers for shell subprocess argv and environment handling.

Interactive/admin shell calls preserve the full ``bash -lc`` surface. Trusted
service calls are different: their access-control profile validates one parsed
argv, so execution must use that exact argv with no shell expansion layer.
"""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from contextvars import ContextVar, Token
from pathlib import Path

_TRUSTED_PATH_DIRS = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
_TRUSTED_PATH = os.pathsep.join(_TRUSTED_PATH_DIRS)
_GH_CONFIG_DIR = tempfile.mkdtemp(prefix="mimir-gh-config-")
Path(_GH_CONFIG_DIR).chmod(0o500)
_ALTERNATE_GITHUB_ENV = ("GH_TOKEN", "GH_HOST")
_DIRECT_EXEC_ARGV: ContextVar[tuple[str, ...] | None] = ContextVar(
    "mimir_direct_exec_argv", default=None,
)


def bind_direct_exec_argv(argv: list[str]) -> Token[tuple[str, ...] | None]:
    """Bind middleware-authorized argv across ToolNode's injected-arg scrub."""
    return _DIRECT_EXEC_ARGV.set(tuple(argv))


def reset_direct_exec_argv(token: Token[tuple[str, ...] | None]) -> None:
    _DIRECT_EXEC_ARGV.reset(token)


def bound_direct_exec_argv() -> list[str] | None:
    argv = _DIRECT_EXEC_ARGV.get()
    return list(argv) if argv is not None else None


def login_shell_command(command: str) -> str:
    """Wrap an interactive/admin command with system tools before the venv bin.

    The free-form login-shell path needs the environment's ``mimir`` console
    script and Python interpreter. Append that directory after the fixed,
    root-owned tool directories so it remains reachable without being able to
    shadow system tools such as Git or GitHub CLI.
    """
    venv_bin = os.path.dirname(sys.executable or "")
    path = os.pathsep.join(
        part for part in (_TRUSTED_PATH, venv_bin) if part
    )
    return f"export PATH={shlex.quote(path)}\n{command}"


def _is_git_argv(argv: list[str] | None) -> bool:
    """Return whether *argv* invokes the server-pinned maintenance Git binary."""
    return bool(argv) and argv[0] == "/usr/bin/git"


def _is_gh_argv(argv: list[str] | None) -> bool:
    # Authorization replaces the command with an operator-pinned absolute path,
    # which may differ from the image default used in production.
    return bool(argv) and Path(argv[0]).name == "gh"


def _gh_has_account_effect(argv: list[str] | None) -> bool:
    if not _is_gh_argv(argv) or argv is None:
        return False
    arguments = argv[1:]
    if len(arguments) >= 2 and arguments[0] in {"pr", "issue"}:
        return arguments[1] in {
            "close", "comment", "create", "edit", "merge", "ready", "reopen", "review",
        }
    if arguments[:1] == ["api"]:
        for index, argument in enumerate(arguments[1:]):
            if argument in {"--method", "-X"} and index + 2 < len(arguments):
                return arguments[index + 2].upper() != "GET"
            if argument.startswith("--method="):
                return argument.partition("=")[2].upper() != "GET"
    return False


def direct_exec_env(argv: list[str] | None = None) -> dict[str, str]:
    """Return a child environment safe for the server-authorized direct argv.

    Service-shell execution deliberately avoids a login shell. Its PATH contains
    only root-owned deployment directories; in particular, it excludes the
    workspace virtualenv. The project test executable and fixed arguments come
    from operator configuration rather than language-specific inference here.
    """
    env = os.environ.copy()
    for key in _ALTERNATE_GITHUB_ENV:
        env.pop(key, None)
    if _is_gh_argv(argv):
        env["GH_CONFIG_DIR"] = _GH_CONFIG_DIR
        if _gh_has_account_effect(argv):
            from ..forge.github import confirm_github_identity

            confirm_github_identity(
                os.environ.get("MIMIR_GITHUB_SELF_LOGIN", ""),
                env.get("GITHUB_TOKEN", ""),
            )
    else:
        env.pop("GH_CONFIG_DIR", None)
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
    env["PATH"] = _TRUSTED_PATH
    return env


def direct_exec_env_overlay(argv: list[str] | None = None) -> dict[str, str | None]:
    """Return an inherited-environment overlay for an authorized async argv.

    ``ShellJobRegistry`` overlays values onto its own inherited environment.
    """
    overlay: dict[str, str | None] = direct_exec_env(argv)
    for key in (*_ALTERNATE_GITHUB_ENV, "GH_CONFIG_DIR"):
        if key in os.environ and key not in overlay:
            overlay[key] = None
    if _is_git_argv(argv):
        for key in os.environ:
            if key.startswith("GIT_") and key not in overlay:
                overlay[key] = None
    return overlay


__all__ = [
    "bind_direct_exec_argv",
    "bound_direct_exec_argv",
    "direct_exec_env",
    "direct_exec_env_overlay",
    "login_shell_command",
    "reset_direct_exec_argv",
]
