"""Read-only file-memory health report for ``mimir memory doctor``.

The doctor intentionally avoids proposal/edit/rebuild paths. It reads memory
files, compares generated content in memory, and returns stable text/JSON
models for operator review.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import sqlite3
from pathlib import Path
from typing import Any

from .core_blocks import (
    _CHANNEL_MEMORY_MAX_BYTES,
    _CORE_BLOCKS_MIN_BYTES,
    _SYNTHETIC_PREFIXES,
    describe_file,
    extract_desc_comment,
)
from .index import (
    IndexEntry,
    build_state_index,
    build_wiki_index,
    render_memory_index,
)
from .wiki_backlinks import build_graph

LEARNINGS_PENDING_MAX_BYTES = 8_192
LEARNINGS_PENDING_MAX_LINES = 200
ISSUE_NOTE_MAX_BYTES = 8_192
STATE_SPEC_OLD_DAYS = 30
TOP_EXAMPLES = 5

_ALLOWED_TOP_LEVEL_STATE_MD = frozenset({
    "INDEX.md",
    "heartbeat-backlog.md",
    "proposed-changes.md",
})

_SEVERITIES: tuple[str, ...] = ("error", "warning", "info")


@dataclass(frozen=True)
class DoctorFinding:
    layer: str
    check: str
    severity: str
    path: str
    message: str
    suggestion: str

    def to_json(self) -> dict[str, str]:
        return {
            "layer": self.layer,
            "check": self.check,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class DoctorSection:
    name: str
    metrics: dict[str, int]

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "metrics": dict(sorted(self.metrics.items()))}


@dataclass(frozen=True)
class DoctorReport:
    status: str
    severity_counts: dict[str, int]
    sections: list[DoctorSection]
    findings: list[DoctorFinding]

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "severity_counts": {k: self.severity_counts.get(k, 0) for k in _SEVERITIES},
            "sections": [s.to_json() for s in self.sections],
            "findings": [f.to_json() for f in self.findings],
        }


def run_doctor(home: Path) -> DoctorReport:
    home = home.resolve()
    findings: list[DoctorFinding] = []
    sections = [
        _check_core(home, findings),
        _check_channels(home, findings),
        _check_issue_notes(home, findings),
        _check_learnings_pending(home, findings),
        _check_memory_index(home, findings),
        _check_saga_substrate(home, findings),
        _check_state(home, findings),
        _check_wiki(home, findings),
    ]
    counts = Counter(f.severity for f in findings)
    severity_counts = {severity: counts.get(severity, 0) for severity in _SEVERITIES}
    if severity_counts["error"]:
        status = "error"
    elif severity_counts["warning"]:
        status = "warning"
    else:
        status = "ok"
    return DoctorReport(
        status=status,
        severity_counts=severity_counts,
        sections=sections,
        findings=sorted(findings, key=lambda f: (f.layer, f.path, f.check, f.severity)),
    )


def render_text(report: DoctorReport) -> str:
    lines = [
        f"Memory doctor status: {report.status}",
        "Severity counts: "
        + ", ".join(f"{s}={report.severity_counts.get(s, 0)}" for s in _SEVERITIES),
        "",
        "Sections:",
    ]
    for section in report.sections:
        metrics = ", ".join(f"{k}={v}" for k, v in sorted(section.metrics.items()))
        lines.append(f"- {section.name}: {metrics}")
    lines.append("")
    lines.append("Findings:")
    if not report.findings:
        lines.append("- none")
    else:
        for finding in report.findings:
            path = f" {finding.path}" if finding.path else ""
            lines.append(
                f"- [{finding.severity}] {finding.layer}/{finding.check}{path}: "
                f"{finding.message} Suggestion: {finding.suggestion}"
            )
    return "\n".join(lines) + "\n"


def _check_core(home: Path, findings: list[DoctorFinding]) -> DoctorSection:
    core_dir = home / "memory" / "core"
    files = _md_files(core_dir, recursive=False)
    empty = missing_desc = undersized = total_bytes = 0
    for path in files:
        text = _read_text(path)
        nbytes = len(text.encode("utf-8"))
        total_bytes += nbytes
        rel = _rel(home, path)
        if not text.strip():
            empty += 1
            findings.append(_finding(
                "core", "empty", "error", rel,
                "Core block is empty.",
                "Restore or remove the empty core block before it is injected.",
            ))
        if extract_desc_comment(text) is None:
            missing_desc += 1
            findings.append(_finding(
                "core", "desc-header", "warning", rel,
                "Core block is missing a leading <!-- desc: ... --> header.",
                "Add a first-line desc comment summarizing the block.",
            ))
        if nbytes < _CORE_BLOCKS_MIN_BYTES:
            undersized += 1
            findings.append(_finding(
                "core", "size", "warning", rel,
                f"Core block is {nbytes} bytes; expected at least {_CORE_BLOCKS_MIN_BYTES}.",
                "Confirm this is intentional or restore the lost content.",
            ))
    return DoctorSection("core", {
        "files": len(files),
        "bytes": total_bytes,
        "empty_files": empty,
        "missing_desc_headers": missing_desc,
        "undersized_files": undersized,
    })


def _check_channels(home: Path, findings: list[DoctorFinding]) -> DoctorSection:
    channels_dir = home / "memory" / "channels"
    channel_dirs = (
        sorted([p for p in channels_dir.iterdir() if p.is_dir()])
        if channels_dir.is_dir()
        else []
    )
    real_dirs = synthetic_dirs = over_cap = total_bytes = 0
    for channel_dir in channel_dirs:
        channel_id = channel_dir.name
        files = _md_files(channel_dir, recursive=False)
        combined = "\n\n---\n\n".join(
            text for path in files if (text := _read_text(path).rstrip())
        )
        nbytes = len(combined.encode("utf-8"))
        total_bytes += nbytes
        rel = _rel(home, channel_dir)
        if any(channel_id.startswith(prefix) for prefix in _SYNTHETIC_PREFIXES):
            synthetic_dirs += 1
            findings.append(_finding(
                "channels", "synthetic-non-injection", "info", rel,
                f"Synthetic channel directory is not injected into prompts ({nbytes} bytes ignored).",
                "Move durable operator context to a real channel or shared memory file if needed.",
            ))
            continue
        real_dirs += 1
        if nbytes > _CHANNEL_MEMORY_MAX_BYTES:
            over_cap += 1
            findings.append(_finding(
                "channels", "over-cap", "warning", rel,
                f"Real-channel memory is {nbytes} bytes over the {_CHANNEL_MEMORY_MAX_BYTES}-byte injection cap.",
                "Split, trim, or summarize this channel memory so the injected context fits.",
            ))
    return DoctorSection("channels", {
        "directories": len(channel_dirs),
        "real_directories": real_dirs,
        "synthetic_directories": synthetic_dirs,
        "bytes": total_bytes,
        "over_cap_directories": over_cap,
        "cap_bytes": _CHANNEL_MEMORY_MAX_BYTES,
    })


def _check_issue_notes(home: Path, findings: list[DoctorFinding]) -> DoctorSection:
    issues_dir = home / "memory" / "issues"
    files = _md_files(issues_dir, recursive=True)
    missing_desc = oversize = duplicate_files = 0
    fingerprints: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        text = _read_text(path)
        rel = _rel(home, path)
        nbytes = len(text.encode("utf-8"))
        if extract_desc_comment(text) is None:
            missing_desc += 1
            findings.append(_finding(
                "issues", "desc-header", "warning", rel,
                "Issue note is missing a leading <!-- desc: ... --> header.",
                "Add a first-line desc comment so INDEX.md can render a useful summary.",
            ))
        if nbytes > ISSUE_NOTE_MAX_BYTES:
            oversize += 1
            findings.append(_finding(
                "issues", "oversize", "warning", rel,
                f"Issue note is {nbytes} bytes; budget is {ISSUE_NOTE_MAX_BYTES}.",
                "Summarize or split the note so issue memory remains prompt-budget friendly.",
            ))
        key = _issue_duplicate_key(path, text)
        if key:
            fingerprints[key].append(path)
    for paths in fingerprints.values():
        if len(paths) < 2:
            continue
        keeper = paths[0]
        for duplicate in paths[1:]:
            duplicate_files += 1
            findings.append(_finding(
                "issues", "obvious-duplicate", "warning", _rel(home, duplicate),
                f"Issue note appears to duplicate {_rel(home, keeper)}.",
                "Merge the duplicate note or make the distinction explicit.",
            ))
    return DoctorSection("issues", {
        "files": len(files),
        "missing_desc_headers": missing_desc,
        "oversize_files": oversize,
        "duplicate_files": duplicate_files,
    })


def _check_learnings_pending(home: Path, findings: list[DoctorFinding]) -> DoctorSection:
    path = home / "memory" / "learnings-pending.md"
    if not path.is_file():
        return DoctorSection("learnings-pending", {
            "exists": 0, "bytes": 0, "lines": 0, "overgrown": 0,
            "max_bytes": LEARNINGS_PENDING_MAX_BYTES,
            "max_lines": LEARNINGS_PENDING_MAX_LINES,
        })
    text = _read_text(path)
    nbytes = len(text.encode("utf-8"))
    line_count = len(text.splitlines())
    overgrown = int(
        nbytes > LEARNINGS_PENDING_MAX_BYTES
        or line_count > LEARNINGS_PENDING_MAX_LINES
    )
    if overgrown:
        findings.append(_finding(
            "learnings-pending", "overgrown", "warning", _rel(home, path),
            f"Pending learnings are {nbytes} bytes across {line_count} lines.",
            "Promote, reject, or summarize pending learnings before the backlog hides signal.",
        ))
    return DoctorSection("learnings-pending", {
        "exists": 1,
        "bytes": nbytes,
        "lines": line_count,
        "overgrown": overgrown,
        "max_bytes": LEARNINGS_PENDING_MAX_BYTES,
        "max_lines": LEARNINGS_PENDING_MAX_LINES,
    })


def _check_memory_index(home: Path, findings: list[DoctorFinding]) -> DoctorSection:
    index_path = home / "memory" / "INDEX.md"
    rendered = _render_memory_index_readonly(home)
    exists = index_path.is_file()
    stale = 0
    if exists:
        current = _read_text(index_path)
        stale = int(current != rendered)
        if stale:
            findings.append(_finding(
                "index", "stale", "warning", _rel(home, index_path),
                "memory/INDEX.md differs from the current rendered memory index.",
                "Let the normal index flush regenerate it after the next memory change.",
            ))
    else:
        findings.append(_finding(
            "index", "missing", "warning", _rel(home, index_path),
            "memory/INDEX.md is missing.",
            "Let the normal index flush regenerate it.",
        ))
    return DoctorSection("index", {
        "exists": int(exists),
        "stale": stale,
        "rendered_bytes": len(rendered.encode("utf-8")),
    })


def _check_saga_substrate(home: Path, findings: list[DoctorFinding]) -> DoctorSection:
    db_path = home / ".mimir" / "saga.db"
    rel = _rel(home, db_path)
    metrics: dict[str, int] = {"exists": int(db_path.is_file())}
    if not db_path.is_file():
        findings.append(_finding(
            "saga", "missing-db", "warning", rel,
            "SAGA database is absent; substrate checks were skipped.",
            "This is expected before SAGA has stored memories; run verify-index once the DB exists.",
        ))
        return DoctorSection("saga", metrics)

    conn = _connect_sqlite_readonly(db_path)
    if conn is None:
        findings.append(_finding(
            "saga", "open-db", "warning", rel,
            "SAGA database could not be opened read-only.",
            "Inspect file permissions and run `mimir verify-index --db saga` for details.",
        ))
        return DoctorSection("saga", metrics)

    try:
        _collect_saga_integrity_metrics(conn, home, db_path, findings, metrics)
        tables = _sqlite_tables(conn)
        for required in (
            "atoms", "embeddings", "triples", "access_events",
            "atom_access_summary",
        ):
            metrics[f"table_{required}_present"] = int(required in tables)
            if required not in tables:
                findings.append(_finding(
                    "saga", f"missing-table-{required}", "warning", rel,
                    f"SAGA table `{required}` is absent; related checks were skipped.",
                    "This usually means an old or partially migrated SAGA DB; complete migrations before relying on these metrics.",
                ))

        if "atoms" in tables:
            _collect_saga_atom_metrics(conn, home, db_path, findings, metrics)
        if "embeddings" in tables:
            _collect_saga_embedding_metrics(conn, home, db_path, findings, metrics, tables)
        if "triples" in tables:
            _collect_saga_triple_metrics(conn, home, db_path, findings, metrics, tables)
        if "access_events" in tables or "atom_access_summary" in tables:
            _collect_saga_access_metrics(conn, home, db_path, findings, metrics, tables)
        _collect_forget_preview_metrics(home, metrics)
    finally:
        conn.close()
    return DoctorSection("saga", metrics)


def _collect_saga_integrity_metrics(
    conn: sqlite3.Connection,
    home: Path,
    db_path: Path,
    findings: list[DoctorFinding],
    metrics: dict[str, int],
) -> None:
    rel = _rel(home, db_path)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        ok = len(rows) == 1 and rows[0][0] == "ok"
        metrics["sqlite_integrity_ok"] = int(ok)
        if not ok:
            issues = "; ".join(str(r[0]) for r in rows[:5])
            if len(rows) > 5:
                issues += f" (+ {len(rows) - 5} more)"
            findings.append(_finding(
                "saga", "sqlite_integrity_check", "warning", rel,
                f"SQLite integrity check reported: {issues}",
                "Run `mimir verify-index --db saga` for the full integrity report.",
            ))
    except sqlite3.Error as exc:
        metrics["sqlite_integrity_ok"] = 0
        findings.append(_finding(
            "saga", "sqlite_integrity_check", "warning", rel,
            f"SQLite integrity check failed: {exc}.",
            "Run `mimir verify-index --db saga` for the full integrity report.",
        ))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        metrics["foreign_key_check_ok"] = int(not rows)
        metrics["foreign_key_violations"] = len(rows)
        if rows:
            summary = "; ".join(f"{r[0]} rowid={r[1]} -> {r[2]}" for r in rows[:5])
            if len(rows) > 5:
                summary += f" (+ {len(rows) - 5} more)"
            findings.append(_finding(
                "saga", "foreign_key_check", "warning", rel,
                f"Foreign-key check reported orphans: {summary}",
                "Run `mimir verify-index --db saga` for the full integrity report.",
            ))
    except sqlite3.Error as exc:
        metrics["foreign_key_check_ok"] = 0
        findings.append(_finding(
            "saga", "foreign_key_check", "warning", rel,
            f"Foreign-key check failed: {exc}.",
            "Run `mimir verify-index --db saga` for the full integrity report.",
        ))


def _connect_sqlite_readonly(db_path: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[0]) for row in rows}


def _collect_saga_atom_metrics(
    conn: sqlite3.Connection,
    home: Path,
    db_path: Path,
    findings: list[DoctorFinding],
    metrics: dict[str, int],
) -> None:
    rel = _rel(home, db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN COALESCE(tombstoned, 0) = 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN COALESCE(tombstoned, 0) != 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN COALESCE(is_pinned, 0) != 0 THEN 1 ELSE 0 END) "
            "FROM atoms"
        ).fetchone()
        total, live, tombstoned, pinned = (int(v or 0) for v in row)
        metrics.update({
            "atoms_total": total,
            "atoms_live": live,
            "atoms_tombstoned": tombstoned,
            "atoms_pinned": pinned,
        })
        for stream, count in conn.execute(
            "SELECT COALESCE(NULLIF(stream, ''), 'unknown'), COUNT(*) "
            "FROM atoms GROUP BY COALESCE(NULLIF(stream, ''), 'unknown')"
        ):
            metrics[f"atoms_stream_{_metric_key(stream)}"] = int(count)
        for memory_type, count in conn.execute(
            "SELECT COALESCE(NULLIF(memory_type, ''), 'unknown'), COUNT(*) "
            "FROM atoms GROUP BY COALESCE(NULLIF(memory_type, ''), 'unknown')"
        ):
            metrics[f"atoms_memory_type_{_metric_key(memory_type)}"] = int(count)
    except sqlite3.Error as exc:
        findings.append(_finding(
            "saga", "atoms-query", "warning", rel,
            f"Atom metrics could not be read: {exc}.",
            "Inspect the SAGA schema; this may be an old or partial migration.",
        ))


def _collect_saga_embedding_metrics(
    conn: sqlite3.Connection,
    home: Path,
    db_path: Path,
    findings: list[DoctorFinding],
    metrics: dict[str, int],
    tables: set[str],
) -> None:
    rel = _rel(home, db_path)
    try:
        metrics["embeddings_total"] = int(
            conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] or 0
        )
        for provider, dim, count in conn.execute(
            "SELECT COALESCE(NULLIF(provider, ''), 'unknown'), "
            "COALESCE(dim, -1), COUNT(*) FROM embeddings "
            "GROUP BY COALESCE(NULLIF(provider, ''), 'unknown'), COALESCE(dim, -1)"
        ):
            metrics[
                f"embeddings_provider_dim_{_metric_key(provider)}_{int(dim)}"
            ] = int(count)
        if "atoms" in tables:
            live_with, live_missing = conn.execute(
                "SELECT "
                "SUM(CASE WHEN e.atom_id IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN e.atom_id IS NULL THEN 1 ELSE 0 END) "
                "FROM atoms a LEFT JOIN embeddings e ON e.atom_id = a.id "
                "WHERE COALESCE(a.tombstoned, 0) = 0"
            ).fetchone()
            orphan = conn.execute(
                "SELECT COUNT(*) FROM embeddings e "
                "LEFT JOIN atoms a ON a.id = e.atom_id WHERE a.id IS NULL"
            ).fetchone()[0]
            metrics["embeddings_live_atoms_covered"] = int(live_with or 0)
            metrics["embeddings_live_atoms_missing"] = int(live_missing or 0)
            metrics["embeddings_orphan_rows"] = int(orphan or 0)
            if live_missing:
                findings.append(_finding(
                    "saga", "missing-embeddings", "warning", rel,
                    f"{int(live_missing)} live SAGA atoms have no embedding row.",
                    "Reindex or repair outside doctor; doctor is read-only and will not embed.",
                ))
            if orphan:
                findings.append(_finding(
                    "saga", "orphan-embeddings", "warning", rel,
                    f"{int(orphan)} embedding rows do not reference an atom.",
                    "Inspect SAGA cleanup/migration history; doctor will not delete rows.",
                ))
    except sqlite3.Error as exc:
        findings.append(_finding(
            "saga", "embeddings-query", "warning", rel,
            f"Embedding metrics could not be read: {exc}.",
            "Inspect the SAGA schema; this may be an old or partial migration.",
        ))


def _collect_saga_triple_metrics(
    conn: sqlite3.Connection,
    home: Path,
    db_path: Path,
    findings: list[DoctorFinding],
    metrics: dict[str, int],
    tables: set[str],
) -> None:
    rel = _rel(home, db_path)
    try:
        total, live, tombstoned, embedded, missing_embedding = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN COALESCE(tombstoned, 0) = 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN COALESCE(tombstoned, 0) != 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN COALESCE(tombstoned, 0) = 0 AND embedding IS NULL THEN 1 ELSE 0 END) "
            "FROM triples"
        ).fetchone()
        metrics.update({
            "triples_total": int(total or 0),
            "triples_live": int(live or 0),
            "triples_tombstoned": int(tombstoned or 0),
            "triples_with_embedding": int(embedded or 0),
            "triples_live_missing_embedding": int(missing_embedding or 0),
        })
        if "atoms" in tables:
            source_live, source_missing, atoms_with_triples = conn.execute(
                "SELECT "
                "SUM(CASE WHEN COALESCE(t.tombstoned, 0) = 0 "
                " AND a.id IS NOT NULL AND COALESCE(a.tombstoned, 0) = 0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN COALESCE(t.tombstoned, 0) = 0 "
                " AND t.source_atom_id IS NOT NULL AND a.id IS NULL THEN 1 ELSE 0 END), "
                "COUNT(DISTINCT CASE WHEN COALESCE(t.tombstoned, 0) = 0 "
                " AND a.id IS NOT NULL AND COALESCE(a.tombstoned, 0) = 0 THEN a.id END) "
                "FROM triples t LEFT JOIN atoms a ON a.id = t.source_atom_id"
            ).fetchone()
            metrics["triples_with_live_source_atom"] = int(source_live or 0)
            metrics["triples_orphan_source_atom"] = int(source_missing or 0)
            metrics["atoms_with_live_triples"] = int(atoms_with_triples or 0)
            if source_missing:
                findings.append(_finding(
                    "saga", "orphan-triples", "warning", rel,
                    f"{int(source_missing)} triples reference a missing source atom.",
                    "Inspect SAGA migrations or cleanup; doctor will not rewrite triples.",
                ))
        if missing_embedding:
            findings.append(_finding(
                "saga", "missing-triple-embeddings", "warning", rel,
                f"{int(missing_embedding)} live triples have no embedding.",
                "Rebuild or re-embed outside doctor if triple semantic retrieval is expected.",
            ))
    except sqlite3.Error as exc:
        findings.append(_finding(
            "saga", "triples-query", "warning", rel,
            f"Triple metrics could not be read: {exc}.",
            "Inspect the SAGA schema; this may be an old or partial migration.",
        ))


def _collect_saga_access_metrics(
    conn: sqlite3.Connection,
    home: Path,
    db_path: Path,
    findings: list[DoctorFinding],
    metrics: dict[str, int],
    tables: set[str],
) -> None:
    rel = _rel(home, db_path)
    try:
        if "access_events" in tables:
            metrics["access_events_total"] = int(
                conn.execute("SELECT COUNT(*) FROM access_events").fetchone()[0] or 0
            )
            for source, count in conn.execute(
                "SELECT COALESCE(NULLIF(source, ''), 'unknown'), COUNT(*) "
                "FROM access_events GROUP BY COALESCE(NULLIF(source, ''), 'unknown')"
            ):
                metrics[f"access_events_source_{_metric_key(source)}"] = int(count)
            if "atoms" in tables:
                orphan = conn.execute(
                    "SELECT COUNT(*) FROM access_events ae "
                    "LEFT JOIN atoms a ON a.id = ae.atom_id WHERE a.id IS NULL"
                ).fetchone()[0]
                metrics["access_events_orphan_rows"] = int(orphan or 0)
                if orphan:
                    findings.append(_finding(
                        "saga", "orphan-access-events", "warning", rel,
                        f"{int(orphan)} access events reference missing atoms.",
                        "Inspect SAGA migrations or cleanup; doctor will not delete events.",
                    ))
        if "atom_access_summary" in tables:
            metrics["access_summary_rows"] = int(
                conn.execute("SELECT COUNT(*) FROM atom_access_summary").fetchone()[0] or 0
            )
            if "atoms" in tables:
                missing, orphan = conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM atoms a LEFT JOIN atom_access_summary s "
                    " ON s.atom_id = a.id WHERE COALESCE(a.tombstoned, 0) = 0 AND s.atom_id IS NULL), "
                    "(SELECT COUNT(*) FROM atom_access_summary s LEFT JOIN atoms a "
                    " ON a.id = s.atom_id WHERE a.id IS NULL)"
                ).fetchone()
                metrics["access_summary_live_atoms_missing"] = int(missing or 0)
                metrics["access_summary_orphan_rows"] = int(orphan or 0)
                if missing:
                    findings.append(_finding(
                        "saga", "missing-access-summary", "warning", rel,
                        f"{int(missing)} live atoms have no access summary row.",
                        "Activation can fall back to access_events, but summary refresh should be inspected.",
                    ))
                if orphan:
                    findings.append(_finding(
                        "saga", "orphan-access-summary", "warning", rel,
                        f"{int(orphan)} access summary rows reference missing atoms.",
                        "Inspect SAGA migrations or cleanup; doctor will not delete summaries.",
                    ))
    except sqlite3.Error as exc:
        findings.append(_finding(
            "saga", "access-query", "warning", rel,
            f"Access-event metrics could not be read: {exc}.",
            "Inspect the SAGA schema; this may be an old or partial migration.",
        ))


def _collect_forget_preview_metrics(home: Path, metrics: dict[str, int]) -> None:
    try:
        from .feedback import pending_forget_candidates_count

        pending = pending_forget_candidates_count(home / "logs" / "events.jsonl")
    except Exception:  # noqa: BLE001 - doctor should not fail on optional logs
        pending = None
    metrics["forget_candidates_preview_available"] = int(pending is not None)
    metrics["forget_candidates_pending"] = int(pending or 0)


def _metric_key(value: object) -> str:
    text = str(value if value is not None else "unknown").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "unknown"
def _check_state(home: Path, findings: list[DoctorFinding]) -> DoctorSection:
    state_root = home / "state"
    if not state_root.is_dir():
        return DoctorSection("state", {
            "exists": 0,
            "index_exists": 0,
            "index_stale": 0,
            "unexpected_top_level_md_files": 0,
            "spec_open_plan_files": 0,
            "spec_old_open_plan_files": 0,
            "spec_old_days": STATE_SPEC_OLD_DAYS,
        })

    index_path = state_root / "INDEX.md"
    rendered = build_state_index(home)
    index_exists = index_path.is_file()
    index_stale = 0
    if index_exists:
        index_stale = int(_read_text(index_path) != rendered)
        if index_stale:
            findings.append(_finding(
                "state", "index-stale", "warning", _rel(home, index_path),
                "state/INDEX.md differs from the current rendered state index.",
                "Let the normal index flush regenerate it after the next state change.",
            ))
    else:
        findings.append(_finding(
            "state", "index-missing", "warning", _rel(home, index_path),
            "state/INDEX.md is missing.",
            "Let the normal index flush regenerate it.",
        ))

    unexpected = [
        path for path in _md_files(state_root, recursive=False)
        if path.name not in _ALLOWED_TOP_LEVEL_STATE_MD
    ]
    for path in unexpected[:TOP_EXAMPLES]:
        findings.append(_finding(
            "state", "top-level-md", "warning", _rel(home, path),
            "Unexpected top-level state markdown file.",
            "Move durable knowledge under state/wiki/, state/raw/, or another named subtree.",
        ))

    open_plans, old_open_plans = _state_spec_plan_files(home)
    for path in old_open_plans[:TOP_EXAMPLES]:
        findings.append(_finding(
            "state", "old-spec-plan", "info", _rel(home, path),
            f"Open state/spec plan is at least {STATE_SPEC_OLD_DAYS} days old.",
            "Decide whether to archive it under state/spec/archive/ or promote durable content to the wiki.",
        ))

    return DoctorSection("state", {
        "exists": 1,
        "index_exists": int(index_exists),
        "index_stale": index_stale,
        "rendered_index_bytes": len(rendered.encode("utf-8")),
        "unexpected_top_level_md_files": len(unexpected),
        "spec_open_plan_files": len(open_plans),
        "spec_old_open_plan_files": len(old_open_plans),
        "spec_old_days": STATE_SPEC_OLD_DAYS,
    })


def _check_wiki(home: Path, findings: list[DoctorFinding]) -> DoctorSection:
    wiki_root = home / "state" / "wiki"
    if not wiki_root.is_dir():
        return DoctorSection("wiki", {
            "exists": 0,
            "index_exists": 0,
            "index_stale": 0,
            "pages": 0,
            "orphans": 0,
            "dangling_links": 0,
            "slug_collisions": 0,
        })

    index_path = wiki_root / "index.md"
    rendered = build_wiki_index(home)
    index_exists = index_path.is_file()
    index_stale = 0
    if index_exists:
        index_stale = int(_read_text(index_path) != rendered)
        if index_stale:
            findings.append(_finding(
                "wiki", "index-stale", "warning", _rel(home, index_path),
                "state/wiki/index.md differs from the current rendered wiki index.",
                "Let the normal wiki index flush regenerate it after the next wiki change.",
            ))
    else:
        findings.append(_finding(
            "wiki", "index-missing", "warning", _rel(home, index_path),
            "state/wiki/index.md is missing.",
            "Let the normal wiki index flush regenerate it.",
        ))

    graph = build_graph(wiki_root)
    for path_str in graph.orphans[:TOP_EXAMPLES]:
        findings.append(_finding(
            "wiki", "orphan", "warning", f"state/wiki/{path_str}",
            "Wiki page has no inbound wikilinks.",
            "Add an inbound link from a related page, merge it, or intentionally leave it documented.",
        ))
    for item in graph.dangling[:TOP_EXAMPLES]:
        source = str(item.get("source") or "")
        target = str(item.get("target") or "")
        line = int(item.get("line") or 0)
        findings.append(_finding(
            "wiki", "dangling-link", "warning", f"state/wiki/{source}",
            f"Wikilink [[{target}]] on line {line} does not resolve to a page.",
            "Create the target page or correct the wikilink target.",
        ))
    for slug, paths in sorted(graph.collisions.items())[:TOP_EXAMPLES]:
        rendered_paths = ", ".join(path.as_posix() for path in paths)
        first = paths[0].as_posix() if paths else ""
        findings.append(_finding(
            "wiki", "slug-collision", "warning", f"state/wiki/{first}",
            f"Slug '{slug}' is shared by multiple wiki pages: {rendered_paths}.",
            "Rename one page so wikilinks resolve unambiguously for readers.",
        ))

    return DoctorSection("wiki", {
        "exists": 1,
        "index_exists": int(index_exists),
        "index_stale": index_stale,
        "rendered_index_bytes": len(rendered.encode("utf-8")),
        "pages": len(graph.pages),
        "orphans": len(graph.orphans),
        "dangling_links": len(graph.dangling),
        "slug_collisions": len(graph.collisions),
    })


def _render_memory_index_readonly(home: Path) -> str:
    memory_root = home / "memory"
    entries: list[IndexEntry] = []
    for path in _md_files(memory_root, recursive=True):
        rel = path.relative_to(memory_root).as_posix()
        if rel == "INDEX.md":
            continue
        text = _read_text(path)
        desc, is_auto = describe_file(text)
        entries.append(IndexEntry(
            rel_path=rel,
            description=desc,
            is_auto=is_auto,
            is_core=rel.startswith("core/"),
        ))
    return render_memory_index(entries)


def _state_spec_plan_files(home: Path) -> tuple[list[Path], list[Path]]:
    spec_root = home / "state" / "spec"
    if not spec_root.is_dir():
        return [], []
    now = datetime.now(timezone.utc).timestamp()
    old_after_seconds = STATE_SPEC_OLD_DAYS * 24 * 60 * 60
    open_plans: list[Path] = []
    old_open_plans: list[Path] = []
    for path in sorted(spec_root.rglob("*.md")):
        rel_parts = path.relative_to(spec_root).parts
        if rel_parts and rel_parts[0] == "archive":
            continue
        if not _is_open_spec_plan(path):
            continue
        open_plans.append(path)
        try:
            age_seconds = now - path.stat().st_mtime
        except OSError:
            continue
        if age_seconds >= old_after_seconds:
            old_open_plans.append(path)
    return open_plans, old_open_plans


def _is_open_spec_plan(path: Path) -> bool:
    name = path.name.lower()
    if "decision" in name:
        return False
    return "plan" in name or "spec" in name


def _issue_duplicate_key(path: Path, text: str) -> str:
    desc = extract_desc_comment(text)
    normalized = _normalize_text(text)
    if desc and normalized:
        return "desc-body:" + _normalize_text(desc) + "|" + normalized
    if desc:
        return "desc:" + _normalize_text(desc)
    if len(normalized) >= 40:
        return "body:" + normalized
    stem = _normalize_text(path.stem)
    return "stem:" + stem if stem else ""


def _normalize_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("<!--") and "desc:" in stripped:
            continue
        if stripped.startswith("#"):
            continue
        lines.append(stripped.lower())
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _md_files(root: Path, *, recursive: bool) -> list[Path]:
    if not root.is_dir():
        return []
    iterator = root.rglob("*.md") if recursive else root.glob("*.md")
    return sorted(path for path in iterator if path.is_file())


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _rel(home: Path, path: Path) -> str:
    try:
        return path.relative_to(home).as_posix()
    except ValueError:
        return path.as_posix()


def _finding(
    layer: str,
    check: str,
    severity: str,
    path: str,
    message: str,
    suggestion: str,
) -> DoctorFinding:
    return DoctorFinding(
        layer=layer,
        check=check,
        severity=severity,
        path=path,
        message=message,
        suggestion=suggestion,
    )
