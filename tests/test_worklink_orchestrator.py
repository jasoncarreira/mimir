from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

import pytest
import asyncio

from mimir.event_logger import _reset_logger_for_tests, init_logger
from mimir.worklink.backends import Caps, ComputeCaps, ComputeResult, RawResult, WorkOrder
from mimir.worklink.evidence import EvidenceValidation, WorklinkEvidence
from mimir.worklink.backends.registry import BackendRegistry, WorklinkConfig, WorklinkDefaults
from mimir.worklink.claims import ChainlinkClaims, ClaimRecord, claim_records_from_comments
from mimir.worklink.compute import WorkSpec
from mimir.worklink.worker import WorkerPayload, run_worker_payload
from mimir.worklink.worktree import WorktreeLease
from mimir.worklink.orchestrator import (
    IssueContext,
    LeafValidationError,
    WorklinkRunner,
    _demote_template_invalid_ready_leaf,
    _run_remote_test_job,
    render_decomposition_prompt,
    validate_leaf,
)


class FakeCompute:
    name = "fake_compute"

    def __init__(self, *, shared_filesystem: bool = False) -> None:
        self.shared_filesystem = shared_filesystem
        self.specs: list[WorkSpec] = []
        self.cleaned: list[WorkSpec] = []

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(self.shared_filesystem, False, True, False)

    async def launch(self, spec: WorkSpec) -> WorkSpec:
        self.specs.append(spec)
        return spec

    async def wait(self, handle: WorkSpec, timeout_s: int) -> ComputeResult:
        return ComputeResult(exit_code=0, stdout="ok", stderr="")

    async def logs(self, handle: WorkSpec) -> str:
        return ""

    async def cancel(self, handle: WorkSpec) -> None:
        return None

    async def cleanup(self, handle: WorkSpec) -> None:
        self.cleaned.append(handle)


class SlowTestCompute(FakeCompute):
    async def wait(self, handle: WorkSpec, timeout_s: int) -> ComputeResult:
        await asyncio.sleep(0)
        return ComputeResult(exit_code=0, stdout="tests ok", stderr="")


class FakeBackend:
    name = "fake"

    def __init__(self, status: str = "success", *, write_change: bool = True) -> None:
        self.status = status
        self.write_change = write_change
        self.orders: list[WorkOrder] = []

    def capabilities(self) -> Caps:
        return Caps("fake", False, False, False, True, None)

    def work_spec(
        self,
        order: WorkOrder,
        *,
        attempt: int,
        repo_url: str,
        base_ref: str,
        branch: str,
        test_command: str,
    ) -> WorkSpec:
        return WorkSpec(
            issue_id=order.issue_id,
            attempt=attempt,
            repo_url=repo_url,
            base_ref=base_ref,
            branch=branch,
            prompt=order.prompt,
            rules=order.rules,
            test_command=test_command,
            backend=self.name,
            timeout_s=order.timeout_s,
            env=order.env,
            local_worktree=order.worktree,
        )

    async def interpret(self, order: WorkOrder, result: object) -> RawResult:
        self.orders.append(order)
        if self.write_change:
            (order.worktree / "changed.txt").write_text("hello\n", encoding="utf-8")
        return RawResult(
            0 if self.status == "success" else 1,
            order.transcript_root / "fake.json",
            self.status,
            None,
        )



class WorkerFakeBackend(FakeBackend):
    def work_spec(
        self,
        order: WorkOrder,
        *,
        attempt: int,
        repo_url: str,
        base_ref: str,
        branch: str,
        test_command: str,
    ) -> WorkSpec:
        return WorkSpec(
            issue_id=order.issue_id,
            attempt=attempt,
            repo_url=repo_url,
            base_ref=base_ref,
            branch=branch,
            prompt=order.prompt,
            rules=order.rules,
            test_command=test_command,
            backend=self.name,
            timeout_s=order.timeout_s,
            env=order.env,
            local_worktree=order.worktree,
            local_argv=("fake-tool", "--cd", str(order.worktree), order.prompt),
        )


@pytest.mark.asyncio
async def test_remote_test_job_heartbeats_claim_during_finalize() -> None:
    comments: list[str] = []
    heartbeat_at = datetime(2026, 6, 12, 12, 45, tzinfo=UTC)

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(args)[1:3] == ["issue", "comment"]:
            comments.append(str(args[-1]))
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

    claims = ChainlinkClaims(
        agent_id="mimir-worklink",
        runner=runner,
        clock=lambda: heartbeat_at,
    )
    claim_record = ClaimRecord(
        issue_id=750,
        attempt=1,
        agent_id="mimir-worklink",
        claimed_at=datetime(2026, 6, 12, 12, tzinfo=UTC),
    )
    spec = WorkSpec(
        issue_id=750,
        attempt=1,
        repo_url="https://example.invalid/repo.git",
        base_ref="main",
        branch="worklink/750-1",
        prompt="",
        rules=None,
        test_command="pytest",
        backend="fake",
        timeout_s=1800,
        env={},
    )

    outcome = await _run_remote_test_job(
        SlowTestCompute(),
        spec,
        timeout_s=1800,
        claims=claims,
        claim_record=claim_record,
    )

    assert outcome.exit_code == 0
    assert outcome.failure_tail is None
    heartbeats = claim_records_from_comments(comments)
    assert heartbeats
    assert heartbeats[-1].issue_id == 750
    assert heartbeats[-1].attempt == 1
    assert heartbeats[-1].heartbeat_at == heartbeat_at


class WorkerOddArgvBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[ComputeResult] = []

    def work_spec(
        self,
        order: WorkOrder,
        *,
        attempt: int,
        repo_url: str,
        base_ref: str,
        branch: str,
        test_command: str,
    ) -> WorkSpec:
        return WorkSpec(
            issue_id=order.issue_id,
            attempt=attempt,
            repo_url=repo_url,
            base_ref=base_ref,
            branch=branch,
            prompt=order.prompt,
            rules=order.rules,
            test_command=test_command,
            backend=self.name,
            timeout_s=order.timeout_s,
            env=order.env,
            local_worktree=order.worktree,
            local_argv=(
                sys.executable,
                "-c",
                "print('ok')",
                "-p",
                order.prompt,
                "--output-format",
                "json",
            ),
        )

    async def interpret(self, order: WorkOrder, result: object) -> RawResult:
        assert isinstance(result, ComputeResult)
        self.results.append(result)
        return await super().interpret(order, result)


def test_worker_payload_clone_branch_fake_backend_pushes_and_writes_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "worker-repo"
    evidence_path = tmp_path / "out" / "evidence.json"
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:2] == ["git", "clone"]:
            repo.mkdir(parents=True)
            (repo / ".git").mkdir()
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "fetch"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "checkout"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--name-only" in args:
            return cp(args, stdout="changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--stat" in args:
            return cp(args, stdout=" changed.txt | 1 +\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "status"]:
            return cp(args, stdout="?? changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "add"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "commit"]:
            return cp(args, stdout="[issue/456-a1 abc123] worklink\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "push"]:
            return cp(args)
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        return cp(args)

    backend = WorkerFakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    spec = backend.work_spec(
        WorkOrder(
            issue_id=456,
            worktree=tmp_path / "origin-local-worktree-is-ignored",
            prompt="Do worker handoff",
            rules=None,
            timeout_s=30,
            env={"MIMIR_HOME": str(tmp_path / "home")},
            transcript_root=tmp_path / "transcripts",
        ),
        attempt=1,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="origin/main",
        branch="issue/456-a1",
        test_command="echo ok",
    )
    payload = WorkerPayload(
        spec=spec,
        repo_dir=repo,
        evidence_path=evidence_path,
        transcript_root=tmp_path / "transcripts",
        safe_env={"PATH": "/bin"},
    )

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.status == "completed"
    assert validation.review_ready is True
    assert evidence_path.is_file()
    assert ["git", "clone", "git@github.com:jasoncarreira/mimir.git", str(repo)] in calls
    assert ["git", "-C", str(repo), "checkout", "-B", "issue/456-a1", "origin/main"] in calls
    assert ["git", "-C", str(repo), "push", "origin", "HEAD:issue/456-a1"] in calls
    assert calls.count("echo ok") == 1
    assert backend.orders[0].worktree == repo
    success_out = capsys.readouterr().out
    assert "WORKLINK_WORKER_DIAG" not in success_out
    assert "WORKLINK_TESTS_TAIL" not in success_out


def test_worker_payload_no_commit_result_does_not_push(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "worker-repo"
    evidence_path = tmp_path / "out" / "evidence.json"
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:2] == ["git", "clone"]:
            repo.mkdir(parents=True)
            (repo / ".git").mkdir()
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--cached" in args:
            return cp(args, stdout="")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--name-only" in args:
            return cp(args, stdout="")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--stat" in args:
            return cp(args, stdout="")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "status"]:
            return cp(args, stdout="")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "add"]:
            return cp(args)
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        return cp(args)

    backend = WorkerFakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    spec = backend.work_spec(
        WorkOrder(
            issue_id=456,
            worktree=tmp_path / "origin-local-worktree-is-ignored",
            prompt="Do worker handoff",
            rules=None,
            timeout_s=30,
            env={"MIMIR_HOME": str(tmp_path / "home")},
            transcript_root=tmp_path / "transcripts",
        ),
        attempt=1,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="origin/main",
        branch="issue/456-a1",
        test_command="echo ok",
    )
    payload = WorkerPayload(
        spec=spec,
        repo_dir=repo,
        evidence_path=evidence_path,
        transcript_root=tmp_path / "transcripts",
        safe_env={"PATH": "/bin"},
    )

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.status == "failed"
    assert validation.review_ready is False
    assert "backend_produced_no_changes" in validation.reasons
    assert validation.evidence.blocked_reason == "backend produced no changes"
    assert not any(isinstance(call, list) and call[:4] == ["git", "-C", str(repo), "commit"] for call in calls)
    assert not any(isinstance(call, list) and call[:4] == ["git", "-C", str(repo), "push"] for call in calls)
    diag = capsys.readouterr().out
    assert "WORKLINK_WORKER_DIAG_BEGIN" in diag
    assert "backend_produced_no_changes" in diag


class WorkerScriptedBackend(WorkerFakeBackend):
    """WorkerFakeBackend whose local compute job runs an arbitrary python -c script."""

    def __init__(self, *, script: str, status: str = "failed") -> None:
        super().__init__()
        self.status = status
        self._script = script

    def work_spec(
        self,
        order: WorkOrder,
        *,
        attempt: int,
        repo_url: str,
        base_ref: str,
        branch: str,
        test_command: str,
    ) -> WorkSpec:
        from dataclasses import replace

        spec = super().work_spec(
            order,
            attempt=attempt,
            repo_url=repo_url,
            base_ref=base_ref,
            branch=branch,
            test_command=test_command,
        )
        return replace(spec, local_argv=(sys.executable, "-c", self._script))


def _worker_diag_payload(
    tmp_path: Path,
    backend: WorkerFakeBackend,
    *,
    env: dict[str, str] | None = None,
    timeout_s: int = 30,
) -> WorkerPayload:
    spec = backend.work_spec(
        WorkOrder(
            issue_id=793,
            worktree=tmp_path / "ignored",
            prompt="Do worker handoff",
            rules=None,
            timeout_s=timeout_s,
            env=env or {},
            transcript_root=tmp_path / "transcripts",
        ),
        attempt=2,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="origin/main",
        branch="issue/793-a2",
        test_command="echo ok",
    )
    return WorkerPayload(
        spec=spec,
        repo_dir=tmp_path / "worker-repo",
        evidence_path=tmp_path / "out" / "evidence.json",
        transcript_root=tmp_path / "transcripts",
        safe_env={"PATH": "/bin"},
    )


def _worker_diag_runner(repo: Path) -> Any:
    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:2] == ["git", "clone"]:
            repo.mkdir(parents=True, exist_ok=True)
            (repo / ".git").mkdir(exist_ok=True)
            return cp(args)
        return cp(args)

    return runner


def test_worker_backend_failure_emits_bounded_redacted_diag_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = WorkerScriptedBackend(
        script=(
            "import sys; "
            "sys.stderr.write('codex: request failed with status 429\\n"
            "Authorization: Bearer live-token-abcdef\\n"
            "env leak: injected-secret-value-123\\n'); "
            "sys.exit(3)"
        ),
    )
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir(parents=True)
    transcript_lines = [f"transcript-line-{index}" for index in range(1, 41)]
    transcript_lines.append('{"api_key": "sk-aaaabbbbccccddddeeee"}')
    (transcripts / "fake.json").write_text("\n".join(transcript_lines), encoding="utf-8")
    payload = _worker_diag_payload(
        tmp_path, backend, env={"CODEX_API_KEY": "injected-secret-value-123"}
    )

    validation = asyncio.run(
        run_worker_payload(payload, registry=registry, runner=_worker_diag_runner(payload.repo_dir))
    )

    assert validation.review_ready is False
    out = capsys.readouterr().out
    assert "WORKLINK_WORKER_DIAG_BEGIN" in out
    assert "WORKLINK_WORKER_DIAG_END" in out
    diag = out.split("WORKLINK_WORKER_DIAG_BEGIN", 1)[1].split("WORKLINK_WORKER_DIAG_END", 1)[0]
    assert "issue=#793 attempt=2" in diag
    assert "backend_exit=3" in diag
    assert "codex: request failed with status 429" in diag
    # Credential patterns and injected env values are redacted.
    assert "live-token-abcdef" not in diag
    assert "injected-secret-value-123" not in diag
    assert "sk-aaaabbbbccccddddeeee" not in diag
    assert "<redacted>" in diag
    # Transcript tail keeps the last 30 lines only.
    assert "transcript-line-40" in diag
    assert "transcript-line-1\n" not in diag


def test_worker_gate_failure_emits_tests_tail_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """chainlink #815: a failed gate run prints a delimited, redacted tests-tail
    section on stdout — the only channel that outlives the worker container."""
    backend = WorkerFakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    repo = tmp_path / "worker-repo"
    gate_output = "\n".join(
        [f"noise-line-{index}" for index in range(1, 71)]
        + [
            "token=super-secret-gate-value",
            "FAILED tests/test_z.py::test_gate - AssertionError",
            "1 failed, 9 passed",
        ]
    )

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:2] == ["git", "clone"]:
            repo.mkdir(parents=True, exist_ok=True)
            (repo / ".git").mkdir(exist_ok=True)
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--cached" in args:
            return cp(args, stdout="changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--name-only" in args:
            return cp(args, stdout="changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--stat" in args:
            return cp(args, stdout=" changed.txt | 1 +\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "status"]:
            return cp(args, stdout="?? changed.txt\n")
        if args == "echo ok":
            return cp(args, returncode=1, stdout=gate_output)
        return cp(args)

    payload = _worker_diag_payload(tmp_path, backend)

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.review_ready is False
    assert "tests_failed" in validation.reasons
    out = capsys.readouterr().out
    assert "WORKLINK_TESTS_TAIL_BEGIN" in out
    section = out.split("WORKLINK_TESTS_TAIL_BEGIN", 1)[1].split("WORKLINK_TESTS_TAIL_END", 1)[0]
    assert "command: echo ok" in section
    assert "exit: 1" in section
    assert "FAILED tests/test_z.py::test_gate" in section
    # Tail-based: early noise dropped; secrets redacted.
    assert "noise-line-1\n" not in section
    assert "super-secret-gate-value" not in section
    # The general diag block still follows for full context, and (chainlink
    # #816) carries its own bounded gate-tests section.
    assert "WORKLINK_WORKER_DIAG_BEGIN" in out
    diag = out.split("WORKLINK_WORKER_DIAG_BEGIN", 1)[1].split("WORKLINK_WORKER_DIAG_END", 1)[0]
    assert "--- gate tests: echo ok (exit 1) ---" in diag
    assert "FAILED tests/test_z.py::test_gate" in diag


def test_extract_gate_test_tail_parses_and_bounds() -> None:
    from mimir.worklink.worker import extract_gate_test_tail

    stdout = (
        "worklink worker: failed — tests_failed\n"
        "WORKLINK_TESTS_TAIL_BEGIN\ncommand: pytest\nexit: 1\nFAILED a::b\nWORKLINK_TESTS_TAIL_END\n"
    )
    assert extract_gate_test_tail(stdout) == "command: pytest\nexit: 1\nFAILED a::b"
    assert extract_gate_test_tail("worklink worker: completed") is None
    assert extract_gate_test_tail(None) is None
    assert extract_gate_test_tail("") is None
    unterminated = "WORKLINK_TESTS_TAIL_BEGIN\n" + "x" * 20000
    parsed = extract_gate_test_tail(unterminated)
    assert parsed is not None and len(parsed) <= 6000


def test_worker_diag_block_is_size_capped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = WorkerScriptedBackend(
        script="import sys; sys.stderr.write(('x' * 400 + '\\n') * 60); sys.exit(1)",
    )
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    payload = _worker_diag_payload(tmp_path, backend)

    asyncio.run(
        run_worker_payload(payload, registry=registry, runner=_worker_diag_runner(payload.repo_dir))
    )

    out = capsys.readouterr().out
    assert "WORKLINK_WORKER_DIAG_END" in out
    diag = out.split("WORKLINK_WORKER_DIAG_BEGIN", 1)[1].split("WORKLINK_WORKER_DIAG_END", 1)[0]
    assert len(diag) <= 8600
    # chainlink #816: per-section caps — the header always survives, and the
    # oversized stderr section is tail-capped with a marker.
    assert "issue=#793 attempt=2" in diag
    assert "(truncated)" in diag


def test_worker_diag_header_survives_giant_single_line_transcript(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """chainlink #816: one giant transcript JSONL line used to eat the whole
    8KB budget and truncate away the header + stderr sections."""
    backend = WorkerScriptedBackend(
        script="import sys; sys.stderr.write('codex: the real error line\\n'); sys.exit(3)",
    )
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "fake.json").write_text('{"blob": "' + "y" * 50000 + '"}', encoding="utf-8")
    payload = _worker_diag_payload(tmp_path, backend)

    asyncio.run(
        run_worker_payload(payload, registry=registry, runner=_worker_diag_runner(payload.repo_dir))
    )

    out = capsys.readouterr().out
    diag = out.split("WORKLINK_WORKER_DIAG_BEGIN", 1)[1].split("WORKLINK_WORKER_DIAG_END", 1)[0]
    assert "issue=#793 attempt=2" in diag
    assert "backend_exit=3" in diag
    assert "codex: the real error line" in diag
    assert len(diag) <= 8600


def test_worker_diag_redacts_secret_straddling_truncation_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """chainlink #816 review blocker: redaction must run BEFORE truncation — a
    secret split by the clip boundary leaves a suffix that exact-value
    replacement can no longer match."""
    secret = "SECRETVALUE1234567890x"
    # Place the secret so the 2400-char stderr cap boundary falls INSIDE it.
    backend = WorkerScriptedBackend(
        script=(
            "import sys; "
            f"sys.stderr.write('padding ' + '{secret}' + 'B' * 2390); "
            "sys.exit(3)"
        ),
    )
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    payload = _worker_diag_payload(tmp_path, backend, env={"CODEX_API_KEY": secret})

    asyncio.run(
        run_worker_payload(payload, registry=registry, runner=_worker_diag_runner(payload.repo_dir))
    )

    out = capsys.readouterr().out
    assert secret not in out
    assert "1234567890x" not in out  # no straddle suffix leak
    assert "<redacted>" in out


def test_tests_tail_redacts_secret_straddling_clip_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "SECRETVALUE1234567890x"
    backend = WorkerScriptedBackend(script="print('ok')", status="success")
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    payload = _worker_diag_payload(tmp_path, backend, env={"CODEX_API_KEY": secret})
    gate_output = "padding " + secret + "B" * 5990  # 6000-char clip lands inside the secret
    repo = payload.repo_dir
    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:2] == ["git", "clone"]:
            repo.mkdir(parents=True, exist_ok=True)
            (repo / ".git").mkdir(exist_ok=True)
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--cached" in args:
            return cp(args, stdout="changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--name-only" in args:
            return cp(args, stdout="changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--stat" in args:
            return cp(args, stdout=" changed.txt | 1 +\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "status"]:
            return cp(args, stdout="?? changed.txt\n")
        if args == "echo ok":
            return cp(args, returncode=1, stdout=gate_output)
        return cp(args)

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.review_ready is False
    out = capsys.readouterr().out
    assert "WORKLINK_TESTS_TAIL_BEGIN" in out
    assert secret not in out
    assert "1234567890x" not in out


def _repair_gate_runner(repo: Path, *, gate_results: list[int], gate_output: str) -> Any:
    """Runner whose gate command pops exit codes from ``gate_results`` per call."""

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:2] == ["git", "clone"]:
            repo.mkdir(parents=True, exist_ok=True)
            (repo / ".git").mkdir(exist_ok=True)
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--cached" in args:
            return cp(args, stdout="changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--name-only" in args:
            return cp(args, stdout="changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--stat" in args:
            return cp(args, stdout=" changed.txt | 1 +\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "status"]:
            return cp(args, stdout="?? changed.txt\n")
        if args == "echo ok":
            code = gate_results.pop(0) if gate_results else 0
            return cp(args, returncode=code, stdout=gate_output if code else "all green")
        return cp(args)

    return runner

def test_worker_repairs_gate_failure_within_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """chainlink #817: a failed gate triggers an in-attempt repair round — the
    backend is re-invoked in the same checkout with the failure tail, the gate
    re-runs, and a now-green attempt pushes normally."""
    backend = WorkerScriptedBackend(script="print('ok')", status="success")
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    payload = _worker_diag_payload(tmp_path, backend, timeout_s=600)
    runner = _repair_gate_runner(
        payload.repo_dir,
        gate_results=[1],  # first gate run fails, re-run passes
        gate_output="FAILED tests/test_q.py::test_it - AssertionError",
    )

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.review_ready is True
    assert validation.evidence.repair_rounds == 1
    assert len(backend.orders) == 2
    repair_prompt = backend.orders[1].prompt
    assert "NOT done until the gate command passes" in repair_prompt
    assert "FAILED tests/test_q.py::test_it" in repair_prompt
    assert "NEVER delete, skip, or weaken tests" in repair_prompt
    out = capsys.readouterr().out
    assert "WORKLINK_TESTS_TAIL" not in out  # repaired attempt is a clean success
    evidence = json.loads(payload.evidence_path.read_text(encoding="utf-8"))
    assert evidence["repair_rounds"] == 1


def test_worker_repair_exhausts_rounds_then_fails_with_tail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = WorkerScriptedBackend(script="print('ok')", status="success")
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    payload = _worker_diag_payload(tmp_path, backend, timeout_s=600)
    runner = _repair_gate_runner(
        payload.repo_dir,
        gate_results=[1, 1],  # fails before AND after the repair round
        gate_output="FAILED tests/test_q.py::test_it - AssertionError",
    )

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.review_ready is False
    assert "tests_failed" in validation.reasons
    assert validation.evidence.repair_rounds == 1
    assert len(backend.orders) == 2  # implement + one bounded repair round
    out = capsys.readouterr().out
    assert "WORKLINK_TESTS_TAIL_BEGIN" in out
    assert "WORKLINK_WORKER_DIAG_BEGIN" in out


def test_worker_repair_rounds_zero_disables(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dataclasses import replace as dc_replace

    backend = WorkerScriptedBackend(script="print('ok')", status="success")
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    payload = _worker_diag_payload(tmp_path, backend, timeout_s=600)
    payload = WorkerPayload(
        spec=dc_replace(payload.spec, gate_repair_rounds=0),
        repo_dir=payload.repo_dir,
        evidence_path=payload.evidence_path,
        transcript_root=payload.transcript_root,
        safe_env=payload.safe_env,
    )
    runner = _repair_gate_runner(
        payload.repo_dir, gate_results=[1, 1], gate_output="FAILED tests/test_q.py::t"
    )

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.review_ready is False
    assert len(backend.orders) == 1  # no repair invocation
    assert validation.evidence.repair_rounds == 0


def test_worker_repair_skipped_when_timeout_budget_low(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Repair never extends wall-clock: with <2min of attempt budget left the
    worker fails straight through rather than starting a round it can't finish."""
    backend = WorkerScriptedBackend(script="print('ok')", status="success")
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    payload = _worker_diag_payload(tmp_path, backend, timeout_s=30)
    runner = _repair_gate_runner(
        payload.repo_dir, gate_results=[1, 1], gate_output="FAILED tests/test_q.py::t"
    )

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.review_ready is False
    assert len(backend.orders) == 1
    assert validation.evidence.repair_rounds == 0


def test_worker_gate_command_not_found_skips_repair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """chainlink #820: exit 127 (gate command missing) must not burn a #817
    repair round — no code change can fix the environment."""
    backend = WorkerScriptedBackend(script="print('ok')", status="success")
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    payload = _worker_diag_payload(tmp_path, backend, timeout_s=600)
    runner = _repair_gate_runner(
        payload.repo_dir, gate_results=[127, 127], gate_output="/bin/sh: 1: pytest: not found"
    )

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.review_ready is False
    assert "gate_command_not_found" in validation.reasons
    assert "tests_failed" not in validation.reasons
    assert len(backend.orders) == 1  # no repair invocation
    assert validation.evidence.repair_rounds == 0
    out = capsys.readouterr().out
    assert "WORKLINK_TESTS_TAIL_BEGIN" in out  # the tail is how 127 was finally seen
    assert "pytest: not found" in out


def test_worker_prepares_slash_named_feature_base(tmp_path: Path) -> None:
    # Regression for #467: a long-running feature base such as
    # `integration/worklink` must be materialized as a local ref and checked out
    # from it — previously slash names were never given a local branch and the
    # checkout failed, so the remote worker could not use the feature-branch model.
    repo = tmp_path / "worker-repo"
    evidence_path = tmp_path / "out" / "evidence.json"
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:2] == ["git", "clone"]:
            repo.mkdir(parents=True)
            (repo / ".git").mkdir()
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--name-only" in args:
            return cp(args, stdout="changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--stat" in args:
            return cp(args, stdout=" changed.txt | 1 +\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "status"]:
            return cp(args, stdout="?? changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "commit"]:
            return cp(args, stdout="[issue/456-a1 abc123] worklink\n")
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        return cp(args)

    backend = WorkerFakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    spec = backend.work_spec(
        WorkOrder(
            issue_id=456,
            worktree=tmp_path / "ignored",
            prompt="Do worker handoff",
            rules=None,
            timeout_s=30,
            env={"MIMIR_HOME": str(tmp_path / "home")},
            transcript_root=tmp_path / "transcripts",
        ),
        attempt=1,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="integration/worklink",
        branch="issue/456-a1",
        test_command="echo ok",
    )
    payload = WorkerPayload(
        spec=spec,
        repo_dir=repo,
        evidence_path=evidence_path,
        transcript_root=tmp_path / "transcripts",
        safe_env={"PATH": "/bin"},
    )

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.status == "completed"
    # The slash base is fetched, the attempt branch is checked out off FETCH_HEAD,
    # THEN base is materialized as a local ref — in that order, so a fresh clone
    # whose HEAD is on base_ref doesn't fail force-updating the checked-out branch
    # ("cannot force update the branch used by worktree" — the docker-sibling bug).
    assert ["git", "-C", str(repo), "fetch", "origin", "integration/worklink"] in calls
    checkout = ["git", "-C", str(repo), "checkout", "-B", "issue/456-a1", "FETCH_HEAD"]
    branch_f = ["git", "-C", str(repo), "branch", "-f", "integration/worklink", "FETCH_HEAD"]
    assert checkout in calls and branch_f in calls
    assert calls.index(checkout) < calls.index(branch_f)


def test_worker_asks_backend_to_localize_tool_argv(tmp_path: Path) -> None:
    repo = tmp_path / "worker-repo"
    evidence_path = tmp_path / "out" / "evidence.json"
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:2] == ["git", "clone"]:
            repo.mkdir(parents=True)
            (repo / ".git").mkdir()
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "fetch"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "checkout"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--name-only" in args:
            return cp(args, stdout="changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "diff"] and "--stat" in args:
            return cp(args, stdout=" changed.txt | 1 +\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "status"]:
            return cp(args, stdout="")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "add"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "commit"]:
            return cp(args, stdout="[issue/456-a1 abc123] worklink\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "push"]:
            return cp(args)
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        return cp(args)

    backend = WorkerOddArgvBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    original = WorkSpec(
        issue_id=456,
        attempt=1,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="origin/main",
        branch="issue/456-a1",
        prompt="Do worker handoff",
        rules=None,
        test_command="echo ok",
        backend=backend.name,
        timeout_s=30,
        env={"MIMIR_HOME": str(tmp_path / "home")},
        local_worktree=tmp_path / "origin-local-worktree-is-ignored",
        local_argv=("orchestrator-tool", "--not-worker-safe"),
    )
    payload = WorkerPayload(
        spec=original,
        repo_dir=repo,
        evidence_path=evidence_path,
        transcript_root=tmp_path / "transcripts",
        safe_env={"PATH": "/bin"},
    )

    validation = asyncio.run(run_worker_payload(payload, registry=registry, runner=runner))

    assert validation.status == "completed"
    assert backend.results
    assert backend.results[0].exit_code == 0
    assert backend.results[0].command == (
        sys.executable,
        "-c",
        "print('ok')",
        "-p",
        "Do worker handoff",
        "--output-format",
        "json",
    )
    assert backend.orders[0].worktree == repo
    assert ["git", "-C", str(repo), "push", "origin", "HEAD:issue/456-a1"] in calls


def test_orchestrator_passes_configured_compute_backend_to_tool_backend(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    compute = FakeCompute(shared_filesystem=True)
    calls: list[Sequence[str] | str] = []

    def runner(
        args: Sequence[str] | str, **_: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "claim"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "release"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "comment"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "close"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "label"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "unlabel"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True)
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "diff"]:
            if "--cached" in args and "--quiet" in args:
                return cp(args, returncode=1)
            return cp(args, stdout=" changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "status"]:
            return cp(args)
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        if isinstance(args, list) and args[:3] == ["gh", "pr", "create"]:
            return cp(args, stdout="https://github.com/jasoncarreira/mimir/pull/999\n")
        return cp(args)

    class ComputeAwareBackend(FakeBackend):
        def work_spec(
            self,
            order: WorkOrder,
            *,
            attempt: int,
            repo_url: str,
            base_ref: str,
            branch: str,
            test_command: str,
        ) -> WorkSpec:
            return WorkSpec(
                issue_id=order.issue_id,
                attempt=attempt,
                repo_url=repo_url,
                base_ref=base_ref,
                branch=branch,
                prompt=order.prompt,
                rules=order.rules,
                test_command=test_command,
                backend=self.name,
                timeout_s=order.timeout_s,
                env=order.env,
                backend_config={"bin": "fake-tool", "args": []},
                local_worktree=order.worktree,
            )

        async def interpret(self, order: WorkOrder, result: object) -> RawResult:
            self.orders.append(order)
            assert isinstance(result, ComputeResult)
            (order.worktree / "changed.txt").write_text(result.stdout + "\n", encoding="utf-8")
            return RawResult(result.exit_code, order.transcript_root / "fake.json", "success", None)


    backend = ComputeAwareBackend(status="success")
    registry = BackendRegistry(WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_compute")))
    registry.register(backend)
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed", (result.reason, calls)
    assert compute.specs
    assert compute.specs[0].issue_id == 441
    assert compute.specs[0].attempt == 1
    assert compute.specs[0].branch == "issue/441-a1"
    assert compute.specs[0].repo_url == "git@github.com:jasoncarreira/mimir.git"
    assert compute.specs[0].base_ref == "main"
    assert compute.specs[0].test_command == "echo ok"
    assert compute.specs[0].local_worktree == worktree
    assert compute.specs[0].env["MIMIR_HOME"] == str(tmp_path)
    assert compute.cleaned == [compute.specs[0]]


def cp(
    args: Sequence[str] | str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


ISSUE_JSON = '''{
  "id": 441,
  "title": "worklink slice",
  "description": "Acceptance criteria:\\n- [ ] do it\\n- [ ] echo ok\\n\\nReview criteria:\\n- reviewer checks it\\n\\nWorklink notes:\\n- Scope: test fixture\\n- Out of scope: unrelated work\\n- Suggested test command: echo ok",
  "labels": ["worklink"],
  "parent_id": 380,
  "comments": []
}'''


def test_validate_leaf_refuses_missing_planner_template() -> None:
    issue = IssueContext(1, "vague", "please do thing", set())

    with pytest.raises(LeafValidationError, match="Acceptance criteria"):
        validate_leaf(issue)


def test_dry_run_prints_rendered_work_order_without_mutations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(tmp_path / "repo"), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        return cp(args)

    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)
    result = asyncio.run(
        WorklinkRunner(
            home=tmp_path, repo=tmp_path / "repo", runner=runner, registry=registry
        ).run(441, backend_name="fake", dry_run=True)
    )

    out = capsys.readouterr().out
    assert result.dry_run is True
    assert "worklink slice" in out
    assert "Acceptance criteria" in out
    # The work order teaches backends how to signal a design-level block.
    assert "WORKLINK_BLOCKED:" in out
    assert not any(isinstance(call, list) and call[:2] == ["chainlink", "locks"] for call in calls)
    assert backend.orders == []




def _orchestrator_runner(
    repo: Path,
    worktree: Path,
    *,
    files_stdout: str = "changed.txt\n",
    dirty_after_commit: bool = False,
    cleanup_returncode: int = 0,
):
    calls: list[Sequence[str] | str] = []
    commit_seen = False

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        nonlocal commit_seen
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "claim"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "release"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "comment"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "close"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "label"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "unlabel"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True)
            return cp(args)
        if (
            isinstance(args, list)
            and args[:4] == ["git", "-C", str(worktree), "diff"]
            and "--name-only" in args
        ):
            return cp(args, stdout=files_stdout)
        if (
            isinstance(args, list)
            and args[:4] == ["git", "-C", str(worktree), "diff"]
            and "--stat" in args
        ):
            return cp(args, stdout=" changed.txt | 1 +\n" if files_stdout else "")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "status"]:
            if commit_seen:
                return cp(args, stdout="?? generated.log\n" if dirty_after_commit else "")
            return cp(args, stdout="?? changed.txt\n" if files_stdout else "")
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "add"]:
            return cp(args)
        if isinstance(args, list) and args[:5] == ["git", "-C", str(worktree), "diff", "--cached"]:
            return cp(args, returncode=1 if files_stdout else 0)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "commit"]:
            commit_seen = True
            return cp(args, stdout="[issue/441-a1 abc123] worklink\n")
        # #518: the attempt branch is pushed from the checkout that owns it
        # (lease.path == worktree here), not the parent repo.
        if isinstance(args, list) and args[:3] == ["git", "-C", str(worktree)] and args[3] == "push":
            return cp(args)
        if isinstance(args, list) and args[:3] == ["gh", "pr", "create"]:
            return cp(args, stdout="https://github.com/jasoncarreira/mimir/pull/999\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "remove"]:
            return cp(
                args,
                returncode=cleanup_returncode,
                stderr="worktree cleanup failed\n" if cleanup_returncode else "",
            )
        return cp(args)

    return calls, runner


def test_worklink_rereads_issue_comments_before_claiming(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-2"
    calls: list[Sequence[str] | str] = []

    issue_with_prior_claim = ISSUE_JSON.replace(
        '"comments": []',
        '"comments": [{"content": "WORKLINK_CLAIM {\\"agent_id\\": \\"mimir-worklink\\", \\"attempt\\": 1, \\"claimed_at\\": \\"2026-06-12T12:04:29+00:00\\", \\"heartbeat_at\\": null, \\"issue_id\\": 441}"}]',
    )

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        show_count = sum(
            1
            for call in calls
            if isinstance(call, list) and call[:4] == ["chainlink", "issue", "show", "441"]
        )
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON if show_count == 1 else issue_with_prior_claim)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "claim"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "release"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "comment"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "close"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "label"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "unlabel"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True)
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "diff"]:
            return cp(args, stdout=" changed.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "status"]:
            return cp(args)
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        return cp(args)

    backend = FakeBackend(status="success", write_change=False)
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.attempt == 2
    assert result.branch == "issue/441-a2"
    assert [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "--no-track",
        "-b",
        "issue/441-a2",
        str(worktree),
        "origin/main",
    ] in calls


def test_worklink_runner_happy_path_fake_backend(tmp_path: Path) -> None:
    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    init_logger(events, session_id="test-worklink")
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree)

    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    assert result.review_ready is True
    assert result.pr_url == "https://github.com/jasoncarreira/mimir/pull/999"
    assert (tmp_path / "state" / "worklink" / "evidence" / "441-1.json").is_file()
    assert ["git", "-C", str(worktree), "commit", "-m", "worklink: issue #441"] in calls
    assert ["chainlink", "locks", "release", "441"] in calls
    # #518: the attempt branch is pushed from the checkout that owns it (lease.path),
    # never from the parent repo — the isolated-checkout shape has the branch only
    # inside lease.path, so a parent-repo push fails "src refspec ... does not match".
    assert ["git", "-C", str(worktree), "push", "-u", "origin", "issue/441-a1"] in calls
    assert not any(
        isinstance(c, list) and c[:3] == ["git", "-C", str(repo)] and len(c) > 3 and c[3] == "push"
        for c in calls
    )
    # Default base: worktree cut from main, PR targets main explicitly.
    assert ["git", "-C", str(repo), "fetch", "origin", "main"] in calls
    assert [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "--no-track",
        "-b",
        "issue/441-a1",
        str(worktree),
        "origin/main",
    ] in calls
    pr_calls = [c for c in calls if isinstance(c, list) and c[:3] == ["gh", "pr", "create"]]
    assert pr_calls and pr_calls[0][pr_calls[0].index("--base") + 1] == "main"
    body = events.read_text(encoding="utf-8")
    assert "worklink_claimed" in body
    assert "worklink_evidence" in body
    assert "worklink_transition" in body
    _reset_logger_for_tests()


def test_post_success_cleanup_failure_does_not_retransition_review_ready_issue(
    tmp_path: Path,
) -> None:
    _reset_logger_for_tests()
    events = tmp_path / "logs" / "events.jsonl"
    init_logger(events, session_id="test-worklink")
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree, cleanup_returncode=128)

    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    assert result.review_ready is True
    assert result.pr_url == "https://github.com/jasoncarreira/mimir/pull/999"
    assert result.reason == "post-transition cleanup failed: worktree cleanup failed"
    assert ["chainlink", "issue", "label", "441", "worklink:review"] in calls
    assert ["chainlink", "issue", "label", "441", "worklink:failed"] not in calls
    assert ["chainlink", "issue", "label", "441", "worklink:ready"] not in calls
    records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert any(record["type"] == "worklink_cleanup_failed" for record in records)
    _reset_logger_for_tests()



def test_worklink_runner_cuts_worktree_and_pr_from_configured_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    # worklink.yaml in the home points Worklink at a long-running feature branch.
    (tmp_path / "worklink.yaml").write_text(
        "defaults:\n  base_branch: integration/worklink\n"
    )
    calls, runner = _orchestrator_runner(repo, worktree)
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    # Worktree is cut from the configured base, not main.
    assert ["git", "-C", str(repo), "fetch", "origin", "integration/worklink"] in calls
    assert [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "--no-track",
        "-b",
        "issue/441-a1",
        str(worktree),
        "origin/integration/worklink",
    ] in calls
    # And the PR targets that base (the feature-branch / stacking model).
    pr_calls = [c for c in calls if isinstance(c, list) and c[:3] == ["gh", "pr", "create"]]
    assert pr_calls
    assert pr_calls[0][pr_calls[0].index("--base") + 1] == "integration/worklink"


def test_worklink_run_base_override_beats_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    # Config says one base; the per-run override must win for both worktree + PR.
    (tmp_path / "worklink.yaml").write_text("defaults:\n  base_branch: develop\n")
    calls, runner = _orchestrator_runner(repo, worktree)
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok", base_branch="release/2.0"
        )
    )

    assert result.status == "completed"
    assert ["git", "-C", str(repo), "fetch", "origin", "release/2.0"] in calls
    assert [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "--no-track",
        "-b",
        "issue/441-a1",
        str(worktree),
        "origin/release/2.0",
    ] in calls
    assert not any(
        isinstance(c, list) and c[:5] == ["git", "-C", str(repo), "worktree", "add"] and c[-1] == "develop"
        for c in calls
    )
    pr_calls = [c for c in calls if isinstance(c, list) and c[:3] == ["gh", "pr", "create"]]
    assert pr_calls and pr_calls[0][pr_calls[0].index("--base") + 1] == "release/2.0"


def test_worklink_base_fetch_can_be_disabled_by_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    (tmp_path / "worklink.yaml").write_text("defaults:\n  base_fetch: false\n")
    calls, runner = _orchestrator_runner(repo, worktree)
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "completed"
    assert not any(
        isinstance(c, list) and c[:4] == ["git", "-C", str(repo), "fetch"] for c in calls
    )
    assert [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "--no-track",
        "-b",
        "issue/441-a1",
        str(worktree),
        "main",
    ] in calls




def test_backend_blocked_result_routes_leaf_to_blocked_with_reason(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree)

    class BlockingBackend(FakeBackend):
        async def interpret(self, order: WorkOrder, result: object) -> RawResult:
            self.orders.append(order)
            return RawResult(
                1,
                order.transcript_root / "fake.json",
                "blocked",
                "planner gave contradictory acceptance criteria",
                "planner gave contradictory acceptance criteria",
            )

    backend = BlockingBackend(write_change=False)
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "blocked"
    assert result.review_ready is False
    assert ["chainlink", "issue", "label", "441", "worklink:blocked"] in calls
    assert [
        "chainlink",
        "issue",
        "comment",
        "441",
        "WORKLINK_BLOCKED planner gave contradictory acceptance criteria",
    ] in calls
    assert not any(isinstance(call, list) and call[:3] == ["gh", "pr", "create"] for call in calls)
    evidence = (tmp_path / "state" / "worklink" / "evidence" / "441-1.json").read_text(
        encoding="utf-8"
    )
    assert '"status": "blocked"' in evidence
    assert "planner gave contradictory acceptance criteria" in evidence


def test_remote_compute_gate_rederives_diff_but_does_not_run_tests_on_controller(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    compute = FakeCompute()
    calls: list[Sequence[str] | str] = []

    def runner(
        args: Sequence[str] | str, *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "claim"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "release"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "comment"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "close"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "label"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "unlabel"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True)
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "fetch"]:
            return cp(args)
        if (
            isinstance(args, list)
            and args[:4] == ["git", "-C", str(worktree), "diff"]
            and "origin/main...origin/issue/441-a1" in args
            and "--name-only" in args
        ):
            return cp(args, stdout="remote.txt\n")
        if (
            isinstance(args, list)
            and args[:4] == ["git", "-C", str(worktree), "diff"]
            and "origin/main...origin/issue/441-a1" in args
            and "--stat" in args
        ):
            return cp(args, stdout=" remote.txt | 1 +\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "checkout"]:
            raise AssertionError("remote gate must not checkout untrusted branch on controller")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "diff"]:
            # A remote substrate must not be gated on the placeholder local worktree.
            return cp(args, stdout="")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "status"]:
            return cp(args)
        if args == "echo ok":
            raise AssertionError("remote gate must not run branch tests on controller")
        if isinstance(args, list) and args[:3] == ["gh", "pr", "create"]:
            return cp(args, stdout="https://github.com/jasoncarreira/mimir/pull/1000\n")
        return cp(args)

    backend = FakeBackend(status="success", write_change=False)
    registry = BackendRegistry(WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_compute")))
    registry.register(backend)
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    # #538: the diff is re-derived from refs (never checked out / tested on the
    # controller), AND a fresh SANDBOXED test job runs on the pushed branch via
    # the compute substrate. FakeCompute returns exit 0, so tests are observed +
    # pass and the run reaches review-ready — the gate no longer fails closed.
    assert result.status == "completed"
    assert result.review_ready is True
    assert result.pr_url == "https://github.com/jasoncarreira/mimir/pull/1000"
    assert ["git", "-C", str(worktree), "fetch", "origin", "+main:refs/remotes/origin/main"] in calls
    assert [
        "git",
        "-C",
        str(worktree),
        "fetch",
        "origin",
        "+issue/441-a1:refs/remotes/origin/issue/441-a1",
    ] in calls
    # Controller-safety invariants preserved: never checks out or runs the
    # untrusted branch's tests ON THE CONTROLLER (the runner raises if it tries).
    assert ["git", "-C", str(worktree), "checkout", "--detach", "origin/issue/441-a1"] not in calls
    # The test job WAS dispatched as a separate test_only compute launch.
    assert len(compute.specs) == 2
    assert compute.specs[0].test_only is False
    assert compute.specs[1].test_only is True
    evidence = (tmp_path / "state" / "worklink" / "evidence" / "441-1.json").read_text(
        encoding="utf-8"
    )
    assert "remote.txt" in evidence
    assert "origin/main...origin/issue/441-a1" in evidence
    assert '"observed": true' in evidence
    assert "remote sandboxed test job" in evidence


def test_remote_compute_fetch_failure_blocks_review_gate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    compute = FakeCompute()
    calls: list[Sequence[str] | str] = []

    def runner(
        args: Sequence[str] | str, *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "claim"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "locks", "release"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "comment"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "close"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "label"]:
            return cp(args)
        if isinstance(args, list) and args[:3] == ["chainlink", "issue", "unlabel"]:
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True)
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "fetch"]:
            return cp(args, returncode=1, stderr="missing ref\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "diff"]:
            return cp(args, stdout="remote.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "checkout"]:
            return cp(args)
        if args == "echo ok":
            return cp(args, stdout="ok\n")
        return cp(args)

    backend = FakeBackend(status="success", write_change=False)
    registry = BackendRegistry(WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_compute")))
    registry.register(backend)
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "failed"
    assert result.review_ready is False
    assert not any(isinstance(call, list) and call[:3] == ["gh", "pr", "create"] for call in calls)
    evidence = (tmp_path / "state" / "worklink" / "evidence" / "441-1.json").read_text(
        encoding="utf-8"
    )
    assert "diff_not_observed" in evidence or "missing ref" in evidence

def test_worklink_runner_backend_nonzero_transitions_failed_without_pr(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree)
    backend = FakeBackend(status="backend_error")
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "failed"
    assert result.review_ready is False
    assert not any(
        isinstance(call, list) and call[:3] == ["gh", "pr", "create"]
        for call in calls
    )
    assert ["chainlink", "issue", "label", "441", "worklink:ready"] in calls
    assert ["chainlink", "locks", "release", "441"] in calls


def test_worklink_runner_timeout_transitions_failed_without_pr(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree)
    backend = FakeBackend(status="timeout")
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "failed"
    assert not any(
        isinstance(call, list) and call[:3] == ["gh", "pr", "create"]
        for call in calls
    )
    assert ["chainlink", "issue", "label", "441", "worklink:ready"] in calls


def test_worklink_runner_dirty_after_commit_fails_before_push(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    calls, runner = _orchestrator_runner(repo, worktree, dirty_after_commit=True)
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="fake", test_command="echo ok"
        )
    )

    assert result.status == "failed"
    assert result.reason is None
    assert not any(
        isinstance(call, list)
        and call[:3] == ["git", "-C", str(repo)]
        and call[3] == "push"
        for call in calls
    )
    assert ["chainlink", "issue", "label", "441", "worklink:ready"] in calls

STRICT_ISSUE_JSON = '''{
  "id": 443,
  "title": "strict worklink leaf",
  "description": "Acceptance criteria:\\n- [ ] implement it\\n- [ ] uv run pytest -q tests/test_worklink_orchestrator.py\\n\\nReview criteria:\\n- reviewer verifies scope\\n\\nWorklink notes:\\n- Scope: mimir/worklink\\n- Out of scope: docs-only cleanup\\n- Suggested test command: uv run pytest -q tests/test_worklink_orchestrator.py",
  "labels": ["worklink", "worklink:ready"],
  "parent_id": 380,
  "comments": []
}'''


INVALID_STRICT_ISSUE_JSON = (
    '{\n'
    '  "id": 443,\n'
    '  "title": "strict malformed worklink leaf",\n'
    '  "description": "Acceptance criteria:\\nplain bullet without checklist\\n\\nReview criteria:\\n- reviewer verifies scope",\n'
    '  "labels": ["worklink", "worklink:ready"],\n'
    '  "parent_id": 380,\n'
    '  "created_at": "2026-06-18T11:58:52Z",\n'
    '  "comments": []\n'
    '}'
)


def test_worklink_runner_demotes_template_invalid_ready_leaf_before_claim(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "443"]:
            return cp(args, stdout=INVALID_STRICT_ISSUE_JSON)
        if isinstance(args, list) and args[:3] in (
            ["chainlink", "issue", "unlabel"],
            ["chainlink", "issue", "label"],
            ["chainlink", "issue", "comment"],
        ):
            return cp(args)
        raise AssertionError(f"unexpected call after validation failure: {args}")

    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())

    with pytest.raises(LeafValidationError, match="acceptance checklist item"):
        asyncio.run(
            WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
                443, backend_name="fake", test_command="echo ok"
            )
        )

    assert ["chainlink", "issue", "unlabel", "443", "worklink:ready"] in calls
    assert ["chainlink", "issue", "label", "443", "worklink:blocked"] in calls
    comments = [
        call
        for call in calls
        if isinstance(call, list) and call[:4] == ["chainlink", "issue", "comment", "443"]
    ]
    assert comments and "acceptance checklist item" in comments[0][4]
    # The invalid leaf is removed from the ready queue before any worker claim,
    # so the poller cannot redispatch this same lowest-id leaf forever.
    assert not any(
        isinstance(call, list) and call[:3] == ["chainlink", "locks", "claim"]
        for call in calls
    )


def test_worklink_runner_does_not_demote_epic_brief_for_leaf_template(tmp_path: Path) -> None:
    issue = IssueContext(
        774,
        "epic brief",
        "Build integrated epic mode as the default routing path.",
        {"worklink:ready", "worklink:epic"},
        created_at=datetime(2026, 6, 18, tzinfo=UTC),
    )
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return cp(args)

    validate_leaf(issue)
    _demote_template_invalid_ready_leaf(
        issue,
        reason="issue missing planner template: Acceptance criteria",
        runner=runner,
        chainlink_bin="chainlink",
    )

    assert calls == []


def test_worklink_runner_dry_run_reports_template_error_without_demoting(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "443"]:
            return cp(args, stdout=INVALID_STRICT_ISSUE_JSON)
        raise AssertionError(f"dry-run must not mutate after validation failure: {args}")

    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())

    with pytest.raises(LeafValidationError, match="acceptance checklist item"):
        asyncio.run(
            WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
                443, backend_name="fake", test_command="echo ok", dry_run=True
            )
        )

    assert calls == [["chainlink", "issue", "show", "443", "--json"]]


def test_validate_leaf_requires_worklink_notes_template_for_new_issues() -> None:
    issue = IssueContext(
        443,
        "new loose leaf",
        "Acceptance criteria:\n- [ ] do it\n\nReview criteria: reviewer checks it",
        {"worklink"},
    )

    with pytest.raises(LeafValidationError, match="Worklink notes"):
        validate_leaf(issue)


def test_validate_leaf_warns_for_legacy_leaves_without_orphaning_them() -> None:
    issue = IssueContext(
        445,
        "legacy queued leaf",
        "Acceptance criteria:\n- [ ] do it\n\nReview criteria: reviewer checks it",
        {"worklink"},
        created_at=datetime(2026, 6, 11, tzinfo=UTC),
    )

    with pytest.warns(RuntimeWarning, match="legacy pre-contract leaf"):
        validate_leaf(issue)


def test_planner_prompt_renders_single_leaf_template_constant() -> None:
    from mimir.prompt_templates import bundled_defaults
    from mimir.worklink.planning import LEAF_TEMPLATE_MARKDOWN

    root = Path(__file__).parent.parent
    prompt_path = root / "mimir" / "prompt_templates" / "decompose.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    rendered = render_decomposition_prompt(
        template_path=prompt_path,
        parent_id=380,
        title="parent",
        labels="worklink",
        priority="normal",
        description="parent body",
    )

    assert "{leaf_template}" in prompt
    assert LEAF_TEMPLATE_MARKDOWN not in prompt
    assert LEAF_TEMPLATE_MARKDOWN in rendered
    assert LEAF_TEMPLATE_MARKDOWN in bundled_defaults()["decompose.md"]
    assert "{leaf_template}" not in bundled_defaults()["decompose.md"]


def test_skill_embeds_single_leaf_template_constant() -> None:
    from mimir.worklink.planning import LEAF_TEMPLATE_MARKDOWN

    root = Path(__file__).parent.parent
    skill = (root / "mimir" / "optional-skills" / "chainlink-orchestrator" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert LEAF_TEMPLATE_MARKDOWN in skill


def test_worklink_ignores_planner_suggested_test_command_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[Sequence[str] | str] = []

    issue_json = STRICT_ISSUE_JSON.replace(
        "- Suggested test command: uv run pytest -q tests/test_worklink_orchestrator.py",
        "- Suggested test command: echo planner-controlled; touch /tmp/owned",
    )

    def runner(args: Sequence[str] | str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "443"]:
            return cp(args, stdout=issue_json)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(tmp_path / "repo"), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        return cp(args)

    (tmp_path / "worklink.yaml").write_text("defaults:\n  test_command: echo safe\n", encoding="utf-8")
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(
            home=tmp_path, repo=tmp_path / "repo", runner=runner, registry=registry
        ).run(443, backend_name="fake", dry_run=True)
    )

    out = capsys.readouterr().out
    assert result.dry_run is True
    assert "echo planner-controlled; touch /tmp/owned" in out
    assert "NOT done until the gate command below passes" in out
    assert "  echo safe" in out


def test_decompose_prompt_teaches_chainlink_block_argument_order() -> None:
    prompt = (Path(__file__).parent.parent / "mimir" / "prompt_templates" / "decompose.md").read_text(
        encoding="utf-8"
    )

    assert "chainlink issue block <ID-that-is-blocked> <BLOCKER>" in prompt
    assert "blocked issue id comes first" in prompt
    assert "chainlink issue block <blocker> <blocked>" not in prompt



def test_worklink_prompt_keeps_planner_suggestion_advisory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[Sequence[str] | str] = []

    issue_json = STRICT_ISSUE_JSON.replace(
        "- Suggested test command: uv run pytest -q tests/test_worklink_orchestrator.py",
        "- Suggested test command: `cd /workspace/mimir && pytest -q tests/test_identities.py`",
    )

    def runner(args: Sequence[str] | str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "443"]:
            return cp(args, stdout=issue_json)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(tmp_path / "repo"), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        return cp(args)

    (tmp_path / "worklink.yaml").write_text("defaults:\n  test_command: echo safe\n", encoding="utf-8")
    backend = FakeBackend()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(backend)

    result = asyncio.run(
        WorklinkRunner(
            home=tmp_path, repo=tmp_path / "repo", runner=runner, registry=registry
        ).run(443, backend_name="fake", dry_run=True)
    )

    out = capsys.readouterr().out
    assert result.dry_run is True
    assert "NOT done until the gate command below passes" in out
    assert "Treat it as advisory only" in out
    assert "  echo safe" in out
    assert "  cd /workspace/mimir && pytest -q tests/test_identities.py" not in out

def test_codex_local_subprocess_uses_isolated_checkout(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _create_backend_checkout

    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if (
            args[:3] == ["git", "-C", str(tmp_path)]
            and args[3:6]
            in (["rev-parse", "--verify", "main"], ["rev-parse", "--verify", "origin/main"])
        ):
            return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")
        # Self-containment assert (#517): report the checkout as rooted at itself.
        if args[3:5] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{args[2]}\n", stderr="")
        if args[3:5] == ["rev-parse", "--absolute-git-dir"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{args[2]}/.git\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    lease = _create_backend_checkout(
        tmp_path,
        issue_id=517,
        attempt=2,
        base="main",
        backend_name="codex",
        compute_shared_filesystem=True,
        runner=runner,
    )

    assert lease.isolated_checkout is True
    assert any(call[:3] == ["git", "clone", "--local"] and "--no-hardlinks" not in call for call in calls)
    assert ["git", "-C", str(lease.path), "checkout", "-B", "issue/517-a2", "abc123"] in calls


def test_outside_worktree_detection_marks_root_leak_failed(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _with_outside_worktree_detection

    validation = EvidenceValidation(
        status="failed",
        review_ready=False,
        reasons=("completed_empty_diff",),
        evidence=WorklinkEvidence(
            issue=517,
            attempt=1,
            backend="codex",
            branch="issue/517-a1",
            worktree=str(tmp_path / ".worklink" / "517-1"),
            started_at="2026-06-16T20:00:00+00:00",
            finished_at="2026-06-16T20:05:00+00:00",
            files_changed=[],
            diff_stat="",
            commands=[],
            tests=None,
            pr_url=None,
            status="failed",
        ),
    )

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=" M mimir/identities.py\n?? scratch.txt\n", stderr="")

    result = _with_outside_worktree_detection(
        validation,
        issue=517,
        attempt=1,
        root=tmp_path,
        worktree=tmp_path / ".worklink" / "517-1",
        runner=runner,
    )

    assert result.status == "failed"
    assert result.review_ready is False
    assert "completed_empty_diff" in result.reasons
    assert any(reason.startswith("backend_wrote_outside_worktree:") for reason in result.reasons)


def test_outside_worktree_leak_is_quarantined_recoverably(tmp_path: Path) -> None:
    from mimir.worklink.orchestrator import _dirty_paths, _with_outside_worktree_detection

    def git(*args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(tmp_path), *args], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(args), capture_output=True, text=True, check=False)

    git("init", "-q")
    git("config", "user.email", "t@e.com")
    git("config", "user.name", "t")
    (tmp_path / "keep.txt").write_text("orig\n")
    (tmp_path / "mod.py").write_text("v1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")

    # Pre-existing, unrelated operator dirt that MUST survive quarantine.
    (tmp_path / "keep.txt").write_text("operator-work\n")
    root_dirty_before = _dirty_paths(tmp_path, runner=runner)
    assert root_dirty_before == ["keep.txt"]

    # The leak: codex wrote into the repo root (a new file + a tracked edit) while
    # the attempt diff is empty. The isolated checkout lives OUTSIDE the repo.
    (tmp_path / "leaked.py").write_text("escaped\n")
    (tmp_path / "mod.py").write_text("v1\nCODEX\n")
    worktree = tmp_path.parent / ".worklink" / tmp_path.name / "517-1"

    validation = EvidenceValidation(
        status="failed",
        review_ready=False,
        reasons=("completed_empty_diff",),
        evidence=WorklinkEvidence(
            issue=517, attempt=1, backend="codex", branch="issue/517-a1",
            worktree=str(worktree), started_at="2026-06-16T20:00:00+00:00",
            finished_at="2026-06-16T20:05:00+00:00", files_changed=[], diff_stat="",
            commands=[], tests=None, pr_url=None, status="failed",
        ),
    )

    result = _with_outside_worktree_detection(
        validation, issue=517, attempt=1, root=tmp_path, worktree=worktree,
        runner=runner, root_dirty_before=root_dirty_before,
    )

    assert result.status == "failed"
    assert any(r.startswith("backend_wrote_outside_worktree:") for r in result.reasons)
    assert any("worklink-leak-517-a1" in r for r in result.reasons)

    # The leaked paths are gone from the working tree; pre-existing dirt survives.
    assert not (tmp_path / "leaked.py").exists()
    assert (tmp_path / "mod.py").read_text() == "v1\n"
    assert (tmp_path / "keep.txt").read_text() == "operator-work\n"
    # ...and the leak is recoverable, not destroyed.
    assert "worklink-leak-517-a1" in git("stash", "list")


# ─── chainlink #517: fail loud on unsafe codex/compute combo ──────────


class _CodexNamedBackend(FakeBackend):
    name = "codex"


def test_codex_on_non_shared_isolated_compute_is_allowed(tmp_path: Path) -> None:
    """A codex worklink on a NON-shared compute (docker_sibling/ecs-style) must NOT
    be blocked: those report shared_filesystem=false because codex runs inside the
    worker's own isolated clone, not against a controller worktree. It is the safe,
    preferred isolated-dispatch path — only controller execution needs the guard
    (chainlink #517)."""
    repo = tmp_path / "repo"
    worktree = repo / ".worklink" / "441-1"
    compute = FakeCompute(shared_filesystem=False)

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True)
            return cp(args)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "diff"] and "--name-only" in args:
            return cp(args, stdout="remote.txt\n")
        if isinstance(args, list) and args[:4] == ["git", "-C", str(worktree), "diff"] and "--stat" in args:
            return cp(args, stdout=" remote.txt | 1 +\n")
        return cp(args)

    registry = BackendRegistry(
        WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_compute"))
    )
    registry.register(_CodexNamedBackend(status="success", write_change=False))
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="codex"
        )
    )

    # Not blocked for the codex/checkout reason, and codex WAS dispatched to the
    # isolated worker compute (the safe path) rather than short-circuited.
    assert "isolated checkout" not in (result.reason or "")
    # Implement launch + the #538 sandboxed test-job launch (non-shared compute,
    # non-empty diff), the latter flagged test_only.
    assert len(compute.specs) == 2
    assert compute.specs[0].test_only is False
    assert compute.specs[1].test_only is True


def test_codex_on_controller_requires_isolated_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive backstop: if the checkout factory ever regresses and hands codex a
    parent-pointing worktree while executing on the controller (shared filesystem),
    the run must fail loud (blocked), not leak into the repo root (chainlink #517)."""
    import mimir.worklink.orchestrator as orch

    repo = tmp_path / "repo"

    def fake_checkout(*_: object, **__: object) -> WorktreeLease:
        # Simulate the regression: a NON-isolated (worktree) lease for codex.
        return WorktreeLease(441, 1, repo, repo / ".worklink" / "441-1", "issue/441-a1", "main")

    monkeypatch.setattr(orch, "_create_backend_checkout", fake_checkout)

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "441"]:
            return cp(args, stdout=ISSUE_JSON)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        return cp(args)

    registry = BackendRegistry(
        WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_compute"))
    )
    registry.register(_CodexNamedBackend(status="success"))
    registry.register_compute(FakeCompute(shared_filesystem=True))

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            441, backend_name="codex"
        )
    )
    assert result.status == "blocked", (result.status, result.reason)
    assert "isolated checkout" in (result.reason or "")


from mimir.worklink.compute import LaunchHandle as _LaunchHandle
from mimir.worklink.orchestrator import _run_remote_test_job as _rjob


class _ScriptedTestCompute:
    """Compute fake whose wait pops scripted (exit, stdout) results (#827)."""

    name = "scripted"

    def __init__(self, results):
        self.results = list(results)
        self.launches = 0

    async def launch(self, spec):
        self.launches += 1
        return _LaunchHandle(substrate="scripted", identifier=f"job-{self.launches}")

    async def wait(self, handle, timeout_s):
        exit_code, stdout = self.results.pop(0)
        if exit_code is None:
            raise RuntimeError("broker wait failed: timed out")
        return ComputeResult(exit_code=exit_code, stdout=stdout, stderr="", handle=handle)

    async def logs(self, handle):
        return "logs-fallback"

    async def cleanup(self, handle):
        return None


def _job_spec():
    return WorkSpec(
        issue_id=793, attempt=1, repo_url="u", base_ref="main", branch="issue/793-a1",
        prompt="p", rules=None, test_command="pytest -q", backend="codex", timeout_s=60,
    )


def test_trusted_job_retries_unobserved_without_burning_attempt(tmp_path: Path) -> None:
    compute = _ScriptedTestCompute([(None, ""), (0, "ok")])
    outcome = asyncio.run(
        _run_remote_test_job(compute, _job_spec(), timeout_s=60, transcript_dir=tmp_path, retries=1)
    )
    assert outcome.exit_code == 0
    assert compute.launches == 2
    assert "unobserved" in (outcome.failure_tail or "")


def test_trusted_job_retries_observed_failure_and_records_both(tmp_path: Path) -> None:
    fail_out = "WORKLINK_TESTS_TAIL_BEGIN\nFAILED tests/a.py::b\nWORKLINK_TESTS_TAIL_END"
    compute = _ScriptedTestCompute([(1, fail_out), (0, "ok")])
    outcome = asyncio.run(
        _run_remote_test_job(compute, _job_spec(), timeout_s=60, transcript_dir=tmp_path, retries=1)
    )
    assert outcome.exit_code == 0
    assert "first: exit 1" in outcome.failure_tail
    wrappers = list(tmp_path.glob("testjob-793-*.json"))
    assert len(wrappers) == 2  # both runs persisted (#826)


def test_trusted_job_fail_twice_folds_failed_with_tail(tmp_path: Path) -> None:
    fail_out = "WORKLINK_TESTS_TAIL_BEGIN\nFAILED tests/a.py::b\nWORKLINK_TESTS_TAIL_END"
    compute = _ScriptedTestCompute([(1, fail_out), (1, fail_out)])
    outcome = asyncio.run(
        _run_remote_test_job(compute, _job_spec(), timeout_s=60, transcript_dir=tmp_path, retries=1)
    )
    assert outcome.exit_code == 1
    assert "FAILED tests/a.py::b" in outcome.failure_tail
    assert compute.launches == 2


def test_trusted_job_retries_zero_is_single_shot(tmp_path: Path) -> None:
    compute = _ScriptedTestCompute([(1, "")])
    outcome = asyncio.run(
        _run_remote_test_job(compute, _job_spec(), timeout_s=60, transcript_dir=tmp_path, retries=0)
    )
    assert outcome.exit_code == 1
    assert compute.launches == 1


def test_worker_emits_structured_test_report(capsys: pytest.CaptureFixture[str]) -> None:
    from mimir.worklink.worker import _print_test_report, extract_test_report

    result = subprocess.CompletedProcess(
        ["pytest"], 1,
        stdout="....\nFAILED tests/test_x.py::test_y - AssertionError: boom\nFAILED tests/test_z.py::test_q\n= 2 failed, 10 passed in 3.2s =",
        stderr="",
    )
    _print_test_report(result)
    out = capsys.readouterr().out
    report = extract_test_report(out)
    assert report is not None
    assert report["exit"] == 1
    assert report["failed"] == ["tests/test_x.py::test_y", "tests/test_z.py::test_q"]
    assert "2 failed, 10 passed" in report["summary"]
    assert extract_test_report("no report here") is None


def test_run_epic_waits_on_launch_handle_and_finalizes(tmp_path: Path) -> None:
    """Regression (mimir review on #1030): run_epic must call
    ``compute.wait(handle, timeout_s)`` — the same 2-arg signature run() uses —
    after launching the factory compute job. A prior bug passed only
    ``timeout_s``; it bound to ``handle`` and dropped ``timeout_s``, so run-epic
    crashed with a TypeError right after launch and transitioned the epic to
    failed. This drives run_epic through launch/wait with a fake compute whose
    factory writes a completed run.json, and asserts a clean completion plus the
    exact wait() arguments.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = repo / ".worklink" / "700-1"
    factory_run = repo / ".opencode" / "factory" / "run.json"

    epic_json = json.dumps(
        {
            "id": 700,
            "title": "epic",
            "description": "build the thing",
            "labels": ["worklink", "worklink:epic", "worklink:ready"],
            "parent_id": None,
            "comments": [],
        }
    )

    class FactoryCompute(FakeCompute):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.waited: tuple[object, int] | None = None

        async def wait(self, handle: WorkSpec, timeout_s: int) -> ComputeResult:
            self.waited = (handle, timeout_s)
            factory_run.parent.mkdir(parents=True, exist_ok=True)
            factory_run.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "heartbeat_at": datetime.now(UTC).isoformat(),
                        "status": "completed",
                        "pr_url": "https://github.com/jasoncarreira/mimir/pull/999",
                    }
                ),
                encoding="utf-8",
            )
            return ComputeResult(exit_code=0, stdout="factory ok", stderr="")

    class FactoryBackend(FakeBackend):
        name = "feature_factory"

    compute = FactoryCompute(shared_filesystem=True)

    def runner(
        args: Sequence[str] | str, **_: object
    ) -> subprocess.CompletedProcess[str]:
        if isinstance(args, list) and args[:4] == ["chainlink", "issue", "show", "700"]:
            return cp(args, stdout=epic_json)
        if isinstance(args, list) and args[:4] == ["git", "-C", str(repo), "config"]:
            return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
        if isinstance(args, list) and args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True, exist_ok=True)
            return cp(args)
        return cp(args)

    registry = BackendRegistry(
        WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_compute"))
    )
    registry.register(FactoryBackend(status="success"))
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(
            home=tmp_path, repo=repo, runner=runner, registry=registry
        ).run_epic(700)
    )

    # With the bug (compute.wait(spec.timeout_s)) run_epic caught a TypeError and
    # returned status="failed"; the fix reaches wait() with the launch handle.
    assert result.status == "completed", (result.status, result.reason)
    assert result.pr_url == "https://github.com/jasoncarreira/mimir/pull/999"
    assert compute.specs, "compute.launch was never called"
    assert compute.waited is not None, "compute.wait was never reached"
    handle, waited_timeout = compute.waited
    assert handle is compute.specs[0], "wait() did not receive the launch handle"
    assert waited_timeout == compute.specs[0].timeout_s
