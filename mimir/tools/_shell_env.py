"""Shared helpers for shell subprocess argv and environment handling.

Interactive/admin shell calls preserve the full ``bash -lc`` surface but receive
only an explicitly selected environment. Trusted service calls validate one
parsed argv and execute it with no shell expansion layer.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import tempfile
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..access_control import DeclaredShellCommand

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
_MODEL_SELECTION_ENV = "MIMIR_MODEL_SPEC"
_MINIMAL_ENV_NAMES = frozenset({"HOME", "LANG", "TZ"})
_INTERACTIVE_ENV_NAMES = _MINIMAL_ENV_NAMES | frozenset({
    "TERM", "TMPDIR", "USER", "LOGNAME", "MIMIR_HOME",
})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CREDENTIAL_ENV_BY_EXECUTABLE = {
    "gh": frozenset({"GITHUB_TOKEN"}),
}


@dataclass(frozen=True)
class _DirectExecBinding:
    argv: tuple[str, ...]
    pass_env: tuple[str, ...] = ()
    operator_declared: bool = False


_DIRECT_EXEC_ARGV: ContextVar[_DirectExecBinding | None] = ContextVar(
    "mimir_direct_exec_argv", default=None,
)


def bind_direct_exec_argv(
    argv: list[str], *, command: str = "",
    declared: tuple[DeclaredShellCommand, ...] = (),
    operator_declared: bool = False,
) -> Token[_DirectExecBinding | None]:
    """Bind middleware-authorized argv across ToolNode's injected-arg scrub."""
    from ..access_control import _declared_command_execution_argv

    names: tuple[str, ...] = ()
    if declared:
        try:
            original = shlex.split(command)
        except ValueError:
            original = []
        for declaration in declared:
            candidate = _declared_command_execution_argv(original, (declaration,))
            if candidate is not None:
                if candidate == argv:
                    names = declaration.pass_env
                break
    return _DIRECT_EXEC_ARGV.set(_DirectExecBinding(tuple(argv), names, operator_declared))


def reset_direct_exec_argv(token: Token[_DirectExecBinding | None]) -> None:
    _DIRECT_EXEC_ARGV.reset(token)


def bound_direct_exec_argv() -> list[str] | None:
    argv = _DIRECT_EXEC_ARGV.get()
    return list(argv.argv) if argv is not None else None


def direct_exec_pass_env(argv: list[str] | None) -> tuple[str, ...]:
    binding = _DIRECT_EXEC_ARGV.get()
    if binding is not None and argv is not None and binding.argv == tuple(argv):
        return binding.pass_env
    return ()


def direct_exec_redact_names(argv: list[str] | None) -> tuple[str, ...]:
    """Mask declared values and implicit credential grants, not baseline settings.

    Resolve values from the child environment, never from a later parent snapshot.
    """
    executable = Path(argv[0]).name if argv else ""
    return tuple(dict.fromkeys((
        *direct_exec_pass_env(argv),
        *sorted(_CREDENTIAL_ENV_BY_EXECUTABLE.get(executable, ())),
    )))


def redact_direct_exec_output(
    text: str, env: dict[str, str], names: tuple[str, ...],
) -> str:
    for value in sorted({env[key] for key in names if env.get(key)}, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


def scrub_model_selection_env(env: dict[str, str]) -> None:
    """Keep Mimir's model selection out of repository-controlled children."""
    env.pop(_MODEL_SELECTION_ENV, None)


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
    return bool(argv) and Path(argv[0]).name == "git"


def _is_gh_argv(argv: list[str] | None) -> bool:
    # Authorization replaces the command with an operator-pinned absolute path,
    # which may differ from the image default used in production.
    return bool(argv) and Path(argv[0]).name == "gh"


def _minimal_direct_exec_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _MINIMAL_ENV_NAMES or key.startswith("LC_")
    }
    env["PATH"] = _TRUSTED_PATH
    return env


def disable_process_dumpability() -> None:
    """Keep same-uid shells from reading this Linux process's proc secrets.

    Call at every CLI entry and reapply after any exec. This disables core
    dumps and same-uid procfs inspection, not root/CAP_SYS_PTRACE access.
    It does not protect non-mimir same-uid processes with inherited secrets.
    Fail startup on Linux if the kernel cannot enforce the requested control.
    """
    if sys.platform != "linux":
        return
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE = 4
        error = ctypes.get_errno()
        raise OSError(error, "cannot disable server process dumpability")


def interactive_shell_env() -> dict[str, str]:
    """Select interactive child settings and exact operator-granted names."""
    env = {
        key: value for key, value in os.environ.items()
        if key in _INTERACTIVE_ENV_NAMES or key.startswith("LC_")
    }
    # login_shell_command pins PATH after bash login startup. Do not feed the
    # parent's PATH to the login startup files in the first place.
    env["PATH"] = _TRUSTED_PATH
    names = dict.fromkeys(
        name.strip() for name in os.environ.get("MIMIR_SHELL_PASS_ENV", "").split(",")
    )
    passed = [name for name in names if name != "PATH" and _ENV_NAME.fullmatch(name)
              and name in os.environ]
    for name in passed:
        env[name] = os.environ[name]
    if passed:
        from ..event_logger import log_event_sync

        log_event_sync("interactive_shell_env_passthrough", pass_env=passed)
    return env


def interactive_shell_env_overlay() -> dict[str, str | None]:
    """Remove inherited registry settings outside the interactive child env."""
    overlay: dict[str, str | None] = interactive_shell_env()
    for key in (*os.environ, "PYTHONUNBUFFERED"):
        if key not in overlay:
            overlay[key] = None
    return overlay


def refuse_protected_shell_operands(command: str, cwd: Path | None, tool: str) -> None:
    """Best-effort literal screen, not a shell sandbox.

    Globs, braces, variable indirection and interpreter code can bypass this
    screen. The child environment scrub and non-dumpable Linux mimir CLI
    processes are the controls preventing recovery of their ambient credentials.
    """
    from ..read_policy import _has_protected_read_name
    from .refusals import ToolPolicyRefusal

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="<>|;&()")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return  # Bash still handles malformed quoting as it did before.

    base = (cwd or Path.cwd()).expanduser().resolve()
    home = Path.home().resolve()
    roots = [home / ".codex", home / ".config/gh", home / ".config/gogcli"]
    gog_home = os.environ.get("GOG_HOME", "").strip()
    if gog_home:
        roots.append(Path(gog_home).expanduser().resolve())
    oauth = os.environ.get("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "").strip()
    oauth_path = Path(oauth).expanduser().resolve() if oauth else None

    for word in words:
        # Ignore shell operators; all other literal words may be operands,
        # including redirect targets and bare protected filenames like .env.
        if not word or all(char in "<>|;&()" for char in word):
            continue
        raw = word
        for name, value in (
            ("HOME", str(home)),
            ("MIMIR_HOME", os.environ.get("MIMIR_HOME", "")),
            ("GOG_HOME", gog_home),
        ):
            if not value:
                continue
            for prefix in (f"${name}", "${" + name + "}"):
                if raw == prefix or raw.startswith(prefix + "/"):
                    raw = value + raw[len(prefix):]
                    break
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        resolved = candidate.resolve()
        if (
            (candidate.parts[:2] == ("/", "proc")
             and candidate.name in {"environ", "cmdline", "mem"})
            or (resolved.parts[:2] == ("/", "proc")
                and resolved.name in {"environ", "cmdline", "mem"})
            or _has_protected_read_name(candidate)
            or _has_protected_read_name(resolved)
            or any(resolved == root or resolved.is_relative_to(root) for root in roots)
            or oauth_path is not None and resolved == oauth_path
        ):
            from .budget_gate import _emit_hard_boundary_denied

            _emit_hard_boundary_denied(
                tool=tool, boundary="protected_read_policy",
                reason="protected_name_match", target=None,
            )
            raise ToolPolicyRefusal(f"{tool} refused: protected_name_match")


def direct_exec_env(argv: list[str] | None = None) -> dict[str, str]:
    """Return a child environment safe for the server-authorized direct argv.

    Service-shell execution deliberately avoids a login shell. Its PATH contains
    only root-owned deployment directories; in particular, it excludes the
    workspace virtualenv. The project test executable and fixed arguments come
    from operator configuration rather than language-specific inference here.
    Children receive only non-secret process settings by default. Executables
    can receive exact names from the matching operator declaration. The legacy
    gh credential grant retains its config isolation and identity confirmation.
    """
    env = _minimal_direct_exec_env()
    binding = _DIRECT_EXEC_ARGV.get()
    if binding is not None and binding.operator_declared and binding.argv == tuple(argv or ()):
        # The interactive shell's explicitly configured pass-through is a
        # deployment baseline, but direct execution still pins PATH.
        for name in os.environ.get("MIMIR_SHELL_PASS_ENV", "").split(","):
            name = name.strip()
            if (name != "PATH" and _ENV_NAME.fullmatch(name) and name in os.environ
                    and not (_is_gh_argv(argv) and (name == "GITHUB_TOKEN" or name.startswith("GH_")))):
                env[name] = os.environ[name]
    names = direct_exec_pass_env(argv)
    passed = [key for key in names if key in os.environ]
    for key in passed:
        env[key] = os.environ[key]
    if names:
        from ..event_logger import log_event_sync

        log_event_sync("service_shell_env_passthrough", pass_env=passed)
    executable = Path(argv[0]).name if argv else ""
    for key in (() if binding is not None and binding.operator_declared
                else _CREDENTIAL_ENV_BY_EXECUTABLE.get(executable, ())):
        if key in os.environ:
            env[key] = os.environ[key]
    if _is_gh_argv(argv):
        # Even explicit chat declarations must not discover ambient on-disk auth.
        env["GH_CONFIG_DIR"] = _GH_CONFIG_DIR
        from .forge import confirm_github_tool_identity

        confirm_github_tool_identity(
            os.environ.get("MIMIR_GITHUB_SELF_LOGIN", ""),
            env.get("GITHUB_TOKEN", ""),
        )
    if _is_git_argv(argv):
        # The maintenance profile binds Git to a configured -C root and injects
        # config-neutralizing argv. Inherited GIT_* variables must not select a
        # different repository, config source, helper executable, or diff tool.
        env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
        })
    return env


def direct_exec_env_overlay(argv: list[str] | None = None) -> dict[str, str | None]:
    """Return an inherited-environment overlay for an authorized async argv.

    ``ShellJobRegistry`` overlays values onto its own inherited environment.
    """
    overlay: dict[str, str | None] = direct_exec_env(argv)
    # ShellJobRegistry starts from its own inherited environment and adds
    # PYTHONUNBUFFERED before applying this overlay. Remove everything outside
    # the direct-exec policy so async and sync children receive the same env.
    for key in (*os.environ, "PYTHONUNBUFFERED"):
        if key not in overlay:
            overlay[key] = None
    return overlay


__all__ = [
    "bind_direct_exec_argv",
    "bound_direct_exec_argv",
    "direct_exec_env",
    "direct_exec_env_overlay",
    "interactive_shell_env",
    "interactive_shell_env_overlay",
    "login_shell_command",
    "refuse_protected_shell_operands",
    "reset_direct_exec_argv",
    "scrub_model_selection_env",
]
