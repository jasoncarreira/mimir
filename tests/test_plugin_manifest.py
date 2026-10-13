"""Static provider discovery and fail-closed enable-list validation."""

from __future__ import annotations

import json
import subprocess
import sys
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


def _skin() -> dict:
    return {
        "id": "alpha-skin", "label": "Alpha", "version": "1", "default": True,
        "tokens": {name: "blue" for name in _SKIN_TOKENS},
        "chrome": {"layout": "sidebar", "density": "compact", "accentPlacement": "side-rule"},
        "panel": {"surface": "flat", "borderStyle": "solid", "hoverBehavior": "none"},
        "characterRenderer": {
            "kind": "static-image", "componentSlot": "agent-character", "variant": "default",
            "assets": [{"id": "avatar", "type": "image", "href": "avatar.png"}],
            "stateMap": {"idle": "avatar"}, "fallbackState": "idle",
            "capabilities": {"supportsExpressions": False, "supportsMotion": False},
        },
    }


def _skin_candidate(tmp_path, monkeypatch, skins: dict[str, dict], *, extra_files: tuple[str, ...] = ()):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "mimir-plugin.toml").write_bytes(
        _toml() + ("themes = [" + ", ".join(json.dumps(name) for name in skins) + "]\n").encode()
    )
    for name, skin in skins.items():
        (root / name).write_text(json.dumps(skin))
    for name in extra_files:
        (root / name).write_bytes(b"asset")
    files = ["pkg/mimir-plugin.toml", *(f"pkg/{name}" for name in skins),
             *(f"pkg/{name}" for name in extra_files)]
    entry = _entry(tmp_path, files=files)
    monkeypatch.setattr("mimir.plugins.discovery.metadata.entry_points", lambda *, group: [entry])
    return discover_plugins(enable="")[0]


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


@pytest.mark.parametrize("resource,kind", [
    ("main.js", "absent"), ("main.js", "directory"),
    ("skills", "absent"), ("skills", "file"),
])
def test_listed_package_resources_require_physical_correct_kind(tmp_path, resource, kind):
    root = tmp_path / "pkg"
    root.mkdir()
    target = root / resource
    if kind == "directory":
        target.mkdir()
        (target / "skill.md").write_text("skill")
    elif kind == "file":
        target.write_text("asset")
    data = (_toml() + b'[defaults]\nversion = "1"\nskills = ["skills"]\n'
            if resource == "skills" else _toml() + b'[frontend]\napi_major = 1\nmodule = "main.js"\n')
    files = frozenset({target / "skill.md"} if kind == "directory" else {target})
    with pytest.raises(PluginManifestError, match="missing or escaping"):
        parse_manifest(data, entry_name="alpha", resource_root=root, distribution_files=files)


def test_existing_but_unlisted_resource_is_rejected(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "main.js").write_text("asset")
    with pytest.raises(PluginManifestError, match="missing or escaping"):
        parse_manifest(_toml() + b'[frontend]\napi_major = 1\nmodule = "main.js"\n',
                       entry_name="alpha", resource_root=root, distribution_files=frozenset())


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
    skin = _skin()
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


@pytest.mark.parametrize("field", ["variant", "assets", "stateMap", "fallbackState", "capabilities"])
def test_skin_rejects_incomplete_renderer(tmp_path, field):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "avatar.png").write_bytes(b"image")
    path = root / "skin.json"
    skin = _skin()
    skin["characterRenderer"].pop(field)
    path.write_text(json.dumps(skin))
    with pytest.raises(PluginManifestError, match="incomplete characterRenderer"):
        parse_manifest(_toml() + b'themes = ["skin.json"]\n', entry_name="alpha",
                       resource_root=root, distribution_files=frozenset({root / "avatar.png", path}))


@pytest.mark.parametrize("location,key", [
    ("chrome", "layout"), ("panel", "surface"),
    ("characterRenderer", "kind"), ("characterRenderer", "fallbackState"),
    ("characterRenderer", "stateMap"), ("skin", "assets"),
])
def test_malformed_skin_nested_values_become_discovery_diagnostics(tmp_path, monkeypatch, location, key):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "avatar.png").write_bytes(b"image")
    (root / "mimir-plugin.toml").write_bytes(_toml() + b'themes = ["skin.json"]\n')
    skin = _skin()
    (skin if location == "skin" else skin[location])[key] = None if location == "skin" else ["invalid"]
    (root / "skin.json").write_text(json.dumps(skin))
    entry = _entry(tmp_path, files=["pkg/mimir-plugin.toml", "pkg/skin.json", "pkg/avatar.png"])
    monkeypatch.setattr("mimir.plugins.discovery.metadata.entry_points", lambda *, group: [entry])
    (candidate,) = discover_plugins(enable="")
    assert not candidate.enabled and candidate.diagnostic.startswith("themes:")
    with pytest.raises(PluginDiscoveryError, match="plugin 'alpha' from provider-alpha: themes:"):
        enabled_plugins(enable="alpha")


def test_character_asset_requires_explicit_href(tmp_path, monkeypatch):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "mimir-plugin.toml").write_bytes(_toml() + b'themes = ["skin.json"]\n')
    skin = _skin()
    skin["characterRenderer"]["assets"][0].pop("href")
    (root / "skin.json").write_text(json.dumps(skin))
    entry = _entry(tmp_path, files=["pkg/mimir-plugin.toml", "pkg/skin.json"])
    monkeypatch.setattr("mimir.plugins.discovery.metadata.entry_points", lambda *, group: [entry])
    (candidate,) = discover_plugins(enable="")
    assert candidate.diagnostic is not None and "invalid character asset" in candidate.diagnostic
    with pytest.raises(PluginDiscoveryError, match="invalid character asset"):
        enabled_plugins(enable="alpha")


@pytest.mark.parametrize("field,value", [
    ("weight", True), ("weight", [400]), ("weight", None),
    ("style", ["italic"]), ("style", "oblique"), ("display", {"name": "swap"}),
    ("display", "hidden"), ("unicodeRange", ["U+0-FF"]),
])
def test_invalid_font_descriptor_is_diagnostic_and_selected_rejects(tmp_path, monkeypatch, field, value):
    skin = _skin()
    skin["fonts"] = [{"family": "Local", "src": [{"url": "local.woff2", "format": "woff2"}], field: value}]
    candidate = _skin_candidate(tmp_path, monkeypatch, {"skin.json": skin},
                                extra_files=("avatar.png", "local.woff2"))
    assert not candidate.enabled and candidate.diagnostic is not None
    assert "invalid font descriptor" in candidate.diagnostic
    with pytest.raises(PluginDiscoveryError, match="invalid font descriptor"):
        enabled_plugins(enable="alpha")


@pytest.mark.parametrize("weight", [400, 400.5, "400 700"])
def test_valid_local_font_descriptors(tmp_path, monkeypatch, weight):
    skin = _skin()
    skin["fonts"] = [{
        "family": "Local", "weight": weight, "style": "italic", "display": "swap",
        "unicodeRange": "U+0-FF", "src": [{"url": "local.woff2", "format": "woff2"}],
    }]
    candidate = _skin_candidate(tmp_path, monkeypatch, {"skin.json": skin},
                                extra_files=("avatar.png", "local.woff2"))
    assert candidate.diagnostic is None
    assert enabled_plugins(enable="alpha")[0].manifest.skin_defaults == ("alpha-skin",)


@pytest.mark.parametrize("collision", ["character-asset", "skin-id"])
def test_duplicate_skin_or_character_asset_ids_fail_discovery(tmp_path, monkeypatch, collision):
    skin = _skin()
    if collision == "character-asset":
        skin["characterRenderer"]["assets"].append(
            {"id": "avatar", "type": "image", "href": "avatar.png"}
        )
        skins = {"skin.json": skin}
    else:
        skins = {"skin.json": skin, "other.json": _skin()}
    candidate = _skin_candidate(tmp_path, monkeypatch, skins, extra_files=("avatar.png",))
    assert not candidate.enabled and candidate.diagnostic is not None
    expected = "duplicate character asset ID" if collision == "character-asset" else "duplicate skin ID"
    assert expected in candidate.diagnostic
    with pytest.raises(PluginDiscoveryError, match="duplicate character asset ID|duplicate skin ID"):
        enabled_plugins(enable="alpha")


def test_duplicate_listed_theme_resource_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "mimir-plugin.toml").write_bytes(_toml() + b'themes = ["skin.json", "skin.json"]\n')
    entry = _entry(tmp_path)
    monkeypatch.setattr("mimir.plugins.discovery.metadata.entry_points", lambda *, group: [entry])
    (candidate,) = discover_plugins(enable="")
    assert candidate.diagnostic == "themes: duplicate package resource"
    with pytest.raises(PluginDiscoveryError, match="themes: duplicate package resource"):
        enabled_plugins(enable="alpha")


@pytest.mark.parametrize("value", ['["string"]', '{kind = "string"}'])
def test_malformed_config_type_is_diagnostic_when_disabled_and_rejected_when_selected(tmp_path, monkeypatch, value):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "mimir-plugin.toml").write_bytes(
        _toml() + f'[config.MIMIR_PLUGIN_ALPHA_TOKEN]\ntype = {value}\n'.encode()
    )
    entry = _entry(tmp_path)
    monkeypatch.setattr("mimir.plugins.discovery.metadata.entry_points", lambda *, group: [entry])
    (candidate,) = discover_plugins(enable="")
    assert not candidate.enabled and candidate.diagnostic == "config: invalid schema for MIMIR_PLUGIN_ALPHA_TOKEN"
    with pytest.raises(PluginDiscoveryError, match="config: invalid schema for MIMIR_PLUGIN_ALPHA_TOKEN"):
        enabled_plugins(enable="alpha")


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
