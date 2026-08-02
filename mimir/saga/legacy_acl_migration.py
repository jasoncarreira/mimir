"""Copy-only migration for attributable historical SAGA operator rows."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


ELIGIBLE_SOURCE_TYPES = ("conversation", "agent_authored")


def _cutover(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("cutover must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("cutover must include a timezone")
    return parsed.isoformat()


def migrate_operator_rows(
    conn: sqlite3.Connection,
    *,
    cutover: str,
    channel_owners: dict[str, str],
    apply: bool = False,
) -> dict[str, Any]:
    """Privatize attributable pre-cutover raws; leave every ambiguity narrow."""

    cutoff = _cutover(cutover)
    mappings = {
        channel: owner
        for channel, owner in channel_owners.items()
        if isinstance(channel, str) and channel and isinstance(owner, str) and owner
    }
    if len(mappings) != len(channel_owners):
        raise ValueError("channel-owner mappings must contain non-empty strings")
    if any(owner in {"legacy_admin", "service", "system"} for owner in mappings.values()):
        raise ValueError("channel-owner mappings cannot target reserved sentinel principals")

    eligible_by_channel: dict[str, int] = {}
    changed_by_channel: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
    try:
        for channel, owner in sorted(mappings.items()):
            params = (cutoff, channel, *ELIGIBLE_SOURCE_TYPES)
            where = (
                "owner_principal = 'legacy_admin' AND visibility = 'legacy_admin' "
                "AND tombstoned = 0 AND memory_type = 'raw' AND created_at < ? "
                "AND origin_channel = ? AND source_type IN (?, ?)"
            )
            eligible = conn.execute(
                f"SELECT COUNT(*) FROM atoms WHERE {where}", params
            ).fetchone()[0]
            eligible_by_channel[channel] = eligible
            changed = 0
            if apply and eligible:
                cursor = conn.execute(
                    f"UPDATE atoms SET owner_principal = ?, visibility = 'private' WHERE {where}",
                    (owner, *params),
                )
                changed = cursor.rowcount
            changed_by_channel[channel] = changed
        if apply:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise

    return {
        "mode": "apply" if apply else "dry_run",
        "cutover_timestamp": cutoff,
        "eligible": sum(eligible_by_channel.values()),
        "changed": sum(changed_by_channel.values()),
        "eligible_by_channel": eligible_by_channel,
        "changed_by_channel": changed_by_channel,
        "rule": {
            "memory_type": "raw",
            "source_types": list(ELIGIBLE_SOURCE_TYPES),
            "current_acl": "legacy_admin/legacy_admin",
            "new_visibility": "private",
            "owner_source": "operator-supplied exact origin_channel mapping",
        },
    }


def migrate_copy(
    *,
    source: Path,
    output: Path | None,
    cutover: str,
    channel_owners: dict[str, str],
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SAGA database not found: {source}")
    if output is None:
        conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            return migrate_operator_rows(
                conn, cutover=cutover, channel_owners=channel_owners, apply=False,
            )
        finally:
            conn.close()

    output = output.expanduser().resolve()
    if output == source:
        raise ValueError("output must differ from source; live in-place migration is forbidden")
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn = sqlite3.connect(output)
    try:
        # SQLite backup captures a transactionally consistent view including
        # committed WAL pages; copying only the main file can lose both.
        source_conn.backup(conn)
        return migrate_operator_rows(
            conn, cutover=cutover, channel_owners=channel_owners, apply=True,
        )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        conn.close()
        source_conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate attributable legacy SAGA rows on a copy")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="New copy to modify; omitted for dry-run")
    parser.add_argument("--cutover", required=True)
    parser.add_argument(
        "--channel-owner", action="append", default=[], metavar="CHANNEL=PRINCIPAL",
    )
    args = parser.parse_args(argv)
    try:
        mappings = dict(item.split("=", 1) for item in args.channel_owner)
        report = migrate_copy(
            source=args.source,
            output=args.output,
            cutover=args.cutover,
            channel_owners=mappings,
        )
    except (FileExistsError, FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
