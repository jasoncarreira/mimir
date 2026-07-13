"""``mimir commitments`` CLI — operator-side management of the
commitments store.

Five subcommands ship in Phase 1:

- ``list``     — show pending / all commitments
- ``add``      — manually create a commitment (for testing /
                  operator-driven entries before extraction lands)
- ``complete`` — mark a commitment completed
- ``snooze``   — push a commitment to a later time
- ``dismiss``  — drop a commitment as no longer relevant

Trim is exposed too (``trim``), though in production a Phase 2 poller
will run it on a schedule.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    CommitmentKind,
    CommitmentRecord,
    CommitmentSensitivity,
    CommitmentStatus,
    CommitmentVisibility,
    make_commitment_id,
)
from .store import CommitmentsStore


def _parse_iso(s: str) -> float:
    """Accept an ISO-8601 string; return unix-seconds. Naive
    datetimes are treated as UTC."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _short_iso(unix_ts: float | None) -> str:
    if not unix_ts:
        return "-"
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M"
    )


# ─── Argparse wiring ───────────────────────────────────────────────


_HOME_HELP = "Agent home (overrides MIMIR_HOME; default: cwd)."


def _add_home_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--home", type=Path, default=None, help=_HOME_HELP)


def add_argparse(p: argparse.ArgumentParser) -> None:
    """Attach subparsers for the six subcommands. The parent
    ``mimir commitments`` parser is registered in ``mimir.cli``.

    ``--home`` lives on each leaf parser (not the parent) so users
    can write it after the action name, matching the natural
    invocation shape (``mimir commitments list --home /path``).

    Bare ``mimir commitments`` (no subcommand) is handled in
    ``mimir.cli`` itself — it calls ``commitments_p.print_help()``
    in lexical scope and exits 1, matching the sibling subcommands
    (identities/wiki/skills/reflection). This module's ``dispatch``
    therefore assumes ``args.commitments_action`` is set; the
    no-action fallback below is defensive (for callers that
    synthesize a namespace without going through ``mimir.cli``)."""
    sub = p.add_subparsers(dest="commitments_action")

    list_p = sub.add_parser(
        "list", help="Show commitments (default: pending only).",
    )
    _add_home_flag(list_p)
    list_p.add_argument(
        "--channel", type=str, default=None,
        help="Filter to this channel id (plus unbound).",
    )
    list_p.add_argument(
        "--status", type=str, default="pending",
        choices=["pending", "delivered", "completed",
                 "dismissed", "snoozed", "expired", "all"],
        help='Filter by status. "all" disables status filter.',
    )
    list_p.add_argument(
        "--owner", type=str, default=None,
        help="Filter by owner principal.",
    )
    list_p.add_argument(
        "--include-service", action="store_true",
        help="Include service-owned commitments (admin only).",
    )

    add_p = sub.add_parser(
        "add", help="Manually create a commitment (operator entry).",
    )
    _add_home_flag(add_p)
    add_p.add_argument("--channel", type=str, default=None)
    add_p.add_argument(
        "--recipient", type=str, default=None,
        help="Canonical identity the commitment is for (resolves via "
             "identities.py at extraction time; manual entry passes "
             "through verbatim).",
    )
    add_p.add_argument(
        "--owner", type=str, default=None,
        help="Owner principal for this commitment (for ownership and dedupe).",
    )
    add_p.add_argument(
        "--recipient-principal", type=str, default=None,
        help="Principal this commitment is addressed to (distinct from recipient).",
    )
    add_p.add_argument(
        "--visibility", type=str,
        default=CommitmentVisibility.PUBLIC.value,
        choices=[v.value for v in CommitmentVisibility],
        help="Visibility level (public/service/private).",
    )
    add_p.add_argument(
        "--service-name", type=str, default=None,
        help="Service name for service-owned commitments.",
    )
    add_p.add_argument(
        "--text", type=str, required=True,
        help="Natural-language commitment description.",
    )
    add_p.add_argument(
        "--kind", type=str,
        default=CommitmentKind.OPEN_LOOP.value,
        choices=[k.value for k in CommitmentKind],
    )
    add_p.add_argument(
        "--sensitivity", type=str,
        default=CommitmentSensitivity.ROUTINE.value,
        choices=[s.value for s in CommitmentSensitivity],
    )
    add_p.add_argument(
        "--due-iso", type=str, default=None,
        help="Due-window start as ISO-8601 (default: no anchor).",
    )
    add_p.add_argument(
        "--due-end-iso", type=str, default=None,
        help="Due-window end as ISO-8601 (default: 7 days after start).",
    )
    add_p.add_argument(
        "--reminder", type=str, default="",
        help="Suggested reminder text (used at delivery).",
    )
    add_p.add_argument(
        "--confidence", type=float, default=1.0,
        help="0-1 confidence (1.0 for manual entries).",
    )
    add_p.add_argument(
        "--dedupe-key", type=str, default=None,
        help="Override the auto-generated dedupe key. Use when "
             "backfilling from a failed extraction and the original "
             "key needs to be preserved for idempotency.",
    )
    add_p.add_argument(
        "--source-turn-id", type=str, default=None,
        help="Turn ID that produced this commitment (for traceability "
             "back to the originating turn).",
    )
    add_p.add_argument(
        "--saga-session-id", type=str, default=None,
        help="Saga session id the commitment was extracted from.",
    )

    complete_p = sub.add_parser(
        "complete", help="Mark a commitment completed.",
    )
    _add_home_flag(complete_p)
    complete_p.add_argument("id", type=str)
    complete_p.add_argument(
        "--message-id", type=str, default=None,
        help="Optional reference to the message that delivered.",
    )
    complete_p.add_argument(
        "--actor", type=str, default=None,
        help="Actor principal performing the action (for authorization).",
    )

    snooze_p = sub.add_parser(
        "snooze", help="Push a commitment to a later time.",
    )
    _add_home_flag(snooze_p)
    snooze_p.add_argument("id", type=str)
    snooze_mut = snooze_p.add_mutually_exclusive_group(required=True)
    snooze_mut.add_argument(
        "--until-iso", type=str, default=None,
        help="New earliest-deliver time as ISO-8601 (absolute).",
    )
    snooze_mut.add_argument(
        "--for-days", type=float, default=None,
        help="Snooze for N days from now (relative; fractional OK).",
    )
    snooze_p.add_argument("--reason", type=str, default=None)
    snooze_p.add_argument(
        "--actor", type=str, default=None,
        help="Actor principal performing the action (for authorization).",
    )

    dismiss_p = sub.add_parser(
        "dismiss", help="Drop a commitment as no longer relevant.",
    )
    _add_home_flag(dismiss_p)
    dismiss_p.add_argument("id", type=str)
    dismiss_p.add_argument("--reason", type=str, default=None)
    dismiss_p.add_argument(
        "--actor", type=str, default=None,
        help="Actor principal performing the action (for authorization).",
    )

    trim_p = sub.add_parser(
        "trim",
        help="Drop terminal records older than the retention window "
             "(30 days). Active records always kept. Default: dry-run "
             "(print what would be dropped); pass --apply to rewrite.",
    )
    _add_home_flag(trim_p)
    # PR #120 review finding #4b: trim is destructive + operator-
    # invoked. Default behavior is preview-only; ``--apply`` opt-in
    # required to actually rewrite the file.
    trim_p.add_argument(
        "--apply", action="store_true",
        help="Actually drop terminal records (default: dry-run).",
    )


# ─── Subcommand handlers ───────────────────────────────────────────


def _resolve_store(args: argparse.Namespace) -> CommitmentsStore:
    home = args.home or Path(
        os.environ.get("MIMIR_HOME") or Path.cwd()
    )
    home = home.resolve()
    os.environ["MIMIR_HOME"] = str(home)
    from ..config import Config
    cfg = Config.from_env()
    return CommitmentsStore(path=cfg.commitments_log)


def cmd_list(args: argparse.Namespace) -> int:
    store = _resolve_store(args)
    status = None if args.status == "all" else args.status
    rows = store.list(
        channel_id=args.channel,
        status=status,
        actor_principal=args.owner,
        include_service=args.include_service,
        owner_principal=args.owner,
    )
    if not rows:
        print("(no commitments match)")
        return 0
    for r in rows:
        due = _short_iso(r.due_window_start_unix)
        ch = r.channel_id or "<unbound>"
        recipient = f" @{r.recipient_identity}" if r.recipient_identity else ""
        owner = f" owner={r.owner_principal}" if r.owner_principal else ""
        print(
            f"{r.id}  [{r.status:9s}] {r.kind:14s} due={due}  "
            f"({ch}{recipient}{owner}) — {r.text}"
        )
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    store = _resolve_store(args)
    start = _parse_iso(args.due_iso) if args.due_iso else None
    if args.due_end_iso:
        end = _parse_iso(args.due_end_iso)
    elif start is not None:
        end = start + 7 * 86400
    else:
        end = None
    rec = CommitmentRecord(
        id=make_commitment_id(),
        channel_id=args.channel,
        text=args.text,
        kind=args.kind,
        sensitivity=args.sensitivity,
        recipient_identity=args.recipient,
        owner_principal=args.owner,
        recipient_principal=args.recipient_principal,
        visibility=args.visibility,
        service_name=args.service_name,
        suggested_reminder=args.reminder,
        due_window_start_unix=start,
        due_window_end_unix=end,
        confidence=args.confidence,
        created_at_unix=time.time(),
        dedupe_key=args.dedupe_key or "",
        source_turn_id=args.source_turn_id,
        saga_session_id=args.saga_session_id,
    )
    saved = asyncio.run(store.add(rec))
    print(f"added {saved.id} ({saved.kind}, status={saved.status})")
    return 0


def _report_transition_failure(
    args: argparse.Namespace, verb: str, store: CommitmentsStore,
) -> int:
    """Helper used by complete/snooze/dismiss when the store rejects
    the transition (already-terminal record). PR #120 re-review N2:
    surface a clear "already terminal" message to the operator rather
    than silently succeeding."""
    rec = store.current_state().get(args.id)
    if rec is None:
        print(f"error: commitment {args.id!r} not found", file=sys.stderr)
        return 2
    print(
        f"error: cannot {verb} {args.id} — already {rec.status}",
        file=sys.stderr,
    )
    return 2


def cmd_complete(args: argparse.Namespace) -> int:
    store = _resolve_store(args)
    if args.id not in store.current_state():
        print(f"error: commitment {args.id!r} not found", file=sys.stderr)
        return 2
    ok = asyncio.run(store.complete(args.id, message_id=args.message_id, actor_principal=args.actor))
    if not ok:
        return _report_transition_failure(args, "complete", store)
    print(f"completed {args.id}")
    return 0


def cmd_snooze(args: argparse.Namespace) -> int:
    store = _resolve_store(args)
    if args.id not in store.current_state():
        print(f"error: commitment {args.id!r} not found", file=sys.stderr)
        return 2
    if args.for_days is not None:
        until_unix = time.time() + args.for_days * 86400
    else:
        until_unix = _parse_iso(args.until_iso)
    ok = asyncio.run(store.snooze(
        args.id, until_unix=until_unix, reason=args.reason,
        actor_principal=args.actor,
    ))
    if not ok:
        return _report_transition_failure(args, "snooze", store)
    print(f"snoozed {args.id} until {_short_iso(until_unix)}")
    return 0


def cmd_dismiss(args: argparse.Namespace) -> int:
    store = _resolve_store(args)
    if args.id not in store.current_state():
        print(f"error: commitment {args.id!r} not found", file=sys.stderr)
        return 2
    ok = asyncio.run(store.dismiss(args.id, reason=args.reason, actor_principal=args.actor))
    if not ok:
        return _report_transition_failure(args, "dismiss", store)
    print(f"dismissed {args.id}")
    return 0


def cmd_trim(args: argparse.Namespace) -> int:
    """``mimir commitments trim`` — preview/apply terminal record purge.

    PR #120 review finding #4b: default is dry-run (preview only).
    Operators opt in to the destructive rewrite with ``--apply``.
    """
    store = _resolve_store(args)
    if args.apply:
        dropped = asyncio.run(store.trim())
        print(f"trimmed {dropped} terminal records older than "
              f"{store.terminal_retention_days} days")
        return 0
    # Dry-run: ask the store for trim candidates via the shared
    # predicate. PR #120 re-review N1: keeps the canonical
    # "what counts as trimmable" definition in one place.
    candidates = store.find_trim_candidates()
    if not candidates:
        print("(dry-run) 0 terminal records older than "
              f"{store.terminal_retention_days} days. Nothing to trim.")
        return 0
    print(f"(dry-run) would drop {len(candidates)} terminal records "
          f"older than {store.terminal_retention_days} days:")
    for rid, rec in sorted(candidates, key=lambda x: x[1].created_at_unix):
        print(f"  {rid}  [{rec.status:9s}] {rec.kind:14s} — {rec.text}")
    print("re-run with --apply to actually drop them.")
    return 0


# ─── Dispatcher ─────────────────────────────────────────────────────


def dispatch(args: argparse.Namespace) -> int:
    """Called from ``mimir/cli.py`` after argparse populates ``args``."""
    action = getattr(args, "commitments_action", None)
    if action == "list":
        return cmd_list(args)
    if action == "add":
        return cmd_add(args)
    if action == "complete":
        return cmd_complete(args)
    if action == "snooze":
        return cmd_snooze(args)
    if action == "dismiss":
        return cmd_dismiss(args)
    if action == "trim":
        return cmd_trim(args)
    # Defensive fallback for callers that synthesize a namespace
    # without going through ``mimir.cli`` (which handles the bare-
    # command help-and-exit shape in lexical scope — see
    # ``mimir/cli.py`` under ``args.command == "commitments"``).
    # Exit 1 matches the sibling subcommands' bare-invocation code.
    print("usage: mimir commitments {list|add|complete|snooze|"
          "dismiss|trim} [...]", file=sys.stderr)
    return 1
