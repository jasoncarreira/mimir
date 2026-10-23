"""Autouse isolation fixtures shared by every first-party test tree.

``tests/conftest.py`` and ``plugins/cultivate/tests/conftest.py`` both import
these, so provider tests get exactly the same process-environment, Git,
identity and telemetry isolation as core tests. Define a new suite-wide
isolation fixture here, never in one tree's conftest, or the other tree runs
without it.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _isolate_installed_saga_config():
    """An entrypoint's explicit installation must not affect the next test."""
    from mimir.saga import _config_io

    generation = _config_io._install_generation
    try:
        yield
    finally:
        if _config_io._install_generation != generation:
            _config_io.install_saga_config(None)


@pytest.fixture(autouse=True)
def _isolate_memory_tool_client():
    """Constructing an Agent with a SagaStore must not affect the next test.

    ``Agent._try_inject_memory_client`` installs the store into the
    process-wide memory-tool slot, and only runtime teardown clears it, so
    a test that builds an Agent directly would leave its store for the next
    test on the same worker. Restore the slot to what this test started with.
    """
    from mimir.tools.memory import _MEMORY_STATE

    previous = _MEMORY_STATE.get("client")
    try:
        yield
    finally:
        _MEMORY_STATE["client"] = previous


@pytest.fixture(autouse=True)
def _event_log_read_barrier(monkeypatch, request):
    """Drain accepted sync telemetry before test-side reads of owned logs.

    A teardown-only flush is too late for assertions inside async tests. Keep
    production writers asynchronous; intercept reads instead, including the
    stat/exists check that often precedes a read. Track actual EventLogger
    paths, not a filename convention (several tests use custom names).

    The logger's own contract tests deliberately inspect an undrained queue
    and retain explicit off-loop barriers. No production reader gets this
    guarantee: see docs/event-log-consistency.md. Worker-thread IO must bypass
    this hook, or the dedicated writer would wait for itself.
    """
    import builtins
    import threading
    from mimir import event_logger

    if request.node.path.name == "test_event_logger.py":
        yield
        return

    paths: set[str] = set()
    owner_thread = threading.get_ident()
    original_init = event_logger.EventLogger.__init__
    original_open = Path.open
    original_stat = Path.stat
    original_builtin_open = builtins.open
    flushing = False

    def key(path):
        return os.path.abspath(os.fspath(path))

    def init(logger, path, *args, **kwargs):
        original_init(logger, path, *args, **kwargs)
        paths.add(key(path))

    def barrier(path):
        nonlocal flushing
        if threading.get_ident() != owner_thread or flushing:
            return
        try:
            owned = key(path) in paths
        except TypeError:  # open() also accepts integer file descriptors
            return
        if owned:
            flushing = True
            try:
                event_logger._SYNC_EXECUTOR.submit(lambda: None).result(timeout=10)
            finally:
                flushing = False

    def path_open(path, mode="r", *args, **kwargs):
        if "r" in mode or "+" in mode:
            barrier(path)
        return original_open(path, mode, *args, **kwargs)

    def path_stat(path, *args, **kwargs):
        barrier(path)
        return original_stat(path, *args, **kwargs)

    def builtin_open(path, mode="r", *args, **kwargs):
        if "r" in mode or "+" in mode:
            barrier(path)
        return original_builtin_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(event_logger.EventLogger, "__init__", init)
    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(Path, "stat", path_stat)
    monkeypatch.setattr(builtins, "open", builtin_open)
    try:
        yield
    finally:
        # Finish this test's writes before its patches/temporary paths expire.
        if paths:
            event_logger._SYNC_EXECUTOR.submit(lambda: None).result(timeout=10)


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


_CORE_IDENTITY_MODULES = (
    "mimir.project_tests",
    "mimir.worklink.checkout",
    "mimir.worklink.worker_exec",
)
# Provider modules that bind ``get_identities`` at import time. They are
# imported before patching so a later import cannot capture the real accessor.
_PROVIDER_IDENTITY_MODULES = (
    "mimir_cultivate.worklink.identities",
    "mimir_cultivate.contained_checkout",
    "mimir_cultivate.repository",
    "mimir_cultivate.worklink.checkout",
    "mimir_cultivate.worklink.orchestrator",
    "mimir_cultivate.worklink.worker_exec",
)


@pytest.fixture(autouse=True)
def synthetic_worklink_identities(monkeypatch, request):
    """Keep containment tests independent of deployment-local accounts.

    Dedicated identity-resolution tests invoke the real accessor in child
    processes with their own injected pwd/grp implementations. All other tests
    receive non-production synthetic values, so they still catch regressions to
    the former 1001/1002 literals without requiring host account provisioning.
    Privileged integration tests opt out because their subject is the real split
    between the provisioned ``mimir`` and ``worklink`` accounts.
    """
    if request.node.get_closest_marker("real_worklink_identities") is not None:
        return None
    from tests.plugin_selection import cultivate_selected

    selected = cultivate_selected()
    values = {
        "mimir_uid": SYNTHETIC_MIMIR_UID,
        "worklink_uid": SYNTHETIC_WORKLINK_UID,
        "worklink_gid": SYNTHETIC_WORKLINK_GID,
    }
    if selected and importlib.util.find_spec("mimir_cultivate") is not None:
        for module_name in _PROVIDER_IDENTITY_MODULES:
            importlib.import_module(module_name)
    # Unselected runs never import the provider, but a test module may have
    # imported it directly; its bindings are replaced all the same.
    identity_module = sys.modules.get("mimir_cultivate.worklink.identities")
    real_accessor = getattr(identity_module, "get_identities", None)
    if identity_module is not None:
        # The production type, so ``dataclasses.replace`` works on it.
        identities = identity_module.WorklinkIdentities(**values)
    else:
        identities = SimpleNamespace(**values)

    def synthetic():
        return identities

    for module_name in _CORE_IDENTITY_MODULES:
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "get_identities"):
            monkeypatch.setattr(module, "get_identities", synthetic)
    # Every loaded provider binding of the real accessor, including the
    # accessor's own module so call-time ``from .identities import`` sees it.
    if real_accessor is not None:
        for module_name, module in sys.modules.copy().items():
            if (
                module_name.startswith("mimir_cultivate.")
                and getattr(module, "get_identities", None) is real_accessor
            ):
                monkeypatch.setattr(module, "get_identities", synthetic)
    return identities



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



# Host-only Mimir settings: set by an operator or a deployment, never by a test.
_HOST_ONLY_ENV = frozenset(
    {
        "MIMIR_API_KEY",
        "MIMIR_HTTP_SHUTDOWN_TIMEOUT_SECONDS",
        "MIMIR_CHANNEL_SCOPE_FILE",
        "MIMIR_DISCORD_ALLOWED_CHANNELS",
        "MIMIR_DISCORD_IGNORED_CHANNELS",
        "MIMIR_DISCORD_REQUIRE_MENTION",
        "MIMIR_DISCORD_FREE_RESPONSE_CHANNELS",
        "MIMIR_DISCORD_ALLOW_BOTS",
        "MIMIR_DISCORD_ALLOWED_BOT_IDS",
        "MIMIR_SLACK_ALLOWED_CHANNELS",
        "MIMIR_SLACK_IGNORED_CHANNELS",
        "MIMIR_SLACK_REQUIRE_MENTION",
        "MIMIR_SLACK_FREE_RESPONSE_CHANNELS",
        "MIMIR_SLACK_ALLOW_BOTS",
        "MIMIR_SLACK_ALLOWED_BOT_IDS",
        "MIMIR_CLAUDE_OAUTH_CREDENTIALS",
        "MIMIR_HOME",
        "MIMIR_MODEL_REASONING_EFFORT",
        "MIMIR_MALLOC_TRIM_CRON",
        "MIMIR_OPEN_BRIDGE",
        "MIMIR_SHELL_PASS_ENV",
        "MIMIR_WORKLINK_REASONING_EFFORT",
        "MIMIR_OUTBOUND_PII_JEV",
        "MIMIR_OUTBOUND_PRIVACY_ENFORCE",
        "JEV_KEY",
        "MIMIR_CODEX_PLUS_BUFFER_NONINTERACTIVE",
        "MIMIR_FACTORY_PUBLISHING_IDENTITY",
        "MIMIR_FACTORY_STARTUP_STATUS_TIMEOUT_S",
        "MIMIR_GITHUB_TRUSTED_BOT_LOGINS",
        "MIMIR_FILE_TOOL_ROOTS",
        "OPENCODE_CONFIG",
        "PYTEST_ADDOPTS",
    }
)

# The poller framework injects these into a poller's child process
# (``mimir/pollers.py`` sets them; ``_POLLER_INJECTED_ENV_KEYS`` is the source of
# truth, and ``test_env_isolation`` asserts this set stays in step with it).
# Inheriting them is not merely noisy -- ``STATE_DIR`` is where
# ``_record_run_failure`` writes dispatch failures, so a test running under an
# inherited value writes into the live poller store.
# Git reads these before any repository config. A test that creates commits must
# own its author, committer and repository, so none of them may be inherited.
_INHERITED_GIT_ENV = frozenset({
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
})


@pytest.fixture(autouse=True, scope="session")
def _clear_inherited_git_environment():
    """Pop inherited Git identity and repository redirection for the session.

    A feature-factory run exports ``GIT_AUTHOR_*`` and ``GIT_COMMITTER_*`` (the
    dispatch identity) to everything it launches, including repository
    verification. Fixture commits then carry that identity instead of the
    repository's configured ``user.name``, and lease attestation correctly
    refuses them: six ``tests/test_lease_source_labels.py`` cases failed only
    inside factory run ``chainlink-1947``, while passing in CI and locally.
    Inherited ``GIT_DIR``/``GIT_WORK_TREE`` can likewise redirect a fixture's Git
    commands away from the repository it owns (chainlink #1945). Tests that
    exercise these variables set them explicitly with ``monkeypatch``.
    """
    saved = {name: os.environ.pop(name) for name in _INHERITED_GIT_ENV if name in os.environ}
    yield
    os.environ.update(saved)


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

    ``MIMIR_HOME`` and ``MIMIR_FILE_TOOL_ROOTS`` are also host-specific. Leaving
    them set makes the live ``repositories.yaml`` override temporary
    ``GITHUB_REPOS`` and writable-root settings in authorization tests.

    ``MIMIR_CLAUDE_OAUTH_CREDENTIALS`` explicitly overrides the credentials path
    derived from a test's temporary ``MIMIR_HOME``. Inheriting it makes Config
    inspect the controller's real credentials, which correctly fails when the
    suite runs across the Worklink worker uid boundary.

    ``OPENCODE_CONFIG`` must not pin tests that replace ``HOME`` to the live
    agent's model configuration. ``PYTEST_ADDOPTS`` has already been consumed by
    the parent pytest process and must not leak its report paths into child pytest
    runs. ``MIMIR_FACTORY_PUBLISHING_IDENTITY`` is host-only for the same reason and
    was missed when #1624 introduced it. It overrides the ``publishing_identity``
    a checkout declares in ``.factory.json``, so a deployment that sets it makes
    every test asserting on the declared identity read the deployment's value
    instead. That is not hypothetical: it ran green in CI, where the variable is
    unset, and failed inside mimirbot, where ``compose.env`` sets it -- taking
    the worklink test gate down, and with it ``review_ready``, the commit step
    and publication for every build. Tests that exercise the override set it
    explicitly in the test body.

    ``MIMIR_PLUGINS`` is the process-owned plugin selection and must survive this
    fixture. Tests requiring core-only behavior select it explicitly; clearing
    selection here would silently turn off the selected-provider test matrix.

    ``MIMIR_MALLOC_TRIM_CRON`` must not let a deployment override change the
    suite's default-registration assertions.

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
