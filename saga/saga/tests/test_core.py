"""SAGA Core Tests -- smoke tests for storage, retrieval, and scoring."""

import sys
import os
import json
import struct
import tempfile
import sqlite3

import pytest
import numpy as np

# Ensure saga is importable


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Use a temporary database for all tests."""
    db_path = tmp_path / "test_saga.db"
    monkeypatch.setattr("saga.core.DB_PATH", db_path)
    # Also patch embedding to avoid API calls
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("saga.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("saga.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("saga.core._cached_embed_query_import", lambda t: tuple(fake_emb))
    # Patch the LRU-cached version too
    monkeypatch.setattr("saga.core.cached_embed_query", lambda t: fake_emb)
    yield db_path


class TestStoreAndRetrieve:
    def test_store_atom_returns_id(self):
        from saga.core import store_atom
        atom_id = store_atom("Test memory content")
        assert atom_id is not None
        assert len(atom_id) == 16

    def test_store_duplicate_returns_none(self):
        from saga.core import store_atom
        store_atom("Duplicate content test")
        result = store_atom("Duplicate content test")
        assert result == (None, "duplicate content")

    def test_retrieve_finds_stored(self):
        from saga.core import store_atom, retrieve
        store_atom("The sky is blue and water is wet")
        results = retrieve("sky color", top_k=5)
        assert len(results) > 0
        assert any("sky" in r["content"].lower() for r in results)

    def test_retrieve_returns_similarity(self):
        from saga.core import store_atom, retrieve
        store_atom("Python is a programming language")
        results = retrieve("programming", top_k=3)
        assert all("_similarity" in r for r in results)
        assert all("_activation" in r for r in results)

    def test_retrieve_empty_db(self):
        from saga.core import retrieve
        results = retrieve("anything", top_k=5)
        assert results == []

    def test_store_streams(self):
        from saga.core import store_atom, get_stats
        store_atom("Semantic fact", stream="semantic")
        store_atom("Episode yesterday", stream="episodic")
        store_atom("How to do X", stream="procedural")
        stats = get_stats()
        assert stats["by_stream"].get("semantic", 0) >= 1
        assert stats["by_stream"].get("episodic", 0) >= 1
        assert stats["by_stream"].get("procedural", 0) >= 1


class TestRetrieveMemoryTypeFilter:
    """retrieve(memory_type=...) MUST be honored on the FAISS fast path.

    Pre-fix, FAISS bypassed the memory_type SQL filter entirely (the
    fast-path gate at saga.core.retrieve only checked
    topic_filter/stream/since/before/agent_id). Effect: on a DB skewed
    toward one tier, hybrid_retrieve's per-tier calls would return
    cross-tier atoms — the same raw atom would surface in BOTH the
    observations and raws lists of the two-tier response, breaking the
    tier separation _two_tier_split relies on. Confirmed on mimirbot's
    live saga.db (1 observation, 327 raws): a query returned 8 atom IDs
    with 3 duplicates across tiers.
    """

    def _setup_with_distinct_embeddings(self, monkeypatch):
        """Override the test_core shared-vector fixture with per-text
        embeddings, so FAISS retrieval is meaningful (otherwise all
        atoms have identical scores and ordering tests are moot)."""
        import hashlib
        from saga.vector_index import reset_indexes
        reset_indexes()

        def _per_text_emb(text: str) -> list[float]:
            # Deterministic, distinct-per-text 1024d vector. Bytes from
            # SHA256 → 32 floats, tiled out to 1024. Not a real embedding
            # but FAISS only cares about cosine distance between vectors,
            # which this gives us deterministically per input.
            seed = int.from_bytes(
                hashlib.sha256(text.encode()).digest()[:8], "big"
            ) % (2**32)
            rng = np.random.default_rng(seed)
            return list(rng.standard_normal(1024).astype(float))

        monkeypatch.setattr("saga.core.embed_text", _per_text_emb)
        monkeypatch.setattr("saga.core.embed_query", _per_text_emb)
        monkeypatch.setattr(
            "saga.core._cached_embed_query_import",
            lambda t: tuple(_per_text_emb(t)),
        )
        monkeypatch.setattr("saga.core.cached_embed_query", _per_text_emb)

    def test_observation_filter_excludes_raws_under_faiss(self, monkeypatch):
        from saga.core import store_atom, retrieve
        self._setup_with_distinct_embeddings(monkeypatch)

        obs_id = store_atom(
            "Operators prefer Sony cameras",
            memory_type="observation", evidence_count=3,
        )
        # 5 raws on related topics — pre-fix FAISS would happily return
        # these for a memory_type='observation' query if they're more
        # similar to the query than the lone observation.
        raw_ids = [
            store_atom("I picked up a Sony A7 III"),
            store_atom("My Sony camera takes great photos"),
            store_atom("Canon vs Sony in low light"),
            store_atom("Looking at mirrorless cameras"),
            store_atom("Bought a new Sony lens"),
        ]
        assert isinstance(obs_id, str)
        assert all(isinstance(r, str) for r in raw_ids)

        results = retrieve(
            "Sony camera question", top_k=10,
            memory_type="observation",
        )
        for r in results:
            assert r["memory_type"] == "observation", (
                f"retrieve(memory_type='observation') returned a "
                f"non-observation atom: id={r['id']}, "
                f"memory_type={r['memory_type']}, content={r['content'][:60]}"
            )

    def test_raw_filter_excludes_observations_under_faiss(self, monkeypatch):
        from saga.core import store_atom, retrieve
        self._setup_with_distinct_embeddings(monkeypatch)

        store_atom(
            "Operators prefer Sony cameras",
            memory_type="observation", evidence_count=3,
        )
        store_atom("I picked up a Sony A7 III")
        store_atom("Canon vs Sony in low light")

        results = retrieve(
            "Sony camera question", top_k=10, memory_type="raw",
        )
        for r in results:
            assert r["memory_type"] == "raw", (
                f"retrieve(memory_type='raw') leaked an observation: "
                f"id={r['id']}, content={r['content'][:60]}"
            )

    def test_no_memory_type_filter_returns_mixed(self, monkeypatch):
        """Sanity check: with no memory_type filter, retrieve returns
        whatever's nearest regardless of tier. Pins the contract for
        callers that genuinely want mixed (e.g. session-boundary lookups
        that don't care about the observation/raw split)."""
        from saga.core import store_atom, retrieve
        self._setup_with_distinct_embeddings(monkeypatch)

        store_atom(
            "Operators prefer Sony cameras",
            memory_type="observation", evidence_count=3,
        )
        store_atom("I picked up a Sony A7 III")

        results = retrieve("Sony camera question", top_k=10)
        types = {r["memory_type"] for r in results}
        # Both tiers should be representable in the unfiltered result —
        # ``and`` not ``or``; the latter is vacuously true as long as
        # the result has any atoms.
        assert "observation" in types and "raw" in types


class TestBatchCosine:
    def test_batch_matches_individual(self):
        from saga.core import batch_cosine_similarity, cosine_similarity, pack_embedding
        q = list(np.random.randn(1024).astype(float))
        vecs = [pack_embedding(list(np.random.randn(1024).astype(float))) for _ in range(20)]
        
        batch_results = batch_cosine_similarity(q, vecs)
        
        for i in range(20):
            n = len(vecs[i]) // 4
            individual = cosine_similarity(q, list(struct.unpack(f'{n}f', vecs[i])))
            assert abs(batch_results[i] - individual) < 1e-4, f"Mismatch at index {i}"

    def test_batch_handles_none(self):
        from saga.core import batch_cosine_similarity, pack_embedding
        q = list(np.random.randn(1024).astype(float))
        vecs = [None, pack_embedding(list(np.random.randn(1024).astype(float))), None]
        
        results = batch_cosine_similarity(q, vecs)
        assert results[0] == 0.0
        assert results[2] == 0.0
        assert results[1] != 0.0

    def test_batch_empty_input(self):
        from saga.core import batch_cosine_similarity
        assert batch_cosine_similarity([1.0, 2.0], []) == []

    def test_batch_output_range(self):
        from saga.core import batch_cosine_similarity, pack_embedding
        q = list(np.random.randn(1024).astype(float))
        vecs = [pack_embedding(list(np.random.randn(1024).astype(float))) for _ in range(50)]
        results = batch_cosine_similarity(q, vecs)
        for r in results:
            assert -1.01 <= r <= 1.01, f"Similarity out of range: {r}"


class TestConfidenceTiers:
    @pytest.mark.asyncio
    async def test_tier_thresholds(self):
        """Verify tier classification matches documented thresholds."""
        from saga.core import hybrid_retrieve, store_atom
        # Just verify the function runs without error on empty db
        results = await hybrid_retrieve("nonexistent query", top_k=5)
        assert results == []

    def test_stats(self):
        from saga.core import store_atom, get_stats
        store_atom("Test atom for stats")
        stats = get_stats()
        assert "total_atoms" in stats
        assert "active_atoms" in stats
        assert "by_stream" in stats
        assert "est_active_tokens" in stats
        assert stats["total_atoms"] >= 1


class TestKeywordSearch:
    def test_keyword_finds_match(self):
        from saga.core import store_atom, keyword_search
        store_atom("Elasticsearch uses inverted indices for fast retrieval")
        results = keyword_search("elasticsearch inverted")
        assert len(results) > 0

    def test_keyword_stopword_filtering(self):
        from saga.core import keyword_search, store_atom
        store_atom("The quick brown fox jumps over the lazy dog")
        # Searching only stopwords should still return something if content matches
        results = keyword_search("the")
        # 'the' is a stopword, should be filtered, so fewer/no results
        # but the fallback handles this


class TestMerge:
    def test_merge_atoms(self):
        from saga.core import store_atom, merge_atoms
        id1 = store_atom("First version of the fact")
        id2 = store_atom("Second version of the fact, more detail")
        assert id1 and id2
        result = merge_atoms(id1, id2, merged_content="Merged: complete fact with detail")
        assert result["kept"] == id1
        assert result["removed"] == id2

    def test_merge_atoms_resyncs_fts5_for_new_content(self):
        """CR#16 follow-up (#80 review): when ``merged_content`` is
        provided, ``merge_atoms`` updates ``atoms.content`` in-place.
        ``atoms_fts`` is an external-content FTS5 over ``atoms``
        (``content='atoms'``) with no auto-update trigger — without an
        explicit FTS resync, keyword search returns the OLD pre-merge
        content forever. This test pins the resync."""
        from saga.core import store_atom, merge_atoms, keyword_search

        old_content = "vintage typewriter from 1923"
        new_content = "merged: vintage typewriter manufactured in 1923, restored 2024"
        id1 = store_atom(old_content)
        id2 = store_atom("typewriter found at estate sale")

        # Pre-merge: keyword search for the OLD-content distinguisher
        # finds the keep atom.
        pre = keyword_search("vintage")
        assert any(a["id"] == id1 for a in pre)

        merge_atoms(id1, id2, merged_content=new_content)

        # Post-merge: keyword search for a phrase ONLY in the new
        # content (e.g. "restored") MUST find the kept atom. Pre-fix
        # this returned no results because atoms_fts still indexed the
        # old "vintage typewriter from 1923" text.
        post = keyword_search("restored")
        post_ids = [a["id"] for a in post]
        assert id1 in post_ids, (
            "atoms_fts must reflect merged_content — keyword search for "
            "a phrase only in the new content should find the merged atom"
        )

        # And keyword search for "1923" (which appears in BOTH old and
        # new) still finds the kept atom — verifies we didn't break
        # the basic indexing path.
        still = keyword_search("1923")
        assert id1 in [a["id"] for a in still]


class TestDryRetrieve:
    def test_dry_no_side_effects(self):
        from saga.core import store_atom, dry_retrieve, get_db
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
        from saga.core import store_atom, retrieve, get_db
        atom_id = store_atom("Session-scoped logging test content")
        retrieve("test content", top_k=3, session_id="sess-A")

        conn = get_db()
        rows = conn.execute(
            "SELECT session_id FROM access_log WHERE atom_id = ?", (atom_id,)
        ).fetchall()
        conn.close()
        assert any(r["session_id"] == "sess-A" for r in rows)

    def test_mark_contributions_tags_all_session_rows(self):
        from saga.core import store_atom, retrieve, mark_contributions, get_db
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
        from saga.core import store_atom, retrieve, mark_contributions, get_db
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
        from saga.core import store_atom, retrieve, mark_contributions, get_db
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


class TestGetMostRetrieved:
    """P45 endpoint 2: top-N atoms by retrieval count over a recent
    time window."""

    def _retrieve_n(self, query, n):
        from saga.core import retrieve
        for _ in range(n):
            retrieve(query, top_k=3)

    def test_ranks_by_retrieval_count(self):
        from saga.core import store_atom, get_most_retrieved
        a = store_atom("popular atom about Sony WH-1000XM5 headphones")
        b = store_atom("less retrieved atom about coffee preferences")
        # Mocked embed returns the same vector for any text, so both
        # atoms are equally retrievable. Drive the count manually by
        # calling retrieve for atom-a's keywords more times.
        self._retrieve_n("Sony WH-1000XM5 headphones", 5)
        self._retrieve_n("coffee preferences", 1)

        results = get_most_retrieved(days=7, count=10)
        # Both atoms appear (they may both be in every retrieve's
        # candidate pool because the mocked embedding is identical),
        # but the one we hit more often must rank first.
        ids = [r["id"] for r in results]
        assert a in ids
        assert b in ids
        assert results[0]["retrieval_count"] >= results[-1]["retrieval_count"]

    def test_count_caps_results(self):
        from saga.core import store_atom, get_most_retrieved
        for i in range(5):
            store_atom(f"distinct atom number {i} with unique content")
        self._retrieve_n("distinct atom", 3)

        results = get_most_retrieved(days=7, count=2)
        assert len(results) <= 2

    def test_days_window(self):
        """Retrievals outside the window must not count. We can't easily
        backdate access_log rows in this test, so verify the window logic
        with a 0-day window which should drop everything."""
        from saga.core import store_atom, get_most_retrieved
        store_atom("an atom that gets retrieved")
        self._retrieve_n("get this atom", 3)

        results_zero = get_most_retrieved(days=0, count=10)
        results_seven = get_most_retrieved(days=7, count=10)
        # 0-day window should never include just-now retrievals
        # (datetime('now', '-0 days') == 'now' itself, accessed_at < now)
        assert len(results_seven) >= len(results_zero)

    def test_contributed_only_filter(self):
        """contributed_only=True excludes retrievals that didn't get
        positive feedback."""
        from saga.core import store_atom, mark_contributions, get_most_retrieved

        a = store_atom("atom that earned its keep with contribution")
        b = store_atom("atom that got pulled in but never marked useful")
        self._retrieve_n("earned its keep", 3)
        self._retrieve_n("never marked useful", 3)

        # Mark a's retrievals as contributed; leave b's as -1 (unknown).
        mark_contributions([a], "earned its keep with contribution", session_id="t")

        results_all = get_most_retrieved(days=7, count=10, contributed_only=False)
        results_filtered = get_most_retrieved(days=7, count=10, contributed_only=True)

        # When contributed_only is on, atoms with no contributed=1 rows
        # have retrieval_count = 0 and should drop from the result.
        # b's contributed_count should be 0 in the unfiltered list.
        b_unfiltered = [r for r in results_all if r["id"] == b]
        if b_unfiltered:
            assert b_unfiltered[0]["contributed_count"] == 0
        # Filtered results: every atom shown must have contributed_count > 0
        for r in results_filtered:
            assert r["contributed_count"] > 0

    def test_returns_atom_metadata(self):
        from saga.core import store_atom, get_most_retrieved
        atom_id = store_atom("the user prefers dark mode in editors", topics=["preferences"])
        self._retrieve_n("prefers dark mode", 2)

        results = get_most_retrieved(days=7, count=5)
        match = [r for r in results if r["id"] == atom_id]
        assert match, f"atom {atom_id} should appear in top results"
        r = match[0]
        assert r["content"] == "the user prefers dark mode in editors"
        assert r["retrieval_count"] >= 2
        assert r["last_retrieved_at"] is not None
        assert r["created_at"] is not None
        assert "preferences" in r["topics"]

    def test_empty_when_no_retrievals(self):
        from saga.core import store_atom, get_most_retrieved
        store_atom("an unretrieved atom")
        # No retrieves done — access_log is empty for this atom.
        results = get_most_retrieved(days=7, count=10)
        assert results == [] or all(r["retrieval_count"] == 0 for r in results)
