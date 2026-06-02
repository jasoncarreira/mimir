"""Open & finalize change proposals for protected files as PRs (chainlink #337/#339/#344).

The agent cannot write live ``memory/core/*`` or ``prompts/*`` (the write
guard blocks core memory at runtime; prompts aren't a writable dir). To change
either, it opens a *proposal*: a throwaway ``git worktree`` of the home repo,
checked out under the gitignored ``scratch/`` workspace, where it edits those
files with its normal file tools — add, edit, delete, move, any number of
files across both surfaces. Submitting commits the changes (secret-scanned,
credential-redacted), pushes the branch, and opens one PR. The operator
reviews the diff and merges; live files update only after the merge (the
per-turn ``git pull --rebase`` apply path, #340). **Merge is the approval.**

The proposable surfaces are :data:`PROPOSAL_SURFACES` (``memory/core`` and
``prompts``) — both git-tracked in the home repo, both protected from live
agent writes.

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

#: Protected surfaces a proposal can change, relative to the home / repo root.
#: Both are git-tracked and blocked from live agent writes (memory/core via the
#: runtime read-only gate, prompts by not being a writable dir).
PROPOSAL_SURFACES: tuple[Path, ...] = (Path("memory") / "core", Path("prompts"))
#: Where proposal worktrees live — under the gitignored scratch workspace.
PROPOSALS_REL = Path("scratch") / "proposals"

#: Named proposal lanes. The agent lane is the existing manual/operator-facing
#: proposal flow; the upgrade lane is reserved for version-triggered default syncs.
AGENT_PROPOSAL_LANE = "agent"
UPGRADE_PROPOSAL_LANE = "upgrade"
PROPOSAL_LANES = (AGENT_PROPOSAL_LANE, UPGRADE_PROPOSAL_LANE)

#: ``(home, branch, base, title, body) -> pr_url | None`` — injectable so tests
#: exercise the git mechanics without a real GitHub.
PrOpener = Callable[[Path, str, str, str, str], "str | None"]


def normalize_lane(lane: str | None) -> str:
    """Return a supported proposal lane name, raising ``ValueError`` if invalid."""
    value = (lane or AGENT_PROPOSAL_LANE).strip().lower()
    if value not in PROPOSAL_LANES:
        allowed = ", ".join(PROPOSAL_LANES)
        raise ValueError(f"unsupported proposal lane {lane!r}; expected one of: {allowed}")
    return value


def default_branch_name(
    label: str = "proposal", *, ts: int | None = None, lane: str = AGENT_PROPOSAL_LANE
) -> str:
    """Derive a unique proposal branch for ``lane``.

    Agent-lane branches keep the historical ``proposal/<slug>-<unix-ts>`` shape;
    upgrade-lane branches use ``upgrade/<slug>-<unix-ts>`` so the lane is visible
    in GitHub and never collides with manual proposals. ``ts`` is injectable for
    deterministic tests; production callers leave it None to stamp with the
    current time.
    """
    lane = normalize_lane(lane)
    prefix = "upgrade" if lane == UPGRADE_PROPOSAL_LANE else "proposal"
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40].rstrip("-")
    if slug == "proposal" and lane != AGENT_PROPOSAL_LANE:
        slug = prefix
    stamp = ts if ts is not None else int(time.time())
    return f"{prefix}/{slug or prefix}-{stamp}"


@dataclass
class OpenResult:
    """Outcome of opening a proposal. On success ``worktree`` is the dir the
    agent edits (its ``memory/core/`` and ``prompts/`` hold the proposable
    files)."""

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


def _proposals_dir(home: Path, *, lane: str | None = None) -> Path:
    root = (home / PROPOSALS_REL).resolve()
    if lane is None:
        return root
    return root / normalize_lane(lane)


def _lane_for_worktree(home: Path, worktree: Path) -> str | None:
    """Infer a proposal lane from a worktree path under ``scratch/proposals``.

    Pre-lane proposal worktrees lived directly under ``scratch/proposals/``;
    treat those as the agent lane so an in-flight proposal survives an upgrade.
    """
    root = _proposals_dir(home)
    try:
        rel = worktree.relative_to(root)
    except ValueError:
        return None
    if not rel.parts:
        return None
    candidate = rel.parts[0]
    return candidate if candidate in PROPOSAL_LANES else AGENT_PROPOSAL_LANE


def _worktree_dir(home: Path, branch: str, *, lane: str = AGENT_PROPOSAL_LANE) -> Path:
    # "/" isn't legal in a dir name; the branch is recovered from git, not the
    # dir name, so the sanitized form is just for a readable path.
    return _proposals_dir(home, lane=lane) / branch.replace("/", "_")


def list_open_proposals(home: Path, *, lane: str | None = None) -> list[tuple[str, Path]]:
    """``(branch, worktree_path)`` for each open proposal worktree.

    With ``lane`` omitted, returns proposals from all supported lanes under
    ``scratch/proposals/<lane>/``. With ``lane`` set, filters to that lane. Parsed
    from ``git worktree list`` so crash-orphaned paths disappear after prune.
    """
    home = Path(home).resolve()
    lane = normalize_lane(lane) if lane is not None else None
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
                inferred_lane = _lane_for_worktree(home, cur_path)
                if inferred_lane and (lane is None or inferred_lane == lane):
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
                "\n# Change-proposal worktrees live under "
                "scratch/proposals/; ignore scratch/ explicitly so the\n"
                "# per-turn `git add -A` doesn't grab them as embedded repos "
                "(chainlink #299/#339).\nscratch/\n"
            )
    except OSError as exc:
        return f"could not update .gitignore: {exc}"
    return None


def open_proposal(
    home: Path,
    *,
    base: str = "main",
    branch: str | None = None,
    lane: str = AGENT_PROPOSAL_LANE,
) -> OpenResult:
    """Open a change proposal in ``lane``.

    Each lane permits one open proposal at a time. Agent-lane worktrees live
    under ``scratch/proposals/agent/``; upgrade-lane worktrees live under
    ``scratch/proposals/upgrade/``.
    """
    home = Path(home).resolve()
    lane = normalize_lane(lane)
    if not _has_origin_remote(home):
        return OpenResult(
            ok=False, branch=None, worktree=None, reason="no_remote",
            detail=(
                "no origin remote — core memory is seeded at setup (scaffold), "
                "not via PR, until a remote exists"
            ),
        )
    _git(["worktree", "prune"], cwd=home)  # clear crash-orphaned worktrees
    existing = list_open_proposals(home, lane=lane)
    if existing:
        b, w = existing[0]
        return OpenResult(
            ok=False, branch=b, worktree=w, reason="exists",
            detail=(
                f"a {lane} proposal is already open ({b}); submit or "
                f"abandon it before opening another {lane} proposal"
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
    branch = branch or default_branch_name(lane=lane)
    wt = _worktree_dir(home, branch, lane=lane)
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
    # Sparse-checkout just the proposable surfaces so the worktree stays small
    # and the agent only sees what it can change (submit stages them regardless).
    # Cone mode also materializes top-level files, which is harmless.
    surfaces = [s.as_posix() for s in PROPOSAL_SURFACES]
    for step in (["sparse-checkout", "set", "--cone", *surfaces], ["checkout"]):
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
    lane: str = AGENT_PROPOSAL_LANE,
    open_pr: PrOpener | None = None,
) -> ProposalResult:
    """Commit the open proposal's changes (memory/core + prompts), push, and PR.

    Stages **only** the proposable surfaces (so stray edits elsewhere in the
    worktree never reach the PR), scans the staged diff for secrets, commits
    with a redacted message, pushes, opens the PR, and tears down the worktree.
    On a recoverable miss (no changes, secret found) the worktree is left
    intact so the agent can fix and resubmit.
    """
    home = Path(home).resolve()
    lane = normalize_lane(lane)
    opens = list_open_proposals(home, lane=lane)
    if not opens:
        return ProposalResult(
            ok=False, branch=None, pushed=False, pr_url=None, reason="no_open",
            detail=f"no open {lane} proposal — open one first",
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

    _git(["add", *[s.as_posix() for s in PROPOSAL_SURFACES]], cwd=wt)
    staged = _git(["diff", "--cached", "--name-only"], cwd=wt)
    if not (staged.stdout or "").strip():
        return ProposalResult(
            ok=False, branch=branch, pushed=False, pr_url=None, reason="no_changes",
            detail="no changes under memory/core/ or prompts/ to propose",
        )

    diff = _git(["diff", "--cached", "-U0"], cwd=wt)
    added = "\n".join(
        ln for ln in (diff.stdout or "").splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    )
    if _scan_for_secrets(added):
        return ProposalResult(
            ok=False, branch=branch, pushed=False, pr_url=None, reason="secret",
            detail="proposed content contains a secret-shaped token — remove it; proposed files must not hold credentials",
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
        f"Proposal lane: `{lane}`.\n"
        "Proposed by the mimir change-proposal workflow (chainlink #337/#344/#348). "
        "Approval = merge; live files update after the merge (#340)."
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


def abandon_proposal(
    home: Path, *, branch: str | None = None, lane: str = AGENT_PROPOSAL_LANE
) -> bool:
    """Discard an open proposal in ``lane`` (remove its worktree + local branch).

    Returns True if one was found and removed, False if there was nothing open.
    """
    home = Path(home).resolve()
    lane = normalize_lane(lane)
    opens = list_open_proposals(home, lane=lane)
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


def render_open_proposals_block(home: Path) -> str | None:
    """Prompt nudge for any open change proposal(s), or None if none.

    Surfaced near the feedback block every turn so the agent doesn't leave a
    proposal dangling (#337/#339). Auto-clears the moment the worktree is gone
    (submitted or abandoned) — which is why the nudge is driven off live state
    rather than a per-turn event.
    """
    opens = list_open_proposals(home)
    if not opens:
        return None
    home = Path(home).resolve()
    lines: list[str] = []
    for branch, worktree in opens:
        try:
            rel = worktree.relative_to(home)
        except ValueError:
            rel = worktree
        lane = _lane_for_worktree(home, worktree) or "unknown"
        args = "title, rationale" if lane == AGENT_PROPOSAL_LANE else f"title, rationale, lane='{lane}'"
        abandon = "abandon_proposal" if lane == AGENT_PROPOSAL_LANE else f"abandon_proposal(lane='{lane}')"
        lines.append(
            f"- `{branch}` (lane `{lane}`): edit the files under `{rel}/memory/core/` or "
            f"`{rel}/prompts/`, then `submit_proposal({args})` to open "
            f"the PR — or `{abandon}` to discard."
        )
    return (
        "You have an open change proposal in progress — don't leave it "
        "hanging:\n" + "\n".join(lines)
    )


__all__ = (
    "OpenResult",
    "render_open_proposals_block",
    "ProposalResult",
    "open_proposal",
    "finalize_proposal",
    "abandon_proposal",
    "list_open_proposals",
    "default_branch_name",
    "normalize_lane",
    "PROPOSAL_SURFACES",
    "PROPOSALS_REL",
    "AGENT_PROPOSAL_LANE",
    "UPGRADE_PROPOSAL_LANE",
    "PROPOSAL_LANES",
)
