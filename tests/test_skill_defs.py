"""Skill seeding into <home>/.mimir_builtin_skills/ (SPEC §8.4)."""

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
    body = (tmp_path / ".mimir_builtin_skills" / "memory" / "SKILL.md").read_text()
    assert "state/wiki" in body or "wiki skill" in body.lower(), (
        "memory skill should point at the wiki layer for graph-shaped content"
    )


def test_refresh_creates_builtin_skills(tmp_path: Path):
    """Bundled skills land under ``<home>/.mimir_builtin_skills/``
    on first refresh. Status is ``"refreshed"`` (always overwrite,
    even on a fresh install)."""
    out = seed_skills(tmp_path)
    target = tmp_path / ".mimir_builtin_skills"
    assert target.is_dir()
    for name, status in out.items():
        assert status == "refreshed", f"{name}: {status}"
        assert (target / name / "SKILL.md").is_file()


def test_refresh_overwrites_existing_content(tmp_path: Path):
    """Unlike the pre-2026-05-22 seed_skills, refresh ALWAYS overwrites.
    The bundle is read-only — there's no user-customization path in
    place. Operator customization happens by installing under
    ``<home>/skills/<name>/``, not by editing the bundle."""
    target = tmp_path / ".mimir_builtin_skills" / "memory"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# stale content from before refresh\n")

    out = seed_skills(tmp_path)
    assert out["memory"] == "refreshed"
    # Content was replaced with the canonical bundle.
    body = (target / "SKILL.md").read_text()
    assert "stale content from before refresh" not in body


def test_memory_skill_no_brand_leaks(tmp_path: Path):
    seed_skills(tmp_path)
    body = (tmp_path / ".mimir_builtin_skills" / "memory" / "SKILL.md").read_text()
    # Adapted from open-strix-base; brand references should be gone.
    assert "open_strix_builtin" not in body
    assert "open-strix" not in body
    # Mimir-specific surface should be present.
    assert "memory/core/" in body
    assert "saga_store" in body or "SAGA" in body or "memory_store" in body


def test_refresh_recovers_poisoned_destination(tmp_path: Path):
    """A pre-existing skill folder missing SKILL.md (a half-copy from a
    crashed prior run) gets fully overwritten by the refresh. The
    half-copy's stray files don't survive — refresh is rmtree-then-copy."""
    target = tmp_path / ".mimir_builtin_skills" / "memory"
    target.mkdir(parents=True)
    (target / "stray.md").write_text("partial")

    out = seed_skills(tmp_path)
    assert out["memory"] == "refreshed"
    assert (target / "SKILL.md").is_file()
    assert not (target / "stray.md").exists(), "poisoned remnants should be gone"


def test_seed_skills_cleans_up_tmp_from_prior_crash(tmp_path: Path):
    """A leftover ``<name>.tmp`` from a crashed prior copy is wiped before
    the next attempt."""
    leftover = tmp_path / ".mimir_builtin_skills" / "memory.tmp"
    leftover.mkdir(parents=True)
    (leftover / "garbage.md").write_text("from a dead process")

    seed_skills(tmp_path)
    # The .tmp dir was either renamed into place (success) or wiped (error).
    # Either way it shouldn't survive as ``<name>.tmp``.
    assert not leftover.exists()
    assert (tmp_path / ".mimir_builtin_skills" / "memory" / "SKILL.md").is_file()


def test_installed_skill_names_includes_user_added_skills(tmp_path: Path):
    """§12.4 review #11: skills the user adds under <home>/.mimir_builtin_skills/
    should appear in the ranker's input alongside bundled skills."""
    seed_skills(tmp_path)
    user_skill = tmp_path / ".mimir_builtin_skills" / "my-custom-skill"
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
    assert "onboarding" in names


def test_installed_skill_names_skips_dirs_without_skill_md(tmp_path: Path):
    """A directory without a SKILL.md is not a valid skill — skip it."""
    skills = tmp_path / ".mimir_builtin_skills"
    skills.mkdir(parents=True)
    (skills / "broken").mkdir()  # no SKILL.md
    names = installed_skill_names(tmp_path)
    assert "broken" not in names
