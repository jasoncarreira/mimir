"""Tests for trusted-service direct-exec environment hardening."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mimir.tools import _shell_env
from mimir.tools._shell_env import direct_exec_env, direct_exec_env_overlay


def test_direct_exec_env_preserves_unrelated_command_environment(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q")
    monkeypatch.setenv("PYTEST_PLUGINS", "example")

    env = direct_exec_env(["/bin/echo", "status"])

    assert env["PYTEST_ADDOPTS"] == "-q"
    assert env["PYTEST_PLUGINS"] == "example"
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in env


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

    completed = subprocess.run(
        [uv, "run", "python", "-c", "import sys; print(sys.prefix)"],
        capture_output=True,
        check=True,
        cwd=project,
        env=direct_exec_env([uv, "run", "python"]),
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
