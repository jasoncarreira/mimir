"""All external GitHub Actions must use immutable commit or image digests."""

from __future__ import annotations

from pathlib import Path
import re

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
ACTION = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*@[0-9a-f]{40}")
DOCKER = re.compile(r"docker://[^\s@]+@sha256:[0-9a-f]{64}")


def _check_uses(value: str, location: str) -> None:
    if value.startswith("./"):
        return
    if value.startswith("docker://"):
        assert DOCKER.fullmatch(value), f"{location}: unpinned uses: {value}"
        return
    assert ACTION.fullmatch(value), f"{location}: unpinned uses: {value}"


def _check_workflow(path: Path, workflow: dict) -> None:
    for job_name, job in workflow["jobs"].items():
        if "uses" in job:
            _check_uses(job["uses"], f"{path} job {job_name}")
        for index, step in enumerate(job.get("steps", []), start=1):
            if "uses" in step:
                _check_uses(
                    step["uses"], f"{path} job {job_name} step {step.get('name', index)}"
                )


def test_workflows_pin_external_actions() -> None:
    paths = sorted(WORKFLOWS.glob("*.yml"))
    assert paths, f"No workflows found in {WORKFLOWS}"
    for path in paths:
        _check_workflow(path, yaml.safe_load(path.read_text()))


@pytest.mark.parametrize(
    "value, valid",
    [
        ("actions/checkout@" + "a" * 40, True),
        ("owner/repo/subdir@" + "b" * 40, True),
        ("./local-action", True),
        ("docker://ghcr.io/owner/image@sha256:" + "c" * 64, True),
        ("actions/checkout@v5", False),
        ("pypa/gh-action-pypi-publish@release/v1", False),
        ("actions/checkout@abcdef0", False),
        ("actions/checkout@" + "A" * 40, False),
        ("actions/checkout@" + "a" * 40 + "extra", False),
        ("docker://ghcr.io/owner/image:latest", False),
        ("docker://ghcr.io/owner/image@sha256:abcdef0", False),
    ],
)
def test_action_pin_formats(value: str, valid: bool) -> None:
    if valid:
        _check_uses(value, "example.yml job sample step 1")
    else:
        with pytest.raises(AssertionError, match="example.yml job sample step 1: unpinned uses"):
            _check_uses(value, "example.yml job sample step 1")


@pytest.mark.parametrize("level", ["job", "step"])
def test_workflow_walk_checks_both_uses_levels(level: str) -> None:
    job = {"runs-on": "ubuntu-latest", "steps": [{"name": "Build", "run": "true"}]}
    if level == "job":
        job["uses"] = "owner/workflow@main"
    else:
        job["steps"][0]["uses"] = "actions/checkout@v5"
    with pytest.raises(AssertionError, match=f"example.yml job sample.*unpinned uses"):
        _check_workflow(Path("example.yml"), {"jobs": {"sample": job}})
