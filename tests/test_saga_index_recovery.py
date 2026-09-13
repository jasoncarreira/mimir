"""Issue #1701 C: a failed provider must not permanently disable atom recall."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mimir.saga.client import SagaStore


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    store = SagaStore(db_path=tmp_path / "saga.db")
    conn = store.connection()
    provider = Mock(side_effect=[RuntimeError("secret-provider-detail"),
                                 SimpleNamespace(dimensions=lambda: 3)])
    monkeypatch.setattr("mimir.saga.embeddings.get_provider", provider)
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: lambda section, key, default=None: (
            "stub" if (section, key) == ("embedding", "provider") else default
        ),
    )
    events = Mock()
    monkeypatch.setattr("mimir.event_logger.log_event_sync", events)
    yield store, conn, provider, events
    conn.close()


@pytest.mark.parametrize("failure_site", ["factory", "dimensions"])
def test_provider_failure_retries_without_latching(recovery, failure_site, caplog):
    store, conn, provider, events = recovery
    if failure_site == "dimensions":
        provider.side_effect = [
            SimpleNamespace(dimensions=Mock(side_effect=RuntimeError("secret-provider-detail"))),
            SimpleNamespace(dimensions=lambda: 3),
        ]
    assert store._ensure_index(conn) is None
    assert store._index_built is False
    assert store._embedding_dim is None
    events.assert_called_once_with(
        "saga_vector_index_degraded", reason="provider_unavailable",
        error_type="RuntimeError",
    )
    assert "secret-provider-detail" not in caplog.text
    index = store._ensure_index(conn)
    assert index is not None and index.built
    assert index.dimension == 3
    assert store._index_built is True
    assert store._ensure_index(conn) is index
    assert provider.call_count == 2


def test_unconfigured_embeddings_are_not_reported_as_provider_failure(recovery, monkeypatch):
    store, conn, _, events = recovery
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config", lambda: lambda *args: None,
    )
    assert store._ensure_index(conn) is None
    assert not store._index_built
    events.assert_called_once_with(
        "saga_vector_index_degraded", reason="embeddings_unconfigured",
        error_type="RuntimeError",
    )
    assert store._ensure_index(conn).built


def test_omitted_provider_uses_default_and_reports_failure(recovery, monkeypatch):
    store, conn, _, events = recovery
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: lambda section, key, default=None: default,
    )
    assert store._ensure_index(conn) is None
    events.assert_called_once_with(
        "saga_vector_index_degraded", reason="provider_unavailable",
        error_type="RuntimeError",
    )


@pytest.mark.parametrize("legacy_latch", [False, True])
async def test_maintenance_recovers_missing_index(recovery, legacy_latch):
    store, conn, provider, _ = recovery
    assert store._ensure_index(conn) is None
    store._index_built = legacy_latch
    assert await store.rebuild_index_if_needed() is True
    assert store._index.built
    assert provider.call_count == 2
    assert await store.rebuild_index_if_needed() is False


def test_explicit_rebuild_clears_latch_under_ordered_locks(recovery, monkeypatch):
    store, conn, _, _ = recovery
    assert store._ensure_index(conn) is None
    store._index_built = True
    held = []

    @contextmanager
    def lock(name, expected):
        assert held == expected
        held.append(name)
        try:
            yield
        finally:
            assert held.pop() == name

    for name, expected in [("db", []), ("write", ["db"]),
                           ("index", ["db", "write"])]:
        monkeypatch.setattr(store, f"_{name}_lock", lock(name, expected))
    ensure = store._ensure_index

    def checked_ensure(connection):
        assert held == ["db", "write", "index"]
        assert not store._index_built
        return ensure(connection)

    monkeypatch.setattr(store, "_ensure_index", checked_ensure)
    store.rebuild_index()
    assert store._index.built
    assert held == []


def test_old_latch_negative_control_prevents_lazy_recovery(recovery, monkeypatch):
    """Reintroduce only the old miss latch: the recovered provider is never tried."""
    store, conn, provider, _ = recovery
    ensure = store._ensure_index

    def old_ensure(connection):
        index = ensure(connection)
        if index is None:
            store._index_built = True
        return index

    monkeypatch.setattr(store, "_ensure_index", old_ensure)
    assert store._ensure_index(conn) is None
    assert store._ensure_index(conn) is None
    assert provider.call_count == 1
