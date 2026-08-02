"""Read-only SAGA strict-scope impact analysis over privileged recall traces.

The replay input deliberately contains atom IDs, not atom content.  It is
produced while replaying representative queries against a store copy with the
admin view: ``pathways`` records candidates before RRF and ``results`` records
the final permissive recall result after scoring and rank fusion.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .ownership import RESERVED_SENTINEL_PRINCIPALS


_GROUP_FIELDS = ("surface", "caller_kind", "trigger")


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _can_read(row: sqlite3.Row, scope: dict[str, Any]) -> bool:
    if scope.get("is_admin") or scope.get("is_platform_service"):
        return True
    if row["visibility"] == "public":
        return True

    owner = scope.get("principal")
    if scope.get("is_service") and scope.get("service_canonical"):
        owner = f"service:{scope['service_canonical']}"
    if owner and owner not in RESERVED_SENTINEL_PRINCIPALS:
        if row["owner_principal"] == owner:
            return True

    domains = scope.get("readable_domains") or []
    return bool(scope.get("is_service") and row["origin_domain"] in domains)


def _load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on trace line {line_number}: {exc}") from exc
            for field in (*_GROUP_FIELDS, "ts", "scope", "pathways", "results"):
                if field not in event:
                    raise ValueError(f"trace line {line_number} is missing {field!r}")
            _parse_timestamp(event["ts"], field=f"trace line {line_number} ts")
            if not isinstance(event["scope"], dict):
                raise ValueError(f"trace line {line_number} scope must be an object")
            if not isinstance(event["pathways"], dict):
                raise ValueError(f"trace line {line_number} pathways must be an object")
            if not isinstance(event["results"], list):
                raise ValueError(f"trace line {line_number} results must be a list")
            events.append(event)
    if not events:
        raise ValueError("trace contains no recall events")
    return events


def _atom_ids(events: Iterable[dict[str, Any]]) -> set[str]:
    atom_ids: set[str] = set()
    for event in events:
        atom_ids.update(aid for aid in event["results"] if isinstance(aid, str))
        for candidates in event["pathways"].values():
            if not isinstance(candidates, list):
                raise ValueError("each pathways value must be a list of atom IDs")
            atom_ids.update(aid for aid in candidates if isinstance(aid, str))
    return atom_ids


def analyze_replay(
    conn: sqlite3.Connection,
    events: list[dict[str, Any]],
    *,
    cutover: datetime,
) -> dict[str, Any]:
    """Compare privileged candidates/results with each event's strict scope."""

    conn.row_factory = sqlite3.Row
    ids = _atom_ids(events)
    atoms: dict[str, sqlite3.Row] = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        atoms = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, owner_principal, visibility, origin_domain, created_at "
                f"FROM atoms WHERE id IN ({placeholders}) AND tombstoned = 0",
                sorted(ids),
            )
        }

    totals: defaultdict[str, int] = defaultdict(int)
    grouped: dict[tuple[str, str, str], defaultdict[str, int]] = {}
    legacy_exclusions: defaultdict[str, int] = defaultdict(int)
    missing_ids: set[str] = set()
    timestamps: list[datetime] = []

    for event in events:
        timestamp = _parse_timestamp(event["ts"], field="event ts")
        timestamps.append(timestamp)
        key = tuple(str(event[field]) for field in _GROUP_FIELDS)
        bucket = grouped.setdefault(key, defaultdict(int))
        scope = event["scope"]

        candidate_occurrences = 0
        excluded_occurrences = 0
        candidate_ids: set[str] = set()
        excluded_ids: set[str] = set()
        for candidates in event["pathways"].values():
            candidate_occurrences += len(candidates)
            for atom_id in candidates:
                candidate_ids.add(atom_id)
                row = atoms.get(atom_id)
                if row is None:
                    missing_ids.add(str(atom_id))
                elif not _can_read(row, scope):
                    excluded_occurrences += 1
                    excluded_ids.add(atom_id)

        result_ids = list(dict.fromkeys(event["results"]))
        excluded_results: list[str] = []
        for atom_id in result_ids:
            row = atoms.get(atom_id)
            if row is None:
                missing_ids.add(str(atom_id))
            elif not _can_read(row, scope):
                excluded_results.append(atom_id)

        metrics = {
            "events": 1,
            "pre_fusion_candidate_occurrences": candidate_occurrences,
            "pre_fusion_excluded_occurrences": excluded_occurrences,
            "pre_fusion_unique_candidates": len(candidate_ids),
            "pre_fusion_unique_excluded": len(excluded_ids),
            "post_fusion_results": len(result_ids),
            "post_fusion_excluded_results": len(excluded_results),
        }
        for name, value in metrics.items():
            totals[name] += value
            bucket[name] += value

        for stage, excluded in (
            ("pre_fusion", excluded_ids),
            ("post_fusion", excluded_results),
        ):
            for atom_id in excluded:
                row = atoms[atom_id]
                if row["owner_principal"] != "legacy_admin":
                    continue
                created = _parse_timestamp(row["created_at"], field=f"atom {atom_id} created_at")
                age = "historical" if created < cutover else "post_cutover"
                legacy_exclusions[f"{stage}_{age}"] += 1

    corpus_legacy = {"historical": 0, "post_cutover": 0}
    for row in conn.execute(
        "SELECT created_at, COUNT(*) AS count FROM atoms "
        "WHERE tombstoned = 0 AND owner_principal = 'legacy_admin' "
        "GROUP BY created_at"
    ):
        created = _parse_timestamp(row["created_at"], field="atom created_at")
        corpus_legacy["historical" if created < cutover else "post_cutover"] += row["count"]

    breakdown = []
    for key, metrics in sorted(grouped.items()):
        breakdown.append({
            **dict(zip(_GROUP_FIELDS, key)),
            **dict(metrics),
        })

    return {
        "schema_version": 1,
        "period": {
            "start": min(timestamps).isoformat(),
            "end": max(timestamps).isoformat(),
            "events": len(events),
        },
        "cutover_timestamp": cutover.isoformat(),
        "totals": dict(totals),
        "breakdown": breakdown,
        "legacy_admin_corpus": corpus_legacy,
        "legacy_admin_exclusions": {
            name: legacy_exclusions[name]
            for name in (
                "pre_fusion_historical",
                "pre_fusion_post_cutover",
                "post_fusion_historical",
                "post_fusion_post_cutover",
            )
        },
        "missing_trace_atom_ids": sorted(missing_ids),
        "notes": [
            "Pre-fusion counts are candidate losses before RRF; one atom can occur in multiple pathways.",
            "Post-fusion counts are hidden IDs in the permissive final results after all production ranking.",
            "Post-cutover legacy_admin rows indicate provenance stamping is still producing unclassified data.",
        ],
    }


def analyze_paths(*, db: Path, trace: Path, cutover: str) -> dict[str, Any]:
    resolved = db.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SAGA database not found: {resolved}")
    events = _load_events(trace.expanduser().resolve())
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        return analyze_replay(
            conn,
            events,
            cutover=_parse_timestamp(cutover, field="cutover"),
        )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure SAGA strict-scope recall impact")
    parser.add_argument("--db", type=Path, required=True, help="Read-only SAGA database copy")
    parser.add_argument("--trace", type=Path, required=True, help="Privileged replay JSONL")
    parser.add_argument("--cutover", required=True, help="Provenance-fix ISO-8601 timestamp")
    args = parser.parse_args(argv)
    try:
        report = analyze_paths(db=args.db, trace=args.trace, cutover=args.cutover)
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
