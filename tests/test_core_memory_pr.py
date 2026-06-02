"""Tests for ``mimir.core_memory_pr`` — the sandbox proposal workflow (#337/#339).

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
from mimir.core_memory_pr import (
    abandon_proposal,
    default_branch_name,
    finalize_proposal,
    list_open_proposals,
    open_proposal,
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
    # A tracked NON-core file, to prove the sparse checkout excludes it.
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
    assert "scratch/core-proposals" in str(r.worktree.relative_to(home))
    # It's a real checkout: the seeded core file is present to edit.
    assert (r.worktree / "memory" / "core" / "40-learned-behaviors.md").read_text() == SEED
    # Sparse: only memory/core is materialized, not other tracked subtrees.
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


def test_open_one_at_a_time(home: Path) -> None:
    r1 = open_proposal(home)
    assert r1.ok
    r2 = open_proposal(home)
    assert not r2.ok and r2.reason == "exists" and r2.branch == r1.branch


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


def test_finalize_stages_only_core(home: Path) -> None:
    r = open_proposal(home)
    assert r.ok
    (r.worktree / "memory" / "core" / "40-learned-behaviors.md").write_text(
        SEED + "- core change\n", encoding="utf-8"
    )
    # Also touch a tracked non-core file in the worktree; it must NOT reach the PR.
    (r.worktree / "state").mkdir(exist_ok=True)
    (r.worktree / "state" / "proposed-changes.md").write_text("stray\n", encoding="utf-8")
    res = finalize_proposal(home, title="t", rationale="r", open_pr=_opener([]))
    assert res.ok
    files = _git("show", "--name-only", "--format=", f"origin/{res.branch}", cwd=home).stdout
    assert "memory/core/40-learned-behaviors.md" in files
    assert "proposed-changes" not in files


def test_finalize_pushes_without_pr_when_opener_returns_none(home: Path) -> None:
    r = open_proposal(home)
    assert r.ok
    (r.worktree / "memory" / "core" / "40-learned-behaviors.md").write_text(
        SEED + "- x\n", encoding="utf-8"
    )
    res = finalize_proposal(home, title="t", rationale="r", open_pr=lambda *a: None)
    assert res.ok and res.pushed and res.pr_url is None and res.reason == "pushed_no_pr"


# ─── abandon ─────────────────────────────────────────────────────────


def test_abandon(home: Path) -> None:
    r = open_proposal(home)
    assert r.ok
    assert abandon_proposal(home) is True
    assert list_open_proposals(home) == []
    assert not r.worktree.exists()
    assert abandon_proposal(home) is False  # nothing open now


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
    assert default_branch_name("Add a Rule!", ts=5) == "core-memory/add-a-rule-5"
    assert default_branch_name(ts=9) == "core-memory/proposal-9"
