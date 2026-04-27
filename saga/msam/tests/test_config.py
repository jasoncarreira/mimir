"""MSAM Config Tests -- configuration loader and accessor."""

import pytest


@pytest.fixture(autouse=True)
def reset_config():
    """Reset config singleton before each test."""
    import msam.config as cfg_mod
    cfg_mod._config = None
    cfg_mod._config_loaded = False
    yield
    cfg_mod._config = None
    cfg_mod._config_loaded = False


class TestGetConfig:
    def test_returns_callable(self):
        from msam.config import get_config
        cfg = get_config()
        assert callable(cfg)

    def test_returns_defaults(self):
        from msam.config import get_config
        cfg = get_config()
        assert cfg('embedding', 'dimensions') == 1024
        assert cfg('storage', 'token_budget_ceiling') == 40000
        assert cfg('decay', 'active_to_fading_threshold') == 0.3

    def test_raises_on_missing(self):
        from msam.config import get_config
        cfg = get_config()
        with pytest.raises(KeyError):
            cfg('nonexistent_section', 'nonexistent_key')

    def test_default_fallback(self):
        from msam.config import get_config
        cfg = get_config()
        result = cfg('nonexistent', 'key', 'my_default')
        assert result == 'my_default'

    def test_nested_section(self):
        from msam.config import get_config
        cfg = get_config()
        aliases = cfg('entity_resolution', 'aliases', {})
        assert isinstance(aliases, dict)


class TestDeepMerge:
    def test_nested_override(self):
        from msam.config import _deep_merge
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        override = {"a": {"y": 99, "z": 100}}
        result = _deep_merge(base, override)
        assert result["a"]["x"] == 1
        assert result["a"]["y"] == 99
        assert result["a"]["z"] == 100
        assert result["b"] == 3

    def test_non_dict_override(self):
        from msam.config import _deep_merge
        base = {"a": {"x": 1}}
        override = {"a": "replaced"}
        result = _deep_merge(base, override)
        assert result["a"] == "replaced"


class TestReloadConfig:
    def test_resets_singleton(self):
        from msam.config import get_config, reload_config
        cfg1 = get_config()
        cfg2 = reload_config()
        # Both should work, reload returns a fresh accessor
        assert callable(cfg1)
        assert callable(cfg2)
        assert cfg2('embedding', 'dimensions') == 1024


class TestGetDataDir:
    def test_returns_path(self, tmp_path, monkeypatch):
        from msam.config import get_data_dir
        monkeypatch.setenv("MSAM_DATA_DIR", str(tmp_path / "test_data"))
        data_dir = get_data_dir()
        assert data_dir.exists()
        assert "test_data" in str(data_dir)

    def test_default_home(self, monkeypatch):
        from msam.config import get_data_dir
        monkeypatch.delenv("MSAM_DATA_DIR", raising=False)
        data_dir = get_data_dir()
        assert ".msam" in str(data_dir)


class TestGetRawConfig:
    def test_returns_dict(self):
        from msam.config import get_raw_config
        raw = get_raw_config()
        assert isinstance(raw, dict)
        assert "embedding" in raw
        assert "retrieval" in raw


class TestNewConfigSections:
    """Verify the newly-added config sections are accessible."""

    def test_compression_section(self):
        from msam.config import get_config
        cfg = get_config()
        assert cfg('compression', 'enable_subatom') is True
        assert cfg('compression', 'subatom_token_budget') == 120
        assert cfg('compression', 'dedup_similarity_threshold') == 0.85

    def test_comparison_section(self):
        from msam.config import get_config
        cfg = get_config()
        assert cfg('comparison', 'startup_files') == []
        assert cfg('comparison', 'query_files') == []

    def test_triples_section(self):
        from msam.config import get_config
        cfg = get_config()
        assert "api.nvidia.com" in cfg('triples', 'llm_url')
        assert "mistral" in cfg('triples', 'llm_model')

    def test_retrieval_confidence_keys(self):
        from msam.config import get_config
        cfg = get_config()
        assert isinstance(cfg('retrieval', 'confidence_sim_high'), float)
        assert 0.0 < cfg('retrieval', 'confidence_sim_high') <= 1.0
        assert isinstance(cfg('retrieval', 'temporal_recency_hours'), (int, float))
        assert cfg('retrieval', 'temporal_recency_hours') > 0

    def test_embedding_api_key(self):
        from msam.config import get_config
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
        from msam.config import _warn_unknown_keys
        caplog.set_level(logging.WARNING, logger="msam.config")
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
        from msam.config import _warn_unknown_keys
        caplog.set_level(logging.WARNING, logger="msam.config")
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
        from msam.config import _warn_unknown_keys
        caplog.set_level(logging.WARNING, logger="msam.config")
        _warn_unknown_keys({
            "experimental_feature": {"some_key": 42},
        })
        # Unknown sections aren't validated -- no warning.
        assert not caplog.records

    def test_known_extra_keys_dont_warn(self, caplog):
        import logging
        from msam.config import _warn_unknown_keys
        caplog.set_level(logging.WARNING, logger="msam.config")
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
        from msam.config import _warn_unknown_keys
        caplog.set_level(logging.WARNING, logger="msam.config")
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
        from msam.config import _warn_unknown_keys
        monkeypatch.setenv("MSAM_QUIET_CONFIG", "1")
        caplog.set_level(logging.WARNING, logger="msam.config")
        _warn_unknown_keys({
            "consolidation": {"cluster_similarity_threshold": 0.75},
        })
        assert not caplog.records


class TestLevenshtein:
    def test_identical(self):
        from msam.config import _levenshtein
        assert _levenshtein("abc", "abc") == 0

    def test_simple_substitution(self):
        from msam.config import _levenshtein
        assert _levenshtein("abc", "abd") == 1

    def test_insertion(self):
        from msam.config import _levenshtein
        assert _levenshtein("abc", "abcd") == 1

    def test_real_typo(self):
        from msam.config import _levenshtein
        # The Mimir typo: missing "_factor" suffix
        assert _levenshtein(
            "stability_reduction",
            "stability_reduction_factor",
        ) == 7
