"""Cultivate owns the coding skills; core retains its Chainlink tracker."""

from __future__ import annotations

from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import re
import tomllib

import pytest

from mimir.chat_skills import ChatSkillError, ChatSkillInvocation, ChatSkillRegistry
from mimir.config import Config
from mimir.plugins.defaults import plugin_default_files, seed_plugin_defaults
from mimir.plugins.discovery import enabled_plugins
from mimir.plugins.skills import install_skill, selected_skills, skill_inventory, skills_for_candidates
from mimir.skill_catalog import load_skill


ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / "plugins/cultivate/mimir_cultivate/defaults/skills"
NAMES = {
    "github-poller", "github-ci-watch", "chainlink-orchestrator",
    "worklink-tool-pins", "github", "review",
}
DEFAULTS = {"chainlink-orchestrator", "worklink-tool-pins"}

# The identity cleanup from #2024 is integrated; no exceptions are allowed.
IDENTITY_EXCEPTIONS: dict[str, set[str]] = {}


@pytest.mark.parametrize("literal", ["--repo jasoncarreira/mimir", "mimir-carreira"])
def test_shipped_repository_guard_rejects_mutation(tmp_path, monkeypatch, literal):
    skills = tmp_path / "skills"
    review = skills / "review" / "SKILL.md"
    review.parent.mkdir(parents=True)
    review.write_text((SKILLS / "review" / "SKILL.md").read_text())
    monkeypatch.setattr(__import__(__name__, fromlist=["SKILLS"]), "SKILLS", skills)
    test_shipped_skills_do_not_pin_repository_or_identity()
    with review.open("a") as stream:
        stream.write(f"\ngh pr review <num> {literal}\n")
    with pytest.raises(AssertionError, match="skills/review/SKILL.md"):
        test_shipped_skills_do_not_pin_repository_or_identity()


@pytest.mark.parametrize("name", ["review", "github-poller", "github", "github-ci-watch"])
def test_current_repository_skill_release_hash(name):
    import hashlib

    releases = json.loads((SKILLS.parent / "skill_releases.json").read_text())
    version = {
        "github-poller": "2046-trusted-forge-ifc",
        "review": "0.9.10-2028",
    }.get(name, "0.9.10-2025")
    expected = releases[version][name]["SKILL.md"]
    actual = hashlib.sha256((SKILLS / name / "SKILL.md").read_bytes()).hexdigest()
    assert actual == expected, f"{name}/SKILL.md: actual sha256={actual}"


@pytest.mark.parametrize("relative", [
    "scripts/poller.py", "tests/test_github_poller_pr_pushes.py",
    "tests/test_github_poller_withhold.py", "tests/test_github_poller_issue_notices.py",
])
def test_current_github_poller_release_hash(relative):
    import hashlib

    releases = json.loads((SKILLS.parent / "skill_releases.json").read_text())
    expected = releases["2046-trusted-forge-ifc"]["github-poller"][relative]
    actual = hashlib.sha256((SKILLS / "github-poller" / relative).read_bytes()).hexdigest()
    assert actual == expected, f"{relative}: actual sha256={actual}"


def test_current_github_poller_release_inventory_is_complete():
    # Historical inventories are upgrade evidence, not mutable current hashes.
    # Pin the merged release explicitly and compare the whole packaged tree so
    # new files and changed tests cannot silently fall outside the hash guards.
    [candidate] = enabled_plugins(enable="cultivate")
    [skill] = [item for item in skills_for_candidates((candidate,))
               if item.name == "github-poller"]
    releases = json.loads((SKILLS.parent / "skill_releases.json").read_text())
    assert skill_inventory(skill) == releases["2046-trusted-forge-ifc"]["github-poller"]


# The guard scans the shipped defaults, not test fixtures or cache files.


def test_shipped_skills_do_not_pin_repository_or_identity() -> None:
    root = SKILLS.parent
    observed_exceptions: dict[str, set[str]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or {"tests", "__pycache__", ".pytest_cache"}.intersection(path.relative_to(root).parts):
            continue
        relative = path.relative_to(root).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            assert "jasoncarreira/" not in line, f"{relative}: {line}"
            if "mimir-carreira" in line:
                assert line in IDENTITY_EXCEPTIONS.get(relative, set()), f"{relative}: {line}"
                assert line not in observed_exceptions.setdefault(relative, set()), f"{relative}: duplicate {line}"
                observed_exceptions[relative].add(line)
    assert observed_exceptions == IDENTITY_EXCEPTIONS


def test_selected_provider_seeds_only_its_coding_skills() -> None:
    [candidate] = enabled_plugins(enable="cultivate")
    manifest = tomllib.loads((ROOT / "plugins/cultivate/mimir-plugin.toml").read_text())
    assert {Path(path).name for path in manifest["defaults"]["skills"]} == DEFAULTS
    assert {Path(entry["path"]).name for entry in manifest["optional"]["skills"]} == NAMES - DEFAULTS
    files = plugin_default_files(candidate)
    for name in DEFAULTS:
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
    for name in DEFAULTS:
        prefix = Path("skills") / f"cultivate-{name}"
        assert (SKILLS / name / "pollers.json").is_file()
        assert (SKILLS / name / "scripts/poller.py").is_file()
        assert prefix / "pollers.json" in files
        assert prefix / "scripts/poller.py" in files


def test_selected_home_catalog_commands_and_documented_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    [candidate] = enabled_plugins(enable="cultivate")
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "")
    monkeypatch.setenv("MIMIR_CHAT_SKILLS_ENABLED", "true")
    monkeypatch.setenv(
        "MIMIR_CHAT_SKILL_ALLOWLIST",
        ",".join(f"cultivate-{name}" for name in sorted(NAMES)),
    )

    created = seed_plugin_defaults(home, selected=(candidate,))
    assert not (home / "skills/cultivate-github-poller").exists()
    for skill in selected_skills():
        if not skill.default:
            install_skill(home, skill)
    registry = ChatSkillRegistry.from_config(Config.from_env())
    assert {skill.command for skill in registry.list_discoverable()} == {
        f"/cultivate-{name}" for name in NAMES
    }
    for name in NAMES:
        source = SKILLS / name
        installed = home / "skills" / f"cultivate-{name}"
        if name in DEFAULTS:
            assert created[f"skills/cultivate-{name}/SKILL.md"] == "created"
        assert installed.joinpath("SKILL.md").read_bytes() == source.joinpath("SKILL.md").read_bytes()
        assert load_skill(installed).name == f"cultivate-{name}"
        if name not in {"github", "review"}:
            text = source.joinpath("SKILL.md").read_text(encoding="utf-8")
            assert f"plugins/cultivate/mimir_cultivate/defaults/skills/{name}/" in text
            assert f"<home>/skills/cultivate-{name}/" in text
            assert installed.joinpath("pollers.json").is_file()
            assert installed.joinpath("scripts/poller.py").is_file()
            documented_tests = re.findall(r"pytest -q ([^\s`]+)", text)
            assert documented_tests, name
            assert all((ROOT / path).exists() for path in documented_tests)

    review = registry.resolve_post_content("/cultivate-review 42")
    assert isinstance(review, ChatSkillInvocation)
    assert review.name == "cultivate-review"
    block = registry.prompt_block_for_invocation(asdict(review))
    assert block is not None and "submit via" in block
    assert isinstance(registry.resolve_post_content("/review 42"), ChatSkillError)
    assert (ROOT / "mimir/skills/chainlink/SKILL.md").is_file()


def test_selected_home_github_poller_emits_resolvable_review_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    [candidate] = enabled_plugins(enable="cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    home = tmp_path / "home"
    seed_plugin_defaults(home, selected=(candidate,))
    for skill in selected_skills():
        if not skill.default:
            install_skill(home, skill)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_GITHUB_PRELOAD_REVIEW_SKILL", "1")
    monkeypatch.delenv("MIMIR_GITHUB_REVIEW_SKILL_PATH", raising=False)
    script = SKILLS / "github-poller/scripts/poller.py"
    spec = importlib.util.spec_from_file_location("cultivate_github_poller_defaults", script)
    assert spec is not None and spec.loader is not None
    poller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(poller)

    poller._emit("Review this pull request", event_type="pr_opened")
    emitted = json.loads(capsys.readouterr().out)
    assert "/cultivate-review skill spells out the full flow" in emitted["prompt"]
    guidance = emitted["prompt"].split("──── /cultivate-review SKILL.md (pre-loaded) ────", 1)[0]
    assert guidance.index("repo_checkout") < guidance.index("pr_metadata")
    assert guidance.index("pr_metadata") < guidance.index("pr_submit_review")
    assert "/cultivate-review SKILL.md (pre-loaded)" in emitted["prompt"]
    assert "submit via" in emitted["prompt"]
    assert "/review skill spells out" not in emitted["prompt"]
    frontmatter = (SKILLS / "github-poller/SKILL.md").read_text(encoding="utf-8")
    assert "<home>/skills/cultivate-review/SKILL.md" in frontmatter
    assert ".mimir_builtin_skills/review/" not in frontmatter


def test_service_review_skill_checks_out_before_every_pr_read():
    skill = (SKILLS / "review/SKILL.md").read_text(encoding="utf-8")
    steps = skill.split("### Service turns: checkout", 1)[1].split("### 1. Get PR context", 1)[0]
    assert steps.index("repo_checkout(repository, pull_request)") < steps.index("pr_metadata")
    assert steps.index("pr_metadata") < steps.index("pr_diff") < steps.index("pr_submit_review")
    assert steps.index("pr_submit_review") < steps.index("repo_cleanup(repository, pull_request)")
