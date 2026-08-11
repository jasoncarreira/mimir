from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from mimir.contained_snapshot import (
    SnapshotCredentialsRefused,
    SnapshotSourceChanged,
    SnapshotUnsafeEntry,
    create_git_snapshot,
    preflight_git_snapshot,
)


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "snapshot@example.invalid")
    git(root, "config", "user.name", "Snapshot Test")
    (root / ".gitignore").write_text("ignored-*\n")
    (root / "tracked.txt").write_text("original\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "seed")
    return root


def source_state(root: Path) -> dict[bytes, tuple[int, int, int, int, int]]:
    result = {}
    root_bytes = os.fsencode(root)
    for directory, names, files in os.walk(root_bytes):
        for name in [*names, *files]:
            path = os.path.join(directory, name)
            value = os.lstat(path)
            result[os.path.relpath(path, root_bytes)] = (
                value.st_uid,
                value.st_gid,
                stat.S_IMODE(value.st_mode),
                value.st_ino,
                value.st_mtime_ns,
            )
    return result


def add_entry(root: Path, inventory: str, name: str, content: bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if inventory == "tracked":
        git(root, "add", "-f", name)
        git(root, "commit", "-qm", f"add {name}")
    elif inventory == "ignored":
        assert name.startswith("ignored-")


@pytest.mark.parametrize("inventory", ["tracked", "untracked", "ignored"])
def test_copies_each_inventory_without_mutating_source(repository: Path, tmp_path: Path, inventory: str) -> None:
    name = "tracked.txt" if inventory == "tracked" else f"{'ignored-' if inventory == 'ignored' else ''}view.txt"
    content = f"{inventory} working view\n".encode()
    if inventory == "tracked":
        (repository / name).write_bytes(content)
    else:
        add_entry(repository, inventory, name, content)
    before = source_state(repository)
    destination = tmp_path / "snapshot"
    result = create_git_snapshot(repository, destination)
    assert (destination / name).read_bytes() == content
    assert getattr(result, f"{inventory}_count") >= 1
    assert source_state(repository) == before


@pytest.mark.parametrize("inventory", ["tracked", "untracked", "ignored"])
def test_env_example_name_exemption_still_scans_content(
    repository: Path, tmp_path: Path, inventory: str
) -> None:
    prefix = "ignored-" if inventory == "ignored" else ""
    basename = ".env.example"
    name = f"{prefix}folder/{basename}" if prefix else f"{inventory}/{basename}"
    if inventory == "ignored":
        (repository / ".gitignore").write_text("ignored-folder/\n")
        git(repository, "add", ".gitignore")
        git(repository, "commit", "-qm", "ignore folder")
    add_entry(repository, inventory, name, b"SETTING=benign\n")
    create_git_snapshot(repository, tmp_path / "benign")
    assert (tmp_path / "benign" / name).read_bytes() == b"SETTING=benign\n"
    (repository / name).write_bytes(b"SERVICE_TOKEN=do-not-copy\n")
    with pytest.raises(SnapshotCredentialsRefused, match="^Snapshot credentials refused$") as error:
        create_git_snapshot(repository, tmp_path / "refused")
    assert name not in str(error.value)
    assert not (tmp_path / "refused").exists()


@pytest.mark.parametrize("inventory", ["tracked", "untracked", "ignored"])
@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("credentials.json", b"ordinary bytes"),
        ("ordinary.txt", b"-----BEGIN PRIVATE KEY-----\nbytes"),
        ("ordinary.txt", b"client_secret: value\n"),
        ("ordinary.txt", b"arbitrary-canary"),
    ],
)
def test_credentials_refused_for_every_inventory_without_disclosure(
    repository: Path,
    tmp_path: Path,
    inventory: str,
    name: str,
    content: bytes,
) -> None:
    relative = f"ignored-{name}" if inventory == "ignored" else f"{inventory}-{name}"
    if name == "credentials.json":
        relative = f"ignored-folder/{name}" if inventory == "ignored" else f"folder/{name}"
        if inventory == "ignored":
            (repository / ".gitignore").write_text("ignored-*\nignored-folder/\n")
    add_entry(repository, inventory, relative, content)
    kwargs = {"known_sensitive": (b"arbitrary-canary",)}
    with pytest.raises(SnapshotCredentialsRefused) as error:
        create_git_snapshot(repository, tmp_path / "snapshot", **kwargs)
    rendered = str(error.value)
    assert relative not in rendered
    assert content.decode(errors="ignore") not in rendered
    assert str(repository) not in rendered
    assert error.value.relative_path_count == 1


def test_preserves_executable_and_safe_relative_symlink(repository: Path, tmp_path: Path) -> None:
    executable = repository / "run"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    os.symlink("run", repository / "link")
    destination = tmp_path / "snapshot"
    create_git_snapshot(repository, destination)
    assert os.access(destination / "run", os.X_OK)
    assert os.readlink(destination / "link") == "run"


def test_overlays_tracked_deletion(repository: Path, tmp_path: Path) -> None:
    (repository / "tracked.txt").unlink()
    destination = tmp_path / "snapshot"
    create_git_snapshot(repository, destination)
    assert not (destination / "tracked.txt").exists()


@pytest.mark.parametrize("target", ["/etc/passwd", "../../outside"])
def test_refuses_unsafe_symlink(repository: Path, tmp_path: Path, target: str) -> None:
    os.symlink(target, repository / "bad-link")
    with pytest.raises(SnapshotUnsafeEntry, match="^Snapshot contains an unsafe entry$"):
        create_git_snapshot(repository, tmp_path / "snapshot")


@pytest.mark.parametrize("inventory", ["untracked", "ignored"])
def test_refuses_special_files(repository: Path, tmp_path: Path, inventory: str) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    name = "ignored-pipe" if inventory == "ignored" else "pipe"
    os.mkfifo(repository / name)
    with pytest.raises(SnapshotUnsafeEntry):
        create_git_snapshot(repository, tmp_path / "snapshot")


def test_refuses_source_change_between_preflight_and_copy(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.contained_snapshot as snapshot

    original = snapshot._overlay_entry
    changed = False

    def mutate_then_overlay(source: bytes, destination: bytes, entry: object) -> None:
        nonlocal changed
        if not changed and getattr(entry, "relative_path") == b"tracked.txt":
            changed = True
            (repository / "tracked.txt").write_text("raced\n")
        original(source, destination, entry)

    monkeypatch.setattr(snapshot, "_overlay_entry", mutate_then_overlay)
    with pytest.raises(SnapshotSourceChanged, match="^Snapshot source changed$"):
        create_git_snapshot(repository, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


def test_inventory_commands_are_nul_delimited(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mimir.contained_snapshot as snapshot

    calls: list[tuple[bytes, ...]] = []
    original = snapshot._run_git

    def capture(source: bytes, args: tuple[bytes, ...]) -> bytes:
        calls.append(args)
        return original(source, args)

    monkeypatch.setattr(snapshot, "_run_git", capture)
    preflight_git_snapshot(repository)
    assert len(calls) == 3
    assert all(b"-z" in call for call in calls)


def test_refuses_destination_beneath_source(repository: Path) -> None:
    with pytest.raises(SnapshotUnsafeEntry):
        create_git_snapshot(repository, repository / "new-snapshot")
    assert not (repository / "new-snapshot").exists()


_SENSITIVE_NAMES = (
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "auth.json",
    "service-account.json",
    "service-account-prod.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".env.local",
    ".env.production",
    ".env.example.backup",
    ".ENV.EXAMPLE",
    "certificate.pem",
    "private.key",
    "archive.p12",
    "archive.pfx",
)


@pytest.mark.parametrize("inventory", ["tracked", "untracked", "ignored"])
@pytest.mark.parametrize("sensitive_name", _SENSITIVE_NAMES)
def test_every_sensitive_name_category_is_refused(
    repository: Path,
    tmp_path: Path,
    inventory: str,
    sensitive_name: str,
) -> None:
    directory = "ignored-folder" if inventory == "ignored" else f"{inventory}-folder"
    relative = f"{directory}/{sensitive_name}"
    add_entry(repository, inventory, relative, b"otherwise benign\n")
    with pytest.raises(SnapshotCredentialsRefused) as error:
        create_git_snapshot(repository, tmp_path / "snapshot")
    assert error.value.relative_path_count == 1
    assert str(error.value) == "Snapshot credentials refused"
    assert relative not in str(error.value)
    assert str(repository) not in str(error.value)
    assert not (tmp_path / "snapshot").exists()


_SECRET_KEYS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "ACCESS_KEY",
    "PRIVATE_KEY",
    "CLIENT_SECRET",
    "REFRESH_TOKEN",
)


@pytest.mark.parametrize("inventory", ["tracked", "untracked", "ignored"])
@pytest.mark.parametrize("secret_key", _SECRET_KEYS)
@pytest.mark.parametrize("separator", ["=", ":"])
def test_every_secret_line_category_is_refused(
    repository: Path,
    tmp_path: Path,
    inventory: str,
    secret_key: str,
    separator: str,
) -> None:
    relative = f"ignored-content-{secret_key}-{ord(separator)}" if inventory == "ignored" else f"content-{inventory}-{secret_key}-{ord(separator)}"
    add_entry(repository, inventory, relative, f"prefix_{secret_key.lower()} {separator} canary\n".encode())
    with pytest.raises(SnapshotCredentialsRefused, match="^Snapshot credentials refused$"):
        create_git_snapshot(repository, tmp_path / "snapshot")


@pytest.mark.parametrize("inventory", ["tracked", "untracked", "ignored"])
def test_known_sensitive_and_private_key_categories_are_refused(
    repository: Path, tmp_path: Path, inventory: str
) -> None:
    first = f"ignored-known-{inventory}" if inventory == "ignored" else f"known-{inventory}"
    add_entry(repository, inventory, first, b"prefix arbitrary projected document suffix")
    with pytest.raises(SnapshotCredentialsRefused):
        create_git_snapshot(
            repository,
            tmp_path / "known-sensitive",
            known_sensitive=(b"arbitrary projected document", b""),
        )
    (repository / first).unlink()
    if inventory == "tracked":
        git(repository, "add", first)
        git(repository, "commit", "-qm", "remove known material")
    second = f"ignored-pem-{inventory}" if inventory == "ignored" else f"pem-{inventory}"
    add_entry(repository, inventory, second, b"-----BEGIN OPENSSH PRIVATE KEY-----\ndata")
    with pytest.raises(SnapshotCredentialsRefused):
        create_git_snapshot(repository, tmp_path / "private-key")


@pytest.mark.parametrize("inventory", ["tracked", "untracked", "ignored"])
def test_unreadable_source_is_fixed_and_non_disclosing(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inventory: str,
) -> None:
    import mimir.contained_snapshot as snapshot

    relative = "tracked.txt" if inventory == "tracked" else ("ignored-unreadable" if inventory == "ignored" else "untracked-unreadable")
    if inventory != "tracked":
        add_entry(repository, inventory, relative, b"benign unreadable content")
    original = snapshot.os.open
    refused_path = os.fsencode(repository / relative)

    def refuse(path: bytes, flags: int, *args: object) -> int:
        if path == refused_path:
            raise PermissionError("revealing unreadable source path")
        return original(path, flags, *args)

    monkeypatch.setattr(snapshot.os, "open", refuse)
    with pytest.raises(SnapshotSourceChanged) as error:
        create_git_snapshot(repository, tmp_path / "snapshot")
    assert str(error.value) == "Snapshot source changed"
    assert str(repository) not in str(error.value)
    assert relative not in str(error.value)


def test_credential_commit_between_preflight_and_clone_is_refused_and_removed(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.contained_snapshot as snapshot

    original = snapshot.subprocess.run
    committed = False

    def commit_before_clone(argv: list[bytes], *args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal committed
        if len(argv) > 1 and argv[1:3] == [b"clone", b"--no-hardlinks"] and not committed:
            committed = True
            (repository / ".env").write_text("benign-looking-content\n")
            git(repository, "add", ".env")
            git(repository, "commit", "-qm", "credential race")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(snapshot.subprocess, "run", commit_before_clone)
    with pytest.raises(SnapshotCredentialsRefused, match="^Snapshot credentials refused$") as error:
        create_git_snapshot(repository, tmp_path / "snapshot")
    assert error.value.relative_path_count == 1
    assert ".env" not in str(error.value)
    assert not (tmp_path / "snapshot").exists()


def test_untracked_inventory_addition_during_overlay_is_refused_and_removed(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.contained_snapshot as snapshot

    original = snapshot._overlay_entry
    added = False

    def add_then_overlay(source: bytes, destination: bytes, entry: object) -> None:
        nonlocal added
        if not added:
            added = True
            (repository / "late-file").write_text("late benign content\n")
        original(source, destination, entry)

    monkeypatch.setattr(snapshot, "_overlay_entry", add_then_overlay)
    with pytest.raises(SnapshotSourceChanged, match="^Snapshot source changed$"):
        create_git_snapshot(repository, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


def test_head_change_without_inventory_change_is_refused_and_removed(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.contained_snapshot as snapshot

    original = snapshot._overlay_entry
    committed = False

    def commit_then_overlay(source: bytes, destination: bytes, entry: object) -> None:
        nonlocal committed
        if not committed:
            committed = True
            git(repository, "commit", "--allow-empty", "-qm", "head race")
        original(source, destination, entry)

    monkeypatch.setattr(snapshot, "_overlay_entry", commit_then_overlay)
    with pytest.raises(SnapshotSourceChanged, match="^Snapshot source changed$"):
        create_git_snapshot(repository, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


def test_unavailable_and_unsafe_errors_are_fixed_and_non_disclosing(tmp_path: Path) -> None:
    source = tmp_path / "not-a-repository"
    source.mkdir()
    with pytest.raises(Exception) as unavailable:
        create_git_snapshot(source, tmp_path / "snapshot")
    assert type(unavailable.value).__name__ == "SnapshotUnavailable"
    assert str(unavailable.value) == "Snapshot unavailable"
    assert str(source) not in str(unavailable.value)

    git(source, "init", "-q")
    git(source, "config", "user.email", "snapshot@example.invalid")
    git(source, "config", "user.name", "Snapshot Test")
    (source / "seed").write_text("seed")
    git(source, "add", "seed")
    git(source, "commit", "-qm", "seed")
    os.symlink("../../revealing-target", source / "bad")
    with pytest.raises(SnapshotUnsafeEntry) as unsafe:
        create_git_snapshot(source, tmp_path / "unsafe-snapshot")
    assert str(unsafe.value) == "Snapshot contains an unsafe entry"
    assert "revealing-target" not in str(unsafe.value)
    assert str(source) not in str(unsafe.value)


@pytest.mark.parametrize("inventory", ["tracked", "untracked", "ignored"])
def test_executable_and_safe_symlink_are_preserved_for_every_inventory(
    repository: Path, tmp_path: Path, inventory: str
) -> None:
    executable_name = "ignored-executable" if inventory == "ignored" else f"{inventory}-executable"
    link_name = "ignored-safe-link" if inventory == "ignored" else f"{inventory}-safe-link"
    executable = repository / executable_name
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    os.symlink(executable_name, repository / link_name)
    if inventory == "tracked":
        git(repository, "add", executable_name, link_name)
        git(repository, "commit", "-qm", "add executable and link")
    destination = tmp_path / "snapshot"
    create_git_snapshot(repository, destination)
    assert os.access(destination / executable_name, os.X_OK)
    assert os.readlink(destination / link_name) == executable_name


def test_secret_key_with_empty_value_is_benign(repository: Path, tmp_path: Path) -> None:
    (repository / "empty-value").write_bytes(b"SERVICE_TOKEN=   \n")
    create_git_snapshot(repository, tmp_path / "snapshot")
    assert (tmp_path / "snapshot" / "empty-value").read_bytes() == b"SERVICE_TOKEN=   \n"


@pytest.mark.parametrize("inventory", ["tracked", "untracked", "ignored"])
def test_env_example_parent_component_is_not_exempt(
    repository: Path, tmp_path: Path, inventory: str
) -> None:
    base = "ignored-folder" if inventory == "ignored" else f"{inventory}-folder"
    relative = f"{base}/.env.example/benign.txt"
    add_entry(repository, inventory, relative, b"benign content")
    with pytest.raises(SnapshotCredentialsRefused, match="^Snapshot credentials refused$"):
        create_git_snapshot(repository, tmp_path / "snapshot")
