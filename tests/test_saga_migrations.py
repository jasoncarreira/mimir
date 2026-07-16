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

    def test_atoms_visibility_returns_v7(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE atoms (id TEXT PRIMARY KEY, visibility TEXT)")
        assert m.detect_schema_version(conn) == 7


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

    def test_unstamped_add_column_migration_can_be_replayed(self) -> None:
        """Regression for an interrupted v4 migration.

        Older migration code ran ``executescript(ddl)`` and stamped
        ``schema_version`` afterward. If the process died after v4's
        ``ALTER TABLE`` statements but before the v4 stamp committed, the
        next open retried v4 and raised ``duplicate column name``.
        """
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                topics_discussed TEXT NOT NULL DEFAULT '[]',
                decisions_made TEXT NOT NULL DEFAULT '[]',
                unfinished TEXT NOT NULL DEFAULT '[]',
                emotional_state TEXT,
                closed_since TEXT NOT NULL DEFAULT '[]',
                embedding BLOB,
                embedding_dim INTEGER
            );
            INSERT INTO schema_version (version, applied_at)
                VALUES (3, '2000-01-01T00:00:00+00:00');
            """
        )

        m.apply_pending_migrations(
            conn,
            fresh=False,
            target_version=4,
            migrations={4: m.MIGRATIONS[4]},
        )

        versions = {r[0] for r in conn.execute(
            "SELECT version FROM schema_version"
        )}
        columns = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
        assert versions == {3, 4}
        assert columns.count("embedding") == 1
        assert columns.count("embedding_dim") == 1

    def test_failed_migration_rolls_back_ddl_and_stamp_together(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_version (version, applied_at)
                VALUES (1, '2000-01-01T00:00:00+00:00');
            """
        )

        with pytest.raises(sqlite3.OperationalError):
            m.apply_pending_migrations(
                conn,
                fresh=False,
                target_version=2,
                migrations={
                    2: (
                        "CREATE TABLE should_roll_back (id INTEGER);"
                        "INSERT INTO missing_table VALUES (1);"
                    ),
                },
            )

        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='should_roll_back'"
        ).fetchone()
        versions = {r[0] for r in conn.execute(
            "SELECT version FROM schema_version"
        )}
        assert table_exists is None
        assert versions == {1}


class TestV6RebuildDataPreservation:
    def test_v6_rebuild_preserves_populated_rows_exactly(self) -> None:
        """Protect the CREATE/COPY/DROP/RENAME path from SELECT-* drift.

        A new column on any v5 source table must not shift or break the v6
        copy. This fixture includes every table rebuilt by v6, plus the SAGA
        tables that must remain untouched, and compares all values before and
        after the migration.
        """
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE atoms (id TEXT PRIMARY KEY);
            INSERT INTO atoms VALUES ('a1'), ('a2');

            CREATE TABLE access_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                atom_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                source TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                session_id TEXT,
                metadata TEXT DEFAULT '{}'
            );
            INSERT INTO access_events VALUES
                (7, 'a1', '2024-01-02', 'retrieval', 1.5, 's1', '{"event":1}');

            CREATE TABLE atom_access_summary (
                atom_id TEXT PRIMARY KEY,
                recent_ts_json TEXT DEFAULT '[]',
                recent_weights_json TEXT DEFAULT '[]',
                old_count INTEGER DEFAULT 0,
                old_weight_sum REAL DEFAULT 0.0,
                old_oldest_ts TEXT,
                last_updated_ts TEXT
            );
            INSERT INTO atom_access_summary VALUES
                ('a1', '["2024-01-02"]', '[1.5]', 3, 2.5,
                 '2023-01-01', '2024-01-03');

            CREATE TABLE embeddings (
                atom_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vec BLOB NOT NULL,
                embedded_at TEXT NOT NULL
            );
            INSERT INTO embeddings VALUES
                ('a1', 'stub', 'stub-2d', 2, X'0102', '2024-01-03');

            CREATE TABLE observations_metadata (
                atom_id TEXT PRIMARY KEY,
                evidence_count INTEGER DEFAULT 0,
                trend TEXT,
                last_evidence_at TEXT,
                consolidated_at TEXT NOT NULL,
                consolidation_session TEXT
            );
            INSERT INTO observations_metadata VALUES
                ('a1', 4, 'stable', '2024-01-02', '2024-01-03', 's1');

            CREATE TABLE atom_topics (
                atom_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                PRIMARY KEY (atom_id, topic)
            );
            INSERT INTO atom_topics VALUES ('a1', 'migration');

            CREATE TABLE atom_relations (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                PRIMARY KEY (source_id, target_id, relation_type)
            );
            INSERT INTO atom_relations VALUES
                ('a1', 'a2', 'evidenced_by', 0.75, '2024-01-04', '{"rel":1}');

            CREATE TABLE triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                source_atom_id TEXT,
                confidence REAL DEFAULT 1.0,
                valid_from TEXT,
                valid_until TEXT,
                embedding BLOB,
                embedding_dim INTEGER,
                tombstoned INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            );
            INSERT INTO triples VALUES
                ('t1', 'subject', 'predicate', 'object', 'a1', 0.8,
                 '2024-01-01', NULL, X'0304', 2, 0, '2024-01-05',
                 '{"triple":1}');

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                summary TEXT,
                reflected_at TEXT,
                topics_discussed TEXT NOT NULL DEFAULT '[]',
                decisions_made TEXT NOT NULL DEFAULT '[]',
                unfinished TEXT NOT NULL DEFAULT '[]',
                emotional_state TEXT,
                closed_since TEXT NOT NULL DEFAULT '[]',
                embedding BLOB,
                embedding_dim INTEGER
            );
            INSERT INTO sessions VALUES
                ('s1', 'channel-1', '2024-01-01', '2024-01-02', 'summary',
                 '2024-01-03', '["topic"]', '["decision"]', '["todo"]',
                 'focused', '["old"]', X'0506', 2);

            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_version VALUES
                (5, '2000-01-01T00:00:00+00:00');
            """
        )
        preserved = (
            "access_events",
            "atom_access_summary",
            "embeddings",
            "observations_metadata",
            "atom_topics",
            "atom_relations",
            "triples",
            "sessions",
        )
        before = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in preserved
        }

        m.apply_pending_migrations(
            conn,
            fresh=False,
            target_version=6,
            migrations={6: m.MIGRATIONS[6]},
        )

        after = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in preserved
        }
        assert after == before
        assert conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall() == [(5,), (6,)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }.issuperset(
            {
                "idx_access_atom_ts",
                "idx_emb_provider",
                "idx_topics_topic",
                "idx_relations_source",
                "idx_triples_spo",
            }
        )


class TestV7OwnershipMigration:
    def test_v7_adds_ownership_columns_to_atoms(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE atoms (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                started_at TEXT NOT NULL,
                topics_discussed TEXT NOT NULL DEFAULT '[]',
                decisions_made TEXT NOT NULL DEFAULT '[]',
                unfinished TEXT NOT NULL DEFAULT '[]',
                closed_since TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE observations_metadata (
                atom_id TEXT PRIMARY KEY,
                consolidated_at TEXT NOT NULL
            );
            CREATE TABLE triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_version (version, applied_at)
                VALUES (6, '2000-01-01T00:00:00+00:00');
            """
        )
        m.apply_pending_migrations(
            conn,
            fresh=False,
            target_version=7,
            migrations={7: m.MIGRATIONS[7]},
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(atoms)")}
        assert "owner_principal" in cols
        assert "origin_channel" in cols
        assert "origin_domain" in cols
        assert "visibility" in cols
        assert "provenance" in cols

    def test_v7_sets_legacy_admin_visibility_default(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE atoms (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                started_at TEXT NOT NULL,
                topics_discussed TEXT NOT NULL DEFAULT '[]',
                decisions_made TEXT NOT NULL DEFAULT '[]',
                unfinished TEXT NOT NULL DEFAULT '[]',
                closed_since TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE observations_metadata (
                atom_id TEXT PRIMARY KEY,
                consolidated_at TEXT NOT NULL
            );
            CREATE TABLE triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_version (version, applied_at)
                VALUES (6, '2000-01-01T00:00:00+00:00');
            """
        )
        conn.execute("INSERT INTO atoms VALUES ('a1', 'test', 'hash', '2024-01-01')")
        conn.execute("INSERT INTO sessions VALUES ('s1', NULL, '2024-01-01', '[]', '[]', '[]', '[]')")
        conn.execute("INSERT INTO observations_metadata VALUES ('a1', '2024-01-01')")
        conn.execute("INSERT INTO triples VALUES ('t1', 'subj', 'pred', 'obj', '2024-01-01')")
        conn.commit()

        m.apply_pending_migrations(
            conn,
            fresh=False,
            target_version=7,
            migrations={7: m.MIGRATIONS[7]},
        )

        atom_vis = conn.execute(
            "SELECT visibility FROM atoms WHERE id = 'a1'"
        ).fetchone()[0]
        assert atom_vis == "legacy_admin"

        sess_vis = conn.execute(
            "SELECT visibility FROM sessions WHERE id = 's1'"
        ).fetchone()[0]
        assert sess_vis == "legacy_admin"

        obs_vis = conn.execute(
            "SELECT visibility FROM observations_metadata WHERE atom_id = 'a1'"
        ).fetchone()[0]
        assert obs_vis == "legacy_admin"

        triple_vis = conn.execute(
            "SELECT visibility FROM triples WHERE id = 't1'"
        ).fetchone()[0]
        assert triple_vis == "legacy_admin"

    def test_v7_creates_ownership_indexes(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE atoms (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                started_at TEXT NOT NULL,
                topics_discussed TEXT NOT NULL DEFAULT '[]',
                decisions_made TEXT NOT NULL DEFAULT '[]',
                unfinished TEXT NOT NULL DEFAULT '[]',
                closed_since TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE observations_metadata (
                atom_id TEXT PRIMARY KEY,
                consolidated_at TEXT NOT NULL
            );
            CREATE TABLE triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_version (version, applied_at)
                VALUES (6, '2000-01-01T00:00:00+00:00');
            """
        )
        m.apply_pending_migrations(
            conn,
            fresh=False,
            target_version=7,
            migrations={7: m.MIGRATIONS[7]},
        )
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        assert "idx_atoms_visibility" in indexes
        assert "idx_atoms_owner" in indexes
        assert "idx_sessions_visibility" in indexes
        assert "idx_sessions_owner" in indexes
        assert "idx_sessions_channel" in indexes
        assert "idx_obs_metadata_visibility" in indexes
        assert "idx_obs_metadata_owner" in indexes
        assert "idx_triples_visibility" in indexes
        assert "idx_triples_owner" in indexes

    def test_v7_preserves_existing_data(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE atoms (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                stream TEXT DEFAULT 'semantic',
                profile TEXT DEFAULT 'standard',
                memory_type TEXT DEFAULT 'raw',
                arousal REAL DEFAULT 0.5,
                valence REAL DEFAULT 0.0,
                encoding_confidence REAL DEFAULT 0.7,
                topics TEXT DEFAULT '[]',
                source_type TEXT DEFAULT 'conversation',
                metadata TEXT DEFAULT '{}',
                tombstoned INTEGER DEFAULT 0,
                is_pinned INTEGER DEFAULT 0,
                agent_id TEXT DEFAULT 'default',
                session_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                summary TEXT,
                reflected_at TEXT,
                topics_discussed TEXT NOT NULL DEFAULT '[]',
                decisions_made TEXT NOT NULL DEFAULT '[]',
                unfinished TEXT NOT NULL DEFAULT '[]',
                emotional_state TEXT,
                closed_since TEXT NOT NULL DEFAULT '[]',
                embedding BLOB,
                embedding_dim INTEGER
            );
            CREATE TABLE observations_metadata (
                atom_id TEXT PRIMARY KEY,
                evidence_count INTEGER DEFAULT 0,
                trend TEXT,
                last_evidence_at TEXT,
                consolidated_at TEXT NOT NULL,
                consolidation_session TEXT
            );
            CREATE TABLE triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                source_atom_id TEXT,
                confidence REAL DEFAULT 1.0,
                valid_from TEXT,
                valid_until TEXT,
                embedding BLOB,
                embedding_dim INTEGER,
                tombstoned INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            );
            CREATE TABLE atom_relations (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                PRIMARY KEY (source_id, target_id, relation_type)
            );
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_version (version, applied_at)
                VALUES (6, '2000-01-01T00:00:00+00:00');
            """
        )
        conn.execute(
            "INSERT INTO atoms VALUES "
            "('a1', 'test content', 'hash123', 'semantic', 'standard', 'raw', "
            "0.5, 0.0, 0.7, '[]', 'conversation', '{}', 0, 0, 'default', NULL, '2024-01-01')"
        )
        conn.execute(
            "INSERT INTO sessions VALUES "
            "('s1', 'ch1', '2024-01-01', '2024-01-02', 'summary', '2024-01-02', "
            "'[]', '[]', '[]', NULL, '[]', NULL, 4)"
        )
        conn.execute(
            "INSERT INTO observations_metadata VALUES "
            "('a1', 5, 'stable', '2024-01-01', '2024-01-01', 's1')"
        )
        conn.execute(
            "INSERT INTO triples VALUES "
            "('t1', 'subject', 'predicate', 'object', 'a1', 1.0, NULL, NULL, NULL, NULL, 0, '2024-01-01', '{}')"
        )
        conn.execute(
            "INSERT INTO atom_relations VALUES "
            "('a1', 'a2', 'evidenced_by', 1.0, '2024-01-01', '{}')"
        )
        conn.commit()

        m.apply_pending_migrations(
            conn,
            fresh=False,
            target_version=7,
            migrations={7: m.MIGRATIONS[7]},
        )

        atom = conn.execute("SELECT id, content, content_hash, stream FROM atoms WHERE id = 'a1'").fetchone()
        assert atom == ("a1", "test content", "hash123", "semantic")

        sess = conn.execute("SELECT id, channel_id, summary FROM sessions WHERE id = 's1'").fetchone()
        assert sess == ("s1", "ch1", "summary")

        obs = conn.execute("SELECT atom_id, evidence_count, trend FROM observations_metadata WHERE atom_id = 'a1'").fetchone()
        assert obs == ("a1", 5, "stable")

        triple = conn.execute("SELECT id, subject, predicate, object FROM triples WHERE id = 't1'").fetchone()
        assert triple == ("t1", "subject", "predicate", "object")

        rel = conn.execute("SELECT source_id, target_id, relation_type FROM atom_relations").fetchone()
        assert rel == ("a1", "a2", "evidenced_by")

        versions = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
        assert 7 in versions

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
    def test_v7_rejects_null_ownership_fields(
        self, table: str, seed_sql: str, required_column: str
    ) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE atoms (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                started_at TEXT NOT NULL
            );
            CREATE TABLE observations_metadata (
                atom_id TEXT PRIMARY KEY,
                consolidated_at TEXT NOT NULL
            );
            CREATE TABLE triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_version (version, applied_at)
                VALUES (6, '2000-01-01T00:00:00+00:00');
            """
        )
        m.apply_pending_migrations(
            conn,
            fresh=False,
            target_version=7,
            migrations={7: m.MIGRATIONS[7]},
        )

        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            conn.execute(seed_sql.format(column=required_column))

    def test_v8_preserves_world_state_rows_and_defaults_acl(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE world_state (
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                value TEXT NOT NULL,
                valid_from TEXT,
                valid_until TEXT,
                is_current INTEGER DEFAULT 1,
                source_triple_id TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (subject, predicate, valid_from)
            );
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_version VALUES
                (7, '2000-01-01T00:00:00+00:00');
            INSERT INTO world_state
                (subject, predicate, value, valid_from, updated_at)
                VALUES ('Alice', 'status', 'active', '2024-01-01', '2024-01-01');
            """
        )

        m.apply_pending_migrations(
            conn,
            fresh=False,
            target_version=8,
            migrations={8: m.MIGRATIONS[8]},
        )

        row = conn.execute(
            "SELECT subject, predicate, value, owner_principal, visibility, provenance "
            "FROM world_state"
        ).fetchone()
        assert row == (
            "Alice", "status", "active", "legacy_admin", "legacy_admin", "{}"
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                "INSERT INTO world_state "
                "(subject, predicate, value, valid_from, updated_at, visibility) "
                "VALUES ('Bob', 'status', 'active', '2024-01-01', "
                "'2024-01-01', 'unexpected')"
            )
