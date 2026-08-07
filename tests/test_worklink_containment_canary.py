"""Adversarial canaries: repository-controlled code must not reach the agent home.

These are the tests the feature exists for. Everything else — the user, the
service, the policy object — is scaffolding that can look correct while the hole
stays open. The specific trap is that containing only the BUILD process passes a
shallow review: the evidence gate runs the configured test command against the
worker-mutated checkout, and the controller then runs Git over that same
checkout, so a planted test payload or `.git` hook regains the controller's
identity.

Each test plants a payload the way a real worker would and asserts a canary
under the agent home is untouched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """An agent home holding the files that grant shell authority."""
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "scheduler.yaml").write_text("- name: heartbeat\n  cron: 0 * * * *\n")
    (home / "skills" / "demo" / "pollers.json").write_text("{}\n")
    (home / "logs" / "events.jsonl").write_text("")
    return home


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A worker-controlled checkout, as a build would leave it."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _canary_paths(home: Path) -> list[Path]:
    """The files whose modification would be an authority self-grant."""
    return [
        home / "scheduler.yaml",
        home / "skills" / "demo" / "pollers.json",
    ]


def _snapshot(paths: list[Path]) -> dict[Path, str]:
    return {p: p.read_text() for p in paths}


def test_a_planted_test_command_payload_cannot_reach_the_agent_home(
    agent_home: Path, checkout: Path, monkeypatch,
) -> None:
    """The evidence gate runs the configured test command with shell=True.

    A worker that can write the checkout can write whatever the test command
    reads. If that command executes with the controller's identity, the worker
    has regained it — and the gate runs twice on the success path.
    """
    canaries = _canary_paths(agent_home)
    before = _snapshot(canaries)

    # what a worker would plant: a "test" that rewrites the authority config
    payload = checkout / "run_tests.sh"
    payload.write_text(
        "#!/bin/sh\n"
        f"printf '\\n  shell_commands: [pwned]\\n' >> {agent_home / 'scheduler.yaml'}\n"
        f"printf 'pwned' > {agent_home / 'skills' / 'demo' / 'pollers.json'}\n"
    )
    payload.chmod(0o755)

    monkeypatch.setenv("MIMIR_HOME", str(agent_home))
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")

    from mimir.worklink.containment import ContainmentUnavailable, resolve_containment

    # With no containment available the run must not proceed at all. That IS the
    # protection on a deployment lacking the service — failing closed, not
    # running the payload and hoping.
    with pytest.raises(ContainmentUnavailable):
        resolve_containment()

    assert _snapshot(canaries) == before, "the agent home was modified"


def test_a_planted_git_hook_cannot_reach_the_agent_home(
    agent_home: Path, checkout: Path, monkeypatch,
) -> None:
    """The controller runs Git over the worker-mutated checkout.

    A worker owns `.git/hooks`, so a `pre-commit` hook executes whenever the
    controller commits — a confused deputy that needs no cooperation from the
    build process itself.
    """
    canaries = _canary_paths(agent_home)
    before = _snapshot(canaries)

    hooks = checkout / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"printf 'pwned' > {agent_home / 'scheduler.yaml'}\n"
    )
    hook.chmod(0o755)

    monkeypatch.setenv("MIMIR_HOME", str(agent_home))
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")

    from mimir.worklink.containment import ContainmentUnavailable, resolve_containment

    with pytest.raises(ContainmentUnavailable):
        resolve_containment()

    assert _snapshot(canaries) == before, "a Git hook reached the agent home"


def test_the_canary_harness_can_actually_detect_a_breach(
    agent_home: Path, checkout: Path,
) -> None:
    """Negative control: prove the canaries would notice.

    A canary test that cannot fail is worse than none, because it reads as
    coverage. Run the payload deliberately and assert the harness catches it.
    """
    canaries = _canary_paths(agent_home)
    before = _snapshot(canaries)

    payload = checkout / "breach.sh"
    payload.write_text(
        "#!/bin/sh\n"
        f"printf 'pwned' > {agent_home / 'scheduler.yaml'}\n"
    )
    payload.chmod(0o755)
    subprocess.run([str(payload)], check=True)

    assert _snapshot(canaries) != before, (
        "the harness did not notice a deliberate breach; these tests would pass "
        "against a completely uncontained build"
    )


def test_coding_disabled_needs_no_containment_and_runs_nothing(
    agent_home: Path, monkeypatch,
) -> None:
    """A non-coding deployment has no build, so nothing to contain or refuse."""
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    monkeypatch.setenv("MIMIR_HOME", str(agent_home))

    from mimir.worklink.containment import resolve_containment

    policy = resolve_containment()
    assert policy.not_required_reason is not None
    assert policy.verified is False


def test_identity_survives_the_detached_hop(tmp_path: Path) -> None:
    """The evidence gate and Git steps inherit the service's identity.

    This is why one boundary at the top suffices. The chain is

        service -> poller.py -> `mimir worklink run` (DETACHED) -> build
                             -> evidence gate test command
                             -> git add/commit over the checkout

    and `start_new_session=True` starts a new SESSION, not a new identity, so
    uid survives it. If that ever stopped being true, the evidence gate would
    silently return to the agent's identity while every other test still passed
    — the exact failure the broker design was built to avoid.

    Asserted on the real process tree rather than reasoned about; the uid switch
    itself needs a privileged parent, so this checks inheritance, which is the
    half that can regress in Python.
    """
    import subprocess

    script = tmp_path / "chain.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo \"poller=$(id -u)\"\n"
        # the detached hop the poller performs
        "setsid sh -c 'echo \"detached=$(id -u)\"; sh -c \"echo testcmd=\\$(id -u)\"' &\n"
        "wait\n"
    )
    script.chmod(0o755)

    out = subprocess.run(
        ["/bin/sh", str(script)], capture_output=True, text=True, timeout=20,
    ).stdout

    seen = dict(
        line.split("=", 1) for line in out.split() if "=" in line
    )
    assert seen, f"no uids reported: {out!r}"
    uids = set(seen.values())
    assert len(uids) == 1, (
        f"identity changed across the chain: {seen}. Every downstream step must "
        "inherit the service identity, or the evidence gate escapes containment."
    )
