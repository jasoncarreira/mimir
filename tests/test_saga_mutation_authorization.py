"""Authorization regressions for destructive SAGA mutations (chainlink #885)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from mimir.access_control import (
    ServicePrincipal,
    ToolRegistry,
    _TRUSTED_SERVICE_PRINCIPALS,
    can_write_saga,
    get_provenance_from_auth_context,
    get_service_principal,
)
from mimir.models import AuthContext
from mimir.saga.client import SagaStore
from mimir.saga.ownership import AuthorizationScope
from mimir.saga.triples import retrieve_by_entity, store_triples


def _auth(principal: str, *, admin: bool = False) -> AuthContext:
    return AuthContext(
        principal=principal,
        canonical_principal=principal,
        roles=("admin",) if admin else ("user",),
        event_ingress="test",
        trigger="user_message",
        channel_id="test-channel",
        interactivity=None,
    )


def _service_auth(
    trigger: str,
    canonical: str,
    *,
    channel_id: str | None = None,
    saga_session_id: str | None = None,
) -> AuthContext:
    return AuthContext(
        principal=canonical,
        canonical_principal=canonical,
        roles=(),
        event_ingress=None,
        trigger=trigger,
        channel_id=channel_id,
        interactivity=None,
        is_service=True,
        saga_session_id=saga_session_id,
    )


_SAGA_MUTATIONS = (
    "memory_store",
    "saga_feedback",
    "saga_mark_contributions",
    "saga_end_session",
    "saga_record_skill_learning",
    "saga_forget",
)


@pytest.mark.parametrize(
    ("trigger", "allowed_operations"),
    [
        ("scheduled_tick", {"saga_forget"}),
        ("poller", set()),
        (
            "saga_session_end",
            {
                "memory_store",
                "saga_feedback",
                "saga_mark_contributions",
                "saga_end_session",
                "saga_record_skill_learning",
            },
        ),
        ("upgrade", set()),
    ],
)
@pytest.mark.parametrize("operation", _SAGA_MUTATIONS)
def test_saga_mutation_service_capability_matrix_matches_at_both_guards(
    trigger: str,
    allowed_operations: set[str],
    operation: str,
) -> None:
    service = get_service_principal(trigger)
    assert service is not None
    auth_context = _service_auth(trigger, service.canonical)
    expected = operation in allowed_operations

    middleware = ToolRegistry().authorize_tool(
        operation,
        auth_context,
        enforce=True,
    )

    assert middleware.allowed is expected
    assert can_write_saga(auth_context, operation) is expected
    if not expected:
        assert middleware.reason == "admin_required"


@pytest.mark.parametrize("operation", _SAGA_MUTATIONS)
def test_saga_mutation_admin_allowed_and_regular_user_denied(operation: str) -> None:
    assert can_write_saga(_auth("admin", admin=True), operation) is True
    assert can_write_saga(_auth("alice"), operation) is False


def test_saga_mutation_service_requires_declared_sink() -> None:
    trigger = "test_missing_saga_sink"
    _TRUSTED_SERVICE_PRINCIPALS[trigger] = ServicePrincipal(
        canonical="missing-saga-sink",
        trigger=trigger,
        capabilities=("memory_store",),
        readable_domains=("saga",),
        sink_destinations=(),
        creation_path="test",
    )
    try:
        auth_context = _service_auth(trigger, "missing-saga-sink")
        middleware = ToolRegistry().authorize_tool(
            "memory_store", auth_context, enforce=True
        )

        assert middleware.allowed is False
        assert middleware.reason == "admin_required"
        assert can_write_saga(auth_context, "memory_store") is False
    finally:
        _TRUSTED_SERVICE_PRINCIPALS.pop(trigger, None)


def _session_row(client: SagaStore, session_id: str):
    return client._ensure_conn().execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()


@pytest.mark.asyncio
async def test_end_session_retry_is_idempotent_and_cannot_rebind(
    client: SagaStore,
) -> None:
    session_id = "victim-session"
    auth = _service_auth(
        "saga_session_end",
        "synthesis",
        channel_id="victim-channel",
        saga_session_id=session_id,
    )
    common = {
        "channel_id": "victim-channel",
        "owner_principal": "legacy_admin",
        "origin_channel": "victim-channel",
        "origin_domain": "saga",
        "visibility": "legacy_admin",
        "provenance": {},
        "auth_context": auth,
    }
    first = await client.end_session(session_id, "victim summary", **common)
    before = _session_row(client, session_id)

    retry = await client.end_session(session_id, "attacker replacement", **common)
    assert first["session_summary_written"] is True
    assert retry["session_summary_written"] is False
    assert _session_row(client, session_id) == before

    with pytest.raises(PermissionError, match="session write denied"):
        await client.end_session(
            session_id,
            "rebound summary",
            **{
                **common,
                "channel_id": "attacker-channel",
                "origin_channel": "attacker-channel",
            },
        )
    assert _session_row(client, session_id) == before


@pytest.mark.asyncio
async def test_end_session_active_carrier_prevents_first_writer_preemption(
    client: SagaStore,
) -> None:
    attacker_auth = _service_auth(
        "saga_session_end",
        "synthesis",
        channel_id="attacker-channel",
        saga_session_id="attacker-session",
    )
    victim_id = "not-yet-reflected-victim"

    with pytest.raises(PermissionError, match="session write denied"):
        await client.end_session(
            victim_id,
            "preempted summary",
            channel_id="attacker-channel",
            owner_principal="legacy_admin",
            origin_channel="attacker-channel",
            origin_domain="saga",
            visibility="legacy_admin",
            auth_context=attacker_auth,
        )
    assert _session_row(client, victim_id) is None

    victim_auth = _service_auth(
        "saga_session_end",
        "synthesis",
        channel_id="victim-channel",
        saga_session_id=victim_id,
    )
    result = await client.end_session(
        victim_id,
        "legitimate summary",
        channel_id="victim-channel",
        owner_principal="legacy_admin",
        origin_channel="victim-channel",
        origin_domain="saga",
        visibility="legacy_admin",
        auth_context=victim_auth,
    )
    assert result["session_summary_written"] is True
    assert _session_row(client, victim_id) is not None


@pytest.mark.parametrize("origin_domain", ["discord", "slack"])
@pytest.mark.asyncio
async def test_end_session_accepts_inherited_user_acl_and_owner_can_read(
    client: SagaStore,
    origin_domain: str,
) -> None:
    session_id = f"{origin_domain}-user-session"
    channel_id = f"{origin_domain}-private"
    auth = _service_auth(
        "saga_session_end",
        "synthesis",
        channel_id=channel_id,
        saga_session_id=session_id,
    )

    result = await client.end_session(
        session_id,
        "user-owned summary",
        channel_id=channel_id,
        owner_principal="alice",
        origin_channel=channel_id,
        origin_domain=origin_domain,
        visibility="private",
        provenance={"created_by": "alice", "derived_by": "service:synthesis"},
        auth_context=auth,
    )

    assert result["session_summary_written"] is True
    row = client._ensure_conn().execute(
        "SELECT owner_principal, origin_domain FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    assert row == ("alice", origin_domain)
    boundaries = await client.recent_session_boundaries(
        channel_id=channel_id,
        auth_context=_auth("alice"),
    )
    assert [boundary["session_id"] for boundary in boundaries] == [session_id]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SagaStore:
    class StubProvider:
        def embed(self, text, *, input_type="passage"):
            h = abs(hash(text)) % 1000
            return [float(h % 7), float(h % 11), float(h % 13), float(h % 17)]

        def dimensions(self):
            return 4

    monkeypatch.setattr("mimir.saga.embeddings.get_provider", lambda: StubProvider())
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: (
            lambda section, key, default=None: {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        ),
    )
    return SagaStore(db_path=tmp_path / "mutation-auth.saga.db", embedding_dim=4)


async def _atom(
    client: SagaStore,
    content: str,
    *,
    owner: str,
    domain: str | None = "saga",
    visibility: str = "private",
) -> str:
    result = await client.store(
        content,
        owner_principal=owner,
        origin_domain=domain,
        visibility=visibility,
        provenance={"created_by": owner},
    )
    return result["atom_id"]


def _feedback_count(client: SagaStore, atom_id: str) -> int:
    return (
        client._ensure_conn()
        .execute(
            "SELECT COUNT(*) FROM access_events WHERE atom_id = ? "
            "AND source IN ('feedback_positive', 'feedback_negative')",
            (atom_id,),
        )
        .fetchone()[0]
    )


@pytest.mark.asyncio
async def test_missing_auth_fails_closed(client: SagaStore):
    atom_id = await _atom(client, "missing authority", owner="alice")

    result = await client.feedback([atom_id], "reply", feedback="positive")

    assert result == {"marked": 0, "total": 1, "authorized": 0}
    assert _feedback_count(client, atom_id) == 0


@pytest.mark.asyncio
async def test_regular_principal_denied_other_legacy_and_service_atoms(
    client: SagaStore,
):
    own = await _atom(client, "alice own", owner="alice")
    other = await _atom(client, "bob private", owner="bob")
    legacy = await _atom(
        client,
        "legacy",
        owner="legacy_admin",
        domain=None,
        visibility="legacy_admin",
    )
    service = await _atom(
        client,
        "service",
        owner="service:synthesis",
        visibility="service",
    )

    for atom_id in (other, legacy, service):
        result = await client.feedback(
            [atom_id],
            "reply",
            feedback="positive",
            auth_context=_auth("alice"),
        )
        assert result == {"marked": 0, "total": 1, "authorized": 0}

    allowed = await client.feedback(
        [own],
        "reply",
        feedback="positive",
        auth_context=_auth("alice"),
    )
    assert allowed["marked"] == 1
    assert [
        _feedback_count(client, atom_id) for atom_id in (own, other, legacy, service)
    ] == [1, 0, 0, 0]


@pytest.mark.asyncio
async def test_mixed_and_forged_feedback_batches_fail_atomically(client: SagaStore):
    own = await _atom(client, "alice mixed", owner="alice")
    other = await _atom(client, "bob mixed", owner="bob")

    for atom_ids in ([own, other], [own, "forged-id"]):
        result = await client.feedback(
            atom_ids,
            "reply",
            feedback="positive",
            auth_context=_auth("alice"),
        )
        assert result["marked"] == 0
        assert result["authorized"] == 0

    assert _feedback_count(client, own) == 0


@pytest.mark.asyncio
async def test_mixed_outcome_batch_fails_atomically(client: SagaStore):
    own = await _atom(client, "alice outcome", owner="alice")
    other = await _atom(client, "bob outcome", owner="bob")

    result = await client.outcome(
        [own, other],
        "negative",
        auth_context=_auth("alice"),
    )

    assert result == {
        "marked": 0,
        "total": 2,
        "signal": "negative",
        "authorized": 0,
    }
    assert _feedback_count(client, own) == 0


@pytest.mark.asyncio
async def test_mixed_contribution_batch_fails_before_access_events(client: SagaStore):
    own = await _atom(client, "unique alice contribution", owner="alice")
    other = await _atom(client, "unique bob contribution", owner="bob")

    result = await client.mark_contributions(
        [
            {"id": own, "content": "unique alice contribution"},
            {"id": other, "content": "unique bob contribution"},
        ],
        "unique alice contribution unique bob contribution",
        auth_context=_auth("alice"),
    )

    assert result["contributed_count"] == 0
    assert result["authorized"] == 0
    assert _feedback_count(client, own) == 0


@pytest.mark.asyncio
async def test_forget_preview_contains_only_authorized_ids(client: SagaStore):
    own = await _atom(client, "alice preview", owner="alice")
    await _atom(client, "bob preview", owner="bob")
    await _atom(
        client,
        "legacy preview",
        owner="legacy_admin",
        domain=None,
        visibility="legacy_admin",
    )

    result = await client.forget(dry_run=True, auth_context=_auth("alice"))

    assert result["preview_ids"] == [own]
    assert result["dry_run"] is True


@pytest.mark.asyncio
async def test_forget_denies_unauthorized_dependent_observation_before_write(
    client: SagaStore,
):
    own = await _atom(client, "alice evidence", owner="alice")
    observation = await _atom(client, "bob observation", owner="bob")
    conn = client._ensure_conn()
    now = "2026-07-16T00:00:00+00:00"
    conn.execute(
        "UPDATE atoms SET memory_type = 'observation', is_pinned = 1 WHERE id = ?",
        (observation,),
    )
    conn.execute(
        "INSERT INTO atom_relations "
        "(source_id, target_id, relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        (observation, own, now),
    )
    conn.execute(
        "INSERT INTO observations_metadata "
        "(atom_id, evidence_count, trend, consolidated_at) VALUES (?, 1, 'stable', ?)",
        (observation, now),
    )
    conn.commit()

    result = await client.forget(dry_run=False, auth_context=_auth("alice"))

    assert result == {"tombstoned_count": 0, "preview_ids": [], "dry_run": False}
    assert (
        conn.execute("SELECT tombstoned FROM atoms WHERE id = ?", (own,)).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT evidence_count FROM observations_metadata WHERE atom_id = ?",
            (observation,),
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_forget_updates_only_authorized_dependents_and_index(
    client: SagaStore, monkeypatch
):
    own = await _atom(client, "alice forget", owner="alice")
    other = await _atom(client, "bob survives", owner="bob")
    observation = await _atom(client, "alice observation", owner="alice")
    conn = client._ensure_conn()
    now = "2026-07-16T00:00:00+00:00"
    conn.execute(
        "UPDATE atoms SET memory_type = 'observation', is_pinned = 1 WHERE id = ?",
        (observation,),
    )
    conn.executemany(
        "INSERT INTO atom_relations "
        "(source_id, target_id, relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        [(observation, own, now), (observation, other, now)],
    )
    conn.execute(
        "INSERT INTO observations_metadata "
        "(atom_id, evidence_count, trend, consolidated_at) VALUES (?, 2, 'stable', ?)",
        (observation, now),
    )
    conn.commit()

    class RecordingIndex:
        built = True

        def __init__(self):
            self.removed: list[str] = []

        def remove(self, atom_id):
            self.removed.append(atom_id)

    index = RecordingIndex()
    client._index = index
    monkeypatch.setattr(client, "_rebuild_index_if_needed", lambda _conn: None)

    result = await client.forget(dry_run=False, auth_context=_auth("alice"))

    assert result["tombstoned_count"] == 1
    assert index.removed == [own]
    assert (
        conn.execute("SELECT tombstoned FROM atoms WHERE id = ?", (other,)).fetchone()[
            0
        ]
        == 0
    )
    assert (
        conn.execute(
            "SELECT evidence_count FROM observations_metadata WHERE atom_id = ?",
            (observation,),
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_forget_rolls_back_tombstones_when_dependent_refresh_fails(
    client: SagaStore,
    monkeypatch: pytest.MonkeyPatch,
):
    own = await _atom(client, "atomic evidence", owner="alice")
    observation = await _atom(client, "atomic observation", owner="alice")
    conn = client._ensure_conn()
    now = "2026-07-16T00:00:00+00:00"
    conn.execute(
        "UPDATE atoms SET memory_type = 'observation', is_pinned = 1 WHERE id = ?",
        (observation,),
    )
    conn.execute(
        "INSERT INTO atom_relations "
        "(source_id, target_id, relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
        (observation, own, now),
    )
    conn.execute(
        "INSERT INTO observations_metadata "
        "(atom_id, evidence_count, trend, consolidated_at) VALUES (?, 1, 'stable', ?)",
        (observation, now),
    )
    conn.commit()

    def fail_refresh(*_args, **_kwargs):
        raise RuntimeError("injected dependent refresh failure")

    forget_module = importlib.import_module("mimir.saga.forget")
    monkeypatch.setattr(forget_module, "refresh_trend", fail_refresh)

    with pytest.raises(RuntimeError, match="injected dependent refresh failure"):
        await client.forget(dry_run=False, auth_context=_auth("alice"))

    assert (
        conn.execute(
            "SELECT tombstoned FROM atoms WHERE id = ?",
            (own,),
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_forgotten_source_atom_hides_derived_triples(client: SagaStore):
    atom_id = await _atom(client, "Alice works at Acme", owner="alice")
    conn = client._ensure_conn()
    store_triples(
        conn,
        [{"subject": "Alice", "predicate": "works_at", "object": "Acme"}],
        source_atom_id=atom_id,
        evidence_ids=[atom_id],
    )
    conn.commit()
    assert retrieve_by_entity(
        conn,
        "Alice",
        auth_context=_auth("admin", admin=True),
    )

    result = await client.forget(dry_run=False, auth_context=_auth("alice"))

    assert result["tombstoned_count"] == 1
    assert (
        retrieve_by_entity(
            conn,
            "Alice",
            auth_context=_auth("admin", admin=True),
        )
        == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        AuthorizationScope(principal="victim"),
        AuthorizationScope(is_admin=True),
        type("ForgedAdmin", (), {"roles": ("admin",)})(),
    ],
)
async def test_forgeable_read_scopes_cannot_authorize_mutation(
    client: SagaStore,
    scope: object,
):
    atom_id = await _atom(client, "victim atom", owner="victim")

    result = await client.feedback(
        [atom_id],
        "reply",
        feedback="positive",
        auth_context=scope,
    )

    assert result == {"marked": 0, "total": 1, "authorized": 0}


@pytest.mark.asyncio
async def test_platform_service_full_read_does_not_widen_mutation_scope(
    client: SagaStore,
):
    """A platform service still cannot mutate an atom outside its domains."""
    denied = await _atom(client, "tenant b", owner="owner-b", domain="tenant:b")
    trigger = "test_platform_saga_service"
    original = _TRUSTED_SERVICE_PRINCIPALS.get(trigger)
    _TRUSTED_SERVICE_PRINCIPALS[trigger] = ServicePrincipal(
        canonical="test-platform-service",
        trigger=trigger,
        capabilities=("saga_feedback",),
        readable_domains=("tenant:a",),
        sink_destinations=("saga",),
        creation_path="test",
    )
    auth_context = _service_auth(trigger, "test-platform-service")
    try:
        result = await client.feedback(
            [denied],
            "reply",
            feedback="positive",
            auth_context=auth_context,
        )
    finally:
        if original is None:
            _TRUSTED_SERVICE_PRINCIPALS.pop(trigger, None)
        else:
            _TRUSTED_SERVICE_PRINCIPALS[trigger] = original

    assert result == {"marked": 0, "total": 1, "authorized": 0}


@pytest.mark.asyncio
async def test_trusted_service_requires_capability_and_readable_domain(
    client: SagaStore,
):
    allowed = await _atom(client, "tenant a", owner="owner-a", domain="tenant:a")
    denied = await _atom(client, "tenant b", owner="owner-b", domain="tenant:b")
    trigger = "test_saga_service"
    original = _TRUSTED_SERVICE_PRINCIPALS.get(trigger)
    _TRUSTED_SERVICE_PRINCIPALS[trigger] = ServicePrincipal(
        canonical="test-service",
        trigger=trigger,
        capabilities=("saga_feedback",),
        readable_domains=("tenant:a",),
        sink_destinations=("saga",),
        creation_path="test",
    )
    auth_context = _service_auth(trigger, "test-service")
    try:
        allowed_result = await client.feedback(
            [allowed],
            "reply",
            feedback="positive",
            auth_context=auth_context,
        )
        denied_result = await client.feedback(
            [denied],
            "reply",
            feedback="positive",
            auth_context=auth_context,
        )
        forget_result = await client.forget(dry_run=True, auth_context=auth_context)
    finally:
        if original is None:
            _TRUSTED_SERVICE_PRINCIPALS.pop(trigger, None)
        else:
            _TRUSTED_SERVICE_PRINCIPALS[trigger] = original

    assert allowed_result["marked"] == 1
    assert denied_result == {"marked": 0, "total": 1, "authorized": 0}
    assert forget_result["preview_ids"] == []


@pytest.mark.asyncio
async def test_non_platform_service_can_read_and_forget_own_domainless_write(
    client: SagaStore,
) -> None:
    trigger = "test_external_saga_service"
    canonical = "external-saga-service"
    original = _TRUSTED_SERVICE_PRINCIPALS.get(trigger)
    _TRUSTED_SERVICE_PRINCIPALS[trigger] = ServicePrincipal(
        canonical=canonical,
        trigger=trigger,
        capabilities=("saga_forget",),
        readable_domains=("tenant:a",),
        sink_destinations=("saga",),
        creation_path="test",
    )
    auth_context = _service_auth(trigger, canonical)
    try:
        provenance = get_provenance_from_auth_context(auth_context)
        stored = await client.store(
            "external service owned memory",
            owner_principal=provenance["created_by"],
            origin_domain=None,
            visibility="service",
            provenance=provenance,
        )
        atom_id = stored["atom_id"]

        read_result = await client.get_atoms([atom_id], auth_context=auth_context)
        forget_preview = await client.forget(
            dry_run=True,
            auth_context=auth_context,
        )
        forget_result = await client.forget(
            dry_run=False,
            auth_context=auth_context,
        )
    finally:
        if original is None:
            _TRUSTED_SERVICE_PRINCIPALS.pop(trigger, None)
        else:
            _TRUSTED_SERVICE_PRINCIPALS[trigger] = original

    row = client._ensure_conn().execute(
        "SELECT owner_principal, origin_domain, tombstoned FROM atoms WHERE id = ?",
        (atom_id,),
    ).fetchone()
    assert provenance["created_by"] == f"service:{canonical}"
    assert [atom["id"] for atom in read_result["atoms"]] == [atom_id]
    assert forget_preview["preview_ids"] == [atom_id]
    assert forget_result["tombstoned_count"] == 1
    assert row == (f"service:{canonical}", None, 1)


@pytest.mark.parametrize(
    "sentinel_principal",
    ["legacy_admin", "service", "system"],
)
def test_sentinel_principal_cannot_mutation_owner_match(
    client: SagaStore,
    sentinel_principal: str,
) -> None:
    """Reserved sentinel principals cannot use owner-match to mutate rows.

    A caller whose principal is a reserved sentinel value (legacy_admin, service,
    system) should NOT be able to mutate rows via the owner-match grant.
    This prevents a regular user who happens to have a sentinel principal from
    mutating the legacy/default-owned corpus.
    """
    from mimir.saga.client import _saga_mutation_scope

    auth_context = _auth(sentinel_principal)
    scope = _saga_mutation_scope(auth_context, "saga_forget")

    assert scope is None
