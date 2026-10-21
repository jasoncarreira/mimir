"""The selected provider does not depend on retired core coding modules."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PROVIDER = Path(__file__).resolve().parents[1] / "mimir_cultivate"
RETIRED = ("mimir.worklink", "mimir.contained_execution", "mimir.opencode_config")
RETIRED_TYPES = frozenset({
    "RepoPRAction", "RepoPRActionScope", "RepoPRScopeProvenance",
    "RepoPRScopeRegistry", "RepoReviewState", "RetainedFactoryScope",
    "ServerDiscoveredPRStates", "ServerDiscoveredPRScopeStore",
})


def test_retired_core_sources_are_absent() -> None:
    assert not tuple((ROOT / "mimir/worklink").rglob("*.py"))
    assert not (ROOT / "mimir/contained_execution.py").exists()
    assert not (ROOT / "mimir/opencode_config.py").exists()


def test_provider_imports_do_not_point_back_to_retired_core() -> None:
    for path in PROVIDER.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Import):
                imports = (alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module == "mimir.models":
                    assert RETIRED_TYPES.isdisjoint(alias.name for alias in node.names), path
                imports = (node.module or "",)
            else:
                continue
            assert all(not (name == old or name.startswith(old + "."))
                       for name in imports for old in RETIRED), path
