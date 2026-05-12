"""SAGA Config Tests -- configuration loader and accessor."""

import pytest


@pytest.fixture(autouse=True)
def reset_config():
    """Reset config singleton before each test."""
    import saga.config as cfg_mod
    cfg_mod._config = None
    cfg_mod._config_loaded = False
    yield
    cfg_mod._config = None
    cfg_mod._config_loaded = False


class TestGetConfig:
    def test_returns_callable(self):
        from saga.config import get_config
        cfg = get_config()
        assert callable(cfg)

    def test_returns_defaults(self):
        from saga.config import get_config
        cfg = get_config()
        assert cfg('embedding', 'dimensions') == 1024
        assert cfg('storage', 'token_budget_ceiling') == 40000
        assert cfg('decay', 'active_to_fading_threshold') == 0.3

    def test_raises_on_missing(self):
        from saga.config import get_config
        cfg = get_config()
        with pytest.raises(KeyError):
            cfg('nonexistent_section', 'nonexistent_key')

    def test_default_fallback(self):
        from saga.config import get_config
        cfg = get_config()
        result = cfg('nonexistent', 'key', 'my_default')
        assert result == 'my_default'

    def test_nested_section(self):
        from saga.config import get_config
        cfg = get_config()
        aliases = cfg('entity_resolution', 'aliases', {})
        assert isinstance(aliases, dict)


class TestDeepMerge:
    def test_nested_override(self):
        from saga.config import _deep_merge
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        override = {"a": {"y": 99, "z": 100}}
        result = _deep_merge(base, override)
        assert result["a"]["x"] == 1
        assert result["a"]["y"] == 99
        assert result["a"]["z"] == 100
        assert result["b"] == 3

    def test_non_dict_override(self):
        from saga.config import _deep_merge
        base = {"a": {"x": 1}}
        override = {"a": "replaced"}
        result = _deep_merge(base, override)
        assert result["a"] == "replaced"


class TestReloadConfig:
    def test_resets_singleton(self):
        from saga.config import get_config, reload_config
        cfg1 = get_config()
        cfg2 = reload_config()
        # Both should work, reload returns a fresh accessor
        assert callable(cfg1)
        assert callable(cfg2)
        assert cfg2('embedding', 'dimensions') == 1024


class TestWasSetInToml:
    """``was_set_in_toml`` — distinguishes operator-written keys from
    ``_DEFAULTS``-inherited values. Used by VoyageProvider for
    override gating (issue #149)."""

    def _write_toml_and_reload(self, tmp_path, monkeypatch, body: str):
        toml_path = tmp_path / "saga.toml"
        toml_path.write_text(body)
        monkeypatch.setenv("SAGA_CONFIG", str(toml_path))
        from saga.config import reload_config
        reload_config()

    def test_explicit_key_returns_true(self, tmp_path, monkeypatch):
        self._write_toml_and_reload(tmp_path, monkeypatch, """
            [embedding]
            provider = "voyage"
            url = "https://custom.example.com/v1/embeddings"
        """)
        from saga.config import was_set_in_toml
        assert was_set_in_toml("embedding", "url") is True
        assert was_set_in_toml("embedding", "provider") is True

    def test_defaults_inherited_key_returns_false(self, tmp_path, monkeypatch):
        """``_DEFAULTS["embedding"]`` has many keys (model, dimensions,
        etc.) — keys NOT written by the operator must NOT report as set,
        even though they show up in the merged config."""
        self._write_toml_and_reload(tmp_path, monkeypatch, """
            [embedding]
            provider = "voyage"
        """)
        from saga.config import was_set_in_toml, get_config
        cfg = get_config()
        # model IS in the merged config (inherited from _DEFAULTS)…
        assert cfg("embedding", "model") is not None
        # …but the operator didn't write it.
        assert was_set_in_toml("embedding", "model") is False
        assert was_set_in_toml("embedding", "url") is False

    def test_unknown_section_returns_false(self, tmp_path, monkeypatch):
        self._write_toml_and_reload(tmp_path, monkeypatch, """
            [embedding]
            provider = "voyage"
        """)
        from saga.config import was_set_in_toml
        assert was_set_in_toml("nonexistent_section", "anything") is False

    def test_nested_sections_use_dotted_path(self, tmp_path, monkeypatch):
        """``[llm.consolidation] model = ...`` registers under section
        ``"llm.consolidation"`` (with leaf key ``"model"``), not under
        section ``"llm"`` with key ``"consolidation"`` only. Lets
        provider subclasses gate on ``was_set_in_toml`` for nested
        config keys without false negatives. The one-level alias
        (``was_set_in_toml("llm", "consolidation")``) also returns True
        for back-compat.
        """
        self._write_toml_and_reload(tmp_path, monkeypatch, """
            [llm.consolidation]
            model = "gpt-4o"
            api_key_env = "OPENAI_API_KEY"
        """)
        from saga.config import was_set_in_toml
        # Dotted-path lookup works as intuited:
        assert was_set_in_toml("llm.consolidation", "model") is True
        assert was_set_in_toml("llm.consolidation", "api_key_env") is True
        assert was_set_in_toml("llm.consolidation", "did_not_set") is False
        # And the prior one-level form still works:
        assert was_set_in_toml("llm", "consolidation") is True

    def test_deeply_nested_section(self, tmp_path, monkeypatch):
        """Recursion handles arbitrary depth — not just one nested level."""
        self._write_toml_and_reload(tmp_path, monkeypatch, """
            [a.b.c]
            leaf = "x"
        """)
        from saga.config import was_set_in_toml
        assert was_set_in_toml("a.b.c", "leaf") is True
        assert was_set_in_toml("a.b", "c") is True
        assert was_set_in_toml("a", "b") is True

    def test_reload_resets_tracking(self, tmp_path, monkeypatch):
        """``reload_config`` must clear ``_explicit_keys`` so a second
        load reflects only the second toml's contents."""
        self._write_toml_and_reload(tmp_path, monkeypatch, """
            [embedding]
            url = "https://first.example.com"
        """)
        from saga.config import was_set_in_toml
        assert was_set_in_toml("embedding", "url") is True

        self._write_toml_and_reload(tmp_path, monkeypatch, """
            [embedding]
            provider = "voyage"
        """)
        # Old `url` should no longer be reported as set.
        assert was_set_in_toml("embedding", "url") is False
        assert was_set_in_toml("embedding", "provider") is True


class TestGetDataDir:
    def test_returns_path(self, tmp_path, monkeypatch):
        from saga.config import get_data_dir
        monkeypatch.setenv("SAGA_DATA_DIR", str(tmp_path / "test_data"))
        data_dir = get_data_dir()
        assert data_dir.exists()
        assert "test_data" in str(data_dir)

    def test_default_home(self, monkeypatch):
        from saga.config import get_data_dir
        monkeypatch.delenv("SAGA_DATA_DIR", raising=False)
        data_dir = get_data_dir()
        assert ".saga" in str(data_dir)


class TestGetRawConfig:
    def test_returns_dict(self):
        from saga.config import get_raw_config
        raw = get_raw_config()
        assert isinstance(raw, dict)
        assert "embedding" in raw
        assert "retrieval" in raw


class TestNewConfigSections:
    """Verify the newly-added config sections are accessible."""

    def test_compression_section(self):
        from saga.config import get_config
        cfg = get_config()
        assert cfg('compression', 'enable_subatom') is True
        assert cfg('compression', 'subatom_token_budget') == 120
        assert cfg('compression', 'dedup_similarity_threshold') == 0.85

    def test_comparison_section(self):
        from saga.config import get_config
        cfg = get_config()
        assert cfg('comparison', 'startup_files') == []
        assert cfg('comparison', 'query_files') == []

    def test_triples_resolves_via_llm_section(self):
        # [triples] no longer has its own LLM defaults; resolve_llm_config()
        # falls back to [llm] which carries the canonical defaults.
        from saga.config import resolve_llm_config
        out = resolve_llm_config('triples')
        assert "api.nvidia.com" in out['url']
        assert "mistral" in out['model']

    def test_llm_section(self):
        from saga.config import get_config
        cfg = get_config()
        assert "api.nvidia.com" in cfg('llm', 'url')
        assert "mistral" in cfg('llm', 'model')
        assert cfg('llm', 'api_key_env') == "NVIDIA_NIM_API_KEY"

    def test_retrieval_confidence_keys(self):
        from saga.config import get_config
        cfg = get_config()
        assert isinstance(cfg('retrieval', 'confidence_sim_high'), float)
        assert 0.0 < cfg('retrieval', 'confidence_sim_high') <= 1.0
        assert isinstance(cfg('retrieval', 'temporal_recency_hours'), (int, float))
        assert cfg('retrieval', 'temporal_recency_hours') > 0

    def test_confidence_sim_defaults_p33(self):
        """Pin the P33-recalibrated defaults so a future change is intentional.
        See NEXT-EXPERIMENTS.md P33 for the offline analysis behind these."""
        from saga.config import get_config
        cfg = get_config()
        assert cfg('retrieval', 'confidence_sim_high') == 0.40
        assert cfg('retrieval', 'confidence_sim_medium') == 0.30
        assert cfg('retrieval', 'confidence_sim_low') == 0.20

    def test_embedding_api_key(self):
        from saga.config import get_config
        cfg = get_config()
        assert cfg('embedding', 'api_key') is None


class TestUnknownKeyWarnings:
    """Validate that misnamed config keys are surfaced at load time.

    Real-world bug we're guarding against: Mimir's TOML had
    `cluster_similarity_threshold = 0.75` and `stability_reduction = 0.1`
    while consolidation reads `similarity_threshold` and
    `stability_reduction_factor`. Those typos silently fell through to
    defaults; the warning catches them.
    """

    def test_warns_unknown_key_in_known_section(self, caplog):
        import logging
        from saga.config import _warn_unknown_keys
        caplog.set_level(logging.WARNING, logger="saga.config")
        _warn_unknown_keys({
            "consolidation": {
                "cluster_similarity_threshold": 0.75,  # typo
                "similarity_threshold": 0.80,           # legit
            },
        })
        msgs = [r.message for r in caplog.records]
        assert any("cluster_similarity_threshold" in m for m in msgs)
        # The legit key shouldn't itself be the subject of a warning.
        # (It may appear inside a "did you mean…" suggestion — that's OK.)
        assert not any(
            "[consolidation] 'similarity_threshold'" in m for m in msgs
        )

    def test_suggests_closest_match(self, caplog):
        import logging
        from saga.config import _warn_unknown_keys
        caplog.set_level(logging.WARNING, logger="saga.config")
        _warn_unknown_keys({
            "consolidation": {"stability_reduction": 0.1},  # missing _factor
        })
        msgs = [r.message for r in caplog.records]
        assert any(
            "stability_reduction" in m and "stability_reduction_factor" in m
            for m in msgs
        )

    def test_skips_unknown_section(self, caplog):
        import logging
        from saga.config import _warn_unknown_keys
        caplog.set_level(logging.WARNING, logger="saga.config")
        _warn_unknown_keys({
            "experimental_feature": {"some_key": 42},
        })
        # Unknown sections aren't validated -- no warning.
        assert not caplog.records

    def test_known_extra_keys_dont_warn(self, caplog):
        import logging
        from saga.config import _warn_unknown_keys
        caplog.set_level(logging.WARNING, logger="saga.config")
        _warn_unknown_keys({
            "retrieval": {
                # Not in _DEFAULTS but registered in _KNOWN_EXTRA_KEYS
                "two_tier_enabled": True,
                "fusion": "rrf",
            },
        })
        assert not caplog.records

    def test_nested_tables_not_validated(self, caplog):
        """User-defined nested tables (entity_mappings, synonyms, etc.)
        should not trigger warnings on their internal keys."""
        import logging
        from saga.config import _warn_unknown_keys
        caplog.set_level(logging.WARNING, logger="saga.config")
        _warn_unknown_keys({
            "retrieval_v2": {
                "entity_mappings": {"foo_pattern": "Foo"},
            },
        })
        # `entity_mappings` is itself unknown? It's in _DEFAULTS, but
        # its value is a dict whose contents are user-defined.
        # The dict-skip logic should handle this without warning on
        # `foo_pattern`.
        msgs = [r.message for r in caplog.records]
        assert not any("foo_pattern" in m for m in msgs)

    def test_quiet_env_suppresses(self, caplog, monkeypatch):
        import logging
        from saga.config import _warn_unknown_keys
        monkeypatch.setenv("SAGA_QUIET_CONFIG", "1")
        caplog.set_level(logging.WARNING, logger="saga.config")
        _warn_unknown_keys({
            "consolidation": {"cluster_similarity_threshold": 0.75},
        })
        assert not caplog.records


class TestResolveLLMConfig:
    """Verify the unified LLM config helper picks up overrides correctly."""

    def test_falls_back_to_top_level_llm_section(self, monkeypatch):
        from saga.config import resolve_llm_config
        import saga.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_config", {
            "llm": {
                "url": "https://example.com/v1/chat",
                "model": "test-model",
                "api_key_env": "TEST_KEY",
                "timeout_seconds": 17,
            },
            "consolidation": {},
        })
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        monkeypatch.setenv("TEST_KEY", "secret-token")
        out = resolve_llm_config("consolidation")
        assert out["url"] == "https://example.com/v1/chat"
        assert out["model"] == "test-model"
        assert out["api_key"] == "secret-token"
        assert out["timeout"] == 17

    def test_subsystem_overrides_top_level(self, monkeypatch):
        from saga.config import resolve_llm_config
        import saga.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_config", {
            "llm": {
                "url": "https://default.example/v1/chat",
                "model": "default-model",
                "api_key_env": "DEFAULT_KEY",
            },
            "triples": {
                "llm_url": "https://triples.example/v1/chat",
                "llm_model": "triples-model",
            },
        })
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        monkeypatch.setenv("DEFAULT_KEY", "default-token")
        out = resolve_llm_config("triples")
        # triples overrides url + model
        assert out["url"] == "https://triples.example/v1/chat"
        assert out["model"] == "triples-model"
        # but inherits api_key_env from [llm]
        assert out["api_key"] == "default-token"

    def test_subsystem_api_key_env_override(self, monkeypatch):
        from saga.config import resolve_llm_config
        import saga.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_config", {
            "llm": {"api_key_env": "WRONG_KEY"},
            "annotation": {"api_key_env": "RIGHT_KEY"},
        })
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        monkeypatch.setenv("WRONG_KEY", "")
        monkeypatch.setenv("RIGHT_KEY", "annotation-token")
        out = resolve_llm_config("annotation")
        assert out["api_key"] == "annotation-token"

    def test_falls_back_to_common_envs_if_no_api_key_env(self, monkeypatch):
        from saga.config import resolve_llm_config
        import saga.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_config", {"llm": {}, "consolidation": {}})
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        # Clear all
        for k in ("OPENAI_API_KEY", "NVIDIA_NIM_API_KEY", "NVIDIA_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "openai-fallback-token")
        out = resolve_llm_config("consolidation")
        assert out["api_key"] == "openai-fallback-token"

    def test_returns_empty_api_key_when_none_set(self, monkeypatch):
        from saga.config import resolve_llm_config
        import saga.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_config", {"llm": {}, "triples": {}})
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        for k in ("OPENAI_API_KEY", "NVIDIA_NIM_API_KEY", "NVIDIA_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        out = resolve_llm_config("triples")
        assert out["api_key"] == ""

    def test_provider_default_is_openai_compat(self, monkeypatch):
        from saga.config import resolve_llm_config
        import saga.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_config", {"llm": {}, "consolidation": {}})
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        out = resolve_llm_config("consolidation")
        assert out["provider"] == "openai_compat"

    def test_subsystem_provider_overrides_top_level(self, monkeypatch):
        from saga.config import resolve_llm_config
        import saga.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_config", {
            "llm": {"provider": "openai_compat"},
            "triples": {"provider": "anthropic"},
        })
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        out = resolve_llm_config("triples")
        assert out["provider"] == "anthropic"

    def test_anthropic_provider_picks_up_anthropic_api_key_env(self, monkeypatch):
        """When provider is anthropic and no other key resolves, fall through
        to ANTHROPIC_API_KEY — saga's bench harness usually has only
        OPENAI/NVIDIA keys, so production setups need this fallback."""
        from saga.config import resolve_llm_config
        import saga.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_config", {
            "llm": {"provider": "anthropic"},
            "consolidation": {},
        })
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        for k in ("OPENAI_API_KEY", "NVIDIA_NIM_API_KEY", "NVIDIA_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-token")
        out = resolve_llm_config("consolidation")
        assert out["provider"] == "anthropic"
        assert out["api_key"] == "ant-token"

    def test_alternate_subsystem_model_keys(self, monkeypatch):
        """retrieval_v2 uses `rerank_model`, compression uses
        `synthesis_model` historically — those should still resolve."""
        from saga.config import resolve_llm_config
        import saga.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_config", {
            "llm": {"model": "default"},
            "retrieval_v2": {"rerank_model": "rerank-special"},
            "compression": {"synthesis_model": "synth-special"},
        })
        monkeypatch.setattr(cfg_mod, "_config_loaded", True)
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        assert resolve_llm_config("retrieval_v2")["model"] == "rerank-special"
        assert resolve_llm_config("compression")["model"] == "synth-special"


class TestLevenshtein:
    def test_identical(self):
        from saga.config import _levenshtein
        assert _levenshtein("abc", "abc") == 0

    def test_simple_substitution(self):
        from saga.config import _levenshtein
        assert _levenshtein("abc", "abd") == 1

    def test_insertion(self):
        from saga.config import _levenshtein
        assert _levenshtein("abc", "abcd") == 1

    def test_real_typo(self):
        from saga.config import _levenshtein
        # The Mimir typo: missing "_factor" suffix
        assert _levenshtein(
            "stability_reduction",
            "stability_reduction_factor",
        ) == 7
