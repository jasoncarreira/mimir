"""Cultivate's repository operations must remain independent of core wrappers."""

from __future__ import annotations

import ast
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import fcntl
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from mimir_cultivate import repository
from mimir._context import reset_current_turn, set_current_turn
from mimir.models import RepoPRScopeRegistry
from mimir.tools.budget_gate import BudgetGateMiddleware
from tests.test_budget_gate_and_alias import _ifc_turn, _make_request, _untainted_ifc_auth
from tests.test_repo_tools import (
    _configure_worklink_test, _fixture_git_env, _repo_scope_and_state,
)


def test_repository_implementation_is_owned_by_provider() -> None:
    from mimir import pr_checkout_lease, project_tests, repo_tools, repository_config
    from mimir.tools import repo

    assert repository.RepoGitTools is not repo_tools.RepoGitTools
    assert repository.RepoProjectTests is not project_tests.RepoProjectTests
    assert repository.PRCheckoutLease is not pr_checkout_lease.PRCheckoutLease
    assert repository.RepositoryInventory is not repository_config.RepositoryInventory
    assert repository.repo_status is not repo.repo_status
    assert {tool.name for tool in repository.REPO_TOOLS} == {
        tool.name for tool in repo.REPO_TOOLS
    }

    tree = ast.parse(Path(repository.__file__).read_text(encoding="utf-8"))
    forbidden = {
        "mimir.repo_tools", "mimir.project_tests", "mimir.pr_checkout_lease",
        "mimir.repository_config", "mimir.repo_pr_parity", "mimir.tools.repo",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in forbidden:
            # Only the host's lock carrier crosses this boundary. No core
            # repository operation or lease implementation is imported.
            assert node.module == "mimir.pr_checkout_lease"
            assert {alias.name for alias in node.names} == {"_borrowed_pr_checkout_lock"}


def test_core_repository_compatibility_does_not_import_provider() -> None:
    root = Path(__file__).resolve().parents[3]
    for name in ("pr_checkout_lease", "repo_tools", "project_tests", "tools/repo"):
        tree = ast.parse((root / "mimir" / f"{name}.py").read_text(encoding="utf-8"))
        modules = [
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]
        modules.extend(
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert all(not (module or "").startswith("mimir_cultivate") for module in modules)


def test_repository_config_and_parity_are_provider_owned(tmp_path: Path) -> None:
    from mimir import repo_pr_parity

    assert repository.ParityProbe is not repo_pr_parity.ParityProbe
    assert repository.run_parity is not repo_pr_parity.run_parity
    assert repository.RepositoryInventory.load(tmp_path / "absent.yaml").declared is False
    report = repository.run_parity(repository.offline_canary_probes())
    assert report["cutover_blocked"] is False
    assert report["counts"]["operations_total"] > 0


def test_repository_git_path_validation_stays_closed() -> None:
    with pytest.raises(repository.GitRefusal, match="repository-relative"):
        repository._validated_paths(("../outside",), required=True)


def test_project_test_failure_summary_requires_source_inventory(tmp_path: Path) -> None:
    path = tmp_path / "test_sample.py"
    path.write_text("def test_known():\n    assert False\n", encoding="utf-8")
    inventory = repository.pytest_node_inventory(tmp_path)
    result = repository.pytest_failure_summary(
        b"= short test summary info =\nFAILED test_sample.py::test_known - failure\n"
        b"FAILED test_sample.py::test_forged - failure\n= 2 failed in 0.1s =\n",
        inventory,
    )
    assert result["failing"] == ["test_sample.py::test_known"]
    assert result["failing_dropped"] == 1


def test_provider_reacquires_and_inspects_an_exact_pr_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools.budget_gate import _hold_scoped_pr_dispatch

    _origin, _source, scope, state = _repo_scope_and_state(tmp_path)
    previous = state.checkout_lease
    assert previous is not None
    acquired = repository.recover_pr_checkout_lease(
        previous.path, scope, owner=scope.principal,
        lease_root=previous.lease_root, review_state=state,
        runner=lambda args: subprocess.run(args, capture_output=True, text=True, check=False),
    )
    assert type(acquired) is repository.PRCheckoutLease
    assert acquired.recovery_id != previous.recovery_id
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(acquired.lease_root))
    with pytest.raises(RuntimeError, match="no longer live"):
        with repository.hold_pr_checkout_lease(previous):
            pass
    with repository.hold_pr_checkout_lease(acquired):
        result = repository.RepoGitTools(state).execute(repository.GitStatus())
    assert result.ok is True
    assert result.code == "ok"
    call = SimpleNamespace(
        tool_name="repo_status", policy=None,
        authorization=SimpleNamespace(repo_pr_action_scope=scope),
        auth_context=SimpleNamespace(
            server_discovered_pr_states=None,
            repo_pr_scope_registry=SimpleNamespace(resolve=lambda *_: state),
        ),
        arguments={"repository": scope.canonical_repo, "pull_request": scope.pr_number},
    )
    with ExitStack() as stack:
        _hold_scoped_pr_dispatch(call, stack)
        assert repository.active_pr_checkout_lease_for_path(acquired.path) is not None
    object.__setattr__(acquired, "expires_at", datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(RuntimeError, match="no longer live"):
        repository.assert_live_pr_checkout_lease(acquired)


def test_provider_creates_and_cleans_up_checkout(tmp_path: Path) -> None:
    _origin, _source, scope, state = _repo_scope_and_state(tmp_path)
    fresh_root = tmp_path / "provider-leases"
    fresh_root.mkdir()
    def run(argv):
        return subprocess.run(
            argv, capture_output=True, text=True, check=False, env=_fixture_git_env(),
        )
    lease = repository.create_pr_checkout_lease(
        scope, owner=scope.principal, lease_root=fresh_root, runner=run,
    )
    assert isinstance(lease, repository.PRCheckoutLease)
    assert (lease.path / ".git/mimir-pr-checkout-lease.json").is_file()
    assert repository.RepoGitTools(state).validated_checkout_root() == state.checkout_lease.path
    assert repository.cleanup_pr_checkout_lease(lease, runner=run) is True
    assert not os.path.lexists(lease.path)


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("operation", ["repo_checkout", "repo_status"])
@pytest.mark.parametrize("expire", [False, True])
def test_provider_checkout_handoff_and_live_result_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    async_mode: bool, operation: str, expire: bool,
) -> None:
    from mimir.tools import budget_gate

    _origin, _source, scope, state = _repo_scope_and_state(tmp_path)
    root = state.checkout_lease.lease_root
    if operation == "repo_checkout":
        root = tmp_path / "provider-leases"
        root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(root))
    auth = replace(
        _untainted_ifc_auth(), repo_pr_scope_registry=RepoPRScopeRegistry((state,)),
        repo_review_state=state, repo_pr_action_scope=scope,
    )
    calls: list[str] = []
    completed = threading.Event()
    competitor_ready = threading.Event()
    release_competitor = threading.Event()
    rebound = []
    threads = []
    original_finish = budget_gate._finish_tool_result

    def run(argv):
        return subprocess.run(
            argv, capture_output=True, text=True, check=False, env=_fixture_git_env(),
        )

    def competitor(lease):
        assert lock_is_held()
        competitor_ready.set()
        assert release_competitor.wait(10)
        rebound.append(repository.recover_pr_checkout_lease(
            lease.path, scope, owner=scope.principal, lease_root=root, runner=run,
        ))
        completed.set()

    def lock_is_held():
        fd = os.open(root, os.O_RDONLY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def handler(req):
        if operation == "repo_checkout":
            lease, _ = repository.acquire_pr_checkout_lease(
                scope, owner=scope.principal, lease_root=root,
                review_state=state, runner=run,
            )
        else:
            lease = state.checkout_lease
            assert repository.RepoGitTools(state).execute(repository.GitStatus()).ok
        assert lock_is_held()
        if expire:
            object.__setattr__(lease, "expires_at", datetime.now(UTC) - timedelta(seconds=1))
        else:
            thread = threading.Thread(target=competitor, args=(lease,))
            threads.append(thread)
            thread.start()
            assert competitor_ready.wait(5)
        calls.append("handler")
        return ToolMessage(content='{"ok": true}', tool_call_id=req.tool_call["id"])

    def finish(call, capture, result):
        assert lock_is_held()
        assert not completed.is_set()
        calls.append("observed")
        return original_finish(call, capture, result)

    monkeypatch.setattr(budget_gate, "_finish_tool_result", finish)
    token = set_current_turn(_ifc_turn(auth))
    try:
        request = _make_request(
            operation, "provider", auth,
            {"repository": scope.canonical_repo, "pull_request": scope.pr_number},
        )
        if async_mode:
            async def async_handler(req):
                return handler(req)

            result = asyncio.run(BudgetGateMiddleware().awrap_tool_call(request, async_handler))
        else:
            result = BudgetGateMiddleware().wrap_tool_call(request, handler)
    finally:
        release_competitor.set()
        reset_current_turn(token)
    if expire:
        assert result.status == "error"
        assert "expired or changed" in str(result.content)
        assert calls == ["handler"]
    else:
        assert result.status != "error", result.content
        assert calls == ["handler", "observed"]
        assert completed.wait(10)
        assert rebound[0].recovery_id != state.checkout_lease.recovery_id
        threads[0].join(timeout=10)


@pytest.mark.asyncio
async def test_provider_project_tests_run_fixed_command_in_contained_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.contained_execution import CollectedExecutionResult

    _origin, _source, _scope, state = _repo_scope_and_state(tmp_path)
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    calls = []

    async def run(argv, capability, environment, projections, **kwargs):
        calls.append((argv, capability, environment, projections, kwargs))
        return CollectedExecutionResult(0, b"ok", b"", False, False, 0, 0)

    def checkout(source, **kwargs):
        capability = SimpleNamespace(path=source)
        return SimpleNamespace(path=source, capability=capability, close=lambda: None)

    result = await repository.RepoProjectTests(
        state, runner=run, checkout_factory=checkout,
    ).execute()
    assert result.ok is True
    argv, capability, environment, projections, _kwargs = calls[0]
    assert argv == ("/usr/bin/true", "-q")
    assert capability.path == state.checkout_lease.path
    assert environment["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert "GITHUB_TOKEN" not in environment
    assert projections == ()
