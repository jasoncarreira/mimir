"""Tests for ``mimir.proposals`` — the sandbox change-proposal workflow (#337/#339/#344).

Real ``git`` against ``tmp_path``; the "remote" is a bare repo in a second tmp
dir (no network). The PR step is injected (``open_pr``). The load-bearing
properties: (1) the proposal worktree lives under the gitignored ``scratch/``
so the home's per-turn ``git add -A`` never grabs it as an embedded repo, and
(2) editing the worktree never moves the live checkout the runtime reads.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import mimir
from mimir.proposals import (
    PollerProposalScope,
    poller_branch_name,
    poller_worktree_path,
    abandon_proposal,
    default_branch_name,
    finalize_proposal,
    list_open_proposals,
    open_proposal,
    ProposalPrError,
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


def test_finalize_rejects_conflict_markers_in_protected_surfaces(home: Path) -> None:
    r = open_proposal(home, lane="upgrade")
    assert r.ok
    (r.worktree / "prompts" / "reflect.md").write_text(
        "# reflect\n\n<<<<<<< home\noperator prompt\n=======\ndefault prompt\n>>>>>>> mimir-defaults\n",
        encoding="utf-8",
    )

    res = finalize_proposal(home, title="upgrade", rationale="r", lane="upgrade", open_pr=_opener([]))

    assert not res.ok and res.reason == "conflict_marker"
    assert res.branch == r.branch
    assert "prompts/reflect.md" in (res.detail or "")
    assert list_open_proposals(home, lane="upgrade") == [(r.branch, r.worktree)]
    assert r.worktree.exists()
    assert r.branch not in _git("branch", "-r", cwd=home).stdout


def test_finalize_allows_setext_heading_equals_underline(home: Path) -> None:
    r = open_proposal(home, lane="upgrade")
    assert r.ok
    (r.worktree / "prompts" / "reflect.md").write_text(
        "# reflect\n\nLegitimate setext heading\n========\n\nbody\n",
        encoding="utf-8",
    )

    calls: list[dict] = []
    res = finalize_proposal(home, title="upgrade", rationale="r", lane="upgrade", open_pr=_opener(calls))

    assert res.ok and res.pushed
    assert calls
    assert list_open_proposals(home, lane="upgrade") == []


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


def test_finalize_reports_failure_when_pr_is_not_opened(home: Path) -> None:
    r = open_proposal(home)
    assert r.ok
    (r.worktree / "memory" / "core" / "40-learned-behaviors.md").write_text(
        SEED + "- x\n", encoding="utf-8"
    )
    res = finalize_proposal(home, title="t", rationale="r", open_pr=lambda *a: None)
    assert not res.ok and res.pushed and res.pr_url is None and res.reason == "pr_open"
    assert "returned no URL" in (res.detail or "")


def test_finalize_preserves_pr_create_failure_detail(home: Path) -> None:
    r = open_proposal(home)
    assert r.ok
    (r.worktree / "prompts" / "reflect.md").write_text(
        "# reflect\n\nchanged\n", encoding="utf-8"
    )

    def fail_open(*args):
        raise ProposalPrError("gh pr create failed: authentication required")

    res = finalize_proposal(home, title="t", rationale="r", open_pr=fail_open)

    assert not res.ok and res.pushed and res.reason == "pr_open"
    assert res.detail == "gh pr create failed: authentication required"
    assert list_open_proposals(home) == []
    assert f"refs/heads/{res.branch}" in _git(
        "ls-remote", "--heads", "origin", res.branch, cwd=home
    ).stdout


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
        "mimir.proposals._proposal_branch_pr_state", lambda home, branch: "no_pr"
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
        "mimir.proposals._proposal_branch_pr_state", lambda home, branch: "open"
    )
    from mimir.proposals import cleanup_resolved_proposal_branches

    records = cleanup_resolved_proposal_branches(home)
    skipped = [r for r in records if r.branch == "proposal/open-pr"]
    assert skipped and skipped[0].action == "skipped" and skipped[0].reason == "open_pr"
    assert "refs/heads/proposal/open-pr" in _git(
        "ls-remote", "--heads", "origin", "proposal/open-pr", cwd=home
    ).stdout


@pytest.mark.parametrize(
    ("forge_state", "expected"),
    [("OPEN", "open"), ("MERGED", "merged"), ("CLOSED", "closed")],
)
def test_proposal_branch_pr_state_distinguishes_forge_states(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    forge_state: str,
    expected: str,
) -> None:
    import mimir.proposals as proposals

    monkeypatch.setattr(proposals.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        requested_state = args[args.index("--state") + 1]
        prs = (
            [{"number": 1, "state": "OPEN"}]
            if requested_state == "open" and forge_state == "OPEN"
            else []
            if requested_state == "open"
            else [{"number": 1, "state": forge_state}]
        )
        return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")

    monkeypatch.setattr(proposals, "_run", fake_run)

    assert proposals._proposal_branch_pr_state(home, "proposal/test") == expected


def test_proposal_branch_pr_state_open_wins_over_terminal_result_order(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.proposals as proposals

    monkeypatch.setattr(proposals.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        requested_state = args[args.index("--state") + 1]
        prs = (
            [{"number": 2, "state": "OPEN"}]
            if requested_state == "open"
            else [
                {"number": 1, "state": "CLOSED"},
                {"number": 2, "state": "OPEN"},
            ]
        )
        return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")

    monkeypatch.setattr(proposals, "_run", fake_run)

    assert proposals._proposal_branch_pr_state(home, "proposal/test") == "open"


def test_proposal_branch_pr_state_reports_no_pr(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.proposals as proposals

    monkeypatch.setattr(proposals.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(
        proposals,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "[]", ""),
    )

    assert proposals._proposal_branch_pr_state(home, "proposal/test") == "no_pr"


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        ("[]", 1),
        ("not json", 0),
        ('{"state":"MERGED"}', 0),
        ('[{"state":"MERGED"}]', 0),
        ('[{"number":1,"state":"OTHER"}]', 0),
    ],
)
def test_proposal_branch_pr_state_fails_closed_on_command_or_output_errors(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    returncode: int,
) -> None:
    import mimir.proposals as proposals

    monkeypatch.setattr(proposals.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(
        proposals,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], returncode, stdout, "failure"
        ),
    )

    assert proposals._proposal_branch_pr_state(home, "proposal/test") is None


def test_proposal_branch_pr_state_fails_closed_when_gh_is_missing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.proposals as proposals

    monkeypatch.setattr(proposals.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        proposals, "_run", lambda *args, **kwargs: pytest.fail("gh must not run")
    )

    assert proposals._proposal_branch_pr_state(home, "proposal/test") is None


def test_proposal_branch_pr_state_fails_closed_on_network_error(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.proposals as proposals

    def raise_network_error(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("network")

    monkeypatch.setattr(proposals.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(proposals, "_run", raise_network_error)

    assert proposals._proposal_branch_pr_state(home, "proposal/test") is None


@pytest.mark.parametrize("pr_state", ["closed", "merged"])
def test_cleanup_sweeps_terminal_pr_without_consulting_content(
    home: Path, monkeypatch: pytest.MonkeyPatch, pr_state: str
) -> None:
    branch = f"proposal/{pr_state}-pr"
    r = open_proposal(home, branch=branch)
    assert r.ok
    (r.worktree / "prompts" / "reflect.md").write_text(
        f"# reflect\n\n{pr_state}\n", encoding="utf-8"
    )
    res = finalize_proposal(
        home, title=pr_state, rationale="r", open_pr=lambda *a: "url"
    )
    assert res.ok

    monkeypatch.setattr(
        "mimir.proposals._proposal_branch_pr_state", lambda home, branch: pr_state
    )
    monkeypatch.setattr(
        "mimir.proposals._proposal_branch_content_is_on_main",
        lambda *args, **kwargs: pytest.fail("terminal PR state must bypass content"),
    )

    from mimir.proposals import cleanup_resolved_proposal_branches

    records = cleanup_resolved_proposal_branches(home)
    deleted = [record for record in records if record.branch == branch]
    assert deleted and deleted[0].action == "deleted"
    assert deleted[0].reason == f"resolved_pr_{pr_state}"
    assert deleted[0].tip
    assert branch not in _git("ls-remote", "--heads", "origin", branch, cwd=home).stdout


def test_cleanup_sweeps_merged_pr_after_main_changes_proposal_blob(
    home: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = "upgrade/merged-then-changed"
    r = open_proposal(home, branch=branch, lane="upgrade")
    assert r.ok
    (r.worktree / "prompts" / "reflect.md").write_text(
        "# reflect\n\nmerged version\n", encoding="utf-8"
    )
    res = finalize_proposal(
        home, title="merged", rationale="r", lane="upgrade", open_pr=lambda *a: "url"
    )
    assert res.ok

    remote_work = home.parent / "merged-then-changed-work"
    _git("clone", "-q", str(upstream), str(remote_work), cwd=home)
    _git("config", "user.email", "remote@example.com", cwd=remote_work)
    _git("config", "user.name", "remote", cwd=remote_work)
    (remote_work / "prompts" / "reflect.md").write_text(
        "# reflect\n\nlater main version\n", encoding="utf-8"
    )
    _git("add", "prompts/reflect.md", cwd=remote_work)
    _git("commit", "-q", "-m", "change merged proposal content later", cwd=remote_work)
    _git("push", "-q", "origin", "main", cwd=remote_work)

    monkeypatch.setattr(
        "mimir.proposals._proposal_branch_pr_state", lambda home, branch: "merged"
    )
    from mimir.proposals import cleanup_resolved_proposal_branches

    records = cleanup_resolved_proposal_branches(home)
    deleted = [record for record in records if record.branch == branch]
    assert deleted and deleted[0].action == "deleted"
    assert deleted[0].reason == "resolved_pr_merged"
    assert branch not in _git("ls-remote", "--heads", "origin", branch, cwd=home).stdout


def test_cleanup_skips_fetch_when_no_local_proposal_refs(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.proposals as proposals

    calls: list[list[str]] = []
    real_git = proposals._git

    def spy_git(args: list[str], cwd: Path):  # type: ignore[no-untyped-def]
        calls.append(args)
        return real_git(args, cwd)

    monkeypatch.setattr(proposals, "_git", spy_git)

    records = proposals.cleanup_resolved_proposal_branches(home)

    assert records == []
    assert ["fetch", "--prune", "origin"] not in calls


@pytest.mark.parametrize("pr_state", ["open", "merged", "closed", "no_pr", None])
def test_cleanup_never_considers_branch_outside_proposal_prefixes(
    home: Path, monkeypatch: pytest.MonkeyPatch, pr_state: str | None
) -> None:
    _git("branch", "unrelated", cwd=home)
    _git("push", "-q", "origin", "unrelated", cwd=home)
    calls: list[str] = []

    def pr_lookup(home: Path, branch: str) -> str | None:
        calls.append(branch)
        return pr_state

    monkeypatch.setattr("mimir.proposals._proposal_branch_pr_state", pr_lookup)
    from mimir.proposals import cleanup_resolved_proposal_branches

    assert cleanup_resolved_proposal_branches(home) == []
    assert calls == []
    assert "refs/heads/unrelated" in _git(
        "ls-remote", "--heads", "origin", "unrelated", cwd=home
    ).stdout


@pytest.mark.parametrize("failure", ["missing", "nonzero", "malformed", "network"])
def test_cleanup_skips_branch_when_pr_status_unknown_and_logs(
    home: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    r = open_proposal(home, branch="proposal/open-pr-unknown")
    assert r.ok
    (r.worktree / "prompts" / "reflect.md").write_text(
        "# reflect\n\nunknown\n", encoding="utf-8"
    )
    res = finalize_proposal(
        home, title="unknown", rationale="r", open_pr=lambda *a: "url"
    )
    assert res.ok

    events: list[dict] = []

    def fake_log(event_type: str, **payload) -> None:  # type: ignore[no-untyped-def]
        events.append({"type": event_type, **payload})

    monkeypatch.setattr("mimir.proposals.log_event_sync", fake_log, raising=False)
    if failure == "missing":
        monkeypatch.setattr("mimir.proposals.shutil.which", lambda name: None)
    else:
        import mimir.proposals as proposals

        real_run = proposals._run
        monkeypatch.setattr(
            "mimir.proposals.shutil.which", lambda name: "/usr/bin/gh"
        )

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            if args[0][0] != "gh":
                return real_run(*args, **kwargs)
            if failure == "network":
                raise OSError("network")
            if failure == "nonzero":
                return subprocess.CompletedProcess(args[0], 1, "", "failure")
            return subprocess.CompletedProcess(args[0], 0, "not json", "")

        monkeypatch.setattr("mimir.proposals._run", fake_run)

    from mimir.proposals import cleanup_resolved_proposal_branches

    records = cleanup_resolved_proposal_branches(home)
    skipped = [r for r in records if r.branch == "proposal/open-pr-unknown"]
    assert skipped and skipped[0].action == "skipped"
    assert skipped[0].reason == "open_pr_unknown"
    assert "refs/heads/proposal/open-pr-unknown" in _git(
        "ls-remote", "--heads", "origin", "proposal/open-pr-unknown", cwd=home
    ).stdout
    assert events[-1]["type"] == "proposal_branch_cleanup_skipped"
    assert events[-1]["branch"] == "proposal/open-pr-unknown"
    assert events[-1]["reason"] == "open_pr_unknown"


def test_cleanup_skips_unmerged_novel_branch(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = open_proposal(home, branch="proposal/novel")
    assert r.ok
    (r.worktree / "prompts" / "reflect.md").write_text("# reflect\n\nnovel\n", encoding="utf-8")
    res = finalize_proposal(
        home, title="novel", rationale="r", open_pr=lambda *a: "url"
    )
    assert res.ok

    monkeypatch.setattr(
        "mimir.proposals._proposal_branch_pr_state", lambda home, branch: "no_pr"
    )
    from mimir.proposals import cleanup_resolved_proposal_branches

    records = cleanup_resolved_proposal_branches(home)
    skipped = [r for r in records if r.branch == "proposal/novel"]
    assert skipped and skipped[0].action == "skipped"
    assert skipped[0].reason == "content_not_on_main"
    assert "refs/heads/proposal/novel" in _git(
        "ls-remote", "--heads", "origin", "proposal/novel", cwd=home
    ).stdout


def test_cleanup_skip_events_are_edge_triggered_until_eventual_cleanup(
    home: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = open_proposal(home, branch="proposal/pending")
    assert r.ok
    (r.worktree / "prompts" / "reflect.md").write_text(
        "# reflect\n\npending\n", encoding="utf-8"
    )
    res = finalize_proposal(
        home, title="pending", rationale="r", open_pr=lambda *a: "url"
    )
    assert res.ok

    events: list[dict] = []
    pr_state = "open"

    def fake_log(event_type: str, **payload) -> None:  # type: ignore[no-untyped-def]
        events.append({"type": event_type, **payload})

    monkeypatch.setattr("mimir.proposals.log_event_sync", fake_log)
    monkeypatch.setattr(
        "mimir.proposals._proposal_branch_pr_state",
        lambda home, branch: pr_state,
    )
    from mimir.proposals import cleanup_resolved_proposal_branches

    records = cleanup_resolved_proposal_branches(
        home, previous_skip_reasons={}
    )
    previous = {record.branch: record.reason for record in records}
    assert [(event["type"], event["reason"]) for event in events] == [
        ("proposal_branch_cleanup_skipped", "open_pr")
    ]

    cleanup_resolved_proposal_branches(
        home, previous_skip_reasons=previous
    )
    assert len(events) == 1

    pr_state = "no_pr"
    records = cleanup_resolved_proposal_branches(
        home, previous_skip_reasons=previous
    )
    previous = {record.branch: record.reason for record in records}
    assert [(event["type"], event["reason"]) for event in events] == [
        ("proposal_branch_cleanup_skipped", "open_pr"),
        ("proposal_branch_cleanup_skipped", "content_not_on_main"),
    ]

    remote_work = home.parent / "pending-remote-work"
    _git("clone", "-q", str(upstream), str(remote_work), cwd=home)
    _git("config", "user.email", "remote@example.com", cwd=remote_work)
    _git("config", "user.name", "remote", cwd=remote_work)
    (remote_work / "prompts" / "reflect.md").write_text(
        "# reflect\n\npending\n", encoding="utf-8"
    )
    _git("add", "prompts/reflect.md", cwd=remote_work)
    _git("commit", "-q", "-m", "land pending proposal", cwd=remote_work)
    _git("push", "-q", "origin", "main", cwd=remote_work)

    records = cleanup_resolved_proposal_branches(
        home, previous_skip_reasons=previous
    )
    assert records == [
        next(record for record in records if record.branch == "proposal/pending")
    ]
    assert records[0].action == "deleted"
    assert events[-1]["type"] == "proposal_branch_cleaned"
    assert "proposal/pending" not in _git(
        "ls-remote", "--heads", "origin", "proposal/pending", cwd=home
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
        "mimir.proposals._proposal_branch_pr_state", lambda home, branch: "no_pr"
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


@pytest.fixture
def wiki_home(home: Path) -> Path:
    (home / "state/wiki").mkdir(parents=True)
    (home / "state/wiki/paper.md").write_text("original research\n")
    _git("add", "-f", "state/wiki/paper.md", cwd=home)
    _git("commit", "-qm", "seed wiki", cwd=home)
    _git("push", "-q", cwd=home)
    return home


@pytest.fixture
def poller() -> PollerProposalScope:
    return PollerProposalScope("poller:research_feed-v2", "turn/123", "https://paper.test/42", "feed:item:9")


@pytest.mark.parametrize("owner", ["research", "poller:", "poller:../x", "poller:a/b", "poller:a.b", "poller:A", "poller:a b", "poller:a\n"])
def test_poller_owner_strict(owner: str) -> None:
    with pytest.raises(ValueError):
        PollerProposalScope(owner, "turn", "paper", "feed")


def test_poller_scope_frozen_and_collision_resistant(poller: PollerProposalScope, tmp_path: Path) -> None:
    from dataclasses import FrozenInstanceError, replace

    with pytest.raises(FrozenInstanceError):
        poller.owner = "poller:other"
    branch = poller_branch_name(poller)
    assert branch.startswith("poller/research_feed-v2/turn-123-")
    assert branch == poller_branch_name(replace(poller, source="other"))
    assert branch != poller_branch_name(replace(poller, turn_id="turn-123"))
    assert branch != poller_branch_name(replace(poller, owner="poller:other"))
    unsafe = replace(poller, turn_id="../../escape\n" + "x" * 1000)
    path = poller_worktree_path(tmp_path, unsafe)
    assert path.is_relative_to(tmp_path / "scratch/proposals/poller/research_feed-v2")
    assert len(path.name) < 100
    assert poller_worktree_path(tmp_path, poller) == tmp_path / "scratch/proposals" / branch


@pytest.mark.parametrize("operation", [open_proposal, finalize_proposal, abandon_proposal, list_open_proposals])
def test_poller_lane_requires_scope_and_rejects_other_lane(home: Path, poller: PollerProposalScope, operation) -> None:
    kwargs = {"title": "t", "rationale": "r"} if operation is finalize_proposal else {}
    with pytest.raises(ValueError, match="requires poller"):
        operation(home, lane="poller", **kwargs)
    for lane in ("agent", "upgrade"):
        with pytest.raises(ValueError, match="another lane"):
            operation(home, lane=lane, poller=poller, **kwargs)


@pytest.mark.parametrize("operation", [open_proposal, finalize_proposal, abandon_proposal])
def test_poller_rejects_branch_override(home: Path, poller: PollerProposalScope, operation) -> None:
    kwargs = {"title": "t", "rationale": "r"} if operation is finalize_proposal else {}
    with pytest.raises(ValueError, match="exact poller scope"):
        operation(home, lane="poller", poller=poller, branch="poller/other/turn", **kwargs)


def test_poller_exact_scope_isolation(wiki_home: Path, poller: PollerProposalScope) -> None:
    from dataclasses import replace

    scopes = [poller, replace(poller, turn_id="turn-123"), replace(poller, owner="poller:other")]
    opened = [open_proposal(wiki_home, lane="poller", poller=s) for s in scopes]
    assert all(r.ok for r in opened)
    assert open_proposal(wiki_home, lane="poller", poller=poller).reason == "exists"
    agent = open_proposal(wiki_home)
    assert agent.ok
    assert len(list_open_proposals(wiki_home)) == 4
    for scope, result in zip(scopes, opened):
        assert list_open_proposals(wiki_home, poller=scope) == [(result.branch, result.worktree)]
        assert result.worktree == poller_worktree_path(wiki_home, scope)
        assert not (result.worktree / "memory/core").exists()
        assert not (result.worktree / "prompts").exists()
    absent = replace(poller, turn_id="absent")
    assert not abandon_proposal(wiki_home, lane="poller", poller=absent)
    assert finalize_proposal(wiki_home, lane="poller", poller=absent, title="t", rationale="r").reason == "no_open"
    assert abandon_proposal(wiki_home, lane="poller", poller=poller)
    assert len(list_open_proposals(wiki_home)) == 3


def test_poller_submit_attribution_and_live_wiki_untouched(wiki_home: Path, poller: PollerProposalScope) -> None:
    from dataclasses import replace

    token = "ghp_" + "A" * 36
    poller = replace(poller, source=poller.source + " " + token)
    r = open_proposal(wiki_home, lane="poller", poller=poller, branch=poller_branch_name(poller))
    assert r.ok
    (r.worktree / "state/wiki/paper.md").write_text("new research\n")
    (r.worktree / "state/wiki/new.md").write_text("new page\n")
    calls = []
    result = finalize_proposal(wiki_home, lane="poller", poller=poller, title="findings", rationale="model claim", open_pr=_opener(calls))
    assert result.ok and result.pushed
    assert calls[0]["title"].startswith("[research poller:research_feed-v2] https://paper.test/42")
    body = calls[0]["body"]
    assert "Untrusted-ingest source (not verified):" in body
    assert "Trusted origin_ref: feed:item:9" in body
    assert "Turn: turn/123" in body
    assert token not in str(calls)
    assert token not in _git("log", "-1", "--format=%B", f"origin/{result.branch}", cwd=wiki_home).stdout
    assert (wiki_home / "state/wiki/paper.md").read_text() == "original research\n"
    assert not (wiki_home / "state/wiki/new.md").exists()
    paths = _git("diff", "--name-only", "main", f"origin/{result.branch}", cwd=wiki_home).stdout.splitlines()
    assert paths == ["state/wiki/new.md", "state/wiki/paper.md"]
    assert _git("status", "--porcelain", cwd=wiki_home).stdout == ""
    assert not r.worktree.exists()


@pytest.mark.parametrize("kind", ["tracked", "staged", "untracked", "ignored", "rename_out", "rename_in", "copy_out", "newline"])
def test_poller_rejects_all_outside_surface_changes(wiki_home: Path, poller: PollerProposalScope, kind: str) -> None:
    r = open_proposal(wiki_home, lane="poller", poller=poller)
    wt = r.worktree
    (wt / "state/wiki/paper.md").write_text("valid research\n")
    outside = "prompts/reflect.md"
    if kind in ("tracked", "staged", "rename_in"):
        _git("sparse-checkout", "disable", cwd=wt)
    if kind in ("tracked", "staged"):
        (wt / outside).write_text("outside\n")
        if kind == "staged":
            _git("add", outside, cwd=wt)
    elif kind == "rename_in":
        _git("mv", outside, "state/wiki/import.md", cwd=wt)
    elif kind in ("rename_out", "copy_out"):
        outside = "outside.md"
        if kind == "rename_out":
            _git("mv", "state/wiki/paper.md", outside, cwd=wt)
        else:
            shutil.copy(wt / "state/wiki/paper.md", wt / outside)
            _git("add", "-f", outside, cwd=wt)
    else:
        outside = "scratch/ignored.txt" if kind == "ignored" else "outside\nname.md" if kind == "newline" else "prompts/new.md"
        (wt / outside).parent.mkdir(parents=True, exist_ok=True)
        (wt / outside).write_text("outside\n")
        if kind == "ignored":
            assert _git("check-ignore", outside, cwd=wt).returncode == 0
    calls = []
    result = finalize_proposal(wiki_home, lane="poller", poller=poller, title="t", rationale="r", open_pr=_opener(calls))
    assert not result.ok and not result.pushed and result.reason == "outside_surface"
    assert outside in result.detail
    assert not calls and wt.exists()
    assert not _git("ls-remote", "--heads", "origin", r.branch, cwd=wiki_home).stdout


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("target", ["../../prompts/reflect.md", "/etc/passwd", "paper.md"])
def test_poller_symlink_surface(wiki_home: Path, poller: PollerProposalScope, staged: bool, target: str) -> None:
    r = open_proposal(wiki_home, lane="poller", poller=poller)
    link = r.worktree / "state/wiki/link.md"
    link.symlink_to(target)
    if staged:
        _git("add", "state/wiki/link.md", cwd=r.worktree)
        # Index inspection must not be fooled by a safe replacement on disk.
        link.unlink()
        link.write_text("safe replacement\n")
    result = finalize_proposal(wiki_home, lane="poller", poller=poller, title="t", rationale="r", open_pr=_opener([]))
    if target == "paper.md":
        assert result.ok
    else:
        assert result.reason == "outside_surface" and not result.pushed


@pytest.mark.parametrize("content,reason", [("ghp_" + "A" * 36, "secret"), ("<<<<<<< unresolved\n", "conflict_marker")])
def test_poller_shared_content_guards(wiki_home: Path, poller: PollerProposalScope, content: str, reason: str) -> None:
    r = open_proposal(wiki_home, lane="poller", poller=poller)
    (r.worktree / "state/wiki/paper.md").write_text(content)
    result = finalize_proposal(wiki_home, lane="poller", poller=poller, title="t", rationale="r", open_pr=_opener([]))
    assert result.reason == reason and not result.pushed


@pytest.mark.parametrize("command", ["diff", "ls-files", "cat-file", "add"])
def test_poller_git_failure_fails_closed(wiki_home: Path, poller: PollerProposalScope, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    import mimir.proposals as proposals

    r = open_proposal(wiki_home, lane="poller", poller=poller)
    (r.worktree / "state/wiki/link.md").symlink_to("paper.md")
    _git("add", "state/wiki/link.md", cwd=r.worktree)
    real_git = proposals._git

    def fail(args, cwd):
        if args[0] == command:
            return subprocess.CompletedProcess(args, 1, "", "failed")
        return real_git(args, cwd)

    monkeypatch.setattr(proposals, "_git", fail)
    result = finalize_proposal(wiki_home, lane="poller", poller=poller, title="t", rationale="r", open_pr=lambda *a: pytest.fail("must not open PR"))
    assert result.reason == "error" and not result.pushed


def test_poller_staged_symlink_chain_rejected(wiki_home: Path, poller: PollerProposalScope) -> None:
    r = open_proposal(wiki_home, lane="poller", poller=poller)
    wiki = r.worktree / "state/wiki"
    (wiki / "first").symlink_to("second/file")
    (wiki / "second").symlink_to("../../prompts")
    _git("add", "state/wiki", cwd=r.worktree)
    (wiki / "second").unlink()
    (wiki / "second").mkdir()
    (wiki / "second/file").write_text("safe disk replacement")
    result = finalize_proposal(wiki_home, lane="poller", poller=poller, title="t", rationale="r")
    assert result.reason == "outside_surface" and not result.pushed


def test_poller_list_requires_exact_worktree_path(wiki_home: Path, poller: PollerProposalScope) -> None:
    r = open_proposal(wiki_home, lane="poller", poller=poller)
    moved = r.worktree.parent / "wrong-turn"
    _git("worktree", "move", str(r.worktree), str(moved), cwd=wiki_home)
    assert list_open_proposals(wiki_home, poller=poller) == []
    assert list_open_proposals(wiki_home) == [(r.branch, moved)]
    assert not abandon_proposal(wiki_home, lane="poller", poller=poller)


def test_poller_worktree_list_failure_fails_closed(home: Path, poller: PollerProposalScope, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mimir.proposals._git", lambda args, cwd: subprocess.CompletedProcess(args, 1, "", "failure"))
    with pytest.raises(RuntimeError, match="worktree list failed"):
        finalize_proposal(home, lane="poller", poller=poller, title="t", rationale="r")


def test_poller_list_requires_exact_branch_at_correct_path(wiki_home: Path, poller: PollerProposalScope) -> None:
    r = open_proposal(wiki_home, lane="poller", poller=poller)
    _git("branch", "-m", "poller/other/turn", cwd=r.worktree)
    assert list_open_proposals(wiki_home, poller=poller) == []
    assert not abandon_proposal(wiki_home, lane="poller", poller=poller)
    assert finalize_proposal(wiki_home, lane="poller", poller=poller, title="t", rationale="r").reason == "no_open"
    assert r.worktree.exists()


@pytest.mark.parametrize("field", ["turn_id", "source", "origin_ref"])
@pytest.mark.parametrize("value", ["", " \n", None])
def test_poller_scope_requires_attribution_fields(poller: PollerProposalScope, field: str, value) -> None:
    from dataclasses import replace

    with pytest.raises(ValueError):
        replace(poller, **{field: value})


@pytest.mark.parametrize("kind", ["gitlink", "unmerged", "cycle", "directory_cycle", "disk_escape"])
def test_poller_special_index_entries(wiki_home: Path, poller: PollerProposalScope, kind: str) -> None:
    import mimir.proposals as proposals

    r = open_proposal(wiki_home, lane="poller", poller=poller)
    wt = r.worktree
    path = "state/wiki/special"
    if kind == "gitlink":
        oid = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        _git("update-index", "--add", "--cacheinfo", f"160000,{oid},{path}", cwd=wt)
    elif kind == "unmerged":
        oid = _git("rev-parse", "HEAD:state/wiki/paper.md", cwd=wt).stdout.strip()
        subprocess.run(["git", "update-index", "--index-info"], cwd=wt,
                       input=f"100644 {oid} 1\t{path}\n100644 {oid} 2\t{path}\n",
                       text=True, check=True, capture_output=True)
    elif kind in ("cycle", "directory_cycle"):
        (wt / path).symlink_to("special" if kind == "cycle" else "directory/file")
        if kind == "directory_cycle":
            (wt / "state/wiki/directory").symlink_to("special")
        _git("add", "state/wiki", cwd=wt)
        # Only the index contains the cycle; disk traversal cannot detect it.
        (wt / path).unlink()
        (wt / path).write_text("safe disk replacement")
        if kind == "directory_cycle":
            (wt / "state/wiki/directory").unlink()
            (wt / "state/wiki/directory").write_text("safe disk replacement")
    else:
        (wt / path).symlink_to("directory/file")
        _git("add", path, cwd=wt)
        (wt / "state/wiki/directory").symlink_to(wiki_home)
    # Probe the real index before git add can remove a gitlink or resolve a conflict.
    # Disk escape is a pre-stage race boundary; no Git output is fabricated here.
    reason = proposals._check_poller_surface(wt)
    assert reason and "outside_surface" in reason
    if kind in ("gitlink", "unmerged"):
        assert "unsupported index entry" in reason
    elif kind in ("cycle", "directory_cycle"):
        assert "cyclic symlink" in reason


@pytest.mark.parametrize("step", ["staged_names", "conflict_names", "content_diff", "post_surface", "surface_exception", "post_exception"])
def test_poller_late_git_failure_fails_closed(wiki_home: Path, poller: PollerProposalScope, monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    import mimir.proposals as proposals

    r = open_proposal(wiki_home, lane="poller", poller=poller)
    (r.worktree / "state/wiki/paper.md").write_text("new research\n")
    real_git = proposals._git
    names = 0
    surface = 0

    def fail(args, cwd):
        nonlocal names, surface
        if args == ["diff", "--cached", "--name-only"]:
            names += 1
            if (step == "staged_names" and names == 1) or (step == "conflict_names" and names == 2):
                return subprocess.CompletedProcess(args, 1, "state/wiki/paper.md\n", "failed")
        if step == "content_diff" and args == ["diff", "--cached", "-U0"]:
            return subprocess.CompletedProcess(args, 1, "", "failed")
        if args == ["diff", "--cached", "--no-renames", "--name-only", "-z"]:
            surface += 1
            if step == "post_surface" and surface == 2:
                return subprocess.CompletedProcess(args, 1, "", "failed")
            if (step == "surface_exception" and surface == 1) or (step == "post_exception" and surface == 2):
                raise OSError("failed")
        return real_git(args, cwd)

    monkeypatch.setattr(proposals, "_git", fail)
    calls = []
    result = finalize_proposal(wiki_home, lane="poller", poller=poller, title="t", rationale="r", open_pr=_opener(calls))
    assert result.reason == "error" and not result.pushed
    assert not calls


def test_poller_symlink_lexical_escape_even_when_disk_resolves_inside(wiki_home: Path, poller: PollerProposalScope) -> None:
    # A tracked root-level link is materialized by cone checkout. It is unchanged,
    # but reaching wiki through it must not legitimize an out-of-surface target.
    (wiki_home / "alias").symlink_to("state/wiki")
    _git("add", "-f", "alias", cwd=wiki_home)
    _git("commit", "-qm", "seed alias", cwd=wiki_home)
    _git("push", "-q", cwd=wiki_home)
    r = open_proposal(wiki_home, lane="poller", poller=poller)
    link = r.worktree / "state/wiki/escape"
    link.symlink_to("../../alias/paper.md")
    assert link.resolve() == r.worktree / "state/wiki/paper.md"
    result = finalize_proposal(wiki_home, lane="poller", poller=poller, title="t", rationale="r", open_pr=_opener([]))
    assert result.reason == "outside_surface" and not result.pushed


def test_poller_surface_requires_directory_boundary(wiki_home: Path, poller: PollerProposalScope) -> None:
    r = open_proposal(wiki_home, lane="poller", poller=poller)
    (r.worktree / "state/wiki/paper.md").write_text("valid research\n")
    outside = r.worktree / "state/wiki-escape/note.md"
    outside.parent.mkdir()
    outside.write_text("outside\n")
    result = finalize_proposal(wiki_home, lane="poller", poller=poller, title="t", rationale="r", open_pr=_opener([]))
    assert result.reason == "outside_surface" and not result.pushed
