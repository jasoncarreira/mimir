"""``mimir memory`` — core-memory PR proposal workflow (chainlink #337/#339).

Operator/debug CLI over :mod:`mimir.core_memory_pr`. The agent's primary path
is the tools (open/submit/abandon_core_memory_proposal); this mirrors them for
manual use and tests:

    mimir memory open                       # create the worktree, print its path
    # ...edit <path>/memory/core/* by hand...
    mimir memory submit --title "…" --rationale "…"
    mimir memory abandon
    mimir memory status                     # list open proposals
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..core_memory_pr import (
    abandon_proposal,
    finalize_proposal,
    list_open_proposals,
    open_proposal,
)


def _run_open(args: argparse.Namespace) -> int:
    result = open_proposal(Path(args.home).resolve())
    if result.ok and result.worktree is not None:
        print(f"opened {result.branch}")
        print(f"edit:  {result.worktree}/memory/core/")
        print('then:  mimir memory submit --title "…" --rationale "…"')
        return 0
    print(f"error ({result.reason}): {result.detail or ''}", file=sys.stderr)
    return 1


def _run_submit(args: argparse.Namespace) -> int:
    result = finalize_proposal(
        Path(args.home).resolve(), title=args.title, rationale=args.rationale
    )
    if result.ok:
        if result.pr_url:
            print(f"opened PR: {result.pr_url}")
        else:
            print(f"pushed {result.branch} ({result.reason}): {result.detail or ''}")
        return 0
    print(f"error ({result.reason}): {result.detail or ''}", file=sys.stderr)
    return 1


def _run_abandon(args: argparse.Namespace) -> int:
    if abandon_proposal(Path(args.home).resolve()):
        print("abandoned the open proposal")
    else:
        print("(no open proposal to abandon)")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    opens = list_open_proposals(Path(args.home).resolve())
    if not opens:
        print("(no open core-memory proposals)")
        return 0
    for branch, worktree in opens:
        print(f"- {branch}  ({worktree})")
    return 0


def add_argparse(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """Register the ``mimir memory`` subcommand tree."""
    mem_p = sub.add_parser(
        "memory",
        help="Core-memory PR proposal workflow (open/submit/abandon/status).",
    )
    mem_sub = mem_p.add_subparsers(dest="memory_action")

    op = mem_sub.add_parser(
        "open", help="Open a proposal worktree under scratch/ (prints the path to edit)."
    )
    op.add_argument("--home", type=Path, default=Path.cwd())

    sb = mem_sub.add_parser(
        "submit", help="Commit + push the open proposal's memory/core changes and open a PR."
    )
    sb.add_argument("--home", type=Path, default=Path.cwd())
    sb.add_argument("--title", required=True, help="PR title.")
    sb.add_argument("--rationale", required=True, help="Why the change (PR body + commit).")

    ab = mem_sub.add_parser("abandon", help="Discard the open proposal (no PR).")
    ab.add_argument("--home", type=Path, default=Path.cwd())

    st = mem_sub.add_parser("status", help="List open core-memory proposals.")
    st.add_argument("--home", type=Path, default=Path.cwd())

    return mem_p


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Handle ``mimir memory …`` dispatch. Returns an exit code."""
    action = args.memory_action
    if action == "open":
        return _run_open(args)
    if action == "submit":
        return _run_submit(args)
    if action == "abandon":
        return _run_abandon(args)
    if action == "status":
        return _run_status(args)
    parser.print_help()
    return 1
