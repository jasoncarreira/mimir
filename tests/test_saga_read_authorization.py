from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.access_control import (
    CapabilityTier,
    build_trigger_service_principal,
    builtin_trigger_service_principal,
    create_auth_context,
)
from mimir.models import AgentEvent, AuthContext
from mimir.saga.client import SagaStore
from mimir.saga.ownership import (
    AuthorizationScope,
    SagaReadAuthorization,
    Visibility,
    authorization_predicate,
    get_authorization_scope,
)
from mimir.saga.recall import recall
from mimir.saga.store import store
from mimir.saga.triples import detect_contradictions, get_current_value, get_history


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


def test_public_authorization_scope_cannot_assert_read_authority() -> None:
    scope = get_authorization_scope(
        AuthorizationScope(
            principal="user:attacker",
            is_admin=True,
            is_service=True,
            is_platform_service=True,
            readable_domains=("private",),
        )
    )

    assert scope == AuthorizationScope()


def test_auth_context_subclass_cannot_carry_read_authority() -> None:
    @dataclass(frozen=True)
    class ForgedAuthContext(AuthContext):
        pass

    forged = ForgedAuthContext(
        principal="attacker",
        canonical_principal="user:attacker",
        roles=("admin",),
        event_ingress=None,
        trigger="user_message",
        channel_id=None,
        interactivity=None,
        enforcement_enabled=True,
    )

    assert get_authorization_scope(forged) == AuthorizationScope()
    assert SagaReadAuthorization(forged, "test").enforcement_enabled is False


def test_world_state_readers_filter_cross_owner_rows(
    conn: sqlite3.Connection,
) -> None:
    for value, valid_from, owner in (
        ("alice-value", "2026-01-01", "user:alice"),
        ("bob-value", "2026-02-01", "user:bob"),
    ):
        conn.execute(
            "INSERT INTO world_state "
            "(subject, predicate, value, valid_from, is_current, updated_at, "
            "owner_principal, visibility) VALUES (?, ?, ?, ?, 1, ?, ?, 'private')",
            ("Shared", "status", value, valid_from, valid_from, owner),
        )
    alice = AuthContext(
        principal="alice",
        canonical_principal="user:alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="channel",
        interactivity=None,
        enforcement_enabled=True,
    )

    current = get_current_value(
        conn, "Shared", "status", auth_context=alice
    )

    assert current is not None
    assert current.value == "alice-value"
    assert [fact.value for fact in get_history(
        conn, "Shared", "status", auth_context=alice
    )] == ["alice-value"]
    assert detect_contradictions(
        conn, subject="Shared", auth_context=alice
    ) == []


def test_read_scope_uses_canonical_principal_for_owner_match() -> None:
    from mimir.models import AuthContext

    auth = AuthContext(
        principal="slack-U1",
        canonical_principal="user:alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C1",
        interactivity=None,
    )

    scope = get_authorization_scope(auth)

    assert scope.principal == "user:alice"


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


def test_service_owner_grant_uses_prefixed_canonical_identity(
    conn: sqlite3.Connection,
) -> None:
    owned = _store(
        conn,
        "service-owned",
        owner="service:external-reader",
        visibility="service",
    )
    scope = AuthorizationScope(
        principal="external-reader",
        is_service=True,
        service_canonical="external-reader",
    )

    where, params = authorization_predicate(scope, table="a")
    readable_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT a.id FROM atoms a WHERE {where}", params,
        ).fetchall()
    }

    assert owned in readable_ids
    assert "service:external-reader" in params


def test_unauthorized_candidates_are_removed_before_rrf_and_access(conn: sqlite3.Connection) -> None:
    hidden = _store(conn, "hidden query term", owner="user:bob", visibility="private")
    visible = _store(conn, "visible query term", owner="user:alice", visibility="private")
    auth_context = AuthContext(
        principal="alice",
        canonical_principal="user:alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id=None,
        interactivity=None,
        enforcement_enabled=True,
    )

    result = recall(
        conn,
        "query term",
        query_embed_fn=lambda _q: [1.0, 0.0, 0.0, 0.0],
        faiss_search_fn=lambda _emb, _k: [(hidden, 0.99), (visible, 0.8)],
        fts_search_fn=lambda _q, _k: [(hidden, 10.0), (visible, 9.0)],
        triple_search_fn=lambda _emb, _k: [(hidden, 0.95), (visible, 0.7)],
        auth_context=auth_context,
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


def test_shadow_recall_preserves_unrestricted_pathway_ranks(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = _store(conn, "hidden query term", owner="user:bob", visibility="private")
    visible = _store(conn, "visible query term", owner="user:alice", visibility="private")
    monkeypatch.setattr("mimir.event_logger.log_event_sync", lambda *_args, **_kwargs: None)
    auth_context = AuthContext(
        principal="alice",
        canonical_principal="user:alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id=None,
        interactivity=None,
    )

    result = recall(
        conn,
        "query term",
        query_embed_fn=lambda _q: [1.0, 0.0, 0.0, 0.0],
        faiss_search_fn=lambda _emb, _k: [(hidden, 0.99), (visible, 0.8)],
        fts_search_fn=lambda _q, _k: [(hidden, 10.0), (visible, 9.0)],
        triple_search_fn=lambda _emb, _k: [(hidden, 0.95), (visible, 0.7)],
        auth_context=auth_context,
        fire_access_events=False,
    )

    candidates = result.observations + result.raws
    assert [candidate.atom["id"] for candidate in candidates] == [hidden, visible]
    assert [candidate.semantic_rank for candidate in candidates] == [1, 2]
    assert [candidate.keyword_rank for candidate in candidates] == [1, 2]
    assert [candidate.triple_rank for candidate in candidates] == [1, 2]


def test_shadow_recall_reports_pre_rrf_candidate_exclusions(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = _store(conn, "hidden query term", owner="user:bob", visibility="private")
    visible = _store(conn, "visible query term", owner="user:alice", visibility="private")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "mimir.event_logger.log_event_sync",
        lambda event_type, **payload: events.append((event_type, payload)),
    )
    auth_context = AuthContext(
        principal="alice",
        canonical_principal="user:alice",
        roles=("user",),
        event_ingress="discord",
        trigger="user_message",
        channel_id="discord-C1",
        interactivity=None,
        enforcement_enabled=False,
        policy_version="test-policy",
    )

    result = recall(
        conn,
        "query term",
        query_embed_fn=lambda _q: [1.0, 0.0, 0.0, 0.0],
        faiss_search_fn=lambda _emb, _k: [(hidden, 0.99), (visible, 0.8)],
        fts_search_fn=lambda _q, _k: [(hidden, 10.0), (visible, 9.0)],
        auth_context=auth_context,
        fire_access_events=False,
    )

    assert [
        candidate.atom["id"] for candidate in result.observations + result.raws
    ] == [hidden, visible]
    assert len(events) == 1
    event_type, payload = events[0]
    assert event_type == "saga_read_would_block"
    assert payload["reason"] == "saga_read_policy_would_exclude_candidates"
    assert payload["observation_stage"] == "pre_rrf_candidates"
    assert payload["risk_direction"] == "over_serving"
    assert payload["resource_counts"] == {"recall": 1}
    assert payload["resource_type_counts"] == {"recall": {"atom": 1}}
    assert payload["principal"] == "user:alice"
    assert payload["trigger"] == "user_message"
    assert payload["event_ingress"] == "discord"
    assert payload["aggregation"] == "one_event_per_read_operation"
    assert payload["sampling"] == "none"
    serialized = repr(payload)
    assert hidden not in serialized
    assert visible not in serialized


def test_shadow_read_events_aggregate_all_surfaces_once_per_turn(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    hidden = _store(conn, "hidden", owner="user:bob", visibility="private")
    hidden_two = _store(conn, "hidden two", owner="user:bob", visibility="private")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "mimir.event_logger.log_event_sync",
        lambda event_type, **payload: events.append((event_type, payload)),
    )
    auth_context = AuthContext(
        principal="alice",
        canonical_principal="user:alice",
        roles=("user",),
        event_ingress="discord",
        trigger="user_message",
        channel_id="discord-C1",
        interactivity=None,
        enforcement_enabled=False,
    )
    token = set_current_turn(SimpleNamespace(turn_id="turn-shadow-bounded"))
    try:
        query_authorization = SagaReadAuthorization(auth_context, "query")
        query_authorization.observe_selected(conn, "atom", "atoms", [hidden])
        query_authorization.finalize()
        get_authorization = SagaReadAuthorization(auth_context, "get_atoms")
        get_authorization.observe_selected(
            conn, "atom", "atoms", [hidden, hidden_two]
        )
        get_authorization.finalize()
        assert events == []
    finally:
        reset_current_turn(token)

    assert len(events) == 1
    _, payload = events[0]
    assert payload["turn_id"] == "turn-shadow-bounded"
    assert payload["surface"] == "multiple"
    assert payload["surfaces"] == ["get_atoms", "query"]
    assert payload["resource_counts"] == {"get_atoms": 2, "query": 1}
    assert payload["resource_type_counts"] == {
        "get_atoms": {"atom": 2},
        "query": {"atom": 1},
    }
    assert payload["aggregation"] == "one_event_per_turn_by_surface"
    assert payload["sampling"] == "none"


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
    auth_context = AuthContext(
        principal="alice",
        canonical_principal="user:alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="channel",
        interactivity=None,
        enforcement_enabled=True,
    )
    atom_ids = client._session_boundary_atom_pathway_with_conn(
        conn,
        "summary",
        limit=1,
        alpha=0.0,
        atoms_per_session=1,
        auth_context=auth_context,
    )

    assert atom_ids == [first]


@pytest.mark.asyncio
async def test_get_atoms_missing_context_preserves_legacy_unrestricted_read(
    conn: sqlite3.Connection,
) -> None:
    public = _store(conn, "public", owner="other", visibility="public")
    private = _store(conn, "private", owner="user:alice", visibility="private")
    legacy = _store(conn, "legacy", owner="legacy_admin", visibility="legacy_admin")
    client = SagaStore(conn=conn, embedding_dim=4)

    payload = await client.get_atoms([public, private, legacy])

    assert [atom["id"] for atom in payload["atoms"]] == [public, private, legacy]
    assert payload["missing"] == []


@pytest.mark.asyncio
async def test_get_atoms_default_and_false_context_preserve_exact_order(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _store(conn, "first", owner="user:bob", visibility="private")
    second = _store(conn, "second", owner="legacy_admin", visibility="legacy_admin")
    default_context = AuthContext(
        principal="alice",
        canonical_principal="user:alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id=None,
        interactivity=None,
    )
    monkeypatch.setattr(
        "mimir.event_logger.log_event_sync", lambda *_args, **_kwargs: None
    )
    client = SagaStore(conn=conn, embedding_dim=4)

    for auth_context in (
        default_context,
        replace(default_context, enforcement_enabled=False),
    ):
        payload = await client.get_atoms([second, first], auth_context=auth_context)
        assert [atom["id"] for atom in payload["atoms"]] == [second, first]
        assert payload["missing"] == []


@pytest.mark.parametrize(
    ("trigger", "principal"),
    [
        ("scheduled_tick", "scheduler"),
        ("saga_session_end", "synthesis"),
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


@pytest.mark.parametrize(
    ("event", "expected_canonical"),
    [
        (
            AgentEvent(
                trigger="poller",
                channel_id="poller:new-feed",
                service_principal="poller:new-feed",
                service_authority=build_trigger_service_principal(
                    canonical="poller:new-feed",
                    trigger="poller",
                    profile="custom",
                    tier=CapabilityTier.SCOPE_CONTAINED,
                    capabilities=(),
                    creation_path="test",
                ),
            ),
            "poller:new-feed",
        ),
        (
            AgentEvent(
                trigger="scheduled_tick",
                channel_id="scheduler:heartbeat",
                service_principal="heartbeat",
                service_authority=builtin_trigger_service_principal(
                    "heartbeat", Path("."),
                ),
            ),
            "heartbeat",
        ),
        (
            AgentEvent(
                trigger="upgrade",
                channel_id="upgrade:test",
                service_principal="system",
            ),
            "system",
        ),
    ],
)
def test_trusted_service_without_corpus_declaration_is_narrow(
    conn: sqlite3.Connection,
    event: AgentEvent,
    expected_canonical: str,
) -> None:
    public = _store(conn, "public", owner="other", visibility="public")
    owned = _store(
        conn,
        "owned",
        owner=f"service:{expected_canonical}",
        visibility="service",
    )
    domain = _store(
        conn,
        "domain",
        owner="service:writer",
        visibility="service",
        domain="poller_payload",
    )
    foreign = _store(
        conn,
        "foreign",
        owner="user:alice",
        visibility="private",
        domain="private",
    )
    scope = get_authorization_scope(create_auth_context(event, enforce=True))

    assert scope.is_service is True
    assert scope.is_platform_service is False
    assert scope.service_canonical == expected_canonical
    where, params = authorization_predicate(scope, table="a")
    readable_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT a.id FROM atoms a WHERE {where}", params,
        ).fetchall()
    }
    assert public in readable_ids
    assert owned in readable_ids
    if "poller_payload" in scope.readable_domains:
        assert domain in readable_ids
    else:
        assert domain not in readable_ids
    assert foreign not in readable_ids


def test_full_corpus_scope_follows_declared_principal_not_poller_trigger() -> None:
    authority = build_trigger_service_principal(
        canonical="poller:corpus-maintenance",
        trigger="poller",
        profile="custom",
        tier=CapabilityTier.SCOPE_CONTAINED,
        capabilities=(),
        saga_full_corpus_read=True,
        creation_path="test",
    )
    auth_context = create_auth_context(
        AgentEvent(
            trigger="poller",
            channel_id="poller:corpus-maintenance",
            service_principal=authority.canonical,
            service_authority=authority,
        ),
        enforce=True,
    )

    scope = get_authorization_scope(auth_context)

    assert scope.is_service is True
    assert scope.is_platform_service is True


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
        (atom_id, "test-session", datetime.now(UTC).isoformat(), source),
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
        enforcement_enabled=True,
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
        enforcement_enabled=True,
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
async def test_most_retrieved_atoms_no_auth_preserves_unrestricted_results(
    conn: sqlite3.Connection,
) -> None:
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
    assert private in result_ids


@pytest.mark.asyncio
async def test_get_atoms_true_enforcement_filters_private_rows(
    conn: sqlite3.Connection,
) -> None:
    public = _store(conn, "public", owner="other", visibility="public")
    private = _store(conn, "private", owner="user:bob", visibility="private")
    auth_context = AuthContext(
        principal="alice",
        canonical_principal="user:alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id=None,
        interactivity=None,
        enforcement_enabled=True,
    )

    payload = await SagaStore(conn=conn, embedding_dim=4).get_atoms(
        [public, private], auth_context=auth_context
    )

    assert [atom["id"] for atom in payload["atoms"]] == [public]
    assert payload["missing"] == [private]


@pytest.mark.asyncio
async def test_query_shadow_read_emits_one_bounded_non_sensitive_event(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = _store(
        conn, "shadow-only-memory", owner="user:bob", visibility="private"
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "mimir.event_logger.log_event_sync",
        lambda event_type, **payload: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        "mimir.saga.client._query_embed_sync",
        lambda _query: [1.0, 0.0, 0.0, 0.0],
    )
    auth_context = AuthContext(
        principal="alice",
        canonical_principal="user:alice",
        roles=("user",),
        event_ingress="test",
        trigger="user_message",
        channel_id="sensitive-channel",
        interactivity=None,
        enforcement_enabled=False,
    )
    client = SagaStore(
        conn=conn, embedding_dim=4, include_triples_in_response=False
    )

    result = await client.query(
        "shadow-only-memory",
        top_k=5,
        enable_contextual_rewrite=False,
        enable_session_boundary_rrf=False,
        extra_atom_ranked_pathways={"repeat": [hidden, hidden]},
        auth_context=auth_context,
    )

    returned = result["observations"] + result["raws"]
    assert [item["id"] for item in returned] == [hidden]
    assert len(events) == 1
    event_type, payload = events[0]
    assert event_type == "saga_read_would_block"
    assert payload["surface"] == "query"
    assert payload["allowed"] is True
    assert payload["status"] == "would_block"
    assert payload["enforcement_enabled"] is False
    assert payload["is_shadow_decision"] is True
    assert payload["would_block"] is True
    assert payload["resource_counts"] == {"query": 1}
    assert payload["resource_type_counts"] == {"query": {"atom": 1}}
    serialized = repr(payload).lower()
    for sensitive in (hidden.lower(), "shadow-only-memory", "sensitive-channel"):
        assert sensitive not in serialized
    for forbidden_key in ("query", "session_id", "channel_id", "domain"):
        assert forbidden_key not in payload


@pytest.mark.asyncio
async def test_matching_owner_shadow_read_emits_no_event(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = _store(conn, "owned", owner="user:alice", visibility="private")
    events: list[dict] = []
    monkeypatch.setattr(
        "mimir.event_logger.log_event_sync",
        lambda _event_type, **payload: events.append(payload),
    )
    auth_context = AuthContext(
        principal="alice",
        canonical_principal="user:alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id=None,
        interactivity=None,
    )

    payload = await SagaStore(conn=conn, embedding_dim=4).get_atoms(
        [owned], auth_context=auth_context
    )

    assert [atom["id"] for atom in payload["atoms"]] == [owned]
    assert events == []


@pytest.mark.asyncio
async def test_shadow_event_logger_failure_does_not_change_read(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = _store(conn, "hidden", owner="user:bob", visibility="private")

    def fail_logger(_event_type: str, **_payload) -> None:
        raise OSError("logger unavailable")

    monkeypatch.setattr("mimir.event_logger.log_event_sync", fail_logger)

    payload = await SagaStore(conn=conn, embedding_dim=4).get_atoms([hidden])

    assert [atom["id"] for atom in payload["atoms"]] == [hidden]


@pytest.mark.asyncio
async def test_shadow_policy_probe_failure_is_reported_without_changing_read(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = _store(conn, "hidden", owner="user:bob", visibility="private")
    events: list[tuple[str, dict]] = []

    def fail_probe(_self, _table: str):
        raise sqlite3.OperationalError("strict probe unavailable")

    monkeypatch.setattr(SagaReadAuthorization, "strict_predicate", fail_probe)
    monkeypatch.setattr(
        "mimir.event_logger.log_event_sync",
        lambda event_type, **payload: events.append((event_type, payload)),
    )

    payload = await SagaStore(conn=conn, embedding_dim=4).get_atoms([hidden])

    assert [atom["id"] for atom in payload["atoms"]] == [hidden]
    assert len(events) == 1
    event_type, event = events[0]
    assert event_type == "saga_read_would_block"
    assert event["status"] == "probe_failed"
    assert event["would_block"] is False
    assert event["resource_count"] == 0
    assert event["probe_failure_count"] == 1
    assert event["probe_failure_counts"] == {"get_atoms": 1}
