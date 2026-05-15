"""Regression tests for PR 160 review fixes.

Pins the behaviors that addressed reviewer findings #4, #5, #7, #9, #10:

- #4: most_retrieved_atoms honors channel_id / contributed_only / trend
- #5: end_session threads channel_id through and persists it
- #7: outcome("negative") writes a feedback_negative event
- #9: schema_version column removed from atoms (the table remains)
- #10: provisional column removed from atoms
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from mimir.memory.client import MemoryClient


# Stub provider so we don't need real voyage credentials in unit tests.
def _patch_provider(monkeypatch):
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            h = abs(hash(text)) % 1000
            return [float(h % 7), float(h % 11), float(h % 13), float(h % 17)]
        def dimensions(self):
            return 4
    monkeypatch.setattr("mimir.memory.embeddings.get_provider", lambda: _StubProvider())
    def fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg
    monkeypatch.setattr("mimir.memory._config_io.get_config", fake_get_config)


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "mimir.memory.db"
    return MemoryClient(db_path=db, embedding_dim=4)


# ─── #10 + #9: schema columns dropped ────────────────────────────────


def test_provisional_column_removed(client):
    """Schema should not declare ``atoms.provisional`` anymore — it was
    never read or written and reviewer flagged it as dead schema."""
    conn = client._ensure_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(atoms)").fetchall()}
    assert "provisional" not in cols


def test_atoms_schema_version_column_removed(client):
    """Per-row ``atoms.schema_version`` was duplicate of the
    table-level schema_version; reviewer flagged it. Dropped column,
    kept the schema_version table."""
    conn = client._ensure_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(atoms)").fetchall()}
    assert "schema_version" not in cols
    # The TABLE schema_version still exists (operator/migration history).
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "schema_version" in tables


# ─── #5: end_session threads channel_id ──────────────────────────────


@pytest.mark.asyncio
async def test_end_session_persists_channel_id(client, monkeypatch):
    _patch_provider(monkeypatch)
    result = await client.end_session(
        "s1", "summary", channel_id="C123",
    )
    assert result["channel"] == "C123"
    conn = client._ensure_conn()
    row = conn.execute(
        "SELECT channel_id FROM sessions WHERE id = 's1'"
    ).fetchone()
    assert row is not None
    assert row[0] == "C123"


@pytest.mark.asyncio
async def test_end_session_without_channel_id_still_works(client, monkeypatch):
    """Caller without a channel_id (e.g. operator-driven manual close)
    should still get a boundary written; sessions.channel_id is NULL."""
    _patch_provider(monkeypatch)
    result = await client.end_session("s2", "summary")
    assert result["channel"] is None
    assert result["atom_id"] is not None


@pytest.mark.asyncio
async def test_recent_session_boundaries_scopes_to_channel(client, monkeypatch):
    """Cross-channel boundaries shouldn't leak into per-channel queries
    once channel_id is properly threaded through."""
    _patch_provider(monkeypatch)
    await client.end_session("sA", "channel A session", channel_id="CHAN_A")
    await client.end_session("sB", "channel B session", channel_id="CHAN_B")
    boundaries_a = await client.recent_session_boundaries(
        channel_id="CHAN_A", count=10,
    )
    sids = [b["session_id"] for b in boundaries_a]
    assert "sA" in sids
    assert "sB" not in sids


# ─── #4: most_retrieved_atoms filters ────────────────────────────────


@pytest.mark.asyncio
async def test_most_retrieved_filters_contributed_only(client, monkeypatch):
    """contributed_only=True excludes plain retrieval events; counts
    only feedback_positive (the credit-pass endorsements)."""
    _patch_provider(monkeypatch)
    r1 = await client.store("alpha")
    r2 = await client.store("beta")
    # r1 gets one feedback_positive (credit pass)
    await client.feedback([r1["atom_id"]], "response", feedback="positive")
    # r2 gets two plain retrievals via query
    await client.query("beta", top_k=5)
    await client.query("beta", top_k=5)
    # Without filter: r2 has more events than r1 (store + 2 retrieval = 3 vs 2)
    bare = await client.most_retrieved_atoms(days=7, count=5)
    bare_ids = [a["id"] for a in bare]
    assert r2["atom_id"] in bare_ids
    # With contributed_only: r2 has zero feedback events; r1 has one.
    contributed = await client.most_retrieved_atoms(
        days=7, count=5, contributed_only=True,
    )
    contributed_ids = [a["id"] for a in contributed]
    assert r1["atom_id"] in contributed_ids
    assert r2["atom_id"] not in contributed_ids


@pytest.mark.asyncio
async def test_most_retrieved_filters_by_channel_id(client, monkeypatch):
    """Atoms accessed only in channel A shouldn't surface under
    channel B's most-retrieved query."""
    _patch_provider(monkeypatch)
    # Bootstrap: close a session in each channel so the sessions rows exist.
    await client.end_session("sA", "A session", channel_id="CHAN_A")
    await client.end_session("sB", "B session", channel_id="CHAN_B")
    # Store an atom and write retrieval events tied to each session.
    r = await client.store("shared atom")
    aid = r["atom_id"]
    conn = client._ensure_conn()
    conn.execute(
        "INSERT INTO access_events (atom_id, ts, source, weight, session_id) "
        "VALUES (?, '2026-05-13T00:00:00Z', 'retrieval', 1.0, 'sA')",
        (aid,),
    )
    conn.execute(
        "INSERT INTO access_events (atom_id, ts, source, weight, session_id) "
        "VALUES (?, '2026-05-13T00:00:01Z', 'retrieval', 1.0, 'sA')",
        (aid,),
    )
    conn.commit()
    # Channel A: the atom should show.
    a = await client.most_retrieved_atoms(
        days=7, count=5, channel_id="CHAN_A",
    )
    assert aid in [x["id"] for x in a]
    # Channel B: no retrieval events tied to its session; the atom should NOT
    # show.
    b = await client.most_retrieved_atoms(
        days=7, count=5, channel_id="CHAN_B",
    )
    assert aid not in [x["id"] for x in b]


# ─── #7: outcome("negative") writes a feedback_negative event ────────


@pytest.mark.asyncio
async def test_outcome_negative_writes_event_with_subtractive_weight(client, monkeypatch):
    """outcome("negative") fires a feedback_negative access event with
    weight −1.0. Updated 2026-05-13: previous design used weight 0.0
    (flag-only), but that left the retrieval event's +1.0 activation
    boost untouched when the access turned out to be unhelpful. The
    subtractive weight cancels exactly one access-equivalent, so an
    atom retrieved once and then marked negative ends up activation-
    neutral relative to never having been touched."""
    _patch_provider(monkeypatch)
    r = await client.store("test atom")
    result = await client.outcome([r["atom_id"]], "negative")
    assert result["marked"] == 1
    assert result["signal"] == "negative"
    conn = client._ensure_conn()
    row = conn.execute(
        "SELECT source, weight FROM access_events "
        "WHERE atom_id = ? AND source = 'feedback_negative'",
        (r["atom_id"],),
    ).fetchone()
    assert row is not None
    assert row[1] == -1.0


@pytest.mark.asyncio
async def test_outcome_negative_cancels_retrieval_boost(client, monkeypatch):
    """End-to-end: atom whose only events are store + retrieval (Σ=2.0)
    → mark negative → Σ=1.0 (store alone). Activation should drop but
    the atom remains retrievable (B = ln(1.0) = 0 > semantic threshold
    of -1.5). Atom whose only event is store (Σ=1.0) → mark negative
    → Σ=0 → B=-inf → filtered. Pins the subtractive-weight semantic."""
    _patch_provider(monkeypatch)
    from mimir.memory.activation import compute_activation
    import json as _json

    r = await client.store("test atom")
    aid = r["atom_id"]
    # Atom has just one 'store' event so far. Mark negative.
    await client.outcome([aid], "negative")
    conn = client._ensure_conn()
    # Read the current summary; activation should be -inf (sum is 0).
    row = conn.execute(
        "SELECT recent_ts_json, recent_weights_json, old_count, "
        "old_weight_sum, old_oldest_ts FROM atom_access_summary "
        "WHERE atom_id = ?",
        (aid,),
    ).fetchone()
    assert row is not None
    activation = compute_activation(
        recent_ts=_json.loads(row[0]),
        recent_weights=_json.loads(row[1]),
        old_count=row[2], old_weight_sum=row[3],
        old_oldest_ts=row[4],
    )
    assert activation == float("-inf")


@pytest.mark.asyncio
async def test_outcome_positive_still_writes_feedback_positive(
    client, monkeypatch,
):
    _patch_provider(monkeypatch)
    r = await client.store("test atom")
    await client.outcome([r["atom_id"]], "positive")
    conn = client._ensure_conn()
    row = conn.execute(
        "SELECT weight FROM access_events "
        "WHERE atom_id = ? AND source = 'feedback_positive'",
        (r["atom_id"],),
    ).fetchone()
    assert row is not None
    assert row[0] == 2.0


@pytest.mark.asyncio
async def test_outcome_other_signals_are_noop(client, monkeypatch):
    _patch_provider(monkeypatch)
    r = await client.store("test atom")
    result = await client.outcome([r["atom_id"]], "neutral")
    assert result["marked"] == 0
    conn = client._ensure_conn()
    rows = conn.execute(
        "SELECT source FROM access_events WHERE atom_id = ? "
        "AND source IN ('feedback_positive', 'feedback_negative')",
        (r["atom_id"],),
    ).fetchall()
    assert rows == []
