#!/usr/bin/env python3
"""Clean up local feature branches whose PRs are squash-merged.

Mimir's workflow is squash-merge via GitHub, which doesn't leave the
local branch reachable from main via git's ancestry. ``git branch
--merged main`` only catches a small fraction of actually-landed
branches (the chainlink #136 audit found 4/57 = 7% on 2026-05-12); the
rest accumulate locally forever.

This script uses ``gh pr list --head <branch>`` to identify merged PRs
per branch and deletes the locals when safe. Four classifications:

- ``merged``: PR is MERGED on GitHub. Safe to delete (work is in main
  as a squash commit).
- ``open``: PR is OPEN. Keep — work in flight.
- ``closed_unmerged``: PR closed without merging. Keep — may need
  attention (manual cleanup or revival).
- ``no_pr``: no PR for this branch. Keep — local-only experiment.

Usage:
    python scripts/cleanup_merged_branches.py            # dry-run (default)
    python scripts/cleanup_merged_branches.py --apply     # delete safe candidates

Exit codes:
    0  success (dry-run printed, or apply completed cleanly)
    1  one or more ``git branch -D`` calls failed during ``--apply``
    2  fatal error (gh / git invocation, JSON parse)

See chainlink #136 for the full design discussion (including why no
purely-local primitive works for squash-merge — ``git cherry``,
``git diff --quiet``, and range-diff all fail on squash because the
combined-patch-ID differs from per-commit patch-IDs).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Callable, Iterable


# Branch classification labels. Module-level constants so tests can
# import them rather than hardcoding the strings.
MERGED = "merged"
OPEN = "open"
CLOSED_UNMERGED = "closed_unmerged"
NO_PR = "no_pr"


def _git(args: list[str]) -> str:
    """Run ``git`` with args, return stdout. Raises on non-zero exit."""
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _gh_pr_list_for_branch(branch: str) -> list[dict]:
    """Return the PRs whose head ref matches ``branch`` (any state)."""
    out = subprocess.run(
        [
            "gh", "pr", "list",
            "--head", branch,
            "--state", "all",
            "--json", "number,state,mergedAt",
            "--limit", "5",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)


def classify_branch(prs: list[dict]) -> tuple[str, int | None, int | None]:
    """Classify a branch by its PR set.

    Returns ``(status, pr_number, also_open_pr_number)`` where status is
    one of MERGED, OPEN, CLOSED_UNMERGED, NO_PR. The PR number is the
    chosen PR's number (None for NO_PR).

    ``also_open_pr_number`` is the number of an OPEN PR using this same
    head ref that was NOT chosen — meaningful only when ``status ==
    MERGED`` and a reused-branch reincarnation has an in-flight PR
    upstream. In that case the local branch is still safe to delete
    (the squash-merged work is in main), but the operator should know
    that pushing or recreating the branch name will collide with the
    in-flight PR. None when no such situation applies.

    Precedence when multiple PRs share a head ref (rare but happens
    when a branch is reused): MERGED > OPEN > CLOSED_UNMERGED. We pick
    the merged PR if one exists because "the work is in main" is the
    safety-determining fact, regardless of any later closed/reopened
    PR cycles.
    """
    if not prs:
        return (NO_PR, None, None)

    merged = [p for p in prs if p.get("state") == "MERGED"]
    open_ = [p for p in prs if p.get("state") == "OPEN"]
    if merged:
        also_open = open_[0]["number"] if open_ else None
        return (MERGED, merged[0]["number"], also_open)

    if open_:
        return (OPEN, open_[0]["number"], None)

    return (CLOSED_UNMERGED, prs[0]["number"], None)


def _list_local_branches(git_runner: Callable[[list[str]], str] = _git) -> list[str]:
    """Return local branch names (excluding the leading marker on the current branch)."""
    raw = git_runner(["branch", "--format=%(refname:short)"])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _current_branch(git_runner: Callable[[list[str]], str] = _git) -> str:
    return git_runner(["branch", "--show-current"]).strip()


def collect_branch_statuses(
    branches: Iterable[str],
    *,
    current_branch: str,
    pr_lister: Callable[[str], list[dict]],
    skip_branches: Iterable[str] = ("main",),
) -> dict[str, list]:
    """Walk branches and bucket them by classification.

    Returns a dict with keys MERGED / OPEN / CLOSED_UNMERGED / NO_PR
    each mapping to a list of (branch, pr_number_or_None) tuples.
    ``current_branch`` and anything in ``skip_branches`` are silently
    excluded from the buckets (can't delete the checked-out branch
    or main).

    The PR-lister call is injected so tests can run without ``gh``
    actually executing.
    """
    skip = set(skip_branches) | {current_branch}
    buckets: dict[str, list[tuple[str, int | None, int | None]]] = {
        MERGED: [], OPEN: [], CLOSED_UNMERGED: [], NO_PR: [],
    }
    for branch in branches:
        if branch in skip or not branch:
            continue
        prs = pr_lister(branch)
        status, pr_num, also_open = classify_branch(prs)
        buckets[status].append((branch, pr_num, also_open))
    return buckets


def _print_buckets(buckets: dict[str, list], *, apply: bool) -> None:
    merged = buckets[MERGED]
    open_ = buckets[OPEN]
    closed = buckets[CLOSED_UNMERGED]
    nopr = buckets[NO_PR]

    print(f"Squash-merged branches (safe to delete): {len(merged)}")
    for branch, pr_num, also_open in sorted(merged):
        suffix = (
            f"  [ALSO has OPEN PR #{also_open} — reused branch name; "
            f"delete is safe but recreate-locally will collide upstream]"
            if also_open is not None
            else ""
        )
        print(f"  {branch}  (PR #{pr_num}){suffix}")
    if open_:
        print(f"\nOpen PRs (keep): {len(open_)}")
        for branch, pr_num, _ in sorted(open_):
            print(f"  {branch}  (PR #{pr_num})")
    if closed:
        print(f"\nClosed unmerged (keep, may need attention): {len(closed)}")
        for branch, pr_num, _ in sorted(closed):
            print(f"  {branch}  (PR #{pr_num})")
    if nopr:
        print(f"\nNo PR (local-only, keep): {len(nopr)}")
        for branch in sorted(b for b, _, _ in nopr):
            print(f"  {branch}")

    if not apply:
        print(
            f"\n(dry-run) re-run with --apply to delete "
            f"{len(merged)} branch(es)."
        )


def _delete_branches(
    candidates: list[tuple[str, int | None, int | None]],
    *,
    git_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> list[tuple[str, str]]:
    """Force-delete each candidate branch. Returns list of (branch, stderr) failures."""
    failures: list[tuple[str, str]] = []
    for branch, pr_num, _also_open in candidates:
        result = subprocess.run(
            ["git", "branch", "-D", branch],
            capture_output=True,
            text=True,
        ) if git_runner is None else git_runner(["branch", "-D", branch])
        if result.returncode != 0:
            failures.append((branch, result.stderr.strip()))
            print(
                f"failed to delete {branch} (PR #{pr_num}): "
                f"{result.stderr.strip()}",
                file=sys.stderr,
            )
        else:
            print(f"deleted {branch} (PR #{pr_num})")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cleanup_merged_branches.py",
        description=(
            "Identify and (optionally) delete local feature branches "
            "whose PRs are squash-merged on GitHub. Default is dry-run; "
            "pass --apply to actually delete."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete the identified branches (default: dry-run only)",
    )
    args = parser.parse_args(argv)

    try:
        branches = _list_local_branches()
        current = _current_branch()
    except subprocess.CalledProcessError as exc:
        print(f"git invocation failed: {exc}", file=sys.stderr)
        return 2

    try:
        buckets = collect_branch_statuses(
            branches,
            current_branch=current,
            pr_lister=_gh_pr_list_for_branch,
        )
    except subprocess.CalledProcessError as exc:
        print(f"gh invocation failed: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"gh returned non-JSON output: {exc}", file=sys.stderr)
        return 2

    _print_buckets(buckets, apply=args.apply)

    if not args.apply:
        return 0

    failures = _delete_branches(buckets[MERGED])
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
