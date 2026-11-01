"""Acceptance probes not covered by the existing traced-turn integration tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_disabled_mimir_imports_no_opentelemetry_api_even_with_mcp_present() -> None:
    """Distinguish Mimir-owned imports from MCP's independent API dependency."""
    source = '''
import builtins
import inspect

original_import = builtins.__import__
owned = []

def observe(name, *args, **kwargs):
    caller = inspect.currentframe().f_back.f_globals.get("__name__", "")
    if caller.startswith("mimir.") and name.startswith("opentelemetry"):
        owned.append((caller, name))
        raise ImportError("Mimir imported optional telemetry while disabled")
    return original_import(name, *args, **kwargs)

builtins.__import__ = observe
import mimir.server
import mimir.runtime
import mimir.agent
from mimir.tracing import TraceController
assert TraceController.create(enabled=False, endpoint="http://localhost:4318") is None
assert owned == [], owned
'''
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
