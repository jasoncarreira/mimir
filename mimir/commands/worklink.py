"""CLI plumbing for ``mimir worklink``."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys

from ..worklink.control import stop_worklink, worklink_status
from ..worklink.orchestrator import (
    LeafValidationError,
    WorklinkError,
    run_worklink,
    run_worklink_epic,
    run_worklink_reattach,
)


def add_argparse(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    worklink_p = sub.add_parser("worklink", help="Run Worklink executor jobs.")
    worklink_sub = worklink_p.add_subparsers(dest="worklink_action")

    run_p = worklink_sub.add_parser("run", help="Run one ready Chainlink leaf issue.")
    run_p.add_argument("issue_id", type=int, help="Chainlink issue id to execute.")
    run_p.add_argument(
        "--backend", default=None, help="Backend name (default: route from worklink.yaml)."
    )

    status_p = worklink_sub.add_parser("status", help="List and classify leaf Worklink runs.")
    status_p.add_argument("issue_ids", type=int, nargs="*", help="Optional issue ids to inspect.")
    status_p.add_argument("--home", type=Path, default=None, help="Agent home.")

    stop_p = worklink_sub.add_parser("stop", help="Safely stop one live leaf Worklink run.")
    stop_p.add_argument("issue_id", type=int, help="Chainlink issue id to stop.")
    stop_p.add_argument("--home", type=Path, default=None, help="Agent home.")
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the WorkOrder and stop before claiming/spawning.",
    )
    run_p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )
    run_p.add_argument(
        "--repo", type=Path, default=None, help="Git repo to work in (default: cwd)."
    )
    run_p.add_argument(
        "--test-command", default=None, help="Override the configured evidence test command."
    )
    run_p.add_argument(
        "--base",
        default=None,
        help=(
            "Base branch to cut the worktree from and target the PR at "
            "(overrides worklink.yaml defaults.base_branch; default: main)."
        ),
    )
    run_p.add_argument(
        "--reattach",
        action="store_true",
        help=(
            "Resume an in-flight run after a controller restart (#561): wait on "
            "the persisted worker handle, harvest evidence, and open the PR "
            "instead of re-claiming and re-running from scratch. Used by the "
            "startup reconcile; no-op (exit 1) if no run state exists for the issue."
        ),
    )
    run_p.add_argument(
        "--autonomous",
        action="store_true",
        help=(
            "Mark this an autonomous dispatch (set by the ready-queue poller). "
            "Enforces the compute-backend autonomy policy: refuses the "
            "unsandboxed local_subprocess substrate unless "
            "defaults.allow_autonomous_local_subprocess is set. Omit for "
            "operator-invoked runs — they always proceed (accept-the-risk; the "
            "local_subprocess backend runs with full container filesystem access)."
        ),
    )

    run_epic_p = worklink_sub.add_parser("run-epic", help="Run a worklink:epic issue via feature-factory.")
    run_epic_p.add_argument("issue_id", type=int, help="Chainlink epic issue id to execute.")
    run_epic_p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )
    run_epic_p.add_argument(
        "--repo", type=Path, default=None, help="Git repo to work in (default: cwd)."
    )
    run_epic_p.add_argument(
        "--autonomous",
        action="store_true",
        help=(
            "Mark this an autonomous dispatch (set by the ready-queue poller). "
            "Enforces the compute-backend autonomy policy."
        ),
    )

    return worklink_p


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.worklink_action is None:
        parser.print_help()
        return 1

    if args.worklink_action == "run-epic":
        return _run_epic(args, parser)

    if args.worklink_action == "status":
        return _status(args)

    if args.worklink_action == "stop":
        return _stop(args)

    if args.worklink_action != "run":
        parser.print_help()
        return 1

    home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
    repo = (args.repo or Path.cwd()).resolve()
    os.environ["MIMIR_HOME"] = str(home)
    try:
        from ..event_logger import init_logger

        init_logger(
            home / "logs" / "events.jsonl",
            session_id=f"worklink-{args.issue_id}",
        )
    except Exception:
        # Worklink events are algedonic telemetry, not a reason to skip the
        # deterministic Chainlink/git state transition.
        pass
    try:
        if args.reattach:
            result = run_worklink_reattach(home=home, repo=repo, issue_id=args.issue_id)
        else:
            result = run_worklink(
                home=home,
                repo=repo,
                issue_id=args.issue_id,
                backend=args.backend,
                dry_run=args.dry_run,
                test_command=args.test_command,
                base_branch=args.base,
                autonomous=args.autonomous,
            )
    except LeafValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except WorklinkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # The run boundary already emitted scrubbed failure telemetry.
        from ..redaction import redact_text

        print(f"error: {redact_text(f'{type(exc).__name__}: {exc}')}", file=sys.stderr)
        return 1

    if result.dry_run:
        return 0
    if result.status == "refused":
        # Autonomy policy declined this run (e.g. unsandboxed compute without
        # opt-in). Not a failure of the work — surface the reason and exit 1 so
        # an autonomous caller treats it as "did not run".
        print(f"worklink #{result.issue_id}: refused — {result.reason}", file=sys.stderr)
        return 1
    print(
        f"worklink #{result.issue_id} attempt {result.attempt}: {result.status}"
        + (" review-ready" if result.review_ready else "")
        + (f" PR {result.pr_url}" if result.pr_url else "")
        + (f" — {result.reason}" if result.reason else "")
    )
    if result.evidence_path:
        print(f"evidence: {result.evidence_path}")
    return 0 if result.status in {"completed", "blocked"} else 1


def _status(args: argparse.Namespace) -> int:
    home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
    try:
        rows = worklink_status(home, issue_ids=args.issue_ids)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("No Worklink leaf runs or labeled issues.")
        return 0
    print("ISSUE  STATE       ATTEMPT  BACKEND           BRANCH                    CHECKOUT  ELAPSED")
    for row in rows:
        state = row.state
        elapsed = _format_elapsed(row.elapsed_s) if row.elapsed_s is not None else "-"
        print(
            f"{row.issue_id:<6} {row.classification:<11} "
            f"{str(state.attempt if state else '-'):<8} "
            f"{(state.backend if state else '-'):<17} "
            f"{(state.branch if state else '-'):<25} "
            f"{(state.checkout if state and state.checkout else '-'):<9} {elapsed}"
        )
        if row.disagreement:
            print(f"  disagreement: {row.disagreement}")
    return 0


def _stop(args: argparse.Namespace) -> int:
    home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
    result = stop_worklink(home, args.issue_id)
    if not result.stopped:
        print(f"worklink #{args.issue_id}: nothing stopped ({result.reason})")
        return 1
    print(
        f"worklink #{args.issue_id}: stopped; "
        f"state={'cleared' if result.state_cleared else 'unchanged'}, "
        f"claim={'released' if result.claim_released else 'unchanged'}, "
        f"in-progress label={'cleared' if result.label_cleared else 'unchanged'}"
    )
    return 0


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def _run_epic(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Handle the run-epic command for worklink:epic issues via feature-factory."""
    home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
    repo = (args.repo or Path.cwd()).resolve()
    os.environ["MIMIR_HOME"] = str(home)
    try:
        from ..event_logger import init_logger

        init_logger(
            home / "logs" / "events.jsonl",
            session_id=f"worklink-epic-{args.issue_id}",
        )
    except Exception:
        pass
    try:
        result = run_worklink_epic(
            home=home,
            repo=repo,
            issue_id=args.issue_id,
            autonomous=args.autonomous,
        )
    except LeafValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except WorklinkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"worklink:epic #{result.issue_id} attempt {result.attempt}: {result.status}"
        + (" review-ready" if result.review_ready else "")
        + (f" PR {result.pr_url}" if result.pr_url else "")
        + (f" — {result.reason}" if result.reason else "")
    )
    if result.evidence_path:
        print(f"evidence: {result.evidence_path}")
    return 0 if result.status in {"completed", "review_ready", "blocked"} else 1
