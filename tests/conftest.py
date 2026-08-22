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
from types import SimpleNamespace

import pytest


SYNTHETIC_MIMIR_UID = 42001
SYNTHETIC_WORKLINK_UID = 42002
SYNTHETIC_WORKLINK_GID = 42003


@pytest.fixture(autouse=True, scope="session")
def _disable_git_auto_maintenance():
    """Keep every test-created repository free of background maintenance."""
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    names = (
        "GIT_CONFIG_COUNT",
        f"GIT_CONFIG_KEY_{count}",
        f"GIT_CONFIG_VALUE_{count}",
    )
    saved = {name: os.environ[name] for name in names if name in os.environ}
    os.environ["GIT_CONFIG_COUNT"] = str(count + 1)
    os.environ[f"GIT_CONFIG_KEY_{count}"] = "maintenance.auto"
    os.environ[f"GIT_CONFIG_VALUE_{count}"] = "false"
    try:
        yield
    finally:
        for name in names:
            os.environ.pop(name, None)
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def synthetic_worklink_identities(monkeypatch):
    """Keep containment tests independent of deployment-local accounts.

    Dedicated identity-resolution tests invoke the real accessor in child
    processes with their own injected pwd/grp implementations. All other tests
    receive non-production synthetic values, so they still catch regressions to
    the former 1001/1002 literals without requiring host account provisioning.
    """
    identities = SimpleNamespace(
        mimir_uid=SYNTHETIC_MIMIR_UID,
        worklink_uid=SYNTHETIC_WORKLINK_UID,
        worklink_gid=SYNTHETIC_WORKLINK_GID,
    )
    for module_name in (
        "mimir.contained_checkout",
        "mimir.project_tests",
        "mimir.worklink.checkout",
        "mimir.worklink.worker_exec",
    ):
        module = __import__(module_name, fromlist=["get_identities"])
        monkeypatch.setattr(module, "get_identities", lambda: identities)
    return identities


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


@pytest.fixture
def repo_review_git_root(tmp_path):
    """Bind repo-review authorization tests to a Git root they own."""
    root = tmp_path / "repo-review"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root.resolve()


# Host-only Mimir settings: set by an operator or a deployment, never by a test.
_HOST_ONLY_ENV = frozenset(
    {
        "MIMIR_API_KEY",
        "MIMIR_HOME",
        "MIMIR_FACTORY_PUBLISHING_IDENTITY",
    }
)

# The poller framework injects these into a poller's child process
# (``mimir/pollers.py`` sets them; ``_POLLER_INJECTED_ENV_KEYS`` is the source of
# truth, and ``test_env_isolation`` asserts this set stays in step with it).
# Inheriting them is not merely noisy -- ``STATE_DIR`` is where
# ``_record_run_failure`` writes dispatch failures, so a test running under an
# inherited value writes into the live poller store.
_POLLER_INJECTED_ENV = frozenset({"STATE_DIR", "POLLER_NAME", "MIMIR_HOME"})


@pytest.fixture(autouse=True, scope="session")
def _clear_host_mimir_environment():
    """Pop host-only Mimir settings from os.environ for the test session.

    ``mimir.server._make_auth_middleware`` reads this env var at
    ``build_app`` time. When non-empty it gates every non-exempt route
    on a matching ``X-API-Key`` header — which the test clients don't
    set, so they hit 401 and fail on ``assert resp.status == 200``.
    Tests that want to exercise the auth-on path should monkeypatch
    the env var explicitly inside the test body.

    ``MIMIR_HOME`` is also host-specific. Leaving it set makes the live
    ``repositories.yaml`` override temporary ``GITHUB_REPOS`` and writable-root
    settings in authorization tests.

    ``MIMIR_FACTORY_PUBLISHING_IDENTITY`` is host-only for the same reason and
    was missed when #1624 introduced it. It overrides the ``publishing_identity``
    a checkout declares in ``.factory.json``, so a deployment that sets it makes
    every test asserting on the declared identity read the deployment's value
    instead. That is not hypothetical: it ran green in CI, where the variable is
    unset, and failed inside mimirbot, where ``compose.env`` sets it -- taking
    the worklink test gate down, and with it ``review_ready``, the commit step
    and publication for every build. Tests that exercise the override set it
    explicitly in the test body.

    Same shape as the SAGA_CONFIG cleanup proposed in chainlink #129's
    PR #75 precedent. Session-scoped so we don't churn os.environ on
    every test; autouse so individual test files don't have to opt in.
    """
    saved = {
        name: os.environ.pop(name)
        for name in _HOST_ONLY_ENV | _POLLER_INJECTED_ENV
        if name in os.environ
    }
    yield
    os.environ.update(saved)


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
