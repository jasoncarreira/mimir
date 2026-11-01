"""Guard published extras against resolving workspace-only names from PyPI."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import pytest

from tests.uv_subprocess import isolated_uv_env

ROOT = Path(__file__).resolve().parents[1]


def test_isolated_uv_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/nonexistent")
    monkeypatch.setenv("UV_CACHE_DIR", "/nonexistent/cache")
    monkeypatch.setenv("UV_PYTHON", "/nonexistent/python")
    env = isolated_uv_env(tmp_path)
    assert env["UV_CACHE_DIR"] == str(tmp_path / "uv-cache")
    assert env["UV_PYTHON"] == sys.executable
    assert env["HOME"] == "/nonexistent"
    assert os.environ["UV_CACHE_DIR"] == "/nonexistent/cache"
    assert os.environ["UV_PYTHON"] == "/nonexistent/python"


def test_published_requirements_exclude_workspace_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the real build even when the worker has no writable home.
    monkeypatch.setenv("HOME", "/nonexistent")
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
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        env=isolated_uv_env(tmp_path),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
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
