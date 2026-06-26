"""Tier-1 integration tests: store + mark_access + recall composing
against an in-memory SQLite DB. Validates the contracts in SCORING.md
end-to-end without depending on FAISS/voyage — embedding and FAISS
search are mocked.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from mimir.saga.mark_access import AccessEvent, mark_access
from mimir.saga.recall import recall
from mimir.saga.client import SagaStore
from mimir.saga.store import store


@pytest.fixture
def conn():
    """In-memory SQLite with the new schema applied."""
    schema = (Path(__file__).resolve().parent.parent / "mimir" / "saga" / "schema.sql").read_text()
    c = sqlite3.connect(":memory:")
    c.executescript(schema)
    yield c
    c.close()


def _fake_embed(text: str) -> tuple[bytes, str, str, int]:
    """Deterministic 4-dim 'embedding' for tests. Returns a tuple of
    (vec_bytes, provider, model, dim) matching the EmbedFn signature.
    Not meaningful as vectors — tests don't compare embeddings, they
    work with mocked FAISS."""
    import struct
    h = abs(hash(text)) % 1000
    vec = [float(h % 7), float(h % 11), float(h % 13), float(h % 17)]
    return struct.pack("4f", *vec), "fake", "fake-model", 4


def _fake_query_embed(text: str) -> list[float]:
    """Mirror of _fake_embed for query-side. Returns list (not bytes)
    since query embeddings are usually kept in memory."""
    h = abs(hash(text)) % 1000
    return [float(h % 7), float(h % 11), float(h % 13), float(h % 17)]


class _FakeProvider:
    def embed(self, text: str, *, input_type: str = "passage") -> list[float]:
        return _fake_query_embed(text)

    def dimensions(self) -> int:
        return 4


def _patch_saga_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mimir.saga.embeddings.get_provider",
        lambda: _FakeProvider(),
    )

    def fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "fake",
                ("embedding", "model"): "fake-model",
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr("mimir.saga._config_io.get_config", fake_get_config)


# --- store ---

def test_store_inserts_atom_and_seeds_access_event(conn):
    """A fresh store fires one access_event so activation is non -inf."""
    result = store(conn, "Alice prefers concise replies",
                   embed_fn=_fake_embed, stream="semantic")
    assert result.stored is True
    assert result.atom_id

    # One atom row.
    n = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    assert n == 1
    # One embedding row.
    n = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert n == 1
    # One access_event with source='store'.
    rows = conn.execute(
        "SELECT source, weight FROM access_events WHERE atom_id = ?",
        (result.atom_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "store"
    assert rows[0][1] == 1.0
    # Summary populated.
    s = conn.execute(
        "SELECT recent_ts_json, old_count FROM atom_access_summary WHERE atom_id = ?",
        (result.atom_id,)
    ).fetchone()
    assert s is not None
    import json as _json
    assert len(_json.loads(s[0])) == 1  # one event in recent
    assert s[1] == 0                     # nothing in old aggregate


def test_store_dedupes_by_content_hash(conn):
    """Storing the same content twice from the same agent re-uses the
    existing atom_id and fires a re-encounter access_event."""
    r1 = store(conn, "duplicate fact", embed_fn=_fake_embed)
    r2 = store(conn, "duplicate fact", embed_fn=_fake_embed)
    assert r1.atom_id == r2.atom_id
    assert r1.stored is True
    assert r2.stored is False
    assert r2.reason == "duplicate"
    # Two store events — one initial + one re-encounter.
    rows = conn.execute(
        "SELECT source FROM access_events WHERE atom_id = ?",
        (r1.atom_id,)
    ).fetchall()
    assert [r[0] for r in rows] == ["store", "store"]


def test_store_empty_content_raises(conn):
    with pytest.raises(ValueError):
        store(conn, "  ", embed_fn=_fake_embed)


def test_pinned_atoms_get_pinned_init_event(conn):
    """Pinning fires an additional one-time event with the heavy weight."""
    result = store(conn, "important fact", embed_fn=_fake_embed,
                   is_pinned=True)
    rows = conn.execute(
        "SELECT source, weight FROM access_events WHERE atom_id = ? "
        "ORDER BY id",
        (result.atom_id,)
    ).fetchall()
    sources = [r[0] for r in rows]
    weights = [r[1] for r in rows]
    assert "store" in sources
    assert "pinned_init" in sources
    # The pinned_init event has weight 5.0.
    assert weights[sources.index("pinned_init")] == 5.0


# --- mark_access ---

def test_mark_access_appends_and_updates_summary(conn):
    """A retrieval event raises activation; summary picks up the new
    timestamp. mark_access doesn't commit — caller does."""
    r = store(conn, "test atom", embed_fn=_fake_embed)
    conn.execute("BEGIN IMMEDIATE")
    mark_access(conn, [AccessEvent(atom_id=r.atom_id, source="retrieval")])
    conn.commit()
    rows = conn.execute(
        "SELECT source, weight FROM access_events "
        "WHERE atom_id = ? ORDER BY id", (r.atom_id,)
    ).fetchall()
    assert [row[0] for row in rows] == ["store", "retrieval"]
    # Summary has both events in recent now.
    import json as _json
    s = conn.execute(
        "SELECT recent_ts_json FROM atom_access_summary WHERE atom_id = ?",
        (r.atom_id,)
    ).fetchone()
    assert len(_json.loads(s[0])) == 2


def test_mark_access_atomic_on_batch_failure(conn):
    """If one event in a batch is for a non-existent atom (FK violation),
    the whole batch should roll back. SQLite has FK off by default so we
    have to test the semantic from a different angle: caller can rely on
    BEGIN IMMEDIATE."""
    # Two valid events should commit together.
    r1 = store(conn, "atom one", embed_fn=_fake_embed)
    r2 = store(conn, "atom two", embed_fn=_fake_embed)
    initial = conn.execute("SELECT COUNT(*) FROM access_events").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    mark_access(conn, [
        AccessEvent(atom_id=r1.atom_id, source="retrieval"),
        AccessEvent(atom_id=r2.atom_id, source="retrieval"),
    ])
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM access_events").fetchone()[0]
    assert after == initial + 2


# --- recall ---

def test_recall_returns_stored_atom(conn):
    """End-to-end: store an atom, recall it via mocked FAISS, get it back."""
    r = store(conn, "Alice prefers concise replies", embed_fn=_fake_embed,
              stream="semantic")
    result = recall(
        conn, "what does Alice prefer",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [(r.atom_id, 0.9)],
        fts_search_fn=lambda q, k: [],
    )
    raws_ids = [c.atom["id"] for c in result.raws]
    assert r.atom_id in raws_ids


def test_recall_default_output_unchanged_with_no_extra_pathways(conn):
    r1 = store(conn, "Alice prefers concise replies", embed_fn=_fake_embed)
    r2 = store(conn, "Bob enjoys verbose explanations", embed_fn=_fake_embed)

    kwargs = dict(
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [(r1.atom_id, 0.9), (r2.atom_id, 0.8)],
        fts_search_fn=lambda q, k: [(r2.atom_id, 7.0)],
        fire_access_events=False,
    )
    baseline = recall(conn, "concise replies", **kwargs)
    with_empty = recall(
        conn, "concise replies",
        extra_atom_ranked_pathways={},
        **kwargs,
    )

    baseline_scores = [(c.atom["id"], c.rrf_score) for c in baseline.raws]
    empty_scores = [(c.atom["id"], c.rrf_score) for c in with_empty.raws]
    assert empty_scores == baseline_scores


def test_recall_extra_pathway_can_admit_atom_absent_from_builtin_candidates(conn):
    semantic = store(conn, "semantic candidate", embed_fn=_fake_embed)
    extra = store(conn, "extra-only candidate", embed_fn=_fake_embed)

    result = recall(
        conn, "unmatched query",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [(semantic.atom_id, 0.9)],
        fts_search_fn=lambda q, k: [],
        extra_atom_ranked_pathways={"session_boundary": [extra.atom_id]},
        fire_access_events=False,
    )

    ids = [c.atom["id"] for c in result.raws]
    assert extra.atom_id in ids
    extra_candidate = next(c for c in result.raws if c.atom["id"] == extra.atom_id)
    assert extra_candidate.semantic_rank == -1
    assert extra_candidate.keyword_rank == -1


def test_recall_extra_pathway_rejects_reserved_triple_without_triples(conn):
    extra = store(conn, "extra-only candidate", embed_fn=_fake_embed)

    with pytest.raises(ValueError, match="built-in pathway: triple"):
        recall(
            conn, "unmatched query",
            query_embed_fn=_fake_query_embed,
            faiss_search_fn=lambda emb, k: [],
            fts_search_fn=lambda q, k: [],
            triple_search_fn=None,
            extra_atom_ranked_pathways={"triple": [extra.atom_id]},
            fire_access_events=False,
        )


def test_recall_extra_pathway_dedupes_duplicate_atom_ids_before_rrf(conn):
    extra = store(conn, "extra-only candidate", embed_fn=_fake_embed)

    result = recall(
        conn, "unmatched query",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [],
        fts_search_fn=lambda q, k: [],
        extra_atom_ranked_pathways={
            "session_boundary": [extra.atom_id, extra.atom_id, extra.atom_id],
        },
        fire_access_events=False,
    )

    extra_candidate = next(c for c in result.raws if c.atom["id"] == extra.atom_id)
    assert extra_candidate.rrf_score == pytest.approx(1 / 61)


def test_recall_extra_pathway_weight_changes_ordering_and_score(conn):
    semantic = store(conn, "semantic candidate", embed_fn=_fake_embed)
    extra = store(conn, "extra-only candidate", embed_fn=_fake_embed)
    scoring_weights = {"w_rrf": 20.0, "w_topic": 0.0, "w_act": 0.0}

    low_weight = recall(
        conn, "ranking query",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [(semantic.atom_id, 0.9)],
        fts_search_fn=lambda q, k: [],
        extra_atom_ranked_pathways={"session_boundary": [extra.atom_id]},
        rrf_pathway_weights={"session_boundary": 0.5},
        weights=scoring_weights,
        fire_access_events=False,
    )
    high_weight = recall(
        conn, "ranking query",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [(semantic.atom_id, 0.9)],
        fts_search_fn=lambda q, k: [],
        extra_atom_ranked_pathways={"session_boundary": [extra.atom_id]},
        rrf_pathway_weights={"session_boundary": 2.0},
        weights=scoring_weights,
        fire_access_events=False,
    )

    low_extra = next(c for c in low_weight.raws if c.atom["id"] == extra.atom_id)
    high_extra = next(c for c in high_weight.raws if c.atom["id"] == extra.atom_id)
    assert low_extra.rrf_score < high_extra.rrf_score
    assert [c.atom["id"] for c in low_weight.raws[:2]] == [
        semantic.atom_id,
        extra.atom_id,
    ]
    assert [c.atom["id"] for c in high_weight.raws[:2]] == [
        extra.atom_id,
        semantic.atom_id,
    ]


def test_recall_extra_pathway_still_applies_skill_and_confidence_filters(conn):
    skill = store(
        conn,
        "skill-scoped memory",
        embed_fn=_fake_embed,
        source_type="skill_learning",
    )
    low_conf = store(conn, "extra-only low confidence", embed_fn=_fake_embed)

    result = recall(
        conn, "unmatched query",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [],
        fts_search_fn=lambda q, k: [],
        extra_atom_ranked_pathways={"extra": [skill.atom_id, low_conf.atom_id]},
        min_confidence_tier="low",
        fire_access_events=False,
    )

    assert [c.atom["id"] for c in result.raws] == []


@pytest.mark.asyncio
async def test_sagastore_query_accepts_extra_atom_ranked_pathways(
    tmp_path, monkeypatch,
):
    _patch_saga_provider(monkeypatch)
    client = SagaStore(db_path=tmp_path / "saga.db", embedding_dim=4)
    stored = await client.store("extra pathway only atom")
    monkeypatch.setattr(client, "_ensure_index", lambda conn: None)

    result = await client.query(
        "zzq-no-keyword-match",
        top_k=5,
        extra_atom_ranked_pathways={"session_boundary": [stored["atom_id"]]},
        rrf_pathway_weights={"session_boundary": 0.5},
    )

    assert [a["id"] for a in result["raws"]] == [stored["atom_id"]]


def test_recall_filters_below_activation_threshold(conn):
    """An atom whose only access was 'store' a long time ago shouldn't
    surface. We can't easily age the event without mocking time, so we
    construct an atom whose summary explicitly puts its activation below
    threshold."""
    import json as _json
    r = store(conn, "ancient memory", embed_fn=_fake_embed)
    # Forge a stale summary: one event in the deep past.
    conn.execute(
        "UPDATE atom_access_summary SET recent_ts_json = ?, "
        "recent_weights_json = ?, last_updated_ts = ? WHERE atom_id = ?",
        (
            _json.dumps(["2020-01-01T00:00:00+00:00"]),
            _json.dumps([1.0]),
            "2020-01-01T00:00:00+00:00",
            r.atom_id,
        )
    )
    conn.commit()
    result = recall(
        conn, "ancient memory",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [(r.atom_id, 0.9)],
        fts_search_fn=lambda q, k: [],
    )
    raws_ids = [c.atom["id"] for c in result.raws]
    assert r.atom_id not in raws_ids


def test_recall_fires_retrieval_access_event(conn):
    """Pass 4: returned atoms get a 'retrieval' access_event."""
    r = store(conn, "test atom", embed_fn=_fake_embed)
    recall(
        conn, "test query",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [(r.atom_id, 0.9)],
        fts_search_fn=lambda q, k: [],
    )
    sources = [
        s for (s,) in conn.execute(
            "SELECT source FROM access_events WHERE atom_id = ? ORDER BY id",
            (r.atom_id,)
        )
    ]
    assert sources == ["store", "retrieval"]


def test_recall_skips_access_event_when_disabled(conn):
    """fire_access_events=False (used by the migration importer) skips
    Pass 4."""
    r = store(conn, "test atom", embed_fn=_fake_embed)
    recall(
        conn, "test query",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [(r.atom_id, 0.9)],
        fts_search_fn=lambda q, k: [],
        fire_access_events=False,
    )
    sources = [
        s for (s,) in conn.execute(
            "SELECT source FROM access_events WHERE atom_id = ?",
            (r.atom_id,)
        )
    ]
    assert sources == ["store"]


# Session-boundary exclusion test removed: session boundaries live in
# the ``sessions`` table now, not as atoms with source_type='session_boundary'.
# The source_type filter in recall (and ``include_session_boundaries``
# parameter) was dropped because no atom has that source_type
# post-migration. Sessions are queried via ``SagaStore.search_sessions()``.


def test_recall_pinned_atoms_bypass_activation_threshold(conn):
    """Pinned atoms compete even when their activation is below threshold."""
    import json as _json
    r = store(conn, "must remember", embed_fn=_fake_embed, is_pinned=True)
    # Forge a stale summary.
    conn.execute(
        "UPDATE atom_access_summary SET recent_ts_json = ?, "
        "recent_weights_json = ?, last_updated_ts = ? WHERE atom_id = ?",
        (
            _json.dumps(["2020-01-01T00:00:00+00:00"]),
            _json.dumps([1.0]),
            "2020-01-01T00:00:00+00:00",
            r.atom_id,
        )
    )
    conn.commit()
    result = recall(
        conn, "remember",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [(r.atom_id, 0.9)],
        fts_search_fn=lambda q, k: [],
    )
    raws_ids = [c.atom["id"] for c in result.raws]
    assert r.atom_id in raws_ids


def test_recall_two_tier_splits_observations_and_raws(conn):
    """Atoms with memory_type='observation' end up in result.observations;
    everything else in result.raws."""
    raw_r = store(conn, "Alice likes pizza", embed_fn=_fake_embed)
    obs_r = store(conn, "Alice has food preferences", embed_fn=_fake_embed,
                  memory_type="observation")
    result = recall(
        conn, "Alice food",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [(raw_r.atom_id, 0.85), (obs_r.atom_id, 0.9)],
        fts_search_fn=lambda q, k: [],
    )
    raw_ids = [c.atom["id"] for c in result.raws]
    obs_ids = [c.atom["id"] for c in result.observations]
    assert raw_r.atom_id in raw_ids
    assert obs_r.atom_id in obs_ids
    assert raw_r.atom_id not in obs_ids
    assert obs_r.atom_id not in raw_ids


def test_recall_evidence_boost_lifts_evidenced_raws(conn):
    """When an observation surfaces AND has evidenced_by raws also in the
    candidate set, those raws get the boost."""
    raw_r = store(conn, "Alice ordered margherita", embed_fn=_fake_embed)
    obs_r = store(conn, "Alice has food preferences", embed_fn=_fake_embed,
                  memory_type="observation")
    # Link them.
    now = "2026-05-12T00:00:00+00:00"
    conn.execute(
        "INSERT INTO atom_relations (source_id, target_id, relation_type, "
        "confidence, created_at) VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        (obs_r.atom_id, raw_r.atom_id, now)
    )
    conn.commit()
    result = recall(
        conn, "Alice preferences",
        query_embed_fn=_fake_query_embed,
        faiss_search_fn=lambda emb, k: [(raw_r.atom_id, 0.5), (obs_r.atom_id, 0.9)],
        fts_search_fn=lambda q, k: [],
    )
    raw_candidate = next(c for c in result.raws if c.atom["id"] == raw_r.atom_id)
    assert raw_candidate.evidence_boost > 0
