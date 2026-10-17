"""Provider-owned factory completion and evidence boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import subprocess

import pytest

from mimir_cultivate.worklink import evidence, orchestrator, worker_client
from mimir_cultivate.worklink.checkout import CheckoutLease
from mimir_cultivate.worklink.backends.base import ComputeLaunchError
from mimir_cultivate.worklink.compute import LocalSubprocessComputeBackend, WorkSpec
from mimir_cultivate.worklink.factory_state import FactoryRunRecord


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


def _factory_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FactoryRunRecord:
    root = tmp_path / "worklink"
    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", root)
    checkout = root / "repository" / "1949-2" / "checkout"
    checkout.mkdir(parents=True)
    record = FactoryRunRecord(
        run_id="chainlink-1949", issue_id=1949, attempt=2,
        repository="owner/repository", base_ref="main", branch="feature/chainlink-1949",
        launcher="/opt/factory.js",
        sandbox=str(checkout / ".factory-sandboxes" / "chainlink-1949"),
        session=None, handle=None, status=None, observed_at=None,
        controller_phase="running",
    )
    lease = CheckoutLease(
        issue_id=1949, attempt=2, repo=tmp_path, path=checkout,
        branch="main", base_ref="main", isolated_checkout=True,
    )
    assert orchestrator._create_factory_sandbox(record, lease) == Path(record.sandbox)
    return record


def _gate_spec(record: FactoryRunRecord) -> WorkSpec:
    return WorkSpec(
        issue_id=record.issue_id, attempt=record.attempt,
        repo_url="", base_ref=record.base_ref, branch=record.branch,
        prompt="", rules=None, test_command="uv run pytest -q",
        backend="feature_factory", timeout_s=30,
        local_checkout=Path(record.sandbox),
    )


@pytest.mark.asyncio
async def test_factory_gate_uses_the_orchestrator_recorded_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _factory_record(tmp_path, monkeypatch)
    seen: list[Path] = []

    async def observed(**kwargs: object) -> str:
        seen.append(kwargs["checkout"])
        return "observed"

    monkeypatch.setattr(evidence, "_observe_evidence_from_ref", observed)
    params = dict(
        issue=record.issue_id, attempt=record.attempt, backend="feature_factory",
        branch=record.branch, checkout=Path(record.sandbox),
        started_at=datetime.now(UTC), base_ref=record.base_ref,
        backend_status="completed", test_command="uv run pytest -q",
        factory_run_id=record.run_id, compute=LocalSubprocessComputeBackend(),
        work_spec=_gate_spec(record),
    )
    # Observe through the real entry point: it forwards the *recorded* sandbox,
    # not a guessed .factory/worktrees child of that sandbox.
    assert await evidence.observe_evidence(**params) == "observed"
    assert seen == [Path(record.sandbox)]


@pytest.mark.asyncio
async def test_factory_gate_rejects_controller_execution_and_unbound_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _factory_record(tmp_path, monkeypatch)
    params = dict(
        issue=record.issue_id, attempt=record.attempt, backend="feature_factory",
        branch=record.branch, checkout=Path(record.sandbox),
        started_at=datetime.now(UTC), base_ref=record.base_ref, head_ref="HEAD",
        backend_status="completed", test_command="uv run pytest -q",
        factory_run_id=record.run_id, safe_git=None,
        work_spec=_gate_spec(record), compute=None, on_gate_launch=None,
        transcript=None, pr_url=None, blocked_reason=None, model=None,
        failure_reason=None, executor_tests=None, skip_test_reason=None,
        runner=None, include_checkout_status=True,
    )
    with pytest.raises(ComputeLaunchError, match="recorded worker sandbox"):
        await evidence._observe_evidence_from_ref(**params)
    params["compute"] = LocalSubprocessComputeBackend()

    def reached_git(*args: object, **kwargs: object) -> None:
        raise RuntimeError("factory gate reached observed git")

    params["runner"] = reached_git
    with pytest.raises(RuntimeError, match="factory gate reached observed git"):
        await evidence._observe_evidence_from_ref(**params)
    params["checkout"] = Path(record.sandbox) / ".factory" / "worktrees" / "leaf"
    with pytest.raises(ComputeLaunchError, match="recorded worker sandbox"):
        await evidence._observe_evidence_from_ref(**params)


@pytest.mark.asyncio
async def test_factory_gate_command_is_executed_by_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _factory_record(tmp_path, monkeypatch)
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def worker(checkout: Path, argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((checkout, argv))
        return subprocess.CompletedProcess(argv, 0, b"passed", b"")

    monkeypatch.setattr(worker_client, "run_factory_control", worker)
    result = await evidence._run_compute_gate(
        "uv run pytest -q", checkout=Path(record.sandbox),
        work_spec=_gate_spec(record), compute=LocalSubprocessComputeBackend(),
    )
    assert result.exit_code == 0
    assert calls == [(Path(record.sandbox), (
        "/bin/sh", "-c", f"cd {record.sandbox} && uv run pytest -q",
    ))]
