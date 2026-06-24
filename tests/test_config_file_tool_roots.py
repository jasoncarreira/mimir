"""Unit tests for ``_parse_file_tool_roots`` — configurable file-tool roots (#650).

Covers parsing of ``MIMIR_FILE_TOOL_ROOTS`` into validated ``(abs_path, mode)``
pairs: rw-default, explicit ro/rw, the validation rejections (non-absolute,
missing, non-dir, ``/`` / ``/etc``, ``~`` / ``..``, home-overlap), the
always-rw ``/tmp`` behavior, and dedupe.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mimir.config import _ALWAYS_RW_FILE_TOOL_ROOTS, _parse_file_tool_roots


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


def test_unknown_mode_treats_whole_entry_as_path(tmp_path: Path) -> None:
    # ``:bogus`` isn't ro/rw, so the whole entry is treated as a path — which is
    # not an existing directory, so it's skipped.
    home = _home(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _parse_file_tool_roots(f"{repo}:bogus", home, always_rw=()) == ()


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


@pytest.mark.parametrize("bad", ["/", "/etc", "/etc/"])
def test_rejects_forbidden_roots(bad: str, tmp_path: Path) -> None:
    assert _parse_file_tool_roots(bad, _home(tmp_path), always_rw=()) == ()


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
