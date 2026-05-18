"""Regression tests for PR #208 — saga correctness batch.

Covers two related fixes:

1. FAISS index tombstone sync — ``SagaStore.forget`` must call
   ``_index.remove(atom_id)`` for each tombstoned atom. Pre-fix the
   index accumulated orphaned positions until
   ``rebuild_if_needed`` (>10% removed) kicked in.

2. Activation decay edge cases — d=1 special branch, negative-weight
   recent events subtracting from total, zero-weight events
   contributing zero. The previous incarnation of this code (MSAM
   decay) regressed on similar edge cases.

The schema-migration empty-applied edge case is left as a
documented limitation in ``_apply_pending_migrations`` — fixing it
correctly requires PRAGMA-driven schema introspection, more work
than the current single-operator deployment posture warrants.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.saga.activation import compute_activation


# ─── FAISS index tombstone sync ──────────────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.mark.asyncio
async def test_saga_forget_removes_tombstoned_atoms_from_faiss_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """After ``SagaStore.forget(dry_run=False)`` tombstones atoms,
    the FAISS index must have those positions removed so over-fetches
    don't return them and ``top_k`` stays accurate.

    Pre-fix: ``forget_by_criteria`` tombstones the SQL row but
    nothing touches the index until ``rebuild_if_needed`` (10%
    removed). Index fragmentation accumulated silently.
    """
    from mimir.saga.client import SagaStore

    # Stub embedding provider so we can run without Voyage credentials.
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            h = abs(hash(text)) % 1000
            return [float((h + i) % 17) / 17.0 for i in range(4)]

        def dimensions(self):
            return 4

    monkeypatch.setattr(
        "mimir.saga.embeddings.get_provider",
        lambda: _StubProvider(),
    )
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: lambda s, k, d=None: {
            ("embedding", "max_input_chars"): 2000,
            ("embedding", "provider"): "stub",
            ("embedding", "model"): "stub-4d",
        }.get((s, k), d),
    )

    store = SagaStore(db_path=tmp_path / "test.saga.db", embedding_dim=4)

    # Store 3 atoms with old timestamps so grace_days filter catches
    # them all. We patch the created_at directly.
    aged = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    atom_ids = []
    for i in range(3):
        result = await store.store(
            content=f"old fact {i}",
            stream="semantic",
        )
        atom_ids.append(result["atom_id"])
    conn = store._ensure_conn()
    conn.executemany(
        "UPDATE atoms SET created_at = ? WHERE id = ?",
        [(aged, aid) for aid in atom_ids],
    )
    conn.commit()

    # Force index build and capture pre-forget state.
    index = store._ensure_index(conn)
    assert index is not None, "test setup: index should build"
    pre_positions = len(index._id_to_pos)
    assert pre_positions == 3, f"expected 3 indexed atoms, got {pre_positions}"

    # Forget with grace_days=1 — all 3 atoms qualify (aged 365 days).
    result = await store.forget(grace_days=1, dry_run=False)
    assert result["tombstoned_count"] == 3, f"expected 3 tombstoned; got {result}"

    # ── The regression guard: index positions for tombstoned atoms
    #    must be removed (gone from id_to_pos OR in _removed set).
    for atom_id in atom_ids:
        if atom_id in index._id_to_pos:
            pos = index._id_to_pos[atom_id]
            assert pos in index._removed, (
                f"atom {atom_id} (pos {pos}) tombstoned in DB but FAISS "
                f"position still active — pre-fix regression"
            )


# ─── Activation decay edge cases ─────────────────────────────────────


def test_compute_activation_d_equals_1_uses_log_integral():
    """The d=1 special case (line 197-200 in activation.py): integral
    becomes ``ln(oldest_age / upper_age)`` instead of the power-form.
    Locks in that the special branch is exercised and doesn't NaN /
    divide-by-zero. Pre-fix coverage relied on default d=0.5 only;
    the d=1 guard was untested.
    """
    now = datetime.now(timezone.utc)
    recent_ts = [_iso(now - timedelta(hours=1))]
    recent_weights = [1.0]
    act = compute_activation(
        recent_ts=recent_ts,
        recent_weights=recent_weights,
        old_count=5,
        old_weight_sum=5.0,
        old_oldest_ts=_iso(now - timedelta(days=7)),
        now=now,
        decay=1.0,  # special case
    )
    # Activation must be finite (not -inf, not NaN). Sign is
    # determined by whether the integral pushes Σ above 0.0.
    assert math.isfinite(act), (
        f"d=1 special case produced non-finite activation {act}"
    )


def test_compute_activation_negative_recent_weight_subtracts():
    """A negative-weight recent event (e.g. ``feedback_negative`` at
    -1.0) must subtract from the total Σ, potentially driving
    activation toward -inf if it cancels positive contributions.

    Pre-existing tests covered negative weights in the DISPLACED
    aggregate (old_weight_sum=0 short-circuit case); this locks in
    the RECENT-window subtraction path too.
    """
    now = datetime.now(timezone.utc)
    one_hour_ago = _iso(now - timedelta(hours=1))

    # Pure positive: 1 retrieval at 1h ago → activation > -inf.
    act_pos = compute_activation(
        recent_ts=[one_hour_ago],
        recent_weights=[1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )
    assert math.isfinite(act_pos)

    # Pure negative (a feedback_negative immediately after store):
    # Σ goes negative → log undefined → returns -inf per the guard
    # at line 211.
    act_neg = compute_activation(
        recent_ts=[one_hour_ago],
        recent_weights=[-1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )
    assert act_neg == float("-inf"), (
        f"negative-weight recent event must drive total ≤ 0 "
        f"and return -inf; got {act_neg}"
    )

    # Cancellation: +1.0 and -1.0 at the same age sum to exactly 0
    # → log(0) → -inf via the guard.
    act_cancel = compute_activation(
        recent_ts=[one_hour_ago, one_hour_ago],
        recent_weights=[1.0, -1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )
    assert act_cancel == float("-inf")

    # Net positive: +2.0 and -1.0 at same age → net +1.0 → matches
    # pure positive (same time-decay applied to net weight).
    act_net = compute_activation(
        recent_ts=[one_hour_ago, one_hour_ago],
        recent_weights=[2.0, -1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )
    assert math.isclose(act_net, act_pos, rel_tol=1e-9)


def test_compute_activation_zero_weight_recent_event_contributes_zero():
    """A weight=0 recent event must contribute exactly zero to Σ —
    not amplify, not suppress. Locks in the multiplicative
    ``weight * age^(-d)`` form (line 173).
    """
    now = datetime.now(timezone.utc)
    one_hour_ago = _iso(now - timedelta(hours=1))

    # Baseline: one retrieval at 1h ago.
    act_baseline = compute_activation(
        recent_ts=[one_hour_ago],
        recent_weights=[1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )

    # Same baseline + one zero-weight event at a different time:
    # must produce identical activation.
    act_with_zero = compute_activation(
        recent_ts=[one_hour_ago, _iso(now - timedelta(minutes=5))],
        recent_weights=[1.0, 0.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )
    assert math.isclose(act_with_zero, act_baseline, rel_tol=1e-9)
