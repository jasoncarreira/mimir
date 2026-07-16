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

from mimir.models import AuthContext
from mimir.saga.activation import compute_activation


ADMIN_AUTH = AuthContext(
    principal="test-admin",
    canonical_principal="test-admin",
    roles=("admin",),
    event_ingress="test",
    trigger="user_message",
    channel_id="test-channel",
    interactivity=None,
)


# ─── FAISS index tombstone sync ──────────────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.mark.asyncio
async def test_saga_forget_removes_tombstoned_atoms_from_faiss_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        lambda: (
            lambda s, k, d=None: {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((s, k), d)
        ),
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
    result = await store.forget(
        grace_days=1,
        dry_run=False,
        auth_context=ADMIN_AUTH,
    )
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        lambda: (
            lambda s, k, d=None: {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((s, k), d)
        ),
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


@pytest.mark.asyncio
async def test_consolidate_reads_hold_db_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """chainlink #386: consolidate's shared-connection reads must run while
    _db_lock is held, so a concurrent turn's write (also _db_lock-guarded) can
    never touch the shared sqlite3 connection at the same time (sqlite3 + FTS5
    can segfault on concurrent access to one connection). Probe _candidate_raws:
    while it runs, a FOREIGN thread must not be able to acquire _db_lock. This
    fails on the pre-fix bare ``asyncio.to_thread`` read."""
    import threading
    from mimir.saga.client import SagaStore
    from mimir.saga import consolidate as consolidate_mod

    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: (
            lambda s, k, d=None: {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((s, k), d)
        ),
    )
    store = SagaStore(db_path=tmp_path / "test.saga.db", embedding_dim=4)

    observed: dict[str, bool] = {}
    orig = consolidate_mod._candidate_raws

    def probe(conn, **kwargs):
        # Runs inside _db_locked's worker thread, which holds store._db_lock.
        # A foreign thread must NOT be able to acquire it concurrently.
        foreign: dict[str, bool] = {}

        def _try():
            got = store._db_lock.acquire(blocking=False)
            foreign["acquired"] = got
            if got:
                store._db_lock.release()

        t = threading.Thread(target=_try)
        t.start()
        t.join()
        observed["lock_held"] = foreign.get("acquired") is False
        return orig(conn, **kwargs)

    monkeypatch.setattr(consolidate_mod, "_candidate_raws", probe)
    await store.consolidate(dry_run=True, dedup_first=False)

    assert observed.get("lock_held") is True, (
        "consolidate's candidate read did not hold _db_lock — a concurrent "
        "shared-connection write could race it on the same sqlite3 connection"
    )


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
    assert math.isfinite(act), f"d=1 special case produced non-finite activation {act}"


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
        old_count=0,
        old_weight_sum=0.0,
        old_oldest_ts=None,
        now=now,
    )
    assert math.isfinite(act_pos)

    # Pure negative (a feedback_negative immediately after store):
    # Σ goes negative → log undefined → returns -inf per the guard
    # at line 211.
    act_neg = compute_activation(
        recent_ts=[one_hour_ago],
        recent_weights=[-1.0],
        old_count=0,
        old_weight_sum=0.0,
        old_oldest_ts=None,
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
        old_count=0,
        old_weight_sum=0.0,
        old_oldest_ts=None,
        now=now,
    )
    assert act_cancel == float("-inf")

    # Net positive: +2.0 and -1.0 at same age → net +1.0 → matches
    # pure positive (same time-decay applied to net weight).
    act_net = compute_activation(
        recent_ts=[one_hour_ago, one_hour_ago],
        recent_weights=[2.0, -1.0],
        old_count=0,
        old_weight_sum=0.0,
        old_oldest_ts=None,
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
        old_count=0,
        old_weight_sum=0.0,
        old_oldest_ts=None,
        now=now,
    )

    # Same baseline + one zero-weight event at a different time:
    # must produce identical activation.
    act_with_zero = compute_activation(
        recent_ts=[one_hour_ago, _iso(now - timedelta(minutes=5))],
        recent_weights=[1.0, 0.0],
        old_count=0,
        old_weight_sum=0.0,
        old_oldest_ts=None,
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
    monkeypatch,
    tmp_path,
):
    """The mid-init retry scenario: ``schema.sql`` ran (producing
    v8-shape tables), then the migration step raised and the connection
    got closed before schema_version was stamped. Next ``_ensure_conn``
    sees ``fresh=False, applied={}``, but tables are already at
    ``CURRENT_SCHEMA_VERSION``. ``_detect_schema_version`` returns 8
    via the visibility column marker — stamp v1..v8 and skip the migrations
    loop rather than re-running v8's ALTER TABLE on already-v8 tables.

    Regression guard: under the naive "treat empty applied as v1"
    fix, this test would attempt ``ALTER TABLE atoms ADD COLUMN visibility``
    against an atoms table that already has it,
    raising ``sqlite3.OperationalError: duplicate column name``.
    """
    from mimir.saga.client import SagaStore

    # Real SagaStore against a real v8 DB (the file-backed path triggers
    # schema.sql; we don't patch CURRENT_SCHEMA_VERSION or MIGRATIONS
    # here because we want the production schema to fire).
    db_path = tmp_path / "v8.saga.db"
    store = SagaStore(db_path=db_path)
    conn = store._ensure_conn()  # creates v8-shape tables + stamps v8

    # Simulate the mid-init failure by clearing schema_version.
    conn.execute("DELETE FROM schema_version")
    conn.commit()

    # Now call _apply_pending_migrations(fresh=False) — it should
    # detect v8 via the visibility column and stamp without running anything.
    store._apply_pending_migrations(conn, fresh=False)

    versions = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8}, (
        f"all baselines 1..8 should be stamped on a v8-shape DB; got {sorted(versions)}"
    )


def test_detect_schema_version_returns_one_for_bare_db(tmp_path):
    """No sessions table, no access_events table → v1."""
    from mimir.saga.client import SagaStore
    import sqlite3 as sq

    store = SagaStore.__new__(SagaStore)
    conn = sq.connect(":memory:")
    assert store._detect_schema_version(conn) == 1


def test_detect_schema_version_returns_eight_for_current_schema(tmp_path):
    """A DB created via the current ``schema.sql`` → v8 (world_state visibility column)."""
    from mimir.saga.client import SagaStore

    store = SagaStore(db_path=tmp_path / "v8.saga.db")
    conn = store._ensure_conn()
    assert store._detect_schema_version(conn) == 8


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

    # v7: atoms has visibility column
    conn7 = sq.connect(":memory:")
    conn7.executescript("CREATE TABLE atoms (id TEXT PRIMARY KEY, visibility TEXT);")
    assert store._detect_schema_version(conn7) == 7


# ─── PRAGMA foreign_keys handling in _apply_pending_migrations ────────


def test_apply_pending_migrations_restores_fk_on_after_each_migration(
    monkeypatch,
):
    """``_apply_pending_migrations`` must restore FK=ON after running each
    migration. PRAGMA foreign_keys cannot be set *inside* the migration
    transaction; the PRAGMA would be a no-op. The migration applier controls
    the PRAGMA with explicit ``conn.execute()`` calls around each migration.

    This test asserts the connection is back at FK=ON after the full migration
    pass completes.
    """
    from mimir.saga.client import SagaStore
    import sqlite3 as sq

    store = SagaStore.__new__(SagaStore)
    # Patch a sentinel migration. We check the post-migration connection
    # state via a separate conn.execute().
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
    """PRAGMA foreign_keys=OFF/ON inside a migration transaction is a no-op.
    The DDL strings in MIGRATIONS must not include these PRAGMAs —
    connection-level control lives in ``_apply_pending_migrations`` instead.
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
            "which is a no-op inside the migration transaction; move it to a "
            "connection-level conn.execute() call instead."
        )
        assert "pragmaforeign_keys=on" not in ddl_norm, (
            f"Migration v{version} DDL contains 'PRAGMA foreign_keys=ON' "
            "which is a no-op inside the migration transaction; the "
            "connection-level restore in _apply_pending_migrations handles this."
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

    src = Path(
        __import__("mimir.saga.migrate", fromlist=["__file__"]).__file__
    ).read_text()
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


# ─── chainlink #390/#391: consolidate index sync + restructure orphan ──


def _stub_embeddings(monkeypatch):
    """All-identical 4-d embeddings + stub config, so atoms cluster."""

    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            return [1.0, 0.0, 0.0, 0.0]

        def dimensions(self):
            return 4

    monkeypatch.setattr("mimir.saga.embeddings.get_provider", lambda: _StubProvider())
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: (
            lambda s, k, d=None: {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((s, k), d)
        ),
    )


@pytest.mark.asyncio
async def test_consolidate_removes_dedup_tombstoned_from_faiss_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """chainlink #390: dedup-tombstoned raws must be removed from the FAISS index
    (mirror forget) so their vectors don't keep consuming top_k slots."""
    from mimir.saga.client import SagaStore

    _stub_embeddings(monkeypatch)
    store = SagaStore(db_path=tmp_path / "t.saga.db", embedding_dim=4)
    for i in range(2):  # distinct content, identical embedding -> dedup cluster
        await store.store(content=f"fact number {i}", stream="semantic")
    conn = store._ensure_conn()
    index = store._ensure_index(conn)
    assert index is not None and len(index._id_to_pos) == 2

    # dedup_first runs the dedup pass (internal min_cluster_size=2); high
    # min_cluster_size short-circuits thematic synthesis (no LLM needed).
    result = await store.consolidate(dedup_first=True, min_cluster_size=99)

    tombstoned = result["dedup"]["duplicates_tombstoned"]
    assert tombstoned, "dedup should have tombstoned the duplicate"
    for atom_id in tombstoned:
        # Removed from the index: gone from id_to_pos, or its position is marked.
        if atom_id in index._id_to_pos:
            assert index._id_to_pos[atom_id] in index._removed, (
                f"dedup-tombstoned {atom_id} still active in FAISS — #390 regression"
            )


@pytest.mark.asyncio
async def test_consolidate_keeps_rollback_branch_live_atom_in_faiss_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """chainlink #748: if a stale overlapping dedup cluster picks a canonical
    already tombstoned by an earlier cluster, the rolled-back cluster must not
    report its duplicate as tombstoned or remove that live atom from FAISS."""
    from mimir.saga.client import SagaStore
    from mimir.saga.mark_access import AccessEvent, mark_access
    import mimir.saga.cluster as cluster_mod

    _stub_embeddings(monkeypatch)
    store = SagaStore(db_path=tmp_path / "t.saga.db", embedding_dim=4)
    stored: list[str] = []
    for i in range(20):
        res = await store.store(
            content=f"dedup rollback fact {i}",
            stream="semantic",
        )
        stored.append(res["atom_id"])
    a, b, c = stored[:3]

    conn = store._ensure_conn()
    for _ in range(3):
        mark_access(conn, [AccessEvent(atom_id=a, source="retrieval")])
    conn.execute("DELETE FROM access_events WHERE atom_id = ?", (c,))
    conn.execute("DELETE FROM atom_access_summary WHERE atom_id = ?", (c,))
    conn.execute("UPDATE atoms SET is_pinned = 1 WHERE id = ?", (b,))
    conn.commit()

    index = store._ensure_index(conn)
    assert index is not None and len(index._id_to_pos) == 20

    def forced_cluster_fn(raws):
        by_id = {raw["id"]: raw for raw in raws}
        return [[by_id[a], by_id[b]], [by_id[b], by_id[c]]]

    monkeypatch.setattr(
        cluster_mod,
        "make_default_cluster_fn",
        lambda *_args, **_kwargs: forced_cluster_fn,
    )

    async def _unused_synth(*_args, **_kwargs):
        raise AssertionError("high min_cluster_size should skip synthesis")

    store._rich_synth_fn = _unused_synth

    result = await store.consolidate(dedup_first=True, min_cluster_size=99)

    tombstoned = result["dedup"]["duplicates_tombstoned"]
    assert tombstoned == [b]
    assert c not in tombstoned
    assert (
        conn.execute(
            "SELECT tombstoned FROM atoms WHERE id = ?",
            (c,),
        ).fetchone()[0]
        == 0
    )
    assert c in index._id_to_pos
    assert c in {
        atom_id
        for atom_id, _score in index.search(
            [1.0, 0.0, 0.0, 0.0],
            top_k=20,
        )
    }


@pytest.mark.asyncio
async def test_consolidate_restructure_tombstones_orphan_on_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """chainlink #391: if the relations transaction fails after the observation
    atom was already committed by _store_atom, the orphan must be tombstoned (not
    left surfacing unbacked / causing a retry duplicate)."""
    from mimir.saga.client import SagaStore

    _stub_embeddings(monkeypatch)
    store = SagaStore(db_path=tmp_path / "t.saga.db", embedding_dim=4)
    for i in range(3):  # identical embedding -> one thematic cluster
        await store.store(content=f"observation seed {i}", stream="semantic")

    async def _stub_synth(cluster, *, prior_block="", vocab_block=""):
        return {
            "content": "synthesized observation",
            "topics": [],
            "triples": [],
            "contradictions": [],
        }

    store._rich_synth_fn = _stub_synth
    # Force the relations transaction to fail AFTER the observation atom is
    # committed (find_superseded_observations runs inside the BEGIN IMMEDIATE).
    import mimir.saga.consolidate as consolidate_mod

    def _boom(*a, **k):
        raise RuntimeError("injected relations-txn failure")

    monkeypatch.setattr(consolidate_mod, "find_superseded_observations", _boom)

    with pytest.raises(RuntimeError, match="injected relations-txn failure"):
        await store.consolidate(dedup_first=False, min_cluster_size=2)

    conn = store._ensure_conn()
    rows = conn.execute(
        "SELECT id, tombstoned FROM atoms WHERE memory_type='observation'"
    ).fetchall()
    assert rows, "the observation atom should have been created by _store_atom"
    assert all(r[1] == 1 for r in rows), (
        "orphaned observation must be tombstoned after the relations rollback "
        "(#391) — found a live unbacked observation"
    )


# ─── chainlink #425: skill-memory index sync + rebuild_if_needed wiring ──


@pytest.mark.asyncio
async def test_consolidate_skill_memories_removes_tombstoned_from_faiss_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """chainlink #425: the per-skill dedup pass must mirror dedup tombstones
    into ``VectorIndex.remove`` — the #390 fix was applied to consolidate()
    but not consolidate_skill_memories(), so a skill's merged learnings kept
    their vectors live and consumed top_k slots."""
    from mimir.saga.client import SagaStore

    _stub_embeddings(monkeypatch)
    store = SagaStore(db_path=tmp_path / "t.saga.db", embedding_dim=4)
    for i in range(2):  # distinct content, identical embedding → dedup cluster
        await store.store(
            content=f"gotcha number {i}",
            source_type="skill_learning",
            metadata={"skill": "cb", "kind": "tip"},
        )
    conn = store._ensure_conn()
    index = store._ensure_index(conn)
    assert index is not None and len(index._id_to_pos) == 2

    result = await store.consolidate_skill_memories()

    tombstoned = result["skills"]["cb"]["duplicates_tombstoned"]
    assert tombstoned, "per-skill dedup should have tombstoned the duplicate"
    for atom_id in tombstoned:
        # Removed from the index: gone from id_to_pos, or its position is marked.
        if atom_id in index._id_to_pos:
            assert index._id_to_pos[atom_id] in index._removed, (
                f"dedup-tombstoned skill-learning {atom_id} still active in "
                "FAISS — #425 (the #390 regression, per-skill pass)"
            )


@pytest.mark.asyncio
async def test_forget_triggers_index_rebuild_past_removal_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """chainlink #425: ``rebuild_if_needed`` (>10% soft-removed → full
    rebuild) had zero callers. forget() must invoke it at end-of-pass so
    a removal batch past the threshold collapses into a fresh index
    (no lingering soft-removed positions)."""
    from mimir.saga.client import SagaStore

    _stub_embeddings(monkeypatch)
    store = SagaStore(db_path=tmp_path / "t.saga.db", embedding_dim=4)
    atom_ids = []
    for i in range(4):
        r = await store.store(content=f"fact number {i}", stream="semantic")
        atom_ids.append(r["atom_id"])
    # Protect 3 of 4 from the min_retrievals criterion below.
    await store.outcome(
        atom_ids[:3],
        feedback="positive",
        auth_context=ADMIN_AUTH,
    )
    conn = store._ensure_conn()
    index = store._ensure_index(conn)
    assert index is not None and len(index._id_to_pos) == 4

    result = await store.forget(
        dry_run=False,
        min_retrievals=1,
        auth_context=ADMIN_AUTH,
    )

    assert result["tombstoned_count"] == 1
    # 1/4 = 25% > 10% → the end-of-forget backstop rebuilt from disk:
    # soft-removed set cleared, only the 3 live atoms indexed.
    assert not index._removed, (
        "soft-removed positions survived forget() — rebuild_if_needed "
        "was not invoked (#425)"
    )
    assert len(index._id_to_pos) == 3
    assert atom_ids[3] not in index._id_to_pos


@pytest.mark.asyncio
async def test_consolidate_triggers_index_rebuild_past_removal_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """chainlink #425: consolidate() must run the same end-of-cycle rebuild
    backstop — a dedup pass that tombstones >10% of indexed vectors leaves a
    freshly-rebuilt index, not an accumulating soft-removed set."""
    from mimir.saga.client import SagaStore

    _stub_embeddings(monkeypatch)
    store = SagaStore(db_path=tmp_path / "t.saga.db", embedding_dim=4)
    for i in range(2):  # identical embedding → one dedup cluster
        await store.store(content=f"fact number {i}", stream="semantic")
    conn = store._ensure_conn()
    index = store._ensure_index(conn)
    assert index is not None and len(index._id_to_pos) == 2

    result = await store.consolidate(dedup_first=True, min_cluster_size=99)

    tombstoned = result["dedup"]["duplicates_tombstoned"]
    assert tombstoned, "dedup should have tombstoned the duplicate"
    # 1/2 = 50% > 10% → rebuilt: tombstoned atom fully gone, no soft marks.
    assert not index._removed
    for atom_id in tombstoned:
        assert atom_id not in index._id_to_pos
    assert len(index._id_to_pos) == 1


# ─── chainlink #417: no embedding I/O inside consolidate transactions ──


@pytest.mark.asyncio
async def test_consolidate_never_embeds_inside_a_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """chainlink #417: _restructure held BEGIN IMMEDIATE + the global write
    lock across per-triple network embedding calls (store_triples embedded
    inside the relations transaction). All embeddings must be precomputed
    before the transaction — the embed fn must never run while the shared
    connection is mid-transaction — and the restructure output (observation
    + embedded triples) must be unchanged."""
    from mimir.saga import client as client_mod
    from mimir.saga.client import SagaStore

    _stub_embeddings(monkeypatch)
    store = SagaStore(db_path=tmp_path / "t.saga.db", embedding_dim=4)
    for i in range(3):  # identical embedding → one thematic cluster
        await store.store(content=f"observation seed {i}", stream="semantic")
    conn = store._ensure_conn()

    real_embed = client_mod._embed_text_sync
    in_txn_calls: list[bool] = []

    def _recording_embed(text):
        in_txn_calls.append(conn.in_transaction)
        return real_embed(text)

    monkeypatch.setattr(client_mod, "_embed_text_sync", _recording_embed)

    async def _stub_synth(cluster, *, prior_block="", vocab_block=""):
        return {
            "content": "synthesized observation",
            "topics": [],
            "triples": [
                {"subject": "Alice", "predicate": "likes", "object": "tea"},
            ],
            "contradictions": [],
        }

    store._rich_synth_fn = _stub_synth
    result = await store.consolidate(dedup_first=False, min_cluster_size=2)

    # Output unchanged: one observation, one embedded triple.
    assert result["observations_created"] == 1
    assert result["triples_stored"] == 1
    triple_row = conn.execute(
        "SELECT subject, embedding, embedding_dim FROM triples"
    ).fetchone()
    assert triple_row is not None and triple_row[0] == "Alice"
    assert triple_row[1] is not None and triple_row[2] == 4, (
        "triple must still get its precomputed embedding"
    )

    # The invariant: embedding ran (observation + triple) and NEVER while
    # the shared connection was inside a transaction.
    assert in_txn_calls, "embed fn should have been called during consolidate"
    assert not any(in_txn_calls), (
        "embedding I/O ran inside an open transaction — #417 regression"
    )
