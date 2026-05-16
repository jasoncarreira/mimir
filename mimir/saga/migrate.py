"""Importer: saga.db (or MSAM-era snapshot) → mimir.saga.db.

Bootstrap policy: port saga's ``access_log`` to the new
``access_events`` table preserving timestamps. This carries forward
saga's "this atom matters more than that one" rank instead of
resetting to a clean slate.

Source-schema variants handled:
- Recent saga (with ``memory_type`` column, ``access_log`` table)
- MSAM-era snapshots (no ``memory_type``; ``access_log`` schema same)
- MSAM's ``state='dormant'`` atoms — kept (state column doesn't exist
  in the new schema; dormancy was the bug we're fixing)
- ``state='tombstone'`` atoms — kept as ``tombstoned=1`` rows so the
  forgetting log stays auditable; they won't be retrievable

What gets dropped:
- ``stability`` column — subsumed by access_events
- ``retrievability`` column — computed on demand
- ``state`` column — replaced by ``tombstoned`` boolean
- Pre-consolidation observations from MSAM — re-synthesized by the
  next ``consolidate()`` pass against the current embedding space

What's preserved alongside atoms:
- Embeddings (from ``atoms.embedding`` blob → ``embeddings`` table)
- atom_relations (evidenced_by, consolidated_into, etc.)
- atom_topics
- triples (saga.db only — MSAM didn't have this table)

After the import: ``SagaStore.rebuild_index()`` rebuilds the FAISS
index from the populated embeddings. The first ``query()`` will also
trigger a lazy build if rebuild_index wasn't called.

Usage as a library:
    >>> from mimir.saga.migrate import migrate
    >>> stats = migrate(source=Path("saga.db"), dest=Path("mimir.saga.db"))

Usage from CLI:
    $ mimir migrate-memory --source saga.db --dest mimir.saga.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


# Columns from the source atoms table that carry over unchanged.
ATOMS_SHARED_COLUMNS = (
    "id", "content", "content_hash", "created_at",
    "stream", "profile", "arousal", "valence",
    "encoding_confidence", "topics", "source_type", "metadata",
    "agent_id", "session_id", "is_pinned",
)

# Source-event weight map. ``access_log`` doesn't distinguish; we use
# ``contributed`` flag when available (saga has it; MSAM may not).
# Falls back to plain retrieval weight if absent.
SOURCE_WEIGHTS = {
    "retrieval": 1.0,
    "feedback_positive": 2.0,
    "store": 1.0,
    "consolidation": 0.5,
}


def _identify_msam_observations(src: sqlite3.Connection) -> set[str]:
    """MSAM-era observations (consolidation targets) — drop these.
    ``mimir.saga.reflect`` will re-synthesize against the current
    provider's embeddings post-migration."""
    try:
        rows = src.execute(
            "SELECT DISTINCT target_id FROM atom_relations "
            "WHERE relation_type='consolidated_into'"
        ).fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        # atom_relations missing → no consolidation history to drop
        return set()


def _migrate_atoms(
    src: sqlite3.Connection, dst: sqlite3.Connection,
    drop_ids: set[str],
) -> dict:
    """Insert atoms into the new schema. Drop columns that don't exist
    in the new model (stability, retrievability, state). Preserve
    everything else."""
    cols = ", ".join(ATOMS_SHARED_COLUMNS)
    placeholders = ", ".join(["?"] * len(ATOMS_SHARED_COLUMNS))
    # State='tombstone' maps to tombstoned=1; everything else to 0.
    insert_sql = (
        f"INSERT INTO atoms ({cols}, memory_type, tombstoned, "
        f"tombstoned_at, tombstoned_reason) "
        f"VALUES ({placeholders}, ?, ?, ?, ?)"
    )

    select_cols = list(ATOMS_SHARED_COLUMNS)
    # Add columns we transform: state (for tombstone mapping),
    # memory_type (for atoms that have it in source schema).
    try:
        src.execute("SELECT memory_type FROM atoms LIMIT 1")
        has_memory_type = True
    except sqlite3.OperationalError:
        has_memory_type = False

    select_cols.append("state")
    if has_memory_type:
        select_cols.append("memory_type")
    src_sql = f"SELECT {', '.join(select_cols)} FROM atoms"

    counts = {"migrated": 0, "dropped_obs": 0, "tombstoned": 0}
    for row in src.execute(src_sql):
        row = dict(zip(select_cols, row))
        atom_id = row["id"]
        if atom_id in drop_ids:
            counts["dropped_obs"] += 1
            continue
        shared = tuple(row[c] for c in ATOMS_SHARED_COLUMNS)
        mem_type = row.get("memory_type") if has_memory_type else "raw"
        if mem_type is None:
            mem_type = "raw"
        if row["state"] == "tombstone":
            tombstoned = 1
            tombstoned_at = row.get("created_at") or _utcnow()
            tombstoned_reason = "migrated_from_saga"
            counts["tombstoned"] += 1
        else:
            tombstoned = 0
            tombstoned_at = None
            tombstoned_reason = None
        dst.execute(insert_sql, shared + (
            mem_type, tombstoned, tombstoned_at, tombstoned_reason,
        ))
        counts["migrated"] += 1
    dst.commit()
    return counts


def _migrate_access_log(
    src: sqlite3.Connection, dst: sqlite3.Connection,
) -> int:
    """legacy ``saga.access_log`` (the workspace-member ``saga/`` package,
    pre-rename) → ``mimir.saga.access_events`` (the new in-process
    package after the ``mimir.memory`` → ``mimir.saga`` rename).

    The legacy table has (atom_id, accessed_at, activation_score,
    retrieval_mode, session_id, contributed). We map:
    - accessed_at → ts
    - retrieval_mode → metadata.mode
    - contributed=1 → source='feedback_positive' (treat as endorsement)
    - contributed=0 → source='retrieval' (plain access)
    - contributed=-1 / NULL → source='retrieval' (no signal yet)
    - session_id → session_id
    """
    try:
        rows = src.execute(
            "SELECT atom_id, accessed_at, retrieval_mode, "
            "session_id, contributed FROM access_log ORDER BY accessed_at"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0

    inserted = 0
    batch = []
    for atom_id, ts, mode, sid, contributed in rows:
        if contributed == 1:
            source = "feedback_positive"
        else:
            source = "retrieval"
        weight = SOURCE_WEIGHTS.get(source, 1.0)
        metadata = json.dumps({"mode": mode} if mode else {})
        batch.append((atom_id, ts, source, weight, sid, metadata))
        if len(batch) >= 1000:
            dst.executemany(
                "INSERT INTO access_events (atom_id, ts, source, weight, "
                "session_id, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                batch,
            )
            inserted += len(batch)
            batch.clear()
    if batch:
        dst.executemany(
            "INSERT INTO access_events (atom_id, ts, source, weight, "
            "session_id, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            batch,
        )
        inserted += len(batch)
    dst.commit()
    return inserted


def _seed_store_events(dst: sqlite3.Connection) -> int:
    """Every atom needs at least one access event for activation to
    be non -inf. For migrated atoms whose access_log was empty (no
    historical retrievals), synthesize a single ``store`` event at
    the atom's created_at timestamp. Idempotent — skip if the atom
    already has any access_events row.
    """
    rows = dst.execute("""
        SELECT a.id, a.created_at
        FROM atoms a
        LEFT JOIN access_events e ON a.id = e.atom_id
        WHERE e.atom_id IS NULL AND a.tombstoned = 0
    """).fetchall()
    if not rows:
        return 0
    dst.executemany(
        "INSERT INTO access_events (atom_id, ts, source, weight) "
        "VALUES (?, ?, 'store', 1.0)",
        [(aid, ts) for aid, ts in rows],
    )
    dst.commit()
    return len(rows)


def _rebuild_summaries(dst: sqlite3.Connection) -> int:
    """Populate atom_access_summary for every atom that has events.
    Uses activation.rebuild_summary_from_events; emit in batches."""
    # Note: when this runs for real, import activation from
    # mimir.saga. Sketch keeps the implementation inline.
    rows = dst.execute(
        "SELECT atom_id, ts, weight FROM access_events ORDER BY atom_id, ts"
    ).fetchall()
    # Group by atom
    by_atom: dict[str, list[tuple[str, float]]] = {}
    for atom_id, ts, weight in rows:
        by_atom.setdefault(atom_id, []).append((ts, weight))

    # Recent K matches activation.RECENT_K — keep in sync.
    RECENT_K = 10
    inserted = 0
    for atom_id, events in by_atom.items():
        recent = events[-RECENT_K:]
        old = events[:-RECENT_K]
        recent_ts = [t for t, _ in reversed(recent)]  # newest first
        recent_w = [w for _, w in reversed(recent)]
        old_count = len(old)
        old_weight_sum = sum(w for _, w in old)
        old_oldest_ts = old[0][0] if old else None
        dst.execute(
            "INSERT OR REPLACE INTO atom_access_summary "
            "(atom_id, recent_ts_json, recent_weights_json, old_count, "
            "old_weight_sum, old_oldest_ts, last_updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                atom_id, json.dumps(recent_ts), json.dumps(recent_w),
                old_count, old_weight_sum, old_oldest_ts,
                events[-1][0] if events else None,
            ),
        )
        inserted += 1
    dst.commit()
    return inserted


def _migrate_topics(
    src: sqlite3.Connection, dst: sqlite3.Connection,
) -> int:
    kept = {r[0] for r in dst.execute("SELECT id FROM atoms")}
    inserted = 0
    batch = []
    for atom_id, topic in src.execute(
        "SELECT atom_id, topic FROM atom_topics"
    ):
        if atom_id not in kept:
            continue
        batch.append((atom_id, topic))
        if len(batch) >= 1000:
            dst.executemany(
                "INSERT OR IGNORE INTO atom_topics (atom_id, topic) "
                "VALUES (?, ?)", batch,
            )
            inserted += len(batch)
            batch.clear()
    if batch:
        dst.executemany(
            "INSERT OR IGNORE INTO atom_topics (atom_id, topic) "
            "VALUES (?, ?)", batch,
        )
        inserted += len(batch)
    dst.commit()
    return inserted


def _migrate_embeddings(
    src: sqlite3.Connection, dst: sqlite3.Connection,
) -> int:
    """saga.atoms.embedding (blob on the atoms row) → mimir.saga's
    ``embeddings`` table (separate row keyed on atom_id).

    Provider/model/dim are read from the source's ``embedding_meta``
    table if present (recent saga) or from the embedding blob's
    inferred dimension otherwise. Falls back to ``("legacy", "unknown",
    dim_from_blob_size_or_1024)`` when the source has no metadata.
    """
    kept = {r[0] for r in dst.execute("SELECT id FROM atoms")}
    if not kept:
        return 0

    # Try to read provider/model from a sidecar if it exists.
    try:
        meta_rows = src.execute(
            "SELECT atom_id, provider, model, dim FROM embedding_meta"
        ).fetchall()
        meta_by_atom = {r[0]: (r[1], r[2], r[3]) for r in meta_rows}
    except sqlite3.OperationalError:
        meta_by_atom = {}

    # Some sagas have a separate embeddings table already (recent
    # versions). Prefer that when present.
    try:
        embed_rows = src.execute(
            "SELECT atom_id, provider, model, dim, vec FROM embeddings"
        ).fetchall()
        has_embeddings_table = True
    except sqlite3.OperationalError:
        embed_rows = None
        has_embeddings_table = False

    inserted = 0
    batch: list[tuple] = []
    now = _utcnow()

    if has_embeddings_table and embed_rows is not None:
        for atom_id, provider, model, dim, vec in embed_rows:
            if atom_id not in kept or vec is None:
                continue
            batch.append((atom_id, provider, model, dim, vec, now))
            if len(batch) >= 1000:
                dst.executemany(
                    "INSERT OR REPLACE INTO embeddings "
                    "(atom_id, provider, model, dim, vec, embedded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)", batch,
                )
                inserted += len(batch)
                batch.clear()
    else:
        # Read embedding blob from atoms.embedding column.
        for atom_id, blob in src.execute(
            "SELECT id, embedding FROM atoms WHERE embedding IS NOT NULL"
        ):
            if atom_id not in kept or blob is None:
                continue
            provider, model, dim = meta_by_atom.get(
                atom_id, ("legacy", "unknown", len(blob) // 4),
            )
            batch.append((atom_id, provider, model, dim, blob, now))
            if len(batch) >= 1000:
                dst.executemany(
                    "INSERT OR REPLACE INTO embeddings "
                    "(atom_id, provider, model, dim, vec, embedded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)", batch,
                )
                inserted += len(batch)
                batch.clear()

    if batch:
        dst.executemany(
            "INSERT OR REPLACE INTO embeddings "
            "(atom_id, provider, model, dim, vec, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", batch,
        )
        inserted += len(batch)
    dst.commit()
    return inserted


def _migrate_atom_relations(
    src: sqlite3.Connection, dst: sqlite3.Connection,
) -> int:
    """saga.atom_relations → mimir.saga.atom_relations. Filter to
    rows whose both endpoints survived the atom migration (i.e. both
    atom ids still exist in dst.atoms)."""
    kept = {r[0] for r in dst.execute("SELECT id FROM atoms")}
    if not kept:
        return 0
    try:
        rows = src.execute(
            "SELECT source_id, target_id, relation_type, confidence, "
            "created_at, metadata FROM atom_relations"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0

    inserted = 0
    batch: list[tuple] = []
    for source_id, target_id, rel_type, conf, created_at, metadata in rows:
        if source_id not in kept or target_id not in kept:
            continue
        batch.append((
            source_id, target_id, rel_type, conf or 1.0,
            created_at or _utcnow(), metadata or "{}",
        ))
        if len(batch) >= 1000:
            dst.executemany(
                "INSERT OR IGNORE INTO atom_relations "
                "(source_id, target_id, relation_type, confidence, "
                "created_at, metadata) VALUES (?, ?, ?, ?, ?, ?)", batch,
            )
            inserted += len(batch)
            batch.clear()
    if batch:
        dst.executemany(
            "INSERT OR IGNORE INTO atom_relations "
            "(source_id, target_id, relation_type, confidence, "
            "created_at, metadata) VALUES (?, ?, ?, ?, ?, ?)", batch,
        )
        inserted += len(batch)
    dst.commit()
    return inserted


def _migrate_triples(
    src: sqlite3.Connection, dst: sqlite3.Connection,
) -> int:
    """saga.triples → mimir.saga.triples. Skip if source doesn't have
    the triples table (MSAM-era snapshots)."""
    try:
        rows = src.execute(
            "SELECT subject, predicate, object, source_atom_id, "
            "confidence, valid_from, valid_until, state, created_at, "
            "metadata FROM triples"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0

    kept = {r[0] for r in dst.execute("SELECT id FROM atoms")}
    inserted = 0
    batch: list[tuple] = []
    for (subject, predicate, obj, src_aid, conf, vfrom, vuntil,
         state, created_at, metadata) in rows:
        # Skip triples whose source atom didn't survive.
        if src_aid is not None and src_aid not in kept:
            continue
        tombstoned = 1 if state == "tombstone" else 0
        batch.append((
            subject, predicate, obj, src_aid, conf or 1.0,
            vfrom, vuntil, tombstoned, created_at or _utcnow(),
            metadata or "{}",
        ))
        if len(batch) >= 1000:
            dst.executemany(
                "INSERT INTO triples "
                "(subject, predicate, object, source_atom_id, confidence, "
                "valid_from, valid_until, tombstoned, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch,
            )
            inserted += len(batch)
            batch.clear()
    if batch:
        dst.executemany(
            "INSERT INTO triples "
            "(subject, predicate, object, source_atom_id, confidence, "
            "valid_from, valid_until, tombstoned, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch,
        )
        inserted += len(batch)
    dst.commit()
    return inserted


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate(
    *,
    source: Path,
    dest: Path,
    schema: Path | None = None,
    force: bool = False,
    log: Callable[[str], None] = lambda _msg: None,
) -> dict:
    """Library entry point. Returns a stats dict.

    Set ``log`` to ``print`` for a CLI-style progress trace.
    """
    if schema is None:
        schema = Path(__file__).parent / "schema.sql"
    if dest.exists():
        if not force:
            raise FileExistsError(
                f"{dest} exists; pass force=True to overwrite"
            )
        dest.unlink()

    log(f"→ Init schema at {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(str(dest))
    dst.execute("PRAGMA journal_mode=WAL")
    dst.executescript(schema.read_text())
    dst.commit()

    log(f"→ Open source {source} (read-only intent)")
    src = sqlite3.connect(str(source))

    obs_ids = _identify_msam_observations(src)
    log(f"  consolidation targets to drop: {len(obs_ids)}")

    log("→ Migrate atoms")
    atoms_counts = _migrate_atoms(src, dst, obs_ids)
    log(
        f"  migrated {atoms_counts['migrated']}, "
        f"dropped_obs {atoms_counts['dropped_obs']}, "
        f"tombstoned {atoms_counts['tombstoned']}"
    )

    log("→ Migrate embeddings")
    n_embeddings = _migrate_embeddings(src, dst)
    log(f"  {n_embeddings} embeddings ported")

    log("→ Migrate access_log → access_events")
    n_events = _migrate_access_log(src, dst)
    log(f"  {n_events} access events ported")

    log("→ Seed store events for atoms with no history")
    n_seeded = _seed_store_events(dst)
    log(f"  {n_seeded} synthetic store events")

    log("→ Rebuild atom_access_summary cache")
    n_summaries = _rebuild_summaries(dst)
    log(f"  {n_summaries} summaries built")

    log("→ Migrate atom_topics")
    n_topics = _migrate_topics(src, dst)
    log(f"  {n_topics} topic rows migrated")

    log("→ Migrate atom_relations")
    n_relations = _migrate_atom_relations(src, dst)
    log(f"  {n_relations} relations migrated")

    log("→ Migrate triples")
    n_triples = _migrate_triples(src, dst)
    log(f"  {n_triples} triples migrated")

    src.close()
    dst.close()
    log("✓ Migration complete")

    return {
        **atoms_counts,
        "embeddings": n_embeddings,
        "access_events": n_events,
        "seeded_store_events": n_seeded,
        "summaries": n_summaries,
        "topics": n_topics,
        "atom_relations": n_relations,
        "triples": n_triples,
    }


def main():
    p = argparse.ArgumentParser(prog="mimir migrate-memory")
    p.add_argument("--source", type=Path, required=True,
                   help="Path to source saga.db or MSAM snapshot")
    p.add_argument("--dest", type=Path, required=True,
                   help="Output mimir.saga.db (must not exist or --force)")
    p.add_argument("--schema", type=Path,
                   default=Path(__file__).parent / "schema.sql",
                   help="Path to mimir.saga schema DDL")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    try:
        migrate(
            source=args.source,
            dest=args.dest,
            schema=args.schema,
            force=args.force,
            log=print,
        )
        return 0
    except FileExistsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
