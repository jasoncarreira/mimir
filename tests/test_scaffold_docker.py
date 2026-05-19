"""Tests for ``mimir.scaffold_docker`` and the ``mimir scaffold-docker``
CLI.

Covers:
- fragment collection (home wins; bundled fallback for ungraded homes)
- pollers.json env-var collection
- compose.env idempotency (commented placeholders count as 'present',
  operator values survive re-runs, new pollers append only missing keys)
- the Dockerfile sentinel-block + skill fragment ordering
- end-to-end ``scaffold(home)`` orchestration on a fresh + populated home
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.scaffold_docker import (
    Fragment,
    collect_fragments,
    collect_required_env_vars,
    existing_env_keys,
    render_compose_env,
    render_dockerfile,
    scaffold,
)


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def home_with_two_skills(tmp_path: Path) -> Path:
    """Mimir home with two installed skills, one with a fragment, one
    without. Sufficient for fragment + env-var collection tests."""
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)

    # Skill A: has dockerfile.fragment + pollers.json
    a = skills / "skill-a"
    a.mkdir()
    (a / "SKILL.md").write_text("---\nname: skill-a\ndescription: A\n---\nbody")
    (a / "dockerfile.fragment").write_text("RUN echo skill-a\n")
    (a / "pollers.json").write_text(
        '{"pollers": [{"name": "a", "command": "true", "pass_env": ["FOO", "BAR"]}]}'
    )

    # Skill B: no fragment, no pollers.json
    b = skills / "skill-b"
    b.mkdir()
    (b / "SKILL.md").write_text("---\nname: skill-b\ndescription: B\n---\nbody")

    return home


# ── collect_fragments ───────────────────────────────────────────────


def test_collect_fragments_picks_up_installed_fragment(home_with_two_skills: Path):
    frags = collect_fragments(home_with_two_skills)
    assert len(frags) == 1
    assert frags[0].skill_name == "skill-a"
    assert frags[0].content == "RUN echo skill-a"


def test_collect_fragments_falls_back_to_bundled(tmp_path: Path, monkeypatch):
    """When a skill is present in the home but lacks a dockerfile.fragment
    locally, the scaffolder must look in the bundled source (mimir/skills/
    or optional-skills/) as a backstop for homes seeded before fragments
    existed in the bundle.
    """
    # Fake bundled root with a fragment for ``mimir/skills/skill-c``.
    bundled = tmp_path / "bundled"
    (bundled / "skill-c").mkdir(parents=True)
    (bundled / "skill-c" / "dockerfile.fragment").write_text("RUN bundled-c")

    # Home with skill-c installed but NO fragment of its own.
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    (skills / "skill-c").mkdir(parents=True)
    (skills / "skill-c" / "SKILL.md").write_text(
        "---\nname: skill-c\ndescription: C\n---\nbody"
    )

    monkeypatch.setattr(
        "mimir.scaffold_docker._BUNDLED_SKILL_ROOTS",
        (bundled,),
    )
    frags = collect_fragments(home)
    assert len(frags) == 1
    assert frags[0].skill_name == "skill-c"
    assert frags[0].content == "RUN bundled-c"


def test_collect_fragments_empty_home(tmp_path: Path):
    assert collect_fragments(tmp_path / "no-such-home") == []


def test_collect_fragments_ordered_by_skill_name(tmp_path: Path):
    """Stable Dockerfile output across runs: fragments come out
    alphabetically by skill name."""
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    for name in ("z-skill", "a-skill", "m-skill"):
        d = skills / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody")
        (d / "dockerfile.fragment").write_text(f"RUN {name}\n")
    frags = collect_fragments(home)
    assert [f.skill_name for f in frags] == ["a-skill", "m-skill", "z-skill"]


# ── collect_required_env_vars ───────────────────────────────────────


def test_collect_required_env_vars_includes_baseline(home_with_two_skills: Path):
    keys = collect_required_env_vars(home_with_two_skills)
    assert "MIMIR_API_KEY" in keys
    assert "VOYAGE_API_KEY" in keys
    assert "GITHUB_TOKEN" in keys


def test_collect_required_env_vars_appends_poller_pass_env(
    home_with_two_skills: Path,
):
    keys = collect_required_env_vars(home_with_two_skills)
    # skill-a's pollers.json adds FOO + BAR
    assert "FOO" in keys
    assert "BAR" in keys


def test_collect_required_env_vars_dedupes(tmp_path: Path):
    """If two pollers both declare the same pass_env entry, it appears
    only once in the result."""
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    for name in ("a", "b"):
        d = skills / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
        (d / "pollers.json").write_text(
            '{"pollers": [{"name": "' + name + '", "command": "true", '
            '"pass_env": ["SHARED_KEY"]}]}'
        )
    keys = collect_required_env_vars(home)
    assert keys.count("SHARED_KEY") == 1


# ── existing_env_keys ───────────────────────────────────────────────


def test_existing_env_keys_picks_up_live_settings():
    keys = existing_env_keys("FOO=value\nBAR=other\n")
    assert keys == {"FOO", "BAR"}


def test_existing_env_keys_picks_up_commented_placeholders():
    """Commented placeholders (`# KEY=`) MUST count as 'key already
    present'. Otherwise scaffold-docker would re-append every placeholder
    on every run."""
    keys = existing_env_keys("# FOO=\n# BAR=\nLIVE=val\n")
    assert keys == {"FOO", "BAR", "LIVE"}


def test_existing_env_keys_ignores_random_comments():
    keys = existing_env_keys("# This is a header\n# Another comment\nFOO=v\n")
    assert keys == {"FOO"}


# ── render_compose_env idempotency ─────────────────────────────────


def test_render_compose_env_fresh_emits_all_keys():
    text, added = render_compose_env(None, ["FOO", "BAR"])
    assert "# FOO=" in text
    assert "# BAR=" in text
    assert set(added) == {"FOO", "BAR"}


def test_render_compose_env_preserves_operator_values():
    existing = "FOO=secret_value\n# BAR=\n"
    text, added = render_compose_env(existing, ["FOO", "BAR", "BAZ"])
    assert "FOO=secret_value" in text
    assert "# BAR=" in text  # was already there, untouched
    assert "BAZ" in text  # newly appended
    assert added == ["BAZ"]


def test_render_compose_env_no_op_when_all_present():
    """All keys already in the file (live OR commented) → return
    unchanged + empty added list."""
    existing = "FOO=v\n# BAR=\n"
    text, added = render_compose_env(existing, ["FOO", "BAR"])
    assert text == existing
    assert added == []


def test_render_compose_env_idempotent_on_re_run():
    """Running render_compose_env back-to-back on its own output must
    not keep appending. Tests the full round-trip."""
    text1, _ = render_compose_env(None, ["FOO", "BAR"])
    text2, added2 = render_compose_env(text1, ["FOO", "BAR"])
    assert added2 == []
    assert text1 == text2


# ── render_dockerfile ──────────────────────────────────────────────


def test_render_dockerfile_inserts_fragments():
    frags = [
        Fragment(skill_name="a", content="RUN echo a"),
        Fragment(skill_name="b", content="RUN echo b"),
    ]
    out = render_dockerfile(frags)
    assert "RUN echo a" in out
    assert "RUN echo b" in out
    assert "BEGIN mimir-scaffold-docker: skill fragments" in out
    assert "END mimir-scaffold-docker: skill fragments" in out
    # Fragments labeled with their skill names so the generated file is readable.
    assert "# --- a ---" in out
    assert "# --- b ---" in out


def test_render_dockerfile_empty_fragments():
    out = render_dockerfile([])
    assert "no skills installed yet ship a dockerfile.fragment" in out
    assert "BEGIN mimir-scaffold-docker" in out


def test_render_dockerfile_has_base_layer():
    """Sanity: regardless of fragments, the base image + tooling are
    present (git, gh, uv, claude-code, mermaid)."""
    out = render_dockerfile([])
    assert "FROM python:3.11-slim" in out
    assert "@anthropic-ai/claude-code" in out
    assert "astral.sh/uv/install.sh" in out


# ── scaffold() end-to-end ──────────────────────────────────────────


def test_scaffold_writes_all_four_files(home_with_two_skills: Path):
    result = scaffold(home_with_two_skills)
    for fname in ("Dockerfile", "compose.yml", "compose.env", "start.sh"):
        assert (home_with_two_skills / fname).is_file(), \
            f"{fname} not written"
    assert "Dockerfile" in result.files_written
    assert "compose.yml" in result.files_written
    assert "start.sh" in result.files_written


def test_scaffold_start_sh_is_executable(home_with_two_skills: Path):
    scaffold(home_with_two_skills)
    import stat
    mode = (home_with_two_skills / "start.sh").stat().st_mode
    assert mode & stat.S_IXUSR, "start.sh must be executable"


def test_scaffold_picks_up_skills(home_with_two_skills: Path):
    result = scaffold(home_with_two_skills)
    assert result.skills_with_fragments == ["skill-a"]


def test_scaffold_idempotent_compose_env(home_with_two_skills: Path):
    """Re-running scaffold-docker on the same home must NOT append
    duplicate env-var placeholders."""
    result1 = scaffold(home_with_two_skills)
    assert len(result1.env_vars_added) > 0

    env_file = home_with_two_skills / "compose.env"
    text_after_first = env_file.read_text()

    result2 = scaffold(home_with_two_skills)
    text_after_second = env_file.read_text()

    assert text_after_first == text_after_second
    assert result2.env_vars_added == []
    assert "compose.env (no changes)" in result2.files_skipped


def test_scaffold_picks_up_new_skill_after_install(
    home_with_two_skills: Path,
):
    """Adding a new skill (with a fragment + pass_env) and re-running
    scaffold-docker must add ITS fragment to the Dockerfile and ITS
    env vars to compose.env — without touching what's already there.
    """
    home = home_with_two_skills
    result1 = scaffold(home)
    df_before = (home / "Dockerfile").read_text()
    env_before = (home / "compose.env").read_text()

    # Operator sets a value in compose.env.
    env_text = env_before.replace("# FOO=", "FOO=operator_value")
    (home / "compose.env").write_text(env_text)

    # Install a new skill with its own fragment + pollers.json.
    new = home / ".claude" / "skills" / "skill-new"
    new.mkdir()
    (new / "SKILL.md").write_text("---\nname: skill-new\n---\n")
    (new / "dockerfile.fragment").write_text("RUN echo skill-new")
    (new / "pollers.json").write_text(
        '{"pollers": [{"name": "n", "command": "true", "pass_env": ["NEW_KEY"]}]}'
    )

    result2 = scaffold(home)
    df_after = (home / "Dockerfile").read_text()
    env_after = (home / "compose.env").read_text()

    # Dockerfile picks up the new fragment.
    assert "skill-new" in result2.skills_with_fragments
    assert "RUN echo skill-new" in df_after
    # Original fragment also still there.
    assert "RUN echo skill-a" in df_after
    # compose.env preserves the operator's value AND appends only the new key.
    assert "FOO=operator_value" in env_after
    assert "# NEW_KEY=" in env_after
    assert result2.env_vars_added == ["NEW_KEY"]


def test_scaffold_missing_home_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        scaffold(tmp_path / "does-not-exist")
