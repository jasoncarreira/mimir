"""Tests for the API-keyed → onnx fallback in ``mimir.saga.embeddings.get_provider``."""

from __future__ import annotations

import pytest


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
