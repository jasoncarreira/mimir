"""Tests for the dedup pass — pass 1 of two-pass consolidation.

Covers:
- pick_canonical chooses the higher-activation atom in a cluster
- pick_canonical tiebreaks via pinned, then evidence_count, then
  older created_at
- merge collapses access_events into the canonical (activation sum
  is preserved)
- merge unions topics, appends dedup_merged_ids to metadata
- merge tombstones duplicates with reason='merged'
- merge adds a consolidated_into edge so retrieval can lift the
  canonical via evidence_boost
- atom_relations involving a duplicate are redirected (with dedup)
- dedup_pass is idempotent (second run on same DB is a no-op)
"""

from __future__ import annotations

import json
import sqlite3
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.saga.activation import compute_activation
from mimir.saga.cluster import make_default_cluster_fn
from mimir.saga.dedup import (
    DEFAULT_DEDUP_THRESHOLD,
    DedupResult,
    dedup_pass,
    pick_canonical,
)
from mimir.saga.mark_access import AccessEvent, mark_access
from mimir.saga.store import store


@pytest.fixture
def conn():
    schema = (
        Path(__file__).resolve().parent.parent
        / "mimir" / "saga" / "schema.sql"
    ).read_text()
    c = sqlite3.connect(":memory:")
    c.executescript(schema)
    yield c
    c.close()


def _emb(vec):
    return struct.pack(f"{len(vec)}f", *vec), "test", "test-model", len(vec)


def _embed_fn_factory(vectors_by_content):
    def fn(text):
        v = vectors_by_content.get(text, [0.0] * 4)
        return _emb(v)
    return fn


# ────────────────────────────────────────────────────────────────────
# pick_canonical
# ────────────────────────────────────────────────────────────────────


def test_pick_canonical_picks_higher_activation(conn):
    """The atom with more recent retrievals beats the one with none."""
    embed_fn = _embed_fn_factory({
        "popular": [1.0, 0.0, 0.0, 0.0],
        "rare":    [1.0, 0.0, 0.0, 0.0],
    })
    pop = store(conn, "popular", embed_fn=embed_fn).atom_id
    rare = store(conn, "rare", embed_fn=embed_fn).atom_id

    # Fire 3 retrievals on `pop` to lift its activation.
    for _ in range(3):
        mark_access(conn, [AccessEvent(atom_id=pop, source="retrieval")])
        time.sleep(0.01)

    atoms = [
        dict(zip(("id", "content", "stream", "memory_type", "source_type",
                  "created_at", "topics", "metadata", "is_pinned",
                  "agent_id", "session_id"), row))
        for row in conn.execute(
            "SELECT id, content, stream, memory_type, source_type, "
            "created_at, topics, metadata, is_pinned, agent_id, session_id "
            "FROM atoms WHERE id IN (?, ?)", (pop, rare),
        ).fetchall()
    ]
    canon = pick_canonical(conn, atoms)
    assert canon["id"] == pop


def test_pick_canonical_prefers_pinned_on_equal_activation(conn):
    embed_fn = _embed_fn_factory({"a": [1.0, 0.0, 0.0, 0.0],
                                   "b": [1.0, 0.0, 0.0, 0.0]})
    a = store(conn, "a", embed_fn=embed_fn).atom_id
    b = store(conn, "b", embed_fn=embed_fn).atom_id
    # Same activation for both (just the store-event). Pin `b`.
    conn.execute("UPDATE atoms SET is_pinned = 1 WHERE id = ?", (b,))
    conn.commit()

    atoms = [
        dict(zip(("id", "content", "stream", "memory_type", "source_type",
                  "created_at", "topics", "metadata", "is_pinned",
                  "agent_id", "session_id"), row))
        for row in conn.execute(
            "SELECT id, content, stream, memory_type, source_type, "
            "created_at, topics, metadata, is_pinned, agent_id, session_id "
            "FROM atoms WHERE id IN (?, ?)", (a, b),
        ).fetchall()
    ]
    assert pick_canonical(conn, atoms)["id"] == b


# ────────────────────────────────────────────────────────────────────
# dedup_pass — basic merge mechanics
# ────────────────────────────────────────────────────────────────────


def test_dedup_pass_merges_near_duplicates(conn):
    """Two atoms with cosine ≥ threshold cluster, one is canonical,
    the other is tombstoned with reason='merged'."""
    # Identical vectors → cosine = 1.0 ≥ any threshold.
    embed_fn = _embed_fn_factory({
        "Hailey posted about Tim's blog today.":         [1.0, 0.0, 0.0, 0.0],
        "--text Hailey posted about Tim's blog today.": [1.0, 0.0, 0.0, 0.0],
    })
    a = store(
        conn, "Hailey posted about Tim's blog today.",
        embed_fn=embed_fn, topics=["hailey", "tim"],
    ).atom_id
    b = store(
        conn, "--text Hailey posted about Tim's blog today.",
        embed_fn=embed_fn, topics=["hailey", "blog"],
    ).atom_id
    # Lift `a`'s activation so it wins canonical pick.
    for _ in range(3):
        mark_access(conn, [AccessEvent(atom_id=a, source="retrieval")])

    conn.commit()
    cluster_fn = make_default_cluster_fn(conn, threshold=0.92)
    result = dedup_pass(conn, cluster_fn=cluster_fn, min_cluster_size=2)

    assert result.candidates_scanned == 2
    assert a in result.canonicals_kept
    assert b in result.duplicates_tombstoned

    # b is tombstoned with reason='merged'.
    row = conn.execute(
        "SELECT tombstoned, tombstoned_reason FROM atoms WHERE id = ?", (b,),
    ).fetchone()
    assert row == (1, "merged")

    # a is still active.
    row = conn.execute(
        "SELECT tombstoned FROM atoms WHERE id = ?", (a,),
    ).fetchone()
    assert row[0] == 0

    # a's topics now include b's "blog".
    topics = json.loads(conn.execute(
        "SELECT topics FROM atoms WHERE id = ?", (a,),
    ).fetchone()[0])
    assert set(topics) >= {"hailey", "tim", "blog"}

    # a's metadata.dedup_merged_ids contains b.
    meta = json.loads(conn.execute(
        "SELECT metadata FROM atoms WHERE id = ?", (a,),
    ).fetchone()[0])
    assert b in meta.get("dedup_merged_ids", [])

    # consolidated_into edge b → a exists.
    rel = conn.execute(
        "SELECT COUNT(*) FROM atom_relations "
        "WHERE source_id = ? AND target_id = ? "
        "AND relation_type = 'consolidated_into'",
        (b, a),
    ).fetchone()[0]
    assert rel == 1


def test_dedup_pass_preserves_activation_sum(conn):
    """When merging, the canonical's activation rises by the duplicate's
    contribution — sum of (now - t_j)^(-d) is linear under Petrov OL."""
    embed_fn = _embed_fn_factory({
        "alpha": [1.0, 0.0, 0.0, 0.0],
        "beta":  [1.0, 0.0, 0.0, 0.0],
    })
    a = store(conn, "alpha", embed_fn=embed_fn).atom_id
    b = store(conn, "beta", embed_fn=embed_fn).atom_id
    # `a` gets 2 retrievals, `b` gets 1 — so total events = 3+1+1+1=6
    # (2 stores + 3 retrievals total).
    for _ in range(2):
        mark_access(conn, [AccessEvent(atom_id=a, source="retrieval")])
    mark_access(conn, [AccessEvent(atom_id=b, source="retrieval")])
    pre_event_count = conn.execute(
        "SELECT COUNT(*) FROM access_events WHERE atom_id IN (?, ?)", (a, b),
    ).fetchone()[0]

    conn.commit()
    cluster_fn = make_default_cluster_fn(conn, threshold=0.92)
    result = dedup_pass(conn, cluster_fn=cluster_fn, min_cluster_size=2)

    # After merge: all 6 events should now belong to one of the two
    # atoms (whichever is canonical). The duplicate's events were
    # redirected.
    canonical = result.canonicals_kept[0]
    post_count = conn.execute(
        "SELECT COUNT(*) FROM access_events WHERE atom_id = ?", (canonical,),
    ).fetchone()[0]
    assert post_count == pre_event_count

    # Duplicate atom now has zero events.
    dup = result.duplicates_tombstoned[0]
    dup_count = conn.execute(
        "SELECT COUNT(*) FROM access_events WHERE atom_id = ?", (dup,),
    ).fetchone()[0]
    assert dup_count == 0


def test_dedup_pass_is_idempotent(conn):
    """Running dedup_pass twice on the same data is a no-op the second
    time — duplicates are already tombstoned and excluded from
    candidates."""
    embed_fn = _embed_fn_factory({
        "x": [1.0, 0.0, 0.0, 0.0],
        "y": [1.0, 0.0, 0.0, 0.0],
    })
    store(conn, "x", embed_fn=embed_fn)
    store(conn, "y", embed_fn=embed_fn)

    conn.commit()
    cluster_fn = make_default_cluster_fn(conn, threshold=0.92)
    first = dedup_pass(conn, cluster_fn=cluster_fn, min_cluster_size=2)
    assert len(first.duplicates_tombstoned) == 1

    second = dedup_pass(conn, cluster_fn=cluster_fn, min_cluster_size=2)
    assert second.duplicates_tombstoned == []
    assert second.candidates_scanned == 1   # only the canonical remains


def test_dedup_pass_respects_min_cluster_size(conn):
    """A singleton cluster never merges."""
    embed_fn = _embed_fn_factory({
        "lonely": [1.0, 0.0, 0.0, 0.0],
        "other":  [0.0, 1.0, 0.0, 0.0],
    })
    store(conn, "lonely", embed_fn=embed_fn)
    store(conn, "other", embed_fn=embed_fn)

    conn.commit()
    cluster_fn = make_default_cluster_fn(conn, threshold=0.92)
    result = dedup_pass(conn, cluster_fn=cluster_fn, min_cluster_size=2)
    assert result.duplicates_tombstoned == []


def test_dedup_pass_dry_run_makes_no_writes(conn):
    """dry_run=True reports the merge plan but doesn't tombstone."""
    embed_fn = _embed_fn_factory({
        "a": [1.0, 0.0, 0.0, 0.0],
        "b": [1.0, 0.0, 0.0, 0.0],
    })
    store(conn, "a", embed_fn=embed_fn)
    b_id = store(conn, "b", embed_fn=embed_fn).atom_id

    conn.commit()
    cluster_fn = make_default_cluster_fn(conn, threshold=0.92)
    result = dedup_pass(
        conn, cluster_fn=cluster_fn, min_cluster_size=2, dry_run=True,
    )
    assert len(result.duplicates_tombstoned) == 1

    # No tombstoning happened.
    row = conn.execute(
        "SELECT tombstoned FROM atoms WHERE id = ?", (b_id,),
    ).fetchone()
    assert row[0] == 0


def test_dedup_pass_does_not_merge_session_boundaries(conn):
    """source_type='session_boundary' atoms are structural and must
    never be deduped — they'd corrupt per-session evidence trails."""
    embed_fn = _embed_fn_factory({
        "Session Boundary X": [1.0, 0.0, 0.0, 0.0],
        "Session Boundary Y": [1.0, 0.0, 0.0, 0.0],
    })
    store(conn, "Session Boundary X", embed_fn=embed_fn,
          source_type="session_boundary")
    store(conn, "Session Boundary Y", embed_fn=embed_fn,
          source_type="session_boundary")

    conn.commit()
    cluster_fn = make_default_cluster_fn(conn, threshold=0.92)
    result = dedup_pass(conn, cluster_fn=cluster_fn, min_cluster_size=2)
    assert result.duplicates_tombstoned == []


def test_dedup_pass_redirects_relations(conn):
    """If atom B has a relation, and B gets merged into A, that
    relation should now point at A."""
    embed_fn = _embed_fn_factory({
        "canon": [1.0, 0.0, 0.0, 0.0],
        "dup":   [1.0, 0.0, 0.0, 0.0],
        "other": [0.0, 1.0, 0.0, 0.0],
    })
    a = store(conn, "canon", embed_fn=embed_fn).atom_id
    b = store(conn, "dup", embed_fn=embed_fn).atom_id
    other = store(conn, "other", embed_fn=embed_fn).atom_id

    # Pre-existing relation: other --(evidenced_by)-> b
    conn.execute(
        "INSERT INTO atom_relations "
        "(source_id, target_id, relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        (other, b, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

    # Lift `a`'s activation so it wins.
    for _ in range(3):
        mark_access(conn, [AccessEvent(atom_id=a, source="retrieval")])
    conn.commit()

    cluster_fn = make_default_cluster_fn(conn, threshold=0.92)
    dedup_pass(conn, cluster_fn=cluster_fn, min_cluster_size=2)

    # After merge: the evidenced_by edge points at `a`, not `b`.
    rows = conn.execute(
        "SELECT source_id, target_id, relation_type FROM atom_relations "
        "WHERE relation_type = 'evidenced_by'",
    ).fetchall()
    redirected = [(s, t) for s, t, _ in rows if s == other]
    assert (other, a) in redirected
    assert (other, b) not in redirected
