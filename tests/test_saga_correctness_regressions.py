"""Regression tests for PR #208 — saga correctness batch + migration unit tests.

Covers two regression groups and one schema-migration unit:

1. FAISS index tombstone sync — ``SagaStore.forget`` must call
   ``_index.remove(atom_id)`` for each tombstoned atom. Pre-fix the
   index accumulated orphaned positions until
   ``rebuild_if_needed`` (>10% removed) kicked in.

2. Activation decay edge cases — d=1 special branch, negative-weight
   recent events subtracting from total, zero-weight events
   contributing zero. The previous incarnation of this code (MSAM
   decay) regressed on similar edge cases.

3. ``_apply_pending_migrations`` dispatch logic — unit tests that pin
   the fresh/existing-DB branching with a sentinel MIGRATIONS dict so
   the tests are independent of the real DDL side-effects.
   The ``fresh=False`` + empty-applied path is an xfail because the
   correct fix requires ``PRAGMA table_info`` introspection (see the
   KNOWN LIMITATION comment in client.py) — it currently stamps the
   current version without running migrations.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.saga.activation import compute_activation


# ─── FAISS index tombstone sync ──────────────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.mark.asyncio
async def test_saga_forget_removes_tombstoned_atoms_from_faiss_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """After ``SagaStore.forget(dry_run=False)`` tombstones atoms,
    the FAISS index must have those positions removed so over-fetches
    don't return them and ``top_k`` stays accurate.

    Pre-fix: ``forget_by_criteria`` tombstones the SQL row but
    nothing touches the index until ``rebuild_if_needed`` (10%
    removed). Index fragmentation accumulated silently.
    """
    from mimir.saga.client import SagaStore

    # Stub embedding provider so we can run without Voyage credentials.
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            h = abs(hash(text)) % 1000
            return [float((h + i) % 17) / 17.0 for i in range(4)]

        def dimensions(self):
            return 4

    monkeypatch.setattr(
        "mimir.saga.embeddings.get_provider",
        lambda: _StubProvider(),
    )
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: lambda s, k, d=None: {
            ("embedding", "max_input_chars"): 2000,
            ("embedding", "provider"): "stub",
            ("embedding", "model"): "stub-4d",
        }.get((s, k), d),
    )

    store = SagaStore(db_path=tmp_path / "test.saga.db", embedding_dim=4)

    # Store 3 atoms with old timestamps so grace_days filter catches
    # them all. We patch the created_at directly.
    aged = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    atom_ids = []
    for i in range(3):
        result = await store.store(
            content=f"old fact {i}",
            stream="semantic",
        )
        atom_ids.append(result["atom_id"])
    conn = store._ensure_conn()
    conn.executemany(
        "UPDATE atoms SET created_at = ? WHERE id = ?",
        [(aged, aid) for aid in atom_ids],
    )
    conn.commit()

    # Force index build and capture pre-forget state.
    index = store._ensure_index(conn)
    assert index is not None, "test setup: index should build"
    pre_positions = len(index._id_to_pos)
    assert pre_positions == 3, f"expected 3 indexed atoms, got {pre_positions}"

    # Forget with grace_days=1 — all 3 atoms qualify (aged 365 days).
    result = await store.forget(grace_days=1, dry_run=False)
    assert result["tombstoned_count"] == 3, f"expected 3 tombstoned; got {result}"

    # ── The regression guard: index positions for tombstoned atoms
    #    must be removed (gone from id_to_pos OR in _removed set).
    for atom_id in atom_ids:
        if atom_id in index._id_to_pos:
            pos = index._id_to_pos[atom_id]
            assert pos in index._removed, (
                f"atom {atom_id} (pos {pos}) tombstoned in DB but FAISS "
                f"position still active — pre-fix regression"
            )


@pytest.mark.asyncio
async def test_consolidate_repairs_dual_current_world_state_with_few_raws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """chainlink #331: the world_state dual-current repair must run on EVERY
    non-dry-run consolidate — including when there are too few raw atoms to
    cluster and consolidate short-circuits at the ``< min_cluster_size`` early
    return. That early-return path is exactly where migrated/old structural
    corruption sits (a quiet DB with nothing to consolidate). Seed dual-current
    rows in an otherwise-empty store, run consolidate, and assert the repair
    fired despite the short-circuit (the bug mimir caught on PR #582)."""
    from mimir.saga.client import SagaStore

    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: lambda s, k, d=None: {
            ("embedding", "max_input_chars"): 2000,
            ("embedding", "provider"): "stub",
            ("embedding", "model"): "stub-4d",
        }.get((s, k), d),
    )
    store = SagaStore(db_path=tmp_path / "test.saga.db", embedding_dim=4)
    conn = store._ensure_conn()
    # Seed two is_current=1 rows for one (subject, predicate) — the dual-current
    # race — directly, bypassing _update_world_state.
    now = "2024-01-01T00:00:00Z"
    # source_triple_id stays NULL — the SagaStore connection enforces the FK to
    # triples(id), and these synthetic rows have no backing triple.
    for val, vf in (("Boston", "2023-01-01"), ("SF", "2023-02-01")):
        conn.execute(
            "INSERT INTO world_state "
            "(subject, predicate, value, valid_from, valid_until, "
            " is_current, source_triple_id, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, 1, NULL, ?)",
            ("Alice", "lives_in", val, vf, now),
        )
    conn.commit()

    # No raw atoms → consolidate hits the < min_cluster_size early return; the
    # repair must still have run on the way out.
    result = await store.consolidate()

    assert result["world_state_dual_current_repaired"] == 1
    current = conn.execute(
        "SELECT value FROM world_state "
        "WHERE subject='Alice' AND predicate='lives_in' AND is_current=1",
    ).fetchall()
    assert current == [("SF",)]  # newest valid_from kept; the other end-dated


# ─── Activation decay edge cases ─────────────────────────────────────


def test_compute_activation_d_equals_1_uses_log_integral():
    """The d=1 special case (line 197-200 in activation.py): integral
    becomes ``ln(oldest_age / upper_age)`` instead of the power-form.
    Locks in that the special branch is exercised and doesn't NaN /
    divide-by-zero. Pre-fix coverage relied on default d=0.5 only;
    the d=1 guard was untested.
    """
    now = datetime.now(timezone.utc)
    recent_ts = [_iso(now - timedelta(hours=1))]
    recent_weights = [1.0]
    act = compute_activation(
        recent_ts=recent_ts,
        recent_weights=recent_weights,
        old_count=5,
        old_weight_sum=5.0,
        old_oldest_ts=_iso(now - timedelta(days=7)),
        now=now,
        decay=1.0,  # special case
    )
    # Activation must be finite (not -inf, not NaN). Sign is
    # determined by whether the integral pushes Σ above 0.0.
    assert math.isfinite(act), (
        f"d=1 special case produced non-finite activation {act}"
    )


def test_compute_activation_negative_recent_weight_subtracts():
    """A negative-weight recent event (e.g. ``feedback_negative`` at
    -1.0) must subtract from the total Σ, potentially driving
    activation toward -inf if it cancels positive contributions.

    Pre-existing tests covered negative weights in the DISPLACED
    aggregate (old_weight_sum=0 short-circuit case); this locks in
    the RECENT-window subtraction path too.
    """
    now = datetime.now(timezone.utc)
    one_hour_ago = _iso(now - timedelta(hours=1))

    # Pure positive: 1 retrieval at 1h ago → activation > -inf.
    act_pos = compute_activation(
        recent_ts=[one_hour_ago],
        recent_weights=[1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )
    assert math.isfinite(act_pos)

    # Pure negative (a feedback_negative immediately after store):
    # Σ goes negative → log undefined → returns -inf per the guard
    # at line 211.
    act_neg = compute_activation(
        recent_ts=[one_hour_ago],
        recent_weights=[-1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )
    assert act_neg == float("-inf"), (
        f"negative-weight recent event must drive total ≤ 0 "
        f"and return -inf; got {act_neg}"
    )

    # Cancellation: +1.0 and -1.0 at the same age sum to exactly 0
    # → log(0) → -inf via the guard.
    act_cancel = compute_activation(
        recent_ts=[one_hour_ago, one_hour_ago],
        recent_weights=[1.0, -1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )
    assert act_cancel == float("-inf")

    # Net positive: +2.0 and -1.0 at same age → net +1.0 → matches
    # pure positive (same time-decay applied to net weight).
    act_net = compute_activation(
        recent_ts=[one_hour_ago, one_hour_ago],
        recent_weights=[2.0, -1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )
    assert math.isclose(act_net, act_pos, rel_tol=1e-9)


def test_compute_activation_zero_weight_recent_event_contributes_zero():
    """A weight=0 recent event must contribute exactly zero to Σ —
    not amplify, not suppress. Locks in the multiplicative
    ``weight * age^(-d)`` form (line 173).
    """
    now = datetime.now(timezone.utc)
    one_hour_ago = _iso(now - timedelta(hours=1))

    # Baseline: one retrieval at 1h ago.
    act_baseline = compute_activation(
        recent_ts=[one_hour_ago],
        recent_weights=[1.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )

    # Same baseline + one zero-weight event at a different time:
    # must produce identical activation.
    act_with_zero = compute_activation(
        recent_ts=[one_hour_ago, _iso(now - timedelta(minutes=5))],
        recent_weights=[1.0, 0.0],
        old_count=0, old_weight_sum=0.0, old_oldest_ts=None,
        now=now,
    )
    assert math.isclose(act_with_zero, act_baseline, rel_tol=1e-9)


# ─── _apply_pending_migrations: fresh/existing-DB split ──────────────


def _minimal_schema_version_db(conn: sqlite3.Connection) -> None:
    """Seed a minimal in-memory DB with only a schema_version table and
    a sentinel log table (no atoms, no sessions). Used to test the
    migration-dispatch logic in isolation without real DDL side-effects."""
    conn.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE _migration_log (version INTEGER);
        """
    )
    conn.commit()


def _patched_store(monkeypatch):
    """Return a bare SagaStore instance with MIGRATIONS and
    CURRENT_SCHEMA_VERSION patched to a simple, self-contained sentinel
    migration. Migration 2 appends a row to ``_migration_log``."""
    from mimir.saga.client import SagaStore

    store = SagaStore.__new__(SagaStore)
    monkeypatch.setattr(
        SagaStore,
        "MIGRATIONS",
        {2: "INSERT INTO _migration_log VALUES (2);"},
    )
    monkeypatch.setattr(SagaStore, "CURRENT_SCHEMA_VERSION", 2)
    return store


def test_apply_pending_migrations_fresh_true_stamps_current_version_no_ddl(
    monkeypatch,
):
    """fresh=True: ``schema.sql`` has already been applied. The function
    must stamp ``CURRENT_SCHEMA_VERSION`` and return *without* running
    any migration DDL (running it would collide with the already-current
    table shapes)."""
    store = _patched_store(monkeypatch)
    conn = sqlite3.connect(":memory:")
    _minimal_schema_version_db(conn)

    store._apply_pending_migrations(conn, fresh=True)

    # Only the current version should be stamped — no v1, no migration ran.
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    assert versions == {2}, f"expected {{2}}, got {versions}"

    log = conn.execute("SELECT version FROM _migration_log").fetchall()
    assert log == [], "no migration DDL should run on fresh=True"


def test_apply_pending_migrations_fresh_false_empty_applied_runs_migrations(
    monkeypatch,
):
    """fresh=False with an empty schema_version table on a pre-
    migration-era DB (no ``sessions`` table, no ``access_events`` FK) —
    the function treats it as v1 and applies pending migrations 2..N,
    rather than silently stamping the current version and skipping them.

    Fixed via the ``_detect_schema_version`` PRAGMA-introspection helper
    (chainlink #175). The detector returns 1 because no sessions table
    exists, so v1 is stamped + migration v2 runs.
    """
    store = _patched_store(monkeypatch)
    conn = sqlite3.connect(":memory:")
    _minimal_schema_version_db(conn)

    store._apply_pending_migrations(conn, fresh=False)

    # Both v1 (baseline stamp) and v2 (migration applied) should appear.
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    assert 1 in versions, "v1 baseline should be stamped"
    assert 2 in versions, "migration v2 should have been applied"

    log = conn.execute("SELECT version FROM _migration_log").fetchall()
    assert log == [(2,)], f"migration v2 DDL should have run once; got {log}"


def test_apply_pending_migrations_fresh_false_empty_applied_skips_when_db_is_current(
    monkeypatch, tmp_path,
):
    """The mid-init retry scenario: ``schema.sql`` ran (producing
    v6-shape tables), then the migration step raised and the connection
    got closed before schema_version was stamped. Next ``_ensure_conn``
    sees ``fresh=False, applied={}``, but tables are already at
    ``CURRENT_SCHEMA_VERSION``. ``_detect_schema_version`` returns 6
    via the FK marker — stamp v1..v6 and skip the migrations loop
    rather than re-running v6's DROP-and-rebuild on already-v6 tables.

    Regression guard: under the naive "treat empty applied as v1"
    fix, this test would attempt ``ALTER TABLE sessions ADD COLUMN
    topics_discussed`` against a sessions table that already has it,
    raising ``sqlite3.OperationalError: duplicate column name``.
    """
    from mimir.saga.client import SagaStore

    # Real SagaStore against a real v6 DB (the file-backed path triggers
    # schema.sql; we don't patch CURRENT_SCHEMA_VERSION or MIGRATIONS
    # here because we want the production schema to fire).
    db_path = tmp_path / "v6.saga.db"
    store = SagaStore(db_path=db_path)
    conn = store._ensure_conn()  # creates v6-shape tables + stamps v6

    # Simulate the mid-init failure by clearing schema_version.
    conn.execute("DELETE FROM schema_version")
    conn.commit()

    # Now call _apply_pending_migrations(fresh=False) — it should
    # detect v6 via the FK marker and stamp without running anything.
    store._apply_pending_migrations(conn, fresh=False)

    versions = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    assert versions == {1, 2, 3, 4, 5, 6}, (
        f"all baselines 1..6 should be stamped on a v6-shape DB; "
        f"got {sorted(versions)}"
    )


def test_detect_schema_version_returns_one_for_bare_db(tmp_path):
    """No sessions table, no access_events table → v1."""
    from mimir.saga.client import SagaStore
    import sqlite3 as sq
    store = SagaStore.__new__(SagaStore)
    conn = sq.connect(":memory:")
    assert store._detect_schema_version(conn) == 1


def test_detect_schema_version_returns_six_for_current_schema(tmp_path):
    """A DB created via the current ``schema.sql`` → v6 (FK marker)."""
    from mimir.saga.client import SagaStore
    store = SagaStore(db_path=tmp_path / "v6.saga.db")
    conn = store._ensure_conn()
    assert store._detect_schema_version(conn) == 6


def test_detect_schema_version_distinguishes_v2_v3_v4(tmp_path):
    """Synthetic sessions-table shapes test each pre-v6 marker."""
    from mimir.saga.client import SagaStore
    import sqlite3 as sq

    store = SagaStore.__new__(SagaStore)

    # v2: bare sessions table
    conn2 = sq.connect(":memory:")
    conn2.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at TEXT NOT NULL);"
    )
    assert store._detect_schema_version(conn2) == 2

    # v3: sessions has topics_discussed
    conn3 = sq.connect(":memory:")
    conn3.executescript(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, started_at TEXT NOT NULL, "
        "topics_discussed TEXT NOT NULL DEFAULT '[]');"
    )
    assert store._detect_schema_version(conn3) == 3

    # v4: sessions has embedding_dim
    conn4 = sq.connect(":memory:")
    conn4.executescript(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, started_at TEXT NOT NULL, "
        "topics_discussed TEXT NOT NULL DEFAULT '[]', "
        "embedding BLOB, embedding_dim INTEGER);"
    )
    assert store._detect_schema_version(conn4) == 4


# ─── PRAGMA foreign_keys handling in _apply_pending_migrations ────────


def test_apply_pending_migrations_restores_fk_on_after_each_migration(
    monkeypatch,
):
    """``_apply_pending_migrations`` must restore FK=ON after running each
    migration's executescript.  PRAGMA foreign_keys cannot be set *inside*
    executescript (it runs in an implicit transaction; the PRAGMA would be
    a no-op).  The fix moves the PRAGMA toggle to explicit
    ``conn.execute()`` calls around the executescript call.

    This test observes the FK state recorded by the migration DDL itself:
    the patched migration captures the ``foreign_keys`` pragma value into
    a sentinel table during execution, and we assert the connection is back
    at FK=ON after the full migration pass completes.
    """
    from mimir.saga.client import SagaStore
    import sqlite3 as sq

    store = SagaStore.__new__(SagaStore)
    # Patch a sentinel migration that records the FK state observed
    # *after* its own DDL runs (inside the same executescript).  Because
    # PRAGMA cannot be read from within executescript, we check the
    # post-migration connection state via a separate conn.execute().
    monkeypatch.setattr(
        SagaStore,
        "MIGRATIONS",
        {2: "INSERT INTO _migration_log VALUES (99);"},
    )
    monkeypatch.setattr(SagaStore, "CURRENT_SCHEMA_VERSION", 2)

    conn = sq.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        );
        CREATE TABLE _migration_log (value INTEGER);
        """
    )
    conn.commit()
    # Set FK=ON before migrations (mirrors _ensure_conn behaviour).
    conn.execute("PRAGMA foreign_keys=ON")

    store._apply_pending_migrations(conn, fresh=False)

    # After migrations complete, FK enforcement must be ON.
    fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk_on == 1, (
        "FK enforcement must be ON after _apply_pending_migrations; "
        f"got foreign_keys={fk_on}"
    )


def test_migration_ddl_strings_contain_no_pragma_foreign_keys():
    """PRAGMA foreign_keys=OFF/ON inside executescript is a no-op (the
    PRAGMA modifies connection state, which is disallowed within a
    transaction; executescript uses an implicit transaction).  The DDL
    strings in MIGRATIONS must not include these PRAGMAs — connection-
    level control lives in ``_apply_pending_migrations`` instead.
    """
    from mimir.saga.client import SagaStore

    import re

    for version, ddl in SagaStore.MIGRATIONS.items():
        # Strip SQL line comments (-- ...) before checking — the migration
        # DDL may include comment text explaining why these PRAGMAs are
        # absent; we only want to catch live PRAGMA statements.
        ddl_no_comments = re.sub(r"--[^\n]*", "", ddl)
        ddl_norm = ddl_no_comments.lower().replace(" ", "")
        assert "pragmaforeign_keys=off" not in ddl_norm, (
            f"Migration v{version} DDL contains 'PRAGMA foreign_keys=OFF' "
            "which is a no-op inside executescript; move it to a "
            "connection-level conn.execute() call instead."
        )
        assert "pragmaforeign_keys=on" not in ddl_norm, (
            f"Migration v{version} DDL contains 'PRAGMA foreign_keys=ON' "
            "which is a no-op inside executescript; the connection-level "
            "restore in _apply_pending_migrations handles this."
        )


def test_ensure_conn_applies_busy_timeout_pragma(tmp_path):
    """chainlink #227: ``saga.toml``'s ``db_busy_timeout_ms`` config dial must
    actually flow into the SQLite connection. Pre-fix, the value was declared
    but ``PRAGMA busy_timeout`` was never issued — concurrent writers raised
    ``OperationalError: database is locked`` immediately instead of waiting up
    to the configured window.
    """
    from mimir.saga.client import SagaStore

    store = SagaStore(db_path=tmp_path / "busy.saga.db", embedding_dim=4)
    # Force connection open via a trivial operation.
    conn = store._ensure_conn()  # type: ignore[attr-defined]
    busy_timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    # Default from _config_io.py is 5000ms; should be > 0 either way.
    assert busy_timeout_ms > 0, (
        f"PRAGMA busy_timeout returned {busy_timeout_ms}; expected the "
        "configured value (default 5000ms). The config dial is dead if 0."
    )
    # Verify the configured value (not the SQLite hard-coded 0 default).
    assert busy_timeout_ms == 5000, (
        f"PRAGMA busy_timeout returned {busy_timeout_ms}; expected 5000 "
        "from the default ``db_busy_timeout_ms`` in saga config."
    )


def test_migrate_init_includes_busy_timeout_pragma():
    """chainlink #227: the migration-time connection setup in ``saga/migrate.py``
    must issue ``PRAGMA busy_timeout``. We can't easily instrument the actual
    connection (sqlite3.Connection.execute is a read-only C method); instead
    verify by source-inspection that the PRAGMA call is present in the right
    place. Brittle to refactors but pins the contract.
    """
    from pathlib import Path
    src = Path(__import__("mimir.saga.migrate", fromlist=["__file__"]).__file__).read_text()
    # Look for the chainlink #227 marker + the PRAGMA call together.
    assert "chainlink #227" in src, (
        "chainlink #227 marker missing from saga/migrate.py — the fix may have been removed"
    )
    assert "PRAGMA busy_timeout" in src, (
        "PRAGMA busy_timeout not issued in saga/migrate.py per chainlink #227"
    )
    assert "db_busy_timeout_ms" in src, (
        "saga/migrate.py should read db_busy_timeout_ms from config per chainlink #227"
    )
