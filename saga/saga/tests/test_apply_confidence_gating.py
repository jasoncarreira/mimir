"""Tests for ``apply_confidence_gating`` (CR#14).

The helper now backs both ``saga/server.py::api_query`` (HTTP path) and
``mimir/saga_client.py::_InProcessSaga.query`` (in-process path), so a
behavior regression here surfaces in BOTH integration paths. Pin every
branch of the filter — gating disabled, no drops, partial drops on each
side, full drops, missing _confidence_tier, malformed tier strings.
"""

from __future__ import annotations

import pytest

from saga.core import apply_confidence_gating


def _atom(tier: str | None = "low", **extra) -> dict:
    out = {"id": extra.get("id", "a1"), "_confidence_tier": tier}
    if tier is None:
        out.pop("_confidence_tier")
    out.update(extra)
    return out


# ─── Gating disabled ─────────────────────────────────────────────────


def test_gating_disabled_returns_inputs_unchanged():
    obs = [_atom(tier="none"), _atom(tier="low")]
    raws = [_atom(tier="medium")]
    out_obs, out_raws, reason = apply_confidence_gating(
        obs, raws, floor="high", gating_enabled=False,
    )
    assert out_obs is obs  # passthrough — same object
    assert out_raws is raws
    assert reason is None


# ─── No drops ────────────────────────────────────────────────────────


def test_no_drops_returns_none_reason():
    obs = [_atom(tier="medium"), _atom(tier="high")]
    raws = [_atom(tier="low")]
    out_obs, out_raws, reason = apply_confidence_gating(
        obs, raws, floor="low",
    )
    assert len(out_obs) == 2
    assert len(out_raws) == 1
    assert reason is None


# ─── Drops on observations ───────────────────────────────────────────


def test_drops_observations_below_floor():
    obs = [
        _atom(tier="low", id="o1"),    # below high
        _atom(tier="high", id="o2"),   # passes
    ]
    raws = [_atom(tier="medium", id="r1")]
    out_obs, out_raws, reason = apply_confidence_gating(
        obs, raws, floor="high",
    )
    assert [o["id"] for o in out_obs] == ["o2"]
    # raws also gated by the same floor — medium < high → dropped.
    assert out_raws == []
    assert reason is not None
    assert "floor=high" in reason
    assert "1 obs" in reason
    assert "1 raws" in reason


# ─── Tier rank — explicit floors ─────────────────────────────────────


@pytest.mark.parametrize("floor,kept_tiers", [
    ("none", ["none", "low", "medium", "high"]),
    ("low", ["low", "medium", "high"]),
    ("medium", ["medium", "high"]),
    ("high", ["high"]),
])
def test_each_floor_keeps_tiers_at_or_above(floor, kept_tiers):
    obs = [_atom(tier=t, id=t) for t in ["none", "low", "medium", "high"]]
    out_obs, _, _ = apply_confidence_gating(obs, [], floor=floor)
    assert [o["id"] for o in out_obs] == kept_tiers


# ─── Missing / malformed tier ────────────────────────────────────────


def test_missing_tier_field_treated_as_none():
    """Atoms without ``_confidence_tier`` must rank at ``"none"`` (0) —
    the most permissive default tier still drops them when the floor
    is anything stricter than ``"none"``."""
    obs = [_atom(tier=None, id="o1")]  # no _confidence_tier field
    out_obs, _, reason = apply_confidence_gating(obs, [], floor="low")
    assert out_obs == []
    assert reason is not None


def test_malformed_tier_string_treated_as_zero_rank():
    """An unrecognized tier value (e.g. an old version's tier name)
    ranks at 0 — gets dropped at any floor stricter than ``"none"``."""
    obs = [_atom(tier="garbage", id="o1")]
    out_obs, _, reason = apply_confidence_gating(obs, [], floor="low")
    assert out_obs == []
    assert reason is not None


def test_unknown_floor_string_defaults_to_low():
    """An unknown floor string defaults to ``"low"`` (rank 1) — the
    same default the server's ``default_min_confidence_tier`` config
    falls back to. Atoms at ``low`` and above pass."""
    obs = [_atom(tier=t, id=t) for t in ["none", "low", "medium"]]
    out_obs, _, _ = apply_confidence_gating(obs, [], floor="zzgarbage")
    assert [o["id"] for o in out_obs] == ["low", "medium"]


# ─── Reason string format ────────────────────────────────────────────


def test_reason_includes_per_side_drop_counts():
    obs = [_atom(tier="none"), _atom(tier="low")]
    raws = [_atom(tier="none"), _atom(tier="none"), _atom(tier="low")]
    _, _, reason = apply_confidence_gating(obs, raws, floor="low")
    # 1 obs + 2 raws dropped.
    assert reason == "floor=low: dropped 1 obs and 2 raws below threshold"


# ─── Empty inputs ────────────────────────────────────────────────────


def test_empty_inputs_return_empty_no_reason():
    out_obs, out_raws, reason = apply_confidence_gating([], [], floor="medium")
    assert out_obs == []
    assert out_raws == []
    assert reason is None
