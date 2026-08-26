from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

import mimir._rmtree as rmtree_module
import mimir.worklink.safe_git as safe_git_module
from mimir.worklink.safe_git import ControllerGitPublication, SafeGitError


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=check,
        text=True,
    )


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Trusted User")
    _git(repo, "config", "user.email", "trusted@example.test")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "tracked.txt").write_text("initial\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    branch = _git(repo, "branch", "--show-current").stdout.strip()
    return repo, remote, branch


def _capture(repo: Path, branch: str, tmp_path: Path) -> tuple[ControllerGitPublication, int]:
    checkout_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    try:
        publication = ControllerGitPublication.capture(
            checkout_fd,
            trusted_repo=repo,
            branch=branch,
            metadata_root=tmp_path / "metadata",
        )
    except BaseException:
        os.close(checkout_fd)
        raise
    return publication, checkout_fd


def _close(publication: ControllerGitPublication, checkout_fd: int) -> None:
    publication.close()
    os.close(checkout_fd)


def test_new_test_repository_disables_auto_maintenance(tmp_path: Path) -> None:
    repo = tmp_path / "repository"
    _git(tmp_path, "init", "-q", str(repo))

    assert _git(repo, "config", "--get", "maintenance.auto").stdout.strip() == "false"


def test_publication_close_tolerates_entry_removed_mid_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, branch = _repository(tmp_path)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    race = publication.metadata_path / "race"
    race.mkdir()
    first = race / "first"
    second = race / "maintenance.lock"
    first.write_text("first\n")
    second.write_text("lock\n")
    real_unlink = os.unlink
    raced = False

    def unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        nonlocal raced
        name = os.fsdecode(path)
        if not raced and name in {first.name, second.name}:
            raced = True
            other = second if name == first.name else first
            real_unlink(other)
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(safe_git_module.os, "unlink", unlink)
    try:
        publication.close()
    finally:
        os.close(checkout_fd)

    assert raced
    assert not publication.metadata_path.exists()


def test_publication_close_does_not_swallow_other_rmtree_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, branch = _repository(tmp_path)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    real_rmtree = rmtree_module.shutil.rmtree
    failure = PermissionError("permission denied")

    def rmtree(_path: object, *, onerror: object) -> None:
        onerror(os.unlink, str(publication.metadata_path), (PermissionError, failure, None))

    monkeypatch.setattr(rmtree_module.shutil, "rmtree", rmtree)
    try:
        with pytest.raises(PermissionError, match="permission denied"):
            publication.close()
    finally:
        monkeypatch.undo()
        real_rmtree(publication.metadata_path)
        os.close(checkout_fd)

    assert not publication._closed


def test_publication_uses_private_index_refs_and_objects(tmp_path: Path) -> None:
    repo, remote, branch = _repository(tmp_path)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    checkout_config = (repo / ".git" / "config").read_bytes()
    (repo / "tracked.txt").write_text("worker change\n")
    try:
        publication.run("add", "tracked.txt", check=True)
        publication.run("commit", "-m", "controller commit", check=True)
        private_head = publication.run("rev-parse", "HEAD", check=True).stdout.strip()
        checkout_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        publication.push(check=True)
    finally:
        metadata_path = publication.metadata_path
        _close(publication, checkout_fd)

    assert private_head != checkout_head
    assert _git(remote, "rev-parse", f"refs/heads/{branch}").stdout.strip() == private_head
    assert (repo / ".git" / "config").read_bytes() == checkout_config
    assert _git(repo, "status", "--short").stdout == " M tracked.txt\n"
    assert not metadata_path.exists()


def test_publication_ignores_hostile_checkout_config_and_hooks(tmp_path: Path) -> None:
    repo, _, branch = _repository(tmp_path)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    canary = tmp_path / "hook-ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {canary}\nexit 9\n")
    hook.chmod(0o755)
    hostile_include = tmp_path / "hostile-config"
    hostile_include.write_text("[alias]\nattack = !false\n")
    _git(repo, "config", "core.hooksPath", str(hooks))
    _git(repo, "config", "include.path", str(hostile_include))
    (repo / "tracked.txt").write_text("worker change\n")
    try:
        publication.run("add", "tracked.txt", check=True)
        publication.run("commit", "-m", "safe commit", check=True)
    finally:
        _close(publication, checkout_fd)

    assert not canary.exists()
    config = (repo / ".git" / "config").read_text()
    assert "hooksPath" in config
    assert "[include]" in config


def test_publication_uses_only_captured_ordered_helpers(tmp_path: Path) -> None:
    repo, _, branch = _repository(tmp_path)
    first = "!f() { echo username=trusted; }; f"
    second = "!f() { echo password=trusted-secret; }; f"
    hostile = "!f() { echo username=hostile; echo password=hostile; }; f"
    _git(repo, "config", "--add", "credential.helper", first)
    _git(repo, "config", "--add", "credential.helper", second)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    _git(repo, "config", "--unset-all", "credential.helper")
    _git(repo, "config", "credential.helper", hostile)
    try:
        result = publication.run(
            "credential",
            "fill",
            input="protocol=https\nhost=example.test\n\n",
            check=True,
        )
    finally:
        _close(publication, checkout_fd)

    assert publication.credential_settings[-2:] == (
        ("credential.helper", first),
        ("credential.helper", second),
    )
    assert "username=trusted" in result.stdout
    assert "password=trusted-secret" in result.stdout
    assert "hostile" not in result.stdout


def test_publication_uses_a_url_scoped_credential_helper(tmp_path: Path) -> None:
    """A helper written under ``credential.<url>.helper`` must reach the push.

    ``gh auth setup-git`` writes exactly this shape, so reading only the unscoped
    ``credential.helper`` captured nothing on a normally configured host and every
    publication pushed unauthenticated.
    """
    repo, _, branch = _repository(tmp_path)
    helper = "!f() { echo username=scoped; echo password=scoped-secret; }; f"
    _git(repo, "config", "--add", "credential.https://example.test.helper", helper)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    try:
        result = publication.run(
            "credential",
            "fill",
            input="protocol=https\nhost=example.test\n\n",
            check=True,
        )
    finally:
        _close(publication, checkout_fd)

    assert ("credential.https://example.test.helper", helper) in publication.credential_settings
    assert "username=scoped" in result.stdout
    assert "password=scoped-secret" in result.stdout


def test_publication_keeps_a_scoped_helper_off_other_hosts(tmp_path: Path) -> None:
    """Scoping is preserved, so one host's helper is never offered to another."""
    repo, _, branch = _repository(tmp_path)
    helper = "!f() { echo username=elsewhere; echo password=elsewhere-secret; }; f"
    _git(repo, "config", "--add", "credential.https://elsewhere.test.helper", helper)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    try:
        # No helper claims example.test, so the fill cannot complete — that failure is
        # the point. check=False because refusing to supply another host's credential
        # is the correct outcome, not an error in the test.
        result = publication.run(
            "credential",
            "fill",
            input="protocol=https\nhost=example.test\n\n",
            check=False,
        )
    finally:
        _close(publication, checkout_fd)

    assert ("credential.https://elsewhere.test.helper", helper) in publication.credential_settings
    assert "elsewhere-secret" not in result.stdout
    assert result.returncode != 0
    # The failure must name the real cause, not a bogus askpass exec.
    assert "cannot exec" not in result.stderr


def test_publication_askpass_never_names_an_unexecutable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git execs the askpass value, so it must not name a non-executable file.

    Pointing it at /dev/null made a missing credential surface as
    "cannot exec '/dev/null': Permission denied", which named the wrong cause.
    """
    repo, _, branch = _repository(tmp_path)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("GIT_ASKPASS", "/tmp/worker-askpass")
    monkeypatch.setenv("SSH_ASKPASS", "/tmp/worker-ssh-askpass")
    monkeypatch.setattr(safe_git_module.subprocess, "run", run)
    try:
        publication.run("status", check=True)
    finally:
        monkeypatch.undo()
        _close(publication, checkout_fd)

    environment = captured["environment"]
    # The worker's value must not survive, and what replaces it must not be a path
    # Git would try to exec.
    assert environment["GIT_ASKPASS"] == ""
    assert environment["SSH_ASKPASS"] == ""
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_push_uses_captured_literal_url_and_exact_branch_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, branch = _repository(tmp_path)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "hostile.git"))
    captured: list[list[str]] = []
    real_run = safe_git_module.subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(safe_git_module.subprocess, "run", run)
    try:
        publication.push(check=True)
    finally:
        monkeypatch.setattr(safe_git_module.subprocess, "run", real_run)
        _close(publication, checkout_fd)

    command = captured[-1]
    assert command[-3:] == ["push", str(remote), f"HEAD:refs/heads/{branch}"]
    assert "origin" not in command
    assert not any(value == "credential.helper=" for value in command)


def test_publication_fails_closed_when_git_directory_is_replaced(tmp_path: Path) -> None:
    repo, _, branch = _repository(tmp_path)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    (repo / ".git").rename(repo / ".git-original")
    (repo / ".git").mkdir()
    try:
        with pytest.raises(SafeGitError, match="replaced"):
            publication.run("status", "--short")
    finally:
        _close(publication, checkout_fd)


def test_publication_fails_closed_when_object_database_is_replaced(tmp_path: Path) -> None:
    repo, _, branch = _repository(tmp_path)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    (repo / ".git" / "objects").rename(repo / ".git" / "objects-original")
    (repo / ".git" / "objects").mkdir()
    try:
        with pytest.raises(SafeGitError, match="object database was replaced"):
            publication.run("status", "--short")
    finally:
        _close(publication, checkout_fd)


def test_publication_scrubs_git_prompt_and_config_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, branch = _repository(tmp_path)
    publication, checkout_fd = _capture(repo, branch, tmp_path)
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'alias.attack=!false'")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/worker-askpass")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/worker-agent")
    monkeypatch.setattr(safe_git_module.subprocess, "run", run)
    try:
        publication.run("status", check=True)
    finally:
        monkeypatch.undo()
        _close(publication, checkout_fd)

    command = captured["command"]
    environment = captured["environment"]
    assert "core.hooksPath=/dev/null" in command
    assert "credential.helper=" not in command
    assert "GIT_CONFIG_PARAMETERS" not in environment
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_ASKPASS"] == ""
    assert "SSH_AUTH_SOCK" not in environment


def test_capture_rejects_invalid_branch_and_missing_remote(tmp_path: Path) -> None:
    repo, _, branch = _repository(tmp_path)
    checkout_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(SafeGitError, match="branch is invalid"):
            ControllerGitPublication.capture(
                checkout_fd,
                trusted_repo=repo,
                branch="../escape",
                metadata_root=tmp_path / "metadata",
            )
        _git(repo, "remote", "remove", "origin")
        with pytest.raises(SafeGitError, match="URL is unavailable"):
            ControllerGitPublication.capture(
                checkout_fd,
                trusted_repo=repo,
                branch=branch,
                metadata_root=tmp_path / "metadata",
            )
    finally:
        os.close(checkout_fd)
