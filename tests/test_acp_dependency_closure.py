from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acp_dependency_closure", ROOT / ".github" / "acp_dependency_closure.py")
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_proxy_relay_and_client_closures_are_finite_and_runtime_blind() -> None:
    module.assert_policy(module.module_paths(ROOT / "mimir"))


@pytest.mark.parametrize("source", [
    "from mimir.runtime import create_core_services\ncreate_core_services()\n",
    "import mimir.runtime as r\nbuild = r.create_agent_runtime\nbuild()\n",
    "from mimir import runtime as r\nctor = r.AgentRuntimeBundle\nctor()\n",
    "from mimir.runtime import AgentRuntimeBundle as B\ncloser = B.aclose\ncloser(value)\n",
    "async def stop(value):\n    await value.aclose()\n",
])
def test_forbidden_constructor_and_closer_aliases_are_detected(tmp_path: Path, source: str) -> None:
    paths = module.module_paths(ROOT / "mimir")
    paths = dict(paths)
    path = tmp_path / "proxy.py"
    path.write_text("from . import profiles, credentials, transport\n" + source)
    paths["mimir.acp.proxy"] = path
    with pytest.raises(AssertionError, match="forbidden"):
        module.assert_policy(paths)


def test_transitive_sink_is_detected(tmp_path: Path) -> None:
    paths = module.module_paths(ROOT / "mimir")
    paths = dict(paths)
    proxy = tmp_path / "proxy.py"
    helper = tmp_path / "profiles.py"
    proxy.write_text("from . import profiles, credentials, transport\n")
    helper.write_text("from mimir.runtime import create_core_services as build\nbuild()\n")
    paths["mimir.acp.proxy"] = proxy
    paths["mimir.acp.profiles"] = helper
    with pytest.raises(AssertionError, match="forbidden"):
        module.assert_policy(paths)


def test_entrypoint_acp_branch_has_only_the_client_dispatch() -> None:
    tree = ast.parse((ROOT / "mimir" / "entrypoint.py").read_text())
    branch = next(node for node in ast.walk(tree) if isinstance(node, ast.If))
    imports = [node.module for statement in branch.body for node in ast.walk(statement) if isinstance(node, ast.ImportFrom)]
    assert imports == ["mimir.acp.bootstrap"]
    assert any(isinstance(node, ast.Return) for statement in branch.body for node in ast.walk(statement))


def test_daemon_consumes_runtime_without_constructing_or_closing_it() -> None:
    source = (ROOT / "mimir" / "acp" / "daemon.py").read_text()
    assert "create_core_services" not in source
    assert "create_agent_runtime" not in source
    assert ".aclose(" not in source


def test_composition_is_globally_absent() -> None:
    assert not (ROOT / "mimir" / "acp" / "composition.py").exists()
