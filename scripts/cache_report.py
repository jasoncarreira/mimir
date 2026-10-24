"""Report cache-token shares from a home's retained turns.jsonl records."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values


@dataclass
class Bucket:
    turns: int = 0
    measured: int = 0
    reads: int = 0
    writes: int = 0
    uncached: int = 0
    inconsistent: int = 0

    def add(self, usage: object, semantics: str = "inclusive") -> None:
        self.turns += 1
        if not isinstance(usage, dict):
            return
        fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        values = [usage.get(field) for field in fields]
        # Unknown cache fields cannot be safely interpreted as zero cache use.
        if any(type(value) is not int or value < 0 for value in values):
            return
        prompt, reads, writes = values
        uncached = prompt - reads - writes if semantics == "inclusive" else prompt
        if uncached < 0:
            self.inconsistent += 1
            return
        self.measured += 1
        self.uncached += uncached
        self.reads += reads
        self.writes += writes


def session_kind(session_id: object) -> str:
    if not isinstance(session_id, str) or not session_id:
        return "unknown"
    if session_id.startswith("poller:"):
        return "poller"
    if session_id.startswith("scheduler:"):
        return "scheduler"
    return "channel"


def _date(text: str) -> date:
    try:
        result = date.fromisoformat(text)
        if result.isoformat() != text:
            raise ValueError
        return result
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _row(label: str, bucket: Bucket) -> str:
    total = bucket.reads + bucket.writes + bucket.uncached
    def part(count: int) -> str:
        return f"{count} ({count / total:.1%})" if total else f"{count} (n/a)"
    return (f"{label}: turns={bucket.turns} measured={bucket.measured} "
            f"read={part(bucket.reads)} write={part(bucket.writes)} "
            f"uncached={part(bucket.uncached)}")


def report(home: Path, start: date, end: date, *, input_semantics: str = "inclusive") -> str:
    """Stream retained turns with an explicit, window-wide input convention."""
    if input_semantics not in {"inclusive", "exclusive"}:
        raise ValueError("input semantics must be inclusive or exclusive")
    log = home / "logs" / "turns.jsonl"
    if not home.is_dir():
        raise ValueError(f"home is not a directory: {home}")
    if not log.is_file():
        raise ValueError(f"no turn log at {log}")
    model = "unknown (no MIMIR_MODEL_SPEC in home/.env)"
    env_file = home / ".env"
    if env_file.is_file():
        config = dotenv_values(env_file, interpolate=False)
        if config.get("MIMIR_MODEL_SPEC"):
            model = config["MIMIR_MODEL_SPEC"]

    total = Bucket()
    by_day: dict[str, Bucket] = defaultdict(Bucket)
    by_trigger: dict[str, Bucket] = defaultdict(Bucket)
    by_kind: dict[str, Bucket] = defaultdict(Bucket)
    skipped = 0
    with log.open(encoding="utf-8") as lines:
        for line in lines:
            try:
                turn = json.loads(line)
                if not isinstance(turn, dict):
                    raise ValueError("not an object")
                ts = datetime.fromisoformat(turn["ts"].replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    raise ValueError("timestamp lacks timezone")
                day = ts.astimezone(timezone.utc).date()
            except (ValueError, TypeError, KeyError, AttributeError):
                skipped += 1
                continue
            if start <= day <= end:
                usage = turn.get("usage")
                total.add(usage, input_semantics)
                by_day[day.isoformat()].add(usage, input_semantics)
                trigger = turn.get("trigger")
                by_trigger[trigger if isinstance(trigger, str) and trigger else "unknown"].add(usage, input_semantics)
                by_kind[session_kind(turn.get("session_id"))].add(usage, input_semantics)

    rows = [f"Cache-hit report (UTC {start} through {end})",
            f"Configured agent model (current home/.env): {model}",
            "Per-turn model attribution unavailable in turns.jsonl.",
            f"Input token semantics: {input_semantics} (selected for the entire window).",
            "Shares use read + write + uncached input tokens from measured turns only.",
            _row("Total", total)]
    for title, buckets in (("Day", by_day), ("Trigger", by_trigger), ("Session kind", by_kind)):
        rows.append(f"{title}:")
        rows.extend(_row(f"  {key}", buckets[key]) for key in sorted(buckets))
    rows.append(f"Skipped malformed/undated lines: {skipped}")
    unmeasured = total.turns - total.measured - total.inconsistent
    if unmeasured:
        rows.append(f"Turns without complete cache usage: {unmeasured}")
    if total.inconsistent:
        rows.append(f"Turns excluded because cache details exceed total input: {total.inconsistent}")
    return "\n".join(rows) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("home", type=Path, help="existing MIMIR_HOME (read only)")
    today = datetime.now(timezone.utc).date()
    parser.add_argument("--from-date", type=_date, default=today - timedelta(days=6))
    parser.add_argument("--to-date", type=_date, default=today)
    parser.add_argument(
        "--input-semantics", choices=("inclusive", "exclusive"), default="inclusive",
        help="inclusive LangChain totals (default), or known Anthropic-raw exclusive counts; "
             "applies to the entire window, never inferred from the current model",
    )
    args = parser.parse_args()
    if args.from_date > args.to_date:
        parser.error("--from-date must not be after --to-date")
    try:
        print(report(args.home, args.from_date, args.to_date,
                     input_semantics=args.input_semantics), end="")
    except (ValueError, OSError, UnicodeError) as exc:
        parser.exit(1, f"cache report: {exc}\n")


if __name__ == "__main__":
    main()
