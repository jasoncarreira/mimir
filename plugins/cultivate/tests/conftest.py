"""Cultivate provider tests run under the core suite's isolation fixtures.

``testpaths`` names only ``tests``, so ``tests/conftest.py`` never applies here.
Import the shared autouse fixtures instead of redefining them: the provider
tests must get the same environment, Git, identity and telemetry isolation, and
must not drift from it.
"""

from __future__ import annotations

from tests.isolation_fixtures import (  # noqa: F401  (autouse fixtures)
    _clear_host_mimir_environment,
    _clear_inherited_git_environment,
    _disable_git_auto_maintenance,
    _event_log_read_barrier,
    _isolate_installed_saga_config,
    _isolate_process_env,
    maintenance_pinned_executables,
    synthetic_worklink_identities,
)
