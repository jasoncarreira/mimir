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
