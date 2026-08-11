from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _imports(module: str) -> set[str]:
    tree = ast.parse((ROOT / (module.replace(".", "/") + ".py")).read_text())
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_transport_is_dependency_neutral() -> None:
    assert all(not name.startswith("mimir") for name in _imports("mimir.acp.transport"))


def test_daemon_consumes_runtime_without_constructing_or_closing_it() -> None:
    source = (ROOT / "mimir/acp/daemon.py").read_text()
    assert "create_agent_runtime" not in source
    assert "create_core_services" not in source
    assert ".aclose(" not in source
    assert "start_unix_server" in source
    assert "start_server(" not in source
