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


BLOCKED_MARKER = "WORKLINK_BLOCKED:"


def blocked_reason_from_output(stdout: str, stderr: str) -> str | None:
    """Extract a backend-requested Worklink blocked reason from output.

    Backend CLIs are model-driven and may discover a planner/design flaw that
    deterministic Worklink cannot repair.  They can route that case back to the
    planner by emitting a line like ``WORKLINK_BLOCKED: <reason>`` to stdout or
    stderr.  The reason is intentionally plain text and bounded to one line so it
    can be copied into Chainlink evidence comments safely.

    The work-order prompt instructs backends to emit the marker as the *final*
    line of their output, so we take the **last** matching line. That way a
    backend that echoes the prompt's instruction (which mentions the marker
    inline) earlier in its output does not false-trigger a block over the real,
    final decision.
    """
    reason: str | None = None
    for line in f"{stdout}\n{stderr}".splitlines():
        if line.startswith(BLOCKED_MARKER):
            reason = _clean_blocked_reason(line[len(BLOCKED_MARKER) :].strip())
    return reason


def _clean_blocked_reason(reason: str) -> str | None:
    # Keep the Chainlink label/comment payload bounded and one-line even if a
    # backend emits a paragraph after the marker.
    reason = reason.strip().replace("\x00", "")
    if not reason:
        return None
    return reason[:500]


@dataclass(frozen=True)
class RawResult:
    exit_code: int
    transcript_path: Path | None
    backend_status: str
    error: str | None
    blocked_reason: str | None = None


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
