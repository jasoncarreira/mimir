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


class TestSkipExistingObservation:
    """Idempotence: cluster with same source set as an existing observation
    must not trigger another LLM call."""

    def test_existing_observation_helper_finds_match(self):
        from msam.core import get_db, run_migrations, add_atom_relation
        from msam.consolidation import ConsolidationEngine

        conn = get_db()
        run_migrations(conn)
        # Set up an observation with evidenced_by edges to two raw atoms.
        same_emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        for aid in ("raw_a", "raw_b", "obs_x"):
            content = f"content {aid}"
            ch = hashlib.sha256(content.encode()).hexdigest()[:32]
            mt = "observation" if aid == "obs_x" else "raw"
            conn.execute("""
                INSERT INTO atoms (id, content, content_hash, created_at, state,
                    is_pinned, embedding, topics, metadata, encoding_confidence,
                    stream, profile, access_count, stability, memory_type)
                VALUES (?, ?, ?, datetime('now'), 'active', 0, ?, '[]', '{}', 0.7,
                    'semantic', 'standard', 0, 1.0, ?)
            """, (aid, content, ch, same_emb, mt))
        conn.commit()
        conn.close()

        add_atom_relation("obs_x", "raw_a", "evidenced_by", confidence=1.0)
        add_atom_relation("obs_x", "raw_b", "evidenced_by", confidence=1.0)

        engine = ConsolidationEngine()
        # Same source set → match
        assert engine._existing_observation_for_cluster(["raw_a", "raw_b"]) == "obs_x"
        # Same set, different order → match
        assert engine._existing_observation_for_cluster(["raw_b", "raw_a"]) == "obs_x"
        # Subset → no match (conservative: must be identical)
        assert engine._existing_observation_for_cluster(["raw_a"]) is None
        # Superset → no match (different cluster)
        assert engine._existing_observation_for_cluster(["raw_a", "raw_b", "raw_c"]) is None
        # Disjoint → no match
        assert engine._existing_observation_for_cluster(["raw_q", "raw_r"]) is None

    def test_existing_observation_helper_skips_tombstoned(self):
        from msam.core import get_db, run_migrations, add_atom_relation
        from msam.consolidation import ConsolidationEngine

        conn = get_db()
        run_migrations(conn)
        same_emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        for aid, state in (("raw_a", "active"), ("raw_b", "active"), ("obs_x", "tombstone")):
            content = f"content {aid}"
            ch = hashlib.sha256(content.encode()).hexdigest()[:32]
            mt = "observation" if aid == "obs_x" else "raw"
            conn.execute("""
                INSERT INTO atoms (id, content, content_hash, created_at, state,
                    is_pinned, embedding, topics, metadata, encoding_confidence,
                    stream, profile, access_count, stability, memory_type)
                VALUES (?, ?, ?, datetime('now'), ?, 0, ?, '[]', '{}', 0.7,
                    'semantic', 'standard', 0, 1.0, ?)
            """, (aid, content, ch, state, same_emb, mt))
        conn.commit()
        conn.close()

        add_atom_relation("obs_x", "raw_a", "evidenced_by", confidence=1.0)
        add_atom_relation("obs_x", "raw_b", "evidenced_by", confidence=1.0)

        engine = ConsolidationEngine()
        # Tombstoned observation should be ignored.
        assert engine._existing_observation_for_cluster(["raw_a", "raw_b"]) is None

    def test_consolidate_skips_existing(self, monkeypatch):
        """End-to-end: clusters_skipped_existing reports the count and the
        LLM is not called for an already-consolidated cluster."""
        from msam.core import get_db, run_migrations, add_atom_relation
        from msam.consolidation import ConsolidationEngine

        conn = get_db()
        run_migrations(conn)

        # Build a 4-atom cluster (all share the same embedding so they cluster).
        same_emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        cluster_ids = [f"raw_skip_{i}" for i in range(4)]
        for aid in cluster_ids:
            content = f"shared topic content {aid}"
            ch = hashlib.sha256(content.encode()).hexdigest()[:32]
            conn.execute("""
                INSERT INTO atoms (id, content, content_hash, created_at, state,
                    is_pinned, embedding, topics, metadata, encoding_confidence,
                    stream, profile, access_count, stability, memory_type)
                VALUES (?, ?, ?, datetime('now'), 'active', 0, ?, '[]', '{}', 0.7,
                    'semantic', 'standard', 0, 1.0, 'raw')
            """, (aid, content, ch, same_emb))

        # Pre-existing observation covering the exact same source set.
        obs_id = "obs_existing"
        obs_content = "[Consolidated from 4 atoms] shared topic"
        ch = hashlib.sha256(obs_content.encode()).hexdigest()[:32]
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                is_pinned, embedding, topics, metadata, encoding_confidence,
                stream, profile, access_count, stability, memory_type, evidence_count)
            VALUES (?, ?, ?, datetime('now'), 'active', 0, ?, '[]', '{}', 0.9,
                'semantic', 'standard', 0, 1.0, 'observation', 4)
        """, (obs_id, obs_content, ch, same_emb))
        conn.commit()
        conn.close()
        for src_id in cluster_ids:
            add_atom_relation(obs_id, src_id, "evidenced_by", confidence=1.0)

        # If the LLM is called, the test fails — track it.
        called = []
        import requests
        def fake_post(*a, **k):
            called.append((a, k))
            class _R:
                status_code = 200
                def json(self):
                    return {"choices": [{"message": {"content": "should not run"}}]}
            return _R()
        monkeypatch.setattr(requests, "post", fake_post)

        engine = ConsolidationEngine(similarity_threshold=0.5, min_cluster_size=3)
        result = engine.consolidate()
        assert result["clusters_found"] >= 1
        assert result["clusters_skipped_existing"] >= 1
        # No LLM calls — synthesis was skipped because the observation already exists.
        assert called == []
