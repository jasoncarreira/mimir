"""Authorization regressions for destructive SAGA mutations (chainlink #885)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from mimir.access_control import ServicePrincipal, _TRUSTED_SERVICE_PRINCIPALS
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


def _service_auth(trigger: str, canonical: str) -> AuthContext:
    return AuthContext(
        principal=canonical,
        canonical_principal=canonical,
        roles=(),
        event_ingress=None,
        trigger=trigger,
        channel_id=None,
        interactivity=None,
        is_service=True,
    )


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
        auth_context=AuthorizationScope(is_admin=True),
    )

    result = await client.forget(dry_run=False, auth_context=_auth("alice"))

    assert result["tombstoned_count"] == 1
    assert (
        retrieve_by_entity(
            conn,
            "Alice",
            auth_context=AuthorizationScope(is_admin=True),
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
