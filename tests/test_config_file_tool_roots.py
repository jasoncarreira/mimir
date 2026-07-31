"""Unit tests for ``_parse_file_tool_roots`` — configurable file-tool roots (#650).

Covers parsing of ``MIMIR_FILE_TOOL_ROOTS`` into validated ``(abs_path, mode)``
pairs: rw-default, explicit ro/rw, the validation rejections (non-absolute,
missing, non-dir, common system roots, ``~`` / ``..``, home-overlap), the
always-rw ``/tmp`` behavior, and dedupe.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from mimir.config import (
    _ALWAYS_RW_FILE_TOOL_ROOTS,
    _FILE_TOOL_FORBIDDEN_ROOTS,
    _parse_file_tool_roots,
)


def _home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_bare_path_defaults_to_rw(tmp_path: Path) -> None:
    home = _home(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _parse_file_tool_roots(str(repo), home, always_rw=()) == ((str(repo.resolve()), "rw"),)


def test_explicit_modes(tmp_path: Path) -> None:
    home = _home(tmp_path)
    rw = tmp_path / "rw"
    rw.mkdir()
    ro = tmp_path / "ro"
    ro.mkdir()
    out = dict(_parse_file_tool_roots(f"{rw}:rw,{ro}:ro", home, always_rw=()))
    assert out[str(rw.resolve())] == "rw"
    assert out[str(ro.resolve())] == "ro"


def test_unknown_mode_is_rejected_as_syntax(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = _home(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    entry = f"{repo}:bogus"
    with caplog.at_level(logging.WARNING):
        assert _parse_file_tool_roots(entry, home, always_rw=()) == ()
    assert entry in caplog.text
    assert "unknown mode 'bogus'" in caplog.text
    assert "expected /absolute/path[:ro|:rw], comma-separated" in caplog.text


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (",", "entry is empty"),
        ("/one:rw:ro", "colon is only valid as the optional mode delimiter"),
        (":rw", "path is empty"),
    ],
)
def test_rejects_malformed_entries_with_actionable_warning(
    raw: str,
    reason: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert _parse_file_tool_roots(raw, _home(tmp_path), always_rw=()) == ()
    assert reason in caplog.text
    assert "expected /absolute/path[:ro|:rw], comma-separated" in caplog.text


def test_rejects_non_absolute(tmp_path: Path) -> None:
    assert _parse_file_tool_roots("relative/dir", _home(tmp_path), always_rw=()) == ()


def test_rejects_missing_dir(tmp_path: Path) -> None:
    home = _home(tmp_path)
    assert _parse_file_tool_roots(str(tmp_path / "nope"), home, always_rw=()) == ()


def test_rejects_file_not_dir(tmp_path: Path) -> None:
    home = _home(tmp_path)
    f = tmp_path / "f.txt"
    f.write_text("x")
    assert _parse_file_tool_roots(str(f), home, always_rw=()) == ()


@pytest.mark.parametrize(
    "bad",
    sorted(
        candidate
        for root in _FILE_TOOL_FORBIDDEN_ROOTS
        for candidate in (root, f"{root}/")
        if Path(root).exists()
    ),
)
def test_rejects_forbidden_roots(bad: str, tmp_path: Path) -> None:
    assert _parse_file_tool_roots(bad, _home(tmp_path), always_rw=()) == ()


def test_rejects_path_resolving_to_forbidden_root(tmp_path: Path) -> None:
    home = _home(tmp_path)
    alias = tmp_path / "system-alias"
    alias.symlink_to("/etc", target_is_directory=True)
    assert _parse_file_tool_roots(str(alias), home, always_rw=()) == ()


def test_every_path_rejection_warns_with_entry_reason_and_expected_form(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = _home(tmp_path)
    file_path = tmp_path / "file"
    file_path.write_text("not a directory")
    forbidden_alias = tmp_path / "forbidden-alias"
    forbidden_alias.symlink_to("/etc", target_is_directory=True)
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    entries = [
        "relative",
        "~/repo",
        str(tmp_path / "a" / ".." / "repo"),
        "/etc",
        str(loop),
        str(tmp_path / "missing"),
        str(file_path),
        str(forbidden_alias),
        str(home),
    ]

    with caplog.at_level(logging.WARNING):
        assert _parse_file_tool_roots(",".join(entries), home, always_rw=()) == ()

    for entry in entries:
        assert repr(entry) in caplog.text
    for reason in (
        "path is not absolute",
        "~ and .. are not allowed",
        "path is a forbidden system root",
        "path cannot be resolved",
        "path is not an existing directory",
        "path resolves to a forbidden system root",
        "path overlaps the agent home",
    ):
        assert reason in caplog.text
    assert caplog.text.count(
        "expected /absolute/path[:ro|:rw], comma-separated"
    ) == len(entries)


def test_tmp_and_project_roots_still_allowed(tmp_path: Path) -> None:
    home = Path("/__mimir_nonexistent_home_for_file_roots_test__")
    repo = tmp_path / "repo"
    repo.mkdir()
    out = dict(_parse_file_tool_roots(str(repo), home, always_rw=("/tmp",)))
    assert out[str(repo.resolve())] == "rw"
    assert out[str(Path("/tmp").resolve())] == "rw"


def test_rejects_tilde_and_traversal(tmp_path: Path) -> None:
    home = _home(tmp_path)
    assert _parse_file_tool_roots("~/repo", home, always_rw=()) == ()
    assert _parse_file_tool_roots(f"{tmp_path}/a/../b", home, always_rw=()) == ()


def test_rejects_home_and_overlap(tmp_path: Path) -> None:
    home = _home(tmp_path)
    sub = home / "sub"
    sub.mkdir()
    # the home itself, a subdir of the home, and a parent of the home all overlap
    assert _parse_file_tool_roots(str(home), home, always_rw=()) == ()
    assert _parse_file_tool_roots(str(sub), home, always_rw=()) == ()
    assert _parse_file_tool_roots(str(tmp_path), home, always_rw=()) == ()


def test_always_rw_added_when_present(tmp_path: Path) -> None:
    home = _home(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    out = dict(_parse_file_tool_roots("", home, always_rw=(str(scratch),)))
    assert out == {str(scratch.resolve()): "rw"}


def test_always_rw_skipped_when_overlapping_home(tmp_path: Path) -> None:
    home = _home(tmp_path)
    # an always-rw root that CONTAINS the home would shadow the home backend → skip
    assert _parse_file_tool_roots("", home, always_rw=(str(tmp_path),)) == ()


def test_explicit_entry_wins_over_always_rw(tmp_path: Path) -> None:
    home = _home(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    # same dir declared explicit-ro AND in always_rw → explicit ro wins, one entry
    out = _parse_file_tool_roots(f"{repo}:ro", home, always_rw=(str(repo),))
    assert out == ((str(repo.resolve()), "ro"),)


def test_default_always_rw_is_tmp() -> None:
    assert _ALWAYS_RW_FILE_TOOL_ROOTS == ("/tmp",)


def test_unset_env_still_appends_default_always_rw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unset-env contract (mimir-carreira #869 review): an empty
    # MIMIR_FILE_TOOL_ROOTS still appends the module default always-rw roots
    # (``/tmp`` in prod) — "unset" is NOT "home-only". Patch the default to a
    # controlled dir outside the home so the assertion is deterministic
    # regardless of the CI temp-dir layout (where /tmp may contain the home).
    home = _home(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("mimir.config._ALWAYS_RW_FILE_TOOL_ROOTS", (str(scratch),))
    out = _parse_file_tool_roots("", home)  # no always_rw= → uses module default
    assert out == ((str(scratch.resolve()), "rw"),)


def test_effective_root_log_names_configured_and_derived_origins(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = _home(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with caplog.at_level(logging.INFO):
        _parse_file_tool_roots(
            f"{repo}:ro",
            home,
            always_rw=(str(scratch),),
            log_effective=True,
        )

    assert f"path='{home.resolve()}' mode=rw origin=derived-home" in caplog.text
    assert f"path='{repo.resolve()}' mode=ro origin=configured" in caplog.text
    assert f"path='{scratch.resolve()}' mode=rw origin=derived" in caplog.text
