"""Static provider discovery and fail-closed enable-list validation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.plugins import PluginDiscoveryError, PluginManifestError, discover_plugins, enabled_plugins
from mimir.plugins.manifest import parse_manifest


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
    manifest = parse_manifest(_toml() + b"[dockerfile]\nbase = 'ignored'\n", entry_name="alpha")
    assert (manifest.name, manifest.version, manifest.license) == ("alpha", "1.2.3", "MIT")
    for data, match in [
        (_toml(name="other"), "does not match"),
        (_toml(version="not a version"), "invalid version"),
        (_toml(license="not an SPDX license"), "SPDX"),
        (_toml(requires_mimir=">=999"), "does not include"),
        (_toml() + b"unsupported = true\n", "unknown manifest keys"),
        (b"name = 'alpha'\n", "missing manifest keys"),
        (b"invalid = [", "invalid mimir-plugin.toml"),
    ]:
        with pytest.raises(PluginManifestError, match=match):
            parse_manifest(data, entry_name="alpha")
    with pytest.raises(PluginManifestError, match="entry-point name"):
        parse_manifest(_toml(), entry_name="Bad_Name")


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
