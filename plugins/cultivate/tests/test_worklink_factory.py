"""Provider-owned factory completion and evidence boundaries."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from mimir_cultivate.worklink import autonomy, evidence, factory_store, orchestrator, worker_client, worker_exec
from mimir_cultivate.worklink.backends.base import ComputeLaunchError
from mimir_cultivate.worklink.backends.feature_factory import FeatureFactoryBackend, parse_factory_status
from mimir_cultivate.worklink.backends.registry import BackendRegistry, WorklinkConfig, WorklinkDefaults
from mimir_cultivate.worklink.checkout import CheckoutLease
from mimir_cultivate.worklink.claims import ClaimRecord, ClaimResult
from mimir_cultivate.worklink.compute import ComputeResult, LaunchHandle, LocalSubprocessComputeBackend, WorkSpec
from mimir_cultivate.worklink.factory_state import load_factory_record
from mimir_cultivate.worklink.run_state import WorklinkRunState, load_run_state, save_run_state
from tests.opencode_fixtures import seed_codex_subscription


@contextmanager
def _executor_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: Path, socket_dir: Path,
):
    """Run the provider executor protocol without deployment-only root credentials."""
    identities = SimpleNamespace(
        mimir_uid=os.getuid(), worklink_uid=os.getuid(), worklink_gid=os.getgid(),
    )
    socket_path = socket_dir / "executor.sock"
    homes = tmp_path / "homes"
    homes.mkdir()
    monkeypatch.setattr(worker_client.identities, "get_identities", lambda: identities)
    monkeypatch.setattr(worker_exec, "get_identities", lambda: identities)
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(root))
    monkeypatch.setattr(worker_exec, "HOME_ROOT", homes)
    monkeypatch.setattr(worker_client, "DEFAULT_EXECUTOR_SOCKET", socket_path)

    # The test has no root-owned socket or uid-changing privileges. Keep the
    # real request, FD transfer, checkout validation, process launch and reply.
    def connect(self: worker_client.WorkerClient, timeout_s: float | None = None) -> socket.socket:
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        peer.connect(str(self.socket_path))
        return peer

    def drop(checkout_fd: int) -> None:
        os.fchdir(checkout_fd)

    monkeypatch.setattr(worker_client.WorkerClient, "_connect", connect)
    monkeypatch.setattr(worker_exec, "_drop_worker", drop)
    errors: list[BaseException] = []
    requests: list[dict[str, object]] = []
    finished = threading.Event()

    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        listener.bind(str(socket_path))
        listener.listen(8)
        listener.settimeout(0.1)

        def serve() -> None:
            while not finished.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    if not finished.is_set():
                        errors.append(exc)
                    break
                try:
                    requests.append(json.loads(connection.recv(65536, socket.MSG_PEEK)))
                    worker_exec.handle_connection(connection)
                except BaseException as exc:
                    errors.append(exc)
                    connection.close()

        thread = threading.Thread(target=serve, name="factory-test-executor")
        thread.start()
        try:
            yield requests
        finally:
            finished.set()
            thread.join(timeout=10)
            assert not thread.is_alive()
            assert not errors


def test_provider_renders_default_work_order_without_override(tmp_path: Path) -> None:
    issue = orchestrator.IssueContext(1949, "Move coding", "Implement the factory", {"worklink:ready"})
    rendered = orchestrator.render_work_order(
        issue,
        template_path=orchestrator._template_path(tmp_path),
        backend_name="feature_factory",
        test_command="uv run pytest -q",
    )
    assert rendered.startswith("You are executing this Worklink issue.\n")
    assert "Title: Move coding" in rendered
    assert "#1949" not in rendered and "chainlink 1949" not in rendered.lower()
    assert "Parent:" not in rendered
    assert "Implement the factory" in rendered
    assert "uv run pytest -q" in rendered
    _assert_publication_rule(rendered)


def _assert_publication_rule(order: str) -> None:
    rule = next((line for line in order.splitlines() if line.startswith("- Worklink commits,")), "")
    for phrase in (
        "pushes the branch and opens the pull request after its gate passes",
        "builder never pushes, calls `gh` or opens a PR itself",
        "opening, titling or describing the PR",
        "`.worklink-pr-body.md` / `.worklink-commit-subject`",
        "PR publication criteria are never a reason for `WORKLINK_BLOCKED`",
    ):
        assert phrase in rule


def test_default_work_order_publication_rule_mutation_is_detected(tmp_path: Path) -> None:
    issue = orchestrator.IssueContext(1949, "Move coding", "Open a PR for review", {"worklink:ready"})
    template = orchestrator._template_path(tmp_path)
    original = template.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    removed = [line for line in lines if line.startswith("- Worklink commits,")]
    assert len(removed) == 1
    mutated = tmp_path / "mutated-order.md"
    mutated.write_text("".join(line for line in lines if line not in removed), encoding="utf-8")
    rendered = orchestrator.render_work_order(
        issue, template_path=mutated, backend_name="opencode", test_command="uv run pytest -q",
    )
    with pytest.raises(AssertionError):
        _assert_publication_rule(rendered)


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
    assert "You are executing this Worklink issue." in output
    assert "Move the factory" in output
    assert "WORKLINK_BLOCKED:" in output


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="factory executor requires Linux")
@pytest.mark.parametrize("reaped", [False, True])
async def test_provider_completion_uses_emitted_record_and_worker_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, short_socket_dir: Path, reaped: bool,
) -> None:
    root = tmp_path / "worklink"
    checkout = root / "repository" / "1949-1" / "checkout"
    checkout.mkdir(parents=True)
    root.chmod(0o755)
    checkout.parent.parent.chmod(0o755)
    checkout.parent.chmod(0o2750)
    checkout.chmod(0o2770)
    db_path = checkout.parent / ".factory-runtime" / "data" / "opencode" / "opencode.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE event (type TEXT)")
        db.execute("INSERT INTO event VALUES ('message.part.updated')")
    # The controller must not open the worker-owned store itself. The executor
    # invokes the same script under the checkout owner, outside this process.
    monkeypatch.setattr(factory_store, "_prune_db", lambda path: pytest.fail("controller opened worker store"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".factory.json").write_text(json.dumps({
        "publishing_identity": "factory-owner", "bootstrap": "uv sync",
    }))
    gate_command = "uv run python -c 'print(1949)'"
    # Pin the opencode model/auth inputs work_spec resolves, as the core factory
    # tests do; otherwise the launch reads the host's ~/.local/share/opencode and
    # fails with feature_factory_opencode_resolution_failed:auth_missing anywhere
    # that file is absent (CI, containers).
    operator_home = tmp_path / "operator-home"
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:gpt-5.6-luna")
    auth = operator_home / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"openai": {"type": "oauth", "refresh": "subscription"}}))
    seed_codex_subscription(operator_home)
    (tmp_path / "worklink.yaml").write_text(f"defaults:\n  test_command: \"{gate_command}\"\n")
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(root))
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
    dead = subprocess.Popen(["true"])
    dead.wait()
    monkeypatch.setattr(orchestrator.LocalSubprocessComputeBackend, "launch", lambda self, spec: asyncio.sleep(
        0, result=LaunchHandle("local_subprocess", str(dead.pid), 1, dead.pid),
    ))
    cancelled = asyncio.Event()
    waits: list[LaunchHandle] = []
    cancels: list[LaunchHandle] = []
    cleanups: list[LaunchHandle] = []

    supervision_order = []
    real_prune = factory_store.prune_factory_store

    def checked_prune(record):
        # Exercise the production observe -> cancel -> drain -> persist -> prune
        # wiring, not just a record with an acknowledgement injected in isolation.
        assert supervision_order == ["cancel", "drained"]
        assert record.children_reaped is reaped
        persisted = load_factory_record(tmp_path, record.run_id)
        assert persisted is not None and persisted.children_reaped is reaped
        supervision_order.append("prune")
        return real_prune(record)

    monkeypatch.setattr(factory_store, "prune_factory_store", checked_prune)

    async def wait(self: object, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        waits.append(handle)
        await cancelled.wait()
        supervision_order.append("drained")
        return ComputeResult(0, "", "", handle=handle, children_reaped=reaped)

    async def cancel(self: object, handle: LaunchHandle) -> None:
        cancels.append(handle)
        supervision_order.append("cancel")
        cancelled.set()

    async def cleanup(self: object, handle: LaunchHandle) -> None:
        cleanups.append(handle)

    monkeypatch.setattr(orchestrator.LocalSubprocessComputeBackend, "wait", wait)
    monkeypatch.setattr(orchestrator.LocalSubprocessComputeBackend, "cancel", cancel)
    monkeypatch.setattr(orchestrator.LocalSubprocessComputeBackend, "cleanup", cleanup)
    monkeypatch.setattr(orchestrator.LocalSubprocessComputeBackend, "job_alive", lambda self, handle: not cancelled.is_set())

    issue = json.dumps({
        "id": 1949, "title": "Factory", "description": "Build factory", "labels": ["worklink:epic"],
        "comments": [],
    })
    branches: list[str] = []
    heads: list[str] = []

    def runner(args: list[str] | str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "1949"]:
            return subprocess.CompletedProcess(args, 0, issue, "")
        if isinstance(args, list) and args[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "html_url": "https://github.com/owner/repository/pull/42", "state": "open", "draft": False,
                "base": {"repo": {"full_name": "owner/repository"}, "ref": "main"},
                "head": {"repo": {"full_name": "owner/repository"}, "ref": branches[0], "sha": heads[0]},
            }), "")
        if isinstance(args, list) and args[:3] == ["git", "-C", str(repo)]:
            return subprocess.CompletedProcess(args, 0, "git@github.com:owner/repository.git\n", "")
        if isinstance(args, list) and args[:3] == ["git", "-C", str(checkout)]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if isinstance(args, list) and args[:2] == ["git", "-C"]:
            return subprocess.run(args, capture_output=True, text=True)
        return subprocess.CompletedProcess(args, 0, "", "")

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(sandbox), *args], capture_output=True, text=True, check=True).stdout.strip()

    def status(self: FeatureFactoryBackend, run_id: str, *, sandbox: Path, launcher: str):
        assert sandbox == checkout / ".factory-sandboxes" / run_id
        if not branches:
            branches.append(f"feature/{run_id}")
            sandbox.mkdir()
            git("init", "-q", "-b", "main")
            git("config", "user.email", "factory@example.org")
            git("config", "user.name", "Factory")
            git("remote", "add", "origin", "git@github.com:owner/repository.git")
            (sandbox / "base.py").write_text("base\n")
            git("add", "base.py")
            git("commit", "-qm", "base")
            git("checkout", "-qb", branches[0])
            (sandbox / "changed.py").write_text("changed\n")
            git("add", "changed.py")
            git("commit", "-qm", "change")
            heads.append(git("rev-parse", "HEAD"))
        return parse_factory_status({
            "run_id": run_id, "issue_key": "1949", "valid": True,
            "sandbox_path": str(sandbox), "status": "completed", "mode": "autonomous",
            "branch": branches[0], "pr_base": "main", "pr_draft": False,
            "lock": "absent", "dead_lock": False, "lock_session": None,
            "slices": [{"id": "implementation", "status": "merged", "attempts": 1,
                         "extra_attempts": 0, "retry_limit": 5}],
            "pr_url": "https://github.com/owner/repository/pull/42",
        })

    monkeypatch.setattr(FeatureFactoryBackend, "status", status)
    sandbox = checkout / ".factory-sandboxes" / "chainlink-1949"
    with _executor_in_process(tmp_path, monkeypatch, root, short_socket_dir) as requests:
        result = await orchestrator.WorklinkRunner(home=tmp_path, repo=repo, runner=runner).run_epic(1949)
        record = load_factory_record(tmp_path, "chainlink-1949")
        assert record is not None and record.sandbox == str(sandbox)
        spec = WorkSpec(
            issue_id=record.issue_id, attempt=record.attempt, repo_url="", base_ref=record.base_ref,
            branch=record.branch, prompt="", rules=None, test_command=gate_command,
            backend="feature_factory", timeout_s=30, local_checkout=sandbox,
        )
        for invalid_checkout, compute in (
            (sandbox / ".factory" / "worktrees" / "leaf", LocalSubprocessComputeBackend()),
            (sandbox, None),
        ):
            before = len(requests)
            with pytest.raises(ComputeLaunchError, match="recorded worker sandbox"):
                await evidence.observe_evidence(
                    issue=record.issue_id, attempt=record.attempt, backend="feature_factory",
                    branch=record.branch, checkout=invalid_checkout, started_at=datetime.now(UTC),
                    base_ref=record.base_ref, backend_status="completed", test_command=gate_command,
                    work_spec=spec, compute=compute, factory_run_id=record.run_id,
                )
            assert len(requests) == before
    assert len(waits) == len(cancels) == len(cleanups) == 1
    assert result.status == "review_ready", result.reason
    assert result.checkout == sandbox
    assert requests and all(request["op"] == "launch_factory_control" for request in requests)
    assert all(request["path"] == str(checkout) for request in requests)
    assert any(request["argv"] == [
        "/bin/sh", "-c", f"cd {result.checkout} && {gate_command}",
    ] for request in requests)
    payload = json.loads(result.evidence_path.read_text())
    assert payload["checkout"] == str(result.checkout)
    assert payload["tests"]["exit_code"] == 0
    assert "1949" in payload["tests"]["summary"]
    assert supervision_order == ["cancel", "drained", "prune"]
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM event").fetchone() == (0 if reaped else 1,)


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
