"""Saga test isolation — unset production saga env vars per test.

Saga's config loader (``saga.config._load_config``) walks an env-var
search path (``$SAGA_CONFIG`` → ``$SAGA_DATA_DIR/saga.toml`` →
``~/.saga/saga.toml`` → packaged default). When a test runs in an
environment that has ``SAGA_CONFIG`` pointing at a real production
saga.toml (e.g., the mimir agent container has
``SAGA_CONFIG=/mimir-home/saga.toml``), tests that assume "default
config" silently load production overrides — embedding dimensions
1024→1536, model swap, populated api keys, the works.

This autouse fixture neutralizes the env-var search path before each
test so tests see the packaged defaults, regardless of where the suite
is run. The config singleton is also reset so a config loaded by an
earlier test (e.g. via explicit env override inside the test) doesn't
leak across tests.

Filed under chainlink #49 — the surfacing was 15 baseline failures on
main that all shared this root cause."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_saga_config_env(monkeypatch):
    """Clear ``SAGA_CONFIG`` and ``SAGA_DATA_DIR`` so tests see
    packaged defaults. Restored automatically by monkeypatch teardown."""
    monkeypatch.delenv("SAGA_CONFIG", raising=False)
    monkeypatch.delenv("SAGA_DATA_DIR", raising=False)
    # Reset the saga config singleton so the next ``get_config()`` call
    # re-walks the (now empty) search path and lands on the packaged
    # defaults.
    import saga.config as cfg_mod
    cfg_mod._config = None
    cfg_mod._config_loaded = False
    yield
    # No explicit teardown needed — monkeypatch restores env vars; the
    # singleton stays reset (next test will see it as None until another
    # autouse fixture or the test itself rebuilds it, which is the same
    # invariant we want).
