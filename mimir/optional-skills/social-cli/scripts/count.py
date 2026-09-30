#!/usr/bin/env python3
"""Count published social-cli posts from sent ledgers and thread archives.

Ledgers identify successful dispatches; archived outboxes supply the post
length for threads. No secondary counter file can drift the daily count.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

POST_CREATING_ACTIONS = {"post", "reply", "thread"}
EXIT_CAP_UNKNOWN = 3


class UnresolvedThreadError(Exception):
    """A published thread has no unambiguous, readable archived outbox."""


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


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit("PyYAML is required for social-cli count") from exc
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _eprint(f"social-cli count: could not read {path}: {exc}")
        return None
    if not text.strip():
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        _eprint(f"social-cli count: skipping malformed ledger {path}: {exc}")
        return None


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
    for key in ("entries", "ledger", "sent", "items", "results", "dispatch"):
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
        for ledger_dir in (directory, directory / ".social-cli" / "state"):
            files.extend(sorted(ledger_dir.glob("sent_ledger*.yaml")))
    return files


def _thread_posts(path: Path, record: dict[str, Any], platform: str) -> int:
    name = str(record.get("createdId") or record.get("key") or record.get("textHash") or record.get("timestamp"))
    dispatch_time = _parse_dt(record.get("dispatchTimestamp") or record.get("timestamp"))
    if dispatch_time is None:
        raise UnresolvedThreadError(name)
    matches: list[int] = []
    archive_dirs = [path.parent / "outbox_archive"]
    if path.parent.name == "state" and path.parent.parent.name == ".social-cli":
        archive_dirs.append(path.parent.parent.parent / "outbox_archive")
    for archive in (p for directory in archive_dirs
                    for p in directory.glob(f"*_outbox-{platform}.yaml")):
        stamp = archive.name.split("_outbox-", 1)[0]
        try:
            archived_at = datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%S-%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        delta = (dispatch_time - archived_at).total_seconds()
        if not -5 <= delta <= 300:
            continue
        data = _load_yaml(archive)
        actions = data.get("dispatch") if isinstance(data, dict) else data
        if not isinstance(actions, list):
            continue
        threads = [item["thread"] for item in actions
                   if isinstance(item, dict) and isinstance(item.get("thread"), dict)
                   and _platform_matches(item["thread"], platform)]
        # A ledger has no per-post IDs or reliable index into a multi-thread
        # outbox. Never infer a size from an ambiguous archive.
        if len(threads) == 1:
            posts = threads[0].get("posts")
            if isinstance(posts, list) and posts:
                matches.append(len(posts))
    if len(matches) != 1:
        raise UnresolvedThreadError(name)
    return matches[0]


def count_ledgers(
    *,
    platform: str,
    action: str,
    since: datetime,
    until: datetime | None,
    state_root: Path,
    state_dirs: list[Path],
) -> int:
    count = 0
    seen: set[str] = set()
    for path in _ledger_files(platform, state_root, state_dirs):
        for record in _records(_load_yaml(path)):
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
            identity = str(record.get("createdId") or "")
            key = f"id:{identity}" if identity else (
                f"fallback:{record.get('key', '')}|{record.get('textHash', '')}|{record.get('timestamp', '')}"
            )
            if key in seen:
                continue
            posts = _thread_posts(path, record, platform) if record_action == "thread" else 1
            seen.add(key)
            count += posts
    return count


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Count published social-cli posts for a platform.",
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
    try:
        total = count_ledgers(
            platform=args.platform,
            action=args.action,
            since=since,
            until=until,
            state_root=state_root,
            state_dirs=state_dirs,
        )
    except UnresolvedThreadError as exc:
        _eprint(f"CAP UNKNOWN: thread {exc} has no readable matching archived outbox; treat the daily cap as reached")
        return EXIT_CAP_UNKNOWN
    if args.json:
        print(json.dumps({
            "count": total,
            "platform": args.platform,
            "action": args.action,
            "since": since.isoformat(),
            "until": until.isoformat() if until else None,
        }, separators=(",", ":")))
    else:
        print(total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
