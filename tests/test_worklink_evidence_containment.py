"""Finding 2: the evidence gate and local Git must run contained.

The gate's test command executes what the build just wrote -- `pytest` reads
conftest.py, `npm test` reads package.json scripts -- so a trusted command
string naming an untrusted program is not a trusted execution. Local Git over
the same checkout has the same shape.

Push is the deliberate exception: it needs the controller's credential, which is
never projected into a contained step.
"""

from __future__ import annotations

import os
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
    checkout = tmp_path / "repo" / ".worklink" / "441-a1"
    checkout.mkdir(parents=True)

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

    checkout = tmp_path / "repo" / ".worklink" / "441-a1"
    checkout.mkdir(parents=True)
    _run(["git", "-C", str(checkout), "push", "-u", "origin", "nope"])
    assert not list(request_dir(coding_on).glob("*.json")), "push was spooled"


def test_evidence_steps_are_inert_without_the_coding_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    monkeypatch.setenv("MIMIR_WORKLINK_SPOOL", str(tmp_path / "spool"))
    checkout = tmp_path / "repo" / ".worklink" / "441-a1"
    checkout.mkdir(parents=True)
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

    checkout = tmp_path / "repo" / ".worklink" / "441-a1"
    checkout.mkdir(parents=True)
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


def test_the_parent_repository_is_never_spooled(
    tmp_path: Path, coding_on: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supervisor chowns whatever cwd it is handed, recursively.

    `_finalize` runs `git -C <parent repo> status` for dirty-path checks on every
    run. A deny-list router ("not the home, not remote") sent that to the
    supervisor, which would have handed the operator's own source repository to
    the worker uid. Reachable on every run, not a corner case.
    """
    from mimir.worklink.containment import request_dir
    from mimir.worklink.orchestrator import _runner_for_home

    parent = tmp_path / "repo"
    parent.mkdir()
    # No mocking: the real router must decline, so this falls through to a direct
    # subprocess and never reaches the spool. Mocking maybe_run_contained here
    # would replace the very check under test.
    runner = _runner_for_home(tmp_path / "home", "chainlink")
    runner(["git", "-C", str(parent), "status", "--porcelain"])
    spooled = list(request_dir(coding_on).glob("*.json"))
    assert not spooled, f"the parent repository was spooled and would be chowned: {spooled}"


@pytest.mark.parametrize(
    ("path", "contained"),
    [
        ("repo/.worklink/441-a1", True),
        ("repo/.worklink/441-a1/src/pkg", True),
        ("repo", False),
        ("repo/src", False),
        ("home/agent", False),
    ],
)
def test_only_attempt_checkouts_cross_the_boundary(
    tmp_path: Path, path: str, contained: bool,
) -> None:
    from mimir.worklink.evidence import _is_attempt_checkout

    target = tmp_path / path
    target.mkdir(parents=True, exist_ok=True)
    assert _is_attempt_checkout(target) is contained


def test_push_disables_what_the_worker_owned_checkout_could_execute() -> None:
    """The refspec pins WHAT is pushed; it does not pin what the push EXECUTES.

    By push time the checkout is worker-owned, so its .git/config and hooks are
    worker-written. pre-push, core.sshCommand and credential.helper would all run
    as the controller, holding the credential.
    """
    from mimir.access_control import _MAINTENANCE_GIT_BASE_OVERRIDES
    from mimir.worklink.orchestrator import _git_push

    calls: list[list[str]] = []

    def runner(args, cwd=None):  # noqa: ANN001, ANN202
        calls.append(list(args))
        import subprocess as sp

        return sp.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    _git_push(Path("/tmp/repo"), "issue/1-a1", runner=runner, oid="deadbeef")
    argv = calls[0]
    for override in _MAINTENANCE_GIT_BASE_OVERRIDES:
        assert override in argv, f"{override} missing from the push argv"
    assert "core.hooksPath=/dev/null" in argv
    assert argv.index("push") > argv.index("-C"), "overrides must precede the subcommand"


def test_worker_env_drops_variables_naming_the_controller_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rewriting HOME/XDG leaves variables that name a controller path outright.

    OPENCODE_CONFIG=/home/mimir/... is the live example: the worker cannot read
    it (0700) and it points at the identity being contained from.
    """
    from mimir.worklink.containment import ContainmentPolicy, worker_runtime_env

    agent_home = tmp_path / "mimir-home"
    agent_home.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(agent_home))
    policy = ContainmentPolicy(user="nobody", spool_root=tmp_path, verified=True)
    env = worker_runtime_env(
        policy,
        {
            "OPENCODE_CONFIG": str(agent_home / "opencode.jsonc"),
            "CODEX_HOME": str(agent_home / ".codex"),
            "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
        },
    )
    assert "OPENCODE_CONFIG" not in env
    assert "CODEX_HOME" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LANG"] == "en_US.UTF-8", "unrelated values must survive"


def test_the_push_oid_comes_from_the_supervisors_own_read(tmp_path: Path) -> None:
    """Not from `git rev-parse` stdout, which originates on the judged side."""
    from mimir.worklink import supervisor as sup
    from mimir.worklink.containment import (
        ContainmentPolicy,
        WorkerRequest,
        await_result,
        submit_request,
    )

    root = tmp_path / "spool"
    sup.prepare_spool(root)
    policy = ContainmentPolicy(user="nobody", spool_root=root, verified=False)
    checkout = tmp_path / "repo" / ".worklink" / "441-a1"
    checkout.mkdir(parents=True)
    import subprocess as sp

    for cmd in (["git", "init", "-q"], ["git", "add", "-A"]):
        sp.run(cmd, cwd=checkout, capture_output=True, check=False)
    (checkout / "f.txt").write_text("x")
    sp.run(["git", "add", "-A"], cwd=checkout, capture_output=True, check=False)
    sp.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "x"],
        cwd=checkout, capture_output=True, check=False,
    )
    expected = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, check=False,
    ).stdout.strip()
    if not expected:
        pytest.skip("git unavailable in this environment")

    rid = submit_request(
        policy,
        WorkerRequest(
            attempt_id="oid", argv=("true",), cwd=checkout,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, report_head=True,
        ),
    )
    sup.serve_forever(policy, poll_seconds=0, max_iterations=1)
    result = await_result(policy, rid, timeout_seconds=30)
    assert result.head_oid == expected, "the supervisor must report the oid it read"
