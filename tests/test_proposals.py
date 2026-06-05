"""Tests for ``mimir.proposals`` — the sandbox change-proposal workflow (#337/#339/#344).

Real ``git`` against ``tmp_path``; the "remote" is a bare repo in a second tmp
dir (no network). The PR step is injected (``open_pr``). The load-bearing
properties: (1) the proposal worktree lives under the gitignored ``scratch/``
so the home's per-turn ``git add -A`` never grabs it as an embedded repo, and
(2) editing the worktree never moves the live checkout the runtime reads.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import mimir
from mimir.proposals import (
    abandon_proposal,
    default_branch_name,
    finalize_proposal,
    list_open_proposals,
    open_proposal,
    render_open_proposals_block,
    normalize_lane,
)

TEMPLATE = Path(mimir.__file__).parent / "templates" / "git" / "gitignore"
SEED = "# Learned behaviors\n\n- original entry\n"


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check
    )


def _init(path: Path, *, bare: bool = False) -> None:
    if bare:
        subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main", str(path)], check=True
        )
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
    """Home repo using the real .gitignore template (which ignores scratch/),
    with seeded core memory, pushed to the bare upstream."""
    h = tmp_path / "home"
    (h / "memory" / "core").mkdir(parents=True)
    (h / "memory" / "core" / "40-learned-behaviors.md").write_text(SEED, encoding="utf-8")
    # A tracked prompt template — prompts/ is the second proposable surface.
    (h / "prompts").mkdir()
    (h / "prompts" / "reflect.md").write_text("# reflect\n\noriginal prompt\n", encoding="utf-8")
    # A tracked NON-surface file, to prove the sparse checkout excludes it.
    (h / "skills").mkdir()
    (h / "skills" / "x.md").write_text("skill", encoding="utf-8")
    shutil.copy(TEMPLATE, h / ".gitignore")
    _init(h)
    _git("add", "-A", cwd=h)
    _git("commit", "-q", "-m", "seed", cwd=h)
    _git("remote", "add", "origin", str(upstream), cwd=h)
    _git("push", "-q", "-u", "origin", "main", cwd=h)
    return h


def _opener(calls: list[dict]):
    def f(home, branch, base, title, body):
        calls.append({"branch": branch, "base": base, "title": title, "body": body})
        return "https://github.com/jasoncarreira/mimirbot/pull/1"

    return f


# ─── open ────────────────────────────────────────────────────────────


def test_open_creates_worktree_under_scratch(home: Path) -> None:
    r = open_proposal(home)
    assert r.ok and r.worktree is not None
    assert r.worktree.is_dir()
    assert "scratch/proposals" in str(r.worktree.relative_to(home))
    # It's a real checkout: the seeded core + prompt files are present to edit.
    assert (r.worktree / "memory" / "core" / "40-learned-behaviors.md").read_text() == SEED
    assert (r.worktree / "prompts" / "reflect.md").exists()
    # Sparse: only the proposable surfaces (memory/core + prompts) are
    # materialized, not other tracked subtrees.
    assert not (r.worktree / "skills" / "x.md").exists()
    assert [b for b, _ in list_open_proposals(home)] == [r.branch]
    # The worktree (an embedded repo) is invisible to the home's `git add -A`.
    assert "scratch" not in _git("add", "-A", "--dry-run", cwd=home).stdout


def test_open_no_remote(tmp_path: Path) -> None:
    h = tmp_path / "h"
    (h / "memory" / "core").mkdir(parents=True)
    (h / "memory" / "core" / "40-learned-behaviors.md").write_text(SEED)
    _init(h)
    _git("add", "-A", cwd=h)
    _git("commit", "-q", "-m", "seed", cwd=h)
    r = open_proposal(h)
    assert not r.ok and r.reason == "no_remote"


def test_open_one_at_a_time_per_lane(home: Path) -> None:
    r1 = open_proposal(home)
    assert r1.ok
    r2 = open_proposal(home)
    assert not r2.ok and r2.reason == "exists" and r2.branch == r1.branch

    # A simultaneous upgrade-lane proposal is allowed and has its own branch
    # prefix + worktree namespace.
    upgrade = open_proposal(home, lane="upgrade")
    assert upgrade.ok and upgrade.branch is not None and upgrade.worktree is not None
    assert upgrade.branch.startswith("upgrade/")
    assert "scratch/proposals/upgrade" in str(upgrade.worktree.relative_to(home))
    assert sorted(b for b, _ in list_open_proposals(home)) == sorted([r1.branch, upgrade.branch])
    assert list_open_proposals(home, lane="agent") == [(r1.branch, r1.worktree)]
    assert list_open_proposals(home, lane="upgrade") == [(upgrade.branch, upgrade.worktree)]


def test_open_upgrade_lane_one_at_a_time(home: Path) -> None:
    r1 = open_proposal(home, lane="upgrade")
    assert r1.ok
    r2 = open_proposal(home, lane="upgrade")
    assert not r2.ok and r2.reason == "exists" and r2.branch == r1.branch


def test_invalid_lane_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported proposal lane"):
        normalize_lane("manual")


# ─── submit / finalize ───────────────────────────────────────────────


def test_finalize_commits_pushes_prs_and_leaves_live_untouched(home: Path) -> None:
    r = open_proposal(home)
    assert r.ok
    # Act as the agent: edit a core file in the worktree with a plain write.
    (r.worktree / "memory" / "core" / "40-learned-behaviors.md").write_text(
        SEED + "- NEW entry\n", encoding="utf-8"
    )
    calls: list[dict] = []
    res = finalize_proposal(
        home, title="Add NEW", rationale="seen repeatedly", open_pr=_opener(calls)
    )
    assert res.ok and res.pushed
    assert res.pr_url == "https://github.com/jasoncarreira/mimirbot/pull/1"
    assert calls[0]["title"] == "Add NEW"
    assert "seen repeatedly" in calls[0]["body"]
    # Live core never moved.
    assert (home / "memory" / "core" / "40-learned-behaviors.md").read_text() == SEED
    assert _git("status", "--porcelain", cwd=home).stdout.strip() == ""
    # The pushed branch carries the change.
    shown = _git(
        "show", f"origin/{res.branch}:memory/core/40-learned-behaviors.md", cwd=home
    ).stdout
    assert "- NEW entry" in shown
    # Worktree torn down.
    assert list_open_proposals(home) == []
    assert not r.worktree.exists()


def test_finalize_no_changes_keeps_worktree(home: Path) -> None:
    r = open_proposal(home)
    assert r.ok
    res = finalize_proposal(home, title="t", rationale="r", open_pr=_opener([]))
    assert not res.ok and res.reason == "no_changes"
    # Left intact so the agent can edit + resubmit.
    assert list_open_proposals(home) and r.worktree.exists()


def test_finalize_rejects_secret_in_content(home: Path) -> None:
    r = open_proposal(home)
    assert r.ok
    token = "ghp_" + "A" * 36
    (r.worktree / "memory" / "core" / "40-learned-behaviors.md").write_text(
        SEED + f"- saw token {token}\n", encoding="utf-8"
    )
    res = finalize_proposal(home, title="t", rationale="r", open_pr=_opener([]))
    assert not res.ok and res.reason == "secret"
    # Nothing pushed; worktree intact for the agent to fix.
    assert list_open_proposals(home)


def test_finalize_stages_both_surfaces_only(home: Path) -> None:
    r = open_proposal(home)
    assert r.ok
    # Edit BOTH proposable surfaces in the worktree — one proposal, one PR.
    (r.worktree / "memory" / "core" / "40-learned-behaviors.md").write_text(
        SEED + "- core change\n", encoding="utf-8"
    )
    (r.worktree / "prompts" / "reflect.md").write_text(
        "# reflect\n\nrevised prompt\n", encoding="utf-8"
    )
    # Also touch a tracked non-surface file in the worktree; it must NOT reach the PR.
    (r.worktree / "state").mkdir(exist_ok=True)
    (r.worktree / "state" / "proposed-changes.md").write_text("stray\n", encoding="utf-8")
    res = finalize_proposal(home, title="t", rationale="r", open_pr=_opener([]))
    assert res.ok
    files = _git("show", "--name-only", "--format=", f"origin/{res.branch}", cwd=home).stdout
    assert "memory/core/40-learned-behaviors.md" in files
    assert "prompts/reflect.md" in files
    assert "proposed-changes" not in files


def test_finalize_proposes_prompts_only_change(home: Path) -> None:
    """A prompts-only edit (no core change) is a valid proposal — prompts is a
    first-class proposable surface, not just along for the ride."""
    r = open_proposal(home)
    assert r.ok
    (r.worktree / "prompts" / "reflect.md").write_text(
        "# reflect\n\nprompts-only revision\n", encoding="utf-8"
    )
    res = finalize_proposal(home, title="tweak prompt", rationale="clearer", open_pr=_opener([]))
    assert res.ok and res.pushed
    # Live prompt never moved.
    assert (home / "prompts" / "reflect.md").read_text() == "# reflect\n\noriginal prompt\n"
    shown = _git("show", f"origin/{res.branch}:prompts/reflect.md", cwd=home).stdout
    assert "prompts-only revision" in shown


def test_finalize_pushes_without_pr_when_opener_returns_none(home: Path) -> None:
    r = open_proposal(home)
    assert r.ok
    (r.worktree / "memory" / "core" / "40-learned-behaviors.md").write_text(
        SEED + "- x\n", encoding="utf-8"
    )
    res = finalize_proposal(home, title="t", rationale="r", open_pr=lambda *a: None)
    assert res.ok and res.pushed and res.pr_url is None and res.reason == "pushed_no_pr"


def test_finalize_selects_requested_lane(home: Path) -> None:
    agent = open_proposal(home)
    upgrade = open_proposal(home, lane="upgrade")
    assert agent.ok and upgrade.ok
    (agent.worktree / "memory" / "core" / "40-learned-behaviors.md").write_text(
        SEED + "- agent\n", encoding="utf-8"
    )
    (upgrade.worktree / "prompts" / "reflect.md").write_text(
        "# reflect\n\nupgrade revision\n", encoding="utf-8"
    )
    calls: list[dict] = []
    res = finalize_proposal(
        home, title="Upgrade defaults", rationale="new release", lane="upgrade", open_pr=_opener(calls)
    )
    assert res.ok and res.branch == upgrade.branch
    assert calls and "Proposal lane: `upgrade`" in calls[0]["body"]
    assert list_open_proposals(home, lane="upgrade") == []
    assert list_open_proposals(home, lane="agent") == [(agent.branch, agent.worktree)]


# ─── abandon ─────────────────────────────────────────────────────────


def test_abandon(home: Path) -> None:
    r = open_proposal(home)
    upgrade = open_proposal(home, lane="upgrade")
    assert r.ok and upgrade.ok
    assert abandon_proposal(home, lane="upgrade") is True
    assert list_open_proposals(home, lane="upgrade") == []
    assert list_open_proposals(home, lane="agent") == [(r.branch, r.worktree)]
    assert not upgrade.worktree.exists()
    assert abandon_proposal(home, lane="upgrade") is False  # nothing open in that lane now
    assert abandon_proposal(home) is True
    assert list_open_proposals(home) == []
    assert not r.worktree.exists()


# ─── resolved-branch cleanup ──────────────────────────────────────────


def test_cleanup_resolved_squash_merged_branch_removes_remote_local_worktree_and_logs(
    home: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = open_proposal(home, branch="proposal/cleanup-test")
    assert r.ok
    target = r.worktree / "memory" / "core" / "40-learned-behaviors.md"
    target.write_text(SEED + "- cleanup landed\n", encoding="utf-8")
    res = finalize_proposal(
        home, title="cleanup", rationale="r", open_pr=lambda *a: "url"
    )
    assert res.ok

    # Simulate GitHub squash-merging the proposal: main receives identical
    # protected-surface content via a different commit SHA, while the proposal
    # branch remains on the remote.
    remote_work = home.parent / "remote-work"
    _git("clone", "-q", str(upstream), str(remote_work), cwd=home)
    _git("config", "user.email", "remote@example.com", cwd=remote_work)
    _git("config", "user.name", "remote", cwd=remote_work)
    (remote_work / "memory" / "core" / "40-learned-behaviors.md").write_text(
        SEED + "- cleanup landed\n", encoding="utf-8"
    )
    _git("add", "memory/core/40-learned-behaviors.md", cwd=remote_work)
    _git("commit", "-q", "-m", "squash proposal", cwd=remote_work)
    _git("push", "-q", "origin", "main", cwd=remote_work)

    # Recreate a local worktree for the now-resolved remote branch; cleanup must
    # remove both remote branch and local worktree/branch.
    local_wt = home / "scratch" / "proposals" / "agent" / "proposal_cleanup-test"
    _git("fetch", "origin", "proposal/cleanup-test", cwd=home)
    _git(
        "worktree", "add", "--no-checkout", "-b", "proposal/cleanup-test",
        str(local_wt), "origin/proposal/cleanup-test", cwd=home,
    )

    events: list[dict] = []

    def fake_log(event_type: str, **payload) -> None:  # type: ignore[no-untyped-def]
        events.append({"type": event_type, **payload})

    monkeypatch.setattr("mimir.proposals.log_event_sync", fake_log, raising=False)
    monkeypatch.setattr(
        "mimir.proposals._proposal_branch_has_open_pr", lambda home, branch: False
    )

    from mimir.proposals import cleanup_resolved_proposal_branches

    records = cleanup_resolved_proposal_branches(home)
    deleted = [r for r in records if r.branch == "proposal/cleanup-test"]
    assert deleted and deleted[0].action == "deleted"
    assert deleted[0].tip
    assert "proposal/cleanup-test" not in _git(
        "ls-remote", "--heads", "origin", "proposal/cleanup-test", cwd=home
    ).stdout
    assert not local_wt.exists()
    assert events[-1]["type"] == "proposal_branch_cleaned"
    assert events[-1]["branch"] == "proposal/cleanup-test"
    assert events[-1]["tip"] == deleted[0].tip


def test_cleanup_preserves_open_pr_branch(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = open_proposal(home, branch="proposal/open-pr")
    assert r.ok
    (r.worktree / "prompts" / "reflect.md").write_text(
        "# reflect\n\nopen\n", encoding="utf-8"
    )
    res = finalize_proposal(
        home, title="open", rationale="r", open_pr=lambda *a: "url"
    )
    assert res.ok

    monkeypatch.setattr(
        "mimir.proposals._proposal_branch_has_open_pr", lambda home, branch: True
    )
    from mimir.proposals import cleanup_resolved_proposal_branches

    records = cleanup_resolved_proposal_branches(home)
    skipped = [r for r in records if r.branch == "proposal/open-pr"]
    assert skipped and skipped[0].action == "skipped" and skipped[0].reason == "open_pr"
    assert "refs/heads/proposal/open-pr" in _git(
        "ls-remote", "--heads", "origin", "proposal/open-pr", cwd=home
    ).stdout


def test_cleanup_skips_unmerged_novel_branch(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = open_proposal(home, branch="proposal/novel")
    assert r.ok
    (r.worktree / "prompts" / "reflect.md").write_text("# reflect\n\nnovel\n", encoding="utf-8")
    res = finalize_proposal(
        home, title="novel", rationale="r", open_pr=lambda *a: "url"
    )
    assert res.ok

    monkeypatch.setattr(
        "mimir.proposals._proposal_branch_has_open_pr", lambda home, branch: False
    )
    from mimir.proposals import cleanup_resolved_proposal_branches

    records = cleanup_resolved_proposal_branches(home)
    skipped = [r for r in records if r.branch == "proposal/novel"]
    assert skipped and skipped[0].action == "skipped"
    assert skipped[0].reason == "content_not_on_main"
    assert "refs/heads/proposal/novel" in _git(
        "ls-remote", "--heads", "origin", "proposal/novel", cwd=home
    ).stdout


def test_cleanup_skips_non_surface_changes_even_if_open_pr_closed(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = open_proposal(home, branch="proposal/non-surface")
    assert r.ok
    # Force a non-proposal-surface commit onto the proposal branch to verify the
    # cleanup sweep does not delete branches whose content is outside its safety
    # envelope.
    (r.worktree / "state").mkdir()
    (r.worktree / "state" / "note.md").write_text(
        "not a proposal surface\n", encoding="utf-8"
    )
    _git("add", "--sparse", "state/note.md", cwd=r.worktree)
    _git("commit", "-q", "-m", "non surface", cwd=r.worktree)
    _git("push", "-q", "-u", "origin", "proposal/non-surface", cwd=r.worktree)
    _git("worktree", "remove", "--force", str(r.worktree), cwd=home)
    _git("branch", "-D", "proposal/non-surface", cwd=home)

    monkeypatch.setattr(
        "mimir.proposals._proposal_branch_has_open_pr", lambda home, branch: False
    )
    from mimir.proposals import cleanup_resolved_proposal_branches

    records = cleanup_resolved_proposal_branches(home)
    skipped = [r for r in records if r.branch == "proposal/non-surface"]
    assert skipped and skipped[0].action == "skipped"
    assert skipped[0].reason == "content_not_on_main"
    assert "refs/heads/proposal/non-surface" in _git(
        "ls-remote", "--heads", "origin", "proposal/non-surface", cwd=home
    ).stdout

# ─── scratch self-heal ───────────────────────────────────────────────


def test_open_self_heals_unignored_scratch(tmp_path: Path, upstream: Path) -> None:
    """A home whose .gitignore doesn't yet ignore scratch/ — open must append
    the rule (else a worktree there would be grabbed by `git add -A`)."""
    h = tmp_path / "h2"
    (h / "memory" / "core").mkdir(parents=True)
    (h / "memory" / "core" / "40-learned-behaviors.md").write_text(SEED)
    initial = "*\n!*/\n!memory/**\n!.gitignore\n"
    (h / ".gitignore").write_text(initial)
    _init(h)
    _git("add", "-A", cwd=h)
    _git("commit", "-q", "-m", "seed", cwd=h)
    _git("remote", "add", "origin", str(upstream), cwd=h)
    _git("push", "-q", "-u", "origin", "main", cwd=h)
    # Precondition: no explicit scratch/ rule yet.
    assert "scratch/" not in initial

    r = open_proposal(h)
    assert r.ok
    # open() self-healed the ignore — and the real safety property holds:
    # the worktree (an embedded repo) is invisible to the home's `git add -A`.
    assert "scratch/" in (h / ".gitignore").read_text()
    assert "scratch" not in _git("add", "-A", "--dry-run", cwd=h).stdout


def test_default_branch_name() -> None:
    assert default_branch_name("Add a Rule!", ts=5) == "proposal/add-a-rule-5"
    assert default_branch_name(ts=9) == "proposal/proposal-9"
    assert default_branch_name("Sync Defaults", ts=10, lane="upgrade") == "upgrade/sync-defaults-10"
    assert default_branch_name(ts=11, lane="upgrade") == "upgrade/upgrade-11"


# ─── live-status nudge ───────────────────────────────────────────────


def test_render_open_proposals_block(home: Path) -> None:
    # Nothing open → no nudge.
    assert render_open_proposals_block(home) is None
    r = open_proposal(home)
    assert r.ok
    upgrade = open_proposal(home, lane="upgrade")
    assert upgrade.ok
    block = render_open_proposals_block(home)
    assert block is not None
    assert r.branch in block and upgrade.branch in block
    assert "lane `agent`" in block and "lane `upgrade`" in block
    assert "submit_proposal" in block
    assert "abandon_proposal" in block
    # Auto-clears lane-by-lane once proposals are gone.
    abandon_proposal(home)
    assert render_open_proposals_block(home) is not None
    abandon_proposal(home, lane="upgrade")
    assert render_open_proposals_block(home) is None


def test_proposal_pr_opened_classifies_positive() -> None:
    from mimir.feedback.rules import classify

    assert classify("proposal_pr_opened") == ("positive", "proposal_pr_opened")
