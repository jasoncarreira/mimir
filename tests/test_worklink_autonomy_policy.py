"""Worklink compute-backend autonomy policy (chainlink #460).

The gate lives in the core executor: autonomous dispatch refuses an unsandboxed
ComputeBackend (``local_subprocess``) unless the operator opted in, and never
gates the operator CLI. Covered at two levels:
  * ``WorklinkConfig.autonomous_compute_allowed`` — the pure decision; and
  * ``WorklinkRunner.run(autonomous=...)`` — refuses BEFORE claiming (no state
    touched), while non-autonomous / opted-in / isolated runs reach the claim.

The "reaches the claim" assertions use a runner whose ``locks claim`` fails, so a
run that passes the gate attempts the claim (recorded) and returns ``failed``
without needing the full worktree/backend happy path.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from mimir.worklink import BackendRegistry, Caps, ComputeCaps, WorklinkConfig
from mimir.worklink.backends.registry import WorklinkDefaults
from mimir.worklink.orchestrator import WorklinkRunner

ISSUE_JSON = (
    '{"id": 441, "title": "worklink slice",'
    ' "description": "Acceptance criteria:\\n- [ ] do it\\n- [ ] echo ok\\n\\n'
    'Review criteria:\\n- reviewer checks it\\n\\nWorklink notes:\\n- Scope: t\\n'
    '- Out of scope: u\\n- Suggested test command: echo ok",'
    ' "labels": ["worklink"], "parent_id": 380, "comments": []}'
)


def cp(args, *, stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeBackend:
    def __init__(self, name: str = "fake") -> None:
        self.name = name

    def capabilities(self) -> Caps:
        return Caps("coding-cli", False, True, False, True, "test-pool")


class _FakeCompute:
    def __init__(
        self,
        name: str,
        *,
        shared_filesystem: bool = True,
        network_isolated: bool = False,
    ) -> None:
        self.name = name
        self.shared_filesystem = shared_filesystem
        self.network_isolated = network_isolated

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(self.shared_filesystem, self.network_isolated, True, False)


def _registry(
    *,
    compute_name: str = "local_subprocess",
    allow_local: bool = False,
    shared_filesystem: bool = True,
    network_isolated: bool = False,
) -> BackendRegistry:
    cfg = WorklinkConfig(defaults=WorklinkDefaults(
        backend="fake",
        compute_backend=compute_name,
        allow_autonomous_local_subprocess=allow_local,
    ))
    reg = BackendRegistry(cfg)
    reg.register(_FakeBackend("fake"))
    reg.register_compute(_FakeCompute(
        compute_name,
        shared_filesystem=shared_filesystem,
        network_isolated=network_isolated,
    ))
    return reg


def _run(
    tmp_path: Path,
    *,
    autonomous: bool,
    compute_name: str = "local_subprocess",
    allow_local: bool = False,
    shared_filesystem: bool = True,
    network_isolated: bool = False,
):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    # The gate reads the authoritative <home>/worklink.yaml (not the injected
    # registry's config), so write the operator policy there.
    (tmp_path / "worklink.yaml").write_text(
        "defaults:\n"
        f"  compute_backend: {compute_name}\n"
        f"  allow_autonomous_local_subprocess: {'true' if allow_local else 'false'}\n",
        encoding="utf-8",
    )
    calls: list = []

    def runner(args: Sequence[str] | str, **_: object) -> subprocess.CompletedProcess:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "claim"]:
            return cp(args, returncode=1)  # fail claim → a run past the gate stops here
        return cp(args)

    registry = _registry(
        compute_name=compute_name,
        allow_local=allow_local,
        shared_filesystem=shared_filesystem,
        network_isolated=network_isolated,
    )
    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, autonomous=autonomous,
        )
    )
    claimed = any(isinstance(a, list) and a[:3] == ["chainlink", "locks", "claim"] for a in calls)
    return result, claimed


# ── pure policy decision ────────────────────────────────────────────


def test_policy_refuses_local_subprocess_by_default() -> None:
    cfg = WorklinkConfig(defaults=WorklinkDefaults(allow_autonomous_local_subprocess=False))
    allowed, reason = cfg.autonomous_compute_allowed("local_subprocess")
    assert allowed is False
    assert reason and "local_subprocess" in reason


def test_policy_allows_local_subprocess_when_opted_in() -> None:
    cfg = WorklinkConfig(defaults=WorklinkDefaults(allow_autonomous_local_subprocess=True))
    assert cfg.autonomous_compute_allowed("local_subprocess") == (True, None)


def test_policy_allows_isolated_capability_substrate() -> None:
    """A capability-based check: a hypothetical substrate that declares
    itself isolated (shared_filesystem=False, network_isolated=True) is
    allowed by autonomous dispatch without operator opt-in. The name check
    alone is not authoritative — capabilities are. After the #832 substrate
    cleanup there is no built-in isolated compute; this test uses a fake name
    to validate the capability check still works."""
    cfg = WorklinkConfig(defaults=WorklinkDefaults(allow_autonomous_local_subprocess=False))
    isolated = ComputeCaps(
        shared_filesystem=False,
        network_isolated=True,
        handle_cancel=True,
        persistent_after_disconnect=False,
    )
    assert cfg.autonomous_compute_allowed("hypothetical_isolated", isolated) == (True, None)


def test_policy_refuses_differently_named_unsandboxed_backend() -> None:
    cfg = WorklinkConfig(defaults=WorklinkDefaults(allow_autonomous_local_subprocess=False))
    caps = ComputeCaps(
        shared_filesystem=True,
        network_isolated=False,
        handle_cancel=True,
        persistent_after_disconnect=False,
    )
    allowed, reason = cfg.autonomous_compute_allowed("custom_alias", caps)
    assert allowed is False
    assert reason and "shared filesystem" in reason


def test_policy_refusal_text_no_longer_recommends_retired_substrates() -> None:
    """chainlink #832: the refusal text used to recommend docker_sibling /
    ecs_runtask as the isolated alternative. With those substrates retired
    the only escape is the opt-in knob; the message must reflect that."""
    cfg = WorklinkConfig(defaults=WorklinkDefaults(allow_autonomous_local_subprocess=False))
    _, reason = cfg.autonomous_compute_allowed("local_subprocess")
    assert reason is not None
    assert "docker_sibling" not in reason
    assert "ecs_runtask" not in reason
    assert "allow_autonomous_local_subprocess" in reason


# ── orchestrator gate ───────────────────────────────────────────────


def test_autonomous_local_subprocess_refused_before_claim(tmp_path: Path) -> None:
    result, claimed = _run(tmp_path, autonomous=True, compute_name="local_subprocess")
    assert result.status == "refused"
    assert not claimed  # gate fires before any claim → no state touched
    assert "local_subprocess" in (result.reason or "")


def test_operator_run_not_gated_even_on_local_subprocess(tmp_path: Path) -> None:
    # autonomous=False (the operator CLI): always proceeds past the gate to claim.
    result, claimed = _run(tmp_path, autonomous=False, compute_name="local_subprocess")
    assert claimed  # reached the claim (which we made fail) → gate did not block
    assert result.status != "refused"


def test_autonomous_local_subprocess_allowed_when_opted_in(tmp_path: Path) -> None:
    result, claimed = _run(tmp_path, autonomous=True, compute_name="local_subprocess", allow_local=True)
    assert claimed
    assert result.status != "refused"


def test_autonomous_isolated_capability_substrate_allowed(tmp_path: Path) -> None:
    """Capability-based gate: a compute substrate whose capabilities declare
    isolation (shared_filesystem=False, network_isolated=True) is allowed for
    autonomous dispatch without the operator opt-in. After the #832 cleanup
    there is no built-in isolated compute; this uses a fake name to validate
    the gate. The legacy "docker_sibling" / "ecs_runtask" names are no longer
    preferred since both substrates were retired."""
    result, claimed = _run(
        tmp_path,
        autonomous=True,
        compute_name="hypothetical_isolated",
        shared_filesystem=False,
        network_isolated=True,
    )
    assert claimed
    assert result.status != "refused"


# ── fail-closed parsing of the safety knob ──────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("true", True),       # real YAML bool
        ("false", False),
        ('"true"', True),     # quoted string alias
        ('"false"', False),   # the bug: bool("false") was True → must be False
        ('"1"', True),
        ('"0"', False),
        ('"off"', False),
        ("maybe", False),     # arbitrary/invalid string → fail closed (OFF)
        ('"enabled"', False),
        ("2", False),
        ("-1", False),
    ],
)
def test_allow_autonomous_local_subprocess_parses_fail_closed(
    tmp_path: Path, raw: str, expected: bool
) -> None:
    (tmp_path / "worklink.yaml").write_text(
        f"defaults:\n  allow_autonomous_local_subprocess: {raw}\n", encoding="utf-8"
    )
    cfg = WorklinkConfig.load(tmp_path / "worklink.yaml")
    assert cfg.defaults.allow_autonomous_local_subprocess is expected


def test_allow_autonomous_local_subprocess_defaults_off(tmp_path: Path) -> None:
    (tmp_path / "worklink.yaml").write_text("defaults:\n  priority: normal\n", encoding="utf-8")
    cfg = WorklinkConfig.load(tmp_path / "worklink.yaml")
    assert cfg.defaults.allow_autonomous_local_subprocess is False
