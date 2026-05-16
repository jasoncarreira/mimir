"""Bulk re-embed all live atoms under a new embedding provider.

One-shot ops tool. Used when migrating from one embedding provider to
another (e.g. voyage-4-lite → text-embedding-3-small or the reverse):

1. Walk every non-tombstoned atom.
2. Embed its content with the target provider.
3. Replace the row in the ``embeddings`` table (atom_id-keyed). The
   ``embeddings.dim`` column is what ``VectorIndex.build_from_db``
   reads to filter stale-dim rows on rebuild — atoms don't carry
   embedding_dim themselves (the ``embedding_dim`` column lives on
   ``triples``, unrelated to atom embeddings).

Compare with ``saga.calibration.re_embed`` (the ancestor): mimir.saga's
embeddings live in a sidecar ``embeddings`` table, not on the atom row,
so the migration doesn't rewrite the atoms table. Also there's no
``state`` machine — ``tombstoned = 0`` selects every live atom.

After ``re_embed`` completes, the caller should drop and rebuild the
FAISS index (``SagaStore.rebuild_index``) — the dim or provider may
have changed, and the in-memory index won't auto-detect that.
"""

from __future__ import annotations

import logging
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._config_io import get_config
from .embeddings import get_provider

log = logging.getLogger(__name__)


def re_embed(
    db_path: Path,
    *,
    target_provider_name: str | None = None,
    batch_size: int = 50,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-embed every non-tombstoned atom under the current (or override)
    embedding provider.

    Args:
        db_path: Path to ``memory.db``.
        target_provider_name: Provider name override. ``None`` uses the
            saga.toml-resolved provider — the common case for cron-driven
            provider migrations where the operator edited the TOML
            already. Pass a specific name (``"openai"``, ``"voyage"``,
            ``"onnx"``) for one-off forced re-embeds without touching
            saga.toml.
        batch_size: Atoms per embed call. 50 is a good tradeoff between
            provider request overhead and memory pressure; bump to 200
            for high-throughput providers (openai), drop to 10 for
            providers that aggressively rate-limit.
        dry_run: Don't write; report what would happen.

    Returns:
        ``{"target_provider", "atoms_total", "atoms_updated",
          "dry_run", "index_rebuild_needed"}``
    """
    cfg = get_config()
    provider_name = target_provider_name or cfg(
        "embedding", "provider", "openai",
    )
    model_name = cfg("embedding", "model", "")

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, content FROM atoms WHERE tombstoned = 0",
        ).fetchall()
        atom_count = len(rows)

        if dry_run:
            return {
                "target_provider": provider_name,
                "atoms_total": atom_count,
                "atoms_updated": 0,
                "dry_run": True,
                "index_rebuild_needed": atom_count > 0,
            }

        if atom_count == 0:
            return {
                "target_provider": provider_name,
                "atoms_total": 0,
                "atoms_updated": 0,
                "dry_run": False,
                "index_rebuild_needed": False,
            }

        provider = get_provider()
        dim = provider.dimensions()
        updated = 0
        now_iso = datetime.now(tz=timezone.utc).isoformat()

        for i in range(0, atom_count, batch_size):
            batch = rows[i:i + batch_size]
            contents = [r["content"] for r in batch]
            ids = [r["id"] for r in batch]
            try:
                vectors = provider.batch_embed(contents, input_type="passage")
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "re_embed: batch embed failed at offset %d: %s",
                    i, exc,
                )
                continue

            try:
                conn.execute("BEGIN IMMEDIATE")
                for atom_id, vec in zip(ids, vectors):
                    vec_bytes = struct.pack(f"{dim}f", *vec)
                    # Upsert into the sidecar embeddings table. The
                    # ``embeddings`` row's ``dim`` is the source of truth
                    # for FAISS index filtering — ``VectorIndex.build_from_db``
                    # reads ``embeddings.dim`` to skip mismatched-dim rows
                    # on rebuild. Atoms don't track dim themselves
                    # (``atoms`` has no ``embedding_dim`` column —
                    # ``triples`` does, but those are unrelated triple
                    # embeddings).
                    conn.execute(
                        "INSERT INTO embeddings "
                        "(atom_id, provider, model, dim, vec, embedded_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(atom_id) DO UPDATE SET "
                        "  provider = excluded.provider, "
                        "  model = excluded.model, "
                        "  dim = excluded.dim, "
                        "  vec = excluded.vec, "
                        "  embedded_at = excluded.embedded_at",
                        (atom_id, provider_name, model_name, dim,
                         vec_bytes, now_iso),
                    )
                    updated += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return {
            "target_provider": provider_name,
            "atoms_total": atom_count,
            "atoms_updated": updated,
            "dry_run": False,
            "index_rebuild_needed": updated > 0,
        }
    finally:
        conn.close()
