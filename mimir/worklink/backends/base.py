"""Protocol types for Worklink tool backends.

Backends own CLI session mechanics only: invoking the tool, capturing its
transcript, enforcing per-run timeouts, and mapping tool-specific failures into
common status strings. Claiming, worktree lifecycle, evidence validation, and
state transitions stay in the shared Worklink orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..compute import ComputeBackend, WorkSpec


@dataclass(frozen=True)
class Caps:
    tool_category: str
    persistent_sessions: bool
    json_output: bool
    native_pr_creation: bool
    worktree_safe: bool
    quota_pool: str | None


@dataclass(frozen=True)
class WorkOrder:
    issue_id: int
    worktree: Path
    prompt: str
    rules: str | None
    timeout_s: int
    env: dict[str, str] = field(default_factory=dict)
    transcript_root: Path | None = None
    attempt: int = 0
    repo_url: str | None = None
    base_ref: str = "main"
    branch: str = ""
    test_command: str = ""
    creds_ref: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RawResult:
    exit_code: int
    transcript_path: Path | None
    backend_status: str
    error: str | None


class ToolBackend(Protocol):
    name: str

    def capabilities(self) -> Caps: ...

    def work_spec(self, order: WorkOrder) -> WorkSpec: ...

    async def run(
        self, order: WorkOrder, *, compute: ComputeBackend | None = None
    ) -> RawResult: ...
