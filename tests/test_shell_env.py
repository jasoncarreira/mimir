"""Tests for trusted-service direct-exec environment hardening."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from mimir.tools import _shell_env
from mimir.tools._shell_env import direct_exec_env, direct_exec_env_overlay


def test_direct_exec_env_scrubs_pytest_argument_injection(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p malicious_plugin --pdb")
    monkeypatch.setenv("PYTEST_PLUGINS", "malicious_plugin")

    for argv in (
        ["pytest", "tests"],
        ["/workspace/mimir/.venv/bin/pytest", "tests"],
        ["/usr/local/bin/python", "-m", "pytest", "tests"],
        ["uv", "run", "pytest", "tests"],
        ["/usr/local/bin/uv", "run", "pytest", "tests"],
    ):
        env = direct_exec_env(argv)
        assert "PYTEST_ADDOPTS" not in env
        assert "PYTEST_PLUGINS" not in env


def test_direct_exec_env_overlay_unsets_inherited_pytest_injection(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p malicious_plugin --pdb")
    monkeypatch.setenv("PYTEST_PLUGINS", "malicious_plugin")

    for argv in (
        ["pytest", "tests"],
        ["/workspace/mimir/.venv/bin/pytest", "tests"],
        ["/usr/local/bin/python", "-m", "pytest", "tests"],
        ["uv", "run", "pytest", "tests"],
        ["/usr/local/bin/uv", "run", "pytest", "tests"],
    ):
        overlay = direct_exec_env_overlay(argv)
        assert overlay["PYTEST_ADDOPTS"] is None
        assert overlay["PYTEST_PLUGINS"] is None


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
    pyvenv.write_text(
        f"home = {Path(sys.executable).parent}\n"
        f"executable = {sys.executable}\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
        encoding="utf-8",
    )
    venv_python = venv_bin / "python"
    venv_python.symlink_to(sys.executable)
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
