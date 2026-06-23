"""Reflection subcommand — ``mimir reflection <action>``.

Extracted from ``mimir.cli`` (Phase 2, chainlink #240).
Business logic lives in ``mimir.reflection.*``; this module owns the
argparse tree and dispatches into it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def add_argparse(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """Register ``mimir reflection`` subcommand tree.  Returns the created
    parser so the caller can pass it to :func:`dispatch` for ``print_help``."""
    refl_p = sub.add_parser(
        "reflection",
        help="Reflection skill helpers (invoked by skills/reflection/SKILL.md).",
    )
    refl_sub = refl_p.add_subparsers(dest="reflection_action")

    # ``mimir reflection most-retrieved``
    refl_mr_p = refl_sub.add_parser(
        "most-retrieved",
        help="Top-N SAGA atoms by retrieval count over the last N days.",
    )
    from ..reflection import most_retrieved as _most_retrieved
    _most_retrieved.add_argparse(refl_mr_p)

    # ``mimir reflection mark-applied`` — §12.2 applied-proposals audit
    refl_ma_p = refl_sub.add_parser(
        "mark-applied",
        help=(
            "Legacy: move a proposal from '## Pending' to '## Applied' in "
            "state/proposed-changes.md and append to applied-proposals.jsonl."
        ),
    )
    refl_ma_p.add_argument(
        "id_match",
        help="Substring of the proposal heading (case-insensitive).",
    )
    refl_ma_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # ``mimir reflection introspection-report``
    refl_intro_p = refl_sub.add_parser(
        "introspection-report",
        help="Weekly behavioral / health report from turns.jsonl + events.jsonl.",
    )
    from ..reflection import introspection_report as _intro_report
    _intro_report.add_argparse(refl_intro_p)

    # ``mimir reflection list-pending``
    refl_lp_p = refl_sub.add_parser(
        "list-pending",
        help=(
            "Legacy: list pending proposals from state/proposed-changes.md "
            "(numbered in chronological order)."
        ),
    )
    refl_lp_p.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Emit JSON array of {num, heading, excerpt} objects.",
    )
    refl_lp_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # ``mimir reflection resolve``
    refl_resolve_p = refl_sub.add_parser(
        "resolve",
        help=(
            "Legacy: apply operator accept/reject decisions to pending proposals. "
            "Example: resolve \"accept 1 3 / reject 2 'not now'\""
        ),
    )
    refl_resolve_p.add_argument(
        "decision_string",
        help=(
            "Accept/reject string, e.g. \"accept 1 3\" or "
            "\"accept 1 / reject 2 'reason'\"."
        ),
    )
    refl_resolve_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # ``mimir reflection audit``
    refl_audit_p = refl_sub.add_parser(
        "audit",
        help=(
            "Print the '## Effects of prior proposals' block — "
            "predicted vs measured signals for proposals applied 1-4 weeks ago."
        ),
    )
    refl_audit_p.add_argument(
        "--weeks-back-min", type=int, default=1,
        help="Inclusive newest age in weeks (default 1).",
    )
    refl_audit_p.add_argument(
        "--weeks-back-max", type=int, default=4,
        help="Inclusive oldest age in weeks (default 4).",
    )
    refl_audit_p.add_argument(
        "--window-days", type=int, default=7,
        help="Before/after measurement window per proposal (default 7).",
    )
    refl_audit_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    return refl_p


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Handle ``mimir reflection …`` dispatch.  Returns an exit code."""
    if args.reflection_action == "most-retrieved":
        from ..reflection import most_retrieved as _most_retrieved
        return asyncio.run(_most_retrieved.run(args))

    if args.reflection_action == "introspection-report":
        from ..reflection import introspection_report as _intro_report
        return _intro_report.run(args)

    if args.reflection_action == "mark-applied":
        from ..reflection import applied_audit as _applied_audit
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        try:
            proposal = _applied_audit.mark_applied(
                home / "state" / "proposed-changes.md",
                home / "state" / "applied-proposals.jsonl",
                args.id_match,
            )
        except (FileNotFoundError, LookupError, ValueError) as exc:
            print(f"mark-applied: {exc}", file=sys.stderr)
            return 1
        print(f"Applied: {proposal.id}")
        return 0

    if args.reflection_action == "list-pending":
        import json as _json
        from ..reflection import applied_audit as _applied_audit
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        try:
            proposals = _applied_audit._list_pending_proposals(
                home / "state" / "proposed-changes.md"
            )
        except FileNotFoundError as exc:
            print(f"list-pending: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"list-pending: {exc}", file=sys.stderr)
            return 1
        if not proposals:
            if getattr(args, "json_output", False):
                print("[]")
            else:
                print("0 pending proposals")
            return 0
        if getattr(args, "json_output", False):
            print(_json.dumps(
                [{"num": n, "heading": h, "excerpt": e}
                 for n, h, e in proposals],
                ensure_ascii=False,
            ))
        else:
            for num, heading, excerpt in proposals:
                line = f"{num}. {heading}"
                if excerpt:
                    line += f"\n   {excerpt}"
                print(line)
        return 0

    if args.reflection_action == "resolve":
        from ..reflection import applied_audit as _applied_audit
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        pc_path = home / "state" / "proposed-changes.md"
        log_path = home / "state" / "applied-proposals.jsonl"

        try:
            ops = _applied_audit.parse_resolve_string(args.decision_string)
        except ValueError as exc:
            print(f"resolve: {exc}", file=sys.stderr)
            return 1

        # Number proposals once — snapshot before any mutation.
        try:
            snapshot = _applied_audit._list_pending_proposals(pc_path)
        except FileNotFoundError as exc:
            print(f"resolve: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"resolve: {exc}", file=sys.stderr)
            return 1

        # Resolve all (action, num) → heading before mutating so that
        # numbering shifts after earlier mutations don't affect later ones.
        resolved: list[tuple[str, str, str]] = []  # (action, heading, reason)
        errors: list[str] = []
        for action, num, reason in ops:
            match = next((h for n, h, _ in snapshot if n == num), None)
            if match is None:
                errors.append(
                    f"  {num}: out of range (1–{len(snapshot)})"
                    if snapshot else f"  {num}: no pending proposals"
                )
                continue
            resolved.append((action, match, reason))

        accepted: list[str] = []
        rejected: list[str] = []

        for action, heading, reason in resolved:
            if action == "accept":
                try:
                    _applied_audit.mark_applied(pc_path, log_path, heading)
                    # Find original num for output label.
                    num_label = next(
                        str(n) for n, h, _ in snapshot if h == heading
                    )
                    accepted.append(num_label)
                except (LookupError, ValueError) as exc:
                    errors.append(f"  {heading!r}: {exc}")
            else:
                default_reason = "operator declined"
                effective_reason = reason.strip() if reason.strip() else default_reason
                try:
                    _applied_audit.mark_reject(pc_path, heading, effective_reason)
                    num_label = next(
                        str(n) for n, h, _ in snapshot if h == heading
                    )
                    rejected.append(f"{num_label} ({effective_reason!r})")
                except (LookupError, ValueError) as exc:
                    errors.append(f"  {heading!r}: {exc}")

        parts = []
        if accepted:
            parts.append(f"Applied: {', '.join(accepted)}.")
        if rejected:
            parts.append(f"Rejected: {', '.join(rejected)}.")
        if errors:
            parts.append("Errors:\n" + "\n".join(errors))
        print("\n".join(parts) if parts else "Nothing to do.")
        return 1 if errors and not accepted and not rejected else 0

    if args.reflection_action == "audit":
        from ..reflection import applied_audit as _applied_audit
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        rows = _applied_audit.audit_window(
            home,
            weeks_back_min=args.weeks_back_min,
            weeks_back_max=args.weeks_back_max,
            window_days=args.window_days,
        )
        block = _applied_audit.render_audit_block(rows)
        if block is None:
            print(
                f"(no proposals applied {args.weeks_back_max}–"
                f"{args.weeks_back_min} weeks ago)"
            )
        else:
            print("## Effects of prior proposals\n")
            print(block)
        return 0

    parser.print_help()
    return 1
