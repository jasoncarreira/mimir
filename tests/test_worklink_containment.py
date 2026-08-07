"""Unit coverage for the Worklink containment policy and spool protocol.

The adversarial canaries live in ``test_worklink_containment_canary.py``; this
file covers the policy states, the spool's permission split, and the request /
result round trip.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mimir.worklink import supervisor
from mimir.worklink.containment import (
    ContainmentPolicy,
    ContainmentUnavailable,
    WorkerRequest,
    containment_required,
    request_dir,
    resolve_containment,
    result_dir,
    spawn_argv,
)


@pytest.fixture
def coding_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    # A real account that is not this process: the identity check verifies the
    # contained user RESOLVES and differs from the controller uid.
    monkeypatch.setenv("MIMIR_WORKLINK_USER", "nobody")


def test_containment_is_not_required_without_the_coding_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment with no coding tools never runs a build."""
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    assert containment_required() is False
    policy = resolve_containment(spool_root=Path("/nonexistent"))
    assert policy.state == "not_required"
    assert policy.contained is False
    assert policy.not_required_reason is not None
    assert policy.override_reason is None


def test_the_three_policy_states_stay_distinguishable(
    tmp_path: Path, coding_enabled: None,
) -> None:
    """"Verified", "bypassed" and "not applicable" mean different things.

    Collapsing them into one boolean is how an operator bypass comes to look like
    a passing verification in the log.
    """
    supervisor.prepare_spool(tmp_path / "spool")
    verified = resolve_containment(spool_root=tmp_path / "spool")
    override = resolve_containment(
        spool_root=tmp_path / "spool", allow_uncontained="operator ran it by hand",
    )
    assert verified.state == "verified"
    assert override.state == "override"
    assert len({verified.state, override.state}) == 2
    assert override.verified is False, "an override must never read as verified"


def test_dispatch_fails_closed_when_the_spool_is_absent(coding_enabled: None) -> None:
    """A missing supervisor is an error, not a licence to run uncontained."""
    with pytest.raises(ContainmentUnavailable, match="does not exist"):
        resolve_containment(spool_root=Path("/nonexistent/worklink-spool"))


def test_dispatch_fails_closed_on_a_world_writable_spool(
    tmp_path: Path, coding_enabled: None,
) -> None:
    """A world-writable spool lets the contained user forge its own verdict."""
    root = tmp_path / "spool"
    supervisor.prepare_spool(root)
    result_dir(root).chmod(0o777)
    with pytest.raises(ContainmentUnavailable, match="world-writable"):
        resolve_containment(spool_root=root)


def test_prepare_spool_denies_the_contained_user_write_access(tmp_path: Path) -> None:
    """The permission split is the whole reason the root supervisor is worth it.

    If ``worklink`` could write the request inbox it could rewrite its own
    request; if it could write the result directory it could forge the verdict
    that gates its own push.
    """
    root = tmp_path / "spool"
    supervisor.prepare_spool(root)
    for path in (request_dir(root), result_dir(root)):
        mode = path.stat().st_mode
        assert not mode & stat.S_IWOTH, f"{path} is world-writable"
        assert not mode & stat.S_IXOTH, f"{path} is world-traversable"


def test_spawn_argv_prefixes_the_privilege_drop_when_contained(tmp_path: Path) -> None:
    """Only the supervisor execs this -- it runs as root and can drop privilege.

    An earlier revision returned this prefix to the AGENT to exec, which fails
    every time with `unable to set supplementary group list`. Its tests passed
    only because they patched `shutil.which` and never executed anything.
    """
    policy = ContainmentPolicy(user="worklink", spool_root=tmp_path, verified=True)
    argv = spawn_argv(policy, ("pytest", "-q"))
    assert argv[-2:] == ("pytest", "-q")
    assert "s6-setuidgid" in argv[0]
    assert argv[1] == "worklink"


def test_spawn_argv_does_not_drop_privilege_on_the_override_path(tmp_path: Path) -> None:
    policy = ContainmentPolicy(
        user="worklink", spool_root=tmp_path, verified=False, override_reason="manual",
    )
    assert spawn_argv(policy, ("pytest", "-q")) == ("pytest", "-q")


def test_spawn_argv_rejects_an_empty_command(tmp_path: Path) -> None:
    policy = ContainmentPolicy(user="worklink", spool_root=tmp_path, verified=True)
    with pytest.raises(ValueError, match="non-empty"):
        spawn_argv(policy, ())


def test_request_round_trips_through_the_spool(tmp_path: Path) -> None:
    """The real submit -> supervise -> result path, end to end."""
    root = tmp_path / "spool"
    supervisor.prepare_spool(root)
    policy = ContainmentPolicy(user="worklink", spool_root=root, verified=False)
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    from mimir.worklink.containment import await_result, submit_request

    request_id = submit_request(
        policy,
        WorkerRequest(
            attempt_id="round-trip",
            argv=("sh", "-c", "echo out; echo err >&2; exit 7"),
            cwd=checkout,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        ),
    )
    supervisor.serve_forever(policy, poll_seconds=0, max_iterations=1)
    result = await_result(policy, request_id, timeout_seconds=30)

    assert result.attempt_id == "round-trip"
    assert result.exit_status == 7
    assert result.ok is False
    assert "out" in result.stdout
    assert "err" in result.stderr


def test_a_consumed_request_is_removed_from_the_inbox(tmp_path: Path) -> None:
    """Otherwise the supervisor re-runs every build on every poll."""
    root = tmp_path / "spool"
    supervisor.prepare_spool(root)
    policy = ContainmentPolicy(user="worklink", spool_root=root, verified=False)
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    from mimir.worklink.containment import submit_request

    submit_request(
        policy,
        WorkerRequest(attempt_id="once", argv=("true",), cwd=checkout, env={"PATH": "/usr/bin:/bin"}),
    )
    assert list(request_dir(root).glob("*.json"))
    supervisor.serve_forever(policy, poll_seconds=0, max_iterations=1)
    assert not list(request_dir(root).glob("*.json"))


def test_await_result_fails_closed_when_no_supervisor_is_running(tmp_path: Path) -> None:
    """A build whose result never arrives must not be read as a success."""
    root = tmp_path / "spool"
    supervisor.prepare_spool(root)
    policy = ContainmentPolicy(user="worklink", spool_root=root, verified=False)
    from mimir.worklink.containment import await_result

    with pytest.raises(ContainmentUnavailable, match="published no result"):
        await_result(policy, "never-submitted", timeout_seconds=0.2, poll_seconds=0.01)
