"""Static provider discovery and fail-closed enable-list validation."""

from __future__ import annotations

import subprocess
import sys
import json
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.plugins import PluginDiscoveryError, PluginManifestError, discover_plugins, enabled_plugins
from mimir.plugins.manifest import _SKIN_TOKENS, parse_manifest


def _toml(**fields: str) -> bytes:
    values = {
        "name": "alpha",
        "version": "1.2.3",
        "license": "MIT",
        "requires_mimir": ">=0.9,<1",
        **fields,
    }
    return "".join(f'{key} = "{value}"\n' for key, value in values.items()).encode()


class _Distribution:
    def __init__(self, root: Path, files: list[str], name: str = "provider-alpha"):
        self.root = root
        self.files = [Path(file) for file in files]
        self.metadata = {"Name": name}

    def locate_file(self, path: Path) -> Path:
        return self.root / path


def _entry(tmp_path: Path, *, name: str = "alpha", source: str = "provider-alpha", files: list[str] | None = None):
    dist = _Distribution(tmp_path, files if files is not None else ["pkg/mimir-plugin.toml"], source)
    return SimpleNamespace(name=name, value="poison_provider:register", dist=dist)


def test_manifest_validates_required_fields_and_reserved_keys():
    manifest = parse_manifest(_toml() + b"[dockerfile]\nsystem_packages = ['git']\n", entry_name="alpha")
    assert (manifest.name, manifest.version, manifest.license) == ("alpha", "1.2.3", "MIT")
    assert manifest.dockerfile["system_packages"] == ["git"]
    for data, match in [
        (_toml(name="other"), "does not match"),
        (_toml(version="not a version"), "invalid version"),
        (_toml(license="not an SPDX license"), "SPDX"),
        (_toml(requires_mimir=">=999"), "does not include"),
        (_toml() + b"unsupported = true\n", "unknown manifest keys"),
        (_toml() + b"[dockerfile]\nbase = 'ignored'\n", "dockerfile: invalid fields"),
        (b"name = 'alpha'\n", "missing manifest keys"),
        (b"invalid = [", "invalid mimir-plugin.toml"),
    ]:
        with pytest.raises(PluginManifestError, match=match):
            parse_manifest(data, entry_name="alpha")
    with pytest.raises(PluginManifestError, match="entry-point name"):
        parse_manifest(_toml(), entry_name="Bad_Name")


def test_reserved_resources_must_belong_to_distribution(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    asset = root / "main.js"
    asset.write_text("export default true")
    data = _toml() + b"[frontend]\napi_major = 1\nmodule = 'main.js'\n"
    manifest = parse_manifest(data, entry_name="alpha", resource_root=root,
                              distribution_files=frozenset({asset}))
    assert manifest.frontend["module"] == "main.js"
    with pytest.raises(PluginManifestError, match="missing or escaping"):
        parse_manifest(data, entry_name="alpha", resource_root=root, distribution_files=frozenset())
    with pytest.raises(PluginManifestError, match="package-relative"):
        parse_manifest(data.replace(b"main.js", b"../main.js"), entry_name="alpha")


def test_config_schema_rejects_foreign_keys():
    data = _toml() + b'[config.MIMIR_PLUGIN_ALPHA_TOKEN]\ntype = "string"\nsecret = true\n'
    assert parse_manifest(data, entry_name="alpha").config["MIMIR_PLUGIN_ALPHA_TOKEN"]["secret"]
    with pytest.raises(PluginManifestError, match="must be under"):
        parse_manifest(data.replace(b"MIMIR_PLUGIN_ALPHA_TOKEN", b"MIMIR_PLUGIN_BETA_TOKEN"), entry_name="alpha")


def test_skin_requires_complete_tokens_and_local_assets(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    asset = root / "avatar.png"
    asset.write_bytes(b"image")
    skin = {
        "id": "alpha-skin", "label": "Alpha", "version": "1", "default": True,
        "tokens": {name: "blue" for name in _SKIN_TOKENS},
        "chrome": {"layout": "sidebar", "density": "compact", "accentPlacement": "side-rule"},
        "panel": {"surface": "flat", "borderStyle": "solid", "hoverBehavior": "none"},
        "characterRenderer": {"kind": "static-image", "componentSlot": "agent-character",
                              "assets": [{"id": "avatar", "href": "avatar.png"}]},
    }
    path = root / "skin.json"
    inventory = frozenset({asset, path})
    def parse():
        path.write_text(json.dumps(skin))
        return parse_manifest(_toml() + b'themes = ["skin.json"]\n', entry_name="alpha",
                              resource_root=root, distribution_files=inventory)

    assert parse().skin_defaults == ("alpha-skin",)
    skin["tokens"].pop("--mimir-color-trigger-poller")
    with pytest.raises(PluginManifestError, match="complete local"):
        parse()
    skin["tokens"]["--mimir-color-trigger-poller"] = "blue"
    skin["characterRenderer"]["assets"][0]["href"] = "https://example.org/asset.png"
    with pytest.raises(PluginManifestError, match="package-relative"):
        parse()


def test_discovery_reads_only_the_owning_distribution(tmp_path, monkeypatch):
    provider = tmp_path / "pkg"
    provider.mkdir()
    (provider / "mimir-plugin.toml").write_bytes(_toml())
    entry = _entry(tmp_path)
    monkeypatch.setattr("mimir.plugins.discovery.metadata.entry_points", lambda *, group: [entry])
    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    (candidate,) = discover_plugins()
    assert (candidate.name, candidate.version, candidate.license, candidate.distribution, candidate.enabled) == (
        "alpha", "1.2.3", "MIT", "provider-alpha", False,
    )
    assert candidate.diagnostic is None
    assert enabled_plugins() == ()
    monkeypatch.setenv("MIMIR_PLUGINS", "alpha")
    assert enabled_plugins() == (discover_plugins()[0],)
    monkeypatch.setenv("MIMIR_PLUGINS", "all")
    assert len(enabled_plugins()) == 1


def test_locally_installed_distribution_manifest_is_read_without_provider_import(tmp_path, monkeypatch):
    """Use actual installed-distribution metadata, not a synthetic entry point."""
    info = tmp_path / "provider_alpha-1.2.3.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Name: provider-alpha\nVersion: 1.2.3\n")
    (info / "entry_points.txt").write_text(
        "[mimir.plugins]\nalpha = poison_provider:register\n"
    )
    manifest = tmp_path / "pkg" / "mimir-plugin.toml"
    manifest.parent.mkdir()
    manifest.write_bytes(_toml())
    (info / "RECORD").write_text(
        "provider_alpha-1.2.3.dist-info/METADATA,,\n"
        "provider_alpha-1.2.3.dist-info/entry_points.txt,,\n"
        "pkg/mimir-plugin.toml,,\n"
    )
    (tmp_path / "poison_provider.py").write_text("raise AssertionError('imported provider')\n")
    (distribution,) = metadata.distributions(path=[str(tmp_path)])
    monkeypatch.setattr(
        "mimir.plugins.discovery.metadata.entry_points",
        lambda *, group: distribution.entry_points if group == "mimir.plugins" else (),
    )
    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    (candidate,) = discover_plugins()
    assert (candidate.name, candidate.version, candidate.license, candidate.distribution,
            candidate.enabled, candidate.diagnostic) == (
        "alpha", "1.2.3", "MIT", "provider-alpha", False, None,
    )
    assert candidate.entry_point.dist is distribution
    assert enabled_plugins(enable="") == ()


@pytest.mark.parametrize("files,diagnostic", [
    ([], "exactly one"),
    (["one/mimir-plugin.toml", "two/mimir-plugin.toml"], "exactly one"),
    (["pkg/mimir-plugin.toml"], "invalid mimir-plugin.toml"),
])
def test_invalid_disabled_distribution_is_diagnostic(tmp_path, monkeypatch, files, diagnostic):
    if files:
        for filename in files:
            path = tmp_path / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("invalid = [")
    monkeypatch.setattr("mimir.plugins.discovery.metadata.entry_points", lambda *, group: [_entry(tmp_path, files=files)])
    (candidate,) = discover_plugins(enable="")
    assert not candidate.enabled and diagnostic in candidate.diagnostic
    with pytest.raises(PluginDiscoveryError, match="plugin 'alpha' from provider-alpha"):
        enabled_plugins(enable="alpha")


def test_duplicate_and_missing_enabled_names(tmp_path, monkeypatch):
    entries = [_entry(tmp_path, source=name, files=[]) for name in ("first", "second")]
    monkeypatch.setattr("mimir.plugins.discovery.metadata.entry_points", lambda *, group: reversed(entries))
    candidates = discover_plugins(enable="")
    assert [item.distribution for item in candidates] == ["first", "second"]
    assert {item.diagnostic for item in candidates} == {"duplicate entry-point name"}
    with pytest.raises(PluginDiscoveryError, match="duplicate entry-point name"):
        enabled_plugins(enable="alpha")
    with pytest.raises(PluginDiscoveryError, match=r"missing plugin\(s\): absent"):
        enabled_plugins(enable="absent")


def test_disabled_provider_never_imported_in_fresh_interpreter(tmp_path):
    (tmp_path / "poison_provider.py").write_text("raise RuntimeError('provider imported')\n")
    (tmp_path / "mimir-plugin.toml").write_bytes(_toml())
    script = """
import importlib.metadata as metadata
import sys
from pathlib import Path
from types import SimpleNamespace
from mimir.plugins import discover_plugins
root = Path(sys.argv[1])
dist = SimpleNamespace(files=[Path('mimir-plugin.toml')], metadata={'Name': 'poison'}, locate_file=lambda p: root / p)
ep = SimpleNamespace(name='alpha', value='poison_provider:register', dist=dist)
metadata.entry_points = lambda *, group: [ep]
assert discover_plugins(enable='')[0].name == 'alpha'
assert 'poison_provider' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
