"""MSAM REST API Server Tests -- tests for the FastAPI-based REST API."""

import json
import os
import sys

import pytest

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed")
from fastapi.testclient import TestClient

from msam.server import app


@pytest.fixture
def client():
    """Create a FastAPI test client."""
    return TestClient(app)


# ─── Health ──────────────────────────────────────────────────────────────────


class TestHealth:
    def test_health_check(self, client):
        rv = client.get("/v1/health")
        assert rv.status_code == 200
        data = rv.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "timestamp" in data

    def test_health_no_auth_required(self, client, monkeypatch):
        """Health check should work even when API key is configured."""
        monkeypatch.setenv("MSAM_API_KEY", "test-secret")
        rv = client.get("/v1/health")
        assert rv.status_code == 200
        data = rv.json()
        assert data["status"] == "ok"


# ─── Store ───────────────────────────────────────────────────────────────────


class TestStore:
    def test_store_requires_content(self, client):
        rv = client.post("/v1/store", json={})
        assert rv.status_code == 422  # Pydantic validation error

    def test_store_success(self, client, monkeypatch):
        """Test successful store with mocked internals."""
        import msam.annotate
        monkeypatch.setattr(msam.annotate, "classify_stream", lambda c: "semantic")
        monkeypatch.setattr(msam.annotate, "classify_profile", lambda c: "standard")
        monkeypatch.setattr(msam.annotate, "smart_annotate", lambda c, use_llm=False: {
            "arousal": 0.5, "valence": 0.0,
            "topics": ["test"], "encoding_confidence": 0.7,
        })

        import msam.core
        monkeypatch.setattr(msam.core, "store_atom", lambda **kwargs: "abc123def456")

        import msam.triples
        monkeypatch.setattr(msam.triples, "extract_and_store", lambda aid, c: 2)

        rv = client.post("/v1/store", json={"content": "User prefers dark mode"})
        assert rv.status_code == 200
        data = rv.json()
        assert data["stored"] is True
        assert data["atom_id"] == "abc123def456"
        assert data["stream"] == "semantic"

    def test_store_empty_content(self, client):
        """Empty string should fail Pydantic validation or return error."""
        rv = client.post("/v1/store", json={"content": ""})
        # FastAPI/Pydantic allows empty strings; the endpoint logic handles it
        assert rv.status_code in (200, 400, 422)


# ─── Query ───────────────────────────────────────────────────────────────────


class TestQuery:
    def test_query_requires_query(self, client):
        rv = client.post("/v1/query", json={})
        assert rv.status_code == 422  # Pydantic validation error

    def test_query_success(self, client, monkeypatch):
        """Test successful query with mocked hybrid retrieval."""
        import msam.triples
        monkeypatch.setattr(msam.triples, "hybrid_retrieve_with_triples", lambda q, mode="task", token_budget=500: {
            "triples": [
                {"subject": "user", "predicate": "prefers", "object": "dark mode"},
            ],
            "atoms": [],
            "_raw_atoms": [
                {
                    "id": "abc123",
                    "content": "User prefers dark mode",
                    "stream": "semantic",
                    "_similarity": 0.85,
                    "_combined_score": 0.9,
                    "_confidence_tier": "high",
                    "_retrieval_confidence_tier": "high",
                    "topics": '["preferences"]',
                },
            ],
            "triple_tokens": 5,
            "atom_tokens": 6,
            "total_tokens": 11,
            "items_returned": 2,
            "query_type": "mixed",
            "triple_ratio": 0.4,
            "latency_ms": 10.0,
        })

        rv = client.post("/v1/query", json={"query": "user preferences"})
        assert rv.status_code == 200
        data = rv.json()
        assert data["query"] == "user preferences"
        assert data["confidence_tier"] in ("none", "low", "medium", "high")
        assert "atoms" in data
        assert "triples" in data

    def test_query_empty_query(self, client, monkeypatch):
        """Empty query should either be rejected or return empty results."""
        import msam.triples
        monkeypatch.setattr(msam.triples, "hybrid_retrieve_with_triples", lambda q, mode="task", token_budget=500: {
            "triples": [], "atoms": [], "_raw_atoms": [],
            "triple_tokens": 0, "atom_tokens": 0, "total_tokens": 0,
            "items_returned": 0, "query_type": "mixed", "triple_ratio": 0, "latency_ms": 0,
        })
        rv = client.post("/v1/query", json={"query": ""})
        assert rv.status_code == 200
        data = rv.json()
        assert data["confidence_tier"] == "none"

    def test_query_two_tier_request_field(self, client, monkeypatch):
        """When the request body asks for two_tier=true, the response shape
        switches to {observations, raws, triples=[]} via hybrid_retrieve."""
        import msam.core
        captured = {}

        def fake_hybrid_retrieve(query, **kw):
            captured.update(kw)
            return {
                "observations": [{
                    "id": "obs1", "content": "user prefers dark mode",
                    "stream": "semantic", "memory_type": "observation",
                    "_similarity": 0.7, "_combined_score": 0.05,
                    "evidence_count": 4,
                }],
                "raws": [{
                    "id": "raw1", "content": "I love the dark theme",
                    "stream": "semantic", "memory_type": "raw",
                    "_similarity": 0.6, "_combined_score": 0.04,
                }],
                # 0.7 max sim → "high" tier; gating keeps full output.
                "confidence_tier": "high",
                "confidence_tier_observations": "high",
                "confidence_tier_raws": "high",
            }

        monkeypatch.setattr(msam.core, "hybrid_retrieve", fake_hybrid_retrieve)

        rv = client.post("/v1/query", json={
            "query": "preferences", "two_tier": True, "top_k": 10,
        })
        assert rv.status_code == 200
        data = rv.json()
        assert data["two_tier"] is True
        assert data["confidence_tier"] == "high"
        assert "observations" in data and len(data["observations"]) == 1
        assert "raws" in data and len(data["raws"]) == 1
        assert data["observations"][0]["memory_type"] == "observation"
        assert data["observations"][0]["evidence_count"] == 4
        assert data["raws"][0]["memory_type"] == "raw"
        assert data["triples"] == []
        assert data["items_returned"] == 2
        # Verify hybrid_retrieve was actually called with two_tier=True
        assert captured.get("two_tier") is True
        assert captured.get("top_k") == 10

    def test_query_two_tier_low_confidence_gated(self, client, monkeypatch):
        """Both tiers low → 0 obs, 1 raw."""
        import msam.core
        monkeypatch.setattr(msam.core, "hybrid_retrieve", lambda q, **kw: {
            "observations": [{
                "id": "obs1", "content": "weak observation",
                "stream": "semantic", "memory_type": "observation",
                "_similarity": 0.18, "_combined_score": 0.02, "evidence_count": 2,
            }],
            "raws": [
                {"id": f"raw{i}", "content": f"weak content {i}",
                 "stream": "semantic", "memory_type": "raw",
                 "_similarity": 0.18, "_combined_score": 0.02}
                for i in range(5)
            ],
            "confidence_tier": "low",
            "confidence_tier_observations": "low",
            "confidence_tier_raws": "low",
        })
        rv = client.post("/v1/query", json={"query": "x", "two_tier": True})
        assert rv.status_code == 200
        data = rv.json()
        assert data["confidence_tier"] == "low"
        assert data["confidence_tier_observations"] == "low"
        assert data["confidence_tier_raws"] == "low"
        assert data["gated"] is True
        assert len(data["observations"]) == 0
        assert len(data["raws"]) == 1

    def test_query_two_tier_per_tier_gating_protects_against_noise(self, client, monkeypatch):
        """Strong obs + weak raws: obs keeps full, raws gates aggressively.
        This is the noise-dilution scenario per-tier gating prevents."""
        import msam.core
        monkeypatch.setattr(msam.core, "hybrid_retrieve", lambda q, **kw: {
            "observations": [{
                "id": "obs1", "content": "strong observation",
                "stream": "semantic", "memory_type": "observation",
                "_similarity": 0.7, "_combined_score": 0.05, "evidence_count": 5,
            }],
            "raws": [
                {"id": f"raw{i}", "content": f"weak raw {i}",
                 "stream": "semantic", "memory_type": "raw",
                 "_similarity": 0.18, "_combined_score": 0.02}
                for i in range(5)
            ],
            "confidence_tier": "high",  # union dominated by strong obs
            "confidence_tier_observations": "high",
            "confidence_tier_raws": "low",
        })
        rv = client.post("/v1/query", json={"query": "x", "two_tier": True})
        assert rv.status_code == 200
        data = rv.json()
        # Obs side stays full thanks to high obs_tier...
        assert len(data["observations"]) == 1
        # ...but raws are gated to 1 because raws_tier=low. Pre-fix, the
        # union "high" tier would have kept multiple raws.
        assert len(data["raws"]) == 1

    def test_query_two_tier_none_confidence_suppresses(self, client, monkeypatch):
        """confidence_tier=none returns empty results."""
        import msam.core
        monkeypatch.setattr(msam.core, "hybrid_retrieve", lambda q, **kw: {
            "observations": [{"id": "obs1", "content": "irrelevant",
                              "stream": "semantic", "_similarity": 0.05,
                              "_combined_score": 0.01, "memory_type": "observation",
                              "evidence_count": 1}],
            "raws": [{"id": "raw1", "content": "irrelevant",
                      "stream": "semantic", "_similarity": 0.05,
                      "_combined_score": 0.01, "memory_type": "raw"}],
            "confidence_tier": "none",
            "confidence_tier_observations": "none",
            "confidence_tier_raws": "none",
        })
        rv = client.post("/v1/query", json={"query": "x", "two_tier": True})
        assert rv.status_code == 200
        data = rv.json()
        assert data["confidence_tier"] == "none"
        assert data["observations"] == []
        assert data["raws"] == []
        assert data["items_returned"] == 0

    def test_query_two_tier_via_config_default(self, client, monkeypatch):
        """When request omits two_tier and config sets two_tier_enabled=true,
        the server picks up the config and returns two-tier shape."""
        import msam.core
        import msam.server
        # Force the config lookup to return True for two_tier_enabled.
        real_cfg = msam.server._cfg
        def fake_cfg(section, key, default=None):
            if section == "retrieval" and key == "two_tier_enabled":
                return True
            return real_cfg(section, key, default)
        monkeypatch.setattr(msam.server, "_cfg", fake_cfg)

        monkeypatch.setattr(msam.core, "hybrid_retrieve", lambda q, **kw: {
            "observations": [], "raws": [],
        })

        rv = client.post("/v1/query", json={"query": "anything"})
        assert rv.status_code == 200
        data = rv.json()
        assert data["two_tier"] is True
        assert "atoms" not in data
        assert "observations" in data

    def test_query_request_overrides_config_two_tier_false(self, client, monkeypatch):
        """Request body two_tier=false must override config two_tier_enabled=true."""
        import msam.triples
        import msam.server
        real_cfg = msam.server._cfg
        def fake_cfg(section, key, default=None):
            if section == "retrieval" and key == "two_tier_enabled":
                return True
            return real_cfg(section, key, default)
        monkeypatch.setattr(msam.server, "_cfg", fake_cfg)

        monkeypatch.setattr(msam.triples, "hybrid_retrieve_with_triples",
                            lambda q, mode="task", token_budget=500: {
            "triples": [], "atoms": [], "_raw_atoms": [],
            "triple_tokens": 0, "atom_tokens": 0, "total_tokens": 0,
            "items_returned": 0, "query_type": "mixed", "triple_ratio": 0,
            "latency_ms": 0,
        })

        rv = client.post("/v1/query", json={"query": "x", "two_tier": False})
        assert rv.status_code == 200
        data = rv.json()
        # Single-tier shape returned; no two_tier key in response
        assert "atoms" in data
        assert "confidence_tier" in data
        assert data.get("two_tier") is None or "two_tier" not in data


# ─── Stats ───────────────────────────────────────────────────────────────────


class TestStats:
    def test_stats_endpoint(self, client, monkeypatch):
        """Test stats endpoint with mocked get_stats."""
        import msam.core
        monkeypatch.setattr(msam.core, "get_stats", lambda: {
            "total_atoms": 42,
            "active": 30,
            "fading": 10,
            "dormant": 2,
        })

        rv = client.get("/v1/stats")
        assert rv.status_code == 200
        data = rv.json()
        assert data["total_atoms"] == 42


# ─── Context ────────────────────────────────────────────────────────────────


# ─── Feedback ────────────────────────────────────────────────────────────────


class TestFeedback:
    def test_feedback_requires_atom_ids(self, client):
        rv = client.post("/v1/feedback", json={"response_text": "some text"})
        assert rv.status_code == 422

    def test_feedback_requires_response_text(self, client):
        rv = client.post("/v1/feedback", json={"atom_ids": ["abc"]})
        assert rv.status_code == 422

    def test_feedback_success(self, client, monkeypatch):
        import msam.core
        monkeypatch.setattr(msam.core, "mark_contributions", lambda ids, text, sid=None: {
            "contributed": ids, "not_contributed": [], "session_id": sid,
        })

        rv = client.post("/v1/feedback", json={
            "atom_ids": ["abc123"],
            "response_text": "The user prefers dark mode",
        })
        assert rv.status_code == 200
        data = rv.json()
        assert "contributed" in data
        assert data["session_id"] is None  # session_id not provided

    def test_feedback_with_session_id(self, client, monkeypatch):
        import msam.core
        monkeypatch.setattr(msam.core, "mark_contributions", lambda ids, text, sid=None: {
            "contributed": ids, "not_contributed": [], "session_id": sid,
        })

        rv = client.post("/v1/feedback", json={
            "atom_ids": ["abc123"],
            "response_text": "The user prefers dark mode",
            "session_id": "slack-dm-jcarreira:2026-04-25",
        })
        assert rv.status_code == 200
        assert rv.json()["session_id"] == "slack-dm-jcarreira:2026-04-25"


# ─── Sessions ───────────────────────────────────────────────────────────────


class TestSessionsEnd:
    def test_session_end_requires_session_id_and_summary(self, client):
        rv = client.post("/v1/sessions/end", json={"summary": "ok"})
        assert rv.status_code == 422
        rv = client.post("/v1/sessions/end", json={"session_id": "abc"})
        assert rv.status_code == 422

    def test_session_end_minimal(self, client, monkeypatch):
        import msam.core
        captured = {}
        def fake_store(**kwargs):
            captured.update(kwargs)
            return "boundary_atom_id_xyz"
        monkeypatch.setattr(msam.core, "store_session_boundary", fake_store)

        rv = client.post("/v1/sessions/end", json={
            "session_id": "abc",
            "summary": "Discussed Q2 roadmap",
        })
        assert rv.status_code == 200
        data = rv.json()
        assert data["atom_id"] == "boundary_atom_id_xyz"
        assert data["session_id"] == "abc"
        assert captured["session_id"] == "abc"
        assert captured["summary"] == "Discussed Q2 roadmap"
        # Optional fields default to None
        assert captured["topics_discussed"] is None
        assert captured["decisions_made"] is None

    def test_session_end_full(self, client, monkeypatch):
        import msam.core
        captured = {}
        def fake_store(**kwargs):
            captured.update(kwargs)
            return "boundary_atom_id_xyz"
        monkeypatch.setattr(msam.core, "store_session_boundary", fake_store)

        rv = client.post("/v1/sessions/end", json={
            "session_id": "slack-dm-jcarreira:2026-04-25",
            "summary": "Discussed Q2 roadmap and shipped P8",
            "topics_discussed": ["roadmap", "P8"],
            "decisions_made": ["Ship P8 by Friday"],
            "unfinished": ["Run benchmark"],
            "emotional_state": "engaged",
        })
        assert rv.status_code == 200
        assert captured["topics_discussed"] == ["roadmap", "P8"]
        assert captured["decisions_made"] == ["Ship P8 by Friday"]
        assert captured["unfinished"] == ["Run benchmark"]
        assert captured["emotional_state"] == "engaged"


# ─── API Key Auth ────────────────────────────────────────────────────────────


class TestApiKey:
    def test_api_key_rejection(self, client, monkeypatch):
        """Requests without API key should be rejected when key is configured."""
        monkeypatch.setenv("MSAM_API_KEY", "test-secret")
        rv = client.get("/v1/stats")
        assert rv.status_code == 401

    def test_api_key_wrong_key(self, client, monkeypatch):
        """Requests with wrong API key should be rejected."""
        monkeypatch.setenv("MSAM_API_KEY", "test-secret")
        rv = client.get("/v1/stats", headers={"X-API-Key": "wrong-key"})
        assert rv.status_code == 401

    def test_api_key_success(self, client, monkeypatch):
        """Requests with correct API key should succeed."""
        monkeypatch.setenv("MSAM_API_KEY", "test-secret")

        import msam.core
        monkeypatch.setattr(msam.core, "get_stats", lambda: {"total_atoms": 0})

        rv = client.get("/v1/stats", headers={"X-API-Key": "test-secret"})
        assert rv.status_code == 200

    def test_no_api_key_configured(self, client, monkeypatch):
        """When no MSAM_API_KEY is set, all requests should be allowed."""
        monkeypatch.delenv("MSAM_API_KEY", raising=False)

        import msam.core
        monkeypatch.setattr(msam.core, "get_stats", lambda: {"total_atoms": 0})

        rv = client.get("/v1/stats")
        assert rv.status_code == 200


# ─── Decay ───────────────────────────────────────────────────────────────────


class TestDecay:
    def test_decay_endpoint(self, client, monkeypatch):
        import msam.decay
        monkeypatch.setattr(msam.decay, "run_decay_cycle", lambda: {
            "processed": 10, "transitioned": 2,
        })

        rv = client.post("/v1/decay", json={})
        assert rv.status_code == 200
        data = rv.json()
        assert "processed" in data


# ─── Triples ────────────────────────────────────────────────────────────────


class TestTriples:
    def test_extract_requires_fields(self, client):
        rv = client.post("/v1/triples/extract", json={"atom_id": "abc"})
        assert rv.status_code == 422

        rv = client.post("/v1/triples/extract", json={"content": "hello"})
        assert rv.status_code == 422

    def test_extract_success(self, client, monkeypatch):
        import msam.triples
        monkeypatch.setattr(msam.triples, "extract_and_store", lambda aid, c: 3)

        rv = client.post("/v1/triples/extract", json={
            "atom_id": "abc123", "content": "The sky is blue",
        })
        assert rv.status_code == 200
        data = rv.json()
        assert data["triples_extracted"] == 3

    def test_graph_traversal(self, client, monkeypatch):
        import msam.triples
        monkeypatch.setattr(msam.triples, "graph_traverse", lambda e, max_hops=3: {
            "entity": e, "nodes": [e], "edges": [],
        })

        rv = client.get("/v1/triples/graph/user")
        assert rv.status_code == 200
        data = rv.json()
        assert data["entity"] == "user"


# ─── Contradictions ─────────────────────────────────────────────────────────


class TestContradictions:
    def test_contradictions_triples_mode(self, client, monkeypatch):
        import msam.triples
        monkeypatch.setattr(msam.triples, "detect_contradictions", lambda: [
            {"pair": ["triple1", "triple2"]},
        ])

        rv = client.post("/v1/contradictions", json={"mode": "triples"})
        assert rv.status_code == 200
        data = rv.json()
        assert data["count"] == 1

    def test_contradictions_default_mode(self, client, monkeypatch):
        """Default mode should be triples."""
        import msam.triples
        monkeypatch.setattr(msam.triples, "detect_contradictions", lambda: [])

        rv = client.post("/v1/contradictions", json={})
        assert rv.status_code == 200
        data = rv.json()
        assert "contradictions" in data


# ─── Predict ────────────────────────────────────────────────────────────────


class TestPredict:
    def test_predict_endpoint(self, client, monkeypatch):
        import msam.core
        monkeypatch.setattr(msam.core, "predict_needed_atoms", lambda ctx: [
            {"id": "pred1", "content": "Predicted atom", "confidence": 0.8},
        ])

        rv = client.post("/v1/predict", json={
            "time_of_day": "morning",
            "user_active": True,
        })
        assert rv.status_code == 200
        data = rv.json()
        assert data["count"] == 1
        assert len(data["predictions"]) == 1

    def test_predict_empty_body(self, client, monkeypatch):
        """Predict should work with empty body."""
        import msam.core
        monkeypatch.setattr(msam.core, "predict_needed_atoms", lambda ctx: [])

        rv = client.post("/v1/predict", json={})
        assert rv.status_code == 200
        data = rv.json()
        assert data["count"] == 0


# ─── Consolidate ─────────────────────────────────────────────────────────────


class TestConsolidate:
    def test_consolidate_dry_run(self, client, monkeypatch):
        from msam import consolidation
        monkeypatch.setattr(consolidation, "ConsolidationEngine", type(
            "MockEngine", (), {
                "__init__": lambda self: None,
                "consolidate": lambda self, dry_run=False, max_clusters=None: {
                    "clusters_found": 2, "dry_run": dry_run,
                },
            }
        ))

        rv = client.post("/v1/consolidate", json={"dry_run": True})
        assert rv.status_code == 200
        data = rv.json()
        assert data["dry_run"] is True


# ─── Replay ──────────────────────────────────────────────────────────────────


class TestReplay:
    def test_replay_endpoint(self, client, monkeypatch):
        import msam.core
        monkeypatch.setattr(msam.core, "episodic_replay", lambda **kwargs: {
            "episodes": [], "total_atoms": 0,
        })

        rv = client.post("/v1/replay", json={"topic": "meetings"})
        assert rv.status_code == 200
        data = rv.json()
        assert "episodes" in data

    def test_replay_requires_topic(self, client):
        rv = client.post("/v1/replay", json={})
        assert rv.status_code == 422


# ─── 404 / Method Errors ────────────────────────────────────────────────────


class TestErrorHandling:
    def test_not_found(self, client):
        rv = client.get("/v1/nonexistent")
        assert rv.status_code == 404

    def test_method_not_allowed(self, client):
        rv = client.get("/v1/store")  # store requires POST
        assert rv.status_code == 405
