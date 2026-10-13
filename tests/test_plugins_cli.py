"""Plugin inventory uses inert distribution metadata and process selection."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

from mimir import cli


def _candidate(name: str, *, enabled: bool = False, diagnostic: str | None = None):
    return SimpleNamespace(
        name=name, version="1.2.3" if diagnostic is None else None,
        license="MIT" if diagnostic is None else None,
        distribution="provider-" + name, enabled=enabled, diagnostic=diagnostic,
    )


def test_plugins_list_human_and_json(monkeypatch, capsys):
    from mimir.commands import plugins

    selections = []

    def discover(*, enable):
        selections.append(enable)
        return (_candidate("alpha", enabled=enable == "alpha"),
                _candidate("broken", diagnostic="invalid mimir-plugin.toml"))

    monkeypatch.setattr(plugins, "discover_plugins", discover)
    monkeypatch.setenv("MIMIR_PLUGINS", "alpha")
    cli.main(["plugins", "list"])
    human = capsys.readouterr().out
    assert "alpha 1.2.3 (MIT) from provider-alpha — enabled" in human
    assert "broken - (-) from provider-broken — disabled" in human
    assert "diagnostic: invalid mimir-plugin.toml" in human

    cli.main(["plugins", "list", "--json"])
    assert json.loads(capsys.readouterr().out) == [
        {"name": "alpha", "version": "1.2.3", "license": "MIT",
         "distribution": "provider-alpha", "enabled": True, "diagnostic": None},
        {"name": "broken", "version": None, "license": None,
         "distribution": "provider-broken", "enabled": False,
         "diagnostic": "invalid mimir-plugin.toml"},
    ]
    assert selections == ["alpha", "alpha"]


def test_plugins_list_ignores_home_dotenv_and_does_not_load_provider(tmp_path):
    (tmp_path / ".env").write_text("MIMIR_PLUGINS=alpha\n")
    (tmp_path / "mimir-plugin.toml").write_text(
        'name = "alpha"\nversion = "1.2.3"\nlicense = "MIT"\nrequires_mimir = ">=0.9,<1"\n'
    )
    script = """
import importlib.metadata as metadata
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

root = Path(sys.argv[1])
class Entry:
    name = 'alpha'
    value = 'poison_provider:register'
    dist = SimpleNamespace(files=[Path('mimir-plugin.toml')], metadata={'Name': 'provider-alpha'}, locate_file=lambda path: root / path)
    def load(self):
        raise AssertionError('provider loaded')
metadata.entry_points = lambda *, group: [Entry()]
os.environ['MIMIR_HOME'] = str(root)
os.environ.pop('MIMIR_PLUGINS', None)
from mimir.cli import main
main(['plugins', 'list', '--json'])
assert 'poison_provider' not in sys.modules
assert 'mimir.server' not in sys.modules
assert 'MIMIR_PLUGINS' not in os.environ
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"name": "alpha", "version": "1.2.3", "license": "MIT",
         "distribution": "provider-alpha", "enabled": False, "diagnostic": None},
    ]


def test_plugins_requires_list(capsys):
    import pytest

    with pytest.raises(SystemExit) as exc:
        cli.main(["plugins"])
    assert exc.value.code == 1
    assert "list" in capsys.readouterr().out
