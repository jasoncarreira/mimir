"""Plugin inventory uses inert distribution metadata and process selection."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mimir import cli
from mimir.plugins.registry import CliCommand, PolicyRegistrationError


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


def test_disabled_provider_not_imported_for_core_command():
    script = """
import importlib.metadata as metadata
import os
import sys
from types import SimpleNamespace

class Entry:
    name = 'disabled'
    value = 'poison_provider:register'
    dist = SimpleNamespace(files=None, metadata={'Name': 'provider-disabled'})
    def load(self):
        raise AssertionError('disabled provider loaded')

metadata.entry_points = lambda *, group: [Entry()]
os.environ.pop('MIMIR_PLUGINS', None)
from mimir.cli import main
try:
    main(['verify-cred', '--help'])
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError('expected argparse help exit')
assert 'poison_provider' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_plugins_requires_list(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["plugins"])
    assert exc.value.code == 1
    assert "list" in capsys.readouterr().out


def test_selected_command_receives_parsed_arguments(monkeypatch, capsys):
    from mimir.plugins import manager

    def configure(sub):
        parser = sub.add_parser("example")
        parser.add_argument("--value", required=True)

    command = CliCommand("example", "example", configure,
                         lambda args: print(args.value))
    monkeypatch.setenv("MIMIR_PLUGINS", "example")
    monkeypatch.setattr(manager, "load_plugins", lambda *, enable: SimpleNamespace(
        policy=SimpleNamespace(cli_commands={"example": command})))
    cli.main(["example", "--value", "parsed"])
    assert capsys.readouterr().out == "parsed\n"


@pytest.mark.parametrize("names", [("run",), ("example", "example")])
def test_command_name_collision_is_fatal(monkeypatch, names):
    from mimir.plugins import manager

    def configure(sub):
        sub.add_parser("example")

    commands = {str(index): CliCommand(f"plugin-{index}", name, configure, lambda args: None)
                for index, name in enumerate(names)}
    monkeypatch.setenv("MIMIR_PLUGINS", "example")
    monkeypatch.setattr(manager, "load_plugins", lambda *, enable: SimpleNamespace(
        policy=SimpleNamespace(cli_commands=commands)))
    with pytest.raises(PolicyRegistrationError, match="conflicts"):
        cli.main(["example"])


def test_dockerfile_stage_and_selection(monkeypatch, capsys):
    from mimir import plugin_dockerfile

    seen = []
    def select_plugins(**kwargs):
        seen.append(kwargs)
        return ("alpha", "zeta") if kwargs["all_installed"] else ("alpha",)

    monkeypatch.setattr(plugin_dockerfile, "select_plugins", select_plugins)
    monkeypatch.setattr(plugin_dockerfile, "render_stage",
                        lambda candidates, stage: f"{stage}: {','.join(candidates)}")
    monkeypatch.setenv("MIMIR_PLUGINS", "alpha")
    cli.main(["plugins", "dockerfile", "--stage", "base"])
    assert capsys.readouterr().out == "base: alpha\n"
    cli.main(["plugins", "dockerfile", "--all-installed"])
    assert capsys.readouterr().out == "\n\n".join(
        f"{stage}: alpha,zeta" for stage in ("base", "build", "runtime", "services")) + "\n"
    assert seen == [dict(all_installed=False, enable="alpha"),
                    dict(all_installed=True, enable="alpha")]


def test_dockerfile_uses_inert_sorted_package_metadata(monkeypatch, capsys):
    from mimir import plugin_dockerfile

    candidates = tuple(SimpleNamespace(
        name=name, enabled=enabled, diagnostic=None,
        manifest=SimpleNamespace(dockerfile={"system_packages": [package]}))
        for name, enabled, package in (("alpha", False, "git"), ("zeta", True, "curl")))
    monkeypatch.setattr(plugin_dockerfile, "discover_plugins", lambda *, enable: candidates)
    monkeypatch.setenv("MIMIR_PLUGINS", "zeta")
    cli.main(["plugins", "dockerfile", "--stage", "base"])
    assert capsys.readouterr().out == (
        "# --- plugin zeta ---\nRUN apt-get update && apt-get install -y "
        "--no-install-recommends curl && rm -rf /var/lib/apt/lists/*\n"
    )
    cli.main(["plugins", "dockerfile", "--stage", "base", "--all-installed"])
    result = capsys.readouterr().out
    assert result.index("# --- plugin alpha ---") < result.index("# --- plugin zeta ---")
