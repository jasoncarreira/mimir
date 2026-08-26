"""Tests for trusted-service direct-exec environment hardening."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mimir.tools import _shell_env
from mimir.tools._shell_env import direct_exec_env, direct_exec_env_overlay


def test_direct_exec_env_defaults_to_minimal_non_secret_environment(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q")
    monkeypatch.setenv("PYTEST_PLUGINS", "example")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("HOME", "/safe/home")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_TEST_SENTINEL", "terminal-setting")

    env = direct_exec_env(["/bin/echo", "status"])

    assert env == {
        "HOME": "/safe/home",
        "LANG": "C.UTF-8",
        "PATH": _shell_env._TRUSTED_PATH,
    } | {
        key: value for key, value in os.environ.items() if key.startswith("LC_")
    }


def test_login_shell_command_keeps_venv_console_scripts_after_system_tools() -> None:
    venv_bin = os.path.dirname(sys.executable)
    wrapped = _shell_env.login_shell_command("mimir --help")
    exported_path = wrapped.splitlines()[0].removeprefix("export PATH=")

    assert exported_path.split(os.pathsep) == [
        *_shell_env._TRUSTED_PATH_DIRS,
        venv_bin,
    ]
    assert wrapped.endswith("\nmimir --help")


def test_direct_exec_env_discards_writable_path_and_does_not_select_decoy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    decoy_dir = repo_root / ".venv" / "bin"
    decoy_dir.mkdir(parents=True)
    decoy = decoy_dir / "pwd"
    decoy.write_text("#!/bin/sh\nprintf 'DECOY\\n'\n", encoding="utf-8")
    decoy.chmod(0o755)
    trusted_dir = tmp_path / "image-root" / "bin"
    trusted_dir.mkdir(parents=True)
    trusted_tool = trusted_dir / "pwd"
    trusted_tool.write_text("#!/bin/sh\nprintf 'SYSTEM\\n'\n", encoding="utf-8")
    trusted_tool.chmod(0o755)
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo_root}:rw")
    monkeypatch.setenv("PATH", os.pathsep.join((str(decoy_dir), "/usr/bin", "/bin")))
    monkeypatch.setattr(_shell_env, "_TRUSTED_PATH", str(trusted_dir))

    env = direct_exec_env(["pwd"])
    completed = subprocess.run(
        ["pwd"], capture_output=True, check=True, env=env, cwd=repo_root, text=True,
    )

    assert completed.stdout.strip() == "SYSTEM"
    assert all(
        not Path(entry).resolve().is_relative_to(repo_root.resolve())
        for entry in env["PATH"].split(os.pathsep)
    )


def test_direct_exec_env_uv_run_uses_project_virtualenv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        import pytest

        pytest.skip("uv is not installed")

    project = tmp_path / "project"
    venv_bin = project / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    venv_bin.mkdir(parents=True)
    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        "[project]\nname = 'uv-path-probe'\nversion = '0.0.0'\n"
        "requires-python = '>=3.11'\n",
        encoding="utf-8",
    )
    pyvenv = project / ".venv" / "pyvenv.cfg"
    # A venv's `home` must name the BASE interpreter's directory. `sys.executable`
    # is only that when pytest itself runs on a base interpreter; under `uv run
    # pytest` it is this project's own `.venv/bin/python`, and a venv derived from
    # it has no stdlib -- uv's Python query then dies with "No module named
    # 'encodings'" and the probe fails for a reason unrelated to what it asserts.
    # `sys._base_executable` is what the stdlib `venv` module writes here.
    base_executable = Path(getattr(sys, "_base_executable", None) or sys.executable)
    pyvenv.write_text(
        f"home = {base_executable.parent}\n"
        f"executable = {base_executable}\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
        encoding="utf-8",
    )
    venv_python = venv_bin / "python"
    venv_python.symlink_to(base_executable)
    monkeypatch.setattr(_shell_env, "_TRUSTED_PATH", str(Path(uv).parent))
    env = direct_exec_env([uv, "run", "python"])
    env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")

    completed = subprocess.run(
        [uv, "run", "python", "-c", "import sys; print(sys.prefix)"],
        capture_output=True,
        check=True,
        cwd=project,
        env=env,
        text=True,
    )

    assert Path(completed.stdout.strip()).resolve() == (project / ".venv").resolve()


def test_direct_exec_env_scrubs_git_repository_and_helper_injection(monkeypatch) -> None:
    injected = {
        "GIT_DIR": "/outside/.git",
        "GIT_WORK_TREE": "/outside",
        "GIT_CONFIG_GLOBAL": "/outside/config",
        "GIT_CONFIG_SYSTEM": "/outside/system-config",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "/outside/helper",
        "GIT_EXEC_PATH": "/outside/git-core",
        "GIT_EXTERNAL_DIFF": "/outside/diff",
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)

    env = direct_exec_env(["/usr/bin/git", "status"])
    overlay = direct_exec_env_overlay(["/usr/bin/git", "status"])

    assert all(key not in env for key in injected)
    assert all(overlay[key] is None for key in injected)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_PAGER"] == "cat"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"


def test_git_uses_minimal_env_and_repository_credentials(monkeypatch) -> None:
    """Only review pull/push need auth, supplied by the configured helper.

    Inspection Git has ``credential.helper=`` injected into argv. Authorized
    review pull/push omit that override and use the operator-installed,
    repository-scoped credential helper; neither case needs a token in env.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/credential-helper")

    env = direct_exec_env(["/usr/bin/git", "push", "origin", "topic"])

    assert "GITHUB_TOKEN" not in env
    assert "GIT_ASKPASS" not in env
    assert set(env) <= {
        "PATH", "HOME", "LANG", "TZ", "GIT_CONFIG_NOSYSTEM", "GIT_PAGER",
        "GIT_OPTIONAL_LOCKS",
    } | {key for key in env if key.startswith("LC_")}


def test_gh_env_uses_isolated_config_and_scrubs_alternate_credentials(monkeypatch) -> None:
    from mimir.forge import github as github_module

    monkeypatch.setenv("GITHUB_TOKEN", "declared-token")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setenv("GH_TOKEN", "stray-token")
    monkeypatch.setenv("GH_HOST", "attacker.invalid")
    monkeypatch.setenv("GH_CONFIG_DIR", "/tmp/stray-gh-config")
    monkeypatch.setattr(github_module, "_verified_identity", (
        "reviewer", hashlib.sha256(b"declared-token").hexdigest(),
    ))
    from mimir.tools import forge as forge_tools
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_error", None)

    env = direct_exec_env(["/usr/bin/gh", "api", "user"])
    overlay = direct_exec_env_overlay(["/usr/bin/gh", "api", "user"])

    assert env["GITHUB_TOKEN"] == "declared-token"
    assert "GH_TOKEN" not in env
    assert "GH_HOST" not in env
    assert env["GH_CONFIG_DIR"] == _shell_env._GH_CONFIG_DIR
    assert env["GH_CONFIG_DIR"] != "/tmp/stray-gh-config"
    assert Path(env["GH_CONFIG_DIR"]).stat().st_mode & 0o777 == 0o500
    assert overlay["GH_TOKEN"] is None
    assert overlay["GH_HOST"] is None


def test_non_gh_direct_exec_scrubs_github_cli_selection(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "stray-token")
    monkeypatch.setenv("GH_HOST", "attacker.invalid")
    monkeypatch.setenv("GH_CONFIG_DIR", "/tmp/stray-gh-config")
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:agent-model")

    env = direct_exec_env(["/bin/echo", "status"])
    overlay = direct_exec_env_overlay(["/bin/echo", "status"])

    scrubbed = ("GH_TOKEN", "GH_HOST", "GH_CONFIG_DIR", "MIMIR_MODEL_SPEC")
    assert all(key not in env for key in scrubbed)
    assert all(overlay[key] is None for key in scrubbed)


def test_jq_direct_exec_env_contains_only_non_secret_process_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JQ_ENV_SENTINEL", "super-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("HOME", "/safe/home")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_TIME", "C")
    monkeypatch.setenv("TZ", "UTC")

    argv = ["/usr/bin/jq", "--null-input", "env"]
    env = direct_exec_env(argv)
    overlay = direct_exec_env_overlay(argv)

    assert all(
        key in {"PATH", "HOME", "LANG", "TZ"} or key.startswith("LC_")
        for key in env
    )
    assert env["PATH"] == _shell_env._TRUSTED_PATH
    assert env["HOME"] == "/safe/home"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_TIME"] == "C"
    assert env["TZ"] == "UTC"
    assert "JQ_ENV_SENTINEL" not in env
    assert "GITHUB_TOKEN" not in env
    assert overlay["JQ_ENV_SENTINEL"] is None
    assert overlay["GITHUB_TOKEN"] is None
    assert overlay["PYTHONUNBUFFERED"] is None


def test_every_pinned_service_command_inherits_safe_default(
    monkeypatch: pytest.MonkeyPatch,
    maintenance_pinned_executables: dict[str, Path],
) -> None:
    from mimir import access_control

    monkeypatch.setenv("SERVICE_ENV_SENTINEL", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")

    assert set(_shell_env._CREDENTIAL_ENV_BY_EXECUTABLE) == {"gh"}
    for command in access_control._MAINTENANCE_PINNED_EXECUTABLE_DEFAULTS:
        if command == "gh":
            continue
        argv = [str(maintenance_pinned_executables[command])]
        env = direct_exec_env(argv)
        expected = _shell_env._minimal_direct_exec_env()
        if command == "git":
            expected.update({
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_PAGER": "cat",
                "GIT_OPTIONAL_LOCKS": "0",
            })
        assert "SERVICE_ENV_SENTINEL" not in env, command
        assert "GITHUB_TOKEN" not in env, command
        assert env == expected, (command, env)


def _run_admitted_proc_reader(command: str) -> bytes:
    """Fork so ``{pid}`` can name the process that will exec the reader."""
    from mimir.access_control import parse_service_shell_argv

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            os.dup2(write_fd, 1)
            os.close(write_fd)
            argv = parse_service_shell_argv(
                command.format(pid=os.getpid()), "maintenance",
            )
            if argv is None:
                os._exit(120)
            os.execve(argv[0], argv, direct_exec_env(argv))
        except BaseException:
            os._exit(121)

    os.close(write_fd)
    chunks = []
    while chunk := os.read(read_fd, 65536):
        chunks.append(chunk)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    return b"".join(chunks)


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="Linux /proc required")
@pytest.mark.parametrize("reader", ["cat", "head -c 65536", "tail -c 65536"])
@pytest.mark.parametrize(
    "operand", ["/proc/self/environ", "/proc/self/../self/environ", "/proc/{pid}/environ"],
)
def test_admitted_file_readers_cannot_disclose_child_environment(
    reader: str,
    operand: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import access_control

    for command in ("cat", "head", "tail"):
        executable = Path(shutil.which(command) or "").resolve(strict=True)
        monkeypatch.setitem(access_control._MAINTENANCE_PINNED_EXECUTABLES, command, executable)
    sentinel = b"SERVICE_PROC_ENV_SENTINEL=super-secret-value"
    monkeypatch.setenv("SERVICE_PROC_ENV_SENTINEL", "super-secret-value")

    output = _run_admitted_proc_reader(f"{reader} {operand}")

    assert sentinel not in output


@pytest.mark.parametrize(
    ("command", "expected"),
    [("cat", "alpha\nbeta\n"), ("head -c 5", "alpha"), ("tail -c 5", "beta\n")],
)
def test_admitted_file_readers_still_read_repository_files(
    command: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import access_control
    from mimir.access_control import parse_service_shell_argv

    executable_name = command.split()[0]
    executable = Path(shutil.which(executable_name) or "").resolve(strict=True)
    monkeypatch.setitem(
        access_control._MAINTENANCE_PINNED_EXECUTABLES, executable_name, executable,
    )
    sample = tmp_path / "sample.txt"
    sample.write_text("alpha\nbeta\n", encoding="utf-8")
    argv = parse_service_shell_argv(
        f"{command} {shlex.quote(str(sample))}", "maintenance",
    )

    assert argv is not None
    completed = subprocess.run(
        argv, capture_output=True, check=True, env=direct_exec_env(argv), text=True,
    )
    assert completed.stdout == expected


def test_real_jq_cannot_read_parent_credentials_and_still_filters_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is not installed")

    sample = tmp_path / "sample.json"
    sample.write_text('{"name": "Ada"}\n', encoding="utf-8")
    sentinel = "super-secret-value-12345"
    monkeypatch.setenv("JQ_ENV_SENTINEL", sentinel)
    monkeypatch.setenv("GITHUB_TOKEN", sentinel)
    commands = (
        [jq, "--null-input", "env"],
        [jq, "env", str(sample)],
        [jq, "$ENV.GITHUB_TOKEN", str(sample)],
        [jq, "-r", "$ENV|tostring", str(sample)],
    )

    for argv in commands:
        completed = subprocess.run(
            argv, capture_output=True, check=True, env=direct_exec_env(argv), text=True,
        )
        assert sentinel not in completed.stdout, argv

    legitimate = [jq, "-r", ".name", str(sample)]
    completed = subprocess.run(
        legitimate,
        capture_output=True,
        check=True,
        env=direct_exec_env(legitimate),
        text=True,
    )
    assert completed.stdout == "Ada\n"


@pytest.mark.parametrize("arguments", [
    ["api", "user"],
    ["pr", "view", "17"],
    ["pr", "review", "17", "--approve"],
])
def test_every_gh_command_requires_cached_declared_identity(monkeypatch, arguments) -> None:
    from mimir.forge import github as github_module
    from mimir.tools import forge as forge_tools
    from mimir.tools.refusals import ToolPolicyRefusal

    monkeypatch.setenv("GITHUB_TOKEN", "declared-token")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(github_module, "_verified_identity", None)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_error", None)
    argv = ["/usr/bin/gh", *arguments]

    with pytest.raises(ToolPolicyRefusal, match="cache is empty"):
        direct_exec_env(argv)
    assert forge_tools.github_identity_is_degraded() is True

    monkeypatch.setattr(github_module, "_verified_identity", (
        "reviewer", hashlib.sha256(b"declared-token").hexdigest(),
    ))
    with pytest.raises(ToolPolicyRefusal, match="disabled until restart"):
        direct_exec_env(argv)
