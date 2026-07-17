from __future__ import annotations

import json

from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.subagents import _build_task_tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain.tools import ToolRuntime
import pytest

from mimir.subagents import (
    CriticFinding,
    CriticFindings,
    build_mimir_subagents,
    readonly_filesystem_permissions,
)


def test_build_mimir_subagents_registers_structured_critic_without_replacing_gp() -> None:
    specs = build_mimir_subagents()

    # The Worklink epic roles are per-run tool-armed agents constructed inside
    # the retired epic roles module (removed #830).
    assert [spec["name"] for spec in specs] == [
        "general-purpose",
        "critic-structured",
    ]
    critic = specs[1]
    assert critic["response_format"] is CriticFindings
    assert critic["tools"] == []
    assert [middleware.__class__.__name__ for middleware in critic["middleware"]] == [
        "BudgetGateMiddleware",
        "StructuredOutputRetryMiddleware",
    ]
    assert "Read-only" in critic["description"]


def test_every_subagent_runs_tool_calls_through_budget_gate() -> None:
    specs = build_mimir_subagents()

    assert all(
        any(
            middleware.__class__.__name__ == "BudgetGateMiddleware"
            for middleware in spec["middleware"]
        )
        for spec in specs
    )


def test_readonly_permissions_deny_write_catchall() -> None:
    rules = readonly_filesystem_permissions()

    assert len(rules) == 1
    assert rules[0].operations == ["write"]
    assert rules[0].paths == ["/**"]
    assert rules[0].mode == "deny"


def test_readonly_permissions_block_filesystem_write_tool() -> None:
    middleware = FilesystemMiddleware(
        backend=StateBackend(),
        _permissions=readonly_filesystem_permissions(),
    )
    write_tool = next(tool for tool in middleware.tools if tool.name == "write_file")
    runtime = ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="toolu-write",
        store=None,
    )

    result = write_tool.func(
        file_path="/blocked.txt",
        content="should not be written",
        runtime=runtime,
    )

    assert result.status == "error"
    assert result.content == "Error: permission denied for write on /blocked.txt"


def test_critic_findings_parses_observed_model_variance_payload() -> None:
    structured = CriticFindings.model_validate(
        {
            "verdict": "findings",
            "summary": "Two concerns found.",
            "ignored": "extra response field",
            "findings": [
                {
                    "title": "Missing guard",
                    "severity": "high",
                    "evidence": "mimir/subagents.py crashes on literal mismatch",
                    "file": "mimir/subagents.py",
                },
                {
                    "title": "Retry loses review",
                    "severity": "medium",
                    "evidence": "agent.astream failed before returning a result",
                    "file": "mimir/subagents.py",
                },
            ],
        }
    )

    assert structured.verdict == "blocker"
    assert [finding.severity for finding in structured.findings] == [
        "blocker",
        "important",
    ]
    assert structured.findings[0].recommendation == ""
    assert structured.findings[0].model_dump() == {
        "title": "Missing guard",
        "severity": "blocker",
        "evidence": "mimir/subagents.py crashes on literal mismatch",
        "recommendation": "",
    }


@pytest.mark.parametrize(
    ("raw_severity", "expected"),
    [
        ("critical", "blocker"),
        ("high", "blocker"),
        ("medium", "important"),
        ("low", "nit"),
        ("minor", "nit"),
        ("Not Mapped", "important"),
    ],
)
def test_critic_finding_normalizes_severity_synonyms(
    raw_severity: str, expected: str
) -> None:
    finding = CriticFinding.model_validate(
        {
            "title": "Concern",
            "severity": raw_severity,
            "evidence": "evidence",
            "recommendation": "fix",
        }
    )

    assert finding.severity == expected


def test_critic_findings_unknown_verdict_falls_back_to_most_severe_finding() -> None:
    structured = CriticFindings.model_validate(
        {
            "verdict": "findings",
            "summary": "Fallback from findings.",
            "findings": [
                {
                    "title": "Minor concern",
                    "severity": "minor",
                    "evidence": "small issue",
                    "recommendation": "polish",
                },
                {
                    "title": "Important concern",
                    "severity": "medium",
                    "evidence": "larger issue",
                    "recommendation": "fix",
                },
            ],
        }
    )

    assert structured.verdict == "important"


def test_critic_findings_unknown_verdict_without_findings_defaults_to_important() -> None:
    structured = CriticFindings.model_validate(
        {"verdict": "findings", "summary": "No concrete findings.", "findings": []}
    )

    assert structured.verdict == "important"


def test_critic_findings_malformed_finding_degrades_to_best_effort() -> None:
    structured = CriticFindings.model_validate(
        {
            "verdict": "findings",
            "summary": "Malformed child item.",
            "findings": [
                "plain finding text",
                {
                    "message": "message-only finding",
                    "severity": {"unexpected": "shape"},
                    "file": "mimir/subagents.py",
                },
            ],
        }
    )

    assert structured.verdict == "important"
    assert structured.findings[0].model_dump() == {
        "title": "plain finding text",
        "severity": "important",
        "evidence": "",
        "recommendation": "",
    }
    assert structured.findings[1].model_dump() == {
        "title": "message-only finding",
        "severity": "important",
        "evidence": "mimir/subagents.py",
        "recommendation": "",
    }


def test_critic_findings_well_formed_round_trip_is_unchanged() -> None:
    payload = {
        "verdict": "important",
        "summary": "One test gap remains.",
        "findings": [
            {
                "title": "Missing regression test",
                "severity": "important",
                "evidence": "tests/test_example.py has no coverage for the new branch",
                "recommendation": "Add a focused test before shipping.",
            }
        ],
        "open_questions": ["Should this path be covered at the API boundary?"],
    }

    assert CriticFindings.model_validate(payload).model_dump() == payload


def test_task_tool_returns_structured_response_as_json_tool_message() -> None:
    finding = CriticFinding(
        title="Missing regression test",
        severity="important",
        evidence="tests/test_example.py has no coverage for the new branch",
        recommendation="Add a focused test before shipping.",
    )
    structured = CriticFindings(
        verdict="important",
        summary="One test gap remains.",
        findings=[finding],
        open_questions=[],
    )
    runnable = RunnableLambda(
        lambda state: {
            "messages": [AIMessage(content="unstructured fallback should not leak")],
            "structured_response": structured,
        }
    )
    tool = _build_task_tool(
        [
            {
                "name": "critic-structured",
                "description": "returns schema JSON",
                "runnable": runnable,
            }
        ]
    )
    runtime = ToolRuntime(
        state={"messages": [HumanMessage(content="parent prompt")]},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="toolu-test",
        store=None,
    )

    result = tool.func(
        description="review this",
        subagent_type="critic-structured",
        runtime=runtime,
    )

    assert result.update["messages"][0] == ToolMessage(
        json.dumps(structured.model_dump(), separators=(",", ":")),
        tool_call_id="toolu-test",
    )
    payload = json.loads(result.update["messages"][0].content)
    assert payload["verdict"] == "important"
    assert payload["findings"][0]["severity"] == "important"
    assert "unstructured fallback" not in result.update["messages"][0].content


def test_task_tool_validation_failure_surfaces_instead_of_falling_back_to_prose() -> None:
    def invalid_structured_response(_state):
        # Simulate a child graph whose structured-output strategy rejected the model output.
        raise ValueError("structured response validation failed: missing verdict")

    tool = _build_task_tool(
        [
            {
                "name": "critic-structured",
                "description": "returns schema JSON",
                "runnable": RunnableLambda(invalid_structured_response),
            }
        ]
    )
    runtime = ToolRuntime(
        state={"messages": [HumanMessage(content="parent prompt")]},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="toolu-test",
        store=None,
    )

    with pytest.raises(ValueError, match="structured response validation failed"):
        tool.func(
            description="review this",
            subagent_type="critic-structured",
            runtime=runtime,
        )
