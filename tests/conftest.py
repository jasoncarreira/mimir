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
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def middleware_event_logger(tmp_path):
    """Initialize logging for middleware fire-and-forget event tasks."""
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    init_logger(tmp_path / "middleware-events.jsonl", session_id="middleware-test")
    yield
    _reset_logger_for_tests()


@pytest.fixture(autouse=True, scope="session")
def maintenance_pinned_executables():
    """Isolate maintenance authorization tests from host executable layout."""
    from mimir import access_control

    # Why not ``tmp_path_factory``: pytest's tmp base lives under ``/tmp``,
    # which is now a service-writable root, and #991 requires every pinned
    # executable to resolve OUTSIDE every writable root — so a pin planted
    # there fails closed.
    #
    # Why ``.resolve()`` on the base: ``_maintenance_resolved_pin`` rejects a
    # pin whose ``resolve(strict=True)`` differs from its spelling. On macOS
    # ``/var`` is a symlink to ``private/var``, so an unresolved ``/var/tmp``
    # base makes every pin fail its own non-symlink identity check — green on
    # Linux CI, 72 failures locally. Canonicalize before planting.
    pin_base = Path("/var/tmp").resolve()
    with tempfile.TemporaryDirectory(
        prefix="mimir-maintenance-executables-", dir=pin_base,
    ) as executable_dir_text:
        executable_dir = Path(executable_dir_text).resolve()
        real_git = Path(shutil.which("git") or "git").resolve(strict=True)
        replacements: dict[str, Path] = {}
        for command in access_control._MAINTENANCE_PINNED_EXECUTABLES:
            executable = executable_dir / command.rsplit("/", 1)[-1]
            if command == "git":
                executable.write_text(
                    f"#!/bin/sh\nexec {shlex.quote(str(real_git))} \"$@\"\n",
                    encoding="utf-8",
                )
            else:
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            replacements[command] = executable

        original = access_control._MAINTENANCE_PINNED_EXECUTABLES.copy()
        access_control._MAINTENANCE_PINNED_EXECUTABLES.update(replacements)
        yield replacements
        access_control._MAINTENANCE_PINNED_EXECUTABLES.clear()
        access_control._MAINTENANCE_PINNED_EXECUTABLES.update(original)


@pytest.fixture
def maintenance_git_home(tmp_path, monkeypatch):
    """Give maintenance-shell authorization an explicit real Git root."""
    home = tmp_path / "maintenance-home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)
    return home


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


@pytest.fixture(autouse=True)
def _isolate_process_env():
    """Snapshot and restore ``os.environ`` around every test.

    ``mimir.config._load_home_dotenv`` calls ``load_dotenv(..., override=False)``,
    which MUTATES the process environment. That is correct at runtime — it is how
    ``<home>/.env`` becomes defaults while operator exports still win — but it
    leaks in tests: ``monkeypatch`` can only restore variables it set itself, so
    keys loaded from a temp-home ``.env`` outlive the test that wrote them.

    The leak is not theoretical. Before #1209, a test wrote
    ``MIMIR_MODEL_SPEC=claude-code:*`` into a temp home and loaded it; under
    ``MIMIR_ACCESS_CONTROL_ENFORCED=1`` every later ``Config.from_env()`` then hit
    the provider gate, producing ~186 cascading failures from one root cause.
    Enforcement amplifies the leak, so this fixture is a prerequisite for running
    the suite enforced in CI.
    """
    saved = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
