"""Whole-tree upgrade classification and notification regression coverage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.plugins import skills


def _agent(home, manager):
    from mimir.agent import Agent

    agent = Agent.__new__(Agent)
    agent._config = SimpleNamespace(home=home, plugin_manager=manager,
                                   operator_alert_channel="alerts", writable_dirs=[],
                                   access_control_enforced=False)
    agent._indexes = None
    return agent


@pytest.mark.parametrize("failure", ["read", "walk", "permissions", "manifest"])
@pytest.mark.parametrize("consumer", ["pollers", "visibility", "prompt"])
def test_unreadable_home_tree_fails_closed_without_breaking_consumers(
    tmp_path, monkeypatch, request, failure, consumer,
):
    from mimir.plugins.manager import load_plugins
    from mimir.pollers import discover_pollers
    from mimir.skill_visibility import hidden_home_skill_dirs

    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    monkeypatch.setenv("GITHUB_REPOS", "o/r")
    manager = load_plugins(enable="cultivate")
    skill = next(s for s in skills.skills_for_candidates(manager.policy.selected_plugin_candidates)
                 if s.name == "github-poller")
    dest, _ = skills.install_skill(tmp_path, skill)
    agent = _agent(tmp_path, manager)
    full_prompt = agent._build_system_prompt(emit_health_events=False)
    assert "## Core memory" in full_prompt
    events = []
    monkeypatch.setattr(skills, "_unreadable_reported", set())
    monkeypatch.setattr("mimir.event_logger.log_event_sync",
                        lambda name, **fields: events.append((name, fields)))
    unreadable = dest / ("pollers.json" if failure == "manifest" else "tests/conftest.py")
    if failure in {"read", "manifest"}:
        original = Path.read_bytes
        def read_bytes(path):
            if path == unreadable:
                raise PermissionError("private filename and contents")
            return original(path)
        monkeypatch.setattr(Path, "read_bytes", read_bytes)
    elif failure == "permissions":
        mode = unreadable.stat().st_mode
        request.addfinalizer(lambda: unreadable.chmod(mode))
        unreadable.chmod(0)
        try:
            unreadable.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("runner can read mode-000 files; injected read/walk cases remain mandatory")
    else:
        original_walk = skills.os.walk
        def walk(root, **kwargs):
            if root == dest:
                kwargs["onerror"](PermissionError("private filename and contents"))
            yield from original_walk(root, **kwargs)
        monkeypatch.setattr(skills.os, "walk", walk)
    assert skills.skill_state(tmp_path, skill) == "modified"
    if consumer == "pollers":
        registered = {p.name: p for p in discover_pollers(tmp_path / "skills", policy=manager.policy)}
        assert registered["github-activity"].authority.github_activity
        assert registered["github-activity"].manifest_path != dest / "pollers.json"
        assert "worklink-tool-pins" in registered
    elif consumer == "visibility":
        assert hidden_home_skill_dirs(tmp_path, manager.policy) == frozenset()
    else:
        assert agent._build_system_prompt(emit_health_events=False) == full_prompt
    assert events
    assert set(name for name, _ in events) == {"plugin_skill_inventory_unreadable"}
    assert all(fields == {"skill": skill.label} for _, fields in events)


@pytest.mark.parametrize("consumer", ["state", "visibility", "pollers", "prompt"])
def test_unreadable_tree_without_initialised_event_logger(tmp_path, monkeypatch, consumer):
    from mimir import event_logger
    from mimir.plugins.manager import load_plugins
    from mimir.pollers import discover_pollers
    from mimir.skill_visibility import hidden_home_skill_dirs

    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    monkeypatch.setenv("GITHUB_REPOS", "o/r")
    manager = load_plugins(enable="cultivate")
    skill = next(s for s in skills.skills_for_candidates(manager.policy.selected_plugin_candidates)
                 if s.name == "github-poller")
    dest, _ = skills.install_skill(tmp_path, skill)
    agent = _agent(tmp_path, manager)
    full_prompt = agent._build_system_prompt(emit_health_events=False)
    assert "## Core memory" in full_prompt
    # Own the singleton state, but do not replace the failing production API.
    monkeypatch.setattr(event_logger, "_logger", None)
    monkeypatch.setattr(skills, "_unreadable_reported", set())
    original = Path.read_bytes
    def read_bytes(path):
        if path == dest / "SKILL.md":
            raise PermissionError("private filename and contents")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    for _ in range(2):
        if consumer == "state":
            assert skills.skill_state(tmp_path, skill) == "modified"
        elif consumer == "visibility":
            assert hidden_home_skill_dirs(tmp_path, manager.policy) == frozenset()
        elif consumer == "pollers":
            registered = {p.name: p for p in discover_pollers(tmp_path / "skills", policy=manager.policy)}
            assert registered["github-activity"].authority.github_activity
            assert registered["github-activity"].manifest_path != dest / "pollers.json"
        else:
            assert agent._build_system_prompt(emit_health_events=False) == full_prompt


def test_unreadable_tree_cli_without_logger_and_late_logger_dedupe(tmp_path, monkeypatch, capsys):
    from mimir import event_logger
    from tests.test_defaults_upgrade import _plugin
    from tests.test_plugin_skills_cli import cli

    candidate = _plugin(tmp_path)
    skill = skills.PluginSkill(candidate, "helper", "github", False)
    home = tmp_path / "home"
    dest, _ = skills.install_skill(home, skill)
    monkeypatch.setattr(skills, "selected_skills", lambda: [skill])
    monkeypatch.setattr(event_logger, "_logger", None)
    monkeypatch.setattr(skills, "_unreadable_reported", set())
    original = Path.read_bytes
    def read_bytes(path):
        if path == dest / "SKILL.md":
            raise PermissionError("private filename and contents")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert cli("list-optional", "--home", str(home)) == 0
    assert "modified" in capsys.readouterr().out
    assert cli("install", "--forge", "github", "--home", str(home)) == 3
    assert "--restore" in capsys.readouterr().out
    # A failed telemetry attempt must not suppress the first event after init.
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_logger, "_logger", event_logger.EventLogger(log_path, "test"))
    for _ in range(4):
        assert skills.skill_state(home, skill) == "modified"
    # Same label in another home is still once per process per skill.
    other_home = tmp_path / "other-home"
    other_dest, _ = skills.install_skill(other_home, skill)
    def other_read_bytes(path):
        if path == other_dest / "SKILL.md":
            raise PermissionError("another private error")
        return read_bytes(path)
    monkeypatch.setattr(Path, "read_bytes", other_read_bytes)
    assert skills.skill_state(other_home, skill) == "modified"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["type"] == "plugin_skill_inventory_unreadable"
    assert records[0]["skill"] == skill.label
    assert "private" not in log_path.read_text()


def test_non_regular_inventory_file_is_rejected_before_read(tmp_path, monkeypatch):
    from tests.test_defaults_upgrade import _plugin

    skill = skills.PluginSkill(_plugin(tmp_path), "helper", None, True)
    home = tmp_path / "home"
    dest, _ = skills.install_skill(home, skill)
    fifo = dest / "pipe"
    skills.os.mkfifo(fifo)
    original = Path.read_bytes
    def read_bytes(path):
        # Test-owned interlock: removing S_ISREG fails instead of hanging.
        assert path != fifo, "non-regular file must never be opened"
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert skills.skill_state(home, skill) == "modified"


def test_existing_non_directory_skill_root_is_modified_not_missing(tmp_path):
    from tests.test_defaults_upgrade import _plugin

    skill = skills.PluginSkill(_plugin(tmp_path), "helper", None, True)
    home = tmp_path / "home"
    root = home / "skills" / skill.dirname
    assert skills.skill_state(home, skill) == "not installed"
    root.parent.mkdir(parents=True)
    root.write_text("operator-owned file")
    assert skills.skill_state(home, skill) == "modified"
    assert root.read_text() == "operator-owned file"


def test_reserved_identity_rejects_previous_inventory_directly(tmp_path, monkeypatch):
    from mimir.plugins.manager import load_plugins
    from mimir.pollers import _is_selected_cultivate_default

    pytest.importorskip("mimir_cultivate")
    manager = load_plugins(enable="cultivate")
    skill = next(s for s in skills.skills_for_candidates(manager.policy.selected_plugin_candidates)
                 if s.name == "github-poller")
    dest, _ = skills.install_skill(tmp_path, skill)
    manifest = dest / "pollers.json"
    assert _is_selected_cultivate_default(manifest, manager.policy)
    old = skills.skill_inventory(skill)
    new = {**old, "new-file": hashlib.sha256(b"new").hexdigest()}
    monkeypatch.setattr(skills, "skill_inventory", lambda s: new if s.name == skill.name else old)
    monkeypatch.setattr(skills, "previous_skill_inventories", lambda s: {"old": old})
    assert skills.skill_state(tmp_path, skill) == "upgrade available"
    assert not _is_selected_cultivate_default(manifest, manager.policy)


@pytest.mark.parametrize("cache", ["__pycache__", ".pytest_cache"])
def test_runtime_caches_ignored_for_current_and_previous_inventory(tmp_path, monkeypatch, cache):
    from tests.test_defaults_upgrade import _plugin

    candidate = _plugin(tmp_path)
    skill = skills.PluginSkill(candidate, "helper", None, True)
    home = tmp_path / "home"
    dest, _ = skills.install_skill(home, skill)
    cached = dest / "scripts" / cache / "runtime.pyc"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"runtime")
    assert skills.skill_state(home, skill) == "installed"
    old = skills.skill_inventory(skill)
    monkeypatch.setattr(skills, "previous_skill_inventories", lambda s: {"old": old})
    (tmp_path / "sample/skills/helper/SKILL.md").write_text("next release\n")
    assert skills.skill_state(home, skill) == "upgrade available"
    # Ignoring cache bytes must not allow a symlink to pass ownership checks.
    link = dest / "__pycache__"
    link.symlink_to(cached.parent, target_is_directory=True)
    assert skills.skill_state(home, skill) == "modified"


def test_hidden_seed_logs_once_per_middleware(tmp_path, monkeypatch, caplog):
    from mimir.readonly_backend import WriteGuardBackend
    from mimir.skill_visibility import SeedFilteredSkillsMiddleware

    hidden = tmp_path / "skills/sample-helper"
    monkeypatch.setattr("mimir.skill_visibility.hidden_home_skill_dirs", lambda *args: frozenset({hidden}))
    middleware = SeedFilteredSkillsMiddleware(
        backend=WriteGuardBackend(root_dir=tmp_path, writable_dirs=["state"]),
        sources=[str(tmp_path / "skills")], home=tmp_path, policy=None,
    )
    entries = [{"name": "helper", "path": str(hidden / "SKILL.md")}]
    for _ in range(3):
        assert middleware._filter(entries) == []
    records = [record for record in caplog.records if "skill_plugin_seed_hidden" in record.message]
    assert len(records) == 1


@pytest.mark.asyncio
async def test_successive_upgrades_same_defaults_version_each_notify_once(tmp_path, monkeypatch):
    from tests.test_defaults_upgrade import _plugin

    candidate = _plugin(tmp_path)
    candidate.manifest.version = "1.0"
    skill = skills.PluginSkill(candidate, "helper", None, True)
    home = tmp_path / "home"
    skills.install_skill(home, skill)
    release_file = tmp_path / "sample/package/defaults/skill_releases.json"
    release_file.parent.mkdir(parents=True)
    candidate.entry_point.dist.files += (Path("package/defaults/skill_releases.json"),)
    releases = {"initial": {skill.name: skills.skill_inventory(skill)}}
    events, sent = [], []
    async def event(name, **fields):
        events.append((name, fields))
    async def send(channel, text, *, final):
        sent.append((channel, text, final))
    monkeypatch.setattr("mimir.event_logger.log_event", event)
    # Legacy version-keyed markers must not suppress even the first target.
    legacy = home / ".mimir/plugin-skill-upgrades" / skill.dirname / "1.0"
    legacy.mkdir(parents=True)
    for kind in ("event", "alert"):
        (legacy / kind).touch()
    for index in (1, 2):
        source = tmp_path / "sample/skills/helper/SKILL.md"
        source.write_text(f"release {index}\n")
        releases[f"release-{index}"] = {skill.name: skills.skill_inventory(skill)}
        release_file.write_text(json.dumps(releases))
        assert candidate.manifest.defaults["version"] == "1.0"
        assert skills.skill_state(home, skill) == "upgrade available"
        for _ in range(3):
            await skills.notify_skill_upgrades(home, (candidate,), "alerts", send)
        assert len(events) == len(sent) == index
        assert events[-1] == ("plugin_skill_upgrade_available", {
            "skill": skill.label, "version": "1.0", "target_release": f"release-{index}",
        })
        assert sent[-1] == ("alerts", f"Plugin skill upgrade available: {skill.label}. "
                            f"Run `{skills.upgrade_command(skill)}` to apply it (backup kept).", True)
        skills.install_skill(home, skill, restore=True)
        assert skills.skill_state(home, skill) == "installed"
        await skills.notify_skill_upgrades(home, (candidate,), "alerts", send)
        assert len(events) == len(sent) == index
