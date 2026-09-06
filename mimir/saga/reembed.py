"""Offline, resumable repair of dimension-mismatched Saga embeddings."""

from __future__ import annotations

import math
import sqlite3
import struct
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


def reembed(
    db_path: Path,
    *,
    batch_size: int = 50,
    batch_delay: float = 0.0,
    dry_run: bool = False,
    progress: Callable[[str], None] = print,
) -> dict[str, int]:
    """Repair existing stale/NULL-dimension vectors; leave matching rows alone.

    The server must be stopped. Each validated batch commits independently;
    failures propagate and reruns skip previously committed rows. No schema or
    in-memory index initialization is performed.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if not math.isfinite(batch_delay) or batch_delay < 0:
        raise ValueError("batch_delay must be finite and nonnegative")

    from ._config_io import get_config
    from .embeddings import get_provider

    max_chars = get_config()("embedding", "max_input_chars", 2000)
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_input_chars must be a positive integer")
    # URI modes prevent accidentally creating an empty database on a typo.
    mode = "ro" if dry_run else "rw"
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode={mode}", uri=True)
    try:
        provider = get_provider()
        dim = provider.dimensions()
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise ValueError("provider dimensions must be a positive integer")
        progress(
            f"{'DRY RUN' if dry_run else 'APPLY'}: {db_path} "
            f"provider={provider.provider_name} model={provider.model_id} dim={dim}"
        )
        sources = {
            "atoms": (
                "embeddings e JOIN atoms a ON a.id = e.atom_id",
                "a.id", "a.content", "a.tombstoned = 0 AND e.dim IS NOT ?",
            ),
            "sessions": (
                "sessions", "id", "summary",
                "embedding IS NOT NULL AND embedding_dim IS NOT ?",
            ),
        }
        report = {}
        for name, (source, key, text, predicate) in sources.items():
            total = conn.execute(
                f"SELECT count(*) FROM {source} WHERE {predicate}", (dim,),
            ).fetchone()[0]
            report[f"{name}_stale"] = total
            report[f"{name}_updated"] = 0
            progress(f"{name}: {total} stale rows")
        usable_summary = "summary IS NOT NULL AND trim(summary, char(9,10,11,12,13,32)) != ''"
        report["atoms_unrepaired"] = 0
        report["sessions_unrepaired"] = conn.execute(
            f"SELECT count(*) FROM sessions WHERE {sources['sessions'][3]} "
            f"AND NOT ({usable_summary})", (dim,),
        ).fetchone()[0]
        progress(
            f"sessions: {report['sessions_unrepaired']} unrepaired "
            "(missing or empty summary; original text required)"
        )
        if dry_run:
            return report

        called_provider = False
        for name, (source, key, text, predicate) in sources.items():
            if name == "sessions":
                predicate += f" AND {usable_summary}"
            last_id = None
            while True:
                # Keyset pagination bounds memory and avoids rescanning repaired rows.
                after = f" AND {key} > ?" if last_id is not None else ""
                params = (dim, last_id, batch_size) if after else (dim, batch_size)
                rows = conn.execute(
                    f"SELECT {key}, {text} FROM {source} WHERE {predicate}"
                    f"{after} ORDER BY {key} LIMIT ?", params,
                ).fetchall()
                if not rows:
                    break
                if called_provider and batch_delay:
                    time.sleep(batch_delay)
                vectors = provider.batch_embed(
                    [r[1][:max_chars] for r in rows], input_type="passage",
                )
                called_provider = True
                if len(vectors) != len(rows):
                    raise ValueError(f"{name}: provider returned wrong vector count")
                blobs = []
                for vector in vectors:
                    if len(vector) != dim:
                        raise ValueError(f"{name}: provider returned wrong vector dimension")
                    if not all(math.isfinite(value) for value in vector):
                        raise ValueError(f"{name}: provider returned non-finite vector")
                    blobs.append(struct.pack(f"<{dim}f", *vector))

                # Validate and serialize the entire batch before the first UPDATE.
                with conn:
                    if name == "atoms":
                        now = datetime.now(timezone.utc).isoformat()
                        conn.executemany(
                            "UPDATE embeddings SET vec=?, dim=?, provider=?, model=?, "
                            "embedded_at=? WHERE atom_id=? AND dim IS NOT ?",
                            [(blob, dim, provider.provider_name, provider.model_id,
                              now, row[0], dim) for row, blob in zip(rows, blobs)],
                        )
                    else:
                        conn.executemany(
                            "UPDATE sessions SET embedding=?, embedding_dim=? "
                            "WHERE id=? AND embedding_dim IS NOT ?",
                            [(blob, dim, row[0], dim) for row, blob in zip(rows, blobs)],
                        )
                last_id = rows[-1][0]
                report[f"{name}_updated"] += len(rows)
                progress(
                    f"{name}: committed {report[f'{name}_updated']}/"
                    f"{report[f'{name}_stale']}"
                )
        for name, (source, _, _, predicate) in sources.items():
            report[f"{name}_unrepaired"] = conn.execute(
                f"SELECT count(*) FROM {source} WHERE {predicate}", (dim,),
            ).fetchone()[0]
            progress(f"{name}: {report[f'{name}_unrepaired']} unrepaired stale rows remain")
        if report["atoms_unrepaired"] or report["sessions_unrepaired"]:
            progress("Incomplete. Restore missing original summaries and rerun; no text was invented.")
        else:
            progress("Complete. Restart mimir to rebuild its in-memory vector indexes.")
        return report
    finally:
        conn.close()
