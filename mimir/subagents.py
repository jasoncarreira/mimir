"""Structured subagent role definitions for the deepagents task tool."""

from __future__ import annotations

import re
from typing import Any, Literal

from deepagents.middleware.filesystem import FilesystemPermission
from pydantic import BaseModel, ConfigDict, Field, model_validator


_SEVERITY_SYNONYMS = {
    "nit": "nit",
    "nits": "nit",
    "low": "nit",
    "minor": "nit",
    "important": "important",
    "medium": "important",
    "moderate": "important",
    "blocker": "blocker",
    "blocking": "blocker",
    "critical": "blocker",
    "high": "blocker",
}
_VERDICT_SYNONYMS = {
    "no_concerns": "no_concerns",
    "none": "no_concerns",
    "no_issues": "no_concerns",
    "no_findings": "no_concerns",
    "nits": "nits",
    "nit": "nits",
    "important": "important",
    "blocker": "blocker",
    "blocking": "blocker",
}
_VERDICT_BY_SEVERITY = {
    "nit": "nits",
    "important": "important",
    "blocker": "blocker",
}
_SEVERITY_RANK = {
    "nit": 0,
    "important": 1,
    "blocker": 2,
}


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _normalize_severity(value: Any) -> str:
    # Keep this map intentionally small: only common review synonyms are
    # promoted to blocker/important/nit; unknown severities become important.
    return _SEVERITY_SYNONYMS.get(_normalized_key(value), "important")


def _normalize_verdict(value: Any, findings: list[dict[str, Any]]) -> str:
    normalized = _VERDICT_SYNONYMS.get(_normalized_key(value))
    if normalized is not None:
        return normalized
    if findings:
        most_severe = max(
            (_normalize_severity(finding.get("severity")) for finding in findings),
            key=lambda severity: _SEVERITY_RANK[severity],
        )
        return _VERDICT_BY_SEVERITY[most_severe]
    return "important"


def _as_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _normalize_finding(value: Any) -> dict[str, Any]:
    if isinstance(value, CriticFinding):
        return value.model_dump()
    if not isinstance(value, dict):
        value = {"title": _as_string(value)}

    return {
        "title": _as_string(
            value.get("title") or value.get("summary") or value.get("message")
        ),
        "severity": _normalize_severity(value.get("severity")),
        "evidence": _as_string(
            value.get("evidence") or value.get("file") or value.get("location")
        ),
        "recommendation": _as_string(value.get("recommendation")),
    }


class CriticFinding(BaseModel):
    """One structured concern from a critic-style subagent."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(default="", description="Short human-readable finding title.")
    severity: Literal["nit", "important", "blocker"] = Field(
        default="important",
        description="How seriously the parent should treat this finding."
    )
    evidence: str = Field(
        default="",
        description="Specific evidence, such as a file/line reference or observed behavior."
    )
    recommendation: str = Field(default="", description="Concrete recommended next action.")

    @model_validator(mode="before")
    @classmethod
    def normalize_model_variance(cls, data: Any) -> dict[str, Any]:
        return _normalize_finding(data)


class CriticFindings(BaseModel):
    """Structured response schema for the first typed critic subagent."""

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["no_concerns", "nits", "important", "blocker"] = Field(
        description="Overall critic verdict."
    )
    summary: str = Field(default="", description="Concise summary of the review outcome.")
    findings: list[CriticFinding] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_model_variance(cls, data: Any) -> dict[str, Any]:
        if isinstance(data, CriticFindings):
            return data.model_dump()
        if not isinstance(data, dict):
            data = {"summary": _as_string(data)}

        findings_value = data.get("findings")
        if not isinstance(findings_value, list):
            findings_value = [] if findings_value is None else [findings_value]
        findings = [_normalize_finding(finding) for finding in findings_value]

        open_questions = data.get("open_questions", [])
        if not isinstance(open_questions, list):
            open_questions = [open_questions]

        return {
            "verdict": _normalize_verdict(data.get("verdict"), findings),
            "summary": _as_string(data.get("summary")),
            "findings": findings,
            "open_questions": [_as_string(question) for question in open_questions],
        }


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

    The Worklink epic roles are NOT registered here: they are action-based
    agents whose tools are per-epic-run closures, constructed inside
    ``mimir.worklink.epic_roles`` for each run.
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
        },
    ]
