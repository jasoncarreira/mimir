"""P42: top-N triples in /v1/query response with valid dates.

Covers the new ``query_triples_for_response`` helper in
``saga/triples.py`` and its wiring through the two-tier
``api_query`` path in ``saga/server.py``.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest


def _stable_vec(text: str, dim: int = 1024) -> list[float]:
    """Deterministic per-text 1024d vector. Uses sha256 as a seed so
    the test gets distinct vectors per triple (otherwise cosine is
    always 1.0 and ranking is meaningless)."""
    h = int(hashlib.sha256(text.encode()).hexdigest(), 16)
    rng = np.random.default_rng(h % (2**32))
    return list(rng.standard_normal(dim).astype(float))


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test_saga.db"
    monkeypatch.setattr("saga.core.DB_PATH", db_path)
    monkeypatch.setattr("saga.triples.DB_PATH", db_path)
    # Per-text embeddings so triples have different vectors and cosine
    # ranking is observable. Triples are embedded as
    # ``"{subject} {predicate} {object}"`` (predicate underscores → spaces).
    monkeypatch.setattr("saga.core.embed_text", _stable_vec)
    monkeypatch.setattr("saga.core.embed_query", _stable_vec)
    monkeypatch.setattr(
        "saga.core._cached_embed_query_import", lambda t: tuple(_stable_vec(t))
    )
    monkeypatch.setattr("saga.core.cached_embed_query", _stable_vec)
    from saga.core import get_db, run_migrations
    from saga.triples import init_triples_schema
    conn = get_db()
    init_triples_schema(conn)
    conn.close()
    run_migrations()
    yield db_path


# ─── query_triples_for_response — shape ────────────────────────────────


def test_returns_empty_when_no_triples():
    from saga.triples import query_triples_for_response
    out = query_triples_for_response("anything", top_k=5)
    assert out == []


def test_returns_empty_when_top_k_zero():
    from saga.triples import update_world, query_triples_for_response
    update_world("user", "lives_in", "Oakland", source_atom_id="atom1")
    assert query_triples_for_response("Where?", top_k=0) == []


def test_response_carries_all_expected_fields():
    """Each row must include subject/predicate/object/valid_from/
    valid_until/confidence/_similarity/source_atom_id."""
    from saga.triples import update_world, query_triples_for_response
    update_world(
        "user", "profession", "engineer",
        confidence=0.92, source_atom_id="atom-abc",
    )
    out = query_triples_for_response("profession", top_k=5)
    assert len(out) == 1
    row = out[0]
    assert set(row.keys()) >= {
        "subject", "predicate", "object",
        "valid_from", "valid_until", "confidence",
        "_similarity", "source_atom_id",
    }
    assert row["subject"] == "user"
    assert row["predicate"] == "profession"
    assert row["object"] == "engineer"
    assert row["confidence"] == pytest.approx(0.92)
    assert row["source_atom_id"] == "atom-abc"


def test_includes_valid_from_and_valid_until():
    """The user-requested invariant: dates are surfaced when present."""
    from saga.triples import update_world, query_triples_for_response
    past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    update_world(
        "user", "subscription", "pro",
        valid_from=past, valid_until=future, source_atom_id="atom-sub",
    )
    out = query_triples_for_response("subscription tier", top_k=5)
    matching = [r for r in out if r["subject"] == "user"
                and r["predicate"] == "subscription"]
    assert len(matching) == 1
    row = matching[0]
    assert row["valid_from"] == past
    assert row["valid_until"] == future


def test_null_valid_until_passes_through_as_none():
    """update_world auto-fills valid_from with now; valid_until stays None
    when not specified. The agent-facing response must preserve that None."""
    from saga.triples import update_world, query_triples_for_response
    update_world(
        "user", "favorite_color", "blue", source_atom_id="atom-x",
    )
    out = query_triples_for_response("color preference", top_k=5)
    assert out[0]["valid_until"] is None
    # valid_from gets auto-set to now() — non-None ISO string.
    assert isinstance(out[0]["valid_from"], str)


# ─── expiry filtering ─────────────────────────────────────────────────


def test_expired_triples_filtered_by_default():
    from saga.triples import update_world, query_triples_for_response
    past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    update_world(
        "user", "is_at", "cafe",
        valid_from=past, valid_until=expired, source_atom_id="atom-exp",
    )
    out = query_triples_for_response("location", top_k=5)
    assert not any(r["object"] == "cafe" for r in out)


def test_include_expired_returns_them():
    from saga.triples import update_world, query_triples_for_response
    past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    update_world(
        "user", "is_at", "cafe",
        valid_from=past, valid_until=expired, source_atom_id="atom-exp",
    )
    out = query_triples_for_response("location", top_k=5, include_expired=True)
    assert any(r["object"] == "cafe" for r in out)


def test_future_valid_until_kept():
    from saga.triples import update_world, query_triples_for_response
    past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    update_world(
        "user", "subscription", "pro",
        valid_from=past, valid_until=future, source_atom_id="atom-sub",
    )
    out = query_triples_for_response("subscription", top_k=5)
    assert any(r["object"] == "pro" for r in out)


# ─── ordering + top_k cap ─────────────────────────────────────────────


def test_top_k_caps_results():
    from saga.triples import update_world, query_triples_for_response
    # update_world auto-closes prior triples for the same (subject,
    # predicate) — vary the predicate so all 10 stay active.
    for i in range(10):
        update_world("user", f"fact_{i}", f"value_{i}", source_atom_id=f"atom{i}")
    out = query_triples_for_response("anything", top_k=3)
    assert len(out) == 3


def test_orders_by_cosine_descending():
    """The triple whose embedded text best matches the query embedding
    should sort first. We use stable per-text vectors so the test is
    deterministic — same query+triple combo always produces the same
    similarity."""
    from saga.triples import update_world, query_triples_for_response
    update_world("user", "lives_in_a", "Oakland", source_atom_id="a")
    update_world("user", "lives_in_b", "Berlin", source_atom_id="b")
    update_world("user", "lives_in_c", "Tokyo", source_atom_id="c")
    out = query_triples_for_response("user lives in Oakland", top_k=3)
    # Strict ordering by _similarity descending.
    sims = [r["_similarity"] for r in out]
    assert sims == sorted(sims, reverse=True)


# ─── server wiring (P42 flag) ─────────────────────────────────────────


def test_two_tier_response_omits_triples_when_flag_off(monkeypatch):
    """Flag default is False: existing two-tier shape is unchanged
    (triples slot stays empty)."""
    from saga.config import _DEFAULTS
    monkeypatch.setitem(_DEFAULTS["retrieval"], "include_triples_in_response", False)
    from saga.triples import update_world
    update_world("user", "lives_in", "Oakland", source_atom_id="atom1")

    from saga.server import api_query, QueryRequest
    req = QueryRequest(query="Where does the user live?", two_tier=True)
    import asyncio
    resp = asyncio.run(api_query(req))
    assert resp["two_tier"] is True
    assert resp["triples"] == []


def test_two_tier_response_populates_triples_when_flag_on(monkeypatch):
    from saga.config import _DEFAULTS
    monkeypatch.setitem(_DEFAULTS["retrieval"], "include_triples_in_response", True)
    monkeypatch.setitem(_DEFAULTS["retrieval"], "response_triples_top_k", 3)
    from saga.triples import update_world
    for i in range(5):
        update_world("user", f"fact_{i}", f"value_{i}", source_atom_id=f"atom{i}")

    from saga.server import api_query, QueryRequest
    req = QueryRequest(query="user fact", two_tier=True)
    import asyncio
    resp = asyncio.run(api_query(req))
    assert resp["two_tier"] is True
    assert len(resp["triples"]) == 3
    for t in resp["triples"]:
        assert "subject" in t and "predicate" in t and "object" in t
        assert "valid_from" in t and "valid_until" in t
        assert "_similarity" in t and "source_atom_id" in t
    # items_returned counts the triples too.
    assert resp["items_returned"] >= 3
