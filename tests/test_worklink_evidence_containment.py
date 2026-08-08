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
import threading
from pathlib import Path

import pytest

from mimir.worklink import supervisor
from mimir.worklink.evidence import _run, _talks_to_the_remote


@pytest.fixture(autouse=True)
def _isolate_attempt_registry():
    """The registry is module-level mutable state shared by the whole process.

    Without this, a checkout registered by one test authorises containment in
    another, so a test can pass on state it does not own -- and the negative
    tests here depend on a path NOT being registered.
    """
    from mimir.worklink import containment

    saved = set(containment._ATTEMPT_CHECKOUTS)
    containment._ATTEMPT_CHECKOUTS.clear()
    try:
        yield
    finally:
        containment._ATTEMPT_CHECKOUTS.clear()
        containment._ATTEMPT_CHECKOUTS.update(saved)


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
    from mimir.worklink.containment import register_attempt_checkout

    register_attempt_checkout(checkout)

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
    from mimir.worklink.containment import register_attempt_checkout

    register_attempt_checkout(checkout)
    _run(["git", "-C", str(checkout), "push", "-u", "origin", "nope"])
    assert not list(request_dir(coding_on).glob("*.json")), "push was spooled"


def test_evidence_steps_are_inert_without_the_coding_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    monkeypatch.setenv("MIMIR_WORKLINK_SPOOL", str(tmp_path / "spool"))
    checkout = tmp_path / "repo" / ".worklink" / "441-a1"
    checkout.mkdir(parents=True)
    from mimir.worklink.containment import register_attempt_checkout

    register_attempt_checkout(checkout)
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
    from mimir.worklink.containment import register_attempt_checkout

    register_attempt_checkout(checkout)
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
    from mimir.worklink.orchestrator import _git_push

    calls: list[list[str]] = []

    def runner(args, cwd=None):  # noqa: ANN001, ANN202
        calls.append(list(args))
        import subprocess as sp

        return sp.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    _git_push(
        Path("/tmp/repo"), "issue/1-a1", runner=runner, oid="deadbeef",
        remote_url="https://github.com/o/r.git",
    )
    argv = calls[0]
    for setting in ("core.hooksPath=/dev/null", "core.fsmonitor=", "diff.external="):
        assert setting in argv, f"{setting} missing from the push argv"
    # NOT `-c credential.helper=`: an empty value RESETS git's helper list and -c
    # wins, so it would also remove the deployment's `gh auth setup-git` helper
    # and leave a token-free HTTPS URL with no credential. Those keys are removed
    # from the CHECKOUT's own config instead.
    assert "credential.helper=" not in argv, (
        "clearing credential.helper removes the deployment's helper, not just the worker's"
    )
    assert argv.index("push") > argv.index("-C"), "overrides must precede the subcommand"
    # NOT _MAINTENANCE_GIT_BASE_OVERRIDES wholesale: it carries
    # protocol.allow=never, which is right for local maintenance and fatal for a
    # push -- verified, git dies with "transport 'https' not allowed". Reusing
    # the constant here broke every real push while this argv check still passed.
    assert "protocol.allow=never" not in argv, (
        "protocol.allow=never makes the push fail with transport 'https' not allowed"
    )
    assert argv[-2] == "https://github.com/o/r.git", "destination must be the controller's URL"


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
    # They are no longer ABSENT -- they are re-pointed at worker-owned paths,
    # because deleting them leaves the CLI with no config at all. The property
    # that matters is that neither still names the controller's home.
    assert str(agent_home) not in env.get("OPENCODE_CONFIG", "")
    assert str(agent_home) not in env.get("CODEX_HOME", "")
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
    from mimir.worklink.containment import register_attempt_checkout

    register_attempt_checkout(checkout)
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


def test_the_hardened_push_actually_works_against_a_real_remote(tmp_path: Path) -> None:
    """Executes the push instead of inspecting its argv.

    The argv-inspecting test above passed while the shipped command was
    unrunnable: _MAINTENANCE_GIT_BASE_OVERRIDES carries protocol.allow=never, so
    every real push died with "transport 'https' not allowed". Only running one
    catches that class of defect.

    Uses a local bare remote so no network is involved.
    """
    import subprocess as sp

    from mimir.worklink.orchestrator import _git_push

    def git(*args: str, cwd: Path) -> sp.CompletedProcess[str]:
        return sp.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)

    remote = tmp_path / "remote.git"
    remote.mkdir()
    if git("init", "--bare", "-q", cwd=remote).returncode != 0:
        pytest.skip("git unavailable")
    work = tmp_path / "repo" / ".worklink" / "441-a1"
    work.mkdir(parents=True)
    git("init", "-q", cwd=work)
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "t", cwd=work)
    (work / "f.txt").write_text("x")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "x", cwd=work)
    git("branch", "-M", "issue/441-a1", cwd=work)
    oid = git("rev-parse", "HEAD", cwd=work).stdout.strip()

    # Planted config the push must neutralise. Asserted AFTER _git_push, so
    # removing the scrub call from the push path fails this test -- a direct
    # test of the scrub helper alone does not.
    # Planted via include.path -- the indirection that defeats key deletion.
    # Unsetting credential.helper leaves it resolving through the include, so a
    # scrub-based fix passes a direct-key test and fails here.
    decoy = tmp_path / "decoy.git"
    decoy.mkdir()
    git("init", "--bare", "-q", cwd=decoy)
    evil = tmp_path / "evil.cfg"
    # The rewrite must target the destination this test ACTUALLY pushes to, or
    # it never fires and the test cannot distinguish anything. An earlier
    # version planted a github.com rewrite against a local remote and survived
    # the mutation that removes the protection entirely.
    evil.write_text(f'[url "{decoy}"]\n\tinsteadOf = {remote}\n')
    git("config", "--local", "include.path", str(evil), cwd=work)
    rewritten = git("config", "--get-urlmatch", "url", str(remote), cwd=work)
    assert git("config", "--get", f"url.{decoy}.insteadOf", cwd=work).stdout.strip() == str(remote), (
        "the include must be live before the push, or this proves nothing"
    )

    # A planted hook that would fail the push if hooks were honoured.
    hooks = work / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text("#!/bin/sh\necho PLANTED >&2\nexit 1\n")
    hook.chmod(0o755)

    captured: list[sp.CompletedProcess[str]] = []

    def runner(args, cwd=None):  # noqa: ANN001, ANN202
        result = sp.run(list(args), capture_output=True, text=True, check=False)
        captured.append(result)
        return result

    # A worker process that survives the verdict and attacks the config path.
    # The previous implementation renamed and rewrote <checkout>/.git/config in
    # place, so this could redirect a controller write to an arbitrary path or
    # swap attacker config back before git opened it.
    victim = tmp_path / "controller-would-clobber-this"
    attacker_stop = threading.Event()

    def attack() -> None:
        cfg = work / ".git" / "config"
        while not attacker_stop.is_set():
            try:
                if not cfg.is_symlink():
                    cfg.unlink(missing_ok=True)
                    cfg.symlink_to(victim)
            except OSError:
                pass

    attacker = threading.Thread(target=attack, daemon=True)
    attacker.start()
    try:
        _git_push(work, "issue/441-a1", runner=runner, oid=oid, remote_url=str(remote))
    finally:
        attacker_stop.set()
        attacker.join(timeout=5)

    assert not victim.exists(), "the controller wrote through a worker-planted symlink"

    assert captured[0].returncode == 0, f"push failed: {captured[0].stderr}"
    # Git must not have been pointed at the checkout at all.
    pushed_from = captured[0].args[captured[0].args.index("-C") + 1]
    assert str(work) != pushed_from, "the push ran inside the worker-owned checkout"
    assert "PLANTED" not in captured[0].stderr, "the worker-planted pre-push hook ran"
    landed = sp.run(
        ["git", "rev-parse", "refs/heads/issue/441-a1"],
        cwd=remote, capture_output=True, text=True, check=False,
    ).stdout.strip()
    assert landed == oid, "the observed object is what reached the remote"
    # The worker's config is restored afterwards -- the point is that it was not
    # consulted DURING the push, which the successful landing above shows: an
    # honoured url.*.insteadOf would have redirected the push away from the
    # local remote entirely.
    decoy_ref = sp.run(
        ["git", "rev-parse", "refs/heads/issue/441-a1"],
        cwd=decoy, capture_output=True, text=True, check=False,
    ).stdout.strip()
    assert decoy_ref != oid, "the worker's url.*.insteadOf redirected the push"
    # No assertion that the checkout's config is "restored": the controller
    # never modifies it now. The attacker thread above is what mangles it, and
    # that is exactly the point -- the push succeeded, landed the right object,
    # and honoured none of the worker's config while an attacker was actively
    # rewriting that file.


def test_an_unregistered_worklink_path_is_declined(
    tmp_path: Path, coding_on: Path,
) -> None:
    """The `.worklink` marker alone must not authorise a chown.

    A configured repository sitting under some `.worklink` directory satisfies
    the marker, and the supervisor chowns whatever cwd it is handed. Only a
    checkout the orchestrator actually issued may cross.
    """
    from mimir.worklink.containment import request_dir
    from mimir.worklink.evidence import _run

    impostor = tmp_path / "elsewhere" / ".worklink" / "not-ours"
    impostor.mkdir(parents=True)
    _run(["git", "-C", str(impostor), "status", "--porcelain"])
    spooled = list(request_dir(coding_on).glob("*.json"))
    assert not spooled, f"an unregistered .worklink path was spooled: {spooled}"


def test_a_registered_checkout_is_accepted(tmp_path: Path, coding_on: Path) -> None:
    """The positive half, so the guard cannot pass by declining everything."""
    from mimir.worklink.containment import register_attempt_checkout, request_dir
    from mimir.worklink.evidence import maybe_run_contained

    real = tmp_path / "repo" / ".worklink" / "441-a1"
    real.mkdir(parents=True)
    register_attempt_checkout(real)
    # Submits and would wait; assert on the spool instead of blocking.
    import threading

    t = threading.Thread(
        target=maybe_run_contained,
        args=(["git", "-C", str(real), "status"], real, {"PATH": "/usr/bin:/bin"}),
        daemon=True,
    )
    t.start()
    t.join(timeout=3)
    assert list(request_dir(coding_on).glob("*.json")), "a registered checkout was not spooled"


def test_the_worker_gets_its_own_cli_config_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping the controller's OPENCODE_CONFIG leaves the CLI with none.

    The worker cannot read the controller's copy (0600 under a 0700 home), so
    the projection has to point somewhere it owns, not just delete the variable.
    """
    import pwd

    from mimir.worklink.containment import ContainmentPolicy, worker_runtime_env

    agent_home = tmp_path / "mimir-home"
    agent_home.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(agent_home))
    policy = ContainmentPolicy(user="nobody", spool_root=tmp_path, verified=True)
    env = worker_runtime_env(policy, {"OPENCODE_CONFIG": str(agent_home / "opencode.jsonc")})
    worker_home = pwd.getpwnam("nobody").pw_dir
    if not worker_home:
        pytest.skip("no home for the test account")
    for key in ("OPENCODE_CONFIG", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        assert key in env, f"{key} must be projected, not merely dropped"
        assert env[key].startswith(worker_home), f"{key}={env[key]} is not worker-owned"
        assert str(agent_home) not in env[key]



def test_provider_material_is_copied_into_the_worker_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty directories give the CLI no config and no credential.

    worker_runtime_env points OpenCode/Codex at paths under the worker's home,
    so those paths have to hold real material or the configured invocation
    cannot start under the worker uid.
    """
    from mimir.worklink.supervisor import _project_provider_material

    controller = tmp_path / "controller-home"
    (controller / ".local" / "share" / "opencode").mkdir(parents=True)
    (controller / ".local" / "share" / "opencode" / "auth.json").write_text('{"t":"secret"}')
    (controller / ".config" / "opencode").mkdir(parents=True)
    (controller / ".config" / "opencode" / "opencode.jsonc").write_text("{}")
    (controller / ".codex").mkdir()
    (controller / ".codex" / "auth.json").write_text('{"c":"secret"}')
    monkeypatch.setenv("MIMIR_CONTROLLER_HOME", str(controller))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)

    worker = tmp_path / "worker-home"
    worker.mkdir()
    projected = _project_provider_material(worker, os.getuid(), os.getgid())

    assert ".local/share/opencode/auth.json" in projected
    assert ".codex/auth.json" in projected
    assert (worker / ".local" / "share" / "opencode" / "auth.json").read_text() == '{"t":"secret"}'
    assert (worker / ".config" / "opencode" / "opencode.jsonc").read_text() == "{}"
    # A COPY, not a link: the worker must not be able to reach the controller's
    # home, which is the boundary this feature exists to draw.
    assert not (worker / ".codex" / "auth.json").is_symlink()
    mode = (worker / ".codex" / "auth.json").stat().st_mode & 0o777
    assert mode == 0o600, f"projected credential is mode {oct(mode)}"


def test_projection_honours_a_configured_opencode_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPENCODE_CONFIG may point outside the default location."""
    from mimir.worklink.supervisor import _project_provider_material

    controller = tmp_path / "controller-home"
    controller.mkdir()
    custom = tmp_path / "elsewhere" / "opencode.jsonc"
    custom.parent.mkdir(parents=True)
    custom.write_text('{"model":"x"}')
    monkeypatch.setenv("MIMIR_CONTROLLER_HOME", str(controller))
    monkeypatch.setenv("OPENCODE_CONFIG", str(custom))

    worker = tmp_path / "worker-home"
    worker.mkdir()
    _project_provider_material(worker, os.getuid(), os.getgid())
    assert (worker / ".config" / "opencode" / "opencode.jsonc").read_text() == '{"model":"x"}'
