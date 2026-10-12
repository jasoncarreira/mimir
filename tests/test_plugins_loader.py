"""Operator selection and provider registration are separate from static discovery."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from mimir.plugins.discovery import PluginDiscoveryError
from mimir.plugins.manager import load_plugins
from mimir.plugins.core import ServicePrincipal
from mimir.tool_descriptors import ResultOriginKind


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


@pytest.mark.parametrize("registration,cause", [
    ("profile", "unknown capability missing-capability"),
    ("action", "repo_pr scope requires typed action"),
])
def test_selected_plugin_freeze_failure_names_owner_and_cause(monkeypatch, registration, cause):
    def register(registrar):
        if registration == "profile":
            registrar.register_authority_profile("alpha-profile", capabilities={"missing-capability"})
        else:
            registrar.register_tool(
                name="alpha-repo", factory=lambda: object(), flow="source",
                tier="scope-contained", operation_class="resource_scoped",
                resource_scope="repo_pr", result_domain=None,
                result_origin=ResultOriginKind.NONE, sink_category=None,
                sink_destination=None, sink_target_extractor=None,
                sink_payload_extractor=None, writes_code_or_state=False,
                profiles=(), read_backend=False,
            )

    candidate = SimpleNamespace(
        name="alpha", distribution="provider-alpha",
        entry_point=SimpleNamespace(load=lambda: SimpleNamespace(register=register)),
    )
    monkeypatch.setattr("mimir.plugins.manager.enabled_plugins", lambda *, enable: (candidate,))
    with pytest.raises(PluginDiscoveryError) as error:
        load_plugins(enable="alpha")
    assert "enabled plugin(s) 'alpha':" in str(error.value)
    assert cause in str(error.value)


def test_principal_freeze_failure_names_only_its_provider(monkeypatch):
    def invalid_principal(registrar):
        registrar.register_service_principal(ServicePrincipal(
            canonical="alpha-service", trigger="alpha-trigger",
            capabilities=("read_file",), readable_domains=("filesystem",),
            sink_destinations=("filesystem",), creation_path="provider-alpha",
            authority_profile="missing-profile",
        ))

    def candidate(name, register):
        return SimpleNamespace(
            name=name, distribution="provider-" + name,
            entry_point=SimpleNamespace(load=lambda: SimpleNamespace(register=register)),
        )

    selected = (candidate("alpha", invalid_principal), candidate("beta", lambda registrar: None))
    monkeypatch.setattr("mimir.plugins.manager.enabled_plugins", lambda *, enable: selected)
    with pytest.raises(PluginDiscoveryError) as error:
        load_plugins(enable="alpha,beta")
    assert "enabled plugin(s) 'alpha':" in str(error.value)
    assert "beta" not in str(error.value)
    assert "alpha-trigger: unknown authority profile missing-profile" in str(error.value)


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
