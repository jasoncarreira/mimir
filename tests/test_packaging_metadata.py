"""Guard published extras against resolving workspace-only names from PyPI."""

from __future__ import annotations

import subprocess
import tomllib
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]


def test_published_requirements_exclude_workspace_members(tmp_path: Path) -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = config["tool"]["uv"]["workspace"]["members"]
    workspace_names = {
        canonicalize_name(
            tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))[
                "project"
            ]["name"]
        )
        for pattern in members
        for member in ROOT.glob(pattern)
        if (member / "pyproject.toml").is_file()
    }
    assert workspace_names, "expected at least one workspace member to guard"

    # Build through the configured backend: uv's workspace source mapping must
    # not mask a Requires-Dist that pip would instead resolve from PyPI.
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        metadata_path = next(
            path for path in wheel.namelist() if path.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser(policy=default).parsebytes(wheel.read(metadata_path))

    published = {
        canonicalize_name(Requirement(requirement).name)
        for requirement in metadata.get_all("Requires-Dist", [])
    }
    assert not (published & workspace_names), (
        "wheel Requires-Dist exposes workspace-only distributions to PyPI: "
        f"{sorted(published & workspace_names)}; put these in [dependency-groups]"
    )
