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
