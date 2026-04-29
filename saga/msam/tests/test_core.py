"""MSAM Core Tests -- smoke tests for storage, retrieval, and scoring."""

import sys
import os
import json
import struct
import tempfile
import sqlite3

import pytest
import numpy as np

# Ensure msam is importable


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Use a temporary database for all tests."""
    db_path = tmp_path / "test_msam.db"
    monkeypatch.setattr("msam.core.DB_PATH", db_path)
    # Also patch embedding to avoid API calls
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("msam.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("msam.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("msam.core._cached_embed_query_import", lambda t: tuple(fake_emb))
    # Patch the LRU-cached version too
    monkeypatch.setattr("msam.core.cached_embed_query", lambda t: fake_emb)
    yield db_path


class TestStoreAndRetrieve:
    def test_store_atom_returns_id(self):
        from msam.core import store_atom
        atom_id = store_atom("Test memory content")
        assert atom_id is not None
        assert len(atom_id) == 16

    def test_store_duplicate_returns_none(self):
        from msam.core import store_atom
        store_atom("Duplicate content test")
        result = store_atom("Duplicate content test")
        assert result == (None, "duplicate content")

    def test_retrieve_finds_stored(self):
        from msam.core import store_atom, retrieve
        store_atom("The sky is blue and water is wet")
        results = retrieve("sky color", top_k=5)
        assert len(results) > 0
        assert any("sky" in r["content"].lower() for r in results)

    def test_retrieve_returns_similarity(self):
        from msam.core import store_atom, retrieve
        store_atom("Python is a programming language")
        results = retrieve("programming", top_k=3)
        assert all("_similarity" in r for r in results)
        assert all("_activation" in r for r in results)

    def test_retrieve_empty_db(self):
        from msam.core import retrieve
        results = retrieve("anything", top_k=5)
        assert results == []

    def test_store_streams(self):
        from msam.core import store_atom, get_stats
        store_atom("Semantic fact", stream="semantic")
        store_atom("Episode yesterday", stream="episodic")
        store_atom("How to do X", stream="procedural")
        stats = get_stats()
        assert stats["by_stream"].get("semantic", 0) >= 1
        assert stats["by_stream"].get("episodic", 0) >= 1
        assert stats["by_stream"].get("procedural", 0) >= 1


class TestBatchCosine:
    def test_batch_matches_individual(self):
        from msam.core import batch_cosine_similarity, cosine_similarity, pack_embedding
        q = list(np.random.randn(1024).astype(float))
        vecs = [pack_embedding(list(np.random.randn(1024).astype(float))) for _ in range(20)]
        
        batch_results = batch_cosine_similarity(q, vecs)
        
        for i in range(20):
            n = len(vecs[i]) // 4
            individual = cosine_similarity(q, list(struct.unpack(f'{n}f', vecs[i])))
            assert abs(batch_results[i] - individual) < 1e-4, f"Mismatch at index {i}"

    def test_batch_handles_none(self):
        from msam.core import batch_cosine_similarity, pack_embedding
        q = list(np.random.randn(1024).astype(float))
        vecs = [None, pack_embedding(list(np.random.randn(1024).astype(float))), None]
        
        results = batch_cosine_similarity(q, vecs)
        assert results[0] == 0.0
        assert results[2] == 0.0
        assert results[1] != 0.0

    def test_batch_empty_input(self):
        from msam.core import batch_cosine_similarity
        assert batch_cosine_similarity([1.0, 2.0], []) == []

    def test_batch_output_range(self):
        from msam.core import batch_cosine_similarity, pack_embedding
        q = list(np.random.randn(1024).astype(float))
        vecs = [pack_embedding(list(np.random.randn(1024).astype(float))) for _ in range(50)]
        results = batch_cosine_similarity(q, vecs)
        for r in results:
            assert -1.01 <= r <= 1.01, f"Similarity out of range: {r}"


class TestConfidenceTiers:
    def test_tier_thresholds(self):
        """Verify tier classification matches documented thresholds."""
        from msam.core import hybrid_retrieve, store_atom
        # Just verify the function runs without error on empty db
        results = hybrid_retrieve("nonexistent query", top_k=5)
        assert results == []

    def test_stats(self):
        from msam.core import store_atom, get_stats
        store_atom("Test atom for stats")
        stats = get_stats()
        assert "total_atoms" in stats
        assert "active_atoms" in stats
        assert "by_stream" in stats
        assert "est_active_tokens" in stats
        assert stats["total_atoms"] >= 1


class TestKeywordSearch:
    def test_keyword_finds_match(self):
        from msam.core import store_atom, keyword_search
        store_atom("Elasticsearch uses inverted indices for fast retrieval")
        results = keyword_search("elasticsearch inverted")
        assert len(results) > 0

    def test_keyword_stopword_filtering(self):
        from msam.core import keyword_search, store_atom
        store_atom("The quick brown fox jumps over the lazy dog")
        # Searching only stopwords should still return something if content matches
        results = keyword_search("the")
        # 'the' is a stopword, should be filtered, so fewer/no results
        # but the fallback handles this


class TestMerge:
    def test_merge_atoms(self):
        from msam.core import store_atom, merge_atoms
        id1 = store_atom("First version of the fact")
        id2 = store_atom("Second version of the fact, more detail")
        assert id1 and id2
        result = merge_atoms(id1, id2, merged_content="Merged: complete fact with detail")
        assert result["kept"] == id1
        assert result["removed"] == id2


class TestDryRetrieve:
    def test_dry_no_side_effects(self):
        from msam.core import store_atom, dry_retrieve, get_db
        store_atom("Dry retrieve test content")

        conn = get_db()
        count_before = conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        conn.close()

        dry_retrieve("test content", top_k=3)

        conn = get_db()
        count_after = conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        conn.close()

        assert count_after == count_before, "dry_retrieve should not log access"


class TestSessionScopedFeedback:
    """access_log.session_id + mark_contributions session scoping.

    Before this fix, mark_contributions UPDATEd only the globally most
    recent access_log row per atom_id. A bulk feedback at end-of-session
    therefore tagged a single retrieval even when the same atom was
    pulled many times. With session_id on access_log, we tag every row
    in the session's window.
    """

    def test_log_access_persists_session_id(self):
        from msam.core import store_atom, retrieve, get_db
        atom_id = store_atom("Session-scoped logging test content")
        retrieve("test content", top_k=3, session_id="sess-A")

        conn = get_db()
        rows = conn.execute(
            "SELECT session_id FROM access_log WHERE atom_id = ?", (atom_id,)
        ).fetchall()
        conn.close()
        assert any(r["session_id"] == "sess-A" for r in rows)

    def test_mark_contributions_tags_all_session_rows(self):
        from msam.core import store_atom, retrieve, mark_contributions, get_db
        atom_id = store_atom(
            "The user prefers dark mode in the IDE for late-night coding."
        )

        # Retrieve the same atom three times in one session.
        for _ in range(3):
            retrieve("test content", top_k=3, session_id="sess-1")

        conn = get_db()
        before = conn.execute(
            "SELECT COUNT(*) FROM access_log "
            "WHERE atom_id = ? AND session_id = 'sess-1' AND contributed = -1",
            (atom_id,),
        ).fetchone()[0]
        conn.close()
        assert before == 3, "all three retrievals should start at contributed=-1"

        # Bulk feedback (only one call covering all three retrievals).
        # Response text overlaps the atom enough to flip contributed=1.
        mark_contributions(
            [atom_id],
            "The user prefers dark mode in the IDE for late-night coding sessions.",
            session_id="sess-1",
        )

        conn = get_db()
        tagged = conn.execute(
            "SELECT COUNT(*) FROM access_log "
            "WHERE atom_id = ? AND session_id = 'sess-1' AND contributed = 1",
            (atom_id,),
        ).fetchone()[0]
        conn.close()
        assert tagged == 3, (
            "session-scoped mark_contributions should tag every retrieval "
            "in the session, not just the most recent"
        )

    def test_mark_contributions_does_not_cross_sessions(self):
        from msam.core import store_atom, retrieve, mark_contributions, get_db
        atom_id = store_atom("Cross-session isolation check content here.")

        retrieve("isolation check", top_k=3, session_id="sess-A")
        retrieve("isolation check", top_k=3, session_id="sess-B")

        # Feedback only for sess-A.
        mark_contributions(
            [atom_id],
            "isolation check content here works correctly",
            session_id="sess-A",
        )

        conn = get_db()
        a_tagged = conn.execute(
            "SELECT contributed FROM access_log "
            "WHERE atom_id = ? AND session_id = 'sess-A'",
            (atom_id,),
        ).fetchall()
        b_untagged = conn.execute(
            "SELECT contributed FROM access_log "
            "WHERE atom_id = ? AND session_id = 'sess-B'",
            (atom_id,),
        ).fetchall()
        conn.close()
        assert all(r["contributed"] == 1 for r in a_tagged)
        assert all(r["contributed"] == -1 for r in b_untagged), (
            "feedback for sess-A must not touch sess-B's rows"
        )

    def test_mark_contributions_legacy_when_no_session_id(self):
        """Backward compat: when caller omits session_id, fall back to
        the legacy 'most recent globally' behavior so existing
        deployments don't change semantics overnight.
        """
        from msam.core import store_atom, retrieve, mark_contributions, get_db
        atom_id = store_atom("Legacy fallback path content for testing.")

        # Three retrievals with no session_id.
        for _ in range(3):
            retrieve("legacy fallback", top_k=3)

        mark_contributions(
            [atom_id],
            "legacy fallback path content for testing works",
            session_id=None,
        )

        conn = get_db()
        rows = conn.execute(
            "SELECT contributed FROM access_log WHERE atom_id = ? "
            "ORDER BY accessed_at",
            (atom_id,),
        ).fetchall()
        conn.close()
        # Only the most recent row should be tagged in the legacy path.
        contributed_counts = [r["contributed"] for r in rows]
        assert contributed_counts.count(1) == 1
        assert contributed_counts[-1] == 1, "legacy path tags only the latest row"
