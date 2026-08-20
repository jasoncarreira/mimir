#!/usr/bin/env python3
"""Count social-cli post-creating dispatches from sent ledgers.

This is intentionally ledger-derived only. It does not read or update any
secondary counter file, so likes/reposts/ignores and missed manual increments
cannot drift the daily post count.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple

POST_CREATING_ACTIONS = {"post", "reply", "thread"}

# Actions the ledger is known to record: the post-creating set plus the
# non-post verbs seen in deployed ledgers and archives. An action outside this
# set cannot be classified — it may be a verb upstream added, or a corrupted
# spelling of a post-creating one, and those are indistinguishable from here.
# Treating it as damage makes the count a floor instead of silently filing it
# as a non-post that does not count against the cap.
#
# The cost is deliberate: a genuinely new upstream verb makes the guard report
# an unestablished count until that verb is added here. A loud stop is the
# right trade against a silent under-count on a safety cap, and the repair is
# one entry in this set.
KNOWN_ACTIONS = POST_CREATING_ACTIONS | {
    "quote",
    "like",
    "repost",
    "ignore",
    "follow",
}


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.lower() == "today":
            return _today_utc()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            try:
                dt = datetime.combine(date.fromisoformat(raw), time.min)
            except ValueError:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _today_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def _load_yaml(path: Path) -> tuple[Any, bool]:
    """Load one ledger, returning ``(data, damaged)``.

    ``damaged`` distinguishes a ledger we could not read from one that is
    legitimately empty. Both yield no records, but only the former means the
    resulting count is a floor rather than a count — and a caller enforcing a
    cap has to be able to tell those apart.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit("PyYAML is required for social-cli count") from exc
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _eprint(f"social-cli count: could not read {path}: {exc}")
        return None, True
    if not text.strip():
        return None, False
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        _eprint(f"social-cli count: skipping malformed ledger {path}: {exc}")
        return None, True
    if not _document_is_recognized(data):
        _eprint(
            f"social-cli count: unrecognised ledger structure in {path} "
            f"(top level is {type(data).__name__}); treating as unreadable"
        )
        return None, True
    return data, False


# Top-level keys under which a ledger may nest its record list. The live
# format is ``{entries: [...]}``; the rest are tolerated historical shapes.
_LEDGER_CONTAINER_KEYS = ("entries", "ledger", "sent", "items", "results", "dispatch")


def _document_is_recognized(data: Any) -> bool:
    """Whether a parsed ledger matches a shape we know how to read.

    Syntactically valid YAML can still be a ledger we cannot interpret — a
    bare scalar, a mapping in no known shape, or a container key holding
    something other than a list of records. Those yield no records for the
    same reason an empty ledger does, which is precisely the ambiguity that
    matters here: an unreadable structure may hold the only record of a post,
    so it must not be reported as zero.

    The legitimately empty shapes (an absent document, an empty list, an empty
    container) are recognized, because an empty ledger is a real zero.
    """
    if data is None:
        return True
    if isinstance(data, list):
        return all(isinstance(item, dict) for item in data)
    if not isinstance(data, dict):
        return False
    if "action" in data and "timestamp" in data:
        return True
    present = [key for key in _LEDGER_CONTAINER_KEYS if key in data]
    if not present:
        return False
    # _records reads every container key it finds, so every one of them has
    # to be a list of records for the document to be fully interpretable.
    return all(
        isinstance(data[key], list)
        and all(isinstance(item, dict) for item in data[key])
        for key in present
    )


def _records(data: Any) -> Iterable[dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
        return
    if not isinstance(data, dict):
        return
    if "action" in data and "timestamp" in data:
        yield data
        return
    for key in _LEDGER_CONTAINER_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            yield from _records(value)


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _platform_matches(record: dict[str, Any], platform: str) -> bool:
    direct = record.get("platform")
    if direct is not None:
        return str(direct) == platform
    platforms = record.get("platforms")
    if isinstance(platforms, list):
        return platform in {str(p) for p in platforms}
    if isinstance(platforms, str):
        return platform in {p.strip() for p in platforms.split(",")}
    return False


def _action_matches(record_action: str, requested: str) -> bool:
    if requested in {"post", "posts", "post-create", "post-creating"}:
        return record_action in POST_CREATING_ACTIONS
    return record_action == requested


EXIT_LEDGER_UNREADABLE = 3


def _default_state_root() -> Path:
    state_dir = os.environ.get("STATE_DIR", "").strip()
    if state_dir:
        p = Path(state_dir).expanduser()
        if p.name.startswith("social-cli-"):
            return p.parent
    home = os.environ.get("MIMIR_HOME", "").strip()
    if home:
        return Path(home).expanduser() / "state" / "pollers"
    return Path.cwd() / "state" / "pollers"


def _ledger_files(
    platform: str,
    state_root: Path,
    state_dirs: list[Path],
) -> list[Path]:
    dirs = state_dirs
    if not dirs:
        dirs = [p for p in state_root.glob("social-cli-*") if p.is_dir()]
        if not dirs and Path.cwd().name.startswith("social-cli-"):
            dirs = [Path.cwd()]
    files: list[Path] = []
    for directory in dirs:
        for path in sorted(directory.glob("sent_ledger-*.yaml")):
            if path.name == f"sent_ledger-{platform}.yaml" or path.is_file():
                files.append(path)
    return files


class LedgerCount(NamedTuple):
    """What a ledger scan established, and how much of it is trustworthy.

    ``count``            matching records found
    ``unreadable``       sources that could not be read at all
    ``scanned``          ledger files actually read
    ``damaged_records``  records missing a field the count depends on
    ``state_dirs``       ``social-cli-*`` directories found

    ``count`` is only a complete answer when ``unreadable`` and
    ``damaged_records`` are both zero. ``state_dirs`` separates "the poller
    has run here and simply has nothing to report" from "we found no sign the
    poller has ever run", which read identically in ``count`` alone.
    """

    count: int
    unreadable: int
    scanned: int
    damaged_records: int
    state_dirs: int

    @property
    def is_floor(self) -> bool:
        """Whether ``count`` is a lower bound rather than a count."""
        return bool(self.unreadable or self.damaged_records)


def _platform_value_is_interpretable(record: dict[str, Any]) -> bool:
    """Whether the record names its platform in a shape we can compare.

    ``_platform_matches`` stringifies whatever it finds, so a mapping or an
    empty list does not raise — it simply fails to equal the requested
    platform and the record is filed as another platform's traffic. That is
    the silent path this rules out. The accepted shapes mirror the matcher: a
    non-empty ``platform`` string, or a ``platforms`` list of non-empty scalar
    names, or a non-empty comma-separated ``platforms`` string.
    """
    direct = record.get("platform")
    if direct is not None:
        return isinstance(direct, str) and bool(direct.strip())
    platforms = record.get("platforms")
    if isinstance(platforms, list):
        return bool(platforms) and all(
            isinstance(name, str) and name.strip() for name in platforms
        )
    if isinstance(platforms, str):
        return any(part.strip() for part in platforms.split(","))
    return False


def _record_is_interpretable(record: dict[str, Any]) -> bool:
    """Whether a record carries the fields this count depends on.

    A record we *chose* not to count is a normal skip: a like, another
    platform, a timestamp outside the window. A record we *could not read* is
    different — if we cannot tell what action it was, when it happened, or
    which platform it went to, we cannot rule out that it was today's post,
    and silently skipping it under-counts against the cap.

    Note that a real ledger's ``timestamp`` arrives as a ``datetime``, not a
    string: YAML parses an unquoted ISO timestamp natively. Validation goes
    through ``_parse_dt``, which accepts both, rather than a type check that
    would reject every deployed record.
    """
    action = record.get("action")
    if not isinstance(action, str) or not action.strip():
        return False
    if action.strip() not in KNOWN_ACTIONS:
        return False
    if _parse_dt(record.get("timestamp")) is None:
        return False
    return _platform_value_is_interpretable(record)


def count_ledgers_detailed(
    *,
    platform: str,
    action: str,
    since: datetime,
    until: datetime | None,
    state_root: Path,
    state_dirs: list[Path],
) -> LedgerCount:
    """Scan the ledgers and report both the count and its trustworthiness."""
    count = 0
    unreadable = 0
    scanned = 0
    damaged_records = 0

    # A source directory that does not exist is not an absence of posts, it is
    # an absence of evidence: we are looking in the wrong place. Counting zero
    # from a missing root reports full confidence in a number derived from
    # nothing.
    for missing in (
        [d for d in state_dirs if not d.is_dir()]
        if state_dirs
        else ([] if state_root.is_dir() else [state_root])
    ):
        _eprint(f"social-cli count: state directory does not exist: {missing}")
        unreadable += 1

    if state_dirs:
        found_dirs = [d for d in state_dirs if d.is_dir()]
    else:
        found_dirs = [p for p in state_root.glob("social-cli-*") if p.is_dir()] if state_root.is_dir() else []

    for path in _ledger_files(platform, state_root, state_dirs):
        scanned += 1
        data, damaged = _load_yaml(path)
        if damaged:
            unreadable += 1
        for record in _records(data):
            if not _record_is_interpretable(record):
                _eprint(
                    f"social-cli count: unreadable record in {path}: "
                    "missing or uninterpretable action/timestamp/platform"
                )
                damaged_records += 1
                continue
            record_action = str(record.get("action") or "")
            if not _action_matches(record_action, action):
                continue
            if _is_true(record.get("dryRun", False)):
                continue
            if not _platform_matches(record, platform):
                continue
            ts = _parse_dt(record.get("timestamp"))
            if ts is None or ts < since:
                continue
            if until is not None and ts >= until:
                continue
            count += 1
    return LedgerCount(
        count=count,
        unreadable=unreadable,
        scanned=scanned,
        damaged_records=damaged_records,
        state_dirs=len(found_dirs),
    )


def count_ledgers(
    *,
    platform: str,
    action: str,
    since: datetime,
    until: datetime | None,
    state_root: Path,
    state_dirs: list[Path],
) -> int:
    """Count matching ledger entries.

    Kept for callers that only need the total. Anything enforcing a cap
    should use :func:`count_ledgers_detailed`, which also reports whether
    any ledger was unreadable.
    """
    return count_ledgers_detailed(
        platform=platform,
        action=action,
        since=since,
        until=until,
        state_root=state_root,
        state_dirs=state_dirs,
    ).count


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Count social-cli sent ledger entries for a platform.",
    )
    p.add_argument("--platform", "-p", required=True)
    p.add_argument(
        "--action",
        default="post",
        help=(
            "Action class to count. Default 'post' means post-creating "
            "ledger actions: post, reply, and thread."
        ),
    )
    p.add_argument(
        "--since",
        default="today",
        help="Inclusive UTC lower bound: 'today', YYYY-MM-DD, or ISO datetime.",
    )
    p.add_argument(
        "--until",
        help="Exclusive UTC upper bound: YYYY-MM-DD or ISO datetime.",
    )
    p.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="Pollers state root. Default: $STATE_DIR parent, $MIMIR_HOME/state/pollers, or ./state/pollers.",
    )
    p.add_argument(
        "--state-dir",
        action="append",
        type=Path,
        default=[],
        help="Specific poller state dir to scan. Repeatable.",
    )
    p.add_argument("--json", action="store_true", help="Emit compact JSON.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    since = _parse_dt(args.since)
    if since is None:
        _eprint(f"social-cli count: invalid --since value: {args.since}")
        return 2
    until = _parse_dt(args.until) if args.until else None
    if args.until and until is None:
        _eprint(f"social-cli count: invalid --until value: {args.until}")
        return 2
    if not args.until and str(args.since).strip().lower() == "today":
        until = since + timedelta(days=1)

    state_root = (args.state_root or _default_state_root()).expanduser()
    state_dirs = [p.expanduser() for p in args.state_dir]
    result = count_ledgers_detailed(
        platform=args.platform,
        action=args.action,
        since=since,
        until=until,
        state_root=state_root,
        state_dirs=state_dirs,
    )
    total = result.count
    if args.json:
        print(json.dumps({
            "count": total,
            "unreadable": result.unreadable,
            "scanned": result.scanned,
            "damagedRecords": result.damaged_records,
            "stateDirs": result.state_dirs,
            "platform": args.platform,
            "action": args.action,
            "since": since.isoformat(),
            "until": until.isoformat() if until else None,
        }, separators=(",", ":")))
    else:
        print(total)
    if result.is_floor:
        # The count is a floor, so exit non-zero rather than let a caller
        # spend the difference as headroom. This is reported through the
        # status code as well as --json so that a consumer which reads only
        # stdout still cannot mistake a partial count for a complete one.
        _eprint(
            f"social-cli count: {result.unreadable} source(s) unreadable and "
            f"{result.damaged_records} record(s) uninterpretable; "
            f"{total} is a floor, not a count"
        )
        return EXIT_LEDGER_UNREADABLE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
