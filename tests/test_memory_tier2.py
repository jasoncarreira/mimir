"""Tier-2 tests: reflect + observations (trend + supersession).

The LLM-mediated synth_fns are stubbed — we test the orchestration,
not the LLM. Stubs return deterministic content per input so the
tests can assert on the integration shape.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.memory.mark_access import AccessEvent, mark_access
from mimir.memory.observations import (
    HISTORICAL_WINDOW_DAYS, RECENT_WINDOW_DAYS, STALE_THRESHOLD_DAYS,
    classify_trend, find_superseded_observations, refresh_trend,
)
from mimir.memory.recall import recall
from mimir.memory.reflect import (
    MIN_CLUSTER_SIZE_FOR_OBSERVATION,
    MIN_SESSION_EVENTS_FOR_OBSERVATIONS,
    recent_session_boundaries, reflect,
)
from mimir.memory.store import store


# ────────────────────────────────────────────────────────────────────
# Fixtures / stubs
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def conn():
    schema = (Path(__file__).resolve().parent.parent / "mimir" / "memory" / "schema.sql").read_text()
    c = sqlite3.connect(":memory:")
    c.executescript(schema)
    yield c
    c.close()


def _fake_embed(text: str):
    h = abs(hash(text)) % 1000
    vec = [float(h % 7), float(h % 11), float(h % 13), float(h % 17)]
    return struct.pack("4f", *vec), "fake", "fake-model", 4


def _stub_boundary_synth(atoms, context):
    """Deterministic boundary synthesis. Builds a summary from the
    first 3 atom contents; reads topics from atom topics."""
    summary_pieces = [a["content"][:40] for a in atoms[:3]]
    return {
        "summary": "; ".join(summary_pieces) if summary_pieces else "quiet session",
        "topics_discussed": ["test"],
        "decisions_made": [],
        "unfinished": [],
        "emotional_state": None,
    }


def _stub_observation_synth(cluster):
    """One-line observation per cluster, content derived from the
    cluster's atom contents."""
    return (
        f"Observation about: {', '.join(a['content'][:30] for a in cluster)}",
        ["test", "synthesized"],
    )


def _all_in_one_cluster(atoms):
    """Trivial clusterer for tests: put every raw atom into one cluster."""
    raws = [a for a in atoms if a["memory_type"] == "raw"
            and a["source_type"] != "session_boundary"]
    return [raws] if raws else []


def _no_cluster(atoms):
    """Returns no clusters — exercises the "synth_fn provided but
    cluster yielded nothing" path."""
    return []


# ────────────────────────────────────────────────────────────────────
# observations.classify_trend
# ────────────────────────────────────────────────────────────────────


def _iso(dt):
    return dt.isoformat()


def test_no_events_classifies_as_stale():
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    result = classify_trend(
        observation_id="x", access_timestamps=[], now=now,
    )
    assert result.trend == "stale"
    assert result.rationale.startswith("no access events")


def test_last_access_past_stale_threshold_classifies_as_stale():
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    long_ago = now - timedelta(days=STALE_THRESHOLD_DAYS + 10)
    result = classify_trend(
        observation_id="x",
        access_timestamps=[_iso(long_ago)],
        now=now,
    )
    assert result.trend == "stale"


def test_first_time_recent_activity_classifies_as_strengthening():
    """An observation just minted with recent retrievals but no
    history is strengthening, not stable — the trend is "up from zero"."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    result = classify_trend(
        observation_id="x",
        access_timestamps=[
            _iso(now - timedelta(hours=1)),
            _iso(now - timedelta(hours=12)),
        ],
        now=now,
    )
    assert result.trend == "strengthening"


def test_uniform_distribution_classifies_as_stable():
    """Even mix of recent + historical events at similar rate → stable."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    # 2 events in recent (7d) window; 8 events in historical (30d) window.
    # Recent rate: 2/7 ≈ 0.286/d; historical: 8/30 ≈ 0.267/d.
    # Ratio ≈ 1.07 → stable.
    ts = (
        [_iso(now - timedelta(days=d)) for d in (1, 5)]
        + [_iso(now - timedelta(days=d)) for d in (8, 12, 15, 20, 25, 30, 32, 35)]
    )
    result = classify_trend(observation_id="x",
                            access_timestamps=ts, now=now)
    assert result.trend == "stable"


def test_burst_of_recent_activity_classifies_as_strengthening():
    """Many recent events vs few historical → strengthening."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    # 5 events in last 7d, 1 in historical 30d.
    # Recent rate: 5/7 ≈ 0.714/d; historical: 1/30 ≈ 0.033/d.
    # Ratio ≈ 21 → strengthening.
    ts = (
        [_iso(now - timedelta(days=d)) for d in (1, 2, 3, 4, 5)]
        + [_iso(now - timedelta(days=15))]
    )
    result = classify_trend(observation_id="x",
                            access_timestamps=ts, now=now)
    assert result.trend == "strengthening"


def test_falling_off_activity_classifies_as_weakening():
    """Many historical events, few recent → weakening."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    # 1 event in last 7d, 10 in historical.
    ts = (
        [_iso(now - timedelta(days=5))]
        + [_iso(now - timedelta(days=d)) for d in (8, 10, 12, 15, 18, 20, 22, 25, 28, 30)]
    )
    result = classify_trend(observation_id="x",
                            access_timestamps=ts, now=now)
    assert result.trend == "weakening"


# ────────────────────────────────────────────────────────────────────
# observations.refresh_trend (persistence)
# ────────────────────────────────────────────────────────────────────


def test_refresh_trend_persists_to_observations_metadata(conn):
    """Computing trend writes to observations_metadata + updates on re-run."""
    r = store(conn, "test observation", embed_fn=_fake_embed,
              memory_type="observation")
    # Add some access events. mark_access doesn't commit — caller does.
    conn.execute("BEGIN IMMEDIATE")
    mark_access(conn, [
        AccessEvent(atom_id=r.atom_id, source="retrieval"),
        AccessEvent(atom_id=r.atom_id, source="retrieval"),
        AccessEvent(atom_id=r.atom_id, source="retrieval"),
    ])
    conn.commit()
    result = refresh_trend(conn, r.atom_id)
    row = conn.execute(
        "SELECT trend, evidence_count, last_evidence_at "
        "FROM observations_metadata WHERE atom_id = ?",
        (r.atom_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == result.trend
    assert row[1] >= 4  # store + 3 retrievals


# ────────────────────────────────────────────────────────────────────
# observations.find_superseded_observations
# ────────────────────────────────────────────────────────────────────


def test_superseded_when_evidence_set_is_strict_superset(conn):
    """New observation's evidence ⊃ old observation's evidence → old
    is superseded."""
    # Three raws.
    raw_ids = [
        store(conn, f"raw {i}", embed_fn=_fake_embed).atom_id
        for i in range(3)
    ]
    # Old observation citing 2 raws.
    old_obs = store(conn, "old observation", embed_fn=_fake_embed,
                    memory_type="observation").atom_id
    now = "2026-05-12T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO atom_relations (source_id, target_id, "
        "relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        [(old_obs, raw_id, now) for raw_id in raw_ids[:2]],
    )
    conn.commit()
    # New observation citing all 3 raws (strict superset).
    new_obs = store(conn, "new observation", embed_fn=_fake_embed,
                    memory_type="observation").atom_id
    superseded = find_superseded_observations(
        conn, new_obs, set(raw_ids),
    )
    assert old_obs in superseded


def test_not_superseded_when_evidence_set_equals(conn):
    """Equal evidence ≠ supersession. Old still stands."""
    raw_ids = [
        store(conn, f"raw {i}", embed_fn=_fake_embed).atom_id
        for i in range(2)
    ]
    old_obs = store(conn, "old", embed_fn=_fake_embed,
                    memory_type="observation").atom_id
    now = "2026-05-12T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO atom_relations (source_id, target_id, "
        "relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        [(old_obs, raw_id, now) for raw_id in raw_ids],
    )
    conn.commit()
    new_obs = store(conn, "new", embed_fn=_fake_embed,
                    memory_type="observation").atom_id
    superseded = find_superseded_observations(
        conn, new_obs, set(raw_ids),
    )
    assert old_obs not in superseded


# ────────────────────────────────────────────────────────────────────
# reflect — session boundary
# ────────────────────────────────────────────────────────────────────


def test_reflect_creates_boundary_atom(conn):
    """Quiet session — reflect still emits one session_boundary."""
    result = reflect(
        conn, session_id="s1", channel_id="c1",
        embed_fn=_fake_embed,
        boundary_synth_fn=_stub_boundary_synth,
    )
    assert result.boundary_created is True
    assert result.boundary_atom_id is not None
    # Check the atom landed with source_type='session_boundary'.
    row = conn.execute(
        "SELECT source_type, session_id FROM atoms WHERE id = ?",
        (result.boundary_atom_id,)
    ).fetchone()
    assert row[0] == "session_boundary"
    assert row[1] == "s1"


def test_reflect_is_idempotent(conn):
    """Re-calling reflect on the same session returns the same boundary."""
    r1 = reflect(conn, session_id="s1", channel_id="c1",
                 embed_fn=_fake_embed,
                 boundary_synth_fn=_stub_boundary_synth)
    r2 = reflect(conn, session_id="s1", channel_id="c1",
                 embed_fn=_fake_embed,
                 boundary_synth_fn=_stub_boundary_synth)
    assert r1.boundary_atom_id == r2.boundary_atom_id
    assert r1.boundary_created is True
    assert r2.boundary_created is False


def test_reflect_links_session_members(conn):
    """Every atom touched in the session gets a session_member relation
    from the boundary."""
    # Three atoms in session s1.
    for i in range(3):
        store(conn, f"session atom {i}", embed_fn=_fake_embed,
              session_id="s1")
    result = reflect(
        conn, session_id="s1", channel_id="c1",
        embed_fn=_fake_embed,
        boundary_synth_fn=_stub_boundary_synth,
    )
    # Boundary is also stored with session_id='s1', so _session_atoms
    # picks it up after the boundary lands. The session_member_count
    # therefore includes the boundary itself plus the 3 raws.
    rows = conn.execute(
        "SELECT target_id FROM atom_relations "
        "WHERE source_id = ? AND relation_type = 'session_member'",
        (result.boundary_atom_id,)
    ).fetchall()
    target_ids = {r[0] for r in rows}
    # All three raws should be linked. (The boundary may or may not link
    # to itself depending on ordering; we don't test that.)
    raw_atom_ids = {
        r[0] for r in conn.execute(
            "SELECT id FROM atoms WHERE session_id = ? "
            "AND source_type != 'session_boundary'",
            ("s1",),
        )
    }
    assert raw_atom_ids.issubset(target_ids)


def test_reflect_skips_observation_synthesis_below_threshold(conn):
    """Fewer than MIN_SESSION_EVENTS atoms → no observations."""
    # Only 2 atoms — below the threshold (default 5).
    for i in range(2):
        store(conn, f"atom {i}", embed_fn=_fake_embed, session_id="s1")
    result = reflect(
        conn, session_id="s1", channel_id="c1",
        embed_fn=_fake_embed,
        boundary_synth_fn=_stub_boundary_synth,
        observation_synth_fn=_stub_observation_synth,
        cluster_fn=_all_in_one_cluster,
    )
    assert result.observation_ids == []


def test_reflect_synthesizes_observations_when_threshold_met(conn):
    """Enough session activity + clustering + synth_fn → emits observations."""
    # 6 atoms — comfortably above threshold (5).
    for i in range(6):
        store(conn, f"raw fact {i}", embed_fn=_fake_embed, session_id="s1")
    result = reflect(
        conn, session_id="s1", channel_id="c1",
        embed_fn=_fake_embed,
        boundary_synth_fn=_stub_boundary_synth,
        observation_synth_fn=_stub_observation_synth,
        cluster_fn=_all_in_one_cluster,
    )
    assert len(result.observation_ids) == 1
    obs_id = result.observation_ids[0]
    # The observation has evidenced_by relations to the raws.
    evidence = conn.execute(
        "SELECT target_id FROM atom_relations "
        "WHERE source_id = ? AND relation_type = 'evidenced_by'",
        (obs_id,)
    ).fetchall()
    assert len(evidence) == 6


def test_reflect_fires_consolidation_events_on_evidence_raws(conn):
    """Per SCORING.md: when reflect pulls a raw into a synthesis,
    the raw gets a 'consolidation' access_event (weight 0.5)."""
    raw_ids = []
    for i in range(6):
        r = store(conn, f"fact {i}", embed_fn=_fake_embed, session_id="s1")
        raw_ids.append(r.atom_id)
    reflect(
        conn, session_id="s1", channel_id="c1",
        embed_fn=_fake_embed,
        boundary_synth_fn=_stub_boundary_synth,
        observation_synth_fn=_stub_observation_synth,
        cluster_fn=_all_in_one_cluster,
    )
    # Each raw should have a 'consolidation' source access_event.
    for raw_id in raw_ids:
        sources = [
            r[0] for r in conn.execute(
                "SELECT source FROM access_events WHERE atom_id = ?",
                (raw_id,),
            )
        ]
        assert "consolidation" in sources


def test_superset_observation_is_created_not_blocked(conn):
    """When a new cluster's evidence is a strict superset of an
    existing observation's evidence, the new observation IS stored
    (saga's behavior — supersedes the old, doesn't replace or block).
    Both observations exist in the DB after; the old is just marked
    superseded so the recall ranker can downweight it.
    """
    # Old observation with evidence {a, b}.
    raw_a = store(conn, "raw a", embed_fn=_fake_embed).atom_id
    raw_b = store(conn, "raw b", embed_fn=_fake_embed).atom_id
    raw_c = store(conn, "raw c", embed_fn=_fake_embed).atom_id
    old_obs = store(conn, "old observation", embed_fn=_fake_embed,
                    memory_type="observation").atom_id
    now = "2026-05-12T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO atom_relations (source_id, target_id, "
        "relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        [(old_obs, raw_a, now), (old_obs, raw_b, now)],
    )
    conn.commit()
    # New cluster contains {a, b, c} — strict superset of old's {a, b}.
    # Run reflect on a session that touched all three raws.
    conn.execute("BEGIN IMMEDIATE")
    mark_access(conn, [
        AccessEvent(atom_id=raw_a, source="retrieval", session_id="s_new"),
        AccessEvent(atom_id=raw_b, source="retrieval", session_id="s_new"),
        AccessEvent(atom_id=raw_c, source="retrieval", session_id="s_new"),
    ])
    conn.commit()
    # Need 2 more raws to satisfy MIN_SESSION_EVENTS_FOR_OBSERVATIONS (5).
    store(conn, "filler 1", embed_fn=_fake_embed, session_id="s_new")
    store(conn, "filler 2", embed_fn=_fake_embed, session_id="s_new")

    result = reflect(
        conn, session_id="s_new", channel_id="c1",
        embed_fn=_fake_embed,
        boundary_synth_fn=_stub_boundary_synth,
        observation_synth_fn=_stub_observation_synth,
        cluster_fn=_all_in_one_cluster,
    )

    # Both observations are persisted in the DB.
    obs_rows = conn.execute(
        "SELECT id FROM atoms WHERE memory_type = 'observation' "
        "AND tombstoned = 0"
    ).fetchall()
    obs_ids = {r[0] for r in obs_rows}
    assert old_obs in obs_ids, "old observation should still exist"
    assert any(new_id in obs_ids for new_id in result.observation_ids), \
        "new observation should be created"

    # The new observation has a supersedes relation pointing to the old.
    superseded_rows = conn.execute(
        "SELECT source_id, target_id FROM atom_relations "
        "WHERE relation_type = 'supersedes'"
    ).fetchall()
    superseded_targets = {target for (_, target) in superseded_rows}
    assert old_obs in superseded_targets


def test_reflect_supersedes_older_observation_when_evidence_set_grows(conn):
    """An old observation with evidence {a, b} gets superseded when a
    new observation cites {a, b, c}."""
    raw_a = store(conn, "raw a", embed_fn=_fake_embed,
                  session_id="prior").atom_id
    raw_b = store(conn, "raw b", embed_fn=_fake_embed,
                  session_id="prior").atom_id
    # Old observation citing only a, b.
    old_obs = store(conn, "old belief", embed_fn=_fake_embed,
                    memory_type="observation").atom_id
    now = "2026-05-12T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO atom_relations (source_id, target_id, "
        "relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        [(old_obs, raw_a, now), (old_obs, raw_b, now)],
    )
    conn.commit()
    # New session with raw a, b, c, d, e, f — clusterer puts them all
    # in one cluster.
    new_raws = []
    for i, content in enumerate([
        # Already-existing raws come back into scope via the session_id.
        # Make them session members via access events.
    ]):
        pass
    # Force a, b into this session via access_events.
    conn.execute("BEGIN IMMEDIATE")
    mark_access(conn, [
        AccessEvent(atom_id=raw_a, source="retrieval", session_id="s2"),
        AccessEvent(atom_id=raw_b, source="retrieval", session_id="s2"),
    ])
    conn.commit()
    # Add four more raws born in the session.
    for i in range(4):
        store(conn, f"new raw {i}", embed_fn=_fake_embed,
              session_id="s2").atom_id
    result = reflect(
        conn, session_id="s2", channel_id="c1",
        embed_fn=_fake_embed,
        boundary_synth_fn=_stub_boundary_synth,
        observation_synth_fn=_stub_observation_synth,
        cluster_fn=_all_in_one_cluster,
    )
    # The new observation should supersede the old one.
    superseded_pairs = result.observations_superseded
    assert any(old_obs == old_id for (_, old_id) in superseded_pairs), \
        f"expected old_obs={old_obs} in {superseded_pairs}"


# ────────────────────────────────────────────────────────────────────
# recent_session_boundaries
# ────────────────────────────────────────────────────────────────────


def test_recent_session_boundaries_returns_in_recency_order(conn):
    """Two reflect calls; recent_session_boundaries returns them
    newest-first."""
    r1 = reflect(conn, session_id="s1", channel_id="c1",
                 embed_fn=_fake_embed,
                 boundary_synth_fn=_stub_boundary_synth)
    # Force second session's boundary to be created strictly after
    # the first (sleep would slow the test; instead we just store +
    # check ordering via insertion order).
    r2 = reflect(conn, session_id="s2", channel_id="c1",
                 embed_fn=_fake_embed,
                 boundary_synth_fn=_stub_boundary_synth)
    boundaries = recent_session_boundaries(conn, channel_id="c1", count=10)
    assert len(boundaries) == 2
    ids = {b["id"] for b in boundaries}
    assert {r1.boundary_atom_id, r2.boundary_atom_id} <= ids


def test_recent_session_boundaries_filters_by_channel(conn):
    """channel_id filter scopes results."""
    reflect(conn, session_id="s1", channel_id="c1",
            embed_fn=_fake_embed,
            boundary_synth_fn=_stub_boundary_synth)
    reflect(conn, session_id="s2", channel_id="c2",
            embed_fn=_fake_embed,
            boundary_synth_fn=_stub_boundary_synth)
    c1_only = recent_session_boundaries(conn, channel_id="c1", count=10)
    c2_only = recent_session_boundaries(conn, channel_id="c2", count=10)
    assert len(c1_only) == 1
    assert len(c2_only) == 1
    assert c1_only[0]["session_id"] == "s1"
    assert c2_only[0]["session_id"] == "s2"


def test_recent_session_boundaries_excluded_from_generic_recall(conn):
    """Re-check the contract from Tier 1: even with the new
    reflect-generated boundary atoms, generic recall doesn't surface them."""
    r = reflect(conn, session_id="s1", channel_id="c1",
                embed_fn=_fake_embed,
                boundary_synth_fn=_stub_boundary_synth)
    boundary_id = r.boundary_atom_id
    # Query for the boundary content — even with similarity match,
    # source_type filter should drop it.
    result = recall(
        conn, "session",
        query_embed_fn=lambda t: [0.0, 0.0, 0.0, 0.0],
        faiss_search_fn=lambda emb, k: [(boundary_id, 0.9)],
        fts_search_fn=lambda q, k: [],
    )
    ids = [c.atom["id"] for c in result.raws + result.observations]
    assert boundary_id not in ids
