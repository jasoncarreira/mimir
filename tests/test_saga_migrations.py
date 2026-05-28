"""Tests for mimir.saga.migrations (chainlink #242, Phase 1).

The migration registry and applier moved out of saga/client.py into a
dedicated module. SagaStore-via-monkeypatch coverage lives in
test_saga_correctness_regressions.py; these tests pin the
module-level surface directly.
"""
from __future__ import annotations

import sqlite3

import pytest

from mimir.saga import migrations as m


class TestModuleSurface:
    def test_current_schema_version_is_int(self) -> None:
        assert isinstance(m.CURRENT_SCHEMA_VERSION, int)
        assert m.CURRENT_SCHEMA_VERSION >= 6

    def test_migrations_dict_is_contiguous_from_2(self) -> None:
        """Adding a v7 migration MUST be the only change needed to bump
        the schema version; tightly contiguous keys make that contract
        explicit."""
        keys = sorted(m.MIGRATIONS.keys())
        assert keys == list(range(2, m.CURRENT_SCHEMA_VERSION + 1)), (
            f"MIGRATIONS keys must be contiguous 2..{m.CURRENT_SCHEMA_VERSION}, "
            f"got {keys}"
        )

    def test_all_migrations_are_non_empty_sql(self) -> None:
        for version, ddl in m.MIGRATIONS.items():
            assert isinstance(ddl, str), f"migration {version} not a string"
            assert ddl.strip(), f"migration {version} is empty"


class TestDetectSchemaVersion:
    def test_no_sessions_table_returns_v1(self) -> None:
        conn = sqlite3.connect(":memory:")
        assert m.detect_schema_version(conn) == 1

    def test_bare_sessions_returns_v2(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        assert m.detect_schema_version(conn) == 2

    def test_sessions_with_topics_returns_v3(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, topics_discussed TEXT)"
        )
        assert m.detect_schema_version(conn) == 3

    def test_sessions_with_embedding_returns_v4(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, topics_discussed TEXT, "
            "embedding_dim INTEGER)"
        )
        assert m.detect_schema_version(conn) == 4

    def test_access_events_fk_returns_v6(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE atoms (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE access_events ("
            "id INTEGER PRIMARY KEY, atom_id TEXT, "
            "FOREIGN KEY (atom_id) REFERENCES atoms(id))"
        )
        assert m.detect_schema_version(conn) == 6


class TestApplyPendingMigrations:
    def test_fresh_true_stamps_target_only(self) -> None:
        """``fresh=True`` means ``schema.sql`` just ran — the table shape
        is at *target_version*, so just stamp without applying any DDL."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE schema_version ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        m.apply_pending_migrations(
            conn,
            fresh=True,
            target_version=3,
            migrations={2: "SELECT 1; ", 3: "SELECT 2; "},
        )
        versions = {r[0] for r in conn.execute(
            "SELECT version FROM schema_version"
        )}
        assert versions == {3}

    def test_uses_custom_detector(self) -> None:
        """Tests can pass an instance-bound detector so monkey-patching
        per-instance is honored."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE schema_version ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        # Create the log table that our custom migration script writes to.
        conn.execute("CREATE TABLE _log (v INTEGER)")
        detector_called = []

        def _fake_detector(c: sqlite3.Connection) -> int:
            detector_called.append(c)
            return 2  # report DB as already at v2

        m.apply_pending_migrations(
            conn,
            fresh=False,
            target_version=3,
            migrations={2: "INSERT INTO _log VALUES (2);",
                        3: "INSERT INTO _log VALUES (3);"},
            detector=_fake_detector,
        )
        assert detector_called == [conn]
        # v2 baseline was stamped from the detector, only v3 DDL ran.
        log = {r[0] for r in conn.execute("SELECT v FROM _log")}
        assert log == {3}, f"expected only v3 to run, got {log}"

    def test_default_detector_used_when_omitted(self) -> None:
        """No detector argument → uses the module-level
        :func:`detect_schema_version`."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE schema_version ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        # No sessions table → detect_schema_version returns 1.
        m.apply_pending_migrations(
            conn,
            fresh=False,
            target_version=1,  # nothing to apply, just stamp
            migrations={},
        )
        versions = {r[0] for r in conn.execute(
            "SELECT version FROM schema_version"
        )}
        assert versions == {1}
