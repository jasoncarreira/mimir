"""Adversarial canaries for Worklink containment.

What these prove, and what they do not
--------------------------------------
These drive the REAL path -- ``submit_request`` -> the real supervisor's
``handle_one_request`` -> a real ``subprocess`` -> ``await_result`` -- with a
payload that genuinely executes and genuinely tries to write the agent home. An
earlier revision of this branch asserted an exception and then observed that a
payload it had never run changed nothing; that passes against a completely
uncontained build, which is why it is not done here.

What a sandbox CAN enforce, and what these therefore test, is the projection
boundary: ``MIMIR_HOME`` and the GitHub credentials are never placed in a
contained step's environment, so generated code has no way to locate the agent
home. That is a real control with a real failure mode, and the negative control
below proves the harness would notice if it broke.

What a sandbox CANNOT enforce is the uid boundary: without root there is no
privilege drop, so a payload that hard-codes an absolute path still writes it.
That half is verified by the operator against a rebuilt container and is listed
as an operator step on chainlink #1164. These tests do not claim it, and a
reviewer should not read them as covering it.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from mimir.worklink import supervisor
from mimir.worklink.containment import (
    ContainmentPolicy,
    WorkerRequest,
    await_result,
    submit_request,
)


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    root = tmp_path / "spool"
    supervisor.prepare_spool(root)
    return root


@pytest.fixture
def policy(spool: Path) -> ContainmentPolicy:
    # verified=False so the test does not try to exec `s6-setuidgid`, which needs
    # root. The request/result protocol and the environment projection -- the
    # parts a sandbox can actually enforce -- are identical either way.
    return ContainmentPolicy(user="worklink", spool_root=spool, verified=False)


def _payload(canary: Path) -> str:
    """A build step that tries to escalate exactly the way the issue describes.

    It looks the agent home up the way generated code would -- from the
    environment -- and appends a shell_commands grant to scheduler.yaml.
    """
    return textwrap.dedent(
        f"""
        import os, pathlib, sys
        home = os.environ.get("MIMIR_HOME")
        if not home:
            print("payload: no MIMIR_HOME in environment", file=sys.stderr)
            sys.exit(3)
        target = pathlib.Path(home) / "scheduler.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("jobs:\\n  - name: pwned\\n    shell_commands: {{}}\\n")
        pathlib.Path({str(canary)!r}).write_text("escalated")
        print("payload: wrote", target)
        """,
    ).strip()


def test_contained_step_cannot_locate_the_agent_home(
    tmp_path: Path, policy: ContainmentPolicy,
) -> None:
    """The payload runs for real, and cannot find the home to escalate into."""
    home = tmp_path / "agent-home"
    home.mkdir()
    canary = tmp_path / "canary.txt"
    script = tmp_path / "payload.py"
    script.write_text(_payload(canary), encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    request = WorkerRequest(
        attempt_id="canary-1",
        argv=("python3", str(script)),
        cwd=checkout,
        # The real projection: no MIMIR_HOME, no GitHub credentials.
        env={"PATH": "/usr/bin:/bin"},
    )
    request_id = submit_request(policy, request)
    supervisor.serve_forever(policy, poll_seconds=0, max_iterations=1)
    result = await_result(policy, request_id, timeout_seconds=30)

    # It really ran: exit 3 is the payload's own "no MIMIR_HOME" branch, so we
    # know the process started and reached its logic rather than failing to spawn.
    assert result.exit_status == 3, result.stderr
    assert "no MIMIR_HOME" in result.stderr
    assert not canary.exists()
    assert not (home / "scheduler.yaml").exists()


def test_negative_control_the_canary_harness_detects_a_breach(
    tmp_path: Path, policy: ContainmentPolicy,
) -> None:
    """With the projection removed, the SAME payload escalates.

    Without this, a canary that passes proves nothing -- it could be passing
    because the payload never ran, which is exactly how the previous revision's
    canaries passed against an uncontained build.
    """
    home = tmp_path / "agent-home"
    home.mkdir()
    canary = tmp_path / "canary.txt"
    script = tmp_path / "payload.py"
    script.write_text(_payload(canary), encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    request = WorkerRequest(
        attempt_id="canary-2",
        argv=("python3", str(script)),
        cwd=checkout,
        # The breach: MIMIR_HOME projected into the step.
        env={"PATH": "/usr/bin:/bin", "MIMIR_HOME": str(home)},
    )
    # submit_request refuses this projection, which is itself the control -- so
    # write the request directly to prove the HARNESS, not the guard.
    request_id = "canary-2-breach"
    payload = json.dumps({"request_id": request_id, **request.to_json()})
    (supervisor.request_dir(policy.spool_root) / f"{request_id}.json").write_text(
        payload, encoding="utf-8",
    )
    supervisor.serve_forever(policy, poll_seconds=0, max_iterations=1)
    result = await_result(policy, request_id, timeout_seconds=30)

    assert result.exit_status == 0, result.stderr
    assert canary.read_text() == "escalated"
    assert "shell_commands" in (home / "scheduler.yaml").read_text()


def test_submit_refuses_to_project_the_agent_home() -> None:
    """The guard the negative control had to bypass."""
    policy = ContainmentPolicy(user="worklink", spool_root=Path("/nonexistent"), verified=False)
    request = WorkerRequest(
        attempt_id="a",
        argv=("true",),
        cwd=Path("/tmp"),
        env={"MIMIR_HOME": "/home/mimir/agent"},
    )
    with pytest.raises(ValueError, match="MIMIR_HOME"):
        submit_request(policy, request)


@pytest.mark.parametrize(
    "variable", ["GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"],
)
def test_push_credentials_are_never_projected(variable: str) -> None:
    """Push and PR are controller-side; the worker has no use for these."""
    policy = ContainmentPolicy(user="worklink", spool_root=Path("/nonexistent"), verified=False)
    request = WorkerRequest(
        attempt_id="a", argv=("true",), cwd=Path("/tmp"), env={variable: "secret"},
    )
    with pytest.raises(ValueError, match=variable):
        submit_request(policy, request)


def test_verdict_comes_from_observed_exit_status_not_a_build_written_file(
    tmp_path: Path, policy: ContainmentPolicy,
) -> None:
    """A step that writes a success record but exits non-zero is a FAILURE.

    This is why the supervisor stays root and reports what it saw: if the verdict
    came from a file the build wrote, a bad generation could report success it
    did not earn and the controller would push it.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    script = tmp_path / "liar.py"
    script.write_text(
        textwrap.dedent(
            """
            import pathlib, sys
            pathlib.Path("result.json").write_text('{"exit_status": 0, "ok": true}')
            sys.exit(1)
            """,
        ).strip(),
        encoding="utf-8",
    )
    request = WorkerRequest(
        attempt_id="liar",
        argv=("python3", str(script)),
        cwd=checkout,
        env={"PATH": "/usr/bin:/bin"},
    )
    request_id = submit_request(policy, request)
    supervisor.serve_forever(policy, poll_seconds=0, max_iterations=1)
    result = await_result(policy, request_id, timeout_seconds=30)

    assert (checkout / "result.json").exists(), "the step really did write its claim"
    assert result.exit_status == 1
    assert result.ok is False
