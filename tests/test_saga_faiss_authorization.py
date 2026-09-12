from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from mimir.models import AuthContext
from mimir.saga.client import _make_faiss_search_fn
from mimir.saga.ownership import SagaReadAuthorization


class RankedIndex:
    """Deterministic neighbors; respects k so a recall cutoff is observable."""

    def __init__(self, ids):
        self.candidates = [(atom_id, 1.0 - i / len(ids)) for i, atom_id in enumerate(ids)]
        self.total_vectors = len(ids)
        self.requests = []

    def search(self, query, *, top_k):
        self.requests.append(top_k)
        return self.candidates[:top_k]


class MeasuredConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if sql.startswith("SELECT a.id FROM atoms"):
            self.lookups.append((sql, parameters))
        return super().execute(sql, parameters)


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:", factory=MeasuredConnection)
    db.lookups = []
    db.execute("""CREATE TABLE atoms (
        id TEXT PRIMARY KEY, tombstoned INTEGER DEFAULT 0,
        agent_id TEXT DEFAULT 'default', owner_principal TEXT DEFAULT 'user:bob',
        visibility TEXT DEFAULT 'private', origin_domain TEXT)""")
    yield db
    db.close()


def authorization(enforced=True):
    context = AuthContext(
        principal="alice", canonical_principal="user:alice", roles=("user",),
        event_ingress="test", trigger="test", channel_id=None,
        interactivity=None, enforcement_enabled=enforced,
    )
    return SagaReadAuthorization(context, "query")


@pytest.mark.parametrize("legacy_scope", [False, True])
def test_enforced_recall_buried_neighbors_and_bounded_batches(conn, legacy_scope):
    hidden = [f"hidden-{i}" for i in range(2048)]
    eligible = [f"eligible-{i}" for i in range(12)]
    conn.executemany("INSERT INTO atoms(id) VALUES (?)", ((i,) for i in hidden))
    conn.executemany(
        "INSERT INTO atoms(id, owner_principal) VALUES (?, 'user:alice')",
        ((i,) for i in eligible),
    )
    index = RankedIndex(hidden + eligible)
    auth = authorization()
    kwargs = {"auth_scope": auth.strict_scope} if legacy_scope else {"read_authorization": auth}
    search = _make_faiss_search_fn(index, conn, **kwargs)
    assert search([1.0], 12) == index.candidates[-12:]
    assert index.requests == [index.total_vectors]  # Full recall still costs a full search.
    assert len(conn.lookups) == 9
    for sql, params in conn.lookups:
        assert "a.id IN (" in sql
        assert len(params) <= 259  # 256 IDs, agent, public visibility, owner.
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        assert any("SEARCH a USING INDEX sqlite_autoindex_atoms_1 (id=?)" in r[3] for r in plan)


@pytest.mark.parametrize("size", [512, 10000])
def test_authorization_materialization_stops_at_top_k(conn, size):
    ids = [f"atom-{i}" for i in range(size)]
    conn.executemany("INSERT INTO atoms(id, visibility) VALUES (?, 'public')", ((i,) for i in ids))
    index = RankedIndex(ids)
    search = _make_faiss_search_fn(index, conn, read_authorization=authorization())
    steps = []
    conn.set_progress_handler(lambda: steps.append(1) or 0, 1)
    assert search([1.0], 12) == index.candidates[:12]
    conn.set_progress_handler(None, 0)
    assert len(conn.lookups) == 1
    assert len(conn.lookups[0][1]) == 259
    assert len(steps) < 6000  # Indexed candidate SQL, not a scan of all authorized rows.


def test_enforced_guards_and_live_acl_changes(conn):
    conn.executemany(
        "INSERT INTO atoms(id, tombstoned, agent_id, owner_principal, visibility) VALUES (?, ?, ?, ?, ?)",
        [
            ("private", 0, "default", "user:bob", "private"),
            ("service", 0, "default", "service", "service"),
            ("legacy", 0, "default", "legacy_admin", "legacy_admin"),
            ("deleted", 1, "default", "user:alice", "public"),
            ("other-agent", 0, "other", "user:alice", "public"),
            ("own", 0, "default", "user:alice", "private"),
            ("shared", 0, "shared", "user:bob", "public"),
        ],
    )
    index = RankedIndex(["missing", "private", "service", "legacy", "deleted", "other-agent", "own", "shared"])
    auth = authorization()
    search = _make_faiss_search_fn(index, conn, read_authorization=auth)
    assert search([1.0], 12) == index.candidates[-2:]
    conn.execute("UPDATE atoms SET owner_principal='user:bob' WHERE id='own'")
    conn.execute("UPDATE atoms SET visibility='private' WHERE id='shared'")
    assert search([1.0], 12) == []
    conn.execute("UPDATE atoms SET visibility='public' WHERE id='private'")
    assert search([1.0], 12) == [index.candidates[1]]
    anonymous = SagaReadAuthorization(replace(auth.auth_context, principal=None, canonical_principal=None), "query")
    assert _make_faiss_search_fn(index, conn, read_authorization=anonymous)([1.0], 12) == [index.candidates[1]]


def test_default_mode_is_unchanged_and_does_not_query_sql(conn):
    index = RankedIndex(["private", "missing", "public"])
    search = _make_faiss_search_fn(index, conn, read_authorization=authorization(False))
    assert search([1.0], 2) == index.candidates[:2]
    assert index.requests == [2]
    assert conn.lookups == []
