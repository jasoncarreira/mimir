from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest

import mimir.contained_checkout as contained_checkout
import mimir.worklink.checkout as checkout
from mimir.contained_checkout import create_opencode_checkout, create_repo_test_checkout


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
    monkeypatch.setattr(contained_checkout, "REPO_TEST_CHECKOUT_ROOT", repo_test)
    monkeypatch.setattr(contained_checkout, "OPENCODE_CHECKOUT_ROOT", opencode)
    monkeypatch.setattr(checkout, "_REPO_TEST_CHECKOUT_ROOT", repo_test)
    monkeypatch.setattr(checkout, "_OPENCODE_CHECKOUT_ROOT", opencode)
    monkeypatch.setattr(contained_checkout, "MIMIR_UID", os.getuid())
    monkeypatch.setattr(contained_checkout, "WORKLINK_GID", os.getgid())
    monkeypatch.setattr(contained_checkout.os, "chown", lambda *args, **kwargs: None)
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
    assert issued.path.read_text if False else (issued.path / "tracked.txt").read_text() == "changed\n"
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
