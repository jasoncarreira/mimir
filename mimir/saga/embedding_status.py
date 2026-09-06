"""Read-only embedding health, using the same population as vector builds."""

from __future__ import annotations

import sqlite3
from typing import Any


def embedding_status(conn: sqlite3.Connection, dimension: int | None) -> dict[str, Any]:
    """Return declared dimension distributions and vector-build exclusions.

    ``None`` means the provider dimension is unknown, not zero exclusions.
    Blob lengths are checked in SQL so health checks never load vector data.
    Missing atom embedding rows and unembedded sessions are not candidates.
    """
    result: dict[str, Any] = {"dimension": dimension}
    for kind, source in (
        ("atoms", """SELECT e.dim AS dim, e.vec AS vec FROM atoms a
                     JOIN embeddings e ON e.atom_id = a.id WHERE a.tombstoned = 0"""),
        ("sessions", """SELECT embedding_dim AS dim, embedding AS vec FROM sessions
                        WHERE embedding IS NOT NULL"""),
    ):
        rows = conn.execute(
            f"""SELECT dim, COUNT(*),
                       SUM(CASE WHEN dim IS NULL OR dim != ? OR vec IS NULL
                                     OR length(vec) != ? THEN 1 ELSE 0 END)
                FROM ({source}) GROUP BY dim ORDER BY dim""",
            (dimension, dimension * 4 if dimension is not None else None),
        ).fetchall()
        result[kind] = {
            "dimensions": {str(dim) if dim is not None else "unknown": count
                           for dim, count, _ in rows},
            "excluded_from_recall": (
                sum(excluded for _, _, excluded in rows) if dimension is not None else None
            ),
        }
    result["status"] = (
        "unknown" if dimension is None else
        "degraded" if any(result[k]["excluded_from_recall"] for k in ("atoms", "sessions"))
        else "healthy"
    )
    return result
