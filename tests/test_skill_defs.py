"""Skill seeding into <home>/.claude/skills/ (SPEC §8.4)."""

from __future__ import annotations

from pathlib import Path

from mimir.skill_defs import _bundled_skill_names, seed_skills


def test_bundled_skills_include_expected_set():
    names = set(_bundled_skill_names())
    # Verbatim ports + the rewritten/adapted ones.
    expected = {
        "five-whys",
        "introspection",
        "long-running-jobs",
        "memory",
        "onboarding",
        "pollers",
        "skill-acquisition",
        "skill-creator",
        "view-attachment",
    }
    missing = expected - names
    assert not missing, f"missing bundled skills: {missing}"


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
    assert "msam_store" in body or "MSAM" in body
