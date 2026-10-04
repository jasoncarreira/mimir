"""Explicit SAGA configuration installation for single-home entrypoints."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mimir.runtime import resolve_saga_config, resolve_saga_db_path
from mimir.saga import _config_io, embeddings


def _home(tmp_path: Path, name: str, provider: str) -> Path:
    home = tmp_path / name
    home.mkdir()
    (home / "saga.toml").write_text(
        f'[llm]\nprovider = "{provider}"\nmodel = "{name}"\n'
        '[embedding]\nprovider = "voyage"\n'
        '[retrieval]\nenable_contextual_rewrite = true\n'
        f'[storage]\ndb_path = "{name}.db"\n',
        encoding="utf-8",
    )
    return home


@pytest.mark.parametrize("reverse", [False, True])
def test_installs_replace_every_field_even_when_accessor_preexists(tmp_path, monkeypatch, reverse):
    monkeypatch.delenv("SAGA_CONFIG", raising=False)
    homes = [_home(tmp_path, "alpha", "codex_plus"), _home(tmp_path, "beta", "minimax")]
    if reverse:
        homes.reverse()
    accessor = _config_io.get_config()
    for home in homes:
        path, source = resolve_saga_config(home)
        assert source == "home"
        assert _config_io.install_saga_config(path) == home / "saga.toml"
        assert accessor("llm", "model") == home.name
        assert accessor("retrieval", "enable_contextual_rewrite") is True
        assert _config_io.was_set_in_toml("embedding", "provider")
        assert resolve_saga_db_path(home) == home / ".mimir" / f"{home.name}.db"


def test_provider_built_before_install_is_replaced(tmp_path, monkeypatch):
    monkeypatch.delenv("SAGA_CONFIG", raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    _config_io.install_saga_config(None)

    class DefaultProvider:
        pass

    class HomeProvider:
        pass

    monkeypatch.setitem(embeddings._PROVIDERS, "voyage", DefaultProvider)
    monkeypatch.setitem(embeddings._PROVIDERS, "openai", HomeProvider)
    first = embeddings.get_provider()
    assert isinstance(first, DefaultProvider)
    home = tmp_path / "saga.toml"
    home.write_text('[embedding]\nprovider = "openai"\n', encoding="utf-8")
    _config_io.install_saga_config(home)
    assert isinstance(embeddings.get_provider(), HomeProvider)
    assert embeddings.get_provider() is not first


def test_exported_config_wins_for_install_and_db(tmp_path, monkeypatch):
    home = _home(tmp_path, "home", "codex_plus")
    operator = _home(tmp_path, "operator", "minimax") / "saga.toml"
    monkeypatch.setenv("SAGA_CONFIG", str(operator))
    path, source = resolve_saga_config(home)
    assert (path, source) == (operator, "env")
    _config_io.install_saga_config(path)
    assert _config_io.resolve_llm_config("consolidation")["provider"] == "minimax"
    assert resolve_saga_db_path(home) == home / ".mimir/operator.db"
    assert resolve_saga_config(home)[0] == operator


def test_parse_error_keeps_previous_install_and_names_file(tmp_path):
    first = _home(tmp_path, "valid", "codex_plus") / "saga.toml"
    _config_io.install_saga_config(first)
    bad = tmp_path / "invalid.toml"
    bad.write_text("[llm\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid.toml"):
        _config_io.install_saga_config(bad)
    assert _config_io.resolve_llm_config("reflection")["provider"] == "codex_plus"


def test_selected_missing_export_fails_instead_of_using_home(tmp_path, monkeypatch):
    home = _home(tmp_path, "home", "codex_plus")
    missing = tmp_path / "missing.toml"
    monkeypatch.setenv("SAGA_CONFIG", str(missing))
    path, source = resolve_saga_config(home)
    assert (path, source) == (missing, "env")
    with pytest.raises(OSError, match="missing.toml"):
        _config_io.install_saga_config(path)


@pytest.mark.parametrize("name", ["one", "two"])
def test_separate_tests_start_on_defaults_and_install_own_home(tmp_path, monkeypatch, name):
    monkeypatch.delenv("SAGA_CONFIG", raising=False)
    assert _config_io.resolve_llm_config("reflection")["provider"] == "openai_compat"
    home = _home(tmp_path, name, "codex_plus")
    _config_io.install_saga_config(resolve_saga_config(home)[0])
    assert _config_io.get_config()("llm", "model") == name


def test_installed_llm_keys_resolve_without_unknown_key_warning(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.delenv("SAGA_QUIET_CONFIG", raising=False)
    monkeypatch.setenv("SAGA_INSTALL_TEST_KEY", "test-only")
    caplog.set_level(logging.WARNING, logger="saga.config")
    path = tmp_path / "saga.toml"
    path.write_text(
        '[llm]\nprovider = "codex_plus"\n'
        'url = "https://example.invalid/v1/chat/completions"\n'
        'model = "home-model"\napi_key_env = "SAGA_INSTALL_TEST_KEY"\n'
        'timeout_seconds = 47\nreasoning_effort = "low"\n',
        encoding="utf-8",
    )
    _config_io.install_saga_config(path)
    assert _config_io.resolve_llm_config("reflection") == {
        "provider": "codex_plus", "model": "home-model",
        "url": "https://example.invalid/v1/chat/completions",
        "api_key": "test-only", "timeout": 47, "reasoning_effort": "low",
    }
    assert "Unknown config key [llm]" not in caplog.text


def test_no_production_assignment_to_saga_config():
    root = Path(__file__).parents[1] / "mimir"
    for file in root.rglob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else (
                [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
            )
            for target in targets:
                assert not (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and isinstance(target.value.value, ast.Name)
                    and target.value.value.id == "os"
                    and target.value.attr == "environ"
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "SAGA_CONFIG"
                ), file
