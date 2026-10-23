"""Core proposal reads must work without the coding provider."""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


_RETIRED_CORE_MODULES = {
    "mimir.ci_attention": "mimir/ci_attention.py",
    "mimir.ci_logs": "mimir/ci_logs.py",
    "mimir.coding": "mimir/coding.py",
    "mimir.contained_checkout": "mimir/contained_checkout.py",
    "mimir.contained_execution": "mimir/contained_execution.py",
    "mimir.forge": "mimir/forge/__init__.py",
    "mimir.forge.client": "mimir/forge/client.py",
    "mimir.forge.github": "mimir/forge/github.py",
    "mimir.github_withhold": "mimir/github_withhold.py",
    "mimir.opencode_config": "mimir/opencode_config.py",
    "mimir.opencode_proposal": "mimir/opencode_proposal.py",
    "mimir.pr_board": "mimir/pr_board.py",
    "mimir.pr_checkout_lease": "mimir/pr_checkout_lease.py",
    "mimir.probe_github_token": "mimir/probe_github_token.py",
    "mimir.project_tests": "mimir/project_tests.py",
    "mimir.repo_pr_parity": "mimir/repo_pr_parity.py",
    "mimir.repo_tools": "mimir/repo_tools.py",
    "mimir.repository_config": "mimir/repository_config.py",
    "mimir.tools.forge": "mimir/tools/forge.py",
    "mimir.tools.github_review_guard": "mimir/tools/github_review_guard.py",
    "mimir.tools.repo": "mimir/tools/repo.py",
    "mimir.worklink": "mimir/worklink/__init__.py",
}
#: Retired packages: no module may survive anywhere beneath them.
_RETIRED_CORE_PACKAGES = ("mimir/forge", "mimir/worklink")
_DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__"})


def _imported_names(node: ast.AST, package: str) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        name = "." * node.level + (node.module or "")
        origin = importlib.util.resolve_name(name, package) if node.level else name
        return (origin, *(f"{origin}.{alias.name}" for alias in node.names))
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        target = node.args[0]
        if (
            callee in _DYNAMIC_IMPORTERS
            and isinstance(target, ast.Constant) and isinstance(target.value, str)
        ):
            return (target.value,)
    return ()


def _retired_import(name: str) -> bool:
    return any(name == module or name.startswith(module + ".")
               for module in _RETIRED_CORE_MODULES)


@pytest.mark.parametrize(("source", "package", "imported"), [
    ("from mimir import forge", "mimir", "mimir.forge"),
    ("from mimir.tools import repo", "mimir.tools", "mimir.tools.repo"),
    ("from . import github_review_guard", "mimir.tools", "mimir.tools.github_review_guard"),
    ("from ..forge import github", "mimir.worklink", "mimir.forge.github"),
    ("import mimir.pr_checkout_lease", "mimir", "mimir.pr_checkout_lease"),
    ("from mimir import project_tests", "mimir", "mimir.project_tests"),
    ("from mimir import worklink", "mimir", "mimir.worklink"),
    ("from mimir.worklink.claims import ChainlinkClaims", "mimir", "mimir.worklink.claims"),
    ("from . import pr_board", "mimir", "mimir.pr_board"),
    ("from mimir import ci_logs, coding", "mimir", "mimir.coding"),
    ("def lazy():\n    from mimir.ci_attention import x", "mimir", "mimir.ci_attention"),
    ("import mimir.github_withhold as gw", "mimir", "mimir.github_withhold"),
    ("importlib.import_module('mimir.probe_github_token')", "mimir", "mimir.probe_github_token"),
    ("__import__('mimir.opencode_config')", "mimir", "mimir.opencode_config"),
    ("from .contained_execution import run", "mimir", "mimir.contained_execution"),
])
def test_retired_coding_import_aliases_are_detected(
    source: str, package: str, imported: str,
) -> None:
    names = {name for node in ast.walk(ast.parse(source))
             for name in _imported_names(node, package)}
    assert imported in names
    assert any(_retired_import(name) for name in names)


def test_opencode_proposal_git_environment_is_provider_owned() -> None:
    pytest.importorskip("mimir_cultivate")
    script = """
import sys
from mimir_cultivate.opencode import _sanitized_git_env
assert 'mimir.opencode_proposal' not in sys.modules
env = _sanitized_git_env()
assert env['GIT_CONFIG_GLOBAL'] == '/dev/null'
assert env['GIT_TERMINAL_PROMPT'] == '0'
assert env['GIT_ASKPASS'] == '/bin/false'
assert 'mimir.opencode_proposal' not in sys.modules
"""
    env = {**os.environ, "MIMIR_PLUGINS": "cultivate"}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True,
        text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_core_spawn_inventory_does_not_import_disabled_provider() -> None:
    script = """
import sys
from mimir.tools.registry import all_mimir_tools
all_mimir_tools(model_spec='openai:test')
assert not any(name == 'mimir_cultivate' or name.startswith('mimir_cultivate.')
               for name in sys.modules)
assert 'mimir.opencode_proposal' not in sys.modules
assert 'mimir.contained_checkout' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "MIMIR_PLUGINS": ""},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_core_shell_does_not_import_provider_and_grants_only_the_process_token() -> None:
    script = """
import os, subprocess, sys
from mimir.tools import _shell_env
from mimir.tools._shell_env import direct_exec_env, direct_exec_env_overlay
from mimir import github_identity
assert 'mimir_cultivate' not in sys.modules
github_identity._verified_identity = ('reviewer', github_identity.credential_fingerprint('secret'))
for argv in (['/usr/bin/gh', 'api', 'user'], ['/usr/bin/git', 'status']):
    env = direct_exec_env(argv)
    overlay = direct_exec_env_overlay(argv)
    if argv[0].endswith('/gh'):
        # Core's floor: the process token only, and an empty isolated config,
        # never ambient on-disk auth.
        assert env['GITHUB_TOKEN'] == 'secret'
        assert env['GH_CONFIG_DIR'] == _shell_env._GH_CONFIG_DIR
        assert not os.listdir(env['GH_CONFIG_DIR'])
    else:
        assert 'GITHUB_TOKEN' not in env and overlay['GITHUB_TOKEN'] is None
        assert 'GH_CONFIG_DIR' not in env
        assert env['GIT_CONFIG_NOSYSTEM'] == '1'
    assert 'GIT_ASKPASS' not in env
    assert overlay['GH_TOKEN'] is None
assert subprocess.run(['/bin/echo', 'generic-ok'], capture_output=True,
                      env=direct_exec_env(['/bin/echo', 'generic-ok']),
                      text=True, check=True).stdout.strip() == 'generic-ok'
assert 'mimir_cultivate' not in sys.modules
"""
    env = {**os.environ, "MIMIR_PLUGINS": "", "GITHUB_TOKEN": "secret",
           "MIMIR_GITHUB_SELF_LOGIN": "reviewer",
           "GH_TOKEN": "other-secret", "GIT_ASKPASS": "/tmp/untrusted-helper",
           "GH_CONFIG_DIR": "/tmp/ambient-gh-config"}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True,
        text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_offline_parity_runs_from_provider_without_core_parity_module() -> None:
    pytest.importorskip("mimir_cultivate")
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


def test_retired_coding_modules_have_no_surviving_importers() -> None:
    root = Path(__file__).resolve().parents[3]
    for module, relative_path in _RETIRED_CORE_MODULES.items():
        assert not (root / relative_path).exists(), module
    for package in _RETIRED_CORE_PACKAGES:
        assert not list((root / package).rglob("*.py")), package
    for source_root in (root / "mimir", root / "plugins/cultivate/mimir_cultivate"):
        for path in source_root.rglob("*.py"):
            module = ".".join(path.relative_to(root).with_suffix("").parts)
            if source_root.name == "mimir_cultivate":
                module = ".".join(("mimir_cultivate", *path.relative_to(source_root).with_suffix("").parts))
            package = module.removesuffix(".__init__") if path.stem == "__init__" else module.rpartition(".")[0]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                for name in _imported_names(node, package):
                    assert not _retired_import(name), f"{path}: retired module import {name}"


def test_core_only_config_boots_with_repository_file_without_loading_provider(tmp_path: Path) -> None:
    (tmp_path / "repositories.yaml").write_text("repositories: []\n", encoding="utf-8")
    script = """
import sys
from mimir.config import Config
from mimir import access_control
config = Config.from_env()
assert config.plugin_manager.policy.policy_consumer_hooks is None
assert access_control._repository_inventory(str(config.home)) is None
assert access_control._configured_repo_write_roots() == []
assert 'mimir.repository_config' not in sys.modules
assert not any(name == 'mimir.worklink' or name.startswith('mimir.worklink.') for name in sys.modules)
assert 'mimir_cultivate' not in sys.modules
"""
    env = {**os.environ, "MIMIR_PLUGINS": "", "MIMIR_HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_selected_repository_inventory_uses_registered_owner(tmp_path: Path) -> None:
    pytest.importorskip("mimir_cultivate")
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
