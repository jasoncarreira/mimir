"""Tests for ``mimir.skill_install`` and its ``mimir skills install /
list / list-optional / update`` CLI wiring.

Covers the install flow (copy from optional-skills/ → home/skills/),
the conflict + --force semantics, the listing helpers, the
CLI-side argparse callbacks (so `mimir skills install bogus` returns
a non-zero exit cleanly), and the drift-detection logic
(``detect_skill_drift`` + ``mimir skills update``).
"""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from pathlib import Path

import pytest

from mimir.skill_install import (
    DEFAULT_OPTIONAL_SKILLS_ROOT,
    OptionalSkill,
    SkillDriftResult,
    SkillEnvSpec,
    _KEEP_PRE_UPDATE_BACKUPS,
    _prune_old_backups,
    accept_skill_drift,
    clear_accepted_skill_drift,
    apply_skill_update,
    cmd_accept_skill_drift,
    cmd_configure,
    cmd_install,
    cmd_list,
    cmd_list_optional,
    cmd_update_skills,
    detect_skill_drift,
    find_skill_path,
    list_accepted_skill_drift,
    install,
    list_available,
    list_installed,
    prompt_and_write_env,
    read_env_specs,
    run_smoke_test,
    walk_configurable_skills,
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


# chainlink #225 — path-traversal guard on `name`. Each test exercises a
# distinct attack shape and asserts ValueError is raised BEFORE any
# filesystem mutation (no rmtree, no copytree).

@pytest.mark.parametrize("bad_name", [
    "../foo",
    "../../tmp/foo",
    "..",
    "../",
    "foo/../bar",
])
def test_install_rejects_path_traversal_via_dotdot(
    bad_name: str, fake_optional_root: Path, fake_home: Path,
):
    """chainlink #225: install() must refuse a name that resolves outside
    the skills root. Pre-fix, ``--force`` would rmtree the resolved path
    happily.
    """
    with pytest.raises(ValueError, match="separator|'\\.\\.'|start with"):
        install(
            bad_name, fake_home, force=True,
            optional_skills_root=fake_optional_root,
        )


@pytest.mark.parametrize("bad_name", [
    "foo/bar",
    "foo\\bar",
    "/etc/passwd",
])
def test_install_rejects_path_separators(
    bad_name: str, fake_optional_root: Path, fake_home: Path,
):
    """chainlink #225: forward and back slashes are rejected since the
    skills root is a flat directory of skill names.
    """
    with pytest.raises(ValueError, match="separator"):
        install(
            bad_name, fake_home, force=True,
            optional_skills_root=fake_optional_root,
        )


@pytest.mark.parametrize("bad_name", [
    ".hidden",
    ".",
    ".cache",
])
def test_install_rejects_leading_dot(
    bad_name: str, fake_optional_root: Path, fake_home: Path,
):
    """chainlink #225: leading-dot names are reserved + catch ``..`` too."""
    with pytest.raises(ValueError, match="start with"):
        install(
            bad_name, fake_home, force=True,
            optional_skills_root=fake_optional_root,
        )


@pytest.mark.parametrize("bad_name", ["", "   ", "\t"])
def test_install_rejects_empty_name(
    bad_name: str, fake_optional_root: Path, fake_home: Path,
):
    """chainlink #225: empty / whitespace-only names rejected."""
    with pytest.raises(ValueError, match="empty"):
        install(
            bad_name, fake_home, force=True,
            optional_skills_root=fake_optional_root,
        )


def test_install_force_with_bad_name_does_not_rmtree(
    fake_optional_root: Path, fake_home: Path, tmp_path: Path,
):
    """chainlink #225: the headline scenario — ``--force`` with a traversal
    name must NOT delete anything. Plant a sentinel directory outside the
    skills root and verify it survives.
    """
    sentinel = tmp_path / "sentinel-must-survive"
    sentinel.mkdir()
    (sentinel / "important.txt").write_text("must not be deleted")

    # The traversal target would resolve under the parent of skills/ —
    # ``<home>/skills/../../sentinel-must-survive`` resolves to
    # ``tmp_path/sentinel-must-survive``.
    relative_target = "../../sentinel-must-survive"

    with pytest.raises(ValueError):
        install(
            relative_target, fake_home, force=True,
            optional_skills_root=fake_optional_root,
        )

    # Sentinel survives.
    assert sentinel.exists()
    assert (sentinel / "important.txt").read_text() == "must not be deleted"


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




def test_accept_skill_drift_records_hashes_and_marks_accepted(
    fake_optional_root: Path, fake_home: Path,
):
    """Accepting a changed file stores a two-sided fingerprint and tags drift."""
    install("fake-poller", fake_home, optional_skills_root=fake_optional_root)
    installed_pollers = fake_home / "skills" / "fake-poller" / "pollers.json"
    installed_pollers.write_text('{"pollers": [{"name": "fake", "command": "true", "cron": "30 * * * *"}]}')

    accepted = accept_skill_drift(
        fake_home, "fake-poller", fake_optional_root, files=["pollers.json"],
    )

    assert accepted == ["pollers.json"]
    entries = list_accepted_skill_drift(fake_home)
    assert [(e.skill, e.relpath) for e in entries] == [("fake-poller", "pollers.json")]
    assert entries[0].installed_hash
    assert entries[0].source_hash
    assert entries[0].installed_hash != entries[0].source_hash

    r = detect_skill_drift(fake_home, fake_optional_root, name="fake-poller")[0]
    assert not r.is_clean  # still visible on demand
    assert r.differs == ["pollers.json"]
    assert r.accepted == ["pollers.json"]
    assert r.unaccepted_differs == []
    assert not r.has_unaccepted_drift


def test_accepted_skill_drift_resurfaces_when_source_hash_changes(
    fake_optional_root: Path, fake_home: Path,
):
    """Changing either side of an accepted fingerprint makes the drift actionable again."""
    install("fake-poller", fake_home, optional_skills_root=fake_optional_root)
    installed_pollers = fake_home / "skills" / "fake-poller" / "pollers.json"
    installed_pollers.write_text('{"cron": "30 * * * *"}')
    accept_skill_drift(fake_home, "fake-poller", fake_optional_root, files=["pollers.json"])

    # Simulate a later release changing upstream pollers.json. The prior
    # acceptance no longer applies because the source hash changed.
    (fake_optional_root / "fake-poller" / "pollers.json").write_text('{"cron": "15 * * * *"}')

    r = detect_skill_drift(fake_home, fake_optional_root, name="fake-poller")[0]
    assert r.differs == ["pollers.json"]
    assert r.accepted == []
    assert r.unaccepted_differs == ["pollers.json"]
    assert r.has_unaccepted_drift


def test_accept_skill_drift_is_per_file_scoped(
    fake_optional_root: Path, fake_home: Path,
):
    """Accepting one file does not suppress drift in another file for the same skill."""
    install("fake-poller", fake_home, optional_skills_root=fake_optional_root)
    (fake_home / "skills" / "fake-poller" / "pollers.json").write_text('{"cron": "30 * * * *"}')
    (fake_home / "skills" / "fake-poller" / "poller.py").write_text("# local behavior\n")

    accept_skill_drift(fake_home, "fake-poller", fake_optional_root, files=["pollers.json"])

    r = detect_skill_drift(fake_home, fake_optional_root, name="fake-poller")[0]
    assert r.differs == ["poller.py", "pollers.json"]
    assert r.accepted == ["pollers.json"]
    assert r.unaccepted_differs == ["poller.py"]
    assert r.has_unaccepted_drift


def test_cmd_update_skills_accepted_only_exits_0_and_tags_drift(
    fake_optional_root: Path, fake_home: Path, capsys,
):
    """Accepted-only drift is shown on demand but does not make update exit dirty."""
    install("fake-poller", fake_home, optional_skills_root=fake_optional_root)
    (fake_home / "skills" / "fake-poller" / "pollers.json").write_text('{"cron": "30 * * * *"}')
    accept_skill_drift(fake_home, "fake-poller", fake_optional_root, files=["pollers.json"])

    rc = cmd_update_skills(Namespace(
        home=fake_home, name="fake-poller", all_skills=False,
        optional_skills_root=fake_optional_root, apply=False, force=False,
    ))

    assert rc == 0
    out = capsys.readouterr().out
    assert "differs from source: pollers.json (accepted)" in out
    assert "out of date" not in out


def test_apply_skill_update_skips_accepted_differs(
    fake_optional_root: Path, fake_home: Path,
):
    """`mimir skills update --apply` must not overwrite accepted intentional drift."""
    install("fake-poller", fake_home, optional_skills_root=fake_optional_root)
    installed_pollers = fake_home / "skills" / "fake-poller" / "pollers.json"
    local_text = '{"cron": "30 * * * *"}'
    installed_pollers.write_text(local_text)
    accept_skill_drift(fake_home, "fake-poller", fake_optional_root, files=["pollers.json"])

    r = detect_skill_drift(fake_home, fake_optional_root, name="fake-poller")[0]
    updated, failed, hint = apply_skill_update(r)

    assert updated == []
    assert failed == []
    assert hint is None
    assert installed_pollers.read_text() == local_text


def test_cmd_accept_skill_drift_list_and_clear(
    fake_optional_root: Path, fake_home: Path, capsys,
):
    """The accept CLI can record, list, and clear accepted drift fingerprints."""
    install("fake-poller", fake_home, optional_skills_root=fake_optional_root)
    (fake_home / "skills" / "fake-poller" / "pollers.json").write_text('{"cron": "30 * * * *"}')

    rc = cmd_accept_skill_drift(Namespace(
        home=fake_home, skill="fake-poller", files=["pollers.json"],
        list_entries=False, clear=None, optional_skills_root=fake_optional_root,
    ))
    assert rc == 0
    assert "accepted 1 file" in capsys.readouterr().out

    rc = cmd_accept_skill_drift(Namespace(
        home=fake_home, skill=None, files=[], list_entries=True,
        clear=None, optional_skills_root=fake_optional_root,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "fake-poller/pollers.json" in out

    assert clear_accepted_skill_drift(fake_home, "fake-poller") is True
    assert list_accepted_skill_drift(fake_home) == []



def test_skills_update_and_accept_help_cross_reference_intent() -> None:
    """Update/apply and accept help should expose the inspect / overwrite / keep split."""
    from mimir.skill_install import add_argparse_accept, add_argparse_update

    update_parser = ArgumentParser(prog="mimir skills update")
    add_argparse_update(update_parser)
    update_help = update_parser.format_help()

    accept_parser = ArgumentParser(prog="mimir skills accept")
    add_argparse_accept(accept_parser)
    accept_help = accept_parser.format_help()

    assert "read-only (dry-run)" in update_help
    assert "mimir" in update_help and "skills accept <skill>" in update_help
    assert "intentional local drift" in accept_help
    assert "mimir" in accept_help and "skills update --apply" in accept_help
    assert "overwrite local files with source" in accept_help


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


def test_render_file_diff_bounded_and_redacted(tmp_path: Path):
    """chainlink #378: _render_file_diff yields a redacted, length-bounded diff."""
    from mimir.skill_install import _MAX_DIFF_LINES_PER_FILE, _render_file_diff

    installed = tmp_path / "installed"
    source = tmp_path / "source"
    installed.mkdir()
    source.mkdir()
    # A token-shaped value on the installed side must be masked in the output.
    (installed / "f.txt").write_text("token=ghp_" + "a" * 30 + "\nsame\n")
    (source / "f.txt").write_text("clean\nsame\n")
    body = "\n".join(_render_file_diff(installed, source, "f.txt"))
    assert "installed/f.txt" in body and "source/f.txt" in body
    assert "ghp_" not in body  # token redacted
    assert "[REDACTED]" in body

    # A large change is capped with an explicit truncation marker (no silent cut).
    (installed / "big.txt").write_text("\n".join(f"line-{i}" for i in range(500)))
    (source / "big.txt").write_text("")
    big = _render_file_diff(installed, source, "big.txt")
    assert len(big) <= _MAX_DIFF_LINES_PER_FILE + 1  # +1 for the marker
    assert any("more diff line" in ln for ln in big)


def test_render_file_diff_missing_file_does_not_raise(tmp_path: Path):
    """Unreadable/missing files yield a note, never an exception."""
    from mimir.skill_install import _render_file_diff

    (tmp_path / "installed").mkdir()
    (tmp_path / "source").mkdir()
    out = _render_file_diff(tmp_path / "installed", tmp_path / "source", "nope.txt")
    assert out and "no textual diff" in out[0]


def test_cmd_update_skills_single_skill_shows_diff_by_default(
    fake_optional_root: Path, fake_home: Path, capsys, monkeypatch,
):
    """chainlink #378: inspecting a single named skill shows the content diff."""
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT", fake_optional_root,
    )
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    (fake_optional_root / "fake-skill" / "SKILL.md").write_text(
        "---\nname: fake-skill\ndescription: Updated description.\n---\n"
    )
    rc = cmd_update_skills(Namespace(
        home=fake_home, name="fake-skill", all_skills=False,
        optional_skills_root=fake_optional_root, apply=False, force=False, diff=False,
    ))
    assert rc == 1
    out = capsys.readouterr().out
    assert "differs from source: SKILL.md" in out
    assert "installed/SKILL.md" in out and "source/SKILL.md" in out  # diff headers
    assert "Updated description" in out  # the changed line is shown


def test_cmd_update_skills_all_terse_unless_diff_flag(
    fake_optional_root: Path, fake_home: Path, capsys, monkeypatch,
):
    """--all stays terse (file-level) by default; --diff forces the content diff."""
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT", fake_optional_root,
    )
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    (fake_optional_root / "fake-skill" / "SKILL.md").write_text(
        "---\nname: fake-skill\ndescription: Updated description.\n---\n"
    )
    cmd_update_skills(Namespace(
        home=fake_home, name=None, all_skills=True,
        optional_skills_root=fake_optional_root, apply=False, force=False, diff=False,
    ))
    terse = capsys.readouterr().out
    assert "differs from source: SKILL.md" in terse
    assert "installed/SKILL.md" not in terse  # no diff body under --all by default

    cmd_update_skills(Namespace(
        home=fake_home, name=None, all_skills=True,
        optional_skills_root=fake_optional_root, apply=False, force=False, diff=True,
    ))
    assert "installed/SKILL.md" in capsys.readouterr().out  # --diff forces it


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

    updated, failed, hint = apply_skill_update(r)

    assert "SKILL.md" in updated
    assert failed == []
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

    updated, failed, _ = apply_skill_update(r)

    assert "SKILL.md" in updated
    assert failed == []

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

    # A note must have been printed (neutral wording — no "local edits" claim).
    out = capsys.readouterr().out
    assert "Note" in out
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

    updated, failed, _ = apply_skill_update(r)

    assert "helper.py" in updated
    assert failed == []
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

    updated, failed, _ = apply_skill_update(r, force=False)

    assert "local-note.md" not in updated
    assert failed == []
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

    updated, failed, _ = apply_skill_update(r, force=True)

    assert "local-note.md" in updated
    assert failed == []
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

    updated, failed, hint = apply_skill_update(r)

    assert "pollers.json" in updated
    assert failed == []
    assert hint is not None
    assert "mcp__mimir__reload_pollers" in hint


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

    updated, failed, hint = apply_skill_update(r)

    assert updated == []
    assert failed == []
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
    assert "mcp__mimir__reload_pollers" in out


def test_apply_skill_update_partial_copy_failure(
    fake_optional_root: Path, fake_home: Path, monkeypatch, capsys,
):
    """apply_skill_update returns non-empty failed list when copy2 raises."""
    import shutil as _shutil

    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    # Introduce drift so there's something to copy.
    (fake_optional_root / "fake-skill" / "SKILL.md").write_text(
        "---\nname: fake-skill\ndescription: updated\n---\n"
    )

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")
    assert "SKILL.md" in r.differs

    original_copy2 = _shutil.copy2

    def _failing_copy2(src, dst, **kw):
        if Path(src).name == "SKILL.md" and ".pre-update-backup" not in str(src):
            raise OSError("disk full (simulated)")
        return original_copy2(src, dst, **kw)

    monkeypatch.setattr("mimir.skill_install.shutil.copy2", _failing_copy2)

    updated, failed, hint = apply_skill_update(r)

    assert "SKILL.md" in failed
    assert "SKILL.md" not in updated


def test_cmd_update_skills_apply_copy_failure_exits_1(
    fake_optional_root: Path, fake_home: Path, monkeypatch, capsys,
):
    """``mimir skills update --apply`` exits 1 when a per-file copy fails."""
    import shutil as _shutil

    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    (fake_optional_root / "fake-skill" / "SKILL.md").write_text(
        "---\nname: fake-skill\ndescription: updated\n---\n"
    )

    original_copy2 = _shutil.copy2

    def _failing_copy2(src, dst, **kw):
        if Path(src).name == "SKILL.md" and ".pre-update-backup" not in str(src):
            raise OSError("disk full (simulated)")
        return original_copy2(src, dst, **kw)

    monkeypatch.setattr("mimir.skill_install.shutil.copy2", _failing_copy2)

    rc = cmd_update_skills(Namespace(
        home=fake_home,
        name=None,
        optional_skills_root=fake_optional_root,
        apply=True,
        force=False,
    ))
    assert rc == 1  # copy failure → partial update → exit 1


# ─── cmd_update_skills --all flag ────────────────────────────────────


def test_cmd_update_skills_all_flag_equivalent_to_no_name(
    fake_optional_root: Path, fake_home: Path, capsys, monkeypatch,
):
    """``mimir skills update --all`` behaves identically to omitting the name."""
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT", fake_optional_root,
    )
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    rc = cmd_update_skills(Namespace(
        home=fake_home,
        name=None,
        all_skills=True,
        optional_skills_root=fake_optional_root,
        apply=False,
        force=False,
    ))
    assert rc == 0
    assert "up to date" in capsys.readouterr().out


def test_cmd_update_skills_all_flag_mutually_exclusive_with_name(
    fake_home: Path, capsys,
):
    """``mimir skills update --all <name>`` exits 2 with an error message."""
    rc = cmd_update_skills(Namespace(
        home=fake_home,
        name="some-skill",
        all_skills=True,
        optional_skills_root=None,
        apply=False,
        force=False,
    ))
    assert rc == 2
    assert "--all" in capsys.readouterr().out


def test_cmd_update_skills_all_flag_apply_updates_all_skills(
    fake_optional_root: Path, fake_home: Path, capsys, monkeypatch,
):
    """``mimir skills update --all --apply`` updates every drifted skill."""
    monkeypatch.setattr(
        "mimir.skill_install.DEFAULT_OPTIONAL_SKILLS_ROOT", fake_optional_root,
    )
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    # Introduce drift in the installed copy.
    installed_md = fake_home / "skills" / "fake-skill" / "SKILL.md"
    installed_md.write_text("---\nname: fake-skill\ndescription: stale\n---\n")

    rc = cmd_update_skills(Namespace(
        home=fake_home,
        name=None,
        all_skills=True,
        optional_skills_root=fake_optional_root,
        apply=True,
        force=False,
    ))
    assert rc == 0
    # File should now match the source.
    src_text = (fake_optional_root / "fake-skill" / "SKILL.md").read_text()
    assert installed_md.read_text() == src_text


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


# ─── find_skill_path + walk_configurable_skills ───────────────────────


def _make_home_with_skills(tmp_path: Path) -> Path:
    """Build a minimal home with one installed skill and one built-in skill."""
    home = tmp_path / "home"
    home.mkdir()

    # Installed optional skill with env block.
    opt_skill = home / "skills" / "opt-skill"
    opt_skill.mkdir(parents=True)
    (opt_skill / "SKILL.md").write_text(
        "---\n"
        "name: opt-skill\n"
        "description: An installed optional skill.\n"
        "env:\n"
        "  required:\n"
        "    - name: OPT_VAR\n"
        "      description: A required var\n"
        "      example: val\n"
        "---\nbody\n"
    )

    # Built-in skill without env block.
    builtin_skill = home / ".mimir_builtin_skills" / "builtin-plain"
    builtin_skill.mkdir(parents=True)
    (builtin_skill / "SKILL.md").write_text(
        "---\nname: builtin-plain\ndescription: A plain built-in.\n---\nbody\n"
    )

    # Built-in skill WITH env block.
    builtin_env = home / ".mimir_builtin_skills" / "builtin-env"
    builtin_env.mkdir(parents=True)
    (builtin_env / "SKILL.md").write_text(
        "---\n"
        "name: builtin-env\n"
        "description: A built-in skill with env vars.\n"
        "env:\n"
        "  required:\n"
        "    - name: BUILTIN_HOST\n"
        "      description: Hostname for built-in service\n"
        "      example: api.example.com\n"
        "---\nbody\n"
    )

    return home


# ─── _skill_env_summary ───────────────────────────────────────────────

_SKILL_WITH_ENV = """\
---
name: needs-key
description: A skill that needs an API key.
env:
  required:
    - name: NEEDS_KEY_API_KEY
      description: "The required API key"
      example: "abc123"
---
body
"""

_SKILL_NO_ENV = """\
---
name: no-env-skill
description: A plain skill with no env: block.
---
body
"""


def _make_fake_home(tmp_path: Path) -> Path:
    """Create a minimal home structure with .mimir_builtin_skills/."""
    home = tmp_path / "home"
    builtin_root = home / ".mimir_builtin_skills"
    builtin_root.mkdir(parents=True)

    # Skill with env: block
    skill_dir = builtin_root / "needs-key"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(_SKILL_WITH_ENV)

    # Skill without env: block (should not appear in summary)
    no_env_dir = builtin_root / "no-env-skill"
    no_env_dir.mkdir()
    (no_env_dir / "SKILL.md").write_text(_SKILL_NO_ENV)

    return home


def test_find_skill_path_installed_skill(tmp_path):
    home = _make_home_with_skills(tmp_path)
    result = find_skill_path(home, "opt-skill")
    assert result is not None
    assert result == home / "skills" / "opt-skill"


def test_find_skill_path_builtin_skill(tmp_path):
    home = _make_home_with_skills(tmp_path)
    result = find_skill_path(home, "builtin-env")
    assert result is not None
    assert result == home / ".mimir_builtin_skills" / "builtin-env"


def test_find_skill_path_not_found(tmp_path):
    home = _make_home_with_skills(tmp_path)
    assert find_skill_path(home, "no-such-skill") is None


def test_find_skill_path_installed_shadows_builtin(tmp_path):
    """Operator-installed skill takes precedence over same-named built-in."""
    home = _make_home_with_skills(tmp_path)
    # Install a "builtin-env" copy in skills/ (shadows the builtin).
    override = home / "skills" / "builtin-env"
    override.mkdir(parents=True)
    (override / "SKILL.md").write_text(
        "---\nname: builtin-env\ndescription: Override.\n---\nbody\n"
    )
    result = find_skill_path(home, "builtin-env")
    assert result == override


def test_walk_configurable_skills_returns_env_skills(tmp_path):
    home = _make_home_with_skills(tmp_path)
    results = walk_configurable_skills(home)
    names = {name for name, _ in results}
    assert "opt-skill" in names
    assert "builtin-env" in names
    assert "builtin-plain" not in names  # no env: block


# ─── cmd_configure ────────────────────────────────────────────────────


def test_cmd_configure_specific_skill(tmp_path, monkeypatch, capsys):
    home = _make_home_with_skills(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "my-api-key")
    rc = cmd_configure(Namespace(
        name="builtin-env", all_skills=False, home=home,
        reconfigure=False, no_smoke_test=True,
    ))
    assert rc == 0
    env_text = (home / ".env").read_text()
    assert "BUILTIN_HOST=my-api-key" in env_text


def test_cmd_configure_no_env_block_is_noop(tmp_path, capsys):
    home = _make_home_with_skills(tmp_path)
    rc = cmd_configure(Namespace(
        name="builtin-plain", all_skills=False, home=home,
        reconfigure=False, no_smoke_test=True,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no env:" in out


def test_cmd_configure_skill_not_found(tmp_path, capsys):
    home = _make_home_with_skills(tmp_path)
    rc = cmd_configure(Namespace(
        name="missing-skill", all_skills=False, home=home,
        reconfigure=False, no_smoke_test=True,
    ))
    assert rc == 2
    out = capsys.readouterr().out
    assert "not found" in out


def test_cmd_configure_all_iterates_env_skills(tmp_path, monkeypatch, capsys):
    home = _make_home_with_skills(tmp_path)
    # Use a fixed value for all prompts — we're testing iteration, not ordering.
    monkeypatch.setattr("builtins.input", lambda _: "test-value")
    rc = cmd_configure(Namespace(
        name=None, all_skills=True, home=home,
        reconfigure=False, no_smoke_test=True,
    ))
    assert rc == 0
    env_text = (home / ".env").read_text()
    # Both configurable skills should have their vars written.
    assert "BUILTIN_HOST=test-value" in env_text
    assert "OPT_VAR=test-value" in env_text


def test_cmd_configure_all_no_configurable_skills(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    # A home with only a skill that has no env: block.
    plain = home / ".mimir_builtin_skills" / "plain"
    plain.mkdir(parents=True)
    (plain / "SKILL.md").write_text(
        "---\nname: plain\ndescription: Plain.\n---\nbody\n"
    )
    rc = cmd_configure(Namespace(
        name=None, all_skills=True, home=home,
        reconfigure=False, no_smoke_test=True,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no configurable" in out


def test_cmd_configure_no_smoke_test_flag_skips_smoke(tmp_path, monkeypatch, capsys):
    """--no-smoke-test prevents the smoke test even when pollers.json is present."""
    home = tmp_path / "home"
    home.mkdir()

    # Build a fake built-in skill with a pollers.json and an env: block.
    skill_dir = home / ".mimir_builtin_skills" / "test-poller"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-poller\n"
        "description: A poller skill for testing no-smoke-test.\n"
        "env:\n"
        "  required:\n"
        "    - name: TEST_VAR\n"
        "      description: A test variable\n"
        "      example: test-val\n"
        "---\nbody\n"
    )
    (skill_dir / "pollers.json").write_text(
        '{"pollers": [{"name": "test", "command": "true", "cron": "*/5 * * * *"}]}'
    )
    # poller.py would run if smoke test fires; emit recognisable output so we
    # can detect an accidental execution.
    (skill_dir / "poller.py").write_text("print('SMOKE_TEST_RAN')\n")

    # Suppress interactive prompt by returning a fixed value.
    monkeypatch.setattr("builtins.input", lambda _: "test-value")

    rc = cmd_configure(Namespace(
        name="test-poller", all_skills=False, home=home,
        reconfigure=False, no_smoke_test=True,
    ))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Running smoke test" not in out
    assert "SMOKE_TEST_RAN" not in out


def test_cmd_configure_fresh_home_hint_mentions_setup(tmp_path, capsys):
    """When built-ins aren't seeded yet, the error points at mimir setup."""
    # Fresh home directory — no .mimir_builtin_skills/ and no skills/ subdir.
    home = tmp_path / "fresh-home"
    home.mkdir()

    rc = cmd_configure(Namespace(
        name="any-skill", all_skills=False, home=home,
        reconfigure=False, no_smoke_test=True,
    ))

    assert rc == 2
    out = capsys.readouterr().out
    assert "not found" in out
    assert "mimir setup" in out


def test_skill_env_summary_returns_skills_with_env_blocks(tmp_path):
    from mimir.cli import _skill_env_summary

    home = _make_fake_home(tmp_path)
    result = _skill_env_summary(str(home))
    assert len(result) == 1
    entry = result[0]
    assert entry["name"] == "needs-key"
    assert len(entry["required"]) == 1
    assert entry["required"][0]["name"] == "NEEDS_KEY_API_KEY"
    # Var not set in test env → set=False
    assert entry["required"][0]["set"] is False
    assert entry["optional"] == []


def test_skill_env_summary_set_true_when_var_in_environ(tmp_path, monkeypatch):
    from mimir.cli import _skill_env_summary

    home = _make_fake_home(tmp_path)
    monkeypatch.setenv("NEEDS_KEY_API_KEY", "test-key-value")
    result = _skill_env_summary(str(home))
    assert len(result) == 1
    assert result[0]["required"][0]["set"] is True


def test_skill_env_summary_operator_skill_shadows_builtin(tmp_path):
    """An operator skill in <home>/skills/ shadows same-named builtin."""
    from mimir.cli import _skill_env_summary

    home = _make_fake_home(tmp_path)

    # Operator version of the same skill — different (empty) env: block
    op_skill_dir = home / "skills" / "needs-key"
    op_skill_dir.mkdir(parents=True)
    (op_skill_dir / "SKILL.md").write_text(_SKILL_NO_ENV.replace(
        "name: no-env-skill", "name: needs-key"
    ))

    result = _skill_env_summary(str(home))
    # Operator copy has no env: block → summary should be empty
    assert result == []


def test_skill_env_summary_weather_and_ntfy_have_env_blocks():
    """Integration: bundled weather + ntfy SKILL.md declare env: blocks."""
    from pathlib import Path as P
    from mimir.skill_md import parse_env_block

    bundled_root = P(__file__).parent.parent / "mimir" / "skills"
    for skill_name, expected_var in [
        ("weather", "OPENWEATHER_API_KEY"),
        ("ntfy", "NTFY_TOPIC"),
    ]:
        skill_md = bundled_root / skill_name / "SKILL.md"
        assert skill_md.is_file(), f"bundled skill SKILL.md not found: {skill_md}"
        req, _ = parse_env_block(skill_md.read_text())
        req_names = [r["name"] for r in req]
        assert expected_var in req_names, (
            f"{skill_name}/SKILL.md expected required env var {expected_var!r}; "
            f"got: {req_names}"
        )


def test_run_smoke_test_uses_minimal_env_not_host_secrets(tmp_path, monkeypatch):
    """chainlink #259: the install-time smoke test runs with a minimal env
    (essentials + the skill's .env), NOT the full inherited os.environ, so a
    third-party skill can't read mimir's unrelated secrets at install time."""
    monkeypatch.setenv("MIMIR_FAKE_HOST_SECRET", "should-not-leak")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "poller.py").write_text(
        "import os\n"
        "print('SECRET=' + os.environ.get('MIMIR_FAKE_HOST_SECRET', 'ABSENT'))\n"
        "print('SKILLVAR=' + os.environ.get('SKILL_DECLARED_VAR', 'ABSENT'))\n"
    )
    env_path = tmp_path / ".env"
    env_path.write_text("SKILL_DECLARED_VAR=present\n")

    code, snippet = run_smoke_test(skill_dir, env_path=env_path)
    assert code == 0, snippet
    # Host secret is NOT visible to the skill's smoke test.
    assert "SECRET=ABSENT" in snippet
    assert "should-not-leak" not in snippet
    # The skill's own declared/configured var (from .env) IS visible.
    assert "SKILLVAR=present" in snippet


# ─── .pre-update-backup drift + pruning ──────────────────────────────


def test_drift_ignores_pre_update_backup(fake_optional_root: Path, fake_home: Path):
    """``apply_skill_update`` writes ``.pre-update-backup/<ts>/`` INSIDE the
    installed skill dir. The drift detector must ignore it — otherwise the
    backup files surface as ``extra`` and the detector flags its own backups
    as drift (the false positive mimirbot hit after the 0.3.0 update)."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    backup = (
        fake_home / "skills" / "fake-skill"
        / ".pre-update-backup" / "20260609T190809Z"
    )
    backup.mkdir(parents=True)
    (backup / "SKILL.md").write_text("old backed-up content\n")
    (backup / "pollers.json").write_text("{}\n")

    results = detect_skill_drift(fake_home, fake_optional_root)
    r = next(r for r in results if r.name == "fake-skill")
    assert r.is_clean, f"backup leaked into drift: extra={r.extra} differs={r.differs}"
    assert r.extra == []
    assert not r.has_unaccepted_drift


def test_prune_old_backups_keeps_most_recent(tmp_path: Path):
    skill = tmp_path / "skill"
    base = skill / ".pre-update-backup"
    stamps = [
        "20260101T000000Z", "20260102T000000Z", "20260103T000000Z",
        "20260104T000000Z", "20260105T000000Z",
    ]
    for s in stamps:
        d = base / s
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("snapshot\n")

    _prune_old_backups(skill)  # default keep=3

    remaining = sorted(d.name for d in base.iterdir() if d.is_dir())
    assert remaining == stamps[-_KEEP_PRE_UPDATE_BACKUPS:]  # newest 3 retained


def test_prune_old_backups_noop_without_backup_dir(tmp_path: Path):
    # No .pre-update-backup dir → no error, nothing created.
    skill = tmp_path / "skill"
    skill.mkdir()
    _prune_old_backups(skill)
    assert not (skill / ".pre-update-backup").exists()


def test_apply_update_prunes_backups_after_overwrite(
    fake_optional_root: Path, fake_home: Path,
):
    """An end-to-end apply that creates a fresh backup also prunes old ones."""
    install("fake-skill", fake_home, optional_skills_root=fake_optional_root)
    skill_dir = fake_home / "skills" / "fake-skill"
    # Seed 4 stale backups (> keep=3).
    for s in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z", "20260104T000000Z"):
        (skill_dir / ".pre-update-backup" / s).mkdir(parents=True)
    # Make source differ so apply overwrites SKILL.md (→ writes a new backup).
    (fake_optional_root / "fake-skill" / "SKILL.md").write_text(
        "---\nname: fake-skill\ndescription: changed.\n---\n# fake-skill\nNEW\n"
    )
    result = next(
        r for r in detect_skill_drift(fake_home, fake_optional_root)
        if r.name == "fake-skill"
    )
    apply_skill_update(result)

    backups = sorted(
        d.name for d in (skill_dir / ".pre-update-backup").iterdir() if d.is_dir()
    )
    assert len(backups) == _KEEP_PRE_UPDATE_BACKUPS  # pruned to the cap
