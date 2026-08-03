"""Protocol types for Worklink tool backends.

Backends own CLI session semantics only: rendering the tool-specific work spec,
capturing transcripts, and mapping tool-specific failures into common status
strings. Claiming, compute launch/wait/cancel/cleanup, checkout lifecycle,
evidence validation, and state transitions stay in shared Worklink plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ..compute import ComputeResult, WorkSpec


@dataclass(frozen=True)
class Caps:
    tool_category: str
    persistent_sessions: bool
    json_output: bool
    native_pr_creation: bool
    quota_pool: str | None


class CheckoutShape(StrEnum):
    """Server-owned containment shape for a backend attempt checkout."""

    ISOLATED_CLONE = "isolated_clone"


@dataclass(frozen=True)
class WorkOrder:
    issue_id: int
    checkout: Path
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

    The marker is honored only when it is the **final non-empty line** of stdout
    or stderr — exactly the convention the work-order prompt enforces ("emit it
    as the final line and stop"). Enforcing the final-line rule (not merely
    last-match-anywhere) means a backend that echoes the prompt's marker
    instruction near the top and then *completes normally* is not mislabeled
    blocked: the real final line is its success output, not the echoed marker.
    """
    for stream in (stdout, stderr):
        last = last_nonempty_line(stream)
        if last is None:
            continue
        stripped = last.strip()
        if stripped.startswith(BLOCKED_MARKER):
            reason = _clean_blocked_reason(stripped[len(BLOCKED_MARKER) :].strip())
            if reason:
                return reason
    return None


def last_nonempty_line(text: str) -> str | None:
    """Return the last line with non-whitespace content, or None.

    Shared by the backend signal readers so "the executor reported this" is
    distinguished from "this text appeared somewhere in the output" the same way
    for every marker — see ``blocked_reason_from_output`` and OpenCode's
    permission-refusal reader.
    """
    for line in reversed(text.splitlines()):
        if line.strip():
            return line
    return None


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
    output_overflow: bool = False


class ToolBackend(Protocol):
    name: str
    checkout_shape: CheckoutShape

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


def checkout_shape_for_backend(backend: ToolBackend) -> CheckoutShape:
    """Return a backend's shape, failing safely for older or third-party backends."""
    return getattr(backend, "checkout_shape", CheckoutShape.ISOLATED_CLONE)
