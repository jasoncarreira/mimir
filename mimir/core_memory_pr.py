"""Open & finalize core-memory change proposals as PRs (chainlink #337/#339).

The agent cannot write live ``memory/core/*`` (the write guard blocks it).
To change core memory it opens a *proposal*: a throwaway ``git worktree`` of
the home repo, checked out under the gitignored ``scratch/`` workspace, where
it edits the core files with its normal file tools — add, edit, delete, move,
any number of files. Submitting commits those changes (secret-scanned,
credential-redacted), pushes the branch, and opens a PR. The operator reviews
the diff and merges; live core memory updates only after the merge (the
per-turn ``git pull --rebase`` apply path, #340). **Merge is the approval.**

Why ``scratch/``: it's already a writable root AND gitignored (config.py,
chainlink #299) — so the agent's Read/Edit/Write reach the worktree, the core
write-guard doesn't fire (the path isn't ``home/memory/core``), and
``git_tracking``'s per-turn ``git add -A`` skips it. ``scratch/`` is only
*implicitly* ignored on older homes via the allowlist's ``*``; because ``!*/``
re-includes directories, a worktree (an embedded repo) there can still be
grabbed by ``git add -A`` (the chainlink #299 breakage). :func:`open_proposal`
self-heals that by ensuring ``scratch/`` is explicitly ignored, and refuses if
it can't be — so the proposal flow never leaves an embedded-repo hazard.

Sync by design; async callers (the agent tools) wrap in ``asyncio.to_thread``.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Reuse the hardened helpers rather than reimplement: ``_redact`` is the shared
# token scrubber (git_tracking imports it too), ``_run`` the uniform 30s-timeout
# subprocess wrapper. One redactor is the point — core diffs must not leak creds.
from .git_bootstrap import _redact, _run

#: Core-memory directory, relative to the home / repo root.
CORE_REL = Path("memory") / "core"
#: Where proposal worktrees live — under the gitignored scratch workspace.
PROPOSALS_REL = Path("scratch") / "core-proposals"

#: ``(home, branch, base, title, body) -> pr_url | None`` — injectable so tests
#: exercise the git mechanics without a real GitHub.
PrOpener = Callable[[Path, str, str, str, str], "str | None"]


def default_branch_name(label: str = "proposal", *, ts: int | None = None) -> str:
    """Derive a unique proposal branch ``core-memory/<slug>-<unix-ts>``.

    ``ts`` is injectable for deterministic tests; production callers leave it
    None to stamp with the current time."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40].rstrip("-")
    stamp = ts if ts is not None else int(time.time())
    return f"core-memory/{slug or 'proposal'}-{stamp}"


@dataclass
class OpenResult:
    """Outcome of opening a proposal. On success ``worktree`` is the dir the
    agent edits (its ``memory/core/`` holds the core files)."""

    ok: bool
    branch: str | None
    worktree: Path | None
    #: None on success; else "no_remote" | "exists" | "error".
    reason: str | None = None
    detail: str | None = None


@dataclass
class ProposalResult:
    """Outcome of submitting (finalizing) a proposal."""

    ok: bool
    branch: str | None
    pushed: bool
    pr_url: str | None
    #: None on full success; else "no_open" | "no_changes" | "secret" |
    #: "pushed_no_pr" | "error".
    reason: str | None
    detail: str | None = None


def _git(args: list[str], cwd: Path):
    return _run(["git", *args], cwd=cwd, capture=True)


def _has_origin_remote(home: Path) -> bool:
    res = _git(["remote", "get-url", "origin"], cwd=home)
    return res.returncode == 0 and bool((res.stdout or "").strip())


def _scan_for_secrets(text: str) -> bool:
    """True if ``text`` contains a token-shaped secret (per the shared
    ``_redact`` patterns). A deployment-independent fast-fail before push; the
    pre-commit secret-scan hook is the authoritative commit-time backstop."""
    return bool(text) and _redact(text) != text


def _proposals_dir(home: Path) -> Path:
    return (home / PROPOSALS_REL).resolve()


def _worktree_dir(home: Path, branch: str) -> Path:
    # "/" isn't legal in a dir name; the branch is recovered from git, not the
    # dir name, so the sanitized form is just for a readable path.
    return _proposals_dir(home) / branch.replace("/", "_")


def list_open_proposals(home: Path) -> list[tuple[str, Path]]:
    """``(branch, worktree_path)`` for each open proposal worktree under
    ``scratch/core-proposals/``. Parsed from ``git worktree list``."""
    home = Path(home).resolve()
    pdir = _proposals_dir(home)
    res = _git(["worktree", "list", "--porcelain"], cwd=home)
    out: list[tuple[str, Path]] = []
    cur_path: Path | None = None
    for line in (res.stdout or "").splitlines():
        if line.startswith("worktree "):
            cur_path = Path(line[len("worktree "):]).resolve()
        elif line.startswith("branch ") and cur_path is not None:
            name = line[len("branch "):].strip().replace("refs/heads/", "", 1)
            if pdir == cur_path or pdir in cur_path.parents:
                out.append((name, cur_path))
            cur_path = None
        elif not line.strip():
            cur_path = None
    return out


def _ensure_scratch_ignored(home: Path) -> str | None:
    """Guarantee a worktree under ``scratch/`` is git-ignored (else the home's
    per-turn ``git add -A`` grabs it as an embedded repo — chainlink #299).

    NOTE: ``git check-ignore`` is NOT a reliable probe here. On the allowlist
    ``*`` + ``!*/`` style, check-ignore reports a *file* path under scratch/ as
    ignored (matched by ``*``) while ``!*/`` still re-includes the *directory*
    — so an embedded repo is grabbed regardless. The robust fix is an explicit
    ``scratch/`` directory ignore, which the empirical probe (chainlink #299)
    confirmed neutralizes the hazard. So: ensure such a line exists, appending
    it if absent. Returns an error string only if .gitignore can't be accessed.
    """
    gitignore = home / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    except OSError as exc:
        return f"could not read .gitignore: {exc}"
    if any(
        line.strip() in ("scratch/", "/scratch/", "scratch")
        for line in existing.splitlines()
    ):
        return None
    try:
        with gitignore.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(
                "\n# Core-memory proposal worktrees live under "
                "scratch/core-proposals/; ignore scratch/ explicitly so the\n"
                "# per-turn `git add -A` doesn't grab them as embedded repos "
                "(chainlink #299/#339).\nscratch/\n"
            )
    except OSError as exc:
        return f"could not update .gitignore: {exc}"
    return None


def open_proposal(home: Path, *, base: str = "main", branch: str | None = None) -> OpenResult:
    """Open a core-memory proposal: a worktree off ``origin/<base>`` under
    ``scratch/core-proposals/`` for the agent to edit. One proposal at a time."""
    home = Path(home).resolve()
    if not _has_origin_remote(home):
        return OpenResult(
            ok=False, branch=None, worktree=None, reason="no_remote",
            detail=(
                "no origin remote — core memory is seeded at setup (scaffold), "
                "not via PR, until a remote exists"
            ),
        )
    _git(["worktree", "prune"], cwd=home)  # clear crash-orphaned worktrees
    existing = list_open_proposals(home)
    if existing:
        b, w = existing[0]
        return OpenResult(
            ok=False, branch=b, worktree=w, reason="exists",
            detail=(
                f"a core-memory proposal is already open ({b}); submit or "
                f"abandon it before opening another"
            ),
        )
    ignore_err = _ensure_scratch_ignored(home)
    if ignore_err:
        return OpenResult(ok=False, branch=None, worktree=None, reason="error", detail=ignore_err)
    fetch = _git(["fetch", "origin", base], cwd=home)
    if fetch.returncode != 0:
        return OpenResult(
            ok=False, branch=None, worktree=None, reason="error",
            detail=_redact(f"git fetch origin {base} failed: {(fetch.stderr or '').strip()}"),
        )
    branch = branch or default_branch_name()
    wt = _worktree_dir(home, branch)
    wt.parent.mkdir(parents=True, exist_ok=True)
    add = _git(
        ["worktree", "add", "--no-checkout", "-b", branch, str(wt), f"origin/{base}"],
        cwd=home,
    )
    if add.returncode != 0:
        return OpenResult(
            ok=False, branch=branch, worktree=None, reason="error",
            detail=_redact(f"git worktree add failed: {(add.stderr or '').strip()}"),
        )
    # Sparse-checkout to just memory/core so the worktree stays tiny and the
    # agent only sees the core files (submit stages memory/core regardless).
    # Cone mode also materializes top-level files, which is harmless.
    for step in (["sparse-checkout", "set", "--cone", "memory/core"], ["checkout"]):
        r = _git(step, cwd=wt)
        if r.returncode != 0:
            _cleanup_worktree(home, wt, branch)
            return OpenResult(
                ok=False, branch=branch, worktree=None, reason="error",
                detail=_redact(f"git {' '.join(step)} failed: {(r.stderr or '').strip()}"),
            )
    return OpenResult(ok=True, branch=branch, worktree=wt, reason=None)


def _default_open_pr(home: Path, branch: str, base: str, title: str, body: str) -> str | None:
    """Open a PR via the ``gh`` CLI; return the URL. None (not an error) when
    ``gh`` is unavailable or fails — the branch is already pushed."""
    if shutil.which("gh") is None:
        return None
    res = _run(
        ["gh", "pr", "create", "--base", base, "--head", branch,
         "--title", title, "--body", body],
        cwd=home, capture=True,
    )
    if res.returncode != 0:
        return None
    lines = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
    return lines[-1].strip() if lines else None


def _cleanup_worktree(home: Path, worktree: Path, branch: str) -> None:
    """Detach the worktree and drop the local branch. The pushed commit, the
    remote branch, and any PR survive independently of the local ref."""
    _git(["worktree", "remove", "--force", str(worktree)], cwd=home)
    if Path(worktree).exists():
        shutil.rmtree(worktree, ignore_errors=True)
    _git(["worktree", "prune"], cwd=home)
    _git(["branch", "-D", branch], cwd=home)


def finalize_proposal(
    home: Path,
    *,
    title: str,
    rationale: str,
    base: str = "main",
    branch: str | None = None,
    open_pr: PrOpener | None = None,
) -> ProposalResult:
    """Commit the open proposal's ``memory/core/`` changes, push, and open a PR.

    Stages **only** ``memory/core/`` (so stray edits elsewhere in the worktree
    never reach the PR), scans the staged diff for secrets, commits with a
    redacted message, pushes, opens the PR, and tears down the worktree. On a
    recoverable miss (no changes, secret found) the worktree is left intact so
    the agent can fix and resubmit.
    """
    home = Path(home).resolve()
    opens = list_open_proposals(home)
    if not opens:
        return ProposalResult(
            ok=False, branch=None, pushed=False, pr_url=None, reason="no_open",
            detail="no open core-memory proposal — open one first",
        )
    if branch is not None:
        match = [(b, w) for b, w in opens if b == branch]
        if not match:
            return ProposalResult(
                ok=False, branch=branch, pushed=False, pr_url=None, reason="no_open",
                detail=f"no open proposal named {branch!r}",
            )
        branch, wt = match[0]
    else:
        branch, wt = opens[0]

    _git(["add", str(CORE_REL)], cwd=wt)
    staged = _git(["diff", "--cached", "--name-only"], cwd=wt)
    if not (staged.stdout or "").strip():
        return ProposalResult(
            ok=False, branch=branch, pushed=False, pr_url=None, reason="no_changes",
            detail="no changes under memory/core/ to propose",
        )

    diff = _git(["diff", "--cached", "-U0"], cwd=wt)
    added = "\n".join(
        ln for ln in (diff.stdout or "").splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    )
    if _scan_for_secrets(added):
        return ProposalResult(
            ok=False, branch=branch, pushed=False, pr_url=None, reason="secret",
            detail="proposed core content contains a secret-shaped token — remove it; core memory must not hold credentials",
        )

    safe_title = _redact(title)
    safe_rationale = _redact(rationale)
    commit = _git(["commit", "-m", f"{safe_title}\n\n{safe_rationale}"], cwd=wt)
    if commit.returncode != 0:
        return ProposalResult(
            ok=False, branch=branch, pushed=False, pr_url=None, reason="error",
            detail=_redact(f"git commit failed: {(commit.stderr or '').strip()}"),
        )
    push = _git(["push", "-u", "origin", branch], cwd=wt)
    if push.returncode != 0:
        return ProposalResult(
            ok=False, branch=branch, pushed=False, pr_url=None, reason="error",
            detail=_redact(f"git push failed: {(push.stderr or '').strip()}"),
        )

    body = (
        f"{safe_rationale}\n\n---\n"
        "Proposed by the mimir core-memory workflow (chainlink #337). "
        "Approval = merge; live core memory updates after the merge (#340)."
    )
    opener = open_pr or _default_open_pr
    pr_url = opener(home, branch, base, safe_title, body)
    _cleanup_worktree(home, wt, branch)
    if pr_url:
        return ProposalResult(ok=True, branch=branch, pushed=True, pr_url=pr_url, reason=None)
    return ProposalResult(
        ok=True, branch=branch, pushed=True, pr_url=None, reason="pushed_no_pr",
        detail="branch pushed; open the PR manually (gh unavailable or failed)",
    )


def abandon_proposal(home: Path, *, branch: str | None = None) -> bool:
    """Discard an open proposal (remove its worktree + local branch). Returns
    True if one was found and removed, False if there was nothing open."""
    home = Path(home).resolve()
    opens = list_open_proposals(home)
    if not opens:
        return False
    if branch is not None:
        match = [(b, w) for b, w in opens if b == branch]
        if not match:
            return False
        b, w = match[0]
    else:
        b, w = opens[0]
    _cleanup_worktree(home, w, b)
    return True


__all__ = (
    "OpenResult",
    "ProposalResult",
    "open_proposal",
    "finalize_proposal",
    "abandon_proposal",
    "list_open_proposals",
    "default_branch_name",
    "CORE_REL",
    "PROPOSALS_REL",
)
