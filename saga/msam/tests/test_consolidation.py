"""MSAM Consolidation Tests -- sleep-inspired memory consolidation."""

import struct
import hashlib
from datetime import datetime, timezone

import pytest
import numpy as np


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Use a temporary database for all tests."""
    db_path = tmp_path / "test_msam.db"
    monkeypatch.setattr("msam.core.DB_PATH", db_path)
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("msam.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("msam.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("msam.core._cached_embed_query_import", lambda t: tuple(fake_emb))
    monkeypatch.setattr("msam.core.cached_embed_query", lambda t: fake_emb)
    yield db_path


def _store_atoms_with_same_embedding(conn, ids, contents, embedding):
    """Store atoms that will cluster together (same embedding)."""
    for atom_id, content in zip(ids, contents):
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:32]
        conn.execute("""
            INSERT OR IGNORE INTO atoms (id, content, content_hash, created_at, state,
                is_pinned, embedding, topics, metadata, encoding_confidence, stream,
                profile, access_count, stability)
            VALUES (?, ?, ?, datetime('now'), 'active', 0, ?, '[]', '{}', 0.7,
                'semantic', 'standard', 0, 1.0)
        """, (atom_id, content, content_hash, embedding))
    conn.commit()


class TestEngineInit:
    def test_defaults(self):
        from msam.consolidation import ConsolidationEngine
        engine = ConsolidationEngine()
        assert engine.similarity_threshold > 0
        assert engine.min_cluster_size >= 2
        assert engine.max_clusters > 0
        assert engine.stability_reduction > 0


class TestClusterBruteForce:
    def test_groups_similar(self):
        from msam.core import get_db, run_migrations
        from msam.consolidation import ConsolidationEngine

        conn = get_db()
        run_migrations(conn)

        # Use the same embedding for all atoms so they cluster
        same_emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        atoms = []
        for i in range(5):
            atom_id = f"clust_{i}"
            content = f"Similar content about topic {i}"
            atoms.append({"id": atom_id, "content": content, "stream": "semantic",
                          "embedding": same_emb, "access_count": 0, "topics": "[]",
                          "is_pinned": 0})

        _store_atoms_with_same_embedding(
            conn,
            [a["id"] for a in atoms],
            [a["content"] for a in atoms],
            same_emb,
        )
        conn.close()

        engine = ConsolidationEngine(similarity_threshold=0.5, min_cluster_size=3)
        clusters = engine._cluster_brute_force(atoms)
        assert len(clusters) >= 1
        assert len(clusters[0]) >= 3


class TestConsolidate:
    def test_dry_run(self):
        from msam.core import get_db, run_migrations
        from msam.consolidation import ConsolidationEngine

        conn = get_db()
        run_migrations(conn)

        same_emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        _store_atoms_with_same_embedding(
            conn,
            [f"dry_{i}" for i in range(5)],
            [f"Dry run content about topic {i}" for i in range(5)],
            same_emb,
        )
        conn.close()

        engine = ConsolidationEngine(similarity_threshold=0.5, min_cluster_size=3)
        result = engine.consolidate(dry_run=True)
        assert result["dry_run"] is True
        assert "clusters_found" in result
        assert "clusters" in result

    def test_skips_pinned(self):
        from msam.core import get_db, run_migrations
        from msam.consolidation import ConsolidationEngine

        conn = get_db()
        run_migrations(conn)

        same_emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        # Store all as pinned
        for i in range(5):
            content = f"Pinned content {i}"
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:32]
            conn.execute("""
                INSERT OR IGNORE INTO atoms (id, content, content_hash, created_at, state,
                    is_pinned, embedding, topics, metadata, encoding_confidence, stream,
                    profile, access_count, stability)
                VALUES (?, ?, ?, datetime('now'), 'active', 1, ?, '[]', '{}', 0.7,
                    'semantic', 'standard', 0, 1.0)
            """, (f"pin_{i}", content, content_hash, same_emb))
        conn.commit()
        conn.close()

        engine = ConsolidationEngine(similarity_threshold=0.5, min_cluster_size=3)
        result = engine.consolidate(dry_run=True)
        assert result["clusters_found"] == 0

    def test_empty_db(self):
        from msam.core import get_db, run_migrations
        from msam.consolidation import ConsolidationEngine

        conn = get_db()
        run_migrations(conn)
        conn.close()

        engine = ConsolidationEngine()
        result = engine.consolidate(dry_run=True)
        assert result["clusters_found"] == 0


def _atom_with_emb(atom_id: str, vec: list[float], stream: str = "semantic") -> dict:
    """Build an atom dict with a packed embedding for merge_phase tests."""
    return {
        "id": atom_id,
        "stream": stream,
        "embedding": struct.pack(f'{len(vec)}f', *vec),
    }


class TestMergePhase:
    """P8: agglomerative merge pass over greedy clusters."""

    def test_empty_passthrough(self):
        from msam.consolidation import ConsolidationEngine
        engine = ConsolidationEngine()
        assert engine._merge_phase([]) == []

    def test_single_cluster_passthrough(self):
        from msam.consolidation import ConsolidationEngine
        engine = ConsolidationEngine()
        c = [_atom_with_emb("a", [1.0, 0.0, 0.0])]
        assert engine._merge_phase([c]) == [c]

    def test_close_centroids_merge(self):
        from msam.consolidation import ConsolidationEngine
        engine = ConsolidationEngine(merge_threshold=0.75, max_cluster_size=10)
        # Two clusters whose centroids are essentially identical.
        c1 = [_atom_with_emb("a1", [1.0, 0.0, 0.0]),
              _atom_with_emb("a2", [0.99, 0.05, 0.0])]
        c2 = [_atom_with_emb("b1", [0.98, 0.0, 0.05]),
              _atom_with_emb("b2", [1.0, 0.02, 0.0])]
        merged = engine._merge_phase([c1, c2])
        assert len(merged) == 1
        assert len(merged[0]) == 4

    def test_distant_centroids_stay_split(self):
        from msam.consolidation import ConsolidationEngine
        engine = ConsolidationEngine(merge_threshold=0.75, max_cluster_size=10)
        # Orthogonal centroids -> cosine 0, well below 0.75.
        c1 = [_atom_with_emb("a1", [1.0, 0.0, 0.0]),
              _atom_with_emb("a2", [1.0, 0.0, 0.0])]
        c2 = [_atom_with_emb("b1", [0.0, 1.0, 0.0]),
              _atom_with_emb("b2", [0.0, 1.0, 0.0])]
        merged = engine._merge_phase([c1, c2])
        assert len(merged) == 2

    def test_different_streams_dont_merge(self):
        from msam.consolidation import ConsolidationEngine
        engine = ConsolidationEngine(merge_threshold=0.75, max_cluster_size=10)
        c1 = [_atom_with_emb("a1", [1.0, 0.0, 0.0], stream="semantic"),
              _atom_with_emb("a2", [1.0, 0.0, 0.0], stream="semantic")]
        c2 = [_atom_with_emb("b1", [1.0, 0.0, 0.0], stream="episodic"),
              _atom_with_emb("b2", [1.0, 0.0, 0.0], stream="episodic")]
        merged = engine._merge_phase([c1, c2])
        assert len(merged) == 2

    def test_max_cluster_size_caps_merge(self):
        from msam.consolidation import ConsolidationEngine
        # Cap is below the combined size, so merge should be skipped.
        engine = ConsolidationEngine(merge_threshold=0.75, max_cluster_size=3)
        c1 = [_atom_with_emb(f"a{i}", [1.0, 0.0, 0.0]) for i in range(2)]
        c2 = [_atom_with_emb(f"b{i}", [1.0, 0.0, 0.0]) for i in range(2)]
        merged = engine._merge_phase([c1, c2])
        assert len(merged) == 2  # 2+2=4 > cap 3, so they don't combine

    def test_chain_merge_collapses_three(self):
        from msam.consolidation import ConsolidationEngine
        engine = ConsolidationEngine(merge_threshold=0.75, max_cluster_size=20)
        # Three close clusters; pairs merge, then merged centroid still
        # close to remaining cluster, so all three collapse.
        c1 = [_atom_with_emb(f"a{i}", [1.0, 0.0, 0.0]) for i in range(2)]
        c2 = [_atom_with_emb(f"b{i}", [0.99, 0.05, 0.0]) for i in range(2)]
        c3 = [_atom_with_emb(f"c{i}", [0.98, 0.0, 0.05]) for i in range(2)]
        merged = engine._merge_phase([c1, c2, c3])
        assert len(merged) == 1
        assert len(merged[0]) == 6

    def test_disabled_via_engine_flag(self):
        from msam.consolidation import ConsolidationEngine
        engine = ConsolidationEngine(enable_merge_pass=False)
        # Even with close centroids, merge pass shouldn't run if disabled.
        # We verify by calling _merge_phase directly returns input untouched
        # only when called -- consolidate() is what gates on the flag.
        # So instead: assert the flag is plumbed correctly.
        assert engine.enable_merge_pass is False
