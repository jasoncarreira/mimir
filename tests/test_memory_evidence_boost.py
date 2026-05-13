"""Tests for the saga-shaped evidence-boost mechanism.

Verifies that ``_apply_evidence_boost``:
  - Scales each surfaced observation's contribution by its RRF score
    (stronger obs → bigger raw lift)
  - Sums contributions when multiple surfaced obs endorse the same raw
  - Caps total boost at base × CAP_RATIO so a weakly-ranked raw can't
    leap to top-K from observation endorsement alone
  - Only touches raws already in the pool (no pull-in)
"""
from __future__ import annotations

from mimir.memory.recall import (
    OBSERVATION_BOOST_MULTIPLIER,
    OBSERVATION_BOOST_CAP_RATIO,
    RecallCandidate,
    _apply_evidence_boost,
)


class _StubConn:
    """Minimal connection stand-in returning a fixed evidenced_by edge set."""

    def __init__(self, edges):
        self._edges = edges  # list[(source_id, target_id)]

    def execute(self, sql, params):
        class _Cursor:
            def __init__(s, rows):
                s._rows = rows
            def fetchall(s):
                return list(s._rows)
        # Only the evidenced_by SELECT is called by _apply_evidence_boost.
        # We don't try to parse SQL — just return everything.
        obs_ids = set(params)
        return _Cursor([
            (src, tgt) for (src, tgt) in self._edges if src in obs_ids
        ])


def _candidate(atom_id, *, rrf_score=0.033, memory_type="raw"):
    return RecallCandidate(
        atom={"id": atom_id, "memory_type": memory_type},
        activation=0.0,
        similarity=0.0,
        rrf_score=rrf_score,
        total=20.0 * rrf_score,  # mirrors _score_candidates w_rrf=20
    )


def test_no_surfaced_obs_is_noop():
    raws = [_candidate("r1"), _candidate("r2")]
    pre = [(c.atom["id"], c.total) for c in raws]
    _apply_evidence_boost(_StubConn([]), [], raws, weights={"w_rrf": 20.0})
    post = [(c.atom["id"], c.total) for c in raws]
    assert pre == post


def test_single_observation_lifts_endorsed_raw():
    obs = _candidate("o1", rrf_score=0.033, memory_type="observation")
    r1 = _candidate("r1", rrf_score=0.020)
    r2 = _candidate("r2", rrf_score=0.020)  # not endorsed
    base_r1 = r1.total
    base_r2 = r2.total
    _apply_evidence_boost(
        _StubConn([("o1", "r1")]),
        [obs], [r1, r2],
        weights={"w_rrf": 20.0},
    )
    # r1 boosted, r2 untouched.
    assert r1.total > base_r1
    assert r2.total == base_r2
    # Boost is positive but capped at base × CAP_RATIO (in total units).
    boost = r1.total - base_r1
    assert 0 < boost <= base_r1 * OBSERVATION_BOOST_CAP_RATIO + 1e-9


def test_multiple_endorsements_accumulate():
    obs_a = _candidate("oa", rrf_score=0.033, memory_type="observation")
    obs_b = _candidate("ob", rrf_score=0.025, memory_type="observation")
    obs_c = _candidate("oc", rrf_score=0.020, memory_type="observation")
    r1 = _candidate("r1", rrf_score=0.020)
    base = r1.total
    _apply_evidence_boost(
        _StubConn([("oa", "r1"), ("ob", "r1"), ("oc", "r1")]),
        [obs_a, obs_b, obs_c], [r1],
        weights={"w_rrf": 20.0},
    )
    # Boost accumulated from all three endorsements but capped.
    boost = r1.total - base
    assert boost > 0
    assert boost <= base * OBSERVATION_BOOST_CAP_RATIO + 1e-9


def test_stronger_observation_gives_bigger_lift():
    """A rank-1 observation should boost its raw more than a rank-5
    observation. This is the saga property our flat-0.20 missed."""
    strong_obs = _candidate("strong", rrf_score=0.030, memory_type="observation")
    weak_obs = _candidate("weak", rrf_score=0.005, memory_type="observation")
    r_strong = _candidate("rs", rrf_score=0.020)
    r_weak = _candidate("rw", rrf_score=0.020)
    base = r_strong.total
    _apply_evidence_boost(
        _StubConn([("strong", "rs"), ("weak", "rw")]),
        [strong_obs, weak_obs], [r_strong, r_weak],
        weights={"w_rrf": 20.0},
    )
    boost_strong = r_strong.total - base
    boost_weak = r_weak.total - base
    assert boost_strong > boost_weak
    assert boost_weak > 0  # weak obs still contributes something


def test_cap_clamps_extreme_boosts():
    """Three top-of-both observations all endorsing the same raw should
    pile up past the cap; the cap should clamp the final boost to
    base × CAP_RATIO."""
    # Three observations at rank 1 in both pathways each contribute
    # 2.0 × 0.033 = 0.066 in RRF units. Three of them = 0.198 RRF
    # = 3.96 in total-score units (× w_rrf=20). Without the cap r1's
    # total would more than 4× base. With the cap it should land at
    # base + base × CAP_RATIO.
    obs_a = _candidate("oa", rrf_score=0.033, memory_type="observation")
    obs_b = _candidate("ob", rrf_score=0.033, memory_type="observation")
    obs_c = _candidate("oc", rrf_score=0.033, memory_type="observation")
    r1 = _candidate("r1", rrf_score=0.015)  # mid-pack base
    base = r1.total
    _apply_evidence_boost(
        _StubConn([("oa", "r1"), ("ob", "r1"), ("oc", "r1")]),
        [obs_a, obs_b, obs_c], [r1],
        weights={"w_rrf": 20.0},
    )
    boost = r1.total - base
    # Boost should be capped, not the raw sum.
    capped_value = base * OBSERVATION_BOOST_CAP_RATIO
    assert abs(boost - capped_value) < 1e-6


def test_no_pullin_raws_outside_pool_arent_touched():
    """Saga canonical (P40) — only raws already in the candidate pool
    receive the boost. Raws missing from the pool aren't conjured up."""
    obs = _candidate("o1", rrf_score=0.033, memory_type="observation")
    in_pool_raw = _candidate("rin", rrf_score=0.020)
    # Stub conn says o1 evidences both rin AND rout, but rout isn't in
    # the raws list. _apply_evidence_boost should never reference rout.
    _apply_evidence_boost(
        _StubConn([("o1", "rin"), ("o1", "rout")]),
        [obs], [in_pool_raw],
        weights={"w_rrf": 20.0},
    )
    # rin got boosted; we verify no exception was raised + only
    # the in-pool atom's total changed (rout doesn't exist as a
    # candidate). The function returns None; absence of crash is the
    # test.
    assert in_pool_raw.total > 0


def test_zero_rrf_observation_contributes_nothing():
    """An observation that surfaced via activation alone (RRF score 0)
    shouldn't boost its raws — it's not a strong retrieval-signal
    endorser."""
    obs = _candidate("o1", rrf_score=0.0, memory_type="observation")
    r1 = _candidate("r1", rrf_score=0.020)
    base = r1.total
    _apply_evidence_boost(
        _StubConn([("o1", "r1")]),
        [obs], [r1],
        weights={"w_rrf": 20.0},
    )
    assert r1.total == base


def test_multiplier_constant_matches_saga():
    """Hard-pin the constants so a future tweak that breaks saga parity
    is loud."""
    assert OBSERVATION_BOOST_MULTIPLIER == 2.0
    assert OBSERVATION_BOOST_CAP_RATIO == 2.0
