"""Feedback subcommand — ``mimir feedback {mark-resolved,emit}``.

Extracted from ``mimir.cli`` (Phase 2, chainlink #240).
Business logic lives in ``mimir.feedback_cmd``; this module owns the
argparse tree and dispatches into it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def add_argparse(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """Register ``mimir feedback`` subcommand tree.  Returns the created
    parser so the caller can pass it to :func:`dispatch` for ``print_help``."""
    feedback_p = sub.add_parser(
        "feedback",
        help="Feedback observability helpers (mark-resolved, emit).",
    )
    feedback_sub = feedback_p.add_subparsers(dest="feedback_action")

    # ``mimir feedback mark-resolved``
    feedback_mr_p = feedback_sub.add_parser(
        "mark-resolved",
        help=(
            "Append a resolved-incident rule to resolved-incidents.jsonl so matching "
            "events are silenced from the algedonic feedback block."
        ),
    )
    feedback_mr_p.add_argument(
        "--type", required=True, dest="event_type",
        help="Event type to suppress, or '*' to match any type.",
    )
    feedback_mr_p.add_argument(
        "--pattern", default="",
        help=(
            "Substring to match against the event JSON (empty = match all events "
            "of the given type)."
        ),
    )
    feedback_mr_p.add_argument(
        "--reason", required=True,
        help="Free-text rationale for marking resolved (stored in the JSONL line).",
    )
    feedback_mr_p.add_argument(
        "--resolved-at", default=None, dest="resolved_at",
        help=(
            "ISO-8601 timestamp the fix landed (default: now() UTC).  Suppresses "
            "events timestamped *before* this value."
        ),
    )
    feedback_mr_p.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help=(
            "Preview how many events in the current 24h window would be filtered; "
            "don't write to resolved-incidents.jsonl."
        ),
    )
    feedback_mr_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # ``mimir feedback emit``
    feedback_emit_p = feedback_sub.add_parser(
        "emit",
        help=(
            "Write a structured event to events.jsonl.  Useful for Bash-side skill "
            "code that wants to emit auditable events without touching Python internals."
        ),
    )
    feedback_emit_p.add_argument(
        "event_type",
        help="Event type to emit (e.g. 'pr_merge_blocked_by_changes_requested').",
    )
    feedback_emit_p.add_argument(
        "pairs",
        nargs="*",
        metavar="KEY=VALUE",
        help=(
            "Optional key=value payload fields.  Values are stored as strings "
            "by default; use --json-values to JSON-parse them."
        ),
    )
    feedback_emit_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )
    feedback_emit_p.add_argument(
        "--json-values",
        action="store_true",
        dest="json_values",
        default=False,
        help=(
            "JSON-parse each KEY=VALUE value. Lets you pass structured data: "
            "blocking_reviewers='[\"alice\",\"bob\"]' pr=42"
        ),
    )

    return feedback_p


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Handle ``mimir feedback …`` dispatch.  Returns an exit code."""
    if args.feedback_action == "mark-resolved":
        from ..feedback_cmd import run_mark_resolved
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        return run_mark_resolved(
            home=home,
            event_type=args.event_type,
            pattern=args.pattern,
            reason=args.reason,
            resolved_at=args.resolved_at,
            dry_run=args.dry_run,
        )
    if args.feedback_action == "emit":
        from ..feedback_cmd import run_emit_event
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        return run_emit_event(
            home=home,
            event_type=args.event_type,
            pairs=args.pairs,
            json_values=getattr(args, "json_values", False),
        )
    parser.print_help()
    return 1
