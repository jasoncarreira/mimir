"""Consolidation lock budgets, with test-owned stalls and negative controls."""

from __future__ import annotations

import asyncio
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from mimir.saga import cluster
from mimir.saga.client import SagaStore
from mimir.saga.dedup import MAX_DEDUP_CANDIDATES, _candidate_raws_for_dedup
from mimir.saga.store import store


@pytest.fixture
def client(tmp_path, monkeypatch):
    client = SagaStore(db_path=tmp_path / "saga.db")
    client._rich_synth_fn = object()  # Tests never enter synthesis.
    monkeypatch.setattr(client, "_rebuild_index_if_needed", lambda conn: False)
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: lambda section, key, default=None: default,
    )
    yield client
    client.connection().close()


def seed(client, path, count=3):
    return [
        store(
            client.connection(), f"note {i}",
            embed_fn=lambda _: (struct.pack("2f", 1, 0), "test", "test", 2),
            source_type="skill_learning" if path == "skill" else "conversation",
            metadata={"skill": "A", "kind": "tip"} if path == "skill" else {},
        )
        for i in range(count)
    ]


async def run(client, path, *, dry_run):
    if path == "skill":
        return await client.consolidate_skill_memories(dry_run=dry_run)
    return await client.consolidate(dry_run=dry_run, dedup_first=path != "thematic")


@pytest.mark.parametrize("path", ["general", "skill", "thematic"])
@pytest.mark.parametrize("old_behavior", [False, True], ids=["fixed", "negative-control"])
async def test_clustering_releases_db_and_write_locks(client, monkeypatch, path, old_behavior):
    seed(client, path)
    entered, release = threading.Event(), threading.Event()
    real = cluster.cluster_by_similarity
    fetch = cluster.fetch_embedding_rows

    def checked_fetch(*args):
        assert client._db_lock._is_owned()
        return fetch(*args)

    monkeypatch.setattr(cluster, "fetch_embedding_rows", checked_fetch)

    def stalled(*args, **kwargs):
        assert kwargs["embedding_rows"]
        assert kwargs["scope_acl"] is True

        def compute():
            entered.set()
            assert release.wait(5)
            return real(*args, **kwargs)

        if old_behavior:
            # Reintroduce the old lock scope around actual clustering.
            with client._db_lock, client._write_lock:
                return compute()
        return compute()

    monkeypatch.setattr(cluster, "cluster_by_similarity", stalled)
    task = asyncio.create_task(run(client, path, dry_run=True))
    try:
        assert await asyncio.to_thread(entered.wait, 5)

        def probe(lock):
            acquired = lock.acquire(timeout=0.15)
            if acquired:
                lock.release()
            return acquired

        available = await asyncio.gather(
            asyncio.to_thread(probe, client._db_lock),
            asyncio.to_thread(probe, client._write_lock),
        )
        assert available == ([False, False] if old_behavior else [True, True])
    finally:
        release.set()
        await task


@pytest.mark.parametrize("path", ["general", "skill"])
@pytest.mark.parametrize("old_behavior", [False, True], ids=["fixed", "negative-control"])
async def test_index_contention_keeps_timer_responsive(client, monkeypatch, path, old_behavior):
    seed(client, path)
    loop = asyncio.get_running_loop()
    start, held, released = threading.Event(), threading.Event(), threading.Event()
    pulse = loop.create_future()
    real = cluster.cluster_by_similarity
    removed = []
    client._index = SimpleNamespace(built=True, remove=removed.append)

    def holder():
        assert start.wait(5)
        with client._index_lock:
            held.set()
            released.wait(0.6)
            released.set()

    thread = threading.Thread(target=holder)
    thread.start()

    def clustered(*args, **kwargs):
        result = real(*args, **kwargs)
        start.set()
        assert held.wait(5)
        scheduled = time.monotonic()
        loop.call_soon_threadsafe(
            lambda: loop.call_later(0.05, lambda: pulse.set_result(
                (time.monotonic() - scheduled, not released.is_set())
            ))
        )
        return result

    monkeypatch.setattr(cluster, "cluster_by_similarity", clustered)
    if old_behavior:
        write = client._write_locked

        async def blocking_write(fn):
            if fn.__name__ == "_write":
                # Old coroutine-side acquisition of the contended threading RLock.
                with client._index_lock:
                    pass
            return await write(fn)

        monkeypatch.setattr(client, "_write_locked", blocking_write)
    try:
        await run(client, path, dry_run=False)
        elapsed, before_release = await pulse
        assert before_release is (not old_behavior)
        if old_behavior:
            assert elapsed > 0.15
        else:
            assert elapsed <= 0.15
        assert len(removed) == 2
    finally:
        start.set()
        released.set()
        await asyncio.to_thread(thread.join, 5)
        assert not thread.is_alive()


@pytest.mark.parametrize("path", ["general", "skill"])
def test_candidate_bound_keeps_oldest(client, path):
    seed(client, path, MAX_DEDUP_CANDIDATES + 1)
    raws = _candidate_raws_for_dedup(
        client.connection(), agent_id="default", lookback_days=None,
        skill_scope="A" if path == "skill" else None,
    )
    assert MAX_DEDUP_CANDIDATES == 1000
    assert len(raws) == MAX_DEDUP_CANDIDATES
    assert [a["content"] for a in raws] == [f"note {i}" for i in range(1000)]


@pytest.mark.parametrize("path", ["general", "skill"])
async def test_sql_and_index_batch_share_worker_locks(client, monkeypatch, path):
    seed(client, path)
    loop_thread = threading.get_ident()
    removed = []

    def remove(atom_id):
        assert threading.get_ident() != loop_thread
        assert client._db_lock._is_owned()
        assert client._write_lock.locked()
        assert client._index_lock._is_owned()
        assert client.connection().execute(
            "SELECT count(*) FROM atoms WHERE tombstoned = 1"
        ).fetchone()[0] == 2
        removed.append(atom_id)

    client._index = SimpleNamespace(built=True, remove=remove)
    from mimir.saga import dedup
    real = dedup.dedup_pass

    def checked_pass(*args, **kwargs):
        assert client._index_lock._is_owned()
        return real(*args, **kwargs)

    monkeypatch.setattr(dedup, "dedup_pass", checked_pass)
    await run(client, path, dry_run=False)
    assert len(removed) == 2


@pytest.mark.parametrize("path", ["general", "skill"])
@pytest.mark.parametrize("change", ["owner_principal = 'other'", "tombstoned = 1"])
async def test_snapshot_candidates_revalidated_before_write(client, monkeypatch, path, change):
    seed(client, path, 2)
    entered, release = threading.Event(), threading.Event()
    real = cluster.cluster_by_similarity

    def stalled(*args, **kwargs):
        result = real(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(cluster, "cluster_by_similarity", stalled)
    task = asyncio.create_task(run(client, path, dry_run=False))
    try:
        assert await asyncio.to_thread(entered.wait, 5)

        def change_candidate():
            conn = client.connection()
            conn.execute(f"UPDATE atoms SET {change} WHERE content = 'note 1'")
            conn.commit()

        await client._write_locked(change_candidate)
    finally:
        release.set()
        result = await task
    summary = result["skills"]["A"] if path == "skill" else result["dedup"]
    assert summary["duplicates_tombstoned"] == []
    assert client.connection().execute(
        "SELECT tombstoned FROM atoms WHERE content = 'note 0'"
    ).fetchone()[0] == 0
