"""Core proposal reads must work without the coding provider."""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


def test_proposal_git_environment_is_core_only() -> None:
    script = """
import sys
from mimir.opencode_proposal import _sanitized_git_env
assert 'mimir_cultivate' not in sys.modules
env = _sanitized_git_env()
assert env['GIT_CONFIG_GLOBAL'] == '/dev/null'
assert env['GIT_TERMINAL_PROMPT'] == '0'
assert env['GIT_ASKPASS'] == '/bin/false'
assert 'mimir_cultivate' not in sys.modules
"""
    env = {**os.environ, "MIMIR_PLUGINS": ""}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True,
        text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_core_shell_does_not_import_provider_or_grant_forge_credentials() -> None:
    script = """
import os, subprocess, sys
from mimir.tools._shell_env import direct_exec_env, direct_exec_env_overlay
assert 'mimir_cultivate' not in sys.modules
for argv in (['/usr/bin/gh', 'api', 'user'], ['/usr/bin/git', 'status']):
    env = direct_exec_env(argv)
    assert 'GITHUB_TOKEN' not in env
    assert 'GH_CONFIG_DIR' not in env
    assert 'GIT_ASKPASS' not in env
    overlay = direct_exec_env_overlay(argv)
    assert overlay['GITHUB_TOKEN'] is None
    assert overlay['GH_TOKEN'] is None
assert subprocess.run(['/bin/echo', 'generic-ok'], capture_output=True,
                      env=direct_exec_env(['/bin/echo', 'generic-ok']),
                      text=True, check=True).stdout.strip() == 'generic-ok'
assert 'mimir_cultivate' not in sys.modules
"""
    env = {**os.environ, "MIMIR_PLUGINS": "", "GITHUB_TOKEN": "secret",
           "GH_TOKEN": "other-secret", "GIT_ASKPASS": "/tmp/untrusted-helper"}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True,
        text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_offline_parity_runs_from_provider_without_core_parity_module() -> None:
    script = """
import sys
from mimir_cultivate.repository import offline_canary_probes, run_parity
assert 'mimir.repo_pr_parity' not in sys.modules
result = run_parity(offline_canary_probes())
assert result['cutover_blocked'] is False
assert result['counts']['operations_matched'] == 14
assert 'mimir.repo_pr_parity' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env={**os.environ, "MIMIR_PLUGINS": "cultivate"},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_repository_inventory_import_closure() -> None:
    root = Path(__file__).resolve().parents[3]
    retired = {"mimir.repository_config", "mimir.repo_pr_parity"}
    for source_root in (root / "mimir", root / "plugins/cultivate/mimir_cultivate"):
        for path in source_root.rglob("*.py"):
            module = ".".join(path.relative_to(root).with_suffix("").parts)
            if source_root.name == "mimir_cultivate":
                module = ".".join(("mimir_cultivate", *path.relative_to(source_root).with_suffix("").parts))
            package = module.removesuffix(".__init__") if path.stem == "__init__" else module.rpartition(".")[0]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = (alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    name = "." * node.level + (node.module or "")
                    names = (importlib.util.resolve_name(name, package),) if node.level else (name,)
                else:
                    continue
                assert not retired.intersection(names), f"{path}: retired module import"


def test_core_only_config_boots_with_repository_file_without_loading_provider(tmp_path: Path) -> None:
    (tmp_path / "repositories.yaml").write_text("repositories: []\n", encoding="utf-8")
    script = """
import sys
from mimir.config import Config
from mimir import access_control
from mimir.worklink import orchestrator, merge_closure
config = Config.from_env()
assert config.plugin_manager.policy.policy_consumer_hooks is None
assert access_control._repository_inventory(str(config.home)) is None
assert access_control._configured_repo_write_roots() == []
assert 'mimir.repository_config' not in sys.modules
assert 'mimir_cultivate' not in sys.modules
"""
    env = {**os.environ, "MIMIR_PLUGINS": "", "MIMIR_HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_selected_repository_inventory_uses_registered_owner(tmp_path: Path) -> None:
    (tmp_path / "repositories.yaml").write_text("repositories: []\n", encoding="utf-8")
    script = """
import sys
from mimir.plugins.core import policy_scope
from mimir.plugins.manager import load_plugins
from mimir import access_control
selected = load_plugins(enable='cultivate').policy
with policy_scope(selected):
    inventory = access_control._repository_inventory(sys.argv[1])
    assert inventory.declared
    assert type(inventory).__module__ == 'mimir_cultivate.repository'
assert 'mimir.repository_config' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={**os.environ, "MIMIR_PLUGINS": "cultivate"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
