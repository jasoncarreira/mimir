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


# Per-1M-token cost estimates keyed on MODEL name (mimir review PR #146:
# voyage's voyage-4-large is $0.12/M, not $0.02 — keying on provider was
# wrong). Used for the cost preview in ``--dry-run`` mode. Conservative —
# actual rates may be lower under tier discounts / free credits.
# Missing entries fall back to None (no per-token cost — assume local).
_MODEL_COST_PER_M_TOKENS: dict[str, float | None] = {
    # OpenAI
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
    # Voyage 4 series
    "voyage-4-lite": 0.02,
    "voyage-4": 0.06,
    "voyage-4-large": 0.12,
    # Voyage 3 series (legacy)
    "voyage-3.5-lite": 0.02,
    "voyage-3.5": 0.06,
    "voyage-3-large": 0.18,
    # NVIDIA NIM — hosted but free under typical signup credits
    "nvidia/nv-embedqa-e5-v5": 0.00,
    # Local / fastembed / ONNX — no per-token cost
    "BAAI/bge-small-en-v1.5": None,
    "BAAI/bge-base-en-v1.5": None,
    "BAAI/bge-large-en-v1.5": None,
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
    embedding provider. Resolves via mimir.saga._config_io + mimir.saga.embeddings."""
    from .saga._config_io import get_config
    from .saga.embeddings import get_provider

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
    """Re-embed atoms in mimir.saga's atoms table under the
    currently-configured provider.

    Delegates to ``mimir.saga.calibration.re_embed`` — the ported
    saga.calibration logic adapted to mimir.saga's schema (atoms
    use ``tombstoned`` not ``state``; embeddings live in a sidecar
    table, not on the atom row). The function:

    - Filters ``tombstoned = 0`` so forgotten atoms don't burn API calls
    - Upserts the ``embeddings`` table with new (provider, model, dim, vec)
    - Bumps ``atoms.embedding_dim`` so the FAISS index loader filters
      mismatched-dim rows on rebuild

    After completion the caller should ``SagaStore.rebuild_index``
    — the dim or provider may have changed.

    Note on the ``sentence_embeddings`` table: if the operator has
    ``[retrieval] enable_subatom_beam = true`` in saga.toml, the
    per-sentence embedding cache (saga-era subatom) is NOT migrated
    by this reindex (scope is atoms only). Operators using subatom
    retrieval should clear that table manually after reindex; the
    next compressed_retrieve call repopulates against the new
    provider. Filed as a chainlink for follow-up.
    """
    started = time.time()
    provider_name, dim = _provider_info()
    try:
        from .saga.calibration import re_embed
    except ImportError:
        log.warning("reindex(atoms): mimir.saga.calibration.re_embed unavailable")
        return ReindexReport(
            target="atoms", db_path=db_path,
            total_rows=0, already_current=0, needs_reindex=0,
            reindexed=0, failed=0, estimated_input_chars=0,
            elapsed_seconds=0.0, provider=provider_name, dimension=dim,
        )

    # The ported re_embed handles dry-run + apply + the tombstoned
    # filter; we just adapt its response to ReindexReport.
    try:
        result = re_embed(
            db_path=db_path,
            target_provider_name=provider_name,
            batch_size=batch_size,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("reindex(atoms): mimir.saga.calibration.re_embed failed")
        return ReindexReport(
            target="atoms", db_path=db_path,
            total_rows=0, already_current=0, needs_reindex=0,
            reindexed=0, failed=1, estimated_input_chars=0,
            elapsed_seconds=time.time() - started,
            provider=provider_name, dimension=dim,
        )

    total = result.get("atoms_total", 0)
    updated = result.get("atoms_updated", 0)
    # saga.re_embed doesn't pre-filter already-current rows — it
    # re-embeds every active/fading atom regardless. For the dry-run
    # estimate we treat them all as needs_reindex.
    needs = total if dry_run else updated
    # Estimated input chars — saga doesn't return this; approximate
    # from total atom count times an average content size.
    estimated_chars = total * 200 if dry_run else 0
    return ReindexReport(
        target="atoms", db_path=db_path,
        total_rows=total,
        already_current=0,
        needs_reindex=needs,
        reindexed=updated,
        failed=0,
        estimated_input_chars=estimated_chars,
        elapsed_seconds=time.time() - started,
        provider=provider_name, dimension=dim,
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
    from .saga.embeddings import get_provider

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
        # Look up cost by MODEL (not provider) — voyage-4-large is
        # $0.12/M, very different from voyage-4-lite's $0.02. Per PR
        # #146 review polish.
        from .saga._config_io import get_config
        cfg = get_config()
        model = cfg("embedding", "model", "")
        cost = _MODEL_COST_PER_M_TOKENS.get(model)
        if cost is not None and cost > 0:
            est_tokens = report.estimated_input_chars / 4
            est_cost = est_tokens / 1_000_000 * cost
            voyage_note = (
                " (voyage has 200M free signup credit)"
                if report.provider == "voyage" else ""
            )
            print(
                f"  model:               {model}\n"
                f"  estimated tokens:    ~{est_tokens:,.0f}\n"
                f"  estimated cost:      ~${est_cost:.4f} "
                f"(at ${cost}/M tokens){voyage_note}\n"
            )
        elif cost == 0:
            print(f"  model:               {model} (free tier)\n")
        else:
            print(f"  model:               {model or '(local)'} — no API spend\n")
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

    # Apply-mode safety: warn loudly about concurrent writes. There's no
    # cross-process lock against a live mimir process, so an operator who
    # forgets to stop mimir first might get write contention on the
    # SQLite files mid-reindex. The DB-level locking would surface as
    # transient errors; clearer to warn up-front than debug after.
    if not dry_run:
        print(
            "\n⚠ APPLY MODE: ensure mimir is stopped before continuing.\n"
            "  Reindex writes to saga.db / index.db without a "
            "cross-process lock; a running mimir may cause write "
            "contention or duplicate work.\n"
            "  If you see SQLite 'database is locked' errors, stop "
            "mimir and re-run.\n"
        )

    # If subatom retrieval is enabled, the sentence_embeddings table
    # isn't migrated by saga.calibration.re_embed — flag it so operators
    # know to manually clear that cache for full provider migration.
    from .saga._config_io import get_config
    cfg = get_config()
    if args.target in ("atoms", "both") and \
            cfg("retrieval", "enable_subatom_beam", False):
        print(
            "\nNote: [retrieval] enable_subatom_beam = true is set. The "
            "sentence_embeddings cache (populated by the subatom-beam "
            "retrieval path) is NOT "
            "migrated by this reindex. Clear it manually after this run "
            "(or wait for it to repopulate naturally on next "
            "compressed_retrieve calls):\n"
            "  sqlite3 <home>/.mimir/saga.db 'DELETE FROM "
            "sentence_embeddings;'\n"
        )

    reports: list[ReindexReport] = []
    if args.target in ("atoms", "both"):
        # saga's db_path comes from saga.toml [storage] db_path —
        # reading from config rather than hardcoding so non-default
        # layouts work. Used only for ReindexReport display since
        # saga.calibration.re_embed reads via get_db() internally.
        atoms_db = Path(cfg("storage", "db_path", str(home / ".mimir" / "saga.db")))
        if not atoms_db.is_absolute():
            atoms_db = home / atoms_db
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
