"""Plugin skills CLI, package integrity and selected-owner regression tests."""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

import pytest

from mimir.commands.skills import add_argparse, dispatch
from mimir.plugins.defaults import plugin_default_files, seed_plugin_defaults
from mimir.plugins.discovery import enabled_plugins
from mimir.plugins.manager import load_plugins
from mimir.plugins.skills import install_skill, selected_skills, skill_state
from mimir.pollers import discover_pollers


def cli(*args: str) -> int:
    parser = ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    skills = add_argparse(sub)
    return dispatch(parser.parse_args(["skills", *args]), skills)


def test_plugin_skill_install_restore_and_forge(tmp_path, monkeypatch, capsys):
    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    home = tmp_path / "home"
    home.mkdir()
    candidate, = enabled_plugins()
    seed_plugin_defaults(home, selected=(candidate,))
    assert not (home / "skills/cultivate-github-poller").exists()
    assert cli("list-optional", "--home", str(home)) == 0
    listing = capsys.readouterr().out
    assert "cultivate  github-poller  [poller]  github  not installed" in listing
    assert "cultivate  chainlink-orchestrator  [poller]  -  installed" in listing

    assert cli("install", "cultivate:github-poller", "--home", str(home)) == 0
    poller = next(s for s in selected_skills() if s.name == "github-poller")
    dest = home / "skills" / poller.dirname
    assert skill_state(home, poller) == "installed"
    assert {p.relative_to(home): p.read_text() for p in dest.rglob("*") if p.is_file()} == plugin_default_files(
        candidate, skill="github-poller")
    assert (home / ".mimir/seed-plugin-defaults/cultivate").is_file()
    monkeypatch.setenv("GITHUB_REPOS", "o/r")
    manager = load_plugins(enable="cultivate")
    names = {p.name: p for p in discover_pollers(home / "skills", policy=manager.policy)}
    assert names["github-activity"].authority.github_activity

    (dest / "SKILL.md").write_text("edited")
    assert skill_state(home, poller) == "modified"
    invalid = []
    assert "github-activity" not in {p.name for p in discover_pollers(
        home / "skills", policy=manager.policy, invalid_entries=invalid)}
    assert any(entry[1] == "github-activity" for entry in invalid)
    assert cli("install", "github-poller", "--home", str(home)) == 3
    assert "--restore" in capsys.readouterr().out
    assert cli("install", "github-poller", "--restore", "--home", str(home)) == 0
    backup, = (home / "skills").glob(".cultivate-github-poller.backup-*")
    assert (backup / "SKILL.md").read_text() == "edited"
    assert str(backup) in capsys.readouterr().out
    assert skill_state(home, poller) == "installed"
    assert {p.name: p for p in discover_pollers(home / "skills", policy=manager.policy)}[
        "github-activity"].authority.github_activity

    assert cli("install", "--forge", "github", "--home", str(home)) == 0
    assert all(skill_state(home, s) == "installed" for s in selected_skills() if s.forge == "github")
    # A symlink in the installed tree must neither be followed for state
    # comparison nor treated as a plugin-owned copy.
    outside = tmp_path / "outside.md"
    outside.write_bytes((dest / "SKILL.md").read_bytes())
    (dest / "SKILL.md").unlink()
    (dest / "SKILL.md").symlink_to(outside)
    assert skill_state(home, poller) == "modified"


def test_unselected_skills_and_symlink_restore_refused(tmp_path, monkeypatch, capsys):
    pytest.importorskip("mimir_cultivate")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    skill = next(s for s in selected_skills() if s.name == "github-poller")
    (home / "skills").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("keep")
    (home / "skills" / skill.dirname).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        install_skill(home, skill, restore=True)
    assert (outside / "SKILL.md").read_text() == "keep"
    (home / "skills" / skill.dirname).unlink()
    monkeypatch.setenv("MIMIR_PLUGINS", "")
    assert cli("install", "cultivate:github-poller", "--home", str(home)) == 2
    assert "MIMIR_PLUGINS" in capsys.readouterr().out
    assert cli("list-optional", "--home", str(home)) == 0
    assert "cultivate  github-poller" not in capsys.readouterr().out
    assert cli("install", "--forge", "github", "--home", str(home)) == 2
