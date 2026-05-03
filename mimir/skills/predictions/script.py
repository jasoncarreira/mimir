"""Predictions CLI — `mimir predictions <action>`.

Bundled-script subcommand the predictions skill invokes from agent
Bash. Records structured predictions to ``state/predictions.jsonl``;
supports add / list / review / mark / stats / verify.

Storage shape (one JSON object per line):

    {
      "id":             "pred-2026-05-02-a1b2",
      "made_at":        "2026-05-02T22:00:00+00:00",
      "by":             "agent",  # "agent" | "operator"
      "claim":          "Tim will reply within 24h",
      "kind":           "binary",  # binary | numeric | tool_freq | error_rate
      "horizon_hours":  24,
      "verifiable_by":  "operator_review",  # | events_jsonl | turns_jsonl
      "rationale":      "...",
      "review_after":   "2026-05-03T22:00:00+00:00",
      "status":         "pending",  # | correct | wrong | partial | unverifiable
      "actual":         null,
      "reviewed_at":    null,
      "lesson":         null,
      # Kind-specific fields:
      "target":         null,    # numeric / tool_freq / error_rate
      "target_tool":    null,    # tool_freq
      "tolerance":      null,    # numeric
    }

Updates use append-only: ``mark`` rewrites the line in place via
load-mutate-save (small file, dozens of entries). Auto-verify
recomputes the status from logs and persists the actual value.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VALID_KINDS = {"binary", "numeric", "tool_freq", "error_rate"}
VALID_STATUSES = {"pending", "correct", "wrong", "partial", "unverifiable"}
VALID_VERIFIABLE_BY = {"operator_review", "events_jsonl", "turns_jsonl"}
VALID_AUTHORS = {"agent", "operator"}


# ─── Data model ────────────────────────────────────────────────────────


@dataclass
class Prediction:
    id: str
    made_at: str
    by: str
    claim: str
    kind: str
    horizon_hours: int
    verifiable_by: str
    rationale: str
    review_after: str
    status: str = "pending"
    actual: str | None = None
    reviewed_at: str | None = None
    lesson: str | None = None
    target: float | None = None
    target_tool: str | None = None
    tolerance: float | None = None


# ─── Storage ───────────────────────────────────────────────────────────


def _path(home: Path) -> Path:
    return home / "state" / "predictions.jsonl"


def _load(home: Path) -> list[Prediction]:
    path = _path(home)
    if not path.is_file():
        return []
    out: list[Prediction] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            out.append(Prediction(**data))
        except TypeError:
            # Schema drift: skip rows we can't construct.
            continue
    return out


def _save_all(home: Path, preds: list[Prediction]) -> None:
    """Rewrite the entire file. Atomic via tmp + rename."""
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for p in preds:
            f.write(json.dumps(asdict(p), ensure_ascii=False) + "\n")
    tmp.replace(path)


def _append_one(home: Path, pred: Prediction) -> None:
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(pred), ensure_ascii=False) + "\n")


# ─── Helpers ───────────────────────────────────────────────────────────


def _gen_id(now: datetime | None = None) -> str:
    now = now or datetime.now(tz=timezone.utc)
    return f"pred-{now.strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:4]}"


def _parse_iso(s: str) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _default_verifiable_by(kind: str) -> str:
    if kind == "tool_freq":
        return "turns_jsonl"
    if kind == "error_rate":
        return "events_jsonl"
    return "operator_review"


# ─── Auto-verification ─────────────────────────────────────────────────


def _count_tool_calls(turns_log: Path, *, tool_name: str,
                       start: datetime, end: datetime) -> int:
    if not turns_log.is_file():
        return 0
    n = 0
    for line in turns_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_iso(rec.get("ts", ""))
        if ts is None or ts < start or ts >= end:
            continue
        for ev in rec.get("events") or []:
            if (isinstance(ev, dict) and ev.get("type") == "tool_call"
                    and ev.get("name") == tool_name):
                n += 1
    return n


_ERROR_EVENT_TYPES = {
    "tool_call_denied", "tool_denied", "scheduled_tick_dropped",
    "scheduled_tick_suppressed", "rate_limit_off_pace", "cost_rate_alert",
    "introspection_report_error", "saga_consolidate_error",
}


def _count_error_events(events_log: Path, *,
                         start: datetime, end: datetime) -> int:
    if not events_log.is_file():
        return 0
    n = 0
    for line in events_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_iso(rec.get("timestamp", ""))
        if ts is None or ts < start or ts >= end:
            continue
        if rec.get("type") in _ERROR_EVENT_TYPES:
            n += 1
    return n


def _auto_verify(pred: Prediction, home: Path,
                  *, now: datetime | None = None) -> tuple[str, str | None]:
    """Compute (status, actual) for an auto-verifiable prediction.
    Returns ("unverifiable", None) when prerequisites are missing."""
    now = now or datetime.now(tz=timezone.utc)
    made = _parse_iso(pred.made_at)
    if made is None:
        return ("unverifiable", "made_at unparseable")

    horizon_end = made + timedelta(hours=pred.horizon_hours)
    if now < horizon_end:
        return ("pending", None)  # not yet ready to evaluate

    if pred.kind == "tool_freq":
        if not pred.target_tool or pred.target is None:
            return ("unverifiable", "target_tool / target missing")
        actual_n = _count_tool_calls(
            home / "logs" / "turns.jsonl",
            tool_name=pred.target_tool,
            start=made, end=horizon_end,
        )
        # Predicate: actual >= target
        status = "correct" if actual_n >= pred.target else "wrong"
        return (status, f"{pred.target_tool} invoked {actual_n} times "
                        f"(target ≥ {int(pred.target)})")

    if pred.kind == "error_rate":
        # Compare error-event counts in equal-length before/after windows.
        if pred.target is None:
            return ("unverifiable", "target ratio missing")
        before = _count_error_events(
            home / "logs" / "events.jsonl",
            start=made - timedelta(hours=pred.horizon_hours),
            end=made,
        )
        after = _count_error_events(
            home / "logs" / "events.jsonl",
            start=made, end=horizon_end,
        )
        if before == 0:
            return ("unverifiable", "no baseline errors to compare against")
        ratio = after / before
        status = "correct" if ratio <= pred.target else "wrong"
        return (status, f"errors {before} → {after} (ratio {ratio:.2f}, "
                        f"target ≤ {pred.target:.2f})")

    if pred.kind == "numeric":
        # Pure numeric needs an --actual on review; can't auto-verify.
        return ("unverifiable", "numeric kind needs operator-supplied actual")

    return ("unverifiable", f"kind {pred.kind} not auto-verifiable")


# ─── Subcommands ───────────────────────────────────────────────────────


def cmd_add(args: argparse.Namespace) -> int:
    if args.kind not in VALID_KINDS:
        print(f"add: invalid --kind {args.kind!r} (valid: {sorted(VALID_KINDS)})",
              file=sys.stderr)
        return 1
    if args.by not in VALID_AUTHORS:
        print(f"add: invalid --by {args.by!r}", file=sys.stderr)
        return 1
    if args.horizon_hours <= 0:
        print("add: --horizon-hours must be positive", file=sys.stderr)
        return 1

    verifiable_by = args.verifiable_by or _default_verifiable_by(args.kind)
    if verifiable_by not in VALID_VERIFIABLE_BY:
        print(f"add: invalid --verifiable-by {verifiable_by!r}",
              file=sys.stderr)
        return 1

    # Kind-specific guard rails so the auto-verifier has what it needs.
    if args.kind == "tool_freq":
        if not args.target_tool or args.target is None:
            print("add: tool_freq requires --target-tool and --target",
                  file=sys.stderr)
            return 1
    if args.kind == "error_rate" and args.target is None:
        print("add: error_rate requires --target (ratio threshold, "
              "e.g. 0.5 = halved)", file=sys.stderr)
        return 1

    now = datetime.now(tz=timezone.utc)
    pred = Prediction(
        id=_gen_id(now),
        made_at=now.isoformat(),
        by=args.by,
        claim=args.claim,
        kind=args.kind,
        horizon_hours=args.horizon_hours,
        verifiable_by=verifiable_by,
        rationale=args.rationale or "",
        review_after=(now + timedelta(hours=args.horizon_hours)).isoformat(),
        target=args.target,
        target_tool=args.target_tool,
        tolerance=args.tolerance,
    )
    home = _resolve_home(args)
    _append_one(home, pred)
    print(pred.id)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    home = _resolve_home(args)
    preds = _load(home)
    status_filter = args.status
    if status_filter and status_filter not in VALID_STATUSES:
        print(f"list: invalid --status {status_filter!r}", file=sys.stderr)
        return 1
    if status_filter:
        preds = [p for p in preds if p.status == status_filter]
    if args.json:
        json.dump([asdict(p) for p in preds], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if not preds:
        print("(no predictions)")
        return 0
    for p in preds:
        print(_format_one(p))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Show predictions ready for evaluation. Auto-verifies the
    auto-verifiable kinds and persists the result."""
    home = _resolve_home(args)
    preds = _load(home)
    now = datetime.now(tz=timezone.utc)

    pending = [p for p in preds if p.status == "pending"]
    if args.horizon_elapsed_only:
        pending = [
            p for p in pending
            if _parse_iso(p.review_after) is not None
            and _parse_iso(p.review_after) <= now
        ]

    # Auto-verify what we can; collect what needs operator attention.
    verified_auto: list[Prediction] = []
    needs_operator: list[Prediction] = []
    for p in pending:
        if p.verifiable_by == "operator_review":
            needs_operator.append(p)
            continue
        status, actual = _auto_verify(p, home, now=now)
        if status == "pending":
            continue  # not yet at horizon
        p.status = status
        p.actual = actual
        p.reviewed_at = now.isoformat()
        verified_auto.append(p)

    if verified_auto:
        # Persist the auto-verified updates.
        by_id = {p.id: p for p in preds}
        for p in verified_auto:
            by_id[p.id] = p
        _save_all(home, list(by_id.values()))

    if args.json:
        json.dump({
            "auto_verified": [asdict(p) for p in verified_auto],
            "needs_operator_review": [asdict(p) for p in needs_operator],
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if verified_auto:
        print(f"## Auto-verified ({len(verified_auto)})")
        print()
        for p in verified_auto:
            print(_format_one(p, include_actual=True))
        print()
    if needs_operator:
        print(f"## Needs operator review ({len(needs_operator)})")
        print()
        for p in needs_operator:
            print(_format_one(p, include_review_age=True, now=now))
        print()
        print("Mark via:")
        print("  mimir predictions mark <id> --status correct|wrong|partial|unverifiable \\")
        print("    [--actual '...'] [--lesson '...']")
    if not verified_auto and not needs_operator:
        print("(no predictions ready for review)")
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    if args.status not in VALID_STATUSES:
        print(f"mark: invalid --status {args.status!r}", file=sys.stderr)
        return 1
    if args.status == "wrong" and not args.lesson:
        print("mark: --lesson required when marking wrong "
              "(trace the incorrect assumption to a memory block)",
              file=sys.stderr)
        return 1

    home = _resolve_home(args)
    preds = _load(home)
    found = None
    for p in preds:
        if p.id == args.id or args.id in p.id:
            found = p
            break
    if found is None:
        print(f"mark: no prediction matching {args.id!r}", file=sys.stderr)
        return 1

    found.status = args.status
    if args.actual is not None:
        found.actual = args.actual
    if args.lesson is not None:
        found.lesson = args.lesson
    found.reviewed_at = datetime.now(tz=timezone.utc).isoformat()
    _save_all(home, preds)
    print(f"marked {found.id}: {found.status}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    home = _resolve_home(args)
    preds = _load(home)
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=args.days)
    recent = [p for p in preds
              if (made := _parse_iso(p.made_at)) is not None and made >= cutoff]

    by_status: Counter[str] = Counter(p.status for p in recent)
    by_kind: Counter[str] = Counter(p.kind for p in recent)
    by_author: Counter[str] = Counter(p.by for p in recent)

    decided = [p for p in recent if p.status in ("correct", "wrong", "partial")]
    correct = sum(1 for p in decided if p.status == "correct")
    accuracy = (correct / len(decided)) if decided else None

    if args.json:
        json.dump({
            "window_days": args.days,
            "total": len(recent),
            "by_status": dict(by_status),
            "by_kind": dict(by_kind),
            "by_author": dict(by_author),
            "decided": len(decided),
            "accuracy": accuracy,
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"# Predictions stats (last {args.days}d)")
    print()
    print(f"Total: {len(recent)}")
    print(f"Decided (correct + wrong + partial): {len(decided)}")
    if accuracy is not None:
        print(f"Accuracy: {accuracy * 100:.1f}% ({correct}/{len(decided)})")
    else:
        print("Accuracy: n/a (no decided predictions in window)")
    print()
    print("By status:")
    for status in ("pending", "correct", "wrong", "partial", "unverifiable"):
        print(f"  {status}: {by_status.get(status, 0)}")
    print()
    print("By kind:")
    for kind, n in by_kind.most_common():
        print(f"  {kind}: {n}")
    print()
    print("By author:")
    for author, n in by_author.most_common():
        print(f"  {author}: {n}")
    return 0


# ─── Formatting ────────────────────────────────────────────────────────


def _format_one(p: Prediction, *, include_actual: bool = False,
                 include_review_age: bool = False,
                 now: datetime | None = None) -> str:
    parts: list[str] = []
    parts.append(f"### {p.id}  ({p.status})")
    parts.append(f"_{p.kind} · by {p.by} · made {p.made_at[:16]} · "
                 f"horizon {p.horizon_hours}h_")
    parts.append(f"**Claim:** {p.claim}")
    if p.kind == "tool_freq" and p.target_tool:
        parts.append(f"**Predicate:** {p.target_tool} invoked ≥ "
                     f"{int(p.target or 0)} times in window")
    if p.kind == "error_rate" and p.target is not None:
        parts.append(f"**Predicate:** error count ratio ≤ "
                     f"{p.target:.2f} (after / before)")
    if p.rationale:
        parts.append(f"**Rationale:** {p.rationale}")
    if include_actual and p.actual:
        parts.append(f"**Actual:** {p.actual}")
    if p.lesson:
        parts.append(f"**Lesson:** {p.lesson}")
    if include_review_age and now is not None:
        review_after = _parse_iso(p.review_after)
        if review_after is not None:
            age = now - review_after
            if age.total_seconds() >= 0:
                hrs = int(age.total_seconds() / 3600)
                parts.append(f"_review-due: {hrs}h ago_")
            else:
                hrs = int(-age.total_seconds() / 3600)
                parts.append(f"_review-due: in {hrs}h_")
    return "\n".join(parts) + "\n"


# ─── Argparse + dispatch ───────────────────────────────────────────────


def _resolve_home(args: argparse.Namespace) -> Path:
    home = args.home or os.environ.get("MIMIR_HOME") or Path.cwd()
    return Path(home).resolve()


def add_argparse(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="predictions_action")

    add_p = sub.add_parser("add", help="Record a new prediction.")
    add_p.add_argument("--claim", required=True,
                       help="The forward-looking claim being made.")
    add_p.add_argument("--kind", default="binary",
                       choices=sorted(VALID_KINDS))
    add_p.add_argument("--horizon-hours", type=int, required=True,
                       help="Hours from now until the prediction is "
                            "ready for evaluation.")
    add_p.add_argument("--verifiable-by", default=None,
                       choices=sorted(VALID_VERIFIABLE_BY),
                       help="How the prediction will be checked. "
                            "Defaults from kind.")
    add_p.add_argument("--rationale", default="",
                       help="Why you believe the claim — what gets "
                            "traced back when wrong.")
    add_p.add_argument("--by", default="agent",
                       choices=sorted(VALID_AUTHORS))
    add_p.add_argument("--target", type=float, default=None,
                       help="numeric: expected value; tool_freq: "
                            "minimum count; error_rate: max ratio.")
    add_p.add_argument("--target-tool", default=None,
                       help="tool_freq: which tool's invocations to count.")
    add_p.add_argument("--tolerance", type=float, default=None,
                       help="numeric: ± tolerance around --target.")
    add_p.add_argument("--home", type=Path, default=None,
                       help="Agent home (overrides MIMIR_HOME; default: cwd).")

    list_p = sub.add_parser("list", help="List predictions, optionally "
                             "filtered by status.")
    list_p.add_argument("--status", default=None,
                        help=f"Filter by status: {sorted(VALID_STATUSES)}")
    list_p.add_argument("--json", action="store_true")
    list_p.add_argument("--home", type=Path, default=None)

    rev_p = sub.add_parser("review", help="Surface predictions past their "
                            "horizon. Auto-verifies what it can.")
    rev_p.add_argument("--horizon-elapsed-only", action="store_true",
                       help="Only show predictions whose review_after has "
                            "passed.")
    rev_p.add_argument("--json", action="store_true")
    rev_p.add_argument("--home", type=Path, default=None)

    mark_p = sub.add_parser("mark", help="Mark a pending prediction "
                             "correct/wrong/partial/unverifiable.")
    mark_p.add_argument("id", help="Prediction id (or unique substring).")
    mark_p.add_argument("--status", required=True,
                        choices=sorted(VALID_STATUSES))
    mark_p.add_argument("--actual", default=None,
                        help="What actually happened (string).")
    mark_p.add_argument("--lesson", default=None,
                        help="REQUIRED when --status wrong: what assumption "
                             "broke and which memory block to update.")
    mark_p.add_argument("--home", type=Path, default=None)

    stats_p = sub.add_parser("stats", help="Accuracy by kind / by author.")
    stats_p.add_argument("--days", type=int, default=30,
                         help="Window in days (default 30).")
    stats_p.add_argument("--json", action="store_true")
    stats_p.add_argument("--home", type=Path, default=None)


def run(args: argparse.Namespace) -> int:
    action = getattr(args, "predictions_action", None)
    if action == "add":
        return cmd_add(args)
    if action == "list":
        return cmd_list(args)
    if action == "review":
        return cmd_review(args)
    if action == "mark":
        return cmd_mark(args)
    if action == "stats":
        return cmd_stats(args)
    print("usage: mimir predictions {add,list,review,mark,stats} ...",
          file=sys.stderr)
    return 1


def main() -> None:
    p = argparse.ArgumentParser(description="Predictions tracking CLI.")
    add_argparse(p)
    sys.exit(run(p.parse_args()))


if __name__ == "__main__":
    main()
