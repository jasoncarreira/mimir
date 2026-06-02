"""``mimir memory`` — change-proposal PR workflow (chainlink #337/#339/#344).

Operator/debug CLI over :mod:`mimir.proposals`. The agent's primary path is the
tools (open/submit/abandon_proposal); this mirrors them for manual use and
tests. A proposal can change both memory/core and prompts in one PR:

    mimir memory open                       # create the worktree, print its path
    # ...edit <path>/memory/core/* and <path>/prompts/* by hand...
    mimir memory submit --title "…" --rationale "…"
    mimir memory abandon
    mimir memory status                     # list open proposals
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..proposals import (
    abandon_proposal,
    finalize_proposal,
    list_open_proposals,
    open_proposal,
)


def _run_open(args: argparse.Namespace) -> int:
    result = open_proposal(Path(args.home).resolve(), lane=args.lane)
    if result.ok and result.worktree is not None:
        print(f"opened {result.branch} (lane: {args.lane})")
        print(f"edit:  {result.worktree}/memory/core/ and {result.worktree}/prompts/")
        print(f'then:  mimir memory submit --lane {args.lane} --title "…" --rationale "…"')
        return 0
    print(f"error ({result.reason}): {result.detail or ''}", file=sys.stderr)
    return 1


def _run_submit(args: argparse.Namespace) -> int:
    result = finalize_proposal(
        Path(args.home).resolve(), title=args.title, rationale=args.rationale, lane=args.lane
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
    if abandon_proposal(Path(args.home).resolve(), lane=args.lane):
        print(f"abandoned the open {args.lane} proposal")
    else:
        print(f"(no open {args.lane} proposal to abandon)")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    opens = list_open_proposals(Path(args.home).resolve(), lane=args.lane)
    if not opens:
        print("(no open proposals)")
        return 0
    for branch, worktree in opens:
        print(f"- {branch}  ({worktree})")
    return 0


def add_argparse(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """Register the ``mimir memory`` subcommand tree."""
    mem_p = sub.add_parser(
        "memory",
        help="Change-proposal PR workflow for memory/core + prompts (open/submit/abandon/status).",
    )
    mem_sub = mem_p.add_subparsers(dest="memory_action")

    op = mem_sub.add_parser(
        "open", help="Open a proposal worktree under scratch/ (prints the path to edit)."
    )
    op.add_argument("--home", type=Path, default=Path.cwd())
    op.add_argument("--lane", choices=("agent", "upgrade"), default="agent")

    sb = mem_sub.add_parser(
        "submit", help="Commit + push the open proposal's memory/core + prompts changes and open a PR."
    )
    sb.add_argument("--home", type=Path, default=Path.cwd())
    sb.add_argument("--lane", choices=("agent", "upgrade"), default="agent")
    sb.add_argument("--title", required=True, help="PR title.")
    sb.add_argument("--rationale", required=True, help="Why the change (PR body + commit).")

    ab = mem_sub.add_parser("abandon", help="Discard the open proposal (no PR).")
    ab.add_argument("--home", type=Path, default=Path.cwd())
    ab.add_argument("--lane", choices=("agent", "upgrade"), default="agent")

    st = mem_sub.add_parser("status", help="List open proposals.")
    st.add_argument("--home", type=Path, default=Path.cwd())
    st.add_argument("--lane", choices=("agent", "upgrade"), default="agent")

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
