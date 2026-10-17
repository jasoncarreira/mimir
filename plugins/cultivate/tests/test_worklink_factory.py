"""Provider-owned factory completion and evidence boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from mimir_cultivate.worklink import autonomy, orchestrator, worker_client
from mimir_cultivate.worklink.backends.feature_factory import FeatureFactoryBackend, parse_factory_status
from mimir_cultivate.worklink.backends.registry import BackendRegistry, WorklinkConfig, WorklinkDefaults
from mimir_cultivate.worklink.checkout import CheckoutLease
from mimir_cultivate.worklink.claims import ClaimRecord, ClaimResult
from mimir_cultivate.worklink.compute import LaunchHandle
from mimir_cultivate.worklink.factory_state import FactoryRunRecord
from mimir_cultivate.worklink.run_state import WorklinkRunState, load_run_state, save_run_state


def test_provider_renders_default_work_order_without_override(tmp_path: Path) -> None:
    issue = orchestrator.IssueContext(1949, "Move coding", "Implement the factory", {"worklink:ready"})
    rendered = orchestrator.render_work_order(
        issue,
        template_path=orchestrator._template_path(tmp_path),
        backend_name="feature_factory",
        test_command="uv run pytest -q",
    )
    assert "Chainlink issue #1949" in rendered
    assert "Implement the factory" in rendered
    assert "uv run pytest -q" in rendered


def test_provider_runner_dry_run_uses_default_work_order(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    issue = json.dumps({
        "id": 1949, "title": "Move coding",
        "description": "Acceptance criteria:\n- [ ] Move the factory\n- [ ] Test it\n\n"
        "Review criteria:\n- Check the gate\n\nWorklink notes:\n- Scope: factory\n"
        "- Out of scope: UI\n- Suggested test command: pytest -q",
        "labels": ["worklink:ready"], "comments": [],
    })

    def runner(args: list[str] | str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "1949"]:
            return subprocess.CompletedProcess(args, 0, issue, "")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return subprocess.CompletedProcess(args, 0, "git@github.com:owner/repository.git\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    result = asyncio.run(orchestrator.WorklinkRunner(home=tmp_path, repo=repo, runner=runner).run(
        1949, backend_name="feature_factory", dry_run=True,
    ))
    assert result.dry_run
    output = capsys.readouterr().out
    assert "Chainlink issue #1949" in output
    assert "Move the factory" in output
    assert "WORKLINK_BLOCKED:" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_case", ["valid", "deeper", "controller"])
async def test_provider_completion_uses_emitted_record_and_worker_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_case: str,
) -> None:
    root = tmp_path / "worklink"
    checkout = root / "repository" / "1949-1" / "checkout"
    checkout.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".factory.json").write_text(json.dumps({
        "publishing_identity": "factory-owner", "bootstrap": "uv sync",
    }))
    gate_command = "uv run python -c 'print(1949)'"
    (tmp_path / "worklink.yaml").write_text(f"defaults:\n  test_command: \"{gate_command}\"\n")
    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", root)
    monkeypatch.setattr(orchestrator, "get_identities", lambda: SimpleNamespace(
        mimir_uid=os.getuid(), worklink_uid=os.getuid(), worklink_gid=os.getgid(),
    ))
    monkeypatch.setattr(orchestrator, "_read_checkout_git_identity", lambda *a: ("Factory", "factory@example.org"))
    monkeypatch.setattr(orchestrator, "_read_factory_publishing_identity", lambda *a: ("factory-owner", ".factory.json"))
    monkeypatch.setattr(orchestrator, "_resolve_factory_github_credential", lambda *a: ("token", {"GH_TOKEN": "token"}))
    monkeypatch.setattr(orchestrator, "GitHubForgeClient", lambda **kw: SimpleNamespace(verify_identity=lambda login: login))
    monkeypatch.setattr(FeatureFactoryBackend, "admit", lambda self: Path(self.entrypoint))
    monkeypatch.setattr(orchestrator, "_create_backend_checkout", lambda *a, **kw: CheckoutLease(
        issue_id=1949, attempt=1, repo=repo, path=checkout, branch="main",
        base_ref="main", isolated_checkout=True,
    ))
    claim = ClaimRecord(1949, 1, "agent", datetime.now(UTC))
    monkeypatch.setattr(orchestrator.ChainlinkClaims, "claim_issue", lambda self, *a, **kw: ClaimResult(True, claim))
    monkeypatch.setattr(orchestrator.ChainlinkClaims, "transition_issue", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator.ChainlinkClaims, "release_issue", lambda *a, **kw: True)
    monkeypatch.setattr(orchestrator.LocalSubprocessComputeBackend, "launch", lambda self, spec: asyncio.sleep(
        0, result=LaunchHandle("local_subprocess", "handle", 123),
    ))

    issue = json.dumps({
        "id": 1949, "title": "Factory", "description": "Build factory", "labels": ["worklink:epic"],
        "comments": [],
    })
    observed: list[Path] = []
    gates: list[Path] = []
    gate_commands: list[tuple[str, ...]] = []
    branches: list[str] = []

    def runner(args: list[str] | str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "1949"]:
            return subprocess.CompletedProcess(args, 0, issue, "")
        if isinstance(args, list) and args[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "html_url": "https://github.com/owner/repository/pull/42", "state": "open", "draft": False,
                "base": {"repo": {"full_name": "owner/repository"}, "ref": "main"},
                "head": {"repo": {"full_name": "owner/repository"}, "ref": branches[0], "sha": "a" * 40},
            }), "")
        if isinstance(args, list) and args[:3] == ["git", "-C", str(repo)]:
            return subprocess.CompletedProcess(args, 0, "git@github.com:owner/repository.git\n", "")
        if isinstance(args, list) and args[:3] == ["git", "-C", str(checkout)]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if isinstance(args, list) and args[:2] == ["git", "-C"]:
            sandbox = Path(args[2])
            observed.append(sandbox)
            operation = args[3:]
            if operation == ["rev-parse", "--show-toplevel"]:
                value = str(sandbox)
            elif operation == ["rev-parse", "--absolute-git-dir"]:
                value = str(sandbox / ".git")
            elif operation == ["branch", "--show-current"]:
                value = branches[0]
            elif operation == ["config", "--get", "remote.origin.url"]:
                value = "git@github.com:owner/repository.git"
            elif operation == ["rev-parse", "HEAD"]:
                value = "a" * 40
            elif operation[:2] == ["rev-parse", "--verify"]:
                value = "b" * 40
            elif operation[:2] == ["diff", "--name-only"]:
                value = "changed.py"
            elif operation[:2] == ["diff", "--stat"]:
                value = " changed.py | 1 +"
            else:
                value = ""
            return subprocess.CompletedProcess(args, 0, value + "\n" if value else "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def worker(path: Path, argv: tuple[str, ...], **kw: object) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "git":
            result = runner(list(argv))
            return subprocess.CompletedProcess(argv, result.returncode, result.stdout.encode(), result.stderr.encode())
        gates.append(path)
        gate_commands.append(argv)
        return subprocess.run(argv, capture_output=True, timeout=30)

    monkeypatch.setattr(worker_client, "run_factory_control", worker)

    async def finish(self: orchestrator.WorklinkRunner, **kwargs: object) -> orchestrator.WorklinkRunResult:
        record = kwargs["factory_record"]
        assert isinstance(record, FactoryRunRecord)
        branches.append(record.branch)
        sandbox = Path(record.sandbox)
        assert sandbox == checkout / ".factory-sandboxes" / record.run_id
        sandbox.mkdir()
        (sandbox / ".git").mkdir()
        if gate_case == "deeper":
            record = replace(record, sandbox=str(sandbox / ".factory" / "worktrees" / "leaf"))
        if gate_case == "controller":
            monkeypatch.setattr(orchestrator, "LocalSubprocessComputeBackend", lambda: None)
        status = parse_factory_status({
            "run_id": record.run_id, "issue_key": str(record.issue_id), "valid": True,
            "sandbox_path": record.sandbox, "status": "completed", "mode": "autonomous",
            "branch": record.branch, "pr_base": record.base_ref, "pr_draft": False,
            "lock": "absent", "dead_lock": False, "lock_session": None,
            "slices": [{"id": "implementation", "status": "merged", "attempts": 1,
                         "extra_attempts": 0, "retry_limit": 5}],
            "pr_url": "https://github.com/owner/repository/pull/42",
        })
        return await self._finish_factory_070(**{
            **{key: value for key, value in kwargs.items() if key != "autonomous"},
            "factory_record": replace(record, status=status),
        })

    monkeypatch.setattr(orchestrator.WorklinkRunner, "_supervise_factory_070", finish)
    result = await orchestrator.WorklinkRunner(home=tmp_path, repo=repo, runner=runner).run_epic(1949)
    if gate_case == "valid":
        assert result.status == "review_ready", (result.reason, observed, result.checkout)
        assert result.checkout == checkout / ".factory-sandboxes" / "chainlink-1949"
        assert observed and set(observed) == {result.checkout}
        assert gates == [result.checkout]
        assert gate_commands[0] == (
            "/bin/sh", "-c", f"cd {result.checkout} && {gate_command}",
        )
        payload = json.loads(result.evidence_path.read_text())
        assert payload["checkout"] == str(result.checkout)
        assert payload["tests"]["exit_code"] == 0
        assert "1949" in payload["tests"]["summary"]
    else:
        assert result.status == "failed"
        assert ("sandbox does not match run id" if gate_case == "deeper" else "worker sandbox") in (result.reason or "")
        assert not gates


@pytest.mark.asyncio
@pytest.mark.parametrize("alive", [False, True])
async def test_provider_reattach_cleans_only_verified_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alive: bool,
) -> None:
    checkout = tmp_path / "retained"
    checkout.mkdir()
    (checkout / "output.txt").write_text("keep")
    state = WorklinkRunState(
        issue_id=1949, attempt=1, backend="opencode", compute_name="local_subprocess",
        handle_substrate="local_subprocess", handle_identifier="123e4567-e89b-42d3-a456-426614174000",
        branch="issue/1949-a1", base_ref="main", local_base="abc123",
        repo=str(tmp_path), repo_url="git@github.com:owner/repository.git",
        test_command="uv run pytest -q", started_at=datetime.now(UTC).isoformat(),
        checkout=str(checkout), process_start_ticks=10, shim_pid=4321,
    )
    save_run_state(tmp_path, state)
    cancellations: list[LaunchHandle] = []
    transitions: list[str] = []

    async def cancel(self: object, handle: LaunchHandle) -> None:
        cancellations.append(handle)

    monkeypatch.setattr(orchestrator, "process_is_alive", lambda current: alive)
    monkeypatch.setattr(orchestrator.LocalSubprocessComputeBackend, "cancel", cancel)
    monkeypatch.setattr(orchestrator.ChainlinkClaims, "transition_issue", lambda self, *a, **kw: transitions.append(kw["status"]))
    monkeypatch.setattr(orchestrator.ChainlinkClaims, "release_issue", lambda *a, **kw: True)
    result = await orchestrator.WorklinkRunner(home=tmp_path, repo=tmp_path).reattach(1949)
    assert result.status == "failed"
    assert cancellations == ([LaunchHandle("local_subprocess", state.handle_identifier, 10, 4321)] if alive else [])
    assert transitions == ["failed"]
    assert (checkout / "output.txt").read_text() == "keep"
    assert load_run_state(tmp_path, 1949) is None


def test_provider_registry_refuses_unknown_backend_and_selects_factory() -> None:
    registry = BackendRegistry(WorklinkConfig(defaults=WorklinkDefaults(backend="feature_factory")))
    assert registry.get("feature_factory").name == "feature_factory"
    assert registry.select(labels={"worklink:epic"}, repo="owner/repository").name == "feature_factory"
    with pytest.raises((KeyError, ValueError)):
        registry.get("unregistered")


@pytest.mark.parametrize("active,allowed", [(1, True), (2, False)])
def test_provider_autonomy_respects_claim_cap(tmp_path: Path, active: int, allowed: bool) -> None:
    (tmp_path / "worklink.yaml").write_text("defaults:\n  max_concurrent: 2\n")

    class Claims:
        def active_worklink_lock_count(self, *, exclude_label: str) -> int:
            assert exclude_label == "worklink:epic"
            return active

    check = autonomy.check_concurrency(tmp_path, claims=Claims())
    assert (check.active, check.cap, check.allowed) == (active, 2, allowed)
