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


# ─── Follow-ups from issue #226 ─────────────────────────────────────


def test_list_available_and_installed_share_helper(fake_optional_root: Path, tmp_path):
    """Both listing paths funnel through ``_walk_skills_dir`` — sanity
    check by giving each the same input and asserting they produce
    equivalent output (same shape, same content, modulo the path field
    which differs by definition)."""
    # Build an agent home that has the same two skills installed.
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    import shutil as _sh
    _sh.copytree(fake_optional_root / "fake-poller", skills / "fake-poller")
    _sh.copytree(fake_optional_root / "fake-skill", skills / "fake-skill")

    avail = list_available(fake_optional_root)
    installed = list_installed(home)
    # Same names, descriptions, poller-flags.
    assert [s.name for s in avail] == [s.name for s in installed]
    assert [s.description for s in avail] == [s.description for s in installed]
    assert [s.has_pollers_json for s in avail] == [s.has_pollers_json for s in installed]


def test_cmd_list_optional_pip_install_hint(tmp_path: Path, capsys, monkeypatch):
    """When the optional-skills tree doesn't exist (wheel install
    case), ``list-optional`` must emit a clear pointer to a source-tree
    install — NOT the silent "no optional skills available" footgun.
    """
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT",
        tmp_path / "does-not-exist",
    )
    rc = cmd_list_optional(Namespace())
    assert rc == 2
    out = capsys.readouterr().out
    assert "optional-skills/ not found" in out
    assert "git clone" in out  # the actionable hint


def test_install_raises_pip_install_hint_when_tree_missing(tmp_path: Path):
    """``install()`` raises FileNotFoundError with the pip-install
    hint, not the per-skill "skill not found" message, when the
    optional-skills tree doesn't exist at all."""
    with pytest.raises(FileNotFoundError) as ei:
        install(
            "anything", tmp_path / "home",
            optional_skills_root=tmp_path / "does-not-exist",
        )
    assert "optional-skills/ not found" in str(ei.value)
    assert "git clone" in str(ei.value)


def test_poller_flag_alignment_in_listings(fake_optional_root: Path, capsys, monkeypatch):
    """Issue #226 nit 3: rows with and without the [poller] flag must
    keep the description column vertically aligned. The pre-fix
    f"{name:<width}{flag}" appended flag AFTER name padding, shifting
    description right on every poller row.
    """
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT",
        fake_optional_root,
    )
    cmd_list_optional(Namespace())
    out = capsys.readouterr().out
    # Find the two skill rows and verify their description offset
    # within the row is identical.
    poller_line = next(l for l in out.splitlines() if "fake-poller" in l)
    skill_line = next(l for l in out.splitlines() if "fake-skill" in l)
    # The description text starts at the same column index in both.
    poller_desc_idx = poller_line.find("A test poller")
    skill_desc_idx = skill_line.find("A plain non-poller")
    assert poller_desc_idx == skill_desc_idx, (
        f"poller row desc at col {poller_desc_idx}, "
        f"non-poller row desc at col {skill_desc_idx} — not aligned"
    )
    # Both lines should be present.
    assert poller_desc_idx > 0
    assert skill_desc_idx > 0


def test_truncate_desc_helper():
    """The truncation logic is centralized (issue #226 nit 4). Spot-check
    boundary behavior."""
    from mimir.skill_install import _truncate_desc, _DESC_BUDGET
    # Shorter than budget: untouched.
    assert _truncate_desc("hello") == "hello"
    # Exactly at budget: untouched.
    s = "x" * _DESC_BUDGET
    assert _truncate_desc(s) == s
    # Over budget: truncates with "..." suffix, total length = budget.
    s = "x" * (_DESC_BUDGET + 50)
    truncated = _truncate_desc(s)
    assert len(truncated) == _DESC_BUDGET
    assert truncated.endswith("...")
    # Custom budget.
    assert _truncate_desc("abcdefghij", budget=5) == "ab..."


# ─── Test gaps Mimir flagged on #224 ─────────────────────────────────


def test_cmd_list_no_skills_dir_emits_setup_hint(tmp_path: Path, capsys):
    """``mimir skills list`` on a home that exists but has no
    .claude/skills/ subdir should emit the "Did you run mimir setup?"
    hint, not crash."""
    home = tmp_path / "home"
    home.mkdir()
    rc = cmd_list(Namespace(home=home))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no skills installed" in out
    assert "mimir setup" in out


def test_cmd_install_returns_2_for_missing_home_dir(tmp_path: Path, capsys):
    """The if-not-home.is_dir() early-exit in ``cmd_install`` — distinct
    from the inner ``install()`` FileNotFoundError path. Both should
    return exit code 2 but via different code paths."""
    args = Namespace(
        name="anything",
        home=tmp_path / "does-not-exist-as-dir",
        force=False,
    )
    rc = cmd_install(args)
    assert rc == 2
    assert "home not a directory" in capsys.readouterr().out


def test_cmd_install_with_force_via_cli(
    fake_optional_root: Path, tmp_path, capsys, monkeypatch,
):
    """The --force semantics are tested at the library level (in
    test_install_force_overwrites_existing) but not via the CLI path.
    This covers the args.force=True branch of cmd_install."""
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT",
        fake_optional_root,
    )
    home = tmp_path / "home"
    home.mkdir()
    # First install.
    args1 = Namespace(name="fake-skill", home=home, force=False)
    assert cmd_install(args1) == 0
    # Second without force → conflict (exit 3).
    assert cmd_install(args1) == 3
    # Second with force → success.
    args2 = Namespace(name="fake-skill", home=home, force=True)
    out_before = capsys.readouterr().out  # consume prior stdout
    assert cmd_install(args2) == 0
    out = capsys.readouterr().out
    assert "overwrote" in out


def test_cmd_install_emits_poller_hint_for_pollers_json(
    fake_optional_root: Path, tmp_path, capsys, monkeypatch,
):
    """Pollers-hint output line ("this skill ships a pollers.json...")
    must fire for any skill whose source has a pollers.json. Was not
    asserted in any test pre-#226."""
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT",
        fake_optional_root,
    )
    home = tmp_path / "home"
    home.mkdir()
    rc = cmd_install(Namespace(
        name="fake-poller", home=home, force=False,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ships a pollers.json" in out
    assert "reload_pollers" in out


def test_cmd_install_no_poller_hint_for_plain_skill(
    fake_optional_root: Path, tmp_path, capsys, monkeypatch,
):
    """Complement to the prior test: a non-poller skill (no
    pollers.json) must NOT emit the pollers hint."""
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT",
        fake_optional_root,
    )
    home = tmp_path / "home"
    home.mkdir()
    rc = cmd_install(Namespace(
        name="fake-skill", home=home, force=False,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ships a pollers.json" not in out
