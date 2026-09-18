from __future__ import annotations

import ast
from pathlib import Path

from mimir.worklink.attention import AttentionSource, SOURCE_RULES


ROOT = Path(__file__).parent.parent


def _produced_sources(relative: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    values: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "AttentionSource"
        ):
            values.add(node.attr.lower())
    return values


def test_real_transport_boundaries_own_stable_reservations_and_sources() -> None:
    queue = _produced_sources(
        "mimir/optional-skills/chainlink-orchestrator/scripts/poller.py"
    )
    cli = _produced_sources("mimir/commands/worklink.py")
    startup = _produced_sources("mimir/server.py")
    runtime = _produced_sources("mimir/worklink/orchestrator.py")
    ledger = _produced_sources("mimir/worklink/dispatch_failures.py")
    assert {
        "queue_leaf_spawn", "queue_factory_spawn",
    } <= queue
    assert {"leaf_cli_repository", "factory_cli_repository"} <= cli
    assert {"startup_leaf_spawn", "startup_factory_spawn"} <= startup
    assert {
        "leaf_backend_outcome", "factory_partial", "factory_blocked", "factory_parked",
        "factory_driver_exit",
    } <= runtime
    assert {"leaf_start", "factory_start", "leaf_success", "factory_success"} <= ledger


def test_closed_source_inventory_has_policy_and_clearance_for_every_member() -> None:
    assert set(SOURCE_RULES) == set(AttentionSource)
