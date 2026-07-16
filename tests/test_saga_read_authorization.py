from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from mimir.access_control import create_auth_context
from mimir.models import AgentEvent, AuthContext
from mimir.saga.client import SagaStore
from mimir.saga.ownership import (
    AuthorizationScope,
    Visibility,
    authorization_predicate,
    get_authorization_scope,
)
from mimir.saga.recall import recall
from mimir.saga.store import store


def _embed(_text: str) -> tuple[bytes, str, str, int]:
    return struct.pack("4f", 1.0, 0.0, 0.0, 0.0), "fake", "fake", 4


@pytest.fixture
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    schema = Path("mimir/saga/schema.sql").read_text()
    db.executescript(schema)
    yield db
    db.close()


def _store(
    conn: sqlite3.Connection,
    content: str,
    *,
    owner: str,
    visibility: str,
    domain: str | None = None,
) -> str:
    return store(
        conn,
        content,
        embed_fn=_embed,
        owner_principal=owner,
        visibility=visibility,
        origin_domain=domain,
    ).atom_id


def test_missing_auth_context_is_public_only_and_never_admin() -> None:
    scope = get_authorization_scope(None)
    assert scope == AuthorizationScope()
    where, params = authorization_predicate(scope, table="a")
    assert "1=1" not in where
    assert params == [Visibility.PUBLIC.value]


def test_owner_and_service_domain_grants_are_alternatives(conn: sqlite3.Connection) -> None:
    public = _store(conn, "public", owner="other", visibility="public")
    owned = _store(conn, "owned", owner="user:alice", visibility="private")
    foreign = _store(conn, "foreign", owner="user:bob", visibility="private")
    domain = _store(
        conn, "domain", owner="service:writer", visibility="service", domain="memory",
    )

    user_where, user_params = authorization_predicate(
        AuthorizationScope(principal="user:alice"), table="a",
    )
    user_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT a.id FROM atoms a WHERE {user_where}", user_params,
        ).fetchall()
    }
    assert user_ids == {public, owned}

    service_where, service_params = authorization_predicate(
        AuthorizationScope(
            principal="service:reader",
            is_service=True,
            readable_domains=("memory",),
        ),
        table="a",
    )
    service_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT a.id FROM atoms a WHERE {service_where}", service_params,
        ).fetchall()
    }
    assert service_ids == {public, domain}
    assert foreign not in service_ids


def test_unauthorized_candidates_are_removed_before_rrf_and_access(conn: sqlite3.Connection) -> None:
    hidden = _store(conn, "hidden query term", owner="user:bob", visibility="private")
    visible = _store(conn, "visible query term", owner="user:alice", visibility="private")
    scope = AuthorizationScope(principal="user:alice")

    result = recall(
        conn,
        "query term",
        query_embed_fn=lambda _q: [1.0, 0.0, 0.0, 0.0],
        faiss_search_fn=lambda _emb, _k: [(hidden, 0.99), (visible, 0.8)],
        fts_search_fn=lambda _q, _k: [(hidden, 10.0), (visible, 9.0)],
        triple_search_fn=lambda _emb, _k: [(hidden, 0.95), (visible, 0.7)],
        auth_scope=scope,
        fire_access_events=True,
    )

    candidates = result.observations + result.raws
    assert [candidate.atom["id"] for candidate in candidates] == [visible]
    assert candidates[0].semantic_rank == 1
    assert candidates[0].keyword_rank == 1
    assert candidates[0].triple_rank == 1

    hidden_retrievals = conn.execute(
        "SELECT COUNT(*) FROM access_events WHERE atom_id = ? AND source = 'retrieval'",
        (hidden,),
    ).fetchone()[0]
    assert hidden_retrievals == 0


def test_session_boundary_expansion_binds_authorization_before_limit(
    conn: sqlite3.Connection,
) -> None:
    session_id = "session-owned"
    conn.execute(
        "INSERT INTO sessions (id, channel_id, started_at, ended_at, summary, "
        "reflected_at, owner_principal, visibility) "
        "VALUES (?, 'channel', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:01:00+00:00', 'summary', "
        "'2026-01-01T00:01:00+00:00', 'user:alice', 'private')",
        (session_id,),
    )
    conn.commit()
    first = _store(conn, "first", owner="user:alice", visibility="private")
    second = _store(conn, "second", owner="user:alice", visibility="private")
    conn.execute(
        "UPDATE atoms SET session_id = ?, created_at = ? WHERE id = ?",
        (session_id, "2026-01-01T00:00:00+00:00", first),
    )
    conn.execute(
        "UPDATE atoms SET session_id = ?, created_at = ? WHERE id = ?",
        (session_id, "2026-01-01T00:00:01+00:00", second),
    )
    conn.commit()

    client = SagaStore(conn=conn, embedding_dim=4)
    auth_scope = AuthorizationScope(principal="user:alice")
    atom_ids = client._session_boundary_atom_pathway_with_conn(
        conn,
        "summary",
        limit=1,
        alpha=0.0,
        atoms_per_session=1,
        auth_context=auth_scope,
    )

    assert atom_ids == [first]


@pytest.mark.asyncio
async def test_get_atoms_missing_context_does_not_reveal_legacy_or_private(
    conn: sqlite3.Connection,
) -> None:
    public = _store(conn, "public", owner="other", visibility="public")
    private = _store(conn, "private", owner="user:alice", visibility="private")
    legacy = _store(conn, "legacy", owner="legacy_admin", visibility="legacy_admin")
    client = SagaStore(conn=conn, embedding_dim=4)

    payload = await client.get_atoms([public, private, legacy])

    assert [atom["id"] for atom in payload["atoms"]] == [public]
    assert payload["missing"] == [private, legacy]


@pytest.mark.parametrize(
    ("trigger", "principal"),
    [
        ("scheduled_tick", "scheduler"),
        ("poller", "poller"),
        ("saga_session_end", "synthesis"),
        ("upgrade", "system"),
    ],
)
def test_trusted_platform_auth_context_can_read_legacy_admin_memory(
    conn: sqlite3.Connection,
    trigger: str,
    principal: str,
) -> None:
    """Each server-created platform carrier can read the migrated legacy corpus."""
    public = _store(conn, "public", owner="other", visibility="public")
    legacy = _store(conn, "legacy", owner="legacy_admin", visibility="legacy_admin")
    service = _store(conn, "service-scoped", owner="scheduler", visibility="service")
    private = _store(conn, "private", owner="user:alice", visibility="private")

    auth_context = create_auth_context(
        AgentEvent(
            trigger=trigger,
            channel_id=f"service:{trigger}",
            service_principal=principal,
        ),
        enforce=True,
    )
    platform_scope = get_authorization_scope(auth_context)

    assert platform_scope.is_service is True
    assert platform_scope.is_platform_service is True
    assert platform_scope.is_admin is False
    assert platform_scope.service_canonical == principal

    where, params = authorization_predicate(platform_scope, table="a")
    readable_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT a.id FROM atoms a WHERE {where}", params,
        ).fetchall()
    }

    assert public in readable_ids
    assert legacy in readable_ids
    assert service in readable_ids
    assert private in readable_ids


def test_forged_platform_trigger_does_not_widen_read_scope(
    conn: sqlite3.Connection,
) -> None:
    legacy = _store(conn, "legacy", owner="legacy_admin", visibility="legacy_admin")
    auth_context = create_auth_context(
        AgentEvent(
            trigger="scheduled_tick",
            channel_id="api-request",
            service_principal="scheduler",
        ),
        event_ingress="http-api",
        enforce=True,
    )
    scope = get_authorization_scope(auth_context)

    assert scope.is_service is False
    assert scope.is_platform_service is False
    where, params = authorization_predicate(scope, table="a")
    readable_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT a.id FROM atoms a WHERE {where}", params,
        ).fetchall()
    }
    assert legacy not in readable_ids


def test_platform_service_can_read_service_scoped_memory(conn: sqlite3.Connection) -> None:
    """Platform services can read service-scoped memory."""
    service_scoped = _store(conn, "service-scoped", owner="poller", visibility="service")

    platform_scope = AuthorizationScope(
        principal="service:poller",
        is_service=True,
        is_platform_service=True,
    )

    where, params = authorization_predicate(platform_scope, table="a")
    readable_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT a.id FROM atoms a WHERE {where}", params,
        ).fetchall()
    }

    assert service_scoped in readable_ids


def test_regular_service_still_restricted_by_readable_domains(conn: sqlite3.Connection) -> None:
    """Non-platform services with readable_domains get domain-restricted access.

    This verifies that we haven't broken the existing service model - regular
    services (e.g., external integration services) still get domain-restricted
    access via readable_domains.
    """
    public = _store(conn, "public", owner="other", visibility="public")
    other_domain = _store(
        conn, "other_domain", owner="service:writer", visibility="service", domain="other",
    )
    allowed_domain = _store(
        conn, "allowed_domain", owner="service:writer", visibility="service", domain="memory",
    )

    regular_service_scope = AuthorizationScope(
        principal="service:reader",
        is_service=True,
        is_platform_service=False,
        readable_domains=("memory",),
    )

    where, params = authorization_predicate(regular_service_scope, table="a")
    readable_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT a.id FROM atoms a WHERE {where}", params,
        ).fetchall()
    }

    assert public in readable_ids
    assert allowed_domain in readable_ids
    assert other_domain not in readable_ids


def test_platform_service_gets_full_read_without_admin_role(
    conn: sqlite3.Connection,
) -> None:
    """Platform read scope includes other owners without conferring admin role."""
    owned = _store(conn, "owned-by-scheduler", owner="scheduler", visibility="private")
    other_owned = _store(conn, "owned-by-user", owner="user:alice", visibility="private")

    platform_scope = AuthorizationScope(
        principal="scheduler",
        is_service=True,
        is_platform_service=True,
    )

    assert platform_scope.is_admin is False
    where, params = authorization_predicate(platform_scope, table="a")
    readable_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT a.id FROM atoms a WHERE {where}", params,
        ).fetchall()
    }

    assert where == "1=1"
    assert params == []
    assert owned in readable_ids
    assert other_owned in readable_ids


@pytest.mark.parametrize(
    "sentinel_principal",
    ["legacy_admin", "service", "system"],
)
def test_sentinel_principal_cannot_owner_match_legacy_admin_rows(
    conn: sqlite3.Connection,
    sentinel_principal: str,
) -> None:
    """Reserved sentinel principals cannot use owner-match to read legacy rows.

    A caller whose principal is a reserved sentinel value (legacy_admin, service,
    system) should NOT be able to read rows owned by legacy_admin via the
    owner-match grant. This prevents a regular user who happens to have a
    sentinel principal from accessing the entire legacy/default-owned corpus.
    """
    public = _store(conn, "public", owner="other", visibility="public")
    legacy_admin_owned = _store(
        conn, "legacy-owned", owner="legacy_admin", visibility="legacy_admin",
    )
    service_owned = _store(
        conn, "service-owned", owner="service", visibility="service",
    )
    system_owned = _store(
        conn, "system-owned", owner="system", visibility="service",
    )
    regular_owned = _store(
        conn, "regular-owned", owner="user:alice", visibility="private",
    )

    scope = AuthorizationScope(principal=sentinel_principal)
    where, params = authorization_predicate(scope, table="a")

    readable_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT a.id FROM atoms a WHERE {where}", params,
        ).fetchall()
    }

    assert public in readable_ids
    assert legacy_admin_owned not in readable_ids
    assert service_owned not in readable_ids
    assert system_owned not in readable_ids
    assert regular_owned not in readable_ids


@pytest.mark.parametrize(
    "sentinel_principal",
    ["legacy_admin", "service", "system"],
)
def test_sentinel_principal_cannot_owner_match_own_rows(
    conn: sqlite3.Connection,
    sentinel_principal: str,
) -> None:
    """Reserved sentinel principals cannot use owner-match to read their own rows.

    Even if a principal has the same name as a sentinel, they should not get
    owner-match grants. This is defense-in-depth - the guard is at the
    predicate level, not just at ingress.
    """
    my_row = _store(
        conn, "my-row", owner=sentinel_principal, visibility="private",
    )

    scope = AuthorizationScope(principal=sentinel_principal)
    where, params = authorization_predicate(scope, table="a")

    readable_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT a.id FROM atoms a WHERE {where}", params,
        ).fetchall()
    }

    assert my_row not in readable_ids
def _store_with_access_event(
    conn: sqlite3.Connection,
    content: str,
    *,
    owner: str,
    visibility: str,
    domain: str | None = None,
    agent_id: str,
    source: str = "retrieval",
) -> str:
    atom_id = store(
        conn,
        content,
        embed_fn=_embed,
        owner_principal=owner,
        visibility=visibility,
        origin_domain=domain,
    ).atom_id
    conn.execute(
        "INSERT INTO access_events (atom_id, session_id, ts, source) "
        "VALUES (?, ?, ?, ?)",
        (atom_id, "test-session", "2026-07-01T00:00:00+00:00", source),
    )
    conn.commit()
    return atom_id


@pytest.mark.asyncio
async def test_most_retrieved_atoms_admin_sees_all(conn: sqlite3.Connection) -> None:
    public = _store_with_access_event(
        conn, "public atom", owner="user:alice", visibility="public", agent_id="test-agent",
    )
    private = _store_with_access_event(
        conn, "private atom", owner="user:bob", visibility="private", agent_id="test-agent",
    )
    admin_auth = AuthContext(
        principal="operator",
        canonical_principal="operator",
        roles=("admin",),
        event_ingress=None,
        trigger="test",
        channel_id=None,
        interactivity=None,
    )
    client = SagaStore(conn=conn, embedding_dim=4)
    result = await client.most_retrieved_atoms(
        days=30,
        count=10,
        auth_context=admin_auth,
    )
    result_ids = {r["id"] for r in result}
    assert public in result_ids
    assert private in result_ids


@pytest.mark.asyncio
async def test_most_retrieved_atoms_scoped_principal_sees_only_authorized(
    conn: sqlite3.Connection,
) -> None:
    public = _store_with_access_event(
        conn, "public atom", owner="user:other", visibility="public", agent_id="test-agent",
    )
    private_owned = _store_with_access_event(
        conn, "my private atom", owner="user:alice", visibility="private", agent_id="test-agent",
    )
    private_other = _store_with_access_event(
        conn, "other private atom", owner="user:bob", visibility="private", agent_id="test-agent",
    )
    alice_auth = AuthContext(
        principal="user:alice",
        canonical_principal="user:alice",
        roles=(),
        event_ingress=None,
        trigger="test",
        channel_id=None,
        interactivity=None,
    )
    client = SagaStore(conn=conn, embedding_dim=4)
    result = await client.most_retrieved_atoms(
        days=30,
        count=10,
        auth_context=alice_auth,
    )
    result_ids = {r["id"] for r in result}
    assert public in result_ids
    assert private_owned in result_ids
    assert private_other not in result_ids


@pytest.mark.asyncio
async def test_most_retrieved_atoms_no_auth_sees_only_public(conn: sqlite3.Connection) -> None:
    public = _store_with_access_event(
        conn, "public atom", owner="user:other", visibility="public", agent_id="test-agent",
    )
    private = _store_with_access_event(
        conn, "private atom", owner="user:bob", visibility="private", agent_id="test-agent",
    )
    client = SagaStore(conn=conn, embedding_dim=4)
    result = await client.most_retrieved_atoms(
        days=30,
        count=10,
    )
    result_ids = {r["id"] for r in result}
    assert public in result_ids
    assert private not in result_ids
