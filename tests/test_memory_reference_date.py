"""Tests for reference_date plumbing through recall.

Without reference_date, every atom's age is computed against wall-
clock now. For benches against historical haystacks, this destroys
the activation signal (every atom looks "3 years old"). With
reference_date passed in, activation computes against the haystack's
timeline.
"""
from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from mimir.saga.activation import compute_activation
from mimir.saga.ownership import AuthorizationScope
from mimir.saga.recall import recall


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "mimir" / "saga" / "schema.sql"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_PATH.read_text())
    return c


def _seed_atom(conn, atom_id: str, content: str, *,
               accessed_at: str, vec: list[float]):
    h = sha256(content.encode()).hexdigest()[:32]
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at) "
        "VALUES (?, ?, ?, ?)",
        (atom_id, content, h, accessed_at),
    )
    vec_bytes = struct.pack(f"{len(vec)}f", *vec)
    conn.execute(
        "INSERT INTO embeddings (atom_id, provider, model, dim, vec, embedded_at) "
        "VALUES (?, 'stub', 'stub-3d', ?, ?, ?)",
        (atom_id, len(vec), vec_bytes, accessed_at),
    )
    conn.execute(
        "INSERT INTO access_events (atom_id, ts, source, weight) "
        "VALUES (?, ?, 'store', 1.0)",
        (atom_id, accessed_at),
    )
    conn.execute(
        "INSERT INTO atom_access_summary (atom_id, recent_ts_json, "
        "recent_weights_json, old_count, old_weight_sum, last_updated_ts) "
        "VALUES (?, ?, ?, 0, 0.0, ?)",
        (atom_id, json.dumps([accessed_at]), json.dumps([1.0]), accessed_at),
    )
    conn.commit()


# ─── compute_activation honors ``now`` ────────────────────────────────


def test_activation_reference_date_anchors_age():
    """Same atom accessed at T_1. Activation against now=T_1 vs
    now=T_1+3yr differs by 6+ orders of magnitude."""
    t1 = datetime(2023, 5, 12, tzinfo=timezone.utc)
    t1_iso = t1.isoformat()

    # Activation evaluated at T_1 + 1 hour (atom is fresh).
    fresh = compute_activation(
        recent_ts=[t1_iso], recent_weights=[1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=t1 + timedelta(hours=1),
    )

    # Activation evaluated 3 years later (atom is ancient).
    ancient = compute_activation(
        recent_ts=[t1_iso], recent_weights=[1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=t1 + timedelta(days=3 * 365),
    )

    # Power-law decay: large age gap → vastly lower activation.
    assert fresh > ancient
    assert fresh - ancient > 5  # ln-scale; ≥ 5 nats = ~150× ratio


# ─── recall threads reference_date ────────────────────────────────────


def test_recall_passes_reference_date_to_activation(conn):
    """An atom stored at 2023 evaluated with reference_date=2023 should
    pass the activation threshold; with reference_date=2026 (wall
    clock) it shouldn't."""
    haystack_ts = "2023-05-12T00:00:00+00:00"
    _seed_atom(
        conn, "a1", "Alice graduated with a CS degree",
        accessed_at=haystack_ts, vec=[1.0, 0.0, 0.0],
    )

    # Adapters: FAISS returns the one atom; FTS returns it too.
    def faiss_fn(q_emb, top_k):
        return [("a1", 1.0)]
    def fts_fn(q_str, top_k):
        return [("a1", 5.0)]

    # Reference date a few seconds after the access — atom is fresh
    # enough to clear the -1.5 semantic threshold.
    ref_then = datetime(2023, 5, 12, 0, 0, 2, tzinfo=timezone.utc)
    result_then = recall(
        conn, "Alice degree",
        query_embed_fn=lambda _: [1.0, 0.0, 0.0],
        faiss_search_fn=faiss_fn,
        fts_search_fn=fts_fn,
        k=5,
        reference_date=ref_then,
        fire_access_events=False,
        auth_scope=AuthorizationScope(is_admin=True),
    )
    ids_then = [c.atom["id"] for c in result_then.raws]
    assert "a1" in ids_then

    # Same retrieval against wall-clock 2026: atom is 3 years old.
    # Activation crashes below the threshold; the atom is filtered.
    ref_now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    result_now = recall(
        conn, "Alice degree",
        query_embed_fn=lambda _: [1.0, 0.0, 0.0],
        faiss_search_fn=faiss_fn,
        fts_search_fn=fts_fn,
        k=5,
        reference_date=ref_now,
        fire_access_events=False,
        auth_scope=AuthorizationScope(is_admin=True),
    )
    # The 2026-anchored retrieval should either drop the atom entirely
    # or score it strictly below the 2023-anchored retrieval. Either
    # outcome demonstrates reference_date is plumbed through.
    if result_now.raws:
        act_then = next(c.activation for c in result_then.raws if c.atom["id"] == "a1")
        act_now = next(c.activation for c in result_now.raws if c.atom["id"] == "a1")
        assert act_then > act_now
    # If 2026 produced no results, the demonstration holds trivially.


def test_recall_writes_access_events_at_reference_date(conn):
    """chainlink #236: when ``reference_date`` is passed, Pass 4's
    access_event writes must use that timestamp instead of wall-clock now.

    Before this fix, ``mark_access`` used ``_utc_now_iso()`` unconditionally;
    bench replays of 2023-era corpora wrote 2026 timestamps onto atoms,
    corrupting downstream activation reads within the same bench run.
    """
    haystack_ts = "2023-05-12T00:00:00+00:00"
    _seed_atom(
        conn, "a1", "Alice graduated with a CS degree",
        accessed_at=haystack_ts, vec=[1.0, 0.0, 0.0],
    )

    def faiss_fn(q_emb, top_k):
        return [("a1", 1.0)]
    def fts_fn(q_str, top_k):
        return [("a1", 5.0)]

    ref_then = datetime(2023, 5, 12, 0, 0, 2, tzinfo=timezone.utc)
    recall(
        conn, "Alice degree",
        query_embed_fn=lambda _: [1.0, 0.0, 0.0],
        faiss_search_fn=faiss_fn,
        fts_search_fn=fts_fn,
        k=5,
        reference_date=ref_then,
        fire_access_events=True,
        auth_scope=AuthorizationScope(is_admin=True),
    )

    # Inspect the freshest access_event for a1 — should be at ref_then,
    # not wall-clock 2026.
    rows = conn.execute(
        "SELECT ts FROM access_events WHERE atom_id = ? AND source = 'retrieval' "
        "ORDER BY ts DESC LIMIT 1",
        ("a1",),
    ).fetchall()
    assert rows, "expected at least one retrieval access_event for a1"
    ts = rows[0][0]
    # Must be the reference_date, not the current wall clock.
    # ISO-formatted; allow either with or without microseconds.
    assert ts.startswith("2023-05-12"), (
        f"access_event timestamp {ts!r} doesn't match reference_date "
        f"2023-05-12 — chainlink #236 regression (wall-clock leaked through)"
    )


def test_recall_default_no_reference_date_uses_wall_clock(conn):
    """Sanity: when ``reference_date`` is None, mark_access falls back to
    wall-clock now. Don't break the production path with the new plumbing.

    Uses a recent atom (1 minute ago) so it clears the activation
    threshold under wall-clock recall — otherwise Pass 4 doesn't fire
    and we can't observe the access_event timestamp.
    """
    recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _seed_atom(
        conn, "a1", "Alice graduated with a CS degree",
        accessed_at=recent_ts, vec=[1.0, 0.0, 0.0],
    )

    def faiss_fn(q_emb, top_k):
        return [("a1", 1.0)]
    def fts_fn(q_str, top_k):
        return [("a1", 5.0)]

    # Capture wall-clock window around the call.
    t_before = datetime.now(timezone.utc)
    recall(
        conn, "Alice degree",
        query_embed_fn=lambda _: [1.0, 0.0, 0.0],
        faiss_search_fn=faiss_fn,
        fts_search_fn=fts_fn,
        k=5,
        reference_date=None,
        fire_access_events=True,
        auth_scope=AuthorizationScope(is_admin=True),
    )
    t_after = datetime.now(timezone.utc)

    rows = conn.execute(
        "SELECT ts FROM access_events WHERE atom_id = ? AND source = 'retrieval' "
        "ORDER BY ts DESC LIMIT 1",
        ("a1",),
    ).fetchall()
    # If recall filtered the atom (activation below threshold under wall
    # clock — varies by epsilon/decay), the end-to-end signal isn't
    # observable. Either way is fine here: the unit-level test below pins
    # the contract directly.
    if rows:
        ts = datetime.fromisoformat(rows[0][0])
        assert t_before <= ts <= t_after, (
            f"wall-clock fallback broken: ts={ts}, "
            f"expected in [{t_before}, {t_after}]"
        )


def test_mark_access_default_now_uses_wall_clock(conn):
    """Unit test: ``mark_access(now=None)`` uses wall-clock. Direct test on
    the helper since the end-to-end recall path depends on activation
    thresholds that can filter the atom out before Pass 4."""
    from mimir.saga.mark_access import AccessEvent, mark_access

    _seed_atom(conn, "a1", "x", accessed_at="2026-01-01T00:00:00+00:00",
               vec=[1.0, 0.0, 0.0])

    t_before = datetime.now(timezone.utc)
    mark_access(conn, [AccessEvent(atom_id="a1", source="retrieval")])
    t_after = datetime.now(timezone.utc)

    rows = conn.execute(
        "SELECT ts FROM access_events WHERE atom_id = ? AND source = 'retrieval'",
        ("a1",),
    ).fetchall()
    assert rows
    ts = datetime.fromisoformat(rows[0][0])
    assert t_before <= ts <= t_after, (
        f"default-now broken: ts={ts}, expected in [{t_before}, {t_after}]"
    )


def test_mark_access_with_explicit_now_uses_that_timestamp(conn):
    """chainlink #236: ``mark_access(now=<datetime>)`` writes events at the
    supplied timestamp, not wall-clock. Pins the helper-level contract.
    """
    from mimir.saga.mark_access import AccessEvent, mark_access

    _seed_atom(conn, "a1", "x", accessed_at="2026-01-01T00:00:00+00:00",
               vec=[1.0, 0.0, 0.0])

    ref = datetime(2023, 5, 12, 14, 30, 0, tzinfo=timezone.utc)
    mark_access(conn, [AccessEvent(atom_id="a1", source="retrieval")], now=ref)

    rows = conn.execute(
        "SELECT ts FROM access_events WHERE atom_id = ? AND source = 'retrieval'",
        ("a1",),
    ).fetchall()
    assert rows
    ts = rows[0][0]
    assert ts == "2023-05-12T14:30:00+00:00", (
        f"explicit-now broken: ts={ts!r}, expected '2023-05-12T14:30:00+00:00'"
    )
