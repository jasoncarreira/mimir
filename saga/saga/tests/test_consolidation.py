"""MSAM Consolidation Tests -- sleep-inspired memory consolidation."""

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
    monkeypatch.setattr("saga.triples.DB_PATH", db_path)
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("saga.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("saga.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("saga.core._cached_embed_query_import", lambda t: tuple(fake_emb))
    monkeypatch.setattr("saga.core.cached_embed_query", lambda t: fake_emb)
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
        from saga.consolidation import ConsolidationEngine
        engine = ConsolidationEngine()
        assert engine.similarity_threshold > 0
        assert engine.min_cluster_size >= 2
        assert engine.max_clusters > 0
        assert engine.stability_reduction > 0


class TestClusterBruteForce:
    def test_groups_similar(self):
        from saga.core import get_db, run_migrations
        from saga.consolidation import ConsolidationEngine

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
        from saga.core import get_db, run_migrations
        from saga.consolidation import ConsolidationEngine

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

    def test_observations_created_field(self):
        """Live (non-dry) runs surface `observations_created` so callers
        don't have to know about the cluster_consolidated /
        synthesis_atoms_stored distinction."""
        from saga.core import get_db, run_migrations, store_atom
        from saga.consolidation import ConsolidationEngine
        run_migrations(get_db())
        # Empty DB - 0 clusters. The field should still exist with value 0.
        engine = ConsolidationEngine()
        result = engine.consolidate(dry_run=False)
        assert "observations_created" in result
        assert result["observations_created"] == 0

    def test_skips_pinned(self):
        from saga.core import get_db, run_migrations
        from saga.consolidation import ConsolidationEngine

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
        from saga.core import get_db, run_migrations
        from saga.consolidation import ConsolidationEngine

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
        from saga.core import get_db, run_migrations, add_atom_relation
        from saga.consolidation import ConsolidationEngine

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
        from saga.core import get_db, run_migrations, add_atom_relation
        from saga.consolidation import ConsolidationEngine

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

    def test_subset_observations_helper(self):
        from saga.core import get_db, run_migrations, add_atom_relation
        from saga.consolidation import ConsolidationEngine

        conn = get_db()
        run_migrations(conn)
        same_emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        # Three raw atoms + one observation that covers ONLY raw_a, raw_b.
        for aid in ("raw_a", "raw_b", "raw_c", "obs_old"):
            content = f"content {aid}"
            ch = hashlib.sha256(content.encode()).hexdigest()[:32]
            mt = "observation" if aid == "obs_old" else "raw"
            conn.execute("""
                INSERT INTO atoms (id, content, content_hash, created_at, state,
                    is_pinned, embedding, topics, metadata, encoding_confidence,
                    stream, profile, access_count, stability, memory_type)
                VALUES (?, ?, ?, datetime('now'), 'active', 0, ?, '[]', '{}', 0.7,
                    'semantic', 'standard', 0, 1.0, ?)
            """, (aid, content, ch, same_emb, mt))
        conn.commit()
        conn.close()
        add_atom_relation("obs_old", "raw_a", "evidenced_by", confidence=1.0)
        add_atom_relation("obs_old", "raw_b", "evidenced_by", confidence=1.0)

        engine = ConsolidationEngine()
        # New cluster covers a strict superset of obs_old's evidence.
        assert engine._subset_observations_for_cluster(["raw_a", "raw_b", "raw_c"]) == ["obs_old"]
        # Identical → not a strict subset, returns empty.
        assert engine._subset_observations_for_cluster(["raw_a", "raw_b"]) == []
        # Disjoint → empty.
        assert engine._subset_observations_for_cluster(["raw_x", "raw_y", "raw_z"]) == []
        # Single-atom new cluster → guarded out.
        assert engine._subset_observations_for_cluster(["raw_a"]) == []

    def test_consolidate_writes_supersedes_for_strict_superset(self, monkeypatch):
        """End-to-end: a cluster that covers a strict superset of an
        existing observation's evidence creates a new observation AND
        writes a supersedes edge from new → old."""
        from saga.core import get_db, run_migrations, add_atom_relation
        from saga.consolidation import ConsolidationEngine

        conn = get_db()
        run_migrations(conn)

        same_emb = struct.pack('1024f', *np.random.randn(1024).astype(np.float32))
        # 5 raw atoms in the same cluster.
        cluster_ids = [f"raw_super_{i}" for i in range(5)]
        for aid in cluster_ids:
            content = f"shared topic super content {aid}"
            ch = hashlib.sha256(content.encode()).hexdigest()[:32]
            conn.execute("""
                INSERT INTO atoms (id, content, content_hash, created_at, state,
                    is_pinned, embedding, topics, metadata, encoding_confidence,
                    stream, profile, access_count, stability, memory_type)
                VALUES (?, ?, ?, datetime('now'), 'active', 0, ?, '[]', '{}', 0.7,
                    'semantic', 'standard', 0, 1.0, 'raw')
            """, (aid, content, ch, same_emb))

        # Pre-existing observation covering only the FIRST 3 of those raws.
        obs_id = "obs_super_old"
        obs_content = "[Consolidated from 3 atoms] earlier coverage"
        ch = hashlib.sha256(obs_content.encode()).hexdigest()[:32]
        conn.execute("""
            INSERT INTO atoms (id, content, content_hash, created_at, state,
                is_pinned, embedding, topics, metadata, encoding_confidence,
                stream, profile, access_count, stability, memory_type, evidence_count)
            VALUES (?, ?, ?, datetime('now'), 'active', 0, ?, '[]', '{}', 0.9,
                'semantic', 'standard', 0, 1.0, 'observation', 3)
        """, (obs_id, obs_content, ch, same_emb))
        conn.commit()
        conn.close()
        for src_id in cluster_ids[:3]:
            add_atom_relation(obs_id, src_id, "evidenced_by", confidence=1.0)

        # Stub the LLM so synthesis runs without external calls.
        import requests
        def fake_post(*a, **k):
            class _R:
                status_code = 200
                def json(self):
                    return {"choices": [{"message": {"content": "user enjoys topic super"}}]}
            return _R()
        monkeypatch.setattr(requests, "post", fake_post)

        engine = ConsolidationEngine(similarity_threshold=0.5, min_cluster_size=3)
        result = engine.consolidate()
        assert result["clusters_skipped_existing"] == 0
        assert result["observations_superseded"] >= 1

        # Verify the supersedes edge points new_obs → obs_super_old.
        conn = get_db()
        rows = conn.execute(
            "SELECT source_id, target_id FROM atom_relations "
            "WHERE relation_type = 'supersedes' AND target_id = ?",
            (obs_id,),
        ).fetchall()
        conn.close()
        assert len(rows) >= 1
        new_obs_id = rows[0][0]
        assert new_obs_id != obs_id

    def test_consolidate_skips_existing(self, monkeypatch):
        """End-to-end: clusters_skipped_existing reports the count and the
        LLM is not called for an already-consolidated cluster."""
        from saga.core import get_db, run_migrations, add_atom_relation
        from saga.consolidation import ConsolidationEngine

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


class TestPriorTriplesOnSupersede:
    """When a new cluster strictly supersedes an existing observation,
    the synthesizer prompt should include the existing observation's
    triples as 'previous beliefs' context."""

    def test_fetch_prior_triples_returns_active_only(self):
        from saga.core import get_db, run_migrations, store_atom
        from saga.triples import init_triples_schema, store_triples_batch
        from saga.consolidation import ConsolidationEngine
        run_migrations(get_db())
        init_triples_schema()

        obs_id = store_atom(
            "Old observation about user", memory_type="observation",
            evidence_count=2,
        )
        store_triples_batch([
            {"atom_id": obs_id, "subject": "User", "predicate": "lives_in", "object": "Oakland"},
            {"atom_id": obs_id, "subject": "User", "predicate": "has_pet", "object": "cat"},
        ])

        engine = ConsolidationEngine()
        result = engine._fetch_prior_triples([obs_id])
        assert len(result) == 2
        assert {t["object"] for t in result} == {"Oakland", "cat"}
        # No atom_id in the prompt-context shape
        assert all("atom_id" not in t for t in result)

    def test_fetch_prior_triples_empty_input(self):
        from saga.consolidation import ConsolidationEngine
        engine = ConsolidationEngine()
        assert engine._fetch_prior_triples([]) == []
        assert engine._fetch_prior_triples(None) == []

    def test_fetch_prior_triples_caps_at_20(self):
        from saga.core import get_db, run_migrations, store_atom
        from saga.triples import init_triples_schema, store_triples_batch
        from saga.consolidation import ConsolidationEngine
        run_migrations(get_db())
        init_triples_schema()

        obs_id = store_atom("o", memory_type="observation", evidence_count=2)
        store_triples_batch([
            {"atom_id": obs_id, "subject": f"User{i}", "predicate": "knows", "object": f"thing{i}"}
            for i in range(30)
        ])
        engine = ConsolidationEngine()
        result = engine._fetch_prior_triples([obs_id])
        assert len(result) == 20


class TestPersistConsolidationTriples:
    """P35.2: when the consolidation LLM restates a triple already
    attached to a soon-to-be-superseded observation, transfer
    ownership rather than dedup-skip the write."""

    def _setup(self):
        from saga.core import get_db, run_migrations, store_atom
        from saga.triples import init_triples_schema, store_triples_batch
        run_migrations(get_db())
        init_triples_schema()

        old_obs_id = store_atom(
            "old observation", memory_type="observation", evidence_count=2,
        )
        store_triples_batch([
            {"atom_id": old_obs_id, "subject": "User",
             "predicate": "lives_in", "object": "Boston"},
            {"atom_id": old_obs_id, "subject": "User",
             "predicate": "has_pet", "object": "cat"},
        ])
        new_obs_id = store_atom(
            "new observation (supersedes old)",
            memory_type="observation", evidence_count=5,
        )
        return old_obs_id, new_obs_id

    def test_fresh_triple_inserted(self):
        from saga.consolidation import ConsolidationEngine
        old_obs, new_obs = self._setup()
        engine = ConsolidationEngine()
        n = engine._persist_consolidation_triples(
            [{"subject": "User", "predicate": "works_at", "object": "Acme"}],
            new_obs_id=new_obs,
            superseded_obs_ids=[old_obs],
        )
        assert n == 1
        # Newly inserted triple is attached to the new observation
        from saga.core import get_db
        conn = get_db()
        rows = conn.execute(
            "SELECT atom_id FROM triples WHERE subject='User' AND predicate='works_at'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == new_obs

    def test_restated_triple_transfers_ownership(self):
        """If the LLM restates a triple already attached to the
        superseded observation, ownership transfers to the new
        observation. No duplicate row is created."""
        from saga.consolidation import ConsolidationEngine
        from saga.core import get_db
        old_obs, new_obs = self._setup()

        engine = ConsolidationEngine()
        n = engine._persist_consolidation_triples(
            [{"subject": "User", "predicate": "lives_in", "object": "Boston"}],
            new_obs_id=new_obs,
            superseded_obs_ids=[old_obs],
        )
        assert n == 1  # transfer counts as persisted

        conn = get_db()
        rows = conn.execute(
            "SELECT atom_id FROM triples WHERE subject='User' AND predicate='lives_in' AND object='Boston'"
        ).fetchall()
        # Exactly one row (no duplicate created)
        assert len(rows) == 1
        # Ownership moved to the new observation
        assert rows[0][0] == new_obs

    def test_unrestated_triple_stays_with_old_observation(self):
        """Triples the LLM did NOT restate stay attached to the old
        (now-superseded) observation. They'll be demoted at retrieval
        via the supersedes edge but aren't deleted."""
        from saga.consolidation import ConsolidationEngine
        from saga.core import get_db
        old_obs, new_obs = self._setup()

        engine = ConsolidationEngine()
        # LLM only restates one of the two prior triples (lives_in).
        # The has_pet triple is missing from its output — it judged
        # that fact no longer holds.
        engine._persist_consolidation_triples(
            [{"subject": "User", "predicate": "lives_in", "object": "Boston"}],
            new_obs_id=new_obs,
            superseded_obs_ids=[old_obs],
        )

        conn = get_db()
        # has_pet triple should still exist, attached to old_obs
        rows = conn.execute(
            "SELECT atom_id FROM triples WHERE subject='User' AND predicate='has_pet'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == old_obs

    def test_existing_triple_on_unrelated_observation_left_alone(self):
        """If the same SPO is attested by some OTHER (non-superseded)
        observation, leave it alone. Don't accidentally hijack it."""
        from saga.consolidation import ConsolidationEngine
        from saga.core import get_db, store_atom
        from saga.triples import store_triples_batch
        old_obs, new_obs = self._setup()

        # A third, unrelated observation also claims (User, lives_in, Boston).
        # Wait — content dedup means only one row exists. To simulate
        # "unrelated" we attach the existing Boston triple to a
        # different observation and ensure that observation is NOT in
        # superseded_obs_ids.
        unrelated = store_atom(
            "an unrelated observation", memory_type="observation",
            evidence_count=2,
        )
        # Move the triple to the unrelated obs to simulate the case
        conn = get_db()
        conn.execute(
            "UPDATE triples SET atom_id = ? "
            "WHERE subject='User' AND predicate='lives_in' AND object='Boston'",
            (unrelated,),
        )
        conn.commit()
        conn.close()

        engine = ConsolidationEngine()
        n = engine._persist_consolidation_triples(
            [{"subject": "User", "predicate": "lives_in", "object": "Boston"}],
            new_obs_id=new_obs,
            superseded_obs_ids=[old_obs],   # unrelated NOT in this list
        )
        assert n == 0  # no transfer, no insert (existing row untouched)

        conn = get_db()
        rows = conn.execute(
            "SELECT atom_id FROM triples WHERE subject='User' AND predicate='lives_in'"
        ).fetchall()
        # Still one row, still attached to the unrelated observation
        assert len(rows) == 1
        assert rows[0][0] == unrelated


class TestParseStructuredSynthesis:
    """P35: parse the OBSERVATION + TRIPLES dual-output format."""

    def test_clean_format(self):
        from saga.consolidation import _parse_structured_synthesis
        text = (
            "OBSERVATION:\n"
            "User graduated with a Business Administration degree.\n"
            "\n"
            "TRIPLES:\n"
            "(User, has_degree, Business_Administration)\n"
            "(User, graduation_year, 2023)\n"
        )
        obs, triples = _parse_structured_synthesis(text)
        assert obs == "User graduated with a Business Administration degree."
        assert len(triples) == 2
        assert triples[0]["subject"] == "User"
        assert triples[0]["predicate"] == "has_degree"
        assert triples[0]["object"] == "Business_Administration"

    def test_observation_only_no_triples_section(self):
        from saga.consolidation import _parse_structured_synthesis
        text = "OBSERVATION:\nUser likes pizza."
        obs, triples = _parse_structured_synthesis(text)
        assert obs == "User likes pizza."
        assert triples == []

    def test_triples_none_block(self):
        from saga.consolidation import _parse_structured_synthesis
        text = (
            "OBSERVATION:\n"
            "User reflects on whether life has meaning.\n"
            "\n"
            "TRIPLES:\n"
            "NONE\n"
        )
        obs, triples = _parse_structured_synthesis(text)
        assert obs == "User reflects on whether life has meaning."
        assert triples == []

    def test_legacy_observation_only_format(self):
        """Old-style single-section response should still parse — the
        whole text becomes the observation."""
        from saga.consolidation import _parse_structured_synthesis
        text = "User said they prefer dark mode UI."
        obs, triples = _parse_structured_synthesis(text)
        assert "User said they prefer dark mode UI." in obs
        assert triples == []

    def test_markdown_bold_headers(self):
        """Some models emit `**OBSERVATION:**` instead of plain
        `OBSERVATION:`. The parser tolerates either."""
        from saga.consolidation import _parse_structured_synthesis
        text = (
            "**OBSERVATION:**\n"
            "User lives in Boston.\n"
            "\n"
            "**TRIPLES:**\n"
            "(User, lives_in, Boston)\n"
        )
        obs, triples = _parse_structured_synthesis(text)
        assert obs == "User lives in Boston."
        assert len(triples) == 1

    def test_trailing_section_ignored(self):
        """If the model emits CONTRADICTIONS: or other sections after
        TRIPLES, those don't pollute the triple parse."""
        from saga.consolidation import _parse_structured_synthesis
        text = (
            "OBSERVATION:\n"
            "User likes pizza.\n"
            "\n"
            "TRIPLES:\n"
            "(User, likes_food, pizza)\n"
            "\n"
            "CONTRADICTIONS:\n"
            "atom 1 vs atom 3 disagree on day of week\n"
        )
        obs, triples = _parse_structured_synthesis(text)
        assert obs == "User likes pizza."
        assert len(triples) == 1
        # CONTRADICTIONS shouldn't end up in the triples list
        assert all("disagree" not in t.get("object", "") for t in triples)

    def test_empty_input(self):
        from saga.consolidation import _parse_structured_synthesis
        obs, triples = _parse_structured_synthesis("")
        assert obs is None
        assert triples == []

    def test_invalid_triples_filtered(self):
        """Malformed triples (too long, missing components) are dropped
        by the underlying _parse_triples validation."""
        from saga.consolidation import _parse_structured_synthesis
        text = (
            "OBSERVATION:\n"
            "Mixed quality output.\n"
            "\n"
            "TRIPLES:\n"
            "(User, lives_in, Boston)\n"
            "(this is not a triple line)\n"
            "(A, b, C)\n"  # too short - rejected
            "(SomeReallyLongSubjectThatExceedsTheLimitForSubjects, has, x)\n"  # too long subject
        )
        obs, triples = _parse_structured_synthesis(text)
        # Only the first valid one survives validation
        assert len(triples) == 1
        assert triples[0]["subject"] == "User"


class TestPromptBranching:
    """Verify the consolidation prompt branches on
    [triples] enable_extraction so we don't waste tokens asking for a
    TRIPLES section we'd discard."""

    def test_prompt_includes_triples_when_extraction_on(self, monkeypatch):
        """When triples.enable_extraction = True, the prompt must
        include the TRIPLES section header and rules."""
        import copy
        from saga import config as cfg_mod
        from saga.core import store_atom
        from saga.consolidation import ConsolidationEngine
        cfg_mod._load_config()
        snapshot = copy.deepcopy(cfg_mod._config) if cfg_mod._config else {}
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        snapshot.setdefault("triples", {})["enable_extraction"] = True
        snapshot.setdefault("consolidation", {})["enable_llm"] = True
        snapshot["consolidation"]["min_cluster_size"] = 2

        # Capture the prompt by intercepting requests.post.
        captured = {}
        class _Resp:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"content":
                    "OBSERVATION:\nA test observation.\n\nTRIPLES:\nNONE"
                }}]}
            text = ""
        def fake_post(url, headers=None, json=None, timeout=None):
            captured["prompt"] = json["messages"][0]["content"]
            return _Resp()
        monkeypatch.setattr("requests.post", fake_post)

        # Seed enough atoms to form a cluster (LLM will be called).
        for i in range(3):
            store_atom(f"Atom {i} about the user enjoying jazz music")
        ConsolidationEngine().consolidate()

        assert "TRIPLES:" in captured.get("prompt", ""), (
            f"prompt missing TRIPLES section: {captured.get('prompt', '')[:500]}"
        )
        assert "Rules for TRIPLES" in captured["prompt"]

    def test_prompt_omits_triples_when_extraction_off(self, monkeypatch):
        """When triples.enable_extraction = False (default), the
        prompt must NOT include the TRIPLES section — saves tokens
        and matches what the bench was doing implicitly with the
        old broken consolidation."""
        import copy
        from saga import config as cfg_mod
        from saga.core import store_atom
        from saga.consolidation import ConsolidationEngine
        cfg_mod._load_config()
        snapshot = copy.deepcopy(cfg_mod._config) if cfg_mod._config else {}
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        snapshot.setdefault("triples", {})["enable_extraction"] = False
        snapshot.setdefault("consolidation", {})["enable_llm"] = True
        snapshot["consolidation"]["min_cluster_size"] = 2

        captured = {}
        class _Resp:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"content":
                    "OBSERVATION:\nA test observation."
                }}]}
            text = ""
        def fake_post(url, headers=None, json=None, timeout=None):
            captured["prompt"] = json["messages"][0]["content"]
            return _Resp()
        monkeypatch.setattr("requests.post", fake_post)

        for i in range(3):
            store_atom(f"Atom {i} about the user enjoying jazz music")
        ConsolidationEngine().consolidate()

        prompt = captured.get("prompt", "")
        assert "TRIPLES:" not in prompt, (
            f"prompt should not request triples: {prompt[:500]}"
        )
        assert "OBSERVATION:" in prompt
