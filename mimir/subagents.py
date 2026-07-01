"""Structured subagent role definitions for the deepagents task tool."""

from __future__ import annotations

from typing import Literal

from deepagents.middleware.filesystem import FilesystemPermission
from pydantic import BaseModel, Field


class CriticFinding(BaseModel):
    """One structured concern from a critic-style subagent."""

    title: str = Field(description="Short human-readable finding title.")
    severity: Literal["nit", "important", "blocker"] = Field(
        description="How seriously the parent should treat this finding."
    )
    evidence: str = Field(
        description="Specific evidence, such as a file/line reference or observed behavior."
    )
    recommendation: str = Field(description="Concrete recommended next action.")


class CriticFindings(BaseModel):
    """Structured response schema for the first typed critic subagent."""

    verdict: Literal["no_concerns", "nits", "important", "blocker"] = Field(
        description="Overall critic verdict."
    )
    summary: str = Field(description="Concise summary of the review outcome.")
    findings: list[CriticFinding] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


CRITIC_STRUCTURED_PROMPT = """You are critic-structured, a narrow review subagent.

Review the user's supplied artifact or plan skeptically. Return only the structured
CriticFindings response requested by the runtime. Use findings for concrete concerns;
use verdict=no_concerns with an empty findings list when nothing material is wrong.
Anchor each finding in specific evidence and make every recommendation actionable.
"""


def readonly_filesystem_permissions() -> list[FilesystemPermission]:
    """Return filesystem rules that permit reads but deny writes everywhere.

    DeepAgents filesystem permissions default to allow when no rule matches, so the
    read-only profile needs an explicit write-deny catch-all. This is accidental-
    overreach protection for built-in filesystem tools, not a security sandbox for
    process execution.
    """

    return [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]


def build_mimir_subagents() -> list[dict]:
    """Build explicit Mimir subagent specs for ``create_deep_agent``.

    The default DeepAgents ``general-purpose`` subagent is still auto-added because
    this list intentionally does not include a spec named ``general-purpose``.
    """

    return [
        {
            "name": "critic-structured",
            "description": (
                "Skeptical critic that returns validated JSON with verdict, "
                "summary, findings, and open_questions. Read-only filesystem "
                "profile; no shell/process execution by default."
            ),
            "system_prompt": CRITIC_STRUCTURED_PROMPT,
            # Do not inherit Mimir's broad parent tool surface (especially
            # shell/process-capable tools). DeepAgents still adds its built-in
            # filesystem tools via middleware, governed by the read-only
            # permissions below.
            "tools": [],
            "permissions": readonly_filesystem_permissions(),
            "response_format": CriticFindings,
        }
    ]
