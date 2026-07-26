"""Tests for trusted-service direct-exec environment hardening."""

from __future__ import annotations

from mimir.tools._shell_env import direct_exec_env, direct_exec_env_overlay


def test_direct_exec_env_scrubs_pytest_argument_injection(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p malicious_plugin --pdb")
    monkeypatch.setenv("PYTEST_PLUGINS", "malicious_plugin")

    for argv in (["pytest", "tests"], ["uv", "run", "pytest", "tests"]):
        env = direct_exec_env(argv)
        assert "PYTEST_ADDOPTS" not in env
        assert "PYTEST_PLUGINS" not in env


def test_direct_exec_env_overlay_unsets_inherited_pytest_injection(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p malicious_plugin --pdb")
    monkeypatch.setenv("PYTEST_PLUGINS", "malicious_plugin")

    for argv in (["pytest", "tests"], ["uv", "run", "pytest", "tests"]):
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
