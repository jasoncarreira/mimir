"""Tests for Temporal World Model (Feature 3)."""

import json
import sqlite3
import numpy as np
import pytest
from datetime import datetime, timezone, timedelta


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
    # Initialize main DB + triples schema + run migrations
    from saga.core import get_db, run_migrations
    from saga.triples import init_triples_schema
    conn = get_db()
    init_triples_schema(conn)
    conn.close()
    run_migrations()
    yield db_path


class TestWorldQueryCurrent:
    def test_world_query_current(self):
        from saga.triples import update_world, query_world
        update_world("Jaden", "lives_in", "Oakland")
        result = query_world()
        assert len(result) >= 1
        assert any(r["subject"] == "Jaden" and r["object"] == "Oakland" for r in result)


class TestWorldQueryExpired:
    def test_world_query_expired(self):
        from saga.triples import update_world, query_world
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        update_world("Jaden", "is_at", "cafe", valid_from=past, valid_until=expired)

        # Current query should NOT show expired
        current = query_world(entity="Jaden", predicate="is_at")
        assert not any(r["object"] == "cafe" for r in current)

        # include_expired should show it
        all_triples = query_world(entity="Jaden", predicate="is_at", include_expired=True)
        assert any(r["object"] == "cafe" for r in all_triples)


class TestWorldQueryPointInTime:
    def test_world_query_point_in_time(self):
        from saga.triples import update_world, query_world
        t0 = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        t1 = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        t2 = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

        # First fact: valid from t0 to t1
        update_world("Jaden", "reads", "book_a", valid_from=t0, valid_until=t1)
        # Second fact: valid from t1 onwards
        update_world("Jaden", "reads", "book_b", valid_from=t1)

        # Query at a time between t0 and t1 should return book_a
        query_time = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        result = query_world(entity="Jaden", predicate="reads", at_time=query_time)
        objects = [r["object"] for r in result]
        assert "book_a" in objects

        # Query at t2 should return book_b
        result2 = query_world(entity="Jaden", predicate="reads", at_time=t2)
        objects2 = [r["object"] for r in result2]
        assert "book_b" in objects2


class TestWorldUpdateClosesOld:
    def test_world_update_closes_old(self):
        from saga.triples import update_world, query_world
        update_world("Agent", "mood", "happy")
        update_world("Agent", "mood", "curious")

        # Current query should show only the latest
        current = query_world(entity="Agent", predicate="mood")
        assert len(current) == 1
        assert current[0]["object"] == "curious"


class TestWorldEntityFilter:
    def test_world_entity_filter(self):
        from saga.triples import update_world, query_world
        update_world("Alice", "likes", "tea")
        update_world("Bob", "likes", "coffee")

        result = query_world(entity="Alice")
        assert all(r["subject"] == "Alice" for r in result)


class TestWorldPredicateFilter:
    def test_world_predicate_filter(self):
        from saga.triples import update_world, query_world
        update_world("Agent", "skill", "coding")
        update_world("Agent", "hobby", "music")

        result = query_world(entity="Agent", predicate="skill")
        assert all(r["predicate"] == "skill" for r in result)


class TestWorldHistory:
    def test_world_history(self):
        from saga.triples import update_world, world_history
        update_world("Agent", "location", "home")
        update_world("Agent", "location", "office")
        update_world("Agent", "location", "park")

        history = world_history("Agent", "location")
        # Should include all versions
        objects = [h["object"] for h in history]
        assert "home" in objects
        assert "office" in objects
        assert "park" in objects
        assert len(history) >= 3


class TestWorldModelPathway:
    """P37(b) — _world_model_pathway extracts query entities, calls
    query_world, returns the source atoms of currently-valid triples
    as a new RRF ranker."""

    def test_off_by_default(self, monkeypatch):
        """Strict no-op when [retrieval] enable_world_model_pathway is False."""
        from saga.core import _world_model_pathway
        # Default is False — nothing should be returned regardless of state.
        out = _world_model_pathway("Where does the user live?", top_k=20)
        assert out == []

    def test_returns_source_atoms_for_matched_entity(self, monkeypatch):
        """When enabled, pathway returns the source atoms of triples whose
        subject matches a query entity."""
        import saga.config as cfg_mod
        cfg_mod._load_config()
        snapshot = dict(cfg_mod._config or {})
        snapshot.setdefault("retrieval", {})["enable_world_model_pathway"] = True
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)

        # Seed: store an atom, then add a triple with that atom as source.
        from saga.core import store_atom, _world_model_pathway
        from saga.triples import update_world
        atom_id = store_atom("User lives in Boston")
        update_world(
            subject="User", predicate="lives_in",
            object_val="Boston", source_atom_id=atom_id,
        )

        # extract_query_entities should pick up "User" from the question.
        out = _world_model_pathway("Where does User live?", top_k=20)
        assert len(out) == 1
        assert out[0]["id"] == atom_id
        assert out[0]["_world_model_pathway"] is True

    def test_excludes_expired_triples(self, monkeypatch):
        """A triple whose valid_until is in the past must NOT surface
        as 'currently valid' — its source atom shouldn't be returned."""
        import saga.config as cfg_mod
        cfg_mod._load_config()
        snapshot = dict(cfg_mod._config or {})
        snapshot.setdefault("retrieval", {})["enable_world_model_pathway"] = True
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)

        from saga.core import store_atom, _world_model_pathway
        from saga.triples import update_world

        old_atom = store_atom("User lived in Seattle")
        update_world(
            subject="User", predicate="lived_in",
            object_val="Seattle", source_atom_id=old_atom,
            valid_from="2020-01-01", valid_until="2022-12-31",
        )
        new_atom = store_atom("User lives in Boston")
        update_world(
            subject="User", predicate="lives_in",
            object_val="Boston", source_atom_id=new_atom,
            valid_from="2023-01-01",
        )

        # No at_time → "currently valid" defaults to wall-clock now.
        # Both predicates are different so update_world doesn't auto-close;
        # only the lives_in/Boston (no valid_until) is currently valid.
        out = _world_model_pathway("Where does User live?", top_k=20)
        atom_ids = {a["id"] for a in out}
        assert new_atom in atom_ids
        assert old_atom not in atom_ids

    def test_reference_date_anchors_currently_valid(self, monkeypatch):
        """When reference_date is passed, 'currently valid' anchors to
        that point in time. Bench harness uses this so 2023-haystack
        questions don't see 'current' as 2026 wall-clock."""
        import saga.config as cfg_mod
        cfg_mod._load_config()
        snapshot = dict(cfg_mod._config or {})
        snapshot.setdefault("retrieval", {})["enable_world_model_pathway"] = True
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)

        from saga.core import store_atom, _world_model_pathway
        from saga.triples import update_world

        atom_2023 = store_atom("User worked at Acme")
        update_world(
            subject="User", predicate="employed_at",
            object_val="Acme", source_atom_id=atom_2023,
            valid_from="2022-01-01", valid_until="2023-12-31",
        )

        # As of 2023-06-15: Acme should match.
        from datetime import datetime, timezone
        ref = datetime(2023, 6, 15, tzinfo=timezone.utc)
        out = _world_model_pathway("Where does User work?", top_k=20, reference_date=ref)
        assert any(a["id"] == atom_2023 for a in out)

        # As of 2024-06-15: expired, no match.
        ref_after = datetime(2024, 6, 15, tzinfo=timezone.utc)
        out = _world_model_pathway("Where does User work?", top_k=20, reference_date=ref_after)
        assert all(a["id"] != atom_2023 for a in out)

    def test_no_entities_returns_empty(self, monkeypatch):
        """Query with no extractable entities skips the pathway cleanly."""
        import saga.config as cfg_mod
        cfg_mod._load_config()
        snapshot = dict(cfg_mod._config or {})
        snapshot.setdefault("retrieval", {})["enable_world_model_pathway"] = True
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)

        from saga.core import _world_model_pathway
        # All-stopword query.
        out = _world_model_pathway("a an the of", top_k=20)
        assert out == []
