"""The committed docker-sibling example configs (chainlink #539) must stay
loadable by the real parsers — otherwise the runbook ships dead YAML.

Validates docs/examples/worklink/* against DockerBrokerPolicy (broker side) and
WorklinkConfig.load + the compute-backend factory (agent side).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from mimir.worklink.backends.registry import BackendRegistry, WorklinkConfig
from mimir.worklink.compute import DockerSiblingComputeBackend
from mimir.worklink.docker_broker import DockerBrokerPolicy

_EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples" / "worklink"


def test_example_broker_policy_parses_and_is_locked_down() -> None:
    data = yaml.safe_load((_EXAMPLES / "docker-broker-policy.yaml").read_text(encoding="utf-8"))
    policy = DockerBrokerPolicy.from_mapping(data)
    assert policy.allowed_images  # at least one allowed image
    assert policy.network == "none"  # locked down by default
    # default_env keys must be a subset of env_allowlist (DockerBrokerPolicy enforces
    # this in __post_init__; assert the example actually satisfies it).
    assert set(policy.default_env).issubset(set(policy.env_allowlist))


def test_example_worklink_yaml_loads_and_builds_docker_sibling(tmp_path: Path) -> None:
    config = WorklinkConfig.load(_EXAMPLES / "worklink.docker-sibling.yaml")
    # the `docker-sibling` key normalizes to docker_sibling
    assert "docker_sibling" in config.compute_backend_settings
    assert config.defaults.compute_backend == "docker_sibling"

    # the factory turns the stanza into a real backend instance
    registry = BackendRegistry(config)
    backend = registry.select_compute(labels={"react"}, repo="jasoncarreira/mimir")
    assert isinstance(backend, DockerSiblingComputeBackend)
    assert backend.image == "mimir-worklink:latest"
    assert backend.broker_url.startswith("unix://")
