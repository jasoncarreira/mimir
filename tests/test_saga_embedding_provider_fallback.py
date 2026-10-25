"""Tests for the API-keyed → onnx fallback in ``mimir.saga.embeddings.get_provider``."""

from __future__ import annotations

import pytest
import sqlite3


@pytest.fixture(autouse=True)
def _reset_provider_singleton() -> None:
    import mimir.saga.embeddings as emb
    emb._provider_instance = None
    yield
    emb._provider_instance = None


@pytest.fixture
def fake_cfg(monkeypatch: pytest.MonkeyPatch):
    def _install(embedding: dict) -> None:
        def accessor(section, key, default=None):
            if section == "embedding" and key in embedding:
                return embedding[key]
            return default

        import mimir.saga._config_io as cfg_io
        import mimir.saga.embeddings as emb

        monkeypatch.setattr(cfg_io, "get_config", lambda: accessor)
        monkeypatch.setattr(emb, "_cfg", accessor)
        monkeypatch.setattr(cfg_io, "was_set_in_toml", lambda s, k: False)

    return _install


def test_fallback_uses_onnx_default_when_model_inherited(
    monkeypatch: pytest.MonkeyPatch, fake_cfg,
) -> None:
    """No API key, default config → fallback lands on the onnx default."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    fake_cfg(embedding={
        "provider": "voyage",
        "model": "voyage-4-lite",
        "api_key_env": "VOYAGE_API_KEY",
    })

    from mimir.saga.embeddings import get_provider, ONNXProvider

    provider = get_provider()
    assert isinstance(provider, ONNXProvider)
    assert provider.model_name == "BAAI/bge-small-en-v1.5"


def test_keyless_fallback_warns_once_and_refuses_mixed_dimensions(tmp_path, monkeypatch, fake_cfg, caplog):
    import mimir.saga.embeddings as emb
    db = tmp_path / "saga.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE embeddings (dim INTEGER)")
        conn.execute("INSERT INTO embeddings VALUES (1024)")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    fake_cfg({"provider": "voyage", "api_key_env": "VOYAGE_API_KEY"})
    original = emb._cfg
    monkeypatch.setattr(emb, "_cfg", lambda section, key, default=None:
                        str(db) if (section, key) == ("storage", "db_path") else original(section, key, default))
    with pytest.raises(RuntimeError, match="mimir saga-reembed"):
        emb.get_provider()
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM embeddings")
    with caplog.at_level("WARNING"):
        assert emb.get_provider().provider_name == "onnx"
        emb.get_provider()
    assert caplog.text.count("VOYAGE_API_KEY is unset") == 1


def test_explicit_provider_cannot_mix_dimensions_but_offline_reembed_can(tmp_path, monkeypatch, fake_cfg):
    import mimir.saga.embeddings as emb
    db = tmp_path / "saga.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE embeddings (dim INTEGER)")
        conn.execute("INSERT INTO embeddings VALUES (1024)")
    fake_cfg({"provider": "onnx", "model": "BAAI/bge-small-en-v1.5", "dimensions": 384})
    original = emb._cfg
    monkeypatch.setattr(emb, "_cfg", lambda section, key, default=None:
                        str(db) if (section, key) == ("storage", "db_path") else original(section, key, default))
    with pytest.raises(RuntimeError, match="mimir saga-reembed"):
        emb.get_provider()
    with emb.offline_reembedding():
        assert emb.get_provider().dimensions() == 384


@pytest.mark.parametrize("matching_table", ["embeddings", "sessions", "triples"])
def test_existing_mixed_dimensions_keep_provider_and_warn_once(
    tmp_path, monkeypatch, fake_cfg, caplog, matching_table,
):
    """Legacy drift must not disable the unique plurality provider dimension."""
    import mimir.saga.embeddings as emb

    db = tmp_path / "saga.db"
    with sqlite3.connect(db) as conn:
        for table, column in (("embeddings", "dim"), ("sessions", "embedding_dim"),
                              ("triples", "embedding_dim")):
            conn.execute(f"CREATE TABLE {table} ({column} INTEGER)")
            # Four 1024 rows beat three rows at each stale dimension across
            # all tables (a plurality, not necessarily an absolute majority).
            dims = [384, *([1024] * 4), 1536] if table == matching_table else [384, 1536]
            conn.executemany(f"INSERT INTO {table} VALUES (?)", [(dim,) for dim in dims])
    fake_cfg({"provider": "voyage", "dimensions": 1024})
    monkeypatch.setenv("VOYAGE_API_KEY", "fixture-key")
    original = emb._cfg
    monkeypatch.setattr(emb, "_cfg", lambda section, key, default=None:
                        str(db) if (section, key) == ("storage", "db_path") else original(section, key, default))
    with caplog.at_level("WARNING", logger="saga.embeddings"):
        provider = emb.get_provider()
        assert emb.get_provider() is provider
    assert provider.dimensions() == 1024
    warnings = [r for r in caplog.records if "mismatched embedding dimensions" in r.message]
    assert len(warnings) == 1
    assert "6 rows" in warnings[0].message
    assert "mimir saga-reembed" in warnings[0].message


@pytest.mark.parametrize("table,column", [
    ("embeddings", "dim"), ("sessions", "embedding_dim"), ("triples", "embedding_dim"),
])
def test_provider_switch_without_matching_vectors_is_refused(tmp_path, monkeypatch, fake_cfg, table, column):
    import mimir.saga.embeddings as emb

    db = tmp_path / "saga.db"
    with sqlite3.connect(db) as conn:
        conn.execute(f"CREATE TABLE {table} ({column} INTEGER)")
        conn.execute(f"INSERT INTO {table} VALUES (384)")
    fake_cfg({"provider": "voyage", "dimensions": 1024})
    monkeypatch.setenv("VOYAGE_API_KEY", "fixture-key")
    original = emb._cfg
    monkeypatch.setattr(emb, "_cfg", lambda section, key, default=None:
                        str(db) if (section, key) == ("storage", "db_path") else original(section, key, default))
    with pytest.raises(RuntimeError, match="mimir saga-reembed"):
        emb.get_provider()
    assert emb._provider_instance is None


def test_keyless_fallback_mixed_dimensions_warns_only_once(tmp_path, monkeypatch, fake_cfg, caplog):
    import mimir.saga.embeddings as emb

    db = tmp_path / "saga.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE embeddings (dim INTEGER)")
        conn.executemany("INSERT INTO embeddings VALUES (?)", [(384,), (384,), (1024,), (1536,)])
    fake_cfg({"provider": "voyage"})
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    original = emb._cfg
    monkeypatch.setattr(emb, "_cfg", lambda section, key, default=None:
                        str(db) if (section, key) == ("storage", "db_path") else original(section, key, default))
    with caplog.at_level("WARNING", logger="saga.embeddings"):
        assert emb.get_provider().dimensions() == 384
        emb.get_provider()
    assert caplog.text.count("mismatched embedding dimensions") == 1
    assert caplog.text.count("VOYAGE_API_KEY is unset") == 1


@pytest.fixture
def dimension_database(tmp_path, monkeypatch, fake_cfg):
    """A test-owned database using the real provider initialization path."""
    import mimir.saga.embeddings as emb

    db = tmp_path / "saga.db"

    def install(rows, provider_dim):
        with sqlite3.connect(db) as conn:
            for (table, column), dims in zip(
                (("embeddings", "dim"), ("sessions", "embedding_dim"),
                 ("triples", "embedding_dim")), rows, strict=True,
            ):
                conn.execute(f"CREATE TABLE {table} ({column} INTEGER)")
                conn.executemany(f"INSERT INTO {table} VALUES (?)", [(d,) for d in dims])
        fake_cfg({"provider": "voyage", "dimensions": provider_dim})
        monkeypatch.setenv("VOYAGE_API_KEY", "fixture-key")
        original = emb._cfg
        monkeypatch.setattr(emb, "_cfg", lambda section, key, default=None:
                            str(db) if (section, key) == ("storage", "db_path")
                            else original(section, key, default))
        return emb

    return install


@pytest.mark.parametrize("provider_dim", [1024, 384, 1536, 3072])
def test_legacy_minority_rows_do_not_permit_provider_switch(dimension_database, caplog, provider_dim):
    """Muninn-shaped drift: minority matching rows are not switch authority."""
    emb = dimension_database(
        ([1024] * 3549 + [384] * 167 + [1536] * 27,
         [1024] * 100 + [None] * 10, [1024] * 100),
        provider_dim,
    )
    with caplog.at_level("WARNING", logger="saga.embeddings"):
        if provider_dim == 1024:
            provider = emb.get_provider()
            assert emb.get_provider() is provider
            warnings = [r.message for r in caplog.records if "mismatched embedding dimensions" in r.message]
            assert len(warnings) == 1
            assert "194 rows" in warnings[0]
            assert "mimir saga-reembed" in warnings[0]
        else:
            with pytest.raises(RuntimeError, match="mimir saga-reembed"):
                emb.get_provider()
            assert emb._provider_instance is None
            assert not caplog.records


@pytest.mark.parametrize("provider_dim", [384, 1024])
def test_dominant_dimension_is_summed_across_tables(dimension_database, provider_dim):
    # Embeddings alone favor 384; session and triple counts make 1024 win.
    emb = dimension_database(([384] * 5, [1024] * 3, [1024] * 3), provider_dim)
    if provider_dim == 1024:
        assert emb.get_provider().dimensions() == 1024
    else:
        with pytest.raises(RuntimeError, match="mimir saga-reembed"):
            emb.get_provider()
        assert emb._provider_instance is None


@pytest.mark.parametrize("rows", [
    ([384, 384], [1024], [1024, None]),
    ([384], [1024], [1536]),
])
@pytest.mark.parametrize("provider_dim", [384, 1024, 1536, 3072])
def test_tied_dimensions_refuse_every_provider(dimension_database, rows, provider_dim):
    emb = dimension_database(rows, provider_dim)
    with pytest.raises(RuntimeError, match="ambiguous.*mimir saga-reembed"):
        emb.get_provider()
    assert emb._provider_instance is None


@pytest.mark.parametrize("rows", [([], [], []), ([None], [None, None], [None])])
def test_empty_or_null_dimensions_allow_provider(dimension_database, caplog, rows):
    emb = dimension_database(rows, 1024)
    with caplog.at_level("WARNING", logger="saga.embeddings"):
        assert emb.get_provider().dimensions() == 1024
    assert not caplog.records


def test_fallback_uses_onnx_default_when_model_explicitly_voyage(
    monkeypatch: pytest.MonkeyPatch, fake_cfg,
) -> None:
    """Operator-configured voyage model must not leak into fastembed."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    fake_cfg(embedding={
        "provider": "voyage",
        "model": "voyage-4-lite",
        "api_key_env": "VOYAGE_API_KEY",
    })

    from mimir.saga.embeddings import get_provider, ONNXProvider

    provider = get_provider()
    assert isinstance(provider, ONNXProvider)
    assert provider.model_name == "BAAI/bge-small-en-v1.5"


def test_no_fallback_when_api_key_set(
    monkeypatch: pytest.MonkeyPatch, fake_cfg,
) -> None:
    """API key present → VoyageProvider with the operator's model."""
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key-not-real")
    fake_cfg(embedding={
        "provider": "voyage",
        "model": "voyage-4-lite",
        "api_key_env": "VOYAGE_API_KEY",
    })

    from mimir.saga.embeddings import get_provider, VoyageProvider

    provider = get_provider()
    assert isinstance(provider, VoyageProvider)
    assert provider.model == "voyage-4-lite"


def test_onnx_provider_direct_unaffected(
    monkeypatch: pytest.MonkeyPatch, fake_cfg,
) -> None:
    """``provider = "onnx"`` directly (no fallback) → default model applies."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    fake_cfg(embedding={
        "provider": "onnx",
        "api_key_env": "VOYAGE_API_KEY",
    })

    from mimir.saga.embeddings import get_provider, ONNXProvider

    provider = get_provider()
    assert isinstance(provider, ONNXProvider)
    assert provider.model_name == "BAAI/bge-small-en-v1.5"


def test_fallback_overrides_dimensions_to_match_onnx_model(
    monkeypatch: pytest.MonkeyPatch, fake_cfg,
) -> None:
    """Fallback must override ``dimensions`` to the ONNX model's native
    dim, not the API provider's. Reviewer repro: with
    ``model = "voyage-4-lite" + dimensions = 1024`` and no API key,
    pre-fix returned a provider with 1024-d ``dimensions()`` but
    384-d vectors, crashing ``struct.pack(f"{dim}f", *vec)`` callers."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    fake_cfg(embedding={
        "provider": "voyage",
        "model": "voyage-4-lite",
        "dimensions": 1024,
        "api_key_env": "VOYAGE_API_KEY",
    })

    from mimir.saga.embeddings import get_provider

    provider = get_provider()
    assert provider.dimensions() == 384


def test_onnx_provider_direct_honors_configured_dimensions(
    monkeypatch: pytest.MonkeyPatch, fake_cfg,
) -> None:
    """Non-fallback path: ``provider = "onnx"`` honors the operator's
    ``dimensions`` config (e.g. for a different onnx model)."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    fake_cfg(embedding={
        "provider": "onnx",
        "dimensions": 768,
        "api_key_env": "VOYAGE_API_KEY",
    })

    from mimir.saga.embeddings import get_provider

    provider = get_provider()
    assert provider.dimensions() == 768


# ─── #493: provenance read off the LIVE provider, not config ───────────


def test_provider_provenance_reflects_onnx_fallback(
    monkeypatch: pytest.MonkeyPatch, fake_cfg,
) -> None:
    """#493: after the keyless fallback, the live provider reports onnx / the
    BGE model — so embedding rows aren't mislabeled voyage/voyage-4-lite."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    fake_cfg(embedding={
        "provider": "voyage",
        "model": "voyage-4-lite",
        "api_key_env": "VOYAGE_API_KEY",
    })

    from mimir.saga.embeddings import get_provider

    provider = get_provider()
    assert provider.provider_name == "onnx"
    assert provider.model_id == "BAAI/bge-small-en-v1.5"


def test_provider_provenance_voyage_when_keyed(
    monkeypatch: pytest.MonkeyPatch, fake_cfg,
) -> None:
    """#493: with the key set, provenance reports voyage / the configured model."""
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key-not-real")
    fake_cfg(embedding={
        "provider": "voyage",
        "model": "voyage-4-lite",
        "api_key_env": "VOYAGE_API_KEY",
    })

    from mimir.saga.embeddings import get_provider

    provider = get_provider()
    assert provider.provider_name == "voyage"
    assert provider.model_id == "voyage-4-lite"


def test_embed_text_sync_stamps_live_provider_not_config(
    monkeypatch: pytest.MonkeyPatch, fake_cfg,
) -> None:
    """#493: _embed_text_sync records provider/model from the live provider
    instance — a configured voyage that fell back to onnx must NOT stamp rows
    voyage/voyage-4-lite over the BGE vectors."""
    fake_cfg(embedding={
        "provider": "voyage",
        "model": "voyage-4-lite",
        "max_input_chars": 2000,
    })

    class _FakeOnnx:
        provider_name = "onnx"
        model_id = "BAAI/bge-small-en-v1.5"

        def embed(self, text, input_type="passage"):
            return [0.1, 0.2, 0.3]

        def dimensions(self):
            return 3

    import mimir.saga.embeddings as emb
    monkeypatch.setattr(emb, "get_provider", lambda: _FakeOnnx())

    from mimir.saga.client import _embed_text_sync

    _vec, provider_name, model, dim = _embed_text_sync("hello world")
    assert provider_name == "onnx"
    assert model == "BAAI/bge-small-en-v1.5"
    assert dim == 3
