from __future__ import annotations

from pathlib import Path

import pytest

from mimir.read_policy import resolve_non_admin_read_target


@pytest.mark.parametrize("virtual", [False, True])
def test_non_admin_can_read_attachments_without_widening_home(
    virtual: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    attachment = home / "attachments" / "fetch-cache" / "paper.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("extracted text\n", encoding="utf-8")
    private = home / "private" / "notes.txt"
    private.parent.mkdir()
    private.write_text("private\n", encoding="utf-8")
    scratch = home / "scratch" / "notes.txt"
    scratch.parent.mkdir()
    scratch.write_text("scratch\n", encoding="utf-8")
    monkeypatch.setenv("MIMIR_HOME", str(home))

    requested = "/attachments/fetch-cache/paper.txt" if virtual else str(attachment)
    assert resolve_non_admin_read_target(requested, scan_file=True) == attachment
    assert resolve_non_admin_read_target(str(home)) is None
    assert resolve_non_admin_read_target(str(private), scan_file=True) is None
    assert resolve_non_admin_read_target(str(scratch), scan_file=True) is None


def test_non_admin_attachment_root_refuses_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    attachments = home / "attachments"
    (home / "state").mkdir(parents=True)
    attachments.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "private.txt"
    target.write_text("private\n", encoding="utf-8")
    (attachments / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    assert resolve_non_admin_read_target(
        "/attachments/escape/private.txt", scan_file=True,
    ) is None
