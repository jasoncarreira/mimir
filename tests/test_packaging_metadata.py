"""Guard published mimir-agent metadata against workspace-only dependencies."""

from __future__ import annotations

import subprocess
import tomllib
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tests.uv_subprocess import isolated_uv_env


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wheel_metadata(tmp_path_factory: pytest.TempPathFactory) -> EmailMessage:
    scratch = tmp_path_factory.mktemp("mimir-wheel-metadata")
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(scratch)],
        cwd=ROOT,
        env=isolated_uv_env(scratch),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    wheels = list(scratch.glob("mimir_agent-*.whl"))
    assert len(wheels) == 1, wheels
    with ZipFile(wheels[0]) as wheel:
        paths = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        assert len(paths) == 1, paths
        metadata = BytesParser(policy=policy.default).parsebytes(wheel.read(paths[0]))
    assert canonicalize_name(metadata["Name"]) == "mimir-agent"
    assert metadata.get_all("Requires-Dist"), "wheel must declare runtime dependencies"
    return metadata


def test_published_extras_exclude_workspace_members(wheel_metadata: EmailMessage) -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = config["tool"]["uv"]["workspace"]["members"]
    workspace_names = {
        canonicalize_name(
            tomllib.loads((ROOT / member / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
        )
        for member in members
    }
    assert workspace_names, "workspace member inventory must not be empty"
    assert wheel_metadata.get_all("Provides-Extra"), "wheel must expose its extras"
    published = {
        canonicalize_name(Requirement(value).name)
        for value in wheel_metadata.get_all("Requires-Dist", [])
    }
    assert not (published & workspace_names), (
        f"workspace-only distributions leaked into published Requires-Dist: {published & workspace_names}"
    )


def test_published_metadata_never_requires_cultivate(wheel_metadata: EmailMessage) -> None:
    assert all(
        canonicalize_name(Requirement(value).name) != "mimir-cultivate"
        for value in wheel_metadata.get_all("Requires-Dist", [])
    )


def test_local_dev_group_installs_bench_harness() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = config["dependency-groups"]["dev"]
    assert any(Requirement(value).name == "saga" for value in dev)
