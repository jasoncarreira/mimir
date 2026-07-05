"""Feature-factory Worklink backend for worklink:epic issues (chainlink #833).

A thin adapter that connects Chainlink epic issues to the external opencode
feature-factory. It reads the factory's run.json atomically, mirrors
progress/gates/PR/terminal state to the Chainlink issue, and handles gate
answers through the factory's file protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Sequence

from ..compute import ComputeResult, WorkSpec
from .base import Caps, RawResult, WorkOrder, blocked_reason_from_output


FACTORY_DIR = ".opencode/factory"
RUN_JSON = "run.json"
GATES_DIR = "gates"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FactoryRunState:
    schema_version: int
    heartbeat_at: str
    status: str
    pr_url: str | None = None
    gates_needed: tuple[str, ...] = ()
    gates_complete: tuple[str, ...] = ()
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    @property
    def is_stale(self) -> bool:
        try:
            heartbeat = datetime.fromisoformat(self.heartbeat_at.replace("Z", "+00:00"))
            now = datetime.now(UTC)
            stale_threshold = 300
            return (now - heartbeat).total_seconds() > stale_threshold
        except (ValueError, TypeError):
            return True


@dataclass(frozen=True)
class FeatureFactoryBackend:
    """Adapter for opencode feature-factory Worklink jobs.

    Handles worklink:epic issues by:
    1. Checking for an existing factory run in the repo
    2. Starting or resuming the factory session
    3. Polling run.json for progress
    4. Mirroring state to the Chainlink issue
    5. Handling gate answers from gates/<gate>.answer files
    """

    bin: str = "opencode"
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    name: str = "feature_factory"
    heartbeat_interval_s: int = 60
    poll_interval_s: int = 10

    def capabilities(self) -> Caps:
        return Caps(
            tool_category="feature-factory",
            persistent_sessions=True,
            json_output=False,
            native_pr_creation=False,
            worktree_safe=False,
            quota_pool=None,
        )

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
        factory_path = order.worktree / FACTORY_DIR
        run_json_path = factory_path / RUN_JSON

        existing_state = self._read_run_json(order.worktree)
        command = self._factory_command(existing_state, order.worktree)

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
            backend_config={
                "bin": self.bin,
                "args": list(self.extra_args),
                "factory_path": str(factory_path),
                "run_json_path": str(run_json_path),
            },
            local_worktree=order.worktree,
            local_argv=command,
        )

    def _factory_command(self, existing_state: FactoryRunState | None, worktree: Path) -> tuple[str, ...]:
        base_args = [self.bin, "feature-factory"]
        if existing_state is None:
            return (*base_args, "start", "--dir", str(worktree), *self.extra_args)
        elif existing_state.is_terminal:
            return (*base_args, "start", "--dir", str(worktree), *self.extra_args)
        else:
            return (*base_args, "resume", "--dir", str(worktree), *self.extra_args)

    def _read_run_json(self, worktree: Path) -> FactoryRunState | None:
        run_json_path = worktree / FACTORY_DIR / RUN_JSON
        if not run_json_path.exists():
            return None

        try:
            content = run_json_path.read_text(encoding="utf-8")
            data = json.loads(content)

            if not isinstance(data, dict):
                return None

            schema_version = data.get("schema_version", 0)
            if schema_version != SCHEMA_VERSION:
                return None

            heartbeat_at = data.get("heartbeat_at")
            if not heartbeat_at:
                return None

            status = data.get("status", "unknown")
            pr_url = data.get("pr_url")
            gates_needed = tuple(data.get("gates_needed", []))
            gates_complete = tuple(data.get("gates_complete", []))
            error = data.get("error")

            return FactoryRunState(
                schema_version=schema_version,
                heartbeat_at=heartbeat_at,
                status=status,
                pr_url=pr_url,
                gates_needed=gates_needed,
                gates_complete=gates_complete,
                error=error,
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    async def interpret(self, order: WorkOrder, result: object) -> RawResult:
        if not isinstance(result, ComputeResult):
            raise TypeError("FeatureFactoryBackend.interpret expects ComputeResult")

        if result.launch_error:
            return RawResult(-1, None, "backend_error", result.launch_error)

        state = self._read_run_json(order.worktree)

        if state is None:
            return RawResult(-1, None, "failed", "factory run.json not found")

        if state.is_stale:
            return RawResult(-1, None, "stale_heartbeat", f"factory heartbeat stale: {state.heartbeat_at}")

        if state.error:
            return RawResult(-1, None, "failed", state.error)

        if state.status == "completed":
            if state.gates_needed:
                blocked_reason = f"gates pending: {', '.join(state.gates_needed)}"
                return RawResult(0, None, "blocked", blocked_reason)
            return RawResult(0, None, "completed", None)

        if state.status == "failed":
            return RawResult(-1, None, "failed", state.error or "factory run failed")

        if state.gates_needed:
            blocked_reason = f"gate required: {state.gates_needed[0]}"
            return RawResult(0, None, "blocked", blocked_reason)

        return RawResult(0, None, "in_progress", None)


def read_factory_run_state(repo_path: Path) -> FactoryRunState | None:
    """Read the factory run state from a repository.

    This is a standalone function for use by the poller/adapter to check
    factory state without launching a full backend.
    """
    run_json_path = repo_path / FACTORY_DIR / RUN_JSON
    if not run_json_path.exists():
        return None

    try:
        content = run_json_path.read_text(encoding="utf-8")
        data = json.loads(content)

        if not isinstance(data, dict):
            return None

        schema_version = data.get("schema_version", 0)
        if schema_version != SCHEMA_VERSION:
            return None

        heartbeat_at = data.get("heartbeat_at")
        if not heartbeat_at:
            return None

        return FactoryRunState(
            schema_version=schema_version,
            heartbeat_at=heartbeat_at,
            status=data.get("status", "unknown"),
            pr_url=data.get("pr_url"),
            gates_needed=tuple(data.get("gates_needed", [])),
            gates_complete=tuple(data.get("gates_complete", [])),
            error=data.get("error"),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def gate_answer_path(repo_path: Path, gate: str) -> Path:
    """Get the path to a gate answer file."""
    return repo_path / FACTORY_DIR / GATES_DIR / f"{gate}.answer"


def read_gate_answer(repo_path: Path, gate: str) -> str | None:
    """Read a gate answer from the factory's file protocol."""
    answer_path = gate_answer_path(repo_path, gate)
    if not answer_path.exists():
        return None
    try:
        return answer_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def write_gate_answer(repo_path: Path, gate: str, answer: str) -> None:
    """Write a gate answer to the factory's file protocol."""
    answer_path = gate_answer_path(repo_path, gate)
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    answer_path.write_text(answer, encoding="utf-8")


def has_concurrent_factory_session(repo_path: Path) -> bool:
    """Check if there's already an active factory session running.

    Returns True if there's a run.json with a non-terminal status and
    a non-stale heartbeat.
    """
    state = read_factory_run_state(repo_path)
    if state is None:
        return False
    if state.is_terminal:
        return False
    if state.is_stale:
        return False
    return True
