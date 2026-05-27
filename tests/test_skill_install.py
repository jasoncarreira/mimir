"""Tests for ``mimir.skill_install`` and its ``mimir skills install /
list / list-optional / update`` CLI wiring.

Covers the install flow (copy from optional-skills/ → home/skills/),
the conflict + --force semantics, the listing helpers, the
CLI-side argparse callbacks (so `mimir skills install bogus` returns
a non-zero exit cleanly), and the drift-detection logic
(``detect_skill_drift`` + ``mimir skills update``).
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from mimir.skill_install import (
    DEFAULT_OPTIONAL_SKILLS_ROOT,
    OptionalSkill,
    SkillDriftResult,
    SkillEnvSpec,
    apply_skill_update,
    cmd_install,
    cmd_list,
    cmd_list_optional,
    cmd_update_skills,
    detect_skill_drift,
    install,
    list_available,
    list_installed,
    prompt_and_write_env,
    read_env_specs,
    run_smoke_test,
)
from mimir.skill_md import parse_env_block


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
    dest = fake_home / "skills" / "fake-poller"
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
    dest = fake_home / "skills" / "fake-skill"
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
    dest = fake_home / "skills" / "fake-poller"
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
    skills = home / "skills"
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
    skills/ subdir should emit the "Did you run mimir setup?"
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


# ─── detect_skill_drift ───────────────────────────────────────────────


def test_drift_clean_skill(fake_optional_root: Path, fake_home: Path):
    """An installed skill whose files are identical to source is clean."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")
    assert r.is_clean
    assert r.differs == []
    assert r.added == []
    assert r.extra == []
    assert not r.orphaned


def test_drift_differs_file(fake_optional_root: Path, fake_home: Path):
    """A file that differs between source and installed shows up in .differs."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    # Mutate the installed copy so it differs from source.
    installed_skill_md = fake_home / "skills" / "fake-skill" / "SKILL.md"
    installed_skill_md.write_text("---\nname: fake-skill\ndescription: edited\n---\n")

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")
    assert not r.is_clean
    assert "SKILL.md" in r.differs
    assert r.added == []
    assert r.extra == []


def test_drift_added_file_in_source(fake_optional_root: Path, fake_home: Path):
    """A file added to source after install shows up as added-in-source."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    # Simulate a new file arriving in source that isn't in installed yet.
    (fake_optional_root / "fake-skill" / "new-helper.py").write_text("# new\n")

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")
    assert not r.is_clean
    assert "new-helper.py" in r.added
    assert r.differs == []
    assert r.extra == []


def test_drift_extra_file_in_installed(fake_optional_root: Path, fake_home: Path):
    """A file present in installed but absent from source shows up as extra."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    # Add a file only in the installed copy (a local edit / leftover).
    (fake_home / "skills" / "fake-skill" / "local-note.md").write_text("notes\n")

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")
    assert not r.is_clean
    assert "local-note.md" in r.extra
    assert r.differs == []
    assert r.added == []


def test_drift_orphaned_skill(fake_home: Path, tmp_path: Path):
    """An installed skill with no matching source directory is orphaned."""
    # Create a skill in home that has no source counterpart.
    orphan = fake_home / "skills" / "orphan-skill"
    orphan.mkdir(parents=True)
    (orphan / "SKILL.md").write_text("---\nname: orphan-skill\ndescription: x\n---\n")

    # Empty optional-skills root — no counterpart for orphan-skill.
    empty_src = tmp_path / "empty-optional-skills"
    empty_src.mkdir()

    results = detect_skill_drift(fake_home, empty_src)
    assert len(results) == 1
    r = results[0]
    assert r.name == "orphan-skill"
    assert r.orphaned
    assert not r.is_clean
    assert r.source_path is None


def test_drift_by_name_not_installed_raises(
    fake_optional_root: Path, fake_home: Path,
):
    """detect_skill_drift(name=...) raises FileNotFoundError for unknown skills."""
    with pytest.raises(FileNotFoundError, match="not installed"):
        detect_skill_drift(fake_home, fake_optional_root, name="not-there")


def test_drift_pycache_excluded(fake_optional_root: Path, fake_home: Path):
    """__pycache__ files do not contribute to drift even when they differ."""
    install("fake-poller", fake_home, optional_skills_root=fake_optional_root)
    # Add __pycache__ to both sides with different content — should not appear.
    src_cache = fake_optional_root / "fake-poller" / "__pycache__"
    src_cache.mkdir(exist_ok=True)
    (src_cache / "poller.pyc").write_text("source-bytecode")

    inst_cache = fake_home / "skills" / "fake-poller" / "__pycache__"
    inst_cache.mkdir(exist_ok=True)
    (inst_cache / "poller.pyc").write_text("installed-bytecode-different")

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-poller")
    assert r.is_clean, f"Expected clean but got: {r}"


# ─── cmd_update_skills CLI ────────────────────────────────────────────


def test_cmd_update_skills_all_clean(
    fake_optional_root: Path, fake_home: Path, capsys, monkeypatch,
):
    """``mimir skills update`` exits 0 and prints 'up to date' when clean."""
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT", fake_optional_root,
    )
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    rc = cmd_update_skills(Namespace(
        home=fake_home, name=None, optional_skills_root=fake_optional_root,
        apply=False, force=False,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "up to date" in out


def test_cmd_update_skills_drift_exits_1(
    fake_optional_root: Path, fake_home: Path, capsys, monkeypatch,
):
    """``mimir skills update`` exits 1 and reports file names when drift found."""
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT", fake_optional_root,
    )
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    # Introduce drift in the source (simulating an upstream update).
    (fake_optional_root / "fake-skill" / "SKILL.md").write_text(
        "---\nname: fake-skill\ndescription: Updated description.\n---\n"
    )
    rc = cmd_update_skills(Namespace(
        home=fake_home, name=None, optional_skills_root=fake_optional_root,
        apply=False, force=False,
    ))
    assert rc == 1
    out = capsys.readouterr().out
    assert "differs from source" in out
    assert "SKILL.md" in out
    assert "1 skill" in out


def test_cmd_update_skills_no_home(tmp_path: Path, capsys):
    """``mimir skills update`` with a non-existent home exits 2."""
    rc = cmd_update_skills(Namespace(
        home=tmp_path / "no-such-home",
        name=None,
        optional_skills_root=None,
        apply=False,
        force=False,
    ))
    assert rc == 2
    assert "not a directory" in capsys.readouterr().out


# ─── apply_skill_update / cmd_update_skills --apply ──────────────────


def test_apply_overwrites_changed_file(
    fake_optional_root: Path, fake_home: Path, capsys,
):
    """--apply overwrites a file that differs from source."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    # Mutate the installed copy.
    installed_md = fake_home / "skills" / "fake-skill" / "SKILL.md"
    installed_md.write_text("---\nname: fake-skill\ndescription: stale\n---\n")

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")
    assert "SKILL.md" in r.differs

    updated, hint = apply_skill_update(r)

    assert "SKILL.md" in updated
    assert hint is None  # no pollers.json in fake-skill
    # File should now match the source.
    src_text = (fake_optional_root / "fake-skill" / "SKILL.md").read_text()
    assert installed_md.read_text() == src_text


def test_apply_differs_file_creates_backup(
    fake_optional_root: Path, fake_home: Path, capsys,
):
    """--apply does NOT silently overwrite a hand-edited differs file.

    Before overwriting, a backup must be written to
    ``.pre-update-backup/<timestamp>/`` inside the installed skill
    directory, and a warning must be printed.
    """
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    installed_md = fake_home / "skills" / "fake-skill" / "SKILL.md"
    # Hand-edit the installed file so it shows up in .differs.
    installed_md.write_text("---\nname: fake-skill\ndescription: hand-edited\n---\n")

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")
    assert "SKILL.md" in r.differs

    updated, _ = apply_skill_update(r)

    assert "SKILL.md" in updated

    # A backup directory must have been created under .pre-update-backup/.
    backup_parent = fake_home / "skills" / "fake-skill" / ".pre-update-backup"
    assert backup_parent.is_dir(), ".pre-update-backup/ directory was not created"

    # Find the timestamped backup sub-directory.
    ts_dirs = list(backup_parent.iterdir())
    assert len(ts_dirs) == 1, f"Expected exactly one timestamp dir, found: {ts_dirs}"
    backup_file = ts_dirs[0] / "SKILL.md"
    assert backup_file.is_file(), f"Backup file not found at {backup_file}"

    # Backup must contain the pre-update (hand-edited) content, not source.
    assert backup_file.read_text() == "---\nname: fake-skill\ndescription: hand-edited\n---\n"

    # Warning must have been printed.
    out = capsys.readouterr().out
    assert "Warning" in out
    assert "SKILL.md" in out
    assert "backed up" in out

    # The installed file must now match source (update applied).
    src_text = (fake_optional_root / "fake-skill" / "SKILL.md").read_text()
    assert installed_md.read_text() == src_text


def test_apply_added_file_does_not_create_backup(
    fake_optional_root: Path, fake_home: Path, capsys,
):
    """Files that are newly added in source (not in .differs) should not
    trigger a backup — there is no pre-existing installed file to back up."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    # Add a new file to source only.
    (fake_optional_root / "fake-skill" / "new-helper.py").write_text("# new\n")

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")
    assert "new-helper.py" in r.added

    apply_skill_update(r)

    # No backup directory should exist for pure-add operations.
    backup_parent = fake_home / "skills" / "fake-skill" / ".pre-update-backup"
    assert not backup_parent.exists(), (
        "Backup directory should not be created for added-only files"
    )


def test_apply_copies_added_file(
    fake_optional_root: Path, fake_home: Path,
):
    """--apply copies a file that was added in source after install."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    # Add a new file to source.
    (fake_optional_root / "fake-skill" / "helper.py").write_text("# new\n")

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")
    assert "helper.py" in r.added

    updated, _ = apply_skill_update(r)

    assert "helper.py" in updated
    assert (fake_home / "skills" / "fake-skill" / "helper.py").read_text() == "# new\n"


def test_apply_skips_extra_without_force(
    fake_optional_root: Path, fake_home: Path, capsys,
):
    """--apply without --force preserves extra (local-only) files and warns."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    extra_file = fake_home / "skills" / "fake-skill" / "local-note.md"
    extra_file.write_text("my notes\n")

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")
    assert "local-note.md" in r.extra

    updated, _ = apply_skill_update(r, force=False)

    assert "local-note.md" not in updated
    assert extra_file.exists()  # preserved
    out = capsys.readouterr().out
    assert "local-note.md" in out
    assert "force" in out.lower()


def test_apply_removes_extra_with_force(
    fake_optional_root: Path, fake_home: Path,
):
    """--apply --force removes extra files in the installed copy."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    extra_file = fake_home / "skills" / "fake-skill" / "local-note.md"
    extra_file.write_text("my notes\n")

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")

    updated, _ = apply_skill_update(r, force=True)

    assert "local-note.md" in updated
    assert not extra_file.exists()  # removed


def test_apply_emits_pollers_hint(
    fake_optional_root: Path, fake_home: Path,
):
    """--apply emits the reload_pollers hint when pollers.json was updated."""
    install("fake-poller", fake_home, optional_skills_root=fake_optional_root)
    # Mutate the installed pollers.json.
    installed_pj = fake_home / "skills" / "fake-poller" / "pollers.json"
    installed_pj.write_text('{"pollers": []}')

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-poller")
    assert "pollers.json" in r.differs

    updated, hint = apply_skill_update(r)

    assert "pollers.json" in updated
    assert hint is not None
    assert "mimir scheduler reload" in hint


def test_apply_orphaned_skill_skipped(fake_home: Path, tmp_path: Path, capsys):
    """--apply skips orphaned skills (no source counterpart) with a warning."""
    orphan = fake_home / "skills" / "orphan-skill"
    orphan.mkdir(parents=True)
    (orphan / "SKILL.md").write_text("---\nname: orphan-skill\ndescription: x\n---\n")

    empty_src = tmp_path / "empty-optional-skills"
    empty_src.mkdir()

    results = detect_skill_drift(fake_home, empty_src)
    r = results[0]
    assert r.orphaned

    updated, hint = apply_skill_update(r)

    assert updated == []
    assert hint is None
    out = capsys.readouterr().out
    assert "orphaned" in out


def test_cmd_update_skills_apply_flag(
    fake_optional_root: Path, fake_home: Path, capsys, monkeypatch,
):
    """``mimir skills update --apply`` updates files and exits 0 on success."""
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT", fake_optional_root,
    )
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    # Introduce drift.
    (fake_optional_root / "fake-skill" / "SKILL.md").write_text(
        "---\nname: fake-skill\ndescription: Updated.\n---\n"
    )
    rc = cmd_update_skills(Namespace(
        home=fake_home,
        name=None,
        optional_skills_root=fake_optional_root,
        apply=True,
        force=False,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "updated" in out
    assert "SKILL.md" in out


def test_cmd_update_skills_apply_extra_exits_1(
    fake_optional_root: Path, fake_home: Path, capsys,
):
    """``mimir skills update --apply`` exits 1 when extra files were skipped."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    (fake_home / "skills" / "fake-skill" / "local-note.md").write_text("notes\n")

    rc = cmd_update_skills(Namespace(
        home=fake_home,
        name=None,
        optional_skills_root=fake_optional_root,
        apply=True,
        force=False,
    ))
    assert rc == 1  # extra file skipped — partial update


def test_cmd_update_skills_apply_pollers_hint_printed(
    fake_optional_root: Path, fake_home: Path, capsys,
):
    """``mimir skills update --apply`` prints the reload_pollers hint."""
    install("fake-poller", fake_home, optional_skills_root=fake_optional_root)
    (fake_home / "skills" / "fake-poller" / "pollers.json").write_text(
        '{"pollers": []}'
    )
    rc = cmd_update_skills(Namespace(
        home=fake_home,
        name=None,
        optional_skills_root=fake_optional_root,
        apply=True,
        force=False,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "mimir scheduler reload" in out


# ─── parse_env_block ─────────────────────────────────────────────────

_ENV_BLOCK_SKILL_MD = """\
---
name: test-skill
description: A skill with an env block. Note the colon: here.
env:
  required:
    - name: REQUIRED_VAR
      description: A required variable
      example: some-value
  optional:
    - name: OPTIONAL_VAR
      description: An optional variable
      example: opt-value
    - name: CONDITIONAL_VAR
      description: Only if OPTIONAL_VAR=yes
      example: cond-value
      only_if: "OPTIONAL_VAR=yes"
---
body
"""

_NO_ENV_SKILL_MD = """\
---
name: plain-skill
description: No env block here.
---
body
"""


def test_parse_env_block_returns_required_and_optional():
    req, opt = parse_env_block(_ENV_BLOCK_SKILL_MD)
    assert [r["name"] for r in req] == ["REQUIRED_VAR"]
    assert [o["name"] for o in opt] == ["OPTIONAL_VAR", "CONDITIONAL_VAR"]


def test_parse_env_block_only_if_present():
    _, opt = parse_env_block(_ENV_BLOCK_SKILL_MD)
    cond = next(o for o in opt if o["name"] == "CONDITIONAL_VAR")
    assert cond["only_if"] == "OPTIONAL_VAR=yes"
    plain = next(o for o in opt if o["name"] == "OPTIONAL_VAR")
    assert plain["only_if"] is None


def test_parse_env_block_no_env_key_returns_empty():
    req, opt = parse_env_block(_NO_ENV_SKILL_MD)
    assert req == [] and opt == []


def test_parse_env_block_description_with_colon_does_not_break():
    req, opt = parse_env_block(_ENV_BLOCK_SKILL_MD)
    assert len(req) == 1


# ─── read_env_specs ──────────────────────────────────────────────────


def test_read_env_specs_reads_from_skill_md(tmp_path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(_ENV_BLOCK_SKILL_MD)
    req, opt = read_env_specs(skill_dir)
    assert req[0].name == "REQUIRED_VAR"
    assert req[0].required is True
    assert opt[0].name == "OPTIONAL_VAR"
    assert opt[0].required is False


def test_read_env_specs_no_skill_md_returns_empty(tmp_path):
    skill_dir = tmp_path / "no-skill"
    skill_dir.mkdir()
    req, opt = read_env_specs(skill_dir)
    assert req == [] and opt == []


def test_read_env_specs_github_poller_real_file():
    from pathlib import Path as P
    poller_dir = P(__file__).parent.parent / "optional-skills" / "github-poller"
    if not poller_dir.is_dir():
        pytest.skip("optional-skills/github-poller not on disk")
    req, opt = read_env_specs(poller_dir)
    assert any(s.name == "GITHUB_REPOS" for s in req)
    assert any(s.name == "MIMIR_GITHUB_REVIEW_SKILL_PATH" for s in opt)
    path_spec = next(s for s in opt if s.name == "MIMIR_GITHUB_REVIEW_SKILL_PATH")
    assert path_spec.only_if == "MIMIR_GITHUB_PRELOAD_REVIEW_SKILL=true"


# ─── prompt_and_write_env ────────────────────────────────────────────

_REQ = SkillEnvSpec(
    name="MY_REQUIRED", description="A required var", example="foo", required=True
)
_OPT = SkillEnvSpec(
    name="MY_OPTIONAL", description="An optional var", example="bar", required=False
)
_COND = SkillEnvSpec(
    name="MY_CONDITIONAL",
    description="Only if MY_OPTIONAL=yes",
    example="cond",
    required=False,
    only_if="MY_OPTIONAL=yes",
)


def test_prompt_and_write_env_writes_required_var(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    monkeypatch.setattr("builtins.input", lambda _: "my-value")
    written = prompt_and_write_env([_REQ], [], env_path)
    assert "MY_REQUIRED" in written
    assert "MY_REQUIRED=my-value" in env_path.read_text()


def test_prompt_and_write_env_skips_already_set_var(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    env_path.write_text("MY_REQUIRED=existing-value\n")
    calls = []
    monkeypatch.setattr("builtins.input", lambda p: (calls.append(p), "new")[1])
    written = prompt_and_write_env([_REQ], [], env_path)
    assert written == []
    assert calls == []
    assert "MY_REQUIRED=existing-value" in env_path.read_text()


def test_prompt_and_write_env_reconfigure_reprompts(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    env_path.write_text("MY_REQUIRED=old-value\n")
    monkeypatch.setattr("builtins.input", lambda _: "new-value")
    written = prompt_and_write_env([_REQ], [], env_path, reconfigure=True)
    assert "MY_REQUIRED" in written
    assert "MY_REQUIRED=new-value" in env_path.read_text()


def test_prompt_and_write_env_blank_skips_optional(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    monkeypatch.setattr("builtins.input", lambda _: "")
    written = prompt_and_write_env([], [_OPT], env_path)
    assert written == []
    assert not env_path.exists()


def test_prompt_and_write_env_only_if_skips_when_condition_not_met(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    responses = iter(["", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    written = prompt_and_write_env([], [_OPT, _COND], env_path)
    assert "MY_CONDITIONAL" not in written


def test_prompt_and_write_env_only_if_prompts_when_condition_met(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    responses = iter(["yes", "cond-value"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    written = prompt_and_write_env([], [_OPT, _COND], env_path)
    assert "MY_OPTIONAL" in written
    assert "MY_CONDITIONAL" in written
    assert "MY_CONDITIONAL=cond-value" in env_path.read_text()


# ─── run_smoke_test ──────────────────────────────────────────────────


def test_run_smoke_test_no_poller_py(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    code, snippet = run_smoke_test(skill_dir)
    assert code == -1
    assert "no poller.py" in snippet


def test_run_smoke_test_exit_zero(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "poller.py").write_text(
        "import sys\nprint('event emitted')\nsys.exit(0)\n"
    )
    code, snippet = run_smoke_test(skill_dir)
    assert code == 0
    assert "event emitted" in snippet


def test_run_smoke_test_exit_nonzero(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "poller.py").write_text(
        "import sys\nprint('something failed', file=sys.stderr)\nsys.exit(1)\n"
    )
    code, snippet = run_smoke_test(skill_dir)
    assert code == 1


def test_run_smoke_test_once_fallback(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "poller.py").write_text(
        "import sys\n"
        "if '--once' in sys.argv:\n"
        "    print('unrecognized argument --once', file=sys.stderr)\n"
        "    sys.exit(2)\n"
        "print('ok without once')\n"
        "sys.exit(0)\n"
    )
    code, snippet = run_smoke_test(skill_dir)
    assert code == 0
    assert "ok without once" in snippet


# ─── cmd_install --configure integration ─────────────────────────────


def _make_configure_skill(root):
    skill_dir = root / "conf-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: conf-skill\n"
        "description: A skill with configure support.\n"
        "env:\n"
        "  required:\n"
        "    - name: CONF_REQUIRED\n"
        "      description: Required config var\n"
        "      example: required-val\n"
        "  optional:\n"
        "    - name: CONF_OPTIONAL\n"
        "      description: Optional config var\n"
        "      example: opt-val\n"
        "---\n"
        "body\n"
    )
    (skill_dir / "pollers.json").write_text(
        '{"pollers": [{"name": "conf", "command": "true", "cron": "*/5 * * * *"}]}'
    )
    (skill_dir / "poller.py").write_text("print('smoke ok')\n")
    return root


def test_cmd_install_configure_writes_vars(tmp_path, monkeypatch, capsys):
    opt_root = _make_configure_skill(tmp_path / "optional-skills")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT", opt_root)
    monkeypatch.setattr("builtins.input", lambda _: "my-conf-value")
    rc = cmd_install(Namespace(
        name="conf-skill", home=home, force=False,
        configure=True, reconfigure=False, no_smoke_test=True,
    ))
    assert rc == 0
    env_text = (home / ".env").read_text()
    assert "CONF_REQUIRED=my-conf-value" in env_text


def test_cmd_install_no_env_block_configure_is_noop(
    fake_optional_root, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT", fake_optional_root)
    home = tmp_path / "home"
    home.mkdir()
    rc = cmd_install(Namespace(
        name="fake-skill", home=home, force=False,
        configure=True, reconfigure=False, no_smoke_test=True,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no env:" in out
