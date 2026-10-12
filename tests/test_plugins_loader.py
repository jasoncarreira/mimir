"""Operator selection and provider registration are separate from static discovery."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from mimir.plugins.discovery import PluginDiscoveryError
from mimir.plugins.manager import load_plugins


def test_selected_provider_registers_once_and_freezes(monkeypatch):
    calls = []

    def register(registrar):
        calls.append(registrar.plugin)
        registrar.register_capability("alpha-tool")

    candidate = SimpleNamespace(name="alpha", distribution="provider-alpha",
                                entry_point=SimpleNamespace(load=lambda: SimpleNamespace(register=register)))
    monkeypatch.setattr("mimir.plugins.manager.enabled_plugins", lambda *, enable: (candidate,))
    manager = load_plugins(enable="alpha")
    assert calls == ["alpha"]
    assert manager.policy.frozen
    assert manager.policy.capabilities["alpha-tool"] == "alpha"
    with pytest.raises(ValueError, match="frozen"):
        manager.policy.register_capability("late")


def test_named_load_and_registration_errors(monkeypatch):
    def broken():
        raise RuntimeError("import broke")

    candidate = SimpleNamespace(name="alpha", distribution="provider-alpha",
                                entry_point=SimpleNamespace(load=broken))
    monkeypatch.setattr("mimir.plugins.manager.enabled_plugins", lambda *, enable: (candidate,))
    with pytest.raises(PluginDiscoveryError, match="plugin 'alpha' from provider-alpha: registration failed: import broke"):
        load_plugins(enable="alpha")


def test_disabled_provider_is_not_imported_by_loader_in_child_process(tmp_path):
    (tmp_path / "poison_provider.py").write_text("raise RuntimeError('disabled provider imported')\n")
    (tmp_path / "mimir-plugin.toml").write_text(
        'name = "alpha"\nversion = "1.2.3"\nlicense = "MIT"\nrequires_mimir = ">=0.9,<1"\n'
    )
    script = """
import importlib.metadata as metadata
import sys
from pathlib import Path
from types import SimpleNamespace
root = Path(sys.argv[1])
class Entry:
    name = 'alpha'
    value = 'poison_provider:register'
    dist = SimpleNamespace(files=[Path('mimir-plugin.toml')], metadata={'Name': 'provider-alpha'}, locate_file=lambda path: root / path)
    def load(self):
        __import__('poison_provider')
metadata.entry_points = lambda *, group: [Entry()]
from mimir.plugins.manager import load_plugins
manager = load_plugins(enable='')
assert manager.plugins == ()
assert 'poison_provider' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
