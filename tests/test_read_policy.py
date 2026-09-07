from __future__ import annotations

from pathlib import Path

import pytest

from mimir.read_policy import (
    _has_protected_read_name,
    is_protected_read_path,
    protected_read_denial_reason,
    resolve_non_admin_read_target,
)


@pytest.mark.parametrize("stem", [
    ".env", ".env.local", ".env.production", ".envrc", "compose.env",
    "secrets.yaml", "credentials.json", "id_rsa", "server.key",
])
@pytest.mark.parametrize("suffix", ["", ".example", ".sample", ".template", ".dist"])
@pytest.mark.parametrize("uppercase", [False, True])
def test_protected_names_and_templates(stem, suffix, uppercase, tmp_path, monkeypatch):
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    name = stem + suffix
    target = tmp_path / (name.upper() if uppercase else name)
    protected = not bool(suffix)
    assert is_protected_read_path(target) is protected
    assert _has_protected_read_name(target) is protected
    assert protected_read_denial_reason(target) == (
        "protected_name_match" if protected else None
    )


@pytest.mark.parametrize("name", [".env.example.local", ".env.sample.bak", ".env.examples"])
def test_template_marker_must_be_final(name, tmp_path):
    assert is_protected_read_path(tmp_path / name)


@pytest.mark.parametrize("boundary", ["credentials", "identities", "operator", "symlink"])
def test_template_does_not_bypass_other_boundaries(boundary, tmp_path, monkeypatch):
    target = tmp_path / ".env.example"
    if boundary in {"credentials", "identities"}:
        target = tmp_path / boundary / target.name
    elif boundary == "operator":
        monkeypatch.setenv("MIMIR_MCP_SERVERS_PATH", str(target))
    else:
        secret = tmp_path / ".env"
        secret.write_text("ordinary text\n")
        target.symlink_to(secret)
    assert is_protected_read_path(target)
    assert _has_protected_read_name(target)


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


def test_non_admin_attachment_grant_is_scoped_to_the_fetch_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the fetch cache is admitted, not `attachments/` as a whole.

    `attachments/inbound` holds files delivered by whichever channel sent them,
    so a generic home root would let a turn on one channel read another
    channel's uploads. The fetch cache holds tool-produced derivatives of
    content this agent fetched itself, including the extracted PDF text that
    `fetch_url` returns as `text_path`.
    """
    home = tmp_path / "home"
    cache = home / "attachments" / "fetch-cache"
    inbound = home / "attachments" / "inbound"
    (home / "state").mkdir(parents=True)
    cache.mkdir(parents=True)
    inbound.mkdir(parents=True)
    (cache / "extracted.txt").write_text("cached\n", encoding="utf-8")
    (inbound / "upload.txt").write_text("someone else's file\n", encoding="utf-8")
    (home / "attachments" / "loose.txt").write_text("loose\n", encoding="utf-8")
    monkeypatch.setenv("MIMIR_HOME", str(home))

    assert not is_protected_read_path(cache / "extracted.txt")
    assert resolve_non_admin_read_target(
        "/attachments/fetch-cache/extracted.txt", scan_file=True,
    ) is not None

    assert is_protected_read_path(inbound / "upload.txt")
    assert resolve_non_admin_read_target(
        "/attachments/inbound/upload.txt", scan_file=True,
    ) is None

    assert is_protected_read_path(home / "attachments" / "loose.txt")
    assert resolve_non_admin_read_target(
        "/attachments/loose.txt", scan_file=True,
    ) is None
    assert resolve_non_admin_read_target(str(home / "attachments")) is None
