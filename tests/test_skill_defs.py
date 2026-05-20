"""Skill seeding into <home>/.claude/skills/ (SPEC §8.4)."""

from __future__ import annotations

from pathlib import Path

from mimir.skill_defs import (
    _bundled_skill_names,
    installed_skill_names,
    seed_skills,
)


def test_bundled_skills_include_expected_set():
    names = set(_bundled_skill_names())
    # Verbatim ports + the rewritten/adapted ones. Skills dropped from
    # the bundle (because they were never loaded in production and the
    # WHEN/WHY guidance fit better as in-prompt convention or didn't
    # apply to mimir-the-agent's workload): ``mountaineering`` (open-
    # strix climb pattern — bench-shaped, never used outside benchmarks);
    # ``journal`` (per-turn checkpoint — superseded by turns.jsonl).
    expected = {
        "alert",  # v0.4 §6
        "five-whys",
        "heartbeat",  # v0.4 §1
        "introspection",
        "long-running-jobs",
        "memory",
        "onboarding",
        "pollers",
        "skill-acquisition",
        "skill-creator",
        "view-attachment",
        "wiki",
    }
    missing = expected - names
    assert not missing, f"missing bundled skills: {missing}"


def test_wiki_skill_is_domain_neutral():
    """The wiki skill should NOT mention bench-specific domains. The whole
    point is that it's general-purpose graph memory; benchmark guidance
    belongs in the bench-task prompts, not the bundled skill."""
    skill = (Path(__file__).parent.parent / "mimir" / "skills" / "wiki" / "SKILL.md").read_text()
    body = skill.lower()
    for bad in ("bluesky", "bsky", "bench-", "minimax"):
        assert bad not in body, f"wiki skill leaks domain reference: {bad}"


def test_memory_skill_references_wiki(tmp_path: Path):
    seed_skills(tmp_path)
    body = (tmp_path / ".claude" / "skills" / "memory" / "SKILL.md").read_text()
    assert "state/wiki" in body or "wiki skill" in body.lower(), (
        "memory skill should point at the wiki layer for graph-shaped content"
    )


def test_seed_skills_creates_missing_skills(tmp_path: Path):
    out = seed_skills(tmp_path)
    # Every bundled skill landed under <home>/.claude/skills/
    target = tmp_path / ".claude" / "skills"
    assert target.is_dir()
    for name, status in out.items():
        assert status == "created", f"{name}: {status}"
        assert (target / name / "SKILL.md").is_file()


def test_seed_skills_preserves_user_customizations(tmp_path: Path):
    """A pre-existing skill folder is left alone — we only create new ones."""
    target = tmp_path / ".claude" / "skills" / "memory"
    target.mkdir(parents=True)
    user_skill = target / "SKILL.md"
    user_skill.write_text("# my custom memory skill\n")

    out = seed_skills(tmp_path)
    assert out["memory"] == "present"
    # Untouched.
    assert user_skill.read_text() == "# my custom memory skill\n"


def test_memory_skill_no_brand_leaks(tmp_path: Path):
    seed_skills(tmp_path)
    body = (tmp_path / ".claude" / "skills" / "memory" / "SKILL.md").read_text()
    # Adapted from open-strix-base; brand references should be gone.
    assert "open_strix_builtin" not in body
    assert "open-strix" not in body
    # Mimir-specific surface should be present.
    assert "memory/core/" in body
    assert "saga_store" in body or "SAGA" in body


def test_seed_skills_recovers_poisoned_destination(tmp_path: Path):
    """A pre-existing skill folder missing SKILL.md (a half-copy from a
    crashed prior run) gets re-seeded from the bundle, not skipped."""
    target = tmp_path / ".claude" / "skills" / "memory"
    target.mkdir(parents=True)
    # Half-copied: a stray file landed but SKILL.md never made it.
    (target / "stray.md").write_text("partial")

    out = seed_skills(tmp_path)
    assert out["memory"] == "created"
    # The half-copy was replaced with a real bundle.
    assert (target / "SKILL.md").is_file()
    assert not (target / "stray.md").exists(), "poisoned remnants should be gone"


def test_seed_skills_cleans_up_tmp_from_prior_crash(tmp_path: Path):
    """A leftover ``<name>.tmp`` from a crashed prior copy is wiped before
    the next attempt."""
    leftover = tmp_path / ".claude" / "skills" / "memory.tmp"
    leftover.mkdir(parents=True)
    (leftover / "garbage.md").write_text("from a dead process")

    seed_skills(tmp_path)
    # The .tmp dir was either renamed into place (success) or wiped (error).
    # Either way it shouldn't survive as ``<name>.tmp``.
    assert not leftover.exists()
    assert (tmp_path / ".claude" / "skills" / "memory" / "SKILL.md").is_file()


def test_installed_skill_names_includes_user_added_skills(tmp_path: Path):
    """§12.4 review #11: skills the user adds under <home>/.claude/skills/
    should appear in the ranker's input alongside bundled skills."""
    seed_skills(tmp_path)
    user_skill = tmp_path / ".claude" / "skills" / "my-custom-skill"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("---\nname: my-custom-skill\n---\nbody")

    names = installed_skill_names(tmp_path)
    assert "my-custom-skill" in names
    # Bundled set still present.
    assert "memory" in names


def test_installed_skill_names_falls_back_to_bundled_for_fresh_home(tmp_path: Path):
    """No .claude/skills dir yet: fall back to the bundled set so a
    fresh agent that hasn't run setup still gets a populated ranker."""
    names = installed_skill_names(tmp_path)
    assert "memory" in names
    assert "heartbeat" in names


def test_installed_skill_names_skips_dirs_without_skill_md(tmp_path: Path):
    """A directory without a SKILL.md is not a valid skill — skip it."""
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "broken").mkdir()  # no SKILL.md
    names = installed_skill_names(tmp_path)
    assert "broken" not in names
