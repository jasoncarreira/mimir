"""Tests for the FAISS index dimension-resolution logic on a fresh DB
(pre-OSS review item #14).

The previous ``_ensure_index`` fallback was hardcoded to 1024 (Voyage
default). An operator running with a non-Voyage embedding provider
(OpenAI ``text-embedding-3-small`` at 1536, fastembed
``BAAI/bge-small-en-v1.5`` at 384, etc.) would have their first
``store()`` write a vector at the provider's dim into the embeddings
table — but the FAISS index was built at 1024, so every subsequent
incremental add silently dropped the vector. New users with
non-Voyage providers got invisible memory.

Fix: ``_ensure_index`` now asks the configured provider for
``dimensions()`` when the embeddings table is empty, matching the
existing ``_ensure_sessions_index`` behavior. If no provider is
available, returns None so the search falls back to FTS-only
rather than building an index at a guessed dim.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest


def _patch_provider(
    monkeypatch: pytest.MonkeyPatch, *, dimensions: int, raises: bool = False
) -> None:
    """Stub ``saga.embeddings.get_provider()`` to return a provider with
    the given ``dimensions``. ``raises=True`` makes ``get_provider``
    raise — simulates an unconfigured / unloadable provider."""

    class _StubProvider:
        def embed(self, text: str, *, model: str | None = None):  # noqa: ARG002
            return [0.0] * dimensions

        def dimensions(self) -> int:
            return dimensions

    def _factory():
        if raises:
            raise RuntimeError("provider not configured")
        return _StubProvider()

    monkeypatch.setattr("mimir.saga.embeddings.get_provider", _factory)
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: lambda s, k, d=None: {
            ("embedding", "max_input_chars"): 2000,
            ("embedding", "provider"): "stub",
            ("embedding", "model"): f"stub-{dimensions}d",
        }.get((s, k), d),
    )


# ─── empty DB + non-Voyage provider — the bug we're fixing ───────────


@pytest.mark.parametrize(
    "provider_dim, label",
    [
        (384, "fastembed BAAI/bge-small-en-v1.5"),
        (1024, "voyage (old hardcoded default)"),
        (1536, "OpenAI text-embedding-3-small"),
        (3072, "OpenAI text-embedding-3-large"),
    ],
)
def test_ensure_index_derives_dim_from_provider_on_empty_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_dim: int,
    label: str,  # noqa: ARG001 — readable in test report
) -> None:
    """The atoms FAISS index built on a fresh DB uses the provider's
    dimension, NOT a hardcoded fallback."""
    _patch_provider(monkeypatch, dimensions=provider_dim)

    from mimir.saga.client import SagaStore

    store = SagaStore(db_path=tmp_path / "test.saga.db")
    conn = store._ensure_conn()

    index = store._ensure_index(conn)
    assert index is not None
    assert index.dimension == provider_dim, (
        f"expected provider dim {provider_dim} on empty DB; got "
        f"{index.dimension}. The hardcoded-1024 fallback would have "
        f"failed this test for every non-Voyage provider."
    )


# ─── empty DB + provider unavailable — graceful fallback ─────────────


def test_ensure_index_returns_none_when_provider_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider can't be loaded AND DB is empty → ``_ensure_index``
    returns None so search degrades to FTS-only. Better than
    building an index at a guessed dim that would silently reject
    every future store()."""
    _patch_provider(monkeypatch, dimensions=384, raises=True)

    from mimir.saga.client import SagaStore

    store = SagaStore(db_path=tmp_path / "test.saga.db")
    conn = store._ensure_conn()

    index = store._ensure_index(conn)
    assert index is None
    # And the miss is cached so we don't keep retrying the provider.
    assert store._index_built is True


# ─── populated DB — row[0] takes precedence over provider ────────────


def test_ensure_index_uses_db_row_when_embeddings_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the embeddings table has rows, their stored ``dim`` is
    authoritative — the provider isn't consulted. Pins the precedence
    order so a provider config change doesn't accidentally
    invalidate an existing index."""
    # Provider claims a wrong dim; DB has the actual dim.
    _patch_provider(monkeypatch, dimensions=9999)

    from mimir.saga.client import SagaStore

    store = SagaStore(db_path=tmp_path / "test.saga.db")
    conn = store._ensure_conn()
    # Insert a fake embedding row at dim=384.
    import struct
    vec_bytes = struct.pack("384f", *([0.0] * 384))
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at) "
        "VALUES ('a1', 'x', 'h1', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO embeddings "
        "(atom_id, vec, provider, model, dim, embedded_at) "
        "VALUES ('a1', ?, 'p', 'm', 384, '2026-01-01T00:00:00Z')",
        (vec_bytes,),
    )
    conn.commit()

    index = store._ensure_index(conn)
    assert index is not None
    assert index.dimension == 384, (
        "embeddings.dim row should win over the provider — got "
        f"{index.dimension} instead of 384."
    )


# ─── pre-set _embedding_dim wins over both ───────────────────────────


def test_ensure_index_pre_set_dim_takes_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SagaStore constructed with ``embedding_dim=N`` uses N
    regardless of DB rows or provider. Pins the constructor-arg
    override path used by the bench harness."""
    _patch_provider(monkeypatch, dimensions=9999)

    from mimir.saga.client import SagaStore

    store = SagaStore(db_path=tmp_path / "test.saga.db", embedding_dim=4)
    conn = store._ensure_conn()

    index = store._ensure_index(conn)
    assert index is not None
    assert index.dimension == 4
