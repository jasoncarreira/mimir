"""Index integrity checks (SPEC §8.3, §16 item 16 — automated detection).

Mimir maintains two independent SQLite indexes. Corruption in either
produces *silent retrieval failures* — zero results or stale results —
not crashes. The §8.3 doc described the rebuild procedure; this
module is the detection half.

Two databases checked:

  - ``<home>/.mimir/index.db`` — the file-corpus index that backs
    ``file_search``. Schema: ``files`` (metadata) + ``chunks``
    (content + dense vector) + ``chunks_fts`` (FTS5 over content).
  - ``<home>/.mimir/saga.db`` — SAGA's atom store. Schema includes
    ``atoms`` + ``atoms_fts`` (FTS5) + ``embeddings`` (per-atom vectors).

Checks run per DB:

  1. ``sqlite_integrity_check`` — ``PRAGMA integrity_check``. Catches
     general database corruption (page checksum failures, malformed
     b-tree nodes, etc.).
  2. ``foreign_key_check`` — ``PRAGMA foreign_key_check``. Catches
     orphaned rows after a partial delete (e.g. a chunk whose parent
     file row was removed but the chunk wasn't).
  3. ``fts5_integrity_<table>`` — FTS5's own self-check via the
     ``INSERT INTO ft(ft) VALUES('integrity-check')`` magic-keyword.
     Catches FTS5-internal index corruption that ``integrity_check``
     misses (FTS5 keeps its own b-tree of token postings).
  4. ``fts5_sync_<table>`` — row-count match between the base table
     and the FTS5 virtual table. Catches FTS5 sync drift from a
     crash mid-write where the base row landed but the FTS5 update
     didn't (or vice versa).
  5. ``embedding_dim_uniform_<table>`` (index.db only) — confirms
     every embedding BLOB is the same length. Mismatch means the
     embedder model was swapped without a rebuild and similarity
     scores will be silently wrong.

Each check is independent — a failure in one doesn't prevent the
others from running. The CLI ``mimir verify-index`` prints the full
report; the scheduled-job callable emits ``index_integrity_ok`` or
``index_integrity_failed`` (algedonic-wired in ``feedback.py``).

The probes are read-only — they never attempt to repair, only to
detect. Repair is the operator's call via ``rebuild_index`` (§8.3).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class IntegrityCheck:
    """One probe's result."""

    name: str
    db: str          # "index" or "saga"
    ok: bool
    detail: str

    def render(self) -> str:
        status = "OK" if self.ok else "FAIL"
        return f"[{self.db}] {status}  {self.name}: {self.detail}"


@dataclass
class IntegrityReport:
    """Aggregate of every check run."""

    checks: list[IntegrityCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[IntegrityCheck]:
        return [c for c in self.checks if not c.ok]

    def render(self) -> str:
        lines = [c.render() for c in self.checks]
        n_ok = sum(1 for c in self.checks if c.ok)
        lines.append("")
        lines.append(f"{n_ok}/{len(self.checks)} checks passed")
        return "\n".join(lines)


# ── individual probes ───────────────────────────────────────────────


def _check_sqlite_integrity(db_path: Path, db_name: str) -> IntegrityCheck:
    """``PRAGMA integrity_check`` — returns one row containing ``"ok"``
    when the database is healthy, or one row per detected issue
    otherwise (up to a default of 100 issues)."""
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        return IntegrityCheck(
            "sqlite_integrity_check", db_name, False,
            f"can't open db: {exc}",
        )
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        conn.close()
        return IntegrityCheck(
            "sqlite_integrity_check", db_name, False,
            f"integrity_check failed: {exc}",
        )
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if len(rows) == 1 and rows[0][0] == "ok":
        return IntegrityCheck(
            "sqlite_integrity_check", db_name, True, "all checks passed",
        )
    # Truncate to first 5 issues — the operator cares that ANY issue
    # exists; the full list is recoverable by running the check manually.
    issues = "; ".join(str(r[0]) for r in rows[:5])
    if len(rows) > 5:
        issues += f" (+ {len(rows) - 5} more)"
    return IntegrityCheck(
        "sqlite_integrity_check", db_name, False, f"corruption: {issues}",
    )


def _check_foreign_keys(db_path: Path, db_name: str) -> IntegrityCheck:
    """``PRAGMA foreign_key_check`` — empty result means consistent.
    Each returned row is ``(child_table, rowid, parent_table, fkid)``
    pointing at an orphaned reference."""
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        return IntegrityCheck(
            "foreign_key_check", db_name, False, f"can't open db: {exc}",
        )
    try:
        # Foreign keys must be enabled per-connection for the pragma to
        # walk them; otherwise it always returns empty.
        conn.execute("PRAGMA foreign_keys = ON")
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        conn.close()
        return IntegrityCheck(
            "foreign_key_check", db_name, False,
            f"foreign_key_check failed: {exc}",
        )
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if not rows:
        return IntegrityCheck(
            "foreign_key_check", db_name, True, "no orphaned references",
        )
    # Each row: (table, rowid, parent, fkid).
    summary = "; ".join(
        f"{r[0]} rowid={r[1]} → {r[2]}" for r in rows[:5]
    )
    if len(rows) > 5:
        summary += f" (+ {len(rows) - 5} more)"
    return IntegrityCheck(
        "foreign_key_check", db_name, False, f"orphans: {summary}",
    )


def _check_fts5_integrity(
    db_path: Path, db_name: str, fts_table: str,
) -> IntegrityCheck:
    """FTS5's own self-check via the magic-keyword INSERT. On corruption
    raises ``sqlite3.DatabaseError`` with details; on clean indexes
    inserts nothing (the keyword is intercepted by FTS5 before the
    INSERT proper)."""
    check_name = f"fts5_integrity_{fts_table}"
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        return IntegrityCheck(check_name, db_name, False, f"can't open db: {exc}")
    try:
        # We have to substitute the table name into the SQL — bound
        # parameters can't be identifiers. ``fts_table`` is supplied
        # by this module's callers (not user input), so injection is
        # not a concern here.
        conn.execute(
            f"INSERT INTO {fts_table}({fts_table}) VALUES('integrity-check')"
        )
        return IntegrityCheck(check_name, db_name, True, "fts5 internal check ok")
    except sqlite3.DatabaseError as exc:
        return IntegrityCheck(check_name, db_name, False, str(exc).splitlines()[0])
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _check_fts5_row_count_match(
    db_path: Path, db_name: str, base_table: str, fts_table: str,
) -> IntegrityCheck:
    """Confirm the FTS5 virtual table has the same row count as its
    backing content table. Drift means an insert into one didn't
    propagate to the other — e.g. crash mid-write."""
    check_name = f"fts5_sync_{fts_table}"
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        return IntegrityCheck(check_name, db_name, False, f"can't open db: {exc}")
    try:
        n_rows = conn.execute(f"SELECT count(*) FROM {base_table}").fetchone()[0]
        n_fts = conn.execute(f"SELECT count(*) FROM {fts_table}").fetchone()[0]
    except sqlite3.Error as exc:
        conn.close()
        return IntegrityCheck(check_name, db_name, False, f"count query failed: {exc}")
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if n_rows == n_fts:
        return IntegrityCheck(
            check_name, db_name, True,
            f"{n_rows} rows, FTS5 in sync",
        )
    return IntegrityCheck(
        check_name, db_name, False,
        f"drift: {base_table}={n_rows} vs {fts_table}={n_fts}",
    )


def _check_embedding_dim_uniform(
    db_path: Path, db_name: str, table: str, column: str,
) -> IntegrityCheck:
    """All vectors in ``<table>.<column>`` should have the same byte
    length. A mix means the embedder model was changed without a
    rebuild — similarity scores against mismatched-dim vectors are
    silently wrong."""
    check_name = f"embedding_dim_uniform_{table}"
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        return IntegrityCheck(check_name, db_name, False, f"can't open db: {exc}")
    try:
        rows = conn.execute(
            f"SELECT length({column}) AS n, count(*) AS c FROM {table} "
            f"WHERE {column} IS NOT NULL GROUP BY n ORDER BY c DESC LIMIT 5"
        ).fetchall()
    except sqlite3.Error as exc:
        conn.close()
        return IntegrityCheck(check_name, db_name, False, f"dim query failed: {exc}")
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if not rows:
        return IntegrityCheck(
            check_name, db_name, True, "no embeddings (empty index)",
        )
    if len(rows) == 1:
        n_bytes, n_rows = rows[0]
        return IntegrityCheck(
            check_name, db_name, True,
            f"{n_rows} embeddings, uniform {n_bytes} bytes",
        )
    distribution = ", ".join(f"{n_bytes}B×{n_rows}" for n_bytes, n_rows in rows)
    return IntegrityCheck(
        check_name, db_name, False,
        f"mixed dims (model swap?): {distribution}",
    )


# ── per-database orchestration ──────────────────────────────────────


def check_file_corpus(home: Path) -> list[IntegrityCheck]:
    """Run all checks against ``<home>/.mimir/index.db`` (the
    file_search index). Returns a list of results — empty only if
    the DB file is missing."""
    db_path = home / ".mimir" / "index.db"
    if not db_path.is_file():
        return [IntegrityCheck(
            "file_corpus_db_present", "index", False,
            f"missing: {db_path}",
        )]
    return [
        _check_sqlite_integrity(db_path, "index"),
        _check_foreign_keys(db_path, "index"),
        _check_fts5_integrity(db_path, "index", "chunks_fts"),
        _check_fts5_row_count_match(db_path, "index", "chunks", "chunks_fts"),
        _check_embedding_dim_uniform(db_path, "index", "chunks", "embedding"),
    ]


def check_saga(home: Path) -> list[IntegrityCheck]:
    """Run all checks against ``<home>/.mimir/saga.db``. SAGA's FAISS
    vector index is in-memory and rebuilt on startup from the atoms
    table, so we check the atoms persistence layer; FAISS itself
    will be consistent if the underlying data is."""
    db_path = home / ".mimir" / "saga.db"
    if not db_path.is_file():
        return [IntegrityCheck(
            "saga_db_present", "saga", False,
            f"missing: {db_path}",
        )]
    return [
        _check_sqlite_integrity(db_path, "saga"),
        _check_foreign_keys(db_path, "saga"),
        _check_fts5_integrity(db_path, "saga", "atoms_fts"),
        _check_fts5_row_count_match(db_path, "saga", "atoms", "atoms_fts"),
    ]


def check_all(home: Path) -> IntegrityReport:
    """Run both DBs' checks; return a single combined report."""
    report = IntegrityReport()
    report.checks.extend(check_file_corpus(home))
    report.checks.extend(check_saga(home))
    return report


# ── CLI entrypoint (wired in cli.py) ────────────────────────────────


def run_verify_index_cmd(home: Path, db: str | None = None) -> int:
    """``mimir verify-index [--db index|saga]`` entrypoint. Returns
    0 if all selected checks pass, 1 otherwise."""
    if db == "index":
        checks = check_file_corpus(home)
    elif db == "saga":
        checks = check_saga(home)
    else:
        report = check_all(home)
        print(report.render())
        return 0 if report.ok else 1
    report = IntegrityReport(checks=checks)
    print(report.render())
    return 0 if report.ok else 1


# ── scheduled-job callable (wired in scheduler.py) ──────────────────


async def run_scheduled_integrity_check(home: Path) -> None:
    """Daily check fired by the scheduler. Emits an algedonic event
    (``index_integrity_ok`` / ``index_integrity_failed``) so the
    agent's feedback block surfaces corruption without operator
    polling.

    Fire-and-forget logging — never raises, so a bad probe can't
    crash the scheduler loop.
    """
    # Local import keeps event_logger out of the import chain for
    # synchronous CLI callers / test isolation.
    from .event_logger import log_event
    try:
        # The integrity probes perform synchronous SQLite PRAGMAs and FTS5
        # checks. Keep the scheduled coroutine cheap on the event loop by
        # running the full probe set in a worker thread; the CLI path remains
        # synchronous via ``check_all`` / ``run_verify_index_cmd``.
        report = await asyncio.to_thread(check_all, home)
    except Exception as exc:  # noqa: BLE001 — defensive scheduler boundary
        log.exception("index_integrity_check raised")
        await log_event(
            "index_integrity_failed",
            reason=f"check raised: {type(exc).__name__}: {exc}",
        )
        return
    if report.ok:
        await log_event(
            "index_integrity_ok",
            checks=len(report.checks),
        )
        return
    await log_event(
        "index_integrity_failed",
        failures=[
            {"db": c.db, "name": c.name, "detail": c.detail}
            for c in report.failures
        ],
        passed=len(report.checks) - len(report.failures),
        total=len(report.checks),
    )
