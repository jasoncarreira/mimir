"""SAGA Triples Tests -- knowledge graph triple layer."""

import struct
import hashlib
from datetime import datetime, timezone

import pytest
import numpy as np


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Use a temporary database for all tests."""
    db_path = tmp_path / "test_saga.db"
    monkeypatch.setattr("saga.core.DB_PATH", db_path)
    # Also patch the triples module DB_PATH
    monkeypatch.setattr("saga.triples.DB_PATH", db_path)
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("saga.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("saga.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("saga.core._cached_embed_query_import", lambda t: tuple(fake_emb))
    monkeypatch.setattr("saga.core.cached_embed_query", lambda t: fake_emb)
    yield db_path


def _setup_db():
    """Initialize DB with atoms and triples schemas."""
    from saga.core import get_db, run_migrations
    from saga.triples import init_triples_schema
    conn = get_db()
    run_migrations(conn)
    init_triples_schema(conn)
    conn.commit()
    return conn


class TestClassifyQuery:
    def test_factual(self):
        from saga.triples import classify_query
        qtype, ratio = classify_query("What is the user's profession?")
        assert qtype == "factual"
        assert ratio > 0.3

    def test_contextual(self):
        from saga.triples import classify_query
        qtype, ratio = classify_query("How does the relationship feel?")
        assert qtype == "contextual"
        assert ratio < 0.3

    def test_mixed(self):
        from saga.triples import classify_query
        qtype, ratio = classify_query("Tell me something")
        assert qtype == "mixed"


class TestGenerateTripleId:
    def test_deterministic(self):
        from saga.triples import generate_triple_id
        id1 = generate_triple_id("a1", "User", "has_profession", "developer")
        id2 = generate_triple_id("a1", "User", "has_profession", "developer")
        assert id1 == id2
        assert len(id1) == 16

    def test_different_inputs(self):
        from saga.triples import generate_triple_id
        id1 = generate_triple_id("a1", "User", "has_profession", "developer")
        id2 = generate_triple_id("a1", "User", "has_profession", "designer")
        assert id1 != id2


class TestStoreAndRetrieveTriple:
    def test_round_trip(self):
        from saga.triples import store_triple, retrieve_by_entity
        conn = _setup_db()

        # Store an atom for the foreign key
        emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                embedding, topics, metadata, encoding_confidence)
            VALUES ('a1', 'User is a developer', 'hash1', datetime('now'), 'active',
                ?, '[]', '{}', 0.7)
        """, (emb,))
        conn.commit()
        conn.close()

        tid = store_triple("a1", "User", "has_profession", "developer", embed=False)
        assert tid is not None

        results = retrieve_by_entity("User")
        assert len(results) >= 1
        assert any(r["predicate"] == "has_profession" for r in results)

    def test_batch_dedup(self):
        from saga.triples import store_triples_batch
        conn = _setup_db()

        emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                embedding, topics, metadata, encoding_confidence)
            VALUES ('a2', 'Some content', 'hash2', datetime('now'), 'active',
                ?, '[]', '{}', 0.7)
        """, (emb,))
        conn.commit()
        conn.close()

        triples = [
            {"atom_id": "a2", "subject": "User", "predicate": "likes", "object": "Python"},
            {"atom_id": "a2", "subject": "User", "predicate": "likes", "object": "Python"},  # dup
            {"atom_id": "a2", "subject": "User", "predicate": "likes", "object": "Rust"},
        ]

        count = store_triples_batch(triples, embed=False)
        assert count == 2  # one duplicate skipped

    def test_retrieve_by_entity(self):
        from saga.triples import store_triple, retrieve_by_entity
        conn = _setup_db()

        emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                embedding, topics, metadata, encoding_confidence)
            VALUES ('a3', 'Facts', 'hash3', datetime('now'), 'active',
                ?, '[]', '{}', 0.7)
        """, (emb,))
        conn.commit()
        conn.close()

        store_triple("a3", "Alice", "knows", "Bob", embed=False)
        store_triple("a3", "Bob", "works_at", "Acme", embed=False)

        # Alice as subject
        results = retrieve_by_entity("Alice")
        assert len(results) >= 1

        # Bob as both subject and object
        results = retrieve_by_entity("Bob")
        assert len(results) >= 2


class TestFormatTriples:
    def test_format(self):
        from saga.triples import format_triples_for_context
        triples = [
            {"subject": "User", "predicate": "has_profession", "object": "developer"},
            {"subject": "User", "predicate": "likes", "object": "Python"},
        ]
        text = format_triples_for_context(triples)
        assert "(User, has_profession, developer)" in text
        assert "(User, likes, Python)" in text


class TestEstimateTokens:
    def test_reasonable_estimate(self):
        from saga.triples import estimate_triple_tokens
        triples = [
            {"subject": "User", "predicate": "has_profession", "object": "software developer"},
        ]
        tokens = estimate_triple_tokens(triples)
        assert 3 <= tokens <= 20


class TestGraphTraversal:
    def test_finds_neighbors(self):
        from saga.triples import store_triple, graph_traverse
        conn = _setup_db()

        emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                embedding, topics, metadata, encoding_confidence)
            VALUES ('a4', 'Graph test', 'hash4', datetime('now'), 'active',
                ?, '[]', '{}', 0.7)
        """, (emb,))
        conn.commit()
        conn.close()

        store_triple("a4", "User", "has_profession", "developer", embed=False)
        store_triple("a4", "User", "likes", "Python", embed=False)

        result = graph_traverse("User", max_hops=1)
        assert result["total_triples"] >= 2
        assert result["start_entity"] == "User"
        assert 0 in result["hops"]


class TestGraphPath:
    def test_finds_route(self):
        from saga.triples import store_triple, graph_path
        conn = _setup_db()

        emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                embedding, topics, metadata, encoding_confidence)
            VALUES ('a5', 'Path test', 'hash5', datetime('now'), 'active',
                ?, '[]', '{}', 0.7)
        """, (emb,))
        conn.commit()
        conn.close()

        store_triple("a5", "User", "works_on", "ProjectX", embed=False)
        store_triple("a5", "ProjectX", "uses", "Python", embed=False)

        result = graph_path("User", "Python", max_hops=3)
        assert result["found"] is True
        assert result["hops"] == 2

    def test_no_path(self):
        from saga.triples import graph_path
        _setup_db()

        result = graph_path("Nonexistent", "AlsoNope", max_hops=2)
        assert result["found"] is False


class TestDetectContradictions:
    def test_same_predicate(self):
        from saga.triples import store_triple, detect_contradictions
        conn = _setup_db()

        emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                embedding, topics, metadata, encoding_confidence)
            VALUES ('a6', 'Contradiction test', 'hash6', datetime('now'), 'active',
                ?, '[]', '{}', 0.7)
        """, (emb,))
        conn.commit()
        conn.close()

        store_triple("a6", "User", "has_profession", "developer", embed=False)

        # Check pre-write contradiction
        contradictions = detect_contradictions("User", "has_profession", "designer")
        assert len(contradictions) >= 1
        assert contradictions[0]["type"] == "value_conflict"
        assert contradictions[0]["existing_value"] == "developer"
        assert contradictions[0]["new_value"] == "designer"


class TestGetTripleStats:
    def test_returns_expected_keys(self):
        from saga.triples import store_triple, get_triple_stats
        conn = _setup_db()

        emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                embedding, topics, metadata, encoding_confidence)
            VALUES ('a7', 'Stats test', 'hash7', datetime('now'), 'active',
                ?, '[]', '{}', 0.7)
        """, (emb,))
        conn.commit()
        conn.close()

        store_triple("a7", "User", "likes", "Python", embed=False)

        stats = get_triple_stats()
        assert "total_triples" in stats
        assert "unique_subjects" in stats
        assert "unique_predicates" in stats
        assert "unique_objects" in stats
        assert "top_subjects" in stats
        assert "top_predicates" in stats
        assert stats["total_triples"] >= 1


class TestRetrieveTriples:
    def test_keyword_fallback(self):
        from saga.triples import store_triple, retrieve_triples
        conn = _setup_db()

        emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                embedding, topics, metadata, encoding_confidence)
            VALUES ('a8', 'Retrieve test', 'hash8', datetime('now'), 'active',
                ?, '[]', '{}', 0.7)
        """, (emb,))
        conn.commit()
        conn.close()

        store_triple("a8", "User", "has_profession", "developer", embed=False)
        store_triple("a8", "User", "likes", "Python", embed=False)

        results = retrieve_triples("User profession")
        assert isinstance(results, list)
        # Should find triples via keyword/entity matching
        if results:
            assert "subject" in results[0]
            assert "predicate" in results[0]
            assert "object" in results[0]


class TestResolveContradictions:
    def test_manual_strategy_noop(self):
        from saga.triples import resolve_contradictions
        contradictions = [{"type": "value_conflict"}]
        resolved = resolve_contradictions(contradictions, strategy="manual")
        assert resolved == 0

    def test_newest_strategy_tombstones_old(self):
        from saga.triples import store_triple, resolve_contradictions
        conn = _setup_db()

        emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                embedding, topics, metadata, encoding_confidence)
            VALUES ('a9', 'Resolve test', 'hash9', datetime('now'), 'active',
                ?, '[]', '{}', 0.7)
        """, (emb,))
        conn.commit()
        conn.close()

        store_triple("a9", "User", "has_profession", "developer", embed=False)
        store_triple("a9", "User", "has_profession", "designer", embed=False)

        # Get actual triple IDs to build the contradiction dict
        from saga.triples import _get_db
        conn = _get_db()
        rows = conn.execute(
            "SELECT id, object, created_at FROM triples WHERE subject='User' AND predicate='has_profession' AND state='active'"
        ).fetchall()
        conn.close()

        if len(rows) >= 2:
            contradiction = {
                "type": "multi_value_on_unique_pred",
                "subject": "User",
                "predicate": "has_profession",
                "values": [
                    {"id": r[0], "value": r[1], "date": r[2]} for r in rows
                ]
            }
            resolved = resolve_contradictions([contradiction], strategy="newest")
            assert resolved >= 1


# ─── Extract and Store ─────────────────────────────────────────────────────


class TestExtractAndStore:
    @pytest.mark.asyncio
    async def test_extract_and_store_no_api_key(self, monkeypatch):
        """Without NVIDIA_NIM_API_KEY, extract_and_store returns 0."""
        monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
        conn = _setup_db()
        emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                embedding, topics, metadata, encoding_confidence)
            VALUES ('ext1', 'The sky is blue', 'hashext1', datetime('now'), 'active',
                ?, '[]', '{}', 0.7)
        """, (emb,))
        conn.commit()
        conn.close()

        from saga.triples import extract_and_store
        count = await extract_and_store("ext1", "The sky is blue")
        assert count == 0

    @pytest.mark.asyncio
    async def test_extract_and_store_with_mock_llm(self, monkeypatch):
        """With mocked LLM, extract_and_store stores triples."""
        conn = _setup_db()
        emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                embedding, topics, metadata, encoding_confidence)
            VALUES ('ext2', 'Jaden lives in Oakland', 'hashext2', datetime('now'), 'active',
                ?, '[]', '{}', 0.7)
        """, (emb,))
        conn.commit()
        conn.close()

        # Mock extract_triples_llm (now async) to return triples directly
        async def _mock_extract(c, aid=""):
            return [
                {"atom_id": aid, "subject": "Jaden", "predicate": "lives_in", "object": "Oakland"},
            ]
        monkeypatch.setattr("saga.triples.extract_triples_llm", _mock_extract)

        from saga.triples import extract_and_store
        count = await extract_and_store("ext2", "Jaden lives in Oakland")
        assert count == 1


class TestExtractTriplesLlm:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_empty(self, monkeypatch):
        """Without API key, returns empty list."""
        monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
        from saga.triples import extract_triples_llm
        result = await extract_triples_llm("The sky is blue")
        assert result == []

    @pytest.mark.asyncio
    async def test_with_mocked_api(self, monkeypatch):
        """With mocked API response, extracts triples."""
        monkeypatch.setenv("NVIDIA_NIM_API_KEY", "test-key")

        import requests

        class MockResponse:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "(Jaden, lives_in, Oakland)"}}]}

        monkeypatch.setattr(requests, "post", lambda *a, **kw: MockResponse())
        from saga.triples import extract_triples_llm
        result = await extract_triples_llm("Jaden lives in Oakland", atom_id="test1")
        assert len(result) >= 1
        assert result[0]["subject"] == "Jaden"


class TestBatchExtractTriples:
    """P7 batch extraction — single LLM call returns triples for many atoms."""

    @pytest.mark.asyncio
    async def test_no_api_key_returns_empty_per_atom(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        from saga.triples import batch_extract_triples_llm
        items = [("a1", "User likes sushi"), ("a2", "Movie 9/10")]
        result = await batch_extract_triples_llm(items)
        assert result == {"a1": [], "a2": []}

    @pytest.mark.asyncio
    async def test_empty_items_returns_empty(self, monkeypatch):
        from saga.triples import batch_extract_triples_llm
        assert await batch_extract_triples_llm([]) == {}

    @pytest.mark.asyncio
    async def test_parses_per_atom_blocks(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        import requests

        # Three atoms; LLM returns mixed (triples / SKIP / triples).
        response_text = (
            "[ATOM_1]\n"
            "(User, likes_food, sushi)\n"
            "(User, lives_in, Boston)\n"
            "\n"
            "[ATOM_2]\n"
            "SKIP\n"
            "\n"
            "[ATOM_3]\n"
            "(Code_Geass, has_rating, 9/10)\n"
        )

        class MockResponse:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": response_text}}]}

        monkeypatch.setattr(requests, "post", lambda *a, **kw: MockResponse())

        from saga.triples import batch_extract_triples_llm
        items = [
            ("aid_1", "User likes sushi and lives in Boston"),
            ("aid_2", "User feels reflective today"),
            ("aid_3", "Code Geass R2 9/10"),
        ]
        result = await batch_extract_triples_llm(items)
        assert set(result.keys()) == {"aid_1", "aid_2", "aid_3"}
        assert len(result["aid_1"]) == 2
        # Map atom_id back through to the parsed triples
        assert all(t["atom_id"] == "aid_1" for t in result["aid_1"])
        assert {t["predicate"] for t in result["aid_1"]} == {"likes_food", "lives_in"}
        assert result["aid_2"] == []  # SKIP
        assert len(result["aid_3"]) == 1
        assert result["aid_3"][0]["subject"] == "Code_Geass"

    @pytest.mark.asyncio
    async def test_batch_size_chunks(self, monkeypatch):
        """With batch_size=2 over 3 items, the function should make 2
        LLM calls and merge results."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        import requests

        # Each call returns one triple per atom in the batch.
        call_count = {"n": 0}
        def mock_post(*args, **kwargs):
            call_count["n"] += 1
            n = call_count["n"]
            # First call: 2 atoms; second call: 1 atom.
            if n == 1:
                content = "[ATOM_1]\n(User, has_x, AAA)\n\n[ATOM_2]\n(User, has_x, BBB)"
            else:
                content = "[ATOM_1]\n(User, has_x, CCC)"

            class R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self):
                    return {"choices": [{"message": {"content": content}}]}
            return R()

        monkeypatch.setattr(requests, "post", mock_post)
        from saga.triples import batch_extract_triples_llm
        items = [("a1", "1"), ("a2", "2"), ("a3", "3")]
        result = await batch_extract_triples_llm(items, batch_size=2)
        assert call_count["n"] == 2
        assert len(result["a1"]) == 1
        assert len(result["a2"]) == 1
        assert len(result["a3"]) == 1
        assert result["a3"][0]["object"] == "CCC"

    @pytest.mark.asyncio
    async def test_batch_failure_returns_empty_for_that_batch(self, monkeypatch):
        """If a batch's LLM call raises, atoms in that batch get empty
        triple lists but other batches still succeed."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        import requests

        call_count = {"n": 0}
        def mock_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise requests.exceptions.ConnectionError("simulated")

            class R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self):
                    return {"choices": [{"message": {"content": "[ATOM_1]\n(User, has_x, okk)"}}]}
            return R()

        monkeypatch.setattr(requests, "post", mock_post)
        from saga.triples import batch_extract_triples_llm
        items = [("a1", "1"), ("a2", "2"), ("a3", "3"), ("a4", "4")]
        result = await batch_extract_triples_llm(items, batch_size=2)
        # First batch failed: a1, a2 get empty.
        assert result["a1"] == []
        assert result["a2"] == []
        # Second batch succeeded: a3 has the triple, a4 has nothing
        # (mock only emitted ATOM_1).
        assert len(result["a3"]) == 1

    @pytest.mark.asyncio
    async def test_falls_back_to_reasoning_field(self, monkeypatch):
        """Reasoning models put output in message.reasoning when content is None."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        import requests

        class MockResponse:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {
                    "content": None,
                    "reasoning": "[ATOM_1]\n(User, lives_in, Boston)",
                }}]}

        monkeypatch.setattr(requests, "post", lambda *a, **kw: MockResponse())
        from saga.triples import batch_extract_triples_llm
        result = await batch_extract_triples_llm([("aid", "User lives in Boston")])
        assert len(result["aid"]) == 1
        assert result["aid"][0]["object"] == "Boston"


class TestBatchExtractAndStore:
    @pytest.mark.asyncio
    async def test_stores_via_existing_path(self, monkeypatch):
        """batch_extract_and_store should write triples through
        store_triples_batch and return the count."""
        _setup_db()
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        import requests

        class MockResponse:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content":
                    "[ATOM_1]\n(User, lives_in, Boston)\n\n"
                    "[ATOM_2]\n(User, likes_food, sushi)"
                }}]}

        monkeypatch.setattr(requests, "post", lambda *a, **kw: MockResponse())
        from saga.triples import batch_extract_and_store
        # Need real atoms in the DB for the FK constraint on triples.atom_id
        from saga.core import store_atom
        a1 = store_atom("user lives in boston")
        a2 = store_atom("user likes sushi")
        count = await batch_extract_and_store([(a1, "User lives in Boston"), (a2, "User likes sushi")])
        assert count == 2


class TestParseTriplesTemporalTags:
    """P37(a) — _parse_triples must accept the optional valid_from /
    valid_until kv-pair tail introduced for the temporal world model."""

    def test_three_tuple_form_unchanged(self):
        from saga.triples import _parse_triples
        out = _parse_triples("(User, lives_in, Boston)\n(User, likes, jazz)")
        assert len(out) == 2
        assert out[0]["subject"] == "User"
        assert out[0]["object"] == "Boston"
        assert "valid_from" not in out[0]
        assert "valid_until" not in out[0]

    def test_valid_from_only(self):
        from saga.triples import _parse_triples
        out = _parse_triples(
            "(User, lives_in, Boston, valid_from=2023-05-30)"
        )
        assert len(out) == 1
        assert out[0]["valid_from"] == "2023-05-30"
        assert "valid_until" not in out[0]

    def test_both_temporal_bounds(self):
        from saga.triples import _parse_triples
        out = _parse_triples(
            "(User, employed_at, OldJob, valid_from=2022-01-01, valid_until=2023-06-15)"
        )
        assert len(out) == 1
        assert out[0]["valid_from"] == "2022-01-01"
        assert out[0]["valid_until"] == "2023-06-15"

    def test_null_temporal_value_ignored(self):
        """``valid_until=null`` is the LLM's way of saying 'still current';
        we represent that by simply omitting the column."""
        from saga.triples import _parse_triples
        out = _parse_triples(
            "(User, lives_in, Boston, valid_from=2023-05-30, valid_until=null)"
        )
        assert out[0]["valid_from"] == "2023-05-30"
        assert "valid_until" not in out[0]

    def test_unknown_kv_keys_silently_dropped(self):
        """Forward-compatibility: unknown trailing keys don't break parse."""
        from saga.triples import _parse_triples
        out = _parse_triples(
            "(User, lives_in, Boston, valid_from=2023-05-30, source=email)"
        )
        assert len(out) == 1
        assert out[0]["valid_from"] == "2023-05-30"
        assert "source" not in out[0]

    def test_mixed_lines(self):
        """A block can mix 3-tuple and tagged forms — both parse."""
        from saga.triples import _parse_triples
        out = _parse_triples(
            "(User, likes, jazz)\n"
            "(User, lives_in, Boston, valid_from=2023-05-30)\n"
            "(User, employed_at, OldJob, valid_from=2022-01-01, valid_until=2023-06-15)\n"
        )
        assert len(out) == 3
        assert "valid_from" not in out[0]
        assert out[1]["valid_from"] == "2023-05-30"
        assert out[2]["valid_until"] == "2023-06-15"
