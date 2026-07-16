"""Unit tests for SAGA ownership value objects and greenfield constraints."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mimir.saga.ownership import (
    Ownership,
    Visibility,
    intersect_acl,
    is_user_accessible,
)


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
    domain: str = "tenant:one",
    visibility: str = "public",
    provenance: dict | None = None,
) -> Ownership:
    return Ownership(
        owner_principal=owner,
        origin_channel="channel:one",
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
    assert result.origin_domain == "tenant:one"
    assert result.visibility == Visibility.PRIVATE
    assert result.provenance == {"a": 1, "b": 2}


@pytest.mark.parametrize(
    "acls",
    [
        [_owned(owner="user:one"), _owned(owner="user:two")],
        [_owned(domain="tenant:one"), _owned(domain="tenant:two")],
        [_owned(owner="legacy_admin")],
        [_owned(provenance={})],
    ],
    ids=["mixed-owner", "mixed-domain", "legacy-source", "missing-provenance"],
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
