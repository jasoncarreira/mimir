"""Tests for version-triggered defaults-upgrade proposals (chainlink #349)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import mimir.defaults_upgrade as du
from mimir.proposals import list_open_proposals
from mimir.plugins.defaults import plugin_default_files, seed_plugin_defaults


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
    # Neutralize the scheduler template by default so prompts/core-focused tests
    # see an unchanged vendor tree. Scheduler-reconcile tests (#666) opt in by
    # re-patching this after calling _defaults.
    monkeypatch.setattr("mimir.scheduler.bundled_scheduler_template_text", lambda: "")


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


def test_merge_error_after_partial_changes_still_cleans_upgrade_proposal(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A three-way merge that fails partway leaves uncommitted changes.

    abandon_proposal refuses uncommitted changes unless discard_changes=True
    (#2384). The upgrade lane's cleanup discards the worktree it just opened,
    so the error is reported instead of raised.
    """
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"
    _defaults(monkeypatch, identity="identity v2\n", heartbeat="heartbeat v2\n")

    def partial_merge(worktree: Path, **_kwargs):
        # Leave a tracked file half-merged, as a three-way failure part way would.
        tracked = _git("ls-files", cwd=worktree).stdout.split()[0]
        (worktree / tracked).write_text("half applied\n", encoding="utf-8")
        return False, False, "boom"

    monkeypatch.setattr(du, "_apply_defaults_three_way", partial_merge)

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


def test_version_bump_opens_upgrade_pull_request_end_to_end(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"
    _defaults(monkeypatch, identity="identity v2\n", heartbeat="heartbeat v2\n")
    opened_prs: list[dict[str, str]] = []

    def open_pr(home_arg, branch, base, title, body):
        opened_prs.append({"branch": branch, "base": base, "title": title, "body": body})
        return "https://github.example/home/pull/17"

    monkeypatch.setattr("mimir.proposals._default_open_pr", open_pr)

    result = du.check_and_open_defaults_upgrade(
        home, version="1.1.0", auto_submit_clean=True,
    )

    assert result.ok and result.action == "auto_submitted"
    assert result.auto_submit and result.auto_submit.pr_url == "https://github.example/home/pull/17"
    assert len(opened_prs) == 1
    assert opened_prs[0]["branch"].startswith("upgrade/defaults-1-1-0-")
    assert opened_prs[0]["base"] == "main"
    assert opened_prs[0]["title"] == "Upgrade mimir defaults to 1.1.0"
    assert "Proposal lane: `upgrade`" in opened_prs[0]["body"]
    assert f"refs/heads/{opened_prs[0]['branch']}" in _git(
        "ls-remote", "--heads", "origin", opened_prs[0]["branch"], cwd=home
    ).stdout
    assert list_open_proposals(home, lane="upgrade") == []


def test_auto_submit_pr_open_failure_is_not_reported_as_upgrade_success(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"
    _defaults(monkeypatch, identity="identity v2\n", heartbeat="heartbeat v2\n")

    monkeypatch.setattr(
        du,
        "finalize_proposal",
        lambda *a, **k: du.ProposalResult(
            ok=False,
            branch="upgrade/defaults-1-1-0-123",
            pushed=True,
            pr_url=None,
            reason="pr_open",
            detail="gh pr create failed: authentication required",
        ),
    )

    result = du.check_and_open_defaults_upgrade(
        home, version="1.1.0", auto_submit_clean=True,
    )

    assert not result.ok and result.action == "error"
    assert result.detail == "gh pr create failed: authentication required"
    assert (home / du.LAST_SYNCED_VERSION_FILE).read_text(encoding="utf-8") == "1.0.0\n"
    assert _git("rev-parse", "--verify", du.PENDING_PREVIOUS_REF, cwd=home).returncode == 0


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
    assert event.service_principal == "system"
    assert event.channel_id == "upgrade:1.1.0"
    assert event.source == "system"
    assert "Upgrade 1.1.0 branch=upgrade/defaults" in event.content
    assert f"wt={wt}" in event.content
    assert "conflicts=true" in event.content
    assert event.extra["proposal_worktree"] == str(wt)
    assert "pr_metadata" not in event.content
    assert "pr_list" not in event.content
    assert "Do not run shell `ls`" in event.content
    assert "Do not call `list_channels`" in event.content


@pytest.mark.asyncio
async def test_upgrade_reconciliation_turn_names_only_changed_readable_docs(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    wt = home / "scratch" / "proposals" / "upgrade" / "upgrade_defaults"
    result = du.DefaultsUpgradeResult(
        ok=True,
        action="proposal_opened",
        version="1.1.0",
        proposal=du.OpenResult(ok=True, branch="upgrade/defaults", worktree=wt),
    )
    events = []

    async def fake_enqueue(event):
        events.append(event)
        return True

    assert await du.enqueue_upgrade_reconciliation_turn(
        home,
        result,
        fake_enqueue,
        doc_changes={
            "docs/code-building-pipeline.md": "updated",
            "docs/new-reference.md": "created",
            "docs/unchanged.md": "unchanged",
            "docs/deleted.md": "skipped_deleted",
            "docs/.env.example": "updated",
        },
    )
    content = events[0].content
    assert "## Updated reference docs" in content
    assert "`/docs/code-building-pipeline.md`" in content
    assert "`/docs/new-reference.md`" in content
    assert "not a prerequisite" in content
    assert "unchanged.md" not in content
    assert "deleted.md" not in content
    assert ".env.example" not in content


@pytest.mark.asyncio
async def test_upgrade_reconciliation_turn_has_no_empty_docs_section(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = du.DefaultsUpgradeResult(
        ok=True,
        action="proposal_opened",
        version="1.1.0",
        proposal=du.OpenResult(
            ok=True, branch="upgrade/defaults", worktree=home / "proposal",
        ),
    )
    events = []

    async def fake_enqueue(event):
        events.append(event)
        return True

    assert await du.enqueue_upgrade_reconciliation_turn(
        home,
        result,
        fake_enqueue,
        doc_changes={"docs/README.md": "unchanged"},
    )
    assert "Updated reference docs" not in events[0].content
    assert "{changed_docs_section}" not in events[0].content


@pytest.mark.asyncio
async def test_upgrade_reconciliation_turn_uses_configured_operator_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MIMIR_OPERATOR_ALERT_CHANNEL", "slack-C-operators")
    result = du.DefaultsUpgradeResult(
        ok=True,
        action="proposal_opened",
        version="1.1.0",
        proposal=du.OpenResult(
            ok=True, branch="upgrade/defaults", worktree=home / "proposal",
        ),
    )
    events = []

    async def fake_enqueue(event):
        events.append(event)
        return True

    assert await du.enqueue_upgrade_reconciliation_turn(home, result, fake_enqueue)
    assert "configured operator alert channel `slack-C-operators`" in events[0].content


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


def test_upgrade_template_uses_proposal_diff() -> None:
    """Upgrade turns review the server-side staged diff without a shell command."""
    from pathlib import Path as _P

    import mimir

    tmpl = _P(mimir.__file__).parent / "prompt_templates" / "upgrade.md"
    text = tmpl.read_text(encoding="utf-8")
    assert "proposal_diff(lane='upgrade')" in text
    assert "git -C {worktree} diff --cached" not in text


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


# ── version-specific upgrade prompts (chainlink #645) ────────────────────


def _fake_upgrade_prompts(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    monkeypatch.setattr(du, "bundled_upgrade_prompts", lambda: dict(mapping))


def test_pending_upgrade_prompts_exact_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_upgrade_prompts(monkeypatch, {"0.6.5": "x", "0.7.0": "y"})
    assert du.pending_upgrade_prompt_versions("0.6.4", "0.6.5") == ["0.6.5"]


def test_pending_upgrade_prompts_cumulative_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_upgrade_prompts(monkeypatch, {"0.6.5": "a", "0.6.6": "b", "0.6.7": "c", "0.7.0": "d"})
    # 0.6.4 -> 0.6.7 runs every intermediate prompt in (prev, cur], oldest first
    assert du.pending_upgrade_prompt_versions("0.6.4", "0.6.7") == ["0.6.5", "0.6.6", "0.6.7"]


def test_pending_upgrade_prompts_absent_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_upgrade_prompts(monkeypatch, {"0.7.0": "later"})
    assert du.pending_upgrade_prompt_versions("0.6.4", "0.6.5") == []


def test_pending_upgrade_prompts_fresh_install_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_upgrade_prompts(monkeypatch, {"0.6.5": "x"})
    assert du.pending_upgrade_prompt_versions(None, "0.6.5") == []


def test_pending_upgrade_prompts_no_forward_movement(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_upgrade_prompts(monkeypatch, {"0.6.5": "x"})
    assert du.pending_upgrade_prompt_versions("0.6.5", "0.6.5") == []
    assert du.pending_upgrade_prompt_versions("0.7.0", "0.6.5") == []


def test_bundled_upgrade_prompts_ships_065() -> None:
    # The first real upgrade prompt is discoverable, keyed by target version.
    prompts = du.bundled_upgrade_prompts()
    assert "0.6.5" in prompts
    assert "memory-hygiene" in prompts["0.6.5"]
    assert "README" not in prompts  # the authoring doc is not a prompt


async def test_enqueue_upgrade_prompts_dispatches_once_per_bump(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _fake_upgrade_prompts(monkeypatch, {"0.6.5": "---\nname: u\n---\nbody {from}->{to} ({version})"})
    events = []

    async def fake_enqueue(event):
        events.append(event)
        return True

    n = await du.enqueue_upgrade_prompt_turns(
        tmp_path, previous="0.6.4", current="0.6.5",
        action="proposal_opened", enqueue=fake_enqueue,
    )
    assert n == 1
    ev = events[0]
    assert ev.channel_id == "upgrade-prompt:0.6.5"
    assert ev.trigger == du.UPGRADE_TRIGGER
    assert ev.service_principal == "system"
    assert "body 0.6.4->0.6.5 (0.6.5)" in ev.content
    assert ev.extra["from_version"] == "0.6.4"
    assert ev.extra["to_version"] == "0.6.5"


async def test_enqueue_upgrade_prompts_skips_non_bump_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _fake_upgrade_prompts(monkeypatch, {"0.6.5": "x"})
    events = []

    async def fake_enqueue(event):
        events.append(event)
        return True

    # already_synced doesn't advance last-synced-version → no dispatch (once-only)
    n = await du.enqueue_upgrade_prompt_turns(
        tmp_path, previous="0.6.4", current="0.6.5",
        action="already_synced", enqueue=fake_enqueue,
    )
    assert n == 0
    assert events == []


async def test_enqueue_upgrade_prompts_fresh_install_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _fake_upgrade_prompts(monkeypatch, {"0.6.5": "x"})

    async def fake_enqueue(event):  # pragma: no cover - must not be called
        raise AssertionError("fresh install should not dispatch upgrade prompts")

    n = await du.enqueue_upgrade_prompt_turns(
        tmp_path, previous=None, current="0.6.5",
        action="baseline_initialized", enqueue=fake_enqueue,
    )
    assert n == 0


def test_bundled_upgrade_prompts_ships_066() -> None:
    prompts = du.bundled_upgrade_prompts()
    assert "0.6.6" in prompts
    body = prompts["0.6.6"]
    assert "add_schedule" in body and "memory-hygiene" in body


# ── scheduler.yaml defaults reconciliation (chainlink #666) ───────────────

from mimir.scheduler import load_jobs_from_text  # noqa: E402

_HEARTBEAT = "- name: heartbeat\n  cron: '0 * * * *'\n  prompt_file: heartbeat.md\n"
_MEMHYG = (
    "- name: memory-hygiene\n  cron: '0 8 * * 2'\n  priority: normal\n"
    "  prompt_file: memory-hygiene.md\n"
)


def test_merge_adds_new_default_tick() -> None:
    base = _HEARTBEAT
    theirs = _HEARTBEAT + _MEMHYG
    home = _HEARTBEAT
    new_text, added = du._merge_scheduler_defaults(
        base_text=base, their_text=theirs, home_text=home, version="0.6.7"
    )
    assert added == ["memory-hygiene"]
    assert new_text is not None
    jobs, rejections = load_jobs_from_text(new_text)
    assert rejections == []
    assert [j.name for j in jobs] == ["heartbeat", "memory-hygiene"]
    mh = jobs[1]
    assert mh.prompt_file == "memory-hygiene.md"
    assert mh.cron == "0 8 * * 2"
    assert mh.priority == "normal"


def test_merge_preserves_operator_customized_job() -> None:
    base = "- name: reflect\n  cron: '0 6 * * 0'\n  prompt_file: reflect.md\n"
    theirs = base + _MEMHYG
    # operator changed reflect's cron + priority
    home = "- name: reflect\n  cron: '30 5 * * 0'\n  priority: high\n  prompt_file: reflect.md\n"
    new_text, added = du._merge_scheduler_defaults(
        base_text=base, their_text=theirs, home_text=home, version="0.6.7"
    )
    assert added == ["memory-hygiene"]
    jobs, rejections = load_jobs_from_text(new_text)
    assert rejections == []
    by = {j.name: j for j in jobs}
    assert by["reflect"].cron == "30 5 * * 0"  # untouched
    assert by["reflect"].priority == "high"
    assert "memory-hygiene" in by


def test_merge_preserves_operator_custom_job() -> None:
    base = _HEARTBEAT
    theirs = _HEARTBEAT + _MEMHYG
    home = _HEARTBEAT + "- name: daily-journal\n  cron: '0 0 * * *'\n  prompt_file: daily-journal.md\n"
    new_text, added = du._merge_scheduler_defaults(
        base_text=base, their_text=theirs, home_text=home, version="0.6.7"
    )
    assert added == ["memory-hygiene"]
    jobs, rejections = load_jobs_from_text(new_text)
    assert rejections == []
    names = [j.name for j in jobs]
    assert "daily-journal" in names  # custom job preserved
    assert "memory-hygiene" in names


def test_merge_does_not_readd_dropped_default() -> None:
    # issues-audit is a long-standing default (in the baseline template); the
    # operator deliberately dropped it. It must NOT be re-added or deleted.
    base = _HEARTBEAT + "- name: issues-audit\n  cron: '0 7 1 * *'\n  prompt_file: issues-audit.md\n"
    theirs = base  # template still ships issues-audit
    home = _HEARTBEAT  # operator removed issues-audit
    new_text, added = du._merge_scheduler_defaults(
        base_text=base, their_text=theirs, home_text=home, version="0.6.7"
    )
    assert added == []
    assert new_text is None


def test_merge_bootstrap_run_is_noop() -> None:
    # No prior template baseline (the run that first ships scheduler.yaml into
    # the vendor branch) → no-op, so a previously-dropped default isn't re-added.
    new_text, added = du._merge_scheduler_defaults(
        base_text=None, their_text=_HEARTBEAT + _MEMHYG, home_text=_HEARTBEAT, version="0.6.6"
    )
    assert (new_text, added) == (None, [])


def test_merge_noop_when_nothing_new() -> None:
    assert du._merge_scheduler_defaults(
        base_text=_HEARTBEAT, their_text=_HEARTBEAT, home_text=_HEARTBEAT, version="0.6.7"
    ) == (None, [])


def test_merge_preserves_operator_comments_via_text_append() -> None:
    base = _HEARTBEAT
    theirs = _HEARTBEAT + _MEMHYG
    home = "# operator notes\n- name: heartbeat\n  cron: '0 * * * *'  # hourly\n  prompt_file: heartbeat.md\n"
    new_text, added = du._merge_scheduler_defaults(
        base_text=base, their_text=theirs, home_text=home, version="0.6.7"
    )
    assert added == ["memory-hygiene"]
    assert "# operator notes" in new_text  # append, not re-serialize
    assert "# hourly" in new_text


@pytest.mark.parametrize("jobs_last", [False, True])
def test_merge_mapping_inserts_in_jobs_without_rewriting_operator_config(jobs_last) -> None:
    jobs_block = (
        "jobs:\n"
        "  # operator's heartbeat\n"
        "  - name: heartbeat\n"
        "    cron: '0 * * * *'  # hourly\n"
        "    prompt_file: heartbeat.md\n"
    )
    commands_block = "operator_shell_commands: []  # operator grants\n"
    home = (commands_block + jobs_block) if jobs_last else (jobs_block + commands_block)
    new_text, added = du._merge_scheduler_defaults(
        base_text=_HEARTBEAT, their_text=_HEARTBEAT + _MEMHYG,
        home_text=home, version="0.6.7",
    )
    assert added == ["memory-hygiene"]
    assert new_text is not None
    assert "  # operator's heartbeat" in new_text
    assert "# hourly" in new_text
    assert commands_block in new_text
    assert (new_text.index("  - name: memory-hygiene")
            < new_text.index("operator_shell_commands:")) == (not jobs_last)
    jobs, rejections = load_jobs_from_text(new_text)
    assert rejections == []
    assert [job.name for job in jobs] == ["heartbeat", "memory-hygiene"]


@pytest.mark.parametrize("home", [
    "jobs: [{name: heartbeat, cron: '0 * * * *', prompt_file: heartbeat.md}]\noperator_shell_commands: []\n",
    "jobs:\n  - name: heartbeat\n    cron: '0 * * * *'\n    prompt_file: heartbeat.md\njobs: []\n",
])
def test_merge_mapping_ambiguous_insertion_is_noop(home, caplog) -> None:
    assert du._merge_scheduler_defaults(
        base_text=_HEARTBEAT, their_text=_HEARTBEAT + _MEMHYG,
        home_text=home, version="0.6.7",
    ) == (None, [])
    assert "ambiguous jobs insertion point" in caplog.text


def test_merge_rejects_result_with_invalid_existing_job(caplog) -> None:
    home = (
        "jobs:\n  - name: heartbeat\n    cron: '0 * * * *'\n    prompt_file: heartbeat.md\n"
        "  - broken\n"
        "operator_shell_commands: []\n"
    )
    assert du._merge_scheduler_defaults(
        base_text=_HEARTBEAT, their_text=_HEARTBEAT + _MEMHYG,
        home_text=home, version="0.6.7",
    ) == (None, [])
    assert "merged scheduler.yaml failed validation" in caplog.text


def test_merge_rejects_result_missing_new_job(monkeypatch, caplog) -> None:
    home = "jobs:\n  - name: heartbeat\n    cron: '0 * * * *'\n    prompt_file: heartbeat.md\n"
    # Even if an insertion yields parseable YAML, it must actually carry the
    # new jobs before the reconciler can write the result.
    monkeypatch.setattr(du, "_insert_scheduler_jobs", lambda text, addition, banner: text)
    assert du._merge_scheduler_defaults(
        base_text=_HEARTBEAT, their_text=_HEARTBEAT + _MEMHYG,
        home_text=home, version="0.6.7",
    ) == (None, [])
    assert "merged scheduler.yaml failed validation" in caplog.text


def test_scheduler_reconcile_proposes_new_default_tick(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # scheduler.yaml is gitignored by the fixture allowlist; force-track it once
    # (real homes allowlist it). Thereafter it's a tracked file, so the
    # reconcile's plain ``git add`` stages modifications normally.
    (home / "scheduler.yaml").write_text(_HEARTBEAT, encoding="utf-8")
    _git("add", "-f", "scheduler.yaml", cwd=home)
    _git("commit", "-q", "-m", "add scheduler.yaml", cwd=home)
    _git("push", "-q", cwd=home)

    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    monkeypatch.setattr("mimir.scheduler.bundled_scheduler_template_text", lambda: _HEARTBEAT)
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"

    # Only the scheduler template changes (adds memory-hygiene); core/prompts same.
    monkeypatch.setattr(
        "mimir.scheduler.bundled_scheduler_template_text", lambda: _HEARTBEAT + _MEMHYG
    )
    result = du.check_and_open_defaults_upgrade(home, version="1.1.0")

    assert result.ok and result.action == "proposal_opened"
    assert result.detail and "memory-hygiene" in result.detail
    wt = result.proposal.worktree
    staged = _git("diff", "--cached", "--name-only", cwd=wt).stdout.splitlines()
    assert "scheduler.yaml" in staged
    jobs, rejections = load_jobs_from_text(
        (wt / "scheduler.yaml").read_text(encoding="utf-8")
    )
    assert rejections == []
    assert [j.name for j in jobs] == ["heartbeat", "memory-hygiene"]
    # Live home stays operator-owned until the proposal PR merges.
    assert "memory-hygiene" not in (home / "scheduler.yaml").read_text(encoding="utf-8")
    _cleanup_upgrade_proposal(home)


def test_scheduler_introduction_run_does_not_add(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The version that first ships scheduler.yaml into the vendor branch must
    # NOT add ticks (no prior baseline) — even though the template already has
    # memory-hygiene and the home lacks it. The upgrade prompt delivers it.
    (home / "scheduler.yaml").write_text(_HEARTBEAT, encoding="utf-8")
    _git("add", "-f", "scheduler.yaml", cwd=home)
    _git("commit", "-q", "-m", "add scheduler.yaml", cwd=home)
    _git("push", "-q", cwd=home)

    # v1 baseline WITHOUT a scheduler template (simulates pre-#666 vendor branch).
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v1\n")
    monkeypatch.setattr("mimir.scheduler.bundled_scheduler_template_text", lambda: "")
    assert du.check_and_open_defaults_upgrade(home, version="1.0.0").action == "baseline_initialized"

    # v2 introduces the template (with memory-hygiene) AND bumps a prompt so a
    # proposal opens for the prompt change — scheduler.yaml must be untouched.
    _defaults(monkeypatch, identity="identity v1\n", heartbeat="heartbeat v2\n")
    monkeypatch.setattr(
        "mimir.scheduler.bundled_scheduler_template_text", lambda: _HEARTBEAT + _MEMHYG
    )
    result = du.check_and_open_defaults_upgrade(home, version="1.1.0")

    assert result.ok and result.action == "proposal_opened"
    wt = result.proposal.worktree
    staged = _git("diff", "--cached", "--name-only", cwd=wt).stdout.splitlines()
    assert "scheduler.yaml" not in staged  # introduction run is a no-op for scheduler
    assert "memory-hygiene" not in (wt / "scheduler.yaml").read_text(encoding="utf-8")
    _cleanup_upgrade_proposal(home)


def _plugin(tmp_path: Path, version: str = "1.0", *, name: str = "sample"):
    root = tmp_path / name
    (root / "skills" / "helper").mkdir(parents=True, exist_ok=True)
    (root / "prompts").mkdir(exist_ok=True)
    (root / "jobs.yaml").write_text(_HEARTBEAT, encoding="utf-8")
    (root / "prompts" / "heartbeat.md").write_text("plugin prompt\n", encoding="utf-8")
    (root / "skills" / "helper" / "SKILL.md").write_text("plugin skill\n", encoding="utf-8")
    (root / "mimir-plugin.toml").write_text("", encoding="utf-8")
    class Distribution:
        files = (Path("mimir-plugin.toml"), Path("jobs.yaml"),
                 Path("prompts/heartbeat.md"), Path("skills/helper/SKILL.md"))

        def locate_file(self, path):
            return root / path

    return SimpleNamespace(
        name=name, entry_point=SimpleNamespace(dist=Distribution()),
        manifest=SimpleNamespace(defaults={
            "version": version, "skills": ("skills/helper",),
            "prompts": ("prompts/heartbeat.md",), "scheduler_jobs": ("jobs.yaml",),
        }, optional_skills=()),
    )


def test_plugin_defaults_seed_is_namespaced_and_preserves_edits(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "scheduler.yaml").write_text(_HEARTBEAT, encoding="utf-8")
    (home / "prompts").mkdir()
    (home / "prompts" / "heartbeat.md").write_text("core prompt\n", encoding="utf-8")
    first = seed_plugin_defaults(home, selected=(plugin,))
    assert first["skills/sample-helper/SKILL.md"] == "created"
    assert first["prompts/sample/heartbeat.md"] == "created"
    assert "sample-heartbeat" in (home / "scheduler.yaml").read_text()
    target = home / "prompts" / "sample" / "heartbeat.md"
    target.write_text("operator edit\n", encoding="utf-8")
    (tmp_path / "sample" / "prompts" / "heartbeat.md").write_text("plugin v2\n")
    assert seed_plugin_defaults(home, selected=(plugin,))["prompts/sample/heartbeat.md"] == "existing"
    assert target.read_text() == "operator edit\n"
    assert (home / "prompts" / "heartbeat.md").read_text() == "core prompt\n"
    assert (home / "scheduler.yaml").read_text().count("name: sample-heartbeat") == 1


def test_plugin_defaults_disabled_does_not_load_package(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("mimir.plugins.defaults.enabled_plugins", lambda: ())
    assert seed_plugin_defaults(tmp_path) == {}
    assert not (tmp_path / "skills").exists()


def test_new_plugin_version_only_fills_missing_files(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    home = tmp_path / "home"
    seed_plugin_defaults(home, selected=(plugin,))
    skill = home / "skills/sample-helper/SKILL.md"
    skill.write_text("operator skill\n")
    plugin.entry_point.dist.files += (Path("skills/helper/README.md"),)
    (tmp_path / "sample/skills/helper/README.md").write_text("new file\n")
    plugin.manifest.defaults["version"] = "2.0"
    result = seed_plugin_defaults(home, selected=(plugin,))
    assert result["skills/sample-helper/README.md"] == "created"
    assert skill.read_text() == "operator skill\n"


def test_plugin_skill_namespace_collision_fails_before_seeding(tmp_path: Path) -> None:
    alpha = _plugin(tmp_path, name="alpha")
    alpha_beta = _plugin(tmp_path, name="alpha-beta")
    skill = tmp_path / "alpha/skills/helper/SKILL.md"
    moved = tmp_path / "alpha/skills/beta-helper"
    moved.mkdir()
    (moved / "SKILL.md").write_text(skill.read_text())
    alpha.manifest.defaults["skills"] = ("skills/beta-helper",)
    alpha.entry_point.dist.files += (Path("skills/beta-helper/SKILL.md"),)
    with pytest.raises(ValueError, match="claimed by alpha and alpha-beta"):
        seed_plugin_defaults(tmp_path / "home", selected=(alpha, alpha_beta))
    assert not (tmp_path / "home").exists()


def test_plugin_defaults_upgrade_proposes_without_touching_live_home(
    home: Path, tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)
    (home / "scheduler.yaml").write_text(_HEARTBEAT, encoding="utf-8")
    seed_plugin_defaults(home, selected=(plugin,))
    assert (home / "skills/sample-helper/SKILL.md").is_file()
    assert (home / "prompts/sample/heartbeat.md").is_file()
    added = _git("add", "-f", "skills/sample-helper/SKILL.md", "prompts/sample/heartbeat.md", "scheduler.yaml", cwd=home, check=False)
    assert added.returncode == 0, added.stderr
    _git("commit", "-q", "-m", "plugin seed", cwd=home)
    _git("push", "-q", cwd=home)
    assert du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"].action == "baseline_initialized"
    assert du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"].action == "already_synced"
    path = home / "prompts" / "sample" / "heartbeat.md"
    path.write_text("operator edit\n")
    _git("add", "-f", "prompts/sample/heartbeat.md", cwd=home)
    _git("commit", "-q", "-m", "operator change", cwd=home)
    _git("push", "-q", cwd=home)
    (tmp_path / "sample" / "prompts" / "heartbeat.md").write_text("plugin v2\n")
    plugin.manifest.defaults["version"] = "2.0"
    result = du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"]
    assert result.action == "proposal_opened_conflicts"
    assert result.proposal and result.proposal.worktree
    assert "operator edit" in (result.proposal.worktree / "prompts/sample/heartbeat.md").read_text()
    assert "plugin v2" in (result.proposal.worktree / "prompts/sample/heartbeat.md").read_text()
    assert path.read_text() == "operator edit\n"
    assert (home / "memory/core/00-identity.md").read_text() == "identity v1\n"
    _cleanup_upgrade_proposal(home)


def test_plugin_skill_only_version_bump_never_opens_conflicting_proposal(home, tmp_path):
    plugin = _plugin(tmp_path)
    seed_plugin_defaults(home, selected=(plugin,))
    _git("add", "-f", "skills/sample-helper/SKILL.md", "prompts/sample/heartbeat.md", cwd=home)
    _git("commit", "-q", "-m", "plugin seed", cwd=home)
    _git("push", "-q", cwd=home)
    assert du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"].action == "baseline_initialized"
    (tmp_path / "sample/skills/helper/SKILL.md").write_text("plugin skill v2\n")
    plugin.manifest.defaults["version"] = "2.0"
    result = du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"]
    assert result.action == "no_changes" and result.proposal is None
    assert (home / "skills/sample-helper/SKILL.md").read_text() == "plugin skill\n"


def test_legacy_plugin_vendor_skill_is_not_merged_into_sparse_proposal(home, tmp_path):
    plugin = _plugin(tmp_path)
    seed_plugin_defaults(home, selected=(plugin,))
    _git("add", "-f", "skills/sample-helper/SKILL.md", "prompts/sample/heartbeat.md", cwd=home)
    _git("commit", "-q", "-m", "plugin seed", cwd=home)
    _git("push", "-q", cwd=home)
    baseline = du._ensure_vendor_branch(
        home, branch="mimir-plugin-defaults-sample", version="1.0",
        files=plugin_default_files(plugin),
    )
    assert baseline.error is None and baseline.changed
    (home / ".mimir/upgrade-plugin-defaults").mkdir(parents=True, exist_ok=True)
    (home / ".mimir/upgrade-plugin-defaults/sample").write_text("1.0\n")
    plugin.manifest.defaults["version"] = "2.0"
    result = du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"]
    assert result.action == "vendor_updated_no_home_diff" and result.proposal is None
    assert (home / "skills/sample-helper/SKILL.md").read_text() == "plugin skill\n"


@pytest.mark.parametrize("edited", [False, True])
def test_default_to_optional_upgrade_preserves_installed_skill(home, tmp_path, edited):
    plugin = _plugin(tmp_path)
    (home / "scheduler.yaml").write_text(_HEARTBEAT)
    seed_plugin_defaults(home, selected=(plugin,))
    rel = "skills/sample-helper/SKILL.md"
    _git("add", "-f", rel, "prompts/sample/heartbeat.md", "scheduler.yaml", cwd=home)
    _git("commit", "-q", "-m", "seed old plugin", cwd=home)
    _git("push", "-q", cwd=home)
    assert du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"].action == "baseline_initialized"
    expected = "operator edit\n" if edited else "plugin skill\n"
    if edited:
        (home / rel).write_text(expected)
        _git("add", "-f", rel, cwd=home)
        _git("commit", "-q", "-m", "edit old seed", cwd=home)
        _git("push", "-q", cwd=home)
    plugin.manifest.defaults.update(version="2.0", skills=())
    plugin.manifest.optional_skills = (("skills/helper", "github"),)
    # Force a real proposal to check its resulting tree, not just vendor output.
    (tmp_path / "sample/prompts/heartbeat.md").write_text("new prompt\n")
    result = du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"]
    assert result.action == "proposal_opened"
    assert result.proposal and result.proposal.worktree
    assert (home / rel).read_text() == expected
    # The vendor branch carries prompts and jobs, never skill bytes.
    assert _git("show", f"mimir-plugin-defaults-sample:{rel}", cwd=home, check=False).returncode != 0
    assert rel not in _git("ls-tree", "-r", "--name-only", "mimir-plugin-defaults-sample", cwd=home).stdout.splitlines()
    assert _git("show", f":{rel}", cwd=result.proposal.worktree).stdout == expected
    assert rel not in _git("diff", "--cached", "--name-only", cwd=result.proposal.worktree).stdout.splitlines()
    _cleanup_upgrade_proposal(home)


def test_upgrade_does_not_install_absent_optional(home, tmp_path):
    plugin = _plugin(tmp_path)
    plugin.manifest.defaults["skills"] = ()
    seed_plugin_defaults(home, selected=(plugin,))
    _git("add", "-f", "prompts/sample/heartbeat.md", cwd=home)
    _git("commit", "-q", "-m", "seed without optional", cwd=home)
    _git("push", "-q", cwd=home)
    assert du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"].action == "baseline_initialized"
    plugin.manifest.defaults["version"] = "2.0"
    plugin.manifest.optional_skills = (("skills/helper", "github"),)
    (tmp_path / "sample/prompts/heartbeat.md").write_text("new prompt\n")
    result = du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"]
    assert result.action == "proposal_opened"
    assert result.proposal and result.proposal.worktree
    assert _git("show", ":skills/sample-helper/SKILL.md", cwd=result.proposal.worktree, check=False).returncode != 0
    assert not (home / "skills/sample-helper").exists()
    assert "skills/sample-helper/SKILL.md" not in _git(
        "ls-tree", "-r", "--name-only", "mimir-plugin-defaults-sample", cwd=home,
    ).stdout.splitlines()
    _cleanup_upgrade_proposal(home)


def test_plugin_defaults_upgrade_keeps_owners_separate_on_shared_upgrade_lane(
    home: Path, tmp_path: Path,
) -> None:
    alpha = _plugin(tmp_path, name="alpha")
    beta = _plugin(tmp_path, name="beta")
    (home / "scheduler.yaml").write_text(_HEARTBEAT, encoding="utf-8")
    seed_plugin_defaults(home, selected=(alpha, beta))
    _git("add", "-f", "skills/alpha-helper/SKILL.md", "skills/beta-helper/SKILL.md",
         "prompts/alpha/heartbeat.md", "prompts/beta/heartbeat.md", "scheduler.yaml", cwd=home)
    _git("commit", "-q", "-m", "seed two plugins", cwd=home)
    _git("push", "-q", cwd=home)

    first = du.check_and_open_plugin_defaults_upgrade(home, selected=(alpha, beta))
    assert {name: result.action for name, result in first.items()} == {
        "alpha": "baseline_initialized", "beta": "baseline_initialized",
    }
    for owner, other in (("alpha", "beta"), ("beta", "alpha")):
        files = _git("ls-tree", "-r", "--name-only", f"mimir-plugin-defaults-{owner}", cwd=home).stdout
        assert f"prompts/{owner}/heartbeat.md" in files
        assert f"prompts/{other}/heartbeat.md" not in files
        assert (home / ".mimir/upgrade-plugin-defaults" / owner).read_text() == "1.0\n"

    for owner in ("alpha", "beta"):
        (home / "prompts" / owner / "heartbeat.md").write_text(f"{owner} operator edit\n")
        (tmp_path / owner / "prompts/heartbeat.md").write_text(f"{owner} v2\n")
    _git("add", "prompts/alpha/heartbeat.md", "prompts/beta/heartbeat.md", cwd=home)
    _git("commit", "-q", "-m", "edit both plugin prompts", cwd=home)
    _git("push", "-q", cwd=home)
    alpha.manifest.defaults["version"] = "2.0"
    beta.manifest.defaults["version"] = "2.0"

    opened = du.check_and_open_plugin_defaults_upgrade(home, selected=(alpha, beta))
    assert opened["alpha"].action == "proposal_opened_conflicts"
    assert opened["beta"].action == "proposal_exists"
    assert opened["alpha"].proposal and opened["alpha"].proposal.branch
    assert "plugin-defaults-alpha-2-0" in opened["alpha"].proposal.branch
    assert (home / ".mimir/upgrade-plugin-defaults/alpha").read_text() == "2.0\n"
    assert (home / ".mimir/upgrade-plugin-defaults/beta").read_text() == "1.0\n"
    assert _git("show", "mimir-plugin-defaults-beta:prompts/beta/heartbeat.md", cwd=home).stdout == "plugin prompt\n"
    alpha_proposal = opened["alpha"].proposal.worktree
    assert alpha_proposal is not None
    assert "alpha v2" in (alpha_proposal / "prompts/alpha/heartbeat.md").read_text()
    assert "beta v2" not in (alpha_proposal / "prompts/beta/heartbeat.md").read_text()
    _cleanup_upgrade_proposal(home)

    retried = du.check_and_open_plugin_defaults_upgrade(home, selected=(alpha, beta))
    assert retried["alpha"].action == "already_synced"
    assert retried["beta"].action == "proposal_opened_conflicts"
    assert retried["beta"].proposal and retried["beta"].proposal.branch
    assert "plugin-defaults-beta-2-0" in retried["beta"].proposal.branch
    beta_proposal = retried["beta"].proposal.worktree
    assert beta_proposal is not None
    assert "beta v2" in (beta_proposal / "prompts/beta/heartbeat.md").read_text()
    assert "alpha v2" not in (beta_proposal / "prompts/alpha/heartbeat.md").read_text()
    for owner in ("alpha", "beta"):
        assert (home / "prompts" / owner / "heartbeat.md").read_text() == f"{owner} operator edit\n"
        assert (home / ".mimir/upgrade-plugin-defaults" / owner).read_text() == "2.0\n"
    _cleanup_upgrade_proposal(home)


def test_new_plugin_scheduler_job_is_proposed_not_seeded_into_live_home(
    home: Path, tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)
    (home / "scheduler.yaml").write_text(_HEARTBEAT, encoding="utf-8")
    seed_plugin_defaults(home, selected=(plugin,))
    _git("add", "-f", "scheduler.yaml", "prompts/sample/heartbeat.md",
         "skills/sample-helper/SKILL.md", cwd=home)
    _git("commit", "-q", "-m", "seed plugin jobs", cwd=home)
    _git("push", "-q", cwd=home)
    assert du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"].action == "baseline_initialized"

    scheduler = home / "scheduler.yaml"
    edited = scheduler.read_text().replace("cron: 0 * * * *\n  channel_id: null",
                                            "cron: 30 * * * *\n  channel_id: null")
    assert edited != scheduler.read_text()
    scheduler.write_text("# operator schedule\n" + edited)
    _git("add", "scheduler.yaml", cwd=home)
    _git("commit", "-q", "-m", "customize plugin schedule", cwd=home)
    _git("push", "-q", cwd=home)
    live = scheduler.read_bytes()
    plugin.manifest.defaults["version"] = "2.0"
    (tmp_path / "sample/jobs.yaml").write_text(
        _HEARTBEAT + "- name: new-tick\n  cron: '0 9 * * *'\n  prompt_file: heartbeat.md\n"
    )

    seeded = seed_plugin_defaults(home, selected=(plugin,))
    assert "scheduler.yaml:sample-new-tick" not in seeded
    assert scheduler.read_bytes() == live
    result = du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"]
    assert result.action == "proposal_opened"
    assert result.proposal and result.proposal.worktree
    proposed = (result.proposal.worktree / "scheduler.yaml").read_text()
    assert "# operator schedule" in proposed
    assert "cron: 30 * * * *" in proposed
    assert "name: sample-new-tick" in proposed
    assert scheduler.read_bytes() == live
    _cleanup_upgrade_proposal(home)


def test_initial_plugin_without_jobs_does_not_seed_jobs_on_later_version(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)
    plugin.manifest.defaults["scheduler_jobs"] = ()
    home = tmp_path / "home"
    home.mkdir()
    scheduler = home / "scheduler.yaml"
    scheduler.write_text(_HEARTBEAT)
    seed_plugin_defaults(home, selected=(plugin,))
    assert (home / ".mimir/seed-plugin-defaults/sample").read_text() == "1.0\n"
    plugin.manifest.defaults["version"] = "2.0"
    plugin.manifest.defaults["scheduler_jobs"] = ("jobs.yaml",)
    seed_plugin_defaults(home, selected=(plugin,))
    assert scheduler.read_text() == _HEARTBEAT


@pytest.mark.parametrize("scheduler_at_install", [False, True])
def test_first_plugin_jobs_after_empty_baseline_are_proposed(
    home: Path, tmp_path: Path, scheduler_at_install: bool,
) -> None:
    alpha = _plugin(tmp_path, name="alpha")
    beta = _plugin(tmp_path, name="beta")
    alpha.manifest.defaults["scheduler_jobs"] = ()
    beta.manifest.defaults["scheduler_jobs"] = ()
    scheduler = home / "scheduler.yaml"
    if scheduler_at_install:
        scheduler.write_text(_HEARTBEAT)
    seed_plugin_defaults(home, selected=(alpha, beta))
    if scheduler_at_install:
        assert scheduler.read_text() == _HEARTBEAT
    else:
        assert not scheduler.exists()
    for owner in ("alpha", "beta"):
        assert (home / ".mimir/seed-plugin-defaults" / owner).read_text() == "1.0\n"
    _git("add", "-f", "prompts/alpha/heartbeat.md", "prompts/beta/heartbeat.md", cwd=home)
    if scheduler_at_install:
        _git("add", "-f", "scheduler.yaml", cwd=home)
    _git("commit", "-q", "-m", "install plugins without jobs", cwd=home)
    _git("push", "-q", cwd=home)
    baseline = du.check_and_open_plugin_defaults_upgrade(home, selected=(alpha, beta))
    assert {owner: result.action for owner, result in baseline.items()} == {
        "alpha": "baseline_initialized", "beta": "baseline_initialized",
    }
    assert _git("show", "mimir-plugin-defaults-alpha:plugins/alpha/scheduler.yaml",
                cwd=home, check=False).returncode != 0

    # The operator may create the missing scheduler later or customize the
    # existing one; neither action opts into live version-bump writes.
    scheduler.write_text("# operator schedule\n" + _HEARTBEAT.replace("0 * * * *", "30 * * * *"))
    _git("add", "-f", "scheduler.yaml", cwd=home)
    _git("commit", "-q", "-m", "operator schedule", cwd=home)
    _git("push", "-q", cwd=home)
    live = scheduler.read_bytes()
    alpha.manifest.defaults["version"] = "2.0"
    alpha.manifest.defaults["scheduler_jobs"] = ("jobs.yaml",)
    seeded = seed_plugin_defaults(home, selected=(alpha, beta))
    assert "scheduler.yaml:alpha-heartbeat" not in seeded
    assert scheduler.read_bytes() == live

    results = du.check_and_open_plugin_defaults_upgrade(home, selected=(alpha, beta))
    assert results["alpha"].action == "proposal_opened"
    assert results["beta"].action == "already_synced"
    assert results["alpha"].proposal and results["alpha"].proposal.worktree
    proposed = results["alpha"].proposal.worktree / "scheduler.yaml"
    body = proposed.read_text()
    assert "# operator schedule" in body
    assert "30 * * * *" in body
    assert "name: alpha-heartbeat" in body
    assert "name: beta-heartbeat" not in body
    assert "scheduler.yaml" in _git("diff", "--cached", "--name-only",
                                    cwd=results["alpha"].proposal.worktree).stdout.splitlines()
    assert scheduler.read_bytes() == live
    assert (home / "prompts/heartbeat.md").read_text() == "heartbeat v1\n"
    assert (home / ".mimir/upgrade-plugin-defaults/alpha").read_text() == "2.0\n"
    assert (home / ".mimir/upgrade-plugin-defaults/beta").read_text() == "1.0\n"
    _cleanup_upgrade_proposal(home)


def test_missing_scheduler_at_install_never_follows_later_symlink(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)
    plugin.manifest.defaults["scheduler_jobs"] = ()
    home = tmp_path / "home"
    seed_plugin_defaults(home, selected=(plugin,))
    assert (home / ".mimir/seed-plugin-defaults/sample").read_text() == "1.0\n"
    assert not (home / "scheduler.yaml").exists()

    outside = tmp_path / "outside.yaml"
    outside.write_text("outside job\n")
    (home / "scheduler.yaml").symlink_to(outside)
    core = home / "prompts/sample/heartbeat.md"
    core.write_text("operator prompt\n")
    plugin.manifest.defaults["version"] = "2.0"
    plugin.manifest.defaults["scheduler_jobs"] = ("jobs.yaml",)
    with pytest.raises(ValueError, match="symlink"):
        seed_plugin_defaults(home, selected=(plugin,))
    assert outside.read_text() == "outside job\n"
    assert core.read_text() == "operator prompt\n"
    assert (home / ".mimir/seed-plugin-defaults/sample").read_text() == "1.0\n"


@pytest.mark.parametrize("symlink", ["skills", "prompts", "scheduler.yaml"])
def test_seed_rejects_symlinked_destinations_before_writing(
    tmp_path: Path, symlink: str,
) -> None:
    plugin = _plugin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside skill\n")
    (outside / "heartbeat.md").write_text("outside prompt\n")
    (outside / "scheduler.yaml").write_text(_HEARTBEAT)
    (home / "prompts").mkdir()
    core = home / "prompts/heartbeat.md"
    core.write_text("core prompt\n")
    if symlink == "scheduler.yaml":
        (home / symlink).symlink_to(outside / symlink)
    else:
        if symlink == "prompts":
            (home / "prompts/heartbeat.md").unlink()
            (home / "prompts").rmdir()
        (home / symlink).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        seed_plugin_defaults(home, selected=(plugin,))
    assert (outside / "SKILL.md").read_text() == "outside skill\n"
    assert (outside / "heartbeat.md").read_text() == "outside prompt\n"
    assert (outside / "scheduler.yaml").read_text() == _HEARTBEAT
    if symlink != "prompts":
        assert core.read_text() == "core prompt\n"
    assert not (home / ".mimir").exists()


def test_plugin_prompt_namespace_cannot_point_into_core(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    (home / "memory/core").mkdir(parents=True)
    core_prompt = home / "prompts/heartbeat.md"
    core_memory = home / "memory/core/heartbeat.md"
    core_prompt.write_text("core prompt\n")
    core_memory.write_text("core memory\n")
    (home / "prompts/sample").symlink_to(core_memory.parent, target_is_directory=True)
    (home / "scheduler.yaml").write_text(_HEARTBEAT)

    with pytest.raises(ValueError, match="symlink"):
        seed_plugin_defaults(home, selected=(plugin,))
    assert core_prompt.read_text() == "core prompt\n"
    assert core_memory.read_text() == "core memory\n"
    assert not (home / "skills").exists()


@pytest.mark.parametrize("target_kind", ["prompt", "prompt_parent", "scheduler"])
def test_plugin_proposal_rejects_symlinked_worktree_target(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str,
) -> None:
    plugin = _plugin(tmp_path)
    (home / "scheduler.yaml").write_text(_HEARTBEAT)
    seed_plugin_defaults(home, selected=(plugin,))
    _git("add", "-f", "scheduler.yaml", "prompts/sample/heartbeat.md", cwd=home)
    _git("commit", "-q", "-m", "plugin baseline", cwd=home)
    _git("push", "-q", cwd=home)
    assert du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"].action == "baseline_initialized"
    plugin.manifest.defaults["version"] = "2.0"
    (tmp_path / "sample/prompts/heartbeat.md").write_text("new plugin prompt\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "heartbeat.md").write_text("outside text\n")
    (outside / "scheduler.yaml").write_text("outside schedule\n")
    original_open = du.open_proposal

    def open_with_symlink(*args, **kwargs):
        opened = original_open(*args, **kwargs)
        assert opened.worktree
        target = opened.worktree / "prompts/sample/heartbeat.md"
        if target_kind == "scheduler":
            scheduler = opened.worktree / "scheduler.yaml"
            scheduler.unlink()
            scheduler.symlink_to(outside / "scheduler.yaml")
        else:
            target.unlink()
            if target_kind == "prompt_parent":
                target.parent.rmdir()
                target.parent.symlink_to(outside, target_is_directory=True)
            else:
                target.symlink_to(outside / "heartbeat.md")
        return opened

    monkeypatch.setattr(du, "open_proposal", open_with_symlink)
    result = du.check_and_open_plugin_defaults_upgrade(home, selected=(plugin,))["sample"]
    assert not result.ok and result.action == "error"
    assert "symlink" in (result.detail or "")
    assert (outside / "heartbeat.md").read_text() == "outside text\n"
    assert (outside / "scheduler.yaml").read_text() == "outside schedule\n"
    assert (home / "prompts/heartbeat.md").read_text() == "heartbeat v1\n"
    assert (home / ".mimir/upgrade-plugin-defaults/sample").read_text() == "1.0\n"
    assert list_open_proposals(home, lane="upgrade") == []
