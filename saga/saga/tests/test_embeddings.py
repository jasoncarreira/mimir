"""SAGA Embeddings Tests -- provider interface, ONNX, retry, batch."""

import sys
import os
import pytest



class TestProviderRegistry:
    def test_all_providers_registered(self):
        from saga.embeddings import _PROVIDERS
        assert "nvidia-nim" in _PROVIDERS
        assert "openai" in _PROVIDERS
        assert "onnx" in _PROVIDERS
        assert "local" in _PROVIDERS

    def test_provider_classes_instantiate(self):
        from saga.embeddings import NvidiaNimProvider, OpenAIProvider, ONNXProvider, LocalProvider
        # Just test they can be created (no API calls)
        nim = NvidiaNimProvider()
        assert nim.model == "nvidia/nv-embedqa-e5-v5"
        oai = OpenAIProvider()
        assert oai.url is not None  # URL comes from config
        onnx = ONNXProvider()
        assert onnx.model_name is not None  # model name comes from config

    def test_base_class_batch_default(self):
        from saga.embeddings import EmbeddingProvider
        provider = EmbeddingProvider()
        with pytest.raises(NotImplementedError):
            provider.batch_embed(["test"])


class TestRetry:
    def test_retry_succeeds_on_first_try(self):
        from saga.embeddings import _retry_with_backoff
        result = _retry_with_backoff(lambda: "ok")
        assert result == "ok"

    def test_retry_retries_on_failure(self):
        import requests
        from saga.embeddings import _retry_with_backoff
        call_count = [0]
        
        def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise requests.exceptions.ConnectionError("transient")
            return "recovered"
        
        result = _retry_with_backoff(flaky, max_retries=3, base_delay=0.01)
        assert result == "recovered"
        assert call_count[0] == 3

    def test_retry_gives_up(self):
        import requests
        from saga.embeddings import _retry_with_backoff
        
        def always_fail():
            raise requests.exceptions.ConnectionError("permanent")
        
        with pytest.raises(requests.exceptions.ConnectionError):
            _retry_with_backoff(always_fail, max_retries=2, base_delay=0.01)


class TestONNXProvider:
    def test_onnx_import(self):
        from saga.embeddings import ONNXProvider
        provider = ONNXProvider()
        # dimensions() reads from config; default may be 1024 if config has nvidia-nim
        assert provider.dimensions() > 0

    def test_onnx_lazy_load(self):
        """ONNXProvider now wraps fastembed (shared cache with mimir's
        file_search). The previous urllib-based downloader + custom
        _get_model_dir is gone. Confirms init doesn't immediately
        load the model (lazy load happens on first embed call)."""
        from saga.embeddings import ONNXProvider
        provider = ONNXProvider()
        assert provider._model is None
        # model_name comes from config (defaults to nvidia/... unless
        # the [embedding] toml overrides it); the actual value isn't
        # what we're testing — just that __init__ stays cheap.
        assert isinstance(provider.model_name, str)


class TestProviderFallback:
    """When the configured provider needs an API key but the env var
    isn't set, get_provider() should silently fall back to onnx
    (fastembed) instead of letting the missing-key error surface on
    the first embed call. Lets fresh ``mimir setup``-only installs
    work without an OpenAI key."""

    def test_openai_falls_back_when_key_missing(self, monkeypatch):
        import saga.config as cfg_mod
        import saga.embeddings as emb
        snapshot = {
            "embedding": {
                "provider": "openai",
                "api_key_env": "OPENAI_API_KEY",
                "model": "text-embedding-3-small",
                "dimensions": 1536,
            }
        }
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(emb, "_provider_instance", None)

        provider = emb.get_provider()
        assert isinstance(provider, emb.ONNXProvider)

    def test_openai_used_when_key_present(self, monkeypatch):
        import saga.config as cfg_mod
        import saga.embeddings as emb
        snapshot = {
            "embedding": {
                "provider": "openai",
                "api_key_env": "OPENAI_API_KEY",
                "model": "text-embedding-3-small",
                "dimensions": 1536,
            }
        }
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-test")
        monkeypatch.setattr(emb, "_provider_instance", None)

        provider = emb.get_provider()
        assert isinstance(provider, emb.OpenAIProvider)

    def test_nvidia_nim_falls_back_when_key_missing(self, monkeypatch):
        import saga.config as cfg_mod
        import saga.embeddings as emb
        snapshot = {
            "embedding": {
                "provider": "nvidia-nim",
                "api_key_env": "NVIDIA_NIM_API_KEY",
            }
        }
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
        monkeypatch.setattr(emb, "_provider_instance", None)

        provider = emb.get_provider()
        assert isinstance(provider, emb.ONNXProvider)


class TestOpenAIProviderInputType:
    """``send_input_type`` flag — required for Voyage AI compatibility.

    Voyage's models REQUIRE ``input_type`` ("query" / "document") on each
    request to produce retrieval-quality embeddings (training-time
    instruction prefixes). OpenAI's own API REJECTS unknown params, so
    the flag defaults False and must be explicitly enabled per
    deployment.

    These tests pin both halves of the contract:
    - Default off: payload matches the pre-patch shape (input + model).
    - Flag on: payload includes ``input_type``, with saga's internal
      ``"passage"`` mapped to voyage's accepted ``"document"`` and
      ``"query"`` passing through unchanged.
    """

    def _make_provider(self, monkeypatch, *, send_input_type):
        import saga.config as cfg_mod
        import saga.embeddings as emb
        snapshot = {
            "embedding": {
                "provider": "openai",
                "url": "https://api.example.com/v1/embeddings",
                "model": "test-model",
                "dimensions": 1024,
                "api_key_env": "TEST_API_KEY",
                "max_input_chars": 8000,
                "timeout_seconds": 10,
                "send_input_type": send_input_type,
            }
        }
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        monkeypatch.setenv("TEST_API_KEY", "fake-key-for-test")
        return emb.OpenAIProvider()

    def _stub_post(self, monkeypatch, captured: dict):
        """Capture POST kwargs; return a stub Response with one embedding."""
        import saga.embeddings as emb

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"embedding": [0.0] * 4, "index": 0}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _Resp()

        # Patch the ``requests`` module *inside the embeddings module* —
        # ``_call_api`` does a local ``import requests``, so monkeypatching
        # the top-level module attribute is what gets imported.
        import requests
        monkeypatch.setattr(requests, "post", fake_post)

    def test_default_excludes_input_type(self, monkeypatch):
        """``send_input_type`` defaults False → JSON payload matches
        the pre-patch shape (OpenAI-compatible)."""
        provider = self._make_provider(monkeypatch, send_input_type=False)
        assert provider.send_input_type is False
        captured: dict = {}
        self._stub_post(monkeypatch, captured)
        provider.embed("hello world", input_type="passage")
        assert captured["json"] == {
            "input": ["hello world"],
            "model": "test-model",
        }
        assert "input_type" not in captured["json"]

    def test_flag_on_maps_passage_to_document(self, monkeypatch):
        """``send_input_type`` True + ``input_type="passage"`` →
        ``"document"`` in the payload (voyage's accepted vocabulary)."""
        provider = self._make_provider(monkeypatch, send_input_type=True)
        assert provider.send_input_type is True
        captured: dict = {}
        self._stub_post(monkeypatch, captured)
        provider.embed("a doc", input_type="passage")
        assert captured["json"]["input_type"] == "document"
        assert captured["json"]["input"] == ["a doc"]
        assert captured["json"]["model"] == "test-model"

    def test_flag_on_passes_through_query(self, monkeypatch):
        """``send_input_type`` True + ``input_type="query"`` →
        ``"query"`` in the payload (voyage's accepted vocabulary)."""
        provider = self._make_provider(monkeypatch, send_input_type=True)
        captured: dict = {}
        self._stub_post(monkeypatch, captured)
        provider.embed("a question", input_type="query")
        assert captured["json"]["input_type"] == "query"

    def test_batch_embed_threads_input_type(self, monkeypatch):
        """``batch_embed`` must pass the same ``input_type`` through to
        ``_call_api`` — otherwise the saga consolidation path (which
        only uses batch_embed) would silently lose the prefix."""
        provider = self._make_provider(monkeypatch, send_input_type=True)
        captured: dict = {}
        self._stub_post(monkeypatch, captured)
        provider.batch_embed(["one", "two"], input_type="query")
        assert captured["json"]["input_type"] == "query"
        assert captured["json"]["input"] == ["one", "two"]


class TestVoyageProvider:
    """``VoyageProvider`` shortcut — sets voyage-friendly defaults so
    ``provider = "voyage"`` alone in saga.toml is enough config."""

    def _install_voyage_config(self, monkeypatch, **overrides):
        """Install a fake saga.toml state simulating ``provider="voyage"``
        plus any operator overrides. Sets both ``_config`` (merged
        values) and ``_explicit_keys`` (what the operator wrote in
        the toml) so ``was_set_in_toml`` returns the right answer
        for VoyageProvider's override gating.
        """
        import saga.config as cfg_mod
        snap = {"embedding": {"provider": "voyage"}}
        snap["embedding"].update(overrides)
        explicit = {"embedding": set(snap["embedding"].keys())}
        monkeypatch.setattr(cfg_mod, "_config", snap)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        monkeypatch.setattr(cfg_mod, "_explicit_keys", explicit)
        return snap

    def test_defaults_set_voyage_url_model_key_env(self, monkeypatch):
        import saga.embeddings as emb
        self._install_voyage_config(monkeypatch)
        provider = emb.VoyageProvider()
        assert provider.url == "https://api.voyageai.com/v1/embeddings"
        assert provider.model == "voyage-4-lite"
        assert provider.api_key_env == "VOYAGE_API_KEY"
        assert provider.send_input_type is True

    def test_operator_can_override_model(self, monkeypatch):
        """Explicit ``model`` in saga.toml beats the default."""
        import saga.embeddings as emb
        self._install_voyage_config(monkeypatch, model="voyage-3-large")
        provider = emb.VoyageProvider()
        assert provider.model == "voyage-3-large"
        assert provider.url == "https://api.voyageai.com/v1/embeddings"
        assert provider.send_input_type is True  # still forced True

    def test_send_input_type_false_is_overridden(self, monkeypatch):
        """Operator setting ``send_input_type=false`` for voyage would
        produce broken embeddings; the provider overrides to True
        regardless."""
        import saga.embeddings as emb
        self._install_voyage_config(monkeypatch, send_input_type=False)
        provider = emb.VoyageProvider()
        assert provider.send_input_type is True

    def test_url_default_survives_nvidia_nim_in_DEFAULTS(self, monkeypatch):
        """Regression for issue #149: minimal voyage saga.toml (no
        explicit ``url``) used to leave ``self.url`` pointing at
        nvidia-nim's endpoint because saga's ``_DEFAULTS`` defaults
        ``url`` to nvidia-nim's URL and the prior detection compared
        against OpenAI's URL. With ``was_set_in_toml``-gated overrides,
        the unset key correctly falls through to voyage's URL.
        """
        import saga.embeddings as emb
        self._install_voyage_config(monkeypatch)  # no explicit url
        provider = emb.VoyageProvider()
        assert provider.url == "https://api.voyageai.com/v1/embeddings"
        assert "nvidia" not in provider.url

    def test_operator_can_override_url(self, monkeypatch):
        """Explicit ``url`` (e.g. corporate proxy) beats the default."""
        import saga.embeddings as emb
        custom = "https://internal-voyage-proxy.example.com/v1/embeddings"
        self._install_voyage_config(monkeypatch, url=custom)
        provider = emb.VoyageProvider()
        assert provider.url == custom

    def test_operator_can_override_api_key_env(self, monkeypatch):
        """Explicit ``api_key_env`` lets operators point at a different
        env var (e.g. ``MY_VOYAGE_KEY``)."""
        import saga.embeddings as emb
        self._install_voyage_config(monkeypatch, api_key_env="MY_VOYAGE_KEY")
        provider = emb.VoyageProvider()
        assert provider.api_key_env == "MY_VOYAGE_KEY"

    def test_registered_in_provider_registry(self):
        from saga.embeddings import _PROVIDERS, VoyageProvider
        assert "voyage" in _PROVIDERS
        assert _PROVIDERS["voyage"] is VoyageProvider


class TestAutoThresholdResolution:
    """``resolve_auto_threshold`` produces per-provider recommendations
    when ``[consolidation] similarity_threshold = "auto"``."""

    def test_voyage_resolves_to_092(self):
        from saga.embeddings import resolve_auto_threshold
        assert resolve_auto_threshold("voyage") == 0.92

    def test_onnx_fastembed_resolves_to_092(self):
        from saga.embeddings import resolve_auto_threshold
        assert resolve_auto_threshold("onnx") == 0.92

    def test_openai_resolves_to_080(self):
        from saga.embeddings import resolve_auto_threshold
        assert resolve_auto_threshold("openai") == 0.80

    def test_nvidia_nim_resolves_to_080(self):
        from saga.embeddings import resolve_auto_threshold
        assert resolve_auto_threshold("nvidia-nim") == 0.80

    def test_unknown_provider_falls_back_to_080(self):
        from saga.embeddings import resolve_auto_threshold
        assert resolve_auto_threshold("does-not-exist") == 0.80


class TestConsolidationThresholdSentinel:
    """``[consolidation] similarity_threshold = "auto"`` flows through
    ``saga.consolidation._resolve_threshold`` to produce the per-
    provider value at module load time."""

    def test_auto_resolves_per_provider(self, monkeypatch):
        import saga.config as cfg_mod
        snapshot = {
            "embedding": {"provider": "voyage"},
            "consolidation": {"similarity_threshold": "auto"},
        }
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        from saga.consolidation import _resolve_threshold
        assert _resolve_threshold() == 0.92

    def test_numeric_passes_through(self, monkeypatch):
        import saga.config as cfg_mod
        snapshot = {
            "embedding": {"provider": "openai"},
            "consolidation": {"similarity_threshold": 0.85},
        }
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        from saga.consolidation import _resolve_threshold
        assert _resolve_threshold() == 0.85

    def test_malformed_falls_back_to_080(self, monkeypatch):
        """Unrecognized non-numeric strings ("high", "auto-magic", etc.)
        fall back to 0.80 rather than crashing."""
        import saga.config as cfg_mod
        snapshot = {
            "embedding": {"provider": "openai"},
            "consolidation": {"similarity_threshold": "highest"},
        }
        monkeypatch.setattr(cfg_mod, "_config", snapshot)
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        from saga.consolidation import _resolve_threshold
        assert _resolve_threshold() == 0.80
