"""Tests for mimir.bridges._attachments."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.bridges._attachments import (
    AttachmentPathError,
    build_inbound_path,
    resolve_outbound_path,
    sanitize_filename,
)


def test_sanitize_basic():
    assert sanitize_filename("normal-file.png") == "normal-file.png"


def test_sanitize_strips_spaces_and_specials():
    assert sanitize_filename("hello world!.txt") == "hello_world_.txt"


def test_sanitize_collapses_to_default_when_empty():
    assert sanitize_filename("@!#") == "attachment"


def test_sanitize_keeps_dots_underscores_dashes():
    assert sanitize_filename("name.v1_2-final.tar.gz") == "name.v1_2-final.tar.gz"


# ─── build_inbound_path ─────────────────────────────────────────────


def test_build_inbound_path_creates_dir(tmp_path: Path):
    target = build_inbound_path(
        tmp_path, channel="discord", chat_id="987654", filename="report.pdf"
    )
    assert target.parent.is_dir()
    assert target.parent.name == "987654"
    assert target.parent.parent.name == "discord"
    # Filename: <ts>-<token>-<safe>
    assert target.name.endswith("-report.pdf")


def test_build_inbound_path_sanitizes_chat_id(tmp_path: Path):
    """Slack thread timestamps look like ``1714768920.000123`` — dots
    are filesystem-safe but underscoring them keeps everything uniform."""
    target = build_inbound_path(
        tmp_path, channel="slack", chat_id="C03ABC.123", filename="x.png"
    )
    assert target.parent.name == "C03ABC.123"  # dots survive — they're allowed


def test_build_inbound_path_collisions_avoided(tmp_path: Path):
    """Repeated calls in the same second still produce unique filenames
    because of the 8-char UUID suffix."""
    a = build_inbound_path(tmp_path, "x", "y", "same.png")
    b = build_inbound_path(tmp_path, "x", "y", "same.png")
    assert a != b


def test_build_inbound_path_default_filename(tmp_path: Path):
    target = build_inbound_path(tmp_path, "x", "y", None)
    assert target.name.endswith("-attachment")


# ─── resolve_outbound_path ──────────────────────────────────────────


def test_outbound_relative_inside_root(tmp_path: Path):
    root = tmp_path / "outbound"
    root.mkdir()
    f = root / "report.pdf"
    f.write_bytes(b"%PDF")
    resolved = resolve_outbound_path(root, "report.pdf")
    assert resolved == f.resolve()


def test_outbound_subdir_inside_root(tmp_path: Path):
    root = tmp_path / "outbound"
    (root / "charts").mkdir(parents=True)
    f = root / "charts" / "q3.png"
    f.write_bytes(b"")
    resolved = resolve_outbound_path(root, "charts/q3.png")
    assert resolved == f.resolve()


def test_outbound_absolute_inside_root_ok(tmp_path: Path):
    root = tmp_path / "outbound"
    root.mkdir()
    f = root / "x.txt"
    f.write_text("ok")
    resolved = resolve_outbound_path(root, str(f))
    assert resolved == f.resolve()


def test_outbound_dotdot_escape_rejected(tmp_path: Path):
    root = tmp_path / "outbound"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    with pytest.raises(AttachmentPathError, match="escapes"):
        resolve_outbound_path(root, "../secret.txt")


def test_outbound_absolute_outside_root_rejected(tmp_path: Path):
    root = tmp_path / "outbound"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("nope")
    with pytest.raises(AttachmentPathError, match="outside"):
        resolve_outbound_path(root, str(elsewhere))


def test_outbound_symlink_escape_rejected(tmp_path: Path):
    """Symlinks pointing outside the root are rejected — Path.resolve()
    follows symlinks before the containment check, so the link's target
    is what's evaluated, not the link path."""
    root = tmp_path / "outbound"
    root.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("nope")
    link = root / "leak.txt"
    link.symlink_to(target)
    with pytest.raises(AttachmentPathError, match="escapes"):
        resolve_outbound_path(root, "leak.txt")


def test_outbound_missing_file_rejected(tmp_path: Path):
    root = tmp_path / "outbound"
    root.mkdir()
    with pytest.raises(AttachmentPathError, match="not found"):
        resolve_outbound_path(root, "nope.pdf")


def test_outbound_directory_rejected(tmp_path: Path):
    root = tmp_path / "outbound"
    sub = root / "subdir"
    sub.mkdir(parents=True)
    with pytest.raises(AttachmentPathError, match="not a file"):
        resolve_outbound_path(root, "subdir")


def test_outbound_empty_path_rejected(tmp_path: Path):
    with pytest.raises(AttachmentPathError, match="empty"):
        resolve_outbound_path(tmp_path, "")


def test_outbound_whitespace_only_rejected(tmp_path: Path):
    with pytest.raises(AttachmentPathError, match="empty"):
        resolve_outbound_path(tmp_path, "   ")
