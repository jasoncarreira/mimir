"""MSAM Sub-Atom Tests -- sentence-level extraction and deduplication."""

import struct

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
    # Also patch embeddings module for subatom
    monkeypatch.setattr("msam.embeddings.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("msam.embeddings.batch_embed_texts",
                        lambda texts: [fake_emb] * len(texts))
    yield db_path


class TestSplitSentences:
    def test_basic_split(self):
        from msam.subatom import split_sentences
        # Realistic conversational sentence lengths (>30 chars each).
        text = (
            "I really enjoyed the concert last weekend. "
            "The band played all my favorite songs from the new album. "
            "We hung around afterwards to meet the lead guitarist."
        )
        sentences = split_sentences(text)
        assert len(sentences) >= 2

    def test_empty_input(self):
        from msam.subatom import split_sentences
        assert split_sentences("") == []
        assert split_sentences("   ") == []

    def test_single_sentence(self):
        from msam.subatom import split_sentences
        sentences = split_sentences("Just a single sentence without any period or break")
        assert len(sentences) == 1

    def test_newline_split(self):
        from msam.subatom import split_sentences
        text = (
            "First paragraph has substantive content about the topic.\n\n"
            "Second paragraph discusses something else entirely but at length."
        )
        sentences = split_sentences(text)
        assert len(sentences) >= 2

    def test_list_block_stays_grouped(self):
        """P46: a header followed by ≥2 bullets becomes ONE chunk, not
        N chunks. Prevents fragments like '* Foo' from competing for
        top-K against actual sentences."""
        from msam.subatom import split_sentences
        text = (
            "Here are some tips:\n\n"
            "1. **Lighting**:\n"
            "* Natural light is ideal, place desk near a window if possible.\n"
            "* Avoid harsh overhead lighting which can cause eye strain.\n"
            "* Use a desk lamp for focused task lighting."
        )
        chunks = split_sentences(text)
        # Header+bullets block should be one chunk, not 4 (header + 3 bullets)
        list_chunk = [c for c in chunks if "Natural light" in c and "harsh overhead" in c]
        assert len(list_chunk) == 1, (
            f"list block split into separate chunks: {chunks}"
        )

    def test_short_fragments_filtered(self):
        """P46: chunks under 30 chars (markdown headers, single-bullet
        formatting) get filtered out. The 30-char floor catches '1.
        **Lighting**:' style fragments."""
        from msam.subatom import split_sentences
        text = (
            "Real sentence with substantive content goes here.\n\n"
            "TODO\n\n"
            "1. **Header**:\n\n"
            "Another real sentence with enough characters to count."
        )
        chunks = split_sentences(text)
        for c in chunks:
            assert len(c) >= 30, f"fragment {c!r} should have been filtered"

    def test_short_atom_falls_through(self):
        """If filtering would wipe the chunk list, the longest chunk
        survives so short atoms still appear in the index."""
        from msam.subatom import split_sentences
        text = "Hi there."
        chunks = split_sentences(text)
        assert chunks == ["Hi there."]

    def test_prose_with_inline_list_still_splits_prose(self):
        """A prose paragraph is sentence-split as before; only blocks
        that look list-shaped (≥2 list markers) bypass sentence-split."""
        from msam.subatom import split_sentences
        text = (
            "First substantive sentence about the topic at hand. "
            "Second sentence elaborates with additional context here."
        )
        chunks = split_sentences(text)
        assert len(chunks) == 2  # both sentences survive 30-char filter


class TestDeduplicate:
    def test_removes_similar(self):
        from msam.subatom import deduplicate_sentences

        # Create sentences with the same fake embedding (they'll be "identical")
        sentences = [
            {"atom_id": "a1", "sentence": "User is a software engineer", "score": 0.9, "tokens": 6},
            {"atom_id": "a2", "sentence": "User works in software engineering", "score": 0.7, "tokens": 6},
        ]
        # With fake embeddings both will have cosine sim ~1.0
        result = deduplicate_sentences(sentences, similarity_threshold=0.5)
        # Should keep the higher-scored one
        assert len(result) == 1
        assert result[0]["score"] == 0.9


class TestExtractRelevantSentences:
    def test_returns_scored_sentences(self):
        from msam.core import get_db, run_migrations, store_atom
        from msam.subatom import extract_relevant_sentences

        conn = get_db()
        run_migrations(conn)
        conn.close()

        atom_id = store_atom(
            "Agent Identity: Curious and analytical personality. "
            "Values growth and authenticity. Professional in technical discussions."
        )

        atoms = [{
            "id": atom_id,
            "content": "Agent Identity: Curious and analytical personality. "
                       "Values growth and authenticity. Professional in technical discussions.",
            "_combined_score": 5.0,
        }]

        results = extract_relevant_sentences("agent personality", atoms, token_budget=200)
        assert len(results) >= 1
        assert "sentence" in results[0]
        assert "score" in results[0]
        assert "tokens" in results[0]


class TestPackUnpackEmbedding:
    def test_round_trip(self):
        from msam.subatom import _pack_embedding, _unpack_embedding
        original = [0.1, 0.2, 0.3, -0.5, 1.0]
        packed = _pack_embedding(original)
        unpacked = _unpack_embedding(packed)
        assert len(unpacked) == 5
        for a, b in zip(original, unpacked):
            assert abs(a - b) < 1e-6

    def test_empty(self):
        from msam.subatom import _pack_embedding, _unpack_embedding
        packed = _pack_embedding([])
        unpacked = _unpack_embedding(packed)
        assert unpacked == []


class TestEstimateTokens:
    def test_basic_estimate(self):
        from msam.subatom import _estimate_tokens
        # "hello world" = 11 chars → ~2-3 tokens
        tokens = _estimate_tokens("hello world")
        assert tokens >= 1
        assert tokens <= 5

    def test_empty_returns_one(self):
        from msam.subatom import _estimate_tokens
        assert _estimate_tokens("") == 1


class TestCacheSentenceEmbeddings:
    def test_caches_and_skips_existing(self):
        from msam.core import get_db, run_migrations, store_atom
        from msam.subatom import cache_sentence_embeddings

        # Realistic conversational sentence lengths so the splitter's
        # 30-char floor doesn't merge them into a single chunk.
        text = (
            "I picked up a Sony WH-1000XM5 last weekend at the BestBuy. "
            "Setup with the iPhone 15 Pro went smoothly and I'm enjoying them. "
            "The noise cancellation works really well on the morning train."
        )

        conn = get_db()
        run_migrations(conn)
        conn.close()

        atom_id = store_atom(text)

        conn = get_db()
        count1 = cache_sentence_embeddings(atom_id, text, conn)
        assert count1 >= 2

        # Second call should skip (already cached)
        count2 = cache_sentence_embeddings(atom_id, text, conn)
        assert count2 == count1  # returns existing count
        conn.close()


class TestCacheAllSentences:
    def test_caches_all_active(self):
        from msam.core import get_db, run_migrations, store_atom
        from msam.subatom import cache_all_sentences

        conn = get_db()
        run_migrations(conn)
        conn.close()

        # Each atom needs ≥2 sentences with ≥30 chars each.
        store_atom(
            "I have been working on the new project all morning. "
            "Progress is steady but the test suite is taking forever."
        )
        store_atom(
            "Met with Alex about the system migration timeline today. "
            "We agreed to push the rollout by another two weeks."
        )

        result = cache_all_sentences()
        assert result["cached"] >= 2
        assert result["sentences"] >= 4
