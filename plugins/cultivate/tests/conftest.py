"""Cultivate provider tests run under the core suite's isolation fixtures.

``testpaths`` names only ``tests``, so ``tests/conftest.py`` never applies here.
Import the shared autouse fixtures instead of redefining them: the provider
tests must get the same environment, Git, identity and telemetry isolation, and
must not drift from it.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.isolation_fixtures import (  # noqa: F401  (autouse fixtures)
    _clear_host_mimir_environment,
    _clear_inherited_git_environment,
    _disable_git_auto_maintenance,
    _event_log_read_barrier,
    _isolate_installed_saga_config,
    _isolate_memory_tool_client,
    _isolate_process_env,
    maintenance_pinned_executables,
    synthetic_worklink_identities,
)


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """A private directory short enough for AF_UNIX socket paths.

    ``sun_path`` holds 108 bytes on Linux and 104 on macOS, and pytest's
    ``tmp_path`` under a CI ``--basetemp`` exceeds that, so ``bind`` fails with
    "AF_UNIX path too long". The core executor tests use a ``/tmp`` directory for
    the same reason.
    """
    with tempfile.TemporaryDirectory(prefix="mimir-sock-", dir="/tmp") as directory:
        yield Path(directory)
