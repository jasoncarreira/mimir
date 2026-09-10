from __future__ import annotations

from pathlib import Path

import pytest

from mimir.read_policy import (
    _has_protected_read_name,
    configured_non_admin_read_roots,
    derived_pr_checkout_read_root,
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


@pytest.mark.parametrize("flag", [None, "0", "false", "invalid", "1", "true"])
def test_derived_lease_read_root_is_coding_only(flag, tmp_path, monkeypatch):
    from mimir.access_control import _configured_file_write_roots

    home = tmp_path / "home"
    home.mkdir()
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", "")
    monkeypatch.delenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", raising=False)
    if flag is None:
        monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MIMIR_CODING_ENABLED", flag)
    baseline = configured_non_admin_read_roots()
    write_roots = _configured_file_write_roots()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))

    enabled = flag in {"1", "true"}
    assert derived_pr_checkout_read_root() == (lease_root if enabled else None)
    roots = configured_non_admin_read_roots()
    assert (lease_root in roots) is enabled
    assert tuple(root for root in roots if root != lease_root) == baseline
    assert _configured_file_write_roots() == write_roots


@pytest.mark.parametrize("invalid", [
    "unset", "empty", "relative", "missing", "file", "symlink", "loop",
    "system", "home", "traversal", "mode",
])
def test_invalid_lease_root_does_not_extend_read_policy(invalid, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    leases = tmp_path / "leases"
    leases.mkdir()
    regular = tmp_path / "file"
    regular.write_text("not a directory")
    alias = tmp_path / "alias"
    alias.symlink_to(leases, target_is_directory=True)
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    values = {
        "empty": " ", "relative": "leases", "missing": str(tmp_path / "missing"),
        "file": str(regular), "symlink": str(alias), "loop": str(loop),
        "system": "/", "home": str(home),
        "traversal": str(leases / ".." / "leases"), "mode": f"{leases}:rw",
    }
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", "")
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    monkeypatch.delenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", raising=False)
    baseline = configured_non_admin_read_roots()
    if invalid != "unset":
        monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", values[invalid])
    assert derived_pr_checkout_read_root() is None
    assert configured_non_admin_read_roots() == baseline
    assert not (tmp_path / "missing").exists()


def test_derived_lease_known_root_preserves_absolute_paths(tmp_path, monkeypatch):
    import mimir.read_policy as policy

    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    physical = tmp_path / "physical"
    (physical / "leases").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    root = physical / "leases"
    lexical = alias / "leases"
    target = root / "source.py"
    target.write_text("print('public')\n")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", "")
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lexical))
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    roots = configured_non_admin_read_roots()
    assert root in roots and lexical in roots
    # Isolate derived-root routing from the unrelated broad /tmp default.
    monkeypatch.setattr(policy, "configured_non_admin_read_roots", lambda: tuple(
        path for path in configured_non_admin_read_roots() if path != Path("/tmp")
    ))
    for requested in (target, lexical / target.name):
        assert policy.resolved_read_target_from_arguments(
            "read_file", {"file_path": str(requested)},
        ) == str(target)
        assert resolve_non_admin_read_target(str(requested), scan_file=True) == target
    outside = physical / "outside.txt"
    outside.write_text("private\n")
    (root / "escape").symlink_to(outside)
    assert resolve_non_admin_read_target(str(root / "escape"), scan_file=True) is None
    assert resolve_non_admin_read_target(str(root / ".." / outside.name)) is None
    protected = root / ".env"
    protected.write_text("private\n")
    assert resolve_non_admin_read_target(str(protected), scan_file=True) is None
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "0")
    assert policy.resolved_read_target_from_arguments(
        "read_file", {"file_path": str(target)},
    ) == str(home / str(target).lstrip("/"))


@pytest.mark.parametrize("escape", ["symlink", "traversal"])
def test_derived_lease_read_containment(escape, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    physical = tmp_path / "physical"
    root = physical / "leases"
    root.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    lexical = alias / "leases"
    outside = physical / "outside.txt"
    outside.write_text("outside lease\n")
    (root / "escape").symlink_to(outside)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", "")
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lexical))
    requested = lexical / ("escape" if escape == "symlink" else "../outside.txt")
    # Keep /tmp enabled: a missing narrow lexical root must not fall back to it.
    assert resolve_non_admin_read_target(str(requested), scan_file=True) is None


def test_derived_lease_does_not_expand_write_configuration(tmp_path, monkeypatch):
    import os

    from mimir.access_control import _configured_file_write_roots

    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", "")
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(root))
    before = _configured_file_write_roots()
    assert root in configured_non_admin_read_roots()
    assert _configured_file_write_roots() == before
    assert os.environ["MIMIR_FILE_TOOL_ROOTS"] == ""


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
