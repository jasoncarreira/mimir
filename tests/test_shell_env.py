"""Tests for trusted-service direct-exec environment hardening."""

from __future__ import annotations

from mimir.tools._shell_env import direct_exec_env


def test_direct_exec_env_scrubs_pytest_argument_injection(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p malicious_plugin --pdb")
    monkeypatch.setenv("PYTEST_PLUGINS", "malicious_plugin")

    for argv in (["pytest", "tests"], ["uv", "run", "pytest", "tests"]):
        env = direct_exec_env(argv)
        assert "PYTEST_ADDOPTS" not in env
        assert "PYTEST_PLUGINS" not in env


def test_direct_exec_env_preserves_unrelated_command_environment(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q")
    monkeypatch.setenv("PYTEST_PLUGINS", "example")

    env = direct_exec_env(["git", "status"])

    assert env["PYTEST_ADDOPTS"] == "-q"
    assert env["PYTEST_PLUGINS"] == "example"
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in env
