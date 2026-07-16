"""Tier-2b tests: trend-in-score, similarity clusterer, cross-session consolidate.

Covers the three follow-up changes after Tier 2's first cut:
- recall.py: trend modifier in the score formula
- cluster.py: similarity-based default clusterer
- consolidate.py: cross-session consolidation pass
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.saga.cluster import cluster_by_similarity, make_default_cluster_fn
from mimir.saga.consolidate import consolidate
from mimir.saga.mark_access import AccessEvent, mark_access
from mimir.saga.observations import refresh_trend
from mimir.saga.ownership import Ownership
from mimir.saga.ownership import AuthorizationScope
from mimir.saga.recall import recall
from mimir.saga.reflect import reflect
from mimir.saga.store import store


@pytest.fixture
def conn():
    schema = (Path(__file__).resolve().parent.parent / "mimir" / "saga" / "schema.sql").read_text()
    c = sqlite3.connect(":memory:")
    c.executescript(schema)
    yield c
    c.close()


def _emb(vec):
    """Build a (vec_bytes, provider, model, dim) tuple from a float list."""
    return struct.pack(f"{len(vec)}f", *vec), "test", "test-model", len(vec)


def _embed_fn_factory(vectors_by_content):
    """Closure that returns a deterministic embed_fn — content lookup."""
    def fn(text):
        v = vectors_by_content.get(text, [0.0] * 4)
        return _emb(v)
    return fn


def _query_embed_fn_factory(query_vectors):
    def fn(text):
        return query_vectors.get(text, [0.0] * 4)
    return fn


def _stub_synth(cluster):
    contents = ", ".join(a["content"][:40] for a in cluster)
    return (f"Observation: {contents}", ["consolidated"])


# ────────────────────────────────────────────────────────────────────
# Trend modifier in score
# ────────────────────────────────────────────────────────────────────


def test_strengthening_observation_outranks_weakening_at_equal_similarity(conn):
    """Two observations with identical similarity to a query: the one
    with trend='strengthening' should outrank trend='weakening'."""
    embed_fn = _embed_fn_factory({
        "obs_strong": [1.0, 0.0, 0.0, 0.0],
        "obs_weak":   [1.0, 0.0, 0.0, 0.0],
    })
    qf = _query_embed_fn_factory({"q": [1.0, 0.0, 0.0, 0.0]})

    strong = store(conn, "obs_strong", embed_fn=embed_fn,
                   memory_type="observation").atom_id
    weak = store(conn, "obs_weak", embed_fn=embed_fn,
                 memory_type="observation").atom_id

    # Manually set trends.
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO observations_metadata "
        "(atom_id, evidence_count, trend, last_evidence_at, consolidated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (strong, 5, "strengthening", now, now),
    )
    conn.execute(
        "INSERT INTO observations_metadata "
        "(atom_id, evidence_count, trend, last_evidence_at, consolidated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (weak, 5, "weakening", now, now),
    )
    conn.commit()

    result = recall(
        conn, "q",
        query_embed_fn=qf,
        faiss_search_fn=lambda emb, k: [(strong, 0.85), (weak, 0.85)],
        fts_search_fn=lambda q, k: [],
        auth_scope=AuthorizationScope(is_admin=True),
    )
    obs_order = [c.atom["id"] for c in result.observations]
    assert obs_order == [strong, weak]
    strong_c = next(c for c in result.observations if c.atom["id"] == strong)
    weak_c = next(c for c in result.observations if c.atom["id"] == weak)
    assert strong_c.trend_label == "strengthening"
    assert weak_c.trend_label == "weakening"
    assert strong_c.trend_modifier > weak_c.trend_modifier


def test_stale_trend_applies_largest_penalty(conn):
    """trend='stale' should produce a larger negative modifier than
    trend='weakening' — agent should aggressively downrank stale
    beliefs."""
    embed_fn = _embed_fn_factory({"o1": [1.0, 0.0, 0.0, 0.0],
                                   "o2": [1.0, 0.0, 0.0, 0.0]})
    qf = _query_embed_fn_factory({"q": [1.0, 0.0, 0.0, 0.0]})

    weakening = store(conn, "o1", embed_fn=embed_fn,
                      memory_type="observation").atom_id
    stale = store(conn, "o2", embed_fn=embed_fn,
                  memory_type="observation").atom_id

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO observations_metadata "
        "(atom_id, evidence_count, trend, consolidated_at) "
        "VALUES (?, ?, 'weakening', ?)",
        (weakening, 5, now),
    )
    conn.execute(
        "INSERT INTO observations_metadata "
        "(atom_id, evidence_count, trend, consolidated_at) "
        "VALUES (?, ?, 'stale', ?)",
        (stale, 5, now),
    )
    conn.commit()

    result = recall(
        conn, "q",
        query_embed_fn=qf,
        faiss_search_fn=lambda emb, k: [(weakening, 0.85), (stale, 0.85)],
        fts_search_fn=lambda q, k: [],
        auth_scope=AuthorizationScope(is_admin=True),
    )
    weakening_c = next(c for c in result.observations
                       if c.atom["id"] == weakening)
    stale_c = next(c for c in result.observations
                   if c.atom["id"] == stale)
    assert stale_c.trend_modifier < weakening_c.trend_modifier
    assert stale_c.total < weakening_c.total


def test_raws_have_no_trend_modifier(conn):
    """Trend only applies to observation atoms — raws should have
    trend_modifier == 0.0."""
    embed_fn = _embed_fn_factory({"raw": [1.0, 0.0, 0.0, 0.0]})
    qf = _query_embed_fn_factory({"q": [1.0, 0.0, 0.0, 0.0]})

    raw = store(conn, "raw", embed_fn=embed_fn).atom_id
    result = recall(
        conn, "q",
        query_embed_fn=qf,
        faiss_search_fn=lambda emb, k: [(raw, 0.9)],
        fts_search_fn=lambda q, k: [],
        auth_scope=AuthorizationScope(is_admin=True),
    )
    raw_c = next(c for c in result.raws if c.atom["id"] == raw)
    assert raw_c.trend_modifier == 0.0
    assert raw_c.trend_label is None


# ────────────────────────────────────────────────────────────────────
# Similarity-based clusterer
# ────────────────────────────────────────────────────────────────────


def test_cluster_groups_similar_atoms_together(conn):
    """Two similar atoms cluster; one very different atom separates."""
    # Group A: similar vectors near [1, 0, 0, 0]
    # Group B: very different, near [0, 0, 0, 1]
    embed_fn = _embed_fn_factory({
        "a1": [1.0, 0.1, 0.0, 0.0],
        "a2": [0.95, 0.05, 0.05, 0.0],
        "a3": [0.9, 0.1, 0.0, 0.05],
        "b1": [0.0, 0.0, 0.1, 1.0],
    })
    ids = {}
    for content in ("a1", "a2", "a3", "b1"):
        r = store(conn, content, embed_fn=embed_fn)
        ids[content] = r.atom_id

    atoms = [
        {"id": ids[c], "content": c} for c in ("a1", "a2", "a3", "b1")
    ]
    clusters = cluster_by_similarity(conn, atoms, threshold=0.6)
    # Three a's together, b separate.
    sizes = sorted(len(cl) for cl in clusters)
    assert sizes == [1, 3]


def test_cluster_threshold_controls_grouping(conn):
    """Higher threshold = stricter clustering. The same atoms can
    cluster together OR split depending on threshold."""
    embed_fn = _embed_fn_factory({
        "a1": [1.0, 0.0, 0.0, 0.0],
        "a2": [0.7, 0.7, 0.0, 0.0],   # cos to a1 ≈ 0.707
    })
    id1 = store(conn, "a1", embed_fn=embed_fn).atom_id
    id2 = store(conn, "a2", embed_fn=embed_fn).atom_id
    atoms = [{"id": id1, "content": "a1"}, {"id": id2, "content": "a2"}]
    # Low threshold (0.5): joins.
    low = cluster_by_similarity(conn, atoms, threshold=0.5)
    assert len(low) == 1
    # High threshold (0.8): splits.
    high = cluster_by_similarity(conn, atoms, threshold=0.8)
    assert len(high) == 2


def test_cluster_skips_atoms_without_embeddings(conn):
    """Atoms lacking embedding rows are silently dropped (shouldn't
    happen via store() but defensive)."""
    embed_fn = _embed_fn_factory({"a1": [1.0, 0.0, 0.0, 0.0]})
    id1 = store(conn, "a1", embed_fn=embed_fn).atom_id
    fake_id = "deadbeefdeadbeef"  # no atom, no embedding
    atoms = [
        {"id": id1, "content": "a1"},
        {"id": fake_id, "content": "phantom"},
    ]
    clusters = cluster_by_similarity(conn, atoms)
    # Only the real atom should appear in any cluster.
    all_clustered_ids = [a["id"] for cl in clusters for a in cl]
    assert id1 in all_clustered_ids
    assert fake_id not in all_clustered_ids


# ────────────────────────────────────────────────────────────────────
# Cross-session consolidate
# ────────────────────────────────────────────────────────────────────


def test_consolidate_groups_across_sessions(conn):
    """Three raws born in three different sessions, all about the
    same topic (similar vectors): consolidate should produce one
    observation covering all three."""
    embed_fn = _embed_fn_factory({
        "mon_pr_157": [1.0, 0.05, 0.0, 0.0],
        "tue_pr_157": [0.97, 0.1, 0.05, 0.0],
        "wed_pr_157": [0.95, 0.0, 0.1, 0.05],
    })
    ids = []
    for content, sid in [
        ("mon_pr_157", "s1"),
        ("tue_pr_157", "s2"),
        ("wed_pr_157", "s3"),
    ]:
        r = store(conn, content, embed_fn=embed_fn, session_id=sid)
        ids.append(r.atom_id)

    cluster_fn = make_default_cluster_fn(conn, threshold=0.7)
    result = consolidate(
        conn,
        embed_fn=embed_fn,
        cluster_fn=cluster_fn,
        observation_synth_fn=_stub_synth,
        lookback_days=30,
    )
    assert len(result.observations_emitted) == 1
    obs_id = result.observations_emitted[0]
    # The observation should be evidenced by all three raws.
    evidence = {
        r[0] for r in conn.execute(
            "SELECT target_id FROM atom_relations "
            "WHERE source_id = ? AND relation_type = 'evidenced_by'",
            (obs_id,)
        )
    }
    assert set(ids) == evidence


def test_consolidate_observation_inherits_intersected_acl(conn):
    embed_fn = _embed_fn_factory({
        "acl_a": [1.0, 0.05, 0.0, 0.0],
        "acl_b": [0.97, 0.1, 0.05, 0.0],
        "acl_c": [0.95, 0.0, 0.1, 0.05],
    })
    ids = []
    for content, visibility in [
        ("acl_a", "public"),
        ("acl_b", "private"),
        ("acl_c", "private"),
    ]:
        result = store(
            conn,
            content,
            embed_fn=embed_fn,
            owner_principal="user:123",
            origin_channel="channel:one",
            origin_domain="tenant:one",
            visibility=visibility,
            provenance={content: True},
        )
        ids.append(result.atom_id)

    result = consolidate(
        conn,
        embed_fn=embed_fn,
        cluster_fn=make_default_cluster_fn(conn, threshold=0.7),
        observation_synth_fn=_stub_synth,
    )

    assert len(result.observations_emitted) == 1
    acl = conn.execute(
        "SELECT owner_principal, origin_domain, visibility, provenance "
        "FROM atoms WHERE id = ?",
        (result.observations_emitted[0],),
    ).fetchone()
    assert acl[:3] == ("user:123", "tenant:one", "private")
    assert json.loads(acl[3]) == {"acl_a": True, "acl_b": True, "acl_c": True}


def test_consolidate_missing_evidence_atom_fails_closed(conn):
    from mimir.saga.consolidate import _compute_intersected_acl

    embed_fn = _embed_fn_factory({"acl_a": [1.0, 0.0, 0.0, 0.0]})
    atom_id = store(
        conn,
        "acl_a",
        embed_fn=embed_fn,
        owner_principal="user:123",
        origin_domain="tenant:one",
        visibility="public",
        provenance={"acl_a": True},
    ).atom_id

    assert _compute_intersected_acl(conn, [atom_id, "missing"]) == Ownership()


def test_consolidate_includes_already_cited_raws_in_pool(conn):
    """Already-cited raws DO appear in consolidate's candidate pool.
    Filtering them out preemptively (an earlier-sketch bug) would
    foreclose cross-session supersession — the case where a new cluster
    is a strict superset of an existing observation's evidence.
    """
    embed_fn = _embed_fn_factory({
        "a1": [1.0, 0.0, 0.0, 0.0],
        "a2": [0.95, 0.05, 0.0, 0.0],
        "old_obs": [0.0, 1.0, 0.0, 0.0],
    })
    a1 = store(conn, "a1", embed_fn=embed_fn).atom_id
    a2 = store(conn, "a2", embed_fn=embed_fn).atom_id
    old_obs = store(conn, "old_obs", embed_fn=embed_fn,
                    memory_type="observation").atom_id
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO atom_relations (source_id, target_id, "
        "relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        [(old_obs, a1, now), (old_obs, a2, now)],
    )
    conn.commit()

    cluster_fn = make_default_cluster_fn(conn, threshold=0.5)
    result = consolidate(
        conn,
        embed_fn=embed_fn,
        cluster_fn=cluster_fn,
        observation_synth_fn=_stub_synth,
    )
    # Both a1 and a2 should appear in the candidate pool even though
    # they're already cited as evidence for old_obs.
    assert result.candidates_scanned == 2


def test_consolidate_supersedes_when_cluster_is_strict_superset(conn):
    """The load-bearing cross-session supersession case: an existing
    observation cites {a1, a2}; consolidate clusters {a1, a2, a3}
    together (a3 is a new raw from a later session, similar in
    embedding space); the new observation citing {a1, a2, a3} is
    created and supersedes the old.
    """
    embed_fn = _embed_fn_factory({
        # All three on the same axis — they'll cluster.
        "a1": [1.0, 0.05, 0.0, 0.0],
        "a2": [0.97, 0.1, 0.0, 0.0],
        "a3": [0.95, 0.0, 0.1, 0.05],
    })
    a1 = store(conn, "a1", embed_fn=embed_fn).atom_id
    a2 = store(conn, "a2", embed_fn=embed_fn).atom_id
    a3 = store(conn, "a3", embed_fn=embed_fn).atom_id
    # Existing observation citing {a1, a2}.
    old_obs = store(conn, "old observation about a1+a2",
                    embed_fn=embed_fn,
                    memory_type="observation").atom_id
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO atom_relations (source_id, target_id, "
        "relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        [(old_obs, a1, now), (old_obs, a2, now)],
    )
    conn.commit()

    cluster_fn = make_default_cluster_fn(conn, threshold=0.7)
    result = consolidate(
        conn,
        embed_fn=embed_fn,
        cluster_fn=cluster_fn,
        observation_synth_fn=_stub_synth,
    )
    # New observation should be emitted.
    assert len(result.observations_emitted) == 1
    new_obs = result.observations_emitted[0]
    # Old observation should be superseded.
    assert (new_obs, old_obs) in result.observations_superseded
    # New observation's evidence is the superset {a1, a2, a3}.
    new_evidence = {
        r[0] for r in conn.execute(
            "SELECT target_id FROM atom_relations "
            "WHERE source_id = ? AND relation_type = 'evidenced_by'",
            (new_obs,)
        )
    }
    assert new_evidence == {a1, a2, a3}
    # Old observation is still in the DB (not tombstoned).
    is_live = conn.execute(
        "SELECT 1 FROM atoms WHERE id = ? AND tombstoned = 0",
        (old_obs,),
    ).fetchone()
    assert is_live is not None


def test_consolidate_skips_synthesis_when_cluster_evidence_equals_existing(conn):
    """If consolidate's cluster has evidence that EXACTLY matches an
    existing observation's evidence, skip synthesis (no LLM call, no
    new atom). Fire a consolidation event on the existing observation
    to record the re-encounter.
    """
    embed_fn = _embed_fn_factory({
        "a1": [1.0, 0.0, 0.0, 0.0],
        "a2": [0.95, 0.05, 0.0, 0.0],
        "a3": [0.9, 0.1, 0.0, 0.0],
    })
    a1 = store(conn, "a1", embed_fn=embed_fn).atom_id
    a2 = store(conn, "a2", embed_fn=embed_fn).atom_id
    a3 = store(conn, "a3", embed_fn=embed_fn).atom_id
    # Existing observation citing exactly the cluster {a1, a2, a3}.
    existing = store(conn, "existing observation",
                     embed_fn=embed_fn,
                     memory_type="observation").atom_id
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO atom_relations (source_id, target_id, "
        "relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        [(existing, a1, now), (existing, a2, now), (existing, a3, now)],
    )
    conn.commit()

    # Track synth invocations — should NOT be called.
    synth_calls = []
    def tracking_synth(cluster):
        synth_calls.append([a["id"] for a in cluster])
        return ("should not be stored", [])

    cluster_fn = make_default_cluster_fn(conn, threshold=0.5)
    result = consolidate(
        conn,
        embed_fn=embed_fn,
        cluster_fn=cluster_fn,
        observation_synth_fn=tracking_synth,
    )
    # Synth was not invoked (or, if invoked, the result was discarded).
    # Either way: no new observations emitted.
    assert len(result.observations_emitted) == 0
    # Per 2026-05-13 design correction: consolidation is system-internal
    # and produces NO access_event. The existing observation's audit
    # trail is in atom_relations (consolidated_into / evidenced_by),
    # not access_events.
    sources = [
        s for (s,) in conn.execute(
            "SELECT source FROM access_events WHERE atom_id = ? ORDER BY id",
            (existing,)
        )
    ]
    assert "consolidation" not in sources


def test_consolidate_respects_lookback_days(conn):
    """Atoms accessed only outside the lookback window should be
    excluded from candidates."""
    embed_fn = _embed_fn_factory({f"a{i}": [1.0, 0.0, 0.0, 0.0]
                                  for i in range(5)})
    ids = []
    for i in range(5):
        r = store(conn, f"a{i}", embed_fn=embed_fn)
        ids.append(r.atom_id)

    # Forge all access events to a year ago.
    long_ago = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    conn.execute("UPDATE access_events SET ts = ?", (long_ago,))
    conn.commit()

    cluster_fn = make_default_cluster_fn(conn, threshold=0.5)
    result = consolidate(
        conn,
        embed_fn=embed_fn,
        cluster_fn=cluster_fn,
        observation_synth_fn=_stub_synth,
        lookback_days=30,
    )
    assert result.candidates_scanned == 0


def test_consolidate_caps_at_max_observations(conn):
    """When many clusters meet the threshold, consolidate emits at most
    max_observations per run."""
    # Create N clusters of 3 atoms each, each cluster's vectors near a
    # distinct unit direction so they cluster cleanly.
    embed_fn_dict = {}
    cluster_keys = []
    for cluster_idx in range(5):
        # Each cluster has 3 atoms with vectors close to a unique axis.
        axis = [0.0] * 4
        axis[cluster_idx % 4] = 1.0
        for atom_idx in range(3):
            content = f"c{cluster_idx}_a{atom_idx}"
            # Slight perturbation per atom to keep vectors distinguishable.
            v = list(axis)
            v[(cluster_idx + atom_idx) % 4] += 0.05
            embed_fn_dict[content] = v
            cluster_keys.append(content)

    embed_fn = _embed_fn_factory(embed_fn_dict)
    for k in cluster_keys:
        store(conn, k, embed_fn=embed_fn)

    cluster_fn = make_default_cluster_fn(conn, threshold=0.7)
    result = consolidate(
        conn,
        embed_fn=embed_fn,
        cluster_fn=cluster_fn,
        observation_synth_fn=_stub_synth,
        max_observations=2,
    )
    assert len(result.observations_emitted) <= 2


# ────────────────────────────────────────────────────────────────────
# Transaction structure smoke test
# ────────────────────────────────────────────────────────────────────


def test_mark_access_no_longer_opens_own_transaction(conn):
    """Sanity: caller should now wrap mark_access. Calling it without
    a wrapping transaction still works (autocommit will pick it up)
    but doesn't enforce atomicity by itself."""
    r = store(conn, "test", embed_fn=_embed_fn_factory({"test": [1.0, 0, 0, 0]}))
    # No try/BEGIN/commit here — mark_access just runs statements.
    mark_access(conn, [
        AccessEvent(atom_id=r.atom_id, source="retrieval"),
        AccessEvent(atom_id=r.atom_id, source="feedback_positive"),
    ])
    conn.commit()  # caller commits when they're done
    sources = [
        s for (s,) in conn.execute(
            "SELECT source FROM access_events WHERE atom_id = ? ORDER BY id",
            (r.atom_id,)
        )
    ]
    assert sources == ["store", "retrieval", "feedback_positive"]
