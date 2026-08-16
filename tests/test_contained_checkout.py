from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest

import mimir.contained_checkout as contained_checkout
import mimir.worklink.checkout as checkout
from mimir.contained_checkout import create_opencode_checkout, create_repo_test_checkout
from mimir.contained_snapshot import SnapshotCredentialsRefused


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    repo_test = tmp_path / "repo-test-checkouts"
    opencode = tmp_path / "opencode-checkouts"
    repo_test.mkdir(mode=0o771)
    opencode.mkdir(mode=0o771)
    repo_test.chmod(0o771)
    opencode.chmod(0o771)
    monkeypatch.setattr(contained_checkout, "REPO_TEST_CHECKOUT_ROOT", repo_test)
    monkeypatch.setattr(contained_checkout, "OPENCODE_CHECKOUT_ROOT", opencode)
    monkeypatch.setattr(checkout, "_REPO_TEST_CHECKOUT_ROOT", repo_test)
    monkeypatch.setattr(checkout, "_OPENCODE_CHECKOUT_ROOT", opencode)
    monkeypatch.setattr(contained_checkout.os, "chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(contained_checkout.os, "fchown", lambda *args, **kwargs: None)
    return repo_test, opencode


def test_repo_test_checkout_snapshots_without_mutating_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _roots(tmp_path, monkeypatch)
    source = _repo(tmp_path)
    (source / "tracked.txt").write_text("changed\n")
    (source / "untracked.txt").write_text("new\n")
    before = source.stat()

    issued = create_repo_test_checkout(source, scope_id="owner/repo", pr_number=41)

    after = source.stat()
    assert (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) == (
        after.st_uid,
        after.st_gid,
        stat.S_IMODE(after.st_mode),
    )
    assert (issued.path / "tracked.txt").read_text() == "changed\n"
    assert (issued.path / "untracked.txt").read_text() == "new\n"
    relative = issued.path.relative_to(root)
    assert len(relative.parts) == 3
    assert len(relative.parts[0]) == 64
    assert relative.parts[1].startswith("41-")
    assert stat.S_IMODE(issued.path.stat().st_mode) == 0o2770
    boundary = issued.path.parent
    issued.close()
    assert not boundary.exists()
    assert list(root.iterdir()) == []


def test_prepare_boundary_refuses_collision_without_deleting_incumbent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _roots(tmp_path, monkeypatch)
    scope = "scope"
    boundary = root / scope / "41-7"
    checkout_path = boundary / "checkout"
    checkout_path.mkdir(parents=True)
    canary = checkout_path / "live-worker"
    canary.write_text("running\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        contained_checkout._prepare_boundary(root, scope, "41-7")

    assert canary.read_text(encoding="utf-8") == "running\n"


def test_repo_test_checkout_normalizes_venv_without_widening_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roots(tmp_path, monkeypatch)
    source = _repo(tmp_path)
    venv = source / ".venv" / "bin"
    venv.mkdir(parents=True, mode=0o700)
    runner = venv / "pytest"
    runner.write_text("#!/bin/sh\n", encoding="utf-8")
    data = venv / "metadata"
    data.write_text("data\n", encoding="utf-8")
    (source / ".venv").chmod(0o700)
    venv.chmod(0o700)
    runner.chmod(0o700)
    data.chmod(0o600)

    issued = create_repo_test_checkout(source, scope_id="owner/repo", pr_number=44)

    copied_venv = issued.path / ".venv"
    copied_runner = copied_venv / "bin" / "pytest"
    copied_data = copied_venv / "bin" / "metadata"
    boundary = issued.path.parent
    assert stat.S_IMODE(boundary.stat().st_mode) == 0o700
    assert stat.S_IMODE(boundary.stat().st_mode) & stat.S_IXGRP == 0
    assert stat.S_IMODE(copied_venv.stat().st_mode) == 0o2770
    assert stat.S_IMODE(copied_runner.stat().st_mode) == 0o770
    assert stat.S_IMODE(copied_data.stat().st_mode) == 0o660
    assert all(
        stat.S_IMODE(path.stat().st_mode) & 0o007 == 0
        for path in (copied_venv, copied_runner, copied_data)
    )
    issued.close()


def test_repo_test_checkout_allows_tracked_credential_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roots(tmp_path, monkeypatch)
    source = _repo(tmp_path)
    (source / "tracked.txt").write_text("SERVICE_TOKEN=fixture-value\n")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-q", "-m", "credential-shaped fixture")

    issued = create_repo_test_checkout(source, scope_id="owner/repo", pr_number=42)

    assert (issued.path / "tracked.txt").read_text() == "SERVICE_TOKEN=fixture-value\n"
    issued.close()


@pytest.mark.parametrize("inventory", ["untracked", "ignored"])
def test_repo_test_checkout_still_refuses_nontracked_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inventory: str
) -> None:
    _roots(tmp_path, monkeypatch)
    source = _repo(tmp_path)
    directory = source / inventory
    directory.mkdir()
    if inventory == "ignored":
        (source / ".gitignore").write_text("ignored/\n")
        _git(source, "add", ".gitignore")
        _git(source, "commit", "-q", "-m", "ignore local files")
    (directory / ".env.local").write_text("SERVICE_TOKEN=do-not-copy\n")
    (directory / "credentials.json").write_text("fixture\n")

    with pytest.raises(SnapshotCredentialsRefused) as error:
        create_repo_test_checkout(source, scope_id="owner/repo", pr_number=43)

    assert error.value.reason_code == "snapshot_credentials"
    assert error.value.relative_path_count == 2
    assert str(error.value) == "Snapshot credentials refused"
    assert ".env.local" not in str(error.value)
    assert "do-not-copy" not in str(error.value)


def test_opencode_checkout_still_refuses_tracked_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roots(tmp_path, monkeypatch)
    default = tmp_path / "projects"
    default.mkdir()
    source = _repo(default)
    (source / "tracked.txt").write_text("SERVICE_TOKEN=fixture-value\n")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-q", "-m", "credential-shaped fixture")

    with pytest.raises(SnapshotCredentialsRefused) as error:
        create_opencode_checkout(source, default_cwd=default)

    assert error.value.reason_code == "snapshot_credentials"
    assert error.value.relative_path_count == 1


def test_opencode_checkout_has_fixed_seed_commit_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root = _roots(tmp_path, monkeypatch)
    default = tmp_path / "projects"
    default.mkdir()
    source = _repo(default)
    (source / "tracked.txt").write_text("working view\n")

    issued = create_opencode_checkout(source, default_cwd=default)

    assert _git(issued.path, "status", "--porcelain") == ""
    assert issued.base_tree == _git(issued.path, "rev-parse", "HEAD^{tree}")
    relative = issued.path.relative_to(root)
    assert len(relative.parts) == 3
    assert relative.parts[1].endswith("-1")
    assert (issued.path / "tracked.txt").read_text() == "working view\n"
    boundary = issued.path.parent
    issued.close()
    assert not boundary.exists()
    assert list(root.iterdir()) == []


def test_opencode_checkout_refuses_seed_outside_default_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roots(tmp_path, monkeypatch)
    source = _repo(tmp_path)
    default = tmp_path / "elsewhere"
    default.mkdir()

    with pytest.raises(ValueError, match="outside"):
        create_opencode_checkout(source, default_cwd=default)


@pytest.mark.parametrize("surface", ["repo_test", "opencode"])
def test_checkout_provisioning_mutates_only_its_admitted_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    repo_test_root, opencode_root = _roots(tmp_path, monkeypatch)
    default = tmp_path / "projects"
    default.mkdir()
    source = _repo(default)
    (source / "tracked.txt").write_text("working view\n")
    source_before = _tree_signature(source)
    mutations: list[tuple[str, tuple[int, int], tuple[int, int] | int]] = []
    real_chmod = os.chmod
    real_fchmod = os.fchmod
    real_stat = os.stat
    real_fstat = os.fstat

    def chown(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        uid: int,
        gid: int,
        **kwargs: object,
    ) -> None:
        observed = real_stat(path, **kwargs)
        mutations.append(("chown", (observed.st_dev, observed.st_ino), (uid, gid)))

    def chmod(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int,
        **kwargs: object,
    ) -> None:
        observed = real_stat(path, **kwargs)
        mutations.append(("chmod", (observed.st_dev, observed.st_ino), mode))
        real_chmod(path, mode, **kwargs)

    def fchown(fd: int, uid: int, gid: int) -> None:
        observed = real_fstat(fd)
        mutations.append(("fchown", (observed.st_dev, observed.st_ino), (uid, gid)))

    def fchmod(fd: int, mode: int) -> None:
        observed = real_fstat(fd)
        mutations.append(("fchmod", (observed.st_dev, observed.st_ino), mode))
        real_fchmod(fd, mode)

    monkeypatch.setattr(contained_checkout.os, "chown", chown)
    monkeypatch.setattr(contained_checkout.os, "chmod", chmod)
    monkeypatch.setattr(contained_checkout.os, "fchown", fchown)
    monkeypatch.setattr(contained_checkout.os, "fchmod", fchmod)

    if surface == "repo_test":
        root = repo_test_root
        issued = create_repo_test_checkout(source, scope_id="owner/repo", pr_number=41)
    else:
        root = opencode_root
        issued = create_opencode_checkout(source, default_cwd=default)

    root_identities = _tree_identities(root)
    assert mutations
    assert all(identity in root_identities for _operation, identity, _value in mutations)
    assert _tree_signature(source) == source_before
    scope = issued.path.parent.parent
    boundary = issued.path.parent
    assert stat.S_IMODE(root.stat().st_mode) == 0o771
    assert stat.S_IMODE(scope.stat().st_mode) == 0o700
    assert stat.S_IMODE(boundary.stat().st_mode) == 0o700
    assert stat.S_IMODE(issued.path.stat().st_mode) == 0o2770
    scope_identity = _identity(scope)
    boundary_identity = _identity(boundary)
    checkout_identity = _identity(issued.path)
    assert ("chown", scope_identity, (1001, 1001)) in mutations
    assert ("chown", boundary_identity, (1001, 1002)) in mutations
    assert ("fchown", checkout_identity, (1001, 1002)) in mutations
    assert ("fchmod", checkout_identity, 0o2770) in mutations
    issued.close()


def _identity(path: Path) -> tuple[int, int]:
    observed = path.stat(follow_symlinks=False)
    return observed.st_dev, observed.st_ino


def _tree_identities(root: Path) -> set[tuple[int, int]]:
    return {_identity(root), *(_identity(path) for path in root.rglob("*"))}


def _tree_signature(root: Path) -> dict[str, tuple[int, int, int]]:
    result: dict[str, tuple[int, int, int]] = {}
    paths = [root]
    paths.extend(
        path for path in root.rglob("*") if ".git" not in path.relative_to(root).parts
    )
    for path in paths:
        observed = path.stat(follow_symlinks=False)
        result[str(path.relative_to(root))] = (
            observed.st_uid,
            observed.st_gid,
            stat.S_IMODE(observed.st_mode),
        )
    return result
