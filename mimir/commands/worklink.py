"""CLI plumbing for ``mimir worklink``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from ..worklink.orchestrator import LeafValidationError, WorklinkError, run_worklink
from ..worklink.worker import payload_from_json, run_worker_payload


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

    worker_p = worklink_sub.add_parser("worker", help="Run one portable Worklink worker payload.")
    worker_p.add_argument("payload", type=Path, help="Path to worker payload JSON.")

    return worklink_p


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.worklink_action is None:
        parser.print_help()
        return 1
    if args.worklink_action == "worker":
        payload = payload_from_json(json.loads(args.payload.read_text(encoding="utf-8")))
        validation = asyncio.run(run_worker_payload(payload))
        suffix = " review-ready" if validation.review_ready else ""
        print(f"worklink worker: {validation.status}{suffix}")
        return 0

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
        result = run_worklink(
            home=home,
            repo=repo,
            issue_id=args.issue_id,
            backend=args.backend,
            dry_run=args.dry_run,
            test_command=args.test_command,
        )
    except LeafValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except WorklinkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if result.dry_run:
        return 0
    print(
        f"worklink #{result.issue_id} attempt {result.attempt}: {result.status}"
        + (" review-ready" if result.review_ready else "")
        + (f" PR {result.pr_url}" if result.pr_url else "")
    )
    if result.evidence_path:
        print(f"evidence: {result.evidence_path}")
    return 0 if result.status in {"completed", "blocked"} else 1
