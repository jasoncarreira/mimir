"""Core proposal reads must work without the coding provider."""

from __future__ import annotations

import os
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
