"""Containment must be REACHABLE from the build path, and inert without the flag.

Two failure modes, opposite directions, both silent:

  - A containment layer nothing calls. An earlier revision of this branch shipped
    one; it verified path existence, was wired to no call site, and every test
    passed. This file drives ``LocalSubprocessComputeBackend.launch`` itself so
    that cannot recur.

  - Containment that fires on a deployment which never runs builds. Gated on
    ``MIMIR_CODING_ENABLED``: with the flag off the build path must behave
    EXACTLY as it did before, spawning in-process with no spool consulted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mimir.worklink import supervisor
from mimir.worklink.compute import (
    LocalSubprocessComputeBackend,
    WorkSpec,
    _containment_policy,
    _SpooledJob,
)
from mimir.worklink.containment import ContainmentUnavailable


def _spec(checkout: Path, argv: tuple[str, ...]) -> WorkSpec:
    return WorkSpec(
        issue_id=441,
        attempt=1,
        branch="issue/441-a1",
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="main",
        prompt="do the thing",
        test_command="true",
        local_checkout=checkout,
        local_argv=list(argv),
        env={},
        rules="",
        backend="local_subprocess",
        timeout_s=60,
    )


# --------------------------------------------------------------------------
# Inert without the coding flag
# --------------------------------------------------------------------------


def test_no_policy_is_resolved_without_the_coding_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole feature is off, and no spool is consulted to discover that."""
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    monkeypatch.setenv("MIMIR_WORKLINK_SPOOL", "/nonexistent/spool")
    # Would raise ContainmentUnavailable if the spool were consulted at all.
    assert _containment_policy() is None


def test_build_spawns_in_process_without_the_coding_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off: the build runs exactly as it did before chainlink #1164."""
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    monkeypatch.setenv("MIMIR_WORKLINK_SPOOL", str(tmp_path / "spool"))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    backend = LocalSubprocessComputeBackend()

    async def run() -> object:
        handle = await backend.launch(_spec(checkout, ("sh", "-c", "echo hi")))
        proc, _spec_, _cmd = backend._job(handle)
        result = await backend.wait(handle, timeout_s=30)
        return proc, result

    proc, result = asyncio.run(run())
    assert not isinstance(proc, _SpooledJob), "must not route through the spool"
    assert result.exit_code == 0
    assert "hi" in result.stdout
    assert not (tmp_path / "spool").exists(), "no spool should have been created"


# --------------------------------------------------------------------------
# Reachable when the flag is on
# --------------------------------------------------------------------------


@pytest.fixture
def coding_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    root = tmp_path / "spool"
    supervisor.prepare_spool(root)
    monkeypatch.setenv("MIMIR_WORKLINK_SPOOL", str(root))
    return root


def test_build_is_routed_through_the_spool_when_contained(
    tmp_path: Path, coding_on: Path,
) -> None:
    """The wiring: launch() hands the build to the supervisor, not to fork/exec."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    backend = LocalSubprocessComputeBackend()

    async def run() -> object:
        handle = await backend.launch(_spec(checkout, ("sh", "-c", "echo built; exit 0")))
        proc, _spec_, _cmd = backend._job(handle)
        assert isinstance(proc, _SpooledJob), "the build must go through the spool"
        # The real supervisor consumes it, in a thread so the loop keeps running.
        from mimir.worklink.containment import ContainmentPolicy

        policy = ContainmentPolicy(user="worklink", spool_root=coding_on, verified=False)
        await asyncio.to_thread(
            supervisor.serve_forever, policy, poll_seconds=0, max_iterations=1,
        )
        return await backend.wait(handle, timeout_s=60)

    result = asyncio.run(run())
    assert result.exit_code == 0
    assert "built" in result.stdout


def test_the_contained_build_never_receives_the_push_credential(
    tmp_path: Path, coding_on: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Push and PR are controller-side.

    ``_local_child_env`` passes GITHUB_TOKEN/GH_TOKEN through as "provider
    credentials", which predates this boundary. A contained build must not get
    them, and ``submit_request`` would refuse the request if it did.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_reach_the_worker")
    monkeypatch.setenv("GH_TOKEN", "gho_should_not_reach_the_worker")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    backend = LocalSubprocessComputeBackend()

    async def run() -> object:
        handle = await backend.launch(_spec(checkout, ("true",)))
        proc, _spec_, _cmd = backend._job(handle)
        return proc

    proc = asyncio.run(run())
    assert isinstance(proc, _SpooledJob)
    assert "GITHUB_TOKEN" not in proc._env
    assert "GH_TOKEN" not in proc._env
    assert "MIMIR_HOME" not in proc._env


def test_dispatch_fails_closed_when_the_supervisor_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on but no spool: refuse, rather than run the build as the agent."""
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    monkeypatch.setenv("MIMIR_WORKLINK_SPOOL", str(tmp_path / "absent"))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    backend = LocalSubprocessComputeBackend()

    with pytest.raises(ContainmentUnavailable, match="does not exist"):
        asyncio.run(backend.launch(_spec(checkout, ("true",))))
