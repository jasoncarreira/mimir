"""Path safety: confine all tool args to <home> (SPEC §7.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir._paths import (
    PathOutsideHomeError,
    resolve_home_path,
    resolve_within_roots,
)


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


# ─── resolve_within_roots — multi-root configuration ─────────────────


def test_multi_root_accepts_absolute_in_extra_root(tmp_path: Path):
    """A path in an extra root (e.g. /workspace) is accepted when the
    primary root (home) is something else. mimirbot's case: home =
    /mimir-home, extra = /workspace, agent reads its own source."""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    target = workspace / "mimir" / "agent.py"
    target.parent.mkdir()
    target.write_text("# agent")

    out = resolve_within_roots([home, workspace], str(target))
    assert out == target.resolve()


def test_multi_root_relative_resolves_against_first_root(tmp_path: Path):
    """Relative paths always resolve against the primary (first) root.
    The secondary roots are absolute-only — the agent has to write the
    full path to read its own source."""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    (home / "memory").mkdir()
    workspace.mkdir()

    out = resolve_within_roots([home, workspace], "memory/core.md")
    assert out == (home / "memory" / "core.md").resolve()


def test_multi_root_rejects_path_outside_all_roots(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    secret = tmp_path / "elsewhere" / "secret.md"
    secret.parent.mkdir()
    secret.write_text("nope")

    with pytest.raises(PathOutsideHomeError, match="escapes configured roots"):
        resolve_within_roots([home, workspace], str(secret))


def test_multi_root_empty_list_raises(tmp_path: Path):
    with pytest.raises(PathOutsideHomeError, match="no roots configured"):
        resolve_within_roots([], "anything")


def test_multi_root_dotdot_escape_caught_via_resolve(tmp_path: Path):
    """Path.resolve() flattens .. segments; a relative path that
    walks past the primary root and lands in an extra root is
    accepted (still inside SOME root). Walking past everything is
    rejected."""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (workspace / "x.md").write_text("ws content")

    # ``../workspace/x.md`` from home walks up and into workspace —
    # accepted because workspace is a configured root.
    out = resolve_within_roots([home, workspace], "../workspace/x.md")
    assert out == (workspace / "x.md").resolve()

    # ``../../etc/passwd`` walks past every root → rejected.
    with pytest.raises(PathOutsideHomeError):
        resolve_within_roots([home, workspace], "../../etc/passwd")
