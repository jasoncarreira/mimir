"""The dashboard manifest names only real, packaged Cultivate resources."""

from __future__ import annotations

import importlib.resources
import subprocess
import tomllib
from pathlib import Path
from zipfile import ZipFile



ROOT = Path(__file__).resolve().parents[1]
PACKAGE = importlib.resources.files("mimir_cultivate")


def test_frontend_resources_are_shipped_and_match_manifest() -> None:
    manifest = tomllib.loads((ROOT / "mimir-plugin.toml").read_text())
    frontend = manifest["frontend"]
    assert frontend == {
        "api_major": 1,
        "module": "frontend/cultivate.v1.js",
        "css": "frontend/cultivate.v1.css",
    }
    for resource in (frontend["module"], frontend["css"]):
        source = ROOT / "mimir_cultivate" / resource
        assert source.is_file()
        assert PACKAGE.joinpath(resource).read_bytes() == source.read_bytes()


def test_bundle_uses_host_singleton_and_owner_scoped_api() -> None:
    bundle = PACKAGE.joinpath("frontend/cultivate.v1.js").read_text()
    assert "window.mimirPluginHost.react" in bundle
    assert "uiKit.components" in bundle
    assert "authenticatedFetch" in bundle
    assert "/api/v1/plugins/cultivate" in bundle
    assert "cultivate-factory-runs" in bundle
    assert "cultivate-pr-panel" in bundle
    assert 'from"react"' not in bundle
    assert 'from"react/jsx-runtime"' not in bundle
    assert "/api/v1/factory-runs" not in bundle


def test_bundle_is_valid_es_module() -> None:
    result = subprocess.run(
        ["node", "--check", str(ROOT / "mimir_cultivate/frontend/cultivate.v1.js")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_wheel_inventory_contains_manifest_resources(tmp_path: Path) -> None:
    result = subprocess.run(
        ["uv", "build", "--package", "mimir-cultivate", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT.parents[1], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    wheel, = tmp_path.glob("*.whl")
    frontend = tomllib.loads((ROOT / "mimir-plugin.toml").read_text())["frontend"]
    with ZipFile(wheel) as archive:
        for name in (frontend["module"], frontend["css"]):
            resource = f"mimir_cultivate/{name}"
            assert archive.namelist().count(resource) == 1
            assert archive.read(resource) == (ROOT / resource).read_bytes()
