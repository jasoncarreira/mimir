"""Path safety: confine all tool args to <home> (SPEC §7.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir._paths import PathOutsideHomeError, resolve_home_path


def test_relative_path_resolves_inside_home(tmp_path: Path):
    out = resolve_home_path(tmp_path, "memory/core/00.md")
    assert out == (tmp_path / "memory" / "core" / "00.md").resolve()


def test_absolute_path_outside_home_rejected(tmp_path: Path):
    with pytest.raises(PathOutsideHomeError):
        resolve_home_path(tmp_path, "/etc/passwd")


def test_absolute_path_inside_home_accepted(tmp_path: Path):
    """The SDK CLI passes absolute paths; hooks must recognize them as
    legitimate when they resolve within home."""
    target = tmp_path / "memory" / "core" / "00.md"
    out = resolve_home_path(tmp_path, str(target))
    assert out == target.resolve()


def test_dotdot_escape_rejected(tmp_path: Path):
    with pytest.raises(PathOutsideHomeError):
        resolve_home_path(tmp_path, "../../etc/passwd")


def test_empty_path_rejected(tmp_path: Path):
    with pytest.raises(PathOutsideHomeError):
        resolve_home_path(tmp_path, "")


def test_dotdot_within_home_is_fine(tmp_path: Path):
    # ``a/../a`` resolves to a path inside home — accepted.
    out = resolve_home_path(tmp_path, "a/../a/file.md")
    assert out == (tmp_path / "a" / "file.md").resolve()


def test_symlink_pointing_outside_home_rejected(tmp_path: Path):
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(PathOutsideHomeError):
        resolve_home_path(tmp_path, "link/secret.md")
