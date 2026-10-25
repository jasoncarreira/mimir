"""Plugin inventory uses inert distribution metadata and process selection."""

from __future__ import annotations

import json
import os
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


def test_unselected_non_operator_command_explains_selection(monkeypatch, capsys):
    from mimir.plugins import discovery

    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    monkeypatch.setattr(discovery, "discover_plugins", lambda *, enable: (
        SimpleNamespace(name="alpha", diagnostic=None,
                        manifest=SimpleNamespace(cli_commands={"alpha-work": False})),
    ))
    with pytest.raises(SystemExit) as exc:
        cli.main(["alpha-work", "--help"])
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "plugin 'alpha'" in error
    assert "MIMIR_PLUGINS=alpha" in error
    assert "invalid choice" not in error


def test_help_points_to_installed_plugin_inventory(monkeypatch, capsys):
    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "mimir plugins list" in capsys.readouterr().out


@pytest.mark.parametrize("owner,operator", [("other", True), ("cultivate", False)])
def test_operator_manifest_must_match_live_registration(monkeypatch, owner, operator):
    from mimir.plugins import discovery, manager

    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    monkeypatch.setattr(discovery, "discover_plugins", lambda *, enable: (
        SimpleNamespace(name="cultivate", diagnostic=None,
                        manifest=SimpleNamespace(cli_commands={"cultivate": True})),
    ))
    command = CliCommand(owner, "cultivate", lambda sub: sub.add_parser("cultivate"),
                         lambda args: None, operator)
    monkeypatch.setattr(manager, "load_plugins", lambda *, enable: SimpleNamespace(
        policy=SimpleNamespace(cli_commands={"cultivate": command})))
    with pytest.raises(PolicyRegistrationError, match="does not match its manifest"):
        cli.main(["cultivate", "--help"])


def test_operator_command_claim_collision_fails_before_provider_load(monkeypatch):
    from mimir.plugins import discovery, manager

    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    monkeypatch.setattr(discovery, "discover_plugins", lambda *, enable: tuple(
        SimpleNamespace(name=name, diagnostic=None,
                        manifest=SimpleNamespace(cli_commands={"setup-agent": True}))
        for name in ("alpha", "beta")
    ))
    monkeypatch.setattr(manager, "load_plugins", lambda *, enable: (
        pytest.fail("ambiguous command must not load a provider")
    ))
    with pytest.raises(SystemExit) as exc:
        cli.main(["setup-agent"])
    assert exc.value.code == 2


def test_legacy_unselected_command_explains_selection(monkeypatch, capsys):
    from mimir.plugins import discovery

    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    monkeypatch.setattr(discovery, "discover_plugins", lambda *, enable: (
        SimpleNamespace(name="legacy", enabled=False, diagnostic=None,
                        manifest=SimpleNamespace(cli_commands=None)),
    ))
    with pytest.raises(SystemExit) as exc:
        cli.main(["legacy-hello", "--help"])
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "MIMIR_PLUGINS=legacy" in error
    assert "invalid choice" not in error


@pytest.mark.parametrize("declared", [{}, {"alpha-work": False}])
def test_declared_cli_unknown_command_does_not_suggest_selection(monkeypatch, capsys, declared):
    from mimir.plugins import discovery, manager

    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    monkeypatch.setattr(discovery, "discover_plugins", lambda *, enable: (
        SimpleNamespace(name="alpha", enabled=False, diagnostic=None,
                        manifest=SimpleNamespace(cli_commands=declared)),
    ))
    monkeypatch.setattr(manager, "load_plugins", lambda **kwargs: pytest.fail("provider loaded"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["bogus"])
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "invalid choice" in error
    assert "MIMIR_PLUGINS=alpha" not in error


def test_plugins_list_shows_declared_commands_without_loading_provider(monkeypatch, capsys):
    from mimir.commands import plugins

    candidate = _candidate("alpha")
    candidate.manifest = SimpleNamespace(cli_commands={"alpha-setup": True, "alpha-work": False})
    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    monkeypatch.setattr(plugins, "discover_plugins", lambda *, enable: (candidate,))
    cli.main(["plugins", "list"])
    output = capsys.readouterr().out
    assert "mimir alpha-setup — operator" in output
    assert "mimir alpha-work — requires MIMIR_PLUGINS=alpha" in output
    candidate.manifest.cli_commands = None
    cli.main(["plugins", "list"])
    assert "mimir alpha" not in capsys.readouterr().out


def test_unselected_run_help_never_imports_provider():
    env = dict(os.environ)
    env.pop("MIMIR_PLUGINS", None)
    script = """
import mimir.cli
import sys
try:
    mimir.cli.main(['run', '--help'])
except SystemExit as exc:
    assert exc.code == 0
assert not any(name == 'mimir_cultivate' or name.startswith('mimir_cultivate.')
               for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, check=False)
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
