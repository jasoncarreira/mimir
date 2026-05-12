"""``mimir reindex`` — re-embed saga atoms and/or mimir's file_search index
under the currently-configured embedding provider.

When an operator switches embedding providers (e.g.
``mimir setup --embedding voyage`` after running on openai for weeks),
the existing BLOBs in saga's ``atoms.embedding`` and mimir's
``chunks.embedding`` columns are in the OLD provider's vector space.
The cosine-similarity math in saga and mimir's file_search still
runs, but the result is meaningless — query embeddings (new
provider's space) get compared against stored embeddings (old
provider's space).

This module walks the saga DB + mimir file_search DB, detects rows
whose embedding dimension doesn't match the configured provider,
re-computes each via the current provider, and writes back atomically
per-row. Naturally resumable: a re-run skips rows whose BLOBs already
match the current dimension.

Safety controls:

- **Dry-run by default.** Operator must pass ``--apply`` to actually
  write. Default behavior reports what would change + cost estimate.
- **Per-row atomic.** Each UPDATE is its own transaction; a crash
  leaves consistent state (some rows on new provider, some on old,
  detectable via dim-mismatch on next run).
- **Cost estimate.** For hosted providers, sums input characters and
  reports estimated $$ cost at typical per-1M-token rates before
  running.
- **Progress logging.** Emits a progress line every 100 rows so an
  operator running interactively can see throughput.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# Per-1M-token cost estimates for hosted embedding providers. Used for
# the cost preview in ``--dry-run`` mode. Conservative — actual rates
# may be lower under tier discounts / free credits. None = local
# inference (no per-token cost).
_PROVIDER_COST_PER_M_TOKENS: dict[str, float | None] = {
    "openai": 0.02,
    "voyage": 0.02,
    "nvidia-nim": 0.00,  # free tier on NIM credits
    "onnx": None,
    "local": None,
}


@dataclass
class ReindexReport:
    """Summary of a reindex pass — emitted at the end + on dry-run."""

    target: str  # "atoms" | "files"
    db_path: Path
    total_rows: int
    already_current: int
    needs_reindex: int
    reindexed: int
    failed: int
    estimated_input_chars: int
    elapsed_seconds: float
    provider: str
    dimension: int


def _expected_blob_len(dimension: int) -> int:
    """Saga and mimir store embeddings as raw little-endian float32 bytes.
    A 1024-d vector is 4096 bytes."""
    return dimension * 4


def _provider_info() -> tuple[str, int]:
    """Return ``(provider_name, dimension)`` for the currently-configured
    saga embedding provider. Resolves via saga.config + saga.embeddings."""
    from saga.config import get_config
    from saga.embeddings import get_provider

    cfg = get_config()
    provider_name = cfg("embedding", "provider", "nvidia-nim")
    provider = get_provider()
    return provider_name, provider.dimensions()


def reindex_saga_atoms(
    db_path: Path,
    *,
    dry_run: bool = True,
    batch_size: int = 50,
) -> ReindexReport:
    """Re-embed atoms in saga's atoms table whose embedding dimension
    doesn't match the currently-configured provider."""
    return _reindex_table(
        target="atoms",
        db_path=db_path,
        table="atoms",
        id_column="id",
        content_column="content",
        embedding_column="embedding",
        dry_run=dry_run,
        batch_size=batch_size,
    )


def reindex_file_search(
    db_path: Path,
    *,
    dry_run: bool = True,
    batch_size: int = 50,
) -> ReindexReport:
    """Re-embed chunks in mimir's file_search index whose embedding
    dimension doesn't match the currently-configured provider."""
    return _reindex_table(
        target="files",
        db_path=db_path,
        table="chunks",
        id_column="rowid",
        content_column="content",
        embedding_column="embedding",
        dry_run=dry_run,
        batch_size=batch_size,
    )


def _reindex_table(
    *,
    target: str,
    db_path: Path,
    table: str,
    id_column: str,
    content_column: str,
    embedding_column: str,
    dry_run: bool,
    batch_size: int,
) -> ReindexReport:
    """Core walk-and-re-embed loop. Used by both atoms + file_search.

    For each row:
    1. Inspect existing BLOB length. If it matches the current
       provider's expected size, skip (already current).
    2. Otherwise: queue for re-embed.
    3. Batch up to ``batch_size`` queued rows, call provider.batch_embed,
       update each row atomically.
    """
    from saga.embeddings import get_provider

    if not db_path.exists():
        log.warning("reindex: db not found at %s — skipping", db_path)
        return ReindexReport(
            target=target, db_path=db_path,
            total_rows=0, already_current=0, needs_reindex=0,
            reindexed=0, failed=0, estimated_input_chars=0,
            elapsed_seconds=0.0, provider="-", dimension=0,
        )

    provider_name, dim = _provider_info()
    provider = get_provider()
    expected_len = _expected_blob_len(dim)

    started = time.time()
    conn = sqlite3.connect(str(db_path))
    try:
        # First pass: count rows and identify candidates. Cheap — only
        # reads (id, length(embedding), length(content)).
        rows = conn.execute(
            f"SELECT {id_column}, length({embedding_column}) AS blob_len, "
            f"length({content_column}) AS content_len, {content_column} "
            f"FROM {table} "
            f"WHERE {embedding_column} IS NOT NULL "
            f"ORDER BY {id_column}"
        ).fetchall()
        total = len(rows)
        candidates: list[tuple] = []
        already_current = 0
        for row in rows:
            row_id, blob_len, content_len, content = row
            if blob_len == expected_len:
                already_current += 1
                continue
            candidates.append((row_id, content_len, content))
        needs = len(candidates)
        estimated_chars = sum(c[1] for c in candidates if c[1])

        if dry_run or needs == 0:
            return ReindexReport(
                target=target, db_path=db_path,
                total_rows=total, already_current=already_current,
                needs_reindex=needs, reindexed=0, failed=0,
                estimated_input_chars=estimated_chars,
                elapsed_seconds=time.time() - started,
                provider=provider_name, dimension=dim,
            )

        # Apply pass: batch the candidates, embed, write back.
        reindexed = 0
        failed = 0
        log.info(
            "reindex(%s): re-embedding %d rows via provider=%s dim=%d",
            target, needs, provider_name, dim,
        )
        for i in range(0, len(candidates), batch_size):
            chunk = candidates[i:i + batch_size]
            contents = [c[2] for c in chunk]
            try:
                vecs = provider.batch_embed(contents, input_type="passage")
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "reindex(%s): batch %d-%d failed: %s",
                    target, i, i + len(chunk), exc,
                )
                failed += len(chunk)
                continue
            for (row_id, _, _), vec in zip(chunk, vecs):
                blob = b"".join(struct.pack("<f", float(v)) for v in vec)
                try:
                    conn.execute(
                        f"UPDATE {table} SET {embedding_column} = ? "
                        f"WHERE {id_column} = ?",
                        (blob, row_id),
                    )
                    conn.commit()
                    reindexed += 1
                except sqlite3.Error as exc:
                    log.warning(
                        "reindex(%s): update failed for %s=%r: %s",
                        target, id_column, row_id, exc,
                    )
                    failed += 1
            if reindexed % 100 == 0 and reindexed > 0:
                log.info(
                    "reindex(%s): %d/%d done (%.0f%%)",
                    target, reindexed, needs, 100 * reindexed / needs,
                )

        return ReindexReport(
            target=target, db_path=db_path,
            total_rows=total, already_current=already_current,
            needs_reindex=needs, reindexed=reindexed, failed=failed,
            estimated_input_chars=estimated_chars,
            elapsed_seconds=time.time() - started,
            provider=provider_name, dimension=dim,
        )
    finally:
        conn.close()


def _print_report(report: ReindexReport, *, dry_run: bool) -> None:
    """Human-readable summary written to stdout."""
    mode = "DRY RUN" if dry_run else "APPLIED"
    print(
        f"\n=== reindex {report.target} ({mode}) ===\n"
        f"  db:                  {report.db_path}\n"
        f"  provider:            {report.provider} (dim={report.dimension})\n"
        f"  total rows:          {report.total_rows}\n"
        f"  already current:     {report.already_current}\n"
        f"  needs reindex:       {report.needs_reindex}\n"
    )
    if dry_run and report.needs_reindex:
        cost = _PROVIDER_COST_PER_M_TOKENS.get(report.provider)
        if cost is not None and cost > 0:
            # Rough estimate: 4 chars per token. Cheap upper bound.
            est_tokens = report.estimated_input_chars / 4
            est_cost = est_tokens / 1_000_000 * cost
            print(
                f"  estimated tokens:    ~{est_tokens:,.0f}\n"
                f"  estimated cost:      ~${est_cost:.4f} "
                f"(at ${cost}/M tokens; voyage has 200M free credit)\n"
            )
        else:
            print("  cost:                local — no API spend\n")
        print(
            "  Re-run with --apply to actually re-embed.\n"
        )
    elif not dry_run:
        print(
            f"  reindexed:           {report.reindexed}\n"
            f"  failed:              {report.failed}\n"
            f"  elapsed:             {report.elapsed_seconds:.1f}s\n"
        )


# ─── argparse wiring ──────────────────────────────────────────────


def add_argparse(p: argparse.ArgumentParser) -> None:
    """Attach the ``reindex`` flags to a subparser.

    Registered from ``mimir.cli`` as a top-level subcommand.
    """
    p.add_argument(
        "--home", type=Path, default=None,
        help="MIMIR_HOME (default: read from env or cwd).",
    )
    p.add_argument(
        "--target", choices=("atoms", "files", "both"), default="both",
        help="Which embedding store to reindex. atoms = saga's atoms.db; "
             "files = mimir's file_search index.db; both = both stores.",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actually write the new embeddings. Default is dry-run "
             "(reports what would change + estimated cost).",
    )
    p.add_argument(
        "--batch-size", type=int, default=50,
        help="Number of rows per provider.batch_embed call (default: 50).",
    )


def dispatch(args: argparse.Namespace) -> int:
    """Called from ``mimir/cli.py`` after argparse populates ``args``."""
    import os

    home = args.home
    if home is None:
        home_env = os.environ.get("MIMIR_HOME")
        if home_env:
            home = Path(home_env)
        else:
            home = Path.cwd()
    home = home.resolve()
    os.environ["MIMIR_HOME"] = str(home)

    # Wire SAGA_CONFIG so saga reads the same per-home toml mimir does.
    saga_toml = home / "saga.toml"
    if saga_toml.is_file() and "SAGA_CONFIG" not in os.environ:
        os.environ["SAGA_CONFIG"] = str(saga_toml)

    dry_run = not args.apply
    reports: list[ReindexReport] = []
    if args.target in ("atoms", "both"):
        atoms_db = home / ".mimir" / "saga.db"
        reports.append(reindex_saga_atoms(
            atoms_db, dry_run=dry_run, batch_size=args.batch_size,
        ))
    if args.target in ("files", "both"):
        files_db = home / ".mimir" / "index.db"
        reports.append(reindex_file_search(
            files_db, dry_run=dry_run, batch_size=args.batch_size,
        ))

    for r in reports:
        _print_report(r, dry_run=dry_run)

    if any(r.failed for r in reports):
        return 2
    return 0
