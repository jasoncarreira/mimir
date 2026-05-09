"""Tests for the thread-local connection pool in ``saga.core.get_db()`` (CR#11).

Pre-fix, every saga function ran ``conn = get_db(); ... ; conn.close()``
which re-opened a fresh ``sqlite3.connect`` + re-PRAGMA'd + re-applied
the schema script every time. Post-fix, ``get_db()`` returns a
thread-local pooled wrapper whose ``.close()`` is a no-op and whose
real connection lives across calls.

These tests pin:
- Repeat ``get_db()`` calls on the same thread reuse the same connection
- ``conn.close()`` is a no-op (the connection stays usable)
- Different DB_PATH (test fixture flip) returns a fresh connection
- Different threads get different connections
- ``with conn:`` (transactional context) still commits / rolls back
- Migrations apply exactly once per process per DB path
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_pool(monkeypatch, tmp_path: Path):
    """Each test gets a fresh DB_PATH and a clean thread-local cache.

    Without explicit reset, a prior test's tmp_path-rooted connection
    would leak into this one. ``_close_thread_local_db`` is the
    intended public-prefixed test helper.
    """
    import saga.core as core
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "test.db")
    # Clear the migration-applied set so each tmp DB gets its own
    # apply (otherwise the second test would skip migrations because
    # the first test "did them" — but this is a different physical DB).
    monkeypatch.setattr(core, "_migrations_done", set())
    core._close_thread_local_db()
    yield
    core._close_thread_local_db()


# ─── Reuse contract ──────────────────────────────────────────────────


def test_repeat_get_db_returns_same_wrapper_on_same_thread():
    from saga.core import get_db

    conn1 = get_db()
    conn2 = get_db()
    # Same wrapper instance — pool hit, no fresh open.
    assert conn1 is conn2


def test_close_is_no_op_pooled_connection_stays_usable():
    """The CR#11 contract: existing call sites that do
    ``conn.close()`` keep working; the conn remains pooled and the
    next ``get_db()`` returns the same wrapper."""
    from saga.core import get_db

    conn = get_db()
    conn.close()  # caller-visible no-op
    # Connection still usable.
    row = conn.execute("SELECT 1").fetchone()
    assert row[0] == 1
    # And still the same wrapper on next get_db().
    assert get_db() is conn


# ─── DB_PATH change ──────────────────────────────────────────────────


def test_db_path_change_returns_fresh_connection(monkeypatch, tmp_path: Path):
    """When the test fixture flips DB_PATH, the next get_db() must
    open a connection against the new path (not return the cached one
    pointing at the old path). Pinned because tests rely on this."""
    import saga.core as core
    from saga.core import get_db

    # First DB_PATH from the autouse fixture.
    conn1 = get_db()

    # Flip DB_PATH — simulate what the per-test fixture does.
    new_path = tmp_path / "other.db"
    monkeypatch.setattr(core, "DB_PATH", new_path)
    monkeypatch.setattr(core, "_migrations_done", set())

    conn2 = get_db()
    assert conn2 is not conn1, (
        "DB_PATH change must invalidate the thread-local cache"
    )


# ─── Thread isolation ────────────────────────────────────────────────


def test_different_threads_get_different_connections():
    """SQLite connections are not safe to share across threads. The
    pool is keyed per-thread to avoid that footgun. Pinned by spinning
    a worker and asserting its conn is distinct from the main thread's."""
    from saga.core import get_db

    main_conn = get_db()
    worker_conn = []

    def _worker():
        # Worker thread must get its OWN _PooledConnection wrapper —
        # not the main thread's. SQLite's check_same_thread default
        # would raise if they shared a real conn.
        worker_conn.append(get_db())

    t = threading.Thread(target=_worker)
    t.start()
    t.join()

    assert len(worker_conn) == 1
    assert worker_conn[0] is not main_conn
    # Both wrappers' real conns are distinct sqlite3.Connection objects.
    assert worker_conn[0]._conn is not main_conn._conn


# ─── Transactional context manager ───────────────────────────────────


def test_with_conn_context_manager_still_works():
    """``with conn:`` is sqlite3.Connection's commit/rollback context.
    The wrapper proxies ``__enter__`` / ``__exit__`` to the real conn
    so existing callers that use this pattern keep working."""
    from saga.core import get_db

    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS _t (x INTEGER)")

    # Commit path.
    with conn:
        conn.execute("INSERT INTO _t VALUES (?)", (1,))
    assert conn.execute("SELECT COUNT(*) FROM _t").fetchone()[0] == 1

    # Rollback path.
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO _t VALUES (?)", (2,))
            # Force a rollback via duplicate primary key on a fresh table:
            conn.execute("CREATE TABLE _u (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO _u (id) VALUES (1)")
            conn.execute("INSERT INTO _u (id) VALUES (1)")
    assert conn.execute("SELECT COUNT(*) FROM _t").fetchone()[0] == 1, (
        "Rollback inside `with conn:` must drop the failed INSERT into _t too"
    )


# ─── Migrations: once per process per DB ─────────────────────────────


def test_migrations_apply_exactly_once_per_process_per_db(monkeypatch):
    """The pool change splits "open the connection" from "apply
    migrations." ``_migrations_done`` is the cross-thread guarantee.
    Pinned by patching ``run_migrations`` to count invocations."""
    import saga.core as core
    from saga.core import get_db

    call_count = {"n": 0}
    real_run_migrations = core.run_migrations

    def _counting(conn):
        call_count["n"] += 1
        return real_run_migrations(conn)

    monkeypatch.setattr(core, "run_migrations", _counting)
    monkeypatch.setattr(core, "_migrations_done", set())
    core._close_thread_local_db()

    # First call: opens conn + runs migrations.
    get_db()
    assert call_count["n"] == 1

    # Second call same thread: pool hit, no migrations re-run.
    get_db()
    assert call_count["n"] == 1

    # Different thread: opens its own conn but DOESN'T re-run migrations
    # (the process-global _migrations_done set already has this DB key).
    def _worker():
        get_db()

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    assert call_count["n"] == 1, (
        "_migrations_done must prevent cross-thread re-application"
    )
