"""Finding 2: the evidence gate and local Git must run contained.

The gate's test command executes what the build just wrote -- `pytest` reads
conftest.py, `npm test` reads package.json scripts -- so a trusted command
string naming an untrusted program is not a trusted execution. Local Git over
the same checkout has the same shape.

Push is the deliberate exception: it needs the controller's credential, which is
never projected into a contained step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.worklink import supervisor
from mimir.worklink.evidence import _run, _talks_to_the_remote


@pytest.fixture
def coding_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    monkeypatch.setenv("MIMIR_WORKLINK_USER", "nobody")
    root = tmp_path / "spool"
    supervisor.prepare_spool(root)
    monkeypatch.setenv("MIMIR_WORKLINK_SPOOL", str(root))
    return root


@pytest.mark.parametrize(
    ("argv", "remote"),
    [
        (["git", "-C", "/w/checkout", "push", "-u", "origin", "b"], True),
        (["git", "-C", "/w/checkout", "fetch", "origin"], True),
        (["git", "-C", "/w/checkout", "status", "--porcelain"], False),
        (["git", "-C", "/w/checkout", "add", "-A"], False),
        (["git", "-C", "/w/checkout", "commit", "-m", "x"], False),
        (["git", "-C", "/w/checkout", "rev-parse", "HEAD"], False),
        (["git", "-c", "core.pager=", "-C", "/w/checkout", "push", "origin", "b"], True),
        (["pytest", "-q"], False),
    ],
)
def test_remote_git_is_identified_regardless_of_leading_options(
    argv: list[str], remote: bool,
) -> None:
    """`-C` and `-c` take a VALUE.

    Skipping them as plain flags makes the value read as the subcommand, so
    `git -C /w/checkout push` resolves to "/w/checkout" and a push gets routed
    into containment -- where the credential it needs has been stripped.
    """
    assert _talks_to_the_remote(argv) is remote


def test_the_test_command_runs_contained(tmp_path: Path, coding_on: Path) -> None:
    """The gate's command executes what the build wrote, so it runs contained."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    import threading

    from mimir.worklink.containment import ContainmentPolicy

    policy = ContainmentPolicy(user="nobody", spool_root=coding_on, verified=False)
    stop = threading.Event()

    def pump() -> None:
        while not stop.is_set():
            supervisor.serve_forever(policy, poll_seconds=0, max_iterations=1)

    worker = threading.Thread(target=pump, daemon=True)
    worker.start()
    try:
        result = _run("echo gate-ran; exit 3", cwd=checkout)
    finally:
        stop.set()
        worker.join(timeout=5)

    assert result.returncode == 3
    assert "gate-ran" in result.stdout


def test_a_push_is_never_routed_into_containment(tmp_path: Path, coding_on: Path) -> None:
    """Contained steps have no credential; a contained push could only fail.

    Asserts the request is not spooled at all rather than asserting on the
    outcome, so this holds whether or not a supervisor is running.
    """
    from mimir.worklink.containment import request_dir

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _run(["git", "-C", str(checkout), "push", "-u", "origin", "nope"])
    assert not list(request_dir(coding_on).glob("*.json")), "push was spooled"


def test_evidence_steps_are_inert_without_the_coding_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    monkeypatch.setenv("MIMIR_WORKLINK_SPOOL", str(tmp_path / "spool"))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    result = _run("echo direct", cwd=checkout)
    assert result.returncode == 0
    assert "direct" in result.stdout
    assert not (tmp_path / "spool").exists(), "no spool should have been consulted"


# --------------------------------------------------------------------------
# The production runner, not _run in isolation
# --------------------------------------------------------------------------


def test_the_injected_orchestrator_runner_routes_through_containment(
    tmp_path: Path, coding_on: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_finalize` injects `_runner_for_home`, not `evidence._run`.

    Routing containment only inside `_run` left the gate test and local Git
    executing as the controller in production, while an isolated test of `_run`
    passed. This drives the runner the orchestrator actually injects.

    Asserts the containment entry point is CALLED and its result returned.
    An earlier version of this test asserted the spool inbox was empty, which a
    bypass satisfies just as well as a consumed request -- it passed against the
    very defect it was written for.
    """
    import subprocess as sp

    from mimir.worklink import evidence as evidence_mod
    from mimir.worklink.orchestrator import _runner_for_home

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    seen: list[Path | None] = []
    sentinel = sp.CompletedProcess(args=["contained"], returncode=42, stdout="via-spool", stderr="")

    def spy(args, resolved, env):  # noqa: ANN001, ANN202
        seen.append(resolved)
        return sentinel

    monkeypatch.setattr(evidence_mod, "maybe_run_contained", spy)
    runner = _runner_for_home(tmp_path / "home", "chainlink")
    result = runner(["git", "-C", str(checkout), "status", "--porcelain"])

    assert seen == [checkout], "the orchestrator's runner never reached containment"
    assert result is sentinel, "the contained result was not returned to the caller"
    assert result.stdout == "via-spool"


def test_chainlink_calls_against_the_agent_home_are_never_contained(
    tmp_path: Path, coding_on: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_runner_for_home` points chainlink at the agent home.

    Those are controller operations on controller state. Containing them would
    break chainlink AND mean the worker held a home path, which is the whole
    thing this boundary removes.
    """
    from mimir.worklink.containment import request_dir

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    from mimir.worklink.orchestrator import _runner_for_home

    runner = _runner_for_home(home, "chainlink")
    # A real binary aimed at the home, so a fall-through actually executes and
    # the assertion is about ROUTING rather than about the command existing.
    runner(["git", "-C", str(home), "status", "--porcelain"])
    assert not list(request_dir(coding_on).glob("*.json")), "a home-targeted call was spooled"


def test_push_fails_closed_without_an_observed_oid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to the branch drops the point of observing an oid.

    Whatever HEAD points at when the push runs would get published, including
    anything a process outliving the evidence gate wrote.
    """
    from mimir.worklink.orchestrator import WorklinkError, _git_push

    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    calls: list[list[str]] = []

    def runner(args, cwd=None):  # noqa: ANN001, ANN202
        calls.append(list(args))
        import subprocess as sp

        return sp.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    with pytest.raises(WorklinkError, match="no commit oid was observed"):
        _git_push(tmp_path, "issue/1-a1", runner=runner, oid=None)
    assert not calls, "nothing may be pushed when the oid is missing"


def test_push_targets_the_observed_object_not_the_branch(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _git_push

    calls: list[list[str]] = []

    def runner(args, cwd=None):  # noqa: ANN001, ANN202
        calls.append(list(args))
        import subprocess as sp

        return sp.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    _git_push(tmp_path, "issue/1-a1", runner=runner, oid="deadbeef")
    assert "deadbeef:refs/heads/issue/1-a1" in calls[0]
