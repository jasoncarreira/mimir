"""SAGA Metrics Tests -- observability and statistics collection."""

import sqlite3
from datetime import datetime, timezone, timedelta

import pytest
import numpy as np


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Use temporary databases for all tests."""
    db_path = tmp_path / "test_saga.db"
    metrics_path = tmp_path / "test_metrics.db"
    monkeypatch.setattr("saga.core.DB_PATH", db_path)
    monkeypatch.setattr("saga.metrics.METRICS_DB", metrics_path)
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("saga.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("saga.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("saga.core._cached_embed_query_import", lambda t: tuple(fake_emb))
    monkeypatch.setattr("saga.core.cached_embed_query", lambda t: fake_emb)
    yield db_path


class TestMetricsDbCreation:
    def test_creates_db(self):
        from saga.metrics import get_metrics_db
        conn = get_metrics_db()
        # Verify tables exist
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r[0] for r in tables}
        assert "retrieval_metrics" in table_names
        assert "store_metrics" in table_names
        assert "decay_metrics" in table_names
        assert "access_events" in table_names
        conn.close()


class TestMsamToSagaMigration:
    """PR #122: legacy `comparison_metrics.msam_*` columns rename to
    `saga_*` on existing databases. CREATE TABLE IF NOT EXISTS won't
    alter an existing table — the migration runs on every connection
    open and is idempotent."""

    def test_renames_legacy_columns_on_existing_db(self, tmp_path, monkeypatch):
        """Pre-create the metrics DB with the legacy schema, then call
        get_metrics_db() — the legacy msam_* columns should be renamed
        to saga_* and an INSERT against the new column names should
        succeed.
        """
        legacy_path = tmp_path / "legacy_metrics.db"
        legacy_conn = sqlite3.connect(str(legacy_path))
        legacy_conn.executescript("""
            CREATE TABLE comparison_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                query TEXT,
                msam_tokens INTEGER,
                msam_latency_ms REAL,
                msam_atoms INTEGER,
                markdown_tokens INTEGER,
                markdown_latency_ms REAL,
                markdown_results INTEGER,
                token_savings_pct REAL,
                info_density_ratio REAL
            );
        """)
        legacy_conn.commit()
        legacy_conn.close()

        monkeypatch.setattr("saga.metrics.METRICS_DB", legacy_path)
        from saga.metrics import get_metrics_db
        conn = get_metrics_db()

        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(comparison_metrics)"
        ).fetchall()}
        assert "saga_tokens" in cols
        assert "saga_latency_ms" in cols
        assert "saga_atoms" in cols
        assert "msam_tokens" not in cols
        assert "msam_latency_ms" not in cols
        assert "msam_atoms" not in cols

        # Sanity: an INSERT against the new column names succeeds.
        conn.execute(
            "INSERT INTO comparison_metrics "
            "(timestamp, query, saga_tokens, saga_latency_ms, saga_atoms) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-05-11T00:00:00", "test", 100, 5.0, 3),
        )
        conn.commit()
        conn.close()

    def test_no_op_on_fresh_install(self, tmp_path, monkeypatch):
        """Fresh install: the migration's RENAME COLUMN raises
        OperationalError ("no such column: msam_tokens") and is
        caught silently. The new schema's saga_* columns are present
        from the CREATE TABLE."""
        fresh_path = tmp_path / "fresh_metrics.db"
        monkeypatch.setattr("saga.metrics.METRICS_DB", fresh_path)
        from saga.metrics import get_metrics_db
        conn = get_metrics_db()
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(comparison_metrics)"
        ).fetchall()}
        assert {"saga_tokens", "saga_latency_ms", "saga_atoms"}.issubset(cols)
        conn.close()

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        """Calling get_metrics_db() repeatedly on an already-migrated
        DB is a no-op (no exception, schema unchanged)."""
        legacy_path = tmp_path / "legacy_metrics.db"
        legacy_conn = sqlite3.connect(str(legacy_path))
        # Match the legacy comparison_metrics shape (timestamp column +
        # CREATE INDEX both depend on it).
        legacy_conn.executescript("""
            CREATE TABLE comparison_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                query TEXT,
                msam_tokens INTEGER,
                msam_latency_ms REAL,
                msam_atoms INTEGER,
                markdown_tokens INTEGER,
                markdown_latency_ms REAL,
                markdown_results INTEGER,
                token_savings_pct REAL,
                info_density_ratio REAL
            );
        """)
        legacy_conn.commit()
        legacy_conn.close()

        monkeypatch.setattr("saga.metrics.METRICS_DB", legacy_path)
        from saga.metrics import get_metrics_db
        # First call applies the migration.
        get_metrics_db().close()
        # Second + third calls are no-ops.
        get_metrics_db().close()
        get_metrics_db().close()
        # Verify the schema is in its final shape.
        conn = get_metrics_db()
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(comparison_metrics)"
        ).fetchall()}
        assert {"saga_tokens", "saga_latency_ms", "saga_atoms"}.issubset(cols)
        assert not any(c.startswith("msam_") for c in cols)
        conn.close()


class TestLogStore:
    def test_log_store_event(self):
        from saga.metrics import log_store, get_metrics_db
        log_store("atom_123", "semantic", "standard", 0.5, 0.2, "manual", 50)

        conn = get_metrics_db()
        row = conn.execute("SELECT * FROM store_metrics ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row["atom_id"] == "atom_123"
        assert row["stream"] == "semantic"


class TestLogAccessEvent:
    def test_all_fields(self):
        from saga.metrics import log_access_event, get_metrics_db
        log_access_event(
            event_type="retrieval",
            caller="test",
            query="test query",
            mode="task",
            atoms_accessed=5,
            tokens_used=100,
            latency_ms=42.5,
            activation_min=0.1,
            activation_max=0.9,
            activation_p50=0.5,
            activation_p90=0.8,
            similarity_min=0.2,
            similarity_max=0.95,
            topics_hit=["memory", "identity"],
            detail="test detail",
        )

        conn = get_metrics_db()
        row = conn.execute("SELECT * FROM access_events ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row["event_type"] == "retrieval"
        assert row["atoms_accessed"] == 5
        assert row["latency_ms"] == 42.5


class TestLogDecayEvent:
    def test_log_decay(self):
        from saga.metrics import log_decay_event, get_metrics_db
        log_decay_event(
            atoms_faded=3,
            atoms_dormant=1,
            atoms_compacted=2,
            tokens_freed=500,
            budget_before=55.0,
            budget_after=52.0,
            total_active=100,
            total_fading=10,
            total_dormant=5,
        )

        conn = get_metrics_db()
        row = conn.execute("SELECT * FROM decay_metrics ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row["atoms_faded"] == 3
        assert row["tokens_freed"] == 500


class TestLogRetrieval:
    def test_logs_retrieval(self):
        from saga.metrics import log_retrieval, get_metrics_db
        results = [
            {"content": "test content here", "_activation": 5.0, "_similarity": 0.8,
             "topics": '["memory"]', "stream": "semantic"},
            {"content": "another atom", "_activation": 3.0, "_similarity": 0.6,
             "topics": '["identity"]', "stream": "episodic"},
        ]
        log_retrieval("test query", "task", results, 42.5)

        conn = get_metrics_db()
        row = conn.execute("SELECT * FROM retrieval_metrics ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row["query"] == "test query"
        assert row["atoms_returned"] == 2
        assert row["latency_ms"] == 42.5


class TestLogSystemSnapshot:
    def test_captures_system_state(self):
        from saga.core import get_db, run_migrations, store_atom
        from saga.metrics import log_system_snapshot, get_metrics_db

        conn = get_db()
        run_migrations(conn)
        conn.close()

        store_atom("Test atom for system snapshot")

        log_system_snapshot()

        conn = get_metrics_db()
        row = conn.execute("SELECT * FROM system_metrics ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row["total_atoms"] >= 1
        assert row["active_atoms"] >= 1


class TestLogEmotionalState:
    def test_logs_emotional_state(self):
        from saga.metrics import log_emotional_state, get_metrics_db
        log_emotional_state(0.7, 0.3, "focused", secondary_state="calm",
                           intensity=0.6, warmth=0.8)

        conn = get_metrics_db()
        row = conn.execute("SELECT * FROM emotional_metrics ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row["arousal"] == 0.7
        assert row["primary_state"] == "focused"


class TestLogTopicHits:
    def test_logs_topics(self):
        from saga.metrics import log_topic_hits, get_metrics_db
        log_topic_hits(["memory", "identity", "schedule"], source="retrieval")

        conn = get_metrics_db()
        count = conn.execute("SELECT COUNT(*) FROM topic_timeseries").fetchone()[0]
        conn.close()
        assert count == 3

    def test_empty_topics_noop(self):
        from saga.metrics import log_topic_hits, get_metrics_db
        log_topic_hits([])
        # Should not raise


class TestLogEmbedding:
    def test_logs_embedding_call(self):
        from saga.metrics import log_embedding, get_metrics_db
        log_embedding("store", 150.5, 200, success=True)

        conn = get_metrics_db()
        row = conn.execute("SELECT * FROM embedding_metrics ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row["operation"] == "store"
        assert row["latency_ms"] == 150.5


class TestLogCanary:
    def test_logs_canary(self):
        from saga.metrics import log_canary, get_metrics_db
        log_canary("canary query", "atom_123", 5.0, 3, 25.0, "abc123")

        conn = get_metrics_db()
        row = conn.execute("SELECT * FROM canary_metrics ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row["query"] == "canary query"
        assert row["top_atom_id"] == "atom_123"


class TestGetRetrievalHistory:
    def test_returns_list(self):
        from saga.metrics import log_retrieval, get_retrieval_history
        results = [{"content": "test", "_activation": 1.0, "_similarity": 0.5,
                     "topics": "[]", "stream": "semantic"}]
        log_retrieval("q1", "task", results, 10.0)
        log_retrieval("q2", "task", results, 20.0)

        history = get_retrieval_history(limit=10)
        assert len(history) >= 2
        assert isinstance(history[0], dict)


class TestGetSystemHistory:
    def test_returns_list(self):
        from saga.metrics import get_system_history
        history = get_system_history(limit=10)
        assert isinstance(history, list)


class TestLogContinuity:
    def test_start_and_end(self):
        from saga.metrics import log_continuity_start, log_continuity_end, get_metrics_db

        row_id = log_continuity_start(
            session_type="startup",
            atom_ids=["a1", "a2"],
            topics_predicted=["memory", "identity"],
            atoms_total=10,
        )
        assert isinstance(row_id, int)

        log_continuity_end(row_id, topics_actual=["memory", "schedule"], atoms_used=5)

        conn = get_metrics_db()
        row = conn.execute("SELECT * FROM continuity_metrics WHERE id = ?", (row_id,)).fetchone()
        conn.close()
        assert row is not None
        assert row["overlap_score"] > 0  # "memory" overlaps


class TestLogCacheStats:
    def test_logs_cache_stats(self):
        from saga.metrics import log_cache_stats, get_metrics_db
        log_cache_stats(hits=100, misses=20, cache_size=500, hit_rate=0.833)

        conn = get_metrics_db()
        row = conn.execute("SELECT * FROM cache_metrics ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row["hits"] == 100


class TestComputeActivationStats:
    def test_basic_stats(self):
        from saga.metrics import _compute_activation_stats
        results = [
            {"_activation": 1.0, "_similarity": 0.3},
            {"_activation": 5.0, "_similarity": 0.8},
            {"_activation": 3.0, "_similarity": 0.5},
        ]
        act_min, act_max, p50, p90, sim_min, sim_max = _compute_activation_stats(results)
        assert act_min == 1.0
        assert act_max == 5.0
        assert sim_min == 0.3
        assert sim_max == 0.8

    def test_empty_results(self):
        from saga.metrics import _compute_activation_stats
        result = _compute_activation_stats([])
        assert result == (None, None, None, None, None, None)


class TestPruneOldMetrics:
    def test_deletes_old_records(self):
        from saga.metrics import get_metrics_db, prune_old_metrics

        conn = get_metrics_db()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        conn.execute(
            "INSERT INTO retrieval_metrics (timestamp, query, atoms_returned) VALUES (?, 'old query', 5)",
            (old_ts,)
        )
        recent_ts = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO retrieval_metrics (timestamp, query, atoms_returned) VALUES (?, 'new query', 3)",
            (recent_ts,)
        )
        conn.commit()
        conn.close()

        deleted = prune_old_metrics(days=30)
        assert deleted >= 1

        conn = get_metrics_db()
        remaining = conn.execute("SELECT COUNT(*) FROM retrieval_metrics").fetchone()[0]
        conn.close()
        assert remaining >= 1  # recent record should survive


# ─── Retrieval Miss ─────────────────────────────────────────────────────────


class TestLogRetrievalMiss:
    def test_logs_retrieval_miss(self):
        from saga.metrics import log_retrieval_miss, get_metrics_db
        log_retrieval_miss("unknown query", "task", 0.5, threshold=2.0)
        conn = get_metrics_db()
        rows = conn.execute(
            "SELECT * FROM access_events WHERE event_type = 'retrieval_miss'"
        ).fetchall()
        conn.close()
        assert len(rows) >= 1
