"""Selected Cultivate command and operator-script wiring."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mimir.cli import main
from mimir.plugins.manager import load_plugins
from mimir_cultivate import commands


def test_selected_cli_registers_both_commands() -> None:
    policy = load_plugins(enable="cultivate").policy
    assert set(policy.cli_commands) >= {"worklink", "opencode-bootstrap"}
    assert {policy.cli_commands[name].owner for name in ("worklink", "opencode-bootstrap")} == {"cultivate"}
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    policy.cli_commands["worklink"].configure(sub)
    policy.cli_commands["opencode-bootstrap"].configure(sub)
    assert parser.parse_args(["worklink", "run-epic", "42", "--autonomous"]).issue_id == 42
    assert parser.parse_args(["opencode-bootstrap", "--home", "/tmp/operator"]).home == Path("/tmp/operator")
    assert policy.cli_commands["cultivate"].operator is True
    assert policy.cli_commands["worklink"].operator is False


@pytest.mark.parametrize("name", ["worklink", "opencode-bootstrap"])
def test_core_cli_does_not_expose_cultivate_commands(
    name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        main([name, "--help"])
    assert exit_info.value.code == 2


def test_unselected_cli_does_not_import_cultivate() -> None:
    env = dict(os.environ)
    env.pop("MIMIR_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, "-c", "import mimir.cli; import sys; "
         "mimir.cli.main(['plugins', 'list']); "
         "assert not any(k == 'mimir_cultivate' or k.startswith('mimir_cultivate.') "
         "for k in sys.modules)"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_opencode_bootstrap_preserves_merge_and_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from mimir import scaffold_docker

    calls: list[Path] = []
    monkeypatch.setattr(scaffold_docker, "opencode_config_path", lambda home: home / "config")
    monkeypatch.setattr(scaffold_docker, "ensure_opencode_config", lambda path: calls.append(path) or True)
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    main(["opencode-bootstrap", "--home", str(tmp_path)])
    assert calls == [tmp_path / "config"]
    assert capsys.readouterr().out == f"updated {tmp_path / 'config'}\n"


def test_operator_scripts_use_provider_records(tmp_path: Path) -> None:
    from scripts import factory_park, factory_resume
    from mimir_cultivate.worklink import factory_state

    assert factory_park.load_factory_record is factory_state.load_factory_record
    assert factory_resume.load_factory_record is factory_state.load_factory_record
    # operator_root resolves the sandbox (macOS /tmp and /var are symlinks), so
    # compare against the resolved checkout rather than a literal spelling.
    checkout = tmp_path / "checkout"
    assert factory_park.operator_root(checkout / ".factory-sandboxes" / "run") == checkout.resolve()
    assert commands._resolve_worklink_repo(tmp_path / "repo") == (tmp_path / "repo").resolve()
