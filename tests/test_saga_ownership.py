"""Unit tests for SAGA ownership value objects and greenfield constraints."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.saga.ownership import (
    Ownership,
    Visibility,
    intersect_acl,
    is_user_accessible,
)


def test_shadow_turn_overflow_reports_aggregate_loss(monkeypatch) -> None:
    from mimir import _context
    from mimir.saga import ownership

    accumulators = {}
    events = []
    turn = SimpleNamespace(turn_id="")
    monkeypatch.setattr(ownership, "_SHADOW_TURN_ACCUMULATORS", accumulators)
    monkeypatch.setattr(_context, "get_current_turn", lambda: turn)
    monkeypatch.setattr(
        ownership, "_emit_saga_event",
        lambda event_type, **payload: events.append((event_type, payload)),
    )
    authorization = ownership.SagaReadAuthorization(None, "search")
    authorization.observe_would_deny("atoms", {"sensitive-id"})
    for index in range(4097):
        turn.turn_id = f"turn-{index}"
        authorization.finalize()

    assert len(accumulators) == 2049
    assert list(accumulators)[0] == "turn-2048"
    assert events == [("saga_read_shadow_turns_dropped", {
        "dropped_turn_count": 2048,
        "reason": "capacity",
    })]
    ownership.finalize_shadow_read_turn(turn)
    assert events[-1][0] == "saga_read_would_block"
    assert events[-1][1]["resource_count"] == 1
    assert turn.turn_id not in accumulators


def test_shadow_turn_age_evicts_late_finalize_orphan(monkeypatch) -> None:
    from mimir import _context
    from mimir.saga import ownership

    accumulators = {}
    events = []
    clock = [0.0]
    turn = SimpleNamespace(turn_id="late-turn")
    monkeypatch.setattr(ownership, "_SHADOW_TURN_ACCUMULATORS", accumulators)
    monkeypatch.setattr(ownership, "monotonic", lambda: clock[0], raising=False)
    monkeypatch.setattr(_context, "get_current_turn", lambda: turn)
    monkeypatch.setattr(
        ownership, "_emit_saga_event",
        lambda event_type, **payload: events.append((event_type, payload)),
    )
    authorization = ownership.SagaReadAuthorization(None, "search")
    authorization.observe_probe_failure()
    authorization.finalize()
    ownership.finalize_shadow_read_turn(turn)
    authorization.finalize()  # A detached operation finishes after turn teardown.
    assert "late-turn" in accumulators
    events.clear()

    clock[0] = 3600.0
    turn.turn_id = "fresh-turn"
    authorization.finalize()

    assert list(accumulators) == ["fresh-turn"]
    assert events == [("saga_read_shadow_turns_dropped", {
        "dropped_turn_count": 1,
        "reason": "age",
    })]
    ownership.finalize_shadow_read_turn(turn)
    assert events[-1][1]["probe_failure_count"] == 1


@pytest.mark.parametrize(
    ("visibility", "accessible"),
    [
        (Visibility.PUBLIC, True),
        (Visibility.PRIVATE, False),
        (Visibility.SERVICE, False),
        (Visibility.LEGACY_ADMIN, False),
        ("public", True),
        ("private", False),
        ("service", False),
        ("legacy_admin", False),
        ("unknown", False),
    ],
)
def test_is_user_accessible_is_fail_closed(
    visibility: str, accessible: bool
) -> None:
    assert is_user_accessible(visibility) is accessible


@pytest.mark.parametrize("ingress", ["http_event", "http-api", ""])
def test_ingress_carrier_admin_roles_cannot_grant_full_corpus(ingress: str) -> None:
    from mimir.models import AuthContext, TurnInteractivity
    from mimir.saga.ownership import authorization_predicate, get_authorization_scope

    context = AuthContext(
        principal="jason", canonical_principal="jason",
        roles=("admin",), event_ingress=ingress,
        trigger="user_message", channel_id="attacker-chosen",
        interactivity=TurnInteractivity.NON_INTERACTIVE,
    )
    scope = get_authorization_scope(context)

    assert scope.is_admin is False
    assert authorization_predicate(scope) != ("1=1", [])


@pytest.mark.parametrize("service", ["scheduler", "heartbeat", "synthesis"])
def test_registered_service_factory_retains_full_corpus(
    service: str, tmp_path: Path,
) -> None:
    from mimir.access_control import builtin_trigger_service_principal, create_auth_context
    from mimir.models import AgentEvent
    from mimir.saga.ownership import authorization_predicate, get_authorization_scope

    context = create_auth_context(AgentEvent(
        trigger="saga_session_end" if service == "synthesis" else "scheduled_tick",
        channel_id="internal", service_principal=service,
        service_authority=(
            builtin_trigger_service_principal("heartbeat", tmp_path)
            if service == "heartbeat" else None
        ),
    ))
    scope = get_authorization_scope(context)

    assert scope.is_admin is False
    assert scope.is_service is True
    assert scope.service_canonical == service
    assert scope.is_platform_service is True
    assert authorization_predicate(scope) == ("1=1", [])


def test_ownership_to_columns_serializes_deterministic_json() -> None:
    ownership = Ownership(
        owner_principal="user:123",
        visibility=Visibility.PRIVATE,
        provenance={"source": "turn", "nested": {"index": 2}},
    )

    columns = ownership.to_columns()

    assert columns["owner_principal"] == "user:123"
    assert columns["visibility"] == "private"
    assert columns["provenance"] == '{"nested":{"index":2},"source":"turn"}'
    assert json.loads(columns["provenance"]) == ownership.provenance


def test_ownership_instances_do_not_share_default_provenance() -> None:
    first = Ownership()
    second = Ownership()

    first.provenance["source"] = "first"

    assert second.provenance == {}


def _owned(
    *,
    owner: str = "user:123",
    channel: str = "channel:one",
    domain: str = "tenant:one",
    visibility: str = "public",
    provenance: dict | None = None,
) -> Ownership:
    return Ownership(
        owner_principal=owner,
        origin_channel=channel,
        origin_domain=domain,
        visibility=visibility,
        provenance={"atom": "a1"} if provenance is None else provenance,
    )


def test_intersect_acl_same_owner_domain_keeps_common_authority() -> None:
    result = intersect_acl([
        _owned(visibility="public", provenance={"a": 1}),
        _owned(visibility="private", provenance={"b": 2}),
    ])

    assert result.owner_principal == "user:123"
    assert result.origin_channel == "channel:one"
    assert result.origin_domain == "tenant:one"
    assert result.visibility == Visibility.PRIVATE
    assert result.provenance == {"a": 1, "b": 2}


@pytest.mark.parametrize(
    "acls",
    [
        [_owned(owner="user:one"), _owned(owner="user:two")],
        [_owned(domain="tenant:one"), _owned(domain="tenant:two")],
        [_owned(channel="channel:one"), _owned(channel="channel:two")],
        [_owned(owner="legacy_admin")],
        [_owned(provenance={})],
    ],
    ids=[
        "mixed-owner", "mixed-domain", "mixed-channel", "legacy-source",
        "missing-provenance",
    ],
)
def test_intersect_acl_ambiguous_inputs_fail_closed(acls: list[Ownership]) -> None:
    result = intersect_acl(acls)

    assert result == Ownership()


def test_intersect_acl_unknown_visibility_fails_closed() -> None:
    result = intersect_acl([_owned(visibility="unexpected")])

    assert result == Ownership()


def _greenfield_conn() -> sqlite3.Connection:
    schema_path = Path(__file__).parents[1] / "mimir" / "saga" / "schema.sql"
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema_path.read_text())
    return conn


@pytest.mark.parametrize(
    ("table", "seed_sql", "required_column"),
    [
        (
            "atoms",
            "INSERT INTO atoms "
            "(id, content, content_hash, created_at, {column}) "
            "VALUES ('a1', 'content', 'hash', '2024-01-01', NULL)",
            "owner_principal",
        ),
        (
            "atoms",
            "INSERT INTO atoms "
            "(id, content, content_hash, created_at, {column}) "
            "VALUES ('a1', 'content', 'hash', '2024-01-01', NULL)",
            "visibility",
        ),
        (
            "atoms",
            "INSERT INTO atoms "
            "(id, content, content_hash, created_at, {column}) "
            "VALUES ('a1', 'content', 'hash', '2024-01-01', NULL)",
            "provenance",
        ),
        (
            "sessions",
            "INSERT INTO sessions (id, started_at, {column}) "
            "VALUES ('s1', '2024-01-01', NULL)",
            "owner_principal",
        ),
        (
            "sessions",
            "INSERT INTO sessions (id, started_at, {column}) "
            "VALUES ('s1', '2024-01-01', NULL)",
            "visibility",
        ),
        (
            "sessions",
            "INSERT INTO sessions (id, started_at, {column}) "
            "VALUES ('s1', '2024-01-01', NULL)",
            "provenance",
        ),
        (
            "observations_metadata",
            "INSERT INTO observations_metadata "
            "(atom_id, consolidated_at, {column}) "
            "VALUES ('a1', '2024-01-01', NULL)",
            "owner_principal",
        ),
        (
            "observations_metadata",
            "INSERT INTO observations_metadata "
            "(atom_id, consolidated_at, {column}) "
            "VALUES ('a1', '2024-01-01', NULL)",
            "visibility",
        ),
        (
            "observations_metadata",
            "INSERT INTO observations_metadata "
            "(atom_id, consolidated_at, {column}) "
            "VALUES ('a1', '2024-01-01', NULL)",
            "provenance",
        ),
        (
            "triples",
            "INSERT INTO triples "
            "(id, subject, predicate, object, created_at, {column}) "
            "VALUES ('t1', 's', 'p', 'o', '2024-01-01', NULL)",
            "owner_principal",
        ),
        (
            "triples",
            "INSERT INTO triples "
            "(id, subject, predicate, object, created_at, {column}) "
            "VALUES ('t1', 's', 'p', 'o', '2024-01-01', NULL)",
            "visibility",
        ),
        (
            "triples",
            "INSERT INTO triples "
            "(id, subject, predicate, object, created_at, {column}) "
            "VALUES ('t1', 's', 'p', 'o', '2024-01-01', NULL)",
            "provenance",
        ),
    ],
)
def test_greenfield_schema_rejects_null_ownership_fields(
    table: str, seed_sql: str, required_column: str
) -> None:
    conn = _greenfield_conn()
    if table == "observations_metadata":
        conn.execute(
            "INSERT INTO atoms (id, content, content_hash, created_at) "
            "VALUES ('a1', 'content', 'hash', '2024-01-01')"
        )

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        conn.execute(seed_sql.format(column=required_column))


def test_greenfield_world_state_rejects_unknown_visibility() -> None:
    conn = _greenfield_conn()

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            "INSERT INTO world_state "
            "(subject, predicate, value, valid_from, updated_at, visibility) "
            "VALUES ('subject', 'predicate', 'value', '2024-01-01', "
            "'2024-01-01', 'unexpected')"
        )
