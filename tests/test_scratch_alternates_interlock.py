"""A reclaimed object store must not be able to break the base repository.

The 2026-07-28 incident, end to end: ``<home>/scratch/pr1188-object-db`` was an
``objects/info/alternates`` target of ``/workspace/mimir``. The janitor reclaimed
it at the 1-day TTL. ``git fetch`` then failed with
``fatal: bad object refs/remotes/origin/pr/1188`` — the ref resolved only through
the reclaimed store — so every worklink base fetch failed, six attempts across
three leaves were consumed, and all three leaves were auto-demoted to
``worklink:blocked``. The demotions looked like bad leaves.

Two independent defects, so two tests:

* the janitor should never have reclaimed a directory a repository was standing on;
* the fetch repair handled a dangling *alternate* but not the dangling *refs* left
  behind once the alternate entry was gone, so it could not recover.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mimir.scratch_janitor import sweep_scratch_roots


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60,
    )


def _repo_with_alternate(tmp_path: Path, alternate_objects: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", ".", cwd=repo)
    info = repo / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(f"{alternate_objects}\n", encoding="utf-8")
    return repo


def test_janitor_refuses_to_reclaim_a_borrowed_object_store(tmp_path, monkeypatch):
    """An aged scratch entry that a repo borrows from is kept, not swept."""
    home = tmp_path / "home"
    scratch = home / "scratch"
    borrowed = scratch / "pr1188-object-db" / "objects"
    borrowed.mkdir(parents=True)
    (borrowed / "info").mkdir()
    unrelated = scratch / "just-junk"
    unrelated.mkdir(parents=True)
    (unrelated / "f.txt").write_text("x", encoding="utf-8")

    repo = _repo_with_alternate(tmp_path, borrowed)
    monkeypatch.setenv("MIMIR_SOURCE_DIR", str(repo))
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)

    # Age everything well past the TTL so only the interlock can save it.
    result = sweep_scratch_roots(home, ttl_days=1, roots=("scratch",), now=1e12)

    assert "scratch/pr1188-object-db" in result.protected, (
        f"the borrowed object store was not protected: {result}"
    )
    assert borrowed.is_dir(), "the janitor deleted a store the repo depends on"
    assert "scratch/just-junk" in result.removed, (
        "the interlock must not stop ordinary reclamation — scratch exists to be "
        f"swept: {result}"
    )


def test_base_fetch_repairs_refs_left_dangling_by_a_reclaimed_alternate(tmp_path):
    """The fetch path recovers from the residue, not just the alternate itself."""
    from mimir.worklink.worktree import _dangling_refs, _prune_dangling_refs

    def runner(argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)

    repo = tmp_path / "base"
    repo.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "c1", cwd=repo)

    # A ref naming an object this repo does not have — exactly what survives when
    # the alternate that held it is reclaimed and its entry already pruned.
    missing = "6795361c8816152361df477dbaf777910c50bc3a"
    (repo / ".git" / "refs" / "remotes" / "origin" / "pr").mkdir(parents=True)
    (repo / ".git" / "refs" / "remotes" / "origin" / "pr" / "1188").write_text(
        missing + "\n", encoding="utf-8",
    )

    found = _dangling_refs(repo, runner=runner)
    assert any(name.endswith("origin/pr/1188") for name, _ in found), (
        f"the dangling ref was not detected: {found}"
    )

    pruned = _prune_dangling_refs(repo, runner=runner)
    assert any(name.endswith("origin/pr/1188") for name, _ in pruned)
    assert _dangling_refs(repo, runner=runner) == [], "residue remained after repair"

    # History is untouched: only refs are removed, never objects.
    log = _git("log", "--oneline", cwd=repo)
    assert log.returncode == 0 and "c1" in log.stdout
