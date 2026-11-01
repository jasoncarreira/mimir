"""Shared pytest fixtures for mimir tests.

The suite-wide autouse isolation fixtures live in ``tests/isolation_fixtures.py``
so the Cultivate provider tests share them; this module re-exports them.

Origin: tests that build mimir's aiohttp server via
``mimir.server.build_app(cfg)`` inherit ``MIMIR_API_KEY`` from the
operator's live environment, which installs the auth middleware and 401s
the test's own un-keyed HTTP requests. The autouse session fixture
``_clear_host_mimir_environment`` pops ``MIMIR_API_KEY`` before any test runs and restores it after,
so the same suite passes whether the env var is set or not.

Spec: chainlink #129. Fix landed alongside chainlink #131 (PR #156)
since the smuggle-detection PR's full-suite run was the load-bearing
case for "all tests pass on every PR" (memory/core/40-learned-behaviors
2026-05-11 entry).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.isolation_fixtures import (  # noqa: F401  (autouse fixtures)
    _isolate_github_identity_latch,
    _isolate_installed_saga_config,
    _isolate_memory_tool_client,
    _event_log_read_barrier,
    SYNTHETIC_MIMIR_UID,
    SYNTHETIC_WORKLINK_UID,
    SYNTHETIC_WORKLINK_GID,
    _disable_git_auto_maintenance,
    synthetic_worklink_identities,
    maintenance_pinned_executables,
    _HOST_ONLY_ENV,
    _INHERITED_GIT_ENV,
    _clear_inherited_git_environment,
    _POLLER_INJECTED_ENV,
    _clear_host_mimir_environment,
    _isolate_process_env,
    _hide_host_coding_clis,
    real_coding_cli,
    stub_opencode,
)


@pytest.fixture
def middleware_event_logger(tmp_path):
    """Initialize logging for middleware fire-and-forget event tasks."""
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    init_logger(tmp_path / "middleware-events.jsonl", session_id="middleware-test")
    yield
    _reset_logger_for_tests()


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


@pytest.fixture
def repo_review_state(repo_review_git_root):
    """A real inspection root with all former remediation grants enabled."""
    pytest.importorskip("mimir_cultivate", reason="PR review state belongs to Cultivate")
    from mimir.plugins.core import policy_scope
    from mimir.plugins.manager import load_plugins
    from mimir_cultivate.policy import RepoPRAction, RepoPRActionScope, RepoReviewState

    state = RepoReviewState(RepoPRActionScope(
        provenance="poller_payload", canonical_repo="o/r",
        canonical_root=str(repo_review_git_root),
        canonical_origin="https://github.com/o/r.git", principal="mimir-bot",
        event_type="pr_changes_requested_stale",
        allowed_operations=frozenset(action.value for action in RepoPRAction),
        pr_number=7, head_repo="o/r", head_remote="origin",
        destination_ref="refs/heads/worklink/7", observed_head_sha="a" * 40,
        base_ref="main", observed_base_sha="b" * 40,
        pull_request_author="mimir-bot",
    ))
    state.attach_checkout_lease(SimpleNamespace(
        path=repo_review_git_root, lease_root=repo_review_git_root.parent,
        scope_id=state.action_scope.scope_id, owner=state.action_scope.principal,
        is_active=True,
    ))
    with policy_scope(load_plugins(enable="cultivate").policy):
        yield state


@pytest.fixture
def authorized_fake_backend_gate(monkeypatch):
    """Exercise mock backends' gates against a real, FD-bound test checkout."""
    import mimir_cultivate.worklink.orchestrator as orchestrator
    from mimir_cultivate.worklink.checkout import _mint_checkout_authorization
    from mimir_cultivate.worklink.compute import (
        ComputeResult, LaunchHandle, LocalSubprocessComputeBackend,
    )
    from mimir_cultivate.worklink.evidence import _command_with_pytest_report

    observe = orchestrator.observe_evidence

    async def observed(**kwargs):
        if kwargs.get("backend") != "fake" or kwargs.get("test_command") is None:
            return await observe(**kwargs)
        checkout = kwargs["checkout"]
        fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        with _mint_checkout_authorization(
            checkout, kwargs["issue"], kwargs["attempt"], fd,
        ) as authorization:
            worker = LocalSubprocessComputeBackend.for_authorized_checkout(authorization)
            results = {}

            async def launch(spec, argv):
                authorization.verify(spec.local_checkout)
                command = argv[2]
                if command.startswith(("pytest ", "uv run pytest ")):
                    options = shlex.split(spec.env["PYTEST_ADDOPTS"])
                    report = Path([
                        option.partition("=")[2] for option in options
                        if option.startswith("--junitxml=")
                    ][-1]).parent
                    if not report.is_absolute():
                        report = checkout / report
                    command = _command_with_pytest_report(command, report)
                try:
                    result = await asyncio.to_thread(
                        kwargs["runner"], command, cwd=checkout, timeout=spec.timeout_s,
                    )
                    observed_result = ComputeResult(
                        result.returncode, result.stdout or "", result.stderr or "",
                    )
                except subprocess.TimeoutExpired:
                    observed_result = ComputeResult(124, "", "", timed_out=True)
                handle = LaunchHandle("local_subprocess", f"gate-{len(results) + 1}")
                results[handle.identifier] = observed_result
                return handle

            async def wait(handle, _timeout):
                return results[handle.identifier]

            async def cleanup(handle):
                results.pop(handle.identifier, None)

            worker._launch_contained = launch
            worker.wait = wait
            worker.cleanup = cleanup
            return await observe(**{**kwargs, "compute": worker})

    monkeypatch.setattr(orchestrator, "observe_evidence", observed)
