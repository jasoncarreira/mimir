"""Tests for ``mimir.saga.calibration.re_embed``.

Coverage rationale: re_embed is the operator-facing ops tool for
re-embedding every live atom under a new provider. The function
operates against a real sqlite DB (no easy mock seam) so these tests
construct an in-memory DB seeded with the production schema, stub the
embedding provider via monkeypatch, and exercise both dry-run + apply
paths.

The schema-mismatch class of bug this file primarily guards: a prior
revision of ``re_embed`` issued ``UPDATE atoms SET embedding_dim = ?``
against the atoms table — but ``atoms`` has no ``embedding_dim``
column (it lives on ``triples``). The ``--apply`` path would crash
on first use. ``test_re_embed_apply_writes_embeddings_row`` exercises
the apply path end-to-end and would have caught that bug.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest


class _StubProvider:
    """Returns a deterministic dim-length vector for each input."""

    def __init__(self, dim: int):
        self._dim = dim
        self.batch_calls: list[tuple[list[str], str]] = []

    def dimensions(self) -> int:
        return self._dim

    def embed(self, text, *, input_type="passage"):
        seed = (sum(ord(c) for c in (text or "")) % 1000) / 1000.0
        return [seed + 0.001 * i for i in range(self._dim)]

    def batch_embed(self, texts, input_type="passage"):
        self.batch_calls.append((list(texts), input_type))
        return [self.embed(t) for t in texts]


def _install_stub_provider(monkeypatch: pytest.MonkeyPatch, dim: int = 4,
                            provider_name: str = "openai") -> _StubProvider:
    stub = _StubProvider(dim)
    monkeypatch.setattr(
        "mimir.saga.embeddings.get_provider", lambda: stub,
    )

    def _fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): provider_name,
                ("embedding", "model"): "stub-model",
            }.get((section, key), default)
        return cfg
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config", _fake_get_config,
    )
    return stub


def _seed_db(db_path: Path, atoms: list[tuple[str, str, int]]) -> None:
    """Seed an in-memory DB by writing schema.sql + inserting atoms.

    ``atoms`` is a list of (id, content, tombstoned) tuples.
    """
    schema_path = Path(__file__).resolve().parents[1] / "mimir" / "saga" / "schema.sql"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema_path.read_text())
    for atom_id, content, tomb in atoms:
        conn.execute(
            "INSERT INTO atoms (id, content, agent_id, content_hash, "
            "tombstoned, created_at) "
            "VALUES (?, ?, 'default', ?, ?, '2026-05-16T00:00:00+00:00')",
            (atom_id, content, atom_id, tomb),
        )
    conn.commit()
    conn.close()


def test_re_embed_dry_run_counts_only(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run reports atoms_total but doesn't write."""
    _install_stub_provider(monkeypatch)
    db = tmp_path / "saga.db"
    _seed_db(db, [
        ("atom1", "alpha content", 0),
        ("atom2", "beta content", 0),
        ("atom3", "tombstoned", 1),
    ])
    from mimir.saga.calibration import re_embed
    result = re_embed(db, dry_run=True)
    assert result["atoms_total"] == 2  # tombstoned excluded
    assert result["atoms_updated"] == 0
    assert result["dry_run"] is True
    assert result["index_rebuild_needed"] is True
    # No embeddings row should exist post-dry-run.
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    conn.close()


def test_re_embed_apply_writes_embeddings_row(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply path inserts one embeddings row per live atom with the
    target provider's dim. This is the test that catches the
    schema-vs-code mismatch class of bug — a prior revision did
    ``UPDATE atoms SET embedding_dim = ?`` against a column that
    doesn't exist on atoms, crashing here."""
    stub = _install_stub_provider(monkeypatch, dim=4, provider_name="openai")
    db = tmp_path / "saga.db"
    _seed_db(db, [
        ("atom1", "alpha content", 0),
        ("atom2", "beta content", 0),
    ])

    from mimir.saga.calibration import re_embed
    result = re_embed(db, dry_run=False)

    assert result["atoms_total"] == 2
    assert result["atoms_updated"] == 2
    assert result["dry_run"] is False
    assert result["index_rebuild_needed"] is True

    # Each atom should now have an embeddings row at dim=4 / provider=openai.
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT atom_id, provider, model, dim FROM embeddings ORDER BY atom_id"
    ).fetchall()
    assert rows == [
        ("atom1", "openai", "stub-model", 4),
        ("atom2", "openai", "stub-model", 4),
    ]
    # The stored vec should be 4 floats == 16 bytes per atom.
    blob_lens = [
        len(r[0]) for r in conn.execute("SELECT vec FROM embeddings").fetchall()
    ]
    assert blob_lens == [16, 16]
    conn.close()


def test_re_embed_skips_tombstoned(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Tombstoned atoms shouldn't burn embedding budget."""
    _install_stub_provider(monkeypatch)
    db = tmp_path / "saga.db"
    _seed_db(db, [
        ("live", "live content", 0),
        ("dead", "tombstoned content", 1),
    ])
    from mimir.saga.calibration import re_embed
    result = re_embed(db, dry_run=False)
    assert result["atoms_total"] == 1
    assert result["atoms_updated"] == 1
    conn = sqlite3.connect(str(db))
    atom_ids = [
        r[0] for r in conn.execute(
            "SELECT atom_id FROM embeddings"
        ).fetchall()
    ]
    assert atom_ids == ["live"]
    conn.close()


def test_re_embed_empty_db_returns_zero(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty corpus → atoms_total=0, no provider call, no rebuild needed."""
    stub = _install_stub_provider(monkeypatch)
    db = tmp_path / "saga.db"
    _seed_db(db, [])  # schema only, no atoms
    from mimir.saga.calibration import re_embed
    result = re_embed(db, dry_run=False)
    assert result["atoms_total"] == 0
    assert result["atoms_updated"] == 0
    assert result["index_rebuild_needed"] is False
    assert stub.batch_calls == []  # didn't even call the provider


def test_re_embed_target_provider_override(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """``target_provider_name`` overrides the TOML-resolved provider —
    useful for forced re-embeds without editing saga.toml."""
    _install_stub_provider(monkeypatch, dim=4, provider_name="voyage")
    db = tmp_path / "saga.db"
    _seed_db(db, [("a1", "content", 0)])
    from mimir.saga.calibration import re_embed
    # Override to "onnx" — that name should land in the embeddings row.
    result = re_embed(db, target_provider_name="onnx", dry_run=False)
    assert result["target_provider"] == "onnx"
    conn = sqlite3.connect(str(db))
    provider = conn.execute(
        "SELECT provider FROM embeddings"
    ).fetchone()[0]
    assert provider == "onnx"
    conn.close()
