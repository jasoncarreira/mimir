from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


def _load_smoke_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "worklink_docker_sibling_smoke.py"
    spec = importlib.util.spec_from_file_location("worklink_docker_sibling_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_docker_smoke_registry_routes_through_docker_sibling() -> None:
    smoke = _load_smoke_module()

    registry = smoke.build_registry(
        broker_url="unix:///tmp/worklink-broker.sock",
        image="mimir-worklink:test",
        network="none",
    )

    compute = registry.select_compute(labels={"worklink"}, repo="jasoncarreira/mimir")
    assert compute.name == "docker_sibling"
    assert compute.capabilities().shared_filesystem is False
    assert compute.capabilities().network_isolated is True


def test_docker_smoke_preflight_requires_existing_unix_socket(tmp_path: Path) -> None:
    smoke = _load_smoke_module()

    with pytest.raises(SystemExit, match="broker socket does not exist"):
        smoke.preflight_broker_url(f"unix://{tmp_path / 'missing.sock'}")


def test_docker_smoke_evidence_requires_remote_rederived_fetches(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        """
        {
          "status": "completed",
          "diff_observed": true,
          "files_changed": ["docs/internal/WORKLINK.md"],
          "commands": [
            {"cmd": "git fetch origin main", "exit_code": 0},
            {"cmd": "git fetch origin issue/474-a1", "exit_code": 0},
            {"cmd": "git diff --name-only origin/main...origin/issue/474-a1", "exit_code": 0}
          ]
        }
        """,
        encoding="utf-8",
    )

    data = smoke.validate_smoke_evidence(evidence)

    assert data["status"] == "completed"
