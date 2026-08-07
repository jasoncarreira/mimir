"""Worklink build steps must run as an identity that cannot write the agent home.

The property under test is not "a user exists" but "repository-controlled code
cannot reach the agent home". These tests pin the parts of that which are
decidable without a live broker; the end-to-end canary tests belong with the
call-site integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.worklink.containment import (
    ContainmentPolicy,
    ContainmentUnavailable,
    contained_argv,
    resolve_containment,
)


def test_missing_broker_raises_rather_than_falling_back(tmp_path, monkeypatch):
    """A missing broker is an error, never a licence to run uncontained.

    Returning an "uncontained" policy here would let a caller treat it as a
    fallback. Raising forces the decision to be explicit.
    """
    launcher = tmp_path / "s6-setuidgid"
    launcher.touch()
    monkeypatch.setattr("shutil.which", lambda _name: str(launcher))
    monkeypatch.setenv("MIMIR_WORKLINK_BROKER_SOCKET", str(tmp_path / "absent.sock"))
    with pytest.raises(ContainmentUnavailable, match="broker socket"):
        resolve_containment()


def test_missing_launcher_raises(tmp_path, monkeypatch):
    """Without a privilege-dropping launcher the agent cannot contain anything.

    The agent runs at CapEff=0, so it cannot switch uid itself; saying so in the
    error is the difference between a fixable report and a mystery.
    """
    sock = tmp_path / "broker.sock"
    sock.touch()
    monkeypatch.setenv("MIMIR_WORKLINK_BROKER_SOCKET", str(sock))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(Path, "exists", lambda self: self == sock)
    with pytest.raises(ContainmentUnavailable, match="launcher"):
        resolve_containment()


def test_verified_policy_wraps_the_command(tmp_path, monkeypatch):
    sock = tmp_path / "broker.sock"
    sock.touch()
    launcher = tmp_path / "s6-setuidgid"
    launcher.touch()
    monkeypatch.setenv("MIMIR_WORKLINK_BROKER_SOCKET", str(sock))
    monkeypatch.setattr("shutil.which", lambda _name: str(launcher))

    policy = resolve_containment()
    assert policy.verified is True
    assert policy.override_reason is None
    assert contained_argv(policy, ["pytest", "-q"])[:2] == (str(launcher), "worklink")
    assert contained_argv(policy, ["pytest", "-q"])[-2:] == ("pytest", "-q")


def test_override_is_recorded_as_a_bypass_not_a_verification(tmp_path, monkeypatch):
    """`warned and continued` must never be indistinguishable from `verified`.

    A single boolean would collapse the two. The policy carries both, and the
    override records its reason so a log reader can tell which happened.
    """
    monkeypatch.setenv("MIMIR_WORKLINK_BROKER_SOCKET", str(tmp_path / "absent.sock"))
    policy = resolve_containment(allow_uncontained="operator: local development")

    assert policy.verified is False
    assert policy.override_reason == "operator: local development"
    # and the command is NOT wrapped, at exactly one decision point
    assert contained_argv(policy, ["pytest", "-q"]) == ("pytest", "-q")


def test_contained_argv_refuses_an_empty_command():
    policy = ContainmentPolicy(
        user="worklink", broker_socket=Path("/x"), launcher=("s6", "worklink"),
        verified=True,
    )
    with pytest.raises(ValueError, match="non-empty"):
        contained_argv(policy, [])


def test_the_contained_user_is_not_the_agent_user(tmp_path, monkeypatch):
    """Containment means a DIFFERENT identity; the default must not be the agent.

    A deployment that sets the contained user to the agent user has containment
    in name only, and this is the cheapest place to notice.
    """
    sock = tmp_path / "broker.sock"; sock.touch()
    launcher = tmp_path / "s6-setuidgid"; launcher.touch()
    monkeypatch.setenv("MIMIR_WORKLINK_BROKER_SOCKET", str(sock))
    monkeypatch.setattr("shutil.which", lambda _name: str(launcher))

    policy = resolve_containment()
    import getpass
    assert policy.user != getpass.getuser(), (
        "the contained identity must differ from the agent's own user"
    )
