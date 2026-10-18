"""Cultivate owns the coding skills; core retains its Chainlink tracker."""

from __future__ import annotations

import tomllib
from pathlib import Path

from mimir.plugins.defaults import plugin_default_files
from mimir.plugins.discovery import enabled_plugins


ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / "plugins/cultivate/mimir_cultivate/defaults/skills"
NAMES = {
    "github-poller", "github-ci-watch", "chainlink-orchestrator",
    "worklink-tool-pins", "github", "review",
}


def test_selected_provider_seeds_only_its_coding_skills() -> None:
    [candidate] = enabled_plugins(enable="cultivate")
    manifest = tomllib.loads((ROOT / "plugins/cultivate/mimir-plugin.toml").read_text())
    assert {Path(path).name for path in manifest["defaults"]["skills"]} == NAMES
    files = plugin_default_files(candidate)
    for name in NAMES:
        assert files[Path("skills") / f"cultivate-{name}" / "SKILL.md"] == (
            SKILLS / name / "SKILL.md"
        ).read_text()
    assert Path("skills/cultivate-chainlink/SKILL.md") not in files
    assert (ROOT / "mimir/skills/chainlink/SKILL.md").is_file()
    for name in NAMES:
        assert not (ROOT / "mimir/optional-skills" / name / "SKILL.md").exists()
    for name in ("github", "review"):
        assert not (ROOT / "mimir/skills" / name / "SKILL.md").exists()


def test_unselected_provider_has_no_defaults() -> None:
    assert enabled_plugins(enable="") == ()


def test_moved_poller_resources_are_inventoried() -> None:
    [candidate] = enabled_plugins(enable="cultivate")
    files = plugin_default_files(candidate)
    for name in NAMES - {"github", "review"}:
        prefix = Path("skills") / f"cultivate-{name}"
        assert (SKILLS / name / "pollers.json").is_file()
        assert (SKILLS / name / "scripts/poller.py").is_file()
        assert prefix / "pollers.json" in files
        assert prefix / "scripts/poller.py" in files
