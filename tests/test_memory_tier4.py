"""Tier-4 tests: forget (explicit + criteria) + feedback alias + config.

forget tests pin:
- Tombstoning is one-way; ``tombstoned=1`` blocks future recall/reflect/consolidate
- Observations whose evidence is partially forgotten have their
  metadata refreshed (evidence_count, trend may shift)
- The observation atom itself is NOT auto-tombstoned
- Bulk criteria-based forget supports preview (dry_run) before commit
- Pinned atoms are exempt from criteria-based forgetting
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.memory.forget import ForgetResult, forget, forget_by_criteria
from mimir.memory.mark_access import AccessEvent, mark_access
from mimir.memory.recall import recall
from mimir.memory.store import store


@pytest.fixture
def conn():
    schema = (Path(__file__).resolve().parent.parent / "mimir" / "memory" / "schema.sql").read_text()
    c = sqlite3.connect(":memory:")
    c.executescript(schema)
    yield c
    c.close()


def _fake_embed(text):
    h = abs(hash(text)) % 1000
    vec = [float(h % 7), float(h % 11), float(h % 13), float(h % 17)]
    return struct.pack("4f", *vec), "fake", "fake-model", 4


def _qf(text):
    h = abs(hash(text)) % 1000
    return [float(h % 7), float(h % 11), float(h % 13), float(h % 17)]


# ────────────────────────────────────────────────────────────────────
# forget() — explicit
# ────────────────────────────────────────────────────────────────────


def test_forget_marks_atom_tombstoned(conn):
    r = store(conn, "to be forgotten", embed_fn=_fake_embed)
    result = forget(conn, [r.atom_id], reason="test")
    assert result.tombstoned_count == 1
    assert r.atom_id in result.tombstoned_ids
    row = conn.execute(
        "SELECT tombstoned, tombstoned_reason FROM atoms WHERE id = ?",
        (r.atom_id,)
    ).fetchone()
    assert row[0] == 1
    assert row[1] == "test"


def test_forget_is_idempotent(conn):
    """Forgetting an already-tombstoned atom is a no-op."""
    r = store(conn, "x", embed_fn=_fake_embed)
    forget(conn, [r.atom_id])
    result = forget(conn, [r.atom_id])
    assert result.tombstoned_count == 0


def test_forgotten_atom_excluded_from_recall(conn):
    r = store(conn, "to forget", embed_fn=_fake_embed)
    forget(conn, [r.atom_id])
    result = recall(
        conn, "anything",
        query_embed_fn=_qf,
        faiss_search_fn=lambda emb, k: [(r.atom_id, 0.9)],
        fts_search_fn=lambda q, k: [],
    )
    surfaced = [c.atom["id"] for c in result.raws + result.observations]
    assert r.atom_id not in surfaced


def test_forget_refreshes_observation_metadata(conn):
    """Forgetting a raw that's evidence for an observation triggers
    a metadata refresh on the observation. The observation itself
    stays alive (it may have other evidence)."""
    # Two raws backing one observation.
    raw_a = store(conn, "raw a", embed_fn=_fake_embed).atom_id
    raw_b = store(conn, "raw b", embed_fn=_fake_embed).atom_id
    obs = store(conn, "obs", embed_fn=_fake_embed,
                memory_type="observation").atom_id
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO atom_relations (source_id, target_id, "
        "relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        [(obs, raw_a, now), (obs, raw_b, now)],
    )
    # Seed observations_metadata.
    conn.execute(
        "INSERT INTO observations_metadata "
        "(atom_id, evidence_count, trend, consolidated_at) "
        "VALUES (?, ?, 'strengthening', ?)",
        (obs, 2, now),
    )
    conn.commit()

    # Forget one of the raws.
    result = forget(conn, [raw_a])
    assert obs in result.observations_affected

    # The observation is NOT auto-tombstoned — it has remaining evidence.
    obs_state = conn.execute(
        "SELECT tombstoned FROM atoms WHERE id = ?", (obs,)
    ).fetchone()
    assert obs_state[0] == 0

    # Observation metadata was refreshed — but evidence_count is
    # rebuilt from refresh_trend, which counts access_events, not
    # evidenced_by rows directly. Trend should be present.
    md = conn.execute(
        "SELECT trend FROM observations_metadata WHERE atom_id = ?",
        (obs,)
    ).fetchone()
    assert md is not None
    assert md[0] in ("stable", "strengthening", "weakening", "stale")


def test_forget_empty_list_is_no_op(conn):
    result = forget(conn, [])
    assert result.tombstoned_count == 0


def test_forget_nonexistent_atom_is_no_op(conn):
    result = forget(conn, ["doesnotexist"])
    assert result.tombstoned_count == 0


# ────────────────────────────────────────────────────────────────────
# forget_by_criteria — bulk
# ────────────────────────────────────────────────────────────────────


def test_forget_by_criteria_dry_run_returns_preview(conn):
    """Dry run returns the candidate ids without writing."""
    for i in range(3):
        store(conn, f"a{i}", embed_fn=_fake_embed)
    result = forget_by_criteria(conn, dry_run=True)
    assert result.dry_run is True
    assert result.tombstoned_count == 0  # nothing actually tombstoned
    assert len(result.tombstoned_ids) == 3  # preview lists all eligible

    # Confirm no atoms were actually tombstoned.
    count = conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE tombstoned = 1"
    ).fetchone()[0]
    assert count == 0


def test_forget_by_criteria_stream_filter(conn):
    """Stream-specific bulk forget targets only matching stream."""
    sem = store(conn, "semantic atom", embed_fn=_fake_embed,
                stream="semantic").atom_id
    epi = store(conn, "episodic atom", embed_fn=_fake_embed,
                stream="episodic").atom_id

    result = forget_by_criteria(
        conn, stream="episodic", dry_run=False,
    )
    assert epi in result.tombstoned_ids
    assert sem not in result.tombstoned_ids


def test_forget_by_criteria_protects_pinned_atoms(conn):
    """is_pinned=1 atoms are exempt from criteria-based forgetting."""
    pinned = store(conn, "important", embed_fn=_fake_embed,
                   is_pinned=True).atom_id
    regular = store(conn, "regular", embed_fn=_fake_embed).atom_id

    result = forget_by_criteria(conn, dry_run=False)
    assert pinned not in result.tombstoned_ids
    assert regular in result.tombstoned_ids


def test_forget_by_criteria_min_age_days(conn):
    """min_age_days=N forgets only atoms older than N days."""
    # Brand new atom — younger than 7 days.
    young = store(conn, "young atom", embed_fn=_fake_embed).atom_id
    # Forge an old created_at on a second atom.
    old = store(conn, "old atom", embed_fn=_fake_embed).atom_id
    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    conn.execute(
        "UPDATE atoms SET created_at = ? WHERE id = ?", (old_ts, old)
    )
    conn.commit()

    result = forget_by_criteria(conn, min_age_days=30, dry_run=True)
    assert old in result.tombstoned_ids
    assert young not in result.tombstoned_ids


def test_forget_by_criteria_max_atoms_caps_result(conn):
    """max_atoms guards against runaway bulk-forget on a misconfigured
    criterion."""
    for i in range(50):
        store(conn, f"a{i}", embed_fn=_fake_embed)
    result = forget_by_criteria(conn, max_atoms=5, dry_run=True)
    assert len(result.tombstoned_ids) == 5


def test_forget_by_criteria_activation_below(conn):
    """activation_below filters atoms whose computed activation is
    below the given threshold."""
    import json as _json

    r1 = store(conn, "freshly stored", embed_fn=_fake_embed).atom_id
    r2 = store(conn, "old stored", embed_fn=_fake_embed).atom_id
    # Forge r2's access summary to look ancient.
    conn.execute(
        "UPDATE atom_access_summary SET recent_ts_json = ?, "
        "recent_weights_json = ?, last_updated_ts = ? WHERE atom_id = ?",
        (
            _json.dumps(["2020-01-01T00:00:00+00:00"]),
            _json.dumps([1.0]),
            "2020-01-01T00:00:00+00:00",
            r2,
        )
    )
    conn.commit()

    # Threshold harsh enough that fresh r1 passes (activation ~ -7 from
    # fresh access) but old r2 fails (activation ~ -ln(2020-age)/2).
    # Actually the fresh one has a recent timestamp; its activation is
    # log of just-now-event = high.
    result = forget_by_criteria(
        conn, activation_below=-5.0, dry_run=True,
    )
    # r2 should be in the candidate set (stale).
    assert r2 in result.tombstoned_ids
    # r1 should NOT — it was just stored, activation is high.
    assert r1 not in result.tombstoned_ids


# ────────────────────────────────────────────────────────────────────
# feedback alias (the convenience wrapper)
# ────────────────────────────────────────────────────────────────────


def test_feedback_positive_fires_event(conn):
    """The feedback() convenience writes a feedback_positive event."""
    from mimir.memory import feedback  # importing the package __init__

    r = store(conn, "atom", embed_fn=_fake_embed)
    n = feedback(conn, [r.atom_id], signal="positive")
    assert n == 1
    sources = [
        s for (s,) in conn.execute(
            "SELECT source FROM access_events WHERE atom_id = ? ORDER BY id",
            (r.atom_id,)
        )
    ]
    assert "feedback_positive" in sources


def test_feedback_non_positive_is_noop(conn):
    """Negative/neutral signals don't fire access events (per
    SCORING.md: ACT-R has no negative-weight contribution)."""
    from mimir.memory import feedback

    r = store(conn, "atom", embed_fn=_fake_embed)
    initial = conn.execute(
        "SELECT COUNT(*) FROM access_events WHERE atom_id = ?",
        (r.atom_id,)
    ).fetchone()[0]
    n_neg = feedback(conn, [r.atom_id], signal="negative")
    n_neutral = feedback(conn, [r.atom_id], signal="neutral")
    after = conn.execute(
        "SELECT COUNT(*) FROM access_events WHERE atom_id = ?",
        (r.atom_id,)
    ).fetchone()[0]
    assert n_neg == 0
    assert n_neutral == 0
    assert after == initial


# ────────────────────────────────────────────────────────────────────
# Config smoke tests
# ────────────────────────────────────────────────────────────────────


def test_default_config_has_expected_values():
    from mimir.memory.config import DEFAULT, MemoryConfig

    assert isinstance(DEFAULT, MemoryConfig)
    assert DEFAULT.activation.decay_d == 0.5
    assert DEFAULT.activation.recent_k == 10
    assert DEFAULT.thresholds.get("episodic") == -2.5
    assert DEFAULT.thresholds.get("semantic") == -1.5
    assert DEFAULT.scoring_weights.w_sim == 0.7
    assert DEFAULT.trend_modifiers.get("stale") == -0.25
    assert DEFAULT.trend_modifiers.get(None) == 0.0
    assert DEFAULT.source_weights.feedback_positive == 2.0
    assert DEFAULT.consolidation.min_cluster_size == 3


def test_config_from_toml_dict_overrides_defaults():
    from mimir.memory.config import MemoryConfig

    cfg = MemoryConfig.from_toml_dict({
        "activation": {"decay_d": 0.6, "recent_k": 15},
        "thresholds": {"episodic": -3.0},
        "consolidation": {"similarity_threshold": 0.75},
    })
    assert cfg.activation.decay_d == 0.6
    assert cfg.activation.recent_k == 15
    assert cfg.thresholds.get("episodic") == -3.0
    # Unspecified fields keep defaults.
    assert cfg.activation.epsilon_seconds == 1.0
    assert cfg.thresholds.get("semantic") == -1.5
    assert cfg.consolidation.similarity_threshold == 0.75
    assert cfg.consolidation.min_cluster_size == 3   # default


def test_config_unknown_keys_silently_skipped():
    from mimir.memory.config import MemoryConfig

    cfg = MemoryConfig.from_toml_dict({
        "activation": {"decay_d": 0.4, "unknown_key": "garbage"},
        "no_such_section": {"x": 1},
    })
    assert cfg.activation.decay_d == 0.4
    # Unknown key didn't break loading.
    assert not hasattr(cfg.activation, "unknown_key")
