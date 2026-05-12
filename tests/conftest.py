"""Shared pytest fixtures for mimir tests.

Currently just env-cleanup: tests that build mimir's aiohttp server via
``mimir.server.build_app(cfg)`` inherit ``MIMIR_API_KEY`` from the
operator's live environment, which installs the auth middleware and 401s
the test's own un-keyed HTTP requests. The autouse session fixture
below pops ``MIMIR_API_KEY`` before any test runs and restores it after,
so the same suite passes whether the env var is set or not.

Spec: chainlink #129. Fix landed alongside chainlink #131 (PR #156)
since the smuggle-detection PR's full-suite run was the load-bearing
case for "all tests pass on every PR" (memory/core/40-learned-behaviors
2026-05-11 entry).
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _clear_mimir_api_key():
    """Pop ``MIMIR_API_KEY`` from os.environ for the whole test session.

    ``mimir.server._make_auth_middleware`` reads this env var at
    ``build_app`` time. When non-empty it gates every non-exempt route
    on a matching ``X-API-Key`` header — which the test clients don't
    set, so they hit 401 and fail on ``assert resp.status == 200``.
    Tests that want to exercise the auth-on path should monkeypatch
    the env var explicitly inside the test body.

    Same shape as the SAGA_CONFIG cleanup proposed in chainlink #129's
    PR #75 precedent. Session-scoped so we don't churn os.environ on
    every test; autouse so individual test files don't have to opt in.
    """
    saved = os.environ.pop("MIMIR_API_KEY", None)
    yield
    if saved is not None:
        os.environ["MIMIR_API_KEY"] = saved
