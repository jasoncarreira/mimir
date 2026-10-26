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
    listing = capsys.readouterr().out
    assert "cultivate  github-poller" not in listing
    assert "Installed but unselected plugins: cultivate; select with MIMIR_PLUGINS." in listing
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


_PRE_OPTIONAL_SKILLS = ("github-poller", "github-ci-watch", "github", "review")
_PRE_OPTIONAL_ROOT = (Path(__file__).parents[1] /
                      "plugins/cultivate/mimir_cultivate/defaults/skills")


def _assert_previous_release_inventory(root: Path) -> dict[str, bytes]:
    import hashlib
    import json

    fixture = json.loads((Path(__file__).parent / "fixtures/plugin_skills_pre_optional.json").read_text())
    assert fixture["source_commit"] == "e52e30daced64c5c5ad0ea266c27a5be07ef299e"
    expected = fixture["files"]
    assert len(expected) == 28
    assert {Path(path).parts[0] for path in expected} == set(_PRE_OPTIONAL_SKILLS)
    actual = {}
    for name in _PRE_OPTIONAL_SKILLS:
        tree = root / name
        assert tree.is_dir() and not tree.is_symlink(), name
        for path in tree.rglob("*"):
            # Runtime caches are not shipped skill resources. Everything else,
            # including newly added files, must match the historical inventory.
            if {"__pycache__", ".pytest_cache"}.intersection(path.relative_to(tree).parts):
                continue
            assert not path.is_symlink(), path
            if path.is_file():
                actual[path.relative_to(root).as_posix()] = path.read_bytes()
    assert set(actual) == set(expected), "historical skill file-set drift"
    for path, data in actual.items():
        assert hashlib.sha256(data).hexdigest() == expected[path], f"historical skill byte drift: {path}"
    return actual


def test_packaged_pre_optional_skill_inventory_is_unchanged(monkeypatch):
    import hashlib
    import json

    fixture = json.loads((Path(__file__).parent / "fixtures/plugin_skills_pre_optional.json").read_text())
    releases = json.loads((_PRE_OPTIONAL_ROOT.parent / "skill_releases.json").read_text())
    assert fixture["source_commit"] in releases
    assert {f"{skill}/{rel}": digest for skill, files in releases[fixture["source_commit"]].items()
            for rel, digest in files.items()} == fixture["files"]
    # An edited shipped tree needs its *previous* complete inventory recorded.
    # Once updated, this first entry is a known earlier version, not a freeze.
    import tomllib

    # Enforce all declared seeds even in a core-only environment where the
    # optional distribution is not installed. The manifest owns this list.
    manifest = tomllib.loads((_PRE_OPTIONAL_ROOT.parents[2] / "mimir-plugin.toml").read_text())
    directories = [*manifest["defaults"]["skills"],
                   *(entry["path"] for entry in manifest["optional"]["skills"])]
    for directory in directories:
        name = Path(directory).name
        tree = _PRE_OPTIONAL_ROOT / name
        current = {path.relative_to(tree).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in tree.rglob("*") if path.is_file()
                   and not {"__pycache__", ".pytest_cache"}.intersection(path.relative_to(tree).parts)}
        assert current, name
        recorded = [files[name] for files in releases.values() if name in files]
        assert recorded, f"{name}: missing release inventory"
        assert current == recorded[-1], f"{name}: record the preceding inventory before changing packaged bytes"
        old = releases[fixture["source_commit"]].get(name)
        if old is not None:
            assert old in recorded


@pytest.mark.parametrize("path", [
    "github-poller/SKILL.md", "github-ci-watch/SKILL.md",
    "github-poller/pollers.json", "github-ci-watch/pollers.json",
    "github-poller/scripts/poller.py", "github-poller/scripts/hooks.py",
    "github-ci-watch/scripts/ci_logs.py", "github-ci-watch/scripts/poller.py",
    "github-poller/tests/test_github_poller_windows.py",
    "github-ci-watch/tests/test_ci_state.py", "github/SKILL.md", "review/SKILL.md",
])
@pytest.mark.parametrize("drift", ["append", "add", "remove", "rename"])
def test_previous_release_inventory_rejects_drift(tmp_path, path, drift):
    import subprocess
    import json

    fixture = json.loads((Path(__file__).parent / "fixtures/plugin_skills_pre_optional.json").read_text())
    released = {}
    for name in fixture["files"]:
        result = subprocess.run(["git", "-c", f"safe.directory={Path(__file__).parents[1]}",
                                 "show", f"{fixture['source_commit']}:plugins/cultivate/mimir_cultivate/defaults/skills/{name}"],
                                capture_output=True, cwd=Path(__file__).parents[1])
        if result.returncode:
            pytest.skip("historical Git objects not available in this checkout")
        released[name] = result.stdout
    for name, data in released.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # Positive control on the same copy and helper used by each negative case.
    assert _assert_previous_release_inventory(tmp_path) == released
    target = tmp_path / path
    if drift == "append":
        target.write_bytes(target.read_bytes() + b"\ntrailing drift\n")
    elif drift == "add":
        target.with_name(target.name + ".new").write_bytes(b"new file\n")
    elif drift == "remove":
        target.unlink()
    else:
        target.rename(target.with_name(target.name + ".renamed"))
    with pytest.raises(AssertionError, match="historical skill (byte|file-set) drift"):
        _assert_previous_release_inventory(tmp_path)


@pytest.mark.parametrize("edit", ["none", "doc", "manifest"])
def test_real_previous_release_poller_seeds_keep_running(tmp_path, monkeypatch, capsys, edit):
    pytest.importorskip("mimir_cultivate")
    import re
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    monkeypatch.setenv("GITHUB_REPOS", "o/r")
    manager = load_plugins(enable="cultivate")
    candidate, = manager.policy.selected_plugin_candidates
    import subprocess
    import json

    fixture = json.loads((Path(__file__).parent / "fixtures/plugin_skills_pre_optional.json").read_text())
    released = {}
    for name in fixture["files"]:
        result = subprocess.run(["git", "-c", f"safe.directory={Path(__file__).parents[1]}",
                                 "show", f"{fixture['source_commit']}:plugins/cultivate/mimir_cultivate/defaults/skills/{name}"],
                                capture_output=True, cwd=Path(__file__).parents[1])
        if result.returncode:
            pytest.skip("historical Git objects not available in this checkout")
        released[name] = result.stdout
    for name, data in released.items():
        dest = tmp_path / "skills" / f"cultivate-{name.split('/')[0]}" / Path(name).relative_to(name.split('/')[0])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    from mimir.plugins.skills import skill_inventory
    import hashlib

    expected_states = {}
    for name in _PRE_OPTIONAL_SKILLS:
        dest = tmp_path / "skills" / f"cultivate-{name}"
        expected = {path.removeprefix(name + "/"): data for path, data in released.items()
                    if path.startswith(name + "/")}
        assert {p.relative_to(dest).as_posix(): p.read_bytes()
                for p in dest.rglob("*") if p.is_file()} == expected
        skill = next(s for s in selected_skills() if s.name == name)
        old_inventory = {rel: hashlib.sha256(data).hexdigest() for rel, data in expected.items()}
        expected_states[name] = "installed" if old_inventory == skill_inventory(skill) else "upgrade available"
        assert skill_state(tmp_path, skill) == expected_states[name]
    for name in ("github-poller", "github-ci-watch"):
        dest = tmp_path / "skills" / f"cultivate-{name}"
        old_doc = released[f"{name}/SKILL.md"]
        if edit == "doc":
            (dest / "SKILL.md").write_bytes(old_doc + b"\noperator note\n")
        elif edit == "manifest":
            manifest = dest / "pollers.json"
            manifest.write_bytes(manifest.read_bytes() + b" ")
    invalid = []
    pollers = {p.name: p for p in discover_pollers(
        tmp_path / "skills", policy=manager.policy, invalid_entries=invalid)}
    for name, skill_name in (("github-activity", "github-poller"), ("github-ci-watch", "github-ci-watch")):
        poller = pollers[name]
        assert poller.authority.canonical == f"poller:{name}"
        home_manifest = tmp_path / "skills" / f"cultivate-{skill_name}" / "pollers.json"
        assert (poller.manifest_path == home_manifest) is (edit == "none" and expected_states[skill_name] == "installed")
        skill = next(s for s in selected_skills() if s.name == skill_name)
        assert skill_state(tmp_path, skill) == (expected_states[skill_name] if edit == "none" else "modified")
    if edit == "none":
        assert not [entry for entry in invalid if entry[1] in {"github-activity", "github-ci-watch"}]
        assert cli("list-optional", "--home", str(tmp_path)) == 0
        listing = capsys.readouterr().out
        for name in _PRE_OPTIONAL_SKILLS:
            poller_flag = "[poller]" if name in {"github-poller", "github-ci-watch"} else "-"
            assert f"cultivate  {name}  {poller_flag}  github  {expected_states[name]}" in listing
            if expected_states[name] == "upgrade available":
                assert f"mimir skills install cultivate:{name} --restore" in listing
        assert cli("install", "--forge", "github", "--home", str(tmp_path)) == 0
        output = capsys.readouterr().out
        for name in _PRE_OPTIONAL_SKILLS:
            if expected_states[name] == "upgrade available":
                assert f"upgraded skill cultivate:{name}" in output
                # github is also a prefix of github-poller and github-ci-watch.
                backup, = (path for path in (tmp_path / ".mimir/plugin-skill-backups").glob(f"cultivate-{name}-*")
                           if re.fullmatch(rf"cultivate-{re.escape(name)}-\d{{8}}T\d{{12}}Z", path.name))
                assert (backup / "SKILL.md").read_bytes() == released[f"{name}/SKILL.md"]
            else:
                assert f"cultivate:{name}: installed (skipped)" in output
    assert {"github-activity", "github-ci-watch"}.isdisjoint(
        p.name for p in discover_pollers(tmp_path / "skills", policy=load_plugins(enable="").policy))


@pytest.mark.asyncio
async def test_prior_release_upgrade_notification_and_apply(tmp_path, monkeypatch, capsys, caplog):
    import hashlib
    import json
    import logging
    import subprocess
    from types import SimpleNamespace
    from mimir.agent import Agent
    from mimir.plugins.skills import (log_optional_hint, notify_skill_upgrades,
                                      pending_skill_upgrades, previous_skill_inventories)
    from mimir.skill_visibility import hidden_home_skill_dirs

    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    monkeypatch.setenv("GITHUB_REPOS", "o/r")
    manager = load_plugins(enable="cultivate")
    skill = next(s for s in selected_skills() if s.name == "github-poller")
    version, inventory = next(iter(previous_skill_inventories(skill).items()))
    dest = tmp_path / "skills" / skill.dirname
    for rel, digest in inventory.items():
        result = subprocess.run([
            "git", "-c", f"safe.directory={Path(__file__).parents[1]}", "show",
            f"{version}:plugins/cultivate/mimir_cultivate/defaults/skills/github-poller/{rel}",
        ], capture_output=True, cwd=Path(__file__).parents[1])
        if result.returncode:
            pytest.skip("historical Git objects not available")
        assert hashlib.sha256(result.stdout).hexdigest() == digest
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(result.stdout)
    assert skill_state(tmp_path, skill) == "upgrade available"
    assert dest in hidden_home_skill_dirs(tmp_path, manager.policy)
    invalid = []
    with caplog.at_level(logging.WARNING):
        pollers = discover_pollers(tmp_path / "skills", policy=manager.policy, invalid_entries=invalid)
    activity = next(p for p in pollers if p.name == "github-activity")
    assert activity.manifest_path != dest / "pollers.json"
    assert activity.authority.github_activity
    assert not [entry for entry in invalid if entry[1] == "github-activity"]
    assert "poller_authority_rejected" not in caplog.text

    agent = Agent.__new__(Agent)
    agent._config = SimpleNamespace(home=tmp_path, plugin_manager=manager,
                                     operator_alert_channel="alerts", writable_dirs=[],
                                     access_control_enforced=False)
    agent._indexes = None
    command = "mimir skills install cultivate:github-poller --restore"
    assert command in agent._build_system_prompt(emit_health_events=False)
    logger = logging.getLogger("plugin-upgrade-boot-test")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_optional_hint(tmp_path, manager.policy.selected_plugin_candidates, logger)
    assert command in caplog.text
    assert cli("list-optional", "--home", str(tmp_path)) == 0
    assert command in capsys.readouterr().out

    events, sent = [], []
    async def event(name, **fields):
        events.append((name, fields))
    async def send(channel, text, *, final):
        sent.append((channel, text, final))
    monkeypatch.setattr("mimir.event_logger.log_event", event)
    for _ in range(3):
        await notify_skill_upgrades(tmp_path, manager.policy.selected_plugin_candidates, "alerts", send)
    from mimir.plugins.skills import previous_skill_inventories, skill_inventory
    target_release = next(key for key, inventory in previous_skill_inventories(skill).items()
                          if inventory == skill_inventory(skill))
    assert events == [("plugin_skill_upgrade_available", {
        "skill": skill.label, "version": skill.candidate.manifest.defaults["version"],
        "target_release": target_release,
    })]
    assert sent == [("alerts", f"Plugin skill upgrade available: {skill.label}. "
                     f"Run `{command}` to apply it (backup kept).", True)]
    # A reload cannot repeat the alert, including after a send failure was retried.
    await notify_skill_upgrades(tmp_path, manager.policy.selected_plugin_candidates, "alerts", send)
    assert len(events) == len(sent) == 1

    # A changed file or a missing file is not a previous whole-tree release.
    original = (dest / "SKILL.md").read_bytes()
    (dest / "SKILL.md").write_bytes(original + b"\noperator edit\n")
    assert skill_state(tmp_path, skill) == "modified"
    assert not pending_skill_upgrades(tmp_path, [skill])
    assert cli("install", "--forge", "github", "--home", str(tmp_path)) == 3
    assert (dest / "SKILL.md").read_bytes() != original
    (dest / "SKILL.md").write_bytes(original)
    (dest / "tests" / "operator-added.py").write_text("# local\n")
    assert skill_state(tmp_path, skill) == "modified"
    (dest / "tests" / "operator-added.py").unlink()
    (dest / "scripts" / "hooks.py").unlink()
    assert skill_state(tmp_path, skill) == "modified"
    result = subprocess.run([
        "git", "-c", f"safe.directory={Path(__file__).parents[1]}", "show",
        f"{version}:plugins/cultivate/mimir_cultivate/defaults/skills/github-poller/scripts/hooks.py",
    ], capture_output=True, check=True, cwd=Path(__file__).parents[1])
    (dest / "scripts" / "hooks.py").write_bytes(result.stdout)
    assert skill_state(tmp_path, skill) == "upgrade available"
    assert cli("install", "cultivate:github-poller", "--restore", "--home", str(tmp_path)) == 0
    assert skill_state(tmp_path, skill) == "installed"
    assert dest not in hidden_home_skill_dirs(tmp_path, manager.policy)
    assert command not in agent._build_system_prompt(emit_health_events=False)
    caplog.clear()
    log_optional_hint(tmp_path, manager.policy.selected_plugin_candidates, logger)
    assert command not in caplog.text
    backup, = (tmp_path / ".mimir/plugin-skill-backups").glob("cultivate-github-poller-*")
    assert (backup / "SKILL.md").read_bytes() == original


@pytest.mark.parametrize("selected", [True, False])
def test_setup_home_populates_optional_report(tmp_path, monkeypatch, capsys, selected):
    from mimir.commands.setup import setup_home, _print_setup_report

    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate" if selected else "")
    home = tmp_path / "agent"
    status = setup_home(home)
    expected = {f"cultivate:{name}" for name in (
        "github-poller", "github-ci-watch", "github", "review",
    )} if selected else set()
    assert set(status["plugin_optional"]) == expected
    _print_setup_report(status)
    printed = capsys.readouterr().out
    if selected:
        assert all(label in printed for label in expected)
        assert "mimir skills install <plugin>:<skill>" in printed
        assert (home / "skills/cultivate-chainlink-orchestrator/SKILL.md").is_file()
        assert not (home / "skills/cultivate-github-poller").exists()
        # Exercise setup's re-run path after installing just one optional.
        assert cli("install", "cultivate:github-poller", "--home", str(home)) == 0
        rerun = setup_home(home)
        assert set(rerun["plugin_optional"]) == expected - {"cultivate:github-poller"}
    else:
        assert "optional plugin skills:" not in printed
        assert not (home / "skills/cultivate-chainlink-orchestrator").exists()


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
