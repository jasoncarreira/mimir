"""Read-only inventory for SAGA atom ACL writer-path analysis.

The atoms schema does not persist a writer-path discriminator.  This report
therefore keeps definitive classes (service ownership and derived observations
with evidence relations) separate from raw legacy rows, which may have arrived
through a direct facade call or the legacy importer.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


CLASSIFICATION_SQL = """
WITH classified AS (
    SELECT
        a.*,
        CASE
            WHEN a.owner_principal LIKE 'service:%' THEN 'service_owned'
            WHEN a.owner_principal = 'legacy_admin'
                 AND a.memory_type = 'observation'
                 AND EXISTS (
                     SELECT 1
                     FROM atom_relations r
                     WHERE r.source_id = a.id
                       AND r.relation_type = 'evidenced_by'
                 ) THEN 'derived_legacy_inheritance'
            WHEN a.owner_principal = 'legacy_admin'
                 AND a.memory_type = 'raw'
                 AND (
                     a.source_type IN ('agent_authored', 'skill_learning')
                     OR a.origin_channel IS NOT NULL
                     OR a.origin_domain IS NOT NULL
                     OR a.provenance != '{}'
                 ) THEN 'direct_write_missing_owner'
            WHEN a.owner_principal = 'legacy_admin'
                 AND a.memory_type = 'raw' THEN 'legacy_unattributed'
            WHEN a.owner_principal != 'legacy_admin' THEN 'other_owned'
            ELSE 'unclassified'
        END AS acl_class
    FROM atoms a
    WHERE (? OR a.tombstoned = 0)
)
SELECT acl_class, COUNT(*) AS atom_count
FROM classified
GROUP BY acl_class
ORDER BY acl_class
"""


DIRECT_LEGACY_DETAIL_SQL = """
SELECT
    source_type,
    COALESCE(origin_channel, '<null>') AS origin_channel,
    COALESCE(origin_domain, '<null>') AS origin_domain,
    CASE WHEN provenance = '{}' THEN 'empty' ELSE 'present' END AS provenance,
    COUNT(*) AS atom_count
FROM atoms
WHERE (? OR tombstoned = 0)
  AND owner_principal = 'legacy_admin'
  AND memory_type = 'raw'
GROUP BY source_type, origin_channel, origin_domain,
         CASE WHEN provenance = '{}' THEN 'empty' ELSE 'present' END
ORDER BY atom_count DESC, source_type, origin_channel, origin_domain
"""


DERIVED_LEGACY_DETAIL_SQL = """
SELECT
    a.source_type,
    COUNT(DISTINCT a.id) AS atom_count,
    COUNT(DISTINCT r.target_id) AS evidence_atoms
FROM atoms a
JOIN atom_relations r
  ON r.source_id = a.id AND r.relation_type = 'evidenced_by'
WHERE (? OR a.tombstoned = 0)
  AND a.owner_principal = 'legacy_admin'
  AND a.memory_type = 'observation'
GROUP BY a.source_type
ORDER BY atom_count DESC, a.source_type
"""


def inventory_acl(conn: sqlite3.Connection, *, include_tombstoned: bool = False) -> dict[str, Any]:
    """Return evidence-based ACL classes without changing the store."""

    include = 1 if include_tombstoned else 0
    counts = {row[0]: row[1] for row in conn.execute(CLASSIFICATION_SQL, (include,))}
    for name in (
        "direct_write_missing_owner",
        "derived_legacy_inheritance",
        "service_owned",
        "other_owned",
        "legacy_unattributed",
        "unclassified",
    ):
        counts.setdefault(name, 0)

    direct_detail = [
        {
            "source_type": row[0],
            "origin_channel": row[1],
            "origin_domain": row[2],
            "provenance": row[3],
            "count": row[4],
        }
        for row in conn.execute(DIRECT_LEGACY_DETAIL_SQL, (include,))
    ]
    derived_detail = [
        {"source_type": row[0], "count": row[1], "distinct_evidence_atoms": row[2]}
        for row in conn.execute(DERIVED_LEGACY_DETAIL_SQL, (include,))
    ]
    return {
        "scope": "all" if include_tombstoned else "live",
        "total": sum(counts.values()),
        "counts": counts,
        "direct_legacy_detail": direct_detail,
        "derived_legacy_detail": derived_detail,
        "limitations": (
            "Legacy-unattributed raw rows are not proof of a defective direct writer: "
            "atoms do not persist writer path, and the legacy importer intentionally "
            "creates the same row shape. The direct-write class requires a direct-tool "
            "source type or persisted channel/domain/provenance evidence."
        ),
    }


def inventory_path(path: Path, *, include_tombstoned: bool = False) -> dict[str, Any]:
    """Open an existing SQLite store read-only and inventory it."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SAGA database not found: {resolved}")
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        return inventory_acl(conn, include_tombstoned=include_tombstoned)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory SAGA atom ACL writer classes")
    parser.add_argument("--db", type=Path, required=True, help="Path to saga.db")
    parser.add_argument("--include-tombstoned", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = inventory_path(args.db, include_tombstoned=args.include_tombstoned)
    except (FileNotFoundError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
