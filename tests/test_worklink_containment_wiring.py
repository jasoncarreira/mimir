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
    # A real account that is not this process: the identity check verifies the
    # contained user RESOLVES and differs from the controller uid.
    monkeypatch.setenv("MIMIR_WORKLINK_USER", "nobody")
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
    # A real account that is not this process: the identity check verifies the
    # contained user RESOLVES and differs from the controller uid.
    monkeypatch.setenv("MIMIR_WORKLINK_USER", "nobody")
    monkeypatch.setenv("MIMIR_WORKLINK_SPOOL", str(tmp_path / "absent"))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    backend = LocalSubprocessComputeBackend()

    with pytest.raises(ContainmentUnavailable, match="does not exist"):
        asyncio.run(backend.launch(_spec(checkout, ("true",))))


# --------------------------------------------------------------------------
# Regressions for the review of 21fad15c0
# --------------------------------------------------------------------------


def test_concurrent_spooled_builds_get_distinct_handles(
    tmp_path: Path, coding_on: Path,
) -> None:
    """A spooled job has no pid, and the fallback identifier is "unknown".

    Every concurrent spooled launch would collide on one `_jobs` key, so
    wait/cancel/cleanup could target the wrong request. worklink.yaml permits
    concurrent attempts, so this is reachable.
    """
    backend = LocalSubprocessComputeBackend()

    async def run() -> tuple[str, str]:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        h1 = await backend.launch(_spec(a, ("true",)))
        h2 = await backend.launch(_spec(b, ("true",)))
        return h1.identifier, h2.identifier

    first, second = asyncio.run(run())
    assert first != second, "concurrent spooled builds collided on one handle"
    assert "unknown" not in (first, second)
    assert len(backend._jobs) == 2, "one job overwrote the other"


def test_the_configured_timeout_reaches_the_supervisor(
    tmp_path: Path, coding_on: Path,
) -> None:
    """Without this the supervisor spawns with no deadline at all."""
    import json

    from mimir.worklink.containment import request_dir

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    backend = LocalSubprocessComputeBackend()
    spec = _spec(checkout, ("true",))
    asyncio.run(backend.launch(spec))

    queued = list(request_dir(coding_on).glob("*.json"))
    assert len(queued) == 1
    payload = json.loads(queued[0].read_text())
    assert payload["timeout_seconds"] == float(spec.timeout_s)


def test_the_supervisor_enforces_the_deadline_on_a_hung_step(
    tmp_path: Path, coding_on: Path,
) -> None:
    """A hung generated process must not outlive its configured timeout.

    This really does hang -- `sleep 300` with a 1s deadline -- so the assertion
    is that the supervisor killed it, not that a mock reported it did.
    """
    from mimir.worklink.containment import (
        ContainmentPolicy,
        WorkerRequest,
        await_result,
        submit_request,
    )

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    policy = ContainmentPolicy(user="nobody", spool_root=coding_on, verified=False)
    request_id = submit_request(
        policy,
        WorkerRequest(
            attempt_id="hung",
            argv=("sh", "-c", "sleep 300"),
            cwd=checkout,
            env={"PATH": "/usr/bin:/bin"},
            timeout_seconds=1.0,
        ),
    )
    supervisor.serve_forever(policy, poll_seconds=0, max_iterations=1)
    result = await_result(policy, request_id, timeout_seconds=30)

    assert result.timed_out is True
    assert result.exit_status == 124
    assert "timed out" in result.stderr


def test_cancellation_terminates_a_step_the_supervisor_already_claimed(
    tmp_path: Path, coding_on: Path,
) -> None:
    """Unlinking a queued request cannot stop one that is already running.

    The step belongs to the contained user, so only the root supervisor can
    signal it; the controller publishes the cancellation instead.
    """
    import threading

    from mimir.worklink.containment import (
        ContainmentPolicy,
        WorkerRequest,
        await_result,
        publish_cancellation,
        submit_request,
    )

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    policy = ContainmentPolicy(user="nobody", spool_root=coding_on, verified=False)
    request_id = submit_request(
        policy,
        WorkerRequest(
            attempt_id="cancelme",
            argv=("sh", "-c", "sleep 300"),
            cwd=checkout,
            env={"PATH": "/usr/bin:/bin"},
        ),
    )
    threading.Timer(1.0, lambda: publish_cancellation(policy, request_id)).start()
    supervisor.serve_forever(policy, poll_seconds=0, max_iterations=1)
    result = await_result(policy, request_id, timeout_seconds=30)

    assert result.exit_status == 124
    assert "cancelled" in result.stderr


def test_a_contained_user_matching_the_controller_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configuration that does not match reality is what this leaf replaces.

    Checking only the spool's world-write bit would accept
    MIMIR_WORKLINK_USER=<the controller> as "verified" while containing nothing.
    """
    import getpass

    from mimir.worklink.containment import resolve_containment

    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    monkeypatch.setenv("MIMIR_WORKLINK_USER", getpass.getuser())
    root = tmp_path / "spool"
    supervisor.prepare_spool(root)
    with pytest.raises(ContainmentUnavailable, match="no-op"):
        resolve_containment(spool_root=root)


def test_an_unresolvable_contained_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.worklink.containment import resolve_containment

    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    monkeypatch.setenv("MIMIR_WORKLINK_USER", "no-such-account-here")
    root = tmp_path / "spool"
    supervisor.prepare_spool(root)
    with pytest.raises(ContainmentUnavailable, match="does not exist"):
        resolve_containment(spool_root=root)


def test_cancelling_before_the_supervisor_claims_still_publishes_a_result(
    tmp_path: Path, coding_on: Path,
) -> None:
    """Otherwise the waiter blocks its full deadline on a step that is gone.

    Unlinking an unclaimed request removes the only thing that would ever have
    produced a result.
    """
    from mimir.worklink.containment import await_result

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    backend = LocalSubprocessComputeBackend()

    async def run() -> object:
        handle = await backend.launch(_spec(checkout, ("sleep", "300")))
        proc, _s, _c = backend._job(handle)
        proc.cancel_request()          # never claimed: no supervisor is running
        return proc

    proc = asyncio.run(run())
    from mimir.worklink.containment import ContainmentPolicy

    policy = ContainmentPolicy(user="nobody", spool_root=coding_on, verified=False)
    result = await_result(policy, proc.request_id, timeout_seconds=5)
    assert result.exit_status == 124
    assert "before the supervisor claimed it" in result.stderr


def test_the_checkout_is_handed_to_the_contained_user_before_the_step_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The controller clones the repo, so the checkout is agent-owned.

    Without a handoff the contained user can read it but not write it, and the
    build fails on its first edit. Chowning /workspace/.worklink in the image
    does not cover it: attempt checkouts are per-run, and repositories can be
    configured anywhere.
    """
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        supervisor,
        "ensure_checkout_ownership",
        lambda checkout, user: calls.append((checkout, user)) or False,
    )
    from mimir.worklink.containment import ContainmentPolicy, WorkerRequest, submit_request

    root = tmp_path / "spool"
    supervisor.prepare_spool(root)
    policy = ContainmentPolicy(user="nobody", spool_root=root, verified=False)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    submit_request(
        policy,
        WorkerRequest(
            attempt_id="handoff", argv=("true",), cwd=checkout, env={"PATH": "/usr/bin:/bin"},
        ),
    )
    supervisor.serve_forever(policy, poll_seconds=0, max_iterations=1)
    assert calls == [(checkout, "nobody")]


def test_the_ownership_handoff_is_a_no_op_without_root(tmp_path: Path) -> None:
    """Only this service can chown to another user; the controller has CapEff=0.

    Off-root it must not pretend to have done it -- the return value is what a
    caller would use to decide whether the boundary is real.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    assert supervisor.ensure_checkout_ownership(checkout, "nobody") is False


def test_the_worker_gets_its_own_runtime_home_not_the_controllers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stripping MIMIR_HOME and the tokens is only half the projection.

    A step inheriting the CONTROLLER's HOME/XDG has nowhere writable for a
    coding CLI's config or caches, and every path resolved from HOME points at
    the identity being contained from.
    """
    import pwd

    from mimir.worklink.containment import ContainmentPolicy, worker_runtime_env

    policy = ContainmentPolicy(user="nobody", spool_root=tmp_path, verified=True)
    base = {
        "HOME": "/home/mimir",
        "USER": "mimir",
        "LOGNAME": "mimir",
        "XDG_CONFIG_HOME": "/home/mimir/.config",
        "MIMIR_HOME": "/home/mimir/agent",
        "GITHUB_TOKEN": "ghp_x",
        "PATH": "/usr/bin:/bin",
    }
    env = worker_runtime_env(policy, base)

    assert env["PATH"] == "/usr/bin:/bin", "unrelated vars must survive"
    for leaked in ("MIMIR_HOME", "GITHUB_TOKEN"):
        assert leaked not in env
    expected_home = pwd.getpwnam("nobody").pw_dir
    if expected_home:
        assert env["HOME"] == expected_home
        assert env["HOME"] != "/home/mimir"
        assert env["USER"] == "nobody"
        assert env["XDG_CONFIG_HOME"].startswith(expected_home)
