"""Tests for ``mimir.skill_install`` and its ``mimir skills install /
list / list-optional`` CLI wiring.

Covers the install flow (copy from optional-skills/ → home/.claude/skills/),
the conflict + --force semantics, the listing helpers, and the
CLI-side argparse callbacks (so `mimir skills install bogus` returns
a non-zero exit cleanly).
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from mimir.skill_install import (
    DEFAULT_OPTIONAL_SKILLS_ROOT,
    OptionalSkill,
    cmd_install,
    cmd_list,
    cmd_list_optional,
    install,
    list_available,
    list_installed,
)


# ─── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def fake_optional_root(tmp_path: Path) -> Path:
    """Build a tiny ``optional-skills/`` analogue with two opt-in
    skills — one with a pollers.json, one without."""
    root = tmp_path / "optional-skills"

    poller = root / "fake-poller"
    poller.mkdir(parents=True)
    (poller / "SKILL.md").write_text(
        "---\nname: fake-poller\n"
        "description: A test poller that does nothing.\n"
        "---\n# fake-poller\nbody\n"
    )
    (poller / "pollers.json").write_text(
        '{"pollers": [{"name": "fake", "command": "true", "cron": "*/5 * * * *"}]}'
    )
    (poller / "poller.py").write_text("# placeholder\n")

    plain = root / "fake-skill"
    plain.mkdir()
    (plain / "SKILL.md").write_text(
        "---\nname: fake-skill\n"
        "description: A plain non-poller skill.\n"
        "---\n# fake-skill\nbody\n"
    )

    # Add a directory with no SKILL.md — must be skipped.
    junk = root / "no-skill-md"
    junk.mkdir()
    (junk / "README.md").write_text("not a skill")

    return root


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


# ─── list_available ──────────────────────────────────────────────────


def test_list_available_skips_non_skill_dirs(fake_optional_root: Path):
    skills = list_available(fake_optional_root)
    names = {s.name for s in skills}
    assert names == {"fake-poller", "fake-skill"}
    assert "no-skill-md" not in names


def test_list_available_marks_poller_skills(fake_optional_root: Path):
    by_name = {s.name: s for s in list_available(fake_optional_root)}
    assert by_name["fake-poller"].has_pollers_json is True
    assert by_name["fake-skill"].has_pollers_json is False


def test_list_available_reads_description(fake_optional_root: Path):
    by_name = {s.name: s for s in list_available(fake_optional_root)}
    assert by_name["fake-poller"].description == "A test poller that does nothing."
    assert by_name["fake-skill"].description == "A plain non-poller skill."


def test_list_available_empty_root(tmp_path: Path):
    assert list_available(tmp_path / "does-not-exist") == []


# ─── install ─────────────────────────────────────────────────────────


def test_install_copies_directory(fake_optional_root: Path, fake_home: Path):
    result = install(
        "fake-poller", fake_home,
        optional_skills_root=fake_optional_root,
    )
    dest = fake_home / ".claude" / "skills" / "fake-poller"
    assert dest.is_dir()
    assert (dest / "SKILL.md").is_file()
    assert (dest / "pollers.json").is_file()
    assert (dest / "poller.py").is_file()
    assert result.name == "fake-poller"
    assert result.dest == dest
    assert result.overwrote is False
    assert result.pollers_registered_hint is True


def test_install_pollers_hint_false_for_non_poller(
    fake_optional_root: Path, fake_home: Path,
):
    result = install(
        "fake-skill", fake_home,
        optional_skills_root=fake_optional_root,
    )
    assert result.pollers_registered_hint is False


def test_install_missing_skill_raises_filenotfound(
    fake_optional_root: Path, fake_home: Path,
):
    with pytest.raises(FileNotFoundError):
        install(
            "does-not-exist", fake_home,
            optional_skills_root=fake_optional_root,
        )


def test_install_existing_dest_raises_without_force(
    fake_optional_root: Path, fake_home: Path,
):
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    with pytest.raises(FileExistsError):
        install(
            "fake-skill", fake_home,
            optional_skills_root=fake_optional_root,
        )


def test_install_force_overwrites_existing(
    fake_optional_root: Path, fake_home: Path,
):
    # First install. Add a marker file inside the dest to verify it gets cleared.
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    dest = fake_home / ".claude" / "skills" / "fake-skill"
    (dest / "user-edit.md").write_text("custom content")
    assert (dest / "user-edit.md").is_file()

    # Second install with --force.
    result = install(
        "fake-skill", fake_home,
        force=True, optional_skills_root=fake_optional_root,
    )
    assert result.overwrote is True
    # User's edit should be gone — that's the documented behavior.
    assert not (dest / "user-edit.md").exists()
    # Original SKILL.md should still be present.
    assert (dest / "SKILL.md").is_file()


def test_install_excludes_pycache(fake_optional_root: Path, fake_home: Path):
    # Add a __pycache__ to the source — verify it doesn't end up at dest.
    pycache = fake_optional_root / "fake-poller" / "__pycache__"
    pycache.mkdir()
    (pycache / "poller.cpython-313.pyc").write_text("binary stuff")

    install("fake-poller", fake_home, optional_skills_root=fake_optional_root)
    dest = fake_home / ".claude" / "skills" / "fake-poller"
    assert not (dest / "__pycache__").exists()


# ─── list_installed ──────────────────────────────────────────────────


def test_list_installed_empty_home(fake_home: Path):
    assert list_installed(fake_home) == []


def test_list_installed_after_install(
    fake_optional_root: Path, fake_home: Path,
):
    install("fake-poller", fake_home, optional_skills_root=fake_optional_root)
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    installed = list_installed(fake_home)
    names = {s.name for s in installed}
    assert names == {"fake-poller", "fake-skill"}
    by_name = {s.name: s for s in installed}
    assert by_name["fake-poller"].has_pollers_json is True
    assert by_name["fake-skill"].has_pollers_json is False


# ─── CLI cmd_install / cmd_list / cmd_list_optional ──────────────────


def test_cmd_install_returns_2_for_missing_skill(
    fake_home: Path, capsys, monkeypatch,
):
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT",
        Path("/nonexistent-optional-skills"),
    )
    args = Namespace(name="missing", home=fake_home, force=False)
    rc = cmd_install(args)
    assert rc == 2
    assert "not found" in capsys.readouterr().out


def test_cmd_install_returns_3_for_conflict(
    fake_optional_root: Path, fake_home: Path, capsys, monkeypatch,
):
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT",
        fake_optional_root,
    )
    args = Namespace(name="fake-skill", home=fake_home, force=False)
    assert cmd_install(args) == 0
    rc = cmd_install(args)  # second time → conflict
    assert rc == 3
    assert "already exists" in capsys.readouterr().out


def test_cmd_list_walks_home(
    fake_optional_root: Path, fake_home: Path, capsys, monkeypatch,
):
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT",
        fake_optional_root,
    )
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    args = Namespace(home=fake_home)
    rc = cmd_list(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "fake-skill" in out
    assert "n=1" in out


def test_cmd_list_optional_walks_optional_skills(
    fake_optional_root: Path, capsys, monkeypatch,
):
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT",
        fake_optional_root,
    )
    args = Namespace()  # list-optional doesn't take args today
    rc = cmd_list_optional(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "fake-poller" in out
    assert "fake-skill" in out
    assert "[poller]" in out
