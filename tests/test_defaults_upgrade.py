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



def test_vendor_worktree_self_heals_missing_scratch_ignore(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defaults-vendor worktrees must not rely on startup call ordering.

    Older homes may have the broad allowlist-style .gitignore without the
    explicit scratch/ line. Vendor branch preparation should repair that before
    it creates scratch/defaults-vendor.
    """
    gitignore = home / ".gitignore"
    gitignore.write_text(
        "*\n!*/\n!memory/**\n!prompts/**\n!state/**\n!.gitignore\n",
        encoding="utf-8",
    )
    _git("add", ".gitignore", cwd=home)
    _git("commit", "-q", "-m", "simulate old gitignore", cwd=home)
    _git("push", "-q", cwd=home)
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")

    result = du.check_and_open_defaults_upgrade(home, version="1.0.0")

    assert result.ok and result.action == "baseline_initialized"
    assert "scratch/" in gitignore.read_text(encoding="utf-8")
    status = _git("status", "--porcelain", cwd=home).stdout.splitlines()
    assert status == [" M .gitignore"]


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


def test_clean_upgrade_can_auto_submit_without_reconciliation_turn(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"
    _defaults(monkeypatch, identity="identity v2\n", heartbeat="heartbeat v2\n")
    captured: dict = {}

    def fake_finalize(home_arg: Path, **kwargs):
        captured.update(home=home_arg, **kwargs)
        return du.ProposalResult(
            ok=True,
            branch="upgrade/defaults-1-1-0-123",
            pushed=True,
            pr_url="https://github.example/pr/1",
            reason=None,
        )

    monkeypatch.setattr(du, "finalize_proposal", fake_finalize)

    result = du.check_and_open_defaults_upgrade(
        home, version="1.1.0", auto_submit_clean=True,
    )

    assert result.ok and result.action == "auto_submitted"
    assert result.auto_submit and result.auto_submit.pr_url == "https://github.example/pr/1"
    assert captured["lane"] == "upgrade"
    assert captured["title"] == "Upgrade mimir defaults to 1.1.0"
    assert (home / du.LAST_SYNCED_VERSION_FILE).read_text(encoding="utf-8") == "1.1.0\n"
    assert _git("rev-parse", "--verify", du.PENDING_PREVIOUS_REF, cwd=home, check=False).returncode != 0


@pytest.mark.asyncio
async def test_upgrade_reconciliation_turn_renders_template_and_enqueues(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "prompts").mkdir()
    (home / "prompts" / du.UPGRADE_PROMPT_TEMPLATE).write_text(
        "Upgrade {version} branch={branch} wt={worktree} conflicts={conflicts}",
        encoding="utf-8",
    )
    wt = home / "scratch" / "proposals" / "upgrade" / "upgrade_defaults"
    result = du.DefaultsUpgradeResult(
        ok=True,
        action="proposal_opened_conflicts",
        version="1.1.0",
        proposal=du.OpenResult(ok=True, branch="upgrade/defaults", worktree=wt),
        conflicts=True,
    )
    events = []

    async def fake_enqueue(event):
        events.append(event)
        return True

    assert await du.enqueue_upgrade_reconciliation_turn(home, result, fake_enqueue) is True
    assert len(events) == 1
    event = events[0]
    assert event.trigger == "upgrade"
    assert event.channel_id == "upgrade:1.1.0"
    assert event.source == "system"
    assert "Upgrade 1.1.0 branch=upgrade/defaults" in event.content
    assert f"wt={wt}" in event.content
    assert "conflicts=true" in event.content
    assert event.extra["proposal_worktree"] == str(wt)


@pytest.mark.asyncio
async def test_upgrade_reconciliation_turn_skips_auto_submitted(tmp_path: Path) -> None:
    result = du.DefaultsUpgradeResult(
        ok=True,
        action="auto_submitted",
        version="1.1.0",
        proposal=du.OpenResult(ok=True, branch="upgrade/defaults", worktree=tmp_path / "wt"),
    )

    async def fail_enqueue(event):  # pragma: no cover - should not be called
        raise AssertionError(event)

    assert await du.enqueue_upgrade_reconciliation_turn(tmp_path, result, fail_enqueue) is False


def _multiline(top: str, bottom: str) -> str:
    """A file with two edit anchors separated by stable context, so a 3-way
    merge that conflicts at both anchors produces two *separate* regions."""
    ctx = "".join(f"ctx{i}\n" for i in range(1, 11))
    return f"{top}\n{ctx}{bottom}\n"


def test_merge_file_keeps_multiple_conflict_regions(tmp_path: Path) -> None:
    # git merge-file returns the conflict-region count as its exit code (2 here);
    # the merge result must be kept, not treated as an error.
    base = _multiline("top", "bottom")
    ours = _multiline("ours-top", "ours-bottom")
    theirs = _multiline("theirs-top", "theirs-bottom")

    merged, had_conflict, err = du._merge_file(
        ours, base, theirs, label="mimir-defaults", cwd=tmp_path
    )

    assert err is None
    assert had_conflict is True
    assert merged.count("<<<<<<< home") == 2
    assert "ours-top" in merged and "theirs-top" in merged
    assert "ours-bottom" in merged and "theirs-bottom" in merged


def test_multi_region_operator_conflicts_open_proposal(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: an operator who customized a file in several separated spots
    # that the new defaults also changed used to abort the whole upgrade with a
    # fake "error" (exit code >= 2 mishandled). It must open a conflict proposal.
    v1 = _multiline("identity TOP v1", "identity BOT v1")
    (home / "memory" / "core" / "00-identity.md").write_text(v1, encoding="utf-8")
    _git("add", "memory/core/00-identity.md", cwd=home)
    _git("commit", "-q", "-m", "multiline identity v1", cwd=home)
    _git("push", "-q", cwd=home)
    _defaults(monkeypatch, identity=v1, heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"

    operator = _multiline("identity TOP operator", "identity BOT operator")
    (home / "memory" / "core" / "00-identity.md").write_text(operator, encoding="utf-8")
    _git("add", "memory/core/00-identity.md", cwd=home)
    _git("commit", "-q", "-m", "operator multi-region edit", cwd=home)
    _git("push", "-q", cwd=home)
    _defaults(monkeypatch, identity=_multiline("identity TOP v2", "identity BOT v2"), heartbeat="heartbeat v1\n")

    result = du.check_and_open_defaults_upgrade(home, version="1.1.0")

    assert result.ok and result.action == "proposal_opened_conflicts"
    assert result.conflicts is True
    assert result.proposal and result.proposal.worktree
    body = (result.proposal.worktree / "memory" / "core" / "00-identity.md").read_text(encoding="utf-8")
    assert body.count("<<<<<<< home") == 2
    assert "identity TOP operator" in body and "identity TOP v2" in body
    assert "identity BOT operator" in body and "identity BOT v2" in body


def test_write_conflict_markers_stay_on_their_own_lines(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    du._write_conflict(
        target, "home text no newline", "theirs text no newline", label="mimir-defaults"
    )
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "<<<<<<< home"
    assert "=======" in lines
    assert lines[-1] == ">>>>>>> mimir-defaults"


def test_upgrade_template_instructs_operator_notification() -> None:
    """The shipped upgrade-reconciliation prompt must tell the agent to notify
    the operator (send_message on the operator alert channel) after submitting,
    so the propose-only PR doesn't sit unreviewed (0.3.1)."""
    from pathlib import Path as _P

    import mimir

    tmpl = _P(mimir.__file__).parent / "prompt_templates" / "upgrade.md"
    text = tmpl.read_text(encoding="utf-8")
    assert "send_message" in text
    assert "operator alert" in text.lower()
    # explicit channel_id, since the upgrade turn is non-interactive
    assert "channel_id" in text


@pytest.mark.asyncio
async def test_upgrade_fallback_prompt_instructs_operator_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither a home nor a bundled upgrade template is available, the
    inline fallback prompt must also carry the operator-notify instruction."""
    home = tmp_path / "home"
    home.mkdir()
    # No home template; force the bundled lookup empty → inline fallback.
    monkeypatch.setattr(du, "bundled_prompt_defaults", lambda: {})
    wt = home / "scratch" / "proposals" / "upgrade" / "upgrade_defaults"
    result = du.DefaultsUpgradeResult(
        ok=True,
        action="proposal_opened",
        version="1.2.0",
        proposal=du.OpenResult(ok=True, branch="upgrade/defaults", worktree=wt),
    )
    events = []

    async def fake_enqueue(event):
        events.append(event)
        return True

    assert await du.enqueue_upgrade_reconciliation_turn(home, result, fake_enqueue) is True
    assert "send_message" in events[0].content
    assert "operator alert" in events[0].content.lower()
