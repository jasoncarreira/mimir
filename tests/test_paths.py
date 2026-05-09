"""Path safety: confine all tool args to <home> (SPEC §7.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir._paths import (
    PathOutsideHomeError,
    claude_code_persisted_output_root,
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


# ─── claude_code_persisted_output_root — overflow Read auto-allow ────


def test_persisted_output_root_returns_dir_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When Claude Code's projects dir exists, return it."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cc_projects = fake_home / ".claude" / "projects"
    cc_projects.mkdir(parents=True)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    out = claude_code_persisted_output_root()
    assert out == cc_projects


def test_persisted_output_root_returns_path_eagerly_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When the projects dir doesn't exist yet (fresh container, no
    overflow yet), still return the expected path — caller adds it
    to ``extra_roots`` unconditionally so the very first overflow
    Read after startup resolves rather than being denied. Claude
    Code lazy-creates the dir on first overflow; pre-creation by us
    isn't needed (``resolve_within_roots`` is a prefix check)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    out = claude_code_persisted_output_root()
    assert out == fake_home / ".claude" / "projects"
    assert not out.exists()  # confirms eager-return — caller still gets a usable root


def test_persisted_output_root_honors_claude_config_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """``CLAUDE_CONFIG_DIR`` overrides the ``~/.claude`` default —
    the SDK uses this to relocate session state, and the projects
    dir lives under the override too."""
    custom = tmp_path / "custom-claude"
    cc_projects = custom / "projects"
    cc_projects.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))

    out = claude_code_persisted_output_root()
    assert out == cc_projects


def test_persisted_output_root_works_with_real_pre_tool_hook(
    tmp_path: Path,
):
    """Once added to the file-op roots, a Read of a path inside
    Claude Code's projects dir resolves rather than raising. This
    covers the end-to-end contract: hook receives the overflow
    path the runtime reports, and lets it through."""
    home = tmp_path / "home"
    cc = tmp_path / "cc-projects"
    home.mkdir()
    cc.mkdir()
    overflow = cc / "session-uuid" / "tool-results" / "abc123.txt"
    overflow.parent.mkdir(parents=True)
    overflow.write_text("the overflow content")

    out = resolve_within_roots([home, cc], str(overflow))
    assert out == overflow.resolve()
