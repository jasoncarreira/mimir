"""Tests for the ``mimir reindex`` utility — atom + file_search migration
under a switched embedding provider.

The reindex utility walks a saga or file_search DB, finds rows whose
embedding BLOB length doesn't match the currently-configured provider's
dimension, re-embeds via the current provider, and writes back. These
tests cover the detection + dry-run + apply paths against in-memory
synthetic DBs (no real provider calls).
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from mimir.reindex import (
    ReindexReport,
    _expected_blob_len,
    reindex_file_search,
    reindex_saga_atoms,
)


# ─── Synthetic DB fixtures ──────────────────────────────────────────


def _pack(vec: list[float]) -> bytes:
    """Pack a float vector as little-endian float32 — matches saga's
    embedding pack_embedding."""
    return b"".join(struct.pack("<f", float(v)) for v in vec)


def _make_atoms_db(path: Path, rows: list[tuple[str, str, list[float] | None]]) -> None:
    """Create a saga-shaped atoms DB with (id, content, embedding) rows.
    None embedding → row is included but its blob is NULL (won't be
    re-embedded; matches saga's WHERE embedding IS NOT NULL filter)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE atoms (id TEXT PRIMARY KEY, content TEXT, embedding BLOB)"
    )
    for atom_id, content, vec in rows:
        blob = _pack(vec) if vec is not None else None
        conn.execute(
            "INSERT INTO atoms (id, content, embedding) VALUES (?, ?, ?)",
            (atom_id, content, blob),
        )
    conn.commit()
    conn.close()


def _make_chunks_db(path: Path, rows: list[tuple[str, str, list[float]]]) -> None:
    """Create a mimir file_search-shaped DB with the chunks table only
    (no files table needed — the reindex only touches chunks).

    Rows are (path, content, vec); chunk_index defaults to 0 each.
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE chunks ("
        "path TEXT NOT NULL, chunk_index INTEGER NOT NULL, "
        "content TEXT NOT NULL, embedding BLOB NOT NULL, "
        "PRIMARY KEY (path, chunk_index))"
    )
    for path_str, content, vec in rows:
        conn.execute(
            "INSERT INTO chunks (path, chunk_index, content, embedding) "
            "VALUES (?, 0, ?, ?)",
            (path_str, content, _pack(vec)),
        )
    conn.commit()
    conn.close()


# ─── Stub the saga provider ─────────────────────────────────────────


class _StubProvider:
    """Deterministic provider for tests — produces ``dim``-length vectors
    of (text_hash + 0.001 * index) so each text gets a unique embedding
    we can verify on read-back."""

    def __init__(self, dim: int):
        self._dim = dim

    def dimensions(self) -> int:
        return self._dim

    def batch_embed(self, texts, input_type="passage"):
        del input_type
        out = []
        for t in texts:
            seed = sum(ord(c) for c in (t or "")) % 1000 / 1000.0
            out.append([seed + 0.001 * i for i in range(self._dim)])
        return out


@pytest.fixture
def patch_provider(monkeypatch):
    """Inject a stub provider with a chosen dimension."""
    def _install(dim: int, provider_name: str = "openai"):
        import mimir.memory.embeddings as saga_emb
        import mimir.memory._config_io as saga_cfg
        stub = _StubProvider(dim)
        monkeypatch.setattr(saga_emb, "get_provider", lambda: stub)
        # Also patch the config read for provider name
        snap = {"embedding": {"provider": provider_name}}
        monkeypatch.setattr(saga_cfg, "_config", snap)
        monkeypatch.setattr(saga_cfg, "_config_loaded", True)
        return stub
    return _install


# ─── Tests ──────────────────────────────────────────────────────────


def test_expected_blob_len_is_4_bytes_per_dim():
    assert _expected_blob_len(384) == 1536
    assert _expected_blob_len(1024) == 4096
    assert _expected_blob_len(1536) == 6144


def test_atoms_delegates_to_saga_calibration_dry_run(tmp_path, patch_provider, monkeypatch):
    """``reindex_saga_atoms`` in dry-run mode calls saga.calibration.
    re_embed(dry_run=True) and maps the result dict to ReindexReport.

    This is the per-mimir-review-feedback change: instead of mimir
    duplicating saga's atom-walking logic, delegate to saga's
    canonical re_embed which handles state filter + embedding_provider
    column + FAISS dirty flag.
    """
    patch_provider(dim=1024, provider_name="voyage")
    import mimir.memory.calibration as _mm_cal

    captured: dict = {}

    def fake_re_embed(db_path, *, target_provider_name, batch_size=50, dry_run=False):
        captured["db_path"] = db_path
        captured["provider"] = target_provider_name
        captured["batch_size"] = batch_size
        captured["dry_run"] = dry_run
        return {
            "target_provider": target_provider_name,
            "atoms_total": 10,
            "atoms_updated": 0,
            "dry_run": True,
            "index_rebuild_needed": True,
        }

    monkeypatch.setattr(_mm_cal, "re_embed", fake_re_embed)

    report = reindex_saga_atoms(tmp_path / "saga.db", dry_run=True)
    assert captured["provider"] == "voyage"
    assert captured["batch_size"] == 50
    assert captured["dry_run"] is True
    assert captured["db_path"] == tmp_path / "saga.db"
    assert report.total_rows == 10
    assert report.needs_reindex == 10
    assert report.reindexed == 0
    assert report.provider == "voyage"
    assert report.dimension == 1024


def test_atoms_delegates_to_saga_calibration_apply(tmp_path, patch_provider, monkeypatch):
    """In apply mode, mimir.memory.calibration.re_embed returns atoms_updated
    > 0 and mimir's reindex maps that to ``reindexed`` in the report."""
    patch_provider(dim=1024, provider_name="voyage")
    import mimir.memory.calibration as _mm_cal

    def fake_re_embed(db_path, *, target_provider_name, batch_size=50, dry_run=False):
        return {
            "target_provider": target_provider_name,
            "atoms_total": 7,
            "atoms_updated": 7,
            "dry_run": False,
            "index_rebuild_needed": True,
        }

    monkeypatch.setattr(_mm_cal, "re_embed", fake_re_embed)

    report = reindex_saga_atoms(tmp_path / "saga.db", dry_run=False)
    assert report.total_rows == 7
    assert report.reindexed == 7
    assert report.failed == 0


def test_atoms_handles_saga_calibration_exception(tmp_path, patch_provider, monkeypatch):
    """If mimir.memory.calibration.re_embed raises, mimir's reindex counts a
    failure rather than crashing — operator sees the error in the
    report instead of an uncaught traceback."""
    patch_provider(dim=1024)
    import mimir.memory.calibration as _mm_cal

    def fake_re_embed(**kwargs):
        raise RuntimeError("simulated saga failure")

    monkeypatch.setattr(_mm_cal, "re_embed", fake_re_embed)

    report = reindex_saga_atoms(tmp_path / "saga.db", dry_run=False)
    assert report.failed == 1
    assert report.reindexed == 0


def test_file_search_reindex_same_path(tmp_path, patch_provider):
    """file_search reindex hits the chunks table with the same logic."""
    patch_provider(dim=1024, provider_name="openai")
    db = tmp_path / "index.db"
    _make_chunks_db(db, [
        ("memory/topics/a.md", "alpha content", [0.0] * 384),   # old dim
        ("memory/topics/b.md", "beta content", [0.0] * 1024),   # current
    ])
    report = reindex_file_search(db, dry_run=False)
    assert report.target == "files"
    assert report.already_current == 1
    assert report.needs_reindex == 1
    assert report.reindexed == 1

    conn = sqlite3.connect(str(db))
    blob_lens = [r[0] for r in conn.execute(
        "SELECT length(embedding) FROM chunks ORDER BY path"
    ).fetchall()]
    conn.close()
    assert all(b == _expected_blob_len(1024) for b in blob_lens)


def test_file_search_missing_db_returns_empty_report(tmp_path, patch_provider):
    """``reindex_file_search`` on a non-existent DB returns a zero-
    count report — useful for ``--target both`` against a home that
    only has one of the two DBs initialized."""
    patch_provider(dim=1024)
    db = tmp_path / "nonexistent.db"
    report = reindex_file_search(db, dry_run=True)
    assert report.total_rows == 0
    assert report.needs_reindex == 0
    assert report.db_path == db


# The previous tests (resumable, batch-failure on atoms) covered
# mimir's own atom-walking implementation. Those behaviors now live
# in saga's calibration.re_embed; we no longer test them at this
# layer — saga owns its own test coverage. mimir's tests focus on
# the delegation contract: provider name passed correctly, return
# shape mapped to ReindexReport, exception isolation.
