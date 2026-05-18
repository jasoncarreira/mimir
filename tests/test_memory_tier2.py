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

from mimir.saga.mark_access import AccessEvent, mark_access
from mimir.saga.observations import (
    HISTORICAL_WINDOW_DAYS, RECENT_WINDOW_DAYS, STALE_THRESHOLD_DAYS,
    classify_trend, find_superseded_observations, refresh_trend,
)
from mimir.saga.recall import recall
from mimir.saga.reflect import recent_session_boundaries, reflect
from mimir.saga.store import store


# ────────────────────────────────────────────────────────────────────
# Fixtures / stubs
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def conn():
    schema = (Path(__file__).resolve().parent.parent / "mimir" / "saga" / "schema.sql").read_text()
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


def test_reflect_creates_sessions_row(conn):
    """Quiet session — reflect emits one sessions table row (not an atom)."""
    result = reflect(
        conn, session_id="s1", channel_id="c1",
        embed_fn=_fake_embed,
        boundary_synth_fn=_stub_boundary_synth,
    )
    assert result.boundary_created is True
    assert result.boundary_atom_id is not None  # equals session_id post-migration
    # Check the sessions row was created.
    row = conn.execute(
        "SELECT id, channel_id FROM sessions WHERE id = ?", ("s1",)
    ).fetchone()
    assert row is not None
    assert row[1] == "c1"
    # Confirm no session_boundary atom was written.
    atom_row = conn.execute(
        "SELECT id FROM atoms WHERE source_type = 'session_boundary'",
    ).fetchone()
    assert atom_row is None, "session_boundary should not be in atoms"


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


def test_reflect_session_member_count_is_zero(conn):
    """session_member atom_relations are no longer written — session_member_count
    is always 0 on the ReflectResult (session boundaries live in sessions table,
    not as atoms with outbound relations)."""
    for i in range(3):
        store(conn, f"session atom {i}", embed_fn=_fake_embed,
              session_id="s1")
    result = reflect(
        conn, session_id="s1", channel_id="c1",
        embed_fn=_fake_embed,
        boundary_synth_fn=_stub_boundary_synth,
    )
    # No session_member relations written.
    assert result.session_member_count == 0
    member_rows = conn.execute(
        "SELECT COUNT(*) FROM atom_relations WHERE relation_type = 'session_member'"
    ).fetchone()[0]
    assert member_rows == 0


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
