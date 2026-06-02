"""Tests for version-triggered defaults-upgrade proposals (chainlink #349)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import mimir.defaults_upgrade as du
from mimir.proposals import list_open_proposals


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check
    )


def _init(path: Path, *, bare: bool = False) -> None:
    if bare:
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(path)], check=True)
        return
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "t@t", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    _git("config", "commit.gpgsign", "false", cwd=path)


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    up = tmp_path / "upstream.git"
    up.mkdir()
    _init(up, bare=True)
    return up


@pytest.fixture
def home(tmp_path: Path, upstream: Path) -> Path:
    h = tmp_path / "home"
    (h / "memory" / "core").mkdir(parents=True)
    (h / "prompts").mkdir()
    (h / "memory" / "core" / "00-identity.md").write_text("identity v1\n", encoding="utf-8")
    (h / "prompts" / "heartbeat.md").write_text("heartbeat v1\n", encoding="utf-8")
    (h / "state").mkdir()
    (h / "state" / "note.md").write_text("home state\n", encoding="utf-8")
    (h / ".gitignore").write_text(
        "*\n!*/\n!memory/**\n!prompts/**\n!state/**\n!.gitignore\nscratch/\n",
        encoding="utf-8",
    )
    _init(h)
    _git("add", "-A", cwd=h)
    _git("commit", "-q", "-m", "seed home", cwd=h)
    _git("remote", "add", "origin", str(upstream), cwd=h)
    _git("push", "-q", "-u", "origin", "main", cwd=h)
    return h


def _defaults(monkeypatch: pytest.MonkeyPatch, *, identity: str, heartbeat: str) -> None:
    monkeypatch.setattr(du, "bundled_core_defaults", lambda: {"00-identity.md": identity})
    monkeypatch.setattr(du, "bundled_prompt_defaults", lambda: {"heartbeat.md": heartbeat})


def _cleanup_upgrade_proposal(home: Path) -> None:
    opens = list_open_proposals(home, lane="upgrade")
    for branch, wt in opens:
        _git("worktree", "remove", "--force", str(wt), cwd=home, check=False)
        _git("branch", "-D", branch, cwd=home, check=False)
    _git("worktree", "prune", cwd=home, check=False)


def test_skip_without_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = tmp_path / "h"
    h.mkdir()
    _init(h)
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    result = du.check_and_open_defaults_upgrade(h, version="1.0.0")
    assert result.ok and result.action == "skip_no_remote"
    assert not (h / du.LAST_SYNCED_VERSION_FILE).exists()


def test_first_run_initializes_vendor_baseline_only(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")

    result = du.check_and_open_defaults_upgrade(home, version="1.0.0")

    assert result.ok and result.action == "baseline_initialized"
    assert (home / du.LAST_SYNCED_VERSION_FILE).read_text(encoding="utf-8") == "1.0.0\n"
    assert list_open_proposals(home, lane="upgrade") == []
    files = set(_git("ls-tree", "-r", "--name-only", du.DEFAULTS_VENDOR_BRANCH, cwd=home).stdout.splitlines())
    assert files == {"memory/core/00-identity.md", "prompts/heartbeat.md"}
    assert _git("show", f"{du.DEFAULTS_VENDOR_BRANCH}:memory/core/00-identity.md", cwd=home).stdout == "identity v1\n"
    assert _git("status", "--porcelain", cwd=home).stdout.strip() == ""


def test_same_version_is_noop(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"

    result = du.check_and_open_defaults_upgrade(home, version="1.0.0")

    assert result.ok and result.action == "already_synced"
    assert list_open_proposals(home, lane="upgrade") == []


def test_new_defaults_open_clean_upgrade_proposal(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"
    _defaults(monkeypatch, identity="identity v2\n", heartbeat="heartbeat v2\n")

    result = du.check_and_open_defaults_upgrade(home, version="1.1.0")

    assert result.ok and result.action == "proposal_opened"
    assert result.proposal and result.proposal.worktree
    assert result.proposal.branch and result.proposal.branch.startswith("upgrade/defaults-1-1-0-")
    assert (home / du.LAST_SYNCED_VERSION_FILE).read_text(encoding="utf-8") == "1.1.0\n"
    wt = result.proposal.worktree
    assert (wt / "memory" / "core" / "00-identity.md").read_text(encoding="utf-8") == "identity v2\n"
    assert (wt / "prompts" / "heartbeat.md").read_text(encoding="utf-8") == "heartbeat v2\n"
    staged = _git("diff", "--cached", "--name-only", cwd=wt).stdout.splitlines()
    assert staged == ["memory/core/00-identity.md", "prompts/heartbeat.md"]
    # Live home files remain operator-owned until the proposal PR is merged.
    assert (home / "memory" / "core" / "00-identity.md").read_text(encoding="utf-8") == "identity v1\n"


def test_version_changed_but_defaults_same_records_no_changes(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"

    result = du.check_and_open_defaults_upgrade(home, version="1.1.0")

    assert result.ok and result.action == "no_changes"
    assert (home / du.LAST_SYNCED_VERSION_FILE).read_text(encoding="utf-8") == "1.1.0\n"
    assert list_open_proposals(home, lane="upgrade") == []


def test_operator_edit_conflicts_are_left_in_upgrade_worktree(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"
    (home / "memory" / "core" / "00-identity.md").write_text("operator identity edit\n", encoding="utf-8")
    _git("add", "memory/core/00-identity.md", cwd=home)
    _git("commit", "-q", "-m", "operator identity edit", cwd=home)
    _git("push", "-q", cwd=home)
    _defaults(monkeypatch, identity="identity v2\n", heartbeat="heartbeat v1\n")

    result = du.check_and_open_defaults_upgrade(home, version="1.1.0")

    assert result.ok and result.action == "proposal_opened_conflicts"
    assert result.conflicts is True
    assert result.proposal and result.proposal.worktree
    body = (result.proposal.worktree / "memory" / "core" / "00-identity.md").read_text(encoding="utf-8")
    assert "<<<<<<< home" in body
    assert "operator identity edit" in body
    assert "identity v2" in body
    assert ">>>>>>> mimir-defaults" in body


def test_existing_upgrade_proposal_blocks_new_one(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"
    _defaults(monkeypatch, identity="identity v2\n", heartbeat="heartbeat v2\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.1.0").action == "proposal_opened"

    _defaults(monkeypatch, identity="identity v3\n", heartbeat="heartbeat v3\n")
    result = du.check_and_open_defaults_upgrade(home, version="1.2.0")

    assert result.ok and result.action == "proposal_exists"
    assert (home / du.LAST_SYNCED_VERSION_FILE).read_text(encoding="utf-8") == "1.1.0\n"


def test_retry_after_proposal_open_failure_still_uses_previous_defaults_base(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If opening the proposal fails after vendor sync, retry must not drop the diff.

    The vendor branch has already advanced to v2 after the first failed attempt;
    a pending previous-ref preserves v1 as the merge base for the next startup.
    """
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"
    _defaults(monkeypatch, identity="identity v2\n", heartbeat="heartbeat v2\n")

    failed = du.check_and_open_defaults_upgrade(home, version="1.1.0", base="missing-base")
    assert not failed.ok and failed.action == "error"
    assert (home / du.LAST_SYNCED_VERSION_FILE).read_text(encoding="utf-8") == "1.0.0\n"
    assert _git("rev-parse", "--verify", du.PENDING_PREVIOUS_REF, cwd=home).returncode == 0

    retried = du.check_and_open_defaults_upgrade(home, version="1.1.0")

    assert retried.ok and retried.action == "proposal_opened"
    assert retried.proposal and retried.proposal.worktree
    assert (retried.proposal.worktree / "memory" / "core" / "00-identity.md").read_text(encoding="utf-8") == "identity v2\n"
    assert (home / du.LAST_SYNCED_VERSION_FILE).read_text(encoding="utf-8") == "1.1.0\n"
    assert _git("rev-parse", "--verify", du.PENDING_PREVIOUS_REF, cwd=home, check=False).returncode != 0


def test_merge_error_cleans_open_upgrade_proposal(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"
    _defaults(monkeypatch, identity="identity v2\n", heartbeat="heartbeat v2\n")
    monkeypatch.setattr(du, "_apply_defaults_three_way", lambda *a, **k: (False, False, "boom"))

    result = du.check_and_open_defaults_upgrade(home, version="1.1.0")

    assert not result.ok and result.detail == "boom"
    assert list_open_proposals(home, lane="upgrade") == []
    assert (home / du.LAST_SYNCED_VERSION_FILE).read_text(encoding="utf-8") == "1.0.0\n"
