"""Protocol types for Worklink tool backends.

Backends own CLI session semantics only: rendering the tool-specific work spec,
capturing transcripts, and mapping tool-specific failures into common status
strings. Claiming, compute launch/wait/cancel/cleanup, worktree lifecycle,
evidence validation, and state transitions stay in shared Worklink plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..compute import ComputeResult, WorkSpec


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


@dataclass(frozen=True)
class RawResult:
    exit_code: int
    transcript_path: Path | None
    backend_status: str
    error: str | None


class ToolBackend(Protocol):
    name: str

    def capabilities(self) -> Caps: ...

    def work_spec(
        self,
        order: WorkOrder,
        *,
        attempt: int,
        repo_url: str,
        base_ref: str,
        branch: str,
        test_command: str,
    ) -> WorkSpec: ...

    async def interpret(self, order: WorkOrder, result: ComputeResult) -> RawResult: ...
