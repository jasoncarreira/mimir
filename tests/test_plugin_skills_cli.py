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
    fallback = {p.name: p for p in discover_pollers(
        home / "skills", policy=manager.policy, invalid_entries=invalid)}
    assert "github-activity" in fallback
    assert fallback["github-activity"].authority.github_activity
    assert fallback["github-activity"].manifest_path != dest / "pollers.json"
    assert skill_state(home, poller) == "modified"
    assert cli("install", "github-poller", "--home", str(home)) == 3
    assert "--restore" in capsys.readouterr().out
    assert cli("install", "github-poller", "--restore", "--home", str(home)) == 0
    backup, = (home / ".mimir/plugin-skill-backups").glob("cultivate-github-poller-*")
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
    for name in ("github-poller", "cultivate:github-poller"):
        assert cli("install", name, "--home", str(home)) == 2
        error = capsys.readouterr().out
        assert "unselected plugin" in error and "MIMIR_PLUGINS" in error
    assert not (home / "skills" / skill.dirname).exists()


def test_forge_installs_only_optionals(tmp_path, monkeypatch):
    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    assert cli("install", "--forge", "github", "--home", str(tmp_path)) == 0
    expected = {s.dirname for s in selected_skills() if s.forge == "github" and not s.default}
    assert expected
    assert {p.name for p in (tmp_path / "skills").iterdir()} == expected
    assert all(skill_state(tmp_path, s) == "not installed" for s in selected_skills() if s.default)


def test_restore_backup_is_outside_all_loaders(tmp_path, monkeypatch):
    from mimir.skill_catalog import load_catalog
    from mimir.plugins.defaults import unselected_seeded_skills

    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    skill = next(s for s in selected_skills() if s.name == "github-poller")
    dest, _ = install_skill(tmp_path, skill)
    (dest / "SKILL.md").write_text("---\nname: backup-must-not-load\ndescription: old\n---\nold")
    _, backup = install_skill(tmp_path, skill, restore=True)
    assert backup is not None and backup.parent == tmp_path / ".mimir/plugin-skill-backups"
    assert "backup-must-not-load" not in {s.name for s in load_catalog(tmp_path / "skills")}
    assert {p.name for p in (tmp_path / "skills").iterdir()} == {skill.dirname}
    assert unselected_seeded_skills(tmp_path, frozenset()) == {skill.dirname: "cultivate"}
    assert "backup-must-not-load" in (backup / "SKILL.md").read_text()


def test_install_before_seeding_preserves_initial_jobs_and_permissions(tmp_path):
    import os
    import stat
    from tests.test_defaults_upgrade import _plugin
    from mimir.plugins.skills import PluginSkill

    candidate = _plugin(tmp_path)
    skill = PluginSkill(candidate, "helper", None, True)
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "scheduler.yaml").write_text("[]\n")
    dest, _ = install_skill(installed, skill)
    assert not (installed / ".mimir/seed-plugin-defaults/sample").exists()
    seed_plugin_defaults(installed, selected=(candidate,))
    assert "sample-heartbeat" in (installed / "scheduler.yaml").read_text()
    assert (installed / ".mimir/seed-plugin-defaults/sample").read_text() == "1.0\n"
    seeded = tmp_path / "seeded"
    previous_umask = os.umask(0o022)
    try:
        seed_plugin_defaults(seeded, selected=(candidate,))
    finally:
        os.umask(previous_umask)
    # Compare against the actual seeder under a controlled umask.
    seeded_dir = seeded / "skills/sample-helper"
    assert stat.S_IMODE(dest.stat().st_mode) == stat.S_IMODE(seeded_dir.stat().st_mode) == 0o755
    assert {p.relative_to(dest): p.read_bytes() for p in dest.rglob("*") if p.is_file()} == {
        p.relative_to(seeded_dir): p.read_bytes() for p in seeded_dir.rglob("*") if p.is_file()}


@pytest.mark.parametrize("edit", ["none", "doc", "manifest"])
def test_real_previous_release_poller_seeds_keep_running(tmp_path, monkeypatch, edit):
    import json
    from dataclasses import replace

    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    monkeypatch.setenv("GITHUB_REPOS", "o/r")
    manager = load_plugins(enable="cultivate")
    candidate, = manager.policy.selected_plugin_candidates
    # Immutable pre-change base, not the current package dressed as an old manifest.
    fixture = json.loads((Path(__file__).parent / "fixtures/plugin_skills_pre_optional.json").read_text())
    assert fixture["source_commit"] == "e52e30daced64c5c5ad0ea266c27a5be07ef299e"
    old = replace(candidate, manifest=replace(candidate.manifest, defaults={
        **candidate.manifest.defaults,
        "skills": (*candidate.manifest.defaults["skills"],
                   *(path for path, _ in candidate.manifest.optional_skills)),
    }, optional_skills=()))
    seed_plugin_defaults(tmp_path, selected=(old,))
    for name in ("github-poller", "github-ci-watch"):
        dest = tmp_path / "skills" / f"cultivate-{name}"
        # Reinsert captured old-release regions, without requiring Git objects
        # in wheel/contained/shallow-CI environments.
        current = (dest / "SKILL.md").read_text()
        lines = current.splitlines(keepends=True)
        lines[2] = f'description: "{fixture[name]["description"]}"\n'
        current = "".join(lines)
        heading_end = current.index("\n\n", current.index("# Cultivate")) + 2
        configure_start = current.index("2. ", heading_end)
        current = current[:heading_end] + fixture[name]["body"] + current[configure_start:]
        current = current.replace("Install it with mimir skills install cultivate:review; no path override is needed.",
                                  "No path override is needed after provider defaults seed the home.")
        old_doc = current.encode()
        assert old_doc != (dest / "SKILL.md").read_bytes()
        (dest / "SKILL.md").write_bytes(old_doc)
        if edit == "doc":
            (dest / "SKILL.md").write_bytes(old_doc + b"\noperator note\n")
        elif edit == "manifest":
            manifest = dest / "pollers.json"
            manifest.write_bytes(manifest.read_bytes() + b" ")
    pollers = {p.name: p for p in discover_pollers(tmp_path / "skills", policy=manager.policy)}
    for name, skill_name in (("github-activity", "github-poller"), ("github-ci-watch", "github-ci-watch")):
        poller = pollers[name]
        assert poller.authority.canonical == f"poller:{name}"
        # Modified copies cannot gain authority. The inventoried package runs.
        assert poller.manifest_path != tmp_path / "skills" / f"cultivate-{skill_name}" / "pollers.json"
    assert {"github-activity", "github-ci-watch"}.isdisjoint(
        p.name for p in discover_pollers(tmp_path / "skills", policy=load_plugins(enable="").policy))


def test_setup_report_and_boot_hint(tmp_path, monkeypatch, capsys, caplog):
    import logging
    from mimir.commands.setup import _print_setup_report
    from mimir.plugins.skills import log_optional_hint, uninstalled_optional

    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    candidates = enabled_plugins()
    labels = [s.label for s in uninstalled_optional(tmp_path)]
    assert "cultivate:github-poller" in labels
    _print_setup_report({"home": str(tmp_path), "dirs_created": [], "files_created": [],
                         "skills": {}, "subagents": {}, "plugin_optional": labels})
    printed = capsys.readouterr().out
    assert "cultivate:github-poller" in printed
    assert "mimir skills install <plugin>:<skill>" in printed
    logger = logging.getLogger("test-plugin-boot")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_optional_hint(tmp_path, candidates, logger)
    assert len(caplog.records) == 1
    assert all(label in caplog.text for label in labels)
    assert "mimir skills install" in caplog.text
    for skill in selected_skills():
        if not skill.default:
            install_skill(tmp_path, skill)
    caplog.clear()
    log_optional_hint(tmp_path, candidates, logger)
    log_optional_hint(tmp_path, (), logger)
    assert not caplog.records
    # Pin production boot wiring as well as exercising the hint implementation.
    import ast
    tree = ast.parse((Path(__file__).parents[1] / "mimir/server.py").read_text())
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "log_optional_hint" for node in ast.walk(tree))
