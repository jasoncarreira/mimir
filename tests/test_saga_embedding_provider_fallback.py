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
