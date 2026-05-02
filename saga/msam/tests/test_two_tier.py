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


class TestMissingRefScorePivot:
    """P39: ref_score for pulled-in (missing-atom) base scoring can pivot
    on either the bottom (default 'min') or the middle ('median') of the
    in-pool RRF distribution. The 'median' pivot raises pulled-in bases
    to compete with mid-rank in-pool raws — addresses the case where
    consolidation endorsed a strong-similarity atom but the cheap path
    missed it and the conservative 'min' pivot scores it below the
    worst in-pool raw.
    """

    def _setup_three_raws_one_obs(self, env, monkeypatch):
        """One obs + three raws (in-pool) + one pulled-in raw via
        evidenced_by. Returns (obs_id, in_pool_ids, missing_id).
        """
        import msam.core
        obs_id = msam.core.store_atom(
            "user collects vintage cameras",
            memory_type="observation",
            evidence_count=3,
        )
        # Three in-pool raws so we have a non-trivial RRF distribution.
        a_id = msam.core.store_atom("Picked up a Leica M3 last weekend")
        b_id = msam.core.store_atom("Found a Nikon F at the flea market")
        c_id = msam.core.store_atom("Restored my dad's Canonet QL17")
        missing_id = msam.core.store_atom(
            "Bought a Hasselblad 500C — beautiful piece"
        )

        conn = msam.core.get_db()
        _ensure_relations(conn)
        _insert_edge(conn, obs_id, missing_id, "evidenced_by")
        # Don't endorse the in-pool atoms — keeps the test focused on
        # how missing-atom scoring varies with the pivot config.
        conn.commit()
        conn.close()
        return obs_id, [a_id, b_id, c_id], missing_id

    def _wire_retrieve(self, monkeypatch, obs_id, in_pool_ids, missing_id):
        """Mock retrieve so the cheap path returns the in-pool raws
        (with descending similarities producing distinct RRF scores)
        and the observation. missing_id is NOT in the cheap path —
        only the evidence-boost branch can surface it."""
        import msam.core
        sims = {in_pool_ids[0]: 0.55, in_pool_ids[1]: 0.40, in_pool_ids[2]: 0.25}

        def fake_retrieve(query, mode="task", top_k=20, memory_type=None, **kw):
            conn = msam.core.get_db()
            if memory_type == "observation":
                rows = conn.execute(
                    "SELECT * FROM atoms WHERE memory_type = 'observation'"
                ).fetchall()
            elif memory_type == "raw":
                rows = conn.execute(
                    "SELECT * FROM atoms WHERE id IN (?, ?, ?)",
                    tuple(in_pool_ids),
                ).fetchall()
            else:
                rows = []
            conn.close()
            out = []
            for r in rows:
                a = dict(r); a.pop("embedding", None)
                a["_activation"] = 1.0
                a["_similarity"] = 0.8 if r["id"] == obs_id else sims.get(r["id"], 0.5)
                out.append(a)
            return out

        monkeypatch.setattr(msam.core, "retrieve", fake_retrieve)
        monkeypatch.setattr(
            msam.core, "keyword_search",
            lambda q, top_k=10, memory_type=None, include_session_boundaries=False:
                fake_retrieve(q, memory_type=memory_type),
        )

    def _run(self, missing_id):
        import msam.core
        result = msam.core.hybrid_retrieve("vintage cameras", top_k=10, two_tier=True)
        # Find the missing atom's score in the raws output.
        for r in result["raws"]:
            if r["id"] == missing_id:
                return r["_combined_score"]
        return None

    def test_min_pivot_is_default_and_back_compat(self, env, monkeypatch):
        """Default pivot is 'min' — preserves P30v1/v3 scoring exactly."""
        import copy
        from msam import config as cfg_mod
        cfg_mod._load_config()
        snapshot = copy.deepcopy(cfg_mod._config) if cfg_mod._config else {}
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        # No override — default of "min" should apply.

        obs_id, in_pool, missing_id = self._setup_three_raws_one_obs(env, monkeypatch)
        self._wire_retrieve(monkeypatch, obs_id, in_pool, missing_id)
        score_min = self._run(missing_id)
        assert score_min is not None and score_min > 0

    def test_median_pivot_raises_missing_atom_score(self, env, monkeypatch):
        """Switching the pivot to 'median' produces a strictly higher
        score for the missing atom (same boost, larger base, larger cap).
        """
        import copy
        from msam import config as cfg_mod
        cfg_mod._load_config()
        snapshot = copy.deepcopy(cfg_mod._config) if cfg_mod._config else {}
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)

        # Run once with 'min' (default).
        snapshot.setdefault("retrieval", {})["missing_ref_score_pivot"] = "min"
        obs_id, in_pool, missing_id = self._setup_three_raws_one_obs(env, monkeypatch)
        self._wire_retrieve(monkeypatch, obs_id, in_pool, missing_id)
        score_min = self._run(missing_id)

        # Run again with 'median'.
        snapshot["retrieval"]["missing_ref_score_pivot"] = "median"
        score_median = self._run(missing_id)

        assert score_min is not None and score_median is not None
        assert score_median > score_min, (
            f"median pivot should raise the missing-atom score, got "
            f"median={score_median} vs min={score_min}"
        )


class TestKeywordOnlyTierBackfill:
    """Regression: in two-tier mode, atoms surfaced only via keyword_search
    (not retrieve) lacked _similarity / _confidence_tier because
    keyword_search doesn't compute cosine. They reached the API filter
    with `_confidence_tier` absent → defaulted to "none" → rejected at any
    floor ≥ "low". Fix: _two_tier_split backfills both fields by
    re-fetching embeddings for atoms missing _similarity."""

    def test_keyword_only_atom_gets_classified(self, env, monkeypatch):
        import msam.core

        # Two raws stored. The fake retrieve() returns only one (semantic
        # path); keyword_search returns both. Without the backfill, the
        # keyword-only atom would surface with no tier set.
        sem_id = msam.core.store_atom("semantic match for the query")
        kw_only_id = msam.core.store_atom("keyword-only match content")

        def fake_retrieve(query, mode="task", top_k=20, memory_type=None, **kw):
            conn = msam.core.get_db()
            if memory_type == "raw":
                # Only return the semantic-matched one
                rows = conn.execute(
                    "SELECT * FROM atoms WHERE id = ?", (sem_id,)
                ).fetchall()
            else:
                rows = []
            conn.close()
            out = []
            for r in rows:
                a = dict(r)
                a.pop("embedding", None)
                a["_activation"] = 1.0
                a["_similarity"] = 0.5  # would classify as "high" at default thresholds
                a["_confidence_tier"] = "high"
                out.append(a)
            return out

        def fake_keyword_search(q, top_k=10, memory_type=None, include_session_boundaries=False):
            conn = msam.core.get_db()
            if memory_type == "raw":
                rows = conn.execute(
                    "SELECT * FROM atoms WHERE id IN (?, ?)", (sem_id, kw_only_id)
                ).fetchall()
            else:
                rows = []
            conn.close()
            out = []
            for r in rows:
                a = dict(r)
                a.pop("embedding", None)
                # keyword_search does NOT set _similarity or _confidence_tier
                a["_keyword_score"] = 1.0
                out.append(a)
            return out

        monkeypatch.setattr(msam.core, "retrieve", fake_retrieve)
        monkeypatch.setattr(msam.core, "keyword_search", fake_keyword_search)

        result = msam.core.hybrid_retrieve("test query", top_k=5, two_tier=True)
        raws = result["raws"]

        # Both atoms should surface
        ids = {r["id"] for r in raws}
        assert sem_id in ids
        assert kw_only_id in ids

        # Critical: keyword-only atom must have _confidence_tier set.
        # The exact tier depends on the backfilled cosine, but it must
        # be one of the recognized values, NOT absent.
        kw_atom = next(r for r in raws if r["id"] == kw_only_id)
        assert "_confidence_tier" in kw_atom
        assert kw_atom["_confidence_tier"] in ("high", "medium", "low", "none")
        # Likewise _similarity should be backfilled (≥ 0).
        assert "_similarity" in kw_atom
        assert kw_atom["_similarity"] >= 0.0
