"""Tests for P47 consolidation-pass-v2 bundle:
- P17: trend writer in consolidation
- P35-c: CONTRADICTIONS parsing in _parse_structured_synthesis
- P45 ext: get_most_retrieved trend filter
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test_saga.db"
    monkeypatch.setattr("saga.core.DB_PATH", db_path)
    monkeypatch.setattr("saga.triples.DB_PATH", db_path)
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("saga.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("saga.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr(
        "saga.core._cached_embed_query_import", lambda t: tuple(fake_emb)
    )
    monkeypatch.setattr("saga.core.cached_embed_query", lambda t: fake_emb)
    from saga.core import get_db, run_migrations
    from saga.triples import init_triples_schema
    conn = get_db()
    init_triples_schema(conn)
    conn.close()
    run_migrations()
    yield db_path


# ─── P35-c: CONTRADICTIONS parsing ────────────────────────────────────


def test_parse_contradictions_section():
    from saga.consolidation import _parse_structured_synthesis
    text = (
        "OBSERVATION:\n"
        "User likes coffee.\n\n"
        "TRIPLES:\n"
        "(User, likes, coffee)\n\n"
        "CONTRADICTIONS:\n"
        "1 vs 3: atom 1 says morning person, atom 3 says night owl\n"
        "2 vs 4: different city of residence\n"
    )
    obs, triples, contras = _parse_structured_synthesis(text)
    assert obs == "User likes coffee."
    assert len(triples) == 1
    assert len(contras) == 2
    assert "morning person" in contras[0]
    assert "different city" in contras[1]


def test_parse_contradictions_none_returns_empty():
    from saga.consolidation import _parse_structured_synthesis
    text = (
        "OBSERVATION:\nUser likes coffee.\n\n"
        "TRIPLES:\n(User, likes, coffee)\n\n"
        "CONTRADICTIONS:\nNONE\n"
    )
    _, _, contras = _parse_structured_synthesis(text)
    assert contras == []


def test_parse_handles_missing_contradictions_section():
    """Legacy 2-section output (OBSERVATION + TRIPLES) still parses."""
    from saga.consolidation import _parse_structured_synthesis
    text = (
        "OBSERVATION:\nUser likes coffee.\n\n"
        "TRIPLES:\n(User, likes, coffee)\n"
    )
    obs, triples, contras = _parse_structured_synthesis(text)
    assert obs == "User likes coffee."
    assert len(triples) == 1
    assert contras == []


def test_contradictions_terminates_triples_block():
    """The CONTRADICTIONS section header must end the TRIPLES block —
    its lines shouldn't bleed into the triple parser."""
    from saga.consolidation import _parse_structured_synthesis
    text = (
        "OBSERVATION:\nx\n\n"
        "TRIPLES:\n(User, likes, coffee)\n\n"
        "CONTRADICTIONS:\n1 vs 2: bogus (Foo, bar_baz, qux) text\n"
    )
    _, triples, contras = _parse_structured_synthesis(text)
    # Only the legit triple, not a (Foo, bar_baz, qux) parsed from the
    # contradictions block.
    assert len(triples) == 1
    assert triples[0]["subject"] == "User"
    assert len(contras) == 1


# ─── P17: trend writer ────────────────────────────────────────────────


def test_compute_trend_returns_none_with_no_access_history():
    from saga.consolidation import ConsolidationEngine
    from saga.core import get_db
    engine = ConsolidationEngine()
    conn = get_db()
    try:
        trend = engine._compute_trend_for_cluster(
            conn, ["nonexistent-id"],
            datetime.now(timezone.utc).isoformat(),
        )
        assert trend is None
    finally:
        conn.close()


def test_compute_trend_improving_when_recent_dominates():
    from saga.consolidation import ConsolidationEngine
    from saga.core import get_db, store_atom
    aid = store_atom("test content", stream="semantic")
    now = datetime.now(timezone.utc)
    conn = get_db()
    try:
        # 10 recent (last 30d) accesses, 1 prior (30-90d ago) → ratio 10
        for i in range(10):
            conn.execute(
                "INSERT INTO access_log (atom_id, accessed_at, "
                "activation_score, retrieval_mode) VALUES (?, ?, 1.0, 'task')",
                (aid, (now - timedelta(days=2 + i)).isoformat()),
            )
        conn.execute(
            "INSERT INTO access_log (atom_id, accessed_at, "
            "activation_score, retrieval_mode) VALUES (?, ?, 1.0, 'task')",
            (aid, (now - timedelta(days=60)).isoformat()),
        )
        conn.commit()
        engine = ConsolidationEngine()
        trend = engine._compute_trend_for_cluster(conn, [aid], now.isoformat())
        assert trend == "improving"
    finally:
        conn.close()


def test_compute_trend_stale_when_recent_collapses():
    from saga.consolidation import ConsolidationEngine
    from saga.core import get_db, store_atom
    aid = store_atom("test content", stream="semantic")
    now = datetime.now(timezone.utc)
    conn = get_db()
    try:
        # 1 recent, 20 prior → ratio 0.05 → stale
        conn.execute(
            "INSERT INTO access_log (atom_id, accessed_at, "
            "activation_score, retrieval_mode) VALUES (?, ?, 1.0, 'task')",
            (aid, (now - timedelta(days=10)).isoformat()),
        )
        for i in range(20):
            conn.execute(
                "INSERT INTO access_log (atom_id, accessed_at, "
                "activation_score, retrieval_mode) VALUES (?, ?, 1.0, 'task')",
                (aid, (now - timedelta(days=40 + i)).isoformat()),
            )
        conn.commit()
        engine = ConsolidationEngine()
        trend = engine._compute_trend_for_cluster(conn, [aid], now.isoformat())
        assert trend == "stale"
    finally:
        conn.close()


def test_compute_trend_stable_when_recent_matches_prior():
    from saga.consolidation import ConsolidationEngine
    from saga.core import get_db, store_atom
    aid = store_atom("test content", stream="semantic")
    now = datetime.now(timezone.utc)
    conn = get_db()
    try:
        # 5 recent, 5 prior → ratio 1.0 → stable
        for i in range(5):
            conn.execute(
                "INSERT INTO access_log (atom_id, accessed_at, "
                "activation_score, retrieval_mode) VALUES (?, ?, 1.0, 'task')",
                (aid, (now - timedelta(days=2 + i)).isoformat()),
            )
        for i in range(5):
            conn.execute(
                "INSERT INTO access_log (atom_id, accessed_at, "
                "activation_score, retrieval_mode) VALUES (?, ?, 1.0, 'task')",
                (aid, (now - timedelta(days=40 + i)).isoformat()),
            )
        conn.commit()
        engine = ConsolidationEngine()
        trend = engine._compute_trend_for_cluster(conn, [aid], now.isoformat())
        assert trend == "stable"
    finally:
        conn.close()


# ─── P45 ext: get_most_retrieved trend filter ────────────────────────


def test_get_most_retrieved_trend_filter():
    from saga.core import get_db, get_most_retrieved, store_atom
    a_imp = store_atom("improving content", stream="semantic")
    a_stale = store_atom("stale content", stream="semantic")
    a_unl = store_atom("unlabeled content", stream="semantic")

    now = datetime.now(timezone.utc)
    conn = get_db()
    try:
        # Label trends directly (bypassing consolidation for the unit test).
        conn.execute("UPDATE atoms SET trend = ? WHERE id = ?",
                     ("improving", a_imp))
        conn.execute("UPDATE atoms SET trend = ? WHERE id = ?",
                     ("stale", a_stale))
        # a_unl: trend stays NULL.

        # Give each atom one recent access so they're in the window.
        for aid in (a_imp, a_stale, a_unl):
            conn.execute(
                "INSERT INTO access_log (atom_id, accessed_at, "
                "activation_score, retrieval_mode) VALUES (?, ?, 1.0, 'task')",
                (aid, (now - timedelta(hours=1)).isoformat()),
            )
        conn.commit()
    finally:
        conn.close()

    # No filter → all 3 returned.
    out = get_most_retrieved(days=7, count=10)
    ids = {a["id"] for a in out}
    assert ids == {a_imp, a_stale, a_unl}

    # Trend filter: improving → only the improving atom.
    out = get_most_retrieved(days=7, count=10, trend="improving")
    assert [a["id"] for a in out] == [a_imp]
    assert out[0]["trend"] == "improving"

    # Trend filter: stale → only the stale atom.
    out = get_most_retrieved(days=7, count=10, trend="stale")
    assert [a["id"] for a in out] == [a_stale]


def test_get_most_retrieved_includes_trend_in_output():
    from saga.core import get_db, get_most_retrieved, store_atom
    aid = store_atom("content", stream="semantic")
    now = datetime.now(timezone.utc)
    conn = get_db()
    try:
        conn.execute("UPDATE atoms SET trend = ? WHERE id = ?",
                     ("weakening", aid))
        conn.execute(
            "INSERT INTO access_log (atom_id, accessed_at, "
            "activation_score, retrieval_mode) VALUES (?, ?, 1.0, 'task')",
            (aid, (now - timedelta(hours=1)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    out = get_most_retrieved(days=7, count=10)
    assert len(out) == 1
    assert out[0]["trend"] == "weakening"
