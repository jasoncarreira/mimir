"""P9: two-tier retrieval + observation->raw evidence boost."""
from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture
def env(monkeypatch, tmp_path):
    import msam.core
    monkeypatch.setattr(msam.core, "DB_PATH", tmp_path / "t.db")
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("msam.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("msam.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("msam.core.cached_embed_query", lambda t: fake_emb)
    msam.core.get_db().close()
    msam.core.run_migrations()
    return tmp_path / "t.db"


def _ensure_relations(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS atom_relations (
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        confidence REAL DEFAULT 1.0,
        created_at TEXT,
        UNIQUE(source_id, target_id, relation_type)
    );
    """)


def _insert_edge(conn, src, tgt, kind):
    conn.execute(
        "INSERT OR IGNORE INTO atom_relations (source_id, target_id, relation_type, confidence, created_at) "
        "VALUES (?, ?, ?, 1.0, datetime('now'))",
        (src, tgt, kind),
    )


class TestTwoTierReturn:
    def test_returns_dict_shape(self, env, monkeypatch):
        import msam.core
        obs_id = msam.core.store_atom(
            "user prefers Sony cameras", memory_type="observation", evidence_count=5
        )
        raw_id = msam.core.store_atom("I picked up a Sony A7 III last week")
        assert isinstance(obs_id, str) and isinstance(raw_id, str)

        def fake_retrieve(query, mode="task", top_k=20, memory_type=None, **kw):
            conn = msam.core.get_db()
            if memory_type:
                rows = conn.execute("SELECT * FROM atoms WHERE memory_type = ?", (memory_type,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM atoms").fetchall()
            conn.close()
            out = []
            for r in rows:
                a = dict(r); a.pop("embedding", None)
                a["_activation"] = 1.0; a["_similarity"] = 0.6
                out.append(a)
            return out
        monkeypatch.setattr(msam.core, "retrieve", fake_retrieve)
        monkeypatch.setattr(msam.core, "keyword_search", lambda q, top_k=10, memory_type=None, include_session_boundaries=False: fake_retrieve(q, memory_type=memory_type))

        result = msam.core.hybrid_retrieve("cameras", top_k=5, two_tier=True)
        assert isinstance(result, dict)
        assert set(result.keys()) == {"observations", "raws"}
        assert any(a["id"] == obs_id for a in result["observations"])
        assert any(a["id"] == raw_id for a in result["raws"])

    def test_observation_below_confidence_floor_dropped(self, env, monkeypatch):
        import msam.core
        obs_id = msam.core.store_atom(
            "weak belief", memory_type="observation", evidence_count=5
        )
        raw_id = msam.core.store_atom("a raw fact")

        def fake_retrieve(query, mode="task", top_k=20, memory_type=None, **kw):
            conn = msam.core.get_db()
            if memory_type:
                rows = conn.execute("SELECT * FROM atoms WHERE memory_type = ?", (memory_type,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM atoms").fetchall()
            conn.close()
            out = []
            for r in rows:
                a = dict(r); a.pop("embedding", None)
                a["_activation"] = 1.0
                # Observation gets sub-threshold similarity; raw clears it
                a["_similarity"] = 0.10 if r["id"] == obs_id else 0.6
                out.append(a)
            return out
        monkeypatch.setattr(msam.core, "retrieve", fake_retrieve)
        monkeypatch.setattr(msam.core, "keyword_search", lambda q, top_k=10, memory_type=None, include_session_boundaries=False: fake_retrieve(q, memory_type=memory_type))

        result = msam.core.hybrid_retrieve("anything", top_k=5, two_tier=True)
        assert result["observations"] == []  # gated out
        assert any(a["id"] == raw_id for a in result["raws"])

    def test_observations_top_k_cap(self, env, monkeypatch):
        import msam.core
        ids = [
            msam.core.store_atom(
                f"observation {i}", memory_type="observation", evidence_count=3
            )
            for i in range(10)
        ]
        assert all(isinstance(i, str) for i in ids)

        def fake_retrieve(query, mode="task", top_k=20, memory_type=None, **kw):
            conn = msam.core.get_db()
            if memory_type:
                rows = conn.execute("SELECT * FROM atoms WHERE memory_type = ?", (memory_type,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM atoms").fetchall()
            conn.close()
            out = []
            for r in rows:
                a = dict(r); a.pop("embedding", None)
                a["_activation"] = 1.0; a["_similarity"] = 0.6
                out.append(a)
            return out
        monkeypatch.setattr(msam.core, "retrieve", fake_retrieve)
        monkeypatch.setattr(msam.core, "keyword_search", lambda q, top_k=10, memory_type=None, include_session_boundaries=False: fake_retrieve(q, memory_type=memory_type))

        real_cfg = msam.core._cfg
        monkeypatch.setattr(msam.core, "_cfg", lambda s, k, d=None: 3 if (s, k) == ("retrieval", "observations_top_k") else real_cfg(s, k, d))

        result = msam.core.hybrid_retrieve("q", top_k=20, two_tier=True)
        assert len(result["observations"]) == 3


class TestEvidenceBoost:
    def test_evidenced_by_lifts_raw_atom(self, env, monkeypatch):
        import msam.core
        # An observation and two raw atoms. Only the "evidence" raw is
        # linked to the observation by an evidenced_by edge.
        obs_id = msam.core.store_atom(
            "user enjoys photography", memory_type="observation", evidence_count=5
        )
        evidence_id = msam.core.store_atom("I got my Sony A7 III last week")
        noise_id = msam.core.store_atom("talked about scheduling a dentist visit")

        conn = msam.core.get_db()
        _ensure_relations(conn)
        _insert_edge(conn, obs_id, evidence_id, "evidenced_by")
        conn.commit(); conn.close()

        def fake_retrieve(query, mode="task", top_k=20, memory_type=None, **kw):
            conn = msam.core.get_db()
            if memory_type:
                rows = conn.execute("SELECT * FROM atoms WHERE memory_type = ?", (memory_type,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM atoms").fetchall()
            conn.close()
            out = []
            for r in rows:
                a = dict(r); a.pop("embedding", None)
                a["_activation"] = 1.0
                # obs and noise match well, evidence atom is middle-ranked
                a["_similarity"] = {obs_id: 0.8, noise_id: 0.7, evidence_id: 0.5}[r["id"]]
                out.append(a)
            # Sort so obs is rank 1, noise rank 2, evidence rank 3 in the
            # pathway (so the boost has to lift it).
            out.sort(key=lambda a: a["_similarity"], reverse=True)
            return out
        monkeypatch.setattr(msam.core, "retrieve", fake_retrieve)
        monkeypatch.setattr(msam.core, "keyword_search", lambda q, top_k=10, memory_type=None, include_session_boundaries=False: fake_retrieve(q, memory_type=memory_type))

        result = msam.core.hybrid_retrieve("photography", top_k=3, two_tier=True)

        raw_ids_in_order = [r["id"] for r in result["raws"]]
        # The evidence atom should rank AT OR ABOVE the noise atom thanks to
        # the boost (would normally rank below by pure RRF).
        assert evidence_id in raw_ids_in_order
        assert raw_ids_in_order.index(evidence_id) <= raw_ids_in_order.index(noise_id)


class TestMissingEvidenceBaseScore:
    """P30: missing-atom base score is its own cosine similarity (scaled to
    in-pool RRF magnitude), not just the boost. Without this, missing atoms
    could outrank in-top-K peers with the same observation backing.
    """

    def test_missing_atom_with_zero_similarity_lands_at_or_below_in_pool_peer(
        self, env, monkeypatch
    ):
        import msam.core
        # Setup: one observation, two raw atoms — both backed by the same
        # observation. One raw is in the candidate pool (rank 1). The other
        # is OUTSIDE the candidate pool (won't appear in retrieve()) so it's
        # the "missing" atom that gets pulled in via the evidence boost.
        obs_id = msam.core.store_atom(
            "user enjoys photography", memory_type="observation", evidence_count=5
        )
        in_pool_id = msam.core.store_atom("I love taking photos")
        missing_id = msam.core.store_atom("unrelated content about cooking")

        conn = msam.core.get_db()
        _ensure_relations(conn)
        _insert_edge(conn, obs_id, in_pool_id, "evidenced_by")
        _insert_edge(conn, obs_id, missing_id, "evidenced_by")
        conn.commit(); conn.close()

        def fake_retrieve(query, mode="task", top_k=20, memory_type=None, **kw):
            conn = msam.core.get_db()
            if memory_type == "observation":
                rows = conn.execute(
                    "SELECT * FROM atoms WHERE memory_type = ?", ("observation",)
                ).fetchall()
            elif memory_type == "raw":
                # Only return in_pool_id so missing_id has to be pulled in
                # via the evidence-boost path.
                rows = conn.execute(
                    "SELECT * FROM atoms WHERE id = ?", (in_pool_id,)
                ).fetchall()
            else:
                rows = []
            conn.close()
            out = []
            for r in rows:
                a = dict(r); a.pop("embedding", None)
                a["_activation"] = 1.0
                a["_similarity"] = 0.8 if r["id"] == obs_id else 0.5
                out.append(a)
            return out

        monkeypatch.setattr(msam.core, "retrieve", fake_retrieve)
        monkeypatch.setattr(
            msam.core, "keyword_search",
            lambda q, top_k=10, memory_type=None, include_session_boundaries=False: fake_retrieve(q, memory_type=memory_type),
        )

        result = msam.core.hybrid_retrieve("photography", top_k=5, two_tier=True)
        raws = result["raws"]
        ids = [r["id"] for r in raws]

        # Both in-pool and missing atoms appear (evidence atoms always pulled in).
        assert in_pool_id in ids
        assert missing_id in ids

        # Critical: missing atom must NOT outrank in-pool peer. Pre-P30, the
        # missing atom got the uncapped boost as its full score and would
        # often outrank in-pool atoms.
        assert ids.index(in_pool_id) <= ids.index(missing_id)
