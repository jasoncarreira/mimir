"""#492: SagaStore._add_atom_to_index_locked must hold _index_lock during the
FAISS add, matching every other index-mutation site. Without it, a concurrent
query()-driven lazy build/rebuild can swap self._index between the built-check
and the add, dropping the add into a discarded index.
"""

from __future__ import annotations

from pathlib import Path

from mimir.saga.client import SagaStore


def test_add_atom_to_index_holds_index_lock(tmp_path: Path) -> None:
    store = SagaStore(db_path=tmp_path / "saga.db", embedding_dim=None)
    conn = store._ensure_conn()
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at, stream, "
        "profile, memory_type, source_type, metadata, agent_id) "
        "VALUES ('x','c','h','2026-06-03T00:00:00+00:00','semantic','standard',"
        "'observation','test','{}','default')",
    )
    conn.execute(
        "INSERT INTO embeddings (atom_id, provider, model, dim, vec, embedded_at) "
        "VALUES ('x','onnx','m',3,?,'2026-06-03T00:00:00+00:00')",
        (b"\x00" * 12,),
    )
    conn.commit()

    state = {"depth": 0, "add_under_lock": None}
    real_lock = store._index_lock

    class _TrackLock:
        """Wraps the real RLock to record whether we're inside it (RLock
        re-acquire can't reveal held-by-same-thread, so track a depth)."""

        def __enter__(self):
            real_lock.acquire()
            state["depth"] += 1
            return self

        def __exit__(self, *exc):
            state["depth"] -= 1
            real_lock.release()
            return False

    store._index_lock = _TrackLock()

    class _FakeIndex:
        built = True

        def add(self, atom_id, vec):
            state["add_under_lock"] = state["depth"] > 0

    store._index = _FakeIndex()

    store._add_atom_to_index_locked(conn, "x")

    assert state["add_under_lock"] is True, "index.add ran without _index_lock held"


def test_add_atom_to_index_noop_when_index_unbuilt(tmp_path: Path) -> None:
    """No built index → no-op (no crash, no add)."""
    store = SagaStore(db_path=tmp_path / "saga.db", embedding_dim=None)
    conn = store._ensure_conn()

    class _Unbuilt:
        built = False

        def add(self, *a):  # pragma: no cover - must not be called
            raise AssertionError("add called on an unbuilt index")

    store._index = _Unbuilt()
    store._add_atom_to_index_locked(conn, "missing")  # no raise