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
        import saga.embeddings as saga_emb
        import saga.config as saga_cfg
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


def test_dry_run_reports_mismatched_rows(tmp_path, patch_provider):
    """Dry-run identifies rows whose blob length != current dim * 4
    and reports counts without writing."""
    patch_provider(dim=1024, provider_name="voyage")
    db = tmp_path / "saga.db"
    _make_atoms_db(db, [
        ("a", "first atom", [0.1] * 1024),   # current dim — already current
        ("b", "second atom", [0.2] * 384),   # old dim — needs reindex
        ("c", "third atom", [0.3] * 1536),   # other old dim — needs reindex
    ])
    report = reindex_saga_atoms(db, dry_run=True)
    assert report.total_rows == 3
    assert report.already_current == 1
    assert report.needs_reindex == 2
    assert report.reindexed == 0
    assert report.provider == "voyage"
    assert report.dimension == 1024
    assert report.estimated_input_chars > 0


def test_apply_rewrites_mismatched_blobs(tmp_path, patch_provider):
    """Apply mode re-embeds candidates via the stub provider and writes
    1024-dim BLOBs."""
    patch_provider(dim=1024, provider_name="voyage")
    db = tmp_path / "saga.db"
    _make_atoms_db(db, [
        ("a", "first", [0.1] * 1024),
        ("b", "second", [0.2] * 384),
        ("c", "third", [0.3] * 1536),
    ])

    report = reindex_saga_atoms(db, dry_run=False, batch_size=10)
    assert report.needs_reindex == 2
    assert report.reindexed == 2
    assert report.failed == 0

    # Verify on-disk blobs are now all 1024 dims.
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT id, length(embedding) FROM atoms ORDER BY id"
    ).fetchall()
    conn.close()
    for atom_id, blob_len in rows:
        assert blob_len == _expected_blob_len(1024), \
            f"atom {atom_id} has length {blob_len}, expected 4096"


def test_reindex_skips_already_current(tmp_path, patch_provider):
    """When all rows match the current dim, nothing gets re-embedded."""
    patch_provider(dim=1024)
    db = tmp_path / "saga.db"
    _make_atoms_db(db, [
        ("a", "first", [0.1] * 1024),
        ("b", "second", [0.2] * 1024),
    ])
    report = reindex_saga_atoms(db, dry_run=False)
    assert report.already_current == 2
    assert report.needs_reindex == 0
    assert report.reindexed == 0


def test_reindex_missing_db_returns_empty_report(tmp_path, patch_provider):
    """A non-existent DB doesn't crash — returns a zero-count report.
    Useful for ``--target both`` against a home that only has one of
    the two DBs initialized."""
    patch_provider(dim=1024)
    db = tmp_path / "nonexistent.db"
    report = reindex_saga_atoms(db, dry_run=True)
    assert report.total_rows == 0
    assert report.needs_reindex == 0
    assert report.db_path == db


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


def test_resumable_after_partial_run(tmp_path, patch_provider):
    """Running reindex twice should be idempotent — the second run finds
    all rows current and does nothing. Simulates a recovery from a
    crash-mid-batch scenario."""
    patch_provider(dim=1024)
    db = tmp_path / "saga.db"
    _make_atoms_db(db, [
        ("a", "first", [0.1] * 384),
        ("b", "second", [0.2] * 384),
        ("c", "third", [0.3] * 384),
    ])
    # First run — re-embeds all 3.
    r1 = reindex_saga_atoms(db, dry_run=False)
    assert r1.reindexed == 3

    # Second run — all 3 are now current; nothing to do.
    r2 = reindex_saga_atoms(db, dry_run=False)
    assert r2.already_current == 3
    assert r2.needs_reindex == 0
    assert r2.reindexed == 0


def test_provider_failure_counts_failures_continues(tmp_path, patch_provider, monkeypatch):
    """If a batch_embed call raises, the rows in that batch are counted
    as failures but the overall reindex doesn't crash — subsequent
    batches still process."""
    patch_provider(dim=1024)
    db = tmp_path / "saga.db"
    _make_atoms_db(db, [
        ("a", "first", [0.1] * 384),
        ("b", "second", [0.2] * 384),
        ("c", "third", [0.3] * 384),
        ("d", "fourth", [0.4] * 384),
    ])

    # Patch the provider so first batch raises, rest succeed.
    import saga.embeddings as saga_emb
    stub = saga_emb.get_provider()
    call_count = [0]
    real_batch = stub.batch_embed

    def flaky(texts, input_type="passage"):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("simulated API failure")
        return real_batch(texts, input_type=input_type)

    monkeypatch.setattr(stub, "batch_embed", flaky)

    report = reindex_saga_atoms(db, dry_run=False, batch_size=2)
    # First batch (a, b) fails; second batch (c, d) succeeds.
    assert report.failed == 2
    assert report.reindexed == 2
    assert report.needs_reindex == 4
